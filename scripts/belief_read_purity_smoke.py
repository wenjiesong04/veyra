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

    print("belief read purity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
