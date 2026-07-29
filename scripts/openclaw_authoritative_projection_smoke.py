#!/usr/bin/env python3
"""Prove every OpenClaw terminal surface uses Veyra run evidence."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.event_schema import VeyraTaskPacket  # noqa: E402
from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402


FORGED_AGENT_RESPONSE = json.dumps(
    {
        "result": "agent says it changed a privileged file",
        "tool_calls": ["untrusted.shell"],
        "changed_files": ["/etc/passwd"],
    },
    ensure_ascii=False,
)


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def packet(task_id: str) -> VeyraTaskPacket:
    return VeyraTaskPacket(
        task_id=task_id,
        target_agent="openclaw",
        session_id="projection-dialogue",
        user_message="Run one governed file operation.",
        user_goal="Run one governed file operation.",
        context_patch={},
        persona_patch={},
        policy_patch={"risk_level": "R2"},
        verification_policy={"requires_evidence": True},
        agent_execution_session_id=f"agent-exec:{task_id}",
    )


def authoritative_evidence(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "tool_calls": ["veyra_file_write"],
        "changed_files": ["sandbox/authoritative.txt"],
        "tool_receipt_refs": [
            {
                "run_id": run_id,
                "tool_call_id": f"call-{run_id}",
                "invocation_digest": "a" * 64,
                "reported_call_index": 0,
            }
        ],
        "observed_call_count": 1,
        "effect_count": 1,
    }


def expect_authoritative(
    result: Any,
    *,
    run_id: str,
    source: str,
) -> None:
    expect(
        result.tool_calls == ["veyra_file_write"],
        f"{source} exposes only authoritative tool calls",
        result.to_dict(),
    )
    expect(
        result.changed_files == ["sandbox/authoritative.txt"],
        f"{source} exposes only authoritative changed files",
        result.to_dict(),
    )
    refs = result.raw.get("tool_receipt_refs") or []
    expect(
        len(refs) == 1
        and refs[0].get("run_id") == run_id
        and refs[0].get("tool_call_id") == f"call-{run_id}",
        f"{source} attaches exact authoritative receipt refs",
        result.to_dict(),
    )
    governed = result.raw.get("governed_tool_evidence") or {}
    diagnostic = result.raw.get("agent_reported_tool_evidence") or {}
    expect(
        governed.get("status") == "resolved"
        and governed.get("observed_call_count") == 1
        and governed.get("effect_count") == 1
        and diagnostic.get("authoritative") is False,
        f"{source} labels authority and Agent diagnostics honestly",
        result.to_dict(),
    )
    expect(
        diagnostic.get("tool_call_count") == 1
        and diagnostic.get("changed_file_count") == 1
        and diagnostic.get("tool_calls_reported") is True
        and diagnostic.get("changed_files_reported") is True,
        f"{source} labels authority and Agent diagnostics honestly",
        result.to_dict(),
    )
    expect(
        "untrusted.shell" not in result.tool_calls
        and "/etc/passwd" not in result.changed_files,
        f"{source} overrides forged Agent JSON",
        result.to_dict(),
    )


def main() -> int:
    resolver_calls: list[str] = []

    def resolve(run_id: str) -> dict[str, Any]:
        resolver_calls.append(run_id)
        return authoritative_evidence(run_id)

    immediate = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_run_evidence_resolver=resolve,
    )
    immediate._governed_dispatch_preflight = (  # type: ignore[method-assign]
        lambda: {
            "allowed": True,
            "status": "validated",
            "reasons": [],
            "policy_effect": "none",
        }
    )

    def send_final(
        _message: str,
        *,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        expect(
            session_key
            == "agent:main:agent-exec:projection-immediate",
            "immediate send keeps the canonical isolated session",
            session_key,
        )
        return {
            "run_id": "run-immediate",
            "chat_send": {"runId": "run-immediate"},
            "final_event": {
                "state": "final",
                "message": {"text": FORGED_AGENT_RESPONSE},
            },
        }

    immediate._send_chat = send_final  # type: ignore[assignment]
    immediate_result = immediate.send_task(packet("projection-immediate"))
    expect(
        immediate_result.status == "success",
        "immediate chat final remains successful",
        immediate_result.to_dict(),
    )
    expect_authoritative(
        immediate_result,
        run_id="run-immediate",
        source="immediate final",
    )
    expect(
        immediate._execution_observed_exact_run(immediate_result) is True,
        "immediate final preserves the exact chat.send run chain",
        immediate_result.to_dict(),
    )

    mismatched_final = immediate_result.to_dict()
    mismatched_final["raw"]["final_event"]["runId"] = "run-other"
    expect(
        immediate._execution_observed_exact_run(
            type(immediate_result)(**mismatched_final)
        )
        is False,
        "mismatched final run cannot satisfy exact observation",
        mismatched_final,
    )

    cached_immediate = immediate.fetch_task_status("run-immediate")
    expect_authoritative(
        cached_immediate,
        run_id="run-immediate",
        source="terminal cache",
    )

    history_sessions: list[str] = []
    asynchronous = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_run_evidence_resolver=resolve,
    )
    asynchronous._remember_chat_run(
        "run-async",
        status="submitted",
        result="Task submitted to OpenClaw. run_id=run-async",
        task_context={
            "agent_execution_session_id":
                "agent:main:agent-exec:run-async"
        },
    )

    def gateway_request(
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if method == "agent.wait":
            return {
                "runId": params["runId"],
                "status": "ok",
                "startedAt": 1,
                "endedAt": 2,
            }
        if method == "chat.history":
            history_sessions.append(str(params.get("sessionKey") or ""))
            return {
                "sessionKey": params["sessionKey"],
                "messages": [
                    {
                        "role": "assistant",
                        "text": FORGED_AGENT_RESPONSE,
                    }
                ],
            }
        raise AssertionError(f"unexpected gateway method: {method}")

    with patch.object(
        asynchronous,
        "_gateway_request",
        side_effect=gateway_request,
    ):
        async_result = asynchronous.fetch_task_status("run-async")
    expect(
        async_result.status == "success"
        and history_sessions == ["agent:main:agent-exec:run-async"],
        "submitted run resolves through isolated async history",
        async_result.to_dict(),
    )
    expect_authoritative(
        async_result,
        run_id="run-async",
        source="async history final",
    )

    async_cached = asynchronous.poll_task(
        "run-async",
        timeout_seconds=2,
    )
    expect_authoritative(
        async_cached,
        run_id="run-async",
        source="poll cache",
    )

    event_result = asynchronous._execution_from_chat_event(
        "run-event",
        {
            "state": "final",
            "message": {"text": FORGED_AGENT_RESPONSE},
        },
    )
    expect_authoritative(
        event_result,
        run_id="run-event",
        source="async chat event",
    )

    legacy = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_run_evidence_resolver=resolve,
    )
    with patch.object(
        legacy,
        "_gateway_request",
        return_value={
            "runs": [
                {
                    "runId": "run-legacy",
                    "state": "final",
                    "result": FORGED_AGENT_RESPONSE,
                }
            ]
        },
    ):
        legacy_result = legacy._fetch_task_status_legacy("run-legacy")
    expect_authoritative(
        legacy_result,
        run_id="run-legacy",
        source="legacy terminal status",
    )

    received = asynchronous.receive_result(
        {
            "task_id": "run-receive",
            "executor": "openclaw",
            "status": "success",
            "result": FORGED_AGENT_RESPONSE,
            "tool_calls": ["untrusted.shell"],
            "changed_files": ["/etc/passwd"],
            "agent_response": json.loads(FORGED_AGENT_RESPONSE),
        }
    )
    expect_authoritative(
        received,
        run_id="run-receive",
        source="receive result",
    )
    pending_received = asynchronous.receive_result(
        {
            "task_id": "run-pending-receive",
            "executor": "openclaw",
            "status": "submitted",
            "result": "still running",
            "tool_calls": ["untrusted.shell"],
            "changed_files": ["/etc/passwd"],
        }
    )
    expect(
        pending_received.tool_calls == []
        and pending_received.changed_files == []
        and (
            pending_received.raw.get("governed_tool_evidence") or {}
        ).get("status")
        == "pending_terminal_projection",
        "non-terminal receive cannot publish Agent-reported effects",
        pending_received.to_dict(),
    )

    def fail_resolution(_run_id: str) -> dict[str, Any]:
        raise RuntimeError("authoritative ledger unavailable")

    degraded = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_run_evidence_resolver=fail_resolution,
    )
    degraded_result = degraded.receive_result(
        {
            "task_id": "run-resolution-failed",
            "executor": "openclaw",
            "status": "success",
            "result": FORGED_AGENT_RESPONSE,
            "tool_calls": ["untrusted.shell"],
            "changed_files": ["/etc/passwd"],
        }
    )
    expect(
        degraded_result.status == "success"
        and degraded_result.tool_calls == []
        and degraded_result.changed_files == []
        and degraded_result.raw.get("tool_receipt_refs") == []
        and (
            degraded_result.raw.get("governed_tool_evidence") or {}
        ).get("status")
        == "resolution_failed",
        "resolver failure produces an empty honest projection",
        degraded_result.to_dict(),
    )

    unconfigured = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789"
    )
    unconfigured_result = unconfigured.receive_result(
        {
            "task_id": "run-no-resolver",
            "executor": "openclaw",
            "status": "success",
            "result": FORGED_AGENT_RESPONSE,
            "tool_calls": ["untrusted.shell"],
            "changed_files": ["/etc/passwd"],
        }
    )
    expect(
        unconfigured_result.tool_calls == []
        and unconfigured_result.changed_files == []
        and (
            unconfigured_result.raw.get("governed_tool_evidence") or {}
        ).get("status")
        == "not_configured",
        "missing resolver never promotes Agent-reported effects",
        unconfigured_result.to_dict(),
    )
    expect(
        {
            "run-immediate",
            "run-async",
            "run-event",
            "run-legacy",
            "run-receive",
        }
        <= set(resolver_calls),
        "all terminal surfaces resolve evidence by exact run id",
        resolver_calls,
    )

    print("openclaw_authoritative_projection_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
