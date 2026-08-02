from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from awareness.general_attention_scheduler import GeneralAttentionScheduler
from awareness.project_guardian import ProjectGuardianEvaluator
from core.situation_evaluator import SituationEvaluator
from core.world_state import WorldStateStore
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import (
    EventSource,
    EventType,
    LoopResult,
    Route,
    VeyraEvent,
    utc_now_iso,
)
from runtime.event_inbox import EventInbox
from runtime.general_situation_runtime import GeneralSituationRuntime
from runtime.project_guardian import ProjectGuardianRuntime
from runtime.project_guardian_github_ci import GitHubActionsCIProvider
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from runtime.suggestion_outbox import SuggestionOutbox


VERIFIED_RESULT_STATUSES = {"verified_failed", "verified_success"}


class ShadowAwarenessRuntime:
    """Connect durable events to evidence-linked situations without authority.

    This component is deliberately outside the routing and execution policy
    path. Failures are observable but never change the user's route, response,
    risk classification, or authorization.
    """

    MODES = {"disabled", "record_only", "shadow"}

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        mode: str = "record_only",
    ) -> None:
        selected_mode = str(mode or "record_only").strip().lower()
        if selected_mode not in self.MODES:
            selected_mode = "record_only"
        self.state_store = state_store
        self.mode = selected_mode
        config = state_store.read_json("ops_config.json")
        section = config.get("event_awareness") if isinstance(config, dict) else {}
        self.mode_epoch = self._nonnegative_int(
            section.get("mode_epoch") if isinstance(section, dict) else 0
        )
        self.event_inbox = EventInbox(state_store)
        self.situation_evaluator = SituationEvaluator(state_store)
        self.general_situations = GeneralSituationRuntime(state_store)
        self.general_attention = GeneralAttentionScheduler(state_store)
        self.suggestion_outbox = SuggestionOutbox(state_store)
        self.project_guardian_signals = ProjectGuardianSignalLedger(state_store)
        self._project_guardian_git_ingress_capability = object()
        self._project_guardian_git_publisher_issued = False
        self._project_guardian_ci_ingress_capability = object()
        self._project_guardian_ci_publisher_issued = False
        self._project_guardian_intent_ingress_capability = object()
        self._project_guardian_intent_publisher_issued = False

    def configure(self, mode: str) -> dict[str, Any]:
        selected = str(mode or "").strip().lower()
        if selected not in self.MODES:
            raise ValueError(f"mode must be one of {sorted(self.MODES)}")
        result: dict[str, Any] = {}
        def update(config: dict[str, Any]) -> None:
            current = (
                config.get("event_awareness")
                if isinstance(config.get("event_awareness"), dict)
                else {}
            )
            previous = str(
                current.get("mode") or self.mode or "record_only"
            ).strip().lower()
            if previous not in self.MODES:
                previous = "record_only"
            previous_epoch = self._nonnegative_int(
                current.get("mode_epoch")
            )
            next_epoch = previous_epoch + int(previous != selected)
            config["event_awareness"] = {
                **copy.deepcopy(current),
                "mode": selected,
                "mode_epoch": next_epoch,
                "allowed_modes": sorted(self.MODES),
            }
            self.mode = selected
            self.mode_epoch = next_epoch
            result.update(
                previous_mode=previous,
                mode=selected,
                mode_epoch=next_epoch,
            )

        self.state_store.mutate_json("ops_config.json", update)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "event_awareness_config",
                "status": "updated",
                "artifacts": copy.deepcopy(result),
            },
        )
        return {
            "status": "updated",
            **result,
            "allowed_modes": sorted(self.MODES),
        }

    def publish(self, event: VeyraEvent) -> dict[str, Any]:
        """Durably admit an internal or external event without processing it."""

        if self._is_project_guardian_signal(event):
            return {
                "status": "disabled",
                "event_id": event.event_id,
                "reason": "reserved_project_guardian_signal_ingress",
            }
        with self.state_store.writer_transaction():
            fabric = self._fabric_snapshot()
            if fabric["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "event_id": event.event_id,
                    "reason": "event_fabric_disabled",
                }
            if self._is_project_guardian_event(event):
                binding = ProjectGuardianRuntime.validate_projection_event(event)
                guardian = self._project_guardian_snapshot()
                if binding is None:
                    return {
                        "status": "disabled",
                        "event_id": event.event_id,
                        "reason": "invalid_project_guardian_projection",
                    }
                if (
                    guardian["mode"] != "shadow"
                    or binding["guardian_mode_epoch"] != guardian["mode_epoch"]
                    or binding["event_fabric_mode_epoch"] != fabric["mode_epoch"]
                ):
                    return {
                        "status": "disabled",
                        "event_id": event.event_id,
                        "reason": "stale_project_guardian_mode_epoch",
                    }
            return self.event_inbox.enqueue(event)

    def issue_project_guardian_git_publisher(
        self,
    ) -> Callable[..., dict[str, Any]]:
        """Issue the single trusted Git producer capability for this runtime."""

        if self._project_guardian_git_publisher_issued:
            raise RuntimeError(
                "Project Guardian Git publisher capability was already issued"
            )
        self._project_guardian_git_publisher_issued = True
        capability = self._project_guardian_git_ingress_capability

        def publish(**kwargs: Any) -> dict[str, Any]:
            return self._publish_project_guardian_git_observation(
                ingress_capability=capability,
                **kwargs,
            )

        return publish

    def issue_project_guardian_ci_publisher(
        self,
    ) -> Callable[..., dict[str, Any]]:
        """Issue the single trusted CI producer capability for this runtime."""

        if self._project_guardian_ci_publisher_issued:
            raise RuntimeError(
                "Project Guardian CI publisher capability was already issued"
            )
        self._project_guardian_ci_publisher_issued = True
        capability = self._project_guardian_ci_ingress_capability

        def publish(**kwargs: Any) -> dict[str, Any]:
            return self._publish_project_guardian_ci_observation(
                ingress_capability=capability,
                **kwargs,
            )

        return publish

    def issue_project_guardian_deployment_intent_publisher(
        self,
    ) -> Callable[..., dict[str, Any]]:
        """Issue the single structured deployment-intent capability."""

        if self._project_guardian_intent_publisher_issued:
            raise RuntimeError(
                "Project Guardian deployment-intent publisher capability "
                "was already issued"
            )
        self._project_guardian_intent_publisher_issued = True
        capability = self._project_guardian_intent_ingress_capability

        def publish(**kwargs: Any) -> dict[str, Any]:
            return self._publish_project_guardian_deployment_intent(
                ingress_capability=capability,
                **kwargs,
            )

        return publish

    def _publish_project_guardian_git_observation(
        self,
        *,
        ingress_capability: object,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        repository_fact: dict[str, Any],
    ) -> dict[str, Any]:
        """Admit one in-process Git fact through the reserved signal channel.

        Scope, component identity, factual evidence, timestamps and the ingress
        receipt are derived here. Callers cannot provide a pre-built envelope.
        """

        if (
            ingress_capability
            is not self._project_guardian_git_ingress_capability
        ):
            return {
                "status": "ignored",
                "reason": "untrusted_project_guardian_git_ingress",
            }
        observed_at = ProjectGuardianSignalLedger._time(
            repository_fact.get("observed_at")
        )
        with self.state_store.writer_transaction():
            admitted_at = datetime.now(timezone.utc).replace(microsecond=0)
            if not self._project_guardian_observation_time_valid(
                observed_at,
                admitted_at,
            ):
                return {
                    "status": "ignored",
                    "reason": "repository_fact_observation_time_invalid",
                }
            fabric = self._fabric_snapshot()
            guardian = self._project_guardian_snapshot()
            if guardian["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "project_guardian_disabled",
                }
            if fabric["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "event_fabric_disabled",
                }
            if fabric["mode"] not in self.MODES:
                return {
                    "status": "degraded",
                    "reason": "event_fabric_unavailable",
                }

            goals_state = self.state_store.read_json("user_goals.json")
            if goals_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "user_goals_state_corrupt",
                }
            goal = self._active_release_goal(
                goals_state,
                user_id=user_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                observed_at=observed_at,
            )
            if goal is None:
                return {
                    "status": "ignored",
                    "reason": "release_goal_not_active_or_mismatched",
                }

            scope = (
                goal.get("scope")
                if isinstance(goal.get("scope"), dict)
                else {}
            )
            producer_state = self.state_store.read_json(
                "project_guardian_producer_state.json"
            )
            if producer_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "project_guardian_producer_state_corrupt",
                }
            bindings = (
                producer_state.get("bindings")
                if isinstance(producer_state.get("bindings"), dict)
                else {}
            )
            binding = bindings.get(goal_id)
            repo_id = str(repository_fact.get("repo_id") or "")
            target_ref = str(repository_fact.get("target_ref") or "")
            target_sha = str(repository_fact.get("target_sha") or "")
            remote_origin_digest = str(
                repository_fact.get("remote_origin_digest") or ""
            )
            expected_sha = str(goal.get("target_sha") or "")
            dirty = repository_fact.get("dirty")
            observed_at_text = observed_at.isoformat()
            index_flags_digest = str(
                repository_fact.get("index_flags_digest") or ""
            )
            index_stage_digest = str(
                repository_fact.get("index_stage_digest") or ""
            )
            probe_digest = str(repository_fact.get("probe_digest") or "")
            expected_probe_digest = hashlib.sha256(
                json.dumps(
                    {
                        "repo_id": repo_id,
                        "target_ref": target_ref,
                        "target_sha": target_sha,
                        "remote_origin_digest": remote_origin_digest,
                        "dirty": dirty,
                        "observed_at": observed_at_text,
                        "index_flags_digest": index_flags_digest,
                        "index_stage_digest": index_stage_digest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                not isinstance(binding, dict)
                or str(binding.get("user_id") or "") != str(user_id)
                or str(binding.get("goal_revision") or "")
                != str(goal_revision)
                or str(binding.get("workspace_id") or "")
                != str(scope.get("workspace_id") or "")
                or str(binding.get("repo_id") or "") != repo_id
                or str(binding.get("target_ref") or "") != target_ref
                or str(binding.get("target_sha") or "") != target_sha
                or str(binding.get("remote_origin_digest") or "")
                != remote_origin_digest
                or not isinstance(dirty, bool)
                or repo_id != str(scope.get("repo_id") or "")
                or target_ref != str(scope.get("target_ref") or "")
                or not expected_sha
                or target_sha != expected_sha
                or len(remote_origin_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in remote_origin_digest
                )
                or len(index_flags_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in index_flags_digest
                )
                or len(index_stage_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in index_stage_digest
                )
                or probe_digest != expected_probe_digest
            ):
                return {
                    "status": "ignored",
                    "reason": "repository_fact_binding_mismatch",
                }

            kind = "git_dirty"
            signal_state = "present" if dirty else "clear"
            component = ProjectGuardianEvaluator.SIGNAL_COMPONENTS[kind]
            producer = ProjectGuardianEvaluator.SIGNAL_PRODUCERS[kind]
            binding_ref = str(binding.get("binding_id") or "")
            if not binding_ref.startswith("pgwb_"):
                return {
                    "status": "ignored",
                    "reason": "repository_binding_identity_missing",
                }
            binding_id = hashlib.sha256(
                binding_ref.encode("utf-8")
            ).hexdigest()[:20]
            provenance_root = f"{component}:binding_{binding_id}"
            evidence_id = "pgge_" + hashlib.sha256(
                json.dumps(
                    {
                        "producer_id": producer["producer_id"],
                        "goal_id": goal_id,
                        "goal_revision": goal_revision,
                        "repo_id": repo_id,
                        "target_ref": target_ref,
                        "target_sha": target_sha,
                        "remote_origin_digest": remote_origin_digest,
                        "dirty": dirty,
                        "observed_at": observed_at_text,
                        "index_flags_digest": index_flags_digest,
                        "index_stage_digest": index_stage_digest,
                        "probe_digest": probe_digest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            occurred_at = observed_at_text
            valid_until = (
                observed_at + timedelta(minutes=6)
            ).isoformat()
            session_id = f"project-guardian-git-{binding_id[:12]}"
            receipt_id = ProjectGuardianEvaluator.producer_receipt_id_for(
                kind=kind,
                state=signal_state,
                source_component=component,
                provenance_root=provenance_root,
                evidence_id=evidence_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                scope=scope,
                valid_until=valid_until,
                producer_id=str(producer["producer_id"]),
                trust_class=str(producer["trust_class"]),
                user_id=user_id,
                session_id=session_id,
                occurred_at=occurred_at,
            )
            event_id = "pgse_" + hashlib.sha256(
                f"{receipt_id}:{occurred_at}".encode("utf-8")
            ).hexdigest()[:24]
            event = VeyraEvent(
                type=EventType.OBSERVATION,
                source=EventSource(
                    channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
                    user_id=user_id,
                    session_id=session_id,
                ),
                payload={
                    "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
                    "project_guardian_signal": {
                        "kind": kind,
                        "state": signal_state,
                        "source_component": component,
                        "provenance_root": provenance_root,
                        "evidence_id": evidence_id,
                        "goal_id": goal_id,
                        "goal_revision": goal_revision,
                        "scope": {
                            key: str(scope.get(key) or "")
                            for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                        },
                        "valid_until": valid_until,
                        "producer_attestation": {
                            "schema_version": (
                                ProjectGuardianEvaluator
                                .PRODUCER_ATTESTATION_SCHEMA
                            ),
                            "producer_id": str(producer["producer_id"]),
                            "trust_class": str(producer["trust_class"]),
                            "admission_source": (
                                "project_guardian_signal_ingress"
                            ),
                            "receipt_id": receipt_id,
                        },
                    },
                },
                event_id=event_id,
                timestamp=occurred_at,
                occurred_at=occurred_at,
                evidence_refs=[
                    {
                        "ref_id": evidence_id,
                        "source": component,
                        "is_fact": True,
                    }
                ],
                privacy_scope="user",
            )
            commit_time = datetime.now(timezone.utc).replace(microsecond=0)
            if not self._project_guardian_observation_time_valid(
                observed_at,
                commit_time,
            ):
                return {
                    "status": "ignored",
                    "reason": "repository_fact_observation_time_invalid",
                }
            return self._admit_project_guardian_signal(event)

    def _publish_project_guardian_ci_observation(
        self,
        *,
        ingress_capability: object,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        ci_fact: dict[str, Any],
    ) -> dict[str, Any]:
        """Admit one bound GitHub Actions fact through the reserved channel."""

        if (
            ingress_capability
            is not self._project_guardian_ci_ingress_capability
        ):
            return {
                "status": "ignored",
                "reason": "untrusted_project_guardian_ci_ingress",
            }
        observed_at = ProjectGuardianSignalLedger._time(
            ci_fact.get("observed_at")
        )
        with self.state_store.writer_transaction():
            admitted_at = datetime.now(timezone.utc)
            if not self._project_guardian_ci_observation_time_valid(
                observed_at,
                admitted_at,
            ):
                return {
                    "status": "ignored",
                    "reason": "ci_fact_observation_time_invalid",
                }
            fabric = self._fabric_snapshot()
            guardian = self._project_guardian_snapshot()
            if guardian["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "project_guardian_disabled",
                }
            if fabric["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "event_fabric_disabled",
                }
            if fabric["mode"] not in self.MODES:
                return {
                    "status": "degraded",
                    "reason": "event_fabric_unavailable",
                }

            goals_state = self.state_store.read_json("user_goals.json")
            if goals_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "user_goals_state_corrupt",
                }
            goal = self._active_release_goal(
                goals_state,
                user_id=user_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                observed_at=observed_at,
            )
            if goal is None:
                return {
                    "status": "ignored",
                    "reason": "release_goal_not_active_or_mismatched",
                }
            scope = (
                goal.get("scope")
                if isinstance(goal.get("scope"), dict)
                else {}
            )

            producer_state = self.state_store.read_json(
                "project_guardian_producer_state.json"
            )
            if producer_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "project_guardian_producer_state_corrupt",
                }
            bindings = (
                producer_state.get("bindings")
                if isinstance(producer_state.get("bindings"), dict)
                else {}
            )
            binding = bindings.get(goal_id)
            policy = (
                binding.get("github_actions")
                if isinstance(binding, dict)
                and isinstance(binding.get("github_actions"), dict)
                else {}
            )
            required_jobs = (
                sorted(
                    str(item)
                    for item in policy.get("required_jobs", [])
                    if isinstance(item, str)
                )
                if isinstance(policy.get("required_jobs"), list)
                else []
            )

            def positive_int(value: Any) -> int:
                if not isinstance(value, int) or isinstance(value, bool):
                    return 0
                return value if value >= 1 else 0

            policy_core = {
                "schema_version": str(
                    policy.get("schema_version") or ""
                ),
                "provider": str(policy.get("provider") or ""),
                "api_origin": str(policy.get("api_origin") or ""),
                "repo_id": str(policy.get("repo_id") or ""),
                "repository_id": positive_int(
                    policy.get("repository_id")
                ),
                "workflow_id": positive_int(policy.get("workflow_id")),
                "workflow_path": str(policy.get("workflow_path") or ""),
                "event": str(policy.get("event") or ""),
                "expected_app_id": positive_int(
                    policy.get("expected_app_id")
                ),
                "required_jobs": required_jobs,
            }
            expected_policy_digest = hashlib.sha256(
                json.dumps(
                    policy_core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            expected_required_jobs_digest = hashlib.sha256(
                json.dumps(
                    required_jobs,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            fact_core = {
                "provider": str(ci_fact.get("provider") or ""),
                "repo_id": str(ci_fact.get("repo_id") or ""),
                "repository_id": positive_int(
                    ci_fact.get("repository_id")
                ),
                "target_ref": str(ci_fact.get("target_ref") or ""),
                "target_sha": str(ci_fact.get("target_sha") or ""),
                "workflow_id": positive_int(ci_fact.get("workflow_id")),
                "workflow_path": str(
                    ci_fact.get("workflow_path") or ""
                ),
                "required_jobs_digest": str(
                    ci_fact.get("required_jobs_digest") or ""
                ),
                "expected_app_id": positive_int(
                    ci_fact.get("expected_app_id")
                ),
                "run_id": positive_int(ci_fact.get("run_id")),
                "run_number": positive_int(ci_fact.get("run_number")),
                "run_attempt": positive_int(ci_fact.get("run_attempt")),
                "check_suite_id": positive_int(
                    ci_fact.get("check_suite_id")
                ),
                "signal_state": str(ci_fact.get("signal_state") or ""),
                "observed_at": (
                    observed_at.isoformat() if observed_at else ""
                ),
                "checks_digest": str(
                    ci_fact.get("checks_digest") or ""
                ),
                "policy_digest": str(
                    ci_fact.get("policy_digest") or ""
                ),
            }
            expected_probe_digest = hashlib.sha256(
                json.dumps(
                    fact_core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            checks_digest = fact_core["checks_digest"]
            expected_sha = str(goal.get("target_sha") or "")
            if (
                not isinstance(binding, dict)
                or str(binding.get("user_id") or "") != str(user_id)
                or str(binding.get("goal_revision") or "")
                != str(goal_revision)
                or str(binding.get("workspace_id") or "")
                != str(scope.get("workspace_id") or "")
                or str(binding.get("repo_id") or "")
                != str(scope.get("repo_id") or "")
                or str(binding.get("target_ref") or "")
                != str(scope.get("target_ref") or "")
                or str(binding.get("target_sha") or "") != expected_sha
                or policy_core["schema_version"]
                != GitHubActionsCIProvider.CONTRACT
                or policy_core["provider"]
                != GitHubActionsCIProvider.PROVIDER
                or policy_core["api_origin"]
                != GitHubActionsCIProvider.API_ORIGIN
                or policy_core["event"] != "push"
                or not required_jobs
                or any(
                    policy_core[key] < 1
                    for key in (
                        "repository_id",
                        "workflow_id",
                        "expected_app_id",
                    )
                )
                or str(policy.get("policy_digest") or "")
                != expected_policy_digest
                or fact_core["policy_digest"] != expected_policy_digest
                or fact_core["provider"]
                != GitHubActionsCIProvider.PROVIDER
                or fact_core["repo_id"] != policy_core["repo_id"]
                or fact_core["repo_id"]
                != str(scope.get("repo_id") or "")
                or fact_core["repository_id"]
                != policy_core["repository_id"]
                or fact_core["target_ref"]
                != str(scope.get("target_ref") or "")
                or fact_core["target_sha"] != expected_sha
                or fact_core["workflow_id"] != policy_core["workflow_id"]
                or fact_core["workflow_path"]
                != policy_core["workflow_path"]
                or fact_core["required_jobs_digest"]
                != expected_required_jobs_digest
                or fact_core["expected_app_id"]
                != policy_core["expected_app_id"]
                or fact_core["signal_state"] not in {"present", "clear"}
                or any(
                    fact_core[key] < 1
                    for key in (
                        "run_id",
                        "run_number",
                        "run_attempt",
                        "check_suite_id",
                    )
                )
                or len(checks_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in checks_digest
                )
                or str(ci_fact.get("probe_digest") or "")
                != expected_probe_digest
            ):
                return {
                    "status": "ignored",
                    "reason": "ci_fact_binding_mismatch",
                }

            kind = "ci_failed"
            component = ProjectGuardianEvaluator.SIGNAL_COMPONENTS[kind]
            producer = ProjectGuardianEvaluator.SIGNAL_PRODUCERS[kind]
            policy_identity = hashlib.sha256(
                expected_policy_digest.encode("utf-8")
            ).hexdigest()[:20]
            provenance_root = (
                f"{component}:github_actions_policy_{policy_identity}"
            )
            evidence_id = "pgce_" + hashlib.sha256(
                json.dumps(
                    {
                        "producer_id": producer["producer_id"],
                        "goal_id": goal_id,
                        "goal_revision": goal_revision,
                        **fact_core,
                        "probe_digest": expected_probe_digest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            occurred_at = observed_at.isoformat()
            valid_until = (
                observed_at + timedelta(minutes=30)
            ).isoformat()
            session_id = (
                f"project-guardian-ci-{policy_identity[:12]}"
            )
            receipt_id = ProjectGuardianEvaluator.producer_receipt_id_for(
                kind=kind,
                state=fact_core["signal_state"],
                source_component=component,
                provenance_root=provenance_root,
                evidence_id=evidence_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                scope=scope,
                valid_until=valid_until,
                producer_id=str(producer["producer_id"]),
                trust_class=str(producer["trust_class"]),
                user_id=user_id,
                session_id=session_id,
                occurred_at=occurred_at,
            )
            event_id = "pgse_" + hashlib.sha256(
                f"{receipt_id}:{occurred_at}".encode("utf-8")
            ).hexdigest()[:24]
            event = VeyraEvent(
                type=EventType.OBSERVATION,
                source=EventSource(
                    channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
                    user_id=user_id,
                    session_id=session_id,
                ),
                payload={
                    "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
                    "project_guardian_signal": {
                        "kind": kind,
                        "state": fact_core["signal_state"],
                        "source_component": component,
                        "provenance_root": provenance_root,
                        "evidence_id": evidence_id,
                        "goal_id": goal_id,
                        "goal_revision": goal_revision,
                        "scope": {
                            key: str(scope.get(key) or "")
                            for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                        },
                        "valid_until": valid_until,
                        "producer_attestation": {
                            "schema_version": (
                                ProjectGuardianEvaluator
                                .PRODUCER_ATTESTATION_SCHEMA
                            ),
                            "producer_id": str(producer["producer_id"]),
                            "trust_class": str(producer["trust_class"]),
                            "admission_source": (
                                "project_guardian_signal_ingress"
                            ),
                            "receipt_id": receipt_id,
                        },
                    },
                },
                event_id=event_id,
                timestamp=occurred_at,
                occurred_at=occurred_at,
                evidence_refs=[
                    {
                        "ref_id": evidence_id,
                        "source": component,
                        "is_fact": True,
                    }
                ],
                privacy_scope="user",
            )
            commit_time = datetime.now(timezone.utc)
            if not self._project_guardian_ci_observation_time_valid(
                observed_at,
                commit_time,
            ):
                return {
                    "status": "ignored",
                    "reason": "ci_fact_observation_time_invalid",
                }
            return self._admit_project_guardian_signal(event)

    def _publish_project_guardian_deployment_intent(
        self,
        *,
        ingress_capability: object,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        expected_goal_state_revision: int,
        target_sha: str,
        target_environment: str,
        transition: str,
        operation_digest: str,
        occurred_at: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Admit one explicit, Goal-bound local deployment-intent command."""

        if (
            ingress_capability
            is not self._project_guardian_intent_ingress_capability
        ):
            return {
                "status": "ignored",
                "reason": "untrusted_project_guardian_intent_ingress",
            }

        observed_at = ProjectGuardianSignalLedger._time(occurred_at)
        selected_user = str(user_id or "").strip()
        selected_goal = str(goal_id or "").strip()
        selected_revision = str(goal_revision or "").strip()
        selected_environment = str(target_environment or "").strip()
        selected_transition = str(transition or "").strip().lower()
        selected_sha = str(target_sha or "").strip().lower()
        selected_operation_digest = str(operation_digest or "").strip().lower()
        selected_session = str(session_id or "").strip()
        selected_state_revision = (
            expected_goal_state_revision
            if isinstance(expected_goal_state_revision, int)
            and not isinstance(expected_goal_state_revision, bool)
            else 0
        )

        with self.state_store.writer_transaction():
            admitted_at = datetime.now(timezone.utc)
            if not self._project_guardian_intent_time_valid(
                observed_at,
                admitted_at,
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_time_invalid",
                }
            fabric = self._fabric_snapshot()
            guardian = self._project_guardian_snapshot()
            if guardian["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "project_guardian_disabled",
                }
            if fabric["mode"] == "disabled":
                return {
                    "status": "disabled",
                    "reason": "event_fabric_disabled",
                }
            if fabric["mode"] not in self.MODES:
                return {
                    "status": "degraded",
                    "reason": "event_fabric_unavailable",
                }
            if (
                not selected_user
                or len(selected_user) > 240
                or "\x00" in selected_user
                or not selected_goal
                or len(selected_goal) > 240
                or "\x00" in selected_goal
                or not selected_revision
                or len(selected_revision) > 120
                or "\x00" in selected_revision
                or not selected_environment
                or len(selected_environment) > 240
                or "\x00" in selected_environment
                or not selected_session
                or len(selected_session) > 240
                or "\x00" in selected_session
                or selected_transition not in {"declare", "withdraw"}
                or selected_state_revision < 1
                or len(selected_sha) not in {40, 64}
                or any(
                    character not in "0123456789abcdef"
                    for character in selected_sha
                )
                or len(selected_operation_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in selected_operation_digest
                )
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_command_invalid",
                }

            goals_state = self.state_store.read_json("user_goals.json")
            if goals_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "user_goals_state_corrupt",
                }
            goal = self._active_release_goal(
                goals_state,
                user_id=selected_user,
                goal_id=selected_goal,
                goal_revision=selected_revision,
                observed_at=observed_at,
            )
            if goal is None:
                return {
                    "status": "ignored",
                    "reason": "release_goal_not_active_or_mismatched",
                }
            active_from = ProjectGuardianSignalLedger._time(
                goal.get("active_from")
            )
            active_until = ProjectGuardianSignalLedger._time(
                goal.get("active_until")
            )
            if (
                active_from is None
                or active_until is None
                or not (active_from <= admitted_at <= active_until)
                or self._nonnegative_int(goal.get("state_revision"))
                != selected_state_revision
                or str(goal.get("target_sha") or "").strip().lower()
                != selected_sha
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_goal_state_mismatch",
                }
            scope = (
                goal.get("scope")
                if isinstance(goal.get("scope"), dict)
                else {}
            )
            if (
                str(scope.get("target_environment") or "")
                != selected_environment
                or any(
                    not str(scope.get(key) or "")
                    for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                )
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_scope_mismatch",
                }

            producer_state = self.state_store.read_json(
                "project_guardian_producer_state.json"
            )
            if producer_state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "project_guardian_producer_state_corrupt",
                }
            bindings = (
                producer_state.get("bindings")
                if isinstance(producer_state.get("bindings"), dict)
                else {}
            )
            binding = bindings.get(selected_goal)
            binding_id = (
                str(binding.get("binding_id") or "")
                if isinstance(binding, dict)
                else ""
            )
            remote_origin_digest = (
                str(binding.get("remote_origin_digest") or "")
                if isinstance(binding, dict)
                else ""
            )
            if (
                not isinstance(binding, dict)
                or not binding_id.startswith("pgwb_")
                or str(binding.get("goal_id") or "") != selected_goal
                or str(binding.get("user_id") or "") != selected_user
                or str(binding.get("goal_revision") or "")
                != selected_revision
                or str(binding.get("workspace_id") or "")
                != str(scope.get("workspace_id") or "")
                or str(binding.get("repo_id") or "")
                != str(scope.get("repo_id") or "")
                or str(binding.get("target_ref") or "")
                != str(scope.get("target_ref") or "")
                or str(binding.get("target_sha") or "") != selected_sha
                or len(remote_origin_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in remote_origin_digest
                )
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_binding_mismatch",
                }

            valid_until_time = min(
                observed_at + timedelta(minutes=30),
                active_until,
            )
            if valid_until_time < admitted_at:
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_expired",
                }
            kind = "deployment_intent"
            signal_state = (
                "present"
                if selected_transition == "declare"
                else "clear"
            )
            component = ProjectGuardianEvaluator.SIGNAL_COMPONENTS[kind]
            producer = ProjectGuardianEvaluator.SIGNAL_PRODUCERS[kind]
            binding_identity = hashlib.sha256(
                binding_id.encode("utf-8")
            ).hexdigest()[:20]
            provenance_root = (
                f"{component}:local_control_binding_{binding_identity}"
            )
            evidence_id = "pgie_" + hashlib.sha256(
                json.dumps(
                    {
                        "producer_id": producer["producer_id"],
                        "goal_id": selected_goal,
                        "goal_revision": selected_revision,
                        "goal_state_revision": selected_state_revision,
                        "target_sha": selected_sha,
                        "target_environment": selected_environment,
                        "transition": selected_transition,
                        "operation_digest": selected_operation_digest,
                        "occurred_at": observed_at.isoformat(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            occurred_at_text = observed_at.isoformat()
            valid_until = valid_until_time.isoformat()
            source_session = (
                "project-guardian-intent-"
                + hashlib.sha256(
                    selected_session.encode("utf-8")
                ).hexdigest()[:12]
            )
            receipt_id = ProjectGuardianEvaluator.producer_receipt_id_for(
                kind=kind,
                state=signal_state,
                source_component=component,
                provenance_root=provenance_root,
                evidence_id=evidence_id,
                goal_id=selected_goal,
                goal_revision=selected_revision,
                scope=scope,
                valid_until=valid_until,
                producer_id=str(producer["producer_id"]),
                trust_class=str(producer["trust_class"]),
                user_id=selected_user,
                session_id=source_session,
                occurred_at=occurred_at_text,
            )
            event_id = "pgse_" + hashlib.sha256(
                f"{receipt_id}:{occurred_at_text}".encode("utf-8")
            ).hexdigest()[:24]
            event = VeyraEvent(
                type=EventType.OBSERVATION,
                source=EventSource(
                    channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
                    user_id=selected_user,
                    session_id=source_session,
                ),
                payload={
                    "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
                    "project_guardian_signal": {
                        "kind": kind,
                        "state": signal_state,
                        "source_component": component,
                        "provenance_root": provenance_root,
                        "evidence_id": evidence_id,
                        "goal_id": selected_goal,
                        "goal_revision": selected_revision,
                        "scope": {
                            key: str(scope.get(key) or "")
                            for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                        },
                        "valid_until": valid_until,
                        "producer_attestation": {
                            "schema_version": (
                                ProjectGuardianEvaluator
                                .PRODUCER_ATTESTATION_SCHEMA
                            ),
                            "producer_id": str(producer["producer_id"]),
                            "trust_class": str(producer["trust_class"]),
                            "admission_source": (
                                "project_guardian_signal_ingress"
                            ),
                            "receipt_id": receipt_id,
                        },
                    },
                },
                event_id=event_id,
                timestamp=occurred_at_text,
                occurred_at=occurred_at_text,
                evidence_refs=[
                    {
                        "ref_id": evidence_id,
                        "source": component,
                        "is_fact": True,
                    }
                ],
                privacy_scope="user",
            )
            commit_time = datetime.now(timezone.utc)
            if (
                not self._project_guardian_intent_time_valid(
                    observed_at,
                    commit_time,
                )
                or commit_time > valid_until_time
            ):
                return {
                    "status": "ignored",
                    "reason": "deployment_intent_time_invalid",
                }
            return self._admit_project_guardian_signal(event)

    def _admit_project_guardian_signal(
        self,
        event: VeyraEvent,
    ) -> dict[str, Any]:
        admission = self.event_inbox.enqueue(event)
        envelope = (
            admission.get("envelope")
            if isinstance(admission.get("envelope"), dict)
            else {}
        )
        try:
            signal_result = self.project_guardian_signals.record_envelope(
                envelope
            )
            admission["signal_ledger_status"] = signal_result.get("status")
        except Exception as exc:
            admission["signal_ledger_status"] = "degraded"
            admission["signal_ledger_error_type"] = type(exc).__name__
            self._record_error(
                "project_guardian_signal_ledger_error",
                event.event_id,
                exc,
            )
        return admission

    def begin(self, event: VeyraEvent) -> dict[str, Any]:
        """Claim and observe a foreground event before the existing turn runs."""

        with self.state_store.writer_transaction():
            return self._begin_locked(
                event,
                mode=self._fabric_snapshot()["mode"],
            )

    def _begin_locked(
        self,
        event: VeyraEvent,
        *,
        mode: str,
    ) -> dict[str, Any]:
        if self._is_project_guardian_signal(event):
            return {
                "status": "suppressed",
                "event_id": event.event_id,
                "reason": "reserved_project_guardian_signal_ingress",
                "finalize_allowed": False,
            }
        if self._is_project_guardian_event(event):
            return {
                "status": "suppressed",
                "event_id": event.event_id,
                "reason": "reserved_project_guardian_projection_ingress",
                "finalize_allowed": False,
            }
        if mode == "disabled":
            return {"status": "disabled"}
        if mode == "record_only":
            try:
                admission = self.event_inbox.enqueue(event)
                return {
                    "status": "recorded",
                    "event_id": admission.get("event_id"),
                }
            except Exception as exc:
                self._record_error("event_record_only_error", event.event_id, exc)
                return {"status": "degraded", "error_type": type(exc).__name__}
        consumer_id = f"awareness-shadow-foreground-{uuid4().hex[:12]}"
        claimed = False
        canonical_event_id = event.event_id
        situation_id = ""
        try:
            admission = self.event_inbox.enqueue_and_claim(
                event,
                consumer_id,
                lease_seconds=120,
            )
            claimed = bool(admission.get("claimed"))
            canonical_event_id = str(
                admission.get("canonical_event_id") or event.event_id
            )
            canonical_envelope = admission.get("canonical_envelope")
            claimed_envelope = admission.get("claimed_envelope")
            envelope = (
                claimed_envelope
                if isinstance(claimed_envelope, dict)
                else canonical_envelope
            )
            if not isinstance(envelope, dict):
                raise ValueError("event inbox did not return a canonical envelope")
            observed_event = VeyraEvent.from_dict(envelope)
            # ``observe`` is replay-safe and also drains a durable trace outbox.
            # Calling it for duplicates repairs a crash between state commit and
            # JSONL trace projection instead of silently trusting prior state.
            situation = self.situation_evaluator.observe(
                observed_event,
                situation_id=self._explicit_situation_id(observed_event),
                salience_components=self._explicit_salience(observed_event),
                observation=self._explicit_observation(observed_event),
            )
            self._project_general_situation(situation, observed_event)
            situation_id = str(situation.get("situation_id") or "")
            same_event_delivery = canonical_event_id == event.event_id
            if not claimed:
                return {
                    "status": str(admission.get("status") or "duplicate"),
                    "event_id": canonical_event_id,
                    "situation_id": situation_id,
                    # Resolution writes are atomically idempotent. A duplicate
                    # can repair an earlier failed finalize without duplicating
                    # lifecycle history under concurrent callers, but a
                    # different event suppressed by a shared dedupe key must
                    # never project its result into the canonical event.
                    "finalize_allowed": same_event_delivery,
                }
            self.event_inbox.complete(
                canonical_event_id,
                consumer_id,
                {
                    "status": "observed",
                    "situation_id": situation_id,
                },
            )
            return {
                "status": "observed",
                "event_id": canonical_event_id,
                "situation_id": situation_id,
                "finalize_allowed": same_event_delivery,
            }
        except Exception as exc:
            if claimed:
                self._fail_claim(canonical_event_id, consumer_id, exc)
            self._record_error(
                "shadow_awareness_intake_error",
                canonical_event_id,
                exc,
            )
            return {
                "status": "degraded",
                "event_id": canonical_event_id,
                "situation_id": situation_id or None,
                # If observation committed before a later inbox completion
                # failure, preserve this turn's decision/outcome projection.
                "finalize_allowed": bool(
                    situation_id
                    and claimed
                    and canonical_event_id == event.event_id
                ),
                "error_type": type(exc).__name__,
            }

    def finalize(
        self,
        event: VeyraEvent,
        result: LoopResult,
        trace: dict[str, Any],
        *,
        situation_id: str | None = None,
        canonical_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the already-made decision/outcome after observation completed."""

        with self.state_store.writer_transaction():
            if self._fabric_snapshot()["mode"] != "shadow":
                return None
            return self._finalize_shadow(
                event,
                result,
                trace,
                situation_id=situation_id,
                canonical_event_id=canonical_event_id,
            )

    def _finalize_shadow(
        self,
        event: VeyraEvent,
        result: LoopResult,
        trace: dict[str, Any],
        *,
        situation_id: str | None = None,
        canonical_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._fabric_snapshot()["mode"] != "shadow":
            return None
        selected_situation_id = str(situation_id or "")
        try:
            if result.event_id != event.event_id:
                raise ValueError("result event does not match the finalized event")
            if (
                canonical_event_id is not None
                and str(canonical_event_id) != event.event_id
            ):
                raise ValueError(
                    "canonical event does not match the finalized event"
                )
            situation = (
                self.situation_evaluator.get(
                    selected_situation_id,
                    user_id=event.source.user_id,
                    session_id=event.source.session_id,
                )
                if selected_situation_id
                else self._situation_for_event(event)
            )
            if (
                isinstance(situation, dict)
                and not self._situation_matches_event(situation, event)
            ):
                raise ValueError(
                    "situation does not belong to the finalized event"
                )
            if not isinstance(situation, dict):
                # A foreground/background interleaving or a repaired intake can
                # leave no lookup result for the delivered id. Observation is
                # idempotent, so upsert the source event rather than dropping
                # the already-made decision and outcome.
                situation = self.situation_evaluator.observe(
                    event,
                    situation_id=self._explicit_situation_id(event),
                    salience_components=self._explicit_salience(event),
                    observation=self._explicit_observation(event),
                )
            if not self._situation_matches_event(situation, event):
                raise ValueError(
                    "observed situation does not belong to the finalized event"
                )
            selected_situation_id = str(situation.get("situation_id") or "")
            trace_id = str(trace.get("trace_id") or "")
            trace_ref = {
                "ref_id": trace_id,
                "source": "runtime_trace",
                "epistemic_status": "observation",
                "is_fact": True,
            }
            verification = (
                result.artifacts.get("verification")
                if isinstance(result.artifacts.get("verification"), dict)
                else {}
            )
            execution_trace = (
                result.artifacts.get("execution_trace")
                if isinstance(result.artifacts.get("execution_trace"), dict)
                else {}
            )
            execution_trace_id = str(execution_trace.get("trace_id") or "")
            persisted_verification = self._persisted_verification(
                event_id=event.event_id,
                result_event_id=result.event_id,
                trace_id=execution_trace_id,
                expected_route=result.route.value,
                expected_result_status=result.status,
                expected_task_id=self._expected_task_id(event, result),
                expected_verification=verification,
            )
            verified_outcome = persisted_verification is not None
            authoritative_verification = (
                persisted_verification.get("verification")
                if isinstance(persisted_verification, dict)
                and isinstance(persisted_verification.get("verification"), dict)
                else verification
            )
            claimed_verification_status = next(
                (
                    status
                    for status in (
                        str(result.status or ""),
                        str(verification.get("status") or ""),
                    )
                    if status in VERIFIED_RESULT_STATUSES
                ),
                "",
            )
            verification_pending = bool(
                claimed_verification_status and not verified_outcome
            )
            verification_summary = (
                {
                    "status": "verification_pending",
                    "claimed_status": claimed_verification_status,
                    "verdict": "persisted_execution_evidence_missing",
                    "next_action": "continue_evaluation",
                }
                if verification_pending
                else {
                    key: authoritative_verification.get(key)
                    for key in ("status", "verdict", "next_action")
                    if key in authoritative_verification
                }
            )
            outcome_value = {
                "route": (
                    str(persisted_verification.get("route") or "")
                    if verified_outcome
                    else result.route.value
                ),
                "status": (
                    str(persisted_verification.get("status") or "")
                    if verified_outcome
                    else "verification_pending"
                    if verification_pending
                    else result.status
                ),
                "verification": verification_summary,
            }
            decision_value = {
                "route": result.route.value,
                "status": (
                    "verification_pending"
                    if verification_pending
                    else result.status
                ),
                "risk_level": result.risk_level.value,
            }
            verification_ref = {
                "ref_id": (
                    f"execution_trace:{execution_trace_id}:verification"
                    if execution_trace_id
                    else f"action_record:{event.event_id}:verification"
                ),
                "source": "execution_trace" if execution_trace_id else "action_record",
                "epistemic_status": (
                    "verified"
                    if verified_outcome
                    else "reference"
                ),
                "is_fact": verified_outcome,
            }
            resolution_key = self._resolution_key(
                event_id=str(canonical_event_id or event.event_id),
                decision=decision_value,
                outcome=outcome_value,
                outcome_is_fact=verified_outcome,
            )
            resolved: dict[str, Any] | None = None
            last_error: Exception | None = None
            # The combined mutation is idempotent, so one immediate retry safely
            # repairs transient atomic-write failures without duplicate history.
            for _ in range(2):
                try:
                    resolved = self.situation_evaluator.record_resolution(
                        selected_situation_id,
                        idempotency_key=resolution_key,
                        expected_source_event_id=event.event_id,
                        expected_correlation_id=(
                            event.correlation_id or event.event_id
                        ),
                        decision=decision_value,
                        outcome=outcome_value,
                        user_id=event.source.user_id,
                        session_id=event.source.session_id,
                        decision_evidence_refs=[trace_ref],
                        outcome_evidence_refs=(
                            [trace_ref, verification_ref]
                            if verification
                            else [trace_ref]
                        ),
                        decision_source="veyra_policy",
                        outcome_source=(
                            "verifier"
                            if verified_outcome
                            else "runtime_result"
                        ),
                        outcome_evidence_verified=verified_outcome,
                        decision_status=self._decision_status(result),
                        outcome_status=(
                            "monitoring"
                            if verification_pending
                            else self._outcome_status(result)
                        ),
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if resolved is None:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("situation resolution did not return state")
            self._project_general_situation(resolved, event)
            return {
                "situation_id": selected_situation_id,
                "correlation_id": resolved.get("correlation_id"),
                "status": resolved.get("status"),
                "decision_id": (
                    resolved.get("decision", {}).get("decision_id")
                    if isinstance(resolved.get("decision"), dict)
                    else None
                ),
                "outcome_id": (
                    resolved.get("outcome", {}).get("outcome_id")
                    if isinstance(resolved.get("outcome"), dict)
                    else None
                ),
                "shadow_only": True,
            }
        except Exception as exc:
            self._record_error(
                "shadow_awareness_finalize_error",
                event.event_id,
                exc,
                situation_id=selected_situation_id,
            )
            return {
                "situation_id": selected_situation_id or None,
                "status": "degraded",
                "error_type": type(exc).__name__,
                "shadow_only": True,
            }

    @staticmethod
    def _resolution_key(
        *,
        event_id: str,
        decision: dict[str, Any],
        outcome: dict[str, Any],
        outcome_is_fact: bool,
    ) -> str:
        encoded = json.dumps(
            {
                "event_id": event_id,
                "decision": decision,
                "outcome": outcome,
                "outcome_is_fact": bool(outcome_is_fact),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"sires_{hashlib.sha256(encoded).hexdigest()[:32]}"

    def process_pending(self, *, limit: int = 20) -> dict[str, Any]:
        """Project pending background events into situations, never actions."""

        if self._fabric_snapshot()["mode"] == "disabled":
            return {
                "status": "disabled",
                "processed_count": 0,
                "suppressed_count": 0,
                "failed_count": 0,
                "processed": [],
                "suppressed": [],
                "failed": [],
                "inbox": self.event_inbox.stats(),
            }
        processed: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        consumer_id = "awareness-shadow-background"
        trace_recovery_error: str | None = None
        signal_reconciliation: dict[str, Any]
        try:
            signal_reconciliation = (
                self.project_guardian_signals.reconcile_event_inbox()
            )
        except Exception as exc:
            signal_reconciliation = {
                "status": "degraded",
                "error_type": type(exc).__name__,
            }
            self._record_error(
                "project_guardian_signal_reconciliation_error",
                "",
                exc,
            )
        try:
            self.situation_evaluator.flush_trace_outbox()
        except Exception as exc:
            trace_recovery_error = type(exc).__name__
            self._record_error(
                "situation_trace_recovery_error",
                "",
                exc,
            )
        for _ in range(max(0, min(int(limit), 100))):
            event_id = ""
            try:
                with self.state_store.writer_transaction():
                    fabric = self._fabric_snapshot()
                    if fabric["mode"] == "disabled":
                        break
                    claimed = self.event_inbox.claim(
                        consumer_id,
                        lease_seconds=60,
                    )
                    if not isinstance(claimed, dict):
                        break
                    event_id = str(claimed.get("event_id") or "")
                    event = VeyraEvent.from_dict(claimed)
                    if self._is_project_guardian_signal(event):
                        signal_result = (
                            self.project_guardian_signals.record_envelope(
                                claimed
                            )
                        )
                        signal_status = str(
                            signal_result.get("status") or "ignored"
                        )
                        if signal_status not in {"recorded", "stale"}:
                            self.event_inbox.complete(
                                event_id,
                                consumer_id,
                                {
                                    "status": "suppressed",
                                    "reason": (
                                        "invalid_project_guardian_signal"
                                    ),
                                },
                            )
                            suppressed.append(
                                {
                                    "event_id": event_id,
                                    "status": "suppressed",
                                    "reason": (
                                        "invalid_project_guardian_signal"
                                    ),
                                }
                            )
                            continue
                        self.event_inbox.complete(
                            event_id,
                            consumer_id,
                            {
                                "status": "signal_recorded",
                                "signal_ledger_status": signal_status,
                            },
                        )
                        processed.append(
                            {
                                "event_id": event_id,
                                "situation_id": None,
                                "status": "signal_recorded",
                            }
                        )
                        continue
                    if self._is_project_guardian_event(event):
                        binding = ProjectGuardianRuntime.validate_projection_event(
                            event
                        )
                        guardian = self._project_guardian_snapshot()
                        suppression_reason = ""
                        if binding is None:
                            suppression_reason = (
                                "invalid_project_guardian_projection"
                            )
                        elif (
                            guardian["mode"] != "shadow"
                            or binding["guardian_mode_epoch"]
                            != guardian["mode_epoch"]
                            or binding["event_fabric_mode_epoch"]
                            != fabric["mode_epoch"]
                        ):
                            suppression_reason = (
                                "stale_project_guardian_mode_epoch"
                            )
                        if suppression_reason:
                            self.event_inbox.complete(
                                event_id,
                                consumer_id,
                                {
                                    "status": "suppressed",
                                    "reason": suppression_reason,
                                },
                            )
                            ProjectGuardianRuntime.record_projection_result(
                                self.state_store,
                                event,
                                status="suppressed",
                            )
                            suppressed.append(
                                {
                                    "event_id": event_id,
                                    "status": "suppressed",
                                    "reason": suppression_reason,
                                }
                            )
                            continue
                        situation_id = self._explicit_situation_id(event)
                        situation = self.situation_evaluator.observe(
                            event,
                            situation_id=situation_id,
                            salience_components=self._explicit_salience(event),
                            observation=self._explicit_observation(event),
                            status=self._explicit_situation_status(event),
                            observation_sequence=binding[
                                "projection_sequence"
                            ],
                            observation_id=(
                                f"{binding['candidate_id']}:"
                                f"{binding['candidate_revision']}"
                            ),
                            allow_terminal_reopen=(
                                binding["projection_kind"] == "reopen"
                            ),
                        )
                        self.event_inbox.complete(
                            event_id,
                            consumer_id,
                            {
                                "status": "observed",
                                "situation_id": situation.get("situation_id"),
                            },
                        )
                        ProjectGuardianRuntime.record_projection_result(
                            self.state_store,
                            event,
                            status="projected",
                            situation_id=str(
                                situation.get("situation_id") or ""
                            ),
                        )
                    else:
                        situation = self.situation_evaluator.observe(
                            event,
                            situation_id=self._explicit_situation_id(event),
                            salience_components=self._explicit_salience(event),
                            observation=self._explicit_observation(event),
                        )
                        self.event_inbox.complete(
                            event_id,
                            consumer_id,
                            {
                                "status": "observed",
                                "situation_id": situation.get("situation_id"),
                            },
                        )
                self._project_general_situation(situation, event)
                processed.append(
                    {
                        "event_id": event_id,
                        "situation_id": situation.get("situation_id"),
                        "status": "observed",
                    }
                )
            except Exception as exc:
                retry_status = self._fail_claim(event_id, consumer_id, exc)
                failed.append(
                    {
                        "event_id": event_id,
                        "status": retry_status,
                        "error_type": type(exc).__name__,
                    }
                )
        return {
            "status": (
                "success"
                if not failed and trace_recovery_error is None
                else "degraded"
            ),
            "processed_count": len(processed),
            "suppressed_count": len(suppressed),
            "failed_count": len(failed),
            "processed": processed,
            "suppressed": suppressed,
            "failed": failed,
            "trace_recovery": {
                "status": "success" if trace_recovery_error is None else "degraded",
                "error_type": trace_recovery_error,
            },
            "signal_reconciliation": signal_reconciliation,
            "inbox": self.event_inbox.stats(),
        }

    def reconcile_general_situations(
        self,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fail-open maintenance for ActiveLoop restart/replay recovery."""

        try:
            state = self.state_store.read_json("situation_state.json")
            if state.get("_state_corrupt") is True:
                return {
                    "status": "degraded",
                    "reason": "situation_state_corrupt",
                    "route_change_allowed": False,
                }
            raw = state.get("situations")
            situations = (
                list(raw.values())
                if isinstance(raw, dict)
                else raw
                if isinstance(raw, list)
                else []
            )
            return self.general_situations.reconcile(
                [item for item in situations if isinstance(item, dict)],
                limit=limit,
            )
        except Exception as exc:
            self._record_error(
                "general_situation_reconcile_error",
                "",
                exc,
            )
            return {
                "status": "degraded",
                "error_type": type(exc).__name__,
                "route_change_allowed": False,
            }

    def _project_general_situation(
        self,
        situation: dict[str, Any],
        event: VeyraEvent,
    ) -> dict[str, Any]:
        """Run the informational pipeline without escaping into routing."""

        try:
            grouped = self.general_situations.ingest_child(
                situation,
                event=event,
            )
            parent = grouped.get("general_situation")
            if not isinstance(parent, dict):
                return {
                    "status": str(grouped.get("status") or "not_grouped"),
                    "general_situation": grouped,
                    "route_change_allowed": False,
                }
            assessment = self.general_attention.assess(parent)
            proposal = self.suggestion_outbox.consider(
                parent,
                assessment,
                user_id=event.source.user_id,
                session_id=event.source.session_id,
            )
            return {
                "status": "success",
                "general_situation_status": grouped.get("status"),
                "attention_status": assessment.get("status"),
                "suggestion_status": proposal.get("status"),
                "route_change_allowed": False,
            }
        except Exception as exc:
            self._record_error(
                "general_situation_pipeline_error",
                event.event_id,
                exc,
            )
            return {
                "status": "degraded",
                "error_type": type(exc).__name__,
                "route_change_allowed": False,
            }

    def _situation_for_event(self, event: VeyraEvent) -> dict[str, Any] | None:
        items = self.situation_evaluator.list(
            user_id=event.source.user_id,
            session_id=event.source.session_id,
            correlation_id=event.correlation_id or event.event_id,
            limit=10,
        )
        return next(
            (
                item
                for item in items
                if str(item.get("source_event_id") or "") == event.event_id
            ),
            None,
        )

    @staticmethod
    def _situation_matches_event(
        situation: dict[str, Any],
        event: VeyraEvent,
    ) -> bool:
        return (
            str(situation.get("source_event_id") or "") == event.event_id
            and str(situation.get("correlation_id") or "")
            == str(event.correlation_id or event.event_id)
        )

    def _fail_claim(
        self,
        event_id: str,
        consumer_id: str,
        exc: Exception,
    ) -> str:
        try:
            failure = self.event_inbox.fail(
                event_id,
                consumer_id,
                exc,
                retry=True,
                retry_delay_seconds=5,
            )
            return str(failure.get("status") or "unknown")
        except Exception:
            return "claim_lost"

    def _record_error(
        self,
        kind: str,
        event_id: str,
        exc: Exception,
        *,
        situation_id: str | None = None,
    ) -> None:
        record = {
            "kind": kind,
            "event_id": event_id,
            "error_type": type(exc).__name__,
            "timestamp": utc_now_iso(),
        }
        if situation_id:
            record["situation_id"] = situation_id
        try:
            self.state_store.append_jsonl("alert_log.jsonl", record)
        except Exception:
            # Shadow observability must never become part of the user-facing
            # availability path.
            return

    def _persisted_verification(
        self,
        *,
        event_id: str,
        result_event_id: str,
        trace_id: str,
        expected_route: str,
        expected_result_status: str,
        expected_task_id: str | None,
        expected_verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        expected_status = str(expected_verification.get("status") or "")
        if (
            not trace_id
            or result_event_id != event_id
            or not expected_route
            or not expected_task_id
            or not expected_status.startswith("verified_")
        ):
            return None
        try:
            rows = self.state_store.read_jsonl("execution_trace.jsonl", limit=2000)
        except Exception:
            return None
        for row in reversed(rows):
            if str(row.get("trace_id") or "") != trace_id:
                continue
            if str(row.get("event_id") or "") != event_id:
                return None
            if str(row.get("route") or "") != expected_route:
                return None
            if str(row.get("task_id") or "") != expected_task_id:
                return None
            persisted_status = str(row.get("status") or "")
            if not self._result_status_matches_trace(
                expected_result_status,
                persisted_status,
            ):
                return None
            verification = (
                row.get("verification")
                if isinstance(row.get("verification"), dict)
                else {}
            )
            if (
                str(verification.get("status") or "") != expected_status
                or persisted_status != expected_status
            ):
                return None
            for key in ("verdict", "next_action", "evidence"):
                if (
                    key in expected_verification
                    and expected_verification.get(key) != verification.get(key)
                ):
                    return None
            evidence = verification.get("evidence")
            if not (
                isinstance(evidence, dict)
                and bool(evidence)
                or isinstance(evidence, list)
                and bool(evidence)
            ):
                return None
            return row
        return None

    @staticmethod
    def _expected_task_id(event: VeyraEvent, result: LoopResult) -> str | None:
        candidates: list[str] = []
        payload_task_id = str(event.payload.get("task_id") or "")
        if payload_task_id:
            candidates.append(payload_task_id)
        for key in ("execution_result", "task_packet"):
            item = (
                result.artifacts.get(key)
                if isinstance(result.artifacts.get(key), dict)
                else {}
            )
            task_id = str(item.get("task_id") or "")
            if task_id:
                candidates.append(task_id)
        if not candidates and result.route in {Route.PROBE, Route.SKILL}:
            candidates.append(event.event_id)
        unique = set(candidates)
        return candidates[0] if len(unique) == 1 else None

    @staticmethod
    def _result_status_matches_trace(result_status: str, trace_status: str) -> bool:
        return result_status == trace_status or (
            result_status == "success" and trace_status == "verified_success"
        )

    @staticmethod
    def _explicit_salience(event: VeyraEvent) -> dict[str, Any]:
        components = event.payload.get("salience_components")
        return components if isinstance(components, dict) else {}

    @staticmethod
    def _explicit_observation(event: VeyraEvent) -> dict[str, Any] | None:
        if event.type != EventType.OBSERVATION:
            return None
        observation = event.payload.get("observation")
        return copy.deepcopy(observation) if isinstance(observation, dict) else None

    @staticmethod
    def _explicit_situation_id(event: VeyraEvent) -> str | None:
        binding = ProjectGuardianRuntime.validate_projection_event(event)
        if binding is None:
            return None
        return str(event.payload.get("situation_id") or "") or None

    @staticmethod
    def _is_project_guardian_event(event: VeyraEvent) -> bool:
        return str(event.source.channel or "") == "project_guardian"

    @staticmethod
    def _is_project_guardian_signal(event: VeyraEvent) -> bool:
        # The channel itself is reserved. Invalid envelopes on it are rejected
        # or suppressed instead of falling through to generic Situation
        # projection.
        return str(event.source.channel or "") == "project_guardian_signal"

    @staticmethod
    def _active_release_goal(
        goals_state: dict[str, Any],
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        observed_at: datetime,
    ) -> dict[str, Any] | None:
        goals = (
            goals_state.get("goals")
            if isinstance(goals_state.get("goals"), list)
            else []
        )
        for raw in goals:
            if not isinstance(raw, dict):
                continue
            scope = (
                raw.get("scope")
                if isinstance(raw.get("scope"), dict)
                else {}
            )
            active_from = ProjectGuardianSignalLedger._time(
                raw.get("active_from")
            )
            active_until = ProjectGuardianSignalLedger._time(
                raw.get("active_until")
            )
            target_sha = str(raw.get("target_sha") or "").strip().lower()
            try:
                state_revision = (
                    0
                    if isinstance(raw.get("state_revision"), bool)
                    else int(raw.get("state_revision"))
                )
            except (TypeError, ValueError):
                state_revision = 0
            if (
                str(raw.get("schema_version") or "")
                != ProjectGuardianEvaluator.GOAL_SCHEMA
                or str(raw.get("source") or "")
                != ProjectGuardianEvaluator.GOAL_SOURCE
                or str(raw.get("kind") or "")
                != ProjectGuardianEvaluator.GOAL_KIND
                or str(raw.get("status") or "") != "active"
                or str(raw.get("user_id") or "") != str(user_id)
                or str(raw.get("goal_id") or "") != str(goal_id)
                or str(raw.get("revision") or "")
                != str(goal_revision)
                or active_from is None
                or active_until is None
                or not (active_from <= observed_at <= active_until)
                or state_revision < 1
                or len(target_sha) not in {40, 64}
                or any(
                    character not in "0123456789abcdef"
                    for character in target_sha
                )
                or any(
                    not str(scope.get(key) or "")
                    for key in ProjectGuardianEvaluator.SCOPE_FIELDS
                )
            ):
                continue
            return copy.deepcopy(raw)
        return None

    @staticmethod
    def _explicit_situation_status(event: VeyraEvent) -> str:
        return (
            "closed"
            if str(event.payload.get("situation_status") or "") == "closed"
            else "observed"
        )

    def _fabric_snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        section = config.get("event_awareness") if isinstance(config, dict) else {}
        section = section if isinstance(section, dict) else {}
        mode = (
            str(section.get("mode") or self.mode or "record_only")
            .strip()
            .lower()
        )
        return {
            "mode": mode if mode in self.MODES else "record_only",
            "mode_epoch": self._nonnegative_int(
                section.get("mode_epoch", self.mode_epoch)
            ),
        }

    def _project_guardian_snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        section = config.get("project_guardian") if isinstance(config, dict) else {}
        section = section if isinstance(section, dict) else {}
        mode = str(section.get("mode") or "disabled").strip().lower()
        return {
            "mode": mode if mode in self.MODES else "disabled",
            "mode_epoch": self._nonnegative_int(section.get("mode_epoch")),
        }

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _project_guardian_observation_time_valid(
        observed_at: datetime | None,
        reference_time: datetime,
    ) -> bool:
        return (
            observed_at is not None
            and observed_at >= reference_time - timedelta(minutes=2)
            and observed_at <= reference_time + timedelta(minutes=2)
        )

    @staticmethod
    def _project_guardian_ci_observation_time_valid(
        observed_at: datetime | None,
        reference_time: datetime,
    ) -> bool:
        return (
            observed_at is not None
            and observed_at >= reference_time - timedelta(minutes=30)
            and observed_at <= reference_time + timedelta(minutes=2)
        )

    @staticmethod
    def _project_guardian_intent_time_valid(
        observed_at: datetime | None,
        reference_time: datetime,
    ) -> bool:
        return (
            observed_at is not None
            and observed_at >= reference_time - timedelta(minutes=30)
            and observed_at <= reference_time + timedelta(minutes=2)
        )

    @staticmethod
    def _decision_status(result: LoopResult) -> str:
        if result.route in {Route.ASK_USER, Route.HUMAN_REVIEW}:
            return "waiting_human"
        if result.status in NON_TERMINAL_STATUSES:
            return "monitoring"
        return "decided"

    @staticmethod
    def _outcome_status(result: LoopResult) -> str:
        status = str(result.status or "").lower()
        if result.route in {Route.ASK_USER, Route.HUMAN_REVIEW}:
            return "waiting_human"
        if status in NON_TERMINAL_STATUSES:
            return "monitoring"
        if status in {"needs_more_probe", "needs_rollback"}:
            return "monitoring"
        if status in {"needs_action_proposal", "needs_parameters"}:
            return "waiting_human"
        if status == "partially_success":
            return "partial"
        if "indeterminate" in status:
            return "indeterminate"
        if any(token in status for token in ("error", "fail", "timeout", "unsupported")):
            return "failed"
        if result.route == Route.BLOCK:
            return "closed"
        return "resolved"
