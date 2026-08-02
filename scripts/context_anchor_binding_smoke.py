#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.attention_core import AttentionCore
from core.context_anchor_binder import ContextAnchorBinder
from core.semantic_frame import TurnSemanticFrame
from core.turn_context_builder import TurnContextBuilder
from core.world_state import WorldStateStore
from interface.event_schema import EventSource, EventType, VeyraEvent
from runtime.event_awareness_runtime import ShadowAwarenessRuntime


def semantic_understanding(
    text: str,
    *,
    target_value: str,
    target_type: str = "project_phase",
    candidate_token: str = "",
    authority: str = "direct_user",
    speaker: str = "user",
    mention_mode: str = "normal_use",
    modality: str = "asserted",
) -> SimpleNamespace:
    attributes = (
        {"anchor_candidate_token": candidate_token}
        if candidate_token
        else {}
    )
    frame = TurnSemanticFrame.from_model_payload(
        {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": [
                {
                    "act_id": "a1",
                    "kind": "request",
                    "goal": "continue the current cognitive architecture work",
                    "operation": "continue_design",
                    "target": {
                        "type": target_type,
                        "value": target_value,
                        "attributes": attributes,
                    },
                    "polarity": "positive",
                    "explicitness": "explicit",
                    "source_quote": {
                        "text": text,
                        "start": 0,
                        "end": len(text),
                    },
                    "speaker": speaker,
                    "authority": authority,
                    "mention_mode": mention_mode,
                    "evidence_need": "context",
                    "referent": {
                        "surface": "",
                        "resolved": "",
                        "status": "not_applicable",
                        "candidates": [],
                    },
                    "condition": None,
                    "modality": modality,
                    "arguments": {},
                }
            ],
            "relations": [],
            "ambiguities": [],
            "resolver_status": "resolved",
            "source": "model",
        },
        source_text=text,
    )
    return SimpleNamespace(source="model", semantic_frame=frame)


def event(
    event_id: str,
    text: str,
    *,
    user_id: str,
    session_id: str,
    dedupe_key: str | None = None,
) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="api",
            user_id=user_id,
            session_id=session_id,
        ),
        payload={"text": text},
        event_id=event_id,
        dedupe_key=dedupe_key,
    )


