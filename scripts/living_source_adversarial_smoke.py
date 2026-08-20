"""Adversarial smoke for Living Source merge blockers.

This intentionally uses temporary WorldState roots and fake providers.  It is
not product evidence; it proves fail-closed state, identity, consent, retry,
payload and Calendar boundaries.
"""

from __future__ import annotations

from datetime import timedelta
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from common.living_source_primitives import MAX_BINDINGS  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import CandidateNeed, LivingContextCandidate  # noqa: E402
from interface.living_source_contract import LivingSourceContractError, SourceNeedBinding, canonical_utc  # noqa: E402
from runtime.calendar_source import IcsCalendarProvider, CalendarSource  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402
from runtime.living_source_runtime import LivingSourceRuntime, SourceAdmissionError, SourceCapacityError, SourceStateCorruptError  # noqa: E402
from scripts.living_source_smoke import FIXED_NOW, NeedCatalog, _binding, _consent  # noqa: E402


class Provider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.lock = threading.Lock()

    def read(self, context):
        with self.lock:
            self.calls += 1
        return self.payload() if callable(self.payload) else dict(self.payload)


class LeaseProvider:
    provider_id = "lease.weather.v1"

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
        return {"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20.5}}}


def _runtime(root: str | Path, catalog: NeedCatalog, *, provider: Provider | None = None, source: str = "weather", max_receipts: int = 500, calendar_source=None) -> LivingSourceRuntime:
    return LivingSourceRuntime(
        WorldStateStore(root),
        current_need_resolver=catalog.resolve,
        providers={source: provider} if provider is not None else None,
        calendar_source=calendar_source,
        max_receipts=max_receipts,
        clock=lambda: FIXED_NOW,
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="veyra-living-source-adversarial-") as temp_dir:
        # Missing resolver must fail closed for real reads, while user_answer
        # remains an upper-layer pending interaction and never calls a provider.
        no_resolver_catalog = NeedCatalog()
        no_resolver_catalog.add("real", "weather")
        no_resolver_catalog.add("answer", "user_answer")
        no_resolver = LivingSourceRuntime(WorldStateStore(Path(temp_dir) / "no-resolver"), clock=lambda: FIXED_NOW)
        real_binding = _binding(no_resolver_catalog, "real", "weather", {"location": "Shanghai"})
        try:
            no_resolver.register_binding(real_binding)
        except SourceAdmissionError:
            pass
        else:
            raise AssertionError("real source registered without current need resolver")
        answer_binding = _binding(no_resolver_catalog, "answer", "user_answer", {})
        no_resolver.register_binding(answer_binding)
        assert no_resolver.request("answer", "user_answer", user_id="u-v1", session_id="s-v1", now=FIXED_NOW).status == "pending"

        # Current need generation/digest is authoritative; a format-valid but
        # stale binding is rejected before it can create a request.
        catalog = NeedCatalog()
        catalog.add("current", "weather")
        provider = Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}})
        runtime = _runtime(Path(temp_dir) / "current", catalog, provider=provider)
        stale = _binding(catalog, "current", "weather", {"location": "Shanghai"})
        catalog.rows["current"]["generation"] = 2
        catalog.refresh_digest("current")
        try:
            runtime.register_binding(stale)
        except SourceAdmissionError:
            pass
        else:
            raise AssertionError("stale InformationNeed binding was admitted")
        current_binding = _binding(catalog, "current", "weather", {"location": "Shanghai"})
        runtime.register_binding(current_binding)
        runtime.grant_consent(_consent("weather"))
        assert runtime.request("current", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW).status == "ok"

        # Two bindings for one need/source cannot silently select the old one.
        catalog2 = NeedCatalog()
        catalog2.add("multi", "weather")
        provider2 = Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}})
        runtime2 = _runtime(Path(temp_dir) / "multi", catalog2, provider=provider2)
        binding_new = _binding(catalog2, "multi", "weather", {"location": "Shanghai"})
        binding_old = SourceNeedBinding(binding_id="old-binding", need_id="multi", need_revision=1, need_digest=binding_new.need_digest, user_id="u-v1", workspace_id="server-derived", session_id="s-v1", situation_id=binding_new.situation_id, source="weather", parameters={"location": "Shanghai"}, issued_at=canonical_utc(FIXED_NOW - timedelta(days=1)), expires_at=canonical_utc(FIXED_NOW + timedelta(days=1)))
        runtime2.register_binding(binding_old)
        runtime2.register_binding(binding_new)
        runtime2.grant_consent(_consent("weather"))
        assert runtime2.admit("multi", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW).binding_id == binding_new.binding_id

        # Binding retention is bounded, but reclamation cannot cross either a
        # current Need generation or any retained request lineage.  Fill the
        # index with expired, unreferenced rows and admit a fresh binding;
        # only the safe rows may be collected.
        reclaim_catalog = NeedCatalog()
        reclaim_catalog.add("active", "weather")
        reclaim_catalog.add("retained", "weather")
        reclaim_catalog.add("current-expired", "weather")
        reclaim_catalog.add("noncurrent", "weather")
        reclaim_catalog.add("fresh", "weather")
        reclaim_catalog.add("fresh-two", "weather")
        reclaim_provider = Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}})
        reclaim_runtime = _runtime(Path(temp_dir) / "binding-reclaim", reclaim_catalog, provider=reclaim_provider)
        active_binding = replace(
            _binding(reclaim_catalog, "active", "weather", {"location": "Shanghai"}),
            binding_id="binding-active",
            expires_at=canonical_utc(FIXED_NOW + timedelta(days=10)),
        )
        retained_binding = _binding(reclaim_catalog, "retained", "weather", {"location": "Shanghai"})
        current_expired_binding = replace(
            _binding(reclaim_catalog, "current-expired", "weather", {"location": "Shanghai"}),
            binding_id="binding-current-expired",
            issued_at=canonical_utc(FIXED_NOW - timedelta(days=2)),
            expires_at=canonical_utc(FIXED_NOW - timedelta(days=1)),
        )
        noncurrent_binding = _binding(reclaim_catalog, "noncurrent", "weather", {"location": "Shanghai"})
        reclaim_catalog.rows["noncurrent"]["generation"] = 2
        reclaim_catalog.refresh_digest("noncurrent")
        reclaim_runtime.register_binding(active_binding)
        reclaim_runtime.register_binding(retained_binding)
        reclaim_runtime.register_binding(current_expired_binding)
        reclaim_runtime.grant_consent(_consent("weather"))
        retained_receipt = reclaim_runtime.request("retained", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert retained_receipt.status == "ok"
        reclaim_store = reclaim_runtime.state_store

        def fill_expired(state):
            for index in range(MAX_BINDINGS - 3):
                filler = SourceNeedBinding(
                    binding_id=f"expired-{index}",
                    need_id="noncurrent",
                    need_revision=noncurrent_binding.need_revision,
                    need_digest=noncurrent_binding.need_digest,
                    user_id="u-v1",
                    workspace_id="server-derived",
                    session_id="s-v1",
                    situation_id=noncurrent_binding.situation_id,
                    source="weather",
                    parameters={"location": "Shanghai"},
                    issued_at=canonical_utc(FIXED_NOW - timedelta(days=2)),
                    expires_at=canonical_utc(FIXED_NOW - timedelta(days=1)),
                )
                state["bindings"][filler.binding_id] = filler.to_dict()
            return state

        reclaim_store.mutate_json("living_source_state.json", fill_expired)
        fresh_binding = _binding(reclaim_catalog, "fresh", "weather", {"location": "Shanghai"})
        reclaim_runtime.register_binding(fresh_binding)
        state_after_reclaim = reclaim_store.read_json("living_source_state.json")
        assert fresh_binding.binding_id in state_after_reclaim["bindings"]
        assert active_binding.binding_id in state_after_reclaim["bindings"]
        assert retained_binding.binding_id in state_after_reclaim["bindings"]
        assert current_expired_binding.binding_id in state_after_reclaim["bindings"]
        assert not any(key.startswith("expired-") for key in state_after_reclaim["bindings"])

        reclaim_runtime._clock = lambda: FIXED_NOW + timedelta(days=2)

        def refill_expired(state):
            for index in range(MAX_BINDINGS - len(state["bindings"])):
                filler = SourceNeedBinding(
                    binding_id=f"expired-again-{index}",
                    need_id="noncurrent",
                    need_revision=noncurrent_binding.need_revision,
                    need_digest=noncurrent_binding.need_digest,
                    user_id="u-v1",
                    workspace_id="server-derived",
                    session_id="s-v1",
                    situation_id=noncurrent_binding.situation_id,
                    source="weather",
                    parameters={"location": "Shanghai"},
                    issued_at=canonical_utc(FIXED_NOW - timedelta(days=3)),
                    expires_at=canonical_utc(FIXED_NOW - timedelta(days=2)),
                )
                state["bindings"][filler.binding_id] = filler.to_dict()
            return state

        reclaim_store.mutate_json("living_source_state.json", refill_expired)
        fresh_two = _binding(reclaim_catalog, "fresh-two", "weather", {"location": "Shanghai"}, now=FIXED_NOW + timedelta(days=2))
        reclaim_runtime.register_binding(fresh_two)
        state_after_reference_check = reclaim_store.read_json("living_source_state.json")
        assert retained_binding.binding_id in state_after_reference_check["bindings"], "expired binding with retained request was reclaimed"
        assert current_expired_binding.binding_id in state_after_reference_check["bindings"], "expired current binding was reclaimed"
        assert fresh_two.binding_id in state_after_reference_check["bindings"]
        assert len(state_after_reference_check["bindings"]) <= MAX_BINDINGS

        # If every row is an expired but resolver-proven current binding, the
        # 200-row cap remains fail-closed and the attempted registration is
        # byte-pure.
        full_catalog = NeedCatalog()
        full_catalog.add("full-0", "weather")
        full_runtime = _runtime(Path(temp_dir) / "binding-full", full_catalog, provider=Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}}))
        first_full = replace(
            _binding(full_catalog, "full-0", "weather", {"location": "Shanghai"}),
            issued_at=canonical_utc(FIXED_NOW - timedelta(days=2)),
            expires_at=canonical_utc(FIXED_NOW - timedelta(days=1)),
        )
        full_runtime.register_binding(first_full)
        full_store = full_runtime.state_store
        full_rows = []
        for index in range(1, MAX_BINDINGS):
            need_id = f"full-{index}"
            full_catalog.add(need_id, "weather")
            full_rows.append(
                replace(
                    _binding(full_catalog, need_id, "weather", {"location": "Shanghai"}),
                    issued_at=canonical_utc(FIXED_NOW - timedelta(days=2)),
                    expires_at=canonical_utc(FIXED_NOW - timedelta(days=1)),
                )
            )

        def fill_current_expired(state):
            for row in full_rows:
                state["bindings"][row.binding_id] = row.to_dict()
            return state

        full_store.mutate_json("living_source_state.json", fill_current_expired)
        full_catalog.add("full-new", "weather")
        full_new = _binding(full_catalog, "full-new", "weather", {"location": "Shanghai"})
        full_before = full_store.path_for("living_source_state.json").read_bytes()
        try:
            full_runtime.register_binding(full_new)
        except SourceCapacityError:
            pass
        else:
            raise AssertionError("current expired bindings were evicted under a full cap")
        full_after = full_store.read_json("living_source_state.json")
        assert full_store.path_for("living_source_state.json").read_bytes() == full_before
        assert len(full_after["bindings"]) == MAX_BINDINGS and full_new.binding_id not in full_after["bindings"]

        # Resolver failure is not evidence that an old binding is stale.
        # Keep the cap full and verify that this path also remains byte-pure.
        full_catalog.add("full-new-unknown", "weather")
        full_unknown = _binding(full_catalog, "full-new-unknown", "weather", {"location": "Shanghai"})
        resolver = full_runtime.current_need_resolver

        def fail_for_existing(user_id, session_id, need_id):
            if str(need_id).startswith("full-") and str(need_id) not in {"full-new-unknown"}:
                raise RuntimeError("resolver unavailable for retention check")
            return resolver(user_id, session_id, need_id)

        full_runtime.current_need_resolver = fail_for_existing
        unknown_before = full_store.path_for("living_source_state.json").read_bytes()
        try:
            full_runtime.register_binding(full_unknown)
        except SourceCapacityError:
            pass
        else:
            raise AssertionError("resolver failure was treated as proof of binding staleness")
        assert full_store.path_for("living_source_state.json").read_bytes() == unknown_before

        # Revocation invalidates cached facts and regrant gets a new consent generation.
        catalog3 = NeedCatalog()
        catalog3.add("consent", "weather")
        provider3 = Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}})
        runtime3 = _runtime(Path(temp_dir) / "consent", catalog3, provider=provider3)
        runtime3.register_binding(_binding(catalog3, "consent", "weather", {"location": "Shanghai"}))
        runtime3.grant_consent(_consent("weather"))
        receipt = runtime3.request("consent", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        runtime3.revoke_consent("consent-weather", user_id="u-v1", session_id="s-v1")
        revoked = runtime3.get_receipt(receipt.receipt_id, user_id="u-v1", session_id="s-v1")
        assert revoked is not None and revoked.status == "revoked" and not revoked.payload and provider3.calls == 1
        assert runtime3.request("consent", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=1)).status == "revoked"
        try:
            runtime3.grant_consent(replace(_consent("weather"), user_id="other"))
        except SourceAdmissionError:
            pass
        else:
            raise AssertionError("revoked consent was rebound across exact owner scope")
        assert runtime3.grant_consent(_consent("weather")).generation == 2
        assert runtime3.request("consent", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=2)).status == "ok" and provider3.calls == 2

        # Invalid provider values become a bounded unknown, never a stuck run;
        # timeout/unknown responses receive a bounded retry generation.
        catalog4 = NeedCatalog()
        catalog4.add("bad", "weather")
        malformed = Provider({"status": "ok", "value": float("nan")})
        runtime4 = _runtime(Path(temp_dir) / "malformed", catalog4, provider=malformed)
        runtime4.register_binding(_binding(catalog4, "bad", "weather", {"location": "Shanghai"}))
        runtime4.grant_consent(_consent("weather"))
        first_bad = runtime4.request("bad", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert first_bad.status == "unknown" and runtime4.get_request(first_bad.request_id, user_id="u-v1", session_id="s-v1").status == "unknown"
        assert runtime4.request("bad", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=1)).status == "unknown" and malformed.calls == 2

        noisy_catalog = NeedCatalog()
        noisy_catalog.add("noisy", "weather")
        noisy = Provider({"status": "unknown", "ttl_seconds": 120, "details": {"location": "Shanghai", "current": {"temperature_2m": 20.5}}})
        noisy_runtime = _runtime(Path(temp_dir) / "noisy", noisy_catalog, provider=noisy)
        noisy_runtime.register_binding(_binding(noisy_catalog, "noisy", "weather", {"location": "Shanghai"}))
        noisy_runtime.grant_consent(_consent("weather"))
        noisy_receipt = noisy_runtime.request("noisy", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert noisy_receipt.status == "unknown" and not noisy_receipt.payload and noisy_receipt.ttl_seconds == 0

        lease_catalog = NeedCatalog()
        lease_catalog.add("lease", "weather")
        lease_provider = LeaseProvider()
        lease_runtime = _runtime(Path(temp_dir) / "lease", lease_catalog, provider=lease_provider)
        lease_runtime._capabilities["weather"] = replace(lease_runtime._capabilities["weather"], max_timeout_seconds=0.05)
        lease_runtime.register_binding(_binding(lease_catalog, "lease", "weather", {"location": "Shanghai"}))
        lease_runtime.grant_consent(_consent("weather"))
        first_lease: list = []
        lease_thread = threading.Thread(target=lambda: first_lease.append(lease_runtime.request("lease", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)))
        lease_thread.start()
        assert lease_provider.started.wait(1)
        lease_thread.join(1)
        assert first_lease and first_lease[0].status == "timeout" and lease_provider.calls == 1
        immediate_retry = lease_runtime.request("lease", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=1))
        assert immediate_retry.status == "timeout" and lease_provider.calls == 1
        lease_status = lease_runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        assert lease_status["execution"]["timed_out_lease_count"] == 1
        assert lease_status["status"] == "degraded"
        lease_provider.release.set()
        for _ in range(100):
            if not lease_runtime._lease_active("binding-lease:weather"):
                break
            time.sleep(0.01)
        second_lease = lease_runtime.request("lease", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=2))
        assert second_lease.status == "ok" and lease_provider.calls == 2

        # Prose-only and reserved/debug provider data cannot become typed facts.
        prose_catalog = NeedCatalog()
        prose_catalog.add("prose", "weather")
        prose = Provider({"status": "ok", "summary": "only prose", "path": "/private", "command": "rm -rf", "token": "secret"})
        prose_runtime = _runtime(Path(temp_dir) / "prose", prose_catalog, provider=prose)
        prose_runtime.register_binding(_binding(prose_catalog, "prose", "weather", {"location": "Shanghai"}))
        prose_runtime.grant_consent(_consent("weather"))
        assert prose_runtime.request("prose", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW).status == "unknown"

        # Calendar scope is not accepted, ICS text is bounded, and invalid
        # event intervals are not projected as facts.
        calendar_catalog = NeedCatalog()
        calendar_catalog.add("cal", "calendar")
        try:
            _binding(calendar_catalog, "cal", "calendar", {"window_start": canonical_utc(FIXED_NOW), "window_end": canonical_utc(FIXED_NOW + timedelta(days=1)), "calendar_scope": "private"})
        except LivingSourceContractError:
            pass
        else:
            raise AssertionError("unimplemented calendar_scope was accepted")
        context = type("Context", (), {"parameters": {"window_start": canonical_utc(FIXED_NOW), "window_end": canonical_utc(FIXED_NOW + timedelta(days=1))}})()
        assert IcsCalendarProvider(ics_text="x" * 1_000_001).read(context)["status"] == "unknown"
        bad_ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:bad\nSUMMARY:bad\nDTSTART:20260817T100000Z\nDTEND:20260817T090000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        assert IcsCalendarProvider(ics_text=bad_ics).read(context)["status"] == "empty"

        # Receipt projection accepts only a typed receipt that is still present
        # in the authoritative source state and bound to the current Need.
        semantic_store = WorldStateStore(Path(temp_dir) / "semantic-receipt")
        semantic = LivingContextRuntime(semantic_store, clock=lambda: FIXED_NOW)
        semantic_event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id="u-v1", session_id="s-v1"),
            payload={"text": "weather situation"},
            event_id="semantic-create",
        )
        semantic_candidate = LivingContextCandidate(
            schema_version="veyra.living_context_candidate.v1",
            disposition="create",
            create_subject="weather situation",
            category="travel",
            label="weather situation",
            title="weather situation",
            summary="weather situation",
            goal="prepare",
            lifecycle="active",
            known=[],
            unknown=["weather unknown"],
            assumptions=[],
            timeline=[],
            material_change="",
            needs=[
                CandidateNeed(
                    blocked_judgment="weather unknown",
                    evidence_kind="weather",
                    why_now="current weather changes the next judgment",
                    urgency=0.5,
                    allowed_source_classes=["weather"],
                    fallback_reaction="read",
                    question="What are the current conditions?",
                )
            ],
            requested_reaction="read",
            source="model",
        )
        semantic_result = semantic.process_user_turn(
            semantic_event,
            SimpleNamespace(living_context_candidate=semantic_candidate),
        )
        semantic_need = semantic_result["information_needs"][0]
        semantic_projection = semantic.needs.authoritative_projection(
            str(semantic_need["need_id"]), owner_id="u-v1", session_id="s-v1"
        )
        assert semantic_projection is not None
        semantic_provider = Provider(
            {"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}}
        )
        semantic_source = LivingSourceRuntime(
            semantic_store,
            current_need_resolver=lambda owner, session, selected: semantic.needs.authoritative_projection(
                selected, owner_id=owner, session_id=session
            ),
            providers={"weather": semantic_provider},
            clock=lambda: FIXED_NOW,
        )
        semantic_binding = SourceNeedBinding(
            binding_id="semantic-weather-binding",
            need_id=str(semantic_need["need_id"]),
            need_revision=int(semantic_projection["generation"]),
            need_digest=str(semantic_projection["record_digest"]),
            user_id="u-v1",
            workspace_id="server-derived",
            session_id="s-v1",
            situation_id=str(semantic_need["situation_id"]),
            source="weather",
            parameters={"location": "Shanghai"},
            issued_at=canonical_utc(FIXED_NOW),
            expires_at=canonical_utc(FIXED_NOW + timedelta(days=1)),
        )
        semantic_source.register_binding(semantic_binding)
        semantic_source.grant_consent(_consent("weather"))
        semantic_receipt = semantic_source.request(
            str(semantic_need["need_id"]),
            "weather",
            user_id="u-v1",
            session_id="s-v1",
            now=FIXED_NOW,
        )
        semantic_situation_path = semantic_store.path_for("situation_state.json")
        semantic_need_path = semantic_store.path_for("information_need_state.json")
        before_situation = semantic_situation_path.read_bytes()
        before_need = semantic_need_path.read_bytes()
        observation_event = VeyraEvent(
            type=EventType.OBSERVATION,
            source=EventSource(channel="api", user_id="u-v1", session_id="s-v1"),
            payload={"source_receipt_id": semantic_receipt.receipt_id},
            event_id="forged-receipt-dict",
        )
        try:
            semantic.apply_source_receipt(
                observation_event,
                {**semantic_receipt.to_dict(), "source": "calendar"},
                expected_generation=int(semantic_projection["generation"]),
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("arbitrary receipt dict was accepted")
        assert semantic_situation_path.read_bytes() == before_situation
        assert semantic_need_path.read_bytes() == before_need
        forged_receipt = replace(
            semantic_receipt,
            source="calendar",
            payload={"facts": {"events": []}, "provider": "forged", "source": "calendar"},
            payload_digest="",
        )
        try:
            semantic.apply_source_receipt(
                replace(observation_event, event_id="forged-receipt-object"),
                forged_receipt,
                expected_generation=int(semantic_projection["generation"]),
            )
        except StateRevisionConflictError:
            pass
        else:
            raise AssertionError("receipt with mismatched authoritative source binding was accepted")
        assert semantic_situation_path.read_bytes() == before_situation
        assert semantic_need_path.read_bytes() == before_need

        # Corrupt rows, wrong schema and authority are byte-pure fail-closed.
        corrupt_catalog = NeedCatalog()
        corrupt_catalog.add("corrupt", "weather")
        corrupt_runtime = _runtime(Path(temp_dir) / "corrupt", corrupt_catalog, provider=Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}}))
        corrupt_runtime.register_binding(_binding(corrupt_catalog, "corrupt", "weather", {"location": "Shanghai"}))
        corrupt_runtime.grant_consent(_consent("weather"))
        corrupt_runtime.request("corrupt", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        store = corrupt_runtime.state_store
        store.mutate_json("living_source_state.json", lambda state: (state["bindings"]["binding-corrupt"].__setitem__("schema_version", "wrong.v0") or state))
        before = store.path_for("living_source_state.json").read_bytes()
        assert corrupt_runtime.state_snapshot()["state_corrupt"] is True
        try:
            corrupt_runtime.grant_consent(_consent("weather"))
        except SourceStateCorruptError:
            pass
        else:
            raise AssertionError("wrong row schema was repaired")
        assert store.path_for("living_source_state.json").read_bytes() == before
        return {"status": "passed", "checks": ["need_fence", "exact_scope", "consent_revoke", "typed_projection", "retry", "single_flight_lease", "lease_cap_status", "receipt_binding", "calendar_bounds", "state_integrity"]}


def main() -> int:
    print("LIVING_SOURCE_ADVERSARIAL_SMOKE_OK", run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
