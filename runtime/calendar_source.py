"""Read-only calendar adapters used by the governed Living Source runtime.

The ICS adapter is deterministic and easy to fixture in tests.  The optional
macOS adapter is disabled by default, has a fixed executable/script, and never
accepts a command or path from a source request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from interface.event_schema import utc_now_iso
from interface.living_source_contract import SourceContext, parse_utc


MAX_ICS_BYTES = 1_000_000
MAX_MACOS_STDOUT_BYTES = 256_000
MAX_MACOS_ERROR_BYTES = 4096
MAX_EVENTS = 200


class CalendarProvider(Protocol):
    def read(self, context: SourceContext) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    title: str
    starts_at: str
    ends_at: str
    location: str = ""
    description: str = ""
    all_day: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "location": self.location,
            "description": self.description,
            "all_day": self.all_day,
        }


def _unescape_ics(value: str) -> str:
    return (
        str(value or "")
        .replace(r"\n", "\n")
        .replace(r"\N", "\n")
        .replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\\", "\\")
        .strip()
    )


def _parse_ics_time(value: str, *, tzid: str | None = None) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("calendar time is empty")
    all_day = len(raw) == 8 and raw.isdigit()
    if all_day:
        parsed = datetime.combine(date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"), datetime.min.time())
        return canonical_time(parsed.replace(tzinfo=timezone.utc)), True
    if raw.endswith("Z"):
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(tzid or "UTC"))
        except Exception:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return canonical_time(parsed), False


def canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_process_output(value: Any) -> str:
    """Return a bounded lower-case diagnostic for local error classification.

    The content is never returned to the product or receipt; only server-owned
    permission classification is exposed.  This keeps TCC/process output out
    of user-facing state even when osascript includes local details.
    """

    if isinstance(value, bytes):
        return value[:MAX_MACOS_ERROR_BYTES].decode("utf-8", errors="ignore").lower()
    if isinstance(value, str):
        return value[:MAX_MACOS_ERROR_BYTES].lower()
    return ""


def _parse_ics(text: str) -> list[CalendarEvent]:
    unfolded: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events: list[CalendarEvent] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None
    for line in unfolded:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                try:
                    start, start_all_day = _property_time(current, "DTSTART")
                    end, end_all_day = _property_time(current, "DTEND", fallback=start)
                    event_id = _unescape_ics(current.get("UID", ({}, ""))[1])
                    if not event_id:
                        event_id = f"ics:{len(events) + 1}"
                    events.append(
                        CalendarEvent(
                            event_id=event_id[:240],
                            title=_unescape_ics(current.get("SUMMARY", ({}, ""))[1])[:600],
                            starts_at=start,
                            ends_at=end,
                            location=_unescape_ics(current.get("LOCATION", ({}, ""))[1])[:600],
                            description=_unescape_ics(current.get("DESCRIPTION", ({}, ""))[1])[:1200],
                            all_day=start_all_day or end_all_day,
                        )
                    )
                except (TypeError, ValueError, KeyError):
                    # One malformed event must not turn a valid feed into an
                    # invented result.  It is simply omitted from the typed
                    # projection; the source receipt still reports its count.
                    pass
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        parts = left.split(";")
        name = parts[0].upper()
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                key, selected = part.split("=", 1)
                params[key.upper()] = selected
        current[name] = (params, value)
    return events[:MAX_EVENTS]


def _property_time(
    properties: Mapping[str, tuple[dict[str, str], str]],
    name: str,
    *,
    fallback: str | None = None,
) -> tuple[str, bool]:
    if name not in properties:
        if fallback is not None:
            return fallback, False
        raise ValueError(f"missing {name}")
    params, value = properties[name]
    return _parse_ics_time(value, tzid=params.get("TZID"))


def _window(context: SourceContext) -> tuple[datetime, datetime]:
    params = context.parameters
    start = params.get("window_start")
    end = params.get("window_end")
    if not start or not end:
        raise ValueError("calendar request needs a bounded window")
    selected_start = parse_utc(str(start))
    selected_end = parse_utc(str(end))
    if selected_end <= selected_start:
        raise ValueError("calendar window is invalid")
    return selected_start, selected_end


def _project_events(events: list[CalendarEvent], start: datetime, end: datetime) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for event in events:
        try:
            event_start = parse_utc(event.starts_at)
            event_end = parse_utc(event.ends_at)
        except ValueError:
            continue
        if event_end <= event_start:
            continue
        if event_start < end and event_end > start:
            selected.append(event.to_dict())
    selected.sort(key=lambda item: (item["starts_at"], item["event_id"]))
    return selected[:MAX_EVENTS]


class IcsCalendarProvider:
    """Read an explicitly configured ICS document; never accepts request paths."""

    provider_id = "calendar.ics.v1"

    configured = True
    system_permission = "not_required"

    def __init__(self, ics_text: str | None = None, *, path: str | Path | None = None, ttl_seconds: int = 300) -> None:
        if ics_text is not None and path is not None:
            raise ValueError("configure either ics_text or path, not both")
        self._ics_text = ics_text
        self._path = Path(path).expanduser() if path is not None else None
        self._ttl_seconds = max(1, min(int(ttl_seconds), 86400))

    @property
    def configured(self) -> bool:
        return self._ics_text is not None or self._path is not None

    def read(self, context: SourceContext) -> dict[str, Any]:
        try:
            start, end = _window(context)
        except ValueError as exc:
            return {"status": "unknown", "reason": str(exc), "events": [], "ttl_seconds": 60}
        if self._ics_text is None and self._path is None:
            return {"status": "unavailable", "reason": "calendar_not_configured", "events": [], "ttl_seconds": 300}
        try:
            if self._ics_text is not None:
                if len(self._ics_text.encode("utf-8")) > MAX_ICS_BYTES:
                    return {"status": "unknown", "reason": "calendar_feed_too_large", "events": [], "ttl_seconds": 60}
                raw = self._ics_text
            else:
                assert self._path is not None
                raw_bytes = self._path.read_bytes()
                if len(raw_bytes) > MAX_ICS_BYTES:
                    return {"status": "unknown", "reason": "calendar_feed_too_large", "events": [], "ttl_seconds": 60}
                raw = raw_bytes.decode("utf-8-sig")
            events = _project_events(_parse_ics(raw), start, end)
        except (OSError, UnicodeError, ValueError) as exc:
            return {"status": "unavailable", "reason": f"calendar_read_failed:{type(exc).__name__}", "events": [], "ttl_seconds": 60}
        status = "ok" if events else "empty"
        return {
            "status": status,
            "summary": f"Calendar returned {len(events)} event(s) in the bounded window.",
            "events": events,
            "window_start": canonical_time(start),
            "window_end": canonical_time(end),
            "provider": self.provider_id,
            "observed_at": utc_now_iso(),
            "ttl_seconds": self._ttl_seconds,
        }


class DisabledCalendarProvider:
    provider_id = "calendar.disabled.v1"
    configured = False
    system_permission = "not_configured"

    def read(self, context: SourceContext) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "calendar_not_configured",
            "events": [],
            "provider": self.provider_id,
            "ttl_seconds": 300,
        }


_MACOS_JXA = r'''
function run(argv) {
  // The script is fixed by the server. argv contains only validated UTC
  // window bounds supplied by the server-bound InformationNeed.
  const start = new Date(argv[0]);
  const end = new Date(argv[1]);
  const app = Application("Calendar");
  const rows = [];
  for (const cal of app.calendars()) {
    // Query the overlap set in Calendar itself.  The server-issued window is
    // a source boundary; do not materialise a user's complete calendar and
    // filter it after the fact.
    const events = cal.events.whose({
      startDate: {_lessThan: end},
      endDate: {_greaterThan: start}
    })();
    for (const event of events) {
      const begins = event.startDate();
      const finishes = event.endDate();
      rows.push({id: String(event.uid()), title: String(event.summary()),
        starts_at: begins.toISOString(), ends_at: finishes.toISOString(),
        location: String(event.location() || "")});
    }
  }
  return JSON.stringify(rows.slice(0, 200));
}
'''


class MacOSCalendarProvider:
    """Optional macOS Calendar reader; disabled and bounded by default."""

    provider_id = "calendar.macos_osascript.v1"

    def __init__(
        self,
        *,
        enabled: bool = False,
        timeout_seconds: float = 3.0,
        runner: Callable[[list[str], float], str] | None = None,
        system_permission: str = "unknown",
    ) -> None:
        self.enabled = bool(enabled)
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 10.0))
        self.runner = runner
        selected_permission = str(system_permission or "unknown").strip().lower()
        if selected_permission not in {"unknown", "ready", "denied"}:
            raise ValueError("macOS Calendar system permission must be unknown, ready, or denied")
        self._system_permission = selected_permission if self.enabled else "not_configured"

    @property
    def configured(self) -> bool:
        return self.enabled

    @property
    def system_permission(self) -> str:
        return self._system_permission

    def read(self, context: SourceContext) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "unavailable", "reason": "calendar_macos_disabled", "events": [], "ttl_seconds": 300}
        if self._system_permission == "denied":
            return {"status": "denied", "reason": "calendar_macos_permission_denied", "events": [], "ttl_seconds": 60}
        try:
            start, end = _window(context)
            args = ["/usr/bin/osascript", "-l", "JavaScript", "-e", _MACOS_JXA, "--", canonical_time(start), canonical_time(end)]
            body = self.runner(args, self.timeout_seconds) if self.runner else self._run(args)
            if isinstance(body, bytes):
                if len(body) > MAX_MACOS_STDOUT_BYTES:
                    return {"status": "unknown", "reason": "calendar_macos_output_too_large", "events": [], "ttl_seconds": 60}
                body = body.decode("utf-8")
            elif len(str(body).encode("utf-8")) > MAX_MACOS_STDOUT_BYTES:
                return {"status": "unknown", "reason": "calendar_macos_output_too_large", "events": [], "ttl_seconds": 60}
            raw = json.loads(body)
            if not isinstance(raw, list):
                raise ValueError("Calendar script returned a non-list")
            events: list[CalendarEvent] = []
            for item in raw[:MAX_EVENTS]:
                if not isinstance(item, Mapping):
                    continue
                event_id = str(item.get("id") or "").strip()
                starts_at = str(item.get("starts_at") or "").strip()
                ends_at = str(item.get("ends_at") or "").strip()
                if not event_id or not starts_at or not ends_at:
                    continue
                events.append(CalendarEvent(event_id=event_id[:240], title=str(item.get("title") or "")[:600], starts_at=canonical_time(parse_utc(starts_at)), ends_at=canonical_time(parse_utc(ends_at)), location=str(item.get("location") or "")[:600]))
            projected = _project_events(events, start, end)
            self._system_permission = "ready"
            return {"status": "ok" if projected else "empty", "summary": f"Calendar returned {len(projected)} event(s) in the bounded window.", "events": projected, "provider": self.provider_id, "observed_at": utc_now_iso(), "ttl_seconds": 300}
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "reason": "calendar_macos_timeout", "events": [], "ttl_seconds": 60}
        except subprocess.CalledProcessError as exc:
            # CalledProcessError.__str__ only carries the command/returncode;
            # TCC's -1743/not-authorized signal is in bounded stderr/stdout.
            # Classify it without ever echoing the process text.
            error_text = " ".join(
                part
                for part in (
                    _bounded_process_output(getattr(exc, "stderr", None)),
                    _bounded_process_output(getattr(exc, "stdout", None)),
                )
                if part
            )
            if any(marker in error_text for marker in ("not authorized", "not permitted", "not allowed", "-1743")):
                self._system_permission = "denied"
                return {"status": "denied", "reason": "calendar_macos_permission_denied", "events": [], "ttl_seconds": 60}
            return {"status": "unknown", "reason": "calendar_macos_command_failed", "events": [], "ttl_seconds": 60}
        except PermissionError:
            self._system_permission = "denied"
            return {"status": "denied", "reason": "calendar_macos_permission_denied", "events": [], "ttl_seconds": 60}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"status": "unknown", "reason": f"calendar_macos_unknown:{type(exc).__name__}", "events": [], "ttl_seconds": 60}

    def _run(self, args: list[str]) -> bytes:
        completed = subprocess.run(args, check=True, capture_output=True, text=False, timeout=self.timeout_seconds, env={"PATH": "/usr/bin:/bin"})
        return completed.stdout


class CalendarSource:
    """Thin source capability wrapper with no caller-controlled provider args."""

    def __init__(self, provider: CalendarProvider | None = None) -> None:
        self.provider = provider or DisabledCalendarProvider()

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "provider_id", "calendar.unknown.v1"))

    @property
    def configured(self) -> bool:
        selected = getattr(self.provider, "configured", None)
        if isinstance(selected, bool):
            return selected
        return self.provider_id != "calendar.disabled.v1"

    @property
    def system_permission(self) -> str:
        selected = str(getattr(self.provider, "system_permission", "not_required") or "not_required").strip().lower()
        return selected if selected in {"not_configured", "not_required", "unknown", "ready", "denied"} else "unknown"

    def read(self, context: SourceContext) -> dict[str, Any]:
        try:
            result = self.provider.read(context)
        except Exception as exc:  # provider failures become explicit unknowns
            return {"status": "unknown", "reason": f"calendar_provider_error:{type(exc).__name__}", "events": [], "ttl_seconds": 60}
        return result if isinstance(result, dict) else {"status": "unknown", "reason": "calendar_provider_malformed", "events": [], "ttl_seconds": 60}


__all__ = ["CalendarEvent", "CalendarProvider", "CalendarSource", "DisabledCalendarProvider", "IcsCalendarProvider", "MacOSCalendarProvider"]
