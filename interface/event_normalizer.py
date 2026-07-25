from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from interface.event_schema import EventSource, EventType, VeyraEvent, utc_now_iso


class EventNormalizer:
    """Converts external channel payloads into VeyraEvent objects."""

    def normalize(
        self,
        event_type: EventType | str | Mapping[str, Any] | VeyraEvent,
        payload: Mapping[str, Any] | None = None,
        *,
        source: EventSource | Mapping[str, Any] | None = None,
        channel: str = "",
        user_id: str = "",
        session_id: str = "",
        event_id: str | None = None,
        timestamp: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        subject: Any = None,
        evidence_refs: list[Any] | None = None,
        dedupe_key: str | None = None,
        occurred_at: str | None = None,
        received_at: str | None = None,
        privacy_scope: Any = None,
    ) -> VeyraEvent:
        """Normalize any supported event type without channel-specific branching.

        A complete legacy/current event mapping can be supplied as the first
        argument.  Explicit keyword arguments override values from that mapping.
        Unknown payload fields remain untouched.
        """
        if isinstance(event_type, VeyraEvent):
            return VeyraEvent.from_dict(event_type.to_dict())

        raw: dict[str, Any] = {}
        if isinstance(event_type, Mapping):
            raw = copy.deepcopy(dict(event_type))
            raw_type = raw.get("type")
            if raw_type is None:
                raise ValueError("event envelope requires type")
            event_type = raw_type if isinstance(raw_type, EventType) else str(raw_type)
            if payload is None:
                raw_payload = raw.get("payload")
                payload = raw_payload if isinstance(raw_payload, Mapping) else None
            if source is None:
                raw_source = raw.get("source")
                source = raw_source if isinstance(raw_source, (EventSource, Mapping)) else None
            event_id = event_id or self._optional_text(raw.get("event_id"))
            timestamp = timestamp or self._optional_text(raw.get("timestamp"))
            correlation_id = correlation_id or self._optional_text(raw.get("correlation_id"))
            causation_id = causation_id or self._optional_text(raw.get("causation_id"))
            subject = subject if subject is not None else copy.deepcopy(raw.get("subject"))
            evidence_refs = evidence_refs if evidence_refs is not None else copy.deepcopy(raw.get("evidence_refs"))
            dedupe_key = dedupe_key or self._optional_text(raw.get("dedupe_key"))
            occurred_at = occurred_at or self._optional_text(raw.get("occurred_at"))
            received_at = received_at or self._optional_text(raw.get("received_at"))
            if privacy_scope is None and "privacy_scope" in raw:
                privacy_scope = copy.deepcopy(raw.get("privacy_scope"))

        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be a mapping")

        normalized_source = self._source(
            source,
            channel=channel,
            user_id=user_id,
            session_id=session_id,
        )
        occurred = occurred_at or timestamp or utc_now_iso()
        normalized_type = event_type if isinstance(event_type, EventType) else EventType(str(event_type))
        return VeyraEvent(
            type=normalized_type,
            source=normalized_source,
            payload=copy.deepcopy(dict(payload)),
            event_id=event_id or "",
            timestamp=timestamp or occurred,
            correlation_id=correlation_id,
            causation_id=causation_id,
            subject=copy.deepcopy(subject),
            evidence_refs=copy.deepcopy(evidence_refs or []),
            dedupe_key=dedupe_key,
            occurred_at=occurred,
            received_at=received_at or utc_now_iso(),
            privacy_scope=copy.deepcopy("user" if privacy_scope is None else privacy_scope),
        )

    def user_message(
        self,
        text: str,
        channel: str,
        user_id: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        subject: Any = None,
        evidence_refs: list[Any] | None = None,
        dedupe_key: str | None = None,
        occurred_at: str | None = None,
        received_at: str | None = None,
        privacy_scope: Any = "user",
    ) -> VeyraEvent:
        payload: dict[str, Any] = {"text": text}
        if metadata:
            payload["metadata"] = copy.deepcopy(metadata)
        return self.normalize(
            EventType.USER_MESSAGE,
            payload=payload,
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            event_id=event_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            subject=subject,
            evidence_refs=evidence_refs,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
            received_at=received_at,
            privacy_scope=privacy_scope,
        )

    @staticmethod
    def _source(
        source: EventSource | Mapping[str, Any] | None,
        *,
        channel: str,
        user_id: str,
        session_id: str,
    ) -> EventSource:
        if isinstance(source, EventSource):
            return EventSource(
                channel=channel or source.channel,
                user_id=user_id or source.user_id,
                session_id=session_id or source.session_id,
            )
        if isinstance(source, Mapping):
            return EventSource(
                channel=channel or str(source.get("channel") or ""),
                user_id=user_id or str(source.get("user_id") or ""),
                session_id=session_id or str(source.get("session_id") or ""),
            )
        return EventSource(channel=channel, user_id=user_id, session_id=session_id)

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None
