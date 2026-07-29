#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from routers.debug_audit import _public_state  # noqa: E402
from routers.phase6_extension_artifacts import (  # noqa: E402
    build_phase6_extension_artifacts_router,
)
from routers.phase6_extension_source_checks import (  # noqa: E402
    build_phase6_extension_source_checks_router,
)
from routers.phase6_extensions import (  # noqa: E402
    build_phase6_extensions_router,
)
from runtime.extension_artifact_quarantine import (  # noqa: E402
    ExtensionArtifactQuarantine,
)
from runtime.extension_source_policy_gate import (  # noqa: E402
    STATE_FILE as SOURCE_CHECK_STATE_FILE,
    ExtensionSourcePolicyGate,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_source_gate_lifecycle_smoke import (  # noqa: E402
    USER,
    VALID_SOURCE,
    WORKSPACE,
    SourceGateContext,
    assert_zero_dynamic_authority,
    prepare_context,
)


COMMAND_SCHEMA = "veyra.phase6.extension_source_check_command.v1"
FORBIDDEN_ACTIONS = (
    "generate",
    "unit",
    "contract",
    "security",
    "fuzz",
    "test",
    "execute",
    "sign",
    "install",
    "activate",
    "promote",
)
SOURCE_CHECK_PATHS = {
    "/phase6/extensions/source-checks/status",
    "/phase6/extensions/source-checks",
    "/phase6/extensions/source-checks/{check_id}",
    "/phase6/extensions/source-checks/{check_id}/integrity",
    "/phase6/extensions/artifacts/{artifact_id}/source-check",
}
DYNAMIC_STATUS_FIELDS = (
    "isolated_generation_status",
    "unit_checks_status",
    "contract_checks_status",
    "security_runtime_checks_status",
    "fuzz_checks_status",
    "test_execution_status",
    "behavior_verification_status",
    "execution_status",
)


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def build_control_plane(
    gate: ExtensionSourcePolicyGate,
) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    artifact_quarantine = gate.artifact_quarantine
    app.include_router(
        build_phase6_extensions_router(
            quarantine=artifact_quarantine.spec_quarantine,
            artifact_quarantine=artifact_quarantine,
            source_check_gate=gate,
        )
    )
    app.include_router(
        build_phase6_extension_artifacts_router(
            quarantine=artifact_quarantine,
            source_check_gate=gate,
        )
    )
    app.include_router(
        build_phase6_extension_source_checks_router(gate=gate)
    )
    return app, TestClient(app)


def command(
    context: SourceGateContext,
    operation_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": COMMAND_SCHEMA,
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_artifact_revision": context.artifact[
            "artifact_revision"
        ],
        "expected_artifact_sha256": context.artifact[
            "artifact_sha256"
        ],
    }
    payload.update(overrides)
    return payload


def start_path(context: SourceGateContext) -> str:
    return (
        "/phase6/extensions/artifacts/"
        f"{context.artifact['artifact_id']}/source-check"
    )


def owner_params() -> dict[str, str]:
    return {
        "user_id": USER,
        "workspace_id": WORKSPACE,
    }


def assert_source_projection(
    value: dict[str, Any],
    *,
    expected_status: str,
    expected_effective_status: str,
    expected_health: str,
    label: str,
) -> None:
    projection = value.get("source_check_projection")
    expect(
        isinstance(projection, dict)
        and value.get("source_check_status") == expected_status
        and value.get("source_check_effective_status")
        == expected_effective_status
        and value.get("source_check_operational_health")
        == expected_health
        and projection.get("source_check_status") == expected_status
        and projection.get("source_check_effective_status")
        == expected_effective_status
        and projection.get("operational_health") == expected_health
        and all(
            value.get(field) == "not_started"
            and projection.get(field) == "not_started"
            for field in DYNAMIC_STATUS_FIELDS
        )
        and value.get("signature_status") == "not_implemented"
        and projection.get("signature_status") == "not_implemented"
        and value.get("activation_status") == "not_installed"
        and projection.get("activation_status") == "not_installed"
        and value.get("capability_registry_visible") is False
        and projection.get("capability_registry_visible") is False
        and value.get("promotion_authorized") is False
        and projection.get("promotion_authorized") is False
        and value.get("policy_effect") == "none"
        and isinstance(value.get("authority"), dict)
        and not any(value["authority"].values()),
        label,
        value,
    )


