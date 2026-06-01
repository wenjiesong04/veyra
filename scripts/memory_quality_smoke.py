#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402


class FakeMemoryAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return ExecutionResult(task_id=task_packet.task_id, executor="fake", status="success", result="ok")

    def connection_status(self) -> dict[str, Any]:
        return {"status": "available", "connected": True}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"status": "success", "summary": f"external memory for {session_id}", "freshness": "fresh", "trust": "fake"}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(memory_patch)
        return {"status": "submitted", "memory_id": "fake-memory-id"}


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-memory-quality-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        fake = FakeMemoryAdapter()
        bridge = LocalMemoryBridge(store, adapter_resolver=lambda: fake, adapter_getter=lambda provider: fake, provider_names=lambda: ["openclaw"])

        patch = {"session_id": "mem-session", "task": "remember deep learning plan", "result": "start with basics", "trust": "verified"}
        first = bridge.write_patch(patch, provider="openclaw")
        second = bridge.write_patch(patch, provider="openclaw")
        expect(first["status"] == "written" and not first["deduped"], "first memory write stored", first)
        expect(second["status"] == "written" and second["deduped"], "duplicate memory write deduped", second)

        state = store.read_json("agent_memory.json")
        items = state.get("items") if isinstance(state.get("items"), list) else []
        expect(len(items) == 1, "duplicate memory collapsed to one item", items)
        item = items[0]
        expect(bool(item.get("memory_id")) and item.get("merged_count") == 2, "memory item has stable id and merge count", item)
        expect((item.get("quality") or {}).get("score", 0) > 0.8, "memory quality score recorded", item)
        expect(fake.writes and fake.writes[-1]["summary"], "external adapter receives normalized patch", fake.writes)

        expired = {
            "memory_id": "expired",
            "patch": {"session_id": "old", "task": "old", "result": "old"},
            "provider": "local",
            "quality": {"score": 0.1},
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        state["items"].append(expired)
        store.write_json("agent_memory.json", state)
        bridge.write_patch({"session_id": "mem-session", "task": "new stable note", "result": "keep"}, provider="local")
        refreshed_items = store.read_json("agent_memory.json").get("items", [])
        expect(not any(entry.get("memory_id") == "expired" for entry in refreshed_items if isinstance(entry, dict)), "expired memory pruned on write", refreshed_items)

        summary = bridge.read_summary("mem-session", ["deep learning"], provider="openclaw")
        expect(summary["external_summary"]["status"] == "success" and summary["summary"], "memory summary includes local and external memory", summary)

        unconfigured_openclaw = OpenClawAdapter(base_url="")
        openclaw_summary = unconfigured_openclaw.fetch_memory_summary("mem-session")
        openclaw_write = unconfigured_openclaw.write_memory_patch({"session_id": "mem-session", "summary": "x"})
        expect(openclaw_summary["status"] == "not_configured", "unconfigured OpenClaw memory summary is explicit", openclaw_summary)
        expect(openclaw_write["status"] == "not_configured", "unconfigured OpenClaw memory write is explicit", openclaw_write)

    print("memory quality smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
