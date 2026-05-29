from __future__ import annotations

from typing import Any

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
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
        relevant_claims = self.belief.relevant_claims(attention_focus)
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
            "relevant_world_state": self._relevant_world_state(state, attention_focus),
            "executor_state": redact_sensitive(self._state_meta(state["executor_state"], ["selected_agent", "status", "connected", "validation"])),
            "task_state": redact_sensitive(self._state_meta(state["task_state"], ["current_task", "short_term_memory"])),
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

    def _relevant_world_state(self, state: dict[str, Any], focus: list[str]) -> dict[str, Any]:
        local = state["local_world"] if isinstance(state.get("local_world"), dict) else {}
        external = state["external_world"] if isinstance(state.get("external_world"), dict) else {}
        user = state["user_world"] if isinstance(state.get("user_world"), dict) else {}
        probes = local.get("probes") if isinstance(local.get("probes"), dict) else {}
        selected_probes: dict[str, Any] = {}
        for key in self._probe_keys_for_focus(focus):
            if key in probes:
                selected_probes[key] = probes[key]
        return redact_sensitive(
            {
                "user": self._state_meta(user, ["current_goal", "preferences", "focus"]),
                "local": {
                    **self._state_meta(local, ["current_project", "last_probe_at"]),
                    "selected_probes": selected_probes,
                },
                "external": self._state_meta(external, ["watchlist", "summaries"]),
            },
            max_string=900,
            max_list=12,
        )

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
                output[field] = value[field]
        return output

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
