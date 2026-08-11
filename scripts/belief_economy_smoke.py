#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore  # noqa: E402
from awareness.belief_economy import (  # noqa: E402
    BELIEF_ECONOMY_SCHEMA_VERSION,
    BeliefEconomyError,
    economy_value,
    make_economy,
)
from awareness.claim_schema import make_claim  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.state_refresh import StateRefresh  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> int:
    complete = make_economy(
        importance=0.9,
        importance_source="registered_policy",
        change_probability=0.5,
        change_probability_source="producer_policy",
        decision_impact=0.8,
        decision_impact_source_refs=["goal:release"],
        max_staleness_seconds=3600,
    )
    expect(
        complete["schema_version"] == BELIEF_ECONOMY_SCHEMA_VERSION
        and complete["belief_value"] == 0.36
        and complete["evaluation_status"] == "complete",
        "typed economy computes only from explicit factors",
        complete,
    )
    unknown = make_economy(
        importance=None,
        importance_source="registered_policy",
        change_probability=0.5,
        change_probability_source="producer_policy",
        decision_impact=0.8,
    )
    expect(
        unknown["belief_value"] is None
        and unknown["evaluation_status"] == "unknown"
        and economy_value(unknown) is None,
        "unknown factor remains null and is not ranked as a value",
        unknown,
    )
    try:
        make_economy(
            importance=1.2,
            importance_source="registered_policy",
            change_probability=0.5,
            change_probability_source="producer_policy",
            decision_impact=0.8,
        )
    except BeliefEconomyError:
        pass
    else:
        raise AssertionError("out-of-range factor was accepted")
    try:
        make_economy(
            importance=0.9,
            importance_source="free prose source",
            change_probability=0.5,
            change_probability_source="producer_policy",
            decision_impact=0.8,
        )
    except BeliefEconomyError:
        pass
    else:
        raise AssertionError("unregistered prose source was accepted")
    print("PASS invalid economy factors fail closed")

    with TemporaryDirectory(prefix="veyra-belief-economy-") as raw:
        store = WorldStateStore(Path(raw) / "state")
        core = BeliefCore(store)
        claim = make_claim(
            key="release:risk",
            claim="release risk is present",
            source="git_probe",
            confidence=0.9,
            ttl_seconds=60,
            evidence={"status": "dirty"},
            economy=complete,
        )
        claim["scope_kind"] = "operator_global"
        persisted = core.upsert_claim(claim)
        stored = store.read_json("belief_state.json")["claims"][0]
        expect(
            persisted.get("economy") == complete
            and stored.get("economy") == complete,
            "typed economy persists with the claim",
            stored,
        )
        invalid_claim = {**claim, "economy": {**complete, "belief_value": 0.1}}
        rejected = core.upsert_claim(invalid_claim)
        expect(
            rejected.get("status") == "rejected_belief_economy"
            and rejected.get("persisted") is False
            and store.read_json("belief_state.json")["claims"][0]["economy"] == complete,
            "invalid economy cannot overwrite durable belief",
            rejected,
        )

        refresh = StateRefresh(store, model_assist_enabled=False)
        claims = [
            {
                "key": "a:high",
                "status": "stale",
                "updated_at": "2026-08-11T10:00:00.000000Z",
                "user_id": "owner-a",
                "session_id": "session-a",
                "economy": make_economy(
                    importance=0.9,
                    importance_source="registered_policy",
                    change_probability=0.9,
                    change_probability_source="producer_policy",
                    decision_impact=0.9,
                ),
            },
            {
                "key": "b:high",
                "status": "stale",
                "updated_at": "2026-08-11T10:01:00.000000Z",
                "user_id": "owner-b",
                "session_id": "session-b",
                "economy": make_economy(
                    importance=0.8,
                    importance_source="registered_policy",
                    change_probability=0.8,
                    change_probability_source="producer_policy",
                    decision_impact=0.8,
                ),
            },
            {
                "key": "a:lower",
                "status": "stale",
                "updated_at": "2026-08-11T10:02:00.000000Z",
                "user_id": "owner-a",
                "session_id": "session-a",
                "economy": make_economy(
                    importance=0.5,
                    importance_source="registered_policy",
                    change_probability=0.5,
                    change_probability_source="producer_policy",
                    decision_impact=0.5,
                ),
            },
            {
                "key": "b:unknown",
                "status": "stale",
                "updated_at": "2026-08-11T09:00:00.000000Z",
                "user_id": "owner-b",
                "session_id": "session-b",
                "economy": unknown,
            },
        ]
        selected, before, after = refresh._select_fair_batch(claims, limit=2)
        expect(
            [item["key"] for item in selected] == ["a:high", "b:high"]
            and before == 0
            and after == 2,
            "known refresh value orders the first bounded batch",
            {"selected": selected, "before": before, "after": after},
        )
        selected_again, _, _ = refresh._select_fair_batch(claims, limit=2)
        expect(
            {item["key"] for item in selected_again} == {"a:lower", "b:unknown"},
            "cursor rotation reaches every owner and unknown claim",
            selected_again,
        )

    print("Belief economy smoke passed: 7/7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
