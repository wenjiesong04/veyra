#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from scripts.project_guardian_smoke import NOW, SCOPE, enqueue_signal, goal, seed_goal


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def case_scope(index: int) -> dict[str, str]:
    return {
        "workspace_id": f"ws_canonical_fixture_{index:02d}",
        "repo_id": f"example-{index}/release-service",
        "target_ref": f"refs/heads/release-{index}",
        "target_environment": f"environment-{index}",
        "release_cycle": f"cycle-{index}",
    }


def run_case(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    store = WorldStateStore(root / str(case["id"]))
    selected_scope = case_scope(int(case["id"]))
    if case.get("goal", True):
        goal_patch = dict(case.get("goal_patch") or {})
        seed_goal(
            store,
            goal(
                scope=selected_scope,
                **goal_patch,
            ),
        )
    for index, signal in enumerate(case.get("signals") or []):
        signal = dict(signal)
        signal_scope = dict(selected_scope)
        signal_scope.update(signal.pop("scope_patch", {}))
        enqueue_signal(
            store,
            event_id=f"evt_canonical_fixture_{case['id']}_{index}",
            scope=signal_scope,
            **signal,
        )
    evaluator = ProjectGuardianEvaluator()
    result = evaluator.evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(store).evaluation_state(),
        now=NOW,
    )
    predicted = result["candidate_count"] > 0
    candidate = result["candidates"][0] if predicted else None
    useful = bool(
        candidate
        and candidate.get("why_now")
        and candidate.get("evidence_refs")
        and candidate.get("unknowns")
        and candidate.get("candidate_advice", {}).get("checks")
        and candidate.get("notification_allowed") is False
        and candidate.get("execution_allowed") is False
    )
    evidence_correct = bool(
        not candidate
        or (
            len(candidate.get("evidence_refs") or []) >= 2
            and not any(
                ref.get("is_fact")
                for ref in candidate.get("evidence_refs") or []
                if isinstance(ref, dict)
            )
        )
    )
    return {
        "id": case["id"],
        "expected": bool(case["expected"]),
        "predicted": predicted,
        "useful_by_contract_rubric": useful,
        "evidence_correct": evidence_correct,
        "candidate_id": candidate.get("candidate_id") if candidate else None,
    }


