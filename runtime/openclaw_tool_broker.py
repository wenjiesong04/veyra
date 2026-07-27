from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from core.world_state import WorldStateStore
from interface.event_schema import VeyraTaskPacket
from runtime.tool_governance_runtime import (
    ToolGovernanceConflict,
    ToolGovernanceRuntime,
    ToolGovernanceStorageError,
)
from tool_proxy.execution_scope import ExecutionScope, ExecutionScopeError
from tool_proxy.governance_contract import (
    GovernedSessionBinding,
    ToolInvocation,
    ToolObservation,
    VerifiedToolEffect,
    canonical_sha256,
    secret_sha256,
)
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell


HOOK_STATE_FILE = "openclaw_tool_hook_state.json"
HOOK_STATE_SCHEMA_VERSION = "veyra.openclaw_tool_hook_state.v1"
HOOK_POLICY_REVISION = "veyra.phase3.scoped_sandbox.v1"
HOOK_REGISTRY_REVISION = "veyra.openclaw.tool_registry.v1"
HOOK_EXECUTOR_ID = "veyra.openclaw.scoped_executor.v1"
HOOK_PLUGIN_PROTOCOL = "veyra.openclaw.governance.v1"
HOOK_PLUGIN_IMPLEMENTATION_REVISION = (
    "veyra.openclaw.governance.phase3.v2"
)

CUSTOM_TOOL_REGISTRY: dict[str, tuple[str, str, str]] = {
    "veyra_file_read": ("file.read", "file", "R1"),
    "veyra_file_write": ("file.write", "file", "R2"),
    "veyra_shell_probe": ("shell.run", "shell", "R0"),
}


def hook_implementation_identity() -> dict[str, str]:
    return {
        "policy_revision": HOOK_POLICY_REVISION,
        "registry_revision": HOOK_REGISTRY_REVISION,
        "executor_id": HOOK_EXECUTOR_ID,
        "plugin_protocol": HOOK_PLUGIN_PROTOCOL,
        "plugin_implementation_revision": (
            HOOK_PLUGIN_IMPLEMENTATION_REVISION
        ),
    }


