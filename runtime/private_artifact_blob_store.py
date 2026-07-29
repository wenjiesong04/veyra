from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator


PRIVATE_ARTIFACT_BLOB_SCHEMA_VERSION = (
    "veyra.phase6.private_artifact_blob.v1"
)
MAX_PRIVATE_ARTIFACT_BLOB_BYTES = 65_536
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_BLOB_MODE = 0o600

_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_FILENAME = re.compile(r"^blob_[0-9a-f]{64}\.bin$")
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_OPEN_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_OPEN_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
)
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class PrivateArtifactBlobStoreError(ValueError):
    """The private artifact blob store failed closed."""


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    """Persistable identity for one immutable private artifact blob."""

    schema_version: str
    artifact_id: str
    relative_filename: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    file_mode: int
    link_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "relative_filename": self.relative_filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "file_mode": self.file_mode,
            "link_count": self.link_count,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BlobMetadata":
        if not isinstance(value, dict):
            raise PrivateArtifactBlobStoreError(
                "blob metadata must be an object"
            )
        required = {
            "schema_version",
            "artifact_id",
            "relative_filename",
            "sha256",
            "size_bytes",
            "device",
            "inode",
            "file_mode",
            "link_count",
        }
        if set(value) != required:
            raise PrivateArtifactBlobStoreError(
                "blob metadata fields are invalid"
            )

        schema_version = value["schema_version"]
        artifact_id = _validated_artifact_id(value["artifact_id"])
        relative_filename = value["relative_filename"]
        digest = _validated_sha256(value["sha256"])
        if schema_version != PRIVATE_ARTIFACT_BLOB_SCHEMA_VERSION:
            raise PrivateArtifactBlobStoreError(
                "blob metadata schema version is invalid"
            )
        if (
            not isinstance(relative_filename, str)
            or not _RELATIVE_FILENAME.fullmatch(relative_filename)
            or relative_filename != _filename_for(artifact_id)
        ):
            raise PrivateArtifactBlobStoreError(
                "blob relative filename is invalid"
            )

        size_bytes = _strict_int(value["size_bytes"], "size_bytes")
        device = _strict_int(value["device"], "device")
        inode = _strict_int(value["inode"], "inode")
        file_mode = _strict_int(value["file_mode"], "file_mode")
        link_count = _strict_int(value["link_count"], "link_count")
        if not 1 <= size_bytes <= MAX_PRIVATE_ARTIFACT_BLOB_BYTES:
            raise PrivateArtifactBlobStoreError(
                "blob metadata size is invalid"
            )
        if device < 0 or inode <= 0:
            raise PrivateArtifactBlobStoreError(
                "blob metadata filesystem identity is invalid"
            )
        if file_mode != PRIVATE_BLOB_MODE or link_count != 1:
            raise PrivateArtifactBlobStoreError(
                "blob metadata security attributes are invalid"
            )
        return cls(
            schema_version=schema_version,
            artifact_id=artifact_id,
            relative_filename=relative_filename,
            sha256=digest,
            size_bytes=size_bytes,
            device=device,
            inode=inode,
            file_mode=file_mode,
            link_count=link_count,
        )


