#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.capability_registry import CapabilityRegistry
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.openclaw_adapter import OpenClawAdapter


def expect(condition: bool, label: str, details: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def main() -> int:
    adapter = OpenClawAdapter(base_url="ws://127.0.0.1:18789")
    tools = adapter._tools_summary(
        {
            "groups": [
                {
                    "id": "workspace",
                    "tools": [
                        {"id": "shell", "enabled": True},
                        {"name": "filesystem"},
                        {"id": "browser.fetch", "disabled": True},
                    ],
                }
            ]
        }
    )
    skills = adapter._skills_summary(
        {
            "skills": [
                {"id": "code_edit", "eligible": True},
                {"name": "web_search", "eligible": False},
            ]
        }
    )
    expect([item["id"] for item in tools["items"]] == ["shell", "filesystem", "browser.fetch"], "tool identities are retained", tools)
    expect([item["id"] for item in skills["items"]] == ["code_edit", "web_search"], "skill identities are retained", skills)

    with tempfile.TemporaryDirectory(prefix="veyra-capabilities-") as temp_dir:
        store = WorldStateStore(Path(temp_dir) / "state")
        store.write_json(
            "agent_config.json",
            {
                "selected_agent": "openclaw",
                "agents": {
                    "openclaw": {
                        "enabled": True,
                        "base_url": "ws://127.0.0.1:18789",
                    }
                },
            },
        )
        store.write_json(
            "executor_state.json",
            {
                "selected_agent": "openclaw",
                "capability_snapshot": {
                    "runtime": "openclaw",
                    "updated_at": utc_now_iso(),
                    "ttl_seconds": 300,
                    "raw": {"tools": tools, "skills": skills},
                },
            },
        )
        snapshot = CapabilityRegistry(store).snapshot()
        agent = snapshot["agent_capabilities"]
        expect(agent["openclaw.shell"]["available"], "shell capability derives from concrete tool id", agent["openclaw.shell"])
        expect(agent["openclaw.file"]["available"], "file capability derives from concrete filesystem id", agent["openclaw.file"])
        expect(agent["openclaw.code_edit"]["available"], "code edit capability derives from concrete skill id", agent["openclaw.code_edit"])
        expect(agent["openclaw.web_search"]["available"], "web search capability derives from concrete skill id", agent["openclaw.web_search"])

    print("openclaw capability identity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
