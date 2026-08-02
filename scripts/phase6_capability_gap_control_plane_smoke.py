#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.phase6_capability_gaps import (  # noqa: E402
    build_phase6_capability_gaps_router,
)
from runtime.capability_gap_registry import STATE_FILE  # noqa: E402
from scripts.phase6_capability_gap_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    RAW_TEXT,
    SESSION,
    USER,
    WORKSPACE,
    generation_receipt,
    prepare_context,
    record_gap,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> int:
    context = prepare_context()
    try:
        gap = record_gap(context)
        app = FastAPI()
        app.include_router(
            build_phase6_capability_gaps_router(registry=context.registry)
        )
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"authorization": f"Bearer {CONTROL_TOKEN}"}
        query = {
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
        }

        unauthorized = client.get(
            "/phase6/capability-gaps/status", params=query
        )
        expect(
            unauthorized.status_code == 401
            and CONTROL_TOKEN not in unauthorized.text,
            "HTTP status endpoint requires the private control token",
            unauthorized.text,
        )
        status = client.get(
            "/phase6/capability-gaps/status",
            params=query,
            headers=headers,
        )
        listing = client.get(
            "/phase6/capability-gaps",
            params=query,
            headers=headers,
        )
        detail = client.get(
            f"/phase6/capability-gaps/{gap['gap_id']}",
            params=query,
            headers=headers,
        )
        expect(
            status.status_code == 200
            and status.json()["automatic_advancement"] is False
            and listing.status_code == 200
            and listing.json()["count"] == 1
            and detail.status_code == 200
            and detail.json()["raw_user_text_present"] is False,
            "HTTP GET projections expose inert source-free capability gaps",
        )
        expect(
            RAW_TEXT not in json.dumps(detail.json(), ensure_ascii=False),
            "HTTP gap response does not expose raw natural-language text",
        )

        cross_session = client.get(
            f"/phase6/capability-gaps/{gap['gap_id']}",
            params={**query, "session_id": "different-session"},
            headers=headers,
        )
        expect(
            cross_session.status_code == 404,
            "HTTP cross-session detail is indistinguishable from missing",
            cross_session.text,
        )
        invalid = client.post(
            f"/phase6/capability-gaps/{gap['gap_id']}/spec-link",
            headers=headers,
            json={
                "schema_version": (
                    "veyra.phase6.capability_gap_spec_link_command.v1"
                ),
                "operation_id": "http-link-invalid",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "candidate_id": context.candidate["candidate_id"],
                "expected_gap_revision": 1,
                "expected_candidate_revision": context.candidate[
                    "candidate_revision"
                ],
                "expected_spec_digest": context.spec_digest,
                "raw_text": RAW_TEXT,
            },
        )
        expect(
            invalid.status_code == 422
            and RAW_TEXT not in invalid.text
            and "raw_text" not in invalid.text,
            "private request validation does not echo rejected input",
            invalid.text,
        )

        linked = client.post(
            f"/phase6/capability-gaps/{gap['gap_id']}/spec-link",
            headers=headers,
            json={
                "schema_version": (
                    "veyra.phase6.capability_gap_spec_link_command.v1"
                ),
                "operation_id": "http-link-spec",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "candidate_id": context.candidate["candidate_id"],
                "expected_gap_revision": 1,
                "expected_candidate_revision": context.candidate[
                    "candidate_revision"
                ],
                "expected_spec_digest": context.spec_digest,
            },
        )
        expect(
            linked.status_code == 200
            and linked.json()["stage"] == "SPEC_LINKED_REVIEW_REQUIRED"
            and linked.json()["authority"]["code_generation"] is False,
            "HTTP spec link observes exact quarantine evidence only",
            linked.text,
        )
        receipt = generation_receipt(context)
        observed = client.post(
            f"/phase6/capability-gaps/{gap['gap_id']}/observations",
            headers=headers,
            json={
                "schema_version": (
                    "veyra.phase6.capability_gap_observation_command.v1"
                ),
                "operation_id": "http-observe-generation",
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "expected_gap_revision": 2,
                "receipt": receipt,
            },
        )
        expect(
            observed.status_code == 200
            and observed.json()["gap_revision"] == 3
            and observed.json()["stage"] == "SPEC_LINKED_REVIEW_REQUIRED"
            and observed.json()["authority"]["execution"] is False,
            "HTTP lifecycle observation never advances governance authority",
            observed.text,
        )

        state_path = context.store.path_for(STATE_FILE)
        before = state_path.read_bytes()
        for path in (
            "/phase6/capability-gaps/status",
            "/phase6/capability-gaps",
            f"/phase6/capability-gaps/{gap['gap_id']}",
            f"/phase6/capability-gaps/{gap['gap_id']}/timeline",
        ):
            response = client.get(path, params=query, headers=headers)
            expect(response.status_code == 200, f"GET {path} succeeds")
        after = state_path.read_bytes()
        expect(
            before == after,
            "HTTP status list detail and timeline preserve registry bytes",
        )
    finally:
        context.close()
    print("Phase 6 capability-gap control-plane smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
