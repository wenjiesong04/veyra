from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from core.autonomy_policy import JSON_SANDBOX_REPAIR_PROFILE
from core.capability_registry import CapabilityRegistry
from core.foresight_contract import capability_effect_contracts
from core.learning_record import LearningRecordValidationError
from core.world_state import WorldStateStore
from interface.agent_contract import AGENT_CONTRACT_VERSION
from interface.provider_certification import certify_agent_provider
from runtime.foresight_runtime import ForesightRuntime
from runtime.learning_calibration_runtime import (
    LearningCalibrationConflict,
    LearningCalibrationRuntime,
    LearningCalibrationStorageError,
)
from runtime.performance_portfolio import PerformancePortfolio
from runtime.playbook_registry import (
    PlaybookRegistration,
    PlaybookRegistry,
)
from runtime.sandbox_repair_playbook import (
    IMPLEMENTATION_REVISION as SANDBOX_IMPLEMENTATION_REVISION,
    MAX_CANDIDATE_BYTES,
    PLAYBOOK_ID as SANDBOX_PLAYBOOK_ID,
    JsonSandboxRepairPlaybook,
    JsonSandboxRepairRequest,
)


PHASE5_STATUS_SCHEMA = "veyra.phase5.control_plane_status.v1"
PHASE5_FEEDBACK_COMMAND_SCHEMA = "veyra.phase5.feedback_command.v1"
PHASE5_SANDBOX_COMMAND_SCHEMA = (
    "veyra.phase5.json_sandbox_candidate_command.v1"
)
PHASE5_PROVIDER_PROJECTION_SCHEMA = (
    "veyra.phase5.provider_certification_projection.v1"
)
_PUBLIC_RUNTIME_ID = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,240}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CERTIFICATION_STATUSES = frozenset(
    {
        "validated",
        "stale",
        "not_configured",
        "incompatible",
        "unverified",
    }
)
_FRESHNESS_STATUSES = frozenset({"fresh", "stale", "unknown"})
_RUNTIME_STATUSES = frozenset(
    {
        "available",
        "ok",
        "success",
        "adapter_unconfigured",
        "not_configured",
        "unconfigured",
        "unknown",
    }
)
_COMPATIBILITY_STATUSES = frozenset(
    {"compatible", "incompatible", "unconfigured", "unknown"}
)
_REQUIRED_PROVIDER_FEATURES = (
    "structured_task_packet",
    "rendered_prompt_fallback",
    "task_status",
    "stop_task",
)
_FIXED_PROVIDER_ISSUES = frozenset(
    {
        "capability_observation_missing",
        "missing_or_invalid_observed_at",
        "observed_at_in_future",
        "observation_stale",
        "runtime_identity_not_advertised",
        "runtime_identity_mismatch",
        "provider_not_configured",
        "provider_not_connected",
        "compatibility_incompatible",
        "compatibility_not_verified",
        "contract_version_mismatch",
        "contract_version_not_advertised",
        "provider_certification_input_invalid",
    }
)


class Phase5FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["veyra.phase5.feedback_command.v1"]
    feedback_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    user_id: str = Field(min_length=1, max_length=240)
    assessment_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    assessment_revision: str = Field(pattern=_OPAQUE_ID_PATTERN)
    candidate_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    candidate_revision: str = Field(pattern=_OPAQUE_ID_PATTERN)
    label: Literal[
        "useful",
        "not_useful",
        "too_frequent",
        "wrong_timing",
        "wrong_evidence",
    ]
    supersedes_learning_id: str | None = Field(
        default=None,
        pattern=_OPAQUE_ID_PATTERN,
    )


class Phase5SandboxCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "veyra.phase5.json_sandbox_candidate_command.v1"
    ]
    playbook_id: Literal["sandbox_repair.json_candidate.v1"]
    version: Literal[1]
    implementation_revision: Literal[
        "veyra.phase5.json_sandbox_repair.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    candidate_json: str = Field(
        min_length=1,
        max_length=MAX_CANDIDATE_BYTES,
    )
    expected_digest: str = Field(pattern=_DIGEST_PATTERN)


