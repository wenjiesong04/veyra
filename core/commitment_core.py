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
from core.proactive_authorization import AuthorizationPolicy
from core.proactive_intent import ProactiveIntent, WatchlistDraft
from core.proactive_intent_planner import ProactiveIntentPlanner
from core.proactive_templates import ProactiveTemplateRegistry, watchlist_id_for
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso
from memory_bridge.local_memory_bridge import LocalMemoryBridge


AFFIRM_EXACT_MARKERS = ("好的", "好", "可以", "行", "同意", "确认", "没问题", "ok", "yes", "sure", "开启", "启用")
AFFIRM_MARKERS = ("好的", "可以", "行", "同意", "确认", "没问题", "ok", "yes", "sure", "开启", "启用")
AFFIRM_PHRASES = ("同意开启", "确认开启", "帮我开启", "可以开启", "开始推送", "开启推送", "订阅", "subscribe")
DECLINE_MARKERS = ("不用", "不要", "取消", "算了", "不需要", "no", "decline", "stop")
DAILY_MARKERS = ("每天", "每日", "每天早上", "每天早晨", "定时", "定期", "daily", "every morning", "each day")
WEATHER_MARKERS = ("天气", "气温", "weather", "下雨", "晴天")
LEARNING_MARKERS = ("学习", "深度学习", "机器学习", "入门", "教程", "课程", "复习", "learn", "study")
PUSH_MARKERS = ("推送", "提醒", "通知", "告诉我", "发给我", "push", "notify", "remind")


