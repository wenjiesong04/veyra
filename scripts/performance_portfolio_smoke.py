#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from rollback_audit.execution_trace import ExecutionTrace
from runtime.performance_portfolio import PerformancePortfolio
from tool_proxy.governance_contract import (
    AuthoritativeToolReceipt,
    VerifiedToolEffect,
    canonical_sha256,
)


NOW = datetime(2026, 7, 28, 11, 0, tzinfo=timezone.utc)
SECRET = "SECRET_PORTFOLIO_MUST_NOT_LEAK"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def _receipt(
    *,
    suffix: str,
    ledger_state: str,
    reserved_at: datetime,
    observed_at: datetime | None,
    outcome: str | None,
    receipt_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    reservation_id: str | None = None,
) -> AuthoritativeToolReceipt:
    result_digest = (
        canonical_sha256({"result": suffix, "secret": SECRET})
        if observed_at is not None
        else None
    )
    return AuthoritativeToolReceipt.create(
        receipt_id=receipt_id or f"receipt-{suffix}",
        run_id=run_id or f"run-{suffix}",
        tool_call_id=tool_call_id or f"call-{suffix}",
        grant_id=f"grant-{suffix}",
        reservation_id=reservation_id or f"reservation-{suffix}",
        tool_name="file.write",
        tool_kind="filesystem",
        risk_level="R2",
        args_digest=canonical_sha256({"path": f"{suffix}.txt"}),
        targets_digest=canonical_sha256([]),
        environment_digest=canonical_sha256({}),
        invocation_digest=canonical_sha256(
            {"invocation": suffix}
        ),
        grant_digest=canonical_sha256({"grant": suffix}),
        ledger_state=ledger_state,
        approval_verified=True,
        approval_id=f"approval-{suffix}",
        approval_revision="approval-revision-1",
        policy_revision="policy-revision-1",
        registry_revision="registry-revision-1",
        reserved_at=reserved_at,
        observed_at=observed_at,
        outcome=outcome,
        result_digest=result_digest,
    )


def _effect(
    receipt: AuthoritativeToolReceipt,
) -> VerifiedToolEffect:
    if receipt.observed_at is None or receipt.result_digest is None:
        raise ValueError("effect fixture requires an observed receipt")
    return VerifiedToolEffect.create(
        source="veyra.file_probe",
        observed_at=receipt.observed_at + timedelta(milliseconds=1),
        receipt_id=receipt.receipt_id,
        run_id=receipt.run_id,
        tool_call_id=receipt.tool_call_id,
        tool_name=receipt.tool_name,
        invocation_digest=receipt.invocation_digest,
        result_digest=receipt.result_digest,
        targets_digest=receipt.targets_digest,
        authorized_targets=[],
        summary="verified bounded write",
        changed_files=[],
    )


def _actor(
    portfolio: dict[str, Any],
    actor_id: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in portfolio["actors"]
        if item.get("actor_id") == actor_id
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one actor {actor_id}, got {matches!r}"
        )
    return matches[0]


def _source(
    portfolio: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in portfolio["sources"]
        if item.get("source") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one source {name}, got {matches!r}"
        )
    return matches[0]


