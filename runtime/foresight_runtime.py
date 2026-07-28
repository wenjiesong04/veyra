from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import ValidationError

from core.foresight_contract import (
    ForesightAssessment,
    PredictionResidual,
    UnknownCapabilityEffectContract,
    assess_tool_invocation,
    risk_at_least,
    resolve_capability_effect_contract,
)
from core.world_state import WorldStateStore
from tool_proxy.agent_tool_contract import ToolReceiptResolver
from tool_proxy.governance_contract import (
    AuthoritativeToolReceipt,
    ToolInvocation,
    VerifiedToolEffect,
    canonical_json,
)


STATE_SCHEMA_VERSION = "veyra.foresight_runtime_state.v1"
PROMOTION_SCHEMA_VERSION = "veyra.foresight_promotion_eligibility.v1"
STATE_FILE = "foresight_runtime_state.json"
MAX_ASSESSMENTS = 256
MAX_CLOCK_SKEW = timedelta(minutes=5)
RETRYABLE_INDETERMINATE_REASONS = frozenset(
    {
        "authoritative_receipt_resolver_unavailable",
        "authoritative_receipt_resolution_failed",
        "authoritative_receipt_missing",
        "receipt_not_terminal_observed",
        "authoritative_effect_missing",
    }
)


class ForesightRuntimeError(RuntimeError):
    """Base error for deterministic Foresight state handling."""


class ForesightStateError(ForesightRuntimeError):
    """Raised when persisted Foresight state cannot be trusted."""


class ForesightConflict(ForesightRuntimeError):
    """Raised for contradictory assessment or residual replay."""


