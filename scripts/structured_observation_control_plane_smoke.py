#!/usr/bin/env python3
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.structured_observation import canonical_utc  # noqa: E402
from routers.structured_observations import (  # noqa: E402
    build_structured_observations_router,
)
from runtime.event_awareness_runtime import (  # noqa: E402
    ShadowAwarenessRuntime,
)
from runtime.structured_observation_ingress import (  # noqa: E402
    StructuredObservationIngress,
)


TOKEN = "structured-observation-control-token"
USER = "structured-owner"
SESSION = "structured-session"
GOAL = "goal-structured-release"
TASK = "task-structured-release"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".lock"):
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def command(
    *,
    operation_id: str,
    evidence_id: str,
    revision: int,
    workspace: str,
    user_id: str = USER,
    session_id: str = SESSION,
    anchors: list[dict[str, str]] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    occurred = (
        datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if occurred_at
        else datetime.now(timezone.utc)
    )
    return {
        "schema_version": "veyra.structured_observation.command.v1",
        "operation_id": operation_id,
        "producer_id": "local_operator",
        "producer_receipt_id": f"receipt-{operation_id}",
        "user_id": user_id,
        "workspace_id": workspace,
        "session_id": session_id,
        "expected_event_inbox_revision": revision,
        "occurred_at": canonical_utc(occurred),
        "valid_until": canonical_utc(occurred + timedelta(hours=1)),
        "anchors": anchors
        or [
            {"kind": "goal", "ref_id": GOAL},
            {"kind": "task", "ref_id": TASK},
        ],
        "evidence": [
            {
                "evidence_id": evidence_id,
                "source": "human_verified",
            }
        ],
        "facts": {
            "kind": "risk_signal",
            "state": "degraded",
            "severity": "high",
            "urgency": "immediate",
            "novelty": "new",
            "uncertainty": "low",
            "evidence_quality": "direct",
            "epistemic_status": "observed",
        },
    }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-structured-observation-") as raw:
        state_root = Path(raw) / "state"
        store = WorldStateStore(state_root)
        # Use a path-shaped identifier so the EventInbox privacy redactor is
        # exercised without losing the typed workspace binding on replay.
        workspace = "/Users/example/veyra-structured-workspace"
        store.mutate_json(
            "local_world.json",
            lambda state: {**state, "current_project": workspace},
        )
        store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    {
                        "goal_id": GOAL,
                        "user_id": USER,
                        "workspace_id": workspace,
                        "status": "active",
                        "goal_priority": 0.95,
                    },
                    {
                        "goal_id": "goal-foreign",
                        "user_id": "another-owner",
                        "workspace_id": workspace,
                        "status": "active",
                        "goal_priority": 1.0,
                    },
                ],
            },
        )
        store.mutate_json(
            "user_commitments.json",
            lambda state: {
                **state,
                "commitments": [
                    {
                        "commitment_id": "commitment-active",
                        "user_id": USER,
                        "workspace_id": workspace,
                        "status": "active",
                    },
                    {
                        "commitment_id": "commitment-paused",
                        "user_id": USER,
                        "workspace_id": workspace,
                        "status": "paused",
                    },
                ],
            },
        )

        def configure(config: dict[str, Any]) -> dict[str, Any]:
            config["event_awareness"] = {
                "mode": "shadow",
                "mode_epoch": 1,
                "allowed_modes": ["disabled", "record_only", "shadow"],
            }
            config["general_suggestions"] = {
                "mode": "advise_only",
                "mode_epoch": 1,
                "allowed_modes": [
                    "advise_only",
                    "disabled",
                    "record_only",
                    "shadow",
                ],
            }
            return config

        store.mutate_json("ops_config.json", configure)
        awareness = ShadowAwarenessRuntime(store, mode="shadow")
        awareness.suggestion_outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json("suggestion_outbox.json").get(
                    "_state_revision"
                )
                or 0
            ),
        )
        ingress = StructuredObservationIngress(
            state_store=store,
            event_awareness=awareness,
            control_token=TOKEN,
        )
        app = FastAPI()
        app.include_router(build_structured_observations_router(ingress=ingress))
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {TOKEN}"}

        paths = {
            (route.path, next(iter(route.methods or [])))
            for route in app.routes
            if route.path.startswith("/awareness/structured-observations")
        }
        expect(
            paths
            == {
                ("/awareness/structured-observations/status", "GET"),
                ("/awareness/structured-observations", "POST"),
                ("/awareness/structured-observations/component-health", "POST"),
                ("/awareness/structured-observations/workspace/status", "GET"),
                ("/awareness/structured-observations/workspace/configure", "POST"),
                ("/awareness/structured-observations/workspace/run-once", "POST"),
            },
            "structured ingress exposes aggregate status and bounded private typed producers",
            paths,
        )

        before_status = tree_digest(state_root)
        status_response = client.get(
            "/awareness/structured-observations/status"
        )
        initial_status = status_response.json()
        after_status = tree_digest(state_root)
        expect(
            status_response.status_code == 200
            and initial_status["status"] == "available"
            and initial_status["event_awareness_mode"] == "shadow"
            and initial_status["numeric_salience_accepted"] is False
            and initial_status["free_text_or_metadata_accepted"] is False
            and initial_status["loopback_token_bypass_allowed"] is False
            and before_status == after_status,
            "public status is aggregate-only and byte-pure",
            initial_status,
        )
        initial_revision = int(initial_status["event_inbox_revision"])
        first_payload = command(
            operation_id="structured-observation-one",
            evidence_id="evidence-structured-one",
            revision=initial_revision,
            workspace=workspace,
        )

        before_rejected = tree_digest(state_root)
        unauthorized = client.post(
            "/awareness/structured-observations",
            json=first_payload,
        )
        free_text = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json={
                **first_payload,
                "text": "DO-NOT-ECHO-free-text",
                "metadata": {"goal_id": GOAL},
            },
        )
        numeric_salience = copy.deepcopy(first_payload)
        numeric_salience["facts"]["severity"] = 0.99
        numeric = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=numeric_salience,
        )
        unknown_producer = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json={**first_payload, "producer_id": "arbitrary_model"},
        )
        spoofed_component = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json={
                **first_payload,
                "producer_id": "component_health",
                "evidence": [
                    {
                        "evidence_id": "component-receipt",
                        "source": "direct_tool_observation",
                    }
                ],
                "anchors": [{"kind": "entity", "ref_id": "openclaw"}],
                "facts": {
                    **first_payload["facts"],
                    "kind": "availability_signal",
                },
            },
        )
        foreign_goal = command(
            operation_id="structured-invalid-goal",
            evidence_id="evidence-invalid-goal",
            revision=initial_revision,
            workspace=workspace,
            anchors=[{"kind": "goal", "ref_id": "goal-foreign"}],
        )
        invalid_goal = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=foreign_goal,
        )
        inactive_commitment = command(
            operation_id="structured-inactive-commitment",
            evidence_id="evidence-inactive-commitment",
            revision=initial_revision,
            workspace=workspace,
            anchors=[
                {"kind": "commitment", "ref_id": "commitment-paused"}
            ],
        )
        invalid_commitment = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=inactive_commitment,
        )
        after_rejected = tree_digest(state_root)
        expect(
            unauthorized.status_code == 401
            and free_text.status_code == 422
            and numeric.status_code == 422
            and unknown_producer.status_code == 422
            and spoofed_component.status_code == 409
            and invalid_goal.status_code == 409
            and invalid_commitment.status_code == 409
            and "DO-NOT-ECHO" not in str(free_text.json())
            and before_rejected == after_rejected,
            "auth, allowlist, typed facts, active durable refs, text and metadata fail closed",
            {
                "unauthorized": unauthorized.json(),
                "free_text": free_text.json(),
                "numeric": numeric.json(),
                "unknown_producer": unknown_producer.json(),
                "spoofed_component": spoofed_component.json(),
                "invalid_goal": invalid_goal.json(),
                "invalid_commitment": invalid_commitment.json(),
            },
        )

        protected_before = {
            name: copy.deepcopy(store.read_json(name))
            for name in (
                "tool_governance_state.json",
                "review_queue.json",
                "executor_state.json",
            )
        }
        first_http = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=first_payload,
        )
        first = first_http.json()
        expect(
            first_http.status_code == 200
            and first["status"] == "observed"
            and first["operation_replayed"] is False
            and first["situation_id"]
            and not any(first["authority"].values()),
            "first exact observation enters shadow awareness without authority",
            first,
        )
        event_record = store.read_json("event_inbox.json")["events"][
            first["event_id"]
        ]
        envelope = event_record["envelope"]
        expect(
            envelope["type"] == "observation"
            and envelope["source"]
            == {
                "channel": "structured_observation",
                "user_id": USER,
                "session_id": SESSION,
            }
            and envelope["payload"]["workspace_id"] == workspace
            and envelope["payload"]["structured_anchor_refs"]
            == first_payload["anchors"]
            and envelope["payload"]["salience_components"]
            == {
                "severity": 0.75,
                "urgency": 1.0,
                "novelty": 0.9,
                "uncertainty": 0.1,
                "evidence_completeness": 1.0,
            }
            and "text" not in envelope["payload"]
            and "metadata" not in envelope["payload"]
            and not any(envelope["payload"]["authority"].values()),
            "durable event has only typed anchors/evidence and server-owned salience",
            envelope,
        )

        before_replay = tree_digest(state_root)
        replay_http = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=first_payload,
        )
        after_replay = tree_digest(state_root)
        conflict_payload = copy.deepcopy(first_payload)
        conflict_payload["facts"]["state"] = "blocked"
        conflict = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=conflict_payload,
        )
        expect(
            replay_http.status_code == 200
            and replay_http.json()["status"] == "replayed"
            and replay_http.json()["event_id"] == first["event_id"]
            and before_replay == after_replay
            and conflict.status_code == 409,
            "EventInbox operation replay is a no-op and semantic rebinding conflicts",
            {
                "replay": replay_http.json(),
                "conflict": conflict.json(),
            },
        )

        stale_new = command(
            operation_id="structured-observation-stale-cas",
            evidence_id="evidence-stale-cas",
            revision=initial_revision,
            workspace=workspace,
        )
        stale = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=stale_new,
        )
        expect(
            stale.status_code == 409,
            "new operation requires the exact current EventInbox CAS",
            stale.json(),
        )

        second_revision = int(
            client.get(
                "/awareness/structured-observations/status"
            ).json()["event_inbox_revision"]
        )
        second_payload = command(
            operation_id="structured-observation-two",
            evidence_id="evidence-structured-two",
            revision=second_revision,
            workspace=workspace,
        )
        second_payload["facts"]["kind"] = "change_signal"
        second_http = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=second_payload,
        )
        general = store.read_json("general_situation_state.json")
        inbox = awareness.suggestion_outbox.list_inbox(
            user_id=USER,
            session_id=SESSION,
        )
        foreign_inbox = awareness.suggestion_outbox.list_inbox(
            user_id=USER,
            session_id="another-session",
        )
        protected_after = {
            name: copy.deepcopy(store.read_json(name))
            for name in protected_before
        }
        expect(
            second_http.status_code == 200
            and second_http.json()["status"] == "observed"
            and general.get("general_situation_count") == 1
            and inbox.get("count") == 1
            and inbox["items"][0]["status"] == "pending"
            and foreign_inbox.get("count") == 0
            and protected_before == protected_after,
            "two distinct events create one eligible parent and exact-owner Console suggestion only",
            {
                "second": second_http.json(),
                "general": general,
                "inbox": inbox,
                "foreign_inbox": foreign_inbox,
            },
        )

        # Non-durable task refs never authorize cross-session aggregation.
        revision = int(ingress.status()["event_inbox_revision"])
        task_a = command(
            operation_id="non-durable-task-session-a",
            evidence_id="evidence-task-session-a",
            revision=revision,
            workspace=workspace,
            session_id="task-session-a",
            anchors=[{"kind": "task", "ref_id": "task-session-isolation"}],
        )
        task_a_http = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=task_a,
        )
        revision = int(ingress.status()["event_inbox_revision"])
        task_b = command(
            operation_id="non-durable-task-session-b",
            evidence_id="evidence-task-session-b",
            revision=revision,
            workspace=workspace,
            session_id="task-session-b",
            anchors=[{"kind": "task", "ref_id": "task-session-isolation"}],
        )
        task_b_http = client.post(
            "/awareness/structured-observations",
            headers=headers,
            json=task_b,
        )
        general_after_isolation = store.read_json(
            "general_situation_state.json"
        )
        expect(
            task_a_http.status_code == 200
            and task_b_http.status_code == 200
            and general_after_isolation.get("general_situation_count") == 1,
            "non-durable anchors remain isolated by exact session and workspace",
            general_after_isolation,
        )

        before_final_get = tree_digest(state_root)
        final_status = client.get(
            "/awareness/structured-observations/status"
        ).json()
        after_final_get = tree_digest(state_root)
        expect(
            final_status["structured_event_count"] == 4
            and final_status["event_awareness_mode"] == "shadow"
            and before_final_get == after_final_get,
            "final status remains pure and never changes operational modes",
            final_status,
        )

    print("Structured observation control-plane smoke passed: 10/10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
