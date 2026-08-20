#!/usr/bin/env python3
"""Adversarial, tempfile-backed checks for the bounded InformationNeed store."""

from __future__ import annotations

import copy
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from runtime.information_need_runtime import (  # noqa: E402
    InformationNeedCapacityError,
    InformationNeedEventConflict,
    InformationNeedRuntime,
    InformationNeedStateError,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(f"{label} failed")
    print(f"PASS {label}")


def candidate(number: int, *, kind: str = "calendar") -> dict[str, object]:
    return {
        "blocked_judgment": f"unknown-{number}",
        "evidence_kind": kind,
        "why_now": "the next observation may change the judgment",
        "urgency": 0.5,
        "expires_at": None,
        "allowed_source_classes": [kind],
        "fallback_reaction": "ask",
        "question": f"what is the value for {number}?",
    }


def candidate_from_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "blocked_judgment": row["blocked_judgment"],
        "evidence_kind": row["evidence_kind"],
        "why_now": row["why_now"],
        "urgency": row["urgency"],
        "expires_at": row["expires_at"],
        "allowed_source_classes": list(row["allowed_source_classes"]),
        "fallback_reaction": row["fallback_reaction"],
        "question": row["question"],
    }


def admit(runtime: InformationNeedRuntime, *, situation: str, owner: str, session: str, number: int, event: str) -> list[dict[str, object]]:
    return runtime.upsert_for_situation(
        situation_id=situation,
        owner_id=owner,
        session_id=session,
        needs=[candidate(number)],
        source_event_id=event,
    )


