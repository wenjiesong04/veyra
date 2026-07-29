#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.belief_core import BeliefCore  # noqa: E402
from core.context_patch_builder import ContextPatchBuilder  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.awareness_context_assembler import AwarenessContextAssembler  # noqa: E402
from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.memory_policy_runtime import MemoryPolicyRuntime  # noqa: E402
from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.perception_layer import PerceptionLayer  # noqa: E402
from core.proactive_intent import WatchlistDraft  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import ExecutionResult  # noqa: E402
from interface.event_schema import (  # noqa: E402
    Decision,
    EventSource,
    EventType,
    LoopResult,
    Route,
    VeyraEvent,
    utc_now_iso,
)
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402
from runtime.state_refresh import StateRefresh  # noqa: E402


SHARED_SESSION = "shared-session"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def event(user_id: str, *, event_id: str, session_id: str = SHARED_SESSION) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="api",
            user_id=user_id,
            session_id=session_id,
        ),
        payload={"text": f"turn from {user_id}"},
        event_id=event_id,
    )


def verified_result(source: VeyraEvent) -> LoopResult:
    return LoopResult(
        event_id=source.event_id,
        route=Route.AGENT,
        status="success",
        response=f"verified result for {source.source.user_id}",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": {
                "status": "verified_success",
                "needs_memory_patch": True,
            }
        },
    )


def belief_claim(
    key: str,
    marker: str,
    source: str,
    **scope: Any,
) -> dict[str, Any]:
    observed_at = utc_now_iso()
    return {
        "key": key,
        "claim": marker,
        "source": source,
        "confidence": 0.9,
        "source_trust": 0.8,
        "observed_at": observed_at,
        "updated_at": observed_at,
        "ttl_seconds": 3600,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "status": "fresh",
        "claim_kind": "observed",
        "evidence": {},
        **scope,
    }


def memory_write_scope_checks(store: WorldStateStore) -> None:
    writes: list[dict[str, Any]] = []
    runtime = MemoryPolicyRuntime(
        store,
        lambda patch: writes.append(dict(patch)) or {"status": "written"},
    )
    decision = Decision(
        route=Route.AGENT,
        risk_level=RiskLevel.R1,
        reason="scope smoke",
        memory_policy="long_term",
    )
    event_a = event("user-a", event_id="evt-same")
    event_b = event("user-b", event_id="evt-same")

    first = runtime.apply(event_a, decision, verified_result(event_a))
    second = runtime.apply(event_b, decision, verified_result(event_b))

    expect(first["status"] == "written", "first scoped durable memory writes", first)
    expect(second["status"] == "written", "same event/session for another user does not dedupe", second)
    expect(len(writes) == 2, "idempotency domain contains user and session", writes)
    expect(
        {(item["user_id"], item["session_id"]) for item in writes}
        == {("user-a", SHARED_SESSION), ("user-b", SHARED_SESSION)},
        "synchronous durable patches carry authoritative owner",
        writes,
    )

    short_decision = Decision(
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        reason="scope smoke",
        memory_policy="short_term",
    )
    runtime.apply(event_a, short_decision, verified_result(event_a))
    runtime.apply(event_b, short_decision, verified_result(event_b))
    short_items = store.read_json("task_state.json").get("short_term_memory", [])
    expect(
        {(item.get("user_id"), item.get("session_id")) for item in short_items}
        >= {("user-a", SHARED_SESSION), ("user-b", SHARED_SESSION)},
        "synchronous short memory carries authoritative owner",
        short_items,
    )

    callback_execution = ExecutionResult(
        task_id="callback-task",
        executor="openclaw",
        status="success",
        result="callback result",
    )
    callback = runtime.apply_agent_result(
        task_context={
            "authority": "veyra_registered",
            "task_id": "callback-task",
            "event_id": "evt-callback",
            "correlation_id": "corr-callback",
            "user_id": "user-a",
            "session_id": SHARED_SESSION,
            "memory_policy": "long_term",
        },
        execution=callback_execution,
        verification={
            "status": "verified_success",
            "needs_memory_patch": True,
            "verdict": "scope_smoke",
        },
        context_found=True,
    )
    expect(callback["status"] == "written", "authoritative callback memory writes", callback)
    expect(
        writes[-1]["user_id"] == "user-a"
        and writes[-1]["session_id"] == SHARED_SESSION,
        "callback patch inherits registered user and session",
        writes[-1],
    )
    short_count = len(short_items)
    missing_owner = runtime.apply_agent_result(
        task_context={
            "authority": "veyra_registered",
            "task_id": "ownerless-callback",
            "session_id": SHARED_SESSION,
            "memory_policy": "short_term",
        },
        execution=ExecutionResult(
            task_id="ownerless-callback",
            executor="openclaw",
            status="success",
            result="must not persist",
        ),
        verification={"status": "verified_success"},
        context_found=True,
    )
    expect(missing_owner["status"] == "skipped", "ownerless callback fails closed", missing_owner)
    expect(
        len(store.read_json("task_state.json").get("short_term_memory", []))
        == short_count,
        "ownerless callback creates no short memory",
    )


