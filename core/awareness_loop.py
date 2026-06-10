from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict
from typing import Any

from awareness.attention_core import AttentionCore
from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.agent_session_router import AgentSessionRouter
from core.capability_registry import CapabilityRegistry
from core.context_patch_builder import ContextPatchBuilder
from core.compact_external_lookup import CompactExternalLookup
from core.context_scope import ContextScopeFilter
from core.execution_tier import TIER_L1_COMPACT_EXTERNAL, TIER_L5_PROACTIVE, classify_execution_tier
from core.definitions import GuardianDecision, LifecycleStatus, RiskLevel
from core.decision_core import DecisionCore
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.memory_policy_runtime import MemoryPolicyRuntime
from core.perception_layer import PerceptionLayer
from core.persona_engine import PersonaEngine
from core.reasoning_core import CoreReasoning
from core.result_interpreter import ResultInterpreter
from core.runtime_entity import RuntimeEntity
from core.response_synthesizer import ResponseSynthesizer
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
        self.memory_policy_runtime = MemoryPolicyRuntime(state_store, lambda patch: self.memory_bridge.write_patch(patch))
        self.controller = VeyraController(self.capabilities)
        self.runtime_trace = RuntimeTraceRecorder(state_store)
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
        self.state_store.append_jsonl("event_log.jsonl", {"event": event.to_dict(), "phase": "sense"})

        attention_focus = self.attention.focus_for_text(text)
        self.belief.update_from_event(event)
        followup_result = self._conversation_followup_result(event, str(text or ""), attention_focus)
        if followup_result:
            return self._finalize_event(event, followup_result, started_at, route_trace, context_observability)
        early_response = self._early_awareness_response(event, str(text or ""), attention_focus)
        if early_response:
            route_trace.append(
                {
                    "phase": "early_awareness",
                    "status": "handled",
                    "reason": early_response.get("reason"),
                }
            )
            result = LoopResult(
                event_id=event.event_id,
                route=Route.DIRECT_ANSWER,
                status="success",
                response=str(early_response.get("response") or ""),
                risk_level=RiskLevel.R0,
                artifacts={
                    "early_awareness": early_response,
                    "attention": attention_focus,
                },
            )
            return self._finalize_event(event, result, started_at, route_trace, context_observability)
        belief_state = self.belief.refresh()
        turn_context = self.core_reasoning.turn_context.build(
            user_message=text,
            attention_focus=attention_focus,
            event=event,
            rule_decision={},
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
            response = "该动作需要用户确认后才能执行。"
            if decision.route == Route.ROLLBACK:
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
                response=self._direct_answer(event, decision, attention_focus),
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
            self.agent_adapter = self.agent_registry.selected()
            selected_agent = decision.target_agent or self.agent_registry.selected_name()
            memory_summary = self.memory_bridge.read_summary(event.source.session_id, attention_focus)
            context_patch = self.context_builder.build(text, attention_focus, decision=decision.to_dict(), foresight=foresight, event=event)
            context_patch["memory_summary"] = memory_summary
            if decision.model_assist:
                context_patch["core_reasoning"] = {
                    "reason": decision.model_assist.get("reason"),
                    "solution_outline": decision.model_assist.get("solution_outline", []),
                    "agent_context": decision.model_assist.get("agent_context", {}),
                    "turn_understanding": decision.model_assist.get("turn_understanding", {}),
                }
            agent_memory_summary = self.agent_adapter.fetch_memory_summary(event.source.session_id)
            if agent_memory_summary.get("summary"):
                context_patch["agent_memory_summary"] = agent_memory_summary
            scoped = self.context_scope.apply(context_patch, user_goal=text)
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
            packet = self.task_packet_builder.build(
                event=event,
                target_agent=selected_agent,
                persona_patch=persona_patch,
                policy_patch=self.guardian.policy_patch(decision.risk_level),
                context_patch=context_patch,
                required_capabilities=decision.required_capabilities,
                memory_policy=decision.memory_policy,
                agent_execution_session_id=session_plan.get("reuse_session_id"),
                agent_session_policy=str(session_plan.get("policy") or "ephemeral_per_task"),
            )
            self.agent_session_router.record(
                dialogue_session_id=event.source.session_id,
                agent_execution_session_id=packet.agent_execution_session_id,
                task_id=packet.task_id,
                user_goal=packet.user_goal,
            )
            execution = self.agent_adapter.send_task(packet)
            execution = self._poll_if_needed(execution)
            verified = self.verifier.verify_execution_result(execution)
            interpreted = self.result_interpreter.interpret_execution(execution, verified)
            synthesized_response = self.response_synthesizer.agent_response(interpreted, verified)
            pending_task = self.task_tracker.register(
                event_id=event.event_id,
                route=decision.route.value,
                execution=execution,
                verification=verified,
                session_id=event.source.session_id,
                channel=event.source.channel,
                user_id=event.source.user_id,
            )
            self._schedule_agent_follow_up(event=event, decision=decision, execution=execution)
            memory_write = None
            if verified.get("needs_memory_patch") and decision.memory_policy == "long_term":
                memory_write = self.memory_bridge.write_patch(
                    {
                        "session_id": event.source.session_id,
                        "task": text,
                        "executor": execution.executor,
                        "status": execution.status,
                        "result": execution.result,
                        "memory_policy": decision.memory_policy,
                    }
                )
            artifacts = {
                "task_packet": packet.to_dict(),
                "execution_result": asdict(execution),
                "interpreted_result": interpreted,
                "verification": verified,
                "memory_write": memory_write,
                "pending_task": pending_task,
                "decision": decision.to_dict(),
                "controller": controller_plan.to_dict(),
                "guardian": guardian_decision,
                "persona": persona_patch,
                "agent_session": {
                    "agent_execution_session_id": packet.agent_execution_session_id,
                    "agent_session_policy": packet.agent_session_policy,
                    "dialogue_session_id": event.source.session_id,
                    **session_plan.get("trace", {}),
                },
                "context_scope": scoped.get("scope"),
            }
            artifacts["execution_trace"] = self.execution_trace.record(
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
                    "execution_result": asdict(execution),
                    "interpreted_result": interpreted,
                    "verification": verified,
                }
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
                or commitment_status in {"created", "confirmed", "declined", "already_exists"}
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
        self._update(event, result)
        return result

    def _result_execution_tier(self, result: LoopResult) -> str:
        direct_tier = str(result.artifacts.get("execution_tier") or "").strip()
        if direct_tier:
            return direct_tier
        decision = result.artifacts.get("decision") if isinstance(result.artifacts.get("decision"), dict) else {}
        assist = decision.get("model_assist") if isinstance(decision.get("model_assist"), dict) else {}
        plan = assist.get("decision_plan") if isinstance(assist.get("decision_plan"), dict) else {}
        return str(plan.get("execution_tier") or assist.get("execution_tier") or "").strip()

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
        params = model_assist.get("probe_params") if isinstance(model_assist.get("probe_params"), dict) else {}
        if probe_name == "weather_probe":
            location = str(params.get("location") or params.get("place") or params.get("city") or "").strip()
            if not location:
                slots = self._conversation_slots_for_event(event)
                location = self._resolve_weather_location_for_turn(text, slots)
            return probe.run(text, location=location or None)
        if probe_name == "search_probe":
            query = str(params.get("query") or params.get("search_query") or "").strip() or self._search_query_from_text(text)
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
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        return bool(details.get("sample") or details.get("results") or details.get("content") or probe_name in {"web", "log", "file"})

    def _direct_answer(self, event: VeyraEvent, decision: Decision, attention_focus: list[str]) -> str:
        text = str(event.payload.get("text", ""))
        if decision.intent == "identity" or "source:governance_identity" in decision.signals:
            return "我是 Veyra。OpenClaw 是我可以在需要执行复杂任务时治理和调用的 Agent Runtime，不是当前对话身份。"
        if decision.intent == "preference" or "memory:preference" in decision.signals:
            return "记住了。之后我会尽量更直接，除非问题本身需要先说明风险、证据或执行边界。"
        if self._is_tracking_memory_question(text):
            return self._tracking_memory_response()
        if self._is_project_continuation_question(text):
            project_response = self._project_context_response(event)
            if project_response:
                return project_response
        contextual_plan = self._user_context_planning_response(text, event)
        if contextual_plan:
            return contextual_plan
        state_ack = self._state_update_ack_response(text)
        if state_ack:
            return state_ack
        if self._is_proactive_weather_request(text):
            return "可以，你要我每天几点发哪个城市/地区的天气？"
        if self._is_learning_memory_question(text):
            topic = self._current_learning_topic(event.source.user_id)
            if topic:
                return f"记得。你现在在学习「{topic}」。我会把它作为当前学习目标来组织后续建议。"
            return "我现在没有找到明确的学习目标记录；你可以直接告诉我要学习的主题，我会记录到 Veyra memory。"
        understanding = decision.model_assist.get("turn_understanding") if isinstance(decision.model_assist, dict) else {}
        if isinstance(understanding, dict) and str(understanding.get("source") or "") == "rule_fallback":
            if str(understanding.get("suggested_mode") or "") in {
                "strategic_discussion",
                "meta_cognition_discussion",
                "project_direction_review",
            }:
                return self._direct_answer_degraded(text=text, decision=decision, answer_assist={})
        answer_assist = self.core_reasoning.answer_assist(text=text, attention_focus=attention_focus, decision=decision.to_dict(), event=event)
        draft = str(answer_assist.get("draft_response") or answer_assist.get("response") or "").strip()
        if not draft:
            model_assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
            draft = str(model_assist.get("draft_response") or "").strip()
            if draft and answer_assist.get("status") != "model_assisted":
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
        if bool(answer_assist.get("needs_observation")) and not decision.freshness_required:
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
        gaps = answer_assist.get("context_gaps") if isinstance(answer_assist.get("context_gaps"), list) else []
        if gaps:
            gap_text = "；".join(str(item) for item in gaps[:2] if item)
            if gap_text:
                return f"针对「{snippet}」，我这轮无法产出足够可靠的直答（缺口：{gap_text}）。我先不乱猜；你可以让我改走可验证探针，或补充约束后我再回答。"
        if decision.intent in {"information", "unknown"}:
            return f"针对「{snippet}」，我这轮拿不到稳定的认知模型输出。为避免误导，我先不编结论；你可以让我改走探针取证，或把问题拆成可验证的小点继续。"
        return "我这轮无法给出稳定直答。为避免误导，我先不输出未经验证的结论；请补充上下文或改为可验证路径。"

    def _state_update_ack_response(self, text: str) -> str:
        lowered = (text or "").lower()
        if self._is_explicit_gsoc_kotlin_profile(text, lowered):
            return "已记录：你准备参加 GSoC，方向是 Kotlin。后续学习路线和项目建议会默认带上这个背景。"
        if re.search(r"我是.{0,12}(计算机|cs|computer science).{0,12}(大二|sophomore|二年级)?学生", text or "", re.IGNORECASE):
            return "已记录：你是计算机专业学生。后续项目建议会优先按你的阶段和作品集产出考虑。"
        if self._is_explicit_veyra_project_statement(text, lowered):
            return "已记录：当前项目是 Veyra。后续说“继续”或“昨天那个项目”时，我会优先恢复这个上下文。"
        if self._is_explicit_agent_governance_project_statement(text, lowered):
            return "已记录：当前项目是 Agent 治理系统。后续架构问题会优先按治理层上下文处理。"
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if all(marker in compact for marker in ("Veyra架构", "学校作业", "Docker部署")):
            return "已更新当前注意力：优先继续 Docker 部署，同时保留 Veyra 架构和学校作业作为次级上下文。"
        return ""

    def _early_awareness_response(self, event: VeyraEvent, text: str, attention_focus: list[str]) -> dict[str, Any]:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if compact in {"继续", "接着刚才"}:
            focus_text = " ".join(str(item) for item in attention_focus).lower()
            if any(marker in focus_text for marker in ("docker", "deploy", "deployment", "部署", "process", "port")):
                return {
                    "reason": "attention_continuation",
                    "response": "继续 Docker 部署上下文。我会优先围绕部署状态、进程、端口和服务可用性推进。",
                }
        if self._is_tracking_memory_question(text):
            return {"reason": "tracking_memory_question", "response": self._tracking_memory_response()}
        if self._is_project_continuation_question(text):
            project_response = self._project_context_response(event)
            if project_response:
                return {"reason": "project_continuation", "response": project_response}
        contextual_plan = self._user_context_planning_response(text, event)
        if contextual_plan:
            return {"reason": "user_profile_context_plan", "response": contextual_plan}
        state_ack = self._state_update_ack_response(text)
        if state_ack:
            return {"reason": "state_update_ack", "response": state_ack}
        return {}

    def _is_meta_cognition_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in text for marker in ("架构", "认知", "prompt", "提示词", "智障")) or any(
            marker in lowered for marker in ("architecture", "cognition", "prompt")
        )

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

    def _tracking_memory_response(self) -> str:
        topics: list[str] = []
        external = self.state_store.read_json("external_world.json")
        watchlist = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        for item in watchlist:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or item.get("query") or item.get("target") or "").strip()
            status = str(item.get("status") or "")
            if topic and status not in {"cancelled", "paused"}:
                topics.append(topic)
        commitments = self.state_store.read_json("user_commitments.json").get("commitments", [])
        if isinstance(commitments, list):
            for item in commitments:
                if not isinstance(item, dict) or item.get("kind") not in {"external_digest", "learning_digest"}:
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
            gsoc = profile.get("gsoc") if isinstance(profile.get("gsoc"), dict) else {}
            direction = str(gsoc.get("direction") or "").strip()
            if direction:
                return (
                    f"结合你之前提到的 GSoC 和 {direction} 方向，我建议未来三个月按三段走："
                    "第 1 个月补 Kotlin 语言、协程、Gradle 和目标项目代码阅读；"
                    "第 2 个月做一个与目标组织相关的小 PR 或 demo；"
                    "第 3 个月整理 proposal、里程碑和风险清单，并提前让导师看到可运行成果。"
                )
        if any(marker in text for marker in ("暑期项目", "暑假项目")) or "summer project" in lowered:
            education = str(profile.get("education") or "").strip()
            if education:
                return (
                    f"结合你是{education}，更适合优先找能产出作品集的暑期项目：开源项目贡献、校内实验室工程任务、"
                    "小型后端/工具链项目、或与 Kotlin/GSoC 方向相关的插件/库。具体项目清单需要再查最新招募信息。"
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
        user_world = self.state_store.read_json("user_world.json")
        topic = str(user_world.get("learning_topic") or "").strip()
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
        if result.route == Route.AGENT:
            return None
        text = str(event.payload.get("text", ""))
        if self._is_tracking_memory_question(text):
            return None
        return self.commitment_core.process_turn(
            event=event,
            user_text=text,
            assistant_response=result.response,
            route=result.route.value,
            status=result.status,
        )

    def _commitment_followup_messages(self, commitment_turn: dict[str, Any]) -> list[str]:
        if self.commitment_core is None:
            return []
        return self.commitment_core.followup_messages_for_turn(commitment_turn)

    def _conversation_followup_result(self, event: VeyraEvent, text: str, attention_focus: list[str]) -> LoopResult | None:
        slots = self._conversation_slots_for_event(event)
        last_tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
        if last_tool.get("type") == "search":
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
        if self._looks_like_weather_followup(text, slots):
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
        state = self.state_store.read_json("task_state.json")
        slots_by_session = state.get("conversation_slots") if isinstance(state.get("conversation_slots"), dict) else {}
        if not isinstance(slots_by_session, dict):
            slots_by_session = {}
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
        slots["updated_at"] = utc_now_iso()
        slots_by_session[event.source.session_id] = slots
        if len(slots_by_session) > 200:
            slots_by_session = dict(list(slots_by_session.items())[-200:])
        state["conversation_slots"] = slots_by_session
        self.state_store.write_json("task_state.json", state)
        result.artifacts["conversation_slots"] = self._compact_conversation_slots(slots)

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
            "last_search_query": slots.get("last_search_query"),
            "last_intent": slots.get("last_intent"),
            "last_tool_result": compact_tool,
            "updated_at": slots.get("updated_at"),
        }

    def _render_final_user_messages(self, event: VeyraEvent, result: LoopResult) -> None:
        result.response = self._render_user_message(event, result, str(result.response or ""))
        rendered_followups = []
        seen_messages = {str(result.response or "").strip()} if str(result.response or "").strip() else set()
        for message in result.followup_messages or []:
            rendered = self._render_user_message(event, result, str(message or ""))
            if rendered and rendered not in seen_messages:
                rendered_followups.append(rendered)
                seen_messages.add(rendered)
        result.followup_messages = rendered_followups

    def _render_user_message(self, event: VeyraEvent, result: LoopResult, message: str) -> str:
        text = str(message or "").strip()
        raw = result.artifacts.get("probe_result") if isinstance(result.artifacts.get("probe_result"), dict) else {}
        if raw.get("probe") == "weather_probe":
            return self._weather_user_response(raw)
        if raw.get("probe") == "search_probe":
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
        self._sync_user_awareness_from_text(event)
        self.state_store.patch_json(
            "task_state.json",
            {"current_task": {"event_id": event.event_id, "route": result.route.value, "status": result.status}},
        )
        self._append_task_history(event, result)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {"event_id": event.event_id, "route": result.route.value, "status": result.status, "artifacts": result.artifacts},
        )
        self.agent_adapter = self.agent_registry.selected()
        self.state_store.patch_json("executor_state.json", {"selected_agent": self.agent_registry.selected_name(), **self.agent_adapter.connection_status()})
        self.runtime_entity.set_idle()

    def _sync_user_awareness_from_text(self, event: VeyraEvent) -> None:
        text = str(event.payload.get("text") or "")
        if not text:
            return
        lowered = text.lower()
        user_world = self.state_store.read_json("user_world.json")
        user_id = str(event.source.user_id or "local-user")
        legacy_profile = user_world.setdefault("profile", {})
        if not isinstance(legacy_profile, dict):
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
        changed = False

        if self._is_explicit_gsoc_kotlin_profile(text, lowered):
            value = {"program": "GSoC", "direction": "Kotlin", "source": "user_explicit_message"}
            legacy_profile["gsoc"] = value
            scoped_profile["gsoc"] = value
            user_world["current_goal"] = "gsoc:Kotlin"
            scoped["current_goal"] = "gsoc:Kotlin"
            focus = self._append_focus(focus, ["GSoC", "Kotlin"])
            scoped_focus = self._append_focus(scoped_focus, ["GSoC", "Kotlin"])
            changed = True
        if re.search(r"我是.{0,12}(计算机|cs|computer science).{0,12}(大二|sophomore|二年级)?学生", text, re.IGNORECASE):
            value = "计算机专业大二学生" if "大二" in text else "计算机专业学生"
            legacy_profile["education"] = value
            scoped_profile["education"] = value
            changed = True
        if self._is_explicit_veyra_project_statement(text, lowered):
            user_world["current_goal"] = "project:Veyra"
            user_world["current_project"] = "Veyra"
            scoped["current_goal"] = "project:Veyra"
            scoped["current_project"] = "Veyra"
            focus = self._append_focus(focus, ["Veyra", "project"])
            scoped_focus = self._append_focus(scoped_focus, ["Veyra", "project"])
            changed = True
        if self._is_explicit_agent_governance_project_statement(text, lowered):
            user_world["current_goal"] = "project:Agent治理系统"
            user_world["current_project"] = "Agent治理系统"
            scoped["current_goal"] = "project:Agent治理系统"
            scoped["current_project"] = "Agent治理系统"
            focus = self._append_focus(focus, ["Agent治理系统", "governance"])
            scoped_focus = self._append_focus(scoped_focus, ["Agent治理系统", "governance"])
            changed = True
        if not changed:
            return
        user_world["focus"] = focus[-12:]
        scoped["profile"] = scoped_profile
        scoped["focus"] = scoped_focus[-12:]
        scoped["updated_at"] = utc_now_iso()
        profiles_by_user[user_id] = scoped
        user_world["profiles_by_user"] = profiles_by_user
        user_world["updated_at"] = utc_now_iso()
        self.state_store.write_json("user_world.json", user_world)

    def _append_focus(self, current: list[Any], values: list[str]) -> list[str]:
        output = [str(item) for item in current if item]
        for value in values:
            if value not in output:
                output.append(value)
        return output

    def _is_explicit_gsoc_kotlin_profile(self, text: str, lowered: str) -> bool:
        if "gsoc" not in lowered or "kotlin" not in lowered:
            return False
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        return any(
            marker in compact
            for marker in (
                "我准备参加",
                "我要参加",
                "我想投",
                "今年想投",
                "准备参加",
                "计划参加",
                "方向是kotlin",
                "投kotlin方向",
            )
        )

    def _is_explicit_veyra_project_statement(self, text: str, lowered: str) -> bool:
        if "veyra" not in lowered:
            return False
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        return any(
            marker in compact
            for marker in (
                "我正在开发veyra",
                "我在开发veyra",
                "正在开发veyra",
                "当前项目是veyra",
                "我的项目是veyra",
                "项目是veyra",
                "在做veyra",
            )
        )

    def _is_explicit_agent_governance_project_statement(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        if "agent治理系统" not in compact and "agentgovernance" not in compact:
            return False
        return any(marker in compact for marker in ("我要做", "我在做", "正在做", "正在开发", "当前项目", "我的项目", "项目是"))

    def _poll_if_needed(self, execution: ExecutionResult) -> ExecutionResult:
        if execution.status not in NON_TERMINAL_STATUSES:
            return execution
        timeout_seconds, interval_seconds = self._agent_poll_windows()
        try:
            polled = self.agent_adapter.poll_task(
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
        state = self.state_store.read_json("task_state.json")
        history = state.setdefault("history", [])
        history.append(
            {
                "event_id": event.event_id,
                "session_id": event.source.session_id,
                "route": result.route.value,
                "status": result.status,
                "risk_level": result.risk_level.value,
                "message": str(event.payload.get("text", ""))[:280],
                "result": str(result.response or "")[:320],
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        state["history"] = history[-100:]
        self.state_store.write_json("task_state.json", state)

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
