#!/usr/bin/env python3
"""Adversarial EvidenceGraph/Belief capacity and truth receipts."""

from __future__ import annotations

import copy
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from awareness.evidence_graph import EvidenceGraphError, validate_evidence_graph
from core.perception_layer import PerceptionLayer
from core.world_state import WorldStateStore


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


BASE = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def claim(
    *,
    owner: str,
    session: str,
    key: str,
    value: str,
    offset: int,
    observation_id: str | None = None,
) -> dict[str, object]:
    observed = (BASE + timedelta(seconds=offset)).isoformat()
    item = make_claim(
        key=key,
        claim=f"{key}={value}",
        source="adversarial_probe",
        confidence=0.9,
        ttl_seconds=300,
        observed_at=observed,
        evidence={
            "status": value,
            "producer": "adversarial_smoke",
            "observation_id": observation_id or f"{owner}-{session}-{key}-{offset}",
        },
    )
    item.update(
        {
            "scope_kind": "tenant",
            "tenant_derived": True,
            "user_id": owner,
            "session_id": session,
            "valid_from": BASE.isoformat(),
            "valid_to": (BASE + timedelta(hours=1)).isoformat(),
        }
    )
    return item


def test_owner_partition_and_long_compaction(root: Path) -> None:
    store = WorldStateStore(root / "partition")
    belief = BeliefCore(store)
    for index in range(620):
        result = belief.upsert_claim(
            claim(
                owner="owner-a",
                session="session-a",
                key=f"event:{index}",
                value="ok",
                offset=index,
            )
        )
        expect(result.get("persisted") is True, "owner A stream stays writable", result)
    peer = belief.upsert_claim(
        claim(
            owner="owner-b",
            session="session-b",
            key="event:peer",
            value="ok",
            offset=1000,
        )
    )
    state = store.read_json("belief_state.json")
    graph = state["evidence_graph"]
    expect(peer.get("persisted") is True, "peer owner is not blocked by another stream", peer)
    expect(len(graph["nodes"]) <= 500, "global node bound holds", len(graph["nodes"]))
    expect(
        any(item.get("user_id") == "owner-b" for item in state.get("claims", [])),
        "peer claim remains readable after compaction",
        state.get("claims"),
    )

    compact = BeliefCore(WorldStateStore(root / "long"))
    for index in range(520):
        result = compact.upsert_claim(
            claim(
                owner="owner-a",
                session="session-a",
                key="service:long:status",
                value="ok",
                offset=index,
            )
        )
        expect(result.get("persisted") is True, "long same-value stream stays writable", result)
    graph = compact.state_store.read_json("belief_state.json")["evidence_graph"]
    expect(
        len(graph["nodes"]) == 500
        and len(graph["edges"]) <= 1000
        and graph.get("node_compaction_count", 0) > 0
        and all(len(item) <= 8 for item in graph.get("frontiers", {}).values()),
        "long compaction remains bounded",
        graph,
    )


def test_incoming_duplicate_and_conflict_receipt(root: Path) -> None:
    store = WorldStateStore(root / "replay")
    belief = BeliefCore(store)
    first = belief.upsert_claim(
        claim(owner="owner-a", session="session-a", key="service:status", value="ok", offset=0)
    )
    contradiction = belief.upsert_claim(
        claim(owner="owner-a", session="session-a", key="service:status", value="down", offset=1)
    )
    replay = belief.upsert_claim(
        claim(
            owner="owner-a",
            session="session-a",
            key="service:status",
            value="ok",
            offset=0,
        )
    )
    expect(first.get("evidence_graph_status") == "new", "first observation is new", first)
    expect(contradiction.get("evidence_graph_status") == "unresolved", "contradiction is durable", contradiction)
    expect(
        replay.get("persistence_status") == "conflict"
        and replay.get("belief_value_persisted") is False,
        "incoming contradiction makes duplicate replay non-authoritative",
        replay,
    )

    perception = PerceptionLayer(
        WorldStateStore(root / "perception"),
        model_assist_enabled=False,
    )
    perception.interpret_probe_result(
        {
            "probe": "status_probe",
            "status": "ok",
            "summary": "service is ok",
            "observed_at": BASE.isoformat(),
            "ttl_seconds": 300,
            "scope_kind": "tenant",
            "tenant_derived": True,
            "user_id": "owner-a",
            "session_id": "session-a",
            "claims": [claim(owner="owner-a", session="session-a", key="service:status", value="ok", offset=0)],
        }
    )
    conflict = perception.interpret_probe_result(
        {
            "probe": "status_probe",
            "status": "down",
            "summary": "service is down",
            "observed_at": (BASE + timedelta(seconds=1)).isoformat(),
            "ttl_seconds": 300,
            "scope_kind": "tenant",
            "tenant_derived": True,
            "user_id": "owner-a",
            "session_id": "session-a",
            "claims": [claim(owner="owner-a", session="session-a", key="service:status", value="down", offset=1)],
        }
    )
    persistence = conflict["belief_persistence"]
    expect(
        persistence["status"] == "conflict" and persistence["accepted_count"] == 0,
        "Perception does not aggregate an existing conflict as accepted",
        persistence,
    )


def test_malformed_graph_rejected(root: Path) -> None:
    store = WorldStateStore(root / "malformed")
    belief = BeliefCore(store)
    belief.upsert_claim(
        claim(owner="owner-a", session="session-a", key="service:status", value="ok", offset=0)
    )
    graph = store.read_json("belief_state.json")["evidence_graph"]
    mutations = [
        ("revision", lambda value: value.update({"revision": True})),
        ("summary", lambda value: value["summary"].update({"nodes": 999})),
        ("node_digest", lambda value: value["nodes"][0].update({"value_digest": "0" * 64})),
        ("frontier", lambda value: value["frontiers"].update({"0" * 64: ["ev_bad"]})),
    ]
    for label, mutate in mutations:
        malformed = copy.deepcopy(graph)
        mutate(malformed)
        try:
            validate_evidence_graph(malformed)
        except EvidenceGraphError:
            continue
        raise AssertionError(f"malformed graph accepted: {label}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-evidence-adversarial-") as tmp:
        root = Path(tmp)
        test_owner_partition_and_long_compaction(root)
        test_incoming_duplicate_and_conflict_receipt(root)
        test_malformed_graph_rejected(root)
    print("evidence graph adversarial smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
