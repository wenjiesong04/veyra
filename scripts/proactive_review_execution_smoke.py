#!/usr/bin/env python3
"""Proves reviewed diagnostics execute while restart reviews stay deny-only."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from execution.action_executor import ActionExecutor  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from rollback_audit.action_journal import ActionJournal  # noqa: E402
from rollback_audit.replay import Replay  # noqa: E402
from rollback_audit.replay_runtime import ReplayRuntime  # noqa: E402
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


def main() -> None:
    with TemporaryDirectory(prefix="veyra-review-exec-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json("task_state.json", {"pending_agent_tasks": [
            {"task_id": "t1", "verification_status": "verified_failed", "poll_count": 2},
        ]})

        pc_connected = ProactiveChecks(store, agency_root=str(Path(tmp) / "a1"), model_assist_enabled=False, agent_adapter_resolver=lambda: ConnectedAdapter())
        review_queue = ReviewQueue(store)
        executor = ActionExecutor(
            state_store=store,
            proactive_executor=pc_connected.execute_approved_proposal,
            review_authorizer=review_queue.authorize_execution,
        )

        def execute_proposal(
            selected_executor: ActionExecutor,
            proposal: dict,
            label: str,
        ) -> dict:
            review = review_queue.create(
                event_id=f"exec-{label}",
                task_text=label,
                risk_level="R2",
                foresight={},
                guardian_decision={"decision": "allow", "risk_level": "R2"},
                proposal=proposal,
            )
            approved, claim_token = review_queue.approve_and_claim(
                review["review_id"],
                "smoke approval",
            )
            expect(bool(claim_token), f"{label} obtains execution claim", approved)
            return selected_executor.execute_review(
                approved,
                claim_token=claim_token,
            )

        # Before: proposal types with top-level "type" used to fall to not_supported.
        # Now: approving an R2 proactive_remediation review actually runs the diagnostic.
        remediation_proposal = {
            "type": "proactive_remediation",
            "gap_id": "agent_task_drift",
            "suggested_action": "review_agent_task_drift",
            "action_text": "review drifting agent tasks",
        }
        result = execute_proposal(
            executor,
            remediation_proposal,
            "proactive-remediation",
        )
        expect(result.get("status") == "diagnosed", "approved remediation executes (not not_supported)", result)
        expect(result.get("operation") == "proactive_remediation", "operation tagged", result)
        expect(any(t.get("task_id") == "t1" for t in (result.get("drifting_tasks") or [])), "remediation surfaced the drifting task", result)

        # A generic review cannot bypass the registered Phase 5 playbook.
        store.write_json("executor_state.json", {"status": "unavailable", "connected": False})
        restart_review = execute_proposal(
            executor,
            {
                "type": "agent_restart",
                "target": "selected_agent",
                "action": {
                    "type": "file_write",
                    "path": str(Path(tmp) / "restart-bypass.txt"),
                    "content": "must-not-run",
                },
            },
            "agent-restart-connected",
        )
        expect(
            restart_review.get("status") == "governance_only",
            "agent_restart review carries no execution authority",
            restart_review,
        )
        expect(
            store.read_json("executor_state.json").get("connected") is False,
            "restart review cannot project an unverified recovery",
            store.read_json("executor_state.json"),
        )
        expect(
            not (Path(tmp) / "restart-bypass.txt").exists(),
            "agent_restart cannot smuggle a nested file action",
            restart_review,
        )

        # Adapter state cannot change that deny-only boundary.
        pc_down = ProactiveChecks(store, agency_root=str(Path(tmp) / "a2"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        executor_down = ActionExecutor(
            state_store=store,
            proactive_executor=pc_down.execute_approved_proposal,
            review_authorizer=review_queue.authorize_execution,
        )
        down_restart_review = execute_proposal(
            executor_down,
            {"type": "agent_restart", "target": "selected_agent"},
            "agent-restart-down",
        )
        expect(
            down_restart_review.get("status") == "governance_only",
            "down agent restart remains governance-only",
            down_restart_review,
        )

        # Unwired executor degrades honestly instead of pretending success.
        bare = ActionExecutor(
            state_store=store,
            review_authorizer=review_queue.authorize_execution,
        )
        bare_result = execute_proposal(
            bare,
            {
                "type": "proactive_remediation",
                "suggested_action": "review_resource_pressure",
            },
            "unwired-remediation",
        )
        expect(bare_result.get("status") == "not_supported", "unwired executor reports not_supported honestly", bare_result)

        status_cases = [
            ("approved-noop", {"status": "approved_noop"}, "governance_only", False),
            ("restart-governance", restart_review, "governance_only", False),
            ("diagnosed", result, "executed", False),
            ("refreshed", {"status": "refreshed"}, "executed", False),
            ("observed", {"status": "observed"}, "executed", False),
            ("probed-fallback", {"status": "probed_fallback"}, "executed", False),
            ("plain-error", {"status": "error", "reason": "simulated failure"}, "execution_failed", True),
            (
                "explicit-false",
                {"status": "success", "success": False, "reason": "business failure"},
                "execution_failed",
                True,
            ),
            (
                "nonzero-returncode",
                {"status": "ok", "returncode": 9, "stderr": "simulated failure"},
                "execution_failed",
                True,
            ),
            (
                "failed-verification",
                {"status": "executed", "verification": {"status": "verified_failed"}},
                "execution_failed",
                True,
            ),
            (
                "rollback-required",
                {"status": "success", "verification": {"needs_rollback": True}},
                "execution_failed",
                True,
            ),
            (
                "failed-tool-result",
                {"status": "executed", "tool_result": {"status": "error"}},
                "execution_failed",
                True,
            ),
            ("unsupported", bare_result, "not_executed", False),
            (
                "incomplete",
                {"status": "partially_success"},
                "execution_incomplete",
                False,
            ),
            (
                "nested-incomplete",
                {
                    "status": "success",
                    "verification": {"status": "partially_success"},
                },
                "execution_incomplete",
                False,
            ),
            (
                "nested-not-executed",
                {"status": "success", "tool_result": {"status": "blocked"}},
                "execution_incomplete",
                False,
            ),
            (
                "unknown",
                {"status": "made_up_success"},
                "execution_unknown",
                False,
            ),
        ]
        expected_replay_events: set[str] = set()
        blocked_replay_events: set[str] = set()
        for label, execution_result, expected_audit_status, replay_candidate in status_cases:
            event_id = f"review-status-{label}"
            review = review_queue.create(event_id, f"status matrix {label}", "R2", {}, {})
            _, claim_token = review_queue.approve_and_claim(
                review["review_id"],
                "status matrix",
            )
            expect(
                bool(claim_token),
                f"{label} obtains one execution claim",
                review,
            )
            review_queue.authorize_execution(
                review["review_id"],
                claim_token,
            )
            review_queue.update_execution(
                review["review_id"],
                execution_result,
                claim_token=claim_token,
            )
            execution_rows = [
                item
                for item in store.read_jsonl("action_record.jsonl", limit=500)
                if item.get("event_id") == event_id
                and item.get("status") not in {"pending", "approved"}
            ]
            expect(
                execution_rows
                and execution_rows[-1].get("status") == expected_audit_status,
                f"{label} maps to {expected_audit_status}",
                execution_rows,
            )
            if replay_candidate:
                expected_replay_events.add(event_id)
            else:
                blocked_replay_events.add(event_id)

        replay_runtime = ReplayRuntime(
            state_store=store,
            replay=Replay(store),
            journal=ActionJournal(store),
            review_queue=review_queue,
            foresight_engine=None,
        )
        replay_scan = replay_runtime.scan(limit=500)
        created_replay_events = {
            str(item.get("event_id") or "")
            for item in replay_scan.get("created", [])
        }
        expect(
            expected_replay_events <= created_replay_events,
            "explicit execution failures enter ReplayRuntime",
            replay_scan,
        )
        expect(
            not (blocked_replay_events & created_replay_events),
            "governance-only and completed outcomes stay out of ReplayRuntime",
            replay_scan,
        )

    print("proactive_review_execution_smoke: ok")


if __name__ == "__main__":
    main()
