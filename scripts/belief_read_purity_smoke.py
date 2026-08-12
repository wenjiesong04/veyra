#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.world_state import WorldStateStore
from routers.debug_audit import build_debug_audit_router


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-belief-read-purity-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        observed_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        claim = make_claim(
            key="read-purity-expired",
            claim="expired claim must be evaluated without a GET write",
            source="read_purity_smoke",
            confidence=0.9,
            ttl_seconds=1,
            observed_at=observed_at,
        )
        store.write_json(
            "belief_state.json",
            {
                "claims": [claim],
                "summary": belief.summary_from_claims([claim]),
            },
        )

        app = FastAPI()
        app.include_router(
            build_debug_audit_router(
                {"awareness_loop": SimpleNamespace(belief=belief)}
            )
        )
        client = TestClient(app)
        belief_path = store.path_for("belief_state.json")

        before_bytes = belief_path.read_bytes()
        before_stat = belief_path.stat()
        before_revision = store.read_json("belief_state.json").get("_state_revision")

        status_response = client.get("/belief/status")
        expect(status_response.status_code == 200, "GET /belief/status succeeds", status_response.text)
        status = status_response.json()
        expect(status["summary"]["expired"] == 1, "status GET evaluates expired claim", status)

        stale_response = client.get("/belief/stale")
        expect(stale_response.status_code == 200, "GET /belief/stale succeeds", stale_response.text)
        stale = stale_response.json()
        expect(stale["items"][0]["status"] == "expired", "stale GET evaluates expired claim", stale)

        relevant = belief.relevant_claims(["read-purity-expired"])
        expect(relevant[0]["status"] == "expired", "relevant claims use evaluated snapshot", relevant)

        after_get_bytes = belief_path.read_bytes()
        after_get_stat = belief_path.stat()
        after_get_revision = store.read_json("belief_state.json").get("_state_revision")
        expect(after_get_bytes == before_bytes, "belief GET paths preserve exact file bytes")
        expect(after_get_revision == before_revision, "belief GET paths preserve revision")
        expect(
            after_get_stat.st_mtime_ns == before_stat.st_mtime_ns,
            "belief GET paths preserve mtime",
        )

        refresh_response = client.post("/belief/refresh", json={})
        expect(refresh_response.status_code == 200, "POST /belief/refresh succeeds", refresh_response.text)
        refreshed = refresh_response.json()
        expect(refreshed["summary"] == status["summary"], "GET and refresh share status semantics", refreshed)
        expect(
            refreshed["claims"][0]["status"] == stale["items"][0]["status"],
            "GET and refresh share claim status semantics",
            refreshed,
        )

        after_post_bytes = belief_path.read_bytes()
        after_post_revision = store.read_json("belief_state.json").get("_state_revision")
        expect(after_post_bytes != before_bytes, "explicit refresh may persist evaluated state")
        expect(
            int(after_post_revision or 0) == int(before_revision or 0) + 1,
            "explicit refresh advances revision",
            {"before": before_revision, "after": after_post_revision},
        )

    with tempfile.TemporaryDirectory(prefix="veyra-belief-scoped-limit-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        now = datetime.now(timezone.utc)
        fresh = make_claim(
            key="scoped-fresh",
            claim="fresh exact-owner claim",
            source="read_purity_smoke",
            confidence=0.9,
            ttl_seconds=3600,
            observed_at=now.isoformat(),
        )
        stale_claim = make_claim(
            key="scoped-stale",
            claim="stale exact-owner claim",
            source="read_purity_smoke",
            confidence=0.9,
            ttl_seconds=1,
            observed_at=(now - timedelta(days=2)).isoformat(),
        )
        for item in (fresh, stale_claim):
            item.update(
                {
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": "scoped-owner",
                    "session_id": "scoped-session",
                }
            )
        persisted = belief.upsert_claims([fresh, stale_claim])
        expect(
            isinstance(persisted, list)
            and len(persisted) == 2
            and all(item.get("persisted") is True for item in persisted),
            "scoped claim fixture persists",
            persisted,
        )

        def append_legacy_ownerless_tenant(
            state: dict[str, object],
        ) -> dict[str, object]:
            claims = state.get("claims")
            if not isinstance(claims, list):
                raise AssertionError("belief claims fixture is unavailable")
            legacy = make_claim(
                key="legacy-ownerless-tenant",
                claim="must be quarantined without hiding valid owner claims",
                source="web_probe",
                confidence=0.5,
                ttl_seconds=1,
                observed_at=(now - timedelta(days=2)).isoformat(),
            )
            legacy.update({"scope_kind": "tenant", "tenant_derived": True})
            claims.append(legacy)
            return state

        store.mutate_json("belief_state.json", append_legacy_ownerless_tenant)
        app = FastAPI()
        app.include_router(
            build_debug_audit_router(
                {"awareness_loop": SimpleNamespace(belief=belief)}
            )
        )
        client = TestClient(app)
        scoped_params = {
            "user_id": "scoped-owner",
            "session_id": "scoped-session",
        }
        bounded = client.get(
            "/belief/status",
            params={**scoped_params, "limit": 5},
        ).json()
        zero = client.get(
            "/belief/status",
            params={**scoped_params, "limit": 0},
        ).json()
        zero_stale = client.get(
            "/belief/stale",
            params={**scoped_params, "limit": 0},
        ).json()
        aggregate_zero = client.get(
            "/belief/status",
            params={"limit": 0},
        ).json()
        aggregate_negative = client.get(
            "/belief/stale",
            params={"limit": -1},
        ).json()
        expect(
            bounded.get("status") == "success"
            and len(bounded.get("oldest") or []) == 2
            and len(bounded.get("newest") or []) == 2
            and len(bounded.get("refreshable") or []) == 1
            and bounded["refreshable"][0].get("next_action")
            == "refresh_probe"
            and zero.get("oldest") == []
            and zero.get("newest") == []
            and zero.get("refreshable") == []
            and zero_stale.get("items") == []
            and aggregate_zero.get("status") == "degraded"
            and aggregate_zero.get("reason") == "belief_claims_quarantined"
            and aggregate_zero.get("quarantined_claim_count") == 1
            and aggregate_zero.get("oldest_count") == 0
            and aggregate_zero.get("newest_count") == 0
            and aggregate_zero.get("refreshable_count") == 0
            and aggregate_negative.get("items") == [],
            "scoped Belief limits are exact and refreshable excludes fresh claims",
            {
                "bounded": bounded,
                "zero": zero,
                "zero_stale": zero_stale,
                "aggregate_zero": aggregate_zero,
                "aggregate_negative": aggregate_negative,
            },
        )

    with tempfile.TemporaryDirectory(prefix="veyra-belief-duplicate-integrity-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        duplicate = make_claim(
            key="duplicate-exact-identity",
            claim="authoritative-looking value",
            source="read_purity_smoke",
            confidence=0.9,
            ttl_seconds=3600,
            observed_at=datetime.now(timezone.utc).isoformat(),
            evidence={"status": "up"},
        )
        duplicate.update(
            {
                "scope_kind": "tenant",
                "tenant_derived": True,
                "user_id": "duplicate-owner",
                "session_id": "duplicate-session",
            }
        )
        persisted = belief.upsert_claim(duplicate)
        expect(persisted.get("persisted") is True, "duplicate fixture persists", persisted)

        def append_malformed_duplicate(state: dict[str, object]) -> dict[str, object]:
            claims = state.get("claims")
            if not isinstance(claims, list) or not claims:
                raise AssertionError("duplicate claims fixture is unavailable")
            claims.append(
                {
                    **claims[0],
                    "claim": "conflicting malformed value",
                    "evidence": {"status": "down"},
                    "claim_revision": "malformed",
                }
            )
            return state

        store.mutate_json("belief_state.json", append_malformed_duplicate)
        belief_path = store.path_for("belief_state.json")
        before = belief_path.read_bytes()
        snapshot = belief._snapshot()
        after = belief_path.read_bytes()
        expect(
            snapshot.get("status") == "degraded"
            and snapshot.get("reason") == "belief_state_integrity_invalid"
            and snapshot.get("_state_corrupt") is True
            and snapshot.get("claims") == [],
            "duplicate exact identity fails closed before per-row quarantine",
            snapshot,
        )
        expect(after == before, "duplicate integrity rejection is byte-pure")

    print("belief read purity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
