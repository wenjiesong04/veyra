#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.general_situation_contract import stable_digest  # noqa: E402
from runtime.attention_hypothesis_runtime import (  # noqa: E402
    AttentionHypothesisRuntime,
)
from scripts.attention_hypothesis_smoke import (  # noqa: E402
    CONFIRMING_VALUES,
    LOW_VALUES,
    MutableClock,
    assessment_for,
    parent_for,
    persist_parent,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def signal(
    *,
    kind: str,
    signal_id: str,
    target_hypothesis_id: str,
    target_hypothesis_revision: int,
    observed_at: str,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "schema_version": AttentionHypothesisRuntime.LIFECYCLE_SIGNAL_SCHEMA_VERSION,
        "signal_id": signal_id,
        "kind": kind,
        "target_hypothesis_id": target_hypothesis_id,
        "target_hypothesis_revision": target_hypothesis_revision,
        "evidence_id": "evidence:" + signal_id,
        "reason_code": reason_code,
        "producer_id": "local_operator",
        "observed_at": observed_at,
    }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-attention-lifecycle-") as raw:
        clock = MutableClock(datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))
        store = WorldStateStore(Path(raw) / "state")
        runtime = AttentionHypothesisRuntime(store, clock=clock)

        contradiction_parent = parent_for(
            user_id="lifecycle-owner",
            session_id="lifecycle-session",
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-lifecycle-contradiction",
        )
        contradiction_assessment = assessment_for(
            contradiction_parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        )
        persist_parent(store, contradiction_parent)
        admitted = runtime.observe(contradiction_parent, contradiction_assessment)
        hypothesis = admitted["hypothesis"]
        hypothesis_id = hypothesis["hypothesis_id"]
        hypothesis_revision = hypothesis["hypothesis_revision"]
        contradiction = signal(
            kind="contradiction",
            signal_id="als_contradiction_1",
            target_hypothesis_id=hypothesis_id,
            target_hypothesis_revision=hypothesis_revision,
            observed_at=clock().isoformat(),
            reason_code="direct_counter_observation",
        )
        before_contradiction = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        contradicted = runtime.observe(
            contradiction_parent,
            contradiction_assessment,
            lifecycle_signal=contradiction,
        )
        after_contradiction = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        expect(
            contradicted.get("status") == "contradicted"
            and contradicted["hypothesis"]["unknowns"][-1].startswith(
                "typed_contradiction:"
            )
            and after_contradiction == before_contradiction + 1,
            "typed contradiction terminalizes the exact current hypothesis",
            contradicted,
        )
        locked_before = after_contradiction
        locked = runtime.observe(contradiction_parent, contradiction_assessment)
        locked_after = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        expect(
            locked.get("status") == "contradicted"
            and locked.get("reason") == "attention_hypothesis_terminal_non_revivable"
            and locked_after == locked_before,
            "a contradicted hypothesis cannot be revived by an untyped later assessment",
            locked,
        )
        contradiction_replay = runtime.observe(
            contradiction_parent,
            contradiction_assessment,
            lifecycle_signal=contradiction,
        )
        expect(
            contradiction_replay.get("replayed") is True
            and contradiction_replay.get("status") == "contradicted"
            and int(
                store.read_json("attention_hypothesis_state.json").get(
                    "_state_revision"
                )
                or 0
            )
            == locked_after,
            "typed contradiction replay is durable and byte-pure",
            contradiction_replay,
        )

        supersede_parent = parent_for(
            user_id="lifecycle-owner",
            session_id="lifecycle-session",
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-lifecycle-old",
        )
        supersede_assessment = assessment_for(
            supersede_parent,
            values=LOW_VALUES,
            store=store,
            clock=clock,
        )
        persist_parent(store, supersede_parent)
        old = runtime.observe(supersede_parent, supersede_assessment)
        old_id = old["hypothesis"]["hypothesis_id"]
        replacement_parent = parent_for(
            user_id="lifecycle-owner",
            session_id="lifecycle-session",
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-lifecycle-replacement",
        )
        replacement_assessment = assessment_for(
            replacement_parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        )
        persist_parent(store, replacement_parent)
        supersede = signal(
            kind="supersede",
            signal_id="als_supersede_1",
            target_hypothesis_id=old_id,
            target_hypothesis_revision=old["hypothesis"]["hypothesis_revision"],
            observed_at=clock().isoformat(),
            reason_code="parent_superseded",
        )
        replacement = runtime.observe(
            replacement_parent,
            replacement_assessment,
            lifecycle_signal=supersede,
        )
        all_items = runtime.list_for_owner(
            user_id="lifecycle-owner",
            session_id="lifecycle-session",
        )["items"]
        old_item = next(item for item in all_items if item["hypothesis_id"] == old_id)
        new_item = next(
            item
            for item in all_items
            if item["hypothesis_id"] != old_id
            and item["general_situation_id"] == "gsit-lifecycle-replacement"
        )
        expect(
            replacement.get("status") == "confirmed"
            and old_item.get("status") == "superseded"
            and replacement["hypothesis"]["hypothesis_id"] == new_item["hypothesis_id"],
            "explicit supersede creates a new identity and terminalizes the old one",
            {"replacement": replacement, "old": old_item},
        )
        replay = runtime.observe(
            replacement_parent,
            replacement_assessment,
            lifecycle_signal=supersede,
        )
        expect(
            replay.get("replayed") is True
            and replay.get("hypothesis", {}).get("hypothesis_id")
            == replacement["hypothesis"]["hypothesis_id"],
            "supersede signal replay preserves the replacement binding",
            replay,
        )
        state = store.read_json("attention_hypothesis_state.json")
        expect(
            state.get("lifecycle_event_count") == 2
            and state.get("status_counts", {}).get("superseded") == 1
            and state.get("status_counts", {}).get("contradicted") == 1,
            "typed lifecycle events and terminal counts are durable",
            state.get("status_counts"),
        )
    print("Attention lifecycle smoke passed: 6/6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
