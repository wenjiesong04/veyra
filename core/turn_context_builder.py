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
    ) -> dict[str, Any]:
        state = self._scoped_state()
        relevant_claims = self.belief.relevant_claims(attention_focus, limit=4)
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
        persona = self._persona_summary(state.get("persona_state", {}))
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
                        session_id=event.source.session_id if event else None,
                    ),
                    "user": self._user_world_summary(
                        state.get("user_world", {}),
                        user_id=event.source.user_id if event else None,
                    ),
                    "task": self._task_summary(
                        state.get("task_state", {}),
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
        session_id = event.source.session_id
        tail: list[dict[str, Any]] = []
        for item in inbox:
            if not isinstance(item, dict) or item.get("session_id") != session_id:
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
            if not isinstance(item, dict) or item.get("session_id") != session_id:
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
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

    def _conversation_slots(self, task_state: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        if not session_id or not isinstance(task_state, dict):
            return {}
        slots_by_session = task_state.get("conversation_slots") if isinstance(task_state.get("conversation_slots"), dict) else {}
        slots = slots_by_session.get(session_id) if isinstance(slots_by_session, dict) else {}
        if not isinstance(slots, dict):
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
        if not isinstance(value, dict):
            return {}
        scoped: dict[str, Any] = {}
        profiles = value.get("profiles_by_user") if isinstance(value.get("profiles_by_user"), dict) else {}
        if user_id and isinstance(profiles.get(user_id), dict):
            scoped = profiles.get(user_id) or {}
        preferences = value.get("preferences", {}) if isinstance(value.get("preferences"), dict) else {}
        scoped_preferences = scoped.get("preferences") if isinstance(scoped.get("preferences"), dict) else {}
        profile = scoped.get("profile") if isinstance(scoped.get("profile"), dict) else value.get("profile", {})
        focus = scoped.get("focus") if isinstance(scoped.get("focus"), list) else value.get("focus", [])
        return {
            "preferences": self._limit_mapping({**preferences, **scoped_preferences}, 6),
            "profile": self._limit_mapping(profile, 6),
            "current_goal": self._clip(scoped.get("current_goal") or value.get("current_goal", ""), 180),
            "current_project": self._clip(scoped.get("current_project") or value.get("current_project", ""), 120),
            "focus": focus,
        }

    def _task_summary(self, value: Any, *, session_id: str | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        history = value.get("history") if isinstance(value.get("history"), list) else []
        short_term = value.get("short_term_memory") if isinstance(value.get("short_term_memory"), list) else []
        pending = value.get("pending_agent_tasks") if isinstance(value.get("pending_agent_tasks"), list) else []
        if session_id:
            history = [
                item
                for item in history
                if not isinstance(item, dict) or not item.get("session_id") or str(item.get("session_id")) == session_id
            ]
            short_term = [
                item
                for item in short_term
                if not isinstance(item, dict) or not item.get("session_id") or str(item.get("session_id")) == session_id
            ]
            pending = [
                item
                for item in pending
                if not isinstance(item, dict) or not item.get("session_id") or str(item.get("session_id")) == session_id
            ]
        return {
            "current_task": self._compact_task_item(value.get("current_task")),
            "recent_history": [self._compact_task_item(item) for item in history[-2:]],
            "short_term_memory": [self._compact_task_item(item) for item in short_term[-3:]],
            "pending_agent_tasks": [self._compact_task_item(item) for item in pending[-2:]],
        }

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

    def _persona_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            "active_modes": value.get("active_modes", []),
            "last_route": (value.get("last_binding") or {}).get("route") if isinstance(value.get("last_binding"), dict) else None,
            "response_style": (value.get("last_binding") or {}).get("response_style") if isinstance(value.get("last_binding"), dict) else None,
        }

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
            for key in ("route", "status", "task", "message", "result", "updated_at")
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
