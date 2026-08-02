from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from core.capability_registry import CapabilityRegistry
from core.cognition_pipeline import CognitionPipeline, use_model_first_pipeline
from core.model_driven_decision_core import ModelDrivenDecisionCore
from core.definitions import RiskLevel, classify_text_risk
from core.final_route_projection import (
    has_effectful_candidate,
    project_nonexecuting_route,
)
from core.memory_policy_runtime import normalize_memory_policy
from core.reasoning_core import CoreReasoning, safe_model_risk
from core.semantic_policy import SemanticPolicy, SemanticPolicyCompiler
from core.understanding_core import TurnUnderstanding
from core.world_state import WorldStateStore
from interface.event_schema import Decision, Route, VeyraEvent


IMPLEMENTATION_ANTI_MARKERS = (
    "修复",
    "解决",
    "实现",
    "新增",
    "添加",
    "开发",
    "重构",
    "改代码",
    "修改代码",
    "fix",
    "repair",
    "implement",
    "build",
    "refactor",
)


FRESHNESS_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "current_weather",
        "probe": "weather_probe",
        "capability": "weather_probe",
        "markers": ("天气", "weather", "temperature", "气温"),
        "requires_volatile_marker": True,
        "anti_markers": ("每天", "每日", "定时", "定期", "daily", "every morning", "each day"),
    },
    {
        "name": "volatile_time",
        "probe": "time",
        "capability": "time_probe",
        "markers": ("今天", "日期", "几点", "时间", "星期", "today", "date", "time"),
        "anti_markers": ("耗时", "时间复杂度", "运行时间", "timeout", "timestamp"),
    },
    {
        "name": "local_port_status",
        "probe": "port",
        "capability": "port_probe",
        "markers": ("端口", "port"),
    },
    {
        "name": "git_workspace_status",
        "probe": "git",
        "capability": "git_probe",
        "markers": ("git", "未提交", "工作区", "workspace"),
    },
    {
        "name": "local_process_status",
        "probe": "process",
        "capability": "process_probe",
        "markers": ("进程", "process"),
    },
    {
        "name": "openclaw_runtime_status",
        "probe": "openclaw",
        "capability": "openclaw_probe",
        "markers": ("openclaw",),
        "requires_volatile_marker": True,
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
    {
        "name": "local_service_failure",
        "probe": "process",
        "capability": "process_probe",
        "markers": (
            "无法响应",
            "不响应",
            "没回应",
            "没响应",
            "没有回应",
            "服务挂",
            "服务挂了",
            "挂了",
            "无法访问",
            "打不开",
            "not responding",
            "service down",
            "service failed",
        ),
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
    {
        "name": "hermes_runtime_status",
        "probe": "hermes",
        "capability": "hermes_probe",
        "markers": ("hermes",),
        "requires_volatile_marker": True,
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
    {
        "name": "local_system_status",
        "probe": "system",
        "capability": "system_probe",
        "markers": ("系统", "环境", "runtime", "当前状态"),
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
    {
        "name": "mcp_runtime_status",
        "probe": "mcp",
        "capability": "mcp_probe",
        "markers": ("mcp",),
        "requires_volatile_marker": True,
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
    {
        "name": "url_status",
        "probe": "web",
        "capability": "web_url_probe",
        "markers": ("http://", "https://"),
    },
    {
        "name": "latest_external_fact",
        "probe": "search_probe",
        "capability": "web_search",
        "markers": ("最新", "新闻", "latest", "recent", "today's", "今天的"),
    },
    {
        "name": "external_recruiting_lookup",
        "probe": "search_probe",
        "capability": "web_search",
        "markers": ("秋招", "春招", "校招", "招聘", "招聘信息", "网申", "内推", "求职", "岗位信息", "公司信息", "公司名单"),
        "anti_markers": IMPLEMENTATION_ANTI_MARKERS,
    },
)

VOLATILE_MARKERS = ("现在", "当前", "今天", "最近", "状态", "最新", "没响应", "不响应", "没回应", "today", "now", "current", "status", "latest")
IDENTITY_MARKERS = ("你是谁", "你是", "你现在是", "身份", "who are you", "are you")
GOVERNANCE_ENTITIES = ("veyra", "openclaw", "hermes", "runtime", "agent")
PREFERENCE_SCOPE_MARKERS = ("以后", "今后", "下次", "记住", "默认", "总是", "一直", "from now on", "remember", "always", "prefer")
PREFERENCE_CONTENT_MARKERS = ("回答", "直接", "简短", "详细", "中文", "英文", "风格", "语气", "格式", "偏好", "style", "language")


class DecisionCore:
    def __init__(self, state_store: WorldStateStore | None = None, reasoning: CoreReasoning | None = None) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or (CoreReasoning(state_store) if state_store else None)
        self.capabilities = CapabilityRegistry(state_store) if state_store else None
        self.model_driven = ModelDrivenDecisionCore()
        self.semantic_policy = SemanticPolicyCompiler()

    def decide(
        self,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        *,
        turn_context: dict[str, Any] | None = None,
        turn_understanding: TurnUnderstanding | dict[str, Any] | None = None,
    ) -> Decision:
        understanding = self._normalize_turn_understanding(turn_understanding)

        def finish(candidate: Decision) -> Decision:
            candidate = self._enforce_preference_lock(text, candidate)
            return self._enforce_semantic_policy(
                candidate,
                understanding,
                current_user_id=str(event.source.user_id or "") if event is not None else "",
            )

        locked = self._governance_locked_decision(text, attention_focus, event=event)
        if locked:
            finalized = finish(self._with_turn_understanding(locked, understanding))
            return self.model_driven.enrich(text, finalized, event=event)
        contract_base = self._rule_decide(text, attention_focus, event=event)
        contract_base = self._apply_understanding_guardrails(text, contract_base, understanding)
        if self._adapter_contract_route_is_deterministic(contract_base):
            deterministic_signal = {
                Route.PROBE: "policy:required_probe_preserved",
                Route.AGENT: "policy:rule_agent_route_preserved",
                Route.SKILL: "policy:rule_skill_route_preserved",
                Route.ASK_USER: "policy:rule_ask_user_route_preserved",
            }.get(contract_base.route)
            if deterministic_signal and deterministic_signal not in contract_base.signals:
                contract_base.signals.append(deterministic_signal)
            finalized = finish(self._with_turn_understanding(contract_base, understanding))
            return self.model_driven.enrich(text, finalized, event=event)
        if self._should_trust_rule_understanding(understanding):
            finalized = finish(self._with_turn_understanding(contract_base, understanding))
            return self.model_driven.enrich(text, finalized, event=event)
        if use_model_first_pipeline(self.reasoning):
            pipeline = CognitionPipeline(self.reasoning, self.state_store, self.capabilities)
            pipeline_decision = pipeline.run(
                text=text,
                attention_focus=attention_focus,
                event=event,
                turn_context=turn_context,
                turn_understanding=understanding,
            )
            if pipeline_decision is not None:
                pipeline_decision = self._apply_understanding_guardrails(text, pipeline_decision, understanding)
                finalized = finish(self._with_turn_understanding(pipeline_decision, understanding))
                return self.model_driven.enrich(text, finalized, event=event)
        base = self._rule_decide(text, attention_focus, event=event)
        base = self._apply_understanding_guardrails(text, base, understanding)
        base = self._with_turn_understanding(base, understanding)
        decision = self._apply_model_assist(text, attention_focus, base, event=event)
        decision = self._apply_understanding_guardrails(text, decision, understanding)
        finalized = finish(self._with_turn_understanding(decision, understanding))
        return self.model_driven.enrich(text, finalized, event=event)

    def _governance_locked_decision(self, text: str, attention_focus: list[str], event: VeyraEvent | None = None) -> Decision | None:
        """Deterministic locks that must never be overridden by model routing."""
        lowered = text.lower()
        risk = self._risk_for_text(lowered)
        intent, intent_signals = self._intent_for_text(lowered)
        complexity, complexity_signals = self._complexity_for_text(lowered, attention_focus)
        signals = intent_signals + complexity_signals + self._risk_signals(risk)
        attachment_gap = self._attachment_capability_gap(event)
        if self._is_rollback_request(lowered):
            return self._decision(
                route=Route.ROLLBACK,
                risk=RiskLevel.R4,
                reason="snapshot rollback requires guarded confirmation",
                intent="action",
                complexity="moderate",
                capability="rollback_audit",
                signals=signals + ["route:rollback"],
                requires_confirmation=True,
                needs_user_confirmation=True,
                memory_policy="short_term",
                reasoning_mode="execution",
                required_capabilities=["rollback_audit"],
                constraints=["restore only an existing snapshot", "record rollback trace", "verify restored checksum"],
            )
        if attachment_gap:
            return self._decision(
                route=Route.ASK_USER,
                risk=RiskLevel.R1,
                reason=str(attachment_gap.get("reason")),
                intent=intent if intent != "unknown" else "information",
                complexity=complexity,
                capability="ask_user",
                signals=signals + ["freshness:attachment_content", "capability:vision"],
                freshness_required=True,
                memory_policy="forget",
                reasoning_mode="evidence",
                required_capabilities=["vision"],
                capability_request=attachment_gap,
                constraints=["do not infer unreadable attachment content"],
            )
        if risk == RiskLevel.R5:
            return self._decision(
                route=Route.BLOCK,
                risk=risk,
                reason="destructive or forbidden action detected",
                intent=intent,
                complexity=complexity,
                capability="guardian",
                signals=signals,
                reasoning_mode="execution" if intent in {"action", "implementation"} else "direct",
                required_capabilities=["guardian"],
                constraints=["block unsafe action", "require safer alternative"],
            )
        if risk in {RiskLevel.R3, RiskLevel.R4}:
            return self._decision(
                route=Route.HUMAN_REVIEW,
                risk=risk,
                reason="medium/high risk action requires review",
                intent=intent,
                complexity=complexity,
                capability="human_review",
                signals=signals,
                requires_confirmation=True,
                needs_user_confirmation=True,
                memory_policy="short_term",
                reasoning_mode="execution",
                required_capabilities=["human_review"],
                constraints=["explain impact", "check rollback path", "wait for explicit approval"],
            )
        return None

    def _normalize_turn_understanding(self, value: TurnUnderstanding | dict[str, Any] | None) -> TurnUnderstanding | None:
        if isinstance(value, TurnUnderstanding):
            return value
        if isinstance(value, dict):
            return TurnUnderstanding.from_payload(value)
        return None

    def _with_turn_understanding(self, decision: Decision, understanding: TurnUnderstanding | None) -> Decision:
        if understanding is None:
            return decision
        assist = dict(decision.model_assist or {})
        assist["turn_understanding"] = understanding.to_dict(include_raw=False)
        if understanding.source:
            assist["understanding_source"] = understanding.source
        decision.model_assist = assist
        decision.signals = self._dedupe([*(decision.signals or []), f"understanding:{understanding.suggested_mode or understanding.intent}"])
        return decision

    def _enforce_semantic_policy(
        self,
        decision: Decision,
        understanding: TurnUnderstanding | None,
        *,
        current_user_id: str = "",
    ) -> Decision:
        """Make the semantic frame authoritative after every routing path.

        Existing rule/model routing remains useful as a candidate planner, but it
        cannot authorize tools, durable memory, or state mutation on its own.
        """

        frame = getattr(understanding, "semantic_frame", None) if understanding is not None else None
        if frame is None:
            missing_policy = SemanticPolicy(
                resolver_status="missing",
                policy_signals=[
                    "semantic_policy:frame_missing",
                    "semantic_policy:read_only_default",
                ],
            )
            assist = dict(decision.model_assist or {})
            assist["semantic_policy"] = missing_policy.to_dict()
            signals = self._dedupe(
                [
                    *(decision.signals or []),
                    *missing_policy.policy_signals,
                    "policy:semantic_frame_required",
                ]
            )
            effectful_route = has_effectful_candidate(decision)
            if effectful_route:
                projected = replace(
                    decision,
                    route=Route.ASK_USER,
                    risk_level=self._max_risk(RiskLevel.R1, decision.risk_level),
                    reason="semantic frame is required before capability execution",
                    requires_confirmation=False,
                    selected_probe=None,
                    capability="ask_user",
                    needs_probe=False,
                    needs_agent=False,
                    needs_user_confirmation=False,
                    memory_policy="forget",
                    required_capabilities=[],
                    signals=signals,
                    constraints=self._dedupe(
                        [
                            *(decision.constraints or []),
                            "do not execute capabilities without a semantic frame",
                        ]
                    ),
                    model_assist=assist,
                )
                return project_nonexecuting_route(
                    projected,
                    route=Route.ASK_USER,
                    reason="semantic frame is required before capability execution",
                    original=decision,
                    signal="policy:semantic_frame_missing_nonexecution",
                )
            decision.model_assist = assist
            decision.signals = signals
            if decision.memory_policy != "forget":
                decision.memory_policy = "forget"
            return decision
        policy = self.semantic_policy.compile(frame, current_user_id=current_user_id)
        frame_payload = frame.model_dump(mode="json") if hasattr(frame, "model_dump") else frame
        assist = dict(decision.model_assist or {})
        assist["semantic_frame"] = frame_payload if isinstance(frame_payload, dict) else {}
        assist["semantic_policy"] = policy.to_dict()
        if policy.capability_arguments:
            assist["probe_params"] = dict(policy.capability_arguments)

        signals = self._dedupe([*(decision.signals or []), *policy.policy_signals, "policy:semantic_frame_authoritative"])
        constraints = self._dedupe(
            [
                *(decision.constraints or []),
                "only semantic-policy-authorized capabilities may execute",
                "quoted, reported, hypothetical, and negated acts cannot authorize side effects",
            ]
        )

        hard_evidence_gap = (
            decision.route == Route.ASK_USER
            and isinstance(decision.capability_request, dict)
            and str(decision.capability_request.get("capability") or "") in {"vision", "attachment_content"}
        )
        if hard_evidence_gap:
            assist["semantic_policy"] = {
                **policy.to_dict(),
                "preferred_route": Route.ASK_USER.value,
                "route_reason": "attachment content is unavailable to the understanding layer",
                "requires_clarification": True,
                "clarification_reason": "the attachment content must be supplied or made readable",
                "allowed_capabilities": ["ask_user"],
                "allowed_effects": [],
            }
            decision.model_assist = assist
            decision.signals = self._dedupe([*signals, "policy:hard_evidence_gap_preserved"])
            decision.constraints = constraints
            decision.memory_policy = "forget"
            return decision

        false_positive_action_words = any(
            signal in policy.policy_signals
            for signal in (
                "semantic_policy:explicit_denial",
                "semantic_policy:ignored_quoted",
                "semantic_policy:ignored_quotation",
                "semantic_policy:ignored_reported",
                "semantic_policy:ignored_reported_speech",
                "semantic_policy:ignored_hypothetical",
                "semantic_policy:ignored_example",
            )
        )
        required_rule_probe = bool(
            decision.route == Route.PROBE
            and decision.selected_probe
            and decision.selected_probe != "search_probe"
            and (decision.freshness_required or decision.needs_probe)
            and "policy:required_probe_preserved" in (decision.signals or [])
            and not false_positive_action_words
        )
        safety_locked = decision.route in {Route.BLOCK, Route.HUMAN_REVIEW, Route.ROLLBACK}
        if (
            decision.route in {Route.HUMAN_REVIEW, Route.ROLLBACK}
            and policy.requires_clarification
            and not policy.allowed_effects
            and not false_positive_action_words
        ):
            # An unresolved or degraded semantic frame is not enough to create
            # a durable Review.  Ask for clarification while retaining the
            # original Veyra risk classification and every destructive-action
            # constraint.  This is a no-effect terminal for the current turn,
            # not a risk downgrade.
            projected = replace(
                decision,
                route=Route.ASK_USER,
                risk_level=decision.risk_level,
                reason=(
                    "semantic clarification required before opening a "
                    "governance review"
                ),
                requires_confirmation=False,
                selected_probe=None,
                capability="ask_user",
                needs_probe=False,
                needs_agent=False,
                needs_user_confirmation=False,
                memory_policy="forget",
                required_capabilities=[],
                signals=self._dedupe(
                    [
                        *signals,
                        "policy:safety_risk_preserved_during_clarification",
                    ]
                ),
                constraints=self._dedupe(
                    [
                        *constraints,
                        "do not create a review or execute before semantic clarification",
                    ]
                ),
                model_assist=assist,
            )
            return project_nonexecuting_route(
                projected,
                route=Route.ASK_USER,
                reason="semantic clarification required before opening a governance review",
                original=decision,
                signal="policy:safety_clarification_nonexecution",
            )
        if safety_locked and not (
            false_positive_action_words and not policy.allowed_effects
        ):
            decision.model_assist = assist
            decision.signals = signals
            decision.constraints = constraints
            return decision

        preferred = policy.preferred_route
        route = decision.route
        selected_probe = decision.selected_probe
        capability = decision.capability
        capability_request = dict(decision.capability_request or {})
        required_capabilities = list(decision.required_capabilities)
        needs_probe = decision.needs_probe
        needs_agent = decision.needs_agent
        needs_confirmation = decision.needs_user_confirmation
        requires_confirmation = decision.requires_confirmation
        memory_policy = decision.memory_policy
        risk = decision.risk_level
        intent = decision.intent
        reason = decision.reason
        reasoning_mode = decision.reasoning_mode
        candidate_effectful = has_effectful_candidate(decision)

        if preferred == Route.ASK_USER.value:
            route = Route.ASK_USER
            selected_probe = None
            capability = "ask_user"
            required_capabilities = []
            needs_probe = False
            needs_agent = False
            needs_confirmation = False
            requires_confirmation = False
            memory_policy = "forget"
            if not policy.allowed_effects and risk != RiskLevel.R5:
                risk = RiskLevel.R1
            reason = f"semantic clarification required: {policy.clarification_reason or 'unresolved intent'}"
        elif preferred == Route.PROBE.value:
            route = Route.PROBE
            selected_probe = policy.selected_probe
            capability = "probe"
            required_capabilities = list(policy.allowed_capabilities)
            needs_probe = True
            needs_agent = False
            requires_confirmation = False
            memory_policy = "forget"
            risk = self._max_risk(RiskLevel.R1, risk) if risk in {RiskLevel.R0, RiskLevel.R1} else risk
            reason = f"semantic frame requires fresh evidence via {policy.selected_probe}"
        elif preferred == Route.AGENT.value:
            route = Route.AGENT
            selected_probe = None
            capability = "selected_agent_runtime"
            required_capabilities = ["selected_agent_runtime"]
            needs_probe = False
            needs_agent = True
            memory_policy = "short_term" if memory_policy == "forget" else memory_policy
            reason = "semantic frame explicitly authorizes governed execution"
        else:
            if required_rule_probe:
                # A semantic frame may narrow an effectful route, but it must
                # not turn a Veyra-owned freshness invariant into an answer
                # from stale context.  This preserves only an already-selected
                # deterministic read-only Probe; it never creates a new tool,
                # Agent, or side-effect authority from user text.
                route = Route.PROBE
                capability = "probe"
                probe_capability = (
                    self.capabilities.capability_for_probe(selected_probe)
                    if self.capabilities is not None
                    else None
                )
                required_capabilities = self._dedupe(
                    [
                        item
                        for item in [*required_capabilities, probe_capability]
                        if item and item != "probe"
                    ]
                )
                needs_probe = True
                needs_agent = False
                needs_confirmation = False
                requires_confirmation = False
                memory_policy = "forget"
                reason = "Veyra freshness policy preserves the required read-only probe"
                signals = self._dedupe(
                    [*signals, "policy:semantic_cannot_weaken_required_probe"]
                )
            else:
                route = Route.DIRECT_ANSWER
                selected_probe = None
                capability = "native_answer"
                required_capabilities = ["native_answer"]
                needs_probe = False
                needs_agent = False
                needs_confirmation = False
                requires_confirmation = False
                reason = "semantic frame authorizes a read-only core response"
                if candidate_effectful:
                    signals = self._dedupe(
                        [*signals, "policy:capability_candidate_denied"]
                    )
                if false_positive_action_words and not policy.allowed_effects:
                    risk = RiskLevel.R0
                    signals = self._dedupe([*signals, "policy:mentioned_action_risk_downgraded"])
                reasoning_mode = "direct"

        if not policy.allows_effect("memory.write"):
            memory_policy = "forget"
            signals = self._dedupe([*signals, "policy:durable_memory_denied"])
            if intent == "preference":
                understood_intent = str(getattr(understanding, "intent", "") or "")
                intent = understood_intent if understood_intent and understood_intent != "preference" else "conversation"
        elif policy.allows_effect("memory.write"):
            memory_policy = "long_term"
            intent = "preference"
        if policy.requires_clarification:
            constraints = self._dedupe([*constraints, "do not produce side effects before clarification"])

        projected = replace(
            decision,
            route=route,
            risk_level=risk,
            reason=reason,
            requires_confirmation=requires_confirmation,
            selected_probe=selected_probe,
            capability=capability,
            freshness_required=route == Route.PROBE,
            needs_probe=needs_probe,
            needs_agent=needs_agent,
            needs_user_confirmation=needs_confirmation,
            memory_policy=memory_policy,
            reasoning_mode=reasoning_mode,
            intent=intent,
            required_capabilities=self._dedupe(required_capabilities),
            capability_request=capability_request,
            signals=signals,
            constraints=constraints,
            model_assist=assist,
        )
        if route in {Route.DIRECT_ANSWER, Route.ASK_USER} and candidate_effectful:
            return project_nonexecuting_route(
                projected,
                route=route,
                reason=reason,
                original=decision,
                signal="policy:capability_candidate_denied",
            )
        return projected

    def _apply_understanding_guardrails(
        self,
        text: str,
        decision: Decision,
        understanding: TurnUnderstanding | None,
    ) -> Decision:
        if understanding is None:
            return decision
        if not understanding.is_strategic_discussion():
            return decision
        if self._understanding_explicitly_needs_execution_or_runtime(text, understanding):
            return self._with_turn_understanding(decision, understanding)
        if decision.route not in {Route.AGENT, Route.PROBE, Route.SKILL} and not decision.needs_agent and not decision.needs_probe:
            return self._with_turn_understanding(decision, understanding)
        assist = dict(decision.model_assist or {})
        assist.update(
            {
                "recommended_route": Route.DIRECT_ANSWER.value,
                "reason": "understanding classified this as strategic discussion, not execution",
                "turn_understanding": understanding.to_dict(include_raw=False),
            }
        )
        projected = self._decision(
            route=Route.DIRECT_ANSWER,
            risk=decision.risk_level if decision.risk_level in {RiskLevel.R0, RiskLevel.R1} else decision.risk_level,
            reason="understanding-first guardrail: strategic discussion should stay in Veyra Core",
            intent="conversation",
            complexity="moderate" if decision.complexity == "complex" else decision.complexity,
            capability="native_answer",
            signals=self._dedupe(
                [
                    *(decision.signals or []),
                    "understanding:strategic_discussion",
                    "policy:understanding_before_route",
                    "policy:agent_route_rejected_for_strategy",
                ]
            ),
            memory_policy="short_term",
            reasoning_mode="direct",
            required_capabilities=["native_answer"],
            capability_request={
                "capability": "native_answer",
                "target_route": Route.DIRECT_ANSWER.value,
                "receiver_type": "core",
                "reason": "strategic discussion needs core reasoning, not probe/agent execution",
            },
            constraints=["do not execute tools or Agent for strategic discussion unless explicitly requested"],
            model_assist=assist,
        )
        return project_nonexecuting_route(
            projected,
            route=Route.DIRECT_ANSWER,
            reason=projected.reason,
            original=decision,
            signal="policy:strategic_discussion_nonexecution",
        )

    def _understanding_explicitly_needs_execution_or_runtime(self, text: str, understanding: TurnUnderstanding) -> bool:
        del text
        return understanding.requests_governed_effect_or_runtime()

    def _should_trust_rule_understanding(self, understanding: TurnUnderstanding | None) -> bool:
        if understanding is None or understanding.source != "rule_fallback" or understanding.confidence < 0.8:
            return False
        return understanding.suggested_mode in {
            "strategic_discussion",
            "meta_cognition_discussion",
            "runtime_evidence",
            "governed_execution",
        }

    def _adapter_contract_route_is_deterministic(self, decision: Decision) -> bool:
        """Skip model planning when the receiver is fixed by Veyra's adapter contract."""
        if decision.route in {Route.BLOCK, Route.HUMAN_REVIEW, Route.ROLLBACK}:
            return True
        if decision.route == Route.PROBE and decision.selected_probe and (decision.freshness_required or decision.needs_probe):
            return True
        if decision.route == Route.SKILL and decision.selected_probe:
            return True
        if decision.route == Route.AGENT and decision.needs_agent and decision.capability == "selected_agent_runtime":
            return True
        return False

    def _rule_decide(self, text: str, attention_focus: list[str], event: VeyraEvent | None = None) -> Decision:
        lowered = text.lower()
        risk = self._risk_for_text(lowered)
        intent, intent_signals = self._intent_for_text(lowered)
        complexity, complexity_signals = self._complexity_for_text(lowered, attention_focus)
        freshness = self._freshness_for_text(lowered)
        attachment_gap = self._attachment_capability_gap(event)
        signals = intent_signals + complexity_signals + self._risk_signals(risk)
        priority_decision = self._priority_intent_decision(
            lowered=lowered,
            risk=risk,
            complexity=complexity,
            signals=signals,
        )
        if priority_decision:
            return priority_decision
        preference_decision = self._preference_decision(
            lowered=lowered,
            risk=risk,
            complexity=complexity,
            signals=signals,
        )
        if preference_decision:
            return preference_decision
        if attachment_gap:
            return self._decision(
                route=Route.ASK_USER,
                risk=RiskLevel.R1,
                reason=str(attachment_gap.get("reason")),
                intent=intent if intent != "unknown" else "information",
                complexity=complexity,
                capability="ask_user",
                signals=signals + ["freshness:attachment_content", "capability:vision"],
                freshness_required=True,
                memory_policy="forget",
                reasoning_mode="evidence",
                required_capabilities=["vision"],
                capability_request=attachment_gap,
                constraints=["do not infer unreadable attachment content"],
            )
        if self._is_rollback_request(lowered):
            return self._decision(
                route=Route.ROLLBACK,
                risk=RiskLevel.R4,
                reason="snapshot rollback requires guarded confirmation",
                intent="action",
                complexity="moderate",
                capability="rollback_audit",
                signals=signals + ["route:rollback"],
                requires_confirmation=True,
                needs_user_confirmation=True,
                memory_policy="short_term",
                reasoning_mode="execution",
                required_capabilities=["rollback_audit"],
                constraints=["restore only an existing snapshot", "record rollback trace", "verify restored checksum"],
            )
        if risk == RiskLevel.R5:
            return self._decision(
                route=Route.BLOCK,
                risk=risk,
                reason="destructive or forbidden action detected",
                intent=intent,
                complexity=complexity,
                capability="guardian",
                signals=signals,
                reasoning_mode="execution" if intent in {"action", "implementation"} else "direct",
                required_capabilities=["guardian"],
                constraints=["block unsafe action", "require safer alternative"],
            )
        if risk in {RiskLevel.R3, RiskLevel.R4}:
            return self._decision(
                route=Route.HUMAN_REVIEW,
                risk=risk,
                reason="medium/high risk action requires review",
                intent=intent,
                complexity=complexity,
                capability="human_review",
                signals=signals,
                requires_confirmation=True,
                needs_user_confirmation=True,
                memory_policy="short_term",
                reasoning_mode="execution",
                required_capabilities=["human_review"],
                constraints=["explain impact", "check rollback path", "wait for explicit approval"],
            )
        skill = self._skill_for_text(lowered)
        if skill:
            skill_risk = RiskLevel.R1 if skill != "safe_git_commit" else RiskLevel.R2
            skill_capability = self._capability_for_skill(skill)
            return self._decision(
                route=Route.SKILL,
                risk=skill_risk,
                reason=f"built-in skill selected: {skill}",
                intent=intent,
                complexity=complexity,
                capability="skill",
                signals=signals + [f"skill:{skill}"],
                selected_probe=skill,
                needs_probe=skill_risk == RiskLevel.R1,
                memory_policy="short_term" if skill_risk == RiskLevel.R1 else "long_term",
                reasoning_mode="execution" if skill_risk != RiskLevel.R1 else "evidence",
                required_capabilities=[skill_capability] if skill_capability else [],
                constraints=["run fixed workflow", "record evidence"],
            )
        if freshness["required"]:
            probe = str(freshness.get("probe") or "")
            capability = str(freshness.get("capability") or "")
            capability_available = self._capability_available(capability)
            route = Route.PROBE if probe and capability_available else Route.ASK_USER
            constraints = ["refresh volatile evidence before answering"] if route == Route.PROBE else ["do not fabricate unavailable live evidence"]
            return self._decision(
                route=route,
                risk=RiskLevel.R1,
                reason=str(freshness.get("reason") or "fresh evidence required"),
                intent=intent,
                complexity=complexity,
                capability="probe" if route == Route.PROBE else "ask_user",
                signals=signals + [f"freshness:{freshness.get('name')}", f"capability:{capability}"],
                selected_probe=probe or None,
                freshness_required=True,
                needs_probe=route == Route.PROBE,
                memory_policy="forget",
                reasoning_mode="evidence",
                required_capabilities=[capability] if capability else [],
                capability_request={
                    "capability": capability,
                    "probe": probe or None,
                    "reason": freshness.get("reason"),
                },
                constraints=constraints,
            )
        if self._needs_agent(lowered, attention_focus, complexity):
            return self._decision(
                route=Route.AGENT,
                risk=risk,
                reason="complex task requires selected agent runtime",
                intent=intent,
                complexity=complexity,
                capability="selected_agent_runtime",
                signals=signals + ["agent_required"],
                needs_agent=True,
                memory_policy="short_term",
                reasoning_mode="execution",
                required_capabilities=["selected_agent_runtime"],
                constraints=["inject context patch", "enforce policy patch", "verify result"],
            )
        if risk == RiskLevel.R2 and intent == "action":
            return self._decision(
                route=Route.AGENT,
                risk=risk,
                reason="low-risk write action requires governed execution",
                intent=intent,
                complexity=complexity,
                capability="selected_agent_runtime",
                signals=signals + ["agent_required", "write_action"],
                needs_agent=True,
                memory_policy="long_term",
                reasoning_mode="execution",
                required_capabilities=["selected_agent_runtime"],
                constraints=["limit write scope", "record diff or snapshot", "verify result"],
            )
        return self._decision(
            route=Route.DIRECT_ANSWER,
            risk=RiskLevel.R0 if risk == RiskLevel.R0 else risk,
            reason="low complexity informational request",
            intent=intent,
            complexity=complexity,
            capability="native_answer",
            signals=signals,
            memory_policy="forget",
            reasoning_mode="direct",
            required_capabilities=["native_answer"],
            constraints=["no tool execution"],
        )

    def _apply_model_assist(self, text: str, attention_focus: list[str], base: Decision, event: VeyraEvent | None = None) -> Decision:
        if not self.reasoning:
            return base
        if self._is_governance_locked(base):
            return base
        if self._is_preference_locked(base):
            return base
        assist = self.reasoning.decision_assist(text=text, attention_focus=attention_focus, rule_decision=base.to_dict(), event=event)
        if assist.get("status") != "model_assisted":
            return base

        route_decision = assist.get("route_decision") if isinstance(assist.get("route_decision"), dict) else {}
        risk_payload = assist.get("risk") if isinstance(assist.get("risk"), dict) else {}
        reply_strategy = assist.get("reply_strategy") if isinstance(assist.get("reply_strategy"), dict) else {}
        capability_request_payload = assist.get("capability_request") if isinstance(assist.get("capability_request"), dict) else {}
        model_risk = safe_model_risk(risk_payload.get("level") or assist.get("risk_level"), base.risk_level)
        risk = self._max_risk(base.risk_level, model_risk)
        candidate_route = self._route_from_model(
            assist.get("recommended_route")
            or assist.get("route")
            or route_decision.get("route")
            or capability_request_payload.get("target_route"),
            base.route,
        )
        policy_signals: list[str] = []
        candidate_route, preserved_signal = self._preserve_rule_route(base, candidate_route)
        if preserved_signal:
            policy_signals.append(preserved_signal)
        model_freshness_required = bool(assist.get("freshness_required") or assist.get("needs_probe"))
        if (base.freshness_required or model_freshness_required) and candidate_route == Route.DIRECT_ANSWER:
            if base.selected_probe or self._selected_probe_from_model(assist, None):
                candidate_route = Route.PROBE
                # A model-requested observation is only a candidate.  Keep its
                # provenance distinct from the deterministic signal installed
                # by ``decide`` for a Veyra-owned freshness rule; otherwise a
                # generic model evidence gap can bypass SemanticPolicy.
                policy_signals.append("policy:model_freshness_probe_requested")
            else:
                candidate_route = Route.ASK_USER
                policy_signals.append("policy:freshness_gap_asks_user")
        if candidate_route == Route.AGENT and not self._agent_allowed_by_policy(text, base, assist):
            candidate_route = Route.DIRECT_ANSWER
            policy_signals.append("policy:agent_route_rejected")
        if risk == RiskLevel.R5 or candidate_route == Route.BLOCK:
            route = Route.BLOCK
            risk = self._max_risk(risk, RiskLevel.R5)
            if candidate_route == Route.BLOCK:
                policy_signals.append("policy:model_block_honored")
        elif risk in {RiskLevel.R3, RiskLevel.R4}:
            route = Route.HUMAN_REVIEW
        elif candidate_route == Route.HUMAN_REVIEW:
            route = Route.HUMAN_REVIEW
            risk = self._max_risk(risk, RiskLevel.R3)
        elif candidate_route == Route.ASK_USER:
            route = Route.ASK_USER
        elif candidate_route == Route.ROLLBACK:
            route = Route.ROLLBACK
            risk = self._max_risk(risk, RiskLevel.R4)
        else:
            route = candidate_route

        selected_probe = base.selected_probe
        if route == Route.PROBE:
            selected_probe = self._selected_probe_from_model(assist, base.selected_probe)
        elif route == Route.SKILL:
            selected_probe = self._selected_skill_from_model(assist, base.selected_probe)

        reason = str(assist.get("reason") or assist.get("rationale") or "model-assisted core reasoning")
        model_signal_hints = self._string_list(assist.get("signals"))
        if model_signal_hints:
            # Signals are consumed as server-owned policy provenance elsewhere
            # in the control path.  Model strings therefore remain untrusted
            # hints and must never enter ``Decision.signals`` verbatim.
            policy_signals.append("model:untrusted_signals_ignored")
        signals = self._dedupe(base.signals + policy_signals + ["model:decision"])
        constraints = self._dedupe(base.constraints + self._string_list(assist.get("constraints")) + ["guardian remains final execution boundary"])
        capability_request = capability_request_payload
        required_capabilities = self._dedupe(
            base.required_capabilities
            + self._string_list(assist.get("required_capabilities"))
            + self._capabilities_from_route(route, selected_probe)
        )
        memory_policy = normalize_memory_policy(
            self._memory_policy_alias(assist.get("memory_policy") or reply_strategy.get("memory_policy")),
            default=base.memory_policy,
        )
        freshness_required = bool(assist.get("freshness_required", base.freshness_required))
        needs_probe = bool(assist.get("needs_probe", route == Route.PROBE or base.needs_probe))
        needs_agent = bool(assist.get("needs_agent", route == Route.AGENT or base.needs_agent))
        needs_user_confirmation = bool(assist.get("needs_user_confirmation", route == Route.HUMAN_REVIEW or base.needs_user_confirmation))
        reasoning_mode = self._normalize_reasoning_mode(assist.get("reasoning_mode"), default=base.reasoning_mode)
        model_assist = {
            "status": "model_assisted",
            "reason": reason,
            "recommended_route": route.value,
            "risk_level": risk.value,
            "freshness_required": freshness_required,
            "needs_probe": needs_probe,
            "needs_agent": needs_agent,
            "needs_user_confirmation": needs_user_confirmation,
            "reasoning_mode": reasoning_mode,
            "confidence": assist.get("confidence"),
            "solution_outline": self._string_list(assist.get("solution_outline")),
            "agent_context": assist.get("agent_context") if isinstance(assist.get("agent_context"), dict) else {},
            "memory_policy": memory_policy,
            "context_gaps": self._string_list(assist.get("context_gaps")),
            "needs_observation": bool(assist.get("needs_observation")),
            "required_capabilities": required_capabilities,
            "capability_request": capability_request,
            "draft_response": str(reply_strategy.get("draft_response") or assist.get("draft_response") or assist.get("response") or "")[:2000],
            "situation_assessment": assist.get("situation_assessment") if isinstance(assist.get("situation_assessment"), dict) else {},
            "route_decision": route_decision,
            "reply_strategy": reply_strategy,
        }
        return self._decision(
            route=route,
            risk=risk,
            reason=f"model-assisted: {reason}; rule baseline: {base.reason}",
            intent=str(assist.get("intent") or base.intent),
            complexity=str(assist.get("complexity") or base.complexity),
            capability=self._capability_for_route(route, base.capability),
            signals=signals,
            requires_confirmation=base.requires_confirmation or risk in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
            selected_probe=selected_probe,
            target_agent=str(assist.get("target_agent") or base.target_agent or "") or None,
            freshness_required=freshness_required,
            needs_probe=needs_probe,
            needs_agent=needs_agent,
            needs_user_confirmation=needs_user_confirmation,
            memory_policy=memory_policy,
            reasoning_mode=reasoning_mode,
            required_capabilities=required_capabilities,
            capability_request=capability_request,
            constraints=constraints,
            model_assist=model_assist,
        )

    def _freshness_for_text(self, lowered: str) -> dict[str, Any]:
        for rule in FRESHNESS_RULES:
            markers = tuple(str(marker).lower() for marker in rule.get("markers", ()))
            if not any(self._contains_freshness_marker(lowered, marker) for marker in markers):
                continue
            anti_markers = tuple(str(marker).lower() for marker in rule.get("anti_markers", ()))
            if any(self._contains_freshness_marker(lowered, marker) for marker in anti_markers):
                continue
            if rule.get("requires_volatile_marker") and not any(
                self._contains_freshness_marker(lowered, marker) for marker in VOLATILE_MARKERS
            ):
                continue
            capability = str(rule.get("capability") or "")
            return {
                "required": True,
                "name": rule.get("name"),
                "probe": rule.get("probe"),
                "capability": capability,
                "reason": f"{rule.get('name')} requires fresh evidence via {capability}",
            }
        return {"required": False}

    def _contains_freshness_marker(self, text: str, marker: str) -> bool:
        """Match English tokens as words so `time` does not match `runtime`."""
        if re.fullmatch(r"[a-z0-9_ ]+", marker) and re.search(r"[a-z]", marker):
            return re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", text) is not None
        return marker in text

    def _priority_intent_decision(self, *, lowered: str, risk: RiskLevel, complexity: str, signals: list[str]) -> Decision | None:
        if risk in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            return None
        if not self._is_identity_or_governance_intent(lowered):
            return None
        return self._decision(
            route=Route.DIRECT_ANSWER,
            risk=RiskLevel.R0,
            reason="governance identity intent takes priority over runtime keyword probes",
            intent="identity",
            complexity=complexity,
            capability="native_answer",
            signals=signals + ["intent:identity", "source:governance_identity", "priority:identity"],
            memory_policy="forget",
            reasoning_mode="direct",
            required_capabilities=["native_answer"],
            constraints=["answer governance identity only", "do not probe runtime keywords"],
        )

    def _is_identity_or_governance_intent(self, lowered: str) -> bool:
        has_identity_form = any(marker in lowered for marker in IDENTITY_MARKERS)
        has_entity = any(entity in lowered for entity in GOVERNANCE_ENTITIES)
        asks_choice = "还是" in lowered or " or " in lowered
        return has_identity_form and (has_entity or asks_choice)

    def _is_governance_locked(self, decision: Decision) -> bool:
        return decision.intent in {"identity", "governance"} or "source:governance_identity" in decision.signals

    def _preference_decision(self, *, lowered: str, risk: RiskLevel, complexity: str, signals: list[str]) -> Decision | None:
        if risk != RiskLevel.R0:
            return None
        if not self._is_preference_intent(lowered):
            return None
        return self._decision(
            route=Route.DIRECT_ANSWER,
            risk=RiskLevel.R0,
            reason="long-term user preference detected",
            intent="preference",
            complexity=complexity,
            capability="native_answer",
            signals=signals + ["intent:preference", "memory:preference", "priority:memory_policy"],
            memory_policy="long_term",
            reasoning_mode="direct",
            required_capabilities=["native_answer"],
            constraints=["acknowledge preference", "write preference memory patch", "no tool execution"],
        )

    def _is_preference_intent(self, lowered: str) -> bool:
        has_scope = any(marker in lowered for marker in PREFERENCE_SCOPE_MARKERS)
        has_content = any(marker in lowered for marker in PREFERENCE_CONTENT_MARKERS)
        return has_scope and has_content

    def _is_preference_locked(self, decision: Decision) -> bool:
        return decision.intent == "preference" or "memory:preference" in decision.signals

    def _enforce_preference_lock(self, text: str, decision: Decision) -> Decision:
        """Deterministic guardrail: explicit user preferences must be remembered.

        The model-first pipeline sometimes routes a clear preference statement as a
        plain reply but marks it 'forget'. When the text is unambiguously a
        preference and the route is a low-risk reply, lock intent/memory so the
        preference is written to long-term memory regardless of model judgment.
        """
        if self._is_preference_locked(decision):
            return self._lock_preference_memory(decision)
        if decision.route not in {Route.DIRECT_ANSWER, Route.ASK_USER}:
            return decision
        if decision.risk_level not in {RiskLevel.R0, RiskLevel.R1}:
            return decision
        if not self._is_preference_intent((text or "").lower()):
            return decision
        return self._lock_preference_memory(decision)

    def _lock_preference_memory(self, decision: Decision) -> Decision:
        decision.intent = "preference"
        decision.memory_policy = "long_term"
        decision.signals = self._dedupe(
            [*(decision.signals or []), "intent:preference", "memory:preference", "priority:memory_policy"]
        )
        assist = dict(decision.model_assist or {})
        if assist:
            assist["memory_policy"] = "long_term"
            plan = assist.get("decision_plan") if isinstance(assist.get("decision_plan"), dict) else {}
            if plan:
                plan["intent"] = "preference"
                assist["decision_plan"] = plan
            decision.model_assist = assist
        return decision

    def _attachment_capability_gap(self, event: VeyraEvent | None) -> dict[str, Any] | None:
        if not event:
            return None
        metadata = event.payload.get("metadata") if isinstance(event.payload, dict) else {}
        if not isinstance(metadata, dict):
            return None
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        message_type = str(feishu.get("message_type") or metadata.get("message_type") or "text")
        if message_type == "text":
            return None
        if feishu.get("content_text") or metadata.get("content_text"):
            return None
        return {
            "capability": "vision",
            "reason": "attachment content is not readable in the current turn context",
            "message_type": message_type,
        }

    def _skill_for_text(self, lowered: str) -> str | None:
        if "诊断" in lowered and "openclaw" in lowered:
            return "diagnose_openclaw"
        if "检查端口" in lowered or "check_port" in lowered:
            return "check_port"
        if "总结日志" in lowered or "summarize_logs" in lowered:
            return "summarize_logs"
        if "安全提交" in lowered or "safe_git_commit" in lowered:
            return "safe_git_commit"
        return None

    def _needs_agent(self, lowered: str, attention_focus: list[str], complexity: str) -> bool:
        execution_markers = [
            "修改",
            "实现",
            "开发",
            "重构",
            "修复",
            "调试",
            "多文件",
            "代码",
            "部署",
            "执行",
            "写入",
            "创建",
            "code edit",
            "implement",
            "debug",
            "fix bug",
        ]
        explicit_agent_markers = [
            "通过 openclaw",
            "让 openclaw",
            "用 openclaw",
            "openclaw 回答",
            "通过 hermes",
            "让 hermes",
            "用 hermes",
            "agent 执行",
        ]
        if any(marker in lowered for marker in execution_markers + explicit_agent_markers):
            return True
        if any(marker in lowered for marker in ("加一个", "新增", "添加")) and any(
            marker in lowered for marker in ("veyra", "router", "模块", "功能", "页面", "ui", "代码")
        ):
            return True
        if complexity == "complex" and any(marker in lowered for marker in ("执行", "修改", "实现", "代码", "部署", "debug", "implement")):
            return True
        return len(attention_focus) >= 3 and any(marker in lowered for marker in execution_markers)

    def _capability_available(self, capability_id: str) -> bool:
        if not capability_id:
            return False
        if not self.capabilities:
            return True
        return self.capabilities.is_available(capability_id)

    def _capability_for_skill(self, skill_name: str) -> str | None:
        if not self.capabilities:
            return None
        return self.capabilities.capability_for_skill(skill_name)

    def _is_rollback_request(self, lowered: str) -> bool:
        rollback_markers = ["rollback", "restore snapshot", "restore snap_", "回滚", "恢复快照"]
        return any(marker in lowered for marker in rollback_markers)

    def _risk_for_text(self, lowered: str) -> RiskLevel:
        return classify_text_risk(lowered)

    def _intent_for_text(self, lowered: str) -> tuple[str, list[str]]:
        action_markers = ["检查", "查看", "看看", "排查", "修复", "执行", "修改", "部署", "重启", "创建", "写入", "诊断"]
        if any(marker in lowered for marker in action_markers):
            return "action", ["intent:action"]
        if any(marker in lowered for marker in ["为什么", "是什么", "解释", "说明", "总结"]):
            return "information", ["intent:information"]
        return "unknown", []

    def _complexity_for_text(self, lowered: str, attention_focus: list[str]) -> tuple[str, list[str]]:
        signals: list[str] = []
        if len(lowered) > 120:
            signals.append("long_request")
        if any(marker in lowered for marker in ["架构", "多步骤", "整体", "完整", "逐步", "跨文件"]):
            signals.append("multi_step")
        if len(attention_focus) >= 3:
            signals.append("broad_attention")
        if "multi_step" in signals or len(signals) >= 2:
            return "complex", [f"complexity:{signal}" for signal in signals]
        if signals:
            return "moderate", [f"complexity:{signal}" for signal in signals]
        return "simple", []

    def _risk_signals(self, risk: RiskLevel) -> list[str]:
        return [f"risk:{risk.value}"]

    def _route_from_model(self, value: Any, default: Route) -> Route:
        if not value:
            return default
        alias = {
            "agent_reasoning": Route.AGENT,
            "agent_proposal": Route.AGENT,
            "core": Route.DIRECT_ANSWER,
            "user": Route.ASK_USER,
            "guardian": Route.HUMAN_REVIEW,
        }.get(str(value).strip().lower())
        if alias:
            return alias
        try:
            route = Route(str(value))
        except ValueError:
            return default
        if route == Route.NATIVE_TOOL:
            return Route.HUMAN_REVIEW
        return route

    def _selected_probe_from_model(self, assist: dict[str, Any], default: str | None) -> str:
        capability_request = assist.get("capability_request") if isinstance(assist.get("capability_request"), dict) else {}
        selected = str(
            assist.get("selected_probe")
            or assist.get("probe")
            or capability_request.get("probe")
            or capability_request.get("executor")
            or capability_request.get("capability_id")
            or ""
        )
        normalized = self._canonical_probe_name(selected)
        if normalized:
            return normalized
        fallback = self._canonical_probe_name(default or "")
        return fallback or str(default or "").strip()

    def _agent_allowed_by_policy(self, text: str, base: Decision, assist: dict[str, Any]) -> bool:
        lowered = text.lower()
        route_markers = [
            "修改",
            "实现",
            "开发",
            "重构",
            "修复",
            "调试",
            "多文件",
            "代码",
            "部署",
            "接入",
            "深入分析",
            "综合判断",
            "复杂判断",
            "agent",
            "openclaw",
            "hermes",
        ]
        assist_intent = str(assist.get("intent") or "").lower()
        assist_complexity = str(assist.get("complexity") or base.complexity).lower()
        route_decision = assist.get("route_decision") if isinstance(assist.get("route_decision"), dict) else {}
        answer_source = str(route_decision.get("answer_source") or assist.get("answer_source") or "").lower()
        if base.route == Route.AGENT or "agent_required" in base.signals:
            return True
        if any(marker in lowered for marker in route_markers):
            return True
        if answer_source in {"agent", "agent_proposal"}:
            return assist_complexity in {"moderate", "complex"} or assist_intent in {"information", "action", "implementation", "execution"}
        return assist_intent in {"action", "implementation", "execution"} and assist_complexity in {"moderate", "complex"}

    def _memory_policy_alias(self, value: Any) -> Any:
        if isinstance(value, str):
            return {"none": "forget", "read": "short_term", "write_candidate": "long_term"}.get(value, value)
        return value

    def _selected_skill_from_model(self, assist: dict[str, Any], default: str | None) -> str:
        allowed = {"diagnose_openclaw", "check_port", "summarize_logs", "safe_git_commit"}
        capability_request = assist.get("capability_request") if isinstance(assist.get("capability_request"), dict) else {}
        selected = str(
            assist.get("selected_skill")
            or assist.get("skill")
            or capability_request.get("skill")
            or capability_request.get("executor")
            or default
            or ""
        )
        return selected if selected in allowed else (default or "")

    def _canonical_probe_name(self, value: str) -> str:
        token = str(value or "").strip().lower()
        if not token:
            return ""
        aliases = {
            "time_probe": "time",
            "system_probe": "system",
            "git_probe": "git",
            "port_probe": "port",
            "process_probe": "process",
            "file_probe": "file",
            "log_probe": "log",
            "network_probe": "network",
            "web_probe": "web",
            "web_url_probe": "web",
            "openclaw_probe": "openclaw",
            "hermes_probe": "hermes",
            "mcp_probe": "mcp",
            "weather": "weather_probe",
        }
        canonical = aliases.get(token, token)
        allowed = {"time", "system", "git", "port", "process", "file", "log", "network", "web", "openclaw", "hermes", "mcp", "weather_probe", "search_probe"}
        return canonical if canonical in allowed else ""

    def _preserve_rule_route(self, base: Decision, candidate: Route) -> tuple[Route, str]:
        if candidate == base.route:
            return candidate, ""
        if base.route == Route.AGENT and candidate in {Route.DIRECT_ANSWER, Route.PROBE, Route.SKILL}:
            return Route.AGENT, "policy:rule_agent_route_preserved"
        if base.route == Route.PROBE and candidate in {Route.DIRECT_ANSWER, Route.AGENT, Route.SKILL}:
            return Route.PROBE, "policy:rule_probe_route_preserved"
        if base.route == Route.SKILL and candidate in {Route.DIRECT_ANSWER, Route.PROBE, Route.AGENT}:
            return Route.SKILL, "policy:rule_skill_route_preserved"
        if base.route == Route.ASK_USER and candidate == Route.DIRECT_ANSWER:
            return Route.ASK_USER, "policy:rule_ask_user_route_preserved"
        return candidate, ""

    def _capability_for_route(self, route: Route, default: str) -> str:
        return {
            Route.DIRECT_ANSWER: "native_answer",
            Route.PROBE: "probe",
            Route.SKILL: "skill",
            Route.AGENT: "selected_agent_runtime",
            Route.ASK_USER: "ask_user",
            Route.HUMAN_REVIEW: "human_review",
            Route.BLOCK: "guardian",
            Route.ROLLBACK: "rollback_audit",
        }.get(route, default)

    def _capabilities_from_route(self, route: Route, selected: str | None) -> list[str]:
        if route == Route.PROBE and self.capabilities:
            capability = self.capabilities.capability_for_probe(selected)
            return [capability] if capability else []
        if route == Route.SKILL and self.capabilities:
            capability = self.capabilities.capability_for_skill(selected)
            return [capability] if capability else []
        if route == Route.AGENT:
            return ["selected_agent_runtime"]
        if route == Route.DIRECT_ANSWER:
            return ["native_answer"]
        if route == Route.HUMAN_REVIEW:
            return ["human_review"]
        if route == Route.BLOCK:
            return ["guardian"]
        if route == Route.ROLLBACK:
            return ["rollback_audit"]
        return []

    def _normalize_reasoning_mode(self, value: Any, *, default: str) -> str:
        mode = str(value or default or "direct").strip().lower()
        return mode if mode in {"direct", "evidence", "execution"} else default

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        order = list(RiskLevel)
        return right if order.index(right) > order.index(left) else left

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value[:12] if item is not None]
        if isinstance(value, str) and value:
            return [value]
        return []

    def _dedupe(self, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            output.append(value)
        return output

    def _decision(
        self,
        *,
        route: Route,
        risk: RiskLevel,
        reason: str,
        intent: str,
        complexity: str,
        capability: str,
        signals: list[str],
        requires_confirmation: bool = False,
        selected_probe: str | None = None,
        target_agent: str | None = None,
        freshness_required: bool = False,
        needs_probe: bool = False,
        needs_agent: bool = False,
        needs_user_confirmation: bool = False,
        memory_policy: str = "forget",
        reasoning_mode: str = "direct",
        required_capabilities: list[str] | None = None,
        capability_request: dict[str, Any] | None = None,
        constraints: list[str] | None = None,
        model_assist: dict[str, Any] | None = None,
    ) -> Decision:
        return Decision(
            route=route,
            risk_level=risk,
            reason=reason,
            requires_confirmation=requires_confirmation,
            selected_probe=selected_probe,
            target_agent=target_agent,
            intent=intent,
            complexity=complexity,
            capability=capability,
            freshness_required=freshness_required,
            needs_probe=needs_probe,
            needs_agent=needs_agent,
            needs_user_confirmation=needs_user_confirmation,
            memory_policy=normalize_memory_policy(memory_policy),
            reasoning_mode=reasoning_mode if reasoning_mode in {"direct", "evidence", "execution"} else "direct",
            required_capabilities=required_capabilities or [],
            capability_request=capability_request or {},
            signals=signals,
            constraints=constraints or [],
            model_assist=model_assist or {},
        )
