"""Bounded model seam for recovering a missing Living Context candidate.

The main understanding response contains several independent, optional
artifacts.  Asking the same response to carry a full understanding document
and a durable Situation proposal is occasionally lossy, especially on a
short continuation turn.  This module provides one deliberately smaller
model call for the candidate only.  It never creates tokens, changes
authority, or performs admission; the normal strict contract and server CAS
remain the boundary after this call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from interface.living_context_contract import (
    CATALOG_SELECTOR_FIELD,
    INFORMATION_NEED_SOURCE_CLASSES,
    LivingContextCandidate,
    SCHEMA_VERSION,
    TIMELINE_ITEM_FIELDS,
    TIMELINE_MAX_ITEMS,
    normalize_information_need_source_class,
    parse_living_context_candidate_detailed,
    quarantine_invalid_known_rows,
    quarantine_invalid_timeline_rows,
    situation_catalog_selector,
)


CANDIDATE_EXTRACTOR_SYSTEM = (
    "You are Veyra's bounded Living Context candidate extractor. Return one "
    "strict JSON object only. The top-level response MUST be an envelope "
    "with exactly the candidate under the living_context_candidate key. "
    "Extract a single non-authoritative proposal "
    "about a real-world Situation from the user message. This is a model "
    "candidate, not a route, tool call, permission, fact, or execution plan. "
    "If the message does not introduce or change a Situation, return a quiet "
    "candidate. Keep the response compact: one candidate, at most one known "
    "item, and no more than three distinct InformationNeeds. Use only the exact situation_selector, "
    "situation_token, situation_revision, catalog_token, need_token, and generation values "
    "supplied in the current server catalog. Never invent, transform, or "
    "combine these values. A create candidate must omit existing Situation "
    "bindings and use a concise create_subject. An update/correct/resolve "
    "candidate must copy the exact situation_selector and all three binding values from one catalog row. "
    "Use the exact schema_version veyra.living_context_candidate.v1. "
    "Use only these exact enums: disposition=quiet|create|update|correct|resolve; "
    "category=general|personal|work|education|health|travel|logistics|finance|other; "
    "lifecycle=emerging|active|waiting|resolved|expired|contradicted|archived; "
    "progress.status=unknown|not_started|in_progress|blocked|waiting|completed; "
    "evidence_kind=user|calendar|email|message|weather|public_web|agent|time|other; "
    "allowed_source_classes is a list containing only "
    "user|calendar|email|message|weather|public_web|agent|time|other; "
    "fallback_reaction and requested_reaction=ask|read|wait|silent; "
    "epistemic_status=reported|inferred. User statements are reported, "
    "inferences are inferred. known is always a list of objects with only "
    "statement, epistemic_status, and optional source_quote; never put "
    "Situation or catalog bindings in known. unknown is always a list of "
    "strings. timeline is always a JSON list of at most twelve objects with "
    "statement, occurred_at, source_quote, and material; use an empty list "
    "when the user turn provides no timeline item. Every "
    "InformationNeed must contain blocked_judgment, evidence_kind, why_now, "
    "a JSON-number urgency from 0 to 1, one to three relevant "
    "allowed_source_classes, "
    "fallback_reaction, and a string question. If those fields cannot all be "
    "filled from the message and current catalog, emit needs=[] instead of a "
    "partial Need. "
    "evidence_kind and allowed_source_classes belong only inside a Need; "
    "never emit them at candidate root. A root evidence_kind is transport "
    "noise and is discarded without being moved or interpreted. Every source_quote is an object "
    "with exact string text and integer start/end, never a string. Do not "
    "emit fields outside the candidate contract. At candidate root, use only "
    "the fields named in candidate_root_fields. Fields named in need_fields "
    "may appear only inside items of needs. "
    "When the user states a relative or absolute time window, deadline_at is "
    "required and is the timezone-aware end of that bounded window, computed "
    "only from current_time. For example, next week ends at the end of the next "
    "local calendar week, and the end of this month is the last local instant "
    "of the current calendar month. Keep the exact event date in unknown when "
    "the user did not state it. When the user states no time expression, "
    "deadline_at may be null. Do not invent an exact event date. Any "
    "source_quote must be an exact Python-character slice "
    "of user_message. source_quote is optional for create/update; omit it "
    "instead of approximating text or indices. When emitting a Need, do not "
    "rebuild an active open_need whose blocked_judgment is exactly the same, even "
    "when evidence_kind changes; retain one exact endpoint. Answering one sub-Need "
    "remains an update/nonterminal Situation change; resolve only when the user "
    "explicitly declares the entire Situation or goal finished. "
    "Do not emit feedback, semantic frames, tokens not supplied by the "
    "catalog, URLs, paths, commands, tools, routes, risk, authority, or "
    "capability fields. Return no prose and do not put candidate fields at "
    "the response root."
)


CANDIDATE_EXTRACTOR_REPAIR_SYSTEM = (
    "You are Veyra's bounded Living Context candidate boundary repairer. "
    "Return strict JSON only, with exactly one top-level envelope and no more "
    "than three distinct InformationNeeds: "
    "{living_context_candidate:{...}}. Rebuild a complete replacement "
    "candidate from the original user_message and the supplied validation "
    "issue codes. Do not return a patch, explanation, feedback, semantic "
    "frame, or any transport metadata. The candidate is non-authoritative "
    "and never a route, tool call, permission, fact, or execution plan. "
    "Use schema_version=veyra.living_context_candidate.v1 and only these "
    "exact enums: disposition=quiet|create|update|correct|resolve; "
    "category=general|personal|work|education|health|travel|logistics|finance|other; "
    "lifecycle=emerging|active|waiting|resolved|expired|contradicted|archived; "
    "progress.status=unknown|not_started|in_progress|blocked|waiting|completed; "
    "evidence_kind=user|calendar|email|message|weather|public_web|agent|time|other; "
    "allowed_source_classes is a list containing only "
    "user|calendar|email|message|weather|public_web|agent|time|other; "
    "fallback_reaction and requested_reaction=ask|read|wait|silent; "
    "epistemic_status=reported|inferred. known is always a list of objects "
    "{statement,epistemic_status} with optional source_quote; never put "
    "Situation or catalog bindings in known. unknown is always a list of strings. "
    "timeline is always a JSON list of at most twelve objects. Each timeline "
    "object may contain only statement (non-empty string), occurred_at "
    "(timezone-aware ISO string or null), source_quote (an exact user-message "
    "quote object or null), and material (boolean). If validation_issues or "
    "repair_hints identify a timeline shape defect, rebuild it as a bounded "
    "list from the original user_message; never repeat a scalar, mapping, "
    "oversized list, or invalid row. Use [] when no valid timeline item is "
    "grounded in the user message. "
    "Every InformationNeed must contain blocked_judgment, evidence_kind, "
    "why_now, a JSON-number urgency from 0 to 1, one to three relevant "
    "allowed_source_classes, "
    "fallback_reaction, and a string question; otherwise emit needs=[]. A "
    "create candidate has a concise non-empty goal grounded in the user "
    "message and has no "
    "situation_selector, situation_token, situation_revision, catalog_token, answered_need_tokens, "
    "or answered_need_bindings. An update/correct/resolve candidate copies "
    "the exact situation_selector, situation_token, situation_revision, and catalog_token from "
    "one current catalog row, and need references copy exact need_token and "
    "generation pairs from that same row. evidence_kind and allowed_source_classes "
    "appear only inside a Need; a root evidence_kind is transport noise and "
    "must not be moved or interpreted. Optional source_quote is an object "
    "{text:string,start:integer,end:integer}, never a string. For create or "
    "update, omit source_quote instead of approximating it. Do not emit "
    "authority, route, risk, state_effect, capability, tool, tool_args, "
    "URL, path, query, command, credential, or recipient keys. Do not invent "
    "dates or server-issued values. If user_message contains a time window, "
    "deadline_at must be the timezone-aware end of that window derived only "
    "from current_time; otherwise deadline_at may be null. At candidate root, use only the fields "
    "named in candidate_root_fields; fields named in need_fields belong only "
    "inside items of needs. Return no prose."
)


_FORBIDDEN_KEYS = frozenset(
    {
        "authority",
        "route",
        "risk",
        "state_effect",
        "capability",
        "capability_grant",
        "allowed_capabilities",
        "tool",
        "tool_args",
        "url",
        "path",
        "query",
        "command",
        "credential",
        "credentials",
        "recipient",
    }
)
_TRANSPORT_KEYS = frozenset({"status", "_model", "duration_ms", "model_status"})
_ROOT_CANDIDATE_KEYS = frozenset(
    {
        "schema_version",
        "disposition",
        CATALOG_SELECTOR_FIELD,
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
        "entities",
        "lifecycle",
        "known",
        "unknown",
        "assumptions",
        "timeline",
        "material_change",
        "next_observation_at",
        "next_step",
        "next_step_epistemic_status",
        "needs",
        "answered_need_tokens",
        "answered_need_bindings",
        "requested_reaction",
        "reopen",
        "reopen_reason",
        "source_quote",
        "assertion_mode",
        "source",
    }
)
_NEED_KEYS = frozenset(
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
)
_CANDIDATE_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        "disposition",
        CATALOG_SELECTOR_FIELD,
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
        "entities",
        "lifecycle",
        "known",
        "unknown",
        "assumptions",
        "timeline",
        "material_change",
        "next_observation_at",
        "next_step",
        "next_step_epistemic_status",
        "needs",
        "answered_need_tokens",
        "answered_need_bindings",
        "requested_reaction",
        "reopen",
        "reopen_reason",
        "source_quote",
        "assertion_mode",
        "source",
        "text",
        "start",
        "end",
        "statement",
        "epistemic_status",
        "kind",
        "value",
        "occurred_at",
        "material",
        "blocked_judgment",
        "evidence_kind",
        "why_now",
        "urgency",
        "expires_at",
        "allowed_source_classes",
        "fallback_reaction",
        "question",
        "need_token",
        "generation",
    }
)

# Model-facing schema hints, not semantic interpretation. These describe the
# strict contract that the candidate-only repair call must satisfy when a
# collection arrives with an invalid transport shape. Values are finite and
# field-only so malformed provider values never get echoed into a follow-up
# prompt.
_REPAIR_SCHEMA_HINTS: dict[str, dict[str, Any]] = {
    "timeline": {
        "field": "timeline",
        "container": "json_list",
        "max_items": TIMELINE_MAX_ITEMS,
        "item_fields": list(TIMELINE_ITEM_FIELDS),
        "statement": "non_empty_string",
        "occurred_at": "timezone_aware_iso_or_null",
        "source_quote": "exact_user_message_quote_object_or_null",
        "material": "boolean",
    },
}
_NONQUIET_DISPOSITION_VALUES = frozenset(
    {
        "create",
        "update",
        "correct",
        "resolve",
        "创建",
        "新建",
        "更新",
        "修改",
        "纠正",
        "更正",
        "解决",
        "已解决",
    }
)


@dataclass(slots=True, frozen=True)
class CandidateExtractionResult:
    """Strict result of the bounded candidate extraction seam."""

    candidate: LivingContextCandidate | None
    issues: list[str]
    metrics: dict[str, Any]


def extract_living_context_candidate(
    client: Any,
    *,
    text: str,
    current_time: Any = None,
    catalog: list[Mapping[str, Any]] | None = None,
    primary_understanding: Mapping[str, Any] | None = None,
) -> CandidateExtractionResult:
    """Make one bounded candidate-only call plus at most one strict repair.

    ``catalog`` is projected to the exact binding fields required by the
    candidate contract.  The returned object is still only a typed proposal;
    callers must pass it through the existing orchestrator admission/CAS.
    """

    request = {
        "user_message": str(text or "")[:4000],
        "current_time": str(current_time or "")[:80],
        "living_context_situation_candidates": _catalog_projection(catalog),
        "primary_understanding": _understanding_projection(primary_understanding),
        "required_json_shape": {
            "living_context_candidate": "one strict candidate object",
            "candidate_root_fields": sorted(_ROOT_CANDIDATE_KEYS),
            "need_fields": sorted(_NEED_KEYS),
            "known_contract": {
                "container": "list",
                "item_fields": ["statement", "epistemic_status", "source_quote"],
                "epistemic_status": "reported|inferred",
            },
            "unknown_contract": "list of strings",
            "temporal_contract": {
                "current_time": str(current_time or "")[:80],
                "with_time_expression": "deadline_at is required and marks the timezone-aware end of the reported window",
                "without_time_expression": "deadline_at may be null",
                "uncertain_exact_date": "retain as an unknown; do not invent an exact event date",
            },
            "need_contract": {
                "blocked_judgment": "non-empty string",
                "evidence_kind": "one exact evidence_kind enum",
                "why_now": "non-empty string",
                "urgency": "JSON number 0..1",
                "expires_at": "timezone-aware string or null",
                "allowed_source_classes": "list of 1..3 relevant exact source enums",
                "fallback_reaction": "ask|read|wait|silent",
                "question": "string",
            },
            "source_quote_policy": "optional for create/update; omit unless it is an exact Python-character slice of user_message",
            "create_requires": [
                "schema_version",
                "disposition=create",
                "create_subject",
                "summary",
                "known",
                "unknown",
                "assumptions",
                "needs",
                "requested_reaction",
                "source=model",
            ],
            "existing_requires": [
                "schema_version",
                "disposition=update|correct|resolve",
                "situation_token",
                "situation_revision",
                "catalog_token",
                "source=model",
            ],
            "quiet_shape": {
                "schema_version": "veyra.living_context_candidate.v1",
                "disposition": "quiet",
                "source": "model",
            },
        },
    }
    try:
        response = client.complete_json(
            purpose="living_context_candidate_extraction",
            system=CANDIDATE_EXTRACTOR_SYSTEM,
            user=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception:
        # The model seam is optional.  Do not allow a provider exception to
        # change the normal read-only understanding path.
        return _rejected("transport_failure")

    if not isinstance(response, Mapping):
        return _rejected("response_object_type")
    if str(response.get("status") or "") != "model_assisted":
        return _rejected("model_transport_status")

    if _contains_forbidden_key(response, ignore_timeline_rows=True):
        return _rejected("forbidden_field")

    raw_candidate = _candidate_from_response(response)
    raw_candidate, source_normalization = _normalize_candidate_source_classes(raw_candidate)
    raw_candidate, timeline_normalization = _normalize_candidate_timeline(
        raw_candidate,
        source_text=str(text or ""),
    )
    raw_candidate, known_normalization = _normalize_candidate_known(
        raw_candidate,
        source_text=str(text or ""),
    )
    candidate, issues, report = _parse_candidate(
        raw_candidate,
        source_text=str(text or ""),
        current_time=current_time,
        catalog=catalog,
        source_normalization=source_normalization,
        timeline_normalization=timeline_normalization,
        known_normalization=known_normalization,
    )
    if candidate is not None and not issues:
        return CandidateExtractionResult(
            candidate,
            [],
            _report_metrics(report, response_status="model_assisted"),
        )

    initial_issues = list(issues) if issues else [
        "living_context_candidate:extractor:candidate_missing"
    ]
    repair_catalog = _repair_catalog_projection(raw_candidate, initial_issues, catalog)
    previous_candidate = _repair_previous_candidate(
        raw_candidate,
        initial_issues=initial_issues,
        catalog=catalog,
    )
    # Keep the repair call materially smaller than the initial extraction
    # request.  It only needs the original bounded user turn, current time,
    # the server-projected catalog, safe issue codes, schema hints, and the
    # projected failed candidate.  In particular, do not echo the full
    # required-shape or primary-understanding prompt back into the repair.
    repair_request = {
        "user_message": request["user_message"],
        "current_time": request["current_time"],
        "living_context_situation_candidates": repair_catalog,
        "validation_issues": _safe_repair_issue_codes(initial_issues),
        "repair_hints": _repair_schema_hints(initial_issues),
        "previous_candidate": previous_candidate,
    }
    try:
        repaired_response = client.complete_json(
            purpose="living_context_candidate_extraction_repair",
            system=CANDIDATE_EXTRACTOR_REPAIR_SYSTEM,
            user=json.dumps(repair_request, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception:
        return _rejected(
            "repair_transport_failure",
            issues=initial_issues,
            repair_attempted=True,
        )
    if not isinstance(repaired_response, Mapping):
        return _rejected(
            "repair_response_object_type",
            issues=initial_issues,
            repair_attempted=True,
        )
    if str(repaired_response.get("status") or "") != "model_assisted":
        return _rejected(
            "repair_model_transport_status",
            issues=initial_issues,
            repair_attempted=True,
        )
    if _contains_forbidden_key(repaired_response, ignore_timeline_rows=True):
        return _rejected(
            "forbidden_field",
            repair_attempted=True,
        )

    repaired_raw = _candidate_from_response(repaired_response)
    repaired_raw, repaired_source_normalization = _normalize_candidate_source_classes(
        repaired_raw
    )
    repaired_raw, repaired_timeline_normalization = _normalize_candidate_timeline(
        repaired_raw,
        source_text=str(text or ""),
    )
    repaired_raw, repaired_known_normalization = _normalize_candidate_known(
        repaired_raw,
        source_text=str(text or ""),
    )
    repaired, repaired_issues, repaired_report = _parse_candidate(
        repaired_raw,
        source_text=str(text or ""),
        current_time=current_time,
        catalog=catalog,
        source_normalization=repaired_source_normalization,
        timeline_normalization=repaired_timeline_normalization,
        known_normalization=repaired_known_normalization,
    )
    if repaired is None or repaired_issues:
        return _rejected(
            "repair_rejected",
            issues=(list(repaired_issues) if repaired_issues else initial_issues),
            repair_attempted=True,
        )
    metrics = _report_metrics(
        repaired_report,
        response_status="model_assisted",
        repair_attempted=True,
    )
    metrics["status"] = "repaired"
    metrics["repair_count"] = max(1, int(metrics.get("repair_count") or 0))
    return CandidateExtractionResult(repaired, [], metrics)


def _safe_repair_issue_codes(issues: list[str]) -> list[str]:
    """Project validation failures to bounded, non-echoing repair codes.

    Pydantic locations are normally field names, but extra-key locations can
    contain provider-controlled strings.  Repair does not need those values;
    it needs only the contract area that must be rebuilt.  Keep a small set of
    stable binding/root codes and collapse collection paths to their field.
    """

    safe: list[str] = []
    for issue in issues[:16]:
        text = str(issue)
        if text.startswith("living_context_candidate:timeline"):
            code = "living_context_candidate:timeline:invalid_shape"
        elif text.startswith("living_context_candidate:needs"):
            code = "living_context_candidate:needs:invalid_shape"
        elif text.startswith("living_context_candidate:known"):
            code = "living_context_candidate:known:invalid_shape"
        elif text.startswith("living_context_candidate:assumptions"):
            code = "living_context_candidate:assumptions:invalid_shape"
        elif text.startswith("living_context_candidate:entities"):
            code = "living_context_candidate:entities:invalid_shape"
        elif text.startswith("living_context_candidate:progress"):
            code = "living_context_candidate:progress:invalid_shape"
        elif text.startswith("living_context_candidate:binding:"):
            suffix = text.rsplit(":", 1)[-1]
            if suffix not in {"not_in_catalog", "mismatch", "invalid"}:
                suffix = "invalid"
            code = f"living_context_candidate:binding:{suffix}"
        elif text.startswith("living_context_candidate:need_binding:"):
            suffix = text.rsplit(":", 1)[-1]
            if suffix not in {"not_in_catalog", "mismatch", "invalid"}:
                suffix = "invalid"
            code = f"living_context_candidate:need_binding:{suffix}"
        elif text in {
            "living_context_candidate:root:unsupported_source_class",
            "living_context_candidate:root:object_type",
            "living_context_candidate:quiet_mutation_conflict",
        }:
            code = text
        else:
            code = "living_context_candidate:boundary:invalid"
        if code not in safe:
            safe.append(code)
    return safe[:8]


def _repair_schema_hints(issues: list[str]) -> list[dict[str, Any]]:
    """Return only contract-derived hints for the failed collection fields."""

    if any(str(issue).startswith("living_context_candidate:timeline") for issue in issues):
        return [dict(_REPAIR_SCHEMA_HINTS["timeline"])]
    return []


def _normalize_candidate_source_classes(
    value: Any,
) -> tuple[Any, dict[str, Any]]:
    """Narrow Need source classes to stable registered canonical values.

    Exact aliases are canonicalized first, unknown strings are dropped only
    when at least one registered class remains, and order-preserving
    duplicates are removed. Any all-unknown, wrong-type, or over-limit list
    records a bounded error so the candidate remains rejected; no default
    source class is introduced.
    """

    empty: dict[str, Any] = {
        "dropped_unsupported_count": 0,
        "normalized_fields": [],
        "source_errors": [],
    }
    if not isinstance(value, Mapping):
        return value, empty
    needs = value.get("needs")
    if not isinstance(needs, list):
        return value, empty
    payload = dict(value)
    normalized_needs = [dict(row) if isinstance(row, Mapping) else row for row in needs]
    payload["needs"] = normalized_needs
    registered = set(INFORMATION_NEED_SOURCE_CLASSES)
    dropped = 0
    normalized_fields: list[str] = []
    source_errors: list[str] = []
    for index, row in enumerate(normalized_needs):
        if not isinstance(row, dict) or "allowed_source_classes" not in row:
            continue
        raw_sources = row.get("allowed_source_classes")
        if not isinstance(raw_sources, list):
            # Do not echo a malformed scalar into a repair request. Clear it
            # and retain an explicit type error; this is rejection, never a
            # coercion into a guessed source list.
            row["allowed_source_classes"] = []
            normalized_fields.append(f"needs[{index}].allowed_source_classes")
            source_errors.append(f"needs[{index}].allowed_source_classes:type")
            continue
        if len(raw_sources) > 3:
            source_errors.append(f"needs[{index}].allowed_source_classes:too_many")
        canonical: list[str] = []
        type_error = False
        for source in raw_sources:
            if not isinstance(source, str):
                type_error = True
                continue
            normalized = normalize_information_need_source_class(source)
            if normalized in registered:
                if normalized not in canonical:
                    canonical.append(normalized)
            else:
                dropped += 1
        if len(canonical) > 3:
            canonical = canonical[:3]
        if canonical != raw_sources:
            row["allowed_source_classes"] = canonical
            normalized_fields.append(f"needs[{index}].allowed_source_classes")
        if type_error:
            source_errors.append(f"needs[{index}].allowed_source_classes:type")
        if raw_sources and not canonical and not type_error:
            source_errors.append(
                f"needs[{index}].allowed_source_classes:unsupported_all"
            )
    empty.update(
        {
            "dropped_unsupported_count": dropped,
            "normalized_fields": normalized_fields[:16],
            "source_errors": source_errors[:16],
        }
    )
    return payload, empty


def _mark_source_normalized(
    report: dict[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Merge bounded source-class normalization metrics into a report."""

    dropped = int(metadata.get("dropped_unsupported_count") or 0)
    normalized_fields = [
        str(field)
        for field in metadata.get("normalized_fields", [])[:16]
        if isinstance(field, str)
    ]
    existing_fields = [
        str(field)
        for field in report.get("normalized_fields", [])[:32]
        if isinstance(field, str)
    ]
    report["normalized_fields"] = list(
        dict.fromkeys([*existing_fields, *normalized_fields])
    )[:32]
    report["dropped_unsupported_count"] = dropped
    if normalized_fields or dropped:
        report["normalized"] = True
        report["normalization_status"] = "normalized"


