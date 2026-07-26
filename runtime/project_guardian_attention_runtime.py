from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from datetime import datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from awareness.project_guardian import ProjectGuardianEvaluator
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ProjectGuardianAttentionConflict(ValueError):
    """An Attention policy update failed its controlled binding contract."""


class ProjectGuardianAttentionRuntime:
    """Persist deterministic, read-only Attention assessments for Guardian.

    This runtime is deliberately not a notification or execution system. It
    follows the Guardian kill switch, stores counterfactual assessments, and
    can aggregate explicitly grouped candidates into private shadow telemetry.
    It never publishes an Event, invokes an Agent, starts a cooldown, consumes
    a notification budget, or mutates a project.
    """

    STATE_FILE = "project_guardian_attention_state.json"
    SCHEMA_VERSION = "veyra.project_guardian_attention_state.v1"
    POLICY_SCHEMA = "veyra.project_guardian_attention_policy_binding.v1"
    SITUATION_SCHEMA = "veyra.project_guardian_general_attention_situation.v1"
    MAX_POLICIES = 64
    MAX_ASSESSMENTS = 128
    MAX_GENERAL_SITUATIONS = 64
    MAX_RUNS = 100
    MODES = {"disabled", "record_only", "shadow"}

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        clock: Callable[[], datetime] | None = None,
        scheduler: ProjectGuardianAttentionScheduler | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.scheduler = scheduler or ProjectGuardianAttentionScheduler()
        self._run_lock = threading.Lock()

    def set_policy(
        self,
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        expected_goal_state_revision: int,
        attention_group_id: str,
        goal_priority: float,
        deadline_at: str,
        timezone_name: str,
        notifications_paused: bool,
        quiet_hours: dict[str, Any],
        daily_notification_budget: int,
        expected_policy_revision: str | None = None,
    ) -> dict[str, Any]:
        """Bind one explicit Attention context to an exact active Goal."""

        selected_user = self._required_text(user_id, "user_id", 240)
        selected_goal = self._required_text(goal_id, "goal_id", 240)
        selected_goal_revision = self._required_text(
            goal_revision,
            "goal_revision",
            120,
        )
        selected_group = self._required_text(
            attention_group_id,
            "attention_group_id",
            120,
        )
        if any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789._:-"
            )
            for character in selected_group
        ):
            raise ValueError(
                "attention_group_id may contain only letters, digits, . _ : -"
            )
        selected_state_revision = self._positive_int(
            expected_goal_state_revision,
            "expected_goal_state_revision",
        )
        selected_priority = self._unit_number(
            goal_priority,
            "goal_priority",
        )
        selected_deadline = self._time(deadline_at)
        if selected_deadline is None:
            raise ValueError("deadline_at must be timezone-aware")
        selected_timezone = self._timezone(timezone_name)
        if not isinstance(notifications_paused, bool):
            raise ValueError("notifications_paused must be boolean")
        selected_quiet_hours = self._quiet_hours(quiet_hours)
        if (
            isinstance(daily_notification_budget, bool)
            or not isinstance(daily_notification_budget, int)
            or not 0 <= daily_notification_budget <= 100
        ):
            raise ValueError(
                "daily_notification_budget must be an integer from 0 to 100"
            )
        selected_expected_policy = (
            self._required_text(
                expected_policy_revision,
                "expected_policy_revision",
                120,
            )
            if expected_policy_revision is not None
            else None
        )
        now = self._aware_now()

        with self.state_store.writer_transaction():
            goals_state = self.state_store.read_json("user_goals.json")
            attention_state = self.state_store.read_json(self.STATE_FILE)
            self._require_healthy(goals_state, "user_goals.json")
            self._require_healthy(attention_state, self.STATE_FILE)
            goal = self._active_goal(
                goals_state,
                user_id=selected_user,
                goal_id=selected_goal,
                goal_revision=selected_goal_revision,
                expected_state_revision=selected_state_revision,
                now=now,
            )
            if goal is None:
                raise ProjectGuardianAttentionConflict(
                    "Attention policy does not match an active release Goal"
                )
            scope = {
                key: str((goal.get("scope") or {}).get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            }
            policies = self._dict_records(
                attention_state.get("policies")
            )
            existing = policies.get(selected_goal)
            current_policy_revision = (
                str(existing.get("policy_revision") or "")
                if isinstance(existing, dict)
                else None
            )
            if existing is None and selected_expected_policy is not None:
                raise ProjectGuardianAttentionConflict(
                    "expected_policy_revision was provided for a new policy"
                )
            if (
                existing is not None
                and selected_expected_policy != current_policy_revision
            ):
                raise ProjectGuardianAttentionConflict(
                    "expected_policy_revision does not match current policy"
                )
            semantic = {
                "user_id": selected_user,
                "goal_id": selected_goal,
                "goal_revision": selected_goal_revision,
                "goal_state_revision": selected_state_revision,
                "scope": scope,
                "attention_group_id": selected_group,
                "goal_priority": selected_priority,
                "deadline_at": selected_deadline.isoformat(),
                "timezone": selected_timezone.key,
                "notifications_paused": notifications_paused,
                "quiet_hours": selected_quiet_hours,
                "daily_notification_budget": daily_notification_budget,
            }
            policy_revision = (
                "pgatp_" + self._digest(semantic)[:20]
            )
            policy = {
                "schema_version": self.POLICY_SCHEMA,
                **semantic,
                "policy_revision": policy_revision,
                "created_at": str(
                    (existing or {}).get("created_at") or now.isoformat()
                ),
                "updated_at": now.isoformat(),
                "agent_invoked": False,
                "notification_allowed": False,
                "execution_allowed": False,
                "interrupt_eligible": False,
            }
            policies[selected_goal] = policy
            if len(policies) > self.MAX_POLICIES:
                raise ProjectGuardianAttentionConflict(
                    "Project Guardian Attention policy capacity exhausted"
                )

            def update(state: dict[str, Any]) -> None:
                self._require_healthy(state, self.STATE_FILE)
                state["schema_version"] = self.SCHEMA_VERSION
                state["policies"] = copy.deepcopy(policies)
                state["policy_count"] = len(policies)
                state.setdefault("dismissals", {})
                state.setdefault("assessments", {})
                state.setdefault("general_situations", {})
                state.setdefault("runs", [])
                state["updated_at"] = utc_now_iso()

            self.state_store.mutate_json(self.STATE_FILE, update)
        return copy.deepcopy(policy)

    def set_dismissal(
        self,
        *,
        user_id: str,
        suppression_key: str,
        dismissed: bool,
    ) -> dict[str, Any]:
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_key = self._required_text(
            suppression_key,
            "suppression_key",
            120,
        )
        if not selected_key.startswith("pgas_"):
            raise ValueError("suppression_key is invalid")
        if not isinstance(dismissed, bool):
            raise ValueError("dismissed must be boolean")
        now = self._aware_now()
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, self.STATE_FILE)
            assessments = self._dict_records(state.get("assessments"))
            matches = [
                item
                for item in assessments.values()
                if str(item.get("user_id") or "") == selected_user
                and str(
                    (item.get("suppression") or {}).get("key") or ""
                )
                == selected_key
            ]
            if len(matches) != 1:
                raise KeyError(
                    "Attention assessment not found in requested user scope"
                )
            dismissals = self._dict_records(state.get("dismissals"))
            dismissal = {
                "user_id": selected_user,
                "suppression_key": selected_key,
                "active": dismissed,
                "updated_at": now.isoformat(),
            }
            dismissals[selected_key] = dismissal
            if len(dismissals) > self.MAX_ASSESSMENTS:
                referenced = {
                    str(
                        (item.get("suppression") or {}).get("key") or ""
                    )
                    for item in assessments.values()
                }
                removable = sorted(
                    (
                        key,
                        item,
                    )
                    for key, item in dismissals.items()
                    if key not in referenced and not item.get("active")
                )
                while len(dismissals) > self.MAX_ASSESSMENTS and removable:
                    key, _ = removable.pop(0)
                    dismissals.pop(key, None)
            if len(dismissals) > self.MAX_ASSESSMENTS:
                raise ProjectGuardianAttentionConflict(
                    "Attention dismissal capacity exhausted"
                )
            state["schema_version"] = self.SCHEMA_VERSION
            state["dismissals"] = dismissals
            state["updated_at"] = utc_now_iso()
            result.update(copy.deepcopy(dismissal))

        self.state_store.mutate_json(self.STATE_FILE, update)
        return {"status": "updated", **result}

    def run_once(self, *, reason: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            snapshot = self._mode_snapshot()
            return {
                "status": "busy",
                "mode": snapshot["mode"],
                "mode_epoch": snapshot["mode_epoch"],
            }
        try:
            return self._run_once_locked(reason=reason)
        finally:
            self._run_lock.release()

    def _run_once_locked(self, *, reason: str) -> dict[str, Any]:
        mode_snapshot = self._mode_snapshot()
        mode = mode_snapshot["mode"]
        if mode == "disabled":
            return {
                "status": "disabled",
                "mode": mode,
                "mode_epoch": mode_snapshot["mode_epoch"],
                "assessment_count": 0,
                "general_situation_count": 0,
            }
        now = self._aware_now()
        goals_state = self.state_store.read_json("user_goals.json")
        guardian_state = self.state_store.read_json(
            "project_guardian_state.json"
        )
        attention_state = self.state_store.read_json(self.STATE_FILE)
        for state, name in (
            (goals_state, "user_goals.json"),
            (guardian_state, "project_guardian_state.json"),
            (attention_state, self.STATE_FILE),
        ):
            if state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "mode": mode,
                    "mode_epoch": mode_snapshot["mode_epoch"],
                    "reason": f"{name}_corrupt",
                    "state_frozen": True,
                    "assessment_count": 0,
                    "general_situation_count": 0,
                }

        input_revisions = {
            "ops_config.json": mode_snapshot["state_revision"],
            "user_goals.json": self._store_revision(goals_state),
            "project_guardian_state.json": self._store_revision(
                guardian_state
            ),
            self.STATE_FILE: self._store_revision(attention_state),
        }
        if any(revision < 1 for revision in input_revisions.values()):
            return {
                "status": "degraded",
                "mode": mode,
                "mode_epoch": mode_snapshot["mode_epoch"],
                "reason": "attention_input_state_revision_invalid",
                "state_frozen": True,
                "assessment_count": 0,
                "general_situation_count": 0,
            }

        policies = self._dict_records(attention_state.get("policies"))
        dismissals = self._dict_records(
            attention_state.get("dismissals")
        )
        prior_assessments = self._dict_records(
            attention_state.get("assessments")
        )
        raw_candidates = (
            guardian_state.get("candidates")
            if isinstance(guardian_state.get("candidates"), list)
            else []
        )
        candidates = [
            copy.deepcopy(item)
            for item in raw_candidates
            if isinstance(item, dict)
            and str(item.get("disposition") or "") != "inactive"
        ]
        assessments: dict[str, dict[str, Any]] = {}
        policy_groups: dict[str, str] = {}
        rejected_policy_count = 0
        for goal_id, policy in sorted(policies.items()):
            goal = self._active_goal(
                goals_state,
                user_id=str(policy.get("user_id") or ""),
                goal_id=str(policy.get("goal_id") or ""),
                goal_revision=str(policy.get("goal_revision") or ""),
                expected_state_revision=self._nonnegative_int(
                    policy.get("goal_state_revision")
                ),
                now=now,
            )
            if (
                goal is None
                or str(goal_id) != str(policy.get("goal_id") or "")
                or not self._policy_matches_goal(policy, goal)
            ):
                rejected_policy_count += 1
                continue
            matching_candidates = [
                item
                for item in candidates
                if self._candidate_matches_policy(item, policy)
            ]
            for candidate in matching_candidates:
                candidate_id = str(candidate.get("candidate_id") or "")
                suppression_key = (
                    self.scheduler.suppression_key_for(candidate)
                )
                dismissal = dismissals.get(suppression_key)
                prior = prior_assessments.get(candidate_id)
                context = self._policy_context(
                    policy,
                    suppression_key=suppression_key,
                    dismissal=dismissal,
                    prior_assessment=prior,
                    now=now,
                )
                evaluation = self.scheduler.evaluate(
                    candidates=[candidate],
                    policy_context=context,
                    now=now,
                )
                rows = (
                    evaluation.get("assessments")
                    if isinstance(evaluation.get("assessments"), list)
                    else []
                )
                if len(rows) != 1 or not isinstance(rows[0], dict):
                    continue
                assessment = copy.deepcopy(rows[0])
                assessment.update(
                    runtime_mode=mode,
                    attention_group_id=str(
                        policy.get("attention_group_id") or ""
                    ),
                    recorded_at=now.isoformat(),
                )
                assessments[candidate_id] = assessment
                policy_groups[candidate_id] = str(
                    policy.get("attention_group_id") or ""
                )

        if len(assessments) > self.MAX_ASSESSMENTS:
            return {
                "status": "degraded",
                "mode": mode,
                "mode_epoch": mode_snapshot["mode_epoch"],
                "reason": "attention_assessment_capacity_exhausted",
                "state_frozen": True,
                "assessment_count": 0,
                "general_situation_count": 0,
            }
        general_situations = self._aggregate(
            assessments,
            policy_groups=policy_groups,
            now=now,
        )
        result = {
            "status": "success",
            "mode": mode,
            "mode_epoch": mode_snapshot["mode_epoch"],
            "reason": reason,
            "policy_count": len(policies),
            "rejected_policy_count": rejected_policy_count,
            "candidate_count": len(candidates),
            "assessment_count": len(assessments),
            "general_situation_count": len(general_situations),
            "agent_invoked": False,
            "notification_count": 0,
            "execution_count": 0,
            "checked_at": now.isoformat(),
        }
        persist_status = self._persist_run(
            result,
            assessments=assessments,
            general_situations=general_situations,
            expected_mode_snapshot=mode_snapshot,
            expected_input_revisions=input_revisions,
        )
        if persist_status != "persisted":
            current_mode = self._mode_snapshot()
            return {
                "status": (
                    "disabled"
                    if current_mode["mode"] == "disabled"
                    else "stale"
                ),
                "mode": current_mode["mode"],
                "mode_epoch": current_mode["mode_epoch"],
                "reason": (
                    "attention_mode_changed_during_run"
                    if persist_status == "mode_changed"
                    else "attention_input_changed_during_run"
                ),
                "state_frozen": True,
                "assessment_count": 0,
                "general_situation_count": 0,
            }
        return result

    def status(self) -> dict[str, Any]:
        mode_snapshot = self._mode_snapshot()
        mode = mode_snapshot["mode"]
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "mode": mode,
                "mode_epoch": mode_snapshot["mode_epoch"],
                "reason": "project_guardian_attention_state_corrupt",
                "state_frozen": True,
            }
        policies = self._dict_records(state.get("policies"))
        assessments = self._dict_records(state.get("assessments"))
        situations = self._dict_records(state.get("general_situations"))
        return {
            "status": "success",
            "mode": mode,
            "mode_epoch": mode_snapshot["mode_epoch"],
            "policy_count": len(policies),
            "assessment_count": len(assessments),
            "general_situation_count": len(situations),
            "last_run": self._public_run(state.get("last_run")),
            "contracts": {
                "deterministic": True,
                "read_only": True,
                "shadow_only": True,
                "agent_invoked": False,
                "notifications": False,
                "execution": False,
                "event_published": False,
                "project_mutation": False,
                "budget_consumed": False,
                "cooldown_started": False,
            },
        }

    def list_assessments(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_user = self._required_text(user_id, "user_id", 240)
        state = self.state_store.read_json(self.STATE_FILE)
        self._require_healthy(state, self.STATE_FILE)
        rows = [
            copy.deepcopy(item)
            for item in self._dict_records(
                state.get("assessments")
            ).values()
            if str(item.get("user_id") or "") == selected_user
        ]
        rows.sort(
            key=lambda item: (
                str(item.get("recorded_at") or ""),
                str(item.get("assessment_id") or ""),
            ),
            reverse=True,
        )
        return rows[: max(0, min(int(limit), 500))]

    def list_general_situations(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_user = self._required_text(user_id, "user_id", 240)
        state = self.state_store.read_json(self.STATE_FILE)
        self._require_healthy(state, self.STATE_FILE)
        rows = [
            copy.deepcopy(item)
            for item in self._dict_records(
                state.get("general_situations")
            ).values()
            if str(item.get("user_id") or "") == selected_user
        ]
        rows.sort(
            key=lambda item: (
                str(item.get("evaluated_at") or ""),
                str(item.get("situation_id") or ""),
            ),
            reverse=True,
        )
        return rows[: max(0, min(int(limit), 500))]

    def _policy_context(
        self,
        policy: dict[str, Any],
        *,
        suppression_key: str,
        dismissal: dict[str, Any] | None,
        prior_assessment: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        user_zone = self._timezone(str(policy.get("timezone") or ""))
        local_date = now.astimezone(user_zone).date().isoformat()
        prior_candidate_revision = (
            str(
                (prior_assessment.get("candidate_ref") or {}).get(
                    "candidate_revision"
                )
                or ""
            )
            if isinstance(prior_assessment, dict)
            else ""
        )
        return {
            "schema_version": self.scheduler.CONTEXT_SCHEMA_VERSION,
            "user_id": str(policy["user_id"]),
            "goal_id": str(policy["goal_id"]),
            "goal_revision": str(policy["goal_revision"]),
            "scope": copy.deepcopy(policy["scope"]),
            "goal_priority": float(policy["goal_priority"]),
            "deadline_at": str(policy["deadline_at"]),
            "timezone": user_zone.key,
            "notifications_paused": bool(
                policy["notifications_paused"]
            ),
            "quiet_hours": copy.deepcopy(policy["quiet_hours"]),
            "notification_budget": {
                "local_date": local_date,
                "limit": int(policy["daily_notification_budget"]),
                # No notification path exists in this Phase 2 slice.
                "used": 0,
            },
            "dismissal": {
                "active": bool(
                    isinstance(dismissal, dict)
                    and dismissal.get("active") is True
                    and str(dismissal.get("user_id") or "")
                    == str(policy["user_id"])
                ),
                **(
                    {"suppression_key": suppression_key}
                    if isinstance(dismissal, dict)
                    and dismissal.get("active") is True
                    else {}
                ),
            },
            "cooldown": {"until": None},
            "novelty": {
                "last_seen_candidate_revision": (
                    prior_candidate_revision or None
                )
            },
            # Investigate remains counterfactual until a separately governed
            # read-only probe/Agent integration is implemented and validated.
            "capabilities": {"read_only_investigation": False},
        }

    def _aggregate(
        self,
        assessments: dict[str, dict[str, Any]],
        *,
        policy_groups: dict[str, str],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for candidate_id, assessment in assessments.items():
            user_id = str(assessment.get("user_id") or "")
            group_id = str(policy_groups.get(candidate_id) or "")
            if user_id and group_id:
                grouped.setdefault((user_id, group_id), []).append(
                    assessment
                )
        situations: dict[str, dict[str, Any]] = {}
        rank = {
            "suppressed": 0,
            "observe": 1,
            "investigate_read_only": 2,
            "would_suggest": 3,
        }
        for (user_id, group_id), rows in sorted(grouped.items()):
            distinct = {
                str((row.get("candidate_ref") or {}).get("candidate_id") or "")
                for row in rows
            }
            distinct.discard("")
            if len(distinct) < 2:
                continue
            refs = sorted(
                (
                    copy.deepcopy(row["candidate_ref"])
                    for row in rows
                    if isinstance(row.get("candidate_ref"), dict)
                ),
                key=lambda item: (
                    str(item.get("candidate_id") or ""),
                    str(item.get("candidate_revision") or ""),
                ),
            )
            dispositions = [
                str(row.get("would_disposition") or "suppressed")
                for row in rows
            ]
            would_disposition = max(
                dispositions,
                key=lambda value: rank.get(value, -1),
            )
            known_scores = [
                float(row["score"])
                for row in rows
                if isinstance(row.get("score"), (int, float))
                and not isinstance(row.get("score"), bool)
                and math.isfinite(float(row["score"]))
            ]
            blockers = sorted(
                {
                    str(blocker)
                    for row in rows
                    for blocker in (
                        row.get("blockers")
                        if isinstance(row.get("blockers"), list)
                        else []
                    )
                    if str(blocker)
                }
            )
            situation_id = "pgags_" + self._digest(
                {
                    "user_id": user_id,
                    "attention_group_id": group_id,
                }
            )[:20]
            semantic = {
                "schema_version": self.SITUATION_SCHEMA,
                "situation_id": situation_id,
                "situation_kind": "project_guardian_attention_group",
                "user_id": user_id,
                "attention_group_id": group_id,
                "candidate_refs": refs,
                "score": max(known_scores) if known_scores else None,
                "would_disposition": would_disposition,
                "blockers": blockers,
                "analysis_mode": "deterministic_read_only_shadow",
                "agent_invoked": False,
                "shadow_only": True,
                "notification_allowed": False,
                "execution_allowed": False,
                "interrupt_eligible": False,
            }
            situation = {
                **semantic,
                "situation_revision": (
                    "pgagsr_" + self._digest(semantic)[:20]
                ),
                "evaluated_at": now.isoformat(),
            }
            situations[situation_id] = situation
        if len(situations) > self.MAX_GENERAL_SITUATIONS:
            raise RuntimeError(
                "Project Guardian general Attention situation capacity "
                "exhausted"
            )
        return situations

    def _persist_run(
        self,
        result: dict[str, Any],
        *,
        assessments: dict[str, dict[str, Any]],
        general_situations: dict[str, dict[str, Any]],
        expected_mode_snapshot: dict[str, Any],
        expected_input_revisions: dict[str, int],
    ) -> str:
        compact = self._public_run(result)
        compact["recorded_at"] = utc_now_iso()

        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, self.STATE_FILE)
            runs = (
                state.get("runs")
                if isinstance(state.get("runs"), list)
                else []
            )
            runs.append(compact)
            state["schema_version"] = self.SCHEMA_VERSION
            state.setdefault("policies", {})
            state.setdefault("dismissals", {})
            state["assessments"] = copy.deepcopy(assessments)
            state["assessment_count"] = len(assessments)
            state["general_situations"] = copy.deepcopy(
                general_situations
            )
            state["general_situation_count"] = len(general_situations)
            state["runs"] = runs[-self.MAX_RUNS :]
            state["last_run"] = compact
            state["updated_at"] = utc_now_iso()

        with self.state_store.writer_transaction():
            if self._mode_snapshot() != expected_mode_snapshot:
                return "mode_changed"
            current_input_revisions = {
                name: self._store_revision(
                    self.state_store.read_json(name)
                )
                for name in expected_input_revisions
            }
            if current_input_revisions != expected_input_revisions:
                return "input_changed"
            self.state_store.mutate_json(self.STATE_FILE, update)
        return "persisted"

    def _mode(self) -> str:
        return self._mode_snapshot()["mode"]

    def _mode_snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        if config.get("_state_corrupt") is True:
            return {
                "mode": "disabled",
                "mode_epoch": 0,
                "state_revision": self._store_revision(config),
            }
        section = (
            config.get("project_guardian")
            if isinstance(config.get("project_guardian"), dict)
            else {}
        )
        mode = str(section.get("mode") or "disabled").strip().lower()
        raw_epoch = section.get("mode_epoch")
        mode_epoch = (
            raw_epoch
            if isinstance(raw_epoch, int)
            and not isinstance(raw_epoch, bool)
            and raw_epoch >= 0
            else 0
        )
        return {
            "mode": mode if mode in self.MODES else "disabled",
            "mode_epoch": mode_epoch,
            "state_revision": self._store_revision(config),
        }

    @classmethod
    def _active_goal(
        cls,
        state: dict[str, Any],
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        expected_state_revision: int,
        now: datetime,
    ) -> dict[str, Any] | None:
        goals = (
            state.get("goals")
            if isinstance(state.get("goals"), list)
            else []
        )
        matches = [
            item
            for item in goals
            if isinstance(item, dict)
            and str(item.get("goal_id") or "") == goal_id
            and str(item.get("kind") or "")
            == ProjectGuardianEvaluator.GOAL_KIND
        ]
        if len(matches) != 1:
            return None
        goal = matches[0]
        scope = (
            goal.get("scope")
            if isinstance(goal.get("scope"), dict)
            else {}
        )
        active_from = cls._time(goal.get("active_from"))
        active_until = cls._time(goal.get("active_until"))
        if (
            str(goal.get("schema_version") or "")
            != ProjectGuardianEvaluator.GOAL_SCHEMA
            or str(goal.get("source") or "")
            != ProjectGuardianEvaluator.GOAL_SOURCE
            or str(goal.get("status") or "") != "active"
            or str(goal.get("user_id") or "") != user_id
            or str(goal.get("revision") or "") != goal_revision
            or cls._nonnegative_int(goal.get("state_revision"))
            != expected_state_revision
            or active_from is None
            or active_until is None
            or not (active_from <= now <= active_until)
            or any(
                not str(scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
        ):
            return None
        return copy.deepcopy(goal)

    @classmethod
    def _policy_matches_goal(
        cls,
        policy: dict[str, Any],
        goal: dict[str, Any],
    ) -> bool:
        goal_scope = (
            goal.get("scope")
            if isinstance(goal.get("scope"), dict)
            else {}
        )
        policy_scope = (
            policy.get("scope")
            if isinstance(policy.get("scope"), dict)
            else {}
        )
        try:
            group_id = cls._required_text(
                policy.get("attention_group_id"),
                "attention_group_id",
                120,
            )
            if any(
                character
                not in (
                    "abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "0123456789._:-"
                )
                for character in group_id
            ):
                return False
            priority = cls._unit_number(
                policy.get("goal_priority"),
                "goal_priority",
            )
            deadline = cls._time(policy.get("deadline_at"))
            if deadline is None:
                return False
            selected_timezone = cls._timezone(policy.get("timezone"))
            paused = policy.get("notifications_paused")
            if not isinstance(paused, bool):
                return False
            quiet_hours = cls._quiet_hours(policy.get("quiet_hours"))
            budget = policy.get("daily_notification_budget")
            if (
                isinstance(budget, bool)
                or not isinstance(budget, int)
                or not 0 <= budget <= 100
            ):
                return False
            goal_state_revision = policy.get("goal_state_revision")
            if (
                isinstance(goal_state_revision, bool)
                or not isinstance(goal_state_revision, int)
                or goal_state_revision < 1
            ):
                return False
        except (TypeError, ValueError):
            return False

        semantic = {
            "user_id": str(policy.get("user_id") or ""),
            "goal_id": str(policy.get("goal_id") or ""),
            "goal_revision": str(policy.get("goal_revision") or ""),
            "goal_state_revision": goal_state_revision,
            "scope": {
                key: str(policy_scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            },
            "attention_group_id": group_id,
            "goal_priority": priority,
            "deadline_at": deadline.isoformat(),
            "timezone": selected_timezone.key,
            "notifications_paused": paused,
            "quiet_hours": quiet_hours,
            "daily_notification_budget": budget,
        }
        return (
            str(policy.get("schema_version") or "") == cls.POLICY_SCHEMA
            and semantic["user_id"] == str(goal.get("user_id") or "")
            and semantic["goal_id"] == str(goal.get("goal_id") or "")
            and semantic["goal_revision"]
            == str(goal.get("revision") or "")
            and semantic["goal_state_revision"]
            == cls._nonnegative_int(goal.get("state_revision"))
            and all(
                semantic["scope"][key] == str(goal_scope.get(key) or "")
                and isinstance(policy_scope.get(key), str)
                and bool(policy_scope.get(key))
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
            and str(policy.get("policy_revision") or "")
            == "pgatp_" + cls._digest(semantic)[:20]
            and policy.get("agent_invoked") is False
            and policy.get("notification_allowed") is False
            and policy.get("execution_allowed") is False
            and policy.get("interrupt_eligible") is False
        )

    @staticmethod
    def _candidate_matches_policy(
        candidate: dict[str, Any],
        policy: dict[str, Any],
    ) -> bool:
        candidate_scope = (
            candidate.get("scope")
            if isinstance(candidate.get("scope"), dict)
            else {}
        )
        policy_scope = (
            policy.get("scope")
            if isinstance(policy.get("scope"), dict)
            else {}
        )
        return (
            str(candidate.get("user_id") or "")
            == str(policy.get("user_id") or "")
            and str(candidate.get("goal_id") or "")
            == str(policy.get("goal_id") or "")
            and str(candidate.get("goal_revision") or "")
            == str(policy.get("goal_revision") or "")
            and all(
                str(candidate_scope.get(key) or "")
                == str(policy_scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
        )

    @staticmethod
    def _public_run(value: Any) -> dict[str, Any]:
        selected = value if isinstance(value, dict) else {}
        allowed = (
            "status",
            "mode",
            "mode_epoch",
            "reason",
            "policy_count",
            "rejected_policy_count",
            "candidate_count",
            "assessment_count",
            "general_situation_count",
            "agent_invoked",
            "notification_count",
            "execution_count",
            "checked_at",
            "state_frozen",
        )
        return {
            key: copy.deepcopy(selected[key])
            for key in allowed
            if key in selected
        }

    def _aware_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(
                "Project Guardian Attention clock must be timezone-aware"
            )
        return value.astimezone(timezone.utc).replace(microsecond=0)

    @staticmethod
    def _quiet_hours(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("quiet_hours must be an object")
        if set(value) - {"enabled", "start", "end"}:
            raise ValueError("quiet_hours contains unsupported fields")
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("quiet_hours.enabled must be boolean")
        if not enabled:
            return {"enabled": False}
        start = ProjectGuardianAttentionRuntime._clock_time(
            value.get("start")
        )
        end = ProjectGuardianAttentionRuntime._clock_time(value.get("end"))
        if start is None or end is None or start == end:
            raise ValueError(
                "enabled quiet_hours require distinct HH:MM start/end"
            )
        return {
            "enabled": True,
            "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"),
        }

    @staticmethod
    def _clock_time(value: Any) -> time | None:
        if not isinstance(value, str):
            return None
        try:
            selected = time.fromisoformat(value.strip())
        except ValueError:
            return None
        if (
            selected.tzinfo is not None
            or selected.second
            or selected.microsecond
        ):
            return None
        return selected

    @staticmethod
    def _timezone(value: Any) -> ZoneInfo:
        name = str(value or "").strip()
        try:
            selected = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone is missing or invalid") from exc
        return selected

    @staticmethod
    def _required_text(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        text = value.strip()
        if not text or len(text) > limit or "\x00" in text:
            raise ValueError(
                f"{field} is required and must be <= {limit} chars"
            )
        return text

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field} must be a positive integer")
        if value < 1:
            raise ValueError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            selected = int(value)
        except (TypeError, ValueError):
            return 0
        return selected if selected >= 0 else 0

    @staticmethod
    def _store_revision(state: Any) -> int:
        if not isinstance(state, dict):
            return -1
        value = state.get("_state_revision")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            return -1
        return value

    @staticmethod
    def _unit_number(value: Any, field: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            raise ValueError(f"{field} must be between 0 and 1")
        selected = float(value)
        if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
            raise ValueError(f"{field} must be between 0 and 1")
        return round(selected, 6)

    @staticmethod
    def _time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            text_value = str(value or "").strip()
            if not text_value:
                return None
            try:
                parsed = datetime.fromisoformat(
                    text_value.replace("Z", "+00:00")
                )
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    @staticmethod
    def _dict_records(value: Any) -> dict[str, dict[str, Any]]:
        return {
            str(key): copy.deepcopy(item)
            for key, item in (
                value.items() if isinstance(value, dict) else []
            )
            if isinstance(item, dict)
        }

    @staticmethod
    def _require_healthy(state: dict[str, Any], name: str) -> None:
        if state.get("_state_corrupt") is True:
            raise RuntimeError(f"corrupt state: {name}")

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
