#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from core.world_state import WorldStateStore  # noqa: E402
from core.context_scope import tenant_scope_storage_key  # noqa: E402
from awareness.general_attention_scheduler import GeneralAttentionScheduler  # noqa: E402
from interface.cognitive_brief_contract import CognitiveBrief  # noqa: E402
from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime  # noqa: E402
from scripts.attention_hypothesis_smoke import (  # noqa: E402
    CONFIRMING_VALUES,
    MutableClock,
    assessment_for,
    parent_for,
    persist_parent,
)
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.read_only_cognitive_loop import (  # noqa: E402
    ReadOnlyCognitiveLoopRuntime,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        self.invalid_plan = False
        self.empty_plan_unsupported = False
        self.output_locators = False

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict[str, Any]:
        self.calls.append(purpose)
        payload = json.loads(user)
        self.payloads.append(payload)
        if purpose == "background_cognitive_observation_plan":
            if self.invalid_plan:
                tokens = ["cogview_not_supplied"]
            elif self.empty_plan_unsupported:
                tokens = []
            else:
                tokens = [
                    item["opportunity_token"]
                    for item in payload["opportunities"]
                    if item["kind"] in {"belief_freshness", "situation_graph"}
                ][:2]
            return {
                "status": "model_assisted",
                "cognitive_plan": {
                    "schema_version": "veyra.cognitive_observation_plan.v1",
                    "selected_opportunity_tokens": tokens,
                    "objective": "check whether evidence or the situation graph materially changed",
                    "expected_information_gain": "freshness and contextual continuity",
                    "source": "model",
                },
            }
        if purpose == "background_cognitive_brief":
            refs = [
                ref
                for item in payload["selected_observations"]
                for ref in item["evidence_refs"]
            ]
            evidence_ref = refs[0] if refs else "view:fabricated:unsupported"
            has_previous = isinstance(payload.get("previous_brief"), dict)
            summary_if_asked = (
                "Review https://model.example/private?q=secret"
                if self.output_locators
                else (
                    "Belief freshness changed and should remain under observation."
                    if has_previous
                    else "A private baseline of current cognition state was recorded."
                )
            )
            known_statement = (
                "A cached view mentions ../private/model-output"
                if self.output_locators
                else "The supplied WorldState view has a bounded freshness summary."
            )
            return {
                "status": "model_assisted",
                "cognitive_brief": {
                    "schema_version": "veyra.cognitive_brief.v1",
                    "disposition": "record_candidate" if has_previous else "quiet",
                    "summary_if_asked": summary_if_asked,
                    "known": [
                        {
                            "statement": known_statement,
                            "evidence_refs": [evidence_ref],
                            "confidence": 0.9,
                        }
                    ],
                    "unknown": ["whether the change is useful enough to interrupt the user"],
                    "assumptions": [],
                    "material_changes": (
                        [
                            {
                                "kind": "belief_freshness_change",
                                "subject": "belief_state",
                                "statement": "The bounded freshness counts changed since the previous brief.",
                                "evidence_refs": [evidence_ref],
                                "why_now": "The server supplied a new state revision in this cycle.",
                                "confidence": 0.8,
                            }
                        ]
                        if has_previous
                        else []
                    ),
                    "why_now": (
                        "The evidence signature changed after the baseline."
                        if has_previous
                        else ""
                    ),
                    "confidence": 0.82,
                    "source": "model",
                },
            }
        raise AssertionError(f"unexpected purpose: {purpose}")


class FakeReasoning:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.traces: list[tuple[str, dict[str, Any]]] = []

    def is_enabled(self) -> bool:
        return True

    def _trace(self, purpose: str, result: dict[str, Any], metadata: dict[str, Any]) -> None:
        self.traces.append((purpose, metadata))


class TraceFailStore(WorldStateStore):
    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        if name == "core_model_trace.jsonl":
            raise OSError("simulated trace sink failure")
        super().append_jsonl(name, payload)


class MalformedConfigStore(WorldStateStore):
    def __init__(self, root: Path) -> None:
        self._malformed_config_enabled = False
        super().__init__(root)
        self._malformed_config_enabled = True

    def read_json(self, name: str) -> dict[str, Any]:
        payload = super().read_json(name)
        if self._malformed_config_enabled and name == "ops_config.json":
            payload["_state_revision"] = {"not": "an integer"}
        return payload


def make_owner_scope(store: WorldStateStore) -> None:
    def mutate(state: dict[str, Any]) -> None:
        state["threads"] = {
            "ctx_cognitive_smoke": {
                "schema_version": "veyra.context_thread.v1",
                "context_id": "ctx_cognitive_smoke",
                "user_id": "cognitive-user",
                "session_id": "cognitive-session",
                "target_type": "project",
                "label": "Veyra cognition",
                "subject_digest": "a" * 64,
                "epistemic_status": "context_hypothesis",
                "is_fact": False,
                "authority": False,
                "confidence": 0.9,
                "status": "corroborated",
                "source_event_ids": ["evt_1", "evt_2"],
                "expires_at": "2099-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        }
        state["thread_count"] = 1

    store.mutate_json("context_binding_state.json", mutate)


def seed_scope_isolation_views(store: WorldStateStore) -> None:
    user_id = "cognitive-user"
    session_id = "cognitive-session"
    peer_session = "peer-session"
    own_scope = tenant_scope_storage_key(user_id, session_id)
    peer_scope = tenant_scope_storage_key(user_id, peer_session)

    store.patch_json(
        "belief_state.json",
        {
            "claims": [
                {
                    "key": "own:claim",
                    "claim": "OWN_VISIBLE_CLAIM",
                    "source": "event",
                    "confidence": 0.9,
                    "source_trust": 0.7,
                    "status": "fresh",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": user_id,
                    "session_id": session_id,
                },
                {
                    "key": "peer:claim",
                    "claim": "PEER_SESSION_SECRET_CLAIM",
                    "source": "event",
                    "confidence": 0.9,
                    "source_trust": 0.7,
                    "status": "fresh",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": user_id,
                    "session_id": peer_session,
                },
                {
                    "key": "system:claim",
                    "claim": "OPERATOR_GLOBAL_VISIBLE",
                    "source": "system_probe",
                    "confidence": 0.9,
                    "source_trust": 0.82,
                    "status": "fresh",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "scope_kind": "operator_global",
                },
            ],
            # A deliberately wrong global summary proves the cognitive view is
            # recomputed after exact-scope filtering.
            "summary": {"total": 999, "fresh": 999},
        },
    )
    store.patch_json(
        "local_world.json",
        {
            "probes": {
                "system_probe": {
                    "probe": "system_probe",
                    "source": "system_probe",
                    "scope_kind": "operator_global",
                    "status": "ok",
                }
            },
            "scoped_probes": {
                own_scope: {
                    "https://internal.example/private/path": {
                        "probe": "https://internal.example/private/path",
                        "source": "internal.example",
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": user_id,
                        "session_id": session_id,
                        "status": "OWN_VISIBLE_PROBE",
                    }
                },
                peer_scope: {
                    "file_probe": {
                        "probe": "file_probe",
                        "source": "file_probe",
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": user_id,
                        "session_id": peer_session,
                        "status": "PEER_SESSION_SECRET_PROBE",
                    }
                },
            },
        },
    )
    store.patch_json(
        "user_goals.json",
        {
            "goals": [
                {
                    "goal_id": "goal_PRIVATE_ID",
                    "user_id": user_id,
                    "session_id": session_id,
                    "title": "OWN_VISIBLE_GOAL",
                    "status": "active",
                },
                {
                    "goal_id": "goal_PEER_SECRET_ID",
                    "user_id": user_id,
                    "session_id": peer_session,
                    "title": "PEER_SESSION_SECRET_GOAL",
                    "status": "completed",
                },
            ]
        },
    )
    store.patch_json(
        "user_commitments.json",
        {
            "commitments": [
                {
                    "commitment_id": "cmt_PRIVATE_ID",
                    "user_id": user_id,
                    "session_id": session_id,
                    "title": "OWN_VISIBLE_COMMITMENT",
                    "status": "active",
                },
                {
                    "commitment_id": "cmt_PEER_SECRET_ID",
                    "user_id": user_id,
                    "session_id": peer_session,
                    "title": "PEER_SESSION_SECRET_COMMITMENT",
                    "status": "cancelled",
                },
            ]
        },
    )

    def add_bindings(state: dict[str, Any]) -> None:
        state["bindings"] = {
            "evt_own": {
                "event_id": "evt_own",
                "user_id": user_id,
                "session_id": session_id,
                "resolution_status": "bound",
            },
            "evt_peer": {
                "event_id": "evt_peer",
                "user_id": user_id,
                "session_id": peer_session,
                "resolution_status": "unresolved",
            },
        }
        state["coverage"] = {
            "eligible": 999,
            "bound": 0,
            "unresolved": 999,
            "bound_rate": 0.0,
        }

    store.mutate_json("context_binding_state.json", add_bindings)
    store.patch_json(
        "general_situation_state.json",
        {
            "general_situations": {
                "gs_own": {
                    "user_id": user_id,
                    "session_scope_keys": [own_scope],
                },
                "gs_peer": {
                    "user_id": user_id,
                    "session_scope_keys": [peer_scope],
                },
            }
        },
    )
    store.patch_json(
        "external_world.json",
        {
            "summaries": [
                {
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": user_id,
                    "session_id": session_id,
                    "kind": "external_search",
                    "topic": "OWN_EXTERNAL_TOPIC",
                    "status": "ok",
                    "summary": (
                        "OWN_EXTERNAL_VISIBLE from "
                        "https://internal.example/private?q=secret"
                    ),
                    "observed_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": user_id,
                    "session_id": peer_session,
                    "kind": "external_search",
                    "topic": "PEER_SESSION_SECRET_EXTERNAL",
                    "status": "ok",
                    "summary": "PEER_SESSION_SECRET_EXTERNAL",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                },
            ],
            "knowledge_items": [],
            "push_candidates": [],
        },
    )


def age_last_cycle(store: WorldStateStore) -> None:
    def mutate(state: dict[str, Any]) -> None:
        for collection in ("scopes", "continuity"):
            for scope in (state.get(collection) or {}).values():
                if isinstance(scope, dict):
                    scope["last_model_at"] = "2020-01-01T00:00:00+00:00"

    store.mutate_json("cognitive_loop_state.json", mutate)


def test_cognitive_brief_attention_bridge() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-brief-bridge-") as tmp:
        clock = MutableClock(
            datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        )
        store = WorldStateStore(tmp)
        parent = parent_for(
            user_id="bridge-user",
            session_id="bridge-session",
            revision=1,
            child_count=2,
            clock=clock,
            anchor="goal:bridge",
            general_id="gsit-brief-bridge",
        )
        persist_parent(store, parent)
        assessment_for(
            parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        )
        runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=store,
            reasoning=FakeReasoning(FakeClient()),
        )
        evidence_refs = ["view:situation_graph:bridge-evidence"]
        cycle = {
            "candidate_recorded": True,
            "user_id": "bridge-user",
            "session_id": "bridge-session",
            "cycle_id": "cog_bbbbbbbbbbbbbbbb",
            "world_digest": "a" * 64,
            "selected_observations": [
                {
                    "kind": "situation_graph",
                    "evidence_refs": evidence_refs,
                    "payload": {
                        "attention_parent": {
                            "general_situation_id": "gsit-brief-bridge",
                            "parent_revision": 1,
                        }
                    },
                }
            ],
            "brief": {
                "material_changes": [
                    {
                        "evidence_refs": evidence_refs,
                        "kind": "structured_situation_change",
                    }
                ]
            },
        }
        bridge_ref = runtime._projection_ref(
            "situation_graph",
            cycle["selected_observations"][0]["payload"],
        )  # noqa: SLF001
        cycle["selected_observations"][0]["evidence_refs"] = [bridge_ref]
        cycle["brief"]["material_changes"][0]["evidence_refs"] = [bridge_ref]
        admitted = runtime._bridge_candidate_to_attention(cycle)  # noqa: SLF001
        first_revision = int(
            store.read_json(AttentionHypothesisRuntime.STATE_FILE).get(
                "_state_revision"
            )
            or 0
        )
        replayed = runtime._bridge_candidate_to_attention(cycle)  # noqa: SLF001
        second_revision = int(
            store.read_json(AttentionHypothesisRuntime.STATE_FILE).get(
                "_state_revision"
            )
            or 0
        )
        assert admitted.get("status") == "confirmed", admitted
        assert admitted.get("general_situation_id") == "gsit-brief-bridge"
        assert admitted.get("parent_revision") == 1
        assert admitted.get("authority") is False
        assert replayed.get("status") == "confirmed", replayed
        assert first_revision == second_revision
        assert (
            store.read_json(AttentionHypothesisRuntime.STATE_FILE).get(
                "hypothesis_count"
            )
            == 1
        )
        bridge_state = store.read_json("cognitive_loop_state.json")
        bridge_bindings = bridge_state.get("bridge_bindings") or {}
        assert len(bridge_bindings) == 1
        binding = next(iter(bridge_bindings.values()))
        assert binding.get("status") == "committed"
        assert binding.get("hypothesis_id") == admitted.get("hypothesis_id")
        assert binding.get("general_situation_id") == "gsit-brief-bridge"
        assert binding.get("parent_revision") == 1

        # A later terminal lifecycle revision is valid Attention history, but
        # it is not the revision this bridge committed. Replay must therefore
        # keep the exact bridge receipt and omit the newer child rather than
        # presenting revision 2 as if it were the bound revision 1.
        attention_runtime = AttentionHypothesisRuntime(store)
        current_assessment = GeneralAttentionScheduler(store).assess(parent)
        current_hypothesis = admitted["attention_hypothesis"]
        terminal = attention_runtime.observe(
            parent,
            current_assessment,
            lifecycle_signal={
                "schema_version": AttentionHypothesisRuntime.LIFECYCLE_SIGNAL_SCHEMA_VERSION,
                "signal_id": "als_bridge_revision_advanced",
                "kind": "contradiction",
                "target_hypothesis_id": binding["hypothesis_id"],
                "target_hypothesis_revision": binding["hypothesis_revision"],
                "evidence_id": "evidence:bridge_revision_advanced",
                "reason_code": "direct_counter_observation",
                "producer_id": "local_operator",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        terminal_replay = runtime._public_bridge(  # noqa: SLF001
            binding,
            admitted=None,
        )
        assert current_hypothesis["hypothesis_revision"] == 1
        assert terminal.get("status") == "contradicted", terminal
        assert terminal["hypothesis"]["hypothesis_revision"] == 2
        assert terminal_replay.get("status") == "committed", terminal_replay
        assert terminal_replay.get("hypothesis_revision") == 1
        assert terminal_replay.get("attention_hypothesis") is None

        # Simulate a process crash after the durable prepared phase but before
        # Attention admission.  Active-loop reconciliation must retry the
        # exact cycle binding rather than leave the row pending forever.
        durable_payload = cycle["selected_observations"][0]["payload"]
        durable_ref = runtime._projection_ref("situation_graph", durable_payload)  # noqa: SLF001
        durable_cycle = {
            **cycle,
            "status": "observed",
            "reason": "smoke",
            "mode": "record_only",
            "created_at": clock().isoformat(),
            "selected_opportunity_tokens": ["cogview_bridge"],
            "selected_kinds": ["situation_graph"],
            "evidence_refs": [durable_ref],
            "selected_observations": [
                {
                    **cycle["selected_observations"][0],
                    "evidence_refs": [durable_ref],
                }
            ],
            "baseline": False,
            "model_calls": 0,
            "epistemic_status": "hypothesis",
            "is_fact": False,
            "authority": False,
            "external_delivery": False,
            "agent_execution": False,
            "tool_execution": False,
            "route_change_allowed": False,
            "risk_change_allowed": False,
        }
        durable_cycle["brief"] = {
            **cycle["brief"],
            "material_changes": [
                {
                    **cycle["brief"]["material_changes"][0],
                    "evidence_refs": [durable_ref],
                }
            ],
        }
        durable_scope_key = tenant_scope_storage_key("bridge-user", "bridge-session")
        store.mutate_json(
            "cognitive_loop_state.json",
            lambda state: {
                **state,
                "scopes": {
                    durable_scope_key: {
                        "user_id": "bridge-user",
                        "session_id": "bridge-session",
                        "cycles": [durable_cycle],
                        "cycle_count": 1,
                        "last_cycle_id": durable_cycle["cycle_id"],
                        "last_model_at": durable_cycle["created_at"],
                        "last_checked_at": durable_cycle["created_at"],
                        "updated_at": durable_cycle["created_at"],
                        "last_world_digest": durable_cycle["world_digest"],
                        "last_brief": durable_cycle["brief"],
                    }
                },
                "scope_count": 1,
            },
        )
        store.mutate_json(
            "cognitive_loop_state.json",
            lambda state: {
                **state,
                "metrics": runtime._metrics(  # noqa: SLF001
                    state.get("scopes") or {},
                    state.get("continuity") or {},
                ),
            },
        )
        prepared = {
            **binding,
            "status": "prepared",
            "hypothesis_revision": None,
            "committed_at": None,
            "rejection_reason": None,
        }
        store.mutate_json(
            "cognitive_loop_state.json",
            lambda state: {
                **state,
                "bridge_bindings": {binding["binding_id"]: prepared},
                "bridge_binding_count": 1,
            },
        )
        store.mutate_json(
            AttentionHypothesisRuntime.STATE_FILE,
            lambda state: {
                **state,
                "hypotheses": {},
                "identity_index": {},
                "hypothesis_count": 0,
                "status_counts": {
                    "candidate": 0,
                    "accumulating": 0,
                    "confirmed": 0,
                    "contradicted": 0,
                    "expired": 0,
                    "superseded": 0,
                },
            },
        )
        runtime._reconcile_attention_bridges()  # noqa: SLF001
        recovered_state = store.read_json("cognitive_loop_state.json")
        recovered_binding = recovered_state["bridge_bindings"][binding["binding_id"]]
        assert recovered_binding.get("status") == "committed", recovered_binding
        assert (
            store.read_json(AttentionHypothesisRuntime.STATE_FILE).get("hypothesis_count")
            == 1
        )

        # A same-ID binding with a forged parent revision is rejected rather
        # than reconciled merely because the hypothesis ID still exists.
        tampered = {
            **recovered_binding,
            "status": "prepared",
            "parent_revision": 999,
            "hypothesis_revision": None,
            "committed_at": None,
            "rejection_reason": None,
        }
        store.mutate_json(
            "cognitive_loop_state.json",
            lambda state: {
                **state,
                "bridge_bindings": {binding["binding_id"]: tampered},
                "bridge_binding_count": 1,
            },
        )
        runtime._reconcile_attention_bridges()  # noqa: SLF001
        rejected_binding = store.read_json("cognitive_loop_state.json")["bridge_bindings"][binding["binding_id"]]
        assert rejected_binding.get("status") == "rejected", rejected_binding
        assert rejected_binding.get("rejection_reason"), rejected_binding

        # If a process crashes after admission and the cycle is no longer
        # recoverable, reconciliation must close admitted rev2 as rejected
        # rev3 while retaining the already-bound hypothesis revision.
        admitted_orphan = {
            **recovered_binding,
            "status": "admitted",
            "phase_revision": 2,
            "committed_at": None,
            "rejection_reason": None,
        }

        def orphan_bridge(state: dict[str, Any]) -> dict[str, Any]:
            continuity = (
                state.get("continuity")
                if isinstance(state.get("continuity"), dict)
                else {}
            )
            state["scopes"] = {}
            state["scope_count"] = 0
            state["bridge_bindings"] = {
                admitted_orphan["binding_id"]: admitted_orphan
            }
            state["bridge_binding_count"] = 1
            state["metrics"] = runtime._metrics({}, continuity)  # noqa: SLF001
            return state

        store.mutate_json("cognitive_loop_state.json", orphan_bridge)
        runtime._reconcile_attention_bridges()  # noqa: SLF001
        orphan_rejected = store.read_json("cognitive_loop_state.json")[
            "bridge_bindings"
        ][admitted_orphan["binding_id"]]
        assert orphan_rejected.get("status") == "rejected", orphan_rejected
        assert orphan_rejected.get("phase_revision") == 3, orphan_rejected
        assert (
            orphan_rejected.get("hypothesis_revision")
            == admitted_orphan.get("hypothesis_revision")
        ), orphan_rejected


def test_admitted_bridge_crash_recovery() -> None:
    """A crash after durable admission resumes with only the commit CAS."""

    with tempfile.TemporaryDirectory(prefix="veyra-admitted-bridge-crash-") as tmp:
        clock = MutableClock(
            datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc)
        )
        store = WorldStateStore(tmp)
        parent = parent_for(
            user_id="admitted-crash-user",
            session_id="admitted-crash-session",
            revision=1,
            child_count=2,
            clock=clock,
            anchor="goal:admitted-crash",
            general_id="gsit-admitted-crash",
        )
        persist_parent(store, parent)
        assessment_for(
            parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        )
        runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=store,
            reasoning=FakeReasoning(FakeClient()),
        )
        projection_payload = {
            "attention_parent": {
                "general_situation_id": parent["general_situation_id"],
                "parent_revision": parent["parent_revision"],
            }
        }
        projection_ref = runtime._projection_ref(  # noqa: SLF001
            "situation_graph",
            projection_payload,
        )
        created_at = clock().isoformat()
        cycle = {
            "candidate_recorded": True,
            "user_id": "admitted-crash-user",
            "session_id": "admitted-crash-session",
            "cycle_id": "cog_dadadadadadadada",
            "world_digest": "d" * 64,
            "status": "observed",
            "reason": "smoke",
            "mode": "record_only",
            "created_at": created_at,
            "selected_opportunity_tokens": ["cogview_admitted_crash"],
            "selected_kinds": ["situation_graph"],
            "evidence_refs": [projection_ref],
            "selected_observations": [
                {
                    "kind": "situation_graph",
                    "evidence_refs": [projection_ref],
                    "payload": projection_payload,
                }
            ],
            "brief": {
                "material_changes": [
                    {
                        "kind": "structured_situation_change",
                        "evidence_refs": [projection_ref],
                    }
                ]
            },
            "baseline": False,
            "model_calls": 0,
            "epistemic_status": "hypothesis",
            "is_fact": False,
            "authority": False,
            "external_delivery": False,
            "agent_execution": False,
            "tool_execution": False,
            "route_change_allowed": False,
            "risk_change_allowed": False,
        }
        scope_key = tenant_scope_storage_key(
            cycle["user_id"],
            cycle["session_id"],
        )

        def seed_cycle(state: dict[str, Any]) -> dict[str, Any]:
            state["scopes"] = {
                scope_key: {
                    "user_id": cycle["user_id"],
                    "session_id": cycle["session_id"],
                    "cycles": [cycle],
                    "cycle_count": 1,
                    "last_cycle_id": cycle["cycle_id"],
                    "last_model_at": created_at,
                    "last_checked_at": created_at,
                    "updated_at": created_at,
                    "last_world_digest": cycle["world_digest"],
                    "last_brief": cycle["brief"],
                }
            }
            state["scope_count"] = 1
            state["metrics"] = runtime._metrics(  # noqa: SLF001
                state["scopes"],
                state.get("continuity") or {},
            )
            return state

        store.mutate_json("cognitive_loop_state.json", seed_cycle)
        persist_bridge = runtime._persist_bridge_binding  # noqa: SLF001

        def crash_after_admitted(
            binding: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            persist_bridge(binding, **kwargs)
            if binding.get("status") == "admitted":
                raise RuntimeError("simulated crash before bridge commit")

        runtime._persist_bridge_binding = crash_after_admitted  # type: ignore[method-assign]  # noqa: SLF001
        interrupted = runtime._bridge_candidate_to_attention(cycle)  # noqa: SLF001
        runtime._persist_bridge_binding = persist_bridge  # type: ignore[method-assign]  # noqa: SLF001
        assert interrupted.get("status") == "rejected", interrupted
        assert interrupted.get("reason") == "attention_bridge_persistence_rejected"

        interrupted_state = store.read_json("cognitive_loop_state.json")
        admitted = next(iter(interrupted_state["bridge_bindings"].values()))
        assert admitted.get("status") == "admitted", admitted
        assert admitted.get("phase_revision") == 2, admitted
        assert admitted.get("hypothesis_revision") == 1, admitted
        attention_before = store.read_json(AttentionHypothesisRuntime.STATE_FILE)
        hypothesis_before = attention_before["hypotheses"][admitted["hypothesis_id"]]
        assert hypothesis_before.get("hypothesis_revision") == 1

        runtime._reconcile_attention_bridges()  # noqa: SLF001
        recovered_state = store.read_json("cognitive_loop_state.json")
        committed = recovered_state["bridge_bindings"][admitted["binding_id"]]
        assert committed.get("status") == "committed", committed
        assert committed.get("phase_revision") == 3, committed
        assert committed.get("hypothesis_revision") == 1, committed
        assert committed.get("rejection_reason") is None, committed
        assert committed.get("committed_at"), committed
        assert (
            store.read_json(AttentionHypothesisRuntime.STATE_FILE)
            == attention_before
        ), "admitted recovery must not rewrite the durable Attention row"
        recovered_cycle = recovered_state["scopes"][scope_key]["cycles"][-1]
        assert recovered_cycle["attention_bridge"]["binding_status"] == "committed"
        assert recovered_cycle["attention_bridge"]["phase_revision"] == 3


def main() -> int:
    test_cognitive_brief_attention_bridge()
    test_admitted_bridge_crash_recovery()
    with tempfile.TemporaryDirectory(prefix="veyra-cognitive-loop-") as tmp:
        store = WorldStateStore(tmp)
        cognitive_config = store.read_json("ops_config.json")["cognitive_loop"]
        assert cognitive_config == {
            "mode": "record_only",
            "allowed_modes": ["disabled", "record_only"],
            "min_interval_seconds": 900,
            "daily_model_cycle_budget": 24,
            "max_owner_scopes_per_tick": 1,
        }
        make_owner_scope(store)
        seed_scope_isolation_views(store)
        client = FakeClient()
        reasoning = FakeReasoning(client)
        runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=store,
            reasoning=reasoning,
        )

        opportunities = runtime._opportunities(  # noqa: SLF001
            cycle_id="cog_scope_audit",
            user_id="cognitive-user",
            session_id="cognitive-session",
        )
        by_kind = {item["kind"]: item["payload"] for item in opportunities}
        serialized_views = json.dumps(opportunities, ensure_ascii=False)
        assert "PEER_SESSION_SECRET" not in serialized_views
        assert "goal_PRIVATE_ID" not in serialized_views
        assert "cmt_PRIVATE_ID" not in serialized_views
        assert "ctx_cognitive_smoke" not in serialized_views
        assert "internal.example" not in serialized_views
        assert "https://" not in serialized_views
        assert by_kind["belief_freshness"]["summary"]["total"] == 2
        assert by_kind["local_sensor_index"]["probe_count"] == 2
        assert len(by_kind["goals_and_commitments"]["goals"]) == 1
        assert len(by_kind["goals_and_commitments"]["commitments"]) == 1
        assert by_kind["situation_graph"]["coverage"] == {
            "eligible": 1,
            "bound": 1,
            "unresolved": 0,
            "bound_rate": 1.0,
            "overconservative_alert": False,
        }
        assert by_kind["situation_graph"]["general_situation_count"] == 1
        assert by_kind["situation_graph"]["attention_parent"] is None
        assert by_kind["external_world_changes"]["summary_count"] == 1
        assert "OWN_EXTERNAL_VISIBLE" in serialized_views

        baseline = runtime.run_once(reason="smoke")
        assert baseline["status"] == "observed", baseline
        assert baseline["results"][0]["reason"] == "baseline_recorded"
        assert baseline["results"][0]["candidate_recorded"] is False
        assert baseline["external_delivery"] is False
        assert baseline["agent_execution"] is False
        assert baseline["tool_execution"] is False
        assert client.calls == [
            "background_cognitive_observation_plan",
            "background_cognitive_brief",
        ]
        model_prompts = json.dumps(client.payloads, ensure_ascii=False)
        assert "cognitive-user" not in model_prompts
        assert "cognitive-session" not in model_prompts
        assert "PEER_SESSION_SECRET" not in model_prompts
        assert "internal.example" not in model_prompts
        assert "https://" not in model_prompts

        # The interval budget is enforced before a model call.
        too_soon = runtime.run_once(reason="smoke")
        assert too_soon["status"] == "idle", too_soon
        assert len(client.calls) == 2

        # A changed server revision after the interval creates only a private
        # record-only candidate; it does not surface or execute anything.
        age_last_cycle(store)

        def change_belief(state: dict[str, Any]) -> None:
            claims = state.get("claims") if isinstance(state.get("claims"), list) else []
            claims.append(
                {
                    "key": "own:changed-claim",
                    "claim": "OWN_VISIBLE_CHANGED_CLAIM",
                    "source": "event",
                    "confidence": 0.8,
                    "source_trust": 0.7,
                    "status": "fresh",
                    "observed_at": "2026-01-02T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": "cognitive-user",
                    "session_id": "cognitive-session",
                }
            )
            state["claims"] = claims

        store.mutate_json("belief_state.json", change_belief)
        changed = runtime.run_once(reason="smoke")
        assert changed["status"] == "observed", changed
        assert changed["results"][0]["candidate_recorded"] is True
        assert changed["results"][0]["reason"] == "record_candidate"
        assert changed["results"][0]["attention_bridge"]["status"] == "rejected"
        assert changed["results"][0]["attention_bridge"]["reason"] == "material_change_not_bound_to_parent"
        assert changed["external_delivery"] is False
        state = store.read_json("cognitive_loop_state.json")
        assert state["metrics"]["cycle_count"] == 2
        assert state["metrics"]["candidate_count"] == 1
        assert state["metrics"]["model_call_count"] == 4
        scope = next(iter(state["scopes"].values()))
        candidate_cycle = scope["cycles"][-1]
        assert candidate_cycle["epistemic_status"] == "hypothesis"
        assert candidate_cycle["is_fact"] is False
        assert candidate_cycle["authority"] is False
        assert candidate_cycle["external_delivery"] is False
        assert 0 < len(candidate_cycle["selected_observations"]) <= 2
        for selected_observation in candidate_cycle["selected_observations"]:
            expected_ref = runtime._projection_ref(  # noqa: SLF001
                selected_observation["kind"],
                selected_observation["payload"],
            )
            assert selected_observation["evidence_refs"] == [expected_ref]
            assert expected_ref in candidate_cycle["evidence_refs"]
        successful_digest = scope["last_world_digest"]
        successful_brief = scope["last_brief"]

        # With no evidence-revision change, even an elapsed interval causes no
        # model call.  Comparison uses structured evidence, not prose diff.
        age_last_cycle(store)
        unchanged = runtime.run_once(reason="smoke")
        assert unchanged["status"] == "unchanged", unchanged
        assert len(client.calls) == 4

        # An invented observation token fails closed and records no brief or
        # candidate.  It cannot escape into an effect path.
        age_last_cycle(store)
        store.patch_json("executor_state.json", {"selected_agent": "negative-test-agent"})
        client.invalid_plan = True
        invalid = runtime.run_once(reason="smoke")
        assert invalid["status"] == "degraded", invalid
        failed_state = store.read_json("cognitive_loop_state.json")
        failed_scope = next(iter(failed_state["scopes"].values()))
        failed_cycle = failed_scope["cycles"][-1]
        assert failed_cycle["status"] == "degraded"
        assert failed_cycle["brief"] is None
        assert failed_cycle["candidate_recorded"] is False
        assert failed_cycle["external_delivery"] is False
        assert failed_cycle["tool_execution"] is False
        assert failed_scope["last_world_digest"] == successful_digest
        assert failed_scope["last_brief"] == successful_brief

        # Selecting no view cannot turn the comparison digest into evidence.
        # Unsupported model knowledge is rejected and the last good brief is
        # retained byte-for-byte.
        client.invalid_plan = False
        client.empty_plan_unsupported = True
        age_last_cycle(store)
        store.patch_json(
            "executor_state.json",
            {"selected_agent": "empty-plan-negative-test-agent"},
        )
        empty_evidence = runtime.run_once(reason="smoke")
        assert empty_evidence["status"] == "degraded", empty_evidence
        empty_evidence_state = store.read_json("cognitive_loop_state.json")
        empty_evidence_scope = next(iter(empty_evidence_state["scopes"].values()))
        assert empty_evidence_scope["last_world_digest"] == successful_digest
        assert empty_evidence_scope["last_brief"] == successful_brief

        # Invalid output consumes the bounded model budget but cannot mark the
        # changed evidence as processed or erase the last valid brief. The same
        # evidence is retried after the interval and can recover normally.
        client.empty_plan_unsupported = False
        age_last_cycle(store)
        recovered = runtime.run_once(reason="smoke")
        assert recovered["status"] == "observed", recovered
        assert recovered["results"][0]["reason"] == "record_candidate"
        recovered_state = store.read_json("cognitive_loop_state.json")
        recovered_scope = next(iter(recovered_state["scopes"].values()))
        assert recovered_scope["cycles"][-1]["status"] == "observed"
        assert recovered_scope["last_world_digest"] != successful_digest
        assert len(client.calls) == 9

        # The shared model trace contains transport evidence only. It must not
        # become a global owner-content log.
        model_trace = json.dumps(
            store.read_jsonl("core_model_trace.jsonl", limit=100),
            ensure_ascii=False,
        )
        assert "cognitive-user" not in model_trace
        assert "cognitive-session" not in model_trace
        assert "OWN_VISIBLE" not in model_trace
        assert "PRIVATE_ID" not in model_trace
        assert "supplied WorldState" not in model_trace
        assert "scope-" not in model_trace
        runtime._trace_transport(  # noqa: SLF001
            purpose="background_cognitive_observation_plan",
            result={
                "status": "model_assisted",
                "_model": {
                    "provider": "openai_compatible",
                    "model": "kimi@secret.example",
                },
            },
            cycle_id="cog_0123456789abcdef",
            scope_ref="not-persisted",
            metadata={"opportunity_count": 1},
        )
        hostile_transport_trace = store.read_jsonl(
            "core_model_trace.jsonl",
            limit=1,
        )[0]
        assert hostile_transport_trace["result"]["_model"]["model"] == ""
        assert "secret.example" not in json.dumps(
            hostile_transport_trace,
            ensure_ascii=False,
        )

        # Status is a byte-pure projection and labels product usefulness as
        # shadow-sample evaluation rather than VERIFIED.
        state_path = Path(tmp) / "runtime" / "cognitive_loop_state.json"
        before = state_path.read_bytes()
        status = runtime.status()
        after = state_path.read_bytes()
        assert before == after
        assert status["metrics"]["evaluation_kind"] == (
            "shadow_samples_not_verified_product_quality"
        )
        assert status["observation_boundary"] == (
            "server_prepared_cached_views_only"
        )

        # A durable active Goal is independently sufficient to create an
        # exact cognitive scope; Context hypotheses are not a prerequisite.
        goal_only_store = WorldStateStore(Path(tmp) / "goal-only")
        goal_only_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_server_private",
                        "user_id": "goal-owner",
                        "session_id": "goal-session",
                        "title": "Goal-backed cognition",
                        "status": "active",
                    }
                ]
            },
        )
        goal_only_client = FakeClient()
        goal_only_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=goal_only_store,
            reasoning=FakeReasoning(goal_only_client),
        )
        goal_only = goal_only_runtime.run_once(reason="smoke")
        assert goal_only["status"] == "observed", goal_only
        assert len(goal_only_client.calls) == 2

        # Locator-shaped model output is stored and replayed only after the
        # same bounded sanitation applied to cached external input.
        output_store = WorldStateStore(Path(tmp) / "model-output-safety")
        output_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_output_safety",
                        "user_id": "output-owner",
                        "session_id": "output-session",
                        "title": "Output safety",
                        "status": "active",
                    }
                ]
            },
        )
        output_client = FakeClient()
        output_client.output_locators = True
        output_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=output_store,
            reasoning=FakeReasoning(output_client),
        )
        assert output_runtime.run_once(reason="smoke")["status"] == "observed"
        persisted_output = json.dumps(
            output_store.read_json("cognitive_loop_state.json"),
            ensure_ascii=False,
        )
        assert "model.example" not in persisted_output
        assert "../private" not in persisted_output
        age_last_cycle(output_store)
        output_store.patch_json(
            "executor_state.json",
            {"selected_agent": "output-safety-change"},
        )
        assert output_runtime.run_once(reason="smoke")["status"] == "observed"
        replayed_output_prompts = json.dumps(
            output_client.payloads,
            ensure_ascii=False,
        )
        assert "model.example" not in replayed_output_prompts
        assert "../private" not in replayed_output_prompts

        # A scope hash never substitutes for the exact owner envelope. A
        # corrupted/misbound previous brief cannot cross into another owner.
        mismatch_store = WorldStateStore(Path(tmp) / "owner-mismatch")
        mismatch_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_victim",
                        "user_id": "victim-owner",
                        "session_id": "victim-session",
                        "title": "Victim goal",
                        "status": "active",
                    }
                ]
            },
        )
        victim_scope = tenant_scope_storage_key(
            "victim-owner",
            "victim-session",
        )
        mismatch_store.patch_json(
            "cognitive_loop_state.json",
            {
                "scopes": {
                    victim_scope: {
                        "user_id": "different-owner",
                        "session_id": "different-session",
                        "last_model_at": "2020-01-01T00:00:00+00:00",
                        "last_brief": {
                            "summary_if_asked": "CROSS_OWNER_PRIVATE_SECRET"
                        },
                    }
                }
            },
        )
        mismatch_client = FakeClient()
        mismatch_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=mismatch_store,
            reasoning=FakeReasoning(mismatch_client),
        )
        owner_mismatch = mismatch_runtime.run_once(reason="smoke")
        assert owner_mismatch["status"] == "degraded", owner_mismatch
        assert mismatch_client.calls == []
        assert "CROSS_OWNER_PRIVATE_SECRET" not in json.dumps(
            mismatch_client.payloads,
            ensure_ascii=False,
        )

        # Semantically invalid source/state records fail closed before a
        # provider call. Private persisted metrics are never reflected by the
        # public status projection.
        bad_source_store = WorldStateStore(Path(tmp) / "bad-source")
        bad_source_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_bad_source",
                        "user_id": "bad-source-owner",
                        "session_id": "bad-source-session",
                        "title": "Bad source",
                        "status": "active",
                    }
                ]
            },
        )
        bad_source_store.patch_json("belief_state.json", {"claims": {}})
        bad_source_client = FakeClient()
        bad_source_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=bad_source_store,
            reasoning=FakeReasoning(bad_source_client),
        )
        bad_source_result = bad_source_runtime.run_once(reason="smoke")
        assert bad_source_result["status"] == "degraded", bad_source_result
        assert bad_source_result["results"][0]["reason"] == (
            "cognitive_source_state_invalid"
        )
        assert bad_source_client.calls == []
        assert bad_source_store.read_json("cognitive_loop_state.json")[
            "metrics"
        ]["cycle_count"] == 0

        private_metrics_store = WorldStateStore(Path(tmp) / "private-metrics")
        private_metrics_store.patch_json(
            "cognitive_loop_state.json",
            {
                "metrics": {
                    "cycle_count": 0,
                    "secret_owner_brief": "ALICE_PRIVATE_SECRET",
                }
            },
        )
        private_metrics_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=private_metrics_store,
            reasoning=FakeReasoning(FakeClient()),
        )
        private_metrics_status = private_metrics_runtime.status()
        assert private_metrics_status["status"] == "degraded"
        assert "ALICE_PRIVATE_SECRET" not in json.dumps(
            private_metrics_status,
            ensure_ascii=False,
        )

        malformed_attempt_store = WorldStateStore(
            Path(tmp) / "malformed-attempt"
        )
        malformed_scope = tenant_scope_storage_key(
            "malformed-owner",
            "malformed-session",
        )
        malformed_attempt_store.patch_json(
            "cognitive_loop_state.json",
            {
                "continuity": {
                    malformed_scope: {
                        "user_id": "malformed-owner",
                        "session_id": "malformed-session",
                        "attempts": [
                            {
                                "cycle_id": "cog_0123456789abcdef",
                                "status": "reserved",
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "model_calls": "bad",
                                "baseline": False,
                                "candidate_recorded": False,
                            }
                        ],
                    }
                },
                "continuity_count": 1,
            },
        )
        malformed_attempt_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=malformed_attempt_store,
            reasoning=FakeReasoning(FakeClient()),
        )
        assert malformed_attempt_runtime.status()["status"] == "degraded"

        # A failed global transport trace still consumes one private attempt
        # and starts the normal interval backoff; a broken audit sink cannot
        # bypass the model-call budget.
        trace_fail_store = TraceFailStore(Path(tmp) / "trace-fail")
        trace_fail_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_trace_fail",
                        "user_id": "trace-owner",
                        "session_id": "trace-session",
                        "title": "Trace failure budget",
                        "status": "active",
                    }
                ]
            },
        )
        trace_fail_client = FakeClient()
        trace_fail_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=trace_fail_store,
            reasoning=FakeReasoning(trace_fail_client),
        )
        trace_failed = trace_fail_runtime.run_once(reason="smoke")
        assert trace_failed["status"] == "degraded", trace_failed
        trace_failed_state = trace_fail_store.read_json(
            "cognitive_loop_state.json"
        )
        assert trace_failed_state["metrics"]["cycle_count"] == 1
        assert trace_failed_state["metrics"]["model_call_count"] == 1
        assert len(trace_fail_client.calls) == 1
        trace_fail_runtime.run_once(reason="smoke")
        assert len(trace_fail_client.calls) == 1

        # last_checked_at, not latest source activity, rotates due owners when
        # the first scope's evidence digest is unchanged.
        fair_store = WorldStateStore(Path(tmp) / "fair-scheduler")
        fair_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_a",
                        "user_id": "fair-owner-a",
                        "session_id": "fair-session-a",
                        "title": "A",
                        "status": "active",
                    },
                    {
                        "goal_id": "goal_b",
                        "user_id": "fair-owner-b",
                        "session_id": "fair-session-b",
                        "title": "B",
                        "status": "active",
                    },
                ]
            },
        )
        fair_a = tenant_scope_storage_key("fair-owner-a", "fair-session-a")
        fair_b = tenant_scope_storage_key("fair-owner-b", "fair-session-b")
        fair_store.patch_json(
            "cognitive_loop_state.json",
            {
                "scopes": {
                    fair_a: {
                        "user_id": "fair-owner-a",
                        "session_id": "fair-session-a",
                        "last_model_at": "2020-01-01T00:00:00+00:00",
                        "last_checked_at": "2020-01-01T00:00:00+00:00",
                        "cycles": [],
                    },
                    fair_b: {
                        "user_id": "fair-owner-b",
                        "session_id": "fair-session-b",
                        "last_model_at": "2020-01-01T00:00:00+00:00",
                        "last_checked_at": "2021-01-01T00:00:00+00:00",
                        "cycles": [],
                    },
                }
            },
        )
        fair_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=fair_store,
            reasoning=FakeReasoning(FakeClient()),
        )
        assert fair_runtime._owner_scopes(limit=1) == [  # noqa: SLF001
            ("fair-owner-a", "fair-session-a")
        ]
        fair_runtime._mark_checked(  # noqa: SLF001
            scope_key=fair_a,
            user_id="fair-owner-a",
            session_id="fair-session-a",
            world_digest="unchanged-a",
            checked_at="2090-01-01T00:00:00+00:00",
            expected_config=fair_runtime._config(),  # noqa: SLF001
            expected_generation=0,
        )
        assert fair_runtime._owner_scopes(limit=1) == [  # noqa: SLF001
            ("fair-owner-b", "fair-session-b")
        ]

        # The persistent scheduler cursor survives private scope eviction, so
        # active owners beyond MAX_SCOPES are not starved by re-admitted old
        # scopes becoming "unseen" again.
        cap_store = WorldStateStore(Path(tmp) / "scope-cap")
        cap_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": f"goal_cap_{index:02d}",
                        "user_id": f"cap-owner-{index:02d}",
                        "session_id": f"cap-session-{index:02d}",
                        "title": f"Capability owner {index:02d}",
                        "status": "active",
                    }
                    for index in range(55)
                ]
            },
        )
        def cap_budget(config: dict[str, Any]) -> None:
            config["cognitive_loop"]["daily_model_cycle_budget"] = 1

        cap_store.mutate_json("ops_config.json", cap_budget)
        cap_client = FakeClient()
        cap_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=cap_store,
            reasoning=FakeReasoning(cap_client),
        )
        cap_seen: set[str] = set()
        for _ in range(55):
            cap_result = cap_runtime.run_once(reason="smoke")
            assert cap_result["status"] == "observed", cap_result
            cap_seen.add(cap_result["results"][0]["user_id"])
        assert len(cap_seen) == 55, sorted(cap_seen)
        cap_state = cap_store.read_json("cognitive_loop_state.json")
        assert len(cap_state["scopes"]) == 50
        assert len(cap_state["continuity"]) == 55
        assert cap_state["metrics"]["cycle_count"] == 55
        assert cap_state["metrics"]["model_call_count"] == 110
        assert len(cap_client.calls) == 110
        for _ in range(55):
            assert cap_runtime.run_once(reason="smoke")["status"] == "idle"
        assert len(cap_client.calls) == 110
        cap_state_after_second_sweep = cap_store.read_json(
            "cognitive_loop_state.json"
        )
        assert cap_state_after_second_sweep["metrics"]["cycle_count"] == 55
        assert all(
            len(scope["attempts"]) == 1
            and isinstance(scope.get("last_brief"), dict)
            for scope in cap_state_after_second_sweep["continuity"].values()
        )
        evicted_scope_key = next(
            key
            for key in cap_state_after_second_sweep["continuity"]
            if key not in cap_state_after_second_sweep["scopes"]
        )
        evicted_continuity = cap_state_after_second_sweep["continuity"][
            evicted_scope_key
        ]

        def allow_second_cycle(config: dict[str, Any]) -> None:
            config["cognitive_loop"]["daily_model_cycle_budget"] = 2

        cap_store.mutate_json("ops_config.json", allow_second_cycle)

        def age_evicted_scope(state: dict[str, Any]) -> None:
            state["continuity"][evicted_scope_key]["last_model_at"] = (
                "2020-01-01T00:00:00+00:00"
            )

        cap_store.mutate_json("cognitive_loop_state.json", age_evicted_scope)
        cap_store.patch_json(
            "executor_state.json",
            {"selected_agent": "continuity-rehydration-change"},
        )
        rehydrated = cap_runtime._run_owner(  # noqa: SLF001
            user_id=evicted_continuity["user_id"],
            session_id=evicted_continuity["session_id"],
            reason="smoke",
            config=cap_runtime._config(),  # noqa: SLF001
            expected_generation=0,
        )
        assert rehydrated["status"] == "observed", rehydrated
        assert rehydrated["reason"] == "record_candidate"
        rehydrated_state = cap_store.read_json("cognitive_loop_state.json")
        assert rehydrated_state["scopes"][evicted_scope_key]["cycles"][-1][
            "baseline"
        ] is False
        assert len(rehydrated_state["continuity"][evicted_scope_key]["attempts"]) == 2

        # Missing or invalid authorization config fails closed.
        disabled_store = WorldStateStore(Path(tmp) / "disabled")
        disabled_store.patch_json("ops_config.json", {"cognitive_loop": []})
        disabled_client = FakeClient()
        disabled_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=disabled_store,
            reasoning=FakeReasoning(disabled_client),
        )
        disabled = disabled_runtime.run_once(reason="smoke")
        assert disabled["status"] == "disabled", disabled
        assert disabled_client.calls == []

        malformed_store = MalformedConfigStore(Path(tmp) / "malformed-config")
        malformed_client = FakeClient()
        malformed_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=malformed_store,
            reasoning=FakeReasoning(malformed_client),
        )
        malformed_status = malformed_runtime.status()
        assert malformed_status["status"] == "degraded"
        assert malformed_status["mode"] == "disabled"
        assert malformed_status["config_status"] == "invalid"
        assert malformed_runtime.schedule_once(reason="smoke")["status"] == (
            "disabled"
        )
        assert malformed_client.calls == []

        # Disabling while a provider call is in flight discards the result and
        # writes no brief/candidate after the kill switch revision changes.
        class PausingClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def complete_json(
                self,
                *,
                purpose: str,
                system: str,
                user: str,
            ) -> dict[str, Any]:
                if purpose == "background_cognitive_observation_plan":
                    self.entered.set()
                    assert self.release.wait(timeout=2.0)
                return super().complete_json(
                    purpose=purpose,
                    system=system,
                    user=user,
                )

        race_store = WorldStateStore(Path(tmp) / "config-race")
        race_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_config_race",
                        "user_id": "race-owner",
                        "session_id": "race-session",
                        "title": "Config race",
                        "status": "active",
                    }
                ]
            },
        )
        race_client = PausingClient()
        race_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=race_store,
            reasoning=FakeReasoning(race_client),
        )
        assert race_runtime.schedule_once(reason="smoke")["status"] == "scheduled"
        assert race_client.entered.wait(timeout=1.0)

        def disable_cognition(config: dict[str, Any]) -> None:
            section = config["cognitive_loop"]
            section["mode"] = "disabled"

        race_store.mutate_json("ops_config.json", disable_cognition)
        race_client.release.set()
        assert race_runtime._worker is not None  # noqa: SLF001
        race_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        race_state = race_store.read_json("cognitive_loop_state.json")
        assert race_state["metrics"]["cycle_count"] == 1
        assert race_state["metrics"]["reserved_count"] == 1
        assert race_state["metrics"]["candidate_count"] == 0
        race_scope = next(iter(race_state["scopes"].values()))
        assert race_scope["cycles"][-1]["status"] == "reserved"
        assert race_scope["cycles"][-1]["brief"] is None
        assert race_runtime.status()["last_worker_status"] == "suppressed"

        # A disable -> re-enable ABA still changes the durable ops revision;
        # an old worker cannot cross that transition and commit a final brief.
        config_aba_store = WorldStateStore(Path(tmp) / "config-aba")
        config_aba_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_config_aba",
                        "user_id": "config-aba-owner",
                        "session_id": "config-aba-session",
                        "title": "Config ABA",
                        "status": "active",
                    }
                ]
            },
        )
        config_aba_client = PausingClient()
        config_aba_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=config_aba_store,
            reasoning=FakeReasoning(config_aba_client),
        )
        assert config_aba_runtime.schedule_once(reason="smoke")["status"] == (
            "scheduled"
        )
        assert config_aba_client.entered.wait(timeout=1.0)
        config_aba_store.mutate_json("ops_config.json", disable_cognition)

        def reenable_cognition(config: dict[str, Any]) -> None:
            config["cognitive_loop"]["mode"] = "record_only"

        config_aba_store.mutate_json("ops_config.json", reenable_cognition)
        config_aba_client.release.set()
        assert config_aba_runtime._worker is not None  # noqa: SLF001
        config_aba_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        config_aba_state = config_aba_store.read_json(
            "cognitive_loop_state.json"
        )
        assert config_aba_state["metrics"]["reserved_count"] == 1
        assert config_aba_state["metrics"]["observed_count"] == 0
        assert len(config_aba_client.calls) == 1
        assert config_aba_runtime.status()["last_worker_status"] == "suppressed"

        # Runtime stop is a linearization fence. A provider already in flight
        # may finish transport, but cannot call the second model stage or
        # replace its durable pre-call reservation with a final brief.
        stop_store = WorldStateStore(Path(tmp) / "lifecycle-stop")
        stop_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_lifecycle_stop",
                        "user_id": "stop-owner",
                        "session_id": "stop-session",
                        "title": "Lifecycle stop",
                        "status": "active",
                    }
                ]
            },
        )
        stop_client = PausingClient()
        stop_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=stop_store,
            reasoning=FakeReasoning(stop_client),
        )
        assert stop_runtime.schedule_once(reason="smoke")["status"] == "scheduled"
        assert stop_client.entered.wait(timeout=1.0)
        assert stop_runtime.stop()["status"] == "stopped"
        assert stop_runtime.schedule_once(reason="smoke")["status"] == "stopped"
        stop_client.release.set()
        assert stop_runtime._worker is not None  # noqa: SLF001
        stop_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        stop_state = stop_store.read_json("cognitive_loop_state.json")
        assert stop_state["metrics"]["reserved_count"] == 1
        assert stop_state["metrics"]["observed_count"] == 0
        assert len(stop_client.calls) == 1
        assert stop_runtime.status()["last_worker_status"] == "suppressed"
        assert stop_runtime.status()["lifecycle_status"] == "stopped"

        # stop -> resume is also fenced by a monotonic generation. Resuming a
        # new Active Loop cannot make an old provider result current again.
        generation_store = WorldStateStore(Path(tmp) / "generation-aba")
        generation_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_generation_aba",
                        "user_id": "generation-owner",
                        "session_id": "generation-session",
                        "title": "Generation ABA",
                        "status": "active",
                    }
                ]
            },
        )
        generation_client = PausingClient()
        generation_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=generation_store,
            reasoning=FakeReasoning(generation_client),
        )
        assert generation_runtime.schedule_once(reason="smoke")["status"] == (
            "scheduled"
        )
        assert generation_client.entered.wait(timeout=1.0)
        generation_runtime.stop()
        generation_runtime.resume()
        generation_client.release.set()
        assert generation_runtime._worker is not None  # noqa: SLF001
        generation_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        generation_state = generation_store.read_json(
            "cognitive_loop_state.json"
        )
        assert generation_state["metrics"]["reserved_count"] == 1
        assert generation_state["metrics"]["observed_count"] == 0
        assert generation_runtime.status()["last_worker_status"] == "suppressed"

        # The final persistence gate is tested after both model transports and
        # after the post-call config check, while strict brief validation is
        # deliberately paused.
        validation_store = WorldStateStore(Path(tmp) / "validation-race")
        validation_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_validation_race",
                        "user_id": "validation-owner",
                        "session_id": "validation-session",
                        "title": "Validation race",
                        "status": "active",
                    }
                ]
            },
        )
        validation_client = FakeClient()
        validation_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=validation_store,
            reasoning=FakeReasoning(validation_client),
        )
        validation_entered = threading.Event()
        validation_release = threading.Event()
        original_validate = CognitiveBrief.model_validate

        def paused_validate(value: Any, *args: Any, **kwargs: Any) -> Any:
            validation_entered.set()
            assert validation_release.wait(timeout=2.0)
            return original_validate(value, *args, **kwargs)

        with patch.object(
            CognitiveBrief,
            "model_validate",
            side_effect=paused_validate,
        ):
            assert validation_runtime.schedule_once(reason="smoke")["status"] == (
                "scheduled"
            )
            assert validation_entered.wait(timeout=1.0)
            validation_store.mutate_json("ops_config.json", disable_cognition)
            validation_release.set()
            assert validation_runtime._worker is not None  # noqa: SLF001
            validation_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        validation_state = validation_store.read_json(
            "cognitive_loop_state.json"
        )
        assert validation_state["metrics"]["reserved_count"] == 1
        assert validation_state["metrics"]["observed_count"] == 0
        assert len(validation_client.calls) == 2
        assert validation_runtime.status()["last_worker_status"] == "suppressed"

        # Active Loop scheduling is non-blocking even though the two model
        # transports run in a bounded background worker.
        class SlowClient(FakeClient):
            def complete_json(
                self,
                *,
                purpose: str,
                system: str,
                user: str,
            ) -> dict[str, Any]:
                time.sleep(0.2)
                return super().complete_json(
                    purpose=purpose,
                    system=system,
                    user=user,
                )

        scheduled_store = WorldStateStore(Path(tmp) / "scheduled")
        scheduled_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_scheduled",
                        "user_id": "scheduled-owner",
                        "session_id": "scheduled-session",
                        "title": "Scheduled cognition",
                        "status": "active",
                    }
                ]
            },
        )
        scheduled_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=scheduled_store,
            reasoning=FakeReasoning(SlowClient()),
        )
        started = time.perf_counter()
        scheduled = scheduled_runtime.schedule_once(reason="smoke")
        elapsed = time.perf_counter() - started
        assert scheduled["status"] == "scheduled", scheduled
        assert elapsed < 0.1, elapsed
        assert scheduled_runtime.status()["worker_alive"] is True
        assert scheduled_runtime._worker is not None  # noqa: SLF001
        scheduled_runtime._worker.join(timeout=2.0)  # noqa: SLF001
        assert scheduled_runtime.status()["worker_alive"] is False

        class FakeCognitionLifecycle:
            def __init__(self) -> None:
                self.resumes = 0
                self.stops = 0

            def resume(self) -> dict[str, Any]:
                self.resumes += 1
                return {"status": "available"}

            def stop(self) -> dict[str, Any]:
                self.stops += 1
                return {"status": "stopped"}

        class LifecycleOnlyActiveLoop(ActiveRuntimeLoop):
            def _run_forever(
                self,
                loop_id: str,
                interval_seconds: float,
                stop_event: threading.Event,
            ) -> None:
                del loop_id, interval_seconds
                stop_event.wait(timeout=2.0)

        lifecycle_store = WorldStateStore(Path(tmp) / "active-loop-lifecycle")
        fake_cognition = FakeCognitionLifecycle()
        lifecycle_loop = LifecycleOnlyActiveLoop(
            state_store=lifecycle_store,
            runtime_entity=None,
            proactive_checks=None,
            state_refresh=None,
            external_world_refresh=None,
            runtime_matrix=None,
            retention_policy=None,
            task_tracker=None,
            adapter_resolver=lambda: None,
            verifier=None,
            cognitive_loop=fake_cognition,
        )
        assert lifecycle_loop.start(interval_seconds=5.0)["status"] == "running"
        assert lifecycle_loop.start(interval_seconds=5.0)["status"] == (
            "already_running"
        )
        assert fake_cognition.resumes == 1
        assert lifecycle_loop.stop()["status"] == "stopped"
        assert fake_cognition.stops == 1

        empty_store = WorldStateStore(Path(tmp) / "empty")
        empty_client = FakeClient()
        empty_runtime = ReadOnlyCognitiveLoopRuntime(
            state_store=empty_store,
            reasoning=FakeReasoning(empty_client),
        )
        idle = empty_runtime.run_once(reason="smoke")
        assert idle["status"] == "idle"
        assert idle["reason"] == "no_exact_owner_cognitive_scope"
        assert empty_client.calls == []

        print(
            json.dumps(
                {
                    "status": "pass",
                    "cycles": recovered_state["metrics"]["cycle_count"],
                    "candidates": recovered_state["metrics"]["candidate_count"],
                    "model_calls": recovered_state["metrics"]["model_call_count"],
                    "owner_session_isolation": "exact",
                    "failed_cycle_retried": True,
                    "unsupported_empty_selection_rejected": True,
                    "goal_backed_scope": True,
                    "active_loop_nonblocking": True,
                    "cross_owner_previous_brief": False,
                    "trace_failure_budget_bypass": False,
                    "config_race_persisted_result": False,
                    "due_owner_starvation": False,
                    "owners_beyond_scope_cap_seen": len(cap_seen),
                    "observation_boundary": status["observation_boundary"],
                    "external_delivery": False,
                    "tool_execution": False,
                    "agent_execution": False,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
