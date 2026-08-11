from __future__ import annotations

"""A small, claim-adjacent EvidenceGraph for Belief provenance.

The graph deliberately stores typed digests and bounded provenance rather than
claim prose.  It is a durable record of what observations were related; it is
not an authority source and it never selects a winning value for a conflict.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


EVIDENCE_GRAPH_SCHEMA_VERSION = "veyra.belief.evidence_graph.v1"
EVIDENCE_NODE_SCHEMA_VERSION = "veyra.belief.evidence_node.v1"
EVIDENCE_EDGE_SCHEMA_VERSION = "veyra.belief.evidence_edge.v1"
MAX_EVIDENCE_NODES = 500
MAX_EVIDENCE_EDGES = 1000
MAX_UNRESOLVED = 256
MAX_FRONTIER_PER_IDENTITY = 8
RELATIONS = frozenset({"supports", "contradicts", "supersedes"})


class EvidenceGraphError(ValueError):
    """Raised when the durable graph cannot safely accept a new observation."""


def empty_evidence_graph() -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_GRAPH_SCHEMA_VERSION,
        "revision": 0,
        "nodes": [],
        "edges": [],
        "unresolved": [],
        # Only this bounded frontier is used for inferred relations.  The
        # graph remains a provenance record, but new observations must not
        # compare against every historical node for the same identity.
        "frontiers": {},
        "compaction_count": 0,
        "summary": {
            "nodes": 0,
            "edges": 0,
            "support": 0,
            "contradiction": 0,
            "supersession": 0,
            "unresolved": 0,
            "invalid_valid_time": 0,
        },
    }


def ingest_claim(
    graph: dict[str, Any] | None,
    claim: dict[str, Any],
    *,
    identity: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Append one claim observation and return graph, node and claim projection.

    The operation is deterministic and idempotent for an exact observation.
    Contradictory overlapping valid-time observations are retained as edges and
    reported as ``unresolved``; no value is silently preferred.
    """

    current = _validate_graph(graph)
    node = _node_from_claim(claim, identity)
    existing = next(
        (
            item
            for item in current["nodes"]
            if isinstance(item, dict) and item.get("evidence_id") == node["evidence_id"]
        ),
        None,
    )
    if existing is not None:
        projection = _projection_for_node(current, node["evidence_id"])
        projection["status"] = "duplicate"
        return current, existing, projection

    if len(current["nodes"]) >= MAX_EVIDENCE_NODES:
        raise EvidenceGraphError("evidence_graph_capacity_exhausted")

    identity_digest = node["identity_digest"]
    projection_status = "new"
    conflict_refs: list[str] = []
    unresolved_before = len(current["unresolved"])
    explicit_edges = _explicit_edges(current, node, claim)
    relation_edges: list[dict[str, Any]] = []

    for previous in _frontier_nodes(current, identity_digest):
        relation, reason = _infer_relation(node, previous)
        if relation is not None:
            relation_edges.append(_edge(node, previous, relation, reason))
            if relation == "contradicts":
                conflict_refs.append(str(previous.get("evidence_id") or ""))
                projection_status = "unresolved"
            elif relation == "supports" and projection_status == "new":
                projection_status = "supported"
        elif reason:
            _record_unresolved(
                current,
                node,
                previous,
                reason,
            )
            projection_status = "unresolved"

    for relation_edge in explicit_edges:
        relation_edges.append(relation_edge)
        if relation_edge["relation"] == "contradicts":
            conflict_refs.append(relation_edge["to"])
            projection_status = "unresolved"
        elif relation_edge["relation"] == "supersedes":
            projection_status = "superseded"

    if len(current["unresolved"]) > unresolved_before:
        projection_status = "unresolved"

    if node["entity_refs_status"] == "invalid":
        _record_unresolved(current, node, None, "entity_refs_invalid")
        projection_status = "unresolved"

    if node["valid_time"]["status"] != "known":
        _record_unresolved(current, node, None, f"valid_time_{node['valid_time']['status']}")
        projection_status = "unresolved"

    current["nodes"].append(node)
    for edge in relation_edges:
        if not any(existing_edge.get("edge_id") == edge["edge_id"] for existing_edge in current["edges"]):
            current["edges"].append(edge)
    _compact_edges(current)
    _update_frontier(current, identity_digest, node["evidence_id"])
    current["revision"] = int(current.get("revision") or 0) + 1
    _refresh_summary(current)
    projection = {
        "status": projection_status,
        "evidence_refs": [node["evidence_id"]],
        "conflict_refs": sorted({item for item in conflict_refs if item}),
    }
    if projection["conflict_refs"]:
        projection["status"] = "unresolved"
    return current, node, projection


