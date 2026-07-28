from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from tool_proxy.governance_contract import (
    ToolInvocation,
    canonical_json,
    canonical_sha256,
)


CAPABILITY_EFFECT_CONTRACT_SCHEMA = "veyra.capability_effect_contract.v1"
EFFECT_GRAPH_SCHEMA = "veyra.effect_dependency_graph.v1"
FORESIGHT_ASSESSMENT_SCHEMA = "veyra.foresight_assessment.v1"
PREDICTION_RESIDUAL_SCHEMA = "veyra.prediction_residual.v1"
FORESIGHT_RULESET_REVISION = "veyra.foresight.exact_effects.v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
_MAX_JSON_BYTES = 256 * 1024

RiskLevelValue = Literal["R0", "R1", "R2", "R3", "R4", "R5"]
EffectKind = Literal[
    "filesystem_read",
    "filesystem_write",
    "bounded_process_probe",
    "agent_status_observation",
    "agent_capability_refresh",
    "agent_transport_reconnect",
]
RollbackMode = Literal["none", "disposable_sandbox", "restore", "compensate"]
TrialMode = Literal["preview_only", "shadow_observation", "sandbox_trial"]
ResidualStatus = Literal["exact", "mismatch", "indeterminate"]


class ForesightContractError(ValueError):
    """Raised when exact Foresight input cannot be safely normalized."""


class UnknownCapabilityEffectContract(ForesightContractError):
    """Raised instead of guessing effects for an unregistered capability."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_assignment=True,
    )


def _without(payload: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    excluded = set(fields)
    return {key: value for key, value in payload.items() if key not in excluded}


def _digest(value: str, *, field_name: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str, *, field_name: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > 600
    ):
        raise ValueError(f"{field_name} must be a bounded normalized identifier")
    return value


def _json_map(value: dict[str, JsonValue], *, field_name: str) -> dict[str, JsonValue]:
    if len(canonical_json(value).encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds {_MAX_JSON_BYTES} bytes")
    return value


class CapabilityEffectContract(_StrictFrozenModel):
    """Static Veyra-owned declaration of one fixed capability's effects."""

    schema_version: Literal["veyra.capability_effect_contract.v1"] = (
        CAPABILITY_EFFECT_CONTRACT_SCHEMA
    )
    capability_id: str = Field(min_length=1, max_length=240)
    accepted_tool_names: tuple[str, ...] = Field(min_length=1, max_length=8)
    canonical_invocation_tool_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
    )
    invocation_tool_kind: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    executor_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
    )
    required_environment: dict[str, JsonValue] = Field(default_factory=dict)
    revision: str = Field(min_length=1, max_length=240)
    effect_kind: EffectKind
    risk_floor: RiskLevelValue
    target_strategy: Literal[
        "exact_derived_targets",
        "no_resource_target",
        "selected_local_agent_endpoint",
    ]
    dependency_kinds: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    verifier_id: str = Field(min_length=1, max_length=240)
    evidence_kind: Literal[
        "authoritative_tool_receipt_and_effect",
        "fresh_local_dual_probe",
    ]
    rollback_mode: RollbackMode
    trial_mode: TrialMode
    network_scope: Literal["none", "local_only"]
    external_account_access: Literal[False] = False
    changed_files_policy: Literal["none", "exact_authorized_targets"]
    promotion_supported: bool
    contract_digest: str

    @field_validator(
        "capability_id",
        "revision",
        "verifier_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _identifier(value, field_name=info.field_name)

    @field_validator("accepted_tool_names", "dependency_kinds")
    @classmethod
    def validate_identifier_lists(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, field_name=info.field_name) for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return normalized

    @field_validator(
        "canonical_invocation_tool_name",
        "invocation_tool_kind",
        "executor_id",
    )
    @classmethod
    def validate_optional_identifiers(
        cls,
        value: str | None,
        info: Any,
    ) -> str | None:
        if value is None:
            return None
        return _identifier(value, field_name=info.field_name)

    @field_validator("required_environment")
    @classmethod
    def validate_required_environment(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        for key in value:
            _identifier(key, field_name="required_environment key")
        return _json_map(value, field_name="required_environment")

    @field_validator("contract_digest")
    @classmethod
    def validate_contract_digest(cls, value: str) -> str:
        return _digest(value, field_name="contract_digest")

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "contract_digest")
        )
        if self.contract_digest != expected:
            raise ValueError("contract_digest does not match effect contract")
        if self.target_strategy == "exact_derived_targets" and self.effect_kind not in {
            "filesystem_read",
            "filesystem_write",
        }:
            raise ValueError("exact target strategy is reserved for filesystem effects")
        if (
            self.changed_files_policy == "exact_authorized_targets"
            and self.effect_kind != "filesystem_write"
        ):
            raise ValueError("only filesystem_write may predict changed files")
        if (
            self.evidence_kind == "authoritative_tool_receipt_and_effect"
            and self.trial_mode != "sandbox_trial"
        ):
            raise ValueError("tool effect evidence requires a sandbox trial")
        tool_bound = self.evidence_kind == "authoritative_tool_receipt_and_effect"
        invocation_identity = (
            self.canonical_invocation_tool_name,
            self.invocation_tool_kind,
            self.executor_id,
        )
        if tool_bound and any(item is None for item in invocation_identity):
            raise ValueError(
                "tool effect contracts require canonical tool, kind, and executor"
            )
        if not tool_bound and any(item is not None for item in invocation_identity):
            raise ValueError(
                "non-tool effect contracts cannot declare tool invocation identity"
            )
        if (
            self.canonical_invocation_tool_name is not None
            and self.canonical_invocation_tool_name
            not in self.accepted_tool_names
        ):
            raise ValueError(
                "canonical invocation tool must be one of the accepted names"
            )
        if (
            self.executor_id is not None
            and self.required_environment.get("executor") != self.executor_id
        ):
            raise ValueError(
                "required environment must bind the declared executor"
            )
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {
            "schema_version": CAPABILITY_EFFECT_CONTRACT_SCHEMA,
            **values,
        }
        payload.setdefault("canonical_invocation_tool_name", None)
        payload.setdefault("invocation_tool_kind", None)
        payload.setdefault("executor_id", None)
        payload.setdefault("required_environment", {})
        return cls.model_validate(
            {
                **payload,
                "contract_digest": canonical_sha256(payload),
            },
            strict=True,
        )


