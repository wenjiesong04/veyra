from __future__ import annotations

import copy
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator


SCHEMA_VERSION = "veyra.semantic_frame.v1"
_SOURCE_QUOTE_LIMIT = 800

Explicitness = Literal["explicit", "strong_implied", "weak_implied", "inferred", "unknown"]
MentionMode = Literal["normal_use", "quoted_term", "reported_speech", "example", "hypothetical", "unknown"]
ReferentStatus = Literal["resolved", "ambiguous", "unresolved", "not_applicable"]
ResolverStatus = Literal["resolved", "ambiguous", "degraded", "invalid_output"]


class StrictSemanticModel(BaseModel):
    """Base contract for model-produced semantic facts.

    Coercion and undeclared fields are intentionally rejected.  Normalization of
    an external model response belongs in the resolver adapter, not in these
    semantic truth objects.
    """

    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)


class SourceQuote(StrictSemanticModel):
    text: str = Field(min_length=1, max_length=_SOURCE_QUOTE_LIMIT)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source_quote.end must be greater than source_quote.start")
        return self


class SemanticTarget(StrictSemanticModel):
    """Open-world target; `type` and `value` are deliberately not enums."""

    type: str = Field(default="unknown", min_length=1, max_length=120)
    value: str = Field(default="", max_length=1000)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ReferentResolution(StrictSemanticModel):
    surface: str = Field(default="", max_length=300)
    resolved: str = Field(default="", max_length=600)
    status: ReferentStatus = "not_applicable"
    candidates: list[str] = Field(default_factory=list, max_length=8)


class SemanticCondition(StrictSemanticModel):
    """A condition stated by the user, without deciding whether it authorizes execution."""

    kind: str = Field(default="if", min_length=1, max_length=80)
    expression: str = Field(min_length=1, max_length=800)
    source_quote: SourceQuote


class SemanticAct(StrictSemanticModel):
    """One independently meaningful act in a turn.

    `kind`, `goal`, `operation`, target types, and evidence needs remain
    open-world strings.  Adding a new user goal must not require deploying a
    larger enum.  This object intentionally has no route, risk, capability
    grant, state effect, or write authorization field.
    """

    act_id: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1200)
    operation: str = Field(default="unknown", min_length=1, max_length=200)
    target: SemanticTarget = Field(default_factory=SemanticTarget)
    polarity: str = Field(default="positive", min_length=1, max_length=80)
    explicitness: Explicitness = "unknown"
    source_quote: SourceQuote
    speaker: str = Field(default="user", min_length=1, max_length=160)
    authority: str = Field(default="direct_user", min_length=1, max_length=120)
    mention_mode: MentionMode = "normal_use"
    evidence_need: str = Field(default="unknown", min_length=1, max_length=160)
    referent: ReferentResolution = Field(default_factory=ReferentResolution)
    condition: SemanticCondition | None = None
    modality: str = Field(default="asserted", min_length=1, max_length=120)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_embedded_authorization(self) -> Self:
        reserved = {
            "allowed_capabilities",
            "allowed_effects",
            "capability_grant",
            "memory_policy",
            "risk",
            "risk_level",
            "route",
            "state_effect",
        }
        embedded = reserved.intersection(self.arguments) | reserved.intersection(self.target.attributes)
        if embedded:
            raise ValueError(f"semantic acts cannot carry authorization fields: {sorted(embedded)}")
        return self


class DiscourseRelation(StrictSemanticModel):
    relation_id: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=120)
    from_act_id: str = Field(min_length=1, max_length=80)
    to_act_id: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    source_quote: SourceQuote | None = None


class SemanticAmbiguity(StrictSemanticModel):
    ambiguity_id: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=800)
    affected_act_ids: list[str] = Field(default_factory=list, max_length=12)
    candidates: list[str] = Field(default_factory=list, max_length=12)


class TurnSemanticFrame(StrictSemanticModel):
    schema_version: Literal["veyra.semantic_frame.v1"] = SCHEMA_VERSION
    acts: list[SemanticAct] = Field(default_factory=list, max_length=24)
    relations: list[DiscourseRelation] = Field(default_factory=list, max_length=24)
    ambiguities: list[SemanticAmbiguity] = Field(default_factory=list, max_length=16)
    resolver_status: ResolverStatus
    source: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        act_ids = [act.act_id for act in self.acts]
        if len(act_ids) != len(set(act_ids)):
            raise ValueError("semantic act ids must be unique")
        known = set(act_ids)
        for relation in self.relations:
            if relation.from_act_id not in known or relation.to_act_id not in known:
                raise ValueError("semantic relation references an unknown act id")
        for ambiguity in self.ambiguities:
            if any(act_id not in known for act_id in ambiguity.affected_act_ids):
                raise ValueError("semantic ambiguity references an unknown act id")
        if self.resolver_status == "ambiguous" and not self.ambiguities:
            raise ValueError("ambiguous resolver status requires at least one ambiguity")
        return self

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, source_text: str = "") -> Self:
        """Validate an exact frame and bind every act to the source text."""
        frame_payload = _extract_frame_payload(payload)
        frame = cls.model_validate(frame_payload, strict=True)
        if source_text:
            _validate_source_binding(frame, source_text)
        return frame

    @classmethod
    def from_model_payload(cls, payload: dict[str, Any], *, source_text: str = "") -> Self:
        """Validate an exact model frame and bind every act to the source text.

        This method does not coerce legacy shapes.  Callers that need a
        fail-closed result should use :meth:`safe_from_model_payload`.
        """

        frame = cls.from_payload(payload, source_text=source_text)
        if frame.source not in {"model", "model_repair"}:
            raise ValueError("model semantic frame must declare source=model or model_repair")
        return frame

    @classmethod
    def safe_from_model_payload(cls, payload: dict[str, Any], *, source_text: str) -> Self:
        """Return a degraded, non-authorizing fallback on any model defect."""

        try:
            return cls.from_model_payload(payload, source_text=source_text)
        except (TypeError, ValueError, ValidationError):
            repaired = _repair_model_source_quotes(payload, source_text)
            if repaired is not None:
                try:
                    return cls.from_model_payload(repaired, source_text=source_text)
                except (TypeError, ValueError, ValidationError):
                    pass
            return cls.fallback(source_text, resolver_status="invalid_output")

    @classmethod
    def fallback(
        cls,
        text: str,
        *,
        resolver_status: Literal["degraded", "invalid_output"] = "degraded",
    ) -> Self:
        return _FallbackSemanticResolver(text).build(resolver_status=resolver_status)

    def compact(self) -> dict[str, Any]:
        """Small trace/context representation with the evidence-bearing fields."""

        return {
            "schema_version": self.schema_version,
            "resolver_status": self.resolver_status,
            "source": self.source,
            "acts": [
                {
                    "act_id": act.act_id,
                    "kind": act.kind,
                    "goal": _clip(act.goal, 240),
                    "operation": act.operation,
                    "target": {
                        "type": act.target.type,
                        "value": _clip(act.target.value, 240),
                    },
                    "explicitness": act.explicitness,
                    "polarity": act.polarity,
                    "source_quote": {
                        "text": _clip(act.source_quote.text, 240),
                        "start": act.source_quote.start,
                        "end": act.source_quote.end,
                    },
                    "speaker": act.speaker,
                    "authority": act.authority,
                    "mention_mode": act.mention_mode,
                    "evidence_need": act.evidence_need,
                    "modality": act.modality,
                    "arguments": act.arguments,
                    "referent": act.referent.model_dump(mode="json"),
                    **(
                        {"condition": act.condition.model_dump(mode="json")}
                        if act.condition is not None
                        else {}
                    ),
                }
                for act in self.acts[:8]
            ],
            "relations": [
                {
                    "kind": relation.kind,
                    "from_act_id": relation.from_act_id,
                    "to_act_id": relation.to_act_id,
                }
                for relation in self.relations[:8]
            ],
            "ambiguities": [
                {
                    "kind": ambiguity.kind,
                    "description": _clip(ambiguity.description, 240),
                    "affected_act_ids": ambiguity.affected_act_ids[:6],
                }
                for ambiguity in self.ambiguities[:6]
            ],
        }


