from __future__ import annotations

from dataclasses import asdict

from awareness.attention_core import AttentionCore
from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.context_patch_builder import ContextPatchBuilder
from core.definitions import GuardianDecision, LifecycleStatus, RiskLevel
from core.decision_core import DecisionCore
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.perception_layer import PerceptionLayer
from core.persona_engine import PersonaEngine
from core.runtime_entity import RuntimeEntity
from core.task_packet_builder import TaskPacketBuilder
from core.verifier import Verifier
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_adapter import ExecutionResult
from interface.agent_registry import AgentRegistry
from interface.event_schema import LoopResult, Route, VeyraEvent
from memory_bridge.local_memory_bridge import LocalMemoryBridge
from probes.git_probe import GitProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from rollback_audit.execution_trace import ExecutionTrace
from skills.skill_loader import SkillLoader
from skills.skill_runtime import SkillRuntime


class AwarenessLoop:
    """Sense -> Understand -> Focus -> Evaluate -> Decide -> Act -> Observe -> Verify -> Update."""

    def __init__(self, state_store: WorldStateStore, runtime_entity: RuntimeEntity) -> None:
        self.state_store = state_store
        self.runtime_entity = runtime_entity
        self.perception = PerceptionLayer(state_store)
        self.attention = AttentionCore(state_store)
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()
        self.decision_core = DecisionCore()
        self.foresight = ForesightEngine()
        self.guardian = GuardianController()
        self.persona_engine = PersonaEngine()
        self.context_builder = ContextPatchBuilder(state_store)
        self.task_packet_builder = TaskPacketBuilder(state_store)
        self.verifier = Verifier()
        self.agent_registry = AgentRegistry(state_store)
        self.runtime_entity.set_selected_agent(self.agent_registry.selected_name())
        self.agent_adapter = self.agent_registry.selected()
        self.review_queue = ReviewQueue(state_store)
        self.memory_bridge = LocalMemoryBridge(state_store, adapter_resolver=lambda: self.agent_registry.selected())
        self.execution_trace = ExecutionTrace(state_store)
        self.skill_loader = SkillLoader()
        self.skill_runtime = SkillRuntime(state_store)
        self.probes = {
            "system": SystemProbe(),
            "git": GitProbe(),
            "port": PortProbe(),
            "process": ProcessProbe(),
        }

    def handle_event(self, event: VeyraEvent) -> LoopResult:
        self.runtime_entity.set_status(LifecycleStatus.THINKING.value)
        text = event.payload.get("text", "")
        self.state_store.append_jsonl("event_log.jsonl", {"event": event.to_dict(), "phase": "sense"})

        attention_focus = self.attention.focus_for_text(text)
        self.belief.update_from_event(event)
        belief_state = self.belief.refresh()
        decision = self.decision_core.decide(text=text, attention_focus=attention_focus)
        self.state_store.patch_json("risk_state.json", {"current_risk": decision.risk_level.value})
        foresight = self.foresight.predict_text_action(text, decision.risk_level)
        guardian_decision = self.guardian.review_text_action(text=text, decision=decision, foresight=foresight)

        if guardian_decision["decision"] == GuardianDecision.BLOCK.value:
            self.runtime_entity.set_status(LifecycleStatus.BLOCKED.value)
            result = LoopResult(
                event_id=event.event_id,
                route=Route.BLOCK,
                status="blocked",
                response=guardian_decision["reason"],
                risk_level=decision.risk_level,
                artifacts={"guardian": guardian_decision, "foresight": foresight},
            )
            self._update(event, result)
            return result

        if guardian_decision["decision"] == GuardianDecision.ASK_USER.value:
            self.runtime_entity.set_status(LifecycleStatus.WAITING_CONFIRMATION.value)
            review = self.review_queue.create(
                event_id=event.event_id,
                task_text=text,
                risk_level=decision.risk_level.value,
                foresight=foresight,
                guardian_decision=guardian_decision,
            )
            result = LoopResult(
                event_id=event.event_id,
                route=Route.HUMAN_REVIEW,
                status="needs_confirmation",
                response="该动作需要用户确认后才能执行。",
                risk_level=decision.risk_level,
                artifacts={"guardian": guardian_decision, "foresight": foresight, "review": review},
            )
            self._update(event, result)
            return result

        self.runtime_entity.set_status(LifecycleStatus.ACTING.value)
        if decision.route == Route.DIRECT_ANSWER:
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status="success",
                response=self._direct_answer(text),
                risk_level=decision.risk_level,
                artifacts={
                    "attention": attention_focus,
                    "decision": decision.to_dict(),
                    "uncertainty": self.uncertainty.uncertainty_summary(belief_state.get("claims", [])),
                },
            )
        elif decision.route == Route.PROBE:
            result = self._run_probe(event, decision.selected_probe or "system")
        elif decision.route == Route.SKILL:
            result = self._run_skill(event, decision.selected_probe or "")
        elif decision.route == Route.AGENT:
            self.agent_adapter = self.agent_registry.selected()
            selected_agent = decision.target_agent or self.agent_registry.selected_name()
            memory_summary = self.memory_bridge.read_summary(event.source.session_id, attention_focus)
            context_patch = self.context_builder.build(text, attention_focus)
            context_patch["memory_summary"] = memory_summary
            agent_memory_summary = self.agent_adapter.fetch_memory_summary(event.source.session_id)
            if agent_memory_summary.get("summary"):
                context_patch["agent_memory_summary"] = agent_memory_summary
            packet = self.task_packet_builder.build(
                event=event,
                target_agent=selected_agent,
                persona_patch=self.persona_engine.patch_for(text, decision.risk_level),
                policy_patch=self.guardian.policy_patch(decision.risk_level),
                context_patch=context_patch,
            )
            execution = self.agent_adapter.send_task(packet)
            execution = self._poll_if_needed(execution)
            verified = self.verifier.verify_execution_result(execution)
            memory_write = None
            if verified.get("needs_memory_patch"):
                memory_write = self.memory_bridge.write_patch(
                    {
                        "session_id": event.source.session_id,
                        "task": text,
                        "executor": execution.executor,
                        "status": execution.status,
                        "result": execution.result,
                    }
                )
            artifacts = {
                "task_packet": packet.to_dict(),
                "execution_result": asdict(execution),
                "verification": verified,
                "memory_write": memory_write,
                "decision": decision.to_dict(),
                "guardian": guardian_decision,
            }
            artifacts["execution_trace"] = self.execution_trace.record(
                {
                    "event_id": event.event_id,
                    "route": decision.route.value,
                    "task_id": execution.task_id,
                    "executor": execution.executor,
                    "status": verified["status"],
                    "decision": decision.to_dict(),
                    "guardian": guardian_decision,
                    "execution_result": asdict(execution),
                    "verification": verified,
                }
            )
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status=verified["status"],
                response=execution.result,
                risk_level=decision.risk_level,
                artifacts=artifacts,
            )
        else:
            result = LoopResult(
                event_id=event.event_id,
                route=decision.route,
                status="not_implemented",
                response=f"Route {decision.route.value} is reserved but not implemented in v0.1.",
                risk_level=decision.risk_level,
            )

        self._update(event, result)
        return result

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

    def _run_probe(self, event: VeyraEvent, probe_name: str) -> LoopResult:
        text = event.payload.get("text", "")
        probe = self.probes.get(probe_name, self.probes["system"])
        raw = probe.run(text)
        state_patch = self.perception.interpret_probe_result(raw)
        belief_state = self.belief.refresh()
        verified = self.verifier.verify_probe_result(raw)
        trace = self.execution_trace.record(
            {
                "event_id": event.event_id,
                "route": Route.PROBE.value,
                "task_id": event.event_id,
                "executor": f"probe:{probe_name}",
                "status": verified["status"],
                "execution_result": raw,
                "verification": verified,
            }
        )
        return LoopResult(
            event_id=event.event_id,
            route=Route.PROBE,
            status=verified["status"],
            response=verified["message"],
            risk_level=RiskLevel.R1,
            artifacts={
                "probe_result": raw,
                "state_patch": state_patch,
                "verification": verified,
                "execution_trace": trace,
                "uncertainty": self.uncertainty.uncertainty_summary(belief_state.get("claims", [])),
            },
        )

    def _direct_answer(self, text: str) -> str:
        if "veyra" in text.lower() or "是什么" in text:
            return "Veyra 是用于产出实时感知的虚拟实体：Awareness Entity + 高权限 Agent Core Middleware + Agent Governance Layer。"
        return "已由 Veyra 直接处理。当前请求不需要调用工具或 Agent。"

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
        try:
            polled = self.agent_adapter.poll_task(execution.task_id, timeout_seconds=2.0, interval_seconds=0.5)
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
                "route": result.route.value,
                "status": result.status,
                "risk_level": result.risk_level.value,
            }
        )
        state["history"] = history[-100:]
        self.state_store.write_json("task_state.json", state)