def validate_evidence_graph(graph: Any) -> dict[str, Any]:
    """Validate and detach a graph before it enters a writer mutation."""

    return _validate_graph(graph)


def _validate_graph(graph: Any) -> dict[str, Any]:
    if graph is None:
        return empty_evidence_graph()
    if not isinstance(graph, dict):
        raise EvidenceGraphError("evidence_graph_not_an_object")
    if graph.get("schema_version") != EVIDENCE_GRAPH_SCHEMA_VERSION:
        raise EvidenceGraphError("evidence_graph_schema_mismatch")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    unresolved = graph.get("unresolved")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(unresolved, list):
        raise EvidenceGraphError("evidence_graph_collections_invalid")
    if len(nodes) > MAX_EVIDENCE_NODES or len(edges) > MAX_EVIDENCE_EDGES or len(unresolved) > MAX_UNRESOLVED:
        raise EvidenceGraphError("evidence_graph_capacity_invalid")
    for node in nodes:
        if not isinstance(node, dict) or not _bounded_text(node.get("evidence_id"), 80):
            raise EvidenceGraphError("evidence_graph_node_invalid")
        if node.get("schema_version") != EVIDENCE_NODE_SCHEMA_VERSION:
            raise EvidenceGraphError("evidence_graph_node_schema_mismatch")
        if not _is_digest(node.get("identity_digest")):
            raise EvidenceGraphError("evidence_graph_node_identity_invalid")
    for edge in edges:
        if not isinstance(edge, dict):
            raise EvidenceGraphError("evidence_graph_edge_invalid")
        if edge.get("schema_version") != EVIDENCE_EDGE_SCHEMA_VERSION or edge.get("relation") not in RELATIONS:
            raise EvidenceGraphError("evidence_graph_edge_schema_mismatch")
        if not all(_bounded_text(edge.get(field), 80) for field in ("edge_id", "from", "to")):
            raise EvidenceGraphError("evidence_graph_edge_identity_invalid")
    for item in unresolved:
        if not isinstance(item, dict) or not _bounded_text(item.get("unresolved_id"), 80):
            raise EvidenceGraphError("evidence_graph_unresolved_invalid")
    detached = json.loads(json.dumps(graph, ensure_ascii=False))
    frontiers = detached.get("frontiers")
    if frontiers is None:
        frontiers = {}
        detached["frontiers"] = frontiers
    if not isinstance(frontiers, dict) or len(frontiers) > MAX_EVIDENCE_NODES:
        raise EvidenceGraphError("evidence_graph_frontier_invalid")
    for identity_digest, evidence_ids in frontiers.items():
        if not _is_digest(identity_digest) or not isinstance(evidence_ids, list):
            raise EvidenceGraphError("evidence_graph_frontier_invalid")
        if len(evidence_ids) > MAX_FRONTIER_PER_IDENTITY or any(
            not _bounded_text(item, 80) for item in evidence_ids
        ):
            raise EvidenceGraphError("evidence_graph_frontier_invalid")
    compaction_count = detached.get("compaction_count", 0)
    if isinstance(compaction_count, bool) or not isinstance(compaction_count, int) or compaction_count < 0:
        raise EvidenceGraphError("evidence_graph_compaction_invalid")
    detached["compaction_count"] = compaction_count
    return detached


