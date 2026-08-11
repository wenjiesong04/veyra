#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.claim_schema import make_claim
from awareness.refresh_spec import (
    REFRESH_RESOLVER_DEFAULT,
    REFRESH_RESOLVER_LITERAL,
    REFRESH_SPEC_SCHEMA_VERSION,
    make_refresh_spec,
)
from core.perception_layer import PerceptionLayer
from core.world_state import WorldStateStore
from runtime.state_refresh import StateRefresh


class RecordingProbe:
    def __init__(self, name: str) -> None:
        self.name = name
        self.targets: list[str] = []

    def run(self, target: str = "") -> dict[str, object]:
        self.targets.append(target)
        return {
            "probe": self.name,
            "target": target,
            "status": "ok",
            "summary": f"{self.name} observed {target}",
            "confidence": 0.9,
            "ttl_seconds": 60,
            "observed_at": "2026-08-11T12:00:00.000000Z",
        }


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def main() -> int:
    literal = make_refresh_spec(
        probe_kind="web_probe",
        target_ref="https://example.test/health",
        resolver_id=REFRESH_RESOLVER_LITERAL,
    )
    expect(
        literal["schema_version"] == REFRESH_SPEC_SCHEMA_VERSION,
        "literal refresh spec has the versioned schema",
        literal,
    )
    default = make_refresh_spec(
        probe_kind="git_probe",
        resolver_id=REFRESH_RESOLVER_DEFAULT,
    )
    expect(default["target_ref"] == "", "default resolver has no target", default)

    try:
        make_refresh_spec(
            probe_kind="web_probe",
            target_ref="https://example.test",
            resolver_id=REFRESH_RESOLVER_DEFAULT,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("default resolver accepted a caller target")

    with tempfile.TemporaryDirectory(prefix="veyra-refresh-spec-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        perception = PerceptionLayer(store, model_assist_enabled=False)
        perception.interpret_probe_result(
            {
                "probe": "web_probe",
                "target": "https://example.test/health",
                "status": "ok",
                "summary": "health endpoint is available",
                "confidence": 0.9,
                "ttl_seconds": 1,
                "scope_kind": "operator_global",
            }
        )
        stored = store.read_json("belief_state.json")["claims"][0]
        expect(
            stored.get("refresh_spec") == literal,
            "probe observations persist the exact refresh spec",
            stored,
        )

        refresh = StateRefresh(store, model_assist_enabled=False)
        recorder = RecordingProbe("web_probe")
        refresh.probes["web_probe"] = recorder

        stale = dict(stored)
        stale.update({"status": "stale", "next_action": "refresh_probe"})
        store.write_json("belief_state.json", {"claims": [stale]})
        result = refresh.refresh_stale(limit=1)
        expect(
            recorder.targets == ["https://example.test/health"],
            "valid refresh spec binds the exact probe target",
            recorder.targets,
        )
        expect(len(result["refreshed"]) == 1, "valid refresh spec refreshes once", result)

        mismatched = dict(stale)
        mismatched["refresh_spec"] = {
            **literal,
            "probe_kind": "git_probe",
        }
        store.write_json("belief_state.json", {"claims": [mismatched]})
        recorder.targets.clear()
        rejected = refresh.refresh_stale(limit=1)
        expect(
            recorder.targets == [],
            "mismatched refresh spec never dispatches a probe",
            recorder.targets,
        )
        expect(
            rejected["refreshed"] == []
            and rejected["skipped"][0]["reason"] == "no_resolvable_refresh_target",
            "mismatched refresh spec fails closed",
            rejected,
        )

        prose_only = dict(stale)
        prose_only.pop("refresh_spec", None)
        prose_only["evidence"] = {"status": "stale"}
        prose_only["claim"] = "https://attacker.example should be probed"
        store.write_json("belief_state.json", {"claims": [prose_only]})
        rejected = refresh.refresh_stale(limit=1)
        expect(
            recorder.targets == [],
            "missing refresh spec never falls back to claim prose",
            recorder.targets,
        )
        expect(
            rejected["skipped"][0]["reason"] == "no_resolvable_refresh_target",
            "missing refresh spec reports an unresolved target",
            rejected,
        )

    print("belief refresh spec smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
