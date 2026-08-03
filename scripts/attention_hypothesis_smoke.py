#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.general_attention_scheduler import GeneralAttentionScheduler  # noqa: E402
from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import (  # noqa: E402
    DURABLE_STATE_FILES,
    STATE_FILE_LAYOUT,
    STATE_METADATA,
    WorldStateStore,
)
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import Route  # noqa: E402
from interface.general_situation_contract import (  # noqa: E402
    ChildSituationRef,
    stable_digest,
)
from runtime.attention_hypothesis_runtime import (  # noqa: E402
    AttentionHypothesisRuntime,
)
from runtime.event_awareness_runtime import ShadowAwarenessRuntime  # noqa: E402
from runtime.general_situation_runtime import GeneralSituationRuntime  # noqa: E402
from runtime.suggestion_outbox import SuggestionOutbox  # noqa: E402
from routers.debug_audit import build_debug_audit_router  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


def child_ref(
    index: int,
    *,
    revision: int = 1,
    namespace: str = "attention-smoke",
) -> dict[str, Any]:
    return {
        "situation_id": f"sit-{namespace}-{index}",
        "observation_revision": revision,
        "source_event_id": f"evt-{namespace}-{index}",
        "digest": stable_digest(
            "attention-hypothesis-smoke-child",
            {
                "namespace": namespace,
                "index": index,
                "revision": revision,
            },
        ),
    }


def parent_for(
    *,
    user_id: str,
    session_id: str,
    revision: int,
    child_count: int,
    clock: MutableClock,
    anchor: str = "goal:attention-smoke",
    general_id: str = "gsit-attention-smoke",
) -> dict[str, Any]:
    refs = [
        child_ref(index, revision=revision, namespace=general_id)
        for index in range(1, child_count + 1)
    ]
    return {
        "schema_version": GeneralSituationRuntime.RECORD_SCHEMA_VERSION,
        "general_situation_id": general_id,
        "user_id": user_id,
        "session_scope_keys": [
            tenant_scope_storage_key(user_id, session_id)
        ],
        "workspace_anchor_key": "workspace:veyra",
        "aggregation_scope": "exact_owner_session",
        "primary_anchor_key": anchor,
        "common_anchor_keys": sorted([anchor, "workspace:veyra"]),
        "child_refs": refs,
        "distinct_event_count": child_count,
        "parent_revision": revision,
        "status": "observed",
        "causality_asserted": False,
        "model_similarity_used_for_merge": False,
        "effective_start": clock().isoformat(),
        "effective_end": clock().isoformat(),
        "expires_at": (clock() + timedelta(days=7)).isoformat(),
        "created_at": clock().isoformat(),
        "updated_at": clock().isoformat(),
    }


