"""One bounded model decision for answers to current InformationNeeds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.living_context_information_state_policy import (
    UNRESOLVED_SUPPORT_POLICY_VERSION,
    has_unresolved_statement,
)
from interface.living_context_contract import ContextQuote


NEED_ANSWER_SELECTOR_PURPOSE = "living_context_need_answer_selection"
NEED_ANSWER_SCHEMA_VERSION = "veyra.living_context_need_answer_selection.v1"
_ACTIVE_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
_MAX_NEEDS = 8
_MAX_UNKNOWNS = 12
_MAX_KNOWN = 12
_MAX_KNOWN_CHARS = 480
_MAX_SOURCE_CHARS = 480

NEED_ANSWER_SELECTOR_SYSTEM = (
    "Return JSON matching required_json_shape. Select a current Need only when "
    "user_message provides affirmative new information that resolves its missing "
    "judgment. A mention is not an answer. Statements that something is still "
    "unknown, unconfirmed, pending, waiting, planned for later, or being asked "
    "about must keep that Need open and must not discard its candidate endpoints. "
    "For example, if the missing item is a date, 'the date is Friday' answers it, "
    "while 'the date is still not confirmed' does not. Copy every token, generation, "
    "and discard string exactly from the supplied collections. Every answer must "
    "include supporting_known_index as a zero-based index into candidate_known; "
    "use each candidate Known at most once, and never invent or reuse an index. "
    "Use answers=[] when no Need is affirmatively resolved."
)


class _RawAnswer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    need_token: str = Field(min_length=1, max_length=240)
    generation: int = Field(ge=1)
    supporting_known_index: int = Field(ge=0)
    discard_candidate_unknowns: list[str] = Field(default_factory=list, max_length=_MAX_UNKNOWNS)
    discard_candidate_need_endpoints: list[str] = Field(default_factory=list, max_length=_MAX_NEEDS)

    @model_validator(mode="after")
    def validate_unique_discards(self) -> "_RawAnswer":
        if len(self.discard_candidate_unknowns) != len(set(self.discard_candidate_unknowns)):
            raise ValueError("duplicate candidate unknown discard")
        if len(self.discard_candidate_need_endpoints) != len(set(self.discard_candidate_need_endpoints)):
            raise ValueError("duplicate candidate Need discard")
        return self


class _RawBatch(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: str
    answers: list[_RawAnswer] = Field(default_factory=list, max_length=_MAX_NEEDS)

    @model_validator(mode="after")
    def validate_batch(self) -> "_RawBatch":
        if self.schema_version != NEED_ANSWER_SCHEMA_VERSION:
            raise ValueError("wrong selector schema")
        pairs = [(item.need_token, item.generation) for item in self.answers]
        if len(pairs) != len(set(pairs)) or len({item.need_token for item in self.answers}) != len(pairs):
            raise ValueError("duplicate answer reference")
        unknown_discards = [value for item in self.answers for value in item.discard_candidate_unknowns]
        need_discards = [value for item in self.answers for value in item.discard_candidate_need_endpoints]
        if len(unknown_discards) != len(set(unknown_discards)):
            raise ValueError("candidate unknown discarded more than once")
        if len(need_discards) != len(set(need_discards)):
            raise ValueError("candidate Need discarded more than once")
        return self


@dataclass(slots=True, frozen=True)
class NeedAnswerBinding:
    need_token: str
    generation: int
    supporting_known_index: int
    source_quote: ContextQuote
    discard_candidate_unknowns: tuple[str, ...] = ()
    discard_candidate_need_endpoints: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class NeedAnswerSelection:
    answers: tuple[NeedAnswerBinding, ...]

    @property
    def discard_unknown(self) -> tuple[str, ...]:
        return tuple(value for item in self.answers for value in item.discard_candidate_unknowns)

    @property
    def discard_need_blocked(self) -> tuple[str, ...]:
        return tuple(value for item in self.answers for value in item.discard_candidate_need_endpoints)


@dataclass(slots=True, frozen=True)
class NeedAnswerSelectionResult:
    selection: NeedAnswerSelection | None
    issues: list[str]
    metrics: dict[str, Any]


def _issues(code: str) -> list[str]:
    return [f"living_context_need_answer_selector:{code}"]


def _project_open_needs(
    *,
    row: Mapping[str, Any] | None,
    open_needs: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    value: Any = open_needs if open_needs is not None else (row.get("open_needs") if isinstance(row, Mapping) else None)
    if not isinstance(value, list) or len(value) > _MAX_NEEDS:
        return None, _issues("open_needs_invalid")
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            return None, _issues("open_need_invalid")
        status = str(raw.get("status") or "open")
        if status not in _ACTIVE_STATUSES:
            continue
        token = raw.get("need_token")
        generation = raw.get("generation")
        blocked = raw.get("blocked_judgment")
        question = raw.get("question", "")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 240
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(blocked, str)
            or not blocked
            or len(blocked) > 480
            or not isinstance(question, str)
            or len(question) > 360
        ):
            return None, _issues("open_need_invalid")
        pair = (token, generation)
        if pair in seen or any(item["need_token"] == token for item in projected):
            return None, _issues("open_need_duplicate")
        seen.add(pair)
        projected.append(
            {
                "need_token": token,
                "generation": generation,
                "blocked_judgment": blocked,
                "question": question,
                "status": status,
            }
        )
    return projected, []


def _candidate_strings(
    value: Sequence[str] | None,
    *,
    limit: int,
    max_item_chars: int | None = None,
    unique: bool = True,
) -> tuple[list[str] | None, list[str]]:
    selected = list(value or [])
    if len(selected) > limit or any(
        not isinstance(item, str)
        or not item
        or (max_item_chars is not None and len(item) > max_item_chars)
        for item in selected
    ):
        return None, _issues("candidate_endpoints_invalid")
    if unique and len(selected) != len(set(selected)):
        return None, _issues("candidate_endpoints_duplicate")
    return selected, []


def _parse_envelope(value: Any) -> tuple[Mapping[str, Any] | None, list[str]]:
    if not isinstance(value, Mapping):
        return None, _issues("response_invalid")
    transport = {"status", "model_status", "duration_ms", "_model"}
    envelope_allowed = {*transport, "living_context_need_answer_selection"}
    root_allowed = {*transport, "schema_version", "answers"}
    keys = set(map(str, value))
    if str(value.get("status") or "") != "model_assisted":
        return None, _issues("response_invalid")
    payload = value.get("living_context_need_answer_selection")
    if isinstance(payload, Mapping) and not keys - envelope_allowed:
        return payload, []
    # Some providers follow ``required_json_shape`` by placing the exact
    # batch fields at response root.  Accept only that closed transport form;
    # do not scan arbitrary nested values or copy unknown root fields.
    if not keys - root_allowed and {"schema_version", "answers"}.issubset(keys):
        return {
            "schema_version": value.get("schema_version"),
            "answers": value.get("answers"),
        }, []
    return None, _issues("selection_missing")


def _rejected(code: str, *, issues: list[str] | None = None) -> NeedAnswerSelectionResult:
    selected = list(issues or _issues(code))[:4]
    return NeedAnswerSelectionResult(
        selection=None,
        issues=selected,
        metrics={
            "status": "rejected",
            "reason": code,
            "answered_count": 0,
            "suppressed_unresolved_support_count": 0,
            "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            "issue_codes": selected,
        },
    )


def select_living_context_need_answers(
    client: Any,
    *,
    user_message: str,
    row: Mapping[str, Any] | None = None,
    open_needs: Sequence[Mapping[str, Any]] | None = None,
    candidate_unknown: Sequence[str] | None = None,
    candidate_need_blocked: Sequence[str] | None = None,
    candidate_known: Sequence[str] | None = None,
) -> NeedAnswerSelectionResult:
    """Run one batch decision and validate it entirely against supplied sets."""

    needs, need_issues = _project_open_needs(row=row, open_needs=open_needs)
    unknowns, unknown_issues = _candidate_strings(candidate_unknown, limit=_MAX_UNKNOWNS)
    candidate_needs, candidate_need_issues = _candidate_strings(candidate_need_blocked, limit=_MAX_NEEDS)
    knowns, known_issues = _candidate_strings(
        candidate_known,
        limit=_MAX_KNOWN,
        max_item_chars=_MAX_KNOWN_CHARS,
        unique=False,
    )
    input_issues = [*need_issues, *unknown_issues, *candidate_need_issues, *known_issues]
    if input_issues or needs is None or unknowns is None or candidate_needs is None or knowns is None or not needs:
        return NeedAnswerSelectionResult(
            selection=None,
            issues=input_issues or _issues("no_active_needs"),
            metrics={
                "status": "unavailable",
                "reason": "invalid_input",
                "answered_count": 0,
                "suppressed_unresolved_support_count": 0,
                "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            },
        )
    if not knowns:
        return NeedAnswerSelectionResult(
            selection=None,
            issues=_issues("candidate_known_empty"),
            metrics={
                "status": "unavailable",
                "reason": "candidate_known_empty",
                "answered_count": 0,
                "suppressed_unresolved_support_count": 0,
                "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            },
        )
    source_text = str(user_message or "")[:_MAX_SOURCE_CHARS]
    request = {
        "user_message": source_text,
        "active_open_needs": needs,
        "candidate_known": knowns,
        "candidate_unknown": unknowns,
        "candidate_need_endpoints": candidate_needs,
        "required_json_shape": {
            "schema_version": NEED_ANSWER_SCHEMA_VERSION,
            "answers": [
                {
                    "need_token": "exact active token",
                    "generation": "exact active generation",
                    "supporting_known_index": "zero-based index into candidate_known",
                    "discard_candidate_unknowns": "exact subset of candidate_unknown",
                    "discard_candidate_need_endpoints": "exact subset of candidate_need_endpoints",
                }
            ],
        },
    }
    try:
        response = client.complete_json(
            purpose=NEED_ANSWER_SELECTOR_PURPOSE,
            system=NEED_ANSWER_SELECTOR_SYSTEM,
            user=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception:
        return NeedAnswerSelectionResult(
            selection=None,
            issues=_issues("transport_error"),
            metrics={
                "status": "unavailable",
                "reason": "transport_error",
                "answered_count": 0,
                "suppressed_unresolved_support_count": 0,
                "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            },
        )
    payload, envelope_issues = _parse_envelope(response)
    if envelope_issues or payload is None:
        return _rejected("invalid_response", issues=envelope_issues)
    try:
        raw = _RawBatch.model_validate(dict(payload), strict=True)
    except ValidationError:
        return _rejected("selection_invalid")
    active = {(item["need_token"], item["generation"]) for item in needs}
    unknown_set = set(unknowns)
    candidate_need_set = set(candidate_needs)
    for item in raw.answers:
        if (item.need_token, item.generation) not in active:
            return _rejected("answer_not_active")
        if item.supporting_known_index >= len(knowns):
            return _rejected("supporting_known_index_out_of_range")
        if any(value not in unknown_set for value in item.discard_candidate_unknowns):
            return _rejected("unknown_discard_not_in_candidate")
        if any(value not in candidate_need_set for value in item.discard_candidate_need_endpoints):
            return _rejected("need_discard_not_in_candidate")
    supporting_indices = [item.supporting_known_index for item in raw.answers]
    if len(supporting_indices) != len(set(supporting_indices)):
        return _rejected("supporting_known_index_duplicate")
    accepted_raw_answers = tuple(
        item
        for item in raw.answers
        if not has_unresolved_statement(knowns[item.supporting_known_index])
    )
    suppressed_count = len(raw.answers) - len(accepted_raw_answers)
    if accepted_raw_answers and not source_text:
        return _rejected("empty_source")
    quote = ContextQuote(text=source_text, start=0, end=len(source_text)) if accepted_raw_answers else None
    answers = tuple(
        NeedAnswerBinding(
            need_token=item.need_token,
            generation=item.generation,
            supporting_known_index=item.supporting_known_index,
            source_quote=quote,  # type: ignore[arg-type]
            discard_candidate_unknowns=tuple(item.discard_candidate_unknowns),
            discard_candidate_need_endpoints=tuple(item.discard_candidate_need_endpoints),
        )
        for item in accepted_raw_answers
    )
    selection = NeedAnswerSelection(answers=answers)
    return NeedAnswerSelectionResult(
        selection=selection,
        issues=[],
        metrics={
            "status": "accepted",
            "answered_count": len(answers),
            "suppressed_unresolved_support_count": suppressed_count,
            "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            "discard_unknown_count": len(selection.discard_unknown),
            "discard_need_count": len(selection.discard_need_blocked),
            "issue_codes": [],
        },
    )


__all__ = [
    "NEED_ANSWER_SCHEMA_VERSION",
    "NEED_ANSWER_SELECTOR_PURPOSE",
    "NEED_ANSWER_SELECTOR_SYSTEM",
    "UNRESOLVED_SUPPORT_POLICY_VERSION",
    "NeedAnswerBinding",
    "NeedAnswerSelection",
    "NeedAnswerSelectionResult",
    "select_living_context_need_answers",
]