class ForesightRuntime:
    """Persist exact predictions and reconcile authoritative sandbox effects.

    This runtime is deliberately not an executor or policy writer. It cannot
    change a feature mode, issue a grant, approve a review, or raise an autonomy
    level. ``preview`` is pure; ``register_sandbox_trial`` only records a
    prediction that an existing governed executor may later satisfy.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        tool_receipt_resolver: ToolReceiptResolver | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.tool_receipt_resolver = tool_receipt_resolver
        self._now = now or (lambda: datetime.now(timezone.utc))

    def preview(
        self,
        invocation: ToolInvocation,
        *,
        valid_for_seconds: int = 900,
    ) -> dict[str, Any]:
        """Return a zero-persistence, zero-execution deterministic preview."""

        assessment = assess_tool_invocation(
            invocation,
            created_at=self._now_utc(),
            valid_for_seconds=valid_for_seconds,
        )
        return {
            "status": "previewed",
            "dry_run": True,
            "persisted": False,
            "execution_attempted": False,
            "assessment": assessment.model_dump(mode="json"),
            **self._no_authority_projection(),
        }

    def register_sandbox_trial(
        self,
        invocation: ToolInvocation,
        *,
        valid_for_seconds: int = 900,
    ) -> dict[str, Any]:
        """Persist the prediction before an existing sandbox executor runs."""

        existing = self._assessment_for_invocation(invocation.invocation_digest)
        if existing is not None:
            return self._registered_projection(existing, replay=True)
        assessment = assess_tool_invocation(
            invocation,
            created_at=self._now_utc(),
            valid_for_seconds=valid_for_seconds,
        )
        contract = resolve_capability_effect_contract(assessment.capability_id)
        if (
            contract.trial_mode != "sandbox_trial"
            or contract.evidence_kind
            != "authoritative_tool_receipt_and_effect"
        ):
            raise ForesightRuntimeError(
                "capability does not use the governed sandbox evidence path"
            )
        stored = self._store_assessment(assessment)
        return self._registered_projection(stored, replay=False)

    def reconcile(self, assessment_id: str) -> dict[str, Any]:
        """Compare a prediction with its Veyra-owned receipt/effect evidence."""

        assessment, existing = self._load_entry(assessment_id)
        if existing is not None:
            return {
                "status": existing.status,
                "residual": existing.model_dump(mode="json"),
                "promotion": self.promotion_eligibility(assessment_id),
                "replayed": True,
                "persisted": True,
                "reevaluable": False,
                **self._no_authority_projection(),
            }
        residual = self._resolve_residual(assessment)
        if self._retryable_residual(residual):
            return {
                "status": "pending",
                "residual": residual.model_dump(mode="json"),
                "promotion": self._promotion_projection(
                    status="not_ready",
                    reason="authoritative_evidence_pending",
                    assessment=assessment,
                    residual=residual,
                ),
                "replayed": False,
                "persisted": False,
                "reevaluable": True,
                **self._no_authority_projection(),
            }
        stored = self._store_residual(assessment, residual)
        return {
            "status": stored.status,
            "residual": stored.model_dump(mode="json"),
            "promotion": self.promotion_eligibility(assessment_id),
            "replayed": False,
            "persisted": True,
            "reevaluable": False,
            **self._no_authority_projection(),
        }

    def promotion_eligibility(self, assessment_id: str) -> dict[str, Any]:
        """Read-only gate: report eligibility without applying promotion."""

        try:
            assessment, residual = self._load_entry(assessment_id)
            contract = resolve_capability_effect_contract(
                assessment.capability_id
            )
        except Exception as exc:
            return self._promotion_projection(
                status="blocked",
                reason=f"foresight_state_untrusted:{type(exc).__name__}",
            )

        if (
            contract.contract_digest != assessment.contract_digest
            or contract.revision != assessment.contract_revision
        ):
            return self._promotion_projection(
                status="blocked",
                reason="capability_effect_contract_changed",
                assessment=assessment,
                residual=residual,
            )
        if self._now_utc() >= assessment.valid_until:
            return self._promotion_projection(
                status="blocked",
                reason="assessment_expired",
                assessment=assessment,
                residual=residual,
            )
        if (
            not contract.promotion_supported
            or assessment.trial_mode != "sandbox_trial"
            or assessment.evidence_kind
            != "authoritative_tool_receipt_and_effect"
        ):
            return self._promotion_projection(
                status="not_ready",
                reason="requires_capability_specific_promotion_gate",
                assessment=assessment,
                residual=residual,
            )
        if residual is None:
            return self._promotion_projection(
                status="not_ready",
                reason="authoritative_sandbox_effect_not_reconciled",
                assessment=assessment,
            )
        if residual.status == "exact":
            return self._promotion_projection(
                status="eligible",
                reason="exact_prediction_matches_authoritative_sandbox_effect",
                assessment=assessment,
                residual=residual,
            )
        return self._promotion_projection(
            status="blocked",
            reason=(
                "prediction_effect_mismatch"
                if residual.status == "mismatch"
                else "prediction_effect_indeterminate"
            ),
            assessment=assessment,
            residual=residual,
        )

    def status(self) -> dict[str, Any]:
        try:
            document = self._read_document()
            entries = document["assessments"]
            counts = {"pending": 0, "exact": 0, "mismatch": 0, "indeterminate": 0}
            for entry in entries.values():
                raw = entry.get("residual") if isinstance(entry, dict) else None
                if not isinstance(raw, dict):
                    counts["pending"] += 1
                    continue
                residual = self._load_residual(raw)
                counts[residual.status] += 1
            return {
                "status": "available",
                "schema_version": STATE_SCHEMA_VERSION,
                "assessment_count": len(entries),
                "residual_counts": counts,
                **self._no_authority_projection(),
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "schema_version": STATE_SCHEMA_VERSION,
                "reason": f"foresight_state_untrusted:{type(exc).__name__}",
                **self._no_authority_projection(),
            }

    def _resolve_residual(
        self,
        assessment: ForesightAssessment,
    ) -> PredictionResidual:
        now = self._now_utc()
        if now >= assessment.valid_until:
            return self._indeterminate(assessment, "assessment_expired", now=now)
        try:
            contract = resolve_capability_effect_contract(
                assessment.capability_id
            )
        except UnknownCapabilityEffectContract:
            return self._indeterminate(
                assessment,
                "capability_effect_contract_unknown",
                now=now,
            )
        if (
            contract.contract_digest != assessment.contract_digest
            or contract.revision != assessment.contract_revision
        ):
            return self._indeterminate(
                assessment,
                "capability_effect_contract_changed",
                now=now,
            )
        if self.tool_receipt_resolver is None:
            return self._indeterminate(
                assessment,
                "authoritative_receipt_resolver_unavailable",
                now=now,
            )
        try:
            resolved = self.tool_receipt_resolver(
                assessment.run_id,
                assessment.tool_call_id,
            )
        except Exception:
            return self._indeterminate(
                assessment,
                "authoritative_receipt_resolution_failed",
                now=now,
            )
        if not isinstance(resolved, Mapping):
            return self._indeterminate(
                assessment,
                "authoritative_receipt_missing",
                now=now,
            )

        raw_receipt = dict(resolved)
        raw_effect = raw_receipt.pop("effect_evidence", None)
        try:
            receipt = AuthoritativeToolReceipt.model_validate_json(
                canonical_json(raw_receipt),
                strict=True,
            )
        except (ValidationError, TypeError, ValueError):
            return self._indeterminate(
                assessment,
                "authoritative_receipt_invalid",
                now=now,
            )

        binding_reasons: list[str] = []
        if receipt.run_id != assessment.run_id:
            binding_reasons.append("cross_run_receipt_rejected")
        if receipt.tool_call_id != assessment.tool_call_id:
            binding_reasons.append("cross_call_receipt_rejected")
        if receipt.invocation_digest != assessment.invocation_digest:
            binding_reasons.append("invocation_digest_mismatch")
        if receipt.args_digest != assessment.args_digest:
            binding_reasons.append("args_digest_mismatch")
        if receipt.targets_digest != assessment.targets_digest:
            binding_reasons.append("targets_digest_mismatch")
        if receipt.environment_digest != assessment.environment_digest:
            binding_reasons.append("environment_digest_mismatch")
        if receipt.tool_name != contract.canonical_invocation_tool_name:
            binding_reasons.append("capability_tool_mismatch")
        if receipt.tool_kind != contract.invocation_tool_kind:
            binding_reasons.append("capability_tool_kind_mismatch")
        if not risk_at_least(receipt.risk_level, contract.risk_floor):
            binding_reasons.append("receipt_risk_below_contract_floor")
        if binding_reasons:
            return self._indeterminate(
                assessment,
                *binding_reasons,
                receipt=receipt,
                now=now,
            )
        if receipt.reserved_at > now + MAX_CLOCK_SKEW:
            return self._indeterminate(
                assessment,
                "receipt_observation_time_invalid",
                receipt=receipt,
                now=now,
            )
        if receipt.observed_at is None:
            return self._indeterminate(
                assessment,
                "receipt_not_terminal_observed",
                receipt=receipt,
                now=now,
            )
        if receipt.observed_at > now + MAX_CLOCK_SKEW:
            return self._indeterminate(
                assessment,
                "receipt_observation_time_invalid",
                receipt=receipt,
                now=now,
            )
        temporal_reasons: list[str] = []
        if receipt.reserved_at < assessment.created_at:
            temporal_reasons.append(
                "execution_reserved_before_prediction"
            )
        if receipt.observed_at < assessment.created_at:
            temporal_reasons.append(
                "authoritative_effect_precedes_prediction"
            )
        if temporal_reasons:
            return self._indeterminate(
                assessment,
                *temporal_reasons,
                receipt=receipt,
                now=now,
            )
        if receipt.ledger_state == "observed_failure":
            return self._indeterminate(
                assessment,
                "execution_failed_without_verified_zero_effect",
                receipt=receipt,
                now=now,
            )
        if receipt.ledger_state != "observed_success":
            return self._indeterminate(
                assessment,
                "receipt_not_terminal_observed",
                receipt=receipt,
                now=now,
            )
        if not isinstance(raw_effect, Mapping):
            return self._indeterminate(
                assessment,
                "authoritative_effect_missing",
                receipt=receipt,
                now=now,
            )
        try:
            effect = VerifiedToolEffect.model_validate_json(
                canonical_json(dict(raw_effect)),
                strict=True,
            )
        except (ValidationError, TypeError, ValueError):
            return self._indeterminate(
                assessment,
                "authoritative_effect_invalid",
                receipt=receipt,
                now=now,
            )

        receipt_binding = (
            receipt.receipt_id,
            receipt.run_id,
            receipt.tool_call_id,
            receipt.tool_name,
            receipt.invocation_digest,
            receipt.result_digest,
            receipt.targets_digest,
        )
        effect_binding = (
            effect.receipt_id,
            effect.run_id,
            effect.tool_call_id,
            effect.tool_name,
            effect.invocation_digest,
            effect.result_digest,
            effect.targets_digest,
        )
        if receipt_binding != effect_binding:
            return self._indeterminate(
                assessment,
                "authoritative_effect_receipt_binding_mismatch",
                receipt=receipt,
                effect=effect,
                now=now,
            )
        if (
            effect.observed_at < receipt.observed_at
            or effect.observed_at > now + MAX_CLOCK_SKEW
        ):
            return self._indeterminate(
                assessment,
                "effect_observation_time_invalid",
                receipt=receipt,
                effect=effect,
                now=now,
            )

        predicted_targets = tuple(
            assessment.predicted_effect.authorized_targets
        )
        predicted_changed = tuple(
            assessment.predicted_effect.expected_changed_files
        )
        observed_targets = tuple(effect.authorized_targets)
        observed_changed = tuple(effect.changed_files)
        if effect.source != contract.executor_id:
            return self._indeterminate(
                assessment,
                "effect_executor_identity_untrusted",
                receipt=receipt,
                effect=effect,
                now=now,
            )

        reasons: list[str] = []
        if observed_targets != predicted_targets:
            reasons.append("authorized_target_projection_mismatch")
        missing = tuple(
            sorted(set(predicted_changed) - set(observed_changed))
        )
        unexpected = tuple(
            sorted(set(observed_changed) - set(predicted_changed))
        )
        if missing:
            reasons.append("predicted_changed_file_missing")
        if unexpected:
            reasons.append("unexpected_changed_file_observed")
        exact_reason = "predicted_and_authoritative_effects_match"
        if not reasons:
            reasons.append(exact_reason)
        return PredictionResidual.create(
            assessment_id=assessment.assessment_id,
            assessment_digest=assessment.assessment_digest,
            capability_id=assessment.capability_id,
            contract_digest=assessment.contract_digest,
            invocation_digest=assessment.invocation_digest,
            run_id=assessment.run_id,
            tool_call_id=assessment.tool_call_id,
            status="exact" if reasons == [exact_reason] else "mismatch",
            reasons=tuple(reasons),
            predicted_changed_files=predicted_changed,
            observed_changed_files=observed_changed,
            missing_changed_files=missing,
            unexpected_changed_files=unexpected,
            receipt_id=receipt.receipt_id,
            receipt_result_digest=receipt.result_digest,
            effect_evidence_digest=effect.evidence_digest,
            observed_at=effect.observed_at,
        )

    def _indeterminate(
        self,
        assessment: ForesightAssessment,
        *reasons: str,
        receipt: AuthoritativeToolReceipt | None = None,
        effect: VerifiedToolEffect | None = None,
        now: datetime | None = None,
    ) -> PredictionResidual:
        predicted = tuple(
            assessment.predicted_effect.expected_changed_files
        )
        observed = tuple(effect.changed_files) if effect is not None else ()
        return PredictionResidual.create(
            assessment_id=assessment.assessment_id,
            assessment_digest=assessment.assessment_digest,
            capability_id=assessment.capability_id,
            contract_digest=assessment.contract_digest,
            invocation_digest=assessment.invocation_digest,
            run_id=assessment.run_id,
            tool_call_id=assessment.tool_call_id,
            status="indeterminate",
            reasons=tuple(reasons) or ("outcome_indeterminate",),
            predicted_changed_files=predicted,
            observed_changed_files=observed,
            missing_changed_files=tuple(
                sorted(set(predicted) - set(observed))
            ),
            unexpected_changed_files=tuple(
                sorted(set(observed) - set(predicted))
            ),
            receipt_id=receipt.receipt_id if receipt is not None else None,
            receipt_result_digest=(
                receipt.result_digest if receipt is not None else None
            ),
            effect_evidence_digest=(
                effect.evidence_digest if effect is not None else None
            ),
            observed_at=now or self._now_utc(),
        )

    def _store_assessment(
        self,
        assessment: ForesightAssessment,
    ) -> ForesightAssessment:
        selected: ForesightAssessment | None = None

        def persist(document: dict[str, Any]) -> None:
            nonlocal selected
            current = self._normalize_document(document)
            entries = current["assessments"]
            index = current["invocation_index"]
            prior_id = index.get(assessment.invocation_digest)
            if isinstance(prior_id, str):
                prior = entries.get(prior_id)
                if not isinstance(prior, dict):
                    raise ForesightStateError(
                        "invocation index points to a missing assessment"
                    )
                loaded = self._load_assessment(prior.get("assessment"))
                if loaded.invocation_digest != assessment.invocation_digest:
                    raise ForesightConflict(
                        "invocation digest points to another prediction"
                    )
                selected = loaded
                document.clear()
                document.update(current)
                return
            if assessment.assessment_id in entries:
                raise ForesightConflict(
                    "assessment id is already bound to another invocation"
                )
            self._make_capacity(entries, index)
            entries[assessment.assessment_id] = {
                "assessment": assessment.model_dump(mode="json"),
                "residual": None,
                "registered_at": self._now_utc().isoformat(),
            }
            index[assessment.invocation_digest] = assessment.assessment_id
            current["updated_at"] = self._now_utc().isoformat()
            selected = assessment
            document.clear()
            document.update(current)

        self.state_store.mutate_json(STATE_FILE, persist)
        if selected is None:
            raise ForesightStateError("assessment admission produced no result")
        return selected

    def _store_residual(
        self,
        assessment: ForesightAssessment,
        residual: PredictionResidual,
    ) -> PredictionResidual:
        selected: PredictionResidual | None = None

        def persist(document: dict[str, Any]) -> None:
            nonlocal selected
            current = self._normalize_document(document)
            entry = current["assessments"].get(assessment.assessment_id)
            if not isinstance(entry, dict):
                raise ForesightStateError("assessment disappeared before reconcile")
            stored_assessment = self._load_assessment(entry.get("assessment"))
            if stored_assessment != assessment:
                raise ForesightConflict("assessment changed before reconcile")
            raw_existing = entry.get("residual")
            if isinstance(raw_existing, dict):
                existing = self._load_residual(raw_existing)
                if not self._same_residual_outcome(existing, residual):
                    raise ForesightConflict(
                        "a different residual already closes this assessment"
                    )
                selected = existing
            else:
                entry["residual"] = residual.model_dump(mode="json")
                entry["reconciled_at"] = self._now_utc().isoformat()
                current["updated_at"] = self._now_utc().isoformat()
                selected = residual
            document.clear()
            document.update(current)

        self.state_store.mutate_json(STATE_FILE, persist)
        if selected is None:
            raise ForesightStateError("residual admission produced no result")
        return selected

    @staticmethod
    def _same_residual_outcome(
        left: PredictionResidual,
        right: PredictionResidual,
    ) -> bool:
        ignored = {"residual_id", "residual_digest", "observed_at"}
        left_payload = {
            key: value
            for key, value in left.model_dump(mode="python").items()
            if key not in ignored
        }
        right_payload = {
            key: value
            for key, value in right.model_dump(mode="python").items()
            if key not in ignored
        }
        return left_payload == right_payload

    @staticmethod
    def _retryable_residual(residual: PredictionResidual) -> bool:
        return (
            residual.status == "indeterminate"
            and bool(residual.reasons)
            and all(
                reason in RETRYABLE_INDETERMINATE_REASONS
                for reason in residual.reasons
            )
        )

    def _assessment_for_invocation(
        self,
        invocation_digest: str,
    ) -> ForesightAssessment | None:
        document = self._read_document()
        assessment_id = document["invocation_index"].get(invocation_digest)
        if not isinstance(assessment_id, str):
            return None
        entry = document["assessments"].get(assessment_id)
        if not isinstance(entry, dict):
            raise ForesightStateError(
                "invocation index points to a missing assessment"
            )
        return self._load_assessment(entry.get("assessment"))

    def _load_entry(
        self,
        assessment_id: str,
    ) -> tuple[ForesightAssessment, PredictionResidual | None]:
        normalized_id = str(assessment_id or "").strip()
        if not normalized_id or normalized_id != assessment_id:
            raise ForesightStateError("normalized assessment_id is required")
        document = self._read_document()
        entry = document["assessments"].get(normalized_id)
        if not isinstance(entry, dict):
            raise KeyError(f"Foresight assessment not found: {normalized_id}")
        assessment = self._load_assessment(entry.get("assessment"))
        raw_residual = entry.get("residual")
        residual = (
            self._load_residual(raw_residual)
            if isinstance(raw_residual, dict)
            else None
        )
        return assessment, residual

    def _read_document(self) -> dict[str, Any]:
        raw = self.state_store.read_json(STATE_FILE)
        return self._normalize_document(copy.deepcopy(raw))

    def _normalize_document(self, document: dict[str, Any]) -> dict[str, Any]:
        if not document:
            return {
                "schema_version": STATE_SCHEMA_VERSION,
                "assessments": {},
                "invocation_index": {},
                "updated_at": None,
            }
        # ``mutate_json`` supplies its live document to callbacks. Work on a
        # detached copy so the final clear/update cannot also clear the
        # normalized source object.
        document = copy.deepcopy(document)
        if document.get("_state_corrupt"):
            raise ForesightStateError("foresight runtime state is corrupt")
        if document.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ForesightStateError(
                "foresight runtime schema is missing or unsupported"
            )
        assessments = document.get("assessments")
        index = document.get("invocation_index")
        if not isinstance(assessments, dict) or not isinstance(index, dict):
            raise ForesightStateError("foresight runtime indexes are malformed")
        if len(assessments) > MAX_ASSESSMENTS:
            raise ForesightStateError("foresight assessment capacity exceeded")
        seen_invocations: dict[str, str] = {}
        for assessment_id, entry in assessments.items():
            if not isinstance(assessment_id, str) or not isinstance(entry, dict):
                raise ForesightStateError("foresight assessment entry is malformed")
            assessment = self._load_assessment(entry.get("assessment"))
            if assessment.assessment_id != assessment_id:
                raise ForesightStateError("foresight assessment id mismatch")
            if assessment.invocation_digest in seen_invocations:
                raise ForesightStateError(
                    "multiple assessments share one invocation digest"
                )
            seen_invocations[assessment.invocation_digest] = assessment_id
            raw_residual = entry.get("residual")
            if raw_residual is not None:
                if not isinstance(raw_residual, dict):
                    raise ForesightStateError("foresight residual is malformed")
                residual = self._load_residual(raw_residual)
                if (
                    residual.assessment_id != assessment_id
                    or residual.assessment_digest
                    != assessment.assessment_digest
                    or residual.capability_id != assessment.capability_id
                    or residual.contract_digest
                    != assessment.contract_digest
                    or residual.invocation_digest
                    != assessment.invocation_digest
                    or residual.run_id != assessment.run_id
                    or residual.tool_call_id != assessment.tool_call_id
                ):
                    raise ForesightStateError(
                        "foresight residual binding is malformed"
                    )
        if index != seen_invocations:
            raise ForesightStateError("foresight invocation index is inconsistent")
        return document

    def _make_capacity(
        self,
        entries: dict[str, Any],
        index: dict[str, Any],
    ) -> None:
        if len(entries) < MAX_ASSESSMENTS:
            return
        evictable: list[tuple[str, str]] = []
        now = self._now_utc()
        for assessment_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("residual"), dict):
                evictable.append(
                    (
                        str(entry.get("reconciled_at") or ""),
                        str(assessment_id),
                    )
                )
                continue
            assessment = self._load_assessment(entry.get("assessment"))
            if assessment.valid_until <= now:
                evictable.append(
                    (
                        assessment.valid_until.isoformat(),
                        str(assessment_id),
                    )
                )
        if not evictable:
            raise ForesightStateError(
                "foresight capacity is full of unresolved assessments"
            )
        _, evicted_id = sorted(evictable)[0]
        evicted = entries.pop(evicted_id)
        assessment = self._load_assessment(evicted.get("assessment"))
        index.pop(assessment.invocation_digest, None)

    def _load_assessment(self, raw: Any) -> ForesightAssessment:
        if not isinstance(raw, dict):
            raise ForesightStateError("foresight assessment is missing")
        try:
            assessment = ForesightAssessment.model_validate_json(
                canonical_json(raw),
                strict=True,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise ForesightStateError(
                "foresight assessment failed strict validation"
            ) from exc
        return assessment

    def _load_residual(self, raw: Any) -> PredictionResidual:
        if not isinstance(raw, dict):
            raise ForesightStateError("prediction residual is missing")
        try:
            return PredictionResidual.model_validate_json(
                canonical_json(raw),
                strict=True,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise ForesightStateError(
                "prediction residual failed strict validation"
            ) from exc

    def _registered_projection(
        self,
        assessment: ForesightAssessment,
        *,
        replay: bool,
    ) -> dict[str, Any]:
        return {
            "status": "registered",
            "assessment_id": assessment.assessment_id,
            "assessment_digest": assessment.assessment_digest,
            "invocation_digest": assessment.invocation_digest,
            "contract_digest": assessment.contract_digest,
            "graph_digest": assessment.effect_graph.graph_digest,
            "trial_mode": assessment.trial_mode,
            "replayed": replay,
            **self._no_authority_projection(),
        }

    def _promotion_projection(
        self,
        *,
        status: str,
        reason: str,
        assessment: ForesightAssessment | None = None,
        residual: PredictionResidual | None = None,
    ) -> dict[str, Any]:
        evidence = {
            "assessment_id": (
                assessment.assessment_id if assessment is not None else None
            ),
            "assessment_digest": (
                assessment.assessment_digest
                if assessment is not None
                else None
            ),
            "contract_digest": (
                assessment.contract_digest if assessment is not None else None
            ),
            "invocation_digest": (
                assessment.invocation_digest
                if assessment is not None
                else None
            ),
            "graph_digest": (
                assessment.effect_graph.graph_digest
                if assessment is not None
                else None
            ),
            "residual_digest": (
                residual.residual_digest if residual is not None else None
            ),
            "residual_status": (
                residual.status if residual is not None else None
            ),
        }
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "status": status,
            "reason": reason,
            "capability_id": (
                assessment.capability_id if assessment is not None else None
            ),
            "evidence": {
                key: value for key, value in evidence.items() if value is not None
            },
            "eligibility_only": True,
            **self._no_authority_projection(),
        }

    @staticmethod
    def _no_authority_projection() -> dict[str, Any]:
        return {
            "promotion_applied": False,
            "mode_changed": False,
            "grant_issued": False,
            "grant_signed": False,
            "review_approved": False,
            "execution_authority_enabled": False,
            "promotion_authority_enabled": False,
            "autonomy_level_changed": False,
        }

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ForesightRuntimeError("runtime clock must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "ForesightConflict",
    "ForesightRuntime",
    "ForesightRuntimeError",
    "ForesightStateError",
    "MAX_ASSESSMENTS",
    "PROMOTION_SCHEMA_VERSION",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
]
