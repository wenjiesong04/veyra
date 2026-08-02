#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_dynamic_validation import (  # noqa: E402
    DYNAMIC_VALIDATION_FUZZ_CASES,
    DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_MEMORY_BYTES,
    DYNAMIC_VALIDATION_PIDS_LIMIT,
    DYNAMIC_VALIDATION_TMPFS_BYTES,
    MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
    DynamicValidationHarnessResult,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_isolated_runner import (  # noqa: E402
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendStatus,
)
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime import trusted_isolated_runner as isolated_module  # noqa: E402
from runtime.trusted_extension_validation_runner import (  # noqa: E402
    TrustedExtensionValidationRunner,
)
from runtime.trusted_isolated_runner import (  # noqa: E402
    IsolatedRunnerCommandResult,
    TrustedIsolatedRunnerBackend,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    BASE_TIME,
    SOURCE,
    available_backend_status,
    expect,
    make_binding,
    valid_spec,
    valid_test_bundle,
)


IMAGE_ID = "sha256:" + "2" * 64
STAGING_ID = "a" * 64
VALIDATION_ID = "b" * 64


def isolation_status() -> IsolatedRunnerBackendStatus:
    return IsolatedRunnerBackendStatus(
        schema_version=ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
        backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
        availability="available",
        reason_code="ready",
        runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
        runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
        harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
        harness_digest="6" * 64,
        image_id=IMAGE_ID,
        image_conformance_digest="3" * 64,
        engine_identity_digest="1" * 64,
        conformance_certified=True,
        authority=IsolatedRunnerAuthority(),
    )


