from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_BACKEND_REVISION,
    DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_CPU_MILLIS,
    DYNAMIC_VALIDATION_HARNESS_REVISION,
    DYNAMIC_VALIDATION_MEMORY_BYTES,
    DYNAMIC_VALIDATION_NOFILE_LIMIT,
    DYNAMIC_VALIDATION_PIDS_LIMIT,
    DYNAMIC_VALIDATION_POLICY_DIGEST,
    DYNAMIC_VALIDATION_POLICY_REVISION,
    DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_TMPFS_BYTES,
    DYNAMIC_VALIDATION_WALL_TIMEOUT_SECONDS,
    MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
    DynamicValidationAuthority,
    DynamicValidationBackendStatus,
    DynamicValidationBinding,
    DynamicValidationHarnessResult,
    DynamicValidationReport,
    DynamicValidationTestBundle,
    parse_dynamic_validation_binding,
    parse_dynamic_validation_harness_result,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_CONTAINER_USER,
    IsolatedRunnerBackendStatus,
    parse_isolated_runner_backend_status,
)
from interface.extension_spec import ExtensionSpec, parse_extension_spec
from runtime.trusted_isolated_runner import (
    IsolatedRunnerCommandResult,
    TrustedIsolatedRunnerBackend,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")


class TrustedExtensionValidationError(RuntimeError):
    """Base error for the fail-closed executable validation profile."""


class TrustedExtensionValidationUnavailableError(
    TrustedExtensionValidationError
):
    """The exact certified validation backend is unavailable."""


class TrustedExtensionValidationBindingError(
    TrustedExtensionValidationError
):
    """A source, contract, test, runner, or backend identity drifted."""


class TrustedExtensionValidationRunner:
    """Run one narrowly shaped candidate in a fixed Docker/Colima sandbox.

    No caller supplies a command, path, mount, image, environment or network
    setting.  Candidate bytes, the immutable Spec and caller-frozen behavior
    vectors are copied into a fresh named volume using a never-started staging
    container.  The final container has no host bind, network, secret or state
    surface and invokes only the fixed stdlib validation harness.
    """

    def __init__(
        self,
        *,
        isolation_backend: TrustedIsolatedRunnerBackend,
        expected_validation_conformance_digest: str | None = None,
        validation_conformance_certified: bool = False,
        nonce_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.isolation_backend = isolation_backend
        self.expected_validation_conformance_digest = (
            expected_validation_conformance_digest
        )
        self.validation_conformance_certified = (
            validation_conformance_certified
        )
        self._nonce_factory = nonce_factory or isolation_backend._nonce_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> DynamicValidationBackendStatus:
        harness_digest = self.harness_digest()
        if not _DIGEST.fullmatch(harness_digest):
            return self._status(
                availability="unavailable",
                reason_code="harness_invalid",
                harness_digest="0" * 64,
            )
        try:
            isolation = parse_isolated_runner_backend_status(
                self.isolation_backend.status()
            )
        except Exception:
            return self._status(
                availability="unavailable",
                reason_code="isolation_backend_unavailable",
                harness_digest=harness_digest,
            )
        if not self._isolation_ready(isolation):
            return self._status(
                availability="unavailable",
                reason_code="isolation_backend_unavailable",
                harness_digest=harness_digest,
                isolation=isolation,
            )
        conformance = self.conformance_identity_digest(
            isolation=isolation,
            harness_digest=harness_digest,
        )
        expected = self.expected_validation_conformance_digest
        if (
            not isinstance(self.validation_conformance_certified, bool)
            or not self.validation_conformance_certified
        ):
            return self._status(
                availability="unavailable",
                reason_code="conformance_not_certified",
                harness_digest=harness_digest,
                isolation=isolation,
                validation_conformance_digest=conformance,
            )
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            return self._status(
                availability="unavailable",
                reason_code="configuration_missing",
                harness_digest=harness_digest,
                isolation=isolation,
                validation_conformance_digest=conformance,
            )
        if expected != conformance:
            return self._status(
                availability="unavailable",
                reason_code="conformance_identity_mismatch",
                harness_digest=harness_digest,
                isolation=isolation,
                validation_conformance_digest=conformance,
            )
        return self._status(
            availability="available",
            reason_code="ready",
            harness_digest=harness_digest,
            isolation=isolation,
            validation_conformance_digest=conformance,
            conformance_certified=True,
        )

    def run(
        self,
        *,
        artifact_bytes: bytes,
        spec: ExtensionSpec | dict[str, Any],
        test_bundle: DynamicValidationTestBundle | dict[str, Any],
        binding: DynamicValidationBinding | dict[str, Any],
    ) -> DynamicValidationReport:
        selected_binding = parse_dynamic_validation_binding(binding)
        selected_spec = parse_extension_spec(spec)
        selected_tests = parse_dynamic_validation_test_bundle(test_bundle)
        if not isinstance(artifact_bytes, bytes):
            raise TrustedExtensionValidationBindingError(
                "dynamic validation artifact must be bytes"
            )
        if (
            len(artifact_bytes) != selected_binding.artifact_size_bytes
            or hashlib.sha256(artifact_bytes).hexdigest()
            != selected_binding.artifact_sha256
            or selected_spec.digest() != selected_binding.spec_digest
            or selected_tests.bundle_digest()
            != selected_binding.test_bundle_digest
        ):
            raise TrustedExtensionValidationBindingError(
                "dynamic validation input identity changed"
            )

        status = self.status()
        if status.availability != "available":
            raise TrustedExtensionValidationUnavailableError(
                f"dynamic validation backend unavailable: {status.reason_code}"
            )
        if not self._binding_matches_backend(selected_binding, status):
            raise TrustedExtensionValidationBindingError(
                "dynamic validation trust binding changed"
            )
        docker_binary = self.isolation_backend._docker_binary()
        if docker_binary is None or status.image_id is None:
            raise TrustedExtensionValidationUnavailableError(
                "dynamic validation backend disappeared"
            )

        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return self._failed_report(selected_binding, "validation_internal_error")
        volume_name = f"veyra-phase6-validation-{nonce}"
        staging_id: str | None = None
        container_id: str | None = None
        result: IsolatedRunnerCommandResult | None = None
        command_runner = self.isolation_backend._command_runner
        prefix = self.isolation_backend._docker_prefix(docker_binary)
        try:
            with tempfile.TemporaryDirectory(
                prefix="veyra-extension-validation-"
            ) as temporary:
                root = Path(temporary)
                input_dir = root / "input"
                harness_dir = root / "harness"
                input_dir.mkdir(mode=0o755)
                harness_dir.mkdir(mode=0o755)
                self.isolation_backend._write_private_copy(
                    input_dir / "artifact.py",
                    artifact_bytes,
                )
                self.isolation_backend._write_private_copy(
                    input_dir / "spec.json",
                    selected_spec.canonical_bytes(),
                )
                self.isolation_backend._write_private_copy(
                    input_dir / "tests.json",
                    selected_tests.canonical_bytes(),
                )
                self.isolation_backend._write_private_copy(
                    input_dir / "binding.json",
                    selected_binding.canonical_bytes(),
                )
                self.isolation_backend._write_private_copy(
                    harness_dir / "validation_harness.py",
                    self.harness_bytes(),
                )

                created_volume = command_runner(
                    prefix
                    + (
                        "volume",
                        "create",
                        "--driver=local",
                        "--label=ai.veyra.phase6=dynamic-validation",
                        volume_name,
                    ),
                    4.0,
                    256,
                )
                if not self.isolation_backend._named_resource_created(
                    created_volume,
                    volume_name,
                ):
                    return self._failed_report(
                        selected_binding,
                        "validation_internal_error",
                    )

                staging_cidfile = root / "staging.cid"
                staging_created = command_runner(
                    self._staging_create_command(
                        docker_binary=docker_binary,
                        image_id=status.image_id,
                        volume_name=volume_name,
                        cidfile=staging_cidfile,
                    ),
                    4.0,
                    256,
                )
                staging_id = self.isolation_backend._created_container_id(
                    staging_created,
                    staging_cidfile,
                )
                if staging_id is None:
                    staging_id = self.isolation_backend._cidfile_container_id(
                        staging_cidfile
                    )
                    return self._failed_report(
                        selected_binding,
                        "validation_internal_error",
                    )
                copied_input = command_runner(
                    prefix
                    + (
                        "cp",
                        "--quiet",
                        str(input_dir),
                        f"{staging_id}:/staging/",
                    ),
                    4.0,
                    256,
                )
                copied_harness = command_runner(
                    prefix
                    + (
                        "cp",
                        "--quiet",
                        str(harness_dir),
                        f"{staging_id}:/staging/",
                    ),
                    4.0,
                    256,
                )
                if not (
                    self.isolation_backend._command_succeeded(copied_input)
                    and self.isolation_backend._command_succeeded(copied_harness)
                ):
                    return self._failed_report(
                        selected_binding,
                        "validation_internal_error",
                    )
                self.isolation_backend._remove_container(
                    docker_binary,
                    staging_id,
                )
                staging_id = None

                cidfile = root / "validation.cid"
                create_command = self._validation_create_command(
                    docker_binary=docker_binary,
                    image_id=status.image_id,
                    volume_name=volume_name,
                    cidfile=cidfile,
                )
                created = command_runner(create_command, 4.0, 256)
                container_id = self.isolation_backend._created_container_id(
                    created,
                    cidfile,
                )
                if container_id is None:
                    container_id = self.isolation_backend._cidfile_container_id(
                        cidfile
                    )
                    return self._failed_report(
                        selected_binding,
                        "validation_internal_error",
                    )
                if not self._container_conforms(
                    docker_binary=docker_binary,
                    container_id=container_id,
                    image_id=status.image_id,
                    volume_name=volume_name,
                ):
                    return self._failed_report(
                        selected_binding,
                        "validation_binding_mismatch",
                    )
                result = command_runner(
                    prefix + ("start", "--attach", container_id),
                    float(DYNAMIC_VALIDATION_WALL_TIMEOUT_SECONDS),
                    MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
                )
                if result.timed_out or result.output_limit_exceeded:
                    self.isolation_backend._terminate_container(
                        docker_binary,
                        container_id,
                    )
                else:
                    self.isolation_backend._remove_container(
                        docker_binary,
                        container_id,
                    )
                container_id = None
        except TrustedExtensionValidationError:
            raise
        except Exception:
            return self._failed_report(
                selected_binding,
                "validation_internal_error",
            )
        finally:
            try:
                if container_id is not None:
                    self.isolation_backend._remove_container(
                        docker_binary,
                        container_id,
                    )
            finally:
                try:
                    if staging_id is not None:
                        self.isolation_backend._remove_container(
                            docker_binary,
                            staging_id,
                        )
                finally:
                    self.isolation_backend._remove_volume(
                        docker_binary,
                        volume_name,
                    )

        if result is None:
            return self._failed_report(selected_binding, "validation_internal_error")
        if result.timed_out:
            return self._failed_report(selected_binding, "validation_timeout")
        if result.output_limit_exceeded:
            return self._failed_report(
                selected_binding,
                "validation_output_budget_exceeded",
            )
        if result.returncode != 0:
            return self._failed_report(
                selected_binding,
                "validation_exit_nonzero",
            )
        parsed = self._parse_harness_result(result.stdout, selected_binding)
        if parsed is None:
            return self._failed_report(
                selected_binding,
                "validation_output_invalid",
            )
        return self._report_from_harness(selected_binding, parsed)

    @staticmethod
    def harness_path() -> Path:
        return Path(__file__).with_name("extension_validation_harness.py")

    @classmethod
    def harness_bytes(cls) -> bytes:
        payload = cls.harness_path().read_bytes()
        if not payload or len(payload) > 128 * 1024:
            raise TrustedExtensionValidationUnavailableError(
                "fixed dynamic validation harness is invalid"
            )
        return payload

    @classmethod
    def harness_digest(cls) -> str:
        try:
            return hashlib.sha256(cls.harness_bytes()).hexdigest()
        except Exception:
            return ""

    @classmethod
    def backend_implementation_digest(cls) -> str:
        try:
            payload = Path(__file__).resolve(strict=True).read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(payload).hexdigest()

    def conformance_identity_digest(
        self,
        *,
        isolation: IsolatedRunnerBackendStatus,
        harness_digest: str,
    ) -> str:
        implementation = self.backend_implementation_digest()
        if (
            not self._isolation_ready(isolation)
            or not _DIGEST.fullmatch(implementation)
            or not _DIGEST.fullmatch(harness_digest)
        ):
            return ""
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.dynamic_validation_conformance_identity.v1"
                    ),
                    "backend_revision": DYNAMIC_VALIDATION_BACKEND_REVISION,
                    "backend_implementation_digest": implementation,
                    "engine_identity_digest": isolation.engine_identity_digest,
                    "image_id": isolation.image_id,
                    "isolation_conformance_digest": (
                        isolation.image_conformance_digest
                    ),
                    "validation_harness_revision": (
                        DYNAMIC_VALIDATION_HARNESS_REVISION
                    ),
                    "validation_harness_digest": harness_digest,
                    "validation_policy_revision": (
                        DYNAMIC_VALIDATION_POLICY_REVISION
                    ),
                    "validation_policy_digest": DYNAMIC_VALIDATION_POLICY_DIGEST,
                    "test_profiles": [
                        "behavior",
                        "contract",
                        "deterministic_fuzz",
                        "security_runtime",
                        "unit",
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _isolation_ready(status: IsolatedRunnerBackendStatus) -> bool:
        return bool(
            status.availability == "available"
            and status.conformance_certified
            and status.engine_identity_digest is not None
            and status.image_id is not None
            and status.image_conformance_digest is not None
            and not any(status.authority.model_dump(mode="python").values())
        )

    @staticmethod
    def _binding_matches_backend(
        binding: DynamicValidationBinding,
        status: DynamicValidationBackendStatus,
    ) -> bool:
        return bool(
            status.availability == "available"
            and status.conformance_certified
            and binding.engine_identity_digest == status.engine_identity_digest
            and binding.image_id == status.image_id
            and binding.isolation_conformance_digest
            == status.isolation_conformance_digest
            and binding.validation_conformance_digest
            == status.validation_conformance_digest
            and binding.validation_policy_revision
            == status.validation_policy_revision
            and binding.validation_policy_digest
            == status.validation_policy_digest
            and binding.validation_harness_revision == status.harness_revision
            and binding.validation_harness_digest == status.harness_digest
            and not any(status.authority.model_dump(mode="python").values())
        )

    def _status(
        self,
        *,
        availability: str,
        reason_code: str,
        harness_digest: str,
        isolation: IsolatedRunnerBackendStatus | None = None,
        validation_conformance_digest: str | None = None,
        conformance_certified: bool = False,
    ) -> DynamicValidationBackendStatus:
        return DynamicValidationBackendStatus(
            schema_version=DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION,
            backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
            availability=availability,
            reason_code=reason_code,
            engine_identity_digest=(
                isolation.engine_identity_digest if isolation else None
            ),
            image_id=isolation.image_id if isolation else None,
            isolation_conformance_digest=(
                isolation.image_conformance_digest if isolation else None
            ),
            validation_conformance_digest=validation_conformance_digest,
            harness_revision=DYNAMIC_VALIDATION_HARNESS_REVISION,
            harness_digest=harness_digest,
            validation_policy_revision=DYNAMIC_VALIDATION_POLICY_REVISION,
            validation_policy_digest=DYNAMIC_VALIDATION_POLICY_DIGEST,
            conformance_certified=conformance_certified,
            authority=DynamicValidationAuthority(),
        )

    def _staging_create_command(
        self,
        *,
        docker_binary: str,
        image_id: str,
        volume_name: str,
        cidfile: Path,
    ) -> tuple[str, ...]:
        if (
            "\n" in str(cidfile)
            or not _RESOURCE_NAME.fullmatch(volume_name)
            or not _IMAGE_ID.fullmatch(image_id)
        ):
            raise TrustedExtensionValidationUnavailableError(
                "dynamic validation staging identity is unsupported"
            )
        prefix = self.isolation_backend._docker_prefix(docker_binary)
        return prefix + (
            "create",
            "--pull=never",
            f"--cidfile={cidfile}",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            f"--pids-limit={DYNAMIC_VALIDATION_PIDS_LIMIT}",
            f"--memory={DYNAMIC_VALIDATION_MEMORY_BYTES}",
            f"--memory-swap={DYNAMIC_VALIDATION_MEMORY_BYTES}",
            f"--cpus={DYNAMIC_VALIDATION_CPU_MILLIS / 1000:.1f}",
            (
                f"--user={ISOLATED_RUNNER_CONTAINER_USER}:"
                f"{ISOLATED_RUNNER_CONTAINER_USER}"
            ),
            "--log-driver=none",
            "--label=ai.veyra.phase6=dynamic-validation-staging",
            "--mount",
            f"type=volume,src={volume_name},dst=/staging",
            image_id,
            "/bin/false",
        )

    def _validation_create_command(
        self,
        *,
        docker_binary: str,
        image_id: str,
        volume_name: str,
        cidfile: Path,
    ) -> tuple[str, ...]:
        if (
            "\n" in str(cidfile)
            or not _RESOURCE_NAME.fullmatch(volume_name)
            or not _IMAGE_ID.fullmatch(image_id)
        ):
            raise TrustedExtensionValidationUnavailableError(
                "dynamic validation container identity is unsupported"
            )
        memory_megabytes = DYNAMIC_VALIDATION_MEMORY_BYTES // (1024 * 1024)
        cpu_text = f"{DYNAMIC_VALIDATION_CPU_MILLIS / 1000:.1f}"
        uid = ISOLATED_RUNNER_CONTAINER_USER
        return self.isolation_backend._docker_prefix(docker_binary) + (
            "create",
            "--pull=never",
            f"--cidfile={cidfile}",
            "--network=none",
            "--ipc=none",
            "--cgroupns=private",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--security-opt=seccomp=builtin",
            "--restart=no",
            f"--pids-limit={DYNAMIC_VALIDATION_PIDS_LIMIT}",
            f"--memory={memory_megabytes}m",
            f"--memory-swap={memory_megabytes}m",
            f"--cpus={cpu_text}",
            (
                f"--ulimit=nofile={DYNAMIC_VALIDATION_NOFILE_LIMIT}:"
                f"{DYNAMIC_VALIDATION_NOFILE_LIMIT}"
            ),
            (
                f"--ulimit=nproc={DYNAMIC_VALIDATION_PIDS_LIMIT}:"
                f"{DYNAMIC_VALIDATION_PIDS_LIMIT}"
            ),
            "--ulimit=core=0:0",
            f"--user={uid}:{uid}",
            "--workdir=/tmp",
            "--log-driver=none",
            "--stop-timeout=1",
            "--label=ai.veyra.phase6=dynamic-validation",
            (
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,"
                f"size={DYNAMIC_VALIDATION_TMPFS_BYTES},mode=700,"
                f"uid={uid},gid={uid}"
            ),
            "--mount",
            (
                f"type=volume,src={volume_name},dst=/input,"
                "volume-subpath=input,readonly"
            ),
            "--mount",
            (
                f"type=volume,src={volume_name},dst=/runner,"
                "volume-subpath=harness,readonly"
            ),
            image_id,
            "/usr/local/bin/python3",
            "-I",
            "-B",
            "/runner/validation_harness.py",
        )

    def _container_conforms(
        self,
        *,
        docker_binary: str,
        container_id: str,
        image_id: str,
        volume_name: str,
    ) -> bool:
        if (
            not _CONTAINER_ID.fullmatch(container_id)
            or not _IMAGE_ID.fullmatch(image_id)
            or not _RESOURCE_NAME.fullmatch(volume_name)
        ):
            return False
        base = self.isolation_backend
        prefix = base._docker_prefix(docker_binary) + (
            "container",
            "inspect",
        )
        if base._single_json_string(
            base._command_runner(
                prefix + ("--format={{json .Image}}", container_id),
                3.0,
                256,
            )
        ) != image_id:
            return False
        config = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .Config}}", container_id),
                3.0,
                4096,
            ),
            dict,
        )
        host = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .HostConfig}}", container_id),
                3.0,
                MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
            ),
            dict,
        )
        mounts = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .Mounts}}", container_id),
                3.0,
                4096,
            ),
            list,
        )
        if not isinstance(config, dict) or not isinstance(host, dict):
            return False
        uid = ISOLATED_RUNNER_CONTAINER_USER
        expected_command = [
            "/usr/local/bin/python3",
            "-I",
            "-B",
            "/runner/validation_harness.py",
        ]
        if (
            config.get("User") != f"{uid}:{uid}"
            or config.get("WorkingDir") != "/tmp"
            or config.get("Cmd") != expected_command
            or config.get("Entrypoint") not in (None, [])
            or config.get("ExposedPorts") not in (None, {})
            or config.get("Labels")
            != {"ai.veyra.phase6": "dynamic-validation"}
            or not base._container_environment_conforms(config.get("Env"))
        ):
            return False
        security_options = host.get("SecurityOpt")
        masked_paths = host.get("MaskedPaths")
        readonly_paths = host.get("ReadonlyPaths")
        expected_nano_cpus = DYNAMIC_VALIDATION_CPU_MILLIS * 1_000_000
        if (
            host.get("NetworkMode") != "none"
            or host.get("IpcMode") != "none"
            or host.get("CgroupnsMode") != "private"
            or host.get("PidMode") not in (None, "")
            or host.get("UTSMode") not in (None, "")
            or host.get("UsernsMode") not in (None, "")
            or host.get("Cgroup") not in (None, "")
            or host.get("CgroupParent") not in (None, "")
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or host.get("AutoRemove") is not False
            or host.get("PublishAllPorts") is not False
            or host.get("PortBindings") not in (None, {})
            or host.get("PidsLimit") != DYNAMIC_VALIDATION_PIDS_LIMIT
            or host.get("Memory") != DYNAMIC_VALIDATION_MEMORY_BYTES
            or host.get("MemorySwap") != DYNAMIC_VALIDATION_MEMORY_BYTES
            or host.get("NanoCpus") != expected_nano_cpus
            or host.get("CapDrop") != ["ALL"]
            or host.get("CapAdd") not in (None, [])
            or not isinstance(security_options, list)
            or set(security_options)
            != {"no-new-privileges=true", "seccomp=builtin"}
            or len(security_options) != 2
            or host.get("LogConfig", {}).get("Type") != "none"
            or host.get("Binds") not in (None, [])
            or host.get("VolumesFrom") not in (None, [])
            or host.get("Links") not in (None, [])
            or host.get("ExtraHosts") not in (None, [])
            or host.get("GroupAdd") not in (None, [])
            or host.get("Dns") not in (None, [])
            or host.get("DnsOptions") not in (None, [])
            or host.get("DnsSearch") not in (None, [])
            or host.get("Devices") not in (None, [])
            or host.get("DeviceCgroupRules") not in (None, [])
            or host.get("DeviceRequests") not in (None, [])
            or host.get("Sysctls") not in (None, {})
            or host.get("Runtime") != "runc"
            or host.get("Isolation") not in (None, "")
            or host.get("OomKillDisable") is not False
            or host.get("RestartPolicy", {}).get("Name") != "no"
            or not isinstance(masked_paths, list)
            or not {"/proc/kcore", "/proc/keys", "/sys/firmware"}.issubset(
                set(masked_paths)
            )
            or not isinstance(readonly_paths, list)
            or "/proc/sys" not in readonly_paths
            or not base._ulimits_conform(host.get("Ulimits"))
        ):
            return False
        expected_tmpfs = {
            "/tmp": (
                "rw,noexec,nosuid,nodev,"
                f"size={DYNAMIC_VALIDATION_TMPFS_BYTES},mode=700,"
                f"uid={uid},gid={uid}"
            )
        }
        if host.get("Tmpfs") != expected_tmpfs:
            return False
        host_mounts = host.get("Mounts")
        if not isinstance(host_mounts, list) or len(host_mounts) != 2:
            return False
        actual_host_mounts = {
            (
                item.get("Type"),
                item.get("Source"),
                item.get("Target"),
                item.get("ReadOnly"),
                (
                    item.get("VolumeOptions", {}).get("Subpath")
                    if isinstance(item.get("VolumeOptions"), dict)
                    else None
                ),
            )
            for item in host_mounts
            if isinstance(item, dict)
        }
        if actual_host_mounts != {
            ("volume", volume_name, "/input", True, "input"),
            ("volume", volume_name, "/runner", True, "harness"),
        }:
            return False
        if not isinstance(mounts, list) or len(mounts) != 2:
            return False
        actual_mounts = {
            (
                item.get("Type"),
                item.get("Name"),
                item.get("Destination"),
                item.get("RW"),
            )
            for item in mounts
            if isinstance(item, dict)
        }
        return actual_mounts == {
            ("volume", volume_name, "/input", False),
            ("volume", volume_name, "/runner", False),
        }

    @staticmethod
    def _parse_harness_result(
        payload: bytes,
        binding: DynamicValidationBinding,
    ) -> DynamicValidationHarnessResult | None:
        if not payload or len(payload) > MAX_DYNAMIC_VALIDATION_STDOUT_BYTES:
            return None
        try:
            decoded = payload.decode("utf-8", errors="strict")
            if not decoded.endswith("\n") or "\n" in decoded[:-1]:
                return None
            value = json.loads(
                decoded[:-1],
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
            parsed = parse_dynamic_validation_harness_result(value)
            canonical = json.dumps(
                parsed.canonical_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if decoded != canonical + "\n":
                return None
        except (UnicodeError, ValueError, TypeError):
            return None
        if (
            parsed.binding_digest != binding.binding_digest()
            or parsed.build_identity_digest != binding.build_identity_digest
            or parsed.artifact_sha256 != binding.artifact_sha256
            or parsed.test_bundle_digest != binding.test_bundle_digest
        ):
            return None
        return parsed

    def _report_from_harness(
        self,
        binding: DynamicValidationBinding,
        result: DynamicValidationHarnessResult,
    ) -> DynamicValidationReport:
        return DynamicValidationReport(
            schema_version=DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            validation_status=result.validation_status,
            candidate_execution_status=result.candidate_execution_status,
            unit_checks_status=result.unit_checks_status,
            contract_checks_status=result.contract_checks_status,
            security_runtime_checks_status=(
                result.security_runtime_checks_status
            ),
            fuzz_checks_status=result.fuzz_checks_status,
            behavior_verification_status=(
                result.behavior_verification_status
            ),
            vector_count=result.vector_count,
            fuzz_case_count=result.fuzz_case_count,
            issue_codes=list(result.issue_codes),
            completed_at=self._now_iso(),
            authority=DynamicValidationAuthority(),
        )

    def _failed_report(
        self,
        binding: DynamicValidationBinding,
        issue_code: str,
    ) -> DynamicValidationReport:
        return DynamicValidationReport(
            schema_version=DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            validation_status="failed",
            candidate_execution_status="failed",
            unit_checks_status="not_checked",
            contract_checks_status="not_checked",
            security_runtime_checks_status="not_checked",
            fuzz_checks_status="not_checked",
            behavior_verification_status="not_checked",
            vector_count=0,
            fuzz_case_count=0,
            issue_codes=[issue_code],
            completed_at=self._now_iso(),
            authority=DynamicValidationAuthority(),
        )

    def _now_iso(self) -> str:
        selected = self._now()
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise TrustedExtensionValidationUnavailableError(
                "dynamic validation clock is invalid"
            )
        value = selected.astimezone(timezone.utc)
        if value.microsecond:
            return value.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


__all__ = [
    "TrustedExtensionValidationBindingError",
    "TrustedExtensionValidationError",
    "TrustedExtensionValidationRunner",
    "TrustedExtensionValidationUnavailableError",
]
