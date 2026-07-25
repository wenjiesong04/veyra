from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop
from core.runtime_entity import RuntimeEntity
from core.turn_context_builder import TurnContextBuilder
from core.world_state import WorldStateStore
from interface.event_schema import EventSource, EventType, VeyraEvent
from scripts.user_profile_generalization_smoke import authorized_profile_result


def _event(user_id: str, session_id: str, text: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-user-profile-isolation-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        loop = AwarenessLoop(store, RuntimeEntity(store))

        event_a = _event("user-a", "session-a", "我正在开发 Veyra")
        event_b = _event("user-b", "session-b", "我在做 Agent治理系统")
        loop._sync_user_awareness_from_text(event_a, authorized_profile_result(event_a))
        loop._sync_user_awareness_from_text(event_b, authorized_profile_result(event_b))

        user_world = store.read_json("user_world.json")
        profiles = user_world.get("profiles_by_user")
        assert isinstance(profiles, dict), user_world
        assert profiles["user-a"]["current_project"] == "Veyra", profiles
        assert profiles["user-b"]["current_project"] == "Agent治理系统", profiles

        builder = TurnContextBuilder(store)
        context_a = builder.build(user_message="继续", attention_focus=[], event=_event("user-a", "session-a", "继续"))
        context_b = builder.build(user_message="继续", attention_focus=[], event=_event("user-b", "session-b", "继续"))
        user_a = context_a["short_memory"]["user"]
        user_b = context_b["short_memory"]["user"]
        assert user_a["current_project"] == "Veyra", user_a
        assert user_b["current_project"] == "Agent治理系统", user_b
        assert "Veyra" in loop._project_context_response(_event("user-a", "session-a", "继续"))
        assert "Agent治理系统" in loop._project_context_response(_event("user-b", "session-b", "继续"))

    print("user_profile_isolation_smoke: ok")


if __name__ == "__main__":
    main()
