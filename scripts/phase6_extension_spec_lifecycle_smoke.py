#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime.extension_spec_quarantine import (  # noqa: E402
    STATE_FILE,
    MAX_NON_TERMINAL_OPERATIONS,
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantine,
    ExtensionSpecStorageError,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


WORKSPACE = "/private/veyra/phase6-extension-workspace"
USER = "phase6-extension-user"


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def prepare_store(root: Path) -> WorldStateStore:
    store = WorldStateStore(root)
    store.mutate_json(
        "local_world.json",
        lambda state: {
            **state,
            "current_project": WORKSPACE,
        },
    )
    return store


def private_invariants(store: WorldStateStore) -> dict[str, Any]:
    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    snapshot = CapabilityRegistry(
        store,
        now=fixed,
    ).snapshot()
    capabilities = snapshot.get("capabilities", {})
    return {
        "capability": {
            "routes": snapshot.get("routes"),
            "items": {
                capability_id: {
                    key: value.get(key)
                    for key in (
                        "available",
                        "kind",
                        "route",
                        "executor",
                        "status",
                        "reason",
                        "namespace",
                    )
                }
                for capability_id, value in capabilities.items()
                if isinstance(value, dict)
            },
        },
        "memory": store.read_json("agent_memory.json"),
        "agent_config": store.read_json("agent_config.json"),
        "task": store.read_json("task_state.json"),
        "durable_case": store.read_json("durable_case_state.json"),
        "phase6_collaboration": store.read_json(
            "phase6_collaboration_state.json"
        ),
        "self_improvement": store.read_json(
            "self_improvement_proposals.json"
        ),
        "state_change_proposals": store.read_json(
            "state_change_proposals.json"
        ),
        "review": store.read_json("review_queue.json"),
        "tool_governance": store.read_json(
            "tool_governance_state.json"
        ),
        "openclaw_broker": store.read_json(
            "openclaw_tool_hook_state.json"
        ),
    }