def main() -> int:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="veyra-information-need-adversarial-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(now)
        runtime = InformationNeedRuntime(store, max_needs=16, max_needs_per_situation=2, clock=clock)

        first = admit(runtime, situation="s1", owner="owner-a", session="session-a", number=1, event="event-1")
        state_path = store.path_for("information_need_state.json")
        before = state_path.read_bytes()
        clock.advance(hours=2)
        replay = admit(runtime, situation="s1", owner="owner-a", session="session-a", number=1, event="event-1")
        expect(replay == first and state_path.read_bytes() == before, "same event replay is byte-pure")
        changed = candidate(1)
        changed["question"] = "rebound content"
        try:
            runtime.upsert_for_situation(situation_id="s1", owner_id="owner-a", session_id="session-a", needs=[changed], source_event_id="event-1")
        except InformationNeedEventConflict:
            pass
        else:
            raise AssertionError("content-rebind conflict was not rejected")
        expect(state_path.read_bytes() == before, "content-rebind conflict leaves bytes unchanged")

        admit(runtime, situation="s1", owner="owner-a", session="session-a", number=2, event="event-2")
        blocked_before = state_path.read_bytes()
        try:
            admit(runtime, situation="s1", owner="owner-a", session="session-a", number=3, event="event-3")
        except InformationNeedCapacityError:
            pass
        else:
            raise AssertionError("active/open cap did not fail closed")
        expect(state_path.read_bytes() == blocked_before, "active/open capacity failure preserves all rows")

        rows = runtime.list(owner_id="owner-a", session_id="session-a", situation_id="s1")
        runtime.resolve(rows[0]["need_id"], owner_id="owner-a", session_id="session-a", answered_by_event_id="answer-1")
        compacted = admit(runtime, situation="s1", owner="owner-a", session="session-a", number=3, event="event-3")
        expect(len(compacted) == 2 and all(row["status"] == "open" for row in compacted), "terminal-first per-situation compaction keeps active rows")
        durable = store.read_json("information_need_state.json")
        expect(durable["retention_events"] and durable["retention_event_count"] == len(durable["retention_events"]), "terminal eviction has bounded audit")

        # A terminal record can be observed again only through a new event,
        # which creates a generation instead of mutating a replay.
        terminal = compacted[0]
        runtime.resolve(terminal["need_id"], owner_id="owner-a", session_id="session-a", answered_by_event_id="answer-2")
        waiting_before = state_path.read_bytes()
        try:
            runtime.mark_waiting(
                terminal["need_id"],
                owner_id="owner-a",
                session_id="session-a",
                event_id="wait-after-terminal",
            )
        except StateRevisionConflictError:
            expect(True, "terminal Need cannot be restored by waiting")
        else:  # pragma: no cover
            raise AssertionError("terminal Need was restored by waiting")
        expect(state_path.read_bytes() == waiting_before, "terminal waiting rejection is byte-pure")
        reopened = runtime.upsert_for_situation(
            situation_id="s1",
            owner_id="owner-a",
            session_id="session-a",
            needs=[candidate_from_row(terminal)],
            source_event_id="event-4",
        )
        reopened_row = next(row for row in reopened if row["need_id"] == terminal["need_id"])
        expect(reopened_row["generation"] == terminal["generation"] + 1 and reopened_row["status"] == "open", "new event reopens a terminal generation")
        stale_ask_before = state_path.read_bytes()
        try:
            runtime.mark_asked(
                terminal["need_id"],
                owner_id="owner-a",
                session_id="session-a",
                event_id="ask-from-stale-generation",
                expected_generation=int(terminal["generation"]),
            )
        except StateRevisionConflictError:
            expect(True, "an ask decision cannot mutate a newer Need generation")
        else:  # pragma: no cover
            raise AssertionError("stale ask mutated a newer Need generation")
        expect(state_path.read_bytes() == stale_ask_before, "stale ask generation rejection is byte-pure")

        # Replay evidence is independent from the 32-row presentation index.
        replay_store = WorldStateStore(Path(tmp) / "replay-index")
        replay_runtime = InformationNeedRuntime(replay_store, max_needs=8, max_needs_per_situation=2, clock=clock)
        for index in range(40):
            admit(replay_runtime, situation="replay", owner="owner-r", session="session-r", number=1, event=f"replay-{index}")
        replay_state = replay_store.read_json("information_need_state.json")
        replay_id = next(iter(replay_state["needs"]))
        expect(
            len(replay_state["event_fingerprints"][replay_id]) == 32
            and len(replay_state["event_replay_evidence"][replay_id]) == 40,
            "Need replay evidence outlives the 32-row display index",
        )
        replay_before = replay_store.path_for("information_need_state.json").read_bytes()
        replayed_rows = admit(replay_runtime, situation="replay", owner="owner-r", session="session-r", number=1, event="replay-0")
        expect(replayed_rows[0]["generation"] == 1 and replay_store.path_for("information_need_state.json").read_bytes() == replay_before, "old Need event replay remains byte-pure")

        bounded_store = WorldStateStore(Path(tmp) / "bounded-replay")
        bounded_runtime = InformationNeedRuntime(
            bounded_store,
            max_needs=8,
            max_needs_per_situation=2,
            max_replay_evidence_per_need=4,
            clock=clock,
        )
        for index in range(4):
            admit(bounded_runtime, situation="bounded", owner="owner-b", session="session-b", number=1, event=f"bounded-{index}")
        bounded_path = bounded_store.path_for("information_need_state.json")
        bounded_before = bounded_path.read_bytes()
        try:
            admit(bounded_runtime, situation="bounded", owner="owner-b", session="session-b", number=1, event="bounded-over-cap")
        except InformationNeedCapacityError:
            pass
        else:
            raise AssertionError("replay evidence silently exceeded its per-Need cap")
        bounded_state = bounded_store.read_json("information_need_state.json")
        bounded_id = next(iter(bounded_state["needs"]))
        expect(
            len(bounded_state["event_replay_evidence"][bounded_id]) == 4
            and bounded_path.read_bytes() == bounded_before,
            "replay cap rejects a new event byte-pure without evicting old evidence",
        )
        exact_at_cap = admit(bounded_runtime, situation="bounded", owner="owner-b", session="session-b", number=1, event="bounded-0")
        expect(exact_at_cap[0]["generation"] == 1 and bounded_path.read_bytes() == bounded_before, "exact replay remains allowed at the replay cap")

        global_runtime = InformationNeedRuntime(store, max_needs=3, max_needs_per_situation=3, clock=clock)
        admit(global_runtime, situation="s2", owner="owner-a", session="session-a", number=10, event="event-10")
        s2_rows = global_runtime.list(owner_id="owner-a", session_id="session-a", situation_id="s2")
        global_runtime.resolve(s2_rows[0]["need_id"], owner_id="owner-a", session_id="session-a", answered_by_event_id="answer-10")
        admit(global_runtime, situation="s3", owner="owner-a", session="session-a", number=11, event="event-11")
        expect(len(global_runtime.list(owner_id="owner-a", session_id="session-a", limit=20)) <= 3, "global cap remains bounded")

        # Every malformed row is rejected by list/read, rather than filtered
        # out and hidden from an operator.
        good_state = copy.deepcopy(store.read_json("information_need_state.json"))
        store.mutate_json("information_need_state.json", lambda state: state["needs"].update({"bad": {}}) or state)
        try:
            runtime.list(owner_id="owner-a", session_id="session-a")
        except InformationNeedStateError:
            pass
        else:
            raise AssertionError("malformed InformationNeed row was accepted")
        store.mutate_json("information_need_state.json", lambda state: good_state)
        store.mutate_json("information_need_state.json", lambda state: (state.pop("schema_version", None), state)[1])
        try:
            runtime.list(owner_id="owner-a", session_id="session-a")
        except InformationNeedStateError:
            pass
        else:
            raise AssertionError("missing InformationNeed schema was accepted")
        store.mutate_json("information_need_state.json", lambda state: good_state)

        # WorldStateStore serialises concurrent writers; the runtime still
        # refuses to silently exceed the per-Situation cap.
        concurrent_store = WorldStateStore(Path(tmp) / "concurrent")
        concurrent = InformationNeedRuntime(concurrent_store, max_needs=64, max_needs_per_situation=8, clock=clock)
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [
                pool.submit(admit, concurrent, situation="concurrent", owner="owner-c", session="session-c", number=index, event=f"event-c-{index}")
                for index in range(40)
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except InformationNeedCapacityError:
                    pass
                except BaseException as exc:  # pragma: no cover - diagnostic guard.
                    errors.append(exc)
        expect(not errors, f"concurrent writers only fail with bounded capacity: {errors}")
        final = concurrent.list(owner_id="owner-c", session_id="session-c", situation_id="concurrent", limit=100)
        expect(len(final) <= 8, "concurrent per-situation cap is enforced")
        expect(all(row["status"] in {"open", "asked", "observing", "waiting"} for row in final), "concurrent cap does not delete active rows")

    print("RESULT InformationNeed adversarial smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