def assert_status_projects_source_gate(
    value: dict[str, Any],
    *,
    expected_status: str,
    expected_health: str,
    expected_check_count: int | None,
    label: str,
) -> None:
    source_gate = value.get("source_check_gate")
    storage = (
        source_gate.get("storage")
        if isinstance(source_gate, dict)
        else None
    )
    next_stage = (
        source_gate.get("next_stage")
        if isinstance(source_gate, dict)
        else None
    )
    expect(
        isinstance(source_gate, dict)
        and source_gate.get("phase") == "6.2c"
        and source_gate.get("status") == expected_status
        and source_gate.get("operational_health") == expected_health
        and isinstance(storage, dict)
        and (
            expected_check_count is None
            or storage.get("check_count") == expected_check_count
        )
        and isinstance(next_stage, dict)
        and next_stage
        and all(value == "not_started" for value in next_stage.values())
        and isinstance(source_gate.get("authority"), dict)
        and not any(source_gate["authority"].values()),
        label,
        value,
    )


def assert_private_public_record(
    value: dict[str, Any],
    *,
    source: bytes,
) -> None:
    assert_zero_dynamic_authority(
        value,
        "source-check HTTP projection grants no dynamic authority",
    )
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
    )
    private_fields = {
        "owner_scope_digest",
        "binding",
        "report",
        "source",
        "source_bytes",
        "user_id",
        "workspace_id",
        "path",
        "command",
        "environment",
    }
    expect(
        not private_fields.intersection(value)
        and source.decode("utf-8") not in serialized
        and USER not in serialized
        and WORKSPACE not in serialized,
        "source-check HTTP projection omits source and private owner data",
        value,
    )


