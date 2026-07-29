from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.capability_registry import CapabilityRegistry
from core.context_drift_detector import ContextDriftDetector
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


class TurnContextBuilder:
    """Builds a compact per-turn context packet for Veyra Core cognition."""

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()
        self.capabilities = CapabilityRegistry(state_store)
        self.drift_detector = ContextDriftDetector()

    def build(
        self,
        *,
        user_message: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        rule_decision: dict[str, Any] | None = None,
        persona_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._scoped_state()
        relevant_claims = self.belief.relevant_claims(
            attention_focus,
            limit=4,
            user_id=event.source.user_id if event else "",
            session_id=event.source.session_id if event else "",
        )
        fresh_claims = [
            self._claim_summary(claim)
            for claim in relevant_claims
            if str(claim.get("status") or "fresh") in {"fresh", "conflict"}
        ]
        stale_claims = [
            self._claim_summary(claim)
            for claim in relevant_claims
            if str(claim.get("status") or "fresh") in {"stale", "expired"} or claim.get("next_action")
        ]
        payload_metadata = event.payload.get("metadata") if event else {}
        risk_state = state.get("risk_state", {}) if isinstance(state.get("risk_state"), dict) else {}
        persona = self._persona_summary(state.get("persona_state", {}), persona_hint=persona_hint)
        context = redact_sensitive(
            {
                "active_context": {
                    "user_message": user_message,
                    "current_time": self._current_time(),
                    "event": self._event_summary(event, payload_metadata),
                    "attachments": self._attachment_summary(payload_metadata),
                    "attention_focus": attention_focus,
                    "persona": persona,
                    "risk": {
                        "current_risk": risk_state.get("current_risk"),
                        "signals": risk_state.get("signals", [])[:4] if isinstance(risk_state.get("signals"), list) else [],
                    },
                    "rule_decision": self._rule_decision_summary(rule_decision or {}),
                },
                "short_memory": {
                    "conversation_tail": self._conversation_tail(event, limit=6),
                    "conversation_slots": self._conversation_slots(
                        state.get("task_state", {}),
                        user_id=event.source.user_id if event else None,
                        session_id=event.source.session_id if event else None,
                    ),
                    "user": self._user_world_summary(
                        state.get("user_world", {}),
                        user_id=event.source.user_id if event else None,
                    ),
                    "task": self._task_summary(
                        state.get("task_state", {}),
                        user_id=event.source.user_id if event else None,
                        session_id=event.source.session_id if event else None,
                    ),
                },
                "belief": {
                    "summary": self._belief_summary(state.get("belief_state", {}).get("summary", {})),
                    "fresh_claims": fresh_claims[:1],
                    "uncertainty": self.uncertainty.uncertainty_summary(relevant_claims),
                },
                "stale_beliefs": self._stale_belief_summary(stale_claims),
                "runtime_summary": {
                    "executor": self._executor_summary(state.get("executor_state", {})),
                    "agent": self._agent_summary(state.get("agent_config", {})),
                },
                "available_capabilities": self._capability_summary(self.capabilities.snapshot()),
                "ignored_state": [
                    "state_schema",
                    "full_risk_catalog",
                    "full_channel_inbox",
                    "full_agent_memory",
                    "runtime_history",
                ],
            },
            max_string=420,
            max_list=6,
        )
        context, drift_report = self.drift_detector.apply(context, decision=rule_decision or {})
        context["_context_metrics"] = {
            "context_chars": drift_report["remediated_context_chars"],
            "estimated_tokens": drift_report["remediated_estimated_tokens"],
        }
        context["_context_drift"] = {
            "drift_score": drift_report["drift_score"],
            "drift_reasons": drift_report["drift_reasons"],
            "suggested_action": drift_report["suggested_action"],
            "warning": drift_report["warning"],
        }
        if drift_report["warning"]:
            self.state_store.append_jsonl(
                "context_drift_log.jsonl",
                {
                    "event_id": event.event_id if event else None,
                    "drift_score": drift_report["drift_score"],
                    "drift_reasons": drift_report["drift_reasons"],
                    "suggested_action": drift_report["suggested_action"],
                    "context_chars": drift_report["context_chars"],
                    "remediated_context_chars": drift_report["remediated_context_chars"],
                },
            )
        return context

    def _scoped_state(self) -> dict[str, Any]:
        return {
            "user_world": self.state_store.read_json("user_world.json"),
            "executor_state": self.state_store.read_json("executor_state.json"),
            "risk_state": self.state_store.read_json("risk_state.json"),
            "attention_state": self.state_store.read_json("attention_state.json"),
            "task_state": self.state_store.read_json("task_state.json"),
            "agent_config": self.state_store.read_json("agent_config.json"),
            "persona_state": self.state_store.read_json("persona_state.json"),
        }

    def _current_time(self) -> dict[str, Any]:
        local = datetime.now().astimezone()
        utc = datetime.now(timezone.utc)
        tz_name = self._local_timezone_name()
        return {
            "utc_iso": utc.isoformat(),
            "local_iso": local.isoformat(),
            "local_timezone": tz_name,
            "local_utc_offset": local.strftime("%z"),
            "epoch_seconds": int(time.time()),
        }

    def _local_timezone_name(self) -> str:
        env_tz = os.getenv("TZ")
        if env_tz:
            try:
                ZoneInfo(env_tz)
                return env_tz
            except ZoneInfoNotFoundError:
                return env_tz
        tzinfo = datetime.now().astimezone().tzinfo
        key = getattr(tzinfo, "key", None)
        return str(key or time.tzname[0] or "local")

    def _event_summary(self, event: VeyraEvent | None, metadata: Any) -> dict[str, Any]:
        if not event:
            return {}
        message_type = ""
        if isinstance(metadata, dict):
            feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
            message_type = str(feishu.get("message_type") or metadata.get("message_type") or "")
        return {
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "channel": event.source.channel,
            "user_id": event.source.user_id,
            "session_id": event.source.session_id,
            "message_type": message_type or "text",
        }

    def _conversation_tail(self, event: VeyraEvent | None, limit: int) -> list[dict[str, Any]]:
        if not event:
            return []
        state = self.state_store.read_json("channel_state.json")
        inbox = state.get("inbox") if isinstance(state.get("inbox"), list) else []
        outbox = state.get("outbox") if isinstance(state.get("outbox"), list) else []
        user_id = str(event.source.user_id or "").strip()
        session_id = event.source.session_id
        if not user_id or not session_id:
            return []
        owned_event_ids = {
            str(item.get("event_id"))
            for item in inbox
            if self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            )
            and item.get("event_id")
        }
        tail: list[dict[str, Any]] = []
        for item in inbox:
            if not self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            ):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
            tail.append(
                {
                    "direction": "inbound",
                    "received_at": item.get("received_at"),
                    "text": self._clip(item.get("text"), 220),
                    "message_type": feishu.get("message_type") or metadata.get("message_type") or "text",
                    "has_attachment": bool(feishu.get("message_type") and feishu.get("message_type") != "text"),
                }
            )
        for item in outbox:
            if not isinstance(item, dict) or str(item.get("session_id") or "") != session_id:
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            direct_user = str(item.get("user_id") or "").strip()
            metadata_user = str(metadata.get("user_id") or "").strip()
            direct_event_id = str(item.get("event_id") or "").strip()
            metadata_event_id = str(metadata.get("event_id") or "").strip()
            if direct_user and metadata_user and direct_user != metadata_user:
                continue
            if (
                direct_event_id
                and metadata_event_id
                and direct_event_id != metadata_event_id
            ):
                continue
            item_user = direct_user or metadata_user
            event_id = direct_event_id or metadata_event_id
            if item_user:
                if item_user != user_id:
                    continue
                if event_id and event_id not in owned_event_ids:
                    continue
            elif not event_id or event_id not in owned_event_ids:
                # Historical outbox records have no owner. They are visible
                # only when an exact owned inbound event proves the linkage.
                continue
            tail.append(
                {
                    "direction": "outbound",
                    "received_at": item.get("created_at"),
                    "text": self._clip(item.get("message"), 220),
                    "message_type": "text",
                    "route": metadata.get("route"),
                }
            )
        tail.sort(key=lambda item: str(item.get("received_at") or ""))
        return tail[-limit:]

    def _conversation_slots(
        self,
        task_state: dict[str, Any],
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        if not user_id or not session_id or not isinstance(task_state, dict):
            return {}
        slots_by_session = task_state.get("conversation_slots") if isinstance(task_state.get("conversation_slots"), dict) else {}
        slots = slots_by_session.get(session_id) if isinstance(slots_by_session, dict) else {}
        if not self._owned_session_item(
            slots,
            user_id=user_id,
            session_id=session_id,
        ):
            return {}
        tool = slots.get("last_tool_result") if isinstance(slots.get("last_tool_result"), dict) else {}
        compact_tool: dict[str, Any] = {
            "type": tool.get("type"),
            "status": tool.get("status"),
        }
        if tool.get("type") == "search":
            results = tool.get("results") if isinstance(tool.get("results"), list) else []
            compact_tool.update({"query": tool.get("query"), "result_count": len(results)})
        elif tool.get("type") == "weather":
            compact_tool.update({"location": tool.get("location"), "requested_location": tool.get("requested_location")})
        return {
            "last_location": self._clip(slots.get("last_location"), 80),
            "last_topic": self._clip(slots.get("last_topic"), 120),
            "last_search_query": self._clip(slots.get("last_search_query"), 120),
            "last_intent": self._clip(slots.get("last_intent"), 40),
            "last_tool_result": compact_tool,
        }

    def _attachment_summary(self, metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {"available": False}
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        message_type = str(feishu.get("message_type") or metadata.get("message_type") or "text")
        if message_type == "text":
            return {"available": False, "message_type": "text"}
        return {
            "available": True,
            "message_type": message_type,
            "content_available_to_core": bool(feishu.get("content_text") or metadata.get("content_text")),
            "content_summary": self._clip(feishu.get("content_text") or metadata.get("content_text") or "", 220),
            "attachment_fetch_status": (
                (feishu.get("attachment_fetch") or {}).get("status")
                if isinstance(feishu.get("attachment_fetch"), dict)
                else None
            ),
            "attachment_local_path": (
                (feishu.get("attachment_fetch") or {}).get("local_path")
                if isinstance(feishu.get("attachment_fetch"), dict)
                else None
            ),
        }

    def _claim_summary(self, claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": claim.get("key"),
            "claim": self._clip(claim.get("claim"), 160),
            "source": claim.get("source"),
            "status": claim.get("status"),
            "confidence": claim.get("confidence"),
            "ttl_remaining_seconds": claim.get("ttl_remaining_seconds"),
            "next_action": claim.get("next_action"),
        }

    def _stale_belief_summary(self, stale_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not stale_claims:
            return []
        return [
            {
                "count": len(stale_claims),
                "keys": [claim.get("key") for claim in stale_claims[:3]],
                "suggested_action": "refresh_or_exclude_stale_beliefs",
            }
        ]

    def _user_world_summary(self, value: Any, *, user_id: str | None = None) -> dict[str, Any]:
        if not isinstance(value, dict) or not user_id:
            return {}
        scoped: dict[str, Any] = {}
        profiles = value.get("profiles_by_user") if isinstance(value.get("profiles_by_user"), dict) else {}
        if user_id and isinstance(profiles.get(user_id), dict):
            scoped = profiles.get(user_id) or {}
        if not scoped:
            return {}
        scoped_preferences = scoped.get("preferences") if isinstance(scoped.get("preferences"), dict) else {}
        profile = scoped.get("profile") if isinstance(scoped.get("profile"), dict) else {}
        focus = scoped.get("focus") if isinstance(scoped.get("focus"), list) else []
        return {
            "preferences": self._limit_mapping(scoped_preferences, 6),
            "profile": self._limit_mapping(profile, 6),
            "current_goal": self._clip(scoped.get("current_goal") or "", 180),
            "current_project": self._clip(scoped.get("current_project") or "", 120),
            "focus": focus,
        }

    def _task_summary(
        self,
        value: Any,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        history = value.get("history") if isinstance(value.get("history"), list) else []
        short_term = value.get("short_term_memory") if isinstance(value.get("short_term_memory"), list) else []
        pending = value.get("pending_agent_tasks") if isinstance(value.get("pending_agent_tasks"), list) else []
        if not user_id or not session_id:
            return {
                "current_task": None,
                "recent_history": [],
                "short_term_memory": [],
                "pending_agent_tasks": [],
            }
        history = [
            item
            for item in history
            if self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            )
        ]
        short_term = [
            item
            for item in short_term
            if self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            )
            and not self._memory_expired(item)
        ]
        pending = [
            item
            for item in pending
            if self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            )
        ]
        current_task = value.get("current_task")
        if not self._owned_session_item(
            current_task,
            user_id=user_id,
            session_id=session_id,
        ):
            current_task = None
        return {
            "current_task": self._compact_task_item(current_task),
            "recent_history": [self._compact_task_item(item) for item in history[-2:]],
            "short_term_memory": [self._compact_task_item(item) for item in short_term[-3:]],
            "pending_agent_tasks": [self._compact_task_item(item) for item in pending[-2:]],
        }

    @staticmethod
    def _owned_session_item(
        item: Any,
        *,
        user_id: str,
        session_id: str,
    ) -> bool:
        if not isinstance(item, dict):
            return False
        context = (
            item.get("task_context")
            if isinstance(item.get("task_context"), dict)
            else {}
        )
        direct_user = str(item.get("user_id") or "").strip()
        context_user = str(context.get("user_id") or "").strip()
        direct_session = str(item.get("session_id") or "").strip()
        context_session = str(context.get("session_id") or "").strip()
        if direct_user and context_user and direct_user != context_user:
            return False
        if direct_session and context_session and direct_session != context_session:
            return False
        item_user = direct_user or context_user
        item_session = direct_session or context_session
        return item_user == user_id and item_session == session_id

    def _memory_expired(self, item: dict[str, Any]) -> bool:
        value = str(item.get("expires_at") or "")
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)

    def _executor_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            "selected_agent": value.get("selected_agent"),
            "status": value.get("status"),
            "connected": value.get("connected"),
            "validation": value.get("validation") if isinstance(value.get("validation"), dict) else {},
        }

    def _agent_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        selected = str(value.get("selected_agent") or "")
        agents = value.get("agents") if isinstance(value.get("agents"), dict) else {}
        selected_config = agents.get(selected) if isinstance(agents.get(selected), dict) else {}
        core_model = value.get("core_model") if isinstance(value.get("core_model"), dict) else {}
        return {
            "selected_agent": selected,
            "selected_agent_kind": selected_config.get("kind"),
            "selected_agent_enabled": selected_config.get("enabled"),
            "selected_agent_base_url_configured": bool(selected_config.get("base_url")),
            "core_model_enabled": bool(core_model.get("enabled")),
            "core_model_decision_mode": core_model.get("decision_mode"),
        }

    def _persona_summary(self, value: Any, *, persona_hint: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            value = {}
        summary = {
            "active_modes": value.get("active_modes", []),
            "last_route": (value.get("last_binding") or {}).get("route") if isinstance(value.get("last_binding"), dict) else None,
            "response_style": (value.get("last_binding") or {}).get("response_style") if isinstance(value.get("last_binding"), dict) else None,
        }
        if isinstance(persona_hint, dict) and persona_hint:
            summary.update(
                {
                    "active_modes": persona_hint.get("mode") or summary.get("active_modes"),
                    "route": persona_hint.get("route"),
                    "response_style": persona_hint.get("response_style") or summary.get("response_style"),
                    "tool_policy": persona_hint.get("tool_policy") if isinstance(persona_hint.get("tool_policy"), dict) else {},
                    "token_budget": persona_hint.get("token_budget") if isinstance(persona_hint.get("token_budget"), dict) else {},
                    "context_budget_chars": persona_hint.get("context_budget_chars"),
                    "guidelines": persona_hint.get("guidelines", [])[:6] if isinstance(persona_hint.get("guidelines"), list) else [],
                }
            )
        return summary

    def _capability_summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
        compact: dict[str, bool] = {}
        always_keep = {
            "native_answer",
            "time_probe",
            "port_probe",
            "git_probe",
            "process_probe",
            "file_probe",
            "openclaw_probe",
            "guardian",
            "verifier",
            "rollback_audit",
            "web_search",
            "weather_probe",
            "vision",
            "selected_agent_runtime",
            "safe_shell",
            "safe_file_read",
            "safe_file_write",
            "safe_browser_open",
            "safe_api_request",
            "mcp_runtime",
            "mcp_config",
        }
        for name, payload in capabilities.items():
            if not isinstance(payload, dict):
                continue
            available = bool(payload.get("available"))
            namespace = str(payload.get("namespace") or "")
            if name not in always_keep and not (available and namespace == "agent"):
                continue
            compact[name] = available
        selected_agent = snapshot.get("selected_agent") if isinstance(snapshot.get("selected_agent"), dict) else {}
        return {
            "schema": snapshot.get("schema"),
            "capabilities": compact,
            "missing_capabilities": self._missing_capability_summary(snapshot.get("missing_capabilities", [])),
            "selected_agent": {
                "name": selected_agent.get("name"),
                "kind": selected_agent.get("kind"),
                "base_url_configured": selected_agent.get("base_url_configured"),
            },
        }

    def _missing_capability_summary(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, Any]] = []
        for item in value[:4]:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "capability": item.get("capability"),
                    "reason": self._clip(item.get("reason"), 120),
                    "native_status": ((item.get("native") or {}).get("status") if isinstance(item.get("native"), dict) else None),
                    "agent_status": ((item.get("agent") or {}).get("status") if isinstance(item.get("agent"), dict) else None),
                }
            )
        return output

    def _belief_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        keys = ("fresh", "stale", "expired", "conflict", "total", "refreshable")
        return {key: value.get(key) for key in keys if key in value}

    def _rule_decision_summary(self, value: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "route",
            "intent",
            "complexity",
            "risk_level",
            "freshness_required",
            "selected_probe",
            "memory_policy",
            "reasoning_mode",
        )
        summary = {key: value.get(key) for key in keys if key in value}
        required = value.get("required_capabilities")
        if isinstance(required, list):
            summary["required_capabilities"] = required[:6]
        return summary

    def _compact_task_item(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            key: self._clip(value.get(key), 180)
            for key in (
                "route",
                "status",
                "task",
                "message",
                "result",
                "summary",
                "intent",
                "session_id",
                "updated_at",
                "created_at",
                "expires_at",
            )
            if key in value
        }

    def _limit_mapping(self, value: Any, limit: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {str(key): self._clip(item, 220) for key, item in list(value.items())[:limit]}

    def _availability_flags(self, value: Any) -> dict[str, bool]:
        if not isinstance(value, dict):
            return {}
        flags: dict[str, bool] = {}
        for key, payload in value.items():
            if isinstance(payload, dict):
                flags[str(key)] = bool(payload.get("available"))
            else:
                flags[str(key)] = bool(payload)
        return flags

    def _clip(self, value: Any, limit: int) -> Any:
        if not isinstance(value, str):
            return value
        return value if len(value) <= limit else value[:limit] + "..."
