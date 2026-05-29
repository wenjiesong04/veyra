from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.tool_policy import ToolPolicy


class SafeAPI:
    def __init__(
        self,
        state_store: WorldStateStore | None = None,
        policy: ToolPolicy | None = None,
        executor_configured: bool = False,
        executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.state_store = state_store
        self.policy = policy or ToolPolicy()
        self.executor_configured = executor_configured
        self.executor = executor
        self.allowed_hosts = allowed_hosts or ["localhost", "127.0.0.1", "::1"]
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def status(self) -> dict[str, Any]:
        configured = bool(self.executor or self.executor_configured)
        execution_mode = "allowlist_only" if configured else "disabled"
        return {
            "tool": "safe_api",
            "configured": configured,
            "mode": "custom_executor" if self.executor else "http_urlopen",
            "execution_mode": execution_mode,
            "default_enabled": False,
            "allowed_hosts": self.allowed_hosts,
            "validation": {
                "implemented": True,
                "configured": configured,
                "validated": configured,
                "status": "validated" if configured else "not_configured",
            },
        }

    def configure_executor(self, enabled: bool, allowed_hosts: list[str] | None = None) -> None:
        self.executor_configured = enabled
        if allowed_hosts is not None:
            self.allowed_hosts = self._normalize_hosts(allowed_hosts)

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
        if self.executor or self.executor_configured:
            if not self._host_allowed(target):
                result = {
                    "status": "blocked",
                    "payload": self._redact(payload),
                    "review": review,
                    "reason": "API host is not in executor allowlist",
                    "allowed_hosts": self.allowed_hosts,
                    "approved_by": approved_by,
                    "validation": {"executor_configured": True, "host_allowed": False, "status": "blocked"},
                }
                result["tool_trace"] = self._record("api_request", target, result, review, approved_by)
                return result
            result = self._execute(payload)
            result["payload"] = self._redact(payload)
            result["review"] = review
            result["approved_by"] = approved_by
            result["validation"] = {"executor_configured": True, "host_allowed": True, "status": "validated" if result.get("status") == "ok" else "validation_pending"}
            result["execution_attempted"] = True
            result["execution_mode"] = "allowlist_only"
        else:
            result = {
                "status": "not_configured",
                "payload": self._redact(payload),
                "review": review,
                "reason": "SafeAPI policy passed, but outbound API execution is not configured in this runtime.",
                "approved_by": approved_by,
                "validation": {"executor_configured": False, "host_allowed": None, "status": "not_configured"},
                "execution_attempted": False,
                "execution_mode": "disabled",
                "next_step": "Configure /tool-proxy/config with api_executor_enabled=true and allowlist hosts.",
            }
        result["tool_trace"] = self._record("api_request", target, result, review, approved_by)
        return result

    def _execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.executor:
            return self.executor(payload)
        method = str(payload.get("method", "GET")).upper()
        url = str(payload.get("url") or payload.get("endpoint") or "")
        if not url:
            return {"status": "error", "reason": "API payload requires url or endpoint"}
        body = payload.get("body") if "body" in payload else payload.get("json")
        data = None
        headers = {"Accept": "application/json", **(payload.get("headers") if isinstance(payload.get("headers"), dict) else {})}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=float(payload.get("timeout") or 10)) as response:
                content = response.read().decode("utf-8", errors="replace")
                return {"status": "ok", "status_code": response.status, "body": content[:8000]}
        except (HTTPError, URLError, TimeoutError) as exc:
            return {"status": "error", "reason": str(exc)}

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
            elif isinstance(value, dict):
                redacted[key] = self._redact(value)
            else:
                redacted[key] = value
        return redacted
