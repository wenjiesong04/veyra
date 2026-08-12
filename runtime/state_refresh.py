from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awareness.belief_core import BeliefCore
from awareness.belief_economy import economy_value
from awareness.claim_schema import claim_identity_key, refresh_claim_status
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from awareness.refresh_spec import (
    REFRESH_RESOLVER_DEFAULT,
    REFRESH_RESOLVER_LITERAL,
    validate_refresh_spec,
)
from interface.event_schema import utc_now_iso
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from probes.time_probe import TimeProbe
from probes.web_probe import WebProbe


class StateRefresh:
    """Refreshes stale belief claims through known read-only probes."""

    MAX_OWNER_SCHEDULER_OWNERS = 256

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None, *, model_assist_enabled: bool = True) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        #: Probes whose observation is meaningless without an explicit target.
        #: Running them with an empty argument produces a `missing_target`
        #: result that describes the call, not the world.
        self.TARGET_REQUIRED_PROBES = frozenset(
            {"web_probe", "network_probe", "port_probe"}
        )
        self.probes = {
            "git_probe": GitProbe(),
            "hermes_probe": HermesProbe(),
            "mcp_probe": McpProbe(),
            "network_probe": NetworkProbe(),
            "openclaw_probe": OpenClawProbe(),
            "port_probe": PortProbe(),
            "process_probe": ProcessProbe(),
            "system_probe": SystemProbe(),
            "time_probe": TimeProbe(),
            "web_probe": WebProbe(),
        }

    def refresh_stale(self, limit: int = 20) -> dict[str, Any]:
        belief_state = self.state_store.read_json("belief_state.json")
        readable_belief, quarantined_claim_count = BeliefCore._readable_projection(
            belief_state
        )
        if readable_belief is None:
            # Integrity is a pre-probe boundary.  In particular, duplicate
            # exact identities and a corrupt EvidenceGraph must not be turned
            # into an arbitrary scheduler winner.  This return intentionally
            # avoids even reserving a scheduler cursor so belief/local/refresh
            # state remain byte-for-byte unchanged.
            return {
                "status": "degraded",
                "reason": "belief_state_integrity_invalid",
                "refreshed": [],
                "failed": [{"reason": "belief_state_integrity_invalid"}],
                "skipped": [],
                "malformed": [
                    {"reason": "belief_state_integrity_invalid"}
                ],
                "remaining_stale": 0,
                "unsupported_stale": 0,
                "selected_count": 0,
                "refreshed_count": 0,
                "failed_count": 1,
                "malformed_count": 1,
                "cursor": {"before": 0, "after": 0},
            }
        belief_state = readable_belief
        raw_claims = belief_state.get("claims")
        malformed: list[dict[str, Any]] = (
            [
                {
                    "reason": "belief_claims_quarantined",
                    "count": quarantined_claim_count,
                }
            ]
            if quarantined_claim_count
            else []
        )
        if not isinstance(raw_claims, list):
            raw_claims = []
            malformed.append(
                {
                    "reason": "belief_claim_batch_malformed",
                    "detail": "claims must be a list",
                }
            )
        now = datetime.now(timezone.utc)
        stale: list[dict[str, Any]] = []
        for claim in raw_claims:
            if not isinstance(claim, dict):
                malformed.append(
                    {
                        "reason": "belief_claim_malformed",
                        "detail": "claim must be an object",
                    }
                )
                continue
            if claim_identity_key(claim) is None:
                malformed.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "reason": "belief_claim_identity_malformed",
                    }
                )
                continue
            try:
                evaluated = refresh_claim_status(claim, now=now)
            except (TypeError, ValueError, OverflowError) as exc:
                malformed.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "reason": "belief_claim_status_malformed",
                        "detail": str(exc),
                    }
                )
                continue
            # Evaluate expiry against the current clock before selection.  A
            # claim still durably marked ``fresh`` therefore becomes eligible
            # as soon as its TTL elapses, without a GET-side lifecycle write.
            if (
                evaluated.get("status") in {"stale", "expired", "conflict"}
                and evaluated.get("next_action") == "refresh_probe"
            ):
                stale.append(evaluated)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 20
        supported = [claim for claim in stale if str(claim.get("source") or "") in self.probes]
        unsupported = [claim for claim in stale if str(claim.get("source") or "") not in self.probes]
        selected, cursor_before, cursor_after = self._select_fair_batch(supported, limit=limit)
        refreshed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        unresolvable: list[dict[str, Any]] = []
        for claim in selected:
            current_claim = self._current_claim(claim)
            if current_claim is None:
                failed.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "reason": "refresh_cas_claim_missing_before_probe",
                    }
                )
                continue
            source = str(current_claim.get("source") or "")
            probe = self.probes.get(source)
            if probe is None:
                continue
            target = self._target_for_claim(current_claim)
            if target is None:
                # Fail closed: a probe that needs a target must not run without
                # one. Guessing a target from the claim's own prose made failed
                # observations reproduce themselves once per tick.
                unresolvable.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "source": source,
                        "reason": "no_resolvable_refresh_target",
                    }
                )
                continue
            refresh_cas = self._refresh_cas_binding(current_claim)
            try:
                raw = probe.run(target)
            except Exception as exc:  # pragma: no cover - probe-specific failures
                failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "source": source,
                        "reason": "probe_error",
                        "detail": str(exc),
                    }
                )
                continue
            if not isinstance(raw, dict):
                failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "source": source,
                        "reason": "probe_result_malformed",
                    }
                )
                continue
            scope_fields = (
                "scope_kind",
                "tenant_derived",
                "user_id",
                "session_id",
            )
            # Scope belongs to the exact durable claim selected before the
            # probe. Remove any adapter-provided scope envelope first (even
            # when the durable claim is operator-global and therefore has no
            # owner fields), then copy only fields actually present on that
            # claim. This prevents a probe echo from creating or leaking a
            # different owner/session binding.
            raw = {
                key: value
                for key, value in raw.items()
                if key not in scope_fields
            }
            raw.update(
                {
                    key: current_claim.get(key)
                    for key in scope_fields
                    if key in current_claim
                }
            )
            raw.update(
                {
                    "refresh_mode": "stale_claim",
                    "refresh_cas": refresh_cas,
                }
            )
            try:
                patch = self.perception.interpret_probe_result(raw)
            except Exception as exc:  # pragma: no cover - adapter-specific failures
                failed.append(
                    {
                        "claim": current_claim.get("key") or current_claim.get("claim"),
                        "probe_result": PerceptionLayer._strip_refresh_cas(raw),
                        "reason": "persistence_error",
                        # Exception text can echo adapter arguments or a
                        # private CAS envelope; expose only its stable type.
                        "detail": type(exc).__name__,
                    }
                )
                continue
            receipt_raw = {
                key: value
                for key, value in raw.items()
                if key != "refresh_cas"
            }
            # Adapters and persistence exceptions may nest the in-flight CAS
            # envelope below another field. Refresh receipts are public
            # diagnostic output, so strip the private transport recursively
            # before retaining either success or failure evidence.
            receipt_raw = PerceptionLayer._strip_refresh_cas(receipt_raw)
            persistence = patch.get("belief_persistence") if isinstance(patch, dict) else None
            persistence_status = (
                str(persistence.get("status") or "")
                if isinstance(persistence, dict)
                else ""
            )
            entry = {
                "claim": current_claim.get("key") or current_claim.get("claim"),
                "probe_result": receipt_raw,
                "state_patch": PerceptionLayer._strip_refresh_cas(patch),
            }
            if persistence is None:
                # Keep compatibility with bounded test doubles and older
                # perception adapters, which returned an explicit accepted
                # status without the detailed persistence envelope.
                if isinstance(patch, dict) and patch.get("status") in {"accepted", "success"}:
                    refreshed.append(entry)
                else:
                    failed.append(
                        {
                            **entry,
                            "reason": f"belief_persistence_{patch.get('status') if isinstance(patch, dict) else 'malformed'}",
                        }
                    )
            elif self._valid_persistence_receipt(persistence):
                refreshed.append(entry)
            else:
                # A probe can be successful while its observation is
                # conflicted or rejected.  It must not be counted as a
                # successful refresh until the Belief value is accepted.
                receipt_statuses = {
                    str(item.get("persistence_status") or "")
                    for item in (persistence.get("results") or [])
                    if isinstance(item, dict)
                }
                failure_status = (
                    "cas_rejected"
                    if "cas_rejected" in receipt_statuses
                    else persistence_status or "malformed"
                )
                failed.append(
                    {
                        **entry,
                        "reason": f"belief_persistence_{failure_status}",
                    }
                )
        skipped = [
            {
                "claim": claim.get("key") or claim.get("claim"),
                "source": claim.get("source"),
                "reason": f"no probe for source {claim.get('source')}",
            }
            for claim in unsupported[:limit]
        ] + unresolvable
        skipped_items = skipped + malformed
        def record_refresh_batch(state: dict[str, Any]) -> None:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "supported_count": len(supported),
                    "unsupported_count": len(unsupported),
                    "malformed_count": len(malformed),
                    "selected_count": len(selected),
                    "refreshed_count": len(refreshed),
                    "failed_count": len(failed),
                    "skipped_count": len(skipped_items),
                    "last_selected": [
                        str(claim.get("key") or claim.get("claim") or "")
                        for claim in selected
                    ],
                }
            )

        self.state_store.mutate_json("state_refresh_state.json", record_refresh_batch)
        if failed or malformed:
            status = "degraded"
        elif refreshed:
            status = "success"
        elif stale or skipped:
            # There was refresh work, but none produced an accepted value.
            # This is deliberately distinct from a successful no-op.
            status = "skipped"
        else:
            status = "idle"
        return {
            "status": status,
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped_items,
            "malformed": malformed,
            "remaining_stale": max(0, len(supported) - len(refreshed)),
            "unsupported_stale": len(unsupported),
            "selected_count": len(selected),
            "refreshed_count": len(refreshed),
            "cursor": {"before": cursor_before, "after": cursor_after},
        }

    def _current_claim(self, selected: dict[str, Any]) -> dict[str, Any] | None:
        """Read the exact claim that will be bound to the probe."""

        identity = claim_identity_key(selected)
        if identity is None:
            return None
        current = self.state_store.read_json("belief_state.json")
        claims = current.get("claims") if isinstance(current.get("claims"), list) else []
        for claim in claims:
            if isinstance(claim, dict) and claim_identity_key(claim) == identity:
                return claim
        return None

    def _refresh_cas_binding(self, claim: dict[str, Any]) -> dict[str, Any]:
        belief = self.state_store.read_json("belief_state.json")
        identity = claim_identity_key(claim)
        if identity is None:
            # This is never dispatched by a valid selected claim, but keeping
            # a typed malformed binding makes the writer reject it honestly.
            identity = ("invalid",)
        return {
            "identity": list(identity),
            "claim_key": str(claim.get("key") or claim.get("claim") or ""),
            "belief_state_revision": int(belief.get("_state_revision") or 0),
            # Legacy rows predate the claim-level CAS field.  Treat a
            # genuinely missing field as revision zero without mutating the
            # row during this read; explicit malformed values stay visible to
            # the writer and fail closed there.
            "claim_revision": (
                claim.get("claim_revision")
                if claim.get("claim_revision") is not None
                else 0
            ),
            "claim_digest": BeliefCore.claim_projection_digest(claim),
            "value_digest": BeliefCore.claim_value_digest(belief, claim, identity),
        }

    @staticmethod
    def _valid_persistence_receipt(persistence: Any) -> bool:
        if not isinstance(persistence, dict):
            return False
        status = str(persistence.get("status") or "")
        if status not in {"accepted", "accepted_with_conflict"}:
            return False
        results = persistence.get("results")
        if not isinstance(results, list) or not results:
            return False
        accepted_results = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("belief_value_persisted") is True
            and item.get("persistence_status") in {"accepted", "accepted_with_conflict"}
        ]
        try:
            accepted_count = int(persistence.get("accepted_count") or 0)
            conflicted_count = int(persistence.get("conflicted_count") or 0)
            rejected_count = int(persistence.get("rejected_count") or 0)
        except (TypeError, ValueError):
            return False
        if accepted_count != len(accepted_results) or accepted_count < 1:
            return False
        if min(conflicted_count, rejected_count) < 0:
            return False
        if accepted_count + conflicted_count + rejected_count != len(results):
            return False
        return True

    def _select_fair_batch(self, claims: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], int, int]:
        if not claims:
            self.state_store.mutate_json(
                "state_refresh_state.json",
                lambda state: {
                    **state,
                    "cursor": 0,
                    "owner_count": 0,
                    "updated_at": utc_now_iso(),
                },
            )
            return [], 0, 0
        ordered = sorted(claims, key=self._refresh_sort_key)
        # A corrupted/legacy document can contain the same exact identity more
        # than once.  Reserve at most one occurrence per refresh batch so a
        # duplicate row cannot consume the owner's fair slot twice.
        unique: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for claim in ordered:
            identity = claim_identity_key(claim)
            marker: tuple[Any, ...]
            if identity is not None:
                marker = ("identity", *identity)
            else:
                marker = (
                    "fallback",
                    self._owner_key(claim),
                    str(claim.get("key") or claim.get("claim") or ""),
                )
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(claim)
        ordered = unique
        count = min(limit, len(ordered))
        groups: dict[str, list[dict[str, Any]]] = {}
        for claim in ordered:
            groups.setdefault(self._owner_key(claim), []).append(claim)
        owner_keys = sorted(groups)
        selected: list[dict[str, Any]] = []
        cursor_before = 0
        cursor_after = 0

        def reserve_batch(state: dict[str, Any]) -> None:
            nonlocal cursor_before, cursor_after
            try:
                legacy_cursor = int(state.get("cursor") or 0) % len(ordered)
            except (TypeError, ValueError):
                legacy_cursor = 0
            offsets = state.get("owner_offsets")
            if not isinstance(offsets, dict):
                offsets = {}
            offsets = {
                str(key): value
                for key, value in offsets.items()
                if str(key) in groups
            }
            last_owner = str(state.get("last_owner") or "")
            if last_owner:
                owner_start = bisect_right(owner_keys, last_owner) % len(owner_keys)
            else:
                owner_start = 0

            # Preserve the old flat cursor for a single owner.  It keeps
            # existing callers' cursor evidence stable while the owner-level
            # round-robin below prevents list churn from starving partitions.
            if len(owner_keys) == 1:
                owner = owner_keys[0]
                try:
                    cursor_before = int(offsets.get(owner, legacy_cursor)) % len(ordered)
                except (TypeError, ValueError):
                    cursor_before = legacy_cursor
                offset = cursor_before % len(groups[owner])
                for _ in range(count):
                    selected.append(groups[owner][offset])
                    offset = (offset + 1) % len(groups[owner])
                offsets[owner] = offset
                cursor_after = (cursor_before + count) % len(ordered)
                last_owner = owner
            else:
                cursor_before = legacy_cursor
                selected_markers: set[tuple[Any, ...]] = set()
                rounds = 0
                while len(selected) < count and rounds < count + len(owner_keys):
                    progressed = False
                    for step in range(len(owner_keys)):
                        owner = owner_keys[(owner_start + step) % len(owner_keys)]
                        values = groups[owner]
                        try:
                            offset = int(offsets.get(owner, 0)) % len(values)
                        except (TypeError, ValueError):
                            offset = 0
                        # A small owner partition must not be selected again
                        # merely because the requested batch is larger than
                        # that partition.  Walk its bounded slice until an
                        # identity not already reserved by this batch is
                        # found; if every row is already selected, leave the
                        # owner for the next refresh tick.
                        candidate = None
                        for _ in range(len(values)):
                            value = values[offset]
                            identity = claim_identity_key(value)
                            marker = (
                                ("identity", *identity)
                                if identity is not None
                                else (
                                    "fallback",
                                    owner,
                                    str(value.get("key") or value.get("claim") or ""),
                                )
                            )
                            if marker not in selected_markers:
                                candidate = (value, marker, offset)
                                break
                            offset = (offset + 1) % len(values)
                        if candidate is None:
                            continue
                        value, marker, selected_offset = candidate
                        selected.append(value)
                        selected_markers.add(marker)
                        offsets[owner] = (selected_offset + 1) % len(values)
                        last_owner = owner
                        progressed = True
                        if len(selected) >= count:
                            break
                    if not progressed:
                        break
                    rounds += 1
                if selected:
                    last_index = ordered.index(selected[-1])
                    cursor_after = (last_index + 1) % len(ordered)
                else:
                    cursor_after = cursor_before

            # Scheduler state is bounded even if malformed state contains an
            # unbounded stream of owner keys.  Current owners remain eligible;
            # deterministic lexical order chooses the retained tail if needed.
            if len(offsets) > self.MAX_OWNER_SCHEDULER_OWNERS:
                keep = sorted(offsets)[-self.MAX_OWNER_SCHEDULER_OWNERS :]
                offsets = {key: offsets[key] for key in keep}
            state["cursor"] = cursor_after
            state["last_owner"] = last_owner
            state["owner_offsets"] = offsets
            state["owner_count"] = len(owner_keys)
            state["owner_scheduler_version"] = 1
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("state_refresh_state.json", reserve_batch)
        return selected, cursor_before, cursor_after

    @staticmethod
    def _status_priority(claim: dict[str, Any]) -> int:
        # Hard stale/expired/conflict obligations are ordered before a merely
        # old claim.  The caller already filters to refreshable claims, but a
        # deterministic rank keeps malformed status values fail-closed.
        return {
            "expired": 0,
            "conflict": 1,
            "stale": 2,
        }.get(str(claim.get("status") or ""), 3)

    @classmethod
    def _refresh_sort_key(cls, claim: dict[str, Any]) -> tuple[Any, ...]:
        value = economy_value(claim.get("economy"))
        return (
            cls._status_priority(claim),
            0 if value is not None else 1,
            -float(value or 0.0),
            str(claim.get("updated_at") or claim.get("observed_at") or ""),
            cls._owner_key(claim),
            str(claim.get("key") or claim.get("claim") or ""),
        )

    @staticmethod
    def _owner_key(claim: dict[str, Any]) -> str:
        raw_user = claim.get("user_id")
        raw_session = claim.get("session_id")
        user = str(raw_user) if raw_user is not None else ""
        session = str(raw_session) if raw_session is not None else ""
        # Length prefixes avoid collisions when a principal itself contains
        # the delimiter.  This is an exact owner/session key, not a hash or a
        # lossy display label.
        user_presence = "present" if user else "missing"
        session_presence = "present" if session else "missing"
        return f"{user_presence}:{len(user)}:{user}|{session_presence}:{len(session)}:{session}"

    def _target_for_claim(self, claim: dict[str, Any]) -> str | None:
        """Resolve a structured refresh target, or None when none is available.

        Returning None means "do not probe". A claim's human-readable text is
        not a probe argument: passing it back made a failed observation
        reproduce itself, because the summary "Web probe needs an http or https
        URL." was handed to WebProbe as the URL.

        This is the narrow form of the refresh hygiene rule; the full
        ``refresh_spec`` (probe_kind + target_ref + resolver_id) is a later
        slice. Until then, probes that need a target fail closed, and probes
        that need none keep receiving an empty string.
        """

        source = str(claim.get("source") or "")
        if "refresh_spec" in claim:
            try:
                spec = validate_refresh_spec(claim.get("refresh_spec"), source=source)
            except ValueError:
                return None
            if spec["resolver_id"] == REFRESH_RESOLVER_LITERAL:
                target = str(spec["target_ref"]).strip()
                return target if source not in self.TARGET_REQUIRED_PROBES or target else None
            if spec["resolver_id"] == REFRESH_RESOLVER_DEFAULT:
                return None if source in self.TARGET_REQUIRED_PROBES else ""
            return None

        evidence = claim.get("evidence") if isinstance(claim.get("evidence"), dict) else {}
        for key in ("target", "url", "host", "path"):
            if evidence.get(key) is not None:
                target = str(evidence[key]).strip()
                if not target:
                    continue
                return target if source not in self.TARGET_REQUIRED_PROBES else target
        details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
        for key in ("target", "url", "host", "path", "port"):
            if details.get(key) is not None:
                value = str(details[key]).strip()
                if not value:
                    continue
                if key == "path" and not Path(value).exists():
                    return None
                return value if source not in self.TARGET_REQUIRED_PROBES else value
        if source in self.TARGET_REQUIRED_PROBES:
            return None
        return ""
