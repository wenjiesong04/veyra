"""Durable, owner-scoped InformationNeed lifecycle.

An InformationNeed describes what would improve a Situation's understanding.
This runtime admits typed candidates and keeps a small, auditable projection;
it never invokes a source, grants authority, or treats a candidate as fact.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from core.world_state import StateRevisionConflictError, WorldStateStore
from interface.living_context_contract import (
    CandidateNeed,
    InformationNeedRecord,
    INFORMATION_NEED_SOURCE_CLASSES,
    INFORMATION_NEED_SCHEMA_VERSION,
    ObservationRequirement,
    stable_evidence_target_digest,
)
from interface.living_source_payload import weather_target_digest


ALLOWED_SOURCE_CLASSES = frozenset(
    INFORMATION_NEED_SOURCE_CLASSES
)
ACTIVE_NEED_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
TERMINAL_NEED_STATUSES = frozenset({"resolved", "expired", "dismissed"})
MAX_EVENT_FINGERPRINTS_PER_NEED = 32
# ``event_fingerprints`` is a display/index projection and may be compacted.
# Replay evidence is the idempotency authority, so it has its own explicit
# bounded ledger.  Once this cap is reached a new event fails closed; silently
# evicting an old event would make a genuine replay look like a new generation.
MAX_REPLAY_EVIDENCE_PER_NEED = 128
MAX_RETENTION_EVENTS = 200


class InformationNeedAccessError(PermissionError):
    """Raised when a caller crosses an owner or session boundary."""


class InformationNeedStateError(ValueError):
    """Raised when the durable need document is not structurally trustworthy."""


class InformationNeedCapacityError(StateRevisionConflictError):
    """Raised when bounded retention cannot remove a terminal record safely."""


class InformationNeedEventConflict(StateRevisionConflictError):
    """Raised when one source event is rebound to different candidate content."""


class InformationNeedStaleReopen(StateRevisionConflictError):
    """Raised when a scheduler reopen CAS no longer matches its read snapshot."""


class InformationNeedRuntime:
    """Single writer for the bounded durable InformationNeed state."""

    STATE_FILE = "information_need_state.json"
    SCHEMA_VERSION = "veyra.information_need_state.v1"
    INDEX_SCHEMA_VERSION = 2

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        state_file: str = STATE_FILE,
        max_needs: int = 1000,
        max_needs_per_situation: int = 32,
        max_replay_evidence_per_need: int = MAX_REPLAY_EVIDENCE_PER_NEED,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_needs, bool) or not isinstance(max_needs, int) or max_needs < 1:
            raise ValueError("InformationNeed max_needs must be a positive integer")
        if isinstance(max_needs_per_situation, bool) or not isinstance(max_needs_per_situation, int) or max_needs_per_situation < 1:
            raise ValueError("InformationNeed max_needs_per_situation must be a positive integer")
        if isinstance(max_replay_evidence_per_need, bool) or not isinstance(max_replay_evidence_per_need, int) or max_replay_evidence_per_need < 1:
            raise ValueError("InformationNeed max_replay_evidence_per_need must be a positive integer")
        self.state_store = state_store
        self.state_file = str(state_file)
        self.max_needs = int(max_needs)
        self.max_needs_per_situation = min(int(max_needs_per_situation), self.max_needs)
        self.max_replay_evidence_per_need = min(int(max_replay_evidence_per_need), MAX_REPLAY_EVIDENCE_PER_NEED)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def upsert_for_situation(
        self,
        *,
        situation_id: str,
        owner_id: str,
        session_id: str,
        needs: Iterable[CandidateNeed | dict[str, Any]],
        source_event_id: str,
        expected_state_revision: int | None = None,
        unknown_bindings: Mapping[str, str | None] | None = None,
        evidence_targets: Iterable[Mapping[str, Any] | None] | None = None,
        expected_generation: int | None = None,
        expected_status: str | None = None,
        expected_record_digest: str | None = None,
    ) -> list[dict[str, Any]]:
        """Admit candidates and return current needs for the exact Situation scope.

        A repeated ``source_event_id`` is content-addressed.  An exact replay
        returns the prior record without changing its timestamp or generation;
        rebinding the event to different content fails closed.
        """

        selected_situation = self._required(situation_id, "situation_id")
        selected_owner = self._required(owner_id, "owner_id")
        selected_session = self._required(session_id, "session_id")
        selected_event = self._required(source_event_id, "source_event_id")
        if expected_generation is not None and (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise ValueError("InformationNeed expected_generation must be a positive integer")
        selected_expected_status = None if expected_status is None else self._required(expected_status, "expected_status")
        if expected_record_digest is not None:
            selected_expected_digest = str(expected_record_digest).strip()
            if len(selected_expected_digest) != 64 or any(ch not in "0123456789abcdef" for ch in selected_expected_digest):
                raise ValueError("InformationNeed expected_record_digest is invalid")
        else:
            selected_expected_digest = None
        parsed = self.validate_candidates(needs)
        selected_targets = self._validate_evidence_targets(evidence_targets, len(parsed))
        selected_bindings = self._validate_unknown_bindings(unknown_bindings)
        candidate_blocked = {str(item.blocked_judgment) for item in parsed}
        if set(selected_bindings) - candidate_blocked:
            raise ValueError("InformationNeed unknown binding is not attached to a candidate Need")
        now = self._now_iso()
        persisted: list[dict[str, Any]] = []

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted
            self._assert_state(state)
            self._prepare_indexes(state)
            if expected_state_revision is not None and (
                isinstance(expected_state_revision, bool)
                or not isinstance(expected_state_revision, int)
                or int(state.get("_state_revision") or 0) != expected_state_revision
            ):
                raise StateRevisionConflictError(
                    "InformationNeed state revision does not match expected_state_revision"
                )

            needs_map = self._needs_map(state)
            fingerprints = state["event_fingerprints"]
            replay_evidence = state.setdefault("event_replay_evidence", {})
            # A single event cannot carry two different versions of one need.
            candidate_rows = self._candidate_rows(
                situation_id=selected_situation,
                candidates=parsed,
                unknown_bindings=selected_bindings,
                evidence_targets=selected_targets,
                existing_needs=needs_map,
                typed_endpoint=evidence_targets is not None,
            )

            changed = False
            for need_id, (candidate, digest, unknown_binding, evidence_target) in candidate_rows.items():
                current = needs_map.get(need_id)
                event_map = fingerprints.setdefault(need_id, {})
                if current is not None:
                    self._assert_owner(current, selected_owner, selected_session)
                    self._assert_expected_reopen(
                        current,
                        expected_generation=expected_generation,
                        expected_status=selected_expected_status,
                        expected_record_digest=selected_expected_digest,
                    )
                    replay_map = replay_evidence.setdefault(need_id, {})
                    known_digest = replay_map.get(selected_event)
                    if known_digest is None and str(current.get("source_event_id") or "") == selected_event:
                        known_digest = self._record_replay_fingerprint(current)
                    if known_digest is not None:
                        if str(known_digest) != self._candidate_event_fingerprint(candidate, unknown_binding, evidence_target):
                            raise InformationNeedEventConflict(
                                "source_event_id is already bound to different InformationNeed content"
                            )
                        # Exact replay is byte-pure.  The bounded display index
                        # must not be repopulated here: replay evidence and
                        # presentation retention have intentionally separate
                        # lifecycles.
                        continue

                    self._assert_replay_write_capacity(replay_map, selected_event)

                    generation = int(current.get("generation") or 1)
                    status = str(current.get("status") or "open")
                    reopened_generation = False
                    if status in TERMINAL_NEED_STATUSES:
                        generation += 1
                        status = "open"
                        reopened_generation = True
                    record = self._record(
                        need_id=need_id,
                        situation_id=selected_situation,
                        owner_id=selected_owner,
                        session_id=selected_session,
                        candidate=candidate,
                        source_event_id=selected_event,
                        now=now,
                        generation=generation,
                        status=status,
                        current=current,
                        unknown_binding=unknown_binding,
                        # A scheduler reopen starts a new generation, but it
                        # must not erase the prior semantic binding/target.
                        reset_unknown_binding=False,
                        evidence_target=evidence_target,
                    )
                else:
                    if expected_generation is not None or selected_expected_status is not None or selected_expected_digest is not None:
                        raise InformationNeedStaleReopen(
                            "InformationNeed reopen target disappeared before the CAS write"
                        )
                    self._assert_replay_write_capacity(
                        replay_evidence.setdefault(need_id, {}),
                        selected_event,
                    )
                    record = self._record(
                        need_id=need_id,
                        situation_id=selected_situation,
                        owner_id=selected_owner,
                        session_id=selected_session,
                        candidate=candidate,
                        source_event_id=selected_event,
                        now=now,
                        generation=1,
                        status="open",
                        current=None,
                        unknown_binding=unknown_binding,
                        evidence_target=evidence_target,
                    )
                needs_map[need_id] = record
                event_map[selected_event] = digest
                replay_evidence.setdefault(need_id, {})[selected_event] = self._candidate_event_fingerprint(
                    candidate,
                    unknown_binding,
                    evidence_target,
                )
                self._trim_event_map(event_map)
                changed = True

            if changed:
                self._compact_needs(state, needs_map, now=now)
                state["needs"] = needs_map
                state["need_count"] = len(needs_map)
                state["updated_at"] = now
            # _prepare_indexes may have migrated a legacy document.  Keep the
            # referential index in the state even when no candidate changed.
            state["event_fingerprints"] = fingerprints
            state["event_replay_evidence"] = replay_evidence
            persisted = self._scope_rows(needs_map, selected_owner, selected_session, selected_situation)
            return state

        self.state_store.mutate_json(self.state_file, mutate)
        return persisted

    def preflight_upsert(
        self,
        *,
        situation_id: str,
        owner_id: str,
        session_id: str,
        needs: Iterable[CandidateNeed | dict[str, Any]],
        source_event_id: str,
        expected_state_revision: int | None = None,
        unknown_bindings: Mapping[str, str | None] | None = None,
        evidence_targets: Iterable[Mapping[str, Any] | None] | None = None,
    ) -> int:
        """Check one upsert against both bounded Need capacities.

        This is deliberately read-only.  Callers use it while holding the
        state-root writer fence, immediately before writing Situation truth,
        so a create cannot commit a Situation and only then discover that its
        Needs would overflow either the per-Situation or global cap.

        The returned state revision is the revision inspected by the check;
        the caller can pass it to ``upsert_for_situation`` as a second CAS
        guard.  No retention compaction or event-index update is persisted by
        this method.
        """

        selected_situation = self._required(situation_id, "situation_id")
        selected_owner = self._required(owner_id, "owner_id")
        selected_session = self._required(session_id, "session_id")
        selected_event = self._required(source_event_id, "source_event_id")
        parsed = self.validate_candidates(needs)
        selected_targets = self._validate_evidence_targets(evidence_targets, len(parsed))
        selected_bindings = self._validate_unknown_bindings(unknown_bindings)
        state = self.state_store.read_json(self.state_file)
        self._assert_state(state)
        self._prepare_indexes(state)
        current_revision = int(state.get("_state_revision") or 0)
        if expected_state_revision is not None and (
            isinstance(expected_state_revision, bool)
            or not isinstance(expected_state_revision, int)
            or current_revision != expected_state_revision
        ):
            raise StateRevisionConflictError(
                "InformationNeed state revision does not match expected_state_revision"
            )

        needs_map = self._needs_map(state)
        candidate_rows = self._candidate_rows(
            situation_id=selected_situation,
            candidates=parsed,
            unknown_bindings=selected_bindings,
            evidence_targets=selected_targets,
            existing_needs=needs_map,
            typed_endpoint=evidence_targets is not None,
        )
        fingerprints = state.get("event_fingerprints") or {}
        replay_evidence = state.get("event_replay_evidence") or {}
        for need_id, (candidate, digest, unknown_binding, evidence_target) in candidate_rows.items():
            current = needs_map.get(need_id)
            if current is None:
                self._assert_replay_write_capacity(
                    replay_evidence.get(need_id) or fingerprints.get(need_id) or {},
                    selected_event,
                )
                continue
            self._assert_owner(current, selected_owner, selected_session)
            event_map = replay_evidence.get(need_id) or fingerprints.get(need_id) or {}
            known_digest = event_map.get(selected_event)
            if known_digest is None and str(current.get("source_event_id") or "") == selected_event:
                known_digest = self._record_replay_fingerprint(current)
            if known_digest is not None and str(known_digest) != self._candidate_event_fingerprint(candidate, unknown_binding, evidence_target):
                raise InformationNeedEventConflict(
                    "source_event_id is already bound to different InformationNeed content"
                )
            if known_digest is None:
                self._assert_replay_write_capacity(event_map, selected_event)

        self._assert_projected_capacity(
            state,
            needs_map,
            candidate_rows,
            owner_id=selected_owner,
            session_id=selected_session,
            situation_id=selected_situation,
            now=self._now_iso(),
        )
        return current_revision

    def validate_candidates(self, needs: Iterable[CandidateNeed | dict[str, Any]]) -> list[CandidateNeed]:
        """Validate source classes before a caller mutates Situation truth."""

        if isinstance(needs, (str, bytes, dict)):
            raise ValueError("InformationNeed candidates must be an iterable of objects")
        return [self._parse_candidate(item) for item in list(needs)[:8]]

    @classmethod
    def _candidate_rows(
        cls,
        *,
        situation_id: str,
        candidates: Iterable[CandidateNeed],
        unknown_bindings: Mapping[str, str | None],
        evidence_targets: Iterable[Mapping[str, Any] | None] | None = None,
        existing_needs: Mapping[str, Mapping[str, Any]] | None = None,
        typed_endpoint: bool | None = None,
    ) -> dict[str, tuple[CandidateNeed, str, str | None, dict[str, Any] | None]]:
        """Normalize candidate identity once for preflight and commit."""

        rows: dict[str, tuple[CandidateNeed, str, str | None, dict[str, Any] | None]] = {}
        selected_typed_endpoint = evidence_targets is not None if typed_endpoint is None else bool(typed_endpoint)
        selected_targets = list(evidence_targets or [])
        for index, candidate in enumerate(candidates):
            raw_target = selected_targets[index] if index < len(selected_targets) else None
            evidence_target = copy.deepcopy(dict(raw_target)) if isinstance(raw_target, Mapping) else None
            target_digest = stable_evidence_target_digest(evidence_target)
            unknown_binding = unknown_bindings.get(candidate.blocked_judgment)
            need_identity_digest = cls.need_identity_digest_for_candidate(
                candidate,
                evidence_target,
            )
            need_id = cls.stable_need_id(
                situation_id=situation_id,
                evidence_target_digest=target_digest,
                need_identity_digest=need_identity_digest,
                unknown_binding_digest=cls._unknown_binding_digest(unknown_binding),
                typed_endpoint=selected_typed_endpoint,
                evidence_kind=candidate.evidence_kind,
                blocked_judgment=candidate.blocked_judgment,
                observation_mode=candidate.observation_mode,
            )
            # During schema migration, preserve the legacy ID when this typed
            # target is the sole legacy Need for the same Situation/source.
            if target_digest and existing_needs and need_id not in existing_needs:
                legacy = [
                    (str(key), row)
                    for key, row in existing_needs.items()
                    if str(row.get("situation_id") or "") == str(situation_id)
                    and str(row.get("evidence_kind") or "") == str(candidate.evidence_kind)
                    and str(row.get("observation_mode") or "once") == str(candidate.observation_mode)
                    and str(row.get("blocked_judgment") or "") == str(candidate.blocked_judgment)
                    and not str(row.get("evidence_target_digest") or "").strip()
                ]
                if len(legacy) == 1:
                    need_id = legacy[0][0]
            digest = cls._candidate_fingerprint(candidate, evidence_target)
            previous = rows.get(need_id)
            if previous is not None and previous[1] != digest:
                raise InformationNeedEventConflict(
                    "one source event contains conflicting InformationNeed content"
                )
            if previous is not None and previous[2] != unknown_binding:
                raise InformationNeedEventConflict(
                    "one source event contains conflicting InformationNeed unknown binding"
                )
            if previous is not None and previous[3] != evidence_target:
                raise InformationNeedEventConflict(
                    "one source event contains conflicting InformationNeed evidence target"
                )
            rows[need_id] = (candidate, digest, unknown_binding, evidence_target)
        return rows

    @staticmethod
    def _assert_capacity(
        projected: int,
        members: list[dict[str, Any]],
        cap: int,
        label: str,
    ) -> None:
        overflow = int(projected) - int(cap)
        if overflow <= 0:
            return
        terminal = sum(
            1
            for row in members
            if str(row.get("status") or "") in TERMINAL_NEED_STATUSES
        )
        if terminal < overflow:
            raise InformationNeedCapacityError(
                f"InformationNeed {label} capacity cannot be admitted before Situation write"
            )

    def list(
        self,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_owner = self._required(owner_id, "owner_id")
        selected_session = self._required(session_id, "session_id")
        selected_statuses = {str(item) for item in (statuses or [])}
        state = self.state_store.read_json(self.state_file)
        self._assert_state(state)
        rows = [
            copy.deepcopy(item)
            for item in self._needs_map(state).values()
            if str(item["owner_id"]) == selected_owner
            and str(item["session_id"]) == selected_session
            and (situation_id is None or str(item["situation_id"]) == str(situation_id))
            and (not selected_statuses or str(item["status"]) in selected_statuses)
        ]
        rows.sort(key=lambda item: str(item["updated_at"]), reverse=True)
        return rows[: max(0, int(limit))]

    def get(self, need_id: str, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        selected = self._required(need_id, "need_id")
        for item in self.list(owner_id=owner_id, session_id=session_id, limit=self.max_needs):
            if str(item["need_id"]) == selected:
                return item
        return None

    def authoritative_projection(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Return a pure, server-owned source-resolver projection."""

        row = self.get(need_id, owner_id=owner_id, session_id=session_id)
        if row is None:
            return None
        return {
            "owner_id": str(row["owner_id"]),
            "session_id": str(row["session_id"]),
            "need_id": str(row["need_id"]),
            "situation_id": str(row["situation_id"]),
            "status": str(row["status"]),
            "generation": int(row["generation"]),
            "record_digest": self.record_digest_for_row(row),
            "allowed_source_classes": list(row.get("allowed_source_classes") or []),
            "evidence_kind": str(row.get("evidence_kind") or "other"),
            "observation_mode": str(row.get("observation_mode") or "once"),
            "observation_requirement": copy.deepcopy(row.get("observation_requirement")),
            "evidence_target": copy.deepcopy(row.get("evidence_target")),
            "evidence_target_digest": row.get("evidence_target_digest"),
            "provider_target_digest": row.get("provider_target_digest"),
            "need_identity_digest": row.get("need_identity_digest"),
            "blocked_judgment": str(row.get("blocked_judgment") or ""),
            "expires_at": row.get("expires_at"),
        }

    def authoritative_projections(
        self,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded current Need projections for a source resolver."""

        rows = self.list(
            owner_id=owner_id,
            session_id=session_id,
            situation_id=situation_id,
            limit=limit,
        )
        return [
            projection
            for row in rows
            for projection in [
                self.authoritative_projection(
                    str(row["need_id"]),
                    owner_id=owner_id,
                    session_id=session_id,
                )
            ]
            if projection is not None
        ]

    def resolve(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
        answered_by_event_id: str,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            need_id,
            owner_id=owner_id,
            session_id=session_id,
            status="resolved",
            answered_by_event_id=answered_by_event_id,
            expected_generation=expected_generation,
        )

    def dismiss(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
        answered_by_event_id: str,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            need_id,
            owner_id=owner_id,
            session_id=session_id,
            status="dismissed",
            answered_by_event_id=answered_by_event_id,
            expected_generation=expected_generation,
        )

    def mark_waiting(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
        event_id: str,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Public CAS seam for a Product/source policy to defer one Need."""

        return self._transition(
            need_id,
            owner_id=owner_id,
            session_id=session_id,
            status="waiting",
            answered_by_event_id=event_id,
            expected_generation=expected_generation,
        )

    def mark_asked(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
        event_id: str,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            need_id,
            owner_id=owner_id,
            session_id=session_id,
            status="asked",
            answered_by_event_id=event_id,
            expected_generation=expected_generation,
        )

    @staticmethod
    def need_identity_digest_for_candidate(
        candidate: CandidateNeed,
        evidence_target: Mapping[str, Any] | None,
    ) -> str:
        """Digest provider target and Need-only requirement separately."""

        provider_target = None
        requirement = (
            candidate.observation_requirement.model_dump(mode="json")
            if candidate.observation_requirement is not None
            else None
        )
        if isinstance(evidence_target, Mapping):
            provider_target = {
                str(key): copy.deepcopy(value)
                for key, value in evidence_target.items()
                if str(key) != "observation_requirement"
            }
            if requirement is None and isinstance(evidence_target.get("observation_requirement"), Mapping):
                requirement = copy.deepcopy(evidence_target["observation_requirement"])
        return InformationNeedRuntime._fingerprint_payload(
            {
                "evidence_kind": candidate.evidence_kind,
                "observation_mode": candidate.observation_mode,
                "provider_target": provider_target,
                "observation_requirement": requirement,
            }
        )

    @staticmethod
    def stable_need_id(
        *,
        situation_id: str,
        blocked_judgment: str = "",
        evidence_kind: str = "",
        evidence_target_digest: str | None = None,
        unknown_binding_digest: str | None = None,
        need_identity_digest: str | None = None,
        typed_endpoint: bool = False,
        observation_mode: str = "once",
    ) -> str:
        # A typed source target is the primary durable endpoint.  Where no
        # source target is safe to derive, a server-owned Unknown binding is
        # the only other semantic endpoint.  Display prose never identifies a
        # newly typed Need; it is retained solely for legacy rows without an
        # admitted endpoint.
        identity = (
            {
                "situation_id": str(situation_id),
                "need_identity_digest": str(need_identity_digest),
            }
            if need_identity_digest
            else {
                "situation_id": str(situation_id),
                "evidence_target_digest": str(evidence_target_digest),
                "evidence_kind": str(evidence_kind),
                "observation_mode": str(observation_mode or "once"),
            }
            if evidence_target_digest
            else {
                "situation_id": str(situation_id),
                "unknown_binding_digest": str(unknown_binding_digest),
                "evidence_kind": str(evidence_kind),
                "observation_mode": str(observation_mode or "once"),
            }
            if unknown_binding_digest
            else {
                "situation_id": str(situation_id),
                "evidence_target_digest": None,
                "evidence_kind": str(evidence_kind),
                "observation_mode": str(observation_mode or "once"),
            }
            if typed_endpoint
            else {
                "situation_id": str(situation_id),
                "blocked_judgment": " ".join(str(blocked_judgment).split())[:480],
                "evidence_kind": str(evidence_kind),
                "observation_mode": str(observation_mode or "once"),
            }
        )
        payload = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"need_{hashlib.sha256(payload).hexdigest()[:24]}"

    def _transition(
        self,
        need_id: str,
        *,
        owner_id: str,
        session_id: str,
        status: str,
        answered_by_event_id: str,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        selected_need = self._required(need_id, "need_id")
        selected_owner = self._required(owner_id, "owner_id")
        selected_session = self._required(session_id, "session_id")
        selected_event = self._required(answered_by_event_id, "event_id")
        if status not in ACTIVE_NEED_STATUSES | TERMINAL_NEED_STATUSES:
            raise ValueError("unsupported InformationNeed status")
        now = self._now_iso()
        persisted: dict[str, Any] | None = None

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted
            self._assert_state(state)
            self._prepare_indexes(state)
            needs_map = self._needs_map(state)
            current = needs_map.get(selected_need)
            if current is None:
                raise KeyError(f"unknown InformationNeed: {selected_need}")
            self._assert_owner(current, selected_owner, selected_session)
            generation = int(current["generation"])
            if expected_generation is not None and generation != expected_generation:
                raise StateRevisionConflictError("InformationNeed generation does not match")
            if (
                str(current["status"]) in TERMINAL_NEED_STATUSES
                and status in ACTIVE_NEED_STATUSES
            ):
                raise StateRevisionConflictError(
                    "terminal InformationNeed cannot be restored by an active transition"
                )
            if str(current["status"]) == status:
                persisted = copy.deepcopy(current)
                return state
            updated = copy.deepcopy(current)
            updated["status"] = status
            if status == "resolved":
                updated["answered_by_event_id"] = selected_event
            updated["updated_at"] = now
            needs_map[selected_need] = updated
            state["needs"] = needs_map
            state["need_count"] = len(needs_map)
            state["updated_at"] = now
            persisted = copy.deepcopy(updated)
            return state

        self.state_store.mutate_json(self.state_file, mutate)
        if persisted is None:  # pragma: no cover - guarded by mutation above.
            raise RuntimeError("InformationNeed transition was not persisted")
        return persisted

    def _record(
        self,
        *,
        need_id: str,
        situation_id: str,
        owner_id: str,
        session_id: str,
        candidate: CandidateNeed,
        source_event_id: str,
        now: str,
        generation: int,
        status: str,
        current: dict[str, Any] | None,
        unknown_binding: str | None = None,
        reset_unknown_binding: bool = False,
        evidence_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_current_binding = (current or {}).get("unknown_binding")
        current_binding = (
            str(raw_current_binding)
            if raw_current_binding is not None and str(raw_current_binding)
            else None
        )
        selected_binding = (None if reset_unknown_binding else current_binding) or (
            str(unknown_binding)
            if unknown_binding is not None and str(unknown_binding)
            else None
        )
        selected_binding_digest = self._unknown_binding_digest(selected_binding)
        current_target = (current or {}).get("evidence_target")
        selected_target = (
            copy.deepcopy(dict(evidence_target))
            if isinstance(evidence_target, Mapping)
            else copy.deepcopy(current_target)
            if isinstance(current_target, Mapping)
            else None
        )
        selected_target_digest = stable_evidence_target_digest(selected_target)
        provider_target_digest = (
            weather_target_digest(
                selected_target.get("location"),
                selected_target.get("target_date"),
            )
            if isinstance(selected_target, Mapping)
            and str(candidate.evidence_kind) == "weather"
            else (current or {}).get("provider_target_digest")
        )
        need_identity_digest = self.need_identity_digest_for_candidate(
            candidate,
            selected_target,
        )
        # Scheduler/reopen candidates built against an older catalog may not
        # carry a newly derived target. Preserve the durable target and mode in
        # that case; a typed target supplied by the current candidate wins.
        raw_mode = (current or {}).get("observation_mode")
        current_mode = raw_mode if raw_mode in {"once", "watch"} else None
        candidate_mode = str(candidate.observation_mode)
        mode_explicit = "observation_mode" in getattr(candidate, "model_fields_set", set())
        selected_mode = candidate_mode if current is None or mode_explicit else (current_mode or candidate_mode)
        record = InformationNeedRecord(
            schema_version=INFORMATION_NEED_SCHEMA_VERSION,
            need_id=need_id,
            situation_id=situation_id,
            owner_id=owner_id,
            session_id=session_id,
            blocked_judgment=candidate.blocked_judgment,
            evidence_kind=candidate.evidence_kind,
            observation_mode=selected_mode,  # type: ignore[arg-type]
            observation_requirement=self._observation_requirement(candidate, selected_target),
            why_now=candidate.why_now,
            urgency=candidate.urgency,
            expires_at=candidate.expires_at,
            allowed_source_classes=list(candidate.allowed_source_classes),
            fallback_reaction=candidate.fallback_reaction,
            question=candidate.question,
            status=status,  # type: ignore[arg-type]
            generation=generation,
            source_event_id=source_event_id,
            answered_by_event_id=(current or {}).get("answered_by_event_id"),
            created_at=str((current or {}).get("created_at") or now),
            updated_at=now,
            unknown_binding=selected_binding,
            unknown_binding_digest=selected_binding_digest,
            evidence_target=selected_target,
            evidence_target_digest=selected_target_digest,
            provider_target_digest=provider_target_digest,
            need_identity_digest=need_identity_digest,
        )
        return record.model_dump(mode="json")

    def _parse_candidate(self, value: CandidateNeed | dict[str, Any]) -> CandidateNeed:
        candidate = value if isinstance(value, CandidateNeed) else CandidateNeed.model_validate(value, strict=True)
        invalid = set(candidate.allowed_source_classes) - ALLOWED_SOURCE_CLASSES
        if invalid:
            raise ValueError(f"unsupported InformationNeed source class: {sorted(invalid)!r}")
        return candidate

    @staticmethod
    def _observation_requirement(
        candidate: CandidateNeed,
        evidence_target: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return one canonical, typed requirement for the durable Need."""

        raw = evidence_target.get("observation_requirement") if isinstance(evidence_target, Mapping) else None
        if raw is None:
            raw = candidate.observation_requirement
        if raw is None:
            return None
        requirement = raw if isinstance(raw, ObservationRequirement) else ObservationRequirement.model_validate(raw, strict=True)
        return requirement.model_dump(mode="json")

    @classmethod
    def record_digest_for_row(cls, row: Mapping[str, Any]) -> str:
        """Return the CAS digest for one authoritative Need row."""

        digest_payload = {
            key: value
            for key, value in row.items()
            if key not in {"created_at", "updated_at"}
        }
        return cls._fingerprint_payload(digest_payload)

    @staticmethod
    def _assert_expected_reopen(
        current: Mapping[str, Any],
        *,
        expected_generation: int | None,
        expected_status: str | None,
        expected_record_digest: str | None,
    ) -> None:
        """Enforce the scheduler's exact read-snapshot CAS, if supplied."""

        if expected_generation is not None and int(current.get("generation") or 0) != expected_generation:
            raise InformationNeedStaleReopen(
                "InformationNeed reopen generation is stale"
            )
        if expected_status is not None and str(current.get("status") or "") != expected_status:
            raise InformationNeedStaleReopen(
                "InformationNeed reopen status is stale"
            )
        if expected_record_digest is not None and InformationNeedRuntime.record_digest_for_row(current) != expected_record_digest:
            raise InformationNeedStaleReopen(
                "InformationNeed reopen record digest is stale"
            )

    @classmethod
    def _validate_unknown_bindings(
        cls,
        bindings: Mapping[str, str | None] | None,
    ) -> dict[str, str | None]:
        if bindings is None:
            return {}
        if not isinstance(bindings, Mapping):
            raise ValueError("InformationNeed unknown_bindings must be an object")
        if len(bindings) > 8:
            raise ValueError("InformationNeed unknown_bindings exceed the bounded candidate limit")
        result: dict[str, str | None] = {}
        for raw_key, raw_value in bindings.items():
            key = str(raw_key or "").strip()
            if not key or len(key) > 480:
                raise ValueError("InformationNeed unknown binding key is invalid")
            if raw_value is None:
                result[key] = None
                continue
            value = str(raw_value)
            if not value or len(value) > 480:
                raise ValueError("InformationNeed unknown binding value is invalid")
            result[key] = value
        return result

    @staticmethod
    def _validate_evidence_targets(
        targets: Iterable[Mapping[str, Any] | None] | None,
        candidate_count: int,
    ) -> list[dict[str, Any] | None]:
        """Validate the bounded server-derived target sidecar.

        This sidecar is positional and never model-authored. Keeping it
        separate from ``CandidateNeed`` prevents arbitrary provider fields
        from becoming source arguments while allowing legacy callers to omit
        targets entirely.
        """

        if targets is None:
            return [None] * max(0, int(candidate_count))
        if isinstance(targets, (str, bytes, Mapping)):
            raise ValueError("InformationNeed evidence_targets must be an iterable of objects")
        selected = list(targets)
        if len(selected) > 8:
            raise ValueError("InformationNeed evidence_targets exceed the bounded candidate limit")
        if len(selected) != int(candidate_count):
            raise ValueError("InformationNeed evidence_targets must align with candidate Needs")
        result: list[dict[str, Any] | None] = []
        for target in selected:
            if target is None:
                result.append(None)
                continue
            if not isinstance(target, Mapping):
                raise ValueError("InformationNeed evidence target must be an object")
            if any(str(key) in {"blocked_judgment", "question"} for key in target):
                raise ValueError("InformationNeed evidence target cannot contain display-only Need wording")
            result.append(copy.deepcopy(dict(target)))
        return result

    @staticmethod
    def _unknown_binding_digest(value: str | None) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _assert_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict) or state.get("_state_corrupt"):
            raise InformationNeedStateError("InformationNeed state is corrupt")
        if not state:
            path = self._state_path()
            if path is not None and path.exists():
                raise InformationNeedStateError("InformationNeed state is empty")
            state.update(self._empty_state())
        if state.get("schema_version") != self.SCHEMA_VERSION:
            raise InformationNeedStateError("InformationNeed state schema is unsupported")
        raw = state.get("needs")
        if not isinstance(raw, dict):
            raise InformationNeedStateError("InformationNeed state needs index is invalid")
        count = state.get("need_count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(raw):
            raise InformationNeedStateError("InformationNeed need_count is inconsistent")
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict) or key != value.get("need_id"):
                raise InformationNeedStateError("InformationNeed row/index identity is invalid")
            try:
                record = InformationNeedRecord.model_validate(value, strict=True)
            except Exception as exc:
                raise InformationNeedStateError(f"InformationNeed row {key!r} is invalid") from exc
            if set(record.allowed_source_classes) - ALLOWED_SOURCE_CLASSES:
                raise InformationNeedStateError(f"InformationNeed row {key!r} has an unsupported source class")
            if record.unknown_binding is not None and self._unknown_binding_digest(record.unknown_binding) != record.unknown_binding_digest:
                raise InformationNeedStateError(f"InformationNeed row {key!r} unknown binding digest is invalid")
            if record.evidence_target is not None and stable_evidence_target_digest(record.evidence_target) != record.evidence_target_digest:
                raise InformationNeedStateError(f"InformationNeed row {key!r} evidence target digest is invalid")
        index_version = state.get("index_schema_version", 1)
        if isinstance(index_version, bool) or not isinstance(index_version, int) or not 1 <= index_version <= self.INDEX_SCHEMA_VERSION:
            raise InformationNeedStateError("InformationNeed index schema is unsupported")
        fingerprints_value = state.get("event_fingerprints")
        if not (fingerprints_value is None and index_version <= 1):
            self._validate_fingerprint_index(fingerprints_value, set(raw))
        self._validate_replay_index(state.get("event_replay_evidence"), set(raw))
        fingerprints = fingerprints_value
        if fingerprints is not None:
            for key, value in raw.items():
                actual = fingerprints[key].get(str(value["source_event_id"]))
                expected = self._record_fingerprint(value)
                if actual == expected:
                    continue
                # Pre-typed rows used exactly this original presentation
                # payload: no observation mode, requirement, or target. This
                # is a read-only compatibility seam; a writer transaction
                # migrates the index to ``_record_fingerprint``.
                if actual != self._pre_typed_legacy_record_fingerprint(value):
                    raise InformationNeedStateError("InformationNeed current event fingerprint does not match its row")
        self._validate_retention(state)

    def _prepare_indexes(self, state: dict[str, Any]) -> None:
        fingerprints = state.setdefault("event_fingerprints", {})
        if not isinstance(fingerprints, dict):
            raise InformationNeedStateError("InformationNeed event_fingerprints is invalid")
        replay_evidence = state.setdefault("event_replay_evidence", {})
        if not isinstance(replay_evidence, dict):
            raise InformationNeedStateError("InformationNeed event replay evidence is invalid")
        for need_id, row in state["needs"].items():
            event_map = fingerprints.setdefault(need_id, {})
            if not isinstance(event_map, dict):
                raise InformationNeedStateError("InformationNeed event fingerprint row is invalid")
            # This assignment is the explicit idempotent migration of the
            # current presentation fingerprint.  It is performed only in a
            # writer transaction; read-only projections remain unchanged.
            event_map[str(row["source_event_id"])] = self._record_fingerprint(row)
            self._trim_event_map(event_map)
            replay_map = replay_evidence.setdefault(need_id, {})
            if not isinstance(replay_map, dict):
                raise InformationNeedStateError("InformationNeed event replay evidence row is invalid")
            # ``event_fingerprints`` is a presentation index and intentionally
            # keeps only the newest 32 rows.  Replay evidence is the durable
            # idempotency index, so it must not be trimmed along with that
            # bounded display projection.  Existing documents may only have
            # the presentation index; retain those digests during migration
            # and bind the current generation to its full record fingerprint.
            for event_id, digest in event_map.items():
                replay_map.setdefault(str(event_id), str(digest))
            if len(replay_map) > self.max_replay_evidence_per_need:
                raise InformationNeedStateError(
                    "InformationNeed event replay evidence exceeds its bounded cap"
                )
            replay_map[str(row["source_event_id"])] = self._record_replay_fingerprint(row)
        state.setdefault("retention_events", [])
        state.setdefault("retention_event_count", len(state["retention_events"]))
        state["index_schema_version"] = self.INDEX_SCHEMA_VERSION

    @staticmethod
    def _needs_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(state["needs"])

    def _compact_needs(self, state: dict[str, Any], needs_map: dict[str, dict[str, Any]], *, now: str) -> None:
        scopes = {
            (str(row["owner_id"]), str(row["session_id"]), str(row["situation_id"]))
            for row in needs_map.values()
        }
        for scope in sorted(scopes):
            self._compact_scope(state, needs_map, scope=scope, cap=self.max_needs_per_situation, now=now)
        self._compact_scope(state, needs_map, scope="__global__", cap=self.max_needs, now=now)

    def _compact_scope(
        self,
        state: dict[str, Any],
        needs_map: dict[str, dict[str, Any]],
        *,
        scope: tuple[str, str, str] | str | None,
        cap: int,
        now: str,
    ) -> None:
        def belongs(row: dict[str, Any]) -> bool:
            if scope is None or scope == "__global__":
                return True
            return (str(row["owner_id"]), str(row["session_id"]), str(row["situation_id"])) == scope

        members = [(key, row) for key, row in needs_map.items() if belongs(row)]
        overflow = len(members) - cap
        if overflow <= 0:
            return
        terminal = [(key, row) for key, row in members if str(row["status"]) in TERMINAL_NEED_STATUSES]
        terminal.sort(key=lambda pair: (str(pair[1]["updated_at"]), str(pair[1]["created_at"]), pair[0]))
        evict = terminal[:overflow]
        if len(evict) < overflow:
            raise InformationNeedCapacityError(
                "InformationNeed capacity would require deleting an active/open need"
            )
        evicted_ids = [key for key, _ in evict]
        for key in evicted_ids:
            needs_map.pop(key, None)
            state["event_fingerprints"].pop(key, None)
            state.setdefault("event_replay_evidence", {}).pop(key, None)
        scope_key = "global" if scope == "__global__" else "\u001f".join(scope or ("", "", ""))
        events = state.setdefault("retention_events", [])
        events.append(
            {
                "event_id": "ret_" + hashlib.sha256(f"{now}:{scope_key}:{','.join(evicted_ids)}".encode()).hexdigest()[:24],
                "scope": scope_key,
                "evicted_need_ids": evicted_ids,
                "reason": "terminal_first_capacity_compaction",
                "created_at": now,
            }
        )
        del events[:-MAX_RETENTION_EVENTS]
        state["retention_event_count"] = len(events)

    @staticmethod
    def _scope_rows(needs_map: dict[str, dict[str, Any]], owner: str, session: str, situation: str) -> list[dict[str, Any]]:
        rows = [
            copy.deepcopy(row)
            for row in needs_map.values()
            if str(row["owner_id"]) == owner
            and str(row["session_id"]) == session
            and str(row["situation_id"]) == situation
        ]
        rows.sort(key=lambda item: (str(item["updated_at"]), str(item["need_id"])))
        return rows

    @staticmethod
    def _candidate_fingerprint(
        candidate: CandidateNeed,
        evidence_target: Mapping[str, Any] | None = None,
    ) -> str:
        payload = {
                "blocked_judgment": candidate.blocked_judgment,
                "evidence_kind": candidate.evidence_kind,
                "observation_mode": candidate.observation_mode,
                "why_now": candidate.why_now,
                "urgency": candidate.urgency,
                "expires_at": candidate.expires_at,
                "allowed_source_classes": list(candidate.allowed_source_classes),
                "fallback_reaction": candidate.fallback_reaction,
                "question": candidate.question,
                "evidence_target": dict(evidence_target) if isinstance(evidence_target, Mapping) else None,
                "need_identity_digest": InformationNeedRuntime.need_identity_digest_for_candidate(
                    candidate,
                    evidence_target,
                ),
            }
        requirement = (
            evidence_target.get("observation_requirement")
            if isinstance(evidence_target, Mapping)
            else None
        ) or (
            candidate.observation_requirement.model_dump(mode="json")
            if candidate.observation_requirement is not None
            else None
        )
        if requirement is not None:
            payload["observation_requirement"] = copy.deepcopy(requirement)
        return InformationNeedRuntime._fingerprint_payload(payload)

    @staticmethod
    def _record_fingerprint(record: dict[str, Any]) -> str:
        payload = {
                "blocked_judgment": record["blocked_judgment"],
                "evidence_kind": record["evidence_kind"],
                "observation_mode": record.get("observation_mode", "once"),
                "why_now": record["why_now"],
                "urgency": record["urgency"],
                "expires_at": record.get("expires_at"),
                "allowed_source_classes": list(record.get("allowed_source_classes") or []),
                "fallback_reaction": record["fallback_reaction"],
                "question": record.get("question") or "",
                "evidence_target": record.get("evidence_target"),
            }
        if record.get("observation_requirement") is not None:
            payload["observation_requirement"] = record.get("observation_requirement")
        if record.get("need_identity_digest") is not None:
            payload["need_identity_digest"] = record.get("need_identity_digest")
        return InformationNeedRuntime._fingerprint_payload(payload)

    @staticmethod
    def _pre_typed_legacy_record_fingerprint(record: Mapping[str, Any]) -> str:
        """Digest the exact pre-typed presentation index for read migration."""

        payload = {
            "blocked_judgment": record["blocked_judgment"],
            "evidence_kind": record["evidence_kind"],
            "why_now": record["why_now"],
            "urgency": record["urgency"],
            "expires_at": record.get("expires_at"),
            "allowed_source_classes": list(record.get("allowed_source_classes") or []),
            "fallback_reaction": record["fallback_reaction"],
            "question": record.get("question") or "",
        }
        return InformationNeedRuntime._fingerprint_payload(payload)

    @staticmethod
    def _legacy_record_fingerprint(record: Mapping[str, Any]) -> str:
        """Compatibility name for the exact pre-typed migration digest."""

        return InformationNeedRuntime._pre_typed_legacy_record_fingerprint(record)

    @staticmethod
    def _record_replay_fingerprint(record: dict[str, Any]) -> str:
        candidate_payload = {
            "blocked_judgment": record["blocked_judgment"],
            "evidence_kind": record["evidence_kind"],
            "observation_mode": record.get("observation_mode", "once"),
            "why_now": record["why_now"],
            "urgency": record["urgency"],
            "expires_at": record.get("expires_at"),
            "allowed_source_classes": list(record.get("allowed_source_classes") or []),
            "fallback_reaction": record["fallback_reaction"],
            "question": record.get("question") or "",
            "evidence_target": record.get("evidence_target"),
        }
        if record.get("observation_requirement") is not None:
            candidate_payload["observation_requirement"] = record.get("observation_requirement")
        if record.get("need_identity_digest") is not None:
            candidate_payload["need_identity_digest"] = record.get("need_identity_digest")
        return InformationNeedRuntime._fingerprint_payload(
            {
                "candidate": candidate_payload,
                "unknown_binding": record.get("unknown_binding"),
            }
        )

    @staticmethod
    def _candidate_event_fingerprint(
        candidate: CandidateNeed,
        unknown_binding: str | None,
        evidence_target: Mapping[str, Any] | None = None,
    ) -> str:
        candidate_payload = {
            "blocked_judgment": candidate.blocked_judgment,
            "evidence_kind": candidate.evidence_kind,
            "observation_mode": candidate.observation_mode,
            "why_now": candidate.why_now,
            "urgency": candidate.urgency,
            "expires_at": candidate.expires_at,
            "allowed_source_classes": list(candidate.allowed_source_classes),
            "fallback_reaction": candidate.fallback_reaction,
            "question": candidate.question,
            "evidence_target": dict(evidence_target) if isinstance(evidence_target, Mapping) else None,
            "need_identity_digest": InformationNeedRuntime.need_identity_digest_for_candidate(
                candidate,
                evidence_target,
            ),
        }
        requirement = (
            evidence_target.get("observation_requirement")
            if isinstance(evidence_target, Mapping)
            else None
        ) or (
            candidate.observation_requirement.model_dump(mode="json")
            if candidate.observation_requirement is not None
            else None
        )
        if requirement is not None:
            candidate_payload["observation_requirement"] = copy.deepcopy(requirement)
        return InformationNeedRuntime._fingerprint_payload(
            {
                "candidate": candidate_payload,
                "unknown_binding": unknown_binding,
            }
        )

    def _assert_projected_capacity(
        self,
        state: dict[str, Any],
        needs_map: dict[str, dict[str, Any]],
        candidate_rows: Mapping[
            str,
            tuple[CandidateNeed, str, str | None, dict[str, Any] | None],
        ],
        *,
        owner_id: str,
        session_id: str,
        situation_id: str,
        now: str,
    ) -> None:
        """Simulate the exact terminal-first compaction order before a write."""

        projected = copy.deepcopy(needs_map)
        for need_id in candidate_rows:
            if need_id in projected:
                continue
            projected[need_id] = {
                "need_id": need_id,
                "owner_id": owner_id,
                "session_id": session_id,
                "situation_id": situation_id,
                "status": "open",
                "created_at": now,
                "updated_at": now,
            }
        projected_state = copy.deepcopy(state)
        projected_state["event_fingerprints"] = {
            str(key): copy.deepcopy(value)
            for key, value in (state.get("event_fingerprints") or {}).items()
            if str(key) in projected
        }
        projected_state["event_replay_evidence"] = {
            str(key): copy.deepcopy(value)
            for key, value in (state.get("event_replay_evidence") or {}).items()
            if str(key) in projected
        }
        self._compact_needs(projected_state, projected, now=now)

    @staticmethod
    def _fingerprint_payload(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()

    def _assert_replay_write_capacity(self, replay_map: Mapping[str, Any], event_id: str) -> None:
        """Reject a new replay key at the cap; never evict old evidence."""

        if str(event_id) in replay_map:
            return
        if len(replay_map) >= self.max_replay_evidence_per_need:
            raise InformationNeedCapacityError(
                "InformationNeed replay evidence capacity is exhausted; old events are retained"
            )

    @staticmethod
    def _trim_event_map(event_map: dict[str, Any]) -> None:
        if len(event_map) > MAX_EVENT_FINGERPRINTS_PER_NEED:
            for key in list(event_map)[:-MAX_EVENT_FINGERPRINTS_PER_NEED]:
                event_map.pop(key, None)

    def _validate_fingerprint_index(self, value: Any, need_ids: set[str]) -> None:
        if value is None:
            if need_ids:
                raise InformationNeedStateError("InformationNeed event fingerprint index is missing")
            return
        if not isinstance(value, dict) or set(value) != need_ids:
            raise InformationNeedStateError("InformationNeed event fingerprint index is inconsistent")
        for need_id, events in value.items():
            if not isinstance(need_id, str) or not isinstance(events, dict) or len(events) > MAX_EVENT_FINGERPRINTS_PER_NEED:
                raise InformationNeedStateError("InformationNeed event fingerprint row is invalid")
            for event_id, digest in events.items():
                if not isinstance(event_id, str) or not event_id or len(event_id) > 240 or not isinstance(digest, str) or len(digest) != 64:
                    raise InformationNeedStateError("InformationNeed event fingerprint is invalid")

    def _validate_replay_index(self, value: Any, need_ids: set[str]) -> None:
        if value is None:
            return
        if not isinstance(value, dict) or not set(value).issubset(need_ids):
            raise InformationNeedStateError("InformationNeed event replay index is inconsistent")
        for need_id, events in value.items():
            if (
                not isinstance(need_id, str)
                or not isinstance(events, dict)
                or len(events) > self.max_replay_evidence_per_need
            ):
                raise InformationNeedStateError("InformationNeed event replay row is invalid")
            for event_id, digest in events.items():
                if (
                    not isinstance(event_id, str)
                    or not event_id
                    or len(event_id) > 240
                    or not isinstance(digest, str)
                    or len(digest) != 64
                ):
                    raise InformationNeedStateError("InformationNeed event replay fingerprint is invalid")

    @staticmethod
    def _validate_retention(state: dict[str, Any]) -> None:
        events = state.get("retention_events")
        if events is None:
            return
        if not isinstance(events, list) or len(events) > MAX_RETENTION_EVENTS:
            raise InformationNeedStateError("InformationNeed retention audit is invalid")
        count = state.get("retention_event_count", len(events))
        if isinstance(count, bool) or not isinstance(count, int) or count != len(events):
            raise InformationNeedStateError("InformationNeed retention audit count is inconsistent")
        for event in events:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("event_id"), str)
                or not isinstance(event.get("scope"), str)
                or not isinstance(event.get("evicted_need_ids"), list)
                or not isinstance(event.get("reason"), str)
                or not isinstance(event.get("created_at"), str)
            ):
                raise InformationNeedStateError("InformationNeed retention audit entry is invalid")
            if len(event["evicted_need_ids"]) > 64 or any(not isinstance(item, str) for item in event["evicted_need_ids"]):
                raise InformationNeedStateError("InformationNeed retention audit IDs are invalid")

    def _state_path(self) -> Any:
        path_for = getattr(self.state_store, "path_for", None)
        if not callable(path_for):
            return None
        try:
            return path_for(self.state_file)
        except Exception:
            return None

    @staticmethod
    def _assert_owner(item: dict[str, Any], owner_id: str, session_id: str) -> None:
        if str(item["owner_id"]) != owner_id:
            raise InformationNeedAccessError("InformationNeed belongs to a different owner")
        if str(item["session_id"]) != session_id:
            raise InformationNeedAccessError("InformationNeed belongs to a different session")

    @staticmethod
    def _required(value: Any, name: str) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > 240:
            raise ValueError(f"{name} is required")
        return selected

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": InformationNeedRuntime.SCHEMA_VERSION,
            "needs": {},
            "need_count": 0,
            "event_fingerprints": {},
            "event_replay_evidence": {},
            "index_schema_version": InformationNeedRuntime.INDEX_SCHEMA_VERSION,
            "retention_events": [],
            "retention_event_count": 0,
        }
