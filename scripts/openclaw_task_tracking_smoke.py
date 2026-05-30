#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter, OpenClawGatewayError  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    adapter = OpenClawAdapter(base_url="ws://127.0.0.1:18789")

    def gateway_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "agent.wait":
            timeout_ms = int(params.get("timeoutMs") or 0)
            if timeout_ms <= 1000:
                return {"runId": params["runId"], "status": "timeout"}
            return {
                "runId": params["runId"],
                "status": "ok",
                "startedAt": 1,
                "endedAt": 2,
            }
        if method == "chat.history":
            return {
                "sessionKey": "main",
                "messages": [
                    {"role": "user", "text": "hello"},
                    {"role": "assistant", "text": "async answer ready"},
                ],
            }
        if method == "status":
            return {"tasks": {"total": 0, "active": 0, "failures": 0}}
        raise OpenClawGatewayError("method_unavailable", f"{method} unavailable")

    with patch.object(adapter, "_gateway_request", side_effect=gateway_request):
        with patch.object(adapter, "_listen_for_chat_run", return_value={"state": "submitted", "reason": "timeout"}):
            running = adapter.fetch_task_status("veyra-test-running")
            expect(running.status == "running", "agent.wait timeout maps to running", running.to_dict())
            expect("not present in the current status snapshot" not in running.result, "running poll avoids snapshot miss", running.result)

            with patch.object(adapter, "_status_wait_ms", return_value=1500):
                success = adapter.fetch_task_status("veyra-test-success")
            expect(success.status == "success", "agent.wait ok maps to success", success.to_dict())
            expect("async answer ready" in success.result, "success poll reads chat.history", success.result)

        adapter._remember_chat_run(
            "veyra-test-cache",
            final_event={"state": "final", "message": {"text": "cached final"}},
            status="success",
            result="cached final",
        )
        cached = adapter.fetch_task_status("veyra-test-cache")
        expect(cached.status == "success" and cached.result == "cached final", "terminal cache short-circuits polling", cached.to_dict())

        with patch.object(adapter, "_listen_for_chat_run", return_value={"state": "submitted", "reason": "timeout"}):
            polled = adapter.poll_task("veyra-test-poll", timeout_seconds=2)
        expect(polled.status == "success", "poll_task uses agent.wait with full timeout", polled.to_dict())

    with patch.object(adapter, "_gateway_request", side_effect=lambda method, params: {"tasks": {"total": 0, "active": 0}} if method == "status" else {}):
        with patch.object(adapter, "_agent_wait", side_effect=OpenClawGatewayError("method_unavailable", "missing")):
            with patch.object(adapter, "_listen_for_chat_run", return_value={"state": "submitted", "reason": "timeout"}):
                legacy = adapter.fetch_task_status("veyra-test-legacy")
                expect(legacy.status == "running", "chat run fallback avoids snapshot miss", legacy.to_dict())
                expect("not present in the current status snapshot" not in legacy.result, "legacy chat fallback message", legacy.result)

    print("openclaw_task_tracking_smoke passed")


if __name__ == "__main__":
    main()
