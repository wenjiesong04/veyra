import time
from datetime import datetime, timezone
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
from interface.event_schema import LoopResult, Route, VeyraEvent
from core.definitions import RiskLevel
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
        instance_id: str | None = None,
        processing_lease_seconds: float = 300.0,
        completion_hook: Any | None = None,
    ) -> None:
        if isinstance(processing_lease_seconds, bool) or not isinstance(processing_lease_seconds, (int, float)):
            raise ValueError("processing_lease_seconds must be a positive number")
        if processing_lease_seconds <= 0:
            raise ValueError("processing_lease_seconds must be a positive number")
        self.awareness_loop = awareness_loop
        self.normalizer = normalizer or EventNormalizer()
        self.state_store = state_store
        self.router = router or ChannelRouter()
        self.session_mapper = session_mapper or SessionMapper()
        self.auth_policy = auth_policy or AuthPolicy()
        # A gateway instance owns the in-flight lease.  The token is never
        # derived from message text and is only used to prevent a stale
        # worker from settling a reservation that another runtime reclaimed.
        self.instance_id = str(instance_id or f"intake-{uuid4().hex}")
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")
        self.processing_lease_seconds = float(processing_lease_seconds)
        self.completion_hook = completion_hook

    def set_completion_hook(self, hook: Any | None) -> None:
        """Install the server-owned post-cognition completion phase."""

        self.completion_hook = hook

    def receive_text(self, text: str, channel: str = "cli", user_id: str = "local-user", session_id: str = "local") -> LoopResult:
        channel_id = self.router.resolve(channel)
        mapped_session = self.session_mapper.map(channel_id, user_id, session_id)
        event = self.normalizer.user_message(text=text, channel=channel_id, user_id=user_id, session_id=mapped_session)
        return self.awareness_loop.handle_event(event)

    def admit_message(
        self,
        *,
        text: str,
        channel: str = "api",
        user_id: str = "local-user",
        session_id: str = "local-session",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run intake gates and reserve the message before cognition starts.

        The returned accepted record is an internal hand-off.  Its event is
        intentionally not processed here: callers that own a product ledger
        can persist the user turn first and then pass this exact admission to
        :meth:`receive_message`.  This keeps authentication, canonical
        session mapping, and idempotency in one boundary.
        """

        started_at = time.perf_counter()
        channel_id = self.router.resolve(channel)
        raw_metadata = metadata if isinstance(metadata, dict) else {}
        # Client metadata is data, not a server correlation envelope.  The
        # reserved internal key is attached only after Product Conversation
        # admission has returned a server-owned conversation ID.
        selected_metadata = {
            key: value for key, value in raw_metadata.items() if key != "_internal"
        }
        state = self._channel_state()
        config = self._channel_config(state, channel_id)
        if not self.auth_policy.allow(channel_id, user_id, config):
            self._record_intake_trace(
                text=text,
                channel=channel_id,
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                metadata=selected_metadata,
                started_at=started_at,
                status="blocked",
                final_route="block",
                reason="channel or user is not allowed",
            )
            return {
                "status": "blocked",
                "reason": "channel or user is not allowed",
                "channel": channel_id,
            }

        mapped_session = self.session_mapper.map(channel_id, user_id, session_id)
        dedupe_id = message_id or f"msg_{uuid4().hex[:12]}"
        dedupe_owner = self._dedupe_owner(
            channel=channel_id,
            user_id=user_id,
            mapped_session=mapped_session,
            message_id=dedupe_id,
        )
        internal_dedupe_key = self._dedupe_key(dedupe_owner)
        reservation_token = f"lease_{uuid4().hex}"
        event = self.normalizer.user_message(
            text=text,
            channel=channel_id,
            user_id=user_id,
            session_id=mapped_session,
            metadata=selected_metadata,
            event_id=self._event_id(dedupe_owner),
        )
        inbox_item = compact_channel_inbox_item(
            {
                "message_id": dedupe_id,
                "event_id": event.event_id,
                "channel": channel_id,
                "user_id": user_id,
                "session_id": mapped_session,
                "text": text,
                "metadata": selected_metadata,
                "received_at": utc_now_iso(),
            }
        )
        previous = self._reserve_inbox(
            internal_dedupe_key,
            inbox_item,
            owner=dedupe_owner,
            reservation_token=reservation_token,
        )
        if previous:
            if previous.get("_completion_pending") and previous.get("_resume_completion"):
                # The cognition result is already durable.  The owner of this
                # live lease may retry only the completion ledger, never Core
                # or an Agent side effect.
                pending = previous["_completion_pending"]
                return self._completion_pending_admission(
                    admission_base={
                        "status": "accepted",
                        "message_id": dedupe_id,
                        "channel": channel_id,
                        "user_id": user_id,
                        "session_id": mapped_session,
                        "_internal_dedupe_key": internal_dedupe_key,
                        "_dedupe_owner": dedupe_owner,
                        "_reservation_token": previous.get("_reservation_token") or reservation_token,
                        "_runtime_instance_id": self.instance_id,
                    },
                    pending=pending,
                )
            self._record_intake_trace(
                text=text,
                channel=channel_id,
                user_id=user_id,
                session_id=mapped_session,
                message_id=dedupe_id,
                metadata=selected_metadata,
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
        pending = self._pending_for_reservation(
            internal_dedupe_key,
            owner=dedupe_owner,
            reservation_token=reservation_token,
        )
        if pending is not None:
            return self._completion_pending_admission(
                admission_base={
                    "status": "accepted",
                    "message_id": dedupe_id,
                    "channel": channel_id,
                    "user_id": user_id,
                    "session_id": mapped_session,
                    "_internal_dedupe_key": internal_dedupe_key,
                    "_dedupe_owner": dedupe_owner,
                    "_reservation_token": reservation_token,
                    "_runtime_instance_id": self.instance_id,
                },
                pending=pending,
            )
        return {
            "status": "accepted",
            "message_id": dedupe_id,
            "channel": channel_id,
            "user_id": user_id,
            "session_id": mapped_session,
            "event": event,
            # These fields never leave this process.  They let the later
            # cognition step finalize exactly the reservation it received.
            "_internal_dedupe_key": internal_dedupe_key,
            "_dedupe_owner": dedupe_owner,
            "_reservation_token": reservation_token,
            "_runtime_instance_id": self.instance_id,
        }

    def receive_message(
        self,
        *,
        text: str,
        channel: str = "api",
        user_id: str = "local-user",
        session_id: str = "local-session",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        admission: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if admission is None:
            admission = self.admit_message(
                text=text,
                channel=channel,
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                metadata=metadata,
            )
        if admission.get("status") != "accepted":
            if admission.get("status") == "duplicate" and admission.get("_completion_pending"):
                # Same-instance retry after a completion-only failure.  The
                # admission remains a duplicate at the user-message layer,
                # while this internal path retries the already persisted
                # assistant completion.
                return self._resume_pending_completion(admission)
            return {
                key: value
                for key, value in admission.items()
                if not key.startswith("_") and key != "event"
            }

        return self._process_admitted_message(admission)

    def attach_conversation_metadata(
        self,
        admission: dict[str, Any],
        *,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Bind server-owned conversation/message IDs to the admitted event."""

        if admission.get("status") != "accepted":
            return admission
        event = admission.get("event")
        if not hasattr(event, "payload"):
            raise TypeError("accepted admission has no event")
        payload = event.payload if isinstance(event.payload, dict) else {}
        metadata = payload.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            payload["metadata"] = metadata
        metadata["_internal"] = {
            "conversation_id": str(conversation_id),
            "message_id": str(admission.get("message_id") or ""),
            "canonical_session_id": str(admission.get("session_id") or ""),
        }
        return admission

    def release_admission(self, admission: dict[str, Any], error: Exception) -> None:
        """Release an accepted reservation when pre-cognition admission fails."""

        if admission.get("status") != "accepted":
            return
        owner = admission.get("_dedupe_owner")
        key = admission.get("_internal_dedupe_key")
        token = admission.get("_reservation_token")
        if isinstance(owner, dict) and isinstance(key, str) and key and isinstance(token, str):
            self._release_failed_reservation(key, owner=owner, reservation_token=token, error=error)

    def finalize_admission_without_cognition(
        self,
        admission: dict[str, Any],
        *,
        status: str = "duplicate",
    ) -> None:
        """Terminally settle an accepted reservation without running Core.

        This path is used only when the Product Conversation ledger already
        owns the exact message identity.  Releasing the reservation would
        make every retry look new and leave the intake audit in ``processing``.
        """

        if status != "duplicate" or admission.get("status") != "accepted":
            return
        event = admission.get("event")
        owner = admission.get("_dedupe_owner")
        key = admission.get("_internal_dedupe_key")
        token = admission.get("_reservation_token")
        if (
            not hasattr(event, "event_id")
            or not isinstance(owner, dict)
            or not isinstance(key, str)
            or not key
            or not isinstance(token, str)
        ):
            raise TypeError("accepted admission is missing terminal reservation identity")
        mapped_session = str(admission.get("session_id") or "")

        def finalize(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.setdefault("seen_message_ids", {})
            if not isinstance(seen, dict):
                return state
            reservation = seen.get(key)
            if not self._reservation_matches(reservation, owner, token):
                return state
            seen[key] = {
                "scope_version": _INTAKE_DEDUPE_SCOPE_VERSION,
                "owner": dict(owner),
                "event_id": event.event_id,
                "session_id": mapped_session,
                "processed_at": utc_now_iso(),
                "status": "duplicate",
                "finalized_without_cognition": True,
            }
            state["seen_message_ids"] = dict(list(seen.items())[-300:])
            return state

        if self.state_store:
            self.state_store.mutate_json("channel_state.json", finalize)

    def _process_admitted_message(self, admission: dict[str, Any]) -> dict[str, Any]:
        event = admission.get("event")
        if not hasattr(event, "event_id"):
            raise TypeError("accepted admission has no normalized event")
        channel_id = str(admission.get("channel") or "")
        mapped_session = str(admission.get("session_id") or "")
        user_id = str(admission.get("user_id") or "")
        dedupe_id = str(admission.get("message_id") or "")
        metadata = event.payload.get("metadata") if isinstance(event.payload, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        internal_metadata = metadata.get("_internal")
        internal_metadata = internal_metadata if isinstance(internal_metadata, dict) else {}
        conversation_id = str(internal_metadata.get("conversation_id") or "").strip() or None
        delivery_inbound_metadata = {
            key: value for key, value in metadata.items() if key != "_internal"
        }
        internal_dedupe_key = str(admission.get("_internal_dedupe_key") or "")
        dedupe_owner = admission.get("_dedupe_owner")
        reservation_token = str(admission.get("_reservation_token") or "")
        if not isinstance(dedupe_owner, dict) or not internal_dedupe_key or not reservation_token:
            raise TypeError("accepted admission is missing dedupe reservation")
        completion_only = bool(admission.get("_completion_only"))
        pending = admission.get("_completion_pending")
        hook_attempted = False
        try:
            if completion_only:
                if not isinstance(pending, dict):
                    raise RuntimeError("completion-only admission is missing its durable result")
                result = self._restore_pending_result(pending.get("loop_result"))
            else:
                result = self.awareness_loop.handle_event(event)
            if self.state_store and not self._reservation_is_current(
                internal_dedupe_key,
                owner=dedupe_owner,
                reservation_token=reservation_token,
            ):
                # Another runtime reclaimed this canonical event while this
                # worker was still thinking. Do not let the stale worker
                # append or deliver a second assistant response.
                return {
                    "status": "duplicate",
                    "message_id": dedupe_id,
                    "channel": channel_id,
                    "session_id": mapped_session,
                    "previous": {"event_id": event.event_id, "status": "duplicate"},
                }
            completion = None
            if self.completion_hook is not None:
                if not completion_only:
                    self._mark_completion_pending(
                        internal_dedupe_key,
                        owner=dedupe_owner,
                        reservation_token=reservation_token,
                        event=event,
                        result=result,
                        conversation_id=conversation_id,
                    )
                hook_attempted = True
                completion = self.completion_hook(admission, result)
        except Exception as exc:
            if hook_attempted or completion_only:
                # The cognition result is already durable.  Releasing here
                # would make the provider retry run cognition/Agent work a
                # second time. Keep the reservation and expose a bounded
                # completion failure for the next completion-only retry.
                self._record_completion_failure(
                    internal_dedupe_key,
                    owner=dedupe_owner,
                    reservation_token=reservation_token,
                    error=exc,
                )
            else:
                self._release_failed_reservation(
                    internal_dedupe_key,
                    owner=dedupe_owner,
                    reservation_token=reservation_token,
                    error=exc,
                )
            raise
        if not isinstance(completion, dict):
            completion = {}
        if hook_attempted:
            self._clear_completion_pending(
                internal_dedupe_key,
                owner=dedupe_owner,
                reservation_token=reservation_token,
            )
        if (
            completion.get("status") == "duplicate"
            and int(completion.get("assistant_message_count") or 0) > 0
        ):
            self.finalize_admission_without_cognition(admission, status="duplicate")
            return {
                "status": "duplicate",
                "message_id": dedupe_id,
                "channel": channel_id,
                "session_id": mapped_session,
                "previous": {"event_id": event.event_id, "status": "duplicate"},
            }
        conversation_id = str(completion.get("conversation_id") or conversation_id or "").strip() or None
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
                "inbound": delivery_inbound_metadata,
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
                if not self._reservation_matches(reservation, dedupe_owner, reservation_token):
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
        loop_result = result.to_dict()
        if conversation_id:
            loop_result["conversation_id"] = conversation_id
        if completion:
            loop_result["conversation_recording"] = completion
        return {
            "status": "delivered",
            "message_id": dedupe_id,
            "channel": channel_id,
            "session_id": mapped_session,
            "conversation_id": conversation_id,
            "event": event.to_dict(),
            "loop_result": loop_result,
            "conversation_recording": completion or None,
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

    def _completion_pending_admission(
        self,
        *,
        admission_base: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        event_payload = pending.get("event")
        if not isinstance(event_payload, dict):
            raise RuntimeError("durable completion is missing its event envelope")
        result = dict(admission_base)
        result["event"] = VeyraEvent.from_dict(event_payload)
        result["_completion_only"] = True
        result["_completion_pending"] = pending
        return result

    def _resume_pending_completion(self, admission: dict[str, Any]) -> dict[str, Any]:
        pending = admission.get("_completion_pending")
        if not isinstance(pending, dict):
            return {
                key: value
                for key, value in admission.items()
                if not key.startswith("_") and key != "event"
            }
        resumed = dict(admission)
        resumed["status"] = "accepted"
        resumed["_completion_only"] = True
        resumed["event"] = VeyraEvent.from_dict(pending.get("event") or {})
        return self._process_admitted_message(resumed)

    @staticmethod
    def _restore_pending_result(raw: Any) -> LoopResult:
        if not isinstance(raw, dict):
            raise RuntimeError("durable completion is missing its loop result")
        try:
            route = Route(str(raw.get("route") or ""))
            risk_level = RiskLevel(str(raw.get("risk_level") or ""))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("durable completion has an invalid loop result") from exc
        response = str(raw.get("response") or raw.get("primary_response") or "")
        followups = raw.get("followup_messages")
        if not isinstance(followups, list):
            messages = raw.get("messages")
            followups = [
                item.get("message")
                for item in messages[1:]
                if isinstance(item, dict) and str(item.get("message") or "").strip()
            ] if isinstance(messages, list) else []
        artifacts = raw.get("artifacts")
        return LoopResult(
            event_id=str(raw.get("event_id") or ""),
            route=route,
            status=str(raw.get("status") or "recorded"),
            response=response,
            risk_level=risk_level,
            artifacts=artifacts if isinstance(artifacts, dict) else {},
            followup_messages=[str(item) for item in followups if str(item or "").strip()],
        )

    def _pending_for_reservation(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        reservation_token: str,
    ) -> dict[str, Any] | None:
        if not self.state_store:
            return None
        state = self._channel_state()
        seen = state.get("seen_message_ids")
        entry = seen.get(internal_dedupe_key) if isinstance(seen, dict) else None
        if not (
            self._reservation_matches(entry, owner, reservation_token)
            and isinstance(entry, dict)
            and entry.get("status") == "processing"
        ):
            return None
        pending = entry.get("completion_pending")
        return dict(pending) if isinstance(pending, dict) else None

    def _mark_completion_pending(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        reservation_token: str,
        event: VeyraEvent,
        result: LoopResult,
        conversation_id: str | None,
    ) -> None:
        if not self.state_store:
            return
        pending = {
            "schema_version": "veyra.intake.completion-pending.v1",
            "event": event.to_dict(),
            "loop_result": result.to_dict(),
            "conversation_id": conversation_id,
            "created_at": utc_now_iso(),
        }

        def mark(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.get("seen_message_ids")
            entry = seen.get(internal_dedupe_key) if isinstance(seen, dict) else None
            if self._reservation_matches(entry, owner, reservation_token) and isinstance(entry, dict):
                entry["completion_pending"] = pending
                entry["completion_attempts"] = int(entry.get("completion_attempts") or 0) + 1
            return state

        self.state_store.mutate_json("channel_state.json", mark)

    def _clear_completion_pending(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        reservation_token: str,
    ) -> None:
        if not self.state_store:
            return

        def clear(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.get("seen_message_ids")
            entry = seen.get(internal_dedupe_key) if isinstance(seen, dict) else None
            if self._reservation_matches(entry, owner, reservation_token) and isinstance(entry, dict):
                entry.pop("completion_pending", None)
            return state

        self.state_store.mutate_json("channel_state.json", clear)

    def _record_completion_failure(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        reservation_token: str,
        error: Exception,
    ) -> None:
        if not self.state_store:
            return

        def record(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.get("seen_message_ids")
            entry = seen.get(internal_dedupe_key) if isinstance(seen, dict) else None
            if not self._reservation_matches(entry, owner, reservation_token):
                return state
            failures = state.setdefault("completion_failures", [])
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
            state["completion_failures"] = failures[-100:]
            return state

        self.state_store.mutate_json("channel_state.json", record)

    def _reserve_inbox(
        self,
        internal_dedupe_key: str,
        item: dict[str, Any],
        *,
        owner: dict[str, str],
        reservation_token: str,
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
            pending = existing.get("completion_pending") if isinstance(existing, dict) else None
            if self._seen_entry_matches(existing, owner):
                if self._can_takeover(existing):
                    # A new runtime may recover a reservation left by a
                    # crashed runtime.  The canonical event/message identity
                    # remains unchanged; only the lease owner changes.
                    pass
                else:
                    previous = self._public_previous(existing)
                    if (
                        isinstance(pending, dict)
                        and str(existing.get("instance_id") or "") == self.instance_id
                    ):
                        previous.update(
                            {
                                "_resume_completion": True,
                                "_completion_pending": pending,
                                "_internal_dedupe_key": internal_dedupe_key,
                                "_dedupe_owner": dict(owner),
                                "_reservation_token": existing.get("reservation_token"),
                            }
                        )
                    return state
            reservation = {
                "scope_version": _INTAKE_DEDUPE_SCOPE_VERSION,
                "owner": dict(owner),
                "event_id": item.get("event_id"),
                "session_id": owner["session_id"],
                "reserved_at": utc_now_iso(),
                "instance_id": self.instance_id,
                "reservation_token": reservation_token,
                "lease_expires_at": self._lease_expiry_iso(),
                "status": "processing",
            }
            if isinstance(pending, dict):
                # Once the original lease has expired, a replacement runtime
                # may recover the completion ledger, but it must not rerun
                # cognition or Agent work.
                reservation["completion_pending"] = pending
            seen[internal_dedupe_key] = reservation
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
        reservation_token: str,
        error: Exception,
    ) -> None:
        if not self.state_store:
            return

        def release(state: dict[str, Any]) -> dict[str, Any]:
            seen = state.get("seen_message_ids")
            if isinstance(seen, dict):
                reservation = seen.get(internal_dedupe_key)
                if (
                    self._reservation_matches(reservation, owner, reservation_token)
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

    def _reservation_is_current(
        self,
        internal_dedupe_key: str,
        *,
        owner: dict[str, str],
        reservation_token: str,
    ) -> bool:
        state = self._channel_state()
        seen = state.get("seen_message_ids")
        reservation = seen.get(internal_dedupe_key) if isinstance(seen, dict) else None
        return (
            self._reservation_matches(reservation, owner, reservation_token)
            and isinstance(reservation, dict)
            and reservation.get("status") == "processing"
        )

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
    def _event_id(owner: dict[str, str]) -> str:
        """Derive one stable event identity for one canonical provider message."""

        digest = framed_sha256(
            "veyra-intake-event-v1",
            owner["channel"],
            owner["user_id"],
            owner["session_id"],
            owner["message_id"],
        )
        return f"evt_{digest[:32]}"

    def _lease_expiry_iso(self) -> str:
        return datetime.fromtimestamp(
            time.time() + self.processing_lease_seconds,
            tz=timezone.utc,
        ).isoformat()

    def _can_takeover(self, entry: Any) -> bool:
        if not isinstance(entry, dict) or entry.get("status") != "processing":
            return False
        # Instance identity is not proof that the previous worker crashed.
        # Every runtime must respect the same durable lease until it expires;
        # otherwise two cognition/Agent executions can overlap.
        return self._lease_expired(entry)

    @staticmethod
    def _lease_expired(entry: dict[str, Any]) -> bool:
        value = entry.get("lease_expires_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) <= time.time()
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp() <= time.time()
            except ValueError:
                return True
        # Reservations written before lease ownership was introduced are
        # recoverable rather than permanently blocking the provider message.
        return True

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

    @classmethod
    def _reservation_matches(
        cls,
        entry: Any,
        owner: dict[str, str],
        reservation_token: str,
    ) -> bool:
        return cls._seen_entry_matches(entry, owner) and isinstance(entry, dict) and entry.get("reservation_token") == reservation_token

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
                "instance_id",
                "lease_expires_at",
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
