#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()
        legacy_payload = {"current_goal": "migrate-me", "preferences": {"language": "zh-CN"}}
        (root / "user_world.json").write_text(json.dumps(legacy_payload), encoding="utf-8")
        (root / "event_log.jsonl").write_text('{"event":"legacy"}\n', encoding="utf-8")

        store = WorldStateStore(root)
        expect((root / "user" / "user_world.json").exists(), "legacy user_world migrated")
        expect(not (root / "user_world.json").exists(), "legacy user_world removed from root")
        expect(store.read_json("user_world.json").get("current_goal") == "migrate-me", "migrated user_world readable")
        expect((root / "logs" / "event_log.jsonl").exists(), "legacy jsonl migrated")
        expect(store.read_jsonl("event_log.jsonl", limit=1)[0].get("event") == "legacy", "migrated jsonl readable")

        store.write_json("local_world.json", {"current_project": "/tmp/demo", "probes": {}})
        expect((root / "local" / "local_world.json").exists(), "new writes use layout paths")
        expect(store.relative_path_for("task_state.json") == "runtime/task_state.json", "runtime path mapping")

    print("state_layout_smoke passed")


if __name__ == "__main__":
    main()
