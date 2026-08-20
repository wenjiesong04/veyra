"""Strict repository for the single durable Situation state document.

Semantic CRUD belongs in ``runtime.semantic_situation_runtime``.  This module
owns only document shape, bounded retention, scope-safe reads, and the shared
writer/normalizer used by that runtime.  A malformed document is an error, not
an empty state, so a read cannot silently erase or replace user context.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from core.world_state import WorldStateStore


class SituationStateError(ValueError):
    """The authoritative Situation document is structurally untrustworthy."""


class SituationStateCapacityError(RuntimeError):
    """Bounded retention cannot preserve active semantic Situations."""


class SituationStateRepository:
    STATE_FILE = "situation_state.json"
    SCHEMA_VERSION = "veyra.situation_state.v1"
    # The semantic runtime uses a small lifecycle vocabulary while the legacy
    # SituationEvaluator has historically used a wider status vocabulary.  A
    # shared retention policy must understand both; otherwise a legacy writer
    # can mistake an active semantic row for an old row and evict it.
    TERMINAL_STATUSES = frozenset({
        "resolved",
        "expired",
        "contradicted",
        "archived",
        "cancelled",
        "closed",
        "dismissed",
        "failed",
        "indeterminate",
        "partial",
        "verified_failed",
        "verified_success",
    })
    ACTIVE_SEMANTIC_LIFECYCLES = frozenset({"emerging", "active", "waiting"})

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        state_file: str = STATE_FILE,
        max_situations: int = 500,
        max_situations_per_user: int = 200,
        max_history_per_situation: int = 24,
    ) -> None:
        if max_situations < 1 or max_situations_per_user < 1:
            raise ValueError("Situation repository capacity must be positive")
        self.state_store = state_store
        self.state_file = str(state_file)
        self.max_situations = int(max_situations)
        self.max_situations_per_user = min(int(max_situations_per_user), self.max_situations)
        self.max_history_per_situation = max(1, int(max_history_per_situation))

    def empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "situations": [],
            "count": 0,
            "retention_audit": [],
            "updated_at": None,
        }

    def read(self) -> dict[str, Any]:
        raw = self.state_store.read_json(self.state_file)
        if not raw:
            path_for = getattr(self.state_store, "path_for", None)
            path = path_for(self.state_file) if callable(path_for) else None
            if path is not None and path.exists():
                raise SituationStateError("Situation state is empty or corrupt")
            return self.empty_state()
        return self.validate(raw)

    def mutate(self, callback: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        def guarded(state: dict[str, Any]) -> dict[str, Any]:
            if not state:
                path_for = getattr(self.state_store, "path_for", None)
                path = path_for(self.state_file) if callable(path_for) else None
                if path is not None and path.exists():
                    raise SituationStateError("Situation state is empty or corrupt")
                current = self.empty_state()
            else:
                current = self.validate(state)
            working = copy.deepcopy(current)
            result = callback(working)
            candidate = working if result is None else result
            if not isinstance(candidate, dict):
                raise TypeError("Situation repository mutator must return an object")
            before_ids = {
                str(row.get("situation_id") or "")
                for row in current.get("situations", [])
                if isinstance(row, dict)
            }
            new_ids = {
                str(row.get("situation_id") or "")
                for row in candidate.get("situations", [])
                if isinstance(row, dict)
            } - before_ids
            candidate = self._enforce_capacity(candidate, preserve_ids=new_ids)
            return self.validate(candidate)

        return self.state_store.mutate_json(self.state_file, guarded)

    def validate(self, state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("_state_corrupt"):
            raise SituationStateError("Situation state is corrupt")
        if state.get("schema_version") != self.SCHEMA_VERSION:
            raise SituationStateError("Situation state schema is unsupported")
        rows = state.get("situations")
        if not isinstance(rows, list):
            raise SituationStateError("Situation state situations must be a list")
        if len(rows) > self.max_situations:
            raise SituationStateCapacityError("Situation state exceeds global bounded capacity")
        count = state.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
            raise SituationStateError("Situation state count is inconsistent")
        identities: set[str] = set()
        semantic_subjects: set[tuple[str, str, str]] = set()
        owner_counts: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise SituationStateError("Situation row must be an object")
            situation_id = str(row.get("situation_id") or "").strip()
            if not situation_id or situation_id in identities:
                raise SituationStateError("Situation identity is missing or duplicated")
            identities.add(situation_id)
            owner = str(row.get("user_id") or "")
            owner_counts[owner] = owner_counts.get(owner, 0) + 1
            if str(row.get("record_kind") or "") == "semantic_situation":
                session = str(row.get("session_id") or "")
                subject = str(row.get("semantic_subject_key") or "")
                if not owner or not session or not subject:
                    raise SituationStateError("semantic Situation scope is incomplete")
                semantic_identity = (owner, session, subject)
                if semantic_identity in semantic_subjects:
                    raise SituationStateError("semantic Situation subject identity is duplicated")
                semantic_subjects.add(semantic_identity)
                revision = row.get("observation_revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise SituationStateError("semantic Situation revision is invalid")
                if not isinstance(row.get("semantic"), dict):
                    raise SituationStateError("semantic Situation projection is invalid")
                try:
                    normalized_semantic = self.normalize_semantic(row["semantic"])
                except (TypeError, ValueError) as exc:
                    raise SituationStateError("semantic Situation projection is invalid") from exc
                row_status = str(row.get("status") or "").strip().lower()
                if row_status != normalized_semantic["lifecycle"]:
                    raise SituationStateError(
                        "semantic Situation status and lifecycle are inconsistent"
                    )
        if any(count > self.max_situations_per_user for count in owner_counts.values()):
            raise SituationStateCapacityError(
                "Situation state exceeds per-owner bounded capacity"
            )
        audit = state.get("retention_audit", [])
        if not isinstance(audit, list) or len(audit) > 128:
            raise SituationStateError("Situation retention audit is invalid")
        return copy.deepcopy(state)

    def list(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        newest_first: bool = True,
        semantic_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self.read()["situations"]
        selected: list[dict[str, Any]] = []
        for row in rows:
            if semantic_only and str(row.get("record_kind") or "") != "semantic_situation":
                continue
            if user_id is not None and str(row.get("user_id") or "") != str(user_id):
                continue
            if session_id is not None and str(row.get("session_id") or "") != str(session_id):
                continue
            selected.append(copy.deepcopy(row))
        selected.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=newest_first)
        return selected[: max(0, int(limit))]

    def get(
        self,
        situation_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        semantic_only: bool = False,
    ) -> dict[str, Any] | None:
        target = str(situation_id or "")
        for row in self.list(
            user_id=user_id,
            session_id=session_id,
            limit=self.max_situations,
            semantic_only=semantic_only,
        ):
            if str(row.get("situation_id") or "") == target:
                return row
        return None

    def retain_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        preserve_ids: Iterable[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply the one bounded-retention policy shared by every writer.

        Semantic Situations in an active lifecycle are protected from both
        per-owner and global eviction.  Terminal semantic rows and legacy
        rows are the only eviction candidates.  If a bound cannot be met
        without deleting protected context, this raises before the caller can
        write, leaving the state document byte-pure.

        The returned rows keep input order for deterministic replay.  The
        policy does not mutate the input list or its row dictionaries.
        """

        if not isinstance(rows, list):
            raise SituationStateError("Situation rows must be a list")
        preserved = {
            str(value).strip()
            for value in (preserve_ids or [])
            if str(value).strip()
        }
        identities: set[str] = set()
        semantic_subjects: set[tuple[str, str, str]] = set()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SituationStateError("Situation row must be an object")
            situation_id = str(row.get("situation_id") or "").strip()
            if not situation_id or situation_id in identities:
                raise SituationStateError("Situation identity is missing or duplicated")
            identities.add(situation_id)
            if self._is_semantic(row):
                owner = str(row.get("user_id") or "")
                session = str(row.get("session_id") or "")
                subject = str(row.get("semantic_subject_key") or "")
                if not owner or not session or not subject:
                    raise SituationStateError("semantic Situation scope is incomplete")
                identity = (owner, session, subject)
                if identity in semantic_subjects:
                    raise SituationStateError("semantic Situation subject identity is duplicated")
                semantic_subjects.add(identity)
                semantic = row.get("semantic")
                if not isinstance(semantic, dict):
                    raise SituationStateError("semantic Situation projection is invalid")
                try:
                    normalized_semantic = self.normalize_semantic(semantic)
                except (TypeError, ValueError) as exc:
                    raise SituationStateError("semantic Situation projection is invalid") from exc
                row_status = str(row.get("status") or "").strip().lower()
                if row_status != normalized_semantic["lifecycle"]:
                    raise SituationStateError(
                        "semantic Situation status and lifecycle are inconsistent"
                    )
            normalized.append(row)

        owner_groups: dict[str, list[dict[str, Any]]] = {}
        for row in normalized:
            owner_groups.setdefault(self._owner_key(row), []).append(row)
        evict: set[str] = set()

        for scoped in owner_groups.values():
            overflow = len(scoped) - self.max_situations_per_user
            if overflow <= 0:
                continue
            candidates = sorted(
                (
                    row
                    for row in scoped
                    if str(row["situation_id"]) not in preserved
                    and self._is_evictable(row)
                ),
                key=self._eviction_key,
            )
            if len(candidates) < overflow:
                raise SituationStateCapacityError(
                    "active semantic Situation capacity cannot be safely compacted"
                )
            evict.update(str(row["situation_id"]) for row in candidates[:overflow])

        remaining = [row for row in normalized if str(row["situation_id"]) not in evict]
        overflow = len(remaining) - self.max_situations
        if overflow > 0:
            candidates = sorted(
                (
                    row
                    for row in remaining
                    if str(row["situation_id"]) not in preserved
                    and self._is_evictable(row)
                ),
                key=self._eviction_key,
            )
            if len(candidates) < overflow:
                raise SituationStateCapacityError(
                    "global Situation capacity cannot be safely compacted"
                )
            evict.update(str(row["situation_id"]) for row in candidates[:overflow])

        retained = [
            copy.deepcopy(row)
            for row in normalized
            if str(row["situation_id"]) not in evict
        ]
        return retained, sorted(evict)

    def stable_situation_id(self, *, user_id: str, subject_key: str) -> str:
        identity = "\0".join((str(user_id), str(subject_key)))
        return f"sit_sem_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"

    def normalize_semantic(self, value: dict[str, Any]) -> dict[str, Any]:
        """Validate and bound the semantic projection without authority fields."""

        if not isinstance(value, dict):
            raise TypeError("semantic_state must be a mapping")
        forbidden = {
            "url", "path", "query", "command", "tool", "tool_args", "credentials",
            "recipient", "authority", "route", "risk", "state_effect", "capability_grant",
            "allowed_capabilities", "execution",
        }

        def safe(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if str(key).strip().lower() in forbidden:
                        raise ValueError(f"semantic state field {key!r} is not permitted")
                    safe(nested)
            elif isinstance(item, list):
                for nested in item:
                    safe(nested)

        safe(value)

        def text(name: str, limit: int) -> str:
            return str(value.get(name) or "")[:limit]

        lifecycle = text("lifecycle", 32).lower()
        if lifecycle not in {"emerging", "active", "waiting", "resolved", "expired", "contradicted", "archived"}:
            raise ValueError("semantic lifecycle is invalid")
        category = text("category", 32).lower() or "general"
        if category not in {"general", "personal", "work", "education", "health", "travel", "logistics", "finance", "other"}:
            raise ValueError("semantic category is invalid")

        def timestamp(name: str) -> str | None:
            raw = value.get(name)
            if raw is None or not str(raw).strip():
                return None
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"semantic {name} must be ISO-8601") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"semantic {name} must include a timezone")
            return parsed.astimezone(timezone.utc).isoformat()

        def records(name: str, limit: int) -> list[dict[str, Any]]:
            raw = value.get(name)
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise TypeError(f"semantic {name} must be a list")
            return [self._bounded(item) for item in raw[:limit] if isinstance(item, dict)]

        unknown_raw = value.get("unknown") or []
        if not isinstance(unknown_raw, list):
            raise TypeError("semantic unknown must be a list")
        unknown = [str(item)[:480] for item in unknown_raw[:12] if str(item).strip()]
        progress = value.get("progress") or {}
        if not isinstance(progress, dict):
            raise TypeError("semantic progress must be an object")
        progress_status = str(progress.get("status") or "unknown").lower()
        if progress_status not in {"unknown", "not_started", "in_progress", "blocked", "waiting", "completed"}:
            raise ValueError("semantic progress status is invalid")
        progress_value = progress.get("value")
        if progress_value is not None:
            if isinstance(progress_value, bool):
                raise TypeError("semantic progress value is invalid")
            try:
                progress_value = float(progress_value)
            except (TypeError, ValueError) as exc:
                raise TypeError("semantic progress value is invalid") from exc
            if not 0 <= progress_value <= 1:
                raise ValueError("semantic progress value is out of range")
        entities_raw = value.get("entities") or []
        if not isinstance(entities_raw, list):
            raise TypeError("semantic entities must be a list")
        entities: list[dict[str, Any]] = []
        for item in entities_raw[:8]:
            if not isinstance(item, dict):
                raise TypeError("semantic entity must be an object")
            kind = str(item.get("kind") or "").lower()
            entity_value = str(item.get("value") or "")[:240]
            if kind not in {"place", "person", "organization", "item", "other"} or not entity_value:
                raise ValueError("semantic entity is invalid")
            epi = str(item.get("epistemic_status") or "reported").lower()
            if epi not in {"reported", "inferred"}:
                raise ValueError("semantic entity epistemic status is invalid")
            entities.append({"kind": kind, "value": entity_value, "epistemic_status": epi, **(
                {"source_quote": self._bounded(item["source_quote"])} if isinstance(item.get("source_quote"), dict) else {}
            )})
        next_step_status = str(value.get("next_step_epistemic_status") or "inferred").lower()
        if next_step_status not in {"reported", "inferred"}:
            raise ValueError("semantic next_step epistemic status is invalid")
        return {
            "title": text("title", 240),
            "label": text("label", 240),
            "summary": text("summary", 640),
            "goal": text("goal", 480),
            "category": category,
            "lifecycle": lifecycle,
            "deadline_at": timestamp("deadline_at"),
            "progress": {"status": progress_status, "value": progress_value},
            "entities": entities,
            "known": records("known", 12),
            "unknown": unknown,
            "assumptions": records("assumptions", 8),
            "timeline": records("timeline", self.max_history_per_situation),
            "evidence": records("evidence", 16),
            "material_change": text("material_change", 480),
            "next_observation_at": timestamp("next_observation_at"),
            "next_step": text("next_step", 480),
            "next_step_epistemic_status": next_step_status,
        }

    def _enforce_capacity(
        self,
        state: dict[str, Any],
        *,
        preserve_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        rows = state.get("situations") if isinstance(state.get("situations"), list) else []
        if not isinstance(state.get("situations"), list):
            raise SituationStateError("Situation state situations must be a list")
        retained, evicted = self.retain_rows(rows, preserve_ids=preserve_ids)
        evict = set(evicted)
        if evict:
            state["situations"] = retained
            audit = state.setdefault("retention_audit", [])
            audit.append(
                {
                    "evicted_situation_ids": evicted,
                    "reason": "terminal_or_legacy_first",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            del audit[:-128]
        state["count"] = len(state.get("situations") or [])
        return state

    @classmethod
    def _is_semantic(cls, row: dict[str, Any]) -> bool:
        return str(row.get("record_kind") or "") == "semantic_situation"

    @classmethod
    def _owner_key(cls, row: dict[str, Any]) -> str:
        return str(row.get("user_id") or "")

    @classmethod
    def _lifecycle(cls, row: dict[str, Any]) -> str:
        semantic = row.get("semantic")
        if isinstance(semantic, dict) and semantic.get("lifecycle"):
            return str(semantic.get("lifecycle") or "").strip().lower()
        return str(row.get("status") or "").strip().lower()

    @classmethod
    def _is_active_semantic(cls, row: dict[str, Any]) -> bool:
        return cls._is_semantic(row) and cls._lifecycle(row) in cls.ACTIVE_SEMANTIC_LIFECYCLES

    @classmethod
    def _is_terminal(cls, row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "").strip().lower()
        return cls._lifecycle(row) in cls.TERMINAL_STATUSES or status in cls.TERMINAL_STATUSES

    @classmethod
    def _is_evictable(cls, row: dict[str, Any]) -> bool:
        # Active/emerging/waiting semantic context is never an eviction
        # candidate.  Every other row is bounded history that may be compacted.
        return not cls._is_active_semantic(row)

    @classmethod
    def _eviction_key(cls, row: dict[str, Any]) -> tuple[int, str, str]:
        # Terminal rows are the least valuable; legacy rows are next.  The
        # timestamp and identity make replay/retention deterministic.
        if cls._is_terminal(row):
            tier = 0
        elif not cls._is_semantic(row):
            tier = 1
        else:
            tier = 2
        timestamp = str(row.get("updated_at") or row.get("created_at") or "")
        return tier, timestamp, str(row.get("situation_id") or "")

    @staticmethod
    def _bounded(value: Any, depth: int = 0) -> Any:
        if depth > 4:
            return str(value)[:500]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value if not isinstance(value, str) else value[:4000]
        if isinstance(value, dict):
            return {
                str(key)[:200]: SituationStateRepository._bounded(item, depth + 1)
                for key, item in list(value.items())[:48]
            }
        if isinstance(value, list):
            return [SituationStateRepository._bounded(item, depth + 1) for item in value[:48]]
        return str(value)[:1000]

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
