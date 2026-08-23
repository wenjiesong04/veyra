#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore  # noqa: E402
from core.world_state import StateNestedRootError, WorldStateStore  # noqa: E402
from scripts.repair_nested_state_root import (  # noqa: E402
    NestedStateRepairError,
    PRESERVED_RUNTIME_ARTIFACT_DIRS,
    repair_nested_state_root,
)
from routers.debug_audit import CoreModelConfigRequest, build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()
        legacy_payload = {"current_goal": "migrate-me", "preferences": {"language": "zh-CN"}}
        (root / "user_world.json").write_text(json.dumps(legacy_payload), encoding="utf-8")
        (root / "event_log.jsonl").write_text('{"event":"legacy"}\n', encoding="utf-8")

        store = WorldStateStore(root)
        expect((root / "user" / "user_world.json").exists(), "legacy user_world migrated")
        expect(not (root / "user_world.json").exists(), "legacy user_world removed from root")
        expect(store.read_json("user_world.json").get("current_goal") == "migrate-me", "migrated user_world readable")
        expect((root / "logs" / "event_log.jsonl").exists(), "legacy jsonl migrated")
        expect(store.read_jsonl("event_log.jsonl", limit=1)[0].get("event") == "legacy", "migrated jsonl readable")

        store.write_json("local_world.json", {"current_project": "/tmp/demo", "probes": {}})
        expect((root / "local" / "local_world.json").exists(), "new writes use layout paths")
        expect(store.relative_path_for("user_goals.json") == "user/user_goals.json", "user goals path mapping")
        expect(store.relative_path_for("task_state.json") == "runtime/task_state.json", "runtime path mapping")

        saved_env = {key: os.environ.get(key) for key in ("VEYRA_STATE_DIR", "VEYRA_STATE_ROOT", "VEYRA_AGENCY_DIR", "VEYRA_AGENCY_ROOT", "VEYRA_ENV")}
        cwd = Path.cwd()
        try:
            os.environ["VEYRA_STATE_DIR"] = str(Path(tmp) / "state-custom")
            os.environ.pop("VEYRA_STATE_ROOT", None)
            env_store = WorldStateStore()
            expect(env_store.root == Path(tmp) / "state-custom", "VEYRA_STATE_DIR selects isolated state root", env_store.root)

            os.environ.pop("VEYRA_STATE_DIR", None)
            os.environ["VEYRA_ENV"] = "test"
            os.chdir(tmp)
            test_store = WorldStateStore()
            expect(test_store.root == Path("state") / "test", "VEYRA_ENV selects state namespace", test_store.root)

            os.environ["VEYRA_AGENCY_DIR"] = str(Path(tmp) / "agency-custom")
            agency = AgencyCore(agency_root="agency", model_assist_enabled=False)
            expect(agency.agency_root == Path(tmp) / "agency-custom", "VEYRA_AGENCY_DIR selects isolated agency root", agency.agency_root)

            os.environ.pop("VEYRA_AGENCY_DIR", None)
            os.environ["VEYRA_ENV"] = "test"
            agency_test = AgencyCore(agency_root="agency", model_assist_enabled=False)
            expect(agency_test.agency_root == Path("agency") / "test", "VEYRA_ENV selects agency namespace", agency_test.agency_root)
        finally:
            os.chdir(cwd)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        loop = ActiveRuntimeLoop.__new__(ActiveRuntimeLoop)
        compacted = loop._compact_result(
            {
                "status": "success",
                "refreshed": [
                    {
                        "claim": {"key": "weather.beijing", "source": "probe"},
                        "probe_result": {"status": "success", "raw": "x" * 5000},
                    }
                ],
            }
        )
        compacted_text = json.dumps(compacted, ensure_ascii=False)
        expect("raw" not in compacted_text and len(compacted_text) < 500, "active loop compact result drops raw probe payloads", compacted)

        class FakeReasoning:
            def status(self) -> dict[str, str]:
                return {"status": "ok"}

        class FakeAwarenessLoop:
            core_reasoning = FakeReasoning()

        router = build_debug_audit_router({"state_store": store, "awareness_loop": FakeAwarenessLoop()})
        endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/core/model/config")
        try:
            asyncio.run(endpoint(CoreModelConfigRequest(api_key="direct-secret")))
            raise AssertionError("direct api_key accepted")
        except HTTPException as exc:
            expect(exc.status_code == 422, "direct core model api_key rejected", exc.status_code)

        config = store.read_json("agent_config.json")
        config.setdefault("core_model", {})["api_key"] = "legacy-secret"
        store.write_json("agent_config.json", config)
        asyncio.run(endpoint(CoreModelConfigRequest(api_key="", api_key_env="VEYRA_CORE_MODEL_API_KEY")))
        updated_config = store.read_json("agent_config.json")
        expect("api_key" not in updated_config.get("core_model", {}), "empty api_key clears legacy direct secret")

        for index in range(4):
            store.append_jsonl("action_record.jsonl", {"route": f"retention_test_{index}", "status": "success"})
        policy = RetentionPolicy(store, limits={"action_record.jsonl": 3})
        before_dry_run = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        policy.enforce(dry_run=True)
        after_dry_run = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        expect(after_dry_run == before_dry_run, "retention dry-run leaves logs unchanged", {"before": before_dry_run, "after": after_dry_run})
        policy.enforce()
        after_enforce = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        expect(after_enforce <= 3, "retention enforce keeps audit log within limit", after_enforce)

        guard_root = Path(tmp) / "guard-state"
        guard_store = WorldStateStore(guard_root)
        try:
            WorldStateStore(guard_root / "runtime")
            raise AssertionError("nested runtime root was accepted")
        except StateNestedRootError as exc:
            expect("canonical state root" in str(exc), "canonical layout partition is rejected", exc)
        try:
            WorldStateStore(guard_root / "runtime", read_only=True)
            raise AssertionError("read-only nested runtime root was accepted")
        except StateNestedRootError:
            expect(True, "read-only canonical layout partition is rejected")

        temporary_parent = Path(tmp) / "ordinary-temp-parent"
        temporary_runtime = temporary_parent / "runtime"
        ordinary_store = WorldStateStore(temporary_runtime)
        expect(ordinary_store.root == temporary_runtime, "ordinary temp runtime subroot remains valid")

        repair_root = Path(tmp) / "repair-state"
        WorldStateStore(repair_root)
        # This fixture is inspected as if the API process had already exited;
        # the in-process store used to seed defaults otherwise owns the lease.
        (repair_root / ".veyra-writer.lock").unlink(missing_ok=True)
        accidental_root = repair_root / "runtime"
        nested_runtime = accidental_root / "runtime"
        nested_runtime.mkdir(parents=True)
        nested_payload = {"status": "nested-only", "_state_revision": 7}
        (nested_runtime / "task_state.json").write_text(json.dumps(nested_payload), encoding="utf-8")
        (repair_root / "runtime" / "task_state.json").unlink(missing_ok=True)
        for directory, name in (
            ("config", "agent_config.json"),
            ("user", "user_world.json"),
            ("local", "local_world.json"),
            ("logs", "event_log.jsonl"),
            ("external", "external_world.json"),
        ):
            path = accidental_root / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        for directory in PRESERVED_RUNTIME_ARTIFACT_DIRS:
            artifact = accidental_root / directory
            artifact.mkdir(parents=True, exist_ok=True)
            (artifact / "sentinel.txt").write_text("preserve\n", encoding="utf-8")

        dry_run = repair_nested_state_root(repair_root)
        expect(dry_run.get("status") == "dry_run", "nested-root repair defaults to dry-run")
        expect(not dry_run.get("conflicts"), "preserved runtime artifacts do not block repair", dry_run.get("conflicts"))
        expect((nested_runtime / "task_state.json").exists(), "dry-run leaves nested runtime unchanged")
        expect(not (repair_root / ".recovery-backups").exists(), "dry-run does not create a backup")
        expect(
            not any(
                any(item.startswith(f"{directory}/") for directory in PRESERVED_RUNTIME_ARTIFACT_DIRS)
                for item in dry_run.get("backup_files", [])
            ),
            "preserved runtime artifacts are excluded from backup",
        )

        (repair_root / ".veyra-writer.lock").write_text(
            json.dumps({"pid": os.getpid(), "state_root": str(repair_root)}),
            encoding="utf-8",
        )
        try:
            repair_nested_state_root(repair_root, apply=True)
            raise AssertionError("active canonical writer was not rejected")
        except NestedStateRepairError:
            expect(True, "active canonical writer blocks repair")
        finally:
            (repair_root / ".veyra-writer.lock").unlink()

        canonical_runtime = repair_root / "runtime"
        (canonical_runtime / "task_state.json").write_text(
            json.dumps({"status": "different", "_state_revision": 8}),
            encoding="utf-8",
        )
        try:
            repair_nested_state_root(repair_root, apply=True)
            raise AssertionError("runtime content conflict was not rejected")
        except NestedStateRepairError:
            expect(True, "runtime content conflict blocks repair")
        (canonical_runtime / "task_state.json").unlink()

        applied = repair_nested_state_root(repair_root, apply=True)
        backup_path = Path(str(applied["backup_path"]))
        expect(applied.get("status") == "applied", "nested-root repair applies after preflight")
        expect((canonical_runtime / "task_state.json").read_text(encoding="utf-8") == json.dumps(nested_payload), "nested runtime JSON promoted")
        expect((backup_path / "runtime" / "task_state.json").exists(), "repair backup contains nested runtime JSON")
        expect((backup_path / "manifest.json").exists(), "repair writes manifest")
        expect(not (accidental_root / "config").exists(), "generated accidental config removed")
        expect(not (accidental_root / "user").exists(), "generated accidental user removed")
        expect(not (accidental_root / "local").exists(), "generated accidental local removed")
        expect(not (accidental_root / "logs").exists(), "generated accidental logs removed")
        expect(not (accidental_root / "external").exists(), "generated accidental external removed")
        for directory in PRESERVED_RUNTIME_ARTIFACT_DIRS:
            expect(
                (accidental_root / directory / "sentinel.txt").exists(),
                f"preserved runtime artifact {directory} remains",
            )
        idempotent = repair_nested_state_root(repair_root, apply=True)
        expect(idempotent.get("status") == "clean", "repair is idempotent")

    print("state_layout_smoke passed")


if __name__ == "__main__":
    main()
