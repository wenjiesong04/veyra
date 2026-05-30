from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict
from typing import Any

from awareness.attention_core import AttentionCore
from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.capability_registry import CapabilityRegistry
from core.context_patch_builder import ContextPatchBuilder
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
from core.verifier import Verifier
from core.veyra_controller import VeyraController
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_adapter import ExecutionResult
from interface.channel_adapter import ChannelAdapter
from interface.agent_registry import AgentRegistry
from interface.event_schema import Decision, LoopResult, Route, VeyraEvent
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
from probes.web_probe import WebProbe
from rollback_audit.execution_trace import ExecutionTrace
from runtime.agent_task_tracker import AgentTaskTracker
from runtime.routing_trace import RuntimeTraceRecorder
from skills.skill_loader import SkillLoader
from skills.skill_runtime import SkillRuntime


class AwarenessLoop:
    """Sense -> Understand -> Focus -> Evaluate -> Decide -> Act -> Observe -> Verify -> Update."""

    def __init__(self, state_store: WorldStateStore, runtime_entity: RuntimeEntity) -> None:
        self.state_store = state_store
        self.runtime_entity = runtime_entity
        self.core_reasoning = CoreReasoning(state_store)
        self.capabilities = CapabilityRegistry(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.core_reasoning)
        self.attention = AttentionCore(state_store)
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()
        self.decision_core = DecisionCore(state_store=state_store, reasoning=self.core_reasoning)
        self.foresight = ForesightEngine(reasoning=self.core_reasoning)
        self.guardian = GuardianController()
        self.persona_engine = PersonaEngine(state_store)
        self.context_builder = ContextPatchBuilder(state_store)
        self.task_packet_builder = TaskPacketBuilder(state_store)
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
            "system": SystemProbe(),
            "git": GitProbe(),
            "port": PortProbe(),
            "process": ProcessProbe(),
            "file": FileProbe(),
            "log": LogProbe(),
            "network": NetworkProbe(),
            "web": WebProbe(),
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
        decision = self.decision_core.decide(text=text, attention_focus=attention_focus, event=event)
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
                }
            agent_memory_summary = self.agent_adapter.fetch_memory_summary(event.source.session_id)
            if agent_memory_summary.get("summary"):
                context_patch["agent_memory_summary"] = agent_memory_summary
            packet = self.task_packet_builder.build(
                event=event,
                target_agent=selected_agent,
                persona_patch=persona_patch,
                policy_patch=self.guardian.policy_patch(decision.risk_level),
                context_patch=context_patch,
                required_capabilities=decision.required_capabilities,
                memory_policy=decision.memory_policy,
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
        raw = probe.run(text)
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

    def _probe_response(self, *, probe_name: str, raw: dict[str, object], verified: dict[str, object], answer_assist: dict[str, object]) -> str:
        draft = str(answer_assist.get("draft_response") or answer_assist.get("response") or "").strip()
        if answer_assist.get("status") == "model_assisted" and draft and self._probe_draft_is_usable(probe_name=probe_name, raw=raw, draft=draft):
            return draft
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
        answer_assist = self.core_reasoning.answer_assist(text=text, attention_focus=attention_focus, decision=decision.to_dict(), event=event)
        draft = str(answer_assist.get("draft_response") or answer_assist.get("response") or "").strip()
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
        if decision.intent in {"information", "unknown"} and any(marker in lowered_question for marker in ("为什么", "是什么", "解释", "说明", "how", "what", "why")):
            if len(draft) < 24:
                return False
        if "openclaw" in lowered_draft and "openclaw" not in lowered_question and decision.intent != "identity":
            return False
        if "hermes" in lowered_draft and "hermes" not in lowered_question and decision.intent != "identity":
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
        gaps = answer_assist.get("context_gaps") if isinstance(answer_assist.get("context_gaps"), list) else []
        if gaps:
            gap_text = "；".join(str(item) for item in gaps[:2] if item)
            if gap_text:
                return f"针对「{snippet}」，我这轮无法产出足够可靠的直答（缺口：{gap_text}）。我先不乱猜；你可以让我改走可验证探针，或补充约束后我再回答。"
        if decision.intent in {"information", "unknown"}:
            return f"针对「{snippet}」，我这轮拿不到稳定的认知模型输出。为避免误导，我先不编结论；你可以让我改走探针取证，或把问题拆成可验证的小点继续。"
        return "我这轮无法给出稳定直答。为避免误导，我先不输出未经验证的结论；请补充上下文或改为可验证路径。"

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
        self.agent_adapter = self.agent_registry.selected()
        self.state_store.patch_json("executor_state.json", {"selected_agent": self.agent_registry.selected_name(), **self.agent_adapter.connection_status()})
        self.runtime_entity.set_idle()

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
