from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent
from interface.general_situation_contract import (
    ChildSituationRef,
    StructuredAnchor,
    WORKSPACE_ANCHOR_KIND,
    stable_digest,
)
from memory_bridge.scope import normalize_scope_component


class GeneralSituationRuntime:
    """Deterministically group event-scoped Situations without authority.

    The event-scoped Situation remains the source of truth. A general
    Situation never embeds a child observation, inference, decision, outcome,
    or message. Its ``child_refs`` contain exactly the immutable four-field
    :class:`ChildSituationRef` projection.

    Free text and model similarity are deliberately absent from matching.
    Events must share a structured anchor inside an effective-time window.
    Crossing sessions additionally requires an active durable Goal or
    Commitment owned by the same user.
    """

    STATE_FILE = "general_situation_state.json"
    SCHEMA_VERSION = "veyra.general_situation_state.v1"
    RECORD_SCHEMA_VERSION = "veyra.general_situation.v1"
    WINDOW_SECONDS = 24 * 60 * 60
    PARENT_TTL_SECONDS = 7 * 24 * 60 * 60
    MAX_GENERAL_SITUATIONS = 500
    MAX_CANDIDATES = 1000
    MAX_CHILD_REFS = 64

    _DIRECT_ANCHORS = {
        "goal_id": "goal",
        "commitment_id": "commitment",
        "case_id": "case",
        "task_id": "task",
        "trace_id": "trace",
        "entity_id": "entity",
        "workspace_id": "workspace",
    }
    _COLLECTION_ANCHORS = {
        "goal_refs": "goal",
        "commitment_refs": "commitment",
        "case_refs": "case",
        "task_refs": "task",
        "trace_refs": "trace",
        "entity_refs": "entity",
        "workspace_refs": "workspace",
    }
    _GOAL_TERMINAL = {"archived", "cancelled", "closed", "completed", "expired"}
    _COMMITMENT_DURABLE = {"active", "paused"}

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest_child(
        self,
        situation: dict[str, Any],
        *,
        event: VeyraEvent | None = None,
        expected_state_revision: int | None = None,
    ) -> dict[str, Any]:
        """Ingest one persisted child snapshot into the observational graph."""

        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return self._closed("general_situation_state_corrupt")
        try:
            child = self._validated_child(situation, event=event)
            anchors = self._anchors(situation, event=event)
            workspace_anchor_key = self._workspace_anchor_key(anchors)
            effective_start, effective_end = self._effective_interval(
                situation,
                event=event,
            )
        except (TypeError, ValueError) as exc:
            return self._closed(
                "invalid_child_situation",
                detail=type(exc).__name__,
            )
        if not anchors:
            return {
                "status": "not_grouped",
                "reason": "structured_anchor_required",
                "child_ref": child.to_dict(),
                "general_situation": None,
                "authority": self._authority_boundary(),
            }
        if expected_state_revision is not None and (
            isinstance(expected_state_revision, bool)
            or not isinstance(expected_state_revision, int)
            or expected_state_revision < 0
        ):
            return self._closed("invalid_expected_state_revision")

        user_id = normalize_scope_component(
            situation.get("user_id"),
            "user_id",
        )
        session_id = normalize_scope_component(
            situation.get("session_id"),
            "session_id",
        )
        scope_key = tenant_scope_storage_key(user_id, session_id)
        durable_anchors = self._valid_durable_anchors(
            user_id=user_id,
            anchors=anchors,
        )
        now = self._now()
        result: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if not self._healthy_state(current):
                result = self._closed("general_situation_state_corrupt")
                return current
            current_revision = self._nonnegative_int(
                current.get("_state_revision")
            )
            if (
                expected_state_revision is not None
                and expected_state_revision != current_revision
            ):
                result = {
                    "status": "cas_conflict",
                    "reason": "expected_state_revision_mismatch",
                    "expected_state_revision": expected_state_revision,
                    "current_state_revision": current_revision,
                    "authority": self._authority_boundary(),
                }
                return current

            parents = self._records(current.get("general_situations"))
            candidates = self._records(current.get("candidates"))
            child_index = self._records(current.get("child_index"))
            self._expire_records(parents, candidates, now=now)

            indexed = child_index.get(child.situation_id)
            if isinstance(indexed, dict):
                binding_issue = self._child_index_binding_issue(
                    indexed,
                    user_id=user_id,
                    session_id=session_id,
                    scope_key=scope_key,
                    workspace_anchor_key=workspace_anchor_key,
                )
                if binding_issue is not None:
                    result = self._closed(binding_issue)
                    return current
                indexed_parent_id = str(
                    indexed.get("general_situation_id") or ""
                )
                indexed_candidate_id = str(indexed.get("candidate_id") or "")
                if indexed_parent_id:
                    indexed_parent = parents.get(indexed_parent_id)
                    parent_issue = self._indexed_parent_binding_issue(
                        indexed_parent,
                        child=child,
                        user_id=user_id,
                        scope_key=scope_key,
                        workspace_anchor_key=workspace_anchor_key,
                    )
                    if parent_issue is not None:
                        result = self._closed(parent_issue)
                        return current
                elif indexed_candidate_id:
                    indexed_candidate = candidates.get(indexed_candidate_id)
                    candidate_issue = self._indexed_candidate_binding_issue(
                        indexed_candidate,
                        child=child,
                        user_id=user_id,
                        session_id=session_id,
                        scope_key=scope_key,
                        workspace_anchor_key=workspace_anchor_key,
                    )
                    if candidate_issue is not None:
                        result = self._closed(candidate_issue)
                        return current
                else:
                    result = self._closed("child_graph_binding_missing")
                    return current
                indexed_event = str(indexed.get("source_event_id") or "")
                indexed_revision = self._nonnegative_int(
                    indexed.get("max_observation_revision")
                )
                indexed_digest = str(indexed.get("digest") or "")
                if indexed_event != child.source_event_id:
                    result = self._closed("child_source_event_binding_conflict")
                    return current
                if child.observation_revision < indexed_revision:
                    result = {
                        "status": "stale",
                        "reason": "observation_revision_out_of_order",
                        "child_ref": child.to_dict(),
                        "authority": self._authority_boundary(),
                    }
                    return current
                if child.observation_revision == indexed_revision:
                    if indexed_digest != child.digest:
                        result = self._closed(
                            "observation_revision_digest_conflict"
                        )
                    else:
                        parent_id = str(indexed.get("general_situation_id") or "")
                        result = {
                            "status": "replayed",
                            "reason": "child_revision_already_ingested",
                            "child_ref": child.to_dict(),
                            "general_situation": copy.deepcopy(
                                parents.get(parent_id)
                            )
                            if parent_id in parents
                            else None,
                            "authority": self._authority_boundary(),
                        }
                    return current
            elif self._graph_contains_child(
                parents,
                candidates,
                child.situation_id,
            ):
                result = self._closed("child_owner_binding_missing")
                return current

            parent = self._parent_for_existing_child(
                parents,
                child=child,
                user_id=user_id,
                scope_key=scope_key,
                workspace_anchor_key=workspace_anchor_key,
            )
            if parent is None:
                parent = self._matching_parent(
                    parents,
                    user_id=user_id,
                    scope_key=scope_key,
                    workspace_anchor_key=workspace_anchor_key,
                    anchors=anchors,
                    durable_anchors=durable_anchors,
                    effective_start=effective_start,
                    effective_end=effective_end,
                )
            if parent is not None:
                preserve_common_anchors = self._parent_contains_situation(
                    parent,
                    child.situation_id,
                )
                if preserve_common_anchors and not self._revision_preserves_parent_anchors(
                    parent,
                    anchors=anchors,
                ):
                    result = self._closed(
                        "child_parent_anchor_binding_conflict"
                    )
                    return current
                parent = self._append_child(
                    parent,
                    child=child,
                    scope_key=scope_key,
                    workspace_anchor_key=workspace_anchor_key,
                    anchors=anchors,
                    effective_start=effective_start,
                    effective_end=effective_end,
                    now=now,
                    preserve_common_anchors=preserve_common_anchors,
                )
                parents[str(parent["general_situation_id"])] = parent
                status = "updated"
            else:
                candidate = self._matching_candidate(
                    candidates,
                    child=child,
                    user_id=user_id,
                    scope_key=scope_key,
                    workspace_anchor_key=workspace_anchor_key,
                    anchors=anchors,
                    durable_anchors=durable_anchors,
                    effective_start=effective_start,
                    effective_end=effective_end,
                )
                if candidate is not None:
                    parent = self._create_parent(
                        candidate,
                        child=child,
                        user_id=user_id,
                        scope_key=scope_key,
                        workspace_anchor_key=workspace_anchor_key,
                        anchors=anchors,
                        durable_anchors=durable_anchors,
                        effective_start=effective_start,
                        effective_end=effective_end,
                        now=now,
                    )
                    parents[str(parent["general_situation_id"])] = parent
                    candidates.pop(str(candidate.get("candidate_id") or ""), None)
                    prior_ref = ChildSituationRef.from_dict(
                        candidate.get("child_ref") or {}
                    )
                    prior_index = child_index.get(prior_ref.situation_id)
                    if isinstance(prior_index, dict):
                        prior_issue = self._child_index_binding_issue(
                            prior_index,
                            user_id=str(candidate.get("user_id") or ""),
                            session_id=str(candidate.get("session_id") or ""),
                            scope_key=str(
                                candidate.get("session_scope_key") or ""
                            ),
                            workspace_anchor_key=self._stored_workspace_key(
                                candidate
                            ),
                        )
                        if prior_issue is not None:
                            result = self._closed(prior_issue)
                            return current
                        prior_index["general_situation_id"] = str(
                            parent["general_situation_id"]
                        )
                        prior_index["candidate_id"] = ""
                    else:
                        result = self._closed("child_owner_binding_missing")
                        return current
                    status = "created"
                else:
                    candidate = self._candidate_record(
                        child=child,
                        user_id=user_id,
                        scope_key=scope_key,
                        session_id=session_id,
                        workspace_anchor_key=workspace_anchor_key,
                        anchors=anchors,
                        effective_start=effective_start,
                        effective_end=effective_end,
                        now=now,
                    )
                    candidates[str(candidate["candidate_id"])] = candidate
                    parent = None
                    status = "awaiting_distinct_event"

            if len(parents) > self.MAX_GENERAL_SITUATIONS:
                result = self._closed("general_situation_capacity_exhausted")
                return current
            if len(candidates) > self.MAX_CANDIDATES:
                result = self._closed("general_situation_candidate_capacity_exhausted")
                return current

            child_index[child.situation_id] = {
                "user_id": user_id,
                "session_id": session_id,
                "session_scope_key": scope_key,
                "workspace_anchor_key": workspace_anchor_key,
                "source_event_id": child.source_event_id,
                "max_observation_revision": child.observation_revision,
                "digest": child.digest,
                "general_situation_id": (
                    str(parent.get("general_situation_id") or "")
                    if isinstance(parent, dict)
                    else ""
                ),
                "candidate_id": (
                    ""
                    if isinstance(parent, dict)
                    else str(candidate.get("candidate_id") or "")
                ),
            }
            current["schema_version"] = self.SCHEMA_VERSION
            current["general_situations"] = parents
            current["general_situation_count"] = len(parents)
            current["candidates"] = candidates
            current["candidate_count"] = len(candidates)
            current["child_index"] = child_index
            current["child_index_count"] = len(child_index)
            current["updated_at"] = now.isoformat()
            result = {
                "status": status,
                "child_ref": child.to_dict(),
                "general_situation": copy.deepcopy(parent),
                "general_situation_count": len(parents),
                "candidate_count": len(candidates),
                "cas_applied": True,
                "authority": self._authority_boundary(),
            }
            return current

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result or self._closed("general_situation_mutation_no_result")

    def reconcile(
        self,
        situations: Iterable[dict[str, Any]],
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Replay persisted children after restart; every child is idempotent."""

        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return self._closed("general_situation_state_corrupt")
        processed = 0
        replayed = 0
        grouped = 0
        failed = 0
        for child in list(situations)[: max(0, min(int(limit), 500))]:
            if not isinstance(child, dict):
                failed += 1
                continue
            result = self.ingest_child(child)
            processed += 1
            status = str(result.get("status") or "")
            replayed += int(status == "replayed")
            grouped += int(status in {"created", "updated"})
            failed += int(status in {"fail_closed", "cas_conflict"})
        return {
            "status": "success" if failed == 0 else "degraded",
            "processed_count": processed,
            "replayed_count": replayed,
            "grouped_count": grouped,
            "failed_count": failed,
            "authority": self._authority_boundary(),
        }

    def list_for_owner(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pure exact-owner read; expiry is projected without mutation."""

        selected_user = normalize_scope_component(user_id, "user_id")
        selected_session = normalize_scope_component(session_id, "session_id")
        scope_key = tenant_scope_storage_key(selected_user, selected_session)
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return self._closed("general_situation_state_corrupt")
        now = self._now()
        items: list[dict[str, Any]] = []
        for item in self._records(state.get("general_situations")).values():
            if str(item.get("user_id") or "") != selected_user:
                continue
            scope_keys = item.get("session_scope_keys")
            if not isinstance(scope_keys, list) or scope_key not in scope_keys:
                continue
            projected = self._public_parent(item, now=now)
            items.append(projected)
        items.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("general_situation_id") or ""),
            ),
            reverse=True,
        )
        selected_limit = max(0, min(int(limit), 500))
        return {
            "status": "success",
            "count": min(len(items), selected_limit),
            "items": items[:selected_limit],
            "state_revision": self._nonnegative_int(state.get("_state_revision")),
            "authority": self._authority_boundary(),
        }

    def semantic_similarity_hypothesis(
        self,
        *,
        user_id: str,
        session_id: str,
        model_source: str,
        similarity: Any,
        model_valid: bool,
    ) -> dict[str, Any]:
        """Describe model similarity as a non-persisted, non-merging hint."""

        normalize_scope_component(user_id, "user_id")
        normalize_scope_component(session_id, "session_id")
        if not model_valid or str(model_source or "") not in {
            "model",
            "model_repair",
        }:
            return {
                "status": "rejected",
                "reason": "validated_model_hypothesis_required",
                "merge_allowed": False,
                "causality_asserted": False,
            }
        if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
            return {
                "status": "rejected",
                "reason": "numeric_similarity_required",
                "merge_allowed": False,
                "causality_asserted": False,
            }
        score = float(similarity)
        if not 0.0 <= score <= 1.0:
            return {
                "status": "rejected",
                "reason": "similarity_out_of_range",
                "merge_allowed": False,
                "causality_asserted": False,
            }
        return {
            "status": "hypothesis_only",
            "similarity": score,
            "merge_allowed": False,
            "causality_asserted": False,
            "persisted": False,
        }

    @classmethod
    def child_digest(cls, situation: dict[str, Any]) -> str:
        # ``observation_revision`` belongs to the observation stream. Decision,
        # prediction and outcome transitions intentionally do not advance it in
        # SituationEvaluator, so none of those mutable result fields may alter
        # the immutable observational reference (or leak into this parent).
        observation_fields = (
            "record_kind",
            "situation_id",
            "observation_revision",
            "observation_sequence",
            "observation_id",
            "source_observation_fingerprint",
            "source_event_id",
            "source_event_type",
            "source_event",
            "correlation_id",
            "user_id",
            "session_id",
            "channel",
            "goal_refs",
            "commitment_refs",
            "structured_anchor_refs",
            "context_bindings",
            "salience_components",
            "salience_score",
            "observations",
            "inferences",
        )
        stable = {
            key: copy.deepcopy(situation.get(key))
            for key in observation_fields
            if key in situation
        }
        return stable_digest("veyra.event_scoped_situation.snapshot.v1", stable)

    def _validated_child(
        self,
        situation: dict[str, Any],
        *,
        event: VeyraEvent | None,
    ) -> ChildSituationRef:
        if not isinstance(situation, dict):
            raise TypeError("child situation must be a mapping")
        user_id = normalize_scope_component(situation.get("user_id"), "user_id")
        session_id = normalize_scope_component(
            situation.get("session_id"),
            "session_id",
        )
        situation_id = str(situation.get("situation_id") or "").strip()
        source_event_id = str(situation.get("source_event_id") or "").strip()
        raw_revision = situation.get("observation_revision")
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
            raise ValueError("child observation revision is invalid")
        if event is not None:
            event_user = normalize_scope_component(event.source.user_id, "user_id")
            event_session = normalize_scope_component(
                event.source.session_id,
                "session_id",
            )
            if (event_user, event_session) != (user_id, session_id):
                raise ValueError("event and child owner scopes differ")
            if str(event.event_id or "") != source_event_id:
                raise ValueError("event and child source event differ")
        return ChildSituationRef(
            situation_id=situation_id,
            observation_revision=raw_revision,
            source_event_id=source_event_id,
            digest=self.child_digest(situation),
        )

    def _anchors(
        self,
        situation: dict[str, Any],
        *,
        event: VeyraEvent | None,
    ) -> dict[str, StructuredAnchor]:
        output: dict[str, StructuredAnchor] = {}
        if "structured_anchor_refs" in situation:
            persisted = situation.get("structured_anchor_refs")
            if not isinstance(persisted, list):
                raise ValueError("structured_anchor_refs must be a list")
            for item in persisted:
                anchor = StructuredAnchor.from_dict(item)
                output[anchor.key] = anchor
            # New event-scoped Situations make the persisted projection the
            # sole anchor authority. This keeps first ingest and restart
            # reconcile identical even if a caller passes a changed event.
            return output
        for key, kind in (("goal_refs", "goal"), ("commitment_refs", "commitment")):
            for anchor in self._anchor_values(situation.get(key), default_kind=kind):
                output[anchor.key] = anchor
        correlation = self._identifier(situation.get("correlation_id"))
        source_event_id = self._identifier(situation.get("source_event_id"))
        if correlation and correlation != source_event_id:
            anchor = StructuredAnchor("trace", correlation)
            output[anchor.key] = anchor
        if event is None:
            return output
        subjects = event.subject if isinstance(event.subject, list) else [event.subject]
        for subject in subjects:
            if not isinstance(subject, dict):
                continue
            kind = str(
                subject.get("kind")
                or subject.get("type")
                or subject.get("subject_type")
                or ""
            ).strip().lower().removesuffix("_ref").removesuffix("_reference")
            ref_id = self._identifier(
                subject.get("ref_id")
                or subject.get("id")
                or subject.get("value")
            )
            try:
                anchor = StructuredAnchor(kind, ref_id)
            except ValueError:
                continue
            output[anchor.key] = anchor
        payload = event.payload if isinstance(event.payload, dict) else {}
        for field, kind in self._DIRECT_ANCHORS.items():
            ref_id = self._identifier(payload.get(field))
            if ref_id:
                anchor = StructuredAnchor(kind, ref_id)
                output[anchor.key] = anchor
        for field, kind in self._COLLECTION_ANCHORS.items():
            for anchor in self._anchor_values(payload.get(field), default_kind=kind):
                output[anchor.key] = anchor
        return output

    def _anchor_values(
        self,
        value: Any,
        *,
        default_kind: str,
    ) -> list[StructuredAnchor]:
        values = value if isinstance(value, list) else [value]
        output: list[StructuredAnchor] = []
        for item in values:
            if isinstance(item, dict):
                kind = str(
                    item.get("kind")
                    or item.get("type")
                    or default_kind
                ).strip().lower().removesuffix("_ref").removesuffix("_reference")
                ref_id = self._identifier(
                    item.get("ref_id")
                    or item.get("id")
                    or item.get(f"{default_kind}_id")
                    or item.get("value")
                )
            else:
                kind = default_kind
                ref_id = self._identifier(item)
            try:
                output.append(StructuredAnchor(kind, ref_id))
            except ValueError:
                continue
        return output

    @staticmethod
    def _merge_anchor_keys(keys: set[str]) -> set[str]:
        """Return anchors that can establish aggregation identity.

        A workspace is an isolation boundary, not evidence that two events
        describe one Situation. It may narrow a match, but can never create it.
        """

        workspace_prefix = f"{WORKSPACE_ANCHOR_KIND}:"
        return {key for key in keys if not key.startswith(workspace_prefix)}

    @staticmethod
    def _workspace_anchor_key(
        anchors: dict[str, StructuredAnchor],
    ) -> str | None:
        matches = sorted(
            anchor.key
            for anchor in anchors.values()
            if anchor.kind == WORKSPACE_ANCHOR_KIND
        )
        if len(matches) > 1:
            raise ValueError("one child Situation cannot bind multiple workspaces")
        return matches[0] if matches else None

    @classmethod
    def _stored_workspace_key(cls, record: dict[str, Any]) -> str | None:
        if "workspace_anchor_key" in record:
            raw = record.get("workspace_anchor_key")
            if raw is None or raw == "":
                return None
            selected = str(raw)
            prefix = f"{WORKSPACE_ANCHOR_KIND}:"
            if not selected.startswith(prefix):
                raise ValueError("persisted workspace binding is invalid")
            anchor = StructuredAnchor(
                WORKSPACE_ANCHOR_KIND,
                selected[len(prefix) :],
            )
            if anchor.key != selected:
                raise ValueError("persisted workspace binding is not canonical")
            return selected

        # Compatibility for records written before the dedicated binding
        # field existed. Exact typed anchor keys are the only recovery source.
        prefix = f"{WORKSPACE_ANCHOR_KIND}:"
        recovered = {
            key
            for field in ("anchor_keys", "common_anchor_keys")
            for key in cls._string_list(record.get(field))
            if key.startswith(prefix)
        }
        if len(recovered) > 1:
            raise ValueError("persisted record binds multiple workspaces")
        if not recovered:
            return None
        selected = next(iter(recovered))
        anchor = StructuredAnchor(
            WORKSPACE_ANCHOR_KIND,
            selected[len(prefix) :],
        )
        if anchor.key != selected:
            raise ValueError("persisted workspace anchor is not canonical")
        return selected

    @staticmethod
    def _workspace_compatible(
        stored: str | None,
        incoming: str | None,
    ) -> bool:
        # The presence of a workspace on either side makes it an exact
        # boundary. This prevents legacy owner-only records from absorbing a
        # workspace-bound child.
        return stored == incoming

    def _child_index_binding_issue(
        self,
        indexed: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
    ) -> str | None:
        required = {
            "user_id",
            "session_id",
            "session_scope_key",
            "workspace_anchor_key",
        }
        if not required.issubset(indexed):
            return "child_owner_binding_missing"
        if (
            not str(indexed.get("user_id") or "")
            or not str(indexed.get("session_id") or "")
            or not str(indexed.get("session_scope_key") or "")
        ):
            return "child_owner_binding_missing"
        if (
            str(indexed.get("user_id")) != user_id
            or str(indexed.get("session_id")) != session_id
            or str(indexed.get("session_scope_key")) != scope_key
        ):
            return "child_owner_binding_conflict"
        stored_workspace = indexed.get("workspace_anchor_key")
        if stored_workspace == "":
            return "child_workspace_binding_invalid"
        if stored_workspace is not None:
            try:
                stored_workspace = self._stored_workspace_key(indexed)
            except (TypeError, ValueError):
                return "child_workspace_binding_invalid"
        if stored_workspace != workspace_anchor_key:
            return "child_workspace_binding_conflict"
        return None

    def _indexed_parent_binding_issue(
        self,
        parent: dict[str, Any] | None,
        *,
        child: ChildSituationRef,
        user_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
    ) -> str | None:
        if not isinstance(parent, dict) or not self._parent_contains_situation(
            parent,
            child.situation_id,
        ):
            return "child_graph_binding_missing"
        if (
            str(parent.get("user_id") or "") != user_id
            or scope_key
            not in set(self._string_list(parent.get("session_scope_keys")))
        ):
            return "child_owner_binding_conflict"
        try:
            stored_workspace = self._stored_workspace_key(parent)
        except (TypeError, ValueError):
            return "child_workspace_binding_invalid"
        if stored_workspace != workspace_anchor_key:
            return "child_workspace_binding_conflict"
        return None

    def _indexed_candidate_binding_issue(
        self,
        candidate: dict[str, Any] | None,
        *,
        child: ChildSituationRef,
        user_id: str,
        session_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
    ) -> str | None:
        if not isinstance(candidate, dict):
            return "child_graph_binding_missing"
        try:
            candidate_ref = ChildSituationRef.from_dict(
                candidate.get("child_ref") or {}
            )
        except (TypeError, ValueError):
            return "child_graph_binding_invalid"
        if candidate_ref.situation_id != child.situation_id:
            return "child_graph_binding_conflict"
        if (
            str(candidate.get("user_id") or "") != user_id
            or str(candidate.get("session_id") or "") != session_id
            or str(candidate.get("session_scope_key") or "") != scope_key
        ):
            return "child_owner_binding_conflict"
        try:
            stored_workspace = self._stored_workspace_key(candidate)
        except (TypeError, ValueError):
            return "child_workspace_binding_invalid"
        if stored_workspace != workspace_anchor_key:
            return "child_workspace_binding_conflict"
        return None

    @classmethod
    def _graph_contains_child(
        cls,
        parents: dict[str, dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
        situation_id: str,
    ) -> bool:
        if any(
            cls._parent_contains_situation(parent, situation_id)
            for parent in parents.values()
        ):
            return True
        return any(
            isinstance(candidate.get("child_ref"), dict)
            and str(candidate["child_ref"].get("situation_id") or "")
            == situation_id
            for candidate in candidates.values()
        )

    def _effective_interval(
        self,
        situation: dict[str, Any],
        *,
        event: VeyraEvent | None,
    ) -> tuple[datetime, datetime]:
        payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
        source = situation.get("source_event") if isinstance(situation.get("source_event"), dict) else {}
        start_value = (
            payload.get("valid_from")
            or payload.get("effective_from")
            or getattr(event, "occurred_at", None)
            or source.get("occurred_at")
            or source.get("timestamp")
            or situation.get("created_at")
        )
        start = self._aware_time(start_value)
        if start is None:
            raise ValueError("effective start must be timezone-aware")
        end_present = "valid_until" in payload or "effective_until" in payload
        end_value = payload.get("valid_until") or payload.get("effective_until")
        end = self._aware_time(end_value) if end_present else start
        if end is None or end < start:
            raise ValueError("effective interval is invalid")
        return start, end

    def _valid_durable_anchors(
        self,
        *,
        user_id: str,
        anchors: dict[str, StructuredAnchor],
    ) -> set[str]:
        requested = {key for key, anchor in anchors.items() if anchor.durable}
        if not requested:
            return set()
        goals_state = self.state_store.read_json("user_goals.json")
        commitments_state = self.state_store.read_json("user_commitments.json")
        if goals_state.get("_state_corrupt") is True or commitments_state.get("_state_corrupt") is True:
            return set()
        valid: set[str] = set()
        goals = goals_state.get("goals") if isinstance(goals_state.get("goals"), list) else []
        for item in goals:
            if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id:
                continue
            if str(item.get("status") or "").strip().lower() in self._GOAL_TERMINAL:
                continue
            key = f"goal:{self._identifier(item.get('goal_id'))}"
            if key in requested:
                valid.add(key)
        commitments = (
            commitments_state.get("commitments")
            if isinstance(commitments_state.get("commitments"), list)
            else []
        )
        for item in commitments:
            if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id:
                continue
            if str(item.get("status") or "").strip().lower() not in self._COMMITMENT_DURABLE:
                continue
            key = f"commitment:{self._identifier(item.get('commitment_id'))}"
            if key in requested:
                valid.add(key)
        return valid

    def _matching_parent(
        self,
        parents: dict[str, dict[str, Any]],
        *,
        user_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
        anchors: dict[str, StructuredAnchor],
        durable_anchors: set[str],
        effective_start: datetime,
        effective_end: datetime,
    ) -> dict[str, Any] | None:
        matches: list[tuple[int, str, dict[str, Any]]] = []
        for parent_id, parent in parents.items():
            if str(parent.get("user_id") or "") != user_id:
                continue
            if not self._workspace_compatible(
                self._stored_workspace_key(parent),
                workspace_anchor_key,
            ):
                continue
            if self._projected_status(parent, now=self._now()) == "expired":
                continue
            common = set(self._string_list(parent.get("common_anchor_keys")))
            shared = common.intersection(anchors)
            merge_shared = self._merge_anchor_keys(shared)
            if not merge_shared or not self._time_matches(
                parent, effective_start, effective_end
            ):
                continue
            scope_keys = set(self._string_list(parent.get("session_scope_keys")))
            if scope_key not in scope_keys and not shared.intersection(durable_anchors):
                continue
            matches.append((len(merge_shared), str(parent_id), parent))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], item[1]))
        return copy.deepcopy(matches[0][2])

    def _matching_candidate(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        child: ChildSituationRef,
        user_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
        anchors: dict[str, StructuredAnchor],
        durable_anchors: set[str],
        effective_start: datetime,
        effective_end: datetime,
    ) -> dict[str, Any] | None:
        matches: list[tuple[int, str, dict[str, Any]]] = []
        for candidate_id, candidate in candidates.items():
            if str(candidate.get("user_id") or "") != user_id:
                continue
            if not self._workspace_compatible(
                self._stored_workspace_key(candidate),
                workspace_anchor_key,
            ):
                continue
            ref = candidate.get("child_ref") if isinstance(candidate.get("child_ref"), dict) else {}
            if str(ref.get("source_event_id") or "") == child.source_event_id:
                continue
            shared = set(self._string_list(candidate.get("anchor_keys"))).intersection(anchors)
            merge_shared = self._merge_anchor_keys(shared)
            if not merge_shared or not self._candidate_time_matches(
                candidate, effective_start, effective_end
            ):
                continue
            candidate_scope = str(candidate.get("session_scope_key") or "")
            if candidate_scope != scope_key and not shared.intersection(durable_anchors):
                continue
            matches.append((len(merge_shared), str(candidate_id), candidate))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], item[1]))
        return copy.deepcopy(matches[0][2])

    def _create_parent(
        self,
        candidate: dict[str, Any],
        *,
        child: ChildSituationRef,
        user_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
        anchors: dict[str, StructuredAnchor],
        durable_anchors: set[str],
        effective_start: datetime,
        effective_end: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        candidate_ref = ChildSituationRef.from_dict(candidate.get("child_ref") or {})
        common = sorted(set(self._string_list(candidate.get("anchor_keys"))).intersection(anchors))
        merge_common = sorted(self._merge_anchor_keys(set(common)))
        if not merge_common:
            raise ValueError("general Situation requires a common structured anchor")
        if not self._workspace_compatible(
            self._stored_workspace_key(candidate),
            workspace_anchor_key,
        ):
            raise ValueError("general Situation workspace binding differs")
        candidate_scope = str(candidate.get("session_scope_key") or "")
        cross_session = candidate_scope != scope_key
        durable_shared = sorted(set(common).intersection(durable_anchors))
        if cross_session and not durable_shared:
            raise ValueError("cross-session aggregation requires a durable anchor")
        primary_anchor = (durable_shared or merge_common)[0]
        child_refs = sorted(
            [candidate_ref.to_dict(), child.to_dict()],
            key=self._child_ref_sort_key,
        )
        identity = {
            "user_id": user_id,
            "primary_anchor": primary_anchor,
            "workspace_anchor_key": workspace_anchor_key,
            "source_event_ids": sorted(
                {candidate_ref.source_event_id, child.source_event_id}
            ),
        }
        general_id = "gsit_" + stable_digest(
            "veyra.general_situation.identity.v1",
            identity,
        )[:24]
        start = min(
            self._required_time(candidate.get("effective_start")),
            effective_start,
        )
        end = max(
            self._required_time(candidate.get("effective_end")),
            effective_end,
        )
        return {
            "schema_version": self.RECORD_SCHEMA_VERSION,
            "general_situation_id": general_id,
            "user_id": user_id,
            "session_scope_keys": sorted({candidate_scope, scope_key}),
            "workspace_anchor_key": workspace_anchor_key,
            "aggregation_scope": (
                "same_user_durable_anchor" if cross_session else "exact_owner_session"
            ),
            "primary_anchor_key": primary_anchor,
            "common_anchor_keys": common,
            "child_refs": child_refs,
            "distinct_event_count": len(
                {item["source_event_id"] for item in child_refs}
            ),
            "parent_revision": 1,
            "status": "observed",
            "causality_asserted": False,
            "model_similarity_used_for_merge": False,
            "effective_start": start.isoformat(),
            "effective_end": end.isoformat(),
            "expires_at": (end + timedelta(seconds=self.PARENT_TTL_SECONDS)).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    def _append_child(
        self,
        parent: dict[str, Any],
        *,
        child: ChildSituationRef,
        scope_key: str,
        workspace_anchor_key: str | None,
        anchors: dict[str, StructuredAnchor],
        effective_start: datetime,
        effective_end: datetime,
        now: datetime,
        preserve_common_anchors: bool,
    ) -> dict[str, Any]:
        refs = [
            ChildSituationRef.from_dict(item).to_dict()
            for item in parent.get("child_refs", [])
            if isinstance(item, dict)
            and str(item.get("situation_id") or "") != child.situation_id
        ]
        refs.append(child.to_dict())
        refs.sort(key=self._child_ref_sort_key)
        if len(refs) > self.MAX_CHILD_REFS:
            raise ValueError("general Situation child reference capacity exhausted")
        common = set(self._string_list(parent.get("common_anchor_keys")))
        if preserve_common_anchors:
            if not self._revision_preserves_parent_anchors(
                parent,
                anchors=anchors,
            ):
                raise ValueError(
                    "child revision changes the parent anchor binding"
                )
        else:
            common.intersection_update(anchors)
        if not common:
            raise ValueError("general Situation common anchor would be lost")
        if not self._workspace_compatible(
            self._stored_workspace_key(parent),
            workspace_anchor_key,
        ):
            raise ValueError("general Situation workspace binding differs")
        start = min(self._required_time(parent.get("effective_start")), effective_start)
        end = max(self._required_time(parent.get("effective_end")), effective_end)
        parent.update(
            {
                "session_scope_keys": sorted(
                    set(self._string_list(parent.get("session_scope_keys")))
                    | {scope_key}
                ),
                "common_anchor_keys": sorted(common),
                "workspace_anchor_key": workspace_anchor_key,
                "child_refs": refs,
                "distinct_event_count": len(
                    {str(item.get("source_event_id") or "") for item in refs}
                ),
                "parent_revision": self._nonnegative_int(
                    parent.get("parent_revision")
                )
                + 1,
                "status": "observed",
                "effective_start": start.isoformat(),
                "effective_end": end.isoformat(),
                "expires_at": (
                    end + timedelta(seconds=self.PARENT_TTL_SECONDS)
                ).isoformat(),
                "updated_at": now.isoformat(),
            }
        )
        return parent

    @classmethod
    def _revision_preserves_parent_anchors(
        cls,
        parent: dict[str, Any],
        *,
        anchors: dict[str, StructuredAnchor],
    ) -> bool:
        required = cls._merge_anchor_keys(
            set(cls._string_list(parent.get("common_anchor_keys")))
        )
        incoming = cls._merge_anchor_keys(set(anchors))
        return bool(required) and required.issubset(incoming)

    def _candidate_record(
        self,
        *,
        child: ChildSituationRef,
        user_id: str,
        scope_key: str,
        session_id: str,
        workspace_anchor_key: str | None,
        anchors: dict[str, StructuredAnchor],
        effective_start: datetime,
        effective_end: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        candidate_id = "gcand_" + stable_digest(
            "veyra.general_situation.candidate.v1",
            {
                "user_id": user_id,
                "session_id": session_id,
                "workspace_anchor_key": workspace_anchor_key,
                "situation_id": child.situation_id,
                "source_event_id": child.source_event_id,
            },
        )[:24]
        return {
            "candidate_id": candidate_id,
            "user_id": user_id,
            "session_id": session_id,
            "session_scope_key": scope_key,
            "workspace_anchor_key": workspace_anchor_key,
            "anchor_keys": sorted(anchors),
            "child_ref": child.to_dict(),
            "effective_start": effective_start.isoformat(),
            "effective_end": effective_end.isoformat(),
            "expires_at": (
                effective_end + timedelta(seconds=self.WINDOW_SECONDS)
            ).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    def _parent_for_existing_child(
        self,
        parents: dict[str, dict[str, Any]],
        *,
        child: ChildSituationRef,
        user_id: str,
        scope_key: str,
        workspace_anchor_key: str | None,
    ) -> dict[str, Any] | None:
        for parent_id in sorted(parents):
            parent = parents[parent_id]
            if str(parent.get("user_id") or "") != user_id:
                continue
            if scope_key not in set(
                self._string_list(parent.get("session_scope_keys"))
            ):
                continue
            if not self._workspace_compatible(
                self._stored_workspace_key(parent),
                workspace_anchor_key,
            ):
                continue
            if self._parent_contains_situation(parent, child.situation_id):
                return copy.deepcopy(parent)
        return None

    @staticmethod
    def _parent_contains_situation(parent: dict[str, Any], situation_id: str) -> bool:
        return any(
            isinstance(item, dict)
            and str(item.get("situation_id") or "") == situation_id
            for item in parent.get("child_refs", [])
        )

    def _time_matches(
        self,
        parent: dict[str, Any],
        start: datetime,
        end: datetime,
    ) -> bool:
        parent_start = self._required_time(parent.get("effective_start"))
        parent_end = self._required_time(parent.get("effective_end"))
        window = timedelta(seconds=self.WINDOW_SECONDS)
        return start <= parent_end + window and end >= parent_start - window

    def _candidate_time_matches(
        self,
        candidate: dict[str, Any],
        start: datetime,
        end: datetime,
    ) -> bool:
        candidate_start = self._required_time(candidate.get("effective_start"))
        candidate_end = self._required_time(candidate.get("effective_end"))
        window = timedelta(seconds=self.WINDOW_SECONDS)
        return start <= candidate_end + window and end >= candidate_start - window

    def _expire_records(
        self,
        parents: dict[str, dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
        *,
        now: datetime,
    ) -> None:
        for parent in parents.values():
            if self._projected_status(parent, now=now) == "expired":
                parent["status"] = "expired"
        for candidate_id in list(candidates):
            expires = self._aware_time(candidates[candidate_id].get("expires_at"))
            if expires is None or expires <= now:
                candidates.pop(candidate_id, None)

    def _public_parent(
        self,
        parent: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        output = {
            key: copy.deepcopy(value)
            for key, value in parent.items()
            if key not in {"session_scope_keys"}
        }
        output["status"] = self._projected_status(parent, now=now)
        return output

    def _projected_status(self, parent: dict[str, Any], *, now: datetime) -> str:
        expires = self._aware_time(parent.get("expires_at"))
        if expires is None or expires <= now:
            return "expired"
        return str(parent.get("status") or "observed")

    @staticmethod
    def _records(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): copy.deepcopy(item)
            for key, item in value.items()
            if isinstance(item, dict)
        }

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        return [str(item) for item in value if str(item)] if isinstance(value, list) else []

    @staticmethod
    def _identifier(value: Any) -> str:
        if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
            return ""
        selected = " ".join(str(value).strip().split())
        if not selected or len(selected) > 240:
            return ""
        if any(ord(character) < 32 for character in selected):
            return ""
        return selected

    @staticmethod
    def _child_ref_sort_key(value: dict[str, Any]) -> tuple[str, int, str]:
        return (
            str(value.get("source_event_id") or ""),
            int(value.get("observation_revision") or 0),
            str(value.get("situation_id") or ""),
        )

    @classmethod
    def _healthy_state(cls, state: dict[str, Any]) -> bool:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            return False
        schema = str(state.get("schema_version") or "")
        return schema in {"", cls.SCHEMA_VERSION}

    @staticmethod
    def _authority_boundary() -> dict[str, bool]:
        return {
            "execution_allowed": False,
            "tool_allowed": False,
            "agent_allowed": False,
            "capability_grant_allowed": False,
            "route_change_allowed": False,
            "causality_asserted": False,
        }

    def _closed(self, reason: str, *, detail: str | None = None) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": "fail_closed",
            "reason": reason,
            "general_situation": None,
            "authority": self._authority_boundary(),
        }
        if detail:
            output["detail"] = detail
        return output

    def _now(self) -> datetime:
        selected = self._clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("general Situation clock must be timezone-aware")
        return selected.astimezone(timezone.utc)

    @staticmethod
    def _aware_time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            selected = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                selected = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if selected.tzinfo is None or selected.utcoffset() is None:
            return None
        return selected.astimezone(timezone.utc)

    def _required_time(self, value: Any) -> datetime:
        selected = self._aware_time(value)
        if selected is None:
            raise ValueError("timezone-aware persisted time required")
        return selected

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
