from __future__ import annotations

from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.tool_policy import ToolPolicy


class SafeBrowser:
    def __init__(self, state_store: WorldStateStore | None = None, policy: ToolPolicy | None = None) -> None:
        self.state_store = state_store
        self.policy = policy or ToolPolicy()
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def open(self, url: str, approved_by: str | None = None) -> dict:
        review = self.policy.review_browser_open(url)
        self._record_policy("browser_open", url, review, approved_by)
        if review["decision"] == "block":
            result = {"status": "blocked", "url": url, "review": review}
            result["tool_trace"] = self._record("browser_open", url, result, review, approved_by)
            return result
        if review["decision"] == "ask_user" and not approved_by:
            result = {"status": "needs_confirmation", "url": url, "review": review}
            result["tool_trace"] = self._record("browser_open", url, result, review, approved_by)
            return result
        result = {
            "status": "not_configured",
            "url": url,
            "review": review,
            "reason": "SafeBrowser policy passed, but browser execution is not configured in this runtime.",
            "approved_by": approved_by,
        }
        result["tool_trace"] = self._record("browser_open", url, result, review, approved_by)
        return result

    def _record(self, action_type: str, target: str, payload: dict, review: dict, approved_by: str | None = None) -> dict:
        return self.tool_trace.record(
            tool="safe_browser",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(self, action_type: str, target: str, review: dict, approved_by: str | None = None) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_browser",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )
