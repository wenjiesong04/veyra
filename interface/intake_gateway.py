import time
from typing import Any
from uuid import uuid4

from core.awareness_loop import AwarenessLoop
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.auth import AuthPolicy
from interface.channel_adapter import ChannelAdapter
from interface.channel_router import ChannelRouter
from interface.event_normalizer import EventNormalizer
from interface.event_schema import LoopResult
from interface.session_mapper import SessionMapper
from interface.event_schema import utc_now_iso


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
        seen = state.setdefault("seen_message_ids", {})
        if isinstance(seen, dict) and dedupe_id in seen:
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
            return {"status": "duplicate", "message_id": dedupe_id, "channel": channel_id, "session_id": mapped_session, "previous": seen[dedupe_id]}

        event = self.normalizer.user_message(text=text, channel=channel_id, user_id=user_id, session_id=mapped_session, metadata=metadata or {})
        inbox_item = {
            "message_id": dedupe_id,
            "event_id": event.event_id,
            "channel": channel_id,
            "user_id": user_id,
            "session_id": mapped_session,
            "text": text,
            "metadata": metadata or {},
            "received_at": utc_now_iso(),
        }
        self._record_inbox(state, inbox_item)
        result = self.awareness_loop.handle_event(event)
        adapter = ChannelAdapter(self.state_store, channel=channel_id)
        outbox = adapter.send(
            mapped_session,
            result.response,
            metadata={
                "event_id": event.event_id,
                "message_id": dedupe_id,
                "route": result.route.value,
                "status": result.status,
                "inbound": metadata or {},
            },
        )
        state = self._channel_state()
        seen = state.setdefault("seen_message_ids", {})
        if isinstance(seen, dict):
            seen[dedupe_id] = {"event_id": event.event_id, "session_id": mapped_session, "processed_at": utc_now_iso(), "status": result.status}
            state["seen_message_ids"] = dict(list(seen.items())[-1000:])
        sessions = state.setdefault("sessions", {})
        if isinstance(sessions, dict):
            sessions[mapped_session] = {"channel": channel_id, "user_id": user_id, "last_event_id": event.event_id, "updated_at": utc_now_iso()}
        self._write_channel_state(state)
        return {
            "status": "delivered",
            "message_id": dedupe_id,
            "channel": channel_id,
            "session_id": mapped_session,
            "event": event.to_dict(),
            "loop_result": result.to_dict(),
            "outbox": outbox,
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
        state = self._channel_state()
        config = self._channel_config(state, channel_id)
        config.update({key: value for key, value in patch.items() if value is not None})
        channels = state.setdefault("channels", {})
        if isinstance(channels, dict):
            channels[channel_id] = config
        self._write_channel_state(state)
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
        state["inbox"] = inbox[-500:]
        self._write_channel_state(state)

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
