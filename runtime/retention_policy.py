from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class RetentionPolicy:
    DEFAULT_LIMITS = {
        "event_log.jsonl": 10000,
        "action_record.jsonl": 10000,
        "tool_call_log.jsonl": 10000,
        "policy_trace.jsonl": 10000,
        "execution_trace.jsonl": 10000,
        "rollback_log.jsonl": 10000,
        "memory_log.jsonl": 10000,
        "core_model_trace.jsonl": 10000,
        "alert_log.jsonl": 10000,
    }

    def __init__(self, state_store: WorldStateStore, limits: dict[str, int] | None = None) -> None:
        self.state_store = state_store
        self.limits = {**self.DEFAULT_LIMITS, **(limits or {})}

    def summary(self, limits: dict[str, int] | None = None) -> dict[str, Any]:
        files = []
        for name, limit in self._active_limits(limits).items():
            path = self.state_store.root / name
            count = len(self._read_lines(path))
            files.append(
                {
                    "file": name,
                    "entries": count,
                    "limit": limit,
                    "status": "over_limit" if count > limit else "ok",
                    "recommendation": "archive_then_truncate" if count > limit else "retain",
                }
            )
        return {"status": "ok", "policy": "append_only_with_archive_before_truncate", "files": files}

    def enforce(self, *, dry_run: bool = False, limits: dict[str, int] | None = None) -> dict[str, Any]:
        active_limits = self._active_limits(limits)
        archive_root = self.state_store.root / "archive" / "retention"
        files = []
        changed = 0
        for name, limit in active_limits.items():
            path = self.state_store.root / name
            lines = self._read_lines(path)
            entries = len(lines)
            if entries <= limit:
                files.append(
                    {
                        "file": name,
                        "entries": entries,
                        "limit": limit,
                        "status": "retained",
                        "pruned_entries": 0,
                    }
                )
                continue

            changed += 1
            pruned_entries = entries - limit
            archive_lines = lines[:pruned_entries]
            retained_lines = lines[pruned_entries:]
            archive_payload = "\n".join(archive_lines)
            archive_bytes = (archive_payload + ("\n" if archive_payload else "")).encode("utf-8")
            archive_name = f"{Path(name).stem}-{utc_now_iso().replace(':', '').replace('.', '')}-{uuid4().hex[:8]}.jsonl"
            archive_path = archive_root / archive_name
            relative_archive = str(archive_path.relative_to(self.state_store.root))
            if not dry_run:
                archive_root.mkdir(parents=True, exist_ok=True)
                archive_path.write_bytes(archive_bytes)
                self._write_lines(path, retained_lines)

            files.append(
                {
                    "file": name,
                    "entries": entries,
                    "limit": limit,
                    "status": "would_truncate" if dry_run else "truncated",
                    "pruned_entries": pruned_entries,
                    "retained_entries": len(retained_lines),
                    "archive_path": relative_archive,
                    "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                }
            )

        result = {
            "status": "success",
            "dry_run": dry_run,
            "changed": changed,
            "policy": "append_only_with_archive_before_truncate",
            "checked_at": utc_now_iso(),
            "files": files,
            "validation": {
                "archive_before_truncate": True,
                "dry_run": dry_run,
                "changed": changed,
                "status": "previewed" if dry_run else "enforced",
            },
        }
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {"route": "ops_retention_enforce", "status": "dry_run" if dry_run else "success", "artifacts": result},
        )
        return result

    def _active_limits(self, limits: dict[str, int] | None = None) -> dict[str, int]:
        active = {**self.limits}
        for name, limit in (limits or {}).items():
            if name not in active:
                continue
            active[name] = max(0, int(limit))
        return active

    def _read_lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def _write_lines(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
