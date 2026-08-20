#!/usr/bin/env python3
"""Deterministic V1 smoke for explicit reported time-window bounds."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.reported_time_window import resolve_reported_window_end  # noqa: E402
from interface.living_context_contract import parse_living_context_candidate_detailed  # noqa: E402


TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=TZ8)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def candidate() -> dict[str, object]:
    return {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "create",
        "create_subject": "一个有明确时间窗口的事项",
        "summary": "用户报告一个有明确时间窗口的事项",
        "goal": "完成该事项",
        "progress": {"status": "not_started", "value": None},
        "known": [],
        "unknown": [],
        "assumptions": [],
        "needs": [],
        "requested_reaction": "wait",
        "source": "model",
    }


def main() -> int:
    expect(
        resolve_reported_window_end("下周有一件重要事项", NOW)
        == "2026-08-30T23:59:59+08:00",
        "next local week resolves to its bounded end",
    )
    expect(
        resolve_reported_window_end("月底前处理完", NOW)
        == "2026-08-31T23:59:59+08:00",
        "current month end resolves without an invented event date",
    )
    expect(
        resolve_reported_window_end("下月底前处理完", NOW)
        == "2026-09-30T23:59:59+08:00",
        "next month end is distinct from current month end",
    )
    expect(
        resolve_reported_window_end("下个月底前处理完", NOW)
        == "2026-09-30T23:59:59+08:00",
        "colloquial next month end shares the same bounded grammar",
    )
    expect(
        resolve_reported_window_end("明天下午有安排", NOW)
        == "2026-08-21T18:00:00+08:00",
        "reported daypart resolves to a bounded local window",
    )
    expect(
        resolve_reported_window_end("2026-09-03 前完成", NOW)
        == "2026-09-03T23:59:59+08:00",
        "explicit ISO date preserves the server timezone",
    )
    expect(
        resolve_reported_window_end("三天后再确认", NOW)
        == "2026-08-23T23:59:59+08:00"
        and resolve_reported_window_end("in 2 weeks", NOW)
        == "2026-09-03T23:59:59+08:00",
        "bounded Chinese and English offsets share one grammar",
    )
    expect(
        resolve_reported_window_end("最近准备一个事项", NOW) is None
        and resolve_reported_window_end("明天", "2026-08-20T12:00:00") is None,
        "missing expression or timezone remains unresolved",
    )

    parsed, issues, report = parse_living_context_candidate_detailed(
        candidate(),
        source_text="下周有一件重要事项",
        current_time=NOW,
    )
    expect(
        parsed is not None
        and not issues
        and parsed.deadline_at == "2026-08-30T23:59:59+08:00"
        and "deadline_at:from_reported_time_window" in report["repaired_fields"],
        "candidate boundary records the deterministic temporal repair",
    )
    print("REPORTED_TIME_WINDOW_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
