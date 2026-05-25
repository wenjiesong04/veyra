from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.architecture import STATE_DEFINITIONS
from core.definitions import lifecycle_statuses, operational_modes, risk_catalog
from interface.event_schema import utc_now_iso


class WorldStateStore:
    def __init__(self, root: str | Path = "state") -> None:
        selected_root = os.getenv("VEYRA_STATE_ROOT", "state") if str(root) == "state" else root
        self.root = Path(selected_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        defaults: dict[str, Any] = {
            "user_world.json": {"preferences": {"language": "zh-CN", "style": "direct_structured"}, "current_goal": ""},
            "local_world.json": {"current_project": str(Path.cwd()), "probes": {}, "last_probe_at": None, "updated_at": utc_now_iso()},
            "external_world.json": {"watchlist": [], "summaries": []},
            "executor_state.json": {"selected_agent": "openclaw", "status": "unknown"},
            "risk_state.json": {"current_risk": "R0", "signals": [], "levels": risk_catalog()},
            "belief_state.json": {
                "claims": [],
                "summary": {
                    "fresh": 0,
                    "stale": 0,
                    "expired": 0,
                    "conflict": 0,
                    "total": 0,
                    "refreshable": 0,
                    "by_source": {},
                    "average_confidence": 0.0,
                    "average_source_trust": 0.0,
                },
            },
            "task_state.json": {"current_task": None, "history": []},
            "attention_state.json": {"focus": [], "ignored_noise": []},
            "channel_state.json": {
                "channels": {
                    "api": {"enabled": True, "delivery": "local_outbox"},
                    "cli": {"enabled": True, "delivery": "local_outbox"},
                    "console": {"enabled": True, "delivery": "local_outbox"},
                    "webhook": {"enabled": True, "delivery": "local_outbox"},
                    "feishu": {
                        "enabled": False,
                        "delivery": "feishu",
                        "base_url": "https://open.feishu.cn",
                        "app_id": "",
                        "app_id_env": "FEISHU_APP_ID",
                        "app_secret": "",
                        "app_secret_env": "FEISHU_APP_SECRET",
                        "tenant_access_token": "",
                        "tenant_access_token_env": "FEISHU_TENANT_ACCESS_TOKEN",
                        "default_receive_id": "",
                        "default_receive_id_env": "FEISHU_DEFAULT_RECEIVE_ID",
                        "default_receive_id_type": "chat_id",
                        "verification_token": "",
                        "verification_token_env": "FEISHU_VERIFICATION_TOKEN",
                        "reply_to_session": True,
                    },
                },
                "sessions": {},
                "seen_message_ids": {},
                "inbox": [],
                "outbox": [],
            },
            "state_schema.json": {
                "version": 1,
                "state_definitions": STATE_DEFINITIONS,
                "lifecycle_statuses": lifecycle_statuses(),
                "operational_modes": operational_modes(),
            },
            "review_queue.json": {"items": []},
            "agent_memory.json": {"items": []},
            "agent_config.json": {
                "selected_agent": "openclaw",
                "core_model": {
                    "enabled": False,
                    "provider": "openai_compatible",
                    "base_url": "",
                    "api_key_env": "VEYRA_CORE_MODEL_API_KEY",
                    "model": "",
                    "timeout": 20,
                    "decision_mode": "auto",
                },
                "agents": {
                    "openclaw": {"kind": "openclaw", "base_url": "", "api_key_env": "OPENCLAW_GATEWAY_TOKEN", "enabled": True},
                    "hermes": {"kind": "hermes", "base_url": "", "api_key_env": "HERMES_API_KEY", "enabled": True},
                    "custom": {"kind": "custom", "base_url": "", "api_key_env": "CUSTOM_AGENT_API_KEY", "enabled": True},
                },
            },
            "rollback_state.json": {"snapshots": []},
            "replay_runtime_state.json": {"status": "idle", "jobs": [], "last_scan_at": None},
            "ops_soak_state.json": {"status": "idle", "runs": []},
            "active_loop_state.json": {
                "status": "stopped",
                "enabled": False,
                "interval_seconds": 300,
                "ticks": [],
            },
            "runtime_cron_state.json": {
                "status": "configured",
                "jobs": {
                    "active_awareness_tick": {
                        "job_id": "active_awareness_tick",
                        "enabled": True,
                        "interval_seconds": 300.0,
                        "include_runtime_matrix": False,
                        "last_run_at": None,
                        "next_run_at": None,
                        "run_count": 0,
                    }
                },
            },
            "ops_runtime_matrix.json": {"status": "not_run", "runtimes": []},
            "ops_config.json": {
                "alerting": {
                    "enabled": True,
                    "local_log": True,
                    "webhook_enabled": False,
                    "webhook_url": "",
                    "webhook_url_env": "VEYRA_ALERT_WEBHOOK_URL",
                    "min_severity": "warning",
                },
                "tool_proxy": {
                    "browser_executor_enabled": False,
                    "api_executor_enabled": False,
                    "browser_allowed_hosts": ["localhost", "127.0.0.1", "::1"],
                    "api_allowed_hosts": ["localhost", "127.0.0.1", "::1"],
                },
            },
        }
        for name, payload in defaults.items():
            path = self.root / name
            if not path.exists():
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            current = self.read_json(name)
            merged = self._merge_missing(current, payload)
            if merged != current:
                path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in [
            "event_log.jsonl",
            "action_record.jsonl",
            "tool_call_log.jsonl",
            "policy_trace.jsonl",
            "execution_trace.jsonl",
            "rollback_log.jsonl",
            "memory_log.jsonl",
            "core_model_trace.jsonl",
            "alert_log.jsonl",
        ]:
            path = self.root / name
            if not path.exists():
                path.write_text("", encoding="utf-8")
        heartbeat = self.root / "heartbeat.md"
        if not heartbeat.exists():
            heartbeat.write_text(f"# Veyra Heartbeat\n\nstatus: online\nupdated_at: {utc_now_iso()}\n", encoding="utf-8")

    def read_json(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8") or "{}")

    def _merge_missing(self, current: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in defaults.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_missing(merged[key], value)
        return merged

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        payload.setdefault("updated_at", utc_now_iso())
        (self.root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def patch_json(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.read_json(name)
        current.update(patch)
        self.write_json(name, current)
        return current

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        payload.setdefault("timestamp", utc_now_iso())
        with (self.root / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_jsonl(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        path = self.root / name
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"raw": line, "parse_error": True})
        return rows[-limit:]

    def read_text(self, name: str) -> str:
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_text(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")

    def read_all(self) -> dict[str, Any]:
        return {
            "user_world": self.read_json("user_world.json"),
            "local_world": self.read_json("local_world.json"),
            "external_world": self.read_json("external_world.json"),
            "executor_state": self.read_json("executor_state.json"),
            "risk_state": self.read_json("risk_state.json"),
            "belief_state": self.read_json("belief_state.json"),
            "task_state": self.read_json("task_state.json"),
            "attention_state": self.read_json("attention_state.json"),
            "channel_state": self.read_json("channel_state.json"),
            "state_schema": self.read_json("state_schema.json"),
            "review_queue": self.read_json("review_queue.json"),
            "rollback_state": self.read_json("rollback_state.json"),
            "replay_runtime_state": self.read_json("replay_runtime_state.json"),
            "agent_memory": self.read_json("agent_memory.json"),
            "agent_config": self.read_json("agent_config.json"),
            "ops_soak_state": self.read_json("ops_soak_state.json"),
            "ops_runtime_matrix": self.read_json("ops_runtime_matrix.json"),
            "active_loop_state": self.read_json("active_loop_state.json"),
            "runtime_cron_state": self.read_json("runtime_cron_state.json"),
            "ops_config": self.read_json("ops_config.json"),
        }
