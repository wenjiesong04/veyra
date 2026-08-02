from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from interface.extension_deployment import (
    EXTENSION_DEPLOYMENT_POLICY_DIGEST,
    EXTENSION_DEPLOYMENT_POLICY_REVISION,
    EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION,
    EXTENSION_INVOCATION_CPU_MILLIS,
    EXTENSION_INVOCATION_HARNESS_REVISION,
    EXTENSION_INVOCATION_MEMORY_BYTES,
    EXTENSION_INVOCATION_NOFILE_LIMIT,
    EXTENSION_INVOCATION_PIDS_LIMIT,
    EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
    EXTENSION_INVOCATION_RUNNER_REVISION,
    EXTENSION_INVOCATION_TMPFS_BYTES,
    EXTENSION_INVOCATION_WALL_TIMEOUT_SECONDS,
    MAX_EXTENSION_INVOCATION_STDOUT_BYTES,
    ExtensionDeploymentAuthority,
    ExtensionInvocationBackendStatus,
    ExtensionInvocationBinding,
    ExtensionInvocationHarnessResult,
    ExtensionInvocationResult,
    canonical_deployment_bytes,
    canonical_utc,
    digest_canonical_value,
    parse_invocation_binding,
    parse_invocation_harness_result,
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


class TrustedExtensionInvocationError(RuntimeError):
    """Base fail-closed invocation-runner error."""


class TrustedExtensionInvocationUnavailableError(TrustedExtensionInvocationError):
    """The exact certified invocation backend is unavailable."""


class TrustedExtensionInvocationBindingError(TrustedExtensionInvocationError):
    """A signed source, contract, request, or runner identity drifted."""


class TrustedExtensionInvocationRunner:
    """Execute one signed pure function inside the certified fixed boundary.

    Callers provide only the already-bound source/spec/input documents. They
    cannot provide a command, image, environment, path, mount, harness, or
    network option. The candidate is copied into a fresh private VM volume and
    is always re-parsed and executed by the fixed harness with empty builtins.
    """

    def __init__(
        self,
        *,
        isolation_backend: TrustedIsolatedRunnerBackend,
        expected_invocation_conformance_digest: str | None = None,
        invocation_conformance_certified: bool = False,
        nonce_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.isolation_backend = isolation_backend
        self.expected_invocation_conformance_digest = (
            expected_invocation_conformance_digest
        )
        self.invocation_conformance_certified = invocation_conformance_certified
        self._nonce_factory = nonce_factory or isolation_backend._nonce_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> ExtensionInvocationBackendStatus:
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
        expected = self.expected_invocation_conformance_digest
        if not self.invocation_conformance_certified:
            return self._status(
                availability="unavailable",
                reason_code="conformance_not_certified",
                harness_digest=harness_digest,
                isolation=isolation,
                invocation_conformance_digest=conformance,
            )
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            return self._status(
                availability="unavailable",
                reason_code="configuration_missing",
                harness_digest=harness_digest,
                isolation=isolation,
                invocation_conformance_digest=conformance,
            )
        if expected != conformance:
            return self._status(
                availability="unavailable",
                reason_code="conformance_identity_mismatch",
                harness_digest=harness_digest,
                isolation=isolation,
                invocation_conformance_digest=conformance,
            )
        return self._status(
            availability="available",
            reason_code="ready",
            harness_digest=harness_digest,
            isolation=isolation,
            invocation_conformance_digest=conformance,
            conformance_certified=True,
        )

    def run(
        self,
        *,
        source_bytes: bytes,
        spec: ExtensionSpec | dict[str, Any],
        input_payload: dict[str, Any],
        binding: ExtensionInvocationBinding | dict[str, Any],
    ) -> ExtensionInvocationResult:
        selected_binding = parse_invocation_binding(binding)
        selected_spec = parse_extension_spec(spec)
        if not isinstance(source_bytes, bytes) or not isinstance(input_payload, dict):
            raise TrustedExtensionInvocationBindingError(
                "invocation inputs must be immutable bytes and one JSON object"
            )
        input_bytes = canonical_deployment_bytes(input_payload)
        if (
            hashlib.sha256(source_bytes).hexdigest()
            != selected_binding.artifact_sha256
            or selected_spec.digest() != selected_binding.spec_digest
            or hashlib.sha256(input_bytes).hexdigest()
            != selected_binding.input_digest
        ):
            raise TrustedExtensionInvocationBindingError(
                "signed invocation input identity changed"
            )
        status = self.status()
        if status.availability != "available":
            raise TrustedExtensionInvocationUnavailableError(
                f"invocation backend unavailable: {status.reason_code}"
            )
        if not self._binding_matches_backend(selected_binding, status):
            raise TrustedExtensionInvocationBindingError(
                "invocation trust binding changed"
            )
        docker_binary = self.isolation_backend._docker_binary()
        if docker_binary is None or status.image_id is None:
            raise TrustedExtensionInvocationUnavailableError(
                "invocation backend disappeared"
            )

        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return self._failed_result(selected_binding, "runner_internal_error")
        volume_name = f"veyra-phase6-invoke-{nonce}"
        staging_id: str | None = None
        container_id: str | None = None
        command_result: IsolatedRunnerCommandResult | None = None
        base = self.isolation_backend
        command_runner = base._command_runner
        prefix = base._docker_prefix(docker_binary)
        try:
            with tempfile.TemporaryDirectory(
                prefix="veyra-signed-extension-invocation-"
            ) as temporary:
                root = Path(temporary)
                input_dir = root / "input"
                harness_dir = root / "harness"
                input_dir.mkdir(mode=0o755)
                harness_dir.mkdir(mode=0o755)
                base._write_private_copy(input_dir / "artifact.py", source_bytes)
                base._write_private_copy(
                    input_dir / "spec.json", selected_spec.canonical_bytes()
                )
                base._write_private_copy(
                    input_dir / "binding.json", selected_binding.canonical_bytes()
                )
                base._write_private_copy(input_dir / "payload.json", input_bytes)
                base._write_private_copy(
                    harness_dir / "invocation_harness.py", self.harness_bytes()
                )

                volume = command_runner(
                    prefix
                    + (
                        "volume",
                        "create",
                        "--driver=local",
                        "--label=ai.veyra.phase6=signed-extension-invocation",
                        volume_name,
                    ),
                    4.0,
                    256,
                )
                if not base._named_resource_created(volume, volume_name):
                    return self._failed_result(
                        selected_binding, "runner_internal_error"
                    )
                staging_cidfile = root / "staging.cid"
                staging = command_runner(
                    self._staging_create_command(
                        docker_binary=docker_binary,
                        image_id=status.image_id,
                        volume_name=volume_name,
                        cidfile=staging_cidfile,
                    ),
                    4.0,
                    256,
                )
                staging_id = base._created_container_id(staging, staging_cidfile)
                if staging_id is None:
                    staging_id = base._cidfile_container_id(staging_cidfile)
                    return self._failed_result(
                        selected_binding, "runner_internal_error"
                    )
                copied_input = command_runner(
                    prefix + ("cp", "--quiet", str(input_dir), f"{staging_id}:/staging/"),
                    4.0,
                    256,
                )
                copied_harness = command_runner(
                    prefix + ("cp", "--quiet", str(harness_dir), f"{staging_id}:/staging/"),
                    4.0,
                    256,
                )
                if not (
                    base._command_succeeded(copied_input)
                    and base._command_succeeded(copied_harness)
                ):
                    return self._failed_result(
                        selected_binding, "runner_internal_error"
                    )
                base._remove_container(docker_binary, staging_id)
                staging_id = None

                cidfile = root / "invocation.cid"
                created = command_runner(
                    self._invocation_create_command(
                        docker_binary=docker_binary,
                        image_id=status.image_id,
                        volume_name=volume_name,
                        cidfile=cidfile,
                    ),
                    4.0,
                    256,
                )
                container_id = base._created_container_id(created, cidfile)
                if container_id is None:
                    container_id = base._cidfile_container_id(cidfile)
                    return self._failed_result(
                        selected_binding, "runner_internal_error"
                    )
                if not self._container_conforms(
                    docker_binary=docker_binary,
                    container_id=container_id,
                    image_id=status.image_id,
                    volume_name=volume_name,
                ):
                    return self._failed_result(
                        selected_binding, "runner_binding_mismatch"
                    )
                command_result = command_runner(
                    prefix + ("start", "--attach", container_id),
                    float(EXTENSION_INVOCATION_WALL_TIMEOUT_SECONDS),
                    MAX_EXTENSION_INVOCATION_STDOUT_BYTES,
                )
                if command_result.timed_out or command_result.output_limit_exceeded:
                    base._terminate_container(docker_binary, container_id)
                else:
                    base._remove_container(docker_binary, container_id)
                container_id = None
        except TrustedExtensionInvocationError:
            raise
        except Exception:
            return self._failed_result(selected_binding, "runner_internal_error")
        finally:
            try:
                if container_id is not None:
                    base._remove_container(docker_binary, container_id)
            finally:
                try:
                    if staging_id is not None:
                        base._remove_container(docker_binary, staging_id)
                finally:
                    base._remove_volume(docker_binary, volume_name)

        if command_result is None:
            return self._failed_result(selected_binding, "runner_internal_error")
        if command_result.timed_out:
            return self._indeterminate_result(selected_binding, "runner_timeout")
        if command_result.output_limit_exceeded:
            return self._indeterminate_result(
                selected_binding, "runner_output_budget_exceeded"
            )
        if command_result.returncode != 0:
            return self._indeterminate_result(selected_binding, "runner_crash")
        parsed = self._parse_harness_result(command_result.stdout, selected_binding)
        if parsed is None:
            return self._indeterminate_result(
                selected_binding, "runner_output_invalid"
            )
        if parsed.invocation_status != "passed":
            return self._failed_result(
                selected_binding, parsed.issue_code or "candidate_failed"
            )
        assert parsed.output_payload is not None
        assert parsed.output_digest is not None
        if selected_binding.mode == "shadow":
            return ExtensionInvocationResult(
                schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
                binding=selected_binding,
                binding_digest=selected_binding.binding_digest(),
                invocation_status="discarded",
                output_payload=None,
                output_digest=parsed.output_digest,
                output_discarded=True,
                issue_code=None,
                completed_at=self._now_iso(),
                authority=ExtensionDeploymentAuthority(),
            )
        return ExtensionInvocationResult(
            schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
            binding=selected_binding,
            binding_digest=selected_binding.binding_digest(),
            invocation_status="passed",
            output_payload=parsed.output_payload,
            output_digest=parsed.output_digest,
            output_discarded=False,
            issue_code=None,
            completed_at=self._now_iso(),
            authority=ExtensionDeploymentAuthority(),
        )

    @staticmethod
    def harness_path() -> Path:
        return Path(__file__).with_name("extension_invocation_harness.py")

    @classmethod
    def harness_bytes(cls) -> bytes:
        payload = cls.harness_path().read_bytes()
        if not payload or len(payload) > 128 * 1024:
            raise TrustedExtensionInvocationUnavailableError(
                "fixed invocation harness is invalid"
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
            return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except OSError:
            return ""

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
                        "veyra.phase6.extension_invocation_conformance_identity.v1"
                    ),
                    "runner_revision": EXTENSION_INVOCATION_RUNNER_REVISION,
                    "runner_implementation_digest": implementation,
                    "engine_identity_digest": isolation.engine_identity_digest,
                    "image_id": isolation.image_id,
                    "isolation_conformance_digest": (
                        isolation.image_conformance_digest
                    ),
                    "harness_revision": EXTENSION_INVOCATION_HARNESS_REVISION,
                    "harness_digest": harness_digest,
                    "deployment_policy_revision": (
                        EXTENSION_DEPLOYMENT_POLICY_REVISION
                    ),
                    "deployment_policy_digest": EXTENSION_DEPLOYMENT_POLICY_DIGEST,
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
        binding: ExtensionInvocationBinding,
        status: ExtensionInvocationBackendStatus,
    ) -> bool:
        return bool(
            status.availability == "available"
            and status.conformance_certified
            and binding.runner_engine_identity_digest
            == status.engine_identity_digest
            and binding.runner_image_id == status.image_id
            and binding.isolation_conformance_digest
            == status.isolation_conformance_digest
            and binding.invocation_conformance_digest
            == status.invocation_conformance_digest
            and binding.invocation_harness_revision == status.harness_revision
            and binding.invocation_harness_digest == status.harness_digest
            and binding.deployment_policy_revision
            == status.deployment_policy_revision
            and binding.deployment_policy_digest == status.deployment_policy_digest
            and not any(status.authority.model_dump(mode="python").values())
        )

    def _status(
        self,
        *,
        availability: str,
        reason_code: str,
        harness_digest: str,
        isolation: IsolatedRunnerBackendStatus | None = None,
        invocation_conformance_digest: str | None = None,
        conformance_certified: bool = False,
    ) -> ExtensionInvocationBackendStatus:
        return ExtensionInvocationBackendStatus(
            schema_version=EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION,
            availability=availability,
            reason_code=reason_code,
            engine_identity_digest=(
                isolation.engine_identity_digest if isolation else None
            ),
            image_id=isolation.image_id if isolation else None,
            isolation_conformance_digest=(
                isolation.image_conformance_digest if isolation else None
            ),
            invocation_conformance_digest=invocation_conformance_digest,
            harness_revision=EXTENSION_INVOCATION_HARNESS_REVISION,
            harness_digest=harness_digest,
            deployment_policy_revision=EXTENSION_DEPLOYMENT_POLICY_REVISION,
            deployment_policy_digest=EXTENSION_DEPLOYMENT_POLICY_DIGEST,
            conformance_certified=conformance_certified,
            authority=ExtensionDeploymentAuthority(),
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
            raise TrustedExtensionInvocationUnavailableError(
                "invocation staging identity is unsupported"
            )
        uid = ISOLATED_RUNNER_CONTAINER_USER
        return self.isolation_backend._docker_prefix(docker_binary) + (
            "create",
            "--pull=never",
            f"--cidfile={cidfile}",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            f"--pids-limit={EXTENSION_INVOCATION_PIDS_LIMIT}",
            f"--memory={EXTENSION_INVOCATION_MEMORY_BYTES}",
            f"--memory-swap={EXTENSION_INVOCATION_MEMORY_BYTES}",
            f"--cpus={EXTENSION_INVOCATION_CPU_MILLIS / 1000:.2f}",
            f"--user={uid}:{uid}",
            "--log-driver=none",
            "--label=ai.veyra.phase6=signed-extension-invocation-staging",
            "--mount",
            f"type=volume,src={volume_name},dst=/staging",
            image_id,
            "/bin/false",
        )

    def _invocation_create_command(
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
            raise TrustedExtensionInvocationUnavailableError(
                "invocation container identity is unsupported"
            )
        uid = ISOLATED_RUNNER_CONTAINER_USER
        memory_megabytes = EXTENSION_INVOCATION_MEMORY_BYTES // (1024 * 1024)
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
            f"--pids-limit={EXTENSION_INVOCATION_PIDS_LIMIT}",
            f"--memory={memory_megabytes}m",
            f"--memory-swap={memory_megabytes}m",
            f"--cpus={EXTENSION_INVOCATION_CPU_MILLIS / 1000:.2f}",
            f"--ulimit=nofile={EXTENSION_INVOCATION_NOFILE_LIMIT}:{EXTENSION_INVOCATION_NOFILE_LIMIT}",
            f"--ulimit=nproc={EXTENSION_INVOCATION_PIDS_LIMIT}:{EXTENSION_INVOCATION_PIDS_LIMIT}",
            "--ulimit=core=0:0",
            f"--user={uid}:{uid}",
            "--workdir=/tmp",
            "--log-driver=none",
            "--stop-timeout=1",
            "--label=ai.veyra.phase6=signed-extension-invocation",
            (
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,"
                f"size={EXTENSION_INVOCATION_TMPFS_BYTES},mode=700,uid={uid},gid={uid}"
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
            "/runner/invocation_harness.py",
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
        prefix = base._docker_prefix(docker_binary) + ("container", "inspect")
        if base._single_json_string(
            base._command_runner(
                prefix + ("--format={{json .Image}}", container_id), 3.0, 256
            )
        ) != image_id:
            return False
        config = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .Config}}", container_id), 3.0, 4096
            ),
            dict,
        )
        host = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .HostConfig}}", container_id),
                3.0,
                MAX_EXTENSION_INVOCATION_STDOUT_BYTES,
            ),
            dict,
        )
        mounts = base._single_json_value(
            base._command_runner(
                prefix + ("--format={{json .Mounts}}", container_id), 3.0, 4096
            ),
            list,
        )
        if not isinstance(config, dict) or not isinstance(host, dict):
            return False
        uid = ISOLATED_RUNNER_CONTAINER_USER
        if (
            config.get("User") != f"{uid}:{uid}"
            or config.get("WorkingDir") != "/tmp"
            or config.get("Cmd")
            != [
                "/usr/local/bin/python3",
                "-I",
                "-B",
                "/runner/invocation_harness.py",
            ]
            or config.get("Entrypoint") not in (None, [])
            or config.get("ExposedPorts") not in (None, {})
            or config.get("Labels")
            != {"ai.veyra.phase6": "signed-extension-invocation"}
            or not base._container_environment_conforms(config.get("Env"))
        ):
            return False
        security = host.get("SecurityOpt")
        expected_nano_cpus = EXTENSION_INVOCATION_CPU_MILLIS * 1_000_000
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
            or host.get("PidsLimit") != EXTENSION_INVOCATION_PIDS_LIMIT
            or host.get("Memory") != EXTENSION_INVOCATION_MEMORY_BYTES
            or host.get("MemorySwap") != EXTENSION_INVOCATION_MEMORY_BYTES
            or host.get("NanoCpus") != expected_nano_cpus
            or host.get("CapDrop") != ["ALL"]
            or host.get("CapAdd") not in (None, [])
            or not isinstance(security, list)
            or set(security) != {"no-new-privileges=true", "seccomp=builtin"}
            or len(security) != 2
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
            or not base._ulimits_conform(host.get("Ulimits"))
        ):
            return False
        expected_tmpfs = {
            "/tmp": (
                "rw,noexec,nosuid,nodev,"
                f"size={EXTENSION_INVOCATION_TMPFS_BYTES},mode=700,uid={uid},gid={uid}"
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
                item.get("VolumeOptions", {}).get("Subpath")
                if isinstance(item.get("VolumeOptions"), dict)
                else None,
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
        binding: ExtensionInvocationBinding,
    ) -> ExtensionInvocationHarnessResult | None:
        if not payload or len(payload) > MAX_EXTENSION_INVOCATION_STDOUT_BYTES:
            return None
        try:
            decoded = payload.decode("utf-8", errors="strict")
            if not decoded.endswith("\n") or "\n" in decoded[:-1]:
                return None
            value = json.loads(decoded[:-1], object_pairs_hook=_reject_duplicate_pairs)
            parsed = parse_invocation_harness_result(value)
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
            or parsed.artifact_sha256 != binding.artifact_sha256
            or parsed.spec_digest != binding.spec_digest
            or parsed.input_digest != binding.input_digest
        ):
            return None
        return parsed

    def _failed_result(
        self, binding: ExtensionInvocationBinding, issue_code: str
    ) -> ExtensionInvocationResult:
        return ExtensionInvocationResult(
            schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            invocation_status="failed",
            output_payload=None,
            output_digest=None,
            output_discarded=False,
            issue_code=issue_code,
            completed_at=self._now_iso(),
            authority=ExtensionDeploymentAuthority(),
        )

    def _indeterminate_result(
        self, binding: ExtensionInvocationBinding, issue_code: str
    ) -> ExtensionInvocationResult:
        return ExtensionInvocationResult(
            schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            invocation_status="indeterminate",
            output_payload=None,
            output_digest=None,
            output_discarded=False,
            issue_code=issue_code,
            completed_at=self._now_iso(),
            authority=ExtensionDeploymentAuthority(),
        )

    def _now_iso(self) -> str:
        return canonical_utc(self._now())


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "TrustedExtensionInvocationBindingError",
    "TrustedExtensionInvocationError",
    "TrustedExtensionInvocationRunner",
    "TrustedExtensionInvocationUnavailableError",
]
