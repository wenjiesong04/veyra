from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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


class AgentAdapter(ABC):
    @abstractmethod
    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        raise NotImplementedError

    def render_prompt(self, task_packet: VeyraTaskPacket) -> str:
        return (
            f"Task: {task_packet.user_message}\n"
            f"Context: {task_packet.context_patch}\n"
            f"Persona: {task_packet.persona_patch}\n"
            f"Policy: {task_packet.policy_patch}"
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        return {"tools": [], "skills": [], "permissions": "unknown"}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": ""}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        return None

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            task_id=raw_result.get("task_id", "unknown"),
            executor=raw_result.get("executor", "unknown"),
            status=raw_result.get("status", "unknown"),
            result=raw_result.get("result", ""),
            raw=raw_result,
        )

    def stop_task(self, task_id: str) -> bool:
        return False

    def connection_status(self) -> dict[str, Any]:
        return {"connected": False, "status": "adapter_unconfigured", "base_url": None}
