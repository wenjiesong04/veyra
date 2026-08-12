"""Durable delivery helpers for :mod:`trusted_workspace_observer`.

The service owns binding and decision policy.  This module owns the narrow
outbox protocol that prepares an immutable transition, admits typed commands
through the in-process publisher, and acknowledges each exact EventInbox
receipt under the state-root writer fence.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta
from typing import Any

from interface.structured_observation import (
    StructuredObservationAnchor,
    StructuredObservationCommand,
    StructuredObservationEvidence,
    StructuredObservationFacts,
    canonical_utc,
)
from runtime.isolated_git_snapshot import GitWorkspaceObservation
from runtime.trusted_workspace_observer_state import (
    MAX_VALIDITY_SECONDS,
    STATE_FILE,
    copy_pending,
    digest,
    snapshot_from_record,
    validate_pending_delivery,
)


def _conflict(message: str) -> Exception:
    # Import lazily: the service imports this module from its method bodies so
    # the concrete public exception remains the service's single API type.
    from runtime.trusted_workspace_observer import TrustedWorkspaceObserverConflict

    return TrustedWorkspaceObserverConflict(message)


def publish_events(
    observer: Any,
    *,
    binding: dict[str, Any],
    workspace: str,
    snapshot: GitWorkspaceObservation,
    ci_fact: dict[str, Any] | None,
    now: datetime,
    subject: str,
    kinds: tuple[str, ...],
    receipt_suffix: str,
) -> list[dict[str, Any]]:
    """Admit one transition under a single state-root writer fence."""

    with observer.state_store.writer_transaction():
        observer._assert_run_binding(
            binding=binding,
            workspace=workspace,
            expected_generation=int(binding["binding_generation"]),
        )
        return _publish_events_locked(
            observer,
            binding=binding,
            workspace=workspace,
            snapshot=snapshot,
            ci_fact=ci_fact,
            now=now,
            subject=subject,
            kinds=kinds,
            receipt_suffix=receipt_suffix,
        )


def recover_pending(
    observer: Any,
    *,
    binding: dict[str, Any],
    workspace: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Retry only the frozen pending transition, never the current snapshot.

    The caller has already pinned the workspace and validated the bound Goal.
    This function validates the pending record and exact binding before any
    ingress call.  The regular run loop returns immediately after this call;
    a later tick is responsible for classifying a new workspace subject.
    """

    pending = observer.state_store.read_json(STATE_FILE).get("pending_delivery")
    if pending is None:
        return []
    validate_pending_delivery(pending)
    if (
        str(pending.get("workspace_digest") or "") != str(binding.get("workspace_digest") or "")
        or int(pending.get("binding_generation") or 0) != int(binding.get("binding_generation") or 0)
    ):
        raise _conflict("pending observation delivery target changed")
    snapshot = snapshot_from_record(pending["snapshot_record"])
    return publish_events(
        observer,
        binding=binding,
        workspace=workspace,
        snapshot=snapshot,
        ci_fact=copy.deepcopy(pending.get("ci_fact")),
        now=now,
        subject=str(pending["subject"]),
        kinds=tuple(pending["required_kinds"]),
        receipt_suffix=str(pending["receipt_suffix"]),
    )


