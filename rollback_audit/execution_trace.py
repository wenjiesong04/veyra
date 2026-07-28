from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from tool_proxy.governance_contract import canonical_sha256


VERIFIER_OBSERVATION_SCHEMA = "veyra.verifier_observation.v1"


class ExecutionTrace:
    def __init__(
        self,
        state_store: WorldStateStore | None = None,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or utc_now_iso

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        recorded_at = str(self._clock())
        trace = {
            "trace_id": str(payload.get("trace_id") or f"exec_{uuid4().hex[:12]}"),
            "event_id": payload.get("event_id"),
            "route": payload.get("route"),
            "task_id": payload.get("task_id"),
            "executor": payload.get("executor"),
            "status": payload.get("status"),
            "decision": payload.get("decision"),
            "guardian": payload.get("guardian"),
            "execution_result": payload.get("execution_result"),
            "verification": payload.get("verification"),
            "artifacts": payload.get("artifacts", {}),
            "recorded_at": recorded_at,
        }
        persisted_trace = {
            **trace,
            "verifier_observation": self._verifier_observation(
                trace
            ),
        }
        if self.state_store:
            self.state_store.append_jsonl(
                "execution_trace.jsonl",
                persisted_trace,
            )
        return trace

    @staticmethod
    def _verifier_observation(
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        verification = (
            trace.get("verification")
            if isinstance(trace.get("verification"), dict)
            else {}
        )
        evidence = (
            verification.get("evidence")
            if isinstance(verification.get("evidence"), dict)
            else {}
        )
        execution_result = (
            trace.get("execution_result")
            if isinstance(trace.get("execution_result"), dict)
            else {}
        )
        semantics = {
            "schema_version": VERIFIER_OBSERVATION_SCHEMA,
            "trace_id": str(trace.get("trace_id") or ""),
            "event_id": (
                str(trace.get("event_id"))
                if trace.get("event_id") is not None
                else None
            ),
            "route": str(trace.get("route") or ""),
            "task_id": str(trace.get("task_id") or ""),
            "executor": str(trace.get("executor") or ""),
            "trace_status": str(trace.get("status") or ""),
            "verification_status": str(
                verification.get("status") or ""
            ),
            "verification_verdict": str(
                verification.get("verdict") or ""
            ),
            "evidence_digest": canonical_sha256(evidence),
            "execution_result_digest": canonical_sha256(
                execution_result
            ),
            "recorded_at": str(trace.get("recorded_at") or ""),
        }
        return {
            **semantics,
            "binding_digest": canonical_sha256(semantics),
        }
