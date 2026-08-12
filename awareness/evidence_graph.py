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
# A single owner may produce an unbounded stream of independent identities
# (notably ``event:{event_id}``).  Support-only provenance is compactable, but
# contradiction/supersession endpoints are safety history and cannot simply be
# evicted.  Keep a per-owner safety budget so one owner cannot consume every
# global node slot while still allowing ordinary support streams to rotate.
MAX_SAFETY_NODES_PER_PARTITION = 256
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
        "node_compaction_count": 0,
        "unresolved_compaction_count": 0,
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
        projection["duplicate"] = True
        # A replay is only an ordinary duplicate when there is no durable
        # relation attached to the node.  Contradiction and supersession edges
        # may point *into* the replayed node; projecting outgoing edges alone
        # would incorrectly resurrect a fresh winner.
        if projection.get("status") == "new":
            projection["status"] = "duplicate"
        return current, existing, projection

    if len(current["nodes"]) >= MAX_EVIDENCE_NODES:
        _compact_nodes(current, preferred_partition=node.get("partition_digest"))
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

    _ensure_safety_partition_capacity(current, node, relation_edges)

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
    # Work on a detached JSON-compatible copy.  A malformed nested value must
    # be reported as EvidenceGraphError, never as a leaked TypeError/ValueError
    # from the copier or a later field operation.
    try:
        detached = json.loads(json.dumps(graph, ensure_ascii=False))
    except Exception as exc:  # pragma: no cover - defensive for non-JSON input
        raise EvidenceGraphError("evidence_graph_not_json") from exc
    try:
        if detached.get("schema_version") != EVIDENCE_GRAPH_SCHEMA_VERSION:
            raise EvidenceGraphError("evidence_graph_schema_mismatch")
        revision = detached.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise EvidenceGraphError("evidence_graph_revision_invalid")
        nodes = detached.get("nodes")
        edges = detached.get("edges")
        unresolved = detached.get("unresolved")
        if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(unresolved, list):
            raise EvidenceGraphError("evidence_graph_collections_invalid")
        if (
            len(nodes) > MAX_EVIDENCE_NODES
            or len(edges) > MAX_EVIDENCE_EDGES
            or len(unresolved) > MAX_UNRESOLVED
        ):
            raise EvidenceGraphError("evidence_graph_capacity_invalid")

        node_ids: set[str] = set()
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            _validate_node_record(node, node_ids, nodes_by_id)

        edge_ids: set[str] = set()
        for edge in edges:
            _validate_edge_record(edge, edge_ids, nodes_by_id)

        unresolved_ids: set[str] = set()
        for item in unresolved:
            _validate_unresolved_record(item, unresolved_ids, nodes_by_id)

        frontiers = detached.get("frontiers")
        if frontiers is None:
            # ``frontiers`` was added after the original v1 writer.  Rebuild it
            # lazily from retained nodes for schema-compatible old state.
            frontiers = {}
            detached["frontiers"] = frontiers
        if not isinstance(frontiers, dict) or len(frontiers) > MAX_EVIDENCE_NODES:
            raise EvidenceGraphError("evidence_graph_frontier_invalid")
        for identity_digest, evidence_ids in frontiers.items():
            if not _is_digest(identity_digest) or not isinstance(evidence_ids, list):
                raise EvidenceGraphError("evidence_graph_frontier_invalid")
            if len(evidence_ids) > MAX_FRONTIER_PER_IDENTITY:
                raise EvidenceGraphError("evidence_graph_frontier_capacity_invalid")
            if len(set(evidence_ids)) != len(evidence_ids):
                raise EvidenceGraphError("evidence_graph_frontier_duplicate")
            for evidence_id in evidence_ids:
                if not _short_id(evidence_id, "ev_") or evidence_id not in nodes_by_id:
                    raise EvidenceGraphError("evidence_graph_frontier_dangling")
                if nodes_by_id[evidence_id].get("identity_digest") != identity_digest:
                    raise EvidenceGraphError("evidence_graph_frontier_cross_identity")

        compaction_count = detached.get("compaction_count", 0)
        if isinstance(compaction_count, bool) or not isinstance(compaction_count, int) or compaction_count < 0:
            raise EvidenceGraphError("evidence_graph_compaction_invalid")
        detached["compaction_count"] = compaction_count
        node_compaction_count = detached.get("node_compaction_count", 0)
        if (
            isinstance(node_compaction_count, bool)
            or not isinstance(node_compaction_count, int)
            or node_compaction_count < 0
        ):
            raise EvidenceGraphError("evidence_graph_node_compaction_invalid")
        detached["node_compaction_count"] = node_compaction_count
        unresolved_compaction_count = detached.get("unresolved_compaction_count", 0)
        if (
            isinstance(unresolved_compaction_count, bool)
            or not isinstance(unresolved_compaction_count, int)
            or unresolved_compaction_count < 0
        ):
            raise EvidenceGraphError("evidence_graph_unresolved_compaction_invalid")
        detached["unresolved_compaction_count"] = unresolved_compaction_count
        _validate_summary(detached, nodes, edges, unresolved)
        return detached
    except EvidenceGraphError:
        raise
    except Exception as exc:  # pragma: no cover - malformed JSON edge cases
        raise EvidenceGraphError("evidence_graph_invalid") from exc


