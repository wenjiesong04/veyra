from __future__ import annotations

import hashlib
from typing import Any

from core.proactive_intent import CommitmentDraft, GoalDraft, ProactiveIntent, WatchlistDraft
from interface.event_schema import VeyraEvent, utc_now_iso


class ProactiveIntentTemplate:
    name = "base"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return False

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        return None


class CancelPauseTemplate(ProactiveIntentTemplate):
    name = "cancel_pause_resume"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type in {"cancel_commitment", "pause_commitment", "resume_commitment"}

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        action = {
            "cancel_commitment": "cancel",
            "pause_commitment": "pause",
            "resume_commitment": "resume",
        }.get(intent.intent_type, "")
        return core.apply_control_intent(intent, action=action)


class LearningPlanTemplate(ProactiveIntentTemplate):
    name = "learning_plan"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type in {"learning_plan", "goal_start"} and bool(intent.topic)

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        result = core._maybe_record_learning_goal(user_text=user_text, event=event)
        if not result:
            return None
        goal = result.get("goal") if isinstance(result.get("goal"), dict) else {}
        commitment = result.get("commitment") if isinstance(result.get("commitment"), dict) else None
        watchlist = core.record_watchlist_draft(
            WatchlistDraft(
                topic=str(intent.topic or goal.get("topic") or "学习计划"),
                query=f"{intent.topic or goal.get('topic') or '学习计划'} latest tutorial course paper",
                sources=["public_web", "courses", "papers"],
                refresh_policy={"kind": "on_authorized_refresh", "ttl_seconds": 1800},
                ranking_policy={"prefer": ["trusted_sources", "freshness", "topic_match"]},
                dedupe_policy={"key": "url"},
                ttl=1800,
                status="pending_confirmation",
                source_intent_id=intent.intent_id,
            ),
            commitment=commitment,
            goal=goal,
        )
        result["intent"] = intent.to_dict()
        result["goal_draft"] = GoalDraft(
            goal_id=str(goal.get("goal_id") or ""),
            title=str(goal.get("title") or f"学习目标：{intent.topic}"),
            description=str(intent.desired_outcome or "启动学习计划并按用户授权持续辅助"),
            category="learning",
            current_stage=str((goal.get("plan") if isinstance(goal.get("plan"), dict) else {}).get("phase") or "getting_started"),
            success_criteria=(goal.get("plan") if isinstance(goal.get("plan"), dict) else {}).get("checkpoints") or [],
            required_context=["user baseline", "learning preferences"],
            proactive_allowed=str((goal.get("permissions") if isinstance(goal.get("permissions"), dict) else {}).get("proactive_push") or "pending_confirmation"),
            created_from_intent_id=intent.intent_id,
            status="active" if goal else "draft",
        ).to_dict()
        result["watchlist_draft"] = watchlist
        result["template"] = self.name
        return result


class WeatherDigestTemplate(ProactiveIntentTemplate):
    name = "weather_digest"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type == "daily_digest" and intent.topic == "weather"

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        offer = core._maybe_offer_subscription(user_text=user_text, assistant_response=assistant_response, route=route, event=event)
        if not offer:
            return None
        return {
            "status": "offered",
            "commitment": offer,
            "commitment_draft": CommitmentDraft(
                type="weather_daily",
                topic="weather",
                cadence=offer.get("schedule") if isinstance(offer.get("schedule"), dict) else intent.cadence,
                delivery_channel=event.source.channel,
                source_intent_id=intent.intent_id,
                payload=offer.get("payload") if isinstance(offer.get("payload"), dict) else {},
            ).to_dict(),
            "intent": intent.to_dict(),
            "followup_offer": core._confirmation_prompt(offer),
            "actions": ["offered_subscription"],
            "template": self.name,
        }


