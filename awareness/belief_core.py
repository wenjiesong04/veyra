from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from awareness.belief_economy import BeliefEconomyError, validate_economy
from awareness.claim_schema import (
    claim_identity_key,
    detect_conflicts,
    make_claim,
    refresh_claim_status,
)
from awareness.evidence_graph import EvidenceGraphError, ingest_claim
from core.context_scope import item_visible_to_scope
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


class BeliefCore:
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
            }
            conflict_refs = graph_projection.get("conflict_refs")
            if isinstance(conflict_refs, list) and conflict_refs:
                result_claim["evidence_graph_conflict_refs"] = sorted(
                    {str(item) for item in conflict_refs if str(item)}
                )
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
                )
                history = current.get("history") if isinstance(current.get("history"), list) else []
                history.append(
                    {
                        "updated_at": current.get("updated_at") or current.get("observed_at"),
                        "status": current.get("status"),
                        "confidence": current.get("confidence"),
                    }
                )
                if (unresolved and not stale_replacement) or current_conflict:
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
                    }
                else:
                    accepted = {
                        **claim,
                        "evidence_refs": result_claim.get("evidence_refs", []),
                        "evidence_graph_status": graph_status,
                        "evidence_value_digest": evidence_node.get("value_digest"),
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
                    "evidence_refs": result_claim.get("evidence_refs", []),
                    "evidence_graph_status": graph_status,
                    "evidence_value_digest": evidence_node.get("value_digest"),
                    "refresh_count": 0,
                }
                if graph_status == "unresolved":
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
                        }
                    )
                claims.append(accepted)
            belief["claims"] = detect_conflicts(
                [refresh_claim_status(item) for item in claims if isinstance(item, dict)]
            )[-250:]
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
        return result_claim

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

    def refresh(
        self,
        *,
        expire_after_seconds: int | None = 3600,
        prune_expired_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.state_store.mutate_json(
            "belief_state.json",
            lambda belief: self._evaluated_state(
                belief,
                expire_after_seconds=expire_after_seconds,
                prune_expired_after_seconds=prune_expired_after_seconds,
            ),
        )

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
            return self._rank_claims(claims)[-limit:]
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
        return ranked[-limit:]

    def ttl_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self._snapshot()
        claims = self._rank_claims(belief.get("claims", []))
        refreshable = [claim for claim in claims if claim.get("next_action") == "refresh_probe"]
        return {
            "status": "success",
            "summary": belief.get("summary", {}),
            "refreshable": refreshable[-limit:],
            "oldest": claims[:limit],
            "newest": claims[-limit:],
        }

    def stale_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self._snapshot()
        stale = [
            claim
            for claim in self._rank_claims(belief.get("claims", []))
            if str(claim.get("status") or "") in {"stale", "expired", "conflict"} or claim.get("next_action") == "refresh_probe"
        ]
        return {
            "status": "success",
            "summary": belief.get("summary", {}),
            "items": stale[-limit:],
            "next_action": "refresh_probe" if stale else None,
        }

    def _snapshot(
        self,
        *,
        expire_after_seconds: int | None = 3600,
        prune_expired_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self._evaluated_state(
            self.state_store.read_json("belief_state.json"),
            expire_after_seconds=expire_after_seconds,
            prune_expired_after_seconds=prune_expired_after_seconds,
        )

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
