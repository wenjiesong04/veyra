from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.definitions import RiskLevel
from core.proactive_authorization import AuthorizationPolicy
from core.proactive_intent import ProactiveIntent, WatchlistDraft
from core.proactive_intent_planner import ProactiveIntentPlanner
from core.schedule_parser import chinese_hour as parsed_chinese_hour
from core.schedule_parser import end_of_local_day_iso, has_explicit_schedule_time, parse_schedule_text
from core.semantic_changeset import build_semantic_change_set, confirmation_message, confirmed_message, declined_message
from core.proactive_templates import ProactiveTemplateRegistry, watchlist_id_for
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso
from memory_bridge.local_memory_bridge import LocalMemoryBridge
from runtime.self_improvement import SelfImprovementProposalRegistry


AFFIRM_EXACT_MARKERS = ("好的", "好", "可以", "行", "同意", "确认", "没问题", "ok", "yes", "sure", "开启", "启用")
AFFIRM_MARKERS = ("好的", "可以", "行", "同意", "确认", "没问题", "ok", "yes", "sure", "开启", "启用")
AFFIRM_PHRASES = ("同意开启", "确认开启", "帮我开启", "可以开启", "开始推送", "开启推送", "订阅", "subscribe")
DECLINE_MARKERS = ("不用", "不要", "取消", "算了", "不需要", "no", "decline", "stop")
DAILY_MARKERS = ("每天", "每日", "每天早上", "每天早晨", "定时", "定期", "daily", "every morning", "each day")
WEATHER_MARKERS = ("天气", "气温", "weather", "下雨", "晴天")
LEARNING_MARKERS = ("学习", "深度学习", "机器学习", "入门", "教程", "课程", "复习", "learn", "study")
PUSH_MARKERS = ("推送", "提醒", "通知", "告诉我", "发给我", "push", "notify", "remind")
TRACKING_REQUEST_MARKERS = ("关注", "跟踪", "留意", "持续关注", "订阅")
TRACKING_REQUEST_MARKERS_EN = ("watch", "track", "monitor", "subscribe")
INVALID_WEATHER_LOCATION_MARKERS = (
    "以后",
    "能每天",
    "每天发",
    "发天气",
    "天气信息",
    "可以",
    "能不能",
    "给我",
    "帮我",
    "吗",
    "？",
    "?",
)


