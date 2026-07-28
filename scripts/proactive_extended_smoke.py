#!/usr/bin/env python3
"""Proves broadened discovery and the legacy self-heal entry is constrained.

Discovery: resource pressure (CPU/memory/disk), log anomalies, and Agent task drift
become state gaps. The former private reconnect helper now delegates to the single
Phase 5 controller and stays shadow-only by default.
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
    gateway_url = "ws://127.0.0.1:18789"

    def connection_status(self, *, force_refresh: bool = False) -> dict:
        capabilities = {
            "contract_version": "veyra.agent_adapter.v2",
            "runtime": "openclaw",
            "status": "available",
            "connected": True,
            "protocol": "openclaw_gateway_ws",
            "compatibility": {
                "status": "compatible",
                "required_methods": {
                    "chat.send": True,
                    "health": True,
                    "status": True,
                },
            },
            "features": {
                "agent_dialogue_v1": True,
                "bounded_agent_dialogue": True,
                "caller_supplied_run_id": True,
                "idempotent_submit": True,
                "exact_stop": True,
                "enforced_execution_profile": "phase3_sandbox_proposal",
                "tool_proxy_enforced": True,
            },
            "raw": {
                "health": {"ok": True},
                "server": {
                    "method_count": 8,
                    "protocol": "openclaw_gateway_ws",
                    "version": "phase5-fixture",
                },
                "gateway_status": {"tasks": {"active": 0}},
                "tools": {"items": []},
                "skills": {"items": []},
            },
        }
        return {
            "connected": True,
            "status": "available",
            "capabilities": capabilities,
        }


class DownAdapter:
    gateway_url = "ws://127.0.0.1:18789"

    def connection_status(self, *, force_refresh: bool = False) -> dict:
        return {
            "connected": False,
            "status": "unavailable",
            "capabilities": {
                "runtime": "openclaw",
                "status": "unavailable",
                "connected": False,
                "compatibility": {"status": "unavailable"},
            },
        }


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

        # --- Legacy helper: only a fresh healthy observation, never a claimed repair. ---
        store.write_json("executor_state.json", {"status": "unavailable", "connected": False})
        pc_ok = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency"), model_assist_enabled=False, agent_adapter_resolver=lambda: ConnectedAdapter())
        pc_ok.self_heal.probe_runner = lambda port: {
            "source": "openclaw_probe",
            "target": "openclaw_runtime",
            "status": "listening",
            "details": {"port": port},
            "validation": {"source": "real_probe", "observed": True},
        }
        result = pc_ok._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        self_heal = result.get("self_heal") or {}
        expect(self_heal.get("status") == "shadow_healthy", "legacy entry delegates to fresh shadow observation", result)
        expect(self_heal.get("attempt_count") == 0, "healthy observation consumes no repair attempt", self_heal)
        expect(
            store.read_json("executor_state.json").get("connected") is False,
            "shadow health does not rewrite authoritative executor state",
            store.read_json("executor_state.json"),
        )

        # --- Default shadow: failure is observed, with no attempt or restart review. ---
        pc_down_idle = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency2"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        pc_down_idle.self_heal.probe_runner = lambda port: {
            "source": "openclaw_probe",
            "target": "openclaw_runtime",
            "status": "closed",
            "details": {"port": port},
            "validation": {"source": "real_probe", "observed": True},
        }
        idle = pc_down_idle._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        idle_self_heal = idle.get("self_heal") or {}
        expect(idle_self_heal.get("status") == "shadow_qualified", "default shadow records failure without acting", idle)
        expect(idle_self_heal.get("attempt_count") == 0, "shadow failure consumes no attempt", idle)

        store.write_json("user_commitments.json", {"commitments": [{"commitment_id": "c1", "kind": "weather_daily", "status": "active"}]})
        pc_down = ProactiveChecks(store, agency_root=str(Path(tmp) / "agency3"), model_assist_enabled=False, agent_adapter_resolver=lambda: DownAdapter())
        pc_down.self_heal.probe_runner = pc_down_idle.self_heal.probe_runner
        observed = pc_down._self_heal_agent({"gap_id": "selected_agent_unavailable", "target": "selected_agent"})
        expect((observed.get("self_heal") or {}).get("attempt_count") == 0, "commitments cannot bypass shadow mode", observed)
        items = store.read_json("review_queue.json").get("items", [])
        restart = [i for i in items if isinstance(i, dict) and "restart" in str((i.get("proposal") or {}).get("type") or "")]
        expect(not restart, "shadow mode never creates restart reviews", restart)

    print("proactive_extended_smoke: ok")


if __name__ == "__main__":
    main()