def build_semantic_frame(
    *,
    text: str,
    model_payload: dict[str, Any] | None = None,
) -> TurnSemanticFrame:
    """Public resolver adapter used by the understanding layer.

    A present but malformed model payload is distinguishable from an unavailable
    model (`invalid_output` versus `degraded`).  Both remain fail-closed inputs
    for later policy compilation.
    """

    if model_payload is None:
        return TurnSemanticFrame.fallback(text)
    return TurnSemanticFrame.safe_from_model_payload(model_payload, source_text=text)


def semantic_frame_json_schema() -> dict[str, Any]:
    """Schema suitable for structured-output capable model transports."""

    return TurnSemanticFrame.model_json_schema()


def semantic_frame_quality_issues(frame: TurnSemanticFrame, source_text: str) -> list[str]:
    """Return conservative completeness defects without inferring authorization.

    Schema validation proves that a frame is well formed, but it cannot prove
    that the resolver kept independently meaningful clauses separate.  These
    checks only reject or request another semantic pass; they never create acts,
    choose a route, or grant an effect.
    """

    text = (source_text or "").strip()
    if text and not frame.acts:
        return ["non_empty_message_has_no_semantic_act"]

    issues: list[str] = []
    relation_kinds = {_semantic_token(relation.kind) for relation in frame.relations}
    for requirement in _hard_discourse_requirements(text):
        cue = str(requirement["cue"])
        left = requirement["left"]
        right = requirement["right"]
        left_ids = _locally_anchored_act_ids(frame, int(left[0]), int(left[1]), text)
        right_ids = _locally_anchored_act_ids(frame, int(right[0]), int(right[1]), text)
        if not left_ids:
            issues.append(f"{cue}_missing_local_act:left")
        if not right_ids:
            issues.append(f"{cue}_missing_local_act:right")
        if left_ids and right_ids and not any(left_id != right_id for left_id in left_ids for right_id in right_ids):
            issues.append(f"{cue}_collapsed_hard_boundary")
        expected_relations = set(requirement["relations"])
        if left_ids and right_ids and relation_kinds.isdisjoint(expected_relations):
            issues.append(f"{cue}_missing_discourse_relation")

    unquoted_text = _mask_quoted_content(text)
    lowered = unquoted_text.lower()
    conditional_surface = bool(
        re.search(
            r"(?:如果|假如|若|一旦).+?(?:再|就|才)|(?:等(?:到)?|待).+?(?:后|之后)?(?:再|就|才)",
            unquoted_text,
            flags=re.DOTALL,
        )
        or re.search(r"\bif\b.+?(?:\bthen\b|[,;])", lowered, flags=re.DOTALL)
        or re.search(r"\bwhen\b.+?[,;]\s*\S+", lowered, flags=re.DOTALL)
        or re.search(r"\b(?:after|upon)\b.+?\b(?:approve|approval|confirm|confirmation)\b", lowered)
    )
    if conditional_surface:
        has_condition = any(act.condition is not None for act in frame.acts) or bool(
            relation_kinds & {"condition", "conditional", "precondition"}
        )
        if not has_condition:
            issues.append("conditional_surface_missing_condition_structure")
    if any(
        _semantic_token(act.modality)
        in {"conditional", "contingent", "pending_approval", "pending_confirmation"}
        and act.condition is None
        for act in frame.acts
    ):
        issues.append("conditional_modality_missing_condition_structure")

    reported_surface = bool(
        re.search(r"(?:他说|她说|他们说|同事说|对方说).*[“\"'].*[”\"']", text, flags=re.DOTALL)
        or re.search(
            r"(?:老板|领导|同事|客户|对方|他|她|他们)\s*(?:说|提到|表示|要求|让)",
            unquoted_text,
            flags=re.DOTALL,
        )
        or re.search(r"\b(?:he|she|they|someone|my colleague)\s+said\b", lowered)
        or re.search(r"\b(?:boss|manager|client|colleague)\s+(?:said|asked|told|requested)\b", lowered)
    )
    if reported_surface and all(
        _semantic_token(act.mention_mode) not in {"reported_speech", "quoted_term"}
        and not _semantic_token(act.authority).startswith("reported")
        and _semantic_token(act.kind) not in {"reported", "reported_speech", "quotation"}
        and _semantic_token(act.modality) not in {"reported", "quoted"}
        for act in frame.acts
    ):
        issues.append("reported_speech_surface_missing_authority_boundary")

    negative_surface = _contains_prohibition(unquoted_text) or bool(
        re.search(r"\bmust not\b", lowered)
    )
    has_negative_act = any(
        _semantic_token(act.polarity) in {"negative", "negated", "prohibit", "prohibited"}
        or _semantic_token(act.kind) in {"deny", "denial", "prohibit", "prohibition", "negative_constraint"}
        for act in frame.acts
    )
    # “别再跟了 / don't keep tracking it” is a positive request to stop an
    # existing commitment, not a denial of the cancellation operation itself.
    # Only this narrow state-control family may satisfy a negative surface
    # without a separate negative act; write/delete/execute operations cannot.
    has_lexicalized_cessation = any(
        _semantic_token(act.kind)
        in {"commitment", "commitment_control", "goal_control", "schedule_control"}
        and any(
            marker in _semantic_token(act.operation)
            for marker in (
                "cancel",
                "discontinue",
                "pause",
                "stop_tracking",
                "unsubscribe",
            )
        )
        for act in frame.acts
    )
    if negative_surface and frame.acts and not has_negative_act and not has_lexicalized_cessation:
        issues.append("explicit_negative_surface_missing_negative_act")

    ambiguous_referents = {
        act.act_id
        for act in frame.acts
        if _semantic_token(act.referent.status) in {"ambiguous", "unresolved"}
    }
    ambiguity_coverage = {
        act_id
        for ambiguity in frame.ambiguities
        for act_id in ambiguity.affected_act_ids
    }
    if ambiguous_referents and (
        frame.resolver_status != "ambiguous"
        or not ambiguous_referents.issubset(ambiguity_coverage)
    ):
        issues.append("unresolved_referent_missing_ambiguity_structure")

    return list(dict.fromkeys(issues))


