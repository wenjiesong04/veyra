from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from interface.agent_contract import (
    AGENT_CONTRACT_VERSION,
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

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        return None

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        payload = normalize_execution_payload(raw_result, default_task_id="unknown", default_executor="unknown", default_status="unknown")
        return ExecutionResult(**payload)

    def stop_task(self, task_id: str) -> bool:
        return False

    def connection_status(self) -> dict[str, Any]:
        status = normalize_capabilities({"status": "adapter_unconfigured"}, runtime="unknown")
        status["contract_version"] = AGENT_CONTRACT_VERSION
        return status
