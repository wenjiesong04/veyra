from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any


LEARNING_RECORD_SCHEMA = "veyra.learning_record.v1"
SUGGESTION_FEEDBACK_RECORD_SCHEMA = "veyra.suggestion_feedback_record.v1"
FEEDBACK_LABELS = frozenset(
    {
        "useful",
        "not_useful",
        "too_frequent",
        "wrong_timing",
        "wrong_evidence",
    }
)
USEFULNESS_LABELS = frozenset({"useful", "not_useful"})
LEARNING_RECORD_STATUSES = frozenset({"active", "superseded"})
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")


class LearningRecordValidationError(ValueError):
    """A shadow learning record failed its strict, privacy-minimal contract."""


def canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LearningRecordValidationError(
            "learning record value is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningRecord:
    """One explicit categorical label with no authority or free-form content."""

    schema_version: str
    learning_id: str
    feedback_id: str
    user_id: str
    assessment_id: str
    assessment_revision: str
    candidate_id: str
    candidate_revision: str
    binding_digest: str
    label: str
    status: str
    supersedes_learning_id: str | None
    superseded_by_learning_id: str | None
    promotion_status: str
    policy_effect: str
    source: str
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        feedback_id: str,
        user_id: str,
        assessment_id: str,
        assessment_revision: str,
        candidate_id: str,
        candidate_revision: str,
        label: str,
        created_at: datetime,
        supersedes_learning_id: str | None = None,
    ) -> LearningRecord:
        selected_feedback = _opaque_id(feedback_id, "feedback_id")
        selected_user = _bounded_identity(user_id, "user_id", 240)
        selected_assessment = _opaque_id(
            assessment_id,
            "assessment_id",
        )
        selected_assessment_revision = _opaque_id(
            assessment_revision,
            "assessment_revision",
        )
        selected_candidate = _opaque_id(candidate_id, "candidate_id")
        selected_candidate_revision = _opaque_id(
            candidate_revision,
            "candidate_revision",
        )
        selected_label = str(label or "").strip()
        if selected_label not in FEEDBACK_LABELS:
            raise LearningRecordValidationError(
                "learning label is unsupported"
            )
        selected_supersedes = (
            _opaque_id(
                supersedes_learning_id,
                "supersedes_learning_id",
            )
            if supersedes_learning_id is not None
            else None
        )
        observed_at = _aware_iso(created_at, "created_at")
        binding = {
            "user_id": selected_user,
            "assessment_id": selected_assessment,
            "assessment_revision": selected_assessment_revision,
            "candidate_id": selected_candidate,
            "candidate_revision": selected_candidate_revision,
        }
        binding_digest = canonical_digest(binding)
        learning_id = "learn_" + canonical_digest(
            {
                "feedback_id": selected_feedback,
                "binding_digest": binding_digest,
                "label": selected_label,
                "supersedes_learning_id": selected_supersedes,
            }
        )[:24]
        return cls(
            schema_version=LEARNING_RECORD_SCHEMA,
            learning_id=learning_id,
            feedback_id=selected_feedback,
            user_id=selected_user,
            assessment_id=selected_assessment,
            assessment_revision=selected_assessment_revision,
            candidate_id=selected_candidate,
            candidate_revision=selected_candidate_revision,
            binding_digest=binding_digest,
            label=selected_label,
            status="active",
            supersedes_learning_id=selected_supersedes,
            superseded_by_learning_id=None,
            promotion_status="candidate",
            policy_effect="none",
            source="explicit_user_feedback",
            created_at=observed_at,
            updated_at=observed_at,
        )

    @classmethod
    def from_dict(cls, value: Any) -> LearningRecord:
        if not isinstance(value, dict):
            raise LearningRecordValidationError(
                "learning record must be an object"
            )
        expected_fields = set(cls.__dataclass_fields__)
        if set(value) != expected_fields:
            raise LearningRecordValidationError(
                "learning record fields do not match the v1 contract"
            )
        record = cls(**value)
        record.validate()
        return record

    def validate(self) -> None:
        if self.schema_version != LEARNING_RECORD_SCHEMA:
            raise LearningRecordValidationError(
                "learning record schema is unsupported"
            )
        _opaque_id(self.learning_id, "learning_id")
        _opaque_id(self.feedback_id, "feedback_id")
        _bounded_identity(self.user_id, "user_id", 240)
        _opaque_id(self.assessment_id, "assessment_id")
        _opaque_id(self.assessment_revision, "assessment_revision")
        _opaque_id(self.candidate_id, "candidate_id")
        _opaque_id(self.candidate_revision, "candidate_revision")
        if not re.fullmatch(r"[0-9a-f]{64}", self.binding_digest):
            raise LearningRecordValidationError(
                "binding_digest must be a lowercase SHA-256 digest"
            )
        expected_binding = canonical_digest(
            {
                "user_id": self.user_id,
                "assessment_id": self.assessment_id,
                "assessment_revision": self.assessment_revision,
                "candidate_id": self.candidate_id,
                "candidate_revision": self.candidate_revision,
            }
        )
        if self.binding_digest != expected_binding:
            raise LearningRecordValidationError(
                "binding_digest does not match the exact feedback target"
            )
        if self.label not in FEEDBACK_LABELS:
            raise LearningRecordValidationError(
                "learning label is unsupported"
            )
        if self.status not in LEARNING_RECORD_STATUSES:
            raise LearningRecordValidationError(
                "learning record status is unsupported"
            )
        if self.supersedes_learning_id is not None:
            _opaque_id(
                self.supersedes_learning_id,
                "supersedes_learning_id",
            )
        if self.superseded_by_learning_id is not None:
            _opaque_id(
                self.superseded_by_learning_id,
                "superseded_by_learning_id",
            )
        if self.status == "active" and self.superseded_by_learning_id is not None:
            raise LearningRecordValidationError(
                "active learning record cannot have a successor"
            )
        if (
            self.status == "superseded"
            and self.superseded_by_learning_id is None
        ):
            raise LearningRecordValidationError(
                "superseded learning record requires a successor"
            )
        if self.promotion_status != "candidate":
            raise LearningRecordValidationError(
                "shadow learning cannot promote itself"
            )
        if self.policy_effect != "none":
            raise LearningRecordValidationError(
                "shadow learning cannot modify policy"
            )
        if self.source != "explicit_user_feedback":
            raise LearningRecordValidationError(
                "learning source must be explicit user feedback"
            )
        created = _parse_aware(self.created_at, "created_at")
        updated = _parse_aware(self.updated_at, "updated_at")
        if updated < created:
            raise LearningRecordValidationError(
                "updated_at cannot precede created_at"
            )
        expected_id = "learn_" + canonical_digest(
            {
                "feedback_id": self.feedback_id,
                "binding_digest": self.binding_digest,
                "label": self.label,
                "supersedes_learning_id": self.supersedes_learning_id,
            }
        )[:24]
        if self.learning_id != expected_id:
            raise LearningRecordValidationError(
                "learning_id does not match record semantics"
            )

    def superseded_by(
        self,
        learning_id: str,
        *,
        updated_at: datetime,
    ) -> LearningRecord:
        if self.status != "active":
            raise LearningRecordValidationError(
                "only an active learning record can be superseded"
            )
        successor = _opaque_id(learning_id, "superseded_by_learning_id")
        updated = replace(
            self,
            status="superseded",
            superseded_by_learning_id=successor,
            updated_at=_aware_iso(updated_at, "updated_at"),
        )
        updated.validate()
        return updated

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }

    def idempotency_semantics(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "binding_digest": self.binding_digest,
            "label": self.label,
            "supersedes_learning_id": self.supersedes_learning_id,
        }


