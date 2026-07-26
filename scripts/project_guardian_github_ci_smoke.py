#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore
from interface.event_normalizer import EventNormalizer
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
from runtime.project_guardian_github_ci import (
    GitHubActionsCIProvider,
    GitHubCIContractError,
    GitHubCIUnknown,
)
from runtime.project_guardian_producers import (
    ProjectGuardianGoalConflict,
    ProjectGuardianProducerRuntime,
)
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from scripts.event_driven_awareness_smoke import (
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)
from scripts.project_guardian_producer_smoke import (
    create_git_fixture,
    frontier_signals,
    git,
    git_snapshot,
)
from scripts.project_guardian_smoke import configure_mode


NOW = datetime.now(timezone.utc).replace(microsecond=0)
REPO_ID = "wenjiesong04/veyra"
TARGET_REF = "refs/heads/main"
WORKFLOW_PATH = ".github/workflows/ci.yml"
REPOSITORY_ID = 424_242
WORKFLOW_ID = 919
APP_ID = 15_368
REQUIRED_JOBS = ["frontend", "gate"]


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    error_types: type[BaseException] | tuple[type[BaseException], ...],
    label: str,
    action: Callable[[], Any],
) -> BaseException:
    try:
        action()
    except error_types as exc:
        print(f"PASS {label}")
        return exc
    except Exception as exc:
        raise AssertionError(
            f"{label}: unexpected {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"{label}: expected an exception")


class StrictFakeGitHubAPI:
    """A closed fake that rejects every path or query outside the CI contract."""

    def __init__(self, target_sha: str) -> None:
        self.target_sha = target_sha.lower()
        self.repository_id = REPOSITORY_ID
        self.repository_full_name = REPO_ID
        self.workflow_id = WORKFLOW_ID
        self.workflow_path = WORKFLOW_PATH
        self.workflow_state = "active"

        self.run_id = 8_001
        self.run_number = 51
        self.run_attempt = 1
        self.run_check_suite_id = 9_001
        self.run_repository_id = REPOSITORY_ID
        self.run_repository_name = REPO_ID
        self.run_head_repository_id = REPOSITORY_ID
        self.run_head_repository_name = REPO_ID
        self.run_head_branch = "main"
        self.run_head_sha = self.target_sha
        self.run_workflow_id = WORKFLOW_ID
        self.run_path = WORKFLOW_PATH
        self.run_status = "completed"
        self.run_conclusion = "failure"
        self.run_updated_at = NOW - timedelta(minutes=2)
        self.include_prior_failure = False
        self.reread_attempt: int | None = None

        self.job_run_id = self.run_id
        self.job_head_branch = "main"
        self.job_head_sha = self.target_sha
        self.job_status = "completed"
        self.job_completed_at = NOW - timedelta(minutes=2)
        self.job_name_override: str | None = None
        self.job_conclusions: dict[str, str] | None = None
        self.check_name_override: str | None = None
        self.check_app_id = APP_ID
        self.check_suite_id = self.run_check_suite_id
        self.runs_total_count_override: int | None = None
        self.jobs_total_count_override: int | None = None
        self.inject_newer_run_on_final_list = False
        self.include_newer_partial_run = False
        self._runs_read_count = 0
        self.calls: list[tuple[str, dict[str, str]]] = []

    def set_pending_rerun(self) -> None:
        self.include_prior_failure = True
        self.run_id = 8_002
        self.run_attempt = 2
        self.run_check_suite_id = 9_002
        self.run_status = "in_progress"
        self.run_conclusion = ""
        self.run_updated_at = NOW - timedelta(seconds=45)
        self.job_run_id = self.run_id
        self.check_suite_id = self.run_check_suite_id

    def set_successful_rerun(self) -> None:
        self.include_prior_failure = True
        self.run_id = 8_002
        self.run_attempt = 2
        self.run_check_suite_id = 9_002
        self.run_status = "completed"
        self.run_conclusion = "success"
        self.run_updated_at = NOW - timedelta(seconds=30)
        self.job_run_id = self.run_id
        self.job_status = "completed"
        self.job_completed_at = NOW - timedelta(seconds=30)
        self.check_suite_id = self.run_check_suite_id

    def __call__(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        selected_params = dict(params)
        self.calls.append((path, selected_params))
        prefix = f"/repos/{REPO_ID}"
        workflow_lookup = (
            f"{prefix}/actions/workflows/"
            f"{quote(WORKFLOW_PATH, safe='')}"
        )
        runs_path = (
            f"{prefix}/actions/workflows/{WORKFLOW_ID}/runs"
        )
        jobs_path = (
            f"{prefix}/actions/runs/{self.run_id}/attempts/"
            f"{self.run_attempt}/jobs"
        )
        checks_path = f"{prefix}/commits/{self.target_sha}/check-runs"
        reread_path = f"{prefix}/actions/runs/{self.run_id}"

        if path == prefix:
            self._require_params(path, selected_params, {})
            return {
                "id": self.repository_id,
                "full_name": self.repository_full_name,
            }
        if path == workflow_lookup:
            self._require_params(path, selected_params, {})
            return {
                "id": self.workflow_id,
                "path": self.workflow_path,
                "state": self.workflow_state,
            }
        if path == runs_path:
            self._require_params(
                path,
                selected_params,
                {
                    "branch": "main",
                    "event": "push",
                    "head_sha": self.target_sha,
                    "per_page": str(GitHubActionsCIProvider.MAX_RUNS),
                },
            )
            self._runs_read_count += 1
            runs = [self._run_payload()]
            if self.include_prior_failure:
                runs.insert(0, self._prior_failure_run())
            if (
                self.inject_newer_run_on_final_list
                and self._runs_read_count >= 2
            ):
                runs.append(self._newer_completed_run())
            if self.include_newer_partial_run:
                runs.append(
                    {
                        "id": self.run_id + 1,
                        "run_number": self.run_number + 1,
                        "run_attempt": 1,
                        "workflow_id": WORKFLOW_ID,
                        "event": "push",
                        "head_branch": "main",
                        "head_sha": self.target_sha,
                    }
                )
            return {
                "total_count": (
                    self.runs_total_count_override
                    if self.runs_total_count_override is not None
                    else len(runs)
                ),
                "workflow_runs": runs,
            }
        if path == jobs_path:
            self._require_params(
                path,
                selected_params,
                {"per_page": str(GitHubActionsCIProvider.MAX_JOBS)},
            )
            jobs = self._jobs()
            return {
                "total_count": (
                    self.jobs_total_count_override
                    if self.jobs_total_count_override is not None
                    else len(jobs)
                ),
                "jobs": jobs,
            }
        if path == checks_path:
            self._require_params(
                path,
                selected_params,
                {
                    "app_id": str(APP_ID),
                    "filter": "all",
                    "per_page": "100",
                },
            )
            return {"check_runs": self._checks()}
        if path == reread_path:
            self._require_params(path, selected_params, {})
            return self._run_payload(reread=True)
        raise AssertionError(f"unexpected GitHub API path: {path}")

    @staticmethod
    def _require_params(
        path: str,
        actual: dict[str, str],
        expected: dict[str, str],
    ) -> None:
        if actual != expected:
            raise AssertionError(
                f"unexpected GitHub API params for {path}: "
                f"{actual!r} != {expected!r}"
            )

    def _run_payload(self, *, reread: bool = False) -> dict[str, Any]:
        attempt = (
            self.reread_attempt
            if reread and self.reread_attempt is not None
            else self.run_attempt
        )
        return {
            "id": self.run_id,
            "run_number": self.run_number,
            "run_attempt": attempt,
            "check_suite_id": self.run_check_suite_id,
            "workflow_id": self.run_workflow_id,
            "path": f"{self.run_path}@refs/heads/main",
            "event": "push",
            "head_branch": self.run_head_branch,
            "head_sha": self.run_head_sha,
            "repository": {
                "id": self.run_repository_id,
                "full_name": self.run_repository_name,
            },
            "head_repository": {
                "id": self.run_head_repository_id,
                "full_name": self.run_head_repository_name,
            },
            "status": self.run_status,
            "conclusion": self.run_conclusion,
            "updated_at": self.run_updated_at.isoformat(),
        }

    def _prior_failure_run(self) -> dict[str, Any]:
        return {
            "id": 8_001,
            "run_number": self.run_number,
            "run_attempt": 1,
            "check_suite_id": 9_001,
            "workflow_id": WORKFLOW_ID,
            "path": f"{WORKFLOW_PATH}@refs/heads/main",
            "event": "push",
            "head_branch": "main",
            "head_sha": self.target_sha,
            "repository": {
                "id": REPOSITORY_ID,
                "full_name": REPO_ID,
            },
            "head_repository": {
                "id": REPOSITORY_ID,
                "full_name": REPO_ID,
            },
            "status": "completed",
            "conclusion": "failure",
            "updated_at": (NOW - timedelta(minutes=2)).isoformat(),
        }

    def _newer_completed_run(self) -> dict[str, Any]:
        return {
            "id": self.run_id + 1,
            "run_number": self.run_number,
            "run_attempt": self.run_attempt + 1,
            "check_suite_id": self.run_check_suite_id + 1,
            "workflow_id": WORKFLOW_ID,
            "path": f"{WORKFLOW_PATH}@refs/heads/main",
            "event": "push",
            "head_branch": "main",
            "head_sha": self.target_sha,
            "repository": {
                "id": REPOSITORY_ID,
                "full_name": REPO_ID,
            },
            "head_repository": {
                "id": REPOSITORY_ID,
                "full_name": REPO_ID,
            },
            "status": "completed",
            "conclusion": "success",
            "updated_at": (NOW - timedelta(seconds=15)).isoformat(),
        }

    def _jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for index, canonical_name in enumerate(REQUIRED_JOBS, start=1):
            name = (
                self.job_name_override
                if index == 1 and self.job_name_override is not None
                else canonical_name
            )
            conclusion = self._job_conclusion(canonical_name)
            completed_at = self.job_completed_at - timedelta(
                seconds=int(canonical_name == "frontend") * 5
            )
            jobs.append(
                {
                    "id": self.run_id * 10 + index,
                    "name": name,
                    "run_id": self.job_run_id,
                    "head_branch": self.job_head_branch,
                    "head_sha": self.job_head_sha,
                    "status": self.job_status,
                    "conclusion": conclusion,
                    "completed_at": completed_at.isoformat(),
                    "check_run_url": (
                        f"https://api.github.com/repos/{REPO_ID}/check-runs/"
                        f"{self.run_id * 100 + index}"
                    ),
                }
            )
        return jobs

    def _checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for index, canonical_name in enumerate(REQUIRED_JOBS, start=1):
            name = (
                self.check_name_override
                if index == 1 and self.check_name_override is not None
                else (
                    self.job_name_override
                    if index == 1 and self.job_name_override is not None
                    else canonical_name
                )
            )
            conclusion = self._job_conclusion(canonical_name)
            checks.append(
                {
                    "id": self.run_id * 100 + index,
                    "name": name,
                    "head_sha": self.job_head_sha,
                    "status": self.job_status,
                    "conclusion": conclusion,
                    "app": {"id": self.check_app_id},
                    "check_suite": {"id": self.check_suite_id},
                }
            )
        return checks

    def _job_conclusion(self, name: str) -> str:
        if self.job_conclusions is not None:
            return str(self.job_conclusions.get(name) or "")
        if name == "gate" and self.run_conclusion == "failure":
            return "failure"
        return self.run_conclusion


class ProviderSpy:
    def __init__(
        self,
        delegate: GitHubActionsCIProvider,
        *,
        observer: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.delegate = delegate
        self.observer = observer
        self.observe_calls: list[dict[str, Any]] = []

    def bind_policy(self, **kwargs: Any) -> dict[str, Any]:
        return self.delegate.bind_policy(**kwargs)

    def observe(self, **kwargs: Any) -> dict[str, Any]:
        self.observe_calls.append(copy.deepcopy(kwargs))
        if self.observer is not None:
            return self.observer(**kwargs)
        return self.delegate.observe(**kwargs)


def provider(fake: StrictFakeGitHubAPI) -> GitHubActionsCIProvider:
    return GitHubActionsCIProvider(
        request_json=fake,
        max_age_seconds=30 * 60,
        max_future_skew_seconds=2 * 60,
    )


def bind(provider_runtime: GitHubActionsCIProvider) -> dict[str, Any]:
    return provider_runtime.bind_policy(
        repo_id=REPO_ID,
        workflow_path=WORKFLOW_PATH,
        required_jobs=["gate", "frontend"],
        expected_app_id=APP_ID,
    )


def create_github_ci_fixture(root: Path) -> Path:
    repo, _ = create_git_fixture(root)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://github.com/{REPO_ID}.git",
    )
    return repo


def register_ci_goal(
    runtime: ProjectGuardianProducerRuntime,
    repo: Path,
    *,
    user_id: str = "user-ci",
    goal_id: str = "goal_release_github_ci",
    active_from: datetime | None = None,
    active_until: datetime | None = None,
    expected_state_revision: int | None = None,
    required_jobs: list[str] | None = None,
) -> dict[str, Any]:
    starts_at = active_from or (NOW - timedelta(hours=1))
    ends_at = active_until or (NOW + timedelta(hours=1))
    return runtime.register_release_goal(
        user_id=user_id,
        workspace_id="workspace-github-ci-smoke",
        repo_id=REPO_ID,
        target_ref=TARGET_REF,
        target_environment="production",
        release_cycle="github_ci_smoke",
        workspace_path=str(repo),
        active_from=starts_at.isoformat(),
        active_until=ends_at.isoformat(),
        goal_id=goal_id,
        expected_state_revision=expected_state_revision,
        github_actions_workflow=WORKFLOW_PATH,
        github_actions_required_jobs=required_jobs or ["gate", "frontend"],
        github_actions_app_id=APP_ID,
    )


def ci_signal(store: WorldStateStore) -> dict[str, Any] | None:
    for signal in frontier_signals(store):
        if str(signal.get("kind") or "") == "ci_failed":
            return signal
    return None


def evaluate(store: WorldStateStore) -> dict[str, Any]:
    return ProjectGuardianEvaluator().evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(
            store
        ).evaluation_state(),
        now=NOW,
    )


def test_binding_failure_state_and_provider_time() -> None:
    target_sha = "a" * 40
    fake = StrictFakeGitHubAPI(target_sha)
    selected_provider = provider(fake)
    binding = bind(selected_provider)
    expected_binding_keys = {
        "schema_version",
        "provider",
        "api_origin",
        "repo_id",
        "repository_id",
        "workflow_id",
        "workflow_path",
        "event",
        "expected_app_id",
        "required_jobs",
        "policy_digest",
    }
    expect(
        set(binding) == expected_binding_keys
        and binding["schema_version"] == selected_provider.CONTRACT
        and binding["provider"] == selected_provider.PROVIDER
        and binding["repo_id"] == REPO_ID
        and binding["repository_id"] == REPOSITORY_ID
        and binding["workflow_id"] == WORKFLOW_ID
        and binding["workflow_path"] == WORKFLOW_PATH
        and binding["required_jobs"] == REQUIRED_JOBS
        and len(binding["policy_digest"]) == 64
        and fake.calls
        == [
            (f"/repos/{REPO_ID}", {}),
            (
                f"/repos/{REPO_ID}/actions/workflows/"
                f"{quote(WORKFLOW_PATH, safe='')}",
                {},
            ),
        ],
        "binding pins the repository, workflow, app, and sorted required jobs",
        {"binding": binding, "calls": fake.calls},
    )

    failure = selected_provider.observe(
        binding=binding,
        target_ref=TARGET_REF,
        target_sha=target_sha,
        now=NOW,
    )
    later_poll = selected_provider.observe(
        binding=binding,
        target_ref=TARGET_REF,
        target_sha=target_sha,
        now=NOW + timedelta(minutes=5),
    )
    expected_provider_time = fake.job_completed_at.isoformat()
    expect(
        failure["signal_state"] == "present"
        and failure["observed_at"] == expected_provider_time
        and later_poll["observed_at"] == expected_provider_time
        and later_poll["probe_digest"] == failure["probe_digest"],
        (
            "a failure is present and repeated polling preserves the provider "
            "completion time instead of refreshing evidence age"
        ),
        {"failure": failure, "later_poll": later_poll},
    )

    bad_repository = StrictFakeGitHubAPI(target_sha)
    bad_repository.repository_full_name = "someone/else"
    expect_raises(
        GitHubCIContractError,
        "binding rejects a repository response with a different identity",
        lambda: bind(provider(bad_repository)),
    )
    fractional_repository = StrictFakeGitHubAPI(target_sha)
    fractional_repository.repository_id = REPOSITORY_ID + 0.9
    expect_raises(
        GitHubCIContractError,
        "binding rejects a fractional JSON repository id",
        lambda: bind(provider(fractional_repository)),
    )
    inactive_workflow = StrictFakeGitHubAPI(target_sha)
    inactive_workflow.workflow_state = "disabled_manually"
    expect_raises(
        GitHubCIContractError,
        "binding rejects an inactive workflow",
        lambda: bind(provider(inactive_workflow)),
    )
    expect_raises(
        GitHubCIContractError,
        "binding rejects repository dot segments",
        lambda: selected_provider.bind_policy(
            repo_id="../veyra",
            workflow_path=WORKFLOW_PATH,
            required_jobs=REQUIRED_JOBS,
            expected_app_id=APP_ID,
        ),
    )
    expect_raises(
        GitHubCIContractError,
        "binding rejects workflow path dot segments",
        lambda: selected_provider.bind_policy(
            repo_id=REPO_ID,
            workflow_path=".github/workflows/../ci.yml",
            required_jobs=REQUIRED_JOBS,
            expected_app_id=APP_ID,
        ),
    )
    path_error = expect_raises(
        GitHubCIUnknown,
        "request boundary rejects API path dot segments before the fake",
        lambda: selected_provider._request(
            f"/repos/{REPO_ID}/actions/../runs",
            {},
        ),
    )
    expect(
        isinstance(path_error, GitHubCIUnknown)
        and path_error.reason == "github_api_path_invalid",
        "API dot-segment rejection has a stable fail-closed reason",
        getattr(path_error, "reason", None),
    )


def test_identity_mismatches_fail_closed() -> None:
    target_sha = "b" * 40
    cases: tuple[
        tuple[str, str, Callable[[StrictFakeGitHubAPI], None]],
        ...,
    ] = (
        (
            "repository",
            "workflow_run_identity_mismatch",
            lambda fake: setattr(
                fake,
                "run_repository_id",
                REPOSITORY_ID + 1,
            ),
        ),
        (
            "fractional repository",
            "workflow_run_identity_mismatch",
            lambda fake: setattr(
                fake,
                "run_repository_id",
                REPOSITORY_ID + 0.9,
            ),
        ),
        (
            "ref",
            "workflow_run_identity_mismatch",
            lambda fake: setattr(fake, "run_head_branch", "release"),
        ),
        (
            "SHA",
            "workflow_run_identity_mismatch",
            lambda fake: setattr(fake, "run_head_sha", "c" * 40),
        ),
        (
            "workflow",
            "workflow_run_identity_mismatch",
            lambda fake: setattr(
                fake,
                "run_path",
                ".github/workflows/other.yml",
            ),
        ),
        (
            "app",
            "required_job_identity_mismatch",
            lambda fake: setattr(fake, "check_app_id", APP_ID + 1),
        ),
        (
            "job",
            "required_job_identity_mismatch",
            lambda fake: setattr(fake, "job_run_id", fake.run_id + 1),
        ),
        (
            "attempt",
            "workflow_run_changed_during_probe",
            lambda fake: setattr(
                fake,
                "reread_attempt",
                fake.run_attempt + 1,
            ),
        ),
    )
    failures: dict[str, Any] = {}
    for label, expected_reason, mutate in cases:
        fake = StrictFakeGitHubAPI(target_sha)
        selected_provider = provider(fake)
        binding = bind(selected_provider)
        mutate(fake)
        try:
            selected_provider.observe(
                binding=binding,
                target_ref=TARGET_REF,
                target_sha=target_sha,
                now=NOW,
            )
        except GitHubCIUnknown as exc:
            if exc.reason != expected_reason:
                failures[label] = {
                    "expected": expected_reason,
                    "actual": exc.reason,
                }
        except Exception as exc:
            failures[label] = {
                "expected": expected_reason,
                "actual": f"{type(exc).__name__}: {exc}",
            }
        else:
            failures[label] = "mismatch incorrectly produced a CI fact"
    expect(
        not failures,
        (
            "repository/ref/SHA/workflow/app/job/attempt mismatches all "
            "fail closed"
        ),
        failures,
    )

    race_fake = StrictFakeGitHubAPI(target_sha)
    race_provider = provider(race_fake)
    race_binding = bind(race_provider)
    race_fake.inject_newer_run_on_final_list = True
    race = expect_raises(
        GitHubCIUnknown,
        "a newer same-SHA run appearing after job reads fails closed",
        lambda: race_provider.observe(
            binding=race_binding,
            target_ref=TARGET_REF,
            target_sha=target_sha,
            now=NOW,
        ),
    )
    expect(
        isinstance(race, GitHubCIUnknown)
        and race.reason == "workflow_run_changed_during_probe"
        and race_fake._runs_read_count == 2,
        "the final workflow-run list detects a same-SHA rerun race",
        {
            "reason": getattr(race, "reason", None),
            "runs_reads": race_fake._runs_read_count,
        },
    )

    partial_fake = StrictFakeGitHubAPI(target_sha)
    partial_provider = provider(partial_fake)
    partial_binding = bind(partial_provider)
    partial_fake.include_newer_partial_run = True
    partial = expect_raises(
        GitHubCIUnknown,
        "a newer partial same-SHA run cannot be dropped in favor of an older run",
        lambda: partial_provider.observe(
            binding=partial_binding,
            target_ref=TARGET_REF,
            target_sha=target_sha,
            now=NOW,
        ),
    )
    expect(
        isinstance(partial, GitHubCIUnknown)
        and partial.reason == "workflow_run_identity_mismatch",
        "a partial filtered run makes the whole observation unknown",
        getattr(partial, "reason", None),
    )


def test_conclusions_pagination_and_missing_token() -> None:
    target_sha = "d" * 40
    failures: dict[str, Any] = {}
    expected_states = {
        "success": "clear",
        "action_required": "present",
        "failure": "present",
        "startup_failure": "present",
        "timed_out": "present",
    }
    for conclusion, expected_state in expected_states.items():
        fake = StrictFakeGitHubAPI(target_sha)
        fake.job_conclusions = {
            "frontend": "success",
            "gate": conclusion,
        }
        selected_provider = provider(fake)
        binding = bind(selected_provider)
        try:
            fact = selected_provider.observe(
                binding=binding,
                target_ref=TARGET_REF,
                target_sha=target_sha,
                now=NOW,
            )
        except Exception as exc:
            failures[conclusion] = f"{type(exc).__name__}: {exc}"
        else:
            if fact.get("signal_state") != expected_state:
                failures[conclusion] = fact

    for conclusion in ("cancelled", "neutral", "skipped", "stale"):
        fake = StrictFakeGitHubAPI(target_sha)
        fake.job_conclusions = {
            "frontend": "success",
            "gate": conclusion,
        }
        selected_provider = provider(fake)
        binding = bind(selected_provider)
        try:
            selected_provider.observe(
                binding=binding,
                target_ref=TARGET_REF,
                target_sha=target_sha,
                now=NOW,
            )
        except GitHubCIUnknown as exc:
            if exc.reason != "required_job_conclusion_unknown":
                failures[conclusion] = exc.reason
        except Exception as exc:
            failures[conclusion] = f"{type(exc).__name__}: {exc}"
        else:
            failures[conclusion] = "unexpected fact"

    pagination_cases = (
        ("runs", "workflow_runs_incomplete"),
        ("jobs", "workflow_jobs_incomplete"),
    )
    for label, expected_reason in pagination_cases:
        fake = StrictFakeGitHubAPI(target_sha)
        if label == "runs":
            fake.runs_total_count_override = 2
        else:
            fake.jobs_total_count_override = 3
        selected_provider = provider(fake)
        binding = bind(selected_provider)
        try:
            selected_provider.observe(
                binding=binding,
                target_ref=TARGET_REF,
                target_sha=target_sha,
                now=NOW,
            )
        except GitHubCIUnknown as exc:
            if exc.reason != expected_reason:
                failures[f"{label}_pagination"] = exc.reason
        except Exception as exc:
            failures[f"{label}_pagination"] = (
                f"{type(exc).__name__}: {exc}"
            )
        else:
            failures[f"{label}_pagination"] = "unexpected fact"

    expect(
        not failures,
        (
            "terminal conclusions and incomplete run/job pagination use the "
            "strict present/clear/unknown contract"
        ),
        failures,
    )

    tokenless = GitHubActionsCIProvider(token_resolver=lambda: "")
    missing = expect_raises(
        GitHubCIUnknown,
        "a real-client request without a token fails before network access",
        lambda: tokenless._request(f"/repos/{REPO_ID}", {}),
    )
    expect(
        isinstance(missing, GitHubCIUnknown)
        and missing.reason == "github_token_missing",
        "missing GitHub token has a stable fail-closed reason",
        getattr(missing, "reason", None),
    )


def test_disabled_mode_has_zero_ci_activity(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    fake = StrictFakeGitHubAPI(target_sha)
    spy = ProviderSpy(provider(fake))
    git_publishes: list[dict[str, Any]] = []
    ci_publishes: list[dict[str, Any]] = []
    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=lambda **kwargs: (
            git_publishes.append(copy.deepcopy(kwargs))
            or {"status": "unexpected"}
        ),
        publish_ci_observation=lambda **kwargs: (
            ci_publishes.append(copy.deepcopy(kwargs))
            or {"status": "unexpected"}
        ),
        ci_provider=spy,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    register_ci_goal(runtime, repo, goal_id="goal_disabled_ci")
    fake.calls.clear()
    spy.observe_calls.clear()
    state_before = store.read_json(runtime.STATE_FILE)
    result = runtime.run_once(reason="disabled_ci_contract")
    expect(
        result
        == {
            "status": "disabled",
            "mode": "disabled",
            "observed_count": 0,
            "published_count": 0,
        }
        and spy.observe_calls == []
        and fake.calls == []
        and git_publishes == []
        and ci_publishes == []
        and store.read_json(runtime.STATE_FILE) == state_before,
        (
            "configured disabled mode makes zero CI provider, API, publisher, "
            "Git, or telemetry calls"
        ),
        {
            "result": result,
            "provider_calls": spy.observe_calls,
            "api_calls": fake.calls,
            "git_publishes": git_publishes,
            "ci_publishes": ci_publishes,
        },
    )


def test_ci_goal_requires_matching_github_origin(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=lambda **kwargs: {"status": "unused"},
        publish_ci_observation=lambda **kwargs: {"status": "unused"},
        ci_provider=provider(StrictFakeGitHubAPI(target_sha)),
        clock=lambda: NOW,
    )

    expect_raises(
        ValueError,
        "a local bare origin cannot join local Git to GitHub CI facts",
        lambda: register_ci_goal(runtime, repo, goal_id="goal_local_origin"),
    )
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://evil.example/{REPO_ID}.git",
    )
    expect_raises(
        ValueError,
        "a same-name foreign host cannot join local Git to GitHub CI facts",
        lambda: register_ci_goal(runtime, repo, goal_id="goal_foreign_origin"),
    )
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"git@github.com:{REPO_ID}.git",
    )
    registered = register_ci_goal(
        runtime,
        repo,
        goal_id="goal_exact_github_origin",
    )
    expect(
        registered["goal_id"] == "goal_exact_github_origin",
        "the exact matching github.com SSH origin can bind CI facts",
        registered,
    )


