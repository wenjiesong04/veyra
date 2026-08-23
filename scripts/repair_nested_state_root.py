#!/usr/bin/env python3
"""Recover the exact ``<state-root>/runtime/runtime`` nesting accident.

The command is intentionally a dry-run unless ``--apply`` is supplied.  It
does not open ``WorldStateStore``: doing so could acquire a lease while the
repair tool is deciding whether the canonical API is still alive.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Veyra's supported runtime is POSIX.
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import (  # noqa: E402
    STATE_FILE_LAYOUT,
    STATE_RUNTIME,
    STATE_CONFIG,
    STATE_WORLD_EXTERNAL,
    STATE_WORLD_LOCAL,
    STATE_WORLD_USER,
    STATE_LOGS,
    _state_root_marker,
)


ACCIDENTAL_LAYOUT_DIRS = (
    STATE_CONFIG,
    STATE_WORLD_USER,
    STATE_WORLD_LOCAL,
    STATE_LOGS,
    STATE_WORLD_EXTERNAL,
)
ACCIDENTAL_TOP_LEVEL_FILES = (".veyra-writer.lock", "heartbeat.md")
# These directories are owned by runtime artifact producers.  They live
# directly under the canonical runtime partition and are not evidence of the
# nested-root accident.  Never back them up, remove them, or classify them as
# unexpected entries during this narrowly scoped recovery.
PRESERVED_RUNTIME_ARTIFACT_DIRS = (
    "local_tool_sandbox",
    "openclaw_native_block_canaries",
    "openclaw_tool_sandboxes",
    "phase6_extension_artifacts",
    "rollback_snapshots",
    "sandbox_repairs",
)
REPAIR_SCHEMA = "veyra.nested_state_root_repair.v1"


class NestedStateRepairError(RuntimeError):
    """Raised when repair cannot prove that a mutation is safe."""


@contextmanager
def _writer_lock(state_root: Path) -> Iterable[bool]:
    """Hold the same OS lock used by ``WorldStateStore`` for the full repair.

    The lock is acquired before the recovery plan is inspected and released
    only after the backup, moves, and manifest are complete.  This closes the
    LaunchAgent restart window between a stale-PID check and mutation.
    """

    lock_path = state_root / ".veyra-writer.lock"
    existed = lock_path.exists()
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise NestedStateRepairError(
                    f"state root {state_root} has an active writer lock"
                ) from exc
            recorded = _lock_status(lock_path)
            if recorded.get("active") is True:
                raise NestedStateRepairError(
                    f"state root {state_root} records an active writer: {recorded}"
                )
        else:
            status = _lock_status(lock_path)
            if status.get("active") is not False:
                raise NestedStateRepairError(
                    f"cannot prove state-root writer inactive: {status}"
                )
        yield not existed
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_equal(left: Path, right: Path) -> bool:
    try:
        left_value = json.loads(left.read_text(encoding="utf-8"))
        right_value = json.loads(right.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return left_value == right_value


def _pid_alive(pid: int) -> bool | None:
    """Return True/False, or None when liveness cannot be established."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _lock_status(path: Path, *, held_by_repair: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "status": "not_present", "active": False}
    if not path.is_file() or path.is_symlink():
        return {"path": str(path), "status": "invalid", "active": None}
    if held_by_repair:
        return {"path": str(path), "status": "held_by_repair", "active": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "")
        pid = int(payload.get("pid")) if isinstance(payload, dict) else 0
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"path": str(path), "status": "invalid", "active": None}
    if pid <= 0:
        return {"path": str(path), "status": "invalid", "active": None}
    alive = _pid_alive(pid)
    if alive is True:
        status = "active"
    elif alive is False:
        status = "stale"
    else:
        status = "unknown"
    return {"path": str(path), "pid": pid, "status": status, "active": alive}


def _expected_layout_files(directory: str) -> set[str]:
    return {
        relative
        for relative in STATE_FILE_LAYOUT.values()
        if relative.split("/", 1)[0] == directory
    }


def _canonical_runtime_file_names() -> set[str]:
    return {
        Path(relative).name
        for relative in STATE_FILE_LAYOUT.values()
        if relative.split("/", 1)[0] == STATE_RUNTIME
    }


def _validate_generated_directory(path: Path, directory: str) -> dict[str, Any]:
    """Validate one accidental layout directory without following symlinks."""

    if not path.exists():
        return {"path": str(path), "status": "missing", "files": []}
    if path.is_symlink() or not path.is_dir():
        return {"path": str(path), "status": "unsafe", "files": []}

    expected = _expected_layout_files(directory)
    actual: list[str] = []
    unsafe: list[str] = []
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                unsafe.append(str(child.relative_to(path)))
        for name in files:
            child = current_path / name
            relative = str(child.relative_to(path))
            actual.append(relative)
            if child.is_symlink() or relative not in {
                item.split("/", 1)[1] for item in expected
            }:
                unsafe.append(relative)
    status = "generated" if not unsafe else "unsafe"
    return {
        "path": str(path),
        "status": status,
        "files": sorted(actual),
        "unsafe": sorted(set(unsafe)),
    }


