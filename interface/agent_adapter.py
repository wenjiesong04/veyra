from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from interface.agent_contract import (
    AGENT_CONTRACT_VERSION,
    TERMINAL_STATUSES,
    normalize_capabilities,
    normalize_execution_payload,
    render_prompt_payload,
)
from interface.event_schema import VeyraTaskPacket


@dataclass(slots=True)
class ExecutionResult:
    task_id: str
    executor: str
    status: str
    result: str
    logs: str = ""
    changed_files: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "executor": self.executor,
            "status": self.status,
            "result": self.result,
            "logs": self.logs,
            "changed_files": self.changed_files,
            "tool_calls": self.tool_calls,
            "raw": self.raw,
        }


class AgentAdapter(ABC):
    # This is local adapter construction evidence, not a provider-advertised
    # capability. Only an adapter implemented and instantiated by Veyra may
    # override it.
    trusted_native_provider_adapter = False

    @abstractmethod
    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        raise NotImplementedError

    def render_prompt(self, task_packet: VeyraTaskPacket) -> str:
        return render_prompt_payload(task_packet.to_dict())

    def fetch_capabilities(self) -> dict[str, Any]:
        return normalize_capabilities(
            {"tools": [], "skills": [], "permissions": "unknown", "status": "adapter_unconfigured"},
            runtime="unknown",
        )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": ""}

    def write_memory_patch(
        self,
        memory_patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        return None

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=task_id,
            executor="unknown",
            status="adapter_unconfigured",
            result="Agent adapter does not expose task status polling.",
            raw={"configured": False},
        )

    def fetch_bound_task_status(
        self,
        task_id: str,
        *,
        identity: dict[str, Any],
    ) -> ExecutionResult:
        """Recover a task using caller-held non-secret durable identity."""

        del identity
        return self.fetch_task_status(task_id)

    def poll_task(self, task_id: str, timeout_seconds: float = 0.0, interval_seconds: float = 1.0) -> ExecutionResult:
        if timeout_seconds <= 0:
            return self.fetch_task_status(task_id)

        import time

        deadline = time.monotonic() + timeout_seconds
        last = self.fetch_task_status(task_id)
        while last.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(max(0.05, interval_seconds))
            last = self.fetch_task_status(task_id)
        return last

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        payload = normalize_execution_payload(raw_result, default_task_id="unknown", default_executor="unknown", default_status="unknown")
        return ExecutionResult(**payload)

    def cancel_task_authority(
        self,
        task_id: str,
        *,
        reason: str = "user_requested_stop",
        identity: dict[str, Any] | None = None,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        del identity, abort_agent
        return {
            "status": "unsupported",
            "task_id": str(task_id or "").strip(),
            "reason": str(reason or "user_requested_stop")[:600],
            "authority_revoked": False,
            "agent_abort_confirmed": False,
        }

    def stop_task(self, task_id: str) -> bool:
        return (
            self.cancel_task_authority(task_id).get("status")
            == "cancelled"
        )

    def connection_status(self) -> dict[str, Any]:
        status = normalize_capabilities({"status": "adapter_unconfigured"}, runtime="unknown")
        status["contract_version"] = AGENT_CONTRACT_VERSION
        return status


class UnavailableAgentAdapter(AgentAdapter):
    """Fail-closed placeholder for an unavailable configured selection.

    The placeholder preserves the operator-selected provider identity without
    falling back to another runtime.  Every potentially mutating operation is
    terminal and side-effect free so non-Agent routes can remain available
    while the selected provider configuration is repaired.
    """

    def __init__(self, runtime: str, *, reason_code: str) -> None:
        self.runtime = str(runtime or "unknown")
        self.reason_code = str(reason_code or "selected_runtime_unavailable")

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return self._result(task_packet.task_id)

    def fetch_capabilities(self) -> dict[str, Any]:
        return normalize_capabilities(
            {
                "runtime": self.runtime,
                "status": "adapter_unconfigured",
                "connected": False,
                "tools": [],
                "skills": [],
                "permissions": "none",
                "features": {
                    "structured_task_packet": False,
                    "rendered_prompt_fallback": False,
                    "memory_summary": False,
                    "memory_patch": False,
                    "task_status": False,
                    "stop_task": False,
                    "bounded_agent_dialogue": False,
                },
                "reason": self.reason_code,
            },
            runtime=self.runtime,
        )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": str(session_id or ""),
            "summary": "",
            "runtime": self.runtime,
            "status": "not_configured",
            "dispatch_allowed": False,
            "reason": self.reason_code,
        }

    def write_memory_patch(
        self,
        memory_patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        del memory_patch
        return {
            "status": "blocked",
            "runtime": self.runtime,
            "configured": False,
            "dispatch_allowed": False,
            "reason": self.reason_code,
        }

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return self._result(task_id)

    def cancel_task_authority(
        self,
        task_id: str,
        *,
        reason: str = "user_requested_stop",
        identity: dict[str, Any] | None = None,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        del reason, identity, abort_agent
        return {
            "status": "unsupported",
            "task_id": str(task_id or "").strip(),
            "reason": self.reason_code,
            "authority_revoked": False,
            "agent_abort_confirmed": False,
        }

    def connection_status(self) -> dict[str, Any]:
        capabilities = self.fetch_capabilities()
        return {
            "name": self.runtime,
            "runtime": self.runtime,
            "status": "adapter_unconfigured",
            "connected": False,
            "transport_connected": False,
            "capabilities": capabilities,
            "contract_version": AGENT_CONTRACT_VERSION,
            "validation": {
                "implemented": True,
                "configured": False,
                "connected": False,
                "validated": False,
                "status": "not_configured",
                "runtime_status": "adapter_unconfigured",
            },
            "dispatch_allowed": False,
            "reason": self.reason_code,
        }

    def _result(self, task_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=str(task_id or "unknown"),
            executor=self.runtime,
            status="adapter_unconfigured",
            result=(
                f"The selected Agent runtime `{self.runtime}` is unavailable."
            ),
            raw={
                "configured": False,
                "dispatch_allowed": False,
                "reason": self.reason_code,
            },
        )
