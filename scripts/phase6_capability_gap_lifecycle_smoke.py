#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.capability_gap_registry import (  # noqa: E402
    STATE_FILE,
    CapabilityGapConflictError,
    CapabilityGapNotFoundError,
    CapabilityGapRegistry,
    CapabilityGapStorageError,
)
from runtime.self_improvement import (  # noqa: E402
    SelfImprovementProposalRegistry,
)
from scripts.phase6_capability_gap_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    RAW_TEXT,
    SESSION,
    USER,
    WORKSPACE,
    deployment_receipt,
    generation_receipt,
    intent,
    link_gap,
    prepare_context,
    proposal_payload,
    record_gap,
    release_receipt,
    validation_receipt,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def content_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".tmp")
    }


class CountingSpecGate:
    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.calls = 0

    def artifact_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return self.wrapped.artifact_subject(**kwargs)


class BrokenGapRegistry:
    def record_from_proposal(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("private-storage-detail-must-not-leak")


def observe(
    context: Any,
    gap: dict[str, Any],
    receipt: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    return context.registry.observe_lifecycle(
        gap_id=gap["gap_id"],
        receipt=receipt,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_gap_revision=gap["gap_revision"],
        operation_id=operation_id,
        control_token=CONTROL_TOKEN,
    )


def main() -> int:
    context = prepare_context()
    try:
        proposal_registry = SelfImprovementProposalRegistry(context.store)
        proposal_registry.capability_gaps = context.registry
        proposal = proposal_registry.propose_from_intent(
            intent(), reason="unknown_proactive_intent"
        )
        linkage = proposal["capability_gap_linkage"]
        expect(
            linkage["status"] == "recorded"
            and linkage["stage"] == "GAP_RECORDED"
            and linkage["required_action"] == "SPEC_REQUIRED"
            and linkage["automatic_advancement"] is False
            and linkage["authority_granted"] is False,
            "unknown proactive proposal automatically records one inert gap",
            linkage,
        )
        first_gap = context.registry.get(
            gap_id=linkage["gap_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        public_text = json.dumps(first_gap, ensure_ascii=False, sort_keys=True)
        expect(
            RAW_TEXT not in public_text
            and "raw_text" not in public_text
            and "topic" not in public_text
            and "desired_outcome" not in public_text
            and first_gap["raw_user_text_present"] is False
            and first_gap["schema_inferred"] is False,
            "public gap projection excludes natural-language proposal content",
            first_gap,
        )

        direct_first = record_gap(context)
        bytes_before_duplicate = context.store.path_for(STATE_FILE).read_bytes()
        direct_second = record_gap(context)
        bytes_after_duplicate = context.store.path_for(STATE_FILE).read_bytes()
        expect(
            direct_first["gap_id"] == direct_second["gap_id"]
            and direct_second["gap_existing"] is True
            and bytes_before_duplicate == bytes_after_duplicate,
            "exact proposal identity deterministically deduplicates without a write",
        )

        restarted = CapabilityGapRegistry(
            state_store=context.store,
            spec_quarantine=context.spec_runtime,
            control_token=CONTROL_TOKEN,
            now=context.clock,
        )
        after_restart = restarted.get(
            gap_id=direct_first["gap_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            after_restart["gap_id"] == direct_first["gap_id"]
            and after_restart["gap_revision"] == 1,
            "capability gap survives registry restart",
        )
        for field, value in (("user_id", "other-user"), ("session_id", "other-session")):
            query = {
                "gap_id": direct_first["gap_id"],
                "user_id": USER,
                "workspace_id": WORKSPACE,
                "session_id": SESSION,
                "control_token": CONTROL_TOKEN,
            }
            query[field] = value
            try:
                restarted.get(**query)
            except CapabilityGapNotFoundError:
                pass
            else:
                raise AssertionError(f"cross-scope {field} read was accepted")
            expect(True, f"cross-scope {field} read is indistinguishable from 404")

        counting_gate = CountingSpecGate(context.spec_runtime)
        context.registry.spec_quarantine = counting_gate  # type: ignore[assignment]
        before_unrelated = {
            name: context.store.path_for(name).read_bytes()
            for name in (
                "phase6_extension_generation_state.json",
                "task_state.json",
                "core_model_trace.jsonl",
                "tool_call_log.jsonl",
            )
        }
        gap_bytes_before_mismatch = context.store.path_for(STATE_FILE).read_bytes()
        try:
            context.registry.link_spec_candidate(
                gap_id=direct_first["gap_id"],
                candidate_id=context.candidate["candidate_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                expected_gap_revision=1,
                expected_candidate_revision=context.candidate["candidate_revision"],
                expected_spec_digest="f" * 64,
                operation_id="capability-gap-wrong-spec",
                control_token=CONTROL_TOKEN,
            )
        except CapabilityGapConflictError:
            pass
        else:
            raise AssertionError("mismatched ExtensionSpec digest was linked")
        expect(
            context.store.path_for(STATE_FILE).read_bytes()
            == gap_bytes_before_mismatch
            and counting_gate.calls == 1,
            "mismatched ExtensionSpec projection fails before durable linkage",
        )
        linked = link_gap(context, direct_first)
        after_unrelated = {
            name: context.store.path_for(name).read_bytes()
            for name in before_unrelated
        }
        expect(
            linked["stage"] == "SPEC_LINKED_REVIEW_REQUIRED"
            and linked["required_action"] == "HUMAN_REVIEW_REQUIRED"
            and linked["gap_revision"] == 2
            and linked["spec_link"]["candidate_stage"] == "SPEC_GATE_PASSED"
            and counting_gate.calls == 2,
            "explicit link binds the exact passed ExtensionSpec projection",
            linked,
        )
        expect(
            before_unrelated == after_unrelated,
            "spec linkage invokes no model Agent tool or generation path",
        )
        replay = link_gap(context, direct_first)
        expect(
            replay["operation_replayed"] is True
            and replay["gap_revision"] == 2
            and counting_gate.calls == 2,
            "exact spec-link replay is state-only and idempotent",
        )
        try:
            context.registry.link_spec_candidate(
                gap_id=direct_first["gap_id"],
                candidate_id=context.candidate["candidate_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                expected_gap_revision=1,
                expected_candidate_revision=context.candidate["candidate_revision"],
                expected_spec_digest="f" * 64,
                operation_id="capability-gap-link-spec",
                control_token=CONTROL_TOKEN,
            )
        except CapabilityGapConflictError:
            pass
        else:
            raise AssertionError("semantic operation replay conflict was accepted")
        expect(True, "operation replay with another digest fails closed")

        generation = observe(
            context,
            linked,
            generation_receipt(context),
            "observe-generation",
        )
        validation = observe(
            context,
            generation,
            validation_receipt(context),
            "observe-validation",
        )
        release = observe(
            context,
            validation,
            release_receipt(context),
            "observe-release",
        )
        deployment = observe(
            context,
            release,
            deployment_receipt(context),
            "observe-deployment",
        )
        expect(
            deployment["gap_revision"] == 6
            and deployment["stage"] == "SPEC_LINKED_REVIEW_REQUIRED"
            and deployment["automatic_advancement"] is False
            and deployment["observations"]["generation"]["receipt"]
            ["generation_status"]
            == "GENERATION_QUARANTINED"
            and deployment["observations"]["validation"]["receipt"]
            ["validation_status"]
            == "DYNAMIC_VALIDATION_PASSED"
            and deployment["observations"]["release"]["receipt"]
            ["release_status"]
            == "RELEASE_SIGNED"
            and deployment["observations"]["deployment"]["receipt"]
            ["deployment_status"]
            == "SHADOW_OBSERVED",
            "explicit source-free receipts project the full lifecycle without authority",
            deployment,
        )
        expect(
            before_unrelated
            == {
                name: context.store.path_for(name).read_bytes()
                for name in before_unrelated
            },
            "receipt observation invokes no model Agent tool or generation path",
        )

        before_gets = content_snapshot(context.root)
        status = context.registry.status(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        listing = context.registry.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        detail = context.registry.get(
            gap_id=direct_first["gap_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        timeline = context.registry.timeline(
            gap_id=direct_first["gap_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        after_gets = content_snapshot(context.root)
        expect(
            status["read_side_effects"] == "none"
            and listing["count"] >= 2
            and detail["gap_revision"] == 6
            and len(timeline["events"]) == 6
            and before_gets == after_gets
            and counting_gate.calls == 2,
            "status list detail and timeline GET paths are byte-pure",
        )

        broken = SelfImprovementProposalRegistry(context.store)
        broken.capability_gaps = BrokenGapRegistry()  # type: ignore[assignment]
        fail_open = broken.propose_from_intent(
            intent(), reason="unknown_proactive_intent"
        )
        fail_open_text = json.dumps(fail_open, ensure_ascii=False)
        expect(
            fail_open["proposal_id"].startswith("sip_")
            and fail_open["capability_gap_linkage"]["status"] == "unavailable"
            and fail_open["capability_gap_linkage"]["authority_granted"] is False
            and "private-storage-detail-must-not-leak" not in fail_open_text,
            "proposal creation remains fail-open with sanitized gap failure",
        )

        context.store.path_for(STATE_FILE).write_text("{broken", encoding="utf-8")
        try:
            context.registry.status(
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=CONTROL_TOKEN,
            )
        except CapabilityGapStorageError:
            pass
        else:
            raise AssertionError("corrupt registry state was accepted")
        expect(True, "corrupt capability-gap state fails closed")
    finally:
        context.close()

    quarantined = prepare_context(spec_gate_passed=False)
    try:
        gap = record_gap(quarantined)
        linked = link_gap(quarantined, gap, operation_id="link-quarantined-spec")
        expect(
            linked["spec_link"]["candidate_stage"] == "SPEC_QUARANTINED"
            and linked["stage"] == "SPEC_LINKED_REVIEW_REQUIRED",
            "explicit linkage may observe a still-quarantined spec but still requires review",
        )
    finally:
        quarantined.close()

    print("Phase 6 capability-gap lifecycle smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
