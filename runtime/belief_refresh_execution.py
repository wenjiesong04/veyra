"""Execution boundary for selected Belief refresh work.

The scheduler remains a pure policy module and ``StateRefresh`` remains the
public facade.  This module only executes an already-admitted batch through
explicit callbacks; it owns no state store and performs no scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from awareness.belief_core import BeliefCore
from awareness.belief_economy import BeliefEconomyError, validate_economy
from awareness.claim_schema import claim_identity_key
from core.perception_layer import PerceptionLayer
from runtime.belief_refresh_scheduler import (
    consumed_entry,
    conflict_retry_entry,
    schedule_marker,
    unrefreshable_entry,
)


@dataclass(frozen=True)
class RefreshExecutionContext:
    """Narrow dependencies needed after scheduler admission."""

    selected: list[dict[str, Any]]
    now: datetime
    probes: Mapping[str, Any]
    current_claim: Callable[[dict[str, Any]], dict[str, Any] | None]
    target_for_claim: Callable[[dict[str, Any]], str | None]
    refresh_cas_binding: Callable[[dict[str, Any]], dict[str, Any]]
    interpret_probe_result: Callable[[dict[str, Any]], dict[str, Any]]
    valid_persistence_receipt: Callable[[Any], bool]
    strip_private: Callable[[Any], Any] = PerceptionLayer._strip_refresh_cas


@dataclass
class RefreshExecutionResult:
    """Receipts and private ledger additions produced by one admitted batch."""

    refreshed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    unresolvable: list[dict[str, Any]] = field(default_factory=list)
    consumed: list[dict[str, Any]] = field(default_factory=list)
    unrefreshable: list[dict[str, Any]] = field(default_factory=list)
    conflict_retry: list[dict[str, Any]] = field(default_factory=list)


def execute_refresh_batch(context: RefreshExecutionContext) -> RefreshExecutionResult:
    """Run probes and accepted Belief persistence for pre-admitted claims.

    The function deliberately does not select claims, mutate scheduler state,
    or write a state store. All state reads/writes remain behind the supplied
    callbacks, making this boundary mechanical and testable without a second
    runtime facade.
    """

    result = RefreshExecutionResult()
    for claim in context.selected:
        current_claim = context.current_claim(claim)
        if current_claim is None:
            result.failed.append(
                {
                    "claim": claim.get("key") or claim.get("claim"),
                    "reason": "refresh_cas_claim_missing_before_probe",
                }
            )
            continue

        selected_original = claim.get("_refresh_original_claim")
        if not isinstance(selected_original, dict):
            selected_original = claim.get("_refresh_selected_projection")
        if not isinstance(selected_original, dict):
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "reason": "refresh_selection_binding_missing",
                }
            )
            continue

        # Compare exact durable rows. Evaluator-only lifecycle fields stay on
        # the private wrapper and are never guessed away for CAS purposes.
        selected_digest = BeliefCore.claim_projection_digest(selected_original)
        current_digest = BeliefCore.claim_projection_digest(current_claim)
        selected_binding = (
            claim_identity_key(selected_original),
            selected_original.get("claim_revision"),
            selected_original.get("economy"),
            selected_original.get("refresh_spec"),
        )
        current_binding = (
            claim_identity_key(current_claim),
            current_claim.get("claim_revision"),
            current_claim.get("economy"),
            current_claim.get("refresh_spec"),
        )
        if selected_digest != current_digest or selected_binding != current_binding:
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "reason": "refresh_selection_cas_lost",
                }
            )
            continue

        source = str(current_claim.get("source") or "")
        probe = context.probes.get(source)
        if probe is None:
            if (claim.get("_refresh_schedule") or {}).get("hard_overdue"):
                result.failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "source": source,
                        "reason": "hard_overdue_unrefreshable",
                    }
                )
            else:
                result.unrefreshable.append(
                    unrefreshable_entry(
                        current_claim,
                        reason="unsupported_source",
                        now=context.now,
                    )
                )
            continue

        target = context.target_for_claim(current_claim)
        if target is None:
            result.unresolvable.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "source": source,
                    "reason": "no_resolvable_refresh_target",
                }
            )
            if (claim.get("_refresh_schedule") or {}).get("hard_overdue"):
                result.failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "source": source,
                        "reason": "hard_overdue_unrefreshable",
                    }
                )
            elif str(current_claim.get("status") or "") != "conflict":
                result.unrefreshable.append(
                    unrefreshable_entry(
                        current_claim,
                        reason="invalid_or_missing_refresh_target",
                        now=context.now,
                    )
                )
            continue

        refresh_cas = context.refresh_cas_binding(current_claim)
        try:
            raw = probe.run(target)
        except Exception as exc:  # pragma: no cover - probe-specific failures
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "source": source,
                    "reason": "probe_error",
                    "detail": str(exc),
                }
            )
            continue
        if not isinstance(raw, dict):
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "source": source,
                    "reason": "probe_result_malformed",
                }
            )
            continue

        scope_fields = ("scope_kind", "tenant_derived", "user_id", "session_id")
        raw = {key: value for key, value in raw.items() if key not in scope_fields}

        # Reserved refresh transport fields are server-owned. An adapter may
        # never supply or override them; in particular, stripping a forged
        # ``_refresh_economy`` would make the spoof invisible to receipts and
        # could let PerceptionLayer persist it as if it were carry-forward.
        reserved_refresh = [key for key in raw if str(key).startswith("_refresh_")]
        if reserved_refresh:
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "source": source,
                    "reason": "economy_spoof_rejected",
                }
            )
            continue

        # Economy is producer-owned. Existing economy may be carried forward,
        # but a refresh adapter cannot create or replace it.
        existing_economy = current_claim.get("economy")
        if isinstance(existing_economy, dict):
            supplied_economy = raw.get("economy")
            if supplied_economy is None:
                raw["_refresh_economy"] = existing_economy
            else:
                try:
                    if validate_economy(supplied_economy) != validate_economy(existing_economy):
                        result.failed.append(
                            {
                                "claim": current_claim.get("key") or current_claim.get("claim"),
                                "source": source,
                                "reason": "economy_spoof_rejected",
                            }
                        )
                        continue
                except BeliefEconomyError:
                    result.failed.append(
                        {
                            "claim": current_claim.get("key") or current_claim.get("claim"),
                            "source": source,
                            "reason": "economy_spoof_rejected",
                        }
                    )
                    continue
        elif raw.get("economy") is not None:
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "source": source,
                    "reason": "economy_spoof_rejected",
                }
            )
            continue

        raw.update({key: current_claim.get(key) for key in scope_fields if key in current_claim})
        raw.update({"refresh_mode": "stale_claim", "refresh_cas": refresh_cas})
        try:
            patch = context.interpret_probe_result(raw)
        except Exception as exc:  # pragma: no cover - adapter-specific failures
            result.failed.append(
                {
                    "claim": current_claim.get("key") or current_claim.get("claim"),
                    "probe_result": context.strip_private(raw),
                    "reason": "persistence_error",
                    "detail": type(exc).__name__,
                }
            )
            continue

        if isinstance(patch, dict):
            patched_economy = patch.get("economy")
            if patched_economy is not None:
                if not isinstance(existing_economy, dict):
                    result.failed.append(
                        {
                            "claim": current_claim.get("key") or current_claim.get("claim"),
                            "source": source,
                            "reason": "economy_spoof_rejected",
                        }
                    )
                    continue
                try:
                    if validate_economy(patched_economy) != validate_economy(existing_economy):
                        result.failed.append(
                            {
                                "claim": current_claim.get("key") or current_claim.get("claim"),
                                "source": source,
                                "reason": "economy_spoof_rejected",
                            }
                        )
                        continue
                except BeliefEconomyError:
                    result.failed.append(
                        {
                            "claim": current_claim.get("key") or current_claim.get("claim"),
                            "source": source,
                            "reason": "economy_spoof_rejected",
                        }
                    )
                    continue

        receipt_raw = context.strip_private({key: value for key, value in raw.items() if key != "refresh_cas"})
        persistence = patch.get("belief_persistence") if isinstance(patch, dict) else None
        persistence_status = str(persistence.get("status") or "") if isinstance(persistence, dict) else ""
        entry = {
            "claim": current_claim.get("key") or current_claim.get("claim"),
            "probe_result": receipt_raw,
            "state_patch": context.strip_private(patch),
        }

        def record_accepted_refresh() -> None:
            consumed_item = consumed_entry(
                {**current_claim, "_refresh_schedule": claim.get("_refresh_schedule")},
                now=context.now,
            )
            if consumed_item is not None:
                result.consumed.append(consumed_item)
            refreshed_claim = context.current_claim(current_claim) or {}
            selected_schedule = claim.get("_refresh_schedule") or {}
            if selected_schedule.get("next_refresh_at") and schedule_marker(refreshed_claim) == schedule_marker(selected_original):
                result.failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "reason": "schedule_not_advanced",
                    }
                )
            if (
                str(selected_original.get("status") or "") == "conflict"
                and not selected_schedule.get("next_refresh_at")
                and str(refreshed_claim.get("status") or "") == "conflict"
            ):
                result.conflict_retry.append(conflict_retry_entry(refreshed_claim, now=context.now))

        if persistence is None:
            if isinstance(patch, dict) and patch.get("status") in {"accepted", "success"}:
                result.refreshed.append(entry)
                record_accepted_refresh()
            else:
                result.failed.append(
                    {
                        **entry,
                        "reason": f"belief_persistence_{patch.get('status') if isinstance(patch, dict) else 'malformed'}",
                    }
                )
        elif context.valid_persistence_receipt(persistence):
            result.refreshed.append(entry)
            record_accepted_refresh()
        else:
            # A conflict receipt is not an accepted Belief value, but it can
            # still prove that the read-only governance attempt was durably
            # recorded.  Cadence-less conflicts must remember that exact
            # post-write revision or they will churn on every ActiveLoop tick.
            # Transient/rejected receipts never enter this suppression ledger.
            receipt_results = (
                persistence.get("results")
                if isinstance(persistence, dict)
                and isinstance(persistence.get("results"), list)
                else []
            )
            selected_key = str(current_claim.get("key") or current_claim.get("claim") or "")
            durable_conflict = any(
                isinstance(item, dict)
                and str(item.get("key") or "") == selected_key
                and item.get("persisted") is True
                and item.get("belief_value_persisted") is False
                and item.get("persistence_status") == "conflict"
                for item in receipt_results
            )
            selected_schedule = claim.get("_refresh_schedule") or {}
            if (
                durable_conflict
                and str(selected_original.get("status") or "") == "conflict"
                and not selected_schedule.get("next_refresh_at")
                and not selected_schedule.get("hard_overdue")
            ):
                refreshed_claim = context.current_claim(current_claim) or {}
                if str(refreshed_claim.get("status") or "") == "conflict":
                    result.conflict_retry.append(
                        conflict_retry_entry(refreshed_claim, now=context.now)
                    )
            receipt_statuses = {
                str(item.get("persistence_status") or "")
                for item in receipt_results
                if isinstance(item, dict)
            }
            failure_status = "cas_rejected" if "cas_rejected" in receipt_statuses else persistence_status or "malformed"
            result.failed.append({**entry, "reason": f"belief_persistence_{failure_status}"})
    return result


__all__ = ["RefreshExecutionContext", "RefreshExecutionResult", "execute_refresh_batch"]
