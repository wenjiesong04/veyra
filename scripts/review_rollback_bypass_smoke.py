#!/usr/bin/env python3
"""Prove reviewed execution and rollback have no string-authority bypass."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from execution.action_executor import ActionExecutor  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from rollback_audit.replay_runtime import ReplayRuntime  # noqa: E402
from rollback_audit.rollback_manager import RollbackManager  # noqa: E402
from routers.debug_audit import (  # noqa: E402
    ReplayRuntimeRequest,
    build_debug_audit_router,
)
from tool_proxy.safe_file import SafeFile  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class CountingSafeFile:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def write_text(
        self,
        path: str,
        content: str,
        reason: str = "",
        approved_by: str | None = None,
        *,
        require_snapshot: bool = False,
        snapshotter: Any = None,
    ) -> dict[str, Any]:
        result = {
            "status": "ok",
            "path": path,
            "content": content,
            "reason": reason,
            "approved_by": approved_by,
            "require_snapshot": require_snapshot,
            "snapshotter_configured": callable(snapshotter),
        }
        self.writes.append(result)
        return result

    def read_text(self, path: str) -> dict[str, Any]:
        return {"status": "ok", "path": path, "content": ""}


class NoopShell:
    def run(
        self,
        command: list[str],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "command": command,
            "approved_by": approved_by,
        }


class NoopBrowser:
    def open(
        self,
        url: str,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "url": url,
            "approved_by": approved_by,
        }


class NoopAPI:
    def request(
        self,
        payload: dict[str, Any],
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "payload": payload,
            "approved_by": approved_by,
        }


class DummyReplay:
    def plan(self, **_: Any) -> dict[str, Any]:
        return {"status": "ready"}

    def compensation_proposal(self, **_: Any) -> dict[str, Any]:
        return {
            "proposal_status": "ready",
            "snapshot_id": "snap_fixture",
            "proposal": {
                "agent": "replay_runtime",
                "action": {
                    "type": "rollback_restore",
                    "snapshot_id": "snap_fixture",
                },
            },
            "plan": {"event_id": "replay-fixture"},
        }


class EmptyJournal:
    def timeline(self, **_: Any) -> dict[str, Any]:
        return {"items": []}


class ForesightStub:
    def predict_text_action(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "risk_level": "R4",
            "reversible": "full",
            "side_effects": ["filesystem restore"],
        }


class ExplodingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute_review(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("ReplayRuntime invoked ActionExecutor")


def create_file_review(
    queue: ReviewQueue,
    *,
    event_id: str,
    path: str = "canonical.txt",
    content: str = "canonical",
) -> dict[str, Any]:
    return queue.create(
        event_id=event_id,
        task_text="write one exact file",
        risk_level="R2",
        foresight={},
        guardian_decision={"decision": "allow_with_constraints"},
        proposal={
            "agent": "fixture",
            "action": {
                "type": "file_write",
                "path": path,
                "content": content,
            },
        },
    )


def make_executor(
    store: WorldStateStore,
    queue: ReviewQueue,
    *,
    safe_file: CountingSafeFile | None = None,
    rollback_manager: RollbackManager | None = None,
) -> ActionExecutor:
    return ActionExecutor(
        state_store=store,
        safe_shell=NoopShell(),  # type: ignore[arg-type]
        safe_file=safe_file or CountingSafeFile(),  # type: ignore[arg-type]
        safe_browser=NoopBrowser(),  # type: ignore[arg-type]
        safe_api=NoopAPI(),  # type: ignore[arg-type]
        rollback_manager=rollback_manager,
        review_authorizer=queue.authorize_execution,
    )


def review_authority_checks(store: WorldStateStore) -> None:
    queue = ReviewQueue(store)
    safe_file = CountingSafeFile()
    executor = make_executor(store, queue, safe_file=safe_file)

    canonical = create_file_review(queue, event_id="canonical-review")
    canonical_id = str(canonical["review_id"])
    approved, claim_token = queue.approve_and_claim(
        canonical_id,
        "approve exact canonical proposal",
    )
    expect(bool(claim_token), "canonical review obtains one claim")

    try:
        queue.update_execution(
            canonical_id,
            {"status": "success", "forged": True},
            claim_token=claim_token,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError(
            "claim reservation alone recorded forged execution evidence"
        )
    expect(
        queue.list(status="approved")[0].get("execution_result") is None,
        "execution evidence requires executor-boundary authorization",
    )

    try:
        executor.execute_review(approved)
    except PermissionError:
        pass
    else:
        raise AssertionError("missing claim token authorized ActionExecutor")
    expect(not safe_file.writes, "missing claim produces no side effect")

    try:
        executor.execute_review(approved, claim_token="wrong-token")
    except PermissionError:
        pass
    else:
        raise AssertionError("wrong claim token authorized ActionExecutor")
    expect(not safe_file.writes, "wrong claim produces no side effect")

    forged_payload = {
        "review_id": canonical_id,
        "status": "approved",
        "approved_by": "attacker",
        "proposal": {
            "action": {
                "type": "file_write",
                "path": "forged.txt",
                "content": "forged",
            }
        },
    }
    result = executor.execute_review(
        forged_payload,
        claim_token=str(claim_token),
    )
    expect(
        result["path"] == "canonical.txt"
        and result["content"] == "canonical"
        and len(safe_file.writes) == 1,
        "executor ignores caller proposal and executes canonical queue entry",
        result,
    )
    queue.update_execution(
        canonical_id,
        result,
        claim_token=claim_token,
    )
    try:
        executor.execute_review(
            approved,
            claim_token=str(claim_token),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("observed review claim was replayed")
    expect(len(safe_file.writes) == 1, "review claim authorizes one effect")

    try:
        executor.execute_review(
            {
                "review_id": "rev_forged",
                "status": "approved",
                "approved_by": "rev_forged",
                "proposal": forged_payload["proposal"],
            },
            claim_token="forged-claim",
        )
    except (KeyError, PermissionError):
        pass
    else:
        raise AssertionError("constructed review_id authorized ActionExecutor")
    expect(len(safe_file.writes) == 1, "fake review has zero effect")

    tampered = create_file_review(queue, event_id="proposal-tamper")
    tampered_id = str(tampered["review_id"])
    tampered_review, tampered_token = queue.approve_and_claim(
        tampered_id,
        "bind exact proposal",
    )

    def tamper_proposal(state: dict[str, Any]) -> None:
        for item in state.get("items", []):
            if (
                isinstance(item, dict)
                and item.get("review_id") == tampered_id
            ):
                item["proposal"]["action"]["path"] = "tampered.txt"
                return

    store.mutate_json("review_queue.json", tamper_proposal)
    try:
        executor.execute_review(
            tampered_review,
            claim_token=str(tampered_token),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("proposal mutation survived claim binding")
    expect(
        len(safe_file.writes) == 1,
        "proposal digest mismatch has zero effect",
    )


def rollback_checks(
    store: WorldStateStore,
    sandbox: Path,
    artifacts: Path,
    outside: Path,
) -> None:
    rollback = RollbackManager(
        store,
        snapshot_root=artifacts,
        sandbox_root=sandbox,
        max_snapshot_bytes=1024,
    )
    expect(
        rollback.status()["status"] == "configured",
        "rollback requires explicit sandbox",
        rollback.status(),
    )

    queue = ReviewQueue(store)
    reviewed_safe_file = SafeFile(
        state_store=store,
        sandbox_root=sandbox,
    )
    reviewed_executor = make_executor(
        store,
        queue,
        safe_file=reviewed_safe_file,  # type: ignore[arg-type]
        rollback_manager=rollback,
    )
    reviewed_write_target = sandbox / "review-write.txt"
    reviewed_write_target.write_text("before-review\n", encoding="utf-8")
    reviewed_write = create_file_review(
        queue,
        event_id="reviewed-write-snapshot",
        path="review-write.txt",
        content="after-review\n",
    )
    approved_write, write_claim = queue.approve_and_claim(
        str(reviewed_write["review_id"]),
        "approve exact rollbackable file write",
    )
    write_result = reviewed_executor.execute_review(
        approved_write,
        claim_token=str(write_claim),
    )
    expect(
        write_result.get("status") == "ok"
        and write_result.get("rollback_status") == "available"
        and isinstance(write_result.get("snapshot"), dict)
        and write_result["snapshot"].get("status") == "created"
        and reviewed_write_target.read_text(encoding="utf-8")
        == "after-review\n",
        "reviewed file replacement creates an exact rollback snapshot first",
        write_result,
    )
    queue.update_execution(
        str(reviewed_write["review_id"]),
        write_result,
        claim_token=str(write_claim),
    )

    restore_review = queue.create(
        event_id="reviewed-write-restore",
        task_text="restore reviewed file write",
        risk_level="R4",
        foresight={},
        guardian_decision={"decision": "ask_user"},
        proposal={
            "agent": "fixture",
            "action": {
                "type": "rollback_restore",
                "snapshot_id": write_result["snapshot"]["snapshot_id"],
            },
        },
    )
    approved_restore, restore_claim = queue.approve_and_claim(
        str(restore_review["review_id"]),
        "restore exact reviewed write snapshot",
    )
    restore_result = reviewed_executor.execute_review(
        approved_restore,
        claim_token=str(restore_claim),
    )
    expect(
        restore_result.get("status") == "restored"
        and reviewed_write_target.read_text(encoding="utf-8")
        == "before-review\n",
        "canonical rollback review restores the reviewed file replacement",
        restore_result,
    )
    queue.update_execution(
        str(restore_review["review_id"]),
        restore_result,
        claim_token=str(restore_claim),
    )

    no_snapshot_target = sandbox / "no-snapshot.txt"
    no_snapshot_target.write_text("unchanged\n", encoding="utf-8")
    no_snapshot_review = create_file_review(
        queue,
        event_id="reviewed-write-no-snapshot",
        path="no-snapshot.txt",
        content="must-not-write\n",
    )
    approved_no_snapshot, no_snapshot_claim = queue.approve_and_claim(
        str(no_snapshot_review["review_id"]),
        "snapshot boundary must fail closed",
    )
    no_snapshot_executor = make_executor(
        store,
        queue,
        safe_file=SafeFile(
            state_store=store,
            sandbox_root=sandbox,
        ),  # type: ignore[arg-type]
        rollback_manager=RollbackManager(store),
    )
    no_snapshot_result = no_snapshot_executor.execute_review(
        approved_no_snapshot,
        claim_token=str(no_snapshot_claim),
    )
    expect(
        no_snapshot_result.get("status") == "blocked"
        and no_snapshot_target.read_text(encoding="utf-8") == "unchanged\n",
        "reviewed file write has zero effect when snapshot authority is absent",
        no_snapshot_result,
    )

    target = sandbox / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    snapshot = rollback.snapshot_file("value.txt", "bounded edit")
    expect(snapshot["status"] == "created", "existing file snapshot created")
    target.write_text("after\n", encoding="utf-8")

    direct = rollback.restore(str(snapshot["snapshot_id"]))
    expect(
        direct["status"] == "blocked"
        and target.read_text(encoding="utf-8") == "after\n",
        "direct rollback without verified review is blocked",
        direct,
    )
    restored = rollback.restore(
        str(snapshot["snapshot_id"]),
        authorized=True,
    )
    expect(
        restored["status"] == "restored"
        and target.read_text(encoding="utf-8") == "before\n",
        "authorized exact rollback restores artifact",
        restored,
    )

    outside_file = outside / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    outside_snapshot = rollback.snapshot_file(outside_file)
    expect(
        outside_snapshot["status"] == "blocked",
        "rollback snapshot cannot escape sandbox",
        outside_snapshot,
    )
    link = sandbox / "outside-link.txt"
    link.symlink_to(outside_file)
    link_snapshot = rollback.snapshot_file("outside-link.txt")
    expect(
        link_snapshot["status"] == "blocked"
        and outside_file.read_text(encoding="utf-8") == "outside\n",
        "rollback snapshot rejects symlink targets",
        link_snapshot,
    )

    ledger_target = sandbox / "ledger.txt"
    ledger_target.write_text("ledger-before\n", encoding="utf-8")
    ledger_snapshot = rollback.snapshot_file("ledger.txt", "tamper ledger")
    ledger_target.write_text("ledger-after\n", encoding="utf-8")
    ledger_id = str(ledger_snapshot["snapshot_id"])

    def tamper_snapshot_path(state: dict[str, Any]) -> None:
        for item in state.get("snapshots", []):
            if (
                isinstance(item, dict)
                and item.get("snapshot_id") == ledger_id
            ):
                item["snapshot"] = str(outside_file)
                return

    store.mutate_json("rollback_state.json", tamper_snapshot_path)
    tampered_restore = rollback.restore(ledger_id, authorized=True)
    expect(
        tampered_restore["status"] == "blocked"
        and ledger_target.read_text(encoding="utf-8") == "ledger-after\n"
        and outside_file.read_text(encoding="utf-8") == "outside\n",
        "ledger path tamper cannot redirect restore",
        tampered_restore,
    )

    artifact_target = sandbox / "artifact.txt"
    artifact_target.write_text("artifact-before\n", encoding="utf-8")
    artifact_snapshot = rollback.snapshot_file(
        "artifact.txt",
        "tamper artifact",
    )
    artifact_target.write_text("artifact-after\n", encoding="utf-8")
    artifact_path = Path(str(artifact_snapshot["snapshot"]))
    artifact_path.unlink()
    artifact_path.symlink_to(outside_file)
    artifact_restore = rollback.restore(
        str(artifact_snapshot["snapshot_id"]),
        authorized=True,
    )
    expect(
        artifact_restore["status"] == "blocked"
        and artifact_target.read_text(encoding="utf-8")
        == "artifact-after\n",
        "snapshot artifact symlink substitution is blocked",
        artifact_restore,
    )

    checksum_target = sandbox / "checksum.txt"
    checksum_target.write_text("checksum-before\n", encoding="utf-8")
    checksum_snapshot = rollback.snapshot_file(
        "checksum.txt",
        "tamper checksum",
    )
    checksum_target.write_text("checksum-after\n", encoding="utf-8")
    checksum_artifact = Path(str(checksum_snapshot["snapshot"]))
    expected_identity = checksum_snapshot["artifact_identity"]
    checksum_artifact.write_bytes(b"checksum-forged\n")
    expect(
        checksum_artifact.stat().st_size
        == int(expected_identity["size"]),
        "checksum fixture preserves artifact size",
    )
    os.utime(
        checksum_artifact,
        ns=(
            int(expected_identity["mtime_ns"]),
            int(expected_identity["mtime_ns"]),
        ),
    )
    checksum_restore = rollback.restore(
        str(checksum_snapshot["snapshot_id"]),
        authorized=True,
    )
    expect(
        checksum_restore["status"] == "blocked"
        and checksum_target.read_text(encoding="utf-8")
        == "checksum-after\n",
        "snapshot checksum substitution is blocked",
        checksum_restore,
    )

    tombstone = rollback.snapshot_file("created-later.txt", "new file")
    expect(
        tombstone["status"] == "tombstone"
        and tombstone["rollback_mode"] == "delete_created_file",
        "missing target creates rollback tombstone",
        tombstone,
    )
    created_later = sandbox / "created-later.txt"
    created_later.write_text("new\n", encoding="utf-8")
    tombstone_restore = rollback.restore(
        str(tombstone["snapshot_id"]),
        authorized=True,
    )
    expect(
        tombstone_restore["status"] == "restored"
        and tombstone_restore["deleted_created_file"] is True
        and not created_later.exists(),
        "tombstone deletes file created by reviewed effect",
        tombstone_restore,
    )

    queue = ReviewQueue(store)
    reviewed_target = sandbox / "reviewed.txt"
    reviewed_target.write_text("reviewed-before\n", encoding="utf-8")
    reviewed_snapshot = rollback.snapshot_file("reviewed.txt", "reviewed")
    reviewed_target.write_text("reviewed-after\n", encoding="utf-8")
    review = queue.create(
        event_id="reviewed-rollback",
        task_text="restore exact snapshot",
        risk_level="R4",
        foresight={},
        guardian_decision={"decision": "ask_user"},
        proposal={
            "agent": "fixture",
            "action": {
                "type": "rollback_restore",
                "snapshot_id": reviewed_snapshot["snapshot_id"],
            },
        },
    )
    approved, claim_token = queue.approve_and_claim(
        str(review["review_id"]),
        "approve exact restore",
    )
    executor = make_executor(
        store,
        queue,
        rollback_manager=rollback,
    )
    execution = executor.execute_review(
        approved,
        claim_token=str(claim_token),
    )
    expect(
        execution["status"] == "restored"
        and reviewed_target.read_text(encoding="utf-8")
        == "reviewed-before\n",
        "ActionExecutor passes restore authority only after review claim",
        execution,
    )


def replay_checks(store: WorldStateStore) -> None:
    queue = ReviewQueue(store)
    exploding = ExplodingExecutor()
    runtime = ReplayRuntime(
        state_store=store,
        replay=DummyReplay(),  # type: ignore[arg-type]
        journal=EmptyJournal(),  # type: ignore[arg-type]
        review_queue=queue,
        foresight_engine=ForesightStub(),  # type: ignore[arg-type]
        action_executor=exploding,
    )
    configured = runtime.configure(
        auto_execute_enabled=True,
        allow_r4_restore=True,
    )
    expect(
        configured["status"] == "blocked"
        and configured["config"]["auto_execute_enabled"] is False
        and configured["config"]["allow_r4_restore"] is False,
        "Replay configuration cannot mint R4 execution authority",
        configured,
    )

    def add_job(state: dict[str, Any]) -> None:
        state["jobs"] = [
            {
                "job_id": "replay_fixture",
                "status": "pending",
                "candidate_key": "replay_fixture",
                "event_id": "replay-fixture",
                "created_at": "2026-07-27T00:00:00+00:00",
                "updated_at": "2026-07-27T00:00:00+00:00",
            }
        ]

    store.mutate_json("replay_runtime_state.json", add_job)
    denied = runtime.run_pending(
        auto_create_reviews=True,
        auto_execute=True,
        allow_r4_restore=True,
    )
    expect(
        denied["status"] == "needs_review"
        and denied["execution_authority_enabled"] is False
        and exploding.calls == 0,
        "Replay auto-execute request creates review with zero effects",
        denied,
    )
    replay_reviews = [
        item
        for item in queue.list()
        if item.get("event_id") == "replay-fixture"
    ]
    expect(
        len(replay_reviews) == 1
        and replay_reviews[0].get("status") == "pending"
        and not isinstance(
            replay_reviews[0].get("execution_claim"),
            dict,
        ),
        "Replay leaves canonical R4 review pending and unclaimed",
        replay_reviews,
    )

    router = build_debug_audit_router({"replay_runtime": runtime})
    execute_endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "")
        == "/audit/replay/runtime/execute"
    )
    route_result = asyncio.run(
        execute_endpoint(
            ReplayRuntimeRequest(
                auto_execute=True,
                allow_r4_restore=True,
            )
        )
    )
    expect(
        route_result["status"] == "needs_review"
        and route_result["execution_authority_enabled"] is False
        and exploding.calls == 0,
        "Replay HTTP execute endpoint remains deny-only",
        route_result,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-review-rollback-bypass-") as temp:
        base = Path(temp)
        sandbox = base / "sandbox"
        artifacts = base / "artifacts"
        outside = base / "outside"
        sandbox.mkdir()
        artifacts.mkdir()
        outside.mkdir()
        store = WorldStateStore(base / "state")

        review_authority_checks(store)
        rollback_checks(store, sandbox, artifacts, outside)
        replay_checks(store)

    print("review_rollback_bypass_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
