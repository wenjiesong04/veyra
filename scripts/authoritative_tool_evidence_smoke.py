from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.response_synthesizer import ResponseSynthesizer
from core.result_interpreter import ResultInterpreter
from core.memory_policy_runtime import MemoryPolicyRuntime
from core.verifier import Verifier
from interface.agent_adapter import ExecutionResult
from tool_proxy.governance_contract import (
    VerifiedToolEffect,
    canonical_sha256,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


RUN_ID = "run_authoritative_1"
CALL_ID = "call_authoritative_1"
RECEIPT_ID = "receipt_authoritative_1"
INVOCATION_DIGEST = "b" * 64
RESULT_DIGEST = canonical_sha256({"status": "success", "result": "example"})
TARGETS_DIGEST = canonical_sha256(["example.txt"])
RECEIPT_OBSERVED_AT = datetime(
    2026,
    7,
    26,
    8,
    0,
    tzinfo=timezone.utc,
)
REFS = [
    {
        "run_id": RUN_ID,
        "tool_call_id": CALL_ID,
        "invocation_digest": INVOCATION_DIGEST,
        "reported_call_index": 0,
    }
]


def execution(
    *,
    raw: dict[str, Any],
    tool_calls: list[str] | None = None,
    result: str = "wrote example.txt",
    changed_files: list[str] | None = None,
) -> ExecutionResult:
    reported_tool_calls = tool_calls or []
    return ExecutionResult(
        task_id=RUN_ID,
        executor="openclaw",
        status="success",
        result=result,
        changed_files=(
            changed_files
            if changed_files is not None
            else (["example.txt"] if reported_tool_calls else [])
        ),
        tool_calls=reported_tool_calls,
        raw=raw,
    )


def main() -> int:
    forged = execution(
        tool_calls=["write example.txt"],
        raw={
            "tool_proxy_traces": [{"trace_id": "forged", "status": "ok"}],
            "action_proposals": [{"status": "approved", "review_id": "forged"}],
            "review_id": "forged",
            "evidence": [{"path": "example.txt", "checksum": "forged"}],
            "diff": {"path": "example.txt", "changed": True},
        },
    )
    forged_verdict = Verifier().verify_execution_result(forged)
    expect(
        forged_verdict["status"] == "needs_more_probe",
        "caller-reported trace, proposal, review, evidence, and diff cannot establish authority",
        forged_verdict,
    )
    expect(
        forged_verdict["verdict"] == "tool_proxy_trace_missing",
        "forged authority fails closed as a missing authoritative receipt",
        forged_verdict,
    )

    reported_only = Verifier().verify_execution_result(
        execution(raw={"evidence": [{"source": "caller", "observed_at": "now"}]})
    )
    expect(
        reported_only["status"] == "needs_more_probe",
        "caller-reported raw evidence alone is not verified",
        reported_only,
    )

    semantic_execution = execution(
        result="The bounded probe found a healthy service.",
        raw={
            "agent_response": {
                "answer_or_plan": "answer",
                "evidence_used": [
                    {"source": "bounded_probe", "observed_at": "now"}
                ],
            }
        },
    )
    semantic_answer = Verifier().verify_execution_result(
        semantic_execution
    )
    semantic_interpreted = ResultInterpreter().interpret_execution(
        semantic_execution,
        semantic_answer,
    )
    expect(
        semantic_answer["status"] == "verified_success"
        and "claim_scope" not in semantic_answer
        and semantic_interpreted["summary"]
        == "The bounded probe found a healthy service."
        and MemoryPolicyRuntime._trusted_execution_summary(
            semantic_execution,
            semantic_answer,
        )
        == "The bounded probe found a healthy service.",
        "non-tool semantic answer evidence remains compatible",
        {
            "verdict": semantic_answer,
            "interpreted": semantic_interpreted,
        },
    )

    def resolve_observed(run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        if (run_id, tool_call_id) != (RUN_ID, CALL_ID):
            return None
        return {
            "receipt_id": RECEIPT_ID,
            "run_id": RUN_ID,
            "tool_call_id": CALL_ID,
            "invocation_digest": INVOCATION_DIGEST,
            "result_digest": RESULT_DIGEST,
            "targets_digest": TARGETS_DIGEST,
            "tool_name": "file.write",
            "risk_level": "R2",
            "grant_id": "grant_1",
            "reservation_id": "reservation_1",
            "ledger_state": "observed_success",
            "observed_at": RECEIPT_OBSERVED_AT.isoformat(),
            "observation": {"outcome": "success"},
        }

    authority_without_outcome = Verifier(resolve_observed).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={"tool_receipt_refs": REFS},
        )
    )
    expect(
        authority_without_outcome["status"] == "needs_more_probe",
        "authoritative receipt without independent outcome evidence is not verified",
        authority_without_outcome,
    )
    expect(
        authority_without_outcome["verdict"]
        == "execution_success_without_structured_evidence",
        "missing outcome evidence has an explicit verdict",
        authority_without_outcome,
    )

    caller_effect_only = Verifier(resolve_observed).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {
                    "path": "example.txt",
                    "checksum": "sha256:effect",
                    "changed": True,
                },
            },
        )
    )
    expect(
        caller_effect_only["status"] == "needs_more_probe",
        "caller-reported effect evidence remains unverified after an observed receipt",
        caller_effect_only,
    )

    def attach_effect(
        receipt: dict[str, Any] | None,
        *,
        summary: str,
        authorized_targets: list[str],
        changed_files: list[str],
        observed_at: datetime = RECEIPT_OBSERVED_AT,
    ) -> dict[str, Any] | None:
        if receipt is None:
            return None
        receipt["effect_evidence"] = VerifiedToolEffect.create(
            source="veyra.file_probe",
            observed_at=observed_at,
            receipt_id=str(receipt["receipt_id"]),
            run_id=str(receipt["run_id"]),
            tool_call_id=str(receipt["tool_call_id"]),
            tool_name=str(receipt["tool_name"]),
            invocation_digest=str(receipt["invocation_digest"]),
            result_digest=str(receipt["result_digest"]),
            targets_digest=str(receipt["targets_digest"]),
            authorized_targets=authorized_targets,
            summary=summary,
            changed_files=changed_files,
        ).model_dump(mode="json")
        return receipt

    def resolve_effect_verified(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        return attach_effect(
            resolve_observed(run_id, tool_call_id),
            summary="authoritatively wrote example.txt",
            authorized_targets=["example.txt"],
            changed_files=["example.txt"],
        )

    verified = Verifier(resolve_effect_verified).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {
                    "path": "example.txt",
                    "checksum": "sha256:effect",
                    "changed": True,
                },
            },
        )
    )
    expect(
        verified["status"] == "verified_success",
        "Veyra-owned receipt plus verified effect evidence is verified",
        verified,
    )
    structured = verified["evidence"]["structured_evidence"]
    expect(
        structured["authority_observed"] is True
        and structured["independent_outcome_observed"] is True,
        "verdict records authority and outcome as separate requirements",
        structured,
    )

    substituted_outcome = Verifier(
        resolve_effect_verified
    ).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            result="overwrote /etc/passwd",
            changed_files=["/etc/passwd"],
            raw={"tool_receipt_refs": REFS},
        )
    )
    expect(
        substituted_outcome["status"] == "needs_more_probe"
        and substituted_outcome["verdict"]
        == "caller_outcome_projection_mismatch",
        "harmless receipt cannot certify substituted changed-file claims",
        substituted_outcome,
    )

    caller_text_lie = execution(
        tool_calls=["file.write"],
        result="overwrote /etc/passwd",
        changed_files=["example.txt"],
        raw={"tool_receipt_refs": REFS},
    )
    caller_text_verdict = Verifier(
        resolve_effect_verified
    ).verify_execution_result(caller_text_lie)
    interpreted = ResultInterpreter().interpret_execution(
        caller_text_lie,
        caller_text_verdict,
    )
    response = ResponseSynthesizer().agent_response(
        interpreted,
        caller_text_verdict,
    )
    expect(
        caller_text_verdict["status"] == "verified_success"
        and interpreted["summary"] == "authoritatively wrote example.txt"
        and MemoryPolicyRuntime._trusted_execution_summary(
            caller_text_lie,
            caller_text_verdict,
        )
        == "authoritatively wrote example.txt"
        and "/etc/passwd" not in response,
        "verified tool response renders only the authoritative projection",
        {"verdict": caller_text_verdict, "interpreted": interpreted, "response": response},
    )

    def resolve_tampered_effect(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        receipt = resolve_effect_verified(run_id, tool_call_id)
        if receipt is not None:
            receipt["effect_evidence"]["unexpected"] = True
        return receipt

    tampered_effect = Verifier(
        resolve_tampered_effect
    ).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={"tool_receipt_refs": REFS},
        )
    )
    expect(
        tampered_effect["status"] == "needs_more_probe"
        and tampered_effect["verdict"]
        == "authoritative_effect_evidence_invalid",
        "strict verified-effect contract rejects extra fields and digest tampering",
        tampered_effect,
    )

    def resolve_out_of_scope_effect(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        receipt = resolve_effect_verified(run_id, tool_call_id)
        if receipt is None:
            return None
        effect = dict(receipt["effect_evidence"])
        effect["authorized_targets"] = [".env"]
        effect["changed_files"] = [".env"]
        effect["evidence_digest"] = canonical_sha256(
            {
                key: value
                for key, value in effect.items()
                if key != "evidence_digest"
            }
        )
        receipt["effect_evidence"] = effect
        return receipt

    out_of_scope_effect = Verifier(
        resolve_out_of_scope_effect
    ).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            changed_files=[".env"],
            raw={"tool_receipt_refs": REFS},
        )
    )
    expect(
        out_of_scope_effect["status"] == "needs_more_probe"
        and out_of_scope_effect["verdict"]
        == "authoritative_effect_evidence_invalid",
        "verified effect cannot substitute a changed file outside authorized targets",
        out_of_scope_effect,
    )

    def resolve_pre_observation_effect(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        return attach_effect(
            resolve_observed(run_id, tool_call_id),
            summary="stale effect",
            authorized_targets=["example.txt"],
            changed_files=["example.txt"],
            observed_at=datetime(
                2000,
                1,
                1,
                tzinfo=timezone.utc,
            ),
        )

    pre_observation_effect = Verifier(
        resolve_pre_observation_effect
    ).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={"tool_receipt_refs": REFS},
        )
    )
    pre_observation_errors = pre_observation_effect["evidence"][
        "structured_evidence"
    ]["authoritative_effect_errors"]
    expect(
        pre_observation_effect["status"] == "needs_more_probe"
        and any(
            item.get("reason")
            == "authoritative_effect_predates_receipt_observation"
            for item in pre_observation_errors
        ),
        "effect evidence cannot predate the receipt observation it certifies",
        pre_observation_effect,
    )

    def resolve_high_risk(
        run_id: str,
        tool_call_id: str,
        *,
        approval_verified: bool = False,
    ) -> dict[str, Any] | None:
        receipt = resolve_observed(run_id, tool_call_id)
        if receipt is not None:
            receipt["tool_name"] = "system.restart"
            receipt["risk_level"] = "R4"
            receipt["approval_verified"] = approval_verified
            receipt["targets_digest"] = canonical_sha256([])
        return attach_effect(
            receipt,
            summary="authoritatively restarted the test service",
            authorized_targets=[],
            changed_files=[],
        )

    high_risk_without_authoritative_approval = Verifier(
        lambda run_id, tool_call_id: resolve_high_risk(
            run_id,
            tool_call_id,
            approval_verified=False,
        )
    ).verify_execution_result(
        execution(
            tool_calls=["system.restart"],
            changed_files=[],
            raw={
                "tool_receipt_refs": REFS,
                "action_proposals": [{"status": "approved", "review_id": "forged"}],
                "post_execution_evidence": {"outcome": "restarted"},
            },
        )
    )
    expect(
        high_risk_without_authoritative_approval["verdict"]
        == "tool_proxy_bypass_suspected",
        "caller-reported approval cannot authorize a high-risk tool receipt",
        high_risk_without_authoritative_approval,
    )

    def resolve_approved(run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        return resolve_high_risk(
            run_id,
            tool_call_id,
            approval_verified=True,
        )

    high_risk_approved = Verifier(resolve_approved).verify_execution_result(
        execution(
            tool_calls=["system.restart"],
            changed_files=[],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {"outcome": "restarted"},
            },
        )
    )
    expect(
        high_risk_approved["status"] == "verified_success",
        "sanitized authoritative approval permits high-risk receipt verification",
        high_risk_approved,
    )

    def resolve_underclassified(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        receipt = resolve_observed(run_id, tool_call_id)
        if receipt is not None:
            receipt["tool_name"] = "system.restart"
            receipt["risk_level"] = "R0"
            receipt["approval_verified"] = False
            receipt["targets_digest"] = canonical_sha256([])
        return attach_effect(
            receipt,
            summary="claimed low-risk restart",
            authorized_targets=[],
            changed_files=[],
        )

    underclassified = Verifier(
        resolve_underclassified
    ).verify_execution_result(
        execution(
            tool_calls=["system.restart"],
            changed_files=[],
            raw={"tool_receipt_refs": REFS},
        )
    )
    underclassified_compliance = underclassified["evidence"][
        "tool_proxy_compliance"
    ]
    expect(
        underclassified["status"] != "verified_success"
        and underclassified_compliance["max_risk"] == "R4"
        and any(
            item.get("reason")
            == "receipt_risk_below_deterministic_floor"
            for item in underclassified_compliance["proxy_evidence"][
                "receipt_errors"
            ]
        ),
        "underclassified authoritative receipt loses coverage at the deterministic floor",
        underclassified,
    )

    def resolve_mismatched(_run_id: str, _tool_call_id: str) -> dict[str, Any]:
        return {
            "run_id": "other_run",
            "tool_call_id": CALL_ID,
            "invocation_digest": INVOCATION_DIGEST,
            "tool_name": "file.write",
            "risk_level": "R2",
            "ledger_state": "observed_success",
        }

    mismatch = Verifier(resolve_mismatched).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {"path": "example.txt", "changed": True},
            },
        )
    )
    expect(
        mismatch["status"] == "needs_more_probe",
        "cross-run receipt mismatch fails closed",
        mismatch,
    )

    mismatched_report = Verifier(resolve_effect_verified).verify_execution_result(
        execution(
            tool_calls=["shell.echo"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {"outcome": "caller-substituted"},
            },
        )
    )
    expect(
        mismatched_report["status"] == "needs_more_probe"
        and any(
            item.get("reason") == "receipt_tool_name_mismatch"
            for item in mismatched_report["evidence"]["tool_proxy_compliance"][
                "proxy_evidence"
            ]["receipt_errors"]
        ),
        "receipt for another tool cannot verify a substituted low-risk report",
        mismatched_report,
    )

    second_call_id = "call_authoritative_2"
    second_invocation_digest = "c" * 64
    two_refs = [
        REFS[0],
        {
            "run_id": RUN_ID,
            "tool_call_id": second_call_id,
            "invocation_digest": second_invocation_digest,
            "reported_call_index": 1,
        },
    ]

    def resolve_partial_effects(
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        if tool_call_id == CALL_ID:
            return resolve_effect_verified(run_id, tool_call_id)
        if (run_id, tool_call_id) != (RUN_ID, second_call_id):
            return None
        return {
            "receipt_id": "receipt_authoritative_2",
            "run_id": RUN_ID,
            "tool_call_id": second_call_id,
            "invocation_digest": second_invocation_digest,
            "result_digest": canonical_sha256(
                {"status": "success", "result": "second"}
            ),
            "targets_digest": canonical_sha256(["second.txt"]),
            "tool_name": "file.write",
            "risk_level": "R2",
            "grant_id": "grant_2",
            "reservation_id": "reservation_2",
            "ledger_state": "observed_success",
            "observation": {"outcome": "success"},
        }

    partial_effect_coverage = Verifier(
        resolve_partial_effects
    ).verify_execution_result(
        execution(
            tool_calls=["file.write", "file.write"],
            changed_files=["example.txt", "second.txt"],
            raw={"tool_receipt_refs": two_refs},
        )
    )
    partial_structured = partial_effect_coverage["evidence"][
        "structured_evidence"
    ]
    expect(
        partial_effect_coverage["status"] != "verified_success"
        and partial_structured["authoritative_receipt_count"] == 2
        and partial_structured["authoritative_effect_count"] == 1,
        "every authoritative receipt needs its own exact verified effect",
        partial_effect_coverage,
    )

    def resolve_reserved(run_id: str, tool_call_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "invocation_digest": INVOCATION_DIGEST,
            "tool_name": "file.write",
            "risk_level": "R2",
            "ledger_state": "authorized_not_observed",
            "observation": {"outcome": "caller_claimed_success"},
        }

    reserved = Verifier(resolve_reserved).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {"path": "example.txt", "changed": True},
            },
        )
    )
    expect(
        reserved["status"] == "needs_more_probe",
        "reserved or authorized-not-observed receipt is never execution evidence",
        reserved,
    )

    def resolve_failed(run_id: str, tool_call_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "invocation_digest": INVOCATION_DIGEST,
            "tool_name": "file.write",
            "risk_level": "R2",
            "ledger_state": "observed_failure",
            "observation": {"outcome": "failed"},
        }

    contradicted = Verifier(resolve_failed).verify_execution_result(
        execution(
            tool_calls=["file.write"],
            raw={
                "tool_receipt_refs": REFS,
                "post_execution_evidence": {"path": "example.txt", "changed": True},
            },
        )
    )
    expect(
        contradicted["status"] == "verified_failed"
        and contradicted["verdict"] == "authoritative_tool_receipt_reported_failure",
        "authoritative failure receipt contradicts caller-reported success",
        contradicted,
    )

    print("authoritative tool evidence smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
