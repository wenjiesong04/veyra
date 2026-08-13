from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Callable

from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.event_schema import EventSource, EventType, VeyraEvent
from interface.structured_observation import (
    STRUCTURED_OBSERVATION_CHANNEL,
    STRUCTURED_OBSERVATION_COMMAND_SCHEMA,
    STRUCTURED_OBSERVATION_EVENT_SCHEMA,
    StructuredObservationAuthority,
    ComponentHealthObservationRequest,
    StructuredObservationCommand,
    canonical_digest,
    canonical_utc,
    parse_aware_utc,
)
from memory_bridge.scope import normalize_scope_component
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
from runtime.event_inbox import EVENT_INBOX_FILE


CONTROL_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"
PUBLIC_STATUS_SCHEMA = "veyra.structured_observation.status.v1"
PUBLIC_RESULT_SCHEMA = "veyra.structured_observation.result.v1"

MAX_PAST_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_VALIDITY = timedelta(hours=24)


PRODUCER_POLICIES: dict[str, dict[str, frozenset[str]]] = {
    "commitment_runtime": {
        "fact_kinds": frozenset(
            {"change_signal", "deadline_signal", "progress_signal"}
        ),
        "anchor_kinds": frozenset({"commitment", "goal"}),
        "evidence_sources": frozenset(
            {"derived_rule", "direct_tool_observation"}
        ),
    },
    "component_health": {
        "fact_kinds": frozenset(
            {"availability_signal", "change_signal", "risk_signal"}
        ),
        "anchor_kinds": frozenset({"entity", "goal"}),
        "evidence_sources": frozenset(
            {"derived_rule", "direct_tool_observation"}
        ),
    },
    "local_operator": {
        "fact_kinds": frozenset(
            {
                "availability_signal",
                "change_signal",
                "deadline_signal",
                "progress_signal",
                "risk_signal",
            }
        ),
        "anchor_kinds": frozenset(
            {"goal", "commitment", "case", "task", "trace", "entity"}
        ),
        "evidence_sources": frozenset({"human_verified"}),
    },
    "task_runtime": {
        "fact_kinds": frozenset(
            {"change_signal", "progress_signal", "risk_signal"}
        ),
        "anchor_kinds": frozenset({"task", "goal"}),
        "evidence_sources": frozenset(
            {"derived_rule", "direct_tool_observation"}
        ),
    },
    # Server-issued, read-only workspace observations.  HTTP never accepts
    # this producer identity; TrustedWorkspaceObserver supplies a private
    # object-identity capability to the in-process method below.
    "workspace_observer": {
        "fact_kinds": frozenset({"change_signal", "risk_signal"}),
        "anchor_kinds": frozenset({"goal", "entity"}),
        "evidence_sources": frozenset(
            {"derived_rule", "direct_tool_observation"}
        ),
    },
}


SALIENCE_MAPS: dict[str, dict[str, float]] = {
    "severity": {
        "info": 0.1,
        "low": 0.25,
        "moderate": 0.5,
        "high": 0.75,
        "critical": 1.0,
    },
    "urgency": {
        "none": 0.1,
        "routine": 0.3,
        "soon": 0.65,
        "immediate": 1.0,
    },
    "novelty": {"known": 0.1, "changed": 0.5, "new": 0.9},
    "uncertainty": {"low": 0.1, "medium": 0.5, "high": 0.9},
    "evidence_completeness": {
        "partial": 0.5,
        "corroborated": 0.8,
        "direct": 1.0,
    },
}


class StructuredObservationIngressError(RuntimeError):
    """Base failure for the trusted structured observation ingress."""


class StructuredObservationUnauthorizedError(StructuredObservationIngressError):
    """The explicit local token was absent or invalid."""


class StructuredObservationUnavailableError(StructuredObservationIngressError):
    """The token, workspace, awareness runtime, or private state is unavailable."""


class StructuredObservationConflictError(StructuredObservationIngressError):
    """CAS, operation identity, producer policy, or durable reference conflicted."""


