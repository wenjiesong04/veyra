from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from interface.agent_contract import AGENT_CONTRACT_VERSION, normalize_capabilities, normalize_execution_payload, validate_task_packet_payload
from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.event_schema import VeyraTaskPacket
from interface.provider_certification import certify_agent_provider


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
    status_path_template: str = "/tasks/{task_id}"
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
            status_path_template=str(overrides.get("status_path_template") or os.getenv(f"{env_prefix}_STATUS_PATH_TEMPLATE", "/tasks/{task_id}")),
            stop_path_template=str(overrides.get("stop_path_template") or os.getenv(f"{env_prefix}_STOP_PATH_TEMPLATE", "/tasks/{task_id}/stop")),
        )
        return cls(config)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        if not self.base_url:
            return self._unconfigured_result(task_packet)
        task_payload = task_packet.to_dict()
        validation_errors = validate_task_packet_payload(task_payload)
        if validation_errors:
            return ExecutionResult(
                task_id=task_packet.task_id,
                executor=self.config.name,
                status="failed",
                result="Invalid VeyraTaskPacket for Agent adapter.",
                raw={"validation_errors": validation_errors, "task_packet": task_payload},
            )
        capabilities = self.fetch_capabilities()
        certification = self._provider_certification(capabilities)
        if certification.get("validated") is not True:
            return ExecutionResult(
                task_id=task_packet.task_id,
                executor=self.config.name,
                status="blocked",
                result=(
                    f"{self.config.name} dispatch is blocked until a fresh "
                    "compatible Veyra Agent contract is verified."
                ),
                raw={
                    "configured": True,
                    "dispatch_allowed": False,
                    "certification_status": certification.get(
                        "certification_status"
                    ),
                    "certification_issues": certification.get("issues", []),
                    "contract_version": AGENT_CONTRACT_VERSION,
                },
            )
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.config.name,
            status="blocked",
            result=(
                f"{self.config.name} is protocol-compatible, but generic "
                "provider task dispatch has no locally enforced execution "
                "profile."
            ),
            raw={
                "configured": True,
                "dispatch_allowed": False,
                "reason": "generic_provider_dispatch_not_certified",
                "certification_status": certification.get(
                    "certification_status"
                ),
                "contract_version": AGENT_CONTRACT_VERSION,
            },
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        if not self.base_url:
            return normalize_capabilities(
                {
                    "runtime": self.config.name,
                    "status": "adapter_unconfigured",
                    "base_url": None,
                    "tools": [],
                    "skills": [],
                    "requires_tool_proxy": True,
                },
                runtime=self.config.name,
                base_url=None,
            )
        try:
            response = self._request("GET", self.config.capabilities_path)
            response.setdefault("status", "available")
            return normalize_capabilities(response, runtime=self.config.name, base_url=self.base_url)
        except RuntimeError as exc:
            return normalize_capabilities(
                {"runtime": self.config.name, "status": "unavailable", "base_url": self.base_url, "error": str(exc)},
                runtime=self.config.name,
                base_url=self.base_url,
            )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        if not self.base_url:
            return super().fetch_memory_summary(session_id)
        certification = self._provider_certification(
            self.fetch_capabilities()
        )
        if certification.get("validated") is not True:
            return {
                "session_id": session_id,
                "summary": "",
                "runtime": self.config.name,
                "status": "blocked",
                "dispatch_allowed": False,
                "certification_status": certification.get(
                    "certification_status"
                ),
                "certification_issues": certification.get("issues", []),
            }
        separator = "&" if "?" in self.config.memory_summary_path else "?"
        return self._request("GET", f"{self.config.memory_summary_path}{separator}{urlencode({'session_id': session_id})}")

    def write_memory_patch(
        self,
        memory_patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.base_url:
            return {
                "status": "not_configured",
                "dispatch_allowed": False,
            }
        del memory_patch
        return {
            "status": "blocked",
            "dispatch_allowed": False,
            "reason": "generic_provider_memory_write_not_certified",
        }

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        if not self.base_url:
            return ExecutionResult(
                task_id=task_id,
                executor=self.config.name,
                status="adapter_unconfigured",
                result=f"{self.config.name} adapter is not connected. Configure its base_url before polling tasks.",
                raw={"configured": False},
            )
        certification = self._provider_certification(
            self.fetch_capabilities()
        )
        if certification.get("validated") is not True:
            return ExecutionResult(
                task_id=task_id,
                executor=self.config.name,
                status="blocked",
                result=(
                    f"{self.config.name} task status is blocked until a "
                    "fresh compatible Veyra Agent contract is verified."
                ),
                raw={
                    "dispatch_allowed": False,
                    "certification_status": certification.get(
                        "certification_status"
                    ),
                    "certification_issues": certification.get("issues", []),
                },
            )
        path = self.config.status_path_template.format(task_id=task_id)
        try:
            response = self._request("GET", path)
        except RuntimeError as exc:
            return ExecutionResult(
                task_id=task_id,
                executor=self.config.name,
                status="error",
                result=f"{self.config.name} task status request failed: {exc}",
                raw={"error": str(exc)},
            )
        response.setdefault("task_id", task_id)
        response.setdefault("executor", self.config.name)
        return self.receive_result(response)

    def stop_task(self, task_id: str) -> bool:
        del task_id
        return False

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        payload = normalize_execution_payload(
            raw_result,
            default_task_id="unknown",
            default_executor=self.config.name,
            default_status="submitted",
        )
        if not payload["result"]:
            payload["result"] = f"Task submitted to {self.config.name}."
        return ExecutionResult(**payload)

    def connection_status(self) -> dict[str, Any]:
        if not self.base_url:
            status = self.fetch_capabilities()
            status["name"] = self.config.name
            status["validation"] = self._validation(False, False, str(status.get("status") or "adapter_unconfigured"))
            return status
        capabilities = self.fetch_capabilities()
        certification = self._provider_certification(capabilities)
        capabilities = {
            **capabilities,
            "updated_at": certification.get("observed_at"),
            "ttl_seconds": certification.get("ttl_seconds") or 300,
        }
        transport_connected = (
            capabilities.get("status")
            not in {"unavailable", "adapter_unconfigured"}
        )
        validated = certification.get("validated") is True
        status = {
            "name": self.config.name,
            "connected": validated,
            "transport_connected": transport_connected,
            "status": (
                capabilities.get("status", "available")
                if validated
                else "compatibility_unverified"
                if transport_connected
                else capabilities.get("status", "unavailable")
            ),
            "base_url": self.base_url,
            "capabilities": capabilities,
            "provider_certification": certification,
        }
        status["validation"] = self._validation(
            True,
            validated,
            str(status.get("status") or "unknown"),
        )
        status["contract_version"] = AGENT_CONTRACT_VERSION
        return status

    def _provider_certification(
        self,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        observed_at = datetime.now(timezone.utc)
        return certify_agent_provider(
            runtime=self.config.name,
            capabilities=capabilities,
            observed_at=observed_at,
            now=observed_at,
            ttl_seconds=300,
        )

    def _unconfigured_result(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        prompt = self.render_prompt(task_packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.config.name,
            status="adapter_unconfigured",
            result=f"{self.config.name} adapter is not connected. Configure its base_url before submitting real tasks.",
            logs=prompt,
            raw={"contract_version": AGENT_CONTRACT_VERSION, "task_packet": task_packet.to_dict(), "configured": False},
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

    def _validation(self, configured: bool, connected: bool, runtime_status: str) -> dict[str, Any]:
        return {
            "implemented": True,
            "configured": configured,
            "connected": connected,
            "validated": connected,
            "status": "validated" if connected else "validation_pending" if configured else "not_configured",
            "runtime_status": runtime_status,
        }