def _record_execution(
    store: WorldStateStore,
    *,
    suffix: str,
    route: str,
    executor: str,
    outcome: str,
    observed_at: datetime | None,
    valid_success_contract: bool = True,
) -> dict[str, Any]:
    task_id = f"task-{suffix}"
    if outcome == "success":
        execution_status = "success"
        if valid_success_contract:
            evidence = {
                "task_id": task_id,
                "executor": executor,
                "reported_status": "success",
                "has_result_text": False,
                "logs_present": False,
                "changed_files": [],
                "tool_calls": [],
                "raw_keys": ["post_execution_evidence"],
                "result_text_is_evidence": False,
                "raw_presence_is_evidence": False,
                "tool_proxy_compliance": {"status": "not_applicable"},
                "structured_evidence": {
                    "sufficient": True,
                    "sources": ["raw.post_execution_evidence"],
                    "reported_sources": [],
                    "reported_outcome_sources": [],
                    "authoritative_effect_sources": [],
                    "authoritative_effect_count": 0,
                    "authoritative_effect_missing_count": 0,
                    "authoritative_effect_errors": [],
                    "authoritative_effect_evidence_invalid": False,
                    "authoritative_projection": {},
                    "caller_projection_mismatch": False,
                    "caller_result_authenticated": False,
                    "authority_observed": False,
                    "authoritative_failure_observed": False,
                    "authoritative_receipt_count": 0,
                    "independent_outcome_observed": True,
                    "agent_plan_only": False,
                    "dialogue_proposal_only": False,
                    "result_text_only": False,
                    "raw_metadata_only": False,
                },
            }
            verification = {
                "status": "verified_success",
                "verdict": (
                    "execution_success_supported_by_structured_evidence"
                ),
                "confidence": 0.8,
                "evidence": evidence,
                "next_action": "update_state_and_memory",
                "needs_rollback": False,
                "needs_memory_patch": True,
            }
        else:
            verification = {
                "status": "verified_success",
                "verdict": (
                    "execution_success_supported_by_structured_evidence"
                ),
                "evidence": {"anything": "nonempty"},
                "needs_rollback": False,
                "needs_memory_patch": True,
            }
        trace_status = "verified_success"
    elif outcome == "failure":
        execution_status = "failed"
        trace_status = "verified_failed"
        verification = {
            "status": "verified_failed",
            "verdict": "fixture_verified_failure",
            "evidence": {
                "task_id": task_id,
                "executor": executor,
                "reported_status": "failed",
            },
            "next_action": "inspect_logs_and_retry",
            "needs_rollback": False,
            "needs_memory_patch": False,
        }
    elif outcome == "pending":
        execution_status = "submitted"
        trace_status = "submitted"
        verification = {
            "status": "needs_more_probe",
            "verdict": "execution_submitted_but_not_final",
            "evidence": {},
            "needs_rollback": False,
            "needs_memory_patch": False,
        }
    else:
        raise ValueError(f"unsupported execution outcome: {outcome}")
    recorder = ExecutionTrace(
        store if observed_at is not None else None,
        clock=lambda: (
            observed_at.isoformat()
            if observed_at is not None
            else ""
        ),
    )
    trace = recorder.record(
        {
            "trace_id": f"execution-{suffix}",
            "route": route,
            "task_id": task_id,
            "executor": executor,
            "status": trace_status,
            "execution_result": {
                "task_id": task_id,
                "executor": executor,
                "status": execution_status,
            },
            "verification": verification,
            "artifacts": {"secret": SECRET},
        }
    )
    if observed_at is None:
        trace["verifier_observation"] = (
            ExecutionTrace._verifier_observation(trace)
        )
        trace["timestamp"] = None
        store.append_jsonl("execution_trace.jsonl", trace)
    return trace


def _seed_decisions(store: WorldStateStore) -> None:
    rows = [
        {
            "trace_id": "route-success",
            "final_route": "direct",
            "outcome_category": "success",
            "probe_used": ["system_probe"],
            "latency_ms": 10,
            "completed_at": (NOW - timedelta(seconds=30)).isoformat(),
            "message_preview": SECRET,
        },
        {
            "trace_id": "route-unknown",
            "final_route": "direct",
            "outcome_category": "expected_governance",
            "probe_used": ["system_probe"],
            "latency_ms": 30,
            "completed_at": (NOW - timedelta(seconds=20)).isoformat(),
        },
        {
            "trace_id": "route-failure",
            "final_route": "agent",
            "outcome_category": "runtime_failure",
            "probe_used": [],
            "latency_ms": 20,
            "completed_at": (NOW - timedelta(seconds=10)).isoformat(),
        },
    ]
    for row in rows:
        store.append_jsonl("decision_trace.jsonl", row)


def _seed_models(store: WorldStateStore) -> None:
    for index, (status, duration) in enumerate(
        (
            ("model_assisted", 100),
            ("unconfigured", 200),
            ("http_error", 300),
        ),
        start=1,
    ):
        store.append_jsonl(
            "core_model_trace.jsonl",
            {
                "purpose": f"portfolio-{index}",
                "status": status,
                "duration_ms": duration,
                "timestamp": (
                    NOW - timedelta(seconds=10 - index)
                ).isoformat(),
                "result": {
                    "status": status,
                    "duration_ms": duration,
                    "_model": {
                        "provider": "openai_compatible",
                        "model": "kimi-shadow",
                    },
                    "raw_text": SECRET,
                },
            },
        )