@dataclass(frozen=True, slots=True)
class SuggestionFeedbackRecord:
    """One exact-scope categorical label for a surfaced Console suggestion."""

    schema_version: str
    learning_id: str
    feedback_id: str
    user_id: str
    session_id: str
    proposal_id: str
    proposal_revision: str
    general_situation_id: str
    parent_revision: int
    binding_digest: str
    label: str
    status: str
    supersedes_learning_id: str | None
    superseded_by_learning_id: str | None
    promotion_status: str
    policy_effect: str
    source: str
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        feedback_id: str,
        user_id: str,
        session_id: str,
        proposal_id: str,
        proposal_revision: str,
        general_situation_id: str,
        parent_revision: int,
        label: str,
        created_at: datetime,
        supersedes_learning_id: str | None = None,
    ) -> "SuggestionFeedbackRecord":
        selected_feedback = _opaque_id(feedback_id, "feedback_id")
        selected_user = _bounded_identity(user_id, "user_id", 240)
        selected_session = _bounded_identity(session_id, "session_id", 240)
        selected_proposal = _opaque_id(proposal_id, "proposal_id")
        selected_proposal_revision = _opaque_id(
            proposal_revision,
            "proposal_revision",
        )
        selected_situation = _opaque_id(
            general_situation_id,
            "general_situation_id",
        )
        selected_parent_revision = _positive_int(
            parent_revision,
            "parent_revision",
        )
        selected_label = str(label or "").strip()
        if selected_label not in FEEDBACK_LABELS:
            raise LearningRecordValidationError(
                "learning label is unsupported"
            )
        selected_supersedes = (
            _opaque_id(
                supersedes_learning_id,
                "supersedes_learning_id",
            )
            if supersedes_learning_id is not None
            else None
        )
        observed_at = _aware_iso(created_at, "created_at")
        binding = {
            "user_id": selected_user,
            "session_id": selected_session,
            "proposal_id": selected_proposal,
            "proposal_revision": selected_proposal_revision,
            "general_situation_id": selected_situation,
            "parent_revision": selected_parent_revision,
        }
        binding_digest = canonical_digest(binding)
        learning_id = "slearn_" + canonical_digest(
            {
                "feedback_id": selected_feedback,
                "binding_digest": binding_digest,
                "label": selected_label,
                "supersedes_learning_id": selected_supersedes,
            }
        )[:24]
        return cls(
            schema_version=SUGGESTION_FEEDBACK_RECORD_SCHEMA,
            learning_id=learning_id,
            feedback_id=selected_feedback,
            user_id=selected_user,
            session_id=selected_session,
            proposal_id=selected_proposal,
            proposal_revision=selected_proposal_revision,
            general_situation_id=selected_situation,
            parent_revision=selected_parent_revision,
            binding_digest=binding_digest,
            label=selected_label,
            status="active",
            supersedes_learning_id=selected_supersedes,
            superseded_by_learning_id=None,
            promotion_status="candidate",
            policy_effect="none",
            source="explicit_console_feedback",
            created_at=observed_at,
            updated_at=observed_at,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "SuggestionFeedbackRecord":
        if not isinstance(value, dict):
            raise LearningRecordValidationError(
                "suggestion feedback record must be an object"
            )
        expected_fields = set(cls.__dataclass_fields__)
        if set(value) != expected_fields:
            raise LearningRecordValidationError(
                "suggestion feedback fields do not match the v1 contract"
            )
        record = cls(**value)
        record.validate()
        return record

    def validate(self) -> None:
        if self.schema_version != SUGGESTION_FEEDBACK_RECORD_SCHEMA:
            raise LearningRecordValidationError(
                "suggestion feedback schema is unsupported"
            )
        _opaque_id(self.learning_id, "learning_id")
        _opaque_id(self.feedback_id, "feedback_id")
        _bounded_identity(self.user_id, "user_id", 240)
        _bounded_identity(self.session_id, "session_id", 240)
        _opaque_id(self.proposal_id, "proposal_id")
        _opaque_id(self.proposal_revision, "proposal_revision")
        _opaque_id(self.general_situation_id, "general_situation_id")
        _positive_int(self.parent_revision, "parent_revision")
        if not re.fullmatch(r"[0-9a-f]{64}", self.binding_digest):
            raise LearningRecordValidationError(
                "binding_digest must be a lowercase SHA-256 digest"
            )
        expected_binding = canonical_digest(
            {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "proposal_id": self.proposal_id,
                "proposal_revision": self.proposal_revision,
                "general_situation_id": self.general_situation_id,
                "parent_revision": self.parent_revision,
            }
        )
        if self.binding_digest != expected_binding:
            raise LearningRecordValidationError(
                "binding_digest does not match the exact suggestion target"
            )
        if self.label not in FEEDBACK_LABELS:
            raise LearningRecordValidationError(
                "learning label is unsupported"
            )
        if self.status not in LEARNING_RECORD_STATUSES:
            raise LearningRecordValidationError(
                "suggestion feedback status is unsupported"
            )
        if self.supersedes_learning_id is not None:
            _opaque_id(
                self.supersedes_learning_id,
                "supersedes_learning_id",
            )
        if self.superseded_by_learning_id is not None:
            _opaque_id(
                self.superseded_by_learning_id,
                "superseded_by_learning_id",
            )
        if self.status == "active" and self.superseded_by_learning_id is not None:
            raise LearningRecordValidationError(
                "active suggestion feedback cannot have a successor"
            )
        if (
            self.status == "superseded"
            and self.superseded_by_learning_id is None
        ):
            raise LearningRecordValidationError(
                "superseded suggestion feedback requires a successor"
            )
        if self.promotion_status != "candidate":
            raise LearningRecordValidationError(
                "suggestion feedback cannot promote itself"
            )
        if self.policy_effect != "none":
            raise LearningRecordValidationError(
                "suggestion feedback cannot modify policy"
            )
        if self.source != "explicit_console_feedback":
            raise LearningRecordValidationError(
                "suggestion feedback source must be explicit Console feedback"
            )
        created = _parse_aware(self.created_at, "created_at")
        updated = _parse_aware(self.updated_at, "updated_at")
        if updated < created:
            raise LearningRecordValidationError(
                "updated_at cannot precede created_at"
            )
        expected_id = "slearn_" + canonical_digest(
            {
                "feedback_id": self.feedback_id,
                "binding_digest": self.binding_digest,
                "label": self.label,
                "supersedes_learning_id": self.supersedes_learning_id,
            }
        )[:24]
        if self.learning_id != expected_id:
            raise LearningRecordValidationError(
                "learning_id does not match suggestion feedback semantics"
            )

    def superseded_by(
        self,
        learning_id: str,
        *,
        updated_at: datetime,
    ) -> "SuggestionFeedbackRecord":
        if self.status != "active":
            raise LearningRecordValidationError(
                "only active suggestion feedback can be superseded"
            )
        successor = _opaque_id(learning_id, "superseded_by_learning_id")
        updated = replace(
            self,
            status="superseded",
            superseded_by_learning_id=successor,
            updated_at=_aware_iso(updated_at, "updated_at"),
        )
        updated.validate()
        return updated

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }

    def idempotency_semantics(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "binding_digest": self.binding_digest,
            "label": self.label,
            "supersedes_learning_id": self.supersedes_learning_id,
        }


def _opaque_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise LearningRecordValidationError(
            f"{field} must be a bounded opaque identifier"
        )
    return value


def _bounded_identity(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise LearningRecordValidationError(f"{field} must be a string")
    selected = value.strip()
    if (
        not selected
        or len(selected) > limit
        or any(ord(character) < 32 for character in selected)
    ):
        raise LearningRecordValidationError(
            f"{field} must be a bounded non-control identity"
        )
    return selected


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LearningRecordValidationError(
            f"{field} must be a positive integer"
        )
    return value


def _aware_iso(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LearningRecordValidationError(
            f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc).isoformat()


def _parse_aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise LearningRecordValidationError(
            f"{field} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LearningRecordValidationError(
            f"{field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise LearningRecordValidationError(
            f"{field} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


__all__ = [
    "FEEDBACK_LABELS",
    "LEARNING_RECORD_SCHEMA",
    "LEARNING_RECORD_STATUSES",
    "SUGGESTION_FEEDBACK_RECORD_SCHEMA",
    "USEFULNESS_LABELS",
    "LearningRecord",
    "LearningRecordValidationError",
    "SuggestionFeedbackRecord",
    "canonical_digest",
]
