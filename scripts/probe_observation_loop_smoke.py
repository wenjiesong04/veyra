#!/usr/bin/env python3
"""A failed observation must not reproduce itself through the refresh loop.

Live state showed 243 identical ``web_probe:web:status`` rows. The mechanism was
a closed loop, not a single bug:

1. ``WebProbe.run("")`` returns ``missing_target`` with the operator hint
   "Web probe needs an http or https URL."
2. PerceptionLayer turned that hint into a Belief claim.
3. The claim went stale with ``next_action=refresh_probe``.
4. ``StateRefresh._target_for_claim`` could not find a target in the evidence
   and fell back to the claim's own text.
5. That sentence was handed to ``WebProbe`` as the URL, producing
   ``missing_target`` again -> back to step 2, once per tick.

This gate pins both cuts: a non-observation never becomes a claim, and a claim's
human-readable text is never used as a probe argument. It also pins that real
observations still flow through unchanged.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


class RecordingProbe:
    """Stand-in probe that records every target it is asked to observe."""

    def __init__(self, probe_name: str) -> None:
        self.probe_name = probe_name
        self.targets: list[str] = []

    def run(self, target: str = "") -> dict[str, Any]:
        self.targets.append(target)
        from probes.schema import probe_payload

        return probe_payload(
            probe=self.probe_name,
            target=target or self.probe_name.replace("_probe", ""),
            status="missing_target" if not target else "ok",
            summary=(
                f"{self.probe_name} needs a target."
                if not target
                else f"{self.probe_name} observed {target}."
            ),
            confidence=0.5 if not target else 0.9,
            ttl_seconds=1,
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["VEYRA_STATE_ROOT"] = tmp
        os.environ["VEYRA_AGENCY_ROOT"] = str(Path(tmp) / "agency")

        from core.perception_layer import PerceptionLayer
        from core.world_state import WorldStateStore
        from probes.web_probe import WebProbe
        from runtime.state_refresh import StateRefresh

        store = WorldStateStore(Path(tmp))
        perception = PerceptionLayer(store, model_assist_enabled=False)

        # 1. A probe that never reached a target produces no belief at all.
        for _ in range(30):
            perception.interpret_probe_result(WebProbe().run(""))
        claims = store.read_json("belief_state.json").get("claims") or []
        expect(
            not claims,
            "30 target-less observations create zero claims",
            len(claims),
        )

        # 2. The same is true for every probe that reports missing_target.
        for probe_name in ("search_probe", "weather_probe", "youtube_feed_probe"):
            perception.interpret_probe_result(
                {
                    "probe": probe_name,
                    "target": probe_name.replace("_probe", ""),
                    "status": "missing_target",
                    "summary": f"{probe_name} needs a target.",
                    "confidence": 0.5,
                    "ttl_seconds": 1,
                }
            )
        claims = store.read_json("belief_state.json").get("claims") or []
        expect(
            not claims,
            "no probe turns missing_target into a belief",
            [claim.get("key") for claim in claims],
        )

        # 3. A real observation still becomes a claim, keyed by its real target.
        perception.interpret_probe_result(
            {
                "probe": "web_probe",
                "target": "http://127.0.0.1:8000/",
                "status": "ok",
                "summary": "Web target http://127.0.0.1:8000/ returned HTTP 200.",
                "confidence": 0.9,
                "ttl_seconds": 1,
                "details": {"target": "http://127.0.0.1:8000/"},
            }
        )
        claims = store.read_json("belief_state.json").get("claims") or []
        expect(
            len(claims) == 1 and "127.0.0.1:8000" in str(claims[0].get("key")),
            "a real observation still becomes a claim keyed by its target",
            claims,
        )

        # 4. Refreshing a claim never passes the claim's own prose as a target.
        refresh = StateRefresh(store, model_assist_enabled=False)
        recorder = RecordingProbe("web_probe")
        refresh.probes["web_probe"] = recorder

        stale_claim = {
            "key": "web_probe:web:status",
            "claim": "Web probe needs an http or https URL.",
            "status": "stale",
            "next_action": "refresh_probe",
            "source": "web_probe",
            "confidence": 0.5,
            "evidence": {"status": "missing_target"},
            "scope_kind": "operator_global",
        }
        store.mutate_json(
            "belief_state.json",
            lambda state: {**state, "claims": [stale_claim]},
        )
        result = refresh.refresh_stale(limit=5)

        expect(
            recorder.targets == [],
            "a target-requiring probe is not run at all when no target resolves",
            recorder.targets,
        )
        expect(
            any(
                item.get("reason") == "no_resolvable_refresh_target"
                for item in result.get("skipped") or []
            ),
            "the skip is reported rather than silently swallowed",
            result.get("skipped"),
        )

        # A probe that needs no target still refreshes normally.
        no_target_recorder = RecordingProbe("git_probe")
        refresh.probes["git_probe"] = no_target_recorder
        store.mutate_json(
            "belief_state.json",
            lambda state: {
                **state,
                "claims": [
                    {
                        "key": "git_workspace:dirty",
                        "claim": "git workspace has uncommitted changes",
                        "status": "stale",
                        "next_action": "refresh_probe",
                        "source": "git_probe",
                        "confidence": 0.9,
                        "evidence": {"status": "dirty"},
                        "scope_kind": "operator_global",
                    }
                ],
            },
        )
        refresh.refresh_stale(limit=5)
        expect(
            no_target_recorder.targets == [""],
            "a probe that needs no target still runs with an empty argument",
            no_target_recorder.targets,
        )

        # Restore the unresolvable claim for the growth checks below.
        refresh.probes["web_probe"] = recorder
        store.mutate_json(
            "belief_state.json",
            lambda state: {**state, "claims": [stale_claim]},
        )

        # 5. The failed refresh did not add a new row: the loop cannot grow.
        claims = store.read_json("belief_state.json").get("claims") or []
        expect(
            len(claims) == 1,
            "a failed refresh does not append another copy of the claim",
            [claim.get("key") for claim in claims],
        )

        # 6. Ten more ticks keep it flat, which is what 243 rows violated.
        for _ in range(10):
            refresh.refresh_stale(limit=5)
        claims = store.read_json("belief_state.json").get("claims") or []
        expect(
            len(claims) == 1,
            "repeated refresh ticks keep the claim count flat",
            len(claims),
        )

    print("probe observation loop smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