def test_malformed_fact_preserves_all_git_probes(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fake = StrictFakeGitHubAPI(target_sha)
    timeline: list[str] = []

    def malformed_fact(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        timeline.append("ci")
        return {"signal_state": "present"}

    spy = ProviderSpy(provider(fake), observer=malformed_fact)
    git_publishes: list[dict[str, Any]] = []
    ci_publishes: list[dict[str, Any]] = []

    def accepted_git(**kwargs: Any) -> dict[str, Any]:
        git_publishes.append(copy.deepcopy(kwargs))
        return {
            "status": "enqueued",
            "event_id": f"git-{len(git_publishes)}",
            "signal_ledger_status": "recorded",
        }

    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=accepted_git,
        publish_ci_observation=lambda **kwargs: (
            ci_publishes.append(copy.deepcopy(kwargs))
            or {
                "status": "enqueued",
                "signal_ledger_status": "recorded",
            }
        ),
        ci_provider=spy,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    for index in range(2):
        register_ci_goal(
            runtime,
            repo,
            goal_id=f"goal_malformed_{index}",
        )

    original_inspect = runtime._inspect_repository

    def observed_inspect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        timeline.append("git")
        return original_inspect(*args, **kwargs)

    runtime._inspect_repository = observed_inspect  # type: ignore[method-assign]
    result = runtime.run_once(reason="malformed_ci_fact")
    git_observations = [
        item
        for item in result["observations"]
        if item.get("producer") == "git_dirty"
    ]
    ci_observations = [
        item
        for item in result["observations"]
        if item.get("producer") == "ci_failed"
    ]
    expect(
        result["status"] == "degraded"
        and result["active_goal_count"] == 2
        and result["observed_count"] == 4
        and result["failed_count"] == 2
        and len(git_observations) == 2
        and all(item["status"] == "enqueued" for item in git_observations)
        and len(ci_observations) == 2
        and all(
            item["status"] == "unknown"
            and item["reason"] == "ci_fact_invalid"
            for item in ci_observations
        )
        and timeline == ["git", "git", "ci", "ci"]
        and len(git_publishes) == 2
        and ci_publishes == [],
        (
            "malformed provider facts cannot abort run_once, and every Git "
            "probe completes before sequential CI probes begin"
        ),
        {
            "result": result,
            "timeline": timeline,
            "git_publishes": git_publishes,
            "ci_publishes": ci_publishes,
        },
    )


def test_zero_run_identity_is_rejected_by_reserved_ingress(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    ci_publisher = fabric.issue_project_guardian_ci_publisher()
    fake = StrictFakeGitHubAPI(target_sha)
    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=(
            fabric.issue_project_guardian_git_publisher()
        ),
        publish_ci_observation=ci_publisher,
        ci_provider=provider(fake),
        clock=lambda: NOW,
    )
    registered = register_ci_goal(
        runtime,
        repo,
        goal_id="goal_zero_ci_identity",
    )
    policy = store.read_json(runtime.STATE_FILE)["bindings"][
        registered["goal_id"]
    ]["github_actions"]
    fact_core = {
        "provider": GitHubActionsCIProvider.PROVIDER,
        "repo_id": REPO_ID,
        "repository_id": REPOSITORY_ID,
        "target_ref": TARGET_REF,
        "target_sha": target_sha,
        "workflow_id": WORKFLOW_ID,
        "workflow_path": WORKFLOW_PATH,
        "required_jobs_digest": GitHubActionsCIProvider._digest(
            REQUIRED_JOBS
        ),
        "expected_app_id": APP_ID,
        "run_id": 0,
        "run_number": 0,
        "run_attempt": 0,
        "check_suite_id": 0,
        "signal_state": "present",
        "observed_at": NOW.isoformat(),
        "checks_digest": "e" * 64,
        "policy_digest": str(policy["policy_digest"]),
    }
    self_digested_fact = {
        **fact_core,
        "probe_digest": hashlib.sha256(
            json.dumps(
                fact_core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    result = ci_publisher(
        user_id=registered["user_id"],
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        ci_fact=self_digested_fact,
    )
    expect(
        result
        == {
            "status": "ignored",
            "reason": "ci_fact_binding_mismatch",
        }
        and ci_signal(store) is None,
        (
            "reserved CI ingress rejects self-digested zero run, number, "
            "attempt, and check-suite identities"
        ),
        {"result": result, "fact": self_digested_fact},
    )


def test_sequential_ci_goals_receive_fresh_probe_clocks(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fake = StrictFakeGitHubAPI(target_sha)
    spy = ProviderSpy(provider(fake))
    ticks = [0]

    def ticking_clock() -> datetime:
        value = NOW + timedelta(seconds=ticks[0])
        ticks[0] += 1
        return value

    publish_counter = [0]

    def admitted(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        publish_counter[0] += 1
        return {
            "status": "enqueued",
            "event_id": f"admission-{publish_counter[0]}",
            "signal_ledger_status": "recorded",
        }

    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=admitted,
        publish_ci_observation=admitted,
        ci_provider=spy,  # type: ignore[arg-type]
        clock=ticking_clock,
    )
    for index in range(2):
        register_ci_goal(
            runtime,
            repo,
            goal_id=f"goal_fresh_clock_{index}",
        )
    spy.observe_calls.clear()
    result = runtime.run_once(reason="fresh_per_probe_clock")
    probe_times = [
        call["now"]
        for call in spy.observe_calls
    ]
    checked_at = datetime.fromisoformat(result["checked_at"])
    expect(
        result["status"] == "success"
        and len(probe_times) == 2
        and probe_times[0] > checked_at
        and probe_times[1] > probe_times[0]
        and probe_times[1] - probe_times[0] == timedelta(seconds=1),
        "sequential CI goals receive a fresh clock value for each provider probe",
        {
            "checked_at": checked_at,
            "probe_times": probe_times,
            "clock_calls": ticks[0],
        },
    )


def test_ci_binding_capacity_lifecycle(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    fake = StrictFakeGitHubAPI(target_sha)
    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=lambda **kwargs: {"status": "unused"},
        publish_ci_observation=lambda **kwargs: {"status": "unused"},
        ci_provider=provider(fake),
        clock=lambda: NOW,
    )
    completed: list[dict[str, Any]] = []
    for index in range(runtime.MAX_CI_BINDINGS):
        created = register_ci_goal(
            runtime,
            repo,
            goal_id=f"goal_completed_capacity_{index}",
        )
        completed.append(
            runtime.set_release_goal_status(
                user_id=created["user_id"],
                goal_id=created["goal_id"],
                expected_state_revision=created["state_revision"],
                status="completed",
            )
        )

    reserved: list[dict[str, Any]] = []
    for index in range(runtime.MAX_CI_BINDINGS):
        reserved.append(
            register_ci_goal(
                runtime,
                repo,
                goal_id=f"goal_reserved_capacity_{index}",
            )
        )
    original_completed = completed[0]
    active_error = expect_raises(
        ProjectGuardianGoalConflict,
        "completed-to-active rechecks a full CI binding cap",
        lambda: runtime.set_release_goal_status(
            user_id=original_completed["user_id"],
            goal_id=original_completed["goal_id"],
            expected_state_revision=original_completed["state_revision"],
            status="active",
        ),
    )
    paused_error = expect_raises(
        ProjectGuardianGoalConflict,
        "completed-to-paused cannot bypass a full CI binding cap",
        lambda: runtime.set_release_goal_status(
            user_id=original_completed["user_id"],
            goal_id=original_completed["goal_id"],
            expected_state_revision=original_completed["state_revision"],
            status="paused",
        ),
    )
    selected_reserved = reserved[0]
    paused = runtime.set_release_goal_status(
        user_id=selected_reserved["user_id"],
        goal_id=selected_reserved["goal_id"],
        expected_state_revision=selected_reserved["state_revision"],
        status="paused",
    )
    resumed = runtime.set_release_goal_status(
        user_id=paused["user_id"],
        goal_id=paused["goal_id"],
        expected_state_revision=paused["state_revision"],
        status="active",
    )
    retained_completed = next(
        item
        for item in runtime.list_release_goals(user_id="user-ci")
        if item["goal_id"] == original_completed["goal_id"]
    )
    expect(
        len(completed) == runtime.MAX_CI_BINDINGS
        and len(reserved) == runtime.MAX_CI_BINDINGS
        and retained_completed["status"] == "completed"
        and retained_completed["state_revision"]
        == original_completed["state_revision"]
        and "capacity exhausted" in str(active_error)
        and "capacity exhausted" in str(paused_error)
        and paused["status"] == "paused"
        and resumed["status"] == "active"
        and resumed["state_revision"] == paused["state_revision"] + 1,
        (
            "completed bindings free slots, completed active/paused transitions "
            "recheck the cap, and paused-to-active retains its reservation"
        ),
        {
            "completed_count": len(completed),
            "reserved_count": len(reserved),
            "retained_completed": retained_completed,
            "paused": paused,
            "resumed": resumed,
        },
    )

    expired_store = WorldStateStore(root / "expired-state")
    expired_runtime = ProjectGuardianProducerRuntime(
        state_store=expired_store,
        publish_git_observation=lambda **kwargs: {"status": "unused"},
        publish_ci_observation=lambda **kwargs: {"status": "unused"},
        ci_provider=provider(StrictFakeGitHubAPI(target_sha)),
        clock=lambda: NOW,
    )
    expired: list[dict[str, Any]] = []
    for index in range(expired_runtime.MAX_CI_BINDINGS):
        expired.append(
            register_ci_goal(
                expired_runtime,
                repo,
                goal_id=f"goal_expired_capacity_{index}",
                active_from=NOW - timedelta(hours=2),
                active_until=NOW - timedelta(hours=1),
            )
        )
    fresh = register_ci_goal(
        expired_runtime,
        repo,
        goal_id="goal_after_expired_capacity",
    )
    expect(
        len(expired) == expired_runtime.MAX_CI_BINDINGS
        and all(item["status"] == "active" for item in expired)
        and fresh["status"] == "active",
        "eight expired active CI goals free capacity for a fresh binding",
        {"expired_count": len(expired), "fresh": fresh},
    )


def test_ingress_transitions_candidate_and_public_status(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    git_publisher = fabric.issue_project_guardian_git_publisher()
    ci_publisher = fabric.issue_project_guardian_ci_publisher()
    fake = StrictFakeGitHubAPI(target_sha)
    runtime = ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=git_publisher,
        publish_ci_observation=ci_publisher,
        ci_provider=provider(fake),
        clock=lambda: NOW,
    )
    registered = register_ci_goal(runtime, repo)
    binding = store.read_json(runtime.STATE_FILE)["bindings"][
        registered["goal_id"]
    ]
    expect(
        binding["github_actions"]["repo_id"] == REPO_ID
        and binding["github_actions"]["workflow_id"] == WORKFLOW_ID
        and binding["github_actions"]["expected_app_id"] == APP_ID
        and runtime.status()["enabled_producers"]
        == ["git_dirty", "ci_failed"],
        "release Goal registration persists the bound CI policy",
        {"goal": registered, "binding": binding},
    )

    expect_raises(
        RuntimeError,
        "CI ingress capability is issued at most once per runtime",
        fabric.issue_project_guardian_ci_publisher,
    )
    untrusted = fabric._publish_project_guardian_ci_observation(
        ingress_capability=object(),
        user_id=registered["user_id"],
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        ci_fact={},
    )
    expect(
        untrusted
        == {
            "status": "ignored",
            "reason": "untrusted_project_guardian_ci_ingress",
        }
        and ci_signal(store) is None,
        "CI ingress rejects the wrong in-process capability before fact parsing",
        untrusted,
    )

    (repo / "tracked.txt").write_text(
        "initial\nrelease-risk\n",
        encoding="utf-8",
    )
    git_before = git_snapshot(repo)
    failure_run = runtime.run_once(reason="github_ci_failure")
    git_after = git_snapshot(repo)
    frontier = {
        str(signal.get("kind") or ""): signal
        for signal in frontier_signals(store)
    }
    evaluation = evaluate(store)
    candidate = (
        evaluation["candidates"][0]
        if evaluation.get("candidate_count") == 1
        else {}
    )
    expect(
        failure_run["status"] == "success"
        and failure_run["published_count"] == 2
        and failure_run["failed_count"] == 0
        and frontier["git_dirty"]["state"] == "present"
        and frontier["ci_failed"]["state"] == "present"
        and evaluation["candidate_count"] == 1
        and candidate.get("signal_kinds") == ["ci_failed", "git_dirty"]
        and candidate.get("shadow_only") is True
        and candidate.get("execution_allowed") is False
        and candidate.get("notification_allowed") is False
        and git_after == git_before,
        (
            "bound dirty Git plus failed CI creates one read-only shadow "
            "candidate without mutating Git"
        ),
        {
            "run": failure_run,
            "frontier": frontier,
            "evaluation": evaluation,
            "git_before": git_before,
            "git_after": git_after,
        },
    )

    prior_ci = copy.deepcopy(frontier["ci_failed"])
    prior_ci_event_count = sum(
        1
        for signal in frontier_signals(store)
        if str(signal.get("kind") or "") == "ci_failed"
    )
    fake.set_pending_rerun()
    pending_run = runtime.run_once(reason="github_ci_pending_rerun")
    pending_ci = ci_signal(store)
    git_observation = next(
        item
        for item in pending_run["observations"]
        if item.get("producer") == "git_dirty"
    )
    ci_observation = next(
        item
        for item in pending_run["observations"]
        if item.get("producer") == "ci_failed"
    )
    expect(
        pending_run["status"] == "degraded"
        and pending_run["failed_count"] == 1
        and git_observation["status"] in {"enqueued", "duplicate"}
        and git_observation["signal_state"] == "present"
        and ci_observation["status"] == "unknown"
        and ci_observation["reason"] == "workflow_run_not_terminal"
        and pending_ci == prior_ci
        and sum(
            1
            for signal in frontier_signals(store)
            if str(signal.get("kind") or "") == "ci_failed"
        )
        == prior_ci_event_count
        and evaluate(store)["candidate_count"] == 1,
        (
            "a pending rerun stays unknown, preserves the prior failure, and "
            "does not block the independent Git producer"
        ),
        {
            "run": pending_run,
            "prior_ci": prior_ci,
            "pending_ci": pending_ci,
        },
    )

    fake.set_successful_rerun()
    success_run = runtime.run_once(reason="github_ci_success")
    cleared_ci = ci_signal(store)
    after_success = evaluate(store)
    expect(
        success_run["status"] == "success"
        and success_run["failed_count"] == 0
        and cleared_ci is not None
        and cleared_ci["state"] == "clear"
        and cleared_ci["evidence_id"] != prior_ci["evidence_id"]
        and after_success["candidate_count"] == 0,
        (
            "a later terminal success emits a newer clear and removes the "
            "dirty-plus-failed qualification"
        ),
        {
            "run": success_run,
            "cleared_ci": cleared_ci,
            "evaluation": after_success,
        },
    )

    public_status = runtime.status()
    public_run = runtime._public_run_result(success_run)
    forbidden_keys = {
        "observations",
        "goal_id",
        "goal_revision",
        "repo_id",
        "repository_id",
        "target_ref",
        "target_sha",
        "workflow_id",
        "workflow_path",
        "expected_app_id",
        "run_id",
        "run_number",
        "run_attempt",
        "check_suite_id",
        "checks_digest",
        "policy_digest",
        "probe_digest",
        "event_id",
    }

    def keys(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(str(key))
                found.update(keys(item))
        elif isinstance(value, list):
            for item in value:
                found.update(keys(item))
        return found

    rendered = json.dumps(
        {"status": public_status, "run": public_run},
        ensure_ascii=False,
        sort_keys=True,
    )
    expect(
        not (keys(public_status) | keys(public_run)) & forbidden_keys
        and REPO_ID not in rendered
        and target_sha not in rendered
        and WORKFLOW_PATH not in rendered
        and registered["goal_id"] not in rendered
        and public_status["contracts"]["raw_provider_output_persisted"] is False,
        "public producer status exposes only tenant-neutral aggregate telemetry",
        {"status": public_status, "run": public_run},
    )


def test_ci_fault_non_interference_for_all_routes(root: Path) -> None:
    repo = create_github_ci_fixture(root / "git")
    target_sha = git(repo, "rev-parse", "HEAD")
    normalizer = EventNormalizer()
    failures: dict[str, Any] = {}
    for case in OFFLINE_ROUTE_CASES:
        baseline_started_at = datetime.now(timezone.utc)
        baseline_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "baseline",
            mode="record_only",
            case=case,
        )
        fault_started_at = datetime.now(timezone.utc)
        fault_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "fault",
            mode="record_only",
            case=case,
        )
        configure_mode(baseline_loop.state_store, "record_only")
        configure_mode(fault_loop.state_store, "record_only")

        baseline_fake = StrictFakeGitHubAPI(target_sha)
        baseline_runtime = ProjectGuardianProducerRuntime(
            state_store=baseline_loop.state_store,
            publish_git_observation=(
                baseline_loop.event_awareness
                .issue_project_guardian_git_publisher()
            ),
            publish_ci_observation=(
                baseline_loop.event_awareness
                .issue_project_guardian_ci_publisher()
            ),
            ci_provider=provider(baseline_fake),
            clock=lambda: NOW,
        )
        fault_fake = StrictFakeGitHubAPI(target_sha)
        fault_runtime = ProjectGuardianProducerRuntime(
            state_store=fault_loop.state_store,
            publish_git_observation=(
                fault_loop.event_awareness
                .issue_project_guardian_git_publisher()
            ),
            publish_ci_observation=(
                fault_loop.event_awareness
                .issue_project_guardian_ci_publisher()
            ),
            ci_provider=provider(fault_fake),
            clock=lambda: NOW,
        )
        goal_id = f"goal_ci_route_{case.case_id}"
        register_ci_goal(
            baseline_runtime,
            repo,
            user_id="matrix-user",
            goal_id=goal_id,
        )
        register_ci_goal(
            fault_runtime,
            repo,
            user_id="matrix-user",
            goal_id=goal_id,
        )
        fault_fake.set_pending_rerun()
        git_before = git_snapshot(repo)
        fault_run = fault_runtime.run_once(
            reason=f"ci_route_fault_{case.case_id}"
        )
        git_after = git_snapshot(repo)
        if not (
            fault_run["status"] == "degraded"
            and fault_run["failed_count"] == 1
            and any(
                item.get("producer") == "git_dirty"
                and item.get("status") in {"enqueued", "duplicate"}
                for item in fault_run["observations"]
            )
            and any(
                item.get("producer") == "ci_failed"
                and item.get("status") == "unknown"
                and item.get("reason") == "workflow_run_not_terminal"
                for item in fault_run["observations"]
            )
            and git_after == git_before
        ):
            failures[f"{case.case_id}:fault_contract"] = {
                "run": fault_run,
                "git_before": git_before,
                "git_after": git_after,
            }

        event = normalizer.user_message(
            case.text,
            "guardian-ci-route-matrix",
            "matrix-user",
            f"github-ci-{case.case_id}",
            event_id=f"evt_github_ci_matrix_{case.case_id}",
            correlation_id=f"corr-github-ci-matrix-{case.case_id}",
        )
        baseline_result = baseline_loop.handle_event(event)
        baseline_window = (
            baseline_started_at,
            datetime.now(timezone.utc),
        )
        fault_result = fault_loop.handle_event(event)
        fault_window = (
            fault_started_at,
            datetime.now(timezone.utc),
        )
        equivalent, differences = offline_public_outputs_equivalent(
            baseline_result,
            fault_result,
            require_distinct_generated_ids=True,
            left_runtime_window=baseline_window,
            right_runtime_window=fault_window,
        )
        if not (
            equivalent
            and baseline_result.route == case.route
            and fault_result.route == case.route
            and baseline_result.status == case.expected_status
            and fault_result.status == case.expected_status
            and baseline_result.risk_level == case.risk_level
            and fault_result.risk_level == case.risk_level
        ):
            failures[f"{case.case_id}:public_output"] = {
                "differences": differences,
                "baseline": baseline_result.to_dict(),
                "fault": fault_result.to_dict(),
            }

    expect(
        len(OFFLINE_ROUTE_CASES) == 9 and not failures,
        (
            "a fail-closed CI poll preserves all nine Route outputs, status, "
            "risk, Agent/Review/tool state, and Git state"
        ),
        failures,
    )


def main() -> int:
    test_binding_failure_state_and_provider_time()
    test_identity_mismatches_fail_closed()
    test_conclusions_pagination_and_missing_token()
    with tempfile.TemporaryDirectory(
        prefix="veyra-project-guardian-github-ci-"
    ) as temporary:
        root = Path(temporary)
        test_disabled_mode_has_zero_ci_activity(root / "disabled")
        test_ci_goal_requires_matching_github_origin(
            root / "origin-binding"
        )
        test_malformed_fact_preserves_all_git_probes(
            root / "malformed-fact"
        )
        test_zero_run_identity_is_rejected_by_reserved_ingress(
            root / "zero-run-identity"
        )
        test_sequential_ci_goals_receive_fresh_probe_clocks(
            root / "fresh-clocks"
        )
        test_ci_binding_capacity_lifecycle(root / "capacity")
        test_ingress_transitions_candidate_and_public_status(
            root / "transitions"
        )
        test_ci_fault_non_interference_for_all_routes(
            root / "non-interference"
        )
    print("All Project Guardian GitHub CI smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
