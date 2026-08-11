from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
from typing import Any, Callable

from awareness.general_attention_scheduler import GeneralAttentionScheduler
from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.general_situation_contract import ChildSituationRef, stable_digest
from memory_bridge.scope import normalize_scope_component
from runtime.general_situation_runtime import GeneralSituationRuntime


class AttentionHypothesisRuntime:
    """Accumulate evidence-bound Attention hypotheses without factual authority.

    The readiness value is a deterministic policy score over the existing
    GeneralAttention components.  It is not a probability that a claim is true
    and it never converts a GeneralSituation into a fact or causal assertion.
    """

    STATE_FILE = "attention_hypothesis_state.json"
    STATE_SCHEMA_VERSION = "veyra.attention_hypothesis_state.v1"
    RECORD_SCHEMA_VERSION = "veyra.attention_hypothesis.v1"
    SURFACE_SCHEMA_VERSION = "veyra.attention_hypothesis_surface.v1"
    RULESET_VERSION = "veyra.attention_hypothesis.rules.v1"
    MIN_DISTINCT_CURRENT_EVENTS = 2
    MIN_COMPONENT_COVERAGE = 0.50
    MIN_PARTIAL_WEIGHTED_SCORE = 0.45
    MAX_HYPOTHESES = 2000
    MAX_EVIDENCE_REFS = 256
    MAX_LIFECYCLE_EVENTS = 2000
    MAX_ASSESSMENT_ADMISSION_AGE_SECONDS = 5
    LIFECYCLE_SIGNAL_SCHEMA_VERSION = "veyra.attention_hypothesis.lifecycle_signal.v1"
    LIFECYCLE_EVENT_SCHEMA_VERSION = "veyra.attention_hypothesis.lifecycle_event.v1"
    LIFECYCLE_SIGNAL_KINDS = {"contradiction", "supersede"}
    LIFECYCLE_REASON_CODES = {
        "direct_counter_observation",
        "source_withdrawn",
        "parent_superseded",
        "operator_correction",
    }
    LIFECYCLE_PRODUCERS = {
        "component_health",
        "commitment_runtime",
        "local_operator",
        "task_runtime",
    }
    TERMINAL_STATUSES = {"contradicted", "expired", "superseded"}
    _HIGH_IMPACT_COMPONENTS = {"goal_priority", "severity", "urgency"}
    _REQUIRED_AUTHORITY_KEYS = {
        "execution_allowed",
        "tool_allowed",
        "agent_allowed",
        "capability_grant_allowed",
        "route_change_allowed",
    }
    _RECORD_KEYS = frozenset(
        {
            "schema_version",
            "hypothesis_id",
            "identity_digest",
            "identity",
            "ruleset_version",
            "general_attention_scorer_version",
            "user_id",
            "session_scope_keys",
            "workspace_anchor_key",
            "primary_anchor_key",
            "common_anchor_keys",
            "general_situation_id",
            "parent_revision",
            "expires_at",
            "status",
            "hypothesis_revision",
            "evaluation_count",
            "evidence_refs",
            "evidence_count",
            "current_evidence_refs",
            "parent_distinct_event_count",
            "current_distinct_event_count_lower_bound",
            "components",
            "unknowns",
            "evidence_diversity",
            "assessment_binding",
            "parent_binding",
            "attention_readiness",
            "last_assessment_digest",
            "is_fact",
            "causality_asserted",
            "model_confidence_used",
            "authority",
            "created_at",
            "first_confirmed_at",
            "confirmed_at",
            "updated_at",
        }
    )
    _PUBLIC_HYPOTHESIS_KEYS = (
        "schema_version",
        "hypothesis_id",
        "ruleset_version",
        "general_attention_scorer_version",
        "workspace_anchor_key",
        "primary_anchor_key",
        "common_anchor_keys",
        "general_situation_id",
        "parent_revision",
        "expires_at",
        "status",
        "hypothesis_revision",
        "evaluation_count",
        "evidence_refs",
        "evidence_count",
        "current_evidence_refs",
        "parent_distinct_event_count",
        "current_distinct_event_count_lower_bound",
        "components",
        "unknowns",
        "evidence_diversity",
        "assessment_binding",
        "attention_readiness",
        "is_fact",
        "causality_asserted",
        "model_confidence_used",
        "authority",
        "created_at",
        "first_confirmed_at",
        "confirmed_at",
        "updated_at",
    )

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def observe(
        self,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        *,
        lifecycle_signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one exact, structured assessment and return an Outbox surface."""

        try:
            evaluated = self._evaluate(general_situation, assessment)
            signal = self._validated_lifecycle_signal(lifecycle_signal)
        except (TypeError, ValueError) as exc:
            return self._closed(
                "invalid_attention_hypothesis_input",
                detail=type(exc).__name__,
            )

        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return self._closed(
                "attention_hypothesis_state_corrupt",
                evaluated=evaluated,
            )

        hypothesis_id = "ahyp_" + evaluated["identity_digest"][:24]
        if signal is not None and signal["kind"] == "contradiction":
            if signal["target_hypothesis_id"] != hypothesis_id:
                return self._closed(
                    "attention_contradiction_target_mismatch",
                    evaluated=evaluated,
                )
        result: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if not self._healthy_state(current):
                result = self._closed(
                    "attention_hypothesis_state_corrupt",
                    evaluated=evaluated,
                )
                return current
            parent_issue = self._current_parent_issue(evaluated)
            if parent_issue is not None:
                result = self._closed(parent_issue, evaluated=evaluated)
                return current
            commit_now = self._now()

            hypotheses = self._records(current.get("hypotheses"))
            identity_index = self._string_map(current.get("identity_index"))
            lifecycle_events = self._records(current.get("lifecycle_events"))
            if signal is not None:
                existing_event = lifecycle_events.get(signal["signal_id"])
                if existing_event is not None:
                    if not self._valid_lifecycle_event(
                        signal["signal_id"], existing_event
                    ) or any(
                        existing_event.get(key) != signal.get(key)
                        for key in (
                            "kind",
                            "target_hypothesis_id",
                            "target_hypothesis_revision",
                            "evidence_id",
                            "reason_code",
                            "producer_id",
                            "observed_at",
                        )
                    ):
                        result = self._closed(
                            "attention_lifecycle_signal_rebound",
                            evaluated=evaluated,
                        )
                        return current
                    replay_id = str(
                        existing_event.get("replacement_hypothesis_id")
                        or existing_event.get("target_hypothesis_id")
                        or ""
                    )
                    replay_record = hypotheses.get(replay_id)
                    if isinstance(replay_record, dict):
                        result = self._result(
                            replay_record,
                            evaluated=evaluated,
                            replayed=True,
                            evidence_added_count=0,
                        )
                        return current
                    result = self._closed(
                        "attention_lifecycle_event_target_missing",
                        evaluated=evaluated,
                    )
                    return current
            indexed_id = identity_index.get(evaluated["identity_digest"])
            if indexed_id and indexed_id != hypothesis_id:
                result = self._closed(
                    "attention_hypothesis_identity_conflict",
                    evaluated=evaluated,
                )
                return current

            previous = hypotheses.get(hypothesis_id)
            if (previous is None) != (indexed_id is None):
                result = self._closed(
                    "attention_hypothesis_identity_index_corrupt",
                    evaluated=evaluated,
                )
                return current
            if previous is not None and not self._valid_record(
                previous,
                hypothesis_id=hypothesis_id,
                identity_digest=evaluated["identity_digest"],
            ):
                result = self._closed(
                    "attention_hypothesis_record_corrupt",
                    evaluated=evaluated,
                )
                return current
            if (
                signal is None
                and
                previous is not None
                and str(previous.get("last_assessment_digest") or "")
                == evaluated["assessment_digest"]
            ):
                result = self._result(
                    previous,
                    evaluated=evaluated,
                    replayed=True,
                    evidence_added_count=0,
                )
                return current
            if signal is None and previous is not None and self._same_assessment_generation(
                previous,
                evaluated,
            ):
                result = self._result(
                    previous,
                    evaluated=evaluated,
                    replayed=True,
                    evidence_added_count=0,
                )
                return current
            if (
                previous is not None
                and previous.get("status") in self.TERMINAL_STATUSES
                and signal is None
            ):
                result = self._terminal_result(
                    previous,
                    evaluated=evaluated,
                )
                return current
            supersede_target = None
            if signal is not None and signal["kind"] == "supersede":
                if signal["target_hypothesis_id"] == hypothesis_id:
                    result = self._closed(
                        "attention_supersede_requires_new_identity",
                        evaluated=evaluated,
                    )
                    return current
                supersede_target = hypotheses.get(signal["target_hypothesis_id"])
                if not isinstance(supersede_target, dict):
                    result = self._closed(
                        "attention_supersede_target_missing",
                        evaluated=evaluated,
                    )
                    return current
                if (
                    supersede_target.get("hypothesis_revision")
                    != signal["target_hypothesis_revision"]
                    or supersede_target.get("status") in self.TERMINAL_STATUSES
                ):
                    result = self._closed(
                        "attention_supersede_target_not_current",
                        evaluated=evaluated,
                    )
                    return current
                if (
                    str(supersede_target.get("user_id") or "")
                    != evaluated["user_id"]
                    or not set(evaluated["session_scope_keys"]).intersection(
                        set(supersede_target.get("session_scope_keys") or [])
                    )
                ):
                    result = self._closed(
                        "attention_supersede_owner_mismatch",
                        evaluated=evaluated,
                    )
                    return current
            if signal is not None and signal["kind"] == "contradiction":
                if previous is None:
                    result = self._closed(
                        "attention_contradiction_target_missing",
                        evaluated=evaluated,
                    )
                    return current
                if previous.get("hypothesis_revision") != signal["target_hypothesis_revision"]:
                    result = self._closed(
                        "attention_contradiction_target_not_current",
                        evaluated=evaluated,
                    )
                    return current
                terminal = self._terminalize_record(
                    previous,
                    status="contradicted",
                    marker=f"typed_contradiction:{signal['signal_id']}",
                    now=commit_now,
                )
                lifecycle_events[signal["signal_id"]] = self._lifecycle_event(
                    signal,
                    replacement_hypothesis_id=terminal["hypothesis_id"],
                    recorded_at=commit_now,
                )
                hypotheses[terminal["hypothesis_id"]] = terminal
                current.update(
                    {
                        "schema_version": self.STATE_SCHEMA_VERSION,
                        "ruleset_version": self.RULESET_VERSION,
                        "hypotheses": hypotheses,
                        "identity_index": identity_index,
                        "lifecycle_events": lifecycle_events,
                        "lifecycle_event_count": len(lifecycle_events),
                        "hypothesis_count": len(hypotheses),
                        "status_counts": self._status_counts(hypotheses),
                        "updated_at": commit_now.isoformat(),
                    }
                )
                result = self._result(
                    terminal,
                    evaluated=evaluated,
                    replayed=False,
                    evidence_added_count=0,
                )
                return current
            if (
                previous is not None
                and str(previous.get("general_situation_id") or "")
                == evaluated["general_situation_id"]
            ):
                previous_parent_revision = self._nonnegative_int(
                    previous.get("parent_revision")
                )
                if evaluated["parent_revision"] < previous_parent_revision:
                    result = self._stale_parent_result(
                        previous,
                        evaluated=evaluated,
                        current_parent_revision=previous_parent_revision,
                    )
                    return current
            if previous is None and len(hypotheses) >= self.MAX_HYPOTHESES:
                result = self._closed(
                    "attention_hypothesis_capacity_exhausted",
                    evaluated=evaluated,
                )
                return current

            evidence_by_stream: dict[tuple[str, str], ChildSituationRef] = {}
            if previous is not None:
                for raw_ref in previous.get("evidence_refs", []):
                    ref = ChildSituationRef.from_dict(raw_ref)
                    stream_key = self._ref_stream_key(ref)
                    if stream_key in evidence_by_stream:
                        result = self._closed(
                            "attention_hypothesis_evidence_stream_conflict",
                            evaluated=evaluated,
                        )
                        return current
                    evidence_by_stream[stream_key] = ref
            evidence_before = len(evidence_by_stream)
            for ref in evaluated["current_evidence_refs"]:
                stream_key = self._ref_stream_key(ref)
                existing = evidence_by_stream.get(stream_key)
                if existing is not None:
                    if ref.observation_revision < existing.observation_revision:
                        result = self._closed(
                            "attention_hypothesis_evidence_revision_out_of_order",
                            evaluated=evaluated,
                        )
                        return current
                    if (
                        ref.observation_revision == existing.observation_revision
                        and ref.digest != existing.digest
                    ):
                        result = self._closed(
                            "attention_hypothesis_evidence_digest_conflict",
                            evaluated=evaluated,
                        )
                        return current
                evidence_by_stream[stream_key] = ref
            if len(evidence_by_stream) > self.MAX_EVIDENCE_REFS:
                result = self._closed(
                    "attention_hypothesis_evidence_capacity_exhausted",
                    evaluated=evaluated,
                )
                return current
            evidence_added = len(evidence_by_stream) - evidence_before

            lifecycle = self._lifecycle_status(evaluated)
            if lifecycle is not None:
                status = lifecycle
            elif evaluated["confirmable"]:
                status = "confirmed"
            elif previous is None:
                status = "candidate"
            else:
                status = "accumulating"

            if supersede_target is not None:
                supersede_target = self._terminalize_record(
                    supersede_target,
                    status="superseded",
                    marker=(
                        f"typed_supersede:{signal['signal_id']}:{hypothesis_id}"
                    ),
                    now=commit_now,
                )

            created_at = (
                str(previous.get("created_at") or "")
                if previous is not None
                else commit_now.isoformat()
            )
            first_confirmed_at = (
                previous.get("first_confirmed_at")
                if previous is not None
                else None
            )
            if status == "confirmed" and not first_confirmed_at:
                first_confirmed_at = commit_now.isoformat()
            record = {
                "schema_version": self.RECORD_SCHEMA_VERSION,
                "hypothesis_id": hypothesis_id,
                "identity_digest": evaluated["identity_digest"],
                "identity": copy.deepcopy(evaluated["identity"]),
                "ruleset_version": self.RULESET_VERSION,
                "general_attention_scorer_version": evaluated[
                    "general_attention_scorer_version"
                ],
                "user_id": evaluated["user_id"],
                "session_scope_keys": copy.deepcopy(
                    evaluated["session_scope_keys"]
                ),
                "workspace_anchor_key": evaluated["workspace_anchor_key"],
                "primary_anchor_key": evaluated["primary_anchor_key"],
                "common_anchor_keys": copy.deepcopy(
                    evaluated["common_anchor_keys"]
                ),
                "general_situation_id": evaluated["general_situation_id"],
                "parent_revision": evaluated["parent_revision"],
                "expires_at": evaluated["expires_at"],
                "status": status,
                "hypothesis_revision": self._nonnegative_int(
                    previous.get("hypothesis_revision")
                    if previous is not None
                    else 0
                )
                + 1,
                "evaluation_count": self._nonnegative_int(
                    previous.get("evaluation_count")
                    if previous is not None
                    else 0
                )
                + 1,
                "evidence_refs": sorted(
                    [ref.to_dict() for ref in evidence_by_stream.values()],
                    key=self._ref_sort_key,
                ),
                "evidence_count": len(evidence_by_stream),
                "current_evidence_refs": [
                    ref.to_dict()
                    for ref in evaluated["current_evidence_refs"]
                ],
                "parent_distinct_event_count": evaluated[
                    "parent_distinct_event_count"
                ],
                "current_distinct_event_count_lower_bound": evaluated[
                    "current_distinct_event_count_lower_bound"
                ],
                "components": copy.deepcopy(evaluated["components"]),
                "unknowns": copy.deepcopy(evaluated["unknowns"]),
                "evidence_diversity": copy.deepcopy(
                    evaluated["evidence_diversity"]
                ),
                "assessment_binding": copy.deepcopy(
                    evaluated["assessment_binding"]
                ),
                "parent_binding": copy.deepcopy(evaluated["parent_binding"]),
                "attention_readiness": copy.deepcopy(
                    evaluated["attention_readiness"]
                ),
                "last_assessment_digest": evaluated["assessment_digest"],
                "is_fact": False,
                "causality_asserted": False,
                "model_confidence_used": False,
                "authority": self._authority_boundary(),
                "created_at": created_at,
                "first_confirmed_at": first_confirmed_at,
                "confirmed_at": (
                    commit_now.isoformat() if status == "confirmed" else None
                ),
                "updated_at": commit_now.isoformat(),
            }
            hypotheses[hypothesis_id] = record
            if supersede_target is not None:
                hypotheses[supersede_target["hypothesis_id"]] = supersede_target
                lifecycle_events[signal["signal_id"]] = self._lifecycle_event(
                    signal,
                    replacement_hypothesis_id=hypothesis_id,
                    recorded_at=commit_now,
                )
            identity_index[evaluated["identity_digest"]] = hypothesis_id
            current.update(
                {
                    "schema_version": self.STATE_SCHEMA_VERSION,
                    "ruleset_version": self.RULESET_VERSION,
                    "hypotheses": hypotheses,
                    "identity_index": identity_index,
                    "lifecycle_events": lifecycle_events,
                    "lifecycle_event_count": len(lifecycle_events),
                    "hypothesis_count": len(hypotheses),
                    "status_counts": self._status_counts(hypotheses),
                    "updated_at": commit_now.isoformat(),
                }
            )
            result = self._result(
                record,
                evaluated=evaluated,
                replayed=False,
                evidence_added_count=evidence_added,
            )
            return current

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result or self._closed(
            "attention_hypothesis_mutation_no_result",
            evaluated=evaluated,
        )

    @staticmethod
    def _same_assessment_generation(
        previous: Any,
        evaluated: Any,
    ) -> bool:
        if not isinstance(previous, dict) or not isinstance(evaluated, dict):
            return False
        previous_components = copy.deepcopy(previous.get("components") or {})
        evaluated_components = copy.deepcopy(evaluated.get("components") or {})
        previous_components.pop("freshness", None)
        evaluated_components.pop("freshness", None)
        previous_readiness = previous.get("attention_readiness")
        evaluated_readiness = evaluated.get("attention_readiness")
        if not isinstance(previous_readiness, dict) or not isinstance(
            evaluated_readiness,
            dict,
        ):
            return False
        try:
            current_refs = [
                ref.to_dict()
                for ref in evaluated.get("current_evidence_refs", [])
                if isinstance(ref, ChildSituationRef)
            ]
        except (TypeError, ValueError):
            return False
        return bool(
            previous.get("general_situation_id")
            == evaluated.get("general_situation_id")
            and previous.get("parent_revision")
            == evaluated.get("parent_revision")
            and previous.get("current_evidence_refs") == current_refs
            and previous_components == evaluated_components
            and previous.get("unknowns") == evaluated.get("unknowns")
            and previous.get("evidence_diversity")
            == evaluated.get("evidence_diversity")
            and previous_readiness.get("confirmation_blockers")
            == evaluated_readiness.get("confirmation_blockers")
        )

    def _stale_parent_result(
        self,
        record: dict[str, Any],
        *,
        evaluated: dict[str, Any],
        current_parent_revision: int,
    ) -> dict[str, Any]:
        """Reject delayed parent revisions without replaying prior eligibility."""

        return {
            "status": "stale",
            "reason": "attention_parent_revision_out_of_order",
            "received_parent_revision": evaluated.get("parent_revision"),
            "current_parent_revision": current_parent_revision,
            "replayed": False,
            "evidence_added_count": 0,
            "hypothesis": copy.deepcopy(record),
            "surface_assessment": self._surface(
                evaluated,
                status="stale",
                extra_unknown="attention_parent_revision_out_of_order",
            ),
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }

    def status(self) -> dict[str, Any]:
        """Return a pure operational summary without refreshing hypotheses."""

        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return self._closed("attention_hypothesis_state_corrupt")
        hypotheses = self._records(state.get("hypotheses"))
        return {
            "status": "success",
            "ruleset_version": self.RULESET_VERSION,
            "hypothesis_count": len(hypotheses),
            "status_counts": self._status_counts(hypotheses),
            "state_revision": self._nonnegative_int(
                state.get("_state_revision")
            ),
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }

    def list_for_owner(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pure exact-owner read; it never refreshes or mutates hypotheses."""

        user = normalize_scope_component(user_id, "user_id")
        session = normalize_scope_component(session_id, "session_id")
        scope_key = tenant_scope_storage_key(user, session)
        if isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        selected_limit = max(0, min(int(limit), 500))
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy_state(state):
            return {
                **self._closed("attention_hypothesis_state_corrupt"),
                "items": [],
                "count": 0,
            }
        hypotheses = self._records(state.get("hypotheses"))
        identity_index = self._string_map(state.get("identity_index"))
        if len(identity_index) != len(hypotheses) or any(
            not self._valid_record(
                item,
                hypothesis_id=hypothesis_id,
                identity_digest=str(item.get("identity_digest") or ""),
            )
            or identity_index.get(str(item.get("identity_digest") or ""))
            != hypothesis_id
            for hypothesis_id, item in hypotheses.items()
        ):
            return {
                **self._closed("attention_hypothesis_record_corrupt"),
                "items": [],
                "count": 0,
            }
        visible = [
            item
            for item in hypotheses.values()
            if str(item.get("user_id") or "") == user
            and scope_key
            in {
                str(value)
                for value in item.get("session_scope_keys", [])
                if isinstance(value, str)
            }
        ]
        visible.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("hypothesis_id") or ""),
            ),
            reverse=True,
        )
        visible_records = {
            str(item.get("hypothesis_id") or ""): item
            for item in visible
        }
        return {
            "status": "success",
            "count": min(len(visible), selected_limit),
            "hypothesis_count": len(visible),
            "items": [
                self._public_hypothesis(item)
                for item in visible[:selected_limit]
            ],
            "status_counts": self._status_counts(visible_records),
            "state_revision": self._nonnegative_int(
                state.get("_state_revision")
            ),
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }

    def _evaluate(
        self,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        parent = self._validated_parent(general_situation)
        normalized = self._validated_assessment(parent, assessment)
        user_id = normalize_scope_component(parent.get("user_id"), "user_id")
        session_scope_keys = self._canonical_strings(
            parent.get("session_scope_keys"),
            field="session_scope_keys",
        )
        common_anchor_keys = self._canonical_strings(
            parent.get("common_anchor_keys"),
            field="common_anchor_keys",
        )
        common_anchor_keys = [
            self._anchor_key(item, field="common_anchor_key")
            for item in common_anchor_keys
        ]
        primary_anchor_key = self._anchor_key(
            parent.get("primary_anchor_key"),
            field="primary_anchor_key",
        )
        if primary_anchor_key not in common_anchor_keys:
            raise ValueError("primary anchor is not common to the parent")
        workspace_raw = parent.get("workspace_anchor_key")
        workspace_anchor_key = (
            None
            if workspace_raw is None
            else self._anchor_key(
                workspace_raw,
                field="workspace_anchor_key",
                required_kind="workspace",
            )
        )

        components = normalized["components"]
        evidence_diversity = normalized["evidence_diversity"]
        assessment_binding = normalized["assessment_binding"]
        known = {
            name: item
            for name, item in components.items()
            if item.get("value") is not None
        }
        component_coverage = round(
            sum(float(item["weight"]) for item in known.values()),
            6,
        )
        partial_score = round(
            sum(float(item["weighted_score"]) for item in known.values()),
            6,
        )
        unknowns = list(normalized["unknowns"])
        expires_at = self._aware_time(parent.get("expires_at"))
        if expires_at is None or expires_at <= self._now():
            unknowns.append("general_situation_expired")
        unknowns = list(dict.fromkeys(unknowns))
        critical_unknowns = sorted(
            item
            for item in unknowns
            if self._critical_unknown(item)
        )
        distinct_event_count = len(
            {ref.source_event_id for ref in normalized["evidence_refs"]}
        )
        distinct_current = (
            distinct_event_count >= self.MIN_DISTINCT_CURRENT_EVENTS
            and "two_distinct_current_child_events_required" not in unknowns
        )
        high_impact_known = bool(self._HIGH_IMPACT_COMPONENTS.intersection(known))
        blockers: list[str] = []
        if not distinct_current:
            blockers.append("distinct_current_events_below_minimum")
        if component_coverage < self.MIN_COMPONENT_COVERAGE:
            blockers.append("component_coverage_below_threshold")
        if not high_impact_known:
            blockers.append("high_impact_component_required")
        if critical_unknowns:
            blockers.append("critical_unknowns_present")
        if partial_score < self.MIN_PARTIAL_WEIGHTED_SCORE:
            blockers.append("partial_weighted_score_below_threshold")
        if evidence_diversity.get("profile_complete") is not True:
            blockers.append("structured_evidence_profile_incomplete")
        if evidence_diversity.get("diversity_requirement_met") is not True:
            blockers.append("independent_evidence_dimension_required")

        readiness = {
            "schema_version": "veyra.attention_readiness.v1",
            "kind": "deterministic_policy_readiness",
            "value": partial_score,
            "threshold": self.MIN_PARTIAL_WEIGHTED_SCORE,
            "component_coverage": component_coverage,
            "minimum_component_coverage": self.MIN_COMPONENT_COVERAGE,
            "distinct_current_event_requirement_met": distinct_current,
            "current_distinct_event_count_lower_bound": (
                self.MIN_DISTINCT_CURRENT_EVENTS if distinct_current else 0
            ),
            "minimum_distinct_current_events": self.MIN_DISTINCT_CURRENT_EVENTS,
            "high_impact_component_known": high_impact_known,
            "critical_unknowns": critical_unknowns,
            "confirmation_blockers": blockers,
            "evidence_diversity": {
                key: copy.deepcopy(evidence_diversity.get(key))
                for key in (
                    "ruleset_version",
                    "time_bucket_seconds",
                    "unit_count",
                    "producer_count",
                    "fact_kind_count",
                    "time_bucket_count",
                    "profile_complete",
                    "diversity_requirement_met",
                    "profile_digest",
                )
            },
            "is_probability": False,
            "is_fact": False,
            "model_confidence_used": False,
            "semantics": "attention_policy_readiness_not_factual_probability",
        }
        identity = {
            "general_situation_schema_version": parent["schema_version"],
            "assessment_schema_version": normalized["schema_version"],
            "general_attention_scorer_version": normalized[
                "general_attention_scorer_version"
            ],
            "general_situation_id": parent["general_situation_id"],
            "user_id": user_id,
            "workspace_anchor_key": workspace_anchor_key,
            "primary_anchor_key": primary_anchor_key,
            "ruleset_version": self.RULESET_VERSION,
        }
        identity_digest = stable_digest(
            "veyra.attention_hypothesis.identity.v2",
            identity,
        )
        assessment_digest = stable_digest(
            "veyra.attention_hypothesis.assessment.v1",
            {
                "identity_digest": identity_digest,
                "general_situation_id": parent["general_situation_id"],
                "parent_revision": parent["parent_revision"],
                "evidence": [ref.to_dict() for ref in normalized["evidence_refs"]],
                "components": components,
                "unknowns": unknowns,
                "readiness": readiness,
                "evidence_diversity": evidence_diversity,
                "assessment_binding": assessment_binding,
            },
        )
        return {
            "identity": identity,
            "identity_digest": identity_digest,
            "assessment_digest": assessment_digest,
            "general_attention_scorer_version": normalized[
                "general_attention_scorer_version"
            ],
            "user_id": user_id,
            "session_scope_keys": session_scope_keys,
            "workspace_anchor_key": workspace_anchor_key,
            "primary_anchor_key": primary_anchor_key,
            "common_anchor_keys": common_anchor_keys,
            "general_situation_id": parent["general_situation_id"],
            "parent_revision": parent["parent_revision"],
            "expires_at": expires_at.isoformat() if expires_at else None,
            "parent_binding": self._parent_binding(parent),
            "current_evidence_refs": normalized["evidence_refs"],
            "parent_distinct_event_count": distinct_event_count,
            "current_distinct_event_count_lower_bound": (
                self.MIN_DISTINCT_CURRENT_EVENTS if distinct_current else 0
            ),
            "components": components,
            "unknowns": unknowns,
            "attention_readiness": readiness,
            "evidence_diversity": evidence_diversity,
            "assessment_binding": assessment_binding,
            "confirmable": not blockers,
        }

    @staticmethod
    def _lifecycle_status(evaluated: dict[str, Any]) -> str | None:
        """Return a terminal epistemic state from typed evidence markers."""
        unknowns = {
            str(item).strip().lower()
            for item in evaluated.get("unknowns", [])
            if isinstance(item, str)
        }
        if "general_situation_expired" in unknowns:
            return "expired"
        if any(item.startswith("typed_supersede:") for item in unknowns):
            return "superseded"
        if any(item.startswith("typed_contradiction:") for item in unknowns):
            return "contradicted"
        if any(
            item == "contradicted"
            or item.startswith("contradicted:")
            or item.endswith("_contradicted")
            for item in unknowns
        ):
            return "contradicted"
        return None

    @classmethod
    def _validated_lifecycle_signal(
        cls,
        value: Any,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "signal_id",
            "kind",
            "target_hypothesis_id",
            "target_hypothesis_revision",
            "evidence_id",
            "reason_code",
            "producer_id",
            "observed_at",
        }:
            raise ValueError("attention lifecycle signal shape is invalid")
        signal_id = str(value.get("signal_id") or "")
        target_id = str(value.get("target_hypothesis_id") or "")
        evidence_id = str(value.get("evidence_id") or "")
        if (
            value.get("schema_version") != cls.LIFECYCLE_SIGNAL_SCHEMA_VERSION
            or not re.fullmatch(r"als_[A-Za-z0-9_.:/-]{1,120}", signal_id)
            or value.get("kind") not in cls.LIFECYCLE_SIGNAL_KINDS
            or not re.fullmatch(r"ahyp_[0-9a-f]{24}", target_id)
            or isinstance(value.get("target_hypothesis_revision"), bool)
            or not isinstance(value.get("target_hypothesis_revision"), int)
            or value["target_hypothesis_revision"] < 1
            or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,240}", evidence_id)
            or value.get("reason_code") not in cls.LIFECYCLE_REASON_CODES
            or value.get("producer_id") not in cls.LIFECYCLE_PRODUCERS
            or cls._aware_time(value.get("observed_at")) is None
        ):
            raise ValueError("attention lifecycle signal values are invalid")
        return {
            **copy.deepcopy(value),
            "signal_id": signal_id,
            "target_hypothesis_id": target_id,
            "observed_at": cls._aware_time(value["observed_at"]).isoformat(),
        }

    @classmethod
    def _lifecycle_event(
        cls,
        signal: dict[str, Any],
        *,
        replacement_hypothesis_id: str,
        recorded_at: datetime,
    ) -> dict[str, Any]:
        return {
            "schema_version": cls.LIFECYCLE_EVENT_SCHEMA_VERSION,
            **copy.deepcopy(signal),
            "replacement_hypothesis_id": replacement_hypothesis_id,
            "recorded_at": recorded_at.isoformat(),
            "authority": cls._authority_boundary(),
        }

    @classmethod
    def _valid_lifecycle_event(
        cls,
        signal_id: Any,
        value: Any,
    ) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "signal_id",
            "kind",
            "target_hypothesis_id",
            "target_hypothesis_revision",
            "evidence_id",
            "reason_code",
            "producer_id",
            "observed_at",
            "replacement_hypothesis_id",
            "recorded_at",
            "authority",
        }:
            return False
        try:
            signal = cls._validated_lifecycle_signal(
                {
                    key: value.get(key)
                    for key in (
                        "schema_version",
                        "signal_id",
                        "kind",
                        "target_hypothesis_id",
                        "target_hypothesis_revision",
                        "evidence_id",
                        "reason_code",
                        "producer_id",
                        "observed_at",
                    )
                }
            )
        except (TypeError, ValueError):
            return False
        return bool(
            signal is not None
            and signal_id == signal["signal_id"]
            and re.fullmatch(
                r"ahyp_[0-9a-f]{24}",
                str(value.get("replacement_hypothesis_id") or ""),
            )
            and cls._aware_time(value.get("recorded_at")) is not None
            and value.get("authority") == cls._authority_boundary()
        )

    @classmethod
    def _terminalize_record(
        cls,
        record: dict[str, Any],
        *,
        status: str,
        marker: str,
        now: datetime,
    ) -> dict[str, Any]:
        if status not in cls.TERMINAL_STATUSES:
            raise ValueError("terminal lifecycle status is invalid")
        updated = copy.deepcopy(record)
        unknowns = [str(item) for item in updated.get("unknowns") or []]
        if marker not in unknowns:
            unknowns.append(marker)
        updated["unknowns"] = list(dict.fromkeys(unknowns))
        updated["status"] = status
        updated["hypothesis_revision"] = cls._nonnegative_int(
            updated.get("hypothesis_revision")
        ) + 1
        updated["evaluation_count"] = cls._nonnegative_int(
            updated.get("evaluation_count")
        ) + 1
        updated["confirmed_at"] = None
        updated["updated_at"] = now.isoformat()
        updated["last_assessment_digest"] = stable_digest(
            "veyra.attention_hypothesis.assessment.v1",
            {
                "identity_digest": updated.get("identity_digest"),
                "general_situation_id": updated.get("general_situation_id"),
                "parent_revision": updated.get("parent_revision"),
                "evidence": copy.deepcopy(updated.get("current_evidence_refs") or []),
                "components": copy.deepcopy(updated.get("components") or {}),
                "unknowns": updated["unknowns"],
                "readiness": copy.deepcopy(updated.get("attention_readiness") or {}),
                "evidence_diversity": copy.deepcopy(
                    updated.get("evidence_diversity") or {}
                ),
                "assessment_binding": copy.deepcopy(
                    updated.get("assessment_binding") or {}
                ),
            },
        )
        return updated

    def _terminal_result(
        self,
        record: dict[str, Any],
        *,
        evaluated: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._result(
            record,
            evaluated=evaluated,
            replayed=False,
            evidence_added_count=0,
        )
        result["reason"] = "attention_hypothesis_terminal_non_revivable"
        result["replayed"] = False
        return result

    def _current_parent_issue(
        self,
        evaluated: dict[str, Any],
    ) -> str | None:
        """Bind admission to the exact durable GeneralSituation head.

        This method is called from inside the Attention ledger mutation, while
        the shared writer transaction is held.  A delayed assessment therefore
        cannot become the first hypothesis record after its parent advanced.
        """

        state = self.state_store.read_json(GeneralSituationRuntime.STATE_FILE)
        if not GeneralSituationRuntime._healthy_state(state):
            return "general_situation_state_corrupt"
        parent = self._records(state.get("general_situations")).get(
            str(evaluated.get("general_situation_id") or "")
        )
        if not isinstance(parent, dict):
            return "attention_parent_source_missing"
        current_revision = self._nonnegative_int(parent.get("parent_revision"))
        received_revision = self._nonnegative_int(evaluated.get("parent_revision"))
        if received_revision < current_revision:
            return "attention_parent_revision_out_of_order"
        if received_revision > current_revision:
            return "attention_parent_revision_not_durable"
        expected = self._parent_binding(parent)
        received = copy.deepcopy(evaluated.get("parent_binding") or {})
        if received != expected:
            return "attention_parent_binding_conflict"
        assessment_binding = evaluated.get("assessment_binding")
        if not isinstance(assessment_binding, dict):
            return "general_attention_assessment_binding_missing"
        snapshots = self.state_store.read_snapshot(
            [
                "situation_state.json",
                GeneralSituationRuntime.STATE_FILE,
                "user_goals.json",
                "project_guardian_attention_state.json",
            ]
        )
        revision_bindings = {
            "situation_state_revision": "situation_state.json",
            "general_situation_state_revision": GeneralSituationRuntime.STATE_FILE,
            "goal_state_revision": "user_goals.json",
            "guardian_attention_state_revision": (
                "project_guardian_attention_state.json"
            ),
        }
        for binding_key, state_file in revision_bindings.items():
            if assessment_binding.get(binding_key) != self._nonnegative_int(
                snapshots[state_file].get("_state_revision")
            ):
                return "general_attention_dependency_revision_stale"
        assessed_at = self._aware_time(assessment_binding.get("assessed_at"))
        if assessed_at is None:
            return "general_attention_assessed_at_invalid"
        commit_now = self._now()
        assessment_age = (commit_now - assessed_at).total_seconds()
        if (
            assessment_age < -self.MAX_ASSESSMENT_ADMISSION_AGE_SECONDS
            or assessment_age > self.MAX_ASSESSMENT_ADMISSION_AGE_SECONDS
        ):
            return "general_attention_assessment_outside_admission_window"
        canonical_scheduler = GeneralAttentionScheduler(
            self.state_store,
            clock=lambda: assessed_at,
        )
        canonical_assessment = canonical_scheduler.assess(parent)
        try:
            canonical = self._validated_assessment(
                parent,
                canonical_assessment,
            )
        except (TypeError, ValueError):
            return "general_attention_canonical_assessment_unavailable"
        if any(
            canonical.get(key) != evaluated.get(key)
            for key in (
                "general_attention_scorer_version",
                "components",
                "unknowns",
                "evidence_diversity",
                "assessment_binding",
            )
        ) or [ref.to_dict() for ref in canonical["evidence_refs"]] != [
            ref.to_dict() for ref in evaluated["current_evidence_refs"]
        ]:
            return "general_attention_assessment_not_canonical"
        return None

    @staticmethod
    def _parent_binding(parent: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(parent.get(key))
            for key in (
                "schema_version",
                "general_situation_id",
                "user_id",
                "session_scope_keys",
                "workspace_anchor_key",
                "aggregation_scope",
                "primary_anchor_key",
                "common_anchor_keys",
                "child_refs",
                "distinct_event_count",
                "parent_revision",
                "status",
                "causality_asserted",
                "model_similarity_used_for_merge",
                "effective_start",
                "effective_end",
                "expires_at",
                "created_at",
                "updated_at",
            )
        }

    def _validated_parent(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("general situation must be a mapping")
        if (
            str(value.get("schema_version") or "")
            != GeneralSituationRuntime.RECORD_SCHEMA_VERSION
        ):
            raise ValueError("general situation schema mismatch")
        general_id = normalize_scope_component(
            value.get("general_situation_id"),
            "general_situation_id",
        )
        revision = value.get("parent_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("parent revision must be positive")
        if value.get("causality_asserted") is not False:
            raise ValueError("general situation cannot assert causality")
        if value.get("model_similarity_used_for_merge") is not False:
            raise ValueError("model similarity cannot establish a general situation")
        refs = self._child_refs(value.get("child_refs"))
        distinct = len({ref.source_event_id for ref in refs})
        if (
            len(refs) < self.MIN_DISTINCT_CURRENT_EVENTS
            or distinct < self.MIN_DISTINCT_CURRENT_EVENTS
            or value.get("distinct_event_count") != distinct
        ):
            raise ValueError("general situation distinct event binding is invalid")
        selected = copy.deepcopy(value)
        selected["general_situation_id"] = general_id
        selected["parent_revision"] = revision
        selected["child_refs"] = [ref.to_dict() for ref in refs]
        return selected

    def _validated_assessment(
        self,
        parent: dict[str, Any],
        value: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("general attention assessment must be a mapping")
        if str(value.get("schema_version") or "") != "veyra.general_attention_assessment.v1":
            raise ValueError("general attention assessment schema mismatch")
        if str(value.get("scorer_version") or "") != GeneralAttentionScheduler.SCORER_VERSION:
            raise ValueError("general attention scorer mismatch")
        if (
            str(value.get("general_situation_id") or "")
            != parent["general_situation_id"]
            or value.get("parent_revision") != parent["parent_revision"]
        ):
            raise ValueError("assessment is not bound to the parent revision")
        authority = value.get("authority")
        if (
            not isinstance(authority, dict)
            or not self._REQUIRED_AUTHORITY_KEYS.issubset(authority)
            or any(item is not False for item in authority.values())
        ):
            raise ValueError("assessment authority boundary is invalid")
        evidence_refs = self._child_refs(value.get("evidence"))
        parent_refs = self._child_refs(parent.get("child_refs"))
        if {ref.key for ref in evidence_refs} != {ref.key for ref in parent_refs}:
            raise ValueError("assessment evidence is not the exact parent projection")
        unknowns = value.get("unknowns")
        if not isinstance(unknowns, list) or any(
            not isinstance(item, str) or not item.strip() for item in unknowns
        ):
            raise ValueError("assessment unknowns are invalid")
        raw_components = value.get("components")
        if not isinstance(raw_components, dict) or not set(raw_components).issubset(
            GeneralAttentionScheduler.WEIGHTS
        ):
            raise ValueError("assessment components are invalid")
        components: dict[str, dict[str, Any]] = {}
        for name, weight in GeneralAttentionScheduler.WEIGHTS.items():
            raw = raw_components.get(name)
            if raw is None:
                components[name] = self._component(name, None)
                continue
            if not isinstance(raw, dict):
                raise ValueError("assessment component must be a mapping")
            value_number = self._unit_number(raw.get("value"))
            if raw.get("value") is not None and value_number is None:
                raise ValueError("assessment component value is invalid")
            supplied_weight = raw.get("weight")
            if (
                isinstance(supplied_weight, bool)
                or not isinstance(supplied_weight, (int, float))
                or abs(float(supplied_weight) - weight) > 0.000001
            ):
                raise ValueError("assessment component weight is invalid")
            expected_weighted = (
                None
                if value_number is None
                else round(value_number * weight, 6)
            )
            supplied_weighted = raw.get("weighted_score")
            if expected_weighted is None:
                if supplied_weighted is not None:
                    raise ValueError("unknown component cannot have a weighted score")
            elif (
                isinstance(supplied_weighted, bool)
                or not isinstance(supplied_weighted, (int, float))
                or abs(float(supplied_weighted) - expected_weighted) > 0.000001
            ):
                raise ValueError("assessment component weighted score is invalid")
            components[name] = self._component(name, value_number)
        return {
            "schema_version": str(value["schema_version"]),
            "general_attention_scorer_version": str(value["scorer_version"]),
            "components": components,
            "unknowns": list(dict.fromkeys(item.strip() for item in unknowns)),
            "evidence_refs": sorted(evidence_refs, key=self._ref_object_sort_key),
            "evidence_diversity": self._validated_evidence_diversity(
                value.get("evidence_diversity"),
                evidence_refs=evidence_refs,
            ),
            "assessment_binding": self._validated_assessment_binding(
                parent,
                value.get("assessment_binding"),
            ),
        }

    @classmethod
    def _validated_assessment_binding(
        cls,
        parent: dict[str, Any],
        value: Any,
    ) -> dict[str, Any]:
        expected_keys = {
            "schema_version",
            "scorer_version",
            "assessed_at",
            "general_situation_id",
            "parent_revision",
            "parent_digest",
            "situation_state_revision",
            "general_situation_state_revision",
            "goal_state_revision",
            "guardian_attention_state_revision",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ValueError("general attention assessment binding is invalid")
        assessed_at = cls._aware_time(value.get("assessed_at"))
        if (
            value.get("schema_version")
            != GeneralAttentionScheduler.ASSESSMENT_BINDING_SCHEMA_VERSION
            or value.get("scorer_version")
            != GeneralAttentionScheduler.SCORER_VERSION
            or assessed_at is None
            or value.get("general_situation_id")
            != parent.get("general_situation_id")
            or value.get("parent_revision") != parent.get("parent_revision")
            or value.get("parent_digest")
            != GeneralAttentionScheduler._parent_digest(parent)
        ):
            raise ValueError("general attention assessment binding does not match")
        for key in (
            "situation_state_revision",
            "general_situation_state_revision",
            "goal_state_revision",
            "guardian_attention_state_revision",
        ):
            revision = value.get(key)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError("general attention state revision is invalid")
        return {
            **copy.deepcopy(value),
            "assessed_at": assessed_at.isoformat(),
        }

    @classmethod
    def _validated_evidence_diversity(
        cls,
        value: Any,
        *,
        evidence_refs: list[ChildSituationRef],
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "ruleset_version",
            "time_bucket_seconds",
            "units",
            "unit_count",
            "producer_count",
            "fact_kind_count",
            "time_bucket_count",
            "profile_complete",
            "diversity_requirement_met",
            "non_observed_excluded",
            "non_observed_excluded_count",
            "unknowns",
            "profile_digest",
        }:
            raise ValueError("attention evidence diversity projection is invalid")
        if (
            value.get("schema_version")
            != GeneralAttentionScheduler.EVIDENCE_DIVERSITY_SCHEMA_VERSION
            or value.get("ruleset_version")
            != "observed_producer_fact_time_bucket.v2"
            or value.get("time_bucket_seconds")
            != GeneralAttentionScheduler.EVIDENCE_TIME_BUCKET_SECONDS
        ):
            raise ValueError("attention evidence diversity ruleset mismatch")
        excluded = value.get("non_observed_excluded")
        if (
            not isinstance(excluded, list)
            or value.get("non_observed_excluded_count") != len(excluded)
            or any(
                not isinstance(item, dict)
                or set(item) != {"child_ref_key", "epistemic_status"}
                or item.get("epistemic_status") == "observed"
                or item.get("epistemic_status")
                not in GeneralAttentionScheduler.EPISTEMIC_STATUSES
                for item in excluded
            )
        ):
            raise ValueError("attention non-observed exclusion record is invalid")
        raw_units = value.get("units")
        unknowns = value.get("unknowns")
        if (
            not isinstance(raw_units, list)
            or not isinstance(unknowns, list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in unknowns
            )
            or len(unknowns) != len(set(unknowns))
        ):
            raise ValueError("attention evidence diversity values are invalid")
        ref_by_key = {ref.key: ref for ref in evidence_refs}
        units: list[dict[str, Any]] = []
        for raw in raw_units:
            if not isinstance(raw, dict) or set(raw) != {
                "child_ref_key",
                "source_event_id",
                "producer_id",
                "fact_kind",
                "epistemic_status",
                "utc_time_bucket",
            }:
                raise ValueError("attention evidence diversity unit is invalid")
            ref_key = str(raw.get("child_ref_key") or "")
            source_event_id = str(raw.get("source_event_id") or "")
            producer_id = str(raw.get("producer_id") or "").strip()
            fact_kind = str(raw.get("fact_kind") or "").strip()
            epistemic_status = str(raw.get("epistemic_status") or "").strip()
            bucket = raw.get("utc_time_bucket")
            ref = ref_by_key.get(ref_key)
            if (
                ref is None
                or source_event_id != ref.source_event_id
                or not producer_id
                or len(producer_id) > 240
                or not fact_kind
                or len(fact_kind) > 240
                # Re-checked here rather than trusted from the scheduler: a
                # counted unit must be a direct observation.
                or epistemic_status != "observed"
                or isinstance(bucket, bool)
                or not isinstance(bucket, int)
            ):
                raise ValueError("attention evidence diversity unit binding is invalid")
            units.append(
                {
                    "child_ref_key": ref_key,
                    "source_event_id": source_event_id,
                    "producer_id": producer_id,
                    "fact_kind": fact_kind,
                    "epistemic_status": epistemic_status,
                    "utc_time_bucket": bucket,
                }
            )
        canonical_units = sorted(
            units,
            key=lambda item: (
                item["child_ref_key"],
                item["producer_id"],
                item["fact_kind"],
                item["utc_time_bucket"],
            ),
        )
        if units != canonical_units or len(
            {item["child_ref_key"] for item in units}
        ) != len(units):
            raise ValueError("attention evidence diversity units are not canonical")
        producer_count = len({item["producer_id"] for item in units})
        fact_kind_count = len({item["fact_kind"] for item in units})
        time_bucket_count = len({item["utc_time_bucket"] for item in units})
        profile_complete = len(units) == len(ref_by_key) and not unknowns
        diversity_met = bool(
            profile_complete
            and len(units) >= cls.MIN_DISTINCT_CURRENT_EVENTS
            and max(producer_count, fact_kind_count, time_bucket_count) >= 2
        )
        expected_counts = {
            "unit_count": len(units),
            "producer_count": producer_count,
            "fact_kind_count": fact_kind_count,
            "time_bucket_count": time_bucket_count,
            "profile_complete": profile_complete,
            "diversity_requirement_met": diversity_met,
            "profile_digest": stable_digest(
                "veyra.attention_evidence_diversity.profile.v1",
                units,
            ),
        }
        if any(value.get(key) != expected for key, expected in expected_counts.items()):
            raise ValueError("attention evidence diversity summary is inconsistent")
        return {
            "schema_version": value["schema_version"],
            "ruleset_version": value["ruleset_version"],
            "time_bucket_seconds": value["time_bucket_seconds"],
            "units": canonical_units,
            **expected_counts,
            "non_observed_excluded": copy.deepcopy(excluded),
            "non_observed_excluded_count": len(excluded),
            "unknowns": list(unknowns),
        }

    def _component(self, name: str, value: float | None) -> dict[str, Any]:
        weight = GeneralAttentionScheduler.WEIGHTS[name]
        return {
            "value": value,
            "weight": weight,
            "weighted_score": (
                None if value is None else round(value * weight, 6)
            ),
            "source": GeneralAttentionScheduler._component_source(name),
        }

    @classmethod
    def _public_hypothesis(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Project only reviewed owner-visible fields as a second boundary."""

        return {
            key: copy.deepcopy(value.get(key))
            for key in cls._PUBLIC_HYPOTHESIS_KEYS
            if key in value
        }

    def _result(
        self,
        record: dict[str, Any],
        *,
        evaluated: dict[str, Any],
        replayed: bool,
        evidence_added_count: int,
    ) -> dict[str, Any]:
        status = str(record.get("status") or "candidate")
        surface_evaluated = evaluated
        current_readiness = evaluated.get("attention_readiness")
        if (
            replayed
            and status == "confirmed"
            and isinstance(current_readiness, dict)
            and current_readiness.get("confirmation_blockers") == []
        ):
            try:
                surface_evaluated = {
                    "general_situation_id": record.get("general_situation_id"),
                    "parent_revision": record.get("parent_revision"),
                    "general_attention_scorer_version": record.get(
                        "general_attention_scorer_version"
                    ),
                    "components": copy.deepcopy(record.get("components") or {}),
                    "unknowns": copy.deepcopy(record.get("unknowns") or []),
                    "attention_readiness": copy.deepcopy(
                        record.get("attention_readiness") or {}
                    ),
                    "evidence_diversity": copy.deepcopy(
                        record.get("evidence_diversity") or {}
                    ),
                    "assessment_binding": copy.deepcopy(
                        record.get("assessment_binding") or {}
                    ),
                    "current_evidence_refs": [
                        ChildSituationRef.from_dict(item)
                        for item in record.get("current_evidence_refs", [])
                    ],
                }
            except (TypeError, ValueError):
                surface_evaluated = evaluated
        return {
            "status": status,
            "replayed": replayed,
            "evidence_added_count": evidence_added_count,
            "hypothesis": copy.deepcopy(record),
            "surface_assessment": self._surface(
                surface_evaluated,
                status=status,
                record=record,
            ),
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }

    def _surface(
        self,
        evaluated: dict[str, Any],
        *,
        status: str,
        record: dict[str, Any] | None = None,
        extra_unknown: str | None = None,
    ) -> dict[str, Any]:
        readiness = evaluated.get("attention_readiness")
        diversity = evaluated.get("evidence_diversity")
        eligible = bool(
            status == "confirmed"
            and extra_unknown is None
            and isinstance(record, dict)
            and record.get("status") == "confirmed"
            and isinstance(readiness, dict)
            and readiness.get("confirmation_blockers") == []
            and isinstance(diversity, dict)
            and diversity.get("profile_complete") is True
            and diversity.get("diversity_requirement_met") is True
        )
        unknowns = list(evaluated.get("unknowns") or [])
        if extra_unknown:
            unknowns.append(extra_unknown)
        output = {
            "schema_version": self.SURFACE_SCHEMA_VERSION,
            "scorer_version": self.RULESET_VERSION,
            "upstream_scorer_version": evaluated.get(
                "general_attention_scorer_version"
            ),
            "general_situation_id": evaluated.get("general_situation_id"),
            "parent_revision": evaluated.get("parent_revision"),
            "status": "eligible" if eligible else "awaiting_evidence",
            "hypothesis_status": status,
            "score": evaluated.get("attention_readiness", {}).get("value"),
            "threshold": self.MIN_PARTIAL_WEIGHTED_SCORE,
            "components": copy.deepcopy(evaluated.get("components") or {}),
            "unknowns": list(dict.fromkeys(unknowns)),
            "evidence": [
                ref.to_dict()
                for ref in evaluated.get("current_evidence_refs", [])
                if isinstance(ref, ChildSituationRef)
            ],
            "attention_readiness": copy.deepcopy(
                evaluated.get("attention_readiness") or {}
            ),
            "evidence_diversity": copy.deepcopy(
                evaluated.get("evidence_diversity") or {}
            ),
            "assessment_binding": copy.deepcopy(
                evaluated.get("assessment_binding") or {}
            ),
            "eligible": eligible,
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }
        if isinstance(record, dict):
            output["attention_hypothesis_ref"] = {
                "hypothesis_id": str(record.get("hypothesis_id") or ""),
                "hypothesis_revision": self._nonnegative_int(
                    record.get("hypothesis_revision")
                ),
                "ruleset_version": self.RULESET_VERSION,
                "readiness_semantics": str(
                    evaluated.get("attention_readiness", {}).get("semantics")
                    or "attention_policy_readiness_not_factual_probability"
                ),
            }
        return output

    def _closed(
        self,
        reason: str,
        *,
        detail: str | None = None,
        evaluated: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": "fail_closed",
            "reason": reason,
            "hypothesis": None,
            "surface_assessment": (
                self._surface(
                    evaluated,
                    status="candidate",
                    extra_unknown=reason,
                )
                if isinstance(evaluated, dict)
                else None
            ),
            "is_fact": False,
            "causality_asserted": False,
            "authority": self._authority_boundary(),
        }
        if detail:
            output["detail"] = detail
        return output

    @classmethod
    def _healthy_state(cls, state: dict[str, Any]) -> bool:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            return False
        state_revision = state.get("_state_revision")
        if (
            isinstance(state_revision, bool)
            or not isinstance(state_revision, int)
            or state_revision < 1
        ):
            return False
        if (
            str(state.get("schema_version") or "")
            != cls.STATE_SCHEMA_VERSION
            or str(state.get("ruleset_version") or "")
            != cls.RULESET_VERSION
        ):
            return False
        hypotheses = state.get("hypotheses")
        identity_index = state.get("identity_index")
        lifecycle_events = state.get("lifecycle_events", {})
        if (
            not isinstance(hypotheses, dict)
            or not isinstance(identity_index, dict)
            or not isinstance(lifecycle_events, dict)
            or len(lifecycle_events) > cls.MAX_LIFECYCLE_EVENTS
            or state.get("lifecycle_event_count", len(lifecycle_events))
            != len(lifecycle_events)
            or not all(
                isinstance(key, str)
                and bool(key)
                and isinstance(item, dict)
                for key, item in hypotheses.items()
            )
            or not all(
                isinstance(key, str)
                and isinstance(item, str)
                and bool(key)
                and bool(item)
                for key, item in identity_index.items()
            )
            or len(hypotheses) != len(identity_index)
            or state.get("hypothesis_count") != len(hypotheses)
            or state.get("status_counts") != cls._status_counts(hypotheses)
            or (
                state.get("updated_at") is not None
                and cls._aware_time(state.get("updated_at")) is None
            )
        ):
            return False
        if any(
            not cls._valid_lifecycle_event(signal_id, event)
            for signal_id, event in lifecycle_events.items()
        ):
            return False
        return all(
            cls._valid_record(
                record,
                hypothesis_id=hypothesis_id,
                identity_digest=str(record.get("identity_digest") or ""),
            )
            and identity_index.get(str(record.get("identity_digest") or ""))
            == hypothesis_id
            for hypothesis_id, record in hypotheses.items()
        )

    @classmethod
    def _valid_record(
        cls,
        record: dict[str, Any],
        *,
        hypothesis_id: str,
        identity_digest: str,
    ) -> bool:
        if not isinstance(record, dict) or set(record) != cls._RECORD_KEYS:
            return False
        authority = record.get("authority")
        identity = record.get("identity")
        expected_identity_keys = {
            "general_situation_schema_version",
            "assessment_schema_version",
            "general_attention_scorer_version",
            "general_situation_id",
            "user_id",
            "workspace_anchor_key",
            "primary_anchor_key",
            "ruleset_version",
        }
        if not isinstance(identity, dict) or set(identity) != expected_identity_keys:
            return False
        computed_identity_digest = stable_digest(
            "veyra.attention_hypothesis.identity.v2",
            identity,
        )
        readiness = record.get("attention_readiness")
        hypothesis_revision = record.get("hypothesis_revision")
        evaluation_count = record.get("evaluation_count")
        parent_revision = record.get("parent_revision")
        valid = (
            str(record.get("schema_version") or "") == cls.RECORD_SCHEMA_VERSION
            and str(record.get("hypothesis_id") or "") == hypothesis_id
            and str(record.get("identity_digest") or "") == identity_digest
            and identity_digest == computed_identity_digest
            and hypothesis_id == "ahyp_" + computed_identity_digest[:24]
            and str(record.get("ruleset_version") or "")
            == cls.RULESET_VERSION
            and identity.get("ruleset_version") == cls.RULESET_VERSION
            and identity.get("general_situation_schema_version")
            == GeneralSituationRuntime.RECORD_SCHEMA_VERSION
            and identity.get("assessment_schema_version")
            == "veyra.general_attention_assessment.v1"
            and identity.get("general_attention_scorer_version")
            == GeneralAttentionScheduler.SCORER_VERSION
            and record.get("general_attention_scorer_version")
            == GeneralAttentionScheduler.SCORER_VERSION
            and identity.get("general_situation_id")
            == record.get("general_situation_id")
            and identity.get("user_id") == record.get("user_id")
            and identity.get("workspace_anchor_key")
            == record.get("workspace_anchor_key")
            and identity.get("primary_anchor_key")
            == record.get("primary_anchor_key")
            and str(record.get("status") or "")
            in {
                "candidate",
                "accumulating",
                "confirmed",
                "contradicted",
                "expired",
                "superseded",
            }
            and isinstance(hypothesis_revision, int)
            and not isinstance(hypothesis_revision, bool)
            and hypothesis_revision >= 1
            and isinstance(evaluation_count, int)
            and not isinstance(evaluation_count, bool)
            and evaluation_count >= 1
            and isinstance(parent_revision, int)
            and not isinstance(parent_revision, bool)
            and parent_revision >= 1
            and cls._aware_time(record.get("expires_at")) is not None
            and isinstance(readiness, dict)
            and readiness.get("semantics")
            == "attention_policy_readiness_not_factual_probability"
            and readiness.get("is_probability") is False
            and readiness.get("is_fact") is False
            and readiness.get("model_confidence_used") is False
            and record.get("is_fact") is False
            and record.get("causality_asserted") is False
            and record.get("model_confidence_used") is False
            and isinstance(record.get("evidence_refs"), list)
            and isinstance(record.get("current_evidence_refs"), list)
            and isinstance(record.get("components"), dict)
            and isinstance(record.get("unknowns"), list)
            and isinstance(record.get("evidence_diversity"), dict)
            and isinstance(record.get("assessment_binding"), dict)
            and isinstance(record.get("parent_binding"), dict)
            and isinstance(record.get("session_scope_keys"), list)
            and isinstance(record.get("common_anchor_keys"), list)
            and authority == cls._authority_boundary()
        )
        if not valid:
            return False
        try:
            refs = [
                ChildSituationRef.from_dict(item)
                for item in record.get("evidence_refs", [])
                if isinstance(item, dict)
            ]
            current_refs = [
                ChildSituationRef.from_dict(item)
                for item in record.get("current_evidence_refs", [])
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError):
            return False
        if not (
            len(refs) == len(record.get("evidence_refs", []))
            and len({cls._ref_stream_key(ref) for ref in refs}) == len(refs)
            and record.get("evidence_count") == len(refs)
            and len(current_refs)
            == len(record.get("current_evidence_refs", []))
            and len({cls._ref_stream_key(ref) for ref in current_refs})
            == len(current_refs)
            and {ref.key for ref in current_refs}.issubset(
                {ref.key for ref in refs}
            )
        ):
            return False
        try:
            session_scope_keys = cls._canonical_strings(
                record.get("session_scope_keys"),
                field="session_scope_keys",
            )
            common_anchor_keys = cls._canonical_strings(
                record.get("common_anchor_keys"),
                field="common_anchor_keys",
            )
            if session_scope_keys != record.get("session_scope_keys"):
                return False
            if common_anchor_keys != record.get("common_anchor_keys"):
                return False
            diversity = cls._validated_evidence_diversity(
                record.get("evidence_diversity"),
                evidence_refs=current_refs,
            )
            assessment_binding = cls._validated_assessment_binding(
                record.get("parent_binding"),
                record.get("assessment_binding"),
            )
        except (TypeError, ValueError):
            return False
        if diversity != record.get("evidence_diversity"):
            return False
        if assessment_binding != record.get("assessment_binding"):
            return False
        parent_binding = record.get("parent_binding")
        if cls._parent_binding(parent_binding) != parent_binding:
            return False
        try:
            parent_session_scope_keys = cls._canonical_strings(
                parent_binding.get("session_scope_keys"),
                field="parent_session_scope_keys",
            )
            parent_common_anchor_keys = cls._canonical_strings(
                parent_binding.get("common_anchor_keys"),
                field="parent_common_anchor_keys",
            )
            parent_refs = cls._child_refs(parent_binding.get("child_refs"))
        except (TypeError, ValueError):
            return False
        if (
            parent_binding.get("schema_version")
            != GeneralSituationRuntime.RECORD_SCHEMA_VERSION
            or parent_binding.get("user_id") != record.get("user_id")
            or parent_session_scope_keys != session_scope_keys
            or parent_binding.get("session_scope_keys") != session_scope_keys
            or parent_binding.get("workspace_anchor_key")
            != record.get("workspace_anchor_key")
            or parent_binding.get("primary_anchor_key")
            != record.get("primary_anchor_key")
            or parent_common_anchor_keys != common_anchor_keys
            or parent_binding.get("common_anchor_keys") != common_anchor_keys
            or parent_binding.get("general_situation_id")
            != record.get("general_situation_id")
            or parent_binding.get("parent_revision") != parent_revision
            or parent_binding.get("expires_at") != record.get("expires_at")
            or parent_binding.get("distinct_event_count")
            != len({ref.source_event_id for ref in parent_refs})
            or {ref.key for ref in parent_refs}
            != {ref.key for ref in current_refs}
            or parent_binding.get("causality_asserted") is not False
            or parent_binding.get("model_similarity_used_for_merge") is not False
        ):
            return False
        created_at = cls._aware_time(record.get("created_at"))
        updated_at = cls._aware_time(record.get("updated_at"))
        first_confirmed_at = cls._aware_time(record.get("first_confirmed_at"))
        confirmed_at = cls._aware_time(record.get("confirmed_at"))
        if (
            created_at is None
            or updated_at is None
            or updated_at < created_at
            or (
                record.get("first_confirmed_at") is not None
                and first_confirmed_at is None
            )
            or (
                record.get("confirmed_at") is not None
                and confirmed_at is None
            )
            or (
                first_confirmed_at is not None
                and not created_at <= first_confirmed_at <= updated_at
            )
            or (
                confirmed_at is not None
                and (
                    first_confirmed_at is None
                    or not first_confirmed_at <= confirmed_at <= updated_at
                )
            )
        ):
            return False
        components = record.get("components")
        if set(components) != set(GeneralAttentionScheduler.WEIGHTS):
            return False
        known: dict[str, dict[str, Any]] = {}
        for name, weight in GeneralAttentionScheduler.WEIGHTS.items():
            item = components.get(name)
            if not isinstance(item, dict) or set(item) != {
                "value",
                "weight",
                "weighted_score",
                "source",
            }:
                return False
            value = item.get("value")
            normalized_value = cls._unit_number(value)
            if value is not None and normalized_value is None:
                return False
            expected_weighted = (
                None
                if normalized_value is None
                else round(normalized_value * weight, 6)
            )
            if (
                item.get("weight") != weight
                or item.get("weighted_score") != expected_weighted
                or item.get("source")
                != GeneralAttentionScheduler._component_source(name)
            ):
                return False
            if normalized_value is not None:
                known[name] = item
        unknowns = record.get("unknowns")
        if (
            any(not isinstance(item, str) or not item.strip() for item in unknowns)
            or len(unknowns) != len(set(unknowns))
        ):
            return False
        component_coverage = round(
            sum(float(item["weight"]) for item in known.values()),
            6,
        )
        partial_score = round(
            sum(float(item["weighted_score"]) for item in known.values()),
            6,
        )
        critical_unknowns = sorted(
            item for item in unknowns if cls._critical_unknown(item)
        )
        distinct_event_count = len(
            {ref.source_event_id for ref in current_refs}
        )
        distinct_current = (
            distinct_event_count >= cls.MIN_DISTINCT_CURRENT_EVENTS
            and "two_distinct_current_child_events_required" not in unknowns
        )
        high_impact_known = bool(cls._HIGH_IMPACT_COMPONENTS.intersection(known))
        blockers: list[str] = []
        if not distinct_current:
            blockers.append("distinct_current_events_below_minimum")
        if component_coverage < cls.MIN_COMPONENT_COVERAGE:
            blockers.append("component_coverage_below_threshold")
        if not high_impact_known:
            blockers.append("high_impact_component_required")
        if critical_unknowns:
            blockers.append("critical_unknowns_present")
        if partial_score < cls.MIN_PARTIAL_WEIGHTED_SCORE:
            blockers.append("partial_weighted_score_below_threshold")
        if diversity.get("profile_complete") is not True:
            blockers.append("structured_evidence_profile_incomplete")
        if diversity.get("diversity_requirement_met") is not True:
            blockers.append("independent_evidence_dimension_required")
        expected_readiness = {
            "schema_version": "veyra.attention_readiness.v1",
            "kind": "deterministic_policy_readiness",
            "value": partial_score,
            "threshold": cls.MIN_PARTIAL_WEIGHTED_SCORE,
            "component_coverage": component_coverage,
            "minimum_component_coverage": cls.MIN_COMPONENT_COVERAGE,
            "distinct_current_event_requirement_met": distinct_current,
            "current_distinct_event_count_lower_bound": (
                cls.MIN_DISTINCT_CURRENT_EVENTS if distinct_current else 0
            ),
            "minimum_distinct_current_events": cls.MIN_DISTINCT_CURRENT_EVENTS,
            "high_impact_component_known": high_impact_known,
            "critical_unknowns": critical_unknowns,
            "confirmation_blockers": blockers,
            "evidence_diversity": {
                key: copy.deepcopy(diversity.get(key))
                for key in (
                    "ruleset_version",
                    "time_bucket_seconds",
                    "unit_count",
                    "producer_count",
                    "fact_kind_count",
                    "time_bucket_count",
                    "profile_complete",
                    "diversity_requirement_met",
                    "profile_digest",
                )
            },
            "is_probability": False,
            "is_fact": False,
            "model_confidence_used": False,
            "semantics": "attention_policy_readiness_not_factual_probability",
        }
        lifecycle = cls._lifecycle_status({"unknowns": unknowns})
        expected_status = (
            lifecycle
            if lifecycle is not None
            else "confirmed"
            if not blockers
            else "candidate"
            if evaluation_count == 1
            else "accumulating"
        )
        if (
            readiness != expected_readiness
            or record.get("status") != expected_status
            or record.get("parent_distinct_event_count") != distinct_event_count
            or record.get("current_distinct_event_count_lower_bound")
            != expected_readiness["current_distinct_event_count_lower_bound"]
        ):
            return False
        if expected_status == "confirmed":
            if not record.get("confirmed_at") or not record.get("first_confirmed_at"):
                return False
        elif record.get("confirmed_at") is not None:
            return False
        expected_assessment_digest = stable_digest(
            "veyra.attention_hypothesis.assessment.v1",
            {
                "identity_digest": identity_digest,
                "general_situation_id": record.get("general_situation_id"),
                "parent_revision": parent_revision,
                "evidence": [ref.to_dict() for ref in current_refs],
                "components": components,
                "unknowns": unknowns,
                "readiness": readiness,
                "evidence_diversity": diversity,
                "assessment_binding": assessment_binding,
            },
        )
        return record.get("last_assessment_digest") == expected_assessment_digest

    @staticmethod
    def _critical_unknown(value: str) -> bool:
        selected = str(value or "").strip().lower()
        return (
            "corrupt" in selected
            or "missing" in selected
            or selected
            in {
                "invalid_general_situation",
                "invalid_child_ref",
                "general_situation_expired",
                "two_distinct_current_child_events_required",
            }
        )

    @staticmethod
    def _child_refs(value: Any) -> list[ChildSituationRef]:
        if not isinstance(value, list):
            raise ValueError("child references must be a list")
        refs: list[ChildSituationRef] = []
        keys: set[str] = set()
        streams: set[tuple[str, str]] = set()
        for raw in value:
            if not isinstance(raw, dict) or set(raw) != {
                "situation_id",
                "observation_revision",
                "source_event_id",
                "digest",
            }:
                raise ValueError("child reference projection is invalid")
            ref = ChildSituationRef.from_dict(raw)
            if ref.key in keys:
                raise ValueError("duplicate immutable child reference")
            stream_key = AttentionHypothesisRuntime._ref_stream_key(ref)
            if stream_key in streams:
                raise ValueError("duplicate child evidence stream")
            keys.add(ref.key)
            streams.add(stream_key)
            refs.append(ref)
        return refs

    @staticmethod
    def _ref_stream_key(ref: ChildSituationRef) -> tuple[str, str]:
        return (ref.situation_id, ref.source_event_id)

    @staticmethod
    def _canonical_strings(value: Any, *, field: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{field} must be a non-empty list")
        selected = [
            normalize_scope_component(item, field)
            for item in value
        ]
        canonical = sorted(set(selected))
        if selected != canonical:
            raise ValueError(f"{field} must be sorted and unique")
        return canonical

    @staticmethod
    def _anchor_key(
        value: Any,
        *,
        field: str,
        required_kind: str | None = None,
    ) -> str:
        selected = normalize_scope_component(value, field)
        if ":" not in selected:
            raise ValueError(f"{field} is not a structured anchor")
        kind, ref_id = selected.split(":", 1)
        if not kind or not ref_id or (required_kind and kind != required_kind):
            raise ValueError(f"{field} is not a valid structured anchor")
        return selected

    @staticmethod
    def _unit_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        selected = float(value)
        return selected if 0.0 <= selected <= 1.0 else None

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
    def _string_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): str(item)
            for key, item in value.items()
            if str(key) and str(item)
        }

    @staticmethod
    def _status_counts(
        hypotheses: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        counts = {
            "candidate": 0,
            "accumulating": 0,
            "confirmed": 0,
            "contradicted": 0,
            "expired": 0,
            "superseded": 0,
        }
        for item in hypotheses.values():
            status = str(item.get("status") or "")
            if status in counts:
                counts[status] += 1
        return counts

    @staticmethod
    def _authority_boundary() -> dict[str, bool]:
        return {
            "execution_allowed": False,
            "tool_allowed": False,
            "agent_allowed": False,
            "capability_grant_allowed": False,
            "route_change_allowed": False,
            "risk_change_allowed": False,
            "state_change_allowed": False,
            "notification_allowed": False,
            "external_delivery_allowed": False,
        }

    def _now(self) -> datetime:
        selected = self._clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("attention hypothesis clock must be timezone-aware")
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

    @staticmethod
    def _ref_sort_key(value: dict[str, Any]) -> tuple[str, int, str, str]:
        return (
            str(value.get("source_event_id") or ""),
            int(value.get("observation_revision") or 0),
            str(value.get("situation_id") or ""),
            str(value.get("digest") or ""),
        )

    @staticmethod
    def _ref_object_sort_key(
        value: ChildSituationRef,
    ) -> tuple[str, int, str, str]:
        return (
            value.source_event_id,
            value.observation_revision,
            value.situation_id,
            value.digest,
        )

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
