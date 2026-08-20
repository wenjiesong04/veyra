"""Focused smoke for governed Living Source bindings and typed receipts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_source_contract import (  # noqa: E402
    LivingSourceContractError,
    SourceConsent,
    SourceNeedBinding,
    canonical_utc,
)
from runtime.calendar_source import CalendarSource, IcsCalendarProvider, MAX_MACOS_STDOUT_BYTES, MacOSCalendarProvider  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402
from runtime.living_source_runtime import LivingSourceRuntime, SourceAdmissionError, SourceCapacityError, SourceStateCorruptError  # noqa: E402


FIXED_NOW = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)


class NeedCatalog:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def add(self, need_id: str, source: str, *, owner: str = "u-v1", session: str = "s-v1", situation: str | None = None, revision: int = 1) -> dict:
        row = {
            "schema_version": "veyra.information_need.v1",
            "need_id": need_id,
            "situation_id": situation or f"situation-{need_id}",
            "owner_id": owner,
            "session_id": session,
            "blocked_judgment": f"Need typed {source} evidence",
            "evidence_kind": source if source in {"calendar", "weather", "public_web"} else "user",
            "why_now": "V1 source smoke",
            "urgency": 0.5,
            "allowed_source_classes": [source if source != "user_answer" else "user"],
            "fallback_reaction": "read",
            "question": "",
            "status": "open",
            "generation": revision,
            "source_event_id": f"event-{need_id}",
            "created_at": canonical_utc(FIXED_NOW),
            "updated_at": canonical_utc(FIXED_NOW),
        }
        row["record_digest"] = self._record_digest(row)
        self.rows[need_id] = row
        return row

    @staticmethod
    def _record_digest(row: dict) -> str:
        digest_payload = {key: value for key, value in row.items() if key not in {"created_at", "updated_at", "record_digest"}}
        return InformationNeedRuntime._fingerprint_payload(digest_payload)

    def refresh_digest(self, need_id: str) -> str:
        row = self.rows[need_id]
        row["record_digest"] = self._record_digest(row)
        return str(row["record_digest"])

    def resolve(self, user_id: str, session_id: str, need_id: str) -> dict | None:
        row = self.rows.get(need_id)
        if row is None or row["owner_id"] != user_id or row["session_id"] != session_id:
            return None
        return dict(row)


def _binding(catalog: NeedCatalog, need_id: str, source: str, parameters: dict[str, object], *, owner: str = "u-v1", session: str = "s-v1", now: datetime = FIXED_NOW) -> SourceNeedBinding:
    row = catalog.rows[need_id]
    return SourceNeedBinding(
        binding_id=f"binding-{need_id}",
        need_id=need_id,
        need_revision=int(row["generation"]),
        need_digest=str(row["record_digest"]),
        user_id=owner,
        workspace_id="server-derived",
        session_id=session,
        situation_id=str(row["situation_id"]),
        source=source,  # type: ignore[arg-type]
        parameters=parameters,
        issued_at=canonical_utc(now),
        expires_at=canonical_utc(now + timedelta(days=1)),
    )


def _consent(source: str, *, now: datetime = FIXED_NOW) -> SourceConsent:
    return SourceConsent(
        consent_id=f"consent-{source}",
        user_id="u-v1",
        workspace_id="server-derived",
        session_id="s-v1",
        source=source,  # type: ignore[arg-type]
        purpose=f"V1 read-only {source}",
        granted_at=canonical_utc(now),
        expires_at=canonical_utc(now + timedelta(days=1)),
    )


class _CountingProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.lock = threading.Lock()

    def read(self, context):
        with self.lock:
            self.calls += 1
        return dict(self.payload)


class _ConcurrentProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def read(self, context):
        with self.lock:
            self.calls += 1
        self.started.set()
        self.release.wait(2)
        return {"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20, "weather_description": "clear"}}}


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="veyra-living-source-") as temp_dir:
        catalog = NeedCatalog()
        catalog.add("core-weather", "weather")
        catalog.add("core-calendar", "calendar")
        catalog.add("core-web", "public_web")
        catalog.add("core-answer", "user_answer")
        weather = _CountingProvider({"status": "ok", "summary": "Shanghai clear", "details": {"location": "Shanghai", "current": {"temperature_2m": 20, "weather_description": "clear"}}, "ttl_seconds": 10})
        web = _CountingProvider({"status": "ok", "details": {"results": [{"title": "controlled", "url": "https://example.com", "snippet": "one result"}]}})
        calendar_text = """BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:trip-1@example.test\nSUMMARY:Shanghai meeting\nDTSTART:20260820T090000Z\nDTEND:20260820T100000Z\nLOCATION:Shanghai\nDESCRIPTION:private notes must not enter the receipt\nEND:VEVENT\nEND:VCALENDAR\n"""
        state_store = WorldStateStore(Path(temp_dir) / "state")
        runtime = LivingSourceRuntime(state_store, current_need_resolver=catalog.resolve, providers={"weather": weather, "public_web": web}, calendar_source=CalendarSource(IcsCalendarProvider(ics_text=calendar_text)), clock=lambda: FIXED_NOW)
        try:
            _binding(catalog, "core-web", "public_web", {"url": "https://attacker.invalid"})
        except LivingSourceContractError:
            pass
        else:
            raise AssertionError("arbitrary URL was accepted as source binding parameter")

        weather_binding = _binding(catalog, "core-weather", "weather", {"location": "Shanghai"})
        runtime.register_binding(weather_binding)
        denied = runtime.request("core-weather", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert denied.status == "denied" and weather.calls == 0
        runtime.grant_consent(_consent("weather"))
        first_weather = runtime.request("core-weather", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        replay_weather = runtime.request("core-weather", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=1))
        assert first_weather.status == "ok" and first_weather.to_dict() == replay_weather.to_dict() and weather.calls == 1
        assert first_weather.payload["facts"]["location"] == "Shanghai"
        stale_weather = runtime.get_receipt(first_weather.receipt_id, user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=11))
        assert stale_weather.status == "stale" and not stale_weather.payload and stale_weather.ttl_seconds == 0
        saved_clock = runtime._clock
        runtime._clock = lambda: FIXED_NOW + timedelta(seconds=11)
        stale_weather_default_clock = runtime.get_receipt(first_weather.receipt_id, user_id="u-v1", session_id="s-v1")
        runtime._clock = saved_clock
        assert stale_weather_default_clock.status == "stale" and not stale_weather_default_clock.payload and stale_weather_default_clock.ttl_seconds == 0
        refreshed_weather = runtime.request("core-weather", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=11))
        assert refreshed_weather.status == "ok" and weather.calls == 2
        restarted_runtime = LivingSourceRuntime(
            state_store,
            current_need_resolver=catalog.resolve,
            providers={"weather": weather, "public_web": web},
            calendar_source=CalendarSource(IcsCalendarProvider(ics_text=calendar_text)),
            clock=lambda: FIXED_NOW,
        )
        restarted_weather = restarted_runtime.request(
            "core-weather", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=12)
        )
        assert restarted_weather.status == "ok" and restarted_weather.to_dict() == refreshed_weather.to_dict() and weather.calls == 2

        calendar_binding = _binding(catalog, "core-calendar", "calendar", {"window_start": canonical_utc(FIXED_NOW + timedelta(days=2)), "window_end": canonical_utc(FIXED_NOW + timedelta(days=4))})
        runtime.register_binding(calendar_binding)
        runtime.grant_consent(_consent("calendar"))
        calendar_receipt = runtime.request("core-calendar", "calendar", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert calendar_receipt.status == "ok" and len(calendar_receipt.payload["facts"]["events"]) == 1
        assert "description" not in calendar_receipt.payload["facts"]["events"][0]

        web_binding = _binding(catalog, "core-web", "public_web", {"query": "上海出差准备", "max_results": 3})
        runtime.register_binding(web_binding)
        runtime.grant_consent(_consent("public_web"))
        web_receipt = runtime.request("core-web", "public_web", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert web_receipt.status == "ok" and web_receipt.payload["facts"]["results"][0]["url"].startswith("https://") and web.calls == 1

        answer_binding = _binding(catalog, "core-answer", "user_answer", {})
        runtime.register_binding(answer_binding)
        pending = runtime.request("core-answer", "user_answer", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert pending.status == "pending"
        try:
            runtime.submit_user_answer(pending.request_id, "wrong owner", user_id="other", session_id="s-v1", now=FIXED_NOW)
        except SourceAdmissionError:
            pass
        else:
            raise AssertionError("cross-owner user answer was accepted")
        answer = runtime.submit_user_answer(pending.request_id, "面试材料已经准备好了", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert answer.status == "ok" and answer.payload["facts"]["answer"]

        product_status = runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert product_status["status"] == "ok"
        assert product_status["scope"] == {"user_id": "u-v1", "session_id": "s-v1"}
        assert product_status["receipts"]["weather"]["count"] >= 1
        assert product_status["receipts"]["weather"]["fresh_count"] >= 1
        assert "bindings" not in product_status and "requests" not in product_status
        assert all("parameters" not in row for row in product_status["receipts"].values())
        other_status = runtime.status(user_id="other", session_id="s-v1", now=FIXED_NOW)
        assert other_status["status"] == "ok" and all(item["count"] == 0 for item in other_status["receipts"].values())

        catalog.add("core-agent", "agent_research")
        agent_binding = _binding(catalog, "core-agent", "agent_research", {"topic": "bounded topic"})
        runtime.register_binding(agent_binding)
        assert runtime.request("core-agent", "agent_research", user_id="u-v1", session_id="s-v1", now=FIXED_NOW).status == "denied"
        assert "needs" not in runtime.state_snapshot()

        concurrent_catalog = NeedCatalog()
        concurrent_catalog.add("core-concurrent", "weather")
        concurrent_store = WorldStateStore(Path(temp_dir) / "concurrent-state")
        concurrent_provider = _ConcurrentProvider()
        concurrent_runtime = LivingSourceRuntime(concurrent_store, current_need_resolver=concurrent_catalog.resolve, providers={"weather": concurrent_provider}, clock=lambda: FIXED_NOW)
        concurrent_runtime.register_binding(_binding(concurrent_catalog, "core-concurrent", "weather", {"location": "Shanghai"}))
        concurrent_runtime.grant_consent(_consent("weather"))
        results: list = []
        first_thread = threading.Thread(target=lambda: results.append(concurrent_runtime.request("core-concurrent", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)))
        first_thread.start()
        assert concurrent_provider.started.wait(1)
        second = concurrent_runtime.request("core-concurrent", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert second.status == "pending" and concurrent_provider.calls == 1
        concurrent_provider.release.set()
        first_thread.join(2)
        assert results and results[0].status == "ok" and concurrent_provider.calls == 1

        capacity_catalog = NeedCatalog()
        capacity_catalog.add("core-capacity", "weather")
        capacity_catalog.add("core-blocked", "weather")
        capacity_store = WorldStateStore(Path(temp_dir) / "capacity-state")
        capacity_provider = _ConcurrentProvider()
        capacity_runtime = LivingSourceRuntime(capacity_store, max_receipts=1, current_need_resolver=capacity_catalog.resolve, providers={"weather": capacity_provider}, clock=lambda: FIXED_NOW)
        capacity_runtime.register_binding(_binding(capacity_catalog, "core-capacity", "weather", {"location": "Shanghai"}))
        capacity_runtime.grant_consent(_consent("weather"))
        capacity_thread = threading.Thread(target=lambda: capacity_runtime.request("core-capacity", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW))
        capacity_thread.start()
        assert capacity_provider.started.wait(1)
        capacity_runtime.register_binding(_binding(capacity_catalog, "core-blocked", "weather", {"location": "Beijing"}))
        try:
            capacity_runtime.request("core-blocked", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        except SourceCapacityError:
            pass
        else:
            raise AssertionError("capacity pressure evicted an inflight referenced receipt")
        capacity_provider.release.set()
        capacity_thread.join(2)

        corrupt_store = WorldStateStore(Path(temp_dir) / "corrupt-state")
        corrupt_store.write_json("living_source_state.json", {"schema_version": "veyra.living_source.runtime.older.v0", "bindings": {}})
        corrupt_runtime = LivingSourceRuntime(corrupt_store, current_need_resolver=catalog.resolve)
        assert corrupt_runtime.state_snapshot()["state_corrupt"] is True
        try:
            corrupt_runtime.register_binding(weather_binding)
        except SourceStateCorruptError:
            pass
        else:
            raise AssertionError("unsupported schema was rewritten")
        empty_store = WorldStateStore(Path(temp_dir) / "empty-state")
        empty_path = empty_store.path_for("living_source_state.json")
        empty_path.parent.mkdir(parents=True, exist_ok=True)
        empty_path.write_bytes(b"")
        empty_runtime = LivingSourceRuntime(empty_store, current_need_resolver=catalog.resolve)
        before = empty_path.read_bytes()
        assert empty_runtime.state_snapshot()["state_corrupt"] is True
        try:
            empty_runtime.register_binding(weather_binding)
        except SourceStateCorruptError:
            pass
        else:
            raise AssertionError("zero-byte schema was rewritten")
        assert empty_path.read_bytes() == before

        context = type("Context", (), {"parameters": {"window_start": canonical_utc(FIXED_NOW), "window_end": canonical_utc(FIXED_NOW + timedelta(days=1))}})()
        huge = MacOSCalendarProvider(enabled=True, runner=lambda args, timeout: b"x" * (MAX_MACOS_STDOUT_BYTES + 1))
        assert huge.read(context)["reason"] == "calendar_macos_output_too_large"
        return {"status": "passed", "sources": ["user_answer", "calendar", "weather", "public_web"], "concurrency_provider_calls": concurrent_provider.calls, "state_file": "living_source_state.json"}


def main() -> int:
    print("LIVING_SOURCE_SMOKE_OK", run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
