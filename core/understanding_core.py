from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from core.awareness_context_assembler import AwarenessContextAssembler
from core.model_client import redact_sensitive
from core.prompt_loader import load_prompt
from core.reasoning_core import CoreReasoning
from core.semantic_frame import (
    ReferentResolution,
    SemanticAct,
    SemanticTarget,
    SourceQuote,
    TurnSemanticFrame,
    semantic_frame_quality_issues,
)
from core.semantic_policy import SemanticPolicyCompiler
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


TURN_UNDERSTANDING_SYSTEM_FALLBACK = (
    "You are Veyra Core's user understanding layer inside an awareness-driven runtime. "
    "The user message is the primary input. The awareness_snapshot contains Veyra's current perception "
    "from belief claims and local_world probe cache; treat it as useful but not automatically true or fresh. "
    "Return strict JSON only. Produce user understanding and awareness orientation, not a route decision. "
    "Start by understanding the person and situation: explicit request, hidden need, emotion, project, goal risk, "
    "constraints, capability needs, time scale, history links, and evidence gaps. "
    "Also produce a multi-act semantic_frame grounded in exact character spans from the user message. "
    "Do not execute probes or agents, do not write the final user reply, and do not treat missing evidence as fact."
)

TURN_UNDERSTANDING_SYSTEM = (
    load_prompt("core/turn_understanding.md", TURN_UNDERSTANDING_SYSTEM_FALLBACK)
    + "\n\nThe semantic_frame records meaning only. It must never contain route, risk, state_effect, "
    "allowed_capabilities, memory policy, or execution authorization. Preserve every independent act, "
    "including prohibitions, quotations, reported speech, corrections, alternatives, and conditions. "
    "kind, goal, operation, target type/value, authority, and evidence_need are open-world strings. "
    "For an act spoken directly by the current message author, emit the literal actor token "
    "speaker=user and authority=direct_user; do not put an authority token in speaker. "
    "Every source_quote must be an exact Python-style character slice of user_message: "
    "user_message[start:end] == text. One semantic act must contain only one independently assertable, "
    "deniable, or fulfillable goal. Never merge clauses with different polarity, authority, condition, "
    "or requested outcome into one operation string. For example, 不要每天推送天气，只告诉我现在上海天气 "
    "must be two acts: a negative recurring-push act and a positive current-weather query, connected by "
    "a contrast relation. session_context.anchor_candidates contains server-issued, event-bound context "
    "candidates. When and only when one candidate is the unique referent of an act, copy its exact kind "
    "into target.type, exact label into target.value, and exact candidate_token into "
    "target.attributes.anchor_candidate_token; never "
    "invent, alter, combine, or treat "
    "that token as authority. If no candidate is a unique match, omit the token and keep the narrowest stable "
    "semantic target or an explicit ambiguity. Keep situation fields concise so the complete JSON fits the "
    "output budget."
)

TURN_UNDERSTANDING_REPAIR_SYSTEM = (
    TURN_UNDERSTANDING_SYSTEM
    + "\nYour previous candidate was incomplete or invalid. Rebuild the entire JSON object from the original "
    "user_message. Treat validation_issues as defects to fix, not as semantic facts. Preserve atomic acts, "
    "opposite polarities, conditions, quotation/authority boundaries, and exact source slices. Return no prose."
)


