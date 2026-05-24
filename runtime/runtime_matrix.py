from __future__ import annotations

from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.agent_registry import AgentRegistry
from interface.event_schema import utc_now_iso
from memory_bridge.local_memory_bridge import LocalMemoryBridge


class RuntimeMatrix:
    """Runs a read-mostly capability matrix across configured Agent runtimes."""

    def __init__(self, state_store: WorldStateStore, registry: AgentRegistry, memory_bridge: LocalMemoryBridge) -> None:
        self.state_store = state_store
        self.registry = registry
        self.memory_bridge = memory_bridge

    def status(self) -> dict[str, Any]:
        return self.state_store.read_json("ops_runtime_matrix.json") or {"status": "not_run", "runtimes": []}

    def run(self, *, write_memory_probe: bool = False) -> dict[str, Any]:
        self.registry.refresh()
        runtimes = [self._runtime_row(name, write_memory_probe=write_memory_probe) for name in self.registry.names()]
        summary = {
            "total": len(runtimes),
            "ready": len([item for item in runtimes if item["status"] == "ready"]),
            "degraded": len([item for item in runtimes if item["status"] == "degraded"]),
            "not_configured": len([item for item in runtimes if item["status"] == "not_configured"]),
            "error": len([item for item in runtimes if item["status"] == "error"]),
        }
        status = "ready" if summary["ready"] and not (summary["degraded"] or summary["error"]) else "not_configured" if summary["not_configured"] == summary["total"] else "degraded"
        result = {
            "status": status,
            "checked_at": utc_now_iso(),
            "write_memory_probe": write_memory_probe,
            "summary": summary,
            "runtimes": runtimes,
        }
        self.state_store.write_json("ops_runtime_matrix.json", result)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "ops_runtime_matrix", "status": status, "artifacts": redact_sensitive(result, max_string=2200)})
        return result

    def _runtime_row(self, name: str, *, write_memory_probe: bool) -> dict[str, Any]:
        adapter = self.registry.get(name)
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            return {"name": name, "status": "error", "error": str(exc)}
        try:
            capabilities = adapter.fetch_capabilities()
        except Exception as exc:
            capabilities = {"status": "error", "error": str(exc)}
        try:
            memory = self.memory_bridge.provider_diagnostics(provider=name, session_id=f"runtime-matrix-{name}", write_probe=write_memory_probe)
        except Exception as exc:
            memory = {"status": "error", "error": str(exc)}
        try:
            task_status = adapter.fetch_task_status("veyra_runtime_matrix_probe").to_dict()
        except Exception as exc:
            task_status = {"status": "error", "error": str(exc)}
        row_status = self._row_status(connection, capabilities, memory, task_status)
        return {
            "name": name,
            "status": row_status,
            "connection": redact_sensitive(connection, max_string=1000),
            "capabilities": redact_sensitive(capabilities, max_string=1000),
            "memory": redact_sensitive(memory, max_string=1000),
            "task_status": redact_sensitive(task_status, max_string=1000),
        }

    def _row_status(self, connection: dict[str, Any], capabilities: dict[str, Any], memory: dict[str, Any], task_status: dict[str, Any]) -> str:
        statuses = {str(connection.get("status")), str(capabilities.get("status")), str(memory.get("status")), str(task_status.get("status"))}
        if "error" in statuses or "unavailable" in statuses:
            return "error"
        if "adapter_unconfigured" in statuses or "not_configured" in statuses:
            return "not_configured"
        if bool(connection.get("connected")) and memory.get("status") == "success":
            return "ready"
        return "degraded"
