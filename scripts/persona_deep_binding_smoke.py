#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_scope import ContextScopeFilter  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.persona_engine import PersonaEngine  # noqa: E402
from core.task_packet_builder import TaskPacketBuilder  # noqa: E402
from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    with TemporaryDirectory(prefix="veyra-persona-binding-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id="u", session_id="s"),
            payload={"text": "帮我修复代码并说明风险"},
        )
        persona = PersonaEngine(store).patch_for(
            "帮我修复代码并说明风险",
            RiskLevel.R2,
            channel="api",
            route="agent",
            target_agent="openclaw",
            decision={"route": "agent", "risk_level": "R2"},
        )

        expect("Engineer" in persona.get("mode", []), "engineer mode selected", persona)
        expect(bool(persona.get("guidelines")), "persona guidelines loaded from files", persona)
        expect(int(persona.get("context_budget_chars") or 0) >= 6000, "persona context budget computed", persona)

        turn_context = TurnContextBuilder(store).build(
            user_message="帮我修复代码并说明风险",
            attention_focus=["codebase"],
            event=event,
            persona_hint=persona,
        )
        active_persona = (turn_context.get("active_context") or {}).get("persona") or {}
        expect(active_persona.get("guidelines"), "turn context carries persona guidelines", active_persona)
        expect(active_persona.get("context_budget_chars") == persona.get("context_budget_chars"), "turn context carries persona budget", active_persona)

        scoped = ContextScopeFilter().apply(
            {"user_goal": "x", "memory_summary": {"summary": "m" * 5000}, "decision_trace": {"route": "agent"}},
            user_goal="帮我修复代码",
            token_budget_chars=int(persona["context_budget_chars"]),
        )
        expect((scoped.get("scope") or {}).get("token_budget_chars") == persona.get("context_budget_chars"), "context scope uses persona budget", scoped)

        packet = TaskPacketBuilder(store).build(
            event=event,
            target_agent="openclaw",
            context_patch=scoped["context_patch"],
            persona_patch=persona,
            policy_patch={"risk_level": "R2", "requires_snapshot": True},
            required_capabilities=["selected_agent_runtime"],
        )
        expect(packet.persona_patch.get("guidelines"), "agent task packet carries persona guidelines", packet.to_dict())
        expect(packet.persona_patch.get("tool_policy"), "agent task packet carries persona tool policy", packet.to_dict())

    print("persona_deep_binding_smoke: ok")


if __name__ == "__main__":
    main()
