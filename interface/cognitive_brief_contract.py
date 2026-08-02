from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class CognitiveBrief(StrictCognitiveModel):
    schema_version: Literal["veyra.cognitive_brief.v1"]
    disposition: Literal["quiet", "record_candidate", "needs_observation"]
    summary_if_asked: str = Field(min_length=1, max_length=1600)
    known: list[CognitiveEvidenceStatement] = Field(default_factory=list, max_length=8)
    unknown: list[str] = Field(default_factory=list, max_length=8)
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

