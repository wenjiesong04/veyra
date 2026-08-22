"""Strict contracts for the first user-life Situation slice.

The objects in this module are model *candidates* and bounded projections.  A
candidate never carries a route, tool invocation, authority grant, or an
unbounded external query.  ``LivingContextRuntime`` is responsible for
checking owner scope and for admitting the candidate into the authoritative
Situation evaluator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Literal, Mapping

from common.reported_time_window import resolve_reported_window_end
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


SCHEMA_VERSION = "veyra.living_context_candidate.v1"
INFORMATION_NEED_SCHEMA_VERSION = "veyra.information_need.v1"
MAX_HISTORY = 24
CATALOG_SELECTOR_FIELD = "situation_selector"
TIMELINE_MAX_ITEMS = 12
TIMELINE_ITEM_FIELDS = ("statement", "occurred_at", "source_quote", "material")
_CATALOG_SELECTOR_RE = re.compile(r"^sitref_[0-9a-f]{16}$")

# These are deliberately broad product categories.  They are labels for
# ranking and filtering, not a registry of hard-coded life demos.
CATEGORY_VALUES = (
    "general",
    "personal",
    "work",
    "education",
    "health",
    "travel",
    "logistics",
    "finance",
    "other",
)
INFORMATION_NEED_SOURCE_CLASSES = (
    "user",
    "calendar",
    "email",
    "message",
    "weather",
    "public_web",
    "agent",
    "time",
    "other",
)
PROGRESS_STATUS_VALUES = (
    "unknown",
    "not_started",
    "in_progress",
    "blocked",
    "waiting",
    "completed",
)

# Validation locations are exposed in bounded diagnostics and repair prompts.
# Keep this allowlist aligned with the candidate/feedback schemas so a
# provider-controlled extra key can never be copied into an issue code.
_BOUNDARY_LOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "disposition",
        "situation_token",
        "situation_revision",
        "catalog_token",
        "create_subject",
        "category",
        "label",
        "title",
        "summary",
        "goal",
        "deadline_at",
        "progress",
        "status",
        "value",
        "entities",
        "kind",
        "known",
        "statement",
        "epistemic_status",
        "source_quote",
        "text",
        "start",
        "end",
        "lifecycle",
        "unknown",
        "assumptions",
        "timeline",
        "occurred_at",
        "material",
        "material_change",
        "next_observation_at",
        "next_step",
        "next_step_epistemic_status",
        "needs",
        "blocked_judgment",
        "evidence_kind",
        "why_now",
        "urgency",
        "expires_at",
        "allowed_source_classes",
        "fallback_reaction",
        "question",
        "answered_need_tokens",
        "answered_need_bindings",
        "need_token",
        "generation",
        "requested_reaction",
        "reopen",
        "reopen_reason",
        "assertion_mode",
        "source",
        "reaction_token",
        "remind_before_seconds",
    }
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+%#=~-]{0,239}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "url",
        "path",
        "query",
        "command",
        "tool",
        "tool_args",
        "arguments_for_tool",
        "credential",
        "credentials",
        "recipient",
        "authority",
        "route",
        "risk",
        "state_effect",
        "capability_grant",
        "allowed_capabilities",
        "execution",
    }
)

# Model output is normalized only at this narrow boundary.  The maps below
# are exact aliases, not keyword heuristics: an unknown value is left intact
# and is rejected by the strict Pydantic model below.  In particular, these
# maps never touch server-issued Situation/Need/reaction tokens or revisions.
_ENUM_ALIASES: dict[str, dict[str, str]] = {
    "disposition": {
        "创建": "create",
        "新建": "create",
        "更新": "update",
        "修改": "update",
        "纠正": "correct",
        "更正": "correct",
        "解决": "resolve",
        "已解决": "resolve",
        "安静": "quiet",
        "静默": "quiet",
        "无": "quiet",
    },
    "category": {
        "旅行": "travel",
        "出行": "travel",
        "差旅": "travel",
        "工作": "work",
        "职场": "work",
        "学习": "education",
        "教育": "education",
        "健康": "health",
        "医疗": "health",
        "搬家": "logistics",
        "物流": "logistics",
        "生活": "personal",
        "个人": "personal",
        "财务": "finance",
        "金融": "finance",
        "一般": "general",
        "其他": "other",
    },
    "lifecycle": {
        "新出现": "emerging",
        "活跃": "active",
        "进行中": "active",
        "等待": "waiting",
        "等待中": "waiting",
        "已解决": "resolved",
        "完成": "resolved",
        "已过期": "expired",
        "矛盾": "contradicted",
        "已归档": "archived",
    },
    "progress.status": {
        "未知": "unknown",
        "未开始": "not_started",
        "尚未开始": "not_started",
        "进行中": "in_progress",
        "正在进行": "in_progress",
        "受阻": "blocked",
        "卡住": "blocked",
        "等待": "waiting",
        "等待中": "waiting",
        "完成": "completed",
        "已完成": "completed",
    },
    "requested_reaction": {
        "询问": "ask",
        "提问": "ask",
        "读取": "read",
        "读": "read",
        "等待": "wait",
        "静默": "silent",
        "沉默": "silent",
    },
    "fallback_reaction": {
        "询问": "ask",
        "提问": "ask",
        "读取": "read",
        "读": "read",
        "等待": "wait",
        "静默": "silent",
        "沉默": "silent",
    },
    "feedback.label": {
        "忽略": "ignore",
        "不用管": "ignore",
        "已解决": "resolved",
        "有用": "useful",
        "有帮助": "useful",
        "没用": "not_useful",
        "无用": "not_useful",
        "太早": "too_early",
        "太晚": "too_late",
        "太频繁": "too_frequent",
        "提前提醒": "remind_before",
    },
    "evidence_kind": {
        "用户": "user",
        "用户输入": "user",
        "user_input": "user",
        "日历": "calendar",
        "行程": "calendar",
        "邮件": "email",
        "邮箱": "email",
        "消息": "message",
        "聊天": "message",
        "短信": "message",
        "天气": "weather",
        "公开网络": "public_web",
        "公开网页": "public_web",
        "public web": "public_web",
        "public-web": "public_web",
        "网页": "public_web",
        "web": "public_web",
        "搜索": "public_web",
        "代理": "agent",
        "研究": "agent",
        "时间": "time",
        "其他": "other",
    },
    # Model providers occasionally use the transport-facing name for a
    # source class.  These are exact aliases only; arbitrary/unknown values
    # remain untouched and are rejected by the strict Literal below.
    "allowed_source_class": {
        "user_message": "user",
        "user_input": "user",
    },
    "entity.kind": {
        "地点": "place",
        "位置": "place",
        "location": "place",
        "人物": "person",
        "人": "person",
        "组织": "organization",
        "公司": "organization",
        "机构": "organization",
        "物品": "item",
        "事项": "item",
        "其他": "other",
    },
    "epistemic_status": {
        "报告": "reported",
        "已报告": "reported",
        "推断": "inferred",
        "推测": "inferred",
    },
    "next_step_epistemic_status": {
        "报告": "reported",
        "已报告": "reported",
        "推断": "inferred",
        "推测": "inferred",
    },
    "assertion_mode": {
        "直接用户": "direct_user",
        "用户直接断言": "direct_user",
        "推断": "inferred",
        "服务器命令": "server_command",
    },
}

# InformationNeed source classes use the same exact bilingual/English alias
# vocabulary as ``evidence_kind``.  Keep the separate transport-facing aliases
# (for example ``user_message``) layered on top without broadening the
# canonical source whitelist.
_ENUM_ALIASES["allowed_source_class"] = {
    **_ENUM_ALIASES["evidence_kind"],
    **_ENUM_ALIASES["allowed_source_class"],
}

_CANDIDATE_NULL_STRING_FIELDS = (
    "create_subject",
    "label",
    "title",
    "summary",
    "goal",
    "material_change",
    "next_step",
    "reopen_reason",
)
_NESTED_NULL_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "known": ("statement",),
    "timeline": ("statement",),
    "assumptions": ("statement",),
    "needs": ("blocked_judgment", "why_now", "question"),
    "entities": ("value",),
}
_SINGLETON_COLLECTION_ITEM_KEYS: dict[str, frozenset[str]] = {
    "known": frozenset({"statement", "source_quote", "epistemic_status"}),
    "assumptions": frozenset({"statement", "epistemic_status"}),
    "entities": frozenset({"kind", "value", "epistemic_status", "source_quote"}),
    "needs": frozenset(
        {
            "blocked_judgment",
            "evidence_kind",
            "why_now",
            "urgency",
            "expires_at",
            "allowed_source_classes",
            "fallback_reaction",
            "question",
        }
    ),
}
_CANDIDATE_NULL_DEFAULTS: dict[str, Any] = {
    "category": "general",
    "lifecycle": "active",
    "next_step_epistemic_status": "inferred",
    "requested_reaction": "wait",
    "reopen": False,
    "assertion_mode": "inferred",
}
_LIFECYCLE_AUTHORITY_DOWNGRADED_REPAIRED_FIELD = (
    "disposition:lifecycle_authority_downgraded_to_update"
)
_NON_TERMINAL_LIFECYCLES = frozenset({"active", "emerging", "waiting"})
_CANDIDATE_NULL_LIST_FIELDS = (
    "entities",
    "known",
    "unknown",
    "assumptions",
    "timeline",
    "needs",
    "answered_need_tokens",
    "answered_need_bindings",
)
_QUIET_CANDIDATE_FIELDS = frozenset({
    "schema_version",
    "disposition",
    "source",
})


def situation_catalog_selector(
    owner_id: str,
    session_id: str,
    situation_token: str,
) -> str:
    """Return a short opaque selector for one scoped Situation row.

    The selector is only a transport aid for model output.  It is derived
    from the server-issued scope and Situation token and is never accepted as
    a durable binding by itself; callers must resolve it against the exact
    current catalog before validating the candidate/CAS triple.
    """

    payload = json.dumps(
        {
            "owner_id": str(owner_id),
            "session_id": str(session_id),
            "situation_token": str(situation_token),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sitref_{hashlib.sha256(payload).hexdigest()[:16]}"


def _catalog_selector_row_matches(
    selector: str,
    catalog: list[Mapping[str, Any]] | list[Any] | None,
) -> list[Mapping[str, Any]]:
    """Find rows whose server-derived selector exactly matches ``selector``."""

    matches: list[Mapping[str, Any]] = []
    if not isinstance(selector, str) or not _CATALOG_SELECTOR_RE.fullmatch(selector):
        return matches
    for raw_row in catalog or []:
        if not isinstance(raw_row, Mapping):
            continue
        situation_token = raw_row.get("situation_token")
        if not isinstance(situation_token, str) or not situation_token:
            continue
        owner_id = raw_row.get("owner_id")
        session_id = raw_row.get("session_id")
        if (
            not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(session_id, str)
            or not session_id
        ):
            continue
        derived = situation_catalog_selector(
            owner_id,
            session_id,
            situation_token,
        )
        advertised = raw_row.get(CATALOG_SELECTOR_FIELD)
        # A catalog-provided selector is trusted only when it agrees with the
        # deterministic server derivation.  This keeps hand-shaped or stale
        # transport rows from becoming an alternate authority.
        if advertised is not None and advertised != derived:
            continue
        if selector == derived:
            matches.append(raw_row)
    return matches

def _normalize_bounded_number(value: Any) -> tuple[Any, bool]:
    """Normalize finite 0..1 transport numbers without guessing semantics."""

    if isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return float(value), True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value, False
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return value, False
        if math.isfinite(parsed) and 0.0 <= parsed <= 1.0:
            return parsed, True
    return value, False


def _safe_boundary_location(location: Any) -> str:
    """Project Pydantic locations to known fields and bounded indices."""

    if not isinstance(location, (tuple, list)):
        return "root"
    parts: list[str] = []
    for item in location:
        if type(item) is int and item >= 0:
            parts.append(str(item))
        elif isinstance(item, str) and item in _BOUNDARY_LOCATION_FIELDS:
            parts.append(item)
        else:
            parts.append("extra_field")
    return ".".join(parts) or "root"


def _boundary_issue_codes(exc: Exception, *, prefix: str) -> list[str]:
    """Return structured validation codes without echoing model values."""

    if isinstance(exc, ValidationError):
        issues: list[str] = []
        for error in exc.errors(include_context=False):
            location = _safe_boundary_location(error.get("loc") or ())
            code = str(error.get("type") or "validation_error")
            # Pydantic reports model-validator failures as a generic root
            # ``value_error``.  Preserve a small allowlist of stable semantic
            # codes for repair prompts/diagnostics without ever copying the
            # validator's message (which can contain provider values).
            message = str(
                (error.get("ctx") or {}).get("error")
                or error.get("msg")
                or ""
            ).lower()
            for marker, safe_code in (
                ("unsupported informationneed source class", "unsupported_source_class"),
                ("create candidates require create_subject", "create_subject_missing"),
                ("create candidates cannot carry catalog bindings", "create_binding_conflict"),
                ("create candidates cannot target an existing token", "create_token_conflict"),
                ("existing situation disposition requires situation_token", "update_token_missing"),
                ("existing situation disposition requires situation_revision", "update_revision_missing"),
                ("existing situation disposition requires catalog_token", "update_catalog_missing"),
                ("quiet candidates cannot carry a situation mutation", "quiet_mutation_conflict"),
                ("answered_need_bindings must exactly match", "need_binding_mismatch"),
                ("lifecycle mutation requires a direct user assertion", "lifecycle_assertion_missing"),
                ("resolved lifecycle requires resolve or correct", "resolved_lifecycle_conflict"),
                ("reopen requires the correct disposition", "reopen_disposition_conflict"),
                ("reopen requires a bounded reason", "reopen_reason_missing"),
            ):
                if marker in message:
                    code = safe_code
                    break
            issues.append(f"{prefix}:{location}:{code}")
        return issues[:16]
    return [f"{prefix}:exception:{type(exc).__name__}"]


def _alias_value(value: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    selected = value.strip()
    if selected in aliases:
        return aliases[selected]
    lowered = selected.lower()
    return aliases.get(lowered, value)


def normalize_information_need_source_class(value: Any) -> Any:
    """Apply the existing exact alias table without widening source classes."""

    return _alias_value(value, _ENUM_ALIASES["allowed_source_class"])


def _normalize_candidate_payload(
    value: Any,
    *,
    catalog: list[dict[str, Any]] | list[Any] | None = None,
) -> tuple[Any, list[str]]:
    """Apply only approved, lossless model-boundary repairs.

    This function intentionally does not repair tokens, revisions, quotes,
    arbitrary objects, or unknown enum values.  Such values remain untouched
    and are rejected by the strict contract.
    """

    if not isinstance(value, dict):
        return value, []
    payload = copy.deepcopy(value)
    repaired: list[str] = []
    missing = object()
    raw_reopen = payload.get("reopen", missing)
    raw_lifecycle = payload.get("lifecycle", missing)
    raw_assertion_mode = payload.get("assertion_mode", missing)

    # Some providers place the Need-only evidence_kind at candidate root. It
    # is fixed transport noise: discard this exact field, never move it into
    # a Need or infer a source class. Other unknown root fields remain strict.
    if "evidence_kind" in payload:
        payload.pop("evidence_kind", None)
        repaired.append("evidence_kind:omitted_root_transport")

    # ``situation_selector`` is a short, server-derived row selector for
    # providers that have trouble copying long opaque tokens.  It is
    # transport-only: resolve it against this exact catalog, copy the full
    # binding triple from that one row, and remove the selector before the
    # strict Pydantic contract sees the payload.  Unknown, stale, or
    # ambiguous selectors remain fail-closed even when the payload also
    # carries plausible-looking binding fields.
    if CATALOG_SELECTOR_FIELD in payload:
        selector = payload.pop(CATALOG_SELECTOR_FIELD)
        if selector in (None, ""):
            # Empty optional transport values are treated as absent.  An
            # existing disposition still has to provide its full binding.
            repaired.append("situation_selector:empty")
        else:
            disposition = _alias_value(
                payload.get("disposition"),
                _ENUM_ALIASES["disposition"],
            )
            matches = _catalog_selector_row_matches(str(selector), catalog)
            if disposition in {"update", "correct", "resolve"} and len(matches) == 1:
                row = matches[0]
                row_situation_token = row.get("situation_token")
                row_revision = row.get("situation_revision")
                if row_revision is None:
                    row_revision = row.get("observation_revision")
                row_catalog_token = row.get("catalog_token")
                if (
                    isinstance(row_situation_token, str)
                    and row_situation_token
                    and type(row_revision) is int
                    and row_revision >= 1
                    and isinstance(row_catalog_token, str)
                    and row_catalog_token
                ):
                    # A valid selector is the explicit model choice of one
                    # server row.  Canonicalize any stale/partially copied
                    # triple to that row, then let the ordinary validation
                    # and final live CAS enforce the revision fence.
                    payload["situation_token"] = row_situation_token
                    payload["situation_revision"] = row_revision
                    payload["catalog_token"] = row_catalog_token
                    repaired.append("situation_binding:from_selector")
                else:
                    repaired.append("situation_selector:rejected")
            else:
                # A selector cannot target a create/quiet candidate and must
                # not be guessed when the current catalog cannot prove one
                # unique row.  The parser turns this marker into a safe issue
                # code before strict model validation.
                repaired.append("situation_selector:rejected")

    # Normalize the disposition discriminator before applying any
    # disposition-specific transport defaults.  Otherwise aliases such as
    # ``新建`` are validated as ``create`` only after the create defaults have
    # already been skipped.
    if "disposition" in payload:
        before_disposition = payload["disposition"]
        after_disposition = _alias_value(
            before_disposition,
            _ENUM_ALIASES["disposition"],
        )
        if after_disposition != before_disposition:
            payload["disposition"] = after_disposition
            repaired.append("disposition")

    # Provenance is owned by this server-side model seam.  A provider may
    # omit it, serialize null, or use an arbitrary literal, but none of those
    # values can change the fact that this candidate came from the model
    # boundary.  Normalize the field before strict validation and retain only
    # the bounded field-level repair marker.
    if payload.get("source") != "model":
        payload["source"] = "model"
        repaired.append("source")

    # Some JSON providers collapse a one-item array into its object. Wrapping
    # an exact item-shaped mapping is a lossless transport repair; mappings
    # with any unknown key remain untouched and are rejected by the strict
    # contract rather than being silently sanitized.
    for collection_name, item_keys in _SINGLETON_COLLECTION_ITEM_KEYS.items():
        item = payload.get(collection_name)
        if isinstance(item, dict) and set(map(str, item.keys())).issubset(item_keys):
            payload[collection_name] = [item]
            repaired.append(f"{collection_name}:singleton_list")

    # ``material_change`` is an optional display field.  Preserve text, keep
    # null/empty text as the model's no-value representation, and omit other
    # transport shapes instead of stringifying them or rejecting the whole
    # Situation candidate.  Core fields remain strict below.
    if "material_change" in payload:
        material_change = payload["material_change"]
        if material_change is not None and not isinstance(material_change, str):
            payload.pop("material_change")
            repaired.append("material_change:omitted_invalid_transport")

    for field_name in _CANDIDATE_NULL_STRING_FIELDS:
        if field_name in payload and payload[field_name] is None:
            payload[field_name] = ""
            repaired.append(field_name)

    for field_name, default in _CANDIDATE_NULL_DEFAULTS.items():
        if field_name in payload and payload[field_name] is None:
            payload[field_name] = default
            repaired.append(f"{field_name}:default")

    for field_name in _CANDIDATE_NULL_LIST_FIELDS:
        if field_name in payload and payload[field_name] is None:
            payload[field_name] = []
            repaired.append(f"{field_name}:empty_list")

    if payload.get("disposition") != "quiet" and payload.get("progress") is None:
        payload["progress"] = {"status": "unknown", "value": None}
        repaired.append("progress:default")

    if payload.get("disposition") == "create":
        for field_name in ("situation_token", "catalog_token"):
            value = payload.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                payload[field_name] = None
                repaired.append(f"{field_name}:create_absent")
            else:
                # A create proposal cannot inherit an old Situation binding.
                # Clear it at the model boundary; never select a catalog row
                # or reinterpret the value as a new server token.
                payload[field_name] = None
                repaired.append(f"{field_name}:create_binding_cleared")
        create_revision = payload.get("situation_revision")
        if create_revision is None or create_revision == "" or create_revision == 0:
            payload["situation_revision"] = None
            repaired.append("situation_revision:create_absent")
        else:
            payload["situation_revision"] = None
            repaired.append("situation_revision:create_binding_cleared")

    if (
        payload.get("disposition") == "create"
        and not str(payload.get("goal") or "").strip()
        and isinstance(payload.get("create_subject"), str)
        and payload["create_subject"].strip()
    ):
        # A create subject is already the model's user-grounded statement of
        # what the Situation is about. Reusing that exact string as the first
        # goal is a transparent bootstrap, not a domain-specific inference;
        # later turns may refine it through normal revision/CAS.
        payload["goal"] = payload["create_subject"]
        repaired.append("goal:from_create_subject")

    for field_name, aliases in _ENUM_ALIASES.items():
        if "." in field_name:
            continue
        if field_name in payload:
            before = payload[field_name]
            after = _alias_value(before, aliases)
            if after != before:
                payload[field_name] = after
                repaired.append(field_name)

    progress = payload.get("progress")
    if isinstance(progress, dict) and "status" in progress:
        before = progress["status"]
        after = _alias_value(before, _ENUM_ALIASES["progress.status"])
        if after != before:
            progress["status"] = after
            repaired.append("progress.status")

    # Providers commonly serialize JSON numbers without preserving whether a
    # value was written as an integer/decimal, and some return a bounded number
    # as a JSON string. Pydantic's strict float field rejects those transport
    # forms, although normalizing a finite 0..1 number does not change its
    # proposed meaning. Keep this repair finite and field-specific; arbitrary
    # strings/numbers remain rejected below.
    if isinstance(progress, dict):
        before = progress.get("value")
        after, changed = _normalize_bounded_number(before)
        if changed:
            progress["value"] = after
            repaired.append("progress.value")
        elif "value" in progress and before is not None:
            # The ratio is optional context, not the user's reported status.
            # Do not let a provider's object, percentage, NaN, or other
            # malformed transport value reject an otherwise valid Situation
            # update.  Omit it and expose only a bounded repair code; never
            # coerce the malformed value into a guessed number.
            progress.pop("value", None)
            repaired.append("progress.value:omitted_invalid_transport")

    # A model can describe an ordinary child-Need update as ``correct`` or
    # ``resolve`` while omitting lifecycle authority. Once exact aliases have
    # been normalized, downgrade only that non-terminal, non-reopen, inferred
    # case to the lower-authority ``update`` disposition. Keep every
    # lifecycle-bearing, terminal, reopen, server-command, and direct-user
    # candidate fail-closed for the normal strict contract.
    if (
        payload.get("disposition") in {"correct", "resolve"}
        and (raw_reopen is missing or raw_reopen is False)
        and raw_lifecycle is not missing
        and payload.get("lifecycle") in _NON_TERMINAL_LIFECYCLES
        and (
            raw_assertion_mode is missing
            or (
                raw_assertion_mode is not None
                and payload.get("assertion_mode") == "inferred"
            )
        )
    ):
        payload["disposition"] = "update"
        repaired.append(_LIFECYCLE_AUTHORITY_DOWNGRADED_REPAIRED_FIELD)

    for collection_name, field_names in _NESTED_NULL_STRING_FIELDS.items():
        rows = payload.get(collection_name)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, str) and collection_name == "assumptions":
                # Kept below as a separate explicit conversion so a string is
                # never silently treated as a reported fact.
                continue
            if not isinstance(row, dict):
                continue
            if collection_name == "needs":
                blocked = row.get("blocked_judgment")
                question = row.get("question")
                if (
                    (not isinstance(blocked, str) or not blocked.strip())
                    and isinstance(question, str)
                    and question.strip()
                ):
                    # ``question`` is the same Need's user-facing wording;
                    # copying it is structural, not semantic inference.  Do
                    # not manufacture a blocked judgment when the question
                    # itself is absent or empty.
                    row["blocked_judgment"] = question
                    repaired.append(f"needs[{index}].blocked_judgment:from_question")
            for field_name in field_names:
                if field_name in row and row[field_name] is None:
                    row[field_name] = ""
                    repaired.append(f"{collection_name}[{index}].{field_name}")
            if collection_name == "needs":
                before = row.get("urgency")
                after, changed = _normalize_bounded_number(before)
                if changed:
                    row["urgency"] = after
                    repaired.append(f"needs[{index}].urgency")

    assumptions = payload.get("assumptions")
    if isinstance(assumptions, list):
        for index, row in enumerate(assumptions):
            if isinstance(row, str):
                assumptions[index] = {
                    "statement": row,
                    "epistemic_status": "inferred",
                }
                repaired.append(f"assumptions[{index}]")

    # Nested enum aliases are exact and finite.  Unknown values stay unchanged.
    for collection_name, alias_key in (
        ("needs", "evidence_kind"),
        ("needs", "fallback_reaction"),
        ("entities", "entity.kind"),
        ("known", "epistemic_status"),
        ("assumptions", "epistemic_status"),
        ("entities", "epistemic_status"),
    ):
        rows = payload.get(collection_name)
        if not isinstance(rows, list):
            continue
        aliases = _ENUM_ALIASES[alias_key]
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            field_name = "kind" if alias_key == "entity.kind" else alias_key
            if field_name not in row:
                continue
            before = row[field_name]
            after = _alias_value(before, aliases)
            if after != before:
                row[field_name] = after
                repaired.append(f"{collection_name}[{index}].{field_name}")

    # Source-class aliases are normalized per item so the boundary report
    # records the repair while the CandidateNeed model still rejects every
    # value outside the canonical whitelist.
    needs = payload.get("needs")
    if isinstance(needs, list):
        aliases = _ENUM_ALIASES["allowed_source_class"]
        for index, row in enumerate(needs):
            if not isinstance(row, dict) or not isinstance(row.get("allowed_source_classes"), list):
                continue
            for source_index, before in enumerate(row["allowed_source_classes"]):
                after = _alias_value(before, aliases)
                if after != before:
                    row["allowed_source_classes"][source_index] = after
                    repaired.append(f"needs[{index}].allowed_source_classes[{source_index}]")

    return payload, repaired[:32]


def _normalize_feedback_payload(value: Any) -> tuple[Any, list[str]]:
    """Normalize only exact, documented feedback-label aliases."""

    if not isinstance(value, dict):
        return value, []
    payload = copy.deepcopy(value)
    label = payload.get("label")
    normalized = _alias_value(label, _ENUM_ALIASES["feedback.label"])
    if normalized == label:
        return payload, []
    payload["label"] = normalized
    return payload, ["label"]


class StrictLivingContextModel(BaseModel):
    """Pydantic boundary shared by model candidates and durable need records."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        validate_assignment=True,
    )


