#!/usr/bin/env python3
"""Contract smoke for the connection-scoped lifecycle SSE endpoint."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FORBIDDEN_STREAM_KEYS = {"delta", "token", "tokens", "thought", "cot", "chain_of_thought"}


def parse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line]
        data = next((line[5:] for line in lines if line.startswith("data:")), "")
        if not data:
            continue
        events.append(json.loads(data))
    return events


def assert_no_stream_tokens(value: Any) -> None:
    if isinstance(value, dict):
        assert not FORBIDDEN_STREAM_KEYS.intersection(value), value
        for child in value.values():
            assert_no_stream_tokens(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_stream_tokens(child)


async def run_disconnect_case(main_module: Any) -> None:
    from core.definitions import RiskLevel
    from interface.event_schema import LoopResult, Route

    original_handler = main_module.intake_gateway.awareness_loop.handle_event
    original_receive = main_module._receive_message
    started = asyncio.Event()
    finished = asyncio.Event()

    def fake_handler(event: Any) -> Any:
        return LoopResult(
            event_id=event.event_id,
            route=Route.DIRECT_ANSWER,
            status="recorded",
            response="disconnect recovery response",
            risk_level=RiskLevel.R0,
        )

    async def delayed_receive(request: Any, *, admission: Any = None) -> Any:
        started.set()
        await asyncio.sleep(0.05)
        try:
            return await original_receive(request, admission=admission)
        finally:
            finished.set()

    main_module.intake_gateway.awareness_loop.handle_event = fake_handler
    main_module._receive_message = delayed_receive
    try:
        request = main_module.MessageRequest(
            text="disconnect smoke",
            channel="api",
            user_id="disconnect-user",
            session_id="disconnect-session",
            message_id="disconnect-message",
        )
        response = await main_module.message_stream(request)
        iterator = response.body_iterator
        accepted = await iterator.__anext__()
        phase = await iterator.__anext__()
        assert "event: accepted" in accepted and "event: phase" in phase
        heartbeat = asyncio.create_task(iterator.__anext__())
        await started.wait()
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        conversations = main_module.product_conversation_runtime.list_conversations(
            owner_id="disconnect-user",
            session_id=main_module.intake_gateway.session_mapper.map(
                "api",
                "disconnect-user",
                "disconnect-session",
            ),
        )
        assert len(conversations) == 1 and conversations[0]["message_count"] == 2, conversations
    finally:
        main_module.intake_gateway.awareness_loop.handle_event = original_handler
        main_module._receive_message = original_receive


async def run_retry_case(main_module: Any) -> None:
    from core.definitions import RiskLevel
    from interface.event_schema import LoopResult, Route

    original_handler = main_module.intake_gateway.awareness_loop.handle_event
    attempts = 0

    def flaky_handler(event: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry smoke cognition failure")
        return LoopResult(
            event_id=event.event_id,
            route=Route.DIRECT_ANSWER,
            status="recorded",
            response="retry completed",
            risk_level=RiskLevel.R0,
        )

    main_module.intake_gateway.awareness_loop.handle_event = flaky_handler
    try:
        request = main_module.MessageRequest(
            text="retry the same provider message",
            channel="api",
            user_id="retry-stream-user",
            session_id="retry-stream-session",
            message_id="retry-stream-message",
        )
        first = await main_module._admit_foreground_user(request)
        first_event_id = first["event"].event_id
        try:
            await main_module._receive_message(
                main_module._bind_foreground_admission(request, first),
                admission=first,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("first retry attempt did not fail")
        second = await main_module._admit_foreground_user(request)
        assert second.get("status") == "accepted"
        assert second["event"].event_id == first_event_id
        receipt = await main_module._receive_message(
            main_module._bind_foreground_admission(request, second),
            admission=second,
        )
        assert receipt.get("status") == "delivered"
        canonical = main_module.intake_gateway.session_mapper.map(
            "api",
            "retry-stream-user",
            "retry-stream-session",
        )
        conversations = main_module.product_conversation_runtime.list_conversations(
            owner_id="retry-stream-user",
            session_id=canonical,
        )
        assert len(conversations) == 1 and conversations[0]["message_count"] == 2, conversations
    finally:
        main_module.intake_gateway.awareness_loop.handle_event = original_handler


def run_case(
    main_module: Any,
    receipt: Any = None,
    *,
    message_id: str,
    raises: bool = False,
) -> list[dict[str, Any]]:
    original = main_module.intake_gateway.receive_message

    def fake_receive(**kwargs: Any) -> Any:
        admission = kwargs.get("admission")
        if raises:
            if isinstance(admission, dict):
                main_module.intake_gateway.release_admission(
                    admission,
                    RuntimeError("isolated intake failure"),
                )
            raise RuntimeError("isolated intake failure")
        if isinstance(admission, dict) and admission.get("status") == "accepted":
            if isinstance(receipt, dict) and receipt.get("status") == "delivered":
                from core.definitions import RiskLevel
                from interface.event_schema import LoopResult, Route

                selected = dict(receipt)
                selected["message_id"] = admission.get("message_id")
                selected["session_id"] = admission.get("session_id")
                selected["conversation_id"] = admission.get("conversation_id")
                selected["event"] = admission.get("event").to_dict()
                loop_result = dict(selected.get("loop_result") or {})
                loop_result["event_id"] = admission.get("event").event_id
                model_result = LoopResult(
                    event_id=admission.get("event").event_id,
                    route=Route.DIRECT_ANSWER,
                    status=str(loop_result.get("status") or "recorded"),
                    response=str(loop_result.get("response") or ""),
                    risk_level=RiskLevel.R0,
                    artifacts=dict(loop_result.get("artifacts") or {}),
                )
                recording = main_module.intake_gateway.completion_hook(admission, model_result)
                selected["conversation_id"] = recording.get("conversation_id")
                selected["conversation_recording"] = recording
                loop_result["conversation_id"] = recording.get("conversation_id")
                loop_result["conversation_recording"] = recording
                selected["loop_result"] = loop_result
                return selected
            main_module.intake_gateway.release_admission(
                admission,
                RuntimeError("smoke receipt rejected before cognition"),
            )
        return receipt

    main_module.intake_gateway.receive_message = fake_receive
    try:
        with TestClient(main_module.app) as client:
            response = client.post(
                "/events/message/stream",
                json={"text": "stream smoke", "channel": "api", "user_id": "smoke-user", "session_id": "smoke-session", "message_id": message_id},
            )
        assert response.status_code == 200, response.text
        return parse_events(response.text)
    finally:
        main_module.intake_gateway.receive_message = original


def main() -> int:
    with TemporaryDirectory(prefix="veyra-message-stream-smoke-") as raw_root:
        root = Path(raw_root)
        os.environ.update(
            {
                "VEYRA_STATE_ROOT": str(root / "state"),
                "VEYRA_STATE_DIR": str(root / "state"),
                "VEYRA_AGENCY_ROOT": str(root / "agency"),
                "VEYRA_AGENCY_DIR": str(root / "agency"),
                "VEYRA_ACTIVE_LOOP_AUTOSTART": "0",
                "VEYRA_FEISHU_WS_AUTOSTART": "0",
                "VEYRA_CORE_MODEL_ENABLED": "0",
                "VEYRA_HOST": "127.0.0.1",
                "VEYRA_PORT": "18080",
                "VEYRA_LOCAL_API_TOKEN": "",
            }
        )
        import main as main_module

        delivered = {
            "status": "delivered",
            "message_id": "stream-smoke-message",
            "session_id": "mapped-smoke-session",
            "event": {"event_id": "event-1"},
            "loop_result": {"event_id": "event-1", "route": "record_only", "status": "recorded", "response": "hello", "risk_level": "R0", "artifacts": {}},
        }
        events = run_case(main_module, delivered, message_id="stream-smoke-message")
        assert [event["type"] for event in events] == ["accepted", "phase", "message", "completed"], events
        assert events[0]["payload"].get("conversation_id"), events[0]
        result = events[2]["payload"]
        conversation_id = str(result.get("conversation_id") or "")
        expected = main_module._public_message_result(delivered)
        expected["event_id"] = result["event_id"]
        expected["conversation_id"] = conversation_id
        if "conversation_recording" in result:
            expected["conversation_recording"] = result["conversation_recording"]
        assert result == expected, result
        assert events[3]["payload"]["status"] == "recorded"
        assert_no_stream_tokens(events)
        conversation = main_module.product_conversation_runtime.get_conversation(
            conversation_id,
            owner_id="smoke-user",
            session_id=main_module.intake_gateway.session_mapper.map(
                "api",
                "smoke-user",
                "smoke-session",
            ),
        )
        assert conversation is not None and [item["role"] for item in conversation["messages"]] == ["user", "assistant"]
        assert conversation["messages"][0]["metadata"].get("event_id") == result["event_id"]

        duplicate = {"status": "duplicate", "previous": {"event_id": "event-1"}}
        duplicate_events = run_case(main_module, duplicate, message_id="stream-smoke-message")
        assert [event["type"] for event in duplicate_events] == ["accepted", "phase", "message", "completed"]
        assert duplicate_events[2]["payload"]["status"] == "duplicate"
        assert duplicate_events[3]["payload"]["status"] == "duplicate"
        assert duplicate_events[2]["payload"]["route"] == "duplicate"
        assert_no_stream_tokens(duplicate_events)

        rejected = {"status": "blocked", "reason": "policy denied"}
        rejected_events = run_case(main_module, rejected, message_id="rejected-stream-message")
        assert [event["type"] for event in rejected_events] == ["accepted", "phase", "message", "completed"]
        assert rejected_events[2]["payload"]["status"] == "blocked"
        assert rejected_events[2]["payload"]["route"] == "block"
        assert rejected_events[3]["payload"]["status"] == "blocked"
        assert_no_stream_tokens(rejected_events)

        failed_events = run_case(main_module, raises=True, message_id="failed-stream-message")
        assert [event["type"] for event in failed_events] == ["accepted", "phase", "failed"], failed_events
        assert isinstance(failed_events[-1]["payload"].get("message"), str)
        assert_no_stream_tokens(failed_events)

        asyncio.run(run_disconnect_case(main_module))
        asyncio.run(run_retry_case(main_module))

    print("message stream smoke passed (lifecycle, duplicate, rejected, failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