def submit(
    runtime: ExtensionSpecQuarantine,
    payload: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    spec = parse_extension_spec(payload)
    return runtime.quarantine(
        spec=spec,
        expected_spec_digest=spec.digest(),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=operation_id,
    )


def main() -> int:
    base_time = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    with TemporaryDirectory(
        prefix="veyra-phase6-extension-lifecycle-"
    ) as raw:
        root = Path(raw)
        store = prepare_store(root)
        clock = Clock(base_time)
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=clock,
        )
        before = private_invariants(store)
        execution_sentinel = root / "candidate-code-must-not-run"
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.lifecycle"
        payload["purpose"] = (
            "Never evaluate this manifest text: "
            f"Path({str(execution_sentinel)!r}).write_text('executed')"
        )
        candidate = submit(runtime, payload, "ext-lifecycle-submit")
        expect(
            candidate["stage"] == "SPEC_QUARANTINED"
            and candidate["candidate_revision"] == 1
            and candidate["execution_status"] == "not_started"
            and candidate["signature_status"] == "not_implemented"
            and candidate["capability_registry_visible"] is False
            and candidate["promotion_authorized"] is False,
            "valid spec enters private non-executing quarantine",
            candidate,
        )
        serialized_public = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            "future isolated generation" not in serialized_public
            and "user_id" not in candidate
            and "workspace_id" not in candidate
            and "spec" not in candidate,
            "public candidate projection excludes owner and raw spec",
            candidate,
        )
        private_text = store.path_for(STATE_FILE).read_text(
            encoding="utf-8"
        )
        expect(
            "ext-lifecycle-submit" not in private_text
            and payload["purpose"] in private_text,
            "operation ids are hashed while the manifest stays private",
        )
        action_text = json.dumps(
            store.read_jsonl("action_record.jsonl", limit=10),
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            candidate["candidate_id"] not in action_text
            and payload["extension_id"] not in action_text
            and candidate["spec_digest"] not in action_text,
            "global action audit omits owner-private extension identity",
        )

        state_before_integrity = store.path_for(STATE_FILE).read_bytes()
        integrity = runtime.integrity(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        state_after_integrity = store.path_for(STATE_FILE).read_bytes()
        expect(
            integrity["status"] == "spec_integrity_passed"
            and integrity["state_mutated"] is False
            and integrity["behavior_verification_status"] == "not_started"
            and integrity["execution_status"] == "not_started"
            and state_before_integrity == state_after_integrity,
            "integrity check reparses the manifest without execution",
            integrity,
        )
        expect(
            not execution_sentinel.exists(),
            "code-like manifest text is never evaluated",
        )

        accepted = runtime.review(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="ext-lifecycle-review",
            decision="accept_for_future_isolated_generation",
            reason="Manifest is eligible only for a later isolated generator.",
        )
        expect(
            accepted["stage"] == "SPEC_GATE_PASSED"
            and accepted["candidate_revision"] == 2
            and accepted["review"]["execution_authorized"] is False
            and accepted["review"]["promotion_authorized"] is False,
            "spec gate pass grants no execution or promotion authority",
            accepted,
        )
        replay = runtime.review(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="ext-lifecycle-review",
            decision="accept_for_future_isolated_generation",
            reason="Manifest is eligible only for a later isolated generator.",
        )
        expect(
            replay["operation_replayed"] is True
            and replay["candidate_revision"] == 2,
            "exact review replay is idempotent",
            replay,
        )
        try:
            runtime.review(
                candidate_id=candidate["candidate_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=2,
                operation_id="ext-lifecycle-review",
                decision="reject",
                reason="Reuse the operation with another meaning.",
            )
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError(
                "operation id semantic conflict did not fail closed"
            )
        print("PASS operation id semantic conflict fails closed")

        try:
            runtime.get(
                candidate_id=candidate["candidate_id"],
                user_id="another-user",
                workspace_id=WORKSPACE,
            )
        except ExtensionSpecNotFoundError:
            pass
        else:
            raise AssertionError("cross-owner read did not fail closed")
        print("PASS owner mismatch is indistinguishable from not found")

        changed_identity = valid_spec(now=base_time)
        changed_identity["extension_id"] = "example.lifecycle"
        changed_identity["purpose"] = "A different manifest at version one."
        try:
            submit(
                runtime,
                changed_identity,
                "ext-lifecycle-conflict",
            )
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError(
                "same extension version accepted another digest"
            )
        print("PASS one extension version cannot be rebound")

        revoked = runtime.revoke(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=2,
            operation_id="ext-lifecycle-revoke",
            reason="Operator revoked this future generation candidate.",
        )
        expect(
            revoked["stage"] == "REVOKED"
            and revoked["candidate_revision"] == 3,
            "revocation is a monotonic lifecycle transition",
            revoked,
        )
        historical_review_replay = runtime.review(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="ext-lifecycle-review",
            decision="accept_for_future_isolated_generation",
            reason="Manifest is eligible only for a later isolated generator.",
        )
        expect(
            historical_review_replay["stage"] == "REVOKED"
            and historical_review_replay["operation_replayed"] is True
            and historical_review_replay[
                "replayed_operation_result"
            ]
            == {
                "candidate_revision": 2,
                "stored_stage": "SPEC_GATE_PASSED",
            },
            "replay distinguishes the historical result from current state",
            historical_review_replay,
        )
        restarted = ExtensionSpecQuarantine(
            state_store=store,
            now=clock,
        )
        restarted_record = restarted.get(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        revoked_replay = restarted.revoke(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=2,
            operation_id="ext-lifecycle-revoke",
            reason="Operator revoked this future generation candidate.",
        )
        expect(
            restarted_record["stage"] == "REVOKED"
            and revoked_replay["operation_replayed"] is True
            and revoked_replay["candidate_revision"] == 3,
            "restart and replay cannot resurrect a revoked spec",
            revoked_replay,
        )

        after = private_invariants(store)
        expect(
            before == after,
            "ExtensionSpec lifecycle changes no routed capability, memory, "
            "review, Agent, or Tool Governance state",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-concurrency-"
    ) as raw:
        store = prepare_store(Path(raw))
        clock = Clock(base_time)
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=clock,
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.concurrent"
        candidate = submit(runtime, payload, "ext-concurrent-submit")

        def decide(decision: str, operation_id: str) -> str:
            try:
                result = runtime.review(
                    candidate_id=candidate["candidate_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                    expected_revision=1,
                    operation_id=operation_id,
                    decision=decision,  # type: ignore[arg-type]
                    reason=f"Concurrent decision: {decision}",
                )
                return str(result["stage"])
            except ExtensionSpecConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: decide(*args),
                    [
                        (
                            "accept_for_future_isolated_generation",
                            "ext-concurrent-accept",
                        ),
                        ("reject", "ext-concurrent-reject"),
                    ],
                )
            )
        expect(
            results.count("conflict") == 1
            and len(set(results) & {"SPEC_GATE_PASSED", "REJECTED"})
            == 1,
            "concurrent CAS permits exactly one review transition",
            results,
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-expiry-"
    ) as raw:
        store = prepare_store(Path(raw))
        clock = Clock(base_time)
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=clock,
        )
        expiring = valid_spec(now=base_time)
        expiring["extension_id"] = "example.expiring"
        expiring["expires_at"] = (
            base_time + timedelta(seconds=1)
        ).isoformat()
        candidate = submit(runtime, expiring, "ext-expiry-submit")
        clock.current = base_time + timedelta(seconds=1)
        expired = runtime.get(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expired_integrity = runtime.integrity(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            expired["stage"] == "EXPIRED"
            and expired_integrity["status"] == "blocked",
            "expiry boundary fails closed at now equals expires_at",
            expired_integrity,
        )
        try:
            runtime.review(
                candidate_id=candidate["candidate_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=1,
                operation_id="ext-expiry-review",
                decision="accept_for_future_isolated_generation",
                reason="Expired candidates cannot pass.",
            )
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError("expired candidate passed the gate")
        print("PASS expired candidate cannot pass the spec gate")

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-corrupt-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.tamper"
        candidate = submit(runtime, payload, "ext-tamper-submit")

        def tamper(state: dict[str, Any]) -> None:
            state["candidates"][candidate["candidate_id"]]["spec"][
                "purpose"
            ] = "Tampered after admission."

        store.mutate_json(STATE_FILE, tamper)
        expect(
            runtime.status()["operational_health"] == "degraded",
            "digest-bound semantic tamper degrades only extension status",
        )
        try:
            runtime.integrity(
                candidate_id=candidate["candidate_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            )
        except ExtensionSpecStorageError:
            pass
        else:
            raise AssertionError("tampered state did not fail closed")
        print("PASS tampered private state blocks extension operations")

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-index-tamper-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.index_tamper"
        candidate = submit(runtime, payload, "ext-index-submit")

        def tamper_identity_index(state: dict[str, Any]) -> None:
            state["identity_index"] = {
                "0" * 64: candidate["candidate_id"],
            }

        store.mutate_json(STATE_FILE, tamper_identity_index)
        expect(
            runtime.status()["operational_health"] == "degraded",
            "candidate identity index tamper fails closed",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-operation-tamper-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.operation_tamper"
        candidate = submit(runtime, payload, "ext-operation-submit")

        def tamper_operation_revision(state: dict[str, Any]) -> None:
            operation = next(iter(state["operation_index"].values()))
            operation["result_revision"] = (
                candidate["candidate_revision"] + 1
            )

        store.mutate_json(STATE_FILE, tamper_operation_revision)
        expect(
            runtime.status()["operational_health"] == "degraded",
            "operation results cannot claim a future candidate revision",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-lifecycle-tamper-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.lifecycle_tamper"
        candidate = submit(runtime, payload, "ext-stage-submit")

        def tamper_stage(state: dict[str, Any]) -> None:
            state["candidates"][candidate["candidate_id"]]["stage"] = (
                "SPEC_GATE_PASSED"
            )

        store.mutate_json(STATE_FILE, tamper_stage)
        expect(
            runtime.status()["operational_health"] == "degraded",
            "stage cannot claim a gate pass without exact review history",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-replay-redirect-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload_a = valid_spec(now=base_time)
        payload_a["extension_id"] = "example.redirect_a"
        spec_a = parse_extension_spec(payload_a)
        candidate_a = runtime.quarantine(
            spec=spec_a,
            expected_spec_digest=spec_a.digest(),
            user_id=USER,
            workspace_id=WORKSPACE,
            operation_id="ext-redirect-a",
        )
        payload_b = valid_spec(now=base_time)
        payload_b["extension_id"] = "example.redirect_b"
        spec_b = parse_extension_spec(payload_b)
        candidate_b = runtime.quarantine(
            spec=spec_b,
            expected_spec_digest=spec_b.digest(),
            user_id="other-extension-user",
            workspace_id=WORKSPACE,
            operation_id="ext-redirect-b",
        )

        def redirect_operation(state: dict[str, Any]) -> None:
            operation_key = runtime._operation_key(  # noqa: SLF001
                USER,
                WORKSPACE,
                "ext-redirect-a",
            )
            operation = state["operation_index"][operation_key]
            operation["candidate_id"] = candidate_b["candidate_id"]
            operation["owner_scope_digest"] = (
                runtime._owner_scope_digest(  # noqa: SLF001
                    "other-extension-user",
                    WORKSPACE,
                )
            )

        store.mutate_json(STATE_FILE, redirect_operation)
        expect(
            runtime.status()["operational_health"] == "available",
            "redirect fixture remains structurally valid",
        )
        try:
            runtime.quarantine(
                spec=spec_a,
                expected_spec_digest=spec_a.digest(),
                user_id=USER,
                workspace_id=WORKSPACE,
                operation_id="ext-redirect-a",
            )
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError(
                "operation replay returned a different candidate"
            )
        print("PASS replay cannot redirect to another owner or candidate")
        expect(
            candidate_a["candidate_id"] != candidate_b["candidate_id"],
            "redirect fixture uses two distinct candidates",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-scope-race-"
    ) as raw:
        store = prepare_store(Path(raw))

        class ScopeSwitchingQuarantine(ExtensionSpecQuarantine):
            def _owner_scope(
                self,
                user_id: str,
                workspace_id: str,
            ) -> tuple[str, str]:
                scope = super()._owner_scope(user_id, workspace_id)
                self.state_store.mutate_json(
                    "local_world.json",
                    lambda state: {
                        **state,
                        "current_project": "/private/other-workspace",
                    },
                )
                return scope

        runtime = ScopeSwitchingQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.scope_race"
        spec = parse_extension_spec(payload)
        try:
            runtime.quarantine(
                spec=spec,
                expected_spec_digest=spec.digest(),
                user_id=USER,
                workspace_id=WORKSPACE,
                operation_id="ext-scope-race-submit",
            )
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError(
                "workspace scope changed before the durable mutation"
            )
        expect(
            store.read_json(STATE_FILE)["candidate_count"] == 0,
            "workspace scope is rechecked at the state writer boundary",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-terminal-capacity-"
    ) as raw:
        store = prepare_store(Path(raw))
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=Clock(base_time),
        )
        payload = valid_spec(now=base_time)
        payload["extension_id"] = "example.terminal_capacity"
        candidate = submit(runtime, payload, "ext-capacity-submit")

        def fill_non_terminal_operations(
            state: dict[str, Any],
        ) -> None:
            owner_scope_digest = runtime._owner_scope_digest(  # noqa: SLF001
                USER,
                WORKSPACE,
            )
            while (
                len(state["operation_index"])
                < MAX_NON_TERMINAL_OPERATIONS
            ):
                index = len(state["operation_index"])
                operation_key = hashlib.sha256(
                    f"capacity-key-{index}".encode("utf-8")
                ).hexdigest()
                state["operation_index"][operation_key] = {
                    "request_digest": hashlib.sha256(
                        f"capacity-request-{index}".encode("utf-8")
                    ).hexdigest(),
                    "kind": "quarantine",
                    "candidate_id": candidate["candidate_id"],
                    "result_revision": 1,
                    "result_stage": "SPEC_QUARANTINED",
                    "owner_scope_digest": owner_scope_digest,
                    "terminal_control": False,
                    "recorded_at": base_time.isoformat(),
                }

        store.mutate_json(STATE_FILE, fill_non_terminal_operations)
        expect(
            runtime.status()["storage"]["operation_count"]
            == MAX_NON_TERMINAL_OPERATIONS,
            "non-terminal operation budget reaches its reserved boundary",
        )
        try:
            submit(runtime, payload, "ext-capacity-extra")
        except ExtensionSpecConflictError:
            pass
        else:
            raise AssertionError(
                "non-terminal operations consumed reserved capacity"
            )
        revoked = runtime.revoke(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="ext-capacity-revoke",
            reason="Terminal safety remains available at capacity.",
        )
        expect(
            revoked["stage"] == "REVOKED"
            and runtime.status()["storage"]["operation_count"]
            == MAX_NON_TERMINAL_OPERATIONS + 1,
            "reserved terminal capacity keeps revocation available",
            revoked,
        )

    print("phase6 extension spec lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
