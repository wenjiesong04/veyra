from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_pipeline import (  # noqa: E402
    EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION,
    EXTENSION_PIPELINE_START_COMMAND_SCHEMA_VERSION,
)
from routers.phase6_extension_pipelines import (  # noqa: E402
    build_phase6_extension_pipelines_router,
)
from scripts.phase6_extension_pipeline_test_support import (  # noqa: E402
    SESSION,
    TOKEN,
    USER,
    WORKSPACE,
    advance_kwargs,
    build_context,
    expect,
    start_kwargs,
)


async def exercise() -> None:
    context = build_context()
    app = FastAPI()
    app.include_router(
        build_phase6_extension_pipelines_router(
            coordinator=context["coordinator"]
        )
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        unauthorized = await client.get(
            "/phase6/extensions/pipelines/status"
        )
        expect(
            unauthorized.status_code == 401
            and unauthorized.json()["detail"] == "unauthorized",
            "pipeline HTTP status requires the private control token",
            unauthorized.text,
        )

        status = await client.get(
            "/phase6/extensions/pipelines/status", headers=headers
        )
        expect(
            status.status_code == 200
            and status.json()["phase"] == "6.2i-governed-pipeline"
            and status.json()["automatic_approval"] is False
            and not any(status.json()["authority"].values()),
            "pipeline HTTP status exposes explicit-only authority",
            status.text,
        )

        start = start_kwargs()
        start.pop("control_token")
        start["schema_version"] = (
            EXTENSION_PIPELINE_START_COMMAND_SCHEMA_VERSION
        )
        rejected = dict(start)
        rejected["approver_token"] = "PRIVATE_APPROVER_SENTINEL"
        invalid = await client.post(
            "/phase6/extensions/pipelines/start",
            headers=headers,
            json=rejected,
        )
        expect(
            invalid.status_code == 422
            and "PRIVATE_APPROVER_SENTINEL" not in invalid.text,
            "private command validation rejects and never echoes an approver credential",
            invalid.text,
        )

        created_response = await client.post(
            "/phase6/extensions/pipelines/start",
            headers=headers,
            json=start,
        )
        expect(
            created_response.status_code == 200,
            "explicit HTTP pipeline start succeeds",
            created_response.text,
        )
        created = created_response.json()
        expect(
            created["stage"] == "AWAITING_SCOPED_CANARY_APPROVAL"
            and created["status"] == "awaiting_independent_approval"
            and created["last_issue_code"] is None
            and created["pending_review"]["status"] == "pending"
            and created["automatic_approval"] is False
            and created["automatic_promotion"] is False
            and "PRIVATE_CANARY_INPUT" not in created_response.text
            and "PRIVATE_CANARY_OUTPUT" not in created_response.text,
            "HTTP start stops at approval and exposes only digests and receipts",
            created_response.text,
        )

        scoped_review_id = created["pending_review"]["review_id"]
        context["deployment"].approve_outside_coordinator(
            scoped_review_id
        )
        advance = advance_kwargs(
            created,
            operation_id="pipeline-http-scoped-resume",
            review_id=scoped_review_id,
        )
        pipeline_id = advance.pop("pipeline_id")
        advance.pop("control_token")
        advance["schema_version"] = (
            EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION
        )
        advanced_response = await client.post(
            f"/phase6/extensions/pipelines/{pipeline_id}/advance",
            headers=headers,
            json=advance,
        )
        expect(
            advanced_response.status_code == 200,
            "explicit HTTP scoped-canary resume succeeds",
            advanced_response.text,
        )
        advanced = advanced_response.json()
        expect(
            advanced["stage"] == "AWAITING_PROMOTION_APPROVAL"
            and advanced["status"] == "awaiting_independent_approval"
            and advanced["last_issue_code"] is None
            and advanced["pending_review"]["target_mode"] == "promoted",
            "HTTP scoped approval is consumed once and the new promotion review remains cleanly pending",
            advanced_response.text,
        )

        query = {
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
        }
        listed = await client.get(
            "/phase6/extensions/pipelines",
            headers=headers,
            params=query,
        )
        fetched = await client.get(
            f"/phase6/extensions/pipelines/{created['pipeline_id']}",
            headers=headers,
            params=query,
        )
        expect(
            listed.status_code == 200
            and listed.json()["count"] == 1
            and fetched.status_code == 200
            and fetched.json()["pipeline_id"] == created["pipeline_id"]
            and fetched.json()["status"]
            == "awaiting_independent_approval"
            and fetched.json()["last_issue_code"] is None,
            "HTTP list/get return the exact scoped pipeline projection",
        )

        wrong_scope = await client.get(
            "/phase6/extensions/pipelines",
            headers=headers,
            params={**query, "session_id": "other-session"},
        )
        expect(
            wrong_scope.status_code == 200
            and wrong_scope.json()["items"] == [],
            "HTTP pipeline list is session isolated",
        )

    print("Phase 6 governed extension pipeline control-plane smoke passed")


def run() -> None:
    asyncio.run(exercise())


if __name__ == "__main__":
    run()
