#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.debug_audit import _public_state  # noqa: E402
from routers.phase6_extension_isolated_runner import (  # noqa: E402
    build_phase6_extension_isolated_runner_router,
)
from runtime.extension_isolated_runner_gate import (  # noqa: E402
    STATE_FILE as RUNNER_STATE_FILE,
    ExtensionIsolatedRunnerGate,
    ExtensionIsolatedRunnerStorageError,
)
from scripts.phase6_extension_isolated_runner_lifecycle_smoke import (  # noqa: E402
    CONTROL_TOKEN,
    USER,
    WORKSPACE,
    CertifiedFakeBackend,
    assert_public_redacted,
    assert_zero_future_authority,
    build_gate,
    passed_context,
)


COMMAND_SCHEMA = "veyra.phase6.extension_isolated_run_command.v1"
RUNNER_PATHS = {
    "/phase6/extensions/isolated-runs/status",
    "/phase6/extensions/isolated-runs",
    "/phase6/extensions/isolated-runs/{run_id}",
    "/phase6/extensions/isolated-runs/{run_id}/integrity",
    "/phase6/extensions/source-checks/{check_id}/isolated-run",
}
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
    "canary",
    "promote",
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def build_control_plane(
    gate: ExtensionIsolatedRunnerGate | Any,
    *,
    raise_server_exceptions: bool = True,
) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.include_router(
        build_phase6_extension_isolated_runner_router(gate=gate)
    )
    return app, TestClient(
        app,
        raise_server_exceptions=raise_server_exceptions,
    )


def command(
    context: Any,
    report_digest: str,
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
        "expected_artifact_sha256": context.artifact["artifact_sha256"],
        "expected_source_check_report_digest": report_digest,
    }
    payload.update(overrides)
    return payload


def owner_params() -> dict[str, str]:
    return {"user_id": USER, "workspace_id": WORKSPACE}


