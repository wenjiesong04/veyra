#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.phase6_extension_dynamic_validations import (  # noqa: E402
    build_phase6_extension_dynamic_validations_router,
)
from routers.private_control_plane import PrivateControlPlaneRoute  # noqa: E402
from runtime.extension_dynamic_validation_gate import (  # noqa: E402
    ExtensionDynamicValidationGate,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    SOURCE,
    SESSION,
    REQUEST,
    USER,
    WORKSPACE,
    Clock,
    FakeIsolatedRunnerGate,
    FakeValidationBackend,
    expect,
    initialize_store,
    make_subject,
    valid_test_bundle,
)


COMMAND_SCHEMA = "veyra.phase6.extension_dynamic_validation_command.v1"
DYNAMIC_PATHS = {
    "/phase6/extensions/dynamic-validations/status",
    "/phase6/extensions/dynamic-validations/backend/refresh",
    "/phase6/extensions/dynamic-validations",
    "/phase6/extensions/dynamic-validations/{validation_id}",
    "/phase6/extensions/dynamic-validations/{validation_id}/integrity",
    "/phase6/extensions/isolated-runs/{run_id}/dynamic-validation",
}


def persisted_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def command(operation_id: str, **overrides: Any) -> dict[str, Any]:
    subject = make_subject()
    value: dict[str, Any] = {
        "schema_version": COMMAND_SCHEMA,
        "operation_id": operation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "request_id": REQUEST,
        "expected_artifact_revision": subject["artifact_revision"],
        "expected_artifact_sha256": subject["artifact_sha256"],
        "expected_source_check_report_digest": subject[
            "source_check_report_digest"
        ],
        "expected_isolated_runner_report_digest": subject[
            "isolated_runner_report_digest"
        ],
        "test_bundle": valid_test_bundle(),
    }
    value.update(overrides)
    return value


def owner_params(**overrides: str) -> dict[str, str]:
    value = {
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
    }
    value.update(overrides)
    return value


