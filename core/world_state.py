from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

from core.architecture import STATE_DEFINITIONS
from core.definitions import lifecycle_statuses, operational_modes, risk_catalog
from core.state_compact import compact_action_record
from interface.event_schema import utc_now_iso

STATE_WORLD_USER = "user"
STATE_WORLD_LOCAL = "local"
STATE_WORLD_EXTERNAL = "external"
STATE_RUNTIME = "runtime"
STATE_CONFIG = "config"
STATE_LOGS = "logs"

JSONL_FILES = [
    "event_log.jsonl",
    "action_record.jsonl",
    "tool_call_log.jsonl",
    "policy_trace.jsonl",
    "execution_trace.jsonl",
    "rollback_log.jsonl",
    "memory_log.jsonl",
    "core_model_trace.jsonl",
    "alert_log.jsonl",
    "runtime_trace.jsonl",
    "decision_trace.jsonl",
    "context_drift_log.jsonl",
]

STATE_FILE_LAYOUT: dict[str, str] = {
    "user_world.json": f"{STATE_WORLD_USER}/user_world.json",
    "user_goals.json": f"{STATE_WORLD_USER}/user_goals.json",
    "user_commitments.json": f"{STATE_WORLD_USER}/user_commitments.json",
    "proactive_intents.json": f"{STATE_WORLD_USER}/proactive_intents.json",
    "proactive_authorizations.json": f"{STATE_WORLD_USER}/proactive_authorizations.json",
    "agent_memory.json": f"{STATE_WORLD_USER}/agent_memory.json",
    "review_queue.json": f"{STATE_WORLD_USER}/review_queue.json",
    "local_world.json": f"{STATE_WORLD_LOCAL}/local_world.json",
    "executor_state.json": f"{STATE_WORLD_LOCAL}/executor_state.json",
    "belief_state.json": f"{STATE_WORLD_LOCAL}/belief_state.json",
    "feishu_ws_state.json": f"{STATE_WORLD_LOCAL}/feishu_ws_state.json",
    "openclaw_device.json": f"{STATE_WORLD_LOCAL}/openclaw_device.json",
    "external_world.json": f"{STATE_WORLD_EXTERNAL}/external_world.json",
    "ops_runtime_matrix.json": f"{STATE_WORLD_EXTERNAL}/ops_runtime_matrix.json",
    "task_state.json": f"{STATE_RUNTIME}/task_state.json",
    "attention_state.json": f"{STATE_RUNTIME}/attention_state.json",
    "persona_state.json": f"{STATE_RUNTIME}/persona_state.json",
    "channel_state.json": f"{STATE_RUNTIME}/channel_state.json",
    "risk_state.json": f"{STATE_RUNTIME}/risk_state.json",
    "active_loop_state.json": f"{STATE_RUNTIME}/active_loop_state.json",
    "runtime_cron_state.json": f"{STATE_RUNTIME}/runtime_cron_state.json",
    "rollback_state.json": f"{STATE_RUNTIME}/rollback_state.json",
    "replay_runtime_state.json": f"{STATE_RUNTIME}/replay_runtime_state.json",
    "ops_soak_state.json": f"{STATE_RUNTIME}/ops_soak_state.json",
    "agent_config.json": f"{STATE_CONFIG}/agent_config.json",
    "ops_config.json": f"{STATE_CONFIG}/ops_config.json",
    "state_schema.json": f"{STATE_CONFIG}/state_schema.json",
    **{name: f"{STATE_LOGS}/{name}" for name in JSONL_FILES},
    "heartbeat.md": "heartbeat.md",
}

