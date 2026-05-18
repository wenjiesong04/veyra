from __future__ import annotations

from typing import Any


AGENT_COMPATIBILITY_POLICY_VERSION = "veyra.agent_compatibility.v1"

DEFAULT_REQUIRED_FEATURES = ("rendered_prompt_fallback",)
DEFAULT_OPTIONAL_FEATURES = (
    "structured_task_packet",
    "memory_summary",
    "memory_patch",
    "stop_task",
    "tool_proxy_enforced",
)

DISCONNECTED_STATUSES = {
    "adapter_unconfigured",
    "auth_required",
    "device_identity_required",
    "pairing_required",
    "scope_required",
    "timeout",
    "unavailable",
}


def compatibility_policy_summary() -> dict[str, Any]:
    return {
        "policy_version": AGENT_COMPATIBILITY_POLICY_VERSION,
        "strategy": "capability_probe_before_version_pin",
        "required_features": list(DEFAULT_REQUIRED_FEATURES),
        "optional_features": list(DEFAULT_OPTIONAL_FEATURES),
        "maintenance_rule": (
            "Version-only runtime updates should not require Veyra changes when the agent still "
            "supports the same transport, auth, task packet, and required methods. Adapter updates "
            "are needed only for breaking changes in those surfaces."
        ),
    }


def evaluate_agent_compatibility(
    raw: dict[str, Any] | None,
    *,
    runtime: str,
    expected_contract_version: str,
    connected: bool | None = None,
    required_features: tuple[str, ...] = DEFAULT_REQUIRED_FEATURES,
    optional_features: tuple[str, ...] = DEFAULT_OPTIONAL_FEATURES,
) -> dict[str, Any]:
    data = dict(raw or {})
    embedded = data.get("compatibility") if isinstance(data.get("compatibility"), dict) else {}
    adapter_status = str(data.get("status") or "adapter_unconfigured")
    connected_flag = _connected(data, adapter_status) if connected is None else connected
    reported_contract = _string(data.get("contract_version") or data.get("adapter_contract_version"))
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    required_feature_status = _feature_status(features, required_features)
    optional_feature_status = _feature_status(features, optional_features)
    required_methods = _bool_map(embedded.get("required_methods"))
    optional_methods = _bool_map(embedded.get("optional_methods"))
    protocol = _protocol_status(data, embedded)
    native_adapter = bool(embedded.get("native_adapter"))

    issues: list[str] = []
    recommendations: list[str] = []
    incompatible = False
    unverified = False

    if adapter_status in DISCONNECTED_STATUSES or not connected_flag:
        if adapter_status == "adapter_unconfigured":
            recommendations.append("Configure the adapter endpoint before sending real tasks.")
            status = "unconfigured"
        else:
            recommendations.append("Refresh the runtime status and verify auth, endpoint, and process health.")
            status = "unavailable"
        if "protocol" in adapter_status:
            issues.append("protocol_handshake_failed")
            recommendations.append("Refresh the gateway process first; update adapter protocol support only if no protocol range overlaps.")
            status = "incompatible"
        return _result(
            status=status,
            runtime=runtime,
            adapter_status=adapter_status,
            connected=connected_flag,
            expected_contract_version=expected_contract_version,
            reported_contract_version=reported_contract,
            native_adapter=native_adapter,
            required_feature_status=required_feature_status,
            optional_feature_status=optional_feature_status,
            required_methods=required_methods,
            optional_methods=optional_methods,
            protocol=protocol,
            issues=issues,
            recommendations=recommendations,
            data=data,
        )

    if reported_contract and reported_contract != expected_contract_version:
        unverified = True
        issues.append("contract_version_not_verified")
        recommendations.append("Use rendered prompt fallback until the runtime confirms the current Veyra contract.")
    if not reported_contract and not native_adapter:
        unverified = True
        issues.append("contract_version_not_advertised")
        recommendations.append("Ask the runtime to expose its Veyra agent contract version in /capabilities.")

    for feature, value in required_feature_status.items():
        if value is False:
            incompatible = True
            issues.append(f"required_feature_disabled:{feature}")
        if value is None:
            unverified = True
            issues.append(f"required_feature_not_advertised:{feature}")

    for method, value in required_methods.items():
        if value is False:
            incompatible = True
            issues.append(f"required_method_missing:{method}")

    if protocol.get("compatible") is False:
        incompatible = True
        issues.append("protocol_range_mismatch")
        recommendations.append("Update adapter protocol support only if the runtime no longer supports any negotiated version.")

    if incompatible:
        status = "incompatible"
    elif unverified:
        status = "unverified"
    else:
        status = str(embedded.get("status") or "compatible")

    if not recommendations:
        recommendations.append("No adapter update is needed while required capabilities remain compatible.")
    return _result(
        status=status,
        runtime=runtime,
        adapter_status=adapter_status,
        connected=connected_flag,
        expected_contract_version=expected_contract_version,
        reported_contract_version=reported_contract,
        native_adapter=native_adapter,
        required_feature_status=required_feature_status,
        optional_feature_status=optional_feature_status,
        required_methods=required_methods,
        optional_methods=optional_methods,
        protocol=protocol,
        issues=issues,
        recommendations=recommendations,
        data=data,
    )


