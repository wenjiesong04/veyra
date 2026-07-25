from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from core.situation_evaluator import SituationEvaluator
from core.world_state import WorldStateStore
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import LoopResult, Route, VeyraEvent, utc_now_iso
from runtime.event_inbox import EventInbox


VERIFIED_RESULT_STATUSES = {"verified_failed", "verified_success"}


class ShadowAwarenessRuntime:
    """Connect durable events to evidence-linked situations without authority.

    This component is deliberately outside the routing and execution policy
    path. Failures are observable but never change the user's route, response,
    risk classification, or authorization.
    """

    MODES = {"disabled", "record_only", "shadow"}

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        mode: str = "record_only",
    ) -> None:
        selected_mode = str(mode or "record_only").strip().lower()
        if selected_mode not in self.MODES:
            selected_mode = "record_only"
        self.state_store = state_store
        self.mode = selected_mode
        self.event_inbox = EventInbox(state_store)
        self.situation_evaluator = SituationEvaluator(state_store)

    def publish(self, event: VeyraEvent) -> dict[str, Any]:
        """Durably admit an internal or external event without processing it."""

        if self.mode == "disabled":
            return {"status": "disabled", "event_id": event.event_id}
        return self.event_inbox.enqueue(event)

    def begin(self, event: VeyraEvent) -> dict[str, Any]:
        """Claim and observe a foreground event before the existing turn runs."""

        if self.mode == "disabled":
            return {"status": "disabled"}
        if self.mode == "record_only":
            try:
                admission = self.event_inbox.enqueue(event)
                return {
                    "status": "recorded",
                    "event_id": admission.get("event_id"),
                }
            except Exception as exc:
                self._record_error("event_record_only_error", event.event_id, exc)
                return {"status": "degraded", "error_type": type(exc).__name__}
        consumer_id = f"awareness-shadow-foreground-{uuid4().hex[:12]}"
        claimed = False
        canonical_event_id = event.event_id
        situation_id = ""
        try:
            admission = self.event_inbox.enqueue_and_claim(
                event,
                consumer_id,
                lease_seconds=120,
            )
            claimed = bool(admission.get("claimed"))
            canonical_event_id = str(
                admission.get("canonical_event_id") or event.event_id
            )
            canonical_envelope = admission.get("canonical_envelope")
            claimed_envelope = admission.get("claimed_envelope")
            envelope = (
                claimed_envelope
                if isinstance(claimed_envelope, dict)
                else canonical_envelope
            )
            if not isinstance(envelope, dict):
                raise ValueError("event inbox did not return a canonical envelope")
            observed_event = VeyraEvent.from_dict(envelope)
            # ``observe`` is replay-safe and also drains a durable trace outbox.
            # Calling it for duplicates repairs a crash between state commit and
            # JSONL trace projection instead of silently trusting prior state.
            situation = self.situation_evaluator.observe(
                observed_event,
                salience_components=self._explicit_salience(observed_event),
            )
            situation_id = str(situation.get("situation_id") or "")
            same_event_delivery = canonical_event_id == event.event_id
            if not claimed:
                return {
                    "status": str(admission.get("status") or "duplicate"),
                    "event_id": canonical_event_id,
                    "situation_id": situation_id,
                    # Resolution writes are atomically idempotent. A duplicate
                    # can repair an earlier failed finalize without duplicating
                    # lifecycle history under concurrent callers, but a
                    # different event suppressed by a shared dedupe key must
                    # never project its result into the canonical event.
                    "finalize_allowed": same_event_delivery,
                }
            self.event_inbox.complete(
                canonical_event_id,
                consumer_id,
                {
                    "status": "observed",
                    "situation_id": situation_id,
                },
            )
            return {
                "status": "observed",
                "event_id": canonical_event_id,
                "situation_id": situation_id,
                "finalize_allowed": same_event_delivery,
            }
        except Exception as exc:
            if claimed:
                self._fail_claim(canonical_event_id, consumer_id, exc)
            self._record_error(
                "shadow_awareness_intake_error",
                canonical_event_id,
                exc,
            )
            return {
                "status": "degraded",
                "event_id": canonical_event_id,
                "situation_id": situation_id or None,
                # If observation committed before a later inbox completion
                # failure, preserve this turn's decision/outcome projection.
                "finalize_allowed": bool(
                    situation_id
                    and claimed
                    and canonical_event_id == event.event_id
                ),
                "error_type": type(exc).__name__,
            }

    def finalize(
        self,
        event: VeyraEvent,
        result: LoopResult,
        trace: dict[str, Any],
        *,
        situation_id: str | None = None,
        canonical_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the already-made decision/outcome after observation completed."""

        if self.mode != "shadow":
            return None
        selected_situation_id = str(situation_id or "")
        try:
            if result.event_id != event.event_id:
                raise ValueError("result event does not match the finalized event")
            if (
                canonical_event_id is not None
                and str(canonical_event_id) != event.event_id
            ):
                raise ValueError(
                    "canonical event does not match the finalized event"
                )
            situation = (
                self.situation_evaluator.get(
                    selected_situation_id,
                    user_id=event.source.user_id,
                    session_id=event.source.session_id,
                )
                if selected_situation_id
                else self._situation_for_event(event)
            )
            if (
                isinstance(situation, dict)
                and not self._situation_matches_event(situation, event)
            ):
                raise ValueError(
                    "situation does not belong to the finalized event"
                )
            if not isinstance(situation, dict):
                # A foreground/background interleaving or a repaired intake can
                # leave no lookup result for the delivered id. Observation is
                # idempotent, so upsert the source event rather than dropping
                # the already-made decision and outcome.
                situation = self.situation_evaluator.observe(
                    event,
                    salience_components=self._explicit_salience(event),
                )
            if not self._situation_matches_event(situation, event):
                raise ValueError(
                    "observed situation does not belong to the finalized event"
                )
            selected_situation_id = str(situation.get("situation_id") or "")
            trace_id = str(trace.get("trace_id") or "")
            trace_ref = {
                "ref_id": trace_id,
                "source": "runtime_trace",
                "epistemic_status": "observation",
                "is_fact": True,
            }
            verification = (
                result.artifacts.get("verification")
                if isinstance(result.artifacts.get("verification"), dict)
                else {}
            )
            execution_trace = (
                result.artifacts.get("execution_trace")
                if isinstance(result.artifacts.get("execution_trace"), dict)
                else {}
            )
            execution_trace_id = str(execution_trace.get("trace_id") or "")
            persisted_verification = self._persisted_verification(
                event_id=event.event_id,
                result_event_id=result.event_id,
                trace_id=execution_trace_id,
                expected_route=result.route.value,
                expected_result_status=result.status,
                expected_task_id=self._expected_task_id(event, result),
                expected_verification=verification,
            )
            verified_outcome = persisted_verification is not None
            authoritative_verification = (
                persisted_verification.get("verification")
                if isinstance(persisted_verification, dict)
                and isinstance(persisted_verification.get("verification"), dict)
                else verification
            )
            claimed_verification_status = next(
                (
                    status
                    for status in (
                        str(result.status or ""),
                        str(verification.get("status") or ""),
                    )
                    if status in VERIFIED_RESULT_STATUSES
                ),
                "",
            )
            verification_pending = bool(
                claimed_verification_status and not verified_outcome
            )
            verification_summary = (
                {
                    "status": "verification_pending",
                    "claimed_status": claimed_verification_status,
                    "verdict": "persisted_execution_evidence_missing",
                    "next_action": "continue_evaluation",
                }
                if verification_pending
                else {
                    key: authoritative_verification.get(key)
                    for key in ("status", "verdict", "next_action")
                    if key in authoritative_verification
                }
            )
            outcome_value = {
                "route": (
                    str(persisted_verification.get("route") or "")
                    if verified_outcome
                    else result.route.value
                ),
                "status": (
                    str(persisted_verification.get("status") or "")
                    if verified_outcome
                    else "verification_pending"
                    if verification_pending
                    else result.status
                ),
                "verification": verification_summary,
            }
            decision_value = {
                "route": result.route.value,
                "status": (
                    "verification_pending"
                    if verification_pending
                    else result.status
                ),
                "risk_level": result.risk_level.value,
            }
            verification_ref = {
                "ref_id": (
                    f"execution_trace:{execution_trace_id}:verification"
                    if execution_trace_id
                    else f"action_record:{event.event_id}:verification"
                ),
                "source": "execution_trace" if execution_trace_id else "action_record",
                "epistemic_status": (
                    "verified"
                    if verified_outcome
                    else "reference"
                ),
                "is_fact": verified_outcome,
            }
            resolution_key = self._resolution_key(
                event_id=str(canonical_event_id or event.event_id),
                decision=decision_value,
                outcome=outcome_value,
                outcome_is_fact=verified_outcome,
            )
            resolved: dict[str, Any] | None = None
            last_error: Exception | None = None
            # The combined mutation is idempotent, so one immediate retry safely
            # repairs transient atomic-write failures without duplicate history.
            for _ in range(2):
                try:
                    resolved = self.situation_evaluator.record_resolution(
                        selected_situation_id,
                        idempotency_key=resolution_key,
                        expected_source_event_id=event.event_id,
                        expected_correlation_id=(
                            event.correlation_id or event.event_id
                        ),
                        decision=decision_value,
                        outcome=outcome_value,
                        user_id=event.source.user_id,
                        session_id=event.source.session_id,
                        decision_evidence_refs=[trace_ref],
                        outcome_evidence_refs=(
                            [trace_ref, verification_ref]
                            if verification
                            else [trace_ref]
                        ),
                        decision_source="veyra_policy",
                        outcome_source=(
                            "verifier"
                            if verified_outcome
                            else "runtime_result"
                        ),
                        outcome_evidence_verified=verified_outcome,
                        decision_status=self._decision_status(result),
                        outcome_status=(
                            "monitoring"
                            if verification_pending
                            else self._outcome_status(result)
                        ),
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if resolved is None:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("situation resolution did not return state")
            return {
                "situation_id": selected_situation_id,
                "correlation_id": resolved.get("correlation_id"),
                "status": resolved.get("status"),
                "decision_id": (
                    resolved.get("decision", {}).get("decision_id")
                    if isinstance(resolved.get("decision"), dict)
                    else None
                ),
                "outcome_id": (
                    resolved.get("outcome", {}).get("outcome_id")
                    if isinstance(resolved.get("outcome"), dict)
                    else None
                ),
                "shadow_only": True,
            }
        except Exception as exc:
            self._record_error(
                "shadow_awareness_finalize_error",
                event.event_id,
                exc,
                situation_id=selected_situation_id,
            )
            return {
                "situation_id": selected_situation_id or None,
                "status": "degraded",
                "error_type": type(exc).__name__,
                "shadow_only": True,
            }

    @staticmethod
    def _resolution_key(
        *,
        event_id: str,
        decision: dict[str, Any],
        outcome: dict[str, Any],
        outcome_is_fact: bool,
    ) -> str:
        encoded = json.dumps(
            {
                "event_id": event_id,
                "decision": decision,
                "outcome": outcome,
                "outcome_is_fact": bool(outcome_is_fact),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"sires_{hashlib.sha256(encoded).hexdigest()[:32]}"

    def process_pending(self, *, limit: int = 20) -> dict[str, Any]:
        """Project pending background events into situations, never actions."""

        if self.mode == "disabled":
            return {
                "status": "disabled",
                "processed_count": 0,
                "failed_count": 0,
                "processed": [],
                "failed": [],
                "inbox": self.event_inbox.stats(),
            }
        processed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        consumer_id = "awareness-shadow-background"
        trace_recovery_error: str | None = None
        try:
            self.situation_evaluator.flush_trace_outbox()
        except Exception as exc:
            trace_recovery_error = type(exc).__name__
            self._record_error(
                "situation_trace_recovery_error",
                "",
                exc,
            )
        for _ in range(max(0, min(int(limit), 100))):
            claimed = self.event_inbox.claim(consumer_id, lease_seconds=60)
            if not isinstance(claimed, dict):
                break
            event_id = str(claimed.get("event_id") or "")
            try:
                event = VeyraEvent.from_dict(claimed)
                situation = self.situation_evaluator.observe(
                    event,
                    salience_components=self._explicit_salience(event),
                )
                self.event_inbox.complete(
                    event_id,
                    consumer_id,
                    {
                        "status": "observed",
                        "situation_id": situation.get("situation_id"),
                    },
                )
                processed.append(
                    {
                        "event_id": event_id,
                        "situation_id": situation.get("situation_id"),
                        "status": "observed",
                    }
                )
            except Exception as exc:
                retry_status = self._fail_claim(event_id, consumer_id, exc)
                failed.append(
                    {
                        "event_id": event_id,
                        "status": retry_status,
                        "error_type": type(exc).__name__,
                    }
                )
        return {
            "status": (
                "success"
                if not failed and trace_recovery_error is None
                else "degraded"
            ),
            "processed_count": len(processed),
            "failed_count": len(failed),
            "processed": processed,
            "failed": failed,
            "trace_recovery": {
                "status": "success" if trace_recovery_error is None else "degraded",
                "error_type": trace_recovery_error,
            },
            "inbox": self.event_inbox.stats(),
        }

    def _situation_for_event(self, event: VeyraEvent) -> dict[str, Any] | None:
        items = self.situation_evaluator.list(
            user_id=event.source.user_id,
            session_id=event.source.session_id,
            correlation_id=event.correlation_id or event.event_id,
            limit=10,
        )
        return next(
            (
                item
                for item in items
                if str(item.get("source_event_id") or "") == event.event_id
            ),
            None,
        )

    @staticmethod
    def _situation_matches_event(
        situation: dict[str, Any],
        event: VeyraEvent,
    ) -> bool:
        return (
            str(situation.get("source_event_id") or "") == event.event_id
            and str(situation.get("correlation_id") or "")
            == str(event.correlation_id or event.event_id)
        )

    def _fail_claim(
        self,
        event_id: str,
        consumer_id: str,
        exc: Exception,
    ) -> str:
        try:
            failure = self.event_inbox.fail(
                event_id,
                consumer_id,
                exc,
                retry=True,
                retry_delay_seconds=5,
            )
            return str(failure.get("status") or "unknown")
        except Exception:
            return "claim_lost"

    def _record_error(
        self,
        kind: str,
        event_id: str,
        exc: Exception,
        *,
        situation_id: str | None = None,
    ) -> None:
        record = {
            "kind": kind,
            "event_id": event_id,
            "error_type": type(exc).__name__,
            "timestamp": utc_now_iso(),
        }
        if situation_id:
            record["situation_id"] = situation_id
        try:
            self.state_store.append_jsonl("alert_log.jsonl", record)
        except Exception:
            # Shadow observability must never become part of the user-facing
            # availability path.
            return

    def _persisted_verification(
        self,
        *,
        event_id: str,
        result_event_id: str,
        trace_id: str,
        expected_route: str,
        expected_result_status: str,
        expected_task_id: str | None,
        expected_verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        expected_status = str(expected_verification.get("status") or "")
        if (
            not trace_id
            or result_event_id != event_id
            or not expected_route
            or not expected_task_id
            or not expected_status.startswith("verified_")
        ):
            return None
        try:
            rows = self.state_store.read_jsonl("execution_trace.jsonl", limit=2000)
        except Exception:
            return None
        for row in reversed(rows):
            if str(row.get("trace_id") or "") != trace_id:
                continue
            if str(row.get("event_id") or "") != event_id:
                return None
            if str(row.get("route") or "") != expected_route:
                return None
            if str(row.get("task_id") or "") != expected_task_id:
                return None
            persisted_status = str(row.get("status") or "")
            if not self._result_status_matches_trace(
                expected_result_status,
                persisted_status,
            ):
                return None
            verification = (
                row.get("verification")
                if isinstance(row.get("verification"), dict)
                else {}
            )
            if (
                str(verification.get("status") or "") != expected_status
                or persisted_status != expected_status
            ):
                return None
            for key in ("verdict", "next_action", "evidence"):
                if (
                    key in expected_verification
                    and expected_verification.get(key) != verification.get(key)
                ):
                    return None
            evidence = verification.get("evidence")
            if not (
                isinstance(evidence, dict)
                and bool(evidence)
                or isinstance(evidence, list)
                and bool(evidence)
            ):
                return None
            return row
        return None

    @staticmethod
    def _expected_task_id(event: VeyraEvent, result: LoopResult) -> str | None:
        candidates: list[str] = []
        payload_task_id = str(event.payload.get("task_id") or "")
        if payload_task_id:
            candidates.append(payload_task_id)
        for key in ("execution_result", "task_packet"):
            item = (
                result.artifacts.get(key)
                if isinstance(result.artifacts.get(key), dict)
                else {}
            )
            task_id = str(item.get("task_id") or "")
            if task_id:
                candidates.append(task_id)
        if not candidates and result.route in {Route.PROBE, Route.SKILL}:
            candidates.append(event.event_id)
        unique = set(candidates)
        return candidates[0] if len(unique) == 1 else None

    @staticmethod
    def _result_status_matches_trace(result_status: str, trace_status: str) -> bool:
        return result_status == trace_status or (
            result_status == "success" and trace_status == "verified_success"
        )

    @staticmethod
    def _explicit_salience(event: VeyraEvent) -> dict[str, Any]:
        components = event.payload.get("salience_components")
        return components if isinstance(components, dict) else {}

    @staticmethod
    def _decision_status(result: LoopResult) -> str:
        if result.route in {Route.ASK_USER, Route.HUMAN_REVIEW}:
            return "waiting_human"
        if result.status in NON_TERMINAL_STATUSES:
            return "monitoring"
        return "decided"

    @staticmethod
    def _outcome_status(result: LoopResult) -> str:
        status = str(result.status or "").lower()
        if result.route in {Route.ASK_USER, Route.HUMAN_REVIEW}:
            return "waiting_human"
        if status in NON_TERMINAL_STATUSES:
            return "monitoring"
        if status in {"needs_more_probe", "needs_rollback"}:
            return "monitoring"
        if status in {"needs_action_proposal", "needs_parameters"}:
            return "waiting_human"
        if status == "partially_success":
            return "partial"
        if "indeterminate" in status:
            return "indeterminate"
        if any(token in status for token in ("error", "fail", "timeout", "unsupported")):
            return "failed"
        if result.route == Route.BLOCK:
            return "closed"
        return "resolved"
