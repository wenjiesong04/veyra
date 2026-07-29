#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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
from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
    EXTENSION_ARTIFACT_SCHEMA_VERSION,
    artifact_owner_scope_digest,
    encode_artifact_content,
)
from interface.extension_spec import (  # noqa: E402
    EXTENSION_POLICY_REVISION,
    parse_extension_spec,
)
from routers.debug_audit import _public_state  # noqa: E402
from routers.phase6_extension_artifacts import (  # noqa: E402
    build_phase6_extension_artifacts_router,
)
from routers.phase6_extensions import (  # noqa: E402
    build_phase6_extensions_router,
)
from runtime.extension_artifact_quarantine import (  # noqa: E402
    STATE_FILE as ARTIFACT_STATE_FILE,
    ExtensionArtifactQuarantine,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    STATE_FILE as SPEC_STATE_FILE,
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_artifact_contract_smoke import (  # noqa: E402
    DEFAULT_SOURCE,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


FIXED_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
WORKSPACE = "/private/veyra/phase6-extension-artifact-control"
USER = "phase6-extension-artifact-control-user"
FORBIDDEN_ACTIONS = (
    "generate",
    "test",
    "sign",
    "install",
    "execute",
    "activate",
    "promote",
)


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_control_plane(
    store: WorldStateStore,
) -> tuple[
    ExtensionSpecQuarantine,
    ExtensionArtifactQuarantine,
    FastAPI,
    TestClient,
]:
    spec_runtime = ExtensionSpecQuarantine(
        state_store=store,
        now=lambda: FIXED_NOW,
    )
    artifact_runtime = ExtensionArtifactQuarantine(
        state_store=store,
        spec_quarantine=spec_runtime,
        now=lambda: FIXED_NOW,
    )
    app = FastAPI()
    app.include_router(
        build_phase6_extensions_router(
            quarantine=spec_runtime,
            artifact_quarantine=artifact_runtime,
        )
    )
    app.include_router(
        build_phase6_extension_artifacts_router(
            quarantine=artifact_runtime,
        )
    )
    return spec_runtime, artifact_runtime, app, TestClient(app)


def spec_command(
    *,
    extension_id: str,
    operation_id: str,
    version: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = valid_spec(now=FIXED_NOW)
    payload["extension_id"] = extension_id
    payload["version"] = version
    payload["purpose"] = (
        f"Describe {extension_id} for a future isolated artifact test."
    )
    parsed = parse_extension_spec(payload)
    return payload, {
        "schema_version": (
            "veyra.phase6.extension_spec_quarantine_command.v1"
        ),
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_spec_digest": parsed.digest(),
        "spec": payload,
    }


def review_command(operation_id: str) -> dict[str, Any]:
    return {
        "schema_version": (
            "veyra.phase6.extension_spec_review_command.v1"
        ),
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_revision": 1,
        "decision": "accept_for_future_isolated_generation",
        "reason": (
            "Allow only a future isolated artifact quarantine admission."
        ),
    }


def submit_and_gate_spec(
    client: TestClient,
    *,
    extension_id: str,
    operation_prefix: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, command = spec_command(
        extension_id=extension_id,
        operation_id=f"{operation_prefix}-spec-submit",
    )
    submitted = client.post(
        "/phase6/extensions/specs",
        json=command,
    )
    expect(
        submitted.status_code == 200
        and submitted.json()["stage"] == "SPEC_QUARANTINED"
        and submitted.json()["artifact_status"] == "not_submitted"
        and submitted.json()["artifact_projection"][
            "operational_health"
        ]
        == "available",
        f"{extension_id} starts with no artifact",
        submitted.json(),
    )
    candidate_id = submitted.json()["candidate_id"]
    reviewed = client.post(
        f"/phase6/extensions/specs/{candidate_id}/review",
        json=review_command(f"{operation_prefix}-spec-review"),
    )
    expect(
        reviewed.status_code == 200
        and reviewed.json()["stage"] == "SPEC_GATE_PASSED"
        and reviewed.json()["candidate_revision"] == 2
        and reviewed.json()["artifact_status"] == "not_submitted",
        f"{extension_id} reaches the spec gate without artifact authority",
        reviewed.json(),
    )
    return payload, reviewed.json()


def artifact_command(
    candidate: dict[str, Any],
    *,
    source: bytes,
    operation_id: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(source).hexdigest()
    envelope = {
        "schema_version": EXTENSION_ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": "python_source_utf8",
        "candidate_id": candidate["candidate_id"],
        "candidate_revision": candidate["candidate_revision"],
        "owner_scope_digest": artifact_owner_scope_digest(
            USER,
            WORKSPACE,
        ),
        "extension_id": candidate["extension_id"],
        "extension_version": candidate["extension_version"],
        "spec_digest": candidate["spec_digest"],
        "extension_policy_revision": EXTENSION_POLICY_REVISION,
        "artifact_policy_revision": EXTENSION_ARTIFACT_POLICY_REVISION,
        "artifact_sha256": digest,
        "size_bytes": len(source),
        "content_b64url": encode_artifact_content(source),
        "expires_at": canonical_utc(
            FIXED_NOW + timedelta(days=6)
        ),
    }
    return {
        "schema_version": (
            "veyra.phase6.extension_artifact_quarantine_command.v1"
        ),
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_artifact_sha256": digest,
        "artifact": envelope,
    }


def terminal_command(
    *,
    operation_id: str,
    expected_revision: int = 1,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": (
            "veyra.phase6.extension_artifact_terminal_command.v1"
        ),
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_revision": expected_revision,
        "reason": reason,
    }


def assert_public_artifact(value: dict[str, Any]) -> None:
    expect(
        not any(value["authority"].values())
        and value["capability_registry_visible"] is False
        and value["promotion_authorized"] is False
        and value["execution_status"] == "not_started"
        and value["activation_status"] == "not_installed"
        and value["source_syntax_status"] == "not_checked"
        and value["static_checks_status"] == "not_started"
        and value["behavior_verification_status"] == "not_started",
        "artifact projection grants no authority",
        value,
    )
    private_fields = {
        "content_b64url",
        "blob",
        "owner_scope_digest",
        "user_id",
        "workspace_id",
        "envelope_digest",
    }
    expect(
        not private_fields.intersection(value),
        "artifact projection omits source, blob, and owner bindings",
        value,
    )


def main() -> int:
    with TemporaryDirectory(
        prefix="veyra-phase6-extension-artifact-control-"
    ) as raw:
        state_root = Path(raw) / "state"
        store = WorldStateStore(state_root)
        store.patch_json(
            "local_world.json",
            {"current_project": WORKSPACE},
        )
        (
            _spec_runtime,
            _artifact_runtime,
            app,
            client,
        ) = build_control_plane(store)

        status = client.get(
            "/phase6/extensions/artifacts/status"
        )
        empty_list = client.get(
            "/phase6/extensions/artifacts",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        combined_status = client.get("/phase6/extensions/status")
        expect(
            status.status_code == 200
            and status.json()["phase"] == "6.2b"
            and status.json()["status"]
            == "technical_complete_artifact_quarantine_only"
            and status.json()["storage"]["artifact_count"] == 0
            and not any(status.json()["authority"].values())
            and empty_list.status_code == 200
            and empty_list.json()["count"] == 0
            and combined_status.status_code == 200
            and combined_status.json()["artifact_quarantine"]["phase"]
            == "6.2b",
            "status and empty list expose the artifact-only boundary",
            {
                "artifact": status.json(),
                "combined": combined_status.json(),
            },
        )

        _, blocked_candidate = submit_and_gate_spec(
            client,
            extension_id="example.artifact_blocked",
            operation_prefix="artifact-blocked",
        )
        blocked_command = artifact_command(
            blocked_candidate,
            source=DEFAULT_SOURCE,
            operation_id="artifact-blocked-submit",
        )

        extra = {
            **blocked_command,
            "execute": True,
        }
        coerced = {
            **blocked_command,
            "operation_id": "artifact-blocked-coerced",
            "artifact": {
                **blocked_command["artifact"],
                "candidate_revision": "2",
            },
        }
        extra_response = client.post(
            "/phase6/extensions/artifacts",
            json=extra,
        )
        coerced_response = client.post(
            "/phase6/extensions/artifacts",
            json=coerced,
        )
        private_source = b"PRIVATE_SOURCE_SENTINEL"
        private_source_response = client.post(
            "/phase6/extensions/artifacts",
            json={
                **blocked_command,
                "operation_id": "artifact-private-validation",
                "artifact": {
                    **blocked_command["artifact"],
                    "content_b64url": encode_artifact_content(
                        private_source
                    ),
                },
            },
        )
        private_non_object_response = client.post(
            "/phase6/extensions/artifacts",
            json={
                **blocked_command,
                "operation_id": "artifact-private-non-object",
                "artifact": "PRIVATE_SOURCE_SENTINEL",
            },
        )
        invalid_path = client.get(
            "/phase6/extensions/artifacts/not-an-artifact",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            extra_response.status_code == 422
            and coerced_response.status_code == 422
            and private_source_response.status_code == 422
            and private_non_object_response.status_code == 422
            and invalid_path.status_code == 422,
            "strict request and path schemas reject extras and coercion",
            {
                "extra": extra_response.json(),
                "coerced": coerced_response.json(),
                "private": private_source_response.json(),
                "private_non_object": (
                    private_non_object_response.json()
                ),
                "path": invalid_path.json(),
            },
        )
        private_error_text = (
            private_source_response.text
            + private_non_object_response.text
        )
        expect(
            "PRIVATE_SOURCE_SENTINEL" not in private_error_text
            and encode_artifact_content(private_source)
            not in private_error_text
            and USER not in private_error_text
            and WORKSPACE not in private_error_text,
            "validation errors never echo private source or owner input",
            private_error_text,
        )

        submitted = client.post(
            "/phase6/extensions/artifacts",
            json=blocked_command,
        )
        expect(
            submitted.status_code == 200
            and submitted.json()["stage"] == "ARTIFACT_QUARANTINED"
            and submitted.json()["artifact_status"] == "quarantined"
            and submitted.json()["artifact_integrity_status"]
            == "validated"
            and submitted.json()["candidate_binding_status"]
            == "validated",
            "artifact submission enters only the private quarantine",
            submitted.json(),
        )
        assert_public_artifact(submitted.json())
        blocked_artifact_id = submitted.json()["artifact_id"]

        listed = client.get(
            "/phase6/extensions/artifacts",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        detail = client.get(
            f"/phase6/extensions/artifacts/{blocked_artifact_id}",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        hidden = client.get(
            f"/phase6/extensions/artifacts/{blocked_artifact_id}",
            params={
                "user_id": "another-user",
                "workspace_id": WORKSPACE,
            },
        )
        missing = client.get(
            "/phase6/extensions/artifacts/extart_000000000000000000000000",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            listed.status_code == 200
            and listed.json()["count"] == 1
            and listed.json()["artifacts"][0]["artifact_id"]
            == blocked_artifact_id
            and detail.status_code == 200
            and detail.json()["artifact_id"] == blocked_artifact_id
            and hidden.status_code == 404
            and missing.status_code == 404,
            "list and detail are owner-scoped and hide missing identities",
            {
                "listed": listed.json(),
                "hidden": hidden.json(),
                "missing": missing.json(),
            },
        )

        artifact_state_before = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        integrity = client.get(
            (
                "/phase6/extensions/artifacts/"
                f"{blocked_artifact_id}/integrity"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        artifact_state_after = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        expect(
            integrity.status_code == 200
            and integrity.json()["status"]
            == "artifact_integrity_passed"
            and integrity.json()["state_mutated"] is False
            and artifact_state_before == artifact_state_after,
            "integrity reopens bytes without mutating state",
            integrity.json(),
        )

        stale_terminal = client.post(
            (
                "/phase6/extensions/artifacts/"
                f"{blocked_artifact_id}/reject"
            ),
            json=terminal_command(
                operation_id="artifact-stale-reject",
                expected_revision=99,
                reason="A stale revision must not mutate the artifact.",
            ),
        )
        expect(
            stale_terminal.status_code == 409,
            "revision conflicts return 409",
            stale_terminal.json(),
        )

        _, rejected_candidate = submit_and_gate_spec(
            client,
            extension_id="example.artifact_rejected",
            operation_prefix="artifact-rejected",
        )
        rejected_submission = client.post(
            "/phase6/extensions/artifacts",
            json=artifact_command(
                rejected_candidate,
                source=(
                    b"def bounded_rejected(value: str) -> str:\n"
                    b"    return value\n"
                ),
                operation_id="artifact-rejected-submit",
            ),
        )
        rejected_artifact_id = rejected_submission.json()["artifact_id"]
        rejected = client.post(
            (
                "/phase6/extensions/artifacts/"
                f"{rejected_artifact_id}/reject"
            ),
            json=terminal_command(
                operation_id="artifact-rejected-terminal",
                reason="The operator rejects this inert artifact.",
            ),
        )
        expect(
            rejected_submission.status_code == 200
            and rejected.status_code == 200
            and rejected.json()["stage"] == "ARTIFACT_REJECTED"
            and rejected.json()["artifact_status"] == "rejected"
            and rejected.json()["artifact_revision"] == 2,
            "reject records a terminal artifact state",
            rejected.json(),
        )

        _, revoked_candidate = submit_and_gate_spec(
            client,
            extension_id="example.artifact_revoked",
            operation_prefix="artifact-revoked",
        )
        revoked_submission = client.post(
            "/phase6/extensions/artifacts",
            json=artifact_command(
                revoked_candidate,
                source=(
                    b"def bounded_revoked(value: str) -> str:\n"
                    b"    return value\n"
                ),
                operation_id="artifact-revoked-submit",
            ),
        )
        revoked_artifact_id = revoked_submission.json()["artifact_id"]
        revoked = client.post(
            (
                "/phase6/extensions/artifacts/"
                f"{revoked_artifact_id}/revoke"
            ),
            json=terminal_command(
                operation_id="artifact-revoked-terminal",
                reason="The operator revokes this inert artifact.",
            ),
        )
        revoked_spec_projection = client.get(
            (
                "/phase6/extensions/specs/"
                f"{revoked_candidate['candidate_id']}"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            revoked_submission.status_code == 200
            and revoked.status_code == 200
            and revoked.json()["stage"] == "ARTIFACT_REVOKED"
            and revoked.json()["artifact_status"] == "revoked"
            and revoked_spec_projection.status_code == 200
            and revoked_spec_projection.json()["artifact_status"]
            == "revoked",
            "revoke is visible through the owning spec projection",
            {
                "artifact": revoked.json(),
                "spec": revoked_spec_projection.json(),
            },
        )

        spec_revoked = client.post(
            (
                "/phase6/extensions/specs/"
                f"{blocked_candidate['candidate_id']}/revoke"
            ),
            json={
                "schema_version": (
                    "veyra.phase6.extension_spec_revoke_command.v1"
                ),
                "operation_id": "artifact-blocked-spec-revoke",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "expected_revision": 2,
                "reason": (
                    "Revoke the spec while retaining its inert artifact."
                ),
            },
        )
        blocked_spec_projection = client.get(
            (
                "/phase6/extensions/specs/"
                f"{blocked_candidate['candidate_id']}"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        blocked_artifact = client.get(
            (
                "/phase6/extensions/artifacts/"
                f"{blocked_artifact_id}"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            spec_revoked.status_code == 200
            and spec_revoked.json()["stage"] == "REVOKED"
            and spec_revoked.json()["artifact_status"]
            == "blocked_candidate"
            and blocked_spec_projection.status_code == 200
            and blocked_spec_projection.json()["artifact_status"]
            == "blocked_candidate"
            and blocked_artifact.status_code == 200
            and blocked_artifact.json()["effective_status"]
            == "BLOCKED_CANDIDATE",
            "spec revocation blocks but does not delete its artifact",
            {
                "spec": blocked_spec_projection.json(),
                "artifact": blocked_artifact.json(),
            },
        )

        openapi_paths = app.openapi()["paths"]
        for action in FORBIDDEN_ACTIONS:
            unavailable_path = (
                "/phase6/extensions/artifacts/"
                f"{blocked_artifact_id}/{action}"
            )
            template_path = (
                "/phase6/extensions/artifacts/"
                f"{{artifact_id}}/{action}"
            )
            response = client.post(unavailable_path, json={})
            expect(
                template_path not in openapi_paths
                and response.status_code == 404,
                f"{action} artifact endpoint does not exist",
                response.json(),
            )

        raw_artifact_state = store.read_json(ARTIFACT_STATE_FILE)
        public = _public_state(
            {
                **store.read_all(),
                "phase6_extension_spec_state": store.read_json(
                    SPEC_STATE_FILE
                ),
                "phase6_extension_artifact_state": raw_artifact_state,
            }
        )
        blob_root = (
            store.path_for(ARTIFACT_STATE_FILE).parent
            / "phase6_extension_artifacts"
        )
        expect(
            "phase6_extension_spec_state" not in public
            and "phase6_extension_artifact_state" not in public
            and "phase6_extension_spec_state" not in store.read_all()
            and "phase6_extension_artifact_state" not in store.read_all()
            and blob_root.is_dir()
            and blob_root.resolve().is_relative_to(
                state_root.resolve()
            ),
            "generic state omits private manifests, records, and blob paths",
            {
                "public_keys": sorted(public),
                "blob_root": str(blob_root),
            },
        )

        (
            _restarted_spec,
            _restarted_artifact,
            _restarted_app,
            restarted_client,
        ) = build_control_plane(store)
        restarted_status = restarted_client.get(
            "/phase6/extensions/artifacts/status"
        )
        restarted_detail = restarted_client.get(
            (
                "/phase6/extensions/artifacts/"
                f"{revoked_artifact_id}"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            restarted_status.status_code == 200
            and restarted_status.json()["storage"]["artifact_count"] == 3
            and restarted_detail.status_code == 200
            and restarted_detail.json()["artifact_status"] == "revoked",
            "default state-relative blob root survives runtime restart",
            {
                "status": restarted_status.json(),
                "detail": restarted_detail.json(),
            },
        )

        store.path_for(ARTIFACT_STATE_FILE).write_text(
            "{invalid-artifact-state",
            encoding="utf-8",
        )
        degraded = client.get(
            "/phase6/extensions/artifacts/status"
        )
        unavailable_list = client.get(
            "/phase6/extensions/artifacts",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        unavailable_detail = client.get(
            (
                "/phase6/extensions/specs/"
                f"{revoked_candidate['candidate_id']}"
            ),
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        _, independent_spec_command = spec_command(
            extension_id="example.spec_survives_artifact_fault",
            operation_id="artifact-fault-independent-spec-submit",
        )
        independent_spec = client.post(
            "/phase6/extensions/specs",
            json=independent_spec_command,
        )
        blocked_artifact_mutation = client.post(
            "/phase6/extensions/artifacts",
            json={
                **blocked_command,
                "operation_id": "artifact-after-state-corruption",
            },
        )
        expect(
            degraded.status_code == 200
            and degraded.json()["status"] == "fail_closed"
            and degraded.json()["operational_health"] == "degraded"
            and unavailable_list.status_code == 503
            and blocked_artifact_mutation.status_code == 503
            and unavailable_detail.status_code == 200
            and unavailable_detail.json()["artifact_status"]
            == "unavailable"
            and unavailable_detail.json()["artifact_projection"][
                "operational_health"
            ]
            == "fail_closed"
            and independent_spec.status_code == 200
            and independent_spec.json()["stage"] == "SPEC_QUARANTINED"
            and independent_spec.json()["artifact_status"]
            == "unavailable",
            "artifact corruption fails closed locally without blocking spec mutation",
            {
                "artifact_status": degraded.json(),
                "artifact_list": unavailable_list.json(),
                "spec_detail": unavailable_detail.json(),
                "spec_mutation": independent_spec.json(),
                "artifact_mutation": blocked_artifact_mutation.json(),
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-artifact-main-assembly-"
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
        assembly = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from fastapi.testclient import TestClient\n"
                    "from runtime.extension_artifact_quarantine import "
                    "STATE_FILE\n"
                    "import main\n"
                    "client = TestClient(main.app)\n"
                    "response = client.get("
                    "'/phase6/extensions/artifacts/status')\n"
                    "assert response.status_code == 200, response.text\n"
                    "assert response.json()['phase'] == '6.2b', "
                    "response.json()\n"
                    "assert main.phase6_extension_artifacts.state_store "
                    "is main.state_store\n"
                    "assert main.phase6_extension_artifacts.spec_quarantine "
                    "is main.phase6_extension_specs\n"
                    "paths = main.app.openapi()['paths']\n"
                    "assert '/phase6/extensions/artifacts' in paths\n"
                    "assert '/phase6/extensions/artifacts/{artifact_id}' "
                    "in paths\n"
                    "assert '/phase6/extensions/artifacts/"
                    "{artifact_id}/integrity' in paths\n"
                    f"for suffix in {FORBIDDEN_ACTIONS!r}:\n"
                    "    assert f'/phase6/extensions/artifacts/"
                    "{{artifact_id}}/{suffix}' not in paths\n"
                    "public = client.get('/state')\n"
                    "assert public.status_code == 200, public.text\n"
                    "assert 'phase6_extension_spec_state' "
                    "not in public.json()\n"
                    "assert 'phase6_extension_artifact_state' "
                    "not in public.json()\n"
                    "assert 'phase6_extension_spec_state' "
                    "not in main.state_store.read_all()\n"
                    "assert 'phase6_extension_artifact_state' "
                    "not in main.state_store.read_all()\n"
                    "private_path = main.state_store.path_for(STATE_FILE)\n"
                    "assert private_path.exists(), private_path\n"
                    "assert private_path.is_relative_to("
                    "main.state_store.root)\n"
                ),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        expect(
            assembly.returncode == 0,
            "real main assembly wires the private artifact control plane",
            {
                "stdout": assembly.stdout[-2_000:],
                "stderr": assembly.stderr[-4_000:],
            },
        )

    print("phase6 extension artifact control plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