def build_phase5_router(
    *,
    state_store: WorldStateStore,
    foresight_runtime: ForesightRuntime,
    learning_runtime: LearningCalibrationRuntime | None = None,
    performance_portfolio: PerformancePortfolio | None = None,
    sandbox_playbook: JsonSandboxRepairPlaybook | None = None,
    playbook_registry: PlaybookRegistry | None = None,
) -> APIRouter:
    """Build the isolated Phase 5 shadow-calibration control plane.

    The router owns no mode/configuration endpoint.  Its only state-changing
    operations are exact categorical feedback and the immutable built-in JSON
    candidate playbook.  The playbook itself decides from trusted ops config;
    default shadow mode has zero persistence, while scoped canary can write
    only below the Veyra state root.
    """

    learning = learning_runtime or LearningCalibrationRuntime(
        state_store=state_store
    )
    portfolio = performance_portfolio or PerformancePortfolio(
        state_store=state_store
    )
    sandbox = sandbox_playbook or JsonSandboxRepairPlaybook(
        state_store=state_store
    )
    for label, runtime in (
        ("learning_runtime", learning),
        ("performance_portfolio", portfolio),
        ("foresight_runtime", foresight_runtime),
        ("sandbox_playbook", sandbox),
    ):
        if getattr(runtime, "state_store", None) is not state_store:
            raise ValueError(
                f"{label} must use the router's exact state store"
            )
    if type(sandbox) is not JsonSandboxRepairPlaybook:
        raise TypeError(
            "sandbox_playbook must be the fixed built-in implementation"
        )

    registry = playbook_registry or PlaybookRegistry(
        (
            PlaybookRegistration(
                playbook_id=SANDBOX_PLAYBOOK_ID,
                version=sandbox.spec.version,
                implementation_revision=(
                    SANDBOX_IMPLEMENTATION_REVISION
                ),
                domain=JSON_SANDBOX_REPAIR_PROFILE.domain,
                profile_id=JSON_SANDBOX_REPAIR_PROFILE.profile_id,
                maximum_level=(
                    JSON_SANDBOX_REPAIR_PROFILE.level.value
                ),
                risk_floor=sandbox.spec.risk_floor,
                allowed_modes=(
                    "disabled",
                    "record_only",
                    "shadow",
                    "scoped_canary",
                ),
                runner=sandbox.run,
                status_reader=sandbox.status,
            ),
        )
    )
    matching_sandbox = [
        item
        for item in registry.catalog()
        if item.get("playbook_id") == SANDBOX_PLAYBOOK_ID
        and item.get("version") == sandbox.spec.version
        and item.get("implementation_revision")
        == SANDBOX_IMPLEMENTATION_REVISION
    ]
    if len(matching_sandbox) != 1:
        raise ValueError(
            "playbook_registry must contain the exact built-in sandbox repair"
        )
    router = APIRouter(prefix="/phase5", tags=["phase5"])

    @router.get("/status")
    async def phase5_status() -> dict[str, Any]:
        learning_status = await run_in_threadpool(learning.status)
        foresight_status = await run_in_threadpool(
            foresight_runtime.status
        )
        sandbox_status = await run_in_threadpool(
            _safe_sandbox_status,
            registry,
            sandbox,
        )
        registry_status = await run_in_threadpool(
            _safe_registry_status,
            registry,
        )
        portfolio_status = await run_in_threadpool(
            _safe_portfolio_status,
            portfolio,
        )
        provider_status = await run_in_threadpool(
            _safe_provider_status,
            state_store,
        )
        degraded = (
            learning_status.get("status") == "degraded"
            or foresight_status.get("status") == "degraded"
            or sandbox_status.get("status") == "fault"
            or registry_status.get("status") == "degraded"
            or portfolio_status.get("status") == "degraded"
            or provider_status.get("status") == "degraded"
        )
        return {
            "schema_version": PHASE5_STATUS_SCHEMA,
            "status": "technical_complete_shadow_calibration",
            "operational_health": (
                "degraded" if degraded else "available"
            ),
            "completion_scope": (
                "shadow calibration and Veyra-private sandbox canary only"
            ),
            "components": {
                "learning_calibration": learning_status,
                "foresight": foresight_status,
                "sandbox_repair": _public_sandbox_result(
                    sandbox_status
                ),
                "playbook_registry": {
                    "status": registry_status.get("status"),
                    "kind": registry_status.get("registry_kind"),
                    "dynamic_registration": (
                        registry_status.get("dynamic_registration") is True
                    ),
                    "catalog": list(registry.catalog()),
                    "playbooks": registry_status.get("playbooks", []),
                },
                "performance_portfolio": portfolio_status,
                "provider_certification": provider_status,
            },
            "autonomy": {
                "scope": "domain_scoped",
                "global_level": None,
                "A3": {
                    "status": "private_sandbox_only",
                    "target_scope": (
                        "veyra_private:sandbox_repair_json"
                    ),
                    "workspace_authority": False,
                    "production_authority": False,
                    "promotion_authority": False,
                },
                "A4": "not_certified",
                "A5": "not_certified",
            },
            "authority": _phase5_authority(),
            "validation_boundary": {
                "live_provider_validation_required": True,
                "remote_multitenant_api": "not_certified",
                "production_promotion": "not_certified",
            },
        }

    @router.get("/portfolio")
    async def phase5_portfolio(
        limit: int = Query(default=1000, ge=1, le=10_000),
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            portfolio.snapshot,
            limit=limit,
        )

    @router.get("/foresight/contracts")
    async def phase5_foresight_contracts() -> dict[str, Any]:
        contracts = await run_in_threadpool(
            capability_effect_contracts
        )
        return {
            "status": "available",
            "registry_kind": "fixed_immutable",
            "contract_count": len(contracts),
            "contracts": [
                contract.model_dump(mode="json")
                for contract in contracts
            ],
            "authority": {
                **_phase5_authority(),
                "prediction_is_authorization": False,
                "eligibility_applies_promotion": False,
            },
        }

    @router.get("/foresight/status")
    async def phase5_foresight_status() -> dict[str, Any]:
        return await run_in_threadpool(foresight_runtime.status)

    @router.get("/providers/certification")
    async def phase5_provider_certification() -> dict[str, Any]:
        return await run_in_threadpool(
            _provider_certification_projection,
            state_store,
        )

    @router.post("/feedback")
    async def phase5_feedback(
        request: Phase5FeedbackRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                learning.record_feedback,
                feedback_id=request.feedback_id,
                user_id=request.user_id,
                assessment_id=request.assessment_id,
                assessment_revision=request.assessment_revision,
                candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision,
                label=request.label,
                supersedes_learning_id=(
                    request.supersedes_learning_id
                ),
            )
        except LearningCalibrationConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="feedback_target_conflict",
            ) from exc
        except LearningCalibrationStorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="learning_calibration_unavailable",
            ) from exc
        except (LearningRecordValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_feedback_contract",
            ) from exc
        return _public_feedback_result(result)

    @router.post("/sandbox/json-candidate")
    async def phase5_sandbox_json_candidate(
        request: Phase5SandboxCandidateRequest,
    ) -> dict[str, Any]:
        candidate = JsonSandboxRepairRequest(
            operation_id=request.operation_id,
            candidate_json=request.candidate_json,
            expected_digest=request.expected_digest,
        )
        try:
            result = await run_in_threadpool(
                sandbox.run,
                candidate,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="playbook_identity_conflict",
            ) from exc
        return _public_sandbox_result(result)

    return router


