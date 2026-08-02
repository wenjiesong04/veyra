from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from core.capability_registry import CapabilityRegistry
from core.delegation_policy import DelegationPolicy
from core.decision_plan import DecisionPlan
from core.execution_tier import (
    TIER_L0_DIRECT_ANSWER,
    TIER_L1_COMPACT_EXTERNAL,
    TIER_L2_PROBE_VERIFY,
    TIER_L3_SCOPED_AGENT,
    TIER_L4_GUARDED_ACTION,
)
from core.final_route_projection import (
    has_effectful_candidate,
    project_governance_route,
    project_nonexecuting_route,
)
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

    def __init__(
        self,
        capability_registry: CapabilityRegistry,
        *,
        agent_runtime_refresher: Callable[[], None] | None = None,
    ) -> None:
        self.capabilities = capability_registry
        self.delegation_policy = DelegationPolicy(capability_registry)
        self._agent_runtime_refresher = agent_runtime_refresher

    def prepare(self, decision: Decision) -> tuple[Decision, ControllerPlan]:
        agent_refresh_attempted = False
        agent_refresh_failed = False
        lineage: list[Decision] = [decision]

        def finish(
            final_decision: Decision,
            plan: ControllerPlan,
        ) -> tuple[Decision, ControllerPlan]:
            finalized = self._finalize_controller_decision(
                final_decision,
                lineage=[*lineage, final_decision],
                reason=plan.reason,
            )
            if finalized.route != plan.route:
                plan = replace(plan, route=finalized.route)
            return finalized, plan

        def refresh_agent_observation() -> None:
            nonlocal agent_refresh_attempted, agent_refresh_failed
            if (
                agent_refresh_attempted
                or self._agent_runtime_refresher is None
            ):
                return
            agent_refresh_attempted = True
            try:
                self._agent_runtime_refresher()
            except Exception:
                # A failed observation must never authorize Agent execution
                # from a previously cached snapshot.
                agent_refresh_failed = True

        delegated, delegation_trace = self.delegation_policy.apply(decision)
        lineage.append(delegated)
        # A degraded semantic resolver cannot authorize even creation of a
        # review proposal.  Preserve the deterministic R3/R4 risk floor, but
        # let the semantic clarification lock project the turn to ASK_USER
        # before the governance route is materialized.  R5 BLOCK and explicit
        # rollback handling remain terminal governance routes.
        semantic_policy = self._semantic_policy(delegated)
        if (
            delegated.route == Route.HUMAN_REVIEW
            and bool(semantic_policy.get("requires_clarification"))
        ):
            clarified, clarification_reason = self._enforce_semantic_boundary(
                delegated
            )
            lineage.append(clarified)
            if clarified.route == Route.ASK_USER:
                return finish(
                    clarified,
                    ControllerPlan(
                        Route.ASK_USER,
                        "clarification_required",
                        clarification_reason,
                        [],
                    ),
                )
        if delegated.route in {
            Route.BLOCK,
            Route.HUMAN_REVIEW,
            Route.ROLLBACK,
        }:
            governed = project_governance_route(
                delegated,
                route=delegated.route,
                reason=str(
                    delegation_trace.get("reason")
                    or delegated.reason
                    or "governance route selected"
                ),
                original=decision,
                signal="controller:governance_route_canonicalized",
            )
            lineage.append(governed)
            governed = self._add_route_capability(governed)
            lineage.append(governed)
            missing = self.capabilities.missing(
                list(dict.fromkeys(governed.required_capabilities))
            )
            return finish(
                governed,
                ControllerPlan(
                    governed.route,
                    "ready",
                    str(
                        delegation_trace.get("reason")
                        or "governance route selected"
                    ),
                    missing,
                ),
            )
        delegated, semantic_boundary_reason = self._enforce_semantic_boundary(delegated)
        lineage.append(delegated)
        if semantic_boundary_reason:
            delegation_trace = {
                **delegation_trace,
                "reason": semantic_boundary_reason,
                "semantic_boundary": "enforced",
            }
        normalized = self._add_route_capability(delegated)
        lineage.append(normalized)
        required = list(dict.fromkeys(normalized.required_capabilities))
        if (
            self._agent_runtime_refresher is not None
            and (
                normalized.route == Route.AGENT
                or normalized.needs_agent
                or "selected_agent_runtime" in required
            )
        ):
            refresh_agent_observation()
        missing = self.capabilities.missing(required)
        if agent_refresh_failed and (
            normalized.route == Route.AGENT
            or normalized.needs_agent
            or "selected_agent_runtime" in required
        ):
            missing = self._with_failed_agent_observation(missing)
        if normalized.route == Route.BLOCK:
            return finish(normalized, ControllerPlan(Route.BLOCK, "ready", delegation_trace.get("reason", "guardian block route selected"), missing))
        unsupported_probe = self._unsupported_probe(normalized)
        if unsupported_probe:
            if self._agent_fallback_refresh_allowed(normalized):
                refresh_agent_observation()
            fallback_required = self._probe_fallback_capabilities(normalized, unsupported_probe)
            agent_capabilities = (
                []
                if agent_refresh_failed
                else self._available_agent_capabilities_for_missing(
                    replace(
                        normalized,
                        required_capabilities=fallback_required,
                    ),
                    [
                        {"capability": capability}
                        for capability in fallback_required
                    ],
                )
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
                return finish(adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, [{"capability": unsupported_probe, "reason": "unsupported_native_probe"}]))
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                reason=f"unsupported native probe: {unsupported_probe}",
                capability="ask_user",
                needs_probe=False,
                signals=list(dict.fromkeys(normalized.signals + ["controller:unsupported_probe"])),
                constraints=list(dict.fromkeys(normalized.constraints + ["do not execute unsupported probe"])),
            )
            adjusted = project_nonexecuting_route(
                adjusted,
                route=Route.ASK_USER,
                reason=adjusted.reason,
                original=normalized,
                signal="controller:unsupported_probe_nonexecution",
            )
            return finish(adjusted, ControllerPlan(Route.ASK_USER, "missing_probe", adjusted.reason, [{"capability": unsupported_probe, "reason": "unsupported_native_probe"}]))
        if missing and self._agent_fallback_refresh_allowed(normalized):
            refresh_agent_observation()
            if agent_refresh_failed:
                missing = self._with_failed_agent_observation(missing)
            else:
                # ASK_USER capability gaps may have been classified from an
                # expired provider snapshot. Recompute after the one bounded
                # refresh before deciding whether an Agent fallback exists.
                missing = self.capabilities.missing(required)
        if missing:
            agent_capabilities = self._available_agent_capabilities_for_missing(normalized, missing)
            if agent_refresh_failed:
                agent_capabilities = []
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
                return finish(adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, missing))
            if (
                not agent_refresh_failed
                and self._should_agent_fallback(normalized)
            ):
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
                return finish(adjusted, ControllerPlan(Route.AGENT, "rerouted", adjusted.reason, missing))
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                reason=f"missing required capability: {', '.join(item.get('capability', '') for item in missing)}",
                capability="ask_user",
                needs_user_confirmation=False,
                signals=list(dict.fromkeys(normalized.signals + ["controller:missing_capability"])),
                constraints=list(dict.fromkeys(normalized.constraints + ["do not fabricate unavailable capability results"])),
            )
            adjusted = project_nonexecuting_route(
                adjusted,
                route=Route.ASK_USER,
                reason=adjusted.reason,
                original=normalized,
                signal="controller:missing_capability_nonexecution",
            )
            return finish(adjusted, ControllerPlan(Route.ASK_USER, "missing_capability", adjusted.reason, missing))
        if normalized.needs_probe and normalized.route != Route.PROBE:
            probe_capability = self.capabilities.capability_for_probe(normalized.selected_probe)
            if normalized.selected_probe and (not probe_capability or self.capabilities.is_available(probe_capability)):
                adjusted = replace(
                    normalized,
                    route=Route.PROBE,
                    capability="probe",
                    signals=list(dict.fromkeys(normalized.signals + ["controller:probe_required"])),
                )
                return finish(adjusted, ControllerPlan(Route.PROBE, "rerouted", "fresh evidence requires a probe", []))
            adjusted = replace(
                normalized,
                route=Route.ASK_USER,
                capability="ask_user",
                signals=list(dict.fromkeys(normalized.signals + ["controller:probe_missing"])),
            )
            adjusted = project_nonexecuting_route(
                adjusted,
                route=Route.ASK_USER,
                reason="fresh evidence is required but no executable probe was selected",
                original=normalized,
                signal="controller:missing_probe_nonexecution",
            )
            return finish(adjusted, ControllerPlan(Route.ASK_USER, "missing_probe", "fresh evidence is required but no executable probe was selected", []))
        if normalized.needs_agent and normalized.route != Route.AGENT:
            adjusted = replace(
                normalized,
                route=Route.AGENT,
                capability="selected_agent_runtime",
                signals=list(dict.fromkeys(normalized.signals + ["controller:agent_required"])),
            )
            return finish(adjusted, ControllerPlan(Route.AGENT, "rerouted", "execution requires agent runtime", []))
        return finish(normalized, ControllerPlan(normalized.route, "ready", str(delegation_trace.get("reason") or "route is executable"), []))

    @staticmethod
    def _finalize_controller_decision(
        decision: Decision,
        *,
        lineage: list[Decision],
        reason: str,
    ) -> Decision:
        """Make the public Decision agree with the Controller's terminal route.

        Intermediate delegation and semantic rewrites are not public authority.
        Every return from ``prepare`` passes here so a later reroute cannot retain
        an execution-shaped DecisionPlan or a stale no-execution renderer.
        """

        effectful_source = next(
            (candidate for candidate in lineage if has_effectful_candidate(candidate)),
            None,
        )
        has_denied_lineage = any(
            bool(candidate.response_authority)
            or bool(
                candidate.model_assist.get("denied_capability_candidate")
                if isinstance(candidate.model_assist, dict)
                else False
            )
            for candidate in lineage
        )
        if decision.route in {Route.DIRECT_ANSWER, Route.ASK_USER} and (
            effectful_source is not None or has_denied_lineage
        ):
            return project_nonexecuting_route(
                decision,
                route=decision.route,
                reason=reason or decision.reason,
                original=effectful_source or lineage[0],
                signal="controller:final_nonexecution",
            )
        if decision.route in {
            Route.BLOCK,
            Route.HUMAN_REVIEW,
            Route.ROLLBACK,
        }:
            return project_governance_route(
                decision,
                route=decision.route,
                reason=reason or decision.reason,
                original=effectful_source or lineage[0],
                signal="controller:final_governance_projection",
            )

        assist = dict(decision.model_assist or {})
        for key in (
            "decision_plan",
            "draft_response",
            "execution_status_claim",
        ):
            assist.pop(key, None)
        reply_strategy = (
            dict(assist.get("reply_strategy") or {})
            if isinstance(assist.get("reply_strategy"), dict)
            else {}
        )
        reply_strategy.pop("draft_response", None)
        assist["reply_strategy"] = reply_strategy
        if decision.route not in {Route.DIRECT_ANSWER, Route.ASK_USER}:
            required_capabilities = list(decision.required_capabilities)
            if decision.route == Route.PROBE:
                required_capabilities = [
                    item
                    for item in required_capabilities
                    if item != "selected_agent_runtime"
                ]
                decision = replace(
                    decision,
                    target_agent=None,
                    needs_agent=False,
                    needs_probe=True,
                    required_capabilities=required_capabilities,
                )
                assist.pop("agent_goal", None)
            elif decision.route == Route.AGENT:
                decision = replace(
                    decision,
                    selected_probe=None,
                    needs_probe=False,
                    needs_agent=True,
                )
                assist.pop("probe_params", None)
            elif decision.route == Route.SKILL:
                decision = replace(
                    decision,
                    target_agent=None,
                    needs_agent=False,
                    needs_probe=False,
                )
                assist.pop("agent_goal", None)
                assist.pop("probe_params", None)
            elif decision.route == Route.NATIVE_TOOL:
                decision = replace(
                    decision,
                    selected_probe=None,
                    target_agent=None,
                    needs_agent=False,
                    needs_probe=False,
                )
                assist.pop("agent_goal", None)
                assist.pop("probe_params", None)
            assist["recommended_route"] = decision.route.value
            assist["needs_probe"] = decision.route == Route.PROBE
            assist["needs_agent"] = decision.route == Route.AGENT
            assist["required_capabilities"] = list(
                decision.required_capabilities
            )
            assist["execution_tier"] = VeyraController._execution_tier_for_route(
                decision.route,
                existing=str(assist.get("execution_tier") or ""),
            )
            assist["capability_request"] = VeyraController._capability_request_for_route(
                decision
            )
            finalized = replace(
                decision,
                capability_request=dict(assist["capability_request"]),
                response_authority={},
                model_assist=assist,
            )
        else:
            finalized = replace(decision, model_assist=assist)
        final_assist = dict(finalized.model_assist or {})
        final_assist["decision_plan"] = DecisionPlan.from_decision(
            "", finalized
        ).to_dict()
        return replace(finalized, model_assist=final_assist)

    @staticmethod
    def _execution_tier_for_route(route: Route, *, existing: str = "") -> str:
        if route in {Route.DIRECT_ANSWER, Route.ASK_USER}:
            return TIER_L0_DIRECT_ANSWER
        if route == Route.PROBE:
            if existing in {TIER_L1_COMPACT_EXTERNAL, TIER_L2_PROBE_VERIFY}:
                return existing
            return TIER_L2_PROBE_VERIFY
        if route == Route.SKILL:
            return TIER_L2_PROBE_VERIFY
        if route == Route.AGENT:
            if existing in {TIER_L3_SCOPED_AGENT, TIER_L4_GUARDED_ACTION}:
                return existing
            return TIER_L3_SCOPED_AGENT
        return TIER_L4_GUARDED_ACTION

    @staticmethod
    def _capability_request_for_route(decision: Decision) -> dict[str, Any]:
        existing = (
            decision.capability_request
            if isinstance(decision.capability_request, dict)
            else {}
        )
        route = decision.route
        receiver = {
            Route.PROBE: "probe",
            Route.SKILL: "skill",
            Route.AGENT: "agent",
            Route.NATIVE_TOOL: "tool",
            Route.HUMAN_REVIEW: "guardian",
            Route.BLOCK: "guardian",
            Route.ROLLBACK: "core",
        }.get(route, "core")
        executor = {
            Route.PROBE: decision.selected_probe or "",
            Route.SKILL: decision.selected_probe or "",
            Route.AGENT: decision.target_agent or "selected_agent_runtime",
            Route.NATIVE_TOOL: str(existing.get("executor") or ""),
        }.get(route, "")
        input_payload = (
            dict(existing.get("input") or {})
            if str(existing.get("target_route") or "") == route.value
            and isinstance(existing.get("input"), dict)
            else {}
        )
        return {
            "capability": decision.capability,
            "target_route": route.value,
            "receiver_type": receiver,
            "executor": executor,
            "input": input_payload,
            "reason": decision.reason,
            **(
                {"probe": decision.selected_probe}
                if route == Route.PROBE and decision.selected_probe
                else {}
            ),
        }

    def _agent_fallback_refresh_allowed(self, decision: Decision) -> bool:
        return bool(
            decision.risk_level.value in {"R0", "R1"}
            and self._semantic_effect_allowed(decision, "agent.execute")
            and decision.route
            in {
                Route.DIRECT_ANSWER,
                Route.PROBE,
                Route.ASK_USER,
                Route.AGENT,
            }
        )

    @staticmethod
    def _with_failed_agent_observation(
        missing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if any(
            str(item.get("capability") or "")
            == "selected_agent_runtime"
            for item in missing
        ):
            return missing
        return [
            *missing,
            {
                "capability": "selected_agent_runtime",
                "available": False,
                "status": "observation_failed",
                "reason": (
                    "selected Agent Runtime freshness observation failed"
                ),
            },
        ]

    def _available_agent_capabilities_for_missing(self, decision: Decision, missing: list[dict[str, Any]]) -> list[str]:
        if not self._semantic_effect_allowed(decision, "agent.execute"):
            return []
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
        if not self._semantic_effect_allowed(decision, "agent.execute"):
            return False
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

    def _enforce_semantic_boundary(self, decision: Decision) -> tuple[Decision, str]:
        """Prevent delegation and capability fallback from widening authority."""

        policy = self._semantic_policy(decision)
        if not policy:
            if decision.route in {
                Route.AGENT,
                Route.NATIVE_TOOL,
                Route.PROBE,
                Route.SKILL,
            } or decision.needs_agent or decision.needs_probe:
                adjusted = replace(
                    decision,
                    route=Route.ASK_USER,
                    capability="ask_user",
                    selected_probe=None,
                    needs_agent=False,
                    needs_probe=False,
                    needs_user_confirmation=False,
                    required_capabilities=[],
                    memory_policy="forget",
                    signals=list(
                        dict.fromkeys(
                            decision.signals
                            + ["controller:semantic_policy_missing_fail_closed"]
                        )
                    ),
                )
                adjusted = project_nonexecuting_route(
                    adjusted,
                    route=Route.ASK_USER,
                    reason="semantic policy is required before capability execution",
                    original=decision,
                    signal="controller:semantic_policy_missing_fail_closed",
                )
                return adjusted, "semantic policy is required before capability execution"
            return decision, ""
        preferred = str(policy.get("preferred_route") or "")
        requires_clarification = bool(policy.get("requires_clarification"))
        agent_allowed = self._semantic_effect_allowed(decision, "agent.execute")
        allowed_capabilities = {
            str(item)
            for item in policy.get("allowed_capabilities", [])
            if isinstance(item, str) and item
        }

        if requires_clarification:
            adjusted = replace(
                decision,
                route=Route.ASK_USER,
                capability="ask_user",
                selected_probe=None,
                needs_agent=False,
                needs_probe=False,
                needs_user_confirmation=False,
                required_capabilities=[],
                signals=list(dict.fromkeys(decision.signals + ["controller:semantic_clarification_lock"])),
            )
            adjusted = project_nonexecuting_route(
                adjusted,
                route=Route.ASK_USER,
                reason="semantic policy requires clarification before execution",
                original=decision,
                signal="controller:semantic_clarification_lock",
            )
            return adjusted, "semantic policy requires clarification before execution"

        if preferred == Route.ASK_USER.value:
            adjusted = replace(
                decision,
                route=Route.ASK_USER,
                capability="ask_user",
                selected_probe=None,
                target_agent=None,
                needs_agent=False,
                needs_probe=False,
                needs_user_confirmation=False,
                required_capabilities=[],
                signals=list(
                    dict.fromkeys(
                        decision.signals
                        + ["controller:semantic_ask_user_lock"]
                    )
                ),
            )
            adjusted = project_nonexecuting_route(
                adjusted,
                route=Route.ASK_USER,
                reason="semantic policy selected clarification instead of execution",
                original=decision,
                signal="controller:semantic_ask_user_lock",
            )
            return (
                adjusted,
                "semantic policy selected clarification instead of execution",
            )

        if decision.route == Route.AGENT and not agent_allowed:
            fallback_route = (
                Route.PROBE
                if preferred == Route.PROBE.value
                else Route.ASK_USER
                if preferred == Route.ASK_USER.value
                else Route.DIRECT_ANSWER
            )
            selected_probe = str(policy.get("selected_probe") or "") or None if fallback_route == Route.PROBE else None
            adjusted = replace(
                decision,
                route=fallback_route,
                capability=(
                    "probe"
                    if fallback_route == Route.PROBE
                    else "ask_user"
                    if fallback_route == Route.ASK_USER
                    else "native_answer"
                ),
                selected_probe=selected_probe,
                needs_agent=False,
                needs_probe=fallback_route == Route.PROBE,
                required_capabilities=(
                    list(allowed_capabilities)
                    if fallback_route == Route.PROBE
                    else ["native_answer"]
                    if fallback_route == Route.DIRECT_ANSWER
                    else ["ask_user"]
                ),
                signals=list(dict.fromkeys(decision.signals + ["controller:semantic_agent_denied"])),
            )
            if fallback_route in {Route.DIRECT_ANSWER, Route.ASK_USER}:
                adjusted = project_nonexecuting_route(
                    adjusted,
                    route=fallback_route,
                    reason="semantic policy denied Agent execution",
                    original=decision,
                    signal="controller:semantic_agent_denied",
                )
            return adjusted, "semantic policy denied Agent execution"

        if decision.route == Route.PROBE:
            probe_capability = self.capabilities.capability_for_probe(decision.selected_probe)
            signals = set(decision.signals or [])
            veyra_freshness_probe = bool(
                decision.selected_probe
                and decision.selected_probe != "search_probe"
                and decision.needs_probe
                and decision.freshness_required
                and "policy:required_probe_preserved" in signals
                and "policy:semantic_cannot_weaken_required_probe" in signals
                and isinstance(decision.capability_request, dict)
                and str(decision.capability_request.get("probe") or "")
                == str(decision.selected_probe or "")
            )
            if not veyra_freshness_probe and (
                preferred != Route.PROBE.value
                or (
                    probe_capability
                    and probe_capability not in allowed_capabilities
                )
            ):
                adjusted = replace(
                    decision,
                    route=Route.DIRECT_ANSWER,
                    capability="native_answer",
                    selected_probe=None,
                    needs_probe=False,
                    required_capabilities=["native_answer"],
                    signals=list(dict.fromkeys(decision.signals + ["controller:semantic_probe_denied"])),
                )
                adjusted = project_nonexecuting_route(
                    adjusted,
                    route=Route.DIRECT_ANSWER,
                    reason="semantic policy denied the selected probe",
                    original=decision,
                    signal="controller:semantic_probe_denied",
                )
                return adjusted, "semantic policy denied the selected probe"
            if veyra_freshness_probe:
                decision = replace(
                    decision,
                    signals=list(
                        dict.fromkeys(
                            decision.signals
                            + ["controller:veyra_freshness_probe_preserved"]
                        )
                    ),
                )

        if decision.needs_agent and not agent_allowed:
            return (
                replace(
                    decision,
                    needs_agent=False,
                    signals=list(dict.fromkeys(decision.signals + ["controller:semantic_agent_need_cleared"])),
                ),
                "semantic policy cleared an unauthorized Agent requirement",
            )
        return decision, ""

    def _semantic_policy(self, decision: Decision) -> dict[str, Any]:
        assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        policy = assist.get("semantic_policy")
        return policy if isinstance(policy, dict) else {}

    def _semantic_effect_allowed(self, decision: Decision, effect: str) -> bool:
        policy = self._semantic_policy(decision)
        if not policy:
            return False
        allowed = {str(item) for item in policy.get("allowed_effects", []) if isinstance(item, str)}
        denied = {str(item) for item in policy.get("denied_effects", []) if isinstance(item, str)}
        return effect in allowed and effect not in denied

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
