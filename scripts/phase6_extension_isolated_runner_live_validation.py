#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
)
from interface.extension_isolated_runner import (  # noqa: E402
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
    IsolatedRunnerBinding,
)
from interface.extension_source_check import (  # noqa: E402
    EXTENSION_SOURCE_CHECK_POLICY_REVISION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_PARSER_IDENTITY,
    EXTENSION_SOURCE_RULESET_DIGEST,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION  # noqa: E402
from runtime.trusted_isolated_runner import (  # noqa: E402
    DEFAULT_DOCKER_CONTEXT,
    TrustedIsolatedRunnerBackend,
)


PROFILE = "veyra-runner"
SENTINEL = Path("/tmp/veyra-phase6-candidate-executed")
RUNNER_STATE = (
    ROOT
    / "state"
    / "runtime"
    / "phase6_extension_isolated_runner_state.json"
)
ARTIFACT = (
    b"from pathlib import Path\n"
    b"Path('/tmp/veyra-phase6-candidate-executed').write_text('bad')\n"
    b"raise RuntimeError('candidate code must remain inert')\n"
)


def require(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def command(*argv: str, limit: int = 64 * 1024) -> bytes:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": str(Path.home()),
        },
    )
    if result.returncode != 0 or len(result.stdout) > limit:
        raise AssertionError(
            f"bounded validation command failed: {argv[0:3]!r}"
        )
    return result.stdout


def file_identity(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    return hashlib.sha256(payload).hexdigest()


def process_identity() -> tuple[str, ...]:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return ()
    result = subprocess.run(
        (pgrep, "-f", "openclaw"),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"},
    )
    if result.returncode not in (0, 1):
        raise AssertionError("OpenClaw process snapshot failed")
    return tuple(sorted(result.stdout.decode("ascii").split()))


def binding_for(status: Any) -> IsolatedRunnerBinding:
    require(
        status.engine_identity_digest is not None
        and status.image_id is not None
        and status.image_conformance_digest is not None,
        "discovered backend exposes exact private trust identities",
    )
    return IsolatedRunnerBinding(
        schema_version=ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
        run_id="extrun_0123456789abcdef01234567",
        check_id="extcheck_0123456789abcdef01234567",
        source_check_binding_digest="1" * 64,
        source_check_report_digest="2" * 64,
        source_check_stage="SOURCE_CHECK_PASSED",
        candidate_id="extspec_0123456789abcdef01234567",
        candidate_revision=1,
        artifact_id="extart_0123456789abcdef01234567",
        artifact_revision=1,
        artifact_envelope_digest="3" * 64,
        owner_scope_digest="4" * 64,
        extension_id="validation.trusted_isolated_runner",
        extension_version=1,
        spec_digest="5" * 64,
        artifact_sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        artifact_size_bytes=len(ARTIFACT),
        extension_policy_revision=EXTENSION_POLICY_REVISION,
        artifact_policy_revision=EXTENSION_ARTIFACT_POLICY_REVISION,
        source_check_policy_revision=EXTENSION_SOURCE_CHECK_POLICY_REVISION,
        source_checker_revision=EXTENSION_SOURCE_CHECKER_REVISION,
        source_parser_identity=EXTENSION_SOURCE_PARSER_IDENTITY,
        source_ruleset_digest=EXTENSION_SOURCE_RULESET_DIGEST,
        runner_policy_revision=status.runner_policy_revision,
        runner_policy_digest=status.runner_policy_digest,
        backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
        engine_identity_digest=status.engine_identity_digest,
        image_id=status.image_id,
        image_conformance_digest=status.image_conformance_digest,
        harness_revision=status.harness_revision,
        harness_digest=status.harness_digest,
    )


def assert_profile() -> dict[str, Any]:
    colima = shutil.which("colima")
    require(colima is not None, "Colima CLI is installed")
    assert colima is not None
    status = json.loads(
        command(colima, "status", "--profile", PROFILE, "--json")
    )
    require(
        status.get("runtime") == "docker"
        and status.get("driver") == "macOS Virtualization.Framework"
        and status.get("arch") == "aarch64"
        and status.get("cpu") == 2
        and status.get("memory") == 2 * 1024 * 1024 * 1024,
        "dedicated Colima runner profile has bounded VM resources",
        status,
    )
    config_path = Path.home() / ".colima" / PROFILE / "colima.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    require(
        isinstance(config, dict)
        and config.get("runtime") == "docker"
        and config.get("vmType") == "vz"
        and config.get("mounts") is None
        and config.get("portForwarder") == "none"
        and config.get("forwardAgent") is False
        and config.get("sshConfig") is False
        and isinstance(config.get("network"), dict)
        and config["network"].get("address") is False,
        "runner VM has no host share, reachable address, forwarding, or agent",
    )
    surface_probe = command(
        colima,
        "ssh",
        "--profile",
        PROFILE,
        "--",
        "sh",
        "-lc",
        (
            "for p in /Users /workspace /state /host /hostfs; do "
            "test ! -e \"$p\" || exit 17; done"
        ),
    )
    require(surface_probe == b"", "host surfaces are absent inside runner VM")
    return status