def _safe_sandbox_status(
    registry: PlaybookRegistry,
    sandbox: JsonSandboxRepairPlaybook,
) -> dict[str, Any]:
    try:
        return registry.playbook_status(
            playbook_id=SANDBOX_PLAYBOOK_ID,
            version=sandbox.spec.version,
            implementation_revision=SANDBOX_IMPLEMENTATION_REVISION,
        )
    except Exception as exc:
        return {
            "playbook_id": SANDBOX_PLAYBOOK_ID,
            "status": "fault",
            "mode": "fail_closed",
            "effective_autonomy_level": "A0",
            "fault_code": (
                f"sandbox_status_unavailable:{type(exc).__name__}"
            ),
        }


def _safe_registry_status(
    registry: PlaybookRegistry,
) -> dict[str, Any]:
    try:
        return registry.status()
    except Exception as exc:
        return {
            "status": "degraded",
            "registry_kind": "builtin_immutable",
            "dynamic_registration": False,
            "playbooks": [],
            "fault_code": (
                f"registry_status_unavailable:{type(exc).__name__}"
            ),
        }


def _safe_portfolio_status(
    portfolio: PerformancePortfolio,
) -> dict[str, Any]:
    try:
        projection = portfolio.snapshot(limit=1000)
    except Exception as exc:
        return {
            "status": "degraded",
            "actor_count": 0,
            "source_count": 0,
            "degraded_source_count": 0,
            "fault_code": (
                f"portfolio_status_unavailable:{type(exc).__name__}"
            ),
        }
    sources = (
        projection.get("sources")
        if isinstance(projection.get("sources"), list)
        else []
    )
    return {
        "status": str(projection.get("status") or "degraded"),
        "actor_count": int(projection.get("actor_count") or 0),
        "source_count": len(sources),
        "degraded_source_count": sum(
            isinstance(item, dict)
            and item.get("status") == "degraded"
            for item in sources
        ),
        "authority": (
            projection.get("authority")
            if isinstance(projection.get("authority"), dict)
            else {
                "mode": "read_only_shadow_portfolio",
                "policy_effect": "none",
                "route_selection_allowed": False,
                "provider_selection_allowed": False,
                "provider_switch_allowed": False,
                "model_weight_change_allowed": False,
                "autonomy_change_allowed": False,
                "capability_grant_allowed": False,
                "execution_allowed": False,
            }
        ),
    }


