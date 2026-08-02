from __future__ import annotations

from pathlib import Path
import sys

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_pipeline import (  # noqa: E402
    EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION,
    ExtensionPipelineAdvanceCommand,
)
from runtime.extension_pipeline_coordinator import (  # noqa: E402
    ExtensionPipelineConflictError,
    ExtensionPipelineNotFoundError,
    ExtensionPipelineUnauthorizedError,
    ExtensionPipelineUnavailableError,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    valid_test_bundle,
)
from scripts.phase6_extension_pipeline_test_support import (  # noqa: E402
    SESSION,
    TOKEN,
    USER,
    WORKSPACE,
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
    disabled = build_context(enabled=False)["coordinator"]
    expect_raises(
        (ExtensionPipelineUnavailableError,),
        lambda: disabled.start(**start_kwargs()),
        "pipeline start is disabled by default policy even with a valid token",
    )

    context = build_context()
    coordinator = context["coordinator"]
    expect_raises(
        (ExtensionPipelineUnauthorizedError,),
        lambda: coordinator.status(control_token="wrong-token"),
        "all pipeline projections require the private control credential",
    )
    created = coordinator.start(**start_kwargs())

    before_reads = context["store"].snapshot()
    status = coordinator.status(control_token=TOKEN)
    listed = coordinator.list(
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        control_token=TOKEN,
    )
    fetched = coordinator.get(
        pipeline_id=created["pipeline_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        control_token=TOKEN,
    )
    after_reads = context["store"].snapshot()
    expect(
        before_reads == after_reads
        and status["reads_are_pure_coordinator_snapshots"] is True
        and listed["state_mutated"] is False
        and fetched["pipeline_id"] == created["pipeline_id"],
        "status/list/get are byte-invariant coordinator-only snapshots",
    )
    expect(
        status["automatic_approval"] is False
        and status["automatic_promotion"] is False
        and status["natural_language_trigger"] is False
        and status["persistence"]["scope"] == "coordinator_state_only",
        "public status states the non-autonomous authority boundary completely",
    )

    replay_before = context["store"].snapshot()
    replay = coordinator.start(**start_kwargs())
    expect(
        replay["operation_replayed"] is True
        and replay["pipeline_revision"] == created["pipeline_revision"]
        and replay_before == context["store"].snapshot(),
        "exact start replay returns its durable source-free result without another effect",
    )

    rebound = start_kwargs()
    rebound["request_id"] = "rebound-request"
    expect_raises(
        (ExtensionPipelineConflictError,),
        lambda: coordinator.start(**rebound),
        "one pipeline operation id cannot be rebound to another request",
    )
    expect_raises(
        (ExtensionPipelineNotFoundError,),
        lambda: coordinator.get(
            pipeline_id=created["pipeline_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id="other-session",
            control_token=TOKEN,
        ),
        "pipeline reads are isolated by exact owner principal and session",
    )

    expect_raises(
        (ValidationError,),
        lambda: ExtensionPipelineAdvanceCommand.model_validate(
            {
                "schema_version": (
                    EXTENSION_PIPELINE_ADVANCE_COMMAND_SCHEMA_VERSION
                ),
                "operation_id": "advance",
                "expected_pipeline_revision": created[
                    "pipeline_revision"
                ],
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "test_bundle": valid_test_bundle(),
                "canary_input": {"name": "A"},
                "review_id": created["pending_review"]["review_id"],
                "approver_token": "forbidden-in-pipeline-command",
            },
            strict=True,
        ),
        "pipeline command schema forbids embedding an approver credential",
    )

    print("Phase 6 governed extension pipeline contract smoke passed")


if __name__ == "__main__":
    run()
