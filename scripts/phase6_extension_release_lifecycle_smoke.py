from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.extension_release_registry import (  # noqa: E402
    ExtensionReleaseConflictError,
    ExtensionReleaseNotFoundError,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    SOURCE,
    USER,
    WORKSPACE,
)
from scripts.phase6_extension_release_contract_smoke import (  # noqa: E402
    BASE_TIME,
    CONTROL_TOKEN,
    SESSION,
    Clock,
    build_registry,
    create_kwargs,
    expect,
    expect_raises,
    snapshot_bytes,
)


def run() -> None:
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        registry, _, _ = build_registry(Path(state_dir), Path(key_dir))
        kwargs = create_kwargs(registry)
        created = registry.create(**kwargs)
        before_gets = snapshot_bytes(registry.state_store)
        status = registry.status(control_token=CONTROL_TOKEN)
        listed = registry.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        fetched = registry.get(
            release_id=created["release_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        integrity = registry.integrity(
            release_id=created["release_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        after_gets = snapshot_bytes(registry.state_store)
        expect(
            before_gets == after_gets
            and status["read_side_effects"] == "none"
            and listed["state_mutated"] is False
            and integrity["state_mutated"] is False,
            "status/list/get/integrity are byte-invariant pure reads",
        )
        expect(
            listed["count"] == 1
            and fetched["release_id"] == created["release_id"]
            and integrity["signature_verification_status"] == "verified",
            "pure GET projections retain complete signed status and risk boundary",
        )
        replay_before = snapshot_bytes(registry.state_store)
        replay = registry.create(**kwargs)
        expect(
            replay["operation_replayed"] is True
            and replay_before == snapshot_bytes(registry.state_store),
            "create operation replay returns the durable release without resigning",
        )
        rebound = dict(kwargs)
        rebound["expires_at"] = "2026-08-04T08:01:00Z"
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.create(**rebound),
            "one operation id cannot be rebound to a new release request",
        )
        stale = dict(kwargs)
        stale["operation_id"] = "release-create-stale-cas"
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.create(**stale),
            "stale registry CAS rejects another release registration",
        )
        expect_raises(
            (ExtensionReleaseNotFoundError,),
            lambda: registry.get(
                release_id=created["release_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id="other-session",
                control_token=CONTROL_TOKEN,
            ),
            "release records remain isolated by authenticated principal and session",
        )
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.revoke(
                release_id=created["release_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                operation_id="release-revoke-stale",
                expected_release_revision=2,
                expected_registry_revision=1,
                reason_digest="a" * 64,
                control_token=CONTROL_TOKEN,
            ),
            "revocation requires exact release CAS",
        )
        revoke_kwargs = {
            "release_id": created["release_id"],
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
            "operation_id": "release-revoke-1",
            "expected_release_revision": 1,
            "expected_registry_revision": 1,
            "reason_digest": "b" * 64,
            "control_token": CONTROL_TOKEN,
        }
        revoked = registry.revoke(**revoke_kwargs)
        state = registry.state_store.read_json(
            "phase6_extension_release_state.json"
        )
        expect(
            revoked["stored_stage"] == "RELEASE_REVOKED"
            and revoked["release_revision"] == 2
            and revoked["registry_revision"] == 2
            and len(state["lifecycle_events"]) == 2
            and [event["transition"] for event in state["lifecycle_events"]]
            == ["signed", "revoked"],
            "revocation is CAS-bound and lifecycle history is append-only",
            state["lifecycle_events"],
        )
        expect(
            revoked["attestation_digest"] == created["attestation_digest"]
            and revoked["manifest_digest"] == created["manifest_digest"],
            "revocation does not rewrite immutable attestation content",
        )
        revoke_replay_before = snapshot_bytes(registry.state_store)
        revoke_replay = registry.revoke(**revoke_kwargs)
        expect(
            revoke_replay["operation_replayed"] is True
            and revoke_replay_before == snapshot_bytes(registry.state_store),
            "revocation replay is idempotent and byte invariant",
        )
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.deployment_subject(
                release_id=created["release_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                expected_release_revision=2,
                expected_attestation_digest=created["attestation_digest"],
                control_token=CONTROL_TOKEN,
            ),
            "revoked release cannot become a deployment subject",
        )
        serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
        expect(
            SOURCE.decode("utf-8") not in serialized
            and "PRIVATE KEY" not in serialized
            and "BEGIN PRIVATE" not in serialized,
            "private registry persists no source or private-key material",
        )

    clock = Clock()
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        registry, _, _ = build_registry(
            Path(state_dir), Path(key_dir), clock=clock
        )
        created = registry.create(**create_kwargs(registry))
        clock.current = BASE_TIME + timedelta(days=2)
        before_expiry_read = snapshot_bytes(registry.state_store)
        expired = registry.get(
            release_id=created["release_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            expired["effective_status"] == "RELEASE_EXPIRED"
            and expired["fresh"] is False
            and before_expiry_read == snapshot_bytes(registry.state_store),
            "expiry is a pure fail-closed projection without lifecycle mutation",
        )
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.deployment_subject(
                release_id=created["release_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                expected_release_revision=1,
                expected_attestation_digest=created["attestation_digest"],
                control_token=CONTROL_TOKEN,
            ),
            "expired release cannot become a deployment subject",
        )

    print("Phase 6 extension release lifecycle smoke passed")


if __name__ == "__main__":
    run()
