"""Provider-independent weather coverage and watch-cadence smoke.

This intentionally uses fakes only.  It proves the typed current/forecast
boundary and scheduler cadence without depending on Open-Meteo availability or
the live InformationNeed model/provider path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.living_context_contract import CandidateNeed, stable_evidence_target_digest  # noqa: E402
from interface.living_source_payload import (  # noqa: E402
    inject_weather_coverage,
    weather_coverage,
    weather_coverage_matches,
    weather_material_digest,
)
from probes.weather_probe import WeatherProbe  # noqa: E402
from runtime.living_context_orchestrator import LivingContextOrchestrator  # noqa: E402
from runtime.living_context_source_policy import LivingContextSourcePolicy  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402


NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


class _FakeProbe(WeatherProbe):
    def _geocode(self, location: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "name": location,
            "provider_id": 1796236,
            "feature_code": "PPLA",
            "population": 24874500,
            "latitude": 31.2,
            "longitude": 121.5,
            "timezone": "Asia/Shanghai",
        }

    def _current_weather(self, *args: Any) -> dict[str, Any]:
        return {"status": "ok", "current": {"time": "2026-08-23T20:00", "temperature_2m": 28, "weather_code": 1, "weather_description": "晴"}}

    def _forecast_weather(self, *args: Any) -> dict[str, Any]:
        return {"status": "ok", "forecast": {"date": "2026-08-29", "temperature_2m_max": 31, "temperature_2m_min": 24, "weather_code": 2, "weather_description": "多云", "precipitation_probability_max": 20}}


class _FakeNeeds:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.max_needs_per_situation = 8
        self.reopened: dict[str, Any] | None = None

    def list(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(self.row)]

    @staticmethod
    def record_digest_for_row(row: dict[str, Any]) -> str:
        # The orchestrator only needs a stable CAS token for this seam.
        return "a" * 64

    def upsert_for_situation(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.reopened = kwargs
        self.row = {**self.row, "status": "open", "generation": int(self.row.get("generation") or 1) + 1}
        return [dict(self.row)]


class _FakeCore:
    def __init__(self, row: dict[str, Any]) -> None:
        self.needs = _FakeNeeds(row)


class _FakeSource:
    def __init__(self, receipt: dict[str, Any], binding: dict[str, Any]) -> None:
        self.receipt = receipt
        self.binding = binding

    def state_snapshot(self) -> dict[str, Any]:
        return {"receipts": {self.receipt["receipt_id"]: self.receipt}, "bindings": {self.binding["binding_id"]: self.binding}}


def _expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")


def run() -> dict[str, Any]:
    probe = _FakeProbe()
    untyped = probe.run(text="上海明天天气")
    _expect(untyped["status"] == "missing_target", "free text never becomes a weather location")
    current = probe.run(location="上海")
    forecast = probe.run(location="上海", target_date="2026-08-29")
    _expect(current["status"] == "ok" and current["details"]["coverage"]["kind"] == "current", "current coverage")
    _expect(forecast["status"] == "ok" and forecast["details"]["coverage"]["kind"] == "forecast_day", "forecast coverage")
    _expect(
        WeatherProbe._select_geocode_candidate(
            [
                {"id": 1, "feature_code": "PPLA", "population": 24000000},
                {"id": 2, "feature_code": "PPL", "population": 1000},
            ]
        )["id"] == 1,
        "administrative provider place dominates ordinary place",
    )
    _expect(
        WeatherProbe._select_geocode_candidate(
            [
                {"id": 1, "feature_code": "PPL", "population": 100000},
                {"id": 2, "feature_code": "PPL", "population": 90000},
            ]
        ) is None,
        "comparable provider places remain ambiguous",
    )
    target_a = {
        "location": "上海",
        "target_date": "2026-08-29",
        "observation_requirement": {"coverage": "forecast_day", "metrics": ["temperature_2m_max"]},
    }
    target_b = {
        **target_a,
        "observation_requirement": {"coverage": "forecast_day", "metrics": ["precipitation_probability_max"]},
    }
    _expect(
        InformationNeedRuntime.stable_need_id(
            situation_id="sit",
            evidence_kind="weather",
            evidence_target_digest=stable_evidence_target_digest(target_a),
            observation_mode="once",
        )
        != InformationNeedRuntime.stable_need_id(
            situation_id="sit",
            evidence_kind="weather",
            evidence_target_digest=stable_evidence_target_digest(target_b),
            observation_mode="once",
        ),
        "Need identity distinguishes same-date different metric requirements",
    )
    _expect(
        not weather_coverage_matches(
            {"coverage": current["details"]["coverage"]},
            location="上海",
            target_date="2026-08-29",
        )[0],
        "current receipt cannot resolve a forecast target",
    )
    current_payload_for_forecast = inject_weather_coverage(
        {"facts": {"location": "上海", "current": {"temperature_2m": 28}}},
        location="上海",
        target_date="2026-08-29",
    )
    _expect("coverage" not in current_payload_for_forecast["facts"], "current-shaped provider cannot be relabeled as forecast")
    _expect(
        weather_coverage_matches(
            {"coverage": forecast["details"]["coverage"], "forecast": forecast["details"]["forecast"]},
            location="上海",
            target_date="2026-08-29",
        )[0],
        "exact forecast coverage resolves",
    )
    _expect(
        not weather_coverage_matches(
            {
                "coverage": forecast["details"]["coverage"],
                "forecast": {"temperature_2m_max": 31},
            },
            location="上海",
            target_date="2026-08-29",
            observation_requirement={
                "coverage": "forecast_day",
                "metrics": ["temperature_2m_max", "temperature_2m_min"],
            },
        )[0],
        "forecast missing a required metric stays unresolved",
    )
    _expect(
        not weather_coverage_matches(
            {"coverage": forecast["details"]["coverage"], "forecast": forecast["details"]["forecast"]},
            location="杭州",
            target_date="2026-08-29",
        )[0],
        "wrong forecast location stays unresolved",
    )
    _expect(
        not weather_coverage_matches(
            {"coverage": forecast["details"]["coverage"], "forecast": {**forecast["details"]["forecast"], "date": "2026-08-30"}},
            location="上海",
            target_date="2026-08-29",
        )[0],
        "forecast date is part of the typed observation",
    )
    _expect(
        not weather_coverage_matches(
            {"coverage": current["details"]["coverage"]}, location="上海"
        )[0],
        "current coverage without current facts cannot resolve",
    )
    _expect(
        weather_coverage_matches(
            {"coverage": current["details"]["coverage"], "current": current["details"]["current"]},
            location="上海",
        )[0],
        "exact current facts resolve",
    )
    _expect(
        weather_material_digest({"current": {"temperature_2m": 28, "time": "a"}})
        == weather_material_digest({"current": {"temperature_2m": 28, "time": "b"}}),
        "observation timestamp does not create a material delta",
    )

    policy_need = {
        "owner_id": "u",
        "session_id": "s",
        "need_id": "n",
        "situation_id": "sit",
        "generation": 1,
        "record_digest": "b" * 64,
        "evidence_kind": "weather",
        "allowed_source_classes": ["weather"],
        "evidence_target": {"location": "上海", "target_date": "2026-08-29"},
    }
    binding = LivingContextSourcePolicy().derive_binding(
        situation={"situation_id": "sit"},
        need=policy_need,
        now=NOW,
    )
    _expect(binding is not None and binding.parameters == {"location": "上海", "target_date": "2026-08-29"}, "typed target binding", binding)
    policy = LivingContextSourcePolicy()
    _expect(
        policy.watch_cadence_seconds(
            "calendar",
            need={"observation_requirement": {"coverage": "window"}},
        ) == 900,
        "non-weather watch cadence comes from source capability",
    )
    _expect(
        policy.watch_cadence_seconds(
            "weather",
            need={"observation_requirement": {"coverage": "current"}},
        ) is None,
        "current coverage follows source freshness rather than forecast cadence",
    )

    receipt = {
        "receipt_id": "r",
        "observed_at": NOW.isoformat(),
        "status": "ok",
        "fresh_until": (NOW + timedelta(minutes=15)).isoformat(),
        "ttl_seconds": 900,
        "need_id": "n",
        "binding_id": "b",
        "user_id": "u",
        "session_id": "s",
    }
    binding_row = {"binding_id": "b", "need_revision": 1}
    watch_row = {
        **policy_need,
        "blocked_judgment": "forecast conditions",
        "why_now": "the next observation boundary arrived",
        "urgency": 0.5,
        "expires_at": None,
        "fallback_reaction": "wait",
        "question": "",
        "allowed_source_classes": ["weather"],
        "status": "resolved",
        "observation_mode": "watch",
        "unknown_binding": "forecast unknown",
        "evidence_target_digest": forecast["details"]["coverage"]["target_digest"],
        "updated_at": NOW.isoformat(),
    }
    # The scheduler uses the source receipt's observed time plus the typed
    # six-hour watch cadence, not the provider's 15-minute freshness TTL.
    source = _FakeSource(receipt, binding_row)
    core = _FakeCore(watch_row)
    orchestrator = LivingContextOrchestrator(core, object(), source, clock=lambda: NOW + timedelta(hours=5))
    situation = {"situation_id": "sit", "semantic": {}}
    _expect(orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s") is None, "watch does not reopen at provider TTL")
    orchestrator._clock = lambda: NOW + timedelta(hours=6)
    reopened = orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s")
    _expect(reopened is not None and core.needs.reopened is not None, "watch reopens at six hours")
    _expect(core.needs.reopened.get("evidence_targets") == [watch_row["evidence_target"]], "reopen preserves target")
    _expect(core.needs.reopened.get("unknown_bindings") == {watch_row["blocked_judgment"]: watch_row["unknown_binding"]}, "reopen preserves unknown binding")

    current_watch_row = {**watch_row, "evidence_target": {"location": "上海"}, "evidence_target_digest": current["details"]["coverage"]["target_digest"]}
    current_core = _FakeCore(current_watch_row)
    current_orchestrator = LivingContextOrchestrator(current_core, object(), source, clock=lambda: NOW + timedelta(minutes=15))
    _expect(current_orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s") is not None, "current watch follows source freshness")

    once_row = {**watch_row, "observation_mode": "once"}
    once_core = _FakeCore(once_row)
    once_orchestrator = LivingContextOrchestrator(once_core, object(), source, clock=lambda: NOW + timedelta(days=2))
    _expect(once_orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s") is None, "once Need never reopens")

    horizon_receipt = {
        **receipt,
        "receipt_id": "r-horizon",
        "status": "empty",
        "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_seconds": 300,
        "payload": {"facts": {"next_eligible_at": (NOW + timedelta(days=2)).isoformat()}},
    }
    horizon_source = _FakeSource(horizon_receipt, binding_row)
    horizon_core = _FakeCore(watch_row)
    horizon_orchestrator = LivingContextOrchestrator(
        horizon_core,
        object(),
        horizon_source,
        clock=lambda: NOW + timedelta(days=1),
    )
    _expect(
        horizon_orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s") is None,
        "forecast horizon does not retry before next_eligible_at",
    )
    horizon_orchestrator._clock = lambda: NOW + timedelta(days=2)
    _expect(
        horizon_orchestrator._ensure_observation_need(situation, owner_id="u", session_id="s") is not None,
        "forecast horizon reopens at next_eligible_at",
    )
    return {"status": "passed", "current_kind": current["details"]["coverage"]["kind"], "forecast_kind": forecast["details"]["coverage"]["kind"], "watch_cadence_hours": 6}


def main() -> int:
    print("WEATHER_COVERAGE_SMOKE_OK", run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
