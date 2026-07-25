#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import time
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import Route, VeyraEvent  # noqa: E402
from scripts.event_driven_awareness_smoke import (  # noqa: E402
    OFFLINE_ROUTE_CASES,
    OfflineRouteCase,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
    offline_result_signature,
)


MODES = ("disabled", "record_only", "shadow")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure deterministic, fully offline AwarenessLoop latency by route "
            "and event-awareness mode. Results are diagnostic, not a CI gate."
        )
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="Measured turns per route/mode (minimum 20; default: 50).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Unmeasured warmup turns per route/mode (default: 5).",
    )
    parser.add_argument(
        "--route",
        action="append",
        dest="routes",
        choices=[case.case_id for case in OFFLINE_ROUTE_CASES],
        help="Benchmark only this route fixture. May be repeated.",
    )
    args = parser.parse_args()
    if args.iterations < 20:
        parser.error("--iterations must be at least 20 so p95 is meaningful")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")
    return args


def percentile(samples: list[float], quantile: float) -> float:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def event_for(
    normalizer: EventNormalizer,
    *,
    case: OfflineRouteCase,
    index: int,
    warmup: bool,
) -> VeyraEvent:
    phase = "warmup" if warmup else "sample"
    return normalizer.user_message(
        case.text,
        "offline-benchmark",
        "benchmark-user",
        f"benchmark-{case.case_id}",
        event_id=f"evt_benchmark_{case.case_id}_{phase}_{index}",
        correlation_id=f"corr-benchmark-{case.case_id}-{phase}-{index}",
    )


def run_case(
    root: Path,
    *,
    case: OfflineRouteCase,
    iterations: int,
    warmup: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalizer = EventNormalizer()
    loops = {
        mode: build_offline_route_loop(
            root / case.case_id / mode,
            mode=mode,
            case=case,
        )
        for mode in MODES
    }
    signatures: dict[str, dict[str, Any]] = {}
    for index in range(warmup):
        event = event_for(
            normalizer,
            case=case,
            index=index,
            warmup=True,
        )
        for loop in loops.values():
            loop.handle_event(event)

    samples_by_mode: dict[str, list[float]] = {mode: [] for mode in MODES}
    for index in range(iterations):
        # Rotate first position to reduce systematic ordering bias.
        offset = index % len(MODES)
        ordered_modes = MODES[offset:] + MODES[:offset]
        event = event_for(
            normalizer,
            case=case,
            index=index,
            warmup=False,
        )
        sample_results: dict[str, Any] = {}
        for mode in ordered_modes:
            started = time.perf_counter_ns()
            result = loops[mode].handle_event(event)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            samples_by_mode[mode].append(elapsed_ms)
            sample_results[mode] = result
        for left_mode, right_mode in combinations(MODES, 2):
            equivalent, differences = offline_public_outputs_equivalent(
                sample_results[left_mode],
                sample_results[right_mode],
                require_distinct_generated_ids=True,
            )
            if not equivalent:
                raise AssertionError(
                    f"{case.case_id}/{left_mode}:{right_mode} public result "
                    f"differs at sample {index}: {differences!r}"
                )
        signatures = {
            mode: offline_result_signature(result)
            for mode, result in sample_results.items()
        }

    equivalent = True
    disabled_p50 = percentile(samples_by_mode["disabled"], 0.50)
    disabled_p95 = percentile(samples_by_mode["disabled"], 0.95)
    rows: list[dict[str, Any]] = []
    for mode in MODES:
        samples = samples_by_mode[mode]
        p50 = percentile(samples, 0.50)
        p95 = percentile(samples, 0.95)
        rows.append(
            {
                "case": case.case_id,
                "route": case.route.value,
                "mode": mode,
                "samples": len(samples),
                "latency_ms": {
                    "min": round(min(samples), 3),
                    "mean": round(statistics.fmean(samples), 3),
                    "p50": round(p50, 3),
                    "p95": round(p95, 3),
                    "max": round(max(samples), 3),
                },
                "delta_vs_disabled_ms": {
                    "p50": round(p50 - disabled_p50, 3),
                    "p95": round(p95 - disabled_p95, 3),
                },
                "public_result": signatures[mode],
            }
        )
    return rows, {
        "case": case.case_id,
        "route": case.route.value,
        "equivalent": equivalent,
        "signatures": signatures,
    }


def main() -> int:
    args = parse_args()
    selected = [
        case
        for case in OFFLINE_ROUTE_CASES
        if not args.routes or case.case_id in set(args.routes)
    ]
    results: list[dict[str, Any]] = []
    equivalence: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="veyra-event-awareness-benchmark-") as tmp:
        root = Path(tmp) / "state"
        for case in selected:
            rows, comparison = run_case(
                root,
                case=case,
                iterations=args.iterations,
                warmup=args.warmup,
            )
            results.extend(rows)
            equivalence.append(comparison)

    catalog_routes = {case.route for case in OFFLINE_ROUTE_CASES}
    selected_routes = {case.route for case in selected}
    fixture_catalog_complete = catalog_routes == set(Route)
    selected_run_complete = selected_routes == set(Route)
    equivalence_passed = len(equivalence) == len(selected) and all(
        item["equivalent"] for item in equivalence
    )
    passed = fixture_catalog_complete and equivalence_passed
    output = {
        "schema": "veyra.event_awareness_benchmark.v1",
        "status": "passed" if passed else "failed",
        "scope": (
            "Full AwarenessLoop.handle_event path with scripted semantic decisions "
            "and in-process Probe/Agent boundaries."
        ),
        "isolation": {
            "network": "disabled_by_fixture",
            "model_transport": "disabled_fail_fast",
            "agent_runtime": "in_process_adapter",
            "probe_runtime": "in_process_probe",
            "state": "temporary_directory",
        },
        "gate": {
            "enforced": False,
            "reason": (
                "Wall-clock microbenchmarks vary by host load. This script records "
                "repeatable p50/p95 diagnostics and exact output equivalence, but "
                "does not impose a CI latency threshold."
            ),
            "route_enum_total": len(Route),
            "route_fixture_total": len(catalog_routes),
            "fixture_catalog_complete": fixture_catalog_complete,
            "selected_route_total": len(selected_routes),
            "selected_run_complete": selected_run_complete,
            "all_routes_covered": selected_run_complete,
            "full_matrix_passed": selected_run_complete and equivalence_passed,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "configuration": {
            "iterations_per_route_mode": args.iterations,
            "warmup_per_route_mode": args.warmup,
            "modes": list(MODES),
            "routes": [case.case_id for case in selected],
            "ordering": "mode order rotates for each measured iteration",
        },
        "summary": {
            "route_cases": len(selected),
            "measurements": len(results),
            "public_output_equivalence_passed": equivalence_passed,
        },
        "equivalence": equivalence,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
