"""Durable, owner-scoped reaction ledger for Living Context.

The runtime persists every disposition, including ``silent``.  It owns
idempotency and explicit feedback aftereffects, while the pure policy module
owns the ask/read/wait/silent/suggest choice.  No method executes a tool,
delivers externally, or expands permissions.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from interface.living_reaction_contract import (
    FEEDBACK_LABELS,
    REACTION_DISPOSITIONS,
    REACTION_SCHEMA,
    FeedbackCommand,
    LivingReactionValidationError,
    ReactionDecision,
    ReactionInput,
    parse_time,
    stable_digest,
    time_iso,
)
from runtime.living_reaction_policy import (
    DEFAULT_FEEDBACK_POLICY,
    apply_feedback_effect,
    decide_reaction,
    normalise_policy,
    temporal_phase,
)
from runtime.living_reaction_archive import LivingReactionArchive, LivingReactionArchiveError


class LivingReactionRuntimeError(RuntimeError):
    """Base reaction-ledger error."""


class LivingReactionConflict(LivingReactionRuntimeError):
    """A replay or feedback target conflicts with durable state."""


class LivingReactionStorageError(LivingReactionRuntimeError):
    """The ledger is corrupt or has reached a bounded capacity."""


class LivingReactionRuntime:
    STATE_FILE = "living_reaction_state.json"
    STATE_SCHEMA = "veyra.living_reaction_state.v1"
    MAX_REACTIONS = 1200
    MAX_FEEDBACK = 600
    MAX_POLICY_REVISIONS = 120
    # The durable limits above remain accepted for legacy state validation;
    # new writes keep a much smaller working set and archive evicted rows.
    HOT_REACTIONS = 120
    HOT_FEEDBACK = 120
    HOT_POLICY_REVISIONS = 32
    MAX_SITUATION_EFFECTS = 600
    MAX_CATEGORY_BUCKETS = 240
    MAX_CATEGORY_LABELS = 8
    MAX_CATEGORY_SITUATIONS = 1200
    CATEGORY_POLICY_TTL_SECONDS = 30 * 86400

    def __init__(self, state_store: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.archive = LivingReactionArchive(state_store, authority=self._authority())

    def evaluate(self, value: ReactionInput | Mapping[str, Any]) -> dict[str, Any]:
        reaction = value if isinstance(value, ReactionInput) else ReactionInput.from_mapping(value)
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            archive = self._archive_snapshot()
            self._ensure_state(state, archive=archive)
            policy = self._effective_policy(state, reaction)
            temporal = temporal_phase(reaction, policy)
            evaluated = replace(
                reaction,
                feedback_policy={
                    **reaction.feedback_policy,
                    "policy_revision": int(policy.get("policy_revision", 0) or 0),
                    "temporal_phase": temporal["phase"],
                    "evaluation_boundary": temporal["evaluation_boundary"],
                },
            )
            decision = decide_reaction(evaluated, feedback_policy=policy)
            existing_id = state["idempotency_index"].get(decision.idempotency_key)
            if not existing_id:
                archived = next(
                    (row for row in archive["reactions"].values() if row.get("idempotency_key") == decision.idempotency_key),
                    None,
                )
                if isinstance(archived, dict):
                    result.update(status="duplicate", decision=copy.deepcopy(archived))
                    return state
            if existing_id:
                existing = state["reactions"].get(existing_id)
                if not isinstance(existing, dict):
                    existing = archive["reactions"].get(existing_id)
                if not isinstance(existing, dict):
                    raise LivingReactionStorageError("reaction idempotency index points to no record")
                result.update(status="duplicate", decision=copy.deepcopy(existing))
                return state
            if len(state["reactions"]) >= self.HOT_REACTIONS:
                self._compact_state(state, archive, reaction_target=self.HOT_REACTIONS - 1)
            if len(state["reactions"]) >= self.HOT_REACTIONS:
                raise LivingReactionStorageError("living reaction hot capacity exhausted")
            persisted = decision.to_dict()
            persisted["ledger_status"] = "recorded"
            persisted["policy_revision"] = int(policy.get("policy_revision", 0) or 0)
            state["reactions"][decision.reaction_id] = persisted
            state["idempotency_index"][decision.idempotency_key] = decision.reaction_id
            state["metrics"]["reaction_count"] = len(state["reactions"])
            state["metrics"]["disposition_counts"][decision.disposition] += 1
            self._compact_state(state, archive)
            result.update(status="recorded", decision=copy.deepcopy(persisted))
            return state

        self.state_store.mutate_json(self.STATE_FILE, update)
        return self._envelope(result)

    react = evaluate
    decide = evaluate

    def record_feedback(self, value: FeedbackCommand | Mapping[str, Any]) -> dict[str, Any]:
        feedback = value if isinstance(value, FeedbackCommand) else FeedbackCommand.from_mapping(value)
        if feedback.label not in FEEDBACK_LABELS:
            raise LivingReactionValidationError("feedback label is unsupported")
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            archive = self._archive_snapshot()
            self._ensure_state(state, archive=archive)
            current = state["feedback"].get(feedback.feedback_id)
            if current is None:
                current = archive["feedback"].get(feedback.feedback_id)
            semantics = feedback.semantics()
            if current is not None:
                if not isinstance(current, dict) or current.get("semantics") != semantics:
                    raise LivingReactionConflict("feedback_id is already bound to different semantics")
                result.update(status="duplicate", feedback=copy.deepcopy(current))
                return state
            reaction = state["reactions"].get(feedback.reaction_id) or archive["reactions"].get(feedback.reaction_id)
            if not isinstance(reaction, dict):
                raise LivingReactionConflict("feedback target reaction does not exist")
            for key in ("owner_id", "session_id", "situation_id"):
                if str(reaction[key]) != str(getattr(feedback, key)):
                    raise LivingReactionConflict(f"feedback {key} does not match reaction scope")
            if len(state["feedback"]) >= self.HOT_FEEDBACK:
                self._compact_state(state, archive, feedback_target=self.HOT_FEEDBACK - 1)
            if len(state["feedback"]) >= self.HOT_FEEDBACK:
                raise LivingReactionStorageError("living reaction feedback hot capacity exhausted")

            now = feedback.now
            scope = self._scope_key(feedback.owner_id, feedback.session_id, feedback.situation_id)
            old_effect = copy.deepcopy(state["situation_effects"].get(scope) or {})
            new_effect = apply_feedback_effect(
                old_effect,
                feedback.label,
                now=now,
                remind_before_seconds=feedback.remind_before_seconds,
            )
            new_effect["policy_revision"] = int(old_effect.get("policy_revision", 0) or 0) + 1
            new_effect["scope"] = scope
            if scope not in state["situation_effects"] and len(state["situation_effects"]) >= self.MAX_SITUATION_EFFECTS:
                raise LivingReactionStorageError("living reaction situation-effect capacity exhausted")
            state["situation_effects"][scope] = new_effect

            category = str(feedback.category or reaction["category"] or "general").strip()[:120] or "general"
            sample = self._record_category_sample(
                state,
                owner_id=feedback.owner_id,
                category=category,
                label=feedback.label,
                situation_id=feedback.situation_id,
                feedback_id=feedback.feedback_id,
                reaction_id=feedback.reaction_id,
            )
            cross = self._maybe_revise_category_policy(
                state,
                owner_id=feedback.owner_id,
                category=category,
                label=feedback.label,
                sample=sample,
                now=now,
            )
            record = {
                "schema_version": "veyra.living_reaction_feedback.v1",
                "feedback_id": feedback.feedback_id,
                "semantics": semantics,
                "created_at": time_iso(now),
                "evidence_refs": list(feedback.evidence_refs) or [feedback.reaction_id],
                "aftereffects": {
                    "current_situation": {
                        "scope": scope,
                        "old": old_effect,
                        "new": copy.deepcopy(new_effect),
                    },
                    "cross_situation": cross,
                },
                "authority": self._authority(),
            }
            state["feedback"][feedback.feedback_id] = record
            state["metrics"]["feedback_count"] = len(state["feedback"])
            self._compact_state(state, archive)
            result.update(status="recorded", feedback=copy.deepcopy(record))
            return state

        self.state_store.mutate_json(self.STATE_FILE, update)
        return self._envelope(result)

    def status(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(self.STATE_FILE)
            archive = self._archive_snapshot()
            self._ensure_state(state, archive=archive)
        except (LivingReactionStorageError, TypeError, ValueError) as exc:
            return {
                "status": "degraded",
                "reason": str(exc),
                "state_frozen": True,
                "authority": self._authority(),
            }
        return {
            "status": "success",
            "schema_version": self.STATE_SCHEMA,
            "reaction_count": len(state["reactions"]) + len(archive["reactions"]),
            "feedback_count": len(state["feedback"]) + len(archive["feedback"]),
            "policy_revision_count": len(state["policy_revisions"]) + len(archive["policy_revisions"]),
            "hot_reaction_count": len(state["reactions"]),
            "hot_feedback_count": len(state["feedback"]),
            "hot_policy_revision_count": len(state["policy_revisions"]),
            "archive_segments": len(archive["manifest"].get("segments", [])),
            "category_count": len(state["category_policies"]),
            "metrics": copy.deepcopy(state["metrics"]),
            "authority": self._authority(),
        }

    def list_reactions(
        self,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        state = self.state_store.read_json(self.STATE_FILE)
        archive = self._archive_snapshot()
        self._ensure_state(state, archive=archive)
        selected_limit = max(1, min(10000, int(limit)))
        combined = {**archive["reactions"], **state["reactions"]}
        rows = [
            row
            for row in combined.values()
            if row["owner_id"] == owner_id
            and row["session_id"] == session_id
            and (situation_id is None or row["situation_id"] == situation_id)
        ]
        rows.sort(key=lambda row: str(row["created_at"]), reverse=True)
        return copy.deepcopy(rows[:selected_limit])

    def get_current_reaction(
        self,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str,
        situation_revision: int,
    ) -> dict[str, Any] | None:
        """Return the authoritative reaction for one exact Situation revision.

        ``list_reactions`` is a bounded history projection.  It is therefore
        not safe for catalog binding: after older rows move to the archive,
        a current row can fall outside its caller-facing page (particularly
        when many rows share one timestamp).  This read scans the validated
        hot state and immutable archive together, then filters by the exact
        owner/session/Situation/revision tuple before selecting the newest
        policy boundary for that revision.  It never falls back to a stale
        revision or mutates state.
        """

        if isinstance(situation_revision, bool) or not isinstance(situation_revision, int):
            raise ValueError("situation_revision must be an integer")
        selected_revision = situation_revision
        if selected_revision < 1:
            raise ValueError("situation_revision must be positive")
        state = self.state_store.read_json(self.STATE_FILE)
        archive = self._archive_snapshot()
        self._ensure_state(state, archive=archive)
        combined = {**archive["reactions"], **state["reactions"]}
        matching = [
            row
            for row in combined.values()
            if isinstance(row, Mapping)
            and str(row.get("owner_id") or "") == str(owner_id)
            and str(row.get("session_id") or "") == str(session_id)
            and str(row.get("situation_id") or "") == str(situation_id)
            and int(row.get("situation_revision") or 0) == selected_revision
        ]
        if not matching:
            return None
        # A feedback policy/temporal boundary can produce more than one
        # reaction for the same Situation revision.  The highest server-owned
        # policy revision is the current reaction; timestamp and content ID
        # make ties deterministic without treating an older revision as a
        # substitute.
        matching.sort(
            key=lambda row: (
                int(row.get("policy_revision") or 0),
                str(row.get("created_at") or ""),
                str(row.get("reaction_id") or ""),
            ),
            reverse=True,
        )
        return copy.deepcopy(dict(matching[0]))

    def get_reaction(
        self,
        reaction_id: str,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one exact reaction from hot state, then immutable archive."""

        state = self.state_store.read_json(self.STATE_FILE)
        archive = self._archive_snapshot()
        self._ensure_state(state, archive=archive)
        row = state["reactions"].get(str(reaction_id)) or archive["reactions"].get(str(reaction_id))
        if not isinstance(row, dict):
            return None
        if str(row.get("owner_id")) != str(owner_id) or str(row.get("session_id")) != str(session_id):
            return None
        if situation_id is not None and str(row.get("situation_id")) != str(situation_id):
            return None
        return copy.deepcopy(row)

    def read_state(self) -> dict[str, Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        archive = self._archive_snapshot()
        self._ensure_state(state, archive=archive)
        state["archive"] = {
            "schema_version": archive["manifest"].get("schema_version"),
            "head_digest": archive["manifest"].get("head_digest", ""),
            "segment_count": len(archive["manifest"].get("segments", [])),
            "reaction_count": len(archive["reactions"]),
            "feedback_count": len(archive["feedback"]),
            "policy_revision_count": len(archive["policy_revisions"]),
        }
        return copy.deepcopy(state)

    def _archive_snapshot(self) -> dict[str, Any]:
        try:
            return self.archive.read()
        except (LivingReactionArchiveError, TypeError, ValueError) as exc:
            raise LivingReactionStorageError(str(exc)) from exc

    @staticmethod
    def _merge_archived_rows(
        hot: Mapping[str, Any],
        archived: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        merged = {str(key): copy.deepcopy(value) for key, value in archived.items()}
        for key, value in hot.items():
            existing = merged.get(str(key))
            if existing is not None and existing != value:
                raise LivingReactionStorageError(f"living reaction {label} hot/archive duplicate conflicts")
            merged[str(key)] = copy.deepcopy(value)
        return merged

    def _protected_reaction_ids(self, state: Mapping[str, Any]) -> set[str]:
        latest: dict[tuple[str, str, str], int] = {}
        for row in state["reactions"].values():
            if not isinstance(row, Mapping):
                continue
            key = (str(row.get("owner_id")), str(row.get("session_id")), str(row.get("situation_id")))
            latest[key] = max(latest.get(key, 0), int(row.get("situation_revision") or 0))
        return {
            str(row["reaction_id"])
            for row in state["reactions"].values()
            if isinstance(row, Mapping)
            and int(row.get("situation_revision") or 0)
            == latest.get((str(row.get("owner_id")), str(row.get("session_id")), str(row.get("situation_id"))), -1)
        }

    def _protected_feedback_ids(self, state: Mapping[str, Any]) -> set[str]:
        latest: dict[tuple[str, str, str], tuple[str, str]] = {}
        for feedback_id, row in state["feedback"].items():
            if not isinstance(row, Mapping):
                continue
            semantics = row.get("semantics")
            if not isinstance(semantics, Mapping):
                continue
            key = (str(semantics.get("owner_id")), str(semantics.get("session_id")), str(semantics.get("situation_id")))
            candidate = (str(row.get("created_at") or ""), str(feedback_id))
            if candidate > latest.get(key, ("", "")):
                latest[key] = candidate
        return {value[1] for value in latest.values()}

    def _compact_state(
        self,
        state: dict[str, Any],
        archive: Mapping[str, Any],
        *,
        reaction_target: int | None = None,
        feedback_target: int | None = None,
        policy_target: int | None = None,
    ) -> None:
        """Archive evictable hot rows before shrinking the working set.

        The method is called from an existing ``mutate_json`` callback, so
        segment publication and the subsequent hot-state shrink share the
        WorldState writer fence.  No row is removed until the segment exists.
        """

        selected: dict[str, list[dict[str, Any]]] = {"reactions": [], "feedback": [], "policy_revisions": []}
        protected_reactions = self._protected_reaction_ids(state)
        reaction_limit = self.HOT_REACTIONS if reaction_target is None else max(0, int(reaction_target))
        feedback_limit = self.HOT_FEEDBACK if feedback_target is None else max(0, int(feedback_target))
        policy_limit = self.HOT_POLICY_REVISIONS if policy_target is None else max(0, int(policy_target))
        if len(protected_reactions) > reaction_limit:
            raise LivingReactionStorageError("living reaction all-current protection exceeds hot capacity")
        if len(state["reactions"]) > reaction_limit:
            eligible = [
                row
                for row in state["reactions"].values()
                if isinstance(row, dict) and str(row.get("reaction_id")) not in protected_reactions
            ]
            eligible.sort(key=lambda row: (str(row.get("created_at") or ""), int(row.get("situation_revision") or 0), str(row.get("reaction_id") or "")))
            needed = len(state["reactions"]) - reaction_limit
            if len(eligible) < needed:
                raise LivingReactionStorageError("living reaction hot capacity is fully protected")
            selected["reactions"] = [copy.deepcopy(row) for row in eligible[:needed]]

        protected_feedback = self._protected_feedback_ids(state)
        if len(protected_feedback) > feedback_limit:
            raise LivingReactionStorageError("living reaction all-current feedback protection exceeds hot capacity")
        if len(state["feedback"]) > feedback_limit:
            eligible = [
                row
                for row in state["feedback"].values()
                if isinstance(row, dict) and str(row.get("feedback_id")) not in protected_feedback
            ]
            eligible.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("feedback_id") or "")))
            needed = len(state["feedback"]) - feedback_limit
            if len(eligible) < needed:
                raise LivingReactionStorageError("living reaction feedback hot capacity is fully protected")
            selected["feedback"] = [copy.deepcopy(row) for row in eligible[:needed]]

        current_policy_numbers = {
            int(row.get("policy_revision"))
            for row in state["category_policies"].values()
            if isinstance(row, Mapping) and isinstance(row.get("policy_revision"), int)
        }
        if len(state["policy_revisions"]) > policy_limit:
            eligible = [
                row
                for row in state["policy_revisions"].values()
                if isinstance(row, dict) and int((row.get("new") or {}).get("policy_revision") or 0) not in current_policy_numbers
            ]
            eligible.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("policy_revision_id") or "")))
            needed = len(state["policy_revisions"]) - policy_limit
            if len(eligible) < needed:
                raise LivingReactionStorageError("living reaction policy revision hot capacity is fully protected")
            selected["policy_revisions"] = [copy.deepcopy(row) for row in eligible[:needed]]

        if not any(selected.values()):
            return

        self.archive.append(selected, now=self._clock())
        for kind in selected:
            key_name = {"reactions": "reaction_id", "feedback": "feedback_id", "policy_revisions": "policy_revision_id"}[kind]
            for row in selected[kind]:
                identity = str(row[key_name])
                state[kind].pop(identity, None)
                if kind == "reactions":
                    state["idempotency_index"].pop(str(row.get("idempotency_key")), None)
        state["metrics"]["reaction_count"] = len(state["reactions"])
        state["metrics"]["feedback_count"] = len(state["feedback"])
        state["metrics"]["disposition_counts"] = {
            disposition: sum(1 for row in state["reactions"].values() if row.get("disposition") == disposition)
            for disposition in REACTION_DISPOSITIONS
        }

    def _effective_policy(self, state: Mapping[str, Any], reaction: ReactionInput) -> dict[str, Any]:
        category = reaction.situation.category
        selected = dict(DEFAULT_FEEDBACK_POLICY)
        now = reaction.now
        category_policy = state["category_policies"].get(self._category_key(reaction.owner_id, category))
        if isinstance(category_policy, Mapping) and not self._expired(category_policy.get("expires_at"), now):
            selected.update(category_policy)
        scope = self._scope_key(reaction.owner_id, reaction.session_id, reaction.situation.situation_id)
        situation_policy = state["situation_effects"].get(scope)
        if isinstance(situation_policy, Mapping) and not self._expired(situation_policy.get("expires_at"), now):
            selected.update(situation_policy)
        selected.update(reaction.feedback_policy)
        return normalise_policy(selected)

    @staticmethod
    def _expired(value: Any, now: datetime) -> bool:
        if not value:
            return False
        try:
            selected = parse_time(value, field_name="policy expiry")
        except LivingReactionValidationError:
            return True
        return selected <= now.astimezone(timezone.utc)

    @staticmethod
    def _scope_key(owner_id: str, session_id: str, situation_id: str) -> str:
        return f"{owner_id}\u001f{session_id}\u001f{situation_id}"

    @staticmethod
    def _category_key(owner_id: str, category: str) -> str:
        return "cat_" + stable_digest(
            "veyra.living_reaction.category_scope.v1",
            {"owner_id": owner_id, "category": category},
        )[:32]

    def _record_category_sample(
        self,
        state: dict[str, Any],
        *,
        owner_id: str,
        category: str,
        label: str,
        situation_id: str,
        feedback_id: str,
        reaction_id: str,
    ) -> dict[str, Any]:
        key = self._category_key(owner_id, category)
        if key not in state["category_samples"] and len(state["category_samples"]) >= self.MAX_CATEGORY_BUCKETS:
            raise LivingReactionStorageError("living reaction category sample capacity exhausted")
        bucket = state["category_samples"].setdefault(
            key,
            {"owner_id": owner_id, "category": category, "labels": {}},
        )
        labels = bucket.setdefault("labels", {})
        if label not in labels and len(labels) >= self.MAX_CATEGORY_LABELS:
            raise LivingReactionStorageError("living reaction category label capacity exhausted")
        row = labels.setdefault(label, {"situation_ids": [], "feedback_ids": [], "reaction_ids": []})
        if situation_id not in row["situation_ids"]:
            if len(row["situation_ids"]) >= self.MAX_CATEGORY_SITUATIONS:
                raise LivingReactionStorageError("living reaction category sample capacity exhausted")
            row["situation_ids"].append(situation_id)
            row["feedback_ids"].append(feedback_id)
            row["reaction_ids"].append(reaction_id)
        return copy.deepcopy(row)

    def _maybe_revise_category_policy(
        self,
        state: dict[str, Any],
        *,
        owner_id: str,
        category: str,
        label: str,
        sample: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        situation_ids = list(sample.get("situation_ids") or [])
        if len(situation_ids) < 3:
            return {
                "status": "insufficient_samples",
                "independent_situations": len(situation_ids),
                "required": 3,
                "policy_effect": "none",
            }
        key = self._category_key(owner_id, category)
        old_policy = normalise_policy(state["category_policies"].get(key) or {})
        new_policy = apply_feedback_effect(old_policy, label, now=now)
        new_policy.pop("cooldown_until", None)
        new_policy.pop("suppression_until", None)
        new_policy.pop("updated_at", None)
        new_policy["owner_id"] = owner_id
        new_policy["category"] = category
        new_policy["expires_at"] = time_iso(now + timedelta(seconds=self.CATEGORY_POLICY_TTL_SECONDS))
        new_policy["sample_count"] = len(situation_ids)
        new_policy["evidence_refs"] = list(sample.get("feedback_ids") or [])[-8:]
        new_policy["rollback"] = copy.deepcopy(old_policy)
        digest = stable_digest(
            "veyra.living_reaction.category_policy.v1",
            {"owner_id": owner_id, "category": category, "label": label, "samples": situation_ids, "policy": new_policy},
        )
        revision_id = "rpol_" + digest[:24]
        existing = state["policy_revisions"].get(revision_id)
        if existing is not None:
            return {
                "status": "already_recorded",
                "independent_situations": len(situation_ids),
                "required": 3,
                "policy_revision_id": revision_id,
                "policy_effect": "bounded_revision",
            }
        if len(state["policy_revisions"]) >= self.MAX_POLICY_REVISIONS:
            raise LivingReactionStorageError("living reaction policy revision capacity exhausted")
        revision_number = int(state.get("policy_revision_counter") or 0) + 1
        new_policy["policy_revision"] = revision_number
        policy_revision = {
            "schema_version": "veyra.living_reaction_policy_revision.v1",
            "policy_revision_id": revision_id,
            "owner_id": owner_id,
            "category": category,
            "label": label,
            "old": copy.deepcopy(old_policy),
            "new": copy.deepcopy(new_policy),
            "evidence_refs": list(sample.get("feedback_ids") or [])[-8:],
            "independent_situations": situation_ids[-8:],
            "created_at": time_iso(now),
            "expires_at": new_policy["expires_at"],
            "rollback": copy.deepcopy(old_policy),
            "authority": self._authority(),
        }
        state["policy_revisions"][revision_id] = policy_revision
        state["category_policies"][key] = new_policy
        state["policy_revision_counter"] = revision_number
        return {
            "status": "revised",
            "independent_situations": len(situation_ids),
            "required": 3,
            "policy_revision_id": revision_id,
            "policy_effect": "bounded_revision",
            "old": copy.deepcopy(old_policy),
            "new": copy.deepcopy(new_policy),
            "expires_at": new_policy["expires_at"],
            "rollback": copy.deepcopy(old_policy),
            "evidence_refs": list(sample.get("feedback_ids") or [])[-8:],
        }

    def _ensure_state(self, state: dict[str, Any], *, archive: Mapping[str, Any] | None = None) -> None:
        if not isinstance(state, dict):
            raise LivingReactionStorageError("living reaction state is not an object")
        if not state:
            path_for = getattr(self.state_store, "path_for", None)
            path = path_for(self.STATE_FILE) if callable(path_for) else None
            if path is not None and path.exists():
                raise LivingReactionStorageError("living reaction state is empty")
            state.update(self._empty_state())
        if state.get("schema_version") != self.STATE_SCHEMA:
            raise LivingReactionStorageError("living reaction state schema is unsupported")
        for key in (
            "reactions",
            "idempotency_index",
            "feedback",
            "situation_effects",
            "category_samples",
            "category_policies",
            "policy_revisions",
            "metrics",
        ):
            if not isinstance(state.get(key), dict):
                raise LivingReactionStorageError(f"living reaction state field {key} is invalid")
        if state.get("authority") != self._authority():
            raise LivingReactionStorageError("living reaction state authority boundary is invalid")
        if len(state["reactions"]) > self.MAX_REACTIONS or len(state["feedback"]) > self.MAX_FEEDBACK:
            raise LivingReactionStorageError("living reaction state exceeds bounded capacity")
        if len(state["situation_effects"]) > self.MAX_SITUATION_EFFECTS or len(state["category_samples"]) > self.MAX_CATEGORY_BUCKETS or len(state["category_policies"]) > self.MAX_CATEGORY_BUCKETS or len(state["policy_revisions"]) > self.MAX_POLICY_REVISIONS:
            raise LivingReactionStorageError("living reaction auxiliary state exceeds bounded capacity")
        self._validate_metrics(state["metrics"], state)
        self._validate_reactions(state["reactions"], state["idempotency_index"])
        selected_archive = archive if archive is not None else self._archive_snapshot()
        archived_reactions = selected_archive.get("reactions") if isinstance(selected_archive, Mapping) else {}
        archived_feedback = selected_archive.get("feedback") if isinstance(selected_archive, Mapping) else {}
        archived_policies = selected_archive.get("policy_revisions") if isinstance(selected_archive, Mapping) else {}
        if not isinstance(archived_reactions, Mapping) or not isinstance(archived_feedback, Mapping) or not isinstance(archived_policies, Mapping):
            raise LivingReactionStorageError("living reaction archive projection is invalid")
        combined_reactions = self._merge_archived_rows(state["reactions"], archived_reactions, label="reaction")
        combined_feedback = self._merge_archived_rows(state["feedback"], archived_feedback, label="feedback")
        combined_policies = self._merge_archived_rows(state["policy_revisions"], archived_policies, label="policy revision")
        archived_index = {str(row.get("idempotency_key")): str(row.get("reaction_id")) for row in archived_reactions.values() if isinstance(row, Mapping)}
        self._validate_reactions(archived_reactions, archived_index)
        self._validate_feedback(combined_feedback, combined_reactions)
        self._validate_policy_revisions(combined_policies, state["category_policies"], allow_archived=True)
        self._validate_effects(state["situation_effects"])
        self._validate_category_state(state)

    def _validate_metrics(self, metrics: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        if metrics.get("reaction_count") != len(state["reactions"]) or metrics.get("feedback_count") != len(state["feedback"]):
            raise LivingReactionStorageError("living reaction metrics are inconsistent")
        counts = metrics.get("disposition_counts")
        if not isinstance(counts, dict) or set(counts) != set(REACTION_DISPOSITIONS):
            raise LivingReactionStorageError("living reaction disposition metrics are invalid")
        total = 0
        for value in counts.values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LivingReactionStorageError("living reaction disposition count is invalid")
            total += value
        if total != len(state["reactions"]):
            raise LivingReactionStorageError("living reaction disposition metrics do not reconcile")
        counter = state.get("policy_revision_counter", 0)
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise LivingReactionStorageError("living reaction policy revision counter is invalid")

    def _validate_reactions(self, reactions: Mapping[str, Any], index: Mapping[str, Any]) -> None:
        if len(index) != len(reactions) or set(index.values()) != set(reactions):
            raise LivingReactionStorageError("living reaction idempotency index is inconsistent")
        for reaction_id, row in reactions.items():
            if not isinstance(reaction_id, str) or not isinstance(row, dict) or row.get("reaction_id") != reaction_id:
                raise LivingReactionStorageError("living reaction record identity is invalid")
            if row.get("schema_version") != REACTION_SCHEMA or row.get("authority") != self._authority():
                raise LivingReactionStorageError("living reaction record schema or authority is invalid")
            if row.get("external_delivery") is not False or row.get("record_only") is not True or row.get("ledger_status") != "recorded":
                raise LivingReactionStorageError("living reaction record authority is invalid")
            for key in ("idempotency_key", "owner_id", "session_id", "situation_id", "category", "reason", "what_happened", "why_it_matters", "why_now", "created_at"):
                if not isinstance(row.get(key), str) or not row[key]:
                    raise LivingReactionStorageError(f"living reaction {key} is invalid")
            if len(row["idempotency_key"]) != 64:
                raise LivingReactionStorageError("living reaction idempotency key is invalid")
            if not isinstance(row.get("suggested_next_step"), str):
                raise LivingReactionStorageError("living reaction suggested_next_step is invalid")
            if row.get("disposition") not in REACTION_DISPOSITIONS:
                raise LivingReactionStorageError("living reaction disposition is invalid")
            revision = row.get("situation_revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise LivingReactionStorageError("living reaction situation revision is invalid")
            rank = row.get("rank")
            if isinstance(rank, bool) or not isinstance(rank, (int, float)) or not 0 <= float(rank) <= 1:
                raise LivingReactionStorageError("living reaction rank is invalid")
            try:
                parse_time(row["created_at"], field_name="reaction.created_at")
            except LivingReactionValidationError as exc:
                raise LivingReactionStorageError("living reaction timestamp is invalid") from exc
            facts = row.get("fact_vs_inference")
            if not isinstance(facts, dict) or set(facts) != {"facts", "inferences"} or any(not isinstance(facts[key], list) or len(facts[key]) > 8 or any(not isinstance(item, str) for item in facts[key]) for key in ("facts", "inferences")):
                raise LivingReactionStorageError("living reaction fact/inference boundary is invalid")
            if not isinstance(row.get("cooldown"), dict) or not isinstance(row.get("suppression"), dict) or not isinstance(row.get("timing"), dict):
                raise LivingReactionStorageError("living reaction explanation fields are invalid")
            expected = "react_" + stable_digest("veyra.living_reaction.id.v1", row["idempotency_key"])[:24]
            if expected != reaction_id:
                raise LivingReactionStorageError("living reaction ID is not content-derived")
            if index.get(row["idempotency_key"]) != reaction_id:
                raise LivingReactionStorageError("living reaction idempotency index points to wrong record")

    def _validate_feedback(self, feedback: Mapping[str, Any], reactions: Mapping[str, Any]) -> None:
        for feedback_id, row in feedback.items():
            if not isinstance(feedback_id, str) or not isinstance(row, dict) or row.get("feedback_id") != feedback_id:
                raise LivingReactionStorageError("living reaction feedback identity is invalid")
            if row.get("schema_version") != "veyra.living_reaction_feedback.v1" or row.get("authority") != self._authority():
                raise LivingReactionStorageError("living reaction feedback schema or authority is invalid")
            semantics = row.get("semantics")
            if not isinstance(semantics, dict) or set(semantics) != {"feedback_id", "owner_id", "session_id", "situation_id", "reaction_id", "label", "category", "remind_before_seconds"} or semantics.get("feedback_id") != feedback_id or semantics.get("label") not in FEEDBACK_LABELS:
                raise LivingReactionStorageError("living reaction feedback semantics are invalid")
            target = reactions.get(semantics.get("reaction_id"))
            if not isinstance(target, dict):
                raise LivingReactionStorageError("living reaction feedback target is missing")
            for key in ("owner_id", "session_id", "situation_id"):
                if semantics.get(key) != target.get(key):
                    raise LivingReactionStorageError("living reaction feedback scope is invalid")
            if not isinstance(semantics.get("category"), str) or semantics.get("remind_before_seconds") is not None and (isinstance(semantics["remind_before_seconds"], bool) or not isinstance(semantics["remind_before_seconds"], int) or not 0 <= semantics["remind_before_seconds"] <= 30 * 86400):
                raise LivingReactionStorageError("living reaction feedback timing semantics are invalid")
            try:
                parse_time(row.get("created_at"), field_name="feedback.created_at")
            except LivingReactionValidationError as exc:
                raise LivingReactionStorageError("living reaction feedback timestamp is invalid") from exc
            refs = row.get("evidence_refs")
            if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(ref, str) or not ref for ref in refs):
                raise LivingReactionStorageError("living reaction feedback evidence is invalid")
            if not isinstance(row.get("aftereffects"), dict):
                raise LivingReactionStorageError("living reaction feedback aftereffects are invalid")

    def _validate_policy_revisions(
        self,
        revisions: Mapping[str, Any],
        category_policies: Mapping[str, Any],
        *,
        allow_archived: bool = False,
    ) -> None:
        for key, row in revisions.items():
            if not isinstance(key, str) or not isinstance(row, dict) or row.get("policy_revision_id") != key or row.get("authority") != self._authority():
                raise LivingReactionStorageError("living reaction policy revision is invalid")
            if not isinstance(row.get("owner_id"), str) or not isinstance(row.get("category"), str):
                raise LivingReactionStorageError("living reaction policy revision scope is invalid")
            category_key = self._category_key(row["owner_id"], row["category"])
            if not allow_archived and category_key not in category_policies:
                raise LivingReactionStorageError("living reaction policy revision scope is invalid")
            if not isinstance(row.get("old"), dict) or not isinstance(row.get("new"), dict) or not isinstance(row.get("rollback"), dict):
                raise LivingReactionStorageError("living reaction policy revision payload is invalid")
            revision_number = row.get("new", {}).get("policy_revision")
            if isinstance(revision_number, bool) or not isinstance(revision_number, int) or revision_number < 1:
                raise LivingReactionStorageError("living reaction policy revision number is invalid")
            refs = row.get("evidence_refs")
            if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(ref, str) or not ref for ref in refs):
                raise LivingReactionStorageError("living reaction policy revision evidence is invalid")

    def _validate_effects(self, effects: Mapping[str, Any]) -> None:
        for scope, row in effects.items():
            if not isinstance(scope, str) or not isinstance(row, dict) or row.get("scope") != scope:
                raise LivingReactionStorageError("living reaction situation effect identity is invalid")
            if len(scope.split("\u001f")) != 3 or any(not item for item in scope.split("\u001f")):
                raise LivingReactionStorageError("living reaction situation effect scope is invalid")
            revision = row.get("policy_revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise LivingReactionStorageError("living reaction situation effect revision is invalid")

    def _validate_category_state(self, state: Mapping[str, Any]) -> None:
        for collection_name in ("category_samples", "category_policies"):
            for key, row in state[collection_name].items():
                if not isinstance(key, str) or not isinstance(row, dict):
                    raise LivingReactionStorageError("living reaction category state is invalid")
                if not isinstance(row.get("owner_id"), str) or not isinstance(row.get("category"), str) or self._category_key(row["owner_id"], row["category"]) != key:
                    raise LivingReactionStorageError("living reaction category scope is invalid")
        for key, bucket in state["category_samples"].items():
            labels = bucket.get("labels")
            if not isinstance(labels, dict) or len(labels) > self.MAX_CATEGORY_LABELS:
                raise LivingReactionStorageError("living reaction category samples are invalid")
            for label, row in labels.items():
                if label not in FEEDBACK_LABELS or not isinstance(row, dict):
                    raise LivingReactionStorageError("living reaction category sample label is invalid")
                ids = row.get("situation_ids")
                feedback_ids = row.get("feedback_ids")
                reaction_ids = row.get("reaction_ids")
                if not isinstance(ids, list) or not isinstance(feedback_ids, list) or not isinstance(reaction_ids, list) or not len(ids) == len(feedback_ids) == len(reaction_ids) or len(ids) > self.MAX_CATEGORY_SITUATIONS or any(not isinstance(item, str) or not item for item in (*ids, *feedback_ids, *reaction_ids)):
                    raise LivingReactionStorageError("living reaction category sample lists are invalid")
        for key, row in state["category_policies"].items():
            if not isinstance(row.get("policy_revision"), int) or row["policy_revision"] < 1 or not isinstance(row.get("expires_at"), str):
                raise LivingReactionStorageError("living reaction category policy is invalid")
        for key, row in state["policy_revisions"].items():
            if not isinstance(key, str) or not isinstance(row, dict) or row.get("policy_revision_id") != key or row.get("authority") != self._authority():
                raise LivingReactionStorageError("living reaction policy revision is invalid")
            if not isinstance(row.get("owner_id"), str) or not isinstance(row.get("category"), str) or self._category_key(row["owner_id"], row["category"]) not in state["category_policies"]:
                raise LivingReactionStorageError("living reaction policy revision scope is invalid")
            if not isinstance(row.get("old"), dict) or not isinstance(row.get("new"), dict) or not isinstance(row.get("rollback"), dict):
                raise LivingReactionStorageError("living reaction policy revision payload is invalid")

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": "veyra.living_reaction_state.v1",
            "reactions": {},
            "idempotency_index": {},
            "feedback": {},
            "situation_effects": {},
            "category_samples": {},
            "category_policies": {},
            "policy_revisions": {},
            "policy_revision_counter": 0,
            "metrics": {
                "reaction_count": 0,
                "feedback_count": 0,
                "disposition_counts": {key: 0 for key in ("ask", "read", "wait", "silent", "suggest")},
            },
            "authority": LivingReactionRuntime._authority(),
        }

    @staticmethod
    def _authority() -> dict[str, bool]:
        return {"execution": False, "external_delivery": False, "permission_expansion": False}

    def _envelope(self, result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **copy.deepcopy(dict(result)),
            "authority": self._authority(),
            "external_delivery": False,
            "record_only": True,
        }
