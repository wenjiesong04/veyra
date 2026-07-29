import time
from typing import Any
from uuid import uuid4

from core.awareness_loop import AwarenessLoop
from core.model_client import redact_sensitive
from core.state_compact import compact_channel_inbox_item
from core.world_state import WorldStateStore
from interface.auth import AuthPolicy
from interface.channel_adapter import ChannelAdapter
from interface.channel_router import ChannelRouter
from interface.event_normalizer import EventNormalizer
from interface.event_schema import LoopResult
from interface.session_mapper import SessionMapper
from interface.event_schema import utc_now_iso
from memory_bridge.scope import framed_sha256, normalize_scope_component


_INTAKE_DEDUPE_SCOPE_VERSION = "veyra-intake-dedupe-v2"


class IntakeGateway:
    """Auth/session/dedupe hooks can be added here without touching VeyraCore."""

    def __init__(
        self,
        awareness_loop: AwarenessLoop,
        normalizer: EventNormalizer | None = None,
        *,
        state_store: WorldStateStore | None = None,
        router: ChannelRouter | None = None,
        session_mapper: SessionMapper | None = None,
        auth_policy: AuthPolicy | None = None,
    ) -> None:
        self.awareness_loop = awareness_loop
        self.normalizer = normalizer or EventNormalizer()
        self.state_store = state_store
        self.router = router or ChannelRouter()
        self.session_mapper = session_mapper or SessionMapper()
        self.auth_policy = auth_policy or AuthPolicy()

    def receive_text(self, text: str, channel: str = "cli", user_id: str = "local-user", session_id: str = "local") -> LoopResult:
        channel_id = self.router.resolve(channel)
        mapped_session = self.session_mapper.map(channel_id, user_id, session_id)
        event = self.normalizer.user_message(text=text, channel=channel_id, user_id=user_id, session_id=mapped_session)
        return self.awareness_loop.handle_event(event)

    def receive_message(
        self,
        *,
        text: str,
        channel: str = "api",
        user_id: str = "local-user",
        session_id: str = "local-session",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        channel_id = self.router.resolve(channel)
        state = self._channel_state()
        config = self._channel_config(state, channel_id)
        if not self.auth_policy.allow(channel_id, user_id, config):
            self._record_intake_trace(
                text=text,
                channel=channel_id,
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                metadata=metadata or {},
                started_at=started_at,
                status="blocked",
                final_route="block",
                reason="channel or user is not allowed",
            )
            return {"status": "blocked", "reason": "channel or user is not allowed", "channel": channel_id}
        mapped_session = self.session_mapper.map(channel_id, user_id, session_id)
        dedupe_id = message_id or f"msg_{uuid4().hex[:12]}"
        dedupe_owner = self._dedupe_owner(
            channel=channel_id,
            user_id=user_id,
            mapped_session=mapped_session,
            message_id=dedupe_id,
        )
        internal_dedupe_key = self._dedupe_key(dedupe_owner)

        event = self.normalizer.user_message(text=text, channel=channel_id, user_id=user_id, session_id=mapped_session, metadata=metadata or {})
        inbox_item = compact_channel_inbox_item(
            {
                "message_id": dedupe_id,
                "event_id": event.event_id,
                "channel": channel_id,
                "user_id": user_id,
                "session_id": mapped_session,
                "text": text,
                "metadata": metadata or {},
                "received_at": utc_now_iso(),
            }
        )
        previous = self._reserve_inbox(
            internal_dedupe_key,
            inbox_item,
            owner=dedupe_owner,
        )
        if previous:
            self._record_intake_trace(
                text=text,
                channel=channel_id,
                user_id=user_id,
                session_id=mapped_session,
                message_id=dedupe_id,
                metadata=metadata or {},
                started_at=started_at,
                status="duplicate",
                final_route="duplicate",
                reason="duplicate message ignored",
            )
            return {
                "status": "duplicate",
                "message_id": dedupe_id,
                "channel": channel_id,
                "session_id": mapped_session,
                "previous": previous,
            }
        try:
            result = self.awareness_loop.handle_event(event)
        except Exception as exc:
            self._release_failed_reservation(
                internal_dedupe_key,
                owner=dedupe_owner,
                error=exc,
            )
            raise
        adapter = ChannelAdapter(self.state_store, channel=channel_id)
        deliveries: list[dict[str, Any]] = []
        messages = result.ordered_messages()
        for item in messages:
            message_type = str(item.get("message_type") or "primary")
            message_index = int(item.get("index") or 0)
            message_text = str(item.get("message") or "").strip()
            if not message_text:
                continue
            delivery_metadata = {
                "event_id": event.event_id,
                "message_id": dedupe_id,
                "route": result.route.value,
                "status": result.status,
                "message_type": message_type,
                "message_index": message_index,
                "message_count": len(messages),
                "inbound": metadata or {},
            }
            try:
                delivery = adapter.send(mapped_session, message_text, metadata=delivery_metadata)
            except Exception as exc:
                delivery = {
                    "channel": channel_id,
                    "session_id": mapped_session,
                    "message": message_text,
                    "metadata": delivery_metadata,
                    "status": "error",
                    "delivery_status": "send_failed",
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                }
            deliveries.append(delivery)
        outbox = deliveries[0] if deliveries else {}
        def finalize_channel_state(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.setdefault("seen_message_ids", {})
            if isinstance(seen, dict):
                reservation = seen.get(internal_dedupe_key)
                if not self._seen_entry_matches(reservation, dedupe_owner):
                    return state
                seen[internal_dedupe_key] = {
                    "scope_version": _INTAKE_DEDUPE_SCOPE_VERSION,
                    "owner": dict(dedupe_owner),
                    "event_id": event.event_id,
                    "session_id": mapped_session,
                    "processed_at": utc_now_iso(),
                    "status": result.status,
                }
                state["seen_message_ids"] = dict(list(seen.items())[-300:])
            sessions = state.setdefault("sessions", {})
            if isinstance(sessions, dict):
                sessions[mapped_session] = {
                    "channel": channel_id,
                    "user_id": user_id,
                    "last_event_id": event.event_id,
                    "updated_at": utc_now_iso(),
                }
            return state

        if self.state_store:
            self.state_store.mutate_json("channel_state.json", finalize_channel_state)
        return {
            "status": "delivered",
            "message_id": dedupe_id,
            "channel": channel_id,
            "session_id": mapped_session,
            "event": event.to_dict(),
            "loop_result": result.to_dict(),
            "outbox": outbox,
            "outbox_messages": deliveries,
        }

    def channel_status(self) -> dict[str, Any]:
        state = self._channel_state()
        inbox = state.get("inbox") if isinstance(state.get("inbox"), list) else []
        outbox = state.get("outbox") if isinstance(state.get("outbox"), list) else []
        return {
            "status": "success",
            "channels": redact_sensitive(state.get("channels", {})),
            "session_count": len(state.get("sessions", {}) if isinstance(state.get("sessions"), dict) else {}),
            "inbox_count": len(inbox),
            "outbox_count": len(outbox),
        }

    def channel_config(self, channel: str) -> dict[str, Any]:
        channel_id = self.router.resolve(channel)
        state = self._channel_state()
        return {"status": "success", "channel": channel_id, "config": redact_sensitive(self._channel_config(state, channel_id))}

    def configure_channel(self, channel: str, patch: dict[str, Any]) -> dict[str, Any]:
        channel_id = self.router.resolve(channel)
        config: dict[str, Any] = {}

        def update_channel(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal config
            config = self._channel_config(state, channel_id)
            config.update({key: value for key, value in patch.items() if value is not None})
            channels = state.setdefault("channels", {})
            if isinstance(channels, dict):
                channels[channel_id] = config
            return state

        if self.state_store:
            self.state_store.mutate_json("channel_state.json", update_channel)
        return {"status": "success", "channel": channel_id, "config": redact_sensitive(config)}

    def send_channel_message(self, *, channel: str, session_id: str, message: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        channel_id = self.router.resolve(channel)
        adapter = ChannelAdapter(self.state_store, channel=channel_id)
        return {"status": "success", "delivery": adapter.send(session_id, message, metadata=metadata or {})}

    def outbox(self, limit: int = 100) -> dict[str, Any]:
        outbox = self._channel_state().get("outbox")
        items = outbox if isinstance(outbox, list) else []
        return {"status": "success", "items": items[-limit:]}

    def sessions(self) -> dict[str, Any]:
        sessions = self._channel_state().get("sessions")
        return {"status": "success", "sessions": sessions if isinstance(sessions, dict) else {}}

    def _channel_state(self) -> dict[str, Any]:
        if not self.state_store:
            return {"channels": {}, "sessions": {}, "seen_message_ids": {}, "inbox": [], "outbox": []}
        return self.state_store.read_json("channel_state.json")

    def _write_channel_state(self, state: dict[str, Any]) -> None:
        if self.state_store:
            self.state_store.write_json("channel_state.json", state)

    def _channel_config(self, state: dict[str, Any], channel: str) -> dict[str, Any]:
        channels = state.setdefault("channels", {})
        if not isinstance(channels, dict):
            channels = {}
            state["channels"] = channels
        config = channels.setdefault(channel, {"enabled": True, "delivery": "local_outbox"})
        return config if isinstance(config, dict) else {"enabled": True, "delivery": "local_outbox"}

    def _record_inbox(self, state: dict[str, Any], item: dict[str, Any]) -> None:
        inbox = state.setdefault("inbox", [])
        if not isinstance(inbox, list):
            inbox = []
            state["inbox"] = inbox
        inbox.append(item)
        state["inbox"] = [compact_channel_inbox_item(entry) for entry in inbox[-200:]]
        self._write_channel_state(state)

    def _reserve_inbox(
        self,
        internal_dedupe_key: str,
        item: dict[str, Any],
        *,
        owner: dict[str, str],
    ) -> dict[str, Any]:
        if not self.state_store:
            return {}
        previous: dict[str, Any] = {}

        def reserve(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal previous
            seen = state.setdefault("seen_message_ids", {})
            if not isinstance(seen, dict):
                seen = {}
                state["seen_message_ids"] = seen
            existing = seen.get(internal_dedupe_key)
            if self._seen_entry_matches(existing, owner):
                previous = self._public_previous(existing)
                return state
            seen[internal_dedupe_key] = {
                "scope_version": _INTAKE_DEDUPE_SCOPE_VERSION,
                "owner": dict(owner),
                "event_id": item.get("event_id"),
                "session_id": owner["session_id"],
                "reserved_at": utc_now_iso(),
                "status": "processing",
            }
            state["seen_message_ids"] = dict(list(seen.items())[-300:])
            inbox = state.setdefault("inbox", [])
            if not isinstance(inbox, list):
                inbox = []
            inbox.append(item)
            state["inbox"] = [compact_channel_inbox_item(entry) for entry in inbox[-200:]]
            return state

        self.state_store.mutate_json("channel_state.json", reserve)
        return previous

    def _release_failed_reservation(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        error: Exception,
    ) -> None:
        if not self.state_store:
            return

        def release(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.get("seen_message_ids")
            if isinstance(seen, dict):
                reservation = seen.get(internal_dedupe_key)
                if (
                    self._seen_entry_matches(reservation, owner)
                    and reservation.get("status") == "processing"
                ):
                    seen.pop(internal_dedupe_key, None)
            failures = state.setdefault("intake_failures", [])
            if not isinstance(failures, list):
                failures = []
            failures.append(
                {
                    "message_id": owner["message_id"],
                    "failed_at": utc_now_iso(),
                    "error_type": type(error).__name__,
                    "reason": redact_sensitive(str(error))[:300],
                }
            )
            state["intake_failures"] = failures[-100:]
            return state

        self.state_store.mutate_json("channel_state.json", release)

    @staticmethod
    def _dedupe_owner(
        *,
        channel: str,
        user_id: str,
        mapped_session: str,
        message_id: str,
    ) -> dict[str, str]:
        return {
            "channel": normalize_scope_component(channel, "channel"),
            "user_id": normalize_scope_component(user_id, "user_id"),
            "session_id": normalize_scope_component(
                mapped_session,
                "mapped_session_id",
            ),
            # The provider's message identity is an opaque value. Keep its
            # exact text instead of delimiter-normalizing it.
            "message_id": str(message_id),
        }

    @staticmethod
    def _dedupe_key(owner: dict[str, str]) -> str:
        digest = framed_sha256(
            _INTAKE_DEDUPE_SCOPE_VERSION,
            owner["channel"],
            owner["user_id"],
            owner["session_id"],
            owner["message_id"],
        )
        return f"intake-dedupe-v2-{digest}"

    @staticmethod
    def _seen_entry_matches(
        entry: Any,
        owner: dict[str, str],
    ) -> bool:
        if not isinstance(entry, dict):
            return False
        entry_owner = entry.get("owner")
        return (
            entry.get("scope_version") == _INTAKE_DEDUPE_SCOPE_VERSION
            and isinstance(entry_owner, dict)
            and entry_owner == owner
        )

    @staticmethod
    def _public_previous(entry: Any) -> dict[str, Any]:
        if not isinstance(entry, dict):
            return {}
        return {
            key: entry[key]
            for key in (
                "event_id",
                "session_id",
                "reserved_at",
                "processed_at",
                "status",
            )
            if key in entry
        }

    def _record_intake_trace(
        self,
        *,
        text: str,
        channel: str,
        user_id: str,
        session_id: str,
        message_id: str | None,
        metadata: dict[str, Any],
        started_at: float,
        status: str,
        final_route: str,
        reason: str,
    ) -> None:
        recorder = getattr(self.awareness_loop, "runtime_trace", None)
        if not recorder:
            return
        recorder.record_intake_result(
            text=text,
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            message_id=message_id,
            metadata=metadata,
            started_at=started_at,
            status=status,
            final_route=final_route,
            reason=reason,
        )