def _semantic_token(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value or "").strip().lower())


def _hard_discourse_requirements(source_text: str) -> list[dict[str, Any]]:
    """Find only high-confidence discourse boundaries outside quotations."""

    masked = _mask_quoted_content(source_text)
    patterns: tuple[tuple[str, re.Pattern[str], set[str]], ...] = (
        (
            "negative_positive_contrast",
            re.compile(
                r"(?P<left>(?:不要|别|不用|无需|请勿|不准|先别).+?)[，,；;]\s*(?P<right>(?:只|请只|只是|但|但是|而是).+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"contrast", "correction", "restriction"},
        ),
        (
            "negative_positive_contrast",
            re.compile(
                r"(?P<left>(?:do not|don't|dont|please don't|never)\b.+?)[,;]\s*(?P<right>(?:only|just|instead|but)\b.+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"contrast", "correction", "restriction"},
        ),
        (
            "explicit_correction",
            re.compile(
                r"(?P<left>(?:不是|并非).+?)[，,；;]\s*(?P<right>(?:而是|是要|是在|只是).+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"correction", "contrast"},
        ),
        (
            "explicit_correction",
            re.compile(
                r"(?P<left>\bnot\b.+?)[,;]\s*(?P<right>(?:but|instead)\b.+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"correction", "contrast"},
        ),
        (
            "ordered_multi_act",
            re.compile(
                r"(?P<left>(?:先|首先).+?)[，,；;]\s*(?P<right>(?:再|然后|接着).+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"sequence", "ordered_sequence"},
        ),
        (
            "ordered_multi_act",
            re.compile(
                r"(?P<left>\bfirst\b.+?)[,;]\s*(?P<right>(?:then|after that|next)\b.+)",
                re.IGNORECASE | re.DOTALL,
            ),
            {"sequence", "ordered_sequence"},
        ),
    )
    requirements: list[dict[str, Any]] = []
    for cue, pattern, relations in patterns:
        match = pattern.search(masked)
        if match is None:
            continue
        requirements.append(
            {
                "cue": cue,
                "left": match.span("left"),
                "right": match.span("right"),
                "relations": relations,
            }
        )
        break

    quote_pattern = re.compile(r"“[^”]+”|\"[^\"]+\"|「[^」]+」|『[^』]+』|`[^`]+`")
    for quote_match in quote_pattern.finditer(source_text):
        prefix = source_text[: quote_match.start()]
        if re.search(
            r"(?:[\w\u4e00-\u9fff]{1,20}?)(?:说|提到|表示)\s*$|\b(?:said|asked|mentioned)\s*$",
            prefix,
            re.IGNORECASE,
        ) is None:
            continue
        right_start, right_end = _trimmed_bounds(source_text, quote_match.end(), len(source_text))
        while right_start < right_end and source_text[right_start] in "，,；;：:":
            right_start += 1
            right_start, right_end = _trimmed_bounds(source_text, right_start, right_end)
        if right_start >= right_end:
            continue
        requirements.append(
            {
                "cue": "reported_speech_boundary",
                "left": _trimmed_bounds(source_text, 0, quote_match.end()),
                "right": (right_start, right_end),
                "relations": {"quotation", "reported_speech", "constraint"},
            }
        )
        break
    return requirements


def _mask_quoted_content(source_text: str) -> str:
    masked = list(source_text)
    for match in re.finditer(r"“[^”]*”|\"[^\"]*\"|「[^」]*」|『[^』]*』|`[^`]*`", source_text):
        for index in range(match.start(), match.end()):
            masked[index] = " "
    return "".join(masked)


def _locally_anchored_act_ids(
    frame: TurnSemanticFrame,
    unit_start: int,
    unit_end: int,
    source_text: str,
) -> set[str]:
    unit_start, unit_end = _trimmed_bounds(source_text, unit_start, unit_end)
    unit_size = len(re.sub(r"[\s，,。.!！?？；;：:]+", "", source_text[unit_start:unit_end]))
    minimum_size = max(2, int(unit_size * 0.35))
    anchored: set[str] = set()
    for act in frame.acts:
        quote = act.source_quote
        if quote.start < unit_start or quote.end > unit_end:
            continue
        quote_size = len(re.sub(r"[\s，,。.!！?？；;：:]+", "", quote.text))
        if quote_size >= minimum_size:
            anchored.add(act.act_id)
    return anchored


def _extract_frame_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("semantic frame payload must be an object")
    if isinstance(payload.get("turn_semantic_frame"), dict):
        return payload["turn_semantic_frame"]
    if isinstance(payload.get("semantic_frame"), dict):
        return payload["semantic_frame"]
    understanding = payload.get("turn_understanding")
    if isinstance(understanding, dict):
        if isinstance(understanding.get("turn_semantic_frame"), dict):
            return understanding["turn_semantic_frame"]
        if isinstance(understanding.get("semantic_frame"), dict):
            return understanding["semantic_frame"]
    situation = payload.get("situation_assessment")
    if isinstance(situation, dict):
        if isinstance(situation.get("turn_semantic_frame"), dict):
            return situation["turn_semantic_frame"]
        if isinstance(situation.get("semantic_frame"), dict):
            return situation["semantic_frame"]
    if isinstance(payload.get("acts"), list):
        return payload
    raise ValueError("model payload does not contain a semantic frame")


def _validate_source_binding(frame: TurnSemanticFrame, source_text: str) -> None:
    for act in frame.acts:
        _validate_quote(act.source_quote, source_text)
        if act.condition is not None:
            _validate_quote(act.condition.source_quote, source_text)
    for relation in frame.relations:
        if relation.source_quote is not None:
            _validate_quote(relation.source_quote, source_text)


def _validate_quote(quote: SourceQuote, source_text: str) -> None:
    if quote.end > len(source_text) or source_text[quote.start : quote.end] != quote.text:
        raise ValueError("semantic source_quote is not an exact slice of the user message")


def _repair_model_source_quotes(payload: dict[str, Any], source_text: str) -> dict[str, Any] | None:
    """Repair only source spans, never semantic meaning or authorization fields.

    Some OpenAI-compatible models preserve the quote's characters but normalize
    whitespace or return byte-like offsets.  Exact traceability is recovered
    locally when the quoted text has one unambiguous match.
    """

    try:
        frame = copy.deepcopy(_extract_frame_payload(payload))
    except (TypeError, ValueError):
        return None
    frame["source"] = "model_repair"
    repaired_any = False

    def repair(value: Any) -> bool:
        nonlocal repaired_any
        if isinstance(value, list):
            return all(repair(item) for item in value)
        if not isinstance(value, dict):
            return True
        if {"text", "start", "end"}.issubset(value):
            quote_text = str(value.get("text") or "")
            span = _unique_source_span(source_text, quote_text)
            if span is None:
                return False
            start, end = span
            exact = source_text[start:end]
            if value.get("start") != start or value.get("end") != end or quote_text != exact:
                value.update({"text": exact, "start": start, "end": end})
                repaired_any = True
            return True
        return all(repair(child) for child in value.values())

    if not repair(frame):
        return None
    return frame if repaired_any else None


def _unique_source_span(source_text: str, quote_text: str) -> tuple[int, int] | None:
    if not quote_text:
        return None
    exact_matches = [match.start() for match in re.finditer(re.escape(quote_text), source_text)]
    if len(exact_matches) == 1:
        start = exact_matches[0]
        return start, start + len(quote_text)
    compact_quote = re.sub(r"\s+", "", quote_text)
    if not compact_quote:
        return None
    compact_chars: list[str] = []
    source_positions: list[int] = []
    for index, char in enumerate(source_text):
        if char.isspace():
            continue
        compact_chars.append(char)
        source_positions.append(index)
    compact_source = "".join(compact_chars)
    compact_matches = [match.start() for match in re.finditer(re.escape(compact_quote), compact_source)]
    if len(compact_matches) != 1:
        return None
    compact_start = compact_matches[0]
    compact_end = compact_start + len(compact_quote)
    start = source_positions[compact_start]
    end = source_positions[compact_end - 1] + 1
    return start, end


class _FallbackSemanticResolver:
    """Conservative linguistic fallback.

    It preserves semantic contrasts, quotation/mention, corrections, and
    conditions needed for safe degradation.  It never grants a route or state
    effect, and every fallback frame is marked degraded/invalid_output.
    """

    _QUOTE_PATTERN = re.compile(r"“([^”]+)”|\"([^\"]+)\"|「([^」]+)」|『([^』]+)』|`([^`]+)`")

    def __init__(self, text: str) -> None:
        self.text = text or ""
        self.acts: list[SemanticAct] = []
        self.relations: list[DiscourseRelation] = []
        self.ambiguities: list[SemanticAmbiguity] = []

    def build(self, *, resolver_status: Literal["degraded", "invalid_output"]) -> TurnSemanticFrame:
        if not self.text.strip():
            self.ambiguities.append(
                SemanticAmbiguity(
                    ambiguity_id="u1",
                    kind="empty_input",
                    description="No user utterance was available to resolve.",
                    affected_act_ids=[],
                    candidates=[],
                )
            )
            return TurnSemanticFrame(
                acts=[],
                relations=[],
                ambiguities=self.ambiguities,
                resolver_status=resolver_status,
                source="fallback",
            )

        if len(self.text) > _SOURCE_QUOTE_LIMIT:
            return self._long_input_frame(resolver_status)

        if self._parse_explained_quoted_term():
            return self._frame(resolver_status)
        if self._parse_reported_speech():
            return self._frame(resolver_status)
        if self._parse_correction():
            return self._frame(resolver_status)
        if self._parse_contrast():
            return self._frame(resolver_status)
        if self._parse_multi_operation():
            return self._frame(resolver_status)
        if self._parse_preference_transition():
            return self._frame(resolver_status)
        if self._parse_condition():
            return self._frame(resolver_status)
        if self._parse_plain_quote_mention():
            return self._frame(resolver_status)

        start, end = _trimmed_bounds(self.text, 0, len(self.text))
        self._append_clause(self.text[start:end], start=start)
        return self._frame(resolver_status)

    def _long_input_frame(
        self,
        resolver_status: Literal["degraded", "invalid_output"],
    ) -> TurnSemanticFrame:
        """Represent an over-limit fallback input without guessing its meaning."""

        quote_end = min(len(self.text), _SOURCE_QUOTE_LIMIT)
        unresolved = self._act(
            kind="task",
            goal="resolve the complete long user message before acting",
            operation="resolve_long_input",
            target=SemanticTarget(
                type="input_segment",
                value="long_input",
                attributes={
                    "captured_end": quote_end,
                    "total_length": len(self.text),
                },
            ),
            quote=_source_quote(self.text, 0, quote_end),
            explicitness="unknown",
            evidence_need="context",
        )
        self.acts.append(unresolved)
        self.ambiguities.append(
            SemanticAmbiguity(
                ambiguity_id="u1",
                kind="fallback_input_truncated",
                description=(
                    "Fallback parsing cannot safely resolve the complete long "
                    "message within the source quote limit."
                ),
                affected_act_ids=[unresolved.act_id],
                candidates=[],
            )
        )
        return self._frame(resolver_status)

    def _frame(self, resolver_status: Literal["degraded", "invalid_output"]) -> TurnSemanticFrame:
        return TurnSemanticFrame(
            acts=self.acts,
            relations=self.relations,
            ambiguities=self.ambiguities,
            resolver_status=resolver_status,
            source="fallback",
        )

    def _parse_explained_quoted_term(self) -> bool:
        match = self._QUOTE_PATTERN.search(self.text)
        if match is None:
            return False
        prefix = self.text[: match.start()]
        suffix = self.text[match.end() :]
        if not (
            any(marker in prefix.lower() for marker in ("解释", "说明", "meaning of", "define"))
            and any(marker in suffix.lower() for marker in ("词", "含义", "意思", "term", "word", "mean"))
        ):
            return False
        quoted = next(group for group in match.groups() if group is not None)
        denial_match = re.search(
            r"[，,；;]\s*(?P<denial>(?:不要|别|不用|无需|请勿|do not|don't|never).+)",
            self.text[match.end() :],
            re.IGNORECASE,
        )
        explanation_end = match.end() + denial_match.start() if denial_match is not None else len(self.text)
        start, explanation_end = _trimmed_bounds(self.text, 0, explanation_end)
        request = self._act(
            kind="request",
            goal=self.text[start:explanation_end],
            operation="explain_term",
            target=SemanticTarget(type="term", value=quoted),
            quote=_source_quote(self.text, start, explanation_end),
            explicitness="explicit",
            mention_mode="quoted_term",
            evidence_need="none",
        )
        self.acts.append(request)
        if denial_match is not None:
            denial_start = match.end() + denial_match.start("denial")
            denial_end = match.end() + denial_match.end("denial")
            denied = self._append_clause(
                self.text[denial_start:denial_end],
                start=denial_start,
                force_prohibition=True,
            )
            if denied is not None:
                self.relations.append(
                    self._relation(
                        kind="constraint",
                        from_act_id=denied.act_id,
                        to_act_id=request.act_id,
                        start=start,
                        end=denial_end,
                        description="The direct prohibition constrains how the quoted term may be explained.",
                    )
                )
        return True

    def _parse_reported_speech(self) -> bool:
        match = self._QUOTE_PATTERN.search(self.text)
        if match is None:
            return False
        prefix = self.text[: match.start()]
        speaker_match = re.search(r"([\w\u4e00-\u9fff]{1,20}?)(?:说|提到|表示|asked|said)\s*$", prefix, re.IGNORECASE)
        if speaker_match is None:
            return False
        quoted = next(group for group in match.groups() if group is not None)
        reported_start, reported_end = _trimmed_bounds(self.text, 0, match.end())
        reported = self._act(
            kind="reported_speech",
            goal=f"report that {speaker_match.group(1)} said: {quoted}",
            operation="report_statement",
            target=SemanticTarget(type="quoted_utterance", value=quoted),
            quote=_source_quote(self.text, reported_start, reported_end),
            explicitness="explicit",
            speaker=speaker_match.group(1),
            authority="reported_speech",
            mention_mode="reported_speech",
            evidence_need="none",
        )
        self.acts.append(reported)

        tail_start, tail_end = _trimmed_bounds(self.text, match.end(), len(self.text))
        if tail_start < tail_end:
            tail = self.text[tail_start:tail_end]
            denial_match = re.search(
                r"[，,；;]\s*(?P<denial>(?:不要|别|不用|无需|请勿|do not|don't|never).+)",
                tail,
                re.IGNORECASE,
            )
            direct_end = tail_end if denial_match is None else tail_start + denial_match.start()
            direct = None
            if denial_match is None or self.text[tail_start:direct_end].strip(" ，,；;"):
                direct = self._append_clause(
                    self.text[tail_start:direct_end],
                    start=tail_start,
                    inherited_target=quoted,
                    force_prohibition=denial_match is None and _contains_prohibition(tail),
                )
            denied = None
            if denial_match is not None:
                denial_start = tail_start + denial_match.start("denial")
                denial_end = tail_start + denial_match.end("denial")
                denied = self._append_clause(
                    self.text[denial_start:denial_end],
                    start=denial_start,
                    inherited_target=quoted,
                    force_prohibition=True,
                )
            if direct is not None:
                self.relations.append(
                    self._relation(
                        kind="constraint" if direct.kind == "prohibition" else "quotation",
                        from_act_id=direct.act_id if direct.kind == "prohibition" else reported.act_id,
                        to_act_id=reported.act_id if direct.kind == "prohibition" else direct.act_id,
                        start=reported_start,
                        end=direct_end,
                        description="Reported words do not grant the reported speaker authority over Veyra.",
                    )
                )
            if denied is not None:
                self.relations.append(
                    self._relation(
                        kind="constraint",
                        from_act_id=denied.act_id,
                        to_act_id=reported.act_id,
                        start=reported_start,
                        end=denial_end,
                        description="The current user's prohibition blocks execution of the reported words.",
                    )
                )
        return True

    def _parse_correction(self) -> bool:
        patterns = (
            re.compile(r"不是(?:让|要|叫)你(?P<old>.+?)[，,；;]\s*(?:我)?(?:是|只是在|是在|而是)(?P<new>.+)", re.IGNORECASE),
            re.compile(r"(?:not asking you to|did not ask you to)\s+(?P<old>.+?)[,;]\s*(?:i am|i'm)\s+(?P<new>.+)", re.IGNORECASE),
        )
        match = next((item.search(self.text) for item in patterns if item.search(self.text)), None)
        if match is None:
            return False
        old_start, old_end = match.span("old")
        new_start, new_end = match.span("new")
        old = self._append_clause(
            self.text[old_start:old_end],
            start=old_start,
            force_prohibition=True,
            goal_prefix="reject the interpretation: ",
        )
        new = self._append_clause(
            self.text[new_start:new_end],
            start=new_start,
            inherited_target=old.target.value if old is not None else "",
        )
        if old is not None and new is not None:
            self.relations.append(
                self._relation(
                    kind="correction",
                    from_act_id=old.act_id,
                    to_act_id=new.act_id,
                    start=match.start(),
                    end=match.end(),
                    description="The later act replaces the rejected interpretation.",
                )
            )
        return True

    def _parse_contrast(self) -> bool:
        patterns = (
            re.compile(
                r"(?P<left>(?:不要|别|不用|无需|请勿|不准|先别).+?)[，,；;]\s*(?P<right>(?:只|而是|但是|但|请只).+)",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?P<left>(?:do not|don't|dont|please don't|never)\b.+?)[,;]\s*(?P<right>(?:only|just|instead|but).+)",
                re.IGNORECASE,
            ),
        )
        match = next((item.search(self.text) for item in patterns if item.search(self.text)), None)
        reverse = False
        if match is None:
            reverse_patterns = (
                re.compile(
                    r"(?P<right>(?:只|只是|仅|请只).+?)[，,；;]\s*(?P<left>.+?(?:别动|不要动|不要改|不要修改|别改|别执行))",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(?P<right>(?:only|just).+?)[,;]\s*(?P<left>.+?(?:do not|don't|dont|never).+)",
                    re.IGNORECASE,
                ),
            )
            match = next((item.search(self.text) for item in reverse_patterns if item.search(self.text)), None)
            reverse = match is not None
        if match is None:
            return False
        left_start, left_end = match.span("left")
        right_start, right_end = match.span("right")
        left = self._append_clause(self.text[left_start:left_end], start=left_start, force_prohibition=True)
        right = self._append_clause(self.text[right_start:right_end], start=right_start)
        if left is not None and right is not None:
            self.relations.append(
                self._relation(
                    kind="contrast",
                    from_act_id=left.act_id,
                    to_act_id=right.act_id,
                    start=match.start(),
                    end=match.end(),
                    description=(
                        "The requested explanation precedes but does not override the later prohibition."
                        if reverse
                        else "The requested alternative contrasts with the prohibited act."
                    ),
                )
            )
        return True

    def _parse_condition(self) -> bool:
        patterns = (
            re.compile(r"(?:如果|假如|若)(?P<condition>.+?)(?:[，,]\s*)?(?:再|就|才)(?P<action>.+)", re.IGNORECASE),
            re.compile(r"\bif\s+(?P<condition>.+?)[,;]\s*(?:then\s+)?(?P<action>.+)", re.IGNORECASE),
        )
        match = next((item.search(self.text) for item in patterns if item.search(self.text)), None)
        if match is None:
            return False
        condition_start, condition_end = match.span("condition")
        action_start, action_end = match.span("action")
        act = self._append_clause(self.text[action_start:action_end], start=action_start)
        if act is None:
            return False
        condition_start, condition_end = _trimmed_bounds(self.text, condition_start, condition_end)
        act.condition = SemanticCondition(
            kind="if",
            expression=self.text[condition_start:condition_end],
            source_quote=_source_quote(self.text, condition_start, condition_end),
        )
        return True

    def _parse_multi_operation(self) -> bool:
        patterns = (
            re.compile(
                r"(?P<cancel>(?:取消|停止|终止)\s*(?P<old>.+?))[，,；;]\s*(?P<switch>(?:改成|换成|转为|改为)\s*(?P<new>.+))",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?P<cancel>(?:cancel|stop|terminate)\s+(?P<old>.+?))[,;]\s*(?P<switch>(?:switch|change)\s+to\s+(?P<new>.+))",
                re.IGNORECASE,
            ),
        )
        match = next((item.search(self.text) for item in patterns if item.search(self.text)), None)
        if match is None:
            return False
        cancel_start, cancel_end = match.span("cancel")
        switch_start, switch_end = match.span("switch")
        old = self.text[match.start("old") : match.end("old")].strip()
        new = self.text[match.start("new") : match.end("new")].strip(" ，,。.!！?？")
        cancel = self._act(
            kind="commitment_control",
            goal=f"cancel {old}",
            operation="cancel_task",
            target=SemanticTarget(type="task", value=old or "unknown"),
            quote=_source_quote(self.text, cancel_start, cancel_end),
            explicitness="explicit",
            evidence_need="local_state",
        )
        self.acts.append(cancel)
        switch = self._act(
            kind="preference_update",
            goal=f"switch from {old} to {new}",
            operation="change_preference",
            target=SemanticTarget(
                type="preference_transition",
                value=new,
                attributes={"from": old, "to": new},
            ),
            quote=_source_quote(self.text, switch_start, switch_end),
            explicitness="explicit",
            evidence_need="none",
        )
        self.acts.append(switch)
        self.relations.append(
            self._relation(
                kind="sequence",
                from_act_id=cancel.act_id,
                to_act_id=switch.act_id,
                start=match.start(),
                end=match.end(),
                description="The user expressed two ordered state-change candidates.",
            )
        )
        return True

    def _parse_preference_transition(self) -> bool:
        patterns = (
            re.compile(r"(?:我)?(?:准备|打算|想要)?从\s*(?P<old>.+?)\s*(?:转向|换到|改用)\s*(?P<new>.+)", re.IGNORECASE),
            re.compile(r"(?:i(?:'m| am)?\s+)?(?:plan|intend|want)?\s*to\s+switch\s+from\s+(?P<old>.+?)\s+to\s+(?P<new>.+)", re.IGNORECASE),
        )
        match = next((item.search(self.text) for item in patterns if item.search(self.text)), None)
        if match is None:
            return False
        start, end = _trimmed_bounds(self.text, match.start(), match.end())
        old = self.text[match.start("old") : match.end("old")].strip()
        new = self.text[match.start("new") : match.end("new")].strip(" ，,。.!！?？")
        self.acts.append(
            self._act(
                kind="preference_update",
                goal=f"switch preference from {old} to {new}",
                operation="change_preference",
                target=SemanticTarget(
                    type="preference_transition",
                    value=new,
                    attributes={"from": old, "to": new},
                ),
                quote=_source_quote(self.text, start, end),
                explicitness="strong_implied",
                evidence_need="none",
            )
        )
        return True

    def _parse_plain_quote_mention(self) -> bool:
        match = self._QUOTE_PATTERN.search(self.text)
        if match is None:
            return False
        lowered = self.text.lower()
        if not (
            any(marker in self.text for marker in ("这个词", "这句话", "这段话", "原话", "引用", "措辞", "说法"))
            or any(marker in lowered for marker in ("the word", "the term", "this phrase", "this quote", "wording"))
        ):
            return False
        quoted = next(group for group in match.groups() if group is not None)
        start, end = _trimmed_bounds(self.text, 0, len(self.text))
        self.acts.append(
            self._act(
                kind="mention",
                goal=self.text[start:end],
                operation="discuss_quoted_content",
                target=SemanticTarget(type="quoted_content", value=quoted),
                quote=_source_quote(self.text, start, end),
                explicitness="explicit" if _looks_like_question(self.text) else "strong_implied",
                authority="quoted_text",
                mention_mode="quoted_term",
                evidence_need="none",
            )
        )
        return True

    def _append_clause(
        self,
        clause: str,
        *,
        start: int,
        inherited_target: str = "",
        force_prohibition: bool = False,
        goal_prefix: str = "",
    ) -> SemanticAct | None:
        local_start, local_end = _trimmed_bounds(clause, 0, len(clause))
        while local_start < local_end and clause[local_start] in "，,；;":
            local_start += 1
            local_start, local_end = _trimmed_bounds(clause, local_start, local_end)
        if local_start >= local_end:
            return None
        absolute_start = start + local_start
        absolute_end = start + local_end
        raw = self.text[absolute_start:absolute_end]
        semantic = _strip_discourse_prefix(raw)
        prohibition = force_prohibition or _contains_prohibition(raw)
        operation, target, evidence_need = _operation_target_evidence(semantic, inherited_target=inherited_target)
        if prohibition:
            kind = "prohibition"
        elif _looks_like_question(semantic):
            kind = "question"
        elif _looks_like_request(semantic):
            kind = "request"
        else:
            kind = "statement"

        explicitness: Explicitness
        if prohibition or kind in {"question", "request"}:
            explicitness = "explicit"
        elif _looks_hypothetical(semantic):
            explicitness = "weak_implied"
        else:
            explicitness = "inferred"

        referent = _referent_from(semantic)
        act = self._act(
            kind=kind,
            goal=f"{goal_prefix}{semantic}".strip(),
            operation=operation,
            target=target,
            quote=_source_quote(self.text, absolute_start, absolute_end),
            explicitness=explicitness,
            mention_mode="hypothetical" if _looks_hypothetical(semantic) else "normal_use",
            evidence_need=evidence_need,
            referent=referent,
        )
        self.acts.append(act)
        if operation == "create_recurring_notification":
            if not str(target.attributes.get("location") or "").strip():
                self.ambiguities.append(
                    SemanticAmbiguity(
                        ambiguity_id=f"u{len(self.ambiguities) + 1}",
                        kind="missing_location",
                        description="请补充要查询天气的城市或地区。",
                        affected_act_ids=[act.act_id],
                        candidates=[],
                    )
                )
            if not bool(re.search(r"(?:[01]?\d|2[0-3])(?:[:：点时][0-5]?\d?)", self.text)):
                self.ambiguities.append(
                    SemanticAmbiguity(
                        ambiguity_id=f"u{len(self.ambiguities) + 1}",
                        kind="missing_schedule_time",
                        description="请补充每天几点提醒。",
                        affected_act_ids=[act.act_id],
                        candidates=[],
                    )
                )
        if referent.status in {"unresolved", "ambiguous"}:
            self.ambiguities.append(
                SemanticAmbiguity(
                    ambiguity_id=f"u{len(self.ambiguities) + 1}",
                    kind="unresolved_referent",
                    description=f"The referent {referent.surface!r} cannot be resolved safely in fallback mode.",
                    affected_act_ids=[act.act_id],
                    candidates=referent.candidates,
                )
            )
        return act

    def _act(
        self,
        *,
        kind: str,
        goal: str,
        operation: str,
        target: SemanticTarget,
        quote: SourceQuote,
        explicitness: Explicitness,
        speaker: str = "user",
        authority: str = "direct_user",
        mention_mode: MentionMode = "normal_use",
        evidence_need: str = "unknown",
        referent: ReferentResolution | None = None,
    ) -> SemanticAct:
        return SemanticAct(
            act_id=f"a{len(self.acts) + 1}",
            kind=kind,
            goal=goal or quote.text,
            operation=operation,
            target=target,
            explicitness=explicitness,
            source_quote=quote,
            speaker=speaker,
            authority=authority,
            mention_mode=mention_mode,
            evidence_need=evidence_need,
            referent=referent or ReferentResolution(),
            condition=None,
            polarity="negative" if kind == "prohibition" else "positive",
            modality=(
                "hypothetical"
                if mention_mode == "hypothetical"
                else "reported"
                if mention_mode == "reported_speech"
                else "asserted"
            ),
            arguments={},
        )

    def _relation(
        self,
        *,
        kind: str,
        from_act_id: str,
        to_act_id: str,
        start: int,
        end: int,
        description: str,
    ) -> DiscourseRelation:
        start, end = _trimmed_bounds(self.text, start, end)
        return DiscourseRelation(
            relation_id=f"r{len(self.relations) + 1}",
            kind=kind,
            from_act_id=from_act_id,
            to_act_id=to_act_id,
            description=description,
            source_quote=_source_quote(self.text, start, end),
        )


def _operation_target_evidence(
    clause: str,
    *,
    inherited_target: str = "",
) -> tuple[str, SemanticTarget, str]:
    lowered = clause.lower()
    compact = re.sub(r"\s+", "", clause)

    if any(marker in compact for marker in ("你是谁", "你现在是", "你的身份")) or any(
        marker in lowered for marker in ("who are you", "what are you", "your identity")
    ):
        return "answer_identity", SemanticTarget(type="assistant_identity", value="Veyra"), "none"
    if _looks_like_question(clause) and (
        any(marker in compact for marker in ("追踪", "关注任务", "订阅任务", "提醒任务"))
        or any(marker in lowered for marker in ("tracking", "subscription task", "reminder task"))
    ):
        topic = _project_or_default(clause, "")
        if not topic:
            topic = _target_after_action(
                clause,
                ("取消", "停止", "终止", "追踪", "关注", "cancel", "stop", "tracking"),
            )
        return "query_task_status", SemanticTarget(type="task", value=topic or inherited_target or "unknown"), "local_state"
    if _looks_like_question(clause) and (
        any(marker in compact for marker in ("取消", "停止", "终止"))
        or any(marker in lowered for marker in ("cancel", "stop", "terminate"))
    ):
        return (
            "query_task_status",
            SemanticTarget(
                type="task",
                value=_project_or_default(clause, "")
                or inherited_target
                or _target_after_action(clause, ("取消", "停止", "终止", "cancel", "stop", "terminate"))
                or "unknown",
            ),
            "local_state",
        )
    runtime_unresponsive = (
        any(marker in compact for marker in ("没响应", "不响应", "没回复", "不回复", "无响应", "没反应"))
        or any(marker in lowered for marker in ("not responding", "no response", "unresponsive"))
    )
    runtime_named = any(marker in lowered for marker in ("openclaw", "hermes", "mcp", "runtime"))
    repair_requested = any(marker in compact for marker in ("修复", "解决", "恢复")) or any(
        marker in lowered for marker in ("fix", "repair", "resolve", "restore")
    )
    if runtime_unresponsive and runtime_named and repair_requested:
        return (
            "repair_runtime",
            SemanticTarget(type="runtime", value=_project_or_default(clause, "system")),
            "runtime_workspace",
        )
    if runtime_unresponsive and runtime_named:
        return (
            "diagnose_runtime",
            SemanticTarget(type="runtime", value=_project_or_default(clause, "system")),
            "runtime",
        )
    if (
        ("git" in lowered)
        and (
            any(marker in compact for marker in ("脏文件", "工作区", "未提交", "改动"))
            or any(marker in lowered for marker in ("dirty", "working tree", "uncommitted", "git status"))
        )
    ):
        return "query_git_status", SemanticTarget(type="git_workspace", value="git"), "fresh_local"
    if _looks_like_question(clause) and (
        any(marker in compact for marker in ("运行状态", "还在运行", "正常运行", "运行态", "进程", "端口"))
        or any(marker in lowered for marker in ("running", "runtime", "process", "port", "service status", "still alive"))
    ):
        return (
            "query_runtime_status",
            SemanticTarget(type="runtime", value=_project_or_default(clause, inherited_target or "system")),
            "runtime",
        )
    if _looks_like_question(clause) and (
        any(marker in compact for marker in ("最新", "新闻", "最近发布", "当前版本"))
        or any(marker in lowered for marker in ("latest", "news", "recent release", "current version"))
    ):
        return (
            "query_fresh_external_fact",
            SemanticTarget(type="external_fact", value=_clean_target(clause)),
            "fresh_external_search",
        )
    if (
        any(marker in compact for marker in ("帮我找", "搜索", "查找", "搜一下", "找一些"))
        or any(marker in lowered for marker in ("find me", "search for", "look up"))
    ) and (
        any(
            marker in compact
            for marker in (
                "秋招",
                "校招",
                "招聘",
                "岗位",
                "公司信息",
                "视频",
                "文章",
                "资料",
                "网页",
                "链接",
                "公开信息",
            )
        )
        or any(
            marker in lowered
            for marker in (
                "article",
                "campus hiring",
                "job openings",
                "public information",
                "recruiting",
                "recruitment",
                "video",
                "web page",
            )
        )
    ):
        return (
            "external_search",
            SemanticTarget(type="search_query", value=_clean_target(clause)),
            "fresh_external_search",
        )
    if _looks_like_question(clause) and (
        any(marker in compact for marker in ("几点", "现在时间", "当前时间", "今天几号", "日期"))
        or any(marker in lowered for marker in ("what time", "current time", "today's date", "what date"))
    ):
        return "query_current_time", SemanticTarget(type="time", value=_location_from(clause)), "fresh_time"
    if (
        any(marker in compact for marker in ("修改代码", "改代码", "写代码", "修复bug", "修复错误", "代码别动", "代码不要动", "别动代码"))
        or any(marker in lowered for marker in ("modify code", "edit code", "fix bug", "write code", "leave the code unchanged"))
    ):
        return "modify_code", SemanticTarget(type="codebase", value=_project_or_default(clause, "repository")), "workspace"
    if any(marker in compact for marker in ("加一个", "添加", "新增", "创建一个", "实现一个")) or any(
        marker in lowered for marker in ("add a", "add an", "create a", "introduce a")
    ):
        return "add_component", SemanticTarget(type="codebase", value=_project_or_default(clause, "repository")), "workspace"
    if any(marker in compact for marker in ("不要执行", "别执行", "执行", "运行操作")) or any(
        marker in lowered for marker in ("execute", "run the operation")
    ):
        return "execute", SemanticTarget(type="capability", value="execution"), "none"
    if any(marker in compact for marker in ("搜索", "联网查", "外部查询")) or any(
        marker in lowered for marker in ("search", "web lookup", "external lookup")
    ):
        return "external_search", SemanticTarget(type="capability", value="external_search"), "fresh_external_search"
    if any(marker in compact for marker in ("删除仓库", "删掉仓库", "仓库全删", "清空仓库")) or any(
        marker in lowered for marker in ("delete repository", "remove the repository", "wipe the repository")
    ):
        return "delete_repository", SemanticTarget(type="repository", value="current_repository"), "workspace"
    if any(marker in compact for marker in ("记住", "保存偏好", "写入记忆", "长期记忆")) or any(
        marker in lowered for marker in ("remember", "save this preference", "write memory")
    ):
        return "remember_preference", SemanticTarget(type="preference", value=_after_marker(clause, ("：", ":"))), "none"
    recurring_weather = _has_weather(clause) and (
        any(marker in compact for marker in ("每天", "每日", "定时", "每早", "每晚"))
        or any(marker in lowered for marker in ("daily", "every day", "every morning", "each day", "subscribe"))
    )
    if recurring_weather or any(marker in compact for marker in ("每天推送", "每日推送", "订阅天气", "定时推送")) or any(
        marker in lowered for marker in ("daily push", "send every day", "subscribe")
    ):
        location = _location_from(clause) if _has_weather(clause) else ""
        return (
            "create_recurring_notification",
            SemanticTarget(
                type="notification",
                value="weather" if _has_weather(clause) else _clean_target(clause),
                attributes={"frequency": "daily", "location": location},
            ),
            "fresh_external" if _has_weather(clause) else "unknown",
        )
    if any(marker in compact for marker in ("取消", "停止", "终止")) or any(
        marker in lowered for marker in ("cancel", "stop", "terminate")
    ):
        value = inherited_target or _target_after_action(clause, ("取消", "停止", "终止", "cancel", "stop", "terminate"))
        if "所有任务" in clause or "all tasks" in lowered or "全部任务" in clause:
            value = "all tasks"
        return "cancel_task", SemanticTarget(type="task", value=value or "unknown"), "local_state"
    if _has_weather(clause) and any(marker in compact for marker in ("现在", "当前", "天气", "告诉")):
        return "query_current_weather", SemanticTarget(type="weather", value=_location_from(clause)), "fresh_external"
    if any(marker in compact for marker in ("运行状态", "状态", "还活着", "正常运行")) or any(
        marker in lowered for marker in ("status", "running", "still alive")
    ):
        return "query_status", SemanticTarget(type="entity", value=_project_or_default(clause, inherited_target or "unknown")), "fresh_local"
    if any(marker in compact for marker in ("解释", "说明", "讲讲", "讲清楚", "告诉我")) or any(
        marker in lowered for marker in ("explain", "describe", "tell me")
    ):
        return "explain", SemanticTarget(type="topic", value=_clean_target(clause) or inherited_target), "context"
    if any(marker in compact for marker in ("提醒", "通知")) or any(marker in lowered for marker in ("remind", "notify")):
        return "remind", SemanticTarget(type="reminder", value=_clean_target(clause) or inherited_target), "future_observation"
    if _looks_like_question(clause):
        return "answer_question", SemanticTarget(type="question", value=clause), "unknown"
    if _looks_like_request(clause):
        return "fulfill_open_request", SemanticTarget(type="open_goal", value=clause), "unknown"
    return "understand_open_goal", SemanticTarget(type="open_goal", value=clause), "unknown"


def _source_quote(text: str, start: int, end: int) -> SourceQuote:
    if start < 0 or end > len(text) or start >= end:
        raise ValueError("invalid fallback source range")
    return SourceQuote(text=text[start:end], start=start, end=end)


def _trimmed_bounds(value: str, start: int, end: int) -> tuple[int, int]:
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    return start, end


def _strip_discourse_prefix(value: str) -> str:
    cleaned = value.strip()
    patterns = (
        r"^(?:但是|但|而是|只|请只|只是|我是在|我只是)\s*",
        r"^(?:不要真的|不要|别|不用|无需|请勿|不准|先别)\s*",
        r"^(?:but|instead|only|just|do not|don't|dont|please don't|never)\s+",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned.strip(" ，,。.!！?？；;")


def _contains_prohibition(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    lowered = value.lower()
    if any(marker in compact for marker in ("不要", "不用", "无需", "请勿", "不准", "先别", "禁止", "不是真的要")):
        return True
    if re.search(
        r"(?:^|[，,。！？!?；;\s]|我|你|我们|请|以后|现在|先)别(?:再|把|给|去|用|做|动|改|执行|运行|删除|取消|继续|跟|关注|订阅|提醒|发|说|搜索|查)",
        value,
    ):
        return True
    if re.search(
        r"(?:代码|仓库|文件|任务|它|这个|那个).{0,4}别(?:动|改|删|执行|运行|继续)",
        compact,
    ):
        return True
    return bool(re.search(r"\b(?:do not|don't|dont|never|not asking)\b", lowered))


def _contains_contrast(value: str) -> bool:
    return any(marker in value for marker in ("但", "但是", "而是", "只")) or any(
        marker in value.lower() for marker in ("but", "instead", "only", "just")
    )


def _looks_like_question(value: str) -> bool:
    lowered = value.lower()
    return any(marker in value for marker in ("？", "吗", "是否", "是不是", "为什么", "怎么", "什么", "哪", "几")) or any(
        marker in lowered for marker in ("?", "why", "how", "what", "whether", "is ", "are ", "can ")
    )


def _looks_like_request(value: str) -> bool:
    lowered = value.lower()
    compact = re.sub(r"\s+", "", value)
    return any(
        marker in compact
        for marker in ("请", "帮我", "给我", "告诉我", "解释", "说明", "讲清楚", "查看", "检查", "修改", "实现", "提醒", "取消", "记住", "推送")
    ) or any(marker in lowered for marker in ("please", "help me", "tell me", "explain", "check", "modify", "implement", "remind"))


def _looks_hypothetical(value: str) -> bool:
    lowered = value.lower()
    return any(marker in value for marker in ("可能", "也许", "或许", "考虑", "假设", "比如")) or any(
        marker in lowered for marker in ("maybe", "might", "could", "consider", "suppose", "for example")
    )


def _referent_from(value: str) -> ReferentResolution:
    match = re.search(r"(它|这个|那个|上述|上面(?:的)?|之前(?:的)?)", value)
    if match is None:
        match = re.search(r"\b(that|it|this one|the former)\b", value, re.IGNORECASE)
    if match is None:
        return ReferentResolution()
    return ReferentResolution(surface=match.group(1), resolved="", status="unresolved", candidates=[])


def _project_or_default(value: str, default: str) -> str:
    for project in ("Veyra", "OpenClaw", "Hermes", "PyTorch", "TensorFlow"):
        if project.lower() in value.lower():
            return project
    return default


def _has_weather(value: str) -> bool:
    lowered = value.lower()
    return "天气" in value or "下雨" in value or "weather" in lowered or "rain" in lowered


def _location_from(value: str) -> str:
    for location in ("北京", "上海", "深圳", "广州", "杭州", "东京", "大阪", "London", "Tokyo", "Shanghai", "Beijing"):
        if location.lower() in value.lower():
            return location
    return ""


def _target_after_action(value: str, markers: tuple[str, ...]) -> str:
    lowered = value.lower()
    for marker in markers:
        index = lowered.find(marker.lower())
        if index >= 0:
            return value[index + len(marker) :].strip(" ：:,，。.!！?？")
    return ""


def _after_marker(value: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        if marker in value:
            return value.split(marker, 1)[1].strip()
    return _clean_target(value)


def _clean_target(value: str) -> str:
    cleaned = _strip_discourse_prefix(value)
    cleaned = re.sub(
        r"^(?:帮我|请|告诉我|解释|说明|讲讲|讲清楚|查看|检查|修改|实现|提醒|通知|取消|停止|记住|推送)\s*",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ：:,，。.!！?？")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
