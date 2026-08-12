from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from awareness.belief_economy import BeliefEconomyError, validate_economy
from awareness.claim_schema import (
    claim_identity_key,
    detect_conflicts,
    make_claim,
    refresh_claim_status,
)
from awareness.evidence_graph import (
    EvidenceGraphError,
    ingest_claim,
    validate_evidence_graph,
)
from core.context_scope import item_visible_to_scope
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


class BeliefCore:
    MAX_CLAIMS = 250
    # Keep a fair slice for each exact owner/session partition.  The graph has
    # its own bounded provenance policy; this claim projection must not let a
    # stream of unique owner-scoped keys evict every other owner's readable
    # value from the global 250-row view.
    MAX_CLAIMS_PER_PARTITION = MAX_CLAIMS
    _REFRESH_CAS_KEY = "_refresh_cas"

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def update_from_event(self, event: VeyraEvent) -> None:
        claim = make_claim(
            key=f"event:{event.event_id}",
            claim=f"received {event.type.value} from {event.source.channel}",
            confidence=1.0,
            source="event",
            ttl_seconds=300,
            evidence={
                "event_id": event.event_id,
                "channel": event.source.channel,
                "user_id": event.source.user_id,
                "session_id": event.source.session_id,
            },
        )
        claim.update(
            {
                "scope_kind": "tenant",
                "tenant_derived": True,
                "user_id": event.source.user_id,
                "session_id": event.source.session_id,
            }
        )
        self.upsert_claim(
            claim
        )

    def upsert_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        # StateRefresh carries this private envelope from its pre-probe
        # snapshot.  It is consumed by the durable writer below and must
        # never become part of the persisted claim projection.
        refresh_cas = (
            claim.get(self._REFRESH_CAS_KEY)
            if isinstance(claim.get(self._REFRESH_CAS_KEY), dict)
            else None
        )
        claim = {
            key: value
            for key, value in claim.items()
            if key != self._REFRESH_CAS_KEY
        }
        if claim.get("economy") is not None:
            try:
                claim = {
                    **claim,
                    "economy": validate_economy(claim.get("economy")),
                }
            except BeliefEconomyError as exc:
                return {
                    **claim,
                    "status": "rejected_belief_economy",
                    "persisted": False,
                    "belief_economy_error": str(exc),
                }
        claim = refresh_claim_status(claim)
        identity = claim_identity_key(claim)
        if identity is None:
            return self._reject_unscoped_claim(claim)

        result_claim: dict[str, Any] = {
            **claim,
            "persisted": False,
            "belief_value_persisted": False,
            "persistence_status": "pending",
        }

        def update_belief(belief: dict[str, Any]) -> dict[str, Any]:
            nonlocal result_claim
            existing_claim = next(
                (
                    item
                    for item in (belief.get("claims") or [])
                    if isinstance(item, dict) and claim_identity_key(item) == identity
                ),
                None,
            )
            if refresh_cas is None and isinstance(existing_claim, dict):
                raw_revision = existing_claim.get("claim_revision")
                if raw_revision is not None and (
                    isinstance(raw_revision, bool)
                    or not isinstance(raw_revision, int)
                    or raw_revision < 1
                ):
                    result_claim = {
                        **claim,
                        "persisted": False,
                        "belief_value_persisted": False,
                        "persistence_status": "rejected_claim_revision",
                        "status": "rejected_claim_revision",
                    }
                    # A malformed durable revision is not coerced or migrated
                    # by a normal observation writer.  Refresh CAS rejection
                    # below provides the more specific receipt when present.
                    return belief
            if refresh_cas is not None:
                cas_rejection = self._refresh_cas_rejection(
                    belief,
                    identity=identity,
                    expected=refresh_cas,
                )
                if cas_rejection is not None:
                    result_claim = {
                        **claim,
                        "persisted": False,
                        "belief_value_persisted": False,
                        "persistence_status": "cas_rejected",
                        "status": "rejected_cas",
                        "cas_rejected": True,
                        "cas": cas_rejection,
                    }
                    # Returning the unchanged document keeps the graph and
                    # claim projection untouched when a slow probe loses the
                    # compare-and-swap race.
                    return belief
            graph, evidence_node, graph_projection = ingest_claim(
                belief.get("evidence_graph"),
                claim,
                identity=identity,
            )
            graph_status = str(graph_projection.get("status") or "unresolved")
            result_claim = {
                **claim,
                "persisted": True,
                "belief_value_persisted": graph_status != "unresolved",
                "persistence_status": (
                    "accepted" if graph_status != "unresolved" else "conflict"
                ),
                "evidence_refs": self._merge_evidence_refs(
                    claim.get("evidence_refs"),
                    graph_projection.get("evidence_refs"),
                ),
                "evidence_graph_status": graph_status,
                "evidence_value_digest": evidence_node.get("value_digest"),
                "evidence_graph_duplicate": graph_projection.get("duplicate") is True,
            }
            conflict_refs = graph_projection.get("conflict_refs")
            if isinstance(conflict_refs, list) and conflict_refs:
                result_claim["evidence_graph_conflict_refs"] = sorted(
                    {str(item) for item in conflict_refs if str(item)}
                )
            # Exact evidence replay is a read of an already durable node. It
            # must not advance the Belief claim revision, append history, or
            # rewrite the state document. Contradiction/supersession edges
            # remain provenance, but replaying an endpoint never makes it a
            # new value winner.
            if graph_projection.get("duplicate") is True:
                current_claim = next(
                    (
                        item
                        for item in belief.get("claims", [])
                        if isinstance(item, dict)
                        and claim_identity_key(item) == identity
                    ),
                    None,
                )
                if graph_status == "superseded":
                    persistence_status = "superseded"
                    public_status = "superseded"
                elif graph_status == "unresolved":
                    persistence_status = "conflict"
                    public_status = "conflict"
                else:
                    persistence_status = "duplicate"
                    public_status = "duplicate"
                result_claim.update(
                    {
                        "persisted": True,
                        "belief_value_persisted": False,
                        "persistence_status": persistence_status,
                        "status": public_status,
                        "evidence_graph_status": graph_status,
                    }
                )
                if isinstance(current_claim, dict):
                    result_claim["claim_revision"] = current_claim.get(
                        "claim_revision"
                    )
                # Do not assign ``graph`` or touch claims/summary. This keeps
                # the serialized state byte-for-byte stable for a replay.
                return belief
            claims = belief.setdefault("claims", [])
            if not isinstance(claims, list):
                claims = []
            for index, current in enumerate(claims):
                if (
                    not isinstance(current, dict)
                    or claim_identity_key(current) != identity
                ):
                    continue
                unresolved = graph_status == "unresolved"
                current_status = str(current.get("status") or "")
                current_conflict = current_status == "conflict"
                stale_replacement = (
                    current_status in {"stale", "expired"}
                    and claim.get("refresh_mode") == "stale_claim"
                    # A durable contradiction must never be bypassed by a
                    # exact replay.  A genuinely new stale probe may replace
                    # the stale row while retaining the contradiction as
                    # reviewable history (the established refresh contract).
                    and not (
                        result_claim.get("evidence_graph_duplicate") is True
                        and bool(result_claim.get("evidence_graph_conflict_refs"))
                    )
                )
                history = current.get("history") if isinstance(current.get("history"), list) else []
                history.append(
                    {
                        "updated_at": current.get("updated_at") or current.get("observed_at"),
                        "status": current.get("status"),
                        "confidence": current.get("confidence"),
                    }
                )
                duplicate_superseded = (
                    result_claim.get("evidence_graph_duplicate") is True
                    and graph_status == "superseded"
                )
                if duplicate_superseded:
                    # Replaying an old superseded node is durable provenance,
                    # not a new authority signal.  Keep the current winner in
                    # the Belief projection and expose the non-success receipt
                    # so callers cannot accidentally resurrect the old value.
                    result_claim.update(
                        {
                            "status": "superseded",
                            "persistence_status": "superseded",
                            "belief_value_persisted": False,
                            "claim_revision": int(current.get("claim_revision") or 0) + 1,
                        }
                    )
                    claims[index] = {
                        **current,
                        "evidence_refs": self._merge_evidence_refs(
                            current.get("evidence_refs"),
                            result_claim.get("evidence_refs"),
                        ),
                        "history": history[-12:],
                        "refresh_count": int(current.get("refresh_count") or 0) + 1,
                        "claim_revision": int(current.get("claim_revision") or 0) + 1,
                    }
                elif (unresolved and not stale_replacement) or current_conflict:
                    # The graph has recorded the new observation, but it has
                    # not established that this value may replace the current
                    # belief.  Keep the existing value and retain a compact
                    # typed conflict record so the disagreement is durable.
                    conflicts = current.get("conflict_observations")
                    if not isinstance(conflicts, list):
                        conflicts = []
                    conflicts.append(
                        {
                            "key": claim.get("key"),
                            "source": claim.get("source"),
                            "observed_at": claim.get("observed_at"),
                            "value_digest": evidence_node.get("value_digest"),
                            "evidence_refs": result_claim.get("evidence_refs", []),
                            "conflict_refs": result_claim.get(
                                "evidence_graph_conflict_refs", []
                            ),
                            "evidence_graph_status": graph_status,
                        }
                    )
                    result_claim.update(
                        {
                            "status": "conflict",
                            "next_action": "refresh_probe",
                            "belief_value_persisted": False,
                            "persistence_status": "conflict",
                            "claim_revision": int(current.get("claim_revision") or 0) + 1,
                        }
                    )
                    claims[index] = {
                        **current,
                        "status": "conflict",
                        "next_action": "refresh_probe",
                        "evidence_refs": self._merge_evidence_refs(
                            current.get("evidence_refs"),
                            result_claim.get("evidence_refs"),
                        ),
                        "evidence_graph_status": "unresolved",
                        "evidence_graph_conflict_refs": sorted(
                            {
                                *(
                                    current.get("evidence_graph_conflict_refs")
                                    if isinstance(current.get("evidence_graph_conflict_refs"), list)
                                    else []
                                ),
                                *(
                                    result_claim.get("evidence_graph_conflict_refs")
                                    if isinstance(result_claim.get("evidence_graph_conflict_refs"), list)
                                    else []
                                ),
                            }
                        ),
                        "conflict_observations": conflicts[-12:],
                        "history": history[-12:],
                        "refresh_count": int(current.get("refresh_count") or 0) + 1,
                        "claim_revision": int(current.get("claim_revision") or 0) + 1,
                    }
                else:
                    next_claim_revision = int(current.get("claim_revision") or 0) + 1
                    accepted = {
                        **claim,
                        "evidence_graph_status": graph_status,
                        "claim_revision": next_claim_revision,
                    }
                    if result_claim.get("evidence_graph_conflict_refs"):
                        accepted["evidence_graph_conflict_refs"] = result_claim[
                            "evidence_graph_conflict_refs"
                        ]
                    if unresolved:
                        # A stale value is allowed to be replaced by a fresh
                        # probe result, while the graph still exposes the
                        # historical contradiction for review.
                        result_claim.update(
                            {
                                "belief_value_persisted": True,
                                "persistence_status": "accepted_with_conflict",
                            }
                        )
                    result_claim["claim_revision"] = next_claim_revision
                    claims[index] = {
                        **current,
                        **accepted,
                        "history": history[-12:],
                        "refresh_count": int(current.get("refresh_count") or 0) + 1,
                    }
                break
            else:
                accepted = {
                    **claim,
                    "evidence_graph_status": graph_status,
                    "refresh_count": 0,
                    "claim_revision": 1,
                }
                if graph_status == "unresolved":
                    accepted.update(
                        {
                            "evidence_refs": result_claim.get("evidence_refs", []),
                            "evidence_value_digest": evidence_node.get("value_digest"),
                        }
                    )
                    accepted.update(
                        {
                            "status": "conflict",
                            "next_action": "refresh_probe",
                            "conflict_observations": [
                                {
                                    "key": claim.get("key"),
                                    "source": claim.get("source"),
                                    "observed_at": claim.get("observed_at"),
                                    "value_digest": evidence_node.get("value_digest"),
                                    "evidence_refs": result_claim.get("evidence_refs", []),
                                    "evidence_graph_status": graph_status,
                                }
                            ],
                        }
                    )
                    result_claim.update(
                        {
                            "status": "conflict",
                            "next_action": "refresh_probe",
                            "belief_value_persisted": False,
                            "claim_revision": 1,
                        }
                    )
                else:
                    result_claim["claim_revision"] = 1
                claims.append(accepted)
            # Quarantined legacy rows are a read-only compatibility surface.
            # Updating another valid claim must not incidentally add lifecycle
            # fields to, migrate, or delete those rows.  Conflict evaluation is
            # therefore limited to claims with a resolvable identity, then
            # merged back into their original positions.
            valid_claims = [
                refresh_claim_status(item)
                for item in claims
                if isinstance(item, dict) and claim_identity_key(item) is not None
            ]
            evaluated_valid = iter(detect_conflicts(valid_claims))
            belief["claims"] = [
                next(evaluated_valid)
                if isinstance(item, dict) and claim_identity_key(item) is not None
                else item
                for item in claims
                if isinstance(item, dict)
            ]
            belief["claims"] = self._retain_claims(belief["claims"])
            belief["evidence_graph"] = graph
            belief["summary"] = self.summary_from_claims(belief["claims"])
            return belief

        try:
            self.state_store.mutate_json("belief_state.json", update_belief)
        except EvidenceGraphError as exc:
            return {
                **claim,
                "status": "rejected_evidence_graph",
                "persisted": False,
                "belief_value_persisted": False,
                "persistence_status": "rejected",
                "evidence_graph_error": str(exc),
            }
        # The writer receipt is useful to the caller, but the in-flight CAS
        # envelope is private transport metadata.  Strip it recursively before
        # any adapter, exception path, or direct caller can persist or expose
        # it.
        return self._strip_private_cas(result_claim)

    @classmethod
    def claim_projection_digest(cls, claim: dict[str, Any]) -> str:
        """Return a stable digest of the durable claim projection.

        Runtime-only receipts and read projections are excluded.  The digest
        deliberately retains observation timestamps and evidence so a newer
        same-value observation cannot be mistaken for the pre-probe claim.
        """

        volatile = {
            cls._REFRESH_CAS_KEY,
            "persisted",
            "belief_value_persisted",
            "persistence_status",
            "evidence_graph_duplicate",
            "age_seconds",
            "ttl_remaining_seconds",
        }
        material = {
            key: value
            for key, value in claim.items()
            if key not in volatile
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _strip_private_cas(cls, value: Any) -> Any:
        """Remove in-flight CAS envelopes from receipts and projections."""

        if isinstance(value, dict):
            return {
                key: cls._strip_private_cas(item)
                for key, item in value.items()
                if key not in {cls._REFRESH_CAS_KEY, "refresh_cas", "cas"}
            }
        if isinstance(value, list):
            return [cls._strip_private_cas(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._strip_private_cas(item) for item in value)
        return value

    @classmethod
    def claim_value_digest(
        cls,
        belief: dict[str, Any],
        claim: dict[str, Any],
        identity: tuple[str, ...] | None = None,
    ) -> str | None:
        """Resolve the exact durable value digest for a current claim.

        Older claim projections did not duplicate the EvidenceGraph value
        digest.  StateRefresh still needs a value-level CAS boundary, so use
        the graph's immutable identity/material rather than widening the
        public ``claims`` projection with a private field.
        """

        direct = claim.get("evidence_value_digest")
        if isinstance(direct, str) and direct:
            return direct
        bound_identity = identity or claim_identity_key(claim)
        if not bound_identity:
            return None
        graph = belief.get("evidence_graph")
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not isinstance(nodes, list):
            nodes = []
        identity_digest = hashlib.sha256(
            json.dumps(
                list(bound_identity),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        claim_key = str(claim.get("key") or claim.get("claim") or "")
        observed_at = str(
            claim.get("observed_at") or claim.get("updated_at") or ""
        )
        candidates: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            raw_identity = node.get("identity")
            same_identity = (
                isinstance(raw_identity, list)
                and tuple(str(item) for item in raw_identity) == bound_identity
            ) or node.get("identity_digest") == identity_digest
            if not same_identity:
                continue
            if claim_key and node.get("claim_key") not in {None, claim_key}:
                continue
            value_digest = node.get("value_digest")
            if not isinstance(value_digest, str) or not value_digest:
                continue
            candidates.append(node)
        if not candidates:
            # A legacy/fixture state may retain the claim projection while
            # omitting its graph (for example after an older state migration).
            # Recompute the same typed digest material used by EvidenceGraph;
            # this remains a value-level binding and still fails closed when
            # no canonical claim material is available.
            evidence = claim.get("evidence")
            if isinstance(evidence, dict):
                for field in ("dirty", "status", "value", "exists", "state", "phase"):
                    if field in evidence:
                        encoded = json.dumps(
                            {"field": field, "value": evidence[field]},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                        return hashlib.sha256(encoded).hexdigest()
            if claim.get("claim"):
                encoded = json.dumps(
                    {"claim": str(claim.get("claim") or "")},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()
            return None
        # Prefer the node whose observed timestamp matches this claim, then
        # the newest graph entry (nodes are append ordered).
        matching = [
            node
            for node in candidates
            if observed_at and node.get("observed_at") == observed_at
        ]
        node = (matching or candidates)[-1]
        value_digest = node.get("value_digest")
        return value_digest if isinstance(value_digest, str) and value_digest else None

    @classmethod
    def _refresh_cas_rejection(
        cls,
        belief: dict[str, Any],
        *,
        identity: tuple[str, ...],
        expected: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check a StateRefresh pre-probe binding inside the writer lock."""

        expected_identity = expected.get("identity")
        if not isinstance(expected_identity, (list, tuple)):
            return {"reason": "refresh_cas_identity_missing"}
        if tuple(str(item) for item in expected_identity) != tuple(identity):
            return {
                "reason": "refresh_cas_identity_mismatch",
                "expected_identity": [str(item) for item in expected_identity],
                "actual_identity": list(identity),
            }

        claims = belief.get("claims") if isinstance(belief.get("claims"), list) else []
        current = next(
            (
                item
                for item in claims
                if isinstance(item, dict) and claim_identity_key(item) == identity
            ),
            None,
        )
        if current is None:
            return {"reason": "refresh_cas_claim_missing"}

        # ``_state_revision`` advances for every Belief mutation, including
        # an unrelated owner/claim.  It is retained in the receipt as useful
        # provenance, but it is deliberately not a CAS precondition: a
        # refresh may commit when only another claim changed.  The exact
        # identity, claim revision, canonical projection digest and value
        # digest below are the claim-level compare-and-swap boundary.
        expected_state_revision = expected.get("belief_state_revision")
        if expected_state_revision is not None:
            try:
                int(expected_state_revision)
                int(belief.get("_state_revision") or 0)
            except (TypeError, ValueError):
                return {"reason": "refresh_cas_revision_malformed"}

        # ``claim_revision`` was added after the first durable Belief rows.
        # A missing legacy field is the zero revision and must not trigger a
        # migration merely because a refresh reads it.  Once present, however,
        # the field is strict: a non-null malformed value fails closed instead
        # of being coerced into a compare-and-swap token.
        expected_claim_revision = expected.get("claim_revision")
        if expected_claim_revision is None:
            return {"reason": "refresh_cas_claim_revision_missing"}
        if (
            isinstance(expected_claim_revision, bool)
            or not isinstance(expected_claim_revision, int)
            or expected_claim_revision < 0
        ):
            return {"reason": "refresh_cas_claim_revision_malformed"}
        raw_actual_revision = current.get("claim_revision")
        if raw_actual_revision is None:
            actual_claim_revision = 0
        elif (
            isinstance(raw_actual_revision, bool)
            or not isinstance(raw_actual_revision, int)
            or raw_actual_revision < 0
        ):
            return {"reason": "refresh_cas_claim_revision_malformed"}
        else:
            actual_claim_revision = raw_actual_revision
        if actual_claim_revision != expected_claim_revision:
            return {
                "reason": "refresh_cas_claim_revision_changed",
                "expected_claim_revision": expected_claim_revision,
                "actual_claim_revision": actual_claim_revision,
            }

        expected_digest = expected.get("claim_digest")
        if not isinstance(expected_digest, str) or not expected_digest:
            return {"reason": "refresh_cas_digest_missing"}
        actual_digest = cls.claim_projection_digest(current)
        if actual_digest != expected_digest:
            return {
                "reason": "refresh_cas_claim_digest_changed",
                "expected_claim_digest": expected_digest,
                "actual_claim_digest": actual_digest,
            }

        expected_value_digest = expected.get("value_digest")
        if not isinstance(expected_value_digest, str) or not expected_value_digest:
            return {"reason": "refresh_cas_value_digest_missing"}
        actual_value_digest = cls.claim_value_digest(belief, current, identity)
        if actual_value_digest != expected_value_digest:
            return {
                "reason": "refresh_cas_value_digest_changed",
                "expected_value_digest": expected_value_digest,
                "actual_value_digest": actual_value_digest,
            }
        return None

    def upsert_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.upsert_claim(claim) for claim in claims]

    MAX_REJECTION_SOURCES = 20

    def _reject_unscoped_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        """Refuse a claim that has no resolvable scope identity.

        ``claim_identity_key`` returns ``None`` for exactly the cases that
        ``item_visible_to_scope`` refuses to return: invalid owner, invalid
        scope kind, and tenant-scoped claims without an exact owner. Such a
        claim can never be read back and can never replace an existing entry,
        so appending it would only consume the bounded claim budget and evict
        usable claims. Rejecting is the fail-closed behaviour; the counters
        keep the rejection visible instead of silently dropping it.
        """

        source = str(claim.get("source") or "unknown")

        def record_rejection(belief: dict[str, Any]) -> dict[str, Any]:
            rejections = belief.get("unscoped_rejections")
            if not isinstance(rejections, dict):
                rejections = {}
            by_source = rejections.get("by_source")
            if not isinstance(by_source, dict):
                by_source = {}
            if source in by_source or len(by_source) < self.MAX_REJECTION_SOURCES:
                by_source[source] = int(by_source.get(source) or 0) + 1
            rejections["count"] = int(rejections.get("count") or 0) + 1
            rejections["by_source"] = by_source
            rejections["last_at"] = utc_now_iso()
            rejections["last_key"] = str(claim.get("key") or "")
            belief["unscoped_rejections"] = rejections
            return belief

        self.state_store.mutate_json("belief_state.json", record_rejection)
        return {**claim, "status": "rejected_unscoped", "persisted": False}

    @staticmethod
    def _merge_evidence_refs(*values: Any) -> list[str]:
        refs: set[str] = set()
        for value in values:
            if isinstance(value, str) and value:
                refs.add(value)
            elif isinstance(value, list):
                refs.update(
                    item
                    for item in value
                    if isinstance(item, str) and item
                )
        return sorted(refs)[-32:]

    @classmethod
    def _retain_claims(cls, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound the claim projection while retaining owner partitions fairly.

        The old ``claims[-250:]`` policy let an owner with many unique keys
        evict an older claim from every other owner.  Keep at most a compact
        partition slice, then select newest rows round-robin across partitions
        until the global cap is reached.  Conflict state remains inside each
        retained row and graph safety edges are independent of this view.
        """

        if len(claims) <= cls.MAX_CLAIMS:
            return claims
        groups: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
        for index, claim in enumerate(claims):
            identity = claim_identity_key(claim)
            if identity is None:
                partition = ("invalid",)
            elif identity[0] == "tenant" and len(identity) >= 3:
                partition = ("tenant", identity[1], identity[2])
            elif identity[0] == "operator_global":
                partition = ("operator_global",)
            else:
                partition = (str(identity[0]),)
            groups.setdefault(partition, []).append((index, claim))

        # Newest rows from each partition are considered in a round-robin
        # order.  This guarantees a small partition is not lost behind a large
        # stream while retaining temporal order in the final projection.
        ordered_groups = sorted(
            groups.values(),
            key=lambda values: values[-1][0],
            reverse=True,
        )
        selected: list[tuple[int, dict[str, Any]]] = []
        for depth in range(cls.MAX_CLAIMS_PER_PARTITION):
            for values in ordered_groups:
                offset = len(values) - 1 - depth
                if offset < 0:
                    continue
                selected.append(values[offset])
                if len(selected) >= cls.MAX_CLAIMS:
                    break
            if len(selected) >= cls.MAX_CLAIMS:
                break
        selected.sort(key=lambda item: item[0])
        return [claim for _, claim in selected]

    def refresh(
        self,
        *,
        expire_after_seconds: int | None = 3600,
        prune_expired_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not self._integrity_valid(self.state_store.read_json("belief_state.json")):
            return self._degraded_snapshot()
        try:
            return self.state_store.mutate_json(
                "belief_state.json",
                lambda belief: self._evaluated_state_or_raise(
                    belief,
                    expire_after_seconds=expire_after_seconds,
                    prune_expired_after_seconds=prune_expired_after_seconds,
                ),
            )
        except (EvidenceGraphError, TypeError, ValueError):
            return self._degraded_snapshot()

    def relevant_claims(
        self,
        focus: list[str],
        limit: int = 30,
        *,
        include_stale: bool = True,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        belief = self._snapshot()
        selected_limit = max(0, min(int(limit), 500))
        claims = belief.get("claims", [])
        if user_id is not None or session_id is not None:
            claims = [
                claim
                for claim in claims
                if item_visible_to_scope(
                    claim,
                    user_id=str(user_id or "").strip(),
                    session_id=str(session_id or "").strip(),
                )
            ]
        if not include_stale:
            claims = [claim for claim in claims if str(claim.get("status") or "fresh") in {"fresh", "conflict"}]
        if not focus:
            ranked = self._rank_claims(claims)
            return ranked[-selected_limit:] if selected_limit else []
        focused: list[dict[str, Any]] = []
        for claim in claims:
            haystack = " ".join(
                [
                    str(claim.get("key", "")),
                    str(claim.get("claim", "")),
                    str(claim.get("source", "")),
                ]
            )
            if any(item in haystack for item in focus):
                focused.append(claim)
        ranked = self._rank_claims(focused if focused else claims)
        return ranked[-selected_limit:] if selected_limit else []

    def ttl_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self._snapshot()
        selected_limit = max(0, min(int(limit), 500))
        claims = self._rank_claims(belief.get("claims", []))
        refreshable = [claim for claim in claims if claim.get("next_action") == "refresh_probe"]
        degraded = belief.get("status") == "degraded"
        report = {
            "status": "degraded" if degraded else "success",
            "summary": belief.get("summary", {}),
            "refreshable": refreshable[-selected_limit:] if selected_limit else [],
            "oldest": claims[:selected_limit],
            "newest": claims[-selected_limit:] if selected_limit else [],
        }
        if degraded:
            report["reason"] = str(
                belief.get("reason") or "belief_state_integrity_invalid"
            )
            if belief.get("quarantined_claim_count"):
                report["quarantined_claim_count"] = int(
                    belief["quarantined_claim_count"]
                )
        return report

    def stale_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self._snapshot()
        selected_limit = max(0, min(int(limit), 500))
        stale = [
            claim
            for claim in self._rank_claims(belief.get("claims", []))
            if claim.get("next_action") == "refresh_probe"
        ]
        degraded = belief.get("status") == "degraded"
        report = {
            "status": "degraded" if degraded else "success",
            "summary": belief.get("summary", {}),
            "items": stale[-selected_limit:] if selected_limit else [],
            "next_action": "refresh_probe" if stale else None,
        }
        if degraded:
            report["reason"] = str(
                belief.get("reason") or "belief_state_integrity_invalid"
            )
            if belief.get("quarantined_claim_count"):
                report["quarantined_claim_count"] = int(
                    belief["quarantined_claim_count"]
                )
        return report

    def _snapshot(
        self,
        *,
        expire_after_seconds: int | None = 3600,
        prune_expired_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        belief = self.state_store.read_json("belief_state.json")
        readable, quarantined = self._readable_projection(belief)
        if readable is None:
            return self._degraded_snapshot()
        evaluated = self._evaluated_state(
            readable,
            expire_after_seconds=expire_after_seconds,
            prune_expired_after_seconds=prune_expired_after_seconds,
        )
        if quarantined:
            # Historical ownerless tenant rows are neither migrated nor
            # deleted during a read. Keep valid partitions available while
            # making the isolated debt explicit to operators.
            evaluated["status"] = "degraded"
            evaluated["reason"] = "belief_claims_quarantined"
            evaluated["integrity_status"] = "degraded"
            evaluated["quarantined_claim_count"] = quarantined
        return evaluated

    def _evaluated_state_or_raise(
        self,
        belief: dict[str, Any],
        *,
        expire_after_seconds: int | None,
        prune_expired_after_seconds: int | None,
    ) -> dict[str, Any]:
        if not self._integrity_valid(belief):
            raise EvidenceGraphError("belief_state_integrity_invalid")
        return self._evaluated_state(
            belief,
            expire_after_seconds=expire_after_seconds,
            prune_expired_after_seconds=prune_expired_after_seconds,
        )

    @classmethod
    def _integrity_valid(cls, belief: Any) -> bool:
        """Validate durable claim identities and graph digests before reads."""

        readable, quarantined = cls._readable_projection(belief)
        return readable is not None and quarantined == 0

    @classmethod
    def _readable_projection(
        cls,
        belief: Any,
    ) -> tuple[dict[str, Any] | None, int]:
        """Return a read-only projection with invalid legacy rows isolated.

        One historical ownerless tenant claim must not erase every valid
        Belief from context. That narrow legacy row remains byte-for-byte in
        durable state and is reported as quarantined; malformed claims,
        ambiguous duplicate identities, and a corrupt EvidenceGraph still fail
        the whole projection closed.
        """

        if not isinstance(belief, dict):
            return None, 0
        claims = belief.get("claims")
        if not isinstance(claims, list) or len(claims) > cls.MAX_CLAIMS:
            return None, 0
        graph = belief.get("evidence_graph")
        if graph is not None:
            try:
                validate_evidence_graph(graph)
            except (EvidenceGraphError, TypeError, ValueError):
                return None, 0
        # Detect identity ambiguity before deciding whether any individual row
        # is safe to quarantine.  A malformed revision/digest does not make a
        # conflicting value for the same exact identity disappear; selecting
        # the other row would silently invent a winner.
        seen: set[tuple[str, ...]] = set()
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            identity = claim_identity_key(claim)
            if identity is None:
                # A conflicting nested owner envelope makes the canonical
                # identity parser reject the row.  Still derive its exact
                # top-level tenant identity for duplicate detection so that
                # quarantining the bad row can never select a valid peer with
                # the same owner/session/key as an implicit winner.
                key = str(claim.get("key") or claim.get("claim") or "").strip()
                user_id = claim.get("user_id")
                session_id = claim.get("session_id")
                if (
                    key
                    and claim.get("scope_kind") == "tenant"
                    and claim.get("tenant_derived") is True
                    and isinstance(user_id, str)
                    and user_id.strip()
                    and isinstance(session_id, str)
                    and session_id.strip()
                ):
                    identity = (
                        "tenant",
                        user_id.strip(),
                        session_id.strip(),
                        key,
                    )
            if identity is None:
                continue
            if identity in seen:
                return None, 0
            seen.add(identity)

        readable_claims: list[dict[str, Any]] = []
        quarantined = 0
        for claim in claims:
            if not isinstance(claim, dict):
                return None, quarantined
            identity = claim_identity_key(claim)
            if identity is None:
                # The only compatibility quarantine is the historical row
                # shape already present in the live checkout: a named,
                # tenant-derived claim whose owner and session were both
                # omitted by an older writer.  Partial owners, malformed scope
                # metadata, and nameless rows remain whole-document failures.
                legacy_ownerless_tenant = (
                    str(claim.get("key") or claim.get("claim") or "").strip()
                    and claim.get("scope_kind") == "tenant"
                    and claim.get("tenant_derived") is True
                    and claim.get("user_id") in (None, "")
                    and claim.get("session_id") in (None, "")
                )
                conflicting_tenant_envelope = (
                    str(claim.get("key") or claim.get("claim") or "").strip()
                    and claim.get("scope_kind") == "tenant"
                    and claim.get("tenant_derived") is True
                    and isinstance(claim.get("user_id"), str)
                    and bool(str(claim.get("user_id") or "").strip())
                    and isinstance(claim.get("session_id"), str)
                    and bool(str(claim.get("session_id") or "").strip())
                )
                if legacy_ownerless_tenant or conflicting_tenant_envelope:
                    quarantined += 1
                    continue
                return None, quarantined
            revision = claim.get("claim_revision")
            if revision is not None and (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                return None, quarantined
            value_digest = claim.get("evidence_value_digest")
            if value_digest is not None and (
                not isinstance(value_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", value_digest)
            ):
                return None, quarantined
            if identity[0] == "tenant" and not item_visible_to_scope(
                claim,
                user_id=identity[1],
                session_id=identity[2],
            ):
                # A legacy row can carry an exact top-level owner while a
                # nested evidence envelope names a different owner.  It is
                # never visible to either scope, but it need not erase other
                # valid partitions.  The full-batch duplicate scan above has
                # already made it impossible to quarantine one conflicting
                # duplicate and silently select its peer as the winner.
                quarantined += 1
                continue
            readable_claims.append(claim)
        projection = dict(belief)
        projection["claims"] = readable_claims
        return projection, quarantined

    @classmethod
    def _degraded_snapshot(cls) -> dict[str, Any]:
        return {
            "status": "degraded",
            "reason": "belief_state_integrity_invalid",
            "_state_corrupt": True,
            "integrity_status": "degraded",
            "claims": [],
            "summary": {
                "fresh": 0,
                "stale": 0,
                "expired": 0,
                "conflict": 0,
                "total": 0,
                "refreshable": 0,
                "by_source": {},
                "average_confidence": 0.0,
                "average_source_trust": 0.0,
            },
        }

    def _evaluated_state(
        self,
        belief: dict[str, Any],
        *,
        expire_after_seconds: int | None,
        prune_expired_after_seconds: int | None,
    ) -> dict[str, Any]:
        evaluated = dict(belief)
        raw_claims = belief.get("claims") if isinstance(belief.get("claims"), list) else []
        now = datetime.now(timezone.utc)
        claims = [
            refresh_claim_status(
                item,
                now=now,
                expire_after_seconds=expire_after_seconds,
            )
            for item in raw_claims
            if isinstance(item, dict)
        ]
        evaluated["claims"] = detect_conflicts(claims)
        if prune_expired_after_seconds is not None:
            evaluated["claims"] = self._prune_expired(
                evaluated["claims"],
                prune_expired_after_seconds,
            )
        evaluated["summary"] = self.summary_from_claims(evaluated["claims"])
        return evaluated

    def summary_from_claims(self, claims: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "fresh": 0,
            "stale": 0,
            "expired": 0,
            "conflict": 0,
            "total": len(claims),
            "refreshable": 0,
            "by_source": {},
            "average_confidence": 0.0,
            "average_source_trust": 0.0,
        }
        confidence_total = 0.0
        trust_total = 0.0
        for claim in claims:
            status = str(claim.get("status") or "fresh")
            if status in summary:
                summary[status] += 1
            if claim.get("next_action") == "refresh_probe":
                summary["refreshable"] += 1
            source = str(claim.get("source") or "unknown")
            by_source = summary["by_source"]
            by_source[source] = by_source.get(source, 0) + 1
            confidence_total += float(claim.get("confidence") or 0.0)
            trust_total += float(claim.get("source_trust") or 0.0)
        if claims:
            summary["average_confidence"] = round(confidence_total / len(claims), 3)
            summary["average_source_trust"] = round(trust_total / len(claims), 3)
        return summary

    def _rank_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(claim: dict[str, Any]) -> tuple[int, float, str]:
            status_order = {"expired": 0, "stale": 1, "conflict": 2, "fresh": 3}
            status = status_order.get(str(claim.get("status") or "fresh"), 1)
            confidence = float(claim.get("confidence") or 0.0) * float(claim.get("source_trust") or 0.5)
            return (status, confidence, str(claim.get("updated_at") or claim.get("observed_at") or ""))

        return sorted([claim for claim in claims if isinstance(claim, dict)], key=key)

    def _prune_expired(self, claims: list[dict[str, Any]], threshold_seconds: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        kept: list[dict[str, Any]] = []
        for claim in claims:
            if claim.get("status") != "expired":
                kept.append(claim)
                continue
            stale_since = str(claim.get("stale_since") or claim.get("expires_at") or "")
            try:
                parsed = datetime.fromisoformat(stale_since.replace("Z", "+00:00"))
            except ValueError:
                kept.append(claim)
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if (now - parsed).total_seconds() <= threshold_seconds:
                kept.append(claim)
        return kept