def _frontier_nodes(graph: dict[str, Any], identity_digest: str) -> list[dict[str, Any]]:
    """Return a bounded relation frontier, rebuilding legacy graphs once."""

    by_id = {
        str(item.get("evidence_id")): item
        for item in graph.get("nodes", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    frontiers = graph.setdefault("frontiers", {})
    selected_ids = frontiers.get(identity_digest)
    if not isinstance(selected_ids, list) or not selected_ids:
        selected_ids = [
            str(item.get("evidence_id"))
            for item in graph.get("nodes", [])
            if isinstance(item, dict) and item.get("identity_digest") == identity_digest
        ][-MAX_FRONTIER_PER_IDENTITY:]
        frontiers[identity_digest] = selected_ids
    return [
        by_id[evidence_id]
        for evidence_id in selected_ids[-MAX_FRONTIER_PER_IDENTITY:]
        if evidence_id in by_id and by_id[evidence_id].get("identity_digest") == identity_digest
    ]


def _update_frontier(graph: dict[str, Any], identity_digest: str, evidence_id: str) -> None:
    frontiers = graph.setdefault("frontiers", {})
    previous = frontiers.get(identity_digest)
    selected = list(previous) if isinstance(previous, list) else []
    selected = [item for item in selected if item != evidence_id]
    selected.append(evidence_id)
    frontiers[identity_digest] = selected[-MAX_FRONTIER_PER_IDENTITY:]


def _compact_edges(graph: dict[str, Any]) -> None:
    """Keep safety relations and a recent bounded support frontier.

    Support edges are corroboration history and can be compacted.  Contradiction
    and supersession edges are retained because dropping either would hide a
    governance-relevant relation.  This makes the edge bound a retention
    policy rather than a sporadic write failure.
    """

    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    if len(edges) <= MAX_EVIDENCE_EDGES:
        return
    safety = [item for item in edges if item.get("relation") != "supports"]
    if len(safety) > MAX_EVIDENCE_EDGES:
        raise EvidenceGraphError("evidence_graph_safety_edge_capacity_exhausted")
    node_order = {
        str(item.get("evidence_id")): index
        for index, item in enumerate(graph.get("nodes", []))
        if isinstance(item, dict)
    }
    supports = [item for item in edges if item.get("relation") == "supports"]
    keep_support_count = MAX_EVIDENCE_EDGES - len(safety)
    recent_supports = sorted(
        supports,
        key=lambda item: (node_order.get(str(item.get("from") or ""), -1), str(item.get("edge_id") or "")),
        reverse=True,
    )[:keep_support_count]
    retained_ids = {str(item.get("edge_id")) for item in safety + recent_supports}
    graph["edges"] = [item for item in edges if str(item.get("edge_id")) in retained_ids]
    graph["compaction_count"] = int(graph.get("compaction_count") or 0) + len(edges) - len(graph["edges"])


def _node_from_claim(claim: dict[str, Any], identity: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(claim, dict):
        raise EvidenceGraphError("evidence_claim_not_an_object")
    source = _bounded_text(claim.get("source"), 120)
    if not source:
        raise EvidenceGraphError("evidence_claim_source_missing")
    key = _bounded_text(claim.get("key") or claim.get("claim"), 240)
    if not key:
        raise EvidenceGraphError("evidence_claim_key_missing")
    evidence = claim.get("evidence") if isinstance(claim.get("evidence"), dict) else {}
    valid_time = _valid_time(claim, evidence)
    entity_refs, entity_status = _entity_refs(claim, evidence)
    value_digest, value_kind = _value_digest(claim, evidence)
    provenance = {
        "source": source,
        "producer": _bounded_text(evidence.get("producer") or claim.get("producer") or source, 120),
        "observation_id": _bounded_text(
            evidence.get("observation_id")
            or evidence.get("receipt_id")
            or evidence.get("event_id")
            or claim.get("observation_id"),
            160,
        ),
        "claim_kind": _bounded_text(claim.get("claim_kind") or "observed", 32),
    }
    if not provenance["producer"]:
        raise EvidenceGraphError("evidence_producer_missing")
    node_material = {
        "identity_digest": _digest(identity),
        "key": key,
        "source": source,
        "provenance": provenance,
        "valid_time": valid_time,
        "entity_refs": entity_refs,
        "value_digest": value_digest,
        "value_kind": value_kind,
    }
    evidence_id = f"ev_{_digest(node_material)[:24]}"
    return {
        "schema_version": EVIDENCE_NODE_SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "claim_key": key,
        "identity_digest": _digest(identity),
        "source": source,
        "provenance": provenance,
        "valid_time": valid_time,
        "entity_refs": entity_refs,
        "entity_refs_status": entity_status,
        "value_digest": value_digest,
        "value_kind": value_kind,
        "observed_at": valid_time.get("observed_at"),
    }


def _projection_for_node(graph: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    conflict_refs = sorted(
        {
            str(edge.get("to") or "")
            for edge in graph["edges"]
            if edge.get("relation") == "contradicts"
            and edge.get("from") == evidence_id
        }
    )
    return {
        "status": "supported" if not conflict_refs else "unresolved",
        "evidence_refs": [evidence_id],
        "conflict_refs": [item for item in conflict_refs if item],
    }


def _infer_relation(new: dict[str, Any], previous: dict[str, Any]) -> tuple[str | None, str | None]:
    temporal = _temporal_relation(new.get("valid_time") or {}, previous.get("valid_time") or {})
    if temporal == "invalid":
        return None, "invalid_valid_time_overlap"
    if temporal == "unknown":
        if new.get("value_digest") != previous.get("value_digest"):
            return None, "valid_time_unknown_for_conflict"
        return None, None
    if temporal == "disjoint":
        return None, None
    if new.get("value_digest") == previous.get("value_digest"):
        return "supports", "same_typed_value_overlapping_valid_time"
    return "contradicts", "different_typed_value_overlapping_valid_time"


def _explicit_edges(graph: dict[str, Any], node: dict[str, Any], claim: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = claim.get("evidence") if isinstance(claim.get("evidence"), dict) else {}
    result: list[dict[str, Any]] = []
    for relation in ("supports", "contradicts", "supersedes"):
        target = claim.get(f"{relation}_evidence_id") or evidence.get(f"{relation}_evidence_id")
        if not isinstance(target, str) or not target:
            continue
        previous = next(
            (
                item
                for item in graph["nodes"]
                if isinstance(item, dict) and item.get("evidence_id") == target
            ),
            None,
        )
        if previous is None or previous.get("identity_digest") != node.get("identity_digest"):
            _record_unresolved(graph, node, previous, f"missing_or_cross_scope_{relation}_target")
            continue
        result.append(_edge(node, previous, relation, f"explicit_{relation}"))
    return result


def _edge(new: dict[str, Any], previous: dict[str, Any], relation: str, reason: str) -> dict[str, Any]:
    material = {
        "from": new["evidence_id"],
        "to": previous["evidence_id"],
        "relation": relation,
        "reason": reason,
    }
    return {
        "schema_version": EVIDENCE_EDGE_SCHEMA_VERSION,
        "edge_id": f"edge_{_digest(material)[:24]}",
        "relation": relation,
        "from": new["evidence_id"],
        "to": previous["evidence_id"],
        "reason": reason,
    }


def _record_unresolved(
    graph: dict[str, Any],
    node: dict[str, Any],
    previous: dict[str, Any] | None,
    reason: str,
) -> None:
    related = str((previous or {}).get("evidence_id") or "")
    material = {"node": node.get("evidence_id"), "related": related, "reason": reason}
    unresolved_id = f"un_{_digest(material)[:24]}"
    if any(item.get("unresolved_id") == unresolved_id for item in graph["unresolved"] if isinstance(item, dict)):
        return
    if len(graph["unresolved"]) >= MAX_UNRESOLVED:
        raise EvidenceGraphError("evidence_graph_unresolved_capacity_exhausted")
    graph["unresolved"].append(
        {
            "unresolved_id": unresolved_id,
            "evidence_id": node.get("evidence_id"),
            "related_evidence_id": related or None,
            "reason": _bounded_text(reason, 120),
        }
    )


def _refresh_summary(graph: dict[str, Any]) -> None:
    edges = [item for item in graph["edges"] if isinstance(item, dict)]
    graph["summary"] = {
        "nodes": len(graph["nodes"]),
        "edges": len(edges),
        "support": sum(item.get("relation") == "supports" for item in edges),
        "contradiction": sum(item.get("relation") == "contradicts" for item in edges),
        "supersession": sum(item.get("relation") == "supersedes" for item in edges),
        "unresolved": len(graph["unresolved"])
        + sum(item.get("relation") == "contradicts" for item in edges),
        "invalid_valid_time": sum(
            (item.get("valid_time") or {}).get("status") == "invalid"
            for item in graph["nodes"]
            if isinstance(item, dict)
        ),
    }


def _valid_time(claim: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    observed_raw = claim.get("observed_at") or evidence.get("observed_at")
    valid_from_raw = claim.get("valid_from") or evidence.get("valid_from") or observed_raw
    valid_to_raw = claim.get("valid_to") or evidence.get("valid_to")
    observed = _aware_time(observed_raw)
    valid_from = _aware_time(valid_from_raw)
    valid_to = _aware_time(valid_to_raw)
    if observed_raw and observed is None:
        return {"status": "invalid", "reason": "observed_at_not_aware", "observed_at": None, "valid_from": None, "valid_to": None}
    if valid_from_raw and valid_from is None:
        return {"status": "invalid", "reason": "valid_from_not_aware", "observed_at": _iso(observed), "valid_from": None, "valid_to": None}
    if valid_to_raw and valid_to is None:
        return {"status": "invalid", "reason": "valid_to_not_aware", "observed_at": _iso(observed), "valid_from": _iso(valid_from), "valid_to": None}
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        return {"status": "invalid", "reason": "valid_to_before_valid_from", "observed_at": _iso(observed), "valid_from": _iso(valid_from), "valid_to": _iso(valid_to)}
    if valid_from is None:
        return {"status": "unknown", "reason": "valid_from_missing", "observed_at": _iso(observed), "valid_from": None, "valid_to": _iso(valid_to)}
    return {
        "status": "known",
        "observed_at": _iso(observed),
        "valid_from": _iso(valid_from),
        "valid_to": _iso(valid_to),
    }


def _temporal_relation(left: dict[str, Any], right: dict[str, Any]) -> str:
    if left.get("status") == "invalid" or right.get("status") == "invalid":
        return "invalid"
    if left.get("status") != "known" or right.get("status") != "known":
        return "unknown"
    left_start = _aware_time(left.get("valid_from"))
    right_start = _aware_time(right.get("valid_from"))
    left_end = _aware_time(left.get("valid_to"))
    right_end = _aware_time(right.get("valid_to"))
    if left_start is None or right_start is None:
        return "unknown"
    start = max(left_start, right_start)
    ends = [item for item in (left_end, right_end) if item is not None]
    if ends and start > min(ends):
        return "disjoint"
    return "overlap"


def _entity_refs(claim: dict[str, Any], evidence: dict[str, Any]) -> tuple[list[str], str]:
    raw = claim.get("entity_refs") if "entity_refs" in claim else evidence.get("entity_refs")
    if raw is None:
        return [], "absent"
    if not isinstance(raw, list):
        return [], "invalid"
    refs: list[str] = []
    for item in raw[:16]:
        normalized = _bounded_text(item, 160)
        if not normalized:
            return [], "invalid"
        refs.append(normalized)
    return sorted(set(refs)), "valid"


def _value_digest(claim: dict[str, Any], evidence: dict[str, Any]) -> tuple[str, str]:
    # Git observations may carry both a transport status (usually ``ok``) and
    # the typed workspace fact ``dirty``.  The fact must win; otherwise a
    # clean-to-dirty transition is incorrectly classified as support.
    for field in ("dirty", "status", "value", "exists", "state", "phase"):
        if field in evidence:
            return _digest({"field": field, "value": evidence[field]}), "typed"
    return _digest({"claim": str(claim.get("claim") or "")}), "claim_digest"


def _aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) > limit or any(ord(char) < 32 for char in text):
        return ""
    return text


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
