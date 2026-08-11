from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from runtime.isolated_git_snapshot import (
    GitSnapshotUnavailable,
    capture_isolated_git_snapshot,
)


_FULL_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PUBLIC_REASONS = frozenset(
    {
        "non_git",
        "unsafe_config",
        "unsupported_layout",
        "timeout",
        "drift",
        "probe_error",
    }
)


def _parse_bounded_aware_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class RuntimeBuildIdentity:
    """A frozen process-start observation of the checkout workspace.

    This is not an attestation that every imported byte came from the reported
    revision. Request handlers consume the immutable projection and never run
    Git or inspect the workspace.
    """

    build_revision: str | None
    dirty_flag: bool | None
    started_at: str
    captured_at: str | None = None
    unavailable_reason: str | None = None

    SCHEMA_VERSION = "veyra.runtime_build_identity.v2"

    @classmethod
    def capture(cls, *, repository_root: Path) -> "RuntimeBuildIdentity":
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            snapshot = capture_isolated_git_snapshot(repository_root)
        except GitSnapshotUnavailable as exc:
            return cls(
                build_revision=None,
                dirty_flag=None,
                started_at=started_at,
                captured_at=None,
                unavailable_reason=exc.reason_code,
            )
        return cls(
            build_revision=snapshot.revision,
            dirty_flag=snapshot.dirty,
            started_at=started_at,
            captured_at=snapshot.captured_at,
        )

    def public_projection(self) -> dict[str, object]:
        started_time = _parse_bounded_aware_time(self.started_at)
        captured_time = _parse_bounded_aware_time(self.captured_at)
        available = (
            isinstance(self.build_revision, str)
            and _FULL_REVISION.fullmatch(self.build_revision) is not None
            and isinstance(self.dirty_flag, bool)
            and started_time is not None
            and captured_time is not None
            and captured_time >= started_time
            and self.unavailable_reason is None
        )
        unavailable_reason = (
            self.unavailable_reason
            if isinstance(self.unavailable_reason, str)
            and self.unavailable_reason in _PUBLIC_REASONS
            else "probe_error"
        )
        return {
            "schema_version": self.SCHEMA_VERSION,
            "status": "available" if available else "unavailable",
            "build_revision": self.build_revision if available else None,
            "dirty_flag": self.dirty_flag if available else None,
            "started_at": (
                started_time.isoformat() if started_time is not None else None
            ),
            "captured_at": (
                captured_time.isoformat()
                if available and captured_time is not None
                else None
            ),
            "source": "startup_git_snapshot" if available else "unavailable",
            "loaded_code_attested": False,
            "unavailable_reason": None if available else unavailable_reason,
            "git_checked_on_request": False,
        }