def project_current_hook_enforcement(
    broker_status: Mapping[str, Any],
    agent_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Combine broker canary evidence with a fresh OpenClaw plugin snapshot."""

    projected = dict(broker_status)
    capabilities = (
        agent_status.get("capabilities")
        if isinstance(agent_status, Mapping)
        and isinstance(agent_status.get("capabilities"), Mapping)
        else {}
    )
    raw = (
        capabilities.get("raw")
        if isinstance(capabilities, Mapping)
        and isinstance(capabilities.get("raw"), Mapping)
        else {}
    )
    plugin = (
        raw.get("governance_plugin")
        if isinstance(raw, Mapping)
        and isinstance(raw.get("governance_plugin"), Mapping)
        else {}
    )
    implementation = (
        broker_status.get("implementation")
        if isinstance(broker_status.get("implementation"), Mapping)
        else {}
    )
    plugin_active = (
        isinstance(agent_status, Mapping)
        and agent_status.get("connected") is True
        and plugin.get("status") in {
            "active",
            "ok",
            "success",
            "validated",
        }
    )
    revision_match = (
        plugin.get("protocol_version")
        == implementation.get("plugin_protocol")
        and plugin.get("implementation_revision")
        == implementation.get("plugin_implementation_revision")
    )
    enforced = bool(
        broker_status.get("canary_validated") is True
        and plugin_active
        and revision_match
    )
    projected.update(
        {
            "status": (
                "validated" if enforced else "validation_pending"
            ),
            "tool_proxy_enforced": enforced,
            "current_plugin": {
                "observed": isinstance(agent_status, Mapping),
                "connected": (
                    agent_status.get("connected") is True
                    if isinstance(agent_status, Mapping)
                    else False
                ),
                "status": plugin.get("status") or "unavailable",
                "protocol_version": plugin.get("protocol_version"),
                "implementation_revision": plugin.get(
                    "implementation_revision"
                ),
                "identity_match": revision_match,
            },
        }
    )
    return projected


class OpenClawToolBrokerError(RuntimeError):
    """Base error for the authenticated OpenClaw hook broker."""


class OpenClawHookDenied(OpenClawToolBrokerError):
    """Raised when a hook request has no exact scoped authority."""


class OpenClawHookConflict(OpenClawToolBrokerError):
    """Raised for token replay or contradictory hook observations."""


@dataclass(frozen=True, slots=True)
class HookDispatchRegistration:
    run_id: str
    session_key: str
    dispatch_token: str
    expires_at: datetime
    binding_digest: str
    sandbox_root: str
    allowed_tools: tuple[str, ...]

    def plugin_payload(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "sessionKey": self.session_key,
            "dispatchToken": self.dispatch_token,
            "expiresAt": self.expires_at.isoformat(),
            "bindingDigest": self.binding_digest,
            "allowedTools": list(self.allowed_tools),
        }


class OpenClawToolBroker:
    """Final server-side fence and executor for Veyra-governed OpenClaw tools.

    OpenClaw never receives direct filesystem or process authority from this
    component.  The plugin gets one short-lived dispatch token, then one exact
    execution token per host tool-call.  The actual operation is performed here
    with :class:`SafeFile` or :class:`SafeShell` after the ledger wins an atomic
    one-use execution claim.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        governance: ToolGovernanceRuntime,
        *,
        sandbox_base: str | Path,
        clock: Any | None = None,
        dispatch_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self.state_store = state_store
        self.governance = governance
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.dispatch_ttl = dispatch_ttl
        if not timedelta(seconds=30) <= dispatch_ttl <= timedelta(hours=1):
            raise ValueError("dispatch_ttl must be between 30 seconds and 1 hour")
        self.sandbox_base = Path(sandbox_base).expanduser().absolute()
        self.sandbox_base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.sandbox_base.is_symlink() or not self.sandbox_base.is_dir():
            raise ValueError("sandbox_base must be a real directory")
        self.native_canary_root = (
            self.sandbox_base.parent / "openclaw_native_block_canaries"
        ).absolute()
        self.native_canary_root.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        if (
            self.native_canary_root.is_symlink()
            or not self.native_canary_root.is_dir()
        ):
            raise ValueError("native canary root must be a real directory")
        self._validate_state()

    def prepare_dispatch(
        self,
        task_packet: VeyraTaskPacket,
        *,
        run_id: str,
        session_key: str,
    ) -> HookDispatchRegistration:
        run = self._identifier(run_id, "run_id")
        session = self._identifier(session_key, "session_key")
        context = (
            task_packet.governance_context
            if isinstance(
                getattr(task_packet, "governance_context", None),
                dict,
            )
            else {}
        )
        user_id = self._identifier(
            context.get("user_id") or "local_user",
            "user_id",
        )
        workspace_id = self._identifier(
            context.get("workspace_id") or str(Path.cwd().resolve()),
            "workspace_id",
        )
        channel_id = self._identifier(
            context.get("channel_id") or "api",
            "channel_id",
        )
        case_id = self._identifier(
            context.get("case_id") or task_packet.task_id,
            "case_id",
        )
        step_id = self._identifier(
            context.get("step_id") or task_packet.task_id,
            "step_id",
        )
        binding = GovernedSessionBinding.create(
            user_id=user_id,
            workspace_id=workspace_id,
            agent_id=self._identifier(
                task_packet.target_agent or "openclaw",
                "agent_id",
            ),
            session_id=session,
            channel_id=channel_id,
            case_id=case_id,
            step_id=step_id,
            run_id=run,
        )
        sandbox_root = (
            self.sandbox_base
            / f"{canonical_sha256({'run_id': run, 'binding': binding.binding_digest})[:32]}"
        )
        sandbox_root.mkdir(mode=0o700)
        scope = ExecutionScope.create(sandbox_root)
        dispatch_token = secrets.token_urlsafe(32)
        token_digest = secret_sha256(dispatch_token)
        now = self._now()
        expires_at = now + self.dispatch_ttl
        dispatch_payload = {
            "run_id": run,
            "session_key": session,
            "binding": binding.model_dump(mode="json"),
            "dispatch_token_digest": token_digest,
            "sandbox_root": str(scope.root),
            "scope_digest": scope.scope_digest,
            "allowed_tools": sorted(CUSTOM_TOOL_REGISTRY),
            "status": "active",
            "registered_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "cancelled_at": None,
        }

        self.governance.register_session(binding)

        def register(document: dict[str, Any]) -> None:
            self._validate_document(document)
            dispatches = self._mapping(document, "dispatches")
            existing = dispatches.get(run)
            if isinstance(existing, dict):
                if (
                    existing.get("binding") != dispatch_payload["binding"]
                    or existing.get("session_key") != session
                    or existing.get("dispatch_token_digest") != token_digest
                ):
                    raise OpenClawHookConflict(
                        "run_id is already registered to another dispatch"
                    )
                return
            dispatches[run] = dispatch_payload
            self._increment(document, "dispatch_registered")

        self.state_store.mutate_json(HOOK_STATE_FILE, register)
        self._audit(
            "hook_dispatch_registered",
            {
                "run_id": run,
                "binding_digest": binding.binding_digest,
                "scope_digest": scope.scope_digest,
                "allowed_tools": sorted(CUSTOM_TOOL_REGISTRY),
            },
        )
        return HookDispatchRegistration(
            run_id=run,
            session_key=session,
            dispatch_token=dispatch_token,
            expires_at=expires_at,
            binding_digest=binding.binding_digest,
            sandbox_root=str(scope.root),
            allowed_tools=tuple(sorted(CUSTOM_TOOL_REGISTRY)),
        )

    def cancel_dispatch(
        self,
        *,
        run_id: str,
        dispatch_token: str,
        reason: str = "dispatch_cancelled",
    ) -> dict[str, Any]:
        dispatch = self._active_dispatch(
            run_id=run_id,
            dispatch_token=dispatch_token,
            allow_expired=True,
            allow_inactive=True,
        )
        return self._cancel_registered_dispatch(
            run_id=run_id,
            dispatch=dispatch,
            reason=reason,
        )

    def cancel_registered_run(
        self,
        run_id: str,
        *,
        reason: str = "dispatch_cancelled",
    ) -> dict[str, Any]:
        """Trusted in-process cancellation without retaining a raw bearer."""

        run = self._identifier(run_id, "run_id")
        document = self.state_store.read_json(HOOK_STATE_FILE)
        self._validate_document(document)
        dispatch = self._mapping(document, "dispatches").get(run)
        if not isinstance(dispatch, dict):
            raise OpenClawHookDenied("governed dispatch not found")
        return self._cancel_registered_dispatch(
            run_id=run,
            dispatch=dict(dispatch),
            reason=reason,
        )

    def _cancel_registered_dispatch(
        self,
        *,
        run_id: str,
        dispatch: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Linearize cancellation against the irreversible execution claim."""

        binding = self._binding(dispatch)
        governance_result = self.governance.cancel_session(
            binding.binding_digest,
            reason=reason,
        )
        cancellation_status = str(
            governance_result.get("status") or "cancelled"
        )
        now = self._now()

        def cancel(document: dict[str, Any]) -> None:
            self._validate_document(document)
            stored = self._mapping(document, "dispatches").get(run_id)
            if not isinstance(stored, dict):
                raise OpenClawHookDenied("governed dispatch not found")
            if self._binding(stored) != binding:
                raise OpenClawHookConflict(
                    "dispatch binding changed during cancellation"
                )
            if (
                stored.get("status") == "cancelled"
                and cancellation_status == "cancelled"
            ):
                return
            if cancellation_status == "too_late":
                stored["status"] = "cancellation_requested"
                stored["cancellation_requested_at"] = now.isoformat()
            else:
                stored["status"] = "cancelled"
                stored["cancelled_at"] = now.isoformat()
            stored["cancellation_reason"] = str(reason or "dispatch_cancelled")[
                :600
            ]
            cancelled_reservations = {
                str(item)
                for item in (
                    governance_result.get("cancelled_reservations") or []
                )
            }
            attempts = self._mapping(document, "attempts")
            token_index = self._mapping(
                document,
                "execution_token_index",
            )
            for reservation_id in cancelled_reservations:
                attempt = attempts.get(reservation_id)
                if not isinstance(attempt, dict):
                    continue
                if attempt.get("status") == "reserved":
                    attempt["status"] = "cancelled_before_start"
                    attempt["execution_completed_at"] = now.isoformat()
                token_digest = str(
                    attempt.get("execution_token_digest") or ""
                )
                if token_digest:
                    token_index.pop(token_digest, None)

        self.state_store.mutate_json(HOOK_STATE_FILE, cancel)
        return {
            "status": cancellation_status,
            "run_id": run_id,
            "binding_digest": binding.binding_digest,
            "revoked_grants": governance_result.get("revoked_grants", 0),
            "executing_reservations": governance_result.get(
                "executing_reservations",
                [],
            ),
            "cancelled_reservations": governance_result.get(
                "cancelled_reservations",
                [],
            ),
        }

    def preflight(
        self,
        *,
        run_id: str,
        session_key: str,
        tool_call_id: str,
        tool_name: str,
        params: Mapping[str, Any],
        dispatch_token: str,
    ) -> dict[str, Any]:
        run = self._identifier(run_id, "run_id")
        call_id = self._identifier(tool_call_id, "tool_call_id")
        host_tool = self._identifier(tool_name, "tool_name")
        try:
            dispatch = self._active_dispatch(
                run_id=run,
                dispatch_token=dispatch_token,
            )
        except (OpenClawToolBrokerError, TypeError, ValueError) as exc:
            # An unauthenticated caller must not be able to consume a real
            # run/call identity or grow the authoritative attempt ledger.
            return {
                "allow": False,
                "reason": self._bounded_reason(exc),
                "runId": run,
                "toolCallId": call_id,
                "toolName": host_tool,
            }
        try:
            if dispatch.get("session_key") != session_key:
                raise OpenClawHookDenied("OpenClaw session key mismatch")
            if host_tool not in CUSTOM_TOOL_REGISTRY:
                raise OpenClawHookDenied(
                    "tool is not in the governed OpenClaw registry"
                )
            invocation, risk_level = self._invocation(
                dispatch=dispatch,
                tool_call_id=call_id,
                host_tool_name=host_tool,
                params=params,
            )
            issued = self.governance.issue_grant(
                invocation,
                approval_id="phase3_scoped_sandbox_policy",
                approval_revision=HOOK_POLICY_REVISION,
                policy_revision=HOOK_POLICY_REVISION,
                registry_revision=HOOK_REGISTRY_REVISION,
                risk_level=risk_level,
                expires_at=min(
                    self._parse_time(dispatch["expires_at"]),
                    self._now() + timedelta(minutes=5),
                ),
            )
            envelope = self.governance.preflight(
                invocation,
                capability_token=issued.capability_token,
            )
            if (
                envelope.decision.outcome != "would_allow"
                or not envelope.reservation_token
                or not envelope.decision.reservation_id
            ):
                raise OpenClawHookDenied(envelope.decision.reason)

            execution_token = secrets.token_urlsafe(32)
            execution_token_digest = secret_sha256(execution_token)
            self.governance.bind_scoped_execution_token(
                reservation_id=envelope.decision.reservation_id,
                reservation_token=envelope.reservation_token,
                execution_token_digest=execution_token_digest,
                executor=HOOK_EXECUTOR_ID,
            )
            attempt_id = envelope.decision.reservation_id
            attempt = {
                "attempt_id": attempt_id,
                "run_id": run,
                "session_key": session_key,
                "tool_call_id": call_id,
                "host_tool_name": host_tool,
                "tool_name": invocation.tool_name,
                "tool_kind": invocation.tool_kind,
                "params_digest": canonical_sha256(dict(params)),
                "invocation": invocation.model_dump(mode="json"),
                "grant_id": issued.grant.grant_id,
                "reservation_id": envelope.decision.reservation_id,
                "execution_token_digest": execution_token_digest,
                "status": "reserved",
                "preflight_at": self._now().isoformat(),
                "execution_started_at": None,
                "execution_completed_at": None,
                "result_digest": None,
                "plugin_observation": None,
            }

            def persist(document: dict[str, Any]) -> None:
                self._validate_document(document)
                stored_dispatch = self._mapping(
                    document,
                    "dispatches",
                ).get(run)
                if (
                    not isinstance(stored_dispatch, dict)
                    or stored_dispatch.get("status") != "active"
                    or self._binding(stored_dispatch) != invocation.binding
                ):
                    raise OpenClawHookDenied(
                        "governed dispatch was cancelled before preflight commit"
                    )
                call_key = self._call_key(run, call_id)
                call_index = self._mapping(document, "call_index")
                if call_key in call_index:
                    raise OpenClawHookConflict("tool call replay")
                token_index = self._mapping(
                    document,
                    "execution_token_index",
                )
                if execution_token_digest in token_index:
                    raise OpenClawHookConflict(
                        "execution token digest collision"
                    )
                self._mapping(document, "attempts")[attempt_id] = attempt
                call_index[call_key] = attempt_id
                token_index[execution_token_digest] = attempt_id
                self._increment(document, "preflight_total")
                self._increment(document, "preflight_allowed")

            self.state_store.mutate_json(HOOK_STATE_FILE, persist)
            self._audit(
                "hook_preflight_allowed",
                {
                    "run_id": run,
                    "tool_call_id": call_id,
                    "tool_name": invocation.tool_name,
                    "invocation_digest": invocation.invocation_digest,
                    "reservation_id": envelope.decision.reservation_id,
                },
            )
            return {
                "allow": True,
                "reason": "exact scoped execution reserved",
                "runId": run,
                "toolCallId": call_id,
                "toolName": host_tool,
                "canonicalToolName": invocation.tool_name,
                "invocationDigest": invocation.invocation_digest,
                "reservationId": envelope.decision.reservation_id,
                "reservationToken": envelope.reservation_token,
                "executionToken": execution_token,
                "expiresAt": issued.grant.expires_at.isoformat(),
            }
        except (
            OpenClawToolBrokerError,
            ToolGovernanceConflict,
            ToolGovernanceStorageError,
            ExecutionScopeError,
            TypeError,
            ValueError,
        ) as exc:
            reason = self._bounded_reason(exc)
            self._record_blocked_preflight(
                run_id=run,
                tool_call_id=call_id,
                tool_name=host_tool,
                reason=reason,
            )
            return {
                "allow": False,
                "reason": reason,
                "runId": run,
                "toolCallId": call_id,
                "toolName": host_tool,
            }

    def execute(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        params: Mapping[str, Any],
        dispatch_token: str,
        reservation_token: str,
        execution_token: str,
    ) -> dict[str, Any]:
        run = self._identifier(run_id, "run_id")
        call_id = self._identifier(tool_call_id, "tool_call_id")
        host_tool = self._identifier(tool_name, "tool_name")
        dispatch = self._active_dispatch(
            run_id=run,
            dispatch_token=dispatch_token,
        )
        attempt = self._attempt(run, call_id)
        if attempt.get("host_tool_name") != host_tool:
            raise OpenClawHookDenied("host tool name changed after preflight")
        if attempt.get("params_digest") != canonical_sha256(dict(params)):
            raise OpenClawHookDenied("tool params changed after preflight")
        invocation = ToolInvocation.model_validate_json(
            json.dumps(
                attempt.get("invocation"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            strict=True,
        )
        if invocation.binding != self._binding(dispatch):
            raise OpenClawHookDenied("dispatch binding changed after preflight")
        execution_token_digest = secret_sha256(execution_token)
        if not secrets.compare_digest(
            str(attempt.get("execution_token_digest") or ""),
            execution_token_digest,
        ):
            raise OpenClawHookDenied("execution token is invalid")

        self.governance.claim_scoped_execution(
            reservation_id=str(attempt["reservation_id"]),
            reservation_token=reservation_token,
            execution_token=execution_token,
            invocation_digest=invocation.invocation_digest,
            executor=HOOK_EXECUTOR_ID,
        )
        started_at = self._now()

        def claim(document: dict[str, Any]) -> None:
            self._validate_document(document)
            attempts = self._mapping(document, "attempts")
            stored = attempts.get(attempt["attempt_id"])
            if not isinstance(stored, dict):
                self._increment(document, "started_without_reservation")
                raise OpenClawHookConflict("reserved attempt is missing")
            if stored.get("status") != "reserved":
                raise OpenClawHookConflict(
                    "execution token was already consumed"
                )
            token_index = self._mapping(
                document,
                "execution_token_index",
            )
            if token_index.get(execution_token_digest) != attempt["attempt_id"]:
                raise OpenClawHookDenied("execution token index mismatch")
            token_index.pop(execution_token_digest, None)
            stored["status"] = "executing"
            stored["execution_started_at"] = started_at.isoformat()
            self._increment(document, "execution_started")

        self.state_store.mutate_json(HOOK_STATE_FILE, claim)
        monotonic_started = time.monotonic()
        observation_recorded = False
        try:
            raw_result = self._execute_tool(
                dispatch=dispatch,
                host_tool_name=host_tool,
                params=dict(params),
            )
            result = self._public_tool_result(host_tool, raw_result)
            succeeded = result.get("status") == "ok"
            result_digest = canonical_sha256(result)
            observed_at = self._now()
            observation = ToolObservation.create(
                grant_id=str(attempt["grant_id"]),
                reservation_id=str(attempt["reservation_id"]),
                reservation_token_digest=secret_sha256(reservation_token),
                invocation_digest=invocation.invocation_digest,
                run_id=run,
                tool_call_id=call_id,
                outcome="success" if succeeded else "failure",
                result_digest=result_digest,
                observed_at=observed_at,
                duration_ms=max(
                    0,
                    int((time.monotonic() - monotonic_started) * 1000),
                ),
            )
            receipt = self.governance.postflight(
                observation,
                reservation_token=reservation_token,
            )
            observation_recorded = True
            effect: VerifiedToolEffect | None = None
            if succeeded:
                effect = self._verify_effect(
                    receipt=receipt,
                    invocation=invocation,
                    host_tool_name=host_tool,
                    params=dict(params),
                    result=result,
                    dispatch=dispatch,
                )
                self.governance.record_verified_effect(effect)
            completion_state = (
                "observed_success" if succeeded else "observed_failure"
            )
            self.governance.complete_scoped_execution(
                reservation_id=str(attempt["reservation_id"]),
                reservation_token=reservation_token,
                state=completion_state,
            )
            self._complete_attempt(
                attempt_id=str(attempt["attempt_id"]),
                state=completion_state,
                result_digest=result_digest,
            )
            return {
                "status": result.get("status"),
                "result": result,
                "resultDigest": result_digest,
                "receiptRef": {
                    "run_id": run,
                    "tool_call_id": call_id,
                    "invocation_digest": invocation.invocation_digest,
                },
                "effectEvidenceDigest": (
                    effect.evidence_digest if effect is not None else None
                ),
            }
        except Exception as exc:
            reason = self._bounded_reason(exc)
            if not observation_recorded:
                try:
                    failure_result = {
                        "status": "error",
                        "reason": reason,
                    }
                    result_digest = canonical_sha256(failure_result)
                    observation = ToolObservation.create(
                        grant_id=str(attempt["grant_id"]),
                        reservation_id=str(attempt["reservation_id"]),
                        reservation_token_digest=secret_sha256(
                            reservation_token
                        ),
                        invocation_digest=invocation.invocation_digest,
                        run_id=run,
                        tool_call_id=call_id,
                        outcome="failure",
                        result_digest=result_digest,
                        observed_at=self._now(),
                        duration_ms=max(
                            0,
                            int(
                                (time.monotonic() - monotonic_started)
                                * 1000
                            ),
                        ),
                    )
                    self.governance.postflight(
                        observation,
                        reservation_token=reservation_token,
                    )
                    self.governance.complete_scoped_execution(
                        reservation_id=str(attempt["reservation_id"]),
                        reservation_token=reservation_token,
                        state="observed_failure",
                    )
                    self._complete_attempt(
                        attempt_id=str(attempt["attempt_id"]),
                        state="observed_failure",
                        result_digest=result_digest,
                    )
                except Exception:
                    self._mark_indeterminate(
                        attempt_id=str(attempt["attempt_id"]),
                        reservation_id=str(attempt["reservation_id"]),
                        reservation_token=reservation_token,
                        reason=reason,
                    )
            raise OpenClawToolBrokerError(reason) from exc

    def observe(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        dispatch_token: str,
        outcome: str,
        params_digest: str | None = None,
        result_digest: str | None = None,
        duration_ms: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Record bounded plugin diagnostics without granting outcome authority."""

        run = self._identifier(run_id, "run_id")
        call_id = self._identifier(tool_call_id, "tool_call_id")
        host_tool = self._identifier(tool_name, "tool_name")
        self._active_dispatch(
            run_id=run,
            dispatch_token=dispatch_token,
            allow_expired=True,
        )
        if outcome not in {
            "completed",
            "error",
            "blocked",
            "indeterminate",
        }:
            raise ValueError("invalid plugin observation outcome")
        if result_digest is not None and (
            len(result_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in result_digest
            )
        ):
            raise ValueError("result_digest must be a SHA-256 digest")
        if params_digest is not None and (
            len(params_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in params_digest
            )
        ):
            raise ValueError("params_digest must be a SHA-256 digest")
        bounded_duration = (
            max(0, min(int(duration_ms), 86_400_000))
            if duration_ms is not None
            else None
        )
        observation = {
            "outcome": outcome,
            "params_digest": params_digest,
            "result_digest": result_digest,
            "duration_ms": bounded_duration,
            "reason": str(reason or "")[:600],
            "observed_at": self._now().isoformat(),
            "authoritative": False,
        }

        def persist(document: dict[str, Any]) -> None:
            self._validate_document(document)
            attempt_id = self._mapping(document, "call_index").get(
                self._call_key(run, call_id)
            )
            if isinstance(attempt_id, str):
                attempt = self._mapping(document, "attempts").get(attempt_id)
                if not isinstance(attempt, dict):
                    raise OpenClawHookConflict(
                        "hook call index points to a missing attempt"
                    )
                existing = attempt.get("plugin_observation")
                if isinstance(existing, dict) and existing != observation:
                    raise OpenClawHookConflict(
                        "contradictory plugin observation"
                    )
                attempt["plugin_observation"] = observation
            elif outcome == "blocked":
                diagnostic_id = f"plugin_block_{canonical_sha256({'run': run, 'call': call_id})[:24]}"
                self._mapping(document, "attempts")[diagnostic_id] = {
                    "attempt_id": diagnostic_id,
                    "run_id": run,
                    "tool_call_id": call_id,
                    "host_tool_name": host_tool,
                    "status": "plugin_blocked",
                    "plugin_observation": observation,
                }
                self._mapping(document, "call_index")[
                    self._call_key(run, call_id)
                ] = diagnostic_id
            else:
                raise OpenClawHookConflict(
                    "plugin reported an unreserved execution"
                )
            self._increment(document, "plugin_observations")

        self.state_store.mutate_json(HOOK_STATE_FILE, persist)
        return {
            "status": "recorded",
            "authoritative": False,
            "runId": run,
            "toolCallId": call_id,
        }

    def status(self) -> dict[str, Any]:
        try:
            document = self.state_store.read_json(HOOK_STATE_FILE)
            self._validate_document(document)
        except (OpenClawToolBrokerError, ToolGovernanceStorageError) as exc:
            return {
                "status": "degraded",
                "scope": "veyra_governed_openclaw_sessions",
                "tool_proxy_enforced": False,
                "execution_authority_enabled": False,
                "reason": str(exc),
            }
        metrics = dict(self._mapping(document, "metrics"))
        started = int(metrics.get("execution_started") or 0)
        missing = int(metrics.get("started_without_reservation") or 0)
        coverage = (
            (started - missing) / started if started > 0 else None
        )
        canary = document.get("canary")
        canary = dict(canary) if isinstance(canary, dict) else {}
        implementation = hook_implementation_identity()
        canary_implementation = canary.get("implementation")
        implementation_match = (
            isinstance(canary_implementation, dict)
            and canary_implementation == implementation
        )
        safety_invariants_ok = missing == 0
        validated = bool(
            canary.get("status") == "validated"
            and implementation_match
            and safety_invariants_ok
        )
        canary["current_implementation_match"] = implementation_match
        return {
            "status": (
                "canary_validated"
                if validated
                else (
                    "degraded"
                    if not safety_invariants_ok
                    else "validation_pending"
                )
            ),
            "scope": "veyra_governed_openclaw_sessions",
            "tool_proxy_enforced": False,
            "canary_validated": validated,
            "safety_invariants_ok": safety_invariants_ok,
            "execution_authority_enabled": True,
            "native_side_effect_tools": "blocked_for_registered_runs",
            "executor": HOOK_EXECUTOR_ID,
            "implementation": implementation,
            "allowed_tools": sorted(CUSTOM_TOOL_REGISTRY),
            "metrics": metrics,
            "pre_tool_coverage": coverage,
            "canary": canary,
            **(
                {
                    "reason": (
                        "execution started without an exact reservation"
                    )
                }
                if not safety_invariants_ok
                else {}
            ),
            "counts": {
                "dispatches": len(self._mapping(document, "dispatches")),
                "attempts": len(self._mapping(document, "attempts")),
            },
        }

    def mark_canary_validated(
        self,
        *,
        run_id: str,
        session_key: str,
        sentinel_relative_path: str,
        native_block_path: str,
        native_block_content: str,
        plugin_protocol: str,
        plugin_implementation_revision: str,
        dispatch_token: str,
    ) -> dict[str, Any]:
        """Attest a canary only from broker-owned attempts and effect evidence."""

        run = self._identifier(run_id, "run_id")
        session = self._identifier(session_key, "session_key")
        if (
            plugin_protocol != HOOK_PLUGIN_PROTOCOL
            or plugin_implementation_revision
            != HOOK_PLUGIN_IMPLEMENTATION_REVISION
        ):
            raise OpenClawHookDenied(
                "canary plugin implementation identity does not match "
                "the broker"
            )
        active_dispatch = self._active_dispatch(
            run_id=run,
            dispatch_token=dispatch_token,
        )
        if active_dispatch.get("session_key") != session:
            raise OpenClawHookDenied(
                "canary session key does not match the governed dispatch"
            )
        document = self.state_store.read_json(HOOK_STATE_FILE)
        self._validate_document(document)
        dispatch = self._mapping(document, "dispatches").get(run)
        if not isinstance(dispatch, dict):
            raise OpenClawHookDenied("canary dispatch not found")
        if dispatch != active_dispatch:
            raise OpenClawHookDenied("canary dispatch changed before attestation")
        scope = ExecutionScope.create(str(dispatch["sandbox_root"]))
        sentinel = scope.resolve_file(
            sentinel_relative_path,
            allow_missing=False,
        )
        native_candidate = Path(native_block_path).expanduser()
        if (
            not native_candidate.is_absolute()
            or native_candidate.parent != self.native_canary_root
            or native_candidate.name in {"", ".", ".."}
            or not native_candidate.name.startswith("phase3-")
            or native_candidate.is_symlink()
            or native_candidate.exists()
        ):
            raise OpenClawHookDenied(
                "native canary target is outside the empty dedicated guard root"
            )
        native_content = str(native_block_content)
        if (
            not native_content
            or "\x00" in native_content
            or len(native_content.encode("utf-8")) > 1024
        ):
            raise OpenClawHookDenied("native canary content is invalid")
        native_params_digest = canonical_sha256(
            {
                "path": str(native_candidate),
                "content": native_content,
            }
        )
        run_attempts = [
            item
            for item in self._mapping(document, "attempts").values()
            if isinstance(item, dict) and item.get("run_id") == run
        ]
        sentinel_absolute = str(sentinel.absolute)
        successful_write = False
        for item in run_attempts:
            if (
                item.get("host_tool_name") != "veyra_file_write"
                or item.get("status") != "observed_success"
            ):
                continue
            raw_invocation = item.get("invocation")
            if (
                isinstance(raw_invocation, dict)
                and raw_invocation.get("derived_targets")
                == [sentinel_absolute]
            ):
                successful_write = True
                break
        blocked_attempt = any(
            item.get("status") == "blocked"
            for item in run_attempts
        )
        native_hook_block = any(
            item.get("status") == "plugin_blocked"
            and item.get("host_tool_name") == "write"
            and isinstance(item.get("plugin_observation"), dict)
            and item["plugin_observation"].get("outcome") == "blocked"
            and item["plugin_observation"].get("authoritative") is False
            and item["plugin_observation"].get("params_digest")
            == native_params_digest
            for item in run_attempts
        )
        evidence = self.governance.run_evidence(run)
        changed_files = set(evidence.get("changed_files") or [])
        sentinel_verified = sentinel_absolute in changed_files
        metrics = self._mapping(document, "metrics")
        coverage_ok = (
            int(metrics.get("execution_started") or 0) > 0
            and int(metrics.get("started_without_reservation") or 0) == 0
        )
        if not (
            successful_write
            and blocked_attempt
            and native_hook_block
            and sentinel_verified
            and coverage_ok
        ):
            raise OpenClawHookDenied(
                "canary lacks broker allow/block, exact native hook block, "
                "independent effect, or coverage evidence"
            )
        validated_at = self._now().isoformat()
        canary_evidence = {
            "run_id": run,
            "sentinel_path_digest": canonical_sha256(
                sentinel_absolute
            ),
            "native_block_path_digest": canonical_sha256(
                str(native_candidate)
            ),
            "native_block_params_digest": native_params_digest,
            "observed_call_count": evidence.get("observed_call_count"),
            "effect_count": evidence.get("effect_count"),
            "blocked_attempt_observed": blocked_attempt,
            "native_hook_block_observed": native_hook_block,
            "started_without_reservation": int(
                metrics.get("started_without_reservation") or 0
            ),
        }
        implementation = hook_implementation_identity()

        def attest(current: dict[str, Any]) -> None:
            self._validate_document(current)
            current["canary"] = {
                "status": "validated",
                "validated_at": validated_at,
                "implementation": implementation,
                "evidence": canary_evidence,
            }

        self.state_store.mutate_json(HOOK_STATE_FILE, attest)
        return {
            "status": "validated",
            "validated_at": validated_at,
            "implementation": implementation,
            "evidence": canary_evidence,
        }

    def _invocation(
        self,
        *,
        dispatch: dict[str, Any],
        tool_call_id: str,
        host_tool_name: str,
        params: Mapping[str, Any],
    ) -> tuple[ToolInvocation, str]:
        canonical_tool, tool_kind, risk = CUSTOM_TOOL_REGISTRY[host_tool_name]
        scope = ExecutionScope.create(str(dispatch["sandbox_root"]))
        normalized_params: dict[str, Any]
        targets: list[str]
        if host_tool_name == "veyra_file_read":
            if set(params) != {"path"} or not isinstance(params.get("path"), str):
                raise OpenClawHookDenied(
                    "veyra_file_read requires exactly one string path"
                )
            scoped = scope.resolve_file(
                str(params["path"]),
                allow_missing=False,
            )
            targets = [str(scoped.absolute)]
            normalized_params = {"path": targets[0]}
        elif host_tool_name == "veyra_file_write":
            if set(params) != {"path", "content"}:
                raise OpenClawHookDenied(
                    "veyra_file_write requires exactly path and content"
                )
            if not isinstance(params.get("path"), str) or not isinstance(
                params.get("content"),
                str,
            ):
                raise OpenClawHookDenied(
                    "veyra_file_write path and content must be strings"
                )
            content_bytes = str(params["content"]).encode("utf-8")
            if len(content_bytes) > 1_000_000:
                raise OpenClawHookDenied(
                    "veyra_file_write content exceeds the sandbox budget"
                )
            scoped = scope.resolve_file(
                str(params["path"]),
                allow_missing=True,
            )
            targets = [str(scoped.absolute)]
            normalized_params = {
                "path": targets[0],
                "content": str(params["content"]),
            }
        else:
            if set(params) != {"argv"}:
                raise OpenClawHookDenied(
                    "veyra_shell_probe requires exactly argv"
                )
            argv = params.get("argv")
            if not isinstance(argv, list):
                raise OpenClawHookDenied(
                    "veyra_shell_probe argv must be a list"
                )
            normalized_params = {"argv": list(argv)}
            targets = []
        environment = {
            "executor": HOOK_EXECUTOR_ID,
            "scope_digest": scope.scope_digest,
            "policy_revision": HOOK_POLICY_REVISION,
            "registry_revision": HOOK_REGISTRY_REVISION,
        }
        invocation = ToolInvocation.create(
            binding=self._binding(dispatch),
            tool_call_id=tool_call_id,
            tool_name=canonical_tool,
            tool_kind=tool_kind,
            arguments=normalized_params,
            derived_targets=targets,
            environment=environment,
            requested_at=self._now(),
        )
        return invocation, risk

    def _execute_tool(
        self,
        *,
        dispatch: dict[str, Any],
        host_tool_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        sandbox_root = str(dispatch["sandbox_root"])
        if host_tool_name == "veyra_file_read":
            return SafeFile(
                state_store=self.state_store,
                sandbox_root=sandbox_root,
            ).read_text(str(params["path"]))
        if host_tool_name == "veyra_file_write":
            return SafeFile(
                state_store=self.state_store,
                sandbox_root=sandbox_root,
            ).write_text(
                str(params["path"]),
                str(params["content"]),
                reason="OpenClaw governed sandbox execution",
            )
        if host_tool_name == "veyra_shell_probe":
            argv = params.get("argv")
            return SafeShell(
                state_store=self.state_store,
                sandbox_root=sandbox_root,
            ).run(list(argv) if isinstance(argv, list) else [])
        raise OpenClawHookDenied("tool is not executable by Veyra")

    def _verify_effect(
        self,
        *,
        receipt: Any,
        invocation: ToolInvocation,
        host_tool_name: str,
        params: dict[str, Any],
        result: dict[str, Any],
        dispatch: dict[str, Any],
    ) -> VerifiedToolEffect:
        authorized_targets = list(invocation.derived_targets)
        changed_files: list[str] = []
        if host_tool_name == "veyra_file_write":
            independent = SafeFile(
                state_store=self.state_store,
                sandbox_root=str(dispatch["sandbox_root"]),
            ).read_text(str(params["path"]))
            expected_digest = canonical_sha256(
                str(params["content"]).encode("utf-8").hex()
            )
            actual_content = independent.get("content")
            actual_digest = (
                canonical_sha256(str(actual_content).encode("utf-8").hex())
                if independent.get("status") == "ok"
                and isinstance(actual_content, str)
                else ""
            )
            if (
                expected_digest != actual_digest
                or independent.get("content_digest")
                != result.get("content_digest")
            ):
                raise OpenClawToolBrokerError(
                    "sandbox write failed independent content verification"
                )
            changed_files = authorized_targets
            summary = "Verified one exact sandbox file write."
        elif host_tool_name == "veyra_file_read":
            if not isinstance(result.get("content_digest"), str):
                raise OpenClawToolBrokerError(
                    "sandbox read lacks a content digest"
                )
            summary = "Verified one exact sandbox file read."
        else:
            if result.get("termination_reason") is not None:
                raise OpenClawToolBrokerError(
                    "shell probe terminated before a verified result"
                )
            summary = "Verified one bounded direct R0 sandbox probe."
        return VerifiedToolEffect.create(
            source=HOOK_EXECUTOR_ID,
            observed_at=self._now(),
            receipt_id=receipt.receipt_id,
            run_id=receipt.run_id,
            tool_call_id=receipt.tool_call_id,
            tool_name=receipt.tool_name,
            invocation_digest=receipt.invocation_digest,
            result_digest=receipt.result_digest,
            targets_digest=receipt.targets_digest,
            authorized_targets=authorized_targets,
            summary=summary,
            changed_files=changed_files,
        )

    def _public_tool_result(
        self,
        host_tool_name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        common = {
            key: result.get(key)
            for key in (
                "status",
                "reason",
                "path",
                "relative_path",
                "operation",
                "content_bytes",
                "content_digest",
                "replaced_existing",
                "scope_digest",
                "stdout",
                "stderr",
                "returncode",
                "command",
                "executable_digest",
                "termination_reason",
                "timeout_seconds",
                "max_output_bytes",
            )
            if key in result
        }
        if host_tool_name == "veyra_file_read" and "content" in result:
            common["content"] = result.get("content")
        encoded = json.dumps(
            common,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise OpenClawToolBrokerError(
                "tool result exceeds the hook response budget"
            )
        return common

    def _complete_attempt(
        self,
        *,
        attempt_id: str,
        state: str,
        result_digest: str,
    ) -> None:
        def complete(document: dict[str, Any]) -> None:
            self._validate_document(document)
            attempt = self._mapping(document, "attempts").get(attempt_id)
            if not isinstance(attempt, dict):
                raise OpenClawHookConflict("attempt disappeared during execution")
            if attempt.get("status") == state:
                if attempt.get("result_digest") != result_digest:
                    raise OpenClawHookConflict(
                        "attempt already has another result digest"
                    )
                return
            if attempt.get("status") != "executing":
                raise OpenClawHookConflict(
                    "attempt was not in executing state"
                )
            attempt["status"] = state
            attempt["result_digest"] = result_digest
            attempt["execution_completed_at"] = self._now().isoformat()
            if state == "observed_success":
                self._increment(document, "execution_completed")
            else:
                self._increment(document, "execution_failed")
            run_id = str(attempt.get("run_id") or "")
            dispatch = self._mapping(document, "dispatches").get(run_id)
            if (
                isinstance(dispatch, dict)
                and dispatch.get("status") == "cancellation_requested"
                and not any(
                    isinstance(candidate, dict)
                    and candidate.get("run_id") == run_id
                    and candidate.get("status") == "executing"
                    for candidate in self._mapping(
                        document,
                        "attempts",
                    ).values()
                )
            ):
                dispatch["status"] = "cancelled"
                dispatch["cancelled_at"] = self._now().isoformat()

        self.state_store.mutate_json(HOOK_STATE_FILE, complete)

    def _mark_indeterminate(
        self,
        *,
        attempt_id: str,
        reservation_id: str,
        reservation_token: str,
        reason: str,
    ) -> None:
        try:
            self.governance.complete_scoped_execution(
                reservation_id=reservation_id,
                reservation_token=reservation_token,
                state="indeterminate",
            )
        except Exception:
            pass

        def mark(document: dict[str, Any]) -> None:
            self._validate_document(document)
            attempt = self._mapping(document, "attempts").get(attempt_id)
            if not isinstance(attempt, dict):
                return
            if attempt.get("status") == "executing":
                attempt["status"] = "indeterminate"
                attempt["failure_reason"] = str(reason or "")[:600]
                attempt["execution_completed_at"] = self._now().isoformat()
                self._increment(document, "execution_failed")
                run_id = str(attempt.get("run_id") or "")
                dispatch = self._mapping(document, "dispatches").get(run_id)
                if (
                    isinstance(dispatch, dict)
                    and dispatch.get("status") == "cancellation_requested"
                    and not any(
                        isinstance(candidate, dict)
                        and candidate.get("run_id") == run_id
                        and candidate.get("status") == "executing"
                        for candidate in self._mapping(
                            document,
                            "attempts",
                        ).values()
                    )
                ):
                    dispatch["status"] = "cancelled"
                    dispatch["cancelled_at"] = self._now().isoformat()

        self.state_store.mutate_json(HOOK_STATE_FILE, mark)

    def _attempt(self, run_id: str, tool_call_id: str) -> dict[str, Any]:
        document = self.state_store.read_json(HOOK_STATE_FILE)
        self._validate_document(document)
        attempt_id = self._mapping(document, "call_index").get(
            self._call_key(run_id, tool_call_id)
        )
        attempt = (
            self._mapping(document, "attempts").get(attempt_id)
            if isinstance(attempt_id, str)
            else None
        )
        if not isinstance(attempt, dict):
            raise OpenClawHookDenied("tool call has no preflight reservation")
        return dict(attempt)

    def _active_dispatch(
        self,
        *,
        run_id: str,
        dispatch_token: str,
        allow_expired: bool = False,
        allow_inactive: bool = False,
    ) -> dict[str, Any]:
        run = self._identifier(run_id, "run_id")
        token_digest = secret_sha256(dispatch_token)
        document = self.state_store.read_json(HOOK_STATE_FILE)
        self._validate_document(document)
        dispatch = self._mapping(document, "dispatches").get(run)
        if not isinstance(dispatch, dict):
            raise OpenClawHookDenied("governed dispatch not found")
        if not secrets.compare_digest(
            str(dispatch.get("dispatch_token_digest") or ""),
            token_digest,
        ):
            raise OpenClawHookDenied("dispatch token is invalid")
        if not allow_inactive and dispatch.get("status") != "active":
            raise OpenClawHookDenied("governed dispatch is not active")
        if not allow_expired and self._now() >= self._parse_time(
            dispatch.get("expires_at")
        ):
            raise OpenClawHookDenied("governed dispatch expired")
        return dict(dispatch)

    def _record_blocked_preflight(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        reason: str,
    ) -> None:
        attempt_id = (
            f"blocked_{canonical_sha256({'run': run_id, 'call': tool_call_id, 'tool': tool_name})[:24]}"
        )

        def persist(document: dict[str, Any]) -> None:
            self._validate_document(document)
            call_key = self._call_key(run_id, tool_call_id)
            call_index = self._mapping(document, "call_index")
            if call_key in call_index:
                return
            self._mapping(document, "attempts")[attempt_id] = {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "host_tool_name": tool_name,
                "status": "blocked",
                "reason": reason,
                "preflight_at": self._now().isoformat(),
            }
            call_index[call_key] = attempt_id
            self._increment(document, "preflight_total")
            self._increment(document, "preflight_blocked")

        try:
            self.state_store.mutate_json(HOOK_STATE_FILE, persist)
        finally:
            self._audit(
                "hook_preflight_blocked",
                {
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "reason": reason,
                },
            )

    def _validate_state(self) -> None:
        document = self.state_store.read_json(HOOK_STATE_FILE)
        self._validate_document(document)

    @staticmethod
    def _binding(dispatch: Mapping[str, Any]) -> GovernedSessionBinding:
        raw = dispatch.get("binding")
        if not isinstance(raw, dict):
            raise OpenClawHookDenied("dispatch binding is missing")
        return GovernedSessionBinding.model_validate_json(
            json.dumps(raw, ensure_ascii=False, allow_nan=False),
            strict=True,
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise OpenClawHookDenied("dispatch time is missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise OpenClawHookDenied("dispatch time must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("hook broker clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _identifier(value: Any, field_name: str) -> str:
        normalized = str(value or "")
        if (
            not normalized
            or normalized != normalized.strip()
            or "\x00" in normalized
            or len(normalized) > 600
        ):
            raise ValueError(f"{field_name} must be a normalized identifier")
        return normalized

    @staticmethod
    def _call_key(run_id: str, tool_call_id: str) -> str:
        return canonical_sha256(
            {"run_id": run_id, "tool_call_id": tool_call_id}
        )

    @staticmethod
    def _mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
        value = document.get(key)
        if not isinstance(value, dict):
            raise OpenClawToolBrokerError(
                f"hook state field {key} must be an object"
            )
        return value

    @classmethod
    def _increment(
        cls,
        document: dict[str, Any],
        key: str,
    ) -> None:
        metrics = cls._mapping(document, "metrics")
        metrics[key] = int(metrics.get(key) or 0) + 1

    @staticmethod
    def _validate_document(document: dict[str, Any]) -> None:
        if document.get("schema_version") != HOOK_STATE_SCHEMA_VERSION:
            raise OpenClawToolBrokerError(
                "OpenClaw hook state schema is missing or unsupported"
            )
        for key in (
            "dispatches",
            "attempts",
            "call_index",
            "execution_token_index",
            "metrics",
        ):
            if not isinstance(document.get(key), dict):
                raise OpenClawToolBrokerError(
                    f"hook state field {key} must be an object"
                )

    @staticmethod
    def _bounded_reason(exc: Exception) -> str:
        text = str(exc or exc.__class__.__name__).strip()
        return text[:600] or exc.__class__.__name__

    def _audit(self, status: str, artifacts: dict[str, Any]) -> None:
        self.state_store.append_jsonl(
            "tool_call_log.jsonl",
            {
                "route": "openclaw_tool_hook",
                "status": status,
                "artifacts": artifacts,
            },
        )


__all__ = [
    "CUSTOM_TOOL_REGISTRY",
    "HookDispatchRegistration",
    "OpenClawHookConflict",
    "OpenClawHookDenied",
    "OpenClawToolBroker",
    "OpenClawToolBrokerError",
]
