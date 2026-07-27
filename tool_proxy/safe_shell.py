from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import time
from typing import Any, Mapping

from core.action_risk import R0_COMMANDS
from core.world_state import WorldStateStore
from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from tool_proxy.execution_scope import ExecutionScope, ExecutionScopeError
from tool_proxy.tool_policy import ToolPolicy


_TRUSTED_SEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_FORBIDDEN_TOKEN_CHARACTERS = frozenset("|&;<>`$")
_MAX_EXECUTABLE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    alias: str
    path: Path
    device: int
    inode: int
    size: int
    mode: int
    digest: str


class SafeShell:
    """Bounded direct execution for fixed-identity R0 executables only."""

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        state_store: WorldStateStore | None = None,
        *,
        sandbox_root: str | Path | None = None,
        scope: ExecutionScope | None = None,
        timeout_seconds: float = 5.0,
        max_output_bytes: int = 64 * 1024,
        executable_allowlist: Mapping[str, str | Path] | None = None,
    ) -> None:
        if scope is not None and sandbox_root is not None:
            raise ValueError("provide scope or sandbox_root, not both")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.05 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds must be between 0.05 and 60")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or not 1 <= max_output_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError(
                "max_output_bytes must be between 1 and 4194304"
            )
        self.policy = policy or ToolPolicy()
        self.state_store = state_store
        self.scope = (
            scope
            if scope is not None
            else ExecutionScope.create(sandbox_root)
            if sandbox_root is not None
            else None
        )
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes
        self.executables = self._load_executables(executable_allowlist)
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def status(self) -> dict[str, Any]:
        return {
            "tool": "safe_shell",
            "configured": self.scope is not None and bool(self.executables),
            "mode": "direct_r0_sandbox" if self.scope else "disabled",
            "sandbox_root": str(self.scope.root) if self.scope else None,
            "scope_digest": self.scope.scope_digest if self.scope else None,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "executables": {
                alias: {
                    "path": str(identity.path),
                    "digest": identity.digest,
                }
                for alias, identity in sorted(self.executables.items())
            },
            "environment": "clean",
        }

    def run(
        self,
        command: list[str],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        boundary_error = self._validate_command(command)
        if boundary_error:
            review = self._boundary_review(boundary_error)
            self._record_policy(
                "shell_command", command, review, approved_by
            )
            return self._finish(
                command,
                {
                    "status": "blocked",
                    "reason": boundary_error,
                    "review": review,
                    "command": command,
                },
                review,
                approved_by,
            )
        assert self.scope is not None
        identity = self.executables[command[0]]
        try:
            self._verify_executable(identity)
        except (ExecutionScopeError, OSError) as exc:
            review = self._boundary_review(str(exc))
            self._record_policy(
                "shell_command", command, review, approved_by
            )
            return self._finish(
                command,
                {
                    "status": "blocked",
                    "reason": str(exc),
                    "review": review,
                    "command": command,
                },
                review,
                approved_by,
            )

        review = self.policy.review_command(command)
        self._record_policy(
            "shell_command", command, review, approved_by
        )
        if review["decision"] != "allow":
            return self._finish(
                command,
                {
                    "status": "blocked",
                    "reason": "SafeShell permits only policy-approved R0 argv",
                    "review": review,
                    "command": command,
                },
                review,
                approved_by,
            )

        argv = [str(identity.path), *command[1:]]
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.scope.root,
                env=self._clean_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                close_fds=True,
                start_new_session=True,
            )
            stdout, stderr, termination_reason = self._collect_bounded(process)
        except OSError as exc:
            payload = {
                "status": "error",
                "reason": f"safe shell spawn failed: {exc}",
                "stdout": "",
                "stderr": "",
                "returncode": None,
                "command": command,
                "approved_by": approved_by,
            }
            return self._finish(command, payload, review, approved_by)

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if termination_reason == "timeout":
            status = "timeout"
            reason = "safe shell timeout exceeded"
        elif termination_reason == "output_limit":
            status = "error"
            reason = "safe shell output budget exceeded"
        else:
            status = "ok" if process.returncode == 0 else "error"
            reason = ""
        payload = {
            "status": status,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "returncode": process.returncode,
            "command": command,
            "executable": str(identity.path),
            "executable_digest": identity.digest,
            "approved_by": approved_by,
            "scope_digest": self.scope.scope_digest,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "termination_reason": termination_reason,
            **({"reason": reason} if reason else {}),
        }
        return self._finish(command, payload, review, approved_by)

    def _validate_command(self, command: Any) -> str | None:
        if self.scope is None:
            return "SafeShell requires an explicit sandbox_root"
        if not isinstance(command, list) or not command:
            return "SafeShell requires a non-empty argv list"
        if any(
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or "\x00" in token
            or any(
                ord(character) < 32
                or ord(character) == 127
                or character in _FORBIDDEN_TOKEN_CHARACTERS
                for character in token
            )
            for token in command
        ):
            return "SafeShell argv contains an invalid or shell-like token"
        alias = command[0]
        if alias not in R0_COMMANDS:
            return "SafeShell only permits direct R0 executable aliases"
        if alias not in self.executables:
            return "SafeShell R0 executable is not pinned"
        return None

    def _load_executables(
        self,
        configured: Mapping[str, str | Path] | None,
    ) -> dict[str, ExecutableIdentity]:
        candidates: dict[str, Path] = {}
        if configured is not None:
            for alias, path in configured.items():
                if alias not in R0_COMMANDS:
                    raise ValueError(
                        f"executable alias is not an R0 command: {alias}"
                    )
                candidates[alias] = Path(path)
        else:
            for alias in sorted(R0_COMMANDS):
                located = shutil.which(alias, path=_TRUSTED_SEARCH_PATH)
                if located:
                    candidates[alias] = Path(located)

        identities: dict[str, ExecutableIdentity] = {}
        for alias, candidate in candidates.items():
            absolute = candidate.expanduser()
            if not absolute.is_absolute():
                raise ValueError(
                    f"executable path must be absolute for {alias}"
                )
            try:
                canonical = absolute.resolve(strict=True)
                identity = os.stat(canonical, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(
                    f"executable cannot be pinned for {alias}: {exc}"
                ) from exc
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_size > _MAX_EXECUTABLE_BYTES
                or not os.access(canonical, os.X_OK)
            ):
                raise ValueError(
                    f"executable must be a bounded executable regular file: {alias}"
                )
            identities[alias] = ExecutableIdentity(
                alias=alias,
                path=canonical,
                device=int(identity.st_dev),
                inode=int(identity.st_ino),
                size=int(identity.st_size),
                mode=int(identity.st_mode),
                digest=self._sha256(canonical),
            )
        return identities

    def _verify_executable(self, expected: ExecutableIdentity) -> None:
        identity = os.stat(expected.path, follow_symlinks=False)
        if (
            not stat.S_ISREG(identity.st_mode)
            or int(identity.st_dev) != expected.device
            or int(identity.st_ino) != expected.inode
            or int(identity.st_size) != expected.size
            or int(identity.st_mode) != expected.mode
            or self._sha256(expected.path) != expected.digest
        ):
            raise ExecutionScopeError(
                f"pinned executable identity changed: {expected.alias}"
            )

    def _collect_bounded(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[bytes, bytes, str | None]:
        if process.stdout is None or process.stderr is None:
            self._terminate_group(process)
            raise OSError("safe shell pipes were not created")

        selector = selectors.DefaultSelector()
        streams = {
            process.stdout.fileno(): ("stdout", process.stdout),
            process.stderr.fileno(): ("stderr", process.stderr),
        }
        for fd, (_label, stream) in streams.items():
            os.set_blocking(fd, False)
            selector.register(stream, selectors.EVENT_READ, data=fd)

        output = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        deadline = time.monotonic() + self.timeout_seconds
        termination_reason: str | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 and termination_reason is None:
                    termination_reason = "timeout"
                    self._terminate_group(process)
                events = selector.select(
                    0.05 if termination_reason else min(0.05, max(0.0, remaining))
                )
                if not events and process.poll() is not None:
                    events = [
                        (key, selectors.EVENT_READ)
                        for key in list(selector.get_map().values())
                    ]
                for key, _mask in events:
                    fd = int(key.data)
                    label, stream = streams[fd]
                    try:
                        chunk = os.read(fd, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    available = max(0, self.max_output_bytes - total)
                    if available:
                        output[label].extend(chunk[:available])
                    total += len(chunk)
                    if (
                        total > self.max_output_bytes
                        and termination_reason is None
                    ):
                        termination_reason = "output_limit"
                        self._terminate_group(process)
                if termination_reason and process.poll() is None:
                    self._terminate_group(process)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate_group(process)
                process.wait(timeout=1.0)
        finally:
            selector.close()
            for _label, stream in streams.values():
                if not stream.closed:
                    stream.close()
        return bytes(output["stdout"]), bytes(output["stderr"]), termination_reason

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def _clean_environment(self) -> dict[str, str]:
        assert self.scope is not None
        executable_dirs = sorted(
            {str(identity.path.parent) for identity in self.executables.values()}
        )
        return {
            "PATH": os.pathsep.join(executable_dirs),
            "HOME": str(self.scope.root),
            "TMPDIR": str(self.scope.root),
            "LANG": "C",
            "LC_ALL": "C",
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _boundary_review(reason: str) -> dict[str, Any]:
        return {
            "decision": "block",
            "risk_level": "R5",
            "reason": reason,
            "required_preconditions": ["direct fixed-identity R0 argv"],
            "forbidden": [
                "shell_wrapper",
                "caller_environment",
                "unbounded_execution",
            ],
        }

    def _finish(
        self,
        command: object,
        payload: dict[str, Any],
        review: dict[str, Any],
        approved_by: str | None,
    ) -> dict[str, Any]:
        payload["tool_trace"] = self._record(
            "shell_command", command, payload, review, approved_by
        )
        return payload

    def _record(
        self,
        action_type: str,
        target: object,
        payload: dict[str, Any],
        review: dict[str, Any],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        return self.tool_trace.record(
            tool="safe_shell",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(
        self,
        action_type: str,
        target: object,
        review: dict[str, Any],
        approved_by: str | None = None,
    ) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_shell",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )


__all__ = ["ExecutableIdentity", "SafeShell"]
