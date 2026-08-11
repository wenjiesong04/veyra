from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_GIT_REVISION = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class RuntimeBuildIdentity:
    """A process-start snapshot used to bind live evidence to loaded code.

    Git is intentionally consulted only while the process starts. Request
    handlers consume the immutable projection so pure status reads neither
    inspect the working tree nor depend on a Git executable.
    """

    build_revision: str | None
    dirty_flag: bool | None
    started_at: str

    SCHEMA_VERSION = "veyra.runtime_build_identity.v1"

    @classmethod
    def capture(cls, *, repository_root: Path) -> "RuntimeBuildIdentity":
        started_at = datetime.now(timezone.utc).isoformat()
        revision = cls._git_output(
            repository_root,
            "rev-parse",
            "--verify",
            "HEAD",
        )
        if revision is not None and not _GIT_REVISION.fullmatch(revision):
            revision = None

        dirty_output = cls._git_output(
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        return cls(
            build_revision=revision,
            dirty_flag=None if dirty_output is None else bool(dirty_output),
            started_at=started_at,
        )

    def public_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "status": "available" if self.build_revision else "unavailable",
            "build_revision": self.build_revision,
            "dirty_flag": self.dirty_flag,
            "started_at": self.started_at,
            "git_checked_on_request": False,
        }

    @staticmethod
    def _git_output(repository_root: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repository_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()
