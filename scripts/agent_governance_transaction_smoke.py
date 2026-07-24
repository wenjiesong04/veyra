from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.verifier import Verifier
from core.world_state import WorldStateStore
from interface.agent_adapter import ExecutionResult
from interface.openclaw_adapter import OpenClawAdapter
from runtime.agent_task_tracker import AgentTaskTracker
from tool_proxy.agent_tool_contract import AgentToolCompliance


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class _DummySocket:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


def check_tool_proxy_and_verifier() -> None:
    compliance = AgentToolCompliance().review_execution(
        ExecutionResult(
            task_id="no_trace",
            executor="openclaw",
            status="success",
            result="done",
            raw={"final_event": {"state": "final", "message": {"text": "done"}}},
        )
    )
    expect(compliance["status"] == "not_observed", "empty tool report is not marked compliant", compliance)
    expect(compliance["enforcement_observed"] is False, "empty tool report does not prove enforcement", compliance)

    verifier = Verifier()
    text_only = verifier.verify_execution_result(
        ExecutionResult(task_id="text_only", executor="openclaw", status="success", result="done")
    )
    expect(text_only["status"] == "needs_more_probe", "result text alone is not verified evidence", text_only)
    expect(
        text_only["verdict"] == "execution_success_without_structured_evidence",
        "text-only verdict names missing structured evidence",
        text_only,
    )

    raw_metadata_only = verifier.verify_execution_result(
        ExecutionResult(
            task_id="raw_metadata",
            executor="openclaw",
            status="success",
            result="done",
            raw={"final_event": {"state": "final"}, "poll_method": "chat.events"},
        )
    )
    expect(raw_metadata_only["status"] == "needs_more_probe", "transport metadata alone is not verified evidence", raw_metadata_only)

    structured = verifier.verify_execution_result(
        ExecutionResult(
            task_id="structured",
            executor="openclaw",
            status="success",
            result="answer",
            raw={
                "agent_response": {
                    "answer_or_plan": "answer",
                    "evidence_used": [{"source": "probe", "observed_at": "2026-07-24T00:00:00Z"}],
                }
            },
        )
    )
    expect(structured["status"] == "verified_success", "structured outcome evidence can be verified", structured)

    plan_only = verifier.verify_execution_result(
        ExecutionResult(
            task_id="plan_only",
            executor="openclaw",
            status="success",
            result="plan",
            raw={"agent_response": {"answer_or_plan": "1. inspect\n2. propose patch", "proposed_actions": []}},
        )
    )
    expect(plan_only["status"] == "partially_success", "plan-only response remains presentable without execution claim", plan_only)
    expect(
        plan_only["verdict"] == "agent_plan_returned_without_execution_evidence",
        "plan-only verdict distinguishes planning from verified execution",
        plan_only,
    )

    untraced_change = verifier.verify_execution_result(
        ExecutionResult(
            task_id="untraced_change",
            executor="openclaw",
            status="success",
            result="changed",
            changed_files=["example.txt"],
            raw={"diff": {"path": "example.txt", "changed": True}},
        )
    )
    expect(
        untraced_change["verdict"] == "changed_files_without_tool_execution_trace",
        "changed files without a tool execution trace are not verified",
        untraced_change,
    )

    missing_r2_trace = verifier.verify_execution_result(
        ExecutionResult(
            task_id="missing_r2_trace",
            executor="openclaw",
            status="success",
            result="wrote file",
            changed_files=["example.txt"],
            tool_calls=["write example.txt"],
            raw={"evidence": [{"path": "example.txt"}]},
        )
    )
    expect(missing_r2_trace["status"] == "needs_more_probe", "R2-like call without proxy trace is not verified", missing_r2_trace)


def check_openclaw_truth_and_history_scope() -> None:
    adapter = OpenClawAdapter(base_url="http://127.0.0.1:18789")
    adapter._open_socket = lambda: _DummySocket()  # type: ignore[assignment]
    adapter._connect = lambda _ws, _events: {"features": {"methods": ["chat.send"]}}  # type: ignore[assignment]
    adapter._compat_request = lambda _ws, _hello, _method, _params, _events: {}  # type: ignore[assignment]
    adapter._compatibility_summary = lambda _hello: {  # type: ignore[assignment]
        "required_methods": {"chat.send": True},
        "optional_methods": {},
    }
    snapshot = adapter._gateway_snapshot()
    features = snapshot["features"]
    expect(features["tool_proxy_enforced"] is False, "OpenClaw status does not overclaim Tool Proxy enforcement", features)
    expect(
        features["tool_proxy_enforcement_status"] == "validation_pending",
        "OpenClaw status exposes pending enforcement validation",
        features,
    )

    cached_adapter = OpenClawAdapter(base_url="http://127.0.0.1:18789")
    snapshot_calls: list[int] = []

    def fake_snapshot() -> dict[str, Any]:
        snapshot_calls.append(1)
        return {
            "runtime": "openclaw",
            "status": "available",
            "connected": True,
            "features": {"rendered_prompt_fallback": True, "tool_proxy_enforced": False},
        }

    cached_adapter._gateway_snapshot = fake_snapshot  # type: ignore[assignment]
    cached_adapter.fetch_capabilities()
    cached_adapter.fetch_capabilities()
    expect(len(snapshot_calls) == 1, "capability reads reuse the bounded cache", snapshot_calls)
    cached_adapter.invalidate_capabilities_cache()
    cached_adapter.fetch_capabilities()
    expect(len(snapshot_calls) == 2, "capability cache can be explicitly invalidated", snapshot_calls)

    captured: list[str] = []

    def fake_history(*, session_key: str, limit: int = 8) -> dict[str, Any]:
        captured.append(session_key)
        return {"messages": [{"role": "assistant", "text": "isolated result"}]}

    adapter._fetch_chat_history = fake_history  # type: ignore[assignment]
    adapter._remember_chat_run(
        "run_isolated",
        status="submitted",
        result="Task submitted to OpenClaw. run_id=run_isolated",
        task_context={"agent_execution_session_id": "agent-exec:task_1"},
    )
    result = adapter._result_text_for_run("run_isolated", {})
    expect(result == "isolated result", "history fallback returns isolated session result", result)
    expect(captured == ["agent-exec:task_1"], "history fallback uses task-specific session", captured)

    adapter._remember_chat_run(
        "run_missing_context",
        status="submitted",
        result="Task submitted to OpenClaw. run_id=run_missing_context",
    )
    missing = adapter._result_text_for_run("run_missing_context", {})
    expect(missing == "", "history fallback fails closed when task session is unknown", missing)
    expect("main" not in captured, "history fallback never queries shared main session", captured)


