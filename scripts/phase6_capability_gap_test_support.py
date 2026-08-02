from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.proactive_intent import ProactiveIntent
from core.world_state import WorldStateStore
from interface.extension_spec import parse_extension_spec
from runtime.capability_gap_registry import CapabilityGapRegistry
from runtime.extension_spec_quarantine import ExtensionSpecQuarantine
from scripts.phase6_extension_spec_contract_smoke import valid_spec


BASE_TIME = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
CONTROL_TOKEN = "phase6-capability-gap-control-token"
USER = "capability-gap-user"
SESSION = "capability-gap-session"
WORKSPACE = "/private/veyra/capability-gap-workspace"
RAW_TEXT = "请持续追踪秘密项目，不要把这段原文公开"


class Clock:
    def __init__(self, current: datetime = BASE_TIME) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@dataclass(slots=True)
class CapabilityGapContext:
    temporary: TemporaryDirectory[str]
    root: Path
    store: WorldStateStore
    clock: Clock
    spec_runtime: ExtensionSpecQuarantine
    registry: CapabilityGapRegistry
    candidate: dict[str, Any]
    spec_digest: str

    def close(self) -> None:
        self.temporary.cleanup()


def prepare_context(
    *, spec_gate_passed: bool = True
) -> CapabilityGapContext:
    temporary = TemporaryDirectory(prefix="veyra-capability-gap-")
    root = Path(temporary.name)
    store = WorldStateStore(root)
    store.mutate_json(
        "local_world.json",
        lambda state: {**state, "current_project": WORKSPACE},
    )
    clock = Clock()
    spec_runtime = ExtensionSpecQuarantine(state_store=store, now=clock)
    payload = valid_spec(now=BASE_TIME)
    payload["extension_id"] = "example.capability_gap_projection"
    payload["dependencies"] = []
    spec = parse_extension_spec(payload)
    candidate = spec_runtime.quarantine(
        spec=spec,
        expected_spec_digest=spec.digest(),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id="capability-gap-spec-submit",
    )
    if spec_gate_passed:
        candidate = spec_runtime.review(
            candidate_id=candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=1,
            operation_id="capability-gap-spec-review",
            decision="accept_for_future_isolated_generation",
            reason="The bridge may observe this candidate but cannot generate it.",
        )
    registry = CapabilityGapRegistry(
        state_store=store,
        spec_quarantine=spec_runtime,
        control_token=CONTROL_TOKEN,
        now=clock,
    )
    return CapabilityGapContext(
        temporary=temporary,
        root=root,
        store=store,
        clock=clock,
        spec_runtime=spec_runtime,
        registry=registry,
        candidate=candidate,
        spec_digest=spec.digest(),
    )


def intent() -> ProactiveIntent:
    return ProactiveIntent(
        intent_id="pin_capabilitygap01",
        user_id=USER,
        session_id=SESSION,
        channel_id="api",
        raw_text=RAW_TEXT,
        intent_type="unknown",
        topic="秘密项目",
        desired_outcome="持续追踪",
        source="semantic_model",
    )


def proposal_payload() -> dict[str, Any]:
    return {
        "proposal_id": "sip_capabilitygap01",
        "source": "veyra_runtime",
    }


def record_gap(context: CapabilityGapContext) -> dict[str, Any]:
    return context.registry.record_from_proposal(
        proposal=proposal_payload(),
        intent=intent().to_dict(),
        reason_code="unknown_proactive_intent",
    )


def link_gap(
    context: CapabilityGapContext,
    gap: dict[str, Any],
    *,
    operation_id: str = "capability-gap-link-spec",
) -> dict[str, Any]:
    return context.registry.link_spec_candidate(
        gap_id=gap["gap_id"],
        candidate_id=context.candidate["candidate_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_gap_revision=gap["gap_revision"],
        expected_candidate_revision=context.candidate["candidate_revision"],
        expected_spec_digest=context.spec_digest,
        operation_id=operation_id,
        control_token=CONTROL_TOKEN,
    )


def generation_receipt(context: CapabilityGapContext) -> dict[str, Any]:
    return {
        "schema_version": (
            "veyra.phase6.capability_gap_generation_receipt.v1"
        ),
        "receipt_kind": "generation",
        "candidate_id": context.candidate["candidate_id"],
        "candidate_revision": context.candidate["candidate_revision"],
        "spec_digest": context.spec_digest,
        "receipt_source": "explicit_local_control_plane_projection",
        "authority_granted": False,
        "generation_id": "extgen_" + "1" * 24,
        "generation_status": "GENERATION_QUARANTINED",
        "generation_report_digest": "2" * 64,
    }


def validation_receipt(context: CapabilityGapContext) -> dict[str, Any]:
    generation = generation_receipt(context)
    return {
        "schema_version": (
            "veyra.phase6.capability_gap_validation_receipt.v1"
        ),
        "receipt_kind": "validation",
        "candidate_id": context.candidate["candidate_id"],
        "candidate_revision": context.candidate["candidate_revision"],
        "spec_digest": context.spec_digest,
        "receipt_source": "explicit_local_control_plane_projection",
        "authority_granted": False,
        "generation_id": generation["generation_id"],
        "validation_id": "extval_" + "3" * 24,
        "artifact_id": "extart_" + "4" * 24,
        "artifact_sha256": "5" * 64,
        "validation_status": "DYNAMIC_VALIDATION_PASSED",
        "validation_report_digest": "6" * 64,
    }


def release_receipt(context: CapabilityGapContext) -> dict[str, Any]:
    validation = validation_receipt(context)
    return {
        "schema_version": "veyra.phase6.capability_gap_release_receipt.v1",
        "receipt_kind": "release",
        "candidate_id": context.candidate["candidate_id"],
        "candidate_revision": context.candidate["candidate_revision"],
        "spec_digest": context.spec_digest,
        "receipt_source": "explicit_local_control_plane_projection",
        "authority_granted": False,
        "generation_id": validation["generation_id"],
        "validation_id": validation["validation_id"],
        "release_id": "extrel_" + "7" * 24,
        "release_status": "RELEASE_SIGNED",
        "release_attestation_digest": "8" * 64,
    }


def deployment_receipt(context: CapabilityGapContext) -> dict[str, Any]:
    release = release_receipt(context)
    return {
        "schema_version": (
            "veyra.phase6.capability_gap_deployment_receipt.v1"
        ),
        "receipt_kind": "deployment",
        "candidate_id": context.candidate["candidate_id"],
        "candidate_revision": context.candidate["candidate_revision"],
        "spec_digest": context.spec_digest,
        "receipt_source": "explicit_local_control_plane_projection",
        "authority_granted": False,
        "release_id": release["release_id"],
        "deployment_id": "extdep_" + "9" * 24,
        "deployment_status": "SHADOW_OBSERVED",
        "deployment_receipt_digest": "a" * 64,
        "output_used_for_policy": False,
    }


__all__ = [
    "BASE_TIME",
    "CONTROL_TOKEN",
    "RAW_TEXT",
    "SESSION",
    "USER",
    "WORKSPACE",
    "CapabilityGapContext",
    "deployment_receipt",
    "generation_receipt",
    "intent",
    "link_gap",
    "prepare_context",
    "proposal_payload",
    "record_gap",
    "release_receipt",
    "validation_receipt",
]
