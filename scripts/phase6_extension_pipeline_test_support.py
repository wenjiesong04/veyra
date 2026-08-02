from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_HARNESS_REVISION,
    DYNAMIC_VALIDATION_POLICY_DIGEST,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_generation import (
    EXTENSION_GENERATION_POLICY_DIGEST,
    EXTENSION_GENERATION_PROMPT_DIGEST,
    EXTENSION_GENERATOR_REVISION,
)
from runtime.extension_pipeline_coordinator import (
    ExtensionPipelineCoordinator,
)
from scripts.phase6_extension_dynamic_validation_test_support import (
    valid_test_bundle,
)


BASE_TIME = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
USER = "phase6-pipeline-user"
WORKSPACE = str(Path(__file__).resolve().parents[1])
SESSION = "phase6-pipeline-session"
TOKEN = "phase6-pipeline-control-token"
CANDIDATE_ID = "extspec_" + "1" * 24
SPEC_DIGEST = "2" * 64
ARTIFACT_ID = "extart_" + "3" * 24
ARTIFACT_SHA = "4" * 64
GENERATION_ID = "extgen_" + "5" * 24
GENERATION_REPORT_DIGEST = "6" * 64
CHECK_ID = "extcheck_" + "7" * 24
SOURCE_REPORT_DIGEST = "8" * 64
RUN_ID = "extrun_" + "9" * 24
RUN_REPORT_DIGEST = "a" * 64
VALIDATION_ID = "extval_" + "b" * 24
VALIDATION_REPORT_DIGEST = "c" * 64
RELEASE_ID = "extrel_" + "d" * 24
ATTESTATION_DIGEST = "e" * 64
MANIFEST_DIGEST = "f" * 64
SIGNER_IDENTITY = "0" * 64
DEPLOYMENT_ID = "extdep_" + "1" * 24


class Clock:
    def __init__(self) -> None:
        self.current = BASE_TIME

    def __call__(self) -> datetime:
        return self.current


