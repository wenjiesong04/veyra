#!/usr/bin/env python3
"""Focused smoke for canonical foreground admission and product-tail scope."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.auth import AuthPolicy  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.intake_gateway import IntakeGateway  # noqa: E402
from interface.living_context_contract import situation_catalog_selector  # noqa: E402
from runtime.product_conversation_runtime import (  # noqa: E402
    ProductConversationNotFound,
    ProductConversationRuntime,
)


class SilentLoop:
    runtime_trace = None

    def handle_event(self, event: Any) -> Any:  # pragma: no cover - admission smoke never cognizes
        raise AssertionError("cognition must not run in this smoke")


class FlakyLoop:
    runtime_trace = None

    def __init__(self) -> None:
        self.fail = True

    def handle_event(self, event: Any) -> Any:
        if self.fail:
            self.fail = False
            raise RuntimeError("transient cognition failure")
        from core.definitions import RiskLevel
        from interface.event_schema import LoopResult, Route

        return LoopResult(
            event_id=event.event_id,
            route=Route.DIRECT_ANSWER,
            status="recorded",
            response="recovered",
            risk_level=RiskLevel.R0,
        )


class CompletionLoop:
    runtime_trace = None

    def handle_event(self, event: Any) -> Any:
        from core.definitions import RiskLevel
        from interface.event_schema import LoopResult, Route

        return LoopResult(
            event_id=event.event_id,
            route=Route.DIRECT_ANSWER,
            status="recorded",
            response="completion response",
            risk_level=RiskLevel.R0,
        )


class RejectAuth(AuthPolicy):
    def allow(self, channel: str, user_id: str, channel_config: dict[str, Any] | None = None) -> bool:
        return False


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(f"{label} failed")
    print(f"PASS {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-conversation-ingress-smoke-") as temporary:
        store = WorldStateStore(Path(temporary) / "state")
        runtime = ProductConversationRuntime(store)
        gateway = IntakeGateway(SilentLoop(), state_store=store)
        raw_session = "browser-session"
        canonical_session = gateway.session_mapper.map("webhook", "owner-a", raw_session)

        blocked_gateway = IntakeGateway(
            SilentLoop(),
            state_store=WorldStateStore(Path(temporary) / "blocked-state"),
            auth_policy=RejectAuth(),
        )
        blocked = blocked_gateway.admit_message(
            text="blocked",
            channel="webhook",
            user_id="owner-a",
            session_id=raw_session,
            message_id="blocked-message",
        )
        expect(blocked["status"] == "blocked", "blocked request stops before admission")
        expect(
            ProductConversationRuntime(blocked_gateway.state_store).list_conversations(
                owner_id="owner-a",
                session_id=canonical_session,
            ) == [],
            "blocked request creates no product conversation",
        )

        first = gateway.admit_message(
            text="first turn",
            channel="webhook",
            user_id="owner-a",
            session_id=raw_session,
            message_id="stable-message",
        )
        expect(first["status"] == "accepted", "accepted admission reserves before cognition")
        expect(first["session_id"] == canonical_session, "external session maps to canonical session")
        conversation = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="first turn",
            message_id=first["message_id"],
            responses=[],
            metadata={
                "channel": "webhook",
                "record_only": True,
                "event_id": first["event"].event_id,
            },
        )
        runtime_ref = str(conversation["conversation_id"])
        gateway.attach_conversation_metadata(first, conversation_id=runtime_ref)
        expect(
            conversation["messages"][0]["metadata"]["event_id"] == first["event"].event_id,
            "product user message preserves cognition event correlation",
        )
        try:
            runtime.get_conversation(runtime_ref, owner_id="owner-a", session_id=raw_session)
        except ProductConversationNotFound:
            raw_scope_visible = False
        else:
            raw_scope_visible = True
        expect(not raw_scope_visible, "raw external session cannot read canonical conversation")
        expect(
            runtime.list_conversations(owner_id="owner-a", session_id=raw_session) == [],
            "no raw-scope product conversation orphan is created",
        )

        duplicate_product = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="first turn",
            message_id=first["message_id"],
            responses=[],
            conversation_id=runtime_ref,
            metadata={
                "channel": "webhook",
                "record_only": True,
                "event_id": first["event"].event_id,
            },
        )
        expect(duplicate_product["status"] == "duplicate", "product duplicate is detected before cognition")
        gateway.finalize_admission_without_cognition(first, status="duplicate")
        reservation = store.read_json("channel_state.json")["seen_message_ids"][
            first["_internal_dedupe_key"]
        ]
        expect(
            reservation["status"] == "duplicate" and reservation.get("finalized_without_cognition") is True,
            "duplicate reservation is terminal and not left processing",
        )

        duplicate = gateway.admit_message(
            text="first turn",
            channel="webhook",
            user_id="owner-a",
            session_id=raw_session,
            message_id="stable-message",
        )
        expect(duplicate["status"] == "duplicate", "duplicate reservation is idempotent")
        expect(duplicate["previous"]["status"] == "duplicate", "terminal duplicate remains auditable on retry")
        expect(
            len(runtime.get_conversation(runtime_ref, owner_id="owner-a", session_id=canonical_session)["messages"]) == 1,
            "duplicate admission does not append a user orphan",
        )

        flaky_loop = FlakyLoop()
        retry_store = WorldStateStore(Path(temporary) / "retry-state")
        retry_gateway = IntakeGateway(flaky_loop, state_store=retry_store)
        retry_first = retry_gateway.admit_message(
            text="retryable turn",
            channel="webhook",
            user_id="retry-owner",
            session_id="retry-session",
            message_id="retry-message",
        )
        retry_runtime = ProductConversationRuntime(retry_store)
        retry_session = str(retry_first["session_id"])
        retry_event_id = retry_first["event"].event_id
        retry_runtime.record_foreground_turn(
            owner_id="retry-owner",
            session_id=retry_session,
            text="retryable turn",
            message_id="retry-message",
            responses=[],
            metadata={"event_id": retry_event_id},
        )
        try:
            retry_gateway.receive_message(admission=retry_first, text="retryable turn")
        except RuntimeError:
            pass
        else:
            raise AssertionError("flaky cognition did not fail on first attempt")
        retry_second = retry_gateway.admit_message(
            text="retryable turn",
            channel="webhook",
            user_id="retry-owner",
            session_id="retry-session",
            message_id="retry-message",
        )
        expect(retry_second["status"] == "accepted", "released cognition reservation accepts the same provider retry")
        expect(retry_second["event"].event_id == retry_event_id, "provider retry reuses the deterministic event identity")
        retry_result = retry_gateway.receive_message(admission=retry_second, text="retryable turn")
        expect(retry_result["status"] == "delivered", "same-message retry completes after transient cognition failure")
        retry_conversations = retry_runtime.list_conversations(
            owner_id="retry-owner",
            session_id=retry_session,
        )
        retry_runtime.record_foreground_turn(
            owner_id="retry-owner",
            session_id=retry_session,
            text="retryable turn",
            message_id="retry-message",
            response="recovered",
            conversation_id=str(retry_conversations[0]["conversation_id"]),
            metadata={"event_id": retry_event_id},
        )
        retry_conversations = retry_runtime.list_conversations(
            owner_id="retry-owner",
            session_id=retry_session,
        )
        expect(len(retry_conversations) == 1 and retry_conversations[0]["message_count"] == 2, "retry appends one assistant without a second user turn")

        # A reservation is active within one runtime instance, but a new
        # runtime can recover the exact canonical provider event after a
        # crash. A stale worker must not release the new owner's lease.
        recovery_store = WorldStateStore(Path(temporary) / "recovery-state")
        runtime_a = IntakeGateway(SilentLoop(), state_store=recovery_store, instance_id="runtime-a")
        runtime_b = IntakeGateway(SilentLoop(), state_store=recovery_store, instance_id="runtime-b")
        in_flight = runtime_a.admit_message(
            text="recover after crash",
            channel="webhook",
            user_id="recovery-owner",
            session_id="recovery-session",
            message_id="recovery-message",
        )
        expect(in_flight["status"] == "accepted", "first runtime owns processing reservation")
        same_runtime = runtime_a.admit_message(
            text="recover after crash",
            channel="webhook",
            user_id="recovery-owner",
            session_id="recovery-session",
            message_id="recovery-message",
        )
        expect(same_runtime["status"] == "duplicate", "same runtime active request remains duplicate")
        recovered = runtime_b.admit_message(
            text="recover after crash",
            channel="webhook",
            user_id="recovery-owner",
            session_id="recovery-session",
            message_id="recovery-message",
        )
        expect(recovered["status"] == "duplicate", "different runtime respects an unexpired processing lease")
        recovery_store.mutate_json(
            "channel_state.json",
            lambda state: {
                **state,
                "seen_message_ids": {
                    **(state.get("seen_message_ids") or {}),
                    in_flight["_internal_dedupe_key"]: {
                        **(state.get("seen_message_ids") or {}).get(in_flight["_internal_dedupe_key"], {}),
                        "lease_expires_at": "2000-01-01T00:00:00+00:00",
                    },
                },
            },
        )
        recovered = runtime_b.admit_message(
            text="recover after crash",
            channel="webhook",
            user_id="recovery-owner",
            session_id="recovery-session",
            message_id="recovery-message",
        )
        expect(recovered["status"] == "accepted", "new runtime can take over crashed reservation")
        expect(recovered["event"].event_id == in_flight["event"].event_id, "takeover keeps canonical event identity")
        runtime_a.release_admission(in_flight, RuntimeError("stale worker"))
        current_reservation = recovery_store.read_json("channel_state.json")["seen_message_ids"][recovered["_internal_dedupe_key"]]
        expect(current_reservation.get("reservation_token") == recovered["_reservation_token"], "stale worker cannot release recovered lease")
        runtime_b.release_admission(recovered, RuntimeError("recovery cleanup"))

        # Completion is a pre-delivery phase. A failed hook releases the
        # reservation, leaves the pending user intact, and a retry appends one
        # assistant message without sending the first failed attempt.
        completion_store = WorldStateStore(Path(temporary) / "completion-hook-state")
        completion_runtime = ProductConversationRuntime(completion_store)
        hook_attempts = 0
        cognition_calls = 0

        class CountingCompletionLoop(CompletionLoop):
            def handle_event(self, event: Any) -> Any:
                nonlocal cognition_calls
                cognition_calls += 1
                return super().handle_event(event)

        def completion_hook(admission: dict[str, Any], result: Any) -> dict[str, Any]:
            nonlocal hook_attempts
            hook_attempts += 1
            if hook_attempts == 1:
                raise RuntimeError("transient completion ledger failure")
            return completion_runtime.record_foreground_completion(
                owner_id=str(admission["user_id"]),
                session_id=str(admission["session_id"]),
                text=str(admission["event"].payload["text"]),
                message_id=str(admission["message_id"]),
                response=result.response,
                conversation_id=str(admission["conversation_id"]),
                metadata={"event_id": admission["event"].event_id},
            )

        completion_gateway = IntakeGateway(
            CountingCompletionLoop(),
            state_store=completion_store,
            completion_hook=completion_hook,
        )
        completion_admission = completion_gateway.admit_message(
            text="hook retry",
            channel="webhook",
            user_id="hook-owner",
            session_id="hook-session",
            message_id="hook-message",
        )
        completion_user = completion_runtime.record_foreground_turn(
            owner_id="hook-owner",
            session_id=str(completion_admission["session_id"]),
            text="hook retry",
            message_id="hook-message",
            responses=[],
            metadata={"event_id": completion_admission["event"].event_id},
        )
        completion_admission["conversation_id"] = completion_user["conversation_id"]
        completion_gateway.attach_conversation_metadata(
            completion_admission,
            conversation_id=str(completion_user["conversation_id"]),
        )
        try:
            completion_gateway.receive_message(text="hook retry", admission=completion_admission)
        except RuntimeError:
            pass
        else:
            raise AssertionError("completion hook failure did not surface")
        expect(completion_store.read_json("channel_state.json").get("outbox") == [], "failed completion hook sends no channel message")
        pending_reservation = completion_store.read_json("channel_state.json")["seen_message_ids"][completion_admission["_internal_dedupe_key"]]
        expect(pending_reservation.get("status") == "processing" and pending_reservation.get("completion_pending"), "failed completion keeps a durable completion-only reservation")
        retry_admission = completion_gateway.admit_message(
            text="hook retry",
            channel="webhook",
            user_id="hook-owner",
            session_id="hook-session",
            message_id="hook-message",
        )
        expect(retry_admission["status"] == "accepted", "completion hook failure reopens a completion-only retry")
        retry_admission["conversation_id"] = completion_user["conversation_id"]
        completion_gateway.attach_conversation_metadata(
            retry_admission,
            conversation_id=str(completion_user["conversation_id"]),
        )
        completion_result = completion_gateway.receive_message(text="hook retry", admission=retry_admission)
        expect(completion_result["status"] == "delivered", "completion retry delivers after hook recovery")
        expect(cognition_calls == 1, "completion retry does not rerun cognition")
        completed = completion_runtime.get_conversation(
            str(completion_user["conversation_id"]),
            owner_id="hook-owner",
            session_id=str(completion_admission["session_id"]),
        )
        expect(completed is not None and [item["role"] for item in completed["messages"]] == ["user", "assistant"], "completion hook appends one assistant after pending user")
        expect(len(completion_store.read_json("channel_state.json").get("outbox") or []) == 1, "retry produces one channel delivery")

        injected = gateway.admit_message(
            text="metadata injection",
            channel="webhook",
            user_id="owner-a",
            session_id=raw_session,
            message_id="metadata-injection",
            metadata={"_internal": {"conversation_id": "attacker-conversation"}, "safe": "kept"},
        )
        expect("_internal" not in injected["event"].payload["metadata"], "client internal metadata is stripped before event creation")
        inbox_items = store.read_json("channel_state.json").get("inbox") or []
        injected_inbox = next(item for item in inbox_items if item.get("message_id") == "metadata-injection")
        expect("_internal" not in injected_inbox.get("metadata", {}), "client internal metadata is stripped before inbox persistence")
        gateway.release_admission(injected, RuntimeError("metadata smoke cleanup"))

        # Build a selected conversation with a prior exchange plus the current
        # pre-written user turn.  The context tail must omit that current turn
        # and must fail closed to the channel tail when the scope is wrong.
        runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="prior question",
            message_id="prior-user",
            response="prior answer",
            conversation_id=runtime_ref,
        )
        current = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="current question",
            message_id="current-user",
            responses=[],
            conversation_id=runtime_ref,
        )
        runtime.append_message(
            runtime_ref,
            owner_id="owner-a",
            session_id=canonical_session,
            message_id="future-response",
            role="assistant",
            text="future completion must stay out of context",
        )
        event = EventNormalizer().user_message(
            text="  current question  ",
            channel="webhook",
            user_id="owner-a",
            session_id=canonical_session,
            metadata={
                "_internal": {
                    "conversation_id": runtime_ref,
                    "message_id": "current-user",
                    "canonical_session_id": canonical_session,
                }
            },
        )
        context = TurnContextBuilder(store).build(
            user_message="current question",
            attention_focus=[],
            event=event,
        )
        tail = context["short_memory"]["conversation_tail"]
        texts = [str(item.get("text") or "") for item in tail]
        expect("current question" not in texts, "selected product tail excludes pre-written current user")
        expect("prior question" in texts and "prior answer" in texts, "selected product tail preserves prior exchange")
        expect(any(item.get("source") == "product_conversation" for item in tail), "canonical text identity keeps the selected product tail with edge whitespace")
        expect("future completion must stay out of context" not in texts, "selected product tail excludes messages after current turn")

        wrong_scope_event = EventNormalizer().user_message(
            text="current question",
            channel="webhook",
            user_id="owner-b",
            session_id=canonical_session,
            metadata={
                "_internal": {
                    "conversation_id": runtime_ref,
                    "message_id": "current-user",
                    "canonical_session_id": canonical_session,
                }
            },
        )
        fallback = TurnContextBuilder(store).build(
            user_message="current question",
            attention_focus=[],
            event=wrong_scope_event,
        )
        expect(
            all(item.get("source") != "product_conversation" for item in fallback["short_memory"]["conversation_tail"]),
            "wrong owner falls back without cross-conversation tail",
        )
        missing_current_event = EventNormalizer().user_message(
            text="current question",
            channel="webhook",
            user_id="owner-a",
            session_id=canonical_session,
            metadata={
                "_internal": {
                    "conversation_id": runtime_ref,
                    "message_id": "missing-current",
                    "canonical_session_id": canonical_session,
                }
            },
        )
        missing_current_context = TurnContextBuilder(store).build(
            user_message="current question",
            attention_focus=[],
            event=missing_current_event,
        )
        expect(
            all(item.get("source") != "product_conversation" for item in missing_current_context["short_memory"]["conversation_tail"]),
            "missing current message disables product history injection",
        )

        # A server-bound Situation is projected into model context only as a
        # selector.  Scope or injected conversation identities never become a
        # continuation hint.
        bound_id = "sit_sem_conversation_anchor"
        bound = runtime.ensure_conversation(
            owner_id="owner-a",
            session_id=canonical_session,
            binding_type="situation",
            binding_id=bound_id,
            title="Bound Situation",
        )
        bound_ref = str(bound["conversation_id"])
        bound_meta = {"channel": "webhook", "record_only": True, "event_id": "bound-event"}
        runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="bound current",
            message_id="bound-current",
            responses=[],
            conversation_id=bound_ref,
            metadata=bound_meta,
        )
        bound_event = EventNormalizer().user_message(
            text="bound current",
            channel="webhook",
            user_id="owner-a",
            session_id=canonical_session,
            metadata={
                "_internal": {
                    "conversation_id": bound_ref,
                    "message_id": "bound-current",
                    "canonical_session_id": canonical_session,
                }
            },
        )
        bound_catalog = [{
            "situation_token": bound_id,
            "owner_id": "owner-a",
            "session_id": canonical_session,
            "observation_revision": 1,
        }]
        bound_context = TurnContextBuilder(store).build(
            user_message="bound current",
            attention_focus=[],
            event=bound_event,
            living_context_situation_candidates=bound_catalog,
        )
        expected_selector = situation_catalog_selector("owner-a", canonical_session, bound_id)
        expect(
            bound_context.get("conversation_binding") == {
                "binding_type": "situation",
                "situation_selector": expected_selector,
            },
            "bound conversation projects an opaque Situation selector",
        )
        expect(
            "binding_id" not in bound_context.get("conversation_binding", {})
            and bound_id not in str(bound_context.get("conversation_binding")),
            "conversation binding does not expose the raw Situation ID",
        )
        injected_event = EventNormalizer().user_message(
            text="bound current",
            channel="webhook",
            user_id="owner-b",
            session_id=canonical_session,
            metadata={
                "_internal": {
                    "conversation_id": bound_ref,
                    "message_id": "bound-current",
                    "canonical_session_id": canonical_session,
                }
            },
        )
        injected_context = TurnContextBuilder(store).build(
            user_message="bound current",
            attention_focus=[],
            event=injected_event,
            living_context_situation_candidates=bound_catalog,
        )
        expect(injected_context.get("conversation_binding") is None, "wrong-owner injected binding is rejected")

        mismatch_user = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="mismatch current",
            message_id="mismatch-current",
            responses=[],
            conversation_id=bound_ref,
            metadata=bound_meta,
        )
        mismatch = runtime.record_foreground_completion(
            owner_id="owner-a",
            session_id=canonical_session,
            text="mismatch current",
            message_id="mismatch-current",
            conversation_id=bound_ref,
            response="assistant stays in the admitted thread",
            situation_id="sit_sem_other",
            metadata=bound_meta,
        )
        expect(
            mismatch.get("diagnostic", {}).get("code") == "conversation_binding_mismatch",
            "mismatched Situation returns a structured continuation diagnostic",
        )
        expect(
            mismatch.get("_server_diagnostic", {}).get("expected_digest")
            and "sit_sem_other" not in str(mismatch),
            "mismatch diagnostic keeps bounded IDs server-side",
        )
        completed_bound = runtime.get_conversation(
            bound_ref,
            owner_id="owner-a",
            session_id=canonical_session,
        )
        expect(
            completed_bound
            and completed_bound.get("binding_id") == bound_id
            and any(item.get("text") == "assistant stays in the admitted thread" for item in completed_bound.get("messages", [])),
            "mismatch completion appends assistant without rebinding the thread",
        )
        runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id=canonical_session,
            text="same binding current",
            message_id="same-binding-current",
            responses=[],
            conversation_id=bound_ref,
            metadata=bound_meta,
        )
        same_binding = runtime.record_foreground_completion(
            owner_id="owner-a",
            session_id=canonical_session,
            text="same binding current",
            message_id="same-binding-current",
            conversation_id=bound_ref,
            response="same Situation remains the thread",
            situation_id=bound_id,
            metadata=bound_meta,
        )
        expect(
            "diagnostic" not in same_binding
            and any(item.get("text") == "same Situation remains the thread" for item in same_binding.get("messages", [])),
            "same Situation completion remains a normal bound turn",
        )
        assert current["conversation_id"] == runtime_ref
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