def main() -> int:
    assert_profile()
    docker = shutil.which("docker")
    require(docker is not None, "Docker CLI is installed")
    require(not SENTINEL.exists(), "candidate execution sentinel starts absent")
    assert docker is not None

    source_before = command(
        "git",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    state_before = file_identity(RUNNER_STATE)
    openclaw_before = process_identity()

    discovery = TrustedIsolatedRunnerBackend(
        docker_binary=docker,
        docker_context=DEFAULT_DOCKER_CONTEXT,
    ).status()
    require(
        discovery.availability == "unavailable"
        and discovery.reason_code == "conformance_not_certified"
        and not discovery.conformance_certified,
        "uncertified backend remains fail closed",
        discovery.canonical_dict(),
    )
    binding = binding_for(discovery)
    assert discovery.image_id is not None
    assert discovery.engine_identity_digest is not None
    assert discovery.image_conformance_digest is not None
    backend = TrustedIsolatedRunnerBackend(
        docker_binary=docker,
        docker_context=DEFAULT_DOCKER_CONTEXT,
        expected_image_id=discovery.image_id,
        expected_engine_identity_digest=discovery.engine_identity_digest,
        image_conformance_digest=discovery.image_conformance_digest,
        conformance_certified=True,
    )
    certified = backend.status()
    require(
        certified.availability == "available"
        and certified.reason_code == "ready"
        and certified.conformance_certified,
        "exact engine, image, implementation, harness, and policy certify",
        certified.canonical_dict(),
    )

    report = backend.run(artifact_bytes=ARTIFACT, binding=binding)
    require(
        report.probe_status == "passed"
        and report.isolated_runner_status == "passed"
        and report.candidate_execution_status == "not_started"
        and report.unit_checks_status == "not_started"
        and report.contract_checks_status == "not_started"
        and report.behavior_verification_status == "not_started"
        and not report.issue_codes
        and not any(report.authority.model_dump().values()),
        "real fixed harness passes without executing the inert candidate",
        report.canonical_dict(),
    )

    containers = command(
        docker,
        "--context",
        DEFAULT_DOCKER_CONTEXT,
        "ps",
        "-aq",
        "--filter",
        "label=ai.veyra.phase6=isolated-runner",
    )
    volumes = command(
        docker,
        "--context",
        DEFAULT_DOCKER_CONTEXT,
        "volume",
        "ls",
        "-q",
        "--filter",
        "label=ai.veyra.phase6=isolated-runner",
    )
    require(
        containers.strip() == b"" and volumes.strip() == b"",
        "real run leaves no labeled container or volume",
    )
    require(not SENTINEL.exists(), "candidate execution sentinel remains absent")
    require(
        source_before
        == command(
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        and state_before == file_identity(RUNNER_STATE)
        and openclaw_before == process_identity(),
        "workspace, runner state, and OpenClaw process set remain unchanged",
    )
    print(
        json.dumps(
            {
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "docker_context": DEFAULT_DOCKER_CONTEXT,
                "engine_identity_digest": certified.engine_identity_digest,
                "image_id": certified.image_id,
                "conformance_identity_digest": (
                    certified.image_conformance_digest
                ),
                "harness_digest": certified.harness_digest,
                "runner_policy_digest": certified.runner_policy_digest,
                "probe_status": report.probe_status,
                "candidate_execution_status": (
                    report.candidate_execution_status
                ),
                "authority_granted": False,
                "cleanup": "empty",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    print("phase6 extension isolated-runner live validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
