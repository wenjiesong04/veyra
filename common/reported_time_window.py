"""Deterministic bounds for explicit user-reported time expressions.

This module is deliberately small and domain-neutral. It recognizes temporal
grammar, never Situation categories, and returns only the end of an explicitly
reported window. Unknown or ambiguous text remains unresolved for the model or
an InformationNeed instead of becoming an invented date.
"""

from __future__ import annotations

import calendar
from datetime import datetime, time, timedelta
import re
from typing import Any


_ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?!\d)")
_ZH_DATE_RE = re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]")
_MONTH_DAY_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]")
_ZH_OFFSET_RE = re.compile(r"(?P<count>[一二两三四五六七八九十\d]+)\s*(?P<unit>天|周|星期|个月|月)后")
_EN_OFFSET_RE = re.compile(r"\bin\s+(?P<count>\d+)\s+(?P<unit>day|days|week|weeks|month|months)\b", re.IGNORECASE)
_ZH_WEEKDAY_RE = re.compile(r"下(?:周|星期|个周|个星期)(?P<weekday>[一二三四五六日天])")

_ZH_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_ZH_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def resolve_reported_calendar_date(text: str, current_time: Any) -> str | None:
    """Resolve only an explicit exact calendar day, never a broad window."""

    now = _aware_datetime(current_time)
    source = str(text or "").strip()
    if now is None or not source:
        return None
    explicit = _explicit_date(source, now)
    if explicit is not None:
        return explicit.date().isoformat()
    match = _ZH_WEEKDAY_RE.search(source)
    if match:
        weekday = _ZH_WEEKDAYS.get(match.group("weekday"))
        if weekday is None:
            return None
        start_next_week = _start_of_day(now) + timedelta(days=(7 - now.weekday()))
        return (start_next_week + timedelta(days=weekday)).date().isoformat()
    lowered = source.lower()
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    match = re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if match:
        start_next_week = _start_of_day(now) + timedelta(days=(7 - now.weekday()))
        return (start_next_week + timedelta(days=names.index(match.group(1)))).date().isoformat()
    return None


def resolve_reported_window_end(text: str, current_time: Any) -> str | None:
    """Return a timezone-aware window end for an explicit time expression.

    Supported forms cover the V1 product's common calendar language: explicit
    dates, today/tomorrow/day-after, this/next week, month end, and bounded
    ``N days/weeks/months`` offsets in Chinese or English. The result is an ISO
    timestamp at second precision. No expression means ``None``.
    """

    now = _aware_datetime(current_time)
    source = str(text or "").strip()
    if now is None or not source:
        return None

    explicit = _explicit_date(source, now)
    if explicit is not None:
        return _end_of_day(explicit).isoformat(timespec="seconds")

    lowered = source.lower()
    if _contains_any(source, ("后天",)) or "day after tomorrow" in lowered:
        return _end_of_day(now + timedelta(days=2)).isoformat(timespec="seconds")
    if _contains_any(source, ("明天", "明日")) or "tomorrow" in lowered:
        return _daypart_end(now + timedelta(days=1), source).isoformat(timespec="seconds")
    if _contains_any(source, ("今天", "今日")) or re.search(r"\btoday\b", lowered):
        return _daypart_end(now, source).isoformat(timespec="seconds")

    exact_calendar_date = resolve_reported_calendar_date(source, now)
    if exact_calendar_date is not None and (
        _ZH_WEEKDAY_RE.search(source)
        or re.search(r"\bnext\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    ):
        exact_datetime = datetime.fromisoformat(exact_calendar_date).replace(tzinfo=now.tzinfo)
        return _end_of_day(exact_datetime).isoformat(timespec="seconds")

    if _contains_any(source, ("下周", "下星期")) or "next week" in lowered:
        start_next_week = _start_of_day(now) + timedelta(days=(7 - now.weekday()))
        return _end_of_day(start_next_week + timedelta(days=6)).isoformat(timespec="seconds")
    if _contains_any(source, ("本周", "这周", "这个星期")) or "this week" in lowered:
        end_this_week = _start_of_day(now) + timedelta(days=(6 - now.weekday()))
        return _end_of_day(end_this_week).isoformat(timespec="seconds")

    if _contains_any(source, ("下月底", "下月末", "下个月底", "下个月末")) or "end of next month" in lowered:
        return _end_of_month(_add_months(now, 1)).isoformat(timespec="seconds")
    if _contains_any(source, ("月底", "月末", "本月末", "这个月末")) or re.search(
        r"\b(?:end of (?:this|the) month|month end)\b",
        lowered,
    ):
        return _end_of_month(now).isoformat(timespec="seconds")

    match = _ZH_OFFSET_RE.search(source)
    if match:
        count = _bounded_count(match.group("count"))
        return _offset_end(now, count, match.group("unit"))
    match = _EN_OFFSET_RE.search(source)
    if match:
        count = _bounded_count(match.group("count"))
        return _offset_end(now, count, match.group("unit").lower())
    return None


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _explicit_date(source: str, now: datetime) -> datetime | None:
    match = _ISO_DATE_RE.search(source) or _ZH_DATE_RE.search(source)
    if match:
        return _date_in_zone(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            now,
        )
    match = _MONTH_DAY_RE.search(source)
    if not match:
        return None
    month = int(match.group("month"))
    day = int(match.group("day"))
    year = now.year
    candidate = _date_in_zone(year, month, day, now)
    if candidate is not None and candidate.date() < now.date():
        candidate = _date_in_zone(year + 1, month, day, now)
    return candidate


def _date_in_zone(year: int, month: int, day: int, now: datetime) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=now.tzinfo)
    except ValueError:
        return None


def _start_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=value.tzinfo)


def _end_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time(23, 59, 59), tzinfo=value.tzinfo)


def _daypart_end(value: datetime, source: str) -> datetime:
    lowered = source.lower()
    hour = 23
    if _contains_any(source, ("上午", "早上", "早晨")) or "morning" in lowered:
        hour = 12
    elif _contains_any(source, ("中午",)) or "noon" in lowered:
        hour = 14
    elif _contains_any(source, ("下午",)) or "afternoon" in lowered:
        hour = 18
    elif _contains_any(source, ("晚上", "今晚")) or "evening" in lowered:
        hour = 23
    selected_time = time(23, 59, 59) if hour == 23 else time(hour, 0, 0)
    return datetime.combine(value.date(), selected_time, tzinfo=value.tzinfo)


def _end_of_month(value: datetime) -> datetime:
    last_day = calendar.monthrange(value.year, value.month)[1]
    return datetime(value.year, value.month, last_day, 23, 59, 59, tzinfo=value.tzinfo)


def _add_months(value: datetime, count: int) -> datetime:
    total = value.year * 12 + (value.month - 1) + count
    year, month_index = divmod(total, 12)
    day = min(value.day, calendar.monthrange(year, month_index + 1)[1])
    return value.replace(year=year, month=month_index + 1, day=day)


def _bounded_count(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        selected = int(text)
    else:
        selected = _ZH_NUMBERS.get(text)
    if selected is None or not 1 <= selected <= 366:
        return None
    return selected


def _offset_end(now: datetime, count: int | None, unit: str) -> str | None:
    if count is None:
        return None
    if unit in {"天", "day", "days"}:
        value = now + timedelta(days=count)
    elif unit in {"周", "星期", "week", "weeks"}:
        value = now + timedelta(weeks=count)
    else:
        value = _add_months(now, count)
    return _end_of_day(value).isoformat(timespec="seconds")


def _contains_any(source: str, values: tuple[str, ...]) -> bool:
    return any(value in source for value in values)
