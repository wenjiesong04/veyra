#!/usr/bin/env python3
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.bounded_extension_generator import (  # noqa: E402
    BoundedExtensionGeneratorOutputError,
)
from runtime.extension_generation_gate import (  # noqa: E402
    ExtensionGenerationConflictError,
    ExtensionGenerationNotFoundError,
    ExtensionGenerationUnauthorizedError,
    ExtensionGenerationUnavailableError,
)
from scripts.phase6_extension_generation_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    FakeGenerator,
    SESSION,
    USER,
    WORKSPACE,
    directory_bytes,
    generation_command,
    prepare_generation_context,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> BaseException:
    try:
        call()
    except errors as exc:
        print(f"PASS {label}")
        return exc
    raise AssertionError(f"{label}: call did not fail closed")


def main() -> int:
    context = prepare_generation_context()
    try:
        before_unrelated = {
            name: context.store.read_json(name)
            for name in (
                "agent_memory.json",
                "durable_case_state.json",
                "review_queue.json",
                "tool_governance_state.json",
                "self_improvement_proposals.json",
            )
        }
        created = generation_command(context)
        expect(
            created["stored_stage"] == "GENERATION_QUARANTINED"
            and created["generation_status"] == "quarantined"
            and created["artifact_id"].startswith("extart_")
            and context.generator.calls == 1,
            "one bounded model result enters private artifact quarantine",
            created,
        )
        expect(
            "source" not in created
            and "user_id" not in created
            and "workspace_id" not in created
            and not created["promotion_authorized"]
            and created["capability_registry_visible"] is False
            and created["authority"]["code_generation"] is True
            and created["authority"]["candidate_execution"] is False
            and created["authority"]["signing"] is False,
            "public generation record is source-free and grants no downstream authority",
            created,
        )

        replay = generation_command(context)
        expect(
            replay["operation_replayed"] is True
            and replay["generation_id"] == created["generation_id"]
            and context.generator.calls == 1,
            "operation replay never calls the model or artifact admission twice",
            replay,
        )
        existing = generation_command(
            context,
            operation_id="generation-second-operation",
        )
        expect(
            existing["generation_existing"] is True
            and existing["generation_id"] == created["generation_id"]
            and context.generator.calls == 1,
            "candidate identity permits only one generation attempt",
            existing,
        )
        expect_raises(
            (ExtensionGenerationConflictError,),
            lambda: generation_command(
                context,
                operation_id="generation-start",
                request_id="rebound-request",
            ),
            "operation identity cannot be rebound",
        )
        expect_raises(
            (ExtensionGenerationUnauthorizedError,),
            lambda: context.gate.get(
                generation_id=created["generation_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token="wrong-control-token-value",
            ),
            "private generation reads require the control principal",
        )
        expect_raises(
            (ExtensionGenerationNotFoundError,),
            lambda: context.gate.get(
                generation_id=created["generation_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id="another-session",
                control_token=CONTROL_TOKEN,
            ),
            "same owner in another session cannot read the generation",
        )

        before_gets = directory_bytes(context.root)
        listed = context.gate.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        fetched = context.gate.get(
            generation_id=created["generation_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        integrity = context.gate.integrity(
            generation_id=created["generation_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        after_gets = directory_bytes(context.root)
        expect(
            listed["count"] == 1
            and fetched["generation_id"] == created["generation_id"]
            and integrity["binding_integrity_status"] == "validated"
            and integrity["report_integrity_status"] == "validated"
            and before_gets == after_gets,
            "list detail and integrity are byte-for-byte pure reads",
        )
        expect(
            {
                name: context.store.read_json(name)
                for name in before_unrelated
            }
            == before_unrelated,
            "generation does not mutate Agent Case Review tool or self-improvement state",
        )
    finally:
        context.close()

    rejected_generator = FakeGenerator(
        crash=BoundedExtensionGeneratorOutputError("invalid strict output")
    )
    rejected_context = prepare_generation_context(
        generator=rejected_generator,
        extension_id="example.generated_rejected",
        operation_prefix="generation-rejected",
    )
    try:
        rejected = generation_command(
            rejected_context,
            operation_id="generation-rejected-start",
            request_id="generation-rejected-request",
        )
        expect(
            rejected["stored_stage"] == "GENERATION_REJECTED"
            and rejected["failure_code"] == "model_output_invalid"
            and rejected["artifact_id"] is None
            and rejected_generator.calls == 1,
            "invalid model output is durably rejected without an artifact",
            rejected,
        )
    finally:
        rejected_context.close()

    crash_generator = FakeGenerator(crash=KeyboardInterrupt("simulated crash"))
    crash_context = prepare_generation_context(
        generator=crash_generator,
        extension_id="example.generated_indeterminate",
        operation_prefix="generation-indeterminate",
    )
    try:
        expect_raises(
            (KeyboardInterrupt,),
            lambda: generation_command(
                crash_context,
                operation_id="generation-crash-start",
                request_id="generation-crash-request",
            ),
            "unknown post-dispatch crash is not converted into a false rejection",
        )
        crash_context.clock.current += timedelta(seconds=181)
        records = crash_context.gate.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            records["count"] == 1
            and records["generations"][0]["stored_stage"]
            == "GENERATION_STARTED"
            and records["generations"][0]["effective_status"]
            == "GENERATION_INDETERMINATE"
            and crash_generator.calls == 1,
            "stale STARTED evidence remains indeterminate and cannot retry",
            records,
        )
        replay = generation_command(
            crash_context,
            operation_id="generation-crash-start",
            request_id="generation-crash-request",
        )
        expect(
            replay["operation_replayed"] is True
            and replay["effective_status"] == "GENERATION_INDETERMINATE"
            and crash_generator.calls == 1,
            "indeterminate replay never calls the external model again",
            replay,
        )
    finally:
        crash_context.close()

    unavailable = prepare_generation_context(
        generator=FakeGenerator(configured=False),
        extension_id="example.generated_unavailable",
        operation_prefix="generation-unavailable",
    )
    try:
        expect_raises(
            (ExtensionGenerationUnavailableError,),
            lambda: generation_command(
                unavailable,
                operation_id="generation-unavailable-start",
                request_id="generation-unavailable-request",
            ),
            "unconfigured model fails before durable dispatch",
        )
    finally:
        unavailable.close()

    print("Phase 6 extension generation lifecycle smoke passed: 14/14")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
