#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from core.durable_case import (  # noqa: E402
    CaseCheckpoint,
    CheckpointEffectState,
    DialogueMessageType,
    DialogueRecord,
)
from core.world_state import DURABLE_STATE_FILES, WorldStateStore
from runtime.durable_case_store import (
    CaseNotFoundError,
    CaseOperationConflictError,
    CaseRevisionConflictError,
    CaseTraceBackpressureError,
    CaseTransitionError,
    DurableCaseStore,
)


def expect(condition: bool, message: str, detail: Any = None) -> None:
    if not condition:
        suffix = f": {detail!r}" if detail is not None else ""
        raise AssertionError(f"{message}{suffix}")


def checkpoint(
    checkpoint_id: str,
    *,
    evidence_refs: list[str] | None = None,
    effect_state: str = "not_started",
) -> CaseCheckpoint:
    return CaseCheckpoint.model_validate(
        {
            "checkpoint_id": checkpoint_id,
            "phase": "agent_dialogue",
            "operation_id": f"dispatch:{checkpoint_id}",
            "step_id": "step-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "session_key": "agent:openclaw:main",
            "binding_digest": "a" * 64,
            "executor": "openclaw",
            "target_agent": "openclaw",
            "dialogue_message_id": "message-1",
            "result_status": "revoked" if evidence_refs else "prepared",
            "effect_state": CheckpointEffectState(effect_state),
            "evidence_refs": evidence_refs or [],
            "recorded_at": datetime.now(timezone.utc),
        },
        strict=True,
    )


def dialogue(message_id: str, *, case_revision: int) -> DialogueRecord:
    turn_index = 1
    return DialogueRecord.model_validate(
        {
            "message_id": message_id,
            "message_type": DialogueMessageType.TASK_REQUEST,
            "sender": "veyra",
            "direction": "veyra_to_agent",
            "case_revision": case_revision,
            "turn_index": turn_index,
            "content": {
                "message_id": message_id,
                "message_type": "TASK_REQUEST",
                "sender": "veyra",
                "case_revision": case_revision,
                "turn_index": turn_index,
                "in_reply_to": None,
                "goal_ref": "case.user_goal",
                "constraints": ["analysis_only"],
            },
            "authority_granted": False,
            "evidence_verified": False,
            "recorded_at": datetime.now(timezone.utc),
        },
        strict=True,
    )


def agent_option_set(
    message_id: str,
    *,
    case_revision: int,
    in_reply_to: str,
) -> DialogueRecord:
    turn_index = 1
    return DialogueRecord.model_validate(
        {
            "message_id": message_id,
            "message_type": DialogueMessageType.OPTION_SET,
            "sender": "agent",
            "direction": "agent_to_veyra",
            "case_revision": case_revision,
            "turn_index": turn_index,
            "in_reply_to": in_reply_to,
            "content": {
                "message_id": message_id,
                "message_type": "OPTION_SET",
                "sender": "agent",
                "case_revision": case_revision,
                "turn_index": turn_index,
                "in_reply_to": in_reply_to,
                "options": [
                    {"option_id": "option-a", "summary": "Read-only analysis"},
                    {"option_id": "option-b", "summary": "Pause for evidence"},
                ]
            },
            "authority_granted": False,
            "evidence_verified": False,
            "recorded_at": datetime.now(timezone.utc),
        },
        strict=True,
    )


def new_case(
    cases: DurableCaseStore,
    *,
    event_id: str = "evt-case-1",
    user_id: str = "user-a",
    workspace_id: str = "workspace-a",
    user_goal: str = "Compare two bounded implementation options.",
) -> dict[str, Any]:
    return cases.admit_event(
        event_id=event_id,
        user_id=user_id,
        workspace_id=workspace_id,
        user_goal=user_goal,
        situation_id="situation-a",
        goal_ids=["goal-a"],
        commitment_ids=["commitment-a"],
    )


