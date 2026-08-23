from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


# The Living Context projection gives the brief a bounded view of at most four
# current Situation rows. Each row carries at most six unresolved items, so
# the output contract must represent the complete bounded input without
# becoming an unbounded model-generated list.
COGNITIVE_BRIEF_MAX_LIVING_SITUATION_ROWS = 4
COGNITIVE_BRIEF_MAX_UNKNOWN_PER_SITUATION = 6
COGNITIVE_BRIEF_MAX_UNKNOWN = (
    COGNITIVE_BRIEF_MAX_LIVING_SITUATION_ROWS
    * COGNITIVE_BRIEF_MAX_UNKNOWN_PER_SITUATION
)


# A background hypothesis is only useful to the reaction bridge when the
# producer itself has enough confidence to ask the bridge to spend work.  The
# downstream living-reaction contract uses the same floor; keeping it here
# prevents an invalid candidate from crossing the producer boundary first.
MIN_COGNITIVE_SUGGESTION_CONFIDENCE = 0.65


class StrictCognitiveModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)


class CognitiveObservationPlan(StrictCognitiveModel):
    schema_version: Literal["veyra.cognitive_observation_plan.v1"]
    selected_opportunity_tokens: list[str] = Field(default_factory=list, max_length=2)
    objective: str = Field(min_length=1, max_length=600)
    expected_information_gain: str = Field(default="", max_length=600)
    source: Literal["model"]


class CognitiveEvidenceStatement(StrictCognitiveModel):
    statement: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)


class CognitiveMaterialChange(StrictCognitiveModel):
    kind: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=240)
    statement: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    why_now: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    # V1 Living Context changes bind to a server-issued opaque token.  These
    # fields remain optional here because the legacy situation_graph /
    # GeneralSituation bridge has a separate binding contract; the cognitive
    # loop enforces the stronger requirement when a change cites its selected
    # living_context view.
    change_token: str | None = Field(default=None, min_length=1, max_length=160)
    suggested_next_step: str | None = Field(default=None, min_length=1, max_length=600)


class CognitiveSuggestionCandidate(StrictCognitiveModel):
    """A server-bound, non-authoritative V1 proactive suggestion candidate."""

    schema_version: Literal["veyra.cognitive_suggestion_candidate.v1"]
    candidate_id: str = Field(min_length=1, max_length=120)
    change_token: str = Field(min_length=1, max_length=160)
    cycle_id: str = Field(min_length=1, max_length=120)
    owner_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    situation_id: str = Field(min_length=1, max_length=240)
    situation_revision: int = Field(ge=1)
    semantic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    material_revision: int | None = Field(default=None, ge=0)
    material_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    statement: str = Field(min_length=1, max_length=800)
    why_now: str = Field(min_length=1, max_length=600)
    suggested_next_step: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(
        ge=MIN_COGNITIVE_SUGGESTION_CONFIDENCE,
        le=1.0,
    )
    epistemic_status: Literal["hypothesis"]
    is_fact: Literal[False]
    authority: Literal[False]


class CognitiveSuggestionHandlerResult(StrictCognitiveModel):
    """Normalized result returned by the record-only suggestion sink."""

    schema_version: Literal["veyra.cognitive_suggestion_handler_result.v1"]
    status: Literal[
        "recorded",
        "duplicate",
        "rejected",
        "stale",
        "silent",
        "suppressed",
        "degraded",
    ]
    candidate_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=240)
    retryable: bool = False


class CognitiveNoveltyCheckpointAck(StrictCognitiveModel):
    """Durable acknowledgement that one observed world has been consumed.

    A cognition attempt and its novelty checkpoint are deliberately separate:
    the attempt may be persisted after a model call while the checkpoint only
    advances after a safe terminal disposition.  This record is written by
    the server-side CAS after a quiet result or a completed V1 bridge.
    """

    schema_version: Literal["veyra.cognitive_novelty_checkpoint_ack.v1"]
    status: Literal["acked"]
    cycle_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=120)
    acknowledged_at: str = Field(min_length=1, max_length=80)
    world_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_digests: dict[str, str] = Field(default_factory=dict)
    situation_row_digests: dict[str, str] = Field(default_factory=dict)
    brief: dict | None = None


class CognitiveBrief(StrictCognitiveModel):
    schema_version: Literal["veyra.cognitive_brief.v1"]
    disposition: Literal["quiet", "record_candidate", "needs_observation"]
    summary_if_asked: str = Field(min_length=1, max_length=1600)
    known: list[CognitiveEvidenceStatement] = Field(default_factory=list, max_length=8)
    unknown: list[str] = Field(
        default_factory=list,
        max_length=COGNITIVE_BRIEF_MAX_UNKNOWN,
    )
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    material_changes: list[CognitiveMaterialChange] = Field(default_factory=list, max_length=6)
    why_now: str = Field(default="", max_length=800)
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["model"]

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        if self.disposition == "record_candidate" and (
            not self.material_changes or not self.why_now.strip()
        ):
            raise ValueError(
                "record_candidate requires a material change and why_now"
            )
        return self
