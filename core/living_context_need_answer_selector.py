"""One bounded model decision for answers to current InformationNeeds."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.living_context_information_state_policy import UNRESOLVED_SUPPORT_POLICY_VERSION
from core.semantic_frame import TurnSemanticFrame
from interface.living_context_contract import ContextQuote


NEED_ANSWER_SELECTOR_PURPOSE = "living_context_need_answer_selection"
NEED_ANSWER_SCHEMA_VERSION = "veyra.living_context_need_answer_selection.v1"
_ACTIVE_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
_MAX_NEEDS = 8
_MAX_UNKNOWNS = 12
_MAX_KNOWN = 12
_MAX_KNOWN_CHARS = 480
_MAX_SOURCE_CHARS = 480
_UNKNOWN_TOKEN_RE = re.compile(r"^unk_[0-9a-f]{32}$")

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
    "A candidate Known is usable only when its epistemic_status is reported and "
    "its source_quote is an exact slice of the current user_message; do not treat "
    "a model summary or inferred Known as direct evidence. "
    "A standalone Unknown may be resolved only through standalone_unknown_resolutions, "
    "which must copy an exact unknown_token+generation from unknown_endpoints and point "
    "to a source-bound, affirmative candidate Known. Never emit the Unknown statement "
    "itself in a standalone resolution. Use answers=[] and "
    "standalone_unknown_resolutions=[] when nothing is affirmatively resolved."
)


class _RawStandaloneUnknownResolution(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    unknown_token: str = Field(min_length=1, max_length=240)
    generation: int = Field(ge=1)
    supporting_known_index: int = Field(ge=0)


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
    standalone_unknown_resolutions: list[_RawStandaloneUnknownResolution] = Field(
        default_factory=list,
        max_length=_MAX_UNKNOWNS,
    )

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
        unknown_pairs = [
            (item.unknown_token, item.generation)
            for item in self.standalone_unknown_resolutions
        ]
        if len(unknown_pairs) != len(set(unknown_pairs)):
            raise ValueError("standalone Unknown resolved more than once")
        if len(
            {item.unknown_token for item in self.standalone_unknown_resolutions}
        ) != len(unknown_pairs):
            raise ValueError("standalone Unknown token resolved more than once")
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
class StandaloneUnknownResolutionBinding:
    """A server-validated exact resolution of one prior semantic Unknown."""

    unknown_token: str
    generation: int
    unknown_text: str
    supporting_known_index: int
    source_quote: ContextQuote


@dataclass(slots=True, frozen=True)
class NeedAnswerSelection:
    answers: tuple[NeedAnswerBinding, ...]
    standalone_unknown_resolutions: tuple[StandaloneUnknownResolutionBinding, ...] = ()

    @property
    def discard_unknown(self) -> tuple[str, ...]:
        return (
            tuple(value for item in self.answers for value in item.discard_candidate_unknowns)
            + tuple(item.unknown_text for item in self.standalone_unknown_resolutions)
        )

    @property
    def discard_need_blocked(self) -> tuple[str, ...]:
        return tuple(value for item in self.answers for value in item.discard_candidate_need_endpoints)

    @property
    def discard_standalone_unknowns(self) -> tuple[str, ...]:
        return tuple(item.unknown_text for item in self.standalone_unknown_resolutions)


@dataclass(slots=True, frozen=True)
class NeedAnswerSelectionResult:
    selection: NeedAnswerSelection | None
    issues: list[str]
    metrics: dict[str, Any]


@dataclass(slots=True, frozen=True)
class _ProjectedKnown:
    """A model Known plus the provenance needed for server-side admission."""

    statement: str
    source_quote: ContextQuote | None
    epistemic_status: str | None

    def model_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"statement": self.statement}
        if self.source_quote is not None:
            payload["source_quote"] = self.source_quote.model_dump(mode="json")
        if self.epistemic_status is not None:
            payload["epistemic_status"] = self.epistemic_status
        return payload


def _issues(code: str) -> list[str]:
    return [f"living_context_need_answer_selector:{code}"]


def _project_open_needs(
    *,
    row: Mapping[str, Any] | None,
    open_needs: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    value: Any = open_needs if open_needs is not None else (row.get("open_needs") if isinstance(row, Mapping) else None)
    if value is None:
        value = []
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


def _project_unknown_endpoints(
    value: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Validate the server-owned Unknown endpoint catalog without rewriting it."""

    selected = list(value or [])
    if len(selected) > _MAX_UNKNOWNS:
        return None, _issues("unknown_endpoints_invalid")
    projected: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, int]] = set()
    seen_tokens: set[str] = set()
    seen_text: set[str] = set()
    for raw in selected:
        if not isinstance(raw, Mapping):
            return None, _issues("unknown_endpoint_invalid")
        if set(raw) != {"unknown_token", "generation", "statement"}:
            return None, _issues("unknown_endpoint_invalid")
        token = raw.get("unknown_token")
        generation = raw.get("generation")
        statement = raw.get("statement")
        if (
            not isinstance(token, str)
            or not _UNKNOWN_TOKEN_RE.fullmatch(token)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(statement, str)
            or not statement
            or len(statement) > _MAX_KNOWN_CHARS
        ):
            return None, _issues("unknown_endpoint_invalid")
        pair = (token, generation)
        if pair in seen_pairs or token in seen_tokens or statement in seen_text:
            return None, _issues("unknown_endpoint_duplicate")
        seen_pairs.add(pair)
        seen_tokens.add(token)
        seen_text.add(statement)
        projected.append(
            {
                "unknown_token": token,
                "generation": generation,
                "statement": statement,
            }
        )
    return projected, []


