#!/usr/bin/env python3
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.extension_artifact import (  # noqa: E402
    artifact_owner_scope_digest,
    parse_extension_artifact,
)
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime.extension_artifact_quarantine import (  # noqa: E402
    STATE_FILE as ARTIFACT_STATE_FILE,
    ExtensionArtifactConflictError,
    ExtensionArtifactNotFoundError,
    ExtensionArtifactQuarantine,
    ExtensionArtifactStorageError,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_artifact_contract_smoke import (  # noqa: E402
    DEFAULT_SOURCE,
    valid_artifact_envelope,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


WORKSPACE = "/private/veyra/phase6-extension-artifact-workspace"
USER = "phase6-extension-artifact-user"
BASE_TIME = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@dataclass(frozen=True)
class GatedContext:
    store: WorldStateStore
    clock: Clock
    spec_runtime: ExtensionSpecQuarantine
    artifact_runtime: ExtensionArtifactQuarantine
    candidate: dict[str, Any]
    subject: dict[str, Any]


@dataclass(frozen=True)
class AdmittedContext:
    gated: GatedContext
    envelope: dict[str, Any]
    artifact: dict[str, Any]
    content: bytes
    blob_root: Path
    blob_path: Path


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> None:
    try:
        call()
    except errors:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: call did not fail closed")


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


def prepare_store(root: Path) -> WorldStateStore:
    store = WorldStateStore(root)
    store.mutate_json(
        "local_world.json",
        lambda state: {
            **state,
            "current_project": WORKSPACE,
        },
    )
    return store


def core_invariants(store: WorldStateStore) -> dict[str, Any]:
    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    snapshot = CapabilityRegistry(store, now=fixed).snapshot()
    capabilities = snapshot.get("capabilities", {})
    return {
        "capability": {
            "routes": snapshot.get("routes"),
            "items": {
                capability_id: {
                    key: value.get(key)
                    for key in (
                        "available",
                        "kind",
                        "route",
                        "executor",
                        "status",
                        "reason",
                        "namespace",
                    )
                }
                for capability_id, value in capabilities.items()
                if isinstance(value, dict)
            },
        },
        "memory": store.read_json("agent_memory.json"),
        "agent_config": store.read_json("agent_config.json"),
        "task": store.read_json("task_state.json"),
        "durable_case": store.read_json("durable_case_state.json"),
        "phase6_collaboration": store.read_json(
            "phase6_collaboration_state.json"
        ),
        "self_improvement": store.read_json(
            "self_improvement_proposals.json"
        ),
        "state_change_proposals": store.read_json(
            "state_change_proposals.json"
        ),
        "review": store.read_json("review_queue.json"),
        "tool_governance": store.read_json(
            "tool_governance_state.json"
        ),
        "openclaw_broker": store.read_json(
            "openclaw_tool_hook_state.json"
        ),
    }


def gate_candidate(
    store: WorldStateStore,
    clock: Clock,
    extension_id: str,
    operation_prefix: str,
) -> GatedContext:
    spec_runtime = ExtensionSpecQuarantine(
        state_store=store,
        now=clock,
    )
    artifact_runtime = ExtensionArtifactQuarantine(
        state_store=store,
        spec_quarantine=spec_runtime,
        now=clock,
    )
    payload = valid_spec(now=clock.current)
    payload["extension_id"] = extension_id
    payload["purpose"] = (
        "Bind one inert source artifact to a reviewed specification."
    )
    spec = parse_extension_spec(payload)
    quarantined = spec_runtime.quarantine(
        spec=spec,
        expected_spec_digest=spec.digest(),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=f"{operation_prefix}-spec-submit",
    )
    expect(
        quarantined["stage"] == "SPEC_QUARANTINED"
        and quarantined["candidate_revision"] == 1,
        f"{operation_prefix} spec enters quarantine",
        quarantined,
    )
    candidate = spec_runtime.review(
        candidate_id=quarantined["candidate_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        expected_revision=1,
        operation_id=f"{operation_prefix}-spec-gate",
        decision="accept_for_future_isolated_generation",
        reason="The manifest is eligible only for inert artifact admission.",
    )
    expect(
        candidate["stage"] == "SPEC_GATE_PASSED"
        and candidate["candidate_revision"] == 2
        and candidate["execution_status"] == "not_started"
        and not any(candidate["authority"].values()),
        f"{operation_prefix} spec gate grants no authority",
        candidate,
    )
    subject = spec_runtime.artifact_subject(
        candidate_id=candidate["candidate_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        require_gate_passed=True,
    )
    return GatedContext(
        store=store,
        clock=clock,
        spec_runtime=spec_runtime,
        artifact_runtime=artifact_runtime,
        candidate=candidate,
        subject=subject,
    )


def build_envelope(
    context: GatedContext,
    *,
    content: bytes = DEFAULT_SOURCE,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    payload = valid_artifact_envelope(
        content=content,
        now=context.clock.current,
    )
    payload.update(
        {
            "candidate_id": context.subject["candidate_id"],
            "candidate_revision": context.subject[
                "candidate_revision"
            ],
            "owner_scope_digest": artifact_owner_scope_digest(
                USER,
                WORKSPACE,
            ),
            "extension_id": context.subject["extension_id"],
            "extension_version": context.subject[
                "extension_version"
            ],
            "spec_digest": context.subject["spec_digest"],
            "extension_policy_revision": context.subject[
                "extension_policy_revision"
            ],
            "expires_at": canonical_utc(
                expires_at
                or context.clock.current + timedelta(days=5)
            ),
        }
    )
    return payload


def submit_artifact(
    context: GatedContext,
    envelope: dict[str, Any],
    operation_id: str,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    parsed = parse_extension_artifact(envelope)
    return context.artifact_runtime.submit(
        envelope=parsed,
        expected_artifact_sha256=(
            expected_digest or parsed.artifact_sha256
        ),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=operation_id,
    )


def admitted_context(
    root: Path,
    *,
    extension_id: str,
    operation_prefix: str,
    content: bytes = DEFAULT_SOURCE,
    expires_at: datetime | None = None,
) -> AdmittedContext:
    store = prepare_store(root)
    clock = Clock(BASE_TIME)
    gated = gate_candidate(
        store,
        clock,
        extension_id,
        operation_prefix,
    )
    envelope = build_envelope(
        gated,
        content=content,
        expires_at=expires_at,
    )
    artifact = submit_artifact(
        gated,
        envelope,
        f"{operation_prefix}-artifact-submit",
    )
    private_state = store.read_json(ARTIFACT_STATE_FILE)
    record = private_state["artifacts"][artifact["artifact_id"]]
    blob_root = (
        store.path_for(ARTIFACT_STATE_FILE).parent
        / "phase6_extension_artifacts"
    )
    blob_path = blob_root / record["blob"]["relative_filename"]
    return AdmittedContext(
        gated=gated,
        envelope=envelope,
        artifact=artifact,
        content=content,
        blob_root=blob_root,
        blob_path=blob_path,
    )


def action_bytes(store: WorldStateStore) -> bytes:
    path = store.path_for("action_record.jsonl")
    return path.read_bytes() if path.exists() else b""


def main_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-lifecycle-"
    ) as raw:
        root = Path(raw)
        store = prepare_store(root)
        before = core_invariants(store)
        clock = Clock(BASE_TIME)
        gated = gate_candidate(
            store,
            clock,
            "example.artifact_lifecycle",
            "artifact-lifecycle",
        )
        sentinel = root / "artifact-source-must-never-run"
        dangerous_source = (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('EXECUTED')\n"
            "raise RuntimeError('artifact source executed')\n"
        ).encode("utf-8")
        envelope = build_envelope(
            gated,
            content=dangerous_source,
        )
        artifact = submit_artifact(
            gated,
            envelope,
            "artifact-lifecycle-submit",
        )
        expect(
            artifact["stage"] == "ARTIFACT_QUARANTINED"
            and artifact["artifact_revision"] == 1
            and artifact["artifact_status"] == "quarantined"
            and artifact["artifact_integrity_status"] == "validated"
            and artifact["source_syntax_status"] == "not_checked"
            and artifact["static_checks_status"] == "not_started"
            and artifact["behavior_verification_status"] == "not_started"
            and artifact["execution_status"] == "not_started"
            and artifact["activation_status"] == "not_installed"
            and not any(artifact["authority"].values()),
            "gated spec admits one inert artifact with zero authority",
            artifact,
        )
        expect(
            not sentinel.exists(),
            "dangerous import-time artifact source is never executed",
        )

        private_state = store.read_json(ARTIFACT_STATE_FILE)
        record = private_state["artifacts"][artifact["artifact_id"]]
        blob_root = (
            store.path_for(ARTIFACT_STATE_FILE).parent
            / "phase6_extension_artifacts"
        )
        blob_path = blob_root / record["blob"]["relative_filename"]
        expect(
            stat.S_IMODE(blob_root.stat().st_mode) == 0o700
            and stat.S_IMODE(blob_path.stat().st_mode) == 0o600
            and blob_path.stat().st_nlink == 1
            and blob_path.read_bytes() == dangerous_source,
            "private blob uses mode-0700 directory and mode-0600 file",
        )

        public_payloads = {
            "status": gated.artifact_runtime.status(),
            "get": gated.artifact_runtime.get(
                artifact_id=artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "list": gated.artifact_runtime.list(
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "projection": (
                gated.artifact_runtime.projection_for_candidate(
                    candidate_id=gated.candidate["candidate_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                )
            ),
            "integrity": gated.artifact_runtime.integrity(
                artifact_id=artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
        }
        public_text = json.dumps(
            public_payloads,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            USER not in public_text
            and WORKSPACE not in public_text
            and str(blob_root) not in public_text
            and str(blob_path) not in public_text
            and record["blob"]["relative_filename"] not in public_text
            and envelope["content_b64url"] not in public_text
            and dangerous_source.decode("utf-8") not in public_text,
            "public artifact surfaces expose no owner, path, or source bytes",
            public_payloads,
        )
        world = store.read_all()
        world_text = json.dumps(
            world,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            "phase6_extension_artifact_state" not in world
            and "phase6_extension_spec_state" not in world
            and artifact["artifact_id"] not in world_text
            and envelope["content_b64url"] not in world_text
            and dangerous_source.decode("utf-8") not in world_text,
            "generic world-state projection excludes extension private state",
        )
        audit_text = action_bytes(store).decode("utf-8")
        expect(
            artifact["artifact_id"] not in audit_text
            and gated.candidate["candidate_id"] not in audit_text
            and gated.candidate["extension_id"] not in audit_text
            and gated.candidate["spec_digest"] not in audit_text
            and USER not in audit_text
            and WORKSPACE not in audit_text
            and envelope["content_b64url"] not in audit_text
            and str(blob_path) not in audit_text,
            "global audit contains only an opaque private scope digest",
        )

        state_before_integrity = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        integrity = gated.artifact_runtime.integrity(
            artifact_id=artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        state_after_integrity = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        expect(
            integrity["status"] == "artifact_integrity_passed"
            and integrity["state_mutated"] is False
            and integrity["execution_status"] == "not_started"
            and state_before_integrity == state_after_integrity
            and not sentinel.exists(),
            "integrity reopens exact bytes without mutation or execution",
            integrity,
        )

        restarted_store = WorldStateStore(root)
        restarted_spec = ExtensionSpecQuarantine(
            state_store=restarted_store,
            now=clock,
        )
        restarted_artifacts = ExtensionArtifactQuarantine(
            state_store=restarted_store,
            spec_quarantine=restarted_spec,
            now=clock,
        )
        restarted = restarted_artifacts.get(
            artifact_id=artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            restarted["stage"] == "ARTIFACT_QUARANTINED"
            and restarted["artifact_sha256"]
            == artifact["artifact_sha256"]
            and not sentinel.exists(),
            "runtime restart reopens the same inert private artifact",
            restarted,
        )

        conflicting_envelope = build_envelope(
            gated,
            content=b"def different(value: str) -> str:\n    return value\n",
        )
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: submit_artifact(
                gated,
                conflicting_envelope,
                "artifact-lifecycle-submit",
            ),
            "operation replay cannot change artifact semantics",
        )

        revoked_spec = gated.spec_runtime.revoke(
            candidate_id=gated.candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=2,
            operation_id="artifact-lifecycle-spec-revoke",
            reason="The specification is no longer eligible.",
        )
        expect(
            revoked_spec["stage"] == "REVOKED"
            and revoked_spec["candidate_revision"] == 3,
            "spec revocation blocks the artifact candidate",
            revoked_spec,
        )
        state_before_replay = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        audit_before_replay = action_bytes(store)
        replay = submit_artifact(
            gated,
            envelope,
            "artifact-lifecycle-submit",
        )
        state_after_replay = store.path_for(
            ARTIFACT_STATE_FILE
        ).read_bytes()
        audit_after_replay = action_bytes(store)
        expect(
            replay["operation_replayed"] is True
            and replay["stage"] == "ARTIFACT_QUARANTINED"
            and replay["effective_status"] == "BLOCKED_CANDIDATE"
            and replay["replayed_operation_result"]
            == {
                "artifact_revision": 1,
                "stored_stage": "ARTIFACT_QUARANTINED",
            }
            and state_before_replay == state_after_replay
            and audit_before_replay == audit_after_replay
            and not sentinel.exists(),
            "exact replay survives spec revoke with zero state or audit effect",
            replay,
        )
        expect_raises(
            (ExtensionSpecConflictError,),
            lambda: submit_artifact(
                gated,
                envelope,
                "artifact-lifecycle-new-after-spec-revoke",
            ),
            "new artifact admission is rejected after spec revoke",
        )
        revoked_artifact = gated.artifact_runtime.revoke(
            artifact_id=artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="artifact-lifecycle-artifact-revoke",
            reason="Terminal safety control remains available.",
        )
        expect(
            revoked_artifact["stage"] == "ARTIFACT_REVOKED"
            and revoked_artifact["artifact_revision"] == 2
            and revoked_artifact["artifact_status"] == "revoked"
            and not any(revoked_artifact["authority"].values()),
            "artifact revocation remains available as terminal safety control",
            revoked_artifact,
        )
        expect(
            before == core_invariants(store),
            "artifact lifecycle changes no capability, memory, Agent, review, "
            "or Tool Governance state",
        )


def binding_matrix() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-bindings-"
    ) as raw:
        root = Path(raw)
        store = prepare_store(root)
        clock = Clock(BASE_TIME)
        gated = gate_candidate(
            store,
            clock,
            "example.artifact_bindings",
            "artifact-bindings",
        )
        original = build_envelope(gated)

        owner_mismatch = copy.deepcopy(original)
        owner_mismatch["owner_scope_digest"] = (
            artifact_owner_scope_digest("another-user", WORKSPACE)
        )
        expect_raises(
            (ValueError,),
            lambda: submit_artifact(
                gated,
                owner_mismatch,
                "artifact-bind-owner",
            ),
            "artifact owner scope digest is bound to the caller",
        )
        expect_raises(
            (ValueError,),
            lambda: gated.artifact_runtime.submit(
                envelope=original,
                expected_artifact_sha256=original[
                    "artifact_sha256"
                ],
                user_id="another-user",
                workspace_id=WORKSPACE,
                operation_id="artifact-bind-owner-call",
            ),
            "artifact caller owner cannot differ from the envelope",
        )

        candidate_mismatch = copy.deepcopy(original)
        candidate_mismatch["candidate_id"] = (
            "extspec_ffffffffffffffffffffffff"
        )
        expect_raises(
            (ExtensionSpecNotFoundError,),
            lambda: submit_artifact(
                gated,
                candidate_mismatch,
                "artifact-bind-candidate",
            ),
            "artifact candidate id must resolve to the exact gated spec",
        )

        spec_digest_mismatch = copy.deepcopy(original)
        spec_digest_mismatch["spec_digest"] = "f" * 64
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: submit_artifact(
                gated,
                spec_digest_mismatch,
                "artifact-bind-spec-digest",
            ),
            "artifact spec digest is bound to the gated manifest",
        )
        expect_raises(
            (ValueError,),
            lambda: submit_artifact(
                gated,
                original,
                "artifact-bind-expected-digest",
                expected_digest="f" * 64,
            ),
            "caller expected digest must match exact artifact bytes",
        )

        revision_mismatch = copy.deepcopy(original)
        revision_mismatch["candidate_revision"] = 1
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: submit_artifact(
                gated,
                revision_mismatch,
                "artifact-bind-revision",
            ),
            "artifact candidate revision is bound to the gate decision",
        )

        extension_mismatch = copy.deepcopy(original)
        extension_mismatch["extension_id"] = "example.other_extension"
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: submit_artifact(
                gated,
                extension_mismatch,
                "artifact-bind-extension",
            ),
            "artifact extension identity is bound to the gated manifest",
        )

        after_candidate = copy.deepcopy(original)
        after_candidate["expires_at"] = canonical_utc(
            BASE_TIME + timedelta(days=8)
        )
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: submit_artifact(
                gated,
                after_candidate,
                "artifact-bind-candidate-expiry",
            ),
            "artifact expiry cannot outlive its candidate",
        )

        expired = copy.deepcopy(original)
        expired["expires_at"] = canonical_utc(
            BASE_TIME - timedelta(seconds=1)
        )
        expect_raises(
            (ValueError,),
            lambda: submit_artifact(
                gated,
                expired,
                "artifact-bind-expired",
            ),
            "already expired artifacts cannot enter quarantine",
        )

        unbounded_expiry = copy.deepcopy(original)
        unbounded_expiry["expires_at"] = canonical_utc(
            BASE_TIME + timedelta(days=31)
        )
        expect_raises(
            (ValueError,),
            lambda: submit_artifact(
                gated,
                unbounded_expiry,
                "artifact-bind-unbounded-expiry",
            ),
            "artifact lifetime is capped before storage",
        )
        state = store.read_json(ARTIFACT_STATE_FILE)
        blob_root = (
            store.path_for(ARTIFACT_STATE_FILE).parent
            / "phase6_extension_artifacts"
        )
        expect(
            state["artifact_count"] == 0
            and (
                not blob_root.exists()
                or not list(blob_root.iterdir())
            ),
            "failed binding attempts leave no state or blob",
        )


def expiry_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-expiry-"
    ) as raw:
        root = Path(raw)
        admitted = admitted_context(
            root,
            extension_id="example.artifact_expiry",
            operation_prefix="artifact-expiry",
            expires_at=BASE_TIME + timedelta(seconds=1),
        )
        admitted.gated.clock.current = BASE_TIME + timedelta(seconds=1)
        expired = admitted.gated.artifact_runtime.get(
            artifact_id=admitted.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        integrity = admitted.gated.artifact_runtime.integrity(
            artifact_id=admitted.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            expired["stage"] == "ARTIFACT_QUARANTINED"
            and expired["effective_status"] == "EXPIRED"
            and expired["artifact_status"] == "expired"
            and integrity["status"] == "blocked",
            "artifact expiry fails closed at now equals expires_at",
            integrity,
        )
        expect_raises(
            (ValueError,),
            lambda: submit_artifact(
                admitted.gated,
                admitted.envelope,
                "artifact-expiry-new-submit",
            ),
            "new artifact admission is rejected after expiry",
        )


def concurrency_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-concurrency-"
    ) as raw:
        root = Path(raw)
        store = prepare_store(root)
        clock = Clock(BASE_TIME)
        gated = gate_candidate(
            store,
            clock,
            "example.artifact_concurrency",
            "artifact-concurrency",
        )
        envelope_a = build_envelope(
            gated,
            content=b"def candidate_a(value: str) -> str:\n    return value\n",
        )
        envelope_b = build_envelope(
            gated,
            content=b"def candidate_b(value: str) -> str:\n    return value\n",
        )

        def concurrent_submit(
            envelope: dict[str, Any],
            operation_id: str,
        ) -> tuple[str, str | None]:
            try:
                result = submit_artifact(
                    gated,
                    envelope,
                    operation_id,
                )
                return "success", str(result["artifact_id"])
            except ExtensionArtifactConflictError:
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    concurrent_submit,
                    envelope_a,
                    "artifact-concurrent-a",
                ),
                pool.submit(
                    concurrent_submit,
                    envelope_b,
                    "artifact-concurrent-b",
                ),
            ]
            results = [future.result() for future in futures]
        state = store.read_json(ARTIFACT_STATE_FILE)
        blob_root = (
            store.path_for(ARTIFACT_STATE_FILE).parent
            / "phase6_extension_artifacts"
        )
        expect(
            [status for status, _ in results].count("success") == 1
            and [status for status, _ in results].count("conflict") == 1
            and state["artifact_count"] == 1
            and len(state["candidate_index"]) == 1
            and len(state["operation_index"]) == 1
            and len(list(blob_root.glob("blob_*.bin"))) == 1,
            "concurrent candidate admission stores exactly one artifact",
            results,
        )


def terminal_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-terminal-"
    ) as raw:
        root = Path(raw)
        store = prepare_store(root)
        clock = Clock(BASE_TIME)

        rejected_gate = gate_candidate(
            store,
            clock,
            "example.artifact_rejected",
            "artifact-rejected",
        )
        rejected_envelope = build_envelope(rejected_gate)
        rejected_artifact = submit_artifact(
            rejected_gate,
            rejected_envelope,
            "artifact-rejected-submit",
        )
        rejected = rejected_gate.artifact_runtime.reject(
            artifact_id=rejected_artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="artifact-rejected-terminal",
            reason="The artifact failed operator review.",
        )
        reject_replay = rejected_gate.artifact_runtime.reject(
            artifact_id=rejected_artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="artifact-rejected-terminal",
            reason="The artifact failed operator review.",
        )
        expect(
            rejected["stage"] == "ARTIFACT_REJECTED"
            and rejected["artifact_revision"] == 2
            and reject_replay["operation_replayed"] is True
            and reject_replay["stage"] == "ARTIFACT_REJECTED",
            "artifact rejection is terminal and exactly replayable",
            reject_replay,
        )
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: rejected_gate.artifact_runtime.revoke(
                artifact_id=rejected_artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=2,
                operation_id="artifact-rejected-new-revoke",
                reason="Rejected artifacts cannot transition again.",
            ),
            "rejected artifact cannot enter another terminal state",
        )

        revoked_gate = gate_candidate(
            store,
            clock,
            "example.artifact_revoked",
            "artifact-revoked",
        )
        revoked_envelope = build_envelope(revoked_gate)
        revoked_artifact = submit_artifact(
            revoked_gate,
            revoked_envelope,
            "artifact-revoked-submit",
        )
        revoked = revoked_gate.artifact_runtime.revoke(
            artifact_id=revoked_artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="artifact-revoked-terminal",
            reason="The operator revoked the artifact.",
        )
        revoke_replay = revoked_gate.artifact_runtime.revoke(
            artifact_id=revoked_artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="artifact-revoked-terminal",
            reason="The operator revoked the artifact.",
        )
        expect(
            revoked["stage"] == "ARTIFACT_REVOKED"
            and revoked["artifact_revision"] == 2
            and revoke_replay["operation_replayed"] is True
            and revoke_replay["stage"] == "ARTIFACT_REVOKED",
            "artifact revocation is terminal and exactly replayable",
            revoke_replay,
        )
        expect_raises(
            (ExtensionArtifactConflictError,),
            lambda: revoked_gate.artifact_runtime.reject(
                artifact_id=revoked_artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=2,
                operation_id="artifact-revoked-new-reject",
                reason="Revoked artifacts cannot transition again.",
            ),
            "revoked artifact cannot enter another terminal state",
        )

        restarted_spec = ExtensionSpecQuarantine(
            state_store=WorldStateStore(root),
            now=clock,
        )
        restarted_artifacts = ExtensionArtifactQuarantine(
            state_store=store,
            spec_quarantine=restarted_spec,
            now=clock,
        )
        expect(
            restarted_artifacts.get(
                artifact_id=rejected_artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            )["stage"]
            == "ARTIFACT_REJECTED"
            and restarted_artifacts.get(
                artifact_id=revoked_artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            )["stage"]
            == "ARTIFACT_REVOKED",
            "restart preserves both artifact terminal states",
        )


