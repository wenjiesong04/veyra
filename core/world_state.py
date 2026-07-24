from __future__ import annotations

import atexit
import copy
import gzip
import json
import hashlib
import math
import os
from pathlib import Path
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from datetime import datetime, timezone
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - Veyra desktop/runtime currently targets POSIX.
    fcntl = None

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
STATE_SCHEMA_VERSION = 2

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
    "openclaw_workspace_memory_fallback.jsonl",
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
    "risk_policy.json": f"{STATE_CONFIG}/risk_policy.json",
    "active_loop_state.json": f"{STATE_RUNTIME}/active_loop_state.json",
    "state_refresh_state.json": f"{STATE_RUNTIME}/state_refresh_state.json",
    "runtime_cron_state.json": f"{STATE_RUNTIME}/runtime_cron_state.json",
    "self_improvement_proposals.json": f"{STATE_RUNTIME}/self_improvement_proposals.json",
    "rollback_state.json": f"{STATE_RUNTIME}/rollback_state.json",
    "replay_runtime_state.json": f"{STATE_RUNTIME}/replay_runtime_state.json",
    "ops_soak_state.json": f"{STATE_RUNTIME}/ops_soak_state.json",
    "agent_config.json": f"{STATE_CONFIG}/agent_config.json",
    "ops_config.json": f"{STATE_CONFIG}/ops_config.json",
    "setup_wizard.json": f"{STATE_CONFIG}/setup_wizard.json",
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
    "state_schema.json": {"source": "architecture", "ttl_seconds": 0, "confidence": 0.9},
    "review_queue.json": {"source": "guardian_review_queue", "ttl_seconds": 3600, "confidence": 0.8},
    "agent_memory.json": {"source": "memory_bridge", "ttl_seconds": 0, "confidence": 0.68},
    "agent_config.json": {"source": "agent_config", "ttl_seconds": 0, "confidence": 0.72},
    "rollback_state.json": {"source": "rollback_audit", "ttl_seconds": 86400, "confidence": 0.82},
    "replay_runtime_state.json": {"source": "replay_runtime", "ttl_seconds": 3600, "confidence": 0.72},
    "ops_soak_state.json": {"source": "ops_soak_runner", "ttl_seconds": 3600, "confidence": 0.7},
    "active_loop_state.json": {"source": "active_runtime_loop", "ttl_seconds": 600, "confidence": 0.78},
    "state_refresh_state.json": {"source": "state_refresh", "ttl_seconds": 0, "confidence": 0.9},
    "runtime_cron_state.json": {"source": "runtime_cron", "ttl_seconds": 3600, "confidence": 0.78},
    "self_improvement_proposals.json": {"source": "self_improvement_registry", "ttl_seconds": 86400, "confidence": 0.72},
    "feishu_ws_state.json": {"source": "feishu_ws_runner", "ttl_seconds": 600, "confidence": 0.65},
    "ops_runtime_matrix.json": {"source": "runtime_matrix", "ttl_seconds": 1800, "confidence": 0.74},
    "ops_config.json": {"source": "ops_config", "ttl_seconds": 0, "confidence": 0.8},
    "setup_wizard.json": {"source": "local_setup", "ttl_seconds": 0, "confidence": 0.9},
    "risk_policy.json": {"source": "guardian_policy", "ttl_seconds": 0, "confidence": 0.9},
}

CONFIG_STATE_FILES = {"agent_config.json", "ops_config.json", "risk_policy.json", "setup_wizard.json", "state_schema.json"}
DURABLE_STATE_FILES = CONFIG_STATE_FILES | {"agent_memory.json"}

_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_WRITER_LEASES: dict[str, dict[str, Any]] = {}


class StateWriterConflictError(RuntimeError):
    """Raised when another process already owns the state-root writer lease."""


class StateRevisionConflictError(RuntimeError):
    """Raised instead of silently overwriting a newer JSON state revision."""


class StateReadOnlyError(RuntimeError):
    """Raised when a read-only state view is asked to mutate local state."""