class ScriptedDocker:
    def __init__(self, mode: str = "passed") -> None:
        self.mode = mode
        self.calls: list[tuple[tuple[str, ...], float, int]] = []
        self.volume_name = ""
        self.binding: Any = None
        self.started = 0

    def __call__(
        self,
        command: tuple[str, ...],
        timeout: float,
        output_limit: int,
    ) -> IsolatedRunnerCommandResult:
        self.calls.append((command, timeout, output_limit))
        if "volume" in command and "create" in command:
            self.volume_name = command[-1]
            return self._ok((self.volume_name + "\n").encode("ascii"))
        if "create" in command:
            cidfile_arg = next(
                item for item in command if item.startswith("--cidfile=")
            )
            cidfile = Path(cidfile_arg.split("=", 1)[1])
            staging = "dynamic-validation-staging" in command
            container_id = STAGING_ID if staging else VALIDATION_ID
            cidfile.write_text(container_id, encoding="ascii")
            return self._ok((container_id + "\n").encode("ascii"))
        if "cp" in command:
            return self._ok()
        if "container" in command and "inspect" in command:
            formatter = next(item for item in command if item.startswith("--format="))
            if ".Image" in formatter:
                return self._json(IMAGE_ID)
            if ".Config" in formatter:
                return self._json(self._config())
            if ".HostConfig" in formatter:
                return self._json(self._host())
            if ".Mounts" in formatter:
                return self._json(self._mounts())
        if "start" in command and "--attach" in command:
            self.started += 1
            if self.mode == "timeout":
                return IsolatedRunnerCommandResult(
                    returncode=None,
                    stdout=b"",
                    timed_out=True,
                )
            result = DynamicValidationHarnessResult(
                schema_version=DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION,
                binding_digest=self.binding.binding_digest(),
                build_identity_digest=self.binding.build_identity_digest,
                artifact_sha256=self.binding.artifact_sha256,
                test_bundle_digest=self.binding.test_bundle_digest,
                validation_status="passed",
                candidate_execution_status="passed",
                unit_checks_status="passed",
                contract_checks_status="passed",
                security_runtime_checks_status="passed",
                fuzz_checks_status="passed",
                behavior_verification_status="passed",
                vector_count=2,
                fuzz_case_count=DYNAMIC_VALIDATION_FUZZ_CASES,
                issue_codes=[],
            )
            payload = json.dumps(
                result.canonical_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            return self._ok(payload)
        if "rm" in command or "kill" in command:
            return self._ok()
        return self._ok()

    @staticmethod
    def _ok(stdout: bytes = b"") -> IsolatedRunnerCommandResult:
        return IsolatedRunnerCommandResult(returncode=0, stdout=stdout)

    @staticmethod
    def _json(value: Any) -> IsolatedRunnerCommandResult:
        return ScriptedDocker._ok(
            json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
        )

    def _config(self) -> dict[str, Any]:
        environment = [
            f"{key}={value}"
            for key, value in isolated_module._EXPECTED_IMAGE_ENVIRONMENT.items()
        ]
        return {
            "User": "65532:65532",
            "WorkingDir": "/tmp",
            "Cmd": [
                "/usr/local/bin/python3",
                "-I",
                "-B",
                "/runner/validation_harness.py",
            ],
            "Entrypoint": None,
            "ExposedPorts": None,
            "Labels": {"ai.veyra.phase6": "dynamic-validation"},
            "Env": environment,
        }

    def _host(self) -> dict[str, Any]:
        uid = 65_532
        tmpfs = (
            "rw,noexec,nosuid,nodev,"
            f"size={DYNAMIC_VALIDATION_TMPFS_BYTES},mode=700,uid={uid},gid={uid}"
        )
        value: dict[str, Any] = {
            "NetworkMode": "none",
            "IpcMode": "none",
            "CgroupnsMode": "private",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "AutoRemove": False,
            "PublishAllPorts": False,
            "PortBindings": None,
            "PidsLimit": DYNAMIC_VALIDATION_PIDS_LIMIT,
            "Memory": DYNAMIC_VALIDATION_MEMORY_BYTES,
            "MemorySwap": DYNAMIC_VALIDATION_MEMORY_BYTES,
            "NanoCpus": 500_000_000,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges=true", "seccomp=builtin"],
            "LogConfig": {"Type": "none"},
            "Binds": None,
            "Runtime": "runc",
            "OomKillDisable": False,
            "RestartPolicy": {"Name": "no"},
            "MaskedPaths": ["/proc/kcore", "/proc/keys", "/sys/firmware"],
            "ReadonlyPaths": ["/proc/sys"],
            "Ulimits": [
                {"Name": "core", "Soft": 0, "Hard": 0},
                {"Name": "nofile", "Soft": 64, "Hard": 64},
                {"Name": "nproc", "Soft": 16, "Hard": 16},
            ],
            "Tmpfs": {"/tmp": tmpfs},
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
        if self.mode == "host_bind_drift":
            value["Binds"] = ["/Users:/host:ro"]
        return value

    def _mounts(self) -> list[dict[str, Any]]:
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


def build_runner(
    scripted: ScriptedDocker,
) -> tuple[TrustedExtensionValidationRunner, Any]:
    base = TrustedIsolatedRunnerBackend(
        docker_binary="/usr/bin/true",
        command_runner=scripted,
        nonce_factory=lambda: "9" * 32,
        now=lambda: BASE_TIME,
    )
    fixed_isolation = isolation_status()
    base.status = lambda: fixed_isolation  # type: ignore[method-assign]
    runner = TrustedExtensionValidationRunner(
        isolation_backend=base,
        validation_conformance_certified=False,
        nonce_factory=lambda: "9" * 32,
        now=lambda: BASE_TIME,
    )
    conformance = runner.conformance_identity_digest(
        isolation=fixed_isolation,
        harness_digest=runner.harness_digest(),
    )
    runner.expected_validation_conformance_digest = conformance
    runner.validation_conformance_certified = True
    status = runner.status()
    return runner, status


def run_mode(mode: str) -> tuple[Any, ScriptedDocker, Any]:
    scripted = ScriptedDocker(mode)
    runner, status = build_runner(scripted)
    binding = make_binding(backend=status)
    scripted.binding = binding
    report = runner.run(
        artifact_bytes=SOURCE,
        spec=parse_extension_spec(valid_spec()),
        test_bundle=parse_dynamic_validation_test_bundle(valid_test_bundle()),
        binding=binding,
    )
    return report, scripted, status


def main() -> int:
    shared_boundary = TrustedIsolatedRunnerBackend._run_bounded_process(
        (sys.executable, "-c", "print('{}')"),
        2.0,
        MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
    )
    expect(
        shared_boundary.returncode == 0
        and not shared_boundary.timed_out
        and not shared_boundary.output_limit_exceeded,
        "shared Docker command boundary admits the dynamic validator's declared output budget",
    )

    bootstrap = ScriptedDocker()
    runner, status = build_runner(bootstrap)
    expect(
        status.availability == "available"
        and status.conformance_certified
        and status.engine_identity_digest == "1" * 64
        and status.image_id == IMAGE_ID
        and status.isolation_conformance_digest == "3" * 64
        and status.validation_conformance_digest
        == runner.expected_validation_conformance_digest,
        "validation backend requires exact isolation and validation conformance identities",
        status.canonical_dict(),
    )

    report, scripted, _status = run_mode("passed")
    create_commands = [
        call[0]
        for call in scripted.calls
        if "create" in call[0] and "volume" not in call[0]
    ]
    final = next(
        command
        for command in create_commands
        if "--label=ai.veyra.phase6=dynamic-validation" in command
    )
    flattened = "\n".join(" ".join(call[0]) for call in scripted.calls)
    expect(
        report.validation_status == "passed"
        and report.candidate_execution_status == "passed"
        and report.unit_checks_status == "passed"
        and report.contract_checks_status == "passed"
        and report.security_runtime_checks_status == "passed"
        and report.fuzz_checks_status == "passed"
        and report.behavior_verification_status == "passed"
        and scripted.started == 1,
        "scripted fixed container returns independently bound validation evidence",
        report.canonical_dict(),
    )
    required_flags = {
        "--network=none",
        "--ipc=none",
        "--cgroupns=private",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--security-opt=seccomp=builtin",
        "--restart=no",
        "--pids-limit=16",
        "--memory=48m",
        "--memory-swap=48m",
        "--cpus=0.5",
        "--ulimit=nofile=64:64",
        "--ulimit=nproc=16:16",
        "--ulimit=core=0:0",
        "--user=65532:65532",
        "--workdir=/tmp",
        "--log-driver=none",
    }
    expect(
        required_flags.issubset(final)
        and not any(item in {"-v", "--volume", "--env", "-e"} for item in final)
        and "type=volume" in " ".join(final)
        and "volume-subpath=input,readonly" in " ".join(final)
        and "volume-subpath=harness,readonly" in " ".join(final)
        and SOURCE.decode("utf-8") not in flattened
        and "/Users:/" not in flattened
        and "/var/run/docker.sock" not in flattened,
        "candidate container has only fixed named-volume mounts and no host bind env or socket",
        final,
    )
    start_commands = [
        call[0] for call in scripted.calls if "start" in call[0]
    ]
    expect(
        len(start_commands) == 1
        and start_commands[0][-1] == VALIDATION_ID
        and STAGING_ID not in " ".join(" ".join(item) for item in start_commands),
        "never-started staging container cannot execute candidate bytes",
        start_commands,
    )

    drift, drift_scripted, _ = run_mode("host_bind_drift")
    expect(
        drift.validation_status == "failed"
        and "validation_binding_mismatch" in drift.issue_codes
        and drift_scripted.started == 0,
        "inspect-detected host bind drift blocks container start fail closed",
        drift.canonical_dict(),
    )

    timeout, timeout_scripted, _ = run_mode("timeout")
    cleanup = "\n".join(" ".join(call[0]) for call in timeout_scripted.calls)
    expect(
        timeout.validation_status == "failed"
        and "validation_timeout" in timeout.issue_codes
        and " kill " in f" {cleanup} "
        and " volume rm " in f" {cleanup} ",
        "timeout terminates the container and removes private resources",
        timeout.canonical_dict(),
    )
    print("phase6 extension dynamic validation backend smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
