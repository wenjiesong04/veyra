#!/usr/bin/env python3
"""Proves the proactive remediation worker turns discovery into bounded action.

R1 read-only gaps are remediated in place (targeted capability refresh through the
agent adapter), and R2 gaps become one-click-approvable review proposals. No write or
restart is auto-executed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402


class FakeAdapter:
    gateway_url = "ws://127.0.0.1:18789"

    @staticmethod
    def capabilities() -> dict:
        return {
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
                "tools": {"items": [{"id": "web_search"}]},
                "skills": {"items": []},
            },
        }

    def connection_status(self, *, force_refresh: bool = False) -> dict:
        caps = self.capabilities()
        return {
            "connected": True,
            "status": "available",
            "capabilities": caps,
        }

    def fetch_capabilities(self, *, force_refresh: bool = False) -> dict:
        return self.capabilities()

    def invalidate_capabilities_cache(self) -> None:
        return None


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    with TemporaryDirectory(prefix="veyra-remediation-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        # Start from a stale capability snapshot so a refresh is observable.
        store.write_json("executor_state.json", {"capability_snapshot": {"freshness": "stale"}})
        pc = ProactiveChecks(
            store,
            agency_root=str(Path(tmp) / "agency"),
            model_assist_enabled=False,
            agent_adapter_resolver=lambda: FakeAdapter(),
        )
        pc.self_heal.probe_runner = lambda port: {
            "source": "openclaw_probe",
            "target": "openclaw_runtime",
            "status": "listening",
            "details": {"port": port},
            "validation": {"source": "real_probe", "observed": True},
        }

        r1 = {
            "intention_id": "int_r1",
            "risk_level": "R1",
            "suggested_action": "refresh_agent_capabilities",
            "status": "pending",
            "source_gap": {
                "gap_id": "selected_agent_capability_stale",
                "target": "selected_agent.capabilities",
                "suggested_action": "refresh_agent_capabilities",
                "action_text": "refresh selected agent capability snapshot with read-only AgentAdapter probe",
            },
        }
        r2 = {
            "intention_id": "int_r2",
            "risk_level": "R2",
            "suggested_action": "review_commitment_push_failures",
            "status": "pending",
            "source_gap": {
                "gap_id": "commitment_push_repeated_failure",
                "target": "commitment_push",
                "observed_status": "2_consecutive_error",
                "suggested_action": "review_commitment_push_failures",
                "action_text": "commitment push failed repeatedly; suggest reviewing the executor and delivery channel",
            },
        }
        pc.agency.write_intentions([r1, r2])

        # R1: read-only remediation actually runs and refreshes the capability snapshot.
        reviewed1 = pc._review_intention(r1)
        expect(reviewed1.get("status") == "executed_read_only", "R1 capability gap is remediated in place", reviewed1)
        remediation = reviewed1.get("remediation") or {}
        expect(
            remediation.get("status") == "observed"
            and (remediation.get("self_heal") or {}).get("status")
            == "shadow_healthy",
            "default shadow records a fresh capability observation",
            remediation,
        )
        snapshot = store.read_json("executor_state.json").get("capability_snapshot", {})
        expect(
            snapshot.get("freshness") == "stale"
            and "web_search" not in (snapshot.get("tools") or []),
            "shadow observation does not rewrite the executor capability snapshot",
            snapshot,
        )

        # R2: becomes a one-click-approvable review proposal (never auto-executed).
        reviewed2 = pc._review_intention(r2)
        expect(reviewed2.get("status") == "review_created", "R2 ops gap creates a review proposal", reviewed2)
        expect(bool(reviewed2.get("review_id")), "R2 remediation links a review id", reviewed2)
        reviews = store.read_json("review_queue.json").get("items", [])
        match = [rv for rv in reviews if isinstance(rv, dict) and rv.get("review_id") == reviewed2.get("review_id")]
        expect(bool(match), "review proposal is persisted in the review queue", reviews)
        proposal = (match[0].get("proposal") or {}) if match else {}
        expect(proposal.get("type") == "proactive_remediation", "review carries the remediation proposal", proposal)

    print("proactive_remediation_smoke: ok")


if __name__ == "__main__":
    main()
