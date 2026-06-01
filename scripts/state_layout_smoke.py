#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from routers.debug_audit import CoreModelConfigRequest, build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


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
        expect(store.relative_path_for("user_goals.json") == "user/user_goals.json", "user goals path mapping")
        expect(store.relative_path_for("task_state.json") == "runtime/task_state.json", "runtime path mapping")

        saved_env = {key: os.environ.get(key) for key in ("VEYRA_STATE_DIR", "VEYRA_STATE_ROOT", "VEYRA_AGENCY_DIR", "VEYRA_AGENCY_ROOT", "VEYRA_ENV")}
        cwd = Path.cwd()
        try:
            os.environ["VEYRA_STATE_DIR"] = str(Path(tmp) / "state-custom")
            os.environ.pop("VEYRA_STATE_ROOT", None)
            env_store = WorldStateStore()
            expect(env_store.root == Path(tmp) / "state-custom", "VEYRA_STATE_DIR selects isolated state root", env_store.root)

            os.environ.pop("VEYRA_STATE_DIR", None)
            os.environ["VEYRA_ENV"] = "test"
            os.chdir(tmp)
            test_store = WorldStateStore()
            expect(test_store.root == Path("state") / "test", "VEYRA_ENV selects state namespace", test_store.root)

            os.environ["VEYRA_AGENCY_DIR"] = str(Path(tmp) / "agency-custom")
            agency = AgencyCore(agency_root="agency", model_assist_enabled=False)
            expect(agency.agency_root == Path(tmp) / "agency-custom", "VEYRA_AGENCY_DIR selects isolated agency root", agency.agency_root)

            os.environ.pop("VEYRA_AGENCY_DIR", None)
            os.environ["VEYRA_ENV"] = "test"
            agency_test = AgencyCore(agency_root="agency", model_assist_enabled=False)
            expect(agency_test.agency_root == Path("agency") / "test", "VEYRA_ENV selects agency namespace", agency_test.agency_root)
        finally:
            os.chdir(cwd)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        loop = ActiveRuntimeLoop.__new__(ActiveRuntimeLoop)
        compacted = loop._compact_result(
            {
                "status": "success",
                "refreshed": [
                    {
                        "claim": {"key": "weather.beijing", "source": "probe"},
                        "probe_result": {"status": "success", "raw": "x" * 5000},
                    }
                ],
            }
        )
        compacted_text = json.dumps(compacted, ensure_ascii=False)
        expect("raw" not in compacted_text and len(compacted_text) < 500, "active loop compact result drops raw probe payloads", compacted)

        class FakeReasoning:
            def status(self) -> dict[str, str]:
                return {"status": "ok"}

        class FakeAwarenessLoop:
            core_reasoning = FakeReasoning()

        router = build_debug_audit_router({"state_store": store, "awareness_loop": FakeAwarenessLoop()})
        endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/core/model/config")
        try:
            asyncio.run(endpoint(CoreModelConfigRequest(api_key="direct-secret")))
            raise AssertionError("direct api_key accepted")
        except HTTPException as exc:
            expect(exc.status_code == 422, "direct core model api_key rejected", exc.status_code)

        config = store.read_json("agent_config.json")
        config.setdefault("core_model", {})["api_key"] = "legacy-secret"
        store.write_json("agent_config.json", config)
        asyncio.run(endpoint(CoreModelConfigRequest(api_key="", api_key_env="VEYRA_CORE_MODEL_API_KEY")))
        updated_config = store.read_json("agent_config.json")
        expect("api_key" not in updated_config.get("core_model", {}), "empty api_key clears legacy direct secret")

        for index in range(4):
            store.append_jsonl("action_record.jsonl", {"route": f"retention_test_{index}", "status": "success"})
        policy = RetentionPolicy(store, limits={"action_record.jsonl": 3})
        before_dry_run = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        policy.enforce(dry_run=True)
        after_dry_run = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        expect(after_dry_run == before_dry_run, "retention dry-run leaves logs unchanged", {"before": before_dry_run, "after": after_dry_run})
        policy.enforce()
        after_enforce = len(store.path_for("action_record.jsonl").read_text(encoding="utf-8").splitlines())
        expect(after_enforce <= 3, "retention enforce keeps audit log within limit", after_enforce)

    print("state_layout_smoke passed")


if __name__ == "__main__":
    main()
