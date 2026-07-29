from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable, Literal

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.extension_spec import (
    EXTENSION_POLICY_REVISION,
    ExtensionSpec,
    canonical_json_bytes,
    parse_extension_spec,
)


STATE_FILE = "phase6_extension_spec_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.extension_spec_state.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.extension_spec_status.v1"
PUBLIC_CANDIDATE_SCHEMA = "veyra.phase6.extension_spec_candidate.v1"
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_spec_candidate_list.v1"
PUBLIC_INTEGRITY_SCHEMA = "veyra.phase6.extension_spec_integrity.v1"
MAX_CANDIDATES = 200
MAX_OPERATIONS = 2_000
MAX_NON_TERMINAL_OPERATIONS = MAX_OPERATIONS - MAX_CANDIDATES
MAX_HISTORY = 32
MAX_EXPIRY_DAYS = 90

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    {
        "SPEC_QUARANTINED",
        "SPEC_GATE_PASSED",
        "REJECTED",
        "REVOKED",
    }
)
_RECORD_SCHEMA_VERSION = "veyra.phase6.extension_spec_record.v1"
_CONTROL_SOURCE = "explicit_local_control_plane"
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "user_id",
        "workspace_id",
        "extension_id",
        "extension_version",
        "spec_digest",
        "spec",
        "stage",
        "revision",
        "expires_at",
        "review",
        "revocation",
        "history",
        "created_at",
        "updated_at",
    }
)
_REVIEW_FIELDS = frozenset(
    {
        "decision",
        "reason_digest",
        "review_source",
        "execution_authorized",
        "promotion_authorized",
        "recorded_at",
    }
)
_REVOCATION_FIELDS = frozenset(
    {
        "reason_digest",
        "source",
        "recorded_at",
    }
)
ReviewDecision = Literal[
    "accept_for_future_isolated_generation",
    "reject",
]


class ExtensionSpecQuarantineError(RuntimeError):
    """Base class for bounded ExtensionSpec lifecycle failures."""


class ExtensionSpecConflictError(ExtensionSpecQuarantineError):
    """A CAS, semantic identity, or operation binding conflicted."""


class ExtensionSpecNotFoundError(ExtensionSpecQuarantineError):
    """A candidate was absent or outside the caller's logical owner scope."""


class ExtensionSpecStorageError(ExtensionSpecQuarantineError):
    """Private ExtensionSpec state is corrupt or unavailable."""