def assessment_for(
    parent: dict[str, Any],
    *,
    values: dict[str, float],
    store: WorldStateStore,
    clock: MutableClock,
    structured_profile: bool = True,
    model_confidence: float | None = None,
    epistemic_status: str = "observed",
) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    raw_refs = copy.deepcopy(parent["child_refs"])
    for index, raw_ref in enumerate(raw_refs):
        occurred_at = clock() - timedelta(minutes=60 - 20 * index)
        fact_kind = "risk_signal" if index % 2 == 0 else "change_signal"
        child = {
            "record_kind": "situation_candidate",
            "situation_id": raw_ref["situation_id"],
            "correlation_id": raw_ref["source_event_id"],
            "user_id": parent["user_id"],
            "session_id": "fixture-session",
            "channel": (
                "structured_observation" if structured_profile else "api"
            ),
            "source_event": {
                "event_id": raw_ref["source_event_id"],
                "type": "observation",
                "channel": (
                    "structured_observation" if structured_profile else "api"
                ),
                "user_id": parent["user_id"],
                "session_id": "fixture-session",
                "occurred_at": occurred_at.isoformat(),
                "timestamp": occurred_at.isoformat(),
            },
            "source_event_id": raw_ref["source_event_id"],
            "source_event_type": "observation",
            "structured_anchor_refs": [
                {
                    "kind": parent["primary_anchor_key"].split(":", 1)[0],
                    "ref_id": parent["primary_anchor_key"].split(":", 1)[1],
                }
            ],
            "evidence_refs": [{"ref_id": f"evidence-{index}"}],
            "salience_components": {
                name: value
                for name, value in values.items()
                if name not in {"goal_priority", "freshness"}
            },
            "status": "observed",
            "observations": [
                {
                    "value": {
                        "schema_version": "veyra.structured_observation.fact.v1",
                        "producer_id": "local_operator",
                        "producer_receipt_id": f"receipt-{index}",
                        "fact_kind": fact_kind,
                        "fact_state": "degraded",
                        "epistemic_status": epistemic_status,
                        "is_fact": epistemic_status == "observed",
                        "valid_from": occurred_at.isoformat(),
                        "valid_until": (clock() + timedelta(days=7)).isoformat(),
                    }
                }
            ],
            "inferences": [],
            "observation_revision": raw_ref["observation_revision"],
            "created_at": occurred_at.isoformat(),
            "updated_at": occurred_at.isoformat(),
        }
        children.append(child)
        refs.append(
            {
                "situation_id": child["situation_id"],
                "observation_revision": child["observation_revision"],
                "source_event_id": child["source_event_id"],
                "digest": GeneralSituationRuntime.child_digest(child),
            }
        )
    parent["child_refs"] = refs
    parent["distinct_event_count"] = len(
        {item["source_event_id"] for item in refs}
    )

    def persist_children(state: dict[str, Any]) -> dict[str, Any]:
        existing = (
            {
                str(item.get("situation_id") or ""): item
                for item in state.get("situations", [])
                if isinstance(item, dict)
            }
            if isinstance(state.get("situations"), list)
            else {}
        )
        existing.update({item["situation_id"]: item for item in children})
        state["situations"] = list(existing.values())
        state["count"] = len(existing)
        return state

    store.mutate_json("situation_state.json", persist_children)
    if "goal_priority" in values:
        goal_id = parent["primary_anchor_key"].split(":", 1)[1]

        def persist_goal(state: dict[str, Any]) -> dict[str, Any]:
            goals = [
                item
                for item in state.get("goals", [])
                if isinstance(item, dict)
                and not (
                    item.get("goal_id") == goal_id
                    and item.get("user_id") == parent["user_id"]
                )
            ]
            goals.append(
                {
                    "goal_id": goal_id,
                    "user_id": parent["user_id"],
                    "status": "active",
                    "goal_priority": values["goal_priority"],
                }
            )
            state["goals"] = goals
            return state

        store.mutate_json("user_goals.json", persist_goal)
    persist_parent(store, parent)
    output = GeneralAttentionScheduler(store, clock=clock).assess(parent)
    if model_confidence is not None:
        output["model_confidence"] = model_confidence
    return output


def persist_parent(store: WorldStateStore, parent: dict[str, Any]) -> None:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        parents = (
            copy.deepcopy(state.get("general_situations"))
            if isinstance(state.get("general_situations"), dict)
            else {}
        )
        parents[str(parent["general_situation_id"])] = copy.deepcopy(parent)
        state["schema_version"] = GeneralSituationRuntime.SCHEMA_VERSION
        state["general_situations"] = parents
        state["general_situation_count"] = len(parents)
        return state

    store.mutate_json(GeneralSituationRuntime.STATE_FILE, mutate)


LOW_VALUES = {
    "severity": 0.40,
    "urgency": 0.40,
    "novelty": 0.40,
    "freshness": 0.90,
}
CONFIRMING_VALUES = {
    "goal_priority": 0.90,
    "severity": 0.90,
    "urgency": 0.90,
}


