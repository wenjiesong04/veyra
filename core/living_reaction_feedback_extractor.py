"""Bounded model seam for explicit Living Context reaction feedback.

The combined understanding response is intentionally allowed to omit the
optional reaction-feedback artifact.  This module gives that artifact one
small, independent retry surface.  It never chooses a Situation on behalf of
the model, creates a token, or mutates the reaction ledger.  The exact
reaction token and source quote are still checked by the shared contract and
the runtime scope/CAS boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from interface.living_context_contract import (
    REACTION_FEEDBACK_LABELS,
    LivingReactionFeedback,
    parse_living_reaction_feedback_detailed,
    situation_catalog_selector,
)


FEEDBACK_EXTRACTOR_SYSTEM = (
    "You are Veyra's bounded reaction-feedback extractor. Return strict JSON "
    "only with exactly one top-level key: living_reaction_feedback. The value "
    "must be null unless the user message directly evaluates a current Veyra "
    "reaction (for example useful, not useful, ignore, resolved, too early, "
    "too late, too frequent, or a request to be reminded before a time). "
    "A normal Situation update, question, acknowledgment, or unrelated message "
    "is not feedback and must return null. Never infer feedback from sentiment "
    "or select a row by broad semantic similarity. Select a row only when the "
    "user message explicitly names or clearly identifies one supplied row; if "
    "more than one row could match, return null. If feedback is present, copy the "
    "exact reaction_token from exactly one supplied reaction_catalog row, use "
    "one of that row's allowed_labels, and copy an exact Python-character slice "
    "of user_message into source_quote. For remind_before, emit a positive "
    "integer remind_before_seconds; for every other label omit it or set it "
    "to null. Use ignore only for an explicit request to suppress, dismiss, "
    "or stop acting on the reaction; not_useful means the reaction was not "
    "helpful but is not a suppression request; resolved means the underlying "
    "Situation is complete or solved. Do not conflate these labels. Do not "
    "emit situation, candidate, route, authority, tool, URL, "
    "path, command, or explanation fields. If no row can be identified with "
    "certainty, return null. The feedback object shape is "
    "{schema_version:'veyra.living_reaction_feedback.v1', reaction_token:'...', "
    "label:'...', remind_before_seconds:null, source_quote:{text:'...',start:0,end:...}}."
)


_ALLOWED_TOP_LEVEL = frozenset({"living_reaction_feedback"})
_ALLOWED_LABELS = tuple(REACTION_FEEDBACK_LABELS)


@dataclass(slots=True, frozen=True)
class FeedbackExtractionResult:
    feedback: LivingReactionFeedback | None
    issues: list[str]
    metrics: dict[str, Any]


def _reaction_selector(row: Mapping[str, Any]) -> str | None:
    """Return the server-derived short row selector when scope is complete."""

    advertised = row.get("situation_selector")
    owner = row.get("owner_id")
    session = row.get("session_id")
    situation = row.get("situation_token")
    if (
        isinstance(owner, str)
        and owner
        and isinstance(session, str)
        and session
        and isinstance(situation, str)
        and situation
    ):
        derived = situation_catalog_selector(owner, session, situation)
        if advertised is None:
            return derived
        if advertised == derived:
            return derived
        return None
    return advertised if isinstance(advertised, str) and advertised else None


def _reaction_rows(catalog: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Project only current reactions needed to identify explicit feedback."""

    rows: list[dict[str, Any]] = []
    for raw in (catalog or [])[:8]:
        if not isinstance(raw, Mapping):
            continue
        reaction = raw.get("reaction")
        if not isinstance(reaction, Mapping):
            # A raw reaction projection is accepted only as an exact nested
            # object under the documented key.  Never search arbitrary fields.
            continue
        token = reaction.get("reaction_token")
        if not isinstance(token, str) or not token:
            continue
        selector = _reaction_selector(raw)
        if not selector:
            continue
        revision = reaction.get("reaction_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            continue
        text_fields: dict[str, str] = {}
        for key in ("title", "label", "summary", "goal"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                text_fields[key] = value.strip()[:240]
        rows.append(
            {
                "situation_selector": selector,
                "title": text_fields.get("title", ""),
                "label": text_fields.get("label", ""),
                "summary": text_fields.get("summary", ""),
                "goal": text_fields.get("goal", ""),
                "category": str(raw.get("category") or "")[:48],
                "reaction_token": token,
                "reaction_revision": revision,
                "disposition": str(reaction.get("disposition") or "")[:24],
                "allowed_labels": list(_ALLOWED_LABELS),
            }
        )
    return rows


def _safe_issue(code: str) -> list[str]:
    return [f"living_reaction_feedback:extractor:{code}"]


def _normalise_response(value: Any) -> tuple[Any, list[str]]:
    if not isinstance(value, Mapping):
        return None, _safe_issue("response_not_object")
    unknown = set(str(key) for key in value) - _ALLOWED_TOP_LEVEL
    if unknown:
        return None, _safe_issue("forbidden_top_level_field")
    return value.get("living_reaction_feedback"), []


def _canonicalise_binding(
    value: Any,
    *,
    rows: list[dict[str, Any]],
) -> tuple[Any, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, Mapping):
        return None, _safe_issue("feedback_not_object")
    payload = dict(value)
    selector = payload.pop("situation_selector", None)
    token = payload.get("reaction_token")
    matches = rows
    if selector is not None:
        if not isinstance(selector, str) or not selector:
            return None, _safe_issue("selector_invalid")
        matches = [row for row in rows if row.get("situation_selector") == selector]
        if len(matches) != 1:
            return None, _safe_issue("selector_not_unique")
        selected_token = matches[0].get("reaction_token")
        if token is not None and token != selected_token:
            return None, _safe_issue("token_selector_mismatch")
        payload["reaction_token"] = selected_token
    elif isinstance(token, str):
        matches = [row for row in rows if row.get("reaction_token") == token]
        if len(matches) != 1:
            return None, _safe_issue("token_not_unique")
    else:
        return None, _safe_issue("token_missing")
    allowed = matches[0].get("allowed_labels") if len(matches) == 1 else None
    if isinstance(allowed, list) and payload.get("label") not in allowed:
        return None, _safe_issue("label_not_allowed")
    # Transport selector is not part of the durable feedback contract.
    return payload, []


def extract_living_reaction_feedback(
    client: Any,
    *,
    text: str,
    catalog: list[Mapping[str, Any]] | None,
) -> FeedbackExtractionResult:
    """Extract and strictly validate one optional current reaction feedback."""

    rows = _reaction_rows(catalog)
    if not rows:
        return FeedbackExtractionResult(
            feedback=None,
            issues=[],
            metrics={
                "status": "unavailable",
                "extractor_attempted": False,
                "reason": "no_current_reaction",
                "row_count": 0,
            },
        )
    payload = {
        "user_message": text,
        "source_binding": {
            "python_character_length": len(text),
            "exact_full_turn_quote": {"text": text, "start": 0, "end": len(text)},
        },
        "reaction_catalog": rows,
        "allowed_labels": list(_ALLOWED_LABELS),
    }
    try:
        response = client.complete_json(
            purpose="living_reaction_feedback_extraction",
            system=FEEDBACK_EXTRACTOR_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        return FeedbackExtractionResult(
            feedback=None,
            issues=_safe_issue("transport_error"),
            metrics={
                "status": "unavailable",
                "extractor_attempted": True,
                "reason": "transport_error",
                "row_count": len(rows),
            },
        )
    value, response_issues = _normalise_response(response)
    if response_issues:
        return FeedbackExtractionResult(
            feedback=None,
            issues=response_issues,
            metrics={
                "status": "rejected",
                "extractor_attempted": True,
                "row_count": len(rows),
                "issue_codes": response_issues,
            },
        )
    if value is None:
        return FeedbackExtractionResult(
            feedback=None,
            issues=[],
            metrics={
                "status": "absent",
                "extractor_attempted": True,
                "row_count": len(rows),
            },
        )
    canonical, binding_issues = _canonicalise_binding(value, rows=rows)
    if binding_issues:
        return FeedbackExtractionResult(
            feedback=None,
            issues=binding_issues,
            metrics={
                "status": "rejected",
                "extractor_attempted": True,
                "row_count": len(rows),
                "issue_codes": binding_issues,
            },
        )
    feedback, issues, report = parse_living_reaction_feedback_detailed(
        canonical,
        source_text=text,
    )
    if feedback is None or issues:
        safe_issues = issues or _safe_issue("feedback_rejected")
        return FeedbackExtractionResult(
            feedback=None,
            issues=safe_issues,
            metrics={
                "status": "rejected",
                "extractor_attempted": True,
                "row_count": len(rows),
                "issue_codes": safe_issues,
            },
        )
    return FeedbackExtractionResult(
        feedback=feedback,
        issues=[],
        metrics={
            "status": str(report.get("status") or "accepted"),
            "extractor_attempted": True,
            "row_count": len(rows),
            "issue_codes": [],
        },
    )
