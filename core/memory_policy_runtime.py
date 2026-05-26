from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import Decision, LoopResult, VeyraEvent, utc_now_iso


MEMORY_POLICIES = {"forget", "short_term", "long_term"}


def normalize_memory_policy(value: Any, *, default: str = "forget") -> str:
    if isinstance(value, dict):
        value = value.get("mode") or value.get("policy") or default
    normalized = str(value or default).strip().lower()
    aliases = {
        "ignore": "forget",
        "none": "forget",
        "session": "short_term",
        "temporary": "short_term",
        "working": "short_term",
        "persistent": "long_term",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in MEMORY_POLICIES else default


class MemoryPolicyRuntime:
    """Applies per-turn memory policy after verification."""

    def __init__(self, state_store: WorldStateStore, long_term_writer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.state_store = state_store
        self.long_term_writer = long_term_writer

    def apply(self, event: VeyraEvent, decision: Decision, result: LoopResult) -> dict[str, Any]:
        policy = normalize_memory_policy(decision.memory_policy)
        if policy == "forget":
            return {"status": "skipped", "policy": policy, "reason": "turn marked forget"}
        if policy == "short_term":
            return self._write_short_term(event, decision, result)
        if result.status not in {"success", "verified_success", "partially_success"}:
            return {"status": "skipped", "policy": policy, "reason": f"result status {result.status} is not stable enough for long-term memory"}
        patch = {
            "session_id": event.source.session_id,
            "task": str(event.payload.get("text", ""))[:1200],
            "route": result.route.value,
            "status": result.status,
            "risk_level": result.risk_level.value,
            "result": redact_sensitive(result.response, max_string=1200),
            "freshness": "fresh",
            "trust": "verified",
            "memory_policy": policy,
        }
        if decision.intent == "preference" or "memory:preference" in decision.signals:
            patch.update(
                {
                    "memory_type": "user_preference",
                    "preference": {
                        "source_text": str(event.payload.get("text", ""))[:500],
                        "scope": "response_style",
                    },
                }
            )
        written = self.long_term_writer(patch)
        return {"status": written.get("status", "unknown"), "policy": policy, "write": written}

    def _write_short_term(self, event: VeyraEvent, decision: Decision, result: LoopResult) -> dict[str, Any]:
        state = self.state_store.read_json("task_state.json")
        short_term = state.setdefault("short_term_memory", [])
        if not isinstance(short_term, list):
            short_term = []
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        item = {
            "event_id": event.event_id,
            "session_id": event.source.session_id,
            "route": result.route.value,
            "status": result.status,
            "risk_level": result.risk_level.value,
            "intent": decision.intent,
            "summary": redact_sensitive(result.response, max_string=800),
            "created_at": utc_now_iso(),
            "expires_at": expires_at,
        }
        state["short_term_memory"] = (short_term + [item])[-50:]
        self.state_store.write_json("task_state.json", state)
        return {"status": "written", "policy": "short_term", "item": item}