def main() -> int:
    cases: list[dict[str, Any]] = [
        {
            "id": 1,
            "expected": True,
            "signals": [
                {"kind": "ci_failed", "session_id": "ci-runtime"},
                {"kind": "git_dirty", "session_id": "developer-session"},
            ],
        },
        {
            "id": 2,
            "expected": True,
            "signals": [
                {"kind": "deployment_intent", "session_id": "intent-session"},
                {"kind": "git_dirty", "session_id": "git-runtime"},
            ],
        },
        {
            "id": 3,
            "expected": True,
            "signals": [
                {
                    "kind": "ci_failed",
                    "occurred_at": NOW - timedelta(minutes=30),
                    "valid_until": NOW + timedelta(minutes=1),
                },
                {
                    "kind": "deployment_intent",
                    "occurred_at": NOW,
                    "valid_until": NOW + timedelta(minutes=1),
                },
            ],
        },
        {
            "id": 4,
            "expected": True,
            "signals": [
                {"kind": "deployment_intent"},
                {"kind": "git_dirty"},
                {"kind": "ci_failed"},
            ],
        },
        {
            "id": 5,
            "expected": False,
            "goal": False,
            "signals": [{"kind": "git_dirty"}, {"kind": "ci_failed"}],
        },
        {
            "id": 6,
            "expected": False,
            "goal_patch": {"status": "completed"},
            "signals": [{"kind": "git_dirty"}, {"kind": "ci_failed"}],
        },
        {
            "id": 7,
            "expected": False,
            "signals": [
                {"kind": "git_dirty"},
                {"kind": "ci_failed", "scope_patch": {"workspace_id": "ws_other"}},
            ],
        },
        {
            "id": 8,
            "expected": False,
            "signals": [
                {"kind": "git_dirty"},
                {"kind": "ci_failed", "scope_patch": {"repo_id": "other/repo"}},
            ],
        },
        {
            "id": 9,
            "expected": False,
            "signals": [
                {"kind": "git_dirty"},
                {
                    "kind": "deployment_intent",
                    "scope_patch": {"target_ref": "refs/heads/feature"},
                },
            ],
        },
        {
            "id": 10,
            "expected": False,
            "signals": [
                {"kind": "ci_failed"},
                {
                    "kind": "deployment_intent",
                    "scope_patch": {"target_environment": "staging"},
                },
            ],
        },
        {
            "id": 11,
            "expected": False,
            "signals": [
                {"kind": "git_dirty"},
                {
                    "kind": "ci_failed",
                    "scope_patch": {"release_cycle": "cycle-other"},
                },
            ],
        },
        {
            "id": 12,
            "expected": False,
            "signals": [
                {"kind": "git_dirty", "user_id": "user-b"},
                {"kind": "ci_failed", "user_id": "user-b"},
            ],
        },
        {
            "id": 13,
            "expected": False,
            "signals": [{"kind": "git_dirty"}, {"kind": "git_dirty"}],
        },
        {
            "id": 14,
            "expected": False,
            "signals": [
                {
                    "kind": "git_dirty",
                    "occurred_at": NOW - timedelta(hours=2),
                    "received_at": NOW,
                    "valid_until": NOW + timedelta(minutes=1),
                },
                {"kind": "ci_failed"},
            ],
        },
        {
            "id": 15,
            "expected": False,
            "signals": [
                {
                    "kind": "git_dirty",
                    "occurred_at": NOW + timedelta(minutes=3),
                    "valid_until": NOW + timedelta(minutes=10),
                },
                {"kind": "ci_failed"},
            ],
        },
        {
            "id": 16,
            "expected": False,
            "signals": [
                {"kind": "git_dirty", "include_evidence": False},
                {"kind": "ci_failed"},
            ],
        },
    ]
    with tempfile.TemporaryDirectory(prefix="veyra-project-guardian-benchmark-") as tmp:
        root = Path(tmp)
        results = [run_case(root, case) for case in cases]
        positives = [item for item in results if item["predicted"]]
        true_positives = [
            item for item in positives if item["expected"] is True
        ]
        false_positives = [
            item for item in positives if item["expected"] is False
        ]
        expected_positives = [
            item for item in results if item["expected"] is True
        ]
        precision = len(true_positives) / max(1, len(positives))
        recall = len(true_positives) / max(1, len(expected_positives))
        usefulness = sum(
            bool(item["useful_by_contract_rubric"]) for item in true_positives
        ) / max(1, len(true_positives))
        evidence_correctness = sum(
            bool(item["evidence_correct"]) for item in results
        ) / max(1, len(results))

        perf_store = WorldStateStore(root / "performance")
        perf_scope = {**SCOPE, "workspace_id": "ws_perf"}
        seed_goal(perf_store, goal(scope=perf_scope))
        enqueue_signal(
            perf_store,
            kind="git_dirty",
            event_id="evt_perf_git",
            scope=perf_scope,
        )
        enqueue_signal(
            perf_store,
            kind="ci_failed",
            event_id="evt_perf_ci",
            scope=perf_scope,
        )
        evaluator = ProjectGuardianEvaluator()
        goals_state = perf_store.read_json("user_goals.json")
        inbox_state = perf_store.read_json("event_inbox.json")
        latencies_ms: list[float] = []
        for _ in range(200):
            started = perf_counter()
            evaluator.evaluate(
                goals_state=goals_state,
                event_inbox_state=inbox_state,
                now=NOW,
            )
            latencies_ms.append((perf_counter() - started) * 1000)

    report = {
        "schema": "veyra.project_guardian_fixture_benchmark.v1",
        "validation_state": "fixture_only_real_project_replay_and_human_usefulness_pending",
        "case_count": len(results),
        "metrics": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "false_association_count": len(false_positives),
            "evidence_correctness": round(evidence_correctness, 4),
            "advice_contract_rubric": round(usefulness, 4),
            "evaluation_latency_ms": {
                "median": round(median(latencies_ms), 4),
                "p95": round(percentile(latencies_ms, 0.95), 4),
            },
        },
        "thresholds": {
            "precision": 0.95,
            "recall": 0.85,
            "false_association_count": 0,
            "evidence_correctness": 1.0,
            "advice_contract_rubric": 0.8,
        },
        "cases": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    passed = (
        precision >= 0.95
        and recall >= 0.85
        and not false_positives
        and evidence_correctness == 1.0
        and usefulness >= 0.8
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