class ContextQuote(StrictLivingContextModel):
    text: str = Field(min_length=1, max_length=480)
    start: int = Field(ge=0, le=100_000)
    end: int = Field(gt=0, le=100_000)

    @model_validator(mode="after")
    def validate_range(self) -> "ContextQuote":
        if self.end <= self.start:
            raise ValueError("context quote end must be after start")
        return self


REACTION_FEEDBACK_SCHEMA_VERSION = "veyra.living_reaction_feedback.v1"
REACTION_FEEDBACK_LABELS = (
    "ignore",
    "resolved",
    "useful",
    "not_useful",
    "too_early",
    "too_late",
    "too_frequent",
    "remind_before",
)


class LivingReactionFeedback(StrictLivingContextModel):
    """A model-owned feedback proposal bound to the current reaction catalog.

    The token is deliberately opaque and the quote is mandatory.  The
    orchestrator rechecks both against the current server projection before
    allowing the reaction ledger to learn from it.
    """

    schema_version: Literal[REACTION_FEEDBACK_SCHEMA_VERSION]
    reaction_token: str = Field(min_length=1, max_length=240)
    label: Literal[
        "ignore",
        "resolved",
        "useful",
        "not_useful",
        "too_early",
        "too_late",
        "too_frequent",
        "remind_before",
    ]
    remind_before_seconds: int | None = Field(default=None, ge=0, le=30 * 86400)
    source_quote: ContextQuote

    @model_validator(mode="after")
    def validate_token(self) -> "LivingReactionFeedback":
        if not _TOKEN_RE.fullmatch(self.reaction_token):
            raise ValueError("reaction_token is not a valid server-issued token")
        if self.label == "remind_before" and self.remind_before_seconds is None:
            raise ValueError("remind_before feedback requires remind_before_seconds")
        if self.label != "remind_before" and self.remind_before_seconds is not None:
            raise ValueError("remind_before_seconds is only valid for remind_before")
        return self


