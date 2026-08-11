from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal


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
class _SnapshotPass:
    revision: str
    dirty: bool
    object_format: GitObjectFormat


def capture_isolated_git_snapshot(
    repository_root: Path,
    *,
    timeout_seconds: float = 2.0,
    clock: Callable[[], datetime] | None = None,
) -> GitWorkspaceSnapshot:
    """Capture a bounded read-only Git workspace snapshot.

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

    def capture_pass(self) -> _SnapshotPass:
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

        dirty = self._isolated_dirty_status(
            revision=revision_before,
            object_format=object_format,
            index_path=index_path,
            objects_path=objects_path,
            shared_index=shared_index,
        )

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
        )

    def _isolated_dirty_status(
        self,
        *,
        revision: str,
        object_format: GitObjectFormat,
        index_path: Path,
        objects_path: Path,
        shared_index: Path | None,
    ) -> bool:
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
                worktree = self._run(
                    [
                        *self._git_command_prefix(),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=normal",
                        "--ignore-submodules=all",
                    ],
                    env=fresh_env,
                )
        except GitSnapshotUnavailable:
            raise
        except OSError as exc:
            raise GitSnapshotUnavailable("probe_error") from exc
        return staged.returncode == 1 or bool(worktree.stdout.strip())

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
                check=False,
                timeout=self.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitSnapshotUnavailable("timeout") from exc
        except OSError as exc:
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