def _seed_executions(store: WorldStateStore) -> None:
    success_trace = _record_execution(
        store,
        suffix="success",
        route="agent",
        executor="openclaw",
        outcome="success",
        observed_at=NOW - timedelta(seconds=5),
    )
    _record_execution(
        store,
        suffix="failure",
        route="agent",
        executor="openclaw",
        outcome="failure",
        observed_at=NOW - timedelta(seconds=4),
    )
    _record_execution(
        store,
        suffix="pending",
        route="agent",
        executor="openclaw",
        outcome="pending",
        observed_at=NOW - timedelta(seconds=3),
    )
    persisted = store.read_jsonl(
        "execution_trace.jsonl",
        limit=10,
    )
    expect(
        "verifier_observation" not in success_trace
        and len(persisted) == 3
        and all(
            isinstance(item.get("verifier_observation"), dict)
            for item in persisted
        ),
        "verifier binding is durable but excluded from public execution artifacts",
    )


def _seed_tools(store: WorldStateStore) -> None:
    success = _receipt(
        suffix="success",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=20),
        observed_at=NOW - timedelta(seconds=19, milliseconds=950),
        outcome="success",
    )
    failure = _receipt(
        suffix="failure",
        ledger_state="observed_failure",
        reserved_at=NOW - timedelta(seconds=18),
        observed_at=NOW - timedelta(seconds=17, milliseconds=930),
        outcome="failure",
    )
    pending_effect = _receipt(
        suffix="effect-pending",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=16),
        observed_at=NOW - timedelta(seconds=15, milliseconds=910),
        outcome="success",
    )
    effect = VerifiedToolEffect.create(
        source="veyra.file_probe",
        observed_at=NOW - timedelta(seconds=19, milliseconds=900),
        receipt_id=success.receipt_id,
        run_id=success.run_id,
        tool_call_id=success.tool_call_id,
        tool_name=success.tool_name,
        invocation_digest=success.invocation_digest,
        result_digest=str(success.result_digest),
        targets_digest=success.targets_digest,
        authorized_targets=[],
        summary="verified bounded write",
        changed_files=[],
    )

    def update(state: dict[str, Any]) -> None:
        state["schema_version"] = "veyra.tool_governance_state.v1"
        state["calls"] = {
            "reservation-success": {
                "receipt": success.model_dump(mode="json"),
                "effect_evidence": effect.model_dump(mode="json"),
                "private": SECRET,
            },
            "reservation-failure": {
                "receipt": failure.model_dump(mode="json"),
            },
            "reservation-effect-pending": {
                "receipt": pending_effect.model_dump(mode="json"),
            },
        }

    store.mutate_json("tool_governance_state.json", update)


def _seed_runtime_matrix(store: WorldStateStore) -> None:
    def update(state: dict[str, Any]) -> None:
        state.update(
            {
                "status": "degraded",
                "observed_at": NOW.isoformat(),
                "checked_at": NOW.isoformat(),
                "ttl_seconds": 60,
                "runtimes": [
                    {
                        "name": "openclaw",
                        "status": "ready",
                        "validation": {
                            "validated": True,
                            "status": "validated",
                        },
                        "connection": {"secret": SECRET},
                    },
                    {
                        "name": "hermes",
                        "status": "error",
                        "validation": {
                            "validated": False,
                            "status": "validation_pending",
                        },
                    },
                    {
                        "name": "custom",
                        "status": "degraded",
                        "validation": {
                            "validated": False,
                            "status": "validation_pending",
                        },
                    },
                ],
            }
        )

    store.mutate_json("ops_runtime_matrix.json", update)


