"""Real InformationNeedRuntime -> LivingSourceRuntime integration smoke."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import Future
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from common.living_source_primitives import MAX_FLIGHT_LEASES  # noqa: E402
from interface.living_context_contract import CandidateNeed  # noqa: E402
from interface.living_source_contract import SourceConsent, SourceNeedBinding, canonical_utc  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402
from runtime.living_source_runtime import LivingSourceRuntime  # noqa: E402


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


class BlockingWeather:
    provider_id = "integration.weather.v1"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def read(self, context):
        self.started.set()
        self.release.wait(2)
        return {
            "status": "ok",
            "details": {
                "location": "Shanghai",
                "current": {"temperature_2m": 20.5, "weather_description": "clear"},
            },
        }


def _candidate() -> CandidateNeed:
    return CandidateNeed(
        blocked_judgment="Whether Shanghai travel conditions are suitable",
        evidence_kind="weather",
        why_now="The trip planning judgment depends on current conditions.",
        urgency=0.7,
        allowed_source_classes=["weather"],
        fallback_reaction="read",
        question="What are the current Shanghai conditions?",
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="veyra-living-source-core-") as temp_dir:
        store = WorldStateStore(Path(temp_dir) / "state")
        # Regression: WorldStateStore owns the public metadata envelope for
        # registered JSON state.  The source validator must accept these
        # defaults while still rejecting unknown metadata keys.
        default_state = store.read_json("living_source_state.json")
        assert default_state["source"] == "living_source_runtime"
        assert default_state["confidence"] == 1.0
        assert default_state["ttl_seconds"] == 0
        assert default_state["status"] == "fresh"
        assert isinstance(default_state["updated_at"], str) and default_state["updated_at"]
        assert isinstance(default_state["_state_revision"], int) and default_state["_state_revision"] >= 1
        needs = InformationNeedRuntime(store, clock=lambda: NOW)
        rows = needs.upsert_for_situation(
            situation_id="situation-shanghai",
            owner_id="core-user",
            session_id="core-session",
            needs=[_candidate()],
            source_event_id="core-event-1",
        )
        need_id = str(rows[0]["need_id"])
        projection = needs.authoritative_projection(need_id, owner_id="core-user", session_id="core-session")
        assert projection is not None and projection["record_digest"]

        provider = BlockingWeather()
        source = LivingSourceRuntime(
            store,
            current_need_resolver=lambda owner, session, selected: needs.authoritative_projection(
                selected, owner_id=owner, session_id=session
            ),
            providers={"weather": provider},
            clock=lambda: NOW,
        )
        binding = SourceNeedBinding(
            binding_id="core-source-binding",
            need_id=need_id,
            need_revision=int(projection["generation"]),
            need_digest=str(projection["record_digest"]),
            user_id="core-user",
            workspace_id="server-derived",
            session_id="core-session",
            situation_id="situation-shanghai",
            source="weather",
            parameters={"location": "Shanghai"},
            issued_at=canonical_utc(NOW),
            expires_at=canonical_utc(NOW + timedelta(days=1)),
        )
        source.register_binding(binding)
        source.grant_consent(
            SourceConsent(
                consent_id="core-weather-consent",
                user_id="core-user",
                workspace_id="server-derived",
                session_id="core-session",
                source="weather",
                purpose="core integration smoke",
                granted_at=canonical_utc(NOW),
                expires_at=canonical_utc(NOW + timedelta(days=1)),
            )
        )

        result: list = []
        thread = threading.Thread(
            target=lambda: result.append(
                source.request(
                    need_id,
                    "weather",
                    user_id="core-user",
                    session_id="core-session",
                    now=NOW,
                )
            )
        )
        thread.start()
        assert provider.started.wait(1)

        # Resolve and reopen through the authoritative core writer.  This
        # advances generation and record_digest while the source call is out.
        needs.resolve(
            need_id,
            owner_id="core-user",
            session_id="core-session",
            answered_by_event_id="core-answer-1",
        )
        reopened = needs.upsert_for_situation(
            situation_id="situation-shanghai",
            owner_id="core-user",
            session_id="core-session",
            needs=[_candidate()],
            source_event_id="core-event-2",
        )
        current = needs.authoritative_projection(need_id, owner_id="core-user", session_id="core-session")
        assert current is not None and current["generation"] == 2 and current["record_digest"] != projection["record_digest"]
        assert reopened

        provider.release.set()
        thread.join(2)
        assert result and result[0].status == "stale" and not result[0].payload and result[0].ttl_seconds == 0

        # A timed-out provider lease is a real resource reservation.  Filling
        # the bounded in-process cap must fail a new provider closed and expose
        # the degraded execution condition instead of overlapping the calls.
        previous_leases = source._leases
        source._leases = {
            f"cap-{index}": {"future": Future(), "executor": None, "timed_out": True}
            for index in range(MAX_FLIGHT_LEASES)
        }
        try:
            _, cap_status = source._invoke(object(), object(), timeout=0.05, lease_key="cap-new")
            assert cap_status == "unavailable"
            cap_projection = source.status(user_id="core-user", session_id="core-session", now=NOW)
            assert cap_projection["status"] == "degraded"
            assert cap_projection["execution"]["lease_count"] == MAX_FLIGHT_LEASES
            assert cap_projection["execution"]["available_slots"] == 0
        finally:
            source._leases = previous_leases
        return {"status": "passed", "need_generation": current["generation"], "receipt_status": result[0].status, "lease_cap": MAX_FLIGHT_LEASES}


def main() -> int:
    print("LIVING_SOURCE_CORE_INTEGRATION_OK", run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