def _safe_provider_status(
    state_store: WorldStateStore,
) -> dict[str, Any]:
    try:
        projection = _provider_certification_projection(state_store)
    except Exception as exc:
        return {
            "status": "degraded",
            "certification_count": 0,
            "validated_count": 0,
            "rejected_runtime_count": 0,
            "selected": {
                "runtime": "unknown",
                "certification": _public_provider_certification({}),
            },
            "fault_code": (
                f"provider_certification_unavailable:{type(exc).__name__}"
            ),
            "authority": _provider_authority(),
        }
    return {
        "status": projection.get("status"),
        "certification_count": projection.get(
            "certification_count", 0
        ),
        "validated_count": projection.get("validated_count", 0),
        "rejected_runtime_count": projection.get(
            "rejected_runtime_count", 0
        ),
        "selected": projection.get("selected"),
        "authority": projection.get("authority"),
    }


def _provider_certification_projection(
    state_store: WorldStateStore,
) -> dict[str, Any]:
    matrix = state_store.read_json("ops_runtime_matrix.json")
    capability_snapshot = CapabilityRegistry(state_store).snapshot()
    selected_agent = (
        capability_snapshot.get("selected_agent")
        if isinstance(capability_snapshot.get("selected_agent"), dict)
        else {}
    )
    selected_runtime = _public_runtime(
        selected_agent.get("name"),
        fallback="openclaw",
    )
    selected_certification_raw = (
        capability_snapshot.get("provider_certification")
        if isinstance(
            capability_snapshot.get("provider_certification"), dict
        )
        else {}
    )
    selected_certification = _public_provider_certification(
        selected_certification_raw,
        runtime_hint=selected_runtime,
    )
    authority = _provider_authority()
    if (
        not isinstance(matrix, dict)
        or matrix.get("_state_corrupt") is True
        or not isinstance(matrix.get("runtimes"), list)
    ):
        return {
            "schema_version": PHASE5_PROVIDER_PROJECTION_SCHEMA,
            "status": "degraded",
            "certifications": [],
            "rejected_runtime_count": 0,
            "reason": "runtime_matrix_untrusted",
            "selected": {
                "runtime": selected_runtime,
                "certification": selected_certification,
            },
            "authority": authority,
        }
    observed_at = (
        matrix.get("observed_at")
        or matrix.get("checked_at")
        or matrix.get("updated_at")
    )
    ttl_seconds = matrix.get("ttl_seconds") or 1800
    certifications: list[dict[str, Any]] = []
    rejected = 0
    for row in matrix["runtimes"][:64]:
        if not isinstance(row, dict):
            rejected += 1
            continue
        runtime = str(row.get("name") or "")
        capabilities = row.get("capabilities")
        adapter_binding = (
            row.get("adapter_binding")
            if isinstance(row.get("adapter_binding"), dict)
            else {}
        )
        trusted_native_adapter = bool(
            runtime == "openclaw"
            and adapter_binding.get("trusted_native_adapter") is True
            and adapter_binding.get("source")
            == "local_adapter_instance"
        )
        if (
            not _PUBLIC_RUNTIME_ID.fullmatch(runtime)
            or not isinstance(capabilities, dict)
        ):
            rejected += 1
            continue
        try:
            certification = certify_agent_provider(
                runtime=runtime,
                capabilities=capabilities,
                observed_at=observed_at,
                ttl_seconds=ttl_seconds,
                trusted_native_adapter=trusted_native_adapter,
            )
        except (TypeError, ValueError):
            rejected += 1
            continue
        certifications.append(
            _public_provider_certification(
                certification,
                runtime_hint=runtime,
            )
        )
    certifications.sort(key=lambda item: str(item.get("runtime") or ""))
    return {
        "schema_version": PHASE5_PROVIDER_PROJECTION_SCHEMA,
        "status": (
            "degraded"
            if rejected
            else "available"
            if certifications
            else "empty"
        ),
        "certifications": certifications,
        "certification_count": len(certifications),
        "validated_count": sum(
            item.get("validated") is True
            for item in certifications
        ),
        "rejected_runtime_count": rejected,
        "selected": {
            "runtime": selected_runtime,
            "certification": selected_certification,
        },
        "authority": authority,
    }