def _publish_events_locked(
    observer: Any,
    *,
    binding: dict[str, Any],
    workspace: str,
    snapshot: GitWorkspaceObservation,
    ci_fact: dict[str, Any] | None,
    now: datetime,
    subject: str,
    kinds: tuple[str, ...],
    receipt_suffix: str,
) -> list[dict[str, Any]]:
    pending = observer.state_store.read_json(STATE_FILE).get("pending_delivery")
    if pending is not None:
        pending = copy_pending(pending)
        if (
            str(pending.get("workspace_digest") or "") != str(binding.get("workspace_digest") or "")
            or int(pending.get("binding_generation") or 0) != int(binding.get("binding_generation") or 0)
        ):
            raise _conflict("pending observation delivery target changed")
        # A direct classification can still reach this path after a legacy
        # restart.  Always finish the immutable old transition first.
        snapshot = snapshot_from_record(pending["snapshot_record"])
        ci_fact = copy.deepcopy(pending.get("ci_fact"))
        subject = str(pending["subject"])
        kinds = tuple(pending["required_kinds"])
        receipt_suffix = str(pending["receipt_suffix"])
        occurred = str(pending["occurred_at"])
        valid_until = str(pending["valid_until"])
        # A durable state flag cannot attest its own delivery.  Reconstruct
        # acknowledgement from the exact EventInbox envelope on every
        # recovery, including within the original validity window.  This
        # prevents a forged or partially written outbox row from suppressing
        # the event it claims to have delivered.
        acknowledged = _pending_durable_kinds(
            observer,
            pending=pending,
            binding=binding,
            kinds=kinds,
        )
        if sorted(acknowledged) != sorted(pending.get("acknowledged_kinds") or []):
            pending["acknowledged_kinds"] = sorted(acknowledged)
            observer.state_store.mutate_json(
                STATE_FILE,
                lambda state: _set_pending(state, pending),
            )
        if _pending_expired(valid_until, now):
            if len(acknowledged) < len(kinds):
                epoch = int(pending.get("delivery_epoch") or 0) + 1
                receipt_suffix = f"{receipt_suffix}:retry{epoch}"
                occurred = canonical_utc(now)
                valid_until = canonical_utc(now + timedelta(seconds=MAX_VALIDITY_SECONDS))
                pending.update(
                    delivery_epoch=epoch,
                    receipt_suffix=receipt_suffix,
                    occurred_at=occurred,
                    valid_until=valid_until,
                    acknowledged_kinds=sorted(acknowledged),
                )
                observer.state_store.mutate_json(
                    STATE_FILE,
                    lambda state: _set_pending(state, pending),
                )
    else:
        occurred = canonical_utc(now)
        valid_until = canonical_utc(now + timedelta(seconds=MAX_VALIDITY_SECONDS))
        acknowledged = set()
        pending = {
            "subject": subject,
            "workspace_digest": str(binding["workspace_digest"]),
            "binding_generation": int(binding["binding_generation"]),
            "occurred_at": occurred,
            "valid_until": valid_until,
            "required_kinds": list(kinds),
            "acknowledged_kinds": [],
            "receipt_suffix": receipt_suffix,
            "snapshot_record": observer._observation_record(snapshot, subject, ci_fact),
            "ci_fact": copy.deepcopy(ci_fact),
            "delivery_epoch": 0,
        }
        validate_pending_delivery(pending)
        observer.state_store.mutate_json(
            STATE_FILE,
            lambda state: _set_pending(state, pending),
        )

    results: list[dict[str, Any]] = []
    for kind in kinds:
        if kind in acknowledged:
            results.append({"status": "replayed", "reason": "pending_acknowledged"})
            continue
        operation = f"{observer.PRODUCER_ID}:{binding['binding_generation']}:{subject}:{receipt_suffix}:{kind}"
        command = _command(observer, binding, snapshot, ci_fact, now, subject, kind, operation, occurred, valid_until)
        try:
            published = observer.publish_observation(command)
            result = published if isinstance(published, dict) else {"status": "degraded", "reason": "invalid_ingress_result"}
            results.append(result)
            if str(result.get("status") or "") in {"recorded", "enqueued", "replayed"}:
                # Detect a configure/generation race after external ingress.
                # The event is already durable, so report its actual count and
                # leave the pending record for a correctly scoped recovery.
                try:
                    observer._assert_generation(expected_generation=int(binding["binding_generation"]))
                except Exception as exc:
                    # Preserve the successful admission as the one delivery
                    # attempt, and attach the post-admission race as typed
                    # metadata.  A synthetic second "event" here would make
                    # attempted/published counters lie.
                    results[-1] = {
                        **result,
                        "post_publish_reason": "published_before_binding_change",
                        "error_type": type(exc).__name__,
                    }
                    return results
                observer.state_store.mutate_json(
                    STATE_FILE,
                    lambda state: _ack_pending(state, kind),
                )
        except Exception as exc:
            # A generation conflict raised before ingress is not a delivery
            # result; preserve it for the service's fail-closed path.
            from runtime.trusted_workspace_observer import TrustedWorkspaceObserverConflict

            if isinstance(exc, TrustedWorkspaceObserverConflict):
                raise
            results.append(
                {
                    "status": "degraded",
                    "reason": "observation_ingress_failed",
                    "error_type": type(exc).__name__,
                }
            )
    return results


