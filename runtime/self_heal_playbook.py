from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from core.autonomy_policy import AutonomyLevel, DomainAutonomyProfile
from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from probes.openclaw_probe import OpenClawProbe
from runtime.authority_fence import (
    agent_transport_authority_fence,
    clear_agent_transport_call_inflight,
    mark_agent_transport_call_inflight,
)


PLAYBOOK_ID = "self_heal.openclaw_reconnect.v1"
STATE_SCHEMA_VERSION = "veyra.self_heal_state.v1"
_SUCCESS_STATUSES = {"available", "ok", "success"}
_COMPATIBLE_STATUSES = {"compatible"}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: dict[str, threading.RLock] = {}


OPENCLAW_RECONNECT_PROFILE = DomainAutonomyProfile(
    profile_id="aut.runtime_health.openclaw_reconnect.v1",
    policy_version=1,
    level=AutonomyLevel.A2,
    enabled=True,
    revoked=False,
    user_scope="local-user",
    domain="runtime_health",
    environment="local",
    capabilities=("agent.status.read", "agent.capabilities.refresh", "agent.reconnect"),
    target_scope="selected_agent:openclaw",
    risk_ceiling=RiskLevel.R1,
    max_attempts=2,
    cooldown_seconds=300,
    allowed_modes=("shadow", "scoped_canary"),
)


@dataclass(frozen=True, slots=True)
class PlaybookSpec:
    playbook_id: str = PLAYBOOK_ID
    version: int = 1
    desired_state: str = "selected_agent.available"
    risk_floor: str = RiskLevel.R1.value
    max_attempts: int = OPENCLAW_RECONNECT_PROFILE.max_attempts
    cooldown_seconds: int = OPENCLAW_RECONNECT_PROFILE.cooldown_seconds
    confirmation_interval_seconds: int = 1
    confirmation_window_seconds: int = 360
    operation_timeout_seconds: int = 90

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "version": self.version,
            "desired_state": self.desired_state,
            "trigger_claims": ["agent_runtime.status=unavailable"],
            "preconditions": [
                "two distinct fresh observation rounds failed",
                "each round contains failed TCP and Gateway protocol probes",
                "no active governed OpenClaw side effect",
            ],
            "allowed_capabilities": list(
                OPENCLAW_RECONNECT_PROFILE.capabilities
            ),
            "risk_floor": self.risk_floor,
            "max_attempts": self.max_attempts,
            "cooldown_seconds": self.cooldown_seconds,
            "confirmation_interval_seconds": self.confirmation_interval_seconds,
            "confirmation_window_seconds": self.confirmation_window_seconds,
            "verification": [
                "fresh local OpenClaw TCP probe listening",
                "fresh OpenClaw Gateway capability snapshot compatible",
            ],
            "success_condition": "both verifications pass",
            "stop_conditions": [
                "scope change",
                "unexpected runtime identity",
                "indeterminate operation",
                "second failed attempt",
            ],
            "fallback": "create one R4 agent restart review",
        }