class MemoryStore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.mutation_count = 0
        self.fail_on_mutation: int | None = None

    def read_json(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self.documents.get(name, {}))

    def mutate_json(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        self.mutation_count += 1
        if self.fail_on_mutation == self.mutation_count:
            self.fail_on_mutation = None
            raise RuntimeError("injected checkpoint crash")
        working = copy.deepcopy(self.documents.get(name, {}))
        selected = mutator(working)
        payload = working if selected is None else selected
        self.documents[name] = copy.deepcopy(payload)
        return copy.deepcopy(payload)

    def snapshot(self) -> bytes:
        return json.dumps(
            self.documents,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class FakeGenerationGate:
    def __init__(self) -> None:
        self.effects: set[str] = set()

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        self.effects.add(kwargs["operation_id"])
        return {
            "generation_id": GENERATION_ID,
            "candidate_id": CANDIDATE_ID,
            "stored_stage": "GENERATION_QUARANTINED",
            "effective_status": "GENERATION_QUARANTINED",
            "generation_status": "quarantined",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": ARTIFACT_SHA,
            "provider_id": "provider.pipeline.test",
            "model_id": "model.pipeline.test",
            "model_config_digest": "3" * 64,
            "generator_revision": EXTENSION_GENERATOR_REVISION,
            "prompt_digest": EXTENSION_GENERATION_PROMPT_DIGEST,
            "generation_policy_digest": EXTENSION_GENERATION_POLICY_DIGEST,
            "report_digest": GENERATION_REPORT_DIGEST,
        }


class FakeArtifactQuarantine:
    def get(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["artifact_id"] != ARTIFACT_ID:
            raise RuntimeError("artifact mismatch")
        return {
            "artifact_id": ARTIFACT_ID,
            "candidate_id": CANDIDATE_ID,
            "artifact_revision": 1,
            "artifact_sha256": ARTIFACT_SHA,
            "artifact_status": "quarantined",
            "effective_status": "ARTIFACT_QUARANTINED",
        }


class FakeSourceCheckGate:
    def start(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "check_id": CHECK_ID,
            "effective_status": "SOURCE_CHECK_PASSED",
            "source_check_status": "passed",
            "source_syntax_status": "passed",
            "static_checks_status": "passed",
            "static_security_policy_status": "passed",
            "source_check_report_digest": SOURCE_REPORT_DIGEST,
            "parser_identity": "cpython_ast_feature_3_11",
            "ruleset_digest": "5" * 64,
        }


class FakeIsolatedRunnerGate:
    def __init__(self) -> None:
        self.state_store = MemoryStore()

    def start(self, **kwargs: Any) -> dict[str, Any]:
        self.state_store.documents[
            "phase6_extension_isolated_runner_state.json"
        ] = {
            "runs": {
                RUN_ID: {
                    "stage": "RUNNER_JOB_PASSED",
                    "report_digest": RUN_REPORT_DIGEST,
                }
            }
        }
        return {
            "run_id": RUN_ID,
            "effective_status": "RUNNER_JOB_PASSED",
            "probe_status": "passed",
            "trusted_isolated_runner_status": "passed",
            "engine_identity_digest": "6" * 64,
            "image_id": "sha256:" + "7" * 64,
            "runner_policy_digest": "8" * 64,
        }

    def integrity(self, **kwargs: Any) -> dict[str, Any]:
        return {"report_integrity_status": "validated"}


class FakeDynamicValidationGate:
    def __init__(self) -> None:
        self.state_store = MemoryStore()

    def start(self, **kwargs: Any) -> dict[str, Any]:
        bundle = parse_dynamic_validation_test_bundle(kwargs["test_bundle"])
        self.state_store.documents[
            "phase6_extension_dynamic_validation_state.json"
        ] = {
            "validations": {
                VALIDATION_ID: {
                    "stage": "DYNAMIC_VALIDATION_PASSED",
                    "report_digest": VALIDATION_REPORT_DIGEST,
                }
            }
        }
        return {
            "validation_id": VALIDATION_ID,
            "effective_status": "DYNAMIC_VALIDATION_PASSED",
            "candidate_execution_status": "passed",
            "unit_checks_status": "passed",
            "contract_checks_status": "passed",
            "security_runtime_checks_status": "passed",
            "fuzz_checks_status": "passed",
            "behavior_verification_status": "passed",
            "test_bundle_digest": bundle.bundle_digest(),
            "build_identity_digest": "9" * 64,
            "engine_identity_digest": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "isolation_conformance_digest": "c" * 64,
            "validation_conformance_digest": "d" * 64,
            "validation_policy_digest": DYNAMIC_VALIDATION_POLICY_DIGEST,
            "validation_harness_revision": DYNAMIC_VALIDATION_HARNESS_REVISION,
            "validation_harness_digest": "e" * 64,
        }

    def integrity(self, **kwargs: Any) -> dict[str, Any]:
        return {"report_integrity_status": "validated"}


class FakeReleaseRegistry:
    def __init__(self) -> None:
        self.registry_revision = 0
        self.create_calls = 0

    def status(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "storage": {"registry_revision": self.registry_revision},
            "signer": {
                "signing_service_identity_digest": SIGNER_IDENTITY
            },
        }

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls += 1
        if kwargs["expected_registry_revision"] != self.registry_revision:
            raise RuntimeError("release registry CAS mismatch")
        self.registry_revision += 1
        return {
            "release_id": RELEASE_ID,
            "release_revision": 1,
            "registry_revision": self.registry_revision,
            "effective_status": "RELEASE_SIGNED",
            "fresh": True,
            "revoked": False,
            "signature_algorithm": "ed25519",
            "signature_verification_status": "verified",
            "attestation_digest": ATTESTATION_DIGEST,
            "manifest_digest": MANIFEST_DIGEST,
        }


class FakeDeploymentGate:
    def __init__(self) -> None:
        self.state_revision = 0
        self.deployment = {
            "deployment_id": DEPLOYMENT_ID,
            "revision": 0,
            "mode": "disabled",
            "mode_epoch": 0,
            "stage": "DISABLED",
            "binding_digest": "0" * 64,
            "successful_modes": [],
        }
        self.reviews: dict[str, dict[str, Any]] = {}
        self.approval_calls = 0

    def status(self, **kwargs: Any) -> dict[str, Any]:
        return {"state_revision": self.state_revision}

    def propose(self, **kwargs: Any) -> dict[str, Any]:
        self._state_cas(kwargs)
        self.deployment.update(
            {
                "revision": 1,
                "mode": "record_only",
                "mode_epoch": 0,
                "stage": "PROPOSED",
                "binding_digest": "1" * 64,
                "successful_modes": ["record_only"],
            }
        )
        self.state_revision += 1
        return copy.deepcopy(self.deployment)

    def transition(self, **kwargs: Any) -> dict[str, Any]:
        self._deployment_cas(kwargs)
        target = kwargs["target_mode"]
        expected = {
            "record_only": "shadow",
            "shadow": "read_only_canary",
            "read_only_canary": "scoped_canary",
            "scoped_canary": "promoted",
        }[self.deployment["mode"]]
        if target != expected:
            raise RuntimeError("transition order mismatch")
        if target in {"scoped_canary", "promoted"}:
            review = self.reviews.get(str(kwargs.get("review_id")))
            if not review or review["status"] != "approved" or review["target_mode"] != target:
                raise RuntimeError("independent approval missing")
        self.deployment["revision"] += 1
        self.deployment["mode_epoch"] += 1
        self.deployment["mode"] = target
        self.deployment["stage"] = target.upper()
        self.deployment["binding_digest"] = hashlib.sha256(
            target.encode()
        ).hexdigest()
        self.state_revision += 1
        return copy.deepcopy(self.deployment)

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self._deployment_cas(kwargs)
        mode = self.deployment["mode"]
        discarded = mode == "shadow"
        status = "discarded" if discarded else "passed"
        output = {"label": "PRIVATE_CANARY_OUTPUT"}
        output_digest = hashlib.sha256(
            json.dumps(
                output, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.deployment["revision"] += 1
        if mode not in self.deployment["successful_modes"]:
            self.deployment["successful_modes"].append(mode)
        self.state_revision += 1
        return {
            "result": {
                "invocation_status": status,
                "output_payload": None if discarded else output,
                "output_digest": output_digest,
                "output_discarded": discarded,
            },
            "result_digest": hashlib.sha256(
                (mode + output_digest).encode()
            ).hexdigest(),
            "deployment": copy.deepcopy(self.deployment),
            "state_revision": self.state_revision,
        }

    def request_transition_review(self, **kwargs: Any) -> dict[str, Any]:
        self._deployment_cas(kwargs)
        target = kwargs["target_mode"]
        review_id = f"review-{target}"
        review = {
            "review_id": review_id,
            "review_revision": 1,
            "status": "pending",
            "target_mode": target,
            "proposal_digest": hashlib.sha256(target.encode()).hexdigest(),
        }
        self.reviews[review_id] = review
        return copy.deepcopy(review)

    def approve_outside_coordinator(self, review_id: str) -> None:
        self.approval_calls += 1
        self.reviews[review_id]["status"] = "approved"

    def public_registry(self, **kwargs: Any) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if self.deployment["mode"] == "promoted":
            items.append(
                {
                    "capability_id": "extension:example.pipeline",
                    "deployment_id": DEPLOYMENT_ID,
                    "release_id": RELEASE_ID,
                    "attestation_digest": ATTESTATION_DIGEST,
                    "input_schema_digest": "2" * 64,
                    "output_schema_digest": "3" * 64,
                    "execution_boundary": "trusted_isolated_runner_only",
                }
            )
        return {"items": items}

    def _state_cas(self, kwargs: dict[str, Any]) -> None:
        if kwargs["expected_state_revision"] != self.state_revision:
            raise RuntimeError("deployment state CAS mismatch")

    def _deployment_cas(self, kwargs: dict[str, Any]) -> None:
        self._state_cas(kwargs)
        if (
            kwargs["expected_deployment_revision"]
            != self.deployment["revision"]
            or kwargs["expected_mode_epoch"]
            != self.deployment["mode_epoch"]
        ):
            raise RuntimeError("deployment record CAS mismatch")


def build_context(*, enabled: bool = True) -> dict[str, Any]:
    store = MemoryStore()
    generation = FakeGenerationGate()
    artifact = FakeArtifactQuarantine()
    source = FakeSourceCheckGate()
    isolated = FakeIsolatedRunnerGate()
    dynamic = FakeDynamicValidationGate()
    release = FakeReleaseRegistry()
    deployment = FakeDeploymentGate()
    clock = Clock()
    coordinator = ExtensionPipelineCoordinator(
        state_store=store,
        generation_gate=generation,
        artifact_quarantine=artifact,
        source_check_gate=source,
        isolated_runner_gate=isolated,
        dynamic_validation_gate=dynamic,
        release_registry=release,
        deployment_gate=deployment,
        enabled=enabled,
        control_token=TOKEN,
        now=clock,
    )
    return {
        "store": store,
        "generation": generation,
        "deployment": deployment,
        "coordinator": coordinator,
    }


def start_kwargs(*, operation_id: str = "pipeline-start") -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "request_id": "pipeline-request",
        "candidate_id": CANDIDATE_ID,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "expected_state_revision": 0,
        "expected_candidate_revision": 1,
        "expected_spec_digest": SPEC_DIGEST,
        "test_bundle": valid_test_bundle(),
        "canary_input": {"name": "PRIVATE_CANARY_INPUT", "count": 1},
        "release_expires_at": (BASE_TIME + timedelta(days=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "deployment_expires_at": (BASE_TIME + timedelta(hours=12))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "shadow_max_invocations": 2,
        "read_only_canary_max_invocations": 2,
        "scoped_canary_max_invocations": 2,
        "promoted_max_invocations": 10,
        "control_token": TOKEN,
    }


def advance_kwargs(
    pipeline: dict[str, Any],
    *,
    operation_id: str,
    review_id: str | None,
) -> dict[str, Any]:
    return {
        "pipeline_id": pipeline["pipeline_id"],
        "operation_id": operation_id,
        "expected_pipeline_revision": pipeline["pipeline_revision"],
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "test_bundle": valid_test_bundle(),
        "canary_input": {"name": "PRIVATE_CANARY_INPUT", "count": 1},
        "review_id": review_id,
        "control_token": TOKEN,
    }


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


__all__ = [
    "CANDIDATE_ID",
    "SESSION",
    "SPEC_DIGEST",
    "TOKEN",
    "USER",
    "WORKSPACE",
    "MemoryStore",
    "advance_kwargs",
    "build_context",
    "expect",
    "start_kwargs",
]
