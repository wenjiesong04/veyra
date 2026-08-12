#!/usr/bin/env python3
"""Adversarial StateRefresh CAS, TTL, fairness, receipt and privacy checks."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.world_state import WorldStateStore
from routers.debug_audit import _public_state, build_debug_audit_router
from runtime.state_refresh import StateRefresh


def expect(condition: bool, label: str, details: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def port_claim(
    *,
    key: str,
    owner: tuple[str, str] | None = None,
    status: str = "stale",
    observed_at: str | None = None,
    value: str = "old",
) -> dict[str, Any]:
    claim = make_claim(
        key=key,
        claim=f"port observation {value}",
        source="port_probe",
        confidence=0.9,
        ttl_seconds=1,
        observed_at=observed_at or (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        status=status,
        next_action="refresh_probe" if status in {"stale", "expired", "conflict"} else None,
        evidence={"target": "127.0.0.1:9999", "status": value},
    )
    if owner is None:
        claim["scope_kind"] = "operator_global"
    else:
        claim.update(
            {
                "scope_kind": "tenant",
                "tenant_derived": True,
                "user_id": owner[0],
                "session_id": owner[1],
            }
        )
    return claim


class SlowProbe:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, target: str) -> dict[str, Any]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("slow probe was not released")
        return {
            "probe": "port_probe",
            "source": "port_probe",
            "target": target,
            "status": "success",
            "summary": "slow probe completed",
            "confidence": 0.9,
            "ttl_seconds": 30,
            "details": {},
        }


class ImmediateProbe:
    def run(self, target: str) -> dict[str, Any]:
        return {
            "probe": "port_probe",
            "source": "port_probe",
            "target": target,
            "status": "success",
            "summary": "probe completed",
            "confidence": 0.9,
            "ttl_seconds": 30,
            "details": {},
        }


class CountingProbe(ImmediateProbe):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, target: str) -> dict[str, Any]:
        self.calls += 1
        return super().run(target)


def integrity_bytes(store: WorldStateStore) -> dict[str, bytes]:
    return {
        name: store.path_for(name).read_bytes()
        for name in (
            "belief_state.json",
            "local_world.json",
            "state_refresh_state.json",
        )
    }


class LegacyAcceptedPerception:
    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"status": "accepted"}


class MalformedPerception:
    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "accepted",
            "belief_persistence": {
                "status": "accepted",
                "accepted_count": 1,
                "conflicted_count": 0,
                "rejected_count": 0,
                "results": [],
            },
        }


class RaisingPerception:
    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("private refresh_cas identity/session/digest")


def test_cas_and_ttl() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-cas-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        key = "port:127.0.0.1:9999:status"
        initial = belief.upsert_claim(port_claim(key=key, value="old"))
        expect(initial.get("persisted") is True, "initial claim persists", initial)
        store.mutate_json(
            "belief_state.json",
            lambda state: {
                **state,
                "claims": [
                    {
                        **state["claims"][0],
                        "status": "stale",
                        "next_action": "refresh_probe",
                    }
                ],
            },
        )
        refresh = StateRefresh(store, model_assist_enabled=False)
        slow = SlowProbe()
        refresh.probes = {"port_probe": slow}
        result_holder: list[dict[str, Any]] = []
        worker = threading.Thread(
            target=lambda: result_holder.append(refresh.refresh_stale(limit=1)),
            daemon=True,
        )
        worker.start()
        expect(slow.started.wait(timeout=5), "slow probe reaches in-flight barrier")

        newer = port_claim(key=key, value="newer", observed_at=datetime.now(timezone.utc).isoformat())
        newer_result = belief.upsert_claim(newer)
        expect(newer_result.get("persisted") is True, "newer observation persists", newer_result)
        slow.release.set()
        worker.join(timeout=5)
        expect(result_holder, "refresh worker completes")
        result = result_holder[0]
        expect(result["status"] == "degraded", "CAS loss is degraded", result)
        expect(not result["refreshed"], "CAS loss is not a refresh success", result)
        expect(
            any("cas" in str(item.get("reason")) for item in result["failed"]),
            "CAS rejection is an honest failure receipt",
            result,
        )
        current = store.read_json("belief_state.json")["claims"][0]
        expect(
            any(item.get("observed_at") == newer.get("observed_at") for item in current.get("conflict_observations") or []),
            "slow probe cannot erase the newer observation evidence",
            current,
        )

        # A durably fresh claim whose TTL has elapsed is selected on the next
        # refresh tick; no GET or lifecycle write is needed for eligibility.
        ttl_key = "port:127.0.0.1:9999:ttl"
        ttl_claim = port_claim(
            key=ttl_key,
            status="fresh",
            observed_at=(datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(),
            value="ttl-old",
        )
        store.write_json("belief_state.json", {"claims": [ttl_claim]})
        ttl_refresh = StateRefresh(store, model_assist_enabled=False)
        ttl_refresh.probes = {"port_probe": ImmediateProbe()}
        ttl_refresh.perception = LegacyAcceptedPerception()
        ttl_result = ttl_refresh.refresh_stale(limit=1)
        expect(ttl_result["status"] == "success", "elapsed TTL is refresh eligible", ttl_result)
        expect(ttl_result["selected_count"] == 1, "elapsed TTL is selected before refresh", ttl_result)


def test_malformed_claim_revision_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-malformed-revision-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        key = "port:127.0.0.1:9999:malformed-revision"
        persisted = belief.upsert_claim(port_claim(key=key, value="old"))
        expect(persisted.get("persisted") is True, "malformed-revision fixture persists", persisted)
        store.mutate_json(
            "belief_state.json",
            lambda state: {
                **state,
                "claims": [
                    {
                        **state["claims"][0],
                        "status": "stale",
                        "next_action": "refresh_probe",
                        "claim_revision": "not-an-integer",
                    }
                ],
            },
        )
        refresh = StateRefresh(store, model_assist_enabled=False)
        probe = CountingProbe()
        refresh.probes = {"port_probe": probe}
        before = integrity_bytes(store)
        result = refresh.refresh_stale(limit=1)
        after = integrity_bytes(store)
        durable = store.read_json("belief_state.json")["claims"][0]
        expect(result.get("status") == "degraded", "malformed nonnull claim revision is degraded", result)
        expect(
            result.get("reason") == "belief_state_integrity_invalid"
            and any(item.get("reason") == "belief_state_integrity_invalid" for item in result.get("failed") or []),
            "malformed nonnull claim revision fails at the integrity preflight",
            result,
        )
        expect(probe.calls == 0, "malformed state cannot reach a probe", probe.calls)
        expect(after == before, "malformed state leaves belief/local/scheduler byte-pure")
        expect(durable.get("claim_revision") == "not-an-integer", "malformed claim revision is not migrated", durable)


def test_duplicate_and_graph_corruption_fail_before_scheduler() -> None:
    for case in ("duplicate_identity", "corrupt_graph"):
        with tempfile.TemporaryDirectory(prefix=f"veyra-refresh-{case}-") as tmp:
            store = WorldStateStore(Path(tmp) / "state")
            belief = BeliefCore(store)
            persisted = belief.upsert_claim(
                port_claim(key=f"port:{case}", value="old")
            )
            expect(persisted.get("persisted") is True, f"{case} fixture persists", persisted)

            def corrupt(state: dict[str, Any]) -> dict[str, Any]:
                if case == "duplicate_identity":
                    claims = state.get("claims")
                    if not isinstance(claims, list) or not claims:
                        raise AssertionError("duplicate fixture claims unavailable")
                    claims.append(
                        {
                            **claims[0],
                            "claim": "conflicting duplicate",
                            "evidence": {
                                "target": "127.0.0.1:9999",
                                "status": "newer-conflict",
                            },
                            "claim_revision": 2,
                        }
                    )
                else:
                    graph = state.get("evidence_graph")
                    nodes = graph.get("nodes") if isinstance(graph, dict) else None
                    if not isinstance(nodes, list) or not nodes:
                        raise AssertionError("graph fixture node unavailable")
                    nodes[0]["partition_digest"] = "0" * 64
                return state

            store.mutate_json("belief_state.json", corrupt)
            before = integrity_bytes(store)
            refresh = StateRefresh(store, model_assist_enabled=False)
            probe = CountingProbe()
            refresh.probes = {"port_probe": probe}
            result = refresh.refresh_stale(limit=20)
            after = integrity_bytes(store)
            expect(
                result.get("status") == "degraded"
                and result.get("reason") == "belief_state_integrity_invalid"
                and result.get("selected_count") == 0,
                f"{case} fails the pre-probe integrity boundary",
                result,
            )
            expect(probe.calls == 0, f"{case} cannot reach a probe", probe.calls)
            expect(after == before, f"{case} leaves belief/local/scheduler byte-pure")


def test_ownerless_legacy_quarantine_is_read_only() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-ownerless-quarantine-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        persisted = belief.upsert_claim(
            port_claim(key="port:quarantine-valid", value="old")
        )
        expect(persisted.get("persisted") is True, "quarantine valid fixture persists", persisted)

        def append_ownerless_legacy(state: dict[str, Any]) -> dict[str, Any]:
            claims = state.get("claims")
            if not isinstance(claims, list):
                raise AssertionError("quarantine fixture claims unavailable")
            legacy = make_claim(
                key="legacy-ownerless-quarantine",
                claim="historical ownerless tenant row",
                source="web_probe",
                confidence=0.5,
                ttl_seconds=1,
                observed_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            )
            legacy.update({"scope_kind": "tenant", "tenant_derived": True})
            claims.append(legacy)
            return state

        store.mutate_json("belief_state.json", append_ownerless_legacy)
        legacy_before = next(
            item
            for item in store.read_json("belief_state.json")["claims"]
            if item.get("key") == "legacy-ownerless-quarantine"
        )
        refresh = StateRefresh(store, model_assist_enabled=False)
        probe = CountingProbe()
        refresh.probes = {"port_probe": probe}
        result = refresh.refresh_stale(limit=20)
        legacy_after = next(
            item
            for item in store.read_json("belief_state.json")["claims"]
            if item.get("key") == "legacy-ownerless-quarantine"
        )
        expect(
            result.get("status") == "degraded"
            and result.get("refreshed_count") == 1
            and result.get("malformed")
            == [{"reason": "belief_claims_quarantined", "count": 1}],
            "ownerless legacy debt stays explicit while valid claims refresh",
            result,
        )
        expect(probe.calls == 1, "ownerless legacy quarantine does not hide valid work", probe.calls)
        expect(legacy_after == legacy_before, "ownerless legacy row remains read-only")


def test_persistence_exception_receipt_strips_private_cas() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-private-receipt-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        persisted = belief.upsert_claim(port_claim(key="port:private-receipt", value="old"))
        expect(persisted.get("persisted") is True, "private-receipt fixture persists", persisted)
        store.mutate_json(
            "belief_state.json",
            lambda state: {
                **state,
                "claims": [
                    {
                        **state["claims"][0],
                        "status": "stale",
                        "next_action": "refresh_probe",
                    }
                ],
            },
        )
        refresh = StateRefresh(store, model_assist_enabled=False)
        refresh.probes = {"port_probe": ImmediateProbe()}
        refresh.perception = RaisingPerception()
        result = refresh.refresh_stale(limit=1)
        encoded = str(result)
        expect(result.get("status") == "degraded", "persistence exception is degraded", result)
        expect(
            "refresh_cas" not in encoded
            and "_refresh_cas" not in encoded
            and "private refresh_cas" not in encoded
            and result.get("failed", [{}])[0].get("detail") == "RuntimeError",
            "persistence exception receipt strips private CAS recursively",
            result,
        )


def test_fairness_and_receipts() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-fair-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        refresh = StateRefresh(store, model_assist_enabled=False)
        refresh.probes = {"port_probe": ImmediateProbe()}
        refresh.perception = LegacyAcceptedPerception()
        claims = [
            port_claim(key=f"port:a:{index}", owner=("a", "session"), value=f"a-{index}")
            for index in range(4)
        ] + [port_claim(key="port:b:0", owner=("b", "session"), value="b-0")]
        store.write_json("belief_state.json", {"claims": claims})
        owners: list[str] = []
        for index in range(4):
            result = refresh.refresh_stale(limit=1)
            expect(result["status"] == "success", "fairness tick succeeds", result)
            owners.append(result["refreshed"][0]["probe_result"].get("user_id") or "ownerless")
            # Inject a new high-priority row for the noisy owner between ticks.
            current = store.read_json("belief_state.json")
            current_claims = current.get("claims") if isinstance(current.get("claims"), list) else []
            current_claims.append(port_claim(key=f"port:a:new-{index}", owner=("a", "session"), value=f"new-{index}"))
            store.write_json("belief_state.json", {"claims": current_claims})
        expect("b" in owners, "noisy owner cannot starve another owner", owners)
        scheduler = store.read_json("state_refresh_state.json")
        expect(len(scheduler.get("owner_offsets") or {}) <= 256, "owner scheduler state is bounded", scheduler)

        malformed_claim = port_claim(key="port:malformed", value="malformed")
        store.write_json("belief_state.json", {"claims": [malformed_claim]})
        refresh.perception = MalformedPerception()
        malformed_result = refresh.refresh_stale(limit=1)
        expect(malformed_result["status"] == "degraded", "malformed persistence is degraded", malformed_result)
        expect(not malformed_result["refreshed"], "malformed persistence cannot claim success", malformed_result)

        store.write_json(
            "belief_state.json",
            {
                "claims": [
                    {
                        "key": "event:unsupported",
                        "claim": "unsupported source",
                        "source": "event",
                        "status": "stale",
                        "next_action": "refresh_probe",
                    }
                ]
            },
        )
        refresh.perception = LegacyAcceptedPerception()
        skipped = refresh.refresh_stale(limit=1)
        expect(skipped["status"] == "skipped", "unsupported-only batch is skipped", skipped)

        store.write_json("belief_state.json", {"claims": []})
        idle = refresh.refresh_stale(limit=1)
        expect(idle["status"] == "idle", "empty batch is idle", idle)


def test_state_privacy() -> None:
    raw = {
        "user_world": {"current_project": "PRIVATE_PROJECT", "profiles": {"alice": {"secret": "PRIVATE_PROFILE"}}},
        "user_goals": {"goals": [{"goal_id": "PRIVATE_GOAL_ID", "title": "PRIVATE_GOAL"}]},
        "user_commitments": {"commitments": [{"commitment_id": "PRIVATE_COMMITMENT_ID", "title": "PRIVATE_COMMITMENT"}]},
        "proactive_intents": {"intents": [{"intent_id": "PRIVATE_INTENT_ID", "text": "PRIVATE_INTENT"}]},
        "proactive_authorizations": {"authorizations": [{"authorization_id": "PRIVATE_AUTH_ID", "token": "PRIVATE_TOKEN"}]},
        "external_world": {"watchlist": [{"target": "PRIVATE_TARGET"}], "summaries": [{"summary": "PRIVATE_SUMMARY"}]},
        "local_world": {"current_project": "PRIVATE_LOCAL_PROJECT", "probes": {"private": {"target": "PRIVATE_PROBE_TARGET"}}},
        "belief_state": {
            "claims": [
                {
                    "user_id": "alice",
                    "session_id": "alice-secret",
                    "history": [{"claim": "private"}],
                }
            ],
            "evidence_graph": {"nodes": [{"claim": "private graph"}], "edges": []},
            "summary": {"fresh": 1, "total": 1},
        },
        "state_refresh_state": {
            "last_owner": "alice|alice-secret",
            "owner_offsets": {"alice|alice-secret": 0},
            "supported_count": 1,
        },
    }
    public = _public_state(raw)
    belief_public = public.get("belief_state") or {}
    encoded = str(public)
    expect("private graph" not in encoded and "alice-secret" not in encoded, "public projection omits owner graph/history", public)
    expect("claims" not in belief_public and "evidence_graph" not in belief_public, "public Belief projection is aggregate-only", belief_public)
    expect("alice|alice-secret" not in str(public.get("state_refresh_state")), "public scheduler omits owner keys", public)
    expect(
        all(marker not in encoded for marker in (
            "PRIVATE_PROJECT",
            "PRIVATE_PROFILE",
            "PRIVATE_GOAL_ID",
            "PRIVATE_COMMITMENT_ID",
            "PRIVATE_INTENT_ID",
            "PRIVATE_AUTH_ID",
            "PRIVATE_TOKEN",
            "PRIVATE_TARGET",
            "PRIVATE_SUMMARY",
            "PRIVATE_PROBE_TARGET",
        )),
        "public projection omits raw commitment, intent, authorization, target and project values",
        public,
    )
    expect(
        "current_project" not in (public.get("local_world") or {})
        and (public.get("user_commitments") or {}).get("commitments") == []
        and (public.get("external_world") or {}).get("watchlist") == [],
        "public state keeps compatibility keys as empty aggregate arrays",
        public,
    )

    with tempfile.TemporaryDirectory(prefix="veyra-refresh-route-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json("belief_state.json", raw["belief_state"])
        app = FastAPI()
        app.include_router(
            build_debug_audit_router(
                {
                    "state_store": store,
                    "agency_core": SimpleNamespace(state=lambda: {}),
                }
            )
        )
        payload = TestClient(app).get("/state").json()
        expect("evidence_graph" not in str(payload), "GET /state does not expose EvidenceGraph", payload)


def main() -> int:
    test_cas_and_ttl()
    test_malformed_claim_revision_fails_closed()
    test_duplicate_and_graph_corruption_fail_before_scheduler()
    test_ownerless_legacy_quarantine_is_read_only()
    test_persistence_exception_receipt_strips_private_cas()
    test_fairness_and_receipts()
    test_state_privacy()
    print("state refresh adversarial smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
