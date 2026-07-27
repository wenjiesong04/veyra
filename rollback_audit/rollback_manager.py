from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Iterator
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from tool_proxy.execution_scope import (
    ExecutionScope,
    ExecutionScopeError,
    ScopedPath,
)


SNAPSHOT_SCHEMA_VERSION = "veyra.rollback_snapshot.v2"
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_DIFF_CHARS = 200_000


class RollbackIntegrityError(RuntimeError):
    """Raised when a rollback record or artifact cannot be trusted."""


class RollbackManager:
    """Exact-file rollback confined to one explicit sandbox.

    A missing ``sandbox_root`` leaves the manager installed but deny-only. This
    keeps legacy application assembly safe while preventing an implicit
    current-working-directory authority boundary.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        snapshot_root: str | Path | None = None,
        *,
        sandbox_root: str | Path | None = None,
        max_snapshot_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if (
            isinstance(max_snapshot_bytes, bool)
            or not isinstance(max_snapshot_bytes, int)
            or not 1 <= max_snapshot_bytes <= _MAX_SNAPSHOT_BYTES
        ):
            raise ValueError(
                f"max_snapshot_bytes must be between 1 and {_MAX_SNAPSHOT_BYTES}"
            )
        self.state_store = state_store
        self.max_snapshot_bytes = max_snapshot_bytes
        self.scope = (
            ExecutionScope.create(sandbox_root)
            if sandbox_root is not None
            else None
        )
        selected_snapshot_root = (
            Path(snapshot_root)
            if snapshot_root is not None
            else Path(state_store.root) / "snapshots"
        )
        self.snapshot_root: Path | None = None
        self._snapshot_root_device: int | None = None
        self._snapshot_root_inode: int | None = None
        if self.scope is not None:
            self._configure_snapshot_root(selected_snapshot_root)

    def status(self) -> dict[str, Any]:
        return {
            "status": "configured" if self._configured else "disabled",
            "sandbox_root": str(self.scope.root) if self.scope else None,
            "scope_digest": self.scope.scope_digest if self.scope else None,
            "snapshot_root": (
                str(self.snapshot_root) if self.snapshot_root else None
            ),
            "max_snapshot_bytes": self.max_snapshot_bytes,
            "restore_authority": "verified_review_only",
        }

    @property
    def _configured(self) -> bool:
        return bool(
            self.scope is not None
            and self.snapshot_root is not None
            and self._snapshot_root_device is not None
            and self._snapshot_root_inode is not None
        )

    def snapshot_file(self, path: str, reason: str = "") -> dict[str, Any]:
        if not self._configured:
            return self._blocked(
                "RollbackManager requires an explicit sandbox_root"
            )
        assert self.scope is not None and self.snapshot_root is not None
        try:
            scoped = self.scope.resolve_file(path, allow_missing=True)
        except ExecutionScopeError as exc:
            return self._blocked(str(exc), path=str(path))

        snapshot_id = f"snap_{uuid4().hex[:20]}"
        artifact_name = f"{snapshot_id}.bin"
        artifact_created = False
        try:
            with self.scope.open_parent(scoped) as parent_fd:
                source_identity = self.scope.target_stat(
                    parent_fd,
                    scoped.name,
                    allow_missing=True,
                )
                if source_identity is None:
                    record = self._snapshot_record(
                        snapshot_id=snapshot_id,
                        status="tombstone",
                        scoped=scoped,
                        reason=reason,
                        artifact_name=None,
                        source_checksum=None,
                        source_identity=None,
                        artifact_identity=None,
                    )
                else:
                    source_fd = os.open(
                        scoped.name,
                        os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=parent_fd,
                    )
                    try:
                        opened_identity = os.fstat(source_fd)
                        self._require_regular_single_link(
                            opened_identity,
                            "rollback source",
                        )
                        if not self._same_file_identity(
                            source_identity,
                            opened_identity,
                        ):
                            raise RollbackIntegrityError(
                                "rollback source identity changed before snapshot"
                            )
                        if opened_identity.st_size > self.max_snapshot_bytes:
                            raise RollbackIntegrityError(
                                "rollback source exceeds max_snapshot_bytes"
                            )
                        with self._open_snapshot_root() as snapshot_root_fd:
                            artifact_fd = os.open(
                                artifact_name,
                                os.O_WRONLY
                                | os.O_CREAT
                                | os.O_EXCL
                                | _NOFOLLOW
                                | _CLOEXEC,
                                0o600,
                                dir_fd=snapshot_root_fd,
                            )
                            artifact_created = True
                            try:
                                checksum, copied = self._copy_and_hash(
                                    source_fd,
                                    artifact_fd,
                                    self.max_snapshot_bytes,
                                )
                                os.fsync(artifact_fd)
                                artifact_identity = os.fstat(artifact_fd)
                            finally:
                                os.close(artifact_fd)
                            os.fsync(snapshot_root_fd)
                        final_source_identity = os.fstat(source_fd)
                        if (
                            not self._same_file_identity(
                                opened_identity,
                                final_source_identity,
                            )
                            or int(final_source_identity.st_size) != copied
                            or int(
                                getattr(final_source_identity, "st_mtime_ns", 0)
                            )
                            != int(getattr(opened_identity, "st_mtime_ns", 0))
                        ):
                            raise RollbackIntegrityError(
                                "rollback source changed during snapshot"
                            )
                    finally:
                        os.close(source_fd)
                    record = self._snapshot_record(
                        snapshot_id=snapshot_id,
                        status="created",
                        scoped=scoped,
                        reason=reason,
                        artifact_name=artifact_name,
                        source_checksum=checksum,
                        source_identity=self._identity_dict(
                            opened_identity
                        ),
                        artifact_identity=self._identity_dict(
                            artifact_identity
                        ),
                    )
        except (ExecutionScopeError, RollbackIntegrityError, OSError) as exc:
            if artifact_created:
                self._delete_artifact_if_present(artifact_name)
            return self._blocked(str(exc), path=str(scoped.absolute))

        try:
            def append_snapshot(state: dict[str, Any]) -> None:
                snapshots = state.setdefault("snapshots", [])
                if not isinstance(snapshots, list):
                    state["snapshots"] = snapshots = []
                if any(
                    isinstance(item, dict)
                    and item.get("snapshot_id") == snapshot_id
                    for item in snapshots
                ):
                    raise RollbackIntegrityError(
                        f"duplicate rollback snapshot id: {snapshot_id}"
                    )
                snapshots.append(record)

            self.state_store.mutate_json(
                "rollback_state.json",
                append_snapshot,
            )
        except Exception:
            if artifact_created:
                self._delete_artifact_if_present(artifact_name)
            raise
        self.state_store.append_jsonl(
            "rollback_log.jsonl",
            {
                "action": "snapshot",
                "snapshot": record,
            },
        )
        return dict(record)

    def restore(
        self,
        snapshot_id: str,
        *,
        authorized: bool = False,
    ) -> dict[str, Any]:
        if authorized is not True:
            return self._blocked(
                "rollback restore requires a verified review execution claim",
                snapshot_id=str(snapshot_id),
            )
        if not self._configured:
            return self._blocked(
                "RollbackManager requires an explicit sandbox_root",
                snapshot_id=str(snapshot_id),
            )
        try:
            record, scoped = self._load_record(snapshot_id)
            if record["status"] == "tombstone":
                result = self._restore_tombstone(record, scoped)
            else:
                content = self._read_valid_artifact(record)
                self._atomic_restore(
                    scoped,
                    content,
                    int(record["source_identity"]["mode"]) & 0o777,
                )
                restored_checksum = hashlib.sha256(content).hexdigest()
                result = {
                    "status": "restored",
                    "snapshot_id": snapshot_id,
                    "source": str(scoped.absolute),
                    "restored_at": utc_now_iso(),
                    "source_checksum": restored_checksum,
                    "snapshot_checksum": record["source_checksum"],
                    "rollback_mode": "restore",
                }
        except KeyError:
            raise
        except (ExecutionScopeError, RollbackIntegrityError, OSError) as exc:
            return self._blocked(
                str(exc),
                snapshot_id=str(snapshot_id),
            )
        self.state_store.append_jsonl(
            "rollback_log.jsonl",
            {"action": "restore", "result": result},
        )
        return result

    def diff(self, snapshot_id: str) -> dict[str, Any]:
        if not self._configured:
            return self._blocked(
                "RollbackManager requires an explicit sandbox_root",
                snapshot_id=str(snapshot_id),
            )
        try:
            record, scoped = self._load_record(snapshot_id)
            if record["status"] == "tombstone":
                current = self._read_scoped_if_present(scoped)
                return {
                    "status": "ok",
                    "snapshot_id": snapshot_id,
                    "source": str(scoped.absolute),
                    "changed": current is not None,
                    "source_exists": current is not None,
                    "snapshot_exists": False,
                    "rollback_mode": "delete_created_file",
                    "diff": (
                        "File exists but snapshot records an absent target."
                        if current is not None
                        else "Target remains absent."
                    ),
                }
            before = self._read_valid_artifact(record)
            after = self._read_scoped_if_present(scoped)
            if after is None:
                return {
                    "status": "missing_source",
                    "snapshot_id": snapshot_id,
                    "source": str(scoped.absolute),
                    "changed": True,
                    "source_exists": False,
                    "snapshot_exists": True,
                }
            rendered = "\n".join(
                difflib.unified_diff(
                    before.decode("utf-8", errors="replace").splitlines(),
                    after.decode("utf-8", errors="replace").splitlines(),
                    fromfile=f"snapshot:{snapshot_id}",
                    tofile=str(scoped.absolute),
                    lineterm="",
                )
            )
            if len(rendered) > _MAX_DIFF_CHARS:
                rendered = (
                    rendered[:_MAX_DIFF_CHARS]
                    + "\n[diff truncated by rollback budget]"
                )
            return {
                "status": "ok",
                "snapshot_id": snapshot_id,
                "source": str(scoped.absolute),
                "changed": before != after,
                "source_exists": True,
                "snapshot_exists": True,
                "source_checksum": hashlib.sha256(after).hexdigest(),
                "snapshot_checksum": record["source_checksum"],
                "diff": rendered or "No changes between snapshot and current file.",
            }
        except KeyError:
            raise
        except (ExecutionScopeError, RollbackIntegrityError, OSError) as exc:
            return self._blocked(
                str(exc),
                snapshot_id=str(snapshot_id),
            )

    def _snapshot_record(
        self,
        *,
        snapshot_id: str,
        status: str,
        scoped: ScopedPath,
        reason: str,
        artifact_name: str | None,
        source_checksum: str | None,
        source_identity: dict[str, int] | None,
        artifact_identity: dict[str, int] | None,
    ) -> dict[str, Any]:
        assert self.scope is not None and self.snapshot_root is not None
        artifact_path = (
            str(self.snapshot_root / artifact_name)
            if artifact_name is not None
            else None
        )
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "status": status,
            "source": str(scoped.absolute),
            "source_relative": scoped.relative,
            "source_scope_digest": self.scope.scope_digest,
            "snapshot": artifact_path,
            "artifact_name": artifact_name,
            "reason": str(reason or "")[:1000],
            "created_at": utc_now_iso(),
            "source_checksum": source_checksum,
            "checksum": source_checksum,
            "size_bytes": (
                int(source_identity["size"])
                if source_identity is not None
                else 0
            ),
            "source_identity": source_identity,
            "artifact_identity": artifact_identity,
            "rollback_mode": (
                "restore" if status == "created" else "delete_created_file"
            ),
        }
        return {
            **payload,
            "record_digest": self._canonical_digest(payload),
        }

    def _load_record(
        self,
        snapshot_id: str,
    ) -> tuple[dict[str, Any], ScopedPath]:
        normalized_id = str(snapshot_id or "")
        if (
            not normalized_id
            or normalized_id != normalized_id.strip()
            or not normalized_id.startswith("snap_")
            or len(normalized_id) > 80
        ):
            raise RollbackIntegrityError("snapshot_id is invalid")
        snapshots = self.state_store.read_json("rollback_state.json").get(
            "snapshots",
            [],
        )
        matches = [
            item
            for item in snapshots
            if isinstance(item, dict)
            and item.get("snapshot_id") == normalized_id
        ]
        if not matches:
            raise KeyError(f"Snapshot not found: {normalized_id}")
        if len(matches) != 1:
            raise RollbackIntegrityError(
                f"Snapshot id is not unique: {normalized_id}"
            )
        record = dict(matches[0])
        if record.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise RollbackIntegrityError(
                "legacy or unsupported rollback snapshot is not executable"
            )
        supplied_digest = str(record.pop("record_digest", ""))
        if not supplied_digest or not secrets.compare_digest(
            supplied_digest,
            self._canonical_digest(record),
        ):
            raise RollbackIntegrityError(
                "rollback snapshot record failed integrity validation"
            )
        record["record_digest"] = supplied_digest
        assert self.scope is not None and self.snapshot_root is not None
        if record.get("source_scope_digest") != self.scope.scope_digest:
            raise RollbackIntegrityError(
                "rollback snapshot belongs to another sandbox"
            )
        relative = str(record.get("source_relative") or "")
        scoped = self.scope.resolve_file(relative, allow_missing=True)
        if (
            scoped.relative != relative
            or str(record.get("source") or "") != str(scoped.absolute)
        ):
            raise RollbackIntegrityError(
                "rollback source path does not match its sandbox binding"
            )
        status = str(record.get("status") or "")
        if status == "created":
            artifact_name = str(record.get("artifact_name") or "")
            expected_name = f"{normalized_id}.bin"
            if (
                artifact_name != expected_name
                or str(record.get("snapshot") or "")
                != str(self.snapshot_root / expected_name)
                or not self._valid_sha256(record.get("source_checksum"))
                or record.get("checksum") != record.get("source_checksum")
                or not isinstance(record.get("source_identity"), dict)
                or not isinstance(record.get("artifact_identity"), dict)
            ):
                raise RollbackIntegrityError(
                    "rollback artifact binding is malformed"
                )
        elif status == "tombstone":
            if any(
                record.get(key) is not None
                for key in (
                    "snapshot",
                    "artifact_name",
                    "source_checksum",
                    "checksum",
                    "source_identity",
                    "artifact_identity",
                )
            ):
                raise RollbackIntegrityError(
                    "rollback tombstone cannot carry an artifact"
                )
        else:
            raise RollbackIntegrityError(
                "rollback snapshot status is unsupported"
            )
        return record, scoped

    def _read_valid_artifact(self, record: dict[str, Any]) -> bytes:
        artifact_name = str(record["artifact_name"])
        expected_identity = record["artifact_identity"]
        with self._open_snapshot_root() as snapshot_root_fd:
            artifact_fd = os.open(
                artifact_name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=snapshot_root_fd,
            )
            try:
                identity = os.fstat(artifact_fd)
                self._require_regular_single_link(
                    identity,
                    "rollback artifact",
                )
                if not self._identity_matches(expected_identity, identity):
                    raise RollbackIntegrityError(
                        "rollback artifact identity changed"
                    )
                content = self._read_bounded(
                    artifact_fd,
                    self.max_snapshot_bytes,
                )
            finally:
                os.close(artifact_fd)
        if not secrets.compare_digest(
            hashlib.sha256(content).hexdigest(),
            str(record["source_checksum"]),
        ):
            raise RollbackIntegrityError(
                "rollback artifact checksum changed"
            )
        return content

    def _restore_tombstone(
        self,
        record: dict[str, Any],
        scoped: ScopedPath,
    ) -> dict[str, Any]:
        assert self.scope is not None
        with self.scope.open_parent(scoped) as parent_fd:
            current = self.scope.target_stat(
                parent_fd,
                scoped.name,
                allow_missing=True,
            )
            if current is not None:
                self._require_regular_single_link(
                    current,
                    "rollback tombstone target",
                )
                os.unlink(scoped.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
        return {
            "status": "restored",
            "snapshot_id": record["snapshot_id"],
            "source": str(scoped.absolute),
            "restored_at": utc_now_iso(),
            "deleted_created_file": current is not None,
            "rollback_mode": "delete_created_file",
        }

    def _atomic_restore(
        self,
        scoped: ScopedPath,
        content: bytes,
        mode: int,
    ) -> None:
        assert self.scope is not None
        temporary_name = f".veyra-restore-{secrets.token_hex(12)}.tmp"
        temporary_created = False
        with self.scope.open_parent(scoped) as parent_fd:
            current = self.scope.target_stat(
                parent_fd,
                scoped.name,
                allow_missing=True,
            )
            if current is not None:
                self._require_regular_single_link(
                    current,
                    "rollback restore target",
                )
            try:
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _NOFOLLOW
                    | _CLOEXEC,
                    mode or 0o600,
                    dir_fd=parent_fd,
                )
                temporary_created = True
                try:
                    self._write_all(temporary_fd, content)
                    os.fchmod(temporary_fd, mode or 0o600)
                    os.fsync(temporary_fd)
                    temporary_identity = os.fstat(temporary_fd)
                finally:
                    os.close(temporary_fd)
                os.replace(
                    temporary_name,
                    scoped.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_created = False
                final_identity = self.scope.target_stat(
                    parent_fd,
                    scoped.name,
                    allow_missing=False,
                )
                assert final_identity is not None
                if not self._same_file_identity(
                    temporary_identity,
                    final_identity,
                ):
                    raise RollbackIntegrityError(
                        "rollback restore identity verification failed"
                    )
                os.fsync(parent_fd)
            finally:
                if temporary_created:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def _read_scoped_if_present(
        self,
        scoped: ScopedPath,
    ) -> bytes | None:
        assert self.scope is not None
        with self.scope.open_parent(scoped) as parent_fd:
            identity = self.scope.target_stat(
                parent_fd,
                scoped.name,
                allow_missing=True,
            )
            if identity is None:
                return None
            self._require_regular_single_link(identity, "rollback source")
            source_fd = os.open(
                scoped.name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(source_fd)
                if not self._same_file_identity(identity, opened):
                    raise RollbackIntegrityError(
                        "rollback source identity changed before read"
                    )
                return self._read_bounded(
                    source_fd,
                    self.max_snapshot_bytes,
                )
            finally:
                os.close(source_fd)

    def _configure_snapshot_root(self, root: Path) -> None:
        assert self.scope is not None
        absolute = Path(os.path.abspath(os.fspath(root.expanduser())))
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_lstat = os.lstat(absolute)
        if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(
            root_lstat.st_mode
        ):
            raise RollbackIntegrityError(
                "snapshot_root must be a real directory"
            )
        canonical = absolute.resolve(strict=True)
        try:
            canonical.relative_to(self.scope.root)
        except ValueError:
            pass
        else:
            raise RollbackIntegrityError(
                "snapshot_root must be outside the Agent sandbox"
            )
        root_fd = os.open(canonical, _OPEN_DIRECTORY_FLAGS)
        try:
            identity = os.fstat(root_fd)
            if not stat.S_ISDIR(identity.st_mode):
                raise RollbackIntegrityError(
                    "snapshot_root identity is not a directory"
                )
        finally:
            os.close(root_fd)
        self.snapshot_root = canonical
        self._snapshot_root_device = int(identity.st_dev)
        self._snapshot_root_inode = int(identity.st_ino)

    @contextmanager
    def _open_snapshot_root(self) -> Iterator[int]:
        if not self._configured or self.snapshot_root is None:
            raise RollbackIntegrityError("snapshot_root is not configured")
        root_fd = os.open(self.snapshot_root, _OPEN_DIRECTORY_FLAGS)
        try:
            identity = os.fstat(root_fd)
            if (
                int(identity.st_dev) != self._snapshot_root_device
                or int(identity.st_ino) != self._snapshot_root_inode
                or not stat.S_ISDIR(identity.st_mode)
            ):
                raise RollbackIntegrityError(
                    "snapshot_root identity changed"
                )
            yield root_fd
        finally:
            os.close(root_fd)

    def _delete_artifact_if_present(self, artifact_name: str) -> None:
        try:
            with self._open_snapshot_root() as root_fd:
                os.unlink(artifact_name, dir_fd=root_fd)
                os.fsync(root_fd)
        except (FileNotFoundError, OSError, RollbackIntegrityError):
            pass

    @staticmethod
    def _copy_and_hash(
        source_fd: int,
        target_fd: int,
        limit: int,
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, min(1024 * 1024, limit + 1 - copied))
            if not chunk:
                return digest.hexdigest(), copied
            copied += len(chunk)
            if copied > limit:
                raise RollbackIntegrityError(
                    "rollback source exceeds max_snapshot_bytes"
                )
            digest.update(chunk)
            RollbackManager._write_all(target_fd, chunk)

    @staticmethod
    def _read_bounded(fd: int, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise RollbackIntegrityError(
                    "rollback artifact exceeds max_snapshot_bytes"
                )

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("rollback write made no progress")
            view = view[written:]

    @staticmethod
    def _require_regular_single_link(
        identity: os.stat_result,
        label: str,
    ) -> None:
        if not stat.S_ISREG(identity.st_mode):
            raise RollbackIntegrityError(
                f"{label} must be a regular file"
            )
        if int(identity.st_nlink) != 1:
            raise RollbackIntegrityError(
                f"{label} cannot be a hard-linked file"
            )

    @staticmethod
    def _same_file_identity(
        left: os.stat_result,
        right: os.stat_result,
    ) -> bool:
        return bool(
            int(left.st_dev) == int(right.st_dev)
            and int(left.st_ino) == int(right.st_ino)
            and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        )

    @staticmethod
    def _identity_dict(identity: os.stat_result) -> dict[str, int]:
        return {
            "device": int(identity.st_dev),
            "inode": int(identity.st_ino),
            "size": int(identity.st_size),
            "mode": int(identity.st_mode),
            "mtime_ns": int(getattr(identity, "st_mtime_ns", 0)),
        }

    @staticmethod
    def _identity_matches(
        expected: dict[str, Any],
        actual: os.stat_result,
    ) -> bool:
        return bool(
            int(expected.get("device", -1)) == int(actual.st_dev)
            and int(expected.get("inode", -1)) == int(actual.st_ino)
            and int(expected.get("size", -1)) == int(actual.st_size)
            and int(expected.get("mode", -1)) == int(actual.st_mode)
            and int(expected.get("mtime_ns", -1))
            == int(getattr(actual, "st_mtime_ns", 0))
        )

    @staticmethod
    def _canonical_digest(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        token = str(value or "")
        return bool(
            len(token) == 64
            and all(character in "0123456789abcdef" for character in token)
        )

    @staticmethod
    def _blocked(
        reason: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "status": "blocked",
            "reason": reason,
            "rollback_mode": "none",
            **extra,
        }


__all__ = [
    "RollbackIntegrityError",
    "RollbackManager",
    "SNAPSHOT_SCHEMA_VERSION",
]
