#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
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
from interface.extension_spec import parse_extension_spec  # noqa: E402
from routers.debug_audit import _public_state  # noqa: E402
from routers.phase6_extensions import (  # noqa: E402
    build_phase6_extensions_router,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    STATE_FILE,
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


WORKSPACE = "/private/veyra/phase6-extension-control"
USER = "phase6-extension-control-user"


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> int:
    with TemporaryDirectory(
        prefix="veyra-phase6-extension-control-"
    ) as raw:
        store = WorldStateStore(Path(raw))
        store.mutate_json(
            "local_world.json",
            lambda state: {
                **state,
                "current_project": WORKSPACE,
            },
        )
        runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=lambda: datetime(
                2026,
                7,
                30,
                8,
                0,
                tzinfo=timezone.utc,
            ),
        )
        app = FastAPI()
        app.include_router(
            build_phase6_extensions_router(
                quarantine=runtime,
            )
        )
        client = TestClient(app)

        status = client.get("/phase6/extensions/status")
        expect(
            status.status_code == 200
            and status.json()["phase"] == "6.2a"
            and status.json()["status"]
            == "technical_complete_specification_only"
            and status.json()["authority"]["execution"] is False
            and status.json()["next_stage"]["promotion"]
            == "not_implemented",
            "status exposes the specification-only completion boundary",
            status.json(),
        )

        spec_payload = valid_spec(
            now=datetime(
                2026,
                7,
                30,
                8,
                0,
                tzinfo=timezone.utc,
            )
        )
        spec_payload["extension_id"] = "example.http_control"
        parsed = parse_extension_spec(spec_payload)
        body = {
            "schema_version": (
                "veyra.phase6.extension_spec_quarantine_command.v1"
            ),
            "operation_id": "phase6-extension-http-submit",
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_spec_digest": parsed.digest(),
            "spec": spec_payload,
        }
        submitted = client.post(
            "/phase6/extensions/specs",
            json=body,
        )
        expect(
            submitted.status_code == 200
            and submitted.json()["stage"] == "SPEC_QUARANTINED"
            and submitted.json()["capability_registry_visible"]
            is False
            and submitted.json()["execution_status"] == "not_started",
            "HTTP submission returns only the public quarantine projection",
            submitted.json(),
        )
        candidate_id = submitted.json()["candidate_id"]
        expect(
            "spec" not in submitted.json()
            and "purpose" not in submitted.json()
            and "user_id" not in submitted.json()
            and "workspace_id" not in submitted.json(),
            "HTTP response redacts private manifest and owner fields",
            submitted.json(),
        )

        listed = client.get(
            "/phase6/extensions/specs",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        detail = client.get(
            f"/phase6/extensions/specs/{candidate_id}",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            listed.status_code == 200
            and listed.json()["count"] == 1
            and detail.status_code == 200
            and detail.json()["candidate_id"] == candidate_id,
            "list and detail are explicitly owner scoped",
        )
        hidden = client.get(
            f"/phase6/extensions/specs/{candidate_id}",
            params={
                "user_id": "another-user",
                "workspace_id": WORKSPACE,
            },
        )
        expect(
            hidden.status_code == 404,
            "owner mismatch returns not found",
            hidden.json(),
        )

        private_before = store.path_for(STATE_FILE).read_bytes()
        integrity = client.get(
            f"/phase6/extensions/specs/{candidate_id}/integrity",
            params={
                "user_id": USER,
                "workspace_id": WORKSPACE,
            },
        )
        private_after = store.path_for(STATE_FILE).read_bytes()
        expect(
            integrity.status_code == 200
            and integrity.json()["status"] == "spec_integrity_passed"
            and integrity.json()["behavior_verification_status"]
            == "not_started"
            and private_before == private_after,
            "HTTP integrity endpoint is read-only and non-executing",
            integrity.json(),
        )

        review_body = {
            "schema_version": (
                "veyra.phase6.extension_spec_review_command.v1"
            ),
            "operation_id": "phase6-extension-http-review",
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_revision": 1,
            "decision": (
                "accept_for_future_isolated_generation"
            ),
            "reason": "Only the future isolated generator may continue.",
        }
        reviewed = client.post(
            f"/phase6/extensions/specs/{candidate_id}/review",
            json=review_body,
        )
        replayed = client.post(
            f"/phase6/extensions/specs/{candidate_id}/review",
            json=review_body,
        )
        expect(
            reviewed.status_code == 200
            and reviewed.json()["stage"] == "SPEC_GATE_PASSED"
            and replayed.status_code == 200
            and replayed.json()["operation_replayed"] is True,
            "review is revision-bound and exactly replayable",
            replayed.json(),
        )

        extra = dict(body)
        extra["activate"] = True
        extra_response = client.post(
            "/phase6/extensions/specs",
            json=extra,
        )
        coerced = dict(body)
        coerced["spec"] = {
            **spec_payload,
            "version": True,
        }
        coerced["operation_id"] = "phase6-extension-http-coerce"
        coerced_response = client.post(
            "/phase6/extensions/specs",
            json=coerced,
        )
        expect(
            extra_response.status_code == 422
            and coerced_response.status_code == 422,
            "strict HTTP contract rejects extras and type coercion",
        )

        for unavailable_path in (
            f"/phase6/extensions/specs/{candidate_id}/sign",
            f"/phase6/extensions/specs/{candidate_id}/promote",
            f"/phase6/extensions/specs/{candidate_id}/activate",
            f"/phase6/extensions/specs/{candidate_id}/execute",
        ):
            response = client.post(unavailable_path, json={})
            expect(
                response.status_code == 404,
                f"{unavailable_path.rsplit('/', 1)[-1]} endpoint does not exist",
                response.json(),
            )

        public = _public_state(
            {
                "local_world": {"current_project": "safe"},
                "phase6_extension_spec_state": {
                    "candidates": {
                        candidate_id: {
                            "spec": spec_payload,
                            "user_id": USER,
                            "workspace_id": WORKSPACE,
                        }
                    }
                },
            }
        )
        expect(
            "phase6_extension_spec_state" not in public
            and "phase6_extension_spec_state"
            not in store.read_all(),
            "generic state projections omit private extension manifests",
            public,
        )

        store.path_for(STATE_FILE).write_text(
            "{invalid-extension-state",
            encoding="utf-8",
        )
        degraded = client.get("/phase6/extensions/status")
        blocked = client.post(
            "/phase6/extensions/specs",
            json={
                **body,
                "operation_id": "phase6-extension-http-after-corrupt",
            },
        )
        expect(
            degraded.status_code == 200
            and degraded.json()["operational_health"] == "degraded"
            and degraded.json()["status"] == "fail_closed"
            and blocked.status_code == 503,
            "corrupt extension state degrades locally and blocks mutation",
            {
                "status": degraded.json(),
                "blocked": blocked.json(),
            },
        )

        restarted_runtime = ExtensionSpecQuarantine(
            state_store=store,
            now=lambda: datetime(
                2026,
                7,
                30,
                8,
                0,
                tzinfo=timezone.utc,
            ),
        )
        restarted_app = FastAPI()
        restarted_app.include_router(
            build_phase6_extensions_router(
                quarantine=restarted_runtime,
            )
        )
        restarted_client = TestClient(restarted_app)
        restarted_status = restarted_client.get(
            "/phase6/extensions/status"
        )
        restarted_blocked = restarted_client.post(
            "/phase6/extensions/specs",
            json={
                **body,
                "operation_id": (
                    "phase6-extension-http-after-corrupt-restart"
                ),
            },
        )
        expect(
            restarted_status.status_code == 200
            and restarted_status.json()["status"] == "fail_closed"
            and restarted_blocked.status_code == 503,
            "corrupt private state cannot fail open after API restart",
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-extension-main-assembly-"
    ) as raw:
        environment = dict(os.environ)
        environment.update(
            {
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
                    "import main\n"
                    "client = TestClient(main.app)\n"
                    "response = client.get('/phase6/extensions/status')\n"
                    "assert response.status_code == 200, response.text\n"
                    "body = response.json()\n"
                    "assert body['phase'] == '6.2a', body\n"
                    "paths = main.app.openapi()['paths']\n"
                    "assert '/phase6/extensions/specs/{candidate_id}/integrity' "
                    "in paths\n"
                    "for suffix in ('sign', 'promote', 'activate', 'execute'):\n"
                    "    assert f'/phase6/extensions/specs/"
                    "{{candidate_id}}/{suffix}' not in paths\n"
                ),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        expect(
            assembly.returncode == 0,
            "real main assembly exposes only the Phase 6.2a control plane",
            {
                "stdout": assembly.stdout[-2_000:],
                "stderr": assembly.stderr[-2_000:],
            },
        )

    print("phase6 extension control plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
