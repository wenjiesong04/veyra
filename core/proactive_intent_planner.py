from __future__ import annotations

import json
import re
from typing import Any

from core.model_client import CoreModelClient, redact_sensitive
from core.proactive_intent import PROACTIVE_INTENT_TYPES, PROACTIVE_NEXT_ACTIONS, ProactiveIntent
from core.schedule_parser import has_explicit_schedule_time, parse_schedule_text
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


PROACTIVE_INTENT_SYSTEM = (
    "You are Veyra's ProactiveIntentPlanner. Return strict JSON only. "
    "You translate user needs into a ProactiveIntent draft, not an active commitment; you do not execute actions, "
    "create commitments, write memory, run probes, browse, or approve delivery. "
    "Cancellation, pause, and resume intents have highest priority. "
    "Long-term proactive behavior, external search, local monitoring, push delivery, "
    "and long-term memory writes require explicit user authorization. "
    "For requests such as daily weather, monitoring, reminders, or recurring digests, propose a draft and ask for "
    "confirmation unless the user is confirming an existing pending offer. "
    "If source, cadence, target, channel, or permission is unclear, set proposed_next_action=ask_confirmation. "
    "If Veyra lacks a template or capability, set intent_type=unknown and describe gaps."
)


class ProactiveIntentPlanner:
    """Model-assisted planner for generic proactive user needs."""

    CONTROL_TYPES = {"cancel_commitment", "pause_commitment", "resume_commitment"}
    AUTH_REQUIRED_TYPES = {"goal_start", "track_external_topic", "daily_digest", "monitor_local_state", "project_assistance", "learning_plan"}

    def __init__(self, state_store: WorldStateStore, client: CoreModelClient | None = None) -> None:
        self.state_store = state_store
        self.client = client or CoreModelClient(state_store)

    def plan(self, *, user_text: str, event: VeyraEvent, memory_summary: dict[str, Any] | None = None) -> ProactiveIntent:
        control = self._control_fallback(user_text=user_text, event=event)
        if control:
            self._trace(control, {"status": "control_fallback"})
            return control
        if self._is_state_inventory_question(user_text):
            answer_only = self._intent(
                event=event,
                user_text=user_text,
                intent_type="unknown",
                desired_outcome="answer the current state without creating proactive work",
                proposed_next_action="answer_only",
                requires_user_authorization=False,
                confidence=0.95,
                entities={"guardrail": "state_inventory_question"},
            )
            self._trace(answer_only, {"status": "state_inventory_guardrail"})
            return answer_only

        model = self._model_plan(user_text=user_text, event=event, memory_summary=memory_summary or {})
        if model and model.confidence >= 0.45:
            self._trace(model, {"status": "model_assisted"})
            return model

        fallback = self._fallback_plan(user_text=user_text, event=event)
        self._trace(fallback, {"status": "fallback", "model_status": model.source if model else "unavailable"})
        return fallback

    def rule_plan(self, *, user_text: str, event: VeyraEvent) -> ProactiveIntent:
        """Return the deterministic plan used to guard explicit control intents."""
        control = self._control_fallback(user_text=user_text, event=event)
        if control:
            return control
        if self._is_state_inventory_question(user_text):
            return self._intent(
                event=event,
                user_text=user_text,
                intent_type="unknown",
                desired_outcome="answer the current state without creating proactive work",
                proposed_next_action="answer_only",
                requires_user_authorization=False,
                confidence=0.95,
                entities={"guardrail": "state_inventory_question"},
            )
        return self._fallback_plan(user_text=user_text, event=event)

    def record_intent(self, intent: ProactiveIntent) -> dict[str, Any]:
        item = intent.to_dict()

        def append_intent(state: dict[str, Any]) -> dict[str, Any]:
            intents = state.get("intents") if isinstance(state.get("intents"), list) else []
            state["intents"] = [*intents, item][-200:]
            state["updated_at"] = utc_now_iso()
            return state

        self.state_store.mutate_json("proactive_intents.json", append_intent)
        return item

    def _model_plan(self, *, user_text: str, event: VeyraEvent, memory_summary: dict[str, Any]) -> ProactiveIntent | None:
        if not self.client.status().get("configured"):
            return None
        payload = {
            "user_message": user_text,
            "event_source": {
                "user_id": event.source.user_id,
                "session_id": event.source.session_id,
                "channel_id": event.source.channel,
            },
            "world_context": self._planner_context(memory_summary),
            "allowed_intent_types": sorted(PROACTIVE_INTENT_TYPES),
            "allowed_next_actions": sorted(PROACTIVE_NEXT_ACTIONS),
            "required_json_fields": {
                "intent_type": "one allowed intent type",
                "topic": "short topic or empty",
                "entities": "object with scope/type/source hints",
                "desired_outcome": "what the user wants Veyra to sustain or change",
                "cadence": "object e.g. {kind,time_local,timezone,interval_seconds}",
                "trigger_condition": "when Veyra should act",
                "information_sources": "list of allowed source categories",
                "local_context_needed": "list",
                "external_context_needed": "list",
                "memory_write_needed": "boolean",
                "requires_user_authorization": "boolean",
                "risk_level": "R0-R5",
                "confidence": "0.0-1.0",
                "proposed_next_action": "one allowed next action",
                "reason": "short rationale",
            },
        }
        result = self.client.complete_json(
            purpose="proactive_intent",
            system=PROACTIVE_INTENT_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        if result.get("status") != "model_assisted":
            return None
        intent = self._intent_from_payload(result, user_text=user_text, event=event, source="model")
        if intent.intent_type in self.CONTROL_TYPES:
            intent.proposed_next_action = self._control_next_action(intent.intent_type)
        intent = self._guard_model_intent(intent, user_text=user_text, event=event)
        if intent.requires_user_authorization and intent.proposed_next_action not in {
            "ask_confirmation",
            "create_commitment_draft",
            "create_watchlist_draft",
            "create_goal",
            "cancel_matching_commitments",
            "pause_matching_commitments",
            "resume_matching_commitments",
        }:
            intent.proposed_next_action = "ask_confirmation"
        return intent

    def _guard_model_intent(self, intent: ProactiveIntent, *, user_text: str, event: VeyraEvent) -> ProactiveIntent:
        fallback = self._fallback_plan(user_text=user_text, event=event)
        if self._should_use_template_guardrail(intent, fallback):
            fallback.intent_id = intent.intent_id
            fallback.source = "model"
            fallback.confidence = max(intent.confidence, fallback.confidence)
            fallback.entities = {
                **fallback.entities,
                "model_guardrail": "known_template_correction",
                "model_intent_type": intent.intent_type,
                "model_topic": intent.topic,
            }
            intent = fallback
        elif intent.intent_type == fallback.intent_type and fallback.confidence >= 0.6:
            if not intent.topic and fallback.topic:
                intent.topic = fallback.topic
            if not intent.cadence and fallback.cadence:
                intent.cadence = fallback.cadence
            if not intent.information_sources and fallback.information_sources:
                intent.information_sources = fallback.information_sources
            if not intent.local_context_needed and fallback.local_context_needed:
                intent.local_context_needed = fallback.local_context_needed
            if not intent.external_context_needed and fallback.external_context_needed:
                intent.external_context_needed = fallback.external_context_needed
            intent.entities = {**fallback.entities, **intent.entities}

        if self._model_intent_requires_authorization(intent):
            intent.requires_user_authorization = True
            intent.authorization_status = "pending_confirmation"
        return intent

    def _should_use_template_guardrail(self, intent: ProactiveIntent, fallback: ProactiveIntent) -> bool:
        if fallback.intent_type == "unknown" or fallback.confidence < 0.6:
            return False
        if intent.intent_type == "unknown":
            return True
        guarded_types = {"daily_digest", "monitor_local_state", "reminder"}
        return fallback.intent_type in guarded_types and intent.intent_type != fallback.intent_type

    def _model_intent_requires_authorization(self, intent: ProactiveIntent) -> bool:
        if intent.intent_type in self.AUTH_REQUIRED_TYPES:
            return True
        if intent.memory_write_needed:
            return True
        if intent.information_sources or intent.external_context_needed or intent.local_context_needed:
            return True
        return False

    def _fallback_plan(self, *, user_text: str, event: VeyraEvent) -> ProactiveIntent:
        text = user_text or ""
        lowered = text.lower()
        cadence = self._cadence(text)
        topic = self._topic(text)
        if self._is_learning(text):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="learning_plan",
                topic=topic or "学习计划",
                desired_outcome="start and sustain a learning plan",
                cadence=cadence or {"kind": "interval", "interval_seconds": 86400, "timezone": "Asia/Shanghai"},
                information_sources=["public_web", "courses", "papers"],
                external_context_needed=["learning resources"],
                memory_write_needed=True,
                proposed_next_action="create_goal",
                confidence=0.72,
            )
        if self._is_local_monitor(text):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="monitor_local_state",
                topic=topic or "local_service",
                desired_outcome="monitor local runtime state with safe probes",
                cadence=cadence or {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai"},
                local_context_needed=["safe_probe_status"],
                memory_write_needed=False,
                proposed_next_action="create_commitment_draft",
                confidence=0.68,
                entities={"probe_family": "runtime_status"},
            )
        if self._is_weather(text):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="daily_digest",
                topic="weather",
                desired_outcome="answer current weather and optionally offer a daily digest",
                cadence=cadence or {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai"},
                information_sources=["weather_probe"],
                external_context_needed=["weather"],
                proposed_next_action="ask_confirmation",
                confidence=0.78,
                entities={"location": self._location(text)},
            )
        if self._is_reminder(text):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="reminder",
                topic=topic or self._reminder_topic(text),
                desired_outcome="remind the user at the requested time",
                cadence=cadence or {"kind": "daily", "time_local": "09:00", "timezone": "Asia/Shanghai"},
                proposed_next_action="create_commitment_draft",
                confidence=0.7,
            )
        if self._is_external_tracking(text):
            confidence = 0.5 if any(marker in text for marker in ("奇怪", "这个", "某个")) else 0.68
            return self._intent(
                event=event,
                user_text=text,
                intent_type="track_external_topic" if confidence >= 0.55 else "unknown",
                topic=topic or "待确认主题",
                desired_outcome="track external updates and surface useful changes",
                cadence=cadence or {},
                information_sources=self._external_sources(text),
                external_context_needed=["source", "cadence"] if confidence < 0.55 else ["public_web"],
                proposed_next_action="create_watchlist_draft" if confidence >= 0.55 else "ask_confirmation",
                confidence=confidence,
                entities={"requires_source_confirmation": confidence < 0.55},
            )
        return self._intent(
            event=event,
            user_text=text,
            intent_type="unknown",
            topic=topic,
            desired_outcome="no proactive action requested",
            proposed_next_action="answer_only",
            requires_user_authorization=False,
            confidence=0.25,
        )

    def _control_fallback(self, *, user_text: str, event: VeyraEvent) -> ProactiveIntent | None:
        text = user_text or ""
        lowered = text.lower()
        if self._has_resume(text, lowered):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="resume_commitment",
                topic=self._topic(text),
                desired_outcome="resume matching proactive commitments",
                proposed_next_action="resume_matching_commitments",
                confidence=0.86,
                requires_user_authorization=False,
                entities=self._control_entities(text),
            )
        if self._has_pause(text, lowered):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="pause_commitment",
                topic=self._topic(text),
                desired_outcome="pause matching proactive commitments",
                proposed_next_action="pause_matching_commitments",
                confidence=0.88,
                requires_user_authorization=False,
                entities=self._control_entities(text),
            )
        if self._has_cancel(text, lowered):
            return self._intent(
                event=event,
                user_text=text,
                intent_type="cancel_commitment",
                topic=self._topic(text),
                desired_outcome="cancel matching proactive commitments",
                proposed_next_action="cancel_matching_commitments",
                confidence=0.9,
                requires_user_authorization=False,
                entities=self._control_entities(text),
            )
        return None

    def _intent(
        self,
        *,
        event: VeyraEvent,
        user_text: str,
        intent_type: str,
        topic: str = "",
        desired_outcome: str = "",
        cadence: dict[str, Any] | None = None,
        trigger_condition: str = "",
        information_sources: list[str] | None = None,
        local_context_needed: list[str] | None = None,
        external_context_needed: list[str] | None = None,
        memory_write_needed: bool = False,
        requires_user_authorization: bool = True,
        risk_level: str = "R1",
        confidence: float = 0.0,
        proposed_next_action: str = "ask_confirmation",
        entities: dict[str, Any] | None = None,
    ) -> ProactiveIntent:
        return ProactiveIntent(
            user_id=event.source.user_id,
            session_id=event.source.session_id,
            channel_id=event.source.channel,
            raw_text=user_text[:1000],
            intent_type=intent_type,
            topic=topic[:180],
            entities=entities or {},
            desired_outcome=desired_outcome[:500],
            cadence=cadence or {},
            trigger_condition=trigger_condition[:240],
            information_sources=(information_sources or [])[:8],
            local_context_needed=(local_context_needed or [])[:8],
            external_context_needed=(external_context_needed or [])[:8],
            memory_write_needed=memory_write_needed,
            requires_user_authorization=requires_user_authorization,
            authorization_status="pending_confirmation" if requires_user_authorization else "granted",
            risk_level=risk_level,
            confidence=confidence,
            proposed_next_action=proposed_next_action,
            source="fallback",
        )

    def _intent_from_payload(self, payload: dict[str, Any], *, user_text: str, event: VeyraEvent, source: str) -> ProactiveIntent:
        data = dict(payload)
        data.update(
            {
                "user_id": event.source.user_id,
                "session_id": event.source.session_id,
                "channel_id": event.source.channel,
                "raw_text": user_text[:1000],
                "source": source,
                "authorization_status": "pending_confirmation" if data.get("requires_user_authorization", True) else "granted",
            }
        )
        return ProactiveIntent.from_dict(data)

    def _planner_context(self, memory_summary: dict[str, Any]) -> dict[str, Any]:
        return redact_sensitive(
            {
                "user_world": self.state_store.read_json("user_world.json"),
                "local_world": self._compact_local_world(self.state_store.read_json("local_world.json")),
                "external_world": self._compact_external_world(self.state_store.read_json("external_world.json")),
                "memory_summary": memory_summary,
                "active_commitments": self._commitments("active"),
                "paused_commitments": self._commitments("paused"),
            },
            max_string=1600,
            max_list=12,
        )

    def _commitments(self, status: str) -> list[dict[str, Any]]:
        state = self.state_store.read_json("user_commitments.json")
        items = state.get("commitments") if isinstance(state.get("commitments"), list) else []
        return [
            {
                "commitment_id": item.get("commitment_id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "title": item.get("title"),
                "topic": (item.get("payload") if isinstance(item.get("payload"), dict) else {}).get("topic"),
                "channel": item.get("channel"),
                "session_id": item.get("session_id"),
            }
            for item in items
            if isinstance(item, dict) and item.get("status") == status
        ][-20:]

    def _compact_local_world(self, local_world: dict[str, Any]) -> dict[str, Any]:
        probes = local_world.get("probes") if isinstance(local_world.get("probes"), dict) else {}
        return {
            "current_project": local_world.get("current_project"),
            "last_probe_at": local_world.get("last_probe_at"),
            "probe_names": list(probes.keys())[:20],
        }

    def _compact_external_world(self, external_world: dict[str, Any]) -> dict[str, Any]:
        watchlist = external_world.get("watchlist") if isinstance(external_world.get("watchlist"), list) else []
        return {
            "watchlist": [
                {"target": item.get("target"), "kind": item.get("kind"), "topic": item.get("topic"), "enabled": item.get("enabled", True)}
                for item in watchlist[-20:]
                if isinstance(item, dict)
            ],
            "knowledge_count": len(external_world.get("knowledge_items") if isinstance(external_world.get("knowledge_items"), list) else []),
        }

    def _trace(self, intent: ProactiveIntent, planner_result: dict[str, Any]) -> None:
        self.state_store.append_jsonl(
            "decision_trace.jsonl",
            {
                "route": "proactive_intent_planner",
                "status": planner_result.get("status"),
                "artifacts": {"intent": intent.to_dict(), "planner": planner_result},
            },
        )

    def _control_next_action(self, intent_type: str) -> str:
        return {
            "cancel_commitment": "cancel_matching_commitments",
            "pause_commitment": "pause_matching_commitments",
            "resume_commitment": "resume_matching_commitments",
        }.get(intent_type, "ask_confirmation")

    def _has_cancel(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if any(marker in compact for marker in ("不要取消", "不用取消", "别取消", "先别取消", "不要停止", "不用停止", "别停止")):
            return False
        return any(marker in lowered for marker in ("stop", "cancel", "不要再", "不用再", "别再")) or any(
            marker in text for marker in ("停止", "停掉", "取消", "以后都停止", "取消所有", "停止所有")
        )

    def _has_pause(self, text: str, lowered: str) -> bool:
        return "pause" in lowered or any(marker in text for marker in ("暂停", "先暂停", "最近先暂停", "暂时停", "先停一下"))

    def _has_resume(self, text: str, lowered: str) -> bool:
        return "resume" in lowered or any(marker in text for marker in ("恢复", "继续给我推", "继续推", "重新开启", "恢复推送"))

    def _control_entities(self, text: str) -> dict[str, Any]:
        return {"scope": "all" if any(marker in text for marker in ("所有", "全部", "以后都")) else "matching"}

    def _is_learning(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in text for marker in ("学习", "我想学", "我要学", "开始学", "入门", "课程", "考研", "背单词")) or any(
            marker in lowered for marker in ("learn", "study")
        )

    def _is_weather(self, text: str) -> bool:
        return "天气" in text or "weather" in text.lower()

    def _is_reminder(self, text: str) -> bool:
        return any(marker in text for marker in ("提醒我", "提醒", "记得", "背单词")) or "remind" in text.lower()

    def _is_state_inventory_question(self, text: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "").lower()
        if any(marker in compact for marker in ("提醒我", "帮我提醒", "请提醒", "设置提醒", "创建提醒", "remindme")):
            return False
        questionish = any(
            marker in compact
            for marker in (
                "有哪些",
                "有什么",
                "有没有",
                "还在吗",
                "状态",
                "现在有",
                "当前有",
                "whattasks",
                "whichtasks",
                "pendingreminders",
            )
        )
        state_subject = any(
            marker in compact
            for marker in ("任务", "提醒", "推送", "订阅", "关注", "追踪", "commitment", "tracking")
        )
        return questionish and state_subject

    def _is_local_monitor(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in text for marker in ("服务器状态", "服务是不是还在跑", "服务是否还在运行", "服务还在运行", "服务状态", "端口", "本地服务")) or any(
            marker in lowered for marker in ("server status", "localhost", "openclaw gateway", "veyra service")
        )

    def _is_external_tracking(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in text for marker in ("关注", "跟踪", "留意", "持续关注", "订阅")) or any(
            marker in lowered for marker in ("watch", "track", "monitor", "subscribe")
        )

    def _topic(self, text: str) -> str:
        known = [
            "PyTorch 3.0",
            "PyTorch",
            "GSoC Kotlin",
            "Kotlin",
            "深度学习",
            "考研",
            "湾区短租房源",
            "短租房源",
            "天气",
            "Veyra 服务",
            "OpenClaw gateway",
            "GitHub repo release",
        ]
        lowered = text.lower()
        for item in known:
            if item.lower() in lowered or item in text:
                return item
        patterns = [
            r"(?:关注一下|关注|跟踪|留意|取消|暂停|恢复|推送|学习|学|提醒我)\s*([A-Za-z0-9_.:/-]{2,80}(?:\s+[A-Za-z0-9_.:/-]{2,80}){0,4})",
            r"(?:关注一下|关注|跟踪|留意|取消|暂停|恢复|推送|学习|学|提醒我)([^，,。！？!?]{2,40})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                topic = self._clean_topic(match.group(1))
                if topic:
                    return topic
        return ""

    def _clean_topic(self, value: str) -> str:
        topic = (value or "").strip(" 的了吧吗呢啊？?！!，,。")
        for prefix in ("一下", "这个", "某个"):
            if topic.startswith(prefix):
                topic = topic[len(prefix) :]
        for suffix in ("最新消息", "重要更新", "新版本", "资料推送", "推送", "提醒", "内容", "了"):
            if topic.endswith(suffix) and len(topic) > len(suffix):
                topic = topic[: -len(suffix)]
        return topic.strip(" 的了吧吗呢啊？?！!，,。")[:180]

    def _reminder_topic(self, text: str) -> str:
        prefix_match = re.search(r"(?:以后|今后|下次)?\s*([^，,。！？!?]{2,60}?)(?:更新(?:视频)?|发布(?:新视频|新内容)?|有新(?:视频|内容)?)?提醒我", text or "")
        if prefix_match:
            topic = self._clean_topic(prefix_match.group(1))
            if topic:
                return topic
        match = re.search(r"提醒我([^，,。！？!?]{2,60})", text or "")
        return self._clean_topic(match.group(1)) if match else self._topic(text) or "提醒"

    def _location(self, text: str) -> str:
        match = re.search(r"([^\s，,。.?！!]{2,20}?)(?:今天|现在|当前|明天|今日)?(?:的)?(?:天气|气温)", text or "")
        return self._clean_topic(match.group(1)) if match else ""

    def _cadence(self, text: str) -> dict[str, Any]:
        lowered = text.lower()
        has_marker = (
            any(marker in text for marker in ("每天", "每日", "明天", "早上", "早晨", "上午", "下午", "晚上", "中午"))
            or any(marker in lowered for marker in ("daily", "every morning", "each day", "tomorrow", "morning", "afternoon", "evening", "tonight"))
            or has_explicit_schedule_time(text)
        )
        if not has_marker:
            return {}
        return parse_schedule_text(text, default_kind="daily", default_time="08:00")

    def _external_sources(self, text: str) -> list[str]:
        lowered = text.lower()
        sources: list[str] = []
        if "github" in lowered or "release" in lowered:
            sources.append("github")
        if any(marker in text for marker in ("论文", "paper")):
            sources.append("papers")
        if any(marker in text for marker in ("房源", "短租", "租房")):
            sources.append("housing_listing")
        sources.append("public_web")
        return list(dict.fromkeys(sources))[:6]
