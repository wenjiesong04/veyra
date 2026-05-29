from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.capability_registry import CapabilityRegistry
from core.definitions import RiskLevel
from interface.event_schema import Decision, Route


class DelegationPolicy:
    """Route by intent, capability, freshness, risk, and complexity.

    This policy keeps Veyra as the governance layer: native read-only work stays
    in Veyra, complex or external capability work is handed to the selected
    Agent Runtime only after capability and risk checks.
    """

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self.capabilities = capabilities

    def apply(self, decision: Decision) -> tuple[Decision, dict[str, Any]]:
        if decision.risk_level == RiskLevel.R5:
            return self._force_route(decision, Route.BLOCK, "R5 is blocked before delegation")
        if decision.risk_level in {RiskLevel.R3, RiskLevel.R4}:
            return self._force_route(decision, Route.HUMAN_REVIEW, "R3/R4 requires human review before delegation")
        if self._should_force_agent_dialogue(decision):
            return self._force_agent_dialogue(decision, "agent_first_dialogue_policy")
        if decision.route in {Route.DIRECT_ANSWER, Route.PROBE, Route.SKILL, Route.HUMAN_REVIEW, Route.BLOCK, Route.ROLLBACK}:
            return decision, self._trace(decision, "native_or_governance_route_preserved")
        if decision.route == Route.AGENT:
            return self._agent_ready(decision, "complex_or_write_task_requires_selected_agent")
        if decision.route == Route.ASK_USER:
            return self._resolve_capability_gap(decision)
        return decision, self._trace(decision, "route_preserved")

    def _resolve_capability_gap(self, decision: Decision) -> tuple[Decision, dict[str, Any]]:
        required = list(dict.fromkeys(decision.required_capabilities))
        agent_matches = [self.capabilities.agent_capability_for(capability) for capability in required]
        available_agent_matches = [item for item in agent_matches if item and item.get("available")]
        if available_agent_matches:
            agent_capabilities = [str(item["capability"]) for item in available_agent_matches]
            adjusted = replace(
                decision,
                route=Route.AGENT,
                capability="selected_agent_runtime",
                needs_agent=True,
                needs_user_confirmation=False,
                required_capabilities=list(dict.fromkeys(["selected_agent_runtime", *agent_capabilities])),
                reason=f"selected Agent Runtime can satisfy capability gap: {', '.join(agent_capabilities)}",
                signals=list(dict.fromkeys(decision.signals + ["delegation:agent_capability_fallback"])),
                constraints=list(
                    dict.fromkeys(
                        decision.constraints
                        + [
                            "selected Agent Runtime must return evidence",
                            "do not fabricate external or attachment content",
                            "route risky tool calls through ToolProxy",
                        ]
                    )
                ),
            )
            return adjusted, self._trace(adjusted, "agent_capability_fallback", agent_capabilities=agent_capabilities)
        if self._should_force_agent_dialogue(decision):
            return self._force_agent_dialogue(decision, "agent_first_dialogue_capability_gap")
        return decision, self._trace(decision, "no_native_or_agent_capability")

    def _agent_ready(self, decision: Decision, reason: str) -> tuple[Decision, dict[str, Any]]:
        required = list(dict.fromkeys(["selected_agent_runtime", *decision.required_capabilities]))
        adjusted = replace(decision, required_capabilities=required, needs_agent=True)
        return adjusted, self._trace(adjusted, reason)

    def _force_agent_dialogue(self, decision: Decision, reason: str) -> tuple[Decision, dict[str, Any]]:
        agent_capabilities: list[str] = []
        for capability in decision.required_capabilities:
            match = self.capabilities.agent_capability_for(capability)
            if isinstance(match, dict) and match.get("available"):
                token = str(match.get("capability") or "")
                if token:
                    agent_capabilities.append(token)
        required = list(dict.fromkeys(["selected_agent_runtime", *agent_capabilities]))
        adjusted = replace(
            decision,
            route=Route.AGENT,
            capability="selected_agent_runtime",
            needs_agent=True,
            needs_user_confirmation=False,
            required_capabilities=required,
            reason=f"agent-first dialogue mode: {decision.reason}",
            signals=list(dict.fromkeys(decision.signals + ["delegation:agent_first_dialogue"])),
            constraints=list(
                dict.fromkeys(
                    decision.constraints
                    + [
                        "selected Agent Runtime should preserve conversation continuity",
                        "report capability limits explicitly instead of asking broad follow-up questions",
                    ]
                )
            ),
        )
        return adjusted, self._trace(adjusted, reason, agent_capabilities=agent_capabilities)

    def _should_force_agent_dialogue(self, decision: Decision) -> bool:
        if not self._agent_first_dialogue_enabled():
            return False
        if decision.risk_level not in {RiskLevel.R0, RiskLevel.R1}:
            return False
        if decision.route not in {Route.DIRECT_ANSWER, Route.ASK_USER}:
            return False
        if decision.intent in {"preference", "identity"}:
            return False
        return True

    def _agent_first_dialogue_enabled(self) -> bool:
        config = self.capabilities.state_store.read_json("agent_config.json")
        routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
        mode = str(routing.get("dialogue_route") or "").strip().lower()
        if mode in {"agent_first", "all_to_agent", "agent"}:
            return True
        if mode in {"hybrid", "native_first", "native"}:
            return False
        return bool(routing.get("agent_first_dialogue", False))

    def _force_route(self, decision: Decision, route: Route, reason: str) -> tuple[Decision, dict[str, Any]]:
        adjusted = replace(
            decision,
            route=route,
            needs_agent=False,
            needs_probe=False,
            needs_user_confirmation=route == Route.HUMAN_REVIEW,
            signals=list(dict.fromkeys(decision.signals + [f"delegation:{route.value}"])),
        )
        return adjusted, self._trace(adjusted, reason)

    def _trace(self, decision: Decision, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "route": decision.route.value,
            "status": "ready",
            "reason": reason,
            "intent": decision.intent,
            "complexity": decision.complexity,
            "risk_level": decision.risk_level.value,
            "freshness_required": decision.freshness_required,
            "required_capabilities": decision.required_capabilities,
            **extra,
        }