def _project_candidate_known(
    value: Sequence[str | Mapping[str, Any]] | None,
) -> tuple[list[_ProjectedKnown] | None, list[str]]:
    """Project Known statements while retaining provenance, never inventing it.

    The string form is kept only as a narrow compatibility input for old
    callers, but it is never promoted to direct evidence. Structured mappings
    must carry an explicit ``reported`` status and a source quote before they
    can support a write.
    """

    selected = list(value or [])
    if len(selected) > _MAX_KNOWN:
        return None, _issues("candidate_endpoints_invalid")
    projected: list[_ProjectedKnown] = []
    for raw in selected:
        if isinstance(raw, str):
            statement = raw
            if not statement or len(statement) > _MAX_KNOWN_CHARS:
                return None, _issues("candidate_known_invalid")
            projected.append(
                _ProjectedKnown(
                    statement=statement,
                    source_quote=None,
                    epistemic_status=None,
                )
            )
            continue
        elif isinstance(raw, Mapping):
            if set(raw) - {"statement", "source_quote", "epistemic_status"}:
                return None, _issues("candidate_known_invalid")
            statement = raw.get("statement")
            if not isinstance(statement, str):
                return None, _issues("candidate_known_invalid")
            epistemic_status = raw.get("epistemic_status")
            if epistemic_status is not None and epistemic_status not in {"reported", "inferred"}:
                return None, _issues("candidate_known_invalid")
            raw_quote = raw.get("source_quote")
            if raw_quote is None:
                quote = None
            else:
                try:
                    quote = ContextQuote.model_validate(raw_quote, strict=True)
                except ValidationError:
                    return None, _issues("candidate_known_invalid")
        else:
            return None, _issues("candidate_known_invalid")
        if not statement or len(statement) > _MAX_KNOWN_CHARS:
            return None, _issues("candidate_known_invalid")
        projected.append(
            _ProjectedKnown(
                statement=statement,
                source_quote=quote,
                epistemic_status=epistemic_status,
            )
        )
    return projected, []


