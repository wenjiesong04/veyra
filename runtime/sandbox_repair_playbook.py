from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Callable, Iterator

from core.autonomy_policy import (
    AutonomyLevel,
    AutonomyPolicyRegistry,
    JSON_SANDBOX_REPAIR_PROFILE,
)
from core.definitions import RiskLevel
from core.world_state import WorldStateStore


PLAYBOOK_ID = "sandbox_repair.json_candidate.v1"
IMPLEMENTATION_REVISION = "veyra.phase5.json_sandbox_repair.v1"
STATE_FILE = "sandbox_repair_state.json"
STATE_SCHEMA_VERSION = "veyra.sandbox_repair_state.v1"
MAX_CANDIDATE_BYTES = 256 * 1024
MAX_OPERATIONS = 200
_OPERATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: dict[str, threading.RLock] = {}
_POLICY = AutonomyPolicyRegistry((JSON_SANDBOX_REPAIR_PROFILE,))
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_OPEN_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_OPEN_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
)


@dataclass(frozen=True, slots=True)
class _DirectoryAnchor:
    parent_fd: int
    name: str
    fd: int
    device: int
    inode: int


class _PinnedOperationDirectory:
    """One operation directory held by descriptor for its whole lifetime."""

    def __init__(
        self,
        *,
        root_path: Path,
        root_fd: int,
        root_identity: os.stat_result,
        anchors: list[_DirectoryAnchor],
        operation_path: Path,
        max_bytes: int,
    ) -> None:
        self.root_path = root_path
        self.root_fd = root_fd
        self.root_device = int(root_identity.st_dev)
        self.root_inode = int(root_identity.st_ino)
        self.anchors = tuple(anchors)
        self.operation_path = operation_path
        self.max_bytes = max_bytes
        self.operation_fd = self.anchors[-1].fd
        self.scope_digest = hashlib.sha256(
            json.dumps(
                {
                    "root": str(root_path),
                    "root_device": self.root_device,
                    "root_inode": self.root_inode,
                    "components": [
                        {
                            "name": anchor.name,
                            "device": anchor.device,
                            "inode": anchor.inode,
                        }
                        for anchor in self.anchors
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._closed = False

    def assert_attached(self) -> None:
        """Prove every held directory still has its original parent edge."""

        if self._closed:
            raise ValueError("sandbox operation directory is closed")
        held_root = os.fstat(self.root_fd)
        try:
            linked_root = os.stat(
                self.root_path,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError(
                "sandbox state root binding is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(held_root.st_mode)
            or not stat.S_ISDIR(linked_root.st_mode)
            or int(held_root.st_dev) != self.root_device
            or int(held_root.st_ino) != self.root_inode
            or int(linked_root.st_dev) != self.root_device
            or int(linked_root.st_ino) != self.root_inode
        ):
            raise ValueError("sandbox state root binding changed")

        for anchor in self.anchors:
            held = os.fstat(anchor.fd)
            try:
                linked = os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(
                    "sandbox directory binding is unavailable"
                ) from exc
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or int(held.st_dev) != anchor.device
                or int(held.st_ino) != anchor.inode
                or int(linked.st_dev) != anchor.device
                or int(linked.st_ino) != anchor.inode
            ):
                raise ValueError("sandbox directory binding changed")

    def write_text(
        self,
        name: str,
        content: str,
    ) -> dict[str, Any]:
        self._validate_name(name)
        try:
            content_bytes = content.encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise ValueError("sandbox content must be UTF-8 text") from exc
        if len(content_bytes) > self.max_bytes:
            raise ValueError("sandbox content exceeds the byte budget")

        self.assert_attached()
        before = self._target_stat(name, allow_missing=True)
        if before is not None:
            raise ValueError("sandbox target already exists")
        temporary_name = f".veyra-write-{secrets.token_hex(12)}.tmp"
        temporary_created = False
        try:
            temporary_fd = os.open(
                temporary_name,
                _OPEN_WRITE_FLAGS,
                0o600,
                dir_fd=self.operation_fd,
            )
            temporary_created = True
            try:
                self._write_all(temporary_fd, content_bytes)
                os.fchmod(temporary_fd, 0o600)
                os.fsync(temporary_fd)
                temporary_identity = os.fstat(temporary_fd)
            finally:
                os.close(temporary_fd)

            # A path-chain swap after staging cannot redirect the final rename:
            # both source and destination stay relative to the pinned fd.
            self.assert_attached()
            if self._target_stat(name, allow_missing=True) is not None:
                raise ValueError("sandbox target appeared during write")
            os.replace(
                temporary_name,
                name,
                src_dir_fd=self.operation_fd,
                dst_dir_fd=self.operation_fd,
            )
            temporary_created = False
            final_identity = self._target_stat(name, allow_missing=False)
            assert final_identity is not None
            if (
                int(final_identity.st_dev)
                != int(temporary_identity.st_dev)
                or int(final_identity.st_ino)
                != int(temporary_identity.st_ino)
                or int(final_identity.st_size) != len(content_bytes)
            ):
                raise ValueError("sandbox atomic write identity mismatch")
            os.fsync(self.operation_fd)
            self.assert_attached()
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=self.operation_fd)
                except FileNotFoundError:
                    pass

        return {
            "status": "ok",
            "content_bytes": len(content_bytes),
            "content_digest": hashlib.sha256(content_bytes).hexdigest(),
            "scope_digest": self.scope_digest,
        }

    def read_text(self, name: str) -> dict[str, Any]:
        self._validate_name(name)
        self.assert_attached()
        fd = os.open(
            name,
            _OPEN_READ_FLAGS,
            dir_fd=self.operation_fd,
        )
        try:
            identity = os.fstat(fd)
            if not stat.S_ISREG(identity.st_mode):
                raise ValueError("sandbox target is not a regular file")
            if identity.st_size > self.max_bytes:
                raise ValueError("sandbox target exceeds the byte budget")
            content_bytes = self._read_bounded(fd)
        finally:
            os.close(fd)
        self.assert_attached()
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("sandbox target is not UTF-8 text") from exc
        return {
            "status": "ok",
            "content": content,
            "content_bytes": len(content_bytes),
            "content_digest": hashlib.sha256(content_bytes).hexdigest(),
            "scope_digest": self.scope_digest,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for anchor in reversed(self.anchors):
            os.close(anchor.fd)
        os.close(self.root_fd)

    def _target_stat(
        self,
        name: str,
        *,
        allow_missing: bool,
    ) -> os.stat_result | None:
        try:
            identity = os.stat(
                name,
                dir_fd=self.operation_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if allow_missing:
                return None
            raise ValueError("sandbox target does not exist") from None
        if stat.S_ISLNK(identity.st_mode):
            raise ValueError("sandbox target cannot be a symlink")
        if not stat.S_ISREG(identity.st_mode):
            raise ValueError("sandbox target must be a regular file")
        return identity

    def _read_bounded(self, fd: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                fd,
                min(64 * 1024, self.max_bytes + 1 - total),
            )
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > self.max_bytes:
                raise ValueError("sandbox target exceeds the byte budget")

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("sandbox write made no progress")
            view = view[written:]

    @staticmethod
    def _validate_name(name: str) -> None:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\x00" in name
            or len(name.encode("utf-8")) > 255
        ):
            raise ValueError("sandbox target name is invalid")


@dataclass(frozen=True, slots=True)
class JsonSandboxRepairRequest:
    """An in-memory JSON candidate; never a path into a real workspace."""

    operation_id: str
    candidate_json: str
    expected_digest: str


@dataclass(frozen=True, slots=True)
class JsonSandboxRepairSpec:
    playbook_id: str = PLAYBOOK_ID
    version: int = 1
    domain: str = "sandbox_repair"
    maximum_level: str = AutonomyLevel.A3.value
    risk_floor: str = RiskLevel.R2.value
    max_attempts: int = 1
    operation_timeout_seconds: int = 30
    max_candidate_bytes: int = MAX_CANDIDATE_BYTES

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "version": self.version,
            "domain": self.domain,
            "maximum_level": self.maximum_level,
            "risk_floor": self.risk_floor,
            "max_attempts": self.max_attempts,
            "operation_timeout_seconds": self.operation_timeout_seconds,
            "max_candidate_bytes": self.max_candidate_bytes,
            "input_contract": "bounded in-memory JSON object plus exact digest",
            "verification": [
                "strict JSON parse with duplicate/non-finite rejection",
                "independent file reopen through a pinned directory descriptor",
                "independent strict JSON round-trip",
            ],
            "success_condition": "sandbox_verified_candidate",
            "promotion": "not_authorized",
        }


class JsonSandboxRepairPlaybook:
    """Canonicalize and verify one JSON candidate in a private sandbox.

    This is an A3 candidate workflow, not a production repair. It cannot read a
    caller-selected path, invoke an Agent or shell, use the network, or promote
    the candidate outside the Veyra-owned sandbox.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        sandbox_base: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.spec = JsonSandboxRepairSpec()
        self._now = now or (lambda: datetime.now(timezone.utc))
        root = state_store.root.resolve()
        selected = (
            Path(
                os.path.abspath(
                    os.fspath(Path(sandbox_base).expanduser())
                )
            )
            if sandbox_base is not None
            else root / "runtime" / "sandbox_repairs"
        )
        try:
            selected.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "sandbox repair base must remain inside the Veyra state root"
            ) from exc
        self.sandbox_base = selected
        lock_key = f"{root}::{PLAYBOOK_ID}"
        with _RUN_LOCKS_GUARD:
            self._run_lock = _RUN_LOCKS.setdefault(
                lock_key,
                threading.RLock(),
            )

    def status(self) -> dict[str, Any]:
        try:
            mode = self._mode_context()
            state = self.state_store.read_json(STATE_FILE)
            if not self._valid_state(state):
                return self._fault("invalid_state")
            last_id = str(state.get("last_operation_id") or "")
            record = (
                state.get("operations", {}).get(last_id)
                if last_id
                and isinstance(state.get("operations"), dict)
                else None
            )
            return self._public_result(
                record if isinstance(record, dict) else None,
                mode=mode["mode"],
            )
        except Exception as exc:
            return self._fault(type(exc).__name__)

    def run(
        self,
        request: JsonSandboxRepairRequest,
    ) -> dict[str, Any]:
        with self._run_lock:
            try:
                return self._run_locked(request)
            except Exception as exc:
                return self._fault(type(exc).__name__)

    def _run_locked(
        self,
        request: JsonSandboxRepairRequest,
    ) -> dict[str, Any]:
        state = self.state_store.read_json(STATE_FILE)
        if not self._valid_state(state):
            return self._fault("invalid_state")
        mode_context = self._mode_context()
        mode = mode_context["mode"]
        if mode == "disabled":
            return self._public_result(None, mode=mode, status="disabled")
        if mode == "record_only":
            return self._public_result(None, mode=mode, status="record_only")

        validated, error = self._validate_request(request)
        if error:
            return self._public_result(
                None,
                mode=mode,
                status="blocked",
                fault_code=error,
            )
        assert validated is not None
        if mode == "shadow":
            return self._public_result(
                None,
                mode=mode,
                status="shadow_candidate_valid",
                candidate_status="would_stage_in_private_sandbox",
            )
        if mode != "scoped_canary":
            return self._fault("invalid_mode")

        for capability in (
            "sandbox.json_candidate.stage",
            "sandbox.json_candidate.verify",
        ):
            decision = _POLICY.decide(
                profile_id=JSON_SANDBOX_REPAIR_PROFILE.profile_id,
                domain="sandbox_repair",
                capability=capability,
                risk_level=RiskLevel.R2,
                user_scope="local-user",
                environment="local",
                target_scope="veyra_private:sandbox_repair_json",
                mode=mode,
                attempt_count=0,
                cooldown_elapsed=True,
                requested_level=AutonomyLevel.A3,
                now=self._now(),
            )
            if not decision.allowed:
                return self._fault(decision.reason_code)

        binding_digest = self._binding_digest(mode_context)
        record, disposition = self._claim(
            request=request,
            validated=validated,
            binding_digest=binding_digest,
            mode_context=mode_context,
        )
        if disposition != "claimed":
            return self._public_result(record, mode=mode)

        operation_id = request.operation_id
        try:
            # Config mutation and the private sandbox effect are ordered by the
            # state writer lock. A disable committed first is observed here;
            # a disable committed later cannot precede this already-finished
            # private effect.
            with self.state_store.writer_transaction():
                current_mode = self._mode_context()
                if (
                    current_mode != mode_context
                    or self._binding_digest(current_mode) != binding_digest
                ):
                    revoked = self._finish(
                        operation_id=operation_id,
                        expected_request_digest=validated["request_digest"],
                        state="revoked",
                        outcome="scope_changed",
                        evidence=None,
                    )
                    return self._public_result(revoked, mode=mode)
                with self._open_operation_root(
                    operation_id
                ) as operation_directory:
                    self._after_operation_directory_pinned(
                        operation_directory.operation_path
                    )
                    operation_directory.assert_attached()
                    staged = operation_directory.write_text(
                        "candidate.json",
                        validated["canonical_json"],
                    )
                    if staged.get("status") != "ok":
                        failed = self._finish(
                            operation_id=operation_id,
                            expected_request_digest=validated[
                                "request_digest"
                            ],
                            state="failed",
                            outcome="sandbox_write_failed",
                            evidence=None,
                        )
                        return self._public_result(failed, mode=mode)
                    verification = self._verify_staged_candidate(
                        operation_directory=operation_directory,
                        canonical_json=validated["canonical_json"],
                        canonical_value=validated["canonical_value"],
                    )
                    manifest = {
                        "schema_version": (
                            "veyra.sandbox_repair_manifest.v1"
                        ),
                        "playbook_id": PLAYBOOK_ID,
                        "operation_id": operation_id,
                        "input_digest": validated["input_digest"],
                        "canonical_digest": validated["canonical_digest"],
                        "verification_digest": verification[
                            "verification_digest"
                        ],
                        "promotion_authorized": False,
                    }
                    manifest_content = json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ) + "\n"
                    manifest_result = operation_directory.write_text(
                        "manifest.json",
                        manifest_content,
                    )
                    if manifest_result.get("status") != "ok":
                        raise RuntimeError(
                            "sandbox manifest write failed"
                        )
                    operation_directory.assert_attached()
                    completed = self._finish(
                        operation_id=operation_id,
                        expected_request_digest=validated[
                            "request_digest"
                        ],
                        state="completed",
                        outcome="sandbox_verified_candidate",
                        evidence={
                            "verification_digest": verification[
                                "verification_digest"
                            ],
                            "canonical_digest": validated[
                                "canonical_digest"
                            ],
                        },
                    )
                    return self._public_result(completed, mode=mode)
        except Exception:
            indeterminate = self._finish_indeterminate(
                operation_id=operation_id,
                expected_request_digest=validated["request_digest"],
            )
            return self._public_result(indeterminate, mode=mode)

    def _claim(
        self,
        *,
        request: JsonSandboxRepairRequest,
        validated: dict[str, Any],
        binding_digest: str,
        mode_context: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        selected: dict[str, Any] | None = None
        disposition = "denied"
        now = self._now()

        def claim(state: dict[str, Any]) -> None:
            nonlocal selected, disposition
            if not self._valid_state(state):
                raise ValueError("invalid sandbox repair state")
            operations = state.setdefault("operations", {})
            request_index = state.setdefault("request_index", {})
            indexed_id = request_index.get(validated["request_digest"])
            if isinstance(indexed_id, str):
                indexed = operations.get(indexed_id)
                if not isinstance(indexed, dict):
                    raise ValueError("sandbox repair request index is invalid")
                if indexed.get("state") == "claimed" and self._claim_expired(
                    indexed,
                    now=now,
                ):
                    indexed.update(
                        {
                            "state": "indeterminate",
                            "status": "indeterminate",
                            "outcome": "indeterminate",
                            "completed_at": self._now_iso(),
                            "updated_at": self._now_iso(),
                        }
                    )
                selected = dict(indexed)
                disposition = "existing"
                return
            existing = operations.get(request.operation_id)
            if isinstance(existing, dict):
                if (
                    existing.get("request_digest")
                    != validated["request_digest"]
                ):
                    selected = {
                        **existing,
                        "status": "operation_conflict",
                    }
                    disposition = "conflict"
                    return
                if existing.get("state") == "claimed" and self._claim_expired(
                    existing,
                    now=now,
                ):
                    existing.update(
                        {
                            "state": "indeterminate",
                            "status": "indeterminate",
                            "outcome": "indeterminate",
                            "completed_at": self._now_iso(),
                            "updated_at": self._now_iso(),
                        }
                    )
                selected = dict(existing)
                disposition = "existing"
                return
            if len(operations) >= MAX_OPERATIONS:
                selected = {
                    "operation_id": request.operation_id,
                    "status": "capacity_exhausted",
                    "state": "failed",
                }
                disposition = "capacity"
                return
            record = {
                "operation_id": request.operation_id,
                "request_digest": validated["request_digest"],
                "input_digest": validated["input_digest"],
                "canonical_digest": validated["canonical_digest"],
                "target_binding_digest": binding_digest,
                "profile_id": JSON_SANDBOX_REPAIR_PROFILE.profile_id,
                "mode": mode_context["mode"],
                "mode_epoch": mode_context["mode_epoch"],
                "attempt_number": 1,
                "state": "claimed",
                "status": "attempt_in_progress",
                "outcome": None,
                "claimed_at": self._now_iso(),
                "completed_at": None,
                "evidence": None,
                "promotion_authorized": False,
                "updated_at": self._now_iso(),
            }
            operations[request.operation_id] = record
            request_index[validated["request_digest"]] = request.operation_id
            state["last_operation_id"] = request.operation_id
            selected = dict(record)
            disposition = "claimed"

        self.state_store.mutate_json(STATE_FILE, claim)
        if selected is None:
            raise RuntimeError("sandbox repair claim did not resolve")
        return selected, disposition

    def _finish(
        self,
        *,
        operation_id: str,
        expected_request_digest: str,
        state: str,
        outcome: str,
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selected: dict[str, Any] | None = None

        def finish(document: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(document):
                raise ValueError("invalid sandbox repair state")
            record = document["operations"].get(operation_id)
            if (
                not isinstance(record, dict)
                or record.get("request_digest") != expected_request_digest
                or record.get("state") != "claimed"
            ):
                raise ValueError("sandbox repair operation claim changed")
            record.update(
                {
                    "state": state,
                    "status": outcome,
                    "outcome": outcome,
                    "completed_at": self._now_iso(),
                    "evidence": evidence,
                    "promotion_authorized": False,
                    "updated_at": self._now_iso(),
                }
            )
            document["last_operation_id"] = operation_id
            selected = dict(record)

        self.state_store.mutate_json(STATE_FILE, finish)
        if selected is None:
            raise RuntimeError("sandbox repair completion did not resolve")
        return selected

    def _finish_indeterminate(
        self,
        *,
        operation_id: str,
        expected_request_digest: str,
    ) -> dict[str, Any]:
        selected: dict[str, Any] | None = None

        def stop(document: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(document):
                raise ValueError("invalid sandbox repair state")
            record = document["operations"].get(operation_id)
            if not isinstance(record, dict):
                raise ValueError("sandbox repair operation is missing")
            if record.get("request_digest") != expected_request_digest:
                raise ValueError("sandbox repair request binding changed")
            if record.get("state") == "claimed":
                record.update(
                    {
                        "state": "indeterminate",
                        "status": "indeterminate",
                        "outcome": "indeterminate",
                        "completed_at": self._now_iso(),
                        "evidence": None,
                        "promotion_authorized": False,
                        "updated_at": self._now_iso(),
                    }
                )
            selected = dict(record)

        self.state_store.mutate_json(STATE_FILE, stop)
        if selected is None:
            raise RuntimeError("sandbox repair indeterminate stop failed")
        return selected

    def _validate_request(
        self,
        request: Any,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(request, JsonSandboxRepairRequest):
            return None, "invalid_request_type"
        if not _OPERATION_ID.fullmatch(str(request.operation_id or "")):
            return None, "invalid_operation_id"
        if not isinstance(request.candidate_json, str):
            return None, "candidate_must_be_text"
        try:
            content = request.candidate_json.encode("utf-8")
        except UnicodeEncodeError:
            return None, "candidate_must_be_utf8"
        if not content or len(content) > self.spec.max_candidate_bytes:
            return None, "candidate_budget_exceeded"
        if not _DIGEST.fullmatch(str(request.expected_digest or "")):
            return None, "invalid_expected_digest"
        input_digest = hashlib.sha256(content).hexdigest()
        if input_digest != request.expected_digest:
            return None, "candidate_digest_mismatch"
        try:
            value = self._strict_loads(request.candidate_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "invalid_strict_json"
        if not isinstance(value, dict):
            return None, "candidate_must_be_json_object"
        canonical_json = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        canonical_bytes = canonical_json.encode("utf-8")
        if len(canonical_bytes) > self.spec.max_candidate_bytes:
            return None, "canonical_candidate_budget_exceeded"
        canonical_digest = hashlib.sha256(canonical_bytes).hexdigest()
        request_digest = self._digest(
            {
                "playbook_id": PLAYBOOK_ID,
                "operation_id": request.operation_id,
                "input_digest": input_digest,
                "canonical_digest": canonical_digest,
            }
        )
        return {
            "canonical_value": value,
            "canonical_json": canonical_json,
            "canonical_digest": canonical_digest,
            "input_digest": input_digest,
            "request_digest": request_digest,
        }, None

    def _verify_staged_candidate(
        self,
        *,
        operation_directory: _PinnedOperationDirectory,
        canonical_json: str,
        canonical_value: dict[str, Any],
    ) -> dict[str, Any]:
        independent = operation_directory.read_text("candidate.json")
        if (
            independent.get("status") != "ok"
            or independent.get("content") != canonical_json
        ):
            raise ValueError("sandbox candidate content verification failed")
        content = str(independent["content"])
        parsed = self._strict_loads(content)
        if parsed != canonical_value:
            raise ValueError("sandbox candidate JSON verification failed")
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_digest != independent.get("content_digest"):
            raise ValueError("sandbox candidate digest verification failed")
        return {
            "verification_digest": self._digest(
                {
                    "verifier": "strict_json_roundtrip.v1",
                    "content_digest": content_digest,
                    "scope_digest": independent.get("scope_digest"),
                }
            )
        }

    @contextmanager
    def _open_operation_root(
        self,
        operation_id: str,
    ) -> Iterator[_PinnedOperationDirectory]:
        if not _DIRECTORY or not _NOFOLLOW:
            raise ValueError(
                "secure descriptor-relative sandbox traversal is unavailable"
            )
        root = self.state_store.root.resolve()
        relative = self.sandbox_base.relative_to(root)
        root_before = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_before.st_mode):
            raise ValueError("sandbox state root is not a real directory")
        root_fd = os.open(root, _OPEN_DIRECTORY_FLAGS)
        anchors: list[_DirectoryAnchor] = []
        pinned: _PinnedOperationDirectory | None = None
        try:
            root_identity = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_identity.st_mode)
                or int(root_identity.st_dev) != int(root_before.st_dev)
                or int(root_identity.st_ino) != int(root_before.st_ino)
            ):
                raise ValueError("sandbox state root identity changed")

            current_fd = root_fd
            current_path = root
            for component in relative.parts:
                next_fd, identity = self._open_or_create_directory(
                    current_fd,
                    component,
                )
                anchor = _DirectoryAnchor(
                    parent_fd=current_fd,
                    name=component,
                    fd=next_fd,
                    device=int(identity.st_dev),
                    inode=int(identity.st_ino),
                )
                anchors.append(anchor)
                current_fd = next_fd
                current_path = current_path / component
                self._after_directory_component_pinned(current_path)
                self._assert_anchor(anchor)

            operation_name = (
                "op_"
                + hashlib.sha256(
                    operation_id.encode("utf-8")
                ).hexdigest()[:24]
            )
            try:
                os.mkdir(
                    operation_name,
                    mode=0o700,
                    dir_fd=current_fd,
                )
            except FileExistsError as exc:
                raise ValueError(
                    "sandbox repair operation root already exists"
                ) from exc
            created_identity = os.stat(
                operation_name,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            operation_fd = os.open(
                operation_name,
                _OPEN_DIRECTORY_FLAGS,
                dir_fd=current_fd,
            )
            operation_identity = os.fstat(operation_fd)
            if (
                not stat.S_ISDIR(created_identity.st_mode)
                or not stat.S_ISDIR(operation_identity.st_mode)
                or int(created_identity.st_dev)
                != int(operation_identity.st_dev)
                or int(created_identity.st_ino)
                != int(operation_identity.st_ino)
            ):
                os.close(operation_fd)
                raise ValueError(
                    "sandbox repair operation root identity changed"
                )
            anchors.append(
                _DirectoryAnchor(
                    parent_fd=current_fd,
                    name=operation_name,
                    fd=operation_fd,
                    device=int(operation_identity.st_dev),
                    inode=int(operation_identity.st_ino),
                )
            )
            pinned = _PinnedOperationDirectory(
                root_path=root,
                root_fd=root_fd,
                root_identity=root_identity,
                anchors=anchors,
                operation_path=current_path / operation_name,
                max_bytes=self.spec.max_candidate_bytes,
            )
            pinned.assert_attached()
            yield pinned
        finally:
            if pinned is not None:
                pinned.close()
            else:
                for anchor in reversed(anchors):
                    os.close(anchor.fd)
                os.close(root_fd)

    def _open_or_create_directory(
        self,
        parent_fd: int,
        component: str,
    ) -> tuple[int, os.stat_result]:
        self._validate_directory_component(component)
        try:
            directory_fd = os.open(
                component,
                _OPEN_DIRECTORY_FLAGS,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(
                component,
                _OPEN_DIRECTORY_FLAGS,
                dir_fd=parent_fd,
            )
        identity = os.fstat(directory_fd)
        if not stat.S_ISDIR(identity.st_mode):
            os.close(directory_fd)
            raise ValueError("sandbox repair base is not a real directory")
        return directory_fd, identity

    @staticmethod
    def _assert_anchor(anchor: _DirectoryAnchor) -> None:
        held = os.fstat(anchor.fd)
        linked = os.stat(
            anchor.name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or int(held.st_dev) != anchor.device
            or int(held.st_ino) != anchor.inode
            or int(linked.st_dev) != anchor.device
            or int(linked.st_ino) != anchor.inode
        ):
            raise ValueError("sandbox directory binding changed")

    @staticmethod
    def _validate_directory_component(component: str) -> None:
        if (
            not component
            or component in {".", ".."}
            or "/" in component
            or "\x00" in component
            or len(component.encode("utf-8")) > 255
        ):
            raise ValueError("sandbox directory component is invalid")

    def _after_directory_component_pinned(
        self,
        component_path: Path,
    ) -> None:
        """Deterministic race-test seam; production behavior is a no-op."""

        del component_path

    def _after_operation_directory_pinned(
        self,
        operation_path: Path,
    ) -> None:
        """Deterministic path-reopen race-test seam; production is a no-op."""

        del operation_path

    def _mode_context(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        if not isinstance(config, dict) or config.get("_state_corrupt"):
            raise ValueError("invalid ops_config")
        playbooks = config.get("playbooks")
        if playbooks is None:
            playbooks = {}
        if not isinstance(playbooks, dict):
            raise ValueError("invalid playbook config")
        selected = playbooks.get("sandbox_repair_json")
        if selected is None:
            selected = {}
        if not isinstance(selected, dict):
            raise ValueError("invalid sandbox repair config")
        if set(selected) - {"mode", "mode_epoch", "allowed_modes"}:
            raise ValueError("unknown sandbox repair config field")
        mode = str(selected.get("mode") or "shadow")
        allowed_modes = selected.get(
            "allowed_modes",
            ["disabled", "record_only", "shadow", "scoped_canary"],
        )
        mode_epoch = selected.get("mode_epoch", 0)
        if (
            mode
            not in {"disabled", "record_only", "shadow", "scoped_canary"}
            or not isinstance(allowed_modes, list)
            or allowed_modes
            != ["disabled", "record_only", "shadow", "scoped_canary"]
            or type(mode_epoch) is not int
            or mode_epoch < 0
        ):
            raise ValueError("invalid sandbox repair mode")
        return {
            "mode": mode,
            "mode_epoch": mode_epoch,
            "config_digest": self._digest(selected),
        }

    def _binding_digest(self, mode_context: dict[str, Any]) -> str:
        return self._digest(
            {
                "playbook_id": PLAYBOOK_ID,
                "spec_version": self.spec.version,
                "implementation_revision": IMPLEMENTATION_REVISION,
                "profile": JSON_SANDBOX_REPAIR_PROFILE.to_public_dict(),
                "state_root": str(self.state_store.root.resolve()),
                "sandbox_base": str(self.sandbox_base),
                "mode_context": mode_context,
            }
        )

    def _valid_state(self, state: Any) -> bool:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt")
            or state.get("schema_version") != STATE_SCHEMA_VERSION
        ):
            return False
        operations = state.get("operations")
        request_index = state.get("request_index")
        if (
            not isinstance(operations, dict)
            or not isinstance(request_index, dict)
            or len(operations) > MAX_OPERATIONS
        ):
            return False
        for operation_id, record in operations.items():
            if not self._valid_record(operation_id, record):
                return False
        for digest, operation_id in request_index.items():
            if (
                not _DIGEST.fullmatch(str(digest or ""))
                or operation_id not in operations
                or operations[operation_id].get("request_digest") != digest
            ):
                return False
        last_id = state.get("last_operation_id")
        return last_id in {None, ""} or (
            isinstance(last_id, str) and last_id in operations
        )

    def _valid_record(self, operation_id: Any, record: Any) -> bool:
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(record, dict)
            or record.get("operation_id") != operation_id
            or record.get("profile_id")
            != JSON_SANDBOX_REPAIR_PROFILE.profile_id
            or record.get("promotion_authorized") is not False
            or record.get("attempt_number") != 1
            or not _DIGEST.fullmatch(str(record.get("request_digest") or ""))
            or not _DIGEST.fullmatch(str(record.get("input_digest") or ""))
            or not _DIGEST.fullmatch(
                str(record.get("canonical_digest") or "")
            )
            or not _DIGEST.fullmatch(
                str(record.get("target_binding_digest") or "")
            )
            or type(record.get("mode_epoch")) is not int
            or record.get("mode_epoch") < 0
            or record.get("mode") != "scoped_canary"
            or self._parse_time(str(record.get("claimed_at") or "")) is None
        ):
            return False
        state = str(record.get("state") or "")
        status = str(record.get("status") or "")
        outcomes = {
            "claimed": {"attempt_in_progress"},
            "completed": {"sandbox_verified_candidate"},
            "failed": {"sandbox_write_failed"},
            "indeterminate": {"indeterminate"},
            "revoked": {"scope_changed"},
        }
        if state not in outcomes or status not in outcomes[state]:
            return False
        if state == "claimed":
            return (
                record.get("outcome") is None
                and record.get("completed_at") is None
                and record.get("evidence") is None
            )
        completed = self._parse_time(str(record.get("completed_at") or ""))
        claimed = self._parse_time(str(record.get("claimed_at") or ""))
        if completed is None or claimed is None or completed < claimed:
            return False
        if record.get("outcome") != status:
            return False
        evidence = record.get("evidence")
        if state == "completed":
            return bool(
                isinstance(evidence, dict)
                and _DIGEST.fullmatch(
                    str(evidence.get("verification_digest") or "")
                )
                and evidence.get("canonical_digest")
                == record.get("canonical_digest")
            )
        return evidence is None

    def _public_result(
        self,
        record: dict[str, Any] | None,
        *,
        mode: str,
        status: str | None = None,
        candidate_status: str | None = None,
        fault_code: str | None = None,
    ) -> dict[str, Any]:
        selected_status = status or str(
            (record or {}).get("status") or "idle"
        )
        if selected_status in {
            "blocked",
            "fault",
            "indeterminate",
            "operation_conflict",
            "capacity_exhausted",
        }:
            effective_level = AutonomyLevel.A0.value
        elif mode == "scoped_canary":
            effective_level = AutonomyLevel.A3.value
        elif mode in {"record_only", "shadow"}:
            effective_level = AutonomyLevel.A1.value
        else:
            effective_level = AutonomyLevel.A0.value
        return {
            "playbook_id": PLAYBOOK_ID,
            "status": selected_status,
            "mode": mode,
            "spec": self.spec.to_public_dict(),
            "autonomy_profile": JSON_SANDBOX_REPAIR_PROFILE.to_public_dict(),
            "effective_autonomy_level": effective_level,
            "operation_state": (record or {}).get("state"),
            "attempt_count": (
                int((record or {}).get("attempt_number") or 0)
            ),
            "candidate_status": candidate_status
            or (record or {}).get("outcome"),
            "evidence_status": (
                "verified_in_private_sandbox"
                if (record or {}).get("state") == "completed"
                else "unverified"
            ),
            "production_effect_status": "not_started",
            "promotion_authorized": False,
            "automatic_effects": (
                [
                    "write canonical JSON candidate inside Veyra private sandbox",
                    "independently verify the private sandbox candidate",
                ]
                if mode == "scoped_canary"
                else []
            ),
            "forbidden_effects": [
                "workspace read or mutation",
                "Agent invocation",
                "shell execution",
                "network access",
                "process restart",
                "provider or model switch",
                "candidate promotion",
            ],
            **({"fault_code": fault_code} if fault_code else {}),
        }

    def _fault(self, code: str) -> dict[str, Any]:
        return {
            "playbook_id": PLAYBOOK_ID,
            "status": "fault",
            "mode": "fail_closed",
            "fault_code": str(code or "fault")[:80],
            "spec": self.spec.to_public_dict(),
            "autonomy_profile": JSON_SANDBOX_REPAIR_PROFILE.to_public_dict(),
            "effective_autonomy_level": AutonomyLevel.A0.value,
            "operation_state": None,
            "attempt_count": 0,
            "candidate_status": None,
            "evidence_status": "unverified",
            "production_effect_status": "not_started",
            "promotion_authorized": False,
            "automatic_effects": [],
            "forbidden_effects": [
                "workspace read or mutation",
                "Agent invocation",
                "shell execution",
                "network access",
                "process restart",
                "provider or model switch",
                "candidate promotion",
            ],
        }

    def _claim_expired(
        self,
        record: dict[str, Any],
        *,
        now: datetime,
    ) -> bool:
        claimed = self._parse_time(str(record.get("claimed_at") or ""))
        return claimed is None or now >= claimed + timedelta(
            seconds=self.spec.operation_timeout_seconds
        )

    @staticmethod
    def _strict_loads(value: str) -> Any:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            selected: dict[str, Any] = {}
            for key, item in pairs:
                if key in selected:
                    raise ValueError("duplicate JSON object key")
                selected[key] = item
            return selected

        def reject_constant(value: str) -> Any:
            raise ValueError(f"non-finite JSON constant: {value}")

        return json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (
            parsed
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=timezone.utc)
        )

    def _now_iso(self) -> str:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.isoformat()

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "IMPLEMENTATION_REVISION",
    "JSON_SANDBOX_REPAIR_PROFILE",
    "JsonSandboxRepairPlaybook",
    "JsonSandboxRepairRequest",
    "JsonSandboxRepairSpec",
    "PLAYBOOK_ID",
]
