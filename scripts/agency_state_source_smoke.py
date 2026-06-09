from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore
from core.commitment_core import CommitmentCore
from core.schedule_parser import parse_schedule_text
from core.world_state import WorldStateStore


def main() -> None:
    old_agency_root = os.environ.get("VEYRA_AGENCY_ROOT")
    with TemporaryDirectory(prefix="veyra-agency-state-source-") as tmp:
        root = Path(tmp)
        agency_root = root / "agency"
        agency_root.mkdir()
        goals_path = agency_root / "goals.json"
        goals_path.write_text(json.dumps({"selected_agent_must_be_available": True}, ensure_ascii=False, indent=2), encoding="utf-8")
        before_goals = goals_path.read_text(encoding="utf-8")
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)

        store = WorldStateStore(root / "state")
        commitment_core = CommitmentCore(store)
        commitment = commitment_core.create_commitment(
            {
                "kind": "weather_daily",
                "status": "active",
                "title": "每日天气（贵阳市）",
                "user_id": "user-a",
                "channel": "api",
                "session_id": "session-a",
                "schedule": parse_schedule_text("每天早上10点"),
                "payload": {"location": "贵阳市", "topic": "weather"},
            }
        )
        assert commitment["status"] == "active", commitment
        assert goals_path.read_text(encoding="utf-8") == before_goals

        agency = AgencyCore(store, model_assist_enabled=False)
        state = agency.state()
        assert state["goals"] == {"selected_agent_must_be_available": True}, state
        assert state["active_commitments"][-1]["commitment_id"] == commitment["commitment_id"], state

        claims = [
            {"key": f"event:{index}", "source": "event", "status": "stale", "next_action": "refresh_probe"}
            for index in range(30)
        ]
        gaps = agency.detect_state_gap(
            goals={"selected_agent_must_be_available": False},
            world_state={"executor_state": {"status": "available"}, "belief_state": {"claims": claims}},
        )
        stale_gaps = [gap for gap in gaps if str(gap.get("gap_id") or "").startswith("stale_claim_group:")]
        assert len(stale_gaps) == 1, gaps
        assert stale_gaps[0]["observed_status"] == "30_stale_claims", stale_gaps
        agency.sync_intentions(world_state={"executor_state": {"status": "available"}, "belief_state": {"claims": claims}})
        intentions = agency.read_intentions()
        stale_intentions = [item for item in intentions if str((item.get("source_gap") or {}).get("gap_id") or "").startswith("stale_claim_group:")]
        assert len(stale_intentions) == 1, intentions

    if old_agency_root is None:
        os.environ.pop("VEYRA_AGENCY_ROOT", None)
    else:
        os.environ["VEYRA_AGENCY_ROOT"] = old_agency_root
    print("agency_state_source_smoke: ok")


if __name__ == "__main__":
    main()