def _source_normalization_issue(metadata: Mapping[str, Any]) -> str | None:
    errors = [
        str(error)
        for error in metadata.get("source_errors", [])[:16]
        if isinstance(error, str)
    ]
    if not errors:
        return None
    if any(error.endswith(":unsupported_all") for error in errors):
        return "living_context_candidate:root:unsupported_source_class"
    first = errors[0]
    if first.endswith(":too_many"):
        return "living_context_candidate:needs:allowed_source_classes_too_many"
    if first.endswith(":type"):
        return "living_context_candidate:needs:allowed_source_classes_type"
    return "living_context_candidate:needs:allowed_source_classes_invalid"


def _normalize_candidate_timeline(
    value: Any,
    *,
    source_text: str,
) -> tuple[Any, dict[str, Any]]:
    """Adapt the shared timeline quarantine result to extractor metrics."""

    normalized, dropped = quarantine_invalid_timeline_rows(
        value,
        source_text=source_text,
    )
    if dropped:
        return normalized, {
            "dropped_timeline_count": dropped,
            "normalized_fields": ["timeline"],
            "timeline_errors": [],
        }
    timeline = value.get("timeline") if isinstance(value, Mapping) else None
    if isinstance(timeline, list) and len(timeline) > TIMELINE_MAX_ITEMS:
        return normalized, {
            "dropped_timeline_count": 0,
            "normalized_fields": [],
            "timeline_errors": ["timeline:too_many"],
        }
    return normalized, {
        "dropped_timeline_count": 0,
        "normalized_fields": [],
        "timeline_errors": [],
    }