def strict_schema_and_success() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-check-control-"
    ) as raw:
        context = prepare_context(
            Path(raw),
            extension_id="example.source_check_control",
            operation_prefix="source-check-control",
        )
        app, client = build_control_plane(context.source_gate)
        path = start_path(context)

        status = client.get(
            "/phase6/extensions/source-checks/status"
        )
        empty = client.get(
            "/phase6/extensions/source-checks",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            status.status_code == 200
            and status.json()["phase"] == "6.2c"
            and status.json()["status"]
            == "technical_complete_non_executing_source_gate_only"
            and status.json()["storage"]["check_count"] == 0
            and not any(status.json()["authority"].values())
            and empty.status_code == 200
            and empty.json()["count"] == 0,
            "status and empty list expose only the source-gate boundary",
            {
                "status": status.json(),
                "list": empty.json(),
            },
        )

        invalid_requests = {
            "extra": {
                **command(context, "source-check-invalid-extra"),
                "execute": True,
            },
            "coerced_revision": command(
                context,
                "source-check-invalid-revision",
                expected_artifact_revision="1",
            ),
            "bad_digest": command(
                context,
                "source-check-invalid-digest",
                expected_artifact_sha256="not-a-digest",
            ),
            "private_source": {
                **command(context, "source-check-invalid-source"),
                "source": "PRIVATE_SOURCE_SENTINEL",
            },
            "private_owner": command(
                context,
                "source-check-invalid-owner",
                user_id="PRIVATE_OWNER_SENTINEL\x00",
            ),
        }
        invalid_responses = {
            name: client.post(path, json=payload)
            for name, payload in invalid_requests.items()
        }
        invalid_path = client.post(
            (
                "/phase6/extensions/artifacts/not-an-artifact/"
                "source-check"
            ),
            json=command(context, "source-check-invalid-path"),
        )
        expect(
            all(
                response.status_code == 422
                for response in invalid_responses.values()
            )
            and invalid_path.status_code == 422,
            "strict command and path schemas reject extras and coercion",
            {
                name: response.json()
                for name, response in invalid_responses.items()
            },
        )
        validation_text = "".join(
            response.text for response in invalid_responses.values()
        ) + invalid_path.text
        expect(
            "PRIVATE_SOURCE_SENTINEL" not in validation_text
            and "PRIVATE_OWNER_SENTINEL" not in validation_text
            and USER not in validation_text
            and WORKSPACE not in validation_text,
            "private validation errors never echo source or owner input",
            validation_text,
        )

        submitted = client.post(
            path,
            json=command(context, "source-check-control-start"),
        )
        expect(
            submitted.status_code == 200
            and submitted.json()["source_check_status"] == "passed"
            and submitted.json()["source_syntax_status"] == "passed"
            and submitted.json()["static_checks_status"] == "passed"
            and submitted.json()["static_security_policy_status"]
            == "passed",
            "HTTP start validates the exact inert source artifact",
            submitted.json(),
        )
        record = submitted.json()
        assert_private_public_record(record, source=context.source)
        check_id = record["check_id"]

        artifact_detail = client.get(
            (
                "/phase6/extensions/artifacts/"
                f"{context.artifact['artifact_id']}"
            ),
            params=owner_params(),
        )
        artifact_list = client.get(
            "/phase6/extensions/artifacts",
            params=owner_params(),
        )
        artifact_status = client.get(
            "/phase6/extensions/artifacts/status"
        )
        spec_detail = client.get(
            (
                "/phase6/extensions/specs/"
                f"{context.candidate['candidate_id']}"
            ),
            params=owner_params(),
        )
        spec_list = client.get(
            "/phase6/extensions/specs",
            params=owner_params(),
        )
        spec_status = client.get("/phase6/extensions/status")
        expect(
            artifact_detail.status_code == 200
            and artifact_list.status_code == 200
            and artifact_list.json()["count"] == 1
            and artifact_status.status_code == 200
            and spec_detail.status_code == 200
            and spec_list.status_code == 200
            and spec_list.json()["count"] == 1
            and spec_status.status_code == 200,
            "Spec and Artifact control planes remain available after source check",
            {
                "artifact_detail": artifact_detail.json(),
                "artifact_list": artifact_list.json(),
                "artifact_status": artifact_status.json(),
                "spec_detail": spec_detail.json(),
                "spec_list": spec_list.json(),
                "spec_status": spec_status.json(),
            },
        )
        projected_records = {
            "Artifact detail": artifact_detail.json(),
            "Artifact list": artifact_list.json()["artifacts"][0],
            "Spec detail": spec_detail.json(),
            "Spec list": spec_list.json()["candidates"][0],
        }
        for projection_label, projected in projected_records.items():
            assert_source_projection(
                projected,
                expected_status="passed",
                expected_effective_status="SOURCE_CHECK_PASSED",
                expected_health="available",
                label=(
                    f"{projection_label} projects the passed source gate "
                    "without dynamic authority"
                ),
            )
        assert_status_projects_source_gate(
            artifact_status.json(),
            expected_status=(
                "technical_complete_non_executing_source_gate_only"
            ),
            expected_health="available",
            expected_check_count=1,
            label=(
                "Artifact status projects the bounded source gate and "
                "keeps every later stage locked"
            ),
        )
        assert_status_projects_source_gate(
            spec_status.json(),
            expected_status=(
                "technical_complete_non_executing_source_gate_only"
            ),
            expected_health="available",
            expected_check_count=1,
            label=(
                "Spec status projects the bounded source gate and keeps "
                "every later stage locked"
            ),
        )

        listed = client.get(
            "/phase6/extensions/source-checks",
            params=owner_params(),
        )
        detail = client.get(
            f"/phase6/extensions/source-checks/{check_id}",
            params=owner_params(),
        )
        hidden = client.get(
            f"/phase6/extensions/source-checks/{check_id}",
            params={
                "user_id": "another-user",
                "workspace_id": WORKSPACE,
            },
        )
        missing = client.get(
            (
                "/phase6/extensions/source-checks/"
                "extcheck_000000000000000000000000"
            ),
            params=owner_params(),
        )
        expect(
            listed.status_code == 200
            and listed.json()["count"] == 1
            and listed.json()["checks"][0]["check_id"] == check_id
            and detail.status_code == 200
            and detail.json()["check_id"] == check_id
            and hidden.status_code == 404
            and missing.status_code == 404,
            "source-check list and detail remain owner-scoped",
            {
                "list": listed.json(),
                "detail": detail.json(),
                "hidden": hidden.json(),
                "missing": missing.json(),
            },
        )
        assert_private_public_record(
            listed.json()["checks"][0],
            source=context.source,
        )
        assert_private_public_record(
            detail.json(),
            source=context.source,
        )

        state_path = context.store.path_for(
            SOURCE_CHECK_STATE_FILE
        )
        state_before_integrity = state_path.read_bytes()
        integrity = client.get(
            (
                "/phase6/extensions/source-checks/"
                f"{check_id}/integrity"
            ),
            params=owner_params(),
        )
        expect(
            integrity.status_code == 200
            and integrity.json()["status"]
            == "source_check_integrity_passed"
            and integrity.json()["report_integrity_status"]
            == "validated"
            and integrity.json()["state_mutated"] is False
            and state_path.read_bytes() == state_before_integrity,
            "HTTP integrity verifies exact state without mutation",
            integrity.json(),
        )
        assert_private_public_record(
            integrity.json(),
            source=context.source,
        )

        state_before_replay = state_path.read_bytes()
        replay = client.post(
            path,
            json=command(context, "source-check-control-start"),
        )
        expect(
            replay.status_code == 200
            and replay.json()["operation_replayed"] is True
            and replay.json()["check_id"] == check_id
            and state_path.read_bytes() == state_before_replay,
            "HTTP exact replay is a state no-op",
            replay.json(),
        )
        conflict = client.post(
            path,
            json=command(
                context,
                "source-check-control-start",
                expected_artifact_sha256="f" * 64,
            ),
        )
        stale = client.post(
            path,
            json=command(
                context,
                "source-check-control-stale",
                expected_artifact_revision=99,
            ),
        )
        cross_owner = client.post(
            path,
            json=command(
                context,
                "source-check-control-cross-owner",
                user_id="another-user",
            ),
        )
        expect(
            conflict.status_code == 409
            and stale.status_code == 409
            and cross_owner.status_code == 404,
            "semantic conflict, stale CAS, and cross-owner start fail closed",
            {
                "conflict": conflict.json(),
                "stale": stale.json(),
                "cross_owner": cross_owner.json(),
            },
        )

        raw_private = context.store.read_json(
            SOURCE_CHECK_STATE_FILE
        )
        public = _public_state(
            {
                **context.store.read_all(),
                "phase6_extension_source_check_state": raw_private,
            }
        )
        expect(
            "phase6_extension_source_check_state" not in public
            and "phase6_extension_source_check_state"
            not in context.store.read_all(),
            "generic state omits the private source-check graph",
            sorted(public),
        )

        openapi_paths = app.openapi()["paths"]
        for action in FORBIDDEN_ACTIONS:
            forbidden = (
                "/phase6/extensions/source-checks/"
                f"{check_id}/{action}"
            )
            template = (
                "/phase6/extensions/source-checks/"
                f"{{check_id}}/{action}"
            )
            response = client.post(forbidden, json={})
            expect(
                template not in openapi_paths
                and response.status_code == 404,
                f"{action} source-check endpoint does not exist",
                response.json(),
            )

        restarted_store = WorldStateStore(context.root)
        restarted_spec = ExtensionSpecQuarantine(
            state_store=restarted_store,
            now=context.clock,
        )
        restarted_artifact = ExtensionArtifactQuarantine(
            state_store=restarted_store,
            spec_quarantine=restarted_spec,
            now=context.clock,
        )
        restarted_gate = ExtensionSourcePolicyGate(
            state_store=restarted_store,
            artifact_quarantine=restarted_artifact,
            now=context.clock,
        )
        _restarted_app, restarted_client = build_control_plane(
            restarted_gate
        )
        restarted_detail = restarted_client.get(
            f"/phase6/extensions/source-checks/{check_id}",
            params=owner_params(),
        )
        expect(
            restarted_detail.status_code == 200
            and restarted_detail.json()["source_check_status"]
            == "passed",
            "source-check control plane survives runtime restart",
            restarted_detail.json(),
        )

        context.spec_runtime.revoke(
            candidate_id=context.candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=context.candidate[
                "candidate_revision"
            ],
            operation_id="source-check-control-spec-revoke",
            reason="Invalidate the source-check prerequisite.",
        )
        blocked_detail = client.get(
            f"/phase6/extensions/source-checks/{check_id}",
            params=owner_params(),
        )
        blocked_replay = client.post(
            path,
            json=command(context, "source-check-control-start"),
        )
        blocked_new = client.post(
            path,
            json=command(
                context,
                "source-check-control-new-after-revoke",
            ),
        )
        expect(
            blocked_detail.status_code == 200
            and blocked_detail.json()["effective_status"]
            in {
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
                "SOURCE_CHECK_INDETERMINATE",
            }
            and blocked_replay.status_code == 200
            and blocked_replay.json()["effective_status"]
            in {
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
                "SOURCE_CHECK_INDETERMINATE",
            }
            and blocked_new.status_code in {409, 503},
            "Spec revoke is visible and no new source check is admitted",
            {
                "detail": blocked_detail.json(),
                "replay": blocked_replay.json(),
                "new": blocked_new.json(),
            },
        )
        assert_private_public_record(
            blocked_detail.json(),
            source=context.source,
        )


