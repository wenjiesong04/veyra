#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from runtime.state_refresh import StateRefresh


class FakeProbe:
    def run(self, target: str) -> dict[str, Any]:
        return {
            "probe": "port_probe",
            "source": "port_probe",
            "target": target,
            "status": "success",
            "summary": f"refreshed {target}",
            "confidence": 1.0,
            "details": {},
        }


class FakePerception:
    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"status": "accepted", "target": raw.get("target")}


def expect(condition: bool, label: str, details: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-state-refresh-") as temp_dir:
        store = WorldStateStore(Path(temp_dir) / "state")
        supported = [
            {
                "key": f"port:{index}",
                "claim": f"port {index}",
                "source": "port_probe",
                "status": "stale",
                "next_action": "refresh_probe",
                "observed_at": f"2026-01-01T00:00:{index:02d}+00:00",
                "evidence": {"target": f"127.0.0.1:{8000 + index}"},
            }
            for index in range(8)
        ]
        unsupported = [
            {
                "key": f"event:{index}",
                "claim": f"old event {index}",
                "source": "event",
                "status": "stale",
                "next_action": "refresh_probe",
                "observed_at": f"2025-01-01T00:00:{index:02d}+00:00",
            }
            for index in range(30)
        ]
        store.write_json("belief_state.json", {"claims": unsupported + supported})
        refresh = StateRefresh(store, model_assist_enabled=False)
        refresh.probes = {"port_probe": FakeProbe()}
        refresh.perception = FakePerception()

        first = refresh.refresh_stale(limit=3)
        second = refresh.refresh_stale(limit=3)
        first_claims = [item["claim"] for item in first["refreshed"]]
        second_claims = [item["claim"] for item in second["refreshed"]]

        expect(len(first_claims) == 3, "supported claims are not starved", first)
        expect(len(second_claims) == 3, "second fair batch runs", second)
        expect(set(first_claims).isdisjoint(second_claims), "cursor advances across supported claims", {"first": first_claims, "second": second_claims})
        expect(first["unsupported_stale"] == 30, "unsupported stale claims are counted separately", first)
        expect(all(item.get("source") == "event" for item in first["skipped"]), "skip diagnostics name unsupported sources", first["skipped"])

    print("state refresh fairness smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