def _assert_time_evidence_fails_closed(root: Path) -> None:
    store = WorldStateStore(root)
    for row in (
        {
            "final_route": "direct",
            "outcome_category": "success",
            "latency_ms": 10,
            "completed_at": (
                NOW - timedelta(seconds=120)
            ).isoformat(),
        },
        {
            "final_route": "direct",
            "outcome_category": "success",
            "latency_ms": 20,
            "timestamp": None,
        },
        {
            "final_route": "direct",
            "outcome_category": "runtime_failure",
            "latency_ms": 30,
            "completed_at": (
                NOW - timedelta(seconds=1)
            ).isoformat(),
        },
    ):
        store.append_jsonl("decision_trace.jsonl", row)
    for row in (
        {
            "status": "model_assisted",
            "timestamp": (
                NOW - timedelta(seconds=120)
            ).isoformat(),
        },
        {"status": "http_error", "timestamp": None},
    ):
        store.append_jsonl(
            "core_model_trace.jsonl",
            {
                **row,
                "result": {
                    "status": row["status"],
                    "_model": {
                        "provider": "fixture",
                        "model": "time-guard",
                    },
                },
            },
        )
    _record_execution(
        store,
        suffix="stale-success",
        route="agent",
        executor="openclaw",
        outcome="success",
        observed_at=NOW - timedelta(seconds=120),
    )
    _record_execution(
        store,
        suffix="missing-time-failure",
        route="agent",
        executor="openclaw",
        outcome="failure",
        observed_at=None,
    )
    stale_receipt = _receipt(
        suffix="stale",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=121),
        observed_at=NOW - timedelta(seconds=120),
        outcome="success",
    )
    store.mutate_json(
        "tool_governance_state.json",
        lambda state: state.update(
            {
                "schema_version": "veyra.tool_governance_state.v1",
                "calls": {
                    stale_receipt.reservation_id: {
                        "receipt": stale_receipt.model_dump(mode="json"),
                        "effect_evidence": _effect(
                            stale_receipt
                        ).model_dump(mode="json"),
                    }
                },
            }
        ),
    )
    portfolio = PerformancePortfolio(
        state_store=store,
        now_fn=lambda: NOW,
        observation_ttl_seconds=60,
    ).snapshot(limit=100)

    route = _actor(portfolio, "route:direct")
    model = _actor(portfolio, "model:fixture/time-guard")
    agent = _actor(portfolio, "agent:openclaw")
    tool = _actor(portfolio, "tool:file.write")
    expect(
        route["success_count"] == 0
        and route["failure_count"] == 1
        and route["unknown_count"] == 2
        and route["evaluated_count"] == 1
        and model["evaluated_count"] == 0
        and model["unknown_count"] == 2
        and agent["evaluated_count"] == 0
        and agent["unknown_count"] == 2
        and tool["evaluated_count"] == 0
        and tool["unknown_count"] == 1,
        "stale and missing observation times cannot enter evaluated outcomes",
        {
            "route": route,
            "model": model,
            "agent": agent,
            "tool": tool,
        },
    )
    timed_sources = {
        name: _source(portfolio, name)
        for name in (
            "decision_trace.jsonl",
            "core_model_trace.jsonl",
            "execution_trace.jsonl",
        )
    }
    expect(
        all(
            {
                "missing_or_invalid_observed_at",
                "stale_observation",
            }.issubset(set(source["issues"]))
            for source in timed_sources.values()
        )
        and "stale_observation"
        in _source(
            portfolio,
            "tool_governance_state.json",
        )["issues"],
        "time evidence gaps remain visible in source health",
        timed_sources,
    )


def _assert_execution_actor_binding(root: Path) -> None:
    store = WorldStateStore(root)
    rows = (
        ("agent", "openclaw"),
        ("probe", "probe:weather"),
        ("skill", "skill:calendar"),
        ("human_review", "skill:calendar"),
        ("agent", "probe:forged"),
        ("probe", "openclaw"),
        ("skill", "probe:misbound"),
    )
    for index, (route, executor) in enumerate(rows):
        pending_review = route == "human_review"
        _record_execution(
            store,
            suffix=f"actor-{index}",
            route=route,
            executor=executor,
            outcome="pending" if pending_review else "success",
            observed_at=NOW - timedelta(seconds=index + 1),
        )
    portfolio = PerformancePortfolio(
        state_store=store,
        now_fn=lambda: NOW,
        observation_ttl_seconds=60,
    ).snapshot(limit=100)
    actor_ids = {
        item["actor_id"]
        for item in portfolio["actors"]
        if item["metric_scope"] == "persisted_verifier_outcome"
    }
    execution_source = _source(
        portfolio,
        "execution_trace.jsonl",
    )
    skill = _actor(portfolio, "skill:calendar")
    expect(
        actor_ids
        == {
            "agent:openclaw",
            "probe:weather",
            "skill:calendar",
        }
        and skill["success_count"] == 1
        and skill["unknown_count"] == 1
        and execution_source["accepted_count"] == 4
        and execution_source["rejected_count"] == 3
        and "execution_actor_route_mismatch"
        in execution_source["issues"],
        "execution actors require a matching route and executor prefix",
        {
            "actor_ids": actor_ids,
            "source": execution_source,
        },
    )