def _affirmative_source_quote(
    known: _ProjectedKnown,
    *,
    source_text: str,
    semantic_frame: TurnSemanticFrame | None,
) -> ContextQuote | None:
    """Return a quote backed by one resolved, direct semantic act.

    The selector deliberately does not interpret words in the Known statement.
    Positive support is admitted only when the candidate carries a reported
    Known with an exact source quote and the already-validated semantic frame
    contains exactly one matching direct-user positive asserted act in normal
    use, without a condition.
    """

    if (
        known.epistemic_status != "reported"
        or known.source_quote is None
        or semantic_frame is None
        or semantic_frame.resolver_status != "resolved"
    ):
        return None
    quote = known.source_quote
    if (
        not source_text
        or quote.end > len(source_text)
        or source_text[quote.start : quote.end] != quote.text
        or known.statement != quote.text
    ):
        return None
    matching_acts = [
        act
        for act in semantic_frame.acts
        if (
            act.source_quote.text == quote.text
            and act.source_quote.start == quote.start
            and act.source_quote.end == quote.end
            and act.speaker == "user"
            and act.authority == "direct_user"
            and act.polarity == "positive"
            and act.modality == "asserted"
            and act.mention_mode == "normal_use"
            and act.condition is None
        )
    ]
    if len(matching_acts) != 1:
        return None
    return quote


