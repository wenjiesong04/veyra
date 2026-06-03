from __future__ import annotations

import json
from typing import Any

from core.model_client import redact_sensitive

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