def _iter_files(paths: Iterable[Path]) -> list[tuple[Path, str]]:
    """Collect files for backup, rejecting symlink traversal."""

    result: list[tuple[Path, str]] = []
    for root in paths:
        if not root.exists():
            continue
        if root.is_symlink():
            raise NestedStateRepairError(f"refusing symlink in accidental root: {root}")
        if root.is_file():
            result.append((root, root.name))
            continue
        if not root.is_dir():
            raise NestedStateRepairError(f"refusing non-directory accidental path: {root}")
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directories:
                child = current_path / name
                if child.is_symlink():
                    raise NestedStateRepairError(f"refusing symlink in accidental root: {child}")
            for name in files:
                child = current_path / name
                if child.is_symlink():
                    raise NestedStateRepairError(f"refusing symlink in accidental root: {child}")
                result.append((child, str(child.relative_to(root.parent))))
    return result


def _nested_runtime_files(nested_runtime: Path, canonical_runtime: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not nested_runtime.exists():
        return [], []
    if nested_runtime.is_symlink() or not nested_runtime.is_dir():
        raise NestedStateRepairError(f"refusing unsafe nested runtime path: {nested_runtime}")

    files: list[dict[str, Any]] = []
    unexpected: list[str] = []
    for entry in sorted(nested_runtime.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            unexpected.append(entry.name)
            continue
        if entry.is_dir():
            # Artifact directories are valid runtime extensions and must stay
            # in place.  They are included in the backup, but never promoted.
            continue
        if not entry.is_file() or entry.suffix != ".json":
            unexpected.append(entry.name)
            continue
        target = canonical_runtime / entry.name
        item: dict[str, Any] = {
            "source": str(entry),
            "target": str(target),
            "name": entry.name,
            "source_sha256": _sha256(entry),
            "target_exists": target.exists() or target.is_symlink(),
        }
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                item["status"] = "conflict"
                item["conflict"] = "target_not_regular_file"
            elif item["source_sha256"] == _sha256(target) or _json_equal(entry, target):
                item["status"] = "identical"
                item["target_sha256"] = _sha256(target)
            else:
                item["status"] = "conflict"
                item["target_sha256"] = _sha256(target)
        else:
            item["status"] = "move"
        files.append(item)
    return files, unexpected


def inspect_nested_state_root(
    state_root: Path | str,
    *,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Build a mutation-free recovery plan for the exact nesting accident."""

    canonical = _resolved(state_root)
    marker = _state_root_marker(canonical)
    if marker is None:
        raise NestedStateRepairError(
            f"{canonical} is not recognized as a canonical Veyra state root "
            "(missing config/state_schema.json or .veyra-writer.lock)"
        )

    accidental_root = canonical / STATE_RUNTIME
    nested_runtime = accidental_root / STATE_RUNTIME
    canonical_runtime = canonical / STATE_RUNTIME
    files, unexpected_runtime = _nested_runtime_files(nested_runtime, canonical_runtime)
    layout = {
        directory: _validate_generated_directory(accidental_root / directory, directory)
        for directory in ACCIDENTAL_LAYOUT_DIRS
    }
    allowed_accidental_entries = {
        STATE_RUNTIME,
        *ACCIDENTAL_LAYOUT_DIRS,
        *ACCIDENTAL_TOP_LEVEL_FILES,
        *PRESERVED_RUNTIME_ARTIFACT_DIRS,
        *_canonical_runtime_file_names(),
    }
    unexpected_accidental_entries = sorted(
        entry.name
        for entry in accidental_root.iterdir()
        if entry.name not in allowed_accidental_entries
    ) if accidental_root.is_dir() else []
    nested_lock = _lock_status(accidental_root / ".veyra-writer.lock")
    canonical_lock = _lock_status(
        canonical / ".veyra-writer.lock",
        held_by_repair=lock_held,
    )
    conflicts = [item for item in files if item.get("status") == "conflict"]
    unsafe_layout = [
        {"directory": directory, **value}
        for directory, value in layout.items()
        if value.get("status") == "unsafe"
    ]
    if unexpected_runtime:
        conflicts.append({"path": str(nested_runtime), "unexpected_files": unexpected_runtime})
    if unexpected_accidental_entries:
        conflicts.append(
            {
                "path": str(accidental_root),
                "unexpected_entries": unexpected_accidental_entries,
            }
        )
    if nested_lock.get("active") is not False and nested_lock.get("status") != "not_present":
        conflicts.append({"path": str(accidental_root / ".veyra-writer.lock"), "lock": nested_lock})
    conflicts.extend(unsafe_layout)

    source_roots = [nested_runtime]
    source_roots.extend(accidental_root / directory for directory in ACCIDENTAL_LAYOUT_DIRS)
    source_roots.extend(accidental_root / name for name in ACCIDENTAL_TOP_LEVEL_FILES)
    backup_files = _iter_files(source_roots)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = canonical / ".recovery-backups" / f"{timestamp}-nested-runtime-root"
    has_generated_layout = any(
        value.get("status") == "generated" for value in layout.values()
    )
    has_stale_nested_lock = nested_lock.get("status") == "stale"
    has_repair_candidates = bool(files or has_generated_layout or has_stale_nested_lock)
    status = "dry_run" if has_repair_candidates else "clean"
    if conflicts:
        status = "blocked"
    return {
        "schema": REPAIR_SCHEMA,
        "status": status,
        "state_root": str(canonical),
        "state_root_marker": marker,
        "accidental_root": str(accidental_root),
        "nested_runtime": str(nested_runtime),
        "canonical_lock": canonical_lock,
        "nested_lock": nested_lock,
        "runtime_files": files,
        "unexpected_runtime_files": unexpected_runtime,
        "unexpected_accidental_entries": unexpected_accidental_entries,
        "accidental_layout": layout,
        "backup_path": str(backup_path),
        "backup_files": [relative for _source, relative in backup_files],
        "conflicts": conflicts,
    }


def _copy_backup(state_root: Path, plan: dict[str, Any]) -> tuple[Path, list[str]]:
    backup_path = Path(str(plan["backup_path"]))
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.mkdir(exist_ok=False)
    copied: list[str] = []
    accidental_root = state_root / STATE_RUNTIME
    source_roots = [accidental_root / STATE_RUNTIME]
    source_roots.extend(accidental_root / directory for directory in ACCIDENTAL_LAYOUT_DIRS)
    source_roots.extend(accidental_root / name for name in ACCIDENTAL_TOP_LEVEL_FILES)
    for source, relative in _iter_files(source_roots):
        destination = backup_path / relative
        if destination.exists() or destination.is_symlink():
            raise NestedStateRepairError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative)
    return backup_path, copied


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _remove_generated_directories(state_root: Path, plan: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    accidental_root = state_root / STATE_RUNTIME
    for directory in ACCIDENTAL_LAYOUT_DIRS:
        item = plan["accidental_layout"][directory]
        if item.get("status") != "generated":
            continue
        path = accidental_root / directory
        shutil.rmtree(path)
        removed.append(str(path))
    nested_lock = accidental_root / ".veyra-writer.lock"
    if plan["nested_lock"].get("status") == "stale" and nested_lock.exists():
        nested_lock.unlink()
        removed.append(str(nested_lock))
    nested_runtime = accidental_root / STATE_RUNTIME
    if nested_runtime.exists() and nested_runtime.is_dir():
        try:
            nested_runtime.rmdir()
            removed.append(str(nested_runtime))
        except OSError:
            # A legal artifact directory remains; leave it untouched.
            pass
    return removed


def repair_nested_state_root(state_root: Path | str, *, apply: bool = False) -> dict[str, Any]:
    """Inspect or apply the exact nesting recovery plan."""

    canonical = _resolved(state_root)
    with _writer_lock(canonical):
        plan = inspect_nested_state_root(canonical, lock_held=True)
        if not apply:
            plan["status"] = "dry_run" if plan["status"] == "dry_run" else plan["status"]
            return plan
        if plan["status"] == "clean":
            return plan
        if plan["status"] == "blocked":
            raise NestedStateRepairError(
                "refusing nested state-root repair: "
                + json.dumps(plan["conflicts"], ensure_ascii=False, sort_keys=True)
            )
        canonical_lock = plan["canonical_lock"]
        if canonical_lock.get("status") not in {"not_present", "stale", "held_by_repair"} or canonical_lock.get("active") is not False:
            raise NestedStateRepairError(
                "canonical state-root writer is active or cannot be proven inactive: "
                + json.dumps(canonical_lock, ensure_ascii=False, sort_keys=True)
            )

        backup_path, copied = _copy_backup(canonical, plan)
        moved: list[str] = []
        identical: list[str] = []
        for item in plan["runtime_files"]:
            source = Path(item["source"])
            target = Path(item["target"])
            if item["status"] == "identical":
                source.unlink()
                identical.append(str(source))
                continue
            os.replace(source, target)
            moved.append(str(target))
        removed = _remove_generated_directories(canonical, plan)
        manifest = {
            "schema": REPAIR_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state_root": str(canonical),
            "accidental_root": str(canonical / STATE_RUNTIME),
            "backup_path": str(backup_path),
            "copied": copied,
            "moved": moved,
            "identical_source_removed": identical,
            "removed": removed,
            "runtime_files": plan["runtime_files"],
        }
        _write_manifest(backup_path / "manifest.json", manifest)
        return {**plan, "status": "applied", "backup_path": str(backup_path), "manifest": manifest}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path, help="canonical Veyra state root")
    parser.add_argument("--apply", action="store_true", help="apply after all safety checks pass")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = repair_nested_state_root(args.state_root, apply=args.apply)
    except NestedStateRepairError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
