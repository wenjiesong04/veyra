from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.world_state import WorldStateStore
from rollback_audit.execution_trace import VERIFIER_OBSERVATION_SCHEMA
from tool_proxy.governance_contract import (
    AuthoritativeToolReceipt,
    VerifiedToolEffect,
    canonical_json,
    canonical_sha256,
)


PERFORMANCE_PORTFOLIO_SCHEMA = "veyra.performance_portfolio.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
_MODEL_SUCCESSES = frozenset({"model_assisted", "ok", "success"})
_MODEL_FAILURES = frozenset(
    {
        "auth_error",
        "auth_missing",
        "error",
        "http_error",
        "invalid_json",
        "invalid_response",
        "timeout",
        "unavailable",
        "unsupported_provider",
    }
)


@dataclass(frozen=True, slots=True)
class _Observation:
    actor_type: str
    actor_name: str
    outcome: str
    metric_scope: str
    observed_at: datetime | None
    latency_ms: int | None
    freshness_ttl_seconds: int


class _SourceHealth:
    def __init__(self, source: str) -> None:
        self.source = source
        self.accepted_count = 0
        self.rejected_count = 0
        self.unknown_count = 0
        self.issues: set[str] = set()
        self.truncated = False

    def accept(self, outcome: str) -> None:
        self.accepted_count += 1
        if outcome == "unknown":
            self.unknown_count += 1

    def reject(self, issue: str) -> None:
        self.rejected_count += 1
        self.issues.add(issue)

    def note(self, issue: str) -> None:
        self.issues.add(issue)

    def to_dict(self) -> dict[str, Any]:
        if self.rejected_count:
            status = "degraded"
        elif self.accepted_count:
            status = "success"
        else:
            status = "empty"
        return {
            "source": self.source,
            "status": status,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "unknown_count": self.unknown_count,
            "window_truncated": self.truncated,
            "issues": sorted(self.issues),
        }


