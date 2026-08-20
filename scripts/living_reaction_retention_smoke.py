#!/usr/bin/env python3
"""Focused capacity/replay smoke for the V1 Living Reaction archive.

This is intentionally a small fault-injection harness.  It exercises the
retention boundary without external delivery, model calls, or network state.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from runtime.living_reaction_runtime import (  # noqa: E402
    LivingReactionConflict,
    LivingReactionRuntime,
    LivingReactionStorageError,
)
from scripts.living_reaction_smoke import SCENARIOS, feedback, payload  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, len(path.read_bytes()))
        for path in root.rglob("*")
        if path.is_file() and ".veyra-writer.lock" not in str(path)
    }


def build_revisions(runtime: LivingReactionRuntime, *, owner: str, session: str, situation_id: str, count: int) -> list[dict[str, Any]]:
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    decisions: list[dict[str, Any]] = []
    for revision in range(1, count + 1):
        decisions.append(
            runtime.evaluate(
                payload(
                    SCENARIOS[0],
                    owner=owner,
                    session=session,
                    situation_id=situation_id,
                    revision=revision,
                    now=now,
                    need=False,
                    material=True,
                )
            )["decision"]
        )
    return decisions


def main() -> int:
    owner = "retention-owner"
    session = "retention-session"
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    with TemporaryDirectory(prefix="veyra-living-reaction-retention-") as temporary:
        root = Path(temporary) / "state"
        store = WorldStateStore(root)
        runtime = LivingReactionRuntime(store)
        decisions = build_revisions(runtime, owner=owner, session=session, situation_id="current", count=130)
        archive = runtime.archive.read()
        current = runtime.get_reaction(decisions[-1]["reaction_id"], owner_id=owner, session_id=session)
        archived = next(iter(archive["reactions"].values()))
        expect(current and current["situation_revision"] == 130, "authoritative current revision stays hot")
        expect(archived["situation_revision"] < 130, "old reaction is moved to archive")

        before = snapshot(root)
        replay = runtime.get_reaction(archived["reaction_id"], owner_id=owner, session_id=session)
        expect(replay and replay["reaction_id"] == archived["reaction_id"], "exact reaction replay searches archive")
        expect(snapshot(root) == before, "reaction replay is byte/mtime pure")

        for index, decision in enumerate(decisions, start=1):
            runtime.record_feedback(
                feedback(
                    feedback_id=f"retention-feedback-{index}",
                    label="useful",
                    decision=decision,
                    now=now,
                )
            )
        archive = runtime.archive.read()
        archived_feedback = next(iter(archive["feedback"].values()))
        feedback_reaction = runtime.get_reaction(
            archived_feedback["semantics"]["reaction_id"],
            owner_id=owner,
            session_id=session,
        )
        expect(feedback_reaction is not None, "archived feedback target remains replayable")
        before = snapshot(root)
        duplicate = runtime.record_feedback(
            feedback(
                feedback_id=archived_feedback["feedback_id"],
                label=archived_feedback["semantics"]["label"],
                decision=feedback_reaction,
                now=now,
            )
        )
        expect(duplicate["status"] == "duplicate", "exact feedback replay searches archive")
        expect(snapshot(root) == before, "feedback replay does not duplicate effects or policy")
        expect(runtime.status()["feedback_count"] == 130, "more than 123 same-kind feedback remains durable")

        wrong_scope = runtime.get_reaction(archived["reaction_id"], owner_id="other-owner", session_id=session)
        expect(wrong_scope is None, "archived reaction is owner/session scoped")
        try:
            runtime.record_feedback(
                {
                    **archived_feedback["semantics"],
                    "owner_id": "other-owner",
                    "now": now.isoformat(),
                }
            )
        except (LivingReactionConflict, LivingReactionStorageError):
            pass
        else:
            raise AssertionError("cross-scope feedback unexpectedly succeeded")
        print("PASS archived feedback scope fails closed")

        tamper_root = Path(temporary) / "tamper-state"
        tamper_store = WorldStateStore(tamper_root)
        tamper_runtime = LivingReactionRuntime(tamper_store)
        tamper_decisions = build_revisions(tamper_runtime, owner=owner, session=session, situation_id="tamper", count=121)
        manifest = tamper_store.read_json(tamper_runtime.archive.MANIFEST_FILE)
        reference = manifest["segments"][0]
        segment_path = tamper_store.path_for(tamper_runtime.archive.MANIFEST_FILE).parent / reference["path"]
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        segment["entries"]["reactions"][0]["title"] = "tampered"
        segment_path.write_text(json.dumps(segment, ensure_ascii=False), encoding="utf-8")
        expect(tamper_runtime.status()["status"] == "degraded", "archive chain tamper fails closed")

        failure_root = Path(temporary) / "failure-state"
        failure_store = WorldStateStore(failure_root)
        failure_runtime = LivingReactionRuntime(failure_store)
        build_revisions(failure_runtime, owner=owner, session=session, situation_id="write-failure", count=120)
        state_before = snapshot(failure_root)
        original_append = failure_runtime.archive.append

        def fail_append(*_: Any, **__: Any) -> dict[str, Any]:
            raise LivingReactionStorageError("injected archive write failure")

        failure_runtime.archive.append = fail_append  # type: ignore[method-assign]
        try:
            failure_runtime.evaluate(
                payload(
                    SCENARIOS[0],
                    owner=owner,
                    session=session,
                    situation_id="write-failure",
                    revision=121,
                    now=now,
                    need=False,
                    material=True,
                )
            )
        except LivingReactionStorageError:
            pass
        else:
            raise AssertionError("archive write failure did not fail closed")
        expect(snapshot(failure_root) == state_before, "archive write failure leaves hot state unchanged")
        failure_runtime.archive.append = original_append  # type: ignore[method-assign]

        protected_root = Path(temporary) / "protected-state"
        protected_store = WorldStateStore(protected_root)
        protected_runtime = LivingReactionRuntime(protected_store)
        for index in range(1, 121):
            protected_runtime.evaluate(
                payload(
                    SCENARIOS[0],
                    owner=owner,
                    session=session,
                    situation_id=f"protected-{index}",
                    revision=1,
                    now=now,
                    need=False,
                    material=True,
                )
            )
        try:
            protected_runtime.evaluate(
                payload(
                    SCENARIOS[0],
                    owner=owner,
                    session=session,
                    situation_id="protected-overflow",
                    revision=1,
                    now=now,
                    need=False,
                    material=True,
                )
            )
        except LivingReactionStorageError:
            pass
        else:
            raise AssertionError("all-protected hot capacity unexpectedly accepted a row")
        print("PASS all-protected hot capacity fails closed")

        current_root = Path(temporary) / "current-feedback-state"
        current_store = WorldStateStore(current_root)
        current_runtime = LivingReactionRuntime(current_store)
        current_decision = build_revisions(current_runtime, owner=owner, session=session, situation_id="current-feedback", count=1)[0]
        build_revisions(current_runtime, owner=owner, session=session, situation_id="other", count=201)
        current_feedback = current_runtime.record_feedback(
            feedback(
                feedback_id="current-after-201",
                label="useful",
                decision=current_decision,
                now=now,
            )
        )
        expect(current_feedback["status"] == "recorded", "current feedback remains addressable after 201 other reactions")
        expect(current_runtime.get_reaction(current_decision["reaction_id"], owner_id=owner, session_id=session) is not None, "current authoritative reaction remains protected")

    print("RESULT living reaction retention smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