def check_task_context_transaction() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-agent-transaction-") as raw_tmp:
        store = WorldStateStore(root=Path(raw_tmp) / "state")
        recovery_calls: list[dict[str, Any]] = []
        tracker = AgentTaskTracker(store, recovery_hook=lambda payload: recovery_calls.append(payload))
        task_context = {
            "task_packet_id": "task_packet_1",
            "correlation_id": "corr_1",
            "session_id": "dialogue_1",
            "agent_execution_session_id": "agent-exec:task_packet_1",
            "agent_session_policy": "ephemeral_per_task",
            "memory_policy": "long_term",
            "verification_policy": {"requires_evidence": True},
            "rollback_requirement": {"rollback_plan_required": True},
            "user_goal": "finish task token=do-not-store",
        }
        pending = ExecutionResult(
            task_id="run_1",
            executor="openclaw",
            status="submitted",
            result="queued",
            raw={"task_context": task_context},
        )
        registered = tracker.register(
            event_id="event_1",
            route="agent",
            execution=pending,
            verification={"status": "partially_success", "next_action": "poll_runtime_or_probe_result"},
            channel="api",
            user_id="user_1",
        )
        expect(bool(registered), "non-terminal task registered", registered)
        resolved = tracker.get_context("run_1") or {}
        expect(resolved.get("memory_policy") == "long_term", "tracker persists memory policy", resolved)
        expect(
            resolved.get("agent_execution_session_id") == "agent-exec:task_packet_1",
            "tracker persists agent execution session",
            resolved,
        )
        expect(
            resolved.get("user_goal") == "finish task token=<redacted>",
            "tracker persists a bounded redacted user goal",
            resolved,
        )
        expect(tracker.get_context("task_packet_1") is not None, "tracker resolves task packet correlation", resolved)

        update = tracker.apply_result(
            execution=ExecutionResult(
                task_id="run_1",
                executor="openclaw",
                status="success",
                result="done",
                raw={"evidence": [{"source": "unit-smoke"}]},
            ),
            verification={"status": "verified_success", "next_action": "update_state_and_memory"},
            event_id="event_1",
        )
        expect(update["matched"] is True, "terminal result matches pending task", update)
        expect(update["task_context"].get("memory_policy") == "long_term", "terminal update returns original task context", update)
        expect(not update["pending_agent_tasks"], "terminal result removes pending task", update)
        archived = tracker.get_context("run_1") or {}
        expect(archived.get("final_status") == "success", "terminal task context remains durably resolvable", archived)

        immediate_failure = ExecutionResult(
            task_id="run_immediate_failure",
            executor="openclaw",
            status="failed",
            result="failed after write",
            changed_files=["example.txt"],
            raw={"task_context": {**task_context, "task_packet_id": "task_packet_failure"}},
        )
        tracker.register(
            event_id="event_failure",
            route="agent",
            execution=immediate_failure,
            verification={"status": "needs_rollback", "needs_rollback": True},
            memory_policy="forget",
        )
        failed_context = tracker.get_context("run_immediate_failure") or {}
        expect(failed_context.get("final_status") == "failed", "immediate terminal task context is archived", failed_context)
        expect(len(recovery_calls) == 1, "rollback verification triggers configured recovery hook", recovery_calls)

        tracker.apply_result(
            execution=ExecutionResult(
                task_id="attacker_chosen_task",
                executor="callback",
                status="success",
                result="done",
                raw={
                    "task_context": {
                        "session_id": "victim",
                        "memory_policy": "long_term",
                        "authority": "veyra_registered",
                    }
                },
            ),
            verification={"status": "verified_success", "needs_memory_patch": True},
        )
        expect(
            tracker.get_context("attacker_chosen_task") is None,
            "unmatched callback cannot inject authoritative task context",
            store.read_json("task_state.json"),
        )


def main() -> int:
    check_tool_proxy_and_verifier()
    check_openclaw_truth_and_history_scope()
    check_task_context_transaction()
    print("Agent governance transaction smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
