#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)
from core.world_state import WorldStateStore
from runtime.learning_calibration_runtime import (
    LearningCalibrationConflict,
    LearningCalibrationRuntime,
    LearningCalibrationStorageError,
)


NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    error_type: type[BaseException],
    label: str,
    call: Any,
) -> None:
    try:
        call()
    except error_type:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: expected {error_type.__name__}")


def _assessment(index: int) -> dict[str, Any]:
    scheduler = ProjectGuardianAttentionScheduler()
    scope = {
        "workspace_id": "workspace-calibration",
        "repo_id": "repo-calibration",
        "target_ref": "refs/heads/main",
        "target_environment": "sandbox",
        "release_cycle": f"cycle-{index}",
    }
    candidate_id = ProjectGuardianEvaluator.candidate_id_for(
        user_id="user-calibration",
        goal_id="goal-calibration",
        goal_revision="goal-revision-1",
        scope=scope,
    )
    candidate_revision = (
        "pgr_"
        + scheduler._digest(
            {
                "fixture": "learning-calibration",
                "index": index,
            }
        )[:20]
    )
    policy_revision = (
        "pgap_"
        + scheduler._digest(
            {
                "fixture": "learning-calibration-policy",
                "index": index,
            }
        )[:20]
    )
    assessment_id = (
        "pga_"
        + scheduler._digest(
            {
                "candidate_id": candidate_id,
                "candidate_revision": candidate_revision,
                "policy_revision": policy_revision,
            }
        )[:20]
    )
    components = {
        name: {
            "status": "known",
            "score": 0.0,
            "reason_codes": ["fixture_zero"],
        }
        for name in scheduler.COMPONENT_NAMES
    }
    assessment = {
        "schema_version": scheduler.ASSESSMENT_SCHEMA_VERSION,
        "ruleset_version": scheduler.RULESET_VERSION,
        "policy_version": scheduler.POLICY_VERSION,
        "assessment_id": assessment_id,
        "candidate_ref": {
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
        },
        "user_id": "user-calibration",
        "goal_id": "goal-calibration",
        "goal_revision": "goal-revision-1",
        "scope": scope,
        "components": components,
        "score": 0.0,
        "thresholds": dict(scheduler.DEFAULT_THRESHOLDS),
        "would_disposition": "suppressed",
        "reason_codes": ["below_observe_threshold"],
        "blockers": [],
        "suppression": {
            "key": scheduler.suppression_key_for(
                {
                    "user_id": "user-calibration",
                    "goal_id": "goal-calibration",
                    "goal_revision": "goal-revision-1",
                    "candidate_id": candidate_id,
                    "candidate_kind": (
                        ProjectGuardianEvaluator.CANDIDATE_KIND
                    ),
                    "scope": scope,
                }
            ),
            "active": True,
            "primary_reason": "below_observe_threshold",
            "cooldown_until": None,
            "cooldown_seconds": scheduler.DEFAULT_COOLDOWN_SECONDS,
        },
        "analysis_mode": "deterministic_read_only_shadow",
        "shadow_only": True,
        "agent_invoked": False,
        "notification_allowed": False,
        "execution_allowed": False,
        "interrupt_eligible": False,
        "evaluated_at": NOW.isoformat(),
        "policy_revision": policy_revision,
        "runtime_mode": "shadow",
        "attention_group_id": "calibration-fixture",
        "recorded_at": NOW.isoformat(),
    }
    revision_semantics = {
        key: value
        for key, value in assessment.items()
        if key
        not in {
            "assessment_id",
            "evaluated_at",
            "runtime_mode",
            "attention_group_id",
            "recorded_at",
        }
    }
    assessment["assessment_revision"] = (
        "pgar_" + scheduler._digest(revision_semantics)[:20]
    )
    return assessment


def _seed_attention(store: WorldStateStore) -> None:
    rows = [_assessment(index) for index in range(1, 6)]
    assessments = {
        str(item["candidate_ref"]["candidate_id"]): item
        for item in rows
    }

    def update(state: dict[str, Any]) -> None:
        state["schema_version"] = (
            "veyra.project_guardian_attention_state.v1"
        )
        state["assessments"] = assessments
        state["assessment_count"] = len(assessments)

    store.mutate_json(
        "project_guardian_attention_state.json",
        update,
    )