class CommitmentCore:
    """User commitments: goals, schedules, and proactive push contracts."""

    STATE_FILE = "user_commitments.json"
    GOAL_STATE_FILE = "user_goals.json"
    MAX_COMMITMENTS = 200
    MAX_GOALS = 100
    MAX_HISTORY = 50
    PUSH_COOLDOWN_SECONDS = 60

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.intent_planner = ProactiveIntentPlanner(state_store)
        self.template_registry = ProactiveTemplateRegistry()
        self.authorization = AuthorizationPolicy(state_store)
        self.memory_bridge = LocalMemoryBridge(state_store)

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
        self._sync_goal_permission_from_commitment(item)
        self.set_watchlists_for_commitment(item)
        self.authorization.record_commitment(item)
        return item

    def confirm_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        return self._patch_commitment(
            commitment_id,
            {
                "status": "active",
                "confirmed_at": utc_now_iso(),
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
            pushable, _reason = self.pushable_reason(item, now=now)
            if not pushable:
                continue
            next_run = self._parse_time(item.get("next_run_at"))
            if next_run is None or next_run <= now:
                due.append(item)
            if len(due) >= limit:
                break
        return due

    def pushable_reason(self, commitment: dict[str, Any], *, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        status = str(commitment.get("status") or "")
        if status != "active":
            return False, f"status:{status}"
        if not commitment.get("confirmed_at"):
            return False, "not_confirmed"
        for field in ("last_run_at", "last_attempt_at"):
            last = self._parse_time(commitment.get(field))
            if last is not None and (now - last).total_seconds() < self.PUSH_COOLDOWN_SECONDS:
                return False, "push_cooldown"
        return True, "ok"

    def touch_attempt(self, commitment_id: str) -> None:
        """Record a push attempt without advancing schedule (cooldown / audit)."""
        self._patch_commitment(commitment_id, {"last_attempt_at": utc_now_iso()})

    def record_push(
        self,
        commitment_id: str,
        *,
        push_result: dict[str, Any],
        message: str,
        advance_schedule: bool = True,
        count_run: bool = True,
    ) -> dict[str, Any] | None:
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
        patch: dict[str, Any] = {"push_history": history[-self.MAX_HISTORY :]}
        if count_run:
            patch["last_run_at"] = utc_now_iso()
            patch["run_count"] = int(item.get("run_count") or 0) + 1
        if advance_schedule:
            patch["next_run_at"] = self._compute_next_run_at(schedule)
        return self._patch_commitment(commitment_id, patch)

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
        intent = self.intent_planner.plan(user_text=user_text, event=event)
        intent_record = self.intent_planner.record_intent(intent)

        if intent.intent_type in {"cancel_commitment", "pause_commitment", "resume_commitment"}:
            control = self.template_registry.apply(
                intent=intent,
                core=self,
                event=event,
                user_text=user_text,
                assistant_response=assistant_response,
                route=route,
            )
            if control:
                control["intent"] = intent_record
                return control

        pending = self._pending_for_session(event.source.session_id)
        if pending and self._is_affirmation(lowered):
            confirmed = self.confirm_commitment(str(pending.get("commitment_id")))
            if confirmed:
                result = {
                    "status": "confirmed",
                    "commitment": confirmed,
                    "actions": ["confirmed_pending"],
                    "response_override": f"已开启：{confirmed.get('title') or confirmed.get('kind')}",
                }
                return result
        if pending and self._is_decline(lowered):
            cancelled = self.cancel_commitment(str(pending.get("commitment_id")))
            if cancelled:
                return {
                    "status": "declined",
                    "commitment": cancelled,
                    "actions": ["cancelled_pending"],
                    "response_override": f"已取消：{cancelled.get('title') or cancelled.get('kind')}",
                }

        templated = self.template_registry.apply(
            intent=intent,
            core=self,
            event=event,
            user_text=user_text,
            assistant_response=assistant_response,
            route=route,
        )
        if templated:
            templated["intent"] = intent_record
            return templated

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
        if result.get("status") != "idle":
            result["intent"] = intent_record
        return result

    def apply_control_intent(self, intent: ProactiveIntent, *, action: str) -> dict[str, Any]:
        statuses = {"cancel": ["active", "pending_confirmation"], "pause": ["active"], "resume": ["paused"]}.get(action, [])
        matches = self.match_commitments(intent, statuses=statuses)
        if not matches:
            return {
                "status": "not_found",
                "actions": [f"{action}_matching_commitments"],
                "response_override": "没有找到正在进行的相关推送。",
                "matched_commitments": [],
                "template": "cancel_pause_resume",
            }
        updated: list[dict[str, Any]] = []
        for item in matches:
            commitment_id = str(item.get("commitment_id") or "")
            if action == "cancel":
                changed = self.cancel_commitment(commitment_id)
            elif action == "pause":
                changed = self.pause_commitment(commitment_id)
            elif action == "resume":
                changed = self.confirm_commitment(commitment_id)
            else:
                changed = None
            if changed:
                updated.append(changed)
        self._audit_commitment_control(intent, action=action, matches=matches, updated=updated)
        action_word = {"cancel": "停止", "pause": "暂停", "resume": "恢复"}.get(action, action)
        status_word = {"cancel": "cancelled", "pause": "paused", "resume": "resumed"}.get(action, action)
        return {
            "status": status_word,
            "actions": [f"{action}_matching_commitments"],
            "matched_commitments": matches,
            "updated_commitments": updated,
            "response_override": f"已{action_word} {len(updated)} 个匹配的主动任务。",
            "template": "cancel_pause_resume",
        }

    def match_commitments(self, intent: ProactiveIntent, *, statuses: list[str]) -> list[dict[str, Any]]:
        topic = str(intent.topic or "").lower()
        scope = str((intent.entities or {}).get("scope") or "matching")
        items = [
            item
            for item in self.list_commitments(user_id=intent.user_id)
            if isinstance(item, dict) and item.get("status") in statuses
        ]
        if scope == "all" or not topic:
            return items
        matched: list[dict[str, Any]] = []
        for item in items:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            haystack = " ".join(
                str(part or "").lower()
                for part in (
                    item.get("kind"),
                    item.get("title"),
                    payload.get("topic"),
                    payload.get("location"),
                    payload.get("note"),
                    payload.get("query"),
                )
            )
            if topic in haystack or haystack in topic:
                matched.append(item)
        return matched

    def record_watchlist_draft(
        self,
        draft: WatchlistDraft,
        *,
        commitment: dict[str, Any] | None = None,
        goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        external = self.state_store.read_json("external_world.json")
        watchlist = external.setdefault("watchlist", [])
        if not isinstance(watchlist, list):
            watchlist = []
        payload = draft.to_dict()
        watchlist_id = watchlist_id_for(str(payload.get("topic") or ""), str(payload.get("query") or ""))
        item = {
            "watchlist_id": watchlist_id,
            "target": f"external:{watchlist_id}",
            "kind": "external_search",
            "enabled": payload.get("status") == "active",
            "status": payload.get("status") or "pending_confirmation",
            "topic": payload.get("topic"),
            "query": payload.get("query"),
            "sources": payload.get("sources") if isinstance(payload.get("sources"), list) else [],
            "refresh_policy": payload.get("refresh_policy") if isinstance(payload.get("refresh_policy"), dict) else {},
            "ranking_policy": payload.get("ranking_policy") if isinstance(payload.get("ranking_policy"), dict) else {},
            "dedupe_policy": payload.get("dedupe_policy") if isinstance(payload.get("dedupe_policy"), dict) else {},
            "ttl": payload.get("ttl"),
            "source_intent_id": payload.get("source_intent_id"),
            "commitment_id": (commitment or {}).get("commitment_id"),
            "goal_id": (goal or {}).get("goal_id"),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        existing = [entry for entry in watchlist if isinstance(entry, dict) and entry.get("watchlist_id") == watchlist_id]
        if existing:
            existing[-1].update({key: value for key, value in item.items() if value not in (None, "", [])})
            item = existing[-1]
        else:
            watchlist.append(item)
        external["watchlist"] = watchlist[-100:]
        self.state_store.write_json("external_world.json", external)
        return item

    def set_watchlists_for_commitment(self, commitment: dict[str, Any]) -> None:
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        watchlist_id = str(payload.get("watchlist_id") or "")
        if not watchlist_id:
            return
        external = self.state_store.read_json("external_world.json")
        watchlist = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        changed = False
        for item in watchlist:
            if not isinstance(item, dict) or item.get("watchlist_id") != watchlist_id:
                continue
            status = str(commitment.get("status") or "")
            item["commitment_id"] = commitment.get("commitment_id")
            if status == "active":
                item["status"] = "active"
                item["enabled"] = True
            elif status in {"cancelled", "paused"}:
                item["status"] = status
                item["enabled"] = False
            else:
                item["status"] = "pending_confirmation"
                item["enabled"] = False
            item["updated_at"] = utc_now_iso()
            changed = True
        if changed:
            external["watchlist"] = watchlist[-100:]
            self.state_store.write_json("external_world.json", external)

    def external_tracking_query(self, intent: ProactiveIntent) -> str:
        topic = str(intent.topic or "updates")
        sources = " ".join(intent.information_sources or [])
        if "github" in sources.lower() or "release" in (intent.raw_text or "").lower():
            return f"{topic} GitHub release latest updates"
        if any(marker in intent.raw_text for marker in ("论文", "paper")):
            return f"{topic} latest paper arxiv"
        if any(marker in intent.raw_text for marker in ("房源", "短租", "租房")):
            return f"{topic} latest listings"
        return f"{topic} latest important updates"

    def _audit_commitment_control(self, intent: ProactiveIntent, *, action: str, matches: list[dict[str, Any]], updated: list[dict[str, Any]]) -> None:
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "commitment_control",
                "status": action,
                "artifacts": {
                    "intent": intent.to_dict(),
                    "matched_count": len(matches),
                    "updated_count": len(updated),
                    "commitment_ids": [item.get("commitment_id") for item in updated],
                },
            },
        )
        if updated:
            self.memory_bridge.write_patch(
                {
                    "session_id": intent.session_id,
                    "memory_type": "proactive_authorization",
                    "topic": intent.topic or "proactive_commitments",
                    "summary": f"User requested {action} for {len(updated)} proactive commitment(s).",
                    "freshness": "fresh",
                    "trust": "user_explicit_request",
                    "confidence": 0.95,
                },
                provider="local",
            )

    def append_followup_to_response(self, response: str, turn_result: dict[str, Any]) -> str:
        override = str(turn_result.get("response_override") or "").strip()
        if override:
            return override
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

    def _maybe_record_learning_goal(self, *, user_text: str, event: VeyraEvent) -> dict[str, Any] | None:
        lowered = (user_text or "").lower()
        if not any(marker in lowered for marker in LEARNING_MARKERS):
            return None
        if not self._looks_like_learning_goal_request(user_text) and not self._mentions_push_intent(lowered):
            return None
        topic = self._extract_learning_topic(user_text)
        goal = self._upsert_user_goal(
            {
                "kind": "learning",
                "status": "active",
                "title": f"学习目标：{topic}",
                "topic": topic,
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "last_user_text": user_text[:500],
                "plan": self._learning_plan(topic),
                "permissions": {
                    "store_progress": "granted_by_request",
                    "external_search": "pending_confirmation",
                    "proactive_push": "pending_confirmation",
                },
            }
        )
        digest = self._ensure_learning_digest_offer(goal=goal, event=event, user_text=user_text)
        if digest:
            goal["commitment_id"] = digest.get("commitment_id")
            permissions = goal.setdefault("permissions", {})
            if isinstance(permissions, dict) and digest.get("status") == "active":
                permissions["external_search"] = "granted_for_digest"
                permissions["proactive_push"] = "granted"
            self._replace_user_goal(goal)
        self._sync_learning_goal_to_user_world(goal, digest)
        actions = ["recorded_learning_goal"]
        if digest:
            actions.append("offered_learning_digest" if digest.get("status") == "pending_confirmation" else "activated_learning_digest")
        return {
            "status": "goal_recorded",
            "goal": goal,
            "commitment": digest,
            "actions": actions,
            "response_override": self._learning_goal_response(goal, digest),
        }

    def _ensure_learning_digest_offer(self, *, goal: dict[str, Any], event: VeyraEvent, user_text: str) -> dict[str, Any] | None:
        topic = str(goal.get("topic") or "学习计划")
        existing = [
            item
            for item in self.list_commitments(user_id=event.source.user_id, session_id=event.source.session_id)
            if item.get("kind") == "learning_digest"
            and item.get("status") in {"active", "pending_confirmation"}
            and str((item.get("payload") or {}).get("topic") or "") == topic
        ]
        if existing:
            return existing[-1]
        status = "active" if self._is_explicit_push_authorization((user_text or "").lower()) else "pending_confirmation"
        return self.create_commitment(
            {
                "kind": "learning_digest",
                "status": status,
                "title": f"学习资料摘要：{topic}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_learning_schedule(user_text),
                "payload": {"topic": topic, "phase": "getting_started", "goal_id": goal.get("goal_id")},
            }
        )

    def _upsert_user_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        state = self._read_goal_state()
        goals = state.get("goals") if isinstance(state.get("goals"), list) else []
        topic = str(payload.get("topic") or "")
        kind = str(payload.get("kind") or "generic")
        user_id = str(payload.get("user_id") or "local-user")
        updated: dict[str, Any] | None = None
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            if goal.get("kind") == kind and goal.get("topic") == topic and goal.get("user_id") == user_id and goal.get("status") != "archived":
                goal.update(payload)
                goal["updated_at"] = now
                updated = goal
                break
        if updated is None:
            updated = {
                "goal_id": f"goal_{uuid4().hex[:12]}",
                "created_at": now,
                "updated_at": now,
                **payload,
            }
            goals.append(updated)
        state["goals"] = [goal for goal in goals if isinstance(goal, dict)][-self.MAX_GOALS :]
        state["updated_at"] = now
        self._write_goal_state(state)
        return updated

    def _replace_user_goal(self, updated_goal: dict[str, Any]) -> None:
        goal_id = updated_goal.get("goal_id")
        if not goal_id:
            return
        state = self._read_goal_state()
        goals = state.get("goals") if isinstance(state.get("goals"), list) else []
        for index, goal in enumerate(goals):
            if isinstance(goal, dict) and goal.get("goal_id") == goal_id:
                updated_goal["updated_at"] = utc_now_iso()
                goals[index] = updated_goal
                state["goals"] = goals[-self.MAX_GOALS :]
                state["updated_at"] = utc_now_iso()
                self._write_goal_state(state)
                return

    def _sync_learning_goal_to_user_world(self, goal: dict[str, Any], commitment: dict[str, Any] | None) -> None:
        topic = str(goal.get("topic") or goal.get("title") or "学习计划")
        user_world = self.state_store.read_json("user_world.json")
        user_world["current_goal"] = f"learning:{topic}"[:180]
        user_world["goal_id"] = goal.get("goal_id")
        user_world["goal_kind"] = goal.get("kind")
        user_world["learning_topic"] = topic
        if commitment:
            user_world["commitment_id"] = commitment.get("commitment_id")
            user_world["commitment_kind"] = commitment.get("kind")
        user_world["updated_at"] = utc_now_iso()
        self.state_store.write_json("user_world.json", user_world)

    def _sync_goal_permission_from_commitment(self, commitment: dict[str, Any]) -> None:
        if commitment.get("kind") != "learning_digest":
            return
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        goal_id = payload.get("goal_id")
        if not goal_id:
            return
        state = self._read_goal_state()
        goals = state.get("goals") if isinstance(state.get("goals"), list) else []
        changed = False
        for goal in goals:
            if not isinstance(goal, dict) or goal.get("goal_id") != goal_id:
                continue
            goal["commitment_id"] = commitment.get("commitment_id")
            permissions = goal.setdefault("permissions", {})
            if isinstance(permissions, dict):
                status = str(commitment.get("status") or "")
                if status == "active":
                    permissions["external_search"] = "granted_for_digest"
                    permissions["proactive_push"] = "granted"
                elif status in {"cancelled", "paused"}:
                    permissions["proactive_push"] = status
                else:
                    permissions["proactive_push"] = "pending_confirmation"
            goal["updated_at"] = utc_now_iso()
            changed = True
            break
        if changed:
            state["goals"] = goals[-self.MAX_GOALS :]
            state["updated_at"] = utc_now_iso()
            self._write_goal_state(state)

    def _learning_plan(self, topic: str) -> dict[str, Any]:
        return {
            "phase": "getting_started",
            "steps": [
                {"name": "建立地图", "focus": f"列出 {topic} 的核心概念、前置知识和目标产出。"},
                {"name": "补基础", "focus": "按缺口补数学、编程或领域背景，只补会阻塞下一步的部分。"},
                {"name": "做小项目", "focus": "用一个可运行的小练习验证理解，保留过程和问题。"},
                {"name": "复盘迭代", "focus": "每周根据完成度调整材料难度和下一步计划。"},
            ],
            "checkpoints": ["今天确定目标与基础水平", "3 天内完成第一份学习地图", "7 天内完成一个最小练习"],
        }

    def _learning_goal_response(self, goal: dict[str, Any], commitment: dict[str, Any] | None) -> str:
        topic = str(goal.get("topic") or "这个主题")
        plan = goal.get("plan") if isinstance(goal.get("plan"), dict) else self._learning_plan(topic)
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        lines = [f"可以。我已把「{topic}」记录为你的学习目标。", "", "先按这个方案启动："]
        for index, step in enumerate(steps[:4], start=1):
            if isinstance(step, dict):
                lines.append(f"{index}. {step.get('name')}：{step.get('focus')}")
        checkpoints = plan.get("checkpoints") if isinstance(plan.get("checkpoints"), list) else []
        if checkpoints:
            lines.append("")
            lines.append("近期检查点：" + "；".join(str(item) for item in checkpoints[:3]))
        if commitment and commitment.get("status") == "active":
            lines.append("")
            lines.append("你已明确要求推送，我会按当前频率整理学习资料摘要。")
        else:
            lines.append("")
            lines.append("我还可以定期检索公开资料、课程或论文摘要并推送给你。回复「好的」或「同意开启」才会开启；回复「不用」则只保留学习目标。")
        return "\n".join(lines)

    def _looks_like_learning_goal_request(self, text: str) -> bool:
        lowered = (text or "").lower()
        markers = (
            "开始学习",
            "我要学",
            "我想学",
            "想学习",
            "准备学",
            "打算学",
            "帮我",
            "学习计划",
            "入门",
            "课程",
            "复习",
            "learn",
            "study",
        )
        if any(marker in lowered for marker in markers):
            return True
        return bool(re.search(r"(?:我要|我想|想|准备|开始|打算).{0,8}(?:学习|学)", text or ""))

    def _extract_from_user_text(self, text: str, *, event: VeyraEvent) -> dict[str, Any] | None:
        lowered = (text or "").lower()
        if not self._mentions_push_intent(lowered) and not any(marker in lowered for marker in LEARNING_MARKERS):
            return None

        if any(marker in lowered for marker in WEATHER_MARKERS) and any(marker in lowered for marker in DAILY_MARKERS + PUSH_MARKERS):
            location = self._extract_location(text)
            return {
                "kind": "weather_daily",
                "status": "active" if self._is_explicit_push_authorization(lowered) else "pending_confirmation",
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
                "status": "active" if self._is_explicit_push_authorization(lowered) else "pending_confirmation",
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
            if recompute_next_run:
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
        self._sync_goal_permission_from_commitment(updated)
        self.set_watchlists_for_commitment(updated)
        self.authorization.record_commitment(updated)
        return updated

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json(self.STATE_FILE) or {"commitments": [], "updated_at": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json(self.STATE_FILE, state)

    def _read_goal_state(self) -> dict[str, Any]:
        return self.state_store.read_json(self.GOAL_STATE_FILE) or {"goals": [], "updated_at": None}

    def _write_goal_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json(self.GOAL_STATE_FILE, state)

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
        agency_root = self._selected_agency_root()
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

    def _selected_agency_root(self) -> Path:
        explicit = os.getenv("VEYRA_AGENCY_DIR") or os.getenv("VEYRA_AGENCY_ROOT")
        if explicit:
            return Path(explicit)
        env = os.getenv("VEYRA_ENV", "").strip().lower()
        if env in {"dev", "prod", "test"}:
            return Path("agency") / env
        return Path("agency")

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
            r"([^\s，,。.?！!]{2,16}?)(?:今天|现在|当前|明天|今日)?(?:的)?(?:天气|气温)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                location = self._clean_location(match.group(1))
                if location:
                    return location
        for token in ("北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"):
            if token in (text or ""):
                return token
        return ""

    def _clean_location(self, value: str) -> str:
        location = (value or "").strip(" 的？?！!，,。")
        for prefix in ("今天", "现在", "当前", "明天", "今日"):
            if location.startswith(prefix) and len(location) > len(prefix):
                location = location[len(prefix) :]
        for suffix in ("今天", "现在", "当前", "明天", "今日", "的"):
            if location.endswith(suffix) and len(location) > len(suffix):
                location = location[: -len(suffix)]
        return location.strip(" 的？?！!，,。")

    def _extract_learning_topic(self, text: str) -> str:
        match = re.search(r"(?:学习|学|入门|掌握)\s*([^\s，,。.?！!？]{2,32})", text or "")
        if match:
            return self._clean_learning_topic(match.group(1))
        if "深度学习" in (text or ""):
            return "深度学习"
        return "学习计划"

    def _clean_learning_topic(self, value: str) -> str:
        topic = (value or "").strip(" 的了吧吗呢啊？?！!，,。")
        for suffix in ("了", "吧", "吗", "呢", "啊", "相关内容", "资料", "课程"):
            if topic.endswith(suffix) and len(topic) > len(suffix):
                topic = topic[: -len(suffix)]
        return topic.strip(" 的了吧吗呢啊？?！!，,。") or "学习计划"

    def _mentions_push_intent(self, lowered: str) -> bool:
        return any(marker in lowered for marker in PUSH_MARKERS + DAILY_MARKERS)

    def _is_affirmation(self, lowered: str) -> bool:
        text = re.sub(r"[\s，,。.!！?？、]+", "", lowered or "")
        if not text:
            return False
        if text in AFFIRM_EXACT_MARKERS:
            return True
        if len(text) <= 12 and any(marker in text for marker in AFFIRM_MARKERS):
            return True
        return any(phrase in text for phrase in AFFIRM_PHRASES)

    def _is_explicit_push_authorization(self, lowered: str) -> bool:
        text = lowered or ""
        has_push_action = any(marker in text for marker in PUSH_MARKERS + ("订阅", "subscribe"))
        has_schedule_or_enable = any(marker in text for marker in DAILY_MARKERS + ("开启", "启用", "定时", "定期", "每天", "每日"))
        return has_push_action and has_schedule_or_enable

    def _is_decline(self, lowered: str) -> bool:
        return any(marker in lowered for marker in DECLINE_MARKERS)
