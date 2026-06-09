from __future__ import annotations

import sys
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.commitment_core import CommitmentCore
from core.schedule_parser import has_explicit_schedule_time, parse_schedule_text
from core.world_state import WorldStateStore


NOW = datetime(2026, 6, 9, 1, 0, tzinfo=timezone.utc)


def main() -> None:
    daily = parse_schedule_text("每天早上10点告诉我天气", now=NOW)
    assert daily["kind"] == "daily", daily
    assert daily["time_local"] == "10:00", daily

    tomorrow = parse_schedule_text("明天九点提醒我", now=NOW)
    assert tomorrow["kind"] == "once", tomorrow
    assert tomorrow["time_local"] == "09:00", tomorrow
    assert tomorrow["relative_day"] == "tomorrow", tomorrow
    assert tomorrow["date"] == "2026-06-10", tomorrow

    evening = parse_schedule_text("每天晚上8点推送，持续到7月1日", now=NOW)
    assert evening["kind"] == "daily", evening
    assert evening["time_local"] == "20:00", evening
    assert evening["end_date"] == "2026-07-01", evening
    assert evening.get("end_at"), evening

    afternoon = parse_schedule_text("下午提醒我", now=NOW)
    assert afternoon["time_local"] == "15:00", afternoon

    assert not has_explicit_schedule_time("每天发天气")
    with TemporaryDirectory(prefix="veyra-schedule-parser-") as tmp:
        core = CommitmentCore(WorldStateStore(Path(tmp) / "state"))
        validation = core._validate_weather_daily_payload(
            {"location": "贵阳市"},
            parse_schedule_text("每天发天气", now=NOW),
            require_explicit_time=True,
            source_text="每天发天气",
        )
        assert validation["reason"] == "missing_schedule_time", validation

    print("schedule_parser_smoke: ok")


if __name__ == "__main__":
    main()
