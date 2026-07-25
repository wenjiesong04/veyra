from __future__ import annotations

from typing import Any

from core.situation_evaluator import SituationEvaluator
from core.world_state import WorldStateStore
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import LoopResult, Route, VeyraEvent, utc_now_iso
from runtime.event_inbox import EventInbox


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
        consumer_id = "awareness-shadow-foreground"
        claimed = False
        try:
            admission = self.event_inbox.enqueue(event)
            envelope = self.event_inbox.claim_by_id(
                event.event_id,
                consumer_id,
                lease_seconds=120,
            )
            if not isinstance(envelope, dict):
                existing = self._situation_for_event(event)
                return {
                    "status": str(admission.get("status") or "duplicate"),
                    "situation_id": existing.get("situation_id") if existing else None,
                }
            claimed = True
            observed_event = VeyraEvent.from_dict(envelope)
            existing = self._situation_for_event(observed_event)
            situation = existing or self.situation_evaluator.observe(
                observed_event,
                salience_components=self._explicit_salience(observed_event),
            )
            situation_id = str(situation.get("situation_id") or "")
            self.event_inbox.complete(
                event.event_id,
                consumer_id,
                {
                    "status": "observed",
                    "situation_id": situation_id,
                },
            )
            return {"status": "observed", "situation_id": situation_id}
        except Exception as exc:
            if claimed:
                self._fail_claim(event.event_id, consumer_id, exc)
            self._record_error("shadow_awareness_intake_error", event.event_id, exc)
            return {"status": "degraded", "error_type": type(exc).__name__}

    def finalize(
        self,
        event: VeyraEvent,
        result: LoopResult,
        trace: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record the already-made decision/outcome after observation completed."""

        if self.mode != "shadow":
            return None
        situation_id = ""
        try:
            situation = self._situation_for_event(event)
            if not isinstance(situation, dict):
                return None
            situation_id = str(situation.get("situation_id") or "")
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
            outcome_value = {
                "route": (
                    str(persisted_verification.get("route") or "")
                    if verified_outcome
                    else result.route.value
                ),
                "status": (
                    str(persisted_verification.get("status") or "")
                    if verified_outcome
                    else result.status
                ),
                "verification": {
                    key: authoritative_verification.get(key)
                    for key in ("status", "verdict", "next_action")
                    if key in authoritative_verification
                },
            }
            if self._matches_existing_outcome(
                situation,
                outcome_value,
                is_fact=verified_outcome,
            ):
                return self._situation_summary(situation)
            decision = self.situation_evaluator.record_decision(
                situation_id,
                {
                    "route": result.route.value,
                    "status": result.status,
                    "risk_level": result.risk_level.value,
                },
                user_id=event.source.user_id,
                session_id=event.source.session_id,
                evidence_refs=[trace_ref],
                source="veyra_policy",
                status=self._decision_status(result),
            )
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
            outcome = self.situation_evaluator.record_outcome(
                situation_id,
                outcome_value,
                user_id=event.source.user_id,
                session_id=event.source.session_id,
                evidence_refs=[trace_ref, verification_ref] if verification else [trace_ref],
                source="verifier" if verified_outcome else "runtime_result",
                evidence_verified=verified_outcome,
                status=self._outcome_status(result),
            )
            return {
                "situation_id": situation_id,
                "correlation_id": outcome.get("correlation_id"),
                "status": outcome.get("status"),
                "decision_id": (
                    decision.get("decision", {}).get("decision_id")
                    if isinstance(decision.get("decision"), dict)
                    else None
                ),
                "outcome_id": (
                    outcome.get("outcome", {}).get("outcome_id")
                    if isinstance(outcome.get("outcome"), dict)
                    else None
                ),
                "shadow_only": True,
            }
        except Exception as exc:
            self._record_error(
                "shadow_awareness_finalize_error",
                event.event_id,
                exc,
                situation_id=situation_id,
            )
            return {
                "situation_id": situation_id or None,
                "status": "degraded",
                "error_type": type(exc).__name__,
                "shadow_only": True,
            }

    @staticmethod
    def _matches_existing_outcome(
        situation: dict[str, Any],
        outcome_value: dict[str, Any],
        *,
        is_fact: bool,
    ) -> bool:
        outcome = (
            situation.get("outcome")
            if isinstance(situation.get("outcome"), dict)
            else {}
        )
        value = outcome.get("value") if isinstance(outcome.get("value"), dict) else {}
        return bool(
            outcome
            and value.get("route") == outcome_value.get("route")
            and value.get("status") == outcome_value.get("status")
            and value.get("verification") == outcome_value.get("verification")
            and bool(outcome.get("is_fact")) is bool(is_fact)
        )

    @staticmethod
    def _situation_summary(situation: dict[str, Any]) -> dict[str, Any]:
        decision = (
            situation.get("decision")
            if isinstance(situation.get("decision"), dict)
            else {}
        )
        outcome = (
            situation.get("outcome")
            if isinstance(situation.get("outcome"), dict)
            else {}
        )
        return {
            "situation_id": situation.get("situation_id"),
            "correlation_id": situation.get("correlation_id"),
            "status": situation.get("status"),
            "decision_id": decision.get("decision_id"),
            "outcome_id": outcome.get("outcome_id"),
            "shadow_only": True,
        }

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
        for _ in range(max(0, min(int(limit), 100))):
            claimed = self.event_inbox.claim(consumer_id, lease_seconds=60)
            if not isinstance(claimed, dict):
                break
            event_id = str(claimed.get("event_id") or "")
            try:
                event = VeyraEvent.from_dict(claimed)
                existing = self._situation_for_event(event)
                situation = existing or self.situation_evaluator.observe(
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
            "status": "success" if not failed else "degraded",
            "processed_count": len(processed),
            "failed_count": len(failed),
            "processed": processed,
            "failed": failed,
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
