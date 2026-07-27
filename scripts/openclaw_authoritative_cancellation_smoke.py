#!/usr/bin/env python3
"""Prove OpenClaw cancellation closes authority in one exact order."""
from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_registry import AgentRegistry  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from interface.openclaw_adapter import (  # noqa: E402
    OpenClawAdapter,
    OpenClawGatewayError,
)


BINDING = "a" * 64


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def identity(run_id: str, session_key: str) -> dict[str, str]:
    return {
        "runId": run_id,
        "sessionKey": session_key,
        "bindingDigest": BINDING,
    }


def broker_result(
    run_id: str,
    *,
    status: str = "cancelled",
) -> dict[str, Any]:
    return {
        "status": status,
        "run_id": run_id,
        "binding_digest": BINDING,
        "revoked_grants": 1,
        "executing_reservations": (
            ["reservation-running"] if status == "too_late" else []
        ),
        "cancelled_reservations": (
            [] if status == "too_late" else ["reservation-pending"]
        ),
    }


def seed(
    adapter: OpenClawAdapter,
    *,
    run_id: str,
    session_key: str,
) -> None:
    adapter._remember_chat_run(
        run_id,
        status="running",
        result="running",
        task_context={"agent_execution_session_id": session_key},
        governance_identity=adapter._normalized_governance_identity(
            identity(run_id, session_key),
            run_id=run_id,
        ),
    )


def packet(task_id: str, session_key: str) -> VeyraTaskPacket:
    return VeyraTaskPacket(
        task_id=task_id,
        target_agent="openclaw",
        session_id="dialogue-phase3",
        user_message="Run a governed operation.",
        user_goal="Run a governed operation.",
        context_patch={},
        persona_patch={},
        policy_patch={"risk_level": "R2"},
        verification_policy={"requires_evidence": True},
        agent_execution_session_id=session_key,
    )


def check_stop_order_and_idempotency() -> None:
    calls: list[tuple[str, Any]] = []
    run_id = "run-stop"
    session_key = "agent:main:agent-exec:run-stop"

    def cancel_dispatch(
        observed_run_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        calls.append(("broker", (observed_run_id, reason)))
        return broker_result(observed_run_id)

    adapter = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_canceller=cancel_dispatch,
    )
    seed(adapter, run_id=run_id, session_key=session_key)

    def gateway(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, dict(params)))
        if method == "veyra.governance.cancelSession":
            expect(
                params
                == {
                    "sessionKey": session_key,
                    "runId": run_id,
                    "bindingDigest": BINDING,
                },
                "plugin cancellation receives the exact governed identity",
                params,
            )
            return {
                "cancelled": True,
                "idempotent": False,
                "sessionKey": session_key,
                "runId": run_id,
            }
        if method == "chat.abort":
            expect(
                params
                == {
                    "sessionKey": session_key,
                    "agentId": "main",
                    "runId": run_id,
                },
                "Agent abort receives the exact canonical session and run",
                params,
            )
            return {"aborted": True, "runId": run_id}
        raise AssertionError(f"unexpected gateway call: {method}")

    adapter._gateway_request = gateway  # type: ignore[assignment]
    expect(adapter.stop_task(run_id), "fully closed stop reports success")
    expect(
        [item[0] for item in calls]
        == [
            "broker",
            "veyra.governance.cancelSession",
            "chat.abort",
        ],
        "stop closes broker, plugin, then Agent in order",
        calls,
    )
    first_call_count = len(calls)
    expect(
        adapter.stop_task(run_id),
        "repeated stop preserves the confirmed result",
    )
    expect(
        len(calls) == first_call_count,
        "repeated stop does not repeat completed cancellation effects",
        calls,
    )


def check_legacy_abort_truth() -> None:
    legacy = OpenClawAdapter(base_url="ws://127.0.0.1:18789")
    calls: list[tuple[str, dict[str, Any]]] = []
    legacy_session = "agent:main:agent-exec:legacy-run"
    seed(
        legacy,
        run_id="legacy-run",
        session_key=legacy_session,
    )

    def confirmed(
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append((method, dict(params)))
        return {"ok": True, "runId": params["runId"]}

    legacy._gateway_request = confirmed  # type: ignore[assignment]
    expect(
        legacy.stop_task("legacy-run"),
        "ungoverned adapter preserves confirmed legacy abort",
    )
    expect(
        calls
        == [
            (
                "chat.abort",
                {
                    "sessionKey": legacy_session,
                    "agentId": "main",
                    "runId": "legacy-run",
                },
            )
        ],
        "legacy stop does not invent governance RPCs",
        calls,
    )

    unconfirmed = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789"
    )
    seed(
        unconfirmed,
        run_id="legacy-not-aborted",
        session_key="agent:main:agent-exec:legacy-not-aborted",
    )
    unconfirmed._gateway_request = (  # type: ignore[assignment]
        lambda _method, _params: {
            "aborted": False,
            "ok": False,
            "status": "success",
        }
    )
    expect(
        not unconfirmed.stop_task("legacy-not-aborted"),
        "explicitly false legacy abort is not reported successful",
    )


