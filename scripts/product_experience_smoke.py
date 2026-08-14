#!/usr/bin/env python3
"""Focused contract smoke for the local Product Preview read models.

This deliberately uses an isolated temporary state root.  It proves the
product boundary without touching the running local runtime or its state
writer, and it keeps the assertions at the user-facing contract level.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.goal_store_policy import (
    WORKSPACE_GOAL_KIND,
    WORKSPACE_GOAL_SCHEMA,
    WORKSPACE_GOAL_SOURCE,
)
from core.world_state import WorldStateStore
from interface.session_mapper import SessionMapper
from routers.product import build_product_router
from runtime.product_experience import LOCAL_EXTERNAL_SESSION, ProductExperienceService
from runtime.product_experience_projection import readiness_projection


SENSITIVE_VALUES = {
    "/private/veyra/workspace-secret",
    "control-token-should-never-leak",
    "state/path/should-stay-private.json",
    "other-owner-only-task",
}


def _goal(user_id: str, external_session: str, title: str) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_GOAL_SCHEMA,
        "kind": WORKSPACE_GOAL_KIND,
        "source": WORKSPACE_GOAL_SOURCE,
        "status": "active",
        "user_id": user_id,
        "session_id": SessionMapper().map("api", user_id, external_session),
        "title": title,
        "description": "A local product preview focus.",
        "priority": 0.9,
        "workspace_path": "/private/veyra/workspace-secret",
        "control_token": "control-token-should-never-leak",
        "state_path": "state/path/should-stay-private.json",
    }


def _assert_no_sensitive_values(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    leaked = [needle for needle in SENSITIVE_VALUES if needle in encoded]
    assert not leaked, f"product projection leaked private values: {leaked}"


def _assert_authority_disabled(value: dict[str, Any]) -> None:
    authority = value.get("authority")
    assert isinstance(authority, dict), "authority summary is required"
    assert all(item is False for item in authority.values()), authority


def _state_bytes(state: WorldStateStore) -> dict[str, bytes]:
    return {
        str(path.relative_to(state.root)): path.read_bytes()
        for path in state.root.rglob("*")
        if path.is_file() and path.name != ".veyra-writer.lock"
    }


def _assert_matter_sections(value: dict[str, Any]) -> None:
    sections = value.get("sections")
    assert isinstance(sections, dict)
    for name, section in sections.items():
        assert set(section) == {"status", "count", "items"}, (name, section)
        assert isinstance(section["count"], int)
        assert isinstance(section["items"], list)


def main() -> int:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    product_block = main_source.split("product_experience = ProductExperienceService(", 1)[1].split("class _DynamicDeps", 1)[0]
    assert "snapshot(read_only=True)" in product_block
    assert "connection_status()" not in product_block
    with TemporaryDirectory(prefix="veyra-product-smoke-") as temporary:
        state = WorldStateStore(Path(temporary))
        service = ProductExperienceService(state)
        user = "owner-a"
        external = "veyra-workspace-primary-v1"
        internal = SessionMapper().map("api", user, external)
        other_user = "owner-b"
        other_external = "external-b"

        state.write_json(
            "user_goals.json",
            {
                "goals": [_goal(user, external, "Local product focus")],
                "updated_at": "2026-08-14T00:00:00+00:00",
            },
        )
        state.write_json(
            "task_state.json",
            {"pending_agent_tasks": [{"title": "other-owner-only-task"}]},
        )
        state.write_json(
            "user_commitments.json",
            {
                "commitments": [
                    {"title": "Owner A commitment", "status": "open", "user_id": user, "session_id": internal},
                    {"title": "other-owner-only-task", "status": "open", "user_id": other_user, "session_id": SessionMapper().map("api", other_user, other_external)},
                ]
            },
        )

        routes = {route.path for route in build_product_router(service=service).routes}
        assert routes == {"/product/context", "/product/today", "/product/matters", "/product/status"}, routes
        app = FastAPI()
        app.include_router(build_product_router(service=service))
        client = TestClient(app)
        for endpoint, schema in {
            "/product/context": "veyra.product_context.v1",
            "/product/today": "veyra.product_today.v1",
            "/product/matters": "veyra.product_matters.v1",
            "/product/status": "veyra.product_status.v1",
        }.items():
            response = client.get(endpoint)
            assert response.status_code == 200, (endpoint, response.status_code)
            assert response.json().get("schema_version") == schema, response.json()
        assert client.get("/product/today?user_id=owner-a").status_code == 422
        assert client.get("/product/context?session_id=one&external_session_id=two").status_code == 422
        assert client.get("/product/context?session_id=one&external_session_id=one").status_code == 200

        context = service.context()
        assert context["status"] == "ready"
        assert context["internal_read_scope"] == {"user_id": user, "session_id": internal}
        assert context["external_input_scope"] == {"channel": "api", "user_id": user, "session_id": external}
        _assert_authority_disabled(context)
        _assert_no_sensitive_values(context)

        mismatch = service.context(user_id=user, external_session_id="wrong-session")
        assert mismatch["status"] == "needs_session_link"
        assert mismatch["internal_read_scope"] is None
        assert mismatch["goal"] is None

        today = service.today(user_id=user, session_id=internal)
        assert today["status"] in {"success", "empty", "degraded"}
        assert today["questions"]["status"] == "unsupported"
        assert all(item.get("delivery") == "none" for item in today["suggestions"])
        for item in today["suggestions"]:
            _assert_authority_disabled(item)
        _assert_authority_disabled(today)
        _assert_no_sensitive_values(today)

        matters = service.matters(user_id=user, session_id=internal)
        commitments = matters["sections"]["commitments"]["items"]
        assert [item["title"] for item in commitments] == ["Owner A commitment"]
        _assert_matter_sections(matters)
        _assert_authority_disabled(matters)
        _assert_no_sensitive_values(matters)

        corrupt_commitments = ProductExperienceService(state)
        state.write_json("user_commitments.json", {"_state_corrupt": True, "commitments": []})
        corrupt_matters = corrupt_commitments.matters(user_id=user, session_id=internal)
        assert corrupt_matters["sections"]["commitments"]["status"] == "fail_closed"

        status = service.status()
        assert status["evidence"] == {
            "implementation": "implemented",
            "configuration": "configuration_pending",
            "automated": "validation_pending",
            "live": "validation_pending",
            "production": "not_in_scope",
        }
        _assert_authority_disabled(status)
        _assert_no_sensitive_values(status)

        # Every product GET is read-only, including the selected-agent status
        # projection.  The callback receives a cached capability snapshot and
        # can never fall through to a live connection probe.
        snapshot_read_only: list[bool] = []

        def capability_snapshot(*, read_only: bool) -> dict[str, Any]:
            snapshot_read_only.append(read_only)
            return {
                "selected_runtime": "openclaw",
                "runtimes": [{
                    "runtime": "openclaw",
                    "operator_selected": True,
                    "status": "not_configured",
                    "configured": True,
                    "base_url": "http://127.0.0.1:18789",
                    "connected": False,
                }],
            }

        pure_service = ProductExperienceService(
            state,
            agent_status_resolver=lambda: capability_snapshot(read_only=True),
            runtime_build_resolver=lambda: {"status": "available", "revision": "abc123456789"},
        )
        before = _state_bytes(state)
        pure_status: dict[str, Any] | None = None
        for endpoint in ("/product/context", "/product/today", "/product/matters", "/product/status"):
            if endpoint == "/product/status":
                pure_status = pure_service.status()
            elif endpoint == "/product/context":
                pure_service.context(user_id=user, external_session_id=external)
            elif endpoint == "/product/today":
                pure_service.today(user_id=user, session_id=internal)
            else:
                pure_service.matters(user_id=user, session_id=internal)
        assert _state_bytes(state) == before
        assert snapshot_read_only == [True]
        assert pure_status is not None
        readiness = pure_status["runtime"]["agent"]
        assert readiness["status"] == "not_configured" and readiness["configured"] is False
        assert readiness_projection({"status": "unknown", "base_url": "http://localhost"})["configured"] is False

        # Product previews are hidden as soon as the suggestion mode leaves
        # record_only; this must hold even when a durable outbox is present.
        state.write_json("ops_config.json", {"general_suggestions": {"mode": "disabled", "mode_epoch": 1}})
        assert service.suggestion_outbox.list_preview(user_id=user, session_id=internal)["items"] == []

        # Waiting rows only bind to current situations.  Malformed source
        # records degrade the whole projection without leaking raw payloads.
        service.attention_hypotheses.list_for_owner = lambda **_: {"status": "fail_closed", "items": []}
        attention_closed = service.today(user_id=user, session_id=internal)
        assert attention_closed["section_statuses"]["attention"] == "fail_closed"
        current_id, expired_id = "s-current", "s-expired"
        service.general_situations.list_for_owner = lambda **_: {"status": "success", "items": [
            {"general_situation_id": current_id, "status": "open", "distinct_event_count": 2},
            {"general_situation_id": expired_id, "status": "open", "distinct_event_count": 1, "expires_at": "2020-01-01T00:00:00+00:00"},
        ]}
        service.attention_hypotheses.list_for_owner = lambda **_: {"status": "success", "items": []}
        service.suggestion_outbox.list_preview = lambda **_: {"status": "success", "items": []}
        service.suggestion_outbox.list_decisions = lambda **_: {"status": "success", "items": [
            {"general_situation_id": current_id, "decision_disposition": "wait"},
            {"general_situation_id": expired_id, "decision_disposition": "wait"},
        ]}
        projection = service.today(user_id=user, session_id=internal)
        assert len(projection["waiting"]) == 1
        assert projection["waiting"][0]["status"] == "waiting"
        service.general_situations.list_for_owner = lambda **_: {"status": "success", "items": [
            {"general_situation_id": "malformed", "status": "open", "distinct_event_count": {"secret": "value"}},
        ]}
        malformed = service.today(user_id=user, session_id=internal)
        assert malformed["status"] == "degraded"
        assert malformed["situations"] == []
        _assert_no_sensitive_values(malformed)

        # An explicit user with no Goal gets a fresh local scope and never
        # borrows the active Goal belonging to another owner.
        state.write_json("user_goals.json", {"goals": [_goal(other_user, other_external, "Other owner")], "updated_at": None})
        empty = service.context(user_id=user, external_session_id=external)
        assert empty["status"] == "empty"
        assert empty["goal"] is None
        assert empty["internal_read_scope"] == {"user_id": user, "session_id": internal}
        _assert_no_sensitive_values(empty)
        empty_default = service.context(user_id=user)
        assert empty_default["external_input_scope"]["session_id"] == LOCAL_EXTERNAL_SESSION

        # Without an explicit owner, more than one active workspace Goal is an
        # ambiguity rather than permission to merge scopes.
        state.write_json("user_goals.json", {"goals": [_goal(user, external, "Owner A"), _goal(other_user, other_external, "Owner B")], "updated_at": None})
        ambiguous = service.context()
        assert ambiguous["status"] == "ambiguous"
        assert ambiguous["internal_read_scope"] is None
        assert ambiguous["external_input_scope"] is None

        # Duplicate active Goals for the same exact owner/session are also
        # ambiguous; Today and Matters must not merge or guess a winner.
        state.write_json("user_goals.json", {"goals": [_goal(user, external, "Owner A1"), _goal(user, external, "Owner A2")], "updated_at": None})
        duplicate_today = service.today(user_id=user, session_id=internal)
        duplicate_matters = service.matters(user_id=user, session_id=internal)
        assert duplicate_today["status"] == duplicate_matters["status"] == "ambiguous"
        _assert_matter_sections(duplicate_matters)
        state.write_json("user_goals.json", {"goals": [_goal(user, external, "Owner A")], "updated_at": None})
        mismatch_today = service.today(user_id=user, session_id="different-internal-session")
        assert mismatch_today["status"] == "needs_session_link"

    print("product experience smoke passed (scope, privacy, preview, status, router)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
