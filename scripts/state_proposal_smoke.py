#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state_proposal import (  # noqa: E402
    ProposalConflictError,
    StateChangeProposalStore,
    deterministic_idempotency_key,
)
from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


def test_repeat_and_concurrent_commit_once(root: Path) -> None:
    clock = MutableClock()
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world, clock=clock)
    memory_revision = int(world.read_json("agent_memory.json").get("_state_revision") or 0)
    key = deterministic_idempotency_key("event-concurrent", "act-memory-write")
    proposal = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "用户偏好简洁回答", "source": "direct_user"}},
        idempotency_key=key,
        expected_state_revisions={"agent_memory.json": memory_revision},
        requires_approval=True,
    )
    repeated = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "用户偏好简洁回答", "source": "direct_user"}},
        idempotency_key=key,
        expected_state_revisions={"agent_memory.json": memory_revision},
        requires_approval=True,
    )
    expect(repeated.proposal_id == proposal.proposal_id, "create_or_get returns the deterministic proposal")

    approval = proposals.approval_for(proposal, approved_by="state-proposal-smoke")
    handler_entered = threading.Event()
    release_handler = threading.Event()
    count_lock = threading.Lock()
    calls = {"count": 0}
    primary_results: list[Any] = []
    follower_results: list[Any] = []

    def handler(claimed: Any) -> dict[str, Any]:
        with count_lock:
            calls["count"] += 1
        expect(claimed.status == "committing", "handler receives an atomically claimed proposal")
        handler_entered.set()
        if not release_handler.wait(timeout=5):
            raise TimeoutError("smoke did not release proposal handler")
        return {"written": True, "effect": claimed.effect}

    primary = threading.Thread(
        target=lambda: primary_results.append(proposals.commit(proposal.proposal_id, handler, approval=approval))
    )
    primary.start()
    expect(handler_entered.wait(timeout=5), "first concurrent caller enters handler")

    followers = [
        threading.Thread(
            target=lambda: follower_results.append(
                proposals.commit(proposal.proposal_id, handler, approval=approval)
            )
        )
        for _ in range(8)
    ]
    for thread in followers:
        thread.start()
    release_handler.set()
    primary.join(timeout=5)
    for thread in followers:
        thread.join(timeout=5)
    expect(not primary.is_alive(), "claimed handler completes")
    expect(all(not thread.is_alive() for thread in followers), "concurrent callers finish after the atomic handler")
    expect(
        len(follower_results) == 8
        and all(result.status in {"in_progress", "committed"} and result.replayed for result in follower_results),
        "concurrent callers replay the existing claim or committed result",
        follower_results,
    )
    expect(calls["count"] == 1, "concurrent commit invokes the supplied handler once", calls)
    expect(
        len(primary_results) == 1
        and primary_results[0].status == "committed"
        and primary_results[0].applied,
        "first caller records a committed result",
        primary_results,
    )

    replay = proposals.commit(proposal.proposal_id, handler, approval=approval)
    expect(
        replay.status == "committed" and replay.replayed and calls["count"] == 1,
        "sequential repeat returns the recorded result without reinvoking handler",
        replay,
    )
    persisted = proposals.get(proposal.proposal_id)
    expect(
        persisted.status == "committed"
        and persisted.commit_result is not None
        and persisted.commit_result.output == {"written": True, "effect": "memory.write"},
        "committed result is durable in state_change_proposals.json",
        persisted,
    )

    try:
        proposals.create_or_get(
            effect="memory.write",
            payload={"patch": {"memory": "不同内容", "source": "direct_user"}},
            idempotency_key=key,
            expected_state_revisions={"agent_memory.json": memory_revision},
            requires_approval=True,
        )
        raise AssertionError("same idempotency key accepted a different payload")
    except ProposalConflictError:
        pass
    expect(calls["count"] == 1, "same-key different-payload conflict is rejected")