def belief_scope_identity_checks() -> None:
    with TemporaryDirectory(prefix="veyra-belief-scope-identity-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        belief = BeliefCore(store)
        same_key = "shared:scope:status"
        claim_a = belief_claim(
            same_key,
            "PRIVATE_A",
            "file_probe",
            scope_kind="tenant",
            tenant_derived=True,
            user_id="user-a",
            session_id="session-a",
        )
        claim_b = belief_claim(
            same_key,
            "PRIVATE_B",
            "file_probe",
            scope_kind="tenant",
            tenant_derived=True,
            user_id="user-b",
            session_id="session-a",
        )
        claim_a_other_session = belief_claim(
            same_key,
            "PRIVATE_A_OTHER_SESSION",
            "file_probe",
            scope_kind="tenant",
            tenant_derived=True,
            user_id="user-a",
            session_id="session-b",
        )
        claim_global = belief_claim(
            same_key,
            "OPERATOR_GLOBAL_SAME_KEY",
            "system_probe",
            scope_kind="operator_global",
        )
        belief.upsert_claims(
            [
                claim_a,
                claim_b,
                claim_a_other_session,
                claim_global,
            ]
        )
        stored = store.read_json("belief_state.json").get("claims", [])
        expect(
            len(stored) == 4,
            "Belief identity keeps same logical key in four scope domains",
            stored,
        )
        visible_a = belief.relevant_claims(
            [same_key],
            user_id="user-a",
            session_id="session-a",
        )
        visible_b = belief.relevant_claims(
            [same_key],
            user_id="user-b",
            session_id="session-a",
        )
        expect(
            {item.get("claim") for item in visible_a}
            == {"PRIVATE_A", "OPERATOR_GLOBAL_SAME_KEY"}
            and {item.get("claim") for item in visible_b}
            == {"PRIVATE_B", "OPERATOR_GLOBAL_SAME_KEY"},
            "same-key Beliefs remain exact per user and session",
            {"user_a": visible_a, "user_b": visible_b},
        )
        updated_a = {
            **claim_a,
            "claim": "PRIVATE_A_UPDATED",
            "updated_at": utc_now_iso(),
        }
        belief.upsert_claim(updated_a)
        stored_after_update = store.read_json(
            "belief_state.json"
        ).get("claims", [])
        expect(
            len(stored_after_update) == 4
            and any(
                item.get("claim") == "PRIVATE_A_UPDATED"
                for item in stored_after_update
            )
            and any(
                item.get("claim") == "PRIVATE_B"
                for item in stored_after_update
            ),
            "same-scope Belief refresh merges without replacing peers",
            stored_after_update,
        )
        false_conflicts = []
        for item in stored_after_update:
            changed = dict(item)
            if changed.get("scope_kind") == "tenant":
                changed["status"] = "conflict"
                changed["next_action"] = "refresh_probe"
                changed["conflicts_with"] = "legacy-cross-scope"
            false_conflicts.append(changed)
        store.write_json(
            "belief_state.json",
            {"claims": false_conflicts},
        )
        refreshed = belief.refresh()
        refreshed_claims = refreshed.get("claims", [])
        expect(
            all(
                item.get("status") != "conflict"
                and "conflicts_with" not in item
                for item in refreshed_claims
            ),
            "scope-aware refresh clears detector-owned legacy false conflicts",
            refreshed_claims,
        )

        class ExactRefreshProbe:
            def run(self, _: str) -> dict[str, Any]:
                return {
                    "probe": "file_probe",
                    "source": "file_probe",
                    "status": "ok",
                    "summary": "PRIVATE_A_REFRESHED",
                    "claims": [
                        {
                            "key": "refresh:exact:status",
                            "claim": "PRIVATE_A_REFRESHED",
                        }
                    ],
                }

        stale = belief_claim(
            "refresh:exact:status",
            "PRIVATE_A_STALE",
            "file_probe",
            scope_kind="tenant",
            tenant_derived=True,
            user_id="user-a",
            session_id="session-a",
        )
        stale["status"] = "stale"
        stale["next_action"] = "refresh_probe"
        belief.upsert_claim(stale)
        state_refresh = StateRefresh(
            store,
            model_assist_enabled=False,
        )
        state_refresh.probes["file_probe"] = ExactRefreshProbe()
        refresh_result = state_refresh.refresh_stale(limit=1)
        exact_refreshed = belief.relevant_claims(
            ["refresh:exact:status"],
            user_id="user-a",
            session_id="session-a",
        )
        expect(
            refresh_result.get("refreshed")
            and len(exact_refreshed) == 1
            and exact_refreshed[0].get("claim")
            == "PRIVATE_A_REFRESHED",
            "stale refresh preserves exact tenant scope identity",
            {
                "refresh": refresh_result,
                "claims": exact_refreshed,
            },
        )


def commitment_control_scope_checks() -> None:
    with TemporaryDirectory(
        prefix="veyra-commitment-control-scope-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        core = CommitmentCore(store)
        pending_a = core.create_commitment(
            {
                "kind": "external_digest",
                "status": "pending_confirmation",
                "title": "A-SECRET-PENDING",
                "user_id": "user-a",
                "session_id": SHARED_SESSION,
                "channel": "api",
                "payload": {"topic": "A-SECRET-TOPIC"},
            }
        )
        event_a = event("user-a", event_id="evt-pending-a")
        event_b = event("user-b", event_id="evt-pending-b")
        result_b_confirm = core.process_turn(
            event=event_b,
            user_text="好的",
            assistant_response="",
            route="direct_answer",
            status="success",
        )
        result_b_decline = core.process_turn(
            event=event_b,
            user_text="不用",
            assistant_response="",
            route="direct_answer",
            status="success",
        )
        pending_after_b = core.get_commitment(
            str(pending_a.get("commitment_id"))
        )
        expect(
            pending_after_b is not None
            and pending_after_b.get("status")
            == "pending_confirmation"
            and result_b_confirm.get("status") != "confirmed"
            and result_b_decline.get("status") != "declined",
            "same-session peer cannot confirm or decline another commitment",
            {
                "commitment": pending_after_b,
                "confirm": result_b_confirm,
                "decline": result_b_decline,
            },
        )
        result_a_confirm = core.process_turn(
            event=event_a,
            user_text="好的",
            assistant_response="",
            route="direct_answer",
            status="success",
        )
        expect(
            result_a_confirm.get("status") == "confirmed"
            and core.get_commitment(
                str(pending_a.get("commitment_id"))
            ).get("status")
            == "active",
            "exact owner can confirm its pending commitment",
            result_a_confirm,
        )

        draft = WatchlistDraft(
            topic="SAME-SCOPE-TOPIC",
            query="same scope topic latest",
            sources=["public_web"],
            refresh_policy={"kind": "authorized"},
            ranking_policy={"prefer": ["freshness"]},
            dedupe_policy={"key": "url"},
            ttl=1800,
            status="active",
            source_intent_id="scope-smoke",
        )
        watch_a = core.record_watchlist_draft(
            draft,
            user_id="user-a",
            session_id=SHARED_SESSION,
        )
        watch_b = core.record_watchlist_draft(
            draft,
            user_id="user-b",
            session_id=SHARED_SESSION,
        )
        expect(
            watch_a.get("watchlist_id")
            and watch_b.get("watchlist_id")
            and watch_a.get("watchlist_id")
            != watch_b.get("watchlist_id"),
            "watchlist identity binds exact owner scope",
            {"user_a": watch_a, "user_b": watch_b},
        )
        semantic = {
            "operation": "query_status",
            "entities": {"topic": "SAME-SCOPE-TOPIC"},
        }
        matches_a = core._match_watchlists_for_semantic(
            semantic,
            event=event_a,
        )
        matches_b = core._match_watchlists_for_semantic(
            semantic,
            event=event_b,
        )
        expect(
            [item.get("watchlist_id") for item in matches_a]
            == [watch_a.get("watchlist_id")]
            and [
                item.get("watchlist_id") for item in matches_b
            ]
            == [watch_b.get("watchlist_id")],
            "semantic watchlist status matches only exact owner",
            {"user_a": matches_a, "user_b": matches_b},
        )
        external = store.read_json("external_world.json")
        watchlist = (
            external.get("watchlist")
            if isinstance(external.get("watchlist"), list)
            else []
        )
        watchlist.append(
            {
                "watchlist_id": "legacy-ownerless-watch",
                "topic": "A-SECRET-TOPIC",
                "status": "active",
            }
        )
        store.write_json(
            "external_world.json",
            {**external, "watchlist": watchlist},
        )
        secret_semantic = {
            "operation": "query_status",
            "entities": {"topic": "A-SECRET-TOPIC"},
        }
        expect(
            core._match_watchlists_for_semantic(
                secret_semantic,
                event=event_b,
            )
            == [],
            "ownerless and peer watchlists fail closed on status lookup",
        )


def seed_scoped_context_state(store: WorldStateStore) -> None:
    store.patch_json(
        "user_world.json",
        {
            "preferences": {
                "legacy-secret": "must-not-leak",
                "default_location": "legacy-location",
            },
            "profile": {
                "legacy-profile": "must-not-leak",
                "education": "legacy-education",
            },
            "current_project": "legacy-project",
            "learning_topic": "legacy-learning-topic",
            "profiles_by_user": {
                "user-a": {
                    "preferences": {
                        "tone": "alpha-tone",
                        "default_location": "alpha-location",
                    },
                    "profile": {"alias": "alpha-profile"},
                    "current_project": "alpha-project",
                },
                "user-b": {
                    "preferences": {"tone": "beta-tone"},
                    "profile": {"alias": "beta-profile"},
                    "current_project": "beta-project",
                },
            },
        },
    )
    store.patch_json(
        "task_state.json",
        {
            "current_task": {
                "user_id": "user-a",
                "session_id": SHARED_SESSION,
                "task": "alpha-current-task",
                "status": "running",
            },
            "history": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "message": "alpha-history",
                },
                {
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "message": "beta-history",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task_context": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                    "message": "conflicting-history",
                },
                {
                    "session_id": SHARED_SESSION,
                    "message": "legacy-ownerless-history",
                },
                {
                    "user_id": "user-a",
                    "session_id": "other-session",
                    "message": "alpha-other-session-history",
                },
            ],
            "short_term_memory": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "summary": "alpha-short-memory",
                },
                {
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "summary": "beta-short-memory",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task_context": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                    "summary": "conflicting-short-memory",
                },
                {
                    "session_id": SHARED_SESSION,
                    "summary": "legacy-ownerless-short-memory",
                },
                {
                    "user_id": "user-a",
                    "session_id": "other-session",
                    "summary": "alpha-other-session-short-memory",
                },
            ],
            "pending_agent_tasks": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task": "alpha-pending",
                },
                {
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "task": "beta-pending",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task_context": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                    "task": "conflicting-pending",
                },
                {
                    "session_id": SHARED_SESSION,
                    "task": "legacy-ownerless-pending",
                },
            ],
            "conversation_slots": {
                SHARED_SESSION: {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "last_topic": "alpha-slot",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
        },
    )
    store.patch_json(
        "external_world.json",
        {
            "watchlist": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "target": "alpha-external-target",
                },
                {
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "target": "beta-external-target",
                },
                {
                    "session_id": SHARED_SESSION,
                    "target": "legacy-ownerless-external-target",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task_context": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                    "target": "conflicting-external-target",
                },
            ],
            "summaries": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "summary": "alpha-external-summary",
                },
                {
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "summary": "beta-external-summary",
                },
                {
                    "session_id": SHARED_SESSION,
                    "summary": "legacy-ownerless-external-summary",
                },
            ],
        },
    )
    alpha_belief = belief_claim(
        "scope_alpha:status",
        "ALPHA_TENANT_BELIEF",
        "file_probe",
        scope_kind="tenant",
        tenant_derived=True,
        user_id="user-a",
        session_id=SHARED_SESSION,
    )
    beta_belief = belief_claim(
        "scope_beta:status",
        "BETA_TENANT_BELIEF",
        "file_probe",
        scope_kind="tenant",
        tenant_derived=True,
        user_id="user-b",
        session_id=SHARED_SESSION,
    )
    ownerless_belief = belief_claim(
        "scope_ownerless:status",
        "OWNERLESS_TENANT_BELIEF",
        "web_probe",
        scope_kind="tenant",
        tenant_derived=True,
    )
    conflicting_belief = belief_claim(
        "scope_conflict:status",
        "CONFLICTING_TENANT_BELIEF",
        "file_probe",
        scope_kind="tenant",
        tenant_derived=True,
        user_id="user-a",
        session_id=SHARED_SESSION,
    )
    conflicting_belief["evidence"] = {
        "user_id": "user-b",
        "session_id": SHARED_SESSION,
    }
    store.patch_json(
        "belief_state.json",
        {
            "claims": [
                belief_claim(
                    "scope_system:status",
                    "OPERATOR_GLOBAL_BELIEF",
                    "system_probe",
                ),
                alpha_belief,
                beta_belief,
                ownerless_belief,
                conflicting_belief,
            ]
        },
    )
    store.patch_json(
        "local_world.json",
        {
            "probes": {
                "time_probe": {
                    "probe": "time_probe",
                    "status": "ok",
                    "summary": "OPERATOR_GLOBAL_PROBE",
                },
                "file_probe": {
                    "probe": "file_probe",
                    "status": "ok",
                    "summary": "ALPHA_TENANT_PROBE",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                },
                "log_probe": {
                    "probe": "log_probe",
                    "status": "ok",
                    "summary": "OWNERLESS_TENANT_PROBE",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                },
                "git_probe": {
                    "probe": "git_probe",
                    "status": "ok",
                    "summary": "CONFLICTING_TENANT_PROBE",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "evidence": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                },
            }
        },
    )
    store.patch_json(
        "channel_state.json",
        {
            "inbox": [
                {
                    "event_id": "evt-alpha-inbound",
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "text": "alpha-inbound",
                    "received_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "event_id": "evt-beta-inbound",
                    "user_id": "user-b",
                    "session_id": SHARED_SESSION,
                    "text": "beta-inbound",
                    "received_at": "2026-01-01T00:00:01+00:00",
                },
                {
                    "event_id": "evt-conflicting-inbound",
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "task_context": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                    },
                    "text": "conflicting-inbound",
                    "received_at": "2026-01-01T00:00:01+00:00",
                },
                {
                    "event_id": "evt-legacy-inbound",
                    "session_id": SHARED_SESSION,
                    "text": "legacy-ownerless-inbound",
                    "received_at": "2026-01-01T00:00:02+00:00",
                },
                {
                    "event_id": "evt-alpha-other",
                    "user_id": "user-a",
                    "session_id": "other-session",
                    "text": "alpha-other-session-inbound",
                    "received_at": "2026-01-01T00:00:03+00:00",
                },
            ],
            "outbox": [
                {
                    "session_id": SHARED_SESSION,
                    "message": "alpha-outbound",
                    "metadata": {"event_id": "evt-alpha-inbound"},
                    "created_at": "2026-01-01T00:00:04+00:00",
                },
                {
                    "session_id": SHARED_SESSION,
                    "message": "beta-outbound",
                    "metadata": {"event_id": "evt-beta-inbound"},
                    "created_at": "2026-01-01T00:00:05+00:00",
                },
                {
                    "session_id": SHARED_SESSION,
                    "message": "legacy-ownerless-outbound",
                    "metadata": {},
                    "created_at": "2026-01-01T00:00:06+00:00",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "message": "conflicting-outbound",
                    "metadata": {
                        "user_id": "user-b",
                        "event_id": "evt-alpha-inbound",
                    },
                    "created_at": "2026-01-01T00:00:07+00:00",
                },
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "message": "conflicting-event-outbound",
                    "metadata": {"event_id": "evt-beta-inbound"},
                    "created_at": "2026-01-01T00:00:08+00:00",
                },
            ],
        },
    )


