from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.extension_pipeline_coordinator import (  # noqa: E402
    ExtensionPipelineConflictError,
    ExtensionPipelineStorageError,
)
from scripts.phase6_extension_pipeline_test_support import (  # noqa: E402
    advance_kwargs,
    build_context,
    expect,
    start_kwargs,
)


def expect_raises(
    errors: tuple[type[BaseException], ...], call: object, label: str
) -> None:
    try:
        call()  # type: ignore[operator]
    except errors:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: call did not fail closed")


def run() -> None:
    context = build_context()
    store = context["store"]
    coordinator = context["coordinator"]
    generation = context["generation"]
    deployment = context["deployment"]

    # Claim is mutation 1. Inject a storage crash at the first post-generation
    # checkpoint, leaving the exact outer and child operation claims durable.
    store.fail_on_mutation = 2
    expect_raises(
        (ExtensionPipelineStorageError,),
        lambda: coordinator.start(**start_kwargs()),
        "checkpoint storage failure leaves the pipeline recoverable instead of making a false completion claim",
    )
    expect(
        len(generation.effects) == 1,
        "the generated-source effect occurred under one deterministic child operation",
    )

    resumed = coordinator.start(**start_kwargs())
    expect(
        resumed["stage"] == "AWAITING_SCOPED_CANARY_APPROVAL"
        and len(generation.effects) == 1,
        "same-operation crash resume replays the child identity without duplicating generation",
        resumed,
    )

    stale = advance_kwargs(
        resumed,
        operation_id="pipeline-stale-cas",
        review_id=None,
    )
    stale["expected_pipeline_revision"] -= 1
    expect_raises(
        (ExtensionPipelineConflictError,),
        lambda: coordinator.advance(**stale),
        "explicit advance requires the exact pipeline revision CAS",
    )

    wrong_review = advance_kwargs(
        resumed,
        operation_id="pipeline-wrong-review",
        review_id="review-other",
    )
    blocked = coordinator.advance(**wrong_review)
    expect(
        blocked["stage"] == "AWAITING_SCOPED_CANARY_APPROVAL"
        and blocked["status"] == "blocked"
        and blocked["last_issue_code"]
        == "pending_review_identity_mismatch"
        and "review-other" not in str(blocked),
        "wrong review identity is safely summarized and cannot cross the approval boundary",
        blocked,
    )

    replay_before = store.snapshot()
    blocked_replay = coordinator.advance(**wrong_review)
    expect(
        blocked_replay["operation_replayed"] is True
        and blocked_replay["pipeline_revision"]
        == blocked["pipeline_revision"]
        and replay_before == store.snapshot(),
        "advance replay returns the exact durable blocked result without mutation",
    )

    rebound = dict(wrong_review)
    rebound["canary_input"] = {"name": "rebound", "count": 1}
    expect_raises(
        (ExtensionPipelineConflictError,),
        lambda: coordinator.advance(**rebound),
        "one advance operation cannot be rebound to another input digest",
    )

    scoped_review_id = resumed["pending_review"]["review_id"]
    deployment.approve_outside_coordinator(scoped_review_id)
    recovered = coordinator.advance(
        **advance_kwargs(
            blocked,
            operation_id="pipeline-correct-review-recovery",
            review_id=scoped_review_id,
        )
    )
    expect(
        recovered["stage"] == "AWAITING_PROMOTION_APPROVAL"
        and recovered["status"] == "awaiting_independent_approval"
        and recovered["last_issue_code"] is None
        and recovered["pending_review"]["target_mode"] == "promoted",
        "a later exact scoped review clears the prior issue and stops cleanly at the independent promotion boundary",
        recovered,
    )

    print("Phase 6 governed extension pipeline recovery smoke passed")


if __name__ == "__main__":
    run()