@dataclass(slots=True)
class TurnUnderstanding:
    intent: str = "unknown"
    task_summary: str = ""
    user_goal: str = ""
    what_user_really_needs: str = ""
    task_type: str = "unknown"
    explicit_request: str = ""
    hidden_need: str = ""
    emotion: str = ""
    project: str = ""
    risk_to_goal: str = ""
    suggested_mode: str = ""
    constraints: list[str] = field(default_factory=list)
    capability_needs: list[str] = field(default_factory=list)
    time_scale: str = ""
    history_links: list[str] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    relevant_awareness: list[Any] = field(default_factory=list)
    stale_or_uncertain_awareness: list[Any] = field(default_factory=list)
    evidence_gap: dict[str, Any] = field(default_factory=dict)
    needs_fresh_evidence: bool = False
    evidence_kind: str = ""
    can_answer_from_world_state: bool = False
    retrieval_hints: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    source: str = ""
    semantic_frame: TurnSemanticFrame | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, source_text: str = "") -> "TurnUnderstanding":
        root_payload = payload
        if isinstance(payload.get("turn_understanding"), dict):
            payload = payload["turn_understanding"]
        situation = payload.get("situation_assessment") if isinstance(payload.get("situation_assessment"), dict) else {}
        if not situation:
            situation = payload.get("understanding") if isinstance(payload.get("understanding"), dict) else {}
        if not situation:
            situation = payload

        entities = _dict_from(payload.get("entities")) or _dict_from(situation.get("entities"))
        evidence_gap = _dict_from(situation.get("evidence_gap")) or _dict_from(payload.get("evidence_gap"))
        hints = _string_list(payload.get("retrieval_hints"), limit=8)
        if not hints:
            hints = _string_list(situation.get("retrieval_hints"), limit=8)

        confidence = _float_between(situation.get("confidence", payload.get("confidence") or 0.0))
        evidence_kind = str(evidence_gap.get("evidence_kind") or situation.get("evidence_kind") or payload.get("evidence_kind") or "")
        needs_fresh = bool(evidence_gap.get("needs_fresh_evidence", payload.get("needs_fresh_evidence")))
        can_answer = bool(situation.get("can_answer_from_current_context", payload.get("can_answer_from_world_state")))
        task_summary = str(situation.get("task_summary") or payload.get("task_summary") or payload.get("summary") or "")
        explicit_request = str(situation.get("explicit_request") or payload.get("explicit_request") or task_summary or "")
        hidden_need = str(
            situation.get("hidden_need")
            or payload.get("hidden_need")
            or situation.get("what_user_really_needs")
            or payload.get("what_user_really_needs")
            or ""
        )
        user_goal = str(situation.get("user_goal") or payload.get("user_goal") or explicit_request or task_summary or "")
        semantic_text = source_text or str(root_payload.get("user_message") or explicit_request or task_summary or "")
        semantic_frame = _semantic_frame_from_payload(root_payload, source_text=semantic_text)
        semantic_frame = _apply_model_clarification_gap(semantic_frame, situation)
        return cls(
            intent=str(situation.get("intent") or payload.get("intent") or "unknown"),
            task_summary=task_summary or explicit_request,
            user_goal=user_goal,
            what_user_really_needs=hidden_need,
            task_type=str(situation.get("task_type") or payload.get("task_type") or "unknown"),
            explicit_request=explicit_request,
            hidden_need=hidden_need,
            emotion=str(situation.get("emotion") or payload.get("emotion") or ""),
            project=str(situation.get("project") or payload.get("project") or ""),
            risk_to_goal=str(situation.get("risk_to_goal") or payload.get("risk_to_goal") or ""),
            suggested_mode=str(situation.get("suggested_mode") or payload.get("suggested_mode") or ""),
            constraints=_string_list(situation.get("constraints") or payload.get("constraints"), limit=8),
            capability_needs=_string_list(situation.get("capability_needs") or payload.get("capability_needs"), limit=8),
            time_scale=str(situation.get("time_scale") or payload.get("time_scale") or ""),
            history_links=_string_list(situation.get("history_links") or payload.get("history_links"), limit=8),
            entities=entities,
            relevant_awareness=_any_list(situation.get("relevant_awareness"), limit=12),
            stale_or_uncertain_awareness=_any_list(situation.get("stale_or_uncertain_awareness"), limit=12),
            evidence_gap=evidence_gap,
            needs_fresh_evidence=needs_fresh,
            evidence_kind=evidence_kind,
            can_answer_from_world_state=can_answer,
            retrieval_hints=hints,
            confidence=confidence,
            reason=str(situation.get("reason") or payload.get("reason") or ""),
            source=str(
                payload.get("source")
                or root_payload.get("source")
                or ("model" if root_payload.get("status") == "model_assisted" else "")
            ),
            semantic_frame=semantic_frame,
            raw=payload,
        )

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "intent": self.intent,
            "task_summary": self.task_summary,
            "user_goal": self.user_goal,
            "what_user_really_needs": self.what_user_really_needs,
            "task_type": self.task_type,
            "explicit_request": self.explicit_request,
            "hidden_need": self.hidden_need,
            "emotion": self.emotion,
            "project": self.project,
            "risk_to_goal": self.risk_to_goal,
            "suggested_mode": self.suggested_mode,
            "constraints": list(self.constraints),
            "capability_needs": list(self.capability_needs),
            "time_scale": self.time_scale,
            "history_links": list(self.history_links),
            "entities": self.entities,
            "relevant_awareness": self.relevant_awareness,
            "stale_or_uncertain_awareness": self.stale_or_uncertain_awareness,
            "evidence_gap": self.evidence_gap,
            "needs_fresh_evidence": self.needs_fresh_evidence,
            "evidence_kind": self.evidence_kind,
            "can_answer_from_world_state": self.can_answer_from_world_state,
            "retrieval_hints": list(self.retrieval_hints),
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "semantic_frame": self.semantic_frame.model_dump(mode="json") if self.semantic_frame is not None else None,
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    def compact(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "intent": self.intent,
                "task_summary": self.task_summary,
                "explicit_request": self.explicit_request,
                "hidden_need": self.hidden_need or self.what_user_really_needs,
                "emotion": self.emotion,
                "project": self.project,
                "risk_to_goal": self.risk_to_goal,
                "suggested_mode": self.suggested_mode,
                "task_type": self.task_type,
                "evidence_kind": self.evidence_kind,
                "needs_fresh_evidence": self.needs_fresh_evidence,
                "capability_needs": self.capability_needs,
                "confidence": self.confidence,
                "source": self.source,
                "semantic_frame": self.semantic_frame.compact() if self.semantic_frame is not None else None,
            },
            max_string=360,
            max_list=6,
        )

    def is_strategic_discussion(self) -> bool:
        return self.suggested_mode in {"strategic_discussion", "meta_cognition_discussion", "project_direction_review"}

    def requests_governed_effect_or_runtime(self) -> bool:
        """Use the resolved act graph, rather than words inside its target.

        A phrase such as ``讨论自扩展实现边界`` mentions implementation as
        the object of a discussion.  Treating the word ``实现`` itself as an
        execution command collapses that distinction.  For a validated model
        frame, the Veyra-owned semantic policy is the authority boundary.  A
        malformed model frame remains read-only.  Only the local rule fallback
        may use its already-derived task classification.
        """

        frame = self.semantic_frame
        if frame is not None and frame.source in {"model", "model_repair"}:
            policy = SemanticPolicyCompiler().compile(frame)
            if policy.preferred_route in {"agent", "probe"}:
                return True
            if policy.allowed_effects:
                return True
            effectful_fail_closed_signals = {
                "semantic_policy:conditional_effect_unresolved",
                "semantic_policy:external_write_enforcement_required",
                "semantic_policy:insufficient_authority",
                "semantic_policy:unscoped_agent_execution_denied",
            }
            return bool(
                effectful_fail_closed_signals.intersection(
                    policy.policy_signals
                )
            )
        if self.source == "model_invalid_output":
            return False
        return bool(
            self.intent == "implementation"
            or self.task_type
            in {"workspace_task", "code_task", "local_status"}
            or (
                self.needs_fresh_evidence
                and self.evidence_kind
                in {"runtime", "local", "file", "attachment"}
            )
        )


