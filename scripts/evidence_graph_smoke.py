#!/usr/bin/env python3
"""Exercise the minimal durable EvidenceGraph contract."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.world_state import WorldStateStore


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def claim(
    *,
    value: str,
    observed_at: str | None,
    valid_from: str | None,
    valid_to: str | None,
    key: str = "service:api:status",
) -> dict[str, object]:
    item = make_claim(
        key=key,
        claim=f"api is {value}",
        source="component_health",
        confidence=0.9,
        ttl_seconds=300,
        evidence={
            "status": value,
            "producer": "server_health",
            "observation_id": f"obs-{value}-{observed_at or 'unknown'}",
            **({"valid_from": valid_from} if valid_from else {}),
            **({"valid_to": valid_to} if valid_to else {}),
        },
        observed_at=observed_at,
    )
    item.update(
        {
            "scope_kind": "tenant",
            "tenant_derived": True,
            "user_id": "evidence-owner",
            "session_id": "evidence-session",
            "entity_refs": ["component:api"],
        }
    )
    if valid_from is not None:
        item["valid_from"] = valid_from
    if valid_to is not None:
        item["valid_to"] = valid_to
    if observed_at is None:
        item.pop("observed_at", None)
    return item


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-evidence-graph-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        start = base.isoformat()
        end = (base + timedelta(minutes=10)).isoformat()

        first = belief.upsert_claim(
            claim(value="ok", observed_at=start, valid_from=start, valid_to=end)
        )
        expect(first.get("evidence_graph_status") == "new", "first observation enters graph", first)
        first_ref = (first.get("evidence_refs") or [None])[0]
        expect(isinstance(first_ref, str) and first_ref.startswith("ev_"), "claim binds evidence node", first)

        supported = belief.upsert_claim(
            claim(
                value="ok",
                observed_at=(base + timedelta(seconds=2)).isoformat(),
                valid_from=start,
                valid_to=end,
            )
        )
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(supported.get("evidence_graph_status") == "supported", "same value overlaps as support", supported)
        expect(graph["summary"]["support"] == 1, "support edge is durable", graph)

        contradiction = belief.upsert_claim(
            claim(
                value="down",
                observed_at=(base + timedelta(seconds=3)).isoformat(),
                valid_from=start,
                valid_to=end,
            )
        )
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(contradiction.get("evidence_graph_status") == "unresolved", "overlap conflict stays unresolved", contradiction)
        expect(graph["summary"]["contradiction"] >= 1, "contradiction edge is durable", graph)
        expect(graph["summary"]["unresolved"] >= 1, "conflict is visible in summary", graph)

        disjoint = belief.upsert_claim(
            claim(
                value="down",
                observed_at=(base + timedelta(hours=1)).isoformat(),
                valid_from=(base + timedelta(hours=1)).isoformat(),
                valid_to=(base + timedelta(hours=2)).isoformat(),
            )
        )
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(disjoint.get("evidence_graph_status") == "new", "disjoint valid time is not guessed as conflict", disjoint)
        expect(graph["summary"]["contradiction"] >= 1, "disjoint observation adds no contradiction", graph)

        superseded = claim(
            value="maintenance",
            observed_at=(base + timedelta(hours=3)).isoformat(),
            valid_from=(base + timedelta(hours=3)).isoformat(),
            valid_to=(base + timedelta(hours=4)).isoformat(),
        )
        superseded["supersedes_evidence_id"] = first_ref
        result = belief.upsert_claim(superseded)
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(result.get("evidence_graph_status") == "superseded", "explicit supersede is typed", result)
        expect(graph["summary"]["supersession"] == 1, "supersession edge is durable", graph)

        invalid = belief.upsert_claim(
            claim(
                value="unknown-time",
                observed_at="2026-08-11T12:00:00",
                valid_from="2026-08-11T12:05:00+00:00",
                valid_to="2026-08-11T12:04:00+00:00",
                key="service:api:invalid-time",
            )
        )
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(invalid.get("evidence_graph_status") == "unresolved", "invalid valid time fails closed", invalid)
        expect(graph["summary"]["invalid_valid_time"] >= 1, "invalid valid time remains visible", graph)

        unknown = belief.upsert_claim(
            claim(
                value="unknown-observation",
                observed_at=None,
                valid_from=None,
                valid_to=None,
                key="service:api:unknown-time",
            )
        )
        graph = store.read_json("belief_state.json")["evidence_graph"]
        expect(unknown.get("evidence_graph_status") == "unresolved", "missing valid time is unresolved", unknown)
        expect(graph["summary"]["contradiction"] >= 1, "unknown time never fabricates contradiction", graph)

        before = store.read_json("belief_state.json")
        store.write_json(
            "belief_state.json",
            {
                "claims": before.get("claims", []),
                "evidence_graph": {
                    "schema_version": "veyra.belief.evidence_graph.unknown",
                    "revision": 1,
                    "nodes": [],
                    "edges": [],
                    "unresolved": [],
                },
            },
        )
        rejected = belief.upsert_claim(
            claim(
                value="blocked",
                observed_at=(base + timedelta(hours=5)).isoformat(),
                valid_from=(base + timedelta(hours=5)).isoformat(),
                valid_to=(base + timedelta(hours=6)).isoformat(),
                key="service:api:corrupt-graph",
            )
        )
        after = store.read_json("belief_state.json")
        expect(rejected.get("status") == "rejected_evidence_graph", "corrupt graph rejects new claim", rejected)
        expect(after["claims"] == before["claims"], "corrupt graph rejection does not append claim", after)

    print("evidence graph smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