class EffectGraphNode(_StrictFrozenModel):
    node_id: str = Field(min_length=1, max_length=96)
    kind: Literal[
        "capability",
        "invocation",
        "target",
        "scope",
        "executor",
        "dependency",
        "verifier",
    ]
    ref: str = Field(min_length=1, max_length=1200)
    ref_digest: str
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("node_id", "ref")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _identifier(value, field_name=info.field_name)

    @field_validator("ref_digest")
    @classmethod
    def validate_ref_digest(cls, value: str) -> str:
        return _digest(value, field_name="ref_digest")

    @field_validator("attributes")
    @classmethod
    def validate_attributes(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return _json_map(value, field_name="attributes")

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.ref_digest != canonical_sha256(self.ref):
            raise ValueError("ref_digest does not match graph node ref")
        return self


class EffectGraphEdge(_StrictFrozenModel):
    source: str = Field(min_length=1, max_length=96)
    relation: Literal[
        "affects",
        "reads",
        "writes",
        "runs_in",
        "invokes",
        "depends_on",
        "verified_by",
        "contained_by",
        "bound_to",
    ]
    target: str = Field(min_length=1, max_length=96)

    @field_validator("source", "target")
    @classmethod
    def validate_node_refs(cls, value: str, info: Any) -> str:
        return _identifier(value, field_name=info.field_name)


class EffectDependencyGraph(_StrictFrozenModel):
    schema_version: Literal["veyra.effect_dependency_graph.v1"] = (
        EFFECT_GRAPH_SCHEMA
    )
    nodes: tuple[EffectGraphNode, ...] = Field(min_length=1, max_length=64)
    edges: tuple[EffectGraphEdge, ...] = Field(default_factory=tuple, max_length=128)
    exact_target_count: int = Field(ge=0, le=256)
    dependency_discovery: Literal["contract_declared_only"] = (
        "contract_declared_only"
    )
    downstream_dependencies_unknown: bool
    graph_digest: str

    @field_validator("graph_digest")
    @classmethod
    def validate_graph_digest(cls, value: str) -> str:
        return _digest(value, field_name="graph_digest")

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("effect graph node ids must be unique")
        known = set(node_ids)
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("effect graph edge points to an unknown node")
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "graph_digest")
        )
        if self.graph_digest != expected:
            raise ValueError("graph_digest does not match effect graph")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {"schema_version": EFFECT_GRAPH_SCHEMA, **values}
        return cls.model_validate(
            {**payload, "graph_digest": canonical_sha256(payload)},
            strict=True,
        )