class PerformancePortfolio:
    """Derive a descriptive portfolio from compact authoritative projections.

    This runtime has no writer, router, selector, or policy callback.  Its
    success rates use only explicit success/failure observations.  Pending,
    stale, incomplete, expected-governance, and unverified outcomes remain
    ``unknown`` and never enter the success denominator.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        now_fn: Any | None = None,
        minimum_support: int = 20,
        observation_ttl_seconds: int = 86_400,
    ) -> None:
        self.state_store = state_store
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.minimum_support = _bounded_int(
            minimum_support,
            "minimum_support",
            minimum=1,
            maximum=100_000,
        )
        self.observation_ttl_seconds = _bounded_int(
            observation_ttl_seconds,
            "observation_ttl_seconds",
            minimum=1,
            maximum=604_800,
        )

    def snapshot(self, *, limit: int = 1000) -> dict[str, Any]:
        selected_limit = _bounded_int(
            limit,
            "limit",
            minimum=1,
            maximum=10_000,
        )
        now = self._now()
        observations: list[_Observation] = []
        sources: list[_SourceHealth] = []
        for reader in (
            self._decision_observations,
            self._model_observations,
            self._execution_observations,
            self._tool_observations,
            self._runtime_matrix_observations,
        ):
            source_observations, source_health = reader(
                limit=selected_limit,
                now=now,
            )
            observations.extend(source_observations)
            sources.append(source_health)

        grouped: dict[
            tuple[str, str, str],
            list[_Observation],
        ] = {}
        for observation in observations:
            key = (
                observation.actor_type,
                observation.actor_name,
                observation.metric_scope,
            )
            grouped.setdefault(key, []).append(observation)
        actors = [
            self._actor_projection(key, rows, now=now)
            for key, rows in sorted(grouped.items())
        ]
        source_rows = [source.to_dict() for source in sources]
        status = (
            "degraded"
            if any(item["status"] == "degraded" for item in source_rows)
            else "success"
            if actors
            else "empty"
        )
        return {
            "schema_version": PERFORMANCE_PORTFOLIO_SCHEMA,
            "status": status,
            "generated_at": now.isoformat(),
            "window_limit_per_source": selected_limit,
            "minimum_support": self.minimum_support,
            "unknown_excluded_from_success_denominator": True,
            "actors": actors,
            "actor_count": len(actors),
            "sources": source_rows,
            "authority": {
                "mode": "read_only_shadow_portfolio",
                "policy_effect": "none",
                "route_selection_allowed": False,
                "provider_selection_allowed": False,
                "provider_switch_allowed": False,
                "model_weight_change_allowed": False,
                "autonomy_change_allowed": False,
                "capability_grant_allowed": False,
                "execution_allowed": False,
            },
        }

    def _decision_observations(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[list[_Observation], _SourceHealth]:
        source = _SourceHealth("decision_trace.jsonl")
        rows = self._jsonl("decision_trace.jsonl", limit, source)
        observations: list[_Observation] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("parse_error") is True:
                source.reject("malformed_record")
                continue
            route = _identifier(row.get("final_route"))
            if route is None:
                # decision_trace also hosts non-route audit records.
                continue
            outcome = _decision_outcome(row.get("outcome_category"))
            observed_at, future = _observation_time(
                row,
                ("completed_at", "timestamp", "received_at"),
                now=now,
            )
            outcome, observed_at = _freshness_guarded_outcome(
                outcome,
                observed_at=observed_at,
                future=future,
                now=now,
                ttl_seconds=self.observation_ttl_seconds,
                source=source,
            )
            latency = _latency(row.get("latency_ms"))
            route_observation = _Observation(
                actor_type="route",
                actor_name=route,
                outcome=outcome,
                metric_scope="request_operational_outcome",
                observed_at=observed_at,
                latency_ms=latency,
                freshness_ttl_seconds=self.observation_ttl_seconds,
            )
            observations.append(route_observation)
            source.accept(outcome)

            raw_probes = row.get("probe_used")
            if isinstance(raw_probes, str):
                raw_probes = [raw_probes]
            if raw_probes is None:
                raw_probes = []
            if not isinstance(raw_probes, list):
                source.reject("invalid_probe_projection")
                continue
            seen: set[str] = set()
            for raw_probe in raw_probes:
                probe = _identifier(raw_probe)
                if probe is None:
                    source.reject("invalid_probe_identity")
                    continue
                if probe in seen:
                    continue
                seen.add(probe)
                observations.append(
                    _Observation(
                        actor_type="probe",
                        actor_name=probe,
                        outcome=outcome,
                        metric_scope="request_outcome_when_probe_used",
                        observed_at=observed_at,
                        latency_ms=latency,
                        freshness_ttl_seconds=(
                            self.observation_ttl_seconds
                        ),
                    )
                )
                source.accept(outcome)
        return observations, source

    def _model_observations(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[list[_Observation], _SourceHealth]:
        source = _SourceHealth("core_model_trace.jsonl")
        rows = self._jsonl("core_model_trace.jsonl", limit, source)
        observations: list[_Observation] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("parse_error") is True:
                source.reject("malformed_record")
                continue
            result = (
                row.get("result")
                if isinstance(row.get("result"), dict)
                else {}
            )
            model = (
                result.get("_model")
                if isinstance(result.get("_model"), dict)
                else {}
            )
            provider = _identifier(
                model.get("provider") or result.get("provider")
            )
            model_name = _identifier(
                model.get("model") or result.get("model")
            )
            actor_name = (
                f"{provider}/{model_name}"
                if provider and model_name
                else provider
                or model_name
                or "unattributed"
            )
            status = str(
                row.get("status") or result.get("status") or ""
            )
            outcome = (
                "success"
                if status in _MODEL_SUCCESSES
                else "failure"
                if status in _MODEL_FAILURES
                else "unknown"
            )
            observed_at, future = _observation_time(
                row,
                ("timestamp",),
                now=now,
            )
            outcome, observed_at = _freshness_guarded_outcome(
                outcome,
                observed_at=observed_at,
                future=future,
                now=now,
                ttl_seconds=self.observation_ttl_seconds,
                source=source,
            )
            observation = _Observation(
                actor_type="model",
                actor_name=actor_name,
                outcome=outcome,
                metric_scope="model_transport_outcome",
                observed_at=observed_at,
                latency_ms=_latency(
                    row.get("duration_ms") or result.get("duration_ms")
                ),
                freshness_ttl_seconds=self.observation_ttl_seconds,
            )
            observations.append(observation)
            source.accept(outcome)
        return observations, source

    def _execution_observations(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[list[_Observation], _SourceHealth]:
        source = _SourceHealth("execution_trace.jsonl")
        rows = self._jsonl("execution_trace.jsonl", limit, source)
        observations: list[_Observation] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("parse_error") is True:
                source.reject("malformed_record")
                continue
            executor = _identifier(row.get("executor"))
            if executor is None:
                source.reject("invalid_executor_identity")
                continue
            actor = _execution_actor(
                route=_identifier(row.get("route")),
                executor=executor,
            )
            if actor is None:
                source.reject("execution_actor_route_mismatch")
                continue
            verification = (
                row.get("verification")
                if isinstance(row.get("verification"), dict)
                else {}
            )
            trace_status = str(row.get("status") or "")
            verification_status = str(
                verification.get("status") or ""
            )
            claimed_terminal = (
                trace_status,
                verification_status,
            ) in {
                ("verified_success", "verified_success"),
                ("verified_failed", "verified_failed"),
            }
            observation_bound = _verifier_observation_matches(
                row,
                verification,
            )
            if (
                trace_status == "verified_success"
                and verification_status == "verified_success"
                and observation_bound
                and _verified_success_contract(
                    row,
                    executor=executor,
                    verification=verification,
                )
            ):
                outcome = "success"
            elif (
                trace_status == "verified_failed"
                and verification_status == "verified_failed"
                and observation_bound
                and _verified_failure_contract(
                    row,
                    executor=executor,
                    verification=verification,
                )
            ):
                outcome = "failure"
            else:
                outcome = "unknown"
                if claimed_terminal:
                    source.reject(
                        "invalid_bound_verifier_observation"
                    )
            observed_at, future = _observation_time(
                row,
                ("recorded_at", "timestamp"),
                now=now,
            )
            outcome, observed_at = _freshness_guarded_outcome(
                outcome,
                observed_at=observed_at,
                future=future,
                now=now,
                ttl_seconds=self.observation_ttl_seconds,
                source=source,
            )
            actor_type, actor_name = actor
            observation = _Observation(
                actor_type=actor_type,
                actor_name=actor_name,
                outcome=outcome,
                metric_scope="persisted_verifier_outcome",
                observed_at=observed_at,
                latency_ms=None,
                freshness_ttl_seconds=self.observation_ttl_seconds,
            )
            observations.append(observation)
            source.accept(outcome)
        return observations, source

    def _tool_observations(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[list[_Observation], _SourceHealth]:
        source = _SourceHealth("tool_governance_state.json")
        try:
            document = self.state_store.read_json(
                "tool_governance_state.json"
            )
        except Exception:
            source.reject("state_read_failed")
            return [], source
        if (
            not isinstance(document, dict)
            or document.get("_state_corrupt") is True
            or document.get("schema_version")
            != "veyra.tool_governance_state.v1"
        ):
            source.reject("state_corrupt_or_unsupported")
            return [], source
        calls = document.get("calls")
        if not isinstance(calls, dict):
            source.reject("calls_projection_invalid")
            return [], source
        entries = list(calls.items())
        if len(entries) > limit:
            entries = entries[-limit:]
            source.truncated = True
        observations: list[_Observation] = []
        seen_receipt_ids: set[str] = set()
        seen_call_identities: set[tuple[str, str]] = set()
        for reservation_id, entry in entries:
            if not isinstance(entry, dict):
                source.reject("malformed_call")
                continue
            try:
                receipt = AuthoritativeToolReceipt.model_validate_json(
                    canonical_json(entry.get("receipt"))
                )
            except Exception:
                source.reject("invalid_authoritative_receipt")
                continue
            if (
                not isinstance(reservation_id, str)
                or reservation_id != receipt.reservation_id
            ):
                source.reject("receipt_reservation_key_mismatch")
                continue
            call_identity = (receipt.run_id, receipt.tool_call_id)
            if receipt.receipt_id in seen_receipt_ids:
                source.reject("duplicate_receipt_id")
                continue
            if call_identity in seen_call_identities:
                source.reject("duplicate_tool_call_identity")
                continue
            seen_receipt_ids.add(receipt.receipt_id)
            seen_call_identities.add(call_identity)
            observed_at = (
                receipt.observed_at or receipt.reserved_at
            ).astimezone(timezone.utc)
            future = (
                observed_at > now + _clock_skew()
            )
            latency = (
                int(
                    (
                        receipt.observed_at - receipt.reserved_at
                    ).total_seconds()
                    * 1000
                )
                if receipt.observed_at is not None
                else None
            )
            if receipt.ledger_state == "observed_failure":
                outcome = "failure"
            elif receipt.ledger_state == "observed_success":
                raw_effect = entry.get("effect_evidence")
                if not isinstance(raw_effect, dict):
                    outcome = "unknown"
                    source.note(
                        "successful_receipt_without_verified_effect"
                    )
                else:
                    try:
                        effect = VerifiedToolEffect.model_validate_json(
                            canonical_json(raw_effect)
                        )
                        self._match_tool_effect(receipt, effect)
                    except Exception:
                        outcome = "unknown"
                        source.reject("invalid_or_mismatched_effect")
                    else:
                        outcome = "success"
            else:
                outcome = "unknown"
            outcome, observed_at = _freshness_guarded_outcome(
                outcome,
                observed_at=observed_at,
                future=future,
                now=now,
                ttl_seconds=self.observation_ttl_seconds,
                source=source,
            )
            observation = _Observation(
                actor_type="tool",
                actor_name=_identifier(receipt.tool_name)
                or "unattributed",
                outcome=outcome,
                metric_scope="authoritative_tool_effect",
                observed_at=observed_at,
                latency_ms=latency,
                freshness_ttl_seconds=self.observation_ttl_seconds,
            )
            observations.append(observation)
            source.accept(outcome)
        return observations, source

    def _runtime_matrix_observations(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[list[_Observation], _SourceHealth]:
        source = _SourceHealth("ops_runtime_matrix.json")
        try:
            document = self.state_store.read_json(
                "ops_runtime_matrix.json"
            )
        except Exception:
            source.reject("state_read_failed")
            return [], source
        if not isinstance(document, dict) or document.get(
            "_state_corrupt"
        ) is True:
            source.reject("state_corrupt")
            return [], source
        runtimes = document.get("runtimes")
        if not isinstance(runtimes, list):
            source.reject("runtime_projection_invalid")
            return [], source
        if len(runtimes) > limit:
            runtimes = runtimes[-limit:]
            source.truncated = True
        observed_at = _parse_aware(
            document.get("observed_at")
            or document.get("checked_at")
            or document.get("updated_at")
        )
        ttl = _safe_ttl(
            document.get("ttl_seconds"),
            default=1800,
        )
        matrix_freshness = "unknown"
        if observed_at is None:
            source.note("missing_or_invalid_observed_at")
        else:
            delta = (now - observed_at).total_seconds()
            if delta < -30:
                source.reject("future_observation")
                observed_at = None
            elif delta <= ttl:
                matrix_freshness = "fresh"
            else:
                matrix_freshness = "stale"
                source.note("runtime_matrix_stale")

        observations: list[_Observation] = []
        for row in runtimes:
            if not isinstance(row, dict):
                source.reject("malformed_runtime_row")
                continue
            runtime = _identifier(row.get("name"))
            if runtime is None:
                source.reject("invalid_runtime_identity")
                continue
            row_status = str(row.get("status") or "unknown")
            validation = (
                row.get("validation")
                if isinstance(row.get("validation"), dict)
                else {}
            )
            if (
                matrix_freshness == "fresh"
                and row_status == "ready"
                and validation.get("validated") is True
                and validation.get("status") == "validated"
            ):
                outcome = "success"
            elif matrix_freshness == "fresh" and row_status == "error":
                outcome = "failure"
            else:
                outcome = "unknown"
            observation = _Observation(
                actor_type="provider",
                actor_name=runtime,
                outcome=outcome,
                metric_scope="fresh_runtime_probe_outcome",
                observed_at=observed_at,
                latency_ms=None,
                freshness_ttl_seconds=ttl,
            )
            observations.append(observation)
            source.accept(outcome)
        return observations, source

    def _jsonl(
        self,
        name: str,
        limit: int,
        source: _SourceHealth,
    ) -> list[Any]:
        try:
            return self.state_store.read_jsonl(name, limit=limit)
        except Exception:
            source.reject("source_read_failed")
            return []

    def _actor_projection(
        self,
        key: tuple[str, str, str],
        rows: list[_Observation],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        actor_type, actor_name, metric_scope = key
        success = sum(row.outcome == "success" for row in rows)
        failure = sum(row.outcome == "failure" for row in rows)
        unknown = sum(row.outcome == "unknown" for row in rows)
        evaluated = success + failure
        latencies = sorted(
            row.latency_ms
            for row in rows
            if row.latency_ms is not None
        )
        timed = [
            row
            for row in rows
            if row.observed_at is not None
        ]
        if timed:
            latest = max(
                timed,
                key=lambda row: row.observed_at
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            last_observed = latest.observed_at
            age_seconds = max(
                0,
                int((now - last_observed).total_seconds()),
            )
            freshness = (
                "fresh"
                if age_seconds <= latest.freshness_ttl_seconds
                else "stale"
            )
        else:
            last_observed = None
            age_seconds = None
            freshness = "unknown"
        return {
            "actor_type": actor_type,
            "actor_id": f"{actor_type}:{actor_name}",
            "metric_scope": metric_scope,
            "success_count": success,
            "failure_count": failure,
            "unknown_count": unknown,
            "evaluated_count": evaluated,
            "total_count": len(rows),
            "success_rate": (
                round(success / evaluated, 6)
                if evaluated
                else None
            ),
            "latency_ms": {
                "sample_count": len(latencies),
                "p50": _nearest_rank(latencies, 0.50),
                "p95": _nearest_rank(latencies, 0.95),
                "method": "nearest_rank",
                "sample_scope": "all_outcomes_with_latency",
            },
            "support": {
                "status": (
                    "sufficient"
                    if evaluated >= self.minimum_support
                    else "insufficient_data"
                ),
                "evaluated_count": evaluated,
                "minimum_evaluated_count": self.minimum_support,
            },
            "freshness": {
                "status": freshness,
                "last_observed_at": (
                    last_observed.isoformat()
                    if last_observed is not None
                    else None
                ),
                "age_seconds": age_seconds,
            },
            "policy_effect": "none",
        }

    @staticmethod
    def _match_tool_effect(
        receipt: AuthoritativeToolReceipt,
        effect: VerifiedToolEffect,
    ) -> None:
        if receipt.observed_at is None:
            raise ValueError("verified effect requires an observed receipt")
        expected = (
            receipt.receipt_id,
            receipt.run_id,
            receipt.tool_call_id,
            receipt.tool_name,
            receipt.invocation_digest,
            receipt.result_digest,
            receipt.targets_digest,
        )
        observed = (
            effect.receipt_id,
            effect.run_id,
            effect.tool_call_id,
            effect.tool_name,
            effect.invocation_digest,
            effect.result_digest,
            effect.targets_digest,
        )
        if (
            receipt.ledger_state != "observed_success"
            or expected != observed
            or effect.observed_at < receipt.observed_at
        ):
            raise ValueError(
                "verified effect does not match its authoritative receipt"
            )

    def _now(self) -> datetime:
        selected = self._now_fn()
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise ValueError(
                "performance portfolio clock must be timezone-aware"
            )
        return selected.astimezone(timezone.utc)


def _verifier_observation_matches(
    row: dict[str, Any],
    verification: dict[str, Any],
) -> bool:
    observation = row.get("verifier_observation")
    expected_fields = {
        "schema_version",
        "trace_id",
        "event_id",
        "route",
        "task_id",
        "executor",
        "trace_status",
        "verification_status",
        "verification_verdict",
        "evidence_digest",
        "execution_result_digest",
        "recorded_at",
        "binding_digest",
    }
    if (
        not isinstance(observation, dict)
        or set(observation) != expected_fields
        or observation.get("schema_version")
        != VERIFIER_OBSERVATION_SCHEMA
    ):
        return False
    evidence = (
        verification.get("evidence")
        if isinstance(verification.get("evidence"), dict)
        else {}
    )
    execution_result = (
        row.get("execution_result")
        if isinstance(row.get("execution_result"), dict)
        else {}
    )
    try:
        semantics = {
            "schema_version": VERIFIER_OBSERVATION_SCHEMA,
            "trace_id": str(row.get("trace_id") or ""),
            "event_id": (
                str(row.get("event_id"))
                if row.get("event_id") is not None
                else None
            ),
            "route": str(row.get("route") or ""),
            "task_id": str(row.get("task_id") or ""),
            "executor": str(row.get("executor") or ""),
            "trace_status": str(row.get("status") or ""),
            "verification_status": str(
                verification.get("status") or ""
            ),
            "verification_verdict": str(
                verification.get("verdict") or ""
            ),
            "evidence_digest": canonical_sha256(evidence),
            "execution_result_digest": canonical_sha256(
                execution_result
            ),
            "recorded_at": str(row.get("recorded_at") or ""),
        }
        return observation == {
            **semantics,
            "binding_digest": canonical_sha256(semantics),
        }
    except (TypeError, ValueError):
        return False


def _verified_success_contract(
    row: dict[str, Any],
    *,
    executor: str,
    verification: dict[str, Any],
) -> bool:
    task_id = _identifier(row.get("task_id"))
    execution_result = (
        row.get("execution_result")
        if isinstance(row.get("execution_result"), dict)
        else {}
    )
    evidence = (
        verification.get("evidence")
        if isinstance(verification.get("evidence"), dict)
        else {}
    )
    structured = (
        evidence.get("structured_evidence")
        if isinstance(evidence.get("structured_evidence"), dict)
        else {}
    )
    tool_calls = evidence.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    execution_task = execution_result.get("task_id")
    if (
        task_id is None
        or execution_result.get("executor") != executor
        or execution_result.get("status") != "success"
        or (
            execution_task is not None
            and execution_task != task_id
        )
        or evidence.get("task_id") != task_id
        or evidence.get("executor") != executor
        or evidence.get("reported_status") != "success"
        or evidence.get("result_text_is_evidence") is not False
        or evidence.get("raw_presence_is_evidence") is not False
        or verification.get("verdict")
        != "execution_success_supported_by_structured_evidence"
        or verification.get("needs_rollback") is not False
        or verification.get("needs_memory_patch") is not True
        or structured.get("sufficient") is not True
        or structured.get("independent_outcome_observed") is not True
        or structured.get("authoritative_failure_observed") is not False
        or structured.get("caller_projection_mismatch") is not False
        or structured.get("authoritative_effect_evidence_invalid")
        is not False
        or structured.get("agent_plan_only") is not False
        or structured.get("dialogue_proposal_only") is not False
    ):
        return False
    if tool_calls:
        compliance = evidence.get("tool_proxy_compliance")
        receipt_count = structured.get("authoritative_receipt_count")
        effect_count = structured.get("authoritative_effect_count")
        return bool(
            isinstance(compliance, dict)
            and compliance.get("status") == "compliant"
            and structured.get("authority_observed") is True
            and type(receipt_count) is int
            and receipt_count > 0
            and type(effect_count) is int
            and effect_count == receipt_count
            and structured.get(
                "authoritative_effect_missing_count"
            )
            == 0
            and structured.get("authoritative_effect_errors") == []
            and verification.get("claim_scope")
            == "authoritative_verified_projection_only"
            and isinstance(
                verification.get("verified_projection"),
                dict,
            )
        )
    sources = structured.get("sources")
    return bool(
        isinstance(sources, list)
        and sources
        and all(
            isinstance(item, str)
            and 0 < len(item) <= 240
            for item in sources
        )
    )


def _verified_failure_contract(
    row: dict[str, Any],
    *,
    executor: str,
    verification: dict[str, Any],
) -> bool:
    task_id = _identifier(row.get("task_id"))
    execution_result = (
        row.get("execution_result")
        if isinstance(row.get("execution_result"), dict)
        else {}
    )
    execution_task = execution_result.get("task_id")
    return bool(
        task_id is not None
        and execution_result.get("executor") == executor
        and (
            execution_task is None
            or execution_task == task_id
        )
        and execution_result.get("status")
        in {"blocked", "error", "failed", "success", "timeout"}
        and _identifier(verification.get("verdict")) is not None
    )


def _decision_outcome(value: Any) -> str:
    selected = str(value or "")
    if selected == "success":
        return "success"
    if selected == "runtime_failure":
        return "failure"
    return "unknown"


def _execution_actor(
    *,
    route: str | None,
    executor: str,
) -> tuple[str, str] | None:
    if executor.startswith("probe:"):
        if route != "probe":
            return None
        actor_name = _identifier(executor.removeprefix("probe:"))
        return ("probe", actor_name) if actor_name else None
    if executor.startswith("skill:"):
        if route not in {"skill", "human_review"}:
            return None
        actor_name = _identifier(executor.removeprefix("skill:"))
        return ("skill", actor_name) if actor_name else None
    if route != "agent":
        return None
    if executor.startswith("agent:"):
        actor_name = _identifier(executor.removeprefix("agent:"))
        return ("agent", actor_name) if actor_name else None
    return "agent", executor


def _freshness_guarded_outcome(
    outcome: str,
    *,
    observed_at: datetime | None,
    future: bool,
    now: datetime,
    ttl_seconds: int,
    source: _SourceHealth,
) -> tuple[str, datetime | None]:
    if future:
        source.reject("future_observation")
        return "unknown", None
    if observed_at is None:
        source.note("missing_or_invalid_observed_at")
        return "unknown", None
    if (now - observed_at).total_seconds() > ttl_seconds:
        source.note("stale_observation")
        return "unknown", observed_at
    return outcome, observed_at


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    selected = value.strip()
    return selected if _IDENTIFIER.fullmatch(selected) else None


def _parse_aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _observation_time(
    value: dict[str, Any],
    fields: tuple[str, ...],
    *,
    now: datetime,
) -> tuple[datetime | None, bool]:
    for field in fields:
        parsed = _parse_aware(value.get(field))
        if parsed is None:
            continue
        if parsed > now + _clock_skew():
            return None, True
        return parsed, False
    return None, False


def _clock_skew() -> timedelta:
    return timedelta(seconds=30)


def _latency(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        selected = int(value)
    except (TypeError, ValueError):
        return None
    return selected if 0 <= selected <= 86_400_000 else None


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return values[index]


def _bounded_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        selected = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not minimum <= selected <= maximum:
        raise ValueError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return selected


def _safe_ttl(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        selected = int(value)
    except (TypeError, ValueError):
        return default
    return selected if 1 <= selected <= 86_400 else default


__all__ = [
    "PERFORMANCE_PORTFOLIO_SCHEMA",
    "PerformancePortfolio",
]
