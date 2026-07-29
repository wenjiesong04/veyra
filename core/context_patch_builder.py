from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.context_scope import exact_owner_visible, visible_probe_map
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


class ContextPatchBuilder:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()

    def build(
        self,
        user_message: str,
        attention_focus: list[str],
        *,
        decision: dict[str, Any] | None = None,
        foresight: dict[str, Any] | None = None,
        event: VeyraEvent | None = None,
    ) -> dict[str, object]:
        state = self.state_store.read_all()
        user_id = str(event.source.user_id or "").strip() if event else ""
        session_id = str(event.source.session_id or "").strip() if event else ""
        relevant_claims = self.belief.relevant_claims(
            attention_focus,
            user_id=user_id,
            session_id=session_id,
        )
        fresh_claims = [claim for claim in relevant_claims if str(claim.get("status") or "fresh") in {"fresh", "conflict"}]
        stale_claims = [
            claim
            for claim in relevant_claims
            if str(claim.get("status") or "fresh") in {"stale", "expired"} or claim.get("next_action") == "refresh_probe"
        ]
        return {
            "user_goal": user_message,
            "attention_focus": attention_focus,
            "attachment_metadata": self._attachment_metadata(event),
            "relevant_world_state": self._relevant_world_state(
                state,
                attention_focus,
                user_id=user_id,
                session_id=session_id,
            ),
            "executor_state": redact_sensitive(self._state_meta(state["executor_state"], ["selected_agent", "status", "connected", "validation"])),
            "task_state": redact_sensitive(
                self._scoped_task_meta(
                    state["task_state"],
                    user_id=user_id,
                    session_id=session_id,
                )
            ),
            "belief_state": {
                "summary": state["belief_state"].get("summary", {}),
                "fresh_claims": fresh_claims[:20],
                "stale_claims": stale_claims[:12],
                "refresh_guidance": "stale beliefs require refresh_probe before execution" if stale_claims else None,
            },
            "uncertainty": self.uncertainty.uncertainty_summary(relevant_claims),
            "risk_state": self._state_meta(state["risk_state"], ["current_risk", "signals"]),
            "decision_trace": redact_sensitive(decision or {}),
            "foresight": redact_sensitive(foresight or {}),
        }

    def _relevant_world_state(
        self,
        state: dict[str, Any],
        focus: list[str],
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        local = state["local_world"] if isinstance(state.get("local_world"), dict) else {}
        external = state["external_world"] if isinstance(state.get("external_world"), dict) else {}
        user = state["user_world"] if isinstance(state.get("user_world"), dict) else {}
        probes = visible_probe_map(
            local,
            user_id=str(user_id or "").strip(),
            session_id=str(session_id or "").strip(),
        )
        selected_probes: dict[str, Any] = {}
        for key in self._probe_keys_for_focus(focus):
            if key in probes:
                selected_probes[key] = probes[key]
        return redact_sensitive(
            {
                "user": self._scoped_user_meta(user, user_id),
                "local": {
                    **self._state_meta(local, ["current_project", "last_probe_at"]),
                    "selected_probes": selected_probes,
                },
                "external": self._scoped_external_meta(
                    external,
                    user_id=str(user_id or "").strip(),
                    session_id=str(session_id or "").strip(),
                ),
            },
            max_string=900,
            max_list=12,
        )

    def _scoped_user_meta(self, user: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        meta = self._state_meta(user, [])
        if not user_id:
            return meta
        profiles = user.get("profiles_by_user") if isinstance(user.get("profiles_by_user"), dict) else {}
        scoped = profiles.get(user_id) if isinstance(profiles.get(user_id), dict) else {}
        if not scoped:
            return meta
        if isinstance(scoped.get("preferences"), dict):
            meta["preferences"] = scoped["preferences"]
        for field in ("current_goal", "current_project", "focus"):
            if scoped.get(field):
                meta[field] = scoped[field]
        if isinstance(scoped.get("profile"), dict) and scoped["profile"]:
            meta["profile"] = scoped["profile"]
        return meta

    def _scoped_external_meta(
        self,
        external: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        meta = self._state_meta(external, [])
        meta["watchlist"] = []
        meta["summaries"] = []
        if not user_id or not session_id or not isinstance(external, dict):
            return meta
        for field in ("watchlist", "summaries"):
            items = (
                external.get(field)
                if isinstance(external.get(field), list)
                else []
            )
            meta[field] = [
                item
                for item in items
                if exact_owner_visible(
                    item,
                    user_id=user_id,
                    session_id=session_id,
                )
            ][-12:]
        return meta

    def _scoped_task_meta(
        self,
        value: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        meta = self._state_meta(value, [])
        meta["current_task"] = None
        meta["short_term_memory"] = []
        if not user_id or not session_id or not isinstance(value, dict):
            return meta
        current_task = value.get("current_task")
        if self._owned_session_item(
            current_task,
            user_id=user_id,
            session_id=session_id,
        ):
            meta["current_task"] = current_task
        short_term = (
            value.get("short_term_memory")
            if isinstance(value.get("short_term_memory"), list)
            else []
        )
        meta["short_term_memory"] = [
            item
            for item in short_term
            if self._owned_session_item(
                item,
                user_id=user_id,
                session_id=session_id,
            )
            and not self._memory_expired(item)
        ]
        return meta

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

    def _state_meta(self, value: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        output = {
            "source": value.get("source"),
            "updated_at": value.get("updated_at"),
            "confidence": value.get("confidence"),
            "ttl_seconds": value.get("ttl_seconds"),
            "status": value.get("status"),
        }
        for field in fields:
            if field in value:
                if field == "short_term_memory" and isinstance(value[field], list):
                    output[field] = [
                        item
                        for item in value[field]
                        if not isinstance(item, dict) or not self._memory_expired(item)
                    ]
                else:
                    output[field] = value[field]
        return output

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

    def _probe_keys_for_focus(self, focus: list[str]) -> list[str]:
        mapping = {
            "current_time": ["time_probe"],
            "ports": ["port_probe"],
            "git_workspace": ["git_probe"],
            "openclaw_runtime": ["openclaw_probe", "port_probe"],
            "hermes_runtime": ["hermes_probe"],
            "logs": ["log_probe"],
            "codebase": ["git_probe", "file_probe"],
        }
        keys: list[str] = []
        for item in focus:
            keys.extend(mapping.get(str(item), []))
        return list(dict.fromkeys(keys))

    def _attachment_metadata(self, event: VeyraEvent | None) -> dict[str, Any]:
        if not event or not isinstance(event.payload, dict):
            return {"available": False}
        metadata = event.payload.get("metadata") if isinstance(event.payload.get("metadata"), dict) else {}
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        message_type = str(feishu.get("message_type") or metadata.get("message_type") or "text")
        if message_type == "text":
            return {"available": False, "message_type": "text"}
        return redact_sensitive(
            {
                "available": True,
                "message_type": message_type,
                "content_available_to_core": bool(feishu.get("content_text") or metadata.get("content_text")),
                "content_text": feishu.get("content_text") or metadata.get("content_text") or "",
                "keys": {
                    "image_key": feishu.get("image_key") or metadata.get("image_key"),
                    "file_key": feishu.get("file_key") or metadata.get("file_key"),
                    "media_key": feishu.get("media_key") or metadata.get("media_key"),
                    "attachment_id": feishu.get("attachment_id") or metadata.get("attachment_id"),
                    "message_id": feishu.get("message_id") or metadata.get("message_id"),
                },
                "attachment_fetch": {
                    "status": ((feishu.get("attachment_fetch") or {}).get("status") if isinstance(feishu.get("attachment_fetch"), dict) else None),
                    "content_type": ((feishu.get("attachment_fetch") or {}).get("content_type") if isinstance(feishu.get("attachment_fetch"), dict) else None),
                    "size_bytes": ((feishu.get("attachment_fetch") or {}).get("size_bytes") if isinstance(feishu.get("attachment_fetch"), dict) else None),
                    "local_path": ((feishu.get("attachment_fetch") or {}).get("local_path") if isinstance(feishu.get("attachment_fetch"), dict) else None),
                },
            },
            max_string=220,
            max_list=6,
        )
