#!/usr/bin/env python3
"""Proves the proactive remediation loop is closed: approving a review executes it.

Previously, approving a proactive_remediation / agent_restart review returned
not_supported. Now ActionExecutor.execute_review routes those proposals to the
proactive executor: R2 reviews gather read-only evidence, and agent_restart attempts
a reversible reconnect (and only runs a configured restart command via SafeShell).
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from execution.action_executor import ActionExecutor  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class ConnectedAdapter:
    def connection_status(self) -> dict:
        return {"connected": True, "status": "available"}


class DownAdapter:
    def connection_status(self) -> dict:
        return {"connected": False, "status": "unavailable"}


def review_for(proposal: dict) -> dict:
    return {"review_id": "rev_test", "status": "pending", "proposal": proposal}


def main() -> None:
    with TemporaryDirectory(prefix="veyra-review-exec-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json("task_state.json", {"pending_agent_tasks": [
            {"task_id": "t1", "verification_status": "verified_failed", "poll_count": 2},
        ]})

        pc_connected = ProactiveChecks(store, agency_root=str(Path(tmp) / "a1"), model_assist_enabled=False, agent_adapter_resolver=lambda: ConnectedAdapter())
        executor = ActionExecutor(state_store=store, proactive_executor=pc_connected.execute_approved_proposal)

        # Before: proposal types with top-level "type" used to fall to not_supported.
        # Now: approving an R2 proactive_remediation review actually runs the diagnostic.
        remediation_review = review_for({
            "type": "proactive_remediation",
            "gap_id": "agent_task_drift",
            "suggested_action": "review_agent_task_drift",
            "action_text": "review drifting agent tasks",
        })
        result = executor.execute_review(remediation_review)
        expect(result.get("status") == "diagnosed", "approved remediation executes (not not_supported)", result)
        expect(result.get("operation") == "proactive_remediation", "operation tagged", result)
        expect(any(t.get("task_id") == "t1" for t in (result.get("drifting_tasks") or [])), "remediation surfaced the drifting task", result)

        # agent_restart with a reachable adapter recovers via reversible reconnect.
        store.write_json("executor_state.json", {"status": "unavailable", "connected": False})
        restart_review = review_for({"type": "agent_restart", "target": "selected_agent"})
        recovered = executor.execute_review(restart_review)
        expect(recovered.get("status") == "recovered" and recovered.get("method") == "reconnect", "agent_restart recovers via reconnect", recovered)
        expect(store.read_json("executor_state.json").get("connected") is True, "executor marked connected after recovery", store.read_json("executor_state.json"))

        # agent_restart with an unreachable adapter and no configured command is a safe no-op.
        pc_down = ProactiveChecks(store, agency_root=str(Path(tmp) / "a2"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        executor_down = ActionExecutor(state_store=store, proactive_executor=pc_down.execute_approved_proposal)
        noop = executor_down.execute_review(review_for({"type": "agent_restart", "target": "selected_agent"}))
        expect(noop.get("status") == "approved_no_op", "no restart command -> safe no-op, never auto-restarts", noop)

        # Unwired executor degrades honestly instead of pretending success.
        bare = ActionExecutor(state_store=store)
        bare_result = bare.execute_review(review_for({"type": "proactive_remediation", "suggested_action": "review_resource_pressure"}))
        expect(bare_result.get("status") == "not_supported", "unwired executor reports not_supported honestly", bare_result)

    print("proactive_review_execution_smoke: ok")


if __name__ == "__main__":
    main()