class ExternalTrackingTemplate(ProactiveIntentTemplate):
    name = "external_tracking"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type == "track_external_topic"

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        topic = intent.topic or "待确认主题"
        watchlist = core.record_watchlist_draft(
            WatchlistDraft(
                topic=topic,
                query=core.external_tracking_query(intent),
                sources=intent.information_sources or ["public_web"],
                refresh_policy={"kind": "authorized_external_refresh", "ttl_seconds": 1800},
                ranking_policy={"prefer": ["trusted_sources", "freshness", "topic_match"]},
                dedupe_policy={"key": "url"},
                ttl=1800,
                status="pending_confirmation",
                source_intent_id=intent.intent_id,
            )
        )
        commitment = core.create_commitment(
            {
                "kind": "external_digest",
                "status": "pending_confirmation",
                "title": f"外部追踪：{topic}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": intent.cadence or {"kind": "interval", "interval_seconds": 86400, "timezone": "Asia/Shanghai"},
                "payload": {"topic": topic, "watchlist_id": watchlist.get("watchlist_id"), "query": watchlist.get("query")},
            }
        )
        return {
            "status": "draft_created",
            "intent": intent.to_dict(),
            "watchlist_draft": watchlist,
            "commitment": commitment,
            "commitment_draft": CommitmentDraft(
                type="external_digest",
                topic=topic,
                cadence=commitment.get("schedule") if isinstance(commitment.get("schedule"), dict) else intent.cadence,
                delivery_channel=event.source.channel,
                source_intent_id=intent.intent_id,
                payload=commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {},
            ).to_dict(),
            "response_override": f"我已记录「{topic}」的外部追踪草案。回复「好的」后，我会按授权刷新外部信息并在有价值更新时推送。",
            "followup_offer": f"我已记录「{topic}」的外部追踪草案。回复「好的」后，我会按授权刷新外部信息并在有价值更新时推送。",
            "actions": ["created_watchlist_draft", "created_commitment_draft"],
            "template": self.name,
        }


class LocalMonitorTemplate(ProactiveIntentTemplate):
    name = "local_monitor"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type == "monitor_local_state"

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        topic = intent.topic or "本地状态"
        commitment = core.create_commitment(
            {
                "kind": "local_probe_monitor",
                "status": "pending_confirmation",
                "title": f"本地监控：{topic}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": intent.cadence or {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai"},
                "payload": {"topic": topic, "probe_family": (intent.entities or {}).get("probe_family") or "runtime_status"},
            }
        )
        return {
            "status": "draft_created",
            "intent": intent.to_dict(),
            "commitment": commitment,
            "commitment_draft": CommitmentDraft(
                type="local_probe_monitor",
                topic=topic,
                cadence=commitment.get("schedule") if isinstance(commitment.get("schedule"), dict) else intent.cadence,
                delivery_channel=event.source.channel,
                source_intent_id=intent.intent_id,
                payload=commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {},
            ).to_dict(),
            "response_override": f"我可以用安全 probe 每天检查「{topic}」，不会执行危险命令。回复「好的」后开启。",
            "followup_offer": f"我可以用安全 probe 每天检查「{topic}」，不会执行危险命令。回复「好的」后开启。",
            "actions": ["created_local_monitor_draft"],
            "template": self.name,
        }


class ReminderTemplate(ProactiveIntentTemplate):
    name = "reminder"

    def can_handle(self, intent: ProactiveIntent) -> bool:
        return intent.intent_type == "reminder"

    def handle(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        topic = intent.topic or "提醒"
        commitment = core.create_commitment(
            {
                "kind": "generic_reminder",
                "status": "active",
                "title": f"提醒：{topic}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": intent.cadence or {"kind": "daily", "time_local": "09:00", "timezone": "Asia/Shanghai"},
                "payload": {"note": topic, "source_intent_id": intent.intent_id},
            }
        )
        return {
            "status": "created",
            "intent": intent.to_dict(),
            "commitment": commitment,
            "commitment_draft": CommitmentDraft(
                type="generic_reminder",
                topic=topic,
                cadence=commitment.get("schedule") if isinstance(commitment.get("schedule"), dict) else intent.cadence,
                delivery_channel=event.source.channel,
                requires_confirmation=False,
                status="active",
                source_intent_id=intent.intent_id,
                payload=commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {},
            ).to_dict(),
            "response_override": f"已记录提醒：{topic}。",
            "actions": ["created_reminder"],
            "template": self.name,
        }


class ProactiveTemplateRegistry:
    def __init__(self) -> None:
        self.templates: list[ProactiveIntentTemplate] = [
            CancelPauseTemplate(),
            LearningPlanTemplate(),
            WeatherDigestTemplate(),
            ExternalTrackingTemplate(),
            LocalMonitorTemplate(),
            ReminderTemplate(),
        ]

    def apply(self, *, intent: ProactiveIntent, core: Any, event: VeyraEvent, user_text: str, assistant_response: str, route: str) -> dict[str, Any] | None:
        for template in self.templates:
            if not template.can_handle(intent):
                continue
            return template.handle(intent=intent, core=core, event=event, user_text=user_text, assistant_response=assistant_response, route=route)
        return None


def watchlist_id_for(topic: str, query: str) -> str:
    digest = hashlib.sha256(f"{topic}:{query}".encode("utf-8")).hexdigest()[:12]
    return f"watch_{digest}"