def context_isolation_checks(store: WorldStateStore) -> None:
    event_a = event("user-a", event_id="evt-current-a")
    event_b = event("user-b", event_id="evt-current-b")

    patch_builder = ContextPatchBuilder(store)
    patch_a = patch_builder.build(
        "continue alpha",
        [],
        event=event_a,
    )
    patch_b = patch_builder.build(
        "continue beta",
        [],
        event=event_b,
    )
    task_a = patch_a["task_state"]
    task_b = patch_b["task_state"]
    expect(
        task_a["current_task"]["task"] == "alpha-current-task",
        "ContextPatchBuilder includes exact owned current task",
        task_a,
    )
    expect(
        task_b["current_task"] is None,
        "same-session user cannot read another current task",
        task_b,
    )
    serialized_patch_a = json.dumps(patch_a, ensure_ascii=False)
    serialized_patch_b = json.dumps(patch_b, ensure_ascii=False)
    expect(
        "alpha-short-memory" in serialized_patch_a
        and "beta-short-memory" not in serialized_patch_a
        and "legacy-ownerless-short-memory" not in serialized_patch_a,
        "ContextPatchBuilder filters short memory before Agent context",
        patch_a,
    )
    expect(
        "beta-short-memory" in serialized_patch_b
        and "alpha-short-memory" not in serialized_patch_b
        and "legacy-ownerless-short-memory" not in serialized_patch_b,
        "same-session users receive disjoint ContextPatch memory",
        patch_b,
    )
    expect(
        "legacy-secret" not in serialized_patch_a
        and "legacy-secret" not in serialized_patch_b,
        "ownerless user-world fallback is excluded",
    )
    expect(
        "conflicting-short-memory" not in serialized_patch_a
        and "conflicting-short-memory" not in serialized_patch_b,
        "conflicting ContextPatch owner envelope fails closed",
    )
    expect(
        "alpha-external-target" in serialized_patch_a
        and "alpha-external-summary" in serialized_patch_a
        and "beta-external" not in serialized_patch_a
        and "legacy-ownerless-external" not in serialized_patch_a
        and "conflicting-external" not in serialized_patch_a,
        "Agent context includes only exact owner-session ExternalWorld for user A",
        patch_a,
    )
    expect(
        "beta-external-target" in serialized_patch_b
        and "beta-external-summary" in serialized_patch_b
        and "alpha-external" not in serialized_patch_b
        and "legacy-ownerless-external" not in serialized_patch_b,
        "same-session user B cannot read another ExternalWorld context",
        patch_b,
    )
    scope_focus = [
        "current_time",
        "codebase",
        "logs",
        "git_workspace",
        "scope_alpha",
        "scope_beta",
        "scope_ownerless",
        "scope_conflict",
        "scope_system",
    ]
    scoped_patch_a = patch_builder.build(
        "inspect alpha scope",
        scope_focus,
        event=event_a,
    )
    scoped_patch_b = patch_builder.build(
        "inspect beta scope",
        scope_focus,
        event=event_b,
    )
    scoped_serialized_a = json.dumps(scoped_patch_a, ensure_ascii=False)
    scoped_serialized_b = json.dumps(scoped_patch_b, ensure_ascii=False)
    expect(
        "OPERATOR_GLOBAL_PROBE" in scoped_serialized_a
        and "ALPHA_TENANT_PROBE" in scoped_serialized_a
        and "OWNERLESS_TENANT_PROBE" not in scoped_serialized_a
        and "CONFLICTING_TENANT_PROBE" not in scoped_serialized_a,
        "ContextPatch probes allow exact tenant and operator-global only",
        scoped_patch_a["relevant_world_state"],
    )
    expect(
        "OPERATOR_GLOBAL_PROBE" in scoped_serialized_b
        and "ALPHA_TENANT_PROBE" not in scoped_serialized_b
        and "OWNERLESS_TENANT_PROBE" not in scoped_serialized_b
        and "CONFLICTING_TENANT_PROBE" not in scoped_serialized_b,
        "same-session user cannot read tenant, ownerless, or conflicting probes",
        scoped_patch_b["relevant_world_state"],
    )
    expect(
        "OPERATOR_GLOBAL_BELIEF" in scoped_serialized_a
        and "ALPHA_TENANT_BELIEF" in scoped_serialized_a
        and "BETA_TENANT_BELIEF" not in scoped_serialized_a
        and "OWNERLESS_TENANT_BELIEF" not in scoped_serialized_a
        and "CONFLICTING_TENANT_BELIEF" not in scoped_serialized_a,
        "ContextPatch beliefs enforce exact owner and fail closed",
        scoped_patch_a["belief_state"],
    )
    expect(
        "OPERATOR_GLOBAL_BELIEF" in scoped_serialized_b
        and "BETA_TENANT_BELIEF" in scoped_serialized_b
        and "ALPHA_TENANT_BELIEF" not in scoped_serialized_b
        and "OWNERLESS_TENANT_BELIEF" not in scoped_serialized_b
        and "CONFLICTING_TENANT_BELIEF" not in scoped_serialized_b,
        "same-session ContextPatch beliefs remain disjoint",
        scoped_patch_b["belief_state"],
    )

    turn_builder = TurnContextBuilder(store)
    context_a = turn_builder.build(
        user_message="continue alpha",
        attention_focus=[],
        event=event_a,
    )
    context_b = turn_builder.build(
        user_message="continue beta",
        attention_focus=[],
        event=event_b,
    )
    serialized_a = json.dumps(context_a["short_memory"], ensure_ascii=False)
    serialized_b = json.dumps(context_b["short_memory"], ensure_ascii=False)
    expect(
        all(
            marker in serialized_a
            for marker in (
                "alpha-inbound",
                "alpha-outbound",
                "alpha-history",
                "alpha-short-memory",
                "alpha-pending",
                "alpha-current-task",
                "alpha-slot",
            )
        ),
        "owner receives exact current/history/conversation/short context",
        context_a["short_memory"],
    )
    expect(
        all(
            marker not in serialized_a
            for marker in (
                "beta-inbound",
                "beta-outbound",
                "beta-history",
                "beta-short-memory",
                "beta-pending",
                "legacy-ownerless",
                "alpha-other-session",
                "conflicting",
            )
        ),
        "other-user, other-session, and legacy context never reaches user A",
        context_a["short_memory"],
    )
    expect(
        all(
            marker in serialized_b
            for marker in (
                "beta-inbound",
                "beta-outbound",
                "beta-history",
                "beta-short-memory",
                "beta-pending",
            )
        ),
        "same-session user B receives only owned context",
        context_b["short_memory"],
    )
    expect(
        all(
            marker not in serialized_b
            for marker in (
                "alpha-inbound",
                "alpha-outbound",
                "alpha-history",
                "alpha-short-memory",
                "alpha-pending",
                "alpha-current-task",
                "alpha-slot",
                "legacy-ownerless",
                "conflicting",
            )
        ),
        "same-session user B cannot read user A or legacy context",
        context_b["short_memory"],
    )
    turn_alpha = turn_builder.build(
        user_message="inspect alpha belief",
        attention_focus=["scope_alpha"],
        event=event_a,
    )
    turn_alpha_as_b = turn_builder.build(
        user_message="inspect alpha belief",
        attention_focus=["scope_alpha"],
        event=event_b,
    )
    turn_system_a = turn_builder.build(
        user_message="inspect system belief",
        attention_focus=["scope_system"],
        event=event_a,
    )
    turn_system_b = turn_builder.build(
        user_message="inspect system belief",
        attention_focus=["scope_system"],
        event=event_b,
    )
    expect(
        "ALPHA_TENANT_BELIEF"
        in json.dumps(turn_alpha["belief"], ensure_ascii=False)
        and "ALPHA_TENANT_BELIEF"
        not in json.dumps(turn_alpha_as_b["belief"], ensure_ascii=False),
        "TurnContextBuilder filters tenant beliefs by exact user and session",
        {"user_a": turn_alpha["belief"], "user_b": turn_alpha_as_b["belief"]},
    )
    expect(
        "OPERATOR_GLOBAL_BELIEF"
        in json.dumps(turn_system_a["belief"], ensure_ascii=False)
        and "OPERATOR_GLOBAL_BELIEF"
        in json.dumps(turn_system_b["belief"], ensure_ascii=False),
        "TurnContextBuilder keeps operator-global system beliefs shared",
        {"user_a": turn_system_a["belief"], "user_b": turn_system_b["belief"]},
    )
    for rejected_marker in (
        "OWNERLESS_TENANT_BELIEF",
        "CONFLICTING_TENANT_BELIEF",
    ):
        rejected_a = turn_builder.build(
            user_message="inspect rejected belief",
            attention_focus=[
                "scope_ownerless"
                if "OWNERLESS" in rejected_marker
                else "scope_conflict"
            ],
            event=event_a,
        )
        rejected_b = turn_builder.build(
            user_message="inspect rejected belief",
            attention_focus=[
                "scope_ownerless"
                if "OWNERLESS" in rejected_marker
                else "scope_conflict"
            ],
            event=event_b,
        )
        expect(
            rejected_marker
            not in json.dumps(
                {"user_a": rejected_a["belief"], "user_b": rejected_b["belief"]},
                ensure_ascii=False,
            ),
            f"TurnContextBuilder rejects {rejected_marker.lower()}",
        )
    commitment = CommitmentCore(store)
    expect(
        commitment._recent_commitment_topic_for_session(
            SHARED_SESSION,
            user_id="user-a",
        )
        == "alpha-slot",
        "commitment topic lookup accepts exact slot owner",
    )
    expect(
        commitment._recent_commitment_topic_for_session(
            SHARED_SESSION,
            user_id="user-b",
        )
        == "",
        "same-session user cannot read another commitment slot",
    )
    event_c = event("user-c", event_id="evt-current-c")
    loop = AwarenessLoop(store, RuntimeEntity(store))
    previous_a = loop._previous_inbound_message(event_a)
    previous_b = loop._previous_inbound_message(event_b)
    outbound_a = loop._last_outbound_message(event_a)
    outbound_b = loop._last_outbound_message(event_b)
    expect(
        previous_a.get("text") == "alpha-inbound"
        and previous_b.get("text") == "beta-inbound",
        "follow-up inbound lookup requires exact user and session",
        {"user_a": previous_a, "user_b": previous_b},
    )
    expect(
        outbound_a.get("message") == "alpha-outbound"
        and outbound_b.get("message") == "beta-outbound",
        "follow-up outbox lookup uses exact owner or owned event linkage",
        {"user_a": outbound_a, "user_b": outbound_b},
    )
    followup_b = loop._generic_previous_turn_followup_result(
        event_b,
        "为什么",
        [],
    )
    expect(
        followup_b is not None
        and "beta-outbound" in followup_b.response
        and "alpha-outbound" not in followup_b.response
        and "conflicting-outbound" not in followup_b.response,
        "same-session follow-up never quotes another user's outbox",
        followup_b.to_dict() if followup_b else None,
    )
    expect(
        loop._project_context_response(event_c) == "",
        "ownerless project and history are not continuation context",
    )
    expect(
        loop._user_context_planning_response("暑期项目", event_c) == "",
        "ownerless profile is not planning context",
    )
    expect(
        "alpha-project" in loop._project_context_response(event_a),
        "exact scoped project remains available",
    )
    expect(
        commitment._default_location_for_event(event_a)
        == "alpha-location",
        "exact scoped location remains available",
    )
    expect(
        commitment._default_location_for_event(event_c) == "",
        "ownerless default location is not inherited by another user",
    )


