from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DAILY_MARKERS = ("每天", "每日", "定时", "定期", "daily", "every morning", "each day")
TOMORROW_MARKERS = ("明天", "tomorrow")
MORNING_MARKERS = ("早上", "早晨", "上午", "morning")
AFTERNOON_MARKERS = ("下午", "afternoon")
EVENING_MARKERS = ("晚上", "傍晚", "夜里", "evening", "tonight")
NOON_MARKERS = ("中午", "noon")


def parse_schedule_text(
    text: str,
    *,
    default_kind: str = "daily",
    default_time: str = "08:00",
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse the small set of schedule phrases Veyra currently supports."""
    value = text or ""
    lowered = value.lower()
    local_now = _local_now(timezone_name, now=now)
    kind = default_kind or "daily"
    if any(marker in value or marker in lowered for marker in TOMORROW_MARKERS):
        kind = "once"
    elif any(marker in value or marker in lowered for marker in DAILY_MARKERS):
        kind = "daily"

    time_local = extract_time_local(value, default_time=default_time)
    result: dict[str, Any] = {
        "kind": kind,
        "time_local": time_local,
        "timezone": timezone_name,
    }
    if kind == "once" and any(marker in value or marker in lowered for marker in TOMORROW_MARKERS):
        run_date = (local_now + timedelta(days=1)).date().isoformat()
        result["relative_day"] = "tomorrow"
        result["date"] = run_date

    end_date = extract_end_date(value, timezone_name=timezone_name, now=local_now)
    if end_date:
        result["end_date"] = end_date
        result["end_at"] = end_of_local_day_iso(end_date, timezone_name=timezone_name)
    return result


def extract_time_local(text: str, *, default_time: str = "08:00") -> str:
    value = text or ""
    match = re.search(r"(\d{1,2})[:：](\d{2})", value)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    chinese = chinese_hour(value)
    if chinese is not None:
        return f"{chinese:02d}:00"

    lowered = value.lower()
    if any(marker in value or marker in lowered for marker in NOON_MARKERS):
        return "12:00"
    if any(marker in value or marker in lowered for marker in EVENING_MARKERS):
        return "20:00"
    if any(marker in value or marker in lowered for marker in AFTERNOON_MARKERS):
        return "15:00"
    if any(marker in value or marker in lowered for marker in MORNING_MARKERS):
        return "08:00"
    return default_time


def has_explicit_schedule_time(text: str) -> bool:
    value = text or ""
    lowered = value.lower()
    if re.search(r"\d{1,2}[:：]\d{2}", value):
        return True
    if chinese_hour(value) is not None:
        return True
    return any(marker in value or marker in lowered for marker in MORNING_MARKERS + AFTERNOON_MARKERS + EVENING_MARKERS + NOON_MARKERS)


def chinese_hour(text: str) -> int | None:
    value = text or ""
    for match in re.finditer(r"([一二两三四五六七八九十〇零]|十[一二三四五六七八九]?|二十[一二三]?|[0-2]?\d)点", value):
        raw = match.group(1)
        if _looks_like_quantity_point(value, match):
            continue
        hour = _parse_chinese_number(raw)
        if hour is None:
            continue
        lowered = value.lower()
        if any(marker in value or marker in lowered for marker in AFTERNOON_MARKERS + EVENING_MARKERS):
            if 1 <= hour <= 11:
                hour += 12
        if any(marker in value or marker in lowered for marker in NOON_MARKERS):
            if 1 <= hour <= 10:
                hour += 12
            elif hour == 0:
                hour = 12
        if 0 <= hour <= 23:
            return hour
    return None


def _looks_like_quantity_point(value: str, match: re.Match[str]) -> bool:
    raw = match.group(1)
    if raw not in {"一", "二", "两"}:
        return False
    after = value[match.end() : match.end() + 1]
    if after == "钟":
        return False
    context = value[max(0, match.start() - 4) : match.end() + 2]
    lowered = context.lower()
    if any(marker in context or marker in lowered for marker in MORNING_MARKERS + AFTERNOON_MARKERS + EVENING_MARKERS + NOON_MARKERS):
        return False
    before = value[match.start() - 1 : match.start()] if match.start() > 0 else ""
    return before in {"推", "发", "看", "学", "读", "做", "来", "要", "吃", "喝", "写", "练", "讲", "整"}


def extract_end_date(text: str, *, timezone_name: str = DEFAULT_TIMEZONE, now: datetime | None = None) -> str | None:
    value = text or ""
    match = re.search(r"(?:持续到|直到|到)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", value)
    if not match:
        match = re.search(r"(?:持续到|直到|到)\s*(\d{1,2})\s*/\s*(\d{1,2})", value)
    if not match:
        return None
    month = int(match.group(1))
    day = int(match.group(2))
    local_now = _local_now(timezone_name, now=now)
    year = local_now.year
    try:
        candidate = datetime(year, month, day, tzinfo=local_now.tzinfo).date()
    except ValueError:
        return None
    if candidate < local_now.date():
        try:
            candidate = datetime(year + 1, month, day, tzinfo=local_now.tzinfo).date()
        except ValueError:
            return None
    return candidate.isoformat()


def end_of_local_day_iso(date_value: str, *, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    tz = _timezone(timezone_name)
    year, month, day = [int(part) for part in date_value.split("-", 2)]
    local_end = datetime.combine(datetime(year, month, day).date(), time(23, 59, 59), tzinfo=tz)
    return local_end.astimezone(timezone.utc).isoformat()


def _parse_chinese_number(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    mapping = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if raw in mapping:
        return mapping[raw]
    if raw.startswith("二十"):
        tail = raw[2:]
        return 20 + (mapping.get(tail, 0) if tail else 0)
    if raw.startswith("十"):
        tail = raw[1:]
        return 10 + (mapping.get(tail, 0) if tail else 0)
    return None


def _local_now(timezone_name: str, *, now: datetime | None = None) -> datetime:
    tz = _timezone(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(tz)


def _timezone(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone.utc
