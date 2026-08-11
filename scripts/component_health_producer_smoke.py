#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from runtime.event_awareness_runtime import ShadowAwarenessRuntime  # noqa: E402
from runtime.structured_observation_ingress import (  # noqa: E402
    StructuredObservationIngress,
)


TOKEN = "component-health-control-token"
WORKSPACE = "/Users/example/component-health-workspace"
USER = "component-health-owner"
SESSION = "component-health-session"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-component-health-") as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.mutate_json(
            "local_world.json",
            lambda state: {**state, "current_project": WORKSPACE},
        )
        awareness = ShadowAwarenessRuntime(store, mode="record_only")
        health = {
            "status": "degraded",
            "alerts": [
                {
                    "component": "agent",
                    "severity": "warning",
                    "code": "agent_runtime_snapshot_stale",
                }
            ],
            "components": {"agent": "stale"},
        }
        server_now = datetime(2026, 8, 12, 3, 4, 5, tzinfo=timezone.utc)
        ingress = StructuredObservationIngress(
            state_store=store,
            event_awareness=awareness,
            control_token=TOKEN,
            component_health_snapshot=lambda: health,
            clock=lambda: server_now,
        )
        app = FastAPI()
        app.include_router(build_structured_observations_router(ingress=ingress))
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request = {
            "schema_version": "veyra.component_health_observation.request.v1",
            "operation_id": "component-health-check-1",
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
            "expected_event_inbox_revision": int(
                ingress.status()["event_inbox_revision"]
            ),
            "occurred_at": canonical_utc(now),
        }
        status_before = client.get(
            "/awareness/structured-observations/status"
        ).json()
        expect(
            status_before["component_health_producer_configured"] is True
            and status_before["trusted_producer_capabilities"] == ["component_health"],
            "component health is advertised as a trusted server producer",
            status_before,
        )
        first = client.post(
            "/awareness/structured-observations/component-health",
            headers=headers,
            json=request,
        )
        first_body = first.json()
        expect(
            first.status_code == 200
            and first_body["status"] in {"recorded", "observed"}
            and first_body["producer_id"] == "component_health"
            and not any(first_body["authority"].values()),
            "server-derived component health enters typed awareness",
            first_body,
        )
        event = store.read_json("event_inbox.json")["events"][first_body["event_id"]]
        payload = event["envelope"]["payload"]
        observation = payload["observation"]
        expect(
            payload["producer_id"] == "component_health"
            and payload["structured_anchor_refs"] == [
                {"kind": "entity", "ref_id": "component:veyra"}
            ]
            and observation["fact_kind"] == "availability_signal"
            and observation["fact_state"] == "degraded"
            and observation["categorical_facts"]["severity"] == "high"
            and observation["epistemic_status"] == "observed"
            and observation["is_fact"] is True
            and not any(payload["authority"].values()),
            "health facts and scope are server-owned and non-authorizing",
            observation,
        )
        expect(
            payload["valid_from"] == canonical_utc(server_now)
            and payload["valid_from"] != request["occurred_at"],
            "component health uses server-owned observation time",
            payload,
        )
        replay = client.post(
            "/awareness/structured-observations/component-health",
            headers=headers,
            json=request,
        )
        expect(
            replay.status_code == 200
            and replay.json()["status"] == "replayed"
            and replay.json()["event_id"] == first_body["event_id"],
            "component health operation replay is idempotent",
            replay.json(),
        )

        before_background_events = len(store.read_json("event_inbox.json")["events"])
        background = ingress.publish_component_health_background(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
        )
        background_replay = ingress.publish_component_health_background(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
        )
        expect(
            background.get("background") is True
            and background.get("status") in {"recorded", "observed", "replayed"}
            and background_replay.get("status") == "unchanged"
            and len(store.read_json("event_inbox.json")["events"])
            == before_background_events + 1,
            "opt-in background producer deduplicates unchanged server health",
            {"first": background, "second": background_replay},
        )
        spoof = client.post(
            "/awareness/structured-observations/component-health",
            headers=headers,
            json={**request, "facts": {"severity": "critical"}},
        )
        expect(
            spoof.status_code == 422,
            "component health route rejects caller-supplied facts",
            spoof.json(),
        )
        print("Component health producer smoke passed: 7/7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
