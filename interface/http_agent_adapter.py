from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.event_schema import VeyraTaskPacket


@dataclass(slots=True)
class AgentHttpConfig:
    name: str
    base_url: str = ""
    api_key: str = ""
    timeout: float = 20.0
    task_path: str = "/tasks"
    capabilities_path: str = "/capabilities"
    memory_summary_path: str = "/memory/summary"
    memory_patch_path: str = "/memory/patch"
    stop_path_template: str = "/tasks/{task_id}/stop"


class HttpAgentAdapter(AgentAdapter):
    """Common HTTP adapter for OpenClaw, Hermes, and custom Agent runtimes."""

    def __init__(self, config: AgentHttpConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.api_key = config.api_key
        self.timeout = config.timeout

    @classmethod
    def from_env(cls, name: str, env_prefix: str, **overrides: Any) -> "HttpAgentAdapter":
        config = AgentHttpConfig(
            name=name,
            base_url=str(overrides.get("base_url") or os.getenv(f"{env_prefix}_BASE_URL", "")),
            api_key=str(overrides.get("api_key") or os.getenv(f"{env_prefix}_API_KEY", "")),
            timeout=float(overrides.get("timeout") or os.getenv(f"{env_prefix}_TIMEOUT", "20")),
            task_path=str(overrides.get("task_path") or os.getenv(f"{env_prefix}_TASK_PATH", "/tasks")),
            capabilities_path=str(overrides.get("capabilities_path") or os.getenv(f"{env_prefix}_CAPABILITIES_PATH", "/capabilities")),
            memory_summary_path=str(overrides.get("memory_summary_path") or os.getenv(f"{env_prefix}_MEMORY_SUMMARY_PATH", "/memory/summary")),
            memory_patch_path=str(overrides.get("memory_patch_path") or os.getenv(f"{env_prefix}_MEMORY_PATCH_PATH", "/memory/patch")),
            stop_path_template=str(overrides.get("stop_path_template") or os.getenv(f"{env_prefix}_STOP_PATH_TEMPLATE", "/tasks/{task_id}/stop")),
        )
        return cls(config)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        if not self.base_url:
            return self._unconfigured_result(task_packet)
        payload = task_packet.to_dict()
        payload["rendered_prompt"] = self.render_prompt(task_packet)
        response = self._request("POST", self.config.task_path, payload)
        return self.receive_result({**response, "task_id": response.get("task_id", task_packet.task_id), "executor": self.config.name})

    def fetch_capabilities(self) -> dict[str, Any]:
        if not self.base_url:
            return {
                "runtime": self.config.name,
                "status": "adapter_unconfigured",
                "base_url": None,
                "tools": [],
                "skills": [],
                "requires_tool_proxy": True,
            }
        try:
            response = self._request("GET", self.config.capabilities_path)
            response.setdefault("runtime", self.config.name)
            response.setdefault("status", "available")
            return response
        except RuntimeError as exc:
            return {"runtime": self.config.name, "status": "unavailable", "base_url": self.base_url, "error": str(exc)}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        if not self.base_url:
            return super().fetch_memory_summary(session_id)
        separator = "&" if "?" in self.config.memory_summary_path else "?"
        return self._request("GET", f"{self.config.memory_summary_path}{separator}{urlencode({'session_id': session_id})}")

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        if self.base_url:
            self._request("POST", self.config.memory_patch_path, memory_patch)
        return None

    def stop_task(self, task_id: str) -> bool:
        if not self.base_url:
            return False
        path = self.config.stop_path_template.format(task_id=task_id)
        response = self._request("POST", path, {})
        return bool(response.get("stopped", True))

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            task_id=str(raw_result.get("task_id", "unknown")),
            executor=str(raw_result.get("executor", self.config.name)),
            status=str(raw_result.get("status", "submitted")),
            result=str(raw_result.get("result") or raw_result.get("message") or f"Task submitted to {self.config.name}."),
            logs=str(raw_result.get("logs", "")),
            changed_files=list(raw_result.get("changed_files", [])),
            tool_calls=list(raw_result.get("tool_calls", [])),
            raw=raw_result,
        )

    def connection_status(self) -> dict[str, Any]:
        if not self.base_url:
            return {
                "name": self.config.name,
                "connected": False,
                "status": "adapter_unconfigured",
                "base_url": None,
            }
        capabilities = self.fetch_capabilities()
        return {
            "name": self.config.name,
            "connected": capabilities.get("status") not in {"unavailable", "adapter_unconfigured"},
            "status": capabilities.get("status", "available"),
            "base_url": self.base_url,
            "capabilities": capabilities,
        }

    def _unconfigured_result(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        prompt = self.render_prompt(task_packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.config.name,
            status="adapter_unconfigured",
            result=f"{self.config.name} adapter is not connected. Configure its base_url before submitting real tasks.",
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
            raise RuntimeError(f"{self.config.name} request failed: {exc}") from exc
        if not body:
            return {"status": "success"}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"status": "success", "result": body}