def main() -> int:
    with TemporaryDirectory(prefix="veyra-attention-hypothesis-") as tmp:
        state_root = Path(tmp) / "state"
        store = WorldStateStore(state_root)
        clock = MutableClock(
            datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
        )
        runtime = AttentionHypothesisRuntime(store, clock=clock)
        user_id = "attention-owner"
        session_id = "attention-session"

        expect(
            "attention_hypothesis_state.json" in STATE_FILE_LAYOUT
            and "attention_hypothesis_state.json" in STATE_METADATA
            and "attention_hypothesis_state.json" in DURABLE_STATE_FILES
            and store.read_json("attention_hypothesis_state.json").get(
                "ttl_seconds"
            )
            == 0
            and "attention_hypothesis_state"
            in {
                item.get("id")
                for item in store.read_json("state_schema.json").get(
                    "state_definitions", []
                )
                if isinstance(item, dict)
            },
            "state contract is defaulted, metadata-backed, and durable",
        )

        first_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=clock,
        )
        persist_parent(store, first_parent)
        first = runtime.observe(
            first_parent,
            assessment_for(
                first_parent,
                values=LOW_VALUES,
                store=store,
                clock=clock,
            ),
        )
        expect(
            first.get("status") == "candidate"
            and first.get("evidence_added_count") == 2
            and first.get("surface_assessment", {}).get("eligible") is False
            and first.get("hypothesis", {}).get("evidence_count") == 2,
            "first partial assessment creates a non-eligible candidate",
            first,
        )

        second_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=2,
            child_count=3,
            clock=clock,
        )
        persist_parent(store, second_parent)
        second = runtime.observe(
            second_parent,
            assessment_for(
                second_parent,
                values=LOW_VALUES,
                store=store,
                clock=clock,
            ),
        )
        expect(
            second.get("status") == "accumulating"
            and second.get("evidence_added_count") == 1
            and second.get("hypothesis", {}).get("evidence_count") == 3,
            "new immutable child evidence advances candidate to accumulating",
            second,
        )

        third_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=3,
            child_count=4,
            clock=clock,
        )
        confirming = assessment_for(
            third_parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
            model_confidence=0.01,
        )
        persist_parent(store, third_parent)
        third = runtime.observe(third_parent, confirming)
        readiness = third.get("hypothesis", {}).get("attention_readiness", {})
        expect(
            third.get("status") == "confirmed"
            and third.get("surface_assessment", {}).get("status") == "eligible"
            and third.get("surface_assessment", {}).get("eligible") is True
            and third.get("surface_assessment", {})
            .get("attention_hypothesis_ref", {})
            .get("hypothesis_id")
            == third.get("hypothesis", {}).get("hypothesis_id")
            and third.get("surface_assessment", {})
            .get("attention_hypothesis_ref", {})
            .get("hypothesis_revision")
            == third.get("hypothesis", {}).get("hypothesis_revision")
            and third.get("surface_assessment", {})
            .get("attention_hypothesis_ref", {})
            .get("ruleset_version")
            == AttentionHypothesisRuntime.RULESET_VERSION
            and third.get("surface_assessment", {})
            .get("attention_hypothesis_ref", {})
            .get("readiness_semantics")
            == "attention_policy_readiness_not_factual_probability"
            and readiness.get("component_coverage", 0)
            >= AttentionHypothesisRuntime.MIN_COMPONENT_COVERAGE
            and readiness.get("value", 0)
            >= AttentionHypothesisRuntime.MIN_PARTIAL_WEIGHTED_SCORE
            and readiness.get("confirmation_blockers") == []
            and readiness.get("is_probability") is False
            and readiness.get("is_fact") is False
            and third.get("hypothesis", {}).get("model_confidence_used") is False
            and all(
                value is False
                for value in third.get("hypothesis", {})
                .get("authority", {})
                .values()
            ),
            "transparent partial score confirms without model confidence or authority",
            third,
        )

        revision_before_replay = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        replay_assessment = copy.deepcopy(confirming)
        replay_assessment["model_confidence"] = 0.99
        replay = runtime.observe(third_parent, replay_assessment)
        revision_after_replay = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        expect(
            replay.get("status") == "confirmed"
            and replay.get("replayed") is True
            and replay.get("evidence_added_count") == 0
            and revision_before_replay == revision_after_replay,
            "duplicate evidence and model confidence changes are byte-pure replay",
            replay,
        )

        clock.advance(seconds=1)
        semantic_revision_before = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        semantic_replay = runtime.observe(
            third_parent,
            GeneralAttentionScheduler(store, clock=clock).assess(third_parent),
        )
        semantic_revision_after = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        expect(
            semantic_replay.get("status") == "confirmed"
            and semantic_replay.get("replayed") is True
            and semantic_replay.get("surface_assessment", {}).get("eligible")
            is True
            and semantic_revision_before == semantic_revision_after,
            "freshness-only drift with unchanged blockers is byte-pure replay",
            semantic_replay,
        )

        counter_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=clock,
            anchor="goal:counterexample",
            general_id="gsit-counterexample",
        )
        persist_parent(store, counter_parent)
        counter = runtime.observe(
            counter_parent,
            assessment_for(
                counter_parent,
                values=CONFIRMING_VALUES,
                store=store,
                clock=clock,
                structured_profile=False,
                model_confidence=1.0,
            ),
        )
        expect(
            counter.get("status") == "candidate"
            and counter.get("surface_assessment", {}).get("eligible") is False
            and "structured_evidence_profile_incomplete"
            in counter.get("hypothesis", {})
            .get("attention_readiness", {})
            .get("confirmation_blockers", []),
            "untyped evidence blocks a high-score counterexample",
            counter,
        )

        owner_parent = parent_for(
            user_id="other-owner",
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-other-owner",
        )
        persist_parent(store, owner_parent)
        owner_result = runtime.observe(
            owner_parent,
            assessment_for(
                owner_parent,
                values=LOW_VALUES,
                store=store,
                clock=clock,
            ),
        )
        session_parent = parent_for(
            user_id=user_id,
            session_id="other-session",
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-other-session",
        )
        persist_parent(store, session_parent)
        session_result = runtime.observe(
            session_parent,
            assessment_for(
                session_parent,
                values=LOW_VALUES,
                store=store,
                clock=clock,
            ),
        )
        ids = {
            third.get("hypothesis", {}).get("hypothesis_id"),
            owner_result.get("hypothesis", {}).get("hypothesis_id"),
            session_result.get("hypothesis", {}).get("hypothesis_id"),
        }
        expect(
            len(ids) == 3
            and store.read_json("attention_hypothesis_state.json").get(
                "hypothesis_count"
            )
            == 4,
            "general Situation lineage keeps owner and session projections isolated",
            ids,
        )

        state_path = store.path_for("attention_hypothesis_state.json")
        bytes_before_read = state_path.read_bytes()
        state_revision_before_read = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        owner_view = runtime.list_for_owner(
            user_id=user_id,
            session_id=session_id,
        )
        foreign_owner_view = runtime.list_for_owner(
            user_id="other-owner",
            session_id=session_id,
        )
        foreign_session_view = runtime.list_for_owner(
            user_id=user_id,
            session_id="unrelated-session",
        )
        visible_ids = {
            item.get("hypothesis_id") for item in owner_view.get("items", [])
        }
        expect(
            owner_view.get("status") == "success"
            and owner_view.get("hypothesis_count") == 2
            and owner_view.get("status_counts")
            == {"candidate": 1, "accumulating": 0, "confirmed": 1}
            and third.get("hypothesis", {}).get("hypothesis_id") in visible_ids
            and counter.get("hypothesis", {}).get("hypothesis_id") in visible_ids
            and foreign_owner_view.get("hypothesis_count") == 1
            and foreign_session_view.get("hypothesis_count") == 0
            and all(
                not {
                    "user_id",
                    "session_scope_keys",
                    "identity",
                    "identity_digest",
                    "last_assessment_digest",
                }.intersection(item)
                for item in owner_view.get("items", [])
                if isinstance(item, dict)
            )
            and state_path.read_bytes() == bytes_before_read
            and int(owner_view.get("state_revision") or 0)
            == state_revision_before_read,
            "exact-owner list is byte-pure and does not leak other sessions",
            {
                "owner": owner_view,
                "foreign_owner": foreign_owner_view,
                "foreign_session": foreign_session_view,
            },
        )

        route_app = FastAPI()
        route_app.include_router(
            build_debug_audit_router(
                {
                    "awareness_loop": SimpleNamespace(
                        event_awareness=SimpleNamespace(
                            attention_hypotheses=runtime,
                        )
                    )
                }
            )
        )
        route_client = TestClient(route_app)
        bytes_before_http = state_path.read_bytes()
        list_response = route_client.get(
            "/awareness/attention-hypotheses",
            params={"user_id": user_id, "session_id": session_id},
        )
        status_response = route_client.get(
            "/awareness/attention-hypotheses/status"
        )
        expect(
            list_response.status_code == 200
            and list_response.json().get("hypothesis_count") == 2
            and status_response.status_code == 200
            and status_response.json().get("hypothesis_count") == 4
            and state_path.read_bytes() == bytes_before_http,
            "AttentionHypothesis HTTP reads are scoped and byte-pure",
            {
                "list": list_response.json(),
                "status": status_response.json(),
            },
        )

        restart_store = WorldStateStore(state_root)
        restarted = AttentionHypothesisRuntime(restart_store, clock=clock)
        restart_revision = int(
            store.read_json("attention_hypothesis_state.json").get(
                "_state_revision"
            )
            or 0
        )
        restart_assessment = GeneralAttentionScheduler(
            restart_store,
            clock=clock,
        ).assess(third_parent)
        after_restart = restarted.observe(third_parent, restart_assessment)
        expect(
            after_restart.get("status") == "confirmed"
            and after_restart.get("replayed") is True
            and restarted.status().get("hypothesis_count") == 4
            and int(
                store.read_json("attention_hypothesis_state.json").get(
                    "_state_revision"
                )
                or 0
            )
            == restart_revision,
            "durable ledger survives restart and preserves replay purity",
            after_restart,
        )

        protected_before = {
            name: copy.deepcopy(store.read_json(name))
            for name in ("task_state.json", "risk_state.json", "agent_config.json")
        }
        awareness = ShadowAwarenessRuntime(store)
        awareness.attention_hypotheses = runtime
        awareness.suggestion_outbox = SuggestionOutbox(store, clock=clock)
        awareness.general_situations = SimpleNamespace(
            ingest_child=lambda *_args, **_kwargs: {
                "status": "replayed",
                "general_situation": copy.deepcopy(third_parent),
            }
        )
        awareness.general_attention = SimpleNamespace(
            assess=lambda *_args, **_kwargs: GeneralAttentionScheduler(
                store,
                clock=clock,
            ).assess(third_parent)
        )
        event = EventNormalizer().user_message(
            "route boundary",
            "attention-hypothesis-smoke",
            user_id,
            session_id,
            occurred_at=clock().isoformat(),
            received_at=clock().isoformat(),
        )
        selected_route = Route.DIRECT_ANSWER
        projection = awareness._project_general_situation({}, event)
        protected_after = {
            name: copy.deepcopy(store.read_json(name))
            for name in protected_before
        }
        expect(
            projection.get("status") == "success"
            and projection.get("hypothesis_status") == "confirmed"
            and projection.get("route_change_allowed") is False
            and selected_route is Route.DIRECT_ANSWER
            and protected_before == protected_after,
            "ShadowAwareness seam surfaces confirmed assessment without route or risk change",
            projection,
        )

        semantic_root = Path(tmp) / "semantic-corrupt-state"
        semantic_store = WorldStateStore(semantic_root)
        semantic_runtime = AttentionHypothesisRuntime(
            semantic_store,
            clock=clock,
        )
        persist_parent(semantic_store, first_parent)
        semantic_runtime.observe(
            first_parent,
            assessment_for(
                first_parent,
                values=LOW_VALUES,
                store=semantic_store,
                clock=clock,
            ),
        )

        def corrupt_semantics(state: dict[str, Any]) -> None:
            record = next(iter(state["hypotheses"].values()))
            record["attention_readiness"]["is_probability"] = True

        semantic_store.mutate_json(
            "attention_hypothesis_state.json",
            corrupt_semantics,
        )
        semantic_path = semantic_store.path_for(
            "attention_hypothesis_state.json"
        )
        semantic_bytes = semantic_path.read_bytes()
        semantic_status = semantic_runtime.status()
        semantic_observe = semantic_runtime.observe(
            first_parent,
            assessment_for(
                first_parent,
                values=LOW_VALUES,
                store=semantic_store,
                clock=clock,
            ),
        )
        expect(
            semantic_status.get("status") == "fail_closed"
            and semantic_observe.get("status") == "fail_closed"
            and semantic_path.read_bytes() == semantic_bytes,
            "semantic ledger corruption freezes status and observation",
            {
                "status": semantic_status,
                "observe": semantic_observe,
            },
        )

        scope_corrupt_root = Path(tmp) / "scope-corrupt-state"
        scope_corrupt_store = WorldStateStore(scope_corrupt_root)
        scope_corrupt_runtime = AttentionHypothesisRuntime(
            scope_corrupt_store,
            clock=clock,
        )
        scope_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-scope-corruption",
        )
        scope_corrupt_runtime.observe(
            scope_parent,
            assessment_for(
                scope_parent,
                values=LOW_VALUES,
                store=scope_corrupt_store,
                clock=clock,
            ),
        )
        injected_session = "injected-session"

        def corrupt_scope_projection(state: dict[str, Any]) -> None:
            record = next(iter(state["hypotheses"].values()))
            record["session_scope_keys"].append(
                tenant_scope_storage_key(user_id, injected_session)
            )
            record["session_scope_keys"].sort()

        scope_corrupt_store.mutate_json(
            "attention_hypothesis_state.json",
            corrupt_scope_projection,
        )
        scope_corrupt_path = scope_corrupt_store.path_for(
            "attention_hypothesis_state.json"
        )
        scope_corrupt_bytes = scope_corrupt_path.read_bytes()
        scope_corrupt_status = scope_corrupt_runtime.status()
        injected_view = scope_corrupt_runtime.list_for_owner(
            user_id=user_id,
            session_id=injected_session,
        )
        expect(
            scope_corrupt_status.get("status") == "fail_closed"
            and injected_view.get("status") == "fail_closed"
            and injected_view.get("items") == []
            and scope_corrupt_path.read_bytes() == scope_corrupt_bytes,
            "session projection corruption cannot expand hypothesis visibility",
            {
                "status": scope_corrupt_status,
                "injected_view": injected_view,
            },
        )

        unknown_field_root = Path(tmp) / "unknown-field-state"
        unknown_field_store = WorldStateStore(unknown_field_root)
        unknown_field_runtime = AttentionHypothesisRuntime(
            unknown_field_store,
            clock=clock,
        )
        unknown_field_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-unknown-record-field",
        )
        unknown_field_runtime.observe(
            unknown_field_parent,
            assessment_for(
                unknown_field_parent,
                values=LOW_VALUES,
                store=unknown_field_store,
                clock=clock,
            ),
        )
        raw_private_sentinel = "RAW-PRIVATE-ATTENTION-SENTINEL"

        def inject_unknown_record_field(state: dict[str, Any]) -> None:
            record = next(iter(state["hypotheses"].values()))
            record["raw_user_text"] = raw_private_sentinel

        unknown_field_store.mutate_json(
            AttentionHypothesisRuntime.STATE_FILE,
            inject_unknown_record_field,
        )
        unknown_field_path = unknown_field_store.path_for(
            AttentionHypothesisRuntime.STATE_FILE
        )
        unknown_field_bytes = unknown_field_path.read_bytes()
        unknown_field_status = unknown_field_runtime.status()
        unknown_field_view = unknown_field_runtime.list_for_owner(
            user_id=user_id,
            session_id=session_id,
        )
        unknown_public_text = json.dumps(
            {
                "status": unknown_field_status,
                "view": unknown_field_view,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            unknown_field_status.get("status") == "fail_closed"
            and unknown_field_view.get("status") == "fail_closed"
            and unknown_field_view.get("items") == []
            and raw_private_sentinel not in unknown_public_text
            and "raw_user_text" not in unknown_public_text
            and unknown_field_path.read_bytes() == unknown_field_bytes,
            "unknown hypothesis record fields fail closed without privacy leak",
            {
                "status": unknown_field_status,
                "view": unknown_field_view,
            },
        )

        identity_root = Path(tmp) / "identity-continuity-state"
        identity_store = WorldStateStore(identity_root)
        identity_clock = MutableClock(clock())
        identity_runtime = AttentionHypothesisRuntime(
            identity_store,
            clock=identity_clock,
        )
        identity_first_parent = parent_for(
            user_id=user_id,
            session_id="identity-session-a",
            revision=1,
            child_count=2,
            clock=identity_clock,
            general_id="gsit-identity-continuity",
        )
        identity_first_parent["common_anchor_keys"].append("entity:transient")
        identity_first_parent["common_anchor_keys"].sort()
        identity_first = identity_runtime.observe(
            identity_first_parent,
            assessment_for(
                identity_first_parent,
                values=CONFIRMING_VALUES,
                store=identity_store,
                clock=identity_clock,
            ),
        )
        identity_second_parent = parent_for(
            user_id=user_id,
            session_id="identity-session-a",
            revision=2,
            child_count=2,
            clock=identity_clock,
            general_id="gsit-identity-continuity",
        )
        identity_second_parent["session_scope_keys"].append(
            tenant_scope_storage_key(user_id, "identity-session-b")
        )
        identity_second_parent["session_scope_keys"].sort()
        identity_second = identity_runtime.observe(
            identity_second_parent,
            assessment_for(
                identity_second_parent,
                values=CONFIRMING_VALUES,
                store=identity_store,
                clock=identity_clock,
            ),
        )
        expect(
            identity_first.get("status") == "confirmed"
            and identity_second.get("status") == "confirmed"
            and identity_first.get("hypothesis", {}).get("hypothesis_id")
            == identity_second.get("hypothesis", {}).get("hypothesis_id")
            and identity_runtime.status().get("hypothesis_count") == 1,
            "mutable session and common-anchor projections do not fork identity",
            {
                "first": identity_first,
                "second": identity_second,
            },
        )

        stale_root = Path(tmp) / "stale-first-delivery-state"
        stale_store = WorldStateStore(stale_root)
        stale_clock = MutableClock(clock())
        stale_runtime = AttentionHypothesisRuntime(stale_store, clock=stale_clock)
        stale_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=stale_clock,
            general_id="gsit-stale-first",
        )
        stale_assessment = assessment_for(
            stale_parent,
            values=LOW_VALUES,
            store=stale_store,
            clock=stale_clock,
        )
        durable_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=2,
            child_count=3,
            clock=stale_clock,
            general_id="gsit-stale-first",
        )
        assessment_for(
            durable_parent,
            values=LOW_VALUES,
            store=stale_store,
            clock=stale_clock,
        )
        stale_path = stale_store.path_for("attention_hypothesis_state.json")
        stale_bytes = stale_path.read_bytes()
        stale_first = stale_runtime.observe(stale_parent, stale_assessment)
        expect(
            stale_first.get("status") == "fail_closed"
            and stale_first.get("reason")
            == "attention_parent_revision_out_of_order"
            and stale_runtime.status().get("hypothesis_count") == 0
            and stale_path.read_bytes() == stale_bytes,
            "first delivery of an old durable parent revision is byte-pure rejected",
            stale_first,
        )

        age_root = Path(tmp) / "assessment-age-state"
        age_store = WorldStateStore(age_root)
        age_clock = MutableClock(clock())
        age_runtime = AttentionHypothesisRuntime(age_store, clock=age_clock)
        age_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=age_clock,
            general_id="gsit-assessment-age",
        )
        aged_assessment = assessment_for(
            age_parent,
            values=LOW_VALUES,
            store=age_store,
            clock=age_clock,
        )
        age_clock.advance(
            seconds=AttentionHypothesisRuntime.MAX_ASSESSMENT_ADMISSION_AGE_SECONDS
            + 1
        )
        age_path = age_store.path_for("attention_hypothesis_state.json")
        age_bytes = age_path.read_bytes()
        aged = age_runtime.observe(age_parent, aged_assessment)
        expect(
            aged.get("status") == "fail_closed"
            and aged.get("reason")
            == "general_attention_assessment_outside_admission_window"
            and age_path.read_bytes() == age_bytes,
            "backdated assessment cannot revive evidence outside admission window",
            aged,
        )

        decay_root = Path(tmp) / "freshness-decay-state"
        decay_store = WorldStateStore(decay_root)
        decay_clock = MutableClock(clock())
        decay_runtime = AttentionHypothesisRuntime(decay_store, clock=decay_clock)
        decay_parent = parent_for(
            user_id=user_id,
            session_id=session_id,
            revision=1,
            child_count=2,
            clock=decay_clock,
            general_id="gsit-freshness-decay",
        )
        decay_initial = decay_runtime.observe(
            decay_parent,
            assessment_for(
                decay_parent,
                values={
                    "goal_priority": 1.0,
                    "severity": 0.5,
                    "urgency": 0.333333,
                },
                store=decay_store,
                clock=decay_clock,
            ),
        )
        decay_clock.advance(hours=25)
        decay_result = decay_runtime.observe(
            decay_parent,
            GeneralAttentionScheduler(
                decay_store,
                clock=decay_clock,
            ).assess(decay_parent),
        )
        expect(
            decay_initial.get("status") == "confirmed"
            and decay_result.get("status") == "accumulating"
            and decay_result.get("replayed") is False
            and decay_result.get("surface_assessment", {}).get("eligible")
            is False
            and "partial_weighted_score_below_threshold"
            in decay_result.get("hypothesis", {})
            .get("attention_readiness", {})
            .get("confirmation_blockers", []),
            "freshness decay withdraws confirmation instead of replaying old eligibility",
            {
                "initial": decay_initial,
                "after_decay": decay_result,
            },
        )

        corrupt_root = Path(tmp) / "corrupt-state"
        corrupt_store = WorldStateStore(corrupt_root)
        corrupt_runtime = AttentionHypothesisRuntime(corrupt_store, clock=clock)
        persist_parent(corrupt_store, first_parent)
        corrupt_store.path_for("attention_hypothesis_state.json").write_text(
            "{broken",
            encoding="utf-8",
        )
        corrupt = corrupt_runtime.observe(
            first_parent,
            assessment_for(
                first_parent,
                values=LOW_VALUES,
                store=corrupt_store,
                clock=clock,
            ),
        )
        expect(
            corrupt.get("status") == "fail_closed"
            and corrupt.get("reason") == "attention_hypothesis_state_corrupt"
            and corrupt.get("surface_assessment", {}).get("eligible") is False
            and corrupt_store.path_for("attention_hypothesis_state.json").read_text(
                encoding="utf-8"
            )
            == "{broken",
            "corrupt ledger fails closed without repair or suggestion eligibility",
            corrupt,
        )

        revision_root = Path(tmp) / "revision-corrupt-state"
        revision_store = WorldStateStore(revision_root)
        revision_runtime = AttentionHypothesisRuntime(
            revision_store,
            clock=clock,
        )
        revision_path = revision_store.path_for(
            "attention_hypothesis_state.json"
        )
        revision_payload = json.loads(revision_path.read_text(encoding="utf-8"))
        revision_payload["_state_revision"] = "garbage"
        revision_path.write_text(
            json.dumps(revision_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        revision_bytes = revision_path.read_bytes()
        revision_status = revision_runtime.status()
        expect(
            revision_status.get("status") == "fail_closed"
            and revision_status.get("reason")
            == "attention_hypothesis_state_corrupt"
            and revision_path.read_bytes() == revision_bytes,
            "invalid ledger state revision is rejected without mutation",
            revision_status,
        )

    print("attention hypothesis smoke passed: 21/21")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
