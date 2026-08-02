from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from fastapi import FastAPI

from interface.extension_deployment import canonical_utc
from routers.phase6_extension_deployments import (
    build_phase6_extension_deployments_router,
)
from scripts.phase6_extension_deployment_test_support import (
    ATTESTATION_DIGEST,
    APPROVER_TOKEN,
    BASE_TIME,
    OTHER_SESSION,
    RELEASE_ID,
    SESSION,
    TOKEN,
    USER,
    WORKSPACE,
    build_gate,
    expect,
)


async def exercise() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-phase6-deployment-http-") as tmp:
        gate, _release, runner, _clock = build_gate(Path(tmp))
        app = FastAPI()
        app.include_router(build_phase6_extension_deployments_router(gate=gate))
        transport = httpx.ASGITransport(app=app)
        headers = {"authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            unauthenticated = await client.get(
                "/phase6/extensions/deployments/status"
            )
            expect(
                unauthenticated.status_code == 401,
                "deployment status requires control token",
                unauthenticated.text,
            )
            status = await client.get(
                "/phase6/extensions/deployments/status", headers=headers
            )
            expect(
                status.status_code == 200
                and status.json()["default_mode"] == "record_only"
                and not any(status.json()["authority"].values()),
                "deployment status exposes fail-closed default and no authority",
                status.text,
            )
            invalid = {
                "schema_version": "veyra.phase6.extension_deployment_proposal_command.v1",
                "operation_id": "http-invalid",
                "request_id": "PRIVATE_DEPLOYMENT_SENTINEL",
                "release_id": RELEASE_ID,
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "expected_state_revision": False,
                "expected_release_revision": 1,
                "expected_attestation_digest": ATTESTATION_DIGEST,
                "expires_at": canonical_utc(BASE_TIME + timedelta(days=2)),
            }
            rejected = await client.post(
                "/phase6/extensions/deployments/proposals",
                headers=headers,
                json=invalid,
            )
            expect(
                rejected.status_code == 422
                and "PRIVATE_DEPLOYMENT_SENTINEL" not in rejected.text,
                "deployment command rejects coercion without echoing input",
                rejected.text,
            )
            proposal = dict(invalid)
            proposal["operation_id"] = "http-proposal"
            proposal["request_id"] = "http-request"
            proposal["expected_state_revision"] = 0
            created_response = await client.post(
                "/phase6/extensions/deployments/proposals",
                headers=headers,
                json=proposal,
            )
            expect(
                created_response.status_code == 200,
                "record_only deployment proposal succeeds",
                created_response.text,
            )
            created = created_response.json()
            expect(
                created["mode"] == "record_only"
                and runner.run_calls == 0
                and "source_bytes" not in created_response.text,
                "proposal executes nothing and exposes no source",
            )
            query = {
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
            }
            listed = await client.get(
                "/phase6/extensions/deployments",
                headers=headers,
                params=query,
            )
            expect(
                listed.status_code == 200 and len(listed.json()["items"]) == 1,
                "owner-scoped deployment list returns proposal",
            )
            wrong_scope = await client.get(
                "/phase6/extensions/deployments",
                headers=headers,
                params={**query, "session_id": OTHER_SESSION},
            )
            expect(
                wrong_scope.status_code == 200
                and wrong_scope.json()["items"] == [],
                "HTTP deployment list is session isolated",
            )
            registry = await client.get(
                "/phase6/extensions/public-extension-registry",
                headers=headers,
                params=query,
            )
            expect(
                registry.status_code == 200 and registry.json()["items"] == [],
                "record_only proposal is absent from public HTTP registry",
            )
            transition = {
                "schema_version": "veyra.phase6.extension_deployment_transition_command.v1",
                "operation_id": "http-shadow",
                "request_id": "http-shadow-request",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "target_mode": "shadow",
                "expected_state_revision": 1,
                "expected_deployment_revision": created["revision"],
                "expected_mode_epoch": created["mode_epoch"],
                "max_invocations": 2,
                "expires_at": canonical_utc(BASE_TIME + timedelta(days=2)),
                "review_id": None,
            }
            shadow_response = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/transitions",
                headers=headers,
                json=transition,
            )
            expect(
                shadow_response.status_code == 200
                and shadow_response.json()["mode"] == "shadow",
                "explicit HTTP transition enters shadow",
                shadow_response.text,
            )
            shadow = shadow_response.json()
            invocation = {
                "schema_version": "veyra.phase6.extension_invocation_command.v1",
                "operation_id": "http-shadow-invoke",
                "request_id": "http-shadow-invoke-request",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "expected_state_revision": 2,
                "expected_deployment_revision": shadow["revision"],
                "expected_mode_epoch": shadow["mode_epoch"],
                "input_payload": {"name": "PRIVATE_SHADOW_OUTPUT"},
            }
            invoked = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/invocations",
                headers=headers,
                json=invocation,
            )
            expect(
                invoked.status_code == 200
                and invoked.json()["result"]["invocation_status"] == "discarded"
                and invoked.json()["result"]["output_payload"] is None,
                "HTTP shadow invocation discards output",
                invoked.text,
            )
            durable = gate.state_store.path_for(
                "phase6_extension_deployment_state.json"
            ).read_text(encoding="utf-8")
            expect(
                "PRIVATE_SHADOW_OUTPUT" not in durable,
                "HTTP invocation persists no input or raw output",
            )
            serialized = json.dumps(invoked.json(), ensure_ascii=False)
            expect(
                "source_bytes" not in serialized
                and not any(invoked.json()["authority"].values()),
                "HTTP invocation response exposes no source or ambient authority",
            )

            shadow_done = invoked.json()["deployment"]
            transition["operation_id"] = "http-read-only"
            transition["request_id"] = "http-read-only-request"
            transition["target_mode"] = "read_only_canary"
            transition["expected_state_revision"] = invoked.json()["state_revision"]
            transition["expected_deployment_revision"] = shadow_done["revision"]
            transition["expected_mode_epoch"] = shadow_done["mode_epoch"]
            read_transition = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/transitions",
                headers=headers,
                json=transition,
            )
            expect(read_transition.status_code == 200, "HTTP enters read-only canary")
            read = read_transition.json()
            invocation["operation_id"] = "http-read-invoke"
            invocation["request_id"] = "http-read-invoke-request"
            invocation["expected_state_revision"] = 5
            invocation["expected_deployment_revision"] = read["revision"]
            invocation["expected_mode_epoch"] = read["mode_epoch"]
            invocation["input_payload"] = {"name": "review-ready"}
            read_invoked = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/invocations",
                headers=headers,
                json=invocation,
            )
            expect(read_invoked.status_code == 200, "read-only canary receipt succeeds")
            read_done = read_invoked.json()["deployment"]
            review_request = {
                "schema_version": "veyra.phase6.extension_deployment_review_request_command.v1",
                "operation_id": "http-request-scoped-review",
                "request_id": "http-scoped-review-request",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "target_mode": "scoped_canary",
                "expected_state_revision": read_invoked.json()["state_revision"],
                "expected_deployment_revision": read_done["revision"],
                "expected_mode_epoch": read_done["mode_epoch"],
            }
            review_response = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/reviews",
                headers=headers,
                json=review_request,
            )
            expect(
                review_response.status_code == 200
                and review_response.json()["status"] == "pending",
                "control principal can request but not approve scoped review",
                review_response.text,
            )
            review = review_response.json()
            approval_payload = {
                "schema_version": "veyra.phase6.extension_deployment_review_approval_command.v1",
                "expected_review_revision": review["review_revision"],
                "reason_digest": "a" * 64,
            }
            same_principal = await client.post(
                f"/phase6/extensions/deployment-reviews/{review['review_id']}/approve",
                headers={
                    **headers,
                    "x-veyra-extension-approver-token": TOKEN,
                },
                json=approval_payload,
            )
            expect(
                same_principal.status_code == 401,
                "control token cannot impersonate extension approver",
            )
            approved_response = await client.post(
                f"/phase6/extensions/deployment-reviews/{review['review_id']}/approve",
                headers={
                    "x-veyra-extension-approver-token": APPROVER_TOKEN
                },
                json=approval_payload,
            )
            expect(
                approved_response.status_code == 200
                and approved_response.json()["status"] == "approved"
                and approved_response.json()["raw_approver_credential_persisted"]
                is False,
                "distinct approver credential creates identified receipt",
                approved_response.text,
            )
            review_state = gate.state_store.read_json("review_queue.json")
            expect(
                APPROVER_TOKEN not in json.dumps(review_state, ensure_ascii=False)
                and APPROVER_TOKEN not in approved_response.text,
                "raw approver credential is never persisted or returned",
            )
            transition["operation_id"] = "http-scoped"
            transition["request_id"] = "http-scoped-request"
            transition["target_mode"] = "scoped_canary"
            transition["expected_state_revision"] = read_invoked.json()["state_revision"]
            transition["expected_deployment_revision"] = read_done["revision"]
            transition["expected_mode_epoch"] = read_done["mode_epoch"]
            transition["review_id"] = review["review_id"]
            scoped_response = await client.post(
                f"/phase6/extensions/deployments/{created['deployment_id']}/transitions",
                headers=headers,
                json=transition,
            )
            expect(
                scoped_response.status_code == 200
                and scoped_response.json()["mode"] == "scoped_canary",
                "identified ReviewQueue receipt unlocks exact scoped transition",
                scoped_response.text,
            )

    print("Phase 6 extension deployment control-plane smoke passed")


def main() -> None:
    asyncio.run(exercise())


if __name__ == "__main__":
    main()