class CandidateKnown(StrictLivingContextModel):
    statement: str = Field(min_length=1, max_length=480)
    source_quote: ContextQuote | None = None
    # A model may distinguish its summary from the user's report, but it may
    # never promote either one to verified fact in this candidate contract.
    epistemic_status: Literal["reported", "inferred"] = "reported"


class CandidateTimelineEntry(StrictLivingContextModel):
    statement: str = Field(min_length=1, max_length=480)
    occurred_at: str | None = Field(default=None, max_length=80)
    source_quote: ContextQuote | None = None
    material: bool = False

    @model_validator(mode="after")
    def validate_time(self) -> "CandidateTimelineEntry":
        if self.occurred_at and iso_time_or_none(self.occurred_at) is None:
            raise ValueError("occurred_at must be timezone-aware ISO-8601")
        return self


def quarantine_invalid_known_rows(
    value: Any,
    *,
    source_text: str,
) -> tuple[Any, int]:
    """Drop only Known rows whose optional source quote is not source-bound."""

    if not isinstance(value, Mapping):
        return value, 0
    known = value.get("known")
    if not isinstance(known, list) or len(known) > 12:
        return value, 0
    retained: list[Any] = []
    dropped = 0
    for row in known:
        has_quote = isinstance(row, Mapping) and "source_quote" in row
        quote = row.get("source_quote") if isinstance(row, Mapping) else None
        valid_quote = True
        if has_quote and quote is not None:
            valid_quote = bool(
                source_text
                and isinstance(quote, Mapping)
                and set(quote) == {"text", "start", "end"}
                and isinstance(quote.get("text"), str)
                and isinstance(quote.get("start"), int)
                and not isinstance(quote.get("start"), bool)
                and isinstance(quote.get("end"), int)
                and not isinstance(quote.get("end"), bool)
                and source_text[quote["start"] : quote["end"]] == quote["text"]
            )
        if has_quote and not valid_quote:
            dropped += 1
        else:
            retained.append(row)
    if dropped == 0:
        return value, 0
    payload = copy.deepcopy(dict(value))
    payload["known"] = retained
    return payload, dropped


