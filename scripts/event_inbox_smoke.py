#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from runtime.event_inbox import EventInbox, EventInboxClaimError  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def test_schema_and_normalizer() -> None:
    expected_types = {
        "observation",
        "state_changed",
        "task_progress",
        "task_completed",
        "commitment_due",
        "component_degraded",
        "user_feedback",
    }
    expect(expected_types <= {value.value for value in EventType}, "event types cover active-awareness lifecycle")

    legacy = VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id="legacy-user", session_id="legacy-session"),
        payload={"text": "legacy constructor remains valid"},
    )
    expect(
        legacy.occurred_at == legacy.timestamp
        and bool(legacy.received_at)
        and legacy.correlation_id == legacy.event_id,
        "legacy event constructor receives compatible envelope defaults",
        legacy.to_dict(),
    )
    restored = VeyraEvent.from_dict(
        {
            "type": "user_message",
            "source": {"channel": "api", "user_id": "legacy-user", "session_id": "legacy-session"},
            "payload": {"text": "legacy persisted event"},
            "event_id": "evt_legacy",
            "timestamp": "2026-07-25T07:59:00+00:00",
        }
    )
    expect(
        restored.event_id == "evt_legacy"
        and restored.occurred_at == restored.timestamp
        and restored.correlation_id == "evt_legacy",
        "legacy persisted envelope restores without new required fields",
        restored.to_dict(),
    )

    raw = {
        "type": "observation",
        "source": {"channel": "sensor", "user_id": "user-a", "session_id": "session-a"},
        "payload": {"metric": "cpu", "value": 0.93, "labels": {"component": "openclaw"}},
        "event_id": "evt_observation",
        "timestamp": "2026-07-25T08:00:00+00:00",
        "correlation_id": "corr_task",
        "causation_id": "evt_heartbeat",
        "subject": {"kind": "component", "id": "openclaw"},
        "evidence_refs": [{"kind": "probe", "id": "probe_cpu_1"}],
        "dedupe_key": "cpu:2026-07-25T08:00",
        "occurred_at": "2026-07-25T07:59:58+00:00",
        "received_at": "2026-07-25T08:00:01+00:00",
        "privacy_scope": {"tenant": "user-a", "visibility": "private"},
    }
    normalized = EventNormalizer().normalize(raw)
    expect(normalized.type is EventType.OBSERVATION, "generic normalizer accepts non-message event type")
    expect(
        normalized.to_dict() == raw,
        "generic normalizer preserves correlation, evidence and user-isolation envelope fields",
        normalized.to_dict(),
    )
    typed = EventNormalizer().normalize(
        EventType.TASK_PROGRESS,
        {"task_id": "task-1", "progress": 0.5},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
    )
    expect(typed.type is EventType.TASK_PROGRESS, "generic normalizer accepts EventType enum directly")


def test_durable_inbox(root: Path) -> None:
    clock = MutableClock()
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="event-inbox-smoke")
    inbox = EventInbox(store, clock=clock)
    state = store.read_json("event_inbox.json")
    expect(
        store.relative_path_for("event_inbox.json") == "runtime/event_inbox.json"
        and state.get("schema_version") == "veyra.event_inbox.v1"
        and state.get("ttl_seconds") == 0
        and "event_inbox" in store.read_all(),
        "event inbox is mapped, initialized, durable and visible in world state",
        state,
    )

    normalizer = EventNormalizer()
    event = normalizer.normalize(
        EventType.USER_FEEDBACK,
        {
            "text": "这个建议有帮助",
            "rating": 5,
            "metadata": {
                "message": "private free-form note",
                "api_token": "secret-token-value",
            },
        },
        channel="api",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_feedback_a",
        correlation_id="corr_feedback",
        subject={"kind": "recommendation", "id": "rec-1"},
        evidence_refs=[{"kind": "message", "id": "msg-1"}],
        dedupe_key="feedback-remote-1",
        occurred_at=clock().isoformat(),
        received_at=clock().isoformat(),
        privacy_scope={"tenant": "user-a", "visibility": "private"},
    )
    original = event.to_dict()
    admitted = inbox.enqueue(event)
    expect(admitted.get("status") == "enqueued", "new event is durably admitted", admitted)
    persisted = inbox.get(event.event_id)
    expect(
        isinstance(persisted, dict)
        and "text" not in persisted.get("payload", {})
        and persisted.get("payload", {}).get("text_metadata", {}).get("length")
        == len(original["payload"]["text"]),
        "durable inbox stores free-form text as length metadata",
        persisted,
    )
    persisted_json = str(persisted)
    expect(
        "private free-form note" not in persisted_json
        and "secret-token-value" not in persisted_json
        and persisted.get("payload", {})
        .get("metadata", {})
        .get("message_metadata", {})
        .get("redacted")
        and persisted.get("payload", {}).get("metadata", {}).get("api_token")
        == "<redacted>",
        "nested free text and secret-like fields are minimized before persistence",
        persisted,
    )
    duplicate = normalizer.normalize(
        {
            **original,
            "event_id": "evt_feedback_duplicate",
            "payload": {"text": "duplicate transport delivery"},
        }
    )
    deduped = inbox.enqueue(duplicate)
    expect(
        deduped.get("status") == "duplicate"
        and deduped.get("event_id") == event.event_id
        and inbox.stats().get("total") == 1,
        "dedupe key suppresses a repeat inside the same user boundary",
        deduped,
    )

    other_user = normalizer.normalize(
        {
            **original,
            "event_id": "evt_feedback_b",
            "source": {"channel": "api", "user_id": "user-b", "session_id": "session-b"},
            "privacy_scope": {"tenant": "user-b", "visibility": "private"},
        }
    )
    expect(
        inbox.enqueue(other_user).get("status") == "enqueued" and inbox.stats().get("total") == 2,
        "same transport dedupe key remains isolated between users",
    )

    claimed = inbox.claim_by_id(event.event_id, "awareness-shadow", lease_seconds=30)
    expect(claimed == persisted, "claim_by_id returns the durable event envelope without queue metadata", claimed)
    try:
        inbox.complete(event.event_id, "other-consumer")
        raise AssertionError("non-owner unexpectedly completed claim")
    except EventInboxClaimError:
        pass
    expect(
        inbox.get_record(event.event_id).get("status") == "claimed",
        "claim owner is enforced for completion",
    )
    inbox.complete(event.event_id, "awareness-shadow", {"route": "direct_answer"})
    expect(
        inbox.get_record(event.event_id).get("status") == "completed",
        "complete records terminal state by event id",
    )

    user_b_claim = inbox.claim(
        "user-b-worker",
        user_id="user-b",
        session_id="session-b",
        privacy_scope={"tenant": "user-b", "visibility": "private"},
    )
    expect(
        user_b_claim == inbox.get(other_user.event_id),
        "filtered claim preserves and respects user isolation fields",
        user_b_claim,
    )
    failed = inbox.fail(other_user.event_id, "user-b-worker", "transient downstream failure")
    expect(
        failed.get("status") == "pending"
        and inbox.get_record(other_user.event_id).get("last_error") == "transient downstream failure",
        "failed claim returns to retryable pending state",
        failed,
    )
    retry_claim = inbox.claim_by_id(other_user.event_id, "user-b-retry")
    expect(retry_claim == inbox.get(other_user.event_id), "retry claim returns the same durable envelope")
    terminal = inbox.fail(other_user.event_id, "user-b-retry", "permanent failure", retry=False)
    expect(terminal.get("status") == "failed", "explicit non-retry failure is terminal", terminal)
    inbox.retry(other_user.event_id, reset_attempts=True)
    expect(inbox.get_record(other_user.event_id).get("status") == "pending", "manual retry revives failed event")

    reloaded = EventInbox(WorldStateStore(root), clock=clock)
    expect(reloaded.get(event.event_id) == persisted, "event envelope survives store reconstruction")


