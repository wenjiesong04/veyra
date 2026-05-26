from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


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
    source: str = "capability_registry"
    updated_at: str = ""
    ttl_seconds: int = 300
    namespace: str = "native"

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "available": self.available,
            "kind": self.kind,
            "route": self.route,
            "executor": self.executor,
            "description": self.description,
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "updated_at": self.updated_at,
            "ttl_seconds": self.ttl_seconds,
            "namespace": self.namespace,
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

AGENT_CAPABILITY_NAMES = (
    "web_search",
    "web_fetch",
    "browser",
    "vision",
    "code_edit",
    "shell",
    "file",
    "memory",
    "mcp",
)

GENERIC_TO_AGENT_CAPABILITY: dict[str, str] = {
    "web_search": "web_search",
    "web_url_probe": "web_fetch",
    "vision": "vision",
    "selected_agent_runtime": "runtime",
}

AGENT_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "web_search": ("web_search", "search", "browser.search", "tools.web_search"),
    "web_fetch": ("web_fetch", "fetch", "browser.fetch", "tools.web_fetch"),
    "browser": ("browser", "browser_automation", "tools.browser"),
    "vision": ("vision", "image", "image_understanding", "multimodal"),
    "code_edit": ("code_edit", "edit", "patch", "workspace_edit"),
    "shell": ("shell", "terminal", "command", "bash"),
    "file": ("file", "filesystem", "fs", "workspace_file"),
    "memory": ("memory", "agent_memory"),
    "mcp": ("mcp", "mcp_tools"),
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
        now = utc_now_iso()

        capabilities = self._static_capabilities()
        agent_runtime_available = bool(agent_enabled and (agent_base_url or selected_agent in agents))
        capabilities["selected_agent_runtime"] = Capability(
            capability_id="selected_agent_runtime",
            available=agent_runtime_available,
            kind="agent_runtime",
            route="agent",
            executor=selected_agent,
            description="Governed handoff to the selected AgentAdapter runtime.",
            status="configured" if agent_base_url else "interface_available",
            reason="" if agent_base_url else "AgentAdapter exists, but the selected runtime endpoint may still be unconfigured.",
            source="agent_config",
            updated_at=now,
            namespace="agent",
        )
        capabilities["openclaw_runtime"] = Capability(
            capability_id="openclaw_runtime",
            available=self._agent_available(agents, "openclaw") or bool(agents.get("openclaw")),
            kind="agent_runtime",
            route="agent",
            executor="openclaw",
            description="OpenClaw runtime execution through the AgentAdapter contract.",
            status="configured" if self._agent_base_url(agents, "openclaw") else "not_configured",
            reason="" if self._agent_base_url(agents, "openclaw") else "OpenClaw base_url is not configured.",
            source="agent_config",
            updated_at=now,
            namespace="agent",
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
            updated_at=now,
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
            updated_at=now,
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
            updated_at=now,
        )
        agent_capabilities = self._agent_capabilities(selected_agent, selected_config)
        capabilities.update(agent_capabilities)
        missing = self._missing_summary(capabilities, selected_agent)

        return {
            "schema": "veyra.capabilities.v2",
            "source": "capability_registry",
            "updated_at": now,
            "ttl_seconds": 300,
            "capabilities": {name: capability.to_dict() for name, capability in sorted(capabilities.items())},
            "native_capabilities": {
                name: capability.to_dict()
                for name, capability in sorted(capabilities.items())
                if capability.namespace == "native"
            },
            "agent_capabilities": {
                name: capability.to_dict()
                for name, capability in sorted(capabilities.items())
                if capability.namespace == "agent" and "." in name
            },
            "missing_capabilities": missing,
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
                    "capability_source": self._agent_capability_source(selected_config),
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

    def agent_capability_for(self, capability_id: str) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        selected_agent = str(snapshot.get("selected_agent", {}).get("name") or "openclaw")
        agent_name = GENERIC_TO_AGENT_CAPABILITY.get(capability_id, capability_id)
        candidates = [f"{selected_agent}.{agent_name}"]
        if capability_id.startswith(f"{selected_agent}."):
            candidates.insert(0, capability_id)
        capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
        for candidate in candidates:
            payload = capabilities.get(candidate) if isinstance(capabilities.get(candidate), dict) else {}
            if payload:
                return {"capability": candidate, **payload}
        return None

    def agent_available_for(self, capability_id: str) -> bool:
        payload = self.agent_capability_for(capability_id)
        return bool(payload and payload.get("available"))

    def capability_for_probe(self, probe_name: str | None) -> str | None:
        if not probe_name:
            return None
        return PROBE_CAPABILITIES.get(probe_name)

    def capability_for_skill(self, skill_name: str | None) -> str | None:
        if not skill_name:
            return None
        return SKILL_CAPABILITIES.get(skill_name)

    def _static_capabilities(self) -> dict[str, Capability]:
        now = utc_now_iso()
        capabilities: dict[str, Capability] = {
            "native_answer": Capability(
                capability_id="native_answer",
                available=True,
                kind="core",
                route="direct_answer",
                executor="core_model_or_fallback",
                description="Direct response composition without tool execution.",
                updated_at=now,
            ),
            "human_review": Capability(
                capability_id="human_review",
                available=True,
                kind="governance",
                route="human_review",
                executor="review_queue",
                description="Ask the user to confirm risky or ambiguous action.",
                updated_at=now,
            ),
            "guardian": Capability(
                capability_id="guardian",
                available=True,
                kind="governance",
                route="block",
                executor="guardian",
                description="Policy block for forbidden requests.",
                updated_at=now,
            ),
            "rollback_audit": Capability(
                capability_id="rollback_audit",
                available=True,
                kind="governance",
                route="rollback",
                executor="rollback_manager",
                description="Snapshot-aware rollback request handling.",
                updated_at=now,
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
                updated_at=now,
            )
        for skill_name, capability_id in SKILL_CAPABILITIES.items():
            capabilities[capability_id] = Capability(
                capability_id=capability_id,
                available=True,
                kind="skill",
                route="skill",
                executor=skill_name,
                description=f"Built-in skill workflow: {skill_name}.",
                updated_at=now,
            )
        return capabilities

    def _agent_capabilities(self, selected_agent: str, selected_config: dict[str, Any]) -> dict[str, Capability]:
        output: dict[str, Capability] = {}
        advertised = self._advertised_agent_capabilities(selected_agent, selected_config)
        runtime_configured = bool(selected_config.get("base_url")) or bool(selected_config)
        snapshot_status = advertised.get("_snapshot_status", "unknown")
        snapshot_updated_at = str(advertised.get("_updated_at") or utc_now_iso())
        ttl_seconds = int(advertised.get("_ttl_seconds") or 300)
        for name in AGENT_CAPABILITY_NAMES:
            capability_id = f"{selected_agent}.{name}"
            available = bool(advertised.get(name))
            status = "available" if available else "unknown" if runtime_configured else "unavailable"
            reason = ""
            if not available:
                reason = (
                    f"{selected_agent} has not advertised {name}; refresh selected runtime capabilities."
                    if runtime_configured
                    else f"{selected_agent} runtime is not configured."
                )
            if snapshot_status in {"stale", "expired"} and available:
                status = snapshot_status
                reason = f"{selected_agent} previously advertised {name}, but the capability snapshot is {snapshot_status}."
            output[capability_id] = Capability(
                capability_id=capability_id,
                available=available,
                kind="agent_runtime_capability",
                route="agent",
                executor=selected_agent,
                description=f"Selected Agent Runtime capability: {name}.",
                status=status,
                reason=reason,
                source=str(advertised.get("_source") or "agent_config"),
                updated_at=snapshot_updated_at,
                ttl_seconds=ttl_seconds,
                namespace="agent",
            )
        return output

    def _advertised_agent_capabilities(self, selected_agent: str, selected_config: dict[str, Any]) -> dict[str, Any]:
        values = set(self._string_list(selected_config.get("capabilities")))
        values.update(self._string_list(selected_config.get("tools")))
        features = selected_config.get("features") if isinstance(selected_config.get("features"), dict) else {}
        values.update(name for name, enabled in features.items() if enabled)
        snapshot = self._executor_capability_snapshot(selected_agent)
        snapshot_capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
        snapshot_tools = snapshot.get("tools") if isinstance(snapshot.get("tools"), list) else []
        snapshot_features = snapshot.get("features") if isinstance(snapshot.get("features"), dict) else {}
        values.update(self._string_list(snapshot_tools))
        values.update(name for name, enabled in snapshot_features.items() if enabled)
        for item in snapshot_capabilities.values():
            if isinstance(item, dict):
                values.update(self._string_list(item.get("tools")))
                values.update(self._string_list(item.get("skills")))
        output: dict[str, Any] = {
            "_source": snapshot.get("source") or self._agent_capability_source(selected_config),
            "_updated_at": snapshot.get("updated_at") or selected_config.get("updated_at") or utc_now_iso(),
            "_ttl_seconds": snapshot.get("ttl_seconds") or selected_config.get("capability_ttl_seconds") or 300,
            "_snapshot_status": snapshot.get("freshness") or self._freshness(str(snapshot.get("updated_at") or selected_config.get("updated_at") or ""), int(snapshot.get("ttl_seconds") or selected_config.get("capability_ttl_seconds") or 300)),
        }
        normalized_values = {str(value).lower() for value in values}
        for name, aliases in AGENT_CAPABILITY_ALIASES.items():
            output[name] = any(alias.lower() in normalized_values for alias in aliases)
        return output

    def _executor_capability_snapshot(self, selected_agent: str) -> dict[str, Any]:
        executor = self.state_store.read_json("executor_state.json")
        snapshot = executor.get("capability_snapshot") if isinstance(executor.get("capability_snapshot"), dict) else {}
        if snapshot and str(snapshot.get("runtime") or snapshot.get("agent") or selected_agent) != selected_agent:
            return {}
        if not snapshot:
            return {}
        updated_at = str(snapshot.get("updated_at") or executor.get("updated_at") or "")
        ttl_seconds = int(snapshot.get("ttl_seconds") or 300)
        return {
            **snapshot,
            "updated_at": updated_at,
            "ttl_seconds": ttl_seconds,
            "freshness": self._freshness(updated_at, ttl_seconds),
            "source": snapshot.get("source") or "agent_capability_snapshot",
        }

    def _agent_capability_source(self, selected_config: dict[str, Any]) -> str:
        if selected_config.get("capabilities") or selected_config.get("tools") or selected_config.get("features"):
            return "agent_config"
        return "agent_capability_snapshot"

    def _missing_summary(self, capabilities: dict[str, Capability], selected_agent: str) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for native_id in ("vision", "web_search", "web_url_probe", "browser"):
            native = capabilities.get(native_id)
            agent_key = f"{selected_agent}.{GENERIC_TO_AGENT_CAPABILITY.get(native_id, native_id)}"
            agent = capabilities.get(agent_key)
            if (native and native.available) or (agent and agent.available):
                continue
            missing.append(
                {
                    "capability": native_id,
                    "native": native.to_dict() if native else None,
                    "agent": agent.to_dict() if agent else None,
                    "reason": "Neither Veyra native nor selected Agent Runtime currently advertises this capability.",
                }
            )
        return missing

    def _freshness(self, updated_at: str, ttl_seconds: int) -> str:
        if not updated_at:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return "unknown"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        if age > ttl_seconds * 2:
            return "expired"
        if age > ttl_seconds:
            return "stale"
        return "fresh"

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None]
        if isinstance(value, tuple):
            return [str(item) for item in value if item is not None]
        if isinstance(value, str) and value:
            return [value]
        return []

    def _agent_available(self, agents: dict[str, Any], name: str) -> bool:
        agent = agents.get(name) if isinstance(agents.get(name), dict) else {}
        return bool(agent.get("enabled", True) and self._agent_base_url(agents, name))

    def _agent_base_url(self, agents: dict[str, Any], name: str) -> str:
        agent = agents.get(name) if isinstance(agents.get(name), dict) else {}
        return str(agent.get("base_url") or "")
