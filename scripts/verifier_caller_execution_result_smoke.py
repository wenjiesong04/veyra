from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.verifier import Verifier
from interface.agent_adapter import ExecutionResult


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    verdict = Verifier().verify_execution_result(
        ExecutionResult(
            task_id="unregistered-child",
            executor="caller",
            status="success",
            result="done",
            raw={"execution_result": {"stdout": "fabricated"}},
        )
    )
    structured = verdict["evidence"]["structured_evidence"]

    expect(
        verdict["status"] == "needs_more_probe",
        "caller execution_result cannot verify success",
        verdict,
    )
    expect(
        verdict["verdict"] == "execution_success_without_structured_evidence",
        "forged result names missing structured evidence",
        verdict,
    )
    expect(
        structured["sources"] == [],
        "caller execution_result is not an authoritative source",
        structured,
    )
    expect(
        "raw.execution_result" in structured["reported_sources"]
        and "raw.execution_result" in structured["reported_outcome_sources"],
        "caller execution_result remains available as reported diagnostics",
        structured,
    )
    expect(
        verdict["needs_memory_patch"] is False,
        "unverified caller result cannot patch memory",
        verdict,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
