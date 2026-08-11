#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.build_identity import RuntimeBuildIdentity


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def main() -> int:
    identity = RuntimeBuildIdentity.capture(repository_root=ROOT)
    projection = identity.public_projection()
    expect(
        projection
        == {
            "schema_version": "veyra.runtime_build_identity.v1",
            "status": "available",
            "build_revision": identity.build_revision,
            "dirty_flag": identity.dirty_flag,
            "started_at": identity.started_at,
            "git_checked_on_request": False,
        },
        "public projection is immutable and contains no request-time Git work",
    )
    expect(
        isinstance(identity.build_revision, str)
        and len(identity.build_revision) >= 7,
        "a Git checkout exposes its loaded revision",
    )
    expect(
        isinstance(identity.dirty_flag, bool),
        "a Git checkout exposes a boolean dirty flag",
    )
    with tempfile.TemporaryDirectory() as temporary:
        unavailable = RuntimeBuildIdentity.capture(
            repository_root=Path(temporary)
        ).public_projection()
    expect(
        unavailable["status"] == "unavailable"
        and unavailable["build_revision"] is None
        and unavailable["dirty_flag"] is None
        and unavailable["git_checked_on_request"] is False,
        "a non-Git deployment degrades honestly without request-time fallback",
    )
    print("runtime build identity smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
