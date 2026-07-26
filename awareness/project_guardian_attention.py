from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, time, timezone
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from awareness.project_guardian import ProjectGuardianEvaluator


class ProjectGuardianAttentionScheduler:
    """Pure shadow attention policy for stable Project Guardian candidates.

    The scheduler deliberately has no state-store, Agent, notification, or
    execution dependency.  Its result is a counterfactual assessment only:
    ``would_suggest`` never means that a suggestion may be sent.

    ``policy_context`` is explicitly bound to one user/Goal/scope and uses:

    .. code-block:: python

        {
            "schema_version": "veyra.project_guardian_attention_context.v1",
            "user_id": "...",
            "goal_id": "...",
            "goal_revision": "...",
            "scope": {...ProjectGuardianEvaluator.SCOPE_FIELDS...},
            "goal_priority": 0.8,
            "deadline_at": "2026-07-26T18:00:00+00:00",
            "timezone": "Asia/Shanghai",
            "notifications_paused": False,
            "quiet_hours": {
                "enabled": True,
                "start": "22:00",
                "end": "08:00",
            },
            "notification_budget": {
                "local_date": "2026-07-27",
                "limit": 3,
                "used": 0,
            },
            "dismissal": {"active": False},
            "cooldown": {"until": None},
            "novelty": {"last_seen_candidate_revision": None},
            "capabilities": {"read_only_investigation": True},
        }

    The user-specific fields above have no guessed defaults.  Missing or
    malformed values remain explicit unknowns and block scoring.  Only
    versioned threshold/weight constants and the *future* cooldown duration use
    deterministic defaults.
    """

    SCHEMA_VERSION = "veyra.project_guardian_attention_evaluation.v1"
    ASSESSMENT_SCHEMA_VERSION = "veyra.project_guardian_attention_assessment.v1"
    CONTEXT_SCHEMA_VERSION = "veyra.project_guardian_attention_context.v1"
    RULESET_VERSION = "veyra.project_guardian_attention_ruleset.v1"
    POLICY_VERSION = "veyra.project_guardian_attention_policy.v1"

    COMPONENT_NAMES = (
        "goal_relevance",
        "severity_impact",
        "urgency",
        "information_value",
        "novelty",
        "actionability",
        "uncertainty",
        "interruption_cost",
        "cooldown",
        "compute_and_tool_cost",
    )
    POSITIVE_COMPONENTS = (
        "goal_relevance",
        "severity_impact",
        "urgency",
        "information_value",
        "novelty",
        "actionability",
    )
    NEGATIVE_COMPONENTS = (
        "uncertainty",
        "interruption_cost",
        "cooldown",
        "compute_and_tool_cost",
    )
    DEFAULT_THRESHOLDS = MappingProxyType(
        {
            "observe": 0.25,
            "investigate": 0.55,
            "suggest": 0.75,
            "act": 0.92,
        }
    )
    DEFAULT_WEIGHTS = MappingProxyType(
        {
            "goal_relevance": 0.24,
            "severity_impact": 0.22,
            "urgency": 0.18,
            "information_value": 0.12,
            "novelty": 0.10,
            "actionability": 0.14,
            "uncertainty": 0.14,
            "interruption_cost": 0.10,
            "cooldown": 0.08,
            "compute_and_tool_cost": 0.06,
        }
    )
    SIGNAL_IMPACT = MappingProxyType(
        {
            "git_dirty": 0.55,
            "ci_failed": 0.80,
            "deployment_intent": 0.65,
        }
    )
    DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60
    MAX_CANDIDATES = 64

    SUPPRESSION_PRECEDENCE = (
        "user_paused",
        "quiet_hours_active",
        "notification_budget_exhausted",
        "user_dismissed",
        "cooldown_active",
    )

    def evaluate(
        self,
        *,
        candidates: list[dict[str, Any]],
        policy_context: dict[str, Any],
        now: datetime | str,
    ) -> dict[str, Any]:
        """Return deterministic, compact shadow assessments.

        Candidates outside the policy context's exact user/Goal/scope are
        rejected without echoing their identifiers.  This prevents a caller
        from using one user's policy context to inspect another user's
        candidate.
        """

        if not isinstance(candidates, list):
            raise TypeError("candidates must be a list")
        evaluated_at = self._time(now)
        if evaluated_at is None:
            raise ValueError("evaluation time must be timezone-aware")
        identity = self._policy_identity(policy_context)
        policy, policy_blockers = self._attention_policy(policy_context)

        assessments: list[dict[str, Any]] = []
        deduplicated = 0
        seen: set[tuple[str, str]] = set()
        eligible: list[dict[str, Any]] = []
        for raw_candidate in candidates:
            candidate = self._candidate(raw_candidate)
            if candidate is None or not self._same_identity(candidate, identity):
                continue
            eligible.append(candidate)
        for candidate in eligible[: self.MAX_CANDIDATES]:
            dedupe_key = (
                str(candidate["candidate_id"]),
                str(candidate["candidate_revision"]),
            )
            if dedupe_key in seen:
                deduplicated += 1
                continue
            seen.add(dedupe_key)
            assessments.append(
                self._assess(
                    candidate=candidate,
                    policy_context=policy_context,
                    identity=identity,
                    policy=policy,
                    policy_blockers=policy_blockers,
                    evaluated_at=evaluated_at,
                )
            )
        assessments.sort(
            key=lambda item: (
                str(item.get("candidate_ref", {}).get("candidate_id") or ""),
                str(item.get("candidate_ref", {}).get("candidate_revision") or ""),
            )
        )
        return {
            "schema_version": self.SCHEMA_VERSION,
            "ruleset_version": self.RULESET_VERSION,
            "policy_version": self.POLICY_VERSION,
            "status": "assessed" if assessments else "no_eligible_candidate",
            "evaluated_at": self._iso(evaluated_at),
            "eligible_candidate_count": len(eligible),
            "assessment_count": len(assessments),
            "deduplicated_candidate_count": deduplicated,
            "capacity_limited": len(eligible) > self.MAX_CANDIDATES,
            "assessments": assessments,
        }

    def _assess(
        self,
        *,
        candidate: dict[str, Any],
        policy_context: dict[str, Any],
        identity: dict[str, Any],
        policy: dict[str, Any],
        policy_blockers: list[str],
        evaluated_at: datetime,
    ) -> dict[str, Any]:
        suppression_key = self.suppression_key_for(candidate)
        blockers = list(policy_blockers)
        active_suppressors: list[str] = []
        component_values: dict[str, dict[str, Any]] = {}

        priority = self._number(policy_context.get("goal_priority"))
        if priority is None or not 0.0 <= priority <= 1.0:
            component_values["goal_relevance"] = self._unknown(
                "missing_or_invalid_goal_priority"
            )
            blockers.append("missing_or_invalid_goal_priority")
        else:
            component_values["goal_relevance"] = self._known(
                priority,
                "active_goal_priority_bound",
            )

        signal_kinds = candidate["signal_kinds"]
        base_impact = max(self.SIGNAL_IMPACT[kind] for kind in signal_kinds)
        impact = min(1.0, base_impact + 0.05 * (len(signal_kinds) - 1))
        component_values["severity_impact"] = self._known(
            impact,
            "release_risk_signal_impact",
            signal_class_count=len(signal_kinds),
        )

        user_zone, zone_reason = self._zone(policy_context.get("timezone"))
        deadline = self._time(policy_context.get("deadline_at"))
        if user_zone is None:
            blockers.append(zone_reason)
        if deadline is None:
            blockers.append("missing_or_invalid_deadline")
        if user_zone is None or deadline is None:
            component_values["urgency"] = self._unknown(
                *[
                    reason
                    for reason in (zone_reason, "missing_or_invalid_deadline")
                    if (
                        (reason == zone_reason and user_zone is None)
                        or (
                            reason == "missing_or_invalid_deadline"
                            and deadline is None
                        )
                    )
                ]
            )
        else:
            urgency, urgency_reason, horizon_seconds = self._urgency(
                deadline,
                evaluated_at,
            )
            component_values["urgency"] = self._known(
                urgency,
                urgency_reason,
                deadline_at=self._iso(deadline),
                horizon_seconds=horizon_seconds,
            )

        unknowns = candidate["unknowns"]
        missing_signal_count = max(
            0,
            len(ProjectGuardianEvaluator.SIGNAL_COMPONENTS) - len(signal_kinds),
        )
        information_value = min(
            1.0,
            0.25
            + 0.10 * min(len(unknowns), 4)
            + 0.15 * missing_signal_count,
        )
        component_values["information_value"] = self._known(
            information_value,
            "bounded_unknown_resolution_value",
            candidate_unknown_count=len(unknowns),
            missing_signal_class_count=missing_signal_count,
        )

        novelty = policy_context.get("novelty")
        if (
            not isinstance(novelty, dict)
            or "last_seen_candidate_revision" not in novelty
        ):
            component_values["novelty"] = self._unknown(
                "novelty_state_unknown"
            )
            blockers.append("novelty_state_unknown")
        else:
            last_seen = novelty.get("last_seen_candidate_revision")
            if last_seen is not None and not self._bounded_text(last_seen, 120):
                component_values["novelty"] = self._unknown(
                    "novelty_state_invalid"
                )
                blockers.append("novelty_state_invalid")
            else:
                repeated = (
                    last_seen is not None
                    and str(last_seen)
                    == str(candidate["candidate_revision"])
                )
                component_values["novelty"] = self._known(
                    0.0 if repeated else 1.0,
                    (
                        "candidate_revision_already_seen"
                        if repeated
                        else "candidate_revision_not_seen"
                    ),
                )

        capabilities = policy_context.get("capabilities")
        read_only_allowed = (
            capabilities.get("read_only_investigation")
            if isinstance(capabilities, dict)
            else None
        )
        if not isinstance(read_only_allowed, bool):
            component_values["actionability"] = self._unknown(
                "read_only_capability_unknown"
            )
            blockers.append("read_only_capability_unknown")
        else:
            check_count = len(candidate["candidate_advice_checks"])
            actionability = (
                min(1.0, 0.5 + 0.1 * check_count)
                if read_only_allowed and check_count
                else 0.1
            )
            component_values["actionability"] = self._known(
                actionability,
                (
                    "read_only_investigation_available"
                    if read_only_allowed and check_count
                    else (
                        "no_read_only_check_available"
                        if read_only_allowed
                        else "read_only_investigation_unavailable"
                    )
                ),
                proposed_check_count=check_count,
            )

        uncertainty = min(
            1.0,
            0.15 * min(len(unknowns), 6)
            + 0.10 * missing_signal_count,
        )
        component_values["uncertainty"] = self._known(
            uncertainty,
            "explicit_candidate_unknowns",
            candidate_unknown_count=len(unknowns),
            missing_signal_class_count=missing_signal_count,
        )

        pause_state = policy_context.get("notifications_paused")
        if not isinstance(pause_state, bool):
            blockers.append("notifications_pause_state_unknown")
        elif pause_state:
            active_suppressors.append("user_paused")

        quiet_state, quiet_blockers = self._quiet_state(
            policy_context.get("quiet_hours"),
            user_zone=user_zone,
            evaluated_at=evaluated_at,
        )
        blockers.extend(quiet_blockers)
        if quiet_state is True:
            active_suppressors.append("quiet_hours_active")

        budget_state, budget_ratio, budget_detail, budget_blockers = (
            self._budget_state(
                policy_context.get("notification_budget"),
                user_zone=user_zone,
                evaluated_at=evaluated_at,
            )
        )
        blockers.extend(budget_blockers)
        if budget_state is True:
            active_suppressors.append("notification_budget_exhausted")

        dismissal_state, dismissal_reason = self._dismissal_state(
            policy_context.get("dismissal"),
            suppression_key=suppression_key,
        )
        if dismissal_state is None:
            blockers.append(dismissal_reason)
        elif dismissal_state:
            active_suppressors.append("user_dismissed")

        cooldown_state, cooldown_detail, cooldown_reason = self._cooldown_state(
            policy_context.get("cooldown"),
            suppression_key=suppression_key,
            evaluated_at=evaluated_at,
        )
        if cooldown_state is None:
            blockers.append(cooldown_reason)
            component_values["cooldown"] = self._unknown(cooldown_reason)
        else:
            if cooldown_state:
                active_suppressors.append("cooldown_active")
            component_values["cooldown"] = self._known(
                1.0 if cooldown_state else 0.0,
                "cooldown_active" if cooldown_state else "cooldown_inactive",
                **cooldown_detail,
            )

        interruption_reasons: list[str] = []
        if not isinstance(pause_state, bool):
            interruption_reasons.append("notifications_pause_state_unknown")
        if quiet_state is None:
            interruption_reasons.extend(quiet_blockers)
        if budget_state is None:
            interruption_reasons.extend(budget_blockers)
        if dismissal_state is None:
            interruption_reasons.append(dismissal_reason)
        if cooldown_state is None:
            interruption_reasons.append(cooldown_reason)
        if interruption_reasons:
            component_values["interruption_cost"] = self._unknown(
                *interruption_reasons
            )
        else:
            interruption_cost = min(
                1.0,
                0.50 * budget_ratio
                + (0.50 if quiet_state else 0.0)
                + (1.0 if pause_state else 0.0)
                + (1.0 if dismissal_state else 0.0)
                + (1.0 if cooldown_state else 0.0),
            )
            component_values["interruption_cost"] = self._known(
                interruption_cost,
                "bounded_interruption_policy_cost",
                quiet_hours_active=bool(quiet_state),
                notifications_paused=bool(pause_state),
                notification_budget=budget_detail,
                dismissed=bool(dismissal_state),
                cooldown_active=bool(cooldown_state),
            )

        component_values["compute_and_tool_cost"] = self._known(
            0.0,
            "no_agent_or_tool_work_in_shadow",
        )
        blockers = self._ordered_unique(blockers)
        components = {
            name: component_values[name]
            for name in self.COMPONENT_NAMES
        }
        score = self._score(components)
        ordered_suppressors = [
            reason
            for reason in self.SUPPRESSION_PRECEDENCE
            if reason in active_suppressors
        ]
        disposition, primary_reason, threshold_reason = self._disposition(
            score=score,
            thresholds=policy["thresholds"],
            blockers=blockers,
            active_suppressors=ordered_suppressors,
        )
        reason_codes = self._ordered_unique(
            [
                primary_reason,
                *ordered_suppressors,
                *([threshold_reason] if threshold_reason else []),
                *blockers,
            ]
        )
        policy_binding = {
            "policy_version": self.POLICY_VERSION,
            "thresholds": dict(policy["thresholds"]),
            "weights": dict(self.DEFAULT_WEIGHTS),
            "default_cooldown_seconds": policy["default_cooldown_seconds"],
            "context_identity": identity,
            "decision_context": self._decision_context_binding(policy_context),
        }
        policy_revision = f"pgap_{self._digest(policy_binding)[:20]}"
        assessment_id = (
            f"pga_{self._digest({
                'candidate_id': candidate['candidate_id'],
                'candidate_revision': candidate['candidate_revision'],
                'policy_revision': policy_revision,
            })[:20]}"
        )
        assessment = {
            "schema_version": self.ASSESSMENT_SCHEMA_VERSION,
            "ruleset_version": self.RULESET_VERSION,
            "policy_version": self.POLICY_VERSION,
            "assessment_id": assessment_id,
            "candidate_ref": {
                "candidate_id": candidate["candidate_id"],
                "candidate_revision": candidate["candidate_revision"],
                "candidate_kind": candidate["candidate_kind"],
            },
            "user_id": identity["user_id"],
            "goal_id": identity["goal_id"],
            "goal_revision": identity["goal_revision"],
            "scope": dict(identity["scope"]),
            "components": components,
            "score": score,
            "thresholds": dict(policy["thresholds"]),
            "would_disposition": disposition,
            "reason_codes": reason_codes,
            "blockers": blockers,
            "suppression": {
                "key": suppression_key,
                "active": disposition == "suppressed",
                "primary_reason": (
                    primary_reason if disposition == "suppressed" else None
                ),
                "cooldown_until": cooldown_detail.get("until"),
                "cooldown_seconds": policy["default_cooldown_seconds"],
            },
            "analysis_mode": "deterministic_read_only_shadow",
            "agent_invoked": False,
            "shadow_only": True,
            "notification_allowed": False,
            "execution_allowed": False,
            "interrupt_eligible": False,
            "evaluated_at": self._iso(evaluated_at),
            "policy_revision": policy_revision,
        }
        revision_semantics = {
            key: value
            for key, value in assessment.items()
            if key not in {"assessment_id", "evaluated_at"}
        }
        assessment["assessment_revision"] = (
            f"pgar_{self._digest(revision_semantics)[:20]}"
        )
        return assessment

    @classmethod
    def suppression_key_for(cls, candidate: dict[str, Any]) -> str:
        """Return the stable Situation-level key used by dismiss/cooldown."""

        scope = (
            candidate.get("scope")
            if isinstance(candidate.get("scope"), dict)
            else {}
        )
        identity = {
            "user_id": str(candidate.get("user_id") or ""),
            "goal_id": str(candidate.get("goal_id") or ""),
            "goal_revision": str(candidate.get("goal_revision") or ""),
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "candidate_kind": str(candidate.get("candidate_kind") or ""),
            "scope": {
                key: str(scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            },
        }
        return f"pgas_{cls._digest(identity)[:24]}"

    def _candidate(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        scope = value.get("scope") if isinstance(value.get("scope"), dict) else {}
        normalized_scope = {
            key: self._identity_text(scope.get(key), 240)
            for key in ProjectGuardianEvaluator.SCOPE_FIELDS
        }
        user_id = self._identity_text(value.get("user_id"), 240)
        goal_id = self._identity_text(value.get("goal_id"), 240)
        goal_revision = self._identity_text(value.get("goal_revision"), 120)
        candidate_id = self._identity_text(value.get("candidate_id"), 120)
        candidate_revision = self._identity_text(
            value.get("candidate_revision"),
            120,
        )
        raw_signal_kinds = value.get("signal_kinds")
        signal_kinds = (
            [str(item) for item in raw_signal_kinds]
            if isinstance(raw_signal_kinds, list)
            and all(isinstance(item, str) for item in raw_signal_kinds)
            else []
        )
        raw_signals = value.get("signals")
        signal_rows = (
            raw_signals
            if isinstance(raw_signals, list)
            and all(isinstance(item, dict) for item in raw_signals)
            else []
        )
        signal_row_kinds = sorted(
            {
                str(item.get("kind") or "")
                for item in signal_rows
                if str(item.get("kind") or "")
            }
        )
        raw_unknowns = value.get("unknowns")
        unknowns = (
            self._ordered_unique(
                [
                    self._bounded_text(item, 160)
                    for item in raw_unknowns[:64]
                    if self._bounded_text(item, 160)
                ]
            )
            if isinstance(raw_unknowns, list)
            and all(isinstance(item, str) for item in raw_unknowns[:64])
            else []
        )
        advice = (
            value.get("candidate_advice")
            if isinstance(value.get("candidate_advice"), dict)
            else {}
        )
        raw_checks = advice.get("checks")
        checks = (
            self._ordered_unique(
                [
                    self._bounded_text(item, 160)
                    for item in raw_checks[:32]
                    if self._bounded_text(item, 160)
                ]
            )
            if isinstance(raw_checks, list)
            and all(isinstance(item, str) for item in raw_checks[:32])
            else []
        )
        expected_id = ProjectGuardianEvaluator.candidate_id_for(
            user_id=user_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            scope=normalized_scope,
        )
        expected_revision = ProjectGuardianEvaluator.candidate_revision_for(value)
        if (
            str(value.get("schema_version") or "")
            != ProjectGuardianEvaluator.CANDIDATE_SCHEMA
            or str(value.get("record_kind") or "") != "situation_candidate"
            or str(value.get("candidate_kind") or "")
            != ProjectGuardianEvaluator.CANDIDATE_KIND
            or not user_id
            or not goal_id
            or not goal_revision
            or not all(normalized_scope.values())
            or candidate_id != expected_id
            or candidate_revision != expected_revision
            or len(signal_kinds) < 2
            or signal_kinds != sorted(set(signal_kinds))
            or any(kind not in self.SIGNAL_IMPACT for kind in signal_kinds)
            or signal_row_kinds != signal_kinds
            or self._time(value.get("qualified_at")) is None
            or str(value.get("analysis_mode") or "")
            != "deterministic_read_only"
            or value.get("agent_invoked") is not False
            or value.get("shadow_only") is not True
            or value.get("notification_allowed") is not False
            or value.get("execution_allowed") is not False
            or value.get("interrupt_eligible") is not False
        ):
            return None
        return {
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
            "user_id": user_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "scope": normalized_scope,
            "signal_kinds": signal_kinds,
            "unknowns": unknowns,
            "candidate_advice_checks": checks,
        }

    def _policy_identity(self, context: Any) -> dict[str, Any]:
        if not isinstance(context, dict):
            raise TypeError("policy_context must be an object")
        scope = context.get("scope") if isinstance(context.get("scope"), dict) else {}
        identity = {
            "user_id": self._identity_text(context.get("user_id"), 240),
            "goal_id": self._identity_text(context.get("goal_id"), 240),
            "goal_revision": self._identity_text(
                context.get("goal_revision"),
                120,
            ),
            "scope": {
                key: self._identity_text(scope.get(key), 240)
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            },
        }
        if (
            str(context.get("schema_version") or "")
            != self.CONTEXT_SCHEMA_VERSION
            or not identity["user_id"]
            or not identity["goal_id"]
            or not identity["goal_revision"]
            or not all(identity["scope"].values())
        ):
            raise ValueError("policy context identity is incomplete or invalid")
        return identity

    def _attention_policy(
        self,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        raw = context.get("attention_policy")
        if raw is None:
            return {
                "thresholds": dict(self.DEFAULT_THRESHOLDS),
                "default_cooldown_seconds": self.DEFAULT_COOLDOWN_SECONDS,
            }, []
        if not isinstance(raw, dict):
            return {
                "thresholds": dict(self.DEFAULT_THRESHOLDS),
                "default_cooldown_seconds": self.DEFAULT_COOLDOWN_SECONDS,
            }, ["attention_policy_invalid"]

        thresholds = {
            name: self._number(
                raw.get(f"{name}_threshold", self.DEFAULT_THRESHOLDS[name])
            )
            for name in ("observe", "investigate", "suggest", "act")
        }
        cooldown = self._integer(
            raw.get("default_cooldown_seconds", self.DEFAULT_COOLDOWN_SECONDS)
        )
        valid_thresholds = (
            all(value is not None for value in thresholds.values())
            and 0.0
            <= float(thresholds["observe"])
            < float(thresholds["investigate"])
            < float(thresholds["suggest"])
            <= float(thresholds["act"])
            <= 1.0
        )
        if not valid_thresholds or cooldown is None or not 60 <= cooldown <= 604800:
            return {
                "thresholds": dict(self.DEFAULT_THRESHOLDS),
                "default_cooldown_seconds": self.DEFAULT_COOLDOWN_SECONDS,
            }, ["attention_policy_invalid"]
        return {
            "thresholds": {
                name: round(float(value), 6)
                for name, value in thresholds.items()
            },
            "default_cooldown_seconds": cooldown,
        }, []

    def _decision_context_binding(
        self,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Canonicalize only decision-relevant context for revision binding."""

        deadline = self._time(context.get("deadline_at"))
        zone, _ = self._zone(context.get("timezone"))
        quiet = (
            context.get("quiet_hours")
            if isinstance(context.get("quiet_hours"), dict)
            else {}
        )
        budget = (
            context.get("notification_budget")
            if isinstance(context.get("notification_budget"), dict)
            else {}
        )
        dismissal = (
            context.get("dismissal")
            if isinstance(context.get("dismissal"), dict)
            else {}
        )
        cooldown = (
            context.get("cooldown")
            if isinstance(context.get("cooldown"), dict)
            else {}
        )
        cooldown_until = self._time(cooldown.get("until"))
        novelty = (
            context.get("novelty")
            if isinstance(context.get("novelty"), dict)
            else {}
        )
        capabilities = (
            context.get("capabilities")
            if isinstance(context.get("capabilities"), dict)
            else {}
        )
        return {
            "goal_priority": self._number(context.get("goal_priority")),
            "deadline_at": self._iso(deadline) if deadline else None,
            "timezone": (
                str(context.get("timezone"))
                if zone is not None
                else None
            ),
            "notifications_paused": (
                context.get("notifications_paused")
                if isinstance(context.get("notifications_paused"), bool)
                else None
            ),
            "quiet_hours": {
                "enabled": (
                    quiet.get("enabled")
                    if isinstance(quiet.get("enabled"), bool)
                    else None
                ),
                "start": (
                    str(quiet.get("start"))
                    if isinstance(quiet.get("start"), str)
                    else None
                ),
                "end": (
                    str(quiet.get("end"))
                    if isinstance(quiet.get("end"), str)
                    else None
                ),
            },
            "notification_budget": {
                "local_date": (
                    str(budget.get("local_date"))
                    if isinstance(budget.get("local_date"), str)
                    else None
                ),
                "limit": self._integer(budget.get("limit")),
                "used": self._integer(budget.get("used")),
            },
            "dismissal": {
                "active": (
                    dismissal.get("active")
                    if isinstance(dismissal.get("active"), bool)
                    else None
                ),
                "suppression_key": (
                    str(dismissal.get("suppression_key"))
                    if isinstance(dismissal.get("suppression_key"), str)
                    else None
                ),
                "reason_code": (
                    str(dismissal.get("reason_code"))
                    if isinstance(dismissal.get("reason_code"), str)
                    else None
                ),
            },
            "cooldown": {
                "until": self._iso(cooldown_until) if cooldown_until else None,
                "suppression_key": (
                    str(cooldown.get("suppression_key"))
                    if isinstance(cooldown.get("suppression_key"), str)
                    else None
                ),
            },
            "novelty": {
                "last_seen_candidate_revision": (
                    str(novelty.get("last_seen_candidate_revision"))
                    if isinstance(
                        novelty.get("last_seen_candidate_revision"),
                        str,
                    )
                    else None
                ),
            },
            "capabilities": {
                "read_only_investigation": (
                    capabilities.get("read_only_investigation")
                    if isinstance(
                        capabilities.get("read_only_investigation"),
                        bool,
                    )
                    else None
                ),
            },
        }

    @staticmethod
    def _same_identity(
        candidate: dict[str, Any],
        identity: dict[str, Any],
    ) -> bool:
        return (
            candidate["user_id"] == identity["user_id"]
            and candidate["goal_id"] == identity["goal_id"]
            and candidate["goal_revision"] == identity["goal_revision"]
            and all(
                candidate["scope"][key] == identity["scope"][key]
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
        )

    def _quiet_state(
        self,
        value: Any,
        *,
        user_zone: ZoneInfo | None,
        evaluated_at: datetime,
    ) -> tuple[bool | None, list[str]]:
        if user_zone is None:
            return None, ["missing_or_invalid_timezone"]
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            return None, ["quiet_hours_unknown"]
        if value["enabled"] is False:
            return False, []
        start = self._clock(value.get("start"))
        end = self._clock(value.get("end"))
        if start is None or end is None or start == end:
            return None, ["quiet_hours_invalid"]
        local_clock = evaluated_at.astimezone(user_zone).time().replace(tzinfo=None)
        active = (
            start <= local_clock < end
            if start < end
            else local_clock >= start or local_clock < end
        )
        return active, []

    def _budget_state(
        self,
        value: Any,
        *,
        user_zone: ZoneInfo | None,
        evaluated_at: datetime,
    ) -> tuple[
        bool | None,
        float,
        dict[str, Any],
        list[str],
    ]:
        if user_zone is None:
            return None, 0.0, {}, ["missing_or_invalid_timezone"]
        if not isinstance(value, dict):
            return None, 0.0, {}, ["notification_budget_unknown"]
        limit = self._integer(value.get("limit"))
        used = self._integer(value.get("used"))
        local_date = self._bounded_text(value.get("local_date"), 10)
        expected_date = evaluated_at.astimezone(user_zone).date().isoformat()
        if (
            limit is None
            or used is None
            or limit < 0
            or used < 0
            or local_date != expected_date
        ):
            return None, 0.0, {}, ["notification_budget_invalid_or_stale"]
        exhausted = limit == 0 or used >= limit
        ratio = 1.0 if limit == 0 else min(1.0, used / limit)
        return (
            exhausted,
            ratio,
            {
                "local_date": local_date,
                "limit": limit,
                "used": used,
                "remaining": max(0, limit - used),
            },
            [],
        )

    def _dismissal_state(
        self,
        value: Any,
        *,
        suppression_key: str,
    ) -> tuple[bool | None, str]:
        if not isinstance(value, dict) or not isinstance(value.get("active"), bool):
            return None, "dismissal_state_unknown"
        if value["active"] is False:
            return False, "dismissal_inactive"
        if str(value.get("suppression_key") or "") != suppression_key:
            return None, "dismissal_binding_mismatch"
        return True, "user_dismissed"

    def _cooldown_state(
        self,
        value: Any,
        *,
        suppression_key: str,
        evaluated_at: datetime,
    ) -> tuple[bool | None, dict[str, Any], str]:
        if not isinstance(value, dict) or "until" not in value:
            return None, {}, "cooldown_state_unknown"
        raw_until = value.get("until")
        if raw_until is None or raw_until == "":
            return False, {"until": None, "remaining_seconds": 0}, "cooldown_inactive"
        until = self._time(raw_until)
        if until is None:
            return None, {}, "cooldown_state_invalid"
        if str(value.get("suppression_key") or "") != suppression_key:
            return None, {}, "cooldown_binding_mismatch"
        remaining = max(0, math.ceil((until - evaluated_at).total_seconds()))
        return (
            remaining > 0,
            {
                "until": self._iso(until),
                "remaining_seconds": remaining,
            },
            "cooldown_active" if remaining > 0 else "cooldown_inactive",
        )

    @staticmethod
    def _urgency(
        deadline: datetime,
        now: datetime,
    ) -> tuple[float, str, int]:
        seconds = math.floor((deadline - now).total_seconds())
        if seconds <= 0:
            return 1.0, "deadline_reached_or_overdue", seconds
        if seconds <= 60 * 60:
            return 1.0, "deadline_within_one_hour", seconds
        if seconds <= 6 * 60 * 60:
            return 0.9, "deadline_within_six_hours", seconds
        if seconds <= 24 * 60 * 60:
            return 0.75, "deadline_within_one_day", seconds
        if seconds <= 3 * 24 * 60 * 60:
            return 0.5, "deadline_within_three_days", seconds
        if seconds <= 7 * 24 * 60 * 60:
            return 0.3, "deadline_within_one_week", seconds
        return 0.1, "deadline_beyond_one_week", seconds

    def _score(self, components: dict[str, dict[str, Any]]) -> float | None:
        if any(
            components[name].get("status") != "known"
            for name in self.COMPONENT_NAMES
        ):
            return None
        positive = sum(
            float(components[name]["score"]) * self.DEFAULT_WEIGHTS[name]
            for name in self.POSITIVE_COMPONENTS
        )
        negative = sum(
            float(components[name]["score"]) * self.DEFAULT_WEIGHTS[name]
            for name in self.NEGATIVE_COMPONENTS
        )
        return round(max(0.0, min(1.0, positive - negative)), 6)

    @staticmethod
    def _disposition(
        *,
        score: float | None,
        thresholds: dict[str, float],
        blockers: list[str],
        active_suppressors: list[str],
    ) -> tuple[str, str, str | None]:
        if active_suppressors:
            return "suppressed", active_suppressors[0], None
        if blockers or score is None:
            return "suppressed", "required_context_unknown", None
        if score < thresholds["observe"]:
            return "suppressed", "below_observe_threshold", None
        if score < thresholds["investigate"]:
            return "observe", "observe_threshold_reached", None
        if score < thresholds["suggest"]:
            return (
                "investigate_read_only",
                "investigate_threshold_reached",
                None,
            )
        if score >= thresholds["act"]:
            return (
                "would_suggest",
                "suggest_threshold_reached",
                "act_threshold_has_no_authority_in_shadow",
            )
        return "would_suggest", "suggest_threshold_reached", None

    @staticmethod
    def _known(
        score: float,
        *reason_codes: str,
        **detail: Any,
    ) -> dict[str, Any]:
        value = {
            "status": "known",
            "score": round(max(0.0, min(1.0, float(score))), 6),
            "reason_codes": ProjectGuardianAttentionScheduler._ordered_unique(
                list(reason_codes)
            ),
        }
        if detail:
            value["detail"] = detail
        return value

    @staticmethod
    def _unknown(*reason_codes: str) -> dict[str, Any]:
        return {
            "status": "unknown",
            "score": None,
            "reason_codes": ProjectGuardianAttentionScheduler._ordered_unique(
                list(reason_codes)
            ),
        }

    @staticmethod
    def _zone(value: Any) -> tuple[ZoneInfo | None, str]:
        name = str(value or "").strip()
        if not name:
            return None, "missing_or_invalid_timezone"
        try:
            return ZoneInfo(name), ""
        except (ZoneInfoNotFoundError, ValueError):
            return None, "missing_or_invalid_timezone"

    @staticmethod
    def _clock(value: Any) -> time | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        try:
            parsed = time.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
            return None
        return parsed

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

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _number(value: Any) -> float | None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            return None
        selected = float(value)
        return selected if math.isfinite(selected) else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        return str("" if value is None else value).strip()[:limit]

    @staticmethod
    def _identity_text(value: Any, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        if not value or value != value.strip() or len(value) > limit:
            return ""
        return value

    @staticmethod
    def _ordered_unique(values: list[str]) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            selected.append(text)
        return selected

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
