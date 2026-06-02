from __future__ import annotations

from typing import Any

from core.proactive_intent import AUTHORIZATION_STATES
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class AuthorizationPolicy:
    """Authorization registry for proactive behavior."""

    STATE_FILE = "proactive_authorizations.json"

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def record_commitment(self, commitment: dict[str, Any]) -> dict[str, Any]:
        status = self._status_from_commitment(commitment)
        record = {
            "subject_type": "commitment",
            "subject_id": commitment.get("commitment_id"),
            "user_id": commitment.get("user_id"),
            "session_id": commitment.get("session_id"),
            "channel": commitment.get("channel"),
            "kind": commitment.get("kind"),
            "topic": self._topic(commitment),
            "authorization_status": status,
            "behaviors": self._behaviors(commitment),
            "updated_at": utc_now_iso(),
        }
        return self._upsert(record)

    def status_for_commitment(self, commitment: dict[str, Any]) -> str:
        subject_id = str(commitment.get("commitment_id") or "")
        for record in self._records():
            if isinstance(record, dict) and record.get("subject_type") == "commitment" and record.get("subject_id") == subject_id:
                status = str(record.get("authorization_status") or "none")
                return status if status in AUTHORIZATION_STATES else "none"
        return self._status_from_commitment(commitment)

    def allows_commitment(self, commitment: dict[str, Any], *, behavior: str = "push_delivery") -> tuple[bool, str]:
        status = self.status_for_commitment(commitment)
        if status == "granted":
            return True, "granted"
        if status in {"revoked", "denied", "paused"}:
            return False, status
        if commitment.get("status") == "active" and commitment.get("confirmed_at"):
            return True, "active_confirmed"
        return False, status or "not_authorized"

    def _upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        records = state.setdefault("authorizations", [])
        if not isinstance(records, list):
            records = []
        replaced = False
        for index, existing in enumerate(records):
            if not isinstance(existing, dict):
                continue
            if existing.get("subject_type") == record.get("subject_type") and existing.get("subject_id") == record.get("subject_id"):
                records[index] = {**existing, **record}
                record = records[index]
                replaced = True
                break
        if not replaced:
            record["created_at"] = utc_now_iso()
            records.append(record)
        state["authorizations"] = records[-300:]
        state["updated_at"] = utc_now_iso()
        self.state_store.write_json(self.STATE_FILE, state)
        return record

    def _records(self) -> list[Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        records = state.get("authorizations") if isinstance(state.get("authorizations"), list) else []
        return records

    def _status_from_commitment(self, commitment: dict[str, Any]) -> str:
        status = str(commitment.get("status") or "")
        if status == "active":
            return "granted" if commitment.get("confirmed_at") else "pending_confirmation"
        if status == "pending_confirmation":
            return "pending_confirmation"
        if status == "cancelled":
            return "revoked"
        if status == "paused":
            return "paused"
        return "none"

    def _behaviors(self, commitment: dict[str, Any]) -> list[str]:
        kind = str(commitment.get("kind") or "")
        behaviors = ["create_commitment", "push_delivery"]
        if kind in {"learning_digest", "external_digest"}:
            behaviors.extend(["external_refresh", "create_watchlist", "memory_write"])
        if kind == "local_probe_monitor":
            behaviors.append("local_probe_monitoring")
        return list(dict.fromkeys(behaviors))

    def _topic(self, commitment: dict[str, Any]) -> str:
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        return str(payload.get("topic") or payload.get("location") or payload.get("note") or commitment.get("title") or "")
