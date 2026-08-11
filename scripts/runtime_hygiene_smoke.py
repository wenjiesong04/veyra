#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def test_action_record_auto_retention() -> None:
    from core.world_state import WorldStateStore  # noqa: E402
    from runtime.retention_policy import RetentionPolicy  # noqa: E402

    previous_limit = os.environ.get("VEYRA_ACTION_RECORD_RETENTION_LIMIT")
    try:
        os.environ["VEYRA_ACTION_RECORD_RETENTION_LIMIT"] = "5"
        with TemporaryDirectory(prefix="veyra-action-record-auto-retention-") as tmp:
            store = WorldStateStore(Path(tmp) / "state")
            for index in range(7):
                store.append_jsonl("action_record.jsonl", {"route": "hygiene_auto_retention_seed", "status": "success", "artifacts": {"index": index}})
            action_path = store.path_for("action_record.jsonl")
            action_text = action_path.read_text(encoding="utf-8")
            archives = list((store.root / "archive" / "retention").glob("action_record-*.jsonl.gz"))
            expect(line_count(action_path) <= 5, "action_record auto-retention keeps append writes within limit", line_count(action_path))
            expect(bool(archives), "action_record auto-retention archives old rows", archives)
            expect("ops_retention_auto_enforce" in action_text, "action_record auto-retention writes audit", action_text)
            expect(len(archives) == 1, "action_record auto-retention rotates a batch, not one file per row", archives)
        with TemporaryDirectory(prefix="veyra-disabled-retention-") as tmp:
            store = WorldStateStore(Path(tmp) / "state")
            store.append_jsonl("event_log.jsonl", {"route": "retention_disabled_seed", "status": "success"})
            policy = RetentionPolicy(store)
            disabled = policy.enforce(limits={"event_log.jsonl": 0})
            event_row = next(item for item in disabled["files"] if item["file"] == "event_log.jsonl")
            expect(event_row["status"] == "disabled", "zero retention limit has disabled semantics", event_row)
            expect(line_count(store.path_for("event_log.jsonl")) == 1, "disabled retention preserves log rows")
    finally:
        if previous_limit is None:
            os.environ.pop("VEYRA_ACTION_RECORD_RETENTION_LIMIT", None)
        else:
            os.environ["VEYRA_ACTION_RECORD_RETENTION_LIMIT"] = previous_limit