def quarantine_invalid_timeline_rows(
    value: Any,
    *,
    source_text: str,
) -> tuple[Any, int]:
    """Drop whole invalid optional timeline rows without repairing them.

    This shared boundary helper is intentionally limited to a bounded list.
    Non-list and oversized values remain untouched for strict rejection. A
    row is retained only when its typed contract and optional source quote
    both validate; no row field is copied or rewritten.
    """

    if not isinstance(value, Mapping):
        return value, 0
    timeline = value.get("timeline")
    if not isinstance(timeline, list) or len(timeline) > TIMELINE_MAX_ITEMS:
        return value, 0
    retained: list[dict[str, Any]] = []
    dropped = 0
    for row in timeline:
        valid = isinstance(row, Mapping)
        if valid:
            try:
                CandidateTimelineEntry.model_validate(row, strict=True)
            except Exception:
                valid = False
        if valid:
            quote = row.get("source_quote")
            if quote is not None:
                valid = bool(
                    source_text
                    and isinstance(quote, Mapping)
                    and source_text[quote["start"] : quote["end"]] == quote["text"]
                ) if (
                    isinstance(quote, Mapping)
                    and isinstance(quote.get("start"), int)
                    and not isinstance(quote.get("start"), bool)
                    and isinstance(quote.get("end"), int)
                    and not isinstance(quote.get("end"), bool)
                    and isinstance(quote.get("text"), str)
                ) else False
        if valid:
            retained.append(dict(row))
        else:
            dropped += 1
    if dropped == 0:
        return value, 0
    payload = copy.deepcopy(dict(value))
    payload["timeline"] = retained
    return payload, dropped