def test_lease_crash_recovery(root: Path) -> None:
    clock = MutableClock()
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="event-lease-smoke")
    inbox = EventInbox(store, clock=clock)
    event = EventNormalizer().normalize(
        EventType.COMPONENT_DEGRADED,
        {"component": "openclaw", "reason": "health probe timeout"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_degraded",
        dedupe_key="openclaw-degraded-1",
        privacy_scope={"tenant": "user-a", "visibility": "private"},
    )
    inbox.enqueue(event, max_attempts=2)
    expect(inbox.claim_by_id(event.event_id, "worker-before-crash", 5) == event.to_dict(), "event lease is acquired")
    clock.advance(6)

    after_crash = EventInbox(WorldStateStore(root), clock=clock)
    recovery = after_crash.recover_expired_leases()
    expect(
        recovery.get("requeued") == [event.event_id]
        and after_crash.get_record(event.event_id).get("status") == "pending",
        "expired claim is recovered after consumer crash",
        recovery,
    )
    expect(
        after_crash.claim_by_id(event.event_id, "worker-after-crash", 5) == event.to_dict(),
        "recovered event can be claimed by a new consumer",
    )
    after_crash.complete(event.event_id, "worker-after-crash")

    terminal_event = EventNormalizer().normalize(
        EventType.TASK_PROGRESS,
        {"task_id": "task-timeout", "progress": 0.2},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_terminal_lease",
    )
    after_crash.enqueue(terminal_event, max_attempts=1)
    after_crash.claim_by_id(terminal_event.event_id, "worker-timeout", 5)
    clock.advance(6)
    exhausted = after_crash.recover_expired_leases()
    expect(
        exhausted.get("failed") == [terminal_event.event_id]
        and after_crash.get_record(terminal_event.event_id).get("status") == "failed",
        "expired final lease fails instead of retrying forever",
        exhausted,
    )


def test_bounded_terminal_retention(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="event-bounds-smoke")
    inbox = EventInbox(store, max_records=2)
    normalizer = EventNormalizer()
    first = normalizer.normalize(
        EventType.OBSERVATION,
        {"sequence": 1},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_bound_1",
    )
    second = normalizer.normalize(
        EventType.OBSERVATION,
        {"sequence": 2},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_bound_2",
    )
    third = normalizer.normalize(
        EventType.OBSERVATION,
        {"sequence": 3},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_bound_3",
    )
    inbox.enqueue(first)
    inbox.claim_by_id(first.event_id, "bounds-worker")
    inbox.complete(first.event_id, "bounds-worker")
    inbox.enqueue(second)
    admitted = inbox.enqueue(third)
    expect(
        admitted.get("evicted_terminal_event_ids") == [first.event_id]
        and inbox.stats().get("total") == 2
        and inbox.get(first.event_id) is None
        and inbox.get(second.event_id)
        and inbox.get(third.event_id),
        "terminal queue history is bounded without evicting active events",
        {"admission": admitted, "stats": inbox.stats()},
    )


def main() -> int:
    test_schema_and_normalizer()
    with TemporaryDirectory(prefix="veyra-event-inbox-") as temp_dir:
        test_durable_inbox(Path(temp_dir))
    with TemporaryDirectory(prefix="veyra-event-lease-") as temp_dir:
        test_lease_crash_recovery(Path(temp_dir))
    with TemporaryDirectory(prefix="veyra-event-bounds-") as temp_dir:
        test_bounded_terminal_retention(Path(temp_dir))
    print("event inbox smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