def _tamper_blob_content(context: AdmittedContext) -> None:
    context.blob_path.write_bytes(b"tampered artifact bytes\n")


def _tamper_state(context: AdmittedContext) -> None:
    context.gated.store.mutate_json(
        ARTIFACT_STATE_FILE,
        lambda state: state["artifacts"][
            context.artifact["artifact_id"]
        ].__setitem__("spec_digest", "f" * 64),
    )


def _tamper_index(context: AdmittedContext) -> None:
    context.gated.store.mutate_json(
        ARTIFACT_STATE_FILE,
        lambda state: state.__setitem__(
            "candidate_index",
            {"0" * 64: context.artifact["artifact_id"]},
        ),
    )


def _tamper_operation(context: AdmittedContext) -> None:
    def mutate(state: dict[str, Any]) -> None:
        operation = next(iter(state["operation_index"].values()))
        operation["result_revision"] = 99

    context.gated.store.mutate_json(ARTIFACT_STATE_FILE, mutate)


def _tamper_history(context: AdmittedContext) -> None:
    def mutate(state: dict[str, Any]) -> None:
        record = state["artifacts"][context.artifact["artifact_id"]]
        record["history"][0]["transition"] = "artifact_revoked"

    context.gated.store.mutate_json(ARTIFACT_STATE_FILE, mutate)


