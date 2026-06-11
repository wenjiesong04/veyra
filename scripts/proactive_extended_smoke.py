#!/usr/bin/env python3
"""Proves the broadened discovery surface and Agent self-heal.

Discovery: resource pressure (CPU/memory/disk), log anomalies, and Agent task drift
become state gaps. Self-heal: a reversible reconnect runs automatically, while an
irreversible restart is only ever escalated to a review (R4), and only when active
commitments depend on the agent.
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def by_id(gaps: list[dict]) -> dict[str, dict]:
    return {str(g.get("gap_id")): g for g in gaps}


class ConnectedAdapter:
    def connection_status(self) -> dict:
        return {"connected": True, "status": "available"}


class DownAdapter:
    def connection_status(self) -> dict:
        return {"connected": False, "status": "unavailable"}


def main() -> None:
    with TemporaryDirectory(prefix="veyra-extended-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        agency = AgencyCore(store, agency_root=str(Path(tmp) / "agency"), model_assist_enabled=False)

        # --- Discovery: broadened coverage ---
        world = {
            "executor_state": {"status": "available"},
            "local_world": {
                "probes": {
                    "system_probe": {"details": {"resource_pressure": ["disk_95.0%", "memory_93.0%"]}},
                    "log_probe": {"target": "/tmp/app.log", "details": {"anomaly": True, "error_count": 7}},
                }
            },
            "task_state": {"pending_agent_tasks": [
                {"task_id": "t1", "verification_status": "verified_failed", "poll_count": 2},
                {"task_id": "t2", "verification_status": "submitted", "poll_count": 6},
            ]},
        }
        gaps = by_id(agency.detect_state_gap(goals={}, world_state=world))
        expect("resource_pressure" in gaps and gaps["resource_pressure"]["risk_level"] == "R2", "resource pressure discovered (R2)", gaps.get("resource_pressure"))
        expect("log_anomaly" in gaps and gaps["log_anomaly"]["risk_level"] == "R2", "log anomaly discovered (R2)", gaps.get("log_anomaly"))
        drift = gaps.get("agent_task_drift")
        expect(drift is not None and drift["risk_level"] == "R2", "agent task drift discovered (R2)", drift)
        expect(set(drift.get("affected_tasks") or []) == {"t1", "t2"}, "drift lists both failed and stalled tasks", drift)

        # --- Self-heal: reversible reconnect runs automatically ---
        store.write_json("executor_state.json", {"status": "unavailable", "connected": False})
        pc_ok = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency"), model_assist_enabled=False, agent_adapter_resolver=lambda: ConnectedAdapter())
        result = pc_ok._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        expect(result.get("self_heal") == "reconnected", "reversible reconnect runs automatically", result)
        expect(store.read_json("executor_state.json").get("connected") is True, "executor state reflects reconnect", store.read_json("executor_state.json"))

        # --- Self-heal: restart is only escalated to review, and only when it matters ---
        pc_down_idle = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency2"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        idle = pc_down_idle._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        expect(idle.get("self_heal") == "reconnect_failed", "no restart review when no commitments depend on agent", idle)
        expect(not idle.get("review_id"), "idle reconnect failure creates no review", idle)

        store.write_json("user_commitments.json", {"commitments": [{"commitment_id": "c1", "kind": "weather_daily", "status": "active"}]})
        pc_down = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency3"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        escalated = pc_down._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        expect(escalated.get("self_heal") == "reconnect_failed_escalated_to_review", "restart escalates to review when commitments depend on agent", escalated)
        expect(bool(escalated.get("review_id")), "restart escalation creates a review", escalated)
        items = store.read_json("review_queue.json").get("items", [])
        restart = [i for i in items if isinstance(i, dict) and (i.get("proposal") or {}).get("type") == "agent_restart"]
        expect(len(restart) == 1 and restart[0].get("risk_level") == "R4", "restart review is R4 and irreversible", restart)

        # Dedup: a second self-heal does not pile up duplicate restart reviews.
        pc_down._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        items2 = store.read_json("review_queue.json").get("items", [])
        restart2 = [i for i in items2 if isinstance(i, dict) and (i.get("proposal") or {}).get("type") == "agent_restart"]
        expect(len(restart2) == 1, "duplicate restart reviews are suppressed", restart2)

    print("proactive_extended_smoke: ok")


if __name__ == "__main__":
    main()
