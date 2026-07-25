from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.world_state import JSONL_FILES, WorldStateStore
from interface.event_schema import utc_now_iso


class RetentionPolicy:
    """Bounded JSONL retention with batched rotation and compact gzip archives."""

    DEFAULT_LIMITS = {name: 10000 for name in JSONL_FILES}

    def __init__(self, state_store: WorldStateStore, limits: dict[str, int] | None = None) -> None:
        self.state_store = state_store
        self.limits = {**self.DEFAULT_LIMITS, **(limits or {})}
        if hasattr(self.state_store, "action_record_auto_retention_limit"):
            self.state_store.action_record_auto_retention_limit = self.limits.get("action_record.jsonl", 10000)

    def summary(self, limits: dict[str, int] | None = None) -> dict[str, Any]:
        files = []
        for name, limit in self._active_limits(limits).items():
            path = self.state_store.path_for(name)
            count = len(self._read_lines(path))
            high_water, low_water = self.state_store.retention_watermarks(max(limit, 1))
            if limit <= 0:
                status = "disabled"
                recommendation = "retain"
            elif count > high_water:
                status = "over_limit"
                recommendation = "archive_batch_then_truncate"
            elif count > limit:
                status = "buffered"
                recommendation = "retain_until_high_water"
            else:
                status = "ok"
                recommendation = "retain"
            files.append(
                {
                    "file": name,
                    "entries": count,
                    "target_limit": limit,
                    "high_water": high_water if limit > 0 else None,
                    "low_water": low_water if limit > 0 else None,
                    "status": status,
                    "recommendation": recommendation,
                }
            )
        archive = self.archive_summary()
        return {
            "status": "ok",
            "policy": "append_only_with_batched_gzip_archive",
            "files": files,
            "archive": archive,
        }

    def enforce(self, *, dry_run: bool = False, limits: dict[str, int] | None = None) -> dict[str, Any]:
        active_limits = self._active_limits(limits)
        files: list[dict[str, Any]] = []
        changed = 0
        for name, limit in active_limits.items():
            if limit <= 0:
                files.append(
                    {
                        "file": name,
                        "entries": len(self._read_lines(self.state_store.path_for(name))),
                        "limit": limit,
                        "status": "disabled",
                        "pruned_entries": 0,
                    }
                )
                continue
            if name == "situation_trace.jsonl":
                # The outbox check and rotation must share the one-writer
                # transaction. Otherwise an append-success/ack-failure can
                # appear between the check and rotate, move the transition to
                # gzip, and make live-file recovery append it a second time.
                with self.state_store.writer_transaction():
                    situation_state = self.state_store.read_json(
                        "situation_state.json"
                    )
                    raw_outbox = situation_state.get("trace_outbox")
                    pending_outbox = max(
                        0,
                        int(
                            situation_state.get("trace_outbox_count")
                            or 0
                        ),
                        (
                            len(raw_outbox)
                            if isinstance(raw_outbox, list)
                            else 0
                        ),
                    )
                    if pending_outbox:
                        rotation = {
                            "file": name,
                            "entries": len(
                                self._read_lines(
                                    self.state_store.path_for(name)
                                )
                            ),
                            "limit": limit,
                            "status": "deferred_pending_outbox",
                            "pending_trace_outbox": pending_outbox,
                            "pruned_entries": 0,
                        }
                    else:
                        rotation = self.state_store.rotate_jsonl(
                            name,
                            limit=limit,
                            dry_run=dry_run,
                        )
                files.append(rotation)
                if rotation.get("status") in {"would_rotate", "rotated"}:
                    changed += 1
                continue
            rotation = self.state_store.rotate_jsonl(name, limit=limit, dry_run=dry_run)
            files.append(rotation)
            if rotation.get("status") in {"would_rotate", "rotated"}:
                changed += 1

        result = {
            "status": "success",
            "dry_run": dry_run,
            "changed": changed,
            "policy": "append_only_with_batched_gzip_archive",
            "checked_at": utc_now_iso(),
            "files": files,
            "validation": {
                "archive_before_truncate": True,
                "compressed_archive": True,
                "dry_run": dry_run,
                "changed": changed,
                "status": "previewed" if dry_run else "enforced",
            },
        }
        if not dry_run:
            self.state_store.append_jsonl(
                "policy_trace.jsonl",
                {
                    "route": "ops_retention_enforce",
                    "status": "success",
                    "artifacts": result,
                },
            )
        return result

    def compact_archives(
        self,
        *,
        dry_run: bool = False,
        min_files_per_group: int = 10,
        max_groups: int | None = None,
    ) -> dict[str, Any]:
        """Consolidate legacy tiny JSONL archives by stream and UTC day.

        The compact gzip is fsynced and verified before the source files are
        removed, so this reduces inode usage without discarding audit rows.
        """
        archive_root = self.state_store.root / "archive" / "retention"
        groups: dict[tuple[str, str], list[Path]] = {}
        pattern = re.compile(r"^(?P<stream>.+?)-(?P<day>\d{4}-\d{2}-\d{2})T")
        if archive_root.exists():
            for path in archive_root.glob("*.jsonl"):
                match = pattern.match(path.name)
                if match:
                    groups.setdefault((match.group("stream"), match.group("day")), []).append(path)

        eligible = [
            (key, sorted(paths, key=lambda item: item.name))
            for key, paths in sorted(groups.items())
            if len(paths) >= max(2, int(min_files_per_group))
        ]
        if max_groups is not None:
            eligible = eligible[: max(0, int(max_groups))]

        compacted: list[dict[str, Any]] = []
        files_removed = 0
        bytes_before = 0
        bytes_after = 0
        with self.state_store.writer_transaction() if not dry_run else _null_transaction():
            for (stream, day), paths in eligible:
                raw_parts: list[bytes] = []
                for path in paths:
                    content = path.read_bytes()
                    raw_parts.append(content if content.endswith(b"\n") or not content else content + b"\n")
                raw = b"".join(raw_parts)
                compressed = gzip.compress(raw, compresslevel=6, mtime=0)
                output = archive_root / f"{stream}-{day}-compacted-{uuid4().hex[:8]}.jsonl.gz"
                row = {
                    "stream": stream,
                    "day": day,
                    "source_files": len(paths),
                    "source_bytes": len(raw),
                    "compressed_bytes": len(compressed),
                    "archive_path": str(output.relative_to(self.state_store.root)),
                    "archive_sha256": hashlib.sha256(compressed).hexdigest(),
                    "status": "would_compact" if dry_run else "compacted",
                }
                if not dry_run:
                    self.state_store._write_bytes_atomic(output, compressed)
                    with gzip.open(output, "rb") as handle:
                        verified = handle.read()
                    if verified != raw:
                        output.unlink(missing_ok=True)
                        raise RuntimeError(f"archive verification failed for {output}")
                    for path in paths:
                        path.unlink()
                    self.state_store._fsync_directory(archive_root)
                compacted.append(row)
                files_removed += len(paths)
                bytes_before += len(raw)
                bytes_after += len(compressed)

        result = {
            "status": "success",
            "dry_run": dry_run,
            "groups": len(compacted),
            "files_compacted": files_removed,
            "files_replaced_by": len(compacted),
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "saved_bytes": max(0, bytes_before - bytes_after),
            "items": compacted,
            "checked_at": utc_now_iso(),
        }
        if not dry_run and compacted:
            self.state_store.append_jsonl(
                "policy_trace.jsonl",
                {
                    "route": "ops_retention_compact_archives",
                    "status": "success",
                    "artifacts": {
                        key: value
                        for key, value in result.items()
                        if key != "items"
                    },
                },
            )
        return result

    def archive_summary(self) -> dict[str, Any]:
        archive_root = self.state_store.root / "archive" / "retention"
        if not archive_root.exists():
            return {"files": 0, "bytes": 0, "legacy_jsonl_files": 0, "gzip_files": 0}
        files = [path for path in archive_root.iterdir() if path.is_file()]
        return {
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "legacy_jsonl_files": sum(1 for path in files if path.suffix == ".jsonl"),
            "gzip_files": sum(1 for path in files if path.name.endswith(".jsonl.gz")),
        }

    def _active_limits(self, limits: dict[str, int] | None = None) -> dict[str, int]:
        active = {**self.limits}
        for name, limit in (limits or {}).items():
            if name not in active:
                continue
            active[name] = max(0, int(limit))
        return active

    def _read_lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class _null_transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