def strict_routes_schema_and_success() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-control-") as raw:
        context, checked, report_digest = passed_context(
            Path(raw),
            extension_id="example.runner_control",
            operation_prefix="runner-control",
        )
        caller_thread = threading.get_ident()
        backend_threads: list[int] = []

        def observe_thread(_source: bytes, _binding: Any) -> None:
            backend_threads.append(threading.get_ident())

        backend = CertifiedFakeBackend(before_return=observe_thread)
        gate = build_gate(context, backend)
        app, client = build_control_plane(gate)
        openapi_paths = set(app.openapi()["paths"])
        expect(
            openapi_paths == RUNNER_PATHS and len(openapi_paths) == 5,
            "isolated-runner exposes exactly five private routes",
            openapi_paths,
        )

        status = client.get("/phase6/extensions/isolated-runs/status")
        empty = client.get(
            "/phase6/extensions/isolated-runs",
            params=owner_params(),
        )
        expect(
            status.status_code == 200
            and status.json()["phase"] == "6.2d"
            and status.json()["status"]
            == "technical_complete_isolated_runner_only"
            and status.json()["admission"]["start_ready"] is True
            and status.json()["isolation_contract"]["candidate_executed"]
            is False
            and not any(status.json()["authority"].values())
            and all(
                value == "not_started"
                for value in status.json()["next_stage"].values()
            )
            and empty.status_code == 200
            and empty.json()["count"] == 0
            and not any(empty.json()["authority"].values()),
            "status and empty list expose only the bounded runner boundary",
            {"status": status.json(), "empty": empty.json()},
        )

        start_path = (
            "/phase6/extensions/source-checks/"
            f"{checked['check_id']}/isolated-run"
        )
        invalid_payloads = {
            "source": {
                **command(
                    context,
                    report_digest,
                    "runner-control-private-source",
                ),
                "source": "PRIVATE_SOURCE_SENTINEL",
            },
            "path": {
                **command(
                    context,
                    report_digest,
                    "runner-control-private-path",
                ),
                "path": "/PRIVATE/PATH/SENTINEL",
            },
            "argv": {
                **command(
                    context,
                    report_digest,
                    "runner-control-private-argv",
                ),
                "argv": ["PRIVATE_ARGV_SENTINEL"],
            },
            "environment": {
                **command(
                    context,
                    report_digest,
                    "runner-control-private-env",
                ),
                "env": {"PRIVATE_ENV_SENTINEL": "secret"},
            },
            "image": {
                **command(
                    context,
                    report_digest,
                    "runner-control-caller-image",
                ),
                "image_id": "PRIVATE_IMAGE_SENTINEL",
            },
            "tests": {
                **command(
                    context,
                    report_digest,
                    "runner-control-caller-tests",
                ),
                "tests": ["PRIVATE_TEST_SENTINEL"],
            },
            "coerced_revision": command(
                context,
                report_digest,
                "runner-control-coerced-revision",
                expected_artifact_revision="1",
            ),
        }
        invalid_responses = {
            label: client.post(
                start_path,
                json=payload,
                headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
            )
            for label, payload in invalid_payloads.items()
        }
        invalid_path = client.post(
            (
                "/phase6/extensions/source-checks/not-a-check/"
                "isolated-run"
            ),
            json=command(
                context,
                report_digest,
                "runner-control-invalid-path",
            ),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            all(
                response.status_code == 422
                for response in invalid_responses.values()
            )
            and invalid_path.status_code == 422
            and all(
                response.json() == {"detail": "invalid private control request"}
                for response in (*invalid_responses.values(), invalid_path)
            ),
            (
                "strict private command rejects source, path, argv, env, "
                "image, tests, and coercion"
            ),
            {label: response.json() for label, response in invalid_responses.items()},
        )
        validation_text = "".join(
            response.text for response in (*invalid_responses.values(), invalid_path)
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
                    "PRIVATE_TEST_SENTINEL",
                    USER,
                    WORKSPACE,
                )
            ),
            "private validation errors never echo caller-controlled input",
            validation_text,
        )

        missing_auth = client.post(
            start_path,
            json=command(
                context,
                report_digest,
                "runner-control-missing-auth",
            ),
        )
        expect(
            missing_auth.status_code == 401
            and missing_auth.json()
            == {"detail": "valid Veyra control token required for isolated run"},
            "start requires an explicit control token with a sanitized error",
            missing_auth.json(),
        )

        started = client.post(
            start_path,
            json=command(
                context,
                report_digest,
                "runner-control-start",
            ),
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        expect(
            started.status_code == 200
            and started.json()["stored_stage"] == "RUNNER_JOB_PASSED"
            and started.json()["trusted_isolated_runner_status"] == "passed"
            and backend.calls == 1
            and len(backend_threads) == 1
            and backend_threads[0] != caller_thread,
            "HTTP start dispatches once through the threadpool after admission",
            {"body": started.json(), "threads": backend_threads},
        )
        record = started.json()
        run_id = record["run_id"]
        assert_zero_future_authority(
            record,
            "HTTP result leaves all candidate and production authority locked",
        )
        assert_public_redacted(
            record,
            source=context.source,
            label="HTTP start result omits source and private runtime data",
        )

        listed = client.get(
            "/phase6/extensions/isolated-runs",
            params=owner_params(),
        )
        detail = client.get(
            f"/phase6/extensions/isolated-runs/{run_id}",
            params=owner_params(),
        )
        state_before_integrity = context.store.path_for(
            RUNNER_STATE_FILE
        ).read_bytes()
        integrity = client.get(
            f"/phase6/extensions/isolated-runs/{run_id}/integrity",
            params=owner_params(),
        )
        hidden = client.get(
            f"/phase6/extensions/isolated-runs/{run_id}",
            params={"user_id": "another-user", "workspace_id": WORKSPACE},
        )
        expect(
            listed.status_code == 200
            and listed.json()["count"] == 1
            and detail.status_code == 200
            and detail.json()["run_id"] == run_id
            and integrity.status_code == 200
            and integrity.json()["status"] == "isolated_runner_integrity_passed"
            and integrity.json()["state_mutated"] is False
            and context.store.path_for(RUNNER_STATE_FILE).read_bytes()
            == state_before_integrity
            and hidden.status_code == 404,
            "list/detail/integrity are owner-scoped and integrity is read-only",
            {
                "list": listed.json(),
                "detail": detail.json(),
                "integrity": integrity.json(),
                "hidden": hidden.json(),
            },
        )
        for label, value in {
            "list": listed.json()["runs"][0],
            "detail": detail.json(),
            "integrity": integrity.json(),
        }.items():
            assert_public_redacted(
                value,
                source=context.source,
                label=f"HTTP {label} projection is private-data-free",
            )

        public_state = _public_state(
            {
                **context.store.read_all(),
                "phase6_extension_isolated_runner_state": (
                    context.store.read_json(RUNNER_STATE_FILE)
                ),
            }
        )
        expect(
            "phase6_extension_isolated_runner_state" not in public_state
            and "phase6_extension_isolated_runner_state"
            not in context.store.read_all(),
            "generic state never exposes the private isolated-run graph",
            sorted(public_state),
        )

        for action in FORBIDDEN_ACTIONS:
            forbidden = (
                f"/phase6/extensions/isolated-runs/{run_id}/{action}"
            )
            response = client.post(forbidden, json={})
            expect(
                response.status_code == 404
                and all(
                    path.rsplit("/", 1)[-1] != action
                    for path in openapi_paths
                ),
                f"{action} endpoint does not exist",
                response.json(),
            )


class FailingGate:
    def status(self) -> dict[str, Any]:
        raise ExtensionIsolatedRunnerStorageError(
            "PRIVATE_STATUS_FAILURE_SENTINEL"
        )

    def list(self, **_kwargs: Any) -> dict[str, Any]:
        raise ExtensionIsolatedRunnerStorageError(
            "PRIVATE_LIST_FAILURE_SENTINEL"
        )

    def get(self, **_kwargs: Any) -> dict[str, Any]:
        raise ExtensionIsolatedRunnerStorageError(
            "PRIVATE_GET_FAILURE_SENTINEL"
        )

    def integrity(self, **_kwargs: Any) -> dict[str, Any]:
        raise ExtensionIsolatedRunnerStorageError(
            "PRIVATE_INTEGRITY_FAILURE_SENTINEL"
        )

    def start(self, **_kwargs: Any) -> dict[str, Any]:
        raise ExtensionIsolatedRunnerStorageError(
            "PRIVATE_START_FAILURE_SENTINEL"
        )


def sanitized_runtime_errors() -> None:
    app, client = build_control_plane(
        FailingGate(),
        raise_server_exceptions=False,
    )
    run_id = "extrun_0123456789abcdef01234567"
    check_id = "extcheck_0123456789abcdef01234567"
    requests = (
        client.get(
            "/phase6/extensions/isolated-runs",
            params=owner_params(),
        ),
        client.get(
            f"/phase6/extensions/isolated-runs/{run_id}",
            params=owner_params(),
        ),
        client.get(
            f"/phase6/extensions/isolated-runs/{run_id}/integrity",
            params=owner_params(),
        ),
        client.post(
            f"/phase6/extensions/source-checks/{check_id}/isolated-run",
            json={
                "schema_version": COMMAND_SCHEMA,
                "operation_id": "runner-sanitized-failure",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "expected_artifact_revision": 1,
                "expected_artifact_sha256": "a" * 64,
                "expected_source_check_report_digest": "b" * 64,
            },
            headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
        ),
    )
    combined = "".join(response.text for response in requests)
    expect(
        all(response.status_code == 503 for response in requests)
        and all(
            response.json()
            == {"detail": "trusted isolated runner is unavailable"}
            for response in requests
        )
        and "PRIVATE_" not in combined
        and USER not in combined
        and WORKSPACE not in combined,
        "runtime failures are sanitized across all owner-scoped routes",
        [response.json() for response in requests],
    )
    expect(
        set(app.openapi()["paths"]) == RUNNER_PATHS,
        "error handling does not broaden the private route surface",
    )


def real_main_assembly() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-main-") as raw:
        environment = dict(os.environ)
        environment.update(
            {
                "VEYRA_STATE_DIR": raw,
                "VEYRA_STATE_ROOT": raw,
                "VEYRA_AGENCY_ROOT": str(Path(raw) / "agency"),
                "VEYRA_ACTIVE_LOOP_AUTOSTART": "0",
                "VEYRA_FEISHU_WS_AUTOSTART": "0",
                "VEYRA_PHASE6_TRUSTED_RUNNER_ENABLED": "0",
                "VEYRA_LOCAL_API_TOKEN": "",
                "VEYRA_ISOLATED_RUNNER_DOCKER_BINARY": "/nonexistent/veyra-docker",
                "VEYRA_ISOLATED_RUNNER_EXPECTED_IMAGE_ID": "",
                "VEYRA_ISOLATED_RUNNER_EXPECTED_ENGINE_DIGEST": "",
                "VEYRA_ISOLATED_RUNNER_CONFORMANCE_DIGEST": "",
                "VEYRA_ISOLATED_RUNNER_CONFORMANCE_CERTIFIED": "0",
            }
        )
        code = (
            "from fastapi.testclient import TestClient\n"
            "from runtime.extension_isolated_runner_gate import STATE_FILE\n"
            "import main\n"
            "client = TestClient(main.app)\n"
            "status = client.get('/phase6/extensions/isolated-runs/status')\n"
            "assert status.status_code == 200, status.text\n"
            "body = status.json()\n"
            "assert body['phase'] == '6.2d', body\n"
            "assert body['status'] == 'fail_closed', body\n"
            "assert not any(body['authority'].values()), body\n"
            "assert all(value == 'not_started' for value in "
            "body['next_stage'].values()), body\n"
            "paths = main.app.openapi()['paths']\n"
            f"expected = {RUNNER_PATHS!r}\n"
            "runner_paths = {path for path in paths if 'isolated-run' in path}\n"
            "assert runner_paths == expected, (runner_paths, expected)\n"
            f"for action in {FORBIDDEN_ACTIONS!r}:\n"
            "    assert all(path.rsplit('/', 1)[-1] != action for path in "
            "paths if path.startswith('/phase6/extensions')), "
            "(action, paths)\n"
            "assert main.phase6_extension_isolated_runner.state_store is "
            "main.state_store\n"
            "assert main.phase6_extension_isolated_runner.source_check_gate "
            "is main.phase6_extension_source_checks\n"
            "public = client.get('/state')\n"
            "assert public.status_code == 200, public.text\n"
            "assert 'phase6_extension_isolated_runner_state' not in public.json()\n"
            "assert 'phase6_extension_isolated_runner_state' not in "
            "main.state_store.read_all()\n"
            "private_path = main.state_store.path_for(STATE_FILE)\n"
            "assert private_path.exists(), private_path\n"
            "assert private_path.is_relative_to(main.state_store.root), private_path\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        expect(
            result.returncode == 0,
            "real main assembles exactly the fail-closed five-route runner slice",
            {
                "stdout": result.stdout[-2_000:],
                "stderr": result.stderr[-4_000:],
            },
        )


def main() -> int:
    strict_routes_schema_and_success()
    sanitized_runtime_errors()
    real_main_assembly()
    print("phase6 extension isolated-runner control plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
