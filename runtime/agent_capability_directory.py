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
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_deployment import PublicExtensionCapability
from interface.extension_generation import initiating_session_digest
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


class ExtensionCapabilitySelectionError(RuntimeError):
    """A promoted extension could not be selected without scope drift."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "extension_capability_not_eligible")
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


@dataclass(frozen=True, slots=True)
class PromotedExtensionCapabilitySelection:
    """Source-free binding to one exact promoted deployment projection."""

    capability_id: str
    deployment_id: str
    deployment_revision: int
    deployment_state_revision: int
    mode_epoch: int
    active_pointer_revision: int
    owner_scope_digest: str
    workspace_identity_digest: str
    initiating_session_digest: str
    release_id: str
    attestation_digest: str
    capability_digest: str
    observed_at: str


class AgentCapabilityDirectory:
    """Fresh, operator-bound runtime eligibility for Phase 6 collaboration.

    The Agent runtime directory is deliberately not a router or portfolio. It
    never changes the selected provider, never falls back, and treats remote
    capability claims as diagnostics unless the local adapter is Veyra's
    trusted native OpenClaw adapter with exact current governance enforcement.
    Promoted extensions stay out of the ambient snapshot and require explicit
    owner/workspace/session discovery plus deployment-gate invocation.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        registry: Any,
        extension_deployment_gate: Any | None = None,
    ) -> None:
        self.state_store = state_store
        self.registry = registry
        self._extension_deployment_gate = extension_deployment_gate

    def bind_extension_deployment_gate(self, gate: Any) -> None:
        """Bind the sole extension authority during startup.

        The directory never receives a runner, release subject, source, or
        ambient control credential. Rebinding to a different gate is rejected
        so an ordinary Agent cannot swap in an alternate invocation path.
        """

        required = ("public_registry", "get", "invoke")
        if gate is None or any(
            not callable(getattr(gate, method, None)) for method in required
        ):
            raise ExtensionCapabilitySelectionError(
                "extension_deployment_gate_invalid"
            )
        current = self._extension_deployment_gate
        if current is not None and current is not gate:
            raise ExtensionCapabilitySelectionError(
                "extension_deployment_gate_rebind_forbidden"
            )
        self._extension_deployment_gate = gate

    def discover_promoted_extensions(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        """Discover only actively signed extensions for one exact session.

        This is an explicit structured control-plane operation. It is never
        called from Agent dispatch or natural-language capability selection.
        Cryptographic freshness and revocation are rechecked by the deployment
        gate's source-free public registry before any row is returned.
        """

        gate = self._extension_gate()
        snapshot = gate.public_registry(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        if not isinstance(snapshot, dict):
            raise ExtensionCapabilitySelectionError(
                "extension_registry_projection_invalid"
            )
        raw_items = snapshot.get("items")
        if not isinstance(raw_items, list):
            raise ExtensionCapabilitySelectionError(
                "extension_registry_projection_invalid"
            )
        expected_owner = artifact_owner_scope_digest(user_id, workspace_id)
        expected_workspace = self._workspace_identity_digest(workspace_id)
        expected_session = initiating_session_digest(session_id)
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            try:
                capability = PublicExtensionCapability.model_validate(
                    raw,
                    strict=True,
                )
            except Exception as exc:
                raise ExtensionCapabilitySelectionError(
                    "extension_registry_projection_invalid"
                ) from exc
            if (
                capability.mode != "promoted"
                or capability.owner_scope_digest != expected_owner
                or capability.workspace_identity_digest
                != expected_workspace
                or capability.initiating_session_digest
                != expected_session
                or capability.execution_boundary
                != "trusted_isolated_runner_only"
                or any(
                    capability.authority.model_dump(mode="python").values()
                )
            ):
                raise ExtensionCapabilitySelectionError(
                    "extension_registry_scope_or_authority_mismatch"
                )
            items.append(capability.model_dump(mode="json"))
        items.sort(key=lambda item: item["capability_id"])
        state_revision = snapshot.get("state_revision")
        if type(state_revision) is not int or state_revision < 0:
            raise ExtensionCapabilitySelectionError(
                "extension_registry_projection_invalid"
            )
        invalid_or_revoked_count = snapshot.get(
            "invalid_or_revoked_count"
        )
        if (
            type(invalid_or_revoked_count) is not int
            or invalid_or_revoked_count < 0
        ):
            raise ExtensionCapabilitySelectionError(
                "extension_registry_projection_invalid"
            )
        return {
            "schema_version": (
                "veyra.phase6.scoped_extension_capability_directory.v1"
            ),
            "state_revision": state_revision,
            "items": items,
            "invalid_or_revoked_count": invalid_or_revoked_count,
            "automatic_selection_allowed": False,
            "natural_language_routing_allowed": False,
            "agent_dispatch_allowed": False,
            "invocation_boundary": "extension_deployment_gate_only",
            "source_free": True,
        }

    def select_promoted_extension(
        self,
        *,
        capability_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> PromotedExtensionCapabilitySelection:
        """Bind an exact capability id; never infer one from natural text."""

        selected_id = self._normalized_capability_id(capability_id)
        first = self.discover_promoted_extensions(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        item = self._exact_extension_item(first["items"], selected_id)
        gate = self._extension_gate()
        deployment = gate.get(
            deployment_id=item["deployment_id"],
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        self._require_deployment_matches_capability(deployment, item)
        second = self.discover_promoted_extensions(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        rebound = self._exact_extension_item(second["items"], selected_id)
        if (
            first["state_revision"] != second["state_revision"]
            or self._digest(item) != self._digest(rebound)
        ):
            raise ExtensionCapabilitySelectionError(
                "extension_capability_changed_during_selection"
            )
        return PromotedExtensionCapabilitySelection(
            capability_id=selected_id,
            deployment_id=item["deployment_id"],
            deployment_revision=deployment["revision"],
            deployment_state_revision=second["state_revision"],
            mode_epoch=item["mode_epoch"],
            active_pointer_revision=item["active_pointer_revision"],
            owner_scope_digest=item["owner_scope_digest"],
            workspace_identity_digest=item["workspace_identity_digest"],
            initiating_session_digest=item["initiating_session_digest"],
            release_id=item["release_id"],
            attestation_digest=item["attestation_digest"],
            capability_digest=self._digest(item),
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    def invoke_promoted_extension(
        self,
        selection: PromotedExtensionCapabilitySelection,
        *,
        operation_id: str,
        request_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        input_payload: dict[str, Any],
        control_token: str,
    ) -> dict[str, Any]:
        """Invoke only by delegating to the authoritative deployment gate."""

        if not isinstance(
            selection, PromotedExtensionCapabilitySelection
        ):
            raise ExtensionCapabilitySelectionError(
                "promoted_extension_selection_required"
            )
        current = self.select_promoted_extension(
            capability_id=selection.capability_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        immutable = (
            "capability_id",
            "deployment_id",
            "mode_epoch",
            "active_pointer_revision",
            "owner_scope_digest",
            "workspace_identity_digest",
            "initiating_session_digest",
            "release_id",
            "attestation_digest",
            "capability_digest",
        )
        if any(
            getattr(selection, field) != getattr(current, field)
            for field in immutable
        ):
            raise ExtensionCapabilitySelectionError(
                "promoted_extension_selection_is_stale"
            )
        return self._extension_gate().invoke(
            operation_id=operation_id,
            request_id=request_id,
            deployment_id=current.deployment_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            expected_state_revision=current.deployment_state_revision,
            expected_deployment_revision=current.deployment_revision,
            expected_mode_epoch=current.mode_epoch,
            input_payload=input_payload,
            control_token=control_token,
        )

    def snapshot(self, *, read_only: bool = False) -> dict[str, Any]:
        """Project runtime eligibility.

        The normal snapshot is an explicit fresh observation used by
        selection. ``read_only`` is reserved for status GETs: it must consume
        process/durable cache only and never invoke a provider handshake.
        """

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
                read_only=read_only,
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
        read_only: bool = False,
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
            status = (
                self._cached_status(runtime, adapter)
                if read_only
                else self._fresh_status(runtime, adapter)
            )
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

    def _cached_status(
        self,
        runtime: str,
        adapter: AgentAdapter,
    ) -> dict[str, Any]:
        """Read status without network calls or state-store writes."""

        cached = getattr(adapter, "connection_status_cached", None)
        if callable(cached):
            try:
                value = cached()
            except Exception:
                value = None
            if isinstance(value, dict):
                return value

        # Adapters without a process-local cache can still expose the last
        # registry projection. Never call their live connection_status from a
        # status GET: a provider is allowed to refresh credentials there.
        executor = self.state_store.read_json("executor_state.json")
        rows = executor.get("agents") if isinstance(executor, dict) else None
        value = rows.get(runtime) if isinstance(rows, dict) else None
        if isinstance(value, dict):
            return {
                **value,
                "status": str(value.get("status") or "cached"),
                "connected": value.get("connected") is True,
                "observation_source": "durable_executor_state",
            }
        return {
            "runtime": runtime,
            "status": "cached_unavailable",
            "connected": False,
            "features": {},
            "provider_certification": {
                "validated": False,
                "certification_status": "unverified",
                "freshness": {"status": "unknown", "age_seconds": None},
                "issues": ["cached_observation_missing"],
            },
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

    def _extension_gate(self) -> Any:
        gate = self._extension_deployment_gate
        if gate is None:
            raise ExtensionCapabilitySelectionError(
                "extension_deployment_gate_unbound"
            )
        return gate

    @staticmethod
    def _exact_extension_item(
        items: list[dict[str, Any]], capability_id: str
    ) -> dict[str, Any]:
        selected = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("capability_id") == capability_id
        ]
        if not selected:
            raise ExtensionCapabilitySelectionError(
                "promoted_extension_not_found"
            )
        if len(selected) != 1:
            raise ExtensionCapabilitySelectionError(
                "promoted_extension_identity_is_ambiguous"
            )
        return dict(selected[0])

    @staticmethod
    def _require_deployment_matches_capability(
        deployment: Any,
        capability: dict[str, Any],
    ) -> None:
        if not isinstance(deployment, dict) or (
            deployment.get("deployment_id")
            != capability.get("deployment_id")
            or deployment.get("release_id")
            != capability.get("release_id")
            or deployment.get("attestation_digest")
            != capability.get("attestation_digest")
            or deployment.get("extension_id")
            != capability.get("extension_id")
            or deployment.get("extension_version")
            != capability.get("extension_version")
            or deployment.get("mode") != "promoted"
            or deployment.get("mode_epoch")
            != capability.get("mode_epoch")
            or deployment.get("breaker_open") is not False
            or type(deployment.get("revision")) is not int
            or deployment["revision"] < 1
        ):
            raise ExtensionCapabilitySelectionError(
                "promoted_extension_deployment_binding_mismatch"
            )

    @staticmethod
    def _normalized_capability_id(value: Any) -> str:
        if not isinstance(value, str):
            raise ExtensionCapabilitySelectionError(
                "extension_capability_id_invalid"
            )
        selected = value.strip()
        if (
            not selected
            or selected != value
            or len(selected) > 120
            or not selected[0].isalpha()
            or selected[0].lower() != selected[0]
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in selected
            )
        ):
            raise ExtensionCapabilitySelectionError(
                "extension_capability_id_invalid"
            )
        return selected

    @staticmethod
    def _workspace_identity_digest(workspace_id: str) -> str:
        return hashlib.sha256(
            json.dumps(
                {"workspace_id": str(workspace_id)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

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