def test_admission_scope_and_replay(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    first = new_case(cases)
    replay = new_case(cases)
    replay_with_new_delivery_operation = cases.admit_event(
        event_id="evt-case-1",
        user_id="user-a",
        workspace_id="workspace-a",
        user_goal="Compare two bounded implementation options.",
        situation_id="situation-a",
        goal_ids=["goal-a"],
        commitment_ids=["commitment-a"],
        operation_id="redelivery-operation",
    )
    other_scope = new_case(
        cases,
        event_id="evt-case-1",
        user_id="user-b",
        workspace_id="workspace-a",
    )
    expect(first["status"] == "QUALIFIED", "admission starts QUALIFIED")
    expect(first["revision"] == 1, "admission starts at case revision 1")
    expect(replay["case_id"] == first["case_id"], "event replay reuses case")
    expect(replay["operation_replayed"] is True, "event replay is explicit")
    expect(
        replay_with_new_delivery_operation["case_id"] == first["case_id"]
        and replay_with_new_delivery_operation["operation_replayed"] is True,
        "event identity deduplicates across delivery operation IDs",
    )
    expect(
        other_scope["case_id"] != first["case_id"],
        "same event in another scope cannot collide",
    )
    try:
        cases.get_case(
            case_id=first["case_id"],
            user_id="user-b",
            workspace_id="workspace-a",
        )
    except CaseNotFoundError:
        pass
    else:
        raise AssertionError("cross-scope read must look like not-found")
    try:
        new_case(cases, user_goal="Changed semantics for the same event.")
    except CaseOperationConflictError:
        pass
    else:
        raise AssertionError("changed admission semantics must conflict")


def test_cas_operation_and_lifecycle(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    admitted = new_case(cases)
    transitioned = cases.transition(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="deliberate-once",
        expected_revision=1,
        to_status="DELIBERATING",
        reason="Begin bounded deliberation.",
    )
    expect(transitioned["revision"] == 2, "transition advances revision")
    replay = cases.transition(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="deliberate-once",
        expected_revision=1,
        to_status="DELIBERATING",
        reason="Begin bounded deliberation.",
    )
    expect(replay["operation_replayed"] is True, "operation replay is stable")
    expect(replay["revision"] == 2, "operation replay does not mutate")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="deliberate-once",
            expected_revision=2,
            to_status="PROPOSED",
            reason="Reuse operation with changed semantics.",
        )
    except CaseOperationConflictError:
        pass
    else:
        raise AssertionError("operation identity reuse must conflict")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="stale-operation",
            expected_revision=1,
            to_status="PROPOSED",
            reason="Stale writer.",
        )
    except CaseRevisionConflictError:
        pass
    else:
        raise AssertionError("stale case revision must conflict")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="invalid-lifecycle",
            expected_revision=2,
            to_status="CANCELLED",
            reason="Cannot skip revocation.",
        )
    except CaseTransitionError:
        pass
    else:
        raise AssertionError("cancel cannot skip CANCELLING")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="execution-forbidden",
            expected_revision=2,
            to_status="AUTHORIZED",
            reason="Execution state is outside Phase 4.",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("analysis-only lifecycle cannot name AUTHORIZED")


