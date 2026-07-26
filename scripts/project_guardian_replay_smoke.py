#!/usr/bin/env python3
from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
import scripts.project_guardian_replay as replay_module
from scripts.project_guardian_replay import (
    EPISODES_SCHEMA,
    LABELS_SCHEMA,
    MANIFEST_SCHEMA,
    ProtocolError,
    REVIEWS_SCHEMA,
    file_sha256,
    predict,
    score,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
REPLAY_SCRIPT = ROOT / "scripts" / "project_guardian_replay.py"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_protocol_error(call: Any, label: str, contains: str) -> None:
    try:
        call()
    except ProtocolError as exc:
        expect(contains in str(exc), label, str(exc))
        return
    raise AssertionError(f"{label}: ProtocolError not raised")


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def scope(episode_id: str) -> dict[str, str]:
    return {
        "workspace_id": f"ws_{episode_id}",
        "repo_id": "wenjiesong04/veyra",
        "target_ref": "refs/heads/main",
        "target_environment": "production",
        "release_cycle": f"release_{episode_id}",
    }


def goal(episode_id: str) -> dict[str, Any]:
    return {
        "schema_version": ProjectGuardianEvaluator.GOAL_SCHEMA,
        "goal_id": f"goal_{episode_id}",
        "kind": ProjectGuardianEvaluator.GOAL_KIND,
        "status": "active",
        "user_id": "user-replay",
        "revision": "1",
        "state_revision": 1,
        "scope": scope(episode_id),
        "target_sha": "a" * 40,
        "active_from": (NOW - timedelta(hours=1)).isoformat(),
        "active_until": (NOW + timedelta(hours=1)).isoformat(),
        "source": ProjectGuardianEvaluator.GOAL_SOURCE,
    }


def signal_record(
    episode_id: str,
    kind: str,
    *,
    offset_minutes: int,
) -> tuple[str, dict[str, Any]]:
    evaluator = ProjectGuardianEvaluator
    component = evaluator.SIGNAL_COMPONENTS[kind]
    producer = evaluator.SIGNAL_PRODUCERS[kind]
    event_id = f"event_{episode_id}_{kind}"
    evidence_id = f"evidence_{episode_id}_{kind}"
    occurred_at = NOW - timedelta(minutes=offset_minutes)
    valid_until = NOW + timedelta(minutes=10)
    provenance_root = f"{component}:{episode_id}_{kind}"
    receipt_id = evaluator.producer_receipt_id_for(
        kind=kind,
        state="present",
        source_component=component,
        provenance_root=provenance_root,
        evidence_id=evidence_id,
        goal_id=f"goal_{episode_id}",
        goal_revision="1",
        scope=scope(episode_id),
        valid_until=valid_until.isoformat(),
        producer_id=producer["producer_id"],
        trust_class=producer["trust_class"],
        user_id="user-replay",
        session_id=f"session_{kind}",
        occurred_at=occurred_at.isoformat(),
    )
    return event_id, {
        "status": "recorded",
        "envelope": {
            "type": "observation",
            "event_id": event_id,
            "timestamp": occurred_at.isoformat(),
            "occurred_at": occurred_at.isoformat(),
            "source": {
                "channel": evaluator.SIGNAL_CHANNEL,
                "user_id": "user-replay",
                "session_id": f"session_{kind}",
            },
            "payload": {
                "schema_version": evaluator.SIGNAL_SCHEMA,
                "project_guardian_signal": {
                    "kind": kind,
                    "state": "present",
                    "source_component": component,
                    "provenance_root": provenance_root,
                    "evidence_id": evidence_id,
                    "goal_id": f"goal_{episode_id}",
                    "goal_revision": "1",
                    "scope": scope(episode_id),
                    "valid_until": valid_until.isoformat(),
                    "producer_attestation": {
                        "schema_version": (
                            evaluator.PRODUCER_ATTESTATION_SCHEMA
                        ),
                        "producer_id": producer["producer_id"],
                        "trust_class": producer["trust_class"],
                        "admission_source": (
                            "project_guardian_signal_ingress"
                        ),
                        "receipt_id": receipt_id,
                    },
                },
            },
            "evidence_refs": [
                {
                    "ref_id": evidence_id,
                    "source": component,
                    "is_fact": True,
                }
            ],
            "privacy_scope": "user",
        },
    }


def refresh_signal_receipt(record: dict[str, Any]) -> None:
    envelope = record["envelope"]
    signal = envelope["payload"]["project_guardian_signal"]
    attestation = signal["producer_attestation"]
    source = envelope["source"]
    attestation["receipt_id"] = (
        ProjectGuardianEvaluator.producer_receipt_id_for(
            kind=signal["kind"],
            state=signal["state"],
            source_component=signal["source_component"],
            provenance_root=signal["provenance_root"],
            evidence_id=signal["evidence_id"],
            goal_id=signal["goal_id"],
            goal_revision=signal["goal_revision"],
            scope=signal["scope"],
            valid_until=signal["valid_until"],
            producer_id=attestation["producer_id"],
            trust_class=attestation["trust_class"],
            user_id=source["user_id"],
            session_id=source["session_id"],
            occurred_at=envelope["occurred_at"],
        )
    )


def episode(
    episode_id: str,
    *,
    kinds: tuple[str, ...],
    source_class: str = "synthetic",
) -> dict[str, Any]:
    records = [
        signal_record(
            episode_id,
            kind,
            offset_minutes=index + 2,
        )
        for index, kind in enumerate(kinds)
    ]
    return {
        "episode_id": episode_id,
        "group_id": f"group_{episode_id}",
        "source_fingerprint": hashlib.sha256(
            f"source:{episode_id}".encode("utf-8")
        ).hexdigest(),
        "source_class": source_class,
        "evaluated_at": NOW.isoformat(),
        "goals_state": {
            "goals": [goal(episode_id)],
        },
        "event_inbox_state": {
            "schema_version": (
                "veyra.project_guardian_signal_frontier.v1"
            ),
            "events": {
                f"frontier_{event_id}": record
                for event_id, record in records
            },
        },
    }


def association(episode_id: str) -> dict[str, Any]:
    return {
        "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
        "user_id": "user-replay",
        "goal_id": f"goal_{episode_id}",
        "goal_revision": "1",
        "scope": scope(episode_id),
    }


def candidate_for(value: dict[str, Any]) -> dict[str, Any] | None:
    result = ProjectGuardianEvaluator().evaluate(
        goals_state=copy.deepcopy(value["goals_state"]),
        event_inbox_state=copy.deepcopy(value["event_inbox_state"]),
        now=value["evaluated_at"],
    )
    candidates = result["candidates"]
    return candidates[0] if candidates else None


def expected_candidate(
    episode_id: str,
    *,
    evidence_kinds: tuple[str, str] = ("git_dirty", "ci_failed"),
) -> dict[str, Any]:
    return {
        "association": association(episode_id),
        "evidence_ref_ids": sorted(
            f"event:event_{episode_id}_{kind}"
            for kind in evidence_kinds
        ),
    }


def dual_review(
    episode_id: str,
    candidate_revision: str,
    *,
    useful: bool = True,
) -> dict[str, Any]:
    return {
        "association": association(episode_id),
        "candidate_revision": candidate_revision,
        "ratings": [
            {"rater_id": "reviewer-a", "useful": useful},
            {"rater_id": "reviewer-b", "useful": useful},
        ],
    }


def adjudicated_review(
    episode_id: str,
    candidate_revision: str,
    *,
    useful: bool,
) -> dict[str, Any]:
    return {
        "association": association(episode_id),
        "candidate_revision": candidate_revision,
        "ratings": [
            {"rater_id": "reviewer-a", "useful": not useful},
        ],
        "adjudication": {
            "adjudicator_id": "reviewer-adjudicator",
            "useful": useful,
            "reason_code": "evidence_and_actionability_review",
        },
    }


def seal_dataset(
    root: Path,
    *,
    name: str,
    episode_values: list[dict[str, Any]],
    label_values: list[dict[str, Any]],
    data_class: str = "synthetic",
) -> dict[str, Any]:
    selected = root / name
    selected.mkdir()
    episodes_path = selected / "episodes.json"
    labels_path = selected / "labels.json"
    reviews_path = selected / "reviews.json"
    manifest_path = selected / "manifest.json"
    predictions_path = selected / "predictions.json"
    report_path = selected / "report.json"
    dataset_id = f"dataset_{name}"
    write_json(
        episodes_path,
        {
            "schema_version": EPISODES_SCHEMA,
            "dataset_id": dataset_id,
            "episodes": episode_values,
        },
    )
    gold_labels: list[dict[str, Any]] = []
    pending_reviews: list[dict[str, Any]] = []
    for raw_label in label_values:
        label = copy.deepcopy(raw_label)
        human_reviews = label.pop("human_reviews", [])
        gold_labels.append(label)
        pending_reviews.append(
            {
                "episode_id": label["episode_id"],
                "human_reviews": human_reviews,
            }
        )
    write_json(
        labels_path,
        {
            "schema_version": LABELS_SCHEMA,
            "dataset_id": dataset_id,
            "episodes": gold_labels,
        },
    )
    write_json(
        manifest_path,
        {
            "schema_version": MANIFEST_SCHEMA,
            "dataset_id": dataset_id,
            "split": "held_out",
            "data_class": data_class,
            "anonymization_version": "synthetic-opaque-v1",
            "label_policy_version": "guardian-label-policy-v1",
            "episodes_sha256": file_sha256(episodes_path),
            "labels_sha256": file_sha256(labels_path),
        },
    )
    return {
        "root": selected,
        "episodes": episodes_path,
        "labels": labels_path,
        "reviews": reviews_path,
        "manifest": manifest_path,
        "predictions": predictions_path,
        "report": report_path,
        "pending_reviews": pending_reviews,
    }


def run_predict(paths: dict[str, Any]) -> dict[str, Any]:
    return predict(
        manifest_path=paths["manifest"],
        episodes_path=paths["episodes"],
        output_path=paths["predictions"],
    )


def seal_reviews(paths: dict[str, Any]) -> None:
    prediction_artifact = json.loads(
        paths["predictions"].read_text(encoding="utf-8")
    )
    write_json(
        paths["reviews"],
        {
            "schema_version": REVIEWS_SCHEMA,
            "dataset_id": prediction_artifact["dataset_id"],
            "prediction_set_sha256": prediction_artifact[
                "prediction_set_sha256"
            ],
            "episodes": copy.deepcopy(paths["pending_reviews"]),
        },
    )


def run_score(paths: dict[str, Any]) -> dict[str, Any]:
    if not paths["reviews"].exists():
        seal_reviews(paths)
    return score(
        manifest_path=paths["manifest"],
        episodes_path=paths["episodes"],
        predictions_path=paths["predictions"],
        labels_path=paths["labels"],
        reviews_path=paths["reviews"],
        output_path=paths["report"],
    )


def test_label_blind_exact_scoring(root: Path) -> dict[str, Any]:
    tp_episode = episode(
        "tp",
        kinds=("git_dirty", "ci_failed"),
    )
    fp_episode = episode(
        "fp",
        kinds=("git_dirty", "deployment_intent"),
    )
    fn_episode = episode("fn", kinds=("git_dirty",))
    candidate = candidate_for(tp_episode)
    expect(candidate is not None, "fixture positive qualifies")
    paths = seal_dataset(
        root,
        name="exact-scoring",
        episode_values=[tp_episode, fp_episode, fn_episode],
        label_values=[
            {
                "episode_id": "tp",
                "expected_candidates": [expected_candidate("tp")],
                "human_reviews": [
                    dual_review("tp", str(candidate["candidate_revision"]))
                ],
            },
            {
                "episode_id": "fp",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "fn",
                "expected_candidates": [expected_candidate("fn")],
                "human_reviews": [],
            },
        ],
    )
    prediction_artifact = run_predict(paths)
    report = run_score(paths)
    expect(
        prediction_artifact["prediction_count"] == 2
        and prediction_artifact["evaluator_ruleset"]
        == ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION
        and report["evaluator_ruleset"]
        == ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION,
        "predict emits evaluator results only",
        prediction_artifact,
    )
    expect(
        report["counts"]["tp"] == 1
        and report["counts"]["fp"] == 1
        and report["counts"]["fn"] == 1,
        "score reports exact TP FP FN",
        report["counts"],
    )
    expect(
        report["counts"]["false_association_count"] == 1,
        "unmatched prediction is false association",
        report["counts"],
    )
    expect(
        report["counts"]["evidence_correct"] == 1
        and report["counts"]["evidence_incorrect"] == 1
        and report["metrics"]["evidence_correctness"] == 0.5,
        "evidence correctness uses exact frozen sets",
        report,
    )
    expect(
        report["metrics"]["precision"] == 0.5
        and report["metrics"]["recall"] == 0.5,
        "precision and recall have no denominator smoothing",
        report["metrics"],
    )
    expect(
        report["support"]["independent_group_count"] == 3
        and report["support"]["positive_group_count"] == 2
        and report["support"]["negative_group_count"] == 1
        and report["support"]["human_reviewed_group_count"] == 1,
        "support is counted by independent groups",
        report["support"],
    )
    expect(
        report["validation_state"] == "not_ready"
        and "corpus_not_real_project" in report["not_ready_reasons"]
        and "positive_group_support_below_minimum"
        in report["not_ready_reasons"]
        and "negative_group_support_below_minimum"
        in report["not_ready_reasons"],
        "synthetic low-support corpus stays not_ready",
        report["not_ready_reasons"],
    )
    return paths


def test_perfect_synthetic_stays_not_ready(root: Path) -> None:
    positive = episode(
        "perfect-positive",
        kinds=("git_dirty", "ci_failed"),
    )
    negative = episode("perfect-negative", kinds=("git_dirty",))
    candidate = candidate_for(positive)
    expect(candidate is not None, "perfect fixture candidate exists")
    paths = seal_dataset(
        root,
        name="perfect-synthetic",
        episode_values=[positive, negative],
        label_values=[
            {
                "episode_id": "perfect-positive",
                "expected_candidates": [
                    expected_candidate("perfect-positive")
                ],
                "human_reviews": [
                    adjudicated_review(
                        "perfect-positive",
                        str(candidate["candidate_revision"]),
                        useful=True,
                    )
                ],
            },
            {
                "episode_id": "perfect-negative",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    run_predict(paths)
    frozen_labels = json.loads(
        paths["labels"].read_text(encoding="utf-8")
    )
    expect(
        not paths["reviews"].exists()
        and all(
            "human_reviews" not in item
            for item in frozen_labels["episodes"]
        ),
        "gold labels exclude reviews before frozen predictions",
    )
    seal_reviews(paths)
    report = run_score(paths)
    expect(
        report["metrics"]
        == {
            "precision": 1.0,
            "recall": 1.0,
            "evidence_correctness": 1.0,
            "human_usefulness": 1.0,
        },
        "perfect synthetic metrics remain measurable",
        report["metrics"],
    )
    expect(
        report["validation_state"] == "not_ready"
        and "corpus_not_real_project" in report["not_ready_reasons"],
        "perfect synthetic corpus can never become ready",
        report["not_ready_reasons"],
    )
    expect(
        report["counts"]["human_useful"] == 1,
        "independent adjudication resolves usefulness",
        report["counts"],
    )


def test_empty_is_not_ready(root: Path) -> None:
    paths = seal_dataset(
        root,
        name="empty",
        episode_values=[],
        label_values=[],
    )
    run_predict(paths)
    report = run_score(paths)
    expect(
        report["validation_state"] == "not_ready"
        and "empty_dataset" in report["not_ready_reasons"]
        and report["metrics"]["precision"] is None
        and report["metrics"]["recall"] is None,
        "empty replay is explicitly not_ready with undefined ratios",
        report,
    )


def test_integrity_binding(root: Path) -> None:
    positive = episode(
        "integrity",
        kinds=("git_dirty", "ci_failed"),
    )
    candidate = candidate_for(positive)
    paths = seal_dataset(
        root,
        name="integrity",
        episode_values=[positive],
        label_values=[
            {
                "episode_id": "integrity",
                "expected_candidates": [expected_candidate("integrity")],
                "human_reviews": [
                    dual_review(
                        "integrity",
                        str(candidate["candidate_revision"]),
                    )
                ],
            }
        ],
    )
    run_predict(paths)
    seal_reviews(paths)

    episodes_bytes = paths["episodes"].read_bytes()
    paths["episodes"].write_bytes(episodes_bytes + b"\n")
    expect_protocol_error(
        lambda: predict(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            output_path=paths["root"] / "tampered-episodes-predictions.json",
        ),
        "episode byte mutation breaks manifest binding",
        "episodes SHA-256",
    )
    paths["episodes"].write_bytes(episodes_bytes)

    manifest_bytes = paths["manifest"].read_bytes()
    paths["manifest"].write_bytes(manifest_bytes + b"\n")
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=paths["predictions"],
            labels_path=paths["labels"],
            reviews_path=paths["reviews"],
            output_path=paths["root"] / "tampered-manifest-report.json",
        ),
        "manifest byte mutation invalidates frozen predictions",
        "not bound to this manifest",
    )
    paths["manifest"].write_bytes(manifest_bytes)

    prediction_value = json.loads(
        paths["predictions"].read_text(encoding="utf-8")
    )
    prediction_value["prediction_count"] += 1
    tampered_predictions = paths["root"] / "tampered-predictions.json"
    write_json(tampered_predictions, prediction_value)
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=tampered_predictions,
            labels_path=paths["labels"],
            reviews_path=paths["reviews"],
            output_path=paths["root"] / "tampered-predictions-report.json",
        ),
        "prediction mutation breaks frozen prediction hash",
        "frozen predictions SHA-256",
    )

    stale_ruleset = json.loads(
        paths["predictions"].read_text(encoding="utf-8")
    )
    stale_ruleset["evaluator_ruleset"] = "veyra.project_guardian_ruleset.old"
    stale_ruleset_path = paths["root"] / "stale-ruleset-predictions.json"
    write_json(stale_ruleset_path, stale_ruleset)
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=stale_ruleset_path,
            labels_path=paths["labels"],
            reviews_path=paths["reviews"],
            output_path=paths["root"] / "stale-ruleset-report.json",
        ),
        "frozen predictions require the current evaluator ruleset",
        "evaluator ruleset does not match",
    )

    labels_bytes = paths["labels"].read_bytes()
    paths["labels"].write_bytes(labels_bytes + b"\n")
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=paths["predictions"],
            labels_path=paths["labels"],
            reviews_path=paths["reviews"],
            output_path=paths["root"] / "tampered-labels-report.json",
        ),
        "label byte mutation breaks manifest binding",
        "labels SHA-256",
    )
    paths["labels"].write_bytes(labels_bytes)

    review_value = json.loads(
        paths["reviews"].read_text(encoding="utf-8")
    )
    review_value["prediction_set_sha256"] = "b" * 64
    stale_reviews_path = paths["root"] / "stale-reviews.json"
    write_json(stale_reviews_path, review_value)
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=paths["predictions"],
            labels_path=paths["labels"],
            reviews_path=stale_reviews_path,
            output_path=paths["root"] / "stale-reviews-report.json",
        ),
        "post-prediction reviews bind the frozen prediction set",
        "reviews are not bound",
    )

    expect_protocol_error(
        lambda: predict(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            output_path=paths["predictions"],
        ),
        "frozen prediction artifact cannot be overwritten",
        "refusing to overwrite",
    )