def test_live_handler_cannot_be_reconciled_away(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    proposal = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "reconcile-race-smoke"}},
        idempotency_key=deterministic_idempotency_key("event-reconcile-race", "act-memory"),
        requires_approval=False,
    )
    handler_entered = threading.Event()
    release_handler = threading.Event()
    calls = {"count": 0}
    commit_results: list[Any] = []
    reconcile_results: list[Any] = []

    def handler(_: Any) -> dict[str, Any]:
        calls["count"] += 1
        handler_entered.set()
        if not release_handler.wait(timeout=5):
            raise TimeoutError("smoke did not release active handler")
        return {"status": "written", "value": "race-safe"}

    committer = threading.Thread(
        target=lambda: commit_results.append(proposals.commit(proposal.proposal_id, handler))
    )
    committer.start()
    expect(handler_entered.wait(timeout=5), "race smoke enters the active handler")

    reconciler = threading.Thread(
        target=lambda: reconcile_results.append(
            proposals.reconcile_indeterminate(
                proposal.proposal_id,
                reason="attempted while handler is still active",
            )
        )
    )
    reconciler.start()
    reconciler.join(timeout=0.1)
    expect(
        reconciler.is_alive(),
        "reconciliation waits instead of stealing a live handler claim",
    )

    release_handler.set()
    committer.join(timeout=5)
    reconciler.join(timeout=5)
    expect(
        not committer.is_alive() and not reconciler.is_alive(),
        "commit and delayed reconciliation both finish",
    )
    expect(
        len(commit_results) == 1
        and commit_results[0].status == "committed"
        and commit_results[0].applied is True
        and calls["count"] == 1,
        "active handler retains its claim and commits exactly once",
        commit_results,
    )
    expect(
        len(reconcile_results) == 1
        and reconcile_results[0].status == "committed"
        and reconcile_results[0].replayed,
        "late reconciler replays the completed handler result",
        reconcile_results,
    )


def test_handler_claim_is_rechecked_after_execution(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    proposal = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "claim-token-smoke"}},
        idempotency_key=deterministic_idempotency_key("event-claim-token", "act-memory"),
        requires_approval=False,
    )

    def changes_own_claim(_: Any) -> dict[str, Any]:
        def replace_claim_token(document: dict[str, Any]) -> dict[str, Any]:
            stored = document.get("proposals", {}).get(proposal.proposal_id)
            if isinstance(stored, dict):
                stored["claim_token"] = "f" * 32
            return document

        world.mutate_json("state_change_proposals.json", replace_claim_token)
        return {"status": "written"}

    result = proposals.commit(proposal.proposal_id, changes_own_claim)
    persisted = proposals.get(proposal.proposal_id)
    expect(
        result.status == "indeterminate"
        and result.applied is None
        and persisted.status == "indeterminate"
        and "claim ownership changed" in str(result.error or ""),
        "post-handler claim mismatch cannot be recorded as committed",
        result,
    )


def test_business_failure_outputs_are_not_committed(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    cases = [
        (
            "blocked",
            {"status": "blocked", "reason": "memory policy rejected the patch"},
            "failed",
            False,
        ),
        (
            "needs-parameters",
            {"status": "needs_parameters", "message": "location is required"},
            "failed",
            False,
        ),
        (
            "explicit-false",
            {"status": "completed", "success": False},
            "failed",
            False,
        ),
        (
            "indeterminate",
            {"status": "indeterminate", "reason": "remote acknowledgement was lost"},
            "indeterminate",
            None,
        ),
    ]
    for label, output, expected_status, expected_applied in cases:
        proposal = proposals.create_or_get(
            effect="memory.write",
            payload={"patch": {"memory": f"business-outcome-{label}"}},
            idempotency_key=deterministic_idempotency_key(
                f"event-business-{label}",
                "act-memory",
            ),
            requires_approval=False,
        )
        calls = {"count": 0}

        def handler(_: Any, *, returned: dict[str, Any] = output) -> dict[str, Any]:
            calls["count"] += 1
            return returned

        result = proposals.commit(proposal.proposal_id, handler)
        replay = proposals.commit(proposal.proposal_id, handler)
        expect(
            result.status == expected_status
            and result.applied is expected_applied
            and result.output == output,
            f"{label} handler outcome is not recorded as applied success",
            result,
        )
        expect(
            replay.status == expected_status and replay.replayed and calls["count"] == 1,
            f"{label} handler outcome is durable and not retried",
            replay,
        )

    success = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "business-outcome-success"}},
        idempotency_key=deterministic_idempotency_key("event-business-success", "act-memory"),
        requires_approval=False,
    )
    committed = proposals.commit(
        success.proposal_id,
        lambda _: {"status": "written", "item": {"memory": "saved"}},
    )
    expect(
        committed.status == "committed" and committed.applied is True,
        "legacy success-shaped handler output remains committed",
        committed,
    )


