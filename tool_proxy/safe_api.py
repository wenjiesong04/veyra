from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.tool_policy import ToolPolicy


class SafeAPI:
    def __init__(self, state_store: WorldStateStore | None = None, policy: ToolPolicy | None = None) -> None:
        self.state_store = state_store
        self.policy = policy or ToolPolicy()
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def request(self, payload: dict[str, Any], approved_by: str | None = None) -> dict[str, Any]:
        review = self.policy.review_api_request(payload)
        target = str(payload.get("url") or payload.get("endpoint") or "")
        self._record_policy("api_request", target, review, approved_by)
        if review["decision"] == "block":
            result = {"status": "blocked", "payload": self._redact(payload), "review": review}
            result["tool_trace"] = self._record("api_request", target, result, review, approved_by)
            return result
        if review["decision"] == "ask_user" and not approved_by:
            result = {"status": "needs_confirmation", "payload": self._redact(payload), "review": review}
            result["tool_trace"] = self._record("api_request", target, result, review, approved_by)
            return result
        result = {
            "status": "not_configured",
            "payload": self._redact(payload),
            "review": review,
            "reason": "SafeAPI policy passed, but outbound API execution is not configured in this runtime.",
            "approved_by": approved_by,
        }
        result["tool_trace"] = self._record("api_request", target, result, review, approved_by)
        return result

    def _record(self, action_type: str, target: str, payload: dict[str, Any], review: dict, approved_by: str | None = None) -> dict[str, Any]:
        return self.tool_trace.record(
            tool="safe_api",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(self, action_type: str, target: str, review: dict, approved_by: str | None = None) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_api",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )

    def _redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if key.lower() in {"authorization", "api_key", "token", "secret"}:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = value
        return redacted
