from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.capability_registry import CapabilityRegistry
from core.delegation_policy import DelegationPolicy
from interface.event_schema import Decision, Route


@dataclass(frozen=True, slots=True)
class ControllerPlan:
    route: Route
    status: str
    reason: str
    missing_capabilities: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.value,
            "status": self.status,
            "reason": self.reason,
            "missing_capabilities": self.missing_capabilities,
        }


class VeyraController:
    """Applies capability and governance routing before any tool or agent executes."""

    def __init__(self, capability_registry: CapabilityRegistry) -> None:
        self.capabilities = capability_registry
        self.delegation_policy = DelegationPolicy(capability_registry)

    def prepare(self, decision: Decision) -> tuple[Decision, ControllerPlan]:
        delegated, delegation_trace = self.delegation_policy.apply(decision)
        normalized = self._add_route_capability(delegated)
        required = list(dict.fromkeys(normalized.required_capabilities))
        missing = self.capabilities.missing(required)
        if normalized.route == Route.BLOCK:
            return normalized, ControllerPlan(Route.BLOCK, "ready", delegation_trace.get("reason", "guardian block route selected"), missing)
        unsupported_probe = self._unsupported_probe(normalized)
        if unsupported_probe:
            fallback_required = self._probe_fallback_capabilities(normalized, unsupported_probe)
            agent_capabilities = self._available_agent_capabilities_for_missing(
                replace(normalized, required_capabilities=fallback_required),
                [{"capability": capability} for capability in fallback_required],
            )
            if agent_capabilities:
                adjusted = replace(
                    normalized,
                    route=Route.AGENT,
                    reason=f"selected Agent Runtime can satisfy unsupported probe `{unsupported_probe}` via {', '.join(agent_capabilities)}",
                    capability="selected_agent_runtime",
                    needs_agent=True,
                    needs_probe=False,
                    needs_user_confirmation=False,
                    required_capabilities=list(dict.fromkeys(["selected_agent_runtime", *agent_capabilities])),
                    signals=list(dict.fromkeys(normalized.signals + ["controller:unsupported_probe_agent_fallback"])),
                    constraints=list(
                        dict.fromkeys(
                            normalized.constraints
                            + [
                                "selected Agent Runtime must return evidence for external facts",
                                "do not fabricate unavailable probe results",
                            ]
                        )
                    ),
                )
                return adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, [{"capability": unsupported_probe, "reason": "unsupported_native_probe"}])
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                reason=f"unsupported native probe: {unsupported_probe}",
                capability="ask_user",
                needs_probe=False,
                signals=list(dict.fromkeys(normalized.signals + ["controller:unsupported_probe"])),
                constraints=list(dict.fromkeys(normalized.constraints + ["do not execute unsupported probe"])),
            )
            return adjusted, ControllerPlan(Route.ASK_USER, "missing_probe", adjusted.reason, [{"capability": unsupported_probe, "reason": "unsupported_native_probe"}])
        if missing:
            agent_capabilities = self._available_agent_capabilities_for_missing(normalized, missing)
            if agent_capabilities:
                adjusted = replace(
                    normalized,
                    route=Route.AGENT,
                    reason=f"selected Agent Runtime can satisfy missing capability: {', '.join(agent_capabilities)}",
                    capability="selected_agent_runtime",
                    needs_agent=True,
                    needs_probe=False,
                    needs_user_confirmation=False,
                    required_capabilities=list(dict.fromkeys(["selected_agent_runtime", *agent_capabilities])),
                    signals=list(dict.fromkeys(normalized.signals + ["controller:agent_capability_fallback"])),
                    constraints=list(
                        dict.fromkeys(
                            normalized.constraints
                            + [
                                "selected Agent Runtime must return evidence for external facts",
                                "do not fabricate unavailable capability results",
                            ]
                        )
                    ),
                )
                return adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, missing)
            if self._should_agent_fallback(normalized):
                adjusted = replace(
                    normalized,
                    route=Route.AGENT,
                    reason=f"agent-first capability fallback: {', '.join(item.get('capability', '') for item in missing)}",
                    capability="selected_agent_runtime",
                    needs_agent=True,
                    needs_user_confirmation=False,
                    required_capabilities=["selected_agent_runtime"],
                    signals=list(dict.fromkeys(normalized.signals + ["controller:agent_first_fallback"])),
                    constraints=list(
                        dict.fromkeys(
                            normalized.constraints
                            + [
                                "selected Agent Runtime should continue execution when native capabilities are missing",
                                "must disclose unsupported capability explicitly if execution still cannot proceed",
                            ]
                        )
                    ),
                )
                return adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, missing)
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                reason=f"missing required capability: {', '.join(item.get('capability', '') for item in missing)}",
                capability="ask_user",
                needs_user_confirmation=False,
                signals=list(dict.fromkeys(normalized.signals + ["controller:missing_capability"])),
                constraints=list(dict.fromkeys(normalized.constraints + ["do not fabricate unavailable capability results"])),
            )
            return adjusted, ControllerPlan(Route.ASK_USER, "missing_capability", adjusted.reason, missing)
        if normalized.needs_probe and normalized.route != Route.PROBE:
            probe_capability = self.capabilities.capability_for_probe(normalized.selected_probe)
            if normalized.selected_probe and (not probe_capability or self.capabilities.is_available(probe_capability)):
                adjusted = replace(
                    normalized,
                    route=Route.PROBE,
                    capability="probe",
                    signals=list(dict.fromkeys(normalized.signals + ["controller:probe_required"])),
                )
                return adjusted, ControllerPlan(Route.PROBE, "rerouted", "fresh evidence requires a probe", [])
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                capability="ask_user",
                signals=list(dict.fromkeys(normalized.signals + ["controller:probe_missing"])),
            )
            return adjusted, ControllerPlan(Route.ASK_USER, "missing_probe", "fresh evidence is required but no executable probe was selected", [])
        if normalized.needs_agent and normalized.route != Route.AGENT:
            adjusted = replace(
                normalized,
                route=Route.AGENT,
                capability="selected_agent_runtime",
                signals=list(dict.fromkeys(normalized.signals + ["controller:agent_required"])),
            )
            return adjusted, ControllerPlan(Route.AGENT, "rerouted", "execution requires agent runtime", [])
        return normalized, ControllerPlan(normalized.route, "ready", str(delegation_trace.get("reason") or "route is executable"), [])

    def _available_agent_capabilities_for_missing(self, decision: Decision, missing: list[dict[str, Any]]) -> list[str]:
        if decision.risk_level.value not in {"R0", "R1"}:
            return []
        agent_capabilities: list[str] = []
        for item in missing:
            capability = str(item.get("capability") or item.get("capability_id") or "")
            if not capability:
                continue
            match = self.capabilities.agent_capability_for(capability)
            if isinstance(match, dict) and match.get("available"):
                token = str(match.get("capability") or "")
                if token:
                    agent_capabilities.append(token)
        return list(dict.fromkeys(agent_capabilities))

    def _should_agent_fallback(self, decision: Decision) -> bool:
        if decision.risk_level.value not in {"R0", "R1"}:
            return False
        if decision.route not in {Route.DIRECT_ANSWER, Route.PROBE, Route.ASK_USER}:
            return False
        if decision.intent in {"identity", "preference"}:
            return False
        if not self.capabilities.is_available("selected_agent_runtime"):
            return False
        config = self.capabilities.state_store.read_json("agent_config.json")
        routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
        mode = str(routing.get("dialogue_route") or "").strip().lower()
        if mode in {"agent_first", "all_to_agent", "agent"}:
            return True
        if mode in {"hybrid", "native_first", "native"}:
            return False
        return bool(routing.get("agent_first_dialogue", False))

    def _add_route_capability(self, decision: Decision) -> Decision:
        required = list(decision.required_capabilities)
        if decision.route == Route.PROBE:
            capability = self.capabilities.capability_for_probe(decision.selected_probe)
            if capability:
                required.append(capability)
        elif decision.route == Route.SKILL:
            capability = self.capabilities.capability_for_skill(decision.selected_probe)
            if capability:
                required.append(capability)
        elif decision.route == Route.AGENT:
            required.append("selected_agent_runtime")
        elif decision.route == Route.DIRECT_ANSWER:
            required.append("native_answer")
        elif decision.route == Route.HUMAN_REVIEW:
            required.append("human_review")
        elif decision.route == Route.ROLLBACK:
            required.append("rollback_audit")
        return replace(decision, required_capabilities=list(dict.fromkeys(item for item in required if item)))

    def _unsupported_probe(self, decision: Decision) -> str:
        if decision.route != Route.PROBE or not decision.selected_probe:
            return ""
        return "" if self.capabilities.capability_for_probe(decision.selected_probe) else str(decision.selected_probe)

    def _probe_fallback_capabilities(self, decision: Decision, unsupported_probe: str) -> list[str]:
        required = list(decision.required_capabilities)
        lowered = unsupported_probe.lower()
        if "search" in lowered and "web_search" not in required:
            required.append("web_search")
        if "web" in lowered and "web_url_probe" not in required:
            required.append("web_url_probe")
        return list(dict.fromkeys(required))
