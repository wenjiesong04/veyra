from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from core.action_risk import RISK_ORDER
from core.world_state import WorldStateStore
from tool_proxy.governance_contract import (
    AuthoritativeToolReceipt,
    CapabilityGrant,
    GovernedSessionBinding,
    PreflightDecision,
    ToolInvocation,
    ToolObservation,
    RiskLevelValue,
    canonical_sha256,
    secret_sha256,
)
from tool_proxy.invocation_risk import invocation_risk_floor


STATE_FILE = "tool_governance_state.json"
STATE_SCHEMA_VERSION = "veyra.tool_governance_state.v1"
CONTRACT_PHASE = "contract_only"
MAX_OBSERVATION_CLOCK_SKEW = timedelta(minutes=5)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class ToolGovernanceError(RuntimeError):
    """Base error for the private Phase 3 tool-governance boundary."""


class ToolGovernanceConflict(ToolGovernanceError):
    """Raised when a replay or contradictory observation is attempted."""


class ToolGovernanceStorageError(ToolGovernanceError):
    """Raised when the authoritative private ledger cannot be trusted."""


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    grant: CapabilityGrant
    capability_token: str


@dataclass(frozen=True, slots=True)
class PreflightEnvelope:
    decision: PreflightDecision
    reservation_token: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.model_dump(mode="json"),
            # The token is returned only to the single preflight caller. It is
            # never part of the persisted contract or audit record.
            "reservation_token": self.reservation_token,
        }


