from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from probes.schema import probe_payload


ZONE_ALIASES = {
    "东京": "Asia/Tokyo",
    "日本": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "jst": "Asia/Tokyo",
    "北京": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "中国": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "纽约": "America/New_York",
    "new york": "America/New_York",
    "洛杉矶": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "伦敦": "Europe/London",
    "london": "Europe/London",
    "utc": "UTC",
}


class TimeProbe:
    """Read-only volatile time/date probe."""

    def run(self, text: str = "") -> dict[str, Any]:
        zone_name = self._extract_zone(text)
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            zone = datetime.now().astimezone().tzinfo or timezone.utc
            zone_name = str(getattr(zone, "key", None) or time.tzname[0] or "local")
        now = datetime.now(zone)
        offset = now.strftime("%z")
        offset_text = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
        summary = f"{zone_name} 当前时间是 {now.strftime('%Y-%m-%d %H:%M:%S')} ({offset_text})."
        return probe_payload(
            probe="time_probe",
            target=zone_name,
            status="ok",
            summary=summary,
            confidence=0.99,
            ttl_seconds=5,
            details={
                "timezone": zone_name,
                "iso": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"),
                "utc_offset": offset_text,
            },
            claims=[
                {
                    "key": f"time:{zone_name}:now",
                    "claim": summary,
                    "confidence": 0.99,
                    "ttl_seconds": 5,
                    "evidence": {"timezone": zone_name, "iso": now.isoformat(), "utc_offset": offset_text},
                }
            ],
        )

    def _extract_zone(self, text: str) -> str:
        lowered = text.lower()
        for marker, zone_name in ZONE_ALIASES.items():
            if marker in lowered:
                return zone_name
        utc_match = re.search(r"utc\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", lowered)
        if utc_match:
            sign, hours, minutes = utc_match.groups()
            return f"Etc/GMT{'-' if sign == '+' else '+'}{int(hours)}" if not minutes or minutes == "00" else "UTC"
        local = datetime.now().astimezone().tzinfo
        return str(getattr(local, "key", None) or time.tzname[0] or "UTC")