def _release_writer_leases() -> None:
    if fcntl is None:
        return
    with _ROOT_LOCKS_GUARD:
        leases = list(_WRITER_LEASES.values())
        _WRITER_LEASES.clear()
    for lease in leases:
        handle = lease.get("handle")
        if handle is None:
            continue
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


atexit.register(_release_writer_leases)


class WorldStateStore:
    def __init__(
        self,
        root: str | Path = "state",
        *,
        exclusive_writer: bool = False,
        writer_owner: str = "veyra-state-writer",
        read_only: bool = False,
    ) -> None:
        if exclusive_writer and read_only:
            raise ValueError("exclusive_writer and read_only cannot both be enabled")
        selected_root = self._selected_root(root)
        self.root = Path(selected_root)
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self._root_key = str(self.root.resolve())
        self._mutation_lock = self._root_mutation_lock(self._root_key)
        self._writer_owner = writer_owner
        self._writer_lease_owned = False
        self._read_only = read_only
        self.action_record_auto_retention_limit = self._action_record_retention_limit()
        self._action_record_retention_active = False
        self._action_record_appends_since_check = 0
        self.recovered_temp_files = 0
        if exclusive_writer:
            self.acquire_writer_lease(owner=writer_owner)
        if read_only:
            return
        self._migrate_legacy_layout()
        self._ensure_defaults()
        if exclusive_writer:
            self._migrate_state_boundaries()
            self.recovered_temp_files = self.cleanup_orphan_temp_files()

    @staticmethod
    def _root_mutation_lock(root_key: str) -> threading.RLock:
        with _ROOT_LOCKS_GUARD:
            return _ROOT_MUTATION_LOCKS.setdefault(root_key, threading.RLock())

    def acquire_writer_lease(self, *, owner: str | None = None) -> dict[str, Any]:
        """Claim the one-writer lease for this state root until process exit."""
        owner = str(owner or self._writer_owner or "veyra-state-writer")
        with _ROOT_LOCKS_GUARD:
            existing = _WRITER_LEASES.get(self._root_key)
            if existing and int(existing.get("pid") or -1) == os.getpid():
                self._writer_lease_owned = True
                return dict(existing.get("metadata") or {})

            lock_path = self.root / ".veyra-writer.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+", encoding="utf-8")
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    handle.seek(0)
                    current_owner = handle.read().strip() or "unknown owner"
                    handle.close()
                    raise StateWriterConflictError(
                        f"state root {self.root} already has an active writer: {current_owner}"
                    ) from exc

            metadata = {
                "pid": os.getpid(),
                "owner": owner,
                "state_root": self._root_key,
                "acquired_at": utc_now_iso(),
            }
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(metadata, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
            _WRITER_LEASES[self._root_key] = {
                "pid": os.getpid(),
                "handle": handle,
                "metadata": metadata,
            }
            self._writer_lease_owned = True
            return metadata

    def writer_status(self) -> dict[str, Any]:
        with _ROOT_LOCKS_GUARD:
            lease = _WRITER_LEASES.get(self._root_key)
            if lease and int(lease.get("pid") or -1) == os.getpid():
                return {"status": "owned", **dict(lease.get("metadata") or {})}
        lock_path = self.root / ".veyra-writer.lock"
        owner = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else ""
        return {
            "status": "not_owned",
            "state_root": self._root_key,
            "recorded_owner": owner,
        }

    def _ensure_writer_lease(self) -> None:
        if self._read_only:
            raise StateReadOnlyError(f"state root {self.root} was opened read-only")
        if not self._writer_lease_owned:
            self.acquire_writer_lease()

    @contextmanager
    def writer_transaction(self) -> Iterator[None]:
        with self._mutation_lock:
            self._ensure_writer_lease()
            yield

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

    def _action_record_retention_limit(self) -> int:
        raw = os.getenv("VEYRA_ACTION_RECORD_RETENTION_LIMIT", "10000").strip()
        try:
            return max(0, int(raw))
        except ValueError:
            return 10000

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
        if target == legacy:
            return
        if not legacy.is_file():
            return
        with self.writer_transaction():
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
            "risk_state.json": {"current_risk": "R0", "signals": [], "assessments": []},
            "risk_policy.json": {
                "schema_version": 1,
                "policy_kind": "guardian_risk_catalog",
                "levels": risk_catalog(),
            },
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
                "version": STATE_SCHEMA_VERSION,
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
            "state_refresh_state.json": {
                "cursor": 0,
                "supported_count": 0,
                "unsupported_count": 0,
                "last_selected": [],
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
            "self_improvement_proposals.json": {"proposals": [], "updated_at": None},
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
                "active_loop": {
                    "autostart": True,
                    "interval_seconds": 300.0,
                    "health_required": True,
                },
            },
            "setup_wizard.json": {"completed": False},
        }
        defaults = {name: self._with_state_metadata(name, payload, touch_updated_at=True) for name, payload in defaults.items()}
        for name, payload in defaults.items():
            path = self.path_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                with self.writer_transaction():
                    prepared = self._prepare_json_write(name, payload, {})
                    self._write_json_atomic(path, prepared)
                continue
            current = self.read_json(name)
            merged = self._merge_missing(current, payload)
            if name == "state_schema.json":
                merged["version"] = STATE_SCHEMA_VERSION
                merged["state_definitions"] = STATE_DEFINITIONS
                merged["lifecycle_statuses"] = lifecycle_statuses()
                merged["operational_modes"] = operational_modes()
            if name in DURABLE_STATE_FILES:
                merged["ttl_seconds"] = 0
            merged.setdefault("_state_revision", int(current.get("_state_revision") or 0))
            if merged != current:
                self.write_json(name, merged)
        for name in JSONL_FILES:
            path = self.path_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                with self.writer_transaction():
                    self._write_text_atomic(path, "")
        heartbeat = self.path_for("heartbeat.md")
        if not heartbeat.exists():
            with self.writer_transaction():
                self._write_text_atomic(heartbeat, f"# Veyra Heartbeat\n\nstatus: online\nupdated_at: {utc_now_iso()}\n")

    def _migrate_state_boundaries(self) -> None:
        """Move durable policy out of volatile runtime state without losing data."""
        risk_state = self.read_json("risk_state.json")
        legacy_levels = risk_state.get("levels")
        if not isinstance(legacy_levels, list) or not legacy_levels:
            return

        def update_policy(policy: dict[str, Any]) -> dict[str, Any]:
            policy["schema_version"] = max(1, int(policy.get("schema_version") or 1))
            policy["policy_kind"] = "guardian_risk_catalog"
            policy["levels"] = legacy_levels
            return policy

        self.mutate_json("risk_policy.json", update_policy)

        def update_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
            runtime.pop("levels", None)
            runtime.setdefault("assessments", [])
            return runtime

        self.mutate_json("risk_state.json", update_runtime)

    def cleanup_orphan_temp_files(self) -> int:
        """Remove abandoned atomic-write temp files while holding the writer lease."""
        removed = 0
        with self.writer_transaction():
            for path in self.root.rglob("*.tmp"):
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def read_json(self, name: str) -> dict[str, Any]:
        with self._mutation_lock:
            path = self.path_for(name)
            if not path.exists():
                return {}
            raw = path.read_text(encoding="utf-8") or "{}"
            try:
                value = json.loads(raw)
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError as exc:
                return {
                    "_state_corrupt": True,
                    "_parse_error": str(exc),
                    "_file": self.relative_path_for(name),
                }

    def read_snapshot(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Read a bounded set of JSON states under one in-process consistency lock."""
        with self._mutation_lock:
            return {name: copy.deepcopy(self.read_json(name)) for name in names}

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_directory(path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _write_text_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_directory(path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _write_bytes_atomic(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_directory(path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _fsync_directory(self, path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _merge_missing(self, current: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in defaults.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_missing(merged[key], value)
        return merged

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        with self.writer_transaction():
            previous = self.read_json(name)
            prepared = self._prepare_json_write(name, payload, previous)
            self._write_json_atomic(self.path_for(name), prepared)

    def mutate_json(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Atomically read, mutate and replace one JSON state document."""
        with self.writer_transaction():
            previous = self.read_json(name)
            working = copy.deepcopy(previous)
            mutated = mutator(working)
            payload = working if mutated is None else mutated
            if not isinstance(payload, dict):
                raise TypeError(f"mutator for {name} must return a dict or None")
            if self._mutation_business_payload(name, payload) == self._mutation_business_payload(name, previous):
                return copy.deepcopy(previous)
            prepared = self._prepare_json_write(name, payload, previous, allow_stale_revision=True)
            self._write_json_atomic(self.path_for(name), prepared)
            return copy.deepcopy(prepared)

    def patch_json(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self.mutate_json(name, lambda current: {**current, **patch})

    @staticmethod
    def _mutation_business_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Compare a mutation without store-managed revision/freshness fields."""
        ignored = {"_state_revision", "updated_at"}
        if name in CONFIG_STATE_FILES:
            ignored.update({"config_revision", "loaded_at", "source_hash"})
        return {key: value for key, value in payload.items() if key not in ignored}

    def _prepare_json_write(
        self,
        name: str,
        payload: dict[str, Any],
        previous: dict[str, Any],
        *,
        allow_stale_revision: bool = False,
    ) -> dict[str, Any]:
        incoming_revision = int(payload.get("_state_revision") or 0)
        previous_revision = int(previous.get("_state_revision") or 0)
        if (
            not allow_stale_revision
            and incoming_revision
            and previous_revision
            and incoming_revision != previous_revision
        ):
            raise StateRevisionConflictError(
                f"stale write for {name}: expected revision {previous_revision}, got {incoming_revision}; use mutate_json"
            )
        prepared = self._with_state_metadata(name, dict(payload), touch_updated_at=True)
        prepared["_state_revision"] = previous_revision + 1
        if name in CONFIG_STATE_FILES:
            prepared["config_revision"] = int(previous.get("config_revision") or 0) + 1
            prepared["loaded_at"] = prepared["updated_at"]
            prepared["source_hash"] = self._config_source_hash(prepared)
        return prepared

    def _config_source_hash(self, payload: dict[str, Any]) -> str:
        excluded = {
            "_state_revision",
            "config_revision",
            "loaded_at",
            "source_hash",
            "updated_at",
            "source",
            "confidence",
            "ttl_seconds",
            "status",
        }
        source = {key: value for key, value in payload.items() if key not in excluded}
        encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        with self.writer_transaction():
            payload = dict(payload)
            payload.setdefault("timestamp", utc_now_iso())
            if name == "action_record.jsonl":
                payload = compact_action_record(payload)
            path = self.path_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
            if name == "action_record.jsonl":
                self._action_record_appends_since_check += 1
                interval = 1 if self.action_record_auto_retention_limit <= 100 else 100
                if self._action_record_appends_since_check == 1 or self._action_record_appends_since_check >= interval:
                    self._enforce_action_record_retention_after_append(trigger=payload)
                    self._action_record_appends_since_check = 0

    def _enforce_action_record_retention_after_append(self, *, trigger: dict[str, Any]) -> None:
        if self._action_record_retention_active:
            return
        limit = max(0, int(self.action_record_auto_retention_limit or 0))
        if limit <= 0:
            return
        path = self.path_for("action_record.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        high_water, low_water = self.retention_watermarks(limit)
        if len(lines) <= high_water:
            return

        self._action_record_retention_active = True
        try:
            retained_capacity = min(max(low_water, 0), max(limit - 1, 0))
            archive_lines = lines[: len(lines) - retained_capacity] if retained_capacity else lines
            retained_lines = lines[len(archive_lines) :]
            archive_payload = "\n".join(archive_lines)
            archive_bytes = (archive_payload + ("\n" if archive_payload else "")).encode("utf-8")
            archive_root = self.root / "archive" / "retention"
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_name = f"action_record-{utc_now_iso().replace(':', '').replace('.', '')}-{uuid4().hex[:8]}.jsonl.gz"
            archive_path = archive_root / archive_name
            compressed = gzip.compress(archive_bytes, compresslevel=6, mtime=0)
            self._write_bytes_atomic(archive_path, compressed)

            audit = compact_action_record(
                {
                    "timestamp": utc_now_iso(),
                    "route": "ops_retention_auto_enforce",
                    "status": "success",
                    "artifacts": {
                        "file": "action_record.jsonl",
                        "entries": len(lines),
                        "limit": limit,
                        "high_water": high_water,
                        "low_water": low_water,
                        "pruned_entries": len(archive_lines),
                        "retained_entries": len(retained_lines) + 1,
                        "archive_path": str(archive_path.relative_to(self.root)),
                        "archive_sha256": hashlib.sha256(compressed).hexdigest(),
                        "trigger_route": trigger.get("route"),
                    },
                }
            )
            final_lines = retained_lines + [json.dumps(audit, ensure_ascii=False)]
            self._write_text_atomic(path, "\n".join(final_lines[-limit:]) + "\n")
        finally:
            self._action_record_retention_active = False

    @staticmethod
    def retention_watermarks(limit: int) -> tuple[int, int]:
        limit = max(1, int(limit))
        high_water = max(limit + 1, int(math.ceil(limit * 1.2)))
        low_water = max(0, int(math.floor(limit * 0.8)))
        return high_water, low_water

    def rotate_jsonl(
        self,
        name: str,
        *,
        limit: int,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Strict manual rotation; automatic action rotation uses batch watermarks."""
        limit = max(0, int(limit))
        with self._mutation_lock:
            path = self.path_for(name)
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            entries = len(lines)
            if entries <= limit:
                return {
                    "file": name,
                    "entries": entries,
                    "limit": limit,
                    "status": "retained",
                    "pruned_entries": 0,
                }
            retained_lines = lines[-limit:] if limit else []
            archive_lines = lines[: entries - len(retained_lines)]
            archive_payload = "\n".join(archive_lines)
            raw_bytes = (archive_payload + ("\n" if archive_payload else "")).encode("utf-8")
            compressed = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
            archive_root = self.root / "archive" / "retention"
            archive_name = f"{Path(name).stem}-{utc_now_iso().replace(':', '').replace('.', '')}-{uuid4().hex[:8]}.jsonl.gz"
            archive_path = archive_root / archive_name
            if not dry_run:
                self._ensure_writer_lease()
                self._write_bytes_atomic(archive_path, compressed)
                self._write_text_atomic(path, "\n".join(retained_lines) + ("\n" if retained_lines else ""))
            return {
                "file": name,
                "entries": entries,
                "limit": limit,
                "status": "would_rotate" if dry_run else "rotated",
                "pruned_entries": len(archive_lines),
                "retained_entries": len(retained_lines),
                "archive_path": str(archive_path.relative_to(self.root)),
                "archive_sha256": hashlib.sha256(compressed).hexdigest(),
                "archive_compression": "gzip",
            }

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
        with self.writer_transaction():
            self._write_text_atomic(self.path_for(name), content)

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
            "risk_policy": self.read_json("risk_policy.json"),
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
            "state_refresh_state": self.read_json("state_refresh_state.json"),
            "runtime_cron_state": self.read_json("runtime_cron_state.json"),
            "self_improvement_proposals": self.read_json("self_improvement_proposals.json"),
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

    def _with_state_metadata(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        touch_updated_at: bool = False,
    ) -> dict[str, Any]:
        if name not in STATE_METADATA:
            if touch_updated_at:
                payload["updated_at"] = utc_now_iso()
            return payload
        metadata = STATE_METADATA[name]
        enriched = dict(payload)
        enriched.setdefault("source", metadata["source"])
        enriched.setdefault("confidence", metadata["confidence"])
        if name in DURABLE_STATE_FILES:
            enriched["ttl_seconds"] = 0
        else:
            enriched.setdefault("ttl_seconds", metadata["ttl_seconds"])
        enriched.setdefault("status", "fresh")
        if touch_updated_at or not enriched.get("updated_at"):
            enriched["updated_at"] = utc_now_iso()
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
        payload = self.read_json(name)
        if payload.get("_state_corrupt"):
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
