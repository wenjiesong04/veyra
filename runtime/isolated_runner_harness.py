from __future__ import annotations

"""Fixed stdlib-only probe executed inside the Phase 6.2d container.

This file intentionally does not import, compile, evaluate, or execute the
candidate artifact.  The candidate is opened only as bytes for identity and
read-only-mount probes.  The sole stdout document contains bounded status
codes, never source, paths, environment values, or exception/log text.
"""

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import socket
import sys
from typing import Any


HARNESS_RESULT_SCHEMA_VERSION = (
    "veyra.phase6.fixed_isolation_probe_result.v1"
)
HARNESS_REVISION = "veyra.phase6.fixed_isolation_probe_harness.v1"
RUNNER_POLICY_DIGEST = (
    "e651519dd9b5301e3ac6e13195594ee1448dc3dd9a468fad5465837e6f9a4a5a"
)
CONTAINER_USER = 65_532
PIDS_LIMIT = 16
MEMORY_LIMIT_BYTES = 32 * 1024 * 1024
CPU_MILLIS = 500
NOFILE_LIMIT = 64
MAX_ARTIFACT_BYTES = 65_536
MAX_OUTPUT_BYTES = 8 * 1024

_ARTIFACT_PATH = Path("/input/artifact.bin")
_HARNESS_PATH = Path("/runner/harness.py")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECK_NAMES = (
    "artifact_identity",
    "harness_identity",
    "non_root",
    "rootfs_read_only",
    "input_read_only",
    "network_isolation",
    "secret_isolation",
    "host_surface_isolation",
    "resource_limits",
)
_ISSUE_BY_CHECK = {
    "artifact_identity": "artifact_identity_failed",
    "harness_identity": "harness_identity_failed",
    "non_root": "non_root_failed",
    "rootfs_read_only": "rootfs_read_only_failed",
    "input_read_only": "input_read_only_failed",
    "network_isolation": "network_isolation_failed",
    "secret_isolation": "secret_isolation_failed",
    "host_surface_isolation": "host_surface_isolation_failed",
    "resource_limits": "resource_limits_failed",
}
_SAFE_IMAGE_METADATA_ENV = frozenset(
    {
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_PIP_VERSION",
        "PYTHON_SETUPTOOLS_VERSION",
        "PYTHON_GET_PIP_URL",
        "PYTHON_GET_PIP_SHA256",
    }
)
_SECRET_ENV_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "API_KEY",
    "AUTH_KEY",
)


def _sha256_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(8192)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                raise ValueError("byte budget exceeded")
            digest.update(block)
    return digest.hexdigest(), size


def _path_is_on_read_only_mount(path: str) -> bool:
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeError):
        return False
    selected_mount = ""
    selected_read_only = False
    for line in lines:
        before, separator, _after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        if len(fields) < 6:
            continue
        mountpoint = fields[4]
        if not (
            path == mountpoint
            or path.startswith(mountpoint.rstrip("/") + "/")
        ):
            continue
        if len(mountpoint) >= len(selected_mount):
            selected_mount = mountpoint
            selected_read_only = "ro" in fields[5].split(",")
    return bool(selected_mount) and selected_read_only


def _write_is_blocked(path: Path) -> bool:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError:
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        path.unlink()
    except OSError:
        pass
    return False


