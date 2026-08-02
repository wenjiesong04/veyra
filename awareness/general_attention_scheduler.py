from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.general_situation_contract import ChildSituationRef
from memory_bridge.scope import normalize_scope_component
from runtime.general_situation_runtime import GeneralSituationRuntime


class GeneralAttentionScheduler:
    """Score a general Situation from structured, evidence-linked inputs only."""

    SCORER_VERSION = "veyra.general_attention.structured.v1"
    MIN_SCORE = 0.65
    MIN_EVIDENCE_COMPLETENESS = 0.5
    FRESHNESS_HORIZON_SECONDS = 24 * 60 * 60
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
        situation_state = self.state_store.read_json("situation_state.json")
        goals_state = self.state_store.read_json("user_goals.json")
        guardian_attention_state = self.state_store.read_json(
            "project_guardian_attention_state.json"
        )
        if situation_state.get("_state_corrupt") is True:
            return self._awaiting(parent, ["situation_state_corrupt"])
        if goals_state.get("_state_corrupt") is True:
            return self._awaiting(parent, ["goal_state_corrupt"])
        if guardian_attention_state.get("_state_corrupt") is True:
            guardian_attention_state = {}

        now = self._now()
        expires_at = self._aware_time(parent.get("expires_at"))
        if expires_at is None or expires_at <= now:
            return self._awaiting(parent, ["general_situation_expired"])
        resolved, resolution_unknowns = self._resolve_children(
            parent,
            situation_state=situation_state,
            user_id=user_id,
        )
        distinct_events = {
            str(item.get("source_event_id") or "")
            for item in resolved
            if str(item.get("source_event_id") or "")
        }
        unknowns = list(resolution_unknowns)
        if len(distinct_events) < 2:
            unknowns.append("two_distinct_current_child_events_required")

        values: dict[str, float | None] = {
            "goal_priority": self._goal_priority(
                parent,
                goals_state=goals_state,
                guardian_attention_state=guardian_attention_state,
                user_id=user_id,
            ),
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
                **self._base(parent),
                "status": "awaiting_evidence",
                "score": None,
                "components": components,
                "unknowns": unknowns,
                "evidence": self._child_refs(parent),
                "eligible": False,
            }
        evidence_completeness = float(values["evidence_completeness"] or 0.0)
        if evidence_completeness < self.MIN_EVIDENCE_COMPLETENESS:
            return {
                **self._base(parent),
                "status": "awaiting_evidence",
                "score": None,
                "components": components,
                "unknowns": ["evidence_completeness_below_threshold"],
                "evidence": self._child_refs(parent),
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
            **self._base(parent),
            "status": "eligible" if eligible else "observed",
            "score": score,
            "threshold": self.MIN_SCORE,
            "components": components,
            "unknowns": [],
            "evidence": self._child_refs(parent),
            "eligible": eligible,
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
    ) -> float | None:
        goal_ids = {
            item.split(":", 1)[1]
            for item in parent.get("common_anchor_keys", [])
            if isinstance(item, str) and item.startswith("goal:") and ":" in item
        }
        if not goal_ids:
            return None
        priorities: list[float] = []
        goals = goals_state.get("goals") if isinstance(goals_state.get("goals"), list) else []
        for goal in goals:
            if (
                not isinstance(goal, dict)
                or str(goal.get("user_id") or "") != user_id
                or str(goal.get("goal_id") or "") not in goal_ids
                or str(goal.get("status") or "").strip().lower()
                in {"archived", "cancelled", "closed", "completed", "expired"}
            ):
                continue
            value = self._unit_number(
                goal.get("goal_priority")
                if "goal_priority" in goal
                else goal.get("priority")
            )
            if value is not None:
                priorities.append(value)
        policies = (
            guardian_attention_state.get("policies")
            if isinstance(guardian_attention_state.get("policies"), dict)
            else {}
        )
        for goal_id in goal_ids:
            policy = policies.get(goal_id)
            if not isinstance(policy, dict) or str(policy.get("user_id") or "") != user_id:
                continue
            value = self._unit_number(policy.get("goal_priority"))
            if value is not None:
                priorities.append(value)
        return max(priorities) if priorities else None

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
    ) -> dict[str, Any]:
        output = {
            **self._base(parent),
            "status": "awaiting_evidence",
            "score": None,
            "components": {},
            "unknowns": list(dict.fromkeys(unknowns)),
            "evidence": self._child_refs(parent),
            "eligible": False,
        }
        if detail:
            output["detail"] = detail
        return output

    def _base(self, parent: dict[str, Any]) -> dict[str, Any]:
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
            "authority": {
                "execution_allowed": False,
                "tool_allowed": False,
                "agent_allowed": False,
                "capability_grant_allowed": False,
                "route_change_allowed": False,
            },
        }

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
