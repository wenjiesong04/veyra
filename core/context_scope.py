from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from core.model_client import redact_sensitive
from memory_bridge.scope import framed_sha256, normalize_scope_component


# Statuses that mean "this is a diagnostic / fallback payload", not real task-relevant
# agent memory. These must never be dumped into the agent prompt; they go to audit only.
DIAGNOSTIC_MEMORY_STATUSES = {
    "workspace_file_fallback",
    "workspace_file_empty",
    "method_unavailable",
    "unknown_method",
    "unsupported",
    "not_configured",
    "memory_unavailable",
    "error",
    "stale",
}

# Sections that are optional and may be dropped (largest first) to honour a token budget.
DROPPABLE_SECTIONS_PRIORITY = (
    "agent_memory_summary",
    "memory_summary",
    "core_reasoning",
    "foresight",
)

DEFAULT_TOKEN_BUDGET_CHARS = 6000
COMPACT_TOKEN_BUDGET_CHARS = 1800


class ContextScopeFilter:
    """Filters the agent context patch so only task-relevant, clean context is sent.

    Responsibilities:
    - Route gateway diagnostics / workspace-file fallback dumps to audit, never the prompt.
    - Bound memory summaries to a small relevant slice.
    - Enforce a serialized character budget, dropping the largest optional sections first.
    - Emit a ``context_scope`` report describing what was included / omitted and why.
    """

    def apply(
        self,
        context_patch: dict[str, Any],
        *,
        user_goal: str = "",
        token_budget_chars: int = DEFAULT_TOKEN_BUDGET_CHARS,
    ) -> dict[str, Any]:
        clean = dict(context_patch or {})
        omitted_sections: list[str] = []
        omitted_audit: dict[str, Any] = {}

        # 1. agent_memory_summary: drop diagnostic/fallback dumps, keep only trusted gateway memory.
        ams = clean.get("agent_memory_summary")
        if isinstance(ams, dict):
            status = str(ams.get("status") or "")
            is_diagnostic = status in DIAGNOSTIC_MEMORY_STATUSES or bool(ams.get("gateway_error"))
            if is_diagnostic:
                clean.pop("agent_memory_summary", None)
                omitted_sections.append("agent_memory_summary")
                omitted_audit["agent_memory_summary"] = {
                    "status": status,
                    "reason": "diagnostic_or_fallback_routed_to_audit",
                    "gateway_error": ams.get("gateway_error"),
                    "files": ams.get("files"),
                    "sync_status": ams.get("sync_status"),
                }
            else:
                clean["agent_memory_summary"] = {
                    "summary": _cap_text(ams.get("summary"), 1200),
                    "status": status,
                    "runtime": ams.get("runtime"),
                }

        # 2. memory_summary: strip diagnostics, bound text, drop when empty.
        memory_items_count = 0
        ms = clean.get("memory_summary")
        if isinstance(ms, dict):
            local_summary = ms.get("summary")
            external = ms.get("external_summary") if isinstance(ms.get("external_summary"), dict) else {}
            external_status = str(external.get("status") or "")
            external_trust = str(external.get("trust") or "")
            external_is_diagnostic = (
                external_status in DIAGNOSTIC_MEMORY_STATUSES
                or external_trust in {"workspace_file_fallback", "untrusted"}
            )
            external_text = "" if external_is_diagnostic else _as_text(external.get("summary") if isinstance(external, dict) else None)
            has_content = bool(_as_text(local_summary).strip()) or bool(external_text.strip())
            if not has_content:
                clean.pop("memory_summary", None)
                omitted_sections.append("memory_summary")
                omitted_audit["memory_summary"] = {
                    "reason": "empty_or_irrelevant",
                    "external_status": external_status or None,
                }
            else:
                memory_items_count = _count_items(local_summary)
                clean["memory_summary"] = {
                    "summary": _cap_any(local_summary, 1400),
                    "external_summary": _cap_text(external_text, 800) if external_text else "",
                    "freshness": ms.get("freshness"),
                    "trust": ms.get("trust"),
                    "relevance": ms.get("relevance"),
                }

        # 3. Global redaction pass (local paths, secrets, oversized strings/lists).
        clean = redact_sensitive(clean, max_string=900, max_list=12)

        # 4. Enforce serialized character budget.
        serialized_chars, budget_omitted = self._enforce_budget(clean, token_budget_chars)
        for section in budget_omitted:
            if section not in omitted_sections:
                omitted_sections.append(section)
            omitted_audit.setdefault(section, {"reason": "token_budget_exceeded"})
        serialized_chars = len(json.dumps(clean, ensure_ascii=False, sort_keys=True))

        budget_label = "compact" if token_budget_chars <= COMPACT_TOKEN_BUDGET_CHARS else "standard"
        clean["context_scope"] = {
            "included_sections": sorted(k for k in clean.keys() if k != "context_scope"),
            "omitted_sections": omitted_sections,
            "relevance_reason": _relevance_reason(user_goal, omitted_sections),
            "token_budget": budget_label,
            "token_budget_chars": token_budget_chars,
            "memory_items_count": memory_items_count,
            "debug_omitted": bool(omitted_audit),
            "serialized_context_chars": serialized_chars,
        }
        return {
            "context_patch": clean,
            "scope": clean["context_scope"],
            "omitted_audit": omitted_audit,
        }

    def _enforce_budget(self, clean: dict[str, Any], budget: int) -> tuple[int, list[str]]:
        dropped: list[str] = []
        serialized_chars = len(json.dumps(clean, ensure_ascii=False, sort_keys=True))
        for section in DROPPABLE_SECTIONS_PRIORITY:
            if serialized_chars <= budget:
                break
            if section in clean:
                clean.pop(section, None)
                dropped.append(section)
                serialized_chars = len(json.dumps(clean, ensure_ascii=False, sort_keys=True))
        return serialized_chars, dropped


