from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Sequence

from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
    ISOLATED_RUNNER_CONTAINER_USER,
    ISOLATED_RUNNER_CPU_MILLIS,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_ISSUE_CODES,
    ISOLATED_RUNNER_MEMORY_BYTES,
    ISOLATED_RUNNER_NOFILE_LIMIT,
    ISOLATED_RUNNER_PIDS_LIMIT,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
    ISOLATED_RUNNER_TMPFS_BYTES,
    ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS,
    MAX_ISOLATED_RUNNER_STDOUT_BYTES,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendStatus,
    IsolatedRunnerBinding,
    IsolatedRunnerReport,
    parse_isolated_runner_binding,
)


DEFAULT_DOCKER_CONTEXT = "colima-veyra-runner"
DEFAULT_RUNNER_IMAGE = (
    "python:3.11-alpine3.21@"
    "sha256:cc89153ee2e125296614f6a032cb473e2bc2c0203cbe2305c917ece8866e5b01"
)
ISOLATED_RUNNER_BACKEND_REVISION = (
    "veyra.phase6.trusted_isolated_runner_backend.v1"
)
DOCKER_CONTEXT_ENV = "VEYRA_ISOLATED_RUNNER_DOCKER_CONTEXT"
HARNESS_RESULT_SCHEMA_VERSION = (
    "veyra.phase6.fixed_isolation_probe_result.v1"
)
# The shared subprocess primitive serves three independently budgeted gates:
# the 8 KiB isolation probe, 16 KiB signed invocation, and 24 KiB dynamic
# validator.  This is only the TCB-wide ceiling; each caller still supplies and
# enforces its smaller policy-specific limit.
MAX_TRUSTED_RUNNER_COMMAND_STDOUT_BYTES = 24 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTEXT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CID = re.compile(r"^[0-9a-f]{12,64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_ENGINE_FACT_FIELDS = frozenset(
    {
        "id",
        "security_options",
        "operating_system",
        "architecture",
        "driver",
        "cgroup_driver",
        "cgroup_version",
        "kernel_version",
    }
)
_ENGINE_INFO_FORMAT = (
    '{"id":{{json .ID}},'
    '"security_options":{{json .SecurityOptions}},'
    '"operating_system":{{json .OperatingSystem}},'
    '"architecture":{{json .Architecture}},'
    '"driver":{{json .Driver}},'
    '"cgroup_driver":{{json .CgroupDriver}},'
    '"cgroup_version":{{json .CgroupVersion}},'
    '"kernel_version":{{json .KernelVersion}}}'
)
_EXPECTED_IMAGE_ENVIRONMENT = {
    "GPG_KEY": "A035C8C19219BA821ECEA86B64E628F8D684696D",
    "LANG": "C.UTF-8",
    "PATH": (
        "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:"
        "/usr/bin:/sbin:/bin"
    ),
    "PYTHON_SHA256": (
        "8d3ed8ec5c88c1c95f5e558612a725450d2452813ddad5e58fdb1a53b1209b78"
    ),
    "PYTHON_VERSION": "3.11.14",
}
_HARNESS_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "harness_revision",
        "binding_digest",
        "artifact_sha256",
        "artifact_size_bytes",
        "harness_digest",
        "runner_policy_digest",
        "probe_status",
        "checks",
        "issue_codes",
    }
)
_HARNESS_CHECK_FIELDS = (
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
_REPORT_STATUS_BY_HARNESS_CHECK = {
    "artifact_identity": "artifact_identity_status",
    "harness_identity": "harness_identity_status",
    "non_root": "non_root_status",
    "rootfs_read_only": "rootfs_read_only_status",
    "input_read_only": "input_read_only_status",
    "network_isolation": "network_isolation_status",
    "secret_isolation": "secret_isolation_status",
    "host_surface_isolation": "host_surface_isolation_status",
    "resource_limits": "resource_limits_status",
}
_ISSUE_ORDER = {
    code: index for index, code in enumerate(ISOLATED_RUNNER_ISSUE_CODES)
}


class TrustedIsolatedRunnerError(RuntimeError):
    """Base error for the fail-closed trusted isolated runner."""


class TrustedIsolatedRunnerUnavailableError(TrustedIsolatedRunnerError):
    """The exact Docker, image, harness, or conformance TCB is unavailable."""


class TrustedIsolatedRunnerBindingError(TrustedIsolatedRunnerError):
    """Artifact or trust identity was rebound before isolated probing."""


@dataclass(frozen=True, slots=True)
class IsolatedRunnerCommandResult:
    """Bounded process result used by the injectable command boundary."""

    returncode: int | None
    stdout: bytes
    timed_out: bool = False
    output_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        if self.returncode is not None and type(self.returncode) is not int:
            raise TypeError("returncode must be an integer or None")
        if not isinstance(self.stdout, bytes):
            raise TypeError("stdout must be bytes")
        if type(self.timed_out) is not bool:
            raise TypeError("timed_out must be bool")
        if type(self.output_limit_exceeded) is not bool:
            raise TypeError("output_limit_exceeded must be bool")


CommandRunner = Callable[
    [tuple[str, ...], float, int],
    IsolatedRunnerCommandResult,
]


class TrustedIsolatedRunnerBackend:
    """Docker/Colima backend for a fixed, non-executing isolation probe.

    Candidate bytes are copied from a fresh private temporary directory into
    a dedicated volume inside the no-host-share runner VM.  The candidate and
    harness volume subpaths are mounted read-only into the final container.
    The only invoked program is the fixed stdlib harness; candidate code is
    never imported, compiled, evaluated, or executed.  A passed report proves
    only this isolation boundary.
    """

    def __init__(
        self,
        *,
        docker_binary: str | None = None,
        docker_context: str | None = None,
        image_reference: str = DEFAULT_RUNNER_IMAGE,
        expected_image_id: str | None = None,
        expected_engine_identity_digest: str | None = None,
        image_conformance_digest: str | None = None,
        conformance_certified: bool = False,
        command_runner: CommandRunner | None = None,
        nonce_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._configured_docker_binary = docker_binary
        context = (
            docker_context
            if docker_context is not None
            else os.environ.get(
                DOCKER_CONTEXT_ENV,
                DEFAULT_DOCKER_CONTEXT,
            )
        )
        self.docker_context = context if isinstance(context, str) else ""
        self.image_reference = image_reference
        self.expected_image_id = expected_image_id
        self.expected_engine_identity_digest = (
            expected_engine_identity_digest
        )
        self.image_conformance_digest = image_conformance_digest
        self.conformance_certified = conformance_certified
        self._command_runner = command_runner or self._run_bounded_process
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._binary_identity_cache: tuple[str, int, int, str] | None = None

    def status(self) -> IsolatedRunnerBackendStatus:
        harness_digest = self.harness_digest()
        if not _DIGEST.fullmatch(harness_digest):
            return self._status(
                availability="unavailable",
                reason_code="harness_invalid",
                harness_digest="0" * 64,
            )
        if (
            not _CONTEXT.fullmatch(self.docker_context)
            or self.image_reference != DEFAULT_RUNNER_IMAGE
            or (
                self.expected_image_id is not None
                and not _IMAGE_ID.fullmatch(self.expected_image_id)
            )
            or (
                self.expected_engine_identity_digest is not None
                and not _DIGEST.fullmatch(
                    self.expected_engine_identity_digest
                )
            )
            or (
                self.conformance_certified
                and (
                    self.expected_image_id is None
                    or self.expected_engine_identity_digest is None
                )
            )
        ):
            return self._status(
                availability="unavailable",
                reason_code="configuration_missing",
                harness_digest=harness_digest,
            )
        docker_binary = self._docker_binary()
        if docker_binary is None:
            return self._status(
                availability="unavailable",
                reason_code="docker_unavailable",
                harness_digest=harness_digest,
            )
        client_identity_digest = self._docker_binary_identity(docker_binary)
        if client_identity_digest is None:
            return self._status(
                availability="unavailable",
                reason_code="docker_unavailable",
                harness_digest=harness_digest,
            )

        try:
            context_endpoint = self._command_runner(
                (
                    docker_binary,
                    "context",
                    "inspect",
                    self.docker_context,
                    "--format={{json .Endpoints.docker.Host}}",
                ),
                4.0,
                2048,
            )
            server = self._command_runner(
                self._docker_prefix(docker_binary)
                + ("version", "--format={{json .Server.Version}}"),
                4.0,
                1024,
            )
            engine_info = self._command_runner(
                self._docker_prefix(docker_binary)
                + ("info", f"--format={_ENGINE_INFO_FORMAT}"),
                4.0,
                4096,
            )
        except Exception:
            return self._status(
                availability="unavailable",
                reason_code="engine_unavailable",
                harness_digest=harness_digest,
            )
        endpoint_identity = self._single_json_string(context_endpoint)
        server_identity = self._single_json_string(server)
        engine_facts = self._normalized_engine_facts(engine_info)
        if (
            endpoint_identity is None
            or len(endpoint_identity.encode("utf-8")) > 1024
            or server_identity is None
            or engine_facts is None
        ):
            return self._status(
                availability="unavailable",
                reason_code="engine_unavailable",
                harness_digest=harness_digest,
            )
        engine_identity_digest = hashlib.sha256(
            json.dumps(
                {
                    "backend_kind": ISOLATED_RUNNER_BACKEND_KIND,
                    "client_identity_digest": client_identity_digest,
                    "context": self.docker_context,
                    # The endpoint itself may contain a private socket path.
                    # Bind its identity without exposing it in public status.
                    "context_endpoint_digest": hashlib.sha256(
                        endpoint_identity.encode("utf-8")
                    ).hexdigest(),
                    "engine_facts": engine_facts,
                    "server_version": server_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            self.expected_engine_identity_digest is not None
            and engine_identity_digest
            != self.expected_engine_identity_digest
        ):
            return self._status(
                availability="unavailable",
                reason_code="engine_identity_mismatch",
                harness_digest=harness_digest,
                engine_identity_digest=engine_identity_digest,
            )

        try:
            image = self._command_runner(
                self._docker_prefix(docker_binary)
                + (
                    "image",
                    "inspect",
                    "--format={{json .Id}}",
                    self.image_reference,
                ),
                4.0,
                1024,
            )
        except Exception:
            return self._status(
                availability="unavailable",
                reason_code="image_unavailable",
                harness_digest=harness_digest,
                engine_identity_digest=engine_identity_digest,
            )
        image_id = self._single_json_string(image)
        if image_id is None or not _IMAGE_ID.fullmatch(image_id):
            return self._status(
                availability="unavailable",
                reason_code="image_unavailable",
                harness_digest=harness_digest,
                engine_identity_digest=engine_identity_digest,
            )
        if (
            self.expected_image_id is not None
            and image_id != self.expected_image_id
        ):
            return self._status(
                availability="unavailable",
                reason_code="image_identity_mismatch",
                harness_digest=harness_digest,
                image_id=image_id,
                engine_identity_digest=engine_identity_digest,
            )
        conformance_identity = self.conformance_identity_digest(
            engine_identity_digest=engine_identity_digest,
            image_id=image_id,
            harness_digest=harness_digest,
        )
        if not _DIGEST.fullmatch(conformance_identity):
            return self._status(
                availability="unavailable",
                reason_code="harness_invalid",
                harness_digest=harness_digest,
                image_id=image_id,
                engine_identity_digest=engine_identity_digest,
            )
        if (
            type(self.conformance_certified) is not bool
            or not self.conformance_certified
            or not isinstance(self.image_conformance_digest, str)
            or not _DIGEST.fullmatch(self.image_conformance_digest)
        ):
            return self._status(
                availability="unavailable",
                reason_code="conformance_not_certified",
                harness_digest=harness_digest,
                image_id=image_id,
                image_conformance_digest=conformance_identity,
                engine_identity_digest=engine_identity_digest,
            )
        if self.image_conformance_digest != conformance_identity:
            return self._status(
                availability="unavailable",
                reason_code="conformance_identity_mismatch",
                harness_digest=harness_digest,
                image_id=image_id,
                image_conformance_digest=conformance_identity,
                engine_identity_digest=engine_identity_digest,
            )
        return self._status(
            availability="available",
            reason_code="ready",
            harness_digest=harness_digest,
            image_id=image_id,
            image_conformance_digest=conformance_identity,
            engine_identity_digest=engine_identity_digest,
            conformance_certified=True,
        )

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
        engine_identity_digest: str,
        image_id: str,
        harness_digest: str,
    ) -> str:
        implementation_digest = self.backend_implementation_digest()
        if not all(
            _DIGEST.fullmatch(value)
            for value in (
                implementation_digest,
                engine_identity_digest,
                harness_digest,
            )
        ) or not _IMAGE_ID.fullmatch(image_id):
            return ""
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.isolated_runner_conformance_identity.v1"
                    ),
                    "backend_revision": ISOLATED_RUNNER_BACKEND_REVISION,
                    "backend_implementation_digest": (
                        implementation_digest
                    ),
                    "docker_context": self.docker_context,
                    "engine_identity_digest": engine_identity_digest,
                    "image_id": image_id,
                    "harness_digest": harness_digest,
                    "runner_policy_digest": ISOLATED_RUNNER_POLICY_DIGEST,
                    "required_probe_set": sorted(_HARNESS_CHECK_FIELDS),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def run(
        self,
        *,
        artifact_bytes: bytes,
        binding: IsolatedRunnerBinding | dict[str, Any],
    ) -> IsolatedRunnerReport:
        selected = parse_isolated_runner_binding(binding)
        if not isinstance(artifact_bytes, bytes):
            raise TrustedIsolatedRunnerBindingError(
                "isolated-runner artifact must be bytes"
            )
        if (
            len(artifact_bytes) != selected.artifact_size_bytes
            or hashlib.sha256(artifact_bytes).hexdigest()
            != selected.artifact_sha256
        ):
            raise TrustedIsolatedRunnerBindingError(
                "isolated-runner artifact identity changed"
            )

        status = self.status()
        if status.availability != "available":
            raise TrustedIsolatedRunnerUnavailableError(
                f"isolated-runner backend unavailable: {status.reason_code}"
            )
        if not self._binding_matches_backend(selected, status):
            raise TrustedIsolatedRunnerBindingError(
                "isolated-runner trust binding changed"
            )
        docker_binary = self._docker_binary()
        if docker_binary is None or status.image_id is None:
            raise TrustedIsolatedRunnerUnavailableError(
                "isolated-runner backend disappeared"
            )

        harness_bytes = self.harness_bytes()
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return self._failed_report(selected, "runner_internal_error")
        volume_name = f"veyra-phase6-{nonce}"
        staging_id: str | None = None
        container_id: str | None = None
        result: IsolatedRunnerCommandResult | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="veyra-isolated-run-"
            ) as temporary:
                root = Path(temporary)
                input_dir = root / "input"
                harness_dir = root / "harness"
                input_dir.mkdir(mode=0o755)
                harness_dir.mkdir(mode=0o755)
                artifact_path = input_dir / "artifact.bin"
                harness_path = harness_dir / "harness.py"
                self._write_private_copy(artifact_path, artifact_bytes)
                self._write_private_copy(harness_path, harness_bytes)

                volume_created = self._command_runner(
                    self._docker_prefix(docker_binary)
                    + (
                        "volume",
                        "create",
                        "--driver=local",
                        "--label=ai.veyra.phase6=isolated-runner",
                        volume_name,
                    ),
                    4.0,
                    256,
                )
                if not self._named_resource_created(
                    volume_created,
                    volume_name,
                ):
                    return self._failed_report(
                        selected,
                        "runner_internal_error",
                    )

                staging_cidfile = root / "staging.cid"
                staging_created = self._command_runner(
                    self._staging_create_command(
                        docker_binary=docker_binary,
                        image_id=status.image_id,
                        volume_name=volume_name,
                        cidfile=staging_cidfile,
                    ),
                    4.0,
                    256,
                )
                staging_id = self._created_container_id(
                    staging_created,
                    staging_cidfile,
                )
                if staging_id is None:
                    staging_id = self._cidfile_container_id(
                        staging_cidfile
                    )
                    return self._failed_report(
                        selected,
                        "runner_internal_error",
                    )
                copied_input = self._command_runner(
                    self._docker_prefix(docker_binary)
                    + (
                        "cp",
                        "--quiet",
                        str(input_dir),
                        f"{staging_id}:/staging/",
                    ),
                    4.0,
                    256,
                )
                copied_harness = self._command_runner(
                    self._docker_prefix(docker_binary)
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
                    self._command_succeeded(copied_input)
                    and self._command_succeeded(copied_harness)
                ):
                    return self._failed_report(
                        selected,
                        "runner_internal_error",
                    )
                self._remove_container(docker_binary, staging_id)
                staging_id = None

                cidfile = root / "runner.cid"
                create_command = self._probe_create_command(
                    docker_binary=docker_binary,
                    image_id=status.image_id,
                    volume_name=volume_name,
                    cidfile=cidfile,
                    binding=selected,
                )
                created = self._command_runner(
                    create_command,
                    4.0,
                    256,
                )
                container_id = self._created_container_id(
                    created,
                    cidfile,
                )
                if container_id is None:
                    container_id = self._cidfile_container_id(cidfile)
                    return self._failed_report(
                        selected,
                        "runner_internal_error",
                    )
                if not self._container_conforms(
                    docker_binary=docker_binary,
                    container_id=container_id,
                    image_id=status.image_id,
                    volume_name=volume_name,
                    binding=selected,
                ):
                    return self._failed_report(
                        selected,
                        "runner_binding_mismatch",
                    )
                result = self._command_runner(
                    self._docker_prefix(docker_binary)
                    + ("start", "--attach", container_id),
                    float(ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS),
                    MAX_ISOLATED_RUNNER_STDOUT_BYTES,
                )
                if result.timed_out or result.output_limit_exceeded:
                    self._terminate_container(docker_binary, container_id)
                else:
                    self._remove_container(docker_binary, container_id)
                container_id = None
        except TrustedIsolatedRunnerError:
            raise
        except Exception:
            return self._failed_report(selected, "runner_internal_error")
        finally:
            # Every cleanup target is attempted even if an injected or broken
            # Docker command raises while cleaning an earlier target.
            try:
                if container_id is not None:
                    self._remove_container(docker_binary, container_id)
            finally:
                try:
                    if staging_id is not None:
                        self._remove_container(docker_binary, staging_id)
                finally:
                    self._remove_volume(docker_binary, volume_name)

        if result is None:
            return self._failed_report(selected, "runner_internal_error")
        if result.timed_out:
            return self._failed_report(selected, "runner_timeout")
        if result.output_limit_exceeded:
            return self._failed_report(
                selected,
                "runner_output_budget_exceeded",
            )
        if result.returncode != 0:
            return self._failed_report(selected, "runner_exit_nonzero")
        parsed = self._parse_harness_result(result.stdout, selected)
        if parsed is None:
            return self._failed_report(selected, "runner_output_invalid")
        return self._report_from_harness(selected, parsed)

    @staticmethod
    def harness_path() -> Path:
        return Path(__file__).with_name("isolated_runner_harness.py")

    @classmethod
    def harness_bytes(cls) -> bytes:
        payload = cls.harness_path().read_bytes()
        if not payload or len(payload) > 128 * 1024:
            raise TrustedIsolatedRunnerUnavailableError(
                "fixed isolation harness is invalid"
            )
        return payload

    @classmethod
    def harness_digest(cls) -> str:
        try:
            return hashlib.sha256(cls.harness_bytes()).hexdigest()
        except Exception:
            return ""

    def _status(
        self,
        *,
        availability: str,
        reason_code: str,
        harness_digest: str,
        image_id: str | None = None,
        image_conformance_digest: str | None = None,
        engine_identity_digest: str | None = None,
        conformance_certified: bool = False,
    ) -> IsolatedRunnerBackendStatus:
        return IsolatedRunnerBackendStatus(
            schema_version=(
                ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION
            ),
            backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
            availability=availability,
            reason_code=reason_code,
            runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
            runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
            harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
            harness_digest=harness_digest,
            image_id=image_id,
            image_conformance_digest=image_conformance_digest,
            engine_identity_digest=engine_identity_digest,
            conformance_certified=conformance_certified,
            authority=IsolatedRunnerAuthority(),
        )

    def _docker_binary(self) -> str | None:
        selected = self._configured_docker_binary or shutil.which("docker")
        if not isinstance(selected, str) or not selected:
            return None
        try:
            resolved = Path(selected).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return None
        return str(resolved)

    def _docker_binary_identity(self, docker_binary: str) -> str | None:
        try:
            metadata = os.stat(docker_binary, follow_symlinks=False)
        except OSError:
            return None
        cached = self._binary_identity_cache
        cache_key = (
            docker_binary,
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
        if cached is not None and cached[:3] == cache_key:
            return cached[3]
        digest = hashlib.sha256()
        try:
            with open(docker_binary, "rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except OSError:
            return None
        selected = digest.hexdigest()
        self._binary_identity_cache = (*cache_key, selected)
        return selected

    def _docker_prefix(self, docker_binary: str) -> tuple[str, ...]:
        return (docker_binary, "--context", self.docker_context)

    @staticmethod
    def _single_json_string(
        result: IsolatedRunnerCommandResult,
    ) -> str | None:
        if (
            result.returncode != 0
            or result.timed_out
            or result.output_limit_exceeded
        ):
            return None
        try:
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _normalized_engine_facts(
        result: IsolatedRunnerCommandResult,
    ) -> dict[str, Any] | None:
        value = TrustedIsolatedRunnerBackend._single_json_value(
            result,
            dict,
        )
        if not isinstance(value, dict) or frozenset(value) != _ENGINE_FACT_FIELDS:
            return None
        normalized: dict[str, Any] = {}
        for key in sorted(_ENGINE_FACT_FIELDS - {"security_options"}):
            selected = value.get(key)
            if (
                not isinstance(selected, str)
                or not selected
                or len(selected.encode("utf-8")) > 512
                or "\x00" in selected
            ):
                return None
            normalized[key] = selected
        security_options = value.get("security_options")
        if (
            not isinstance(security_options, list)
            or not 1 <= len(security_options) <= 32
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 256
                or "\x00" in item
                for item in security_options
            )
            or len(set(security_options)) != len(security_options)
            or "name=seccomp,profile=builtin" not in security_options
            or "name=cgroupns" not in security_options
        ):
            return None
        normalized["security_options"] = sorted(security_options)
        return normalized

    @staticmethod
    def _binding_matches_backend(
        binding: IsolatedRunnerBinding,
        status: IsolatedRunnerBackendStatus,
    ) -> bool:
        return (
            binding.backend_kind == status.backend_kind
            and binding.runner_policy_revision
            == status.runner_policy_revision
            and binding.runner_policy_digest == status.runner_policy_digest
            and binding.engine_identity_digest
            == status.engine_identity_digest
            and binding.harness_revision == status.harness_revision
            and binding.harness_digest == status.harness_digest
            and binding.image_id == status.image_id
            and binding.image_conformance_digest
            == status.image_conformance_digest
            and status.conformance_certified
            and not any(status.authority.model_dump(mode="python").values())
        )

    @staticmethod
    def _write_private_copy(path: Path, payload: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        path.chmod(0o444)

    def _staging_create_command(
        self,
        *,
        docker_binary: str,
        image_id: str,
        volume_name: str,
        cidfile: Path,
    ) -> tuple[str, ...]:
        if "\n" in str(cidfile) or not _CONTEXT.fullmatch(volume_name):
            raise TrustedIsolatedRunnerUnavailableError(
                "isolated-runner staging identity is unsupported"
            )
        return self._docker_prefix(docker_binary) + (
            "create",
            "--pull=never",
            f"--cidfile={cidfile}",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            f"--pids-limit={ISOLATED_RUNNER_PIDS_LIMIT}",
            f"--memory={ISOLATED_RUNNER_MEMORY_BYTES}",
            f"--memory-swap={ISOLATED_RUNNER_MEMORY_BYTES}",
            f"--cpus={ISOLATED_RUNNER_CPU_MILLIS / 1000:.1f}",
            f"--user={ISOLATED_RUNNER_CONTAINER_USER}:{ISOLATED_RUNNER_CONTAINER_USER}",
            "--log-driver=none",
            "--mount",
            f"type=volume,src={volume_name},dst=/staging",
            image_id,
            "/bin/false",
        )

    def _probe_create_command(
        self,
        *,
        docker_binary: str,
        image_id: str,
        volume_name: str,
        cidfile: Path,
        binding: IsolatedRunnerBinding,
    ) -> tuple[str, ...]:
        if "\n" in str(cidfile) or not _CONTEXT.fullmatch(volume_name):
            raise TrustedIsolatedRunnerUnavailableError(
                "isolated-runner temporary path is unsupported"
            )
        memory_megabytes = ISOLATED_RUNNER_MEMORY_BYTES // (1024 * 1024)
        cpu_text = f"{ISOLATED_RUNNER_CPU_MILLIS / 1000:.1f}"
        return self._docker_prefix(docker_binary) + (
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
            f"--pids-limit={ISOLATED_RUNNER_PIDS_LIMIT}",
            f"--memory={memory_megabytes}m",
            f"--memory-swap={memory_megabytes}m",
            f"--cpus={cpu_text}",
            (
                f"--ulimit=nofile={ISOLATED_RUNNER_NOFILE_LIMIT}:"
                f"{ISOLATED_RUNNER_NOFILE_LIMIT}"
            ),
            f"--ulimit=nproc={ISOLATED_RUNNER_PIDS_LIMIT}:{ISOLATED_RUNNER_PIDS_LIMIT}",
            "--ulimit=core=0:0",
            f"--user={ISOLATED_RUNNER_CONTAINER_USER}:{ISOLATED_RUNNER_CONTAINER_USER}",
            "--workdir=/runner",
            "--log-driver=none",
            "--stop-timeout=1",
            (
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,"
                f"size={ISOLATED_RUNNER_TMPFS_BYTES},mode=700,"
                f"uid={ISOLATED_RUNNER_CONTAINER_USER},"
                f"gid={ISOLATED_RUNNER_CONTAINER_USER}"
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
            "/runner/harness.py",
            binding.binding_digest(),
            binding.artifact_sha256,
            str(binding.artifact_size_bytes),
            binding.harness_digest,
            binding.runner_policy_digest,
        )

    @staticmethod
    def _command_succeeded(result: IsolatedRunnerCommandResult) -> bool:
        return (
            result.returncode == 0
            and not result.timed_out
            and not result.output_limit_exceeded
        )

    @staticmethod
    def _named_resource_created(
        result: IsolatedRunnerCommandResult,
        expected_name: str,
    ) -> bool:
        if not TrustedIsolatedRunnerBackend._command_succeeded(result):
            return False
        try:
            actual = result.stdout.decode(
                "ascii",
                errors="strict",
            ).strip()
        except UnicodeError:
            return False
        return actual == expected_name

    @staticmethod
    def _created_container_id(
        result: IsolatedRunnerCommandResult,
        cidfile: Path,
    ) -> str | None:
        if not TrustedIsolatedRunnerBackend._command_succeeded(result):
            return None
        try:
            stdout_id = result.stdout.decode(
                "ascii",
                errors="strict",
            ).strip()
            file_id = cidfile.read_text(
                encoding="ascii",
                errors="strict",
            ).strip()
        except (OSError, UnicodeError):
            return None
        if (
            not _CID.fullmatch(stdout_id)
            or not _CID.fullmatch(file_id)
            or stdout_id != file_id
        ):
            return None
        return file_id

    @staticmethod
    def _cidfile_container_id(cidfile: Path) -> str | None:
        try:
            selected = cidfile.read_text(
                encoding="ascii",
                errors="strict",
            ).strip()
        except (OSError, UnicodeError):
            return None
        return selected if _CID.fullmatch(selected) else None

    def _terminate_container(
        self,
        docker_binary: str,
        container_id: str,
    ) -> None:
        if not _CID.fullmatch(container_id):
            return
        try:
            self._command_runner(
                self._docker_prefix(docker_binary)
                + ("kill", container_id),
                2.0,
                256,
            )
        except Exception:
            pass
        self._remove_container(docker_binary, container_id)

    def _container_conforms(
        self,
        *,
        docker_binary: str,
        container_id: str,
        image_id: str,
        volume_name: str,
        binding: IsolatedRunnerBinding,
    ) -> bool:
        if (
            not _CID.fullmatch(container_id)
            or not _IMAGE_ID.fullmatch(image_id)
            or not _CONTEXT.fullmatch(volume_name)
        ):
            return False
        prefix = self._docker_prefix(docker_binary) + (
            "container",
            "inspect",
        )
        image_result = self._command_runner(
            prefix + ("--format={{json .Image}}", container_id),
            3.0,
            256,
        )
        if self._single_json_string(image_result) != image_id:
            return False
        config = self._single_json_value(
            self._command_runner(
                prefix + ("--format={{json .Config}}", container_id),
                3.0,
                4096,
            ),
            dict,
        )
        host = self._single_json_value(
            self._command_runner(
                prefix
                + ("--format={{json .HostConfig}}", container_id),
                3.0,
                MAX_ISOLATED_RUNNER_STDOUT_BYTES,
            ),
            dict,
        )
        mounts = self._single_json_value(
            self._command_runner(
                prefix + ("--format={{json .Mounts}}", container_id),
                3.0,
                4096,
            ),
            list,
        )
        if not isinstance(config, dict) or not isinstance(host, dict):
            return False
        expected_command = [
            "/usr/local/bin/python3",
            "-I",
            "-B",
            "/runner/harness.py",
            binding.binding_digest(),
            binding.artifact_sha256,
            str(binding.artifact_size_bytes),
            binding.harness_digest,
            binding.runner_policy_digest,
        ]
        if (
            config.get("User")
            != f"{ISOLATED_RUNNER_CONTAINER_USER}:{ISOLATED_RUNNER_CONTAINER_USER}"
            or config.get("WorkingDir") != "/runner"
            or config.get("Cmd") != expected_command
            or config.get("Entrypoint") not in (None, [])
            or config.get("ExposedPorts") not in (None, {})
            or not self._container_environment_conforms(config.get("Env"))
        ):
            return False
        expected_nano_cpus = ISOLATED_RUNNER_CPU_MILLIS * 1_000_000
        security_options = host.get("SecurityOpt")
        capability_drop = host.get("CapDrop")
        masked_paths = host.get("MaskedPaths")
        readonly_paths = host.get("ReadonlyPaths")
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
            or host.get("PidsLimit") != ISOLATED_RUNNER_PIDS_LIMIT
            or host.get("Memory") != ISOLATED_RUNNER_MEMORY_BYTES
            or host.get("MemorySwap") != ISOLATED_RUNNER_MEMORY_BYTES
            or host.get("NanoCpus") != expected_nano_cpus
            or capability_drop != ["ALL"]
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
        ):
            return False
        if not self._ulimits_conform(host.get("Ulimits")):
            return False
        expected_tmpfs = {
            "/tmp": (
                "rw,noexec,nosuid,nodev,"
                f"size={ISOLATED_RUNNER_TMPFS_BYTES},mode=700,"
                f"uid={ISOLATED_RUNNER_CONTAINER_USER},"
                f"gid={ISOLATED_RUNNER_CONTAINER_USER}"
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
    def _container_environment_conforms(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        selected: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                return False
            name, _separator, content = item.partition("=")
            if not name or name in selected:
                return False
            selected[name] = content
        return selected == _EXPECTED_IMAGE_ENVIRONMENT

    @staticmethod
    def _single_json_value(
        result: IsolatedRunnerCommandResult,
        expected_type: type,
    ) -> Any | None:
        if not TrustedIsolatedRunnerBackend._command_succeeded(result):
            return None
        try:
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, expected_type) else None

    @staticmethod
    def _ulimits_conform(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        selected: dict[str, tuple[int, int]] = {}
        for item in value:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("Name"), str)
                or type(item.get("Soft")) is not int
                or type(item.get("Hard")) is not int
            ):
                return False
            selected[item["Name"]] = (item["Soft"], item["Hard"])
        return selected == {
            "core": (0, 0),
            "nofile": (
                ISOLATED_RUNNER_NOFILE_LIMIT,
                ISOLATED_RUNNER_NOFILE_LIMIT,
            ),
            "nproc": (
                ISOLATED_RUNNER_PIDS_LIMIT,
                ISOLATED_RUNNER_PIDS_LIMIT,
            ),
        }

    def _remove_container(
        self,
        docker_binary: str,
        container_id: str,
    ) -> None:
        if not _CID.fullmatch(container_id):
            return
        try:
            self._command_runner(
                self._docker_prefix(docker_binary)
                + ("rm", "--force", container_id),
                2.0,
                256,
            )
        except Exception:
            return

    def _remove_volume(
        self,
        docker_binary: str,
        volume_name: str,
    ) -> None:
        if not _CONTEXT.fullmatch(volume_name):
            return
        try:
            self._command_runner(
                self._docker_prefix(docker_binary)
                + ("volume", "rm", "--force", volume_name),
                3.0,
                256,
            )
        except Exception:
            return

    def _parse_harness_result(
        self,
        stdout: bytes,
        binding: IsolatedRunnerBinding,
    ) -> dict[str, Any] | None:
        try:
            decoded = stdout.decode("ascii", errors="strict")
            value = json.loads(decoded)
            canonical = json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ) + "\n"
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return None
        if decoded != canonical or not isinstance(value, dict):
            return None
        if frozenset(value) != _HARNESS_RESULT_FIELDS:
            return None
        if (
            value.get("schema_version") != HARNESS_RESULT_SCHEMA_VERSION
            or value.get("harness_revision")
            != ISOLATED_RUNNER_HARNESS_REVISION
            or value.get("binding_digest") != binding.binding_digest()
            or value.get("artifact_sha256") != binding.artifact_sha256
            or value.get("artifact_size_bytes")
            != binding.artifact_size_bytes
            or value.get("harness_digest") != binding.harness_digest
            or value.get("runner_policy_digest")
            != binding.runner_policy_digest
            or value.get("probe_status") not in {"passed", "failed"}
        ):
            return None
        checks = value.get("checks")
        issues = value.get("issue_codes")
        if (
            not isinstance(checks, dict)
            or frozenset(checks) != frozenset(_HARNESS_CHECK_FIELDS)
            or any(item not in {"passed", "failed"} for item in checks.values())
            or not isinstance(issues, list)
            or any(
                not isinstance(item, str) or item not in _ISSUE_ORDER
                for item in issues
            )
            or len(set(issues)) != len(issues)
            or issues != sorted(issues, key=lambda item: _ISSUE_ORDER[item])
        ):
            return None
        expected_passed = not issues and all(
            item == "passed" for item in checks.values()
        )
        if (value["probe_status"] == "passed") != expected_passed:
            return None
        return value

    def _report_from_harness(
        self,
        binding: IsolatedRunnerBinding,
        result: dict[str, Any],
    ) -> IsolatedRunnerReport:
        checks = result["checks"]
        statuses = {
            report_name: checks[check_name]
            for check_name, report_name in (
                _REPORT_STATUS_BY_HARNESS_CHECK.items()
            )
        }
        return IsolatedRunnerReport(
            schema_version=ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            probe_status=result["probe_status"],
            isolated_runner_status=result["probe_status"],
            issue_codes=result["issue_codes"],
            completed_at=self._now_iso(),
            authority=IsolatedRunnerAuthority(),
            **statuses,
        )

    def _failed_report(
        self,
        binding: IsolatedRunnerBinding,
        issue_code: str,
    ) -> IsolatedRunnerReport:
        statuses = {
            report_name: "not_checked"
            for report_name in _REPORT_STATUS_BY_HARNESS_CHECK.values()
        }
        return IsolatedRunnerReport(
            schema_version=ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            probe_status="failed",
            isolated_runner_status="failed",
            issue_codes=[issue_code],
            completed_at=self._now_iso(),
            authority=IsolatedRunnerAuthority(),
            **statuses,
        )

    def _now_iso(self) -> str:
        selected = self._now().astimezone(timezone.utc)
        if selected.microsecond:
            return selected.isoformat(
                timespec="microseconds",
            ).replace("+00:00", "Z")
        return selected.isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _clean_cli_env() -> dict[str, str]:
        environment = {
            "PATH": (
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                "/usr/sbin:/sbin"
            ),
            "LANG": "C",
            "LC_ALL": "C",
            "DOCKER_CLI_HINTS": "false",
        }
        home = os.environ.get("HOME")
        if isinstance(home, str) and home.startswith("/"):
            environment["HOME"] = home
        docker_config = os.environ.get("DOCKER_CONFIG")
        if isinstance(docker_config, str) and docker_config.startswith("/"):
            environment["DOCKER_CONFIG"] = docker_config
        return environment

    @classmethod
    def _run_bounded_process(
        cls,
        argv: tuple[str, ...],
        timeout_seconds: float,
        stdout_limit: int,
    ) -> IsolatedRunnerCommandResult:
        if (
            not argv
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
            or not 0.1 <= timeout_seconds <= 30.0
            or not 64
            <= stdout_limit
            <= MAX_TRUSTED_RUNNER_COMMAND_STDOUT_BYTES
        ):
            raise TrustedIsolatedRunnerUnavailableError(
                "isolated-runner command boundary is invalid"
            )
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cls._clean_cli_env(),
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise TrustedIsolatedRunnerUnavailableError(
                "isolated-runner pipes are unavailable"
            )
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        stdout = bytearray()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        exceeded = False
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(remaining, 0.1))
            if not events and process.poll() is not None:
                events = selector.select(timeout=0)
                if not events:
                    break
            for key, _mask in events:
                try:
                    block = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stdout:
                    stdout.extend(block)
                    if len(stdout) > stdout_limit:
                        exceeded = True
                        break
            if exceeded:
                break
        selector.close()
        if timed_out or exceeded:
            process.kill()
        try:
            returncode = process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=1.0)
        return IsolatedRunnerCommandResult(
            returncode=returncode,
            stdout=bytes(stdout[: stdout_limit + 1]),
            timed_out=timed_out,
            output_limit_exceeded=exceeded,
        )


__all__ = [
    "DEFAULT_DOCKER_CONTEXT",
    "DEFAULT_RUNNER_IMAGE",
    "DOCKER_CONTEXT_ENV",
    "ISOLATED_RUNNER_BACKEND_REVISION",
    "IsolatedRunnerCommandResult",
    "TrustedIsolatedRunnerBackend",
    "TrustedIsolatedRunnerBindingError",
    "TrustedIsolatedRunnerError",
    "TrustedIsolatedRunnerUnavailableError",
]
