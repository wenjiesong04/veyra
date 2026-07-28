from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.provider_certification import certify_agent_provider


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
    "time_probe": "time_probe",
    "system": "system_probe",
    "git": "git_probe",
    "port": "port_probe",
    "process": "process_probe",
    "file": "file_probe",
    "log": "log_probe",
    "network": "network_probe",
    "web": "web_url_probe",
    "search_probe": "web_search",
    "weather_probe": "weather_probe",
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
    "weather_probe": "web_search",
    "web_url_probe": "web_fetch",
    "vision": "vision",
    "selected_agent_runtime": "runtime",
}

AGENT_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "web_search": ("web_search", "search", "browser.search", "tools.web_search", "web"),
    "web_fetch": ("web_fetch", "fetch", "browser.fetch", "tools.web_fetch", "web"),
    "browser": ("browser", "browser_automation", "tools.browser"),
    "vision": ("vision", "image", "image_understanding", "multimodal", "media"),
    "code_edit": ("code_edit", "edit", "patch", "workspace_edit"),
    "shell": ("shell", "terminal", "command", "bash"),
    "file": ("file", "filesystem", "fs", "workspace_file"),
    "memory": ("memory", "agent_memory"),
    "mcp": ("mcp", "mcp_tools"),
}


class CapabilityRegistry:
    """Compact capability inventory exposed to Core cognition and Controller gates."""

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        now: datetime | None = None,
    ) -> None:
        self.state_store = state_store
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._fixed_now = now.astimezone(timezone.utc) if now is not None else None

    def snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("agent_config.json")
        selected_agent = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected_config = agents.get(selected_agent) if isinstance(agents.get(selected_agent), dict) else {}
        agent_enabled = bool(selected_config and selected_config.get("enabled", True))
        agent_base_url = str(selected_config.get("base_url") or "")
        executor_state = self.state_store.read_json("executor_state.json")
        selected_certification = self._provider_certification(
            selected_agent,
            executor_state,
            trusted_native_adapter=self._trusted_native_adapter(
                selected_agent,
                selected_config,
            ),
        )
        selected_provider_available = self._provider_dispatch_available(
            selected_agent,
            selected_config,
            selected_certification,
            executor_state,
        )
        now = utc_now_iso()

        capabilities = self._static_capabilities()
        capabilities.update(self._tool_proxy_capabilities(now))
        capabilities.update(self._mcp_capabilities(now))
        capabilities["selected_agent_runtime"] = Capability(
            capability_id="selected_agent_runtime",
            available=selected_provider_available,
            kind="agent_runtime",
            route="agent",
            executor=selected_agent,
            description="Governed handoff to the selected AgentAdapter runtime.",
            status=self._provider_status(
                selected_certification,
                enabled=agent_enabled,
                dispatch_available=selected_provider_available,
            ),
            reason=self._provider_reason(
                selected_agent,
                selected_certification,
                enabled=agent_enabled,
                dispatch_available=selected_provider_available,
            ),
            source="executor_state.capabilities",
            updated_at=str(
                selected_certification.get("observed_at") or ""
            ),
            ttl_seconds=int(selected_certification.get("ttl_seconds") or 300),
            namespace="agent",
        )
        openclaw_config = (
            agents.get("openclaw")
            if isinstance(agents.get("openclaw"), dict)
            else {}
        )
        openclaw_enabled = bool(
            openclaw_config and openclaw_config.get("enabled", True)
        )
        openclaw_certification = (
            selected_certification
            if selected_agent == "openclaw"
            else self._provider_certification(
                "openclaw",
                executor_state,
                trusted_native_adapter=self._trusted_native_adapter(
                    "openclaw",
                    openclaw_config,
                ),
            )
        )
        openclaw_available = self._provider_dispatch_available(
            "openclaw",
            openclaw_config,
            openclaw_certification,
            executor_state,
        )
        capabilities["openclaw_runtime"] = Capability(
            capability_id="openclaw_runtime",
            available=openclaw_available,
            kind="agent_runtime",
            route="agent",
            executor="openclaw",
            description="OpenClaw runtime execution through the AgentAdapter contract.",
            status=self._provider_status(
                openclaw_certification,
                enabled=openclaw_enabled,
                dispatch_available=openclaw_available,
            ),
            reason=self._provider_reason(
                "openclaw",
                openclaw_certification,
                enabled=openclaw_enabled,
                dispatch_available=openclaw_available,
            ),
            source="executor_state.capabilities",
            updated_at=str(
                openclaw_certification.get("observed_at") or ""
            ),
            ttl_seconds=int(openclaw_certification.get("ttl_seconds") or 300),
            namespace="agent",
        )
        capabilities["web_search"] = Capability(
            capability_id="web_search",
            available=True,
            kind="probe",
            route="probe",
            executor="search_probe",
            description="General web search via SearchProbe (DuckDuckGo HTML / optional OpenClaw CLI).",
            status="available",
            reason="",
            updated_at=now,
        )
        capabilities["weather_probe"] = Capability(
            capability_id="weather_probe",
            available=True,
            kind="probe",
            route="probe",
            executor="weather_probe",
            description="Current weather lookup via Open-Meteo.",
            status="available",
            reason="",
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
        agent_capabilities = self._agent_capabilities(
            selected_agent,
            selected_config,
            selected_certification,
            executor_state,
            provider_dispatch_available=selected_provider_available,
        )
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
            "tool_proxy_capabilities": {
                name: capability.to_dict()
                for name, capability in sorted(capabilities.items())
                if capability.namespace == "tool_proxy"
            },
            "mcp_capabilities": {
                name: capability.to_dict()
                for name, capability in sorted(capabilities.items())
                if capability.namespace == "mcp"
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
                    "capability_source": (
                        "executor_state.capabilities"
                        if isinstance(executor_state.get("capabilities"), dict)
                        and executor_state.get("capabilities")
                        else self._agent_capability_source(selected_config)
                    ),
                    "provider_certified": (
                        selected_certification.get("validated") is True
                    ),
                    "task_dispatch_available": (
                        selected_provider_available
                    ),
                    "certification_status": selected_certification.get(
                        "certification_status"
                    ),
                }
            ),
            "provider_certification": selected_certification,
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
            "verifier": Capability(
                capability_id="verifier",
                available=True,
                kind="governance",
                route="direct_answer",
                executor="verifier",
                description="Verify Agent, tool, and probe results before user synthesis.",
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

    def _tool_proxy_capabilities(self, now: str) -> dict[str, Capability]:
        ops_config = self.state_store.read_json("ops_config.json")
        tool_proxy = ops_config.get("tool_proxy") if isinstance(ops_config.get("tool_proxy"), dict) else {}
        browser_enabled = bool(tool_proxy.get("browser_executor_enabled", False))
        api_enabled = bool(tool_proxy.get("api_executor_enabled", False))
        browser_hosts = tool_proxy.get("browser_allowed_hosts") if isinstance(tool_proxy.get("browser_allowed_hosts"), list) else []
        api_hosts = tool_proxy.get("api_allowed_hosts") if isinstance(tool_proxy.get("api_allowed_hosts"), list) else []
        return {
            "safe_shell": Capability(
                capability_id="safe_shell",
                available=True,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_shell",
                description="Governed shell command execution through ToolProxy policy and trace.",
                status="available",
                updated_at=now,
                namespace="tool_proxy",
            ),
            "safe_file_read": Capability(
                capability_id="safe_file_read",
                available=True,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_file",
                description="Governed local file reads through ToolProxy policy and trace.",
                status="available",
                updated_at=now,
                namespace="tool_proxy",
            ),
            "safe_file_write": Capability(
                capability_id="safe_file_write",
                available=True,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_file",
                description="Governed local file writes with review/snapshot policy.",
                status="available",
                updated_at=now,
                namespace="tool_proxy",
            ),
            "safe_browser_open": Capability(
                capability_id="safe_browser_open",
                available=browser_enabled,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_browser",
                description="Guarded browser open action with allowlisted executor hosts.",
                status="configured" if browser_enabled else "not_configured",
                reason="" if browser_enabled else "Browser execution is disabled until /tool-proxy/config enables it.",
                updated_at=now,
                namespace="tool_proxy",
            ),
            "browser": Capability(
                capability_id="browser",
                available=browser_enabled,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_browser",
                description="Generic browser capability backed by SafeBrowser.",
                status="configured" if browser_enabled else "not_configured",
                reason=", ".join(str(item) for item in browser_hosts[:6]) if browser_enabled else "SafeBrowser executor is not configured.",
                updated_at=now,
                namespace="tool_proxy",
            ),
            "safe_api_request": Capability(
                capability_id="safe_api_request",
                available=api_enabled,
                kind="tool_proxy",
                route="native_tool",
                executor="safe_api",
                description="Guarded outbound API request with method and host policy.",
                status="configured" if api_enabled else "not_configured",
                reason=", ".join(str(item) for item in api_hosts[:6]) if api_enabled else "SafeAPI executor is disabled until /tool-proxy/config enables it.",
                updated_at=now,
                namespace="tool_proxy",
            ),
        }

    def _mcp_capabilities(self, now: str) -> dict[str, Capability]:
        config_files = [
            Path.cwd() / ".mcp.json",
            Path.home() / ".config" / "mcp" / "config.json",
            Path.home() / ".cursor" / "mcp.json",
        ]
        existing = [str(path) for path in config_files if path.exists()]
        env_servers = [key for key in os.environ if key.startswith("MCP_")]
        cli = shutil.which("mcp") or shutil.which("npx")
        configured = bool(existing or env_servers)
        runnable = bool(configured and cli)
        return {
            "mcp_runtime": Capability(
                capability_id="mcp_runtime",
                available=runnable,
                kind="external_tool_runtime",
                route="probe",
                executor="mcp_probe",
                description="MCP runtime/configuration visibility through Veyra probes.",
                status="available" if runnable else "config_detected" if configured else "not_detected",
                reason="" if runnable else "MCP config exists but no CLI was detected." if configured else "No MCP config files or MCP_* env keys detected.",
                updated_at=now,
                namespace="mcp",
            ),
            "mcp_config": Capability(
                capability_id="mcp_config",
                available=configured,
                kind="external_tool_config",
                route="probe",
                executor="mcp_probe",
                description="Detected MCP config files or MCP_* environment keys.",
                status="available" if configured else "not_detected",
                reason=", ".join(existing[:3]) if existing else "No MCP config detected.",
                updated_at=now,
                namespace="mcp",
            ),
        }

    def _agent_capabilities(
        self,
        selected_agent: str,
        selected_config: dict[str, Any],
        provider_certification: dict[str, Any],
        executor_state: dict[str, Any],
        *,
        provider_dispatch_available: bool,
    ) -> dict[str, Capability]:
        output: dict[str, Capability] = {}
        advertised = self._advertised_agent_capabilities(
            selected_agent,
            selected_config,
            executor_state,
        )
        runtime_configured = bool(selected_config.get("base_url")) or bool(selected_config)
        provider_available = provider_dispatch_available
        certification_status = str(
            provider_certification.get("certification_status") or "unverified"
        )
        certification_reason = self._provider_reason(
            selected_agent,
            provider_certification,
            enabled=bool(
                selected_config and selected_config.get("enabled", True)
            ),
            dispatch_available=provider_dispatch_available,
        )
        snapshot_status = advertised.get("_snapshot_status", "unknown")
        snapshot_updated_at = str(advertised.get("_updated_at") or "")
        ttl_seconds = int(advertised.get("_ttl_seconds") or 300)
        for name in AGENT_CAPABILITY_NAMES:
            capability_id = f"{selected_agent}.{name}"
            advertised_available = bool(advertised.get(name))
            available = bool(provider_available and advertised_available)
            status = (
                "available"
                if available
                else certification_status
                if not provider_available
                else "unknown"
                if runtime_configured
                else "unavailable"
            )
            reason = ""
            if not provider_available:
                reason = certification_reason
            elif not advertised_available:
                reason = (
                    f"{selected_agent} has not advertised {name}; refresh selected runtime capabilities."
                    if runtime_configured
                    else f"{selected_agent} runtime is not configured."
                )
            if snapshot_status in {"stale", "expired"} and advertised_available:
                available = False
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

    def _advertised_agent_capabilities(
        self,
        selected_agent: str,
        selected_config: dict[str, Any],
        executor_state: dict[str, Any],
    ) -> dict[str, Any]:
        observed_values: set[str] = set()
        snapshot = self._executor_capability_snapshot(
            selected_agent,
            executor_state,
        )
        snapshot_capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
        snapshot_tools = snapshot.get("tools") if isinstance(snapshot.get("tools"), list) else []
        snapshot_features = snapshot.get("features") if isinstance(snapshot.get("features"), dict) else {}
        observed_values.update(self._collect_capability_tokens(snapshot_tools))
        observed_values.update(
            self._collect_capability_tokens(snapshot.get("raw"))
        )
        observed_values.update(
            name for name, enabled in snapshot_features.items() if enabled
        )
        for item in snapshot_capabilities.values():
            if isinstance(item, dict):
                observed_values.update(
                    self._collect_capability_tokens(item.get("tools"))
                )
                observed_values.update(
                    self._collect_capability_tokens(item.get("skills"))
                )
                observed_values.update(
                    self._collect_capability_tokens(item.get("raw"))
                )
        output: dict[str, Any] = {
            "_source": snapshot.get("source") or self._agent_capability_source(selected_config),
            "_updated_at": (
                snapshot.get("updated_at")
                or selected_config.get("updated_at")
                or ""
            ),
            "_ttl_seconds": snapshot.get("ttl_seconds") or selected_config.get("capability_ttl_seconds") or 300,
            "_snapshot_status": snapshot.get("freshness") or self._freshness(str(snapshot.get("updated_at") or selected_config.get("updated_at") or ""), int(snapshot.get("ttl_seconds") or selected_config.get("capability_ttl_seconds") or 300)),
        }
        normalized_values = {
            str(value).lower() for value in observed_values
        }
        for name, aliases in AGENT_CAPABILITY_ALIASES.items():
            output[name] = any(alias.lower() in normalized_values for alias in aliases)
        return output

    def _executor_capability_snapshot(
        self,
        selected_agent: str,
        executor: dict[str, Any],
    ) -> dict[str, Any]:
        capabilities = (
            executor.get("capabilities")
            if isinstance(executor.get("capabilities"), dict)
            else {}
        )
        if capabilities:
            snapshot = {
                **capabilities,
                "runtime": capabilities.get("runtime") or selected_agent,
                "updated_at": capabilities.get("updated_at"),
                "ttl_seconds": capabilities.get("ttl_seconds")
                or executor.get("ttl_seconds")
                or 300,
                "source": "executor_state.capabilities",
            }
        else:
            snapshot = (
                executor.get("capability_snapshot")
                if isinstance(executor.get("capability_snapshot"), dict)
                else {}
            )
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
        age = (self._now() - parsed).total_seconds()
        if age > ttl_seconds * 2:
            return "expired"
        if age > ttl_seconds:
            return "stale"
        return "fresh"

    def _provider_certification(
        self,
        selected_agent: str,
        executor_state: dict[str, Any],
        *,
        trusted_native_adapter: bool = False,
    ) -> dict[str, Any]:
        capabilities = (
            executor_state.get("capabilities")
            if isinstance(executor_state.get("capabilities"), dict)
            else {}
        )
        ttl_seconds = self._provider_ttl(
            executor_state.get("ttl_seconds")
            or capabilities.get("ttl_seconds")
            or 300
        )
        try:
            certification = certify_agent_provider(
                runtime=selected_agent,
                capabilities=capabilities,
                observed_at=capabilities.get("updated_at"),
                now=self._now(),
                ttl_seconds=ttl_seconds,
                trusted_native_adapter=trusted_native_adapter,
            )
        except (TypeError, ValueError):
            certification = {
                "schema_version": "veyra.provider_certification.v1",
                "runtime": selected_agent[:120],
                "certification_status": "unverified",
                "validated": False,
                "observed_at": None,
                "ttl_seconds": ttl_seconds,
                "expires_at": None,
                "freshness": {
                    "status": "unknown",
                    "age_seconds": None,
                },
                "evidence": {},
                "issues": ["provider_certification_input_invalid"],
                "read_only_dispatch_allowed": False,
                "automatic_selection_allowed": False,
                "provider_switch_allowed": False,
                "side_effect_dispatch_allowed": False,
                "policy_effect": "none",
            }
        executor_runtime = str(
            executor_state.get("selected_agent") or ""
        )
        executor_status = str(executor_state.get("status") or "unknown")
        executor_ready = bool(
            executor_runtime == selected_agent
            and executor_state.get("connected") is True
            and executor_status in {"available", "ok", "success"}
        )
        if executor_ready:
            return certification
        issues = certification.get("issues")
        selected_issues = (
            [str(item) for item in issues]
            if isinstance(issues, list)
            else []
        )
        selected_issues.append(
            "executor_runtime_identity_mismatch"
            if executor_runtime != selected_agent
            else "executor_not_currently_available"
        )
        return {
            **certification,
            "certification_status": (
                "incompatible"
                if executor_runtime
                and executor_runtime != selected_agent
                else "unverified"
            ),
            "validated": False,
            "issues": sorted(set(selected_issues)),
            "read_only_dispatch_allowed": False,
            "automatic_selection_allowed": False,
            "provider_switch_allowed": False,
            "side_effect_dispatch_allowed": False,
            "policy_effect": "none",
        }

    @staticmethod
    def _trusted_native_adapter(
        runtime: str,
        config: dict[str, Any],
    ) -> bool:
        if runtime != "openclaw" or not isinstance(config, dict):
            return False
        return (
            bool(config)
            and config.get("enabled", True) is not False
            and str(config.get("kind") or runtime).lower() == "openclaw"
        )

    def _provider_status(
        self,
        certification: dict[str, Any],
        *,
        enabled: bool,
        dispatch_available: bool,
    ) -> str:
        if not enabled:
            return "disabled"
        if dispatch_available:
            return "available"
        if certification.get("validated") is True:
            return "diagnostic_only"
        return str(
            certification.get("certification_status") or "unverified"
        )

    def _provider_reason(
        self,
        runtime: str,
        certification: dict[str, Any],
        *,
        enabled: bool,
        dispatch_available: bool = False,
    ) -> str:
        if not enabled:
            return f"{runtime} runtime is disabled or absent from agent config."
        if dispatch_available:
            return ""
        if certification.get("validated") is True:
            return (
                f"{runtime} is protocol-compatible for diagnostics, but "
                "has no fresh locally enforced governed task-dispatch path."
            )
        issues = certification.get("issues")
        details = (
            ", ".join(str(item) for item in issues[:8])
            if isinstance(issues, list)
            else "provider_certification_missing"
        )
        return (
            f"{runtime} has no fresh exact-compatible v2 provider "
            f"certification: {details}."
        )

    def _provider_dispatch_available(
        self,
        runtime: str,
        config: dict[str, Any],
        certification: dict[str, Any],
        executor_state: dict[str, Any],
    ) -> bool:
        if (
            certification.get("validated") is not True
            or not self._trusted_native_adapter(runtime, config)
        ):
            return False
        capabilities = (
            executor_state.get("capabilities")
            if isinstance(executor_state.get("capabilities"), dict)
            else {}
        )
        features = (
            capabilities.get("features")
            if isinstance(capabilities.get("features"), dict)
            else {}
        )
        return bool(
            features.get("tool_proxy_enforced") is True
            and features.get("tool_proxy_identity_match") is True
            and features.get("tool_proxy_enforcement_scope")
            == "veyra_governed_openclaw_sessions"
            and features.get("governance_callbacks_complete") is True
        )

    def _now(self) -> datetime:
        return self._fixed_now or datetime.now(timezone.utc)

    def _provider_ttl(self, value: Any) -> int:
        if isinstance(value, bool):
            return 300
        try:
            selected = int(value)
        except (TypeError, ValueError):
            return 300
        return selected if 1 <= selected <= 86_400 else 300

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None]
        if isinstance(value, tuple):
            return [str(item) for item in value if item is not None]
        if isinstance(value, str) and value:
            return [value]
        return []

    def _collect_capability_tokens(self, value: Any) -> set[str]:
        tokens: set[str] = set()
        if isinstance(value, str):
            token = value.strip().lower()
            if token:
                tokens.add(token)
            return tokens
        if isinstance(value, (list, tuple)):
            for item in value:
                tokens.update(self._collect_capability_tokens(item))
            return tokens
        if isinstance(value, dict):
            for key in ("id", "name", "kind", "type", "runtime", "group", "namespace"):
                token = str(value.get(key) or "").strip().lower()
                if token:
                    tokens.add(token)
            groups = value.get("groups")
            if isinstance(groups, list):
                for item in groups:
                    token = str(item or "").strip().lower()
                    if token:
                        tokens.add(token)
            features = value.get("features")
            if isinstance(features, dict):
                for feature, enabled in features.items():
                    if enabled:
                        token = str(feature or "").strip().lower()
                        if token:
                            tokens.add(token)
            capabilities = value.get("capabilities")
            if isinstance(capabilities, dict):
                for capability, payload in capabilities.items():
                    if isinstance(payload, dict):
                        if payload.get("available"):
                            token = str(capability or "").strip().lower()
                            if token:
                                tokens.add(token)
                        tokens.update(self._collect_capability_tokens(payload))
                    elif payload:
                        token = str(capability or "").strip().lower()
                        if token:
                            tokens.add(token)
            for nested_key in ("tools", "skills", "items", "raw", "optional_methods", "required_methods"):
                if nested_key in value:
                    tokens.update(self._collect_capability_tokens(value.get(nested_key)))
        return tokens
