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
from core.perception_layer import PerceptionLayer
from core.world_state import WorldStateStore
from runtime.state_refresh import StateRefresh


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
        stored_conflict = store.read_json("belief_state.json")["claims"][0]
        expect(
            stored_conflict.get("claim") == "api is ok"
            and stored_conflict.get("status") == "conflict"
            and stored_conflict.get("conflict_observations"),
            "conflicting observation cannot silently overwrite the current belief",
            stored_conflict,
        )

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

        # Long-lived same-value observations use only a bounded identity
        # frontier.  They must remain writable without exhausting the global
        # edge cap, and old corroboration edges may be compacted safely.
        long_store = WorldStateStore(Path(tmp) / "long-state")
        long_belief = BeliefCore(long_store)
        for index in range(520):
            observed = (base + timedelta(seconds=index)).isoformat()
            result = long_belief.upsert_claim(
                claim(
                    value="ok",
                    observed_at=observed,
                    valid_from=start,
                    valid_to=end,
                    key="service:long-lived:status",
                )
            )
            expect(result.get("persisted") is True, "bounded graph accepts long-lived observations", result)
        long_graph = long_store.read_json("belief_state.json")["evidence_graph"]
        expect(
            len(long_graph["nodes"]) == 500
            and len(long_graph["edges"]) <= 1000
            and long_graph.get("compaction_count", 0) > 0
            and long_graph.get("node_compaction_count", 0) > 0
            and all(len(item) <= 8 for item in long_graph.get("frontiers", {}).values()),
            "identity frontier and support retention stay bounded",
            {
                "nodes": len(long_graph["nodes"]),
                "edges": len(long_graph["edges"]),
                "compaction_count": long_graph.get("compaction_count"),
                "node_compaction_count": long_graph.get("node_compaction_count"),
                "frontiers": long_graph.get("frontiers"),
            },
        )

        # Transport status must not mask the typed Git dirty fact.
        git_store = WorldStateStore(Path(tmp) / "git-state")
        git_belief = BeliefCore(git_store)
        clean = claim(value="clean", observed_at=start, valid_from=start, valid_to=end, key="git_workspace:dirty")
        clean["source"] = "git_probe"
        clean["claim"] = "git workspace is clean"
        clean["evidence"] = {"status": "ok", "dirty": False, "producer": "git_probe"}
        clean.update({"scope_kind": "operator_global"})
        for key in ("tenant_derived", "user_id", "session_id"):
            clean.pop(key, None)
        git_belief.upsert_claim(clean)
        dirty = dict(clean)
        dirty["claim"] = "git workspace has uncommitted changes"
        dirty["evidence"] = {"status": "ok", "dirty": True, "producer": "git_probe"}
        dirty["observed_at"] = (base + timedelta(seconds=1)).isoformat()
        dirty["updated_at"] = dirty["observed_at"]
        dirty["expires_at"] = (base + timedelta(minutes=5)).isoformat()
        dirty_result = git_belief.upsert_claim(dirty)
        git_claim = git_store.read_json("belief_state.json")["claims"][0]
        expect(
            dirty_result.get("evidence_graph_status") == "unresolved"
            and git_claim.get("status") == "conflict"
            and git_claim.get("claim") == "git workspace is clean",
            "Git clean-to-dirty is a typed contradiction rather than support",
            {"result": dirty_result, "claim": git_claim},
        )

        # A successful probe whose value is rejected by the evidence graph is
        # a degraded refresh, never a reported success.
        refresh_store = WorldStateStore(Path(tmp) / "refresh-state")
        perception = PerceptionLayer(refresh_store, model_assist_enabled=False)
        perception.interpret_probe_result(
            {
                "probe": "git_probe",
                "status": "ok",
                "dirty": False,
                "summary": "git workspace is clean",
                "observed_at": start,
                "ttl_seconds": 1,
                "scope_kind": "operator_global",
            }
        )
        refresh_store.mutate_json(
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

        class DirtyGitProbe:
            def run(self, target: str = "") -> dict[str, object]:
                return {
                    "probe": "git_probe",
                    "status": "ok",
                    "dirty": True,
                    "summary": "git workspace is dirty",
                    "observed_at": (base + timedelta(seconds=1)).isoformat(),
                    "ttl_seconds": 60,
                    "scope_kind": "operator_global",
                }

        refresh = StateRefresh(refresh_store, model_assist_enabled=False)
        refresh.probes["git_probe"] = DirtyGitProbe()
        class ConflictPerception:
            def interpret_probe_result(self, raw: dict[str, object]) -> dict[str, object]:
                return {
                    "status": "conflict",
                    "belief_persistence": {
                        "status": "conflict",
                        "results": [],
                        "accepted_count": 0,
                        "conflicted_count": 1,
                        "rejected_count": 0,
                    },
                    "probe": raw,
                }

        refresh.perception = ConflictPerception()
        refresh_result = refresh.refresh_stale(limit=1)
        expect(
            refresh_result["status"] == "degraded"
            and refresh_result["refreshed"] == []
            and refresh_result["failed"]
            and refresh_result["failed"][0]["reason"] == "belief_persistence_conflict",
            "refresh propagates conflict instead of reporting a false success",
            refresh_result,
        )

    print("evidence graph smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
