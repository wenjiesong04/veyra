from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Protocol

from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_POLICY_REVISION,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_generation import (
    EXTENSION_GENERATION_POLICY_REVISION,
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_pipeline import (
    EXTENSION_PIPELINE_LIST_SCHEMA_VERSION,
    EXTENSION_PIPELINE_PUBLIC_RECORD_SCHEMA_VERSION,
    EXTENSION_PIPELINE_RECORD_SCHEMA_VERSION,
    EXTENSION_PIPELINE_STATE_SCHEMA_VERSION,
    EXTENSION_PIPELINE_STATUS_SCHEMA_VERSION,
    ExtensionPipelineAuthority,
    MAX_EXTENSION_PIPELINES,
    MAX_EXTENSION_PIPELINE_INPUT_BYTES,
    MAX_EXTENSION_PIPELINE_OPERATIONS,
)
from interface.extension_release import canonical_utc, parse_canonical_utc


PIPELINE_STATE_FILE = "phase6_extension_pipeline_state.json"

_PIPELINE_ID = re.compile(r"^extpipe_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_STAGES = (
    "CREATED",
    "GENERATION_PASSED",
    "SOURCE_CHECK_PASSED",
    "ISOLATED_RUNNER_PASSED",
    "DYNAMIC_VALIDATION_PASSED",
    "RELEASE_SIGNED",
    "DEPLOYMENT_PROPOSED",
    "SHADOW_ACTIVE",
    "SHADOW_PASSED",
    "READ_ONLY_CANARY_ACTIVE",
    "READ_ONLY_CANARY_PASSED",
    "AWAITING_SCOPED_CANARY_APPROVAL",
    "SCOPED_CANARY_ACTIVE",
    "SCOPED_CANARY_PASSED",
    "AWAITING_PROMOTION_APPROVAL",
    "PROMOTED",
)
_APPROVAL_BOUNDARIES = {
    "AWAITING_SCOPED_CANARY_APPROVAL": "scoped_canary",
    "AWAITING_PROMOTION_APPROVAL": "promoted",
}


class PipelineStateStore(Protocol):
    def read_json(self, name: str) -> dict[str, Any]: ...

    def mutate_json(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]: ...


class ExtensionPipelineError(RuntimeError):
    """Base governed-pipeline error."""


class ExtensionPipelineUnauthorizedError(ExtensionPipelineError):
    pass


class ExtensionPipelineConflictError(ExtensionPipelineError):
    pass


class ExtensionPipelineNotFoundError(ExtensionPipelineError):
    pass


class ExtensionPipelineStorageError(ExtensionPipelineError):
    pass


class ExtensionPipelineUnavailableError(ExtensionPipelineError):
    pass


class _StageBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExtensionPipelineCoordinator:
    """Crash-resumable composition of the existing Phase 6 gates.

    The coordinator has no generation, execution, signing, approval, or
    promotion authority of its own.  Every effect is delegated to an existing
    exact gate through deterministic child operation identities.  It stores
    only identifiers, digests, lifecycle state, and source-free receipts.

    A start command stops after the read-only canary and creates a pending
    scoped-canary review.  A later explicit advance may consume an already
    approved review, run the scoped canary, and create a separate promotion
    review.  Promotion likewise needs another approved review and explicit
    advance.  No method accepts an approver credential.
    """

    def __init__(
        self,
        *,
        state_store: PipelineStateStore,
        generation_gate: Any,
        artifact_quarantine: Any,
        source_check_gate: Any,
        isolated_runner_gate: Any,
        dynamic_validation_gate: Any,
        release_registry: Any,
        deployment_gate: Any,
        enabled: bool = False,
        control_token: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.generation_gate = generation_gate
        self.artifact_quarantine = artifact_quarantine
        self.source_check_gate = source_check_gate
        self.isolated_runner_gate = isolated_runner_gate
        self.dynamic_validation_gate = dynamic_validation_gate
        self.release_registry = release_registry
        self.deployment_gate = deployment_gate
        self.enabled = bool(enabled)
        self.control_token = str(control_token or "").strip()
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Pure coordinator-state projections.  These never open another gate.
    # ------------------------------------------------------------------
    def status(self, *, control_token: str) -> dict[str, Any]:
        self._authorize(control_token, require_enabled=False)
        state = self._read_state()
        counts = {stage: 0 for stage in _STAGES}
        blocked = 0
        for record in state["pipelines"].values():
            counts[record["stage"]] += 1
            blocked += int(record.get("last_issue_code") is not None)
        return {
            "schema_version": EXTENSION_PIPELINE_STATUS_SCHEMA_VERSION,
            "phase": "6.2i-governed-pipeline",
            "availability": (
                "configured"
                if self.enabled and self.control_token
                else "disabled_by_policy"
                if not self.enabled
                else "not_configured"
            ),
            "state_revision": state["revision"],
            "pipeline_count": len(state["pipelines"]),
            "operation_count": len(state["operations"]),
            "blocked_count": blocked,
            "counts": counts,
            "execution_mode": "explicit_private_control_plane_only",
            "automatic_approval": False,
            "automatic_promotion": False,
            "natural_language_trigger": False,
            "crash_recovery": "deterministic_child_operations_and_checkpoints",
            "persistence": {
                "source_in_coordinator_state": False,
                "test_or_canary_input_in_coordinator_state": False,
                "raw_invocation_output_in_coordinator_state": False,
                "identifiers_digests_and_receipts_only": True,
                "scope": "coordinator_state_only",
            },
            "reads_are_pure_coordinator_snapshots": True,
            "authority": self._authority(),
        }

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        records = [
            record
            for record in state["pipelines"].values()
            if self._owned(record, owner, principal, session)
        ]
        records.sort(
            key=lambda row: (row["updated_at"], row["pipeline_id"]),
            reverse=True,
        )
        return {
            "schema_version": EXTENSION_PIPELINE_LIST_SCHEMA_VERSION,
            "state_revision": state["revision"],
            "count": len(records[:limit]),
            "items": [self._public_record(row) for row in records[:limit]],
            "state_mutated": False,
            "authority": self._authority(),
        }

    def get(
        self,
        *,
        pipeline_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        record = self._record(state, self._pipeline_id(pipeline_id))
        self._require_owner(record, owner, principal, session)
        return self._public_record(record)

    # ------------------------------------------------------------------
    # Explicit lifecycle commands.
    # ------------------------------------------------------------------
    def start(
        self,
        *,
        operation_id: str,
        request_id: str,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_candidate_revision: int,
        expected_spec_digest: str,
        test_bundle: dict[str, Any],
        canary_input: dict[str, Any],
        release_expires_at: str,
        deployment_expires_at: str,
        shadow_max_invocations: int,
        read_only_canary_max_invocations: int,
        scoped_canary_max_invocations: int,
        promoted_max_invocations: int,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        operation = self._identifier(operation_id, "operation_id")
        request = self._identifier(request_id, "request_id")
        candidate = self._candidate_id(candidate_id)
        candidate_revision = self._revision(
            expected_candidate_revision, "expected_candidate_revision", minimum=1
        )
        spec_digest = self._digest_value(
            expected_spec_digest, "expected_spec_digest"
        )
        expected_state = self._revision(
            expected_state_revision, "expected_state_revision", minimum=0
        )
        tests, test_digest = self._test_bundle(test_bundle)
        selected_input, input_digest = self._canary_input(canary_input)
        release_expiry = self._canonical_time(release_expires_at)
        deployment_expiry = self._canonical_time(deployment_expires_at)
        budgets = self._budgets(
            shadow_max_invocations=shadow_max_invocations,
            read_only_canary_max_invocations=(
                read_only_canary_max_invocations
            ),
            scoped_canary_max_invocations=scoped_canary_max_invocations,
            promoted_max_invocations=promoted_max_invocations,
        )
        user = self._text(user_id, "user_id", 240)
        workspace = self._text(workspace_id, "workspace_id", 1024)
        session_text = self._text(session_id, "session_id", 240)
        owner, principal, session = self._scope(
            user, workspace, session_text, token
        )
        provenance = self._digest(
            {
                "schema_version": "veyra.phase6.extension_pipeline_request.v1",
                "request_id": request,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
            }
        )
        pipeline_id = "extpipe_" + self._digest(
            {
                "candidate_id": candidate,
                "candidate_revision": candidate_revision,
                "spec_digest": spec_digest,
                "test_bundle_digest": test_digest,
                "canary_input_digest": input_digest,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
            }
        )[:24]
        operation_key = self._operation_key(principal, session, operation)
        request_digest = self._digest(
            {
                "kind": "start",
                "operation_id": operation,
                "pipeline_id": pipeline_id,
                "expected_state_revision": expected_state,
                "request_provenance_digest": provenance,
                "release_expires_at": release_expiry,
                "deployment_expires_at": deployment_expiry,
                "budgets": budgets,
            }
        )
        replay = self._claim_start(
            pipeline_id=pipeline_id,
            operation_key=operation_key,
            operation_id=operation,
            request_digest=request_digest,
            expected_state_revision=expected_state,
            record_values={
                "user_id": user,
                "workspace_id": workspace,
                "session_id": session_text,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "request_provenance_digest": provenance,
                "candidate_id": candidate,
                "candidate_revision": candidate_revision,
                "spec_digest": spec_digest,
                "test_bundle_digest": test_digest,
                "canary_input_digest": input_digest,
                "release_expires_at": release_expiry,
                "deployment_expires_at": deployment_expiry,
                "budgets": budgets,
            },
        )
        if replay is not None:
            return {**replay, "operation_replayed": True}
        return self._run_operation(
            pipeline_id=pipeline_id,
            operation_key=operation_key,
            test_bundle=tests,
            canary_input=selected_input,
            review_id=None,
            control_token=token,
        )

    def advance(
        self,
        *,
        pipeline_id: str,
        operation_id: str,
        expected_pipeline_revision: int,
        user_id: str,
        workspace_id: str,
        session_id: str,
        test_bundle: dict[str, Any],
        canary_input: dict[str, Any],
        review_id: str | None,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        selected_pipeline = self._pipeline_id(pipeline_id)
        operation = self._identifier(operation_id, "operation_id")
        expected_revision = self._revision(
            expected_pipeline_revision,
            "expected_pipeline_revision",
            minimum=1,
        )
        tests, test_digest = self._test_bundle(test_bundle)
        selected_input, input_digest = self._canary_input(canary_input)
        selected_review = (
            self._text(review_id, "review_id", 240)
            if review_id is not None
            else None
        )
        user = self._text(user_id, "user_id", 240)
        workspace = self._text(workspace_id, "workspace_id", 1024)
        session_text = self._text(session_id, "session_id", 240)
        owner, principal, session = self._scope(
            user, workspace, session_text, token
        )
        operation_key = self._operation_key(principal, session, operation)
        request_digest = self._digest(
            {
                "kind": "advance",
                "operation_id": operation,
                "pipeline_id": selected_pipeline,
                "expected_pipeline_revision": expected_revision,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "test_bundle_digest": test_digest,
                "canary_input_digest": input_digest,
                "review_id": selected_review,
            }
        )
        replay = self._claim_advance(
            pipeline_id=selected_pipeline,
            operation_key=operation_key,
            operation_id=operation,
            request_digest=request_digest,
            expected_pipeline_revision=expected_revision,
            owner=owner,
            principal=principal,
            session=session,
            test_bundle_digest=test_digest,
            canary_input_digest=input_digest,
        )
        if replay is not None:
            return {**replay, "operation_replayed": True}
        return self._run_operation(
            pipeline_id=selected_pipeline,
            operation_key=operation_key,
            test_bundle=tests,
            canary_input=selected_input,
            review_id=selected_review,
            control_token=token,
        )

    # ------------------------------------------------------------------
    # Deterministic stage composition.
    # ------------------------------------------------------------------
    def _run_operation(
        self,
        *,
        pipeline_id: str,
        operation_key: str,
        test_bundle: dict[str, Any],
        canary_input: dict[str, Any],
        review_id: str | None,
        control_token: str,
    ) -> dict[str, Any]:
        attempted = "coordinator"
        try:
            while True:
                record = self._active_record(pipeline_id, operation_key)
                stage = record["stage"]
                if stage == "PROMOTED":
                    break
                consumes_review = stage in _APPROVAL_BOUNDARIES
                if stage in _APPROVAL_BOUNDARIES:
                    expected_review = record["receipts"].get(
                        "scoped_review"
                        if stage == "AWAITING_SCOPED_CANARY_APPROVAL"
                        else "promotion_review"
                    )
                    expected_id = (
                        expected_review.get("review_id")
                        if isinstance(expected_review, dict)
                        else None
                    )
                    if review_id is None:
                        break
                    if review_id != expected_id:
                        raise _StageBlocked(
                            "pending_review_identity_mismatch"
                        )

                attempted = self._next_action(stage)
                self._execute_stage(
                    record=record,
                    operation_key=operation_key,
                    test_bundle=test_bundle,
                    canary_input=canary_input,
                    review_id=review_id,
                    control_token=control_token,
                )
                if consumes_review:
                    # One explicit advance may consume only the review that
                    # authorized its starting approval boundary.  The same
                    # identity must never flow through the scoped canary and
                    # get compared with (or authorize) the newly-created,
                    # independent promotion review later in this loop.
                    review_id = None
        except (
            ExtensionPipelineConflictError,
            ExtensionPipelineStorageError,
        ):
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, _StageBlocked) else type(exc).__name__
            self._mark_issue(
                pipeline_id=pipeline_id,
                operation_key=operation_key,
                attempted_stage=attempted,
                issue_code=self._safe_issue(code),
            )
        return self._finish_operation(pipeline_id, operation_key)

    def _execute_stage(
        self,
        *,
        record: dict[str, Any],
        operation_key: str,
        test_bundle: dict[str, Any],
        canary_input: dict[str, Any],
        review_id: str | None,
        control_token: str,
    ) -> None:
        stage = record["stage"]
        common = {
            "user_id": record["user_id"],
            "workspace_id": record["workspace_id"],
        }
        scoped = {
            **common,
            "session_id": record["session_id"],
        }
        child = lambda name: self._child_operation(record["pipeline_id"], name)
        receipts = record["receipts"]

        if stage == "CREATED":
            result = self.generation_gate.generate(
                candidate_id=record["candidate_id"],
                **scoped,
                request_id=child("generation-request"),
                operation_id=child("generation"),
                expected_candidate_revision=record["candidate_revision"],
                expected_spec_digest=record["spec_digest"],
                control_token=control_token,
            )
            self._require_values(
                result,
                stored_stage="GENERATION_QUARANTINED",
                effective_status="GENERATION_QUARANTINED",
                generation_status="quarantined",
            )
            artifact_id = self._required_result_id(
                result, "artifact_id", "extart_"
            )
            artifact_sha = self._required_result_digest(
                result, "artifact_sha256"
            )
            artifact = self.artifact_quarantine.get(
                artifact_id=artifact_id, **common
            )
            self._require_values(
                artifact,
                artifact_status="quarantined",
                effective_status="ARTIFACT_QUARANTINED",
                artifact_sha256=artifact_sha,
                candidate_id=record["candidate_id"],
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="GENERATION_PASSED",
                receipt_name="generation",
                receipt={
                    "generation_id": self._required_result_id(
                        result, "generation_id", "extgen_"
                    ),
                    "generation_report_digest": self._required_result_digest(
                        result, "report_digest"
                    ),
                    "artifact_id": artifact_id,
                    "artifact_revision": self._result_revision(
                        artifact, "artifact_revision", minimum=1
                    ),
                    "artifact_sha256": artifact_sha,
                    "provider_id": self._safe_text(result.get("provider_id")),
                    "model_id": self._safe_text(result.get("model_id")),
                    "model_config_digest": self._required_result_digest(
                        result, "model_config_digest"
                    ),
                    "generator_revision": self._safe_text(
                        result.get("generator_revision")
                    ),
                    "prompt_digest": self._required_result_digest(
                        result, "prompt_digest"
                    ),
                    "generation_policy_digest": self._required_result_digest(
                        result, "generation_policy_digest"
                    ),
                },
            )
            return

        generation = self._receipt(receipts, "generation")
        if stage == "GENERATION_PASSED":
            result = self.source_check_gate.start(
                artifact_id=generation["artifact_id"],
                **common,
                expected_artifact_revision=generation["artifact_revision"],
                expected_artifact_sha256=generation["artifact_sha256"],
                operation_id=child("source-check"),
            )
            self._require_values(
                result,
                effective_status="SOURCE_CHECK_PASSED",
                source_check_status="passed",
                source_syntax_status="passed",
                static_checks_status="passed",
                static_security_policy_status="passed",
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="SOURCE_CHECK_PASSED",
                receipt_name="source_check",
                receipt={
                    "check_id": self._required_result_id(
                        result, "check_id", "extcheck_"
                    ),
                    "source_check_report_digest": (
                        self._required_result_digest(
                            result, "source_check_report_digest"
                        )
                    ),
                    "parser_identity": self._safe_text(
                        result.get("parser_identity")
                    ),
                    "ruleset_digest": self._required_result_digest(
                        result, "ruleset_digest"
                    ),
                },
            )
            return

        source = self._receipt(receipts, "source_check")
        if stage == "SOURCE_CHECK_PASSED":
            result = self.isolated_runner_gate.start(
                check_id=source["check_id"],
                **common,
                expected_artifact_revision=generation["artifact_revision"],
                expected_artifact_sha256=generation["artifact_sha256"],
                expected_source_check_report_digest=(
                    source["source_check_report_digest"]
                ),
                operation_id=child("isolated-runner"),
                control_token=control_token,
            )
            self._require_values(
                result,
                effective_status="RUNNER_JOB_PASSED",
                probe_status="passed",
                trusted_isolated_runner_status="passed",
            )
            run_id = self._required_result_id(result, "run_id", "extrun_")
            runner_digest = self._durable_report_digest(
                gate=self.isolated_runner_gate,
                state_file="phase6_extension_isolated_runner_state.json",
                collection="runs",
                record_id=run_id,
                expected_stage="RUNNER_JOB_PASSED",
                integrity_kwargs={"run_id": run_id, **common},
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="ISOLATED_RUNNER_PASSED",
                receipt_name="isolated_runner",
                receipt={
                    "run_id": run_id,
                    "isolated_runner_report_digest": runner_digest,
                    "engine_identity_digest": self._required_result_digest(
                        result, "engine_identity_digest"
                    ),
                    "image_id": self._safe_text(result.get("image_id")),
                    "runner_policy_digest": self._required_result_digest(
                        result, "runner_policy_digest"
                    ),
                },
            )
            return

        runner = self._receipt(receipts, "isolated_runner")
        if stage == "ISOLATED_RUNNER_PASSED":
            result = self.dynamic_validation_gate.start(
                isolated_run_id=runner["run_id"],
                **scoped,
                request_id=child("dynamic-request"),
                operation_id=child("dynamic-validation"),
                expected_artifact_revision=generation["artifact_revision"],
                expected_artifact_sha256=generation["artifact_sha256"],
                expected_source_check_report_digest=(
                    source["source_check_report_digest"]
                ),
                expected_isolated_runner_report_digest=(
                    runner["isolated_runner_report_digest"]
                ),
                test_bundle=test_bundle,
                control_token=control_token,
            )
            self._require_values(
                result,
                effective_status="DYNAMIC_VALIDATION_PASSED",
                candidate_execution_status="passed",
                unit_checks_status="passed",
                contract_checks_status="passed",
                security_runtime_checks_status="passed",
                fuzz_checks_status="passed",
                behavior_verification_status="passed",
                test_bundle_digest=record["test_bundle_digest"],
            )
            validation_id = self._required_result_id(
                result, "validation_id", "extval_"
            )
            validation_digest = self._durable_report_digest(
                gate=self.dynamic_validation_gate,
                state_file="phase6_extension_dynamic_validation_state.json",
                collection="validations",
                record_id=validation_id,
                expected_stage="DYNAMIC_VALIDATION_PASSED",
                integrity_kwargs={
                    "validation_id": validation_id,
                    **scoped,
                    "control_token": control_token,
                },
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="DYNAMIC_VALIDATION_PASSED",
                receipt_name="dynamic_validation",
                receipt={
                    "validation_id": validation_id,
                    "validation_report_digest": validation_digest,
                    "build_identity_digest": self._required_result_digest(
                        result, "build_identity_digest"
                    ),
                    "engine_identity_digest": self._required_result_digest(
                        result, "engine_identity_digest"
                    ),
                    "image_id": self._safe_text(result.get("image_id")),
                    "isolation_conformance_digest": (
                        self._required_result_digest(
                            result, "isolation_conformance_digest"
                        )
                    ),
                    "validation_conformance_digest": (
                        self._required_result_digest(
                            result, "validation_conformance_digest"
                        )
                    ),
                    "validation_policy_digest": self._required_result_digest(
                        result, "validation_policy_digest"
                    ),
                    "validation_harness_revision": self._safe_text(
                        result.get("validation_harness_revision")
                    ),
                    "validation_harness_digest": self._required_result_digest(
                        result, "validation_harness_digest"
                    ),
                },
            )
            return

        validation = self._receipt(receipts, "dynamic_validation")
        if stage == "DYNAMIC_VALIDATION_PASSED":
            release_status = self.release_registry.status(
                control_token=control_token
            )
            registry_revision = self._result_revision(
                self._result_mapping(release_status, "storage"),
                "registry_revision",
                minimum=0,
            )
            signer = self._result_mapping(release_status, "signer")
            signer_identity = self._required_result_digest(
                signer, "signing_service_identity_digest"
            )
            generator_identity = self._generator_identity_digest(generation)
            verifier_identity = self._verifier_identity_digest(
                source, validation
            )
            result = self.release_registry.create(
                generation_id=generation["generation_id"],
                validation_id=validation["validation_id"],
                **scoped,
                operation_id=child("signed-release"),
                expected_registry_revision=registry_revision,
                expected_generation_report_digest=(
                    generation["generation_report_digest"]
                ),
                expected_validation_report_digest=(
                    validation["validation_report_digest"]
                ),
                expected_generator_identity_digest=generator_identity,
                expected_verifier_identity_digest=verifier_identity,
                expected_signing_identity_digest=signer_identity,
                expires_at=record["release_expires_at"],
                control_token=control_token,
            )
            self._require_values(
                result,
                effective_status="RELEASE_SIGNED",
                fresh=True,
                revoked=False,
                signature_algorithm="ed25519",
                signature_verification_status="verified",
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="RELEASE_SIGNED",
                receipt_name="release",
                receipt={
                    "release_id": self._required_result_id(
                        result, "release_id", "extrel_"
                    ),
                    "release_revision": self._result_revision(
                        result, "release_revision", minimum=1
                    ),
                    "attestation_digest": self._required_result_digest(
                        result, "attestation_digest"
                    ),
                    "manifest_digest": self._required_result_digest(
                        result, "manifest_digest"
                    ),
                    "signing_service_identity_digest": signer_identity,
                    "generator_identity_digest": generator_identity,
                    "verifier_identity_digest": verifier_identity,
                },
            )
            return

        release = self._receipt(receipts, "release")
        if stage == "RELEASE_SIGNED":
            state_revision = self._deployment_state_revision(control_token)
            result = self.deployment_gate.propose(
                operation_id=child("deployment-proposal"),
                request_id=child("deployment-proposal-request"),
                release_id=release["release_id"],
                **scoped,
                expected_state_revision=state_revision,
                expected_release_revision=release["release_revision"],
                expected_attestation_digest=release["attestation_digest"],
                expires_at=record["deployment_expires_at"],
                control_token=control_token,
            )
            self._require_values(result, mode="record_only", stage="PROPOSED")
            self._checkpoint(
                record,
                operation_key,
                next_stage="DEPLOYMENT_PROPOSED",
                receipt_name="deployment",
                receipt=self._deployment_receipt(result),
            )
            return

        deployment = self._receipt(receipts, "deployment")
        if stage == "DEPLOYMENT_PROPOSED":
            result = self._transition(
                record,
                deployment,
                target_mode="shadow",
                max_invocations=record["budgets"]["shadow"],
                review_id=None,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="SHADOW_ACTIVE",
                receipt_name="deployment",
                receipt=self._deployment_receipt(result),
            )
            return

        if stage == "SHADOW_ACTIVE":
            response = self._invoke(
                record,
                deployment,
                mode="shadow",
                canary_input=canary_input,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="SHADOW_PASSED",
                receipt_name="shadow_invocation",
                receipt=self._invocation_receipt(
                    response, expected_status="discarded", expected_discarded=True
                ),
                deployment_receipt=self._deployment_receipt(
                    self._result_mapping(response, "deployment")
                ),
            )
            return

        if stage == "SHADOW_PASSED":
            result = self._transition(
                record,
                deployment,
                target_mode="read_only_canary",
                max_invocations=record["budgets"]["read_only_canary"],
                review_id=None,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="READ_ONLY_CANARY_ACTIVE",
                receipt_name="deployment",
                receipt=self._deployment_receipt(result),
            )
            return

        if stage == "READ_ONLY_CANARY_ACTIVE":
            response = self._invoke(
                record,
                deployment,
                mode="read_only_canary",
                canary_input=canary_input,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="READ_ONLY_CANARY_PASSED",
                receipt_name="read_only_canary_invocation",
                receipt=self._invocation_receipt(
                    response, expected_status="passed", expected_discarded=False
                ),
                deployment_receipt=self._deployment_receipt(
                    self._result_mapping(response, "deployment")
                ),
            )
            return

        if stage == "READ_ONLY_CANARY_PASSED":
            review = self._request_review(
                record,
                deployment,
                target_mode="scoped_canary",
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="AWAITING_SCOPED_CANARY_APPROVAL",
                receipt_name="scoped_review",
                receipt=self._review_receipt(review, "scoped_canary"),
            )
            return

        if stage == "AWAITING_SCOPED_CANARY_APPROVAL":
            result = self._transition(
                record,
                deployment,
                target_mode="scoped_canary",
                max_invocations=record["budgets"]["scoped_canary"],
                review_id=review_id,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="SCOPED_CANARY_ACTIVE",
                receipt_name="deployment",
                receipt=self._deployment_receipt(result),
            )
            return

        if stage == "SCOPED_CANARY_ACTIVE":
            response = self._invoke(
                record,
                deployment,
                mode="scoped_canary",
                canary_input=canary_input,
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="SCOPED_CANARY_PASSED",
                receipt_name="scoped_canary_invocation",
                receipt=self._invocation_receipt(
                    response, expected_status="passed", expected_discarded=False
                ),
                deployment_receipt=self._deployment_receipt(
                    self._result_mapping(response, "deployment")
                ),
            )
            return

        if stage == "SCOPED_CANARY_PASSED":
            review = self._request_review(
                record,
                deployment,
                target_mode="promoted",
                control_token=control_token,
            )
            self._checkpoint(
                record,
                operation_key,
                next_stage="AWAITING_PROMOTION_APPROVAL",
                receipt_name="promotion_review",
                receipt=self._review_receipt(review, "promoted"),
            )
            return

        if stage == "AWAITING_PROMOTION_APPROVAL":
            result = self._transition(
                record,
                deployment,
                target_mode="promoted",
                max_invocations=record["budgets"]["promoted"],
                review_id=review_id,
                control_token=control_token,
            )
            registry = self.deployment_gate.public_registry(
                **scoped, control_token=control_token
            )
            rows = registry.get("items")
            capability = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and row.get("deployment_id") == result.get("deployment_id")
                ),
                None,
            ) if isinstance(rows, list) else None
            if not isinstance(capability, dict):
                raise _StageBlocked("promoted_capability_not_public")
            self._checkpoint(
                record,
                operation_key,
                next_stage="PROMOTED",
                receipt_name="deployment",
                receipt=self._deployment_receipt(result),
                capability_receipt={
                    "capability_id": self._safe_text(
                        capability.get("capability_id")
                    ),
                    "deployment_id": self._required_result_id(
                        capability, "deployment_id", "extdep_"
                    ),
                    "release_id": self._required_result_id(
                        capability, "release_id", "extrel_"
                    ),
                    "attestation_digest": self._required_result_digest(
                        capability, "attestation_digest"
                    ),
                    "input_schema_digest": self._required_result_digest(
                        capability, "input_schema_digest"
                    ),
                    "output_schema_digest": self._required_result_digest(
                        capability, "output_schema_digest"
                    ),
                    "execution_boundary": self._safe_text(
                        capability.get("execution_boundary")
                    ),
                },
            )
            return

        raise ExtensionPipelineStorageError("pipeline stage is invalid")

    # ------------------------------------------------------------------
    # Child gate helpers.
    # ------------------------------------------------------------------
    def _transition(
        self,
        record: dict[str, Any],
        deployment: dict[str, Any],
        *,
        target_mode: str,
        max_invocations: int,
        review_id: str | None,
        control_token: str,
    ) -> dict[str, Any]:
        result = self.deployment_gate.transition(
            operation_id=self._child_operation(
                record["pipeline_id"], f"transition-{target_mode}"
            ),
            request_id=self._child_operation(
                record["pipeline_id"], f"transition-{target_mode}-request"
            ),
            deployment_id=deployment["deployment_id"],
            target_mode=target_mode,
            user_id=record["user_id"],
            workspace_id=record["workspace_id"],
            session_id=record["session_id"],
            expected_state_revision=self._deployment_state_revision(
                control_token
            ),
            expected_deployment_revision=deployment["revision"],
            expected_mode_epoch=deployment["mode_epoch"],
            max_invocations=max_invocations,
            expires_at=record["deployment_expires_at"],
            review_id=review_id,
            control_token=control_token,
        )
        self._require_values(result, mode=target_mode)
        return result

    def _invoke(
        self,
        record: dict[str, Any],
        deployment: dict[str, Any],
        *,
        mode: str,
        canary_input: dict[str, Any],
        control_token: str,
    ) -> dict[str, Any]:
        if deployment.get("mode") != mode:
            raise ExtensionPipelineStorageError(
                "pipeline deployment checkpoint has another mode"
            )
        return self.deployment_gate.invoke(
            operation_id=self._child_operation(
                record["pipeline_id"], f"invoke-{mode}"
            ),
            request_id=self._child_operation(
                record["pipeline_id"], f"invoke-{mode}-request"
            ),
            deployment_id=deployment["deployment_id"],
            user_id=record["user_id"],
            workspace_id=record["workspace_id"],
            session_id=record["session_id"],
            expected_state_revision=self._deployment_state_revision(
                control_token
            ),
            expected_deployment_revision=deployment["revision"],
            expected_mode_epoch=deployment["mode_epoch"],
            input_payload=canary_input,
            control_token=control_token,
        )

    def _request_review(
        self,
        record: dict[str, Any],
        deployment: dict[str, Any],
        *,
        target_mode: str,
        control_token: str,
    ) -> dict[str, Any]:
        return self.deployment_gate.request_transition_review(
            operation_id=self._child_operation(
                record["pipeline_id"], f"review-{target_mode}"
            ),
            request_id=self._child_operation(
                record["pipeline_id"], f"review-{target_mode}-request"
            ),
            deployment_id=deployment["deployment_id"],
            target_mode=target_mode,
            user_id=record["user_id"],
            workspace_id=record["workspace_id"],
            session_id=record["session_id"],
            expected_state_revision=self._deployment_state_revision(
                control_token
            ),
            expected_deployment_revision=deployment["revision"],
            expected_mode_epoch=deployment["mode_epoch"],
            control_token=control_token,
        )

    def _deployment_state_revision(self, control_token: str) -> int:
        status = self.deployment_gate.status(control_token=control_token)
        return self._result_revision(status, "state_revision", minimum=0)

    def _durable_report_digest(
        self,
        *,
        gate: Any,
        state_file: str,
        collection: str,
        record_id: str,
        expected_stage: str,
        integrity_kwargs: dict[str, Any],
    ) -> str:
        integrity = gate.integrity(**integrity_kwargs)
        if integrity.get("report_integrity_status") != "validated":
            raise _StageBlocked("durable_report_integrity_unavailable")
        store = getattr(gate, "state_store", None)
        if store is None or not callable(getattr(store, "read_json", None)):
            raise ExtensionPipelineUnavailableError(
                "gate durable report store is unavailable"
            )
        raw = store.read_json(state_file)
        records = raw.get(collection) if isinstance(raw, dict) else None
        selected = records.get(record_id) if isinstance(records, dict) else None
        if (
            not isinstance(selected, dict)
            or selected.get("stage") != expected_stage
        ):
            raise _StageBlocked("durable_report_stage_changed")
        return self._required_result_digest(selected, "report_digest")

    # ------------------------------------------------------------------
    # Coordinator durability and exact replay.
    # ------------------------------------------------------------------
    def _claim_start(
        self,
        *,
        pipeline_id: str,
        operation_key: str,
        operation_id: str,
        request_digest: str,
        expected_state_revision: int,
        record_values: dict[str, Any],
    ) -> dict[str, Any] | None:
        replay: dict[str, Any] | None = None

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            nonlocal replay
            state = self._normalize_state(raw)
            existing_operation = state["operations"].get(operation_key)
            if existing_operation is not None:
                self._validate_operation(
                    existing_operation,
                    operation_id=operation_id,
                    request_digest=request_digest,
                    pipeline_id=pipeline_id,
                )
                if existing_operation["status"] == "complete":
                    replay = copy.deepcopy(existing_operation["result"])
                    return state
                record = self._record(state, pipeline_id)
                if record["active_operation"] != operation_key:
                    raise ExtensionPipelineStorageError(
                        "in-progress operation lost its pipeline claim"
                    )
                return state
            if state["revision"] != expected_state_revision:
                raise ExtensionPipelineConflictError(
                    "pipeline state revision CAS failed"
                )
            if pipeline_id in state["pipelines"]:
                raise ExtensionPipelineConflictError(
                    "an exact pipeline already exists; use explicit advance"
                )
            if (
                len(state["pipelines"]) >= MAX_EXTENSION_PIPELINES
                or len(state["operations"])
                >= MAX_EXTENSION_PIPELINE_OPERATIONS
            ):
                raise ExtensionPipelineConflictError(
                    "pipeline durable capacity is exhausted"
                )
            now = self._now_iso()
            record = {
                "schema_version": EXTENSION_PIPELINE_RECORD_SCHEMA_VERSION,
                "pipeline_id": pipeline_id,
                **copy.deepcopy(record_values),
                "revision": 1,
                "stage": "CREATED",
                "receipts": {},
                "last_issue_code": None,
                "last_attempted_stage": None,
                "active_operation": operation_key,
                "created_at": now,
                "updated_at": now,
            }
            self._validate_record(record)
            state["pipelines"][pipeline_id] = record
            state["operations"][operation_key] = {
                "operation_id_digest": self._operation_id_digest(
                    operation_id
                ),
                "pipeline_id": pipeline_id,
                "request_digest": request_digest,
                "status": "in_progress",
                "result": None,
            }
            state["revision"] += 1
            return state

        self._mutate_state(mutate)
        return replay

    def _claim_advance(
        self,
        *,
        pipeline_id: str,
        operation_key: str,
        operation_id: str,
        request_digest: str,
        expected_pipeline_revision: int,
        owner: str,
        principal: str,
        session: str,
        test_bundle_digest: str,
        canary_input_digest: str,
    ) -> dict[str, Any] | None:
        replay: dict[str, Any] | None = None

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            nonlocal replay
            state = self._normalize_state(raw)
            existing_operation = state["operations"].get(operation_key)
            if existing_operation is not None:
                self._validate_operation(
                    existing_operation,
                    operation_id=operation_id,
                    request_digest=request_digest,
                    pipeline_id=pipeline_id,
                )
                if existing_operation["status"] == "complete":
                    replay = copy.deepcopy(existing_operation["result"])
                    return state
                record = self._record(state, pipeline_id)
                self._require_owner(record, owner, principal, session)
                if record["active_operation"] != operation_key:
                    raise ExtensionPipelineStorageError(
                        "in-progress operation lost its pipeline claim"
                    )
                return state
            record = self._record(state, pipeline_id)
            self._require_owner(record, owner, principal, session)
            if record["revision"] != expected_pipeline_revision:
                raise ExtensionPipelineConflictError(
                    "pipeline revision CAS failed"
                )
            if record["active_operation"] is not None:
                raise ExtensionPipelineConflictError(
                    "another pipeline operation is in progress"
                )
            if (
                record["test_bundle_digest"] != test_bundle_digest
                or record["canary_input_digest"] != canary_input_digest
            ):
                raise ExtensionPipelineConflictError(
                    "pipeline frozen test or canary input digest changed"
                )
            if len(state["operations"]) >= MAX_EXTENSION_PIPELINE_OPERATIONS:
                raise ExtensionPipelineConflictError(
                    "pipeline operation capacity is exhausted"
                )
            record["active_operation"] = operation_key
            record["last_issue_code"] = None
            record["last_attempted_stage"] = None
            record["updated_at"] = self._now_iso()
            record["revision"] += 1
            state["operations"][operation_key] = {
                "operation_id_digest": self._operation_id_digest(
                    operation_id
                ),
                "pipeline_id": pipeline_id,
                "request_digest": request_digest,
                "status": "in_progress",
                "result": None,
            }
            state["revision"] += 1
            return state

        self._mutate_state(mutate)
        return replay

    def _checkpoint(
        self,
        record: dict[str, Any],
        operation_key: str,
        *,
        next_stage: str,
        receipt_name: str,
        receipt: dict[str, Any],
        deployment_receipt: dict[str, Any] | None = None,
        capability_receipt: dict[str, Any] | None = None,
    ) -> None:
        if next_stage not in _STAGES:
            raise ExtensionPipelineStorageError("checkpoint stage is invalid")

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            current = self._record(state, record["pipeline_id"])
            if (
                current["active_operation"] != operation_key
                or current["revision"] != record["revision"]
                or current["stage"] != record["stage"]
            ):
                raise ExtensionPipelineConflictError(
                    "pipeline checkpoint CAS failed"
                )
            current["stage"] = next_stage
            current["receipts"][receipt_name] = copy.deepcopy(receipt)
            if deployment_receipt is not None:
                current["receipts"]["deployment"] = copy.deepcopy(
                    deployment_receipt
                )
            if capability_receipt is not None:
                current["receipts"]["capability"] = copy.deepcopy(
                    capability_receipt
                )
            current["last_issue_code"] = None
            current["last_attempted_stage"] = None
            current["updated_at"] = self._now_iso()
            current["revision"] += 1
            self._validate_record(current)
            state["revision"] += 1
            return state

        self._mutate_state(mutate)

    def _mark_issue(
        self,
        *,
        pipeline_id: str,
        operation_key: str,
        attempted_stage: str,
        issue_code: str,
    ) -> None:
        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            record = self._record(state, pipeline_id)
            if record["active_operation"] != operation_key:
                raise ExtensionPipelineConflictError(
                    "pipeline issue checkpoint lost its operation claim"
                )
            record["last_issue_code"] = issue_code
            record["last_attempted_stage"] = self._safe_text(
                attempted_stage, maximum=120
            )
            record["updated_at"] = self._now_iso()
            record["revision"] += 1
            state["revision"] += 1
            return state

        self._mutate_state(mutate)

    def _finish_operation(
        self, pipeline_id: str, operation_key: str
    ) -> dict[str, Any]:
        result: dict[str, Any] | None = None

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            state = self._normalize_state(raw)
            record = self._record(state, pipeline_id)
            operation = state["operations"].get(operation_key)
            if not isinstance(operation, dict):
                raise ExtensionPipelineStorageError(
                    "pipeline operation record is unavailable"
                )
            if operation["status"] == "complete":
                result = copy.deepcopy(operation["result"])
                return state
            if record["active_operation"] != operation_key:
                raise ExtensionPipelineConflictError(
                    "pipeline finish lost its operation claim"
                )
            record["active_operation"] = None
            record["updated_at"] = self._now_iso()
            record["revision"] += 1
            self._validate_record(record)
            state["revision"] += 1
            result = self._public_record(record)
            operation["status"] = "complete"
            operation["result"] = copy.deepcopy(result)
            return state

        self._mutate_state(mutate)
        if result is None:
            raise ExtensionPipelineStorageError(
                "pipeline operation produced no durable result"
            )
        return {**result, "operation_replayed": False}

    def _active_record(
        self, pipeline_id: str, operation_key: str
    ) -> dict[str, Any]:
        state = self._read_state()
        record = self._record(state, pipeline_id)
        if record["active_operation"] != operation_key:
            raise ExtensionPipelineConflictError(
                "pipeline operation no longer owns the exact claim"
            )
        return copy.deepcopy(record)

    def _mutate_state(
        self,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        try:
            return self.state_store.mutate_json(
                PIPELINE_STATE_FILE, mutator
            )
        except ExtensionPipelineError:
            raise
        except Exception as exc:
            raise ExtensionPipelineStorageError(
                "pipeline durable mutation failed"
            ) from exc

    # ------------------------------------------------------------------
    # Validation and projections.
    # ------------------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        try:
            return self._normalize_state(
                self.state_store.read_json(PIPELINE_STATE_FILE)
            )
        except ExtensionPipelineError:
            raise
        except Exception as exc:
            raise ExtensionPipelineStorageError(
                "pipeline state is unavailable"
            ) from exc

    def _normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not raw:
            return {
                "schema_version": EXTENSION_PIPELINE_STATE_SCHEMA_VERSION,
                "revision": 0,
                "pipelines": {},
                "operations": {},
            }
        if raw.get("_state_corrupt"):
            raise ExtensionPipelineStorageError("pipeline state is corrupt")
        state = {
            "schema_version": raw.get("schema_version"),
            "revision": raw.get("revision"),
            "pipelines": raw.get("pipelines"),
            "operations": raw.get("operations"),
        }
        if (
            state["schema_version"] != EXTENSION_PIPELINE_STATE_SCHEMA_VERSION
            or type(state["revision"]) is not int
            or state["revision"] < 0
            or not isinstance(state["pipelines"], dict)
            or not isinstance(state["operations"], dict)
            or len(state["pipelines"]) > MAX_EXTENSION_PIPELINES
            or len(state["operations"]) > MAX_EXTENSION_PIPELINE_OPERATIONS
        ):
            raise ExtensionPipelineStorageError("pipeline state is invalid")
        for pipeline_id, record in state["pipelines"].items():
            if pipeline_id != record.get("pipeline_id"):
                raise ExtensionPipelineStorageError(
                    "pipeline state index is invalid"
                )
            self._validate_record(record)
        for operation in state["operations"].values():
            if (
                not isinstance(operation, dict)
                or operation.get("status") not in {"in_progress", "complete"}
                or not isinstance(operation.get("operation_id_digest"), str)
                or not _DIGEST.fullmatch(operation["operation_id_digest"])
                or not isinstance(operation.get("request_digest"), str)
                or not _DIGEST.fullmatch(operation["request_digest"])
                or not isinstance(operation.get("pipeline_id"), str)
                or operation["pipeline_id"] not in state["pipelines"]
                or (
                    operation["status"] == "complete"
                    and not isinstance(operation.get("result"), dict)
                )
                or (
                    operation["status"] == "in_progress"
                    and operation.get("result") is not None
                )
            ):
                raise ExtensionPipelineStorageError(
                    "pipeline operation index is invalid"
                )
        return state

    def _validate_record(self, record: dict[str, Any]) -> None:
        required = {
            "schema_version",
            "pipeline_id",
            "user_id",
            "workspace_id",
            "session_id",
            "owner_scope_digest",
            "principal_digest",
            "session_digest",
            "request_provenance_digest",
            "candidate_id",
            "candidate_revision",
            "spec_digest",
            "test_bundle_digest",
            "canary_input_digest",
            "release_expires_at",
            "deployment_expires_at",
            "budgets",
            "revision",
            "stage",
            "receipts",
            "last_issue_code",
            "last_attempted_stage",
            "active_operation",
            "created_at",
            "updated_at",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record["schema_version"]
            != EXTENSION_PIPELINE_RECORD_SCHEMA_VERSION
            or not _PIPELINE_ID.fullmatch(str(record["pipeline_id"]))
            or not _CANDIDATE_ID.fullmatch(str(record["candidate_id"]))
            or record["stage"] not in _STAGES
            or type(record["candidate_revision"]) is not int
            or record["candidate_revision"] < 1
            or type(record["revision"]) is not int
            or record["revision"] < 1
            or not isinstance(record["receipts"], dict)
            or not isinstance(record["budgets"], dict)
            or set(record["budgets"])
            != {"shadow", "read_only_canary", "scoped_canary", "promoted"}
            or any(
                type(value) is not int or not 1 <= value <= 100
                for value in record["budgets"].values()
            )
            or any(
                not _DIGEST.fullmatch(str(record[field]))
                for field in (
                    "owner_scope_digest",
                    "principal_digest",
                    "session_digest",
                    "request_provenance_digest",
                    "spec_digest",
                    "test_bundle_digest",
                    "canary_input_digest",
                )
            )
            or (
                record["active_operation"] is not None
                and not isinstance(record["active_operation"], str)
            )
        ):
            raise ExtensionPipelineStorageError(
                "pipeline durable record is invalid"
            )
        if self._contains_private_payload(record):
            raise ExtensionPipelineStorageError(
                "pipeline record contains a forbidden private payload field"
            )

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        self._validate_record(record)
        return {
            "schema_version": EXTENSION_PIPELINE_PUBLIC_RECORD_SCHEMA_VERSION,
            "pipeline_id": record["pipeline_id"],
            "pipeline_revision": record["revision"],
            "candidate_id": record["candidate_id"],
            "candidate_revision": record["candidate_revision"],
            "spec_digest": record["spec_digest"],
            "stage": record["stage"],
            "status": (
                "blocked"
                if record["last_issue_code"] is not None
                else "promoted"
                if record["stage"] == "PROMOTED"
                else "awaiting_independent_approval"
                if record["stage"] in _APPROVAL_BOUNDARIES
                else "in_progress"
            ),
            "next_action": self._next_action(record["stage"]),
            "pending_review": self._pending_review(record),
            "receipts": copy.deepcopy(record["receipts"]),
            "last_issue_code": record["last_issue_code"],
            "last_attempted_stage": record["last_attempted_stage"],
            "test_bundle_digest": record["test_bundle_digest"],
            "canary_input_digest": record["canary_input_digest"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "active_operation": record["active_operation"] is not None,
            "source_persisted_in_pipeline_state": False,
            "raw_input_persisted_in_pipeline_state": False,
            "raw_output_persisted_in_pipeline_state": False,
            "automatic_approval": False,
            "automatic_promotion": False,
            "authority": self._authority(),
        }

    @staticmethod
    def _pending_review(record: dict[str, Any]) -> dict[str, Any] | None:
        name = {
            "AWAITING_SCOPED_CANARY_APPROVAL": "scoped_review",
            "AWAITING_PROMOTION_APPROVAL": "promotion_review",
        }.get(record["stage"])
        if name is None:
            return None
        receipt = record["receipts"].get(name)
        return copy.deepcopy(receipt) if isinstance(receipt, dict) else None

    @staticmethod
    def _next_action(stage: str) -> str:
        if stage == "AWAITING_SCOPED_CANARY_APPROVAL":
            return "independent_approval_then_explicit_resume_scoped_canary"
        if stage == "AWAITING_PROMOTION_APPROVAL":
            return "independent_approval_then_explicit_resume_promotion"
        if stage == "PROMOTED":
            return "complete"
        return {
            "CREATED": "generation",
            "GENERATION_PASSED": "static_source_check",
            "SOURCE_CHECK_PASSED": "trusted_isolated_runner_probe",
            "ISOLATED_RUNNER_PASSED": "dynamic_validation",
            "DYNAMIC_VALIDATION_PASSED": "ed25519_signed_release",
            "RELEASE_SIGNED": "deployment_proposal",
            "DEPLOYMENT_PROPOSED": "shadow_transition",
            "SHADOW_ACTIVE": "shadow_invocation",
            "SHADOW_PASSED": "read_only_canary_transition",
            "READ_ONLY_CANARY_ACTIVE": "read_only_canary_invocation",
            "READ_ONLY_CANARY_PASSED": "scoped_canary_review_request",
            "SCOPED_CANARY_ACTIVE": "scoped_canary_invocation",
            "SCOPED_CANARY_PASSED": "promotion_review_request",
        }[stage]

    @staticmethod
    def _contains_private_payload(value: Any) -> bool:
        forbidden = {
            "source",
            "source_bytes",
            "content_b64url",
            "test_bundle",
            "canary_input",
            "input_payload",
            "output_payload",
            "raw_output",
            "private_key",
            "approver_token",
            "control_token",
        }
        if isinstance(value, dict):
            return any(
                str(key).lower() in forbidden
                or ExtensionPipelineCoordinator._contains_private_payload(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(
                ExtensionPipelineCoordinator._contains_private_payload(item)
                for item in value
            )
        return False

    # ------------------------------------------------------------------
    # Small canonical helpers.
    # ------------------------------------------------------------------
    def _authorize(self, token: str, *, require_enabled: bool) -> str:
        selected = str(token or "").strip()
        if not self.control_token or not hmac.compare_digest(
            selected, self.control_token
        ):
            raise ExtensionPipelineUnauthorizedError(
                "pipeline control credential is invalid"
            )
        if require_enabled and not self.enabled:
            raise ExtensionPipelineUnavailableError(
                "pipeline coordinator is disabled by policy"
            )
        return selected

    @staticmethod
    def _scope(
        user_id: str, workspace_id: str, session_id: str, token: str
    ) -> tuple[str, str, str]:
        return (
            artifact_owner_scope_digest(user_id, workspace_id),
            authenticated_local_principal_digest(token),
            initiating_session_digest(session_id),
        )

    @staticmethod
    def _owned(
        record: dict[str, Any], owner: str, principal: str, session: str
    ) -> bool:
        return bool(
            record.get("owner_scope_digest") == owner
            and record.get("principal_digest") == principal
            and record.get("session_digest") == session
        )

    def _require_owner(
        self,
        record: dict[str, Any],
        owner: str,
        principal: str,
        session: str,
    ) -> None:
        if not self._owned(record, owner, principal, session):
            raise ExtensionPipelineNotFoundError("pipeline was not found")

    def _record(
        self, state: dict[str, Any], pipeline_id: str
    ) -> dict[str, Any]:
        record = state["pipelines"].get(pipeline_id)
        if not isinstance(record, dict):
            raise ExtensionPipelineNotFoundError("pipeline was not found")
        self._validate_record(record)
        return record

    @staticmethod
    def _validate_operation(
        operation: dict[str, Any],
        *,
        operation_id: str,
        request_digest: str,
        pipeline_id: str,
    ) -> None:
        if (
            operation.get("operation_id_digest")
            != ExtensionPipelineCoordinator._operation_id_digest(
                operation_id
            )
            or operation.get("request_digest") != request_digest
            or operation.get("pipeline_id") != pipeline_id
        ):
            raise ExtensionPipelineConflictError(
                "pipeline operation identity was rebound"
            )

    @staticmethod
    def _operation_key(principal: str, session: str, operation: str) -> str:
        return hashlib.sha256(
            f"{principal}\0{session}\0{operation}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _operation_id_digest(operation: str) -> str:
        return hashlib.sha256(
            ("veyra.phase6.extension_pipeline.operation.v1\0" + operation).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _child_operation(pipeline_id: str, stage: str) -> str:
        return f"phase6-pipeline-{pipeline_id.removeprefix('extpipe_')}-{stage}"

    def _test_bundle(
        self, value: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        parsed = parse_dynamic_validation_test_bundle(value)
        canonical = parsed.canonical_dict()
        self._bounded_document(canonical, "test_bundle")
        return canonical, parsed.bundle_digest()

    def _canary_input(
        self, value: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(value, dict):
            raise ValueError("canary_input must be one JSON object")
        selected = copy.deepcopy(value)
        self._bounded_document(selected, "canary_input")
        return selected, self._digest(selected)

    @staticmethod
    def _bounded_document(value: Any, label: str) -> None:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be canonical JSON") from exc
        if len(encoded) > MAX_EXTENSION_PIPELINE_INPUT_BYTES:
            raise ValueError(f"{label} exceeds the pipeline input budget")

    def _budgets(self, **values: int) -> dict[str, int]:
        return {
            "shadow": self._revision(
                values["shadow_max_invocations"],
                "shadow_max_invocations",
                minimum=1,
                maximum=100,
            ),
            "read_only_canary": self._revision(
                values["read_only_canary_max_invocations"],
                "read_only_canary_max_invocations",
                minimum=1,
                maximum=100,
            ),
            "scoped_canary": self._revision(
                values["scoped_canary_max_invocations"],
                "scoped_canary_max_invocations",
                minimum=1,
                maximum=100,
            ),
            "promoted": self._revision(
                values["promoted_max_invocations"],
                "promoted_max_invocations",
                minimum=1,
                maximum=100,
            ),
        }

    @staticmethod
    def _generator_identity_digest(generation: dict[str, Any]) -> str:
        return ExtensionPipelineCoordinator._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_generator_service_identity.v1"
                ),
                "provider_id": generation["provider_id"],
                "model_id": generation["model_id"],
                "model_config_digest": generation["model_config_digest"],
                "generator_revision": generation["generator_revision"],
                "generation_policy_revision": (
                    EXTENSION_GENERATION_POLICY_REVISION
                ),
                "generation_policy_digest": generation[
                    "generation_policy_digest"
                ],
                "prompt_digest": generation["prompt_digest"],
            }
        )

    @staticmethod
    def _verifier_identity_digest(
        source: dict[str, Any], validation: dict[str, Any]
    ) -> str:
        return ExtensionPipelineCoordinator._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_verifier_service_identity.v1"
                ),
                "source_parser_identity": source["parser_identity"],
                "source_ruleset_digest": source["ruleset_digest"],
                "engine_identity_digest": validation[
                    "engine_identity_digest"
                ],
                "image_id": validation["image_id"],
                "isolation_conformance_digest": validation[
                    "isolation_conformance_digest"
                ],
                "validation_conformance_digest": validation[
                    "validation_conformance_digest"
                ],
                "validation_policy_revision": (
                    DYNAMIC_VALIDATION_POLICY_REVISION
                ),
                "validation_policy_digest": validation[
                    "validation_policy_digest"
                ],
                "validation_harness_revision": validation[
                    "validation_harness_revision"
                ],
                "validation_harness_digest": validation[
                    "validation_harness_digest"
                ],
            }
        )

    @staticmethod
    def _deployment_receipt(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "deployment_id": ExtensionPipelineCoordinator._required_result_id(
                result, "deployment_id", "extdep_"
            ),
            "revision": ExtensionPipelineCoordinator._result_revision(
                result, "revision", minimum=1
            ),
            "mode": ExtensionPipelineCoordinator._safe_text(
                result.get("mode")
            ),
            "mode_epoch": ExtensionPipelineCoordinator._result_revision(
                result, "mode_epoch", minimum=0
            ),
            "binding_digest": ExtensionPipelineCoordinator._required_result_digest(
                result, "binding_digest"
            ),
        }

    @staticmethod
    def _invocation_receipt(
        response: dict[str, Any],
        *,
        expected_status: str,
        expected_discarded: bool,
    ) -> dict[str, Any]:
        result = ExtensionPipelineCoordinator._result_mapping(
            response, "result"
        )
        if (
            result.get("invocation_status") != expected_status
            or result.get("output_discarded") is not expected_discarded
            or not isinstance(result.get("output_digest"), str)
            or not _DIGEST.fullmatch(result["output_digest"])
        ):
            raise _StageBlocked("canary_invocation_not_passed")
        return {
            "invocation_status": expected_status,
            "output_digest": result["output_digest"],
            "output_discarded": expected_discarded,
            "result_digest": ExtensionPipelineCoordinator._required_result_digest(
                response, "result_digest"
            ),
        }

    @staticmethod
    def _review_receipt(
        review: dict[str, Any], target_mode: str
    ) -> dict[str, Any]:
        if (
            review.get("status") != "pending"
            or review.get("target_mode") != target_mode
        ):
            raise _StageBlocked("independent_review_not_pending")
        return {
            "review_id": ExtensionPipelineCoordinator._safe_text(
                review.get("review_id")
            ),
            "review_revision": ExtensionPipelineCoordinator._result_revision(
                review, "review_revision", minimum=1
            ),
            "status": "pending",
            "target_mode": target_mode,
            "proposal_digest": ExtensionPipelineCoordinator._required_result_digest(
                review, "proposal_digest"
            ),
        }

    @staticmethod
    def _receipt(receipts: dict[str, Any], name: str) -> dict[str, Any]:
        receipt = receipts.get(name)
        if not isinstance(receipt, dict):
            raise ExtensionPipelineStorageError(
                f"pipeline {name} receipt is unavailable"
            )
        return receipt

    @staticmethod
    def _require_values(result: dict[str, Any], **expected: Any) -> None:
        if not isinstance(result, dict) or any(
            result.get(key) != value for key, value in expected.items()
        ):
            raise _StageBlocked("stage_result_did_not_pass")

    @staticmethod
    def _required_result_id(
        result: dict[str, Any], field: str, prefix: str
    ) -> str:
        value = result.get(field)
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or len(value) != len(prefix) + 24
            or any(character not in "0123456789abcdef" for character in value[len(prefix):])
        ):
            raise _StageBlocked(f"{field}_invalid")
        return value

    @staticmethod
    def _required_result_digest(result: dict[str, Any], field: str) -> str:
        value = result.get(field)
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise _StageBlocked(f"{field}_invalid")
        return value

    @staticmethod
    def _result_mapping(result: dict[str, Any], field: str) -> dict[str, Any]:
        value = result.get(field)
        if not isinstance(value, dict):
            raise _StageBlocked(f"{field}_invalid")
        return value

    @staticmethod
    def _result_revision(
        result: dict[str, Any],
        field: str,
        *,
        minimum: int,
    ) -> int:
        value = result.get(field)
        if type(value) is not int or value < minimum:
            raise _StageBlocked(f"{field}_invalid")
        return value

    @staticmethod
    def _canonical_time(value: str) -> str:
        try:
            return canonical_utc(parse_canonical_utc(str(value or "")))
        except Exception as exc:
            raise ValueError("pipeline expiry must be canonical UTC") from exc

    @staticmethod
    def _revision(
        value: int,
        label: str,
        *,
        minimum: int,
        maximum: int = 2_147_483_647,
    ) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{label} is invalid")
        return value

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > maximum or "\x00" in selected:
            raise ValueError(f"{label} is invalid")
        return selected

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        selected = str(value or "").strip()
        if not _IDENTIFIER.fullmatch(selected):
            raise ValueError(f"{label} is invalid")
        return selected

    @staticmethod
    def _pipeline_id(value: str) -> str:
        selected = str(value or "")
        if not _PIPELINE_ID.fullmatch(selected):
            raise ValueError("pipeline_id is invalid")
        return selected

    @staticmethod
    def _candidate_id(value: str) -> str:
        selected = str(value or "")
        if not _CANDIDATE_ID.fullmatch(selected):
            raise ValueError("candidate_id is invalid")
        return selected

    @staticmethod
    def _digest_value(value: str, label: str) -> str:
        selected = str(value or "")
        if not _DIGEST.fullmatch(selected):
            raise ValueError(f"{label} is invalid")
        return selected

    @staticmethod
    def _safe_text(value: Any, maximum: int = 240) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > maximum or "\x00" in selected:
            raise _StageBlocked("stage_text_invalid")
        return selected

    @staticmethod
    def _safe_issue(value: Any) -> str:
        selected = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value or "unknown"))
        return selected[:120] or "unknown"

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _now_iso(self) -> str:
        return canonical_utc(self._now().astimezone(timezone.utc))

    @staticmethod
    def _authority() -> dict[str, Any]:
        return ExtensionPipelineAuthority().model_dump(mode="json")


__all__ = [
    "PIPELINE_STATE_FILE",
    "ExtensionPipelineConflictError",
    "ExtensionPipelineCoordinator",
    "ExtensionPipelineError",
    "ExtensionPipelineNotFoundError",
    "ExtensionPipelineStorageError",
    "ExtensionPipelineUnauthorizedError",
    "ExtensionPipelineUnavailableError",
]