def assert_redacted(value: dict[str, Any], label: str) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    expect(
        SOURCE.decode("utf-8") not in serialized
        and USER not in serialized
        and WORKSPACE not in serialized
        and "input_payload" not in serialized
        and "expected_output" not in serialized
        and "Veyra" not in serialized
        and "owner_scope_digest" not in serialized,
        label,
        value,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-control-") as raw:
        root = Path(raw)
        caller_thread = threading.get_ident()
        backend_threads: list[int] = []

        def observe_thread(_binding: Any) -> None:
            backend_threads.append(threading.get_ident())

        subject_gate = FakeIsolatedRunnerGate()
        backend = FakeValidationBackend(before_return=observe_thread)
        gate = ExtensionDynamicValidationGate(
            state_store=initialize_store(root),
            isolated_runner_gate=subject_gate,  # type: ignore[arg-type]
            backend=backend,  # type: ignore[arg-type]
            enabled=True,
            control_token=CONTROL_TOKEN,
            now=Clock(),
        )
        app = FastAPI()
        router = build_phase6_extension_dynamic_validations_router(gate=gate)
        app.include_router(router)
        client = TestClient(app)
        openapi_paths = set(app.openapi()["paths"])
        expect(
            openapi_paths == DYNAMIC_PATHS
            and len(openapi_paths) == 6
            and all(
                isinstance(route, PrivateControlPlaneRoute)
                for route in router.routes
            ),
            "dynamic validation exposes exactly six private control-plane routes",
            openapi_paths,
        )

        initial_tree = persisted_tree(root)
        status = client.get(
            "/phase6/extensions/dynamic-validations/status"
        )
        unauthorized_empty = client.get(
            "/phase6/extensions/dynamic-validations",
            params=owner_params(),
        )
        empty = client.get(
            "/phase6/extensions/dynamic-validations",
            params=owner_params(),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            status.status_code == 200
            and unauthorized_empty.status_code == 401
            and status.json()["phase"] == "6.2e"
            and status.json()["backend_snapshot"]["status"] == "missing"
            and status.json()["admission"]["start_ready"] is False
            and status.json()["signature_status"] == "not_implemented"
            and status.json()["activation_status"] == "not_installed"
            and status.json()["capability_registry_visible"] is False
            and status.json()["canary_status"] == "not_started"
            and status.json()["promotion_authorized"] is False
            and not any(status.json()["authority"].values())
            and empty.status_code == 200
            and empty.json()["count"] == 0
            and persisted_tree(root) == initial_tree
            and backend.status_calls == 0
            and subject_gate.calls == 0,
            "initial status and list are pure persisted snapshots",
            {"status": status.json(), "empty": empty.json()},
        )

        unauthorized_refresh = client.post(
            "/phase6/extensions/dynamic-validations/backend/refresh"
        )
        refreshed = client.post(
            "/phase6/extensions/dynamic-validations/backend/refresh",
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            unauthorized_refresh.status_code == 401
            and refreshed.status_code == 200
            and refreshed.json()["backend_snapshot"]["status"] == "fresh"
            and refreshed.json()["admission"]["start_ready"] is True
            and backend.status_calls == 1
            and subject_gate.calls == 0,
            "only token-auth POST observes and caches backend readiness",
            refreshed.json(),
        )

        subject = make_subject()
        start_path = (
            "/phase6/extensions/isolated-runs/"
            f"{subject['run_id']}/dynamic-validation"
        )
        invalid_payloads: dict[str, dict[str, Any]] = {
            "source": {
                **command("dynamic-private-source"),
                "source": "PRIVATE_SOURCE_SENTINEL",
            },
            "path": {
                **command("dynamic-private-path"),
                "path": "/PRIVATE/PATH/SENTINEL",
            },
            "command": {
                **command("dynamic-private-command"),
                "argv": ["PRIVATE_ARGV_SENTINEL"],
            },
            "environment": {
                **command("dynamic-private-env"),
                "env": {"PRIVATE_ENV_SENTINEL": "secret"},
            },
            "image": {
                **command("dynamic-private-image"),
                "image_id": "PRIVATE_IMAGE_SENTINEL",
            },
            "oracle": command("dynamic-private-oracle"),
            "coerced revision": command(
                "dynamic-coerced-revision",
                expected_artifact_revision="1",
            ),
        }
        invalid_payloads["oracle"]["test_bundle"] = {
            **valid_test_bundle(),
            "oracle": "PRIVATE_ORACLE_SENTINEL",
        }
        invalid = {
            label: client.post(
                start_path,
                json=payload,
                headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
            )
            for label, payload in invalid_payloads.items()
        }
        invalid_path = client.post(
            "/phase6/extensions/isolated-runs/not-a-run/dynamic-validation",
            json=command("dynamic-invalid-path"),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            all(response.status_code == 422 for response in invalid.values())
            and invalid_path.status_code == 422
            and all(
                response.json() == {"detail": "invalid private control request"}
                for response in (*invalid.values(), invalid_path)
            ),
            "strict command rejects caller source path argv env image oracle and coercion",
            {label: response.json() for label, response in invalid.items()},
        )
        validation_text = "".join(
            response.text for response in (*invalid.values(), invalid_path)
        )
        expect(
            not any(
                sentinel in validation_text
                for sentinel in (
                    "PRIVATE_SOURCE_SENTINEL",
                    "PRIVATE/PATH/SENTINEL",
                    "PRIVATE_ARGV_SENTINEL",
                    "PRIVATE_ENV_SENTINEL",
                    "PRIVATE_IMAGE_SENTINEL",
                    "PRIVATE_ORACLE_SENTINEL",
                    USER,
                    WORKSPACE,
                )
            ),
            "private validation errors never echo caller-controlled data",
            validation_text,
        )

        missing_auth = client.post(
            start_path,
            json=command("dynamic-missing-auth"),
        )
        expect(
            missing_auth.status_code == 401
            and missing_auth.json()
            == {
                "detail": (
                    "valid Veyra control token required for dynamic validation"
                )
            },
            "dynamic validation start requires explicit local control token",
            missing_auth.json(),
        )

        started = client.post(
            start_path,
            json=command("dynamic-http-start"),
            headers={"x-veyra-token": CONTROL_TOKEN},
        )
        expect(
            started.status_code == 200
            and started.json()["stored_stage"] == "DYNAMIC_VALIDATION_PASSED"
            and started.json()["candidate_execution_status"] == "passed"
            and started.json()["unit_checks_status"] == "passed"
            and started.json()["contract_checks_status"] == "passed"
            and started.json()["security_runtime_checks_status"] == "passed"
            and started.json()["fuzz_checks_status"] == "passed"
            and started.json()["behavior_verification_status"] == "passed"
            and backend.run_calls == 1
            and backend.status_calls == 2
            and subject_gate.calls == 3
            and len(backend_threads) == 1
            and backend_threads[0] != caller_thread,
            "HTTP POST dispatches one exact validation through the threadpool",
            {"body": started.json(), "threads": backend_threads},
        )
        assert_redacted(
            started.json(),
            "HTTP start result contains no source owner or behavior vectors",
        )

        validation_id = started.json()["validation_id"]
        before_gets = persisted_tree(root)
        calls_before = (
            subject_gate.calls,
            backend.status_calls,
            backend.run_calls,
        )
        cached = client.get(
            "/phase6/extensions/dynamic-validations/status"
        )
        listed = client.get(
            "/phase6/extensions/dynamic-validations",
            params=owner_params(),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        detail = client.get(
            f"/phase6/extensions/dynamic-validations/{validation_id}",
            params=owner_params(),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        integrity = client.get(
            (
                "/phase6/extensions/dynamic-validations/"
                f"{validation_id}/integrity"
            ),
            params=owner_params(),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        hidden = client.get(
            f"/phase6/extensions/dynamic-validations/{validation_id}",
            params=owner_params(user_id="another-user"),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        hidden_session = client.get(
            f"/phase6/extensions/dynamic-validations/{validation_id}",
            params=owner_params(session_id="another-session"),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            cached.status_code == 200
            and listed.status_code == 200
            and listed.json()["count"] == 1
            and detail.status_code == 200
            and detail.json()["validation_id"] == validation_id
            and integrity.status_code == 200
            and integrity.json()["status"]
            == "dynamic_validation_integrity_passed"
            and hidden.status_code == 404
            and hidden_session.status_code == 404
            and (
                subject_gate.calls,
                backend.status_calls,
                backend.run_calls,
            )
            == calls_before
            and persisted_tree(root) == before_gets,
            "HTTP GETs remain owner-scoped byte-pure snapshots",
            {
                "list": listed.json(),
                "detail": detail.json(),
                "integrity": integrity.json(),
                "hidden": hidden.json(),
                "hidden_session": hidden_session.json(),
            },
        )
        for label, value in {
            "list": listed.json()["validations"][0],
            "detail": detail.json(),
            "integrity": integrity.json(),
        }.items():
            assert_redacted(value, f"HTTP {label} projection is redacted")

    print("phase6 extension dynamic validation control-plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