class FailingChecker:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise RuntimeError("PRIVATE_CHECKER_FAILURE_SENTINEL")


def failure_and_corruption() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-check-control-failure-"
    ) as raw:
        failing_checker = FailingChecker()
        failed = prepare_context(
            Path(raw) / "checker-failure",
            extension_id="example.source_check_control_failure",
            operation_prefix="source-check-control-failure",
            checker=failing_checker,  # type: ignore[arg-type]
        )
        _failed_app, failed_client = build_control_plane(
            failed.source_gate
        )
        failed_response = failed_client.post(
            start_path(failed),
            json=command(
                failed,
                "source-check-control-failing-checker",
            ),
        )
        expect(
            failed_response.status_code == 200
            and failed_response.json()["source_check_status"]
            == "indeterminate"
            and failed_response.json()["effective_status"]
            == "SOURCE_CHECK_INDETERMINATE"
            and failing_checker.calls == 1
            and "PRIVATE_CHECKER_FAILURE_SENTINEL"
            not in failed_response.text,
            "checker failure persists a source-free indeterminate result",
            failed_response.json(),
        )
        assert_private_public_record(
            failed_response.json(),
            source=failed.source,
        )

        corrupt = prepare_context(
            Path(raw) / "state-corruption",
            extension_id="example.source_check_control_corrupt",
            operation_prefix="source-check-control-corrupt",
        )
        _corrupt_app, corrupt_client = build_control_plane(
            corrupt.source_gate
        )
        started = corrupt_client.post(
            start_path(corrupt),
            json=command(
                corrupt,
                "source-check-control-corrupt-start",
            ),
        )
        expect(
            started.status_code == 200,
            "corruption scenario starts from a valid source check",
            started.json(),
        )
        check_id = started.json()["check_id"]
        corrupt.store.path_for(
            SOURCE_CHECK_STATE_FILE
        ).write_text(
            "{invalid-source-check-state",
            encoding="utf-8",
        )
        degraded = corrupt_client.get(
            "/phase6/extensions/source-checks/status"
        )
        unavailable_list = corrupt_client.get(
            "/phase6/extensions/source-checks",
            params=owner_params(),
        )
        unavailable_detail = corrupt_client.get(
            f"/phase6/extensions/source-checks/{check_id}",
            params=owner_params(),
        )
        unavailable_start = corrupt_client.post(
            start_path(corrupt),
            json=command(
                corrupt,
                "source-check-control-after-corruption",
            ),
        )
        expect(
            degraded.status_code == 200
            and degraded.json()["status"] == "fail_closed"
            and degraded.json()["operational_health"] == "degraded"
            and unavailable_list.status_code == 503
            and unavailable_detail.status_code == 503
            and unavailable_start.status_code == 503,
            "private source-check corruption fails closed locally with 503",
            {
                "status": degraded.json(),
                "list": unavailable_list.json(),
                "detail": unavailable_detail.json(),
                "start": unavailable_start.json(),
            },
        )
        artifact_detail = corrupt_client.get(
            (
                "/phase6/extensions/artifacts/"
                f"{corrupt.artifact['artifact_id']}"
            ),
            params=owner_params(),
        )
        artifact_list = corrupt_client.get(
            "/phase6/extensions/artifacts",
            params=owner_params(),
        )
        artifact_status = corrupt_client.get(
            "/phase6/extensions/artifacts/status"
        )
        spec_detail = corrupt_client.get(
            (
                "/phase6/extensions/specs/"
                f"{corrupt.candidate['candidate_id']}"
            ),
            params=owner_params(),
        )
        spec_list = corrupt_client.get(
            "/phase6/extensions/specs",
            params=owner_params(),
        )
        spec_status = corrupt_client.get(
            "/phase6/extensions/status"
        )
        expect(
            artifact_detail.status_code == 200
            and artifact_list.status_code == 200
            and artifact_list.json()["count"] == 1
            and artifact_status.status_code == 200
            and spec_detail.status_code == 200
            and spec_list.status_code == 200
            and spec_list.json()["count"] == 1
            and spec_status.status_code == 200,
            (
                "source-gate corruption stays isolated from Spec and "
                "Artifact queries"
            ),
            {
                "artifact_detail": artifact_detail.json(),
                "artifact_list": artifact_list.json(),
                "artifact_status": artifact_status.json(),
                "spec_detail": spec_detail.json(),
                "spec_list": spec_list.json(),
                "spec_status": spec_status.json(),
            },
        )
        unavailable_projections = {
            "Artifact detail": artifact_detail.json(),
            "Artifact list": artifact_list.json()["artifacts"][0],
            "Spec detail": spec_detail.json(),
            "Spec list": spec_list.json()["candidates"][0],
        }
        for projection_label, projected in (
            unavailable_projections.items()
        ):
            assert_source_projection(
                projected,
                expected_status="indeterminate",
                expected_effective_status="SOURCE_CHECK_UNAVAILABLE",
                expected_health="fail_closed",
                label=(
                    f"{projection_label} explicitly projects source-gate "
                    "unavailability without dynamic authority"
                ),
            )
        assert_status_projects_source_gate(
            artifact_status.json(),
            expected_status="fail_closed",
            expected_health="degraded",
            expected_check_count=None,
            label=(
                "Artifact status reports source-gate corruption while "
                "remaining queryable"
            ),
        )
        assert_status_projects_source_gate(
            spec_status.json(),
            expected_status="fail_closed",
            expected_health="degraded",
            expected_check_count=None,
            label=(
                "Spec status reports source-gate corruption while "
                "remaining queryable"
            ),
        )

        artifact_revoke = corrupt_client.post(
            (
                "/phase6/extensions/artifacts/"
                f"{corrupt.artifact['artifact_id']}/revoke"
            ),
            json={
                "schema_version": (
                    "veyra.phase6.extension_artifact_terminal_command.v1"
                ),
                "operation_id": (
                    "source-check-control-artifact-revoke-after-corrupt"
                ),
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "expected_revision": corrupt.artifact[
                    "artifact_revision"
                ],
                "reason": (
                    "Prove Artifact lifecycle remains independent from "
                    "the corrupt source gate."
                ),
            },
        )
        expect(
            artifact_revoke.status_code == 200
            and artifact_revoke.json()["stage"] == "ARTIFACT_REVOKED"
            and artifact_revoke.json()["source_check_status"]
            == "indeterminate"
            and artifact_revoke.json()[
                "source_check_operational_health"
            ]
            == "fail_closed",
            (
                "Artifact lifecycle mutation succeeds independently and "
                "still exposes a fail-closed source projection"
            ),
            artifact_revoke.json(),
        )
        assert_source_projection(
            artifact_revoke.json(),
            expected_status="indeterminate",
            expected_effective_status="SOURCE_CHECK_UNAVAILABLE",
            expected_health="fail_closed",
            label=(
                "Artifact revoke cannot turn source corruption into "
                "dynamic authority"
            ),
        )
        artifact = corrupt.artifact_runtime.get(
            artifact_id=corrupt.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            artifact["artifact_status"] == "revoked",
            (
                "source-check corruption neither damages nor blocks the "
                "Artifact quarantine lifecycle"
            ),
            artifact,
        )


