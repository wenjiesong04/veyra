#!/usr/bin/env python3
"""A model inference must never satisfy the confirmation threshold.

Before this gate, an evidence unit carried producer, fact kind and time bucket
but no epistemic dimension. Diversity was met when any one of those three had
two distinct values, so a single model-derived observation with its own
producer id could confirm a hypothesis on its own. Confirmation would then rest
on something nobody observed.

The rule is: a producer declares how it knows (`observed`, `inference`,
`prediction`); the server derives `is_fact`; and only a direct observation is
counted toward confirmation. Inferences are retained and reported, not silently
dropped, so an operator can see what was excluded and why.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.general_attention_scheduler import GeneralAttentionScheduler  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.structured_observation import (  # noqa: E402
    StructuredObservationFacts,
)
from runtime.attention_hypothesis_runtime import (  # noqa: E402
    AttentionHypothesisRuntime,
)
from scripts.attention_hypothesis_smoke import (  # noqa: E402
    CONFIRMING_VALUES,
    MutableClock,
    assessment_for,
    parent_for,
    persist_parent,
)


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")


OWNER = "attention-owner"
SESSION = "attention-session"


def fixture_parent(clock: MutableClock) -> dict[str, Any]:
    return parent_for(
        user_id=OWNER,
        session_id=SESSION,
        revision=1,
        child_count=2,
        clock=clock,
    )


def diversity_for(status: str, store: WorldStateStore, clock: MutableClock) -> dict[str, Any]:
    parent = fixture_parent(clock)
    assessment = assessment_for(
        parent,
        values=CONFIRMING_VALUES,
        store=store,
        clock=clock,
        epistemic_status=status,
    )
    return assessment["evidence_diversity"]


def main() -> int:
    # 1. The producer contract requires an explicit epistemic status.
    base_facts = {
        "kind": "risk_signal",
        "state": "degraded",
        "severity": "high",
        "urgency": "immediate",
        "novelty": "new",
        "uncertainty": "low",
        "evidence_quality": "direct",
    }
    try:
        StructuredObservationFacts(**base_facts)
    except Exception:
        pass
    else:
        raise AssertionError("facts without an epistemic status must be rejected")

    observed = StructuredObservationFacts(**base_facts, epistemic_status="observed")
    inferred = StructuredObservationFacts(**base_facts, epistemic_status="inference")
    expect(
        observed.epistemic_status == "observed" and inferred.epistemic_status == "inference",
        "an explicit epistemic status is accepted and preserved",
        (observed.epistemic_status, inferred.epistemic_status),
    )

    # 2. `is_fact` is derived by the server, never accepted from the caller.
    try:
        StructuredObservationFacts(
            **base_facts,
            epistemic_status="observed",
            is_fact=True,
        )
    except Exception:
        pass
    else:
        raise AssertionError("a caller must not be able to assert is_fact")

    with TemporaryDirectory(prefix="veyra-epistemic-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc))
        persist_parent(store, fixture_parent(clock))

        # 3. Direct observations count and satisfy diversity.
        observed_profile = diversity_for("observed", store, clock)
        expect(
            observed_profile["unit_count"] >= 2
            and observed_profile["profile_complete"] is True
            and observed_profile["diversity_requirement_met"] is True,
            "direct observations satisfy the diversity requirement",
            observed_profile,
        )
        expect(
            all(unit["epistemic_status"] == "observed" for unit in observed_profile["units"]),
            "every counted unit is a direct observation",
            observed_profile["units"],
        )
        expect(
            observed_profile["non_observed_excluded_count"] == 0,
            "nothing is excluded when all evidence is observed",
            observed_profile,
        )

        # 4. Inferences count for nothing, no matter how many there are.
        for status in ("inference", "prediction"):
            profile = diversity_for(status, store, clock)
            expect(
                profile["unit_count"] == 0
                and profile["diversity_requirement_met"] is False
                and profile["profile_complete"] is False,
                f"{status} evidence can never satisfy the confirmation threshold",
                profile,
            )
            expect(
                profile["non_observed_excluded_count"] > 0
                and all(
                    item["epistemic_status"] == status
                    for item in profile["non_observed_excluded"]
                ),
                f"{status} evidence is reported as excluded, not silently dropped",
                profile,
            )
            expect(
                "structured_evidence_not_observed" in profile["unknowns"],
                "the exclusion reason is visible in unknowns",
                profile["unknowns"],
            )

        # 5. An inference cannot reach confirmation through the hypothesis runtime.
        hypotheses = AttentionHypothesisRuntime(store, clock=clock)
        parent = fixture_parent(clock)
        inferred_assessment = assessment_for(
            parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
            epistemic_status="inference",
        )
        result = hypotheses.observe(parent, inferred_assessment)
        expect(
            result.get("status") != "confirmed",
            "a hypothesis built only on inference is never confirmed",
            result.get("status"),
        )
        blockers = (
            (result.get("hypothesis") or {})
            .get("attention_readiness", {})
            .get("confirmation_blockers")
        )
        expect(
            isinstance(blockers, list)
            and "structured_evidence_profile_incomplete" in blockers,
            "the blocker explains that the evidence profile is not observed",
            blockers,
        )

        # 6. The verifier independently rejects a forged observed unit.
        forged = dict(observed_profile)
        forged["units"] = [
            {**unit, "epistemic_status": "inference"} for unit in observed_profile["units"]
        ]
        try:
            AttentionHypothesisRuntime._validated_evidence_diversity(
                forged,
                evidence_refs=[],
            )
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("a counted unit claiming inference must be rejected")

        expect(
            GeneralAttentionScheduler.EPISTEMIC_STATUSES
            == frozenset({"observed", "inference", "prediction"}),
            "the epistemic vocabulary stays closed",
            GeneralAttentionScheduler.EPISTEMIC_STATUSES,
        )

    print("epistemic hygiene smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