STATE_METADATA: dict[str, dict[str, Any]] = {
    "user_world.json": {"source": "veyra_core", "ttl_seconds": 86400, "confidence": 0.72},
    "user_goals.json": {"source": "goal_core", "ttl_seconds": 86400, "confidence": 0.82},
    "user_commitments.json": {"source": "commitment_core", "ttl_seconds": 86400, "confidence": 0.85},
    "proactive_intents.json": {"source": "proactive_intent_framework", "ttl_seconds": 86400, "confidence": 0.78},
    "proactive_authorizations.json": {"source": "proactive_authorization_policy", "ttl_seconds": 86400, "confidence": 0.84},
    "local_world.json": {"source": "local_probe_cache", "ttl_seconds": 300, "confidence": 0.82},
    "external_world.json": {"source": "external_watchlist", "ttl_seconds": 1800, "confidence": 0.62},
    "executor_state.json": {"source": "agent_registry", "ttl_seconds": 300, "confidence": 0.75},
    "risk_state.json": {"source": "guardian", "ttl_seconds": 600, "confidence": 0.82},
    "belief_state.json": {"source": "belief_core", "ttl_seconds": 300, "confidence": 0.72},
    "task_state.json": {"source": "awareness_loop", "ttl_seconds": 1800, "confidence": 0.7},
    "attention_state.json": {"source": "attention_core", "ttl_seconds": 300, "confidence": 0.76},
    "persona_state.json": {"source": "persona_engine", "ttl_seconds": 1800, "confidence": 0.72},
    "channel_state.json": {"source": "intake_gateway", "ttl_seconds": 3600, "confidence": 0.72},
    "state_schema.json": {"source": "architecture", "ttl_seconds": 604800, "confidence": 0.9},
    "review_queue.json": {"source": "guardian_review_queue", "ttl_seconds": 3600, "confidence": 0.8},
    "agent_memory.json": {"source": "memory_bridge", "ttl_seconds": 1800, "confidence": 0.68},
    "agent_config.json": {"source": "agent_config", "ttl_seconds": 86400, "confidence": 0.72},
    "rollback_state.json": {"source": "rollback_audit", "ttl_seconds": 86400, "confidence": 0.82},
    "replay_runtime_state.json": {"source": "replay_runtime", "ttl_seconds": 3600, "confidence": 0.72},
    "ops_soak_state.json": {"source": "ops_soak_runner", "ttl_seconds": 3600, "confidence": 0.7},
    "active_loop_state.json": {"source": "active_runtime_loop", "ttl_seconds": 600, "confidence": 0.78},
    "runtime_cron_state.json": {"source": "runtime_cron", "ttl_seconds": 3600, "confidence": 0.78},
    "feishu_ws_state.json": {"source": "feishu_ws_runner", "ttl_seconds": 600, "confidence": 0.65},
    "ops_runtime_matrix.json": {"source": "runtime_matrix", "ttl_seconds": 1800, "confidence": 0.74},
    "ops_config.json": {"source": "ops_config", "ttl_seconds": 86400, "confidence": 0.8},
}