def _validate_node_record(
    node: Any,
    node_ids: set[str],
    nodes_by_id: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(node, dict):
        raise EvidenceGraphError("evidence_graph_node_invalid")
    evidence_id = node.get("evidence_id")
    if not _short_id(evidence_id, "ev_") or evidence_id in node_ids:
        raise EvidenceGraphError("evidence_graph_node_id_invalid")
    if node.get("schema_version") != EVIDENCE_NODE_SCHEMA_VERSION:
        raise EvidenceGraphError("evidence_graph_node_schema_mismatch")
    if not _bounded_text(node.get("claim_key"), 240):
        raise EvidenceGraphError("evidence_graph_node_claim_key_invalid")
    if not _bounded_text(node.get("source"), 120):
        raise EvidenceGraphError("evidence_graph_node_source_invalid")
    identity_digest = node.get("identity_digest")
    if not _is_digest(identity_digest):
        raise EvidenceGraphError("evidence_graph_node_identity_invalid")
    partition_digest = node.get("partition_digest")
    if partition_digest is not None and not _is_digest(partition_digest):
        raise EvidenceGraphError("evidence_graph_node_partition_invalid")
    # New writers retain the identity material needed to recompute the
    # identity/partition digests at load time.  Legacy v1 nodes may omit these
    # private fields and remain readable, but a present field is a strict
    # security boundary rather than caller-controlled metadata.
    raw_identity = node.get("identity")
    if raw_identity is not None:
        if (
            not isinstance(raw_identity, list)
            or not raw_identity
            or len(raw_identity) > 8
            or any(not _bounded_text(item, 240) for item in raw_identity)
        ):
            raise EvidenceGraphError("evidence_graph_node_identity_material_invalid")
        identity_material = tuple(str(item) for item in raw_identity)
        if _digest(identity_material) != identity_digest:
            raise EvidenceGraphError("evidence_graph_node_identity_digest_mismatch")
        expected_partition = _partition_digest(identity_material)
        if partition_digest != expected_partition:
            raise EvidenceGraphError("evidence_graph_node_partition_digest_mismatch")
        raw_partition = node.get("partition")
        if raw_partition is not None:
            if (
                not isinstance(raw_partition, list)
                or tuple(str(item) for item in raw_partition)
                != _partition_material(identity_material)
            ):
                raise EvidenceGraphError("evidence_graph_node_partition_material_invalid")

    provenance = node.get("provenance")
    if not isinstance(provenance, dict):
        raise EvidenceGraphError("evidence_graph_node_provenance_invalid")
    if not _bounded_text(provenance.get("source"), 120) or not _bounded_text(provenance.get("producer"), 120):
        raise EvidenceGraphError("evidence_graph_node_provenance_invalid")
    observation_id = provenance.get("observation_id")
    if observation_id is not None and observation_id != "" and not _bounded_text(observation_id, 160):
        raise EvidenceGraphError("evidence_graph_node_provenance_invalid")
    if not _bounded_text(provenance.get("claim_kind"), 32):
        raise EvidenceGraphError("evidence_graph_node_provenance_invalid")
    if provenance.get("claim_kind") not in {"observed", "derived"}:
        raise EvidenceGraphError("evidence_graph_node_claim_kind_invalid")

    valid_time = node.get("valid_time")
    if not isinstance(valid_time, dict) or valid_time.get("status") not in {"known", "unknown", "invalid"}:
        raise EvidenceGraphError("evidence_graph_node_valid_time_invalid")
    for field in ("observed_at", "valid_from", "valid_to"):
        value = valid_time.get(field)
        if value is not None and _aware_time(value) is None:
            raise EvidenceGraphError("evidence_graph_node_valid_time_invalid")
    status = valid_time.get("status")
    if status == "known" and _aware_time(valid_time.get("valid_from")) is None:
        raise EvidenceGraphError("evidence_graph_node_valid_time_invalid")
    if status == "known" and valid_time.get("valid_to") is not None:
        if _aware_time(valid_time.get("valid_to")) < _aware_time(valid_time.get("valid_from")):
            raise EvidenceGraphError("evidence_graph_node_valid_time_invalid")
    reason = valid_time.get("reason")
    if reason is not None and not _bounded_text(reason, 120):
        raise EvidenceGraphError("evidence_graph_node_valid_time_invalid")

    entity_refs = node.get("entity_refs")
    if not isinstance(entity_refs, list) or len(entity_refs) > 16:
        raise EvidenceGraphError("evidence_graph_node_entity_refs_invalid")
    if any(not _bounded_text(item, 160) for item in entity_refs):
        raise EvidenceGraphError("evidence_graph_node_entity_refs_invalid")
    if len(set(entity_refs)) != len(entity_refs):
        raise EvidenceGraphError("evidence_graph_node_entity_refs_invalid")
    if node.get("entity_refs_status") not in {"absent", "valid", "invalid"}:
        raise EvidenceGraphError("evidence_graph_node_entity_refs_invalid")
    if node.get("entity_refs_status") == "absent" and entity_refs:
        raise EvidenceGraphError("evidence_graph_node_entity_refs_invalid")

    if not _is_digest(node.get("value_digest")):
        raise EvidenceGraphError("evidence_graph_node_value_digest_invalid")
    if node.get("value_kind") not in {"typed", "claim_digest"}:
        raise EvidenceGraphError("evidence_graph_node_value_kind_invalid")
    observed_at = node.get("observed_at")
    if observed_at is not None and _aware_time(observed_at) is None:
        raise EvidenceGraphError("evidence_graph_node_observed_at_invalid")
    if observed_at != valid_time.get("observed_at"):
        raise EvidenceGraphError("evidence_graph_node_observed_at_mismatch")

    expected_id = f"ev_{_digest(_node_material_from_record(node))[:24]}"
    if evidence_id != expected_id:
        raise EvidenceGraphError("evidence_graph_node_id_content_mismatch")
    node_ids.add(evidence_id)
    nodes_by_id[evidence_id] = node


def _validate_edge_record(
    edge: Any,
    edge_ids: set[str],
    nodes_by_id: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(edge, dict):
        raise EvidenceGraphError("evidence_graph_edge_invalid")
    edge_id = edge.get("edge_id")
    if not _short_id(edge_id, "edge_") or edge_id in edge_ids:
        raise EvidenceGraphError("evidence_graph_edge_id_invalid")
    if edge.get("schema_version") != EVIDENCE_EDGE_SCHEMA_VERSION or edge.get("relation") not in RELATIONS:
        raise EvidenceGraphError("evidence_graph_edge_schema_mismatch")
    source = edge.get("from")
    target = edge.get("to")
    if not _short_id(source, "ev_") or not _short_id(target, "ev_"):
        raise EvidenceGraphError("evidence_graph_edge_identity_invalid")
    if source == target:
        raise EvidenceGraphError("evidence_graph_edge_self_loop")
    if source not in nodes_by_id or target not in nodes_by_id:
        raise EvidenceGraphError("evidence_graph_edge_dangling")
    if nodes_by_id[source].get("identity_digest") != nodes_by_id[target].get("identity_digest"):
        raise EvidenceGraphError("evidence_graph_edge_cross_identity")
    reason = edge.get("reason")
    if not _bounded_text(reason, 120):
        raise EvidenceGraphError("evidence_graph_edge_reason_invalid")
    expected_id = f"edge_{_digest({'from': source, 'to': target, 'relation': edge['relation'], 'reason': reason})[:24]}"
    if edge_id != expected_id:
        raise EvidenceGraphError("evidence_graph_edge_id_content_mismatch")
    edge_ids.add(edge_id)


def _validate_unresolved_record(
    item: Any,
    unresolved_ids: set[str],
    nodes_by_id: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(item, dict):
        raise EvidenceGraphError("evidence_graph_unresolved_invalid")
    unresolved_id = item.get("unresolved_id")
    if not _short_id(unresolved_id, "un_") or unresolved_id in unresolved_ids:
        raise EvidenceGraphError("evidence_graph_unresolved_id_invalid")
    evidence_id = item.get("evidence_id")
    if not _short_id(evidence_id, "ev_") or evidence_id not in nodes_by_id:
        raise EvidenceGraphError("evidence_graph_unresolved_dangling")
    related = item.get("related_evidence_id")
    if related is not None:
        if not _short_id(related, "ev_") or related not in nodes_by_id:
            raise EvidenceGraphError("evidence_graph_unresolved_dangling")
        if related == evidence_id:
            raise EvidenceGraphError("evidence_graph_unresolved_self_loop")
        if nodes_by_id[evidence_id].get("identity_digest") != nodes_by_id[related].get("identity_digest"):
            raise EvidenceGraphError("evidence_graph_unresolved_cross_identity")
    reason = item.get("reason")
    if not _bounded_text(reason, 120):
        raise EvidenceGraphError("evidence_graph_unresolved_reason_invalid")
    expected_id = f"un_{_digest({'node': evidence_id, 'related': str(related or ''), 'reason': reason})[:24]}"
    if unresolved_id != expected_id:
        raise EvidenceGraphError("evidence_graph_unresolved_id_content_mismatch")
    unresolved_ids.add(unresolved_id)


def _validate_summary(
    graph: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> None:
    summary = graph.get("summary")
    if not isinstance(summary, dict):
        raise EvidenceGraphError("evidence_graph_summary_invalid")
    expected = {
        "nodes": len(nodes),
        "edges": len(edges),
        "support": sum(item.get("relation") == "supports" for item in edges),
        "contradiction": sum(item.get("relation") == "contradicts" for item in edges),
        "supersession": sum(item.get("relation") == "supersedes" for item in edges),
        "unresolved": len(unresolved) + sum(item.get("relation") == "contradicts" for item in edges),
        "invalid_valid_time": sum(
            (item.get("valid_time") or {}).get("status") == "invalid"
            for item in nodes
        ),
    }
    for key, value in expected.items():
        actual = summary.get(key)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != value:
            raise EvidenceGraphError("evidence_graph_summary_mismatch")


def _short_id(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and len(value) == len(prefix) + 24 and value.startswith(prefix) and all(
        char in "0123456789abcdef" for char in value[len(prefix):]
    )


def _node_material_from_record(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity_digest": node.get("identity_digest"),
        "key": node.get("claim_key"),
        "source": node.get("source"),
        "provenance": node.get("provenance"),
        "valid_time": node.get("valid_time"),
        "entity_refs": node.get("entity_refs"),
        "value_digest": node.get("value_digest"),
        "value_kind": node.get("value_kind"),
    }


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
    # Remove stale keys before adding a new identity.  The frontier is an
    # inference cache, not safety history; contradiction/supersession edges
    # retain their endpoints independently.
    live_ids = {
        str(item.get("evidence_id"))
        for item in graph.get("nodes", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    for key, values in list(frontiers.items()):
        if not isinstance(values, list):
            frontiers.pop(key, None)
            continue
        filtered = [value for value in values if value in live_ids]
        if filtered:
            frontiers[key] = filtered[-MAX_FRONTIER_PER_IDENTITY:]
        else:
            frontiers.pop(key, None)
    if identity_digest not in frontiers and len(frontiers) >= MAX_EVIDENCE_NODES:
        # Evict the oldest inference partition once the index itself reaches
        # its bound.  This does not erase any safety edge or endpoint.
        frontiers.pop(next(iter(frontiers)), None)
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


def _compact_nodes(graph: dict[str, Any], *, preferred_partition: str | None = None) -> None:
    """Retain safety evidence while rotating old support-only observations.

    Contradiction and supersession edges are governance-relevant and therefore
    pin both endpoint nodes.  Frontiers are only a bounded inference cache;
    support-only frontier nodes may be compacted.  This distinction is what
    lets a stream of unique identities rotate instead of exhausting the graph.
    """

    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    if len(nodes) < MAX_EVIDENCE_NODES:
        return
    node_ids = {
        str(item.get("evidence_id"))
        for item in nodes
        if item.get("evidence_id")
    }
    safety_ids: set[str] = set()
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") == "supports":
            continue
        for field in ("from", "to"):
            value = edge.get(field)
            if isinstance(value, str) and value in node_ids:
                safety_ids.add(value)
    protected = safety_ids
    removable = [
        item
        for item in nodes
        if str(item.get("evidence_id")) not in protected
    ]
    required = len(nodes) - (MAX_EVIDENCE_NODES - 1)
    if len(removable) < required:
        raise EvidenceGraphError("evidence_graph_safety_node_capacity_exhausted")
    if preferred_partition:
        same_partition = [
            item
            for item in removable
            if _node_partition(item) == preferred_partition
        ]
        other_partition = [item for item in removable if item not in same_partition]
        removable = same_partition + other_partition
    remove_ids = {
        str(item.get("evidence_id"))
        for item in removable[:required]
    }
    retained_ids = node_ids - remove_ids
    graph["nodes"] = [
        item
        for item in nodes
        if str(item.get("evidence_id")) in retained_ids
    ]
    graph["edges"] = [
        edge
        for edge in graph.get("edges", [])
        if isinstance(edge, dict)
        and str(edge.get("from") or "") in retained_ids
        and str(edge.get("to") or "") in retained_ids
    ]
    graph["unresolved"] = [
        item
        for item in graph.get("unresolved", [])
        if isinstance(item, dict)
        and str(item.get("evidence_id") or "") in retained_ids
        and (
            not item.get("related_evidence_id")
            or str(item.get("related_evidence_id")) in retained_ids
        )
    ]
    frontiers = graph.get("frontiers")
    if isinstance(frontiers, dict):
        for identity_digest, values in list(frontiers.items()):
            if not isinstance(values, list):
                frontiers.pop(identity_digest, None)
                continue
            filtered = [
                value for value in values
                if isinstance(value, str) and value in retained_ids
            ][-MAX_FRONTIER_PER_IDENTITY:]
            if filtered:
                frontiers[identity_digest] = filtered
            else:
                frontiers.pop(identity_digest, None)
    graph["node_compaction_count"] = (
        int(graph.get("node_compaction_count") or 0) + len(remove_ids)
    )
    _refresh_summary(graph)


def _partition_material(identity: tuple[str, ...] | list[str] | Any) -> tuple[str, ...]:
    """Return the owner/session partition for retention fairness.

    Identity keys include the claim key as their final component.  Partition
    by owner/session (or by the operator-global bucket) so independent keys
    from one owner cannot starve another owner.  Unknown legacy identities use
    a stable one-identity fallback rather than guessing an owner boundary.
    """

    if isinstance(identity, (tuple, list)) and identity:
        values = tuple(str(item) for item in identity)
        if values[0] == "tenant" and len(values) >= 3:
            return ("tenant", values[1], values[2])
        if values[0] == "operator_global":
            return ("operator_global",)
        if values[0] == "legacy_ownerless":
            return ("legacy_ownerless",)
    return ("legacy_identity", str(identity))


def _partition_digest(identity: tuple[str, ...] | list[str] | Any) -> str:
    return _digest(_partition_material(identity))


def _node_partition(node: dict[str, Any]) -> str:
    partition = node.get("partition_digest")
    if isinstance(partition, str) and _is_digest(partition):
        return partition
    # Graphs written before partition metadata existed remain valid.  They are
    # conservatively treated as one partition per identity until replaced by a
    # newly-ingested node carrying the owner partition.
    return _digest(("legacy_identity", str(node.get("identity_digest") or "")))


def _ensure_safety_partition_capacity(
    graph: dict[str, Any],
    node: dict[str, Any],
    relation_edges: list[dict[str, Any]],
) -> None:
    if not any(edge.get("relation") != "supports" for edge in relation_edges):
        return
    partition = _node_partition(node)
    safety_ids: set[str] = set()
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") == "supports":
            continue
        for field in ("from", "to"):
            value = edge.get(field)
            if isinstance(value, str):
                safety_ids.add(value)
    safety_nodes = {
        str(item.get("evidence_id")): item
        for item in graph.get("nodes", [])
        if isinstance(item, dict) and str(item.get("evidence_id")) in safety_ids
    }
    used = sum(_node_partition(item) == partition for item in safety_nodes.values())
    if used >= MAX_SAFETY_NODES_PER_PARTITION:
        raise EvidenceGraphError("evidence_graph_partition_safety_capacity_exhausted")


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
        "partition_digest": _partition_digest(identity),
        "identity": list(identity),
        "partition": list(_partition_material(identity)),
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
    conflict_refs: set[str] = set()
    has_supersession = False
    has_support = False
    for edge in graph["edges"]:
        if not isinstance(edge, dict):
            continue
        relation = edge.get("relation")
        if evidence_id not in {edge.get("from"), edge.get("to")}:
            continue
        other = edge.get("to") if edge.get("from") == evidence_id else edge.get("from")
        if relation == "contradicts" and isinstance(other, str):
            conflict_refs.add(other)
        elif relation == "supersedes":
            has_supersession = True
        elif relation == "supports":
            has_support = True
    if any(
        isinstance(item, dict)
        and evidence_id in {
            item.get("evidence_id"),
            item.get("related_evidence_id"),
        }
        for item in graph.get("unresolved", [])
    ):
        unresolved = True
    else:
        unresolved = False
    status = "unresolved" if conflict_refs or unresolved else "superseded" if has_supersession else "supported" if has_support else "new"
    return {
        "status": status,
        "evidence_refs": [evidence_id],
        "conflict_refs": sorted(conflict_refs),
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
        # Unresolved diagnostics are bounded retention data.  Safety relations
        # are represented by contradiction/supersession edges and remain
        # pinned; rotate the oldest diagnostic rather than allowing one noisy
        # owner to block unrelated observations.
        _compact_unresolved(graph, node)
    graph["unresolved"].append(
        {
            "unresolved_id": unresolved_id,
            "evidence_id": node.get("evidence_id"),
            "related_evidence_id": related or None,
            "reason": _bounded_text(reason, 120),
        }
    )


def _compact_unresolved(graph: dict[str, Any], incoming_node: dict[str, Any]) -> None:
    unresolved = [item for item in graph.get("unresolved", []) if isinstance(item, dict)]
    if not unresolved:
        return
    preferred_partition = _node_partition(incoming_node)
    node_by_id = {
        str(item.get("evidence_id")): item
        for item in graph.get("nodes", [])
        if isinstance(item, dict)
    }
    same_partition_index = next(
        (
            index
            for index, item in enumerate(unresolved)
            if _node_partition(node_by_id.get(str(item.get("evidence_id")), {})) == preferred_partition
        ),
        None,
    )
    remove_index = same_partition_index if same_partition_index is not None else 0
    graph["unresolved"] = unresolved[:remove_index] + unresolved[remove_index + 1 :]
    graph["unresolved_compaction_count"] = int(graph.get("unresolved_compaction_count") or 0) + 1


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
