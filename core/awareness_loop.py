from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict, replace
from typing import Any

from awareness.attention_core import AttentionCore
from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.action_risk import assess_text_risk
from core.agent_session_router import AgentSessionRouter
from core.capability_registry import CapabilityRegistry
from core.context_patch_builder import ContextPatchBuilder
from core.compact_external_lookup import CompactExternalLookup
from core.context_scope import ContextScopeFilter
from core.execution_tier import (
    TIER_L0_DIRECT_ANSWER,
    TIER_L1_COMPACT_EXTERNAL,
    TIER_L2_PROBE_VERIFY,
    TIER_L3_SCOPED_AGENT,
    TIER_L5_PROACTIVE,
    classify_execution_tier,
)
from core.definitions import GuardianDecision, LifecycleStatus, RiskLevel
from core.decision_core import DecisionCore
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.memory_policy_runtime import MemoryPolicyRuntime
from core.model_client import redact_sensitive
from core.perception_layer import PerceptionLayer
from core.persona_engine import PersonaEngine
from core.reasoning_core import CoreReasoning
from core.result_interpreter import ResultInterpreter
from core.runtime_entity import RuntimeEntity
from core.response_synthesizer import ResponseSynthesizer
from core.state_proposal import (
    ProposalNotFoundError,
    StateChangeProposal,
    StateChangeProposalStore,
    deterministic_idempotency_key,
    deterministic_proposal_id,
)
from core.task_packet_builder import TaskPacketBuilder
from core.understanding_core import UnderstandingCore
from core.verifier import Verifier
from core.veyra_controller import VeyraController
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_adapter import ExecutionResult
from interface.channel_adapter import ChannelAdapter
from interface.agent_registry import AgentRegistry
from interface.event_schema import Decision, LoopResult, Route, VeyraEvent, utc_now_iso
from memory_bridge.local_memory_bridge import LocalMemoryBridge
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.file_probe import FileProbe
from probes.log_probe import LogProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from probes.time_probe import TimeProbe
from probes.search_probe import SearchProbe
from probes.weather_probe import WeatherProbe
from probes.web_probe import WebProbe
from rollback_audit.execution_trace import ExecutionTrace
from runtime.agent_task_tracker import AgentTaskTracker
from runtime.bounded_agent_negotiation import (
    BoundedNegotiationError,
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import DurableCaseStore
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
from runtime.routing_trace import RuntimeTraceRecorder
from skills.skill_loader import SkillLoader
from skills.skill_runtime import SkillRuntime


class AwarenessLoop:
    """Sense -> Understand -> Focus -> Evaluate -> Decide -> Act -> Observe -> Verify -> Update."""

    def __init__(
        self,
        state_store: WorldStateStore,
        runtime_entity: RuntimeEntity,
        *,
        commitment_core: Any | None = None,
    ) -> None:
        self.state_store = state_store
        self.runtime_entity = runtime_entity
        self.commitment_core = commitment_core
        self.core_reasoning = CoreReasoning(state_store)
        self.capabilities = CapabilityRegistry(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.core_reasoning)
        self.attention = AttentionCore(state_store)
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()
        self.decision_core = DecisionCore(state_store=state_store, reasoning=self.core_reasoning)
        self.understanding_core = UnderstandingCore(self.core_reasoning, state_store)
        self.foresight = ForesightEngine(reasoning=self.core_reasoning)
        self.guardian = GuardianController()
        self.persona_engine = PersonaEngine(state_store)
        self.context_builder = ContextPatchBuilder(state_store)
        self.context_scope = ContextScopeFilter()
        self.task_packet_builder = TaskPacketBuilder(state_store)
        self.agent_session_router = AgentSessionRouter(state_store)
        self.compact_external_lookup = CompactExternalLookup()
        self.verifier = Verifier()
        self.result_interpreter = ResultInterpreter()
        self.response_synthesizer = ResponseSynthesizer()
        self.agent_registry = AgentRegistry(state_store)
        self.runtime_entity.set_selected_agent(self.agent_registry.selected_name())
        self.agent_adapter = self.agent_registry.selected()
        self.review_queue = ReviewQueue(state_store)
        self.memory_bridge = LocalMemoryBridge(
            state_store,
            adapter_resolver=lambda: self.agent_registry.selected(),
            adapter_getter=lambda provider: self.agent_registry.get(provider),
            provider_names=lambda: self.agent_registry.names(),
            reasoning=self.core_reasoning,
        )
        self.execution_trace = ExecutionTrace(state_store)
        self.task_tracker = AgentTaskTracker(state_store, self.execution_trace)
        self.durable_case_store = DurableCaseStore(state_store)
        self.bounded_negotiation = BoundedNegotiationRuntime(
            state_store=state_store,
            case_store=self.durable_case_store,
            task_packet_builder=self.task_packet_builder,
            verifier=self.verifier,
            response_synthesizer=self.response_synthesizer,
            registry=self.agent_registry,
            task_tracker=self.task_tracker,
        )
        self.memory_policy_runtime = MemoryPolicyRuntime(state_store, lambda patch: self.memory_bridge.write_patch(patch))
        self.state_proposals = StateChangeProposalStore(state_store)
        self.controller = VeyraController(self.capabilities)
        self.runtime_trace = RuntimeTraceRecorder(state_store)
        event_awareness_config = state_store.read_json("ops_config.json").get(
            "event_awareness",
            {},
        )
        if not isinstance(event_awareness_config, dict):
            event_awareness_config = {}
        self.event_awareness = ShadowAwarenessRuntime(
            state_store,
            mode=str(event_awareness_config.get("mode") or "record_only"),
        )
        self.event_inbox = self.event_awareness.event_inbox
        self.situation_evaluator = self.event_awareness.situation_evaluator
        self.skill_loader = SkillLoader()
        self.skill_runtime = SkillRuntime(state_store)
        self.probes = {
            "time": TimeProbe(),
            "time_probe": TimeProbe(),
            "weather_probe": WeatherProbe(),
            "system": SystemProbe(),
            "git": GitProbe(),
            "port": PortProbe(),
            "process": ProcessProbe(),
            "file": FileProbe(),
            "log": LogProbe(),
            "network": NetworkProbe(),
            "web": WebProbe(),
            "search_probe": SearchProbe(),
            "openclaw": OpenClawProbe(),
            "hermes": HermesProbe(),
            "mcp": McpProbe(),
        }

    def handle_event(self, event: VeyraEvent) -> LoopResult:
        started_at = time.perf_counter()
        route_trace: list[dict[str, object]] = [
            {
                "phase": "intake",
                "status": "received",
                "channel": event.source.channel,
                "event_id": event.event_id,
            }
        ]
        context_observability: dict[str, Any] = {}
        self.runtime_entity.set_status(LifecycleStatus.THINKING.value)
        text = event.payload.get("text", "")
        self.state_store.append_jsonl("event_log.jsonl", self._event_log_record(event))
        try:
            shadow_intake = self.event_awareness.begin(event)
        except Exception as exc:
            shadow_intake = {
                "status": "degraded",
                "error_type": type(exc).__name__,
            }
        route_trace.append(
            {
                "phase": "event_fabric_shadow",
                "status": shadow_intake.get("status", "unavailable"),
                "event_id": shadow_intake.get("event_id"),
                "situation_id": shadow_intake.get("situation_id"),
                "finalize_allowed": bool(shadow_intake.get("finalize_allowed")),
            }
        )

        attention_focus = self.attention.focus_for_text(text)
        self.belief.update_from_event(event)
        low_latency_response = self._low_latency_short_response(str(text or ""))
        if low_latency_response and str(low_latency_response.get("reason") or "") in {
            "empty_short_message",
            "casual_short_message",
            "latency_complaint",
        }:
            low_latency_route = Route(str(low_latency_response.get("route") or Route.DIRECT_ANSWER.value))
            route_trace.append(
                {
                    "phase": "low_latency_short_reply",
                    "status": "handled",
                    "reason": low_latency_response.get("reason"),
                }
            )
            result = LoopResult(
                event_id=event.event_id,
                route=low_latency_route,
                status=str(low_latency_response.get("status") or "success"),
                response=str(low_latency_response.get("response") or ""),
                risk_level=RiskLevel.R0,
                artifacts={
                    "low_latency_short_reply": low_latency_response,
                    "attention": attention_focus,
                    "execution_tier": TIER_L0_DIRECT_ANSWER,
                },
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)
        belief_state = self.belief.refresh()
        preliminary_persona = self.persona_engine.patch_for(
            text,
            RiskLevel.R1,
            channel=event.source.channel,
            route=None,
            target_agent=None,
            decision={},
        )
        turn_context = self.core_reasoning.turn_context.build(
            user_message=text,
            attention_focus=attention_focus,
            event=event,
            rule_decision={},
            persona_hint=preliminary_persona,
        )
        context_observability = {
            "context_metrics": turn_context.get("_context_metrics", {}) if isinstance(turn_context, dict) else {},
            "context_drift": turn_context.get("_context_drift", {}) if isinstance(turn_context, dict) else {},
        }
        route_trace.append(
            {
                "phase": "awareness_loop",
                "status": "context_built",
                "attention_focus": attention_focus[:8],
                "context_chars": context_observability.get("context_metrics", {}).get("context_chars"),
                "drift_score": context_observability.get("context_drift", {}).get("drift_score"),
            }
        )
        turn_understanding = self.understanding_core.build(
            text=str(text or ""),
            attention_focus=attention_focus,
            event=event,
            turn_context=turn_context,
        )
        understanding_payload = turn_understanding.to_dict(include_raw=False)
        turn_context["turn_understanding"] = understanding_payload
        route_trace.append(
            {
                "phase": "understanding",
                "status": turn_understanding.source or "available",
                "intent": turn_understanding.intent,
                "suggested_mode": turn_understanding.suggested_mode,
                "project": turn_understanding.project,
                "needs_fresh_evidence": turn_understanding.needs_fresh_evidence,
                "evidence_kind": turn_understanding.evidence_kind,
            }
        )
        decision = self.decision_core.decide(
            text=text,
            attention_focus=attention_focus,
            event=event,
            turn_context=turn_context,
            turn_understanding=turn_understanding,
        )
        route_trace.append(
            {
                "phase": "core_cognition",
                "route": decision.route.value,
                "risk_level": decision.risk_level.value,
                "intent": decision.intent,
                "model_status": decision.model_assist.get("status") if isinstance(decision.model_assist, dict) else None,
            }
        )
        followup_result = self._conversation_followup_result(
            event,
            str(text or ""),
            attention_focus,
            decision=decision,
        )
        if followup_result and self._semantic_result_allowed(decision, followup_result):
            followup_result.artifacts.setdefault("decision", decision.to_dict())
            followup_result.artifacts.setdefault("turn_understanding", understanding_payload)
            route_trace.append(
                {
                    "phase": "conversation_followup",
                    "status": "handled",
                    "route": followup_result.route.value,
                }
            )
            return self._finalize_event(event, followup_result, started_at, route_trace, context_observability)
        early_response = self._early_awareness_response(
            event,
            str(text or ""),
            attention_focus,
            decision=decision,
        )
        if early_response:
            route_trace.append(
                {
                    "phase": "semantic_awareness",
                    "status": "handled",
                    "reason": early_response.get("reason"),
                }
            )
            result = LoopResult(
                event_id=event.event_id,
                route=Route.DIRECT_ANSWER,
                status="success",
                response=str(early_response.get("response") or ""),
                risk_level=decision.risk_level,
                artifacts={
                    "early_awareness": early_response,
                    "attention": attention_focus,
                    "decision": decision.to_dict(),
                    "turn_understanding": understanding_payload,
                },
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)
        decision, controller_plan = self.controller.prepare(decision)
        route_trace.append({"phase": "controller", **controller_plan.to_dict()})
        self.state_store.patch_json("risk_state.json", {"current_risk": decision.risk_level.value})
        persona_patch = self.persona_engine.patch_for(
            text,
            decision.risk_level,
            channel=event.source.channel,
            route=decision.route.value,
            target_agent=decision.target_agent or self.agent_registry.selected_name(),
            decision=decision.to_dict(),
        )
        self.persona_engine.record_binding(event, persona_patch)
        self.runtime_entity.operational_mode = list(persona_patch.get("mode", []))
        foresight = self._foresight_for_decision(text, decision)
        guardian_decision = self.guardian.review_text_action(text=text, decision=decision, foresight=foresight)
        route_trace.append(
            {
                "phase": "guardian",
                "decision": guardian_decision.get("decision"),
                "risk_level": guardian_decision.get("risk_level"),
                "reason": guardian_decision.get("reason"),
            }
        )

        if guardian_decision["decision"] == GuardianDecision.BLOCK.value:
            self.runtime_entity.set_status(LifecycleStatus.BLOCKED.value)
            result = LoopResult(
                event_id=event.event_id,
                route=Route.BLOCK,
                status="blocked",
                response=guardian_decision["reason"],
                risk_level=decision.risk_level,
                artifacts={"guardian": guardian_decision, "foresight": foresight, "persona": persona_patch, "controller": controller_plan.to_dict()},
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)

        if guardian_decision["decision"] == GuardianDecision.ASK_USER.value:
            self.runtime_entity.set_status(LifecycleStatus.WAITING_CONFIRMATION.value)
            proposal = self._confirmation_proposal(text, decision)
            review = self.review_queue.create(
                event_id=event.event_id,
                task_text=text,
                risk_level=decision.risk_level.value,
                foresight=foresight,
                guardian_decision=guardian_decision,
                proposal=proposal,
            )
            response = (
                "该动作需要用户确认；当前尚无可执行 proposal，"
                "确认只记录授权，不会自动执行。"
                if proposal is None
                else "该动作需要用户确认后才能执行。"
            )
            if decision.route == Route.ROLLBACK and proposal is not None:
                response = "该回滚动作需要用户确认；确认后将通过 RollbackManager 恢复指定 snapshot。"
            result = LoopResult(
                event_id=event.event_id,
                route=Route.HUMAN_REVIEW,
                status="needs_confirmation",
                response=response,
                risk_level=decision.risk_level,
                artifacts={"guardian": guardian_decision, "foresight": foresight, "review": review, "proposal": proposal, "persona": persona_patch, "controller": controller_plan.to_dict()},
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)

        self.runtime_entity.set_status(LifecycleStatus.ACTING.value)
        execution_tier = classify_execution_tier(text, decision)
        semantic_policy = self._semantic_policy_from_decision(decision)
        semantic_route = str(semantic_policy.get("preferred_route") or "")
        if semantic_route in {Route.DIRECT_ANSWER.value, Route.ASK_USER.value}:
            execution_tier = TIER_L0_DIRECT_ANSWER
        elif semantic_route == Route.PROBE.value and execution_tier == TIER_L5_PROACTIVE:
            execution_tier = TIER_L2_PROBE_VERIFY
        elif semantic_route == Route.AGENT.value and execution_tier == TIER_L5_PROACTIVE:
            execution_tier = TIER_L3_SCOPED_AGENT
        model_assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        model_first = model_assist.get("pipeline") in {"model_first", "awareness_model_first"}
        if isinstance(decision.model_assist, dict):
            decision.model_assist["execution_tier"] = execution_tier
        else:
            decision.model_assist = {"execution_tier": execution_tier}
        if execution_tier == TIER_L1_COMPACT_EXTERNAL and (not model_first or decision.route == Route.DIRECT_ANSWER):
            compact = self.compact_external_lookup.run(text)
            if compact.get("status") == "ok":
                route_trace.append({"phase": "compact_external_lookup", "status": "ok", "provider": compact.get("provider")})
                result = LoopResult(
                    event_id=event.event_id,
                    route=Route.PROBE,
                    status="compact_lookup_success",
                    response=str(compact.get("response") or ""),
                    risk_level=decision.risk_level,
                    artifacts={
                        "decision": decision.to_dict(),
                        "controller": controller_plan.to_dict(),
                        "persona": persona_patch,
                        "execution_tier": execution_tier,
                        "compact_lookup": compact,
                        **({"probe_result": compact.get("probe_result")} if isinstance(compact.get("probe_result"), dict) else {}),
                    },
                )
                return self._finalize_event(event, result, started_at, route_trace, context_observability)
            route_trace.append({"phase": "compact_external_lookup", "status": "fallback", "reason": compact.get("reason")})
            compact_probe_result = self._compact_search_probe_result(compact)
            if compact_probe_result:
                result = LoopResult(
                    event_id=event.event_id,
                    route=Route.PROBE,
                    status="compact_lookup_search_results",
                    response=self._search_user_response(compact_probe_result),
                    risk_level=decision.risk_level,
                    artifacts={
                        "decision": decision.to_dict(),
                        "controller": controller_plan.to_dict(),
                        "persona": persona_patch,
                        "execution_tier": execution_tier,
                        "compact_lookup": compact,
                        "probe_result": compact_probe_result,
                    },
                )
                return self._finalize_event(event, result, started_at, route_trace, context_observability)
            failed_probe_result = self._compact_lookup_failure_probe_result(compact)
            result = LoopResult(
                event_id=event.event_id,
                route=Route.PROBE,
                status="compact_lookup_failed",
                response=(
                    "这类最新 YouTube 标题需要可验证的官方频道/RSS 或可信搜索结果。"
                    "我这轮没有拿到可靠证据，因此不会把未验证搜索结果当答案，也不会把简单查询升级成重型 Agent 任务。"
                ),
                risk_level=decision.risk_level,
                artifacts={
                    "decision": decision.to_dict(),
                    "controller": controller_plan.to_dict(),
                    "persona": persona_patch,
                    "execution_tier": execution_tier,
                    "compact_lookup": compact,
                    "probe_result": failed_probe_result,
                    "evidence_attempt": {
                        "provider": "compact_external_lookup",
                        "probe": failed_probe_result.get("probe"),
                        "status": failed_probe_result.get("status"),
                        "reason": compact.get("reason"),
                    },
                },
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)

        if decision.route == Route.DIRECT_ANSWER:
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status="success",
                response=self._direct_answer(event, decision, attention_focus, persona_patch=persona_patch),
                risk_level=decision.risk_level,
                artifacts={
                    "attention": attention_focus,
                    "decision": decision.to_dict(),
                    "controller": controller_plan.to_dict(),
                    "persona": persona_patch,
                    "uncertainty": self.uncertainty.uncertainty_summary(belief_state.get("claims", [])),
                },
            )
        elif decision.route == Route.PROBE:
            result = self._run_probe(event, decision, attention_focus)
        elif decision.route == Route.SKILL:
            result = self._run_skill(event, decision.selected_probe or "")
        elif decision.route == Route.ASK_USER:
            result = LoopResult(
                event_id=event.event_id,
                route=Route.ASK_USER,
                status="needs_user_input",
                response=self._ask_user_response(decision),
                risk_level=decision.risk_level,
                artifacts={
                    "decision": decision.to_dict(),
                    "controller": controller_plan.to_dict(),
                    "persona": persona_patch,
                },
            )
        elif decision.route == Route.AGENT:
            configured_agent = self.agent_registry.selected_name()
            # Model output may recommend a target runtime, but it cannot
            # change the configured provider or choose where user context is
            # sent. Provider selection remains an operator-controlled setting.
            selected_agent = configured_agent
            # Keep the dispatch-time adapter local for the whole event. HTTP
            # polling/status traffic may resolve another historical provider
            # concurrently and must never redirect this user's context.
            selected_adapter = self.agent_registry.selected()
            memory_summary = self.memory_bridge.read_summary(event.source.session_id, attention_focus)
            context_patch = self.context_builder.build(text, attention_focus, decision=decision.to_dict(), foresight=foresight, event=event)
            context_patch["memory_summary"] = memory_summary
            semantic_frame = model_assist.get("semantic_frame") if isinstance(model_assist.get("semantic_frame"), dict) else {}
            if semantic_frame:
                context_patch["semantic_frame"] = semantic_frame
            if semantic_policy:
                context_patch["semantic_policy"] = semantic_policy
                canonical_goals = (
                    semantic_policy.get("canonical_goals")
                    if isinstance(semantic_policy.get("canonical_goals"), list)
                    else []
                )
                if canonical_goals:
                    context_patch["user_goal"] = "\n".join(str(item) for item in canonical_goals if item)[:2000]
            if decision.model_assist:
                context_patch["core_reasoning"] = {
                    "reason": decision.model_assist.get("reason"),
                    "solution_outline": decision.model_assist.get("solution_outline", []),
                    "agent_context": decision.model_assist.get("agent_context", {}),
                    "turn_understanding": decision.model_assist.get("turn_understanding", {}),
                }
            agent_memory_summary = (
                memory_summary.get("external_summary")
                if isinstance(memory_summary.get("external_summary"), dict)
                else {}
            )
            if agent_memory_summary.get("summary"):
                context_patch["agent_memory_summary"] = agent_memory_summary
            scoped = self.context_scope.apply(
                context_patch,
                user_goal=text,
                token_budget_chars=int(persona_patch.get("context_budget_chars") or 6000),
            )
            context_patch = scoped["context_patch"]
            if scoped.get("omitted_audit"):
                self.state_store.append_jsonl(
                    "context_scope_audit.jsonl",
                    {
                        "event_id": event.event_id,
                        "session_id": event.source.session_id,
                        "scope": scoped.get("scope"),
                        "omitted": scoped.get("omitted_audit"),
                    },
                )
            session_plan = self.agent_session_router.resolve(
                dialogue_session_id=event.source.session_id,
                text=text,
            )
            phase4_enabled = self.bounded_negotiation.supports_adapter(
                selected_adapter
            )
            prepared: dict[str, Any] | None = None
            if phase4_enabled:
                prepared = self.bounded_negotiation.prepare(
                    event=event,
                    selected_agent=selected_agent,
                    persona_patch=persona_patch,
                    policy_patch=self.guardian.policy_patch(
                        decision.risk_level
                    ),
                    context_patch=context_patch,
                    required_capabilities=decision.required_capabilities,
                    memory_policy=decision.memory_policy,
                    agent_execution_session_id=str(
                        session_plan.get("reuse_session_id") or ""
                    ),
                    agent_session_policy=str(
                        session_plan.get("policy")
                        or "ephemeral_per_task"
                    ),
                )
                packet = prepared.get("packet")
            else:
                packet = self.task_packet_builder.build(
                    event=event,
                    target_agent=selected_agent,
                    persona_patch=persona_patch,
                    policy_patch=self.guardian.policy_patch(
                        decision.risk_level
                    ),
                    context_patch=context_patch,
                    required_capabilities=decision.required_capabilities,
                    memory_policy=decision.memory_policy,
                    agent_execution_session_id=session_plan.get(
                        "reuse_session_id"
                    ),
                    agent_session_policy=str(
                        session_plan.get("policy")
                        or "ephemeral_per_task"
                    ),
                )
            if packet is None:
                replayed_case = (
                    prepared.get("case")
                    if isinstance(prepared, dict)
                    and isinstance(prepared.get("case"), dict)
                    else {}
                )
                replayed_case = (
                    self.bounded_negotiation.public_case_summary(
                        replayed_case
                    )
                    if replayed_case
                    else {}
                )
                result = LoopResult(
                    event_id=event.event_id,
                    route=decision.route,
                    status="partially_success",
                    response=(
                        "该事件已绑定到现有 Durable Case；我没有重复下发 "
                        "Agent 或产生新的副作用。"
                    ),
                    risk_level=decision.risk_level,
                    artifacts={
                        "durable_case": replayed_case,
                        "phase4": {
                            "enabled": True,
                            "operation_replayed": True,
                            "execution_profile": (
                                self.bounded_negotiation.EXECUTION_PROFILE
                            ),
                        },
                        "decision": decision.to_dict(),
                        "controller": controller_plan.to_dict(),
                        "guardian": guardian_decision,
                        "persona": persona_patch,
                        "context_scope": scoped.get("scope"),
                    },
                )
            else:
                self.agent_session_router.record(
                    dialogue_session_id=event.source.session_id,
                    agent_execution_session_id=(
                        packet.agent_execution_session_id
                    ),
                    task_id=packet.task_id,
                    user_goal=packet.user_goal,
                )
                execution = selected_adapter.send_task(packet)
                execution = self._poll_if_needed(
                    execution,
                    adapter=selected_adapter,
                )
                phase4_supervision_required = False
                if phase4_enabled and prepared is not None:
                    try:
                        negotiation = (
                            self.bounded_negotiation.accept_execution(
                                prepared=prepared,
                                execution=execution,
                            )
                        )
                    except BoundedNegotiationError as exc:
                        binding = (
                            prepared.get("binding")
                            if isinstance(
                                prepared.get("binding"), dict
                            )
                            else {}
                        )
                        current_case = self.durable_case_store.get_case(
                            case_id=str(binding["case_id"]),
                            user_id=str(binding["user_id"]),
                            workspace_id=str(binding["workspace_id"]),
                        )
                        phase4_supervision_required = True
                        execution = ExecutionResult(
                            task_id=str(binding["runtime_run_id"]),
                            executor=str(binding["target_agent"]),
                            status="submitted",
                            result=(
                                "Agent result awaits exact governance "
                                "reconciliation."
                            ),
                            raw={
                                "phase4_supervision_required": True,
                                "acceptance_error_type": type(exc).__name__,
                            },
                        )
                        verified = {
                            "status": "needs_more_probe",
                            "verdict": (
                                "agent_result_requires_governance_reconciliation"
                            ),
                            "confidence": 0.4,
                            "evidence": {
                                "proposal_is_evidence": False,
                                "proposal_is_authority": False,
                                "terminal_authority_closed": False,
                            },
                            "next_action": (
                                "reconcile_exact_bound_agent_run"
                            ),
                            "needs_rollback": False,
                            "needs_memory_patch": False,
                        }
                        negotiation = {
                            "case": (
                                self.bounded_negotiation
                                .public_case_summary(current_case)
                            ),
                            "dialogue_message": None,
                            "verification": verified,
                        }
                        synthesized_response = (
                            "Agent 终态尚未完成治理闭合；Durable Case 保持"
                            "可评估并由恢复循环继续监督，没有重复下发。"
                        )
                        interpreted = (
                            self.result_interpreter.interpret_execution(
                                execution, verified
                            )
                        )
                    else:
                        verified = negotiation["verification"]
                        synthesized_response = negotiation["response"]
                        interpreted = (
                            self.result_interpreter.interpret_execution(
                                execution, verified
                            )
                        )
                else:
                    negotiation = None
                    verified = self.verifier.verify_execution_result(
                        execution
                    )
                    interpreted = (
                        self.result_interpreter.interpret_execution(
                            execution, verified
                        )
                    )
                    synthesized_response = (
                        self.response_synthesizer.agent_response(
                            interpreted, verified
                        )
                    )
                    self._schedule_agent_follow_up(
                        event=event,
                        decision=decision,
                        execution=execution,
                    )
                phase4_task_packet = (
                    {
                        "target_agent": packet.target_agent,
                        "required_capabilities": list(
                            packet.required_capabilities
                        ),
                        "memory_policy": packet.memory_policy,
                        "verification_policy": dict(
                            packet.verification_policy
                        ),
                        "rollback_requirement": dict(
                            packet.rollback_requirement
                        ),
                        "agent_session_policy": (
                            packet.agent_session_policy
                        ),
                        "dialogue_contract": (
                            str(
                                (
                                    packet.dialogue_message
                                    if isinstance(
                                        packet.dialogue_message, dict
                                    )
                                    else {}
                                ).get("contract_version")
                                or ""
                            )
                        ),
                    }
                    if phase4_enabled
                    else packet.to_dict()
                )
                phase4_interpreted = (
                    {
                        "status": interpreted.get("status"),
                        "executor": interpreted.get("executor"),
                        "verification_status": verified.get("status"),
                        "verification_verdict": verified.get("verdict"),
                        "next_action": verified.get("next_action"),
                    }
                    if phase4_enabled
                    else interpreted
                )
                artifacts = {
                    "task_packet": phase4_task_packet,
                    "execution_result": (
                        self.bounded_negotiation.public_execution_summary(
                            execution
                        )
                        if phase4_enabled
                        else asdict(execution)
                    ),
                    "interpreted_result": phase4_interpreted,
                    "verification": verified,
                    "pending_task": None,
                    "decision": decision.to_dict(),
                    "controller": controller_plan.to_dict(),
                    "guardian": guardian_decision,
                    "persona": persona_patch,
                    "agent_session": {
                        "agent_session_policy": (
                            packet.agent_session_policy
                        ),
                        **(
                            {"isolated": True}
                            if phase4_enabled
                            else {
                                "agent_execution_session_id": (
                                    packet.agent_execution_session_id
                                ),
                                "dialogue_session_id": (
                                    event.source.session_id
                                ),
                                **session_plan.get("trace", {}),
                            }
                        ),
                    },
                    "context_scope": scoped.get("scope"),
                    "phase4": {
                        "enabled": phase4_enabled,
                        "execution_profile": (
                            self.bounded_negotiation.EXECUTION_PROFILE
                            if phase4_enabled
                            else None
                        ),
                    },
                }
                if negotiation is not None:
                    artifacts["durable_case"] = negotiation.get("case")
                    artifacts["agent_dialogue"] = negotiation.get(
                        "dialogue_message"
                    )
                execution_trace = self.execution_trace.record(
                    {
                        "event_id": event.event_id,
                        "route": decision.route.value,
                        "task_id": execution.task_id,
                        "executor": execution.executor,
                        "status": verified["status"],
                        "decision": decision.to_dict(),
                        "controller": controller_plan.to_dict(),
                        "guardian": guardian_decision,
                        "persona": persona_patch,
                        "execution_result": (
                            self.bounded_negotiation
                            .public_execution_summary(execution)
                            if phase4_enabled
                            else asdict(execution)
                        ),
                        "interpreted_result": phase4_interpreted,
                        "verification": verified,
                        "durable_case": (
                            negotiation.get("case")
                            if negotiation is not None
                            else None
                        ),
                    }
                )
                artifacts["execution_trace"] = (
                    self.bounded_negotiation.public_trace_summary(
                        execution_trace
                    )
                    if phase4_enabled
                    else execution_trace
                )
                binding = (
                    prepared.get("binding")
                    if phase4_enabled
                    and isinstance(prepared, dict)
                    and isinstance(prepared.get("binding"), dict)
                    else {}
                )
                pending_task = self.task_tracker.register(
                    event_id=event.event_id,
                    route=decision.route.value,
                    execution=execution,
                    verification=verified,
                    session_id=event.source.session_id,
                    channel=event.source.channel,
                    user_id=event.source.user_id,
                    correlation_id=event.event_id,
                    task_packet_id=packet.task_id,
                    agent_execution_session_id=(
                        packet.agent_execution_session_id
                    ),
                    agent_session_policy=packet.agent_session_policy,
                    memory_policy=packet.memory_policy,
                    verification_policy=packet.verification_policy,
                    rollback_requirement=packet.rollback_requirement,
                    user_goal=packet.user_goal,
                    case_id=binding.get("case_id"),
                    case_workspace_id=binding.get("workspace_id"),
                    case_step_id=binding.get("step_id"),
                    case_operation_id=binding.get("operation_id"),
                    case_revision=binding.get("case_revision"),
                    dialogue_message_id=binding.get("message_id"),
                    target_agent=selected_agent,
                    force_pending=phase4_supervision_required,
                )
                artifacts["pending_task"] = (
                    {
                        "registered": pending_task is not None,
                        "status": execution.status,
                        "verification_status": verified.get("status"),
                        "next_action": verified.get("next_action"),
                        "case_id": binding.get("case_id"),
                        "case_revision": binding.get("case_revision"),
                    }
                    if phase4_enabled
                    else pending_task
                )
                result = LoopResult(
                    event_id=event.event_id,
                    route=decision.route,
                    status=verified["status"],
                    response=synthesized_response,
                    risk_level=decision.risk_level,
                    artifacts=artifacts,
                )
        elif decision.route == Route.NATIVE_TOOL:
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status="needs_action_proposal",
                response="Native tool execution must be submitted as a structured ActionProposal or routed through Tool Proxy.",
                risk_level=decision.risk_level,
                artifacts={
                    "decision": decision.to_dict(),
                    "controller": controller_plan.to_dict(),
                    "guardian": guardian_decision,
                    "persona": persona_patch,
                    "next_action": "submit /actions/proposals with action.type and target details",
                },
            )
        elif decision.route == Route.ROLLBACK:
            result = self._run_rollback_request(event, decision, guardian_decision, foresight)
        else:
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status="unsupported_route",
                response=f"Route {decision.route.value} is not executable without a structured proposal.",
                risk_level=decision.risk_level,
                artifacts={"decision": decision.to_dict(), "next_action": "use a supported route or submit an ActionProposal"},
            )

        result.artifacts.setdefault("persona", persona_patch)
        result.artifacts.setdefault("controller", controller_plan.to_dict())
        result.artifacts.setdefault("execution_tier", execution_tier)
        if result.artifacts.get("memory_write"):
            result.artifacts["memory_policy_execution"] = {"status": "written", "policy": decision.memory_policy, "write": result.artifacts.get("memory_write")}
        else:
            result.artifacts["memory_policy_execution"] = self.memory_policy_runtime.apply(event, decision, result)
        return self._finalize_event(event, result, started_at, route_trace, context_observability)

    def _finalize_event(
        self,
        event: VeyraEvent,
        result: LoopResult,
        started_at: float,
        route_trace: list[dict[str, object]],
        context_observability: dict[str, Any],
    ) -> LoopResult:
        context_metrics = context_observability.get("context_metrics") if isinstance(context_observability.get("context_metrics"), dict) else {}
        context_drift = context_observability.get("context_drift") if isinstance(context_observability.get("context_drift"), dict) else {}
        result.artifacts.setdefault("context_metrics", context_metrics)
        result.artifacts.setdefault("context_drift", context_drift)
        route_trace.append({"phase": "route_execution", "route": result.route.value, "status": result.status})
        verification = result.artifacts.get("verification") if isinstance(result.artifacts.get("verification"), dict) else {}
        if verification:
            route_trace.append(
                {
                    "phase": "verifier",
                    "status": verification.get("status"),
                    "verdict": verification.get("verdict"),
                    "next_action": verification.get("next_action"),
                }
            )
        memory_policy = result.artifacts.get("memory_policy_execution") if isinstance(result.artifacts.get("memory_policy_execution"), dict) else {}
        if memory_policy:
            route_trace.append({"phase": "memory_policy", "status": memory_policy.get("status"), "policy": memory_policy.get("policy")})
        route_trace.append({"phase": "response", "status": result.status, "final_route": result.route.value})
        commitment_turn = self._process_commitment_turn(event, result)
        if commitment_turn:
            result.artifacts["commitment"] = commitment_turn
            # Commitment-only turns should render the commitment control outcome as primary;
            # incidental subscription offers remain followups.
            primary_override = str(commitment_turn.get("primary_response_override") or "").strip()
            commitment_status = str(commitment_turn.get("status") or "").strip()
            followups = self._commitment_followup_messages(commitment_turn)
            if primary_override and (
                self._result_execution_tier(result) == TIER_L5_PROACTIVE
                or commitment_status
                in {
                    "created",
                    "confirmed",
                    "declined",
                    "already_exists",
                    "state_answer",
                    "needs_disambiguation",
                    "not_found",
                    "cancelled",
                    "paused",
                    "resumed",
                    "semantic_change_proposed",
                    "semantic_change_recorded",
                    "semantic_change_confirmed",
                    "semantic_change_declined",
                    "multi_state_change",
                    "state_change_failed",
                    "state_change_indeterminate",
                    "state_change_stale_revision",
                    "state_change_expired",
                }
            ):
                result.primary_response = primary_override
                result.artifacts["commitment_primary_applied"] = primary_override
                followups = [message for message in followups if message != primary_override]
            elif commitment_status == "needs_parameters" and followups:
                result.primary_response = followups[0]
                result.artifacts["commitment_primary_applied"] = followups[0]
                followups = followups[1:]
            elif primary_override:
                result.artifacts["commitment_primary_deferred_to_followup"] = primary_override
            if followups:
                existing = list(result.followup_messages or [])
                result.followup_messages = existing + [message for message in followups if message not in existing]
                result.artifacts["followup_messages"] = result.followup_messages
            route_trace.append({"phase": "commitment", "status": commitment_turn.get("status"), "actions": commitment_turn.get("actions", [])})
        profile_state_change = self._sync_user_awareness_from_text(event, result)
        if profile_state_change:
            result.artifacts["profile_state_change"] = profile_state_change
            route_trace.append(
                {
                    "phase": "profile_state_change",
                    "status": profile_state_change.get("status"),
                }
            )
        self._apply_state_change_response_truth(result)
        self._persist_conversation_slots(event, result)
        self._render_final_user_messages(event, result)
        trace = self.runtime_trace.record(
            event=event,
            result=result,
            started_at=started_at,
            route_trace=route_trace,
            context_metrics=context_metrics,
        )
        result.artifacts["runtime_trace"] = {
            "trace_id": trace["trace_id"],
            "latency_ms": trace["latency_ms"],
            "final_route": trace["final_route"],
        }
        try:
            shadow_entry = next(
                (
                    entry
                    for entry in reversed(route_trace)
                    if entry.get("phase") == "event_fabric_shadow"
                ),
                {},
            )
            if shadow_entry.get("finalize_allowed"):
                self.event_awareness.finalize(
                    event,
                    result,
                    trace,
                    situation_id=(
                        str(shadow_entry.get("situation_id") or "") or None
                    ),
                    canonical_event_id=(
                        str(shadow_entry.get("event_id") or "") or None
                    ),
                )
        except Exception:
            # Event/situation projection is observational. It must never make an
            # otherwise completed user turn fail.
            pass
        self._update(event, result)
        return result

    def publish_event(self, event: VeyraEvent) -> dict[str, Any]:
        """Durably admit an internal or external event without granting authority."""

        return self.event_awareness.publish(event)

    def process_event_inbox(self, *, limit: int = 20) -> dict[str, Any]:
        """Project pending events into situations, never actions."""

        return self.event_awareness.process_pending(limit=limit)

    def _result_execution_tier(self, result: LoopResult) -> str:
        direct_tier = str(result.artifacts.get("execution_tier") or "").strip()
        if direct_tier:
            return direct_tier
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        assist = decision.get("model_assist") if isinstance(decision.get("model_assist"), dict) else {}
        plan = assist.get("decision_plan") if isinstance(assist.get("decision_plan"), dict) else {}
        return str(plan.get("execution_tier") or assist.get("execution_tier") or "").strip()

    def _early_high_risk_result(self, event: VeyraEvent, text: str) -> LoopResult | None:
        assessment = assess_text_risk(text)
        try:
            risk_level = RiskLevel(assessment.risk_level)
        except ValueError:
            return None
        if risk_level not in {RiskLevel.R4, RiskLevel.R5}:
            return None

        route = Route.BLOCK if risk_level == RiskLevel.R5 else Route.HUMAN_REVIEW
        decision = Decision(
            route=route,
            risk_level=risk_level,
            reason=assessment.reason,
            requires_confirmation=risk_level != RiskLevel.R5,
            intent="action",
            complexity="moderate",
            capability="guardian" if risk_level == RiskLevel.R5 else "human_review",
            needs_user_confirmation=risk_level == RiskLevel.R4,
            reasoning_mode="execution",
            required_capabilities=["guardian"] if risk_level == RiskLevel.R5 else ["human_review"],
            signals=list(dict.fromkeys(["early_risk_gate", *assessment.signals])),
            constraints=["do not call model before safety floor", "request explicit confirmation" if risk_level == RiskLevel.R4 else "block unsafe action"],
        )
        foresight = ForesightEngine().predict_text_action(text, risk_level, decision=decision.to_dict())
        guardian_decision = self.guardian.review_text_action(text=text, decision=decision, foresight=foresight)
        guardian_decision["risk_assessment"] = assessment.to_dict()
        artifacts = {
            "decision": decision.to_dict(),
            "guardian": guardian_decision,
            "foresight": foresight,
            "risk_assessment": assessment.to_dict(),
            "safety_gate": {
                "phase": "pre_model",
                "reason": "R4/R5 risk floor is applied before model-assisted understanding",
            },
        }
        if guardian_decision["decision"] == GuardianDecision.BLOCK.value or risk_level == RiskLevel.R5:
            self.runtime_entity.set_status(LifecycleStatus.BLOCKED.value)
            return LoopResult(
                event_id=event.event_id,
                route=Route.BLOCK,
                status="blocked",
                response=str(guardian_decision.get("reason") or "该动作被安全策略阻断。"),
                risk_level=risk_level,
                artifacts=artifacts,
            )

        self.runtime_entity.set_status(LifecycleStatus.WAITING_CONFIRMATION.value)
        review = self.review_queue.create(
            event_id=event.event_id,
            task_text=text,
            risk_level=risk_level.value,
            foresight=foresight,
            guardian_decision=guardian_decision,
            proposal={
                "agent": "veyra_core",
                "action": {"type": "natural_language_action", "text": text},
                "risk_guess": risk_level.value,
                "reversible": str(foresight.get("reversible") or "unknown"),
                "reason": assessment.reason,
            },
        )
        artifacts["review"] = review
        return LoopResult(
            event_id=event.event_id,
            route=Route.HUMAN_REVIEW,
            status="needs_confirmation",
            response="该动作需要用户确认后才能执行。",
            risk_level=risk_level,
            artifacts=artifacts,
        )

    def _confirmation_proposal(self, text: str, decision: Decision) -> dict[str, object] | None:
        if decision.route != Route.ROLLBACK:
            return None
        snapshot_id = self._snapshot_id_from_text(text)
        if not snapshot_id:
            return None
        return {
            "agent": "veyra_core",
            "action": {"type": "rollback_restore", "snapshot_id": snapshot_id},
            "risk_guess": RiskLevel.R4.value,
            "reversible": "yes",
            "reason": "Restore an existing snapshot through guarded rollback flow.",
        }

    def _run_rollback_request(
        self,
        event: VeyraEvent,
        decision: Decision,
        guardian_decision: dict[str, object],
        foresight: dict[str, object],
    ) -> LoopResult:
        snapshot_id = self._snapshot_id_from_text(str(event.payload.get("text", "")))
        return LoopResult(
            event_id=event.event_id,
            route=Route.ROLLBACK,
            status="needs_confirmation" if snapshot_id else "missing_snapshot_id",
            response=(
                "Rollback restore requires review approval before execution."
                if snapshot_id
                else "请提供要恢复的 snapshot_id，例如 snap_xxxxxxxxxxxx。"
            ),
            risk_level=decision.risk_level,
            artifacts={
                "decision": decision.to_dict(),
                "guardian": guardian_decision,
                "foresight": foresight,
                "snapshot_id": snapshot_id,
            },
        )

    def _snapshot_id_from_text(self, text: str) -> str | None:
        match = re.search(r"\bsnap_[a-fA-F0-9]{6,32}\b", text)
        return match.group(0) if match else None

    def _foresight_for_decision(self, text: str, decision: Decision) -> dict[str, object]:
        if not self._foresight_required(decision):
            return {
                "status": "skipped",
                "reason": "foresight_not_required_for_non_execution_turn",
                "risk_level": decision.risk_level.value,
                "route": decision.route.value,
            }
        return self.foresight.predict_text_action(text, decision.risk_level, decision=decision.to_dict())

    def _foresight_required(self, decision: Decision) -> bool:
        if decision.risk_level in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            return True
        if decision.reasoning_mode == "execution":
            return True
        return decision.route in {Route.AGENT, Route.NATIVE_TOOL, Route.HUMAN_REVIEW, Route.ROLLBACK}

    def _ask_user_response(self, decision: Decision) -> str:
        semantic_policy = self._semantic_policy_from_decision(decision)
        clarification = str(semantic_policy.get("clarification_reason") or "").strip()
        if bool(semantic_policy.get("requires_clarification")) and clarification:
            return self._humanize_semantic_clarification(clarification)
        capability = decision.capability_request.get("capability") if isinstance(decision.capability_request, dict) else ""
        message_type = decision.capability_request.get("message_type") if isinstance(decision.capability_request, dict) else ""
        if capability == "vision":
            attachment_label = "图片" if str(message_type or "").lower() in {"image", "img"} else "附件"
            return f"我已收到{attachment_label}，但当前没有启用可用的图片理解能力。我不会假装看过内容；请启用 Agent vision 能力，或补充图片文字描述。"
        draft = str(decision.model_assist.get("draft_response") or "").strip()
        if draft:
            return draft
        missing = decision.model_assist.get("context_gaps") if isinstance(decision.model_assist.get("context_gaps"), list) else []
        missing_text = "；".join(str(item) for item in missing[:3] if item)
        if missing_text:
            return f"我还需要补充信息：{missing_text}"
        if capability:
            return f"这个请求需要当前不可用的能力 `{capability}`。我不会猜测结果；请补充可验证信息或启用相应能力。"
        return "我需要更多上下文才能可靠处理这个请求。"

    @staticmethod
    def _humanize_semantic_clarification(reason: str) -> str:
        normalized = reason.strip().rstrip("。")
        messages = {
            "external writes need a tool proxy that can enforce the semantic effect scope": (
                "这个请求会改动外部系统。请明确你是只要我起草内容，还是要实际发送或提交；"
                "在执行边界可验证前，我不会直接写入外部系统。"
            ),
            "non-local Agent execution needs effect-scoped tool enforcement": (
                "这个请求涉及外部对象或服务。请补充目标和所需内容，并说明是只要草稿/方案，"
                "还是要实际执行；在执行边界可验证前，我不会直接操作。"
            ),
            "the referenced target is unresolved": "你说的对象目前不唯一。请告诉我具体是哪一个，我再继续处理。",
            "state-changing request is not explicit enough to authorize": (
                "这句话可能会改变状态，但授权意图还不够明确。请直接说明要改什么、改成什么。"
            ),
            "conditional request needs a satisfied trigger before capability use": (
                "这个动作带有尚未满足或无法验证的条件。请补充条件如何判断，以及满足后是否允许执行。"
            ),
            "conflicting state-changing acts need clarification": (
                "这句话里包含互相冲突的操作。请明确最终要保留哪一个动作。"
            ),
            "semantic output remained invalid after one bounded repair": (
                "我还不能可靠确定你要我回答问题，还是执行操作。请换一种说法，并明确期望结果。"
            ),
            "semantic resolution is degraded; no side effect is authorized": (
                "我理解到了大意，但还不足以安全执行操作。请明确目标、对象和期望结果。"
            ),
            "degraded semantic resolution lacks a high-confidence positive read request": (
                "我还不能确定你是在要求查询，还是只是在提及相关内容。请直接说明要查什么。"
            ),
            "speaker and authority fields conflict": (
                "这句话里的发言者和授权来源不一致。请由当前用户直接确认是否要执行。"
            ),
        }
        return messages.get(normalized, f"我还需要先确认：{normalized}")

    def _run_skill(self, event: VeyraEvent, skill_name: str) -> LoopResult:
        skill = self.skill_loader.load(skill_name)
        raw = self.skill_runtime.run(skill, {"text": event.payload.get("text", ""), "event": event.to_dict()})
        status = str(raw.get("status", "unknown"))
        risk = RiskLevel.R2 if skill_name == "safe_git_commit" else RiskLevel.R1
        response = str(raw.get("summary") or raw.get("status") or f"Skill {skill_name} completed.")
        if status == "needs_confirmation":
            review = self.review_queue.create(
                event_id=event.event_id,
                task_text=str(event.payload.get("text", "")),
                risk_level=RiskLevel.R2.value,
                foresight={"risk_level": "R2", "reversible": "partial", "side_effects": ["git history change"], "safer_alternatives": ["review git diff first"]},
                guardian_decision={"decision": "ask_user", "risk_level": "R2", "reason": "Skill requires confirmation before write action."},
                proposal={"agent": "skill", "action": {"type": "skill", "name": skill_name}, "risk_guess": "R2"},
            )
            result = LoopResult(
                event_id=event.event_id,
                route=Route.HUMAN_REVIEW,
                status="needs_confirmation",
                response=response,
                risk_level=RiskLevel.R2,
                artifacts={"skill_result": raw, "review": review},
            )
            result.artifacts["execution_trace"] = self.execution_trace.record(
                {
                    "event_id": event.event_id,
                    "route": result.route.value,
                    "task_id": event.event_id,
                    "executor": f"skill:{skill_name}",
                    "status": result.status,
                    "execution_result": raw,
                    "verification": {"status": "partially_success", "verdict": "skill_waiting_for_confirmation"},
                }
            )
            return result
        execution = ExecutionResult(
            task_id=event.event_id,
            executor=f"skill:{skill_name}",
            status="success" if status == "success" else status,
            result=response,
            raw=raw,
        )
        verified = self.verifier.verify_execution_result(execution)
        result_status = "success" if verified["status"] == "verified_success" else verified["status"]
        artifacts = {"skill_result": raw, "verification": verified}
        artifacts["execution_trace"] = self.execution_trace.record(
            {
                "event_id": event.event_id,
                "route": Route.SKILL.value,
                "task_id": event.event_id,
                "executor": f"skill:{skill_name}",
                "status": verified["status"],
                "execution_result": asdict(execution),
                "verification": verified,
            }
        )
        return LoopResult(
            event_id=event.event_id,
            route=Route.SKILL,
            status=result_status,
            response=response,
            risk_level=risk,
            artifacts=artifacts,
        )

    def _run_probe(self, event: VeyraEvent, decision: Decision, attention_focus: list[str]) -> LoopResult:
        policy = self._semantic_policy_from_decision(decision)
        requests = [
            item
            for item in (
                policy.get("probe_requests")
                if isinstance(policy.get("probe_requests"), list)
                else []
            )
            if isinstance(item, dict) and str(item.get("probe") or "").strip()
        ]
        if len(requests) <= 1:
            return self._run_single_probe(event, decision, attention_focus)

        results: list[LoopResult] = []
        result_records: list[dict[str, Any]] = []
        for request in requests:
            probe_name = str(request.get("probe") or "").strip()
            arguments = (
                request.get("arguments")
                if isinstance(request.get("arguments"), dict)
                else {}
            )
            scoped_policy = {
                **policy,
                "selected_probe": probe_name,
                "capability_arguments": dict(arguments),
                "probe_requests": [request],
            }
            scoped_assist = {
                **(decision.model_assist if isinstance(decision.model_assist, dict) else {}),
                "semantic_policy": scoped_policy,
                "probe_params": dict(arguments),
            }
            scoped_decision = replace(
                decision,
                selected_probe=probe_name,
                model_assist=scoped_assist,
            )
            item_result = self._run_single_probe(
                event,
                scoped_decision,
                attention_focus,
            )
            results.append(item_result)
            result_records.append(
                {
                    "act_id": str(request.get("act_id") or ""),
                    "goal": str(request.get("goal") or ""),
                    "probe": probe_name,
                    "status": item_result.status,
                    "response": item_result.response,
                    "probe_result": item_result.artifacts.get("probe_result"),
                    "verification": item_result.artifacts.get("verification"),
                    "state_patch": item_result.artifacts.get("state_patch"),
                    "execution_trace": item_result.artifacts.get("execution_trace"),
                }
            )

        successful = all(
            item.status in {"success", "verified_success", "partially_success"}
            for item in results
        )
        assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        frame = assist.get("semantic_frame") if isinstance(assist.get("semantic_frame"), dict) else {}
        source_labels: dict[str, str] = {}
        for act in frame.get("acts", []) if isinstance(frame.get("acts"), list) else []:
            if not isinstance(act, dict):
                continue
            quote = act.get("source_quote") if isinstance(act.get("source_quote"), dict) else {}
            label = str(quote.get("text") or "").strip().rstrip("。.!！")
            act_id = str(act.get("act_id") or "").strip()
            if act_id and label:
                source_labels[act_id] = label
        response_parts = []
        for request, item_result in zip(requests, results, strict=True):
            act_id = str(request.get("act_id") or "").strip()
            label = str(
                source_labels.get(act_id)
                or request.get("goal")
                or request.get("probe")
                or "查询结果"
            ).strip()
            response_parts.append(f"{label}：{item_result.response}")
        aggregate_probe_result = {
            "probe": "multi_probe",
            "status": "success" if successful else "partial",
            "results": [
                {
                    "act_id": item.get("act_id"),
                    "probe": item.get("probe"),
                    "status": item.get("status"),
                    "result": item.get("probe_result"),
                }
                for item in result_records
            ],
        }
        traces = [
            item.get("execution_trace")
            for item in result_records
            if isinstance(item.get("execution_trace"), dict)
        ]
        return LoopResult(
            event_id=event.event_id,
            route=Route.PROBE,
            status="verified_success" if successful else "partial",
            response="\n\n".join(response_parts),
            risk_level=RiskLevel.R1,
            artifacts={
                "probe_result": aggregate_probe_result,
                "probe_results": result_records,
                "verification": {
                    "status": "verified_success" if successful else "partial",
                    "count": len(result_records),
                },
                "decision": decision.to_dict(),
                "execution_trace": traces[0] if traces else {},
                "execution_traces": traces,
            },
        )

    def _run_single_probe(self, event: VeyraEvent, decision: Decision, attention_focus: list[str]) -> LoopResult:
        text = event.payload.get("text", "")
        probe_name = (decision.selected_probe or "").strip()
        if not probe_name:
            return LoopResult(
                event_id=event.event_id,
                route=Route.ASK_USER,
                status="needs_user_input",
                response="这个请求需要明确且可执行的探针名称；我不会在缺少探针时默认执行 system_probe。",
                risk_level=RiskLevel.R1,
                artifacts={"decision": decision.to_dict(), "next_action": "provide a concrete probe target"},
            )
        probe = self.probes.get(probe_name)
        if not probe:
            return LoopResult(
                event_id=event.event_id,
                route=Route.ASK_USER,
                status="needs_user_input",
                response=f"当前不支持探针 `{probe_name}`。请改用可执行探针后再试。",
                risk_level=RiskLevel.R1,
                artifacts={"decision": decision.to_dict(), "next_action": "use a supported probe"},
            )
        raw = self._invoke_probe(probe_name=probe_name, probe=probe, text=text, decision=decision, event=event)
        state_patch = self.perception.interpret_probe_result(raw)
        belief_state = self.belief.refresh()
        verified = self.verifier.verify_probe_result(raw)
        answer_assist = (
            self.core_reasoning.probe_answer_assist(
                text=text,
                attention_focus=attention_focus,
                probe_result=raw,
                decision=decision.to_dict(),
                event=event,
            )
            if self._probe_needs_answer_model(probe_name, raw)
            else {"status": "skipped", "reason": "deterministic_probe_response"}
        )
        trace = self.execution_trace.record(
            {
                "event_id": event.event_id,
                "route": Route.PROBE.value,
                "task_id": event.event_id,
                "executor": f"probe:{probe_name}",
                "status": verified["status"],
                "execution_result": raw,
                "verification": verified,
                "decision": decision.to_dict(),
            }
        )
        return LoopResult(
            event_id=event.event_id,
            route=Route.PROBE,
            status=verified["status"],
            response=self._probe_response(probe_name=probe_name, raw=raw, verified=verified, answer_assist=answer_assist),
            risk_level=RiskLevel.R1,
            artifacts={
                "probe_result": raw,
                "state_patch": state_patch,
                "verification": verified,
                "decision": decision.to_dict(),
                "answer_assist": answer_assist,
                "execution_trace": trace,
                "uncertainty": self.uncertainty.uncertainty_summary(belief_state.get("claims", [])),
            },
        )

    def _invoke_probe(self, *, probe_name: str, probe: Any, text: str, decision: Decision, event: VeyraEvent) -> dict[str, Any]:
        model_assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        semantic_policy = self._semantic_policy_from_decision(decision)
        semantic_params = (
            semantic_policy.get("capability_arguments")
            if isinstance(semantic_policy.get("capability_arguments"), dict)
            else {}
        )
        params = semantic_params or (
            model_assist.get("probe_params") if isinstance(model_assist.get("probe_params"), dict) else {}
        )
        if probe_name == "weather_probe":
            location = str(params.get("location") or params.get("place") or params.get("city") or "").strip()
            if not location:
                slots = self._conversation_slots_for_event(event)
                if semantic_policy:
                    location = str(slots.get("last_location") or "").strip()
                else:
                    location = self._resolve_weather_location_for_turn(text, slots)
            return probe.run(text, location=location or None)
        if probe_name == "search_probe":
            query = str(params.get("query") or params.get("search_query") or "").strip()
            if not query and semantic_policy:
                goals = semantic_policy.get("canonical_goals") if isinstance(semantic_policy.get("canonical_goals"), list) else []
                query = str(goals[0] if goals else "").strip()
            if not query:
                query = self._search_query_from_text(text)
            max_results = params.get("max_results")
            try:
                limit = int(max_results) if max_results is not None else 5
            except (TypeError, ValueError):
                limit = 5
            return probe.run(query, max_results=max(1, min(limit, 10)))
        if probe_name == "web":
            url = str(params.get("url") or "").strip()
            if url and hasattr(probe, "run"):
                try:
                    return probe.run(url)
                except TypeError:
                    pass
        if probe_name == "port":
            port = params.get("port")
            if port is not None and hasattr(probe, "run"):
                try:
                    return probe.run(port=int(port))
                except TypeError:
                    pass
        return probe.run(text)

    def _probe_response(self, *, probe_name: str, raw: dict[str, object], verified: dict[str, object], answer_assist: dict[str, object]) -> str:
        draft = str(answer_assist.get("draft_response") or answer_assist.get("response") or "").strip()
        if answer_assist.get("status") == "model_assisted" and draft and self._probe_draft_is_usable(probe_name=probe_name, raw=raw, draft=draft):
            return draft
        if probe_name == "search_probe":
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            results = details.get("results") if isinstance(details.get("results"), list) else []
            generic_titles = {"here", "result", "search result", "source: web search", "openclaw web search summary"}
            titles = [
                str(item.get("title") or "").strip()
                for item in results[:3]
                if isinstance(item, dict)
                and item.get("title")
                and str(item.get("title") or "").strip().lower() not in generic_titles
            ]
            if titles:
                return f"我拿到了 {len(results)} 条搜索结果，靠前结果包括：" + "；".join(titles)
            if results:
                return "我拿到了搜索结果，但结果标题信息不够清晰，不能可靠确认最新视频标题。"
        return str(raw.get("summary") or verified.get("message") or "Probe completed.")

    def _probe_needs_answer_model(self, probe_name: str, raw: dict[str, object]) -> bool:
        if probe_name == "search_probe":
            return False
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        return bool(details.get("sample") or details.get("results") or details.get("content") or probe_name in {"web", "log", "file"})

    def _direct_answer(self, event: VeyraEvent, decision: Decision, attention_focus: list[str], *, persona_patch: dict[str, Any]) -> str:
        text = str(event.payload.get("text", ""))
        if decision.intent == "identity" or "source:governance_identity" in decision.signals:
            return "我是 Veyra。OpenClaw 是我可以在需要执行复杂任务时治理和调用的 Agent Runtime，不是当前对话身份。"
        if (decision.intent == "preference" or "memory:preference" in decision.signals) and self._semantic_effect_allowed(
            decision, "memory.write"
        ):
            return "记住了。之后我会尽量更直接，除非问题本身需要先说明风险、证据或执行边界。"
        if self._is_tracking_memory_question(text):
            return self._tracking_memory_response(event)
        if self._is_project_continuation_question(text):
            project_response = self._project_context_response(event)
            if project_response:
                return project_response
        contextual_plan = self._user_context_planning_response(text, event)
        if contextual_plan:
            return contextual_plan
        if self._semantic_effect_allowed(decision, "profile.write"):
            state_ack = self._state_update_ack_response(text)
            if state_ack:
                return state_ack
        if self._semantic_effect_allowed(decision, "proactive.create") and self._is_proactive_weather_request(text):
            return "可以，你要我每天几点发哪个城市/地区的天气？"
        if self._is_learning_memory_question(text):
            topic = self._current_learning_topic(event.source.user_id)
            if topic:
                return f"记得。你现在在学习「{topic}」。我会把它作为当前学习目标来组织后续建议。"
            return "我现在没有找到明确的学习目标记录；你可以直接告诉我要学习的主题，我会记录到 Veyra memory。"
        project_identity = self._project_identity_direct_response(text)
        if project_identity:
            return project_identity
        understanding = decision.model_assist.get("turn_understanding") if isinstance(decision.model_assist, dict) else {}
        if isinstance(understanding, dict) and str(understanding.get("source") or "") == "rule_fallback":
            if str(understanding.get("suggested_mode") or "") in {
                "strategic_discussion",
                "meta_cognition_discussion",
                "project_direction_review",
            }:
                return self._direct_answer_degraded(text=text, decision=decision, answer_assist={})
        model_assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        plan_draft = str(model_assist.get("draft_response") or "").strip()
        # Speed: the awareness execution-plan step already drafted a final reply for
        # direct answers. Reuse it instead of making a third sequential model call,
        # and only fall back to a dedicated answer pass when that draft is unusable.
        if plan_draft:
            plan_answer = {
                "status": "model_assisted",
                "draft_response": plan_draft,
                "confidence": model_assist.get("confidence"),
                "source": "execution_plan_draft",
            }
            if self._direct_draft_is_usable(text=text, decision=decision, draft=plan_draft, answer_assist=plan_answer):
                decision.model_assist = {**decision.model_assist, "answer_assist": plan_answer}
                return plan_draft
        answer_assist = self.core_reasoning.answer_assist(
            text=text,
            attention_focus=attention_focus,
            decision=decision.to_dict(),
            event=event,
            persona_patch=persona_patch,
        )
        draft = str(answer_assist.get("draft_response") or answer_assist.get("response") or "").strip()
        if not draft and plan_draft:
            draft = plan_draft
            answer_assist = {**answer_assist, "status": "model_assisted", "draft_response": draft}
        if answer_assist.get("status") == "model_assisted" and draft and self._direct_draft_is_usable(text=text, decision=decision, draft=draft, answer_assist=answer_assist):
            decision.model_assist = {**decision.model_assist, "answer_assist": answer_assist}
            return draft
        if decision.freshness_required:
            capability = decision.capability_request.get("capability") if isinstance(decision.capability_request, dict) else ""
            return f"这个问题需要先获取新鲜证据{f'（{capability}）' if capability else ''}，我不会凭模板猜测。"
        return self._direct_answer_degraded(text=text, decision=decision, answer_assist=answer_assist)

    def _probe_draft_is_usable(self, *, probe_name: str, raw: dict[str, object], draft: str) -> bool:
        if self._looks_like_low_quality_template(draft):
            return False
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            return True
        anchors = self._probe_evidence_anchors(probe_name=probe_name, raw=raw, summary=summary)
        if not anchors:
            return True
        lowered = draft.lower()
        return any(anchor in lowered for anchor in anchors)

    def _probe_evidence_anchors(self, *, probe_name: str, raw: dict[str, object], summary: str) -> list[str]:
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        anchors: list[str] = []
        if probe_name == "time_probe":
            anchors.extend(
                [
                    str(details.get("timezone") or "").strip().lower(),
                    str(details.get("date") or "").strip().lower(),
                    str(details.get("time") or "").strip().lower(),
                    str(details.get("utc_offset") or "").strip().lower(),
                ]
            )
        elif probe_name == "weather_probe":
            current = details.get("current") if isinstance(details.get("current"), dict) else {}
            anchors.extend(
                [
                    str(details.get("location") or "").strip().lower(),
                    str(details.get("weather_description") or "").strip().lower(),
                    str(current.get("temperature_2m") or "").strip().lower(),
                    str(current.get("time") or "").strip().lower(),
                ]
            )
        elif probe_name == "web_probe":
            anchors.extend(
                [
                    str(details.get("url") or "").strip().lower(),
                    str(details.get("status_code") or "").strip().lower(),
                ]
            )
        elif probe_name in {"port_probe", "openclaw_probe", "hermes_probe"}:
            anchors.extend(
                [
                    str(raw.get("status") or "").strip().lower(),
                    str(raw.get("host") or details.get("host") or "").strip().lower(),
                    str(raw.get("port") or details.get("port") or "").strip().lower(),
                ]
            )
        anchors.extend(token.lower() for token in re.findall(r"[A-Za-z0-9_:/+.-]{3,}", summary))
        return [item for item in anchors if item]

    def _direct_draft_is_usable(self, *, text: str, decision: Decision, draft: str, answer_assist: dict[str, object]) -> bool:
        if self._looks_like_low_quality_template(draft):
            return False
        confidence_raw = answer_assist.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and confidence < 0.45:
            return False
        if (
            bool(answer_assist.get("needs_observation"))
            and not decision.freshness_required
            and not self._plain_explanation_question(text)
        ):
            return False
        lowered_question = text.lower()
        lowered_draft = draft.lower()
        if not decision.needs_probe and any(
            marker in draft for marker in ("正在检查", "正在获取", "我会先检查", "我将检查", "正在搜索", "正在为您搜索", "我来搜索", "请稍等")
        ):
            return False
        if decision.intent in {"information", "unknown"} and any(marker in lowered_question for marker in ("为什么", "是什么", "解释", "说明", "how", "what", "why")):
            if len(draft) < 24:
                return False
        meta_context = any(marker in lowered_question for marker in ("架构", "认知", "prompt", "提示词", "智障", "architecture", "cognition"))
        if "openclaw" in lowered_draft and "openclaw" not in lowered_question and decision.intent != "identity" and not meta_context:
            return False
        if "hermes" in lowered_draft and "hermes" not in lowered_question and decision.intent != "identity" and not meta_context:
            return False
        return True

    def _looks_like_low_quality_template(self, text: str) -> bool:
        lowered = text.strip().lower()
        if len(lowered) < 4:
            return True
        markers = (
            "无法稳定访问认知模型",
            "我不能保证准确",
            "可能不太准确",
            "作为ai",
            "无法访问互联网",
            "请稍后再试",
            "我现在无法",
            "你想问哪个对象、现象或决定",
        )
        return any(marker in lowered for marker in markers)

    def _direct_answer_degraded(self, *, text: str, decision: Decision, answer_assist: dict[str, object]) -> str:
        compact = " ".join(text.split())
        snippet = compact[:48] + ("..." if len(compact) > 48 else "")
        understanding = decision.model_assist.get("turn_understanding") if isinstance(decision.model_assist, dict) else {}
        if isinstance(understanding, dict) and str(understanding.get("suggested_mode") or "") in {
            "strategic_discussion",
            "meta_cognition_discussion",
            "project_direction_review",
        }:
            project = str(understanding.get("project") or "Veyra")
            hidden = str(understanding.get("hidden_need") or understanding.get("what_user_really_needs") or "项目方向验证")
            return (
                f"这不是一个需要先调 probe 或下发 Agent 的问题，而是「{project}」的方向问题。"
                f"我理解你的核心需求是：{hidden}。应该先把用户到底要什么、当前项目风险、可验证的价值闭环拆清楚，"
                "再决定是否需要运行态证据或执行任务；否则 Veyra 会继续在理解之前路由。"
            )
        if self._is_meta_cognition_question(text):
            return (
                "问题主要不在架构名词，而在认知链路被压成了机械路由：先急着选 direct/probe/agent，"
                "再套回答模板，导致它没有先判断用户真实目标、当前状态是否新鲜、缺什么证据、该不该让 Agent 做更深分析。"
                "应该把入口改成 situation assessment，再决定证据、Agent 提案或直答。"
            )
        casual = self._casual_direct_fallback(text)
        if casual:
            return casual
        gaps = answer_assist.get("context_gaps") if isinstance(answer_assist.get("context_gaps"), list) else []
        if gaps:
            gap_text = "；".join(str(item) for item in gaps[:2] if item)
            if gap_text:
                if self._direct_degraded_needs_evidence(decision):
                    return f"我现在缺少可靠信息：{gap_text}。请补充目标或约束后我再回答。"
                return f"我还需要一点上下文：{gap_text}。你补一句具体对象，我就能接着回答。"
        if self._direct_degraded_needs_evidence(decision):
            return f"这个问题需要先获取当前状态或新鲜证据，不能只凭旧上下文判断「{snippet}」。"
        if self._bare_clarification_question(text):
            return "你想问哪件事的原因？补一句具体对象，我就能直接解释。"
        if decision.intent in {"information", "unknown"}:
            if self._plain_explanation_question(text):
                return (
                    "这是一个普通解释问题，不需要当前状态或外部证据，也不是要你补“对象、现象或决定”。"
                    f"但当前核心模型请求失败，我暂时不能给出可靠完整解释；模型恢复后可以直接回答「{snippet}」。"
                )
            return "我需要你补一点具体上下文：你想问哪个对象、现象或决定？"
        return "我在。你可以直接说要继续哪个问题。"

    def _plain_explanation_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        if self._bare_clarification_question(text):
            return False
        explanation_markers = ("为什么", "为何", "为啥", "是什么", "怎么", "如何", "解释", "说明", "why", "what", "how")
        volatile_markers = ("现在", "当前", "今天", "最新", "状态", "还在", "有没有", "current", "latest", "status", "now")
        return any(marker in compact for marker in explanation_markers) and not any(marker in compact for marker in volatile_markers)

    def _project_identity_direct_response(self, text: str) -> str:
        lowered = (text or "").lower()
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if any(marker in compact for marker in ("你是谁", "你能做什么", "你现在能做什么", "现在可以用吗")):
            return (
                "我是 Veyra，一个 awareness + governance harness。"
                "我负责理解对话、维护世界状态、选择直答/probe/skill/Agent/阻断路径，并在执行前做风险和权限边界控制。"
            )
        if "Veyra和OpenClaw有什么区别" in compact or ("veyra" in lowered and "openclaw" in lowered and any(marker in compact for marker in ("区别", "不同"))):
            return (
                "Veyra 是控制层：理解用户意图、读取世界状态、做证据和风险判断，并决定是否调用执行体。"
                "OpenClaw 是可插拔 Agent Runtime：只有当任务需要代码、调试或多步执行时，Veyra 才会按策略把 TaskPacket 下发给它。"
            )
        if compact in {"Veyra是什么", "Veyra是啥"} or ("veyra" in lowered and any(marker in compact for marker in ("是什么", "是啥"))):
            return (
                "Veyra 不是普通聊天机器人，也不是单个 Agent。"
                "它更像一个带感知、世界状态和治理边界的 harness：先判断当前对话需要什么，再选择直答、只读 probe、skill、Agent 或安全阻断。"
            )
        return ""

    def _casual_direct_fallback(self, text: str) -> str:
        compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
        if not compact:
            return "我在。你可以直接说要我看什么。"
        greetings = {"hi", "hello", "hey", "哈喽", "你好", "嗨", "在吗", "在不在"}
        thanks = {"谢谢", "谢了", "thanks", "thankyou", "thx"}
        if compact in greetings:
            return "Hi，我在。"
        if compact in thanks:
            return "不客气。"
        return ""

    def _low_latency_short_response(self, text: str) -> dict[str, object]:
        compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
        if not compact:
            return {"reason": "empty_short_message", "response": "我在。你可以直接说要我看什么。"}
        casual = self._casual_direct_fallback(text)
        if casual:
            return {"reason": "casual_short_message", "response": casual}
        if compact in {"ok", "okay", "收到", "好的", "好", "嗯", "嗯嗯", "行", "可以"}:
            return {"reason": "ack_short_message", "response": "收到。"}
        if self._bare_clarification_question(text):
            return {
                "reason": "bare_clarification_question",
                "route": Route.ASK_USER.value,
                "status": "needs_user_input",
                "response": "你想问哪件事的原因？补一句具体对象，我就能直接解释。",
            }
        if compact in {"太慢了", "回应太慢了", "响应太慢了", "回得太慢了", "慢死了"} or (
            len(compact) <= 12 and "慢" in compact and any(marker in compact for marker in ("回", "响应", "回应"))
        ):
            return {
                "reason": "latency_complaint",
                "response": "确实慢。短消息我会直接轻量回复；需要查状态或执行任务时，我会先说明正在处理什么。",
            }
        return {}

    def _bare_clarification_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        return compact in {"为什么", "why", "为啥", "怎么了", "怎么回事", "what", "是什么"}

    def _direct_degraded_needs_evidence(self, decision: Decision) -> bool:
        if decision.freshness_required or decision.needs_probe:
            return True
        request = decision.capability_request if isinstance(decision.capability_request, dict) else {}
        evidence_kind = str(request.get("evidence_kind") or request.get("freshness_need") or "")
        capability = str(request.get("capability") or "")
        return evidence_kind in {"local", "runtime", "external", "file", "attachment", "weather", "time", "search", "web"} or capability in {
            "web_search",
            "weather_probe",
            "time_probe",
            "system_probe",
            "file_probe",
            "log_probe",
            "process_probe",
            "port_probe",
            "vision",
        }

    def _state_update_ack_response(self, text: str) -> str:
        signals = self._extract_profile_signals(text)
        program = signals.get("program") if isinstance(signals.get("program"), dict) else None
        if program and program.get("name"):
            direction = str(program.get("direction") or "").strip()
            tail = f"，方向是 {direction}" if direction else ""
            return f"已记录：你准备参加 {program['name']}{tail}。后续学习路线和项目建议会默认带上这个背景。"
        if signals.get("education"):
            return f"已记录：你是{signals['education']}。后续项目建议会优先按你的阶段和作品集产出考虑。"
        if signals.get("project"):
            return f"已记录：当前项目是 {signals['project']}。后续说“继续”或“昨天那个项目”时，我会优先恢复这个上下文。"
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if all(marker in compact for marker in ("Veyra架构", "学校作业", "Docker部署")):
            return "已更新当前注意力：优先继续 Docker 部署，同时保留 Veyra 架构和学校作业作为次级上下文。"
        return ""

    def _early_awareness_response(
        self,
        event: VeyraEvent,
        text: str,
        attention_focus: list[str],
        *,
        decision: Decision,
    ) -> dict[str, Any]:
        semantic_policy = self._semantic_policy_from_decision(decision)
        if not semantic_policy or bool(semantic_policy.get("requires_clarification")):
            return {}
        if str(semantic_policy.get("preferred_route") or "") != Route.DIRECT_ANSWER.value:
            return {}
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if compact in {"继续", "接着刚才"}:
            focus_text = " ".join(str(item) for item in attention_focus).lower()
            if any(marker in focus_text for marker in ("docker", "deploy", "deployment", "部署", "process", "port")):
                return {
                    "reason": "attention_continuation",
                    "response": "继续 Docker 部署上下文。我会优先围绕部署状态、进程、端口和服务可用性推进。",
                }
        if self._is_tracking_memory_question(text):
            return {"reason": "tracking_memory_question", "response": self._tracking_memory_response(event)}
        if self._is_learning_memory_question(text):
            topic = self._current_learning_topic(event.source.user_id)
            response = (
                f"记得。你现在在学习「{topic}」。我会把它作为当前学习目标来组织后续建议。"
                if topic
                else "我现在没有找到明确的学习目标记录；你可以直接告诉我要学习的主题，我会记录到 Veyra memory。"
            )
            return {"reason": "state_grounded_learning_memory", "response": response}
        state_answer = self._state_answer_for_turn(event, text)
        if state_answer:
            return state_answer
        # Effectful commitment, tracking, and learning requests deliberately do
        # not run in the early-response path. They are committed only after the
        # semantic frame has produced a typed, idempotent state proposal.
        project_identity = self._project_identity_direct_response(text)
        if project_identity:
            return {"reason": "project_identity_direct", "response": project_identity}
        if self._is_project_continuation_question(text):
            project_response = self._project_context_response(event)
            if project_response:
                return {"reason": "project_continuation", "response": project_response}
        contextual_plan = self._user_context_planning_response(text, event)
        if contextual_plan:
            return {"reason": "user_profile_context_plan", "response": contextual_plan}
        if self._semantic_effect_allowed(decision, "profile.write"):
            state_ack = self._state_update_ack_response(text)
            if state_ack:
                return {"reason": "state_update_ack", "response": state_ack}
        return {}

    def _semantic_result_allowed(self, decision: Decision, result: LoopResult) -> bool:
        policy = self._semantic_policy_from_decision(decision)
        if not policy or bool(policy.get("requires_clarification")):
            return False
        preferred = str(policy.get("preferred_route") or "")
        if result.route == Route.PROBE:
            if preferred != Route.PROBE.value:
                return False
            selected = str(policy.get("selected_probe") or "")
            probe_result = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
            actual = str(probe_result.get("probe") or "")
            return not selected or not actual or selected == actual
        return result.route == Route.DIRECT_ANSWER and preferred == Route.DIRECT_ANSWER.value

    def _semantic_policy_from_decision(self, decision: Decision | dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(decision, Decision):
            assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        elif isinstance(decision, dict):
            assist = decision.get("model_assist") if isinstance(decision.get("model_assist"), dict) else {}
        else:
            return {}
        policy = assist.get("semantic_policy")
        return policy if isinstance(policy, dict) else {}

    def _semantic_effect_allowed(self, decision: Decision | dict[str, Any] | None, effect: str) -> bool:
        policy = self._semantic_policy_from_decision(decision)
        if not policy:
            return False
        allowed = {str(item) for item in policy.get("allowed_effects", []) if isinstance(item, str)}
        denied = {str(item) for item in policy.get("denied_effects", []) if isinstance(item, str)}
        return effect in allowed and effect not in denied

    def _semantic_effect_context(
        self,
        decision: Decision | dict[str, Any] | None,
        effect: str,
        *,
        fallback_all: bool = False,
        authorized_act_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(decision, Decision):
            assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        elif isinstance(decision, dict):
            assist = decision.get("model_assist") if isinstance(decision.get("model_assist"), dict) else {}
        else:
            assist = {}
        policy = assist.get("semantic_policy") if isinstance(assist.get("semantic_policy"), dict) else {}
        frame = assist.get("semantic_frame") if isinstance(assist.get("semantic_frame"), dict) else {}
        authoritative = {
            str(item)
            for item in policy.get("authoritative_act_ids", [])
            if isinstance(item, str)
        }
        effect_bindings = (
            policy.get("effect_authorized_act_ids")
            if isinstance(policy.get("effect_authorized_act_ids"), dict)
            else None
        )
        effect_authoritative = (
            {
                str(item)
                for item in effect_bindings.get(effect, [])
                if isinstance(item, str)
            }
            if effect_bindings is not None
            else authoritative
        )
        all_acts = [
            item
            for item in (frame.get("acts") if isinstance(frame.get("acts"), list) else [])
            if isinstance(item, dict)
            and str(item.get("act_id") or "") in authoritative
            and str(item.get("act_id") or "") in effect_authoritative
            and (
                authorized_act_id is None
                or str(item.get("act_id") or "") == authorized_act_id
            )
        ]
        acts = [item for item in all_acts if self._act_matches_state_effect(item, effect)]
        if fallback_all and not acts:
            acts = all_acts
        acts = sorted(
            acts,
            key=lambda item: int(
                (item.get("source_quote") if isinstance(item.get("source_quote"), dict) else {}).get("start")
                or 0
            ),
        )
        act_ids = [str(item.get("act_id") or "") for item in acts if str(item.get("act_id") or "")]
        operations: list[str] = []
        source_parts: list[str] = []
        for act in acts:
            operation = str(act.get("operation") or "").strip()
            if operation and operation not in operations:
                operations.append(operation)
            quote = act.get("source_quote") if isinstance(act.get("source_quote"), dict) else {}
            quote_text = str(quote.get("text") or "").strip()
            if quote_text and quote_text not in source_parts:
                source_parts.append(quote_text)
        first = acts[0] if acts else {}
        target = first.get("target") if isinstance(first.get("target"), dict) else {}
        source_span = first.get("source_quote") if isinstance(first.get("source_quote"), dict) else None
        explicitness = str(first.get("explicitness") or "unknown")
        if explicitness not in {"explicit", "strong_implied", "weak_implied", "inferred", "unknown"}:
            explicitness = "unknown"
        attributes = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
        arguments = first.get("arguments") if isinstance(first.get("arguments"), dict) else {}
        scope = str(arguments.get("scope") or attributes.get("scope") or "current_user")
        relations = [
            relation
            for relation in (frame.get("relations") if isinstance(frame.get("relations"), list) else [])
            if isinstance(relation, dict)
            and (
                str(relation.get("from_act_id") or "") in act_ids
                or str(relation.get("to_act_id") or "") in act_ids
            )
        ]
        return {
            "effect": effect,
            "act_ids": act_ids,
            "acts": acts,
            "relations": relations,
            "operation": "+".join(operations) or "unknown",
            "target": {
                "type": str(target.get("type") or "unknown"),
                "value": str(target.get("value") or ""),
                "scope": scope,
                "attributes": attributes,
            },
            "source_span": source_span,
            "source_text": "，".join(source_parts),
            "explicitness": explicitness,
            "resolver_status": str(frame.get("resolver_status") or ""),
        }

    @staticmethod
    def _act_matches_state_effect(act: dict[str, Any], effect: str) -> bool:
        target = act.get("target") if isinstance(act.get("target"), dict) else {}
        haystack = " ".join(
            str(value or "")
            for value in (
                act.get("kind"),
                act.get("operation"),
                act.get("goal"),
                target.get("type"),
            )
        ).lower()
        if effect == "proactive.create":
            return any(
                marker in haystack
                for marker in (
                    "proactive",
                    "recurring",
                    "reminder",
                    "subscribe",
                    "subscription",
                    "track",
                    "monitor",
                    "提醒",
                    "订阅",
                    "关注",
                    "跟踪",
                    "追踪",
                    "推送",
                )
            )
        if effect == "commitment.mutate":
            if AwarenessLoop._act_matches_state_effect(act, "proactive.create"):
                return False
            return any(
                marker in haystack
                for marker in (
                    "commitment",
                    "goal_control",
                    "schedule_control",
                    "cancel",
                    "pause",
                    "resume",
                    "confirm",
                    "decline",
                    "取消",
                    "暂停",
                    "恢复",
                    "确认",
                    "拒绝",
                )
            )
        if effect in {"memory.write", "profile.write"}:
            return any(
                marker in haystack
                for marker in (
                    "preference",
                    "profile",
                    "self_disclosure",
                    "user_fact",
                    "偏好",
                    "回答风格",
                    "个人信息",
                )
            )
        return False

    def _early_learning_goal_response(self, event: VeyraEvent, text: str) -> dict[str, Any]:
        if not self.commitment_core:
            return {}
        result = self.commitment_core.process_explicit_learning_goal(user_text=text, event=event)
        if not result:
            return {}
        messages = self.commitment_core.followup_messages_for_turn(result)
        response = str(messages[0] if messages else result.get("primary_response_override") or "").strip()
        if not response:
            return {}
        return {
            "reason": "explicit_learning_goal",
            "response": response,
            "commitment_turn": result,
        }

    def _early_commitment_control_response(self, event: VeyraEvent, text: str) -> dict[str, Any]:
        if not self.commitment_core:
            return {}
        semantic = self.commitment_core.semantic_intent_for_turn(user_text=text, event=event)
        if semantic.get("operation") not in {"confirm", "decline", "cancel", "pause", "resume"}:
            return {}
        result = self.commitment_core.process_turn(
            event=event,
            user_text=text,
            assistant_response="",
            route=Route.DIRECT_ANSWER.value,
            status="success",
        )
        if not result:
            return {}
        messages = self.commitment_core.followup_messages_for_turn(result)
        response = str(result.get("primary_response_override") or (messages[0] if messages else "")).strip()
        if not response:
            return {}
        return {
            "reason": "commitment_semantic_control",
            "response": response,
            "commitment_turn": result,
        }

    def _early_tracking_request_response(self, event: VeyraEvent, text: str) -> dict[str, Any]:
        if not self.commitment_core:
            return {}
        result = self.commitment_core.process_explicit_tracking_request(user_text=text, event=event)
        if not result:
            return {}
        intent = result.get("intent") if isinstance(result.get("intent"), dict) else {}
        topic = str(intent.get("topic") or "这个主题").strip()
        return {
            "reason": "explicit_tracking_request",
            "response": f"已理解：你希望持续关注「{topic}」的更新。",
            "commitment_turn": result,
        }

    def _early_semantic_change_response(self, event: VeyraEvent, text: str) -> dict[str, Any]:
        if not self.commitment_core:
            return {}
        result = self.commitment_core.propose_semantic_change_for_turn(user_text=text, event=event)
        if not result:
            return {}
        response = str(result.get("primary_response_override") or "").strip()
        if not response:
            followups = result.get("followup_messages") if isinstance(result.get("followup_messages"), list) else []
            response = str(followups[0] if followups else "").strip()
        if not response:
            return {}
        return {
            "reason": "semantic_change_requires_confirmation",
            "response": response,
            "commitment_turn": result,
        }

    def _event_log_record(self, event: VeyraEvent) -> dict[str, Any]:
        raw = event.to_dict()
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        text = str(payload.get("text") or "")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "phase": "sense",
            "event": {
                "event_id": raw.get("event_id"),
                "type": raw.get("type"),
                "source": redact_sensitive(raw.get("source") or {}, max_string=160),
                "received_at": raw.get("received_at") or raw.get("timestamp"),
                "payload": {
                    "text_preview": text[:160],
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
                    "text_length": len(text),
                    "metadata": redact_sensitive(metadata, max_string=300, max_list=10),
                    "attachment_count": len(payload.get("attachments") or [])
                    if isinstance(payload.get("attachments"), list)
                    else 0,
                },
            },
            "privacy": {
                "full_text_persisted": False,
                "classification": "runtime_event_redacted",
            },
        }

    def _state_answer_for_turn(self, event: VeyraEvent, text: str) -> dict[str, Any]:
        if self._is_local_state_overview_question(text) or self._is_state_workload_question(text):
            contract = self._local_state_answer_contract(text, event)
            return {
                "reason": "state_grounded_local_answer",
                "response": contract["answer_text"],
                "state_answer": contract,
            }
        if self.commitment_core and self._is_commitment_state_question(text):
            semantic = self.commitment_core.semantic_intent_for_turn(user_text=text, event=event)
            if semantic.get("operation") == "query_status":
                answer = self.commitment_core.answer_commitment_status(semantic, event=event)
                response = str(answer.get("state_answer") or "").strip()
                if response:
                    matches = answer.get("commitment_matches") if isinstance(answer.get("commitment_matches"), list) else []
                    watchlists = answer.get("watchlist_matches") if isinstance(answer.get("watchlist_matches"), list) else []
                    return {
                        "reason": "state_grounded_commitment_answer",
                        "response": response,
                        "commitment_turn": answer,
                        "state_answer": {
                            "answer_text": response,
                            "sources": ["user_commitments.json", "external_world.json"],
                            "observed_at": self._latest_timestamp([*matches, *watchlists]),
                            "freshness": "state_snapshot",
                            "confidence": 0.9 if matches or watchlists else 0.72,
                            "degraded_reason": None,
                        },
                    }
        return {}

    def _is_local_state_overview_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        asks_state = any(marker in compact for marker in ("状态", "怎么样", "情况", "health"))
        local_subject = any(marker in compact for marker in ("veyra本地", "本地veyra", "本地状态", "veyra状态", "localstate"))
        current = any(marker in compact for marker in ("当前", "现在", "目前", "current", "now"))
        return asks_state and local_subject and current

    def _is_state_workload_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        markers = (
            "未完成任务",
            "待处理任务",
            "关注主题",
            "可用执行器",
            "正在做什么",
            "还有哪些任务",
            "有哪些正在进行的任务",
            "有哪些进行中的任务",
            "正在进行的任务或提醒",
            "进行中的任务或提醒",
            "有哪些任务或提醒",
            "有什么任务或提醒",
            "activetasks",
            "pendingreminders",
        )
        return any(marker in compact for marker in markers)

    def _is_commitment_state_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        state_markers = (
            "还在",
            "状态",
            "运行",
            "取消了吗",
            "暂停了吗",
            "有没有",
            "有哪些",
            "有什么",
            "现在有",
            "当前有",
        )
        subject_markers = ("追踪", "关注", "提醒", "推送", "订阅", "主动任务", "commitment", "tracking")
        return any(marker in compact for marker in state_markers) and any(marker in compact for marker in subject_markers)

    def _local_state_answer_contract(self, text: str, event: VeyraEvent) -> dict[str, Any]:
        names = [
            "task_state.json",
            "attention_state.json",
            "executor_state.json",
            "agent_config.json",
            "user_commitments.json",
            "external_world.json",
            "risk_state.json",
            "belief_state.json",
            "local_world.json",
        ]
        snapshot = self.state_store.read_snapshot(names)
        task_state = snapshot["task_state.json"]
        attention = snapshot["attention_state.json"]
        executor = snapshot["executor_state.json"]
        agent_config = snapshot["agent_config.json"]
        commitment_state = snapshot["user_commitments.json"]
        external = snapshot["external_world.json"]
        risk = snapshot["risk_state.json"]

        tasks: list[str] = []
        current_task = task_state.get("current_task")
        if (
            isinstance(current_task, dict)
            and self._state_item_visible_to_event(current_task, event)
            and str(current_task.get("status") or "") not in {
            "success",
            "completed",
            "cancelled",
            "failed",
            }
        ):
            tasks.append(str(current_task.get("title") or current_task.get("task") or current_task.get("task_id") or "当前任务"))
        pending_tasks = task_state.get("pending_agent_tasks") if isinstance(task_state.get("pending_agent_tasks"), list) else []
        for item in pending_tasks:
            if (
                not isinstance(item, dict)
                or not self._state_item_visible_to_event(item, event)
                or str(item.get("status") or "") not in NON_TERMINAL_STATUSES
            ):
                continue
            tasks.append(str(item.get("title") or item.get("task_id") or "Agent 任务"))

        commitments = commitment_state.get("commitments") if isinstance(commitment_state.get("commitments"), list) else []
        active_commitments = [
            item
            for item in commitments
            if (
                isinstance(item, dict)
                and self._state_item_visible_to_event(item, event)
                and str(item.get("status") or "") in {"active", "pending_confirmation"}
            )
        ]
        for item in active_commitments:
            tasks.append(str(item.get("title") or item.get("kind") or item.get("commitment_id") or "主动任务"))

        topics: list[str] = []
        focus = attention.get("focus") if isinstance(attention.get("focus"), list) else []
        if event.source.user_id in {"", "local-user"}:
            topics.extend(str(item) for item in focus if item)
        watchlist = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        for item in watchlist:
            if (
                not isinstance(item, dict)
                or not self._state_item_visible_to_event(item, event)
                or str(item.get("status") or "") in {"cancelled", "paused"}
            ):
                continue
            topic = str(item.get("topic") or item.get("query") or "").strip()
            if topic:
                topics.append(topic)
        for item in active_commitments:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            topic = str(payload.get("topic") or payload.get("location") or "").strip()
            if topic:
                topics.append(topic)

        selected_agent = str(executor.get("selected_agent") or agent_config.get("selected_agent") or "unknown")
        executor_status = str(executor.get("status") or "unknown")
        if executor.get("connected") is True:
            executor_status = "connected"
        unique_tasks = list(dict.fromkeys(item for item in tasks if item))[:6]
        unique_topics = list(dict.fromkeys(item for item in topics if item))[:6]
        health = self.state_store.state_health()
        health_summary = health.get("summary") if isinstance(health.get("summary"), dict) else {}
        stale_count = int(health_summary.get("stale") or 0) + int(health_summary.get("expired") or 0)
        fresh_count = int(health_summary.get("fresh") or 0)
        observed_at = self._latest_timestamp(list(snapshot.values()))
        workload_only = self._is_state_workload_question(text) and not self._is_local_state_overview_question(text)

        parts: list[str] = []
        if not workload_only:
            parts.append(f"本地状态文件：{fresh_count} 个新鲜，{stale_count} 个过期或待刷新")
        parts.append(f"执行器：{selected_agent}（{executor_status}）")
        parts.append("未完成事项：" + ("；".join(unique_tasks) if unique_tasks else "没有记录"))
        parts.append("当前关注：" + ("、".join(unique_topics) if unique_topics else "没有记录"))
        if not workload_only:
            parts.append(f"当前风险：{risk.get('current_risk') or 'unknown'}")
        if observed_at:
            parts.append(f"快照时间：{observed_at}")
        answer_text = "；".join(parts) + "。"
        return {
            "answer_text": answer_text,
            "sources": names,
            "observed_at": observed_at,
            "freshness": "mixed" if stale_count else "fresh",
            "confidence": 0.9 if fresh_count else 0.72,
            "degraded_reason": None,
        }

    def _state_item_visible_to_event(self, item: dict[str, Any], event: VeyraEvent) -> bool:
        context = item.get("task_context") if isinstance(item.get("task_context"), dict) else {}
        item_user = str(item.get("user_id") or context.get("user_id") or "").strip()
        item_session = str(item.get("session_id") or context.get("session_id") or "").strip()
        if item_user and item_user != event.source.user_id:
            return False
        if item_session and item_session != event.source.session_id:
            return False
        if item_user or item_session:
            return True
        return event.source.user_id in {"", "local-user"}

    def _latest_timestamp(self, items: list[Any]) -> str | None:
        values: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("updated_at") or item.get("observed_at") or item.get("created_at") or "")
            if value:
                values.append(value)
        return max(values) if values else None

    def _is_meta_cognition_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in text for marker in ("架构", "认知", "prompt", "提示词", "智障")) or any(
            marker in lowered for marker in ("architecture", "cognition", "prompt")
        )

    def _early_deterministic_probe_result(
        self,
        event: VeyraEvent,
        text: str,
        attention_focus: list[str],
    ) -> LoopResult | None:
        decision = self.decision_core._rule_decide(text, attention_focus, event=event)
        probe_name = str(decision.selected_probe or "").strip()
        # Creator/latest-video lookups have a stricter L1 evidence path (official
        # feed first, verified search fallback). Do not let the generic fast
        # search probe bypass that contract.
        if classify_execution_tier(text, decision) == TIER_L1_COMPACT_EXTERNAL:
            return None
        fast_probes = {
            "time",
            "time_probe",
            "port",
            "port_probe",
            "git",
            "git_probe",
            "process",
            "process_probe",
            "system",
            "system_probe",
            "openclaw",
            "openclaw_probe",
            "hermes",
            "hermes_probe",
            "mcp",
            "mcp_probe",
            "file",
            "file_probe",
            "log",
            "log_probe",
            "network",
            "network_probe",
            "search_probe",
        }
        if decision.route != Route.PROBE or not probe_name or probe_name not in fast_probes:
            return None
        if decision.risk_level not in {RiskLevel.R0, RiskLevel.R1}:
            return None
        if not (decision.freshness_required or decision.needs_probe):
            return None
        decision.model_assist = {
            **(decision.model_assist if isinstance(decision.model_assist, dict) else {}),
            "status": "skipped",
            "reason": "adapter_contract_fast_probe",
            "recommended_route": Route.PROBE.value,
        }
        decision = self.decision_core.model_driven.enrich(text, decision, event=event)
        return self._run_probe(event, decision, attention_focus)

    def _is_proactive_weather_request(self, text: str) -> bool:
        lowered = (text or "").lower()
        wants_recurring = any(marker in text for marker in ("每天", "每日", "定时", "定期")) or any(
            marker in lowered for marker in ("daily", "every morning", "each day")
        )
        return wants_recurring and ("天气" in text or "weather" in lowered)

    def _is_learning_memory_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        return ("记得" in text or "remember" in lowered) and any(marker in text for marker in ("在学什么", "学习什么", "学什么", "学习目标"))

    def _is_tracking_memory_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return (
            any(marker in compact for marker in ("刚才关注什么", "关注什么来着", "我关注了什么", "我在关注什么"))
            or "what am i tracking" in lowered
            or "what did i ask you to track" in lowered
        )

    def _tracking_memory_response(self, event: VeyraEvent) -> str:
        topics: list[str] = []
        external = self.state_store.read_json("external_world.json")
        watchlist = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        for item in watchlist:
            if not isinstance(item, dict) or not self._state_item_visible_to_event(item, event):
                continue
            topic = str(item.get("topic") or item.get("query") or item.get("target") or "").strip()
            status = str(item.get("status") or "")
            if topic and status not in {"cancelled", "paused"}:
                topics.append(topic)
        commitments = self.state_store.read_json("user_commitments.json").get("commitments", [])
        if isinstance(commitments, list):
            for item in commitments:
                if (
                    not isinstance(item, dict)
                    or not self._state_item_visible_to_event(item, event)
                    or item.get("kind") not in {"external_digest", "learning_digest"}
                ):
                    continue
                if item.get("status") in {"cancelled", "paused"}:
                    continue
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                topic = str(payload.get("topic") or item.get("title") or "").strip()
                if topic:
                    topics.append(topic)
        unique = list(dict.fromkeys(topics))
        if not unique:
            return "我现在没有找到正在关注的外部主题。"
        return "你刚才让我关注的是：" + "、".join(unique[:5]) + "。"

    def _is_project_continuation_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return compact in {"继续", "继续昨天那个项目", "继续上次那个项目", "接着刚才"} or "continue previous project" in lowered

    def _project_context_response(self, event: VeyraEvent | None = None) -> str:
        user_world = self.state_store.read_json("user_world.json")
        scoped = self._scoped_user_profile(user_world, event.source.user_id if event else None)
        project = str(scoped.get("current_project") or user_world.get("current_project") or "").strip()
        goal = str(scoped.get("current_goal") or user_world.get("current_goal") or "").strip()
        if not project and goal.startswith("project:"):
            project = goal.split(":", 1)[1]
        if not project:
            task_state = self.state_store.read_json("task_state.json")
            history = task_state.get("history") if isinstance(task_state.get("history"), list) else []
            for item in reversed(history):
                if not isinstance(item, dict):
                    continue
                if event and item.get("session_id") and str(item.get("session_id")) != event.source.session_id:
                    continue
                message = str(item.get("message") or "")
                if "Veyra" in message:
                    project = "Veyra"
                    break
                if "Agent治理系统" in message:
                    project = "Agent治理系统"
                    break
        if not project:
            return ""
        return f"继续「{project}」这个上下文。你可以直接说要继续哪一块，我会按当前项目状态、风险和可用能力决定直答、probe、Agent 或 Guardian review。"

    def _user_context_planning_response(self, text: str, event: VeyraEvent | None = None) -> str:
        user_world = self.state_store.read_json("user_world.json")
        scoped = self._scoped_user_profile(user_world, event.source.user_id if event else None)
        profile = scoped.get("profile") if isinstance(scoped.get("profile"), dict) else user_world.get("profile") if isinstance(user_world.get("profile"), dict) else {}
        lowered = (text or "").lower()
        if any(marker in text for marker in ("三个月学习路线", "学习路线", "学习计划")):
            program = profile.get("program") if isinstance(profile.get("program"), dict) else (
                profile.get("gsoc") if isinstance(profile.get("gsoc"), dict) else {}
            )
            name = str(program.get("name") or program.get("program") or "").strip()
            direction = str(program.get("direction") or "").strip()
            if name or direction:
                anchor = " 和 ".join([part for part in (name, f"{direction} 方向" if direction else "") if part])
                return (
                    f"结合你之前提到的 {anchor}，我建议未来三个月按三段走："
                    "第 1 个月补齐核心语言/工具与目标项目代码阅读；"
                    "第 2 个月做一个与目标方向相关的小 PR 或 demo；"
                    "第 3 个月整理 proposal、里程碑和风险清单，并提前让导师/评审看到可运行成果。"
                )
        if any(marker in text for marker in ("暑期项目", "暑假项目")) or "summer project" in lowered:
            education = str(profile.get("education") or "").strip()
            if education:
                return (
                    f"结合你是{education}，更适合优先找能产出作品集的暑期项目：开源项目贡献、校内实验室工程任务、"
                    "小型后端/工具链项目，或与你当前方向相关的插件/库。具体项目清单需要再查最新招募信息。"
                )
        return ""

    def _scoped_user_profile(self, user_world: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        if not user_id or not isinstance(user_world, dict):
            return {}
        profiles = user_world.get("profiles_by_user") if isinstance(user_world.get("profiles_by_user"), dict) else {}
        scoped = profiles.get(user_id) if isinstance(profiles.get(user_id), dict) else {}
        return scoped

    def _current_learning_topic(self, user_id: str) -> str:
        goals_state = self.state_store.read_json("user_goals.json")
        goals = goals_state.get("goals") if isinstance(goals_state.get("goals"), list) else []
        for goal in reversed(goals):
            if not isinstance(goal, dict):
                continue
            if goal.get("kind") == "learning" and goal.get("status") == "active" and str(goal.get("user_id") or "") == user_id:
                topic = str(goal.get("topic") or "").strip()
                if topic:
                    return topic
        if self.commitment_core is not None:
            commitments = self.commitment_core.list_commitments(user_id=user_id)
            for commitment in reversed(commitments):
                if not isinstance(commitment, dict):
                    continue
                if commitment.get("kind") != "learning_digest":
                    continue
                if commitment.get("status") not in {"active", "pending_confirmation", "paused"}:
                    continue
                payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
                topic = str(payload.get("topic") or "").strip()
                if topic:
                    return topic
        user_world = self.state_store.read_json("user_world.json")
        scoped = self._scoped_user_profile(user_world, user_id)
        topic = str(scoped.get("learning_topic") or "").strip()
        if topic:
            return topic
        topic = str(user_world.get("learning_topic") or "").strip() if user_id in {"", "local-user"} else ""
        if topic:
            return topic
        memory_state = self.state_store.read_json("agent_memory.json")
        items = memory_state.get("items") if isinstance(memory_state.get("items"), list) else []
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            patch = item.get("patch") if isinstance(item.get("patch"), dict) else {}
            if patch.get("memory_type") == "learning_goal":
                topic = str(patch.get("topic") or item.get("topic") or "").strip()
                if topic:
                    return topic
        return ""

    def _process_commitment_turn(self, event: VeyraEvent, result: LoopResult) -> dict[str, Any] | None:
        if self.commitment_core is None:
            return None
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        commitment_allowed = self._semantic_effect_allowed(decision, "commitment.mutate")
        proactive_allowed = self._semantic_effect_allowed(decision, "proactive.create")
        early = result.artifacts.get("early_awareness") if isinstance(result.artifacts.get("early_awareness"), dict) else {}
        if str(early.get("reason") or "").startswith("state_grounded_"):
            return None
        if not commitment_allowed and not proactive_allowed:
            return None
        text = str(event.payload.get("text", ""))
        if self._is_tracking_memory_question(text):
            return None
        policy = self._semantic_policy_from_decision(decision)
        contexts: list[tuple[str, dict[str, Any]]] = []
        proactive_context = self._semantic_effect_context(decision, "proactive.create")
        commitment_context = self._semantic_effect_context(decision, "commitment.mutate")
        if proactive_allowed and proactive_context.get("acts"):
            contexts.extend(
                (
                    "proactive.create",
                    self._semantic_effect_context(
                        decision,
                        "proactive.create",
                        authorized_act_id=act_id,
                    ),
                )
                for act_id in proactive_context.get("act_ids", [])
                if isinstance(act_id, str)
            )
        if commitment_allowed and commitment_context.get("acts"):
            contexts.extend(
                (
                    "commitment.mutate",
                    self._semantic_effect_context(
                        decision,
                        "commitment.mutate",
                        authorized_act_id=act_id,
                    ),
                )
                for act_id in commitment_context.get("act_ids", [])
                if isinstance(act_id, str)
            )
        if not contexts and commitment_allowed:
            contexts.append(("commitment.mutate", self._semantic_effect_context(decision, "commitment.mutate", fallback_all=True)))
        if not contexts and proactive_allowed:
            contexts.append(("proactive.create", self._semantic_effect_context(decision, "proactive.create", fallback_all=True)))

        outcomes: list[dict[str, Any]] = []
        for effect, semantic_context in contexts:
            effect_text = str(semantic_context.get("source_text") or text)
            act_ids = [
                str(item)
                for item in semantic_context.get("act_ids", [])
                if isinstance(item, str)
            ]
            identity = ",".join(act_ids) or "authoritative_turn"
            idempotency_key = deterministic_idempotency_key(
                event.event_id,
                effect,
                identity,
                namespace="veyra.commitment-turn.v1",
            )
            proposal_id = deterministic_proposal_id(idempotency_key)
            state_names = [
                "user_commitments.json",
                "user_goals.json",
                "external_world.json",
                "proactive_authorizations.json",
                "proactive_intents.json",
                "semantic_change_sets.json",
                "self_improvement_proposals.json",
            ]
            existing_proposal: StateChangeProposal | None = None
            try:
                existing_proposal = self.state_proposals.get(proposal_id)
            except ProposalNotFoundError:
                pass
            revisions = (
                dict(existing_proposal.expected_state_revisions)
                if existing_proposal is not None
                else {
                    name: int(self.state_store.read_json(name).get("_state_revision") or 0)
                    for name in state_names
                }
            )
            proposal = self.state_proposals.create_or_get(
                effect=effect,
                operation=str(semantic_context.get("operation") or "unknown"),
                target=semantic_context.get("target") if isinstance(semantic_context.get("target"), dict) else {},
                source_span=semantic_context.get("source_span") if isinstance(semantic_context.get("source_span"), dict) else None,
                explicitness=str(semantic_context.get("explicitness") or "unknown"),
                preconditions=[
                    "semantic resolver completed successfully",
                    f"semantic policy explicitly authorizes {effect}",
                    "quoted, reported, hypothetical, conditional, and negated acts are excluded",
                ],
                payload={
                    "event": {
                        "event_id": event.event_id,
                        "user_id": event.source.user_id,
                        "session_id": event.source.session_id,
                        "channel": event.source.channel,
                    },
                    "semantic_context": semantic_context,
                },
                idempotency_key=idempotency_key,
                expected_state_revisions=revisions,
                requires_approval=False,
            )

            def apply_commitment(_: StateChangeProposal) -> dict[str, Any]:
                local_effects = (
                    {"proactive.create", "commitment.mutate"}
                    if effect == "proactive.create"
                    else {"commitment.mutate"}
                )
                with self.commitment_core.authorized_effects(local_effects):
                    turn = self.commitment_core.process_turn(
                        event=event,
                        user_text=effect_text,
                        assistant_response=result.response,
                        route=result.route.value,
                        status=result.status,
                        semantic_context={**semantic_context, "effect": effect},
                    )
                if not isinstance(turn, dict) or not turn:
                    raise ValueError("authorized semantic state change could not be mapped to a local operation")
                return turn

            committed = self.state_proposals.commit(proposal.proposal_id, apply_commitment)
            commit_payload = committed.model_dump(mode="json")
            if committed.status == "committed" and isinstance(committed.output, dict):
                outcome = dict(committed.output)
                outcome["state_change"] = commit_payload
                outcomes.append(outcome)
                continue
            if committed.status == "indeterminate":
                message = "我理解了这项状态变更，但提交结果不确定。为避免重复执行，我不会自动重试；需要先核对当前状态。"
            elif committed.status == "stale_revision":
                message = "我理解了这项变更，但相关状态刚刚发生变化，因此没有按旧状态继续写入。请确认最新目标后再试。"
            else:
                message = "我理解了这项变更，但它没有成功写入；我不会把它说成已经完成。"
            outcomes.append(
                {
                    "status": f"state_change_{committed.status}",
                    "actions": [],
                    "primary_response_override": message,
                    "state_change": commit_payload,
                }
            )

        if not outcomes:
            return None
        if len(outcomes) == 1:
            return outcomes[0]
        primary_messages = [
            str(item.get("primary_response_override") or "").strip()
            for item in outcomes
            if str(item.get("primary_response_override") or "").strip()
        ]
        return {
            "status": "multi_state_change",
            "actions": [
                str(action)
                for item in outcomes
                for action in (item.get("actions") if isinstance(item.get("actions"), list) else [])
            ],
            "outcomes": outcomes,
            "primary_response_override": "；".join(primary_messages),
        }

    def _commitment_followup_messages(self, commitment_turn: dict[str, Any]) -> list[str]:
        if self.commitment_core is None:
            return []
        return self.commitment_core.followup_messages_for_turn(commitment_turn)

    def _conversation_followup_result(
        self,
        event: VeyraEvent,
        text: str,
        attention_focus: list[str],
        *,
        decision: Decision,
    ) -> LoopResult | None:
        policy = self._semantic_policy_from_decision(decision)
        preferred = str(policy.get("preferred_route") or "")
        selected_probe = str(policy.get("selected_probe") or "")
        search_allowed = preferred == Route.PROBE.value and selected_probe in {"search", "search_probe"}
        weather_allowed = preferred == Route.PROBE.value and selected_probe in {"weather", "weather_probe"}
        direct_allowed = preferred == Route.DIRECT_ANSWER.value

        if search_allowed:
            clarification_rejection = self._clarification_rejection_result(event, text, attention_focus)
            if clarification_rejection:
                return clarification_rejection
        if direct_allowed:
            generic = self._generic_previous_turn_followup_result(event, text, attention_focus)
            if generic:
                return generic
        slots = self._conversation_slots_for_event(event)
        last_tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
        if last_tool.get("type") == "search":
            if search_allowed:
                retry = self._search_followup_retry_result(event, text, attention_focus, last_tool)
                if retry:
                    return retry
            if direct_allowed:
                response = self._answer_search_followup(text, last_tool)
                if response:
                    return LoopResult(
                        event_id=event.event_id,
                        route=Route.DIRECT_ANSWER,
                        status="success",
                        response=response,
                        risk_level=RiskLevel.R0,
                        artifacts={"conversation_followup": {"type": "search_result_reference"}, "attention": attention_focus},
                    )
        if weather_allowed and self._looks_like_weather_followup(text, slots):
            location = self._resolve_weather_location_for_turn(text, slots)
            raw = self.probes["weather_probe"].run(text, location=location or None)
            state_patch = self.perception.interpret_probe_result(raw)
            verified = self.verifier.verify_probe_result(raw)
            trace = self.execution_trace.record(
                {
                    "event_id": event.event_id,
                    "route": Route.PROBE.value,
                    "task_id": event.event_id,
                    "executor": "probe:weather_probe",
                    "status": verified["status"],
                    "execution_result": raw,
                    "verification": verified,
                    "conversation_followup": True,
                }
            )
            return LoopResult(
                event_id=event.event_id,
                route=Route.PROBE,
                status=verified["status"],
                response=self._weather_user_response(raw),
                risk_level=RiskLevel.R1,
                artifacts={
                    "conversation_followup": {"type": "weather_location_reference", "resolved_location": location},
                    "probe_result": raw,
                    "state_patch": state_patch,
                    "verification": verified,
                    "execution_trace": trace,
                },
            )
        return None

    def _search_followup_retry_result(
        self,
        event: VeyraEvent,
        text: str,
        attention_focus: list[str],
        last_tool: dict[str, Any],
    ) -> LoopResult | None:
        compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
        retry_markers = {"没有这些", "没有", "都没有", "不是这些", "没这些", "换一批", "再找", "继续找", "重新搜", "重新找"}
        if compact not in retry_markers:
            return None
        previous_query = str(last_tool.get("query") or "").strip()
        if not previous_query:
            return None
        query = self._broaden_search_query(previous_query)
        decision = Decision(
            route=Route.PROBE,
            risk_level=RiskLevel.R1,
            reason="user rejected previous search results; retry with broadened query",
            selected_probe="search_probe",
            intent="information",
            complexity="simple",
            capability="probe",
            freshness_required=True,
            needs_probe=True,
            memory_policy="forget",
            reasoning_mode="evidence",
            required_capabilities=["web_search"],
            capability_request={"capability": "web_search", "probe": "search_probe", "reason": "retry external lookup"},
            signals=["followup:search_retry", "freshness:external_lookup", "capability:web_search"],
            constraints=["retry search with a broader query", "do not fall back to generic clarification"],
            model_assist={
                "status": "skipped",
                "reason": "search_followup_retry",
                "recommended_route": Route.PROBE.value,
                "probe_params": {"query": query},
            },
        )
        decision = self.decision_core.model_driven.enrich(query, decision, event=event)
        result = self._run_probe(event, decision, attention_focus)
        result.artifacts["conversation_followup"] = {
            "type": "search_retry",
            "previous_query": previous_query[:240],
            "query": query[:240],
        }
        return result

    def _broaden_search_query(self, query: str) -> str:
        compact = re.sub(r"[\s，,。！？!?、]+", "", query or "")
        if "秋招" in compact or "校招" in compact or "招聘" in compact:
            year_match = re.search(r"20\d{2}", query or "")
            year = year_match.group(0) if year_match else "2026"
            return f"{year} 秋招 校招 公司 招聘 信息 网申 岗位"
        return f"{query} 相关信息".strip()

    def _clarification_rejection_result(self, event: VeyraEvent, text: str, attention_focus: list[str]) -> LoopResult | None:
        compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
        if compact not in {"没有这些", "没有", "都没有", "不是这些", "没这些"}:
            return None
        previous = self._last_outbound_message(event)
        previous_text = str(previous.get("message") or "")
        if "你想问哪个对象、现象或决定" not in previous_text:
            return None
        original = self._previous_inbound_message(event)
        original_text = str(original.get("text") or "").strip()
        if not self._looks_like_external_lookup_request(original_text):
            return LoopResult(
                event_id=event.event_id,
                route=Route.ASK_USER,
                status="needs_user_input",
                response="明白，你不是在补具体对象。那请直接说你要我查哪类信息，我会按泛化检索处理。",
                risk_level=RiskLevel.R0,
                artifacts={
                    "conversation_followup": {
                        "type": "clarification_rejection",
                        "source": "channel_outbox",
                        "previous_prompt": "generic_context_gap",
                    },
                    "attention": attention_focus,
                    "execution_tier": TIER_L0_DIRECT_ANSWER,
                },
            )
        decision = Decision(
            route=Route.PROBE,
            risk_level=RiskLevel.R1,
            reason="user rejected generic clarification; resume previous external lookup request",
            selected_probe="search_probe",
            intent="information",
            complexity="simple",
            capability="probe",
            freshness_required=True,
            needs_probe=True,
            memory_policy="forget",
            reasoning_mode="evidence",
            required_capabilities=["web_search"],
            capability_request={"capability": "web_search", "probe": "search_probe", "reason": "external information lookup"},
            signals=["followup:clarification_rejection", "freshness:external_lookup", "capability:web_search"],
            constraints=["use the previous user request as the search query", "do not ask the same generic clarification again"],
            model_assist={
                "status": "skipped",
                "reason": "clarification_rejection_resumes_external_lookup",
                "recommended_route": Route.PROBE.value,
                "probe_params": {"query": original_text},
            },
        )
        decision = self.decision_core.model_driven.enrich(original_text, decision, event=event)
        result = self._run_probe(event, decision, attention_focus)
        result.artifacts["conversation_followup"] = {
            "type": "clarification_rejection",
            "source": "channel_outbox",
            "previous_user_text": original_text[:280],
        }
        return result

    def _previous_inbound_message(self, event: VeyraEvent) -> dict[str, Any]:
        channel_state = self.state_store.read_json("channel_state.json")
        inbox = channel_state.get("inbox") if isinstance(channel_state.get("inbox"), list) else []
        for item in reversed(inbox):
            if not isinstance(item, dict):
                continue
            if str(item.get("session_id") or "") != event.source.session_id:
                continue
            if str(item.get("event_id") or "") == event.event_id:
                continue
            text = str(item.get("text") or "").strip()
            if text:
                return dict(item)
        return {}

    def _looks_like_external_lookup_request(self, text: str) -> bool:
        lowered = (text or "").lower()
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        lookup_markers = ("找", "搜索", "搜", "查", "查询", "信息", "资料", "名单", "list", "search", "lookup", "find")
        external_markers = (
            "秋招",
            "春招",
            "校招",
            "招聘",
            "网申",
            "内推",
            "求职",
            "岗位",
            "公司",
            "新闻",
            "最新",
            "2026",
            "2027",
            "company",
            "companies",
            "recruit",
            "hiring",
            "jobs",
        )
        return any(marker in compact or marker in lowered for marker in lookup_markers) and any(
            marker in compact or marker in lowered for marker in external_markers
        )

    def _generic_previous_turn_followup_result(self, event: VeyraEvent, text: str, attention_focus: list[str]) -> LoopResult | None:
        followup = self._classify_previous_turn_followup(text)
        if not followup:
            return None
        previous = self._last_outbound_message(event)
        if not previous:
            return None
        response = self._previous_turn_followup_response(kind=followup["kind"], previous=previous)
        if not response:
            return None
        return LoopResult(
            event_id=event.event_id,
            route=Route.DIRECT_ANSWER,
            status="success",
            response=response,
            risk_level=RiskLevel.R0,
            artifacts={
                "conversation_followup": {
                    "type": "previous_turn_reference",
                    "kind": followup["kind"],
                    "operation": followup["operation"],
                    "source": "channel_outbox",
                },
                "attention": attention_focus,
                "execution_tier": TIER_L0_DIRECT_ANSWER,
            },
        )

    def _classify_previous_turn_followup(self, text: str) -> dict[str, str]:
        compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
        if not compact:
            return {}
        if compact in {"为什么", "为啥", "why", "为什么这么说", "你为什么这么回答", "为什么这么回答", "怎么这么回答"}:
            return {"kind": "why", "operation": "explain_previous"}
        if compact in {"这是什么意思", "什么意思", "这个是什么意思", "啥意思", "这是啥意思"}:
            return {"kind": "meaning", "operation": "explain_previous"}
        if compact in {"继续", "然后呢", "接着说", "继续说", "继续说下去", "接着刚才"}:
            return {"kind": "continue", "operation": "continue_previous"}
        if compact in {"具体点", "展开说", "详细点", "说具体点"}:
            return {"kind": "expand", "operation": "expand_previous"}
        if compact in {"简单说", "简单点", "简短点", "一句话说"}:
            return {"kind": "simplify", "operation": "simplify_previous"}
        return {}

    def _last_outbound_message(self, event: VeyraEvent) -> dict[str, Any]:
        channel_state = self.state_store.read_json("channel_state.json")
        outbox = channel_state.get("outbox") if isinstance(channel_state.get("outbox"), list) else []
        for item in reversed(outbox):
            if not isinstance(item, dict):
                continue
            if str(item.get("session_id") or "") != event.source.session_id:
                continue
            message = str(item.get("message") or item.get("response") or "").strip()
            if message:
                return {
                    "message": message,
                    "route": (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("route"),
                    "created_at": item.get("created_at"),
                }
        return {}

    def _previous_turn_followup_response(self, *, kind: str, previous: dict[str, Any]) -> str:
        message = re.sub(r"\s+", " ", str(previous.get("message") or "").strip())
        if not message:
            return ""
        snippet = message[:180] + ("..." if len(message) > 180 else "")
        if kind == "why":
            return (
                "我是在接着上一轮回答的判断来解释："
                f"{snippet} 这个判断的核心依据是上一轮上下文，而不是把你的短句当成新任务。"
                "如果你指的是其中某个具体结论，我可以继续拆它的依据。"
            )
        if kind == "meaning":
            return f"上一轮的意思是：{snippet} 换句话说，我是在说明这个概念/判断在当前上下文里的作用。"
        if kind == "continue":
            return f"接着上一轮说：{snippet} 下一步要先明确你想继续讨论概念、查当前状态，还是让我下发具体执行任务。"
        if kind == "expand":
            return f"更具体地说，上一轮这句话的重点是：{snippet} 关键边界是先判断是否需要新鲜证据，再决定直答、probe 或 Agent。"
        if kind == "simplify":
            return f"简单说：{snippet}"
        return ""

    def _compact_search_probe_result(self, compact: dict[str, Any]) -> dict[str, Any]:
        candidates = [
            compact.get("probe_result") if isinstance(compact.get("probe_result"), dict) else {},
            compact.get("search") if isinstance(compact.get("search"), dict) else {},
        ]
        for item in candidates:
            if item.get("probe") != "search_probe":
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            results = details.get("results") if isinstance(details.get("results"), list) else []
            if results:
                return item
        return {}

    def _compact_lookup_failure_probe_result(self, compact: dict[str, Any]) -> dict[str, Any]:
        search = compact.get("search") if isinstance(compact.get("search"), dict) else {}
        if search.get("probe") == "search_probe":
            result = dict(search)
            result.setdefault("status", "failed")
            result.setdefault("summary", "compact external lookup attempted search but found no reliable evidence")
            return result
        task = compact.get("compact_task") if isinstance(compact.get("compact_task"), dict) else {}
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        creator = str(target.get("creator") or "").strip()
        query = f"{creator} YouTube 最新视频".strip() if creator else "latest external fact"
        return {
            "probe": "search_probe",
            "status": "failed",
            "source": "compact_external_lookup",
            "target": query,
            "confidence": 0.0,
            "summary": "compact external lookup attempted search but found no reliable evidence",
            "details": {
                "query": query,
                "reason": compact.get("reason") or "compact_lookup_failed",
                "compact_task": task,
            },
        }

    def _conversation_slots_for_event(self, event: VeyraEvent) -> dict[str, Any]:
        state = self.state_store.read_json("task_state.json")
        slots_by_session = state.get("conversation_slots") if isinstance(state.get("conversation_slots"), dict) else {}
        slots = slots_by_session.get(event.source.session_id) if isinstance(slots_by_session, dict) else {}
        return dict(slots) if isinstance(slots, dict) else {}

    def _persist_conversation_slots(self, event: VeyraEvent, result: LoopResult) -> None:
        persisted_slots: dict[str, Any] = {}

        def persist(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted_slots
            slots_by_session = state.get("conversation_slots") if isinstance(state.get("conversation_slots"), dict) else {}
            slots_by_session = dict(slots_by_session)
            slots = dict(slots_by_session.get(event.source.session_id) or {})
            text = str(event.payload.get("text") or "")
            slots["last_intent"] = self._intent_from_result(result)
            slots["last_topic"] = self._topic_from_turn(text, result, slots)
            tool_result = self._structured_tool_result(result)
            if tool_result:
                slots["last_tool_result"] = tool_result
                if tool_result.get("type") == "weather":
                    location = str(tool_result.get("requested_location") or tool_result.get("location") or "").strip()
                    if location:
                        slots["last_location"] = location
                    slots["last_topic"] = "weather"
                elif tool_result.get("type") == "search":
                    query = str(tool_result.get("query") or "").strip()
                    if query:
                        slots["last_search_query"] = query
                        slots["last_topic"] = query
            elif self._looks_like_location_text(text):
                resolved_location = self._resolve_weather_location_for_turn(text, slots)
                if resolved_location:
                    slots["last_location"] = resolved_location
            commitment_artifact = result.artifacts.get("commitment") if isinstance(result.artifacts.get("commitment"), dict) else {}
            if commitment_artifact:
                commitment_topic = self._commitment_topic_from_artifact(commitment_artifact)
                if commitment_topic:
                    slots["last_commitment_topic"] = commitment_topic
                    slots["last_topic"] = commitment_topic
            slots["updated_at"] = utc_now_iso()
            slots_by_session[event.source.session_id] = slots
            if len(slots_by_session) > 200:
                slots_by_session = dict(list(slots_by_session.items())[-200:])
            state["conversation_slots"] = slots_by_session
            persisted_slots = slots
            return state

        self.state_store.mutate_json("task_state.json", persist)
        result.artifacts["conversation_slots"] = self._compact_conversation_slots(persisted_slots)

    def _commitment_topic_from_artifact(self, artifact: dict[str, Any]) -> str:
        semantic = artifact.get("semantic_intent") if isinstance(artifact.get("semantic_intent"), dict) else {}
        entities = semantic.get("entities") if isinstance(semantic.get("entities"), dict) else {}
        for value in (entities.get("topic"), entities.get("location")):
            text = str(value or "").strip()
            if text:
                return text
        candidates = []
        for key in ("commitment", "updated_commitments"):
            value = artifact.get(key)
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        for item in candidates:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            text = str(payload.get("topic") or payload.get("location") or item.get("title") or "").strip()
            if text:
                return text
        return ""

    def _structured_tool_result(self, result: LoopResult) -> dict[str, Any]:
        raw = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
        probe_name = str(raw.get("probe") or "")
        if probe_name == "search_probe":
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            results = details.get("results") if isinstance(details.get("results"), list) else []
            return {
                "type": "search",
                "status": str(raw.get("status") or ""),
                "query": str(details.get("query") or raw.get("target") or "").strip(),
                "summary": str(raw.get("summary") or "").strip(),
                "observed_at": str(raw.get("observed_at") or raw.get("timestamp") or utc_now_iso()),
                "results": [self._normalize_search_result(item) for item in results[:5] if isinstance(item, dict)],
            }
        if probe_name == "weather_probe":
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            current = details.get("current") if isinstance(details.get("current"), dict) else {}
            return {
                "type": "weather",
                "status": str(raw.get("status") or ""),
                "requested_location": str(raw.get("target") or details.get("location_query") or "").strip(),
                "location": str(details.get("location") or raw.get("target") or "").strip(),
                "summary": str(raw.get("summary") or "").strip(),
                "weather_description": str(details.get("weather_description") or current.get("weather_description") or "").strip(),
                "temperature_2m": current.get("temperature_2m"),
                "current": current,
                "observed_at": str(raw.get("observed_at") or raw.get("timestamp") or utc_now_iso()),
            }
        return {}

    def _normalize_search_result(self, item: dict[str, Any]) -> dict[str, Any]:
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = str(item.get("snippet") or item.get("summary") or "").strip()
        title = self._clean_search_title(str(item.get("title") or item.get("name") or "").strip(), snippet=snippet, url=url)
        return {
            "title": title,
            "url": url,
            "source": str(item.get("source") or self._host_from_url(url) or "").strip(),
            "snippet": snippet[:900],
            "published_at": str(item.get("published_at") or item.get("date") or "").strip(),
        }

    def _clean_search_title(self, title: str, *, snippet: str = "", url: str = "") -> str:
        generic = {"", "here", "result", "search result", "source: web search", "openclaw web search summary"}
        cleaned = title.strip()
        if cleaned.lower() not in generic:
            return cleaned[:240]
        for line in str(snippet or "").splitlines():
            candidate = line.strip(" #*-\t")
            if not candidate or candidate.startswith("<<<") or candidate.startswith("---") or candidate.lower() == "source: web search":
                continue
            if len(candidate) >= 4:
                return candidate[:240]
        return url or "未命名搜索结果"

    def _host_from_url(self, url: str) -> str:
        match = re.match(r"https?://([^/]+)", url or "")
        return match.group(1).lower() if match else ""

    def _intent_from_result(self, result: LoopResult) -> str:
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        intent = str(decision.get("intent") or "").strip()
        raw = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
        if raw.get("probe") == "weather_probe":
            return "weather"
        if raw.get("probe") == "search_probe":
            return "search"
        return intent or result.route.value

    def _topic_from_turn(self, text: str, result: LoopResult, slots: dict[str, Any]) -> str:
        raw = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        if raw.get("probe") == "search_probe":
            return str(details.get("query") or raw.get("target") or "").strip()
        if raw.get("probe") == "weather_probe":
            return "weather"
        return str(slots.get("last_topic") or "").strip() if self._is_short_followup(text) else ""

    def _compact_conversation_slots(self, slots: dict[str, Any]) -> dict[str, Any]:
        tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
        compact_tool = {"type": tool.get("type"), "status": tool.get("status")}
        if tool.get("type") == "search":
            compact_tool["query"] = tool.get("query")
            compact_tool["result_count"] = len(tool.get("results") if isinstance(tool.get("results"), list) else [])
        if tool.get("type") == "weather":
            compact_tool["location"] = tool.get("location")
            compact_tool["requested_location"] = tool.get("requested_location")
        return {
            "last_location": slots.get("last_location"),
            "last_topic": slots.get("last_topic"),
            "last_commitment_topic": slots.get("last_commitment_topic"),
            "last_search_query": slots.get("last_search_query"),
            "last_intent": slots.get("last_intent"),
            "last_tool_result": compact_tool,
            "updated_at": slots.get("updated_at"),
        }

    def _render_final_user_messages(self, event: VeyraEvent, result: LoopResult) -> None:
        result.response = self._render_user_message(event, result, str(result.response or ""), primary=True)
        rendered_followups = []
        seen_messages = {str(result.response or "").strip()} if str(result.response or "").strip() else set()
        for message in result.followup_messages or []:
            rendered = self._render_user_message(event, result, str(message or ""), primary=False)
            if rendered and rendered not in seen_messages:
                rendered_followups.append(rendered)
                seen_messages.add(rendered)
        result.followup_messages = rendered_followups

    def _render_user_message(self, event: VeyraEvent, result: LoopResult, message: str, *, primary: bool) -> str:
        text = str(message or "").strip()
        raw = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
        # The probe renderer owns only the primary evidence answer. Commitment
        # confirmations and other follow-ups must retain their own text instead
        # of being rewritten into a duplicate search/weather response.
        if primary and raw.get("probe") == "weather_probe":
            return self._weather_user_response(raw)
        if primary and raw.get("probe") == "search_probe":
            return self._search_user_response(raw)
        forbidden = (
            "No geocoding result",
            "Weather probe needs a city",
            "Search returned",
            "traceback",
            "raw exception",
            "internal probe error",
        )
        lowered = text.lower()
        if any(marker.lower() in lowered for marker in forbidden):
            slots = self._conversation_slots_for_event(event)
            last_tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
            if last_tool.get("type") == "search":
                return self._answer_search_followup("结果呢", last_tool) or "我拿到了搜索结果，但还需要你指定想看标题、链接还是摘要。"
            if last_tool.get("type") == "weather":
                location = str(last_tool.get("requested_location") or last_tool.get("location") or "").strip()
                if location:
                    return f"我暂时没能精确查到「{location}」的天气；你可以补充上级城市，或让我按上次地点继续查。"
            return "这轮内部工具没有返回可直接展示的结果。我不会把工具错误原样发给你；请补充地点、关键词或让我重新查一次。"
        return text

    def _weather_user_response(self, raw: dict[str, Any]) -> str:
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        status = str(raw.get("status") or "")
        requested = str(raw.get("target") or details.get("location_query") or "").strip()
        resolved = str(details.get("location") or "").strip()
        current = details.get("current") if isinstance(details.get("current"), dict) else {}
        description = str(details.get("weather_description") or current.get("weather_description") or "").strip()
        temp = current.get("temperature_2m")
        if status == "ok":
            body = f"{resolved or requested}: {description}" if description else str(raw.get("summary") or "")
            if temp is not None:
                body = f"{resolved or requested}: {description}, {temp}°C"
            if requested and resolved and requested != resolved:
                return f"我没能精确到「{requested}」的区县级天气，先按「{resolved}」给你：{description}{f'，{temp}°C' if temp is not None else ''}。"
            return body
        if status == "missing_target":
            return "你想查哪个城市或地区的天气？请直接发地点，例如“贵阳花溪区天气”。"
        if requested:
            return f"我暂时没查到「{requested}」的天气。如果这是区县或街道，请补充上级城市，我再按完整地点查。"
        return "我暂时没拿到可用天气结果。请补充城市或地区后我再查。"

    def _search_user_response(self, raw: dict[str, Any]) -> str:
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        query = str(details.get("query") or raw.get("target") or "").strip()
        results = details.get("results") if isinstance(details.get("results"), list) else []
        normalized = [self._normalize_search_result(item) for item in results[:3] if isinstance(item, dict)]
        if not normalized:
            return f"我没有拿到「{query or '这个查询'}」的可展示搜索结果。你可以换个关键词或补充平台。"
        lines = [f"我找到 {len(results)} 条结果，先给你靠前的："]
        for index, item in enumerate(normalized, start=1):
            title = item.get("title") or "未命名搜索结果"
            url = item.get("url") or ""
            snippet = item.get("snippet") or ""
            line = f"{index}. {title}"
            if url:
                line += f"\n链接：{url}"
            if snippet:
                line += f"\n摘要：{snippet[:180]}"
            lines.append(line)
        return "\n".join(lines)

    def _answer_search_followup(self, text: str, tool: dict[str, Any]) -> str:
        cleaned = (text or "").strip()
        results = tool.get("results") if isinstance(tool.get("results"), list) else []
        results = [item for item in results if isinstance(item, dict)]
        if not results:
            return ""
        wants_title = any(marker in cleaned for marker in ("标题", "名字", "叫什么", "题目"))
        wants_link = any(marker in cleaned for marker in ("链接", "地址", "网址", "url", "URL"))
        wants_first = any(marker in cleaned for marker in ("第一个", "第一条", "首个", "第1个", "第1条"))
        wants_result = any(marker in cleaned for marker in ("结果", "内容", "是什么", "呢"))
        if not any((wants_title, wants_link, wants_first, wants_result)):
            return ""
        if wants_first:
            item = results[0]
            lines = [f"第一个结果是：{item.get('title') or '未命名搜索结果'}"]
            if item.get("url"):
                lines.append(f"链接：{item.get('url')}")
            if item.get("snippet"):
                lines.append(f"摘要：{str(item.get('snippet'))[:220]}")
            return "\n".join(lines)
        if wants_link:
            links = [str(item.get("url") or "").strip() for item in results[:3] if str(item.get("url") or "").strip()]
            return "链接是：\n" + "\n".join(links) if links else "上一轮搜索结果没有提供可用链接。"
        if wants_title:
            titles = [str(item.get("title") or "").strip() for item in results[:3] if str(item.get("title") or "").strip()]
            return "标题是：\n" + "\n".join(f"{index}. {title}" for index, title in enumerate(titles, start=1)) if titles else "上一轮搜索结果没有提供清晰标题。"
        lines = ["上一轮搜索结果里，靠前结果是："]
        for index, item in enumerate(results[:3], start=1):
            title = item.get("title") or "未命名搜索结果"
            snippet = str(item.get("snippet") or "")[:160]
            lines.append(f"{index}. {title}" + (f"\n摘要：{snippet}" if snippet else ""))
        return "\n".join(lines)

    def _search_query_from_text(self, text: str) -> str:
        query = " ".join(str(text or "").split())
        query = re.sub(r"^(帮我|麻烦你|请你)?(找一下|搜索一下|搜一下|查一下|查询|找|搜索|搜)\s*", "", query)
        query = re.sub(r"^(一些|有关|关于)\s*", "", query)
        query = query.replace("秋招的公司", "秋招 公司").replace("校招的公司", "校招 公司")
        query = query.replace("公司的信息", "公司 信息").replace("公司信息", "公司 信息")
        query = query.replace("的最新", " 最新").replace("的视频", " 视频").replace("视频", " 视频")
        query = re.sub(r"\s+", " ", query).strip(" ，,。！？!?")
        return query or str(text or "").strip()

    def _looks_like_weather_followup(self, text: str, slots: dict[str, Any]) -> bool:
        if not slots:
            return False
        if self._looks_like_search_result_reference(text):
            return False
        last_tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
        if slots.get("last_intent") != "weather" and last_tool.get("type") != "weather":
            return False
        return self._looks_like_location_text(text) or self._is_short_followup(text)

    def _looks_like_search_result_reference(self, text: str) -> bool:
        cleaned = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return cleaned in {"结果呢", "链接呢", "标题是什么", "第一个是什么"} or any(marker in cleaned for marker in ("标题", "链接", "网址", "结果", "第一个"))

    def _looks_like_location_text(self, text: str) -> bool:
        location = self._clean_location_fragment(text)
        if not location or len(location) > 16:
            return False
        return bool(re.search(r"[\u4e00-\u9fff]", location)) and (
            location.endswith(("区", "县", "市", "镇", "乡", "州", "省")) or len(location) <= 6
        )

    def _is_short_followup(self, text: str) -> bool:
        cleaned = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return 0 < len(cleaned) <= 10 and (cleaned.endswith("呢") or cleaned in {"结果呢", "链接呢", "标题是什么", "第一个是什么"})

    def _resolve_weather_location_for_turn(self, text: str, slots: dict[str, Any]) -> str:
        direct = self.probes["weather_probe"]._extract_location(text) if hasattr(self.probes.get("weather_probe"), "_extract_location") else ""
        fragment = direct or self._clean_location_fragment(text)
        previous = str(slots.get("last_location") or "").strip()
        if not fragment:
            return previous
        if previous:
            return self._combine_location(previous, fragment)
        return fragment

    def _clean_location_fragment(self, text: str) -> str:
        value = str(text or "").strip(" 的？?！!，,。 、")
        for token in ("现在", "当前", "今天", "今日", "天气", "气温", "怎么样", "如何", "呢", "的"):
            value = value.replace(token, "")
        return value.strip(" 的？?！!，,。 、")

    def _combine_location(self, previous: str, fragment: str) -> str:
        previous = previous.strip()
        fragment = fragment.strip()
        if not previous:
            return fragment
        if not fragment or fragment in previous:
            return previous
        if previous in fragment:
            return fragment
        parent = previous
        if "市" in parent:
            parent = parent.split("市", 1)[0] + "市"
        else:
            parent = re.sub(r"(区|县|镇|乡)$", "", parent)
        if fragment.endswith(("区", "县", "镇", "乡")):
            return f"{parent}{fragment}"
        return fragment

    def _update(self, event: VeyraEvent, result: LoopResult) -> None:
        risk_level = result.risk_level.value if hasattr(result.risk_level, "value") else str(result.risk_level)
        self.state_store.patch_json("risk_state.json", {"current_risk": risk_level})
        self.state_store.patch_json(
            "task_state.json",
            {"current_task": {"event_id": event.event_id, "route": result.route.value, "status": result.status}},
        )
        self._append_task_history(event, result)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {"event_id": event.event_id, "route": result.route.value, "status": result.status, "artifacts": result.artifacts},
        )
        status_adapter = self.agent_registry.selected()
        self.state_store.patch_json(
            "executor_state.json",
            {
                "selected_agent": self.agent_registry.selected_name(),
                **status_adapter.connection_status(),
            },
        )
        self.runtime_entity.set_idle()

    def _sync_user_awareness_from_text(self, event: VeyraEvent, result: LoopResult) -> dict[str, Any] | None:
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        if not self._semantic_effect_allowed(decision, "profile.write"):
            return None
        text = str(event.payload.get("text") or "")
        if not text:
            return None
        semantic_context = self._semantic_effect_context(decision, "profile.write", fallback_all=True)
        source_text = str(semantic_context.get("source_text") or text)
        signals = self._extract_profile_signals(source_text)
        if not signals:
            return None
        user_id = str(event.source.user_id or "local-user")
        act_ids = [
            str(item)
            for item in semantic_context.get("act_ids", [])
            if isinstance(item, str)
        ]
        key = deterministic_idempotency_key(
            event.event_id,
            "profile.write",
            ",".join(act_ids) or "authoritative_turn",
            namespace="veyra.profile-write.v1",
        )
        proposal_id = deterministic_proposal_id(key)
        existing_proposal: StateChangeProposal | None = None
        try:
            existing_proposal = self.state_proposals.get(proposal_id)
        except ProposalNotFoundError:
            pass
        expected_revisions = (
            dict(existing_proposal.expected_state_revisions)
            if existing_proposal is not None
            else {
                "user_world.json": int(
                    self.state_store.read_json("user_world.json").get("_state_revision")
                    or 0
                )
            }
        )
        proposal = self.state_proposals.create_or_get(
            effect="profile.write",
            operation=str(semantic_context.get("operation") or "update_user_profile"),
            target=semantic_context.get("target") if isinstance(semantic_context.get("target"), dict) else {},
            source_span=semantic_context.get("source_span") if isinstance(semantic_context.get("source_span"), dict) else None,
            explicitness=str(semantic_context.get("explicitness") or "unknown"),
            preconditions=[
                "semantic policy explicitly authorizes profile.write",
                "profile values come from an authoritative source span in the current user turn",
            ],
            payload={"signals": signals, "user_id": user_id},
            idempotency_key=key,
            expected_state_revisions=expected_revisions,
            requires_approval=False,
        )

        def apply_profile(_: StateChangeProposal) -> dict[str, Any]:
            stored = self.state_store.mutate_json(
                "user_world.json",
                lambda user_world: self._apply_user_awareness_signals(
                    user_world,
                    user_id=user_id,
                    signals=signals,
                ),
            )
            return {
                "status": "written",
                "user_id": user_id,
                "state_revision": int(stored.get("_state_revision") or 0),
                "signal_keys": sorted(str(key) for key in signals),
            }

        committed = self.state_proposals.commit(proposal.proposal_id, apply_profile)
        return committed.model_dump(mode="json")

    def _apply_state_change_response_truth(self, result: LoopResult) -> None:
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        memory = (
            result.artifacts.get("memory_policy_execution")
            if isinstance(result.artifacts.get("memory_policy_execution"), dict)
            else {}
        )
        memory_change = memory.get("state_change") if isinstance(memory.get("state_change"), dict) else {}
        preference_turn = (
            str(decision.get("intent") or "") == "preference"
            or "memory:preference" in {
                str(item)
                for item in (decision.get("signals") if isinstance(decision.get("signals"), list) else [])
            }
        )
        if preference_turn and memory_change and str(memory_change.get("status") or "") != "committed":
            result.primary_response = "我理解了你的偏好，但这次没有成功保存；后续不会假装已经记住。"
            result.artifacts["response_corrected_from_commit_result"] = "memory.write"
        profile_change = (
            result.artifacts.get("profile_state_change")
            if isinstance(result.artifacts.get("profile_state_change"), dict)
            else {}
        )
        if profile_change and str(profile_change.get("status") or "") != "committed":
            result.primary_response = "我理解了你提供的背景信息，但这次没有成功写入个人上下文。"
            result.artifacts["response_corrected_from_commit_result"] = "profile.write"

    def _apply_user_awareness_signals(
        self,
        user_world: dict[str, Any],
        *,
        user_id: str,
        signals: dict[str, Any],
    ) -> dict[str, Any]:
        mirror_legacy = user_id in {"", "local-user"}
        legacy_profile = user_world.setdefault("profile", {}) if mirror_legacy else {}
        if mirror_legacy and not isinstance(legacy_profile, dict):
            legacy_profile = {}
            user_world["profile"] = legacy_profile
        profiles_by_user = user_world.setdefault("profiles_by_user", {})
        if not isinstance(profiles_by_user, dict):
            profiles_by_user = {}
            user_world["profiles_by_user"] = profiles_by_user
        scoped = profiles_by_user.setdefault(user_id, {})
        if not isinstance(scoped, dict):
            scoped = {}
        scoped_profile = scoped.setdefault("profile", {})
        if not isinstance(scoped_profile, dict):
            scoped_profile = {}
            scoped["profile"] = scoped_profile
        focus = user_world.get("focus") if isinstance(user_world.get("focus"), list) else []
        scoped_focus = scoped.get("focus") if isinstance(scoped.get("focus"), list) else []

        program = signals.get("program") if isinstance(signals.get("program"), dict) else None
        if program and program.get("name"):
            value = {"program": program["name"], "name": program["name"], "direction": program.get("direction", ""), "source": "user_explicit_message"}
            if mirror_legacy:
                legacy_profile["program"] = value
            scoped_profile["program"] = value
            if str(program["name"]).strip().upper() == "GSOC":
                if mirror_legacy:
                    legacy_profile["gsoc"] = value
                scoped_profile["gsoc"] = value
            goal = f"program:{program['name']}" + (f":{program['direction']}" if program.get("direction") else "")
            if mirror_legacy:
                user_world["current_goal"] = goal
            scoped["current_goal"] = goal
            focus_tokens = [t for t in (program["name"], program.get("direction")) if t]
            focus = self._append_focus(focus, focus_tokens)
            scoped_focus = self._append_focus(scoped_focus, focus_tokens)
        if signals.get("education"):
            if mirror_legacy:
                legacy_profile["education"] = signals["education"]
            scoped_profile["education"] = signals["education"]
        if signals.get("project"):
            project = signals["project"]
            if mirror_legacy:
                user_world["current_goal"] = f"project:{project}"
                user_world["current_project"] = project
            scoped["current_goal"] = f"project:{project}"
            scoped["current_project"] = project
            focus = self._append_focus(focus, [project, "project"])
            scoped_focus = self._append_focus(scoped_focus, [project, "project"])

        if mirror_legacy:
            user_world["focus"] = focus[-12:]
        scoped["profile"] = scoped_profile
        scoped["focus"] = scoped_focus[-12:]
        scoped["updated_at"] = utc_now_iso()
        profiles_by_user[user_id] = scoped
        user_world["profiles_by_user"] = profiles_by_user
        user_world["updated_at"] = utc_now_iso()
        return user_world

    # --- Generic user-profile extraction (model-first reasoning still flows through
    # turn_context; persistence captures *explicit* self-statements generically so it
    # works for any project/program/identity, not a hardcoded brand list). ---

    _PROJECT_MARKERS_ZH = (
        "我正在开发", "我现在在开发", "我目前在开发", "我正在做", "我现在在做", "我目前在做",
        "当前项目是", "我的项目是", "我的项目叫", "我在开发", "正在开发", "我要做", "我想做",
        "我准备做", "我在做", "正在做", "我在搞", "项目是", "我做的是",
    )
    _PROJECT_MARKERS_EN = (
        "i'm working on", "i am working on", "i'm developing", "i am developing",
        "i'm building", "i am building", "my project is", "working on",
    )
    _PROFILE_OBJECT_STOPWORDS = {
        "什么", "啥", "这个", "那个", "东西", "项目", "事情", "事", "work", "what", "it", "this",
    }

    def _extract_profile_signals(self, text: str) -> dict[str, Any]:
        signals: dict[str, Any] = {}
        program = self._extract_program_statement(text)
        if program:
            signals["program"] = program
        education = self._extract_education_statement(text)
        if education:
            signals["education"] = education
        # A program/identity statement is not also a "project" statement.
        if not program:
            project = self._extract_project_statement(text)
            if project:
                signals["project"] = project
        return signals

    def _extract_project_statement(self, text: str) -> str:
        lowered = (text or "").lower()
        for marker in self._PROJECT_MARKERS_EN:
            idx = lowered.find(marker)
            if idx >= 0:
                return self._clean_profile_object(text[idx + len(marker):])
        for marker in self._PROJECT_MARKERS_ZH:
            idx = text.find(marker)
            if idx >= 0:
                return self._clean_profile_object(text[idx + len(marker):])
        return ""

    def _clean_profile_object(self, raw: str) -> str:
        obj = re.split(r"[，,。！!？?、；;\n]", (raw or "").strip(), 1)[0].strip()
        for quantifier in ("一个", "一款", "一台", "一套", "一项", "个", "the ", "a "):
            if obj.lower().startswith(quantifier):
                obj = obj[len(quantifier):].strip()
                break
        obj = obj.lstrip("：:。 ").rstrip("的 ").strip()
        if len(obj) < 2 or obj.lower() in self._PROFILE_OBJECT_STOPWORDS:
            return ""
        return obj[:40]

    def _extract_program_statement(self, text: str) -> dict[str, str] | None:
        text = text or ""
        name_match = re.search(r"(?:参加|报名|申请)\s*([A-Za-z0-9][\w.\-]{1,19}|[\u4e00-\u9fa5]{2,12})", text)
        if not name_match:
            return None
        name = name_match.group(1).strip("的 ")
        direction = ""
        dir_match = re.search(r"(?:方向是|主攻|投)\s*([A-Za-z0-9+#./\u4e00-\u9fa5]{1,20}?)\s*方向", text)
        if not dir_match:
            dir_match = re.search(r"方向是\s*([A-Za-z0-9+#./\u4e00-\u9fa5]{1,20})", text)
        if dir_match:
            direction = dir_match.group(1).strip("的 ")
        if not name:
            return None
        return {"name": name, "direction": direction}

    def _extract_education_statement(self, text: str) -> str:
        match = re.search(
            r"我是\s*([\u4e00-\u9fa5A-Za-z0-9]{0,20}?(?:学生|本科生|研究生|博士生|工程师|开发者|程序员))",
            text or "",
        )
        if not match:
            return ""
        return match.group(1).strip()

    def _append_focus(self, current: list[Any], values: list[str]) -> list[str]:
        output = [str(item) for item in current if item]
        for value in values:
            if value not in output:
                output.append(value)
        return output

    def _poll_if_needed(
        self,
        execution: ExecutionResult,
        *,
        adapter: AgentAdapter,
    ) -> ExecutionResult:
        if execution.status not in NON_TERMINAL_STATUSES:
            return execution
        timeout_seconds, interval_seconds = self._agent_poll_windows()
        try:
            polled = adapter.poll_task(
                execution.task_id,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
            )
        except Exception as exc:  # Adapter polling must not erase the submitted evidence.
            execution.raw["poll_error"] = str(exc)
            return execution
        if polled.status == "adapter_unconfigured":
            return execution
        if polled.status in NON_TERMINAL_STATUSES and not polled.result:
            polled.result = execution.result
        if not polled.raw:
            polled.raw = execution.raw
        return polled

    def _append_task_history(self, event: VeyraEvent, result: LoopResult) -> None:
        item = {
            "event_id": event.event_id,
            "session_id": event.source.session_id,
            "route": result.route.value,
            "status": result.status,
            "risk_level": result.risk_level.value,
            "message": str(event.payload.get("text", ""))[:280],
            "result": str(result.response or "")[:320],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        def append_history(state: dict[str, Any]) -> dict[str, Any]:
            history = state.get("history") if isinstance(state.get("history"), list) else []
            history.append(item)
            state["history"] = history[-100:]
            return state

        self.state_store.mutate_json("task_state.json", append_history)

    def _agent_poll_windows(self) -> tuple[float, float]:
        config = self.state_store.read_json("agent_config.json")
        selected_agent = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected = agents.get(selected_agent) if isinstance(agents.get(selected_agent), dict) else {}
        try:
            timeout_seconds = float(selected.get("poll_timeout_seconds") or 18.0)
        except (TypeError, ValueError):
            timeout_seconds = 18.0
        try:
            interval_seconds = float(selected.get("poll_interval_seconds") or 1.0)
        except (TypeError, ValueError):
            interval_seconds = 1.0
        timeout_seconds = max(2.0, min(timeout_seconds, 60.0))
        interval_seconds = max(0.3, min(interval_seconds, 5.0))
        return timeout_seconds, interval_seconds

    def _schedule_agent_follow_up(self, *, event: VeyraEvent, decision: Decision, execution: ExecutionResult) -> None:
        if execution.status not in NON_TERMINAL_STATUSES:
            return
        channel_config = self._channel_config(event.source.channel)
        if channel_config.get("agent_follow_up_enabled", True) is False:
            return
        timeout_seconds = float(channel_config.get("agent_follow_up_timeout_seconds") or 180)
        interval_seconds = float(channel_config.get("agent_follow_up_interval_seconds") or 2)
        first_progress_seconds = float(channel_config.get("agent_follow_up_first_progress_seconds") or 30)
        progress_interval_seconds = float(channel_config.get("agent_follow_up_progress_interval_seconds") or 90)
        missing_snapshot_fail_count = int(channel_config.get("agent_missing_snapshot_fail_count") or 8)
        thread = threading.Thread(
            target=self._run_agent_follow_up,
            kwargs={
                "event": event,
                "execution": execution,
                "timeout_seconds": max(10.0, min(timeout_seconds, 900.0)),
                "interval_seconds": max(1.0, min(interval_seconds, 15.0)),
                "first_progress_seconds": max(10.0, min(first_progress_seconds, 180.0)),
                "progress_interval_seconds": max(30.0, min(progress_interval_seconds, 600.0)),
                "missing_snapshot_fail_count": max(3, min(missing_snapshot_fail_count, 60)),
            },
            daemon=True,
            name=f"veyra-followup-{execution.task_id[:10]}",
        )
        thread.start()

    def _run_agent_follow_up(
        self,
        *,
        event: VeyraEvent,
        execution: ExecutionResult,
        timeout_seconds: float,
        interval_seconds: float,
        first_progress_seconds: float,
        progress_interval_seconds: float,
        missing_snapshot_fail_count: int,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        started = time.monotonic()
        last = execution
        last_progress_at = started
        progress_sent = 0
        missing_snapshot_count = 0
        adapter = self.agent_registry.get(execution.executor)
        while time.monotonic() < deadline:
            try:
                last = adapter.fetch_task_status(execution.task_id)
            except Exception:
                time.sleep(interval_seconds)
                continue
            verification = self.verifier.verify_execution_result(last)
            self.task_tracker.apply_result(
                execution=last,
                verification=verification,
                event_id=event.event_id,
                route=Route.AGENT.value,
            )
            if self._task_missing_from_runtime_snapshot(last):
                missing_snapshot_count += 1
            else:
                missing_snapshot_count = 0
            if missing_snapshot_count >= missing_snapshot_fail_count:
                self._auto_finalize_task_failure(
                    event=event,
                    execution=last,
                    reason="task_missing_from_runtime_snapshot",
                    message=(
                        "任务后续更新：Agent 运行时连续多次未返回该任务状态，"
                        "我已自动结束本次任务以避免一直 pending。你可以回复“重试一次”让我重新发起。"
                    ),
                    details={"missing_snapshot_count": missing_snapshot_count},
                )
                return
            if last.status not in NON_TERMINAL_STATUSES:
                interpreted = self.result_interpreter.interpret_execution(last, verification)
                summary = self.response_synthesizer.agent_response(interpreted, verification)
                self._send_agent_follow_up(
                    event=event,
                    execution=last,
                    status=str(verification.get("status") or ""),
                    follow_up_kind="final",
                    message=f"任务后续更新：{summary}",
                )
                return
            now = time.monotonic()
            elapsed_seconds = int(now - started)
            if elapsed_seconds >= int(first_progress_seconds) and (progress_sent == 0 or now - last_progress_at >= progress_interval_seconds):
                self._send_agent_follow_up(
                    event=event,
                    execution=last,
                    status=str(verification.get("status") or "partially_success"),
                    follow_up_kind="progress",
                    message=f"任务仍在执行中（{last.status}，已跟进 {elapsed_seconds}s）。我会继续追踪，完成后自动回你。",
                )
                progress_sent += 1
                last_progress_at = now
            time.sleep(interval_seconds)
        self._auto_finalize_task_failure(
            event=event,
            execution=last,
            reason="follow_up_timeout",
            message=(
                f"任务后续更新：该任务超过 {int(timeout_seconds)}s 仍未完成，"
                "我已自动结束本次任务，避免无限等待。你可以回复“重试一次”让我立即重发。"
            ),
            details={"timeout_seconds": int(timeout_seconds), "last_status": last.status},
        )

    def _task_missing_from_runtime_snapshot(self, execution: ExecutionResult) -> bool:
        if execution.status not in NON_TERMINAL_STATUSES:
            return False
        text = str(execution.result or "").lower()
        return "not present in the current status snapshot" in text

    def _auto_finalize_task_failure(
        self,
        *,
        event: VeyraEvent,
        execution: ExecutionResult,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        failed_raw = dict(execution.raw or {})
        failed_raw.update(
            {
                "auto_finalized": True,
                "auto_finalize_reason": reason,
                **(details or {}),
            }
        )
        failed = ExecutionResult(
            task_id=execution.task_id,
            executor=execution.executor,
            status="failed",
            result=f"Auto-finalized by Veyra follow-up policy: {reason}.",
            raw=failed_raw,
        )
        verification = self.verifier.verify_execution_result(failed)
        self.task_tracker.apply_result(
            execution=failed,
            verification=verification,
            event_id=event.event_id,
            route=Route.AGENT.value,
        )
        self._send_agent_follow_up(
            event=event,
            execution=failed,
            status=str(verification.get("status") or "failed"),
            follow_up_kind="auto_finalized",
            message=message,
        )

    def _send_agent_follow_up(
        self,
        *,
        event: VeyraEvent,
        execution: ExecutionResult,
        status: str,
        follow_up_kind: str,
        message: str,
    ) -> None:
        metadata: dict[str, Any] = {
            "route": "agent_follow_up",
            "status": status,
            "follow_up_kind": follow_up_kind,
            "event_id": event.event_id,
            "task_id": execution.task_id,
            "executor": execution.executor,
        }
        inbound = event.payload.get("metadata") if isinstance(event.payload.get("metadata"), dict) else {}
        if inbound:
            metadata["inbound"] = inbound
            feishu = inbound.get("feishu") if isinstance(inbound.get("feishu"), dict) else {}
            if feishu:
                metadata["feishu"] = feishu
        try:
            ChannelAdapter(self.state_store, channel=event.source.channel).send(
                event.source.session_id,
                message,
                metadata=metadata,
            )
        except Exception:
            return

    def _channel_config(self, channel: str) -> dict[str, Any]:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
        return config
