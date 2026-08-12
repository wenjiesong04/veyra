"""A bounded, server-owned workspace observation producer.

This module is deliberately a producer, not a second awareness pipeline.  It
captures one exact Git worktree, turns the result into typed structured
observations, and submits those observations through the existing
``StructuredObservationIngress``.  No workspace command, path, test process,
or free-form fact is accepted from a caller.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import os
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.structured_observation import (
    StructuredObservationCommand,
    canonical_utc,
)
from runtime.isolated_git_snapshot import (
    GitSnapshotUnavailable,
    GitWorkspaceObservation,
    capture_isolated_git_observation,
)
from runtime.trusted_workspace_observer_state import (
    CONFIG_SCHEMA_VERSION,
    DIGEST_RE,
    REF_RE,
    SCHEMA_VERSION,
    STATE_FILE,
    StateValidationError,
    digest as _digest,
    observation_record,
    state_revision,
    validate_state,
)

_DIGEST_RE = DIGEST_RE


CONTROL_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"


class TrustedWorkspaceObserverError(RuntimeError):
    """Base failure for the private trusted workspace observer."""


class TrustedWorkspaceObserverUnauthorized(TrustedWorkspaceObserverError):
    """The private configuration token was absent or invalid."""


class TrustedWorkspaceObserverConflict(TrustedWorkspaceObserverError):
    """Configuration or observation binding no longer matches current state."""


def _observer_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if not self._lock.acquire(blocking=False):
            raise TrustedWorkspaceObserverConflict("observer is busy")
        try:
            return method(self, *args, **kwargs)
        finally:
            self._lock.release()
    return wrapped


class TrustedWorkspaceObserver:
    """Observe one explicitly configured Git workspace in ``record_only`` mode.

    The observer is intentionally single-binding.  The binding stores no raw
    repository path; each run resolves ``local_world.current_project`` and
    requires the server-captured canonical root and identity digests to match.
    """

    PRODUCER_ID = "workspace_observer"
    DEFAULT_GRACE_SECONDS = 300

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        publish_observation: Callable[[StructuredObservationCommand], dict[str, Any]],
        ci_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        snapshotter: Callable[..., GitWorkspaceObservation] | None = None,
        control_token: str | None = None,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.state_store = state_store
        self.publish_observation = publish_observation
        self.ci_provider = ci_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._snapshotter = snapshotter or capture_isolated_git_observation
        self.control_token = str(
            os.getenv(CONTROL_TOKEN_ENV, "")
            if control_token is None
            else control_token
        ).strip()
        self.grace_seconds = max(1, min(int(grace_seconds), 7 * 24 * 3600))
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Private configuration and status
    # ------------------------------------------------------------------
    @_observer_locked
    def configure(
        self,
        *,
        control_token: str,
        expected_state_revision: int,
        mode: str,
        user_id: str,
        session_id: str,
        workspace_id: str,
        goal_id: str,
        ci_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Install one explicit binding using a token-protected CAS update."""

        self._authorize(control_token)
        selected_mode = str(mode or "").strip().lower()
        if selected_mode not in {"disabled", "record_only"}:
            raise TrustedWorkspaceObserverConflict(
                "workspace observer mode must be disabled or record_only"
            )
        if (
            isinstance(expected_state_revision, bool)
            or not isinstance(expected_state_revision, int)
            or expected_state_revision < 0
        ):
            raise TrustedWorkspaceObserverConflict("invalid observer CAS revision")
        selected_user = self._scope(user_id, "user_id")
        selected_session = self._scope(session_id, "session_id")
        selected_goal = self._scope(goal_id, "goal_id")
        if selected_mode == "disabled":
            return self._configure_disabled(
                expected_state_revision=expected_state_revision,
                user_id=selected_user,
                session_id=selected_session,
                goal_id=selected_goal,
            )
        selected_workspace = self._workspace(workspace_id)
        # Validate the durable observer document before doing any probe or
        # CAS work.  A malformed mode/binding must fail closed rather than be
        # interpreted as a fresh configuration.
        self._read_state()
        snapshot = self._capture(selected_workspace)
        if snapshot.dirty:
            raise TrustedWorkspaceObserverConflict(
                "workspace must be clean before observer configuration"
            )
        goal = self._active_goal(
            user_id=selected_user,
            workspace_id=selected_workspace,
            goal_id=selected_goal,
        )
        if goal is None:
            raise TrustedWorkspaceObserverConflict(
                "goal must be exactly one active owner/workspace Goal with priority"
            )
        ci = self._validate_ci_binding(ci_binding) if ci_binding is not None else None
        if ci is not None and self.ci_provider is None:
            raise TrustedWorkspaceObserverConflict("CI provider is unavailable")
        if ci is not None:
            if str(snapshot.origin_host or "") != "github.com":
                raise TrustedWorkspaceObserverConflict(
                    "GitHub CI binding requires canonical github.com origin"
                )
            if str(ci.get("repo_id") or "").lower() != snapshot.repo_id.lower():
                raise TrustedWorkspaceObserverConflict("CI repository does not match workspace")
        binding = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "status": selected_mode,
            "user_id": selected_user,
            "session_id": selected_session,
            "workspace_digest": self._workspace_digest(selected_workspace),
            "repo_id": snapshot.repo_id,
            "origin_digest": snapshot.origin_digest,
            "origin_host": str(snapshot.origin_host or ""),
            "full_ref": snapshot.full_ref,
            "goal_id": selected_goal,
            "goal_digest": str(goal["goal_digest"]),
            "goal_revision": str(goal.get("goal_revision") or ""),
            "goal_priority": float(goal["priority"]),
            "ci_binding": ci,
            "binding_generation": 0,
        }
        configure_baseline = self._observation_record(snapshot, self._subject(snapshot, None), None)
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            current_revision = self._state_revision(state)
            if current_revision != expected_state_revision:
                raise TrustedWorkspaceObserverConflict(
                    "observer configuration CAS revision mismatch"
                )
            previous = state.get("binding") if isinstance(state.get("binding"), dict) else None
            generation = int(previous.get("binding_generation") or 0) + 1 if previous else 1
            binding["binding_generation"] = generation
            state["schema_version"] = SCHEMA_VERSION
            state["binding"] = copy.deepcopy(binding)
            # Persist the exact clean configure-time identity.  The first
            # scheduled run therefore detects any revision drift that occurs
            # between configure and run instead of silently absorbing it.
            state["baseline"] = copy.deepcopy(configure_baseline)
            state["last_observation"] = copy.deepcopy(configure_baseline)
            state["active_change"] = None
            state["last_run"] = None
            state["pending_delivery"] = None
            result.update(
                status="configured",
                mode=selected_mode,
                binding_generation=generation,
                state_revision=current_revision + 1,
            )
            return state

        self.state_store.mutate_json(STATE_FILE, update)
        return {**result, "authority": self.authority()}

    def _configure_disabled(
        self,
        *,
        expected_state_revision: int,
        user_id: str,
        session_id: str,
        goal_id: str,
    ) -> dict[str, Any]:
        """Disable without probing Git, Goal, CI, or the local workspace."""
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            current_revision = self._state_revision(state)
            if current_revision != expected_state_revision:
                raise TrustedWorkspaceObserverConflict(
                    "observer configuration CAS revision mismatch"
                )
            previous = state.get("binding") if isinstance(state.get("binding"), dict) else None
            generation = int(previous.get("binding_generation") or 0) + 1 if previous else 1
            state["schema_version"] = SCHEMA_VERSION
            state["binding"] = {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "status": "disabled",
                "user_id": user_id,
                "session_id": session_id,
                "workspace_digest": "0" * 64,
                "repo_id": "disabled",
                "origin_digest": "0" * 64,
                "origin_host": "",
                "full_ref": "refs/heads/disabled",
                "goal_id": goal_id,
                "goal_digest": "0" * 64,
                "goal_revision": "",
                "goal_priority": 0.0,
                "ci_binding": None,
                "binding_generation": generation,
            }
            state["baseline"] = None
            state["last_observation"] = None
            state["active_change"] = None
            state["pending_delivery"] = None
            state["last_run"] = None
            result.update(
                status="configured",
                mode="disabled",
                binding_generation=generation,
                state_revision=current_revision + 1,
            )
            return state

        self.state_store.mutate_json(STATE_FILE, update)
        return {**result, "authority": self.authority()}

    def status(self) -> dict[str, Any]:
        try:
            state = self._read_state()
        except TrustedWorkspaceObserverConflict as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "degraded",
                "mode": "disabled",
                "binding_count": 0,
                "reason": self._reason(exc),
                "authority": self.authority(),
            }
        binding = state.get("binding") if isinstance(state.get("binding"), dict) else None
        last_run = state.get("last_run") if isinstance(state.get("last_run"), dict) else None
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "configured" if binding else "not_configured",
            "mode": str(binding.get("status") or "disabled") if binding else "disabled",
            "binding_count": 1 if binding else 0,
            "binding_generation": int(binding.get("binding_generation") or 0) if binding else 0,
            "repository_binding_present": bool(
                binding
                and str(binding.get("status") or "") == "record_only"
                and binding.get("repo_id")
                and binding.get("full_ref")
                and binding.get("origin_digest")
            ),
            "ci_configured": bool(binding and isinstance(binding.get("ci_binding"), dict)),
            "baseline_present": bool(state.get("baseline")),
            "last_run": self._public_run(last_run),
            "authority": self.authority(),
        }

    def bind_ci_policy(
        self,
        *,
        repo_id: str,
        workflow_path: str,
        required_jobs: list[str],
        expected_app_id: int,
    ) -> dict[str, Any]:
        """Bind only through the provider's canonical GitHub contract."""

        if self.ci_provider is None:
            raise TrustedWorkspaceObserverConflict("CI provider unavailable")
        binder = getattr(self.ci_provider, "bind_policy", None)
        if not callable(binder):
            raise TrustedWorkspaceObserverConflict("CI provider cannot bind policy")
        try:
            policy = binder(
                repo_id=repo_id,
                workflow_path=workflow_path,
                required_jobs=list(required_jobs),
                expected_app_id=expected_app_id,
            )
        except Exception as exc:
            raise TrustedWorkspaceObserverConflict("CI binding failed closed") from exc
        return self._validate_ci_binding(policy)

    def authorize_control_token(self, supplied_token: str) -> None:
        self._authorize(supplied_token)

    # ------------------------------------------------------------------
    # Scheduled producer
    # ------------------------------------------------------------------
    def run_once(self, *, reason: str = "manual") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"status": "busy", "events_published": 0, "reason": "busy"}
        try:
            try:
                return self._run_once(reason=reason)
            except TrustedWorkspaceObserverConflict as exc:
                # A binding generation change between probe and persistence
                # invalidates the whole run.  Do not write telemetry against
                # the new generation and do not expose partial events as a
                # successful observation.
                return {
                    "status": "degraded",
                    "events_published": 0,
                    "reason": "observer_binding_race",
                    "error_type": type(exc).__name__,
                    "authority": self.authority(),
                }
        finally:
            self._lock.release()

    def _run_once(self, *, reason: str) -> dict[str, Any]:
        state = self._read_state()
        binding = state.get("binding") if isinstance(state.get("binding"), dict) else None
        if not binding:
            return {"status": "not_configured", "events_published": 0, "reason": "binding_missing"}
        mode = str(binding.get("status") or "disabled")
        if mode == "disabled":
            return {"status": "disabled", "events_published": 0, "reason": "observer_disabled"}
        generation = int(binding.get("binding_generation") or 0)
        try:
            # Resolve the workspace exactly once.  The resulting path is
            # pinned for the complete run and is passed to the publisher;
            # later local-world edits cannot redirect this observation.
            workspace = self._workspace_from_local_world()
            snapshot = self._capture(workspace)
            self._validate_binding(binding, workspace, snapshot)
            goal = self._active_goal(
                user_id=str(binding["user_id"]),
                workspace_id=workspace,
                goal_id=str(binding["goal_id"]),
            )
            if (
                goal is None
                or float(goal.get("priority")) != float(binding.get("goal_priority"))
                or str(goal.get("goal_digest") or "") != str(binding.get("goal_digest") or "")
                or str(goal.get("goal_revision") or "") != str(binding.get("goal_revision") or "")
            ):
                raise TrustedWorkspaceObserverConflict("active Goal binding changed")
        except (GitSnapshotUnavailable, TrustedWorkspaceObserverError, TypeError, ValueError) as exc:
            result = {
                "status": "degraded",
                "events_published": 0,
                "reason": self._reason(exc),
                "binding_generation": int(binding.get("binding_generation") or 0),
            }
            self._record_run(result, expected_generation=generation)
            return result

        now = self._now()
        self._assert_generation(generation)
        baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else None
        active = state.get("active_change") if isinstance(state.get("active_change"), dict) else None
        pending_before = state.get("pending_delivery")
        events: list[dict[str, Any]] = []

        # A prepared transition is an immutable outbox item.  Recover it
        # before deriving a subject or classifying the current snapshot; the
        # current workspace is intentionally handled by the next tick.
        if pending_before is not None:
            events = self._recover_pending(
                binding=binding,
                workspace=workspace,
                now=now,
            )
            current_subject = self._subject(snapshot, None)
            result = self._summary(
                "success",
                reason,
                snapshot,
                events,
                "pending_delivery_recovered"
                if self._events_succeeded(events)
                else "pending_delivery_retry",
            )
            result["binding_generation"] = generation
            try:
                self._persist_observation(
                    snapshot,
                    current_subject,
                    None,
                    update_baseline=False,
                    active=active,
                    clear_pending=self._events_succeeded(events),
                    expected_generation=generation,
                )
                self._record_run(result, expected_generation=generation)
            except TrustedWorkspaceObserverConflict:
                # Ingress may already have durably accepted one event before
                # a concurrent configure changed generation.  Do not erase
                # that evidence from the returned result or report zero.
                if result.get("events_published", 0):
                    result.update(
                        status="degraded",
                        reason="published_before_binding_change",
                    )
                    return result
                raise
            return result

        try:
            ci_fact = self._ci_fact(binding, snapshot)
        except (GitSnapshotUnavailable, TrustedWorkspaceObserverError, TypeError, ValueError) as exc:
            result = {
                "status": "degraded",
                "events_published": 0,
                "reason": self._reason(exc),
                "binding_generation": int(binding.get("binding_generation") or 0),
            }
            self._record_run(result, expected_generation=generation)
            return result

        subject = self._subject(snapshot, ci_fact)

        # Configure persists a clean baseline.  Keep this fallback only for
        # legacy state created before the configure-time baseline contract;
        # it remains fail-closed for dirty/CI-unknown probes.
        if baseline is None:
            if snapshot.dirty:
                result = self._summary(
                    "degraded", reason, snapshot, events,
                    "initial_dirty_not_baselined",
                )
                result["binding_generation"] = generation
                self._record_run(result, expected_generation=generation)
                return result
            if isinstance(binding.get("ci_binding"), dict):
                if ci_fact is None:
                    result = self._summary(
                        "degraded", reason, snapshot, events,
                        "initial_ci_unknown",
                    )
                    result["binding_generation"] = generation
                    self._record_run(result, expected_generation=generation)
                    return result
                if ci_fact.get("signal_state") != "clear":
                    result = self._summary(
                        "degraded", reason, snapshot, events,
                        "initial_ci_failure_not_baselined",
                    )
                    result["binding_generation"] = generation
                    self._record_run(result, expected_generation=generation)
                    return result
            self._persist_observation(
                snapshot,
                subject,
                ci_fact,
                update_baseline=True,
                expected_generation=generation,
            )
            result = self._summary("success", reason, snapshot, events, "baseline_established")
            result["binding_generation"] = generation
            self._record_run(result, expected_generation=generation)
            return result

        docs_only = snapshot.dirty and self._docs_only(snapshot)
        non_doc_dirty = snapshot.dirty and not docs_only
        clean_new_revision = (
            not snapshot.dirty
            and str(snapshot.revision) != str(baseline.get("revision") or "")
        )
        ci_transition = bool(
            isinstance(binding.get("ci_binding"), dict)
            and not snapshot.dirty
            and str(snapshot.revision) == str(baseline.get("revision") or "")
            and ci_fact is not None
            and (
                str(ci_fact.get("signal_state") or "")
                != str(baseline.get("ci_signal_state") or "")
                or str(ci_fact.get("probe_digest") or "")
                != str(baseline.get("ci_probe_digest") or "")
            )
        )
        update_baseline = False
        degraded_decision = False

        if docs_only:
            result_reason = "docs_only_silent"
        elif (
            isinstance(binding.get("ci_binding"), dict)
            and ci_fact is None
            and not clean_new_revision
        ):
            # CI failures are not allowed to collapse into an unchanged
            # observation.  Do not persist a new baseline or active state.
            result = self._summary(
                "degraded", reason, snapshot, events, "ci_probe_unknown"
            )
            result["binding_generation"] = generation
            self._record_run(result, expected_generation=generation)
            return result
        elif ci_transition:
            if ci_fact and ci_fact.get("signal_state") == "present":
                events.extend(
                    self._publish_events(
                        binding=binding,
                        workspace=workspace,
                        snapshot=snapshot,
                        ci_fact=ci_fact,
                        now=now,
                        subject=subject,
                        kinds=("change_signal", "risk_signal"),
                        receipt_suffix="clean-failure",
                    )
                )
                if self._events_succeeded(events):
                    result_reason = "ci_rerun_failure"
                    active = None
                    update_baseline = True
                else:
                    result_reason = "ci_rerun_ingress_failed"
            elif ci_fact and ci_fact.get("signal_state") == "clear":
                result_reason = "ci_rerun_success_silent"
                active = None
                update_baseline = True
            else:
                result_reason = "ci_rerun_unknown"
        elif clean_new_revision:
            if not isinstance(binding.get("ci_binding"), dict):
                result_reason = "clean_revision_silent"
                active = None
                update_baseline = True
            elif ci_fact and ci_fact.get("signal_state") == "present":
                events.extend(
                    self._publish_events(
                        binding=binding,
                        workspace=workspace,
                        snapshot=snapshot,
                        ci_fact=ci_fact,
                        now=now,
                        subject=subject,
                        kinds=("change_signal", "risk_signal"),
                        receipt_suffix="clean-failure",
                    )
                )
                if self._events_succeeded(events):
                    result_reason = "clean_revision_ci_failure"
                    active = None
                    update_baseline = True
                else:
                    result_reason = "clean_revision_ci_ingress_failed"
            elif ci_fact and ci_fact.get("signal_state") == "clear":
                result_reason = "clean_revision_ci_success_silent"
                active = None
                update_baseline = True
            else:
                result_reason = "clean_revision_ci_unknown"
                degraded_decision = True
                update_baseline = False
        elif non_doc_dirty:
            same_subject = bool(active and str(active.get("subject") or "") == subject)
            if not same_subject:
                events.extend(
                    self._publish_events(
                        binding=binding,
                        workspace=workspace,
                        snapshot=snapshot,
                        ci_fact=ci_fact,
                        now=now,
                        subject=subject,
                        kinds=("change_signal",),
                        receipt_suffix="dirty-change",
                    )
                )
                if self._events_succeeded(events):
                    active = {
                        "subject": subject,
                        "changed_at": canonical_utc(now),
                        "risk_emitted": False,
                    }
                    result_reason = "dirty_change_signal"
                else:
                    result_reason = "dirty_change_ingress_failed"
            elif active and not bool(active.get("risk_emitted")) and self._elapsed(active, now) >= self.grace_seconds:
                events.extend(
                    self._publish_events(
                        binding=binding,
                        workspace=workspace,
                        snapshot=snapshot,
                        ci_fact=ci_fact,
                        now=now,
                        subject=subject,
                        kinds=("risk_signal",),
                        receipt_suffix="dirty-validation-gap",
                    )
                )
                if self._events_succeeded(events):
                    active["risk_emitted"] = True
                    result_reason = "dirty_validation_gap_after_grace"
                else:
                    result_reason = "dirty_risk_ingress_failed"
            else:
                result_reason = "dirty_grace_or_duplicate"
            update_baseline = False
        else:
            result_reason = "unchanged_silent"
            active = None
            update_baseline = False

        result = self._summary(
            "degraded" if degraded_decision else "success",
            reason,
            snapshot,
            events,
            result_reason,
        )
        result["binding_generation"] = generation
        try:
            self._persist_observation(
                snapshot,
                subject,
                ci_fact,
                update_baseline=update_baseline,
                active=active,
                clear_pending=(self._events_succeeded(events) or (not events and pending_before is None)),
                expected_generation=generation,
            )
            self._record_run(result, expected_generation=generation)
        except TrustedWorkspaceObserverConflict:
            if result.get("events_published", 0):
                result.update(status="degraded", reason="published_before_binding_change")
                return result
            raise
        return result

    # ------------------------------------------------------------------
    # Typed event production
    # ------------------------------------------------------------------
    def _publish_events(
        self,
        *,
        binding: dict[str, Any],
        workspace: str,
        snapshot: GitWorkspaceObservation,
        ci_fact: dict[str, Any] | None,
        now: datetime,
        subject: str,
        kinds: tuple[str, ...],
        receipt_suffix: str,
    ) -> list[dict[str, Any]]:
        from runtime.trusted_workspace_observer_delivery import publish_events

        return publish_events(
            self,
            binding=binding,
            workspace=workspace,
            snapshot=snapshot,
            ci_fact=ci_fact,
            now=now,
            subject=subject,
            kinds=kinds,
            receipt_suffix=receipt_suffix,
        )

    def _recover_pending(
        self,
        *,
        binding: dict[str, Any],
        workspace: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        from runtime.trusted_workspace_observer_delivery import recover_pending

        return recover_pending(
            self,
            binding=binding,
            workspace=workspace,
            now=now,
        )

    # ------------------------------------------------------------------
    # Binding, CI, persistence and helpers
    # ------------------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        state = self.state_store.read_json(STATE_FILE)
        self._validate_state(state)
        return state

    def _validate_state(self, state: dict[str, Any]) -> None:
        try:
            validate_state(state)
        except StateValidationError as exc:
            raise TrustedWorkspaceObserverConflict(str(exc)) from exc

    def _capture(self, workspace: str) -> GitWorkspaceObservation:
        return self._snapshotter(Path(workspace))

    def _validate_binding(self, binding: dict[str, Any], workspace: str, snapshot: GitWorkspaceObservation) -> None:
        checks = {
            "workspace_digest": self._workspace_digest(workspace),
            "repo_id": snapshot.repo_id,
            "origin_digest": snapshot.origin_digest,
            "origin_host": str(snapshot.origin_host or ""),
            "full_ref": snapshot.full_ref,
        }
        if any(str(binding.get(key) or "") != str(value) for key, value in checks.items()):
            raise TrustedWorkspaceObserverConflict("workspace binding drifted")
        if isinstance(binding.get("ci_binding"), dict):
            if str(snapshot.origin_host or "") != "github.com":
                raise TrustedWorkspaceObserverConflict("CI workspace origin is not canonical GitHub")
            if str(binding["ci_binding"].get("repo_id") or "").lower() != snapshot.repo_id.lower():
                raise TrustedWorkspaceObserverConflict("CI repository binding drifted")

    def _assert_run_binding(
        self,
        *,
        binding: dict[str, Any],
        workspace: str,
        expected_generation: int,
    ) -> None:
        self._assert_generation(expected_generation)
        local = self.state_store.read_json("local_world.json")
        if self._workspace(str(local.get("current_project") or "")) != workspace:
            raise TrustedWorkspaceObserverConflict("workspace changed during run")
        goal = self._active_goal(
            user_id=str(binding["user_id"]),
            workspace_id=workspace,
            goal_id=str(binding["goal_id"]),
        )
        if (
            goal is None
            or float(goal.get("priority")) != float(binding.get("goal_priority"))
            or str(goal.get("goal_digest") or "") != str(binding.get("goal_digest") or "")
            or str(goal.get("goal_revision") or "") != str(binding.get("goal_revision") or "")
        ):
            raise TrustedWorkspaceObserverConflict("active Goal binding changed during run")

    def _ci_fact(self, binding: dict[str, Any], snapshot: GitWorkspaceObservation) -> dict[str, Any] | None:
        policy = binding.get("ci_binding")
        if not isinstance(policy, dict):
            return None
        if self.ci_provider is None:
            raise TrustedWorkspaceObserverConflict("CI provider unavailable")
        try:
            fact = self.ci_provider.observe(
                binding=policy,
                target_ref=snapshot.full_ref,
                target_sha=snapshot.revision,
                now=self._now(),
            )
        except Exception:
            return None
        if not isinstance(fact, dict):
            return None
        if (
            str(fact.get("repo_id") or "").lower() != snapshot.repo_id.lower()
            or str(fact.get("target_ref") or "") != snapshot.full_ref
            or str(fact.get("target_sha") or "").lower() != snapshot.revision.lower()
            or str(fact.get("signal_state") or "") not in {"present", "clear"}
        ):
            return None
        probe_digest = str(fact.get("probe_digest") or "").lower()
        if not _DIGEST_RE.fullmatch(probe_digest):
            return None
        return {
            "signal_state": str(fact["signal_state"]),
            "repo_id": snapshot.repo_id,
            "target_ref": snapshot.full_ref,
            "target_sha": snapshot.revision,
            "probe_digest": probe_digest,
            **{
                key: fact[key]
                for key in ("run_id", "run_number", "run_attempt", "check_suite_id", "policy_digest")
                if key in fact
            },
        }

    def _validate_ci_binding(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TrustedWorkspaceObserverConflict("CI binding must be an object")
        validator = getattr(self.ci_provider, "_validated_binding", None)
        if not callable(validator):
            validator = getattr(self.ci_provider, "validate_binding", None)
        if not callable(validator):
            raise TrustedWorkspaceObserverConflict("CI provider has no binding validator")
        try:
            validated = validator(value)
        except Exception as exc:
            raise TrustedWorkspaceObserverConflict("CI binding is invalid") from exc
        if not isinstance(validated, dict):
            raise TrustedWorkspaceObserverConflict("CI binding is invalid")
        if str(validated.get("api_origin") or "") != "https://api.github.com":
            raise TrustedWorkspaceObserverConflict("CI provider origin is not canonical GitHub")
        return copy.deepcopy(validated)

    def _active_goal(self, *, user_id: str, workspace_id: str, goal_id: str) -> dict[str, Any] | None:
        state = self.state_store.read_json("user_goals.json")
        goals = state.get("goals") if isinstance(state.get("goals"), list) else []
        matches: list[dict[str, Any]] = []
        for raw in goals:
            if not isinstance(raw, dict) or str(raw.get("goal_id") or "") != goal_id:
                continue
            scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
            bound_workspace = str(raw.get("workspace_id") or scope.get("workspace_id") or "")
            priority = raw.get("goal_priority", raw.get("priority"))
            if (
                str(raw.get("user_id") or "") == user_id
                and str(raw.get("status") or "").lower() == "active"
                and bound_workspace == workspace_id
                and isinstance(priority, (int, float))
                and not isinstance(priority, bool)
                and 0.0 <= float(priority) <= 1.0
            ):
                goal_copy = copy.deepcopy(raw)
                matches.append(
                    {
                        **goal_copy,
                        "priority": float(priority),
                        "goal_digest": _digest(goal_copy),
                        "goal_revision": str(goal_copy.get("revision") or ""),
                    }
                )
        return matches[0] if len(matches) == 1 else None

    def _persist_observation(
        self,
        snapshot: GitWorkspaceObservation,
        subject: str,
        ci_fact: dict[str, Any] | None,
        *,
        update_baseline: bool,
        expected_generation: int,
        active: dict[str, Any] | None = None,
        clear_pending: bool = True,
    ) -> None:
        record = self._observation_record(snapshot, subject, ci_fact)
        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            current = state.get("binding") if isinstance(state.get("binding"), dict) else None
            if not current or int(current.get("binding_generation") or 0) != expected_generation:
                raise TrustedWorkspaceObserverConflict("observer binding changed during run")
            state["schema_version"] = SCHEMA_VERSION
            state["last_observation"] = copy.deepcopy(record)
            if update_baseline:
                state["baseline"] = copy.deepcopy(record)
            state["active_change"] = copy.deepcopy(active)
            # Delivery is cleared only with the observer state transition.
            # If the process dies after ingress but before this mutation, the
            # exact prepared command remains available for replay.
            if clear_pending:
                state["pending_delivery"] = None
            return state
        self.state_store.mutate_json(STATE_FILE, update)

    def _record_run(self, result: dict[str, Any], *, expected_generation: int) -> None:
        public = self._public_run(result)
        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            current = state.get("binding") if isinstance(state.get("binding"), dict) else None
            if not current or int(current.get("binding_generation") or 0) != expected_generation:
                raise TrustedWorkspaceObserverConflict("observer binding changed during run")
            state["last_run"] = public
            return state
        self.state_store.mutate_json(STATE_FILE, update)

    def _assert_generation(self, expected_generation: int) -> None:
        state = self.state_store.read_json(STATE_FILE)
        self._validate_state(state)
        binding = state.get("binding") if isinstance(state.get("binding"), dict) else None
        if not binding or int(binding.get("binding_generation") or 0) != expected_generation:
            raise TrustedWorkspaceObserverConflict("observer binding changed during run")

    def _observation_record(self, snapshot: GitWorkspaceObservation, subject: str, ci_fact: dict[str, Any] | None) -> dict[str, Any]:
        return observation_record(snapshot, subject, ci_fact)

    def _summary(self, status: str, reason: str, snapshot: GitWorkspaceObservation, events: list[dict[str, Any]], decision: str) -> dict[str, Any]:
        published = [item for item in events if str(item.get("status") or "") in {"recorded", "enqueued", "replayed"}]
        race = any(
            isinstance(item, dict)
            and str(item.get("post_publish_reason") or "")
            == "published_before_binding_change"
            for item in events
        )
        if race:
            status = "degraded"
            decision = "published_before_binding_change"
        return {
            "status": status if len(published) == len(events) else "degraded",
            "reason": decision,
            "trigger": reason,
            "events_published": len(published),
            "events_attempted": len(events),
            "durable_events_published": len(published) if race else 0,
            "category_counts": dict(snapshot.category_counts),
            "dirty": bool(snapshot.dirty),
            "authority": self.authority(),
        }

    @staticmethod
    def _events_succeeded(events: list[dict[str, Any]]) -> bool:
        return bool(events) and all(
            isinstance(item, dict)
            and str(item.get("status") or "") in {"recorded", "enqueued", "replayed"}
            and item.get("post_publish_reason") is None
            for item in events
        )

    @staticmethod
    def _public_run(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            key: copy.deepcopy(value[key])
            for key in (
                "status", "reason", "events_published", "events_attempted",
                "category_counts", "dirty", "binding_generation",
            )
            if key in value
        }

    @staticmethod
    def _docs_only(snapshot: GitWorkspaceObservation) -> bool:
        counts = snapshot.category_counts
        return bool(counts.get("docs")) and not any(
            counts.get(key) for key in ("code", "test", "config", "other")
        )

    @staticmethod
    def _subject(snapshot: GitWorkspaceObservation, ci_fact: dict[str, Any] | None) -> str:
        return _digest(
            {
                "revision": snapshot.revision,
                "manifest_digest": snapshot.manifest_digest,
                "dirty": snapshot.dirty,
                "ci_probe_digest": ci_fact.get("probe_digest") if ci_fact else None,
            }
        )[:48]

    @staticmethod
    def _elapsed(active: dict[str, Any], now: datetime) -> float:
        try:
            raw = str(active.get("changed_at") or "")
            if len(raw) > 40:
                return 0.0
            selected = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if selected.tzinfo is None:
                selected = selected.replace(tzinfo=timezone.utc)
            return max(0.0, (now - selected.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def _event_inbox_revision(self) -> int:
        inbox = self.state_store.read_json("event_inbox.json")
        raw = inbox.get("_state_revision") if isinstance(inbox, dict) else 0
        return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0

    def _workspace_from_local_world(self) -> str:
        local = self.state_store.read_json("local_world.json")
        return self._workspace(str(local.get("current_project") or ""))

    @staticmethod
    def _workspace(value: str) -> str:
        selected = str(value or "").strip()
        if not selected or "\x00" in selected:
            raise TrustedWorkspaceObserverConflict("workspace is unavailable")
        try:
            path = Path(selected).expanduser().resolve(strict=True)
        except OSError as exc:
            raise TrustedWorkspaceObserverConflict("workspace is unavailable") from exc
        if not path.is_dir():
            raise TrustedWorkspaceObserverConflict("workspace must be a directory")
        return str(path)

    @staticmethod
    def _workspace_digest(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _scope(value: str, name: str) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > 240 or any(char in selected for char in ("\x00", "\r", "\n")):
            raise TrustedWorkspaceObserverConflict(f"{name} is invalid")
        return selected

    def _authorize(self, supplied: str) -> None:
        if not self.control_token:
            raise TrustedWorkspaceObserverUnauthorized("local control token unavailable")
        if not supplied or not hmac.compare_digest(str(supplied), self.control_token):
            raise TrustedWorkspaceObserverUnauthorized("local control token invalid")

    @staticmethod
    def _state_revision(state: dict[str, Any]) -> int:
        return state_revision(state)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, GitSnapshotUnavailable):
            return f"git_snapshot_{exc.reason_code}"
        if isinstance(exc, TrustedWorkspaceObserverConflict):
            return str(exc)[:120]
        return "observer_probe_failed"

    @staticmethod
    def authority() -> dict[str, bool]:
        return {
            "route_change": False,
            "agent_dispatch": False,
            "tool_call": False,
            "capability_grant": False,
            "external_delivery": False,
            "execution": False,
            "fact_certification": False,
        }


__all__ = ["TrustedWorkspaceObserver", "TrustedWorkspaceObserverError", "TrustedWorkspaceObserverConflict", "TrustedWorkspaceObserverUnauthorized"]