def _assert_forged_execution_evidence_is_unknown(
    root: Path,
) -> None:
    store = WorldStateStore(root)
    _record_execution(
        store,
        suffix="forged-nonempty-evidence",
        route="agent",
        executor="openclaw",
        outcome="success",
        observed_at=NOW - timedelta(seconds=1),
        valid_success_contract=False,
    )
    portfolio = PerformancePortfolio(
        state_store=store,
        now_fn=lambda: NOW,
        observation_ttl_seconds=60,
    ).snapshot(limit=100)
    actor = _actor(portfolio, "agent:openclaw")
    source = _source(portfolio, "execution_trace.jsonl")
    expect(
        actor["success_count"] == 0
        and actor["failure_count"] == 0
        and actor["unknown_count"] == 1
        and actor["evaluated_count"] == 0
        and source["status"] == "degraded"
        and "invalid_bound_verifier_observation"
        in source["issues"],
        "arbitrary nonempty evidence cannot become verified success",
        {"actor": actor, "source": source},
    )


def _assert_tool_receipt_binding_and_deduplication(
    root: Path,
) -> None:
    store = WorldStateStore(root)
    canonical = _receipt(
        suffix="canonical",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=20),
        observed_at=NOW - timedelta(seconds=19),
        outcome="success",
    )
    duplicate_receipt_id = _receipt(
        suffix="duplicate-receipt",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=18),
        observed_at=NOW - timedelta(seconds=17),
        outcome="success",
        receipt_id=canonical.receipt_id,
    )
    duplicate_call = _receipt(
        suffix="duplicate-call",
        ledger_state="observed_success",
        reserved_at=NOW - timedelta(seconds=16),
        observed_at=NOW - timedelta(seconds=15),
        outcome="success",
        run_id=canonical.run_id,
        tool_call_id=canonical.tool_call_id,
    )
    store.mutate_json(
        "tool_governance_state.json",
        lambda state: state.update(
            {
                "schema_version": "veyra.tool_governance_state.v1",
                "calls": {
                    canonical.reservation_id: {
                        "receipt": canonical.model_dump(mode="json"),
                        "effect_evidence": _effect(
                            canonical
                        ).model_dump(mode="json"),
                    },
                    "reservation-wrong-key": {
                        "receipt": canonical.model_dump(mode="json"),
                        "effect_evidence": _effect(
                            canonical
                        ).model_dump(mode="json"),
                    },
                    duplicate_receipt_id.reservation_id: {
                        "receipt": duplicate_receipt_id.model_dump(
                            mode="json"
                        ),
                        "effect_evidence": _effect(
                            duplicate_receipt_id
                        ).model_dump(mode="json"),
                    },
                    duplicate_call.reservation_id: {
                        "receipt": duplicate_call.model_dump(
                            mode="json"
                        ),
                        "effect_evidence": _effect(
                            duplicate_call
                        ).model_dump(mode="json"),
                    },
                },
            }
        ),
    )
    portfolio = PerformancePortfolio(
        state_store=store,
        now_fn=lambda: NOW,
        observation_ttl_seconds=60,
    ).snapshot(limit=100)
    tool = _actor(portfolio, "tool:file.write")
    source = _source(portfolio, "tool_governance_state.json")
    expect(
        tool["success_count"] == 1
        and tool["total_count"] == 1
        and source["accepted_count"] == 1
        and source["rejected_count"] == 3
        and {
            "receipt_reservation_key_mismatch",
            "duplicate_receipt_id",
            "duplicate_tool_call_identity",
        }.issubset(set(source["issues"])),
        "tool receipts bind to their map key and cannot be counted twice",
        {"tool": tool, "source": source},
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        _seed_decisions(store)
        _seed_models(store)
        _seed_executions(store)
        _seed_tools(store)
        _seed_runtime_matrix(store)
        runtime = PerformancePortfolio(
            state_store=store,
            now_fn=lambda: NOW,
            minimum_support=3,
            observation_ttl_seconds=60,
        )

        tool_state_before = store.read_json(
            "tool_governance_state.json"
        )
        traces_before = store.read_jsonl(
            "decision_trace.jsonl",
            limit=100,
        )
        portfolio = runtime.snapshot(limit=100)
        expect(
            store.read_json("tool_governance_state.json")
            == tool_state_before
            and store.read_jsonl(
                "decision_trace.jsonl",
                limit=100,
            )
            == traces_before,
            "portfolio performs no state or trace writes",
        )

        direct = _actor(portfolio, "route:direct")
        expect(
            direct["success_count"] == 1
            and direct["failure_count"] == 0
            and direct["unknown_count"] == 1
            and direct["evaluated_count"] == 1
            and direct["success_rate"] == 1.0
            and direct["latency_ms"]["p50"] == 10
            and direct["latency_ms"]["p95"] == 30,
            "unknown route outcomes stay outside success denominator while latency remains descriptive",
            direct,
        )
        probe = _actor(portfolio, "probe:system_probe")
        expect(
            probe["metric_scope"]
            == "request_outcome_when_probe_used"
            and probe["success_count"] == 1
            and probe["unknown_count"] == 1,
            "probe portfolio describes request outcome rather than claiming factual correctness",
            probe,
        )

        model = _actor(
            portfolio,
            "model:openai_compatible/kimi-shadow",
        )
        expect(
            model["success_count"] == 1
            and model["failure_count"] == 1
            and model["unknown_count"] == 1
            and model["success_rate"] == 0.5
            and model["latency_ms"]["p50"] == 200
            and model["latency_ms"]["p95"] == 300
            and model["support"]["status"] == "insufficient_data",
            "model portfolio separates transport success, failure, and unconfigured unknown",
            model,
        )

        agent = _actor(portfolio, "agent:openclaw")
        expect(
            agent["success_count"] == 1
            and agent["failure_count"] == 1
            and agent["unknown_count"] == 1
            and agent["success_rate"] == 0.5
            and agent["metric_scope"] == "persisted_verifier_outcome",
            "agent success requires a persisted matching verified result with evidence",
            agent,
        )

        tool = _actor(portfolio, "tool:file.write")
        expect(
            tool["success_count"] == 1
            and tool["failure_count"] == 1
            and tool["unknown_count"] == 1
            and tool["success_rate"] == 0.5
            and tool["latency_ms"]["p50"] == 70
            and tool["latency_ms"]["p95"] == 90
            and tool["metric_scope"] == "authoritative_tool_effect",
            "tool success requires a valid receipt and matching independent effect",
            tool,
        )

        openclaw = _actor(portfolio, "provider:openclaw")
        hermes = _actor(portfolio, "provider:hermes")
        custom = _actor(portfolio, "provider:custom")
        expect(
            openclaw["success_count"] == 1
            and hermes["failure_count"] == 1
            and custom["unknown_count"] == 1,
            "fresh runtime matrix retains ready, error, and degraded distinctions",
            {
                "openclaw": openclaw,
                "hermes": hermes,
                "custom": custom,
            },
        )

        serialized = json.dumps(
            portfolio,
            ensure_ascii=False,
            sort_keys=True,
        )
        authority = portfolio["authority"]
        expect(
            SECRET not in serialized
            and authority["policy_effect"] == "none"
            and authority["route_selection_allowed"] is False
            and authority["provider_selection_allowed"] is False
            and authority["provider_switch_allowed"] is False
            and authority["model_weight_change_allowed"] is False
            and authority["autonomy_change_allowed"] is False
            and authority["capability_grant_allowed"] is False
            and authority["execution_allowed"] is False,
            "portfolio exposes compact metrics without raw payloads or authority",
            authority,
        )

        store.mutate_json(
            "ops_runtime_matrix.json",
            lambda state: state.update(
                {
                    "observed_at": (
                        NOW - timedelta(seconds=61)
                    ).isoformat(),
                    "checked_at": (
                        NOW - timedelta(seconds=61)
                    ).isoformat(),
                }
            ),
        )
        store.path_for("tool_governance_state.json").write_text(
            "{broken",
            encoding="utf-8",
        )
        degraded = runtime.snapshot(limit=100)
        stale_openclaw = _actor(
            degraded,
            "provider:openclaw",
        )
        expect(
            degraded["status"] == "degraded"
            and _source(
                degraded,
                "tool_governance_state.json",
            )["status"]
            == "degraded"
            and stale_openclaw["success_count"] == 0
            and stale_openclaw["unknown_count"] == 1
            and stale_openclaw["freshness"]["status"] == "stale"
            and not any(
                item["actor_id"] == "tool:file.write"
                for item in degraded["actors"]
            ),
            "corrupt authoritative state fails closed and stale runtime evidence becomes unknown",
            degraded,
        )

        _assert_time_evidence_fails_closed(
            Path(tmp) / "time-evidence-state"
        )
        _assert_execution_actor_binding(
            Path(tmp) / "actor-binding-state"
        )
        _assert_forged_execution_evidence_is_unknown(
            Path(tmp) / "forged-execution-evidence-state"
        )
        _assert_tool_receipt_binding_and_deduplication(
            Path(tmp) / "receipt-binding-state"
        )

    print("performance portfolio smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
