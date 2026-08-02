from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from awareness.project_guardian import ProjectGuardianEvaluator
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)
from core.learning_record import (
    FEEDBACK_LABELS,
    LearningRecord,
    LearningRecordValidationError,
    SuggestionFeedbackRecord,
    USEFULNESS_LABELS,
)
from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from runtime.suggestion_outbox import SuggestionOutbox


class LearningCalibrationError(RuntimeError):
    """Base error for the shadow-only learning calibration ledger."""


class LearningCalibrationConflict(LearningCalibrationError):
    """Feedback identity, target binding, or correction chain conflicted."""


class LearningCalibrationStorageError(LearningCalibrationError):
    """Durable learning or Attention state is corrupt or semantically invalid."""


class LearningCalibrationRuntime:
    """Persist explicit categorical feedback without changing runtime policy.

    Feedback binds either to one Project Guardian Attention assessment or to
    one exact surfaced Suggestion revision. The ledger accepts no notes,
    message bodies, prompts, channel payloads, or inferred signals. A changed
    label is an explicit correction and must name the current active
    ``learning_id`` it supersedes.
    """

    STATE_FILE = "learning_calibration_state.json"
    STATE_SCHEMA = "veyra.learning_calibration_state.v1"
    ATTENTION_STATE_SCHEMA = "veyra.project_guardian_attention_state.v1"
    ASSESSMENT_SCHEMA = "veyra.project_guardian_attention_assessment.v1"
    MAX_RECORDS = 500
    MAX_SUGGESTION_RECORDS = 2000
    MIN_DESCRIPTIVE_SUPPORT = 20

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record_feedback(
        self,
        *,
        feedback_id: str,
        user_id: str,
        assessment_id: str,
        assessment_revision: str,
        candidate_id: str,
        candidate_revision: str,
        label: str,
        supersedes_learning_id: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        candidate_record = LearningRecord.create(
            feedback_id=feedback_id,
            user_id=user_id,
            assessment_id=assessment_id,
            assessment_revision=assessment_revision,
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            label=label,
            created_at=now,
            supersedes_learning_id=supersedes_learning_id,
        )
        result: dict[str, Any] = {}

        with self.state_store.writer_transaction():
            initial_state = self.state_store.read_json(self.STATE_FILE)
            self._require_state(initial_state)
            initial_records = self._record_map(initial_state)
            initial_feedback_index = self._string_map(
                initial_state.get("feedback_index"),
                "feedback_index",
            )
            existing_id = initial_feedback_index.get(
                candidate_record.feedback_id
            )
            if existing_id is not None:
                existing = initial_records.get(existing_id)
                if (
                    existing is None
                    or existing.idempotency_semantics()
                    != candidate_record.idempotency_semantics()
                ):
                    raise LearningCalibrationConflict(
                        "feedback_id is already bound to different semantics"
                    )
                result.update(
                    status="duplicate",
                    record=existing.to_dict(),
                )
                return {
                    **result,
                    "authority": self._authority_contract(),
                }

            attention_state = self.state_store.read_json(
                "project_guardian_attention_state.json"
            )
            self._require_attention_state(attention_state)
            attention_revision = self._state_revision(attention_state)
            self._require_exact_assessment(
                attention_state,
                candidate_record,
            )

            def update(state: dict[str, Any]) -> None:
                self._require_state(state)
                current_attention = self.state_store.read_json(
                    "project_guardian_attention_state.json"
                )
                self._require_attention_state(current_attention)
                if self._state_revision(current_attention) != attention_revision:
                    raise LearningCalibrationConflict(
                        "Attention assessment changed during feedback admission"
                    )
                self._require_exact_assessment(
                    current_attention,
                    candidate_record,
                )

                records = self._record_map(state)
                feedback_index = self._string_map(
                    state.get("feedback_index"),
                    "feedback_index",
                )
                active_by_binding = self._string_map(
                    state.get("active_by_binding"),
                    "active_by_binding",
                )

                existing_id = feedback_index.get(
                    candidate_record.feedback_id
                )
                if existing_id is not None:
                    existing = records.get(existing_id)
                    if (
                        existing is None
                        or existing.idempotency_semantics()
                        != candidate_record.idempotency_semantics()
                    ):
                        raise LearningCalibrationConflict(
                            "feedback_id is already bound to different semantics"
                        )
                    result.update(
                        status="duplicate",
                        record=existing.to_dict(),
                    )
                    return

                current_id = active_by_binding.get(
                    candidate_record.binding_digest
                )
                current = records.get(current_id) if current_id else None
                if current is None:
                    if candidate_record.supersedes_learning_id is not None:
                        raise LearningCalibrationConflict(
                            "supersedes_learning_id has no active target"
                        )
                    result_status = "recorded"
                else:
                    if (
                        candidate_record.supersedes_learning_id
                        != current.learning_id
                    ):
                        raise LearningCalibrationConflict(
                            "an existing label requires an exact correction target"
                        )
                    current = current.superseded_by(
                        candidate_record.learning_id,
                        updated_at=now,
                    )
                    records[current.learning_id] = current
                    result_status = "corrected"

                if len(records) >= self.MAX_RECORDS:
                    raise LearningCalibrationStorageError(
                        "learning calibration record capacity exhausted"
                    )
                if candidate_record.learning_id in records:
                    raise LearningCalibrationStorageError(
                        "learning_id collision detected"
                    )
                records[candidate_record.learning_id] = candidate_record
                feedback_index[candidate_record.feedback_id] = (
                    candidate_record.learning_id
                )
                active_by_binding[candidate_record.binding_digest] = (
                    candidate_record.learning_id
                )
                state["schema_version"] = self.STATE_SCHEMA
                state["records"] = {
                    learning_id: record.to_dict()
                    for learning_id, record in sorted(records.items())
                }
                state["feedback_index"] = dict(
                    sorted(feedback_index.items())
                )
                state["active_by_binding"] = dict(
                    sorted(active_by_binding.items())
                )
                state["record_count"] = len(records)
                state["active_count"] = len(active_by_binding)
                state["policy_effect"] = "none"
                state["updated_at"] = utc_now_iso()
                self._require_state(state)
                result.update(
                    status=result_status,
                    record=candidate_record.to_dict(),
                )

            self.state_store.mutate_json(self.STATE_FILE, update)

        return {
            **result,
            "authority": self._authority_contract(),
        }

    def status(self) -> dict[str, Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        try:
            self._require_state(state)
        except LearningCalibrationStorageError as exc:
            return {
                "status": "degraded",
                "reason": str(exc),
                "state_frozen": True,
                "authority": self._authority_contract(),
            }
        return {
            "status": "success",
            "record_count": int(state.get("record_count") or 0),
            "active_count": int(state.get("active_count") or 0),
            "suggestion_record_count": int(
                state.get("suggestion_record_count") or 0
            ),
            "suggestion_active_count": int(
                state.get("suggestion_active_count") or 0
            ),
            "support": (
                "sufficient_for_description"
                if int(state.get("active_count") or 0)
                >= self.MIN_DESCRIPTIVE_SUPPORT
                else "insufficient_data"
            ),
            "authority": self._authority_contract(),
        }

    def record_suggestion_feedback(
        self,
        *,
        feedback_id: str,
        user_id: str,
        session_id: str,
        proposal_id: str,
        proposal_revision: str,
        general_situation_id: str,
        parent_revision: int,
        label: str,
        expected_outbox_state_revision: int,
        supersedes_learning_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist exact categorical feedback for one surfaced suggestion."""

        if (
            isinstance(expected_outbox_state_revision, bool)
            or not isinstance(expected_outbox_state_revision, int)
            or expected_outbox_state_revision < 0
        ):
            raise ValueError(
                "expected_outbox_state_revision must be non-negative"
            )
        now = self._now()
        candidate_record = SuggestionFeedbackRecord.create(
            feedback_id=feedback_id,
            user_id=user_id,
            session_id=session_id,
            proposal_id=proposal_id,
            proposal_revision=proposal_revision,
            general_situation_id=general_situation_id,
            parent_revision=parent_revision,
            label=label,
            created_at=now,
            supersedes_learning_id=supersedes_learning_id,
        )
        result: dict[str, Any] = {}

        with self.state_store.writer_transaction():
            initial_state = self.state_store.read_json(self.STATE_FILE)
            self._require_state(initial_state)
            initial_records = self._suggestion_record_map(initial_state)
            initial_feedback_index = self._optional_string_map(
                initial_state.get("suggestion_feedback_index"),
                "suggestion_feedback_index",
            )
            existing_id = initial_feedback_index.get(
                candidate_record.feedback_id
            )
            if existing_id is not None:
                existing = initial_records.get(existing_id)
                if (
                    existing is None
                    or existing.idempotency_semantics()
                    != candidate_record.idempotency_semantics()
                ):
                    raise LearningCalibrationConflict(
                        "feedback_id is already bound to different semantics"
                    )
                return {
                    "status": "duplicate",
                    "record": existing.to_dict(),
                    "authority": self._suggestion_authority_contract(),
                }

            outbox_state = self.state_store.read_json(
                SuggestionOutbox.STATE_FILE
            )
            self._require_suggestion_outbox_state(outbox_state)
            outbox_revision = self._state_revision(outbox_state)
            if outbox_revision != expected_outbox_state_revision:
                raise LearningCalibrationConflict(
                    "expected outbox revision does not match suggestion state"
                )
            self._require_exact_surfaced_suggestion(
                outbox_state,
                candidate_record,
            )

            def update(state: dict[str, Any]) -> None:
                self._require_state(state)
                current_outbox = self.state_store.read_json(
                    SuggestionOutbox.STATE_FILE
                )
                self._require_suggestion_outbox_state(current_outbox)
                if self._state_revision(current_outbox) != outbox_revision:
                    raise LearningCalibrationConflict(
                        "suggestion state changed during feedback admission"
                    )
                self._require_exact_surfaced_suggestion(
                    current_outbox,
                    candidate_record,
                )

                records = self._suggestion_record_map(state)
                feedback_index = self._optional_string_map(
                    state.get("suggestion_feedback_index"),
                    "suggestion_feedback_index",
                )
                active_by_binding = self._optional_string_map(
                    state.get("suggestion_active_by_binding"),
                    "suggestion_active_by_binding",
                )
                existing_id = feedback_index.get(
                    candidate_record.feedback_id
                )
                if existing_id is not None:
                    existing = records.get(existing_id)
                    if (
                        existing is None
                        or existing.idempotency_semantics()
                        != candidate_record.idempotency_semantics()
                    ):
                        raise LearningCalibrationConflict(
                            "feedback_id is already bound to different semantics"
                        )
                    result.update(status="duplicate", record=existing.to_dict())
                    return

                current_id = active_by_binding.get(
                    candidate_record.binding_digest
                )
                current = records.get(current_id) if current_id else None
                if current is None:
                    if candidate_record.supersedes_learning_id is not None:
                        raise LearningCalibrationConflict(
                            "supersedes_learning_id has no active target"
                        )
                    result_status = "recorded"
                else:
                    if (
                        candidate_record.supersedes_learning_id
                        != current.learning_id
                    ):
                        raise LearningCalibrationConflict(
                            "an existing label requires an exact correction target"
                        )
                    current = current.superseded_by(
                        candidate_record.learning_id,
                        updated_at=now,
                    )
                    records[current.learning_id] = current
                    result_status = "corrected"

                if len(records) >= self.MAX_SUGGESTION_RECORDS:
                    raise LearningCalibrationStorageError(
                        "suggestion feedback record capacity exhausted"
                    )
                if candidate_record.learning_id in records:
                    raise LearningCalibrationStorageError(
                        "suggestion learning_id collision detected"
                    )
                records[candidate_record.learning_id] = candidate_record
                feedback_index[candidate_record.feedback_id] = (
                    candidate_record.learning_id
                )
                active_by_binding[candidate_record.binding_digest] = (
                    candidate_record.learning_id
                )
                state["suggestion_records"] = {
                    learning_id: record.to_dict()
                    for learning_id, record in sorted(records.items())
                }
                state["suggestion_feedback_index"] = dict(
                    sorted(feedback_index.items())
                )
                state["suggestion_active_by_binding"] = dict(
                    sorted(active_by_binding.items())
                )
                state["suggestion_record_count"] = len(records)
                state["suggestion_active_count"] = len(active_by_binding)
                state["suggestion_policy_effect"] = "none"
                state["updated_at"] = utc_now_iso()
                self._require_state(state)
                result.update(
                    status=result_status,
                    record=candidate_record.to_dict(),
                )

            self.state_store.mutate_json(self.STATE_FILE, update)

        return {
            **result,
            "authority": self._suggestion_authority_contract(),
        }

    def list_suggestion_feedback(
        self,
        *,
        user_id: str,
        session_id: str,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_user = self._selected_identity(user_id, "user_id")
        selected_session = self._selected_identity(session_id, "session_id")
        state = self.state_store.read_json(self.STATE_FILE)
        self._require_state(state)
        rows = [
            record.to_dict()
            for record in self._suggestion_record_map(state).values()
            if record.user_id == selected_user
            and record.session_id == selected_session
            and (not active_only or record.status == "active")
        ]
        rows.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("learning_id") or ""),
            ),
            reverse=True,
        )
        return rows[: max(0, min(int(limit), self.MAX_SUGGESTION_RECORDS))]

    def suggestion_summary(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        selected_user = self._selected_identity(user_id, "user_id")
        selected_session = self._selected_identity(session_id, "session_id")
        rows = self.list_suggestion_feedback(
            user_id=selected_user,
            session_id=selected_session,
            active_only=True,
            limit=self.MAX_SUGGESTION_RECORDS,
        )
        counts = {
            label: sum(item.get("label") == label for item in rows)
            for label in sorted(FEEDBACK_LABELS)
        }
        useful = counts["useful"]
        not_useful = counts["not_useful"]
        usefulness_total = useful + not_useful
        return {
            "status": "success",
            "user_id": selected_user,
            "session_id": selected_session,
            "active_feedback_count": len(rows),
            "usefulness_feedback_count": usefulness_total,
            "diagnostic_feedback_count": len(rows) - usefulness_total,
            "counts": counts,
            "useful_rate": (
                round(useful / usefulness_total, 6)
                if usefulness_total
                else None
            ),
            "accuracy": (
                "unavailable_without_falsifiable_prediction_and_verified_outcome"
            ),
            "support": (
                "sufficient_for_description"
                if usefulness_total >= self.MIN_DESCRIPTIVE_SUPPORT
                else "insufficient_data"
            ),
            "minimum_descriptive_support": self.MIN_DESCRIPTIVE_SUPPORT,
            "usefulness_denominator_labels": sorted(USEFULNESS_LABELS),
            "categorical_feedback_only": True,
            "dismissal_inferred_as_usefulness": False,
            "policy_effect": "none",
            "promotion_allowed": False,
            "authority": self._suggestion_authority_contract(),
        }

    def list_records(
        self,
        *,
        user_id: str,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_user = str(user_id or "").strip()
        if not selected_user or len(selected_user) > 240:
            raise ValueError("user_id is required and must be <= 240 chars")
        state = self.state_store.read_json(self.STATE_FILE)
        self._require_state(state)
        rows = [
            record.to_dict()
            for record in self._record_map(state).values()
            if record.user_id == selected_user
            and (not active_only or record.status == "active")
        ]
        rows.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("learning_id") or ""),
            ),
            reverse=True,
        )
        return rows[: max(0, min(int(limit), 500))]

    def summary(self, *, user_id: str) -> dict[str, Any]:
        selected_user = str(user_id or "").strip()
        rows = self.list_records(
            user_id=selected_user,
            active_only=True,
            limit=self.MAX_RECORDS,
        )
        counts = {
            label: sum(item.get("label") == label for item in rows)
            for label in sorted(FEEDBACK_LABELS)
        }
        useful = counts["useful"]
        not_useful = counts["not_useful"]
        usefulness_total = useful + not_useful
        return {
            "status": "success",
            "user_id": selected_user,
            "active_feedback_count": len(rows),
            "usefulness_feedback_count": usefulness_total,
            "diagnostic_feedback_count": (
                len(rows) - usefulness_total
            ),
            "counts": counts,
            "useful_rate": (
                round(useful / usefulness_total, 6)
                if usefulness_total
                else None
            ),
            "support": (
                "sufficient_for_description"
                if usefulness_total >= self.MIN_DESCRIPTIVE_SUPPORT
                else "insufficient_data"
            ),
            "minimum_descriptive_support": self.MIN_DESCRIPTIVE_SUPPORT,
            "usefulness_denominator_labels": sorted(USEFULNESS_LABELS),
            "categorical_feedback_only": True,
            "policy_effect": "none",
            "promotion_allowed": False,
        }

    def _require_exact_assessment(
        self,
        attention_state: dict[str, Any],
        record: LearningRecord,
    ) -> None:
        assessments = attention_state.get("assessments")
        if not isinstance(assessments, dict):
            raise LearningCalibrationStorageError(
                "Attention assessments must be a mapping"
            )
        assessment = assessments.get(record.candidate_id)
        if not isinstance(assessment, dict):
            raise LearningCalibrationConflict(
                "feedback does not match the current candidate assessment"
            )
        self._validate_attention_assessment(
            record.candidate_id,
            assessment,
        )
        candidate_ref = assessment["candidate_ref"]
        if (
            assessment.get("assessment_id") != record.assessment_id
            or assessment.get("assessment_revision")
            != record.assessment_revision
            or assessment.get("user_id") != record.user_id
            or candidate_ref.get("candidate_id")
            != record.candidate_id
            or candidate_ref.get("candidate_revision")
            != record.candidate_revision
        ):
            raise LearningCalibrationConflict(
                "feedback does not match the exact current Attention assessment"
            )

    def _require_suggestion_outbox_state(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            raise LearningCalibrationStorageError(
                "suggestion outbox state is corrupt"
            )
        if str(state.get("schema_version") or "") != SuggestionOutbox.SCHEMA_VERSION:
            raise LearningCalibrationStorageError(
                "suggestion outbox state schema is unsupported"
            )
        if self._state_revision(state) < 1:
            raise LearningCalibrationStorageError(
                "suggestion outbox state revision is invalid"
            )
        proposals = state.get("proposals")
        if not isinstance(proposals, dict):
            raise LearningCalibrationStorageError(
                "suggestion proposals must be a mapping"
            )
        proposal_count = state.get("proposal_count")
        if (
            isinstance(proposal_count, bool)
            or not isinstance(proposal_count, int)
            or proposal_count != len(proposals)
        ):
            raise LearningCalibrationStorageError(
                "suggestion proposal_count is inconsistent"
            )
        owner_inboxes = state.get("owner_inboxes")
        if not isinstance(owner_inboxes, dict) or any(
            not isinstance(key, str) or not isinstance(value, list)
            for key, value in owner_inboxes.items()
        ):
            raise LearningCalibrationStorageError(
                "suggestion owner inboxes are invalid"
            )

    def _require_exact_surfaced_suggestion(
        self,
        state: dict[str, Any],
        record: SuggestionFeedbackRecord,
    ) -> None:
        proposals = state.get("proposals")
        proposal = (
            proposals.get(record.proposal_id)
            if isinstance(proposals, dict)
            else None
        )
        if not isinstance(proposal, dict):
            raise LearningCalibrationConflict(
                "feedback does not match a persisted suggestion"
            )
        try:
            expected_revision = SuggestionOutbox.proposal_revision_for(
                proposal
            )
        except (TypeError, ValueError) as exc:
            raise LearningCalibrationStorageError(
                "suggestion proposal revision is invalid"
            ) from exc
        delivery = (
            proposal.get("delivery")
            if isinstance(proposal.get("delivery"), dict)
            else {}
        )
        authority = (
            proposal.get("authority")
            if isinstance(proposal.get("authority"), dict)
            else {}
        )
        owner_inboxes = state.get("owner_inboxes")
        scope_key = tenant_scope_storage_key(
            record.user_id,
            record.session_id,
        )
        surfaced_ids = (
            owner_inboxes.get(scope_key)
            if isinstance(owner_inboxes, dict)
            else None
        )
        if (
            proposal.get("schema_version")
            != SuggestionOutbox.PROPOSAL_SCHEMA_VERSION
            or proposal.get("proposal_id") != record.proposal_id
            or proposal.get("proposal_revision") != record.proposal_revision
            or expected_revision != record.proposal_revision
            or proposal.get("user_id") != record.user_id
            or proposal.get("session_id") != record.session_id
            or proposal.get("general_situation_id")
            != record.general_situation_id
            or proposal.get("parent_revision") != record.parent_revision
            or proposal.get("mode") != "advise_only"
            or proposal.get("status")
            not in {"pending", "acknowledged", "dismissed"}
            or not isinstance(surfaced_ids, list)
            or record.proposal_id not in surfaced_ids
            or delivery.get("channel") != "owner_scoped_console"
            or delivery.get("external_delivery") is not False
            or delivery.get("feishu_delivery") is not False
            or delivery.get("agent_delivery") is not False
            or any(
                authority.get(key) is not False
                for key in (
                    "execution_allowed",
                    "tool_allowed",
                    "agent_allowed",
                    "capability_grant_allowed",
                    "route_change_allowed",
                )
            )
        ):
            raise LearningCalibrationConflict(
                "feedback does not match the exact surfaced suggestion revision"
            )

    def _require_attention_state(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            raise LearningCalibrationStorageError(
                "Project Guardian Attention state is corrupt"
            )
        if (
            str(state.get("schema_version") or "")
            != self.ATTENTION_STATE_SCHEMA
        ):
            raise LearningCalibrationStorageError(
                "Project Guardian Attention state schema is unsupported"
            )
        if self._state_revision(state) < 1:
            raise LearningCalibrationStorageError(
                "Project Guardian Attention state revision is invalid"
            )
        if not isinstance(state.get("assessments"), dict):
            raise LearningCalibrationStorageError(
                "Project Guardian Attention assessments are invalid"
            )
        for candidate_id, assessment in state["assessments"].items():
            self._validate_attention_assessment(
                candidate_id,
                assessment,
            )

    def _validate_attention_assessment(
        self,
        map_candidate_id: Any,
        assessment: Any,
    ) -> None:
        if not isinstance(assessment, dict):
            raise LearningCalibrationStorageError(
                "Attention assessment must be an object"
            )
        required_fields = {
            "schema_version",
            "ruleset_version",
            "policy_version",
            "assessment_id",
            "candidate_ref",
            "user_id",
            "goal_id",
            "goal_revision",
            "scope",
            "components",
            "score",
            "thresholds",
            "would_disposition",
            "reason_codes",
            "blockers",
            "suppression",
            "analysis_mode",
            "agent_invoked",
            "shadow_only",
            "notification_allowed",
            "execution_allowed",
            "interrupt_eligible",
            "evaluated_at",
            "policy_revision",
            "assessment_revision",
            "runtime_mode",
            "attention_group_id",
            "recorded_at",
        }
        if set(assessment) != required_fields:
            raise LearningCalibrationStorageError(
                "Attention assessment fields do not match the persisted v1 contract"
            )
        scheduler = ProjectGuardianAttentionScheduler()
        if (
            assessment.get("schema_version")
            != scheduler.ASSESSMENT_SCHEMA_VERSION
            or assessment.get("ruleset_version")
            != scheduler.RULESET_VERSION
            or assessment.get("policy_version")
            != scheduler.POLICY_VERSION
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment contract version is unsupported"
            )

        candidate_ref = assessment.get("candidate_ref")
        if not isinstance(candidate_ref, dict) or set(candidate_ref) != {
            "candidate_id",
            "candidate_revision",
            "candidate_kind",
        }:
            raise LearningCalibrationStorageError(
                "Attention assessment candidate_ref is invalid"
            )
        candidate_id = self._bounded_identifier(
            candidate_ref.get("candidate_id"),
            "candidate_id",
            120,
        )
        candidate_revision = self._bounded_identifier(
            candidate_ref.get("candidate_revision"),
            "candidate_revision",
            120,
        )
        if (
            map_candidate_id != candidate_id
            or candidate_ref.get("candidate_kind")
            != ProjectGuardianEvaluator.CANDIDATE_KIND
            or re.fullmatch(r"pgc_[0-9a-f]{20}", candidate_id) is None
            or re.fullmatch(r"pgr_[0-9a-f]{20}", candidate_revision) is None
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment candidate binding is invalid"
            )

        user_id = self._bounded_identifier(
            assessment.get("user_id"),
            "user_id",
            240,
        )
        goal_id = self._bounded_identifier(
            assessment.get("goal_id"),
            "goal_id",
            240,
        )
        goal_revision = self._bounded_identifier(
            assessment.get("goal_revision"),
            "goal_revision",
            120,
        )
        scope = assessment.get("scope")
        if not isinstance(scope, dict) or set(scope) != set(
            ProjectGuardianEvaluator.SCOPE_FIELDS
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment scope is invalid"
            )
        normalized_scope = {
            field: self._bounded_identifier(
                scope.get(field),
                f"scope.{field}",
                240,
            )
            for field in ProjectGuardianEvaluator.SCOPE_FIELDS
        }
        expected_candidate_id = ProjectGuardianEvaluator.candidate_id_for(
            user_id=user_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            scope=normalized_scope,
        )
        if candidate_id != expected_candidate_id:
            raise LearningCalibrationStorageError(
                "Attention assessment candidate identity digest is invalid"
            )

        components = assessment.get("components")
        if not isinstance(components, dict) or set(components) != set(
            scheduler.COMPONENT_NAMES
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment components are invalid"
            )
        for name in scheduler.COMPONENT_NAMES:
            component = components[name]
            if (
                not isinstance(component, dict)
                or not {"status", "score", "reason_codes"}.issubset(component)
                or set(component)
                - {"status", "score", "reason_codes", "detail"}
            ):
                raise LearningCalibrationStorageError(
                    f"Attention component {name} is invalid"
                )
            status = component.get("status")
            score = component.get("score")
            if status == "known":
                self._unit_number(score, f"components.{name}.score")
            elif status == "unknown":
                if score is not None or "detail" in component:
                    raise LearningCalibrationStorageError(
                        f"Attention component {name} unknown value is invalid"
                    )
            else:
                raise LearningCalibrationStorageError(
                    f"Attention component {name} status is invalid"
                )
            self._bounded_code_list(
                component.get("reason_codes"),
                f"components.{name}.reason_codes",
                allow_empty=False,
            )
            if "detail" in component and not isinstance(
                component.get("detail"),
                dict,
            ):
                raise LearningCalibrationStorageError(
                    f"Attention component {name} detail is invalid"
                )

        expected_score = scheduler._score(components)
        score = assessment.get("score")
        if expected_score is None:
            if score is not None:
                raise LearningCalibrationStorageError(
                    "Attention assessment score is inconsistent"
                )
        elif self._unit_number(score, "score") != expected_score:
            raise LearningCalibrationStorageError(
                "Attention assessment score is inconsistent"
            )

        thresholds = assessment.get("thresholds")
        threshold_names = ("observe", "investigate", "suggest", "act")
        if not isinstance(thresholds, dict) or set(thresholds) != set(
            threshold_names
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment thresholds are invalid"
            )
        selected_thresholds = {
            name: self._unit_number(
                thresholds.get(name),
                f"thresholds.{name}",
            )
            for name in threshold_names
        }
        if not (
            selected_thresholds["observe"]
            < selected_thresholds["investigate"]
            < selected_thresholds["suggest"]
            <= selected_thresholds["act"]
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment threshold order is invalid"
            )

        blockers = self._bounded_code_list(
            assessment.get("blockers"),
            "blockers",
            allow_empty=True,
        )
        reason_codes = self._bounded_code_list(
            assessment.get("reason_codes"),
            "reason_codes",
            allow_empty=False,
        )
        suppression = assessment.get("suppression")
        if not isinstance(suppression, dict) or set(suppression) != {
            "key",
            "active",
            "primary_reason",
            "cooldown_until",
            "cooldown_seconds",
        }:
            raise LearningCalibrationStorageError(
                "Attention assessment suppression contract is invalid"
            )
        suppression_key = suppression.get("key")
        expected_suppression_key = scheduler.suppression_key_for(
            {
                "user_id": user_id,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "candidate_id": candidate_id,
                "candidate_kind": (
                    ProjectGuardianEvaluator.CANDIDATE_KIND
                ),
                "scope": normalized_scope,
            }
        )
        if (
            not isinstance(suppression_key, str)
            or re.fullmatch(r"pgas_[0-9a-f]{24}", suppression_key) is None
            or suppression_key != expected_suppression_key
            or type(suppression.get("active")) is not bool
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment suppression binding is invalid"
            )
        primary_reason = suppression.get("primary_reason")
        if primary_reason is not None and (
            not isinstance(primary_reason, str)
            or not primary_reason
            or len(primary_reason) > 160
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment primary reason is invalid"
            )
        cooldown_until = suppression.get("cooldown_until")
        if cooldown_until is not None:
            self._aware_timestamp(
                cooldown_until,
                "suppression.cooldown_until",
            )
        cooldown_seconds = suppression.get("cooldown_seconds")
        if (
            type(cooldown_seconds) is not int
            or not 60 <= cooldown_seconds <= 604800
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment cooldown is invalid"
            )
        active_suppressors = (
            [primary_reason]
            if primary_reason in scheduler.SUPPRESSION_PRECEDENCE
            else []
        )
        expected_disposition, expected_reason, threshold_reason = (
            scheduler._disposition(
                score=expected_score,
                thresholds=selected_thresholds,
                blockers=blockers,
                active_suppressors=active_suppressors,
            )
        )
        if (
            assessment.get("would_disposition") != expected_disposition
            or primary_reason
            != (
                expected_reason
                if expected_disposition == "suppressed"
                else None
            )
            or suppression.get("active")
            is not (expected_disposition == "suppressed")
            or reason_codes[0] != expected_reason
            or any(blocker not in reason_codes for blocker in blockers)
            or (
                threshold_reason is not None
                and threshold_reason not in reason_codes
            )
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment disposition is inconsistent"
            )

        if (
            assessment.get("analysis_mode")
            != "deterministic_read_only_shadow"
            or assessment.get("agent_invoked") is not False
            or assessment.get("shadow_only") is not True
            or assessment.get("notification_allowed") is not False
            or assessment.get("execution_allowed") is not False
            or assessment.get("interrupt_eligible") is not False
            or assessment.get("runtime_mode")
            not in {"record_only", "shadow"}
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment authority contract is invalid"
            )
        attention_group_id = self._bounded_identifier(
            assessment.get("attention_group_id"),
            "attention_group_id",
            120,
        )
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", attention_group_id) is None:
            raise LearningCalibrationStorageError(
                "Attention assessment group identity is invalid"
            )
        evaluated_at = self._aware_timestamp(
            assessment.get("evaluated_at"),
            "evaluated_at",
        )
        recorded_at = self._aware_timestamp(
            assessment.get("recorded_at"),
            "recorded_at",
        )
        if recorded_at < evaluated_at:
            raise LearningCalibrationStorageError(
                "Attention assessment was recorded before evaluation"
            )

        policy_revision = assessment.get("policy_revision")
        if (
            not isinstance(policy_revision, str)
            or re.fullmatch(r"pgap_[0-9a-f]{20}", policy_revision) is None
        ):
            raise LearningCalibrationStorageError(
                "Attention assessment policy revision is invalid"
            )
        expected_assessment_id = "pga_" + scheduler._digest(
            {
                "candidate_id": candidate_id,
                "candidate_revision": candidate_revision,
                "policy_revision": policy_revision,
            }
        )[:20]
        if assessment.get("assessment_id") != expected_assessment_id:
            raise LearningCalibrationStorageError(
                "Attention assessment identity digest is invalid"
            )
        revision_semantics = {
            key: value
            for key, value in assessment.items()
            if key
            not in {
                "assessment_id",
                "assessment_revision",
                "evaluated_at",
                "runtime_mode",
                "attention_group_id",
                "recorded_at",
            }
        }
        expected_revision = (
            "pgar_" + scheduler._digest(revision_semantics)[:20]
        )
        if assessment.get("assessment_revision") != expected_revision:
            raise LearningCalibrationStorageError(
                "Attention assessment revision digest is invalid"
            )

    @staticmethod
    def _bounded_identifier(
        value: Any,
        field: str,
        limit: int,
    ) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > limit
        ):
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} is invalid"
            )
        return value

    @staticmethod
    def _unit_number(value: Any, field: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} is invalid"
            )
        return round(float(value), 6)

    @staticmethod
    def _bounded_code_list(
        value: Any,
        field: str,
        *,
        allow_empty: bool,
    ) -> list[str]:
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or len(value) > 64
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or len(item) > 160
                for item in value
            )
            or len(set(value)) != len(value)
        ):
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} is invalid"
            )
        return list(value)

    @staticmethod
    def _aware_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} is invalid"
            )
        try:
            selected = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} is invalid"
            ) from exc
        if selected.tzinfo is None:
            raise LearningCalibrationStorageError(
                f"Attention assessment {field} must be timezone-aware"
            )
        return selected.astimezone(timezone.utc)

    def _require_state(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            raise LearningCalibrationStorageError(
                "learning calibration state is corrupt"
            )
        if str(state.get("schema_version") or "") != self.STATE_SCHEMA:
            raise LearningCalibrationStorageError(
                "learning calibration state schema is unsupported"
            )
        records = self._record_map(state)
        feedback_index = self._string_map(
            state.get("feedback_index"),
            "feedback_index",
        )
        active_by_binding = self._string_map(
            state.get("active_by_binding"),
            "active_by_binding",
        )
        if len(records) > self.MAX_RECORDS:
            raise LearningCalibrationStorageError(
                "learning calibration record capacity exceeded"
            )
        record_count = self._exact_nonnegative_count(
            state.get("record_count"),
            "record_count",
        )
        if record_count != len(records):
            raise LearningCalibrationStorageError(
                "learning calibration record_count is inconsistent"
            )
        active_records = {
            record.learning_id: record
            for record in records.values()
            if record.status == "active"
        }
        active_count = self._exact_nonnegative_count(
            state.get("active_count"),
            "active_count",
        )
        if active_count != len(active_records):
            raise LearningCalibrationStorageError(
                "learning calibration active_count is inconsistent"
            )
        if len(feedback_index) != len(records):
            raise LearningCalibrationStorageError(
                "learning calibration feedback index is inconsistent"
            )
        for feedback_id, learning_id in feedback_index.items():
            record = records.get(learning_id)
            if record is None or record.feedback_id != feedback_id:
                raise LearningCalibrationStorageError(
                    "learning calibration feedback index is invalid"
                )
        if set(active_by_binding.values()) != set(active_records):
            raise LearningCalibrationStorageError(
                "learning calibration active index is inconsistent"
            )
        for binding_digest, learning_id in active_by_binding.items():
            record = records.get(learning_id)
            if (
                record is None
                or record.status != "active"
                or record.binding_digest != binding_digest
            ):
                raise LearningCalibrationStorageError(
                    "learning calibration active binding is invalid"
                )
        for record in records.values():
            if record.status != "superseded":
                continue
            successor = records.get(
                str(record.superseded_by_learning_id or "")
            )
            if (
                successor is None
                or successor.supersedes_learning_id != record.learning_id
                or successor.binding_digest != record.binding_digest
            ):
                raise LearningCalibrationStorageError(
                    "learning correction chain is invalid"
                )
        if state.get("policy_effect") != "none":
            raise LearningCalibrationStorageError(
                "learning calibration cannot carry policy authority"
            )
        suggestion_records = self._suggestion_record_map(state)
        suggestion_feedback_index = self._optional_string_map(
            state.get("suggestion_feedback_index"),
            "suggestion_feedback_index",
        )
        suggestion_active_by_binding = self._optional_string_map(
            state.get("suggestion_active_by_binding"),
            "suggestion_active_by_binding",
        )
        if len(suggestion_records) > self.MAX_SUGGESTION_RECORDS:
            raise LearningCalibrationStorageError(
                "suggestion feedback record capacity exceeded"
            )
        suggestion_record_count = self._optional_nonnegative_count(
            state.get("suggestion_record_count"),
            "suggestion_record_count",
        )
        if suggestion_record_count != len(suggestion_records):
            raise LearningCalibrationStorageError(
                "suggestion feedback record_count is inconsistent"
            )
        active_suggestion_records = {
            record.learning_id: record
            for record in suggestion_records.values()
            if record.status == "active"
        }
        suggestion_active_count = self._optional_nonnegative_count(
            state.get("suggestion_active_count"),
            "suggestion_active_count",
        )
        if suggestion_active_count != len(active_suggestion_records):
            raise LearningCalibrationStorageError(
                "suggestion feedback active_count is inconsistent"
            )
        if len(suggestion_feedback_index) != len(suggestion_records):
            raise LearningCalibrationStorageError(
                "suggestion feedback index is inconsistent"
            )
        for feedback_id, learning_id in suggestion_feedback_index.items():
            record = suggestion_records.get(learning_id)
            if record is None or record.feedback_id != feedback_id:
                raise LearningCalibrationStorageError(
                    "suggestion feedback index is invalid"
                )
        if set(suggestion_active_by_binding.values()) != set(
            active_suggestion_records
        ):
            raise LearningCalibrationStorageError(
                "suggestion feedback active index is inconsistent"
            )
        for binding_digest, learning_id in suggestion_active_by_binding.items():
            record = suggestion_records.get(learning_id)
            if (
                record is None
                or record.status != "active"
                or record.binding_digest != binding_digest
            ):
                raise LearningCalibrationStorageError(
                    "suggestion feedback active binding is invalid"
                )
        for record in suggestion_records.values():
            if record.status != "superseded":
                continue
            successor = suggestion_records.get(
                str(record.superseded_by_learning_id or "")
            )
            if (
                successor is None
                or successor.supersedes_learning_id != record.learning_id
                or successor.binding_digest != record.binding_digest
            ):
                raise LearningCalibrationStorageError(
                    "suggestion feedback correction chain is invalid"
                )
        if state.get("suggestion_policy_effect", "none") != "none":
            raise LearningCalibrationStorageError(
                "suggestion feedback cannot carry policy authority"
            )

    def _record_map(
        self,
        state: dict[str, Any],
    ) -> dict[str, LearningRecord]:
        raw = state.get("records")
        if not isinstance(raw, dict):
            raise LearningCalibrationStorageError(
                "learning calibration records must be a mapping"
            )
        records: dict[str, LearningRecord] = {}
        try:
            for learning_id, value in raw.items():
                record = LearningRecord.from_dict(value)
                if learning_id != record.learning_id:
                    raise LearningRecordValidationError(
                        "learning record key does not match learning_id"
                    )
                records[record.learning_id] = record
        except (LearningRecordValidationError, TypeError, ValueError) as exc:
            raise LearningCalibrationStorageError(str(exc)) from exc
        return records

    def _suggestion_record_map(
        self,
        state: dict[str, Any],
    ) -> dict[str, SuggestionFeedbackRecord]:
        raw = state.get("suggestion_records", {})
        if not isinstance(raw, dict):
            raise LearningCalibrationStorageError(
                "suggestion feedback records must be a mapping"
            )
        records: dict[str, SuggestionFeedbackRecord] = {}
        try:
            for learning_id, value in raw.items():
                record = SuggestionFeedbackRecord.from_dict(value)
                if learning_id != record.learning_id:
                    raise LearningRecordValidationError(
                        "suggestion feedback key does not match learning_id"
                    )
                records[record.learning_id] = record
        except (LearningRecordValidationError, TypeError, ValueError) as exc:
            raise LearningCalibrationStorageError(str(exc)) from exc
        return records

    @staticmethod
    def _exact_nonnegative_count(value: Any, field: str) -> int:
        if type(value) is not int or value < 0:
            raise LearningCalibrationStorageError(
                f"{field} must be a non-negative integer"
            )
        return value

    @staticmethod
    def _optional_nonnegative_count(value: Any, field: str) -> int:
        if value is None:
            return 0
        return LearningCalibrationRuntime._exact_nonnegative_count(
            value,
            field,
        )

    @staticmethod
    def _string_map(value: Any, field: str) -> dict[str, str]:
        if not isinstance(value, dict):
            raise LearningCalibrationStorageError(
                f"{field} must be a mapping"
            )
        selected: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise LearningCalibrationStorageError(
                    f"{field} contains a non-string entry"
                )
            selected[key] = item
        return selected

    @staticmethod
    def _optional_string_map(value: Any, field: str) -> dict[str, str]:
        if value is None:
            return {}
        return LearningCalibrationRuntime._string_map(value, field)

    @staticmethod
    def _selected_identity(value: Any, field: str) -> str:
        selected = str(value or "").strip()
        if (
            not selected
            or len(selected) > 240
            or any(ord(character) < 32 for character in selected)
        ):
            raise ValueError(f"{field} is required and must be <= 240 chars")
        return selected

    @staticmethod
    def _state_revision(state: dict[str, Any]) -> int:
        value = state.get("_state_revision")
        if isinstance(value, bool) or not isinstance(value, int):
            return -1
        return value

    def _now(self) -> datetime:
        selected = self._clock()
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise ValueError(
                "learning calibration clock must be timezone-aware"
            )
        return selected.astimezone(timezone.utc)

    @staticmethod
    def _authority_contract() -> dict[str, Any]:
        return {
            "mode": "shadow_calibration",
            "explicit_feedback_only": True,
            "dismissal_inferred_as_usefulness": False,
            "free_text_persisted": False,
            "policy_effect": "none",
            "route_selection_allowed": False,
            "autonomy_change_allowed": False,
            "capability_grant_allowed": False,
            "provider_selection_allowed": False,
        }

    @staticmethod
    def _suggestion_authority_contract() -> dict[str, Any]:
        return {
            "mode": "suggestion_sandbox_calibration",
            "exact_owner_session": True,
            "explicit_feedback_only": True,
            "dismissal_inferred_as_usefulness": False,
            "free_text_persisted": False,
            "policy_effect": "none",
            "notification_allowed": False,
            "route_selection_allowed": False,
            "execution_allowed": False,
            "autonomy_change_allowed": False,
            "capability_grant_allowed": False,
            "provider_selection_allowed": False,
        }


__all__ = [
    "LearningCalibrationConflict",
    "LearningCalibrationError",
    "LearningCalibrationRuntime",
    "LearningCalibrationStorageError",
]