class CandidateAssumption(StrictLivingContextModel):
    """A bounded model inference, never a verified fact."""

    statement: str = Field(min_length=1, max_length=480)
    epistemic_status: Literal["reported", "inferred"] = "inferred"


class CandidateEntity(StrictLivingContextModel):
    """A bounded entity mention for later server-owned source binding."""

    kind: Literal["place", "person", "organization", "item", "other"]
    value: str = Field(min_length=1, max_length=240)
    epistemic_status: Literal["reported", "inferred"] = "reported"
    source_quote: ContextQuote | None = None


class SituationProgress(StrictLivingContextModel):
    status: Literal[
        "unknown",
        "not_started",
        "in_progress",
        "blocked",
        "waiting",
        "completed",
    ] = "unknown"
    value: float | None = Field(default=None, ge=0.0, le=1.0)


class CandidateNeed(StrictLivingContextModel):
    blocked_judgment: str = Field(min_length=1, max_length=480)
    evidence_kind: Literal[
        "user",
        "calendar",
        "email",
        "message",
        "weather",
        "public_web",
        "agent",
        "time",
        "other",
    ]
    why_now: str = Field(min_length=1, max_length=480)
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)
    expires_at: str | None = Field(default=None, max_length=80)
    allowed_source_classes: list[str] = Field(
        min_length=1,
        max_length=3,
    )
    fallback_reaction: Literal["ask", "read", "wait", "silent"] = "wait"
    question: str = Field(default="", max_length=360)

    @model_validator(mode="before")
    @classmethod
    def normalize_source_aliases(cls, value: Any) -> Any:
        """Normalize exact evidence-kind and transport aliases only."""

        if not isinstance(value, dict) or "allowed_source_classes" not in value:
            return value
        payload = dict(value)
        raw = payload.get("allowed_source_classes")
        if isinstance(raw, list):
            aliases = _ENUM_ALIASES["allowed_source_class"]
            payload["allowed_source_classes"] = [
                _alias_value(item, aliases) for item in raw
            ]
        return payload

    @model_validator(mode="after")
    def validate_expiry(self) -> "CandidateNeed":
        if self.expires_at:
            if iso_time_or_none(self.expires_at) is None:
                raise ValueError("expires_at must be timezone-aware ISO-8601")
        return self


class CandidateNeedReference(StrictLivingContextModel):
    """A Need reference copied from the current server catalog snapshot."""

    need_token: str = Field(min_length=1, max_length=240)
    generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_token(self) -> "CandidateNeedReference":
        if not _TOKEN_RE.fullmatch(self.need_token):
            raise ValueError("need_token is not a valid server-issued token")
        return self