def _public_provider_certification(
    certification: Any,
    *,
    runtime_hint: str = "unknown",
) -> dict[str, Any]:
    document = certification if isinstance(certification, dict) else {}
    document_runtime = _public_runtime(document.get("runtime"))
    selected_hint = _public_runtime(runtime_hint)
    runtime = (
        selected_hint
        if selected_hint != "unknown"
        else document_runtime
    )
    runtime_identity_bound = (
        document_runtime != "unknown"
        and document_runtime == runtime
    )
    certification_status = _enum_value(
        document.get("certification_status"),
        _CERTIFICATION_STATUSES,
        "unverified",
    )
    freshness = (
        document.get("freshness")
        if isinstance(document.get("freshness"), dict)
        else {}
    )
    evidence = (
        document.get("evidence")
        if isinstance(document.get("evidence"), dict)
        else {}
    )
    contract = (
        evidence.get("contract")
        if isinstance(evidence.get("contract"), dict)
        else {}
    )
    feature_document = (
        evidence.get("required_features")
        if isinstance(evidence.get("required_features"), dict)
        else {}
    )
    issues = (
        document.get("issues")
        if isinstance(document.get("issues"), list)
        else []
    )
    public_issues: list[str] = []
    for issue in issues[:64]:
        if not isinstance(issue, str):
            continue
        if issue in _FIXED_PROVIDER_ISSUES:
            public_issues.append(issue)
            continue
        for feature in _REQUIRED_PROVIDER_FEATURES:
            if issue in {
                f"required_feature_disabled:{feature}",
                f"required_feature_not_advertised:{feature}",
            }:
                public_issues.append(issue)
                break
    if document and not runtime_identity_bound:
        public_issues.append("runtime_identity_mismatch")
    ttl_seconds = _bounded_int(
        document.get("ttl_seconds"),
        minimum=1,
        maximum=86_400,
    )
    age_seconds = _bounded_int(
        freshness.get("age_seconds"),
        minimum=0,
        maximum=31_536_000,
    )
    public_freshness_status = _enum_value(
        freshness.get("status"),
        _FRESHNESS_STATUSES,
        "unknown",
    )
    public_runtime_identity_match = (
        runtime_identity_bound
        and evidence.get("runtime_identity_match") is True
    )
    public_connected = evidence.get("connected") is True
    public_contract_match = contract.get("exact_match") is True
    public_features = {
        feature: (
            True
            if feature_document.get(feature) is True
            else False
            if feature_document.get(feature) is False
            else None
        )
        for feature in _REQUIRED_PROVIDER_FEATURES
    }
    public_validated = (
        document.get("validated") is True
        and certification_status == "validated"
        and public_freshness_status == "fresh"
        and public_runtime_identity_match
        and public_connected
        and public_contract_match
        and all(value is True for value in public_features.values())
    )
    if document.get("validated") is True and not public_validated:
        certification_status = "incompatible"
    return {
        "schema_version": "veyra.provider_certification.v1",
        "runtime": runtime,
        "certification_status": certification_status,
        "validated": public_validated,
        "observed_at": _public_timestamp(document.get("observed_at")),
        "ttl_seconds": ttl_seconds,
        "expires_at": _public_timestamp(document.get("expires_at")),
        "freshness": {
            "status": public_freshness_status,
            "age_seconds": age_seconds,
        },
        "evidence": {
            "runtime_identity_match": public_runtime_identity_match,
            "connected": public_connected,
            "runtime_status": _enum_value(
                evidence.get("runtime_status"),
                _RUNTIME_STATUSES,
                "unknown",
            ),
            "compatibility_status": _enum_value(
                evidence.get("compatibility_status"),
                _COMPATIBILITY_STATUSES,
                "unknown",
            ),
            "native_adapter": (
                evidence.get("native_adapter") is True
            ),
            "native_adapter_claimed": (
                evidence.get("native_adapter_claimed") is True
            ),
            "contract": {
                "expected": AGENT_CONTRACT_VERSION,
                "exact_match": public_contract_match,
            },
            "required_features": public_features,
        },
        "issues": sorted(set(public_issues)),
        "diagnostic_reads_eligible": (
            public_validated
            and document.get("diagnostic_reads_eligible") is True
        ),
        "read_only_dispatch_allowed": False,
        "automatic_selection_allowed": False,
        "provider_switch_allowed": False,
        "side_effect_dispatch_allowed": False,
        "dispatch_authority": "none",
        "policy_effect": "none",
    }