def _remove_blob(context: AdmittedContext) -> None:
    context.blob_path.unlink()


def _replace_blob_inode(context: AdmittedContext) -> None:
    replacement = context.blob_root / "replacement.bin"
    replacement.write_bytes(context.content)
    replacement.chmod(0o600)
    os.replace(replacement, context.blob_path)


def _replace_blob_with_symlink(context: AdmittedContext) -> None:
    outside = context.gated.store.root / "outside-artifact.bin"
    outside.write_bytes(context.content)
    outside.chmod(0o600)
    context.blob_path.unlink()
    context.blob_path.symlink_to(outside)


def _add_blob_hardlink(context: AdmittedContext) -> None:
    os.link(
        context.blob_path,
        context.blob_root / "unexpected-hardlink.bin",
    )


def _weaken_blob_mode(context: AdmittedContext) -> None:
    context.blob_path.chmod(0o644)


def tamper_matrix() -> None:
    attacks: list[
        tuple[str, Callable[[AdmittedContext], None]]
    ] = [
        ("blob content tamper", _tamper_blob_content),
        ("private state tamper", _tamper_state),
        ("candidate index tamper", _tamper_index),
        ("operation binding tamper", _tamper_operation),
        ("lifecycle history tamper", _tamper_history),
        ("missing blob", _remove_blob),
        ("replacement inode attack", _replace_blob_inode),
        ("blob symlink attack", _replace_blob_with_symlink),
        ("blob hardlink attack", _add_blob_hardlink),
        ("blob mode attack", _weaken_blob_mode),
    ]
    for index, (label, attack) in enumerate(attacks):
        with TemporaryDirectory(
            prefix=f"veyra-phase6-artifact-tamper-{index}-"
        ) as raw:
            context = admitted_context(
                Path(raw),
                extension_id=f"example.artifact_tamper_{index}",
                operation_prefix=f"artifact-tamper-{index}",
            )
            attack(context)
            status = context.gated.artifact_runtime.status()
            expect(
                status["status"] == "fail_closed"
                and status["operational_health"] == "degraded",
                f"{label} degrades artifact quarantine",
                status,
            )
            expect_raises(
                (ExtensionArtifactStorageError,),
                lambda context=context: (
                    context.gated.artifact_runtime.get(
                        artifact_id=context.artifact["artifact_id"],
                        user_id=USER,
                        workspace_id=WORKSPACE,
                    )
                ),
                f"{label} blocks artifact reads",
            )


def owner_read_isolation() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-artifact-owner-"
    ) as raw:
        context = admitted_context(
            Path(raw),
            extension_id="example.artifact_owner",
            operation_prefix="artifact-owner",
        )
        expect_raises(
            (ExtensionArtifactNotFoundError,),
            lambda: context.gated.artifact_runtime.get(
                artifact_id=context.artifact["artifact_id"],
                user_id="another-user",
                workspace_id=WORKSPACE,
            ),
            "cross-owner artifact read is indistinguishable from not found",
        )


def main() -> int:
    main_lifecycle()
    binding_matrix()
    expiry_lifecycle()
    concurrency_lifecycle()
    terminal_lifecycle()
    owner_read_isolation()
    tamper_matrix()
    print("phase6 extension artifact lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
