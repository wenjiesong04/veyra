#!/usr/bin/env python3
"""The conservatism monitor must be additive: read-only and off by default.

Every existing alert answers "did something overstep?". None answers "did
anything happen at all?", which is why an empty Attention focus and an empty
suggestion outbox stayed invisible while every component reported healthy.

This gate pins three properties:

1. Default `disabled` keeps /health and /ops/alerts byte-identical, so adding
   the monitor cannot change any existing response.
2. A stage is only reported when the stage above it has real input. Zero output
   with zero input is correct, not conservatism.
3. Evaluation never writes state and never raises severity above `info`, so it
   cannot push /health into degraded or block deployment readiness.
"""

from __future__ import annotations

import hashlib
import json
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


def digest_state(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*.json")):
        digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def set_mode(store: Any, mode: str) -> None:
    store.mutate_json("ops_config.json", lambda state: {**state, "conservatism": {"mode": mode}})


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["VEYRA_STATE_ROOT"] = tmp
        os.environ["VEYRA_AGENCY_ROOT"] = str(Path(tmp) / "agency")

        from core.world_state import WorldStateStore
        from runtime.conservatism_monitor import ConservatismMonitor

        root = Path(tmp)
        store = WorldStateStore(root)
        monitor = ConservatismMonitor(store)

        # 1. Default mode is disabled and contributes nothing to the alert stream.
        expect(monitor.mode() == "disabled", "default mode is disabled", monitor.mode())
        expect(not monitor.findings(), "disabled contributes no alerts", monitor.findings())

        # 2. Upstream with no input never reports: a quiet system is not broken.
        set_mode(store, "record_only")
        expect(monitor.mode() == "record_only", "mode can be enabled", monitor.mode())
        expect(
            not monitor.findings(),
            "empty pipelines with no input produce no findings",
            monitor.findings(),
        )

        # 3. Input upstream with zero output downstream is the actual signal.
        store.write_json(
            "general_situation_state.json",
            {"general_situations": {"gsit_a": {}, "gsit_b": {}}, "general_situation_count": 2},
        )
        store.write_json("attention_hypothesis_state.json", {"hypotheses": {}, "hypothesis_count": 0})
        store.write_json("suggestion_outbox.json", {"proposals": {}, "proposal_count": 0})
        codes = {finding["code"] for finding in monitor.findings()}
        expect(
            "general_situations_never_reach_hypothesis" in codes,
            "aggregated situations that never reach a hypothesis are reported",
            codes,
        )
        expect(
            "suggestions_never_produced" in codes,
            "upstream items with an empty outbox are reported",
            codes,
        )

        # 4. Once the downstream produces, the finding disappears.
        store.write_json(
            "attention_hypothesis_state.json",
            {"hypotheses": {"hyp_a": {}}, "hypothesis_count": 1},
        )
        store.write_json(
            "suggestion_outbox.json",
            {"proposals": {"prop_a": {}}, "proposal_count": 1},
        )
        codes = {finding["code"] for finding in monitor.findings()}
        expect(
            "general_situations_never_reach_hypothesis" not in codes
            and "suggestions_never_produced" not in codes,
            "a producing pipeline reports nothing",
            codes,
        )

        # 5. Severity never exceeds info, so /health status cannot change.
        store.write_json("attention_hypothesis_state.json", {"hypotheses": {}, "hypothesis_count": 0})
        store.write_json("suggestion_outbox.json", {"proposals": {}, "proposal_count": 0})
        findings = monitor.findings()
        expect(
            findings and all(item["severity"] == "info" for item in findings),
            "every finding stays informational",
            [(item["code"], item["severity"]) for item in findings],
        )
        expect(
            all(item["component"] == "conservatism" for item in findings),
            "findings are attributed to one queryable component",
            findings,
        )

        # 6. report() works regardless of mode and states whether alerts are on.
        set_mode(store, "disabled")
        report = monitor.report()
        expect(
            report["mode"] == "disabled" and report["alert_stream_enabled"] is False,
            "report states that the alert stream is off",
            report,
        )
        expect(
            report["finding_count"] >= 1,
            "report still evaluates while disabled, for direct diagnosis",
            report["finding_count"],
        )
        expect(
            not monitor.findings(),
            "but the alert stream stays empty while disabled",
            monitor.findings(),
        )

        # 7. Evaluation is read-only: no state byte changes.
        before = digest_state(root)
        for _ in range(5):
            monitor.report()
            monitor.findings()
        after = digest_state(root)
        expect(
            before == after,
            "evaluating the monitor never writes state",
            [key for key in before if before.get(key) != after.get(key)],
        )

        # 8. A corrupt ops_config falls back to disabled rather than raising.
        (root / "ops_config.json").write_text("{ not json", encoding="utf-8")
        expect(
            monitor.mode() == "disabled" and not monitor.findings(),
            "unreadable config falls back to the silent default",
            monitor.mode(),
        )

    print("conservatism monitor smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
