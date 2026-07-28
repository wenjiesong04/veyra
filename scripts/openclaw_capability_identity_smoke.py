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
from interface.agent_contract import (
    AGENT_CONTRACT_VERSION,
    normalize_capabilities,
)
from interface.event_schema import VeyraTaskPacket, utc_now_iso
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
                "status": "available",
                "connected": True,
                "ttl_seconds": 300,
                "capabilities": {
                    **normalize_capabilities(
                        {
                        "runtime": "openclaw",
                        "status": "available",
                        "connected": True,
                        "contract_version": AGENT_CONTRACT_VERSION,
                        "features": {
                            "structured_task_packet": True,
                            "rendered_prompt_fallback": True,
                            "task_status": True,
                            "stop_task": True,
                            "tool_proxy_enforced": True,
                            "tool_proxy_identity_match": True,
                            "tool_proxy_enforcement_scope": (
                                "veyra_governed_openclaw_sessions"
                            ),
                            "governance_callbacks_complete": True,
                        },
                        "tools": tools,
                        "skills": skills,
                        "compatibility": {
                            "status": "compatible",
                            "native_adapter": False,
                        },
                        },
                        runtime="openclaw",
                    ),
                    "updated_at": utc_now_iso(),
                    "ttl_seconds": 300,
                },
            },
        )
        snapshot = CapabilityRegistry(store).snapshot()
        agent = snapshot["agent_capabilities"]
        expect(agent["openclaw.shell"]["available"], "shell capability derives from concrete tool id", agent["openclaw.shell"])
        expect(agent["openclaw.file"]["available"], "file capability derives from concrete filesystem id", agent["openclaw.file"])
        expect(agent["openclaw.code_edit"]["available"], "code edit capability derives from concrete skill id", agent["openclaw.code_edit"])
        expect(agent["openclaw.web_search"]["available"], "web search capability derives from concrete skill id", agent["openclaw.web_search"])

    callback = lambda *_args, **_kwargs: {}  # noqa: E731
    guarded = OpenClawAdapter(
        base_url="ws://127.0.0.1:18789",
        governance_dispatch_preparer=callback,
        governance_dispatch_canceller=callback,
        governance_run_evidence_resolver=callback,
        governance_status_resolver=callback,
    )

    def dispatch_capabilities(enforced: bool) -> dict[str, Any]:
        return normalize_capabilities(
            {
                "runtime": "openclaw",
                "status": "available",
                "connected": True,
                "features": {
                    "structured_task_packet": True,
                    "rendered_prompt_fallback": True,
                    "task_status": True,
                    "stop_task": True,
                    "tool_proxy_enforced": enforced,
                    "tool_proxy_identity_match": enforced,
                    "tool_proxy_enforcement_scope": (
                        "veyra_governed_openclaw_sessions"
                    ),
                    "governance_callbacks_complete": True,
                },
                "compatibility": {
                    "status": "compatible",
                    "native_adapter": True,
                },
            },
            runtime="openclaw",
        )

    guarded.fetch_capabilities = (  # type: ignore[method-assign]
        lambda *, force_refresh=False: dispatch_capabilities(False)
    )
    send_calls: list[str] = []

    def must_not_send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        send_calls.append("sent")
        raise AssertionError(
            "OpenClaw prompt escaped without fresh Tool Proxy enforcement"
        )

    guarded._send_chat = must_not_send  # type: ignore[assignment]
    blocked = guarded.send_task(
        VeyraTaskPacket(
            task_id="task_openclaw_preflight_block",
            target_agent="openclaw",
            session_id="openclaw-preflight",
            user_message="Attempt one Agent task.",
            context_patch={},
            persona_patch={},
            policy_patch={"risk_level": "R0"},
            agent_execution_session_id=(
                "agent-exec:task_openclaw_preflight_block"
            ),
        )
    )
    expect(
        blocked.status == "blocked"
        and blocked.raw.get("reason")
        == "openclaw_governed_dispatch_not_verified"
        and send_calls == [],
        "OpenClaw dispatch fails before prompt transmission when enforcement is pending",
        blocked.to_dict(),
    )

    guarded.fetch_capabilities = (  # type: ignore[method-assign]
        lambda *, force_refresh=False: dispatch_capabilities(True)
    )
    allowed = guarded._governed_dispatch_preflight()
    expect(
        allowed["allowed"] is True
        and allowed["status"] == "validated"
        and allowed["tool_proxy_enforced"] is True
        and allowed["tool_proxy_identity_match"] is True
        and allowed["governance_callbacks_complete"] is True,
        "fresh exact scoped enforcement satisfies the local OpenClaw preflight",
        allowed,
    )

    print("openclaw capability identity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
