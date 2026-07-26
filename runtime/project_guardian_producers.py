from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from runtime.project_guardian_github_ci import (
    GitHubActionsCIProvider,
    GitHubCIContractError,
    GitHubCIUnknown,
)


class ProjectGuardianGoalConflict(ValueError):
    """A release Goal update failed its compare-and-swap contract."""


class ProjectGuardianProducerRuntime:
    """Trusted, read-only signal producers for the Project Guardian.

    Local Git and an optional GitHub Actions poller provide factual signals.
    Deployment intent is accepted only as an explicit structured local-control
    transition; it is never inferred by a scheduled tick, model, or text rule.
    """

    STATE_FILE = "project_guardian_producer_state.json"
    SCHEMA_VERSION = "veyra.project_guardian_producers.v1"
    GOAL_SOURCE = ProjectGuardianEvaluator.GOAL_SOURCE
    MAX_BINDINGS = 128
    MAX_CI_BINDINGS = 8
    MAX_INTENT_OPERATIONS = 512
    MAX_RELEASE_GOALS = 64
    MAX_RUNS = 100
    ALLOWED_GOAL_STATUSES = {"active", "paused", "completed"}

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        publish_git_observation: Callable[..., dict[str, Any]],
        publish_ci_observation: Callable[..., dict[str, Any]] | None = None,
        publish_deployment_intent: (
            Callable[..., dict[str, Any]] | None
        ) = None,
        ci_provider: GitHubActionsCIProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        command_timeout_seconds: float = 4.0,
    ) -> None:
        self.state_store = state_store
        self.publish_git_observation = publish_git_observation
        self.publish_ci_observation = publish_ci_observation
        self.publish_deployment_intent = publish_deployment_intent
        self.ci_provider = ci_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.command_timeout_seconds = max(
            0.5,
            min(float(command_timeout_seconds), 15.0),
        )
        self._run_lock = threading.Lock()

    def register_release_goal(
        self,
        *,
        user_id: str,
        workspace_id: str,
        repo_id: str,
        target_ref: str,
        target_environment: str,
        release_cycle: str,
        workspace_path: str,
        active_from: str | None = None,
        active_until: str | None = None,
        goal_id: str | None = None,
        expected_state_revision: int | None = None,
        github_actions_workflow: str | None = None,
        github_actions_required_jobs: list[str] | None = None,
        github_actions_app_id: int | None = None,
    ) -> dict[str, Any]:
        now = self._aware_now()
        if (
            expected_state_revision is not None
            and (
                not isinstance(expected_state_revision, int)
                or isinstance(expected_state_revision, bool)
                or expected_state_revision < 1
            )
        ):
            raise ValueError(
                "expected_state_revision must be a positive integer"
            )
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_goal_id = (
            self._required_text(goal_id, "goal_id", 240)
            if goal_id
            else f"goal_release_{uuid4().hex[:16]}"
        )
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
            240,
        )
        selected_repo = self._normalize_repo_id(repo_id)
        if not selected_repo:
            raise ValueError("repo_id must use owner/repository form")
        selected_ref = self._required_text(target_ref, "target_ref", 240)
        if not selected_ref.startswith("refs/heads/"):
            raise ValueError("target_ref must be a full refs/heads/* ref")
        selected_environment = self._required_text(
            target_environment,
            "target_environment",
            240,
        )
        selected_cycle = self._required_text(
            release_cycle,
            "release_cycle",
            240,
        )
        starts_at = self._time(active_from) if active_from else now
        ends_at = (
            self._time(active_until)
            if active_until
            else starts_at + timedelta(days=7)
        )
        if starts_at is None or ends_at is None or ends_at < starts_at:
            raise ValueError(
                "active_from/active_until must be timezone-aware and ordered"
            )

        repository = self._inspect_repository(
            workspace_path,
            expected_repo_id=selected_repo,
            expected_ref=selected_ref,
        )
        scope = {
            "workspace_id": selected_workspace,
            "repo_id": selected_repo,
            "target_ref": selected_ref,
            "target_environment": selected_environment,
            "release_cycle": selected_cycle,
        }
        goals_state = self.state_store.read_json("user_goals.json")
        self._require_healthy(goals_state, "user_goals.json")
        existing = self._goal_by_id(goals_state, selected_goal_id)
        producer_state = self.state_store.read_json(self.STATE_FILE)
        self._require_healthy(producer_state, self.STATE_FILE)
        existing_binding = self._binding_for(
            selected_goal_id,
            str((existing or {}).get("revision") or ""),
            state=producer_state,
        )
        current_state_revision = 0
        if existing is not None:
            if (
                str(existing.get("schema_version") or "")
                != ProjectGuardianEvaluator.GOAL_SCHEMA
                or str(existing.get("source") or "") != self.GOAL_SOURCE
            ):
                raise ProjectGuardianGoalConflict(
                    "goal_id is not a controlled Project Guardian release Goal"
                )
            if str(existing.get("user_id") or "") != selected_user:
                raise ProjectGuardianGoalConflict(
                    "goal_id already belongs to another user"
                )
            current_state_revision = self._state_revision(existing)
            if current_state_revision < 1:
                raise ProjectGuardianGoalConflict(
                    "existing release Goal lacks a controlled state_revision"
                )
            if expected_state_revision != current_state_revision:
                raise ProjectGuardianGoalConflict(
                    "expected_state_revision does not match the current "
                    "release Goal"
                )
        elif expected_state_revision is not None:
            raise ProjectGuardianGoalConflict(
                "expected_state_revision was provided for a new release Goal"
            )
        next_state_revision = current_state_revision + 1
        ci_inputs = (
            github_actions_workflow,
            github_actions_required_jobs,
            github_actions_app_id,
        )
        ci_configured = any(value is not None for value in ci_inputs)
        if ci_configured and not all(value is not None for value in ci_inputs):
            raise ValueError(
                "GitHub Actions workflow, required jobs, and app id must "
                "be configured together"
            )
        retains_ci_policy = (
            isinstance(existing_binding, dict)
            and isinstance(existing_binding.get("github_actions"), dict)
        )
        if (
            ci_configured or retains_ci_policy
        ) and repository.get("github_repo_id") != selected_repo:
            raise ValueError(
                "GitHub Actions requires origin to be the exact matching "
                "github.com repository"
            )
        ci_policy: dict[str, Any] | None = None
        if ci_configured:
            if self.ci_provider is None or self.publish_ci_observation is None:
                raise ValueError("GitHub Actions CI producer is unavailable")
            if not isinstance(github_actions_required_jobs, list):
                raise ValueError(
                    "GitHub Actions required jobs must be a list"
                )
            if (
                isinstance(github_actions_app_id, bool)
                or not isinstance(github_actions_app_id, int)
                or github_actions_app_id < 1
            ):
                raise ValueError(
                    "GitHub Actions app id must be a positive integer"
                )
            other_ci_bindings = self._ci_binding_reservation_count(
                producer_state=producer_state,
                goals_state=goals_state,
                excluding_goal_id=selected_goal_id,
                reference_time=now,
            )
            if (
                ends_at >= now
                and other_ci_bindings >= self.MAX_CI_BINDINGS
            ):
                raise ProjectGuardianGoalConflict(
                    "Project Guardian GitHub CI binding capacity exhausted"
                )
            try:
                ci_policy = self.ci_provider.bind_policy(
                    repo_id=selected_repo,
                    workflow_path=str(github_actions_workflow or ""),
                    required_jobs=list(github_actions_required_jobs or []),
                    expected_app_id=github_actions_app_id,
                )
            except GitHubCIContractError as exc:
                raise ValueError(str(exc)) from exc
        elif isinstance(existing_binding, dict) and isinstance(
            existing_binding.get("github_actions"),
            dict,
        ):
            ci_policy = copy.deepcopy(existing_binding["github_actions"])
        ci_policy_digest = str((ci_policy or {}).get("policy_digest") or "")

        revision = self._goal_revision(
            goal_id=selected_goal_id,
            user_id=selected_user,
            scope=scope,
            target_sha=repository["target_sha"],
            canonical_path=repository["canonical_path"],
            remote_origin_digest=repository["remote_origin_digest"],
            active_from=starts_at.isoformat(),
            active_until=ends_at.isoformat(),
            ci_policy_digest=ci_policy_digest,
        )
        binding = {
            "binding_id": self._binding_id(
                goal_id=selected_goal_id,
                goal_revision=revision,
                canonical_path=repository["canonical_path"],
                repo_id=selected_repo,
                target_ref=selected_ref,
                target_sha=repository["target_sha"],
                remote_origin_digest=repository["remote_origin_digest"],
                ci_policy_digest=ci_policy_digest,
            ),
            "goal_id": selected_goal_id,
            "user_id": selected_user,
            "goal_revision": revision,
            "workspace_id": selected_workspace,
            "repo_id": selected_repo,
            "target_ref": selected_ref,
            "target_sha": repository["target_sha"],
            "remote_origin_digest": repository["remote_origin_digest"],
            "canonical_path": repository["canonical_path"],
            "registered_at": now.isoformat(),
        }
        if ci_policy is not None:
            binding["github_actions"] = copy.deepcopy(ci_policy)
        goal = {
            "schema_version": ProjectGuardianEvaluator.GOAL_SCHEMA,
            "goal_id": selected_goal_id,
            "kind": ProjectGuardianEvaluator.GOAL_KIND,
            "status": "active",
            "user_id": selected_user,
            "revision": revision,
            "state_revision": next_state_revision,
            "scope": scope,
            "target_sha": repository["target_sha"],
            "active_from": starts_at.isoformat(),
            "active_until": ends_at.isoformat(),
            "source": self.GOAL_SOURCE,
            "created_at": str(
                (existing or {}).get("created_at") or now.isoformat()
            ),
            "updated_at": now.isoformat(),
        }

        # Persist the private workspace binding first. A crash can leave an
        # unused binding, but never an active Goal whose producer lacks a
        # binding and might incorrectly emit a clean tombstone.
        with self.state_store.writer_transaction():
            current_goals = self.state_store.read_json("user_goals.json")
            self._require_healthy(current_goals, "user_goals.json")
            current_existing = self._goal_by_id(
                current_goals,
                selected_goal_id,
            )
            if existing is None:
                if current_existing is not None:
                    raise ProjectGuardianGoalConflict(
                        "release Goal was created concurrently"
                    )
            elif (
                current_existing is None
                or self._state_revision(current_existing)
                != current_state_revision
            ):
                raise ProjectGuardianGoalConflict(
                    "release Goal changed during registration"
                )
            current_producer_state = self.state_store.read_json(
                self.STATE_FILE
            )
            self._require_healthy(
                current_producer_state,
                self.STATE_FILE,
            )
            if (
                ci_policy is not None
                and ends_at >= now
                and self._ci_binding_reservation_count(
                    producer_state=current_producer_state,
                    goals_state=current_goals,
                    excluding_goal_id=selected_goal_id,
                    reference_time=now,
                )
                >= self.MAX_CI_BINDINGS
            ):
                raise ProjectGuardianGoalConflict(
                    "Project Guardian GitHub CI binding capacity exhausted"
                )
            self._store_binding(binding)
            self._store_goal(goal, existing=current_existing)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "project_guardian_release_goal",
                "status": "registered",
                "artifacts": {
                    "goal_id": selected_goal_id,
                    "goal_revision": revision,
                    "goal_state_revision": next_state_revision,
                    "workspace_id": selected_workspace,
                    "repo_id": selected_repo,
                    "target_ref": selected_ref,
                    "target_environment": selected_environment,
                    "release_cycle": selected_cycle,
                    "github_actions_configured": ci_policy is not None,
                    "project_read_only": True,
                },
            },
        )
        return copy.deepcopy(goal)

    def set_release_goal_status(
        self,
        *,
        user_id: str,
        goal_id: str,
        expected_state_revision: int,
        status: str,
    ) -> dict[str, Any]:
        selected_status = str(status or "").strip().lower()
        if selected_status not in self.ALLOWED_GOAL_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(self.ALLOWED_GOAL_STATUSES)}"
        )
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_goal = self._required_text(goal_id, "goal_id", 240)
        if (
            not isinstance(expected_state_revision, int)
            or isinstance(expected_state_revision, bool)
        ):
            raise ValueError(
                "expected_state_revision must be a positive integer"
            )
        selected_state_revision = expected_state_revision
        if selected_state_revision < 1:
            raise ValueError(
                "expected_state_revision must be a positive integer"
            )
        updated: dict[str, Any] | None = None
        transition_time = self._aware_now()

        def mutate(state: dict[str, Any]) -> None:
            nonlocal updated
            self._require_healthy(state, "user_goals.json")
            goals = (
                state.get("goals")
                if isinstance(state.get("goals"), list)
                else []
            )
            for index, raw in enumerate(goals):
                if not isinstance(raw, dict):
                    continue
                if (
                    str(raw.get("goal_id") or "") != selected_goal
                    or str(raw.get("user_id") or "") != selected_user
                    or str(raw.get("kind") or "")
                    != ProjectGuardianEvaluator.GOAL_KIND
                ):
                    continue
                if (
                    str(raw.get("schema_version") or "")
                    != ProjectGuardianEvaluator.GOAL_SCHEMA
                    or str(raw.get("source") or "") != self.GOAL_SOURCE
                ):
                    raise ProjectGuardianGoalConflict(
                        "goal_id is not a controlled Project Guardian "
                        "release Goal"
                    )
                current_state_revision = self._state_revision(raw)
                if current_state_revision != selected_state_revision:
                    raise ProjectGuardianGoalConflict(
                        "expected_state_revision does not match the current "
                        "release Goal"
                    )
                current_status = str(raw.get("status") or "")
                producer_state: dict[str, Any] | None = None
                binding: dict[str, Any] | None = None
                if selected_status in {"active", "paused"}:
                    producer_state = self.state_store.read_json(
                        self.STATE_FILE
                    )
                    self._require_healthy(
                        producer_state,
                        self.STATE_FILE,
                    )
                    binding = self._binding_for(
                        selected_goal,
                        str(raw.get("revision") or ""),
                        state=producer_state,
                    )
                    if selected_status == "active" and binding is None:
                        raise ProjectGuardianGoalConflict(
                            "release Goal workspace binding is unavailable"
                        )
                    if (
                        current_status not in {"active", "paused"}
                        and binding is not None
                        and isinstance(
                            binding.get("github_actions"),
                            dict,
                        )
                        and self._goal_reserves_ci_capacity(
                            raw,
                            status=selected_status,
                            reference_time=transition_time,
                        )
                        and self._ci_binding_reservation_count(
                            producer_state=producer_state,
                            goals_state=state,
                            excluding_goal_id=selected_goal,
                            reference_time=transition_time,
                        )
                        >= self.MAX_CI_BINDINGS
                    ):
                        raise ProjectGuardianGoalConflict(
                            "Project Guardian GitHub CI binding capacity "
                            "exhausted"
                        )
                replacement = copy.deepcopy(raw)
                if current_status != selected_status:
                    replacement.update(
                        status=selected_status,
                        state_revision=current_state_revision + 1,
                        updated_at=transition_time.isoformat(),
                    )
                goals[index] = replacement
                state["goals"] = goals
                state["updated_at"] = replacement["updated_at"]
                updated = replacement
                return
            raise KeyError("release Goal not found in the requested user scope")

        self.state_store.mutate_json("user_goals.json", mutate)
        assert updated is not None
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "project_guardian_release_goal_status",
                "status": selected_status,
                "artifacts": {
                    "goal_id": selected_goal,
                    "goal_revision": str(updated.get("revision") or ""),
                    "goal_state_revision": int(
                        updated.get("state_revision") or 0
                    ),
                    "read_only_guardian": True,
                },
            },
        )
        return copy.deepcopy(updated)

    def record_deployment_intent(
        self,
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        expected_goal_state_revision: int,
        target_sha: str,
        target_environment: str,
        transition: str,
        operation_id: str,
        occurred_at: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Record one explicit local-control deployment intent transition.

        This entrypoint never infers intent from text, Goal registration, a
        model, or a scheduled producer tick. The caller must bind the command
        to the exact Goal state it observed and provide a stable operation id
        and occurrence time for transport retries.
        """

        selected_user = self._required_text(user_id, "user_id", 240)
        selected_goal = self._required_text(goal_id, "goal_id", 240)
        selected_revision = self._required_text(
            goal_revision,
            "goal_revision",
            120,
        )
        selected_session = self._required_text(
            session_id,
            "session_id",
            240,
        )
        selected_environment = self._required_text(
            target_environment,
            "target_environment",
            240,
        )
        selected_transition = str(transition or "").strip().lower()
        if selected_transition not in {"declare", "withdraw"}:
            raise ValueError("transition must be declare or withdraw")
        selected_operation = self._required_text(
            operation_id,
            "operation_id",
            240,
        )
        if any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789._:-"
            )
            for character in selected_operation
        ):
            raise ValueError(
                "operation_id may contain only letters, digits, . _ : -"
            )
        selected_occurred_at = self._time(occurred_at)
        if selected_occurred_at is None:
            raise ValueError("occurred_at must be timezone-aware")
        if (
            not isinstance(expected_goal_state_revision, int)
            or isinstance(expected_goal_state_revision, bool)
        ):
            raise ValueError(
                "expected_goal_state_revision must be a positive integer"
            )
        selected_state_revision = expected_goal_state_revision
        if selected_state_revision < 1:
            raise ValueError(
                "expected_goal_state_revision must be a positive integer"
            )
        selected_sha = str(target_sha or "").strip().lower()
        if (
            len(selected_sha) not in {40, 64}
            or any(
                character not in "0123456789abcdef"
                for character in selected_sha
            )
        ):
            raise ValueError("target_sha must be a full hexadecimal Git SHA")
        operation_digest = self._digest(
            {
                "user_id": selected_user,
                "operation_id": selected_operation,
            }
        )
        operation_ref = f"pgio_{operation_digest[:24]}"
        signal_state = (
            "present" if selected_transition == "declare" else "clear"
        )
        semantic_core = {
            "user_id": selected_user,
            "goal_id": selected_goal,
            "goal_revision": selected_revision,
            "expected_goal_state_revision": selected_state_revision,
            "target_sha": selected_sha,
            "target_environment": selected_environment,
            "transition": selected_transition,
            "occurred_at": selected_occurred_at.isoformat(),
            "session_id": selected_session,
        }
        semantic_digest = self._digest(semantic_core)

        with self.state_store.writer_transaction():
            mode = self._mode()
            fabric_mode = self._event_awareness_mode()
            if mode == "disabled":
                return {
                    "status": "disabled",
                    "mode": mode,
                    "reason": "project_guardian_disabled",
                    "operation_ref": operation_ref,
                    "transition": selected_transition,
                    "signal_state": signal_state,
                    "committed": False,
                }
            if fabric_mode == "disabled":
                return {
                    "status": "disabled",
                    "mode": mode,
                    "reason": "event_fabric_disabled",
                    "operation_ref": operation_ref,
                    "transition": selected_transition,
                    "signal_state": signal_state,
                    "committed": False,
                }
            if self.publish_deployment_intent is None:
                raise ValueError(
                    "Project Guardian deployment intent producer is unavailable"
                )

            producer_state = self.state_store.read_json(self.STATE_FILE)
            self._require_healthy(producer_state, self.STATE_FILE)
            operations = (
                producer_state.get("intent_operations")
                if isinstance(
                    producer_state.get("intent_operations"),
                    dict,
                )
                else {}
            )
            existing_operation = operations.get(operation_digest)
            if isinstance(existing_operation, dict):
                if (
                    str(existing_operation.get("semantic_digest") or "")
                    != semantic_digest
                ):
                    raise ProjectGuardianGoalConflict(
                        "operation_id is already bound to different semantics"
                    )
                operation = copy.deepcopy(existing_operation)
                if str(operation.get("status") or "") == "committed":
                    return {
                        "status": "duplicate",
                        "mode": mode,
                        "operation_ref": operation_ref,
                        "intent_status": "committed",
                        "transition": selected_transition,
                        "signal_state": signal_state,
                        "event_id": operation.get("event_id"),
                        "signal_ledger_status": operation.get(
                            "signal_ledger_status"
                        ),
                        "occurred_at": operation.get("occurred_at"),
                        "committed": True,
                    }
            else:
                accepted_at = self._aware_now()
                if (
                    selected_occurred_at
                    < accepted_at - timedelta(minutes=2)
                    or selected_occurred_at
                    > accepted_at + timedelta(minutes=2)
                ):
                    raise ValueError(
                        "occurred_at must be within the command acceptance "
                        "window"
                    )
                goals_state = self.state_store.read_json("user_goals.json")
                self._require_healthy(goals_state, "user_goals.json")
                goal = self._intent_goal(
                    goals_state,
                    user_id=selected_user,
                    goal_id=selected_goal,
                    goal_revision=selected_revision,
                    expected_state_revision=selected_state_revision,
                    target_sha=selected_sha,
                    target_environment=selected_environment,
                    occurred_at=selected_occurred_at,
                    accepted_at=accepted_at,
                )
                if goal is None:
                    raise ProjectGuardianGoalConflict(
                        "deployment intent does not match an active release Goal"
                    )
                binding = self._binding_for(
                    selected_goal,
                    selected_revision,
                    state=producer_state,
                )
                if (
                    binding is None
                    or not self._binding_matches_goal(binding, goal)
                ):
                    raise ProjectGuardianGoalConflict(
                        "deployment intent workspace binding is unavailable"
                    )
                operation = {
                    "schema_version": (
                        "veyra.project_guardian_deployment_intent_operation.v1"
                    ),
                    "operation_ref": operation_ref,
                    "operation_digest": operation_digest,
                    "semantic_digest": semantic_digest,
                    "goal_id": selected_goal,
                    "goal_revision": selected_revision,
                    "goal_state_revision": selected_state_revision,
                    "target_sha": selected_sha,
                    "target_environment": selected_environment,
                    "transition": selected_transition,
                    "occurred_at": selected_occurred_at.isoformat(),
                    "session_digest": self._digest(selected_session),
                    "signal_state": signal_state,
                    "status": "pending",
                    "created_at": accepted_at.isoformat(),
                    "updated_at": accepted_at.isoformat(),
                    "attempt_count": 0,
                }
                self._store_intent_operation(operation)

            try:
                admission = self.publish_deployment_intent(
                    user_id=selected_user,
                    goal_id=selected_goal,
                    goal_revision=selected_revision,
                    expected_goal_state_revision=selected_state_revision,
                    target_sha=selected_sha,
                    target_environment=selected_environment,
                    transition=selected_transition,
                    operation_digest=operation_digest,
                    occurred_at=selected_occurred_at.isoformat(),
                    session_id=selected_session,
                )
            except Exception as exc:
                admission = {
                    "status": "degraded",
                    "reason": "signal_ingress_failed",
                    "error_type": type(exc).__name__,
                }
            if not isinstance(admission, dict):
                admission = {
                    "status": "degraded",
                    "reason": "signal_ingress_invalid",
                }
            admission_status = str(admission.get("status") or "unknown")
            ledger_status = str(
                admission.get("signal_ledger_status") or "unknown"
            )
            committed = (
                admission_status in {"enqueued", "duplicate"}
                and ledger_status in {"recorded", "stale"}
            )
            operation.update(
                status="committed" if committed else "pending",
                updated_at=self._aware_now().isoformat(),
                attempt_count=int(operation.get("attempt_count") or 0) + 1,
                ingress_status=admission_status,
                signal_ledger_status=ledger_status,
                event_id=admission.get("event_id"),
                last_reason=(
                    None
                    if committed
                    else str(
                        admission.get("reason")
                        or "signal_ledger_not_committed"
                    )
                ),
            )
            self._store_intent_operation(operation)

        return {
            "status": admission_status if committed else "degraded",
            "mode": mode,
            "reason": (
                None
                if committed
                else str(
                    admission.get("reason")
                    or "signal_ledger_not_committed"
                )
            ),
            "operation_ref": operation_ref,
            "intent_status": operation["status"],
            "transition": selected_transition,
            "signal_state": signal_state,
            "event_id": admission.get("event_id"),
            "signal_ledger_status": ledger_status,
            "occurred_at": selected_occurred_at.isoformat(),
            "committed": committed,
        }

    def list_release_goals(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_user = self._required_text(user_id, "user_id", 240)
        state = self.state_store.read_json("user_goals.json")
        self._require_healthy(state, "user_goals.json")
        goals = (
            state.get("goals")
            if isinstance(state.get("goals"), list)
            else []
        )
        selected = [
            copy.deepcopy(goal)
            for goal in goals
            if isinstance(goal, dict)
            and str(goal.get("kind") or "")
            == ProjectGuardianEvaluator.GOAL_KIND
            and str(goal.get("schema_version") or "")
            == ProjectGuardianEvaluator.GOAL_SCHEMA
            and str(goal.get("source") or "") == self.GOAL_SOURCE
            and str(goal.get("user_id") or "") == selected_user
        ]
        selected.sort(
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        return selected[: max(0, min(int(limit), 500))]

    def status(self) -> dict[str, Any]:
        mode = self._mode()
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "mode": mode,
                "reason": "project_guardian_producer_state_corrupt",
                "state_frozen": True,
            }
        bindings = (
            state.get("bindings")
            if isinstance(state.get("bindings"), dict)
            else {}
        )
        ci_binding_count = sum(
            1
            for binding in bindings.values()
            if isinstance(binding, dict)
            and isinstance(binding.get("github_actions"), dict)
        )
        enabled_producers: list[str] = []
        if mode in {"record_only", "shadow"}:
            enabled_producers.append("git_dirty")
            if (
                ci_binding_count
                and self.ci_provider is not None
                and self.publish_ci_observation is not None
            ):
                enabled_producers.append("ci_failed")
            if self.publish_deployment_intent is not None:
                enabled_producers.append("deployment_intent")
        pending_producers: list[str] = []
        if "ci_failed" not in enabled_producers:
            pending_producers.append("ci_failed")
        if "deployment_intent" not in enabled_producers:
            pending_producers.append("deployment_intent")
        intent_operations = (
            state.get("intent_operations")
            if isinstance(state.get("intent_operations"), dict)
            else {}
        )
        last_run = (
            self._public_run_result(state.get("last_run"))
            if isinstance(state.get("last_run"), dict)
            else None
        )
        return {
            "status": "success",
            "mode": mode,
            "producer_contract": self.SCHEMA_VERSION,
            "enabled_producers": enabled_producers,
            "pending_producers": pending_producers,
            "binding_count": len(bindings),
            "ci_binding_count": ci_binding_count,
            "intent_operation_count": len(intent_operations),
            "last_run": last_run,
            "contracts": {
                "scheduled_pollers_background_only": True,
                "read_only": True,
                "deployment_intent_trigger": (
                    "explicit_local_control_only"
                ),
                "goal_registration_is_deployment_intent": False,
                "raw_git_output_persisted": False,
                "raw_provider_output_persisted": False,
                "raw_operation_id_persisted": False,
                "agent_invoked": False,
                "notifications": False,
                "project_mutation": False,
            },
        }

    def run_once(self, *, reason: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "busy", "mode": self._mode()}
        try:
            return self._run_once_locked(reason=reason)
        finally:
            self._run_lock.release()

    def public_run_once(self, *, reason: str = "manual") -> dict[str, Any]:
        """Run producers and return only tenant-neutral aggregate telemetry."""

        return self._public_run_result(self.run_once(reason=reason))

    def _run_once_locked(self, *, reason: str) -> dict[str, Any]:
        mode = self._mode()
        if mode == "disabled":
            return {
                "status": "disabled",
                "mode": mode,
                "observed_count": 0,
                "published_count": 0,
            }
        state = self.state_store.read_json(self.STATE_FILE)
        goals_state = self.state_store.read_json("user_goals.json")
        if state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "mode": mode,
                "reason": "project_guardian_producer_state_corrupt",
                "state_frozen": True,
                "observed_count": 0,
                "published_count": 0,
            }
        if goals_state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "mode": mode,
                "reason": "user_goals_state_corrupt",
                "state_frozen": True,
                "observed_count": 0,
                "published_count": 0,
            }

        now = self._aware_now()
        goals, _ = ProjectGuardianEvaluator()._active_release_goals(
            goals_state,
            now,
        )
        observations: list[dict[str, Any]] = []
        published_count = 0
        failed_count = 0
        producers = ["git_dirty"]
        ci_targets: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for goal in goals:
            raw_goal = self._goal_by_id(
                goals_state,
                str(goal["goal_id"]),
            )
            binding = self._binding_for(
                str(goal["goal_id"]),
                str(goal["revision"]),
                state=state,
            )
            if (
                raw_goal is None
                or binding is None
                or not self._binding_matches_goal(binding, raw_goal)
            ):
                failed_count += 1
                observations.append(
                    {
                        "goal_id": str(goal["goal_id"]),
                        "goal_revision": str(goal["revision"]),
                        "status": "degraded",
                        "reason": "workspace_binding_missing_or_mismatched",
                    }
                )
                continue
            git_observation = self._run_git_observation(
                goal=goal,
                binding=binding,
            )
            published_count += int(git_observation.pop("_published"))
            failed_count += int(git_observation.pop("_failed"))
            observations.append(git_observation)

            if isinstance(binding.get("github_actions"), dict):
                if "ci_failed" not in producers:
                    producers.append("ci_failed")
                ci_targets.append((goal, binding))

        for goal, binding in ci_targets:
            ci_observation = self._run_ci_observation(
                goal=goal,
                binding=binding,
            )
            published_count += int(ci_observation.pop("_published"))
            failed_count += int(ci_observation.pop("_failed"))
            observations.append(ci_observation)

        result = {
            "status": "degraded" if failed_count else "success",
            "mode": mode,
            "reason": reason,
            "producer": "git_dirty",
            "producers": producers,
            "active_goal_count": len(goals),
            "observed_count": len(observations),
            "published_count": published_count,
            "failed_count": failed_count,
            "observations": observations,
            "checked_at": now.isoformat(),
        }
        self._persist_run(result)
        return result

    def _run_git_observation(
        self,
        *,
        goal: dict[str, Any],
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        base = {
            "producer": "git_dirty",
            "goal_id": str(goal["goal_id"]),
            "goal_revision": str(goal["revision"]),
        }
        try:
            repository = self._inspect_repository(
                str(binding["canonical_path"]),
                expected_repo_id=str(binding["repo_id"]),
                expected_ref=str(binding["target_ref"]),
                expected_sha=str(binding["target_sha"]),
                expected_remote_origin_digest=str(
                    binding["remote_origin_digest"]
                ),
                expected_github_repo_id=(
                    str(binding["repo_id"])
                    if isinstance(binding.get("github_actions"), dict)
                    else None
                ),
            )
        except Exception as exc:
            return {
                **base,
                "status": "degraded",
                "reason": "git_probe_failed",
                "error_type": type(exc).__name__,
                "_published": False,
                "_failed": True,
            }
        repository_fact = {
            "repo_id": repository["repo_id"],
            "target_ref": repository["target_ref"],
            "target_sha": repository["target_sha"],
            "remote_origin_digest": repository[
                "remote_origin_digest"
            ],
            "dirty": repository["dirty"],
            "observed_at": repository["observed_at"],
            "index_flags_digest": repository["index_flags_digest"],
            "index_stage_digest": repository["index_stage_digest"],
            "probe_digest": repository["probe_digest"],
        }
        try:
            admission = self.publish_git_observation(
                user_id=str(goal["user_id"]),
                goal_id=str(goal["goal_id"]),
                goal_revision=str(goal["revision"]),
                repository_fact=repository_fact,
            )
        except Exception as exc:
            admission = {
                "status": "degraded",
                "reason": "signal_ingress_failed",
                "error_type": type(exc).__name__,
            }
        return self._producer_admission_observation(
            base=base,
            admission=admission,
            signal_state=(
                "present" if repository["dirty"] else "clear"
            ),
            fact={
                "repo_id": repository["repo_id"],
                "target_ref": repository["target_ref"],
                "target_sha": repository["target_sha"],
                "probe_digest": repository["probe_digest"],
            },
        )

    def _run_ci_observation(
        self,
        *,
        goal: dict[str, Any],
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        base = {
            "producer": "ci_failed",
            "goal_id": str(goal["goal_id"]),
            "goal_revision": str(goal["revision"]),
        }
        policy = binding.get("github_actions")
        if (
            not isinstance(policy, dict)
            or self.ci_provider is None
            or self.publish_ci_observation is None
        ):
            return {
                **base,
                "status": "degraded",
                "reason": "ci_producer_unavailable",
                "_published": False,
                "_failed": True,
            }
        try:
            fact = self.ci_provider.observe(
                binding=policy,
                target_ref=str(binding["target_ref"]),
                target_sha=str(binding["target_sha"]),
                now=self._aware_now(),
            )
            if not isinstance(fact, dict):
                raise GitHubCIUnknown("ci_fact_invalid")
            try:
                signal_state = str(fact["signal_state"])
                integer_fields = {
                    key: fact[key]
                    for key in ("workflow_id", "run_id", "run_attempt")
                }
                if any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 1
                    for value in integer_fields.values()
                ):
                    raise GitHubCIUnknown("ci_fact_invalid")
                fact_summary = {
                    "repo_id": str(fact["repo_id"]),
                    "target_ref": str(fact["target_ref"]),
                    "target_sha": str(fact["target_sha"]),
                    **integer_fields,
                    "probe_digest": str(fact["probe_digest"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubCIUnknown("ci_fact_invalid") from exc
            if (
                signal_state not in {"present", "clear"}
                or any(
                    fact_summary[key] < 1
                    for key in ("workflow_id", "run_id", "run_attempt")
                )
                or len(fact_summary["probe_digest"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in fact_summary["probe_digest"]
                )
            ):
                raise GitHubCIUnknown("ci_fact_invalid")
        except GitHubCIUnknown as exc:
            return {
                **base,
                "status": "unknown",
                "reason": exc.reason,
                "_published": False,
                "_failed": True,
            }
        except Exception as exc:
            return {
                **base,
                "status": "degraded",
                "reason": "ci_probe_failed",
                "error_type": type(exc).__name__,
                "_published": False,
                "_failed": True,
            }
        try:
            admission = self.publish_ci_observation(
                user_id=str(goal["user_id"]),
                goal_id=str(goal["goal_id"]),
                goal_revision=str(goal["revision"]),
                ci_fact=fact,
            )
        except Exception as exc:
            admission = {
                "status": "degraded",
                "reason": "signal_ingress_failed",
                "error_type": type(exc).__name__,
            }
        return self._producer_admission_observation(
            base=base,
            admission=admission,
            signal_state=signal_state,
            fact=fact_summary,
        )

    @staticmethod
    def _producer_admission_observation(
        *,
        base: dict[str, Any],
        admission: dict[str, Any],
        signal_state: str,
        fact: dict[str, Any],
    ) -> dict[str, Any]:
        admission_status = str(admission.get("status") or "unknown")
        ledger_status = str(
            admission.get("signal_ledger_status") or "unknown"
        )
        committed = (
            admission_status in {"enqueued", "duplicate"}
            and ledger_status in {"recorded", "stale"}
        )
        return {
            **base,
            "status": admission_status if committed else "degraded",
            "reason": (
                None
                if committed
                else str(
                    admission.get("reason")
                    or "signal_ledger_not_committed"
                )
            ),
            "ingress_status": admission_status,
            "signal_state": signal_state,
            **copy.deepcopy(fact),
            "event_id": admission.get("event_id"),
            "signal_ledger_status": ledger_status,
            "_published": admission_status == "enqueued" and committed,
            "_failed": not committed,
        }

    def _store_binding(self, binding: dict[str, Any]) -> None:
        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, self.STATE_FILE)
            bindings = (
                state.get("bindings")
                if isinstance(state.get("bindings"), dict)
                else {}
            )
            bindings = {
                str(key): copy.deepcopy(value)
                for key, value in bindings.items()
                if isinstance(value, dict)
            }
            bindings[str(binding["goal_id"])] = copy.deepcopy(binding)
            if len(bindings) > self.MAX_BINDINGS:
                inactive = sorted(
                    (
                        item
                        for item in bindings.values()
                        if str(item.get("goal_id") or "")
                        != str(binding["goal_id"])
                    ),
                    key=lambda item: str(item.get("registered_at") or ""),
                )
                while len(bindings) > self.MAX_BINDINGS and inactive:
                    stale = inactive.pop(0)
                    bindings.pop(str(stale.get("goal_id") or ""), None)
            if len(bindings) > self.MAX_BINDINGS:
                raise RuntimeError(
                    "project Guardian workspace binding capacity exhausted"
                )
            state["schema_version"] = self.SCHEMA_VERSION
            state["bindings"] = bindings
            state["binding_count"] = len(bindings)
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json(self.STATE_FILE, update)

    def _store_intent_operation(
        self,
        operation: dict[str, Any],
    ) -> None:
        operation_digest = str(operation.get("operation_digest") or "")
        if (
            len(operation_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in operation_digest
            )
        ):
            raise ValueError("deployment intent operation digest is invalid")

        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, self.STATE_FILE)
            operations = (
                state.get("intent_operations")
                if isinstance(state.get("intent_operations"), dict)
                else {}
            )
            operations = {
                str(key): copy.deepcopy(value)
                for key, value in operations.items()
                if isinstance(value, dict)
            }
            operations[operation_digest] = copy.deepcopy(operation)
            if len(operations) > self.MAX_INTENT_OPERATIONS:
                evictable = sorted(
                    (
                        (key, value)
                        for key, value in operations.items()
                        if key != operation_digest
                        and str(value.get("status") or "") == "committed"
                    ),
                    key=lambda item: (
                        str(item[1].get("updated_at") or ""),
                        item[0],
                    ),
                )
                while (
                    len(operations) > self.MAX_INTENT_OPERATIONS
                    and evictable
                ):
                    stale_digest, _ = evictable.pop(0)
                    operations.pop(stale_digest, None)
            if len(operations) > self.MAX_INTENT_OPERATIONS:
                raise ProjectGuardianGoalConflict(
                    "Project Guardian deployment intent operation capacity "
                    "exhausted"
                )
            state["schema_version"] = self.SCHEMA_VERSION
            state.setdefault("bindings", {})
            state["intent_operations"] = operations
            state["intent_operation_count"] = len(operations)
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json(self.STATE_FILE, update)

    def _store_goal(
        self,
        goal: dict[str, Any],
        *,
        existing: dict[str, Any] | None,
    ) -> None:
        def update(state: dict[str, Any]) -> None:
            self._require_healthy(state, "user_goals.json")
            goals = (
                state.get("goals")
                if isinstance(state.get("goals"), list)
                else []
            )
            release_goals = [
                item
                for item in goals
                if isinstance(item, dict)
                and str(item.get("kind") or "")
                == ProjectGuardianEvaluator.GOAL_KIND
            ]
            if existing is None and len(release_goals) >= self.MAX_RELEASE_GOALS:
                raise RuntimeError(
                    "project release Goal capacity exhausted"
                )
            replaced = False
            for index, raw in enumerate(goals):
                if (
                    isinstance(raw, dict)
                    and str(raw.get("goal_id") or "")
                    == str(goal["goal_id"])
                ):
                    current_state_revision = self._state_revision(raw)
                    if (
                        existing is None
                        or current_state_revision
                        != self._state_revision(existing)
                    ):
                        raise ProjectGuardianGoalConflict(
                            "release Goal changed during registration"
                        )
                    goals[index] = copy.deepcopy(goal)
                    replaced = True
                    break
            if not replaced:
                goals.append(copy.deepcopy(goal))
            state["goals"] = goals
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("user_goals.json", update)

    def _binding_for(
        self,
        goal_id: str,
        goal_revision: str,
        *,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        selected = (
            state
            if isinstance(state, dict)
            else self.state_store.read_json(self.STATE_FILE)
        )
        self._require_healthy(selected, self.STATE_FILE)
        bindings = (
            selected.get("bindings")
            if isinstance(selected.get("bindings"), dict)
            else {}
        )
        binding = bindings.get(goal_id)
        if (
            not isinstance(binding, dict)
            or str(binding.get("goal_revision") or "")
            != str(goal_revision)
        ):
            return None
        return copy.deepcopy(binding)

    def _event_awareness_mode(self) -> str:
        config = self.state_store.read_json("ops_config.json")
        if config.get("_state_corrupt") is True:
            return "disabled"
        section = (
            config.get("event_awareness")
            if isinstance(config.get("event_awareness"), dict)
            else {}
        )
        mode = str(section.get("mode") or "disabled").strip().lower()
        return (
            mode
            if mode in {"disabled", "record_only", "shadow"}
            else "disabled"
        )

    @classmethod
    def _intent_goal(
        cls,
        goals_state: dict[str, Any],
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        expected_state_revision: int,
        target_sha: str,
        target_environment: str,
        occurred_at: datetime,
        accepted_at: datetime,
    ) -> dict[str, Any] | None:
        goals = (
            goals_state.get("goals")
            if isinstance(goals_state.get("goals"), list)
            else []
        )
        matches = [
            raw
            for raw in goals
            if isinstance(raw, dict)
            and str(raw.get("goal_id") or "") == goal_id
            and str(raw.get("kind") or "")
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
            or str(goal.get("source") or "") != cls.GOAL_SOURCE
            or str(goal.get("status") or "") != "active"
            or str(goal.get("user_id") or "") != user_id
            or str(goal.get("revision") or "") != goal_revision
            or cls._state_revision(goal) != expected_state_revision
            or str(goal.get("target_sha") or "").strip().lower()
            != target_sha
            or str(scope.get("target_environment") or "")
            != target_environment
            or any(
                not str(scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
            or active_from is None
            or active_until is None
            or not (active_from <= occurred_at <= active_until)
            or not (active_from <= accepted_at <= active_until)
        ):
            return None
        return copy.deepcopy(goal)

    @classmethod
    def _ci_binding_reservation_count(
        cls,
        *,
        producer_state: dict[str, Any],
        goals_state: dict[str, Any],
        excluding_goal_id: str | None = None,
        reference_time: datetime,
    ) -> int:
        bindings = (
            producer_state.get("bindings")
            if isinstance(producer_state.get("bindings"), dict)
            else {}
        )
        goals = (
            goals_state.get("goals")
            if isinstance(goals_state.get("goals"), list)
            else []
        )
        controlled_goals = {
            str(goal.get("goal_id") or ""): goal
            for goal in goals
            if isinstance(goal, dict)
            and str(goal.get("schema_version") or "")
            == ProjectGuardianEvaluator.GOAL_SCHEMA
            and str(goal.get("source") or "") == cls.GOAL_SOURCE
            and cls._goal_reserves_ci_capacity(
                goal,
                status=str(goal.get("status") or ""),
                reference_time=reference_time,
            )
        }
        return sum(
            1
            for goal_id, binding in bindings.items()
            if str(goal_id) != str(excluding_goal_id or "")
            and isinstance(binding, dict)
            and isinstance(binding.get("github_actions"), dict)
            and str(goal_id) in controlled_goals
            and str(binding.get("goal_revision") or "")
            == str(
                controlled_goals[str(goal_id)].get("revision") or ""
            )
        )

    @classmethod
    def _goal_reserves_ci_capacity(
        cls,
        goal: dict[str, Any],
        *,
        status: str,
        reference_time: datetime,
    ) -> bool:
        if str(status or "") not in {"active", "paused"}:
            return False
        active_until = cls._time(goal.get("active_until"))
        return active_until is None or active_until >= reference_time

    @staticmethod
    def _binding_matches_goal(
        binding: dict[str, Any],
        goal: dict[str, Any],
    ) -> bool:
        scope = (
            goal.get("scope")
            if isinstance(goal.get("scope"), dict)
            else {}
        )
        return (
            str(binding.get("goal_id") or "")
            == str(goal.get("goal_id") or "")
            and str(binding.get("user_id") or "")
            == str(goal.get("user_id") or "")
            and str(binding.get("goal_revision") or "")
            == str(goal.get("revision") or "")
            and str(binding.get("workspace_id") or "")
            == str(scope.get("workspace_id") or "")
            and str(binding.get("repo_id") or "")
            == str(scope.get("repo_id") or "")
            and str(binding.get("target_ref") or "")
            == str(scope.get("target_ref") or "")
            and str(binding.get("target_sha") or "")
            == str(goal.get("target_sha") or "")
            and len(str(binding.get("remote_origin_digest") or "")) == 64
        )

    def _inspect_repository(
        self,
        workspace_path: str,
        *,
        expected_repo_id: str,
        expected_ref: str,
        expected_sha: str | None = None,
        expected_remote_origin_digest: str | None = None,
        expected_github_repo_id: str | None = None,
    ) -> dict[str, Any]:
        selected_path = Path(
            self._required_text(workspace_path, "workspace_path", 4096)
        ).expanduser()
        try:
            canonical_path = selected_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace_path does not exist") from exc
        if not canonical_path.is_dir():
            raise ValueError("workspace_path must be a directory")

        root_text = self._git_output(
            canonical_path,
            ["rev-parse", "--show-toplevel"],
        )
        try:
            git_root = Path(root_text).resolve(strict=True)
        except OSError as exc:
            raise ValueError("Git reported an invalid workspace root") from exc
        if git_root != canonical_path:
            raise ValueError(
                "workspace_path must resolve to the exact Git worktree root"
            )
        remote = self._remote_origin(git_root)
        remote_origin_digest = self._digest(remote)
        repo_id = self._repo_id_from_remote(remote)
        if repo_id != self._normalize_repo_id(expected_repo_id):
            raise ValueError("Git remote does not match the release Goal repo_id")
        github_repo_id = self._github_repo_id_from_remote(remote)
        if (
            expected_github_repo_id
            and github_repo_id
            != self._normalize_repo_id(expected_github_repo_id)
        ):
            raise ValueError(
                "Git origin is not the expected github.com repository"
            )
        if (
            expected_remote_origin_digest
            and remote_origin_digest
            != str(expected_remote_origin_digest).strip().lower()
        ):
            raise ValueError(
                "Git remote origin does not match the registered binding"
            )
        target_ref = self._git_output(
            git_root,
            ["symbolic-ref", "-q", "HEAD"],
        )
        if target_ref != expected_ref:
            raise ValueError(
                "Git worktree ref does not match the release Goal target_ref"
            )
        target_sha = self._git_output(git_root, ["rev-parse", "HEAD"]).lower()
        if expected_sha and target_sha != str(expected_sha).strip().lower():
            raise ValueError(
                "Git HEAD does not match the release Goal target_sha"
            )
        if (git_root / ".gitmodules").exists():
            raise ValueError(
                "Git submodules are unsupported by this producer slice"
            )
        self._require_no_repository_filters(git_root)
        index_flags = self._git_output(
            git_root,
            ["ls-files", "-v"],
            allow_empty=True,
        )
        self._require_supported_index_flags(index_flags)
        index_stage = self._git_output(
            git_root,
            ["ls-files", "--stage"],
            allow_empty=True,
        )
        self._require_supported_index_stage(index_stage)
        self._require_no_repository_filters(git_root)
        status = self._isolated_status_output(git_root)
        self._require_no_repository_filters(git_root)
        # A commit/ref switch racing the status command must not let a clean
        # result escape with the pre-race SHA. Re-read the repository identity
        # after status and fail closed if the snapshot moved.
        final_root_text = self._git_output(
            git_root,
            ["rev-parse", "--show-toplevel"],
        )
        try:
            final_root = Path(final_root_text).resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                "Git workspace root changed during the read-only probe"
            ) from exc
        final_remote = self._remote_origin(git_root)
        final_repo_id = self._repo_id_from_remote(final_remote)
        final_remote_origin_digest = self._digest(final_remote)
        final_ref = self._git_output(
            git_root,
            ["symbolic-ref", "-q", "HEAD"],
        )
        final_sha = self._git_output(
            git_root,
            ["rev-parse", "HEAD"],
        ).lower()
        if (
            final_root != git_root
            or final_repo_id != repo_id
            or final_remote_origin_digest != remote_origin_digest
            or final_ref != target_ref
            or final_sha != target_sha
        ):
            raise RuntimeError(
                "Git repository identity changed during the read-only probe"
            )
        # This timestamp intentionally precedes the final status command.
        # Without a lock shared by every worktree writer, the producer cannot
        # claim that a repository remains unchanged at the later ingress time.
        # The signal therefore represents this bounded point-in-time snapshot.
        observed_at = self._aware_now()
        final_index_flags = self._git_output(
            git_root,
            ["ls-files", "-v"],
            allow_empty=True,
        )
        self._require_supported_index_flags(final_index_flags)
        final_index_stage = self._git_output(
            git_root,
            ["ls-files", "--stage"],
            allow_empty=True,
        )
        self._require_supported_index_stage(final_index_stage)
        if (
            final_index_flags != index_flags
            or final_index_stage != index_stage
        ):
            raise RuntimeError(
                "Git index changed during the read-only probe"
            )
        self._require_no_repository_filters(git_root)
        final_status = self._isolated_status_output(git_root)
        self._require_no_repository_filters(git_root)
        settled_root_text = self._git_output(
            git_root,
            ["rev-parse", "--show-toplevel"],
        )
        try:
            settled_root = Path(settled_root_text).resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                "Git workspace root changed during the read-only probe"
            ) from exc
        settled_remote = self._remote_origin(git_root)
        settled_repo_id = self._repo_id_from_remote(settled_remote)
        settled_remote_origin_digest = self._digest(settled_remote)
        settled_ref = self._git_output(
            git_root,
            ["symbolic-ref", "-q", "HEAD"],
        )
        settled_sha = self._git_output(
            git_root,
            ["rev-parse", "HEAD"],
        ).lower()
        settled_index_flags = self._git_output(
            git_root,
            ["ls-files", "-v"],
            allow_empty=True,
        )
        self._require_supported_index_flags(settled_index_flags)
        settled_index_stage = self._git_output(
            git_root,
            ["ls-files", "--stage"],
            allow_empty=True,
        )
        self._require_supported_index_stage(settled_index_stage)
        if (
            final_status != status
            or settled_index_flags != index_flags
            or settled_index_stage != index_stage
            or settled_root != git_root
            or settled_repo_id != repo_id
            or settled_remote_origin_digest != remote_origin_digest
            or settled_ref != target_ref
            or settled_sha != target_sha
        ):
            raise RuntimeError(
                "Git worktree changed during the read-only probe"
            )
        self._require_no_repository_filters(git_root)
        dirty = bool(final_status.strip())
        probe_digest = hashlib.sha256(
            json.dumps(
                {
                    "repo_id": repo_id,
                    "target_ref": target_ref,
                    "target_sha": target_sha,
                    "remote_origin_digest": remote_origin_digest,
                    "dirty": dirty,
                    "observed_at": observed_at.isoformat(),
                    "index_flags_digest": self._digest(index_flags),
                    "index_stage_digest": self._digest(index_stage),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "canonical_path": str(git_root),
            "repo_id": repo_id,
            "github_repo_id": github_repo_id,
            "target_ref": target_ref,
            "target_sha": target_sha,
            "remote_origin_digest": remote_origin_digest,
            "dirty": dirty,
            "observed_at": observed_at.isoformat(),
            "index_flags_digest": self._digest(index_flags),
            "index_stage_digest": self._digest(index_stage),
            "probe_digest": probe_digest,
        }

    @staticmethod
    def _sanitized_git_env() -> dict[str, str]:
        git_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_ATTR_SOURCE",
                "GIT_COMMON_DIR",
                "GIT_CONFIG",
                "GIT_DIR",
                "GIT_GRAFT_FILE",
                "GIT_INDEX_FILE",
                "GIT_NAMESPACE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_REPLACE_REF_BASE",
                "GIT_SHALLOW_FILE",
                "GIT_WORK_TREE",
            }
            and not key.startswith("GIT_CONFIG_")
            and not key.startswith("GIT_TRACE")
        }
        git_env.update(
            GIT_ATTR_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
        )
        return git_env

    @staticmethod
    def _git_command_prefix() -> list[str]:
        return [
            "git",
            "--no-replace-objects",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.trustctime=true",
            "-c",
            "core.checkStat=default",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.symlinks=true",
            "-c",
            "core.ignoreCase=false",
            "--no-optional-locks",
        ]

    def _isolated_status_output(self, root: Path) -> str:
        index_path = self._resolved_git_path(root, "index")
        objects_path = self._resolved_git_path(root, "objects")
        if not index_path.is_file() or not objects_path.is_dir():
            raise RuntimeError("Git repository metadata is unavailable")
        shared_index_text = self._git_output(
            root,
            ["rev-parse", "--shared-index-path"],
            allow_empty=True,
        )
        shared_index = (
            self._resolved_path(root, shared_index_text)
            if shared_index_text
            else None
        )
        if shared_index is not None and not shared_index.is_file():
            raise RuntimeError("Git shared index metadata is unavailable")
        object_format = self._git_output(
            root,
            ["rev-parse", "--show-object-format"],
        )
        if object_format not in {"sha1", "sha256"}:
            raise RuntimeError("unsupported Git object format")
        head_sha = self._git_output(root, ["rev-parse", "HEAD"]).lower()

        try:
            with tempfile.TemporaryDirectory(
                prefix="veyra-project-guardian-git-"
            ) as temporary:
                git_dir = Path(temporary) / "git"
                git_dir.mkdir()
                (git_dir / "refs").mkdir()
                source_index = git_dir / "source-index"
                fresh_index = git_dir / "fresh-index"
                shutil.copyfile(index_path, source_index)
                if shared_index is not None:
                    shutil.copyfile(
                        shared_index,
                        git_dir / shared_index.name,
                    )
                repository_format = 1 if object_format == "sha256" else 0
                config = (
                    "[core]\n"
                    f"\trepositoryFormatVersion = {repository_format}\n"
                    "\tbare = false\n"
                )
                if object_format == "sha256":
                    config += (
                        "[extensions]\n"
                        "\tobjectFormat = sha256\n"
                    )
                (git_dir / "config").write_text(
                    config,
                    encoding="utf-8",
                )
                (git_dir / "HEAD").write_text(
                    f"{head_sha}\n",
                    encoding="ascii",
                )
                git_env = self._sanitized_git_env()
                git_env.update(
                    GIT_DIR=str(git_dir),
                    GIT_OBJECT_DIRECTORY=str(objects_path),
                    GIT_WORK_TREE=str(root),
                )
                source_env = dict(git_env)
                source_env["GIT_INDEX_FILE"] = str(source_index)
                index_result = subprocess.run(
                    [
                        *self._git_command_prefix(),
                        "diff-index",
                        "--cached",
                        "--quiet",
                        "--no-ext-diff",
                        "--no-textconv",
                        "HEAD",
                        "--",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.command_timeout_seconds,
                    env=source_env,
                )
                if index_result.returncode not in {0, 1}:
                    raise RuntimeError(
                        "isolated Git index comparison returned an error"
                    )

                # Build a new index from HEAD instead of trusting the copied
                # index's filesystem stat cache. Its entries have no
                # worktree stat data, so status must inspect tracked content
                # even when a same-size rewrite restores mtime within the
                # filesystem's ctime resolution. The copied index is compared
                # separately above so index-only staged changes still count.
                fresh_env = dict(git_env)
                fresh_env["GIT_INDEX_FILE"] = str(fresh_index)
                read_tree_result = subprocess.run(
                    [
                        *self._git_command_prefix(),
                        "read-tree",
                        "--reset",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.command_timeout_seconds,
                    env=fresh_env,
                )
                if read_tree_result.returncode != 0:
                    raise RuntimeError(
                        "isolated Git index reconstruction returned an error"
                    )
                result = subprocess.run(
                    [
                        *self._git_command_prefix(),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=normal",
                        "--ignore-submodules=all",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.command_timeout_seconds,
                    env=fresh_env,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("isolated read-only Git status failed") from exc
        if result.returncode != 0:
            raise RuntimeError("isolated read-only Git status returned an error")
        index_marker = "index:dirty" if index_result.returncode == 1 else ""
        return "\n".join(
            item for item in (index_marker, result.stdout.strip()) if item
        )

    def _resolved_git_path(self, root: Path, name: str) -> Path:
        value = self._git_output(
            root,
            ["rev-parse", "--git-path", name],
        )
        return self._resolved_path(root, value)

    @staticmethod
    def _resolved_path(root: Path, value: str) -> Path:
        selected = Path(value)
        if not selected.is_absolute():
            selected = root / selected
        try:
            return selected.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("Git reported an invalid metadata path") from exc

    def _git_output(
        self,
        root: Path,
        args: list[str],
        *,
        allow_empty: bool = False,
        allow_missing: bool = False,
    ) -> str:
        git_env = self._sanitized_git_env()
        try:
            result = subprocess.run(
                [
                    *self._git_command_prefix(),
                    "-C",
                    str(root),
                    *args,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.command_timeout_seconds,
                env=git_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("read-only Git probe failed") from exc
        output = result.stdout.strip()
        allowed_returncodes = {0, 1} if allow_missing else {0}
        if (
            result.returncode not in allowed_returncodes
            or (not allow_empty and not output)
        ):
            raise RuntimeError("read-only Git probe returned an error")
        return output

    def _remote_origin(self, root: Path) -> str:
        output = self._git_output(
            root,
            ["remote", "get-url", "--all", "origin"],
        )
        urls = [
            line.strip()
            for line in output.splitlines()
            if line.strip()
        ]
        if len(urls) != 1:
            raise ValueError(
                "Git origin must have exactly one non-empty fetch URL"
            )
        return urls[0]

    def _require_no_repository_filters(self, root: Path) -> None:
        attributes_path_text = self._git_output(
            root,
            ["rev-parse", "--git-path", "info/attributes"],
        )
        attributes_path = Path(attributes_path_text)
        if not attributes_path.is_absolute():
            attributes_path = root / attributes_path
        if attributes_path.exists():
            raise RuntimeError(
                "Repository-private Git attributes are unsupported by this "
                "producer slice"
            )
        filter_config = self._git_output(
            root,
            [
                "config",
                "--includes",
                "--name-only",
                "--get-regexp",
                r"^filter\.",
            ],
            allow_empty=True,
            allow_missing=True,
        )
        if filter_config:
            raise RuntimeError(
                "Repository-configured Git filters are unsupported by this "
                "producer slice"
            )

    @staticmethod
    def _require_supported_index_flags(value: str) -> None:
        for line in value.splitlines():
            if not line:
                continue
            tag = line[0]
            if tag.islower() or tag.upper() == "S":
                raise RuntimeError(
                    "Git assume-unchanged, skip-worktree, or sparse index "
                    "flags are unsupported by this producer slice"
                )

    @staticmethod
    def _require_supported_index_stage(value: str) -> None:
        if any(
            line.startswith("160000 ")
            for line in value.splitlines()
            if line
        ):
            raise RuntimeError(
                "Gitlink index entries are unsupported by this producer slice"
            )

    @classmethod
    def _repo_id_from_remote(cls, value: str) -> str:
        remote = str(value or "").strip()
        if not remote:
            return ""
        if "://" in remote:
            path = urlsplit(remote).path
        elif "@" in remote and ":" in remote:
            path = remote.split(":", 1)[1]
        else:
            path = remote
        parts = [
            item
            for item in path.replace("\\", "/").strip("/").split("/")
            if item
        ]
        if len(parts) < 2:
            return ""
        name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
        return cls._normalize_repo_id(f"{parts[-2]}/{name}")

    @classmethod
    def _github_repo_id_from_remote(cls, value: str) -> str:
        """Return owner/repo only for an exact canonical github.com origin."""

        remote = str(value or "").strip()
        if not remote:
            return ""
        path = ""
        if "://" in remote:
            parsed = urlsplit(remote)
            if (
                parsed.scheme.lower() not in {"https", "ssh"}
                or str(parsed.hostname or "").lower() != "github.com"
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme.lower() == "ssh"
                    and str(parsed.username or "") != "git"
                )
            ):
                return ""
            path = parsed.path
        elif "@" in remote and ":" in remote:
            authority, path = remote.split(":", 1)
            try:
                username, host = authority.rsplit("@", 1)
            except ValueError:
                return ""
            if username != "git" or host.lower() != "github.com":
                return ""
        else:
            return ""
        parts = [
            item
            for item in path.replace("\\", "/").strip("/").split("/")
            if item
        ]
        if len(parts) != 2:
            return ""
        name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
        return cls._normalize_repo_id(f"{parts[0]}/{name}")

    @staticmethod
    def _normalize_repo_id(value: Any) -> str:
        text = str(value or "").strip().strip("/")
        if text.endswith(".git"):
            text = text[:-4]
        parts = text.split("/")
        if (
            len(parts) != 2
            or any(not part or len(part) > 120 for part in parts)
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for part in parts
                for character in part
            )
        ):
            return ""
        return f"{parts[0].lower()}/{parts[1].lower()}"

    @classmethod
    def _goal_revision(
        cls,
        *,
        goal_id: str,
        user_id: str,
        scope: dict[str, str],
        target_sha: str,
        canonical_path: str,
        remote_origin_digest: str,
        active_from: str,
        active_until: str,
        ci_policy_digest: str = "",
    ) -> str:
        return "pgrg_" + cls._digest(
            {
                "goal_id": goal_id,
                "user_id": user_id,
                "scope": scope,
                "target_sha": target_sha,
                "workspace_binding": cls._digest(
                    {
                        "canonical_path": canonical_path,
                        "remote_origin_digest": remote_origin_digest,
                    }
                ),
                "ci_policy_digest": ci_policy_digest,
                "active_from": active_from,
                "active_until": active_until,
            }
        )[:20]

    @classmethod
    def _binding_id(
        cls,
        *,
        goal_id: str,
        goal_revision: str,
        canonical_path: str,
        repo_id: str,
        target_ref: str,
        target_sha: str,
        remote_origin_digest: str,
        ci_policy_digest: str = "",
    ) -> str:
        return "pgwb_" + cls._digest(
            {
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "canonical_path": canonical_path,
                "repo_id": repo_id,
                "target_ref": target_ref,
                "target_sha": target_sha,
                "remote_origin_digest": remote_origin_digest,
                "ci_policy_digest": ci_policy_digest,
            }
        )[:24]

    def _persist_run(self, result: dict[str, Any]) -> None:
        compact = copy.deepcopy(result)
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
            state.setdefault("bindings", {})
            state["runs"] = runs[-self.MAX_RUNS :]
            state["last_run"] = compact
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json(self.STATE_FILE, update)

    @staticmethod
    def _public_run_result(value: Any) -> dict[str, Any]:
        selected = value if isinstance(value, dict) else {}
        allowed = (
            "status",
            "mode",
            "reason",
            "producer",
            "producers",
            "active_goal_count",
            "observed_count",
            "published_count",
            "failed_count",
            "checked_at",
            "state_frozen",
            "error_type",
        )
        return {
            key: copy.deepcopy(selected[key])
            for key in allowed
            if key in selected
        }

    def _mode(self) -> str:
        config = self.state_store.read_json("ops_config.json")
        section = (
            config.get("project_guardian")
            if isinstance(config.get("project_guardian"), dict)
            else {}
        )
        mode = str(section.get("mode") or "disabled").strip().lower()
        return mode if mode in {"disabled", "record_only", "shadow"} else "disabled"

    def _aware_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Project Guardian producer clock must be timezone-aware")
        return value.astimezone(timezone.utc).replace(microsecond=0)

    @classmethod
    def _goal_by_id(
        cls,
        state: dict[str, Any],
        goal_id: str,
    ) -> dict[str, Any] | None:
        goals = (
            state.get("goals")
            if isinstance(state.get("goals"), list)
            else []
        )
        for goal in goals:
            if (
                isinstance(goal, dict)
                and str(goal.get("goal_id") or "") == str(goal_id)
                and str(goal.get("kind") or "")
                == ProjectGuardianEvaluator.GOAL_KIND
            ):
                return copy.deepcopy(goal)
        return None

    @staticmethod
    def _required_text(value: Any, field: str, limit: int) -> str:
        text = str(value or "").strip()
        if not text or len(text) > limit or "\x00" in text:
            raise ValueError(f"{field} is required and must be <= {limit} chars")
        return text

    @staticmethod
    def _state_revision(value: dict[str, Any]) -> int:
        raw = value.get("state_revision")
        if isinstance(raw, bool):
            return 0
        try:
            selected = int(raw)
        except (TypeError, ValueError):
            return 0
        return selected if selected >= 1 else 0

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
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

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