class LivingContextCandidate(StrictLivingContextModel):
    """Strict, non-authoritative model proposal for one semantic Situation turn."""

    schema_version: Literal[SCHEMA_VERSION]
    disposition: Literal["quiet", "create", "update", "correct", "resolve"]
    situation_token: str | None = Field(default=None, max_length=240)
    situation_revision: int | None = Field(default=None, ge=1)
    catalog_token: str | None = Field(default=None, max_length=240)
    # ``create_subject`` is a request to create a new server-owned Situation;
    # it is not an ID and is converted to one only by the evaluator.
    create_subject: str = Field(default="", max_length=240)
    category: Literal[
        "general",
        "personal",
        "work",
        "education",
        "health",
        "travel",
        "logistics",
        "finance",
        "other",
    ] = "general"
    label: str = Field(default="", max_length=240)
    title: str = Field(default="", max_length=240)
    summary: str = Field(default="", max_length=640)
    goal: str = Field(default="", max_length=480)
    deadline_at: str | None = Field(default=None, max_length=80)
    progress: SituationProgress = Field(default_factory=SituationProgress)
    entities: list[CandidateEntity] = Field(default_factory=list, max_length=8)
    lifecycle: Literal[
        "emerging",
        "active",
        "waiting",
        "resolved",
        "expired",
        "contradicted",
        "archived",
    ] = "active"
    known: list[CandidateKnown] = Field(default_factory=list, max_length=12)
    unknown: list[str] = Field(default_factory=list, max_length=12)
    assumptions: list[CandidateAssumption] = Field(default_factory=list, max_length=8)
    timeline: list[CandidateTimelineEntry] = Field(
        default_factory=list,
        max_length=TIMELINE_MAX_ITEMS,
    )
    material_change: str = Field(default="", max_length=480)
    next_observation_at: str | None = Field(default=None, max_length=80)
    next_step: str = Field(default="", max_length=480)
    next_step_epistemic_status: Literal["reported", "inferred"] = "inferred"
    needs: list[CandidateNeed] = Field(default_factory=list, max_length=8)
    answered_need_tokens: list[str] = Field(default_factory=list, max_length=8)
    answered_need_bindings: list[CandidateNeedReference] = Field(default_factory=list, max_length=8)
    requested_reaction: Literal["ask", "read", "wait", "silent"] = "wait"
    reopen: bool = False
    reopen_reason: str = Field(default="", max_length=240)
    source_quote: ContextQuote | None = None
    assertion_mode: Literal["direct_user", "inferred", "server_command"] = "inferred"
    source: Literal["model"] = "model"

    @model_validator(mode="after")
    def validate_disposition(self) -> "LivingContextCandidate":
        token = str(self.situation_token or "").strip()
        subject = self.create_subject.strip()
        if token and not _TOKEN_RE.fullmatch(token):
            raise ValueError("situation_token is not a valid server-issued token")
        for value in self.answered_need_tokens:
            if not _TOKEN_RE.fullmatch(value):
                raise ValueError("answered_need_tokens must contain server-issued tokens")
        if self.disposition == "create":
            if token:
                raise ValueError("create candidates cannot target an existing token")
            if self.situation_revision is not None or self.catalog_token:
                raise ValueError("create candidates cannot carry catalog bindings")
            if not subject:
                raise ValueError("create candidates require create_subject")
            if self.answered_need_tokens:
                raise ValueError("create candidates cannot answer existing InformationNeeds")
            if self.answered_need_bindings:
                raise ValueError("create candidates cannot answer existing InformationNeeds")
        elif self.disposition in {"update", "correct", "resolve"}:
            if not token:
                raise ValueError("existing Situation disposition requires situation_token")
            if self.situation_revision is None:
                raise ValueError("existing Situation disposition requires situation_revision")
            if not self.catalog_token:
                raise ValueError("existing Situation disposition requires catalog_token")
        elif self.disposition == "quiet":
            if set(self.model_fields_set) - _QUIET_CANDIDATE_FIELDS:
                raise ValueError("quiet candidates cannot carry a Situation mutation")
            if token or subject or self.known or self.timeline or self.needs or self.reopen or self.catalog_token:
                raise ValueError("quiet candidates cannot carry a Situation mutation")
        if self.reopen and self.disposition != "correct":
            raise ValueError("reopen requires the correct disposition")
        if self.reopen and not self.reopen_reason.strip():
            raise ValueError("reopen requires a bounded reason")
        binding_tokens = {item.need_token for item in self.answered_need_bindings}
        answer_tokens = set(self.answered_need_tokens)
        binding_pairs = {
            (item.need_token, item.generation)
            for item in self.answered_need_bindings
        }
        if (
            binding_tokens != answer_tokens
            or len(self.answered_need_tokens) != len(answer_tokens)
            or len(self.answered_need_bindings) != len(binding_pairs)
        ):
            raise ValueError("answered_need_bindings must exactly match answered_need_tokens")
        if self.disposition in {"correct", "resolve"} and self.assertion_mode == "inferred":
            raise ValueError("lifecycle mutation requires a direct user assertion or server command")
        if self.deadline_at and iso_time_or_none(self.deadline_at) is None:
            raise ValueError("deadline_at must be timezone-aware ISO-8601")
        if self.next_observation_at and iso_time_or_none(self.next_observation_at) is None:
            raise ValueError("next_observation_at must be timezone-aware ISO-8601")
        if self.lifecycle == "resolved" and self.disposition not in {"resolve", "correct"}:
            raise ValueError("resolved lifecycle requires resolve or correct disposition")
        return self

    @model_validator(mode="after")
    def validate_source_classes(self) -> "LivingContextCandidate":
        invalid = set(self._source_classes()) - set(INFORMATION_NEED_SOURCE_CLASSES)
        if invalid:
            raise ValueError(f"unsupported InformationNeed source class: {sorted(invalid)!r}")
        return self

    def _source_classes(self) -> list[str]:
        return [
            str(source)
            for need in self.needs
            for source in need.allowed_source_classes
        ]

    @property
    def creates_situation(self) -> bool:
        return self.disposition == "create"


def quarantine_typed_timeline_rows(
    candidate: LivingContextCandidate,
    *,
    source_text: str,
) -> tuple[LivingContextCandidate, int]:
    """Second-layer source binding quarantine after typed construction."""

    retained: list[CandidateTimelineEntry] = []
    dropped = 0
    for row in candidate.timeline:
        quote = row.source_quote
        if quote is not None and (
            not source_text
            or source_text[quote.start : quote.end] != quote.text
        ):
            dropped += 1
            continue
        retained.append(row)
    if dropped == 0:
        return candidate, 0
    return candidate.model_copy(update={"timeline": retained}), dropped


def quarantine_typed_known_rows(
    candidate: LivingContextCandidate,
    *,
    source_text: str,
) -> tuple[LivingContextCandidate, int]:
    """Second-layer source binding quarantine for typed Known rows."""

    retained: list[CandidateKnown] = []
    dropped = 0
    for row in candidate.known:
        quote = row.source_quote
        if quote is not None and (
            not source_text
            or source_text[quote.start : quote.end] != quote.text
        ):
            dropped += 1
            continue
        retained.append(row)
    if dropped == 0:
        return candidate, 0
    return candidate.model_copy(update={"known": retained}), dropped


