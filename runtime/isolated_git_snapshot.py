from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit


GitObjectFormat = Literal["sha1", "sha256"]
GitSnapshotReason = Literal[
    "non_git",
    "unsafe_config",
    "unsupported_layout",
    "timeout",
    "drift",
    "probe_error",
]

_REVISION_PATTERNS: dict[GitObjectFormat, re.Pattern[str]] = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
}


class GitSnapshotUnavailable(RuntimeError):
    """An isolated workspace snapshot could not be established safely."""

    def __init__(self, reason_code: GitSnapshotReason) -> None:
        self.reason_code = reason_code
        super().__init__("isolated Git snapshot is unavailable")


@dataclass(frozen=True, slots=True)
class GitWorkspaceSnapshot:
    revision: str
    dirty: bool
    captured_at: str
    object_format: GitObjectFormat


@dataclass(frozen=True, slots=True)
class GitWorkspaceObservation:
    """Path-redacted workspace identity and bounded persisted summary.

    Paths are consumed only in memory. The public projection contains only
    category counts and a manifest digest; callers must never persist or
    return repository paths from this reader.
    """

    repository_root: str
    repo_id: str
    origin_digest: str
    full_ref: str
    revision: str
    dirty: bool
    category_counts: dict[str, int]
    manifest_digest: str
    captured_at: str
    object_format: GitObjectFormat
    # A deliberately tiny, path-free origin classification.  This is used
    # only when a caller wants to join an observation to the GitHub Actions
    # provider; arbitrary remotes must never be treated as GitHub bindings.
    # The default keeps test/fake observations source-compatible.
    origin_host: str = ""


@dataclass(frozen=True, slots=True)
class _SnapshotPass:
    revision: str
    dirty: bool
    object_format: GitObjectFormat
    status_output: str = ""
    category_counts: tuple[tuple[str, int], ...] = ()
    manifest_digest: str = ""


MAX_STATUS_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_FILE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_TOTAL_BYTES = 64 * 1024 * 1024


def capture_isolated_git_observation(
    repository_root: Path,
    *,
    timeout_seconds: float = 2.0,
    clock: Callable[[], datetime] | None = None,
) -> GitWorkspaceObservation:
    """Capture a stable identity plus path-redacted Git change summary.

    This is the only richer snapshot used by trusted cognition producers. It
    inherits the same synthetic-index, no-filter and two-pass drift checks as
    :func:`capture_isolated_git_snapshot`; status paths are consumed in
    memory, classified, and represented only by counts/digest.
    """

    reader = _IsolatedGitReader(
        repository_root=repository_root,
        timeout_seconds=timeout_seconds,
    )
    first_ref = reader.full_ref()
    first_origin = reader.origin_url()
    first = reader.capture_pass(include_status=True)
    second_ref = reader.full_ref()
    second_origin = reader.origin_url()
    second = reader.capture_pass(include_status=True)
    third_ref = reader.full_ref()
    third_origin = reader.origin_url()
    if (
        first != second
        or first_ref != second_ref
        or first_origin != second_origin
        or second_ref != third_ref
        or second_origin != third_origin
    ):
        raise GitSnapshotUnavailable("drift")
    full_ref = second_ref
    origin = second_origin
    repo_id = _repo_id_from_origin(origin)
    if not repo_id:
        raise GitSnapshotUnavailable("unsupported_layout")
    origin_digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
    categories = dict(first.category_counts)
    manifest_digest = first.manifest_digest
    origin_host = _canonical_github_origin_host(origin)
    observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return GitWorkspaceObservation(
        repository_root=str(reader.repository_root),
        repo_id=repo_id,
        origin_digest=origin_digest,
        full_ref=full_ref,
        revision=first.revision,
        dirty=first.dirty,
        category_counts=categories,
        manifest_digest=manifest_digest,
        captured_at=observed_at.astimezone(timezone.utc).isoformat(),
        object_format=first.object_format,
        origin_host=origin_host,
    )


