#!/usr/bin/env python3
"""Deterministic virtual-clock checks for the P2 refresh scheduler."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_economy import evaluate_refresh_schedule, make_economy, unknown_economy
from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.world_state import WorldStateStore
from runtime.belief_refresh_scheduler import (
    MAX_OWNER_OFFSETS,
    conflict_retry_entry,
    conflict_retry_generation,
    conflict_retry_suppressed,
    merge_conflict_retry_ledger,
    schedule_marker,
    select_fair,
    validate_refresh_state,
)
from runtime.state_refresh import StateRefresh


BASE = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, details: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def economy(*, due: str | None = None, max_stale: int | None = None, known: bool = True) -> dict[str, Any]:
    if not known:
        value = unknown_economy()
        value["next_refresh_at"] = due
        value["max_staleness_seconds"] = max_stale
        return value
    return make_economy(
        importance=0.9,
        importance_source="registered_policy",
        change_probability=0.8,
        change_probability_source="producer_policy",
        decision_impact=0.7,
        next_refresh_at=due,
        max_staleness_seconds=max_stale,
    )


def claim(*, key: str, status: str = "fresh", observed: str = "2026-08-12T23:59:00.000000Z", ttl: int = 3600, **extra: Any) -> dict[str, Any]:
    return make_claim(
        key=key,
        claim=f"claim {key}",
        source=str(extra.pop("source", "git_probe")),
        confidence=0.9,
        ttl_seconds=ttl,
        observed_at=observed,
        status=status,
        evidence=extra.pop("evidence", {}),
        economy=extra.pop("economy", None),
        refresh_spec=extra.pop("refresh_spec", None),
    )


class ImmediateProbe:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, target: str = "") -> dict[str, Any]:
        self.calls += 1
        return {
            "probe": "git_probe",
            "source": "git_probe",
            "status": "success",
            "summary": "workspace clean",
            "confidence": 0.9,
            "ttl_seconds": 3600,
            "details": {},
        }


class TransientProbe(ImmediateProbe):
    def run(self, target: str = "") -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary probe outage")
        return super().run(target)


class AcceptedPerception:
    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"status": "accepted"}


class EconomyPerception:
    def __init__(self, economy: dict[str, Any] | None = None) -> None:
        self.economy = economy

    def interpret_probe_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"status": "accepted", "economy": self.economy} if self.economy is not None else {"status": "accepted"}


class ReservedEconomyProbe(ImmediateProbe):
    def __init__(self, economy: dict[str, Any]) -> None:
        super().__init__()
        self._economy = economy

    def run(self, target: str = "") -> dict[str, Any]:
        result = super().run(target)
        result["_refresh_economy"] = self._economy
        return result


def scheduler_v2(**overrides: Any) -> dict[str, Any]:
    state = {
        "schema_version": "veyra.state_refresh_state.v2",
        "cursor": 0,
        "owner_offsets": {},
        "owner_count": 0,
        "owner_scheduler_version": 2,
        "consumed_due": [],
        "unrefreshable": [],
        "conflict_retry": [],
    }
    state.update(overrides)
    return state


def main() -> int:
    future = "2026-08-13T01:00:00.000000Z"
    due = claim(key="due", economy=economy(due="2026-08-12T23:00:00.000000Z"))
    fresh = claim(key="fresh", economy=economy(due=future))
    stale_future = claim(
        key="stale-future",
        status="stale",
        observed="2026-08-12T00:00:00.000000Z",
        economy=economy(due=future, max_stale=172800),
    )
    hard = claim(
        key="hard",
        observed="2026-08-12T00:00:00.000000Z",
        economy=economy(due=future, max_stale=3600),
    )
    hard_eval = evaluate_refresh_schedule(hard, now=BASE)
    stale_eval = evaluate_refresh_schedule(stale_future, now=BASE)
    fresh_eval = evaluate_refresh_schedule(fresh, now=BASE)
    expect(hard_eval["tier"] == "hard_overdue", "hard deadline outranks future next_due", hard_eval)
    expect(stale_eval["tier"] == "lifecycle", "stale current lifecycle ignores future next_due", stale_eval)
    expect(fresh_eval["tier"] == "not_due", "fresh future due remains quiet", fresh_eval)
    expect(
        evaluate_refresh_schedule(
            claim(key="unknown", economy=economy(due="2026-08-12T23:00:00.000000Z", known=False)),
            now=BASE,
        )["economy_value"] is None,
        "unknown economy never becomes numeric",
    )
    expect(
        validate_refresh_state({"schema_version": "veyra.state_refresh_state.v2"})
        == "state_refresh_v2_fields_missing",
        "malformed v2 state fails closed",
    )
    expect(
        validate_refresh_state(scheduler_v2(rogue="secret"))
        == "state_refresh_v2_fields_unknown"
        and validate_refresh_state(scheduler_v2(cursor=-1))
        == "state_refresh_cursor_malformed",
        "v2 scheduler state rejects unknown fields and negative counters",
    )
    missing_observed = claim(key="missing-observed", observed="2026-08-12T00:00:00.000000Z", economy=economy(max_stale=3600))
    missing_observed.pop("observed_at", None)
    missing_observed["updated_at"] = "2026-08-12T00:00:00.000000Z"
    invalid_schedule = evaluate_refresh_schedule(missing_observed, now=BASE)
    expect(invalid_schedule["valid"] is False and invalid_schedule["reason"] == "observed_at_invalid", "hard deadline does not fallback to updated_at", invalid_schedule)

    selected, _, _ = select_fair(
        [
            {**hard, "_refresh_schedule": hard_eval, "user_id": "a", "session_id": "s"},
            {**stale_future, "_refresh_schedule": stale_eval, "user_id": "b", "session_id": "s"},
            {**due, "_refresh_schedule": evaluate_refresh_schedule(due, now=BASE), "user_id": "c", "session_id": "s"},
        ],
        limit=10,
    )
    expect([item["key"] for item in selected] == ["hard", "stale-future", "due"], "tier precedence and no duplicate selection", selected)

    # Exercise the real BeliefCore persistence shape: a durable stale claim
    # already carries lifecycle fields, while refresh_claim_status adds
    # runtime-only fields. Pre-probe CAS must bind the original raw row and
    # still dispatch exactly once.
    with tempfile.TemporaryDirectory(prefix="veyra-refresh-beliefcore-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        persisted = belief.upsert_claim(
            claim(
                key="durable-stale",
                status="fresh",
                observed="2026-08-12T00:00:00.000000Z",
                ttl=60,
            )
        )
        expect(persisted.get("persisted") is True, "real BeliefCore stale fixture persists", persisted)
        store.mutate_json(
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
        refresh = StateRefresh(store, model_assist_enabled=False)
        probe = ImmediateProbe()
        refresh.probes = {"git_probe": probe}
        refresh.perception = AcceptedPerception()
        result = refresh.refresh_stale(limit=1, now=BASE)
        expect(result["refreshed_count"] == 1 and probe.calls == 1, "durable BeliefCore stale row passes pre-probe CAS", result)

        unsupported = claim(
            key="limit-zero-event",
            source="event",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
            economy=economy(due="2026-08-12T23:00:00.000000Z"),
        )
        supported = claim(
            key="limit-zero-git",
            source="git_probe",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
        )
        store.write_json("belief_state.json", {"claims": [unsupported, supported]})
        before = {
            name: store.path_for(name).read_bytes()
            for name in ("belief_state.json", "local_world.json", "state_refresh_state.json")
        }
        zero = refresh.refresh_stale(limit=0, now=BASE)
        after = {
            name: store.path_for(name).read_bytes()
            for name in ("belief_state.json", "local_world.json", "state_refresh_state.json")
        }
        expect(zero["selected_count"] == 0 and zero["unsupported_stale"] == 1, "limit zero reports unsupported stale without scheduler mutation", zero)
        expect(before == after and probe.calls == 1, "limit zero is byte-pure and does not probe", zero)

        store.write_json(
            "state_refresh_state.json",
            scheduler_v2(
                owner_offsets={f"legacy:{index}": 0 for index in range(MAX_OWNER_OFFSETS)}
            ),
        )
        store.write_json(
            "belief_state.json",
            {
                "claims": [
                    claim(
                        key="offset-capacity",
                        status="stale",
                        observed="2026-08-12T00:00:00.000000Z",
                        refresh_spec={
                            "schema_version": "veyra.belief.refresh_spec.v1",
                            "probe_kind": "git_probe",
                            "target_ref": "",
                            "resolver_id": "probe_default.v1",
                        },
                    )
                ]
            },
        )
        refresh.perception = AcceptedPerception()
        bounded = refresh.refresh_stale(limit=1, now=BASE)
        bounded_state = store.read_json("state_refresh_state.json")
        expect(
            bounded["refreshed_count"] == 1
            and len(bounded_state.get("owner_offsets") or {}) == MAX_OWNER_OFFSETS
            and validate_refresh_state(bounded_state) is None,
            "full owner cursor remains valid after selection",
            {"result": bounded, "offset_count": len(bounded_state.get("owner_offsets") or {})},
        )

    with tempfile.TemporaryDirectory(prefix="veyra-refresh-scheduler-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json("belief_state.json", {"claims": [claim(key="git", status="stale", observed="2026-08-12T00:00:00.000000Z", economy=economy(due="2026-08-12T23:00:00.000000Z"), refresh_spec={"schema_version": "veyra.belief.refresh_spec.v1", "probe_kind": "git_probe", "target_ref": "", "resolver_id": "probe_default.v1"})]})
        refresh = StateRefresh(store, model_assist_enabled=False)
        probe = ImmediateProbe()
        refresh.probes = {"git_probe": probe}
        refresh.perception = AcceptedPerception()
        first = refresh.refresh_stale(limit=1, now=BASE)
        second = refresh.refresh_stale(limit=1, now=BASE)
        expect(first["refreshed_count"] == 1 and second["selected_count"] == 0, "same due is consumed once", {"first": first, "second": second})
        expect(probe.calls == 1, "consumed due suppresses duplicate probe", probe.calls)

        # Legacy TTL lifecycle has no explicit schedule generation: once the
        # replacement becomes stale at a later virtual time it is eligible
        # again rather than permanently suppressed by the first refresh.
        legacy = claim(key="legacy", status="stale", observed="2026-08-12T00:00:00.000000Z", ttl=3600)
        store.write_json("belief_state.json", {"claims": [legacy]})
        legacy_probe = ImmediateProbe()
        refresh.probes = {"git_probe": legacy_probe}
        refresh.perception = AcceptedPerception()
        refresh.refresh_stale(limit=1, now=BASE)
        later = refresh.refresh_stale(limit=1, now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc))
        expect(later["selected_count"] == 1, "legacy TTL refresh is reusable at a later expiry", later)

        # Adapter omission carries the old verified economy; a changed
        # producer value is rejected before persistence.
        durable = store.read_json("belief_state.json")["claims"][0]
        old_economy = durable.get("economy")
        store.write_json("belief_state.json", {"claims": [{**durable, "status": "stale"}]})
        refresh.perception = EconomyPerception(None)
        refresh.refresh_stale(limit=1, now=BASE)
        carried = store.read_json("belief_state.json")["claims"][0].get("economy")
        expect(carried == old_economy, "refresh omission carries prior economy", carried)
        changed_for_spoof = {**store.read_json("belief_state.json")["claims"][0], "status": "stale", "economy": economy(due="2026-08-13T00:00:00.000000Z")}
        store.write_json("belief_state.json", {"claims": [changed_for_spoof]})
        refresh.perception = EconomyPerception(unknown_economy())
        spoof = refresh.refresh_stale(limit=1, now=BASE)
        expect(any(item.get("reason") == "economy_spoof_rejected" for item in spoof["failed"]), "adapter economy spoof is rejected", spoof)

        no_economy = claim(
            key="legacy-spoof",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
            refresh_spec={
                "schema_version": "veyra.belief.refresh_spec.v1",
                "probe_kind": "git_probe",
                "target_ref": "",
                "resolver_id": "probe_default.v1",
            },
        )
        store.write_json("belief_state.json", {"claims": [no_economy]})
        refresh.perception = EconomyPerception(economy(due="2026-08-13T01:00:00.000000Z"))
        legacy_spoof = refresh.refresh_stale(limit=1, now=BASE)
        expect(
            any(item.get("reason") == "economy_spoof_rejected" for item in legacy_spoof["failed"]),
            "refresh adapter cannot establish economy for a legacy claim",
            legacy_spoof,
        )

        # Reserved transport metadata cannot be supplied by a probe. This is
        # rejected before PerceptionLayer, so no hidden carry-forward can be
        # persisted and durable truth remains byte-identical.
        reserved_before = store.read_json("belief_state.json")
        refresh.probes = {"git_probe": ReservedEconomyProbe(economy(due="2026-08-13T01:00:00.000000Z"))}
        reserved = refresh.refresh_stale(limit=1, now=BASE)
        expect(reserved["refreshed_count"] == 0 and any(item.get("reason") == "economy_spoof_rejected" for item in reserved["failed"]), "probe reserved economy is rejected", reserved)
        expect(store.read_json("belief_state.json") == reserved_before, "reserved economy rejection preserves durable truth", store.read_json("belief_state.json"))

        durable_with_economy = {**no_economy, "economy": economy(due="2026-08-12T23:00:00.000000Z")}
        store.write_json("belief_state.json", {"claims": [durable_with_economy]})
        reserved_existing = refresh.refresh_stale(limit=1, now=BASE)
        expect(reserved_existing["refreshed_count"] == 0 and any(item.get("reason") == "economy_spoof_rejected" for item in reserved_existing["failed"]), "probe cannot replace server carry economy via reserved field", reserved_existing)

        # Changing producer-owned economy releases the marker without changing
        # the claim identity; no scheduler-generated next due is invented.
        current = store.read_json("belief_state.json")
        current["claims"][0]["economy"] = economy(due="2026-08-13T00:00:00.000000Z")
        store.write_json("belief_state.json", current)
        changed = refresh.refresh_stale(limit=1, now=BASE)
        expect(changed["selected_count"] == 1, "schedule change releases suppression", changed)

        # Unsupported ordinary claim archives once and remains byte-pure on the
        # next tick; changing its schedule unarchives it.
        unsupported = claim(key="event", source="event", status="stale", observed="2026-08-12T00:00:00.000000Z", economy=economy(due="2026-08-12T23:00:00.000000Z"))
        store.write_json("belief_state.json", {"claims": [unsupported]})
        before = store.read_json("state_refresh_state.json")
        archived = refresh.refresh_stale(limit=1, now=BASE)
        after_archive = store.read_json("state_refresh_state.json")
        repeat = refresh.refresh_stale(limit=1, now=BASE)
        after_repeat = store.read_json("state_refresh_state.json")
        expect(archived["status"] == "skipped" and after_archive.get("unrefreshable"), "unsupported source archives", archived)
        expect(repeat["selected_count"] == 0 and after_repeat == after_archive, "archive suppresses repeat and is byte-pure", repeat)
        changed = dict(unsupported)
        changed["economy"] = economy(due="2026-08-13T00:00:00.000000Z")
        store.write_json("belief_state.json", {"claims": [changed]})
        unarchived = refresh.refresh_stale(limit=1, now=BASE)
        expect(unarchived["selected_count"] == 0 and unarchived["skipped"], "unsupported schedule remains terminal without probe", unarchived)
        revised = {**changed, "claim_revision": 2, "observed_at": "2026-08-13T00:00:00.000000Z", "updated_at": "2026-08-13T00:00:00.000000Z"}
        store.write_json("belief_state.json", {"claims": [revised]})
        revised_result = refresh.refresh_stale(limit=1, now=BASE)
        expect(revised_result["selected_count"] == 0 and revised_result["skipped"], "unsupported revision remains terminal without probe", revised_result)

        conflict = claim(
            key="conflict",
            status="conflict",
            observed="2026-08-12T23:59:00.000000Z",
            refresh_spec={
                "schema_version": "veyra.belief.refresh_spec.v1",
                "probe_kind": "git_probe",
                "target_ref": "",
                "resolver_id": "probe_default.v1",
            },
        )
        store.write_json("belief_state.json", {"claims": [conflict]})
        # Use the real PerceptionLayer/BeliefCore receipt. A durable conflict
        # remains a degraded result, but its exact post-write revision is
        # attempted once and then suppressed instead of churning every tick.
        conflict_refresh = StateRefresh(store, model_assist_enabled=False)
        conflict_probe = ImmediateProbe()
        conflict_refresh.probes = {"git_probe": conflict_probe}
        first_conflict = conflict_refresh.refresh_stale(limit=1, now=BASE)
        first_conflict_state = store.read_json("state_refresh_state.json")
        second_conflict = conflict_refresh.refresh_stale(limit=1, now=BASE)
        expect(
            first_conflict["status"] == "degraded"
            and any(
                item.get("reason") == "belief_persistence_conflict"
                for item in first_conflict["failed"]
            )
            and len(first_conflict_state.get("conflict_retry") or []) == 1
            and second_conflict["selected_count"] == 0
            and conflict_probe.calls == 1,
            "durable cadence-less conflict is recorded once then suppressed",
            {"first": first_conflict, "second": second_conflict},
        )

        # A conflict retry slot is a latest-generation upsert, not a revision
        # history.  Six hundred ordinary claim revisions/ticks with the same
        # typed evidence frontier must remain one bounded private entry.
        conflict_generation_claim = {
            **conflict,
            "user_id": "owner-a",
            "session_id": "session-a",
            "claim_revision": 1,
            "evidence_graph_status": "unresolved",
            "evidence_graph_conflict_refs": ["ev_" + "a" * 24],
            "conflict_observations": [
                {
                    "value_digest": "b" * 64,
                    "conflict_refs": ["ev_" + "a" * 24],
                    "evidence_graph_status": "unresolved",
                }
            ],
        }
        first_generation = conflict_retry_generation(conflict_generation_claim)
        expect(first_generation, "conflict retry generation has typed frontier")
        ledger = [conflict_retry_entry(conflict_generation_claim, now=BASE)]
        expect(
            conflict_retry_suppressed(conflict_generation_claim, ledger, now=BASE),
            "fresh conflict generation suppresses after first attempt",
        )
        changed_spec = {
            **conflict_generation_claim,
            "refresh_spec": {
                "schema_version": "veyra.belief.refresh_spec.v1",
                "probe_kind": "git_probe",
                "target_ref": "main",
                "resolver_id": "probe_default.v1",
            },
        }
        expect(
            not conflict_retry_suppressed(changed_spec, ledger, now=BASE),
            "refresh spec generation releases suppression",
        )
        for revision in range(2, 602):
            churned = {
                **conflict_generation_claim,
                "claim_revision": revision,
                "updated_at": f"2026-08-13T00:{revision // 60:02d}:{revision % 60:02d}.000000Z",
                "history": [{"claim_revision": revision}],
            }
            expect(
                conflict_retry_suppressed(churned, ledger, now=BASE),
                "ordinary revision churn remains suppressed",
                revision,
            )
            ledger, blocked = merge_conflict_retry_ledger(
                ledger,
                [conflict_retry_entry(churned, now=BASE)],
                limit=512,
            )
            expect(blocked == 0 and len(ledger) == 1, "revision churn reuses one ledger slot", revision)
        # A real value/frontier change releases exactly one retry and updates
        # the same slot; it does not append an unbounded history record.
        new_evidence = {
            **conflict_generation_claim,
            "claim_revision": 602,
            "conflict_observations": [
                {
                    "value_digest": "c" * 64,
                    "conflict_refs": ["ev_" + "d" * 24],
                    "evidence_graph_status": "unresolved",
                }
            ],
        }
        expect(
            not conflict_retry_suppressed(new_evidence, ledger, now=BASE),
            "new evidence frontier releases one retry",
        )
        ledger, blocked = merge_conflict_retry_ledger(
            ledger,
            [conflict_retry_entry(new_evidence, now=BASE)],
            limit=512,
        )
        expect(
            blocked == 0
            and len(ledger) == 1
            and conflict_retry_suppressed(new_evidence, ledger, now=BASE),
            "new evidence replaces the prior identity slot",
            ledger,
        )
        other_owner = {
            **new_evidence,
            "user_id": "owner-b",
            "session_id": "session-b",
        }
        other_ledger, blocked = merge_conflict_retry_ledger(
            ledger,
            [conflict_retry_entry(other_owner, now=BASE)],
            limit=512,
        )
        expect(
            blocked == 0
            and len(other_ledger) == 2
            and not conflict_retry_suppressed(other_owner, ledger, now=BASE),
            "cross-owner conflict retry slots remain isolated",
            other_ledger,
        )
        replayed = json.loads(json.dumps(other_ledger))
        expect(
            conflict_retry_suppressed(other_owner, replayed, now=BASE),
            "conflict retry suppression survives restart replay",
        )
        try:
            merge_conflict_retry_ledger(
                [{"marker": "m1"}, {"marker": "m2"}], [], limit=1
            )
        except ValueError:
            pass
        else:
            raise AssertionError("over-capacity conflict ledger must fail closed")
        malformed_frontier = {
            **conflict_generation_claim,
            "conflict_observations": [{"value_digest": 42}],
        }
        malformed_entry = conflict_retry_entry(malformed_frontier, now=BASE)
        expect(
            malformed_entry.get("generation") is None
            and not conflict_retry_suppressed(malformed_frontier, [malformed_entry], now=BASE),
            "malformed evidence frontier never suppresses a retry",
            malformed_entry,
        )
        refresh.probes = {"git_probe": ImmediateProbe()}
        refresh.perception = AcceptedPerception()

        hard_unsupported = claim(
            key="hard-event",
            source="event",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
            economy=economy(max_stale=1),
        )
        store.write_json("belief_state.json", {"claims": [hard_unsupported]})
        hard_result = refresh.refresh_stale(limit=1, now=BASE)
        expect(
            len(hard_result["failed"]) == 1 and not hard_result["skipped"],
            "hard unsupported obligation receives one bounded disposition",
            hard_result,
        )

        mixed_supported = claim(
            key="mixed-git",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
            refresh_spec={
                "schema_version": "veyra.belief.refresh_spec.v1",
                "probe_kind": "git_probe",
                "target_ref": "",
                "resolver_id": "probe_default.v1",
            },
        )
        mixed_unsupported = claim(
            key="mixed-event",
            source="event",
            status="stale",
            observed="2026-08-12T00:00:00.000000Z",
        )
        store.write_json("belief_state.json", {"claims": [mixed_supported, mixed_unsupported]})
        mixed = refresh.refresh_stale(limit=1, now=BASE)
        expect(
            mixed["refreshed_count"] == 1
            and not mixed["skipped"]
            and mixed["unsupported_stale"] == 1,
            "unadmitted unsupported obligation is counted but not reported as handled",
            mixed,
        )

        store.write_json("state_refresh_state.json", scheduler_v2(owner_offsets="bad"))
        health = next(
            item
            for item in store.state_health()["items"]
            if item.get("name") == "state_refresh_state.json"
        )
        expect(
            health.get("health_status") == "invalid"
            and health.get("next_action") == "repair_state_json",
            "scheduler integrity failure is visible in state health",
            health,
        )
        store.write_json("state_refresh_state.json", scheduler_v2())

        transient_claim = claim(key="transient", status="stale", observed="2026-08-12T00:00:00.000000Z", economy=economy(due="2026-08-12T23:00:00.000000Z"), refresh_spec={"schema_version": "veyra.belief.refresh_spec.v1", "probe_kind": "git_probe", "target_ref": "", "resolver_id": "probe_default.v1"})
        store.write_json("belief_state.json", {"claims": [transient_claim]})
        transient = TransientProbe()
        refresh.probes = {"git_probe": transient}
        first_error = refresh.refresh_stale(limit=1, now=BASE)
        retry = refresh.refresh_stale(limit=1, now=BASE)
        expect(first_error["status"] == "degraded" and retry["selected_count"] == 1, "transient errors retry and are not archived", {"first": first_error, "retry": retry})

    print("belief refresh scheduler smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
