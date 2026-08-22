#!/usr/bin/env python3
"""Contract smoke for the connection-scoped lifecycle SSE endpoint."""

from __future__ import annotations

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


def run_case(main_module: Any, receipt: Any = None, *, raises: bool = False) -> list[dict[str, Any]]:
    original = main_module.intake_gateway.receive_message

    def fake_receive(**_: Any) -> Any:
        if raises:
            raise RuntimeError("isolated intake failure")
        return receipt

    main_module.intake_gateway.receive_message = fake_receive
    try:
        with TestClient(main_module.app) as client:
            response = client.post(
                "/events/message/stream",
                json={"text": "stream smoke", "channel": "api", "user_id": "smoke-user", "session_id": "smoke-session"},
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
        events = run_case(main_module, delivered)
        assert [event["type"] for event in events] == ["accepted", "phase", "message", "completed"], events
        assert events[2]["payload"] == main_module._public_message_result(delivered)
        assert events[3]["payload"]["status"] == "recorded"
        assert_no_stream_tokens(events)
        conversation_id = str(events[2]["payload"].get("conversation_id") or "")
        conversation = main_module.product_conversation_runtime.get_conversation(
            conversation_id,
            owner_id="smoke-user",
            session_id="mapped-smoke-session",
        )
        assert conversation is not None and [item["role"] for item in conversation["messages"]] == ["user", "assistant"]

        duplicate = {"status": "duplicate", "previous": {"event_id": "event-1"}}
        duplicate_events = run_case(main_module, duplicate)
        assert [event["type"] for event in duplicate_events] == ["accepted", "phase", "message", "completed"]
        assert duplicate_events[2]["payload"]["status"] == "duplicate"
        assert duplicate_events[3]["payload"]["status"] == "duplicate"
        assert duplicate_events[2]["payload"]["route"] == "duplicate"
        assert_no_stream_tokens(duplicate_events)

        rejected = {"status": "blocked", "reason": "policy denied"}
        rejected_events = run_case(main_module, rejected)
        assert [event["type"] for event in rejected_events] == ["accepted", "phase", "message", "completed"]
        assert rejected_events[2]["payload"]["status"] == "blocked"
        assert rejected_events[2]["payload"]["route"] == "block"
        assert rejected_events[3]["payload"]["status"] == "blocked"
        assert_no_stream_tokens(rejected_events)

        failed_events = run_case(main_module, raises=True)
        assert [event["type"] for event in failed_events] == ["accepted", "phase", "failed"], failed_events
        assert isinstance(failed_events[-1]["payload"].get("message"), str)
        assert_no_stream_tokens(failed_events)

    print("message stream smoke passed (lifecycle, duplicate, rejected, failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