def test_score_recomputes_frozen_predictions(root: Path) -> None:
    negative = episode("forged", kinds=("git_dirty",))
    paths = seal_dataset(
        root,
        name="forged-predictions",
        episode_values=[negative],
        label_values=[
            {
                "episode_id": "forged",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    forged_prediction = {
        "candidate_id": "forged_candidate",
        "candidate_revision": "forged_revision",
        "association": association("forged"),
        "evidence_ref_ids": [
            "event:forged_evidence_a",
            "event:forged_evidence_b",
        ],
        "review_payload": {
            "why_now": {},
            "unknowns": [],
            "candidate_advice": {},
        },
        "agent_invoked": False,
        "shadow_only": True,
        "notification_allowed": False,
        "execution_allowed": False,
        "interrupt_eligible": False,
    }
    artifact = {
        "schema_version": replay_module.PREDICTIONS_SCHEMA,
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": file_sha256(paths["manifest"]),
        "episodes_sha256": file_sha256(paths["episodes"]),
        "evaluator_contract": ProjectGuardianEvaluator.CANDIDATE_SCHEMA,
        "evaluator_ruleset": (
            ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION
        ),
        "episode_count": 1,
        "prediction_count": 1,
        "episodes": [
            {
                "episode_id": "forged",
                "predictions": [forged_prediction],
            }
        ],
    }
    artifact["prediction_set_sha256"] = replay_module._json_sha256(
        artifact
    )
    write_json(paths["predictions"], artifact)
    expect_protocol_error(
        lambda: score(
            manifest_path=paths["manifest"],
            episodes_path=paths["episodes"],
            predictions_path=paths["predictions"],
            labels_path=paths["labels"],
            reviews_path=paths["reviews"],
            output_path=paths["report"],
        ),
        (
            "score recomputes evaluator output before labels and rejects a "
            "self-hashed forged prediction artifact"
        ),
        "do not match current evaluator output",
    )
    expect(
        not paths["report"].exists(),
        "forged predictions cannot create a gate report",
    )


def test_group_independence_and_canonical_whitelist(root: Path) -> None:
    first = episode(
        "canonical-a",
        kinds=("git_dirty", "ci_failed"),
    )
    alias = copy.deepcopy(first)
    alias["episode_id"] = "canonical-b"
    alias["group_id"] = "group_canonical-b"
    alias["source_fingerprint"] = hashlib.sha256(
        b"source:canonical-b"
    ).hexdigest()
    alias_events = alias["event_inbox_state"]["events"]
    alias["event_inbox_state"]["events"] = {
        f"frontier-alias-{index}": value
        for index, value in enumerate(alias_events.values())
    }
    paths = seal_dataset(
        root,
        name="duplicate-canonical-input",
        episode_values=[first, alias],
        label_values=[
            {
                "episode_id": "canonical-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "canonical-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "episode aliases cannot duplicate semantic evaluator input",
        "duplicate semantic evaluator input",
    )

    semantic_first = episode(
        "semantic-a",
        kinds=("git_dirty", "ci_failed"),
    )
    semantic_alias = copy.deepcopy(semantic_first)
    semantic_alias["episode_id"] = "semantic-b"
    semantic_alias["group_id"] = "group_semantic-b"
    semantic_alias["source_fingerprint"] = hashlib.sha256(
        b"source:semantic-b"
    ).hexdigest()
    semantic_alias["evaluated_at"] = (
        NOW + timedelta(minutes=1)
    ).isoformat()
    alias_goal = semantic_alias["goals_state"]["goals"][0]
    alias_goal["state_revision"] = 2
    alias_goal["target_sha"] = "b" * 40
    alias_goal["active_from"] = (NOW - timedelta(hours=2)).isoformat()
    alias_goal["active_until"] = (NOW + timedelta(hours=2)).isoformat()
    for record in semantic_alias["event_inbox_state"]["events"].values():
        signal = record["envelope"]["payload"][
            "project_guardian_signal"
        ]
        signal["valid_until"] = (
            NOW + timedelta(minutes=20)
        ).isoformat()
        refresh_signal_receipt(record)
    paths = seal_dataset(
        root,
        name="semantic-decision-alias",
        episode_values=[semantic_first, semantic_alias],
        label_values=[
            {
                "episode_id": "semantic-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "semantic-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        (
            "validation-only Goal fields and equivalent time windows cannot "
            "inflate semantic support"
        ),
        "duplicate semantic evaluator input",
    )

    shifted_first = episode(
        "time-shift-a",
        kinds=("git_dirty", "ci_failed"),
    )
    shifted_alias = copy.deepcopy(shifted_first)
    shifted_alias["episode_id"] = "time-shift-b"
    shifted_alias["group_id"] = "group_time-shift-b"
    shifted_alias["source_fingerprint"] = hashlib.sha256(
        b"source:time-shift-b"
    ).hexdigest()

    def shift_one_day(value: str) -> str:
        return (
            datetime.fromisoformat(value) + timedelta(days=1)
        ).isoformat()

    shifted_alias["evaluated_at"] = shift_one_day(
        shifted_alias["evaluated_at"]
    )
    for shifted_goal in shifted_alias["goals_state"]["goals"]:
        shifted_goal["active_from"] = shift_one_day(
            shifted_goal["active_from"]
        )
        shifted_goal["active_until"] = shift_one_day(
            shifted_goal["active_until"]
        )
    for record in shifted_alias["event_inbox_state"]["events"].values():
        envelope = record["envelope"]
        signal = envelope["payload"]["project_guardian_signal"]
        envelope["timestamp"] = shift_one_day(envelope["timestamp"])
        envelope["occurred_at"] = shift_one_day(envelope["occurred_at"])
        signal["valid_until"] = shift_one_day(signal["valid_until"])
        refresh_signal_receipt(record)
    paths = seal_dataset(
        root,
        name="global-time-translation",
        episode_values=[shifted_first, shifted_alias],
        label_values=[
            {
                "episode_id": "time-shift-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "time-shift-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "global timestamp translation cannot inflate semantic support",
        "duplicate semantic evaluator input",
    )

    ignored_goal_first = episode(
        "ignored-goal-a",
        kinds=("git_dirty", "ci_failed"),
    )
    ignored_goal_alias = copy.deepcopy(ignored_goal_first)
    ignored_goal_alias["episode_id"] = "ignored-goal-b"
    ignored_goal_alias["group_id"] = "group_ignored-goal-b"
    ignored_goal_alias["source_fingerprint"] = hashlib.sha256(
        b"source:ignored-goal-b"
    ).hexdigest()
    paused_goal = goal("ignored-paused")
    paused_goal["status"] = "paused"
    future_goal = goal("ignored-future")
    future_goal["active_from"] = (NOW + timedelta(hours=2)).isoformat()
    future_goal["active_until"] = (NOW + timedelta(hours=3)).isoformat()
    unmatched_active_goal = goal("ignored-active")
    ignored_goal_alias["goals_state"]["goals"].extend(
        [paused_goal, future_goal, unmatched_active_goal]
    )
    paths = seal_dataset(
        root,
        name="ignored-goal-alias",
        episode_values=[ignored_goal_first, ignored_goal_alias],
        label_values=[
            {
                "episode_id": "ignored-goal-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "ignored-goal-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "Goals skipped by the evaluator cannot inflate semantic support",
        "duplicate semantic evaluator input",
    )

    duplicate_goal = episode(
        "duplicate-goal",
        kinds=("git_dirty",),
    )
    duplicate_goal["goals_state"]["goals"].append(
        copy.deepcopy(duplicate_goal["goals_state"]["goals"][0])
    )
    paths = seal_dataset(
        root,
        name="duplicate-controlled-goal",
        episode_values=[duplicate_goal],
        label_values=[
            {
                "episode_id": "duplicate-goal",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "duplicate controlled Goal identities cannot inflate support",
        "duplicate controlled Goal goal_id",
    )

    stale_frontier = episode(
        "stale-frontier",
        kinds=("git_dirty", "ci_failed"),
    )
    existing_git_record = next(
        record
        for record in stale_frontier["event_inbox_state"]["events"].values()
        if record["envelope"]["payload"]["project_guardian_signal"]["kind"]
        == "git_dirty"
    )
    older_git_record = copy.deepcopy(existing_git_record)
    older_envelope = older_git_record["envelope"]
    older_signal = older_envelope["payload"]["project_guardian_signal"]
    older_envelope["event_id"] = "event_stale-frontier_git_dirty_older"
    older_envelope["timestamp"] = (NOW - timedelta(minutes=8)).isoformat()
    older_envelope["occurred_at"] = older_envelope["timestamp"]
    older_signal["provenance_root"] = (
        "git_probe:stale-frontier_git_dirty_older"
    )
    older_signal["evidence_id"] = (
        "evidence_stale-frontier_git_dirty_older"
    )
    older_envelope["evidence_refs"][0]["ref_id"] = older_signal[
        "evidence_id"
    ]
    refresh_signal_receipt(older_git_record)
    stale_frontier["event_inbox_state"]["events"][
        "frontier_stale_git_older"
    ] = older_git_record
    paths = seal_dataset(
        root,
        name="noncanonical-stale-frontier",
        episode_values=[stale_frontier],
        label_values=[
            {
                "episode_id": "stale-frontier",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "older same-kind records cannot inflate compact frontier support",
        "at most one current record",
    )

    timezone_first = episode(
        "timezone-a",
        kinds=("git_dirty", "ci_failed"),
    )
    timezone_alias = copy.deepcopy(timezone_first)
    timezone_alias["episode_id"] = "timezone-b"
    timezone_alias["group_id"] = "group_timezone-b"
    timezone_alias["source_fingerprint"] = hashlib.sha256(
        b"source:timezone-b"
    ).hexdigest()
    timezone_alias["evaluated_at"] = NOW.astimezone(
        timezone(timedelta(hours=8))
    ).isoformat()
    paths = seal_dataset(
        root,
        name="timezone-alias",
        episode_values=[timezone_first, timezone_alias],
        label_values=[
            {
                "episode_id": "timezone-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "timezone-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "equivalent timezone aliases cannot inflate independent support",
        "canonical UTC",
    )

    status_first = episode(
        "status-a",
        kinds=("git_dirty", "ci_failed"),
    )
    status_alias = copy.deepcopy(status_first)
    status_alias["episode_id"] = "status-b"
    status_alias["group_id"] = "group_status-b"
    status_alias["source_fingerprint"] = hashlib.sha256(
        b"source:status-b"
    ).hexdigest()
    for record in status_alias["event_inbox_state"]["events"].values():
        record["status"] = "pending"
    paths = seal_dataset(
        root,
        name="frontier-status-alias",
        episode_values=[status_first, status_alias],
        label_values=[
            {
                "episode_id": "status-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "status-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "frontier status aliases cannot inflate independent support",
        "status must be recorded",
    )

    timestamp_value = episode(
        "timestamp-alias",
        kinds=("git_dirty", "ci_failed"),
    )
    first_record = next(
        iter(timestamp_value["event_inbox_state"]["events"].values())
    )
    first_record["envelope"]["timestamp"] = (
        datetime.fromisoformat(first_record["envelope"]["occurred_at"])
        + timedelta(seconds=1)
    ).isoformat()
    paths = seal_dataset(
        root,
        name="frontier-timestamp-alias",
        episode_values=[timestamp_value],
        label_values=[
            {
                "episode_id": "timestamp-alias",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "ignored envelope timestamp aliases are rejected",
        "timestamp must equal occurred_at",
    )

    invalid_goal_status = episode(
        "invalid-goal-status",
        kinds=("git_dirty", "ci_failed"),
    )
    invalid_goal_status["goals_state"]["goals"][0]["status"] = (
        "active_alias"
    )
    paths = seal_dataset(
        root,
        name="invalid-goal-status",
        episode_values=[invalid_goal_status],
        label_values=[
            {
                "episode_id": "invalid-goal-status",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "unsupported Goal status strings cannot create negative support",
        "unsupported Goal status",
    )

    invalid_signal_contract = episode(
        "invalid-signal-contract",
        kinds=("git_dirty", "ci_failed"),
    )
    first_record = next(
        iter(
            invalid_signal_contract["event_inbox_state"]["events"].values()
        )
    )
    first_record["envelope"]["payload"]["schema_version"] = (
        "veyra.project_guardian_signal.alias"
    )
    paths = seal_dataset(
        root,
        name="invalid-signal-contract",
        episode_values=[invalid_signal_contract],
        label_values=[
            {
                "episode_id": "invalid-signal-contract",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "invalid signal contracts cannot create negative support",
        "exactly one evaluator-accepted signal per event",
    )

    one = episode("group-a", kinds=("git_dirty",))
    two = episode("group-b", kinds=("git_dirty",))
    two["group_id"] = one["group_id"]
    paths = seal_dataset(
        root,
        name="duplicate-group",
        episode_values=[one, two],
        label_values=[
            {
                "episode_id": "group-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "group-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "support groups must be independent and unique",
        "duplicate group_id",
    )

    one = episode("fingerprint-a", kinds=("git_dirty",))
    two = episode("fingerprint-b", kinds=("git_dirty",))
    two["source_fingerprint"] = one["source_fingerprint"]
    paths = seal_dataset(
        root,
        name="duplicate-source-fingerprint",
        episode_values=[one, two],
        label_values=[
            {
                "episode_id": "fingerprint-a",
                "expected_candidates": [],
                "human_reviews": [],
            },
            {
                "episode_id": "fingerprint-b",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "source fingerprints must be unique",
        "duplicate source_fingerprint",
    )

    whitelist_mutations: list[tuple[str, Any]] = [
        (
            "Goal",
            lambda value: value["goals_state"]["goals"][0].__setitem__(
                "private_note",
                "secret",
            ),
        ),
        (
            "event",
            lambda value: next(
                iter(value["event_inbox_state"]["events"].values())
            ).__setitem__("private_note", "secret"),
        ),
        (
            "signal",
            lambda value: next(
                iter(value["event_inbox_state"]["events"].values())
            )["envelope"]["payload"]["project_guardian_signal"].__setitem__(
                "private_note",
                "secret",
            ),
        ),
    ]
    for index, (label, mutate) in enumerate(whitelist_mutations):
        value = episode(
            f"whitelist-{index}",
            kinds=("git_dirty", "ci_failed"),
        )
        mutate(value)
        paths = seal_dataset(
            root,
            name=f"strict-whitelist-{index}",
            episode_values=[value],
            label_values=[
                {
                    "episode_id": f"whitelist-{index}",
                    "expected_candidates": [],
                    "human_reviews": [],
                }
            ],
        )
        expect_protocol_error(
            lambda selected=paths: run_predict(selected),
            f"{label} input rejects unknown fields",
            "unexpected fields",
        )


def test_leakage_rejection(root: Path) -> None:
    base = episode("leak", kinds=("git_dirty", "ci_failed"))
    for index, forbidden in enumerate(
        ("raw_text", "diff", "raw_log", "file_path", "path")
    ):
        leaked = copy.deepcopy(base)
        leaked[forbidden] = "secret"
        paths = seal_dataset(
            root,
            name=f"leak-{index}",
            episode_values=[leaked],
            label_values=[
                {
                    "episode_id": "leak",
                    "expected_candidates": [],
                    "human_reviews": [],
                }
            ],
        )
        expect_protocol_error(
            lambda selected=paths: run_predict(selected),
            f"reject leakage field {forbidden}",
            "leakage field rejected",
        )

    labelled = copy.deepcopy(base)
    labelled["labels"] = {"expected": True}
    paths = seal_dataset(
        root,
        name="embedded-label",
        episode_values=[labelled],
        label_values=[
            {
                "episode_id": "leak",
                "expected_candidates": [],
                "human_reviews": [],
            }
        ],
    )
    expect_protocol_error(
        lambda: run_predict(paths),
        "predict rejects labels embedded in episodes",
        "label leakage field rejected",
    )


def test_duplicate_json_keys_cannot_hide_leakage(root: Path) -> None:
    selected = root / "duplicate-json-key"
    selected.mkdir()
    episodes_path = selected / "episodes.json"
    labels_path = selected / "labels.json"
    manifest_path = selected / "manifest.json"
    output_path = selected / "predictions.json"
    dataset_id = "dataset_duplicate-json-key"
    episodes_path.write_text(
        "{"
        f"\"schema_version\":\"{EPISODES_SCHEMA}\","
        f"\"dataset_id\":\"{dataset_id}\","
        "\"episodes\":[{\"label\":\"SECRET_GOLD_LABEL\"}],"
        "\"episodes\":[]"
        "}\n",
        encoding="utf-8",
    )
    write_json(
        labels_path,
        {
            "schema_version": LABELS_SCHEMA,
            "dataset_id": dataset_id,
            "episodes": [],
        },
    )
    write_json(
        manifest_path,
        {
            "schema_version": MANIFEST_SCHEMA,
            "dataset_id": dataset_id,
            "split": "held_out",
            "data_class": "synthetic",
            "anonymization_version": "synthetic-opaque-v1",
            "label_policy_version": "guardian-label-policy-v1",
            "episodes_sha256": file_sha256(episodes_path),
            "labels_sha256": file_sha256(labels_path),
        },
    )
    expect_protocol_error(
        lambda: predict(
            manifest_path=manifest_path,
            episodes_path=episodes_path,
            output_path=output_path,
        ),
        "duplicate JSON keys cannot hide embedded labels or leakage fields",
        "duplicate JSON object key",
    )
    expect(
        not output_path.exists(),
        "duplicate-key protocol errors never create frozen predictions",
    )


def test_human_review_requirement(root: Path) -> None:
    positive = episode(
        "single-review",
        kinds=("git_dirty", "ci_failed"),
    )
    candidate = candidate_for(positive)
    invalid_review = {
        "association": association("single-review"),
        "candidate_revision": str(candidate["candidate_revision"]),
        "ratings": [{"rater_id": "reviewer-a", "useful": True}],
    }
    paths = seal_dataset(
        root,
        name="single-review",
        episode_values=[positive],
        label_values=[
            {
                "episode_id": "single-review",
                "expected_candidates": [
                    expected_candidate("single-review")
                ],
                "human_reviews": [invalid_review],
            }
        ],
    )
    run_predict(paths)
    expect_protocol_error(
        lambda: run_score(paths),
        "single-rater usefulness is rejected",
        "at least two independent raters",
    )

    disputed = copy.deepcopy(invalid_review)
    disputed["ratings"].append(
        {"rater_id": "reviewer-b", "useful": False}
    )
    paths = seal_dataset(
        root,
        name="disputed-review",
        episode_values=[positive],
        label_values=[
            {
                "episode_id": "single-review",
                "expected_candidates": [
                    expected_candidate("single-review")
                ],
                "human_reviews": [disputed],
            }
        ],
    )
    run_predict(paths)
    expect_protocol_error(
        lambda: run_score(paths),
        "disputed usefulness requires adjudication",
        "disputed ratings require adjudication",
    )


def test_frozen_inputs_do_not_hash_and_parse_separately(root: Path) -> None:
    value = episode("single-read", kinds=("git_dirty",))
    paths = seal_dataset(
        root,
        name="single-read-freeze",
        episode_values=[value],
        label_values=[
            {
                "episode_id": "single-read",
                "expected_candidates": [],
                "human_reviews": [],
            },
        ],
    )
    original_file_sha256 = replay_module.file_sha256

    def forbidden_second_pass(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise AssertionError(
            "frozen protocol reader performed a separate hash pass"
        )

    replay_module.file_sha256 = forbidden_second_pass
    try:
        prediction = run_predict(paths)
        seal_reviews(paths)
        report = run_score(paths)
    finally:
        replay_module.file_sha256 = original_file_sha256
    expect(
        prediction["episode_count"] == 1
        and report["support"]["independent_group_count"] == 1,
        (
            "manifest, episodes, labels, and reviews derive their hash and "
            "parsed JSON from the same bounded byte read"
        ),
        {"prediction": prediction, "report": report},
    )


def test_predict_cli_has_no_labels(root: Path, paths: dict[str, Any]) -> None:
    output = root / "cli-must-not-exist.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "predict",
            "--manifest",
            str(paths["manifest"]),
            "--episodes",
            str(paths["episodes"]),
            "--labels",
            str(paths["labels"]),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expect(
        result.returncode == 2
        and not output.exists()
        and "unrecognized arguments: --labels" in result.stderr,
        "predict CLI cannot accept labels",
        {"returncode": result.returncode, "stderr": result.stderr},
    )


def test_not_ready_cli_exit(root: Path) -> None:
    paths = seal_dataset(
        root,
        name="not-ready-cli",
        episode_values=[],
        label_values=[],
    )
    run_predict(paths)
    seal_reviews(paths)
    result = subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "score",
            "--manifest",
            str(paths["manifest"]),
            "--episodes",
            str(paths["episodes"]),
            "--predictions",
            str(paths["predictions"]),
            "--labels",
            str(paths["labels"]),
            "--reviews",
            str(paths["reviews"]),
            "--output",
            str(paths["report"]),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    expect(
        result.returncode == 1
        and payload["status"] == "not_ready"
        and paths["report"].exists(),
        "score CLI returns 1 for a valid but not-ready gate report",
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


def test_ready_cli_exit(root: Path) -> None:
    episodes: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for index in range(20):
        episode_id = f"ready-positive-{index:02d}"
        positive = episode(
            episode_id,
            kinds=("git_dirty", "ci_failed"),
            source_class="real_project",
        )
        candidate = candidate_for(positive)
        if candidate is None:
            raise AssertionError(
                f"ready CLI fixture did not qualify: {episode_id}"
            )
        episodes.append(positive)
        labels.append(
            {
                "episode_id": episode_id,
                "expected_candidates": [expected_candidate(episode_id)],
                "human_reviews": [
                    dual_review(
                        episode_id,
                        str(candidate["candidate_revision"]),
                    )
                ],
            }
        )
    for index in range(20):
        episode_id = f"ready-negative-{index:02d}"
        episodes.append(
            episode(
                episode_id,
                kinds=("git_dirty",),
                source_class="real_project",
            )
        )
        labels.append(
            {
                "episode_id": episode_id,
                "expected_candidates": [],
                "human_reviews": [],
            }
        )
    paths = seal_dataset(
        root,
        name="ready-cli",
        episode_values=episodes,
        label_values=labels,
        data_class="real_project",
    )
    run_predict(paths)
    seal_reviews(paths)
    result = subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "score",
            "--manifest",
            str(paths["manifest"]),
            "--episodes",
            str(paths["episodes"]),
            "--predictions",
            str(paths["predictions"]),
            "--labels",
            str(paths["labels"]),
            "--reviews",
            str(paths["reviews"]),
            "--output",
            str(paths["report"]),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    expect(
        result.returncode == 0
        and payload["status"] == "ready"
        and paths["report"].exists(),
        "score CLI returns 0 for a valid ready gate report",
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


def test_protocol_error_cli_exit(root: Path) -> None:
    output = root / "protocol-error-must-not-exist.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "predict",
            "--manifest",
            str(root / "missing-manifest.json"),
            "--episodes",
            str(root / "missing-episodes.json"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expect(
        result.returncode == 2
        and not output.exists()
        and "ERROR:" in result.stderr,
        "CLI returns 2 for a replay protocol error",
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


def test_static_authority_boundary() -> None:
    source = REPLAY_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", 1)[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    forbidden_imports = {
        "core",
        "execution",
        "interface",
        "requests",
        "runtime",
        "subprocess",
        "tool_proxy",
    }
    expect(
        not (imported_roots & forbidden_imports),
        "replay protocol imports no state runtime or external action surface",
        sorted(imported_roots & forbidden_imports),
    )
    forbidden_symbols = (
        "WorldStateStore",
        "AgentRuntime",
        "notification_service",
        "git_command",
    )
    expect(
        not any(symbol in source for symbol in forbidden_symbols),
        "replay protocol has no Git Agent notification or state symbol",
    )


def main() -> int:
    test_static_authority_boundary()
    with tempfile.TemporaryDirectory(
        prefix="veyra-project-guardian-replay-"
    ) as tmp:
        root = Path(tmp)
        paths = test_label_blind_exact_scoring(root)
        test_perfect_synthetic_stays_not_ready(root)
        test_empty_is_not_ready(root)
        test_integrity_binding(root)
        test_score_recomputes_frozen_predictions(root)
        test_group_independence_and_canonical_whitelist(root)
        test_leakage_rejection(root)
        test_duplicate_json_keys_cannot_hide_leakage(root)
        test_human_review_requirement(root)
        test_frozen_inputs_do_not_hash_and_parse_separately(root)
        test_predict_cli_has_no_labels(root, paths)
        test_not_ready_cli_exit(root)
        test_ready_cli_exit(root)
        test_protocol_error_cli_exit(root)
    print("All Project Guardian held-out replay smokes passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
