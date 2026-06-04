from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from core.awareness_context_assembler import AwarenessContextAssembler
from core.capability_registry import CapabilityRegistry
from core.definitions import RiskLevel, normalize_risk
from core.memory_policy_runtime import normalize_memory_policy
from core.model_client import redact_sensitive
from core.reasoning_core import CoreReasoning, safe_model_risk
from core.world_state import WorldStateStore
from interface.event_schema import Decision, Route, VeyraEvent


TURN_UNDERSTANDING_SYSTEM = (
    "You are Veyra Core's orientation layer inside an awareness-driven runtime. "
    "The user message is primary input. You also receive an awareness_snapshot built from "
    "Veyra's belief claims and local_world probe cache - this is existing perception, not new execution. "
    "Return strict JSON only. Structure the user's goal, entities, and whether fresh evidence is still needed. "
    "Prefer answering from fresh world-state evidence when it already satisfies the question. "
    "Do not execute probes/agents and do not write the final user reply."
)

EXECUTION_PLAN_SYSTEM = (
    "You are Veyra Core's planner. You receive turn understanding, awareness_snapshot, expanded context, "
    "and available_capabilities. Choose the minimal safe route. Return strict JSON only. "
    "Your main output contract is capability_request: the same adapter-shaped request used by native probes, "
    "skills, and selected Agent Runtime handoff. "
    "Routing policy: "
    "1) If awareness_snapshot already contains fresh sufficient evidence, use direct_answer and cite it in draft_response. "
    "2) Else if a read-only probe can fetch evidence and is available, use probe with concrete probe_params. "
    "3) Use agent only for multi-step implementation/debugging/browser/code workflows. "
    "4) Use ask_user when required evidence is missing and cannot be inferred. "
    "Probes are executors - you supply params (location, query, url, port); do not rely on regex inside probes."
)


@dataclass(slots=True)
class TurnUnderstanding:
    intent: str = "unknown"
    task_summary: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    needs_fresh_evidence: bool = False
    evidence_kind: str = ""
    can_answer_from_world_state: bool = False
    retrieval_hints: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TurnUnderstanding:
        entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
        hints = payload.get("retrieval_hints") if isinstance(payload.get("retrieval_hints"), list) else []
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            intent=str(payload.get("intent") or "unknown"),
            task_summary=str(payload.get("task_summary") or payload.get("summary") or ""),
            entities=entities,
            needs_fresh_evidence=bool(payload.get("needs_fresh_evidence")),
            evidence_kind=str(payload.get("evidence_kind") or ""),
            can_answer_from_world_state=bool(payload.get("can_answer_from_world_state")),
            retrieval_hints=[str(item) for item in hints[:8] if item],
            confidence=max(0.0, min(confidence, 1.0)),
            reason=str(payload.get("reason") or ""),
            raw=payload,
        )


@dataclass(slots=True)
class ExecutionPlan:
    recommended_route: str = "direct_answer"
    answer_source: str = "model_knowledge"
    probe: str | None = None
    probe_params: dict[str, Any] = field(default_factory=dict)
    skill: str | None = None
    needs_agent: bool = False
    agent_goal: str = ""
    draft_response: str = ""
    risk_level: str = "R0"
    intent: str = "unknown"
    complexity: str = "simple"
    freshness_required: bool = False
    memory_policy: str = "forget"
    reasoning_mode: str = "direct"
    required_capabilities: list[str] = field(default_factory=list)
    capability_request: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ExecutionPlan:
        probe_params = payload.get("probe_params") if isinstance(payload.get("probe_params"), dict) else {}
        capability_request = payload.get("capability_request") if isinstance(payload.get("capability_request"), dict) else {}
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            recommended_route=str(payload.get("recommended_route") or payload.get("route") or "direct_answer"),
            answer_source=str(payload.get("answer_source") or "model_knowledge"),
            probe=str(payload.get("probe") or payload.get("selected_probe") or "").strip() or None,
            probe_params=probe_params,
            skill=str(payload.get("skill") or "").strip() or None,
            needs_agent=bool(payload.get("needs_agent")),
            agent_goal=str(payload.get("agent_goal") or payload.get("user_goal") or ""),
            draft_response=str(payload.get("draft_response") or payload.get("response") or "")[:2000],
            risk_level=str(payload.get("risk_level") or "R0"),
            intent=str(payload.get("intent") or "unknown"),
            complexity=str(payload.get("complexity") or "simple"),
            freshness_required=bool(payload.get("freshness_required") or payload.get("needs_probe")),
            memory_policy=str(payload.get("memory_policy") or "forget"),
            reasoning_mode=str(payload.get("reasoning_mode") or "direct"),
            required_capabilities=[str(item) for item in (payload.get("required_capabilities") or [])[:12] if item],
            capability_request=capability_request,
            reason=str(payload.get("reason") or payload.get("rationale") or ""),
            confidence=max(0.0, min(confidence, 1.0)),
            raw=payload,
        )


