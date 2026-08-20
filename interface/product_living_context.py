"""Strict request contracts for the V1 product living-context surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ProductSituationCommand = Literal["correct", "resolve", "reopen", "quiet"]
ProductQuestionAction = Literal["answer", "defer", "dismiss"]
ProductFeedbackLabel = Literal[
    "useful",
    "not_useful",
    "ignore",
    "resolved",
    "too_early",
    "too_late",
    "too_frequent",
    "remind_before",
    "remind_offset",
]


class ProductRequest(BaseModel):
    """Reject unknown fields so client state cannot become an authority seam."""

    model_config = ConfigDict(strict=True, extra="forbid")


class SituationCommandRequest(ProductRequest):
    command: ProductSituationCommand
    expected_revision: int = Field(ge=1)
    patch: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=240)
    event_id: str | None = Field(default=None, min_length=1, max_length=240)


class QuestionAnswerRequest(ProductRequest):
    answer: str = Field(min_length=1, max_length=480)
    expected_generation: int = Field(ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    event_id: str | None = Field(default=None, min_length=1, max_length=240)


class QuestionTransitionRequest(ProductRequest):
    expected_generation: int = Field(ge=1)
    event_id: str | None = Field(default=None, min_length=1, max_length=240)


class ReactionFeedbackRequest(ProductRequest):
    label: ProductFeedbackLabel
    situation_revision: int = Field(ge=1)
    category: str = Field(default="", max_length=120)
    remind_before_seconds: int | None = Field(default=None, ge=0, le=30 * 86400)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)


class SourceConsentRequest(ProductRequest):
    expected_generation: int = Field(default=0, ge=0)
    purpose: str = Field(default="Veyra V1 read-only Living Context", min_length=1, max_length=600)
    consent_id: str | None = Field(default=None, min_length=1, max_length=240)
    expires_at: str | None = Field(default=None, min_length=1, max_length=80)


class SourceRevokeRequest(ProductRequest):
    expected_generation: int = Field(ge=1)
    consent_id: str | None = Field(default=None, min_length=1, max_length=240)
