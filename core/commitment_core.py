from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


AFFIRM_MARKERS = ("好的", "可以", "行", "同意", "要", "需要", "没问题", "ok", "yes", "sure", "开启", "启用")
DECLINE_MARKERS = ("不用", "不要", "取消", "算了", "不需要", "no", "decline", "stop")
DAILY_MARKERS = ("每天", "每日", "每天早上", "每天早晨", "定时", "定期", "daily", "every morning", "each day")
WEATHER_MARKERS = ("天气", "气温", "weather", "下雨", "晴天")
LEARNING_MARKERS = ("学习", "深度学习", "入门", "教程", "课程", "复习", "learn", "study")
PUSH_MARKERS = ("推送", "提醒", "通知", "告诉我", "发给我", "push", "notify", "remind")


class CommitmentCore:
    """User commitments: goals, schedules, and proactive push contracts."""

    STATE_FILE = "user_commitments.json"
    MAX_COMMITMENTS = 200
    MAX_HISTORY = 50

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def list_commitments(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self._read_state().get("commitments", [])
        if not isinstance(items, list):
            return []
        filtered = [item for item in items if isinstance(item, dict)]
        if user_id:
            filtered = [item for item in filtered if item.get("user_id") == user_id]
        if session_id:
            filtered = [item for item in filtered if item.get("session_id") == session_id]
        if status:
            filtered = [item for item in filtered if item.get("status") == status]
        return filtered

    def get_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        for item in self.list_commitments():
            if item.get("commitment_id") == commitment_id:
                return item
        return None

    def create_commitment(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "generic_reminder").strip()
        schedule = self._normalize_schedule(payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {})
        now = utc_now_iso()
        item = {
            "commitment_id": f"cmt_{uuid4().hex[:12]}",
            "kind": kind,
            "status": str(payload.get("status") or "pending_confirmation"),
            "title": str(payload.get("title") or self._default_title(kind, payload)),
            "user_id": str(payload.get("user_id") or "local-user"),
            "channel": str(payload.get("channel") or "api"),
            "session_id": str(payload.get("session_id") or "local-session"),
            "risk_level": str(payload.get("risk_level") or RiskLevel.R0.value),
            "schedule": schedule,
            "payload": payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            "source_event_id": payload.get("source_event_id"),
            "created_at": now,
            "updated_at": now,
            "confirmed_at": payload.get("confirmed_at"),
            "next_run_at": payload.get("next_run_at") or self._compute_next_run_at(schedule),
            "last_run_at": None,
            "run_count": 0,
            "push_history": [],
        }
        if item["status"] == "active" and not item["confirmed_at"]:
            item["confirmed_at"] = now
        state = self._read_state()
        commitments = state.setdefault("commitments", [])
        if not isinstance(commitments, list):
            commitments = []
        commitments.append(item)
        state["commitments"] = commitments[-self.MAX_COMMITMENTS :]
        state["updated_at"] = now
        self._write_state(state)
        self._sync_user_world(item)
        self._sync_agency_goal(item)
        return item

    def confirm_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        return self._patch_commitment(
            commitment_id,
            {
                "status": "active",
                "confirmed_at": utc_now_iso(),
                "next_run_at": None,
            },
            recompute_next_run=True,
        )

    def pause_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        return self._patch_commitment(commitment_id, {"status": "paused"})

    def cancel_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        return self._patch_commitment(commitment_id, {"status": "cancelled"})

    def due_commitments(self, *, limit: int = 20, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for item in self.list_commitments(status="active"):
            next_run = self._parse_time(item.get("next_run_at"))
            if next_run is None or next_run <= now:
                due.append(item)
            if len(due) >= limit:
                break
        return due

    def record_push(self, commitment_id: str, *, push_result: dict[str, Any], message: str) -> dict[str, Any] | None:
        item = self.get_commitment(commitment_id)
        if not item:
            return None
        history = item.get("push_history") if isinstance(item.get("push_history"), list) else []
        history.append(
            {
                "pushed_at": utc_now_iso(),
                "status": push_result.get("status"),
                "delivery_status": push_result.get("delivery_status") or push_result.get("status"),
                "message_preview": message[:240],
            }
        )
        schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
        return self._patch_commitment(
            commitment_id,
            {
                "last_run_at": utc_now_iso(),
                "run_count": int(item.get("run_count") or 0) + 1,
                "push_history": history[-self.MAX_HISTORY :],
                "next_run_at": self._compute_next_run_at(schedule),
            },
        )

    def process_turn(
        self,
        *,
        event: VeyraEvent,
        user_text: str,
        assistant_response: str,
        route: str,
        status: str,
    ) -> dict[str, Any]:
        """Extract or confirm commitments from a conversation turn."""
        if status in {"blocked", "duplicate"}:
            return {"status": "skipped", "reason": status}

        lowered = (user_text or "").lower()
        result: dict[str, Any] = {"status": "idle", "actions": []}

        pending = self._pending_for_session(event.source.session_id)
        if pending and self._is_affirmation(lowered):
            confirmed = self.confirm_commitment(str(pending.get("commitment_id")))
            if confirmed:
                result = {"status": "confirmed", "commitment": confirmed, "actions": ["confirmed_pending"]}
                return result
        if pending and self._is_decline(lowered):
            cancelled = self.cancel_commitment(str(pending.get("commitment_id")))
            if cancelled:
                return {"status": "declined", "commitment": cancelled, "actions": ["cancelled_pending"]}

        extracted = self._extract_from_user_text(user_text, event=event)
        if extracted:
            created = self.create_commitment(extracted)
            result = {"status": "created", "commitment": created, "actions": ["created_from_user"]}
            if created.get("status") == "pending_confirmation":
                result["followup_offer"] = self._confirmation_prompt(created)
            return result

        offer = self._maybe_offer_subscription(
            user_text=user_text,
            assistant_response=assistant_response,
            route=route,
            event=event,
        )
        if offer:
            result = {"status": "offered", "commitment": offer, "followup_offer": self._confirmation_prompt(offer), "actions": ["offered_subscription"]}
        return result

    def append_followup_to_response(self, response: str, turn_result: dict[str, Any]) -> str:
        followup = str(turn_result.get("followup_offer") or "").strip()
        if not followup:
            return response
        if followup in response:
            return response
        return f"{response.rstrip()}\n\n{followup}"

    def _maybe_offer_subscription(
        self,
        *,
        user_text: str,
        assistant_response: str,
        route: str,
        event: VeyraEvent,
    ) -> dict[str, Any] | None:
        lowered = (user_text or "").lower()
        if not any(marker in lowered for marker in WEATHER_MARKERS):
            return None
        if not self._mentions_push_intent(lowered) and route not in {"probe", "direct_answer"}:
            return None
        existing = [
            item
            for item in self.list_commitments(user_id=event.source.user_id, session_id=event.source.session_id)
            if item.get("kind") == "weather_daily" and item.get("status") in {"active", "pending_confirmation"}
        ]
        if existing:
            return None
        location = self._extract_location(user_text) or str(
            self.state_store.read_json("user_world.json").get("preferences", {}).get("default_location") or ""
        )
        schedule = self._default_daily_schedule(user_text)
        return self.create_commitment(
            {
                "kind": "weather_daily",
                "status": "pending_confirmation",
                "title": f"每日天气推送（{location or '待确认地点'}）",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": schedule,
                "payload": {"location": location, "topic": "weather"},
            }
        )

    def _extract_from_user_text(self, text: str, *, event: VeyraEvent) -> dict[str, Any] | None:
        lowered = (text or "").lower()
        if not self._mentions_push_intent(lowered) and not any(marker in lowered for marker in LEARNING_MARKERS):
            return None

        if any(marker in lowered for marker in WEATHER_MARKERS) and any(marker in lowered for marker in DAILY_MARKERS + PUSH_MARKERS):
            location = self._extract_location(text)
            return {
                "kind": "weather_daily",
                "status": "active" if self._is_affirmation(lowered) else "pending_confirmation",
                "title": f"每日天气（{location or '默认地点'}）",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_daily_schedule(text),
                "payload": {"location": location, "topic": "weather"},
            }

        if any(marker in lowered for marker in LEARNING_MARKERS):
            topic = self._extract_learning_topic(text)
            return {
                "kind": "learning_digest",
                "status": "active" if self._is_affirmation(lowered) else "pending_confirmation",
                "title": f"学习辅导：{topic}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_learning_schedule(text),
                "payload": {"topic": topic, "phase": "getting_started"},
            }

        if any(marker in lowered for marker in DAILY_MARKERS + PUSH_MARKERS):
            return {
                "kind": "generic_reminder",
                "status": "pending_confirmation",
                "title": "定时提醒",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_daily_schedule(text),
                "payload": {"note": text[:500]},
            }
        return None

    def _pending_for_session(self, session_id: str) -> dict[str, Any] | None:
        pending = self.list_commitments(session_id=session_id, status="pending_confirmation")
        return pending[-1] if pending else None

    def _patch_commitment(self, commitment_id: str, patch: dict[str, Any], *, recompute_next_run: bool = False) -> dict[str, Any] | None:
        state = self._read_state()
        commitments = state.get("commitments") if isinstance(state.get("commitments"), list) else []
        updated = None
        for index, item in enumerate(commitments):
            if not isinstance(item, dict) or item.get("commitment_id") != commitment_id:
                continue
            item.update(patch)
            item["updated_at"] = utc_now_iso()
            if recompute_next_run or patch.get("next_run_at") is None:
                schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
                item["next_run_at"] = self._compute_next_run_at(schedule)
            commitments[index] = item
            updated = item
            break
        if updated is None:
            return None
        state["commitments"] = commitments
        state["updated_at"] = utc_now_iso()
        self._write_state(state)
        self._sync_user_world(updated)
        return updated

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json(self.STATE_FILE) or {"commitments": [], "updated_at": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json(self.STATE_FILE, state)

    def _sync_user_world(self, commitment: dict[str, Any]) -> None:
        if commitment.get("status") not in {"active", "pending_confirmation"}:
            return
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        topic = str(payload.get("topic") or commitment.get("title") or "")
        goal = f"active_commitment:{commitment.get('kind')}:{topic}"[:180]
        user_world = self.state_store.read_json("user_world.json")
        user_world["current_goal"] = goal
        user_world["commitment_id"] = commitment.get("commitment_id")
        user_world["commitment_kind"] = commitment.get("kind")
        preferences = user_world.setdefault("preferences", {})
        if isinstance(preferences, dict) and payload.get("location"):
            preferences["default_location"] = payload.get("location")
        user_world["updated_at"] = utc_now_iso()
        self.state_store.write_json("user_world.json", user_world)

    def _sync_agency_goal(self, commitment: dict[str, Any]) -> None:
        if commitment.get("status") != "active":
            return
        agency_root = Path(os.getenv("VEYRA_AGENCY_ROOT", "agency"))
        goals_path = agency_root / "goals.json"
        if not goals_path.parent.exists():
            return
        try:
            goals = json.loads(goals_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            goals = {}
        active = goals.setdefault("user_commitments", [])
        if not isinstance(active, list):
            active = []
        active.append(
            {
                "commitment_id": commitment.get("commitment_id"),
                "kind": commitment.get("kind"),
                "title": commitment.get("title"),
                "updated_at": utc_now_iso(),
            }
        )
        goals["user_commitments"] = active[-20:]
        try:
            goals_path.write_text(json.dumps(goals, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return

    def _normalize_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        kind = str(schedule.get("kind") or "daily")
        timezone_name = str(schedule.get("timezone") or "Asia/Shanghai")
        return {
            "kind": kind,
            "time_local": str(schedule.get("time_local") or "08:00"),
            "timezone": timezone_name,
            "interval_seconds": max(3600.0, float(schedule.get("interval_seconds") or 86400.0)),
        }

    def _default_daily_schedule(self, text: str) -> dict[str, Any]:
        match = re.search(r"(\d{1,2})[:：](\d{2})", text or "")
        time_local = f"{int(match.group(1)):02d}:{match.group(2)}" if match else "08:00"
        if "早上" in (text or "") or "早晨" in (text or "") or "morning" in (text or "").lower():
            time_local = "08:00" if time_local == "08:00" and not match else time_local
        return self._normalize_schedule({"kind": "daily", "time_local": time_local, "timezone": "Asia/Shanghai"})

    def _default_learning_schedule(self, text: str) -> dict[str, Any]:
        if any(marker in (text or "").lower() for marker in ("每天", "每日", "daily")):
            return self._normalize_schedule({"kind": "daily", "time_local": "09:00", "timezone": "Asia/Shanghai"})
        return self._normalize_schedule({"kind": "interval", "interval_seconds": 86400, "timezone": "Asia/Shanghai"})

    def _compute_next_run_at(self, schedule: dict[str, Any]) -> str:
        kind = str(schedule.get("kind") or "daily")
        now = datetime.now(timezone.utc)
        if kind == "interval":
            interval = max(3600.0, float(schedule.get("interval_seconds") or 86400.0))
            return (now + timedelta(seconds=interval)).isoformat()
        tz_name = str(schedule.get("timezone") or "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        local_now = now.astimezone(tz)
        time_local = str(schedule.get("time_local") or "08:00")
        try:
            hour, minute = [int(part) for part in time_local.split(":", 1)]
        except (ValueError, IndexError):
            hour, minute = 8, 0
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat()

    def _parse_time(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _confirmation_prompt(self, commitment: dict[str, Any]) -> str:
        kind = commitment.get("kind")
        schedule = commitment.get("schedule") if isinstance(commitment.get("schedule"), dict) else {}
        time_local = schedule.get("time_local", "08:00")
        if kind == "weather_daily":
            location = (commitment.get("payload") or {}).get("location") or "你的地点"
            return f"我可以每天在 {time_local}（{schedule.get('timezone', 'Asia/Shanghai')}）向你推送 {location} 的天气。回复「好的」或「同意」即可开启；回复「不用」则取消。"
        if kind == "learning_digest":
            topic = (commitment.get("payload") or {}).get("topic") or "该主题"
            return f"我可以按你的学习进度定期整理 {topic} 的资料摘要并推送。回复「好的」确认开启；也可直接说明希望的推送频率。"
        return f"是否开启定时提醒（约 {time_local}）？回复「好的」确认，「不用」取消。"

    def _default_title(self, kind: str, payload: dict[str, Any]) -> str:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        if kind == "weather_daily":
            return f"天气推送：{inner.get('location') or '默认地点'}"
        if kind == "learning_digest":
            return f"学习辅导：{inner.get('topic') or '学习计划'}"
        return "用户承诺提醒"

    def _extract_location(self, text: str) -> str:
        patterns = [
            r"(?:在|于)\s*([^\s，,。.?！!]{2,16}?)(?:的)?(?:天气|气温)",
            r"(?:今天|现在|当前)?\s*([^\s，,。.?！!]{2,16}?)(?:的)?天气",
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                location = match.group(1).strip(" 的？?！!，,")
                for prefix in ("今天", "现在", "当前"):
                    if location.startswith(prefix) and len(location) > len(prefix):
                        location = location[len(prefix) :]
                return location
        for token in ("北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"):
            if token in (text or ""):
                return token
        return ""

    def _extract_learning_topic(self, text: str) -> str:
        match = re.search(r"(?:学习|学|入门|掌握)\s*([^\s，,。.?！!]{2,32})", text or "")
        if match:
            return match.group(1).strip()
        if "深度学习" in (text or ""):
            return "深度学习"
        return "学习计划"

    def _mentions_push_intent(self, lowered: str) -> bool:
        return any(marker in lowered for marker in PUSH_MARKERS + DAILY_MARKERS)

    def _is_affirmation(self, lowered: str) -> bool:
        return any(marker in lowered for marker in AFFIRM_MARKERS)

    def _is_decline(self, lowered: str) -> bool:
        return any(marker in lowered for marker in DECLINE_MARKERS)
