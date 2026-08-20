"""Durable event admission ledger for cross-file Situation/Need writes."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.world_state import StateRevisionConflictError, WorldStateStore


class AdmissionLedgerError(RuntimeError):
    """An event cannot be admitted or safely replayed."""


class AdmissionRetentionError(AdmissionLedgerError):
    """An unknown event is older than the ledger's retained evidence floor."""


class LivingContextAdmissionLedger:
    STATE_FILE = "situation_admission_ledger.json"
    SCHEMA_VERSION = "veyra.situation_admission_ledger.v1"
    # The ledger is a progress record, not a transaction log pretending that
    # two independent JSON documents can roll back together.  Each boundary
    # is durable and monotonically advances so a retry can resume exactly
    # where a process stopped.
    PHASES = ("prepared", "semantic_applied", "needs_applied", "committed")
    _PHASE_ALIASES = {"repair": "prepared"}

    def __init__(self, state_store: WorldStateStore, *, max_entries: int = 2048) -> None:
        if max_entries < 8:
            raise ValueError("admission ledger capacity is too small")
        self.state_store = state_store
        self.max_entries = int(max_entries)

    def prepare(
        self,
        *,
        event_id: str,
        owner_id: str,
        session_id: str,
        situation_id: str,
        operation: str,
        semantic_digest: str,
        need_plan_digest: str,
        answered_need_generations: dict[str, int],
        event_timestamp: str,
        expected_situation_revision: int | None = None,
        expected_need_state_revision: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "event_id": str(event_id)[:240],
            "owner_id": str(owner_id)[:240],
            "session_id": str(session_id)[:240],
            "situation_id": str(situation_id)[:240],
            "operation": str(operation)[:32],
            "semantic_digest": str(semantic_digest),
            "need_plan_digest": str(need_plan_digest),
            "answered_need_generations": {str(k): int(v) for k, v in answered_need_generations.items()},
            "event_timestamp": str(event_timestamp)[:80],
            "expected_situation_revision": (
                None if expected_situation_revision is None else int(expected_situation_revision)
            ),
            "expected_need_state_revision": (
                None if expected_need_state_revision is None else int(expected_need_state_revision)
            ),
        }

        # A committed replay is a read-only observation.  Do this fast path
        # before ``mutate_json`` so even store-managed revision/timestamp
        # metadata and the ledger's committed row remain byte-pure.
        current = self.state_store.read_json(self.STATE_FILE)
        if current:
            self._validate_state(current)
            existing = current["entries"].get(payload["event_id"])
            if existing is not None and str(existing.get("phase") or "") == "committed":
                self._assert_same(existing, payload)
                result = copy.deepcopy(existing)
                result["replayed"] = True
                return result

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not state:
                path_for = getattr(self.state_store, "path_for", None)
                path = path_for(self.STATE_FILE) if callable(path_for) else None
                if path is not None and path.exists():
                    raise AdmissionLedgerError("admission ledger is empty or corrupt")
                state.update({
                    "schema_version": self.SCHEMA_VERSION,
                    "entries": {},
                    "entry_count": 0,
                    "retention_floor_at": None,
                    "updated_at": None,
                })
            self._validate_state(state)
            entries = state["entries"]
            existing = entries.get(payload["event_id"])
            if existing is not None:
                self._assert_same(existing, payload)
                if str(existing.get("phase") or "") == "committed":
                    # Exact replay is byte-pure.  The caller receives the
                    # replay marker below without mutating durable evidence.
                    return state
                else:
                    # Keep a previously completed phase.  A retry of an
                    # interrupted event must never make durable progress look
                    # as if it went backwards.
                    if str(existing.get("phase") or "") not in self.PHASES[1:]:
                        existing["phase"] = "prepared"
                    existing["updated_at"] = self._now()
                return state
            floor = state.get("retention_floor_at")
            if floor and self._parse_time(payload["event_timestamp"]) <= self._parse_time(str(floor)):
                raise AdmissionRetentionError("unknown event is older than the admission retention floor")
            self._compact(state)
            entries = state["entries"]
            if len(entries) >= self.max_entries:
                raise AdmissionLedgerError("admission ledger cannot safely retain another event")
            now = self._now()
            entries[payload["event_id"]] = {
                **payload,
                "phase": "prepared",
                "prepared_at": now,
                "updated_at": now,
            }
            state["entry_count"] = len(entries)
            state["updated_at"] = now
            return state

        state = self.state_store.mutate_json(self.STATE_FILE, mutate)
        result = copy.deepcopy(state["entries"][payload["event_id"]])
        if result.get("phase") == "committed":
            result["replayed"] = True
        return result

    def mark_phase(
        self,
        *,
        event_id: str,
        phase: str,
        semantic_digest: str,
        need_plan_digest: str,
        result_situation_revision: int | None = None,
        result_need_state_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist one monotonic cross-file admission boundary.

        This method intentionally records only exact, server-derived
        revisions and digests.  It does not attempt to undo a writer that has
        already succeeded; callers resume later stages using this evidence.
        """

        selected = self._PHASE_ALIASES.get(str(phase), str(phase))
        if selected not in self.PHASES[1:]:
            raise ValueError("admission phase is invalid")

        current = self.state_store.read_json(self.STATE_FILE)
        if not current:
            raise AdmissionLedgerError("cannot advance an unknown admission event")
        self._validate_state(current)
        existing = current["entries"].get(str(event_id))
        if not isinstance(existing, dict):
            raise AdmissionLedgerError("cannot advance an unknown admission event")
        self._assert_digest(existing, semantic_digest=semantic_digest, need_plan_digest=need_plan_digest)
        current_phase = self._canonical_phase(existing.get("phase"))
        if self._phase_rank(current_phase) > self._phase_rank(selected):
            # A committed/replayed phase is already authoritative.  Returning
            # it is both idempotent and byte-pure.
            return copy.deepcopy(existing)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            row = state["entries"].get(str(event_id))
            if not isinstance(row, dict):
                raise AdmissionLedgerError("cannot advance an unknown admission event")
            self._assert_digest(row, semantic_digest=semantic_digest, need_plan_digest=need_plan_digest)
            prior = self._canonical_phase(row.get("phase"))
            if self._phase_rank(prior) > self._phase_rank(selected):
                return state
            row["phase"] = selected
            if result_situation_revision is not None:
                row["result_situation_revision"] = int(result_situation_revision)
            if result_need_state_revision is not None:
                row["result_need_state_revision"] = int(result_need_state_revision)
            row["updated_at"] = self._now()
            if selected == "committed":
                row.setdefault("committed_at", row["updated_at"])
            state["updated_at"] = row["updated_at"]
            return state

        state = self.state_store.mutate_json(self.STATE_FILE, mutate)
        return copy.deepcopy(state["entries"][str(event_id)])

    def mark_committed(self, *, event_id: str, semantic_digest: str, need_plan_digest: str) -> dict[str, Any]:
        result = self.mark_phase(
            event_id=event_id,
            phase="committed",
            semantic_digest=semantic_digest,
            need_plan_digest=need_plan_digest,
        )
        return copy.deepcopy(result)

    def get(self, event_id: str) -> dict[str, Any] | None:
        state = self.state_store.read_json(self.STATE_FILE)
        if not state:
            return None
        self._validate_state(state)
        row = state["entries"].get(str(event_id))
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def _compact(self, state: dict[str, Any]) -> None:
        entries = state["entries"]
        if len(entries) < self.max_entries:
            return
        removable = sorted(
            ((key, row) for key, row in entries.items() if str(row.get("phase") or "") == "committed"),
            key=lambda pair: str(pair[1].get("committed_at") or pair[1].get("updated_at") or ""),
        )
        overflow = len(entries) - self.max_entries + 1
        if len(removable) < overflow:
            return
        evicted = [row for _, row in removable[:overflow]]
        for key, _ in removable[:overflow]:
            entries.pop(key, None)
        times = [str(row.get("event_timestamp") or "") for row in evicted if row.get("event_timestamp")]
        if times:
            newest_evicted = max(times, key=self._parse_time)
            prior_floor = state.get("retention_floor_at")
            if not prior_floor or self._parse_time(newest_evicted) > self._parse_time(str(prior_floor)):
                state["retention_floor_at"] = newest_evicted
        state["entry_count"] = len(entries)

    @staticmethod
    def _assert_same(existing: dict[str, Any], payload: dict[str, Any]) -> None:
        keys = (
            "owner_id",
            "session_id",
            "situation_id",
            "operation",
            "semantic_digest",
            "need_plan_digest",
            "answered_need_generations",
            "event_timestamp",
            "expected_situation_revision",
            "expected_need_state_revision",
        )
        for key in keys:
            if existing.get(key) != payload.get(key):
                raise StateRevisionConflictError("same event is bound to different admission content")

    def _validate_state(self, state: dict[str, Any]) -> None:
        if state.get("schema_version") != self.SCHEMA_VERSION or not isinstance(state.get("entries"), dict):
            raise AdmissionLedgerError("admission ledger is corrupt")
        if state.get("entry_count") != len(state["entries"]):
            raise AdmissionLedgerError("admission ledger count is inconsistent")
        if len(state["entries"]) > self.max_entries:
            raise AdmissionLedgerError("admission ledger exceeds bounded capacity")
        for key, row in state["entries"].items():
            if not isinstance(key, str) or not isinstance(row, dict) or key != row.get("event_id"):
                raise AdmissionLedgerError("admission ledger identity is invalid")
            if self._canonical_phase(row.get("phase")) not in self.PHASES:
                raise AdmissionLedgerError("admission ledger phase is invalid")
            for field in ("expected_situation_revision", "expected_need_state_revision", "result_situation_revision", "result_need_state_revision"):
                value = row.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise AdmissionLedgerError("admission ledger revision metadata is invalid")

    @classmethod
    def _canonical_phase(cls, value: Any) -> str:
        return cls._PHASE_ALIASES.get(str(value or ""), str(value or ""))

    @classmethod
    def _phase_rank(cls, value: Any) -> int:
        selected = cls._canonical_phase(value)
        try:
            return cls.PHASES.index(selected)
        except ValueError:
            raise AdmissionLedgerError("admission ledger phase is invalid")

    @classmethod
    def phase_rank(cls, value: Any) -> int:
        """Public monotonic phase rank used by admission coordinators."""

        return cls._phase_rank(value)

    @staticmethod
    def _assert_digest(existing: dict[str, Any], *, semantic_digest: str, need_plan_digest: str) -> None:
        if existing.get("semantic_digest") != semantic_digest or existing.get("need_plan_digest") != need_plan_digest:
            raise StateRevisionConflictError("admission digest changed before phase advance")

    @staticmethod
    def digest(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AdmissionLedgerError("admission timestamp must include timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
