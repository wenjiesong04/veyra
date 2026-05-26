from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    print("Veyra active awareness smoke")
    with TemporaryDirectory(prefix="veyra-active-awareness-") as tmp:
        root = Path(tmp)
        state = WorldStateStore(root / "state")
        agency = AgencyCore(state, agency_root=root / "agency", model_assist_enabled=False)
        executor = state.read_json("executor_state.json")
        executor["capability_snapshot"] = {
            "runtime": "openclaw",
            "status": "available",
            "connected": True,
            "tools": ["web_search"],
            "updated_at": "2000-01-01T00:00:00+00:00",
            "ttl_seconds": 1,
            "freshness": "stale",
        }
        state.write_json("executor_state.json", executor)

        intentions = agency.sync_intentions(state.read_all())
        stale = [
            item
            for item in intentions
            if (item.get("source_gap") or {}).get("gap_id") == "selected_agent_capability_stale"
        ]
        expect(bool(stale), "stale OpenClaw capability creates intention", intentions)
        item = stale[-1]
        expect(item.get("risk_level") == "R1", "capability refresh intention is low risk", item)
        expect(item.get("suggested_action") == "refresh_agent_capabilities", "refresh suggestion", item)
        expect(item.get("status") == "pending", "intention waits for read-only review/execution", item)
        print(json.dumps({"status": "success", "intention": item}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