def _provider_authority() -> dict[str, Any]:
    return {
        "policy_effect": "none",
        "automatic_selection_allowed": False,
        "provider_switch_allowed": False,
        "side_effect_dispatch_allowed": False,
    }


def _public_runtime(value: Any, *, fallback: str = "unknown") -> str:
    if isinstance(value, str) and _PUBLIC_RUNTIME_ID.fullmatch(value):
        return value
    if isinstance(fallback, str) and _PUBLIC_RUNTIME_ID.fullmatch(fallback):
        return fallback
    return "unknown"


def _enum_value(
    value: Any,
    allowed: frozenset[str],
    fallback: str,
) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _bounded_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if (
        type(value) is int
        and minimum <= value <= maximum
    ):
        return value
    return None


def _public_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _public_feedback_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=503,
            detail="learning_calibration_invalid_projection",
        )
    record = (
        result.get("record")
        if isinstance(result.get("record"), dict)
        else {}
    )
    return {
        "status": str(result.get("status") or "unknown"),
        "record": {
            key: record.get(key)
            for key in (
                "schema_version",
                "learning_id",
                "feedback_id",
                "assessment_id",
                "assessment_revision",
                "candidate_id",
                "candidate_revision",
                "label",
                "status",
                "supersedes_learning_id",
                "superseded_by_learning_id",
                "promotion_status",
                "policy_effect",
                "source",
                "created_at",
                "updated_at",
            )
        },
        "authority": (
            result.get("authority")
            if isinstance(result.get("authority"), dict)
            else _phase5_authority()
        ),
    }


def _public_sandbox_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=503,
            detail="sandbox_playbook_invalid_projection",
        )
    public = {
        key: result.get(key)
        for key in (
            "playbook_id",
            "status",
            "mode",
            "effective_autonomy_level",
            "operation_state",
            "attempt_count",
            "candidate_status",
            "evidence_status",
            "production_effect_status",
            "promotion_authorized",
            "automatic_effects",
            "forbidden_effects",
            "fault_code",
        )
        if key in result
    }
    spec = result.get("spec")
    if isinstance(spec, dict):
        public["spec"] = {
            key: spec.get(key)
            for key in (
                "playbook_id",
                "version",
                "domain",
                "maximum_level",
                "risk_floor",
                "max_attempts",
                "operation_timeout_seconds",
                "max_candidate_bytes",
                "input_contract",
                "verification",
                "success_condition",
                "promotion",
            )
        }
    profile = result.get("autonomy_profile")
    if isinstance(profile, dict):
        public["autonomy_profile"] = {
            key: profile.get(key)
            for key in (
                "profile_id",
                "policy_version",
                "level",
                "enabled",
                "revoked",
                "user_scope",
                "domain",
                "environment",
                "capabilities",
                "target_scope",
                "risk_ceiling",
                "max_attempts",
                "cooldown_seconds",
                "allowed_modes",
                "valid_from",
                "valid_until",
                "global_authority",
            )
        }
    return public


def _phase5_authority() -> dict[str, Any]:
    return {
        "policy_effect": "none",
        "provider_auto_switch_allowed": False,
        "notification_allowed": False,
        "workspace_read_allowed": False,
        "workspace_mutation_allowed": False,
        "production_effect_allowed": False,
        "promotion_allowed": False,
        "capability_grant_allowed": False,
        "autonomy_raise_allowed": False,
    }


__all__ = [
    "PHASE5_FEEDBACK_COMMAND_SCHEMA",
    "PHASE5_PROVIDER_PROJECTION_SCHEMA",
    "PHASE5_SANDBOX_COMMAND_SCHEMA",
    "PHASE5_STATUS_SCHEMA",
    "Phase5FeedbackRequest",
    "Phase5SandboxCandidateRequest",
    "build_phase5_router",
]
