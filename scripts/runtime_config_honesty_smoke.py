#!/usr/bin/env python3
from __future__ import annotations

import json
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


def main() -> None:
    with TemporaryDirectory(prefix="veyra-config-honesty-") as tmp:
        root = Path(tmp)
        agency_root = root / "agency"
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text(
            json.dumps(
                {
                    "selected_agent_must_be_available": True,
                    "user_commitments": [{"commitment_id": "legacy"}],
                }
            ),
            encoding="utf-8",
        )
        (agency_root / "triggers.yaml").write_text(
            "triggers:\n  - name: selected_agent_unavailable\n    action: probe_executor_status\n",
            encoding="utf-8",
        )
        (agency_root / "preferences.json").write_text('{"language": "zh-CN"}', encoding="utf-8")
        (agency_root / "self_policy.yaml").write_text("default:\n  observe_before_act: true\n", encoding="utf-8")

        store = WorldStateStore(root / "state")
        store.write_json("executor_state.json", {"status": "unavailable"})
        agency = AgencyCore(store, agency_root=agency_root, model_assist_enabled=False)
        state = agency.state()
        files = state["config_status"]["files"]

        expect(
            state["config_status"]["autonomy_level"]["status"]
            == "domain_scoped"
            and state["config_status"]["autonomy_level"]["value"] is None
            and state["config_status"]["autonomy_level"]["certification"]
            == {"A4": "not_certified", "A5": "not_certified"},
            "autonomy status has no false process-wide A3",
            state,
        )
        expect("user_commitments" in files["goals.json"]["legacy_unused_keys"], "goals.user_commitments marked legacy unused", files["goals.json"])
        expect(files["self_policy.yaml"]["status"] == "legacy_unused", "self policy marked legacy unused", files["self_policy.yaml"])
        expect(files["preferences.json"]["status"] == "local_user_defaults", "preferences marked local-user default only", files["preferences.json"])
        expect(files["triggers.yaml"]["status"] == "active_overlay", "triggers marked active overlay", files["triggers.yaml"])

        intentions = agency.sync_intentions(store.read_all())
        first = next(item for item in intentions if (item.get("source_gap") or {}).get("gap_id") == "selected_agent_unavailable")
        expect(first.get("suggested_action") == "probe_executor_status", "trigger overlay maps gap to action", first)

    print("runtime_config_honesty_smoke: ok")


if __name__ == "__main__":
    main()
