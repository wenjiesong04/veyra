from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from routers.phase6_extension_releases import (  # noqa: E402
    build_phase6_extension_releases_router,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    SOURCE,
    USER,
    WORKSPACE,
)
from scripts.phase6_extension_release_contract_smoke import (  # noqa: E402
    CONTROL_TOKEN,
    SESSION,
    build_registry,
    create_kwargs,
    dynamic_report,
    expect,
    snapshot_bytes,
)


def create_payload(registry: Any) -> dict[str, Any]:
    values = create_kwargs(registry)
    return {
        "schema_version": "veyra.phase6.extension_release_create_command.v1",
        "operation_id": values["operation_id"],
        "generation_id": values["generation_id"],
        "user_id": values["user_id"],
        "workspace_id": values["workspace_id"],
        "session_id": values["session_id"],
        "expected_registry_revision": values["expected_registry_revision"],
        "expected_generation_report_digest": values[
            "expected_generation_report_digest"
        ],
        "expected_validation_report_digest": values[
            "expected_validation_report_digest"
        ],
        "expected_generator_identity_digest": values[
            "expected_generator_identity_digest"
        ],
        "expected_verifier_identity_digest": values[
            "expected_verifier_identity_digest"
        ],
        "expected_signing_identity_digest": values[
            "expected_signing_identity_digest"
        ],
        "expires_at": values["expires_at"],
    }


async def exercise() -> None:
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        registry, _, _ = build_registry(Path(state_dir), Path(key_dir))
        app = FastAPI()
        app.include_router(
            build_phase6_extension_releases_router(registry=registry)
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"authorization": f"Bearer {CONTROL_TOKEN}"}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            unauthenticated = await client.get(
                "/phase6/extensions/signed-releases/status"
            )
            expect(
                unauthenticated.status_code == 401,
                "signed-release status GET is token authenticated",
                unauthenticated.text,
            )
            status = await client.get(
                "/phase6/extensions/signed-releases/status",
                headers=headers,
            )
            expect(
                status.status_code == 200
                and status.json()["authority"]["installation"] is False
                and status.json()["authority"]["candidate_execution"] is False,
                "control-plane status exposes the complete no-authority contract",
                status.text,
            )
            payload = create_payload(registry)
            validation_id = dynamic_report().binding.validation_id
            invalid = dict(payload)
            invalid["expected_registry_revision"] = False
            invalid["workspace_id"] = "PRIVATE_ECHO_SENTINEL"
            rejected = await client.post(
                f"/phase6/extensions/dynamic-validations/{validation_id}/signed-release",
                headers=headers,
                json=invalid,
            )
            expect(
                rejected.status_code == 422
                and "PRIVATE_ECHO_SENTINEL" not in rejected.text,
                "private route rejects coercion without echoing request fields",
                rejected.text,
            )
            created_response = await client.post(
                f"/phase6/extensions/dynamic-validations/{validation_id}/signed-release",
                headers=headers,
                json=payload,
            )
            expect(
                created_response.status_code == 200,
                "exact token-authenticated release command succeeds",
                created_response.text,
            )
            created = created_response.json()
            serialized = json.dumps(created, ensure_ascii=False, sort_keys=True)
            expect(
                SOURCE.decode("utf-8") not in serialized
                and "BEGIN PRIVATE KEY" not in serialized
                and "PRIVATE KEY-----" not in serialized
                and created["private_key_material_in_response"] is False
                and not any(created["authority"].values()),
                "HTTP create response is source/private-key-free and grants no authority",
            )
            query = {
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
            }
            before_gets = snapshot_bytes(registry.state_store)
            listed = await client.get(
                "/phase6/extensions/signed-releases",
                headers=headers,
                params=query,
            )
            fetched = await client.get(
                f"/phase6/extensions/signed-releases/{created['release_id']}",
                headers=headers,
                params=query,
            )
            integrity = await client.get(
                f"/phase6/extensions/signed-releases/{created['release_id']}/integrity",
                headers=headers,
                params=query,
            )
            expect(
                listed.status_code == fetched.status_code
                == integrity.status_code
                == 200
                and before_gets == snapshot_bytes(registry.state_store),
                "HTTP list/detail/integrity GETs are byte-invariant",
            )
            get_serialized = listed.text + fetched.text + integrity.text
            expect(
                SOURCE.decode("utf-8") not in get_serialized
                and "source_bytes" not in get_serialized
                and integrity.json()["signature_verification_status"]
                == "verified",
                "public GETs never expose deployment source and preserve signature status",
            )
            wrong_token = await client.get(
                f"/phase6/extensions/signed-releases/{created['release_id']}",
                headers={"x-veyra-token": "wrong-token"},
                params=query,
            )
            expect(
                wrong_token.status_code == 401,
                "public release detail still requires the exact local principal token",
            )
            revoke_payload = {
                "schema_version": (
                    "veyra.phase6.extension_release_revoke_command.v1"
                ),
                "operation_id": "release-http-revoke",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "expected_release_revision": 1,
                "expected_registry_revision": 1,
                "reason_digest": "c" * 64,
            }
            revoked = await client.post(
                f"/phase6/extensions/signed-releases/{created['release_id']}/revoke",
                headers=headers,
                json=revoke_payload,
            )
            expect(
                revoked.status_code == 200
                and revoked.json()["effective_status"] == "RELEASE_REVOKED"
                and not any(revoked.json()["authority"].values()),
                "HTTP revocation is explicit CAS-bound and grants no authority",
                revoked.text,
            )

        paths = {route.path for route in app.routes}
        expect(
            not any("deployment" in path for path in paths),
            "private deployment_subject is not exposed as an HTTP route",
            sorted(paths),
        )

    print("Phase 6 extension release control-plane smoke passed")


def run() -> None:
    asyncio.run(exercise())


if __name__ == "__main__":
    run()
