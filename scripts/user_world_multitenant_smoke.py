#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def event(user_id: str, session_id: str, text: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-user-world-multitenant-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        loop = AwarenessLoop(store, RuntimeEntity(store))
        core = CommitmentCore(store)

        loop._sync_user_awareness_from_text(event("user-a", "s-a", "我正在开发 Veyra"))
        loop._sync_user_awareness_from_text(event("user-b", "s-b", "我在做 Agent治理系统"))
        core.create_commitment(
            {
                "kind": "weather_daily",
                "status": "active",
                "title": "A weather",
                "user_id": "user-a",
                "session_id": "s-a",
                "payload": {"topic": "weather", "location": "北京"},
            }
        )
        core.create_commitment(
            {
                "kind": "weather_daily",
                "status": "active",
                "title": "B weather",
                "user_id": "user-b",
                "session_id": "s-b",
                "payload": {"topic": "weather", "location": "上海"},
            }
        )

        user_world = store.read_json("user_world.json")
        profiles = user_world.get("profiles_by_user")
        expect(isinstance(profiles, dict), "profiles_by_user exists", user_world)
        a = profiles.get("user-a", {})
        b = profiles.get("user-b", {})
        expect(a.get("current_project") == "Veyra", "user-a project remains scoped", profiles)
        expect(b.get("current_project") == "Agent治理系统", "user-b project remains scoped", profiles)
        expect((a.get("preferences") or {}).get("default_location") == "北京", "user-a default location scoped", a)
        expect((b.get("preferences") or {}).get("default_location") == "上海", "user-b default location scoped", b)
        expect(user_world.get("current_goal") != b.get("current_goal"), "non-local user does not overwrite top-level current_goal", user_world)

    print("user_world_multitenant_smoke: ok")


if __name__ == "__main__":
    main()