def test_legacy_registry_without_claim_token(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    proposal = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "legacy-registry-smoke"}},
        idempotency_key=deterministic_idempotency_key("event-legacy-registry", "act-memory"),
        requires_approval=False,
    )

    def remove_new_field(document: dict[str, Any]) -> dict[str, Any]:
        stored = document.get("proposals", {}).get(proposal.proposal_id)
        if isinstance(stored, dict):
            stored.pop("claim_token", None)
        return document

    world.mutate_json("state_change_proposals.json", remove_new_field)
    legacy = proposals.get(proposal.proposal_id)
    result = proposals.commit(
        legacy.proposal_id,
        lambda _: {"status": "written", "legacy_document": True},
    )
    expect(
        legacy.claim_token is None
        and result.status == "committed"
        and result.applied is True,
        "v1 proposal documents without claim_token remain readable and committable",
        result,
    )


def test_stale_revision(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    current = int(world.read_json("user_commitments.json").get("_state_revision") or 0)
    proposal = proposals.create_or_get(
        effect="commitment.mutate",
        payload={"operation": "cancel", "target": "daily-weather"},
        idempotency_key=deterministic_idempotency_key("event-stale", "act-cancel"),
        expected_state_revisions={"user_commitments.json": current},
        requires_approval=False,
    )
    world.patch_json("user_commitments.json", {"smoke_revision_change": True})
    calls = {"count": 0}

    def handler(_: Any) -> dict[str, bool]:
        calls["count"] += 1
        return {"cancelled": True}

    result = proposals.commit(proposal.proposal_id, handler)
    expect(
        result.status == "stale_revision"
        and not result.applied
        and calls["count"] == 0
        and result.observed_state_revisions["user_commitments.json"] > current,
        "stale expected revision blocks the handler",
        result,
    )


def test_expiry(root: Path) -> None:
    clock = MutableClock()
    proposals = StateChangeProposalStore(WorldStateStore(root), clock=clock)
    proposal = proposals.create_or_get(
        effect="proactive.create",
        payload={"kind": "weather", "location": "上海", "schedule": "09:00"},
        idempotency_key=deterministic_idempotency_key("event-expired", "act-weather"),
        requires_approval=False,
        expires_at=clock() + timedelta(seconds=30),
    )
    clock.advance(seconds=31)
    calls = {"count": 0}

    def handler(_: Any) -> dict[str, bool]:
        calls["count"] += 1
        return {"created": True}

    result = proposals.commit(proposal.proposal_id, handler)
    expect(
        result.status == "expired" and not result.applied and calls["count"] == 0,
        "expired proposal blocks the handler",
        result,
    )


def test_approval_missing_then_commit(root: Path) -> None:
    clock = MutableClock()
    proposals = StateChangeProposalStore(WorldStateStore(root), clock=clock)
    proposal = proposals.create_or_get(
        effect="profile.write",
        payload={"profile": {"response_style": "concise"}},
        idempotency_key=deterministic_idempotency_key("event-approval", "act-preference"),
        requires_approval=True,
    )
    calls = {"count": 0}

    def handler(_: Any) -> dict[str, bool]:
        calls["count"] += 1
        return {"updated": True}

    missing = proposals.commit(proposal.proposal_id, handler)
    expect(
        missing.status == "approval_required"
        and not missing.applied
        and calls["count"] == 0
        and proposals.get(proposal.proposal_id).status == "pending",
        "missing approval is recorded but leaves proposal eligible for approval",
        missing,
    )
    approval = proposals.approval_for(proposal, approved_by="state-proposal-smoke")
    committed = proposals.commit(proposal.proposal_id, handler, approval=approval)
    expect(
        committed.status == "committed" and committed.applied and calls["count"] == 1,
        "valid payload-bound approval permits one commit",
        committed,
    )


def test_indeterminate_result_and_durable_replay(root: Path) -> None:
    world = WorldStateStore(root)
    proposals = StateChangeProposalStore(world)
    revision = int(world.read_json("agent_memory.json").get("_state_revision") or 0)
    proposal = proposals.create_or_get(
        effect="memory.write",
        payload={"patch": {"memory": "indeterminate-smoke"}},
        idempotency_key=deterministic_idempotency_key("event-indeterminate", "act-memory"),
        expected_state_revisions={"agent_memory.json": revision},
        requires_approval=False,
    )
    calls = {"count": 0}

    def writes_then_returns_invalid(_: Any) -> Any:
        calls["count"] += 1
        world.patch_json("agent_memory.json", {"indeterminate_smoke": True})
        return {"not-json-a-set": {1, 2, 3}}

    result = proposals.commit(proposal.proposal_id, writes_then_returns_invalid)
    expect(
        result.status == "indeterminate"
        and result.applied is None
        and calls["count"] == 1
        and bool(world.read_json("agent_memory.json").get("indeterminate_smoke")),
        "post-effect output failure is recorded as indeterminate rather than falsely unapplied",
        result,
    )
    restarted = StateChangeProposalStore(WorldStateStore(root))
    replay = restarted.commit(proposal.proposal_id, writes_then_returns_invalid)
    expect(
        replay.status == "indeterminate" and replay.replayed and calls["count"] == 1,
        "a new coordinator instance replays the durable result without rerunning the handler",
        replay,
    )


def test_registry_layout_and_health(root: Path) -> None:
    world = WorldStateStore(root)
    expect(
        world.relative_path_for("state_change_proposals.json") == "runtime/state_change_proposals.json",
        "proposal registry uses the runtime state boundary",
    )
    expect(
        "state_change_proposals" in world.read_all(),
        "proposal registry is included in world snapshots",
    )
    health = world.state_health()
    proposal_health = next(
        (
            item
            for item in health.get("items", [])
            if isinstance(item, dict) and item.get("name") == "state_change_proposals.json"
        ),
        {},
    )
    expect(
        proposal_health.get("health_status") == "fresh" and proposal_health.get("ttl_seconds") == 0,
        "proposal registry participates in durable state health",
        proposal_health,
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-state-proposal-smoke-") as tmp:
        base = Path(tmp)
        test_repeat_and_concurrent_commit_once(base / "concurrency")
        test_live_handler_cannot_be_reconciled_away(base / "reconcile-race")
        test_handler_claim_is_rechecked_after_execution(base / "claim-recheck")
        test_business_failure_outputs_are_not_committed(base / "business-outcomes")
        test_legacy_registry_without_claim_token(base / "legacy-registry")
        test_stale_revision(base / "stale")
        test_expiry(base / "expired")
        test_approval_missing_then_commit(base / "approval")
        test_indeterminate_result_and_durable_replay(base / "indeterminate")
        test_registry_layout_and_health(base / "registry")
    print("state proposal smoke passed")


if __name__ == "__main__":
    main()