class CommitmentCore:
    """User commitments: goals, schedules, and proactive push contracts."""

    STATE_FILE = "user_commitments.json"
    GOAL_STATE_FILE = "user_goals.json"
    SEMANTIC_CHANGE_SET_FILE = "semantic_change_sets.json"
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
        self.self_improvement = SelfImprovementProposalRegistry(state_store)

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
        inner_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        validation: dict[str, Any] = {"valid": True}
        if kind == "weather_daily":
            location = self._normalize_weather_location(str(inner_payload.get("location") or ""))
            if location:
                inner_payload = {**inner_payload, "location": location}
            validation = self._validate_weather_daily_payload(inner_payload, schedule, require_explicit_time=False)
            payload = {**payload, "payload": inner_payload}
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
            "payload": inner_payload,
            "source_event_id": payload.get("source_event_id"),
            "created_at": now,
            "updated_at": now,
            "confirmed_at": payload.get("confirmed_at"),
            "next_run_at": payload.get("next_run_at") or self._compute_next_run_at(schedule),
            "last_run_at": None,
            "run_count": 0,
            "push_history": [],
        }
        if not validation.get("valid"):
            item["status"] = "paused"
            item["invalid"] = True
            item["invalid_reason"] = str(validation.get("reason") or "invalid_weather_commitment")
            item["invalid_detected_at"] = now
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
        self.heal_invalid_commitments()
        now = now or datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for item in self.list_commitments(status="active"):
            schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
            if self._schedule_has_ended(schedule, now=now):
                self._pause_ended_commitment(item, now=now)
                continue
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
        schedule = commitment.get("schedule") if isinstance(commitment.get("schedule"), dict) else {}
        if self._schedule_has_ended(schedule, now=now):
            return False, "schedule_ended"
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
            next_run_at = self._compute_next_run_at(schedule)
            patch["next_run_at"] = next_run_at
            if str(schedule.get("kind") or "") == "once" and not next_run_at:
                patch["status"] = "paused"
                patch["ended_at"] = utc_now_iso()
                patch["end_reason"] = "once_completed"
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

        if self._is_learning_memory_question(user_text):
            return {}

        lowered = (user_text or "").lower()
        semantic_intent = self.semantic_intent_for_turn(user_text=user_text, event=event)
        result: dict[str, Any] = {"status": "idle", "actions": [], "semantic_intent": semantic_intent}

        if semantic_intent.get("operation") == "query_status":
            return self.answer_commitment_status(semantic_intent, event=event)

        pending = self._pending_for_session(event.source.session_id)
        if pending and (semantic_intent.get("operation") == "confirm" or self._is_affirmation(lowered)):
            confirmed = self._confirm_pending_commitment(pending, user_text=user_text)
            if confirmed:
                result = {
                    "status": "confirmed",
                    "commitment": confirmed,
                    "actions": ["confirmed_pending"],
                    "semantic_intent": semantic_intent,
                    "primary_response_override": f"已开启：{confirmed.get('title') or confirmed.get('kind')}",
                }
                return result
        if pending and (semantic_intent.get("operation") == "decline" or self._is_decline(lowered)):
            cancelled = self.cancel_commitment(str(pending.get("commitment_id")))
            if cancelled:
                return {
                    "status": "declined",
                    "commitment": cancelled,
                    "actions": ["cancelled_pending"],
                    "semantic_intent": semantic_intent,
                    "primary_response_override": f"已取消：{cancelled.get('title') or cancelled.get('kind')}",
                }

        pending_change_set = self._pending_semantic_change_set_for_session(event.source.session_id, user_id=event.source.user_id)
        if pending_change_set and (semantic_intent.get("operation") == "confirm" or self._is_affirmation(lowered)):
            return self._confirm_semantic_change_set(pending_change_set, event=event)
        if pending_change_set and (semantic_intent.get("operation") == "decline" or self._is_decline(lowered)):
            return self._decline_semantic_change_set(pending_change_set)

        # Bare confirmations without a pending offer must not create or confirm commitments.
        if self._is_affirmation(lowered) and not pending:
            return {}

        change_set = self.semantic_change_set_for_turn(user_text=user_text, event=event)
        if change_set and self._semantic_change_should_precede_control(semantic_intent, change_set):
            return self._handle_semantic_change_set(change_set, event=event)

        if semantic_intent.get("operation") in {"cancel", "pause", "resume"}:
            return self.apply_control_semantic(semantic_intent, event=event)

        if change_set:
            return self._handle_semantic_change_set(change_set, event=event)

        if self._looks_like_plain_information_query(user_text, lowered):
            return {}

        if not self._looks_like_proactive_request(user_text, lowered):
            return {}

        if self._is_weather_commitment_request(user_text, lowered):
            candidate = self._weather_commitment_candidate(user_text, event=event)
            validation = self._validate_weather_daily_payload(
                candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {},
                candidate.get("schedule") if isinstance(candidate.get("schedule"), dict) else {},
                require_explicit_time=True,
                source_text=user_text,
            )
            if not validation.get("valid"):
                return self._weather_parameters_needed(validation)
            if self._is_explicit_push_authorization(lowered):
                existing = self._existing_weather_commitment(event, str((candidate.get("payload") or {}).get("location") or ""))
                if existing:
                    return {
                        "status": "already_exists",
                        "commitment": existing,
                        "actions": ["weather_commitment_already_exists"],
                        "primary_response_override": f"已经有这个每日天气任务：{existing.get('title') or existing.get('kind')}",
                    }
                created = self.create_commitment(candidate)
                return {
                    "status": "created",
                    "commitment": created,
                    "actions": ["created_from_user"],
                    "primary_response_override": f"已开启：{created.get('title') or '每日天气'}",
                }

        intent = self.intent_planner.plan(user_text=user_text, event=event)
        if self._intent_is_answer_only(intent):
            return {}
        intent_record = self.intent_planner.record_intent(intent)

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

        if intent.intent_type == "unknown":
            proposal = self.self_improvement.propose_from_intent(intent, reason="unknown_proactive_intent")
            return {
                "status": "intent_draft",
                "actions": ["recorded_intent_draft", "created_self_improvement_proposal"],
                "intent": intent_record,
                "capability_gap": proposal.get("gap_description"),
                "self_improvement_proposal": proposal,
                "followup_messages": ["我先把这个需求记录为主动意图草案，但还不能安全确定信息源、频率或模板；不会创建 active 推送。已生成一条需要人工审查的能力改进建议。"],
            }

        extracted = self._extract_from_user_text(user_text, event=event)
        if extracted:
            if extracted.get("kind") == "weather_daily":
                validation = self._validate_weather_daily_payload(
                    extracted.get("payload") if isinstance(extracted.get("payload"), dict) else {},
                    extracted.get("schedule") if isinstance(extracted.get("schedule"), dict) else {},
                    require_explicit_time=True,
                    source_text=user_text,
                )
                if not validation.get("valid"):
                    return self._weather_parameters_needed(validation)
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
        return result if result.get("status") != "idle" else {}

    def semantic_intent_for_turn(self, *, user_text: str, event: VeyraEvent) -> dict[str, Any]:
        """Narrow semantic guardrail for commitment state/control turns.

        The model planner can still propose proactive creations, but local state
        queries and destructive controls must be classified before mutation.
        """
        text = user_text or ""
        lowered = text.lower()
        pending = self._pending_for_session(event.source.session_id)
        operation = self._semantic_operation(text=text, lowered=lowered, pending=pending)
        scope = "all" if self._semantic_all_scope(text, lowered) else "matching"
        target_type = self._semantic_target_type(text, lowered, scope=scope)
        topic = self._semantic_topic(text)
        location = ""
        if target_type == "weather":
            location = self._normalize_weather_location(self._extract_location(text))
        confidence = 0.15
        if operation in {"query_status", "cancel", "pause", "resume"}:
            confidence = 0.72
        if topic or location or scope == "all":
            confidence = max(confidence, 0.84)
        if operation == "query_status":
            confidence = max(confidence, 0.88)
        if operation in {"confirm", "decline"} and pending:
            confidence = max(confidence, 0.9)
        ambiguous = operation in {"cancel", "pause", "resume"} and scope != "all" and not topic and not location and not pending
        return {
            "operation": operation,
            "target_type": target_type,
            "entities": {
                "topic": topic,
                "location": location,
                "scope": scope,
                "session_id": event.source.session_id,
                "user_id": event.source.user_id,
            },
            "confidence": round(confidence, 3),
            "ambiguous": bool(ambiguous),
            "source": "commitment_semantic_guardrail",
            "raw_text": text,
        }

    def answer_commitment_status(self, semantic_intent: dict[str, Any], *, event: VeyraEvent) -> dict[str, Any]:
        match_info = self.match_commitments_semantic(
            semantic_intent,
            event=event,
            statuses=["active", "pending_confirmation", "paused", "cancelled"],
        )
        matches = match_info.get("matches") if isinstance(match_info.get("matches"), list) else []
        entities = semantic_intent.get("entities") if isinstance(semantic_intent.get("entities"), dict) else {}
        requested = self._requested_status_from_query(str(entities.get("topic") or ""), semantic_intent)
        topic = str(entities.get("topic") or entities.get("location") or "").strip()
        watchlists = self._match_watchlists_for_semantic(semantic_intent, event=event)
        state_answer = ""

        if matches:
            label = self._semantic_target_label(topic, matches[0])
            statuses = {str(item.get("status") or "") for item in matches}
            if requested == "cancelled":
                if statuses <= {"cancelled"}:
                    state_answer = f"是，{label}已取消。"
                elif "cancelled" in statuses and len(statuses) > 1:
                    state_answer = f"{label}存在已取消记录，也还有未取消的相关任务：{self._status_summary(matches)}。"
                elif "paused" in statuses:
                    state_answer = f"还没有取消，{label}当前是已暂停状态。"
                else:
                    state_answer = f"还没有取消，{label}当前仍在运行或待确认：{self._status_summary(matches)}。"
            elif requested == "paused":
                if statuses <= {"paused"}:
                    state_answer = f"是，{label}已暂停。"
                elif "active" in statuses:
                    state_answer = f"还没有暂停，{label}当前仍在运行。"
                else:
                    state_answer = f"{label}当前状态：{self._status_summary(matches)}。"
            elif requested == "active":
                if "active" in statuses:
                    state_answer = f"还在，{label}当前仍在运行。"
                elif statuses <= {"cancelled"}:
                    state_answer = f"不在了，{label}已取消。"
                elif statuses <= {"paused"}:
                    state_answer = f"不在运行，{label}已暂停。"
                else:
                    state_answer = f"{label}当前状态：{self._status_summary(matches)}。"
            else:
                state_answer = f"{label}当前状态：{self._status_summary(matches)}。"
        elif watchlists:
            label = topic or str(watchlists[0].get("topic") or "这个追踪")
            statuses = {str(item.get("status") or "") for item in watchlists}
            if requested == "cancelled" and statuses <= {"cancelled"}:
                state_answer = f"是，{label}相关内容追踪已取消。"
            elif "active" in statuses:
                state_answer = f"{label}相关内容追踪仍在运行。"
            else:
                state_answer = f"{label}相关内容追踪状态：{self._watchlist_status_summary(watchlists)}。"
        elif match_info.get("ambiguous"):
            candidates = match_info.get("candidates") if isinstance(match_info.get("candidates"), list) else []
            state_answer = f"你想查哪个主动任务？当前有：{self._candidate_summary(candidates)}。"
        else:
            label = topic or "相关主动任务"
            state_answer = f"没有找到{label}的主动任务记录。"

        return {
            "status": "state_answer",
            "actions": ["answered_commitment_state"],
            "semantic_intent": semantic_intent,
            "commitment_matches": [self._compact_commitment(item) for item in matches],
            "watchlist_matches": [self._compact_watchlist(item) for item in watchlists],
            "state_answer": state_answer,
            "primary_response_override": state_answer,
        }

    def apply_control_semantic(self, semantic_intent: dict[str, Any], *, event: VeyraEvent) -> dict[str, Any]:
        action = str(semantic_intent.get("operation") or "")
        statuses = {"cancel": ["active", "pending_confirmation"], "pause": ["active", "pending_confirmation"], "resume": ["paused"]}.get(action, [])
        match_info = self.match_commitments_semantic(semantic_intent, event=event, statuses=statuses)
        matches = match_info.get("matches") if isinstance(match_info.get("matches"), list) else []
        candidates = match_info.get("candidates") if isinstance(match_info.get("candidates"), list) else []
        if match_info.get("ambiguous"):
            response = f"你要{self._action_word(action)}哪个主动任务？当前可操作的任务有：{self._candidate_summary(candidates)}。"
            return {
                "status": "needs_disambiguation",
                "actions": [f"{action}_needs_disambiguation"],
                "semantic_intent": {**semantic_intent, "ambiguous": True},
                "commitment_matches": [],
                "state_answer": response,
                "primary_response_override": response,
                "template": "cancel_pause_resume",
            }
        if not matches:
            response = "没有找到正在进行的相关主动任务。"
            return {
                "status": "not_found",
                "actions": [f"{action}_matching_commitments"],
                "semantic_intent": semantic_intent,
                "commitment_matches": [],
                "state_answer": response,
                "primary_response_override": response,
                "template": "cancel_pause_resume",
            }

        updated: list[dict[str, Any]] = []
        for item in matches:
            commitment_id = str(item.get("commitment_id") or "")
            if not commitment_id:
                continue
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
        intent = self._intent_from_semantic(semantic_intent, event=event, action=action)
        self._audit_commitment_control(intent, action=action, matches=matches, updated=updated)
        status_word = {"cancel": "cancelled", "pause": "paused", "resume": "resumed"}.get(action, action)
        response = self._control_response(action, updated)
        return {
            "status": status_word,
            "actions": [f"{action}_matching_commitments"],
            "semantic_intent": semantic_intent,
            "commitment_matches": [self._compact_commitment(item) for item in matches],
            "matched_commitments": matches,
            "updated_commitments": updated,
            "state_answer": response,
            "primary_response_override": response,
            "template": "cancel_pause_resume",
        }

    def semantic_change_set_for_turn(self, *, user_text: str, event: VeyraEvent) -> dict[str, Any] | None:
        commitments = self.list_commitments(user_id=event.source.user_id)
        change_set = build_semantic_change_set(user_text, active_commitments=commitments)
        return change_set.to_dict() if change_set else None

    def _semantic_change_should_precede_control(self, semantic_intent: dict[str, Any], change_set: dict[str, Any]) -> bool:
        """Prefer the semantic changeset when a turn implies more than one state change.

        Direct single controls such as "取消 PyTorch 追踪" should still execute.
        Compound or tentative turns such as "取消 PyTorch，改成 TensorFlow" must
        be confirmed as a change set so the second operation is not dropped.
        """
        operation = str(semantic_intent.get("operation") or "")
        if operation not in {"cancel", "pause", "resume"}:
            return False
        actions = change_set.get("proposed_actions") if isinstance(change_set.get("proposed_actions"), list) else []
        changes = change_set.get("changes") if isinstance(change_set.get("changes"), list) else []
        if len(actions) > 1 or len(changes) > 1:
            return True
        event = change_set.get("semantic_event") if isinstance(change_set.get("semantic_event"), dict) else {}
        return str(event.get("modality") or "") in {"tentative", "hypothetical"}

    def _handle_semantic_change_set(self, change_set: dict[str, Any], *, event: VeyraEvent) -> dict[str, Any]:
        record = self._record_semantic_change_set(change_set, event=event)
        execution = record.get("execution_decision") if isinstance(record.get("execution_decision"), dict) else {}
        if execution.get("mode") == "ask_confirmation":
            response = confirmation_message(record)
            return {
                "status": "semantic_change_proposed",
                "actions": ["recorded_semantic_change_set", "asked_semantic_change_confirmation"],
                "semantic_change_set": record,
                "primary_response_override": response,
                "followup_messages": [response],
            }
        response = confirmation_message(record)
        return {
            "status": "semantic_change_recorded",
            "actions": ["recorded_semantic_change_set"],
            "semantic_change_set": record,
            "primary_response_override": response,
            "followup_messages": [response],
        }

    def _confirm_semantic_change_set(self, change_set: dict[str, Any], *, event: VeyraEvent) -> dict[str, Any]:
        actions = change_set.get("proposed_actions") if isinstance(change_set.get("proposed_actions"), list) else []
        applied = [self._apply_semantic_proposed_action(action, event=event, change_set=change_set) for action in actions if isinstance(action, dict)]
        applied = [item for item in applied if item]
        patch = {
            "status": "confirmed",
            "confirmed_at": utc_now_iso(),
            "confirmed_by_event_id": event.event_id,
            "applied_actions": applied,
        }
        updated = self._patch_semantic_change_set(str(change_set.get("changeset_id") or ""), patch) or {**change_set, **patch}
        response = confirmed_message(applied)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "semantic_change_set",
                "status": "confirmed",
                "artifacts": {
                    "changeset_id": updated.get("changeset_id"),
                    "applied_actions": applied,
                },
            },
        )
        return {
            "status": "semantic_change_confirmed",
            "actions": ["confirmed_semantic_change_set", *[str(item.get("action") or "") for item in applied]],
            "semantic_change_set": updated,
            "applied_actions": applied,
            "primary_response_override": response,
            "followup_messages": [response],
        }

    def _decline_semantic_change_set(self, change_set: dict[str, Any]) -> dict[str, Any]:
        patch = {"status": "declined", "declined_at": utc_now_iso()}
        updated = self._patch_semantic_change_set(str(change_set.get("changeset_id") or ""), patch) or {**change_set, **patch}
        response = declined_message(updated)
        return {
            "status": "semantic_change_declined",
            "actions": ["declined_semantic_change_set"],
            "semantic_change_set": updated,
            "primary_response_override": response,
            "followup_messages": [response],
        }

    def _apply_semantic_proposed_action(self, action: dict[str, Any], *, event: VeyraEvent, change_set: dict[str, Any]) -> dict[str, Any] | None:
        action_name = str(action.get("action") or "")
        target = str(action.get("target") or "").strip()
        if not action_name or not target:
            return None
        if action_name == "deprioritize_tracking":
            commitment_id = str(action.get("commitment_id") or "")
            target_item = self.get_commitment(commitment_id) if commitment_id else self._existing_topic_commitment(
                target,
                user_id=event.source.user_id,
                kinds={"external_digest", "learning_digest"},
                statuses={"active", "pending_confirmation"},
            )
            if not target_item:
                return {"action": action_name, "target": target, "status": "not_found"}
            changed = self.pause_commitment(str(target_item.get("commitment_id") or ""))
            return {
                "action": action_name,
                "target": target,
                "status": "paused" if changed else "not_found",
                "commitment_id": (changed or target_item).get("commitment_id"),
                "commitment_kind": (changed or target_item).get("kind"),
            }
        if action_name == "create_learning_tracking":
            return self._create_learning_tracking_from_semantic(target=target, event=event, change_set=change_set)
        if action_name == "create_tracking":
            return self._create_external_tracking_from_semantic(target=target, event=event, change_set=change_set)
        return {"action": action_name, "target": target, "status": "unsupported"}

    def _create_learning_tracking_from_semantic(self, *, target: str, event: VeyraEvent, change_set: dict[str, Any]) -> dict[str, Any]:
        existing = self._existing_topic_commitment(
            target,
            user_id=event.source.user_id,
            kinds={"learning_digest"},
            statuses={"active", "pending_confirmation"},
        )
        if existing:
            return {
                "action": "create_learning_tracking",
                "target": target,
                "status": "already_exists",
                "commitment_id": existing.get("commitment_id"),
                "commitment_kind": existing.get("kind"),
            }
        goal = self._upsert_user_goal(
            {
                "kind": "learning",
                "status": "active",
                "title": f"学习目标：{target}",
                "topic": target,
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "source_changeset_id": change_set.get("changeset_id"),
                "last_user_text": str(change_set.get("source_text") or "")[:500],
                "plan": self._learning_plan(target),
                "permissions": {
                    "store_progress": "granted_by_semantic_confirmation",
                    "external_search": "granted_for_digest",
                    "proactive_push": "granted",
                },
            }
        )
        commitment = self.create_commitment(
            {
                "kind": "learning_digest",
                "status": "active",
                "title": f"学习资料摘要：{target}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_learning_schedule(str(change_set.get("source_text") or target)),
                "payload": {"topic": target, "phase": "getting_started", "goal_id": goal.get("goal_id"), "source_changeset_id": change_set.get("changeset_id")},
            }
        )
        goal["commitment_id"] = commitment.get("commitment_id")
        self._replace_user_goal(goal)
        return {
            "action": "create_learning_tracking",
            "target": target,
            "status": "created",
            "goal_id": goal.get("goal_id"),
            "commitment_id": commitment.get("commitment_id"),
            "commitment_kind": commitment.get("kind"),
        }

    def _create_external_tracking_from_semantic(self, *, target: str, event: VeyraEvent, change_set: dict[str, Any]) -> dict[str, Any]:
        existing = self._existing_topic_commitment(
            target,
            user_id=event.source.user_id,
            kinds={"external_digest"},
            statuses={"active", "pending_confirmation"},
        )
        if existing:
            return {
                "action": "create_tracking",
                "target": target,
                "status": "already_exists",
                "commitment_id": existing.get("commitment_id"),
                "commitment_kind": existing.get("kind"),
            }
        watchlist = self.record_watchlist_draft(
            WatchlistDraft(
                topic=target,
                query=self.external_tracking_query(
                    ProactiveIntent(
                        user_id=event.source.user_id,
                        session_id=event.source.session_id,
                        channel_id=event.source.channel,
                        raw_text=str(change_set.get("source_text") or ""),
                        intent_type="track_external_topic",
                        topic=target,
                        information_sources=["public_web"],
                        source="semantic_change_set",
                    )
                ),
                sources=["public_web"],
                refresh_policy={"kind": "authorized_external_refresh", "ttl_seconds": 1800},
                ranking_policy={"prefer": ["trusted_sources", "freshness", "topic_match"]},
                dedupe_policy={"key": "url"},
                ttl=1800,
                status="active",
                source_intent_id=str(change_set.get("changeset_id") or ""),
            )
        )
        commitment = self.create_commitment(
            {
                "kind": "external_digest",
                "status": "active",
                "title": f"外部追踪：{target}",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": self._default_learning_schedule(str(change_set.get("source_text") or target)),
                "payload": {"topic": target, "watchlist_id": watchlist.get("watchlist_id"), "query": watchlist.get("query"), "source_changeset_id": change_set.get("changeset_id")},
            }
        )
        return {
            "action": "create_tracking",
            "target": target,
            "status": "created",
            "watchlist_id": watchlist.get("watchlist_id"),
            "commitment_id": commitment.get("commitment_id"),
            "commitment_kind": commitment.get("kind"),
        }

    def _existing_topic_commitment(
        self,
        topic: str,
        *,
        user_id: str,
        kinds: set[str],
        statuses: set[str],
    ) -> dict[str, Any] | None:
        needle = self._normalize_match_text(topic)
        for item in reversed(self.list_commitments(user_id=user_id)):
            if not isinstance(item, dict) or item.get("kind") not in kinds or item.get("status") not in statuses:
                continue
            if self._commitment_matches_needle(item, needle):
                return item
        return None

    def _record_semantic_change_set(self, change_set: dict[str, Any], *, event: VeyraEvent) -> dict[str, Any]:
        state = self._read_semantic_change_state()
        items = state.setdefault("change_sets", [])
        if not isinstance(items, list):
            items = []
        record = {
            **change_set,
            "user_id": event.source.user_id,
            "channel": event.source.channel,
            "session_id": event.source.session_id,
            "source_event_id": event.event_id,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        items.append(record)
        state["change_sets"] = items[-200:]
        state["updated_at"] = utc_now_iso()
        self._write_semantic_change_state(state)
        self.state_store.append_jsonl(
            "decision_trace.jsonl",
            {
                "route": "semantic_change_set",
                "status": record.get("status"),
                "artifacts": {"changeset_id": record.get("changeset_id"), "execution_decision": record.get("execution_decision")},
            },
        )
        return record

    def _pending_semantic_change_set_for_session(self, session_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        state = self._read_semantic_change_state()
        items = state.get("change_sets") if isinstance(state.get("change_sets"), list) else []
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("status") != "pending_confirmation":
                continue
            if item.get("session_id") != session_id:
                continue
            if user_id and item.get("user_id") != user_id:
                continue
            return item
        return None

    def _patch_semantic_change_set(self, changeset_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        if not changeset_id:
            return None
        state = self._read_semantic_change_state()
        items = state.get("change_sets") if isinstance(state.get("change_sets"), list) else []
        updated: dict[str, Any] | None = None
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("changeset_id") != changeset_id:
                continue
            item.update(patch)
            item["updated_at"] = utc_now_iso()
            items[index] = item
            updated = item
            break
        if updated is None:
            return None
        state["change_sets"] = items[-200:]
        state["updated_at"] = utc_now_iso()
        self._write_semantic_change_state(state)
        return updated

    def _read_semantic_change_state(self) -> dict[str, Any]:
        return self.state_store.read_json(self.SEMANTIC_CHANGE_SET_FILE) or {"change_sets": [], "updated_at": None}

    def _write_semantic_change_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json(self.SEMANTIC_CHANGE_SET_FILE, state)

    def match_commitments_semantic(
        self,
        semantic_intent: dict[str, Any],
        *,
        event: VeyraEvent,
        statuses: list[str],
    ) -> dict[str, Any]:
        entities = semantic_intent.get("entities") if isinstance(semantic_intent.get("entities"), dict) else {}
        topic = str(entities.get("topic") or "").strip()
        location = str(entities.get("location") or "").strip()
        scope = str(entities.get("scope") or "matching")
        target_type = str(semantic_intent.get("target_type") or "unknown")
        candidates = [
            item
            for item in self.list_commitments(user_id=event.source.user_id)
            if isinstance(item, dict)
            and item.get("status") in statuses
            and self._commitment_kind_matches(target_type, item)
        ]
        if scope == "all":
            return {"matches": candidates, "candidates": candidates, "ambiguous": False}

        exact = self._exact_commitment_matches(entities, candidates)
        if exact:
            return {"matches": exact, "candidates": candidates, "ambiguous": False}

        needle = location if target_type == "weather" and location else topic
        if needle:
            matches = [item for item in candidates if self._commitment_matches_needle(item, needle)]
            return {"matches": matches, "candidates": candidates, "ambiguous": False}

        recent_topic = self._recent_commitment_topic_for_session(event.source.session_id)
        if recent_topic:
            matches = [item for item in candidates if self._commitment_matches_needle(item, recent_topic)]
            if matches:
                return {"matches": matches, "candidates": candidates, "ambiguous": False}

        if len(candidates) == 1:
            return {"matches": candidates, "candidates": candidates, "ambiguous": False}
        return {"matches": [], "candidates": candidates, "ambiguous": len(candidates) > 1}

    def _semantic_operation(self, *, text: str, lowered: str, pending: dict[str, Any] | None) -> str:
        if self._is_commitment_status_query(text, lowered):
            return "query_status"
        if pending and self._is_affirmation(lowered):
            return "confirm"
        if pending and self._is_decline(lowered):
            return "decline"
        if self._has_resume_command(text, lowered):
            return "resume"
        if self._has_pause_command(text, lowered):
            return "pause"
        if self._has_cancel_command(text, lowered):
            return "cancel"
        if self._looks_like_proactive_request(text, lowered):
            return "create"
        return "unknown"

    def _is_commitment_status_query(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if not compact:
            return False
        questionish = any(
            marker in compact
            for marker in (
                "是否",
                "是不是",
                "有没有",
                "还在吗",
                "还在",
                "还会",
                "了吗",
                "了么",
                "状态",
                "查一下",
                "现在是否",
            )
        ) or any(marker in lowered for marker in ("still", "status", "cancelled", "canceled", "is it", "did you", "has it"))
        stateish = any(
            marker in compact
            for marker in (
                "取消",
                "停止",
                "停了",
                "停掉",
                "暂停",
                "恢复",
                "追踪",
                "跟踪",
                "关注",
                "推送",
                "提醒",
                "任务",
                "订阅",
            )
        ) or any(marker in lowered for marker in ("track", "watch", "monitor", "push", "subscription", "commitment"))
        return bool(questionish and stateish)

    def _semantic_all_scope(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return any(marker in compact for marker in ("所有", "全部", "全都", "都停止", "都取消", "都停掉")) or any(
            marker in lowered for marker in ("all", "everything")
        )

    def _semantic_target_type(self, text: str, lowered: str, *, scope: str) -> str:
        if scope == "all":
            return "all"
        if any(marker in lowered for marker in WEATHER_MARKERS):
            return "weather"
        if any(marker in lowered for marker in LEARNING_MARKERS):
            return "learning"
        if any(marker in (text or "") for marker in ("追踪", "跟踪", "关注", "订阅")) or any(
            marker in lowered for marker in TRACKING_REQUEST_MARKERS_EN + ("release", "github", "pytorch")
        ):
            return "external_tracking"
        return "unknown"

    def _semantic_topic(self, text: str) -> str:
        value = text or ""
        match = re.search(r"(?i)(PyTorch(?:\s*\d+(?:\.\d+)*)?)", value)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
        token_match = re.search(r"(?i)(?:关注|追踪|跟踪|留意|订阅|取消|停止|暂停|恢复|monitor|track|watch|cancel|pause|resume)\s*([A-Za-z][A-Za-z0-9_.+-]*(?:\s*\d+(?:\.\d+)*)?)", value)
        if token_match:
            return re.sub(r"\s+", " ", token_match.group(1)).strip()
        cleaned = value.strip(" \t\r\n的了吧吗呢啊？?！!，,。")
        prefixes = (
            "现在是否已经",
            "现在是否",
            "是不是已经",
            "是否已经",
            "有没有",
            "别再关注",
            "不要再关注",
            "不用再关注",
            "取消",
            "停止",
            "停掉",
            "暂停",
            "恢复",
            "继续",
            "查询",
            "查一下",
        )
        for prefix in prefixes:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        suffixes = (
            "相关内容追踪",
            "内容追踪",
            "相关追踪",
            "更新提醒",
            "相关内容",
            "新版本和重要更新",
            "重要更新",
            "追踪",
            "跟踪",
            "关注",
            "推送",
            "提醒",
            "任务",
            "订阅",
            "了吗",
            "吗",
            "了",
        )
        for suffix in suffixes:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        cleaned = cleaned.strip(" 的了吧吗呢啊？?！!，,。")
        if not cleaned or cleaned in {"这个", "刚才那个", "刚才", "所有", "全部", "任务", "推送", "追踪", "关注", "取消", "停止", "暂停", "恢复"}:
            return ""
        if len(cleaned) > 64:
            return ""
        return cleaned

    def _has_cancel_command(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        if any(marker in compact for marker in ("不要取消", "不用取消", "别取消", "先别取消", "不要停止", "不用停止", "别停止")):
            return False
        return any(marker in lowered for marker in ("cancel", "stop")) or any(
            marker in compact for marker in ("停止", "停掉", "取消", "别再", "不要再", "不用再", "不再关注", "关闭")
        )

    def _has_pause_command(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return "pause" in lowered or any(marker in compact for marker in ("暂停", "先暂停", "暂时停", "先停一下", "最近先暂停"))

    def _has_resume_command(self, text: str, lowered: str) -> bool:
        compact = re.sub(r"[\s，,。！？!?、]+", "", text or "")
        return "resume" in lowered or any(marker in compact for marker in ("恢复", "重新开启", "恢复推送", "继续推", "继续给我推"))

    def _commitment_kind_matches(self, target_type: str, item: dict[str, Any]) -> bool:
        kind = str(item.get("kind") or "")
        if target_type == "weather":
            return kind == "weather_daily"
        if target_type == "learning":
            return kind == "learning_digest"
        if target_type == "external_tracking":
            return kind == "external_digest"
        return True

    def _exact_commitment_matches(self, entities: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        commitment_id = str(entities.get("commitment_id") or "").strip()
        watchlist_id = str(entities.get("watchlist_id") or "").strip()
        if commitment_id:
            return [item for item in candidates if str(item.get("commitment_id") or "") == commitment_id]
        if watchlist_id:
            return [
                item
                for item in candidates
                if str((item.get("payload") if isinstance(item.get("payload"), dict) else {}).get("watchlist_id") or "") == watchlist_id
            ]
        return []

    def _commitment_matches_needle(self, item: dict[str, Any], needle: str) -> bool:
        normalized_needle = self._normalize_match_text(needle)
        if len(normalized_needle) < 2:
            return False
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        haystack = " ".join(
            str(part or "")
            for part in (
                item.get("commitment_id"),
                item.get("kind"),
                item.get("title"),
                payload.get("watchlist_id"),
                payload.get("topic"),
                payload.get("location"),
                payload.get("note"),
                payload.get("query"),
            )
        )
        normalized_haystack = self._normalize_match_text(haystack)
        return normalized_needle in normalized_haystack or normalized_haystack in normalized_needle

    def _normalize_match_text(self, value: str) -> str:
        text = re.sub(r"[\s，,。！？!?、·:：；;（）()【】\[\]_\-]+", "", (value or "").lower())
        for token in ("相关内容", "内容", "更新提醒", "提醒", "追踪", "跟踪", "关注", "外部", "新版本", "重要更新"):
            text = text.replace(token, "")
        return text

    def _recent_commitment_topic_for_session(self, session_id: str) -> str:
        state = self.state_store.read_json("task_state.json")
        slots_by_session = state.get("conversation_slots") if isinstance(state.get("conversation_slots"), dict) else {}
        slots = slots_by_session.get(session_id) if isinstance(slots_by_session, dict) else {}
        if not isinstance(slots, dict):
            return ""
        for key in ("last_commitment_topic", "last_search_query", "last_topic"):
            value = str(slots.get(key) or "").strip()
            if value and value not in {"weather", "天气"}:
                return value
        return ""

    def _requested_status_from_query(self, text: str, semantic_intent: dict[str, Any]) -> str:
        raw = " ".join(
            str(part or "")
            for part in (
                text,
                semantic_intent.get("raw_text"),
                (semantic_intent.get("entities") if isinstance(semantic_intent.get("entities"), dict) else {}).get("topic"),
            )
        )
        compact = re.sub(r"[\s，,。！？!?、]+", "", raw)
        if any(marker in compact for marker in ("取消", "停止", "停掉", "停了")):
            return "cancelled"
        if "暂停" in compact:
            return "paused"
        if any(marker in compact for marker in ("还在", "还会", "运行", "开启")):
            return "active"
        return ""

    def _match_watchlists_for_semantic(self, semantic_intent: dict[str, Any], *, event: VeyraEvent) -> list[dict[str, Any]]:
        entities = semantic_intent.get("entities") if isinstance(semantic_intent.get("entities"), dict) else {}
        topic = str(entities.get("topic") or "").strip()
        if not topic:
            return []
        external = self.state_store.read_json("external_world.json")
        watchlists = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        matched: list[dict[str, Any]] = []
        for item in watchlists:
            if not isinstance(item, dict):
                continue
            commitment_id = str(item.get("commitment_id") or "")
            owner_ok = True
            if commitment_id:
                commitment = self.get_commitment(commitment_id)
                owner_ok = not commitment or commitment.get("user_id") == event.source.user_id
            if owner_ok and self._normalize_match_text(topic) in self._normalize_match_text(str(item.get("topic") or item.get("query") or "")):
                matched.append(item)
        return matched

    def _semantic_target_label(self, topic: str, item: dict[str, Any]) -> str:
        if topic:
            if item.get("kind") == "external_digest":
                return f"{topic} 相关内容追踪"
            if item.get("kind") == "weather_daily":
                return f"{topic} 天气推送"
            return topic
        title = str(item.get("title") or item.get("kind") or "这个主动任务").strip()
        return title

    def _status_summary(self, items: list[dict[str, Any]]) -> str:
        parts = []
        for item in items[:5]:
            parts.append(f"{self._commitment_label(item)}：{self._status_label(str(item.get('status') or ''))}")
        return "；".join(parts) or "无匹配记录"

    def _watchlist_status_summary(self, items: list[dict[str, Any]]) -> str:
        parts = []
        for item in items[:5]:
            topic = str(item.get("topic") or item.get("query") or "追踪")
            parts.append(f"{topic}：{self._status_label(str(item.get('status') or ''))}")
        return "；".join(parts) or "无匹配记录"

    def _status_label(self, status: str) -> str:
        return {
            "active": "运行中",
            "pending_confirmation": "待确认",
            "paused": "已暂停",
            "cancelled": "已取消",
        }.get(status, status or "未知")

    def _candidate_summary(self, candidates: list[dict[str, Any]]) -> str:
        if not candidates:
            return "没有可操作任务"
        return "；".join(f"{self._commitment_label(item)}（{self._status_label(str(item.get('status') or ''))}）" for item in candidates[:5])

    def _commitment_label(self, item: dict[str, Any]) -> str:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("kind") == "external_digest":
            return f"外部追踪：{payload.get('topic') or item.get('title') or '未命名'}"
        if item.get("kind") == "weather_daily":
            return f"每日天气：{payload.get('location') or item.get('title') or '未命名'}"
        if item.get("kind") == "learning_digest":
            return f"学习资料：{payload.get('topic') or item.get('title') or '未命名'}"
        return str(item.get("title") or item.get("kind") or item.get("commitment_id") or "主动任务")

    def _compact_commitment(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        return {
            "commitment_id": item.get("commitment_id"),
            "kind": item.get("kind"),
            "status": item.get("status"),
            "title": item.get("title"),
            "topic": payload.get("topic"),
            "location": payload.get("location"),
            "watchlist_id": payload.get("watchlist_id"),
            "session_id": item.get("session_id"),
        }

    def _compact_watchlist(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "watchlist_id": item.get("watchlist_id"),
            "status": item.get("status"),
            "enabled": item.get("enabled"),
            "topic": item.get("topic"),
            "query": item.get("query"),
            "commitment_id": item.get("commitment_id"),
        }

    def _action_word(self, action: str) -> str:
        return {"cancel": "取消", "pause": "暂停", "resume": "恢复"}.get(action, action)

    def _control_response(self, action: str, updated: list[dict[str, Any]]) -> str:
        if not updated:
            return "没有找到可更新的主动任务。"
        labels = "；".join(self._commitment_label(item) for item in updated[:5])
        return f"已{self._action_word(action)}：{labels}。"

    def _intent_from_semantic(self, semantic_intent: dict[str, Any], *, event: VeyraEvent, action: str) -> ProactiveIntent:
        entities = semantic_intent.get("entities") if isinstance(semantic_intent.get("entities"), dict) else {}
        return ProactiveIntent(
            user_id=event.source.user_id,
            session_id=event.source.session_id,
            channel_id=event.source.channel,
            raw_text=str(event.payload.get("text") or ""),
            intent_type={"cancel": "cancel_commitment", "pause": "pause_commitment", "resume": "resume_commitment"}.get(action, "unknown"),
            topic=str(entities.get("topic") or entities.get("location") or ""),
            entities=dict(entities),
            desired_outcome=f"{action}_matching_commitments",
            requires_user_authorization=False,
            authorization_status="granted",
            risk_level=RiskLevel.R1.value,
            confidence=float(semantic_intent.get("confidence") or 0.0),
            proposed_next_action=f"{action}_matching_commitments",
            source="commitment_semantic_guardrail",
        )

    def _intent_is_answer_only(self, intent: ProactiveIntent) -> bool:
        return str(intent.proposed_next_action or "") == "answer_only"

    def apply_control_intent(self, intent: ProactiveIntent, *, action: str) -> dict[str, Any]:
        statuses = {"cancel": ["active", "pending_confirmation"], "pause": ["active", "pending_confirmation"], "resume": ["paused"]}.get(action, [])
        matches = self.match_commitments(intent, statuses=statuses)
        if not matches:
            return {
                "status": "not_found",
                "actions": [f"{action}_matching_commitments"],
                "primary_response_override": "没有找到正在进行的相关推送。",
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
            "primary_response_override": f"已{action_word} {len(updated)} 个匹配的主动任务。",
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
        if scope == "all":
            return items
        if not topic:
            return []
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
        messages = self.followup_messages_for_turn(turn_result)
        if not messages:
            return response
        additions = [message for message in messages if message and message not in response]
        if not additions:
            return response
        return f"{response.rstrip()}\n\n" + "\n\n".join(additions)

    def followup_messages_for_turn(self, turn_result: dict[str, Any]) -> list[str]:
        messages: list[str] = []
        for key in ("primary_response_override", "response_override", "followup_offer"):
            message = str(turn_result.get(key) or "").strip()
            if message and message not in messages:
                messages.append(message)
        explicit = turn_result.get("followup_messages")
        if isinstance(explicit, list):
            for item in explicit:
                message = str(item or "").strip()
                if message and message not in messages:
                    messages.append(message)
        return messages

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
        if not self._mentions_push_intent(lowered):
            return None
        if route not in {"probe", "direct_answer"}:
            return None
        existing = [
            item
            for item in self.list_commitments(user_id=event.source.user_id, session_id=event.source.session_id)
            if item.get("kind") == "weather_daily" and item.get("status") in {"active", "pending_confirmation"}
        ]
        if existing:
            return None
        location = self._extract_location(user_text) or self._default_location_for_event(event)
        schedule = self._default_daily_schedule(user_text)
        validation = self._validate_weather_daily_payload(
            {"location": location, "topic": "weather"},
            schedule,
            require_explicit_time=True,
            source_text=user_text,
        )
        if not validation.get("valid"):
            return None
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
        memory_write = self.memory_bridge.write_patch(
            {
                "session_id": event.source.session_id,
                "memory_type": "learning_goal",
                "topic": topic,
                "summary": f"User is learning {topic}; current phase is getting_started.",
                "freshness": "fresh",
                "trust": "user_explicit_request",
                "confidence": 0.94,
            },
            provider="selected",
        )
        actions = ["recorded_learning_goal"]
        if digest:
            actions.append("offered_learning_digest" if digest.get("status") == "pending_confirmation" else "activated_learning_digest")
        return {
            "status": "goal_recorded",
            "goal": goal,
            "commitment": digest,
            "actions": actions,
            "memory_write": memory_write,
            "followup_messages": [self._learning_goal_response(goal, digest)],
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
        user_id = str(goal.get("user_id") or (commitment or {}).get("user_id") or "local-user")
        scoped = self._scoped_user_world(user_world, user_id)
        patch = {
            "current_goal": f"learning:{topic}"[:180],
            "goal_id": goal.get("goal_id"),
            "goal_kind": goal.get("kind"),
            "learning_topic": topic,
        }
        if commitment:
            patch["commitment_id"] = commitment.get("commitment_id")
            patch["commitment_kind"] = commitment.get("kind")
        scoped.update(patch)
        scoped["updated_at"] = utc_now_iso()
        if self._should_mirror_legacy_user(user_id):
            user_world.update(patch)
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

    def _is_learning_memory_question(self, text: str) -> bool:
        lowered = (text or "").lower()
        return ("记得" in text or "remember" in lowered) and any(marker in text for marker in ("在学什么", "学习什么", "学什么", "学习目标"))

    def heal_invalid_commitments(self) -> dict[str, Any]:
        state = self._read_state()
        commitments = state.get("commitments") if isinstance(state.get("commitments"), list) else []
        checked = 0
        invalid_items: list[dict[str, Any]] = []
        newly_invalid: list[dict[str, Any]] = []
        changed = False
        now = utc_now_iso()
        for item in commitments:
            if not isinstance(item, dict) or item.get("kind") != "weather_daily":
                continue
            if item.get("status") not in {"active", "pending_confirmation"}:
                continue
            checked += 1
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
            validation = self._validate_weather_daily_payload(payload, schedule, require_explicit_time=False)
            if validation.get("valid"):
                continue
            reason = str(validation.get("reason") or "invalid_weather_commitment")
            was_invalid = bool(item.get("invalid"))
            item["status"] = "paused"
            item["invalid"] = True
            item["invalid_reason"] = reason
            item["invalid_detected_at"] = item.get("invalid_detected_at") or now
            item["updated_at"] = now
            item["next_run_at"] = None
            invalid_items.append(item)
            if not was_invalid:
                newly_invalid.append(item)
            changed = True
        if changed:
            state["commitments"] = commitments
            state["updated_at"] = now
            self._write_state(state)
            for item in invalid_items:
                self.set_watchlists_for_commitment(item)
        return {
            "status": "success",
            "checked": checked,
            "invalid_count": len(invalid_items),
            "newly_invalid_count": len(newly_invalid),
            "invalid_commitments": invalid_items,
            "newly_invalid_commitments": newly_invalid,
        }

    def mark_invalid_notified(self, commitment_id: str) -> dict[str, Any] | None:
        return self._patch_commitment(commitment_id, {"invalid_notified_at": utc_now_iso()}, recompute_next_run=False)

    def _mark_invalid_commitment(self, commitment_id: str, reason: str) -> dict[str, Any] | None:
        return self._patch_commitment(
            commitment_id,
            {
                "status": "paused",
                "invalid": True,
                "invalid_reason": reason,
                "invalid_detected_at": utc_now_iso(),
                "next_run_at": None,
            },
            recompute_next_run=False,
        )

    def _is_weather_commitment_request(self, text: str, lowered: str) -> bool:
        return any(marker in lowered for marker in WEATHER_MARKERS) and any(marker in lowered for marker in DAILY_MARKERS + PUSH_MARKERS)

    def _weather_commitment_candidate(self, text: str, *, event: VeyraEvent) -> dict[str, Any]:
        location = self._normalize_weather_location(self._extract_location(text))
        return {
            "kind": "weather_daily",
            "status": "active" if self._is_explicit_push_authorization((text or "").lower()) else "pending_confirmation",
            "title": f"每日天气（{location or '待确认地点'}）",
            "user_id": event.source.user_id,
            "channel": event.source.channel,
            "session_id": event.source.session_id,
            "source_event_id": event.event_id,
            "schedule": self._default_daily_schedule(text),
            "payload": {"location": location, "topic": "weather"},
        }

    def _existing_weather_commitment(self, event: VeyraEvent, location: str) -> dict[str, Any] | None:
        normalized = self._normalize_weather_location(location)
        for item in self.list_commitments(user_id=event.source.user_id, session_id=event.source.session_id):
            if item.get("kind") != "weather_daily" or item.get("status") not in {"active", "pending_confirmation"}:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if self._normalize_weather_location(str(payload.get("location") or "")) == normalized:
                return item
        return None

    def _validate_weather_daily_payload(
        self,
        payload: dict[str, Any],
        schedule: dict[str, Any],
        *,
        require_explicit_time: bool,
        source_text: str = "",
    ) -> dict[str, Any]:
        location = self._normalize_weather_location(str(payload.get("location") or ""))
        if not location:
            return {"valid": False, "reason": "missing_location", "field": "location"}
        if self._invalid_weather_location(location):
            return {"valid": False, "reason": "invalid_location", "field": "location", "location": location}
        time_local = str(schedule.get("time_local") or "").strip()
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time_local):
            return {"valid": False, "reason": "missing_schedule_time", "field": "schedule_time"}
        if require_explicit_time and not self._has_explicit_schedule_time(source_text):
            return {"valid": False, "reason": "missing_schedule_time", "field": "schedule_time"}
        return {"valid": True, "location": location, "schedule_time": time_local}

    def _weather_parameters_needed(self, validation: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "needs_parameters",
            "actions": ["asked_for_weather_commitment_parameters"],
            "validation": validation,
            "followup_messages": ["可以，你要我每天几点发哪个城市/地区的天气？"],
        }

    def _normalize_weather_location(self, location: str) -> str:
        value = self._clean_location(location)
        value = re.sub(r"^[\u4e00-\u9fff]{2,8}(?:省|自治区|特别行政区)", "", value)
        value = value.strip(" 的？?！!，,。")
        if not value:
            return ""
        if "市" not in value and value.endswith(("区", "县")) and len(value) >= 5:
            district_len = 3 if value.endswith("区") else 2
            district = value[-district_len:]
            parent = value[:-district_len].strip()
            if len(parent) >= 2:
                return f"{parent}市{district}"
        return value

    def _invalid_weather_location(self, location: str) -> bool:
        value = (location or "").strip()
        if not value or value in {"默认地点", "待确认地点", "天气"}:
            return True
        if len(value) > 18:
            return True
        if any(marker in value for marker in INVALID_WEATHER_LOCATION_MARKERS):
            return True
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", value):
            return True
        return False

    def _has_explicit_schedule_time(self, text: str) -> bool:
        return has_explicit_schedule_time(text)

    def _chinese_hour(self, text: str) -> int | None:
        return parsed_chinese_hour(text)

    def _extract_from_user_text(self, text: str, *, event: VeyraEvent) -> dict[str, Any] | None:
        lowered = (text or "").lower()
        if not self._mentions_push_intent(lowered) and not any(marker in lowered for marker in LEARNING_MARKERS):
            return None

        if any(marker in lowered for marker in WEATHER_MARKERS) and any(marker in lowered for marker in DAILY_MARKERS + PUSH_MARKERS):
            location = self._extract_location(text)
            schedule = self._default_daily_schedule(text)
            return {
                "kind": "weather_daily",
                "status": "active" if self._is_explicit_push_authorization(lowered) else "pending_confirmation",
                "title": f"每日天气（{location or '默认地点'}）",
                "user_id": event.source.user_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "source_event_id": event.event_id,
                "schedule": schedule,
                "payload": {"location": self._normalize_weather_location(location), "topic": "weather"},
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

    def _confirm_pending_commitment(self, pending: dict[str, Any], *, user_text: str) -> dict[str, Any] | None:
        patch: dict[str, Any] = {
            "status": "active",
            "confirmed_at": utc_now_iso(),
        }
        schedule = self._confirmation_schedule(user_text=user_text, pending=pending)
        if schedule:
            patch["schedule"] = schedule
        if pending.get("kind") == "weather_daily":
            payload = pending.get("payload") if isinstance(pending.get("payload"), dict) else {}
            candidate_schedule = patch.get("schedule") if isinstance(patch.get("schedule"), dict) else pending.get("schedule") if isinstance(pending.get("schedule"), dict) else {}
            validation = self._validate_weather_daily_payload(payload, candidate_schedule, require_explicit_time=False)
            if not validation.get("valid"):
                self._mark_invalid_commitment(str(pending.get("commitment_id")), str(validation.get("reason") or "invalid_weather_commitment"))
                return None
        return self._patch_commitment(str(pending.get("commitment_id")), patch, recompute_next_run=True)

    def _confirmation_schedule(self, *, user_text: str, pending: dict[str, Any]) -> dict[str, Any] | None:
        text = user_text or ""
        lowered = text.lower()
        if any(marker in lowered for marker in ("每天", "每日", "daily", "every morning", "each day", "tomorrow")) or "明天" in text:
            return self._default_daily_schedule(text)
        if has_explicit_schedule_time(text):
            return self._default_daily_schedule(text)
        if pending.get("kind") == "learning_digest" and any(marker in lowered for marker in PUSH_MARKERS):
            return self._default_learning_schedule(text)
        return None

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
        user_id = str(commitment.get("user_id") or "local-user")
        scoped = self._scoped_user_world(user_world, user_id)
        patch = {
            "current_goal": goal,
            "commitment_id": commitment.get("commitment_id"),
            "commitment_kind": commitment.get("kind"),
        }
        scoped.update(patch)
        preferences = scoped.setdefault("preferences", {})
        if isinstance(preferences, dict) and payload.get("location"):
            preferences["default_location"] = payload.get("location")
        scoped["updated_at"] = utc_now_iso()
        if self._should_mirror_legacy_user(user_id):
            user_world.update(patch)
            legacy_preferences = user_world.setdefault("preferences", {})
            if isinstance(legacy_preferences, dict) and payload.get("location"):
                legacy_preferences["default_location"] = payload.get("location")
        user_world["updated_at"] = utc_now_iso()
        self.state_store.write_json("user_world.json", user_world)

    def _default_location_for_event(self, event: VeyraEvent) -> str:
        user_world = self.state_store.read_json("user_world.json")
        user_id = str(event.source.user_id or "local-user")
        profiles = user_world.get("profiles_by_user") if isinstance(user_world.get("profiles_by_user"), dict) else {}
        scoped = profiles.get(user_id) if isinstance(profiles.get(user_id), dict) else {}
        scoped_preferences = scoped.get("preferences") if isinstance(scoped.get("preferences"), dict) else {}
        location = str(scoped_preferences.get("default_location") or "").strip()
        if location:
            return location
        if self._should_mirror_legacy_user(user_id) or not profiles:
            preferences = user_world.get("preferences") if isinstance(user_world.get("preferences"), dict) else {}
            return str(preferences.get("default_location") or "").strip()
        return ""

    def _scoped_user_world(self, user_world: dict[str, Any], user_id: str) -> dict[str, Any]:
        profiles = user_world.setdefault("profiles_by_user", {})
        if not isinstance(profiles, dict):
            profiles = {}
            user_world["profiles_by_user"] = profiles
        scoped = profiles.setdefault(user_id, {})
        if not isinstance(scoped, dict):
            scoped = {}
            profiles[user_id] = scoped
        return scoped

    def _should_mirror_legacy_user(self, user_id: str) -> bool:
        return user_id in {"", "local-user"}

    def _normalize_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        kind = str(schedule.get("kind") or "daily")
        timezone_name = str(schedule.get("timezone") or "Asia/Shanghai")
        normalized = {
            "kind": kind,
            "time_local": str(schedule.get("time_local") or "08:00"),
            "timezone": timezone_name,
            "interval_seconds": max(3600.0, float(schedule.get("interval_seconds") or 86400.0)),
        }
        for key in ("relative_day", "date", "end_date", "end_at"):
            if schedule.get(key):
                normalized[key] = str(schedule.get(key))
        return normalized

    def _default_daily_schedule(self, text: str) -> dict[str, Any]:
        return self._normalize_schedule(parse_schedule_text(text, default_kind="daily", default_time="08:00"))

    def _default_learning_schedule(self, text: str) -> dict[str, Any]:
        if any(marker in (text or "") for marker in ("早上", "早晨")) or "morning" in (text or "").lower():
            return self._normalize_schedule(parse_schedule_text(text, default_kind="daily", default_time="08:00"))
        if any(marker in (text or "").lower() for marker in ("每天", "每日", "daily")):
            return self._normalize_schedule(parse_schedule_text(text, default_kind="daily", default_time="09:00"))
        return self._normalize_schedule({"kind": "interval", "interval_seconds": 86400, "timezone": "Asia/Shanghai"})

    def _compute_next_run_at(self, schedule: dict[str, Any]) -> str:
        kind = str(schedule.get("kind") or "daily")
        now = datetime.now(timezone.utc)
        if self._schedule_has_ended(schedule, now=now):
            return ""
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
        if kind == "once":
            date_value = str(schedule.get("date") or "")
            if not date_value and str(schedule.get("relative_day") or "") == "tomorrow":
                date_value = (local_now + timedelta(days=1)).date().isoformat()
            if date_value:
                try:
                    year, month, day = [int(part) for part in date_value.split("-", 2)]
                    candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
                except (ValueError, IndexError):
                    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            else:
                candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= local_now:
                return ""
            return candidate.astimezone(timezone.utc).isoformat()
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate = candidate + timedelta(days=1)
        end_time = self._schedule_end_time(schedule)
        if end_time and candidate.astimezone(timezone.utc) > end_time:
            return ""
        return candidate.astimezone(timezone.utc).isoformat()

    def _schedule_has_ended(self, schedule: dict[str, Any], *, now: datetime) -> bool:
        end_time = self._schedule_end_time(schedule)
        return bool(end_time and now > end_time)

    def _schedule_end_time(self, schedule: dict[str, Any]) -> datetime | None:
        end_at = schedule.get("end_at")
        if not end_at and schedule.get("end_date"):
            try:
                end_at = end_of_local_day_iso(str(schedule.get("end_date")), timezone_name=str(schedule.get("timezone") or "Asia/Shanghai"))
            except (ValueError, TypeError):
                return None
        return self._parse_time(end_at)

    def _pause_ended_commitment(self, commitment: dict[str, Any], *, now: datetime) -> None:
        commitment_id = str(commitment.get("commitment_id") or "")
        if not commitment_id:
            return
        changed = self._patch_commitment(
            commitment_id,
            {
                "status": "paused",
                "ended_at": now.isoformat(),
                "end_reason": "schedule_end_reached",
                "next_run_at": None,
            },
            recompute_next_run=False,
        )
        if not changed:
            return
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "commitment_schedule",
                "status": "paused",
                "reason": "schedule_end_reached",
                "commitment_id": commitment_id,
                "kind": changed.get("kind"),
                "ended_at": now.isoformat(),
            },
        )

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
            r"(?:发|发送|推送|通知|告诉我|提醒我)\s*([^\s，,。.?！!]{2,24}?)(?:的)?(?:天气|气温)",
            r"(?:在|于)\s*([^\s，,。.?！!]{2,24}?)(?:的)?(?:天气|气温)",
            r"([^\s，,。.?！!]{2,24}?)(?:今天|现在|当前|明天|今日)?(?:的)?(?:天气|气温)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                location = self._normalize_weather_location(self._clean_location(match.group(1)))
                if location:
                    return location
        for token in ("北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"):
            if token in (text or ""):
                return token
        return ""

    def _clean_location(self, value: str) -> str:
        location = (value or "").strip(" 的？?！!，,。")
        for prefix in ("今天", "现在", "当前", "明天", "今日", "每天", "每日", "早上", "早晨", "上午", "晚上"):
            if location.startswith(prefix) and len(location) > len(prefix):
                location = location[len(prefix) :]
        location = re.sub(r"^(?:[一二两三四五六七八九十0-9]{1,3}点(?:钟)?|[0-2]?\d[:：]\d{2})", "", location)
        location = re.sub(r"^发", "", location)
        for suffix in ("今天", "现在", "当前", "明天", "今日", "天气信息", "天气", "气温", "的"):
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

    def _looks_like_plain_information_query(self, text: str, lowered: str) -> bool:
        """One-shot factual lookups should not spawn proactive commitments."""
        if self._mentions_push_intent(lowered):
            return False
        if any(marker in (text or "") for marker in TRACKING_REQUEST_MARKERS):
            return False
        if any(marker in lowered for marker in TRACKING_REQUEST_MARKERS_EN):
            return False
        lookup_markers = (
            "帮我找",
            "查一下",
            "查询",
            "是什么",
            "标题",
            "最新一期",
            "最新视频",
            "最新版本",
            "youtube",
            "bilibili",
            "github release",
            "release note",
        )
        if any(marker in lowered for marker in lookup_markers):
            return True
        if any(marker in (text or "") for marker in ("最新", "最近", "当前")) and any(
            marker in lowered for marker in ("新闻", "版本", "视频", "标题", "天气怎么样", "气温")
        ):
            return True
        return False

    def _looks_like_proactive_request(self, text: str, lowered: str) -> bool:
        if self._is_commitment_control_request(text, lowered):
            return True
        if self._mentions_push_intent(lowered):
            return True
        if any(marker in lowered for marker in LEARNING_MARKERS) and self._looks_like_learning_goal_request(text):
            return True
        if any(marker in (text or "") for marker in TRACKING_REQUEST_MARKERS):
            return True
        return any(marker in lowered for marker in TRACKING_REQUEST_MARKERS_EN)

    def _is_commitment_control_request(self, text: str, lowered: str) -> bool:
        if any(marker in lowered for marker in ("pause", "resume", "stop", "cancel")):
            return True
        return any(
            marker in (text or "")
            for marker in (
                "停止",
                "停掉",
                "取消",
                "以后都停止",
                "取消所有",
                "停止所有",
                "暂停",
                "先暂停",
                "最近先暂停",
                "暂时停",
                "先停一下",
                "恢复",
                "继续给我推",
                "继续推",
                "重新开启",
                "恢复推送",
            )
        )

    def _is_affirmation(self, lowered: str) -> bool:
        text = re.sub(r"[\s，,。.!！?？、]+", "", lowered or "")
        if not text:
            return False
        if any(marker in text for marker in ("先别订阅", "别订阅", "不要订阅", "不用订阅", "不要取消", "不用取消", "别取消")):
            return False
        if text in AFFIRM_EXACT_MARKERS:
            return True
        if len(text) <= 12 and any(marker in text for marker in AFFIRM_MARKERS):
            return True
        return any(phrase in text for phrase in AFFIRM_PHRASES)

    def _is_explicit_push_authorization(self, lowered: str) -> bool:
        text = lowered or ""
        has_push_action = any(marker in text for marker in PUSH_MARKERS + ("订阅", "subscribe")) or ("发" in text and "天气" in text)
        has_schedule_or_enable = any(marker in text for marker in DAILY_MARKERS + ("开启", "启用", "定时", "定期", "每天", "每日"))
        return has_push_action and has_schedule_or_enable

    def _is_decline(self, lowered: str) -> bool:
        text = re.sub(r"[\s，,。.!！?？、]+", "", lowered or "")
        if any(marker in text for marker in ("不要取消", "不用取消", "别取消", "先别订阅", "别订阅", "不要订阅", "不用订阅")):
            return False
        return any(marker in lowered for marker in DECLINE_MARKERS)