def capture_isolated_git_snapshot(
    repository_root: Path,
    *,
    timeout_seconds: float = 2.0,
    clock: Callable[[], datetime] | None = None,
) -> GitWorkspaceSnapshot:
    """Capture an isolated read-only Git workspace snapshot.

    Repository configuration is never used for status evaluation. Tracked and
    untracked state is evaluated through a synthetic Git directory and
    temporary indexes, so the repository index cannot be refreshed or locked.
    Two matching passes are required; any partial result or drift fails closed.
    """

    reader = _IsolatedGitReader(
        repository_root=repository_root,
        timeout_seconds=timeout_seconds,
    )
    first = reader.capture_pass()
    second = reader.capture_pass()
    if first != second:
        raise GitSnapshotUnavailable("drift")
    observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return GitWorkspaceSnapshot(
        revision=first.revision,
        dirty=first.dirty,
        captured_at=observed_at.astimezone(timezone.utc).isoformat(),
        object_format=first.object_format,
    )


class _IsolatedGitReader:
    def __init__(self, *, repository_root: Path, timeout_seconds: float) -> None:
        try:
            self.repository_root = repository_root.resolve(strict=True)
        except OSError as exc:
            raise GitSnapshotUnavailable("non_git") from exc
        if not self.repository_root.is_dir():
            raise GitSnapshotUnavailable("non_git")
        self.timeout_seconds = max(0.2, min(float(timeout_seconds), 10.0))

    def capture_pass(self, *, include_status: bool = False) -> _SnapshotPass:
        root_text = self._git_output(
            ["rev-parse", "--show-toplevel"],
            failure_reason="non_git",
        )
        try:
            git_root = Path(root_text).resolve(strict=True)
        except OSError as exc:
            raise GitSnapshotUnavailable("unsupported_layout") from exc
        if git_root != self.repository_root:
            raise GitSnapshotUnavailable("unsupported_layout")
        if (git_root / ".gitmodules").exists():
            raise GitSnapshotUnavailable("unsupported_layout")

        object_format_raw = self._git_output(
            ["rev-parse", "--show-object-format"]
        )
        if object_format_raw not in _REVISION_PATTERNS:
            raise GitSnapshotUnavailable("unsupported_layout")
        object_format: GitObjectFormat = object_format_raw  # type: ignore[assignment]

        revision_before = self._revision(object_format)
        self._require_safe_repository_config()
        self._require_supported_index()
        index_path = self._resolved_git_path("index", require_file=True)
        objects_path = self._resolved_git_path("objects", require_dir=True)
        shared_index = self._shared_index_path()
        index_before = self._file_digest(index_path)
        shared_before = (
            self._file_digest(shared_index) if shared_index is not None else None
        )

        dirty_result = self._isolated_dirty_status(
            revision=revision_before,
            object_format=object_format,
            index_path=index_path,
            objects_path=objects_path,
            shared_index=shared_index,
            return_output=include_status,
        )
        if include_status:
            dirty, status_output = dirty_result
            categories, manifest_digest = _classify_status(
                status_output,
                repository_root=self.repository_root,
                index_digest=index_before,
            )
        else:
            dirty, status_output = bool(dirty_result), ""
            categories, manifest_digest = {}, ""

        revision_after = self._revision(object_format)
        index_after = self._file_digest(index_path)
        shared_after = (
            self._file_digest(shared_index) if shared_index is not None else None
        )
        self._require_safe_repository_config()
        self._require_supported_index()
        if (
            revision_after != revision_before
            or index_after != index_before
            or shared_after != shared_before
        ):
            raise GitSnapshotUnavailable("drift")
        return _SnapshotPass(
            revision=revision_before,
            dirty=dirty,
            object_format=object_format,
            status_output=status_output,
            category_counts=tuple(sorted(categories.items())),
            manifest_digest=manifest_digest,
        )

    def _isolated_dirty_status(
        self,
        *,
        revision: str,
        object_format: GitObjectFormat,
        index_path: Path,
        objects_path: Path,
        shared_index: Path | None,
        return_output: bool = False,
    ) -> bool | tuple[bool, str]:
        try:
            with tempfile.TemporaryDirectory(
                prefix="veyra-runtime-build-git-"
            ) as temporary:
                git_dir = Path(temporary) / "git"
                git_dir.mkdir()
                (git_dir / "refs").mkdir()
                source_index = git_dir / "source-index"
                fresh_index = git_dir / "fresh-index"
                shutil.copyfile(index_path, source_index)
                if shared_index is not None:
                    shutil.copyfile(shared_index, git_dir / shared_index.name)

                repository_format = 1 if object_format == "sha256" else 0
                config = (
                    "[core]\n"
                    f"\trepositoryFormatVersion = {repository_format}\n"
                    "\tbare = false\n"
                )
                if object_format == "sha256":
                    config += "[extensions]\n\tobjectFormat = sha256\n"
                (git_dir / "config").write_text(config, encoding="utf-8")
                (git_dir / "HEAD").write_text(
                    f"{revision}\n",
                    encoding="ascii",
                )

                isolated_env = self._sanitized_git_env()
                isolated_env.update(
                    GIT_DIR=str(git_dir),
                    GIT_OBJECT_DIRECTORY=str(objects_path),
                    GIT_WORK_TREE=str(self.repository_root),
                )
                source_env = dict(isolated_env)
                source_env["GIT_INDEX_FILE"] = str(source_index)
                staged = self._run(
                    [
                        *self._git_command_prefix(),
                        "diff-index",
                        "--cached",
                        "--quiet",
                        "--no-ext-diff",
                        "--no-textconv",
                        "HEAD",
                        "--",
                    ],
                    env=source_env,
                    allowed_returncodes={0, 1},
                )
                staged_paths = self._run(
                    [
                        *self._git_command_prefix(),
                        "diff-index",
                        "--cached",
                        "--name-status",
                        "-z",
                        "--no-ext-diff",
                        "--no-textconv",
                        "HEAD",
                        "--",
                    ],
                    env=source_env,
                )

                fresh_env = dict(isolated_env)
                fresh_env["GIT_INDEX_FILE"] = str(fresh_index)
                self._run(
                    [
                        *self._git_command_prefix(),
                        "read-tree",
                        "--reset",
                        "HEAD",
                    ],
                    env=fresh_env,
                )
                status_args = [
                    *self._git_command_prefix(),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                ]
                if return_output:
                    status_args.append("-z")
                worktree = self._run(status_args, env=fresh_env)
        except GitSnapshotUnavailable:
            raise
        except OSError as exc:
            raise GitSnapshotUnavailable("probe_error") from exc
        if len(worktree.stdout.encode("utf-8", "surrogateescape")) > MAX_STATUS_BYTES:
            raise GitSnapshotUnavailable("probe_error")
        if len(staged_paths.stdout.encode("utf-8", "surrogateescape")) > MAX_STATUS_BYTES:
            raise GitSnapshotUnavailable("probe_error")
        dirty = staged.returncode == 1 or bool(worktree.stdout.strip())
        if return_output:
            # ``-z`` makes paths unambiguous internally.  The output never
            # leaves this function and is immediately reduced to categories.
            return dirty, _normalize_staged_status(staged_paths.stdout) + worktree.stdout
        return dirty

    def full_ref(self) -> str:
        value = self._git_output(["symbolic-ref", "-q", "HEAD"])
        if not value.startswith("refs/heads/") or len(value) > 240:
            raise GitSnapshotUnavailable("unsupported_layout")
        return value

    def origin_url(self) -> str:
        value = self._git_output(["remote", "get-url", "--all", "origin"])
        urls = [line.strip() for line in value.splitlines() if line.strip()]
        if len(urls) != 1:
            raise GitSnapshotUnavailable("unsupported_layout")
        remote = urls[0]
        if any(char in remote for char in ("\x00", "\r", "\n")):
            raise GitSnapshotUnavailable("unsupported_layout")
        return remote


    def _require_safe_repository_config(self) -> None:
        attributes_path = self._resolved_git_path("info/attributes")
        if attributes_path.exists():
            raise GitSnapshotUnavailable("unsafe_config")
        filters = self._git_output(
            [
                "config",
                "--includes",
                "--name-only",
                "--get-regexp",
                r"^filter\.",
            ],
            allow_empty=True,
            allowed_returncodes={0, 1},
        )
        if filters:
            raise GitSnapshotUnavailable("unsafe_config")

    def _require_supported_index(self) -> None:
        flags = self._git_output(["ls-files", "-v"], allow_empty=True)
        for line in flags.splitlines():
            if not line:
                continue
            tag = line[0]
            if tag.islower() or tag.upper() == "S":
                raise GitSnapshotUnavailable("unsupported_layout")
        stage = self._git_output(["ls-files", "--stage"], allow_empty=True)
        if any(
            line.startswith("160000 ")
            for line in stage.splitlines()
            if line
        ):
            raise GitSnapshotUnavailable("unsupported_layout")

    def _revision(self, object_format: GitObjectFormat) -> str:
        value = self._git_output(["rev-parse", "--verify", "HEAD"]).lower()
        if not _REVISION_PATTERNS[object_format].fullmatch(value):
            raise GitSnapshotUnavailable("probe_error")
        return value

    def _shared_index_path(self) -> Path | None:
        value = self._git_output(
            ["rev-parse", "--shared-index-path"],
            allow_empty=True,
        )
        if not value:
            return None
        path = self._resolved_path(value)
        if not path.is_file():
            raise GitSnapshotUnavailable("unsupported_layout")
        return path

    def _resolved_git_path(
        self,
        name: str,
        *,
        require_file: bool = False,
        require_dir: bool = False,
    ) -> Path:
        value = self._git_output(["rev-parse", "--git-path", name])
        path = self._resolved_path(value)
        if require_file and not path.is_file():
            raise GitSnapshotUnavailable("unsupported_layout")
        if require_dir and not path.is_dir():
            raise GitSnapshotUnavailable("unsupported_layout")
        return path

    def _resolved_path(self, value: str) -> Path:
        selected = Path(value)
        if not selected.is_absolute():
            selected = self.repository_root / selected
        try:
            return selected.resolve(strict=False)
        except OSError as exc:
            raise GitSnapshotUnavailable("unsupported_layout") from exc

    @staticmethod
    def _file_digest(path: Path) -> str:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError as exc:
            raise GitSnapshotUnavailable("unsupported_layout") from exc

    def _git_output(
        self,
        args: list[str],
        *,
        allow_empty: bool = False,
        allowed_returncodes: set[int] | None = None,
        failure_reason: GitSnapshotReason = "probe_error",
    ) -> str:
        result = self._run(
            [
                *self._git_command_prefix(),
                "-C",
                str(self.repository_root),
                *args,
            ],
            env=self._sanitized_git_env(),
            allowed_returncodes=allowed_returncodes,
            failure_reason=failure_reason,
        )
        output = result.stdout.strip()
        if not allow_empty and not output:
            raise GitSnapshotUnavailable(failure_reason)
        return output

    def _run(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        allowed_returncodes: set[int] | None = None,
        failure_reason: GitSnapshotReason = "probe_error",
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                check=False,
                timeout=self.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitSnapshotUnavailable("timeout") from exc
        except (OSError, UnicodeError) as exc:
            raise GitSnapshotUnavailable(failure_reason) from exc
        if result.returncode not in (allowed_returncodes or {0}):
            raise GitSnapshotUnavailable(failure_reason)
        return result

    @staticmethod
    def _sanitized_git_env() -> dict[str, str]:
        git_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_env.update(
            GIT_ATTR_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            LC_ALL="C",
        )
        return git_env

    @staticmethod
    def _git_command_prefix() -> list[str]:
        return [
            "git",
            "--no-replace-objects",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.trustctime=true",
            "-c",
            "core.checkStat=default",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.symlinks=true",
            "-c",
            "core.ignoreCase=false",
            "--no-optional-locks",
        ]


def _repo_id_from_origin(value: str) -> str:
    remote = str(value or "").strip()
    if not remote:
        return ""
    if "://" in remote:
        parsed = urlsplit(remote)
        if (
            parsed.query
            or parsed.fragment
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port is not None
        ):
            return ""
        path = parsed.path
    elif "@" in remote and ":" in remote:
        _authority, path = remote.split(":", 1)
    else:
        path = remote
    parts = [item for item in path.replace("\\", "/").strip("/").split("/") if item]
    if len(parts) < 2:
        return ""
    name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[-2]) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return ""
    return f"{parts[-2]}/{name}".lower()