def perception_persistence_scope_checks() -> None:
    with TemporaryDirectory(prefix="veyra-perception-scope-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        perception = PerceptionLayer(
            store,
            model_assist_enabled=False,
        )
        perception.interpret_probe_result(
            {
                "probe": "file_probe",
                "path": "/tmp/alpha-scope",
                "exists": True,
                "status": "ok",
                "summary": "PERCEPTION_ALPHA_PROBE",
                "claims": [
                    {
                        "key": "perception_alpha:status",
                        "claim": "PERCEPTION_ALPHA_CLAIM",
                    }
                ],
                "user_id": "user-a",
                "session_id": SHARED_SESSION,
                "scope_kind": "tenant",
            }
        )
        perception.interpret_probe_result(
            {
                "probe": "file_probe",
                "path": "/tmp/beta-scope",
                "exists": True,
                "status": "ok",
                "summary": "PERCEPTION_BETA_PROBE",
                "claims": [
                    {
                        "key": "perception_alpha:status",
                        "claim": "PERCEPTION_BETA_CLAIM",
                    }
                ],
                "user_id": "user-b",
                "session_id": SHARED_SESSION,
                "scope_kind": "tenant",
            }
        )
        perception.interpret_probe_result(
            {
                "probe": "log_probe",
                "path": "/tmp/ownerless-scope",
                "status": "ok",
                "summary": "PERCEPTION_OWNERLESS_PROBE",
                "claims": [
                    {
                        "key": "perception_ownerless:status",
                        "claim": "PERCEPTION_OWNERLESS_CLAIM",
                    }
                ],
                "scope_kind": "tenant",
                "tenant_derived": True,
            }
        )
        perception.interpret_probe_result(
            {
                "probe": "time_probe",
                "status": "ok",
                "summary": "PERCEPTION_GLOBAL_PROBE",
                "claims": [
                    {
                        "key": "perception_global:status",
                        "claim": "PERCEPTION_GLOBAL_CLAIM",
                    }
                ],
            }
        )
        local_world = store.read_json("local_world.json")
        local_probes = local_world.get("probes", {})
        scoped_probes = local_world.get("scoped_probes", {})
        alpha_scope = scoped_probes.get(
            tenant_scope_storage_key("user-a", SHARED_SESSION),
            {},
        )
        beta_scope = scoped_probes.get(
            tenant_scope_storage_key("user-b", SHARED_SESSION),
            {},
        )
        claims = store.read_json("belief_state.json").get("claims", [])
        alpha_probe = alpha_scope.get("file_probe", {})
        beta_probe = beta_scope.get("file_probe", {})
        alpha_claim = next(
            (
                claim
                for claim in claims
                if isinstance(claim, dict)
                and claim.get("key") == "perception_alpha:status"
            ),
            {},
        )
        ownerless_claim = next(
            (
                claim
                for claim in claims
                if isinstance(claim, dict)
                and claim.get("key") == "perception_ownerless:status"
            ),
            {},
        )
        global_claim = next(
            (
                claim
                for claim in claims
                if isinstance(claim, dict)
                and claim.get("key") == "perception_global:status"
            ),
            {},
        )
        expect(
            alpha_probe.get("scope_kind") == "tenant"
            and alpha_probe.get("user_id") == "user-a"
            and alpha_probe.get("session_id") == SHARED_SESSION
            and alpha_claim.get("scope_kind") == "tenant"
            and alpha_claim.get("user_id") == "user-a"
            and alpha_claim.get("session_id") == SHARED_SESSION,
            "PerceptionLayer persists exact tenant scope on probe and claim",
            {"probe": alpha_probe, "claim": alpha_claim},
        )
        expect(
            beta_probe.get("summary") == "PERCEPTION_BETA_PROBE"
            and alpha_probe.get("summary") == "PERCEPTION_ALPHA_PROBE",
            "same probe name keeps independent tenant cache slots",
            {"alpha": alpha_probe, "beta": beta_probe},
        )
        expect(
            ownerless_claim.get("scope_kind") == "tenant"
            and ownerless_claim.get("scope_status") == "ownerless",
            "PerceptionLayer marks ownerless tenant claim fail-closed",
            ownerless_claim,
        )
        expect(
            global_claim.get("scope_kind") == "operator_global",
            "PerceptionLayer preserves explicit system-probe global rule",
            global_claim,
        )
        focus = ["codebase", "logs", "current_time", "perception"]
        patch_a = ContextPatchBuilder(store).build(
            "inspect perception scope",
            focus,
            event=event("user-a", event_id="evt-perception-a"),
        )
        patch_b = ContextPatchBuilder(store).build(
            "inspect perception scope",
            focus,
            event=event("user-b", event_id="evt-perception-b"),
        )
        serialized_a = json.dumps(patch_a, ensure_ascii=False)
        serialized_b = json.dumps(patch_b, ensure_ascii=False)
        expect(
            "PERCEPTION_ALPHA_PROBE" in serialized_a
            and "PERCEPTION_ALPHA_CLAIM" in serialized_a
            and "PERCEPTION_OWNERLESS" not in serialized_a
            and "PERCEPTION_GLOBAL_PROBE" in serialized_a
            and "PERCEPTION_GLOBAL_CLAIM" in serialized_a,
            "Perception scope reaches only exact owner plus operator-global context",
            patch_a,
        )
        expect(
            "PERCEPTION_ALPHA" not in serialized_b
            and "PERCEPTION_BETA_PROBE" in serialized_b
            and "PERCEPTION_BETA_CLAIM" in serialized_b
            and "PERCEPTION_OWNERLESS" not in serialized_b
            and "PERCEPTION_GLOBAL_PROBE" in serialized_b
            and "PERCEPTION_GLOBAL_CLAIM" in serialized_b,
            "same-session peer receives only its own Perception tenant data",
            patch_b,
        )
        assembler = AwarenessContextAssembler(store)
        awareness_a = assembler.snapshot(
            user_message="inspect perception scope",
            attention_focus=["perception"],
            user_id="user-a",
            session_id=SHARED_SESSION,
        )
        awareness_b = assembler.snapshot(
            user_message="inspect perception scope",
            attention_focus=["perception"],
            user_id="user-b",
            session_id=SHARED_SESSION,
        )
        awareness_serialized_a = json.dumps(
            awareness_a,
            ensure_ascii=False,
        )
        awareness_serialized_b = json.dumps(
            awareness_b,
            ensure_ascii=False,
        )
        expect(
            "PERCEPTION_ALPHA" in awareness_serialized_a
            and "PERCEPTION_OWNERLESS" not in awareness_serialized_a
            and "PERCEPTION_GLOBAL" in awareness_serialized_a
            and awareness_a.get("belief_summary", {}).get("total") == 2,
            "cognition awareness snapshot sees exact tenant plus global state",
            awareness_a,
        )
        expect(
            "PERCEPTION_ALPHA" not in awareness_serialized_b
            and "PERCEPTION_BETA" in awareness_serialized_b
            and "PERCEPTION_OWNERLESS" not in awareness_serialized_b
            and "PERCEPTION_GLOBAL" in awareness_serialized_b
            and awareness_b.get("belief_summary", {}).get("total") == 2,
            "cognition awareness snapshot cannot leak peer claim or counts",
            awareness_b,
        )