def _append_is_blocked(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError:
        return True
    os.close(descriptor)
    return False


def _network_is_blocked() -> bool:
    try:
        interfaces = [name for _index, name in socket.if_nameindex()]
    except OSError:
        return False
    if any(name != "lo" for name in interfaces):
        return False
    blocked_errors = {
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.EACCES,
        errno.EPERM,
    }
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        result = probe.connect_ex(("1.1.1.1", 443))
    except OSError as exc:
        return exc.errno in blocked_errors
    finally:
        probe.close()
    return result in blocked_errors


def _secrets_absent() -> bool:
    for name in os.environ:
        selected = name.upper()
        if selected in _SAFE_IMAGE_METADATA_ENV:
            continue
        if any(marker in selected for marker in _SECRET_ENV_MARKERS):
            return False
    return True


def _host_surfaces_absent() -> bool:
    forbidden = (
        "/Users",
        "/workspace",
        "/repo",
        "/state",
        "/host",
        "/hostfs",
        "/var/run/docker.sock",
    )
    return not any(os.path.lexists(path) for path in forbidden)


def _bounded_number(path: str, *, maximum: int) -> bool:
    try:
        value = Path(path).read_text(
            encoding="ascii",
            errors="strict",
        ).strip()
        return value != "max" and 0 < int(value) <= maximum
    except (OSError, UnicodeError, ValueError):
        return False


def _cpu_v2_bounded() -> bool:
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text(
            encoding="ascii",
            errors="strict",
        ).strip()
        quota_text, period_text = raw.split()
        if quota_text == "max":
            return False
        quota = int(quota_text)
        period = int(period_text)
        return (
            quota > 0
            and period > 0
            and quota * 1000 <= CPU_MILLIS * period
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _cpu_v1_bounded() -> bool:
    try:
        quota = int(
            Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text(
                encoding="ascii",
                errors="strict",
            ).strip()
        )
        period = int(
            Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(
                encoding="ascii",
                errors="strict",
            ).strip()
        )
        return (
            quota > 0
            and period > 0
            and quota * 1000 <= CPU_MILLIS * period
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _resource_limits_bounded() -> bool:
    nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
    nproc = resource.getrlimit(resource.RLIMIT_NPROC)
    if (
        nofile[0] < 0
        or nofile[1] < 0
        or nofile[0] > NOFILE_LIMIT
        or nofile[1] > NOFILE_LIMIT
        or nproc[0] < 0
        or nproc[1] < 0
        or nproc[0] > PIDS_LIMIT
        or nproc[1] > PIDS_LIMIT
    ):
        return False

    v2 = Path("/sys/fs/cgroup/cgroup.controllers").exists()
    if v2:
        return (
            _bounded_number(
                "/sys/fs/cgroup/pids.max",
                maximum=PIDS_LIMIT,
            )
            and _bounded_number(
                "/sys/fs/cgroup/memory.max",
                maximum=MEMORY_LIMIT_BYTES,
            )
            and _cpu_v2_bounded()
        )
    return (
        _bounded_number(
            "/sys/fs/cgroup/pids/pids.max",
            maximum=PIDS_LIMIT,
        )
        and _bounded_number(
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            maximum=MEMORY_LIMIT_BYTES,
        )
        and _cpu_v1_bounded()
    )


def _probe(
    *,
    binding_digest: str,
    expected_artifact_digest: str,
    expected_artifact_size: int,
    expected_harness_digest: str,
    expected_policy_digest: str,
) -> dict[str, Any]:
    artifact_digest, artifact_size = _sha256_file(
        _ARTIFACT_PATH,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    harness_digest, _harness_size = _sha256_file(
        _HARNESS_PATH,
        max_bytes=128 * 1024,
    )
    checks = {
        "artifact_identity": (
            artifact_digest == expected_artifact_digest
            and artifact_size == expected_artifact_size
        ),
        "harness_identity": (
            harness_digest == expected_harness_digest
            and expected_policy_digest == RUNNER_POLICY_DIGEST
        ),
        "non_root": (
            os.geteuid() == CONTAINER_USER
            and os.getegid() == CONTAINER_USER
        ),
        "rootfs_read_only": (
            _path_is_on_read_only_mount("/")
            and _write_is_blocked(Path("/.veyra-isolation-probe"))
        ),
        "input_read_only": (
            _path_is_on_read_only_mount("/input/artifact.bin")
            and _append_is_blocked(_ARTIFACT_PATH)
        ),
        "network_isolation": _network_is_blocked(),
        "secret_isolation": _secrets_absent(),
        "host_surface_isolation": _host_surfaces_absent(),
        "resource_limits": _resource_limits_bounded(),
    }
    issue_codes = [
        _ISSUE_BY_CHECK[name]
        for name in _CHECK_NAMES
        if not checks[name]
    ]
    return {
        "schema_version": HARNESS_RESULT_SCHEMA_VERSION,
        "harness_revision": HARNESS_REVISION,
        "binding_digest": binding_digest,
        "artifact_sha256": artifact_digest,
        "artifact_size_bytes": artifact_size,
        "harness_digest": harness_digest,
        "runner_policy_digest": expected_policy_digest,
        "probe_status": "passed" if not issue_codes else "failed",
        "checks": {
            name: "passed" if checks[name] else "failed"
            for name in _CHECK_NAMES
        },
        "issue_codes": issue_codes,
    }


def _emit(payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("output budget exceeded")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    selected = list(sys.argv[1:] if argv is None else argv)
    if len(selected) != 5:
        return 2
    (
        binding_digest,
        artifact_digest,
        artifact_size_text,
        harness_digest,
        policy_digest,
    ) = selected
    if not all(
        _DIGEST.fullmatch(value)
        for value in (
            binding_digest,
            artifact_digest,
            harness_digest,
            policy_digest,
        )
    ):
        return 2
    try:
        artifact_size = int(artifact_size_text)
    except ValueError:
        return 2
    if not 1 <= artifact_size <= MAX_ARTIFACT_BYTES:
        return 2
    try:
        result = _probe(
            binding_digest=binding_digest,
            expected_artifact_digest=artifact_digest,
            expected_artifact_size=artifact_size,
            expected_harness_digest=harness_digest,
            expected_policy_digest=policy_digest,
        )
        _emit(result)
    except Exception:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HARNESS_RESULT_SCHEMA_VERSION",
    "HARNESS_REVISION",
    "main",
]