class StructuredObservationIngress:
    """Admit typed evidence into Event Awareness without execution authority.

    The ingress never reads chat text or metadata. Its categorical facts are
    mapped to numeric salience by this Veyra-owned policy, and its exact event
    identity is persisted by EventInbox. The active Event Awareness and
    Suggestion modes remain independent operator controls and are never changed
    here.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        event_awareness: ShadowAwarenessRuntime,
        control_token: str | None = None,
        component_health_snapshot: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.event_awareness = event_awareness
        self.control_token = str(
            os.getenv(CONTROL_TOKEN_ENV, "")
            if control_token is None
            else control_token
        ).strip()
        self.component_health_snapshot = component_health_snapshot
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.BACKGROUND_STATE_FILE = "component_health_background_state.json"
        self._workspace_observer_capability = object()
        self._workspace_observer_publisher_issued = False

    def status(self) -> dict[str, Any]:
        """Pure aggregate readiness; no tenant event or evidence is exposed."""

        inbox = self.state_store.read_json(EVENT_INBOX_FILE)
        config = self.state_store.read_json("ops_config.json")
        storage_ready = self._healthy_inbox(inbox)
        counts: dict[str, int] = {}
        structured_total = 0
        if storage_ready:
            for record in inbox.get("events", {}).values():
                envelope = (
                    record.get("envelope") if isinstance(record, dict) else None
                )
                source = (
                    envelope.get("source")
                    if isinstance(envelope, dict)
                    and isinstance(envelope.get("source"), dict)
                    else {}
                )
                if source.get("channel") != STRUCTURED_OBSERVATION_CHANNEL:
                    continue
                structured_total += 1
                selected = str(record.get("status") or "unknown")
                counts[selected] = counts.get(selected, 0) + 1
        awareness_section = (
            config.get("event_awareness")
            if isinstance(config, dict)
            and isinstance(config.get("event_awareness"), dict)
            else {}
        )
        mode = str(
            awareness_section.get("mode")
            or self.event_awareness.mode
            or "record_only"
        ).strip().lower()
        if mode not in self.event_awareness.MODES:
            mode = "record_only"
        token_ready = bool(self.control_token)
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "status": (
                "available"
                if storage_ready and token_ready
                else "fail_closed"
            ),
            "storage_ready": storage_ready,
            "token_configured": token_ready,
            "loopback_token_bypass_allowed": False,
            "event_awareness_mode": mode,
            "suggestion_mode_changed_by_ingress": False,
            "event_awareness_mode_changed_by_ingress": False,
            "structured_event_count": structured_total,
            "counts": counts,
            "event_inbox_revision": self._revision(inbox),
            "producer_allowlist": sorted(PRODUCER_POLICIES),
            # Producer identities other than local_operator are reserved for
            # future in-process bindings.  A network caller must never be able
            # to self-assert a component/runtime identity in the request body.
            "http_producer_allowlist": ["local_operator"],
            "trusted_producer_capabilities": [
                *(["component_health"] if callable(self.component_health_snapshot) else []),
                *(["workspace_observer"] if self._workspace_observer_publisher_issued else []),
            ],
            "component_health_producer_configured": callable(
                self.component_health_snapshot
            ),
            "numeric_salience_accepted": False,
            "free_text_or_metadata_accepted": False,
            "authority": self._authority(),
        }

    def submit(
        self,
        command: StructuredObservationCommand,
        *,
        control_token: str,
        _internal: bool = False,
        _publisher_capability: object | None = None,
    ) -> dict[str, Any]:
        if not _internal:
            self._authorize(control_token)
        if not isinstance(command, StructuredObservationCommand):
            command = StructuredObservationCommand.model_validate(
                command,
                strict=True,
            )
        if command.producer_id == "workspace_observer" and (
            _publisher_capability is not self._workspace_observer_capability
        ):
            raise StructuredObservationUnauthorizedError(
                "workspace observer capability is required"
            )
        user = normalize_scope_component(command.user_id, "user_id")
        workspace = normalize_scope_component(
            command.workspace_id,
            "workspace_id",
            max_chars=1024,
        )
        session = normalize_scope_component(command.session_id, "session_id")
        self._validate_producer_policy(command)
        durable_workspace = self._validate_workspace(
            workspace,
            producer_id=command.producer_id,
        )
        self._validate_durable_references(
            command,
            user_id=user,
            session_id=session,
            workspace_id=durable_workspace,
        )
        event = self._event(
            command,
            user_id=user,
            workspace_id=workspace,
            session_id=session,
        )
        command_digest = command.command_digest()
        selected: dict[str, Any] = {}

        with self.state_store.writer_transaction():
            before = self.state_store.read_json(EVENT_INBOX_FILE)
            if not self._healthy_inbox(before):
                raise StructuredObservationUnavailableError(
                    "structured observation EventInbox is unavailable"
                )
            existing = before["events"].get(event.event_id)
            if isinstance(existing, dict):
                self._require_exact_replay(
                    existing,
                    event=event,
                    command_digest=command_digest,
                )
                selected = self._public_result(
                    event=event,
                    awareness_result=self._existing_awareness_result(existing),
                    event_inbox=before,
                    operation_replayed=True,
                    durable_event_status=str(existing.get("status") or "unknown"),
                )
            else:
                # Freshness governs first admission, not an exact immutable
                # replay. A server-owned producer may be acknowledging a
                # durable event after a long restart; rejecting that replay
                # solely because its original validity window elapsed would
                # strand its recovery outbox forever. Exact replay below is
                # byte-bound and never creates new evidence.
                self._validate_time_window(command)
                if (
                    self._revision(before)
                    != command.expected_event_inbox_revision
                ):
                    raise StructuredObservationConflictError(
                        "expected EventInbox revision does not match"
                    )
                awareness_result = self.event_awareness.begin(event)
                after = self.state_store.read_json(EVENT_INBOX_FILE)
                record = after.get("events", {}).get(event.event_id)
                durable_status = (
                    str(record.get("status") or "unknown")
                    if isinstance(record, dict)
                    else "not_persisted"
                )
                selected = self._public_result(
                    event=event,
                    awareness_result=awareness_result,
                    event_inbox=after,
                    operation_replayed=False,
                    durable_event_status=durable_status,
                )
        return selected

    def issue_workspace_observer_publisher(
        self,
    ) -> Callable[[StructuredObservationCommand], dict[str, Any]]:
        """Issue one in-process publisher for the server-owned workspace observer.

        The capability is an object identity kept only in this process.  No
        request field, bearer token, or serializable value can mint the
        ``workspace_observer`` producer identity.
        """

        if self._workspace_observer_publisher_issued:
            raise RuntimeError(
                "workspace observer publisher capability was already issued"
            )
        self._workspace_observer_publisher_issued = True
        capability = self._workspace_observer_capability

        def publish(command: StructuredObservationCommand) -> dict[str, Any]:
            if capability is not self._workspace_observer_capability:
                raise StructuredObservationUnauthorizedError(
                    "workspace observer capability is invalid"
                )
            if not isinstance(command, StructuredObservationCommand):
                command = StructuredObservationCommand.model_validate(
                    command,
                    strict=True,
                )
            if command.producer_id != "workspace_observer":
                raise StructuredObservationConflictError(
                    "workspace observer publisher requires workspace_observer producer"
                )
            return self.submit(
                command,
                control_token="",
                _internal=True,
                _publisher_capability=capability,
            )

        return publish

    def submit_component_health(
        self,
        request: ComponentHealthObservationRequest,
        *,
        control_token: str,
        _internal: bool = False,
        _snapshot: dict[str, Any] | None = None,
        _server_now: datetime | None = None,
    ) -> dict[str, Any]:
        """Publish a server-derived health observation under an exact owner.

        The request carries only owner/workspace/session and an EventInbox CAS.
        Component status, severity, evidence id and timestamps come from the
        server callback, so a network caller cannot self-assert health or
        impersonate another typed producer.
        """

        if not _internal:
            self._authorize(control_token)
        if not isinstance(request, ComponentHealthObservationRequest):
            request = ComponentHealthObservationRequest.model_validate(
                request,
                strict=True,
            )
        if not callable(self.component_health_snapshot):
            raise StructuredObservationUnavailableError(
                "component health producer is not configured"
            )
        if _snapshot is not None:
            snapshot = copy.deepcopy(_snapshot)
        else:
            try:
                snapshot = self.component_health_snapshot()
            except Exception as exc:
                raise StructuredObservationUnavailableError(
                    "component health snapshot is unavailable"
                ) from exc
        if not isinstance(snapshot, dict):
            raise StructuredObservationUnavailableError(
                "component health snapshot is invalid"
            )
        status = str(snapshot.get("status") or "").strip().lower()
        if status not in {"healthy", "degraded", "critical"}:
            raise StructuredObservationUnavailableError(
                "component health status is outside the typed producer contract"
            )
        alerts = snapshot.get("alerts")
        if not isinstance(alerts, list) or any(not isinstance(item, dict) for item in alerts):
            raise StructuredObservationUnavailableError(
                "component health alerts are invalid"
            )
        # The request timestamp is schema-validated for transport stability,
        # but it is not trusted as observation time.  A replay reuses the
        # durable first observation timestamp; a new observation uses the
        # server-owned clock.
        now = self._existing_component_health_time(request) or _server_now or self._now()
        snapshot_digest = self._component_health_digest(snapshot)
        if status == "critical":
            state, severity, urgency, novelty, uncertainty = (
                "degraded", "critical", "immediate", "changed", "high"
            )
            evidence_quality = "corroborated"
        elif status == "degraded":
            state, severity, urgency, novelty, uncertainty = (
                "degraded", "high", "soon", "changed", "medium"
            )
            evidence_quality = "corroborated"
        else:
            state, severity, urgency, novelty, uncertainty = (
                "present", "info", "routine", "known", "low"
            )
            evidence_quality = "direct"
        command = StructuredObservationCommand.model_validate(
            {
                "schema_version": STRUCTURED_OBSERVATION_COMMAND_SCHEMA,
                "operation_id": request.operation_id,
                "producer_id": "component_health",
                "producer_receipt_id": "component_health:" + snapshot_digest[:32],
                "user_id": request.user_id,
                "workspace_id": request.workspace_id,
                "session_id": request.session_id,
                "expected_event_inbox_revision": request.expected_event_inbox_revision,
                "occurred_at": canonical_utc(now),
                "valid_until": canonical_utc(now + timedelta(minutes=15)),
                "anchors": [{"kind": "entity", "ref_id": "component:veyra"}],
                "evidence": [
                    {
                        "evidence_id": "component-health:" + snapshot_digest[:32],
                        "source": "derived_rule",
                    }
                ],
                "facts": {
                    "kind": "availability_signal",
                    "state": state,
                    "severity": severity,
                    "urgency": urgency,
                    "novelty": novelty,
                    "uncertainty": uncertainty,
                    "evidence_quality": evidence_quality,
                    "epistemic_status": "observed",
                },
            },
            strict=True,
        )
        return self.submit(
            command,
            control_token=control_token,
            _internal=_internal,
        )

    def publish_component_health_background(
        self,
        *,
        user_id: str,
        session_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Publish one deduplicated server-owned health snapshot.

        This is intentionally an explicit in-process producer.  It is only
        called when the operator opts in through the active-loop wiring and it
        never sends notifications or changes authority.  Unchanged snapshots
        are a pure no-op so a long-lived loop cannot flood EventInbox/Belief.
        """

        if not callable(self.component_health_snapshot):
            raise StructuredObservationUnavailableError(
                "component health producer is not configured"
            )
        try:
            snapshot = self.component_health_snapshot()
        except Exception as exc:
            raise StructuredObservationUnavailableError(
                "component health snapshot is unavailable"
            ) from exc
        if not isinstance(snapshot, dict):
            raise StructuredObservationUnavailableError(
                "component health snapshot is invalid"
            )
        status = str(snapshot.get("status") or "").strip().lower()
        alerts = snapshot.get("alerts")
        if status not in {"healthy", "degraded", "critical"} or not isinstance(alerts, list) or any(
            not isinstance(item, dict) for item in alerts
        ):
            raise StructuredObservationUnavailableError(
                "component health snapshot is outside the typed producer contract"
            )
        snapshot_digest = self._component_health_digest(snapshot)
        previous = self.state_store.read_json(self.BACKGROUND_STATE_FILE)
        if previous.get("snapshot_digest") == snapshot_digest:
            return {
                "status": "unchanged",
                "producer_id": "component_health",
                "snapshot_digest": snapshot_digest,
                "event_id": previous.get("event_id"),
                "authority": self._authority(),
            }
        now = self._now()
        operation_id = f"component-health-background-{snapshot_digest[:32]}"
        request = ComponentHealthObservationRequest.model_validate(
            {
                "schema_version": "veyra.component_health_observation.request.v1",
                "operation_id": operation_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "expected_event_inbox_revision": int(
                    self.status().get("event_inbox_revision") or 0
                ),
                # This is validated but ignored by submit_component_health;
                # the server clock above is authoritative.
                "occurred_at": canonical_utc(now),
            },
            strict=True,
        )
        result = self.submit_component_health(
            request,
            control_token="",
            _internal=True,
            _snapshot=snapshot,
            _server_now=now,
        )
        if str(result.get("status") or "") in {"recorded", "observed", "replayed"}:
            durable_time = self._existing_component_health_time(request) or now
            self.state_store.mutate_json(
                self.BACKGROUND_STATE_FILE,
                lambda state: {
                    **state,
                    "schema_version": "veyra.component_health_background_state.v1",
                    "snapshot_digest": snapshot_digest,
                    "event_id": result.get("event_id"),
                    "observed_at": canonical_utc(durable_time),
                    "updated_at": canonical_utc(self._now()),
                },
            )
        return {
            **result,
            "background": True,
            "snapshot_digest": snapshot_digest,
        }

    @staticmethod
    def _component_health_digest(snapshot: dict[str, Any]) -> str:
        status = str(snapshot.get("status") or "").strip().lower()
        alerts = snapshot.get("alerts") if isinstance(snapshot.get("alerts"), list) else []
        return canonical_digest(
            {
                "status": status,
                "alert_count": len(alerts),
                "alerts": alerts,
                "components": snapshot.get("components")
                if isinstance(snapshot.get("components"), dict)
                else {},
            }
        )

    def _existing_component_health_time(
        self,
        request: ComponentHealthObservationRequest,
    ) -> datetime | None:
        """Find the durable first timestamp for an idempotent operation."""

        inbox = self.state_store.read_json(EVENT_INBOX_FILE)
        events = inbox.get("events") if isinstance(inbox, dict) else None
        if not isinstance(events, dict):
            return None
        for record in events.values():
            envelope = record.get("envelope") if isinstance(record, dict) else None
            if not isinstance(envelope, dict):
                continue
            source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
            payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
            if (
                source.get("channel") != STRUCTURED_OBSERVATION_CHANNEL
                or source.get("user_id") != request.user_id
                or source.get("session_id") != request.session_id
                or payload.get("producer_id") != "component_health"
                or payload.get("operation_id") != request.operation_id
                or payload.get("workspace_id") != request.workspace_id
            ):
                continue
            raw = payload.get("valid_from") or envelope.get("occurred_at")
            try:
                return parse_aware_utc(raw) if isinstance(raw, str) else None
            except (TypeError, ValueError):
                return None
        return None

    def _event(
        self,
        command: StructuredObservationCommand,
        *,
        user_id: str,
        session_id: str,
        workspace_id: str,
    ) -> VeyraEvent:
        command_digest = command.command_digest()
        event_id = "sob_" + hashlib.sha256(
            json.dumps(
                {
                    "schema": STRUCTURED_OBSERVATION_EVENT_SCHEMA,
                    "producer_id": command.producer_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "operation_id": command.operation_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        anchors = [item.model_dump(mode="json") for item in command.anchors]
        evidence = [
            {
                "ref_id": item.evidence_id,
                "source": item.source,
                "epistemic_status": "observation",
                "is_fact": False,
            }
            for item in command.evidence
        ]
        salience = self._salience(command)
        payload = {
            "schema_version": STRUCTURED_OBSERVATION_EVENT_SCHEMA,
            "workspace_id": workspace_id,
            "structured_anchor_refs": anchors,
            "evidence_refs": copy.deepcopy(evidence),
            "salience_components": salience,
            "observation": {
                "schema_version": "veyra.structured_observation.fact.v1",
                "producer_id": command.producer_id,
                "producer_receipt_id": command.producer_receipt_id,
                "fact_kind": command.facts.kind,
                "fact_state": command.facts.state,
                "categorical_facts": {
                    "severity": command.facts.severity,
                    "urgency": command.facts.urgency,
                    "novelty": command.facts.novelty,
                    "uncertainty": command.facts.uncertainty,
                    "evidence_quality": command.facts.evidence_quality,
                },
                "epistemic_status": command.facts.epistemic_status,
                # Derived here, never accepted from the caller: only a direct
                # observation is a candidate fact. An inference or a prediction
                # may be recorded, but it cannot present itself as one.
                "is_fact": command.facts.epistemic_status == "observed",
                "evidence_count": len(evidence),
                "payload_claims_verified": False,
                "fact_certified": False,
            },
            "operation_id": command.operation_id,
            "producer_id": command.producer_id,
            "producer_receipt_id": command.producer_receipt_id,
            "command_digest": command_digest,
            "valid_from": command.occurred_at,
            "valid_until": command.valid_until,
            "authority": self._authority(),
        }
        return VeyraEvent(
            type=EventType.OBSERVATION,
            source=EventSource(
                channel=STRUCTURED_OBSERVATION_CHANNEL,
                user_id=user_id,
                session_id=session_id,
            ),
            payload=payload,
            event_id=event_id,
            timestamp=command.occurred_at,
            correlation_id=event_id,
            subject=copy.deepcopy(anchors),
            evidence_refs=copy.deepcopy(evidence),
            dedupe_key=(
                f"{command.producer_id}:{command.operation_id}:"
                f"{tenant_scope_storage_key(user_id, session_id)}:"
                f"{hashlib.sha256(workspace_id.encode('utf-8')).hexdigest()}"
            ),
            occurred_at=command.occurred_at,
            received_at=canonical_utc(self._now()),
            privacy_scope={
                "kind": "owner_workspace_session",
                "workspace_id": workspace_id,
            },
        )

    def _salience(
        self,
        command: StructuredObservationCommand,
    ) -> dict[str, float]:
        return {
            "severity": SALIENCE_MAPS["severity"][command.facts.severity],
            "urgency": SALIENCE_MAPS["urgency"][command.facts.urgency],
            "novelty": SALIENCE_MAPS["novelty"][command.facts.novelty],
            "uncertainty": SALIENCE_MAPS["uncertainty"][
                command.facts.uncertainty
            ],
            "evidence_completeness": SALIENCE_MAPS[
                "evidence_completeness"
            ][command.facts.evidence_quality],
        }

    def _validate_producer_policy(
        self,
        command: StructuredObservationCommand,
    ) -> None:
        policy = PRODUCER_POLICIES.get(command.producer_id)
        anchor_kinds = {item.kind for item in command.anchors}
        evidence_sources = {item.source for item in command.evidence}
        if (
            not isinstance(policy, dict)
            or command.facts.kind not in policy["fact_kinds"]
            or not anchor_kinds.intersection(policy["anchor_kinds"])
            or not evidence_sources
            or not evidence_sources.issubset(policy["evidence_sources"])
        ):
            raise StructuredObservationConflictError(
                "structured observation producer policy rejected the command"
            )

    def _validate_workspace(
        self,
        workspace_id: str,
        *,
        producer_id: str,
    ) -> str:
        """Bind an event-safe workspace ref to the server's current project.

        Ordinary private producers retain the existing exact-path contract.
        The in-process workspace observer instead persists only an opaque
        digest.  Its object capability was already checked in ``submit``;
        resolving that digest here lets Goal validation still use the exact
        durable workspace without leaking a local path into EventInbox,
        GeneralSituation, Attention, or Console projections.
        """

        local_world = self.state_store.read_json("local_world.json")
        if local_world.get("_state_corrupt") is True:
            raise StructuredObservationUnavailableError(
                "local workspace state is unavailable"
            )
        current = str(local_world.get("current_project") or "").strip()
        if not current:
            raise StructuredObservationConflictError(
                "structured observation workspace is not current"
            )
        expected = current
        durable_current = current
        if producer_id == "workspace_observer":
            try:
                current_path = Path(current).expanduser().resolve(strict=True)
            except OSError as exc:
                raise StructuredObservationUnavailableError(
                    "local workspace state is unavailable"
                ) from exc
            if not current_path.is_dir():
                raise StructuredObservationUnavailableError(
                    "local workspace state is unavailable"
                )
            durable_current = str(current_path)
            expected = (
                "workspace:"
                + hashlib.sha256(durable_current.encode("utf-8")).hexdigest()
            )
        if workspace_id != expected:
            raise StructuredObservationConflictError(
                "structured observation workspace is not current"
            )
        return durable_current

    def _validate_durable_references(
        self,
        command: StructuredObservationCommand,
        *,
        user_id: str,
        session_id: str,
        workspace_id: str,
    ) -> None:
        goal_ids = {
            item.ref_id for item in command.anchors if item.kind == "goal"
        }
        commitment_ids = {
            item.ref_id
            for item in command.anchors
            if item.kind == "commitment"
        }
        if goal_ids:
            goals_state = self.state_store.read_json("user_goals.json")
            if goals_state.get("_state_corrupt") is True:
                raise StructuredObservationUnavailableError(
                    "Goal state is unavailable"
                )
            goals = (
                goals_state.get("goals")
                if isinstance(goals_state.get("goals"), list)
                else []
            )
            for goal_id in goal_ids:
                matches = [
                    item
                    for item in goals
                    if isinstance(item, dict)
                    and str(item.get("goal_id") or "") == goal_id
                    and str(item.get("user_id") or "") == user_id
                    and str(item.get("status") or "").strip().lower()
                    == "active"
                    and self._workspace_matches(item, workspace_id)
                    and self._goal_session_matches(item, session_id)
                ]
                if len(matches) != 1:
                    raise StructuredObservationConflictError(
                        "structured observation Goal is not active for owner"
                    )
        if commitment_ids:
            commitments_state = self.state_store.read_json(
                "user_commitments.json"
            )
            if commitments_state.get("_state_corrupt") is True:
                raise StructuredObservationUnavailableError(
                    "Commitment state is unavailable"
                )
            commitments = (
                commitments_state.get("commitments")
                if isinstance(commitments_state.get("commitments"), list)
                else []
            )
            for commitment_id in commitment_ids:
                matches = [
                    item
                    for item in commitments
                    if isinstance(item, dict)
                    and str(item.get("commitment_id") or "") == commitment_id
                    and str(item.get("user_id") or "") == user_id
                    and str(item.get("status") or "").strip().lower()
                    == "active"
                    and self._workspace_matches(item, workspace_id)
                ]
                if len(matches) != 1:
                    raise StructuredObservationConflictError(
                        "structured observation Commitment is not active for owner"
                    )

    @staticmethod
    def _workspace_matches(record: dict[str, Any], workspace_id: str) -> bool:
        scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
        bound = str(
            record.get("workspace_ref")
            or record.get("workspace_id")
            or scope.get("workspace_id")
            or ""
        ).strip()
        opaque = (
            "workspace:"
            + hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
        )
        return not bound or bound in {workspace_id, opaque}

    @staticmethod
    def _goal_session_matches(record: dict[str, Any], session_id: str) -> bool:
        """Require exact session only for the session-bound workspace Goal."""

        if (
            str(record.get("schema_version") or "")
            != "veyra.workspace_goal.v1"
            or str(record.get("kind") or "") != "workspace_observation"
            or str(record.get("source") or "") != "workspace_goal_control"
        ):
            return True
        scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
        return str(record.get("session_id") or scope.get("session_id") or "") == session_id

    def _validate_time_window(
        self,
        command: StructuredObservationCommand,
    ) -> None:
        now = self._now()
        occurred = parse_aware_utc(command.occurred_at)
        valid_until = parse_aware_utc(command.valid_until)
        if (
            occurred < now - MAX_PAST_AGE
            or occurred > now + MAX_FUTURE_SKEW
            or valid_until < now
            or valid_until - occurred > MAX_VALIDITY
        ):
            raise StructuredObservationConflictError(
                "structured observation time window is not admissible"
            )

    def _require_exact_replay(
        self,
        record: dict[str, Any],
        *,
        event: VeyraEvent,
        command_digest: str,
    ) -> None:
        envelope = record.get("envelope")
        payload = (
            envelope.get("payload")
            if isinstance(envelope, dict)
            and isinstance(envelope.get("payload"), dict)
            else {}
        )
        source = (
            envelope.get("source")
            if isinstance(envelope, dict)
            and isinstance(envelope.get("source"), dict)
            else {}
        )
        expected_source = event.to_dict()["source"]
        if (
            str(payload.get("command_digest") or "") != command_digest
            or source != expected_source
            or str(envelope.get("dedupe_key") or "") != event.dedupe_key
            or str(envelope.get("event_id") or "") != event.event_id
        ):
            raise StructuredObservationConflictError(
                "structured observation operation identity was rebound"
            )

    @staticmethod
    def _existing_awareness_result(record: dict[str, Any]) -> dict[str, Any]:
        completion = record.get("completion_result")
        if isinstance(completion, dict):
            return copy.deepcopy(completion)
        return {
            "status": (
                "recorded"
                if str(record.get("status") or "") == "pending"
                else str(record.get("status") or "unknown")
            )
        }

    def _public_result(
        self,
        *,
        event: VeyraEvent,
        awareness_result: dict[str, Any],
        event_inbox: dict[str, Any],
        operation_replayed: bool,
        durable_event_status: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": PUBLIC_RESULT_SCHEMA,
            "status": (
                "replayed"
                if operation_replayed
                else str(awareness_result.get("status") or "degraded")
            ),
            "event_id": event.event_id,
            "event_type": EventType.OBSERVATION.value,
            "producer_id": event.payload["producer_id"],
            "durable_event_status": durable_event_status,
            "event_inbox_revision": self._revision(event_inbox),
            "situation_id": awareness_result.get("situation_id"),
            "operation_replayed": operation_replayed,
            "server_salience_mapping": "veyra.structured_salience.enums.v1",
            "numeric_salience_accepted": False,
            "free_text_or_metadata_accepted": False,
            "event_awareness_mode_changed": False,
            "suggestion_mode_changed": False,
            "authority": self._authority(),
        }

    def _authorize(self, supplied_token: str) -> None:
        if not self.control_token:
            raise StructuredObservationUnavailableError(
                "VEYRA_LOCAL_API_TOKEN is not configured"
            )
        selected = str(supplied_token or "").strip()
        if not selected or not hmac.compare_digest(
            selected,
            self.control_token,
        ):
            raise StructuredObservationUnauthorizedError(
                "valid Veyra control token is required"
            )

    @staticmethod
    def _healthy_inbox(value: dict[str, Any]) -> bool:
        return bool(
            isinstance(value, dict)
            and value.get("_state_corrupt") is not True
            and value.get("schema_version") == "veyra.event_inbox.v1"
            and isinstance(value.get("events"), dict)
            and isinstance(value.get("dedupe_index"), dict)
        )

    @staticmethod
    def _revision(value: dict[str, Any]) -> int:
        raw = value.get("_state_revision") if isinstance(value, dict) else 0
        if isinstance(raw, bool):
            return 0
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _authority() -> dict[str, bool]:
        return StructuredObservationAuthority().model_dump(mode="json")

    def _now(self) -> datetime:
        selected = self._clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise StructuredObservationUnavailableError(
                "structured observation clock is not timezone-aware"
            )
        return selected.astimezone(timezone.utc)


__all__ = [
    "PRODUCER_POLICIES",
    "SALIENCE_MAPS",
    "StructuredObservationConflictError",
    "StructuredObservationIngress",
    "StructuredObservationIngressError",
    "StructuredObservationUnauthorizedError",
    "StructuredObservationUnavailableError",
]