def _relevance_reason(user_goal: str, omitted_sections: list[str]) -> str:
    goal = (user_goal or "").strip()
    if not omitted_sections:
        return f"All retained context judged relevant to: {goal[:80]}" if goal else "All retained context relevant."
    return (
        f"Omitted {', '.join(omitted_sections)} as unrelated diagnostics/oversized context "
        f"for goal: {goal[:80]}" if goal else f"Omitted {', '.join(omitted_sections)} as unrelated/oversized."
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        items = value.get("items")
        return len(items) if isinstance(items, list) else (1 if value else 0)
    return 1 if _as_text(value).strip() else 0


def _cap_text(value: Any, limit: int) -> str:
    text = _as_text(value)
    return text[:limit]


def _cap_any(value: Any, limit: int) -> Any:
    if isinstance(value, list):
        return value[:5]
    if isinstance(value, dict):
        return value
    return _cap_text(value, limit)


TENANT_SCOPE = "tenant"
OPERATOR_GLOBAL_SCOPE = "operator_global"

_SAFE_OPERATOR_GLOBAL_PROBES = {
    "git_probe",
    "hermes_probe",
    "mcp_probe",
    "openclaw_probe",
    "port_probe",
    "process_probe",
    "system_probe",
    "time_probe",
}
_SCOPE_CONTAINER_KEYS = ("task_context", "evidence", "owner", "scope")
_SCOPE_KIND_KEYS = ("scope_kind", "visibility_scope")


def owner_scope(item: Any) -> tuple[str, str, str]:
    """Resolve an owner envelope as (state, user_id, session_id).

    state is one of exact, ownerless, or invalid. Conflicting direct/nested
    owner fields and partial owner envelopes are invalid and therefore fail
    closed at context boundaries.
    """

    if not isinstance(item, dict):
        return ("invalid", "", "")
    containers = _scope_containers(item)
    users = _field_values(containers, "user_id")
    sessions = _field_values(containers, "session_id")
    if len(users) > 1 or len(sessions) > 1:
        return ("invalid", "", "")
    user_id = next(iter(users), "")
    session_id = next(iter(sessions), "")
    if bool(user_id) != bool(session_id):
        return ("invalid", user_id, session_id)
    if not user_id:
        return ("ownerless", "", "")
    try:
        user_id = normalize_scope_component(user_id, "user_id")
        session_id = normalize_scope_component(
            session_id,
            "session_id",
        )
    except ValueError:
        return ("invalid", "", "")
    return ("exact", user_id, session_id)


def scope_kind(item: Any) -> str:
    """Return one scope kind, or invalid when nested scope metadata conflicts."""

    if not isinstance(item, dict):
        return "invalid"
    values: set[str] = set()
    for container in _scope_containers(item):
        for key in _SCOPE_KIND_KEYS:
            value = str(container.get(key) or "").strip().lower()
            if value:
                values.add(value)
        nested_scope = container.get("scope")
        if isinstance(nested_scope, dict):
            value = str(nested_scope.get("kind") or "").strip().lower()
            if value:
                values.add(value)
    if len(values) > 1:
        return "invalid"
    return next(iter(values), "")


def item_visible_to_scope(
    item: Any,
    *,
    user_id: str,
    session_id: str,
    probe_name: str | None = None,
) -> bool:
    """Allow exact tenant data or explicitly defined operator-global probes."""

    if not isinstance(item, dict):
        return False
    owner_state, _, _ = owner_scope(item)
    kind = scope_kind(item)
    if owner_state == "invalid" or kind == "invalid":
        return False
    if owner_state == "exact":
        return exact_owner_visible(
            item,
            user_id=user_id,
            session_id=session_id,
        )
    if kind == TENANT_SCOPE or _tenant_derived(item):
        return False
    return kind == OPERATOR_GLOBAL_SCOPE or is_operator_global_probe(
        item,
        probe_name=probe_name,
    )


def exact_owner_visible(
    item: Any,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    """Require one consistent, complete owner envelope with an exact match."""

    owner_state, item_user, item_session = owner_scope(item)
    kind = scope_kind(item)
    try:
        requested_user = normalize_scope_component(user_id, "user_id")
        requested_session = normalize_scope_component(
            session_id,
            "session_id",
        )
    except ValueError:
        return False
    return (
        owner_state == "exact"
        and kind not in {"invalid", OPERATOR_GLOBAL_SCOPE}
        and item_user == requested_user
        and item_session == requested_session
    )


def tenant_scope_storage_key(user_id: Any, session_id: Any) -> str:
    """Return a collision-safe private cache key for one exact tenant scope."""

    normalized_user = normalize_scope_component(user_id, "user_id")
    normalized_session = normalize_scope_component(
        session_id,
        "session_id",
    )
    return "scope-" + framed_sha256(
        "veyra-context-scope-v1",
        normalized_user,
        normalized_session,
    )[:32]


def visible_probe_map(
    local_world: dict[str, Any],
    *,
    user_id: str,
    session_id: str,
) -> dict[str, dict[str, Any]]:
    """Project operator-global plus exact tenant probe caches for one turn."""

    visible: dict[str, dict[str, Any]] = {}
    probes = (
        local_world.get("probes")
        if isinstance(local_world.get("probes"), dict)
        else {}
    )
    for name, payload in probes.items():
        if isinstance(payload, dict) and item_visible_to_scope(
            payload,
            user_id=user_id,
            session_id=session_id,
            probe_name=str(name),
        ):
            visible[str(name)] = payload
    try:
        scope_key = tenant_scope_storage_key(user_id, session_id)
    except ValueError:
        return visible
    scoped_root = (
        local_world.get("scoped_probes")
        if isinstance(local_world.get("scoped_probes"), dict)
        else {}
    )
    scoped = (
        scoped_root.get(scope_key)
        if isinstance(scoped_root.get(scope_key), dict)
        else {}
    )
    for name, payload in scoped.items():
        if isinstance(payload, dict) and exact_owner_visible(
            payload,
            user_id=user_id,
            session_id=session_id,
        ):
            visible[str(name)] = payload
    return visible


def probe_scope_metadata(
    probe_result: dict[str, Any],
    *,
    probe_name: str | None = None,
) -> dict[str, Any]:
    """Build canonical scope metadata for a persisted probe and its claims."""

    owner_state, user_id, session_id = owner_scope(probe_result)
    kind = scope_kind(probe_result)
    if owner_state == "exact" and kind not in {
        "invalid",
        OPERATOR_GLOBAL_SCOPE,
    }:
        return {
            "scope_kind": TENANT_SCOPE,
            "tenant_derived": True,
            "user_id": user_id,
            "session_id": session_id,
        }
    if (
        owner_state == "ownerless"
        and kind != TENANT_SCOPE
        and (
            kind == OPERATOR_GLOBAL_SCOPE
            or is_operator_global_probe(probe_result, probe_name=probe_name)
        )
    ):
        return {"scope_kind": OPERATOR_GLOBAL_SCOPE}
    return {
        "scope_kind": TENANT_SCOPE,
        "tenant_derived": True,
        "scope_status": "invalid" if owner_state == "invalid" or kind == "invalid" else "ownerless",
    }


def is_operator_global_probe(
    item: dict[str, Any],
    *,
    probe_name: str | None = None,
) -> bool:
    """Recognize the narrow legacy/system probe set safe to share globally."""

    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw_name = str(
        probe_name
        or item.get("probe")
        or evidence.get("probe")
        or item.get("source")
        or ""
    ).strip().lower()
    if raw_name.startswith("core_model:"):
        raw_name = raw_name.split(":", 1)[1]
    if raw_name in _SAFE_OPERATOR_GLOBAL_PROBES:
        return True
    if raw_name not in {"network_probe", "web_probe"}:
        return False
    target = str(
        item.get("target")
        or item.get("host")
        or evidence.get("target")
        or evidence.get("host")
        or ""
    ).strip()
    return _is_loopback_target(target)


def _scope_containers(item: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [item]
    for key in _SCOPE_CONTAINER_KEYS:
        nested = item.get(key)
        if isinstance(nested, dict) and nested not in containers:
            containers.append(nested)
            nested_scope = nested.get("scope")
            if isinstance(nested_scope, dict) and nested_scope not in containers:
                containers.append(nested_scope)
    return containers


def _field_values(
    containers: list[dict[str, Any]],
    field: str,
) -> set[str]:
    return {
        value
        for container in containers
        if (value := str(container.get(field) or "").strip())
    }


def _tenant_derived(item: dict[str, Any]) -> bool:
    return any(
        bool(container.get("tenant_derived"))
        for container in _scope_containers(item)
    )


def _is_loopback_target(target: str) -> bool:
    lowered = target.lower()
    if not lowered:
        return False
    candidate = lowered if "://" in lowered else f"//{lowered}"
    parsed = urlparse(candidate)
    host = str(parsed.hostname or "").strip("[]")
    if not host and lowered in {"localhost", "127.0.0.1", "::1"}:
        host = lowered
    return host in {"localhost", "127.0.0.1", "::1"}