class OpenClawReconnectPlaybook:
    """The first Phase 5 self-heal playbook.

    It can refresh the selected local OpenClaw transport and capability cache.
    It cannot invoke an Agent, execute a tool, restart a process, switch provider,
    or widen its own immutable A2 authority profile.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        adapter_resolver: Callable[[], Any] | None,
        review_creator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        probe_runner: Callable[[int], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        call_timeout_seconds: float = 25.0,
    ) -> None:
        self.state_store = state_store
        self.adapter_resolver = adapter_resolver
        self.review_creator = review_creator
        self.probe_runner = probe_runner or (
            lambda port: OpenClawProbe().run(str(port))
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.call_timeout_seconds = max(
            0.05,
            min(float(call_timeout_seconds), 30.0),
        )
        self.spec = PlaybookSpec()
        lock_key = f"{state_store.root.resolve()}::{PLAYBOOK_ID}"
        with _RUN_LOCKS_GUARD:
            self._run_lock = _RUN_LOCKS.setdefault(lock_key, threading.RLock())

    def run(
        self,
        *,
        port_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._run_lock:
            with agent_transport_authority_fence(self.state_store):
                try:
                    return self._run_locked(
                        port_observation=port_observation
                    )
                except Exception as exc:
                    # Foreground proactive checks remain usable, but recovery
                    # fails closed and arbitrary exception strings never enter
                    # public state.
                    return self._fault_result(type(exc).__name__)

    def status(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json("self_heal_state.json")
            if not self._valid_state(state):
                return self._fault_result("invalid_state")
            record = self._record_from_state(state)
            return self._public_result(record)
        except Exception as exc:
            return self._fault_result(type(exc).__name__)

    def configured_port(self) -> int | None:
        """Return the exact selected local OpenClaw port without probing it."""

        try:
            target = self._target_context(self._resolve_adapter())
        except Exception:
            return None
        port = target.get("port")
        return (
            port
            if target.get("applicable")
            and type(port) is int
            and 0 < port <= 65535
            else None
        )

    def note_review(self, review_id: str) -> None:
        if not review_id:
            return

        def link(state: dict[str, Any]) -> None:
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if record.get("breaker_open"):
                record["review_id"] = review_id
                record["updated_at"] = self._now_iso()

        self.state_store.mutate_json("self_heal_state.json", link)

    def allow_post_review_observation(self, review_id: str = "") -> None:
        """Allow L1 observation after an explicitly reviewed restart/reconnect."""

        def reopen_observation(state: dict[str, Any]) -> None:
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if review_id and record.get("review_id") not in {None, "", review_id}:
                return
            record.update(
                {
                    "status": "observing",
                    "breaker_open": False,
                    "cooldown_until": None,
                    "operation": None,
                    "attempt_count": 0,
                    "failure_confirmation_count": 0,
                    "failure_confirmations": [],
                    "incident_id": None,
                    "updated_at": self._now_iso(),
                }
            )

        self.state_store.mutate_json(
            "self_heal_state.json",
            reopen_observation,
        )

    def _run_locked(
        self,
        *,
        port_observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # A caller may already have a coarse port result for its own report, but
        # a playbook confirmation must be generated inside this run. Reusing a
        # caller-owned object would let stale evidence count as a fresh round.
        del port_observation
        state = self.state_store.read_json("self_heal_state.json")
        if not self._valid_state(state):
            return self._fault_result("invalid_state")

        mode_context = self._mode_context()
        mode = str(mode_context["mode"])
        mode_epoch = int(mode_context["mode_epoch"])
        if mode not in {
            "disabled",
            "record_only",
            "shadow",
            "scoped_canary",
        }:
            return self._fault_result("invalid_mode")
        if mode == "disabled":
            record = self._set_non_active_status(
                    "disabled",
                    mode=mode,
                    mode_epoch=mode_epoch,
            )
            result = self._public_result(record, mode=mode)
            return (
                self._ensure_review(result)
                if record.get("breaker_open")
                else result
            )

        adapter = self._resolve_adapter()
        target = self._target_context(adapter)
        if not target.get("applicable"):
            status = str(target.get("status") or "not_applicable")
            record = self._set_non_active_status(
                    status,
                    mode=mode,
                    mode_epoch=mode_epoch,
            )
            result = self._public_result(record, mode=mode)
            return (
                self._ensure_review(result)
                if record.get("breaker_open")
                else result
            )

        record = self._bind_target(
            str(target["binding_digest"]),
            identity_scope_digest=str(target["identity_scope_digest"]),
            mode=mode,
            mode_epoch=mode_epoch,
        )
        if record.get("status") == "indeterminate":
            return self._ensure_review(
                self._public_result(record, mode=mode)
            )
        operation = (
            record.get("operation")
            if isinstance(record.get("operation"), dict)
            else {}
        )
        if operation.get("state") == "claimed":
            if not self._operation_expired(operation):
                return self._public_result(record, mode=mode)
            record = self._mark_indeterminate(str(operation.get("operation_id")))
            result = self._public_result(record, mode=mode)
            return self._ensure_review(result)

        cooldown_blocked = bool(
            record.get("status") == "cooldown"
            and not self._cooldown_elapsed(record)
        )
        breaker_open = bool(record.get("breaker_open"))
        if (
            cooldown_blocked or breaker_open
        ) and not self._observation_due(record):
            result = self._public_result(record, mode=mode)
            return self._ensure_review(result) if breaker_open else result

        if mode == "record_only":
            return self._public_result(
                self._record_observation(
                    observations=[],
                    status="record_only",
                    qualifies=False,
                    increment_failure=False,
                ),
                mode=mode,
            )

        for capability in (
            "agent.status.read",
            "agent.capabilities.refresh",
        ):
            if not OPENCLAW_RECONNECT_PROFILE.permits(
                capability=capability,
                risk_level=RiskLevel.R1,
                user_scope="local-user",
                environment="local",
                target_scope="selected_agent:openclaw",
                mode=mode,
                attempt_count=int(record.get("attempt_count") or 0),
                cooldown_elapsed=self._cooldown_elapsed(record),
                now=self._now(),
            ):
                return self._fault_result("autonomy_profile_denied")

        round_id = f"obs_{uuid4().hex[:16]}"
        observed_at = self._now_iso()
        tcp = self._port_observation(
            port=int(target["port"]),
            observed_at=observed_at,
            round_id=round_id,
        )
        gateway = self._gateway_observation(
            adapter,
            observed_at=observed_at,
            round_id=round_id,
        )
        observations = [tcp, gateway]

        if not self._scope_matches(
            target_binding_digest=str(target["binding_digest"]),
            mode=mode,
            mode_epoch=mode_epoch,
        ):
            return self._public_result(
                {**record, "status": "scope_changed"},
                mode=mode,
            )

        if tcp["passed"] and gateway["passed"]:
            # Scope validation and the authoritative executor projection share
            # one state transaction, so disable/config writes cannot land in
            # the check-to-commit window.
            with self.state_store.writer_transaction():
                if not self._scope_matches(
                    target_binding_digest=str(target["binding_digest"]),
                    mode=mode,
                    mode_epoch=mode_epoch,
                ):
                    return self._public_result(
                        {**record, "status": "scope_changed"},
                        mode=mode,
                    )
                current_state = self.state_store.read_json(
                    "self_heal_state.json"
                )
                current_record = (
                    current_state.get("playbooks", {}).get(PLAYBOOK_ID, {})
                    if isinstance(current_state.get("playbooks"), dict)
                    else {}
                )
                expected_identity = str(
                    (
                        current_record
                        if isinstance(current_record, dict)
                        else {}
                    ).get("runtime_identity_digest")
                    or ""
                )
                observed_identity = str(
                    self._runtime_identity_digest(
                        gateway.get("capability_snapshot")
                    )
                    or ""
                )
                identity_mismatch = bool(
                    expected_identity
                    and observed_identity != expected_identity
                )
                if identity_mismatch:
                    record = self._record_identity_mismatch(
                        observations=observations,
                        round_id=round_id,
                        target_binding_digest=str(
                            target["binding_digest"]
                        ),
                    )
                else:
                    record = self._record_healthy(
                        observations=observations,
                        round_id=round_id,
                        target_binding_digest=str(
                            target["binding_digest"]
                        ),
                        capability_snapshot=gateway.get(
                            "capability_snapshot"
                        ),
                        outcome=(
                            "already_healthy"
                            if mode == "scoped_canary"
                            else "shadow_healthy"
                        ),
                        status=(
                            "healthy"
                            if mode == "scoped_canary"
                            else "shadow_healthy"
                        ),
                    )
                if mode == "scoped_canary" and not identity_mismatch:
                    self._project_executor_healthy(
                        gateway.get("capability_snapshot"),
                        observed_at=observed_at,
                    )
            result = self._public_result(record, mode=mode)
            return (
                self._ensure_review(result)
                if record.get("breaker_open")
                else result
            )

        both_failed = not tcp["passed"] and not gateway["passed"]
        if breaker_open:
            record = self._record_observation(
                observations=observations,
                round_id=round_id,
                status="breaker_open",
                qualifies=both_failed,
                increment_failure=False,
            )
            return self._ensure_review(self._public_result(record, mode=mode))
        if cooldown_blocked:
            record = self._record_observation(
                observations=observations,
                round_id=round_id,
                status="cooldown",
                qualifies=both_failed,
                increment_failure=False,
            )
            return self._public_result(record, mode=mode)
        if not both_failed or mode != "scoped_canary":
            record = self._record_observation(
                observations=observations,
                round_id=round_id,
                status=(
                    "observing"
                    if mode == "scoped_canary"
                    else "shadow_qualified"
                    if both_failed
                    else "shadow"
                ),
                qualifies=False,
                increment_failure=False,
            )
            return self._public_result(record, mode=mode)

        record = self._record_observation(
            observations=observations,
            round_id=round_id,
            status="confirming_failure",
            qualifies=True,
            increment_failure=True,
        )
        if int(record.get("failure_confirmation_count") or 0) < 2:
            return self._public_result(record, mode=mode)

        # The dedicated authority fence is shared by governed Agent admission.
        # Unlike the state RLock, it does not deadlock when the Gateway snapshot
        # reads governance state from the bounded worker thread.
        with agent_transport_authority_fence(self.state_store):
            with self.state_store.writer_transaction():
                if not self._scope_matches(
                    target_binding_digest=str(target["binding_digest"]),
                    mode=mode,
                    mode_epoch=mode_epoch,
                ):
                    return self._public_result(
                        {**record, "status": "scope_changed"},
                        mode=mode,
                    )
                if not self._activity_safe(gateway_observation=gateway):
                    record = self._record_observation(
                        observations=observations,
                        round_id=round_id,
                        status="blocked_active_effects",
                        qualifies=True,
                        increment_failure=False,
                    )
                    return self._public_result(record, mode=mode)

                if not OPENCLAW_RECONNECT_PROFILE.permits(
                    capability="agent.reconnect",
                    risk_level=RiskLevel.R1,
                    user_scope="local-user",
                    environment="local",
                    target_scope="selected_agent:openclaw",
                    mode=mode,
                    attempt_count=int(record.get("attempt_count") or 0),
                    cooldown_elapsed=self._cooldown_elapsed(record),
                    now=self._now(),
                ):
                    return self._fault_result("autonomy_profile_denied")

                operation_id = f"healop_{uuid4().hex[:16]}"
                incident_id = str(
                    record.get("incident_id") or f"heal_{uuid4().hex[:16]}"
                )
                claimed, record = self._claim_attempt(
                    operation_id=operation_id,
                    incident_id=incident_id,
                    observations=observations,
                    target_binding_digest=str(target["binding_digest"]),
                )
                if not claimed:
                    result = self._public_result(record, mode=mode)
                    return (
                        self._ensure_review(result)
                        if record.get("breaker_open")
                        else result
                    )

            recovery = self._recover(
                adapter,
                port=int(target["port"]),
                observed_at=self._now_iso(),
                round_id=f"verify_{uuid4().hex[:16]}",
            )
            with self.state_store.writer_transaction():
                if not self._scope_matches(
                    target_binding_digest=str(target["binding_digest"]),
                    mode=mode,
                    mode_epoch=mode_epoch,
                ):
                    record = self._finish_scope_changed(operation_id)
                    return self._public_result(record, mode=mode)

                accepted, record = self._finish_attempt(
                    operation_id=operation_id,
                    recovery=recovery,
                )
                if not accepted:
                    return self._public_result(record, mode=mode)
                if recovery["passed"]:
                    self._project_executor_healthy(
                        recovery.get("capability_snapshot"),
                        observed_at=str(
                            recovery.get("observed_at") or self._now_iso()
                        ),
                    )
                elif (
                    not recovery.get("indeterminate")
                    and not recovery.get("identity_mismatch")
                ):
                    self._project_executor_unavailable(
                        observed_at=str(
                            recovery.get("observed_at") or self._now_iso()
                        )
                    )
                result = self._public_result(record, mode=mode)
        return self._ensure_review(result) if record.get("breaker_open") else result

    def _recover(
        self,
        adapter: Any,
        *,
        port: int,
        observed_at: str,
        round_id: str,
    ) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        capability_error = ""
        def refresh() -> Any:
            invalidate = getattr(adapter, "invalidate_capabilities_cache", None)
            fetch = getattr(adapter, "fetch_capabilities", None)
            if not callable(invalidate) or not callable(fetch):
                raise AttributeError("unsupported_adapter_contract")
            invalidate()
            return fetch(force_refresh=True)

        completed, value, error_code = self._bounded_call(refresh)
        if completed:
            capabilities = value if isinstance(value, dict) else {}
        else:
            capability_error = (
                "force_refresh_not_supported"
                if error_code == "TypeError"
                else "unsupported_adapter_contract"
                if error_code == "AttributeError"
                else error_code
            )

        tcp = self._port_observation(
            port=port,
            observed_at=observed_at,
            round_id=round_id,
        )
        capability = self._capability_observation(
            capabilities,
            observed_at=observed_at,
            round_id=round_id,
            error_code=capability_error,
        )
        return {
            "passed": bool(tcp["passed"] and capability["passed"]),
            "indeterminate": capability_error in {"timeout", "call_inflight"},
            "observed_at": observed_at,
            "round_id": round_id,
            "verifiers": [tcp, capability],
            "capability_snapshot": capability.get("capability_snapshot"),
            "runtime_identity_digest": self._runtime_identity_digest(
                capability.get("capability_snapshot")
            ),
        }

    def _gateway_observation(
        self,
        adapter: Any,
        *,
        observed_at: str,
        round_id: str,
    ) -> dict[str, Any]:
        connection: dict[str, Any] = {}
        error_code = ""
        call = getattr(adapter, "connection_status", None)
        if not callable(call):
            error_code = "unsupported_adapter_contract"
        else:
            completed, value, call_error = self._bounded_call(
                lambda: call(force_refresh=True)
            )
            if completed:
                connection = value if isinstance(value, dict) else {}
            else:
                error_code = (
                    "force_refresh_not_supported"
                    if call_error == "TypeError"
                    else call_error
                )
        capabilities = (
            connection.get("capabilities")
            if isinstance(connection.get("capabilities"), dict)
            else {}
        )
        capability = self._capability_observation(
            capabilities,
            observed_at=observed_at,
            round_id=round_id,
            error_code=error_code,
        )
        connected = bool(
            connection.get("connected")
            and str(connection.get("status") or "") in _SUCCESS_STATUSES
        )
        passed = bool(connected and capability["passed"])
        return {
            "source": "openclaw_gateway_protocol",
            "status": "available" if passed else self._safe_status(connection),
            "passed": passed,
            "observed_at": observed_at,
            "round_id": round_id,
            "evidence_kind": "real_force_refresh",
            "error_code": error_code or None,
            "active_task_count": capability.get("active_task_count"),
            "active_task_count_malformed": capability.get(
                "active_task_count_malformed"
            ),
            "capability_snapshot": capability.get("capability_snapshot"),
        }

    def _capability_observation(
        self,
        capabilities: dict[str, Any],
        *,
        observed_at: str,
        round_id: str,
        error_code: str = "",
    ) -> dict[str, Any]:
        compatibility = (
            capabilities.get("compatibility")
            if isinstance(capabilities.get("compatibility"), dict)
            else {}
        )
        raw = (
            capabilities.get("raw")
            if isinstance(capabilities.get("raw"), dict)
            else {}
        )
        health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
        server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
        gateway_status = (
            raw.get("gateway_status")
            if isinstance(raw.get("gateway_status"), dict)
            else {}
        )
        gateway_tasks = (
            gateway_status.get("tasks")
            if isinstance(gateway_status.get("tasks"), dict)
            else {}
        )
        active_task_count = self._nonnegative_int_or_none(
            gateway_tasks.get("active")
        )
        active_task_count_malformed = bool(
            "gateway_status" in raw
            and (
                not isinstance(raw.get("gateway_status"), dict)
                or not isinstance(gateway_status.get("tasks"), dict)
                or active_task_count is None
            )
        )
        required_methods = (
            compatibility.get("required_methods")
            if isinstance(compatibility.get("required_methods"), dict)
            else {}
        )
        features = (
            capabilities.get("features")
            if isinstance(capabilities.get("features"), dict)
            else {}
        )
        status = str(capabilities.get("status") or "unavailable")
        runtime = str(capabilities.get("runtime") or "")
        compatible = str(compatibility.get("status") or "")
        passed = bool(
            runtime == "openclaw"
            and capabilities.get("connected") is True
            and status in _SUCCESS_STATUSES
            and isinstance(capabilities.get("contract_version"), str)
            and bool(str(capabilities.get("contract_version") or "").strip())
            and compatible in _COMPATIBLE_STATUSES
            and health.get("ok") is True
            and isinstance(server.get("method_count"), int)
            and not isinstance(server.get("method_count"), bool)
            and int(server["method_count"]) > 0
            and type(server.get("protocol")) in {int, str}
            and not isinstance(server.get("protocol"), bool)
            and str(server.get("protocol") or "")
            and isinstance(server.get("version"), str)
            and bool(str(server.get("version") or "").strip())
            and required_methods.get("chat.send") is True
            and all(value is True for value in required_methods.values())
            and features.get("agent_dialogue_v1") is True
            and features.get("bounded_agent_dialogue") is True
            and features.get("caller_supplied_run_id") is True
            and features.get("idempotent_submit") is True
            and features.get("exact_stop") is True
            and features.get("enforced_execution_profile")
            == "phase3_sandbox_proposal"
            and features.get("tool_proxy_enforced") is True
            and active_task_count == 0
        )
        snapshot = None
        if passed:
            safe_features = {
                str(key): bool(value)
                for key, value in list(features.items())[:80]
                if isinstance(key, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key)
                and isinstance(value, bool)
            }
            safe_features["enforced_execution_profile"] = (
                "phase3_sandbox_proposal"
            )
            snapshot = {
                "runtime": "openclaw",
                "status": status,
                "connected": True,
                "protocol": str(
                    capabilities.get("protocol") or "openclaw_gateway_ws"
                ),
                "contract_version": capabilities.get("contract_version"),
                "compatibility": {"status": compatible},
                "health": {"ok": True},
                "server": {
                    "method_count": int(server["method_count"]),
                    "protocol": server.get("protocol"),
                    "version": str(server.get("version") or "")[:80],
                },
                "gateway_status": {
                    "active_task_count": active_task_count,
                },
                "features": safe_features,
                "tools": self._safe_catalog_ids(raw.get("tools")),
                "skills": self._safe_catalog_ids(raw.get("skills")),
                "source": "openclaw_gateway_force_refresh",
                "freshness": "fresh",
                "observed_at": observed_at,
                "refreshed_at": observed_at,
                "updated_at": observed_at,
                "ttl_seconds": 300,
            }
        return {
            "source": "openclaw_capability_snapshot",
            "status": status if status in _SUCCESS_STATUSES else "unavailable",
            "passed": passed,
            "observed_at": observed_at,
            "round_id": round_id,
            "evidence_kind": "real_force_refresh",
            "error_code": error_code or None,
            "active_task_count": active_task_count,
            "active_task_count_malformed": active_task_count_malformed,
            "capability_snapshot": snapshot,
        }

    @staticmethod
    def _safe_catalog_ids(value: Any) -> list[str]:
        source = value if isinstance(value, dict) else {}
        items = source.get("items") if isinstance(source.get("items"), list) else []
        output: list[str] = []
        for item in items[:120]:
            candidate = (
                item.get("id")
                if isinstance(item, dict)
                else item
                if isinstance(item, str)
                else None
            )
            text = str(candidate or "")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", text):
                output.append(text)
        return output

    @staticmethod
    def _nonnegative_int_or_none(value: Any) -> int | None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    def _port_observation(
        self,
        *,
        port: int,
        observed_at: str,
        round_id: str,
    ) -> dict[str, Any]:
        error_code = ""
        value: dict[str, Any] = {}
        try:
            raw = self.probe_runner(port)
            value = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            error_code = type(exc).__name__
        validation = (
            value.get("validation")
            if isinstance(value.get("validation"), dict)
            else {}
        )
        details = (
            value.get("details")
            if isinstance(value.get("details"), dict)
            else {}
        )
        passed = bool(
            value.get("source") == "openclaw_probe"
            and value.get("target") == "openclaw_runtime"
            and value.get("status") == "listening"
            and validation.get("source") == "real_probe"
            and validation.get("observed") is True
            and type(details.get("port")) is int
            and details.get("port") == port
        )
        return {
            "source": "openclaw_tcp_probe",
            "status": "listening" if passed else "unavailable",
            "passed": passed,
            "observed_at": observed_at,
            "round_id": round_id,
            "evidence_kind": "real_probe",
            "error_code": error_code or None,
        }

    def _claim_attempt(
        self,
        *,
        operation_id: str,
        incident_id: str,
        observations: list[dict[str, Any]],
        target_binding_digest: str,
    ) -> tuple[bool, dict[str, Any]]:
        claimed = False
        selected: dict[str, Any] = {}

        def claim(state: dict[str, Any]) -> None:
            nonlocal claimed, selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if record.get("target_binding_digest") != target_binding_digest:
                selected = dict(record)
                return
            active = (
                record.get("operation")
                if isinstance(record.get("operation"), dict)
                else {}
            )
            if active.get("state") == "claimed":
                selected = dict(record)
                return
            if record.get("breaker_open") or not self._cooldown_elapsed(record):
                selected = dict(record)
                return
            attempts = int(record.get("attempt_count") or 0)
            if attempts >= self.spec.max_attempts:
                record.update(
                    {
                        "status": "breaker_open",
                        "breaker_open": True,
                        "updated_at": self._now_iso(),
                    }
                )
                selected = dict(record)
                return
            attempt_number = attempts + 1
            record.update(
                {
                    "status": "attempt_in_progress",
                    "incident_id": incident_id,
                    "attempt_count": attempt_number,
                    "breaker_open": False,
                    "cooldown_until": None,
                    "last_observation": {
                        "qualifies": True,
                        "sources": observations,
                        "round_id": self._observation_round_id(observations),
                        "observed_at": self._now_iso(),
                    },
                    "operation": {
                        "operation_id": operation_id,
                        "state": "claimed",
                        "attempt_number": attempt_number,
                        "claimed_at": self._now_iso(),
                    },
                    "updated_at": self._now_iso(),
                }
            )
            claimed = True
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", claim)
        return claimed, selected

    def _finish_attempt(
        self,
        *,
        operation_id: str,
        recovery: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        selected: dict[str, Any] = {}
        accepted = False

        def finish(state: dict[str, Any]) -> None:
            nonlocal accepted, selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            operation = (
                record.get("operation")
                if isinstance(record.get("operation"), dict)
                else {}
            )
            if (
                operation.get("operation_id") != operation_id
                or operation.get("state") != "claimed"
            ):
                selected = dict(record)
                return
            accepted = True
            attempt_count = int(record.get("attempt_count") or 0)
            indeterminate = recovery.get("indeterminate") is True
            expected_identity = str(
                record.get("runtime_identity_digest") or ""
            )
            observed_identity = str(
                recovery.get("runtime_identity_digest") or ""
            )
            identity_mismatch = bool(
                recovery.get("passed") is True
                and expected_identity
                and observed_identity != expected_identity
            )
            if identity_mismatch:
                recovery["passed"] = False
                recovery["identity_mismatch"] = True
            operation["state"] = (
                "stopped"
                if identity_mismatch
                else "indeterminate"
                if indeterminate
                else "completed"
            )
            operation["completed_at"] = self._now_iso()
            operation["outcome"] = (
                "identity_mismatch"
                if identity_mismatch
                else "indeterminate"
                if indeterminate
                else "recovered"
                if recovery.get("passed")
                else "failed"
            )
            record["last_verification"] = {
                "passed": bool(recovery.get("passed")),
                "observed_at": recovery.get("observed_at"),
                "sources": recovery.get("verifiers"),
                "runtime_identity_status": (
                    "mismatch"
                    if identity_mismatch
                    else "matched"
                    if expected_identity and recovery.get("passed")
                    else "established"
                    if observed_identity and recovery.get("passed")
                    else "unverified"
                ),
            }
            if identity_mismatch:
                record.update(
                    {
                        "status": "identity_mismatch",
                        "breaker_open": True,
                        "cooldown_until": None,
                        "operation": operation,
                        "last_outcome": "identity_mismatch",
                        "updated_at": self._now_iso(),
                    }
                )
            elif indeterminate:
                record.update(
                    {
                        "status": "indeterminate",
                        "breaker_open": True,
                        "cooldown_until": None,
                        "operation": operation,
                        "last_outcome": "indeterminate",
                        "updated_at": self._now_iso(),
                    }
                )
            elif recovery.get("passed"):
                record.update(
                    {
                        "status": "recovered",
                        "breaker_open": False,
                        "cooldown_until": None,
                        "incident_id": None,
                        "attempt_count": 0,
                        "failure_confirmation_count": 0,
                        "failure_confirmations": [],
                        "review_id": None,
                        "operation": operation,
                        "last_outcome": "recovered",
                        "updated_at": self._now_iso(),
                    }
                )
                if observed_identity:
                    record["runtime_identity_digest"] = observed_identity
            elif attempt_count >= self.spec.max_attempts:
                record.update(
                    {
                        "status": "breaker_open",
                        "breaker_open": True,
                        "cooldown_until": None,
                        "operation": operation,
                        "last_outcome": "failed",
                        "updated_at": self._now_iso(),
                    }
                )
            else:
                cooldown_until = self._now() + timedelta(
                    seconds=self.spec.cooldown_seconds
                )
                record.update(
                    {
                        "status": "cooldown",
                        "breaker_open": False,
                        "cooldown_until": self._iso(cooldown_until),
                        "operation": operation,
                        "last_outcome": "failed",
                        "updated_at": self._now_iso(),
                    }
                )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", finish)
        return accepted, selected

    def _finish_scope_changed(self, operation_id: str) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def stop(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            operation = (
                record.get("operation")
                if isinstance(record.get("operation"), dict)
                else {}
            )
            if operation.get("operation_id") == operation_id:
                operation.update(
                    {
                        "state": "stopped",
                        "outcome": "scope_changed",
                        "completed_at": self._now_iso(),
                    }
                )
                record.update(
                    {
                        "status": "scope_changed",
                        "breaker_open": False,
                        "cooldown_until": None,
                        "operation": operation,
                        "updated_at": self._now_iso(),
                    }
                )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", stop)
        return selected

    def _mark_indeterminate(self, operation_id: str) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def stop(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            operation = (
                record.get("operation")
                if isinstance(record.get("operation"), dict)
                else {}
            )
            if (
                operation.get("operation_id") == operation_id
                and operation.get("state") == "claimed"
            ):
                operation.update(
                    {
                        "state": "indeterminate",
                        "outcome": "indeterminate",
                        "completed_at": self._now_iso(),
                    }
                )
                record.update(
                    {
                        "status": "indeterminate",
                        "breaker_open": True,
                        "cooldown_until": None,
                        "operation": operation,
                        "last_outcome": "indeterminate",
                        "updated_at": self._now_iso(),
                    }
                )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", stop)
        return selected

    def _record_healthy(
        self,
        *,
        observations: list[dict[str, Any]],
        round_id: str,
        target_binding_digest: str,
        capability_snapshot: Any,
        outcome: str,
        status: str,
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def healthy(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if record.get("target_binding_digest") != target_binding_digest:
                selected = dict(record)
                return
            record.update(
                {
                    "status": status,
                    "incident_id": None,
                    "attempt_count": 0,
                    "failure_confirmation_count": 0,
                    "failure_confirmations": [],
                    "cooldown_until": None,
                    "breaker_open": False,
                    "operation": None,
                    "review_id": None,
                    "last_observation": {
                        "qualifies": False,
                        "sources": observations,
                        "round_id": round_id,
                        "observed_at": self._now_iso(),
                    },
                    "last_verification": {
                        "passed": True,
                        "sources": observations,
                        "round_id": round_id,
                        "observed_at": self._now_iso(),
                    },
                    "last_outcome": outcome,
                    "updated_at": self._now_iso(),
                }
            )
            if isinstance(capability_snapshot, dict):
                record["capability_freshness"] = "fresh"
                identity_digest = self._runtime_identity_digest(
                    capability_snapshot
                )
                if identity_digest:
                    record["runtime_identity_digest"] = identity_digest
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", healthy)
        return selected

    def _record_identity_mismatch(
        self,
        *,
        observations: list[dict[str, Any]],
        round_id: str,
        target_binding_digest: str,
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def stop(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if record.get("target_binding_digest") != target_binding_digest:
                selected = dict(record)
                return
            record.update(
                {
                    "status": "identity_mismatch",
                    "incident_id": str(
                        record.get("incident_id")
                        or f"heal_{uuid4().hex[:16]}"
                    ),
                    "failure_confirmation_count": 0,
                    "failure_confirmations": [],
                    "cooldown_until": None,
                    "breaker_open": True,
                    "last_observation": {
                        "qualifies": False,
                        "sources": observations,
                        "round_id": round_id,
                        "observed_at": self._now_iso(),
                    },
                    "last_verification": {
                        "passed": False,
                        "sources": observations,
                        "round_id": round_id,
                        "observed_at": self._now_iso(),
                        "runtime_identity_status": "mismatch",
                    },
                    "last_outcome": "identity_mismatch",
                    "updated_at": self._now_iso(),
                }
            )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", stop)
        return selected

    def _record_observation(
        self,
        *,
        observations: list[dict[str, Any]],
        round_id: str | None = None,
        status: str,
        qualifies: bool,
        increment_failure: bool,
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def observe(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            failure_count = int(record.get("failure_confirmation_count") or 0)
            confirmation_chain = (
                list(record.get("failure_confirmations"))
                if isinstance(record.get("failure_confirmations"), list)
                else []
            )
            previous = (
                record.get("last_observation")
                if isinstance(record.get("last_observation"), dict)
                else {}
            )
            current_observed_at = self._now_iso()
            current_round_id = (
                str(round_id)
                if round_id
                else self._observation_round_id(observations)
            )
            previous_observed_at = self._parse_iso(
                str(previous.get("observed_at") or "")
            )
            confirmation_separated = bool(
                previous_observed_at is not None
                and self._now()
                >= previous_observed_at
                + timedelta(
                    seconds=self.spec.confirmation_interval_seconds
                )
            )
            confirmation_recent = bool(
                previous_observed_at is not None
                and self._now()
                <= previous_observed_at
                + timedelta(
                    seconds=self.spec.confirmation_window_seconds
                )
            )
            if increment_failure and current_round_id:
                if failure_count == 0 or not confirmation_recent:
                    confirmation_chain = [
                        {
                            "qualifies": True,
                            "sources": observations,
                            "round_id": current_round_id,
                            "observed_at": current_observed_at,
                        }
                    ]
                    failure_count = len(confirmation_chain)
                elif (
                    previous.get("qualifies") is True
                    and previous.get("round_id") != current_round_id
                    and confirmation_separated
                ):
                    confirmation_chain = [
                        *confirmation_chain,
                        {
                            "qualifies": True,
                            "sources": observations,
                            "round_id": current_round_id,
                            "observed_at": current_observed_at,
                        },
                    ][-2:]
                    failure_count = len(confirmation_chain)
            elif not qualifies:
                failure_count = 0
                confirmation_chain = []
            record.update(
                {
                    "status": status,
                    "failure_confirmation_count": failure_count,
                    "failure_confirmations": confirmation_chain,
                    "last_observation": {
                        "qualifies": qualifies,
                        "sources": observations,
                        "round_id": current_round_id or None,
                        "observed_at": current_observed_at,
                    },
                    "updated_at": self._now_iso(),
                }
            )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", observe)
        return selected

    def _bind_target(
        self,
        binding_digest: str,
        *,
        identity_scope_digest: str,
        mode: str,
        mode_epoch: int,
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def bind(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            if (
                record.get("target_binding_digest") != binding_digest
                or record.get("mode") != mode
                or int(record.get("mode_epoch") or 0) != mode_epoch
            ):
                previous = dict(record)
                previous_operation = (
                    dict(previous.get("operation"))
                    if isinstance(previous.get("operation"), dict)
                    else None
                )
                preserved_identity = (
                    str(record.get("runtime_identity_digest") or "")
                    if record.get("identity_scope_digest")
                    == identity_scope_digest
                    else ""
                )
                record.clear()
                record.update(
                    self._new_record(
                        target_binding_digest=binding_digest,
                        status="idle",
                        mode=mode,
                        mode_epoch=mode_epoch,
                    )
                )
                record["identity_scope_digest"] = identity_scope_digest
                if preserved_identity:
                    record["runtime_identity_digest"] = preserved_identity
                if (
                    previous_operation is not None
                    and previous_operation.get("state")
                    in {"claimed", "indeterminate"}
                ):
                    if previous_operation.get("state") == "claimed":
                        previous_operation.update(
                            {
                                "state": "indeterminate",
                                "outcome": "indeterminate",
                                "completed_at": self._now_iso(),
                            }
                        )
                    record.update(
                        {
                            "status": "indeterminate",
                            "incident_id": str(
                                previous.get("incident_id")
                                or f"heal_{uuid4().hex[:16]}"
                            ),
                            "attempt_count": int(
                                previous.get("attempt_count") or 0
                            ),
                            "breaker_open": True,
                            "operation": previous_operation,
                            "review_id": previous.get("review_id"),
                            "last_outcome": "indeterminate",
                            "updated_at": self._now_iso(),
                        }
                    )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", bind)
        return selected

    def _set_non_active_status(
        self,
        status: str,
        *,
        mode: str,
        mode_epoch: int,
    ) -> dict[str, Any]:
        selected: dict[str, Any] = {}

        def set_status(state: dict[str, Any]) -> None:
            nonlocal selected
            if not self._valid_state(state):
                return
            record = self._record_from_state(state)
            operation = (
                dict(record.get("operation"))
                if isinstance(record.get("operation"), dict)
                and record.get("operation", {}).get("state")
                in {"claimed", "indeterminate"}
                else None
            )
            if operation is not None and operation.get("state") == "claimed":
                operation.update(
                    {
                        "state": "indeterminate",
                        "outcome": "indeterminate",
                        "completed_at": self._now_iso(),
                    }
                )
            previous = dict(record)
            record.clear()
            record.update(
                self._new_record(
                    status=(
                        "scope_inactive_indeterminate"
                        if operation is not None
                        else status
                    ),
                    mode=mode,
                    mode_epoch=mode_epoch,
                )
            )
            record["operation"] = operation
            if operation is not None:
                record.update(
                    {
                        "incident_id": str(
                            previous.get("incident_id")
                            or f"heal_{uuid4().hex[:16]}"
                        ),
                        "attempt_count": int(
                            previous.get("attempt_count") or 0
                        ),
                        "breaker_open": True,
                        "review_id": previous.get("review_id"),
                        "last_outcome": "indeterminate",
                    }
                )
            selected = dict(record)

        self.state_store.mutate_json("self_heal_state.json", set_status)
        return selected

    def _ensure_review(self, result: dict[str, Any]) -> dict[str, Any]:
        if not result.get("needs_review") or self.review_creator is None:
            return result
        try:
            state = self.state_store.read_json("self_heal_state.json")
            if not self._valid_state(state):
                raise ValueError("invalid self-heal state")
            review_context = {
                **result,
                "review_dedupe_key": self._review_dedupe_key(
                    self._record_from_state(state)
                ),
            }
            review = self.review_creator(review_context)
        except Exception:
            return {**result, "review_status": "creation_failed"}
        review_id = (
            str(review.get("review_id") or "")
            if isinstance(review, dict)
            else ""
        )
        if review_id:
            self.note_review(review_id)
            review_status = str(review.get("status") or "unknown")
            return {
                **self.status(),
                "review_id": review_id,
                "review_status": review_status,
            }
        return {**result, "review_status": "creation_failed"}

    def _project_executor_healthy(
        self,
        capability_snapshot: Any,
        *,
        observed_at: str,
    ) -> None:
        if not isinstance(capability_snapshot, dict):
            return

        def update(executor: dict[str, Any]) -> None:
            executor.update(
                {
                    "selected_agent": "openclaw",
                    "status": "available",
                    "connected": True,
                    "capability_snapshot": dict(capability_snapshot),
                    "self_heal_observation": {
                        "playbook_id": PLAYBOOK_ID,
                        "status": "healthy",
                        "observed_at": observed_at,
                    },
                    "updated_at": observed_at,
                }
            )

        self.state_store.mutate_json("executor_state.json", update)

    def _project_executor_unavailable(self, *, observed_at: str) -> None:
        def update(executor: dict[str, Any]) -> None:
            executor.update(
                {
                    "selected_agent": "openclaw",
                    "status": "unavailable",
                    "connected": False,
                    "self_heal_observation": {
                        "playbook_id": PLAYBOOK_ID,
                        "status": "recovery_pending",
                        "observed_at": observed_at,
                    },
                    "updated_at": observed_at,
                }
            )

        self.state_store.mutate_json("executor_state.json", update)

    def _target_context(self, adapter: Any) -> dict[str, Any]:
        config = self.state_store.read_json("agent_config.json")
        if config.get("_state_corrupt"):
            return {"applicable": False, "status": "fault"}
        selected = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected_config = (
            agents.get(selected)
            if isinstance(agents.get(selected), dict)
            else {}
        )
        kind = str(selected_config.get("kind") or selected)
        if selected != "openclaw" or kind != "openclaw":
            return {"applicable": False, "status": "not_applicable"}
        if selected_config.get("enabled") is False:
            return {"applicable": False, "status": "disabled_target"}
        if adapter is None:
            return {"applicable": False, "status": "adapter_unavailable"}
        configured_url = str(selected_config.get("base_url") or "").strip()
        adapter_url = str(getattr(adapter, "gateway_url", "") or "").strip()
        configured_endpoint = (
            self._canonical_gateway_endpoint(configured_url)
            if configured_url
            else None
        )
        adapter_endpoint = (
            self._canonical_gateway_endpoint(adapter_url)
            if adapter_url
            else None
        )
        if configured_url and configured_endpoint is None:
            return {"applicable": False, "status": "invalid_target"}
        if adapter_url and adapter_endpoint is None:
            return {"applicable": False, "status": "invalid_target"}
        if configured_endpoint is not None and adapter_endpoint is None:
            return {"applicable": False, "status": "scope_mismatch"}
        if (
            configured_endpoint is not None
            and adapter_endpoint is not None
            and configured_endpoint != adapter_endpoint
        ):
            return {"applicable": False, "status": "scope_mismatch"}
        endpoint = adapter_endpoint or configured_endpoint
        if endpoint is None:
            return {"applicable": False, "status": "not_configured"}
        host = str(endpoint["host"])
        if host not in _LOCAL_HOSTS:
            return {"applicable": False, "status": "unsupported_target"}
        port = endpoint["port"]
        identity_epoch = selected_config.get(
            "self_heal_identity_epoch",
            0,
        )
        if type(identity_epoch) is not int or identity_epoch < 0:
            return {"applicable": False, "status": "invalid_target"}
        binding_digest = self._digest(
            {
                "selected_agent": selected,
                "kind": kind,
                "selected_config_digest": self._digest(
                    {"selected_config": selected_config}
                ),
                "host": host,
                "port": port,
                "path": endpoint["path"],
                "scheme": endpoint["scheme"],
                "environment": "local",
                "adapter_type": type(adapter).__name__,
                "profile_policy_version": OPENCLAW_RECONNECT_PROFILE.policy_version,
            }
        )
        identity_scope_digest = self._digest(
            {
                "selected_agent": selected,
                "kind": kind,
                "host": host,
                "port": port,
                "path": endpoint["path"],
                "scheme": endpoint["scheme"],
                "environment": "local",
                "self_heal_identity_epoch": identity_epoch,
                "adapter_type": type(adapter).__name__,
                "profile_policy_version": (
                    OPENCLAW_RECONNECT_PROFILE.policy_version
                ),
            }
        )
        return {
            "applicable": True,
            "status": "configured",
            "port": port,
            "binding_digest": binding_digest,
            "identity_scope_digest": identity_scope_digest,
        }

    @staticmethod
    def _canonical_gateway_endpoint(value: str) -> dict[str, Any] | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        parsed = urlsplit(raw if "://" in raw else f"ws://{raw}")
        scheme = {"http": "ws", "https": "wss"}.get(
            parsed.scheme.lower(),
            parsed.scheme.lower(),
        )
        if (
            scheme not in {"ws", "wss"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = str(parsed.hostname or "").lower()
        if not host:
            return None
        try:
            port = int(
                parsed.port or (443 if scheme == "wss" else 18789)
            )
        except ValueError:
            return None
        if not 0 < port <= 65535:
            return None
        return {
            "scheme": scheme,
            "host": host,
            "port": port,
            "path": parsed.path.rstrip("/") or "/",
        }

    def _activity_safe(
        self,
        *,
        gateway_observation: dict[str, Any],
    ) -> bool:
        """Fail closed if a governed OpenClaw side effect may still be active."""

        hook = self.state_store.read_json("openclaw_tool_hook_state.json")
        governance = self.state_store.read_json("tool_governance_state.json")
        task_state = self.state_store.read_json("task_state.json")
        if any(
            not isinstance(item, dict) or item.get("_state_corrupt")
            for item in (hook, governance, task_state)
        ):
            return False
        if (
            hook.get("schema_version")
            != "veyra.openclaw_tool_hook_state.v1"
            or governance.get("schema_version")
            != "veyra.tool_governance_state.v1"
        ):
            return False

        dispatches = hook.get("dispatches")
        attempts = hook.get("attempts")
        sessions = governance.get("sessions")
        pending = task_state.get("pending_agent_tasks")
        if (
            not isinstance(dispatches, dict)
            or not isinstance(attempts, dict)
            or not isinstance(sessions, dict)
            or not isinstance(pending, list)
        ):
            return False
        if any(not isinstance(item, dict) for item in dispatches.values()):
            return False
        if any(not isinstance(item, dict) for item in attempts.values()):
            return False
        if any(not isinstance(item, dict) for item in sessions.values()):
            return False
        if any(not isinstance(item, dict) for item in pending):
            return False

        # Unknown or newly introduced ledger states are never assumed terminal.
        # This is intentionally an allowlist: an unavailable Gateway cannot
        # resolve ambiguity for us.
        if any(
            str(item.get("status") or "") != "cancelled"
            for item in dispatches.values()
        ):
            return False
        if any(
            str(item.get("status") or "")
            not in {
                "observed_success",
                "observed_failure",
                "cancelled_before_start",
                "indeterminate",
            }
            for item in attempts.values()
        ):
            return False
        if any(
            str(item.get("status") or "") != "cancelled"
            for item in sessions.values()
        ):
            return False
        # AgentTaskTracker removes terminal entries. Any remaining item in the
        # explicitly named pending ledger therefore blocks transport recovery,
        # including an entry with fields from a newer or malformed schema.
        if pending:
            return False
        active_task_count = gateway_observation.get("active_task_count")
        if gateway_observation.get("active_task_count_malformed") is True:
            return False
        if type(active_task_count) is int and active_task_count > 0:
            return False
        # A fully unavailable Gateway cannot report its task count. In that
        # case the durable broker, governance, and task ledgers above are the
        # fail-closed local authority. A reported-but-malformed count is never
        # accepted.
        return True

    def _resolve_adapter(self) -> Any:
        if self.adapter_resolver is None:
            return None
        try:
            return self.adapter_resolver()
        except Exception:
            return None

    def _bounded_call(
        self,
        call: Callable[[], Any],
    ) -> tuple[bool, Any, str]:
        """Bound adapter calls without trusting third-party timeout behavior."""

        # Marking the worker and admitting a governed Agent share the same
        # fence. This closes the check/start race even for the initial status
        # observation, which can refresh a real transport.
        with agent_transport_authority_fence(self.state_store):
            completed = threading.Event()
            holder: dict[str, Any] = {}
            if not mark_agent_transport_call_inflight(self.state_store):
                return False, None, "call_inflight"

            def invoke() -> None:
                try:
                    holder["value"] = call()
                except Exception as exc:
                    holder["error"] = type(exc).__name__
                finally:
                    clear_agent_transport_call_inflight(self.state_store)
                    completed.set()

            worker = threading.Thread(
                target=invoke,
                name="veyra-self-heal-adapter-call",
                daemon=True,
            )
            try:
                worker.start()
            except Exception as exc:
                clear_agent_transport_call_inflight(self.state_store)
                return False, None, type(exc).__name__
            if not completed.wait(self.call_timeout_seconds):
                return False, None, "timeout"
            error_code = str(holder.get("error") or "")
            if error_code:
                return False, None, error_code
            return True, holder.get("value"), ""

    def _scope_matches(
        self,
        *,
        target_binding_digest: str,
        mode: str,
        mode_epoch: int,
    ) -> bool:
        try:
            current_target = self._target_context(self._resolve_adapter())
            current_mode = self._mode_context()
        except Exception:
            return False
        return bool(
            current_target.get("applicable")
            and current_target.get("binding_digest") == target_binding_digest
            and current_mode.get("mode") == mode
            and current_mode.get("mode_epoch") == mode_epoch
        )

    def _mode_context(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        if not isinstance(config, dict) or config.get("_state_corrupt"):
            raise ValueError("invalid self-heal ops_config")
        self_heal = (
            config.get("self_heal")
            if isinstance(config.get("self_heal"), dict)
            else {}
        )
        playbook = (
            self_heal.get("openclaw_reconnect")
            if isinstance(self_heal.get("openclaw_reconnect"), dict)
            else {}
        )
        mode_epoch = playbook.get("mode_epoch", 0)
        if type(mode_epoch) is not int or mode_epoch < 0:
            raise ValueError("invalid self-heal mode_epoch")
        return {
            "mode": str(playbook.get("mode") or "shadow"),
            "mode_epoch": mode_epoch,
        }

    def _valid_state(self, state: dict[str, Any]) -> bool:
        if not isinstance(state, dict) or state.get("_state_corrupt"):
            return False
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            return False
        playbooks = state.get("playbooks")
        if not isinstance(playbooks, dict):
            return False
        record = playbooks.get(PLAYBOOK_ID)
        if record is None:
            return True
        if not isinstance(record, dict):
            return False
        attempts = record.get("attempt_count", 0)
        confirmations = record.get("failure_confirmation_count", 0)
        confirmation_chain = record.get("failure_confirmations", [])
        mode_epoch = record.get("mode_epoch", 0)
        target_binding_digest = record.get("target_binding_digest")
        identity_scope_digest = record.get("identity_scope_digest")
        runtime_identity_digest = record.get("runtime_identity_digest")
        return bool(
            type(attempts) is int
            and 0 <= attempts <= self.spec.max_attempts
            and type(confirmations) is int
            and 0 <= confirmations <= 2
            and type(mode_epoch) is int
            and mode_epoch >= 0
            and isinstance(record.get("breaker_open", False), bool)
            and self._valid_optional_digest(target_binding_digest)
            and self._valid_optional_digest(
                runtime_identity_digest
            )
            and self._valid_optional_digest(
                identity_scope_digest
            )
            and (
                target_binding_digest is None
                or identity_scope_digest is not None
            )
            and (
                runtime_identity_digest is None
                or identity_scope_digest is not None
            )
            and self._valid_record_operation(
                record,
                attempt_count=attempts,
                confirmation_count=confirmations,
            )
            and self._valid_confirmation_evidence(
                record,
                confirmation_count=confirmations,
                confirmation_chain=confirmation_chain,
            )
        )

    def _valid_record_operation(
        self,
        record: dict[str, Any],
        *,
        attempt_count: Any,
        confirmation_count: Any,
    ) -> bool:
        operation = record.get("operation")
        if not self._valid_operation(operation):
            return False
        if not isinstance(operation, dict) or operation.get("state") != "claimed":
            return True
        return bool(
            record.get("status") == "attempt_in_progress"
            and confirmation_count == 2
            and attempt_count == operation.get("attempt_number")
        )

    def _valid_operation(self, operation: Any) -> bool:
        if operation is None:
            return True
        if not isinstance(operation, dict):
            return False
        operation_id = str(operation.get("operation_id") or "")
        state = str(operation.get("state") or "")
        attempt_number = operation.get("attempt_number")
        claimed_at = self._parse_iso(str(operation.get("claimed_at") or ""))
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", operation_id)
            or state
            not in {
                "claimed",
                "completed",
                "stopped",
                "indeterminate",
                "revoked",
            }
            or type(attempt_number) is not int
            or not 1 <= attempt_number <= self.spec.max_attempts
            or claimed_at is None
        ):
            return False
        if state == "claimed":
            return (
                operation.get("completed_at") is None
                or operation.get("completed_at") == ""
            ) and (
                operation.get("outcome") is None
                or operation.get("outcome") == ""
            )
        completed_at = self._parse_iso(
            str(operation.get("completed_at") or "")
        )
        if completed_at is None or completed_at < claimed_at:
            return False
        outcomes = {
            "completed": {"recovered", "failed"},
            "stopped": {"scope_changed", "identity_mismatch"},
            "indeterminate": {"indeterminate"},
            "revoked": {"scope_inactive"},
        }
        return str(operation.get("outcome") or "") in outcomes[state]

    def _valid_confirmation_evidence(
        self,
        record: dict[str, Any],
        *,
        confirmation_count: Any,
        confirmation_chain: Any,
    ) -> bool:
        if type(confirmation_count) is not int:
            return False
        if confirmation_count == 0:
            return (
                confirmation_chain is None
                or confirmation_chain == ()
                or confirmation_chain == []
            )
        if (
            not isinstance(confirmation_chain, list)
            or len(confirmation_chain) != confirmation_count
        ):
            return False
        previous_round = ""
        previous_time: datetime | None = None
        for observation in confirmation_chain:
            if not self._valid_failure_observation(observation):
                return False
            round_id = str(observation.get("round_id") or "")
            observed_at = self._parse_iso(
                str(observation.get("observed_at") or "")
            )
            if (
                round_id == previous_round
                or (
                    previous_time is not None
                    and (
                        observed_at is None
                        or observed_at
                        < previous_time
                        + timedelta(
                            seconds=self.spec.confirmation_interval_seconds
                        )
                        or observed_at
                        > previous_time
                        + timedelta(
                            seconds=self.spec.confirmation_window_seconds
                        )
                    )
                )
            ):
                return False
            previous_round = round_id
            previous_time = observed_at
        return self._valid_failure_observation(
            record.get("last_observation")
        )

    def _valid_failure_observation(self, observation: Any) -> bool:
        if not isinstance(observation, dict):
            return False
        round_id = str(observation.get("round_id") or "")
        sources = observation.get("sources")
        if (
            observation.get("qualifies") is not True
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", round_id)
            or self._parse_iso(str(observation.get("observed_at") or ""))
            is None
            or not isinstance(sources, list)
            or len(sources) != 2
        ):
            return False
        observed_sources: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                return False
            source_name = str(source.get("source") or "")
            expected_evidence = {
                "openclaw_tcp_probe": "real_probe",
                "openclaw_gateway_protocol": "real_force_refresh",
            }.get(source_name)
            if (
                expected_evidence is None
                or source_name in observed_sources
                or source.get("passed") is not False
                or source.get("round_id") != round_id
                or source.get("evidence_kind") != expected_evidence
                or self._parse_iso(str(source.get("observed_at") or ""))
                is None
            ):
                return False
            observed_sources.add(source_name)
        return observed_sources == {
            "openclaw_tcp_probe",
            "openclaw_gateway_protocol",
        }

    @staticmethod
    def _record_from_state(state: dict[str, Any]) -> dict[str, Any]:
        playbooks = state.setdefault("playbooks", {})
        record = playbooks.setdefault(PLAYBOOK_ID, {})
        return record

    def _new_record(
        self,
        *,
        target_binding_digest: str | None = None,
        status: str,
        mode: str | None = None,
        mode_epoch: int = 0,
    ) -> dict[str, Any]:
        return {
            "playbook_id": PLAYBOOK_ID,
            "spec_version": self.spec.version,
            "profile_id": OPENCLAW_RECONNECT_PROFILE.profile_id,
            "target_binding_digest": target_binding_digest,
            "identity_scope_digest": None,
            "mode": mode or self._mode_context()["mode"],
            "mode_epoch": mode_epoch,
            "status": status,
            "incident_id": None,
            "attempt_count": 0,
            "failure_confirmation_count": 0,
            "failure_confirmations": [],
            "cooldown_until": None,
            "breaker_open": False,
            "operation": None,
            "review_id": None,
            "last_observation": None,
            "last_verification": None,
            "last_outcome": None,
            "runtime_identity_digest": None,
            "updated_at": self._now_iso(),
        }

    def _public_result(
        self,
        record: dict[str, Any],
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        effective_mode = mode or self._mode_context()["mode"]
        operation = (
            record.get("operation")
            if isinstance(record.get("operation"), dict)
            else {}
        )
        status = str(record.get("status") or "idle")
        inactive_statuses = {
            "adapter_unavailable",
            "disabled",
            "disabled_target",
            "fault",
            "invalid_target",
            "not_applicable",
            "not_configured",
            "scope_mismatch",
            "scope_inactive_indeterminate",
            "unsupported_target",
        }
        if effective_mode == "disabled" or status in inactive_statuses:
            effective_level = AutonomyLevel.A0.value
            automatic_effects: list[str] = []
        elif effective_mode in {"record_only", "shadow"} or record.get(
            "breaker_open"
        ):
            effective_level = AutonomyLevel.A1.value
            automatic_effects = (
                ["record bounded playbook observation"]
                if effective_mode == "record_only"
                else [
                    "read local OpenClaw status",
                    "refresh OpenClaw capability snapshot",
                ]
            )
        else:
            effective_level = OPENCLAW_RECONNECT_PROFILE.level.value
            automatic_effects = [
                "read local OpenClaw status",
                "refresh OpenClaw capability snapshot",
                "reconnect Gateway transport",
            ]
        return {
            "playbook_id": PLAYBOOK_ID,
            "status": status,
            "mode": effective_mode,
            "autonomy_profile": OPENCLAW_RECONNECT_PROFILE.to_public_dict(),
            "spec": self.spec.to_public_dict(),
            "attempt_count": int(record.get("attempt_count") or 0),
            "failure_confirmation_count": int(
                record.get("failure_confirmation_count") or 0
            ),
            "cooldown_until": record.get("cooldown_until"),
            "breaker_open": bool(record.get("breaker_open")),
            "effective_autonomy_level": effective_level,
            "operation_state": operation.get("state"),
            "last_observation": self._public_evidence_summary(
                record.get("last_observation")
            ),
            "last_verification": self._public_evidence_summary(
                record.get("last_verification")
            ),
            "last_outcome": record.get("last_outcome"),
            "runtime_identity_status": (
                "mismatch"
                if status == "identity_mismatch"
                else "established"
                if record.get("runtime_identity_digest")
                else "unobserved"
            ),
            "review_id": record.get("review_id"),
            "needs_review": bool(
                record.get("breaker_open") and not record.get("review_id")
            ),
            "automatic_effects": automatic_effects,
            "forbidden_effects": [
                "Agent invocation",
                "tool execution",
                "process restart",
                "provider switch",
                "workspace mutation",
            ],
        }

    def _review_dedupe_key(self, record: dict[str, Any]) -> str:
        if not record.get("breaker_open") or not record.get("incident_id"):
            return ""
        return (
            f"{PLAYBOOK_ID}:review:"
            + self._digest(
                {
                    "target_binding_digest": str(
                        record.get("target_binding_digest") or ""
                    ),
                    "incident_id": str(record.get("incident_id") or ""),
                }
            )[:32]
        )

    def _public_evidence_summary(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        summary: dict[str, Any] = {}
        if isinstance(value.get("qualifies"), bool):
            summary["qualifies"] = value["qualifies"]
        if isinstance(value.get("passed"), bool):
            summary["passed"] = value["passed"]
        observed_at = str(value.get("observed_at") or "")
        if self._parse_iso(observed_at) is not None:
            summary["observed_at"] = observed_at
        identity_status = str(value.get("runtime_identity_status") or "")
        if identity_status in {"established", "matched", "mismatch", "unverified"}:
            summary["runtime_identity_status"] = identity_status
        sources = (
            value.get("sources")
            if isinstance(value.get("sources"), list)
            else []
        )
        compact_sources: list[dict[str, Any]] = []
        allowed_sources = {
            "openclaw_tcp_probe",
            "openclaw_gateway_protocol",
            "openclaw_capability_snapshot",
        }
        allowed_statuses = {
            "available",
            "listening",
            "ok",
            "success",
            "unavailable",
            "timeout",
        }
        for source in sources[:3]:
            if not isinstance(source, dict):
                continue
            source_name = str(source.get("source") or "")
            if source_name not in allowed_sources:
                continue
            item: dict[str, Any] = {"source": source_name}
            status = str(source.get("status") or "")
            item["status"] = (
                status if status in allowed_statuses else "unavailable"
            )
            if isinstance(source.get("passed"), bool):
                item["passed"] = source["passed"]
            source_observed_at = str(source.get("observed_at") or "")
            if self._parse_iso(source_observed_at) is not None:
                item["observed_at"] = source_observed_at
            active_task_count = source.get("active_task_count")
            if (
                type(active_task_count) is int
                and active_task_count >= 0
            ):
                item["active_task_count"] = active_task_count
            elif source_name in {
                "openclaw_gateway_protocol",
                "openclaw_capability_snapshot",
            }:
                item["active_task_count"] = None
            compact_sources.append(item)
        if compact_sources:
            summary["sources"] = compact_sources
        return summary or None

    def _fault_result(self, code: str) -> dict[str, Any]:
        return {
            "playbook_id": PLAYBOOK_ID,
            "status": "fault",
            "mode": "fail_closed",
            "fault_code": str(code)[:80],
            "autonomy_profile": OPENCLAW_RECONNECT_PROFILE.to_public_dict(),
            "spec": self.spec.to_public_dict(),
            "attempt_count": 0,
            "failure_confirmation_count": 0,
            "cooldown_until": None,
            "breaker_open": True,
            "effective_autonomy_level": AutonomyLevel.A0.value,
            "needs_review": False,
            "automatic_effects": [],
            "forbidden_effects": [
                "Agent invocation",
                "tool execution",
                "process restart",
                "provider switch",
                "workspace mutation",
            ],
        }

    def _cooldown_elapsed(self, record: dict[str, Any]) -> bool:
        value = str(record.get("cooldown_until") or "")
        if not value:
            return True
        parsed = self._parse_iso(value)
        return parsed is not None and self._now() >= parsed

    def _observation_due(self, record: dict[str, Any]) -> bool:
        observation = (
            record.get("last_observation")
            if isinstance(record.get("last_observation"), dict)
            else {}
        )
        observed_at = self._parse_iso(
            str(observation.get("observed_at") or "")
        )
        if observed_at is None:
            return True
        return self._now() >= observed_at + timedelta(
            seconds=self.spec.cooldown_seconds
        )

    @staticmethod
    def _observation_round_id(
        observations: list[dict[str, Any]],
    ) -> str:
        values = {
            str(item.get("round_id") or "")
            for item in observations
            if isinstance(item, dict)
        }
        values.discard("")
        return next(iter(values)) if len(values) == 1 else ""

    def _operation_expired(self, operation: dict[str, Any]) -> bool:
        claimed_at = self._parse_iso(str(operation.get("claimed_at") or ""))
        if claimed_at is None:
            return True
        return self._now() >= claimed_at + timedelta(
            seconds=self.spec.operation_timeout_seconds
        )

    @staticmethod
    def _safe_status(payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "unavailable").lower()
        allowed = {
            "available",
            "ok",
            "success",
            "unavailable",
            "timeout",
            "not_configured",
            "adapter_unconfigured",
            "auth_required",
            "pairing_required",
            "device_identity_required",
            "incompatible_gateway",
        }
        return status if status in allowed else "unavailable"

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _runtime_identity_digest(self, snapshot: Any) -> str | None:
        if not isinstance(snapshot, dict):
            return None
        server = (
            snapshot.get("server")
            if isinstance(snapshot.get("server"), dict)
            else {}
        )
        runtime = str(snapshot.get("runtime") or "")
        transport = str(snapshot.get("protocol") or "")
        contract_version = str(snapshot.get("contract_version") or "")
        server_protocol = server.get("protocol")
        server_version = str(server.get("version") or "").strip()
        if (
            runtime != "openclaw"
            or not transport
            or not contract_version
            or type(server_protocol) not in {int, str}
            or isinstance(server_protocol, bool)
            or not str(server_protocol or "")
            or not server_version
        ):
            return None
        return self._digest(
            {
                "runtime": runtime,
                "transport": transport,
                "contract_version": contract_version,
                "server_protocol": server_protocol,
                "server_version": server_version,
            }
        )

    @staticmethod
    def _valid_optional_digest(value: Any) -> bool:
        if value is None:
            return True
        return bool(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
        )

    def _now_iso(self) -> str:
        return self._iso(self._now())

    @staticmethod
    def _iso(value: datetime) -> str:
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _parse_iso(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
