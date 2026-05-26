from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.capability_registry import CapabilityRegistry
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

    def build(
        self,
        *,
        user_message: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        rule_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._scoped_state()
        relevant_claims = self.belief.relevant_claims(attention_focus, limit=10)
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
        return redact_sensitive(
            {
                "current_time": self._current_time(),
                "event": self._event_summary(event, payload_metadata),
                "conversation_tail": self._conversation_tail(event, limit=4),
                "input": {
                    "text": user_message,
                    "attachments": self._attachment_summary(payload_metadata),
                },
                "user_world": self._user_world_summary(state.get("user_world", {})),
                "attention": {
                    "focus": attention_focus,
                    "scope": state.get("attention_state", {}).get("context_scope", {}),
                },
                "belief": {
                    "summary": state.get("belief_state", {}).get("summary", {}),
                    "fresh_claims": fresh_claims[:10],
                    "stale_or_uncertain_claims": stale_claims[:6],
                    "uncertainty": self.uncertainty.uncertainty_summary(relevant_claims),
                },
                "runtime": {
                    "executor_state": self._executor_summary(state.get("executor_state", {})),
                    "task_state": self._task_summary(state.get("task_state", {})),
                    "risk_state": state.get("risk_state", {}),
                    "agent_config": self._agent_summary(state.get("agent_config", {})),
                },
                "persona": self._persona_summary(state.get("persona_state", {})),
                "rule_decision": rule_decision or {},
                "available_capabilities": self.capabilities.snapshot(),
            },
            max_string=1400,
            max_list=12,
        )

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
                    "text": item.get("text"),
                    "message_type": feishu.get("message_type") or metadata.get("message_type") or "text",
                    "has_attachment": bool(feishu.get("message_type") and feishu.get("message_type") != "text"),
                }
            )
        return tail[-limit:]

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
            "content_summary": feishu.get("content_text") or metadata.get("content_text") or "",
            "note": "Attachment bytes are not available to Core unless the channel adapter supplies OCR or vision text.",
        }

    def _claim_summary(self, claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": claim.get("key"),
            "claim": claim.get("claim"),
            "source": claim.get("source"),
            "status": claim.get("status"),
            "confidence": claim.get("confidence"),
            "age_seconds": claim.get("age_seconds"),
            "ttl_remaining_seconds": claim.get("ttl_remaining_seconds"),
            "next_action": claim.get("next_action"),
        }

    def _user_world_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            "preferences": value.get("preferences", {}),
            "current_goal": value.get("current_goal", ""),
            "focus": value.get("focus", []),
        }

    def _task_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        history = value.get("history") if isinstance(value.get("history"), list) else []
        short_term = value.get("short_term_memory") if isinstance(value.get("short_term_memory"), list) else []
        return {
            "current_task": value.get("current_task"),
            "recent_history": history[-3:],
            "short_term_memory": short_term[-5:],
            "pending_agent_tasks": value.get("pending_agent_tasks", [])[-3:] if isinstance(value.get("pending_agent_tasks"), list) else [],
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
