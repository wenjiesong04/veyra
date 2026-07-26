from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx


class GitHubCIContractError(ValueError):
    """A configured GitHub Actions identity is invalid or cannot be bound."""


class GitHubCIUnknown(RuntimeError):
    """The provider response cannot support a present or clear CI fact."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GitHubActionsCIProvider:
    """Read-only, fail-closed GitHub Actions evidence provider."""

    API_ORIGIN = "https://api.github.com"
    API_HOST = "api.github.com"
    API_VERSION = "2022-11-28"
    PROVIDER = "github_actions"
    CONTRACT = "veyra.project_guardian.github_actions_ci.v1"
    TOKEN_ENV = "VEYRA_GITHUB_TOKEN"
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    MAX_REQUIRED_JOBS = 20
    MAX_RUNS = 100
    MAX_JOBS = 100
    FAILURE_CONCLUSIONS = {
        "action_required",
        "failure",
        "startup_failure",
        "timed_out",
    }
    RUN_STATUSES = {
        "completed",
        "in_progress",
        "pending",
        "queued",
        "requested",
        "waiting",
    }
    RUN_CONCLUSIONS = FAILURE_CONCLUSIONS | {
        "cancelled",
        "neutral",
        "skipped",
        "stale",
        "success",
    }

    def __init__(
        self,
        *,
        request_json: Callable[
            [str, dict[str, str]], dict[str, Any]
        ]
        | None = None,
        token_resolver: Callable[[], str] | None = None,
        timeout_seconds: float = 4.0,
        max_age_seconds: int = 30 * 60,
        max_future_skew_seconds: int = 2 * 60,
    ) -> None:
        self._request_json_override = request_json
        self._token_resolver = token_resolver or (
            lambda: str(os.getenv(self.TOKEN_ENV) or "")
        )
        self.timeout_seconds = max(0.5, min(float(timeout_seconds), 15.0))
        self.max_age_seconds = max(60, int(max_age_seconds))
        self.max_future_skew_seconds = max(
            0,
            int(max_future_skew_seconds),
        )

    def bind_policy(
        self,
        *,
        repo_id: str,
        workflow_path: str,
        required_jobs: list[str],
        expected_app_id: int,
    ) -> dict[str, Any]:
        selected_repo = self._repo_id(repo_id)
        selected_workflow = self._workflow_path(workflow_path)
        selected_jobs = self._required_jobs(required_jobs)
        selected_app_id = self._positive_int(
            expected_app_id,
            "expected_app_id",
            contract=True,
        )
        owner, repository = selected_repo.split("/", 1)
        prefix = f"/repos/{owner}/{repository}"
        try:
            repository_payload = self._request(prefix, {})
        except GitHubCIUnknown as exc:
            raise GitHubCIContractError(
                f"GitHub repository binding failed: {exc.reason}"
            ) from None
        actual_repo_id = self._positive_int(
            repository_payload.get("id"),
            "repository.id",
            contract=True,
        )
        actual_full_name = self._repo_id(
            str(repository_payload.get("full_name") or "")
        )
        if actual_full_name.lower() != selected_repo.lower():
            raise GitHubCIContractError(
                "GitHub repository identity does not match repo_id"
            )

        try:
            workflow_payload = self._request(
                (
                    f"{prefix}/actions/workflows/"
                    f"{quote(selected_workflow, safe='')}"
                ),
                {},
            )
        except GitHubCIUnknown as exc:
            raise GitHubCIContractError(
                f"GitHub workflow binding failed: {exc.reason}"
            ) from None
        workflow_id = self._positive_int(
            workflow_payload.get("id"),
            "workflow.id",
            contract=True,
        )
        actual_workflow_path = self._workflow_path(
            str(workflow_payload.get("path") or "")
        )
        if actual_workflow_path != selected_workflow:
            raise GitHubCIContractError(
                "GitHub workflow path does not match the requested path"
            )
        if str(workflow_payload.get("state") or "") != "active":
            raise GitHubCIContractError(
                "GitHub Actions workflow must be active"
            )

        binding = {
            "schema_version": self.CONTRACT,
            "provider": self.PROVIDER,
            "api_origin": self.API_ORIGIN,
            "repo_id": selected_repo.lower(),
            "repository_id": actual_repo_id,
            "workflow_id": workflow_id,
            "workflow_path": actual_workflow_path,
            "event": "push",
            "expected_app_id": selected_app_id,
            "required_jobs": selected_jobs,
        }
        binding["policy_digest"] = self._digest(binding)
        return binding

    def observe(
        self,
        *,
        binding: dict[str, Any],
        target_ref: str,
        target_sha: str,
        now: datetime,
    ) -> dict[str, Any]:
        policy = self._validated_binding(binding)
        selected_ref = str(target_ref or "").strip()
        if not selected_ref.startswith("refs/heads/"):
            raise GitHubCIUnknown("target_ref_invalid")
        branch = selected_ref.removeprefix("refs/heads/")
        if not branch:
            raise GitHubCIUnknown("target_ref_invalid")
        selected_sha = self._sha(target_sha)
        checked_at = self._utc(now)
        owner, repository = str(policy["repo_id"]).split("/", 1)
        prefix = f"/repos/{owner}/{repository}"

        runs_path = (
            f"{prefix}/actions/workflows/"
            f"{int(policy['workflow_id'])}/runs"
        )
        runs_params = {
            "branch": branch,
            "event": "push",
            "head_sha": selected_sha,
            "per_page": str(self.MAX_RUNS),
        }
        selected_run = self._select_current_run(
            self._request(runs_path, runs_params),
            policy=policy,
            branch=branch,
            target_sha=selected_sha,
        )

        jobs_payload = self._request(
            (
                f"{prefix}/actions/runs/{int(selected_run['run_id'])}"
                f"/attempts/{int(selected_run['run_attempt'])}/jobs"
            ),
            {"per_page": str(self.MAX_JOBS)},
        )
        raw_jobs = jobs_payload.get("jobs")
        total_jobs = self._nonnegative_int(
            jobs_payload.get("total_count"),
            "workflow_jobs.total_count",
        )
        if (
            not isinstance(raw_jobs, list)
            or total_jobs != len(raw_jobs)
            or total_jobs > self.MAX_JOBS
        ):
            raise GitHubCIUnknown("workflow_jobs_incomplete")
        jobs_by_name: dict[str, list[dict[str, Any]]] = {}
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                raise GitHubCIUnknown("workflow_job_invalid")
            name = str(raw_job.get("name") or "")
            if name in policy["required_jobs"]:
                jobs_by_name.setdefault(name, []).append(raw_job)
        if any(
            len(jobs_by_name.get(name, [])) != 1
            for name in policy["required_jobs"]
        ):
            raise GitHubCIUnknown("required_job_missing_or_duplicate")

        checks_payload = self._request(
            f"{prefix}/commits/{selected_sha}/check-runs",
            {
                "app_id": str(int(policy["expected_app_id"])),
                "filter": "all",
                "per_page": "100",
            },
        )
        raw_checks = checks_payload.get("check_runs")
        if not isinstance(raw_checks, list):
            raise GitHubCIUnknown("check_runs_invalid")
        checks_by_id: dict[int, dict[str, Any]] = {}
        for raw_check in raw_checks:
            if not isinstance(raw_check, dict):
                continue
            check_id = self._optional_positive_int(raw_check.get("id"))
            if check_id is not None:
                checks_by_id[check_id] = raw_check

        required_outcomes: list[dict[str, Any]] = []
        for job_name in policy["required_jobs"]:
            raw_job = jobs_by_name[job_name][0]
            outcome = self._validated_required_job(
                raw_job,
                checks_by_id=checks_by_id,
                policy=policy,
                selected_run=selected_run,
                branch=branch,
                target_sha=selected_sha,
            )
            required_outcomes.append(outcome)

        reread_payload = self._request(
            f"{prefix}/actions/runs/{int(selected_run['run_id'])}",
            {},
        )
        reread = self._validated_run(
            reread_payload,
            policy=policy,
            branch=branch,
            target_sha=selected_sha,
        )
        if (
            reread is None
            or self._run_identity(reread)
            != self._run_identity(selected_run)
        ):
            raise GitHubCIUnknown("workflow_run_changed_during_probe")
        latest_run = self._select_current_run(
            self._request(runs_path, runs_params),
            policy=policy,
            branch=branch,
            target_sha=selected_sha,
        )
        if self._run_identity(latest_run) != self._run_identity(selected_run):
            raise GitHubCIUnknown("workflow_run_changed_during_probe")

        conclusions = {
            str(item["conclusion"]) for item in required_outcomes
        }
        known_conclusions = {"success"} | self.FAILURE_CONCLUSIONS
        if not conclusions or not conclusions <= known_conclusions:
            raise GitHubCIUnknown("required_job_conclusion_unknown")
        if conclusions <= {"success"}:
            signal_state = "clear"
        elif conclusions & self.FAILURE_CONCLUSIONS:
            signal_state = "present"
        observed_at = max(
            item["completed_at"] for item in required_outcomes
        )
        if (
            observed_at < checked_at - timedelta(
                seconds=self.max_age_seconds
            )
            or observed_at
            > checked_at + timedelta(
                seconds=self.max_future_skew_seconds
            )
        ):
            raise GitHubCIUnknown("ci_observation_time_invalid")

        checks_digest = self._digest(
            [
                {
                    key: (
                        value.isoformat()
                        if isinstance(value, datetime)
                        else value
                    )
                    for key, value in item.items()
                }
                for item in required_outcomes
            ]
        )
        fact = {
            "provider": self.PROVIDER,
            "repo_id": str(policy["repo_id"]),
            "repository_id": int(policy["repository_id"]),
            "target_ref": selected_ref,
            "target_sha": selected_sha,
            "workflow_id": int(policy["workflow_id"]),
            "workflow_path": str(policy["workflow_path"]),
            "required_jobs_digest": self._digest(
                list(policy["required_jobs"])
            ),
            "expected_app_id": int(policy["expected_app_id"]),
            "run_id": int(selected_run["run_id"]),
            "run_number": int(selected_run["run_number"]),
            "run_attempt": int(selected_run["run_attempt"]),
            "check_suite_id": int(selected_run["check_suite_id"]),
            "signal_state": signal_state,
            "observed_at": observed_at.isoformat(),
            "checks_digest": checks_digest,
            "policy_digest": str(policy["policy_digest"]),
        }
        fact["probe_digest"] = self._digest(fact)
        return fact

    def _request(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        if (
            not path.startswith("/repos/")
            or "://" in path
            or "\\" in path
            or any(character in path for character in ("\r", "\n", "\x00"))
            or any(
                segment in {".", ".."}
                for segment in path.split("/")
            )
        ):
            raise GitHubCIUnknown("github_api_path_invalid")
        if self._request_json_override is not None:
            try:
                payload = self._request_json_override(path, dict(params))
            except GitHubCIUnknown:
                raise
            except Exception as exc:
                raise GitHubCIUnknown("github_api_request_failed") from exc
            if not isinstance(payload, dict):
                raise GitHubCIUnknown("github_api_response_invalid")
            return payload

        token = str(self._token_resolver() or "").strip()
        if not token:
            raise GitHubCIUnknown("github_token_missing")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION,
            "User-Agent": "Veyra-Project-Guardian/1",
            "Authorization": f"Bearer {token}",
        }
        try:
            with httpx.Client(
                base_url=self.API_ORIGIN,
                headers=headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get(path, params=params)
                if response.is_redirect:
                    raise GitHubCIUnknown("github_api_redirect_rejected")
                if response.status_code != 200:
                    raise GitHubCIUnknown(
                        f"github_api_http_{response.status_code}"
                    )
                content = response.content
        except GitHubCIUnknown:
            raise
        except Exception as exc:
            raise GitHubCIUnknown("github_api_request_failed") from exc
        if len(content) > self.MAX_RESPONSE_BYTES:
            raise GitHubCIUnknown("github_api_response_too_large")
        try:
            decoded = content.decode("utf-8")
            payload = json.loads(
                decoded,
                object_pairs_hook=self._reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GitHubCIUnknown("github_api_json_invalid") from exc
        if not isinstance(payload, dict):
            raise GitHubCIUnknown("github_api_response_invalid")
        return payload

    def _validated_binding(
        self,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(binding, dict):
            raise GitHubCIUnknown("ci_binding_invalid")
        try:
            selected = {
                "schema_version": binding.get("schema_version"),
                "provider": binding.get("provider"),
                "api_origin": binding.get("api_origin"),
                "repo_id": self._repo_id(
                    str(binding.get("repo_id") or "")
                ),
                "repository_id": self._positive_int(
                    binding.get("repository_id"),
                    "repository_id",
                ),
                "workflow_id": self._positive_int(
                    binding.get("workflow_id"),
                    "workflow_id",
                ),
                "workflow_path": self._workflow_path(
                    str(binding.get("workflow_path") or "")
                ),
                "event": binding.get("event"),
                "expected_app_id": self._positive_int(
                    binding.get("expected_app_id"),
                    "expected_app_id",
                ),
                "required_jobs": self._required_jobs(
                    binding.get("required_jobs")
                ),
            }
        except (GitHubCIContractError, GitHubCIUnknown) as exc:
            raise GitHubCIUnknown("ci_binding_invalid") from exc
        if (
            selected["schema_version"] != self.CONTRACT
            or selected["provider"] != self.PROVIDER
            or selected["api_origin"] != self.API_ORIGIN
            or selected["event"] != "push"
        ):
            raise GitHubCIUnknown("ci_binding_invalid")
        expected_digest = self._digest(selected)
        if str(binding.get("policy_digest") or "") != expected_digest:
            raise GitHubCIUnknown("ci_binding_digest_mismatch")
        selected["policy_digest"] = expected_digest
        return selected

    def _select_current_run(
        self,
        runs_payload: dict[str, Any],
        *,
        policy: dict[str, Any],
        branch: str,
        target_sha: str,
    ) -> dict[str, Any]:
        raw_runs = runs_payload.get("workflow_runs")
        total_runs = self._nonnegative_int(
            runs_payload.get("total_count"),
            "workflow_runs.total_count",
        )
        if (
            not isinstance(raw_runs, list)
            or total_runs != len(raw_runs)
            or total_runs > self.MAX_RUNS
        ):
            raise GitHubCIUnknown("workflow_runs_incomplete")
        matching_runs: list[dict[str, Any]] = []
        for raw in raw_runs:
            if not isinstance(raw, dict):
                raise GitHubCIUnknown("workflow_run_invalid")
            item = self._validated_run(
                raw,
                policy=policy,
                branch=branch,
                target_sha=target_sha,
            )
            if item is None:
                raise GitHubCIUnknown("workflow_run_identity_mismatch")
            matching_runs.append(item)
        if not matching_runs:
            raise GitHubCIUnknown("workflow_run_missing")
        matching_runs.sort(
            key=lambda item: (
                int(item["run_number"]),
                int(item["run_attempt"]),
                int(item["run_id"]),
            )
        )
        selected_run = matching_runs[-1]
        semantic_run_keys = (
            int(selected_run["run_number"]),
            int(selected_run["run_attempt"]),
        )
        if (
            sum(
                1
                for item in matching_runs
                if (
                    int(item["run_number"]),
                    int(item["run_attempt"]),
                )
                == semantic_run_keys
            )
            != 1
        ):
            raise GitHubCIUnknown("workflow_run_ambiguous")
        if selected_run["status"] != "completed":
            raise GitHubCIUnknown("workflow_run_not_terminal")
        if (
            str(selected_run["conclusion"])
            not in {"success"} | self.FAILURE_CONCLUSIONS
        ):
            raise GitHubCIUnknown("workflow_run_conclusion_unknown")
        return selected_run

    def _validated_run(
        self,
        raw: dict[str, Any],
        *,
        policy: dict[str, Any],
        branch: str,
        target_sha: str,
    ) -> dict[str, Any] | None:
        repository = (
            raw.get("repository")
            if isinstance(raw.get("repository"), dict)
            else {}
        )
        head_repository = (
            raw.get("head_repository")
            if isinstance(raw.get("head_repository"), dict)
            else {}
        )
        path = str(raw.get("path") or "").rsplit("@", 1)[0]
        if (
            str(raw.get("event") or "") != "push"
            or str(raw.get("head_branch") or "") != branch
            or str(raw.get("head_sha") or "").lower() != target_sha
            or self._optional_positive_int(raw.get("workflow_id"))
            != int(policy["workflow_id"])
            or path != str(policy["workflow_path"])
            or self._optional_positive_int(repository.get("id"))
            != int(policy["repository_id"])
            or str(repository.get("full_name") or "").lower()
            != str(policy["repo_id"]).lower()
            or self._optional_positive_int(head_repository.get("id"))
            != int(policy["repository_id"])
            or str(head_repository.get("full_name") or "").lower()
            != str(policy["repo_id"]).lower()
        ):
            return None
        status = str(raw.get("status") or "")
        conclusion = str(raw.get("conclusion") or "")
        if (
            status not in self.RUN_STATUSES
            or (
                status == "completed"
                and conclusion not in self.RUN_CONCLUSIONS
            )
            or (status != "completed" and conclusion)
        ):
            return None
        try:
            return {
                "run_id": self._positive_int(raw.get("id"), "run.id"),
                "run_number": self._positive_int(
                    raw.get("run_number"),
                    "run.run_number",
                ),
                "run_attempt": self._positive_int(
                    raw.get("run_attempt"),
                    "run.run_attempt",
                ),
                "check_suite_id": self._positive_int(
                    raw.get("check_suite_id"),
                    "run.check_suite_id",
                ),
                "workflow_id": int(policy["workflow_id"]),
                "path": path,
                "head_branch": branch,
                "head_sha": target_sha,
                "repository_id": int(policy["repository_id"]),
                "event": "push",
                "status": status,
                "conclusion": conclusion,
                "updated_at": self._time(raw.get("updated_at")),
            }
        except GitHubCIUnknown:
            return None

    def _validated_required_job(
        self,
        raw_job: dict[str, Any],
        *,
        checks_by_id: dict[int, dict[str, Any]],
        policy: dict[str, Any],
        selected_run: dict[str, Any],
        branch: str,
        target_sha: str,
    ) -> dict[str, Any]:
        job_id = self._positive_int(raw_job.get("id"), "job.id")
        job_name = str(raw_job.get("name") or "")
        check_run_id = self._check_run_id(
            raw_job.get("check_run_url"),
            repo_id=str(policy["repo_id"]),
        )
        raw_check = checks_by_id.get(check_run_id)
        app = (
            raw_check.get("app")
            if isinstance(raw_check, dict)
            and isinstance(raw_check.get("app"), dict)
            else {}
        )
        check_suite = (
            raw_check.get("check_suite")
            if isinstance(raw_check, dict)
            and isinstance(raw_check.get("check_suite"), dict)
            else {}
        )
        status = str(raw_job.get("status") or "")
        conclusion = str(raw_job.get("conclusion") or "")
        completed_at = self._time(raw_job.get("completed_at"))
        if (
            self._optional_positive_int(raw_job.get("run_id"))
            != int(selected_run["run_id"])
            or str(raw_job.get("head_sha") or "").lower() != target_sha
            or str(raw_job.get("head_branch") or "") != branch
            or status != "completed"
            or not conclusion
            or raw_check is None
            or self._optional_positive_int(raw_check.get("id"))
            != check_run_id
            or str(raw_check.get("name") or "") != job_name
            or str(raw_check.get("head_sha") or "").lower() != target_sha
            or str(raw_check.get("status") or "") != status
            or str(raw_check.get("conclusion") or "") != conclusion
            or self._optional_positive_int(app.get("id"))
            != int(policy["expected_app_id"])
            or self._optional_positive_int(check_suite.get("id"))
            != int(selected_run["check_suite_id"])
        ):
            raise GitHubCIUnknown("required_job_identity_mismatch")
        return {
            "name": job_name,
            "job_id": job_id,
            "check_run_id": check_run_id,
            "status": status,
            "conclusion": conclusion,
            "completed_at": completed_at,
        }

    def _check_run_id(self, value: Any, *, repo_id: str) -> int:
        parsed = urlsplit(str(value or ""))
        expected_prefix = f"/repos/{repo_id}/check-runs/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.API_HOST
            or parsed.query
            or parsed.fragment
            or not parsed.path.lower().startswith(expected_prefix.lower())
        ):
            raise GitHubCIUnknown("check_run_url_invalid")
        suffix = parsed.path[len(expected_prefix) :]
        if not suffix.isdigit() or "/" in suffix:
            raise GitHubCIUnknown("check_run_url_invalid")
        return self._positive_int(int(suffix), "check_run.id")

    @staticmethod
    def _run_identity(run: dict[str, Any]) -> str:
        return GitHubActionsCIProvider._digest(
            {
                key: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else value
                )
                for key, value in run.items()
            }
        )

    @classmethod
    def _required_jobs(cls, value: Any) -> list[str]:
        if (
            not isinstance(value, list)
            or not value
            or len(value) > cls.MAX_REQUIRED_JOBS
        ):
            raise GitHubCIContractError(
                "required_jobs must be a non-empty bounded list"
            )
        jobs: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise GitHubCIContractError(
                    "required job names must be strings"
                )
            job = raw.strip()
            if (
                not job
                or len(job) > 160
                or any(character in job for character in ("\r", "\n", "\x00"))
            ):
                raise GitHubCIContractError(
                    "required job names must be non-empty and <= 160 chars"
                )
            jobs.append(job)
        if len(set(jobs)) != len(jobs):
            raise GitHubCIContractError("required job names must be unique")
        return sorted(jobs)

    @staticmethod
    def _workflow_path(value: str) -> str:
        selected = str(value or "").strip()
        prefix = ".github/workflows/"
        filename = selected.removeprefix(prefix)
        if (
            not selected.startswith(prefix)
            or not filename
            or "/" in filename
            or not filename.endswith((".yml", ".yaml"))
            or any(
                character in selected
                for character in ("\\", "\r", "\n", "\x00", "?", "#")
            )
        ):
            raise GitHubCIContractError(
                "workflow_path must be one .github/workflows/*.yml file"
            )
        return selected

    @staticmethod
    def _repo_id(value: str) -> str:
        selected = str(value or "").strip().removesuffix(".git")
        parts = selected.split("/")
        if (
            len(parts) != 2
            or any(not part or len(part) > 100 for part in parts)
            or any(part in {".", ".."} for part in parts)
            or any(
                character not in (
                    "abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "0123456789-_."
                )
                for part in parts
                for character in part
            )
        ):
            raise GitHubCIContractError(
                "repo_id must use GitHub owner/repository form"
            )
        return selected

    @staticmethod
    def _sha(value: Any) -> str:
        selected = str(value or "").strip().lower()
        if (
            len(selected) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in selected)
        ):
            raise GitHubCIUnknown("target_sha_invalid")
        return selected

    @staticmethod
    def _time(value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            raise GitHubCIUnknown("provider_time_invalid")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubCIUnknown("provider_time_invalid") from exc
        if parsed.tzinfo is None:
            raise GitHubCIUnknown("provider_time_invalid")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise GitHubCIUnknown("probe_clock_invalid")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _positive_int(
        value: Any,
        field: str,
        *,
        contract: bool = False,
    ) -> int:
        error_type: type[Exception] = (
            GitHubCIContractError if contract else GitHubCIUnknown
        )
        if not isinstance(value, int) or isinstance(value, bool):
            raise error_type(f"{field}_invalid")
        if value < 1:
            raise error_type(f"{field}_invalid")
        return value

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return value if value >= 1 else None

    @staticmethod
    def _nonnegative_int(value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise GitHubCIUnknown(f"{field}_invalid")
        if value < 0:
            raise GitHubCIUnknown(f"{field}_invalid")
        return value

    @staticmethod
    def _reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for key, value in pairs:
            if key in selected:
                raise ValueError("duplicate JSON key")
            selected[key] = value
        return selected

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
