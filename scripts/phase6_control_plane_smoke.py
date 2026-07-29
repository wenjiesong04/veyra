#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.phase6 import build_phase6_router  # noqa: E402
from runtime.agent_capability_directory import (  # noqa: E402
    AgentCapabilitySelectionError,
)
from runtime.read_only_agent_collaboration import (  # noqa: E402
    CollaborationNotFoundError,
)


WORKSPACE = "/tmp/veyra-phase6-control"
USER = "phase6-control-user"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.revision = 7

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": "veyra.phase6.status.v1",
            "phase": "6.1",
            "status": "technical_complete_read_only",
            "mode": "shadow_proposal_only",
            "topology": "single_runtime_multi_participant",
            "authority": {
                "tools": [],
                "execution_authorized": False,
            },
        }

    def start(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", kwargs))
        if kwargs.get("runtime") != "openclaw":
            raise AgentCapabilitySelectionError(
                "runtime_is_diagnostic_only"
            )
        return self._public("PROPOSED")

    def list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        return {
            "schema_version": "veyra.phase6.collaboration_list.v1",
            "count": 1,
            "collaborations": [self._public("PROPOSED")],
        }

    def get(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        if kwargs.get("user_id") != USER:
            raise CollaborationNotFoundError("not found")
        return self._public("PROPOSED")

    def advance(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("advance", kwargs))
        return self._public("PROPOSED")

    def select_plan(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("select", kwargs))
        self.revision += 1
        value = self._public("CLOSED")
        value["selection"] = {
            "selected_option_id": kwargs["selected_option_id"],
            "execution_authorized": False,
        }
        return value

    def cancel(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("cancel", kwargs))
        self.revision += 1
        return self._public("CANCELLED")

    def _public(self, status: str) -> dict[str, Any]:
        return {
            "schema_version": "veyra.phase6.collaboration.v1",
            "case_id": "case_phase6_control",
            "status": status,
            "case_revision": self.revision,
            "runtime": "openclaw",
            "topology": "single_runtime_multi_participant",
            "execution_profile": (
                "phase6_read_only_collaboration.v1"
            ),
            "proposal_only": True,
            "effect_status": "not_started",
            "verification_status": "unverified",
            "execution_authorized": False,
            "provider_switch_allowed": False,
            "participants": [],
            "budgets": {},
            "selection": None,
            "issue": None,
        }


def main() -> int:
    runtime = FakeRuntime()
    app = FastAPI()
    app.include_router(build_phase6_router(collaboration=runtime))  # type: ignore[arg-type]
    client = TestClient(app)

    status = client.get("/phase6/status")
    expect(
        status.status_code == 200
        and status.json()["status"]
        == "technical_complete_read_only"
        and status.json()["authority"]["tools"] == [],
        "status exposes fixed read-only authority",
        status.json(),
    )

    start_body = {
        "schema_version": "veyra.phase6.collaboration_start.v1",
        "event_id": "phase6_control_event",
        "operation_id": "phase6_control_start",
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": "phase6_control_session",
        "channel": "api",
        "runtime": "openclaw",
        "user_goal": "Compare two safe read-only options.",
        "context_summary": "No authoritative evidence is available.",
        "evidence_refs": [],
    }
    started = client.post("/phase6/collaborations", json=start_body)
    expect(
        started.status_code == 200
        and started.json()["status"] == "PROPOSED"
        and started.json()["effect_status"] == "not_started"
        and started.json()["execution_authorized"] is False,
        "start returns only the public proposal projection",
        started.json(),
    )
    start_call = next(
        kwargs for name, kwargs in runtime.calls if name == "start"
    )
    expect(
        start_call["event"].source.user_id == USER
        and start_call["workspace_id"] == WORKSPACE
        and start_call["operation_id"] == "phase6_control_start",
        "HTTP start binds exact owner, workspace, and operation",
        start_call,
    )

    extra = client.post(
        "/phase6/collaborations",
        json={**start_body, "provider_switch": True},
    )
    expect(
        extra.status_code == 422,
        "unknown control fields fail strict validation",
        extra.json(),
    )
    wrong_runtime = client.post(
        "/phase6/collaborations",
        json={
            **start_body,
            "event_id": "phase6_control_other_runtime",
            "operation_id": "phase6_control_other_runtime",
            "runtime": "custom",
        },
    )
    expect(
        wrong_runtime.status_code == 409
        and "diagnostic_only" in str(wrong_runtime.json()),
        "non-eligible provider fails without fallback",
        wrong_runtime.json(),
    )

    listed = client.get(
        "/phase6/collaborations",
        params={"user_id": USER, "workspace_id": WORKSPACE},
    )
    fetched = client.get(
        "/phase6/collaborations/case_phase6_control",
        params={"user_id": USER, "workspace_id": WORKSPACE},
    )
    wrong_owner = client.get(
        "/phase6/collaborations/case_phase6_control",
        params={
            "user_id": "another-user",
            "workspace_id": WORKSPACE,
        },
    )
    expect(
        listed.status_code == fetched.status_code == 200
        and wrong_owner.status_code == 404,
        "list/get remain owner scoped",
        {
            "list": listed.json(),
            "get": fetched.json(),
            "wrong_owner": wrong_owner.json(),
        },
    )

    advanced = client.post(
        "/phase6/collaborations/case_phase6_control/advance",
        json={
            "schema_version": (
                "veyra.phase6.collaboration_advance.v1"
            ),
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "operation_id": "phase6_control_advance",
        },
    )
    selected = client.post(
        "/phase6/collaborations/case_phase6_control/select",
        json={
            "schema_version": (
                "veyra.phase6.plan_selection_command.v1"
            ),
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_revision": 7,
            "operation_id": "phase6_control_select",
            "selected_option_id": "conservative",
            "decision_reason": (
                "Prefer the proposal that preserves uncertainty."
            ),
        },
    )
    cancelled = client.post(
        "/phase6/collaborations/case_phase6_control/cancel",
        json={
            "schema_version": (
                "veyra.phase6.collaboration_cancel.v1"
            ),
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_revision": 8,
            "operation_id": "phase6_control_cancel",
            "reason": "stop after control-plane validation",
        },
    )
    expect(
        advanced.status_code == 200
        and selected.status_code == 200
        and selected.json()["selection"]["execution_authorized"]
        is False
        and cancelled.status_code == 200
        and cancelled.json()["status"] == "CANCELLED",
        "advance/select/cancel commands stay explicit and non-authorizing",
        {
            "advance": advanced.json(),
            "select": selected.json(),
            "cancel": cancelled.json(),
        },
    )
    select_call = next(
        kwargs for name, kwargs in runtime.calls if name == "select"
    )
    expect(
        select_call["decision_reason"]
        == "Prefer the proposal that preserves uncertainty.",
        "selection reason reaches the exact runtime command",
        select_call,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
