from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter
from interface.agent_dialogue_contract import (
    DialogueContractError,
    DialogueType,
    parse_dialogue_message,
)
from runtime.authority_fence import agent_transport_authority_fence


CAPABILITY_DIRECTORY_SCHEMA = "veyra.phase6.agent_capability_directory.v1"
PHASE6_EXECUTION_PROFILE = "phase6_read_only_collaboration.v1"
PHASE6_RUNTIME_ALLOWLIST = frozenset({"openclaw"})
PHASE6_ROLES = ("primary_analyst", "critic")


class AgentCapabilitySelectionError(RuntimeError):
    """An exact Phase 6 runtime could not be selected without fallback."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "runtime_not_eligible")
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class AgentCapabilitySelection:
    runtime: str
    adapter: AgentAdapter
    adapter_identity: int
    config_digest: str
    certification_digest: str
    provider_binding: dict[str, Any]
    observed_at: str


class AgentCapabilityDirectory:
    """Fresh, operator-bound runtime eligibility for Phase 6 collaboration.

    The directory is deliberately not a router or portfolio. It never changes
    the selected provider, never falls back, and treats remote capability
    claims as diagnostics unless the local adapter is Veyra's trusted native
    OpenClaw adapter with exact current governance enforcement.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        registry: Any,
    ) -> None:
        self.state_store = state_store
        self.registry = registry

    def snapshot(self) -> dict[str, Any]:
        config = self._config()
        selected = self._selected_runtime(config)
        names = sorted(
            set(
                [
                    *(
                        config.get("agents", {}).keys()
                        if isinstance(config.get("agents"), dict)
                        else []
                    ),
                    *self.registry.names(),
                ]
            )
        )
        rows = [
            self._runtime_row(
                runtime=name,
                selected_runtime=selected,
                config=config,
            )
            for name in names
        ]
        eligible = [
            row["runtime"]
            for row in rows
            if row.get("collaboration_dispatch_eligible") is True
        ]
        return {
            "schema_version": CAPABILITY_DIRECTORY_SCHEMA,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "selection_mode": "explicit_operator_selection",
            "selected_runtime": selected or None,
            "automatic_selection_allowed": False,
            "provider_switch_allowed": False,
            "execution_profile": PHASE6_EXECUTION_PROFILE,
            "topology": "single_runtime_multi_participant",
            "participant_limit": 2,
            "handoff_limit": 1,
            "roles": list(PHASE6_ROLES),
            "eligible_runtimes": eligible,
            "runtimes": rows,
        }

    def select_exact(self, runtime: str) -> AgentCapabilitySelection:
        selected_runtime = self._normalized_runtime(runtime)
        config = self._config()
        configured_selection = self._selected_runtime(config)
        if not configured_selection:
            raise AgentCapabilitySelectionError(
                "operator_selected_runtime_missing"
            )
        if selected_runtime != configured_selection:
            raise AgentCapabilitySelectionError(
                "runtime_is_not_operator_selected"
            )
        if selected_runtime not in PHASE6_RUNTIME_ALLOWLIST:
            raise AgentCapabilitySelectionError(
                "runtime_is_diagnostic_only"
            )
        try:
            adapter = self.registry.get(selected_runtime)
        except KeyError as exc:
            raise AgentCapabilitySelectionError(
                "operator_selected_runtime_unavailable"
            ) from exc
        status = self._fresh_status(selected_runtime, adapter)
        reasons = self._eligibility_reasons(
            runtime=selected_runtime,
            adapter=adapter,
            status=status,
            selected_runtime=configured_selection,
            config=config,
        )
        if reasons:
            raise AgentCapabilitySelectionError(reasons[0])
        certification = (
            status.get("provider_certification")
            if isinstance(status.get("provider_certification"), dict)
            else {}
        )
        config_digest = self._config_digest(config)
        certification_digest = self._certification_identity_digest(
            certification
        )
        runtime_config = (
            config.get("agents", {}).get(selected_runtime, {})
            if isinstance(config.get("agents"), dict)
            and isinstance(
                config.get("agents", {}).get(selected_runtime), dict
            )
            else {}
        )
        configured_model = str(
            runtime_config.get("model") or ""
        ).strip()
        return AgentCapabilitySelection(
            runtime=selected_runtime,
            adapter=adapter,
            adapter_identity=id(adapter),
            config_digest=config_digest,
            certification_digest=certification_digest,
            provider_binding={
                "runtime": selected_runtime,
                "provider": "openclaw_native_gateway",
                "model": (
                    configured_model
                    or "runtime_managed_model_not_advertised"
                ),
                "instance_id": (
                    "inst_"
                    + self._digest(
                        {
                            "config_digest": config_digest,
                            "certification_digest": (
                                certification_digest
                            ),
                        }
                    )[:24]
                ),
                "automatic_switch_allowed": False,
            },
            observed_at=str(
                certification.get("observed_at")
                or datetime.now(timezone.utc).isoformat()
            ),
        )

    def dispatch(
        self,
        selection: AgentCapabilitySelection,
        task_packet: Any,
    ) -> Any:
        """Send only while the exact selected config and adapter stay bound."""

        with agent_transport_authority_fence(self.state_store):
            config = self._config()
            if self._selected_runtime(config) != selection.runtime:
                raise AgentCapabilitySelectionError(
                    "operator_selected_runtime_changed"
                )
            if self._config_digest(config) != selection.config_digest:
                raise AgentCapabilitySelectionError(
                    "runtime_configuration_changed"
                )
            try:
                current = self.registry.get(selection.runtime)
            except KeyError as exc:
                raise AgentCapabilitySelectionError(
                    "operator_selected_runtime_unavailable"
                ) from exc
            if (
                current is not selection.adapter
                or id(current) != selection.adapter_identity
            ):
                raise AgentCapabilitySelectionError(
                    "runtime_adapter_identity_changed"
                )
            try:
                dialogue = parse_dialogue_message(
                    task_packet.dialogue_message,
                    expected_sender="veyra",
                    allowed_types={
                        DialogueType.TASK_REQUEST,
                        DialogueType.CONTEXT_PATCH,
                        DialogueType.PLAN_SELECTION,
                    },
                )
            except (
                AttributeError,
                DialogueContractError,
                TypeError,
                ValueError,
            ) as exc:
                raise AgentCapabilitySelectionError(
                    "collaboration_dialogue_binding_invalid"
                ) from exc
            binding = dialogue.collaboration_binding
            if (
                binding is None
                or binding.provider.model_dump(mode="json")
                != selection.provider_binding
            ):
                raise AgentCapabilitySelectionError(
                    "collaboration_provider_binding_changed"
                )
            return selection.adapter.send_task(task_packet)

    def assert_selection_current(
        self,
        selection: AgentCapabilitySelection,
    ) -> None:
        config = self._config()
        if (
            self._selected_runtime(config) != selection.runtime
            or self._config_digest(config) != selection.config_digest
        ):
            raise AgentCapabilitySelectionError(
                "runtime_selection_is_stale"
            )
        try:
            adapter = self.registry.get(selection.runtime)
        except KeyError as exc:
            raise AgentCapabilitySelectionError(
                "operator_selected_runtime_unavailable"
            ) from exc
        if adapter is not selection.adapter:
            raise AgentCapabilitySelectionError(
                "runtime_adapter_identity_changed"
            )

    def _runtime_row(
        self,
        *,
        runtime: str,
        selected_runtime: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            adapter = self.registry.get(runtime)
        except KeyError:
            adapter = None
            status: dict[str, Any] = {
                "status": "adapter_unconfigured",
                "connected": False,
            }
        else:
            status = self._fresh_status(runtime, adapter)
        reasons = self._eligibility_reasons(
            runtime=runtime,
            adapter=adapter,
            status=status,
            selected_runtime=selected_runtime,
            config=config,
        )
        certification = (
            status.get("provider_certification")
            if isinstance(status.get("provider_certification"), dict)
            else {}
        )
        features = self._features(status)
        return {
            "runtime": runtime,
            "operator_selected": runtime == selected_runtime,
            "status": str(status.get("status") or "unknown"),
            "connected": status.get("connected") is True,
            "trusted_native_adapter": bool(
                adapter is not None
                and getattr(
                    adapter,
                    "trusted_native_provider_adapter",
                    False,
                )
                is True
            ),
            "certification": {
                "status": str(
                    certification.get("certification_status")
                    or "unverified"
                ),
                "validated": certification.get("validated") is True,
                "freshness": (
                    dict(certification.get("freshness"))
                    if isinstance(
                        certification.get("freshness"), dict
                    )
                    else {"status": "unknown", "age_seconds": None}
                ),
                "issues": self._bounded_strings(
                    certification.get("issues"), limit=16
                ),
            },
            "execution_profiles": self._execution_profiles(features),
            "roles": list(PHASE6_ROLES) if not reasons else [],
            "collaboration_dispatch_eligible": not reasons,
            "diagnostic_only": bool(reasons),
            "reasons": reasons,
            "automatic_selection_allowed": False,
            "provider_switch_allowed": False,
            "side_effect_dispatch_allowed": False,
            "tool_allowlist": [],
        }

    def _eligibility_reasons(
        self,
        *,
        runtime: str,
        adapter: AgentAdapter | None,
        status: dict[str, Any],
        selected_runtime: str,
        config: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        agents = (
            config.get("agents")
            if isinstance(config.get("agents"), dict)
            else {}
        )
        runtime_config = (
            agents.get(runtime)
            if isinstance(agents.get(runtime), dict)
            else {}
        )
        if not runtime_config or runtime_config.get("enabled") is False:
            reasons.append("runtime_not_enabled")
        if runtime != selected_runtime:
            reasons.append("runtime_is_not_operator_selected")
        if runtime not in PHASE6_RUNTIME_ALLOWLIST:
            reasons.append("runtime_is_diagnostic_only")
        if adapter is None:
            reasons.append("runtime_adapter_unavailable")
        elif (
            getattr(
                adapter,
                "trusted_native_provider_adapter",
                False,
            )
            is not True
        ):
            reasons.append("trusted_native_adapter_required")
        certification = (
            status.get("provider_certification")
            if isinstance(status.get("provider_certification"), dict)
            else {}
        )
        if certification.get("validated") is not True:
            reasons.append("fresh_provider_certification_required")
        features = self._features(status)
        if features.get("tool_proxy_enforced") is not True:
            reasons.append("tool_proxy_enforcement_required")
        if features.get("tool_proxy_identity_match") is not True:
            reasons.append("tool_proxy_identity_match_required")
        if (
            features.get("tool_proxy_enforcement_scope")
            != "veyra_governed_openclaw_sessions"
        ):
            reasons.append("tool_proxy_scope_mismatch")
        if features.get("governance_callbacks_complete") is not True:
            reasons.append("governance_callbacks_incomplete")
        required_phase4 = (
            "agent_dialogue_v1",
            "caller_supplied_run_id",
            "idempotent_submit",
            "exact_stop",
        )
        for feature in required_phase4:
            if features.get(feature) is not True:
                reasons.append(f"required_feature_missing:{feature}")
        if PHASE6_EXECUTION_PROFILE not in self._execution_profiles(
            features
        ):
            reasons.append("phase6_execution_profile_not_advertised")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _fresh_status(
        runtime: str,
        adapter: AgentAdapter,
    ) -> dict[str, Any]:
        if runtime == "openclaw":
            try:
                status = adapter.connection_status(force_refresh=True)  # type: ignore[call-arg]
            except TypeError:
                status = adapter.connection_status()
        else:
            status = adapter.connection_status()
        return status if isinstance(status, dict) else {}

    @staticmethod
    def _features(status: dict[str, Any]) -> dict[str, Any]:
        direct = status.get("features")
        if isinstance(direct, dict):
            return direct
        capabilities = status.get("capabilities")
        if isinstance(capabilities, dict) and isinstance(
            capabilities.get("features"), dict
        ):
            return capabilities["features"]
        return {}

    @staticmethod
    def _execution_profiles(features: dict[str, Any]) -> list[str]:
        values = features.get("enforced_execution_profiles")
        profiles = (
            [str(item) for item in values if isinstance(item, str)]
            if isinstance(values, list)
            else []
        )
        legacy = features.get("enforced_execution_profile")
        if isinstance(legacy, str) and legacy:
            profiles.append(legacy)
        return sorted(set(profiles))

    def _config(self) -> dict[str, Any]:
        value = self.registry.config()
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _selected_runtime(config: dict[str, Any]) -> str:
        value = config.get("selected_agent")
        if not isinstance(value, str):
            return ""
        return value.strip()

    @staticmethod
    def _normalized_runtime(value: Any) -> str:
        if not isinstance(value, str):
            raise AgentCapabilitySelectionError(
                "runtime_must_be_a_string"
            )
        selected = value.strip()
        if (
            not selected
            or selected != value
            or len(selected) > 120
            or any(ord(character) < 32 for character in selected)
        ):
            raise AgentCapabilitySelectionError(
                "runtime_identity_invalid"
            )
        return selected

    @classmethod
    def _config_digest(cls, value: dict[str, Any]) -> str:
        return cls._digest(value)

    @classmethod
    def _certification_identity_digest(
        cls,
        certification: dict[str, Any],
    ) -> str:
        """Bind stable certification facts while freshness is rechecked live."""

        return cls._digest(
            {
                "schema_version": certification.get("schema_version"),
                "runtime": certification.get("runtime"),
                "certification_status": certification.get(
                    "certification_status"
                ),
                "validated": certification.get("validated") is True,
                "ttl_seconds": certification.get("ttl_seconds"),
                "evidence": (
                    certification.get("evidence")
                    if isinstance(
                        certification.get("evidence"), dict
                    )
                    else {}
                ),
                "issues": (
                    certification.get("issues")
                    if isinstance(
                        certification.get("issues"), list
                    )
                    else []
                ),
            }
        )

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _bounded_strings(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(item)[:240]
            for item in value[:limit]
            if isinstance(item, (str, int, float, bool))
        ]