class ExtensionSpecQuarantine:
    """Specification-only extension quarantine with zero execution authority.

    The runtime accepts only a strict ``ExtensionSpec`` whose artifact is
    explicitly ``not_generated``.  It never imports, compiles, executes,
    signs, installs, registers, or promotes an extension.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        try:
            state = self._read_valid_state()
            candidates = state["candidates"]
            counts: dict[str, int] = {
                "SPEC_QUARANTINED": 0,
                "SPEC_GATE_PASSED": 0,
                "REJECTED": 0,
                "REVOKED": 0,
                "EXPIRED": 0,
            }
            for record in candidates.values():
                stage = self._effective_stage(record)
                counts[stage] = counts.get(stage, 0) + 1
            storage = {
                "status": "ready",
                "issue": None,
                "candidate_count": len(candidates),
                "operation_count": len(state["operation_index"]),
                "counts": counts,
            }
            operational_health = "available"
        except Exception as exc:
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "candidate_count": 0,
                "operation_count": 0,
                "counts": {},
            }
            operational_health = "degraded"
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2a",
            "status": (
                "technical_complete_specification_only"
                if operational_health == "available"
                else "fail_closed"
            ),
            "operational_health": operational_health,
            "lifecycle": [
                "SPEC_QUARANTINED",
                "SPEC_GATE_PASSED",
                "REJECTED",
                "REVOKED",
            ],
            "completion_scope": (
                "strict ExtensionSpec manifest quarantine only"
            ),
            "storage": storage,
            "policy_revision": EXTENSION_POLICY_REVISION,
            "authority": self._authority(),
            "next_stage": {
                "isolated_generation": "not_implemented",
                "behavior_tests": "not_implemented",
                "signature_verification": "not_implemented",
                "read_only_execution_canary": "not_implemented",
                "scoped_execution_canary": "not_implemented",
                "promotion": "not_implemented",
            },
        }

    def quarantine(
        self,
        *,
        spec: ExtensionSpec | dict[str, Any],
        expected_spec_digest: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace = self._owner_scope(
            user_id,
            workspace_id,
        )
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        selected_digest = str(expected_spec_digest or "")
        if not _DIGEST.fullmatch(selected_digest):
            raise ValueError(
                "expected_spec_digest must be a SHA-256 digest"
            )
        parsed = parse_extension_spec(spec)
        self._validate_new_expiry(parsed.expires_at)
        canonical_spec = parsed.canonical_dict()
        actual_digest = parsed.digest()
        if actual_digest != selected_digest:
            raise ValueError("ExtensionSpec digest mismatch")
        identity_key = self._identity_key(
            selected_user,
            selected_workspace,
            parsed.extension_id,
            parsed.version,
        )
        candidate_id = self._candidate_id(
            identity_key,
            actual_digest,
        )
        request_digest = self._digest(
            {
                "kind": "quarantine",
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "operation_id": selected_operation,
                "spec_digest": actual_digest,
            }
        )
        operation_key = self._operation_key(
            selected_user,
            selected_workspace,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        operation_replayed = False
        replayed_operation: dict[str, Any] | None = None
        candidate_existing = False
        changed = False

        def admit(state: dict[str, Any]) -> None:
            nonlocal selected
            nonlocal operation_replayed
            nonlocal replayed_operation
            nonlocal candidate_existing
            nonlocal changed
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            writer_snapshot = parse_extension_spec(canonical_spec)
            if writer_snapshot.digest() != actual_digest:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec changed before durable admission"
                )
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="quarantine",
                candidate_id=candidate_id,
            )
            if replay is not None:
                selected = self._record_by_id(
                    state,
                    replay["candidate_id"],
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                )
                operation_replayed = True
                replayed_operation = dict(replay)
                return

            indexed = state["identity_index"].get(identity_key)
            if indexed is not None:
                current = self._record_by_id(state, str(indexed))
                if current["spec_digest"] != actual_digest:
                    raise ExtensionSpecConflictError(
                        "extension identity already has another spec digest; "
                        "increment the extension version"
                    )
                self._require_owner(
                    current,
                    selected_user,
                    selected_workspace,
                )
                candidate_existing = True
                selected = current
                self._record_operation(
                    state,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    kind="quarantine",
                    candidate_id=current["candidate_id"],
                    result_revision=current["revision"],
                )
                changed = True
                return

            if len(state["candidates"]) >= MAX_CANDIDATES:
                raise ExtensionSpecConflictError(
                    "ExtensionSpec quarantine capacity is exhausted"
                )
            created_at = self._now_iso()
            record = {
                "schema_version": _RECORD_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "extension_id": parsed.extension_id,
                "extension_version": parsed.version,
                "spec_digest": actual_digest,
                "spec": canonical_spec,
                "stage": "SPEC_QUARANTINED",
                "revision": 1,
                "expires_at": parsed.expires_at,
                "review": None,
                "revocation": None,
                "history": [
                    {
                        "revision": 1,
                        "transition": "spec_quarantined",
                        "recorded_at": created_at,
                    }
                ],
                "created_at": created_at,
                "updated_at": created_at,
            }
            state["candidates"][candidate_id] = record
            state["identity_index"][identity_key] = candidate_id
            state["candidate_count"] = len(state["candidates"])
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="quarantine",
                candidate_id=candidate_id,
                result_revision=1,
            )
            selected = record
            changed = True

        try:
            self.state_store.mutate_json(STATE_FILE, admit)
        except ExtensionSpecQuarantineError:
            raise
        except Exception as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec quarantine is unavailable"
            ) from exc
        if selected is None:
            raise ExtensionSpecStorageError(
                "ExtensionSpec admission produced no candidate"
            )
        if changed:
            self._audit(
                route="phase6_extension_spec_quarantine",
                status=(
                    "existing" if candidate_existing else "quarantined"
                ),
                record=selected,
            )
        return self._public_candidate(
            selected,
            operation_replayed=operation_replayed,
            replayed_operation=replayed_operation,
            candidate_existing=candidate_existing,
        )

    def review(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        decision: ReviewDecision,
        reason: str,
    ) -> dict[str, Any]:
        selected_candidate = self._required_id(
            candidate_id,
            "candidate_id",
        )
        selected_user, selected_workspace = self._owner_scope(
            user_id,
            workspace_id,
        )
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        selected_revision = self._required_revision(expected_revision)
        selected_decision = str(decision or "")
        if selected_decision not in {
            "accept_for_future_isolated_generation",
            "reject",
        }:
            raise ValueError("unsupported ExtensionSpec review decision")
        selected_reason = self._required_text(reason, "reason", 1_200)
        reason_digest = hashlib.sha256(
            selected_reason.encode("utf-8")
        ).hexdigest()
        request_digest = self._digest(
            {
                "kind": "review",
                "candidate_id": selected_candidate,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "expected_revision": selected_revision,
                "operation_id": selected_operation,
                "decision": selected_decision,
                "reason_digest": reason_digest,
            }
        )
        operation_key = self._operation_key(
            selected_user,
            selected_workspace,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        operation_replayed = False
        replayed_operation: dict[str, Any] | None = None
        changed = False

        def apply_review(state: dict[str, Any]) -> None:
            nonlocal selected
            nonlocal operation_replayed
            nonlocal replayed_operation
            nonlocal changed
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="review",
                candidate_id=selected_candidate,
            )
            if replay is not None:
                selected = self._record_by_id(
                    state,
                    replay["candidate_id"],
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                )
                operation_replayed = True
                replayed_operation = dict(replay)
                return
            record = self._record_by_id(state, selected_candidate)
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
            )
            if record["revision"] != selected_revision:
                raise ExtensionSpecConflictError(
                    "ExtensionSpec candidate revision conflict"
                )
            if record["stage"] != "SPEC_QUARANTINED":
                raise ExtensionSpecConflictError(
                    "only a quarantined spec can be reviewed"
                )
            if (
                selected_decision
                == "accept_for_future_isolated_generation"
                and self._is_expired(record)
            ):
                raise ExtensionSpecConflictError(
                    "expired ExtensionSpec cannot pass the gate"
                )
            # Reparse and rehash under the state writer before accepting.
            self._validate_record(record)
            next_revision = selected_revision + 1
            next_stage = (
                "SPEC_GATE_PASSED"
                if selected_decision
                == "accept_for_future_isolated_generation"
                else "REJECTED"
            )
            now = self._now_iso()
            record["stage"] = next_stage
            record["revision"] = next_revision
            record["review"] = {
                "decision": selected_decision,
                "reason_digest": reason_digest,
                "review_source": _CONTROL_SOURCE,
                "execution_authorized": False,
                "promotion_authorized": False,
                "recorded_at": now,
            }
            record["updated_at"] = now
            self._append_history(
                record,
                {
                    "revision": next_revision,
                    "transition": (
                        "spec_gate_passed"
                        if next_stage == "SPEC_GATE_PASSED"
                        else "spec_rejected"
                    ),
                    "recorded_at": now,
                    "reason_digest": reason_digest,
                },
            )
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="review",
                candidate_id=selected_candidate,
                result_revision=next_revision,
                terminal_control=next_stage == "REJECTED",
            )
            selected = record
            changed = True

        self._mutate(apply_review)
        if selected is None:
            raise ExtensionSpecStorageError(
                "ExtensionSpec review produced no candidate"
            )
        if changed:
            self._audit(
                route="phase6_extension_spec_review",
                status=str(selected["stage"]).lower(),
                record=selected,
            )
        return self._public_candidate(
            selected,
            operation_replayed=operation_replayed,
            replayed_operation=replayed_operation,
        )

    def revoke(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        selected_candidate = self._required_id(
            candidate_id,
            "candidate_id",
        )
        selected_user, selected_workspace = self._owner_scope(
            user_id,
            workspace_id,
        )
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        selected_revision = self._required_revision(expected_revision)
        selected_reason = self._required_text(reason, "reason", 1_200)
        reason_digest = hashlib.sha256(
            selected_reason.encode("utf-8")
        ).hexdigest()
        request_digest = self._digest(
            {
                "kind": "revoke",
                "candidate_id": selected_candidate,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "expected_revision": selected_revision,
                "operation_id": selected_operation,
                "reason_digest": reason_digest,
            }
        )
        operation_key = self._operation_key(
            selected_user,
            selected_workspace,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        operation_replayed = False
        replayed_operation: dict[str, Any] | None = None
        changed = False

        def apply_revocation(state: dict[str, Any]) -> None:
            nonlocal selected
            nonlocal operation_replayed
            nonlocal replayed_operation
            nonlocal changed
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="revoke",
                candidate_id=selected_candidate,
            )
            if replay is not None:
                selected = self._record_by_id(
                    state,
                    replay["candidate_id"],
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                )
                operation_replayed = True
                replayed_operation = dict(replay)
                return
            record = self._record_by_id(state, selected_candidate)
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
            )
            if record["revision"] != selected_revision:
                raise ExtensionSpecConflictError(
                    "ExtensionSpec candidate revision conflict"
                )
            if record["stage"] == "REJECTED":
                raise ExtensionSpecConflictError(
                    "a rejected ExtensionSpec cannot change lifecycle"
                )
            if record["stage"] == "REVOKED":
                raise ExtensionSpecConflictError(
                    "ExtensionSpec is already revoked; replay the original "
                    "revocation operation"
                )
            next_revision = selected_revision + 1
            now = self._now_iso()
            record["stage"] = "REVOKED"
            record["revision"] = next_revision
            record["revocation"] = {
                "reason_digest": reason_digest,
                    "source": _CONTROL_SOURCE,
                "recorded_at": now,
            }
            record["updated_at"] = now
            self._append_history(
                record,
                {
                    "revision": next_revision,
                    "transition": "spec_revoked",
                    "recorded_at": now,
                    "reason_digest": reason_digest,
                },
            )
            changed = True
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="revoke",
                candidate_id=selected_candidate,
                result_revision=record["revision"],
                terminal_control=True,
            )
            selected = record

        self._mutate(apply_revocation)
        if selected is None:
            raise ExtensionSpecStorageError(
                "ExtensionSpec revocation produced no candidate"
            )
        if changed:
            self._audit(
                route="phase6_extension_spec_revoke",
                status="revoked",
                record=selected,
            )
        return self._public_candidate(
            selected,
            operation_replayed=operation_replayed,
            replayed_operation=replayed_operation,
        )

    def get(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_id(candidate_id, "candidate_id"),
        )
        self._require_owner(
            record,
            selected_user,
            selected_workspace,
        )
        return self._public_candidate(record)

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        records = [
            record
            for record in state["candidates"].values()
            if record["user_id"] == selected_user
            and record["workspace_id"] == selected_workspace
        ]
        records.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("candidate_id") or ""),
            ),
            reverse=True,
        )
        projected = [
            self._public_candidate(record)
            for record in records[:limit]
        ]
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(projected),
            "candidates": projected,
            "authority": self._authority(),
        }

    def integrity(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_id(candidate_id, "candidate_id"),
        )
        self._require_owner(
            record,
            selected_user,
            selected_workspace,
        )
        # The record has already been reparsed and rehashed by state
        # validation.  This integrity check intentionally performs no
        # artifact or candidate execution.
        stage = self._effective_stage(record)
        admissible = stage in {
            "SPEC_QUARANTINED",
            "SPEC_GATE_PASSED",
        }
        return {
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "candidate_id": record["candidate_id"],
            "candidate_revision": record["revision"],
            "status": (
                "spec_integrity_passed"
                if admissible
                else "blocked"
            ),
            "stage": stage,
            "spec_digest": record["spec_digest"],
            "spec_integrity_status": "validated",
            "artifact_integrity_status": "unverified",
            "behavior_verification_status": "not_started",
            "execution_status": "not_started",
            "signature_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "state_mutated": False,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            self._validate_state(state)
            return state
        except ExtensionSpecQuarantineError:
            raise
        except Exception as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec quarantine state is unavailable"
            ) from exc

    def _read_owner_state(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str, dict[str, Any]]:
        selected_user = self._required_text(
            user_id,
            "user_id",
            240,
        )
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
            240,
        )
        try:
            snapshot = self.state_store.read_snapshot(
                ["local_world.json", STATE_FILE]
            )
            self._assert_workspace_document(
                snapshot["local_world.json"],
                selected_workspace,
            )
            state = snapshot[STATE_FILE]
            self._validate_state(state)
        except ExtensionSpecQuarantineError:
            raise
        except Exception as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec owner-scoped state is unavailable"
            ) from exc
        return selected_user, selected_workspace, state

    def _mutate(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionSpecQuarantineError:
            raise
        except Exception as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec quarantine state is unavailable"
            ) from exc

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("candidates"), dict)
            or not isinstance(state.get("identity_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("candidate_count")) is not int
            or state.get("candidate_count")
            != len(state.get("candidates", {}))
            or len(state.get("candidates", {})) > MAX_CANDIDATES
            or len(state.get("operation_index", {})) > MAX_OPERATIONS
        ):
            raise ExtensionSpecStorageError(
                "ExtensionSpec quarantine state is invalid"
            )
        candidates = state["candidates"]
        expected_identity_index: dict[str, str] = {}
        for key, record in candidates.items():
            if (
                not isinstance(key, str)
                or not isinstance(record, dict)
                or record.get("candidate_id") != key
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate index is invalid"
                )
            self._validate_record(record)
            identity_key = self._identity_key(
                str(record["user_id"]),
                str(record["workspace_id"]),
                str(record["extension_id"]),
                int(record["extension_version"]),
            )
            expected_candidate_id = self._candidate_id(
                identity_key,
                str(record["spec_digest"]),
            )
            if key != expected_candidate_id:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate identity binding is invalid"
                )
            if identity_key in expected_identity_index:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec identity is duplicated"
                )
            expected_identity_index[identity_key] = key
        if state["identity_index"] != expected_identity_index:
            raise ExtensionSpecStorageError(
                "ExtensionSpec identity index is invalid"
            )
        non_terminal_operation_count = 0
        terminal_operation_candidates: set[str] = set()
        for operation_key, operation in state[
            "operation_index"
        ].items():
            candidate = (
                candidates.get(operation.get("candidate_id"))
                if isinstance(operation, dict)
                else None
            )
            if (
                not isinstance(operation_key, str)
                or not _DIGEST.fullmatch(operation_key)
                or not isinstance(operation, dict)
                or set(operation)
                != {
                    "request_digest",
                    "kind",
                    "candidate_id",
                    "result_revision",
                    "result_stage",
                    "owner_scope_digest",
                    "terminal_control",
                    "recorded_at",
                }
                or not isinstance(candidate, dict)
                or operation.get("kind")
                not in {"quarantine", "review", "revoke"}
                or not _DIGEST.fullmatch(
                    str(operation.get("request_digest") or "")
                )
                or type(operation.get("result_revision")) is not int
                or operation["result_revision"] < 1
                or operation["result_revision"] > candidate["revision"]
                or operation.get("result_stage")
                not in _STAGES
                or type(operation.get("terminal_control")) is not bool
                or operation.get("owner_scope_digest")
                != self._owner_scope_digest(
                    str(candidate["user_id"]),
                    str(candidate["workspace_id"]),
                )
                or not isinstance(operation.get("recorded_at"), str)
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec operation index is invalid"
                )
            try:
                self._parse_time(operation["recorded_at"])
            except Exception as exc:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec operation index is invalid"
                ) from exc
            if operation["result_stage"] != self._stage_at_revision(
                candidate,
                operation["result_revision"],
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec operation result binding is invalid"
                )
            terminal_control = bool(operation["terminal_control"])
            expected_terminal_control = (
                operation["kind"] == "revoke"
                or (
                    operation["kind"] == "review"
                    and operation["result_stage"] == "REJECTED"
                )
            )
            if terminal_control != expected_terminal_control:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec operation control binding is invalid"
                )
            if terminal_control:
                candidate_id = str(operation["candidate_id"])
                if candidate_id in terminal_operation_candidates:
                    raise ExtensionSpecStorageError(
                        "ExtensionSpec terminal operation is duplicated"
                    )
                terminal_operation_candidates.add(candidate_id)
            else:
                non_terminal_operation_count += 1
        if (
            non_terminal_operation_count
            > MAX_NON_TERMINAL_OPERATIONS
            or len(terminal_operation_candidates) > len(candidates)
        ):
            raise ExtensionSpecStorageError(
                "ExtensionSpec operation capacity binding is invalid"
            )

    def _validate_record(self, record: dict[str, Any]) -> None:
        try:
            candidate_id = record["candidate_id"]
            user_id = record["user_id"]
            workspace_id = record["workspace_id"]
            extension_id = record["extension_id"]
            extension_version = record["extension_version"]
            spec_digest = record["spec_digest"]
            stage = record["stage"]
            revision = record["revision"]
            history = record["history"]
            spec = parse_extension_spec(record["spec"])
            created_at = record["created_at"]
            updated_at = record["updated_at"]
            expires_at = record["expires_at"]
            created_time = self._parse_time(created_at)
            updated_time = self._parse_time(updated_at)
            expires_time = self._parse_time(expires_at)
        except Exception as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec candidate record is invalid"
            ) from exc
        if (
            set(record) != _RECORD_FIELDS
            or record.get("schema_version") != _RECORD_SCHEMA_VERSION
            or not isinstance(candidate_id, str)
            or not _OPAQUE_ID.fullmatch(candidate_id)
            or not self._stored_owner_text_is_valid(user_id)
            or not self._stored_owner_text_is_valid(workspace_id)
            or not isinstance(extension_id, str)
            or extension_id != spec.extension_id
            or type(extension_version) is not int
            or extension_version != spec.version
            or not isinstance(spec_digest, str)
            or not _DIGEST.fullmatch(spec_digest)
            or spec.digest() != spec_digest
            or expires_at != spec.expires_at
            or stage not in _STAGES
            or type(revision) is not int
            or revision < 1
            or not isinstance(history, list)
            or not 1 <= len(history) <= MAX_HISTORY
            or len(history) != revision
            or not isinstance(created_at, str)
            or not isinstance(updated_at, str)
            or not isinstance(expires_at, str)
            or updated_time < created_time
            or expires_time <= created_time
        ):
            raise ExtensionSpecStorageError(
                "ExtensionSpec candidate record failed integrity checks"
            )

        transitions: list[str] = []
        history_times: list[datetime] = []
        reason_digests: list[str | None] = []
        for index, item in enumerate(history, start=1):
            expected_fields = (
                {"revision", "transition", "recorded_at"}
                if index == 1
                else {
                    "revision",
                    "transition",
                    "recorded_at",
                    "reason_digest",
                }
            )
            try:
                recorded_at = item["recorded_at"]
                recorded_time = self._parse_time(recorded_at)
            except Exception as exc:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate history is invalid"
                ) from exc
            reason_digest = item.get("reason_digest")
            if (
                not isinstance(item, dict)
                or set(item) != expected_fields
                or item.get("revision") != index
                or not isinstance(item.get("transition"), str)
                or not isinstance(recorded_at, str)
                or (
                    index > 1
                    and (
                        not isinstance(reason_digest, str)
                        or not _DIGEST.fullmatch(reason_digest)
                    )
                )
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate history is invalid"
                )
            transitions.append(item["transition"])
            history_times.append(recorded_time)
            reason_digests.append(
                reason_digest if isinstance(reason_digest, str) else None
            )
        if (
            history[0]["recorded_at"] != created_at
            or history[-1]["recorded_at"] != updated_at
            or history_times != sorted(history_times)
            or transitions[0] != "spec_quarantined"
        ):
            raise ExtensionSpecStorageError(
                "ExtensionSpec candidate history is invalid"
            )

        review = record.get("review")
        revocation = record.get("revocation")
        review_decision: str | None = None
        if review is not None:
            if (
                not isinstance(review, dict)
                or set(review) != _REVIEW_FIELDS
                or review.get("decision")
                not in {
                    "accept_for_future_isolated_generation",
                    "reject",
                }
                or not _DIGEST.fullmatch(
                    str(review.get("reason_digest") or "")
                )
                or review.get("review_source") != _CONTROL_SOURCE
                or review.get("execution_authorized") is not False
                or review.get("promotion_authorized") is not False
                or not isinstance(review.get("recorded_at"), str)
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate review is invalid"
                )
            try:
                self._parse_time(review["recorded_at"])
            except Exception as exc:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate review is invalid"
                ) from exc
            review_decision = str(review["decision"])

        if revocation is not None:
            if (
                not isinstance(revocation, dict)
                or set(revocation) != _REVOCATION_FIELDS
                or not _DIGEST.fullmatch(
                    str(revocation.get("reason_digest") or "")
                )
                or revocation.get("source") != _CONTROL_SOURCE
                or not isinstance(revocation.get("recorded_at"), str)
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate revocation is invalid"
                )
            try:
                self._parse_time(revocation["recorded_at"])
            except Exception as exc:
                raise ExtensionSpecStorageError(
                    "ExtensionSpec candidate revocation is invalid"
                ) from exc

        expected_transitions: list[str]
        if stage == "SPEC_QUARANTINED":
            expected_transitions = ["spec_quarantined"]
            lifecycle_valid = (
                revision == 1
                and review is None
                and revocation is None
            )
        elif stage == "SPEC_GATE_PASSED":
            expected_transitions = [
                "spec_quarantined",
                "spec_gate_passed",
            ]
            lifecycle_valid = (
                revision == 2
                and review_decision
                == "accept_for_future_isolated_generation"
                and revocation is None
            )
        elif stage == "REJECTED":
            expected_transitions = [
                "spec_quarantined",
                "spec_rejected",
            ]
            lifecycle_valid = (
                revision == 2
                and review_decision == "reject"
                and revocation is None
            )
        else:
            if review is None:
                expected_transitions = [
                    "spec_quarantined",
                    "spec_revoked",
                ]
                lifecycle_valid = revision == 2
            else:
                expected_transitions = [
                    "spec_quarantined",
                    "spec_gate_passed",
                    "spec_revoked",
                ]
                lifecycle_valid = (
                    revision == 3
                    and review_decision
                    == "accept_for_future_isolated_generation"
                )
            lifecycle_valid = lifecycle_valid and revocation is not None
        if not lifecycle_valid or transitions != expected_transitions:
            raise ExtensionSpecStorageError(
                "ExtensionSpec lifecycle binding is invalid"
            )

        if review is not None:
            if (
                review["recorded_at"] != history[1]["recorded_at"]
                or review["reason_digest"] != reason_digests[1]
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec review history binding is invalid"
                )
        if revocation is not None:
            if (
                revocation["recorded_at"] != history[-1]["recorded_at"]
                or revocation["reason_digest"] != reason_digests[-1]
            ):
                raise ExtensionSpecStorageError(
                    "ExtensionSpec revocation history binding is invalid"
                )

    def _record_by_id(
        self,
        state: dict[str, Any],
        candidate_id: str,
    ) -> dict[str, Any]:
        record = state["candidates"].get(candidate_id)
        if not isinstance(record, dict):
            raise ExtensionSpecNotFoundError(
                "ExtensionSpec candidate not found"
            )
        self._validate_record(record)
        return record

    @staticmethod
    def _stage_at_revision(
        record: dict[str, Any],
        revision: int,
    ) -> str:
        try:
            transition = record["history"][revision - 1]["transition"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ExtensionSpecStorageError(
                "ExtensionSpec operation revision is invalid"
            ) from exc
        stage_by_transition = {
            "spec_quarantined": "SPEC_QUARANTINED",
            "spec_gate_passed": "SPEC_GATE_PASSED",
            "spec_rejected": "REJECTED",
            "spec_revoked": "REVOKED",
        }
        stage = stage_by_transition.get(transition)
        if stage is None:
            raise ExtensionSpecStorageError(
                "ExtensionSpec operation transition is invalid"
            )
        return stage

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        user_id: str,
        workspace_id: str,
    ) -> None:
        if (
            record.get("user_id") != user_id
            or record.get("workspace_id") != workspace_id
        ):
            raise ExtensionSpecNotFoundError(
                "ExtensionSpec candidate not found"
            )

    @staticmethod
    def _stored_owner_text_is_valid(value: Any) -> bool:
        return (
            isinstance(value, str)
            and value == value.strip()
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-8")) <= 240
        )

    def _owner_scope(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str]:
        selected_user = self._required_text(
            user_id,
            "user_id",
            240,
        )
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
            240,
        )
        self._assert_current_workspace(selected_workspace)
        return selected_user, selected_workspace

    def _assert_current_workspace(
        self,
        workspace_id: str,
    ) -> None:
        self._assert_workspace_document(
            self.state_store.read_json("local_world.json"),
            workspace_id,
        )

    @staticmethod
    def _assert_workspace_document(
        local_world: dict[str, Any],
        workspace_id: str,
    ) -> None:
        current_workspace = str(
            local_world.get("current_project") or ""
        ).strip()
        if (
            not current_workspace
            or workspace_id != current_workspace
        ):
            raise ExtensionSpecConflictError(
                "workspace must match the current local Veyra scope"
            )

    def _operation_replay(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        existing = state["operation_index"].get(operation_key)
        if existing is None:
            return None
        if (
            not isinstance(existing, dict)
            or existing.get("request_digest") != request_digest
            or existing.get("kind") != kind
            or existing.get("candidate_id") != candidate_id
        ):
            raise ExtensionSpecConflictError(
                "operation_id is already bound to another command"
            )
        return existing

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        candidate_id: str,
        result_revision: int,
        terminal_control: bool = False,
    ) -> None:
        if operation_key in state["operation_index"]:
            raise ExtensionSpecConflictError(
                "operation_id is already recorded"
            )
        operation_count = len(state["operation_index"])
        if operation_count >= MAX_OPERATIONS:
            raise ExtensionSpecConflictError(
                "ExtensionSpec operation capacity is exhausted"
            )
        if (
            not terminal_control
            and operation_count >= MAX_NON_TERMINAL_OPERATIONS
        ):
            raise ExtensionSpecConflictError(
                "ExtensionSpec non-terminal operation capacity is exhausted; "
                "reserved terminal-control capacity remains available"
            )
        record = self._record_by_id(state, candidate_id)
        state["operation_index"][operation_key] = {
            "request_digest": request_digest,
            "kind": kind,
            "candidate_id": candidate_id,
            "result_revision": result_revision,
            "result_stage": self._stage_at_revision(
                record,
                result_revision,
            ),
            "owner_scope_digest": self._owner_scope_digest(
                str(record["user_id"]),
                str(record["workspace_id"]),
            ),
            "terminal_control": terminal_control,
            "recorded_at": self._now_iso(),
        }

    @staticmethod
    def _append_history(
        record: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        history = record.get("history")
        if not isinstance(history, list):
            raise ExtensionSpecStorageError(
                "ExtensionSpec candidate history is invalid"
            )
        history.append(item)
        if len(history) > MAX_HISTORY:
            raise ExtensionSpecConflictError(
                "ExtensionSpec candidate history capacity is exhausted"
            )

    def _public_candidate(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        replayed_operation: dict[str, Any] | None = None,
        candidate_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        stage = self._effective_stage(record)
        review = (
            record.get("review")
            if isinstance(record.get("review"), dict)
            else None
        )
        return {
            "schema_version": PUBLIC_CANDIDATE_SCHEMA,
            "candidate_id": record["candidate_id"],
            "extension_id": record["extension_id"],
            "extension_version": record["extension_version"],
            "candidate_revision": record["revision"],
            "stage": stage,
            "spec_digest": record["spec_digest"],
            "spec_integrity_status": "validated",
            "artifact_status": "not_generated",
            "artifact_integrity_status": "unverified",
            "behavior_verification_status": "not_started",
            "execution_status": "not_started",
            "signature_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "policy_effect": "none",
            "review": (
                {
                    "decision": review.get("decision"),
                    "review_source": review.get("review_source"),
                    "execution_authorized": False,
                    "promotion_authorized": False,
                    "recorded_at": review.get("recorded_at"),
                }
                if review
                else None
            ),
            "history_count": len(record["history"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "expires_at": record["expires_at"],
            "operation_replayed": operation_replayed,
            "replayed_operation_result": (
                {
                    "candidate_revision": replayed_operation.get(
                        "result_revision"
                    ),
                    "stored_stage": replayed_operation.get(
                        "result_stage"
                    ),
                }
                if operation_replayed
                and isinstance(replayed_operation, dict)
                else None
            ),
            "candidate_existing": candidate_existing,
            "authority": self._authority(),
        }

    @staticmethod
    def _authority() -> dict[str, Any]:
        return {
            "code_generation": False,
            "artifact_write": False,
            "workspace_access": False,
            "state_access": False,
            "environment_access": False,
            "network_access": False,
            "secret_access": False,
            "tool_access": False,
            "agent_dispatch": False,
            "signing": False,
            "installation": False,
            "activation": False,
            "capability_registration": False,
            "execution": False,
            "behavior_verification": False,
            "promotion": False,
            "provider_switch": False,
        }

    def _effective_stage(self, record: dict[str, Any]) -> str:
        stored = str(record.get("stage") or "")
        if stored in {"REVOKED", "REJECTED"}:
            return stored
        if self._is_expired(record):
            return "EXPIRED"
        return stored

    def _is_expired(self, record: dict[str, Any]) -> bool:
        expires = self._parse_time(str(record.get("expires_at") or ""))
        return self._now_utc() >= expires

    def _validate_new_expiry(self, value: str) -> None:
        expires = self._parse_time(value)
        now = self._now_utc()
        if expires <= now:
            raise ValueError("ExtensionSpec expires_at must be in the future")
        if expires > now + timedelta(days=MAX_EXPIRY_DAYS):
            raise ValueError(
                "ExtensionSpec expiry exceeds the bounded lifetime"
            )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "ExtensionSpec expires_at must be ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError(
                "ExtensionSpec expires_at must be timezone-aware"
            )
        return parsed.astimezone(timezone.utc)

    def _now_utc(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("runtime clock must be timezone-aware")
        return current.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._now_utc().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _identity_key(
        user_id: str,
        workspace_id: str,
        extension_id: str,
        extension_version: int,
    ) -> str:
        return ExtensionSpecQuarantine._digest(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "extension_id": extension_id,
                "extension_version": extension_version,
            }
        )

    @staticmethod
    def _candidate_id(
        identity_key: str,
        spec_digest: str,
    ) -> str:
        digest = ExtensionSpecQuarantine._digest(
            {
                "identity_key": identity_key,
                "spec_digest": spec_digest,
            }
        )
        return f"extspec_{digest[:24]}"

    @staticmethod
    def _operation_key(
        user_id: str,
        workspace_id: str,
        operation_id: str,
    ) -> str:
        return ExtensionSpecQuarantine._digest(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "operation_id": operation_id,
            }
        )

    @staticmethod
    def _owner_scope_digest(
        user_id: str,
        workspace_id: str,
    ) -> str:
        return ExtensionSpecQuarantine._digest(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
            }
        )

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    @staticmethod
    def _required_id(value: Any, field: str) -> str:
        selected = str(value or "")
        if not _OPAQUE_ID.fullmatch(selected):
            raise ValueError(
                f"{field} must be a bounded opaque identifier"
            )
        return selected

    @staticmethod
    def _required_text(
        value: Any,
        field: str,
        limit: int,
    ) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        selected = value.strip()
        if (
            not selected
            or len(selected.encode("utf-8")) > limit
            or "\x00" in selected
        ):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _required_revision(value: Any) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(
                "expected_revision must be a positive integer"
            )
        return value

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionSpecStorageError):
            return "extension_spec_state_invalid"
        return f"extension_spec_unavailable:{type(exc).__name__}"

    def _audit(
        self,
        *,
        route: str,
        status: str,
        record: dict[str, Any],
    ) -> None:
        try:
            candidate_scope_digest = self._digest(
                {
                    "candidate_id": record.get("candidate_id"),
                    "user_id": record.get("user_id"),
                    "workspace_id": record.get("workspace_id"),
                }
            )
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "route": route,
                    "status": status,
                    "artifacts": {
                        "private_candidate_scope_digest": (
                            candidate_scope_digest
                        ),
                        "candidate_revision": record.get("revision"),
                        "execution_authorized": False,
                        "promotion_authorized": False,
                    },
                },
            )
        except Exception:
            # The private candidate is already durable.  An auxiliary compact
            # audit failure must not cause the caller to retry the state write.
            return


__all__ = [
    "MAX_CANDIDATES",
    "MAX_EXPIRY_DAYS",
    "MAX_HISTORY",
    "MAX_NON_TERMINAL_OPERATIONS",
    "MAX_OPERATIONS",
    "PUBLIC_CANDIDATE_SCHEMA",
    "PUBLIC_INTEGRITY_SCHEMA",
    "PUBLIC_LIST_SCHEMA",
    "PUBLIC_STATUS_SCHEMA",
    "ReviewDecision",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
    "ExtensionSpecConflictError",
    "ExtensionSpecNotFoundError",
    "ExtensionSpecQuarantine",
    "ExtensionSpecQuarantineError",
    "ExtensionSpecStorageError",
]
