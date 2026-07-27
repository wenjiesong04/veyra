from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Callable

from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.execution_scope import (
    ExecutionScope,
    ExecutionScopeError,
    ScopedPath,
)
from tool_proxy.tool_policy import ToolPolicy


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_MAX_CONFIGURED_BYTES = 16 * 1024 * 1024
Snapshotter = Callable[[str, str], dict[str, Any]]


class SafeFile:
    """Exact-file UTF-8 I/O confined to an explicitly configured sandbox."""

    def __init__(
        self,
        state_store: WorldStateStore | None = None,
        policy: ToolPolicy | None = None,
        *,
        sandbox_root: str | Path | None = None,
        scope: ExecutionScope | None = None,
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
    ) -> None:
        if scope is not None and sandbox_root is not None:
            raise ValueError("provide scope or sandbox_root, not both")
        self.state_store = state_store
        self.scope = (
            scope
            if scope is not None
            else ExecutionScope.create(sandbox_root)
            if sandbox_root is not None
            else None
        )
        self.max_read_bytes = self._validate_budget(
            max_read_bytes, "max_read_bytes"
        )
        self.max_write_bytes = self._validate_budget(
            max_write_bytes, "max_write_bytes"
        )
        self.policy = policy or ToolPolicy()
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def status(self) -> dict[str, Any]:
        return {
            "tool": "safe_file",
            "configured": self.scope is not None,
            "mode": "exact_file_sandbox" if self.scope else "disabled",
            "sandbox_root": str(self.scope.root) if self.scope else None,
            "scope_digest": self.scope.scope_digest if self.scope else None,
            "max_read_bytes": self.max_read_bytes,
            "max_write_bytes": self.max_write_bytes,
            "rollback_mode": "exact_snapshot_when_required",
        }

    def read_text(self, path: str) -> dict[str, Any]:
        scoped, failure = self._resolve(path, allow_missing=False)
        if failure is not None:
            return self._finish("file_read", path, failure, failure["review"])
        assert scoped is not None and self.scope is not None

        review = self.policy.review_file_read(str(scoped.absolute))
        self._record_policy("file_read", str(scoped.absolute), review)
        if review["decision"] == "block":
            return self._finish(
                "file_read",
                str(scoped.absolute),
                {
                    "status": "blocked",
                    "review": review,
                    "path": str(scoped.absolute),
                },
                review,
            )

        try:
            with self.scope.open_parent(scoped) as parent_fd:
                fd = os.open(
                    scoped.name,
                    os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=parent_fd,
                )
                try:
                    identity = os.fstat(fd)
                    if not stat.S_ISREG(identity.st_mode):
                        raise ExecutionScopeError(
                            "sandbox target must be a regular file"
                        )
                    if identity.st_size > self.max_read_bytes:
                        raise ExecutionScopeError(
                            "sandbox file exceeds max_read_bytes"
                        )
                    content_bytes = self._read_bounded(fd, self.max_read_bytes)
                finally:
                    os.close(fd)
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                result = {
                    "status": "error",
                    "reason": "sandbox file is not valid UTF-8 text",
                    "path": str(scoped.absolute),
                    "review": review,
                }
                return self._finish(
                    "file_read", str(scoped.absolute), result, review
                )
        except (ExecutionScopeError, OSError) as exc:
            result = {
                "status": "blocked",
                "reason": str(exc),
                "path": str(scoped.absolute),
                "review": review,
            }
            return self._finish("file_read", str(scoped.absolute), result, review)

        result = {
            "status": "ok",
            "path": str(scoped.absolute),
            "relative_path": scoped.relative,
            "content": content,
            "content_bytes": len(content_bytes),
            "content_digest": hashlib.sha256(content_bytes).hexdigest(),
            "scope_digest": self.scope.scope_digest,
        }
        trace_payload = {
            key: value for key, value in result.items() if key != "content"
        }
        result["tool_trace"] = self._record(
            "file_read", str(scoped.absolute), trace_payload, review
        )
        return result

    def write_text(
        self,
        path: str,
        content: str,
        reason: str = "",
        approved_by: str | None = None,
        *,
        require_snapshot: bool = False,
        snapshotter: Snapshotter | None = None,
    ) -> dict[str, Any]:
        try:
            content_bytes = content.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            review = self._boundary_review("content must be valid UTF-8 text")
            result = {
                "status": "blocked",
                "reason": review["reason"],
                "path": str(path),
                "operation": "write_text",
                "review": review,
            }
            self._record_policy("file_write", str(path), review, approved_by)
            return self._finish(
                "file_write", str(path), result, review, approved_by
            )
        if len(content_bytes) > self.max_write_bytes:
            review = self._boundary_review(
                "content exceeds max_write_bytes"
            )
            result = {
                "status": "blocked",
                "reason": review["reason"],
                "path": str(path),
                "operation": "write_text",
                "review": review,
            }
            self._record_policy("file_write", str(path), review, approved_by)
            return self._finish(
                "file_write", str(path), result, review, approved_by
            )

        scoped, failure = self._resolve(path, allow_missing=True)
        if failure is not None:
            return self._finish(
                "file_write", path, failure, failure["review"], approved_by
            )
        assert scoped is not None and self.scope is not None

        review = self.policy.review_file_write(str(scoped.absolute))
        self._record_policy(
            "file_write", str(scoped.absolute), review, approved_by
        )
        if review["decision"] == "block":
            return self._finish(
                "file_write",
                str(scoped.absolute),
                {
                    "status": "blocked",
                    "path": str(scoped.absolute),
                    "operation": "write_text",
                    "review": review,
                },
                review,
                approved_by,
            )
        # A string such as ``approved_by=rev_...`` is diagnostic metadata, not
        # an authorization credential. A later executor slice will pass a
        # verified execution context instead.
        if review["decision"] == "ask_user":
            return self._finish(
                "file_write",
                str(scoped.absolute),
                {
                    "status": "needs_confirmation",
                    "path": str(scoped.absolute),
                    "operation": "write_text",
                    "review": review,
                },
                review,
                approved_by,
            )

        snapshot: dict[str, Any] | None = None
        if require_snapshot:
            if snapshotter is None:
                result = {
                    "status": "blocked",
                    "reason": (
                        "governed file write requires an exact rollback "
                        "snapshot before execution"
                    ),
                    "path": str(scoped.absolute),
                    "operation": "write_text",
                    "review": review,
                }
                return self._finish(
                    "file_write",
                    str(scoped.absolute),
                    result,
                    review,
                    approved_by,
                )
            try:
                snapshot = snapshotter(
                    str(scoped.absolute),
                    reason or "safe_file.write_text",
                )
            except Exception as exc:
                result = {
                    "status": "blocked",
                    "reason": f"rollback snapshot failed: {exc}",
                    "path": str(scoped.absolute),
                    "operation": "write_text",
                    "review": review,
                }
                return self._finish(
                    "file_write",
                    str(scoped.absolute),
                    result,
                    review,
                    approved_by,
                )
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("status") not in {"created", "tombstone"}
                or snapshot.get("source") != str(scoped.absolute)
                or snapshot.get("source_scope_digest")
                != self.scope.scope_digest
                or not isinstance(snapshot.get("snapshot_id"), str)
            ):
                result = {
                    "status": "blocked",
                    "reason": (
                        "rollback snapshot did not certify the exact sandbox "
                        "target"
                    ),
                    "path": str(scoped.absolute),
                    "operation": "write_text",
                    "review": review,
                }
                return self._finish(
                    "file_write",
                    str(scoped.absolute),
                    result,
                    review,
                    approved_by,
                )

        try:
            replaced_existing = self._atomic_write(scoped, content_bytes)
        except (ExecutionScopeError, OSError) as exc:
            result = {
                "status": "blocked",
                "reason": str(exc),
                "path": str(scoped.absolute),
                "operation": "write_text",
                "review": review,
            }
            return self._finish(
                "file_write",
                str(scoped.absolute),
                result,
                review,
                approved_by,
            )

        result = {
            "status": "ok",
            "path": str(scoped.absolute),
            "relative_path": scoped.relative,
            "operation": "write_text",
            "content_bytes": len(content_bytes),
            "content_digest": hashlib.sha256(content_bytes).hexdigest(),
            "replaced_existing": replaced_existing,
            "snapshot": snapshot,
            "rollback_status": (
                "available" if snapshot is not None else "not_requested"
            ),
            "scope_digest": self.scope.scope_digest,
            "approved_by": approved_by,
            "reason": reason,
            "review": review,
        }
        result["tool_trace"] = self._record(
            "file_write",
            str(scoped.absolute),
            result,
            review,
            approved_by,
        )
        return result

    def _atomic_write(self, scoped: ScopedPath, content: bytes) -> bool:
        assert self.scope is not None
        temporary_name = f".veyra-write-{secrets.token_hex(12)}.tmp"
        temporary_created = False
        with self.scope.open_parent(scoped) as parent_fd:
            before = self.scope.target_stat(
                parent_fd, scoped.name, allow_missing=True
            )
            mode = stat.S_IMODE(before.st_mode) if before is not None else 0o600
            try:
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _NOFOLLOW
                    | _CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
                temporary_created = True
                try:
                    self._write_all(temporary_fd, content)
                    os.fchmod(temporary_fd, mode)
                    os.fsync(temporary_fd)
                    temporary_identity = os.fstat(temporary_fd)
                finally:
                    os.close(temporary_fd)

                current = self.scope.target_stat(
                    parent_fd, scoped.name, allow_missing=True
                )
                if before is None and current is not None:
                    raise ExecutionScopeError(
                        "sandbox target appeared during atomic write"
                    )
                if before is not None and (
                    current is None
                    or int(current.st_dev) != int(before.st_dev)
                    or int(current.st_ino) != int(before.st_ino)
                    or int(current.st_mode) != int(before.st_mode)
                ):
                    raise ExecutionScopeError(
                        "sandbox target identity changed during atomic write"
                    )
                os.replace(
                    temporary_name,
                    scoped.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_created = False
                final_identity = self.scope.target_stat(
                    parent_fd, scoped.name, allow_missing=False
                )
                assert final_identity is not None
                if (
                    int(final_identity.st_dev) != int(temporary_identity.st_dev)
                    or int(final_identity.st_ino)
                    != int(temporary_identity.st_ino)
                    or int(final_identity.st_size) != len(content)
                ):
                    raise ExecutionScopeError(
                        "atomic write identity verification failed"
                    )
                os.fsync(parent_fd)
            finally:
                if temporary_created:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
        return before is not None

    def _resolve(
        self,
        path: str,
        *,
        allow_missing: bool,
    ) -> tuple[ScopedPath | None, dict[str, Any] | None]:
        if self.scope is None:
            review = self._boundary_review(
                "SafeFile requires an explicit sandbox_root"
            )
            self._record_policy(
                "file_write" if allow_missing else "file_read",
                str(path),
                review,
            )
            return None, {
                "status": "blocked",
                "reason": review["reason"],
                "path": str(path),
                "review": review,
            }
        try:
            return self.scope.resolve_file(
                path, allow_missing=allow_missing
            ), None
        except ExecutionScopeError as exc:
            review = self._boundary_review(str(exc))
            self._record_policy(
                "file_write" if allow_missing else "file_read",
                str(path),
                review,
            )
            return None, {
                "status": "blocked",
                "reason": str(exc),
                "path": str(path),
                "review": review,
            }

    @staticmethod
    def _read_bounded(fd: int, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ExecutionScopeError("sandbox file exceeds max_read_bytes")

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("atomic write made no progress")
            view = view[written:]

    @staticmethod
    def _validate_budget(value: int, field_name: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > _MAX_CONFIGURED_BYTES
        ):
            raise ValueError(
                f"{field_name} must be between 1 and {_MAX_CONFIGURED_BYTES}"
            )
        return value

    @staticmethod
    def _boundary_review(reason: str) -> dict[str, Any]:
        return {
            "decision": "block",
            "risk_level": "R5",
            "reason": reason,
            "required_preconditions": ["configure and remain inside sandbox"],
            "forbidden": ["sandbox_escape", "symlink_traversal"],
        }

    def _finish(
        self,
        action_type: str,
        target: str,
        result: dict[str, Any],
        review: dict[str, Any],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        result["tool_trace"] = self._record(
            action_type, target, result, review, approved_by
        )
        return result

    def _record(
        self,
        action_type: str,
        target: str,
        payload: dict[str, Any],
        review: dict[str, Any],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        return self.tool_trace.record(
            tool="safe_file",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(
        self,
        action_type: str,
        target: str,
        review: dict[str, Any],
        approved_by: str | None = None,
    ) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_file",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )


__all__ = ["SafeFile"]
