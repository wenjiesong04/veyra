from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator


_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_MAX_PATH_CHARS = 4096


class ExecutionScopeError(ValueError):
    """Raised when a requested target cannot be proven inside a sandbox."""


@dataclass(frozen=True, slots=True)
class ScopedPath:
    requested: str
    absolute: Path
    relative_parts: tuple[str, ...]
    scope_digest: str

    @property
    def name(self) -> str:
        return self.relative_parts[-1]

    @property
    def relative(self) -> str:
        return "/".join(self.relative_parts)


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """Filesystem identity boundary for sandboxed tool operations.

    Paths are reduced to components relative to one fixed directory identity.
    Callers must still use :meth:`open_parent` for the actual I/O so validation
    and use share pinned directory file descriptors.
    """

    root: Path
    requested_root: Path
    root_device: int
    root_inode: int
    scope_digest: str

    @classmethod
    def create(cls, sandbox_root: str | Path) -> "ExecutionScope":
        if not _NOFOLLOW or not _DIRECTORY:
            raise ExecutionScopeError(
                "secure sandbox traversal is unavailable on this platform"
            )
        raw = str(sandbox_root)
        if not raw or "\x00" in raw or len(raw) > _MAX_PATH_CHARS:
            raise ExecutionScopeError("sandbox_root must be a bounded path")
        requested = Path(raw).expanduser()
        absolute = Path(os.path.abspath(os.fspath(requested)))
        try:
            root_lstat = os.lstat(absolute)
        except OSError as exc:
            raise ExecutionScopeError(f"sandbox_root is unavailable: {exc}") from exc
        if stat.S_ISLNK(root_lstat.st_mode):
            raise ExecutionScopeError("sandbox_root itself cannot be a symlink")
        if not stat.S_ISDIR(root_lstat.st_mode):
            raise ExecutionScopeError("sandbox_root must be an existing directory")
        try:
            canonical = absolute.resolve(strict=True)
            root_fd = os.open(canonical, _OPEN_DIRECTORY_FLAGS)
        except OSError as exc:
            raise ExecutionScopeError(f"sandbox_root cannot be pinned: {exc}") from exc
        try:
            identity = os.fstat(root_fd)
            if not stat.S_ISDIR(identity.st_mode):
                raise ExecutionScopeError("sandbox_root identity is not a directory")
        finally:
            os.close(root_fd)
        digest = hashlib.sha256(
            (
                f"{canonical}\0{identity.st_dev}\0{identity.st_ino}"
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            root=canonical,
            requested_root=absolute,
            root_device=int(identity.st_dev),
            root_inode=int(identity.st_ino),
            scope_digest=digest,
        )

    def resolve_file(
        self,
        path: str | Path,
        *,
        allow_missing: bool,
    ) -> ScopedPath:
        parts = self._relative_parts(path)
        scoped = ScopedPath(
            requested=str(path),
            absolute=self.root.joinpath(*parts),
            relative_parts=parts,
            scope_digest=self.scope_digest,
        )
        with self.open_parent(scoped) as parent_fd:
            current = self.target_stat(parent_fd, scoped.name, allow_missing=True)
            if current is None and not allow_missing:
                raise ExecutionScopeError("sandbox target does not exist")
        return scoped

    @contextmanager
    def open_parent(self, scoped: ScopedPath) -> Iterator[int]:
        if scoped.scope_digest != self.scope_digest:
            raise ExecutionScopeError("scoped path belongs to another sandbox")
        root_fd = self._open_root()
        current_fd = root_fd
        try:
            for component in scoped.relative_parts[:-1]:
                try:
                    next_fd = os.open(
                        component,
                        _OPEN_DIRECTORY_FLAGS,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise ExecutionScopeError(
                        f"sandbox parent component is unavailable: {component}"
                    ) from exc
                identity = os.fstat(next_fd)
                if not stat.S_ISDIR(identity.st_mode):
                    os.close(next_fd)
                    raise ExecutionScopeError(
                        f"sandbox parent component is not a directory: {component}"
                    )
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd
        finally:
            os.close(current_fd)

    @staticmethod
    def target_stat(
        parent_fd: int,
        name: str,
        *,
        allow_missing: bool,
    ) -> os.stat_result | None:
        try:
            identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise ExecutionScopeError("sandbox target does not exist") from None
        except OSError as exc:
            raise ExecutionScopeError(f"sandbox target cannot be inspected: {exc}") from exc
        if stat.S_ISLNK(identity.st_mode):
            raise ExecutionScopeError("sandbox target cannot be a symlink")
        if not stat.S_ISREG(identity.st_mode):
            raise ExecutionScopeError("sandbox target must be a regular file")
        return identity

    def _open_root(self) -> int:
        try:
            root_fd = os.open(self.root, _OPEN_DIRECTORY_FLAGS)
        except OSError as exc:
            raise ExecutionScopeError(f"sandbox_root cannot be reopened: {exc}") from exc
        identity = os.fstat(root_fd)
        if (
            int(identity.st_dev) != self.root_device
            or int(identity.st_ino) != self.root_inode
            or not stat.S_ISDIR(identity.st_mode)
        ):
            os.close(root_fd)
            raise ExecutionScopeError("sandbox_root identity changed")
        return root_fd

    def _relative_parts(self, path: str | Path) -> tuple[str, ...]:
        raw = str(path)
        if (
            not raw
            or raw != raw.strip()
            or "\x00" in raw
            or len(raw) > _MAX_PATH_CHARS
        ):
            raise ExecutionScopeError("sandbox path must be a bounded normalized string")
        requested = Path(raw).expanduser()
        if ".." in requested.parts:
            raise ExecutionScopeError("sandbox path cannot contain parent traversal")

        if requested.is_absolute():
            absolute = Path(os.path.abspath(os.fspath(requested)))
            try:
                relative = absolute.relative_to(self.root)
            except ValueError:
                try:
                    relative = absolute.relative_to(self.requested_root)
                except ValueError as exc:
                    raise ExecutionScopeError(
                        "sandbox path escapes sandbox_root"
                    ) from exc
        else:
            relative = requested
        parts = tuple(part for part in relative.parts if part not in {"", "."})
        if not parts:
            raise ExecutionScopeError("sandbox path must identify one file")
        for component in parts:
            if (
                component in {".", ".."}
                or "/" in component
                or "\x00" in component
                or len(component.encode("utf-8")) > 255
            ):
                raise ExecutionScopeError("sandbox path contains an invalid component")
        return parts


__all__ = [
    "ExecutionScope",
    "ExecutionScopeError",
    "ScopedPath",
]
