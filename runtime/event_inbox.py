from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import EventType, VeyraEvent


EVENT_INBOX_FILE = "event_inbox.json"
EVENT_INBOX_SCHEMA = "veyra.event_inbox.v1"
FREE_TEXT_KEYS = {
    "body",
    "content",
    "message",
    "prompt",
    "query",
    "response",
    "text",
}
MAX_ENVELOPE_BYTES = 512 * 1024


class EventInboxError(RuntimeError):
    """Base error for durable event inbox operations."""


class EventInboxConflictError(EventInboxError):
    """Raised when an event identity is reused for a different envelope."""


class EventInboxNotFoundError(EventInboxError):
    """Raised when an event id is not present in the inbox."""


class EventInboxClaimError(EventInboxError):
    """Raised when a consumer tries to finish a claim it does not own."""


class EventInbox:
    """Durable, deduplicated event inbox with leased consumer claims.

    The durable record keeps queue metadata separate from the event envelope.
    ``claim`` and ``claim_by_id`` return only that envelope so source,
    user/session isolation, privacy, correlation and evidence fields survive
    across persistence and crash recovery. Free-form ``payload.text`` is
    represented by redaction/length metadata instead of being duplicated at
    rest.
    """

    def __init__(
        self,
        store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
        default_max_attempts: int = 3,
        max_records: int = 500,
    ) -> None:
        if default_max_attempts < 1:
            raise ValueError("default_max_attempts must be at least 1")
        if max_records < 1:
            raise ValueError("max_records must be at least 1")
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.default_max_attempts = int(default_max_attempts)
        self.max_records = int(max_records)

    def enqueue(
        self,
        event: VeyraEvent | Mapping[str, Any],
        *,
        max_attempts: int | None = None,
        available_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Persist an event once and return its queue admission result."""
        envelope, immutable_fingerprint = self._prepared_event(event)
        event_id = str(envelope.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event envelope requires event_id")
        attempts_limit = int(
            self.default_max_attempts
            if max_attempts is None
            else max_attempts
        )
        if attempts_limit < 1:
            raise ValueError("max_attempts must be at least 1")
        now = self._now()
        available = self._coerce_time(available_at, default=now)
        identity = self._dedupe_identity(envelope)
        selected: dict[str, Any] = {}

        def admit(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            selected.update(
                self._admit(
                    document,
                    envelope=envelope,
                    event_id=event_id,
                    identity=identity,
                    immutable_fingerprint=immutable_fingerprint,
                    attempts_limit=attempts_limit,
                    available=available,
                    now=now,
                )
            )
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, admit)
        return copy.deepcopy(selected)

    def enqueue_and_claim(
        self,
        event: VeyraEvent | Mapping[str, Any],
        consumer_id: str,
        lease_seconds: float = 60.0,
        *,
        max_attempts: int | None = None,
        available_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Atomically admit an event and acquire its foreground consumer lease.

        ``status`` retains the normal admission status (``enqueued`` or
        ``duplicate``). ``canonical_event_id`` and ``canonical_envelope`` are
        always the dedupe-resolved durable record. ``claimed`` and
        ``claimed_envelope`` separately report whether this consumer owns the
        lease. The canonical id can differ from the delivered id when a
        user-scoped dedupe key resolves to an existing record.
        """

        envelope, immutable_fingerprint = self._prepared_event(event)
        event_id = str(envelope.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event envelope requires event_id")
        consumer = self._consumer(consumer_id)
        lease = self._lease_seconds(lease_seconds)
        attempts_limit = int(
            self.default_max_attempts
            if max_attempts is None
            else max_attempts
        )
        if attempts_limit < 1:
            raise ValueError("max_attempts must be at least 1")
        now = self._now()
        available = self._coerce_time(available_at, default=now)
        identity = self._dedupe_identity(envelope)
        selected: dict[str, Any] = {}

        def admit_and_claim(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            self._recover_expired(document, now)
            admission = self._admit(
                document,
                envelope=envelope,
                event_id=event_id,
                identity=identity,
                immutable_fingerprint=immutable_fingerprint,
                attempts_limit=attempts_limit,
                available=available,
                now=now,
            )
            canonical_id = str(admission.get("event_id") or event_id)
            record = document["events"].get(canonical_id)
            claimed_envelope: dict[str, Any] | None = None
            claim_status = "unavailable"
            if isinstance(record, dict):
                if record.get("status") == "claimed" and record.get("claimed_by") == consumer:
                    candidate = record.get("envelope")
                    if isinstance(candidate, dict):
                        claimed_envelope = copy.deepcopy(candidate)
                        claim_status = "already_claimed"
                elif (
                    record.get("status") == "pending"
                    and self._available(record, now)
                    and isinstance(record.get("envelope"), dict)
                ):
                    self._claim_record(
                        record,
                        consumer=consumer,
                        lease_seconds=lease,
                        now=now,
                    )
                    candidate = record.get("envelope")
                    if isinstance(candidate, dict):
                        claimed_envelope = copy.deepcopy(candidate)
                        claim_status = "claimed"
            selected.update(admission)
            selected.update(
                canonical_event_id=canonical_id,
                canonical_envelope=copy.deepcopy(admission.get("envelope")),
                claimed=claimed_envelope is not None,
                claim_status=claim_status,
                claimed_envelope=claimed_envelope,
            )
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, admit_and_claim)
        return copy.deepcopy(selected)

    def claim(
        self,
        consumer_id: str,
        lease_seconds: float = 60.0,
        *,
        event_types: Iterable[EventType | str] | EventType | str | None = None,
        channel: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        privacy_scope: Any = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest available matching event and return its envelope."""
        consumer = self._consumer(consumer_id)
        lease = self._lease_seconds(lease_seconds)
        accepted_types = self._event_types(event_types)
        selected: dict[str, Any] = {}
        now = self._now()

        def claim_oldest(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            self._recover_expired(document, now)
            candidates: list[dict[str, Any]] = []
            for record in document["events"].values():
                if not isinstance(record, dict) or record.get("status") != "pending":
                    continue
                if not self._available(record, now):
                    continue
                envelope = record.get("envelope")
                if not isinstance(envelope, dict):
                    continue
                if not self._matches(
                    envelope,
                    event_types=accepted_types,
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    privacy_scope=privacy_scope,
                ):
                    continue
                candidates.append(record)
            if not candidates:
                return document
            candidates.sort(key=self._queue_order)
            claimed = candidates[0]
            self._claim_record(claimed, consumer=consumer, lease_seconds=lease, now=now)
            selected["envelope"] = copy.deepcopy(claimed["envelope"])
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, claim_oldest)
        envelope = selected.get("envelope")
        return copy.deepcopy(envelope) if isinstance(envelope, dict) else None

    def claim_by_id(
        self,
        event_id: str,
        consumer_id: str,
        lease_seconds: float = 60.0,
    ) -> dict[str, Any] | None:
        """Claim one known event for synchronous/shadow processing."""
        selected_id = self._event_id(event_id)
        consumer = self._consumer(consumer_id)
        lease = self._lease_seconds(lease_seconds)
        selected: dict[str, Any] = {}
        now = self._now()

        def claim_selected(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            self._recover_expired(document, now)
            record = document["events"].get(selected_id)
            if not isinstance(record, dict):
                return document
            if record.get("status") == "claimed" and record.get("claimed_by") == consumer:
                selected["envelope"] = copy.deepcopy(record.get("envelope"))
                return document
            if record.get("status") != "pending" or not self._available(record, now):
                return document
            self._claim_record(record, consumer=consumer, lease_seconds=lease, now=now)
            selected["envelope"] = copy.deepcopy(record.get("envelope"))
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, claim_selected)
        envelope = selected.get("envelope")
        return copy.deepcopy(envelope) if isinstance(envelope, dict) else None

    def complete(
        self,
        event_id: str,
        consumer_id: str,
        result: Any = None,
    ) -> dict[str, Any]:
        """Complete a currently leased event by event id."""
        selected_id = self._event_id(event_id)
        consumer = self._consumer(consumer_id)
        completion_result = self._json_value(result)
        selected: dict[str, Any] = {}
        now = self._now()

        def finish(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            record = self._record(document, selected_id)
            if record.get("status") == "completed" and record.get("completed_by") == consumer:
                selected["envelope"] = copy.deepcopy(record.get("envelope"))
                return document
            self._require_active_claim(record, consumer=consumer, now=now)
            record.update(
                {
                    "status": "completed",
                    "completed_by": consumer,
                    "completed_at": self._iso(now),
                    "completion_result": completion_result,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                }
            )
            selected["envelope"] = copy.deepcopy(record.get("envelope"))
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, finish)
        return copy.deepcopy(selected["envelope"])

    def fail(
        self,
        event_id: str,
        consumer_id: str,
        error: BaseException | str,
        *,
        retry: bool = True,
        retry_delay_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Record a failed attempt and either retry or terminally fail the event."""
        selected_id = self._event_id(event_id)
        consumer = self._consumer(consumer_id)
        delay = max(0.0, float(retry_delay_seconds))
        error_text = self._error_text(error)
        selected: dict[str, Any] = {}
        now = self._now()

        def record_failure(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            record = self._record(document, selected_id)
            self._require_active_claim(record, consumer=consumer, now=now)
            attempts = int(record.get("attempts") or 0)
            max_attempts = max(1, int(record.get("max_attempts") or self.default_max_attempts))
            should_retry = bool(retry and attempts < max_attempts)
            record.update(
                {
                    "status": "pending" if should_retry else "failed",
                    "available_at": self._iso(now + timedelta(seconds=delay)),
                    "last_error": error_text,
                    "last_failed_at": self._iso(now),
                    "failed_by": None if should_retry else consumer,
                    "failed_at": None if should_retry else self._iso(now),
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                }
            )
            selected["envelope"] = copy.deepcopy(record.get("envelope"))
            selected["status"] = record["status"]
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, record_failure)
        return {
            "status": selected["status"],
            "event_id": selected_id,
            "envelope": copy.deepcopy(selected["envelope"]),
        }

    def retry(
        self,
        event_id: str,
        *,
        delay_seconds: float = 0.0,
        reset_attempts: bool = False,
    ) -> dict[str, Any]:
        """Manually make a non-completed, non-claimed event available again."""
        selected_id = self._event_id(event_id)
        delay = max(0.0, float(delay_seconds))
        selected: dict[str, Any] = {}
        now = self._now()

        def make_pending(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            record = self._record(document, selected_id)
            status = str(record.get("status") or "")
            if status == "completed":
                raise EventInboxClaimError(f"completed event {selected_id} cannot be retried")
            if status == "claimed":
                raise EventInboxClaimError(f"claimed event {selected_id} must fail or expire before retry")
            record.update(
                {
                    "status": "pending",
                    "available_at": self._iso(now + timedelta(seconds=delay)),
                    "failed_by": None,
                    "failed_at": None,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                }
            )
            if reset_attempts:
                record["attempts"] = 0
            selected["envelope"] = copy.deepcopy(record.get("envelope"))
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, make_pending)
        return copy.deepcopy(selected["envelope"])

    def recover_expired_leases(self) -> dict[str, list[str]]:
        """Recover claims abandoned by a crashed or stalled consumer."""
        recovered: dict[str, list[str]] = {"requeued": [], "failed": []}
        now = self._now()

        def recover(document: dict[str, Any]) -> dict[str, Any]:
            self._require_document(document)
            recovered.update(self._recover_expired(document, now))
            return document

        self.store.mutate_json(EVENT_INBOX_FILE, recover)
        return copy.deepcopy(recovered)

    def get(self, event_id: str) -> dict[str, Any] | None:
        """Return the original envelope without queue metadata."""
        record = self.store.read_json(EVENT_INBOX_FILE).get("events", {}).get(str(event_id))
        if not isinstance(record, dict) or not isinstance(record.get("envelope"), dict):
            return None
        return copy.deepcopy(record["envelope"])

    def get_record(self, event_id: str) -> dict[str, Any] | None:
        """Return queue metadata for operations and diagnostics."""
        record = self.store.read_json(EVENT_INBOX_FILE).get("events", {}).get(str(event_id))
        return copy.deepcopy(record) if isinstance(record, dict) else None

    def stats(self) -> dict[str, int]:
        document = self.store.read_json(EVENT_INBOX_FILE)
        counts: dict[str, int] = {}
        events = document.get("events")
        if not isinstance(events, dict):
            return counts
        for record in events.values():
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        counts["total"] = len(events)
        return counts

    def _prepared_event(
        self,
        event: VeyraEvent | Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if isinstance(event, VeyraEvent):
            raw_envelope = event.to_dict()
        elif isinstance(event, Mapping):
            raw_envelope = copy.deepcopy(dict(event))
        else:
            raise TypeError("event must be VeyraEvent or a mapping")
        if not isinstance(raw_envelope.get("source"), dict):
            raise ValueError("event envelope requires a source mapping")
        if not isinstance(raw_envelope.get("payload"), dict):
            raise ValueError("event envelope requires a payload mapping")
        self._json_value(raw_envelope)
        envelope = self._redact_free_text(raw_envelope)
        envelope = redact_sensitive(envelope, max_string=4000, max_list=100)
        self._json_value(envelope)
        # Never persist an unsalted digest derived from raw free text or
        # secret-bearing fields: short values could be brute-forced. The
        # fingerprint deliberately sees only the minimized durable envelope.
        # Consequently, equal-length replacements of redacted free text are
        # indistinguishable; structured immutable fields still detect conflict.
        immutable_fingerprint = self._immutable_fingerprint(envelope)
        envelope_size = len(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if envelope_size > MAX_ENVELOPE_BYTES:
            raise ValueError(
                f"event envelope exceeds the {MAX_ENVELOPE_BYTES}-byte durable inbox limit"
            )
        return envelope, immutable_fingerprint

    def _event_envelope(self, event: VeyraEvent | Mapping[str, Any]) -> dict[str, Any]:
        envelope, _ = self._prepared_event(event)
        return envelope

    @classmethod
    def _redact_free_text(cls, envelope: dict[str, Any]) -> dict[str, Any]:
        def sanitize(value: Any) -> Any:
            if isinstance(value, dict):
                sanitized: dict[str, Any] = {}
                for raw_key, item in value.items():
                    key = str(raw_key)
                    if key.lower() in FREE_TEXT_KEYS and isinstance(item, str):
                        sanitized[f"{key}_metadata"] = {
                            "redacted": True,
                            "length": len(item),
                        }
                    else:
                        sanitized[key] = sanitize(item)
                return sanitized
            if isinstance(value, list):
                return [sanitize(item) for item in value]
            return copy.deepcopy(value)

        result = sanitize(envelope)
        return result if isinstance(result, dict) else {}

    def _admit(
        self,
        document: dict[str, Any],
        *,
        envelope: dict[str, Any],
        event_id: str,
        identity: str,
        immutable_fingerprint: str,
        attempts_limit: int,
        available: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        events = document["events"]
        dedupe_index = document["dedupe_index"]
        received_at = self._delivery_received_at(envelope, now)
        existing = events.get(event_id)
        if isinstance(existing, dict):
            existing_envelope = existing.get("envelope")
            existing_fingerprint = str(existing.get("immutable_fingerprint") or "")
            if existing_fingerprint:
                matches = existing_fingerprint == immutable_fingerprint
            else:
                matches = self._canonical(
                    self._immutable_projection(existing_envelope)
                ) == self._canonical(self._immutable_projection(envelope))
            if not matches:
                raise EventInboxConflictError(
                    f"event id {event_id} is already bound to a different envelope"
                )
            if not existing_fingerprint:
                existing["immutable_fingerprint"] = immutable_fingerprint
            delivery_count = self._record_delivery(existing, received_at=received_at)
            return {
                "status": "duplicate",
                "event_id": event_id,
                "envelope": copy.deepcopy(existing_envelope),
                "delivery_count": delivery_count,
            }

        duplicate_id = str(dedupe_index.get(identity) or "")
        duplicate = events.get(duplicate_id)
        if duplicate_id and isinstance(duplicate, dict):
            delivery_count = self._record_delivery(duplicate, received_at=received_at)
            return {
                "status": "duplicate",
                "event_id": duplicate_id,
                "duplicate_of": duplicate_id,
                "envelope": copy.deepcopy(duplicate.get("envelope")),
                "delivery_count": delivery_count,
            }

        evicted = self._prune_terminal(document, reserve=1)
        events = document["events"]
        dedupe_index = document["dedupe_index"]
        if len(events) >= self.max_records:
            raise EventInboxError(
                "event inbox capacity is exhausted by active events; "
                "consumer recovery or operator intervention is required"
            )
        record = {
            "event_id": event_id,
            "envelope": copy.deepcopy(envelope),
            "dedupe_identity": identity,
            "immutable_fingerprint": immutable_fingerprint,
            "status": "pending",
            "attempts": 0,
            "max_attempts": attempts_limit,
            "available_at": self._iso(available),
            "enqueued_at": self._iso(now),
            "first_received_at": received_at,
            "last_received_at": received_at,
            "delivery_count": 1,
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "completed_by": None,
            "completed_at": None,
            "failed_by": None,
            "failed_at": None,
            "last_error": None,
            "lease_expirations": 0,
        }
        events[event_id] = record
        dedupe_index[identity] = event_id
        document["events"] = events
        document["dedupe_index"] = dedupe_index
        return {
            "status": "enqueued",
            "event_id": event_id,
            "envelope": copy.deepcopy(envelope),
            "delivery_count": 1,
            "evicted_terminal_event_ids": evicted,
        }

    @staticmethod
    def _immutable_projection(envelope: Any) -> Any:
        if not isinstance(envelope, dict):
            return envelope
        projected = copy.deepcopy(envelope)
        projected.pop("received_at", None)
        return projected

    @classmethod
    def _immutable_fingerprint(cls, envelope: dict[str, Any]) -> str:
        canonical = cls._canonical(cls._immutable_projection(envelope))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _delivery_received_at(self, envelope: dict[str, Any], now: datetime) -> str:
        received_at = str(envelope.get("received_at") or "").strip()
        return received_at or self._iso(now)

    @staticmethod
    def _record_delivery(record: dict[str, Any], *, received_at: str) -> int:
        existing_envelope = record.get("envelope")
        original_received_at = (
            str(existing_envelope.get("received_at") or "").strip()
            if isinstance(existing_envelope, dict)
            else ""
        )
        record["first_received_at"] = str(
            record.get("first_received_at")
            or original_received_at
            or record.get("enqueued_at")
            or received_at
        )
        record["last_received_at"] = received_at
        try:
            previous_count = max(1, int(record.get("delivery_count") or 1))
        except (TypeError, ValueError):
            previous_count = 1
        record["delivery_count"] = previous_count + 1
        return int(record["delivery_count"])

    def _recover_expired(
        self,
        document: dict[str, Any],
        now: datetime,
    ) -> dict[str, list[str]]:
        recovered: dict[str, list[str]] = {"requeued": [], "failed": []}
        for event_id, record in document["events"].items():
            if not isinstance(record, dict) or record.get("status") != "claimed":
                continue
            expires_at = self._parse_time(record.get("lease_expires_at"))
            if expires_at is None or expires_at > now:
                continue
            attempts = int(record.get("attempts") or 0)
            max_attempts = max(1, int(record.get("max_attempts") or self.default_max_attempts))
            terminal = attempts >= max_attempts
            previous_consumer = str(record.get("claimed_by") or "")
            record.update(
                {
                    "status": "failed" if terminal else "pending",
                    "available_at": self._iso(now),
                    "last_error": "consumer lease expired before completion",
                    "last_failed_at": self._iso(now),
                    "failed_by": previous_consumer if terminal else None,
                    "failed_at": self._iso(now) if terminal else None,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "lease_expirations": int(record.get("lease_expirations") or 0) + 1,
                }
            )
            recovered["failed" if terminal else "requeued"].append(str(event_id))
        return recovered

    def _prune_terminal(
        self,
        document: dict[str, Any],
        *,
        reserve: int,
    ) -> list[str]:
        """Bound retained queue history without ever evicting active work."""

        events = document["events"]
        target = max(0, self.max_records - max(0, int(reserve)))
        if len(events) <= target:
            return []
        terminal = [
            record
            for record in events.values()
            if isinstance(record, dict) and record.get("status") in {"completed", "failed"}
        ]
        terminal.sort(
            key=lambda record: (
                str(record.get("completed_at") or record.get("failed_at") or ""),
                str(record.get("enqueued_at") or ""),
                str(record.get("event_id") or ""),
            )
        )
        evicted: list[str] = []
        dedupe_index = document["dedupe_index"]
        for record in terminal:
            if len(events) <= target:
                break
            event_id = str(record.get("event_id") or "")
            identity = str(record.get("dedupe_identity") or "")
            if event_id:
                events.pop(event_id, None)
                evicted.append(event_id)
            if identity and dedupe_index.get(identity) == event_id:
                dedupe_index.pop(identity, None)
        if evicted:
            document["evicted_terminal_count"] = int(
                document.get("evicted_terminal_count") or 0
            ) + len(evicted)
        return evicted

    def _claim_record(
        self,
        record: dict[str, Any],
        *,
        consumer: str,
        lease_seconds: float,
        now: datetime,
    ) -> None:
        record.update(
            {
                "status": "claimed",
                "attempts": int(record.get("attempts") or 0) + 1,
                "claimed_by": consumer,
                "claimed_at": self._iso(now),
                "lease_expires_at": self._iso(now + timedelta(seconds=lease_seconds)),
            }
        )

    def _require_active_claim(
        self,
        record: dict[str, Any],
        *,
        consumer: str,
        now: datetime,
    ) -> None:
        event_id = str(record.get("event_id") or "")
        if record.get("status") != "claimed":
            raise EventInboxClaimError(f"event {event_id} has no active claim")
        if record.get("claimed_by") != consumer:
            raise EventInboxClaimError(f"event {event_id} is claimed by another consumer")
        lease_expires = self._parse_time(record.get("lease_expires_at"))
        if lease_expires is None or lease_expires <= now:
            raise EventInboxClaimError(f"claim lease expired for event {event_id}")

    def _require_document(self, document: dict[str, Any]) -> None:
        if document.get("_state_corrupt"):
            raise EventInboxError("event inbox state is corrupt and requires recovery")
        document.setdefault("schema_version", EVENT_INBOX_SCHEMA)
        events = document.setdefault("events", {})
        dedupe_index = document.setdefault("dedupe_index", {})
        if not isinstance(events, dict) or not isinstance(dedupe_index, dict):
            raise EventInboxError("event inbox document has an invalid shape")

    def _record(self, document: dict[str, Any], event_id: str) -> dict[str, Any]:
        record = document["events"].get(event_id)
        if not isinstance(record, dict):
            raise EventInboxNotFoundError(f"unknown event: {event_id}")
        return record

    @staticmethod
    def _matches(
        envelope: dict[str, Any],
        *,
        event_types: set[str] | None,
        channel: str | None,
        user_id: str | None,
        session_id: str | None,
        privacy_scope: Any,
    ) -> bool:
        source = envelope.get("source")
        if not isinstance(source, dict):
            return False
        if event_types is not None and str(envelope.get("type") or "") not in event_types:
            return False
        if channel is not None and source.get("channel") != channel:
            return False
        if user_id is not None and source.get("user_id") != user_id:
            return False
        if session_id is not None and source.get("session_id") != session_id:
            return False
        if privacy_scope is not None and envelope.get("privacy_scope") != privacy_scope:
            return False
        return True

    @staticmethod
    def _queue_order(record: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(record.get("available_at") or ""),
            str(record.get("enqueued_at") or ""),
            str(record.get("event_id") or ""),
        )

    def _available(self, record: dict[str, Any], now: datetime) -> bool:
        available_at = self._parse_time(record.get("available_at"))
        return available_at is None or available_at <= now

    def _dedupe_identity(self, envelope: dict[str, Any]) -> str:
        event_id = str(envelope.get("event_id") or "")
        dedupe_key = str(envelope.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return f"event:{event_id}"
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        boundary = [
            source.get("channel"),
            source.get("user_id"),
            source.get("session_id"),
            envelope.get("privacy_scope"),
            dedupe_key,
        ]
        digest = hashlib.sha256(self._canonical(boundary).encode("utf-8")).hexdigest()
        return f"dedupe:{digest}"

    @staticmethod
    def _event_types(
        values: Iterable[EventType | str] | EventType | str | None,
    ) -> set[str] | None:
        if values is None:
            return None
        if isinstance(values, (EventType, str)):
            values = [values]
        normalized: set[str] = set()
        for value in values:
            normalized.add(value.value if isinstance(value, EventType) else str(value))
        return normalized

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _coerce_time(
        self,
        value: datetime | str | None,
        *,
        default: datetime,
    ) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str) and value.strip():
            parsed = self._parse_time(value)
            if parsed is None:
                raise ValueError(f"invalid timestamp: {value}")
            return parsed
        return default

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _consumer(value: str) -> str:
        consumer = str(value or "").strip()
        if not consumer:
            raise ValueError("consumer_id must be non-empty")
        return consumer

    @staticmethod
    def _event_id(value: str) -> str:
        event_id = str(value or "").strip()
        if not event_id:
            raise ValueError("event_id must be non-empty")
        return event_id

    @staticmethod
    def _lease_seconds(value: float) -> float:
        seconds = float(value)
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return seconds

    @staticmethod
    def _error_text(error: BaseException | str) -> str:
        if isinstance(error, BaseException):
            text = f"{type(error).__name__}: {error}"
        else:
            text = str(error)
        return text.strip()[:2000] or "unknown event processing failure"

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise TypeError("event inbox values must be JSON serializable") from exc

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
