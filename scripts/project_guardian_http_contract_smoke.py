#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.debug_audit import build_debug_audit_router


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


class ProducerStub:
    def __init__(self) -> None:
        self.register_calls = 0
        self.intent_calls = 0

    def status(self) -> dict[str, Any]:
        return {"status": "success"}

    def public_run_once(self, *, reason: str) -> dict[str, Any]:
        time.sleep(0.3)
        return {"status": "success", "reason": reason}

    def register_release_goal(self, **kwargs: Any) -> dict[str, Any]:
        self.register_calls += 1
        return kwargs

    def record_deployment_intent(self, **kwargs: Any) -> dict[str, Any]:
        self.intent_calls += 1
        return kwargs


class AttentionStub:
    def __init__(self) -> None:
        self.policy_calls = 0
        self.dismissal_calls = 0

    def set_policy(self, **kwargs: Any) -> dict[str, Any]:
        self.policy_calls += 1
        return kwargs

    def set_dismissal(self, **kwargs: Any) -> dict[str, Any]:
        self.dismissal_calls += 1
        return kwargs


async def run_contract() -> None:
    producer = ProducerStub()
    attention = AttentionStub()
    app = FastAPI()
    app.include_router(
        build_debug_audit_router(
            {
                "project_guardian_producers": producer,
                "project_guardian_attention": attention,
            }
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://veyra.test",
    ) as client:
        slow = asyncio.create_task(
            client.post(
                "/awareness/project-guardian/producers/run-once"
            )
        )
        await asyncio.sleep(0.04)
        started = time.monotonic()
        status_response = await client.get(
            "/awareness/project-guardian/producers/status"
        )
        status_elapsed = time.monotonic() - started
        slow_was_running = not slow.done()
        slow_response = await slow
        expect(
            status_response.status_code == 200
            and slow_response.status_code == 200
            and slow_was_running
            and status_elapsed < 0.15,
            (
                "blocking Git/CI producer work runs outside the FastAPI "
                "event loop"
            ),
            {
                "status_code": status_response.status_code,
                "slow_code": slow_response.status_code,
                "slow_was_running": slow_was_running,
                "status_elapsed": status_elapsed,
            },
        )

        release_response = await client.post(
            "/awareness/project-guardian/release-goals",
            json={
                "user_id": "user-a",
                "workspace_id": "workspace-a",
                "repo_id": "owner/repo",
                "target_ref": "refs/heads/main",
                "target_environment": "production",
                "release_cycle": "strict-boundary",
                "workspace_path": "/tmp/repo",
                "github_actions_workflow": ".github/workflows/ci.yml",
                "github_actions_required_jobs": ["gate"],
                "github_actions_app_id": True,
            },
        )
        expect(
            release_response.status_code == 422
            and producer.register_calls == 0,
            "HTTP release Goal rejects bool-to-int CI app coercion",
            release_response.json(),
        )

        intent_response = await client.post(
            (
                "/awareness/project-guardian/release-goals/"
                "goal-a/deployment-intent"
            ),
            json={
                "schema_version": (
                    "veyra.project_guardian_deployment_intent_command.v1"
                ),
                "user_id": "user-a",
                "session_id": "session-a",
                "goal_revision": "revision-a",
                "expected_goal_state_revision": True,
                "target_sha": "a" * 40,
                "target_environment": "production",
                "transition": "declare",
                "operation_id": "operation-a",
                "occurred_at": "2026-07-26T12:00:00+00:00",
            },
        )
        expect(
            intent_response.status_code == 422
            and producer.intent_calls == 0,
            "HTTP deployment intent rejects bool-to-int Goal CAS coercion",
            intent_response.json(),
        )

        policy_response = await client.post(
            (
                "/awareness/project-guardian/release-goals/"
                "goal-a/attention-policy"
            ),
            json={
                "schema_version": (
                    "veyra.project_guardian_attention_policy_command.v1"
                ),
                "user_id": "user-a",
                "goal_revision": "revision-a",
                "expected_goal_state_revision": 1,
                "attention_group_id": "release-group-a",
                "goal_priority": 0.9,
                "deadline_at": "2026-07-26T14:00:00+00:00",
                "timezone": "Asia/Shanghai",
                "notifications_paused": False,
                "quiet_hours": {"enabled": False},
                "daily_notification_budget": True,
            },
        )
        expect(
            policy_response.status_code == 422
            and attention.policy_calls == 0,
            "HTTP Attention policy rejects bool-to-int budget coercion",
            policy_response.json(),
        )

        dismissal_response = await client.post(
            (
                "/awareness/project-guardian/attention/dismissals/"
                "pgas_example"
            ),
            json={
                "schema_version": (
                    "veyra.project_guardian_attention_dismissal_command.v1"
                ),
                "user_id": "user-a",
                "dismissed": 1,
            },
        )
        expect(
            dismissal_response.status_code == 422
            and attention.dismissal_calls == 0,
            "HTTP Attention dismissal rejects int-to-bool coercion",
            dismissal_response.json(),
        )


def main() -> int:
    asyncio.run(run_contract())
    print("Project Guardian HTTP contract smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
