from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore


class RetentionPolicy:
    DEFAULT_LIMITS = {
        "event_log.jsonl": 10000,
        "action_record.jsonl": 10000,
        "tool_call_log.jsonl": 10000,
        "policy_trace.jsonl": 10000,
        "execution_trace.jsonl": 10000,
        "rollback_log.jsonl": 10000,
        "memory_log.jsonl": 10000,
        "core_model_trace.jsonl": 10000,
        "alert_log.jsonl": 10000,
    }

    def __init__(self, state_store: WorldStateStore, limits: dict[str, int] | None = None) -> None:
        self.state_store = state_store
        self.limits = {**self.DEFAULT_LIMITS, **(limits or {})}

    def summary(self) -> dict[str, Any]:
        files = []
        for name, limit in self.limits.items():
            path = self.state_store.root / name
            count = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
            files.append(
                {
                    "file": name,
                    "entries": count,
                    "limit": limit,
                    "status": "over_limit" if count > limit else "ok",
                    "recommendation": "archive_then_truncate" if count > limit else "retain",
                }
            )
        return {"status": "ok", "policy": "append_only_with_archive_before_truncate", "files": files}