class CognitionPipeline:
    """Awareness-driven cognition: Observe -> Orient -> Retrieve -> Plan -> Act (via Decision)."""

    def __init__(
        self,
        reasoning: CoreReasoning,
        state_store: WorldStateStore | None = None,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self.reasoning = reasoning
        self.capabilities = capabilities
        self.assembler = AwarenessContextAssembler(state_store) if state_store else None

    def run(
        self,
        *,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        turn_context: dict[str, Any] | None = None,
    ) -> Decision | None:
        if not self.reasoning.is_enabled():
            return None
        ctx = turn_context if isinstance(turn_context, dict) else self._build_turn_context(
            text=text, attention_focus=attention_focus, event=event
        )
        awareness = self._awareness_snapshot(text=text, attention_focus=attention_focus)
        understanding = self._orient_turn(text=text, awareness_snapshot=awareness, turn_context=ctx, event=event)
        if understanding is None:
            return None
        awareness = self._awareness_snapshot(
            text=text,
            attention_focus=attention_focus,
            evidence_kind=understanding.evidence_kind,
        )
        expanded = self._retrieve_context(
            understanding=understanding,
            attention_focus=attention_focus,
            turn_context=ctx,
        )
        sufficiency = self._evidence_gate(understanding=understanding, awareness=awareness)
        plan = self._plan_execution(
            text=text,
            understanding=understanding,
            awareness_snapshot=awareness,
            expanded_context=expanded,
            sufficiency=sufficiency,
            event=event,
        )
        if plan is None:
            return None
        plan = self._apply_evidence_gate(plan, sufficiency=sufficiency, understanding=understanding)
        plan = self._enforce_adapter_contract(
            text=text,
            understanding=understanding,
            plan=plan,
            sufficiency=sufficiency,
        )
        return self._to_decision(
            text=text,
            understanding=understanding,
            plan=plan,
            awareness=awareness,
            sufficiency=sufficiency,
        )

    def _awareness_snapshot(self, *, text: str, attention_focus: list[str], evidence_kind: str = "") -> dict[str, Any]:
        if not self.assembler:
            return {"status": "unavailable"}
        return self.assembler.snapshot(
            user_message=text,
            attention_focus=attention_focus,
            evidence_kind=evidence_kind,
        )

    def _orient_turn(
        self,
        *,
        text: str,
        awareness_snapshot: dict[str, Any],
        turn_context: dict[str, Any],
        event: VeyraEvent | None,
    ) -> TurnUnderstanding | None:
        active = turn_context.get("active_context") if isinstance(turn_context.get("active_context"), dict) else {}
        payload = {
            "user_message": text,
            "awareness_snapshot": awareness_snapshot,
            "session_context": {
                "current_time": active.get("current_time"),
                "event": active.get("event"),
                "attention_focus": active.get("attention_focus"),
                "persona": active.get("persona"),
            },
            "required_json_fields": {
                "intent": "conversation|information|action|implementation|preference|unknown",
                "task_summary": "one sentence summary of user goal",
                "entities": "object: location, person, query, url, port, platform, topic",
                "needs_fresh_evidence": "boolean",
                "evidence_kind": "weather|time|search|local_status|web|other|none",
                "can_answer_from_world_state": "boolean - true if awareness_snapshot already enough",
                "retrieval_hints": "optional list: conversation, belief, user, task, capabilities, runtime",
                "confidence": "0.0-1.0",
                "reason": "short rationale",
            },
        }
        result = self.reasoning.client.complete_json(
            purpose="turn_understanding",
            system=TURN_UNDERSTANDING_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("turn_understanding", result, {"text_len": len(text)})
        if result.get("status") != "model_assisted":
            return None
        return TurnUnderstanding.from_payload(result)

    def _retrieve_context(
        self,
        *,
        understanding: TurnUnderstanding,
        attention_focus: list[str],
        turn_context: dict[str, Any],
    ) -> dict[str, Any]:
        hints = list(understanding.retrieval_hints)
        if understanding.needs_fresh_evidence and "capabilities" not in hints:
            hints.append("capabilities")
        if understanding.can_answer_from_world_state and "belief" not in hints:
            hints.append("belief")
        if self.assembler:
            return self.assembler.expand(
                retrieval_hints=hints,
                attention_focus=attention_focus,
                turn_context=turn_context,
            )
        return {"status": "unavailable"}

    def _evidence_gate(self, *, understanding: TurnUnderstanding, awareness: dict[str, Any]) -> dict[str, Any]:
        if not self.assembler:
            return {"sufficient": False, "reason": "assembler_unavailable"}
        if not understanding.needs_fresh_evidence and understanding.can_answer_from_world_state:
            return {"sufficient": True, "reason": "model_marked_world_state_sufficient"}
        return self.assembler.evidence_sufficient(
            understanding_entities=understanding.entities,
            evidence_kind=understanding.evidence_kind,
            snapshot=awareness,
        )

    def _plan_execution(
        self,
        *,
        text: str,
        understanding: TurnUnderstanding,
        awareness_snapshot: dict[str, Any],
        expanded_context: dict[str, Any],
        sufficiency: dict[str, Any],
        event: VeyraEvent | None,
    ) -> ExecutionPlan | None:
        payload = {
            "user_message": text,
            "turn_understanding": {
                "intent": understanding.intent,
                "task_summary": understanding.task_summary,
                "entities": understanding.entities,
                "needs_fresh_evidence": understanding.needs_fresh_evidence,
                "evidence_kind": understanding.evidence_kind,
                "can_answer_from_world_state": understanding.can_answer_from_world_state,
                "confidence": understanding.confidence,
                "reason": understanding.reason,
            },
            "awareness_snapshot": awareness_snapshot,
            "expanded_context": expanded_context,
            "evidence_sufficiency": sufficiency,
            "available_capabilities": self._public_capabilities(),
            "allowed_routes": ["direct_answer", "probe", "skill", "agent", "ask_user", "human_review", "block"],
            "probe_catalog": self._probe_catalog(),
            "required_json_fields": {
                "recommended_route": "direct_answer|probe|skill|agent|ask_user|human_review|block",
                "answer_source": "world_state|probe|model_knowledge|agent",
                "probe": "probe name when route=probe",
                "probe_params": "object with concrete inputs",
                "draft_response": "natural reply; cite world_state evidence when answer_source=world_state",
                "risk_level": "R0-R5",
                "intent": "conversation|information|action|implementation|preference|unknown",
                "complexity": "simple|moderate|complex",
                "freshness_required": "boolean",
                "memory_policy": "forget|short_term|long_term",
                "reasoning_mode": "direct|evidence|execution",
                "required_capabilities": "list",
                "capability_request": {
                    "target_route": "direct_answer|probe|skill|agent|ask_user",
                    "receiver_type": "native|probe|skill|agent|user",
                    "capability_id": "capability id from available_capabilities when possible",
                    "executor": "probe name, skill id, or selected_agent_runtime",
                    "adapter": "native_probe|skill_executor|agent_task_packet|none",
                    "input": "object; exact params the receiver needs, e.g. location/query/url/port/user_goal",
                    "fit_score": "0.0-1.0 adapter fit for the user request",
                    "missing_context": "list",
                    "reason": "short rationale",
                },
                "confidence": "0.0-1.0",
                "reason": "short rationale",
            },
        }
        result = self.reasoning.client.complete_json(
            purpose="execution_plan",
            system=EXECUTION_PLAN_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("execution_plan", result, {"route": result.get("recommended_route"), "probe": result.get("probe")})
        if result.get("status") != "model_assisted":
            return None
        plan = ExecutionPlan.from_payload(result)
        return self._normalize_plan(plan, understanding=understanding, text=text)

    def _apply_evidence_gate(
        self,
        plan: ExecutionPlan,
        *,
        sufficiency: dict[str, Any],
        understanding: TurnUnderstanding,
    ) -> ExecutionPlan:
        if not sufficiency.get("sufficient"):
            return plan
        if plan.recommended_route == "probe" and understanding.can_answer_from_world_state:
            plan.recommended_route = "direct_answer"
            plan.answer_source = "world_state"
            plan.freshness_required = False
            if not plan.draft_response:
                claim = str(sufficiency.get("claim") or sufficiency.get("summary") or "").strip()
                if claim:
                    plan.draft_response = claim
            plan.reason = f"{plan.reason}; evidence_gate:world_state_sufficient".strip("; ")
        return plan

    def _enforce_adapter_contract(
        self,
        *,
        text: str,
        understanding: TurnUnderstanding,
        plan: ExecutionPlan,
        sufficiency: dict[str, Any],
    ) -> ExecutionPlan:
        """Validate the model's adapter-shaped request after planning.

        The model is still the first semantic router. This pass only checks whether
        its chosen receiver/input can actually satisfy Veyra's adapter contract.
        """
        probe = self._probe_for_capability_request(plan)
        if probe and plan.recommended_route == "direct_answer" and not self._has_real_world_state_evidence(sufficiency):
            plan.recommended_route = "probe"
            plan.probe = probe
            plan.answer_source = "probe"
            plan.freshness_required = True
            plan.reason = f"{plan.reason}; adapter_contract:{probe}".strip("; ")

        if self._requires_current_time_adapter(text=text, understanding=understanding, sufficiency=sufficiency):
            plan.recommended_route = "probe"
            plan.probe = "time_probe"
            plan.answer_source = "probe"
            plan.freshness_required = True
            if "time_probe" not in plan.required_capabilities:
                plan.required_capabilities.append("time_probe")
            plan.reason = f"{plan.reason}; adapter_contract:time_probe".strip("; ")
        if self._requires_weather_adapter(text=text, understanding=understanding, sufficiency=sufficiency):
            plan.recommended_route = "probe"
            plan.probe = "weather_probe"
            plan.answer_source = "probe"
            plan.freshness_required = True
            if "weather_probe" not in plan.required_capabilities:
                plan.required_capabilities.append("weather_probe")
            plan.reason = f"{plan.reason}; adapter_contract:weather_probe".strip("; ")
        if self._requires_agent_handoff(text=text, understanding=understanding):
            plan.recommended_route = "agent"
            plan.needs_agent = True
            plan.answer_source = "agent"
            plan.probe = None
            if not plan.agent_goal:
                plan.agent_goal = text
            plan.reason = f"{plan.reason}; adapter_contract:agent_task_packet".strip("; ")
        if plan.recommended_route == "agent" or plan.needs_agent:
            plan.required_capabilities = self._agent_handoff_capabilities(plan.required_capabilities)
            if "selected_agent_runtime" not in plan.required_capabilities:
                plan.required_capabilities.insert(0, "selected_agent_runtime")
        return plan

    def _normalize_plan(self, plan: ExecutionPlan, *, understanding: TurnUnderstanding, text: str = "") -> ExecutionPlan:
        entities = understanding.entities if isinstance(understanding.entities, dict) else {}
        request = plan.capability_request if isinstance(plan.capability_request, dict) else {}
        request_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        target_route = str(request.get("target_route") or "").strip().lower()
        receiver_type = str(request.get("receiver_type") or "").strip().lower()
        executor = str(request.get("executor") or "").strip()
        capability_id = str(request.get("capability_id") or "").strip()
        if target_route:
            plan.recommended_route = target_route
        if receiver_type == "probe" and executor:
            plan.probe = executor
            plan.recommended_route = "probe"
        elif receiver_type == "skill" and executor:
            plan.skill = executor
            plan.recommended_route = "skill"
        elif receiver_type == "agent":
            plan.needs_agent = True
            plan.recommended_route = "agent"
            if request_input.get("user_goal") and not plan.agent_goal:
                plan.agent_goal = str(request_input.get("user_goal"))
        if capability_id and receiver_type != "agent" and capability_id not in plan.required_capabilities:
            plan.required_capabilities.append(capability_id)
        probe_params = dict(plan.probe_params)
        probe_params.update(request_input)
        if plan.probe == "weather_probe" and not probe_params.get("location"):
            location = entities.get("location") or entities.get("place") or entities.get("city")
            if location:
                probe_params["location"] = str(location)
        if plan.probe == "search_probe" and not probe_params.get("query"):
            query = entities.get("query") or entities.get("person") or entities.get("topic")
            if query:
                probe_params["query"] = str(query)
        if plan.probe == "time":
            plan.probe = "time_probe"
        if plan.probe == "time_probe" and not probe_params.get("timezone"):
            tz = entities.get("timezone") or entities.get("city")
            if tz:
                probe_params["timezone"] = str(tz)
        plan.probe_params = probe_params
        if understanding.needs_fresh_evidence and plan.recommended_route == "direct_answer" and plan.probe:
            plan.recommended_route = "probe"
            plan.freshness_required = True
        return plan

    def _probe_for_capability_request(self, plan: ExecutionPlan) -> str:
        request = plan.capability_request if isinstance(plan.capability_request, dict) else {}
        capability = str(request.get("capability_id") or request.get("capability") or "").strip()
        executor = str(request.get("executor") or "").strip()
        candidates = [executor, capability, *plan.required_capabilities]
        for item in candidates:
            probe = {
                "time": "time_probe",
                "time_probe": "time_probe",
                "weather": "weather_probe",
                "weather_probe": "weather_probe",
                "web_search": "search_probe",
                "search": "search_probe",
                "search_probe": "search_probe",
                "web_url_probe": "web",
            }.get(str(item or "").strip())
            if probe:
                return probe
        return ""

    def _requires_current_time_adapter(
        self,
        *,
        text: str,
        understanding: TurnUnderstanding,
        sufficiency: dict[str, Any],
    ) -> bool:
        if understanding.evidence_kind in {"time", "current_time"} and not self._has_real_world_state_evidence(sufficiency):
            return True
        lowered = (text or "").lower()
        has_time_subject = any(marker in text for marker in ("几点", "几号", "时间", "日期")) or any(
            marker in lowered for marker in ("what time", "current time", "date")
        )
        has_current_marker = any(marker in text for marker in ("现在", "当前", "今天")) or any(
            marker in lowered for marker in ("now", "current", "today")
        )
        return bool(has_time_subject and has_current_marker and not self._has_real_world_state_evidence(sufficiency))

    def _has_real_world_state_evidence(self, sufficiency: dict[str, Any]) -> bool:
        return bool(sufficiency.get("sufficient") and sufficiency.get("source") in {"belief", "local_world"})

    def _requires_weather_adapter(
        self,
        *,
        text: str,
        understanding: TurnUnderstanding,
        sufficiency: dict[str, Any],
    ) -> bool:
        if understanding.evidence_kind == "weather" and not self._has_real_world_state_evidence(sufficiency):
            return True
        lowered = (text or "").lower()
        asks_weather = "天气" in text or "weather" in lowered
        current = any(marker in text for marker in ("现在", "当前", "今天", "最近")) or any(
            marker in lowered for marker in ("now", "current", "today")
        )
        return bool(asks_weather and current and not self._has_real_world_state_evidence(sufficiency))

    def _agent_handoff_capabilities(self, capabilities: list[str]) -> list[str]:
        output: list[str] = []
        for capability in capabilities:
            item = str(capability or "").strip()
            if not item or "." in item:
                continue
            output.append(item)
        return list(dict.fromkeys(output))

    def _requires_agent_handoff(self, *, text: str, understanding: TurnUnderstanding) -> bool:
        if understanding.intent == "implementation":
            return True
        lowered = (text or "").lower()
        markers = (
            "修改项目代码",
            "修改代码",
            "实现功能",
            "把 xxx 功能实现",
            "调试",
            "修复 bug",
            "写代码",
            "改代码",
            "code edit",
            "implement",
            "debug",
            "fix bug",
        )
        return any(marker in text for marker in markers[:7]) or any(marker in lowered for marker in markers[7:])

    def _to_decision(
        self,
        *,
        text: str,
        understanding: TurnUnderstanding,
        plan: ExecutionPlan,
        awareness: dict[str, Any],
        sufficiency: dict[str, Any],
    ) -> Decision:
        route = self._route_from_name(plan.recommended_route)
        risk = safe_model_risk(plan.risk_level, RiskLevel.R0)
        rule_risk = self._rule_risk_floor(text)
        if rule_risk:
            risk = self._max_risk(risk, rule_risk)
        if risk == RiskLevel.R5:
            route = Route.BLOCK
        elif risk in {RiskLevel.R3, RiskLevel.R4} and route not in {Route.HUMAN_REVIEW, Route.BLOCK, Route.ROLLBACK}:
            route = Route.HUMAN_REVIEW

        selected_probe = plan.probe if route == Route.PROBE else (plan.skill if route == Route.SKILL else None)
        model_request = plan.capability_request if isinstance(plan.capability_request, dict) else {}
        capability_request = {
            **model_request,
            "capability": (plan.required_capabilities[0] if plan.required_capabilities else plan.probe or ""),
            "probe": selected_probe,
            "reason": plan.reason,
            "target_route": route.value,
            "receiver_type": self._receiver_type_for_route(route),
            "executor": selected_probe or ("selected_agent_runtime" if route == Route.AGENT else ""),
            "input": plan.probe_params if route == Route.PROBE else {"user_goal": plan.agent_goal or text} if route == Route.AGENT else {},
        }
        model_assist = {
            "status": "model_assisted",
            "pipeline": "awareness_model_first",
            "reason": plan.reason or understanding.reason,
            "recommended_route": route.value,
            "risk_level": risk.value,
            "freshness_required": plan.freshness_required,
            "needs_probe": route == Route.PROBE,
            "needs_agent": plan.needs_agent or route == Route.AGENT,
            "needs_user_confirmation": route in {Route.HUMAN_REVIEW, Route.ASK_USER},
            "reasoning_mode": plan.reasoning_mode,
            "confidence": plan.confidence or understanding.confidence,
            "memory_policy": normalize_memory_policy(plan.memory_policy),
            "draft_response": plan.draft_response,
            "required_capabilities": plan.required_capabilities,
            "capability_request": capability_request,
            "probe_params": plan.probe_params,
            "agent_goal": plan.agent_goal,
            "answer_source": plan.answer_source,
            "turn_understanding": {
                "intent": understanding.intent,
                "task_summary": understanding.task_summary,
                "entities": understanding.entities,
                "evidence_kind": understanding.evidence_kind,
                "can_answer_from_world_state": understanding.can_answer_from_world_state,
            },
            "awareness_snapshot": awareness,
            "evidence_sufficiency": sufficiency,
        }
        signals = [
            "cognition:awareness_model_first",
            f"intent:{plan.intent or understanding.intent}",
            f"complexity:{plan.complexity}",
            f"answer_source:{plan.answer_source}",
        ]
        if plan.probe:
            signals.append(f"probe:{plan.probe}")
        if understanding.needs_fresh_evidence:
            signals.append("freshness:model_requested")
        if sufficiency.get("sufficient"):
            signals.append("evidence:world_state_sufficient")
        constraints = ["guardian remains final execution boundary"]
        if route == Route.PROBE:
            constraints.append("execute probe with model-provided params")
        if plan.answer_source == "world_state":
            constraints.append("prefer existing perception/world-state evidence")
        return Decision(
            route=route,
            risk_level=risk,
            reason=f"awareness plan: {plan.reason or understanding.task_summary or 'structured cognition'}",
            requires_confirmation=route in {Route.HUMAN_REVIEW, Route.ROLLBACK} or risk in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
            selected_probe=selected_probe,
            intent=plan.intent or understanding.intent,
            complexity=plan.complexity,
            capability=self._capability_for_route(route),
            freshness_required=plan.freshness_required,
            needs_probe=route == Route.PROBE,
            needs_agent=plan.needs_agent or route == Route.AGENT,
            needs_user_confirmation=route in {Route.HUMAN_REVIEW, Route.ASK_USER},
            memory_policy=normalize_memory_policy(plan.memory_policy),
            reasoning_mode=plan.reasoning_mode if plan.reasoning_mode in {"direct", "evidence", "execution"} else "direct",
            required_capabilities=plan.required_capabilities,
            capability_request=capability_request,
            signals=signals,
            constraints=constraints,
            model_assist=model_assist,
        )

    def _build_turn_context(
        self,
        *,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None,
    ) -> dict[str, Any]:
        return self.reasoning.turn_context.build(
            user_message=text,
            attention_focus=attention_focus,
            event=event,
            rule_decision={},
        )

    def _public_capabilities(self) -> dict[str, Any]:
        if not self.capabilities:
            return {}
        snapshot = self.capabilities.snapshot()
        return redact_sensitive(snapshot if isinstance(snapshot, dict) else {})

    def _probe_catalog(self) -> list[dict[str, str]]:
        return [
            {"probe": "weather_probe", "params": "location", "for": "current weather"},
            {"probe": "search_probe", "params": "query", "for": "web/video/news lookup"},
            {"probe": "time_probe", "params": "timezone|city", "for": "current time/date"},
            {"probe": "git", "params": "none", "for": "git workspace status"},
            {"probe": "port", "params": "port", "for": "local port status"},
            {"probe": "web", "params": "url", "for": "fetch a specific URL"},
        ]

    def _route_from_name(self, value: str) -> Route:
        mapping = {
            "direct_answer": Route.DIRECT_ANSWER,
            "probe": Route.PROBE,
            "skill": Route.SKILL,
            "agent": Route.AGENT,
            "ask_user": Route.ASK_USER,
            "human_review": Route.HUMAN_REVIEW,
            "block": Route.BLOCK,
            "rollback": Route.ROLLBACK,
        }
        return mapping.get(str(value or "").strip().lower(), Route.DIRECT_ANSWER)

    def _capability_for_route(self, route: Route) -> str:
        return {
            Route.DIRECT_ANSWER: "native_answer",
            Route.PROBE: "probe",
            Route.SKILL: "skill",
            Route.AGENT: "selected_agent_runtime",
            Route.ASK_USER: "ask_user",
            Route.HUMAN_REVIEW: "human_review",
            Route.BLOCK: "guardian",
            Route.ROLLBACK: "rollback_audit",
        }.get(route, "unknown")

    def _receiver_type_for_route(self, route: Route) -> str:
        return {
            Route.DIRECT_ANSWER: "native",
            Route.PROBE: "probe",
            Route.SKILL: "skill",
            Route.AGENT: "agent",
            Route.ASK_USER: "user",
            Route.HUMAN_REVIEW: "user",
            Route.BLOCK: "none",
            Route.ROLLBACK: "native",
        }.get(route, "none")

    def _rule_risk_floor(self, text: str) -> RiskLevel | None:
        lowered = (text or "").lower()
        destructive = ("删除整个", "rm -rf", "drop database", "格式化", " wipe ")
        if any(marker in lowered for marker in destructive):
            return RiskLevel.R5
        return None

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        order = list(RiskLevel)
        return right if order.index(right) > order.index(left) else left

    def _trace(self, purpose: str, result: dict[str, Any], request_summary: dict[str, Any]) -> None:
        self.reasoning._trace(purpose, result, request_summary)


def cognition_mode() -> str:
    mode = os.getenv("VEYRA_COGNITION_MODE", "model_first").strip().lower()
    if mode in {"model-first"}:
        return "model_first"
    return mode


def use_model_first_pipeline(reasoning: CoreReasoning | None) -> bool:
    if not reasoning or not reasoning.is_enabled():
        return False
    client = getattr(reasoning, "client", None)
    turn_context = getattr(reasoning, "turn_context", None)
    if not callable(getattr(client, "complete_json", None)) or not callable(getattr(turn_context, "build", None)):
        return False
    mode = cognition_mode()
    if mode in {"rules", "hybrid", "legacy"}:
        return False
    return True