class WorldStateStore:
    def __init__(self, root: str | Path = "state") -> None:
        selected_root = self._selected_root(root)
        self.root = Path(selected_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_layout()
        self._ensure_defaults()

    def _selected_root(self, root: str | Path) -> str | Path:
        if str(root) != "state":
            return root
        explicit = os.getenv("VEYRA_STATE_DIR") or os.getenv("VEYRA_STATE_ROOT")
        if explicit:
            return explicit
        env = os.getenv("VEYRA_ENV", "").strip().lower()
        if env in {"dev", "prod", "test"}:
            return Path("state") / env
        return "state"

    def path_for(self, name: str) -> Path:
        relative = STATE_FILE_LAYOUT.get(name, name)
        return self.root / relative

    def relative_path_for(self, name: str) -> str:
        return str(self.path_for(name).relative_to(self.root))

    def _migrate_legacy_layout(self) -> None:
        for logical_name, relative in STATE_FILE_LAYOUT.items():
            self._migrate_legacy_file(logical_name, self.root / relative, self.root / logical_name)
        for logical_name in JSONL_FILES:
            self._migrate_legacy_file(logical_name, self.path_for(logical_name), self.root / logical_name)

    def _migrate_legacy_file(self, logical_name: str, target: Path, legacy: Path) -> None:
        if not legacy.is_file():
            return
        if target.exists():
            if legacy.stat().st_mtime > target.stat().st_mtime:
                target.parent.mkdir(parents=True, exist_ok=True)
                legacy.replace(target)
            else:
                legacy.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)

    def _ensure_defaults(self) -> None:
        defaults: dict[str, Any] = {
            "user_world.json": {"preferences": {"language": "zh-CN", "style": "direct_structured"}, "current_goal": ""},
            "user_goals.json": {"goals": [], "updated_at": None},
            "user_commitments.json": {"commitments": [], "updated_at": None},
            "proactive_intents.json": {"intents": [], "updated_at": None},
            "proactive_authorizations.json": {"authorizations": [], "updated_at": None},
            "local_world.json": {"current_project": str(Path.cwd()), "probes": {}, "last_probe_at": None, "updated_at": utc_now_iso()},
            "external_world.json": {"watchlist": [], "summaries": [], "knowledge_items": [], "push_candidates": []},
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
            "persona_state.json": {"active_modes": ["Minimalist"], "last_binding": None, "history": [], "updated_at": None},
            "channel_state.json": {
                "channels": {
                    "api": {"enabled": True, "delivery": "local_outbox"},
                    "cli": {"enabled": True, "delivery": "local_outbox"},
                    "console": {"enabled": True, "delivery": "local_outbox"},
                    "webhook": {
                        "enabled": True,
                        "delivery": "local_outbox",
                        "webhook_url": "",
                        "webhook_url_env": "VEYRA_CHANNEL_WEBHOOK_URL",
                        "timeout": 15,
                        "trust_env": False,
                    },
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
                        "connection_mode": "callback",
                        "verification_token": "",
                        "verification_token_env": "FEISHU_VERIFICATION_TOKEN",
                        "encrypt_key": "",
                        "encrypt_key_env": "FEISHU_ENCRYPT_KEY",
                        "reply_to_session": True,
                        "agent_follow_up_enabled": True,
                        "agent_follow_up_timeout_seconds": 180,
                        "agent_follow_up_interval_seconds": 2,
                        "agent_follow_up_first_progress_seconds": 30,
                        "agent_follow_up_progress_interval_seconds": 90,
                        "agent_missing_snapshot_fail_count": 8,
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
                "routing": {
                    "dialogue_route": "hybrid",
                    "agent_first_dialogue": False,
                },
                "core_model": {
                    "enabled": False,
                    "provider": "openai_compatible",
                    "base_url": "",
                    "api_key_env": "VEYRA_CORE_MODEL_API_KEY",
                    "model": "",
                    "timeout": 20,
                    "decision_mode": "auto",
                    "max_tokens": 700,
                },
                "agents": {
                    "openclaw": {"kind": "openclaw", "base_url": "", "api_key_env": "OPENCLAW_GATEWAY_TOKEN", "enabled": True},
                    "hermes": {"kind": "hermes", "base_url": "", "api_key_env": "HERMES_API_KEY", "enabled": True},
                    "custom": {"kind": "custom", "base_url": "", "api_key_env": "CUSTOM_AGENT_API_KEY", "enabled": True},
                },
            },
            "rollback_state.json": {"snapshots": []},
            "replay_runtime_state.json": {
                "status": "idle",
                "jobs": [],
                "last_scan_at": None,
                "config": {"auto_execute_enabled": False, "allow_r4_restore": False},
            },
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
            "feishu_ws_state.json": {"status": "stopped", "last_event_at": None},
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
        defaults = {name: self._with_state_metadata(name, payload) for name, payload in defaults.items()}
        for name, payload in defaults.items():
            path = self.path_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            current = self.read_json(name)
            merged = self._merge_missing(current, payload)
            if merged != current:
                path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in JSONL_FILES:
            path = self.path_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
        heartbeat = self.path_for("heartbeat.md")
        if not heartbeat.exists():
            heartbeat.write_text(f"# Veyra Heartbeat\n\nstatus: online\nupdated_at: {utc_now_iso()}\n", encoding="utf-8")

    def read_json(self, name: str) -> dict[str, Any]:
        path = self.path_for(name)
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "_state_corrupt": True,
                "_parse_error": str(exc),
                "_file": self.relative_path_for(name),
            }

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _merge_missing(self, current: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in defaults.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_missing(merged[key], value)
        return merged

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        payload = self._with_state_metadata(name, payload)
        payload.setdefault("updated_at", utc_now_iso())
        self._write_json_atomic(self.path_for(name), payload)

    def patch_json(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.read_json(name)
        current.update(patch)
        self.write_json(name, current)
        return current

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        payload.setdefault("timestamp", utc_now_iso())
        if name == "action_record.jsonl":
            payload = compact_action_record(payload)
        path = self.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_jsonl(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        path = self.path_for(name)
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
        path = self.path_for(name)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_text(self, name: str, content: str) -> None:
        path = self.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read_all(self) -> dict[str, Any]:
        return {
            "user_world": self.read_json("user_world.json"),
            "user_goals": self.read_json("user_goals.json"),
            "proactive_intents": self.read_json("proactive_intents.json"),
            "proactive_authorizations": self.read_json("proactive_authorizations.json"),
            "local_world": self.read_json("local_world.json"),
            "external_world": self.read_json("external_world.json"),
            "executor_state": self.read_json("executor_state.json"),
            "risk_state": self.read_json("risk_state.json"),
            "belief_state": self.read_json("belief_state.json"),
            "task_state": self.read_json("task_state.json"),
            "attention_state": self.read_json("attention_state.json"),
            "persona_state": self.read_json("persona_state.json"),
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
            "feishu_ws_state": self.read_json("feishu_ws_state.json"),
            "ops_config": self.read_json("ops_config.json"),
        }

    def state_health(self) -> dict[str, Any]:
        items = [self._state_file_health(name) for name in STATE_METADATA]
        heartbeat = self._heartbeat_health()
        items.append(heartbeat)
        counts: dict[str, int] = {}
        for item in items:
            health = str(item.get("health_status") or "unknown")
            counts[health] = counts.get(health, 0) + 1
        return {
            "status": "success",
            "summary": counts,
            "items": items,
            "stale": [item for item in items if item.get("health_status") in {"stale", "expired", "missing", "invalid"}],
        }

    def _with_state_metadata(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in STATE_METADATA:
            return payload
        metadata = STATE_METADATA[name]
        enriched = dict(payload)
        enriched.setdefault("source", metadata["source"])
        enriched.setdefault("confidence", metadata["confidence"])
        enriched.setdefault("ttl_seconds", metadata["ttl_seconds"])
        enriched.setdefault("status", "fresh")
        enriched.setdefault("updated_at", utc_now_iso())
        return enriched

    def _state_file_health(self, name: str) -> dict[str, Any]:
        path = self.path_for(name)
        metadata = STATE_METADATA[name]
        if not path.exists():
            return {
                "name": name,
                "path": self.relative_path_for(name),
                "source": metadata["source"],
                "status": "missing",
                "health_status": "missing",
                "confidence": 0.0,
                "ttl_seconds": metadata["ttl_seconds"],
                "age_seconds": None,
                "next_action": "recreate_default_state",
            }
        try:
            payload = self.read_json(name)
        except json.JSONDecodeError:
            return {
                "name": name,
                "path": self.relative_path_for(name),
                "source": metadata["source"],
                "status": "invalid",
                "health_status": "invalid",
                "confidence": 0.0,
                "ttl_seconds": metadata["ttl_seconds"],
                "age_seconds": None,
                "next_action": "repair_state_json",
            }
        updated_at = str(payload.get("updated_at") or "")
        age_seconds = self._age_seconds(updated_at)
        ttl_seconds = int(payload.get("ttl_seconds") or metadata["ttl_seconds"])
        confidence = float(payload.get("confidence") or metadata["confidence"])
        if age_seconds is None:
            health_status = "uncertain"
        elif ttl_seconds > 0 and age_seconds > ttl_seconds * 2:
            health_status = "expired"
        elif ttl_seconds > 0 and age_seconds > ttl_seconds:
            health_status = "stale"
        else:
            health_status = "fresh"
        return {
            "name": name,
            "path": self.relative_path_for(name),
            "source": payload.get("source") or metadata["source"],
            "status": payload.get("status") or "unknown",
            "health_status": health_status,
            "confidence": confidence,
            "ttl_seconds": ttl_seconds,
            "updated_at": updated_at,
            "age_seconds": age_seconds,
            "next_action": "refresh_probe" if health_status in {"stale", "expired", "uncertain"} else None,
        }

    def _heartbeat_health(self) -> dict[str, Any]:
        text = self.read_text("heartbeat.md")
        updated_at = ""
        for line in text.splitlines():
            if line.startswith("updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
                break
        age_seconds = self._age_seconds(updated_at)
        ttl_seconds = 300
        health_status = "uncertain" if age_seconds is None else "expired" if age_seconds > ttl_seconds * 2 else "stale" if age_seconds > ttl_seconds else "fresh"
        return {
            "name": "heartbeat.md",
            "source": "runtime_entity",
            "status": "online" if "status: online" in text else "unknown",
            "health_status": health_status,
            "confidence": 0.82 if text else 0.0,
            "ttl_seconds": ttl_seconds,
            "updated_at": updated_at,
            "age_seconds": age_seconds,
            "next_action": "heartbeat_tick" if health_status in {"stale", "expired", "uncertain"} else None,
        }

    def _age_seconds(self, value: str) -> int | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
