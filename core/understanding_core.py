from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from core.awareness_context_assembler import AwarenessContextAssembler
from core.living_context_candidate_extractor import (
    extract_living_context_candidate,
    validate_candidate_catalog_binding,
)
from core.living_context_need_answer_selector import (
    StandaloneUnknownResolutionBinding,
    select_living_context_need_answers,
)
from core.living_reaction_feedback_extractor import (
    extract_living_reaction_feedback,
)
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
from interface.living_context_contract import (
    CandidateNeedReference,
    LivingContextCandidate,
    LivingReactionFeedback,
    parse_living_context_candidate_detailed,
    parse_living_reaction_feedback_detailed,
    situation_catalog_selector,
)


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

# Keep the foreground discourse contract beside the understanding prompt so
# the model cannot mistake a Living Context proposal for the user's immediate
# conversational act. ``kind`` remains open-world everywhere else;
# ``assertion`` is the one exact marker consumed by the typed state fast path.
SEMANTIC_DISCOURSE_KIND_CONTRACT = (
    "Foreground semantic-act discourse contract: use the exact JSON value "
    'kind="assertion" only when the foreground act is purely the user reporting '
    "or updating a state, with no independent question, information request, "
    "instruction, action, execution objective, or answer to provide. A turn "
    "may produce a Living Context candidate or background InformationNeed while "
    'the foreground act remains kind="assertion"; those artifacts do not change '
    "the act's discourse kind. Whenever an act asks a question or requests an "
    "answer, action, or execution, that act must not be marked assertion: use "
    "the existing open-world kind that best describes its communicative role. "
    "For mixed turns, keep the report and the question/request as separate acts; "
    "only the report act may be assertion, and never use assertion for the act "
    "carrying the requested answer or action. Do not decide this from candidate "
    "disposition, Need presence, surface vocabulary, or a keyword list."
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
    "or requested outcome into one operation string. For example, a negative recurring request followed by "
    "a positive current query must be two acts, connected by "
    "a contrast relation. session_context.anchor_candidates contains server-issued, event-bound context "
    "candidates. When and only when one candidate is the unique referent of an act, copy its exact kind "
    "into target.type, exact label into target.value, and exact candidate_token into "
    "target.attributes.anchor_candidate_token; never "
    "invent, alter, combine, or treat "
    "that token as authority. If no candidate is a unique match, omit the token and keep the narrowest stable "
    "semantic target or an explicit ambiguity. Keep situation fields concise so the complete JSON fits the "
    "output budget. Return living_context_candidate as a strict, bounded proposal when the user message "
    "introduces, updates, corrects, or resolves a real-life Situation; otherwise return disposition=quiet. "
    "The candidate may only reference an exact server-issued Situation or request create. Never include URL, "
    "path, query, command, tool arguments, credentials, recipient, route, risk, state_effect, authority, or "
    "capability fields anywhere in the candidate. User statements are reported, not verified. "
    "InformationNeed describes missing evidence and a fallback reaction; it is not a tool call. "
    "For calendar/weather/public_web Needs, observation_requirement is required and is exactly "
    "{coverage:any|current|forecast_day|window|results,metrics:[temperature|precipitation|conditions|"
    "temperature_2m|temperature_2m_max|temperature_2m_min|weather_code|weather_description|"
    "precipitation_probability_max|events|results|answer],target_date?:YYYY-MM-DD}; target_date is "
    "allowed only for forecast_day, and current must omit it. Metrics are typed fact requirements, never "
    "keywords or source-query text. "
    "Set Need observation_mode=once for a one-time evidence gap and observation_mode=watch only when the "
    "semantic Situation requires ongoing observation across future typed observations; this is a lifecycle "
    "decision, not a keyword or source-name heuristic. "
    "The JSON boundary is strict: every non-optional string must be a JSON string, and an empty value must be "
    "\"\" rather than null, an object, or an array. This includes create_subject, label, title, summary, goal, "
    "material_change, next_step, reopen_reason, nested statement/value fields, and Need blocked_judgment, "
    "why_now, and question. Optional fields explicitly marked nullable may be null. assumptions is always a "
    "list of objects {statement, epistemic_status}; never emit a bare assumption string. Use only these exact "
    "enums: disposition quiet|create|update|correct|resolve; category general|personal|work|education|health|"
    "travel|logistics|finance|other; progress.status unknown|not_started|in_progress|blocked|waiting|completed; "
    "lifecycle emerging|active|waiting|resolved|expired|contradicted|archived; requested_reaction and "
    "fallback_reaction ask|read|wait|silent; Need evidence_kind user|calendar|email|message|weather|public_web|"
    "agent|time|other; entity.kind place|person|organization|item|other; epistemic_status reported|inferred. "
    "Unknown enum values are invalid, not approximate synonyms. The literal candidate schema_version is "
    "veyra.living_context_candidate.v1 (never veyra.context.v1). The literal semantic_frame schema_version is "
    "veyra.semantic_frame.v1. For semantic_frame explicitness use only explicit|strong_implied|weak_implied|"
    "inferred|unknown, mention_mode only normal_use|quoted_term|reported_speech|example|hypothetical|unknown, "
    "and evidence_need is a string such as none or context, never an object. A minimal valid Situation candidate "
    "is {schema_version:'veyra.living_context_candidate.v1',disposition:'create',create_subject:'...',"
    "category:'general',summary:'...',known:[{statement:'...',epistemic_status:'reported'}],unknown:[],"
    "assumptions:[],needs:[],requested_reaction:'wait',source:'model'}. A non-Situation candidate is "
    "{schema_version:'veyra.living_context_candidate.v1',disposition:'quiet',source:'model'}. A minimal valid "
    "feedback object is {schema_version:'veyra.living_reaction_feedback.v1',reaction_token:'<exact current "
    "reaction token>',label:'useful',source_quote:{text:'<exact substring of user_message>',start:0,end:<length>}}; "
    "feedback.source_quote is always an object, never a string. An explicit evaluation of a prior reaction "
    "(useful, not useful, ignore, resolved, too early, too late, too frequent, or remind before) is direct "
    "feedback: when the current catalog row contains reaction, emit living_reaction_feedback and copy its exact "
    "reaction.reaction_token; do not omit feedback merely because the message is phrased as an acknowledgment. "
    "Use feedback.label=ignore only when the user explicitly asks Veyra to suppress, dismiss, or stop acting on "
    "that reaction; use not_useful when the reaction was evaluated as unhelpful but should not be suppressed; use "
    "resolved only when the user states that the underlying Situation is complete or solved. Do not conflate these "
    "labels, and do not infer one from a generic negative sentiment. "
    "If no current reaction is supplied, omit feedback. A minimal valid "
    "semantic frame has one act with exact keys act_id,kind,goal,operation,target,polarity,explicitness,"
    "source_quote,speaker,authority,mention_mode,evidence_need,referent,condition,modality,arguments. Use "
    "referent:{surface:'',resolved:'',status:'not_applicable',candidates:[]}, condition:null, arguments:{} "
    "when they do not apply; condition is never a string and arguments is never a list. Use only "
    "explicit|strong_implied|weak_implied|inferred|unknown for explicitness and only the four referent statuses. "
    "Set relations:[], ambiguities:[], resolver_status:'resolved', source:'model'. A living Situation token "
    "is not an anchor_candidate_token: only copy anchor_candidate_token from session_context.anchor_candidates, "
    "and omit it when no such exact anchor is supplied. For a create or update candidate, extract a concise "
    "user-grounded goal whenever the message states an objective; do not leave goal empty when the objective is directly "
    "stated. Set progress.status from the user's wording: preparing/underway maps to in_progress, planning maps "
    "to not_started, and only an explicit completion maps to completed; otherwise use unknown. Treat relative time "
    "as a bounded reported time expression. Use only session_context.current_time (the server-provided "
    "timezone-aware ISO timestamp) to resolve phrases such as next week or the end of this month. For a window, "
    "deadline_at may be the timezone-aware end of that bounded window, while a Need or inferred assumption must "
    "preserve that the exact event date is unknown. Never invent a calendar date from unstated facts, and leave "
    "deadline_at null when no time expression is present."
    " For an existing Situation update that directly answers an open InformationNeed, "
    "copy the exact need_token and generation from the same current catalog row into "
    "answered_need_tokens and answered_need_bindings, and include a root source_quote "
    "that is an exact slice of user_message with assertion_mode=direct_user. If the "
    "user turn does not uniquely answer that Need, omit both answer-binding fields and "
    "leave the Need open; never infer a binding from similar wording. Answering one "
    "sub-Need is still an update/nonterminal Situation change; emit resolve only when "
    "the user explicitly declares that the entire Situation or goal is finished."
    "\n\n"
    + SEMANTIC_DISCOURSE_KIND_CONTRACT
)

