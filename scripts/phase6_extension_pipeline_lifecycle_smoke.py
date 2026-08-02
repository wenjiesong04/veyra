from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase6_extension_pipeline_test_support import (  # noqa: E402
    advance_kwargs,
    build_context,
    expect,
    start_kwargs,
)


def run() -> None:
    context = build_context()
    coordinator = context["coordinator"]
    deployment = context["deployment"]

    first = coordinator.start(**start_kwargs())
    expect(
        first["stage"] == "AWAITING_SCOPED_CANARY_APPROVAL"
        and first["status"] == "awaiting_independent_approval"
        and first["pending_review"]["target_mode"] == "scoped_canary"
        and deployment.approval_calls == 0,
        "start executes generation through read-only canary then stops for independent scoped approval",
        first,
    )
    expect(
        first["receipts"]["shadow_invocation"]["invocation_status"]
        == "discarded"
        and first["receipts"]["shadow_invocation"]["output_discarded"]
        is True
        and first["receipts"]["read_only_canary_invocation"][
            "invocation_status"
        ]
        == "passed",
        "shadow discards output before the read-only canary passes",
    )

    scoped_review_id = first["pending_review"]["review_id"]
    deployment.approve_outside_coordinator(scoped_review_id)
    second = coordinator.advance(
        **advance_kwargs(
            first,
            operation_id="pipeline-scoped-resume",
            review_id=scoped_review_id,
        )
    )
    expect(
        second["stage"] == "AWAITING_PROMOTION_APPROVAL"
        and second["status"] == "awaiting_independent_approval"
        and second["last_issue_code"] is None
        and second["pending_review"]["target_mode"] == "promoted"
        and second["receipts"]["scoped_canary_invocation"][
            "invocation_status"
        ]
        == "passed"
        and deployment.approval_calls == 1,
        "approved scoped review permits one explicit scoped canary and then stops for a separate promotion review",
        second,
    )

    promotion_review_id = second["pending_review"]["review_id"]
    deployment.approve_outside_coordinator(promotion_review_id)
    third = coordinator.advance(
        **advance_kwargs(
            second,
            operation_id="pipeline-promotion-resume",
            review_id=promotion_review_id,
        )
    )
    expect(
        third["stage"] == "PROMOTED"
        and third["status"] == "promoted"
        and third["last_issue_code"] is None
        and third["next_action"] == "complete"
        and third["receipts"]["capability"]["deployment_id"]
        == third["receipts"]["deployment"]["deployment_id"]
        and deployment.approval_calls == 2,
        "second independent approval plus explicit resume atomically publishes the promoted source-free capability",
        third,
    )

    serialized = json.dumps(
        context["store"].documents,
        sort_keys=True,
        ensure_ascii=False,
    )
    expect(
        "PRIVATE_CANARY_INPUT" not in serialized
        and "PRIVATE_CANARY_OUTPUT" not in serialized
        and "def run_extension" not in serialized
        and '"operation_id"' not in serialized
        and "approver_token" not in serialized,
        "coordinator durability contains no source, raw input, raw output, raw operation id, or approver credential",
    )

    print("Phase 6 governed extension pipeline lifecycle smoke passed")


if __name__ == "__main__":
    run()