def _feedback(
    runtime: LearningCalibrationRuntime,
    *,
    index: int,
    feedback_id: str,
    label: str,
    supersedes_learning_id: str | None = None,
) -> dict[str, Any]:
    assessment = _assessment(index)
    candidate_ref = assessment["candidate_ref"]
    return runtime.record_feedback(
        feedback_id=feedback_id,
        user_id="user-calibration",
        assessment_id=str(assessment["assessment_id"]),
        assessment_revision=str(assessment["assessment_revision"]),
        candidate_id=str(candidate_ref["candidate_id"]),
        candidate_revision=str(candidate_ref["candidate_revision"]),
        label=label,
        supersedes_learning_id=supersedes_learning_id,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        _seed_attention(store)
        runtime = LearningCalibrationRuntime(
            state_store=store,
            clock=lambda: NOW,
        )

        first = _feedback(
            runtime,
            index=1,
            feedback_id="feedback-1",
            label="useful",
        )
        before_duplicate = store.read_json(runtime.STATE_FILE)
        duplicate = _feedback(
            runtime,
            index=1,
            feedback_id="feedback-1",
            label="useful",
        )
        after_duplicate = store.read_json(runtime.STATE_FILE)
        expect(
            first["status"] == "recorded"
            and duplicate["status"] == "duplicate"
            and before_duplicate == after_duplicate,
            "exact feedback replay is idempotent without state churn",
            duplicate,
        )
        expect_raises(
            LearningCalibrationConflict,
            "one feedback id cannot be rebound to another category",
            lambda: _feedback(
                runtime,
                index=1,
                feedback_id="feedback-1",
                label="not_useful",
            ),
        )
        expect_raises(
            LearningCalibrationConflict,
            "a changed category requires an exact supersedes target",
            lambda: _feedback(
                runtime,
                index=1,
                feedback_id="feedback-2",
                label="not_useful",
            ),
        )

        corrected = _feedback(
            runtime,
            index=1,
            feedback_id="feedback-2",
            label="not_useful",
            supersedes_learning_id=first["record"]["learning_id"],
        )
        before_correction_replay = store.read_json(runtime.STATE_FILE)
        correction_replay = _feedback(
            runtime,
            index=1,
            feedback_id="feedback-2",
            label="not_useful",
            supersedes_learning_id=first["record"]["learning_id"],
        )
        expect(
            corrected["status"] == "corrected"
            and corrected["record"]["supersedes_learning_id"]
            == first["record"]["learning_id"],
            "correction supersedes the exact active learning record",
            corrected,
        )
        expect(
            correction_replay["status"] == "duplicate"
            and store.read_json(runtime.STATE_FILE)
            == before_correction_replay,
            "exact correction replay is idempotent",
            correction_replay,
        )
        _feedback(
            runtime,
            index=2,
            feedback_id="feedback-3",
            label="too_frequent",
        )
        _feedback(
            runtime,
            index=3,
            feedback_id="feedback-4",
            label="wrong_timing",
        )
        _feedback(
            runtime,
            index=4,
            feedback_id="feedback-5",
            label="wrong_evidence",
        )
        fifth_useful = _feedback(
            runtime,
            index=5,
            feedback_id="feedback-6",
            label="useful",
        )

        summary_before_dismissal = runtime.summary(
            user_id="user-calibration"
        )
        store.mutate_json(
            "project_guardian_attention_state.json",
            lambda state: state.update(
                {
                    "dismissals": {
                        "dismissed-candidate": {
                            "user_id": "user-calibration",
                            "reason": "dismissed",
                        }
                    }
                }
            ),
        )
        summary_after_dismissal = runtime.summary(
            user_id="user-calibration"
        )
        expect(
            summary_before_dismissal == summary_after_dismissal
            and summary_after_dismissal["active_feedback_count"] == 5
            and summary_after_dismissal[
                "usefulness_feedback_count"
            ]
            == 2
            and summary_after_dismissal[
                "diagnostic_feedback_count"
            ]
            == 3
            and summary_after_dismissal["counts"]
            == {
                "not_useful": 1,
                "too_frequent": 1,
                "useful": 1,
                "wrong_evidence": 1,
                "wrong_timing": 1,
            }
            and summary_after_dismissal["useful_rate"] == 0.5,
            "diagnostic categories stay outside the usefulness denominator and dismissal is not inferred",
            summary_after_dismissal,
        )

        fifth_candidate_id = str(
            _assessment(5)["candidate_ref"]["candidate_id"]
        )

        def rotate_fifth_assessment(state: dict[str, Any]) -> None:
            state["assessments"].pop(fifth_candidate_id, None)

        store.mutate_json(
            "project_guardian_attention_state.json",
            rotate_fifth_assessment,
        )
        before_rotated_replay = store.read_json(runtime.STATE_FILE)
        rotated_replay = _feedback(
            runtime,
            index=5,
            feedback_id="feedback-6",
            label="useful",
        )
        expect(
            rotated_replay["status"] == "duplicate"
            and store.read_json(runtime.STATE_FILE)
            == before_rotated_replay,
            "exact feedback replay survives Attention rotation without churn",
            rotated_replay,
        )
        expect_raises(
            LearningCalibrationConflict,
            "Attention rotation blocks a new feedback binding",
            lambda: _feedback(
                runtime,
                index=5,
                feedback_id="feedback-after-rotation",
                label="not_useful",
                supersedes_learning_id=(
                    fifth_useful["record"]["learning_id"]
                ),
            ),
        )

        expect_raises(
            LearningCalibrationConflict,
            "feedback cannot cross user identity",
            lambda: runtime.record_feedback(
                feedback_id="feedback-cross-user",
                user_id="other-user",
                assessment_id=_assessment(2)["assessment_id"],
                assessment_revision=(
                    _assessment(2)["assessment_revision"]
                ),
                candidate_id=(
                    _assessment(2)["candidate_ref"]["candidate_id"]
                ),
                candidate_revision=(
                    _assessment(2)["candidate_ref"][
                        "candidate_revision"
                    ]
                ),
                label="useful",
            ),
        )
        expect_raises(
            LearningCalibrationConflict,
            "feedback cannot bind a stale assessment revision",
            lambda: runtime.record_feedback(
                feedback_id="feedback-stale",
                user_id="user-calibration",
                assessment_id=_assessment(2)["assessment_id"],
                assessment_revision="pgar_stale",
                candidate_id=(
                    _assessment(2)["candidate_ref"]["candidate_id"]
                ),
                candidate_revision=(
                    _assessment(2)["candidate_ref"][
                        "candidate_revision"
                    ]
                ),
                label="useful",
            ),
        )
        expect_raises(
            LearningCalibrationConflict,
            "feedback cannot bind a different candidate revision",
            lambda: runtime.record_feedback(
                feedback_id="feedback-wrong-candidate",
                user_id="user-calibration",
                assessment_id=_assessment(2)["assessment_id"],
                assessment_revision=(
                    _assessment(2)["assessment_revision"]
                ),
                candidate_id=(
                    _assessment(2)["candidate_ref"]["candidate_id"]
                ),
                candidate_revision="pgr_00000000000000000000",
                label="useful",
            ),
        )
        expect_raises(
            ValueError,
            "dismissed is not silently accepted as usefulness feedback",
            lambda: _feedback(
                runtime,
                index=2,
                feedback_id="feedback-dismissed",
                label="dismissed",
            ),
        )

        persisted = store.read_json(runtime.STATE_FILE)
        persisted_text = json.dumps(
            persisted,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            persisted["policy_effect"] == "none"
            and corrected["authority"]["route_selection_allowed"] is False
            and corrected["authority"]["autonomy_change_allowed"] is False
            and corrected["authority"]["capability_grant_allowed"] is False
            and corrected["authority"]["provider_selection_allowed"] is False
            and corrected["authority"][
                "dismissal_inferred_as_usefulness"
            ]
            is False
            and all(
                field not in persisted_text
                for field in (
                    '"message"',
                    '"note"',
                    '"prompt"',
                    '"raw"',
                    "SECRET_FREE_TEXT",
                )
            ),
            "learning ledger is privacy-minimal and has no policy authority",
            persisted,
        )

        normal_store = WorldStateStore(
            Path(tmp) / "normal-unsuppressed-assessment"
        )
        _seed_attention(normal_store)
        normal_runtime = LearningCalibrationRuntime(
            state_store=normal_store,
            clock=lambda: NOW,
        )
        normal_assessment = deepcopy(_assessment(1))
        scheduler = ProjectGuardianAttentionScheduler()
        for name in scheduler.POSITIVE_COMPONENTS:
            normal_assessment["components"][name]["score"] = 0.3
        normal_assessment["score"] = scheduler._score(
            normal_assessment["components"]
        )
        normal_assessment["would_disposition"] = "observe"
        normal_assessment["reason_codes"] = [
            "observe_threshold_reached"
        ]
        normal_assessment["suppression"].update(
            active=False,
            primary_reason=None,
        )
        normal_assessment.pop("assessment_revision", None)
        normal_revision_semantics = {
            key: value
            for key, value in normal_assessment.items()
            if key
            not in {
                "assessment_id",
                "evaluated_at",
                "runtime_mode",
                "attention_group_id",
                "recorded_at",
            }
        }
        normal_assessment["assessment_revision"] = (
            "pgar_"
            + scheduler._digest(normal_revision_semantics)[:20]
        )
        normal_candidate_id = str(
            normal_assessment["candidate_ref"]["candidate_id"]
        )
        normal_store.mutate_json(
            "project_guardian_attention_state.json",
            lambda state: state["assessments"].update(
                {normal_candidate_id: normal_assessment}
            ),
        )
        normal_feedback = normal_runtime.record_feedback(
            feedback_id="feedback-unsuppressed",
            user_id="user-calibration",
            assessment_id=str(normal_assessment["assessment_id"]),
            assessment_revision=str(
                normal_assessment["assessment_revision"]
            ),
            candidate_id=normal_candidate_id,
            candidate_revision=str(
                normal_assessment["candidate_ref"][
                    "candidate_revision"
                ]
            ),
            label="useful",
        )
        expect(
            normal_feedback["status"] == "recorded",
            "valid unsuppressed Attention assessment is accepted",
            normal_feedback,
        )

        corruptions: list[tuple[str, Any]] = []

        def forge_map_key(state: dict[str, Any]) -> None:
            candidate_id = str(
                _assessment(1)["candidate_ref"]["candidate_id"]
            )
            row = state["assessments"].pop(candidate_id)
            state["assessments"]["forged-candidate-key"] = row

        corruptions.append(("forged assessment map key", forge_map_key))
        corruptions.append(
            (
                "tampered assessment score",
                lambda state: state["assessments"][
                    _assessment(1)["candidate_ref"]["candidate_id"]
                ].update({"score": 0.9}),
            )
        )
        corruptions.append(
            (
                "unsupported assessment ruleset",
                lambda state: state["assessments"][
                    _assessment(1)["candidate_ref"]["candidate_id"]
                ].update({"ruleset_version": "forged-ruleset"}),
            )
        )
        for index, (label, corrupt) in enumerate(
            corruptions,
            start=1,
        ):
            case_store = WorldStateStore(
                Path(tmp) / f"assessment-corruption-{index}"
            )
            _seed_attention(case_store)
            case_runtime = LearningCalibrationRuntime(
                state_store=case_store,
                clock=lambda: NOW,
            )
            case_store.mutate_json(
                "project_guardian_attention_state.json",
                corrupt,
            )
            expect_raises(
                LearningCalibrationStorageError,
                f"{label} fails feedback admission closed",
                lambda runtime=case_runtime: _feedback(
                    runtime,
                    index=2,
                    feedback_id=f"feedback-corrupt-{index}",
                    label="useful",
                ),
            )

        for index, invalid_count in enumerate(
            ([], "0", True, "not-an-int"),
            start=1,
        ):
            case_store = WorldStateStore(
                Path(tmp) / f"count-corruption-{index}"
            )
            _seed_attention(case_store)
            case_runtime = LearningCalibrationRuntime(
                state_store=case_store,
                clock=lambda: NOW,
            )
            case_store.mutate_json(
                case_runtime.STATE_FILE,
                lambda state, value=deepcopy(
                    invalid_count
                ): state.update(record_count=value),
            )
            degraded_count = case_runtime.status()
            expect(
                degraded_count["status"] == "degraded"
                and degraded_count["state_frozen"] is True,
                f"invalid record_count type {index} degrades without escaping",
                degraded_count,
            )
            expect_raises(
                LearningCalibrationStorageError,
                f"invalid record_count type {index} blocks new feedback",
                lambda runtime=case_runtime, case=index: _feedback(
                    runtime,
                    index=1,
                    feedback_id=f"feedback-invalid-count-{case}",
                    label="useful",
                ),
            )

        digest_store = WorldStateStore(
            Path(tmp) / "binding-digest-corruption"
        )
        _seed_attention(digest_store)
        digest_runtime = LearningCalibrationRuntime(
            state_store=digest_store,
            clock=lambda: NOW,
        )
        digest_record = _feedback(
            digest_runtime,
            index=1,
            feedback_id="feedback-before-digest-corruption",
            label="useful",
        )
        digest_store.mutate_json(
            digest_runtime.STATE_FILE,
            lambda state: state["records"][
                digest_record["record"]["learning_id"]
            ].update({"binding_digest": 123}),
        )
        degraded_digest = digest_runtime.status()
        expect(
            degraded_digest["status"] == "degraded"
            and degraded_digest["state_frozen"] is True,
            "invalid binding digest type degrades without escaping",
            degraded_digest,
        )
        expect_raises(
            LearningCalibrationStorageError,
            "invalid binding digest type blocks new feedback",
            lambda: _feedback(
                digest_runtime,
                index=2,
                feedback_id="feedback-after-digest-corruption",
                label="useful",
            ),
        )

        store.path_for(runtime.STATE_FILE).write_text(
            "{broken",
            encoding="utf-8",
        )
        degraded = runtime.status()
        expect(
            degraded["status"] == "degraded"
            and degraded["state_frozen"] is True,
            "corrupt learning state is reported as frozen and degraded",
            degraded,
        )
        expect_raises(
            LearningCalibrationStorageError,
            "corrupt learning state fails closed on new feedback",
            lambda: _feedback(
                runtime,
                index=2,
                feedback_id="feedback-after-corruption",
                label="useful",
            ),
        )

    print("learning calibration smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
