from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.awareness_context_assembler import AwarenessContextAssembler
from core.model_client import redact_sensitive
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


TURN_UNDERSTANDING_SYSTEM = (
    "You are Veyra Core's user understanding layer inside an awareness-driven runtime. "
    "The user message is the primary input. The awareness_snapshot contains Veyra's current perception "
    "from belief claims and local_world probe cache; treat it as useful but not automatically true or fresh. "
    "Return strict JSON only. Produce user understanding and awareness orientation, not a route decision. "
    "Start by understanding the person and situation: explicit request, hidden need, emotion, project, goal risk, "
    "constraints, capability needs, time scale, history links, and evidence gaps. "
    "Do not execute probes or agents, do not write the final user reply, and do not treat missing evidence as fact."
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
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TurnUnderstanding":
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
            source=str(payload.get("source") or ("model" if payload.get("status") == "model_assisted" else "")),
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
            },
            max_string=360,
            max_list=6,
        )

    def is_strategic_discussion(self) -> bool:
        return self.suggested_mode in {"strategic_discussion", "meta_cognition_discussion", "project_direction_review"}


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
        snapshot = awareness_snapshot if isinstance(awareness_snapshot, dict) else self._awareness_snapshot(text=text, attention_focus=attention_focus)
        fallback = self.fallback(text=text, attention_focus=attention_focus, event=event, awareness_snapshot=snapshot)
        if self._prefer_rule_understanding(fallback):
            return fallback
        if allow_model:
            model = self.orient_model(text=text, awareness_snapshot=snapshot, turn_context=turn_context or {}, event=event)
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
    ) -> TurnUnderstanding | None:
        if not self.reasoning.is_enabled():
            return None
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
                    "history_links": "list of prior context anchors that matter",
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
                "retrieval_hints": "optional list: conversation, belief, user, task, capabilities, runtime",
            },
        }
        result = self.reasoning.client.complete_json(
            purpose="turn_understanding",
            system=TURN_UNDERSTANDING_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self.reasoning._trace("turn_understanding", result, {"text_len": len(text)})
        if result.get("status") != "model_assisted":
            return None
        return TurnUnderstanding.from_payload(result)

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
        return TurnUnderstanding.from_payload(payload)

    def _awareness_snapshot(self, *, text: str, attention_focus: list[str]) -> dict[str, Any]:
        if not self.assembler:
            return {"status": "unavailable"}
        return self.assembler.snapshot(user_message=text, attention_focus=attention_focus)

    def _prefer_rule_understanding(self, understanding: TurnUnderstanding) -> bool:
        if understanding.source != "rule_fallback" or understanding.confidence < 0.8:
            return False
        return understanding.suggested_mode in {
            "strategic_discussion",
            "meta_cognition_discussion",
            "runtime_evidence",
            "governed_execution",
        }

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