class UnderstandingCore:
    """Thin user-understanding layer before Veyra chooses route or executor."""

    def __init__(self, reasoning: CoreReasoning, state_store: WorldStateStore | None = None) -> None:
        self.reasoning = reasoning
        self.assembler = AwarenessContextAssembler(state_store) if state_store else None

    def build(
        self,
        *,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        turn_context: dict[str, Any] | None = None,
        awareness_snapshot: dict[str, Any] | None = None,
        allow_model: bool = True,
    ) -> TurnUnderstanding:
        snapshot = (
            awareness_snapshot
            if isinstance(awareness_snapshot, dict)
            else self._awareness_snapshot(
                text=text,
                attention_focus=attention_focus,
                event=event,
            )
        )
        fallback = self.fallback(text=text, attention_focus=attention_focus, event=event, awareness_snapshot=snapshot)
        fallback = _contextualize_read_only_fallback(fallback, text=text, turn_context=turn_context or {})
        if allow_model:
            model = self.orient_model(
                text=text,
                awareness_snapshot=snapshot,
                turn_context=turn_context or {},
                event=event,
                fallback=fallback,
            )
            if model is not None:
                return model
        return fallback

    def orient_model(
        self,
        *,
        text: str,
        awareness_snapshot: dict[str, Any],
        turn_context: dict[str, Any],
        event: VeyraEvent | None = None,
        fallback: TurnUnderstanding | None = None,
    ) -> TurnUnderstanding | None:
        if not self.reasoning.is_enabled():
            return None
        active = turn_context.get("active_context") if isinstance(turn_context.get("active_context"), dict) else {}
        short_memory = turn_context.get("short_memory") if isinstance(turn_context.get("short_memory"), dict) else {}
        conversation_tail = (
            short_memory.get("conversation_tail")
            if isinstance(short_memory.get("conversation_tail"), list)
            else []
        )
        conversation_slots = (
            short_memory.get("conversation_slots")
            if isinstance(short_memory.get("conversation_slots"), dict)
            else {}
        )
        payload = {
            "user_message": text,
            "awareness_snapshot": awareness_snapshot,
            "session_context": {
                "current_time": active.get("current_time"),
                "event": active.get("event"),
                "attention_focus": active.get("attention_focus"),
                "persona": active.get("persona"),
                "conversation_tail": conversation_tail[-6:],
                "conversation_slots": conversation_slots,
                "anchor_candidates": (
                    turn_context.get("anchor_candidates")
                    if isinstance(turn_context.get("anchor_candidates"), list)
                    else []
                ),
            },
            "required_json_fields": {
                "situation_assessment": {
                    "explicit_request": "what the user directly asks for",
                    "hidden_need": "practical need behind the words, including frustration/confusion if present",
                    "emotion": "neutral|frustrated|confused|urgent|curious|other",
                    "project": "project or domain anchor, e.g. Veyra, OpenClaw, course, deployment",
                    "risk_to_goal": "none|abandonment|wrong_direction|unsafe_execution|stale_evidence|lost_context|other",
                    "suggested_mode": "direct_answer|strategic_discussion|runtime_evidence|governed_execution|clarify|meta_cognition_discussion|other",
                    "constraints": "list of user or environment constraints",
                    "capability_needs": "list of capability ids or classes needed later, not route decisions",
                    "time_scale": "immediate|today|week|long_term|unknown",
                    "history_links": "list of exact candidate_token values from session_context.anchor_candidates that matter; never synthesize an id",
                    "user_goal": "what the user is trying to accomplish",
                    "what_user_really_needs": "same as hidden_need if useful",
                    "task_type": "chat|explanation|current_fact|local_status|workspace_task|code_task|proactive_request|meta_question|other",
                    "intent": "conversation|information|action|implementation|preference|unknown",
                    "task_summary": "one sentence summary",
                    "entities": "object: location, person, query, url, port, platform, topic, project",
                    "relevant_awareness": "list of current perception items that may help",
                    "stale_or_uncertain_awareness": "list of claims that must not be treated as fresh fact",
                    "evidence_gap": {
                        "needs_fresh_evidence": "boolean",
                        "evidence_kind": "none|local|runtime|external|file|attachment|calendar|email|memory|weather|time|search|web",
                        "what_would_change_the_answer": "concrete observation needed before a reliable answer",
                    },
                    "can_answer_from_current_context": "boolean",
                    "confidence": "0.0-1.0",
                    "reason": "short rationale",
                },
                "semantic_frame": {
                    "schema_version": "veyra.semantic_frame.v1",
                    "acts": [
                        {
                            "act_id": "unique id such as a1",
                            "kind": "open-world discourse act, e.g. request, question, prohibition, correction, preference, statement",
                            "goal": "the act's natural-language goal without collapsing other acts",
                            "operation": "concise open-world semantic operation; unknown is allowed",
                            "target": {
                                "type": "open-world target type",
                                "value": "target value",
                                "attributes": "JSON object with target qualifiers; optional anchor_candidate_token must exactly copy one supplied candidate token, target.type its kind, and target.value its label",
                            },
                            "polarity": "positive|negative|neutral|other open-world semantic polarity",
                            "explicitness": "explicit|strong_implied|weak_implied|inferred|unknown",
                            "source_quote": {
                                "text": "exact quote from user_message",
                                "start": "integer Unicode character offset, inclusive",
                                "end": "integer Unicode character offset, exclusive",
                            },
                            "speaker": "actual speaker of this act",
                            "authority": "direct_user|reported_speech|quoted_text|hypothetical|other open-world value",
                            "mention_mode": "normal_use|quoted_term|reported_speech|example|hypothetical|unknown",
                            "evidence_need": "open-world evidence requirement such as none, context, fresh_local, fresh_external",
                            "referent": {
                                "surface": "pronoun or referring expression, empty when absent",
                                "resolved": "resolved entity, empty when unresolved or absent",
                                "status": "resolved|ambiguous|unresolved|not_applicable",
                                "candidates": "list of possible referents",
                            },
                            "condition": "null or {kind, expression, source_quote}",
                            "modality": "open-world modality such as asserted, conditional, hypothetical, reported",
                            "arguments": "JSON object with semantic arguments; never put route, risk, state_effect, or capability grants here",
                        }
                    ],
                    "relations": [
                        {
                            "relation_id": "unique id such as r1",
                            "kind": "open-world relation such as contrast, correction, condition, sequence, alternative, quotation",
                            "from_act_id": "existing act id",
                            "to_act_id": "existing act id",
                            "description": "short explanation",
                            "source_quote": "null or exact source quote object",
                        }
                    ],
                    "ambiguities": [
                        {
                            "ambiguity_id": "unique id such as u1",
                            "kind": "open-world ambiguity type",
                            "description": "what cannot yet be resolved",
                            "affected_act_ids": "list of existing act ids",
                            "candidates": "list of candidates",
                        }
                    ],
                    "resolver_status": "resolved|ambiguous",
                    "source": "model",
                },
                "retrieval_hints": "optional list: conversation, belief, user, task, capabilities, runtime",
            },
        }
        result = self.reasoning.client.complete_json(
            purpose="turn_understanding",
            system=TURN_UNDERSTANDING_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self.reasoning._trace("turn_understanding", result, {"text_len": len(text)})
        candidate, issues = _validated_model_understanding(result, source_text=text)
        if candidate is not None and not issues:
            return candidate

        repairable_status = str(result.get("status") or "") in {
            "invalid_json",
            "invalid_response",
            "model_assisted",
        }
        if repairable_status:
            repair_payload = {
                **payload,
                "validation_issues": issues or [str(result.get("status") or "invalid_model_output")],
                "previous_candidate": _repair_candidate_for_prompt(result),
            }
            repaired = self.reasoning.client.complete_json(
                purpose="turn_understanding_repair",
                system=TURN_UNDERSTANDING_REPAIR_SYSTEM,
                user=json.dumps(repair_payload, ensure_ascii=False),
            )
            self.reasoning._trace(
                "turn_understanding_repair",
                repaired,
                {"text_len": len(text), "validation_issues": issues[:8]},
            )
            repaired_candidate, repaired_issues = _validated_model_understanding(repaired, source_text=text)
            if repaired_candidate is not None and not repaired_issues:
                repaired_candidate.reason = (
                    f"{repaired_candidate.reason}; semantic repair retry passed"
                    if repaired_candidate.reason
                    else "semantic repair retry passed"
                )
                return repaired_candidate
            issues = repaired_issues or issues

        if not repairable_status or fallback is None:
            return None
        return replace(
            fallback,
            semantic_frame=TurnSemanticFrame.fallback(text, resolver_status="invalid_output"),
            source="model_invalid_output",
            reason=f"model semantic output invalid after bounded repair: {', '.join(issues[:4]) or 'invalid output'}",
        )

    def fallback(
        self,
        *,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        awareness_snapshot: dict[str, Any] | None = None,
    ) -> TurnUnderstanding:
        lowered = (text or "").lower()
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        entities: dict[str, Any] = {}
        project = self._project_from_text(text)
        if project:
            entities["project"] = project
        confidence = 0.45
        data: dict[str, Any] = {
            "intent": "information" if self._looks_like_question(text, lowered) else "conversation",
            "task_type": "chat",
            "explicit_request": text.strip()[:120],
            "hidden_need": "",
            "emotion": self._emotion_from_text(text, lowered),
            "project": project,
            "risk_to_goal": "none",
            "suggested_mode": "direct_answer",
            "constraints": [],
            "capability_needs": [],
            "time_scale": "unknown",
            "history_links": [],
            "entities": entities,
            "evidence_gap": {"needs_fresh_evidence": False, "evidence_kind": "none", "what_would_change_the_answer": ""},
            "can_answer_from_current_context": True,
            "confidence": confidence,
            "source": "rule_fallback",
            "reason": "heuristic understanding fallback",
        }

        if self._is_strategic_veyra_turn(text, lowered, compact):
            data.update(
                {
                    "intent": "conversation",
                    "task_type": "meta_question",
                    "explicit_request": "讨论项目问题",
                    "hidden_need": "项目方向验证",
                    "emotion": data["emotion"] or "frustrated",
                    "project": project or "Veyra",
                    "risk_to_goal": "abandonment",
                    "suggested_mode": "strategic_discussion",
                    "time_scale": "long_term",
                    "history_links": ["Veyra architecture", "project direction"],
                    "confidence": 0.88,
                    "reason": "user is expressing project-direction frustration before asking for execution",
                }
            )
        elif self._is_meta_cognition_turn(text, lowered):
            data.update(
                {
                    "intent": "conversation",
                    "task_type": "meta_question",
                    "explicit_request": "分析 Veyra 认知或架构问题",
                    "hidden_need": "诊断为什么当前认知链路没有先理解用户",
                    "emotion": data["emotion"] or "frustrated",
                    "project": project or "Veyra",
                    "risk_to_goal": "wrong_direction",
                    "suggested_mode": "meta_cognition_discussion",
                    "history_links": ["Veyra architecture", "cognition pipeline"],
                    "confidence": 0.84,
                    "reason": "meta-cognition critique should be handled as strategic discussion unless runtime evidence is explicitly requested",
                }
            )
        elif self._is_runtime_status_turn(text, lowered):
            platform = self._runtime_platform(text, lowered)
            entities["platform"] = platform
            capability = f"{platform}_probe" if platform in {"openclaw", "hermes", "mcp"} else "system_probe"
            data.update(
                {
                    "intent": "information",
                    "task_type": "local_status",
                    "hidden_need": "确认当前运行态事实",
                    "suggested_mode": "runtime_evidence",
                    "capability_needs": [capability],
                    "entities": entities,
                    "evidence_gap": {
                        "needs_fresh_evidence": True,
                        "evidence_kind": "runtime",
                        "what_would_change_the_answer": f"fresh {platform} runtime probe result",
                    },
                    "can_answer_from_current_context": False,
                    "confidence": 0.86,
                    "reason": "runtime status needs fresh evidence",
                }
            )
        elif self._is_code_execution_turn(text, lowered):
            data.update(
                {
                    "intent": "implementation",
                    "task_type": "code_task",
                    "hidden_need": "执行受治理的代码修改",
                    "suggested_mode": "governed_execution",
                    "capability_needs": ["selected_agent_runtime"],
                    "can_answer_from_current_context": False,
                    "confidence": 0.82,
                    "reason": "code implementation needs governed execution",
                }
            )
        elif self._is_latest_external_turn(text, lowered):
            data.update(
                {
                    "intent": "information",
                    "task_type": "current_fact",
                    "hidden_need": "获取可验证的最新外部事实",
                    "suggested_mode": "runtime_evidence",
                    "capability_needs": ["web_search"],
                    "evidence_gap": {
                        "needs_fresh_evidence": True,
                        "evidence_kind": "search",
                        "what_would_change_the_answer": "fresh search or official feed evidence",
                    },
                    "can_answer_from_current_context": False,
                    "confidence": 0.82,
                    "reason": "latest external facts require fresh evidence",
                }
            )

        hidden = str(data.get("hidden_need") or "")
        payload = {
            **data,
            "task_summary": str(data.get("explicit_request") or text).strip()[:160],
            "user_goal": hidden or str(data.get("explicit_request") or text).strip()[:160],
            "what_user_really_needs": hidden,
            "retrieval_hints": self._retrieval_hints_for(data),
        }
        return TurnUnderstanding.from_payload(payload, source_text=text)

    def _awareness_snapshot(
        self,
        *,
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
    ) -> dict[str, Any]:
        if not self.assembler:
            return {"status": "unavailable"}
        return self.assembler.snapshot(
            user_message=text,
            attention_focus=attention_focus,
            user_id=str(event.source.user_id or "").strip() if event else "",
            session_id=(
                str(event.source.session_id or "").strip()
                if event
                else ""
            ),
        )

    def _retrieval_hints_for(self, data: dict[str, Any]) -> list[str]:
        hints = ["conversation"]
        if data.get("project") or data.get("history_links"):
            hints.append("user")
        if (data.get("evidence_gap") or {}).get("needs_fresh_evidence"):
            hints.extend(["capabilities", "runtime"])
        return list(dict.fromkeys(hints))

    def _project_from_text(self, text: str) -> str:
        if "Veyra" in text or "veyra" in text.lower():
            return "Veyra"
        if "OpenClaw" in text or "openclaw" in text.lower():
            return "OpenClaw"
        if "Hermes" in text or "hermes" in text.lower():
            return "Hermes"
        return ""

    def _emotion_from_text(self, text: str, lowered: str) -> str:
        if any(marker in text for marker in ("做不下去", "智障", "崩溃", "烦", "没意义", "不想做", "放弃")):
            return "frustrated"
        if any(marker in lowered for marker in ("frustrated", "stuck", "confused")):
            return "frustrated" if "frustrated" in lowered or "stuck" in lowered else "confused"
        return "neutral"

    def _looks_like_question(self, text: str, lowered: str) -> bool:
        return any(marker in text for marker in ("？", "为什么", "是什么", "怎么", "吗")) or any(
            marker in lowered for marker in ("why", "what", "how", "?")
        )

    def _is_strategic_veyra_turn(self, text: str, lowered: str, compact: str) -> bool:
        has_project = "veyra" in lowered or "Veyra" in text
        frustration = any(marker in compact for marker in ("做不下去了", "做不下去", "不想做了", "放弃", "没意义"))
        direction = any(marker in compact for marker in ("方向", "为什么存在", "路线", "灵魂"))
        return bool(has_project and (frustration or direction))

    def _is_meta_cognition_turn(self, text: str, lowered: str) -> bool:
        return any(marker in text for marker in ("架构", "认知", "prompt", "提示词", "智障")) or any(
            marker in lowered for marker in ("architecture", "cognition", "prompt")
        )

    def _is_runtime_status_turn(self, text: str, lowered: str) -> bool:
        runtime = any(marker in lowered for marker in ("openclaw", "hermes", "mcp", "runtime")) or any(
            marker in text for marker in ("运行态", "进程", "端口")
        )
        status = any(marker in lowered for marker in ("running", "status", "current", "now")) or any(
            marker in text for marker in ("现在", "当前", "状态", "运行", "还在")
        )
        return runtime and status

    def _runtime_platform(self, text: str, lowered: str) -> str:
        if "openclaw" in lowered:
            return "openclaw"
        if "hermes" in lowered:
            return "hermes"
        if "mcp" in lowered:
            return "mcp"
        return "system"

    def _is_code_execution_turn(self, text: str, lowered: str) -> bool:
        return any(marker in text for marker in ("改代码", "修改代码", "实现这个能力", "修复 bug", "调试", "写代码")) or any(
            marker in lowered for marker in ("code edit", "implement", "debug", "fix bug")
        )

    def _is_latest_external_turn(self, text: str, lowered: str) -> bool:
        return any(marker in text for marker in ("最新", "新闻")) or any(marker in lowered for marker in ("latest", "recent", "today's"))


def _dict_from(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contextualize_read_only_fallback(
    fallback: TurnUnderstanding,
    *,
    text: str,
    turn_context: dict[str, Any],
) -> TurnUnderstanding:
    """Resolve a narrow, read-only continuation from trusted session context.

    This adapter may recover an external read request, but it never creates a
    state-changing act or upgrades a degraded resolver to resolved.
    """

    if _blocks_contextual_read_recovery(text):
        return fallback

    short_memory = turn_context.get("short_memory") if isinstance(turn_context.get("short_memory"), dict) else {}
    slots = short_memory.get("conversation_slots") if isinstance(short_memory.get("conversation_slots"), dict) else {}
    conversation_tail = (
        short_memory.get("conversation_tail")
        if isinstance(short_memory.get("conversation_tail"), list)
        else []
    )
    last_tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
    compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "").lower()
    previous_weather_location = str(
        slots.get("last_location")
        or last_tool.get("requested_location")
        or last_tool.get("location")
        or ""
    ).strip()
    weather_followup = (
        str(last_tool.get("type") or "") == "weather"
        or str(slots.get("last_topic") or "") == "weather"
    ) and _looks_like_weather_followup(text)
    if previous_weather_location and weather_followup:
        location = _weather_followup_location(
            text=text,
            previous_location=previous_weather_location,
        )
        source = text or ""
        frame = TurnSemanticFrame(
            acts=[
                SemanticAct(
                    act_id="a1",
                    kind="read_request",
                    goal=f"continue current weather query for {location}",
                    operation="query_current_weather",
                    target=SemanticTarget(
                        type="weather",
                        value=location,
                        attributes={"continuation": True},
                    ),
                    polarity="positive",
                    explicitness="strong_implied",
                    source_quote=SourceQuote(text=source, start=0, end=len(source)),
                    speaker="user",
                    authority="direct_user",
                    mention_mode="normal_use",
                    evidence_need="fresh_external_weather",
                    referent=ReferentResolution(
                        surface=text,
                        resolved=location,
                        status="resolved",
                        candidates=[],
                    ),
                    condition=None,
                    modality="asserted",
                    arguments={"location": location, "continuation": True},
                )
            ],
            relations=[],
            ambiguities=[],
            resolver_status="degraded",
            source="context_fallback",
        )
        return replace(
            fallback,
            intent="information",
            task_type="current_fact",
            user_goal=f"查询 {location} 的当前天气",
            explicit_request=text,
            suggested_mode="runtime_evidence",
            needs_fresh_evidence=True,
            evidence_kind="weather",
            can_answer_from_world_state=False,
            semantic_frame=frame,
            source="context_fallback",
            reason="resolved a read-only weather continuation from the same-session tool slot",
        )
    retry_markers = {
        "没有这些",
        "没有",
        "都没有",
        "不是这些",
        "没这些",
        "换一批",
        "再找",
        "继续找",
        "重新搜",
        "重新找",
    }
    previous_query = str(slots.get("last_search_query") or last_tool.get("query") or "").strip()
    if not previous_query:
        for item in reversed(conversation_tail[-6:]):
            if not isinstance(item, dict) or str(item.get("direction") or "") != "inbound":
                continue
            candidate = str(item.get("text") or "").strip()
            if not candidate or candidate == text:
                continue
            candidate_compact = re.sub(r"[\s，,。！？!?、]+", "", candidate).lower()
            has_lookup = any(marker in candidate_compact for marker in ("找", "搜索", "搜", "查", "信息", "search", "find", "lookup"))
            has_external_topic = any(
                marker in candidate_compact
                for marker in ("秋招", "春招", "校招", "招聘", "岗位", "公司", "新闻", "最新", "recruit", "hiring", "jobs")
            )
            if has_lookup and has_external_topic:
                previous_query = candidate
                break
    if compact not in retry_markers or not previous_query:
        return fallback
    source = text or ""
    if not source:
        return fallback
    frame = TurnSemanticFrame(
        acts=[
            SemanticAct(
                act_id="a1",
                kind="read_request",
                goal=f"continue external search for {previous_query}",
                operation="retry_external_search",
                target=SemanticTarget(
                    type="search_query",
                    value=previous_query,
                    attributes={"continuation": True},
                ),
                polarity="positive",
                explicitness="strong_implied",
                source_quote=SourceQuote(text=source, start=0, end=len(source)),
                speaker="user",
                authority="direct_user",
                mention_mode="normal_use",
                evidence_need="fresh_external_search",
                referent=ReferentResolution(
                    surface="这些",
                    resolved="previous_search_results",
                    status="resolved",
                    candidates=[],
                ),
                condition=None,
                modality="asserted",
                arguments={"query": previous_query, "retry": True},
            )
        ],
        relations=[],
        ambiguities=[],
        resolver_status="degraded",
        source="context_fallback",
    )
    return replace(
        fallback,
        intent="information",
        task_type="current_fact",
        user_goal=f"继续查找：{previous_query}",
        explicit_request=text,
        suggested_mode="runtime_evidence",
        needs_fresh_evidence=True,
        evidence_kind="search",
        can_answer_from_world_state=False,
        semantic_frame=frame,
        source="context_fallback",
        reason="resolved a read-only search continuation from the same-session tool slot",
    )


def _looks_like_weather_followup(text: str) -> bool:
    compact = re.sub(r"[\s，,。！？!?、~～]+", "", text or "")
    if not compact or len(compact) > 30:
        return False
    if compact in {"呢", "今天呢", "现在呢", "明天呢", "温度呢", "气温呢", "天气呢"}:
        return True
    if re.fullmatch(
        r"[\w\u4e00-\u9fff·-]{1,8}(?:省|市|区|县|镇|乡)(?:呢|(?:天气|气温|温度)(?:呢|怎么样|如何))",
        compact,
    ):
        return True
    return False


def _blocks_contextual_read_recovery(text: str) -> bool:
    """Keep degraded continuation recovery from erasing discourse boundaries."""

    compact = re.sub(r"\s+", "", text or "").lower()
    return bool(
        re.search(r"(?:不要|请勿|禁止|先别|别再|不用再|无需再)(?:查|看|搜|查询|搜索)?", compact)
        or re.search(r"别(?:查|看|搜|查询|搜索|执行|运行)", compact)
        or re.search(r"\b(?:do not|don't|dont|never|stop)\b", compact)
        or re.search(r"(?:如果|假如|若|一旦|等我|等到|待).+(?:再|就|才)", compact)
        or re.search(r"\b(?:if|when)\b.+", compact)
        or re.search(r"(?:老板|领导|同事|客户|对方|他|她|他们)(?:说|要求|让)", compact)
        or re.search(r"\b(?:boss|manager|client|colleague|he|she|they)\s+(?:said|asked|told)\b", compact)
    )


def _weather_followup_location(*, text: str, previous_location: str) -> str:
    fragment = re.sub(r"[\s，,。！？!?、~～]+", "", text or "")
    for token in ("现在", "当前", "今天", "今日", "明天", "天气", "气温", "温度", "怎么样", "如何", "呢"):
        fragment = fragment.replace(token, "")
    fragment = fragment.strip()
    if not fragment or fragment in previous_location:
        return previous_location
    if previous_location in fragment:
        return fragment
    if fragment.endswith(("区", "县", "镇", "乡")):
        city_match = re.match(r"^(.+?市)", previous_location)
        if city_match:
            return f"{city_match.group(1)}{fragment}"
    return fragment


def _semantic_frame_from_payload(payload: dict[str, Any], *, source_text: str) -> TurnSemanticFrame | None:
    is_model_output = payload.get("status") == "model_assisted" or payload.get("source") in {"model", "model_repair"}
    try:
        if is_model_output:
            return TurnSemanticFrame.safe_from_model_payload(payload, source_text=source_text)
        return TurnSemanticFrame.from_payload(payload, source_text=source_text)
    except (TypeError, ValueError):
        if not source_text:
            return None
        return TurnSemanticFrame.fallback(
            source_text,
            resolver_status="invalid_output" if is_model_output else "degraded",
        )


def _validated_model_understanding(
    payload: dict[str, Any],
    *,
    source_text: str,
) -> tuple[TurnUnderstanding | None, list[str]]:
    if payload.get("status") != "model_assisted":
        return None, [str(payload.get("status") or "model_transport_failure")]
    candidate = TurnUnderstanding.from_payload(payload, source_text=source_text)
    frame = candidate.semantic_frame
    if frame is None:
        return None, ["semantic_frame_missing"]
    if frame.resolver_status == "invalid_output":
        return None, ["semantic_frame_schema_or_source_binding_invalid"]
    return candidate, semantic_frame_quality_issues(frame, source_text)


def _repair_candidate_for_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        key: value
        for key, value in payload.items()
        if key not in {"_model", "duration_ms"}
    }
    raw_text = candidate.get("raw_text")
    if isinstance(raw_text, str):
        candidate["raw_text"] = raw_text[:1600]
    return redact_sensitive(candidate, max_string=1600, max_list=16)


def _apply_model_clarification_gap(
    frame: TurnSemanticFrame | None,
    situation: dict[str, Any],
) -> TurnSemanticFrame | None:
    """Keep a model-declared context gap inside the authoritative frame."""

    if frame is None or frame.source not in {"model", "model_repair"}:
        return frame
    if str(situation.get("suggested_mode") or "") != "clarify" or frame.ambiguities:
        return frame
    gap = situation.get("evidence_gap") if isinstance(situation.get("evidence_gap"), dict) else {}
    description = str(
        gap.get("what_would_change_the_answer")
        or situation.get("hidden_need")
        or "需要补充执行对象、范围或期望结果。"
    ).strip()
    payload = frame.model_dump(mode="json")
    payload["resolver_status"] = "ambiguous"
    payload["ambiguities"] = [
        {
            "ambiguity_id": "model_context_gap",
            "kind": "missing_context",
            "description": description[:800],
            "affected_act_ids": [act.act_id for act in frame.acts],
            "candidates": [],
        }
    ]
    return TurnSemanticFrame.model_validate(payload, strict=True)


def _any_list(value: Any, *, limit: int) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value[:limit] if item]


def _float_between(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))