def _normalize_candidate_known(
    value: Any,
    *,
    source_text: str,
) -> tuple[Any, dict[str, Any]]:
    """Adapt shared Known-row quarantine to extractor metrics."""

    normalized, dropped = quarantine_invalid_known_rows(
        value,
        source_text=source_text,
    )
    return normalized, {
        "dropped_known_count": dropped,
        "normalized_fields": ["known"] if dropped else [],
    }


def _mark_timeline_normalized(
    report: dict[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Merge bounded timeline quarantine metrics into a report."""

    dropped = int(metadata.get("dropped_timeline_count") or 0)
    normalized_fields = [
        str(field)
        for field in metadata.get("normalized_fields", [])[:8]
        if isinstance(field, str)
    ]
    existing_fields = [
        str(field)
        for field in report.get("normalized_fields", [])[:32]
        if isinstance(field, str)
    ]
    report["normalized_fields"] = list(
        dict.fromkeys([*existing_fields, *normalized_fields])
    )[:32]
    report["dropped_timeline_count"] = dropped
    if dropped:
        report["normalized"] = True
        report["normalization_status"] = "normalized"


def _mark_known_normalized(
    report: dict[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    dropped = int(metadata.get("dropped_known_count") or 0)
    normalized_fields = [
        str(field)
        for field in metadata.get("normalized_fields", [])[:8]
        if isinstance(field, str)
    ]
    existing_fields = [
        str(field)
        for field in report.get("normalized_fields", [])[:32]
        if isinstance(field, str)
    ]
    report["normalized_fields"] = list(
        dict.fromkeys([*existing_fields, *normalized_fields])
    )[:32]
    existing_dropped = int(report.get("dropped_known_count") or 0)
    report["dropped_known_count"] = max(existing_dropped, dropped)
    if dropped:
        repaired_fields = list(report.get("repaired_fields") or [])
        if "known:quarantined" not in repaired_fields:
            repaired_fields.append("known:quarantined")
        report["repaired_fields"] = repaired_fields[:32]
        report["normalized"] = True
        report["normalization_status"] = "normalized"


def _timeline_normalization_issue(metadata: Mapping[str, Any]) -> str | None:
    if any(
        isinstance(error, str) and error == "timeline:too_many"
        for error in metadata.get("timeline_errors", [])[:4]
    ):
        return "living_context_candidate:timeline:too_long"
    return None


def _parse_candidate(
    value: Any,
    *,
    source_text: str,
    current_time: Any,
    catalog: list[Mapping[str, Any]] | None,
    source_normalization: Mapping[str, Any] | None = None,
    timeline_normalization: Mapping[str, Any] | None = None,
    known_normalization: Mapping[str, Any] | None = None,
) -> tuple[LivingContextCandidate | None, list[str], dict[str, Any]]:
    if value is None:
        issue = "living_context_candidate:extractor:candidate_missing"
        return None, [issue], {
            "status": "rejected",
            "repair_count": 0,
            "repaired_fields": [],
            "issue_codes": [issue],
        }
    value, schema_normalized = _normalize_missing_candidate_schema(value)
    if source_normalization is None:
        value, source_normalization = _normalize_candidate_source_classes(value)
    else:
        source_normalization = dict(source_normalization)
    if timeline_normalization is None:
        value, timeline_normalization = _normalize_candidate_timeline(
            value,
            source_text=source_text,
        )
    else:
        timeline_normalization = dict(timeline_normalization)
    if known_normalization is None:
        value, known_normalization = _normalize_candidate_known(
            value,
            source_text=source_text,
        )
    else:
        known_normalization = dict(known_normalization)
    candidate, issues, report = parse_living_context_candidate_detailed(
        value,
        source_text=source_text,
        current_time=current_time,
        catalog=catalog,
    )
    if schema_normalized:
        _mark_schema_normalized(report)
    _mark_source_normalized(report, source_normalization)
    _mark_timeline_normalized(report, timeline_normalization)
    _mark_known_normalized(report, known_normalization)
    source_issue = _source_normalization_issue(source_normalization)
    timeline_issue = _timeline_normalization_issue(timeline_normalization)
    if timeline_issue:
        other_issues = [
            issue
            for issue in issues
            if not str(issue).startswith("living_context_candidate:timeline")
        ]
        normalized_issues = list(dict.fromkeys([*other_issues, timeline_issue]))[:16]
        report["status"] = "rejected"
        report["issue_codes"] = normalized_issues
        return None, normalized_issues, dict(report)
    if source_issue:
        other_issues = [
            issue
            for issue in issues
            if "allowed_source_classes" not in str(issue)
        ]
        normalized_issues = list(dict.fromkeys([*other_issues, source_issue]))[:16]
        report["status"] = "rejected"
        report["issue_codes"] = normalized_issues
        return None, normalized_issues, dict(report)
    if candidate is None or issues:
        return candidate, list(issues), dict(report)
    extractor_issues: list[str] = []
    if candidate.disposition == "create" and not candidate.goal.strip():
        extractor_issues.append("living_context_candidate:extractor:goal_missing")
    if len(candidate.needs) > 3:
        extractor_issues.append("living_context_candidate:extractor:needs_too_many")
    if extractor_issues:
        rejected_report = dict(report)
        rejected_report.update(
            {
                "status": "rejected",
                "issue_codes": extractor_issues,
            }
        )
        return None, extractor_issues, rejected_report
    binding_issues = validate_candidate_catalog_binding(candidate, catalog)
    if binding_issues:
        rejected_report = dict(report)
        rejected_report.update(
            {
                "status": "rejected",
                "issue_codes": binding_issues,
            }
        )
        return None, binding_issues, rejected_report
    return candidate, [], dict(report)


def _normalize_missing_candidate_schema(value: Any) -> tuple[Any, bool]:
    """Add only the fixed candidate schema literal when a candidate is present.

    This is intentionally narrower than the general contract normalizer.  A
    non-quiet textual disposition is enough to identify an existing candidate
    object, but no semantic, binding, provenance, Need, or timeline
    field is synthesized here.  An explicit wrong version remains untouched
    and is rejected by the strict contract.
    """

    if not isinstance(value, Mapping) or "schema_version" in value:
        return value, False
    disposition = value.get("disposition")
    if not isinstance(disposition, str) or not disposition.strip():
        return value, False
    if disposition.strip() not in _NONQUIET_DISPOSITION_VALUES:
        return value, False
    normalized = dict(value)
    normalized["schema_version"] = SCHEMA_VERSION
    return normalized, True


def _mark_schema_normalized(report: dict[str, Any]) -> None:
    """Expose a bounded metric for the fixed metadata normalization."""

    repaired_fields = list(report.get("repaired_fields") or [])
    if "schema_version:normalized" not in repaired_fields:
        repaired_fields.append("schema_version:normalized")
    report["repaired_fields"] = repaired_fields[:32]
    normalized_fields = [
        str(field)
        for field in report.get("normalized_fields", [])[:32]
        if isinstance(field, str)
    ]
    report["normalized_fields"] = list(
        dict.fromkeys([*normalized_fields, "schema_version"])
    )[:32]
    report["schema_version_normalized"] = True
    report["normalized"] = True
    report["normalization_status"] = "normalized"
    report["repair_count"] = int(report.get("repair_count") or 0) + 1
    if report.get("status") == "accepted":
        report["status"] = "repaired"


def _report_metrics(
    report: Mapping[str, Any],
    *,
    response_status: str,
    repair_attempted: bool = False,
) -> dict[str, Any]:
    metrics = dict(report)
    metrics.update(
        {
            "extractor": "living_context_candidate_v1",
            "extractor_attempted": True,
            "response_status": response_status,
            "repair_attempted": bool(repair_attempted),
        }
    )
    return metrics


def _contains_forbidden_key(
    value: Any,
    *,
    ignore_timeline_rows: bool = False,
) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in _FORBIDDEN_KEYS:
                return True
            if ignore_timeline_rows and str(key) == "timeline":
                continue
            if _contains_forbidden_key(
                nested,
                ignore_timeline_rows=ignore_timeline_rows,
            ):
                return True
        return False
    if isinstance(value, list):
        return any(
            _contains_forbidden_key(
                item,
                ignore_timeline_rows=ignore_timeline_rows,
            )
            for item in value[:32]
        )
    return False


def _candidate_projection(value: Any) -> dict[str, Any]:
    """Project a failed candidate without transport or unknown fields."""

    if not isinstance(value, Mapping):
        return {}
    # Do not teach the repair call to repeat a legitimate nested field that
    # the initial model placed at candidate root. Nested rows still use the
    # broader structural allowlist inside ``_project_mapping``.
    return _project_mapping(value, allowed=_ROOT_CANDIDATE_KEYS, depth=0)


def _project_mapping(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    depth: int,
) -> dict[str, Any]:
    if depth > 4:
        return {}
    output: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name not in allowed or name in _TRANSPORT_KEYS:
            continue
        if isinstance(item, Mapping):
            output[name] = _project_mapping(item, allowed=_CANDIDATE_PROJECTION_KEYS, depth=depth + 1)
        elif isinstance(item, list):
            nested_allowed = _NEED_KEYS if name == "needs" else _CANDIDATE_PROJECTION_KEYS
            output[name] = [
                _project_mapping(row, allowed=nested_allowed, depth=depth + 1)
                if isinstance(row, Mapping)
                else _bounded_scalar(row)
                for row in item[:12]
            ]
        else:
            output[name] = _bounded_scalar(item)
    return output


def _bounded_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value[:640]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:160]


def validate_candidate_catalog_binding(
    candidate: LivingContextCandidate,
    catalog: list[Mapping[str, Any]] | None,
) -> list[str]:
    """Validate all server-issued bindings against one catalog snapshot.

    This is intentionally pure: it never changes a candidate, refreshes a
    revision, or mutates the catalog.  Callers may use the result to decide
    whether a bounded candidate-only recovery is warranted, while final
    admission must still perform its own CAS check against the live state.
    """

    if candidate.disposition not in {"update", "correct", "resolve"}:
        return []
    rows = _catalog_projection(catalog)
    binding = (
        candidate.situation_token,
        candidate.situation_revision,
        candidate.catalog_token,
    )
    matched = [
        row
        for row in rows
        if (
            row.get("situation_token"),
            row.get("situation_revision"),
            row.get("catalog_token"),
        )
        == binding
    ]
    if not matched:
        return ["living_context_candidate:binding:not_in_catalog"]
    binding_tokens = {reference.need_token for reference in candidate.answered_need_bindings}
    answer_tokens = set(candidate.answered_need_tokens)
    binding_pairs = {
        (reference.need_token, reference.generation)
        for reference in candidate.answered_need_bindings
    }
    if (
        binding_tokens != answer_tokens
        or len(candidate.answered_need_tokens) != len(answer_tokens)
        or len(candidate.answered_need_bindings) != len(binding_pairs)
    ):
        return ["living_context_candidate:need_binding:mismatch"]
    allowed_needs = {
        (str(need.get("need_token")), int(need.get("generation")))
        for need in matched[0].get("open_needs", [])
        if isinstance(need, Mapping)
        and isinstance(need.get("need_token"), str)
        and type(need.get("generation")) is int
        and need.get("generation") >= 1
    }
    for reference in candidate.answered_need_bindings:
        if (reference.need_token, reference.generation) not in allowed_needs:
            return ["living_context_candidate:need_binding:not_in_catalog"]
    return []


# Kept as a narrow compatibility alias for existing local diagnostics.  New
# callers should use the public pure function above.
_catalog_binding_issues = validate_candidate_catalog_binding


def _candidate_from_response(response: Mapping[str, Any]) -> Any:
    """Accept documented envelopes plus an exact transport-root candidate.

    ``CoreModelClient`` adds transport metadata to a provider's root JSON
    object.  A provider that returned a candidate at that root is compatible
    only when the two discriminator fields are exact; arbitrary nested/root
    dictionaries are never searched.
    """

    direct = response.get("living_context_candidate")
    if isinstance(direct, Mapping):
        return direct
    direct = response.get("candidate")
    if isinstance(direct, Mapping):
        return direct
    situation = response.get("situation_assessment")
    if isinstance(situation, Mapping):
        nested = situation.get("living_context_candidate")
        if isinstance(nested, Mapping):
            return nested
    root_disposition = response.get("disposition")
    root_shape_fields = {
        "create_subject",
        "situation_token",
        "situation_revision",
        "catalog_token",
        "summary",
        "goal",
        "known",
        "timeline",
        "needs",
    }
    # A root candidate with a missing schema_version can still be recognized
    # only by a non-quiet disposition plus at least one candidate-shaped
    # field.  This prevents an arbitrary transport object or quiet marker
    # from being promoted into a candidate.  Explicit wrong versions remain
    # parseable here so the strict Literal rejects them below.
    root_disposition_text = (
        root_disposition.strip()
        if isinstance(root_disposition, str)
        else ""
    )
    root_is_nonquiet = root_disposition_text in _NONQUIET_DISPOSITION_VALUES
    if (
        root_disposition in {"quiet", "create", "update", "correct", "resolve"}
        or (
            root_is_nonquiet
            and any(field in response for field in root_shape_fields)
        )
    ) and (
        response.get("schema_version") == SCHEMA_VERSION
        or (
            root_is_nonquiet
            and any(field in response for field in root_shape_fields)
        )
    ):
        root_candidate = dict(response)
        for key in ("status", "_model", "duration_ms", "model_status"):
            root_candidate.pop(key, None)
        return root_candidate
    return None


def _catalog_projection(rows: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep only server-issued binding material and bounded context labels.

    The durable/runtime catalog calls the Situation revision
    ``observation_revision``; the model candidate contract calls the same
    server-issued value ``situation_revision``.  Project the value under the
    contract name without changing it.  This is a transport-key normalization,
    not a candidate repair: IDs and revisions are never synthesized or
    rewritten here.
    """

    projected: list[dict[str, Any]] = []
    for row in (rows or [])[:8]:
        if not isinstance(row, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in (
            CATALOG_SELECTOR_FIELD,
            "situation_token",
            "catalog_token",
            "title",
            "summary",
            "goal",
        ):
            value = row.get(key)
            if key in {
                CATALOG_SELECTOR_FIELD,
                "situation_token",
                "catalog_token",
                "title",
                "summary",
                "goal",
            }:
                if isinstance(value, str) and value:
                    item[key] = value[:480]
        if CATALOG_SELECTOR_FIELD not in item:
            owner_id = row.get("owner_id")
            session_id = row.get("session_id")
            situation_token = row.get("situation_token")
            if (
                isinstance(owner_id, str)
                and owner_id
                and isinstance(session_id, str)
                and session_id
                and isinstance(situation_token, str)
                and situation_token
            ):
                item[CATALOG_SELECTOR_FIELD] = situation_catalog_selector(
                    owner_id,
                    session_id,
                    situation_token,
                )
        situation_revision = row.get("situation_revision")
        observation_revision = row.get("observation_revision")
        # A row carrying both names must agree.  An inconsistent server
        # projection is not made valid by choosing one revision.
        if (
            isinstance(situation_revision, bool)
            or not isinstance(situation_revision, int)
            or situation_revision < 1
        ):
            situation_revision = None
        if (
            isinstance(observation_revision, bool)
            or not isinstance(observation_revision, int)
            or observation_revision < 1
        ):
            observation_revision = None
        if (
            situation_revision is not None
            and observation_revision is not None
            and situation_revision != observation_revision
        ):
            situation_revision = None
        elif situation_revision is None:
            situation_revision = observation_revision
        if situation_revision is not None:
            item["situation_revision"] = situation_revision
        needs: list[dict[str, Any]] = []
        raw_needs = row.get("open_needs")
        if isinstance(raw_needs, list):
            for need in raw_needs[:8]:
                if not isinstance(need, Mapping):
                    continue
                projected_need: dict[str, Any] = {}
                for key in ("need_token", "generation", "blocked_judgment", "question", "status"):
                    value = need.get(key)
                    if key in {"need_token", "blocked_judgment", "question", "status"}:
                        if isinstance(value, str) and value:
                            projected_need[key] = value[:480]
                    elif isinstance(value, int) and value >= 1:
                        projected_need[key] = value
                if projected_need:
                    needs.append(projected_need)
        if needs:
            item["open_needs"] = needs
        if item:
            projected.append(item)
    return projected


def _repair_catalog_projection(
    candidate: Any,
    initial_issues: list[str],
    rows: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Narrow a repair catalog only after a unique binding selector.

    A model may copy one opaque selector while serializing the other binding
    fields incorrectly.  The repair call receives the selected server row
    only when the current catalog proves that selector unique.  It never
    chooses a Situation semantically: the repaired output must still copy all
    three binding fields and pass the normal catalog validation and admission
    CAS.
    """

    projected = _catalog_projection(rows)
    selected, _selector = _repair_binding_selection(
        candidate,
        initial_issues=initial_issues,
        projected=projected,
    )
    return selected


_BINDING_FIELDS = (
    CATALOG_SELECTOR_FIELD,
    "situation_token",
    "situation_revision",
    "catalog_token",
    "answered_need_tokens",
    "answered_need_bindings",
)


def _repair_previous_candidate(
    candidate: Any,
    *,
    initial_issues: list[str],
    catalog: list[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Keep only a uniquely proven opaque selector for a binding repair.

    A failed candidate can contain a provider-invented Situation token.  In
    that case repeating the token and need references in the repair prompt
    gives the model an accidental authority hint and can make it bind a
    different Situation by inference.  For a binding issue, retain only a
    unique selector already present in the server catalog: a unique
    ``situation_token`` takes precedence; otherwise a unique
    ``catalog_token`` may be retained.  Revisions, selectors, and answered-
    Need fields are removed unless the selector is copied from the same
    uniquely selected server row.  The repair output must still provide the
    complete binding triple and pass validation; this helper never fills it
    in.
    """

    projected = _candidate_projection(candidate)
    if not _is_binding_issue(initial_issues):
        return projected
    rows = _catalog_projection(catalog)
    selected, selector = _repair_binding_selection(
        candidate,
        initial_issues=initial_issues,
        projected=rows,
    )
    for key in _BINDING_FIELDS:
        projected.pop(key, None)
    if selector is not None:
        key, value = selector
        projected[key] = value
        if len(selected) == 1:
            selected_selector = selected[0].get(CATALOG_SELECTOR_FIELD)
            if isinstance(selected_selector, str) and selected_selector:
                projected[CATALOG_SELECTOR_FIELD] = selected_selector
    return projected


def _is_binding_issue(issues: list[str]) -> bool:
    """Return whether the failed parse is in the opaque binding boundary."""

    return any(
        issue.startswith("living_context_candidate:binding:")
        or issue.startswith("living_context_candidate:need_binding:")
        for issue in issues
    )


def _repair_binding_selection(
    candidate: Any,
    *,
    initial_issues: list[str],
    projected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    """Select one server row and one opaque selector, without rebinding.

    The candidate's Situation token is the preferred selector when it
    uniquely identifies one current row.  If it does not, a unique catalog
    token is the only fallback.  An ambiguous or absent pair leaves the full
    bounded catalog and no binding fields in ``previous_candidate``.
    """

    if not _is_binding_issue(initial_issues) or not isinstance(candidate, Mapping):
        return projected, None

    situation_token = candidate.get("situation_token")
    if isinstance(situation_token, str) and situation_token:
        situation_rows = [
            row for row in projected if row.get("situation_token") == situation_token
        ]
        if len(situation_rows) == 1:
            return situation_rows, ("situation_token", situation_token)

    catalog_token = candidate.get("catalog_token")
    if isinstance(catalog_token, str) and catalog_token:
        catalog_rows = [
            row for row in projected if row.get("catalog_token") == catalog_token
        ]
        if len(catalog_rows) == 1:
            return catalog_rows, ("catalog_token", catalog_token)

    return projected, None


def _understanding_projection(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, str] = {}
    for key in ("intent", "task_type", "task_summary", "explicit_request", "user_goal", "hidden_need"):
        item = value.get(key)
        if isinstance(item, str) and item:
            output[key] = item[:480]
    return output


def _rejected(
    reason: str,
    *,
    issues: list[str] | None = None,
    repair_attempted: bool = False,
) -> CandidateExtractionResult:
    issue = f"living_context_candidate:extractor:{reason}"
    safe_issues = list(issues or [issue])[:16]
    return CandidateExtractionResult(
        candidate=None,
        issues=safe_issues,
        metrics={
            "status": "rejected",
            "repair_count": 1 if repair_attempted else 0,
            "repaired_fields": [],
            "issue_codes": safe_issues,
            "extractor": "living_context_candidate_v1",
            "extractor_attempted": True,
            "repair_attempted": bool(repair_attempted),
        },
    )