class InformationNeedRecord(StrictLivingContextModel):
    """Durable need projection; it is not an instruction to call a source."""

    schema_version: Literal[INFORMATION_NEED_SCHEMA_VERSION]
    need_id: str = Field(min_length=1, max_length=240)
    situation_id: str = Field(min_length=1, max_length=240)
    owner_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    blocked_judgment: str = Field(min_length=1, max_length=480)
    evidence_kind: Literal[
        "user",
        "calendar",
        "email",
        "message",
        "weather",
        "public_web",
        "agent",
        "time",
        "other",
    ]
    why_now: str = Field(min_length=1, max_length=480)
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)
    expires_at: str | None = Field(default=None, max_length=80)
    allowed_source_classes: list[str] = Field(default_factory=list, max_length=6)
    fallback_reaction: Literal["ask", "read", "wait", "silent"] = "wait"
    question: str = Field(default="", max_length=360)
    status: Literal["open", "asked", "observing", "waiting", "resolved", "expired", "dismissed"] = "open"
    generation: int = Field(default=1, ge=1)
    source_event_id: str = Field(min_length=1, max_length=240)
    answered_by_event_id: str | None = Field(default=None, max_length=240)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)
    # Server-owned, exact binding to the semantic unknown that this Need was
    # admitted to resolve.  Older rows omit these fields and therefore remain
    # readable but cannot claim a successful unknown projection.
    unknown_binding: str | None = Field(default=None, max_length=480)
    unknown_binding_digest: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_times(self) -> "InformationNeedRecord":
        for name in ("expires_at", "created_at", "updated_at", "answered_by_event_id"):
            value = getattr(self, name, None)
            if name != "answered_by_event_id" and value:
                if iso_time_or_none(value) is None:
                    raise ValueError(f"{name} must be timezone-aware ISO-8601")
        if (self.unknown_binding is None) != (self.unknown_binding_digest is None):
            raise ValueError("unknown_binding and unknown_binding_digest must be paired")
        if self.unknown_binding_digest is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.unknown_binding_digest
        ):
            raise ValueError("unknown_binding_digest must be a lowercase SHA-256 digest")
        return self


class ReactionPlan(StrictLivingContextModel):
    """Server-derived in-app reaction; it has no delivery or execution authority."""

    situation_id: str = Field(min_length=1, max_length=240)
    need_id: str | None = Field(default=None, max_length=240)
    kind: Literal["ask", "read", "wait", "silent"]
    related_to: str = Field(min_length=1, max_length=480)
    material_change: str = Field(default="", max_length=480)
    why_now: str = Field(default="", max_length=480)
    rank: float = Field(default=0.0, ge=0.0, le=1.0)
    cooldown_until: str | None = Field(default=None, max_length=80)
    suppression_reason: str = Field(default="", max_length=240)