def _command(
    observer: Any,
    binding: dict[str, Any],
    snapshot: GitWorkspaceObservation,
    ci_fact: dict[str, Any] | None,
    now: datetime,
    subject: str,
    kind: str,
    operation: str,
    occurred: str,
    valid_until: str,
) -> StructuredObservationCommand:
    evidence = [
        StructuredObservationEvidence(
            evidence_id="git:" + str(snapshot.manifest_digest)[:48],
            source="direct_tool_observation",
        )
    ]
    if ci_fact is not None:
        evidence.append(
            StructuredObservationEvidence(
                evidence_id="ci:" + str(ci_fact["target_sha"]) + ":" + str(ci_fact["probe_digest"]),
                source="direct_tool_observation",
            )
        )
    return StructuredObservationCommand(
        schema_version="veyra.structured_observation.command.v1",
        operation_id=operation,
        producer_id=observer.PRODUCER_ID,
        producer_receipt_id="wobs:" + digest(operation)[:32],
        user_id=str(binding["user_id"]),
        workspace_id="workspace:" + str(binding["workspace_digest"]),
        session_id=str(binding["session_id"]),
        expected_event_inbox_revision=int(observer._event_inbox_revision()),
        occurred_at=occurred,
        valid_until=valid_until,
        anchors=[
            StructuredObservationAnchor(kind="goal", ref_id=str(binding["goal_id"])),
            StructuredObservationAnchor(
                kind="entity", ref_id="workspace:" + str(binding["workspace_digest"])[:48]
            ),
        ],
        evidence=evidence,
        facts=StructuredObservationFacts(
            kind=kind,
            state="degraded" if kind == "risk_signal" else "changed",
            severity="high" if kind == "risk_signal" else "moderate",
            urgency="soon",
            novelty="new" if kind == "change_signal" else "changed",
            uncertainty="medium" if kind == "risk_signal" else "low",
            evidence_quality="corroborated" if ci_fact else "direct",
            epistemic_status="observed",
        ),
    )


def _pending_expired(valid_until: str, now: datetime) -> bool:
    try:
        expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return now >= expiry.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return True


def _pending_durable_kinds(
    observer: Any,
    *,
    pending: dict[str, Any],
    binding: dict[str, Any],
    kinds: tuple[str, ...],
) -> set[str]:
    from runtime.event_inbox import EventInbox

    inbox = observer.state_store.read_json("event_inbox.json")
    events = inbox.get("events") if isinstance(inbox.get("events"), dict) else {}
    subject = str(pending.get("subject") or "")
    suffix = str(pending.get("receipt_suffix") or "")
    durable: set[str] = set()
    for kind in kinds:
        operation = f"{observer.PRODUCER_ID}:{binding['binding_generation']}:{subject}:{suffix}:{kind}"
        expected_receipt = "wobs:" + digest(operation)[:32]
        for record in events.values():
            if not isinstance(record, dict):
                continue
            envelope = record.get("envelope")
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict):
                continue
            immutable_fingerprint = str(record.get("immutable_fingerprint") or "")
            if (
                not immutable_fingerprint
                or EventInbox._immutable_fingerprint(envelope) != immutable_fingerprint
            ):
                continue
            if (
                str(payload.get("producer_id") or "") == observer.PRODUCER_ID
                and str(payload.get("operation_id") or "") == operation
                and str(payload.get("producer_receipt_id") or "") == expected_receipt
                and str(payload.get("workspace_id") or "") == "workspace:" + str(binding["workspace_digest"])
            ):
                durable.add(kind)
                break
    return durable


def _set_pending(state: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    state["pending_delivery"] = copy.deepcopy(pending)
    return state


def _ack_pending(state: dict[str, Any], kind: str) -> dict[str, Any]:
    pending = state.get("pending_delivery")
    if isinstance(pending, dict):
        acknowledged = list(pending.get("acknowledged_kinds") or [])
        if kind not in acknowledged:
            acknowledged.append(kind)
        pending["acknowledged_kinds"] = acknowledged
    return state


__all__ = ["publish_events", "recover_pending"]