def main() -> int:
    test_action_record_auto_retention()
    with TemporaryDirectory(prefix="veyra-runtime-hygiene-") as tmp:
        state_root = Path(tmp) / "state"
        agency_root = Path(tmp) / "agency"
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_STATE_ROOT"] = str(state_root)
        os.environ["VEYRA_AGENCY_DIR"] = str(agency_root)
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
        os.environ["VEYRA_ACTIVE_LOOP_AUTOSTART"] = "0"
        os.environ["VEYRA_FEISHU_WS_AUTOSTART"] = "0"
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text("{}", encoding="utf-8")

        import main as app_module  # noqa: E402

        client = TestClient(app_module.app)
        store = app_module.state_store
        store.patch_json("ops_config.json", {"active_loop": {"autostart": False, "interval_seconds": 300.0, "health_required": False}})

        for index in range(8):
            store.append_jsonl("action_record.jsonl", {"route": "hygiene_retention_seed", "status": "success", "artifacts": {"index": index}})
        retention = client.post("/ops/retention/enforce", json={"limit_overrides": {"action_record.jsonl": 5}}).json()
        action_path = store.path_for("action_record.jsonl")
        expect(retention.get("changed", 0) >= 1, "retention enforcement changes over-limit log", retention)
        expect(line_count(action_path) <= 5, "action_record retained within override limit", line_count(action_path))
        archive_rows = [item for item in retention.get("files", []) if item.get("file") == "action_record.jsonl" and item.get("archive_path")]
        expect(bool(archive_rows), "retention archive path recorded", retention)
        expect((store.root / archive_rows[0]["archive_path"]).exists(), "retention archive file exists", archive_rows[0])
        expect(
            any(item.get("route") == "ops_retention_enforce" for item in store.read_jsonl("policy_trace.jsonl", limit=20)),
            "retention audit is written to policy trace",
            store.read_jsonl("policy_trace.jsonl", limit=20),
        )

        first = app_module.review_queue.create("evt_hygiene_1", "帮我重启 OpenClaw 服务", "R4", {"risk_level": "R4"}, {"decision": "ask_user", "risk_level": "R4"})
        second = app_module.review_queue.create("evt_hygiene_2", "历史测试 review", "R1", {"risk_level": "R1"}, {"decision": "ask_user", "risk_level": "R1"})
        state = store.read_json("review_queue.json")
        for item in state.get("items", []):
            if item.get("review_id") in {first["review_id"], second["review_id"]}:
                item["created_at"] = "2026-05-25T06:00:00+00:00"
        store.write_json("review_queue.json", state)

        diagnostic = client.get("/ops/reviews/diagnostic", params={"stale_after_days": 1}).json()
        expect(diagnostic.get("pending_count") == 2, "review diagnostic counts pending reviews", diagnostic)
        pending = diagnostic.get("pending") if isinstance(diagnostic.get("pending"), list) else []
        expect(all(item.get("type") and item.get("source") and item.get("created_at") and item.get("risk") for item in pending), "review diagnostic exposes type/source/created_at/risk", pending)

        resolved = client.post(f"/ops/reviews/{first['review_id']}/resolve", json={"reason": "runtime hygiene smoke resolved historical review"}).json()
        archived = client.post(f"/ops/reviews/{second['review_id']}/archive", json={"reason": "runtime hygiene smoke archived historical test review"}).json()
        expect(resolved.get("status") == "resolved", "pending review can be marked resolved", resolved)
        expect(archived.get("status") == "archived" and archived.get("archive_path"), "pending review can be archived", archived)
        expect((store.root / archived["archive_path"]).exists(), "review archive file exists", archived)
        expect(client.get("/ops/reviews/diagnostic").json().get("pending_count") == 0, "resolved/archived reviews no longer pending", store.read_json("review_queue.json"))

        app_module.runtime_entity.set_status("online")
        app_module.ops_monitor.agent_status_resolver = lambda: store.read_json(
            "executor_state.json"
        )
        app_module.ops_monitor.model_status_resolver = lambda: {"enabled": True, "configured": True, "status": "configured", "api_key_set": True}
        app_module.ops_monitor.feishu_status_resolver = lambda: {"status": "stopped", "thread_alive": False, "channel_config": {"enabled": False}}
        app_module.ops_monitor.active_loop_status_resolver = lambda: {"status": "stopped", "thread_alive": False, "enabled": False}

        def state_digest() -> tuple[tuple[str, int, str], ...]:
            import hashlib

            rows = []
            for path in sorted(store.root.rglob("*")):
                if path.is_file():
                    rows.append(
                        (
                            path.relative_to(store.root).as_posix(),
                            path.stat().st_size,
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    )
            return tuple(rows)

        cached_agent = {
            "name": "openclaw",
            "status": "available",
            "connected": True,
            "capabilities": {
                "features": {"memory_summary": False, "memory_patch": False}
            },
        }
        store.patch_json("executor_state.json", cached_agent)
        before_health = state_digest()
        with patch(
            "runtime.isolated_git_snapshot.subprocess.run",
            side_effect=AssertionError("status GET attempted Git"),
        ), patch.object(
            app_module.awareness_loop.agent_registry.selected(),
            "connection_status",
            side_effect=AssertionError("health GET attempted OpenClaw probe"),
        ):
            health = client.get("/health").json()
            runtime = client.get("/runtime").json()
        expect(
            state_digest() == before_health,
            "health GET does not write cached state",
        )
        expect(
            health.get("runtime_build") == runtime.get("runtime_build")
            and health.get("runtime_build", {}).get("git_checked_on_request")
            is False,
            "health and runtime reuse one frozen build identity without Git",
            {
                "health": health.get("runtime_build"),
                "runtime": runtime.get("runtime_build"),
            },
        )
        expect(health.get("status") == "healthy", "info-only health remains healthy", health)
        info_codes = {item.get("code") for item in health.get("alerts", []) if item.get("severity") == "info"}
        expect("openclaw_workspace_memory_fallback" in info_codes, "workspace memory fallback is info-level", health)

        app_module.ops_monitor.agent_status_resolver = lambda: {
            **cached_agent,
            "updated_at": "2020-01-01T00:00:00+00:00",
            "ttl_seconds": 300,
        }
        stale_health = client.get("/health").json()
        stale_codes = {
            item.get("code")
            for item in stale_health.get("alerts", [])
            if item.get("severity") == "warning"
        }
        expect(
            stale_health.get("status") == "degraded"
            and "agent_runtime_snapshot_stale" in stale_codes,
            "health degrades an expired Agent snapshot instead of reporting healthy",
            stale_health,
        )

    print("runtime hygiene smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
