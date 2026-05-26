from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    available: bool
    kind: str
    route: str
    executor: str
    description: str
    status: str = "available"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "kind": self.kind,
            "route": self.route,
            "executor": self.executor,
            "description": self.description,
            "status": self.status,
            "reason": self.reason,
        }


PROBE_CAPABILITIES: dict[str, str] = {
    "time": "time_probe",
    "system": "system_probe",
    "git": "git_probe",
    "port": "port_probe",
    "process": "process_probe",
    "file": "file_probe",
    "log": "log_probe",
    "network": "network_probe",
    "web": "web_url_probe",
    "openclaw": "openclaw_probe",
    "hermes": "hermes_probe",
    "mcp": "mcp_probe",
}

SKILL_CAPABILITIES: dict[str, str] = {
    "diagnose_openclaw": "diagnose_openclaw_skill",
    "check_port": "check_port_skill",
    "summarize_logs": "summarize_logs_skill",
    "safe_git_commit": "safe_git_commit_skill",
}


class CapabilityRegistry:
    """Compact capability inventory exposed to Core cognition and Controller gates."""

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("agent_config.json")
        selected_agent = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected_config = agents.get(selected_agent) if isinstance(agents.get(selected_agent), dict) else {}
        agent_enabled = bool(selected_config.get("enabled", True))
        agent_base_url = str(selected_config.get("base_url") or "")

        capabilities = self._static_capabilities()
        capabilities["selected_agent_runtime"] = Capability(
            capability_id="selected_agent_runtime",
            available=agent_enabled,
            kind="agent_runtime",
            route="agent",
            executor=selected_agent,
            description="Governed handoff to the selected AgentAdapter runtime.",
            status="configured" if agent_base_url else "interface_available",
            reason="" if agent_base_url else "AgentAdapter exists, but the selected runtime endpoint may still be unconfigured.",
        )
        capabilities["openclaw_runtime"] = Capability(
            capability_id="openclaw_runtime",
            available=self._agent_available(agents, "openclaw"),
            kind="agent_runtime",
            route="agent",
            executor="openclaw",
            description="OpenClaw runtime execution through the AgentAdapter contract.",
            status="configured" if self._agent_base_url(agents, "openclaw") else "not_configured",
            reason="" if self._agent_base_url(agents, "openclaw") else "OpenClaw base_url is not configured.",
        )
        capabilities["web_search"] = Capability(
            capability_id="web_search",
            available=False,
            kind="probe",
            route="probe",
            executor="none",
            description="General web search over arbitrary current facts.",
            status="not_implemented",
            reason="Only direct URL fetch is implemented as web_url_probe.",
        )
        capabilities["weather_probe"] = Capability(
            capability_id="weather_probe",
            available=False,
            kind="probe",
            route="probe",
            executor="none",
            description="Current weather lookup.",
            status="not_implemented",
            reason="No weather provider is configured in Veyra Core.",
        )
        capabilities["vision"] = Capability(
            capability_id="vision",
            available=False,
            kind="perception",
            route="ask_user",
            executor="none",
            description="Image or attachment visual understanding.",
            status="not_implemented",
            reason="The channel must supply OCR, caption, or a vision adapter before Core can inspect image bytes.",
        )

        return {
            "schema": "veyra.capabilities.v1",
            "capabilities": {name: capability.to_dict() for name, capability in sorted(capabilities.items())},
            "routes": {
                "direct_answer": True,
                "probe": True,
                "skill": True,
                "agent": capabilities["selected_agent_runtime"].available,
                "ask_user": True,
                "human_review": True,
                "block": True,
                "rollback": True,
            },
            "probes": {name: {"capability": cap, "available": capabilities[cap].available} for name, cap in PROBE_CAPABILITIES.items()},
            "skills": {name: {"capability": cap, "available": capabilities[cap].available} for name, cap in SKILL_CAPABILITIES.items()},
            "selected_agent": redact_sensitive(
                {
                    "name": selected_agent,
                    "enabled": agent_enabled,
                    "base_url_configured": bool(agent_base_url),
                    "kind": selected_config.get("kind"),
                }
            ),
        }

    def is_available(self, capability_id: str) -> bool:
        capabilities = self.snapshot().get("capabilities", {})
        value = capabilities.get(capability_id) if isinstance(capabilities, dict) else {}
        return bool(isinstance(value, dict) and value.get("available"))

    def missing(self, capability_ids: list[str]) -> list[dict[str, Any]]:
        snapshot = self.snapshot()
        capabilities = snapshot.get("capabilities", {}) if isinstance(snapshot.get("capabilities"), dict) else {}
        missing: list[dict[str, Any]] = []
        for capability_id in capability_ids:
            current = capabilities.get(capability_id) if isinstance(capabilities.get(capability_id), dict) else {}
            if current.get("available"):
                continue
            missing.append({"capability": capability_id, **current})
        return missing

    def capability_for_probe(self, probe_name: str | None) -> str | None:
        if not probe_name:
            return None
        return PROBE_CAPABILITIES.get(probe_name)

    def capability_for_skill(self, skill_name: str | None) -> str | None:
        if not skill_name:
            return None
        return SKILL_CAPABILITIES.get(skill_name)

    def _static_capabilities(self) -> dict[str, Capability]:
        capabilities: dict[str, Capability] = {
            "native_answer": Capability(
                capability_id="native_answer",
                available=True,
                kind="core",
                route="direct_answer",
                executor="core_model_or_fallback",
                description="Direct response composition without tool execution.",
            ),
            "human_review": Capability(
                capability_id="human_review",
                available=True,
                kind="governance",
                route="human_review",
                executor="review_queue",
                description="Ask the user to confirm risky or ambiguous action.",
            ),
            "guardian": Capability(
                capability_id="guardian",
                available=True,
                kind="governance",
                route="block",
                executor="guardian",
                description="Policy block for forbidden requests.",
            ),
            "rollback_audit": Capability(
                capability_id="rollback_audit",
                available=True,
                kind="governance",
                route="rollback",
                executor="rollback_manager",
                description="Snapshot-aware rollback request handling.",
            ),
        }
        for probe_name, capability_id in PROBE_CAPABILITIES.items():
            capabilities[capability_id] = Capability(
                capability_id=capability_id,
                available=True,
                kind="probe",
                route="probe",
                executor=probe_name,
                description=f"Read-only {probe_name} probe.",
            )
        for skill_name, capability_id in SKILL_CAPABILITIES.items():
            capabilities[capability_id] = Capability(
                capability_id=capability_id,
                available=True,
                kind="skill",
                route="skill",
                executor=skill_name,
                description=f"Built-in skill workflow: {skill_name}.",
            )
        return capabilities

    def _agent_available(self, agents: dict[str, Any], name: str) -> bool:
        agent = agents.get(name) if isinstance(agents.get(name), dict) else {}
        return bool(agent.get("enabled", True) and self._agent_base_url(agents, name))

    def _agent_base_url(self, agents: dict[str, Any], name: str) -> str:
        agent = agents.get(name) if isinstance(agents.get(name), dict) else {}
        return str(agent.get("base_url") or "")