def test_checkpoint_dialogue_and_cancel(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    admitted = new_case(cases)
    with_dialogue = cases.append_dialogue(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="record-task-request",
        expected_revision=1,
        dialogue_message=dialogue("message-1", case_revision=1),
    )
    expect(
        with_dialogue["dialogue"][0]["authority_granted"] is False,
        "dialogue record cannot grant authority",
    )
    with_checkpoint = cases.append_checkpoint(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="checkpoint-dispatch",
        expected_revision=2,
        checkpoint=checkpoint("checkpoint-1"),
    )
    expect(
        with_checkpoint["checkpoints"][0]["executor"] == "openclaw",
        "checkpoint retains adapter recovery identity",
    )
    cancelling = cases.request_cancel(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="cancel-request",
        expected_revision=3,
        reason="User requested cancellation.",
    )
    try:
        cases.complete_cancel(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="cancel-without-proof",
            expected_revision=4,
            reason="No proof.",
            checkpoint=checkpoint("checkpoint-no-proof"),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("cancel completion requires revocation evidence")
    cancelled = cases.complete_cancel(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="cancel-complete",
        expected_revision=4,
        reason="Broker revocation was observed.",
        checkpoint=checkpoint(
            "checkpoint-revoked",
            evidence_refs=["broker-revocation:run-1"],
            effect_state="observed",
        ),
    )
    expect(cancelling["status"] == "CANCELLING", "cancel is two-phase")
    expect(cancelled["status"] == "CANCELLED", "proof completes cancellation")
    expect(cancelled["next_wakeup_at"] is None, "cancel clears wakeup")


def test_reply_binds_parent_request_revision(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    admitted = new_case(cases)
    requested = cases.transition(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="dispatch-task-request",
        expected_revision=1,
        to_status="DELIBERATING",
        reason="Dispatch bounded TASK_REQUEST.",
        dialogue_message=dialogue("task-request-1", case_revision=1),
    )
    expect(requested["revision"] == 2, "request advances case after binding rev1")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="reply-wrong-parent",
            expected_revision=2,
            to_status="PROPOSED",
            reason="Reject unbound Agent reply.",
            dialogue_message=agent_option_set(
                "option-set-wrong-parent",
                case_revision=1,
                in_reply_to="missing-task-request",
            ),
        )
    except CaseOperationConflictError:
        pass
    else:
        raise AssertionError("Agent reply must bind an existing TASK_REQUEST")
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="reply-wrong-revision",
            expected_revision=2,
            to_status="PROPOSED",
            reason="Reject revision substitution.",
            dialogue_message=agent_option_set(
                "option-set-wrong-revision",
                case_revision=2,
                in_reply_to="task-request-1",
            ),
        )
    except CaseRevisionConflictError:
        pass
    else:
        raise AssertionError("Agent reply must echo its parent request revision")
    wrong_turn = agent_option_set(
        "option-set-wrong-turn",
        case_revision=1,
        in_reply_to="task-request-1",
    ).model_dump(mode="python")
    wrong_turn["turn_index"] = 2
    wrong_turn["content"]["turn_index"] = 2
    try:
        cases.transition(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="reply-wrong-turn",
            expected_revision=2,
            to_status="PROPOSED",
            reason="Reject turn substitution.",
            dialogue_message=wrong_turn,
        )
    except CaseOperationConflictError:
        pass
    else:
        raise AssertionError("Agent reply must echo its parent request turn")
    proposed = cases.transition(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="reply-option-set",
        expected_revision=2,
        to_status="PROPOSED",
        reason="Record bounded Agent options.",
        dialogue_message=agent_option_set(
            "option-set-1",
            case_revision=1,
            in_reply_to="task-request-1",
        ),
    )
    expect(proposed["revision"] == 3, "bound Agent reply advances current CAS")
    expect(
        proposed["dialogue"][-1]["case_revision"] == 1,
        "Agent reply preserves parent request revision",
    )
    expect(proposed["status"] == "PROPOSED", "OPTION_SET advances to PROPOSED")


def test_concurrent_cas(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    admitted = new_case(cases)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    lock = threading.Lock()

    def writer(index: int) -> None:
        barrier.wait()
        try:
            cases.transition(
                case_id=admitted["case_id"],
                user_id="user-a",
                workspace_id="workspace-a",
                operation_id=f"concurrent-{index}",
                expected_revision=1,
                to_status="DELIBERATING",
                reason=f"Concurrent writer {index}.",
            )
            outcome = "won"
        except CaseRevisionConflictError:
            outcome = "conflict"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    expect(
        sorted(outcomes) == ["conflict", "won"],
        "exactly one same-revision writer wins",
        outcomes,
    )


def test_trace_outbox_recovery(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    original_append = world.append_jsonl

    def fail_append(name: str, payload: dict[str, Any]) -> None:
        if name == "durable_case_trace.jsonl":
            raise OSError("injected trace append failure")
        original_append(name, payload)

    world.append_jsonl = fail_append  # type: ignore[method-assign]
    admitted = new_case(cases)
    pending = world.read_json("durable_case_state.json")
    expect(
        pending.get("trace_outbox_count") == 1,
        "case commit survives trace append failure",
    )
    world.append_jsonl = original_append  # type: ignore[method-assign]

    original_mutate = world.mutate_json
    fail_ack = {"enabled": True}

    def interrupt_ack(
        name: str, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any]:
        if name != "durable_case_state.json":
            return original_mutate(name, mutator)

        def wrapped(current: dict[str, Any]) -> dict[str, Any] | None:
            before = int(current.get("trace_outbox_count") or 0)
            updated = mutator(current)
            selected = current if updated is None else updated
            after = int(selected.get("trace_outbox_count") or 0)
            if fail_ack["enabled"] and before > after:
                raise OSError("injected acknowledgement interruption")
            return updated

        return original_mutate(name, wrapped)

    world.mutate_json = interrupt_ack  # type: ignore[method-assign]
    try:
        cases.flush_trace_outbox()
    except Exception:
        pass
    else:
        raise AssertionError("injected acknowledgement interruption must surface")
    expect(
        len(world.read_jsonl("durable_case_trace.jsonl")) == 1,
        "trace append completed before acknowledgement interruption",
    )
    expect(
        world.read_json("durable_case_state.json").get("trace_outbox_count") == 1,
        "unacknowledged trace remains durable",
    )
    fail_ack["enabled"] = False
    world.mutate_json = original_mutate  # type: ignore[method-assign]
    recovered = DurableCaseStore(world)
    repair = recovered.flush_trace_outbox()
    expect(repair["deduplicated"] == 1, "restart deduplicates appended trace")
    expect(repair["acknowledged"] == 1, "restart acknowledges pending trace")
    expect(
        recovered.get_case(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
        )["revision"]
        == 1,
        "restart preserves checkpoint",
    )
    trace_text = world.path_for("durable_case_trace.jsonl").read_text(
        encoding="utf-8"
    )
    expect(
        "Compare two bounded implementation options." not in trace_text,
        "trace excludes full user goal",
    )


def test_bounds_and_strict_models(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world, max_checkpoints_per_case=3)
    admitted = new_case(cases)
    once = cases.append_checkpoint(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="checkpoint-one",
        expected_revision=1,
        checkpoint=checkpoint("checkpoint-1"),
    )
    try:
        cases.append_checkpoint(
            case_id=admitted["case_id"],
            user_id="user-a",
            workspace_id="workspace-a",
            operation_id="checkpoint-two",
            expected_revision=once["revision"],
            checkpoint=checkpoint("checkpoint-2"),
        )
    except CaseTraceBackpressureError:
        pass
    else:
        raise AssertionError("bounded checkpoint history must fail closed")
    try:
        DialogueRecord.model_validate(
            {
                **dialogue("strict-message", case_revision=1).model_dump(
                    mode="python"
                ),
                "authority_granted": True,
            },
            strict=True,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("dialogue cannot claim authority")
    expect(
        "durable_case_state.json" in DURABLE_STATE_FILES,
        "case registry is a TTL-zero durable state file",
    )
    state = world.read_json("durable_case_state.json")
    expect(state.get("ttl_seconds") == 0, "case registry TTL is zero")


def test_idempotency_capacity_and_pause_metadata(root: Path) -> None:
    world = WorldStateStore(root)
    cases = DurableCaseStore(world)
    admitted = new_case(cases)
    first_checkpoint = checkpoint("checkpoint-retry")
    recorded = cases.append_checkpoint(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="checkpoint-retry-operation",
        expected_revision=1,
        checkpoint=first_checkpoint,
        reason="Record a retry-safe checkpoint.",
    )
    retry_payload = first_checkpoint.model_dump(mode="python")
    retry_payload["recorded_at"] = (
        first_checkpoint.recorded_at + timedelta(seconds=1)
    )
    replayed = cases.append_checkpoint(
        case_id=admitted["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="checkpoint-retry-operation",
        expected_revision=1,
        checkpoint=retry_payload,
        reason="Record a retry-safe checkpoint.",
    )
    expect(
        replayed["operation_replayed"] is True
        and replayed["revision"] == recorded["revision"],
        "storage timestamps do not break operation idempotency",
    )

    paused_case = new_case(cases, event_id="evt-paused-metadata")
    paused = cases.transition(
        case_id=paused_case["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="pause-metadata",
        expected_revision=1,
        to_status="PAUSED",
        reason="Pause before recording metadata.",
    )
    paused_with_checkpoint = cases.append_checkpoint(
        case_id=paused_case["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="pause-metadata-checkpoint",
        expected_revision=paused["revision"],
        checkpoint=checkpoint("checkpoint-paused"),
    )
    expect(
        paused_with_checkpoint["paused_from_status"] == "QUALIFIED",
        "metadata append preserves the original paused_from_status",
    )

    reserved_world = WorldStateStore(root / "reserved")
    reserved = DurableCaseStore(
        reserved_world,
        max_operations_per_case=5,
        max_checkpoints_per_case=3,
    )
    cancellable = new_case(reserved)
    with_checkpoint = reserved.append_checkpoint(
        case_id=cancellable["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="normal-checkpoint",
        expected_revision=1,
        checkpoint=checkpoint("normal-checkpoint"),
    )
    deliberating = reserved.transition(
        case_id=cancellable["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="normal-transition",
        expected_revision=with_checkpoint["revision"],
        to_status="DELIBERATING",
        reason="Use the last non-cancellation operation slot.",
    )
    cancelling = reserved.request_cancel(
        case_id=cancellable["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="reserved-cancel",
        expected_revision=deliberating["revision"],
        reason="Exercise the reserved cancellation slots.",
        checkpoint=checkpoint("reserved-cancel-request"),
    )
    cancelled = reserved.complete_cancel(
        case_id=cancellable["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="reserved-cancel-complete",
        expected_revision=cancelling["revision"],
        reason="Observed all required revocations.",
        checkpoint=checkpoint(
            "reserved-cancel-complete",
            evidence_refs=["cancel-receipt:reserved"],
            effect_state="observed",
        ),
    )
    expect(
        cancelled["status"] == "CANCELLED",
        "ordinary history cannot consume cancellation safety reserve",
    )
    for kwargs in (
        {"max_operations_per_case": 2},
        {"max_checkpoints_per_case": 1},
    ):
        try:
            DurableCaseStore(WorldStateStore(root / "invalid"), **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe capacity minimum was accepted")

    bounded_world = WorldStateStore(root / "bounded")
    bounded = DurableCaseStore(bounded_world, max_cases=1)
    first = new_case(bounded, event_id="evt-retained")
    bounded.transition(
        case_id=first["case_id"],
        user_id="user-a",
        workspace_id="workspace-a",
        operation_id="close-retained",
        expected_revision=1,
        to_status="CLOSED",
        reason="Close without weakening source-event dedupe.",
    )
    try:
        new_case(bounded, event_id="evt-over-capacity")
    except CaseTraceBackpressureError:
        pass
    else:
        raise AssertionError("Case capacity silently evicted terminal dedupe")
    retained = new_case(bounded, event_id="evt-retained")
    expect(
        retained["operation_replayed"] is True
        and retained["status"] == "CLOSED",
        "terminal Case remains the source-event replay authority",
    )


def main() -> None:
    tests = [
        test_admission_scope_and_replay,
        test_cas_operation_and_lifecycle,
        test_checkpoint_dialogue_and_cancel,
        test_reply_binds_parent_request_revision,
        test_concurrent_cas,
        test_trace_outbox_recovery,
        test_bounds_and_strict_models,
        test_idempotency_capacity_and_pause_metadata,
    ]
    with tempfile.TemporaryDirectory(prefix="veyra-durable-case-") as temp:
        base = Path(temp)
        for test in tests:
            test(base / test.__name__)
            print(f"PASS {test.__name__}")
    print(
        json.dumps(
            {
                "status": "success",
                "tests": len(tests),
                "phase": "phase4",
                "authority": "analysis_only",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
