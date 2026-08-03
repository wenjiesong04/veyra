from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore
from interface.general_situation_contract import ChildSituationRef
from interface.general_situation_contract import stable_digest
from memory_bridge.scope import normalize_scope_component
from runtime.general_situation_runtime import GeneralSituationRuntime
from runtime.project_guardian_attention_runtime import (
    ProjectGuardianAttentionRuntime,
)


class GeneralAttentionScheduler:
    """Score a general Situation from structured, evidence-linked inputs only."""

    SCORER_VERSION = "veyra.general_attention.structured.v1"
    MIN_SCORE = 0.65
    MIN_EVIDENCE_COMPLETENESS = 0.5
    FRESHNESS_HORIZON_SECONDS = 24 * 60 * 60
    EVIDENCE_DIVERSITY_SCHEMA_VERSION = (
        "veyra.attention_evidence_diversity.v1"
    )
    ASSESSMENT_BINDING_SCHEMA_VERSION = (
        "veyra.general_attention_assessment_binding.v1"
    )
    EVIDENCE_TIME_BUCKET_SECONDS = 15 * 60
    #: Only a direct observation can satisfy the confirmation threshold. An
    #: inference or a prediction is retained as context and reported separately.
    EPISTEMIC_STATUSES = frozenset({"observed", "inference", "prediction"})
    WEIGHTS = {
        "goal_priority": 0.25,
        "severity": 0.20,
        "urgency": 0.15,
        "novelty": 0.10,
        "uncertainty": 0.10,
        "freshness": 0.10,
        "evidence_completeness": 0.10,
    }

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def assess(self, general_situation: dict[str, Any]) -> dict[str, Any]:
        """Pure assessment. Missing or unverifiable inputs never get guessed."""

        try:
            parent = self._validated_parent(general_situation)
            user_id = normalize_scope_component(parent.get("user_id"), "user_id")
        except (TypeError, ValueError) as exc:
            return self._awaiting(
                general_situation,
                ["invalid_general_situation"],
                detail=type(exc).__name__,
            )
        snapshots = self.state_store.read_snapshot(
            [
                "situation_state.json",
                "general_situation_state.json",
                "user_goals.json",
                "project_guardian_attention_state.json",
            ]
        )
        situation_state = snapshots["situation_state.json"]
        general_state = snapshots["general_situation_state.json"]
        goals_state = snapshots["user_goals.json"]
        guardian_attention_state = snapshots[
            "project_guardian_attention_state.json"
        ]
        if situation_state.get("_state_corrupt") is True:
            return self._awaiting(parent, ["situation_state_corrupt"])
        if goals_state.get("_state_corrupt") is True:
            return self._awaiting(parent, ["goal_state_corrupt"])
        if guardian_attention_state.get("_state_corrupt") is True:
            guardian_attention_state = {}

        now = self._now()
        assessment_binding = {
            "schema_version": self.ASSESSMENT_BINDING_SCHEMA_VERSION,
            "scorer_version": self.SCORER_VERSION,
            "assessed_at": now.isoformat(),
            "general_situation_id": parent.get("general_situation_id"),
            "parent_revision": parent.get("parent_revision"),
            "parent_digest": self._parent_digest(parent),
            "situation_state_revision": self._state_revision(situation_state),
            "general_situation_state_revision": self._state_revision(
                general_state
            ),
            "goal_state_revision": self._state_revision(goals_state),
            "guardian_attention_state_revision": self._state_revision(
                guardian_attention_state
            ),
        }
        expires_at = self._aware_time(parent.get("expires_at"))
        if expires_at is None or expires_at <= now:
            return self._awaiting(
                parent,
                ["general_situation_expired"],
                assessment_binding=assessment_binding,
            )
        resolved, resolution_unknowns = self._resolve_children(
            parent,
            situation_state=situation_state,
            user_id=user_id,
        )
        evidence_diversity = self._evidence_diversity(
            parent,
            resolved,
            now=now,
        )
        distinct_events = {
            str(item.get("source_event_id") or "")
            for item in resolved
            if str(item.get("source_event_id") or "")
        }
        unknowns = list(resolution_unknowns)
        if len(distinct_events) < 2:
            unknowns.append("two_distinct_current_child_events_required")

        goal_priority, goal_currentness_unknowns = self._goal_priority(
            parent,
            goals_state=goals_state,
            guardian_attention_state=guardian_attention_state,
            user_id=user_id,
            now=now,
        )
        unknowns.extend(goal_currentness_unknowns)
        values: dict[str, float | None] = {
            "goal_priority": goal_priority,
            "severity": self._child_component(resolved, "severity"),
            "urgency": self._child_component(resolved, "urgency"),
            "novelty": self._child_component(resolved, "novelty"),
            "uncertainty": self._child_component(resolved, "uncertainty"),
            "freshness": self._freshness(resolved, now=now),
            "evidence_completeness": self._child_component(
                resolved,
                "evidence_completeness",
            ),
        }
        for name, value in values.items():
            if value is None:
                unknowns.append(f"{name}_unknown")
        unknowns = list(dict.fromkeys(unknowns))
        components = {
            name: {
                "value": value,
                "weight": self.WEIGHTS[name],
                "weighted_score": (
                    round(value * self.WEIGHTS[name], 6)
                    if value is not None
                    else None
                ),
                "source": self._component_source(name),
            }
            for name, value in values.items()
        }
        if unknowns:
            return {
                **self._base(parent, assessment_binding=assessment_binding),
                "status": "awaiting_evidence",
                "score": None,
                "components": components,
                "unknowns": unknowns,
                "evidence": self._child_refs(parent),
                "evidence_diversity": evidence_diversity,
                "eligible": False,
            }
        evidence_completeness = float(values["evidence_completeness"] or 0.0)
        if evidence_completeness < self.MIN_EVIDENCE_COMPLETENESS:
            return {
                **self._base(parent, assessment_binding=assessment_binding),
                "status": "awaiting_evidence",
                "score": None,
                "components": components,
                "unknowns": ["evidence_completeness_below_threshold"],
                "evidence": self._child_refs(parent),
                "evidence_diversity": evidence_diversity,
                "eligible": False,
            }
        score = round(
            sum(
                float(value) * self.WEIGHTS[name]
                for name, value in values.items()
                if value is not None
            ),
            6,
        )
        eligible = score >= self.MIN_SCORE
        return {
            **self._base(parent, assessment_binding=assessment_binding),
            "status": "eligible" if eligible else "observed",
            "score": score,
            "threshold": self.MIN_SCORE,
            "components": components,
            "unknowns": [],
            "evidence": self._child_refs(parent),
            "evidence_diversity": evidence_diversity,
            "eligible": eligible,
        }

    def _evidence_diversity(
        self,
        parent: dict[str, Any],
        children: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Project typed evidence independence without reading any text."""

        refs: dict[str, ChildSituationRef] = {}
        for raw_ref in self._child_refs(parent):
            try:
                ref = ChildSituationRef.from_dict(raw_ref)
            except (TypeError, ValueError):
                continue
            refs[ref.situation_id] = ref
        units: list[dict[str, Any]] = []
        non_observed: list[dict[str, Any]] = []
        unknowns: list[str] = []
        for child in children:
            situation_id = str(child.get("situation_id") or "")
            ref = refs.get(situation_id)
            if ref is None:
                unknowns.append("structured_evidence_ref_missing")
                continue
            source = (
                child.get("source_event")
                if isinstance(child.get("source_event"), dict)
                else {}
            )
            if str(source.get("channel") or "") != "structured_observation":
                unknowns.append("structured_evidence_profile_incomplete")
                continue
            observations = (
                child.get("observations")
                if isinstance(child.get("observations"), list)
                else []
            )
            typed = [
                item.get("value")
                for item in observations
                if isinstance(item, dict)
                and isinstance(item.get("value"), dict)
                and str(item["value"].get("schema_version") or "")
                == "veyra.structured_observation.fact.v1"
            ]
            if len(typed) != 1:
                unknowns.append("structured_evidence_profile_ambiguous")
                continue
            fact = typed[0]
            producer_id = str(fact.get("producer_id") or "").strip()
            fact_kind = str(fact.get("fact_kind") or "").strip()
            epistemic_status = str(fact.get("epistemic_status") or "").strip()
            occurred_at = self._aware_time(source.get("occurred_at"))
            valid_from = self._aware_time(fact.get("valid_from"))
            valid_until = self._aware_time(fact.get("valid_until"))
            if (
                not producer_id
                or not fact_kind
                or epistemic_status not in self.EPISTEMIC_STATUSES
                or occurred_at is None
                or valid_from is None
                or valid_until is None
                or valid_from != occurred_at
            ):
                unknowns.append("structured_evidence_profile_incomplete")
                continue
            if epistemic_status != "observed":
                # An inference or a prediction is recorded as context but never
                # counts toward the confirmation threshold. Without this, a
                # model-derived unit with a distinct producer or fact kind would
                # satisfy diversity on its own.
                non_observed.append(
                    {
                        "child_ref_key": ref.key,
                        "epistemic_status": epistemic_status,
                    }
                )
                unknowns.append("structured_evidence_not_observed")
                continue
            if occurred_at > now + timedelta(minutes=5):
                unknowns.append("structured_evidence_from_future")
                continue
            if valid_until < now or valid_until < valid_from:
                unknowns.append("structured_evidence_expired")
                continue
            units.append(
                {
                    "child_ref_key": ref.key,
                    "source_event_id": ref.source_event_id,
                    "producer_id": producer_id,
                    "fact_kind": fact_kind,
                    "epistemic_status": epistemic_status,
                    "utc_time_bucket": int(occurred_at.timestamp())
                    // self.EVIDENCE_TIME_BUCKET_SECONDS,
                }
            )
        units.sort(
            key=lambda item: (
                str(item["child_ref_key"]),
                str(item["producer_id"]),
                str(item["fact_kind"]),
                int(item["utc_time_bucket"]),
            )
        )
        producer_count = len({str(item["producer_id"]) for item in units})
        fact_kind_count = len({str(item["fact_kind"]) for item in units})
        time_bucket_count = len({int(item["utc_time_bucket"]) for item in units})
        profile_complete = len(units) == len(refs) and not unknowns
        diversity_met = bool(
            profile_complete
            and len(units) >= 2
            and max(producer_count, fact_kind_count, time_bucket_count) >= 2
        )
        return {
            "schema_version": self.EVIDENCE_DIVERSITY_SCHEMA_VERSION,
            "ruleset_version": "observed_producer_fact_time_bucket.v2",
            "time_bucket_seconds": self.EVIDENCE_TIME_BUCKET_SECONDS,
            "units": units,
            "unit_count": len(units),
            "producer_count": producer_count,
            "fact_kind_count": fact_kind_count,
            "time_bucket_count": time_bucket_count,
            "profile_complete": profile_complete,
            "diversity_requirement_met": diversity_met,
            "non_observed_excluded": sorted(
                non_observed,
                key=lambda item: str(item["child_ref_key"]),
            ),
            "non_observed_excluded_count": len(non_observed),
            "unknowns": list(dict.fromkeys(unknowns)),
            "profile_digest": stable_digest(
                "veyra.attention_evidence_diversity.profile.v1",
                units,
            ),
        }

    def _resolve_children(
        self,
        parent: dict[str, Any],
        *,
        situation_state: dict[str, Any],
        user_id: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raw = situation_state.get("situations")
        if isinstance(raw, dict):
            children = [item for item in raw.values() if isinstance(item, dict)]
        elif isinstance(raw, list):
            children = [item for item in raw if isinstance(item, dict)]
        else:
            children = []
        by_id = {
            str(item.get("situation_id") or ""): item
            for item in children
            if str(item.get("situation_id") or "")
            and str(item.get("user_id") or "") == user_id
        }
        resolved: dict[str, dict[str, Any]] = {}
        unknowns: list[str] = []
        for raw_ref in self._child_refs(parent):
            try:
                ref = ChildSituationRef.from_dict(raw_ref)
            except (TypeError, ValueError):
                unknowns.append("invalid_child_ref")
                continue
            child = by_id.get(ref.situation_id)
            if not isinstance(child, dict):
                unknowns.append("child_situation_missing")
                continue
            revision = child.get("observation_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision != ref.observation_revision
                or str(child.get("source_event_id") or "")
                != ref.source_event_id
                or GeneralSituationRuntime.child_digest(child) != ref.digest
            ):
                # An older immutable ref is expected after the child advances.
                # It is not evidence for the current assessment.
                continue
            resolved[ref.situation_id] = copy.deepcopy(child)
        return list(resolved.values()), list(dict.fromkeys(unknowns))

    def _goal_priority(
        self,
        parent: dict[str, Any],
        *,
        goals_state: dict[str, Any],
        guardian_attention_state: dict[str, Any],
        user_id: str,
        now: datetime,
    ) -> tuple[float | None, list[str]]:
        goal_ids = {
            item.split(":", 1)[1]
            for item in parent.get("common_anchor_keys", [])
            if isinstance(item, str) and item.startswith("goal:") and ":" in item
        }
        if not goal_ids:
            return None, []
        priorities: list[float] = []
        current_goal_found = False
        guardian_policy_not_current = False
        goals = goals_state.get("goals") if isinstance(goals_state.get("goals"), list) else []
        policies = (
            guardian_attention_state.get("policies")
            if isinstance(guardian_attention_state.get("policies"), dict)
            else {}
        )
        for goal_id in goal_ids:
            owner_matches = [
                goal
                for goal in goals
                if isinstance(goal, dict)
                and str(goal.get("goal_id") or "") == goal_id
                and str(goal.get("user_id") or "") == user_id
            ]
            if len(owner_matches) != 1:
                continue
            goal = owner_matches[0]
            if str(goal.get("status") or "").strip().lower() != "active":
                continue

            # A controlled Project Guardian Goal receives its priority only
            # from the exact policy bound to its current immutable revision
            # and mutable state revision.  Retained policies from a paused,
            # completed, expired, or subsequently revised Goal are not
            # evidence for current attention.
            if self._guardian_goal_marker_present(goal):
                state_revision = goal.get("state_revision")
                if (
                    isinstance(state_revision, bool)
                    or not isinstance(state_revision, int)
                    or state_revision < 1
                ):
                    continue
                current_goal = ProjectGuardianAttentionRuntime._active_goal(
                    goals_state,
                    user_id=user_id,
                    goal_id=goal_id,
                    goal_revision=str(goal.get("revision") or ""),
                    expected_state_revision=state_revision,
                    now=now,
                )
                if (
                    current_goal is None
                ):
                    continue
                current_goal_found = True
                policy = policies.get(goal_id)
                if (
                    not isinstance(policy, dict)
                    or str(policy.get("goal_id") or "") != goal_id
                    or not ProjectGuardianAttentionRuntime._policy_matches_goal(
                        policy,
                        current_goal,
                    )
                ):
                    guardian_policy_not_current = True
                    continue
                value = self._unit_number(policy.get("goal_priority"))
                if value is not None:
                    priorities.append(value)
                continue

            current_goal_found = True
            value = self._unit_number(
                goal.get("goal_priority")
                if "goal_priority" in goal
                else goal.get("priority")
            )
            if value is not None:
                priorities.append(value)

        if priorities:
            return max(priorities), []
        if guardian_policy_not_current:
            return None, ["guardian_policy_missing_or_not_current"]
        if not current_goal_found:
            return None, ["active_goal_missing_or_not_current"]
        return None, []

    @staticmethod
    def _guardian_goal_marker_present(goal: dict[str, Any]) -> bool:
        """Do not downgrade a malformed controlled Goal to a generic Goal."""

        return bool(
            str(goal.get("kind") or "")
            == ProjectGuardianEvaluator.GOAL_KIND
            or str(goal.get("schema_version") or "")
            == ProjectGuardianEvaluator.GOAL_SCHEMA
            or str(goal.get("source") or "")
            == ProjectGuardianEvaluator.GOAL_SOURCE
        )

    def _child_component(
        self,
        children: list[dict[str, Any]],
        name: str,
    ) -> float | None:
        values: list[float] = []
        for child in children:
            components = (
                child.get("salience_components")
                if isinstance(child.get("salience_components"), dict)
                else {}
            )
            value = self._unit_number(components.get(name))
            if value is not None:
                values.append(value)
        return round(max(values), 6) if values else None

    def _freshness(
        self,
        children: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> float | None:
        values: list[float] = []
        for child in children:
            source = (
                child.get("source_event")
                if isinstance(child.get("source_event"), dict)
                else {}
            )
            observed = self._aware_time(
                source.get("occurred_at")
                or source.get("timestamp")
                or child.get("created_at")
            )
            if observed is None:
                return None
            age = (now - observed).total_seconds()
            if age < -300:
                return None
            age = max(0.0, age)
            values.append(
                max(0.0, 1.0 - age / self.FRESHNESS_HORIZON_SECONDS)
            )
        return round(sum(values) / len(values), 6) if values else None

    def _validated_parent(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("general situation must be a mapping")
        if str(value.get("schema_version") or "") != GeneralSituationRuntime.RECORD_SCHEMA_VERSION:
            raise ValueError("general situation schema mismatch")
        if self._nonnegative_int(value.get("distinct_event_count")) < 2:
            raise ValueError("general situation requires two distinct events")
        refs = value.get("child_refs")
        if not isinstance(refs, list) or len(refs) < 2:
            raise ValueError("general situation child refs are invalid")
        allowed_keys = {
            "situation_id",
            "observation_revision",
            "source_event_id",
            "digest",
        }
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != allowed_keys:
                raise ValueError("general situation contains a non-reference child projection")
            ChildSituationRef.from_dict(ref)
        return copy.deepcopy(value)

    def _awaiting(
        self,
        parent: dict[str, Any],
        unknowns: list[str],
        *,
        detail: str | None = None,
        assessment_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output = {
            **self._base(parent, assessment_binding=assessment_binding),
            "status": "awaiting_evidence",
            "score": None,
            "components": {},
            "unknowns": list(dict.fromkeys(unknowns)),
            "evidence": self._child_refs(parent),
            "evidence_diversity": self._evidence_diversity(
                parent if isinstance(parent, dict) else {},
                [],
                now=self._now(),
            ),
            "eligible": False,
        }
        if detail:
            output["detail"] = detail
        return output

    def _base(
        self,
        parent: dict[str, Any],
        *,
        assessment_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "veyra.general_attention_assessment.v1",
            "scorer_version": self.SCORER_VERSION,
            "general_situation_id": (
                parent.get("general_situation_id")
                if isinstance(parent, dict)
                else None
            ),
            "parent_revision": (
                parent.get("parent_revision")
                if isinstance(parent, dict)
                else None
            ),
            "assessment_binding": copy.deepcopy(assessment_binding),
            "authority": {
                "execution_allowed": False,
                "tool_allowed": False,
                "agent_allowed": False,
                "capability_grant_allowed": False,
                "route_change_allowed": False,
            },
        }

    @staticmethod
    def _state_revision(state: dict[str, Any]) -> int:
        value = state.get("_state_revision") if isinstance(state, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    @staticmethod
    def _parent_digest(parent: dict[str, Any]) -> str:
        projection = {
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
        return stable_digest(
            "veyra.general_attention.parent_binding.v1",
            projection,
        )

    @staticmethod
    def _component_source(name: str) -> str:
        if name == "goal_priority":
            return "active_structured_goal"
        if name == "freshness":
            return "verified_child_effective_time"
        return "child_salience_component"

    @staticmethod
    def _child_refs(parent: dict[str, Any]) -> list[dict[str, Any]]:
        refs = parent.get("child_refs") if isinstance(parent, dict) else []
        return [copy.deepcopy(item) for item in refs if isinstance(item, dict)] if isinstance(refs, list) else []

    @staticmethod
    def _unit_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        selected = float(value)
        return selected if 0.0 <= selected <= 1.0 else None

    def _now(self) -> datetime:
        selected = self._clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("attention scheduler clock must be timezone-aware")
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
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
