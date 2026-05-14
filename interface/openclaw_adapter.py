from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.event_schema import VeyraTaskPacket


class OpenClawAdapter(AgentAdapter):
    """HTTP adapter for a selected OpenClaw runtime.

    Configure with:
    - OPENCLAW_BASE_URL, for example http://127.0.0.1:18789
    - OPENCLAW_API_KEY, optional bearer token
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 20.0) -> None:
        self.base_url = (base_url or os.getenv("OPENCLAW_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENCLAW_API_KEY", "")
        self.timeout = timeout

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        if not self.base_url:
            return self._unconfigured_result(task_packet)
        payload = task_packet.to_dict()
        payload["rendered_prompt"] = self.render_prompt(task_packet)
        response = self._request("POST", "/tasks", payload)
        return self.receive_result({**response, "task_id": response.get("task_id", task_packet.task_id), "executor": "openclaw"})

    def fetch_capabilities(self) -> dict[str, object]:
        if not self.base_url:
            return {
                "runtime": "openclaw",
                "status": "adapter_unconfigured",
                "base_url": None,
                "tools": [],
                "requires_tool_proxy": True,
            }
        try:
            return self._request("GET", "/capabilities")
        except RuntimeError as exc:
            return {"runtime": "openclaw", "status": "unavailable", "base_url": self.base_url, "error": str(exc)}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        if not self.base_url:
            return super().fetch_memory_summary(session_id)
        return self._request("GET", f"/memory/summary?{urlencode({'session_id': session_id})}")

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        if not self.base_url:
            return None
        self._request("POST", "/memory/patch", memory_patch)
        return None

    def stop_task(self, task_id: str) -> bool:
        if not self.base_url:
            return False
        response = self._request("POST", f"/tasks/{task_id}/stop", {})
        return bool(response.get("stopped", True))

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            task_id=str(raw_result.get("task_id", "unknown")),
            executor=str(raw_result.get("executor", "openclaw")),
            status=str(raw_result.get("status", "submitted")),
            result=str(raw_result.get("result") or raw_result.get("message") or "Task submitted to OpenClaw."),
            logs=str(raw_result.get("logs", "")),
            changed_files=list(raw_result.get("changed_files", [])),
            tool_calls=list(raw_result.get("tool_calls", [])),
            raw=raw_result,
        )

    def _unconfigured_result(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        prompt = self.render_prompt(task_packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor="openclaw",
            status="adapter_unconfigured",
            result="OpenClaw adapter is not connected. Set OPENCLAW_BASE_URL to submit this VeyraTaskPacket to a real runtime.",
            logs=prompt,
            raw={"task_packet": task_packet.to_dict(), "configured": False},
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"OpenClaw request failed: {exc}") from exc
        if not body:
            return {"status": "success"}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"status": "success", "result": body}

    def connection_status(self) -> dict[str, Any]:
        if not self.base_url:
            return {"connected": False, "status": "adapter_unconfigured", "base_url": None}
        capabilities = self.fetch_capabilities()
        return {
            "connected": capabilities.get("status") not in {"unavailable", "adapter_unconfigured"},
            "status": capabilities.get("status", "available"),
            "base_url": self.base_url,
            "capabilities": capabilities,
        }
