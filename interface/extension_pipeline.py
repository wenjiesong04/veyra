from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
)


EXTENSION_PIPELINE_STATE_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_state.v1"
)
EXTENSION_PIPELINE_RECORD_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_record.v1"
)
EXTENSION_PIPELINE_PUBLIC_RECORD_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_public_record.v1"
)
EXTENSION_PIPELINE_STATUS_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_status.v1"
)
EXTENSION_PIPELINE_LIST_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_list.v1"
)
EXTENSION_PIPELINE_START_COMMAND_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_start_command.v1"
)
EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION = (
    "veyra.phase6.extension_pipeline_advance_command.v1"
)

MAX_EXTENSION_PIPELINES = 250
MAX_EXTENSION_PIPELINE_OPERATIONS = 1_000
MAX_EXTENSION_PIPELINE_INPUT_BYTES = 64 * 1024


class ExtensionPipelineAuthority(BaseModel):
    """The coordinator composes gates but grants no new authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    natural_language_trigger: Literal[False] = False
    background_trigger: Literal[False] = False
    automatic_approval: Literal[False] = False
    automatic_scoped_canary: Literal[False] = False
    automatic_promotion: Literal[False] = False
    permission_escalation: Literal[False] = False
    policy_mutation: Literal[False] = False
    key_management: Literal[False] = False
    source_export: Literal[False] = False
    workspace_access: Literal[False] = False
    host_execution: Literal[False] = False
    network_access_outside_gates: Literal[False] = False
    agent_dispatch: Literal[False] = False
    raw_input_persistence: Literal[False] = False
    raw_output_persistence: Literal[False] = False


class ExtensionPipelineStartCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        EXTENSION_PIPELINE_START_COMMAND_SCHEMA_VERSION
    ]
    operation_id: str = Field(min_length=1, max_length=240)
    request_id: str = Field(min_length=1, max_length=240)
    candidate_id: str = Field(min_length=32, max_length=32)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=1024)
    session_id: str = Field(min_length=1, max_length=240)
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_candidate_revision: StrictInt = Field(
        ge=1, le=2_147_483_647
    )
    expected_spec_digest: str = Field(min_length=64, max_length=64)
    test_bundle: dict[str, Any] = Field(max_length=64)
    canary_input: dict[str, Any] = Field(max_length=64)
    release_expires_at: str = Field(min_length=20, max_length=32)
    deployment_expires_at: str = Field(min_length=20, max_length=32)
    shadow_max_invocations: StrictInt = Field(ge=1, le=100)
    read_only_canary_max_invocations: StrictInt = Field(ge=1, le=100)
    scoped_canary_max_invocations: StrictInt = Field(ge=1, le=100)
    promoted_max_invocations: StrictInt = Field(ge=1, le=100)

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not value.startswith("extspec_") or len(value) != 32:
            raise ValueError("candidate_id is invalid")
        return value


class ExtensionPipelineAdvanceCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION
    ]
    operation_id: str = Field(min_length=1, max_length=240)
    expected_pipeline_revision: StrictInt = Field(
        ge=1, le=2_147_483_647
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=1024)
    session_id: str = Field(min_length=1, max_length=240)
    test_bundle: dict[str, Any] = Field(max_length=64)
    canary_input: dict[str, Any] = Field(max_length=64)
    review_id: str | None = Field(default=None, min_length=1, max_length=240)


__all__ = [
    "EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_LIST_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_PUBLIC_RECORD_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_RECORD_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_START_COMMAND_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_STATE_SCHEMA_VERSION",
    "EXTENSION_PIPELINE_STATUS_SCHEMA_VERSION",
    "ExtensionPipelineAdvanceCommand",
    "ExtensionPipelineAuthority",
    "ExtensionPipelineStartCommand",
    "MAX_EXTENSION_PIPELINES",
    "MAX_EXTENSION_PIPELINE_INPUT_BYTES",
    "MAX_EXTENSION_PIPELINE_OPERATIONS",
]