def situation_catalog_projection(
    situation: dict[str, Any],
    *,
    open_needs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the bounded, server-issued catalog sent to the understanding model."""

    semantic = situation.get("semantic") if isinstance(situation.get("semantic"), dict) else {}
    needs = []
    for item in (open_needs or [])[:8]:
        if not isinstance(item, dict):
            continue
        needs.append(
            {
                "need_token": str(item.get("need_id") or ""),
                "generation": int(item.get("generation") or 1),
                "catalog_token": _catalog_binding_token(
                    "need",
                    str(situation.get("situation_id") or ""),
                    str(item.get("need_id") or ""),
                    int(item.get("generation") or 1),
                ),
                "blocked_judgment": str(item.get("blocked_judgment") or "")[:240],
                "question": str(item.get("question") or "")[:240],
                "status": str(item.get("status") or "open"),
            }
        )
    situation_token = str(situation.get("situation_id") or "")
    observation_revision = int(situation.get("observation_revision") or 1)
    owner_id = str(situation.get("user_id") or "")
    session_id = str(situation.get("session_id") or "")
    return {
        "situation_token": situation_token,
        CATALOG_SELECTOR_FIELD: situation_catalog_selector(
            owner_id,
            session_id,
            situation_token,
        ),
        "observation_revision": observation_revision,
        "catalog_token": _catalog_binding_token(
            "situation", owner_id, session_id, situation_token, observation_revision
        ),
        "owner_id": owner_id,
        "session_id": session_id,
        "category": str(semantic.get("category") or "general"),
        "label": str(semantic.get("label") or semantic.get("title") or "")[:240],
        "title": str(semantic.get("title") or "")[:240],
        "summary": str(semantic.get("summary") or "")[:360],
        "goal": str(semantic.get("goal") or "")[:360],
        "lifecycle": str(semantic.get("lifecycle") or situation.get("status") or "active"),
        "deadline_at": semantic.get("deadline_at"),
        "progress": semantic.get("progress") if isinstance(semantic.get("progress"), dict) else {},
        "unknown": [str(value)[:240] for value in semantic.get("unknown", [])[:6] if str(value).strip()],
        "entities": [
            item
            for item in semantic.get("entities", [])[:8]
            if isinstance(item, dict)
        ],
        "open_needs": needs,
    }


def _catalog_binding_token(kind: str, *values: Any) -> str:
    payload = json.dumps(
        {"kind": kind, "values": [str(value) for value in values]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"cat_{hashlib.sha256(payload).hexdigest()[:32]}"


def stable_subject_digest(owner_id: str, subject: str) -> str:
    """Return a deterministic subject key without exposing raw user text as an ID."""

    payload = json.dumps(
        {"owner_id": str(owner_id), "subject": " ".join(str(subject).split())[:240]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_living_context_candidate_detailed(
    value: Any,
    *,
    source_text: str = "",
    current_time: Any = None,
    catalog: list[dict[str, Any]] | list[Any] | None = None,
) -> tuple[LivingContextCandidate | None, list[str], dict[str, Any]]:
    """Normalize and strictly validate a model candidate.

    The report is deliberately metadata-only.  It contains no model text or
    validation ``input`` values, so callers can persist it in diagnostics.
    """

    if value is None:
        return None, [], {
            "status": "absent",
            "repair_count": 0,
            "repaired_fields": [],
            "issue_codes": [],
        }
    if not isinstance(value, dict):
        issues = ["living_context_candidate:root:object_type"]
        return None, issues, {
            "status": "rejected",
            "repair_count": 0,
            "repaired_fields": [],
            "issue_codes": issues,
        }
    disposition = _alias_value(value.get("disposition"), _ENUM_ALIASES["disposition"])
    if disposition == "quiet":
        # A quiet result is a non-mutation marker, not a partially populated
        # Situation.  Check the raw transport keys before the normalizer adds
        # internal defaults such as ``progress`` and server-owned provenance.
        # This keeps explicit mutation fields fail-closed while preserving the
        # minimal {schema_version, disposition, source} quiet shape.
        unexpected = {
            str(key)
            for key in value
            if str(key) not in _QUIET_CANDIDATE_FIELDS
        }
        if unexpected:
            issues = ["living_context_candidate:quiet_mutation_conflict"]
            return None, issues, {
                "status": "rejected",
                "repair_count": 0,
                "repaired_fields": [],
                "issue_codes": issues,
            }
    value, dropped_known_count = quarantine_invalid_known_rows(
        value,
        source_text=source_text,
    )
    value, dropped_timeline_count = quarantine_invalid_timeline_rows(
        value,
        source_text=source_text,
    )
    normalized, repaired_fields = _normalize_candidate_payload(value, catalog=catalog)
    if dropped_known_count:
        repaired_fields.append("known:quarantined")
    if dropped_timeline_count:
        repaired_fields.append("timeline:quarantined")
    if "situation_selector:rejected" in repaired_fields:
        issues = ["living_context_candidate:binding:situation_selector_not_unique"]
        return None, issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": issues,
            "dropped_known_count": dropped_known_count,
            "dropped_timeline_count": dropped_timeline_count,
        }
    try:
        candidate = LivingContextCandidate.model_validate(normalized, strict=True)
    except Exception as exc:
        issues = _boundary_issue_codes(exc, prefix="living_context_candidate")
        return None, issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": issues,
            "dropped_known_count": dropped_known_count,
            "dropped_timeline_count": dropped_timeline_count,
        }
    candidate, post_parse_known_dropped = quarantine_typed_known_rows(
        candidate,
        source_text=source_text,
    )
    if post_parse_known_dropped:
        dropped_known_count += post_parse_known_dropped
        if "known:quarantined" not in repaired_fields:
            repaired_fields.append("known:quarantined")
    candidate, post_parse_timeline_dropped = quarantine_typed_timeline_rows(
        candidate,
        source_text=source_text,
    )
    if post_parse_timeline_dropped:
        dropped_timeline_count += post_parse_timeline_dropped
        if "timeline:quarantined" not in repaired_fields:
            repaired_fields.append("timeline:quarantined")
    quote_issues = _source_bound_candidate_quote_issues(candidate, source_text)
    if quote_issues:
        return None, quote_issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": quote_issues,
            "dropped_known_count": dropped_known_count,
            "dropped_timeline_count": dropped_timeline_count,
        }
    answered_need_issues = answered_need_contract_issues(
        candidate,
        source_text=source_text,
    )
    if answered_need_issues:
        return None, answered_need_issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": answered_need_issues,
            "dropped_known_count": dropped_known_count,
            "dropped_timeline_count": dropped_timeline_count,
        }
    if candidate.deadline_at is None and candidate.disposition in {"create", "update"}:
        resolved_deadline = resolve_reported_window_end(source_text, current_time)
        if resolved_deadline is not None:
            candidate = candidate.model_copy(update={"deadline_at": resolved_deadline})
            repaired_fields.append("deadline_at:from_reported_time_window")
    status = "repaired" if repaired_fields else "accepted"
    report: dict[str, Any] = {
        "status": status,
        "repair_count": len(repaired_fields),
        "repaired_fields": repaired_fields,
        "issue_codes": [],
        "dropped_known_count": dropped_known_count,
        "dropped_timeline_count": dropped_timeline_count,
    }
    if "evidence_kind:omitted_root_transport" in repaired_fields:
        report["normalized_fields"] = ["evidence_kind"]
        report["normalization_status"] = "normalized"
    return candidate, [], report


def parse_living_context_candidate(value: Any) -> tuple[LivingContextCandidate | None, list[str]]:
    """Validate a model candidate without allowing a permissive dict fallback."""

    candidate, issues, _report = parse_living_context_candidate_detailed(value)
    return candidate, issues


def parse_living_reaction_feedback_detailed(
    value: Any,
    *,
    source_text: str = "",
) -> tuple[LivingReactionFeedback | None, list[str], dict[str, Any]]:
    """Strictly parse optional model feedback with independent diagnostics."""

    if value is None:
        return None, [], {
            "status": "absent",
            "repair_count": 0,
            "repaired_fields": [],
            "issue_codes": [],
        }
    if not isinstance(value, dict):
        issues = ["living_reaction_feedback:root:object_type"]
        return None, issues, {
            "status": "rejected",
            "repair_count": 0,
            "repaired_fields": [],
            "issue_codes": issues,
        }
    normalized, repaired_fields = _normalize_feedback_payload(value)
    try:
        feedback = LivingReactionFeedback.model_validate(normalized, strict=True)
    except Exception as exc:
        issues = _boundary_issue_codes(exc, prefix="living_reaction_feedback")
        return None, issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": issues,
        }
    quote = feedback.source_quote
    if source_text and (
        quote.end > len(source_text)
        or source_text[quote.start : quote.end] != quote.text
    ):
        issues = ["living_reaction_feedback:source_quote:not_source_bound"]
        return None, issues, {
            "status": "rejected",
            "repair_count": len(repaired_fields),
            "repaired_fields": repaired_fields,
            "issue_codes": issues,
        }
    return feedback, [], {
        "status": "repaired" if repaired_fields else "accepted",
        "repair_count": len(repaired_fields),
        "repaired_fields": repaired_fields,
        "issue_codes": [],
    }


def _source_bound_candidate_quote_issues(
    candidate: LivingContextCandidate,
    source_text: str,
) -> list[str]:
    """Reject model quotes that do not point into the current user turn."""

    if not source_text:
        return []
    checks: list[tuple[str, ContextQuote | None]] = [("source_quote", candidate.source_quote)]
    checks.extend(
        (f"known.{index}.source_quote", item.source_quote)
        for index, item in enumerate(candidate.known)
    )
    checks.extend(
        (f"timeline.{index}.source_quote", item.source_quote)
        for index, item in enumerate(candidate.timeline)
    )
    checks.extend(
        (f"entities.{index}.source_quote", item.source_quote)
        for index, item in enumerate(candidate.entities)
    )
    for location, quote in checks:
        if quote is None:
            continue
        if quote.end > len(source_text) or source_text[quote.start : quote.end] != quote.text:
            return [f"living_context_candidate:{location}:not_source_bound"]
    return []


def answered_need_contract_issues(
    candidate: LivingContextCandidate,
    *,
    source_text: str,
) -> list[str]:
    """Require provenance for a candidate that answers an InformationNeed.

    Answer bindings are a semantic mutation of the Need lifecycle, not merely
    another model field.  They therefore require both an exact quote from the
    current user turn and an assertion mode that can make a direct assertion.
    The exact slice itself is checked by ``_source_bound_candidate_quote_issues``;
    this helper handles presence and the direct-assertion contract without
    matching text to a Need or guessing a token.

    ``server_command`` remains valid for the existing bounded command path,
    where the server owns the command event but the candidate still carries
    the same source-bound root quote when one is available.
    """

    if not candidate.answered_need_tokens and not candidate.answered_need_bindings:
        return []
    issues: list[str] = []
    if candidate.source_quote is None:
        issues.append("living_context_candidate:answered_need:source_quote:required")
    elif not source_text:
        issues.append("living_context_candidate:answered_need:source_quote:source_text_missing")
    if candidate.assertion_mode not in {"direct_user", "server_command"}:
        issues.append("living_context_candidate:answered_need:assertion_mode:direct_required")
    return issues


def parse_living_reaction_feedback(value: Any) -> tuple[LivingReactionFeedback | None, list[str]]:
    """Strictly parse optional model feedback without a permissive fallback."""

    feedback, issues, _report = parse_living_reaction_feedback_detailed(value)
    return feedback, issues


def iso_time_or_none(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone().isoformat()
    except ValueError:
        return None