TURN_UNDERSTANDING_REPAIR_SYSTEM = (
    TURN_UNDERSTANDING_SYSTEM
    + "\nYour previous candidate was incomplete or invalid. Rebuild the entire JSON object from the original "
    "user_message. Treat validation_issues as defects to fix, not as semantic facts. Preserve atomic acts, "
    "opposite polarities, conditions, quotation/authority boundaries, and exact source slices. Every non-optional "
    "string must be a JSON string (use \"\" when empty, never null/object/array). Keep assumptions as a list of "
    "{statement,epistemic_status} objects. answered_need_tokens is a JSON list of strings and "
    "answered_need_bindings is a JSON list of {need_token,generation} objects. Keep the output compact: at most "
    "one item per candidate list and one semantic act; timeline must remain a JSON list of bounded objects "
    "{statement,occurred_at,source_quote,material}, never a scalar or mapping; omit optional feedback when no exact current reaction token "
    "and quote exist. If the original user_message explicitly reports progress or a change to an existing Situation "
    "or asks to add it to the original Situation, do not emit a quiet candidate: emit one update candidate using "
    "only the exact matching situation_token, situation_revision, and catalog_token from the supplied current "
    "catalog. When the same direct user turn answers an open Need, copy its exact "
    "need_token+generation pair and include a source-bound root source_quote with "
    "assertion_mode=direct_user; otherwise omit answer bindings and keep that Need "
    "open. Answering one sub-Need remains update/nonterminal; use resolve only for an "
    "explicit statement that the whole Situation or goal is finished. This is a strict "
    "boundary repair, not permission to invent a Situation or token. Return no prose."
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
    living_context_candidate: LivingContextCandidate | None = None
    living_context_candidate_issues: list[str] = field(default_factory=list)
    living_context_candidate_metrics: dict[str, Any] = field(default_factory=dict)
    living_reaction_feedback: LivingReactionFeedback | None = None
    living_reaction_feedback_issues: list[str] = field(default_factory=list)
    living_reaction_feedback_metrics: dict[str, Any] = field(default_factory=dict)
    model_boundary_metrics: dict[str, Any] = field(default_factory=dict)
    # Server-only sidecar populated by the bounded Need/Unknown selector.
    # It is intentionally not part of LivingContextCandidate's public model.
    living_context_standalone_unknown_resolutions: tuple[
        StandaloneUnknownResolutionBinding, ...
    ] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        source_text: str = "",
        current_time: Any = None,
        catalog: list[dict[str, Any]] | None = None,
        conversation_binding: dict[str, str] | None = None,
    ) -> "TurnUnderstanding":
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
        living_candidate_payload = situation.get("living_context_candidate")
        if living_candidate_payload is None:
            living_candidate_payload = payload.get("living_context_candidate")
        if living_candidate_payload is None:
            living_candidate_payload = root_payload.get("living_context_candidate")
        living_candidate_payload = _bind_unbound_continuation_candidate(
            living_candidate_payload,
            conversation_binding=conversation_binding,
            catalog=catalog or [],
        )
        (
            living_candidate,
            living_candidate_issues,
            living_candidate_metrics,
        ) = parse_living_context_candidate_detailed(
            living_candidate_payload,
            source_text=semantic_text,
            current_time=current_time,
            catalog=catalog,
        )
        reaction_feedback_payload = situation.get("living_reaction_feedback")
        if reaction_feedback_payload is None:
            reaction_feedback_payload = payload.get("living_reaction_feedback")
        if reaction_feedback_payload is None:
            reaction_feedback_payload = root_payload.get("living_reaction_feedback")
        (
            reaction_feedback,
            reaction_feedback_issues,
            reaction_feedback_metrics,
        ) = parse_living_reaction_feedback_detailed(
            reaction_feedback_payload,
            source_text=semantic_text,
        )
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
            living_context_candidate=living_candidate,
            living_context_candidate_issues=living_candidate_issues,
            living_context_candidate_metrics=living_candidate_metrics,
            living_reaction_feedback=reaction_feedback,
            living_reaction_feedback_issues=reaction_feedback_issues,
            living_reaction_feedback_metrics=reaction_feedback_metrics,
            model_boundary_metrics={
                "candidate": dict(living_candidate_metrics),
                "feedback": dict(reaction_feedback_metrics),
            },
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
            "living_context_candidate": (
                self.living_context_candidate.model_dump(mode="json")
                if self.living_context_candidate is not None
                else None
            ),
            "living_context_candidate_metrics": dict(self.living_context_candidate_metrics),
            "living_reaction_feedback": (
                self.living_reaction_feedback.model_dump(mode="json")
                if self.living_reaction_feedback is not None
                else None
            ),
            "living_reaction_feedback_metrics": dict(self.living_reaction_feedback_metrics),
            "model_boundary_metrics": dict(self.model_boundary_metrics),
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
                "living_context_candidate": (
                    {
                        "disposition": self.living_context_candidate.disposition,
                        "situation_token": self.living_context_candidate.situation_token,
                        "needs": len(self.living_context_candidate.needs),
                    }
                    if self.living_context_candidate is not None
                    else None
                ),
                "living_reaction_feedback": (
                    {
                        "label": self.living_reaction_feedback.label,
                        "reaction_token": self.living_reaction_feedback.reaction_token,
                    }
                    if self.living_reaction_feedback is not None
                    else None
                ),
                "model_boundary": {
                    "candidate": dict(self.living_context_candidate_metrics),
                    "feedback": dict(self.living_reaction_feedback_metrics),
                    "overall": dict(self.model_boundary_metrics),
                },
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
        # The turn-context builder intentionally keeps a small allowlist of
        # catalog fields.  Add the server-derived short selector locally so
        # the model can copy one stable row reference without widening the
        # shared context surface; the selector is resolved back to the full
        # binding triple at the model boundary.
        turn_context = _with_situation_catalog_selectors(turn_context)
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
        catalog = (
            turn_context.get("living_context_situation_candidates")
            if isinstance(turn_context.get("living_context_situation_candidates"), list)
            else []
        )
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
        conversation_binding = _safe_conversation_binding(
            turn_context.get("conversation_binding")
        )
        payload = {
            "user_message": text,
            # This is a source-binding aid, not a semantic fact.  Supplying
            # only the runtime-visible prefix prevents the model request from
            # duplicating an unbounded user turn while keeping Python-character
            # offsets deterministic for a bounded ContextQuote.
            "source_binding": {
                "python_character_length": len(text),
                **(
                    {
                        "bounded_quote": {
                            "text": text[:480],
                            "start": 0,
                            "end": min(len(text), 480),
                        }
                    }
                    if text
                    else {}
                ),
            },
            "awareness_snapshot": awareness_snapshot,
            "session_context": {
                "current_time": active.get("current_time"),
                "event": active.get("event"),
                "attention_focus": active.get("attention_focus"),
                "persona": active.get("persona"),
                "conversation_tail": conversation_tail[-6:],
                "conversation_slots": conversation_slots,
                "conversation_binding": conversation_binding,
                "anchor_candidates": (
                    turn_context.get("anchor_candidates")
                    if isinstance(turn_context.get("anchor_candidates"), list)
                    else []
                ),
                "living_context_situation_candidates": (
                    turn_context.get("living_context_situation_candidates")
                    if isinstance(turn_context.get("living_context_situation_candidates"), list)
                    else []
                ),
            },
            "required_json_fields": {
                "situation_assessment": {
                    "explicit_request": "string",
                    "hidden_need": "string",
                    "emotion": "neutral|frustrated|confused|urgent|curious|other",
                    "project": "string",
                    "risk_to_goal": "none|abandonment|wrong_direction|unsafe_execution|stale_evidence|lost_context|other",
                    "suggested_mode": "direct_answer|strategic_discussion|runtime_evidence|governed_execution|clarify|meta_cognition_discussion|other",
                    "constraints": "list[string]",
                    "capability_needs": "list[string] (ids only, never routes)",
                    "time_scale": "immediate|today|week|long_term|unknown",
                    "history_links": "list of exact anchor_candidate_token values only",
                    "user_goal": "string",
                    "what_user_really_needs": "string",
                    "task_type": "chat|explanation|current_fact|local_status|workspace_task|code_task|proactive_request|meta_question|other",
                    "intent": "conversation|information|action|implementation|preference|unknown",
                    "task_summary": "short string",
                    "entities": "object of bounded mentions",
                    "relevant_awareness": "list",
                    "stale_or_uncertain_awareness": "list",
                    "evidence_gap": "object {needs_fresh_evidence:boolean,evidence_kind:enum,what_would_change_the_answer:string}; evidence_kind=none|local|runtime|external|file|attachment|calendar|email|memory|weather|time|search|web",
                    "can_answer_from_current_context": "boolean",
                    "confidence": "number 0.0..1.0",
                    "reason": "short string",
                },
                "living_context_candidate": "If this message introduces or changes a real-life Situation, emit one compact object; otherwise emit {schema_version, disposition=quiet, source=model}. For create use {schema_version, disposition=create, create_subject, category, summary, goal, progress, deadline_at, known:[{statement,epistemic_status}], unknown:[string], assumptions:[{statement,epistemic_status}], entities:[{kind,value,epistemic_status,source_quote}], needs:[{blocked_judgment,evidence_kind,observation_mode,observation_requirement,why_now,urgency,allowed_source_classes,fallback_reaction,question}], requested_reaction, source}. Set observation_mode=once for a one-time evidence gap; use observation_mode=watch only when the Situation needs repeated future observation. For calendar/weather/public_web Needs, observation_requirement is required and must be {coverage:any|current|forecast_day|window|results,metrics:[exact typed metric enums]}; never infer it from question prose. This is a semantic lifecycle field, not a keyword or source-name heuristic. When the user states an objective, goal is required and must be grounded in the direct user wording; progress.status reflects explicit wording and progress.value, when present, must be a JSON number from 0 to 1 or null (never a string, percentage, object, or NaN). Resolve next-week/end-of-month windows only from the server current_time into a timezone-aware bounded deadline_at; keep the exact event date as an unknown/Need, and never invent an exact date. Omit deadline_at only when the user gives no time expression. Emit an entities row only for a concrete thing the user named in this message, with kind=place|person|organization|item|other and a source_quote that is an exact user_message slice; use epistemic_status=reported only when the user stated it directly, otherwise inferred. A place entity is how a weather or location-bound source is later resolved, so name the place exactly as the user wrote it and never invent, translate, or normalise it. Exact enums: category=general|personal|work|education|health|travel|logistics|finance|other; epistemic_status=reported|inferred; evidence_kind=user|calendar|email|message|weather|public_web|agent|time|other; fallback_reaction/requested_reaction=ask|read|wait|silent; urgency is a JSON number 0..1. For update/correct/resolve copy exact situation_selector, situation_token, situation_revision, catalog_token from one current catalog row; situation_selector is a short opaque row selector and never replaces the final binding triple; never invent server-issued values.",
                "living_reaction_feedback": "Emit when the user directly evaluates the current reaction; otherwise omit. Shape: {schema_version:'veyra.living_reaction_feedback.v1', reaction_token copied exactly from the current catalog, label=ignore|resolved|useful|not_useful|too_early|too_late|too_frequent|remind_before, remind_before_seconds:number only for remind_before, source_quote:{text,start,end} exact}. source_quote is an object, never a string; its text must be an exact user_message slice. Candidate and feedback are independent.",
                "semantic_frame": "Compact exact object: {schema_version:'veyra.semantic_frame.v1', acts:[{act_id,kind,goal,operation,target:{type,value,attributes:{}},polarity,explicitness,source_quote:{text,start,end},speaker:'user',authority:'direct_user',mention_mode:'normal_use',evidence_need:'none',referent:{surface:'',resolved:'',status:'not_applicable',candidates:[]},condition:null,modality:'asserted',arguments:{}}], relations:[], ambiguities:[], resolver_status:'resolved'|'ambiguous', source:'model'}. Every source_quote must be an exact user_message slice; keep one act per independent assertion. condition is null or an object {kind,expression,source_quote}, never a string; arguments is an object, never a list.",
                "retrieval_hints": "optional list of strings",
            },
            "model_rules": [
                "For calendar/weather/public_web Needs, observation_requirement must be a typed object with coverage, metrics, and optional target_date; target_date is canonical YYYY-MM-DD only for forecast_day and must agree with the server-resolved local time window. Do not infer it from question or blocked prose.",
                "Never invent a Situation revision or Situation/InformationNeed token.",
                "Use only exact server-issued selectors, tokens, revisions, and Need references from session_context.living_context_situation_candidates.",
                "For update/correct/resolve copy situation_selector, situation_revision, and catalog_token from the same current catalog row; never infer them.",
                "When session_context.conversation_binding is present, treat its situation_selector as the default continuation thread for this turn. Preserve it unless the user's semantic act resolves to a different exact catalog row; never choose a thread from text matching or keywords.",
                "A direct continuation may select another exact catalog Situation when the semantic act clearly refers to that row. Copy only that row's server-issued selector and binding fields; never emit or derive a raw conversation or Situation ID.",
                "For answered_need_tokens copy matching need_token and generation into answered_need_bindings from the same current catalog row.",
                "A direct user turn that answers an open catalog Need must carry the exact need_token+generation pair, a root source_quote bound to this user_message, and assertion_mode=direct_user; if any part is not proven, omit both answer-binding fields and keep the Need open.",
                "Answering one sub-Need is still an update/nonterminal Situation change; resolve only when the user explicitly declares the entire Situation or goal finished.",
                "A correct/resolve candidate requires an exact source_quote and direct_user assertion; otherwise leave it inferred and do not emit the lifecycle disposition.",
                "A living_reaction_feedback entry requires an exact user source_quote and an exact current reaction_token; never infer either token or quote.",
                "Feedback changes only the bounded reaction ledger. It never changes route, risk, authority, tools, execution, or delivery.",
                "For all non-optional strings, emit \"\" when empty, never null. Keep assumptions as objects and copy only exact server-issued tokens, revisions, and Need generations.",
                "Do not emit URL, path, query, command, tool, tool_args, authority, route, risk, or capability fields.",
                "answered_need_tokens is a JSON list of strings; answered_need_bindings is a JSON list of {need_token, generation} objects. Do not use an object or string for either list.",
                "Keep model output compact: at most one entity, one known item, one timeline item, and one InformationNeed. timeline is always a JSON list of bounded objects {statement,occurred_at,source_quote,material}; use [] when no valid timeline item is grounded in the user turn. If no exact source class is clear, use evidence_kind=other; never use a category such as travel or logistics as evidence_kind. A Need with observation_mode=watch must describe a genuinely ongoing observation; otherwise use once.",
            ],
        }
        result = _complete_model_json_safe(
            self.reasoning.client,
            purpose="turn_understanding",
            system=TURN_UNDERSTANDING_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self.reasoning._trace("turn_understanding", result, {"text_len": len(text)})
        candidate, issues = _validated_model_understanding(
            result,
            source_text=text,
            current_time=active.get("current_time"),
            catalog=catalog,
            conversation_binding=conversation_binding,
        )
        initial_candidate = candidate
        initial_issues = list(issues)
        candidate_extraction_attempted = False
        repaired_candidate: TurnUnderstanding | None = None
        repaired: dict[str, Any] | None = None
        if candidate is not None and not _has_semantic_boundary_issues(issues):
            feedback_status = _boundary_status(candidate.living_reaction_feedback_metrics)
            candidate, candidate_extraction_attempted, quiet_continuation_conflict = (
                _recover_candidate_if_needed(
                    candidate,
                    text=text,
                    turn_context=turn_context,
                    current_time=active.get("current_time"),
                    client=self.reasoning.client,
                )
            )
            candidate = _recover_feedback_if_needed(
                candidate,
                text=text,
                turn_context=turn_context,
                client=self.reasoning.client,
            )
            candidate_status = _boundary_status(candidate.living_context_candidate_metrics)
            if candidate_extraction_attempted and (
                candidate.living_context_candidate is None
                or quiet_continuation_conflict
            ):
                # Preserve an independently valid feedback artifact, but do
                # not enter a second full semantic repair after this bounded
                # candidate recovery has failed.
                return candidate
            # Candidate and feedback are independent optional model artifacts.
            # A valid candidate remains useful even when optional feedback is
            # malformed; a valid feedback artifact is retained across a
            # candidate repair attempt below.
            if candidate_status in {"accepted", "repaired", "absent"} and not quiet_continuation_conflict:
                if candidate_status in {"accepted", "repaired"} or feedback_status in {
                    "accepted",
                    "repaired",
                    "absent",
                }:
                    return candidate
            elif feedback_status in {"accepted", "repaired"}:
                # Try to recover the Situation candidate, but never throw away
                # an independently valid reaction feedback proposal.
                pass
            if quiet_continuation_conflict:
                issues = [
                    *issues,
                    "living_context_candidate:quiet_conflicts_with_explicit_continuation",
                ]

        repairable_status = str(result.get("status") or "") in {
            "invalid_json",
            "invalid_response",
            "model_assisted",
        }
        if repairable_status and not candidate_extraction_attempted:
            repair_payload = {
                **payload,
                "validation_issues": issues or [_safe_status_marker(result.get("status"))],
                "previous_candidate": _repair_candidate_for_prompt(result),
            }
            repaired = _complete_model_json_safe(
                self.reasoning.client,
                purpose="turn_understanding_repair",
                system=TURN_UNDERSTANDING_REPAIR_SYSTEM,
                user=json.dumps(repair_payload, ensure_ascii=False),
            )
            self.reasoning._trace(
                "turn_understanding_repair",
                repaired,
                {"text_len": len(text), "validation_issues": issues[:8]},
            )
            repaired_candidate, repaired_issues = _validated_model_understanding(
                repaired,
                source_text=text,
                current_time=active.get("current_time"),
                catalog=catalog,
                conversation_binding=conversation_binding,
            )
            repaired_quiet_conflict = False
            if repaired_candidate is not None and not _has_semantic_boundary_issues(repaired_issues):
                repaired_candidate, recovery_attempted, repaired_quiet_conflict = (
                    _recover_candidate_if_needed(
                        repaired_candidate,
                        text=text,
                        turn_context=turn_context,
                        current_time=active.get("current_time"),
                        client=self.reasoning.client,
                    )
                )
                candidate_extraction_attempted = (
                    candidate_extraction_attempted or recovery_attempted
                )
            if (
                repaired_candidate is not None
                and not _has_semantic_boundary_issues(repaired_issues)
                and _model_boundary_is_usable(repaired_candidate)
                and not repaired_quiet_conflict
                and (
                    repaired_candidate.living_context_candidate is not None
                    or _boundary_status(repaired_candidate.living_reaction_feedback_metrics)
                    in {"accepted", "repaired"}
                )
            ):
                _merge_model_boundary_metrics(
                    repaired_candidate,
                    initial=candidate,
                    repair_attempted=True,
                )
                repaired_candidate.reason = (
                    f"{repaired_candidate.reason}; semantic repair retry passed"
                    if repaired_candidate.reason
                    else "semantic repair retry passed"
                )
                return repaired_candidate
            issues = repaired_issues or issues
            if repaired_quiet_conflict:
                issues = [
                    *issues,
                    "living_context_candidate:quiet_after_explicit_continuation_repair",
                ]

        # A strict candidate rejection must not erase an exact, independently
        # validated feedback token/label/quote.  Keep the model semantic frame
        # and feedback while leaving Situation admission fail-closed.
        if (
            initial_candidate is not None
            and not _has_semantic_boundary_issues(initial_issues)
            and _boundary_status(initial_candidate.living_reaction_feedback_metrics)
            in {"accepted", "repaired"}
        ):
            _merge_model_boundary_metrics(
                initial_candidate,
                initial=initial_candidate,
                repair_attempted=bool(repairable_status),
            )
            initial_candidate.reason = (
                f"{initial_candidate.reason}; Situation candidate rejected at model boundary"
                if initial_candidate.reason
                else "Situation candidate rejected at model boundary"
            )
            return initial_candidate

        if fallback is not None and not candidate_extraction_attempted:
            # The combined understanding envelope is larger than the
            # Situation contract and may fail independently at transport. A
            # failed full response repair is still one bounded opportunity for
            # the smaller candidate seam; never start another full repair.
            return _attach_candidate_to_fallback(
                fallback,
                text=text,
                turn_context=turn_context,
                current_time=active.get("current_time"),
                client=self.reasoning.client,
                prior_repair_attempted=bool(repairable_status),
                combined_status=_safe_status_marker(result.get("status")),
                full_repair_status=(
                    _safe_status_marker(repaired.get("status"))
                    if isinstance(repaired, dict)
                    else None
                ),
            )
        if not repairable_status or fallback is None:
            return None
        degraded = replace(
            fallback,
            semantic_frame=TurnSemanticFrame.fallback(text, resolver_status="invalid_output"),
            source="model_invalid_output",
            reason=f"model semantic output invalid after bounded repair: {', '.join(issues[:4]) or 'invalid output'}",
        )
        boundary_holder = repaired_candidate or initial_candidate
        candidate_metrics = (
            dict(boundary_holder.living_context_candidate_metrics)
            if boundary_holder is not None
            else _rejected_boundary_report("living_context_candidate", issues, result)
        )
        feedback_metrics = (
            dict(boundary_holder.living_reaction_feedback_metrics)
            if boundary_holder is not None
            else _rejected_boundary_report("living_reaction_feedback", issues, result)
        )
        degraded.living_context_candidate_metrics = candidate_metrics
        degraded.living_reaction_feedback_metrics = feedback_metrics
        degraded.living_context_candidate_issues = _boundary_issue_subset(
            issues,
            "living_context_candidate",
        )
        degraded.living_reaction_feedback_issues = _boundary_issue_subset(
            issues,
            "living_reaction_feedback",
        )
        degraded.model_boundary_metrics = {
            "candidate": dict(candidate_metrics),
            "feedback": dict(feedback_metrics),
            "repair_attempted": bool(repairable_status),
            "initial": (
                dict(initial_candidate.model_boundary_metrics)
                if initial_candidate is not None
                else {}
            ),
        }
        return degraded

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


def _quiet_candidate_conflicts_with_explicit_continuation(
    *,
    text: str,
    candidate: LivingContextCandidate | None,
    turn_context: dict[str, Any],
) -> bool:
    """Identify a narrow model-boundary conflict without choosing a Situation.

    A valid ``quiet`` proposal is normally authoritative as a model boundary
    result.  The one exception is a user turn that explicitly says it is
    adding progress/change to an existing Situation while the current catalog
    is non-empty.  The marker set is discourse-level (continuation/update),
    not a domain or scenario vocabulary; the bounded repair model still has
    to select an exact catalog row and produce the typed update candidate.
    """

    if candidate is None or candidate.disposition != "quiet":
        return False
    rows = turn_context.get("living_context_situation_candidates")
    if not isinstance(rows, list) or not rows:
        return False
    compact = re.sub(r"[\s，,。！？!?、:：;；]+", "", str(text or "")).lower()
    if not compact:
        return False
    continuation_markers = (
        "补充",
        "更新",
        "进展",
        "原事项",
        "原来的事项",
        "记到",
        "同一件",
        "继续处理",
        "addthis",
        "addthis to",
        "update",
        "progress",
        "followup",
        "follow-up",
        "same situation",
        "original situation",
    )
    return any(marker.replace(" ", "") in compact for marker in continuation_markers)


def _answer_recovery_target(
    candidate: LivingContextCandidate | None,
    *,
    catalog: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return one exact row plus all current Need/Unknown endpoints for one batch call."""

    if candidate is None or candidate.disposition not in {"update", "correct", "resolve"}:
        return None
    if not candidate.situation_token or candidate.situation_revision is None or not candidate.catalog_token:
        return None
    if not candidate.material_change.strip() and not candidate.known:
        return None
    matched_rows: list[dict[str, Any]] = []
    for row in catalog:
        if not isinstance(row, dict):
            continue
        row_revision = row.get("observation_revision")
        if row_revision is None:
            row_revision = row.get("situation_revision")
        if (
            str(row.get("situation_token") or "") == str(candidate.situation_token)
            and type(row_revision) is int
            and int(row_revision) == int(candidate.situation_revision)
            and str(row.get("catalog_token") or "") == str(candidate.catalog_token)
        ):
            matched_rows.append(row)
    if len(matched_rows) != 1:
        return None
    raw_open_needs = matched_rows[0].get("open_needs")
    if not isinstance(raw_open_needs, list):
        return None
    active_open_needs: list[dict[str, Any]] = []
    seen_references: set[tuple[str, int]] = set()
    for raw_need in raw_open_needs:
        if not isinstance(raw_need, dict):
            continue
        status = str(raw_need.get("status") or "open")
        if status not in {"open", "asked", "observing", "waiting"}:
            continue
        blocked = raw_need.get("blocked_judgment")
        token = raw_need.get("need_token")
        generation = raw_need.get("generation")
        if (
            not isinstance(blocked, str)
            or not blocked.strip()
            or not isinstance(token, str)
            or not token.strip()
            or type(generation) is not int
            or generation < 1
        ):
            return None
        reference = (token, generation)
        if reference in seen_references:
            return None
        seen_references.add(reference)
        active_open_needs.append(
            {
                "need_token": token,
                "generation": generation,
                "blocked_judgment": blocked,
                "question": str(raw_need.get("question") or ""),
                "status": status,
            }
        )
    raw_unknown_endpoints = matched_rows[0].get("unknown_endpoints")
    if not isinstance(raw_unknown_endpoints, list):
        # ``TurnContextBuilder`` intentionally preserves the existing bounded
        # ``unknown`` field.  The runtime catalog replaces its strings with
        # endpoint objects while retaining this compatibility key.
        raw_unknown_endpoints = matched_rows[0].get("unknown")
    standalone_unknown_endpoints = [
        item
        for item in (raw_unknown_endpoints or [])
        if isinstance(item, dict)
        and set(item) == {"unknown_token", "generation", "statement"}
    ]
    if not active_open_needs and not standalone_unknown_endpoints:
        return None
    if not active_open_needs and not candidate.known:
        return None
    return {
        "row": matched_rows[0],
        "open_needs": active_open_needs,
        "candidate_known": [known.model_dump(mode="json") for known in candidate.known],
        "candidate_unknown": list(candidate.unknown),
        "candidate_need_blocked": [need.blocked_judgment for need in candidate.needs],
        "standalone_unknown_endpoints": standalone_unknown_endpoints,
    }


def _apply_need_answer_selector(
    understanding: TurnUnderstanding,
    *,
    candidate: LivingContextCandidate,
    text: str,
    catalog: list[dict[str, Any]],
    client: Any,
) -> tuple[LivingContextCandidate, bool]:
    """Run one batch selector and merge only its exact typed decisions."""

    target = _answer_recovery_target(candidate, catalog=catalog)
    if target is None:
        return candidate, False
    selection = select_living_context_need_answers(
        client,
        user_message=text,
        row=target["row"],
        open_needs=target["open_needs"],
        candidate_known=target["candidate_known"],
        candidate_unknown=target["candidate_unknown"],
        candidate_need_blocked=target["candidate_need_blocked"],
        standalone_unknown_endpoints=target["standalone_unknown_endpoints"],
        semantic_frame=understanding.semantic_frame,
    )
    boundary = dict(understanding.model_boundary_metrics)
    boundary["need_answer_selector"] = dict(selection.metrics)
    understanding.model_boundary_metrics = boundary
    if selection.selection is not None:
        selected = selection.selection
        understanding.living_context_standalone_unknown_resolutions = (
            selected.standalone_unknown_resolutions
        )
        existing_tokens = list(candidate.answered_need_tokens)
        existing_bindings = list(candidate.answered_need_bindings)
        seen_pairs = {
            (binding.need_token, binding.generation)
            for binding in existing_bindings
        }
        for answer in selected.answers:
            pair = (answer.need_token, answer.generation)
            if pair in seen_pairs:
                continue
            existing_bindings.append(
                CandidateNeedReference(
                    need_token=answer.need_token,
                    generation=answer.generation,
                )
            )
            if answer.need_token not in existing_tokens:
                existing_tokens.append(answer.need_token)
            seen_pairs.add(pair)
        discarded_unknown = set(selected.discard_unknown)
        discarded_need_blocked = set(selected.discard_need_blocked)
        updates: dict[str, Any] = {
            "answered_need_tokens": existing_tokens,
            "answered_need_bindings": existing_bindings,
            "unknown": [
                value for value in candidate.unknown
                if value not in discarded_unknown
            ],
            "needs": [
                need for need in candidate.needs
                if need.blocked_judgment not in discarded_need_blocked
            ],
        }
        if selected.answers or selected.standalone_unknown_resolutions:
            # The quote is generated from the server-visible turn prefix, not
            # copied from the model response. All answers in one batch share
            # this exact source-bound root quote.
            root_quote = (
                selected.answers[0].source_quote
                if selected.answers
                else selected.standalone_unknown_resolutions[0].source_quote
            )
            updates.update(
                {
                    "source_quote": root_quote,
                    "assertion_mode": "direct_user",
                }
            )
        candidate = candidate.model_copy(update=updates)
        understanding.reason = (
            f"{understanding.reason}; bounded Need-answer selector accepted"
            if understanding.reason
            else "bounded Need-answer selector accepted"
        )
    else:
        understanding.living_context_standalone_unknown_resolutions = ()
        reason = str(selection.metrics.get("reason") or "rejected")
        understanding.reason = (
            f"{understanding.reason}; bounded Need-answer selector {reason}"
            if understanding.reason
            else f"bounded Need-answer selector {reason}"
        )
    return candidate, True


def _recover_candidate_if_needed(
    understanding: TurnUnderstanding,
    *,
    text: str,
    turn_context: dict[str, Any],
    current_time: Any,
    client: Any,
) -> tuple[TurnUnderstanding, bool, bool]:
    """Run the single candidate-only recovery seam when it is needed.

    Both the initial model result and a successful full semantic repair use
    this helper.  The helper never decides admission and never retries the
    full Understanding response; its typed result remains subject to the
    existing server catalog/CAS downstream.
    """

    catalog = (
        turn_context.get("living_context_situation_candidates")
        if isinstance(turn_context.get("living_context_situation_candidates"), list)
        else []
    )
    primary_candidate = understanding.living_context_candidate
    primary_binding_issues = validate_candidate_catalog_binding(
        primary_candidate,
        catalog,
    ) if primary_candidate is not None else []
    if primary_binding_issues:
        # Keep the primary artifact's diagnostics, but mark it rejected for
        # this catalog snapshot before bounded recovery.  The model boundary
        # must never silently rewrite a stale revision or token.
        primary_metrics = dict(understanding.living_context_candidate_metrics)
        primary_metrics.update(
            {
                "status": "rejected",
                "issue_codes": list(primary_binding_issues),
                "binding_issues": list(primary_binding_issues),
            }
        )
        understanding.living_context_candidate_metrics = primary_metrics
        understanding.living_context_candidate_issues = list(
            dict.fromkeys(
                [
                    *understanding.living_context_candidate_issues,
                    *primary_binding_issues,
                ]
            )
        )[:16]
    quiet_conflict = _quiet_candidate_conflicts_with_explicit_continuation(
        text=text,
        candidate=primary_candidate,
        turn_context=turn_context,
    )
    # Need-answer recovery is a strictly independent selector seam.  It is
    # eligible only after the primary candidate has an exact Situation binding;
    # stale/missing/quiet candidates continue through the ordinary extractor
    # below. The helper captures the unique turn-start target and is the only
    # place that can call the selector.
    if (
        primary_candidate is not None
        and not primary_binding_issues
        and not quiet_conflict
    ):
        selected_candidate, selector_called = _apply_need_answer_selector(
            understanding,
            candidate=primary_candidate,
            text=text,
            catalog=catalog,
            client=client,
        )
        if selector_called:
            understanding.living_context_candidate = selected_candidate
            return understanding, True, False

    if (
        primary_candidate is not None
        and not quiet_conflict
        and not primary_binding_issues
    ):
        return understanding, False, False

    extraction = extract_living_context_candidate(
        client,
        text=text,
        current_time=current_time,
        catalog=catalog,
        primary_understanding={
            "intent": understanding.intent,
            "task_type": understanding.task_type,
            "task_summary": understanding.task_summary,
            "explicit_request": understanding.explicit_request,
            "user_goal": understanding.user_goal,
            "hidden_need": understanding.hidden_need,
        },
    )
    if extraction.candidate is not None and not extraction.issues:
        understanding.living_context_candidate = extraction.candidate
        understanding.living_context_candidate_issues = []
        understanding.living_context_candidate_metrics = dict(extraction.metrics)
        _record_candidate_extraction(understanding, extraction)
        quiet_conflict = _quiet_candidate_conflicts_with_explicit_continuation(
            text=text,
            candidate=understanding.living_context_candidate,
            turn_context=turn_context,
        )
        if quiet_conflict:
            understanding.living_context_candidate_issues = [
                "living_context_candidate:quiet_after_explicit_continuation_extraction",
            ]
        if not quiet_conflict:
            selected_candidate, selector_called = _apply_need_answer_selector(
                understanding,
                candidate=understanding.living_context_candidate,
                text=text,
                catalog=catalog,
                client=client,
            )
            if selector_called:
                understanding.living_context_candidate = selected_candidate
    else:
        _record_candidate_extraction(understanding, extraction)
        if primary_binding_issues:
            # Do not leave a parsed-but-stale candidate object attached to the
            # turn when recovery fails.  Its primary issue/metrics remain in
            # model_boundary_metrics.primary_candidate.
            understanding.living_context_candidate = None
            understanding.living_context_candidate_issues = list(
                dict.fromkeys(
                    [
                        *primary_binding_issues,
                        *understanding.living_context_candidate_issues,
                        *list(extraction.issues or []),
                    ]
                )
            )[:16]
        if quiet_conflict:
            understanding.living_context_candidate_issues = [
                "living_context_candidate:quiet_conflicts_with_explicit_continuation",
                *extraction.issues[:2],
            ]
    return understanding, True, quiet_conflict


def _recover_feedback_if_needed(
    understanding: TurnUnderstanding,
    *,
    text: str,
    turn_context: dict[str, Any],
    client: Any,
) -> TurnUnderstanding:
    """Recover only an omitted current-reaction feedback artifact.

    The combined understanding response and the optional feedback proposal
    are independent model products.  When the response already carries a
    strict feedback artifact this helper is a no-op.  Otherwise one bounded
    feedback-only call receives the exact current reaction rows and may return
    either an independently source-bound proposal or explicit absence.  It
    never selects a Situation, rewrites a candidate, or changes admission.
    """

    feedback_status = _boundary_status(understanding.living_reaction_feedback_metrics)
    if feedback_status in {"accepted", "repaired"}:
        return understanding
    catalog = (
        turn_context.get("living_context_situation_candidates")
        if isinstance(turn_context.get("living_context_situation_candidates"), list)
        else []
    )
    extraction = extract_living_reaction_feedback(
        client,
        text=text,
        catalog=catalog,
    )
    if extraction.feedback is not None and not extraction.issues:
        understanding.living_reaction_feedback = extraction.feedback
        understanding.living_reaction_feedback_issues = []
        understanding.living_reaction_feedback_metrics = dict(extraction.metrics)
        boundary_metrics = dict(understanding.model_boundary_metrics)
        boundary_metrics["feedback"] = dict(extraction.metrics)
        boundary_metrics["feedback_extraction"] = dict(extraction.metrics)
        understanding.model_boundary_metrics = boundary_metrics
        return understanding
    if extraction.metrics.get("extractor_attempted"):
        understanding.living_reaction_feedback = None
        understanding.living_reaction_feedback_issues = list(extraction.issues)[:16]
        understanding.living_reaction_feedback_metrics = dict(extraction.metrics)
        boundary_metrics = dict(understanding.model_boundary_metrics)
        boundary_metrics["feedback"] = dict(extraction.metrics)
        boundary_metrics["feedback_extraction"] = dict(extraction.metrics)
        understanding.model_boundary_metrics = boundary_metrics
    return understanding


def _attach_candidate_to_fallback(
    understanding: TurnUnderstanding,
    *,
    text: str,
    turn_context: dict[str, Any],
    current_time: Any,
    client: Any,
    prior_repair_attempted: bool = False,
    combined_status: str = "unavailable",
    full_repair_status: str | None = None,
) -> TurnUnderstanding:
    """Attach one typed candidate when the combined model envelope is absent.

    The rest of the turn remains the ordinary deterministic understanding;
    only the optional Living Context artifact is model-derived. This avoids
    treating a transport failure in the larger prompt as proof that the user
    did not introduce or update a Situation.
    """

    catalog = (
        turn_context.get("living_context_situation_candidates")
        if isinstance(turn_context.get("living_context_situation_candidates"), list)
        else []
    )
    extraction = extract_living_context_candidate(
        client,
        text=text,
        current_time=current_time,
        catalog=catalog,
        primary_understanding={
            "intent": understanding.intent,
            "task_type": understanding.task_type,
            "task_summary": understanding.task_summary,
            "explicit_request": understanding.explicit_request,
            "user_goal": understanding.user_goal,
            "hidden_need": understanding.hidden_need,
        },
    )
    understanding.living_context_candidate = extraction.candidate
    understanding.living_context_candidate_metrics = dict(extraction.metrics)
    understanding.living_context_candidate_issues = list(extraction.issues)[:16]
    understanding.model_boundary_metrics = {
        "candidate": dict(extraction.metrics),
        "feedback": dict(understanding.living_reaction_feedback_metrics),
        "candidate_extraction": dict(extraction.metrics),
        "repair_attempted": bool(
            extraction.metrics.get("repair_attempted")
            or prior_repair_attempted
        ),
        "combined_understanding_available": False,
        "combined_understanding_status": combined_status,
    }
    if full_repair_status is not None:
        understanding.model_boundary_metrics["full_repair_status"] = full_repair_status
    if extraction.candidate is not None:
        understanding.source = "model_candidate"
        understanding.reason = "bounded model candidate extracted after combined understanding was unavailable"
        # The ordinary extractor is another entry point into the same
        # optional candidate seam. Reuse the single batch selector here too;
        # its helper is called at most once for this turn.
        selected_candidate, selector_called = _apply_need_answer_selector(
            understanding,
            candidate=extraction.candidate,
            text=text,
            catalog=catalog,
            client=client,
        )
        if selector_called:
            understanding.living_context_candidate = selected_candidate
    else:
        understanding.reason = "combined understanding unavailable and bounded model candidate was not admitted"
    return understanding


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


def _complete_model_json_safe(
    client: Any,
    *,
    purpose: str,
    system: str,
    user: str,
) -> dict[str, Any]:
    """Keep model transport failures inside the bounded understanding seam.

    The normal ``CoreModelClient`` already returns a structured status, but a
    test/provider adapter can still raise while completing a request.  Keep
    only a stable exception type marker; never retain or surface exception
    text, then let the caller use the same candidate-only fallback path.
    """

    try:
        result = client.complete_json(
            purpose=purpose,
            system=system,
            user=user,
        )
    except Exception as exc:
        return {
            "status": "exception",
            "error_type": type(exc).__name__[:80],
        }
    if isinstance(result, dict):
        safe_result = dict(result)
        if safe_result.get("status") in {"error", "http_error"}:
            # CoreModelClient normally returns a provider exception string in
            # ``error``.  Preserve the transport class, but never carry that
            # text into the understanding trace or boundary diagnostics.
            safe_result.pop("error", None)
            safe_result["error_code"] = "transport_error"
        return safe_result
    return {
        "status": "invalid_response",
        "response_type": type(result).__name__[:80],
    }


_SAFE_MODEL_STATUS_MARKERS = frozenset(
    {
        "model_assisted",
        "invalid_json",
        "invalid_response",
        "error",
        "http_error",
        "unconfigured",
        "unsupported_provider",
        "auth_missing",
        "exception",
        "skipped",
    }
)


def _safe_status_marker(value: Any) -> str:
    marker = str(value or "").strip()
    return marker if marker in _SAFE_MODEL_STATUS_MARKERS else "unknown"


def _with_situation_catalog_selectors(
    turn_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy a turn context and add deterministic selectors to catalog rows."""

    context = dict(turn_context) if isinstance(turn_context, dict) else {}
    raw_catalog = context.get("living_context_situation_candidates")
    if not isinstance(raw_catalog, list):
        return context
    catalog: list[dict[str, Any]] = []
    for raw_row in raw_catalog:
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        selector = row.get("situation_selector")
        owner_id = row.get("owner_id")
        session_id = row.get("session_id")
        situation_token = row.get("situation_token")
        if (
            isinstance(owner_id, str)
            and owner_id
            and isinstance(session_id, str)
            and session_id
            and isinstance(situation_token, str)
            and situation_token
        ):
            expected = situation_catalog_selector(owner_id, session_id, situation_token)
            # Only advertise a selector that agrees with the server-derived
            # value.  An inconsistent incoming projection is left without a
            # selector and remains governed by the existing full-token path.
            if selector is None:
                row["situation_selector"] = expected
            elif selector != expected:
                row.pop("situation_selector", None)
        catalog.append(row)
    context["living_context_situation_candidates"] = catalog
    context["conversation_binding"] = _safe_conversation_binding(
        context.get("conversation_binding")
    )
    return context


def _safe_conversation_binding(value: Any) -> dict[str, str] | None:
    """Keep only the opaque server-derived continuation selector."""

    if not isinstance(value, dict):
        return None
    if str(value.get("binding_type") or "").strip().lower() != "situation":
        return None
    selector = str(value.get("situation_selector") or "").strip()
    if not re.fullmatch(r"sitref_[0-9a-f]{16}", selector):
        return None
    return {
        "binding_type": "situation",
        "situation_selector": selector,
    }


def _bind_unbound_continuation_candidate(
    value: Any,
    *,
    conversation_binding: dict[str, str] | None,
    catalog: list[dict[str, Any]],
) -> Any:
    """Add one server-selected Situation binding before strict parsing."""

    if not isinstance(value, dict) or str(value.get("disposition") or "") not in {"update", "correct", "resolve"}:
        return value
    fields = ("situation_selector", "situation_token", "situation_revision", "catalog_token")
    if any(field in value for field in fields):
        return value
    binding = _safe_conversation_binding(conversation_binding)
    if binding is None:
        return value
    selector = binding["situation_selector"]
    matches: list[dict[str, Any]] = []
    for row in catalog:
        if not isinstance(row, dict) or row.get("situation_selector") != selector:
            continue
        revision = row.get("observation_revision", row.get("situation_revision"))
        token = row.get("situation_token")
        catalog_token = row.get("catalog_token")
        if isinstance(token, str) and token and type(revision) is int and revision >= 1 and isinstance(catalog_token, str) and catalog_token:
            matches.append(row)
    if len(matches) != 1:
        return value
    row = matches[0]
    repaired = deepcopy(value)
    repaired.update({
        "situation_selector": selector,
        "situation_token": row["situation_token"],
        "situation_revision": row.get("observation_revision", row.get("situation_revision")),
        "catalog_token": row["catalog_token"],
    })
    return repaired


def _validated_model_understanding(
    payload: dict[str, Any],
    *,
    source_text: str,
    current_time: Any = None,
    catalog: list[dict[str, Any]] | None = None,
    conversation_binding: dict[str, str] | None = None,
) -> tuple[TurnUnderstanding | None, list[str]]:
    if payload.get("status") != "model_assisted":
        status = _safe_status_marker(payload.get("status"))
        return None, [status if status != "unknown" else "model_transport_failure"]
    candidate = TurnUnderstanding.from_payload(
        payload,
        source_text=source_text,
        current_time=current_time,
        catalog=catalog,
        conversation_binding=conversation_binding,
    )
    frame = candidate.semantic_frame
    if frame is None:
        return None, ["semantic_frame_missing"]
    if frame.resolver_status == "invalid_output":
        return None, ["semantic_frame_schema_or_source_binding_invalid"]
    issues = semantic_frame_quality_issues(frame, source_text)
    if candidate.living_context_candidate_issues:
        issues.extend(_ensure_boundary_issue_prefix(
            candidate.living_context_candidate_issues[:4],
            "living_context_candidate",
        ))
    if candidate.living_reaction_feedback_issues:
        issues.extend(_ensure_boundary_issue_prefix(
            candidate.living_reaction_feedback_issues[:4],
            "living_reaction_feedback",
        ))
    return candidate, issues


def _boundary_status(metrics: dict[str, Any] | None) -> str:
    if not isinstance(metrics, dict):
        return "absent"
    return str(metrics.get("status") or "absent")


def _has_semantic_boundary_issues(issues: list[str]) -> bool:
    """Return true only for frame/transport issues, not optional artifacts."""

    return any(
        not str(issue).startswith(("living_context_candidate:", "living_reaction_feedback:"))
        for issue in issues
    )


def _model_boundary_is_usable(understanding: TurnUnderstanding) -> bool:
    candidate_status = _boundary_status(understanding.living_context_candidate_metrics)
    feedback_status = _boundary_status(understanding.living_reaction_feedback_metrics)
    return candidate_status in {"accepted", "repaired", "absent"} or feedback_status in {
        "accepted",
        "repaired",
    }


def _merge_model_boundary_metrics(
    understanding: TurnUnderstanding,
    *,
    initial: TurnUnderstanding | None,
    repair_attempted: bool,
) -> None:
    """Keep bounded quality counters without copying model output."""

    initial_metrics = initial.model_boundary_metrics if initial is not None else {}
    existing_metrics = dict(understanding.model_boundary_metrics)
    merged = {
        "candidate": dict(understanding.living_context_candidate_metrics),
        "feedback": dict(understanding.living_reaction_feedback_metrics),
        "repair_attempted": bool(
            repair_attempted or existing_metrics.get("repair_attempted")
        ),
        "initial": dict(initial_metrics) if isinstance(initial_metrics, dict) else {},
    }
    for key in ("candidate_extraction", "primary_candidate"):
        if key in existing_metrics:
            merged[key] = dict(existing_metrics[key])
    understanding.model_boundary_metrics = merged


def _record_candidate_extraction(
    understanding: TurnUnderstanding,
    extraction: Any,
) -> None:
    """Attach bounded extractor diagnostics without copying model payloads."""

    previous_candidate_metrics = dict(understanding.living_context_candidate_metrics)
    extraction_metrics = dict(getattr(extraction, "metrics", {}) or {})
    if getattr(extraction, "candidate", None) is None and str(
        previous_candidate_metrics.get("status") or ""
    ) in {"absent", "rejected"}:
        # The primary artifact was not admissible and the bounded recovery
        # also failed.  Surface the recovery's safe issue codes instead of
        # leaving an unhelpful ``absent/none`` diagnosis at the boundary.
        understanding.living_context_candidate_metrics = extraction_metrics
        extraction_issues = list(getattr(extraction, "issues", []) or [])
        if extraction_issues:
            understanding.living_context_candidate_issues = extraction_issues[:16]
    boundary = dict(understanding.model_boundary_metrics)
    boundary["candidate_extraction"] = extraction_metrics
    boundary["primary_candidate"] = previous_candidate_metrics
    boundary["repair_attempted"] = True
    boundary["candidate"] = dict(understanding.living_context_candidate_metrics)
    boundary["feedback"] = dict(understanding.living_reaction_feedback_metrics)
    understanding.model_boundary_metrics = boundary
    if getattr(extraction, "candidate", None) is not None:
        understanding.reason = (
            f"{understanding.reason}; bounded candidate extraction passed"
            if understanding.reason
            else "bounded candidate extraction passed"
        )
    else:
        issues = list(getattr(extraction, "issues", []) or [])
        if issues:
            understanding.reason = (
                f"{understanding.reason}; bounded candidate extraction unavailable"
                if understanding.reason
                else "bounded candidate extraction unavailable"
            )


def _boundary_issue_subset(issues: list[str], prefix: str) -> list[str]:
    return [str(issue) for issue in issues if str(issue).startswith(f"{prefix}:")][:16]


def _ensure_boundary_issue_prefix(issues: list[str], prefix: str) -> list[str]:
    return [
        str(issue)
        if str(issue).startswith(f"{prefix}:")
        else f"{prefix}:{issue}"
        for issue in issues
    ]


def _rejected_boundary_report(
    prefix: str,
    issues: list[str],
    result: dict[str, Any],
) -> dict[str, Any]:
    codes = _boundary_issue_subset(issues, prefix)
    if not codes:
        transport_status = _safe_status_marker(result.get("status"))
        codes = [f"{prefix}:transport:{transport_status}"]
    return {
        "status": "rejected",
        "repair_count": 0,
        "repaired_fields": [],
        "issue_codes": codes[:16],
    }


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
