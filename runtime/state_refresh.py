from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awareness.belief_core import BeliefCore
from awareness.belief_economy import economy_value, evaluate_refresh_schedule
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
from runtime.belief_refresh_scheduler import (
    MAX_CONSUMED_DUE,
    MAX_CONFLICT_RETRY,
    MAX_UNREFRESHABLE,
    SCHEDULER_SCHEMA_VERSION,
    merge_bounded_ledger,
    schedule_marker,
    select_fair,
    unrefreshable_entry,
    unrefreshable_marker,
    validate_bounded_ledger,
    validate_refresh_state,
)
from runtime.belief_refresh_execution import (
    RefreshExecutionContext,
    execute_refresh_batch,
)
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

    def refresh_stale(self, limit: int = 20, *, now: datetime | None = None) -> dict[str, Any]:
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
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        stale: list[dict[str, Any]] = []
        schedule_malformed: list[dict[str, Any]] = []
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
                schedule = evaluate_refresh_schedule(claim, now=now)
            except (TypeError, ValueError, OverflowError) as exc:
                malformed.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "reason": "belief_claim_status_malformed",
                        "detail": str(exc),
                    }
                )
                continue
            # Legacy unsupported rows historically carried only status and
            # next_action. Preserve their terminal skip semantics without
            # inventing an age/deadline; the pure evaluator itself remains
            # strict for new callers.
            if (
                not schedule.get("valid")
                and schedule.get("reason") in {"observed_at_invalid", "lifecycle_invalid"}
                and str(claim.get("status") or "") in {"stale", "expired", "conflict"}
                and not claim.get("economy")
            ):
                schedule = {
                    "valid": True,
                    "eligible": True,
                    "tier": "lifecycle",
                    "tier_rank": 1,
                    "hard_overdue": False,
                    "economy_value": None,
                    "economy_known": False,
                    "age_seconds": 0,
                }
            if not schedule.get("valid"):
                schedule_malformed.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "reason": str(schedule.get("reason") or "schedule_invalid"),
                    }
                )
                continue
            # Current-clock TTL/lifecycle failures are always eligible, even
            # when a producer supplied a future next_refresh_at.
            if schedule.get("eligible"):
                evaluated["_refresh_schedule"] = schedule
                # Keep the exact durable row observed during admission. The
                # lifecycle evaluator adds runtime-only fields (such as
                # stale_since/next_action), so its projection is not a CAS
                # representation. This private copy never enters a receipt
                # or persistence payload.
                evaluated["_refresh_original_claim"] = copy.deepcopy(claim)
                stale.append(evaluated)
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 20
        # A batch containing only malformed schedule/economy claims must not
        # reserve a cursor or rewrite scheduler state. This keeps the
        # fail-closed admission boundary byte-pure while allowing a mixed
        # batch to continue with independently valid claims below.
        if schedule_malformed and not stale:
            return {
                "status": "degraded",
                "refreshed": [],
                "failed": [],
                "skipped": [],
                "malformed": malformed + schedule_malformed,
                "remaining_stale": 0,
                "unsupported_stale": 0,
                "selected_count": 0,
                "refreshed_count": 0,
                "cursor": {"before": 0, "after": 0},
            }
        if limit == 0:
            # Strict zero-budget contract: do not read/mutate scheduler state,
            # dispatch probes, archive, consume, or advance any cursor.
            unsupported_stale = sum(
                1
                for item in stale
                if str(item.get("source") or "") not in self.probes
            )
            return {
                "status": "idle" if not stale else "skipped",
                "refreshed": [],
                "failed": [],
                "skipped": [],
                "malformed": malformed + schedule_malformed,
                "remaining_stale": len(stale),
                "unsupported_stale": unsupported_stale,
                "selected_count": 0,
                "refreshed_count": 0,
                "cursor": {"before": 0, "after": 0},
            }
        supported = [claim for claim in stale if str(claim.get("source") or "") in self.probes]
        unsupported = [claim for claim in stale if str(claim.get("source") or "") not in self.probes]
        refresh_state = self.state_store.read_json("state_refresh_state.json")
        state_error = validate_refresh_state(refresh_state)
        if state_error:
            return {
                "status": "degraded",
                "reason": state_error,
                "refreshed": [],
                "failed": [{"reason": state_error}],
                "skipped": [],
                "malformed": [],
                "remaining_stale": len(stale),
                "unsupported_stale": len(unsupported),
                "selected_count": 0,
                "refreshed_count": 0,
                "cursor": {"before": 0, "after": 0},
            }
        ledger_error = (
            validate_bounded_ledger(refresh_state.get("consumed_due"), limit=MAX_CONSUMED_DUE)
            or validate_bounded_ledger(refresh_state.get("unrefreshable"), limit=MAX_UNREFRESHABLE)
            or validate_bounded_ledger(refresh_state.get("conflict_retry"), limit=MAX_CONFLICT_RETRY)
        )
        if ledger_error:
            return {
                "status": "degraded",
                "reason": ledger_error,
                "refreshed": [],
                "failed": [{"reason": ledger_error}],
                "skipped": [],
                "malformed": [],
                "remaining_stale": len(stale),
                "unsupported_stale": 0,
                "selected_count": 0,
                "refreshed_count": 0,
                "cursor": {"before": 0, "after": 0},
            }
        archived_markers = {
            str(item.get("marker"))
            for item in (refresh_state.get("unrefreshable") or [])
            if isinstance(item, dict) and item.get("marker")
        }
        consumed_markers = {
            str(item.get("marker"))
            for item in (refresh_state.get("consumed_due") or [])
            if isinstance(item, dict) and item.get("marker")
        }
        conflict_markers = {
            str(item.get("marker"))
            for item in (refresh_state.get("conflict_retry") or [])
            if isinstance(item, dict) and item.get("marker")
        }
        archived_skipped: list[dict[str, Any]] = []
        selectable: list[dict[str, Any]] = []
        pre_archived: list[dict[str, Any]] = []
        pre_failed: list[dict[str, Any]] = []
        conflict_retry: list[dict[str, Any]] = []
        unsupported_hard_pending: list[dict[str, Any]] = []
        unsupported_conflict_pending: list[dict[str, Any]] = []
        unsupported_ordinary_pending: list[dict[str, Any]] = []
        for claim in stale:
            schedule = claim.get("_refresh_schedule") or {}
            durable_claim = claim.get("_refresh_original_claim")
            if not isinstance(durable_claim, dict):
                durable_claim = claim
            marker = unrefreshable_marker(durable_claim)
            if not schedule.get("hard_overdue") and marker in archived_markers:
                archived_skipped.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "source": claim.get("source"),
                        "reason": "unrefreshable_archived",
                    }
                )
                continue
            consumed_marker = schedule_marker(claim)
            if not schedule.get("hard_overdue") and consumed_marker in consumed_markers:
                archived_skipped.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "source": claim.get("source"),
                        "reason": "due_already_consumed",
                    }
                )
                continue
            if (
                str(claim.get("status") or "") == "conflict"
                and not schedule.get("next_refresh_at")
                and not schedule.get("hard_overdue")
                and marker in conflict_markers
            ):
                archived_skipped.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "source": claim.get("source"),
                        "reason": "conflict_retry_suppressed",
                    }
                )
                continue
            if str(claim.get("source") or "") not in self.probes:
                if schedule.get("hard_overdue"):
                    unsupported_hard_pending.append(claim)
                elif str(claim.get("status") or "") == "conflict":
                    unsupported_conflict_pending.append(claim)
                elif str(claim.get("status") or "") != "conflict":
                    unsupported_ordinary_pending.append(claim)
                continue
            selectable.append(claim)
        # Unsupported obligations still consume the same bounded batch budget
        # as probes. Hard markers have precedence; ordinary dispositions are
        # admitted only with remaining capacity.
        unsupported_hard_pending.sort(key=lambda item: (item.get("_refresh_schedule") or {}).get("tier_rank", 3))
        for claim in unsupported_hard_pending[:limit]:
            pre_failed.append(
                {
                    "claim": claim.get("key") or claim.get("claim"),
                    "source": claim.get("source"),
                    "reason": "hard_overdue_unrefreshable",
                }
            )
        remaining_budget = max(0, limit - len(pre_failed))
        selected, cursor_before, cursor_after = self._select_fair_batch(selectable, limit=remaining_budget)
        disposition_budget = max(0, remaining_budget - len(selected))
        for claim in unsupported_conflict_pending[:disposition_budget]:
            pre_archived.append(
                unrefreshable_entry(
                    claim,
                    reason="unsupported_conflict_needs_review",
                    now=now,
                )
            )
        disposition_budget = max(0, disposition_budget - len(unsupported_conflict_pending[:disposition_budget]))
        for claim in unsupported_ordinary_pending[:disposition_budget]:
            pre_archived.append(
                unrefreshable_entry(
                    claim,
                    reason="unsupported_source",
                    now=now,
                )
            )
        execution = execute_refresh_batch(
            RefreshExecutionContext(
                selected=selected,
                now=now,
                probes=self.probes,
                current_claim=self._current_claim,
                target_for_claim=self._target_for_claim,
                refresh_cas_binding=self._refresh_cas_binding,
                interpret_probe_result=self.perception.interpret_probe_result,
                valid_persistence_receipt=self._valid_persistence_receipt,
            )
        )
        refreshed = execution.refreshed
        failed = list(pre_failed) + execution.failed
        unresolvable = execution.unresolvable
        consumed = execution.consumed
        unrefreshable = list(pre_archived) + execution.unrefreshable
        conflict_retry = execution.conflict_retry
        admitted_unsupported = pre_archived
        skipped = [
            {
                "claim": item.get("identity"),
                "source": "unsupported",
                "reason": str(item.get("reason") or "unsupported_source"),
            }
            for item in admitted_unsupported
        ] + unresolvable + archived_skipped
        skipped_items = skipped + malformed + schedule_malformed
        def record_refresh_batch(state: dict[str, Any]) -> None:
            state.update(
                {
                    "schema_version": SCHEDULER_SCHEMA_VERSION,
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
            # Upgrade sparse v1 defaults in the same mutation that first
            # records a v2 ledger. Unsupported-only/empty batches otherwise
            # leave required owner fields absent and fail their next tick.
            if not isinstance(state.get("owner_offsets"), dict):
                state["owner_offsets"] = {}
            state.setdefault("owner_count", 0)
            state["owner_scheduler_version"] = 2
            state.setdefault("last_owner", "")
            consumed_items, consumed_blocked = merge_bounded_ledger(
                state.get("consumed_due"), consumed, limit=MAX_CONSUMED_DUE
            )
            archived_items, archive_blocked = merge_bounded_ledger(
                state.get("unrefreshable"), unrefreshable, limit=MAX_UNREFRESHABLE
            )
            conflict_items, conflict_blocked = merge_bounded_ledger(
                state.get("conflict_retry"), conflict_retry, limit=MAX_CONFLICT_RETRY
            )
            # Capacity pressure is observable and fail-closed. Existing hard
            # markers are never silently discarded by this bounded ledger.
            state["consumed_due"] = consumed_items
            state["unrefreshable"] = archived_items
            state["ledger_capacity"] = {
                "consumed_due_blocked": consumed_blocked,
                "unrefreshable_blocked": archive_blocked,
                "conflict_retry_blocked": conflict_blocked,
            }
            state["conflict_retry"] = conflict_items
            state_error = validate_refresh_state(state)
            ledger_error = (
                validate_bounded_ledger(state.get("consumed_due"), limit=MAX_CONSUMED_DUE)
                or validate_bounded_ledger(state.get("unrefreshable"), limit=MAX_UNREFRESHABLE)
                or validate_bounded_ledger(state.get("conflict_retry"), limit=MAX_CONFLICT_RETRY)
            )
            if state_error or ledger_error:
                raise ValueError(state_error or ledger_error)

        try:
            persisted_refresh_state = self.state_store.mutate_json(
                "state_refresh_state.json", record_refresh_batch
            )
        except ValueError as exc:
            return {
                "status": "degraded",
                "reason": "bounded_ledger_invalid",
                "refreshed": [],
                "failed": [{"reason": "bounded_ledger_invalid", "detail": type(exc).__name__}],
                "skipped": [],
                "malformed": malformed + schedule_malformed,
                "remaining_stale": len(stale),
                "unsupported_stale": len(unsupported),
                "selected_count": len(selected),
                "refreshed_count": 0,
                "cursor": {"before": cursor_before, "after": cursor_after},
            }
        malformed = malformed + schedule_malformed
        persisted_capacity = persisted_refresh_state.get("ledger_capacity") or {}
        capacity_blocked = any(
            int(value or 0) > 0
            for value in persisted_capacity.values()
            if isinstance(value, (int, float))
        )
        if capacity_blocked:
            failed.append({"reason": "bounded_ledger_capacity_blocked"})
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
        normalized_claims: list[dict[str, Any]] = []
        for claim in claims:
            if isinstance(claim.get("_refresh_schedule"), dict):
                normalized_claims.append(claim)
                continue
            # Compatibility for direct callers and legacy fixtures that invoke
            # the selection helper without a prior evaluator pass.
            value = economy_value(claim.get("economy"))
            status = str(claim.get("status") or "")
            normalized_claims.append(
                {
                    **claim,
                    "_refresh_schedule": {
                        "tier_rank": 1 if status in {"stale", "expired", "conflict"} else 2,
                        "economy_value": value,
                        "economy_known": value is not None,
                        "age_seconds": 0,
                        "hard_overdue": status in {"expired", "conflict"},
                        "next_refresh_at": None,
                        "hard_deadline": None,
                    },
                }
            )
        claims = normalized_claims
        # Pure tiered scheduler: hard overdue > lifecycle > ordinary due, and
        # owner round-robin is restarted within each tier.  Keep the legacy
        # cursor fields in the receipt for compatibility with existing route
        # projections, but do not let that flat cursor override tier order.
        selected: list[dict[str, Any]] = []
        cursor_before = 0
        cursor_after = 0

        def reserve_tiered(state: dict[str, Any]) -> None:
            nonlocal selected, cursor_before, cursor_after
            try:
                cursor_before = int(state.get("cursor") or 0)
            except (TypeError, ValueError):
                cursor_before = 0
            selected, offsets, last_owner = select_fair(
                claims,
                limit=limit,
                owner_cursor=state.get("owner_offsets") if isinstance(state.get("owner_offsets"), dict) else {},
                last_owner=str(state.get("last_owner") or ""),
            )
            cursor_after = (cursor_before + len(selected)) % max(1, len(claims))
            state["cursor"] = cursor_after
            state["last_owner"] = last_owner
            state["owner_offsets"] = offsets
            state["owner_count"] = len({self._owner_key(claim) for claim in claims})
            state["owner_scheduler_version"] = 2
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("state_refresh_state.json", reserve_tiered)
        return selected, cursor_before, cursor_after

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
