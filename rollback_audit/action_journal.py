from __future__ import annotations

from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


LOG_SOURCES: tuple[tuple[str, str], ...] = (
    ("event", "event_log.jsonl"),
    ("action", "action_record.jsonl"),
    ("execution", "execution_trace.jsonl"),
    ("tool", "tool_call_log.jsonl"),
    ("policy", "policy_trace.jsonl"),
    ("rollback", "rollback_log.jsonl"),
    ("memory", "memory_log.jsonl"),
    ("core_model", "core_model_trace.jsonl"),
)


class ActionJournal:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def record(self, payload: dict[str, Any]) -> None:
        self.state_store.append_jsonl("action_record.jsonl", payload)

    def timeline(
        self,
        *,
        limit: int = 100,
        event_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        items = self._read_items(limit=max(limit, 500))
        filtered = [
            item
            for item in items
            if self._matches(item, event_id=event_id, task_id=task_id, trace_id=trace_id)
        ]
        filtered = sorted(filtered, key=lambda item: item.get("timestamp") or "")[-limit:]
        return {
            "status": "success",
            "filters": {"event_id": event_id, "task_id": task_id, "trace_id": trace_id},
            "items": filtered,
            "summary": self._summary(filtered),
        }

    def time_travel(self, *, until: str | None = None, limit: int = 200) -> dict[str, Any]:
        items = self._read_items(limit=max(limit, 500))
        if until:
            items = [item for item in items if str(item.get("timestamp") or "") <= until]
        items = sorted(items, key=lambda item: item.get("timestamp") or "")[-limit:]
        return {
            "status": "success",
            "until": until or "latest",
            "summary": self._summary(items),
            "last_known": self._last_known(items),
            "items": items,
        }

    def find_by_trace(self, trace_id: str, limit: int = 200) -> list[dict[str, Any]]:
        direct = self.timeline(trace_id=trace_id, limit=limit)["items"]
        if direct:
            related = self._related_ids(direct)
            return self._with_related(related, limit=limit)
        return []

    def find_by_event(self, event_id: str, limit: int = 200) -> list[dict[str, Any]]:
        direct = self.timeline(event_id=event_id, limit=limit)["items"]
        related = self._related_ids(direct)
        return self._with_related(related, limit=limit) if direct else []

    def _read_items(self, limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for source, filename in LOG_SOURCES:
            rows = self.state_store.read_jsonl(filename, limit=limit)
            for index, row in enumerate(rows):
                items.append(self._normalize_row(source, filename, index, row))
        return items

    def _normalize_row(self, source: str, filename: str, index: int, row: dict[str, Any]) -> dict[str, Any]:
        raw = row if isinstance(row, dict) else {"raw": row}
        timestamp = str(
            raw.get("timestamp")
            or raw.get("recorded_at")
            or raw.get("created_at")
            or raw.get("updated_at")
            or raw.get("observed_at")
            or utc_now_iso()
        )
        event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
        execution = raw.get("execution_result") if isinstance(raw.get("execution_result"), dict) else {}
        verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
        rollback_result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
        normalized = {
            "journal_id": f"{source}:{raw.get('trace_id') or raw.get('review_id') or raw.get('snapshot_id') or raw.get('task_id') or index}",
            "source": source,
            "file": filename,
            "timestamp": timestamp,
            "event_id": raw.get("event_id") or event.get("event_id"),
            "task_id": raw.get("task_id") or execution.get("task_id") or rollback_result.get("task_id"),
            "trace_id": raw.get("trace_id") or execution.get("trace_id"),
            "review_id": raw.get("review_id") or raw.get("approved_by"),
            "snapshot_id": raw.get("snapshot_id") or snapshot.get("snapshot_id") or rollback_result.get("snapshot_id"),
            "route": raw.get("route"),
            "status": raw.get("status") or rollback_result.get("status") or verification.get("status"),
            "risk_level": raw.get("risk_level") or verification.get("risk_level"),
            "summary": self._row_summary(source, raw),
            "raw": redact_sensitive(raw, max_string=1800),
        }
        return normalized

    def _row_summary(self, source: str, raw: dict[str, Any]) -> str:
        if source == "event":
            event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            return str(payload.get("text") or raw.get("phase") or "event")
        if source == "execution":
            return f"{raw.get('route', 'execution')} {raw.get('status', 'unknown')} via {raw.get('executor', '-')}"
        if source == "tool":
            return f"{raw.get('tool', 'tool')} {raw.get('action_type', '-')}: {raw.get('status', 'unknown')}"
        if source == "policy":
            return f"{raw.get('tool', 'policy')} decision={raw.get('decision', raw.get('status', 'unknown'))}"
        if source == "rollback":
            return f"rollback {raw.get('action', '-')}"
        if source == "memory":
            return f"memory {raw.get('status', 'unknown')}"
        if source == "core_model":
            return f"core_model {raw.get('purpose', '-')}: {raw.get('status', 'unknown')}"
        return str(raw.get("status") or raw.get("route") or source)

    def _matches(self, item: dict[str, Any], *, event_id: str | None, task_id: str | None, trace_id: str | None) -> bool:
        if event_id and item.get("event_id") != event_id:
            return False
        if task_id and item.get("task_id") != task_id:
            return False
        if trace_id and item.get("trace_id") != trace_id:
            return False
        return True

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        by_source: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for item in items:
            source = str(item.get("source") or "unknown")
            status = str(item.get("status") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
            statuses[status] = statuses.get(status, 0) + 1
        return {"count": len(items), "by_source": by_source, "statuses": statuses}

    def _last_known(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        last_task = next((item for item in reversed(items) if item.get("task_id") or item.get("route")), None)
        last_risk = next((item for item in reversed(items) if item.get("risk_level")), None)
        last_execution = next((item for item in reversed(items) if item.get("source") == "execution"), None)
        return {"task": last_task, "risk": last_risk, "execution": last_execution}

    def _related_ids(self, items: list[dict[str, Any]]) -> dict[str, set[str]]:
        related = {"event_id": set(), "task_id": set(), "trace_id": set(), "review_id": set(), "snapshot_id": set()}
        for item in items:
            for key in related:
                value = item.get(key)
                if value:
                    related[key].add(str(value))
        return related

    def _with_related(self, related: dict[str, set[str]], limit: int) -> list[dict[str, Any]]:
        items = self._read_items(limit=max(limit, 500))
        matched = []
        for item in items:
            if any(item.get(key) and str(item[key]) in values for key, values in related.items()):
                matched.append(item)
        return sorted(matched, key=lambda item: item.get("timestamp") or "")[-limit:]