class ScopeReasoning:
    def external_world_assist(self, **_: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": "scope_smoke"}


def external_world_perception_scope_checks() -> None:
    with TemporaryDirectory(prefix="veyra-external-perception-scope-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json(
            "external_world.json",
            {
                "watchlist": [
                    {
                        "target": "tenant-private.example",
                        "kind": "network",
                        "enabled": True,
                        "user_id": "user-a",
                        "session_id": SHARED_SESSION,
                    },
                    {
                        "target": "localhost",
                        "kind": "network",
                        "enabled": True,
                        "scope_kind": "operator_global",
                    },
                ],
                "summaries": [],
                "knowledge_items": [],
                "push_candidates": [],
            },
        )
        refresh = ExternalWorldRefresh(
            store,
            reasoning=ScopeReasoning(),  # type: ignore[arg-type]
        )

        def fake_probe(target: str) -> dict[str, Any]:
            marker = (
                "TENANT_EXTERNAL_PROBE"
                if target == "tenant-private.example"
                else "OPERATOR_EXTERNAL_PROBE"
            )
            return {
                "probe": "network_probe",
                "source": "network_probe",
                "target": target,
                "status": "reachable",
                "summary": marker,
                "confidence": 0.9,
                "ttl_seconds": 300,
                "observed_at": utc_now_iso(),
                "model_assist": False,
            }

        refresh._probe_target = fake_probe  # type: ignore[method-assign]
        result = refresh.refresh_watchlist(limit=2)
        refreshed = result.get("refreshed", [])
        tenant_result = next(
            item
            for item in refreshed
            if item.get("target") == "tenant-private.example"
        )
        global_result = next(
            item for item in refreshed if item.get("target") == "localhost"
        )
        local_serialized = json.dumps(
            store.read_json("local_world.json"),
            ensure_ascii=False,
        )
        belief_serialized = json.dumps(
            store.read_json("belief_state.json"),
            ensure_ascii=False,
        )
        expect(
            tenant_result.get("scope_kind") == "tenant"
            and tenant_result.get("user_id") == "user-a"
            and tenant_result.get("session_id") == SHARED_SESSION
            and tenant_result.get("state_patch") == {},
            "owner-scoped ExternalWorld result stays in exact scoped summary",
            tenant_result,
        )
        expect(
            "TENANT_EXTERNAL_PROBE" not in local_serialized
            and "TENANT_EXTERNAL_PROBE" not in belief_serialized,
            "owner-scoped ExternalWorld never enters global Perception or Belief",
            {
                "local_world": store.read_json("local_world.json"),
                "belief_state": store.read_json("belief_state.json"),
            },
        )
        expect(
            global_result.get("scope_kind") == "operator_global"
            and bool(global_result.get("state_patch"))
            and "OPERATOR_EXTERNAL_PROBE" in local_serialized
            and "OPERATOR_EXTERNAL_PROBE" in belief_serialized,
            "explicit operator-global ExternalWorld probe keeps Perception behavior",
            global_result,
        )
        external = store.read_json("external_world.json")
        tenant_summary = next(
            item
            for item in external.get("summaries", [])
            if item.get("target") == "tenant-private.example"
        )
        expect(
            tenant_summary.get("scope_kind") == "tenant"
            and tenant_summary.get("user_id") == "user-a"
            and tenant_summary.get("session_id") == SHARED_SESSION,
            "ExternalWorld persisted summary retains exact owner",
            tenant_summary,
        )


def learning_memory_envelope_checks(store: WorldStateStore) -> None:
    loop = AwarenessLoop(store, RuntimeEntity(store))
    store.patch_json(
        "agent_memory.json",
        {
            "items": [
                {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "patch": {
                        "user_id": "user-b",
                        "session_id": SHARED_SESSION,
                        "memory_type": "learning_goal",
                        "topic": "conflicting-learning-topic",
                    },
                },
                {
                    "session_id": SHARED_SESSION,
                    "patch": {
                        "session_id": SHARED_SESSION,
                        "memory_type": "learning_goal",
                        "topic": "ownerless-learning-topic",
                    },
                },
            ]
        },
    )
    expect(
        loop._current_learning_topic("user-a", SHARED_SESSION) == "",
        "conflicting and ownerless learning memory fail closed",
    )

    def append_exact(state: dict[str, Any]) -> dict[str, Any]:
        items = state.get("items") if isinstance(state.get("items"), list) else []
        items.append(
            {
                "user_id": "user-a",
                "session_id": SHARED_SESSION,
                "patch": {
                    "user_id": "user-a",
                    "session_id": SHARED_SESSION,
                    "memory_type": "learning_goal",
                    "topic": "alpha-learning-topic",
                },
            }
        )
        state["items"] = items
        return state

    store.mutate_json("agent_memory.json", append_exact)
    expect(
        loop._current_learning_topic("user-a", SHARED_SESSION)
        == "alpha-learning-topic",
        "exact learning-memory envelope remains readable",
    )
    expect(
        loop._current_learning_topic("user-b", SHARED_SESSION) == "",
        "same-session user cannot read another learning-memory topic",
    )


def main() -> int:
    belief_scope_identity_checks()
    commitment_control_scope_checks()
    with TemporaryDirectory(prefix="veyra-memory-context-scope-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        memory_write_scope_checks(store)
        seed_scoped_context_state(store)
        context_isolation_checks(store)
        learning_memory_envelope_checks(store)
    perception_persistence_scope_checks()
    external_world_perception_scope_checks()
    print("memory context scope smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
