#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

GATE_SMOKES = [
    "agency_single_source_smoke.py",
    "runtime_config_honesty_smoke.py",
    "user_world_multitenant_smoke.py",
    "persona_deep_binding_smoke.py",
    "capability_registry_unified_smoke.py",
    "local_setup_smoke.py",
    "proactive_review_execution_smoke.py",
    "agency_state_source_smoke.py",
    "user_profile_isolation_smoke.py",
    "user_profile_generalization_smoke.py",
    "commitment_smoke.py",
    "cognition_pipeline_smoke.py",
    "tool_proxy_guard_smoke.py",
]


def smoke_files(group: str) -> list[Path]:
    scripts_dir = ROOT / "scripts"
    if group == "all":
        return sorted(path for path in scripts_dir.glob("*_smoke.py") if path.name != "run_smokes.py")
    if group != "gate":
        raise ValueError(f"unknown smoke group: {group}")
    return [scripts_dir / name for name in GATE_SMOKES]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Veyra smoke scripts with timeouts.")
    parser.add_argument("--group", choices=["gate", "all"], default="gate")
    parser.add_argument("--all", action="store_true", help="Run every scripts/*_smoke.py file.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--list", action="store_true", help="List selected smoke scripts without running them.")
    args = parser.parse_args()

    group = "all" if args.all else args.group
    selected = smoke_files(group)
    missing = [path for path in selected if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing smoke: {path.relative_to(ROOT)}", file=sys.stderr)
        return 2
    if args.list:
        for path in selected:
            print(path.relative_to(ROOT))
        return 0

    failures: list[tuple[Path, str]] = []
    for index, path in enumerate(selected, start=1):
        label = str(path.relative_to(ROOT))
        print(f"[{index}/{len(selected)}] {label}")
        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            failures.append((path, f"timeout after {args.timeout:.1f}s\n{exc.stdout or ''}\n{exc.stderr or ''}"))
            print(f"FAIL {label}: timeout", file=sys.stderr)
            continue
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        if result.returncode != 0:
            failures.append((path, f"exit {result.returncode}"))
            print(f"FAIL {label}: exit {result.returncode}", file=sys.stderr)
        else:
            print(f"PASS {label}")

    if failures:
        print("\nSmoke failures:", file=sys.stderr)
        for path, reason in failures:
            print(f"- {path.relative_to(ROOT)}: {reason}", file=sys.stderr)
        return 1
    print(f"All {len(selected)} {group} smoke scripts passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
