#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from runtime.sandbox_repair_playbook import (  # noqa: E402
    JsonSandboxRepairPlaybook,
    JsonSandboxRepairRequest,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def request(operation_id: str, value: str) -> JsonSandboxRepairRequest:
    return JsonSandboxRepairRequest(
        operation_id=operation_id,
        candidate_json=value,
        expected_digest=hashlib.sha256(value.encode("utf-8")).hexdigest(),
    )


def configure(store: WorldStateStore, mode: str, epoch: int) -> None:
    def update(config: dict[str, Any]) -> None:
        playbooks = config.setdefault("playbooks", {})
        playbooks["sandbox_repair_json"] = {
            "mode": mode,
            "mode_epoch": epoch,
            "allowed_modes": [
                "disabled",
                "record_only",
                "shadow",
                "scoped_canary",
            ],
        }

    store.mutate_json("ops_config.json", update)


def main() -> None:
    with TemporaryDirectory(prefix="veyra-sandbox-repair-") as tmp:
        base = Path(tmp)
        store = WorldStateStore(base / "state")
        outside = base / "workspace-sentinel.json"
        outside.write_text('{"workspace":"unchanged"}\n', encoding="utf-8")
        clock = Clock()
        playbook = JsonSandboxRepairPlaybook(
            state_store=store,
            now=clock,
        )
        sandbox_base = store.root / "runtime" / "sandbox_repairs"
        healthy = request(
            "op-shadow",
            '{ "z": 2, "nested": {"ok": true}, "a": 1 }',
        )

        shadow = playbook.run(healthy)
        expect(
            shadow["status"] == "shadow_candidate_valid"
            and shadow["effective_autonomy_level"] == "A1"
            and shadow["production_effect_status"] == "not_started"
            and shadow["promotion_authorized"] is False,
            "default shadow validates without claiming production repair",
            shadow,
        )
        expect(
            not sandbox_base.exists()
            and store.read_json("sandbox_repair_state.json")["operations"]
            == {}
            and outside.read_text(encoding="utf-8")
            == '{"workspace":"unchanged"}\n',
            "default shadow creates no sandbox or workspace effect",
        )

        invalid_cases = [
            JsonSandboxRepairRequest(
                operation_id="op-bad-digest",
                candidate_json='{"ok":true}',
                expected_digest="0" * 64,
            ),
            request("op-duplicate", '{"same":1,"same":2}'),
            request("op-nonfinite", '{"value":NaN}'),
            request("op-list", "[1,2,3]"),
            request("bad operation id", '{"ok":true}'),
            request("op-oversize", '{"value":"' + ("x" * 262_145) + '"}'),
        ]
        for index, invalid in enumerate(invalid_cases):
            result = playbook.run(invalid)
            expect(
                result["status"] == "blocked"
                and result["effective_autonomy_level"] == "A0"
                and result["production_effect_status"] == "not_started",
                f"strict invalid candidate {index + 1} fails closed",
                result,
            )
        expect(
            not sandbox_base.exists(),
            "invalid candidates never create a sandbox",
        )

        configure(store, "scoped_canary", 1)
        canary_request = request(
            "op-canary",
            '{ "z": 2, "nested": {"ok": true}, "a": 1 }',
        )
        canary = playbook.run(canary_request)
        expect(
            canary["status"] == "sandbox_verified_candidate"
            and canary["operation_state"] == "completed"
            and canary["evidence_status"]
            == "verified_in_private_sandbox"
            and canary["production_effect_status"] == "not_started"
            and canary["promotion_authorized"] is False,
            "scoped canary verifies only a private JSON candidate",
            canary,
        )
        operation_dirs = sorted(sandbox_base.glob("op_*"))
        expect(
            len(operation_dirs) == 1,
            "one exact private operation directory created",
            operation_dirs,
        )
        candidate_path = operation_dirs[0] / "candidate.json"
        manifest_path = operation_dirs[0] / "manifest.json"
        expect(
            candidate_path.read_text(encoding="utf-8")
            == '{"a":1,"nested":{"ok":true},"z":2}\n'
            and json.loads(manifest_path.read_text(encoding="utf-8"))[
                "promotion_authorized"
            ]
            is False
            and outside.read_text(encoding="utf-8")
            == '{"workspace":"unchanged"}\n',
            "canonical artifact and non-promotion manifest stay private",
        )
        public_json = json.dumps(canary, ensure_ascii=False)
        expect(
            "workspace-sentinel" not in public_json
            and canary_request.candidate_json not in public_json,
            "public result does not expose candidate content or workspace path",
            canary,
        )

        before_files = {
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in operation_dirs[0].iterdir()
        }
        repeated = playbook.run(canary_request)
        after_files = {
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in operation_dirs[0].iterdir()
        }
        expect(
            repeated["status"] == "sandbox_verified_candidate"
            and before_files == after_files
            and len(list(sandbox_base.glob("op_*"))) == 1,
            "completed operation is idempotent and never replayed",
            repeated,
        )

        concurrent_request = request("op-concurrent", '{"value":7}')
        with ThreadPoolExecutor(max_workers=8) as pool:
            concurrent = list(
                pool.map(
                    lambda _: playbook.run(concurrent_request),
                    range(8),
                )
            )
        expect(
            all(
                item["status"] == "sandbox_verified_candidate"
                for item in concurrent
            )
            and len(list(sandbox_base.glob("op_*"))) == 2,
            "concurrent callers produce one exact sandbox effect",
            concurrent,
        )

        conflicting = playbook.run(
            request("op-concurrent", '{"value":8}')
        )
        expect(
            conflicting["status"] == "operation_conflict"
            and conflicting["effective_autonomy_level"] == "A0"
            and len(list(sandbox_base.glob("op_*"))) == 2,
            "operation id cannot be rebound to another candidate",
            conflicting,
        )

        stale_request = request("op-stale-claim", '{"stale":true}')
        validated, error = playbook._validate_request(  # noqa: SLF001
            stale_request
        )
        expect(error is None and validated is not None, "stale fixture valid")
        mode_context = playbook._mode_context()  # noqa: SLF001
        claimed, disposition = playbook._claim(  # noqa: SLF001
            request=stale_request,
            validated=validated,
            binding_digest=playbook._binding_digest(  # noqa: SLF001
                mode_context
            ),
            mode_context=mode_context,
        )
        expect(
            disposition == "claimed" and claimed["state"] == "claimed",
            "durable claim fixture created without a sandbox effect",
            claimed,
        )
        clock.advance(31)
        indeterminate = playbook.run(stale_request)
        expect(
            indeterminate["status"] == "indeterminate"
            and indeterminate["effective_autonomy_level"] == "A0"
            and indeterminate["production_effect_status"] == "not_started"
            and len(list(sandbox_base.glob("op_*"))) == 2,
            "expired claim becomes indeterminate without replay",
            indeterminate,
        )

        configure(store, "disabled", 2)
        disabled = playbook.run(request("op-disabled", '{"ok":true}'))
        expect(
            disabled["status"] == "disabled"
            and disabled["effective_autonomy_level"] == "A0"
            and len(list(sandbox_base.glob("op_*"))) == 2,
            "disabled mode has zero sandbox effects",
            disabled,
        )

        corrupt_store = WorldStateStore(base / "corrupt-state")
        corrupt_path = corrupt_store.path_for("sandbox_repair_state.json")
        corrupt_path.write_text("{broken", encoding="utf-8")
        corrupt = JsonSandboxRepairPlaybook(
            state_store=corrupt_store
        ).run(request("op-corrupt", '{"ok":true}'))
        expect(
            corrupt["status"] == "fault"
            and corrupt["effective_autonomy_level"] == "A0",
            "corrupt durable state faults before any sandbox effect",
            corrupt,
        )

        chain_store = WorldStateStore(base / "chain-race-state")
        configure(chain_store, "scoped_canary", 1)
        chain_outside = base / "chain-race-outside"
        chain_outside.mkdir()
        chain_parent = chain_store.root.resolve() / "private-sandbox"
        displaced_parent = (
            chain_store.root.resolve() / "private-sandbox-pinned"
        )
        chain_ready = threading.Event()
        chain_swapped = threading.Event()

        def replace_pinned_parent() -> None:
            if not chain_ready.wait(timeout=5):
                raise RuntimeError("chain race hook was not reached")
            chain_parent.rename(displaced_parent)
            chain_parent.symlink_to(
                chain_outside,
                target_is_directory=True,
            )
            chain_swapped.set()

        chain_playbook = JsonSandboxRepairPlaybook(
            state_store=chain_store,
            sandbox_base=chain_parent / "sandbox_repairs",
        )

        def pause_after_runtime_pinned(component_path: Path) -> None:
            if component_path == chain_parent:
                chain_ready.set()
                if not chain_swapped.wait(timeout=5):
                    raise RuntimeError(
                        "chain replacement did not complete"
                    )

        chain_playbook._after_directory_component_pinned = (  # type: ignore[method-assign]  # noqa: SLF001
            pause_after_runtime_pinned
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            chain_swap = pool.submit(replace_pinned_parent)
            chain_result = chain_playbook.run(
                request("op-chain-race", '{"safe":true}')
            )
            chain_swap.result(timeout=5)
        expect(
            chain_result["status"] == "indeterminate"
            and chain_swapped.is_set()
            and not any(chain_outside.rglob("candidate.json"))
            and not any(chain_outside.rglob("manifest.json")),
            (
                "concurrent parent replacement after openat pinning "
                "cannot redirect directory creation"
            ),
            chain_result,
        )

        reopen_store = WorldStateStore(base / "reopen-race-state")
        configure(reopen_store, "scoped_canary", 1)
        reopen_outside = base / "reopen-race-outside"
        reopen_outside.mkdir()
        operation_holder: dict[str, Path] = {}
        reopen_ready = threading.Event()
        reopen_swapped = threading.Event()

        def replace_operation_chain() -> None:
            if not reopen_ready.wait(timeout=5):
                raise RuntimeError("operation race hook was not reached")
            operation_path = operation_holder["path"]
            sandbox_path = operation_path.parent
            displaced = sandbox_path.with_name(
                "sandbox_repairs-pinned"
            )
            sandbox_path.rename(displaced)
            (reopen_outside / operation_path.name).mkdir()
            sandbox_path.symlink_to(
                reopen_outside,
                target_is_directory=True,
            )
            reopen_swapped.set()

        reopen_playbook = JsonSandboxRepairPlaybook(
            state_store=reopen_store
        )

        def pause_before_pinned_write(operation_path: Path) -> None:
            operation_holder["path"] = operation_path
            reopen_ready.set()
            if not reopen_swapped.wait(timeout=5):
                raise RuntimeError(
                    "operation path replacement did not complete"
                )

        reopen_playbook._after_operation_directory_pinned = (  # type: ignore[method-assign]  # noqa: SLF001
            pause_before_pinned_write
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            reopen_swap = pool.submit(replace_operation_chain)
            reopen_result = reopen_playbook.run(
                request("op-reopen-race", '{"safe":true}')
            )
            reopen_swap.result(timeout=5)
        outside_operation = (
            reopen_outside / operation_holder["path"].name
        )
        expect(
            reopen_result["status"] == "indeterminate"
            and reopen_swapped.is_set()
            and outside_operation.is_dir()
            and not list(outside_operation.iterdir()),
            (
                "concurrent operation-path symlink swap cannot redirect "
                "candidate or manifest writes"
            ),
            reopen_result,
        )

        symlink_store = WorldStateStore(base / "symlink-state")
        configure(symlink_store, "scoped_canary", 1)
        symlink_base = symlink_store.root / "runtime" / "sandbox_repairs"
        symlink_outside = base / "symlink-outside"
        symlink_outside.mkdir()
        symlink_base.symlink_to(symlink_outside, target_is_directory=True)
        symlink_result = JsonSandboxRepairPlaybook(
            state_store=symlink_store
        ).run(request("op-symlink", '{"ok":true}'))
        expect(
            symlink_result["status"] == "indeterminate"
            and not list(symlink_outside.iterdir()),
            "symlink sandbox base is rejected with zero outside effect",
            symlink_result,
        )

    print("sandbox_repair_playbook_smoke: ok")


if __name__ == "__main__":
    main()
