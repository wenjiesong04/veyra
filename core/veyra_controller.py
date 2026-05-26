from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.capability_registry import CapabilityRegistry
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

    def prepare(self, decision: Decision) -> tuple[Decision, ControllerPlan]:
        normalized = self._add_route_capability(decision)
        required = list(dict.fromkeys(normalized.required_capabilities))
        missing = self.capabilities.missing(required)
        if normalized.route == Route.BLOCK:
            return normalized, ControllerPlan(Route.BLOCK, "ready", "guardian block route selected", missing)
        if missing:
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
        return normalized, ControllerPlan(normalized.route, "ready", "route is executable", [])

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
