#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.phase6_extension_generation import (  # noqa: E402
    build_phase6_extension_generation_router,
)
from scripts.phase6_extension_generation_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    REQUEST,
    SESSION,
    USER,
    WORKSPACE,
    directory_bytes,
    prepare_generation_context,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def command(context: Any) -> dict[str, Any]:
    candidate = context.gated.candidate
    return {
        "schema_version": "veyra.phase6.extension_generation_command.v1",
        "operation_id": "http-generation-start",
        "request_id": REQUEST,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "expected_candidate_revision": candidate["candidate_revision"],
        "expected_spec_digest": candidate["spec_digest"],
    }


def main() -> int:
    context = prepare_generation_context(
        extension_id="example.generated_http",
        operation_prefix="generation-http",
    )
    try:
        app = FastAPI()
        app.include_router(
            build_phase6_extension_generation_router(gate=context.gate)
        )
        client = TestClient(app)
        paths = {
            route.path
            for route in app.routes
            if route.path.startswith("/phase6/extensions")
        }
        expect(
            paths
            == {
                "/phase6/extensions/generations/status",
                "/phase6/extensions/generations",
                "/phase6/extensions/generations/{generation_id}",
                "/phase6/extensions/generations/{generation_id}/integrity",
                "/phase6/extensions/candidates/{candidate_id}/generate",
            },
            "generation exposes exactly five private routes",
            paths,
        )

        payload = command(context)
        candidate_id = context.gated.candidate["candidate_id"]
        unauthorized = client.post(
            f"/phase6/extensions/candidates/{candidate_id}/generate",
            json=payload,
        )
        expect(
            unauthorized.status_code == 401
            and "token" in str(unauthorized.json()).lower()
            and context.generator.calls == 0,
            "generation POST requires explicit control authentication",
            unauthorized.json(),
        )
        invalid = client.post(
            f"/phase6/extensions/candidates/{candidate_id}/generate",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            json={**payload, "source": "print('forbidden')"},
        )
        expect(
            invalid.status_code == 422
            and "forbidden" not in str(invalid.json()).lower()
            and context.generator.calls == 0,
            "HTTP contract rejects caller source without echo",
            invalid.json(),
        )
        created_response = client.post(
            f"/phase6/extensions/candidates/{candidate_id}/generate",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            json=payload,
        )
        created = created_response.json()
        expect(
            created_response.status_code == 200
            and created["generation_status"] == "quarantined"
            and context.generator.calls == 1,
            "authenticated HTTP generation performs one bounded dispatch",
            created,
        )
        expect(
            "source" not in created
            and "source_bytes" not in created
            and "content_b64url" not in created
            and created["authority"]["candidate_execution"] is False
            and created["authority"]["signing"] is False
            and created["authority"]["promotion"] is False,
            "HTTP result is redacted and grants no downstream authority",
            created,
        )

        query = {
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
        }
        headers = {"X-Veyra-Token": CONTROL_TOKEN}
        before = directory_bytes(context.root)
        status = client.get(
            "/phase6/extensions/generations/status"
        )
        listed = client.get(
            "/phase6/extensions/generations",
            headers=headers,
            params=query,
        )
        detail = client.get(
            f"/phase6/extensions/generations/{created['generation_id']}",
            headers=headers,
            params=query,
        )
        integrity = client.get(
            (
                f"/phase6/extensions/generations/"
                f"{created['generation_id']}/integrity"
            ),
            headers=headers,
            params=query,
        )
        after = directory_bytes(context.root)
        expect(
            status.status_code == 200
            and listed.status_code == 200
            and listed.json()["count"] == 1
            and detail.status_code == 200
            and integrity.status_code == 200
            and before == after,
            "all generation GETs are byte-pure private snapshots",
        )
        cross_session = client.get(
            f"/phase6/extensions/generations/{created['generation_id']}",
            headers=headers,
            params={**query, "session_id": "another-session"},
        )
        expect(
            cross_session.status_code == 404,
            "HTTP detail is isolated by initiating session",
            cross_session.json(),
        )
    finally:
        context.close()

    print("Phase 6 extension generation control-plane smoke passed: 7/7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