class PredictedEffect(_StrictFrozenModel):
    effect_kind: EffectKind
    authorized_targets: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    expected_changed_files: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=256,
    )
    changed_files_policy: Literal["none", "exact_authorized_targets"]
    external_effects_allowed: Literal[False] = False

    @field_validator("authorized_targets", "expected_changed_files")
    @classmethod
    def validate_paths(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, field_name=info.field_name) for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.changed_files_policy == "none" and self.expected_changed_files:
            raise ValueError("changed-files-none prediction cannot contain paths")
        if (
            self.changed_files_policy == "exact_authorized_targets"
            and self.expected_changed_files != self.authorized_targets
        ):
            raise ValueError("write prediction must bind every exact authorized target")
        return self


class ForesightAssessment(_StrictFrozenModel):
    """Deterministic prediction bound to one exact invocation."""

    schema_version: Literal["veyra.foresight_assessment.v1"] = (
        FORESIGHT_ASSESSMENT_SCHEMA
    )
    assessment_id: str = Field(min_length=1, max_length=96)
    ruleset_revision: Literal["veyra.foresight.exact_effects.v1"] = (
        FORESIGHT_RULESET_REVISION
    )
    capability_id: str = Field(min_length=1, max_length=240)
    contract_revision: str = Field(min_length=1, max_length=240)
    contract_digest: str
    invocation_digest: str
    run_id: str = Field(min_length=1, max_length=240)
    tool_call_id: str = Field(min_length=1, max_length=240)
    binding_digest: str
    args_digest: str
    targets_digest: str
    environment_digest: str
    scope_digest: str
    executor_id: str = Field(min_length=1, max_length=240)
    risk_floor: RiskLevelValue
    predicted_effect: PredictedEffect
    effect_graph: EffectDependencyGraph
    preconditions: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    invariants: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    stop_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    verification_plan: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    rollback_mode: RollbackMode
    trial_mode: TrialMode
    evidence_kind: Literal[
        "authoritative_tool_receipt_and_effect",
        "fresh_local_dual_probe",
    ]
    prediction_source: Literal["deterministic_capability_contract"] = (
        "deterministic_capability_contract"
    )
    created_at: AwareDatetime
    valid_until: AwareDatetime
    assessment_digest: str

    @field_validator(
        "assessment_id",
        "capability_id",
        "contract_revision",
        "run_id",
        "tool_call_id",
        "executor_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _identifier(value, field_name=info.field_name)

    @field_validator(
        "contract_digest",
        "invocation_digest",
        "binding_digest",
        "args_digest",
        "targets_digest",
        "environment_digest",
        "scope_digest",
        "assessment_digest",
    )
    @classmethod
    def validate_digests(cls, value: str, info: Any) -> str:
        return _digest(value, field_name=info.field_name)

    @field_validator(
        "preconditions",
        "invariants",
        "stop_conditions",
        "verification_plan",
    )
    @classmethod
    def validate_text_lists(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, field_name=info.field_name) for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be later than created_at")
        if self.effect_graph.exact_target_count != len(
            self.predicted_effect.authorized_targets
        ):
            raise ValueError("effect graph target count does not match prediction")
        expected = canonical_sha256(
            _without(
                self.model_dump(mode="python"),
                "assessment_id",
                "assessment_digest",
            )
        )
        if self.assessment_digest != expected:
            raise ValueError("assessment_digest does not match assessment")
        expected_id = f"far_{self.assessment_digest[:20]}"
        if self.assessment_id != expected_id:
            raise ValueError("assessment_id does not match assessment digest")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {
            "schema_version": FORESIGHT_ASSESSMENT_SCHEMA,
            "ruleset_revision": FORESIGHT_RULESET_REVISION,
            **values,
        }
        final_digest = canonical_sha256(
            _without(payload, "assessment_id", "assessment_digest")
        )
        payload["assessment_id"] = f"far_{final_digest[:20]}"
        return cls.model_validate(
            {**payload, "assessment_digest": final_digest},
            strict=True,
        )


class PredictionResidual(_StrictFrozenModel):
    schema_version: Literal["veyra.prediction_residual.v1"] = (
        PREDICTION_RESIDUAL_SCHEMA
    )
    residual_id: str = Field(min_length=1, max_length=96)
    assessment_id: str = Field(min_length=1, max_length=96)
    assessment_digest: str
    capability_id: str = Field(min_length=1, max_length=240)
    contract_digest: str
    invocation_digest: str
    run_id: str = Field(min_length=1, max_length=240)
    tool_call_id: str = Field(min_length=1, max_length=240)
    status: ResidualStatus
    reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    predicted_changed_files: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    observed_changed_files: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    missing_changed_files: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    unexpected_changed_files: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    receipt_id: str | None = Field(default=None, min_length=1, max_length=240)
    receipt_result_digest: str | None = None
    effect_evidence_digest: str | None = None
    observed_at: AwareDatetime
    residual_digest: str

    @field_validator(
        "residual_id",
        "assessment_id",
        "capability_id",
        "run_id",
        "tool_call_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _identifier(value, field_name=info.field_name)

    @field_validator(
        "assessment_digest",
        "contract_digest",
        "invocation_digest",
        "residual_digest",
    )
    @classmethod
    def validate_required_digests(cls, value: str, info: Any) -> str:
        return _digest(value, field_name=info.field_name)

    @field_validator(
        "receipt_result_digest",
        "effect_evidence_digest",
    )
    @classmethod
    def validate_optional_digests(
        cls,
        value: str | None,
        info: Any,
    ) -> str | None:
        if value is None:
            return None
        return _digest(value, field_name=info.field_name)

    @field_validator(
        "predicted_changed_files",
        "observed_changed_files",
        "missing_changed_files",
        "unexpected_changed_files",
    )
    @classmethod
    def validate_path_lists(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, field_name=info.field_name) for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return normalized

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_identifier(item, field_name="reasons") for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("reasons cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        expected = canonical_sha256(
            _without(
                self.model_dump(mode="python"),
                "residual_id",
                "residual_digest",
            )
        )
        if self.residual_digest != expected:
            raise ValueError("residual_digest does not match prediction residual")
        expected_id = f"pres_{self.residual_digest[:20]}"
        if self.residual_id != expected_id:
            raise ValueError("residual_id does not match residual digest")
        predicted = set(self.predicted_changed_files)
        observed = set(self.observed_changed_files)
        if set(self.missing_changed_files) != predicted - observed:
            raise ValueError("missing_changed_files does not match set difference")
        if set(self.unexpected_changed_files) != observed - predicted:
            raise ValueError("unexpected_changed_files does not match set difference")
        if self.status == "exact" and (
            self.missing_changed_files or self.unexpected_changed_files
        ):
            raise ValueError("exact residual cannot contain changed-file mismatch")
        exact_reason = "predicted_and_authoritative_effects_match"
        if self.status == "exact" and (
            self.reasons != (exact_reason,)
            or self.predicted_changed_files != self.observed_changed_files
            or self.receipt_id is None
            or self.receipt_result_digest is None
            or self.effect_evidence_digest is None
        ):
            raise ValueError(
                "exact residual requires one exact reason and bound evidence"
            )
        if self.status != "exact" and exact_reason in self.reasons:
            raise ValueError("non-exact residual cannot claim an exact match")
        if self.status == "mismatch" and (
            self.receipt_id is None
            or self.receipt_result_digest is None
            or self.effect_evidence_digest is None
        ):
            raise ValueError("mismatch residual requires bound observed evidence")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {
            "schema_version": PREDICTION_RESIDUAL_SCHEMA,
            **values,
        }
        digest_payload = _without(payload, "residual_id")
        residual_digest = canonical_sha256(digest_payload)
        payload["residual_id"] = f"pres_{residual_digest[:20]}"
        return cls.model_validate(
            {**payload, "residual_digest": residual_digest},
            strict=True,
        )


def _contract(**values: Any) -> CapabilityEffectContract:
    return CapabilityEffectContract.create(
        revision="veyra.fixed_capability_effects.v1",
        external_account_access=False,
        **values,
    )


_SCOPED_TOOL_EXECUTOR = "veyra.openclaw.scoped_executor.v1"
_SCOPED_TOOL_ENVIRONMENT: dict[str, JsonValue] = {
    "executor": _SCOPED_TOOL_EXECUTOR,
    "policy_revision": "veyra.phase3.scoped_sandbox.v1",
    "registry_revision": "veyra.openclaw.tool_registry.v1",
}


_CONTRACTS: tuple[CapabilityEffectContract, ...] = (
    _contract(
        capability_id="agent.status.read",
        accepted_tool_names=("agent.status.read",),
        effect_kind="agent_status_observation",
        risk_floor="R1",
        target_strategy="selected_local_agent_endpoint",
        dependency_kinds=("selected_agent_config", "local_gateway_transport"),
        verifier_id="fresh_agent_status",
        evidence_kind="fresh_local_dual_probe",
        rollback_mode="none",
        trial_mode="shadow_observation",
        network_scope="local_only",
        changed_files_policy="none",
        promotion_supported=False,
    ),
    _contract(
        capability_id="agent.capabilities.refresh",
        accepted_tool_names=("agent.capabilities.refresh",),
        effect_kind="agent_capability_refresh",
        risk_floor="R1",
        target_strategy="selected_local_agent_endpoint",
        dependency_kinds=(
            "selected_agent_config",
            "local_gateway_transport",
            "adapter_contract",
        ),
        verifier_id="fresh_compatible_capability_snapshot",
        evidence_kind="fresh_local_dual_probe",
        rollback_mode="none",
        trial_mode="shadow_observation",
        network_scope="local_only",
        changed_files_policy="none",
        promotion_supported=False,
    ),
    _contract(
        capability_id="agent.reconnect",
        accepted_tool_names=("agent.reconnect",),
        effect_kind="agent_transport_reconnect",
        risk_floor="R1",
        target_strategy="selected_local_agent_endpoint",
        dependency_kinds=(
            "selected_agent_config",
            "local_gateway_transport",
            "runtime_identity",
        ),
        verifier_id="fresh_tcp_and_compatible_gateway_snapshot",
        evidence_kind="fresh_local_dual_probe",
        rollback_mode="none",
        trial_mode="shadow_observation",
        network_scope="local_only",
        changed_files_policy="none",
        promotion_supported=True,
    ),
    _contract(
        capability_id="file.read",
        accepted_tool_names=("file.read", "veyra_file_read"),
        canonical_invocation_tool_name="file.read",
        invocation_tool_kind="file",
        executor_id=_SCOPED_TOOL_EXECUTOR,
        required_environment=_SCOPED_TOOL_ENVIRONMENT,
        effect_kind="filesystem_read",
        risk_floor="R1",
        target_strategy="exact_derived_targets",
        dependency_kinds=("sandbox_scope", "filesystem_target"),
        verifier_id="veyra_authoritative_tool_effect",
        evidence_kind="authoritative_tool_receipt_and_effect",
        rollback_mode="none",
        trial_mode="sandbox_trial",
        network_scope="none",
        changed_files_policy="none",
        promotion_supported=True,
    ),
    _contract(
        capability_id="file.write",
        accepted_tool_names=("file.write", "veyra_file_write"),
        canonical_invocation_tool_name="file.write",
        invocation_tool_kind="file",
        executor_id=_SCOPED_TOOL_EXECUTOR,
        required_environment=_SCOPED_TOOL_ENVIRONMENT,
        effect_kind="filesystem_write",
        risk_floor="R2",
        target_strategy="exact_derived_targets",
        dependency_kinds=(
            "sandbox_scope",
            "filesystem_target",
            "veyra_authoritative_effect_verifier",
        ),
        verifier_id="veyra_authoritative_tool_effect",
        evidence_kind="authoritative_tool_receipt_and_effect",
        rollback_mode="disposable_sandbox",
        trial_mode="sandbox_trial",
        network_scope="none",
        changed_files_policy="exact_authorized_targets",
        promotion_supported=True,
    ),
    _contract(
        capability_id="shell.run",
        accepted_tool_names=("shell.run", "veyra_shell_probe"),
        canonical_invocation_tool_name="shell.run",
        invocation_tool_kind="shell",
        executor_id=_SCOPED_TOOL_EXECUTOR,
        required_environment=_SCOPED_TOOL_ENVIRONMENT,
        effect_kind="bounded_process_probe",
        risk_floor="R0",
        target_strategy="no_resource_target",
        dependency_kinds=(
            "sandbox_scope",
            "pinned_executable_identity",
            "bounded_process_runtime",
        ),
        verifier_id="veyra_authoritative_tool_effect",
        evidence_kind="authoritative_tool_receipt_and_effect",
        rollback_mode="none",
        trial_mode="sandbox_trial",
        network_scope="none",
        changed_files_policy="none",
        promotion_supported=True,
    ),
)

_CONTRACT_BY_CAPABILITY = {item.capability_id: item for item in _CONTRACTS}
_CONTRACT_BY_TOOL = {
    tool_name: item
    for item in _CONTRACTS
    for tool_name in item.accepted_tool_names
}


def _contract_copy(
    contract: CapabilityEffectContract,
) -> CapabilityEffectContract:
    return CapabilityEffectContract.model_validate_json(
        canonical_json(contract.model_dump(mode="json")),
        strict=True,
    )


def capability_effect_contracts() -> tuple[CapabilityEffectContract, ...]:
    return tuple(_contract_copy(item) for item in _CONTRACTS)


def resolve_capability_effect_contract(
    capability_or_tool: str,
) -> CapabilityEffectContract:
    normalized = str(capability_or_tool or "").strip()
    contract = _CONTRACT_BY_CAPABILITY.get(normalized) or _CONTRACT_BY_TOOL.get(
        normalized
    )
    if contract is None:
        raise UnknownCapabilityEffectContract(
            f"unregistered capability effect contract: {normalized or 'missing'}"
        )
    return _contract_copy(contract)


def _graph_node(
    *,
    node_id: str,
    kind: str,
    ref: str,
    attributes: dict[str, JsonValue] | None = None,
) -> EffectGraphNode:
    return EffectGraphNode.model_validate(
        {
            "node_id": node_id,
            "kind": kind,
            "ref": ref,
            "ref_digest": canonical_sha256(ref),
            "attributes": attributes or {},
        },
        strict=True,
    )


def _effect_relation(effect_kind: EffectKind) -> str:
    if effect_kind == "filesystem_read":
        return "reads"
    if effect_kind == "filesystem_write":
        return "writes"
    return "affects"


def _validate_current_invocation_shape(
    invocation: ToolInvocation,
    *,
    capability_id: str,
    targets: tuple[str, ...],
) -> None:
    """Re-derive the fixed broker targets instead of trusting a target claim."""

    arguments = invocation.arguments
    if capability_id == "file.read":
        if (
            set(arguments) != {"path"}
            or not isinstance(arguments.get("path"), str)
            or len(targets) != 1
            or arguments["path"] != targets[0]
        ):
            raise ForesightContractError(
                "file.read requires one exact normalized path target"
            )
        return
    if capability_id == "file.write":
        if (
            set(arguments) != {"path", "content"}
            or not isinstance(arguments.get("path"), str)
            or not isinstance(arguments.get("content"), str)
            or len(targets) != 1
            or arguments["path"] != targets[0]
        ):
            raise ForesightContractError(
                "file.write requires one exact normalized path target"
            )
        return
    if capability_id == "shell.run":
        argv = arguments.get("argv")
        if (
            set(arguments) != {"argv"}
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
            or targets
        ):
            raise ForesightContractError(
                "shell.run requires one bounded argv vector and no target"
            )
        return
    raise UnknownCapabilityEffectContract(
        f"no exact invocation derivation for {capability_id}"
    )


def assess_tool_invocation(
    invocation: ToolInvocation,
    *,
    created_at: datetime | None = None,
    valid_for_seconds: int = 900,
) -> ForesightAssessment:
    """Create a deterministic assessment from a canonical ToolInvocation.

    The raw arguments are never copied into the assessment. Exact parameter
    identity is retained through the invocation and argument digests.
    """

    if not isinstance(invocation, ToolInvocation):
        raise ForesightContractError("canonical ToolInvocation is required")
    contract = resolve_capability_effect_contract(invocation.tool_name)
    if contract.evidence_kind != "authoritative_tool_receipt_and_effect":
        raise ForesightContractError(
            "tool invocation requires an authoritative tool-effect contract"
        )
    if invocation.tool_name != contract.canonical_invocation_tool_name:
        raise ForesightContractError(
            "canonical ToolInvocation tool name is required"
        )
    if invocation.tool_kind != contract.invocation_tool_kind:
        raise ForesightContractError(
            "ToolInvocation kind does not match the capability contract"
        )
    targets = tuple(invocation.derived_targets)
    if contract.target_strategy == "exact_derived_targets" and not targets:
        raise ForesightContractError(
            "filesystem effect requires at least one exact derived target"
        )
    if contract.target_strategy == "no_resource_target" and targets:
        raise ForesightContractError(
            "no-resource capability cannot carry derived targets"
        )
    _validate_current_invocation_shape(
        invocation,
        capability_id=contract.capability_id,
        targets=targets,
    )
    environment = invocation.environment
    expected_environment_keys = set(contract.required_environment) | {
        "scope_digest"
    }
    if set(environment) != expected_environment_keys:
        raise ForesightContractError(
            "ToolInvocation environment keys do not match the fixed contract"
        )
    scope_digest = str(environment.get("scope_digest") or "")
    executor_id = str(environment.get("executor") or "")
    _digest(scope_digest, field_name="scope_digest")
    _identifier(executor_id, field_name="executor_id")
    for key, expected in contract.required_environment.items():
        if environment.get(key) != expected:
            raise ForesightContractError(
                f"ToolInvocation environment does not match required {key}"
            )
    now = created_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ForesightContractError("created_at must be timezone-aware")
    if (
        isinstance(valid_for_seconds, bool)
        or not isinstance(valid_for_seconds, int)
        or not 1 <= valid_for_seconds <= 3600
    ):
        raise ForesightContractError(
            "valid_for_seconds must be between 1 and 3600"
        )

    capability_node = _graph_node(
        node_id="capability",
        kind="capability",
        ref=contract.capability_id,
        attributes={
            "effect_kind": contract.effect_kind,
            "risk_floor": contract.risk_floor,
            "contract_digest": contract.contract_digest,
        },
    )
    invocation_node = _graph_node(
        node_id="invocation",
        kind="invocation",
        ref=invocation.invocation_digest,
        attributes={
            "run_id_digest": canonical_sha256(invocation.binding.run_id),
            "tool_call_id_digest": canonical_sha256(invocation.tool_call_id),
            "binding_digest": invocation.binding.binding_digest,
            "args_digest": invocation.args_digest,
            "targets_digest": invocation.targets_digest,
            "environment_digest": invocation.environment_digest,
        },
    )
    scope_node = _graph_node(
        node_id="scope",
        kind="scope",
        ref=scope_digest,
        attributes={"trial_mode": contract.trial_mode},
    )
    executor_node = _graph_node(
        node_id="executor",
        kind="executor",
        ref=executor_id,
    )
    verifier_node = _graph_node(
        node_id="verifier",
        kind="verifier",
        ref=contract.verifier_id,
    )
    nodes: list[EffectGraphNode] = [
        capability_node,
        invocation_node,
        scope_node,
        executor_node,
        verifier_node,
    ]
    edges: list[EffectGraphEdge] = [
        EffectGraphEdge(
            source="capability",
            relation="bound_to",
            target="invocation",
        ),
        EffectGraphEdge(
            source="invocation",
            relation="runs_in",
            target="scope",
        ),
        EffectGraphEdge(
            source="invocation",
            relation="invokes",
            target="executor",
        ),
        EffectGraphEdge(
            source="capability",
            relation="verified_by",
            target="verifier",
        ),
    ]
    for index, target in enumerate(targets):
        node_id = f"target_{index}"
        nodes.append(
            _graph_node(
                node_id=node_id,
                kind="target",
                ref=target,
                attributes={"target_index": index},
            )
        )
        edges.extend(
            [
                EffectGraphEdge(
                    source="invocation",
                    relation=_effect_relation(contract.effect_kind),
                    target=node_id,
                ),
                EffectGraphEdge(
                    source=node_id,
                    relation="contained_by",
                    target="scope",
                ),
            ]
        )
    for index, dependency in enumerate(contract.dependency_kinds):
        node_id = f"dependency_{index}"
        nodes.append(
            _graph_node(
                node_id=node_id,
                kind="dependency",
                ref=dependency,
            )
        )
        edges.append(
            EffectGraphEdge(
                source="capability",
                relation="depends_on",
                target=node_id,
            )
        )

    graph = EffectDependencyGraph.create(
        nodes=tuple(nodes),
        edges=tuple(edges),
        exact_target_count=len(targets),
        dependency_discovery="contract_declared_only",
        downstream_dependencies_unknown=contract.effect_kind
        == "filesystem_write",
    )
    expected_changed_files = (
        targets if contract.changed_files_policy == "exact_authorized_targets" else ()
    )
    predicted_effect = PredictedEffect(
        effect_kind=contract.effect_kind,
        authorized_targets=targets,
        expected_changed_files=expected_changed_files,
        changed_files_policy=contract.changed_files_policy,
        external_effects_allowed=False,
    )
    assessment_values = {
        "capability_id": contract.capability_id,
        "contract_revision": contract.revision,
        "contract_digest": contract.contract_digest,
        "invocation_digest": invocation.invocation_digest,
        "run_id": invocation.binding.run_id,
        "tool_call_id": invocation.tool_call_id,
        "binding_digest": invocation.binding.binding_digest,
        "args_digest": invocation.args_digest,
        "targets_digest": invocation.targets_digest,
        "environment_digest": invocation.environment_digest,
        "scope_digest": scope_digest,
        "executor_id": executor_id,
        "risk_floor": contract.risk_floor,
        "predicted_effect": predicted_effect,
        "effect_graph": graph,
        "preconditions": (
            "exact invocation digest remains unchanged",
            "capability contract revision remains unchanged",
            "sandbox scope identity remains unchanged",
            "authoritative preflight reserves the exact invocation",
        ),
        "invariants": (
            "no changed file may fall outside exact authorized targets",
            "real workspace and external effects remain forbidden",
            "caller and Agent reports cannot establish observed effects",
        ),
        "stop_conditions": (
            "scope or invocation identity changes",
            "contract or executor identity changes",
            "authoritative receipt or verifier evidence is missing",
            "execution outcome is indeterminate",
        ),
        "verification_plan": (
            "resolve receipt from Veyra authoritative ledger",
            "validate receipt against exact invocation and run",
            "validate Veyra-owned effect evidence against receipt",
            "compare predicted and observed exact changed-file sets",
        ),
        "rollback_mode": contract.rollback_mode,
        "trial_mode": contract.trial_mode,
        "evidence_kind": contract.evidence_kind,
        "prediction_source": "deterministic_capability_contract",
        "created_at": now,
        "valid_until": now + timedelta(seconds=valid_for_seconds),
    }
    return ForesightAssessment.create(**assessment_values)


def risk_at_least(value: str, floor: str) -> bool:
    return _RISK_ORDER.get(str(value), -1) >= _RISK_ORDER.get(str(floor), 99)


__all__ = [
    "CAPABILITY_EFFECT_CONTRACT_SCHEMA",
    "EFFECT_GRAPH_SCHEMA",
    "FORESIGHT_ASSESSMENT_SCHEMA",
    "PREDICTION_RESIDUAL_SCHEMA",
    "FORESIGHT_RULESET_REVISION",
    "CapabilityEffectContract",
    "EffectDependencyGraph",
    "EffectGraphEdge",
    "EffectGraphNode",
    "ForesightAssessment",
    "ForesightContractError",
    "PredictionResidual",
    "PredictedEffect",
    "UnknownCapabilityEffectContract",
    "assess_tool_invocation",
    "capability_effect_contracts",
    "resolve_capability_effect_contract",
    "risk_at_least",
]