def _parse_envelope(value: Any) -> tuple[Mapping[str, Any] | None, list[str]]:
    if not isinstance(value, Mapping):
        return None, _issues("response_invalid")
    transport = {"status", "model_status", "duration_ms", "_model"}
    envelope_allowed = {*transport, "living_context_need_answer_selection"}
    root_allowed = {
        *transport,
        "schema_version",
        "answers",
        "standalone_unknown_resolutions",
    }
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
            "standalone_unknown_resolutions": value.get("standalone_unknown_resolutions", []),
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
            "unique_asked_fallback_count": 0,
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
    candidate_known: Sequence[str | Mapping[str, Any]] | None = None,
    standalone_unknown_endpoints: Sequence[Mapping[str, Any]] | None = None,
    semantic_frame: TurnSemanticFrame | None = None,
) -> NeedAnswerSelectionResult:
    """Run one batch decision and validate it entirely against supplied sets."""

    needs, need_issues = _project_open_needs(row=row, open_needs=open_needs)
    unknowns, unknown_issues = _candidate_strings(candidate_unknown, limit=_MAX_UNKNOWNS)
    candidate_needs, candidate_need_issues = _candidate_strings(candidate_need_blocked, limit=_MAX_NEEDS)
    knowns, known_issues = _project_candidate_known(candidate_known)
    raw_unknown_endpoints = standalone_unknown_endpoints
    if raw_unknown_endpoints is None and isinstance(row, Mapping):
        selected_row_unknowns = row.get("unknown_endpoints")
        if not isinstance(selected_row_unknowns, list):
            selected_row_unknowns = row.get("unknown")
        if isinstance(selected_row_unknowns, list) and all(
            isinstance(item, Mapping) for item in selected_row_unknowns
        ):
            raw_unknown_endpoints = selected_row_unknowns
    unknown_endpoints, unknown_endpoint_issues = _project_unknown_endpoints(
        raw_unknown_endpoints
    )
    input_issues = [
        *need_issues,
        *unknown_issues,
        *candidate_need_issues,
        *known_issues,
        *unknown_endpoint_issues,
    ]
    if (
        input_issues
        or needs is None
        or unknowns is None
        or candidate_needs is None
        or knowns is None
        or (not needs and not unknown_endpoints)
    ):
        return NeedAnswerSelectionResult(
            selection=None,
            issues=input_issues or _issues("no_active_needs"),
            metrics={
                "status": "unavailable",
                "reason": "invalid_input",
                "answered_count": 0,
                "suppressed_unresolved_support_count": 0,
                "unique_asked_fallback_count": 0,
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
                "unique_asked_fallback_count": 0,
                "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            },
        )
    if (
        not isinstance(semantic_frame, TurnSemanticFrame)
        or semantic_frame.resolver_status != "resolved"
    ):
        return _rejected("semantic_frame_unavailable")
    source_text = str(user_message or "")
    request_source = source_text[:_MAX_SOURCE_CHARS]
    request = {
        "user_message": request_source,
        "active_open_needs": needs,
        "candidate_known": [known.model_payload() for known in knowns],
        "candidate_unknown": unknowns,
        "candidate_need_endpoints": candidate_needs,
        "unknown_endpoints": unknown_endpoints or [],
        "semantic_frame": semantic_frame.compact(),
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
            "standalone_unknown_resolutions": [
                {
                    "unknown_token": "exact token from unknown_endpoints",
                    "generation": "exact generation from unknown_endpoints",
                    "supporting_known_index": "zero-based index into candidate_known",
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
                "unique_asked_fallback_count": 0,
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
    endpoint_by_pair = {
        (item["unknown_token"], item["generation"]): item
        for item in (unknown_endpoints or [])
    }
    for item in raw.answers:
        if (item.need_token, item.generation) not in active:
            return _rejected("answer_not_active")
        if item.supporting_known_index >= len(knowns):
            return _rejected("supporting_known_index_out_of_range")
        if any(value not in unknown_set for value in item.discard_candidate_unknowns):
            return _rejected("unknown_discard_not_in_candidate")
        if any(value not in candidate_need_set for value in item.discard_candidate_need_endpoints):
            return _rejected("need_discard_not_in_candidate")
    for item in raw.standalone_unknown_resolutions:
        if (item.unknown_token, item.generation) not in endpoint_by_pair:
            return _rejected("unknown_endpoint_not_current")
        if item.supporting_known_index >= len(knowns):
            return _rejected("supporting_known_index_out_of_range")
    supporting_indices = [item.supporting_known_index for item in raw.answers]
    supporting_indices.extend(
        item.supporting_known_index for item in raw.standalone_unknown_resolutions
    )
    if len(supporting_indices) != len(set(supporting_indices)):
        return _rejected("supporting_known_index_duplicate")
    if (raw.answers or raw.standalone_unknown_resolutions) and not source_text:
        return _rejected("empty_source")
    accepted_answers: list[NeedAnswerBinding] = []
    suppressed_count = 0
    for item in raw.answers:
        known_index = item.supporting_known_index
        source_bound_known_quote = _affirmative_source_quote(
            knowns[known_index],
            source_text=source_text,
            semantic_frame=semantic_frame,
        )
        if source_bound_known_quote is None:
            suppressed_count += 1
            continue
        accepted_answers.append(
            NeedAnswerBinding(
                need_token=item.need_token,
                generation=item.generation,
                supporting_known_index=known_index,
                source_quote=source_bound_known_quote,
                discard_candidate_unknowns=tuple(item.discard_candidate_unknowns),
                discard_candidate_need_endpoints=tuple(item.discard_candidate_need_endpoints),
            )
        )
    accepted_unknown_resolutions = []
    suppressed_unknown_support_count = 0
    for item in raw.standalone_unknown_resolutions:
        known_index = item.supporting_known_index
        endpoint = endpoint_by_pair[(item.unknown_token, item.generation)]
        known = knowns[known_index]
        source_bound_known_quote = _affirmative_source_quote(
            known,
            source_text=source_text,
            semantic_frame=semantic_frame,
        )
        if (
            not source_text
            or source_bound_known_quote is None
        ):
            suppressed_unknown_support_count += 1
            continue
        accepted_unknown_resolutions.append(
            StandaloneUnknownResolutionBinding(
                unknown_token=item.unknown_token,
                generation=item.generation,
                unknown_text=endpoint["statement"],
                supporting_known_index=known_index,
                source_quote=source_bound_known_quote,
            )
        )
    unique_asked_fallback_count = 0
    selection = NeedAnswerSelection(
        answers=tuple(accepted_answers),
        standalone_unknown_resolutions=tuple(accepted_unknown_resolutions),
    )
    return NeedAnswerSelectionResult(
        selection=selection,
        issues=[],
        metrics={
            "status": "accepted",
            "answered_count": len(accepted_answers),
            "suppressed_unresolved_support_count": suppressed_count,
            "suppressed_unresolved_unknown_support_count": suppressed_unknown_support_count,
            "unique_asked_fallback_count": unique_asked_fallback_count,
            "unresolved_support_policy_version": UNRESOLVED_SUPPORT_POLICY_VERSION,
            "discard_unknown_count": len(selection.discard_unknown),
            "discard_need_count": len(selection.discard_need_blocked),
            "standalone_unknown_resolved_count": len(accepted_unknown_resolutions),
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
    "StandaloneUnknownResolutionBinding",
    "select_living_context_need_answers",
]