class ToolGovernanceRuntime:
    """Atomic, deny-only CapabilityGrant and pre/post tool ledger.

    This first Phase 3 slice deliberately cannot authorize a real executor:
    every :class:`PreflightDecision` fixes ``execute_allowed`` to ``False``.
    ``would_allow`` means only that the exact one-use Grant was atomically
    reserved and can be exercised by a future, separately validated hook
    integration.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def register_session(
        self,
        binding: GovernedSessionBinding,
    ) -> GovernedSessionBinding:
        payload = binding.model_dump(mode="json")

        def register(document: dict[str, Any]) -> None:
            self._validate_document(document)
            sessions = self._mapping(document, "sessions")
            existing = sessions.get(binding.binding_digest)
            if isinstance(existing, dict):
                stored = self._load_model(
                    GovernedSessionBinding,
                    existing.get("binding"),
                    label="governed session",
                )
                if stored != binding:
                    raise ToolGovernanceConflict(
                        "binding digest is already registered to another scope"
                    )
                if existing.get("status") == "cancelled":
                    raise ToolGovernanceConflict(
                        "cancelled governed sessions cannot be reactivated"
                    )
                return
            sessions[binding.binding_digest] = {
                "binding": payload,
                "status": "active",
                "registered_at": self._now().isoformat(),
                "cancelled_at": None,
            }

        self.state_store.mutate_json(STATE_FILE, register)
        self._audit(
            "session_registered",
            {
                "binding_digest": binding.binding_digest,
                "run_id": binding.run_id,
            },
        )
        return binding

    def issue_grant(
        self,
        invocation: ToolInvocation,
        *,
        approval_id: str,
        approval_revision: str,
        policy_revision: str,
        registry_revision: str,
        risk_level: RiskLevelValue,
        expires_at: datetime | None = None,
        not_before: datetime | None = None,
        max_uses: int = 1,
    ) -> IssuedCapability:
        """Issue an internal exact-scope Grant without exposing an HTTP issuer."""

        if max_uses != 1:
            raise ValueError("contract-only grants are single-use")
        risk_floor = invocation_risk_floor(invocation)
        if risk_floor == "R5" or risk_level == "R5":
            raise ValueError(
                "blocked or structurally inconsistent invocations cannot receive a grant"
            )
        if risk_level not in RISK_ORDER:
            raise ValueError("risk_level is invalid")
        if RISK_ORDER.index(risk_level) < RISK_ORDER.index(risk_floor):
            raise ValueError(
                "risk_level cannot be lower than the deterministic invocation "
                f"floor ({risk_floor})"
            )
        now = self._now()
        start = not_before or now
        expiry = expires_at or (now + timedelta(minutes=5))
        capability_token = secrets.token_urlsafe(32)
        grant = CapabilityGrant.issue(
            grant_id=f"grant_{uuid4().hex}",
            capability_token=capability_token,
            invocation=invocation,
            issued_at=now,
            not_before=start,
            expires_at=expiry,
            max_uses=max_uses,
            risk_level=risk_level,
            approval_id=approval_id,
            approval_revision=approval_revision,
            policy_revision=policy_revision,
            registry_revision=registry_revision,
        )

        def persist(document: dict[str, Any]) -> None:
            self._validate_document(document)
            sessions = self._mapping(document, "sessions")
            registered = sessions.get(invocation.binding.binding_digest)
            if not isinstance(registered, dict):
                raise ToolGovernanceConflict(
                    "governed session must be registered before grant issuance"
                )
            stored_binding = self._load_model(
                GovernedSessionBinding,
                registered.get("binding"),
                label="governed session",
            )
            if (
                stored_binding != invocation.binding
                or registered.get("status") != "active"
            ):
                raise ToolGovernanceConflict(
                    "grant scope does not match an active governed session"
                )

            grants = self._mapping(document, "grants")
            for entry in grants.values():
                if not isinstance(entry, dict):
                    raise ToolGovernanceStorageError(
                        "grant ledger contains a malformed entry"
                    )
                existing = self._load_model(
                    CapabilityGrant,
                    entry.get("grant"),
                    label="capability grant",
                )
                if (
                    existing.invocation_digest == invocation.invocation_digest
                    and str(entry.get("status") or "") in {"active", "consumed"}
                ):
                    raise ToolGovernanceConflict(
                        "an exact-scope grant already exists for this invocation"
                    )

            token_index = self._mapping(document, "grant_token_index")
            if grant.capability_token_digest in token_index:
                raise ToolGovernanceConflict("capability token digest collision")
            grants[grant.grant_id] = {
                "grant": grant.model_dump(mode="json"),
                "status": "active",
                "uses_reserved": 0,
                "revoked_at": None,
                "revocation_reason": None,
            }
            token_index[grant.capability_token_digest] = grant.grant_id

        self.state_store.mutate_json(STATE_FILE, persist)
        self._audit(
            "grant_issued",
            {
                "grant_id": grant.grant_id,
                "grant_digest": grant.grant_digest,
                "invocation_digest": grant.invocation_digest,
                "risk_level": grant.risk_level,
                "risk_floor": risk_floor,
            },
        )
        return IssuedCapability(grant=grant, capability_token=capability_token)

    def preflight(
        self,
        invocation: ToolInvocation,
        *,
        capability_token: str,
    ) -> PreflightEnvelope:
        now = self._now()
        try:
            token_digest = secret_sha256(capability_token)
        except ValueError:
            return self._blocked(invocation, now, "invalid_capability_token")

        reservation_id = f"reservation_{uuid4().hex}"
        reservation_token = secrets.token_urlsafe(32)
        reservation_token_digest = secret_sha256(reservation_token)
        selected: dict[str, PreflightDecision] = {}
        claim_won = False

        def reserve(document: dict[str, Any]) -> None:
            nonlocal claim_won
            self._validate_document(document)
            token_index = self._mapping(document, "grant_token_index")
            grant_id = token_index.get(token_digest)
            if not isinstance(grant_id, str) or not grant_id:
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="grant_not_found",
                )
                return

            grants = self._mapping(document, "grants")
            entry = grants.get(grant_id)
            if not isinstance(entry, dict):
                raise ToolGovernanceStorageError(
                    "capability token index points to a missing grant"
                )
            grant = self._load_model(
                CapabilityGrant,
                entry.get("grant"),
                label="capability grant",
            )
            status = str(entry.get("status") or "")
            if status == "revoked":
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="revoked",
                    reason="grant_revoked",
                )
                return
            if status in {"expired"} or now >= grant.expires_at:
                entry["status"] = "expired"
                entry["expired_at"] = now.isoformat()
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="expired",
                    reason="grant_expired",
                )
                return
            if now < grant.not_before:
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="grant_not_yet_valid",
                )
                return
            if status != "active":
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="grant_already_consumed",
                )
                return
            if not grant.matches(invocation.binding, invocation):
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="grant_scope_or_invocation_mismatch",
                )
                return

            sessions = self._mapping(document, "sessions")
            session = sessions.get(invocation.binding.binding_digest)
            if not isinstance(session, dict) or session.get("status") != "active":
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="revoked",
                    reason="governed_session_cancelled_or_missing",
                )
                return
            stored_binding = self._load_model(
                GovernedSessionBinding,
                session.get("binding"),
                label="governed session",
            )
            if stored_binding != invocation.binding:
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="governed_session_scope_mismatch",
                )
                return

            uses_reserved = int(entry.get("uses_reserved") or 0)
            if uses_reserved >= grant.max_uses:
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="grant_use_limit_exhausted",
                )
                return
            call_key = self._call_key(
                invocation.binding.run_id,
                invocation.tool_call_id,
            )
            call_index = self._mapping(document, "run_call_index")
            if call_key in call_index:
                selected["decision"] = self._decision(
                    invocation,
                    now,
                    outcome="block",
                    ledger_state="blocked",
                    reason="tool_call_replay",
                )
                return

            decision = self._decision(
                invocation,
                now,
                outcome="would_allow",
                ledger_state="authorized_not_observed",
                reason="exact_single_use_grant_reserved_contract_only",
                grant_id=grant.grant_id,
                reservation_id=reservation_id,
                reservation_token_digest=reservation_token_digest,
            )
            receipt = AuthoritativeToolReceipt.create(
                receipt_id=f"receipt_{uuid4().hex}",
                run_id=invocation.binding.run_id,
                tool_call_id=invocation.tool_call_id,
                grant_id=grant.grant_id,
                reservation_id=reservation_id,
                tool_name=grant.tool_name,
                tool_kind=grant.tool_kind,
                risk_level=grant.risk_level,
                args_digest=grant.args_digest,
                targets_digest=grant.targets_digest,
                environment_digest=grant.environment_digest,
                invocation_digest=invocation.invocation_digest,
                grant_digest=grant.grant_digest,
                ledger_state="authorized_not_observed",
                approval_verified=False,
                approval_id=grant.approval_id,
                approval_revision=grant.approval_revision,
                policy_revision=grant.policy_revision,
                registry_revision=grant.registry_revision,
                reserved_at=now,
                observed_at=None,
                outcome=None,
                result_digest=None,
            )
            calls = self._mapping(document, "calls")
            calls[reservation_id] = {
                "receipt": receipt.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "reservation_token_digest": reservation_token_digest,
                "observation": None,
            }
            call_index[call_key] = reservation_id
            entry["uses_reserved"] = uses_reserved + 1
            if entry["uses_reserved"] >= grant.max_uses:
                entry["status"] = "consumed"
            selected["decision"] = decision
            claim_won = True

        self.state_store.mutate_json(STATE_FILE, reserve)
        decision = selected.get("decision")
        if decision is None:
            raise ToolGovernanceStorageError(
                "preflight mutation did not produce a decision"
            )
        self._audit(
            "preflight",
            {
                "decision_id": decision.decision_id,
                "outcome": decision.outcome,
                "reason": decision.reason,
                "grant_id": decision.grant_id,
                "reservation_id": decision.reservation_id,
                "invocation_digest": decision.invocation_digest,
            },
        )
        return PreflightEnvelope(
            decision=decision,
            reservation_token=reservation_token if claim_won else None,
        )

    def postflight(
        self,
        observation: ToolObservation,
        *,
        reservation_token: str,
    ) -> AuthoritativeToolReceipt:
        now = self._now()
        try:
            supplied_token_digest = secret_sha256(reservation_token)
        except ValueError as exc:
            raise ToolGovernanceConflict(
                "reservation token is invalid"
            ) from exc
        if supplied_token_digest != observation.reservation_token_digest:
            raise ToolGovernanceConflict(
                "reservation token does not match the observation"
            )
        selected: dict[str, AuthoritativeToolReceipt] = {}
        changed = False

        def observe(document: dict[str, Any]) -> None:
            nonlocal changed
            self._validate_document(document)
            calls = self._mapping(document, "calls")
            call = calls.get(observation.reservation_id)
            if not isinstance(call, dict):
                raise ToolGovernanceConflict("reservation not found")
            stored_token_digest = str(call.get("reservation_token_digest") or "")
            if (
                not secrets.compare_digest(
                    stored_token_digest,
                    observation.reservation_token_digest,
                )
                or not secrets.compare_digest(
                    stored_token_digest,
                    supplied_token_digest,
                )
            ):
                raise ToolGovernanceConflict("reservation token is invalid")

            receipt = self._load_model(
                AuthoritativeToolReceipt,
                call.get("receipt"),
                label="tool receipt",
            )
            self._assert_observation_scope(receipt, observation)
            if observation.observed_at < receipt.reserved_at:
                raise ToolGovernanceConflict(
                    "postflight observation predates its reservation"
                )
            if observation.observed_at > now + MAX_OBSERVATION_CLOCK_SKEW:
                raise ToolGovernanceConflict(
                    "postflight observation is too far in the future"
                )
            elapsed_ms = int(
                (observation.observed_at - receipt.reserved_at).total_seconds()
                * 1000
            )
            if observation.duration_ms > (
                elapsed_ms
                + int(MAX_OBSERVATION_CLOCK_SKEW.total_seconds() * 1000)
            ):
                raise ToolGovernanceConflict(
                    "postflight duration exceeds the reservation timeline"
                )
            existing_observation = call.get("observation")
            if existing_observation is not None:
                stored_observation = self._load_model(
                    ToolObservation,
                    existing_observation,
                    label="tool observation",
                )
                if stored_observation.observation_digest != (
                    observation.observation_digest
                ):
                    raise ToolGovernanceConflict(
                        "a contradictory observation is already recorded"
                    )
                selected["receipt"] = receipt
                return
            if receipt.ledger_state != "authorized_not_observed":
                raise ToolGovernanceConflict(
                    "reservation is not awaiting an observation"
                )

            observed_receipt = AuthoritativeToolReceipt.create(
                receipt_id=receipt.receipt_id,
                run_id=receipt.run_id,
                tool_call_id=receipt.tool_call_id,
                grant_id=receipt.grant_id,
                reservation_id=receipt.reservation_id,
                tool_name=receipt.tool_name,
                tool_kind=receipt.tool_kind,
                risk_level=receipt.risk_level,
                args_digest=receipt.args_digest,
                targets_digest=receipt.targets_digest,
                environment_digest=receipt.environment_digest,
                invocation_digest=receipt.invocation_digest,
                grant_digest=receipt.grant_digest,
                ledger_state=(
                    "observed_success"
                    if observation.outcome == "success"
                    else "observed_failure"
                ),
                approval_verified=receipt.approval_verified,
                approval_id=receipt.approval_id,
                approval_revision=receipt.approval_revision,
                policy_revision=receipt.policy_revision,
                registry_revision=receipt.registry_revision,
                reserved_at=receipt.reserved_at,
                observed_at=observation.observed_at,
                outcome=observation.outcome,
                result_digest=observation.result_digest,
            )
            call["observation"] = observation.model_dump(mode="json")
            call["receipt"] = observed_receipt.model_dump(mode="json")
            selected["receipt"] = observed_receipt
            changed = True

        self.state_store.mutate_json(STATE_FILE, observe)
        receipt = selected.get("receipt")
        if receipt is None:
            raise ToolGovernanceStorageError(
                "postflight mutation did not produce a receipt"
            )
        if changed:
            self._audit(
                "postflight",
                {
                    "receipt_id": receipt.receipt_id,
                    "run_id": receipt.run_id,
                    "tool_call_id": receipt.tool_call_id,
                    "ledger_state": receipt.ledger_state,
                    "result_digest": receipt.result_digest,
                },
            )
        return receipt

    def revoke_grant(self, grant_id: str, *, reason: str) -> dict[str, Any]:
        now = self._now()
        selected: dict[str, Any] = {}

        def revoke(document: dict[str, Any]) -> None:
            self._validate_document(document)
            grants = self._mapping(document, "grants")
            entry = grants.get(grant_id)
            if not isinstance(entry, dict):
                raise KeyError(f"Capability grant not found: {grant_id}")
            if entry.get("status") == "revoked":
                selected.update(entry)
                return
            entry["status"] = "revoked"
            entry["revoked_at"] = now.isoformat()
            entry["revocation_reason"] = str(reason or "revoked")[:600]
            selected.update(entry)

        self.state_store.mutate_json(STATE_FILE, revoke)
        self._audit(
            "grant_revoked",
            {"grant_id": grant_id, "reason": str(reason or "revoked")[:600]},
        )
        return dict(selected)

    def cancel_session(
        self,
        binding_digest: str,
        *,
        reason: str = "session_cancelled",
    ) -> dict[str, Any]:
        now = self._now()
        result: dict[str, Any] = {}

        def cancel(document: dict[str, Any]) -> None:
            self._validate_document(document)
            sessions = self._mapping(document, "sessions")
            session = sessions.get(binding_digest)
            if not isinstance(session, dict):
                raise KeyError(f"Governed session not found: {binding_digest}")
            session["status"] = "cancelled"
            session["cancelled_at"] = now.isoformat()
            session["cancellation_reason"] = str(reason or "session_cancelled")[
                :600
            ]
            revoked = 0
            for entry in self._mapping(document, "grants").values():
                if not isinstance(entry, dict):
                    raise ToolGovernanceStorageError(
                        "grant ledger contains a malformed entry"
                    )
                grant = self._load_model(
                    CapabilityGrant,
                    entry.get("grant"),
                    label="capability grant",
                )
                if (
                    grant.binding.binding_digest == binding_digest
                    and entry.get("status") == "active"
                ):
                    entry["status"] = "revoked"
                    entry["revoked_at"] = now.isoformat()
                    entry["revocation_reason"] = "governed_session_cancelled"
                    revoked += 1
            result.update({"session": dict(session), "revoked_grants": revoked})

        self.state_store.mutate_json(STATE_FILE, cancel)
        self._audit(
            "session_cancelled",
            {
                "binding_digest": binding_digest,
                "revoked_grants": result.get("revoked_grants", 0),
            },
        )
        return result

    def resolve_receipt(
        self,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        """Resolve only an observed authoritative receipt for Verifier."""

        document = self.state_store.read_json(STATE_FILE)
        self._validate_document(document)
        call_index = self._mapping(document, "run_call_index")
        reservation_id = call_index.get(self._call_key(run_id, tool_call_id))
        if not isinstance(reservation_id, str):
            return None
        call = self._mapping(document, "calls").get(reservation_id)
        if not isinstance(call, dict):
            raise ToolGovernanceStorageError(
                "tool-call index points to a missing receipt"
            )
        receipt = self._load_model(
            AuthoritativeToolReceipt,
            call.get("receipt"),
            label="tool receipt",
        )
        if receipt.ledger_state not in {"observed_success", "observed_failure"}:
            return None
        return receipt.model_dump(mode="json")

    def status(self) -> dict[str, Any]:
        try:
            document = self.state_store.read_json(STATE_FILE)
            self._validate_document(document)
            sessions = self._mapping(document, "sessions")
            grants = self._mapping(document, "grants")
            calls = self._mapping(document, "calls")
        except ToolGovernanceStorageError as exc:
            return {
                "status": "degraded",
                "phase": CONTRACT_PHASE,
                "tool_proxy_enforced": False,
                "execution_authority_enabled": False,
                "reason": str(exc),
            }
        call_states: dict[str, int] = {}
        for entry in calls.values():
            if not isinstance(entry, dict):
                continue
            receipt = entry.get("receipt")
            state = (
                str(receipt.get("ledger_state") or "invalid")
                if isinstance(receipt, dict)
                else "invalid"
            )
            call_states[state] = call_states.get(state, 0) + 1
        return {
            "status": "contract_only",
            "phase": CONTRACT_PHASE,
            "tool_proxy_enforced": False,
            "execution_authority_enabled": False,
            "counts": {
                "sessions": len(sessions),
                "grants": len(grants),
                "calls": len(calls),
                "call_states": call_states,
            },
        }

    def _blocked(
        self,
        invocation: ToolInvocation,
        now: datetime,
        reason: str,
    ) -> PreflightEnvelope:
        decision = self._decision(
            invocation,
            now,
            outcome="block",
            ledger_state="blocked",
            reason=reason,
        )
        self._audit(
            "preflight",
            {
                "decision_id": decision.decision_id,
                "outcome": decision.outcome,
                "reason": decision.reason,
                "invocation_digest": decision.invocation_digest,
            },
        )
        return PreflightEnvelope(decision=decision)

    def _decision(
        self,
        invocation: ToolInvocation,
        decided_at: datetime,
        *,
        outcome: str,
        ledger_state: str,
        reason: str,
        grant_id: str | None = None,
        reservation_id: str | None = None,
        reservation_token_digest: str | None = None,
    ) -> PreflightDecision:
        return PreflightDecision.create(
            decision_id=f"decision_{uuid4().hex}",
            outcome=outcome,
            execute_allowed=False,
            reason=reason,
            ledger_state=ledger_state,
            grant_id=grant_id,
            reservation_id=reservation_id,
            reservation_token_digest=reservation_token_digest,
            invocation_digest=invocation.invocation_digest,
            decided_at=decided_at,
        )

    def _assert_observation_scope(
        self,
        receipt: AuthoritativeToolReceipt,
        observation: ToolObservation,
    ) -> None:
        expected = (
            receipt.grant_id,
            receipt.reservation_id,
            receipt.invocation_digest,
            receipt.run_id,
            receipt.tool_call_id,
        )
        observed = (
            observation.grant_id,
            observation.reservation_id,
            observation.invocation_digest,
            observation.run_id,
            observation.tool_call_id,
        )
        if expected != observed:
            raise ToolGovernanceConflict(
                "postflight observation does not match its reservation scope"
            )

    def _audit(self, event: str, artifacts: dict[str, Any]) -> None:
        self.state_store.append_jsonl(
            "tool_call_log.jsonl",
            {
                "route": "tool_governance",
                "status": event,
                "artifacts": artifacts,
            },
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("tool governance clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _call_key(run_id: str, tool_call_id: str) -> str:
        return canonical_sha256({"run_id": run_id, "tool_call_id": tool_call_id})

    @staticmethod
    def _mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
        value = document.get(key)
        if not isinstance(value, dict):
            raise ToolGovernanceStorageError(
                f"tool governance state field {key} must be an object"
            )
        return value

    @staticmethod
    def _load_model(
        model: type[_ModelT],
        raw: Any,
        *,
        label: str,
    ) -> _ModelT:
        if not isinstance(raw, dict):
            raise ToolGovernanceStorageError(f"{label} is missing or malformed")
        try:
            return model.model_validate_json(
                json.dumps(raw, ensure_ascii=False, allow_nan=False),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise ToolGovernanceStorageError(
                f"{label} failed strict integrity validation"
            ) from exc

    @staticmethod
    def _validate_document(document: dict[str, Any]) -> None:
        if document.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ToolGovernanceStorageError(
                "tool governance state schema is missing or unsupported"
            )
        if document.get("phase") != CONTRACT_PHASE:
            raise ToolGovernanceStorageError(
                "tool governance state phase is not contract_only"
            )
        if document.get("execution_authority_enabled") is not False:
            raise ToolGovernanceStorageError(
                "contract-only runtime cannot enable execution authority"
            )
        if document.get("tool_proxy_enforced") is not False:
            raise ToolGovernanceStorageError(
                "contract-only runtime cannot report Tool Proxy enforcement"
            )
        for key in (
            "sessions",
            "grants",
            "grant_token_index",
            "calls",
            "run_call_index",
        ):
            if not isinstance(document.get(key), dict):
                raise ToolGovernanceStorageError(
                    f"tool governance state field {key} must be an object"
                )


__all__ = [
    "IssuedCapability",
    "PreflightEnvelope",
    "ToolGovernanceConflict",
    "ToolGovernanceError",
    "ToolGovernanceRuntime",
    "ToolGovernanceStorageError",
]