def _canonical_github_origin_host(value: str) -> str:
    """Return ``github.com`` only for a canonical GitHub remote.

    Repository identity parsing intentionally remains useful for local,
    non-GitHub remotes.  GitHub CI authorization is narrower: no alternate
    host, credentials, port, query, fragment, or path-like authority may
    enter that binding.  We return the host only so no remote path leaks into
    the durable observation record.
    """

    remote = str(value or "").strip()
    if not remote or any(char in remote for char in ("\x00", "\r", "\n")):
        return ""
    if "://" not in remote:
        # GitHub's canonical SSH shorthand.  Keep the authority exact; this
        # rejects look-alikes such as ``git@github.com.evil:...``.
        if not re.fullmatch(
            r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
            remote,
        ):
            return ""
        return "github.com"
    try:
        parsed = urlsplit(remote)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or hostname.lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
            parsed.path,
        )
    ):
        return ""
    return "github.com"


def _classify_status(
    status_output: str,
    *,
    repository_root: Path,
    index_digest: str,
) -> tuple[dict[str, int], str]:
    """Classify status and derive a bounded, path-private change identity.

    A path-only digest incorrectly treats two successive edits to the same
    file as one observation.  The manifest therefore binds the isolated Git
    index plus bounded content digests for current worktree entries.  Raw
    paths are consumed only while hashing and never leave this function.
    """

    paths: list[str] = []
    tokens = str(status_output or "").split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            continue
        status = token[:2]
        paths.append(token[3:])
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index < len(tokens) and tokens[index]:
                paths.append(tokens[index])
                index += 1

    unique_paths: set[str] = set()
    for path in paths:
        if not path:
            continue
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        unique_paths.add(normalized.lstrip("/"))
    counts = {"code": 0, "test": 0, "docs": 0, "config": 0, "other": 0}
    manifest = hashlib.sha256()
    manifest.update(b"index\0")
    manifest.update(index_digest.encode("ascii"))
    manifest.update(b"\0")
    total_content_bytes = 0
    for normalized in sorted(unique_paths):
        lowered = normalized.lower()
        parts = tuple(part for part in lowered.split("/") if part)
        basename = parts[-1] if parts else ""
        suffix = Path(basename).suffix
        if (
            parts[:1] in {("tests",), ("test",)}
            or "test" in basename
            or "/tests/" in f"/{lowered}/"
            or "/test/" in f"/{lowered}/"
        ):
            category = "test"
        elif (
            parts[:1] in {(".github",), ("config",), ("configs",)}
            or suffix in {".toml", ".ini", ".yaml", ".yml", ".json"}
            or basename in {"makefile", "dockerfile"}
        ):
            category = "config"
        elif suffix in {
            ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
            ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
        }:
            category = "code"
        elif (
            parts[:1] == ("docs",)
            or suffix in {".md", ".mdx", ".rst", ".adoc"}
        ):
            category = "docs"
        else:
            category = "other"
        counts[category] += 1
        # Digest is an internal identity; no path enters a persisted/public
        # projection.  It allows duplicate suppression without disclosure.
        manifest.update(category.encode("ascii"))
        manifest.update(b"\0")
        manifest.update(normalized.encode("utf-8", "surrogateescape"))
        manifest.update(b"\0")
        total_content_bytes += _update_content_manifest(
            manifest,
            repository_root=repository_root,
            relative_path=normalized,
            remaining_bytes=MAX_MANIFEST_TOTAL_BYTES - total_content_bytes,
        )
    return counts, manifest.hexdigest()