def check_failure_and_too_late_truth() -> None:
    calls: list[str] = []
    run_id = "run-plugin-failure"
    session_key = "agent-exec:run-plugin-failure"

    def cancel_dispatch(
        observed_run_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        del reason
        calls.append("broker")
        return broker_result(observed_run_id)

    adapter = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_canceller=cancel_dispatch,
    )
    seed(adapter, run_id=run_id, session_key=session_key)

    def gateway(method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        calls.append(method)
        if method == "veyra.governance.cancelSession":
            raise OpenClawGatewayError(
                "unavailable",
                "plugin connection failed",
            )
        return {"aborted": True}

    adapter._gateway_request = gateway  # type: ignore[assignment]
    expect(
        not adapter.stop_task(run_id),
        "plugin failure never reports authoritative stop success",
    )
    expect(
        calls
        == [
            "broker",
            "veyra.governance.cancelSession",
            "chat.abort",
        ],
        "Agent abort remains best effort after plugin failure",
        calls,
    )
    cleanup = adapter._run_cache[run_id]["governance_cleanup"]
    expect(
        cleanup["authority_revoked"] is True
        and cleanup["plugin_authority_closed"] is False
        and cleanup["agent_abort_confirmed"] is True,
        "partial closure is reported without weakening broker revocation",
        cleanup,
    )

    too_late_calls: list[str] = []
    too_late_attempts = 0
    too_late_run = "run-too-late"
    too_late_session = "agent-exec:run-too-late"

    def too_late_dispatch(
        observed_run_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        nonlocal too_late_attempts
        del reason
        too_late_calls.append("broker")
        too_late_attempts += 1
        return broker_result(
            observed_run_id,
            status=(
                "too_late" if too_late_attempts == 1 else "cancelled"
            ),
        )

    too_late = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_canceller=too_late_dispatch,
    )
    seed(
        too_late,
        run_id=too_late_run,
        session_key=too_late_session,
    )

    def too_late_gateway(
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del params
        too_late_calls.append(method)
        if method == "veyra.governance.cancelSession":
            return {
                "cancelled": True,
                "idempotent": False,
                "sessionKey": too_late_session,
                "runId": too_late_run,
            }
        return {"aborted": True}

    too_late._gateway_request = too_late_gateway  # type: ignore[assignment]
    expect(
        not too_late.stop_task(too_late_run),
        "irreversible in-flight execution is never reported stopped",
    )
    too_late_cleanup = too_late._run_cache[too_late_run][
        "governance_cleanup"
    ]
    expect(
        too_late_cleanup["status"] == "too_late"
        and too_late_cleanup["broker"]["executing_reservations"]
        == ["reservation-running"],
        "too-late result preserves the exact in-flight reservation",
        too_late_cleanup,
    )
    converged = too_late.receive_result(
        {
            "task_id": too_late_run,
            "executor": "openclaw",
            "status": "success",
            "result": "execution completed",
        }
    )
    expect(
        converged.raw["governance_cleanup"]["status"] == "cancelled"
        and too_late_attempts == 2,
        "terminal projection retries a previously too-late broker close",
        converged.to_dict(),
    )
    converged_call_count = len(too_late_calls)
    expect(
        too_late.stop_task(too_late_run),
        "terminal convergence makes later stop truthfully idempotent",
    )
    expect(
        len(too_late_calls) == converged_call_count,
        "converged stop reuses broker, plugin and Agent confirmations",
        too_late_calls,
    )

    abort_false_run = "run-abort-false"
    abort_false_session = "agent-exec:run-abort-false"
    abort_false = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_canceller=lambda observed_run_id, **_kwargs: (
            broker_result(observed_run_id)
        ),
    )
    seed(
        abort_false,
        run_id=abort_false_run,
        session_key=abort_false_session,
    )

    def abort_false_gateway(
        method: str,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        if method == "veyra.governance.cancelSession":
            return {
                "cancelled": True,
                "idempotent": False,
                "sessionKey": abort_false_session,
                "runId": abort_false_run,
            }
        return {
            "aborted": False,
            "ok": False,
            "status": "success",
        }

    abort_false._gateway_request = (  # type: ignore[assignment]
        abort_false_gateway
    )
    expect(
        not abort_false.stop_task(abort_false_run),
        "governed stop rejects an explicitly false chat.abort body",
    )


def check_terminal_cleanup_after_projection() -> None:
    calls: list[str] = []
    run_id = "run-terminal"
    session_key = "agent-exec:run-terminal"

    def resolve(observed_run_id: str) -> dict[str, Any]:
        calls.append("evidence")
        return {
            "run_id": observed_run_id,
            "tool_calls": [],
            "changed_files": [],
            "tool_receipt_refs": [],
            "observed_call_count": 0,
            "effect_count": 0,
        }

    def cancel_dispatch(
        observed_run_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        del reason
        calls.append("broker")
        return broker_result(observed_run_id)

    adapter = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_canceller=cancel_dispatch,
        governance_run_evidence_resolver=resolve,
    )
    seed(adapter, run_id=run_id, session_key=session_key)

    def gateway(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        expect(
            method == "veyra.governance.cancelSession",
            "terminal cleanup does not abort an already terminal run",
            (method, params),
        )
        return {
            "cancelled": True,
            "idempotent": False,
            "sessionKey": session_key,
            "runId": run_id,
        }

    adapter._gateway_request = gateway  # type: ignore[assignment]
    payload = {
        "task_id": run_id,
        "executor": "openclaw",
        "status": "success",
        "result": "done",
    }
    first = adapter.receive_result(payload)
    second = adapter.receive_result(payload)
    expect(
        calls
        == [
            "evidence",
            "broker",
            "veyra.governance.cancelSession",
            "evidence",
        ],
        "terminal evidence is projected before one idempotent authority cleanup",
        calls,
    )
    expect(
        first.raw["governance_cleanup"]["status"] == "cancelled"
        and second.raw["governance_cleanup"]["status"] == "cancelled",
        "repeated terminal projection reuses confirmed cleanup",
        (first.to_dict(), second.to_dict()),
    )


def check_submission_failure_cleanup() -> None:
    calls: list[tuple[str, Any]] = []
    captured: dict[str, str] = {}
    session_key = "agent-exec:submission-failure"

    def prepare(
        _packet: VeyraTaskPacket,
        *,
        run_id: str,
        session_key: str,
    ) -> dict[str, Any]:
        captured["run_id"] = run_id
        captured["session_key"] = session_key
        return {
            **identity(run_id, session_key),
            "dispatchToken": "do-not-expose",
            "expiresAt": "2026-07-27T12:00:00+00:00",
            "allowedTools": ["veyra_file_read"],
        }

    def cancel_dispatch(
        observed_run_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        calls.append(("broker", (observed_run_id, reason)))
        return broker_result(observed_run_id)

    adapter = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_preparer=prepare,
        governance_dispatch_canceller=cancel_dispatch,
    )

    def fail_send(
        _message: str,
        *,
        session_key: str | None = None,
        idempotency_key: str | None = None,
        governance_registration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(
            (
                "send",
                {
                    "session_key": session_key,
                    "idempotency_key": idempotency_key,
                    "governed": governance_registration is not None,
                },
            )
        )
        raise OpenClawGatewayError(
            "unavailable",
            "chat.send failed after registration",
        )

    def gateway(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, dict(params)))
        if method == "veyra.governance.cancelSession":
            return {
                "cancelled": True,
                "idempotent": False,
                "sessionKey": captured["session_key"],
                "runId": captured["run_id"],
            }
        return {"aborted": True, "runId": captured["run_id"]}

    adapter._send_chat = fail_send  # type: ignore[assignment]
    adapter._gateway_request = gateway  # type: ignore[assignment]
    result = adapter.send_task(
        packet("task-submission-failure", session_key)
    )
    expect(
        result.task_id == captured["run_id"]
        and result.task_id.startswith("veyra-"),
        "submission failure returns the generated governed run id",
        result.to_dict(),
    )
    expect(
        [item[0] for item in calls]
        == [
            "send",
            "broker",
            "veyra.governance.cancelSession",
            "chat.abort",
        ],
        "submission failure closes all authority in exact order",
        calls,
    )
    expect(
        result.raw["governance_cleanup"]["status"] == "cancelled",
        "submission failure exposes confirmed cleanup",
        result.to_dict(),
    )


def check_registry_wiring() -> None:
    def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def cancel(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def evidence(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    with tempfile.TemporaryDirectory(
        prefix="veyra-openclaw-cancellation-"
    ) as raw_tmp:
        registry = AgentRegistry(
            WorldStateStore(Path(raw_tmp) / "state")
        )
        registry.configure_openclaw_governance(
            dispatch_preparer=prepare,
            dispatch_canceller=cancel,
            run_evidence_resolver=evidence,
            status_resolver=status,
        )
        adapter = registry.get("openclaw")
        expect(
            isinstance(adapter, OpenClawAdapter)
            and adapter._governance_dispatch_canceller is cancel,
            "AgentRegistry injects the trusted canceller into refreshed adapters",
        )


def main() -> int:
    check_stop_order_and_idempotency()
    check_legacy_abort_truth()
    check_failure_and_too_late_truth()
    check_terminal_cleanup_after_projection()
    check_submission_failure_cleanup()
    check_registry_wiring()
    print("openclaw_authoritative_cancellation_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