def real_main_assembly() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-check-main-assembly-"
    ) as raw:
        environment = dict(os.environ)
        environment.update(
            {
                "VEYRA_STATE_DIR": raw,
                "VEYRA_STATE_ROOT": raw,
                "VEYRA_AGENCY_ROOT": str(Path(raw) / "agency"),
                "VEYRA_ACTIVE_LOOP_AUTOSTART": "0",
                "VEYRA_FEISHU_WS_AUTOSTART": "0",
            }
        )
        assembly_code = (
            "from fastapi.testclient import TestClient\n"
            "from runtime.extension_source_policy_gate import STATE_FILE\n"
            "import main\n"
            "client = TestClient(main.app)\n"
            "response = client.get("
            "'/phase6/extensions/source-checks/status')\n"
            "assert response.status_code == 200, response.text\n"
            "body = response.json()\n"
            "assert body['phase'] == '6.2c', body\n"
            "assert body['status'] == "
            "'technical_complete_non_executing_source_gate_only', body\n"
            "assert not any(body['authority'].values()), body\n"
            "assert all(value == 'not_started' "
            "for value in body['next_stage'].values()), body\n"
            "assert main.phase6_extension_source_checks.state_store "
            "is main.state_store\n"
            "assert main.phase6_extension_source_checks."
            "artifact_quarantine is main.phase6_extension_artifacts\n"
            "assert main.phase6_extension_artifacts.spec_quarantine "
            "is main.phase6_extension_specs\n"
            "paths = main.app.openapi()['paths']\n"
            f"expected = {SOURCE_CHECK_PATHS!r}\n"
            "source_paths = {path for path in paths "
            "if 'source-check' in path}\n"
            "assert source_paths == expected, "
            "(source_paths, expected)\n"
            f"for action in {FORBIDDEN_ACTIONS!r}:\n"
            "    assert all("
            "path.rsplit('/', 1)[-1] != action "
            "for path in paths "
            "if path.startswith('/phase6/extensions')), "
            "(action, paths)\n"
            "public = client.get('/state')\n"
            "assert public.status_code == 200, public.text\n"
            "assert 'phase6_extension_source_check_state' "
            "not in public.json()\n"
            "assert 'phase6_extension_source_check_state' "
            "not in main.state_store.read_all()\n"
            "private_path = main.state_store.path_for(STATE_FILE)\n"
            "assert private_path.exists(), private_path\n"
            "assert private_path.is_relative_to("
            "main.state_store.root), private_path\n"
        )
        assembly = subprocess.run(
            [sys.executable, "-c", assembly_code],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        expect(
            assembly.returncode == 0,
            (
                "real main assembles exactly five source-check routes "
                "and no dynamic extension endpoint"
            ),
            {
                "stdout": assembly.stdout[-2_000:],
                "stderr": assembly.stderr[-4_000:],
            },
        )


def main() -> int:
    strict_schema_and_success()
    failure_and_corruption()
    real_main_assembly()
    print("phase6 extension source-check control plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