@dataclass(frozen=True, slots=True)
class _PinnedBaseDirectory:
    parent_path: Path
    parent_fd: int
    parent_device: int
    parent_inode: int
    name: str
    fd: int
    device: int
    inode: int

    def assert_attached(self) -> None:
        parent_held = _safe_fstat(self.parent_fd, "blob base parent")
        try:
            parent_linked = os.stat(
                self.parent_path,
                follow_symlinks=False,
            )
            base_linked = os.stat(
                self.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PrivateArtifactBlobStoreError(
                "private blob base binding is unavailable"
            ) from exc
        base_held = _safe_fstat(self.fd, "private blob base")
        if (
            not stat.S_ISDIR(parent_held.st_mode)
            or not stat.S_ISDIR(parent_linked.st_mode)
            or int(parent_held.st_dev) != self.parent_device
            or int(parent_held.st_ino) != self.parent_inode
            or int(parent_linked.st_dev) != self.parent_device
            or int(parent_linked.st_ino) != self.parent_inode
        ):
            raise PrivateArtifactBlobStoreError(
                "blob base parent binding changed"
            )
        if (
            not stat.S_ISDIR(base_held.st_mode)
            or not stat.S_ISDIR(base_linked.st_mode)
            or int(base_held.st_dev) != self.device
            or int(base_held.st_ino) != self.inode
            or int(base_linked.st_dev) != self.device
            or int(base_linked.st_ino) != self.inode
            or stat.S_IMODE(base_held.st_mode) != PRIVATE_DIRECTORY_MODE
            or stat.S_IMODE(base_linked.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            raise PrivateArtifactBlobStoreError(
                "private blob base binding or mode changed"
            )


class PrivateArtifactBlobStore:
    """Immutable private bytes, deliberately without parsing or execution.

    ``base_dir`` is the one private directory selected by the owning runtime.
    Artifact IDs never become path components: each maps to one fixed hashed
    filename below the descriptor-pinned base directory.
    """

    def __init__(self, base_dir: Path) -> None:
        if not isinstance(base_dir, Path):
            raise TypeError("base_dir must be a pathlib.Path")
        if not base_dir.is_absolute():
            raise PrivateArtifactBlobStoreError(
                "private blob base_dir must be absolute"
            )
        if not hasattr(os, "O_NOFOLLOW"):
            raise PrivateArtifactBlobStoreError(
                "private blob storage requires O_NOFOLLOW"
            )
        if os.open not in os.supports_dir_fd:
            raise PrivateArtifactBlobStoreError(
                "private blob storage requires descriptor-relative open"
            )

        name = base_dir.name
        if (
            not name
            or name in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name)
        ):
            raise PrivateArtifactBlobStoreError(
                "private blob base directory name is invalid"
            )
        try:
            parent_path = base_dir.parent.resolve(strict=True)
        except OSError as exc:
            raise PrivateArtifactBlobStoreError(
                "private blob base parent is unavailable"
            ) from exc
        if not parent_path.is_dir():
            raise PrivateArtifactBlobStoreError(
                "private blob base parent must be a directory"
            )

        self._parent_path = parent_path
        self._base_path = parent_path / name
        self._base_name = name
        lock_key = str(self._base_path)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(lock_key, threading.RLock())

    def store(
        self,
        artifact_id: str,
        content: bytes,
        expected_sha256: str,
    ) -> BlobMetadata:
        """Store one immutable blob or recover an existing exact blob."""

        selected_id = _validated_artifact_id(artifact_id)
        expected_digest = _validated_sha256(expected_sha256)
        selected_content = _validated_content(content)
        actual_digest = hashlib.sha256(selected_content).hexdigest()
        if actual_digest != expected_digest:
            raise PrivateArtifactBlobStoreError(
                "artifact content digest mismatch"
            )
        filename = _filename_for(selected_id)

        with self._lock, self._pinned_base() as base:
            base.assert_attached()
            if self._target_exists(base, filename):
                existing_content, metadata = self._read_verified(
                    base,
                    selected_id,
                    expected_sha256=expected_digest,
                )
                if existing_content != selected_content:
                    raise PrivateArtifactBlobStoreError(
                        "existing artifact blob content conflicts"
                    )
                return metadata

            temporary_name = (
                ".pending_"
                + hashlib.sha256(
                    (selected_id + "\0" + expected_digest).encode("ascii")
                ).hexdigest()
                + "_"
                + os.urandom(16).hex()
            )
            temporary_created = False
            try:
                temporary_fd = os.open(
                    temporary_name,
                    _OPEN_CREATE_FLAGS,
                    PRIVATE_BLOB_MODE,
                    dir_fd=base.fd,
                )
                temporary_created = True
                try:
                    _write_all(temporary_fd, selected_content)
                    os.fchmod(temporary_fd, PRIVATE_BLOB_MODE)
                    os.fsync(temporary_fd)
                    temporary_identity = _safe_fstat(
                        temporary_fd,
                        "private artifact staging blob",
                    )
                    _assert_secure_blob_identity(
                        temporary_identity,
                        expected_size=len(selected_content),
                    )
                finally:
                    os.close(temporary_fd)

                base.assert_attached()
                try:
                    os.link(
                        temporary_name,
                        filename,
                        src_dir_fd=base.fd,
                        dst_dir_fd=base.fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # Another writer won publication. Its blob still has to be
                    # byte-for-byte identical before this call is idempotent.
                    pass
                else:
                    published = os.stat(
                        filename,
                        dir_fd=base.fd,
                        follow_symlinks=False,
                    )
                    if (
                        int(published.st_dev)
                        != int(temporary_identity.st_dev)
                        or int(published.st_ino)
                        != int(temporary_identity.st_ino)
                    ):
                        raise PrivateArtifactBlobStoreError(
                            "published blob identity changed"
                        )
                finally:
                    os.unlink(temporary_name, dir_fd=base.fd)
                    temporary_created = False
                    os.fsync(base.fd)
            finally:
                if temporary_created:
                    try:
                        os.unlink(temporary_name, dir_fd=base.fd)
                        os.fsync(base.fd)
                    except FileNotFoundError:
                        pass

            content_read, metadata = self._read_verified(
                base,
                selected_id,
                expected_sha256=expected_digest,
            )
            if content_read != selected_content:
                raise PrivateArtifactBlobStoreError(
                    "existing artifact blob content conflicts"
                )
            return metadata

    def read(
        self,
        artifact_id: str,
        expected_metadata: BlobMetadata | dict[str, Any],
    ) -> bytes:
        """Reopen and verify a blob against persisted identity and hash."""

        selected_id = _validated_artifact_id(artifact_id)
        metadata = (
            expected_metadata
            if isinstance(expected_metadata, BlobMetadata)
            else BlobMetadata.from_dict(expected_metadata)
        )
        # Revalidate dataclass instances too; frozen dataclasses may have been
        # created directly instead of through ``from_dict``.
        metadata = BlobMetadata.from_dict(metadata.to_dict())
        if metadata.artifact_id != selected_id:
            raise PrivateArtifactBlobStoreError(
                "blob metadata artifact binding mismatch"
            )

        with self._lock, self._pinned_base() as base:
            content, observed = self._read_verified(
                base,
                selected_id,
                expected_sha256=metadata.sha256,
                expected_metadata=metadata,
            )
            if observed != metadata:
                raise PrivateArtifactBlobStoreError(
                    "private artifact blob metadata changed"
                )
            return content

    def inspect(
        self,
        artifact_id: str,
        expected_sha256: str,
    ) -> BlobMetadata:
        """Recover metadata only when the caller already knows the hash."""

        selected_id = _validated_artifact_id(artifact_id)
        digest = _validated_sha256(expected_sha256)
        with self._lock, self._pinned_base() as base:
            _, metadata = self._read_verified(
                base,
                selected_id,
                expected_sha256=digest,
            )
            return metadata

    @contextmanager
    def _pinned_base(self) -> Iterator[_PinnedBaseDirectory]:
        parent_fd = -1
        base_fd = -1
        try:
            parent_fd = os.open(
                self._parent_path,
                _OPEN_DIRECTORY_FLAGS,
            )
            parent_identity = _safe_fstat(
                parent_fd,
                "private blob base parent",
            )
            parent_linked = os.stat(
                self._parent_path,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(parent_identity.st_mode)
                or not stat.S_ISDIR(parent_linked.st_mode)
                or int(parent_identity.st_dev)
                != int(parent_linked.st_dev)
                or int(parent_identity.st_ino)
                != int(parent_linked.st_ino)
            ):
                raise PrivateArtifactBlobStoreError(
                    "private blob base parent identity changed"
                )

            created = False
            try:
                os.mkdir(
                    self._base_name,
                    PRIVATE_DIRECTORY_MODE,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                pass
            else:
                created = True
                os.fsync(parent_fd)

            base_fd = os.open(
                self._base_name,
                _OPEN_DIRECTORY_FLAGS,
                dir_fd=parent_fd,
            )
            base_identity = _safe_fstat(base_fd, "private blob base")
            if created:
                os.fchmod(base_fd, PRIVATE_DIRECTORY_MODE)
                os.fsync(base_fd)
                base_identity = _safe_fstat(base_fd, "private blob base")
            base_linked = os.stat(
                self._base_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(base_identity.st_mode)
                or not stat.S_ISDIR(base_linked.st_mode)
                or int(base_identity.st_dev) != int(base_linked.st_dev)
                or int(base_identity.st_ino) != int(base_linked.st_ino)
                or stat.S_IMODE(base_identity.st_mode)
                != PRIVATE_DIRECTORY_MODE
                or stat.S_IMODE(base_linked.st_mode)
                != PRIVATE_DIRECTORY_MODE
            ):
                raise PrivateArtifactBlobStoreError(
                    "private blob base is not a mode-0700 real directory"
                )
            pinned = _PinnedBaseDirectory(
                parent_path=self._parent_path,
                parent_fd=parent_fd,
                parent_device=int(parent_identity.st_dev),
                parent_inode=int(parent_identity.st_ino),
                name=self._base_name,
                fd=base_fd,
                device=int(base_identity.st_dev),
                inode=int(base_identity.st_ino),
            )
            pinned.assert_attached()
            yield pinned
            pinned.assert_attached()
        except PrivateArtifactBlobStoreError:
            raise
        except OSError as exc:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob filesystem operation failed"
            ) from exc
        finally:
            if base_fd >= 0:
                os.close(base_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    @staticmethod
    def _target_exists(
        base: _PinnedBaseDirectory,
        filename: str,
    ) -> bool:
        try:
            identity = os.stat(
                filename,
                dir_fd=base.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        _assert_secure_blob_identity(identity)
        return True

    @staticmethod
    def _read_verified(
        base: _PinnedBaseDirectory,
        artifact_id: str,
        *,
        expected_sha256: str,
        expected_metadata: BlobMetadata | None = None,
    ) -> tuple[bytes, BlobMetadata]:
        filename = _filename_for(artifact_id)
        base.assert_attached()
        try:
            linked_before = os.stat(
                filename,
                dir_fd=base.fd,
                follow_symlinks=False,
            )
            fd = os.open(
                filename,
                _OPEN_READ_FLAGS,
                dir_fd=base.fd,
            )
        except FileNotFoundError as exc:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob does not exist"
            ) from exc
        except OSError as exc:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob cannot be opened securely"
            ) from exc

        try:
            opened_before = _safe_fstat(fd, "private artifact blob")
            _assert_same_file(linked_before, opened_before)
            _assert_secure_blob_identity(opened_before)
            content = _read_bounded(fd)
            opened_after = _safe_fstat(fd, "private artifact blob")
            _assert_unchanged_during_read(opened_before, opened_after)
            try:
                linked_after = os.stat(
                    filename,
                    dir_fd=base.fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise PrivateArtifactBlobStoreError(
                    "private artifact blob path binding disappeared"
                ) from exc
            _assert_same_file(linked_after, opened_after)
            _assert_secure_blob_identity(linked_after)
        finally:
            os.close(fd)
        base.assert_attached()

        if len(content) != int(opened_after.st_size):
            raise PrivateArtifactBlobStoreError(
                "private artifact blob size changed"
            )
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_sha256:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob digest mismatch"
            )
        metadata = _metadata_for(
            artifact_id,
            actual_digest,
            opened_after,
        )
        if expected_metadata is not None and metadata != expected_metadata:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob identity mismatch"
            )
        return content, metadata


def _validated_artifact_id(value: Any) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise PrivateArtifactBlobStoreError(
            "artifact_id must be a bounded identifier"
        )
    return value


def _validated_sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PrivateArtifactBlobStoreError(
            "expected_sha256 must be a lowercase SHA-256 digest"
        )
    return value


def _validated_content(value: Any) -> bytes:
    if type(value) is not bytes:
        raise TypeError("artifact content must be bytes")
    if not 1 <= len(value) <= MAX_PRIVATE_ARTIFACT_BLOB_BYTES:
        raise PrivateArtifactBlobStoreError(
            "artifact content exceeds the private blob byte budget"
        )
    return value


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrivateArtifactBlobStoreError(
            f"blob metadata {field} must be an integer"
        )
    return value


def _filename_for(artifact_id: str) -> str:
    return (
        "blob_"
        + hashlib.sha256(artifact_id.encode("ascii")).hexdigest()
        + ".bin"
    )


def _metadata_for(
    artifact_id: str,
    digest: str,
    identity: os.stat_result,
) -> BlobMetadata:
    return BlobMetadata(
        schema_version=PRIVATE_ARTIFACT_BLOB_SCHEMA_VERSION,
        artifact_id=artifact_id,
        relative_filename=_filename_for(artifact_id),
        sha256=digest,
        size_bytes=int(identity.st_size),
        device=int(identity.st_dev),
        inode=int(identity.st_ino),
        file_mode=stat.S_IMODE(identity.st_mode),
        link_count=int(identity.st_nlink),
    )


def _safe_fstat(fd: int, label: str) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError as exc:
        raise PrivateArtifactBlobStoreError(
            f"{label} identity is unavailable"
        ) from exc


def _assert_secure_blob_identity(
    identity: os.stat_result,
    *,
    expected_size: int | None = None,
) -> None:
    if not stat.S_ISREG(identity.st_mode):
        raise PrivateArtifactBlobStoreError(
            "private artifact blob must be a regular file"
        )
    if stat.S_IMODE(identity.st_mode) != PRIVATE_BLOB_MODE:
        raise PrivateArtifactBlobStoreError(
            "private artifact blob mode must be 0600"
        )
    if int(identity.st_nlink) != 1:
        raise PrivateArtifactBlobStoreError(
            "private artifact blob must have one filesystem link"
        )
    size = int(identity.st_size)
    if not 1 <= size <= MAX_PRIVATE_ARTIFACT_BLOB_BYTES:
        raise PrivateArtifactBlobStoreError(
            "private artifact blob size is invalid"
        )
    if expected_size is not None and size != expected_size:
        raise PrivateArtifactBlobStoreError(
            "private artifact blob size does not match content"
        )


def _assert_same_file(
    linked: os.stat_result,
    opened: os.stat_result,
) -> None:
    if (
        int(linked.st_dev) != int(opened.st_dev)
        or int(linked.st_ino) != int(opened.st_ino)
    ):
        raise PrivateArtifactBlobStoreError(
            "private artifact blob path binding changed"
        )


def _assert_unchanged_during_read(
    before: os.stat_result,
    after: os.stat_result,
) -> None:
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mode),
        int(before.st_nlink),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mode),
        int(after.st_nlink),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    )
    if before_identity != after_identity:
        raise PrivateArtifactBlobStoreError(
            "private artifact blob changed during read"
        )


def _read_bounded(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = MAX_PRIVATE_ARTIFACT_BLOB_BYTES + 1 - total
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_PRIVATE_ARTIFACT_BLOB_BYTES:
            raise PrivateArtifactBlobStoreError(
                "private artifact blob exceeds the byte budget"
            )


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("private artifact blob write made no progress")
        view = view[written:]