def bind_turn(
    *,
    store: WorldStateStore,
    runtime: ShadowAwarenessRuntime,
    binder: ContextAnchorBinder,
    attention: AttentionCore,
    turn: VeyraEvent,
    understanding: SimpleNamespace,
    candidate_index: dict | None = None,
) -> dict:
    index = candidate_index or binder.candidate_index(turn)
    attention.focus_for_text(
        str(turn.payload.get("text") or ""),
        user_id=turn.source.user_id,
        session_id=turn.source.session_id,
        event=turn,
    )
    assessment = attention.assess_understanding(
        text=str(turn.payload.get("text") or ""),
        understanding=understanding,
    )
    envelope = binder.bind(
        event=turn,
        understanding=understanding,
        attention_assessment=assessment,
        candidate_index=index,
    )
    assert envelope["status"] == "bound", envelope
    assert envelope["is_fact"] is False
    assert envelope["causality_asserted"] is False
    assert all(value is False for value in envelope["authority"].values())
    assert runtime.begin(turn)["status"] == "recorded"
    registered = runtime.record_context_binding(turn, envelope)
    assert registered["status"] == "pending", registered
    processed = runtime.process_pending(limit=10)
    assert processed["status"] == "success", processed
    assert processed["processed_count"] == 1, processed
    return envelope


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-context-anchor-") as tmp:
        store = WorldStateStore(tmp)
        runtime = ShadowAwarenessRuntime(store, mode="record_only")
        binder = ContextAnchorBinder(store)
        attention = AttentionCore(store)
        user_id = "context-user"
        session_id = "context-session"
        text_a = "继续完成 Veyra 的认知锚点"
        text_b = "检查 Veyra 认知锚点的聚合结果"

        first = event("evt_context_1", text_a, user_id=user_id, session_id=session_id)
        first_index = binder.candidate_index(first)
        first_understanding = semantic_understanding(
            text_a,
            target_value="Veyra cognitive context anchors",
        )
        attention.focus_for_text(
            text_a,
            user_id=user_id,
            session_id=session_id,
            event=first,
        )
        first_assessment = attention.assess_understanding(
            text=text_a,
            understanding=first_understanding,
        )
        first_retry_a = binder.bind(
            event=first,
            understanding=first_understanding,
            attention_assessment=first_assessment,
            candidate_index=first_index,
        )
        first_retry_b = binder.bind(
            event=first,
            understanding=first_understanding,
            attention_assessment=first_assessment,
            candidate_index=first_index,
        )
        assert first_retry_a["binding_digest"] == first_retry_b["binding_digest"]
        assert first_retry_a["operation_id"] == first_retry_b["operation_id"]
        first_envelope = bind_turn(
            store=store,
            runtime=runtime,
            binder=binder,
            attention=attention,
            turn=first,
            understanding=first_understanding,
            candidate_index=first_index,
        )
        assert first_envelope["anchors"][0]["kind"] == "context"
        first_context_id = first_envelope["anchors"][0]["ref_id"]
        situations = runtime.situation_evaluator.list(
            user_id=user_id,
            session_id=session_id,
            limit=10,
        )
        assert len(situations) == 1
        assert situations[0]["structured_anchor_refs"] == [
            {"kind": "context", "ref_id": first_context_id}
        ]
        assert situations[0]["observation_revision"] == 2
        assert situations[0]["context_bindings"][0]["is_fact"] is False
        general = store.read_json("general_situation_state.json")
        assert len(general.get("candidates") or {}) == 1
        assert len(general.get("general_situations") or {}) == 0

        # A second independently understood turn receives the same server-owned
        # context id and creates a non-causal GeneralSituation parent.
        second = event("evt_context_2", text_b, user_id=user_id, session_id=session_id)
        second_envelope = bind_turn(
            store=store,
            runtime=runtime,
            binder=binder,
            attention=attention,
            turn=second,
            understanding=semantic_understanding(
                text_b,
                target_value="Veyra cognitive context anchors",
            ),
        )
        assert second_envelope["anchors"][0]["ref_id"] == first_context_id
        general = store.read_json("general_situation_state.json")
        parents = general.get("general_situations") or {}
        assert len(parents) == 1, general
        parent = next(iter(parents.values()))
        assert parent["distinct_event_count"] == 2
        assert parent["causality_asserted"] is False
        assert parent["model_similarity_used_for_merge"] is False
        context_state = store.read_json("context_binding_state.json")
        assert context_state["threads"][first_context_id]["status"] == "corroborated"
        assert context_state["coverage"] == {
            "eligible": 2,
            "bound": 2,
            "unresolved": 0,
            "bound_rate": 1.0,
            "overconservative_alert": False,
        }

        # Exact replay is byte-stable across Situation, binding, and parent
        # state; a model retry cannot create a second observation revision.
        paths = [
            Path(tmp) / "runtime" / "situation_state.json",
            Path(tmp) / "runtime" / "context_binding_state.json",
            Path(tmp) / "runtime" / "general_situation_state.json",
        ]
        before = [digest(path) for path in paths]
        replay = runtime.record_context_binding(second, second_envelope)
        assert replay["status"] == "applied", replay
        after = [digest(path) for path in paths]
        assert before == after

        # Application replay keeps the first applied Situation revision even
        # if an unrelated later Situation observation exists.
        applied_record = runtime.context_bindings.get(
            second.event_id,
            user_id=user_id,
            session_id=session_id,
        )
        assert isinstance(applied_record, dict)
        context_bytes = paths[1].read_bytes()
        application_replay = runtime.context_bindings.mark_applied(
            event_id=second.event_id,
            binding_digest=second_envelope["binding_digest"],
            situation_id=str(applied_record["situation_id"]),
            observation_revision=99,
        )
        assert application_replay["status"] == "replayed"
        assert application_replay["observation_revision"] == 2
        assert paths[1].read_bytes() == context_bytes

        # Operation identity and status/anchor consistency are fail-closed
        # before any state mutation.
        tamper_store = WorldStateStore(Path(tmp) / "tamper")
        tamper_runtime = ShadowAwarenessRuntime(tamper_store, mode="record_only")
        tamper_event = event(
            "evt_context_tamper",
            "继续安全边界",
            user_id="tamper-user",
            session_id="tamper-session",
        )
        tamper_binder = ContextAnchorBinder(tamper_store)
        tamper_attention = AttentionCore(tamper_store)
        tamper_understanding = semantic_understanding(
            "继续安全边界",
            target_value="context binding safety boundary",
        )
        tamper_attention.focus_for_text(
            "继续安全边界",
            user_id="tamper-user",
            session_id="tamper-session",
            event=tamper_event,
        )
        tamper_envelope = tamper_binder.bind(
            event=tamper_event,
            understanding=tamper_understanding,
            attention_assessment=tamper_attention.assess_understanding(
                text="继续安全边界",
                understanding=tamper_understanding,
            ),
            candidate_index=tamper_binder.candidate_index(tamper_event),
        )
        tamper_path = Path(tmp) / "tamper" / "runtime" / "context_binding_state.json"
        tamper_before = tamper_path.read_bytes()
        invalid_operation = copy.deepcopy(tamper_envelope)
        invalid_operation["operation_id"] = "context-bind:forged"
        try:
            tamper_runtime.context_bindings.register(invalid_operation)
            raise AssertionError("forged operation_id was accepted")
        except ValueError:
            pass
        assert tamper_path.read_bytes() == tamper_before
        invalid_status = copy.deepcopy(tamper_envelope)
        invalid_status["status"] = "unresolved"
        try:
            tamper_runtime.context_bindings.register(invalid_status)
            raise AssertionError("status/anchor mismatch was accepted")
        except ValueError:
            pass
        assert tamper_path.read_bytes() == tamper_before

        # Existing durable identifiers are selected only via an event-bound
        # opaque candidate token generated from the exact owner state.
        def add_goal(state: dict) -> None:
            state["goals"] = [
                {
                    "goal_id": "goal_context_live",
                    "user_id": user_id,
                    "session_id": "another-session",
                    "status": "active",
                    "title": "Veyra cognitive architecture",
                }
            ]

        store.mutate_json("user_goals.json", add_goal)
        goal_text = "继续 Veyra cognitive architecture"
        goal_turn = event(
            "evt_context_goal",
            goal_text,
            user_id=user_id,
            session_id=session_id,
        )
        goal_index = binder.candidate_index(goal_turn)
        goal_token = next(
            item["candidate_token"]
            for item in goal_index["model_catalog"]
            if item["kind"] == "goal"
        )
        # The actual Understanding prompt receives the event-bound token
        # unchanged, but never the private durable ref_id behind it.
        turn_context = TurnContextBuilder(store).build(
            user_message=goal_text,
            attention_focus=[],
            event=goal_turn,
            anchor_candidates=goal_index["model_catalog"],
        )
        prompt_catalog = turn_context["anchor_candidates"]
        assert any(
            item.get("candidate_token") == goal_token
            for item in prompt_catalog
        )
        serialized_context = json.dumps(turn_context, ensure_ascii=False)
        assert "<redacted>" not in json.dumps(prompt_catalog, ensure_ascii=False)
        assert "goal_context_live" not in serialized_context
        goal_understanding = semantic_understanding(
            goal_text,
            target_value="Veyra cognitive architecture",
            target_type="goal",
            candidate_token=goal_token,
        )
        attention.focus_for_text(
            goal_text,
            user_id=user_id,
            session_id=session_id,
            event=goal_turn,
        )
        goal_binding = binder.bind(
            event=goal_turn,
            understanding=goal_understanding,
            attention_assessment=attention.assess_understanding(
                text=goal_text,
                understanding=goal_understanding,
            ),
            candidate_index=goal_index,
        )
        assert goal_binding["anchors"] == [
            {"kind": "goal", "ref_id": "goal_context_live"}
        ]
        assert goal_binding["bindings"][0]["candidate_source"]["candidate_token"] == goal_token

        # A token plus copied label is still insufficient when the user's
        # source quote does not actually name that durable object.
        unrelated_text = "继续这个工作"
        unrelated_turn = event(
            "evt_context_goal_unrelated",
            unrelated_text,
            user_id=user_id,
            session_id=session_id,
        )
        unrelated_index = binder.candidate_index(unrelated_turn)
        unrelated_token = next(
            item["candidate_token"]
            for item in unrelated_index["model_catalog"]
            if item["kind"] == "goal"
        )
        unrelated_understanding = semantic_understanding(
            unrelated_text,
            target_value="Veyra cognitive architecture",
            target_type="goal",
            candidate_token=unrelated_token,
        )
        attention.focus_for_text(
            unrelated_text,
            user_id=user_id,
            session_id=session_id,
            event=unrelated_turn,
        )
        unrelated_binding = binder.bind(
            event=unrelated_turn,
            understanding=unrelated_understanding,
            attention_assessment=attention.assess_understanding(
                text=unrelated_text,
                understanding=unrelated_understanding,
            ),
            candidate_index=unrelated_index,
        )
        assert all(
            item["kind"] != "goal" for item in unrelated_binding["anchors"]
        )

        wrong_kind_understanding = semantic_understanding(
            goal_text,
            target_value="Veyra cognitive architecture",
            target_type="commitment",
            candidate_token=goal_token,
        )
        wrong_kind_binding = binder.bind(
            event=goal_turn,
            understanding=wrong_kind_understanding,
            attention_assessment=attention.assess_understanding(
                text=goal_text,
                understanding=wrong_kind_understanding,
            ),
            candidate_index=goal_index,
        )
        assert all(
            item["kind"] != "goal" for item in wrong_kind_binding["anchors"]
        )

        # A valid event-bound token is still not semantic proof. If target.value
        # does not copy the candidate label exactly, the binder must not attach
        # the durable Goal identifier.
        mismatch_turn = event(
            "evt_context_goal_mismatch",
            "继续另一个目标",
            user_id=user_id,
            session_id=session_id,
        )
        mismatch_index = binder.candidate_index(mismatch_turn)
        mismatch_token = next(
            item["candidate_token"]
            for item in mismatch_index["model_catalog"]
            if item["kind"] == "goal"
        )
        mismatch_understanding = semantic_understanding(
            "继续另一个目标",
            target_value="a different durable goal",
            target_type="goal",
            candidate_token=mismatch_token,
        )
        attention.focus_for_text(
            "继续另一个目标",
            user_id=user_id,
            session_id=session_id,
            event=mismatch_turn,
        )
        mismatch_binding = binder.bind(
            event=mismatch_turn,
            understanding=mismatch_understanding,
            attention_assessment=attention.assess_understanding(
                text="继续另一个目标",
                understanding=mismatch_understanding,
            ),
            candidate_index=mismatch_index,
        )
        assert all(item["kind"] != "goal" for item in mismatch_binding["anchors"])

        # Duplicate visible labels are ambiguous even if the model picked one
        # candidate token. The server refuses to guess which durable object was
        # intended and keeps only a provisional context association.
        def add_duplicate_goal(state: dict) -> None:
            state.setdefault("goals", []).append(
                {
                    "goal_id": "goal_context_duplicate",
                    "user_id": user_id,
                    "session_id": "duplicate-session",
                    "status": "active",
                    "title": "Veyra cognitive architecture",
                }
            )

        store.mutate_json("user_goals.json", add_duplicate_goal)
        ambiguous_turn = event(
            "evt_context_goal_ambiguous",
            "继续 Veyra cognitive architecture",
            user_id=user_id,
            session_id=session_id,
        )
        ambiguous_index = binder.candidate_index(ambiguous_turn)
        ambiguous_token = next(
            item["candidate_token"]
            for item in ambiguous_index["model_catalog"]
            if item["kind"] == "goal"
        )
        ambiguous_understanding = semantic_understanding(
            "继续 Veyra cognitive architecture",
            target_value="Veyra cognitive architecture",
            target_type="goal",
            candidate_token=ambiguous_token,
        )
        attention.focus_for_text(
            "继续 Veyra cognitive architecture",
            user_id=user_id,
            session_id=session_id,
            event=ambiguous_turn,
        )
        ambiguous_binding = binder.bind(
            event=ambiguous_turn,
            understanding=ambiguous_understanding,
            attention_assessment=attention.assess_understanding(
                text="继续 Veyra cognitive architecture",
                understanding=ambiguous_understanding,
            ),
            candidate_index=ambiguous_index,
        )
        assert all(item["kind"] != "goal" for item in ambiguous_binding["anchors"])

        # The token is useless in another owner index; it cannot bind the Goal
        # and falls back to a distinct exact-session context hypothesis.
        peer_turn = event(
            "evt_context_peer",
            "继续这个目标",
            user_id="peer-user",
            session_id="peer-session",
        )
        peer_understanding = semantic_understanding(
            "继续这个目标",
            target_value="Veyra cognitive architecture",
            candidate_token=goal_token,
        )
        attention.focus_for_text(
            "继续这个目标",
            user_id="peer-user",
            session_id="peer-session",
            event=peer_turn,
        )
        peer_binding = binder.bind(
            event=peer_turn,
            understanding=peer_understanding,
            attention_assessment=attention.assess_understanding(
                text="继续这个目标",
                understanding=peer_understanding,
            ),
            candidate_index=binder.candidate_index(peer_turn),
        )
        assert all(item["kind"] != "goal" for item in peer_binding["anchors"])
        assert peer_binding["anchors"][0]["ref_id"] != first_context_id

        # Reported/hypothetical content remains unresolved and cannot create a
        # context thread merely because it names the same target.
        quoted_turn = event(
            "evt_context_reported",
            "同事说继续 Veyra",
            user_id=user_id,
            session_id=session_id,
        )
        quoted_understanding = semantic_understanding(
            "同事说继续 Veyra",
            target_value="Veyra cognitive context anchors",
            authority="reported_speech",
            speaker="colleague",
            mention_mode="reported_speech",
            modality="reported",
        )
        attention.focus_for_text(
            "同事说继续 Veyra",
            user_id=user_id,
            session_id=session_id,
            event=quoted_turn,
        )
        quoted_binding = binder.bind(
            event=quoted_turn,
            understanding=quoted_understanding,
            attention_assessment=attention.assess_understanding(
                text="同事说继续 Veyra",
                understanding=quoted_understanding,
            ),
            candidate_index=binder.candidate_index(quoted_turn),
        )
        assert quoted_binding["status"] == "unresolved"
        assert quoted_binding["anchors"] == []

        # Full shadow replay is byte-stable across the Situation projection,
        # sidecar, non-causal parent, and append-only Situation trace.
        shadow_root = Path(tmp) / "shadow-replay"
        shadow_store = WorldStateStore(shadow_root)
        shadow_runtime = ShadowAwarenessRuntime(shadow_store, mode="shadow")
        shadow_runtime.configure("shadow")
        shadow_binder = ContextAnchorBinder(shadow_store)
        shadow_attention = AttentionCore(shadow_store)
        shadow_event = event(
            "evt_shadow_replay",
            "继续 shadow replay context",
            user_id="shadow-user",
            session_id="shadow-session",
        )
        shadow_understanding = semantic_understanding(
            "继续 shadow replay context",
            target_value="shadow replay context",
        )
        shadow_attention.focus_for_text(
            "继续 shadow replay context",
            user_id="shadow-user",
            session_id="shadow-session",
            event=shadow_event,
        )
        shadow_envelope = shadow_binder.bind(
            event=shadow_event,
            understanding=shadow_understanding,
            attention_assessment=shadow_attention.assess_understanding(
                text="继续 shadow replay context",
                understanding=shadow_understanding,
            ),
            candidate_index=shadow_binder.candidate_index(shadow_event),
        )
        shadow_begin = shadow_runtime.begin(shadow_event)
        assert shadow_begin["status"] == "observed", shadow_begin
        shadow_bound = shadow_runtime.record_context_binding(
            shadow_event,
            shadow_envelope,
        )
        assert shadow_bound["status"] == "applied", shadow_bound
        shadow_paths = [
            shadow_root / "runtime" / "situation_state.json",
            shadow_root / "runtime" / "context_binding_state.json",
            shadow_root / "runtime" / "general_situation_state.json",
            shadow_root / "logs" / "situation_trace.jsonl",
        ]
        shadow_before = [path.read_bytes() for path in shadow_paths]
        shadow_replay_begin = shadow_runtime.begin(shadow_event)
        assert shadow_replay_begin["event_id"] == shadow_event.event_id
        assert shadow_replay_begin["finalize_allowed"] is True
        shadow_replay_binding = shadow_runtime.record_context_binding(
            shadow_event,
            shadow_envelope,
        )
        assert shadow_replay_binding["status"] == "applied"
        assert [path.read_bytes() for path in shadow_paths] == shadow_before

        # A second event suppressed by a shared dedupe identity has no exact
        # EventInbox record and therefore cannot leave a phantom sidecar or
        # alter Event A's Situation.
        dedupe_root = Path(tmp) / "dedupe"
        dedupe_store = WorldStateStore(dedupe_root)
        dedupe_runtime = ShadowAwarenessRuntime(dedupe_store, mode="shadow")
        dedupe_runtime.configure("shadow")
        dedupe_binder = ContextAnchorBinder(dedupe_store)
        dedupe_attention = AttentionCore(dedupe_store)
        dedupe_a = event(
            "evt_dedupe_a",
            "观察 canonical A",
            user_id="dedupe-user",
            session_id="dedupe-session",
            dedupe_key="shared-provider-delivery",
        )
        dedupe_b = event(
            "evt_dedupe_b",
            "观察 suppressed B",
            user_id="dedupe-user",
            session_id="dedupe-session",
            dedupe_key="shared-provider-delivery",
        )
        dedupe_a_begin = dedupe_runtime.begin(dedupe_a)
        assert dedupe_a_begin["status"] == "observed", dedupe_a_begin
        dedupe_paths = [
            dedupe_root / "runtime" / "situation_state.json",
            dedupe_root / "runtime" / "context_binding_state.json",
            dedupe_root / "runtime" / "general_situation_state.json",
            dedupe_root / "logs" / "situation_trace.jsonl",
        ]
        dedupe_before = [path.read_bytes() for path in dedupe_paths]
        dedupe_b_begin = dedupe_runtime.begin(dedupe_b)
        assert dedupe_b_begin["event_id"] == dedupe_a.event_id
        assert dedupe_b_begin["finalize_allowed"] is False
        assert dedupe_runtime.event_inbox.get_record(dedupe_b.event_id) is None
        assert dedupe_runtime.event_inbox.get_record(dedupe_a.event_id)[
            "delivery_count"
        ] == 2
        dedupe_understanding = semantic_understanding(
            "观察 suppressed B",
            target_value="suppressed B context",
        )
        dedupe_attention.focus_for_text(
            "观察 suppressed B",
            user_id="dedupe-user",
            session_id="dedupe-session",
            event=dedupe_b,
        )
        dedupe_b_envelope = dedupe_binder.bind(
            event=dedupe_b,
            understanding=dedupe_understanding,
            attention_assessment=dedupe_attention.assess_understanding(
                text="观察 suppressed B",
                understanding=dedupe_understanding,
            ),
            candidate_index=dedupe_binder.candidate_index(dedupe_b),
        )
        dedupe_suppressed = dedupe_runtime.record_context_binding(
            dedupe_b,
            dedupe_b_envelope,
        )
        assert dedupe_suppressed["status"] == "suppressed"
        assert dedupe_suppressed["reason"] == (
            "context_binding_event_not_exactly_admitted"
        )
        assert dedupe_runtime.context_bindings.get(
            dedupe_b.event_id,
            user_id="dedupe-user",
            session_id="dedupe-session",
        ) is None
        assert [path.read_bytes() for path in dedupe_paths] == dedupe_before

        # A pending record-only sidecar remains untouched after the event fabric
        # is disabled; maintenance cannot bypass the kill switch.
        disabled_root = Path(tmp) / "disabled-reconcile"
        disabled_store = WorldStateStore(disabled_root)
        disabled_runtime = ShadowAwarenessRuntime(
            disabled_store,
            mode="record_only",
        )
        disabled_binder = ContextAnchorBinder(disabled_store)
        disabled_attention = AttentionCore(disabled_store)
        disabled_event = event(
            "evt_disabled_pending",
            "继续 disabled pending context",
            user_id="disabled-user",
            session_id="disabled-session",
        )
        disabled_understanding = semantic_understanding(
            "继续 disabled pending context",
            target_value="disabled pending context",
        )
        disabled_attention.focus_for_text(
            "继续 disabled pending context",
            user_id="disabled-user",
            session_id="disabled-session",
            event=disabled_event,
        )
        disabled_envelope = disabled_binder.bind(
            event=disabled_event,
            understanding=disabled_understanding,
            attention_assessment=disabled_attention.assess_understanding(
                text="继续 disabled pending context",
                understanding=disabled_understanding,
            ),
            candidate_index=disabled_binder.candidate_index(disabled_event),
        )
        assert disabled_runtime.begin(disabled_event)["status"] == "recorded"
        assert disabled_runtime.record_context_binding(
            disabled_event,
            disabled_envelope,
        )["status"] == "pending"
        disabled_runtime.configure("disabled")
        disabled_paths = [
            disabled_root / "runtime" / "situation_state.json",
            disabled_root / "runtime" / "context_binding_state.json",
            disabled_root / "runtime" / "general_situation_state.json",
            disabled_root / "logs" / "situation_trace.jsonl",
        ]
        disabled_before = [path.read_bytes() for path in disabled_paths]
        disabled_reconcile = disabled_runtime.reconcile_general_situations()
        assert disabled_reconcile["status"] == "disabled"
        assert [path.read_bytes() for path in disabled_paths] == disabled_before

        # More than the per-turn anchor limit is deterministically truncated,
        # not rejected as one oversized all-or-nothing envelope.
        bounded_root = Path(tmp) / "bounded"
        bounded_store = WorldStateStore(bounded_root)
        bounded_binder = ContextAnchorBinder(bounded_store)
        bounded_attention = AttentionCore(bounded_store)
        labels = [f"bounded durable goal {index}" for index in range(9)]
        bounded_store.patch_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": f"goal_bounded_{index}",
                        "user_id": "bounded-user",
                        "session_id": "bounded-session",
                        "title": label,
                        "status": "active",
                    }
                    for index, label in enumerate(labels)
                ]
            },
        )
        bounded_text = "; ".join(labels)
        bounded_event = event(
            "evt_context_bounded",
            bounded_text,
            user_id="bounded-user",
            session_id="bounded-session",
        )
        bounded_index = bounded_binder.candidate_index(bounded_event)
        token_by_label = {
            item["label"]: item["candidate_token"]
            for item in bounded_index["model_catalog"]
            if item["kind"] == "goal"
        }
        assert len(token_by_label) == 9
        acts = []
        for index, label in enumerate(labels):
            start = bounded_text.index(label)
            acts.append(
                {
                    "act_id": f"a{index}",
                    "kind": "request",
                    "goal": "continue bounded context",
                    "operation": "continue_design",
                    "target": {
                        "type": "goal",
                        "value": label,
                        "attributes": {
                            "anchor_candidate_token": token_by_label[label]
                        },
                    },
                    "polarity": "positive",
                    "explicitness": "explicit",
                    "source_quote": {
                        "text": label,
                        "start": start,
                        "end": start + len(label),
                    },
                    "speaker": "user",
                    "authority": "direct_user",
                    "mention_mode": "normal_use",
                    "evidence_need": "context",
                    "referent": {
                        "surface": "",
                        "resolved": "",
                        "status": "not_applicable",
                        "candidates": [],
                    },
                    "condition": None,
                    "modality": "asserted",
                    "arguments": {},
                }
            )
        bounded_frame = TurnSemanticFrame.from_model_payload(
            {
                "schema_version": "veyra.semantic_frame.v1",
                "acts": acts,
                "relations": [],
                "ambiguities": [],
                "resolver_status": "resolved",
                "source": "model",
            },
            source_text=bounded_text,
        )
        bounded_understanding = SimpleNamespace(
            source="model",
            semantic_frame=bounded_frame,
        )
        bounded_attention.focus_for_text(
            bounded_text,
            user_id="bounded-user",
            session_id="bounded-session",
            event=bounded_event,
        )
        bounded_envelope = bounded_binder.bind(
            event=bounded_event,
            understanding=bounded_understanding,
            attention_assessment=bounded_attention.assess_understanding(
                text=bounded_text,
                understanding=bounded_understanding,
            ),
            candidate_index=bounded_index,
        )
        assert len(bounded_envelope["anchors"]) == 8
        bounded_registration = ShadowAwarenessRuntime(
            bounded_store,
            mode="record_only",
        ).context_bindings.register(bounded_envelope)
        assert bounded_registration["status"] == "registered"

        print(
            json.dumps(
                {
                    "status": "pass",
                    "context_id": first_context_id,
                    "situation_count": len(
                        runtime.situation_evaluator.list(
                            user_id=user_id,
                            session_id=session_id,
                            limit=20,
                        )
                    ),
                    "general_situation_count": len(parents),
                    "coverage": context_state["coverage"],
                    "cross_owner_goal_binding": False,
                    "candidate_label_mismatch_bound": False,
                    "duplicate_candidate_label_bound": False,
                    "retry_digest_stable": True,
                    "full_shadow_replay_byte_stable": True,
                    "dedupe_phantom_sidecar": False,
                    "disabled_reconcile_write": False,
                    "bounded_anchor_count": len(bounded_envelope["anchors"]),
                    "route_change_allowed": False,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
