#!/usr/bin/env python3
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_isolated_runner import (  # noqa: E402
    ISOLATED_RUNNER_CONTAINER_USER,
    ISOLATED_RUNNER_CPU_MILLIS,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_MEMORY_BYTES,
    ISOLATED_RUNNER_NOFILE_LIMIT,
    ISOLATED_RUNNER_PIDS_LIMIT,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_TMPFS_BYTES,
    ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS,
    MAX_ISOLATED_RUNNER_STDOUT_BYTES,
    IsolatedRunnerBinding,
    parse_isolated_runner_binding,
)
from runtime.trusted_isolated_runner import (  # noqa: E402
    DEFAULT_DOCKER_CONTEXT,
    DEFAULT_RUNNER_IMAGE,
    HARNESS_RESULT_SCHEMA_VERSION,
    IsolatedRunnerCommandResult,
    TrustedIsolatedRunnerBackend,
    TrustedIsolatedRunnerUnavailableError,
)
from scripts.phase6_extension_isolated_runner_contract_smoke import (  # noqa: E402
    valid_binding,
)


IMAGE_ID = "sha256:" + "b" * 64
SERVER_VERSION = "26.1.4"
FIXED_NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
NONCE = "0123456789abcdef0123456789abcdef"
STAGING_ID = "a" * 64
PROBE_ID = "c" * 64
ARTIFACT = (
    b"from pathlib import Path\n"
    b"Path('/tmp/CANDIDATE_MUST_NOT_EXECUTE').write_text('bad')\n"
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def require(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> BaseException:
    try:
        call()
    except errors as exc:
        print(f"PASS {label}")
        return exc
    raise AssertionError(f"{label}: call did not fail closed")


def json_result(value: Any) -> IsolatedRunnerCommandResult:
    return IsolatedRunnerCommandResult(
        returncode=0,
        stdout=json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )


class ScriptedDocker:
    """Deterministic Docker CLI protocol double; it never starts Docker."""

    def __init__(
        self,
        *,
        docker_binary: Path,
        inspect_mutation: str | None = None,
        start_mode: str = "passed",
        context_endpoint: str = "unix:///private/runner/docker.sock",
        engine_id: str = "runner-engine-fixture",
        security_options: tuple[str, ...] = (
            "name=seccomp,profile=builtin",
            "name=cgroupns",
        ),
    ) -> None:
        self.docker_binary = str(docker_binary.resolve(strict=True))
        self.inspect_mutation = inspect_mutation
        self.start_mode = start_mode
        self.context_endpoint = context_endpoint
        self.engine_id = engine_id
        self.security_options = security_options
        self.calls: list[tuple[tuple[str, ...], float, int]] = []
        self.probe_command: tuple[str, ...] | None = None
        self.volume_name: str | None = None

    def __call__(
        self,
        argv: tuple[str, ...],
        timeout_seconds: float,
        stdout_limit: int,
    ) -> IsolatedRunnerCommandResult:
        self.calls.append((argv, timeout_seconds, stdout_limit))
        if argv == (
            self.docker_binary,
            "context",
            "inspect",
            DEFAULT_DOCKER_CONTEXT,
            "--format={{json .Endpoints.docker.Host}}",
        ):
            return json_result(self.context_endpoint)
        if argv[:3] != (
            self.docker_binary,
            "--context",
            DEFAULT_DOCKER_CONTEXT,
        ):
            raise AssertionError(f"untrusted Docker prefix: {argv!r}")
        command = argv[3:]
        if command[:2] == ("version", "--format={{json .Server.Version}}"):
            return json_result(SERVER_VERSION)
        if command[:1] == ("info",):
            require(
                len(command) == 2
                and command[1].startswith("--format={")
                and '"security_options":' in command[1]
                and '"kernel_version":' in command[1],
                "engine info requests the fixed security and daemon fact set",
                command,
            )
            return json_result(
                {
                    "id": self.engine_id,
                    "security_options": list(self.security_options),
                    "operating_system": "Docker Desktop",
                    "architecture": "aarch64",
                    "driver": "overlay2",
                    "cgroup_driver": "cgroupfs",
                    "cgroup_version": "2",
                    "kernel_version": "6.10.0-runner",
                }
            )
        if command[:3] == (
            "image",
            "inspect",
            "--format={{json .Id}}",
        ):
            require(
                command[3:] == (DEFAULT_RUNNER_IMAGE,),
                "image inspect is pinned to the immutable reference",
                command,
            )
            return json_result(IMAGE_ID)
        if command[:2] == ("volume", "create"):
            self.volume_name = command[-1]
            return IsolatedRunnerCommandResult(
                returncode=0,
                stdout=(self.volume_name + "\n").encode("ascii"),
            )
        if command[:1] == ("create",):
            return self._create(command)
        if command[:2] == ("cp", "--quiet"):
            return IsolatedRunnerCommandResult(returncode=0, stdout=b"")
        if command[:2] == ("container", "inspect"):
            return self._inspect(command)
        if command[:2] == ("start", "--attach"):
            return self._start(command)
        if command[:1] in {("kill",), ("rm",)}:
            return IsolatedRunnerCommandResult(returncode=0, stdout=b"")
        if command[:2] == ("volume", "rm"):
            return IsolatedRunnerCommandResult(returncode=0, stdout=b"")
        raise AssertionError(f"unexpected Docker command: {command!r}")

    def _create(
        self,
        command: tuple[str, ...],
    ) -> IsolatedRunnerCommandResult:
        cidfile_argument = next(
            item for item in command if item.startswith("--cidfile=")
        )
        cidfile = Path(cidfile_argument.partition("=")[2])
        is_staging = command[-1] == "/bin/false"
        container_id = STAGING_ID if is_staging else PROBE_ID
        cidfile.write_text(container_id + "\n", encoding="ascii")
        if not is_staging:
            self.probe_command = command
        return IsolatedRunnerCommandResult(
            returncode=0,
            stdout=(container_id + "\n").encode("ascii"),
        )

    def _inspect(
        self,
        command: tuple[str, ...],
    ) -> IsolatedRunnerCommandResult:
        require(
            command[-1] == PROBE_ID,
            "container inspect is bound to the exact created ID",
            command,
        )
        template = command[2]
        if template == "--format={{json .Image}}":
            return json_result(IMAGE_ID)
        if template == "--format={{json .Config}}":
            return json_result(self._config())
        if template == "--format={{json .HostConfig}}":
            return json_result(self._host_config())
        if template == "--format={{json .Mounts}}":
            return json_result(self._mounts())
        raise AssertionError(f"unexpected inspect template: {template!r}")

    def _config(self) -> dict[str, Any]:
        if self.probe_command is None:
            raise AssertionError("probe create command was not observed")
        image_index = self.probe_command.index(IMAGE_ID)
        fixed_command = list(self.probe_command[image_index + 1 :])
        environment = [
            (
                "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "LANG=C.UTF-8",
            "GPG_KEY=A035C8C19219BA821ECEA86B64E628F8D684696D",
            "PYTHON_VERSION=3.11.14",
            (
                "PYTHON_SHA256="
                "8d3ed8ec5c88c1c95f5e558612a725450d2452813ddad5e58fdb1a53b1209b78"
            ),
        ]
        if self.inspect_mutation == "secret_environment":
            environment.append("VEYRA_API_KEY=PRIVATE_SECRET_SENTINEL")
        if self.inspect_mutation == "caller_argv":
            fixed_command.append("PRIVATE_CALLER_ARGV_SENTINEL")
        return {
            "User": (
                f"{ISOLATED_RUNNER_CONTAINER_USER}:"
                f"{ISOLATED_RUNNER_CONTAINER_USER}"
            ),
            "WorkingDir": "/runner",
            "Cmd": fixed_command,
            "Entrypoint": None,
            "ExposedPorts": None,
            "Env": environment,
        }

    def _host_config(self) -> dict[str, Any]:
        if self.volume_name is None:
            raise AssertionError("volume was not created")
        network = "host" if self.inspect_mutation == "network" else "none"
        binds: list[str] = []
        if self.inspect_mutation == "host_bind":
            binds = ["/Users/private:/host:ro"]
        if self.inspect_mutation == "api_socket":
            binds = [
                "/var/run/docker.sock:/var/run/docker.sock:rw"
            ]
        security_options = [
            "no-new-privileges=true",
            "seccomp=builtin",
        ]
        if self.inspect_mutation == "security_opt":
            security_options.append("label=disable")
        return {
            "NetworkMode": network,
            "IpcMode": "none",
            "CgroupnsMode": "private",
            "PidMode": "",
            "UTSMode": "",
            "UsernsMode": "",
            "Cgroup": "",
            "CgroupParent": "",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "AutoRemove": False,
            "PublishAllPorts": False,
            "PortBindings": {},
            "PidsLimit": ISOLATED_RUNNER_PIDS_LIMIT,
            "Memory": ISOLATED_RUNNER_MEMORY_BYTES,
            "MemorySwap": ISOLATED_RUNNER_MEMORY_BYTES,
            "NanoCpus": ISOLATED_RUNNER_CPU_MILLIS * 1_000_000,
            "CapDrop": ["ALL"],
            "CapAdd": (
                ["SYS_ADMIN"]
                if self.inspect_mutation == "cap_add"
                else None
            ),
            "SecurityOpt": security_options,
            "LogConfig": {"Type": "none"},
            "Binds": binds,
            "VolumesFrom": None,
            "Links": None,
            "ExtraHosts": (
                ["host.docker.internal:host-gateway"]
                if self.inspect_mutation == "extra_hosts"
                else None
            ),
            "GroupAdd": None,
            "Dns": None,
            "DnsOptions": [],
            "DnsSearch": [],
            "Devices": [],
            "DeviceCgroupRules": None,
            "DeviceRequests": [],
            "Sysctls": (
                {"net.ipv4.ip_forward": "1"}
                if self.inspect_mutation == "sysctls"
                else None
            ),
            "Runtime": (
                "host-runtime"
                if self.inspect_mutation == "runtime"
                else "runc"
            ),
            "Isolation": "",
            "OomKillDisable": False,
            "RestartPolicy": {"Name": "no"},
            "MaskedPaths": [
                "/proc/kcore",
                "/proc/keys",
                "/sys/firmware",
            ],
            "ReadonlyPaths": ["/proc/sys"],
            "Ulimits": [
                {"Name": "core", "Soft": 0, "Hard": 0},
                {
                    "Name": "nofile",
                    "Soft": ISOLATED_RUNNER_NOFILE_LIMIT,
                    "Hard": ISOLATED_RUNNER_NOFILE_LIMIT,
                },
                {
                    "Name": "nproc",
                    "Soft": ISOLATED_RUNNER_PIDS_LIMIT,
                    "Hard": ISOLATED_RUNNER_PIDS_LIMIT,
                },
            ],
            "Tmpfs": {
                "/tmp": (
                    "rw,noexec,nosuid,nodev,"
                    f"size={ISOLATED_RUNNER_TMPFS_BYTES},mode=700,"
                    f"uid={ISOLATED_RUNNER_CONTAINER_USER},"
                    f"gid={ISOLATED_RUNNER_CONTAINER_USER}"
                )
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": self.volume_name,
                    "Target": "/input",
                    "ReadOnly": True,
                    "VolumeOptions": {"Subpath": "input"},
                },
                {
                    "Type": "volume",
                    "Source": self.volume_name,
                    "Target": "/runner",
                    "ReadOnly": True,
                    "VolumeOptions": {"Subpath": "harness"},
                },
            ],
        }

    def _mounts(self) -> list[dict[str, Any]]:
        if self.volume_name is None:
            raise AssertionError("volume was not created")
        return [
            {
                "Type": "volume",
                "Name": self.volume_name,
                "Destination": "/input",
                "RW": False,
            },
            {
                "Type": "volume",
                "Name": self.volume_name,
                "Destination": "/runner",
                "RW": False,
            },
        ]

    def _start(
        self,
        command: tuple[str, ...],
    ) -> IsolatedRunnerCommandResult:
        require(
            command == ("start", "--attach", PROBE_ID),
            "only the exact conformed probe container is started",
            command,
        )
        if self.start_mode == "timeout":
            return IsolatedRunnerCommandResult(
                returncode=None,
                stdout=b"",
                timed_out=True,
            )
        if self.start_mode == "output_limit":
            return IsolatedRunnerCommandResult(
                returncode=None,
                stdout=b"x" * (MAX_ISOLATED_RUNNER_STDOUT_BYTES + 1),
                output_limit_exceeded=True,
            )
        if self.start_mode == "invalid_output":
            return IsolatedRunnerCommandResult(
                returncode=0,
                stdout=b"PRIVATE_LOG_SENTINEL\n",
            )
        if self.start_mode == "exit_nonzero":
            return IsolatedRunnerCommandResult(returncode=7, stdout=b"")
        if self.probe_command is None:
            raise AssertionError("probe create command was not observed")
        image_index = self.probe_command.index(IMAGE_ID)
        harness_command = self.probe_command[image_index + 1 :]
        result = {
            "schema_version": HARNESS_RESULT_SCHEMA_VERSION,
            "harness_revision": ISOLATED_RUNNER_HARNESS_REVISION,
            "binding_digest": harness_command[4],
            "artifact_sha256": harness_command[5],
            "artifact_size_bytes": int(harness_command[6]),
            "harness_digest": harness_command[7],
            "runner_policy_digest": harness_command[8],
            "probe_status": "passed",
            "checks": {
                "artifact_identity": "passed",
                "harness_identity": "passed",
                "non_root": "passed",
                "rootfs_read_only": "passed",
                "input_read_only": "passed",
                "network_isolation": "passed",
                "secret_isolation": "passed",
                "host_surface_isolation": "passed",
                "resource_limits": "passed",
            },
            "issue_codes": [],
        }
        return IsolatedRunnerCommandResult(
            returncode=0,
            stdout=(
                json.dumps(
                    result,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii"),
        )

    def commands(self) -> list[tuple[str, ...]]:
        return [
            (
                argv[3:]
                if argv[1:3] == ("--context", DEFAULT_DOCKER_CONTEXT)
                else argv[1:]
            )
            for argv, _timeout, _limit in self.calls
        ]


def make_fake_docker(root: Path) -> Path:
    path = root / "docker-fixture"
    path.write_bytes(b"#!/bin/sh\nexit 99\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def discover_trust(
    docker_binary: Path,
    scripted: ScriptedDocker,
) -> tuple[str, str, str]:
    discovery = TrustedIsolatedRunnerBackend(
        docker_binary=str(docker_binary),
        command_runner=scripted,
        nonce_factory=lambda: NONCE,
        now=lambda: FIXED_NOW,
    ).status()
    expect(
        discovery.availability == "unavailable"
        and discovery.reason_code == "conformance_not_certified"
        and discovery.engine_identity_digest is not None
        and discovery.image_id == IMAGE_ID
        and discovery.image_conformance_digest is not None
        and not any(discovery.authority.model_dump().values()),
        "uncertified discovered identity remains fail-closed",
        discovery.canonical_dict(),
    )
    assert discovery.engine_identity_digest is not None
    assert discovery.image_conformance_digest is not None
    return (
        discovery.engine_identity_digest,
        discovery.image_id,
        discovery.image_conformance_digest,
    )


def trusted_backend(
    docker_binary: Path,
    scripted: ScriptedDocker,
    *,
    engine_digest: str,
    image_id: str,
    conformance_digest: str,
) -> TrustedIsolatedRunnerBackend:
    return TrustedIsolatedRunnerBackend(
        docker_binary=str(docker_binary),
        expected_image_id=image_id,
        expected_engine_identity_digest=engine_digest,
        image_conformance_digest=conformance_digest,
        conformance_certified=True,
        command_runner=scripted,
        nonce_factory=lambda: NONCE,
        now=lambda: FIXED_NOW,
    )


def bound_artifact(
    status: Any,
) -> IsolatedRunnerBinding:
    payload = valid_binding().canonical_dict()
    payload.update(
        {
            "artifact_sha256": hashlib.sha256(ARTIFACT).hexdigest(),
            "artifact_size_bytes": len(ARTIFACT),
            "engine_identity_digest": status.engine_identity_digest,
            "image_id": status.image_id,
            "image_conformance_digest": (
                status.image_conformance_digest
            ),
            "harness_digest": status.harness_digest,
        }
    )
    return parse_isolated_runner_binding(payload)


def assert_fixed_commands(
    scripted: ScriptedDocker,
    binding: IsolatedRunnerBinding,
) -> None:
    commands = scripted.commands()
    expect(
        (
            "context",
            "inspect",
            DEFAULT_DOCKER_CONTEXT,
            "--format={{json .Endpoints.docker.Host}}",
        )
        in commands
        and ("version", "--format={{json .Server.Version}}") in commands
        and any(command[:1] == ("info",) for command in commands)
        and (
            "image",
            "inspect",
            "--format={{json .Id}}",
            DEFAULT_RUNNER_IMAGE,
        )
        in commands,
        "status binds context endpoint, server facts, and immutable image",
        commands,
    )
    probe_creates = [
        command
        for command in commands
        if command[:1] == ("create",)
        and "/runner/harness.py" in command
    ]
    expect(
        len(probe_creates) == 1,
        "one fixed probe container is created",
        commands,
    )
    probe = probe_creates[0]
    expected_tail = (
        IMAGE_ID,
        "/usr/local/bin/python3",
        "-I",
        "-B",
        "/runner/harness.py",
        binding.binding_digest(),
        binding.artifact_sha256,
        str(binding.artifact_size_bytes),
        binding.harness_digest,
        binding.runner_policy_digest,
    )
    expect(
        probe[-len(expected_tail) :] == expected_tail
        and "--pull=never" in probe
        and "--network=none" in probe
        and "--ipc=none" in probe
        and "--cgroupns=private" in probe
        and "--read-only" in probe
        and "--cap-drop=ALL" in probe
        and "--security-opt=no-new-privileges=true" in probe
        and "--security-opt=seccomp=builtin" in probe
        and "--log-driver=none" in probe
        and f"--pids-limit={ISOLATED_RUNNER_PIDS_LIMIT}" in probe
        and f"--memory={ISOLATED_RUNNER_MEMORY_BYTES // (1024 * 1024)}m"
        in probe
        and f"--cpus={ISOLATED_RUNNER_CPU_MILLIS / 1000:.1f}" in probe,
        "probe command pins harness, image, network, privilege, and resources",
        probe,
    )
    serialized = json.dumps(commands, ensure_ascii=False)
    expect(
        "type=bind" not in serialized
        and "/var/run/docker.sock" not in serialized
        and "--network=host" not in serialized
        and "PRIVATE_SECRET_SENTINEL" not in serialized
        and "PRIVATE_CALLER_ARGV_SENTINEL" not in serialized
        and ARTIFACT.decode("utf-8") not in serialized
        and all("--env" not in item for item in probe),
        (
            "commands expose no host bind, API socket, network, secret, "
            "source, or caller argv"
        ),
        commands,
    )
    inspect_templates = {
        command[2]
        for command in commands
        if command[:2] == ("container", "inspect")
    }
    expect(
        inspect_templates
        == {
            "--format={{json .Image}}",
            "--format={{json .Config}}",
            "--format={{json .HostConfig}}",
            "--format={{json .Mounts}}",
        },
        "container image, config, host policy, and mounts are re-inspected",
        inspect_templates,
    )


def success_and_command_contract() -> None:
    with TemporaryDirectory(prefix="veyra-runner-backend-success-") as raw:
        root = Path(raw)
        docker_binary = make_fake_docker(root)
        scripted = ScriptedDocker(docker_binary=docker_binary)
        engine, image, conformance = discover_trust(
            docker_binary,
            scripted,
        )
        backend = trusted_backend(
            docker_binary,
            scripted,
            engine_digest=engine,
            image_id=image,
            conformance_digest=conformance,
        )
        status = backend.status()
        expect(
            status.availability == "available"
            and status.reason_code == "ready"
            and status.conformance_certified
            and status.candidate_operation
            == "identity_probe_only_nonexecuting"
            and status.network_mode == "none"
            and status.rootfs_mode == "read_only"
            and not any(status.authority.model_dump().values()),
            "exact certified backend identity becomes available",
            status.canonical_dict(),
        )
        binding = bound_artifact(status)
        report = backend.run(artifact_bytes=ARTIFACT, binding=binding)
        expect(
            report.probe_status == "passed"
            and report.isolated_runner_status == "passed"
            and not report.issue_codes
            and report.candidate_execution_status == "not_started"
            and report.behavior_verification_status == "not_started"
            and not any(report.authority.model_dump().values()),
            "canonical harness result proves isolation only",
            report.canonical_dict(),
        )
        assert_fixed_commands(scripted, binding)
        commands = scripted.commands()
        expect(
            ("rm", "--force", STAGING_ID) in commands
            and ("rm", "--force", PROBE_ID) in commands
            and (
                "volume",
                "rm",
                "--force",
                f"veyra-phase6-{NONCE}",
            )
            in commands,
            "success removes staging, probe container, and private volume",
            commands,
        )

        harness_path = backend.harness_path()
        harness_bytes = harness_path.read_bytes()
        parsed = ast.parse(harness_bytes, filename=str(harness_path))
        forbidden_calls = {
            node.func.id
            for node in ast.walk(parsed)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"compile", "eval", "exec", "__import__"}
        }
        expect(
            backend.harness_digest() == hashlib.sha256(harness_bytes).hexdigest()
            and not forbidden_calls
            and b"/input/artifact.bin" in harness_bytes
            and b"/runner/harness.py" in harness_bytes,
            "fixed harness is digest-bound and contains no dynamic execution primitive",
            forbidden_calls,
        )


def trust_identity_mismatches() -> None:
    with TemporaryDirectory(prefix="veyra-runner-backend-identity-") as raw:
        root = Path(raw)
        docker_binary = make_fake_docker(root)
        discovery_runner = ScriptedDocker(docker_binary=docker_binary)
        engine, image, conformance = discover_trust(
            docker_binary,
            discovery_runner,
        )
        reference_runner = ScriptedDocker(docker_binary=docker_binary)
        reference = trusted_backend(
            docker_binary,
            reference_runner,
            engine_digest=engine,
            image_id=image,
            conformance_digest=conformance,
        )
        binding = bound_artifact(reference.status())
        cases = (
            (
                "engine",
                "engine_identity_mismatch",
                "f" * 64,
                image,
                conformance,
            ),
            (
                "image",
                "image_identity_mismatch",
                engine,
                "sha256:" + "e" * 64,
                conformance,
            ),
            (
                "conformance",
                "conformance_identity_mismatch",
                engine,
                image,
                "d" * 64,
            ),
        )
        for (
            label,
            reason,
            selected_engine,
            selected_image,
            selected_conformance,
        ) in cases:
            scripted = ScriptedDocker(docker_binary=docker_binary)
            backend = trusted_backend(
                docker_binary,
                scripted,
                engine_digest=selected_engine,
                image_id=selected_image,
                conformance_digest=selected_conformance,
            )
            status = backend.status()
            expect(
                status.availability == "unavailable"
                and status.reason_code == reason
                and not status.conformance_certified
                and not any(status.authority.model_dump().values()),
                f"{label} identity mismatch reports fail-closed status",
                status.canonical_dict(),
            )
            expect_raises(
                (TrustedIsolatedRunnerUnavailableError,),
                lambda backend=backend: backend.run(
                    artifact_bytes=ARTIFACT,
                    binding=binding,
                ),
                f"{label} identity mismatch blocks all container creation",
            )
            expect(
                not any(
                    command[:2] == ("volume", "create")
                    for command in scripted.commands()
                ),
                f"{label} mismatch has no isolated-run side effects",
                scripted.commands(),
            )

        identity_drifts = (
            {
                "label": "context endpoint",
                "context_endpoint": "unix:///private/runner/rebound.sock",
            },
            {
                "label": "daemon ID",
                "engine_id": "rebound-runner-engine",
            },
            {
                "label": "daemon security options",
                "security_options": (
                    "name=seccomp,profile=builtin",
                    "name=cgroupns",
                    "name=userns",
                ),
            },
        )
        for drift in identity_drifts:
            label = str(drift["label"])
            kwargs = {
                key: value
                for key, value in drift.items()
                if key != "label"
            }
            scripted = ScriptedDocker(
                docker_binary=docker_binary,
                **kwargs,  # type: ignore[arg-type]
            )
            backend = trusted_backend(
                docker_binary,
                scripted,
                engine_digest=engine,
                image_id=image,
                conformance_digest=conformance,
            )
            status = backend.status()
            expect(
                status.availability == "unavailable"
                and status.reason_code == "engine_identity_mismatch"
                and status.engine_identity_digest != engine,
                f"{label} rebind changes the private engine identity",
                status.canonical_dict(),
            )
            expect_raises(
                (TrustedIsolatedRunnerUnavailableError,),
                lambda backend=backend: backend.run(
                    artifact_bytes=ARTIFACT,
                    binding=binding,
                ),
                f"{label} rebind blocks all container creation",
            )
            expect(
                not any(
                    command[:2] == ("volume", "create")
                    for command in scripted.commands()
                ),
                f"{label} rebind has no isolated-run side effects",
                scripted.commands(),
            )


def inspect_mutations_fail_closed() -> None:
    mutations = (
        "host_bind",
        "api_socket",
        "network",
        "cap_add",
        "security_opt",
        "extra_hosts",
        "sysctls",
        "runtime",
        "secret_environment",
        "caller_argv",
    )
    with TemporaryDirectory(prefix="veyra-runner-backend-inspect-") as raw:
        root = Path(raw)
        docker_binary = make_fake_docker(root)
        discovery_runner = ScriptedDocker(docker_binary=docker_binary)
        engine, image, conformance = discover_trust(
            docker_binary,
            discovery_runner,
        )
        for mutation in mutations:
            scripted = ScriptedDocker(
                docker_binary=docker_binary,
                inspect_mutation=mutation,
            )
            backend = trusted_backend(
                docker_binary,
                scripted,
                engine_digest=engine,
                image_id=image,
                conformance_digest=conformance,
            )
            binding = bound_artifact(backend.status())
            report = backend.run(artifact_bytes=ARTIFACT, binding=binding)
            commands = scripted.commands()
            expect(
                report.probe_status == "failed"
                and report.issue_codes == ["runner_binding_mismatch"]
                and not any(
                    command[:2] == ("start", "--attach")
                    for command in commands
                ),
                f"inspect rejects {mutation} before container start",
                {"report": report.canonical_dict(), "commands": commands},
            )
            expect(
                ("rm", "--force", PROBE_ID) in commands
                and (
                    "volume",
                    "rm",
                    "--force",
                    f"veyra-phase6-{NONCE}",
                )
                in commands,
                f"{mutation} rejection still cleans container and volume",
                commands,
            )


def bounded_results_and_cleanup() -> None:
    cases = (
        ("timeout", "runner_timeout", True),
        ("output_limit", "runner_output_budget_exceeded", True),
        ("invalid_output", "runner_output_invalid", False),
        ("exit_nonzero", "runner_exit_nonzero", False),
    )
    with TemporaryDirectory(prefix="veyra-runner-backend-bounds-") as raw:
        root = Path(raw)
        docker_binary = make_fake_docker(root)
        discovery_runner = ScriptedDocker(docker_binary=docker_binary)
        engine, image, conformance = discover_trust(
            docker_binary,
            discovery_runner,
        )
        for mode, issue, terminated in cases:
            scripted = ScriptedDocker(
                docker_binary=docker_binary,
                start_mode=mode,
            )
            backend = trusted_backend(
                docker_binary,
                scripted,
                engine_digest=engine,
                image_id=image,
                conformance_digest=conformance,
            )
            binding = bound_artifact(backend.status())
            report = backend.run(artifact_bytes=ARTIFACT, binding=binding)
            commands = scripted.commands()
            start_calls = [
                call
                for call in scripted.calls
                if call[0][3:5] == ("start", "--attach")
            ]
            expect(
                report.probe_status == "failed"
                and report.issue_codes == [issue]
                and len(start_calls) == 1
                and start_calls[0][1]
                == float(ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS)
                and start_calls[0][2] == MAX_ISOLATED_RUNNER_STDOUT_BYTES,
                f"{mode} maps bounded runner result to canonical failure",
                report.canonical_dict(),
            )
            expect(
                (("kill", PROBE_ID) in commands) is terminated
                and ("rm", "--force", PROBE_ID) in commands
                and (
                    "volume",
                    "rm",
                    "--force",
                    f"veyra-phase6-{NONCE}",
                )
                in commands,
                f"{mode} follows deterministic termination and cleanup",
                commands,
            )


def real_bounded_process_boundary() -> None:
    secret_name = "VEYRA_RUNNER_PRIVATE_SECRET_SENTINEL"
    previous = os.environ.get(secret_name)
    os.environ[secret_name] = "must-not-cross"
    try:
        clean = TrustedIsolatedRunnerBackend._run_bounded_process(
            (
                sys.executable,
                "-c",
                (
                    "import os; print("
                    f"os.environ.get({secret_name!r}, 'absent'))"
                ),
            ),
            2.0,
            256,
        )
    finally:
        if previous is None:
            os.environ.pop(secret_name, None)
        else:
            os.environ[secret_name] = previous
    expect(
        clean.returncode == 0
        and clean.stdout == b"absent\n"
        and not clean.timed_out
        and not clean.output_limit_exceeded,
        "real command boundary does not inherit arbitrary secret environment",
        clean,
    )

    timed = TrustedIsolatedRunnerBackend._run_bounded_process(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
        ),
        0.1,
        64,
    )
    exceeded = TrustedIsolatedRunnerBackend._run_bounded_process(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()",
        ),
        2.0,
        64,
    )
    expect(
        timed.timed_out
        and timed.returncode is not None
        and exceeded.output_limit_exceeded
        and len(exceeded.stdout) == 65
        and exceeded.returncode is not None,
        "real command boundary kills on wall timeout and stdout budget",
        {"timed": timed, "exceeded": exceeded},
    )
    expect_raises(
        (TrustedIsolatedRunnerUnavailableError,),
        lambda: TrustedIsolatedRunnerBackend._run_bounded_process(
            (sys.executable, "bad\x00argument"),
            2.0,
            64,
        ),
        "command boundary rejects NUL-bearing caller argv",
    )


def main() -> int:
    success_and_command_contract()
    trust_identity_mismatches()
    inspect_mutations_fail_closed()
    bounded_results_and_cleanup()
    real_bounded_process_boundary()
    print("phase6 extension isolated-runner backend smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
