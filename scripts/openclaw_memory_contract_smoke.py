#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class ContractOpenClawAdapter(OpenClawAdapter):
    def __init__(self) -> None:
        super().__init__(base_url="ws://127.0.0.1:18789", api_key="contract-test")
        self.calls: list[dict[str, Any]] = []

    def _gateway_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"method": method, "params": params})
        if method == "memory.summary":
            return {"status": "success", "summary": "OpenClaw remembers the user's learning plan."}
        if method == "memory.patch":
            return {"status": "submitted", "memory_id": "mem_contract_1"}
        raise AssertionError(f"unexpected method: {method}")


def main() -> int:
    adapter = ContractOpenClawAdapter()
    summary = adapter.fetch_memory_summary("session-1")
    write = adapter.write_memory_patch(
        {
            "session_id": "session-1",
            "summary": "User authorized daily learning digests.",
            "confidence": 0.92,
        }
    )

    expect(summary.get("status") == "success", "memory summary status is surfaced", summary)
    expect("learning plan" in str(summary.get("summary")), "memory summary text is surfaced", summary)
    expect(write.get("status") == "submitted", "memory patch status is surfaced", write)
    expect([call["method"] for call in adapter.calls] == ["memory.summary", "memory.patch"], "OpenClaw memory methods are called in contract order", adapter.calls)
    expect(adapter.calls[0]["params"] == {"sessionId": "session-1", "sessionKey": adapter.session_key}, "memory.summary params match gateway contract", adapter.calls[0])
    expect(adapter.calls[1]["params"]["patch"]["session_id"] == "session-1", "memory.patch includes normalized patch", adapter.calls[1])

    print("OpenClaw memory contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
