#!/usr/bin/env python3
"""Proves Veyra proactively discovers operational problems (not just user-authorized work).

Covers two new ops-oriented state gaps in AgencyCore.detect_state_gap:
  - commitment_push_repeated_failure (R2): scheduled delivery failing across ticks.
  - selected_agent_unavailable_blocks_commitments (R2): executor down while active
    commitments depend on it.

Both stay conservative (R2 -> suggest/review, never auto-executed) and do not fire when
the runtime is healthy.
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


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def ids(gaps: list[dict]) -> set[str]:
    return {str(g.get("gap_id")) for g in gaps}


def main() -> None:
    with TemporaryDirectory(prefix="veyra-proactive-ops-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        agency = AgencyCore(store, agency_root=str(Path(tmp) / "agency"), model_assist_enabled=False)

        # 1. Healthy runtime: neither ops gap fires.
        store.write_json("active_loop_state.json", {"ticks": [{"steps": [{"name": "commitment_push", "result_status": "success"}]}]})
        gaps = agency.detect_state_gap(goals={"selected_agent_must_be_available": True}, world_state={"executor_state": {"status": "available"}})
        expect("commitment_push_repeated_failure" not in ids(gaps), "no push-failure gap when healthy", ids(gaps))
        expect("selected_agent_unavailable_blocks_commitments" not in ids(gaps), "no commitment-block gap when available", ids(gaps))

        # 2. Two consecutive failing ticks -> R2 review gap.
        store.write_json("active_loop_state.json", {"ticks": [
            {"steps": [{"name": "commitment_push", "result_status": "degraded"}]},
            {"steps": [{"name": "commitment_push", "result_status": "error"}]},
        ]})
        gaps = agency.detect_state_gap(goals={}, world_state={"executor_state": {"status": "available"}})
        push_gap = next((g for g in gaps if g.get("gap_id") == "commitment_push_repeated_failure"), None)
        expect(push_gap is not None, "repeated push failure surfaces a gap", ids(gaps))
        expect(push_gap.get("risk_level") == "R2", "push-failure gap is R2 (suggest/review)", push_gap)

        # 3. A single failure (preceded by success) does not fire.
        store.write_json("active_loop_state.json", {"ticks": [
            {"steps": [{"name": "commitment_push", "result_status": "success"}]},
            {"steps": [{"name": "commitment_push", "result_status": "error"}]},
        ]})
        gaps = agency.detect_state_gap(goals={}, world_state={"executor_state": {"status": "available"}})
        expect("commitment_push_repeated_failure" not in ids(gaps), "single push failure does not fire", ids(gaps))

        # 4. Executor unavailable + active commitment -> R2 block gap alongside the base R1.
        store.write_json("active_loop_state.json", {"ticks": [{"steps": [{"name": "commitment_push", "result_status": "success"}]}]})
        store.write_json("user_commitments.json", {"commitments": [
            {"commitment_id": "c1", "kind": "weather_daily", "status": "active", "payload": {"topic": "weather"}},
        ]})
        gaps = agency.detect_state_gap(goals={"selected_agent_must_be_available": True}, world_state={"executor_state": {"status": "offline"}})
        expect("selected_agent_unavailable" in ids(gaps), "base R1 unavailable gap still present", ids(gaps))
        block = next((g for g in gaps if g.get("gap_id") == "selected_agent_unavailable_blocks_commitments"), None)
        expect(block is not None and block.get("risk_level") == "R2", "active commitment elevates unavailable to R2 review", ids(gaps))

    print("proactive_ops_discovery_smoke: ok")


if __name__ == "__main__":
    main()
