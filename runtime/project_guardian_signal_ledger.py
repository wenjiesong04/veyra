from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore


class ProjectGuardianSignalLedger:
    """Bounded latest-state frontier for structured Guardian signals.

    EventInbox is a delivery queue and may evict completed records. Qualification
    therefore reads this compact frontier instead of treating queue retention as
    signal truth. One latest envelope is retained per Goal revision, release
    scope, user, and signal kind, so a newer clear permanently replaces its
    older positive within the bounded ledger.
    """

    STATE_FILE = "project_guardian_signal_state.json"
    MAX_RECORDS = 512
    MAX_RECORDS_PER_USER = 128

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def record_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepared(envelope)
        if prepared is None:
            return {"status": "ignored"}
        result = self._record_prepared([prepared])
        return {
            "status": str(
                (result.get("results") or {}).get(
                    prepared["event_id"],
                    "recorded",
                )
            ),
            "event_id": prepared["event_id"],
            "frontier_key": prepared["frontier_key"],
        }

    def reconcile_event_inbox(self) -> dict[str, Any]:
        self._require_healthy(
            self.state_store.read_json(self.STATE_FILE),
            self.STATE_FILE,
        )
        inbox = self.state_store.read_json("event_inbox.json")
        self._require_healthy(inbox, "event_inbox.json")
        events = inbox.get("events") if isinstance(inbox.get("events"), dict) else {}
        goals_state = self.state_store.read_json("user_goals.json")
        self._require_healthy(goals_state, "user_goals.json")
        prepared = [
            item
            for item in (
                self._prepared(
                    record.get("envelope")
                    if isinstance(record, dict)
                    else None,
                    goals_state=goals_state,
                )
                for record in events.values()
            )
            if item is not None
        ]
        result = self._record_prepared(prepared)
        statuses = list((result.get("results") or {}).values())
        return {
            "status": "success",
            "scanned_count": len(events),
            "accepted_signal_count": len(prepared),
            "recorded_count": statuses.count("recorded"),
            "stale_count": statuses.count("stale"),
        }

    def _record_prepared(
        self,
        prepared_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected: dict[str, str] = {}
        if not prepared_items:
            return {"results": selected}

        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, self.STATE_FILE)
            signals = (
                state.get("signals")
                if isinstance(state.get("signals"), dict)
                else {}
            )
            signals = {
                str(key): copy.deepcopy(value)
                for key, value in signals.items()
                if isinstance(value, dict)
            }
            for prepared in prepared_items:
                current = signals.get(prepared["frontier_key"])
                if (
                    not isinstance(current, dict)
                    or self._order(prepared["record"])
                    > self._order(current)
                ):
                    signals[prepared["frontier_key"]] = prepared["record"]
                    selected[prepared["event_id"]] = "recorded"
                else:
                    selected[prepared["event_id"]] = "stale"
            by_user: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            for key, record in signals.items():
                by_user.setdefault(self._user_id(record), []).append(
                    (key, record)
                )
            for records in by_user.values():
                if len(records) <= self.MAX_RECORDS_PER_USER:
                    continue
                records.sort(key=lambda item: self._order(item[1]))
                for key, _ in records[: -self.MAX_RECORDS_PER_USER]:
                    signals.pop(key, None)
            if len(signals) > self.MAX_RECORDS:
                ordered = sorted(
                    signals.items(),
                    key=lambda item: self._order(item[1]),
                )
                signals = dict(ordered[-self.MAX_RECORDS :])
            state["schema_version"] = (
                "veyra.project_guardian_signal_frontier.v1"
            )
            state["signals"] = signals
            state["signal_count"] = len(signals)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

        self.state_store.mutate_json(self.STATE_FILE, update)
        return {"results": selected}

    def evaluation_state(self) -> dict[str, Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        self._require_healthy(state, self.STATE_FILE)
        signals = state.get("signals") if isinstance(state.get("signals"), dict) else {}
        return {
            "schema_version": "veyra.project_guardian_signal_frontier.v1",
            "events": {
                str(key): copy.deepcopy(value)
                for key, value in signals.items()
                if isinstance(value, dict)
            },
        }

    def _prepared(
        self,
        envelope: Any,
        *,
        goals_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(envelope, dict):
            return None
        accepted, _ = ProjectGuardianEvaluator()._signals(
            {
                "events": {
                    "candidate": {
                        "status": "recorded",
                        "envelope": copy.deepcopy(envelope),
                    }
                }
            },
            datetime.now(timezone.utc),
        )
        if len(accepted) != 1:
            return None
        signal = accepted[0]
        selected_goals = (
            goals_state
            if isinstance(goals_state, dict)
            else self.state_store.read_json("user_goals.json")
        )
        self._require_healthy(selected_goals, "user_goals.json")
        if not self._matches_active_goal(signal, selected_goals):
            return None
        identity = {
            "user_id": str(signal["user_id"]),
            "goal_id": str(signal["goal_id"]),
            "goal_revision": str(signal["goal_revision"]),
            "scope": {
                key: str(signal["scope"].get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            },
            "kind": str(signal["kind"]),
        }
        event_id = str(envelope.get("event_id") or "")
        occurred_at = self._time(
            envelope.get("occurred_at") or envelope.get("timestamp")
        )
        if (
            not event_id
            or occurred_at is None
        ):
            return None
        canonical_envelope = {
            "type": "observation",
            "event_id": event_id,
            "timestamp": occurred_at.isoformat(),
            "occurred_at": occurred_at.isoformat(),
            "source": {
                "channel": ProjectGuardianEvaluator.SIGNAL_CHANNEL,
                "user_id": str(signal["user_id"]),
                "session_id": str(signal["session_id"]),
            },
            "payload": {
                "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
                "project_guardian_signal": {
                    "kind": str(signal["kind"]),
                    "state": str(signal["state"]),
                    "source_component": str(signal["source_component"]),
                    "provenance_root": str(signal["provenance_root"]),
                    "evidence_id": str(signal["evidence_id"]),
                    "goal_id": str(signal["goal_id"]),
                    "goal_revision": str(signal["goal_revision"]),
                    "scope": copy.deepcopy(signal["scope"]),
                    "valid_until": signal["valid_until"].isoformat(),
                },
            },
            "evidence_refs": [
                {
                    "ref_id": str(signal["evidence_id"]),
                    "source": str(signal["source_component"]),
                    "is_fact": True,
                }
            ],
            "privacy_scope": "user",
        }
        frontier_key = (
            "pgsf_"
            + hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        return {
            "event_id": event_id,
            "frontier_key": frontier_key,
            "record": {
                "status": "recorded",
                "envelope": canonical_envelope,
            },
        }

    @classmethod
    def _order(
        cls,
        record: dict[str, Any],
    ) -> tuple[datetime, int, str]:
        envelope = (
            record.get("envelope")
            if isinstance(record.get("envelope"), dict)
            else {}
        )
        payload = (
            envelope.get("payload")
            if isinstance(envelope.get("payload"), dict)
            else {}
        )
        signal = (
            payload.get("project_guardian_signal")
            if isinstance(payload.get("project_guardian_signal"), dict)
            else {}
        )
        occurred_at = cls._time(
            envelope.get("occurred_at") or envelope.get("timestamp")
        )
        return (
            occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            int(str(signal.get("state") or "") == "clear"),
            str(envelope.get("event_id") or ""),
        )

    @staticmethod
    def _user_id(record: dict[str, Any]) -> str:
        envelope = (
            record.get("envelope")
            if isinstance(record.get("envelope"), dict)
            else {}
        )
        source = (
            envelope.get("source")
            if isinstance(envelope.get("source"), dict)
            else {}
        )
        return str(source.get("user_id") or "")

    @classmethod
    def _matches_active_goal(
        cls,
        signal: dict[str, Any],
        goals_state: dict[str, Any],
    ) -> bool:
        goals = (
            goals_state.get("goals")
            if isinstance(goals_state.get("goals"), list)
            else []
        )
        occurred_at = signal.get("occurred_at")
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            scope = goal.get("scope") if isinstance(goal.get("scope"), dict) else {}
            active_from = cls._time(goal.get("active_from"))
            active_until = cls._time(goal.get("active_until"))
            if (
                str(goal.get("kind") or "")
                != ProjectGuardianEvaluator.GOAL_KIND
                or str(goal.get("status") or "") != "active"
                or str(goal.get("goal_id") or "")
                != str(signal.get("goal_id") or "")
                or str(goal.get("user_id") or "")
                != str(signal.get("user_id") or "")
                or str(goal.get("revision") or "")
                != str(signal.get("goal_revision") or "")
                or any(
                    str(scope.get(key) or "")
                    != str((signal.get("scope") or {}).get(key) or "")
                    for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                )
                or active_from is None
                or active_until is None
                or not isinstance(occurred_at, datetime)
                or not (active_from <= occurred_at <= active_until)
            ):
                continue
            return True
        return False

    @staticmethod
    def _require_healthy(state: dict[str, Any], name: str) -> None:
        if state.get("_state_corrupt"):
            raise RuntimeError(f"corrupt state: {name}")

    @staticmethod
    def _time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