def _update_content_manifest(
    manifest: "hashlib._Hash",
    *,
    repository_root: Path,
    relative_path: str,
    remaining_bytes: int,
) -> int:
    """Hash one worktree entry without following links or escaping root."""

    parts = Path(relative_path).parts
    if (
        not parts
        or Path(relative_path).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise GitSnapshotUnavailable("unsupported_layout")
    candidate = repository_root.joinpath(*parts)
    # Validate every parent component before opening the leaf.  O_NOFOLLOW on
    # the final file alone does not stop ``pkg/file.py`` from escaping through
    # a symlinked ``pkg`` directory.
    parent = repository_root
    for component in parts[:-1]:
        parent = parent / component
        try:
            parent_meta = os.lstat(parent)
        except OSError as exc:
            raise GitSnapshotUnavailable("drift") from exc
        if stat.S_ISLNK(parent_meta.st_mode) or not stat.S_ISDIR(parent_meta.st_mode):
            raise GitSnapshotUnavailable("unsupported_layout")
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        manifest.update(b"missing\0")
        return 0
    except OSError as exc:
        raise GitSnapshotUnavailable("probe_error") from exc

    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(candidate).encode("utf-8", "surrogateescape")
        except OSError as exc:
            raise GitSnapshotUnavailable("drift") from exc
        if len(target) > 4096:
            raise GitSnapshotUnavailable("unsupported_layout")
        manifest.update(b"symlink\0")
        manifest.update(target)
        manifest.update(b"\0")
        return len(target)
    if stat.S_ISDIR(metadata.st_mode):
        # ``--untracked-files=all`` should enumerate files.  A directory can
        # still disappear between status and hashing; do not recurse outside
        # Git's explicit path set.
        manifest.update(b"directory\0")
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        raise GitSnapshotUnavailable("unsupported_layout")
    if metadata.st_size > MAX_MANIFEST_FILE_BYTES or metadata.st_size > remaining_bytes:
        raise GitSnapshotUnavailable("unsupported_layout")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    content = hashlib.sha256()
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != metadata.st_size:
                raise GitSnapshotUnavailable("drift")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                content.update(chunk)
            after = os.fstat(stream.fileno())
    except GitSnapshotUnavailable:
        raise
    except OSError as exc:
        raise GitSnapshotUnavailable("drift") from exc
    stable_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    stable_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stable_before != stable_after:
        raise GitSnapshotUnavailable("drift")
    manifest.update(b"file\0")
    manifest.update(str(before.st_size).encode("ascii"))
    manifest.update(b"\0")
    manifest.update(content.digest())
    manifest.update(b"\0")
    return int(before.st_size)


def _normalize_staged_status(value: str) -> str:
    """Convert ``diff-index --name-status -z`` to porcelain-like records."""

    tokens = str(value or "").split("\x00")
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        if index >= len(tokens) or not tokens[index]:
            break
        path = tokens[index]
        index += 1
        code = status[:1]
        normalized.append(f"{code}  {path}")
        if code in {"R", "C"} and index < len(tokens) and tokens[index]:
            normalized.append(f"{code}  {tokens[index]}")
            index += 1
    return "\x00".join(normalized) + ("\x00" if normalized else "")
