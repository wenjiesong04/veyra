#!/usr/bin/env python3
"""Belief claims that can never be read back must not consume the claim budget.

``claim_identity_key`` returns ``None`` for exactly the cases that
``item_visible_to_scope`` refuses: invalid owner, invalid scope kind, and
tenant-scoped claims without an exact owner. Before this gate, such a claim was
appended on every observation because the replacement loop could never match a
``None`` identity. A single repeating probe filled the bounded 250-claim budget
and evicted usable claims.

This gate pins the fail-closed behaviour and the non-regression of ordinary
scoped upserts.
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


def unscoped_probe_claim() -> dict[str, Any]:
    """Reproduce the real shape observed in state/local/belief_state.json."""

    from awareness.claim_schema import make_claim

    claim = make_claim(
        key="web_probe:web:status",
        claim="Web probe needs an http or https URL.",
        confidence=0.5,
        source="web_probe",
        ttl_seconds=60,
        evidence={"status": "missing_target"},
    )
    claim.update({"scope_kind": "tenant", "user_id": None, "session_id": None})
    return claim


def scoped_claim(value: str) -> dict[str, Any]:
    from awareness.claim_schema import make_claim

    claim = make_claim(
        key="local_system:platform",
        claim=value,
        confidence=0.95,
        source="system_probe",
        ttl_seconds=300,
        evidence={"status": value},
    )
    claim.update(
        {
            "scope_kind": "tenant",
            "tenant_derived": True,
            "user_id": "user_a",
            "session_id": "session_a",
        }
    )
    return claim


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["VEYRA_STATE_ROOT"] = tmp
        os.environ["VEYRA_AGENCY_ROOT"] = str(Path(tmp) / "agency")

        from awareness.belief_core import BeliefCore
        from awareness.claim_schema import claim_identity_key
        from core.context_scope import item_visible_to_scope
        from core.world_state import WorldStateStore

        store = WorldStateStore(Path(tmp))
        belief = BeliefCore(store)

        unscoped = unscoped_probe_claim()
        expect(
            claim_identity_key(unscoped) is None,
            "the reproduced claim really has no scope identity",
            unscoped,
        )
        expect(
            not item_visible_to_scope(unscoped, user_id="user_a", session_id="session_a"),
            "a claim with no identity is also never visible to any tenant",
            unscoped,
        )

        for _ in range(50):
            result = belief.upsert_claim(unscoped_probe_claim())
            expect(
                result.get("status") == "rejected_unscoped" and result.get("persisted") is False,
                "unscoped upsert reports rejection instead of silent success",
                result,
            )

        state = store.read_json("belief_state.json")
        claims = state.get("claims") or []
        expect(
            not claims,
            "50 unscoped observations persist zero claims",
            len(claims),
        )

        rejections = state.get("unscoped_rejections") or {}
        expect(
            rejections.get("count") == 50,
            "every rejection is counted rather than dropped silently",
            rejections,
        )
        expect(
            (rejections.get("by_source") or {}).get("web_probe") == 50,
            "rejections are attributed to the producing source",
            rejections,
        )
        expect(
            rejections.get("last_key") == "web_probe:web:status",
            "the last rejected key stays diagnosable",
            rejections,
        )

        # Non-regression: an ordinary owner-scoped claim still upserts in place.
        for index in range(30):
            belief.upsert_claim(scoped_claim(f"observation-{index}"))

        state = store.read_json("belief_state.json")
        claims = [item for item in state.get("claims") or [] if isinstance(item, dict)]
        platform = [item for item in claims if item.get("key") == "local_system:platform"]
        expect(
            len(platform) == 1,
            "repeated scoped observations replace one row instead of appending",
            len(platform),
        )
        expect(
            platform[0].get("claim") == "observation-29",
            "the surviving row holds the newest observation",
            platform[0].get("claim"),
        )
        expect(
            int(platform[0].get("refresh_count") or 0) == 29,
            "refresh_count still tracks replacement history",
            platform[0].get("refresh_count"),
        )
        expect(
            item_visible_to_scope(platform[0], user_id="user_a", session_id="session_a"),
            "the retained scoped claim remains readable by its owner",
            platform[0],
        )
        expect(
            not item_visible_to_scope(platform[0], user_id="user_b", session_id="session_a"),
            "the retained scoped claim stays invisible to another user",
            platform[0],
        )

        # The bounded budget is now spent only on readable claims.
        expect(
            all(claim_identity_key(item) is not None for item in claims),
            "no stored claim lacks a scope identity",
            [item.get("key") for item in claims if claim_identity_key(item) is None],
        )

    print("belief scope retention smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
