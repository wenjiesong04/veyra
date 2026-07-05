#!/usr/bin/env python3
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


def main() -> None:
    with TemporaryDirectory(prefix="veyra-agency-single-source-") as tmp:
        root = Path(tmp)
        store = WorldStateStore(root / "state")
        agency_root = root / "agency"
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text('{"selected_agent_must_be_available": true}', encoding="utf-8")
        (agency_root / "triggers.yaml").write_text(
            "triggers:\n  - name: selected_agent_unavailable\n    action: probe_executor_status\n",
            encoding="utf-8",
        )
        store.write_json("executor_state.json", {"status": "unavailable"})

        agency = AgencyCore(store, agency_root=agency_root, model_assist_enabled=False)
        proactive = ProactiveChecks(store, agency=agency, model_assist_enabled=False)

        expect(proactive.agency is agency, "proactive checks uses injected AgencyCore")
        proactive.agency.sync_intentions(store.read_all())
        api_state = agency.state()
        proactive_state = proactive.agency.state()

        expect(api_state["intention_path"] == proactive_state["intention_path"], "API and proactive intention path matches", api_state)
        expect(len(api_state["intentions"]) >= 1, "shared agency state exposes proactive intentions", api_state)
        first = api_state["intentions"][0]
        expect((first.get("source_gap") or {}).get("gap_id") == "selected_agent_unavailable", "expected gap is present", first)
        expect(str(api_state["config_status"]["files"]["triggers.yaml"]["status"]) == "active_overlay", "trigger config is active overlay", api_state["config_status"])

    print("agency_single_source_smoke: ok")


if __name__ == "__main__":
    main()
