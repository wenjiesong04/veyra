from __future__ import annotations

import webbrowser
from typing import Callable
from urllib.parse import urlparse

from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.tool_policy import ToolPolicy


class SafeBrowser:
    def __init__(
        self,
        state_store: WorldStateStore | None = None,
        policy: ToolPolicy | None = None,
        executor: Callable[[str], dict] | None = None,
        executor_configured: bool = False,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.state_store = state_store
        self.policy = policy or ToolPolicy()
        self.executor = executor
        self.executor_configured = executor_configured
        self.allowed_hosts = allowed_hosts or ["localhost", "127.0.0.1", "::1"]
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def status(self) -> dict:
        return {
            "tool": "safe_browser",
            "configured": bool(self.executor or self.executor_configured),
            "mode": "custom_executor" if self.executor else "system_browser",
            "default_enabled": False,
            "allowed_hosts": self.allowed_hosts,
        }

    def configure_executor(self, enabled: bool, allowed_hosts: list[str] | None = None) -> None:
        self.executor_configured = enabled
        if allowed_hosts is not None:
            self.allowed_hosts = self._normalize_hosts(allowed_hosts)

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
        if self.executor or self.executor_configured:
            if not self._host_allowed(url):
                result = {"status": "blocked", "url": url, "review": review, "reason": "browser host is not in executor allowlist", "allowed_hosts": self.allowed_hosts}
                result["tool_trace"] = self._record("browser_open", url, result, review, approved_by)
                return result
            result = self.executor(url) if self.executor else self._execute_system_browser(url)
            result.setdefault("url", url)
            result.setdefault("review", review)
            result.setdefault("approved_by", approved_by)
        else:
            result = {
                "status": "not_configured",
                "url": url,
                "review": review,
                "reason": "SafeBrowser policy passed, but browser execution is not configured in this runtime.",
                "approved_by": approved_by,
            }
        result["tool_trace"] = self._record("browser_open", url, result, review, approved_by)
        return result

    def _execute_system_browser(self, url: str) -> dict:
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:
            return {"status": "error", "url": url, "reason": str(exc)}
        return {"status": "ok" if opened else "error", "url": url, "opened": opened}

    def _host_allowed(self, url: str) -> bool:
        allowed = set(self.allowed_hosts)
        if "*" in allowed:
            return True
        host = urlparse(url).hostname or ""
        return host in allowed or any(host.endswith(item) for item in allowed if item.startswith("."))

    def _normalize_hosts(self, hosts: list[str]) -> list[str]:
        if not isinstance(hosts, list):
            return ["localhost", "127.0.0.1", "::1"]
        normalized = [str(item).strip().lower() for item in hosts if str(item).strip()]
        return normalized or ["localhost", "127.0.0.1", "::1"]

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