def _result(
    *,
    status: str,
    runtime: str,
    adapter_status: str,
    connected: bool,
    expected_contract_version: str,
    reported_contract_version: str,
    native_adapter: bool,
    required_feature_status: dict[str, bool | None],
    optional_feature_status: dict[str, bool | None],
    required_methods: dict[str, bool],
    optional_methods: dict[str, bool],
    protocol: dict[str, Any],
    issues: list[str],
    recommendations: list[str],
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "policy_version": AGENT_COMPATIBILITY_POLICY_VERSION,
        "status": status,
        "runtime": runtime,
        "adapter_status": adapter_status,
        "connected": connected,
        "native_adapter": native_adapter,
        "contract": {
            "expected": expected_contract_version,
            "reported": reported_contract_version or None,
            "compatible": True if reported_contract_version == expected_contract_version else None,
        },
        "server_version": _server_version(data),
        "protocol": protocol,
        "required_features": required_feature_status,
        "optional_features": optional_feature_status,
        "required_methods": required_methods,
        "optional_methods": optional_methods,
        "issues": issues,
        "recommendations": recommendations,
    }


def _connected(data: dict[str, Any], adapter_status: str) -> bool:
    if "connected" in data:
        return bool(data.get("connected"))
    return adapter_status in {"available", "ok", "success"}


def _feature_status(features: dict[str, Any], names: tuple[str, ...]) -> dict[str, bool | None]:
    status: dict[str, bool | None] = {}
    for name in names:
        if name in features:
            status[name] = bool(features.get(name))
        else:
            status[name] = None
    return status


def _bool_map(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(item) for key, item in value.items()}


def _protocol_status(data: dict[str, Any], embedded: dict[str, Any]) -> dict[str, Any]:
    requested = embedded.get("requested_protocol") if isinstance(embedded.get("requested_protocol"), dict) else {}
    server_protocol = embedded.get("server_protocol") or data.get("protocol_version")
    if server_protocol is None:
        server = data.get("server") if isinstance(data.get("server"), dict) else {}
        server_protocol = server.get("protocol")
    server_protocol_int = _int_or_none(server_protocol)
    min_protocol = _int_or_none(requested.get("min"))
    max_protocol = _int_or_none(requested.get("max"))
    compatible: bool | None = None
    if server_protocol_int is not None and min_protocol is not None and max_protocol is not None:
        compatible = min_protocol <= server_protocol_int <= max_protocol
    return {
        "transport": embedded.get("transport") or data.get("protocol") or "unknown",
        "requested": requested or None,
        "server_protocol": server_protocol_int if server_protocol_int is not None else server_protocol,
        "compatible": compatible,
    }


def _server_version(data: dict[str, Any]) -> str | None:
    for key in ("server_version", "runtime_version", "version"):
        value = _string(data.get(key))
        if value:
            return value
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    gateway_status = data.get("gateway_status") if isinstance(data.get("gateway_status"), dict) else {}
    return _string(server.get("version") or gateway_status.get("runtime_version")) or None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
