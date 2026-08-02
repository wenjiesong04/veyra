#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
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
from core.situation_evaluator import SituationEvaluator  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.general_situation_contract import StructuredAnchor  # noqa: E402
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.general_situation_runtime import GeneralSituationRuntime  # noqa: E402
from runtime.suggestion_outbox import (  # noqa: E402
    SuggestionOutbox,
    SuggestionOutboxConflict,
)


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


COMPONENTS = {
    "severity": 0.90,
    "urgency": 0.90,
    "novelty": 0.80,
    "uncertainty": 0.70,
    "evidence_completeness": 0.90,
}
CHILD_REF_KEYS = {
    "situation_id",
    "observation_revision",
    "source_event_id",
    "digest",
}


def set_goals(store: WorldStateStore, user_id: str, goal_ids: list[str]) -> None:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state["goals"] = [
            {
                "goal_id": goal_id,
                "user_id": user_id,
                "status": "active",
                "goal_priority": 0.95,
            }
            for goal_id in goal_ids
        ]
        return state

    store.mutate_json("user_goals.json", mutate)


def child_for(
    evaluator: SituationEvaluator,
    normalizer: EventNormalizer,
    clock: MutableClock,
    *,
    user_id: str,
    session_id: str,
    anchor_kind: str,
    anchor_id: str,
    text: str,
    components: dict[str, float] | None = None,
) -> tuple[Any, dict[str, Any]]:
    now = clock().isoformat()
    event = normalizer.user_message(
        text,
        "general-situation-smoke",
        user_id,
        session_id,
        subject={"kind": anchor_kind, "id": anchor_id},
        occurred_at=now,
        received_at=now,
    )
    event.payload[f"{anchor_kind}_id"] = anchor_id
    child = evaluator.observe(
        event,
        salience_components=(
            copy.deepcopy(COMPONENTS)
            if components is None
            else copy.deepcopy(components)
        ),
        evidence_refs=[f"evidence:{event.event_id}"],
    )
    clock.advance(seconds=1)
    return event, child


def group_goal(
    evaluator: SituationEvaluator,
    normalizer: EventNormalizer,
    runtime: GeneralSituationRuntime,
    clock: MutableClock,
    *,
    user_id: str,
    session_id: str,
    goal_id: str,
    text_a: str,
    text_b: str,
    components: dict[str, float] | None = None,
) -> tuple[dict[str, Any], list[tuple[Any, dict[str, Any]]]]:
    pair = [
        child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id=session_id,
            anchor_kind="goal",
            anchor_id=goal_id,
            text=text,
            components=components,
        )
        for text in (text_a, text_b)
    ]
    first = runtime.ingest_child(pair[0][1], event=pair[0][0])
    expect(
        first.get("status") == "awaiting_distinct_event",
        f"{goal_id} first event remains a candidate",
        first,
    )
    second = runtime.ingest_child(pair[1][1], event=pair[1][0])
    expect(
        second.get("status") == "created"
        and isinstance(second.get("general_situation"), dict),
        f"{goal_id} two distinct events create a general Situation",
        second,
    )
    return second["general_situation"], pair


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configure_mode(outbox: SuggestionOutbox, store: WorldStateStore, mode: str) -> None:
    revision = int(store.read_json("ops_config.json").get("_state_revision") or 0)
    outbox.configure_mode(mode, expected_state_revision=revision)


def configure_policy(
    outbox: SuggestionOutbox,
    store: WorldStateStore,
    *,
    user_id: str,
    session_id: str,
    budget: int,
    quiet_hours: dict[str, Any] | None = None,
) -> None:
    revision = int(
        store.read_json("suggestion_outbox.json").get("_state_revision") or 0
    )
    outbox.configure_policy(
        user_id=user_id,
        session_id=session_id,
        daily_budget=budget,
        quiet_hours=quiet_hours,
        cooldown_seconds=3600,
        dismiss_cooldown_seconds=86400,
        expected_state_revision=revision,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-general-situation-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        normalizer = EventNormalizer()
        evaluator = SituationEvaluator(store, clock=clock)
        runtime = GeneralSituationRuntime(store, clock=clock)
        scheduler = GeneralAttentionScheduler(store, clock=clock)
        outbox = SuggestionOutbox(store, clock=clock)
        user_id = "owner-a"
        session_id = "session-a"
        goals = [f"goal-{index}" for index in range(1, 10)]
        set_goals(store, user_id, goals)

        parent, pair = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[0],
            text_a="依赖监测给出第一份结构化观察。",
            text_b="A separately worded update reports the second observation.",
        )
        expect(
            parent.get("distinct_event_count") == 2
            and len(parent.get("child_refs") or []) == 2
            and all(set(ref) == CHILD_REF_KEYS for ref in parent["child_refs"]),
            "parent stores only four-field immutable child references",
            parent,
        )
        expect(
            parent.get("causality_asserted") is False
            and parent.get("model_similarity_used_for_merge") is False
            and not {
                "decision",
                "outcome",
                "observations",
                "inferences",
                "source_event",
            }.intersection(parent),
            "parent asserts neither child content nor causality",
            parent,
        )

        assessment = scheduler.assess(parent)
        expect(
            assessment.get("status") == "eligible"
            and assessment.get("eligible") is True
            and abs(float(assessment.get("score") or 0.0) - 0.8925) < 0.00001,
            "structured attention score is eligible",
            assessment,
        )
        words_changed = copy.deepcopy(parent)
        words_changed["display_text"] = "unrelated paraphrase words are non-authoritative"
        expect(
            scheduler.assess(words_changed).get("score") == assessment.get("score"),
            "free-text paraphrase does not change structured score",
        )

        state_before_model = file_digest(runtime.state_store.path_for(runtime.STATE_FILE))
        invalid_model = runtime.semantic_similarity_hypothesis(
            user_id=user_id,
            session_id=session_id,
            model_source="model_invalid_output",
            similarity=0.99,
            model_valid=False,
        )
        valid_model = runtime.semantic_similarity_hypothesis(
            user_id=user_id,
            session_id=session_id,
            model_source="model",
            similarity=0.99,
            model_valid=True,
        )
        expect(
            invalid_model.get("status") == "rejected"
            and valid_model.get("status") == "hypothesis_only"
            and valid_model.get("merge_allowed") is False
            and valid_model.get("causality_asserted") is False
            and file_digest(runtime.state_store.path_for(runtime.STATE_FILE))
            == state_before_model,
            "model similarity is non-persisted hypothesis and cannot merge",
        )

        replay = GeneralSituationRuntime(store, clock=clock).ingest_child(
            pair[0][1], event=pair[0][0]
        )
        restarted_view = GeneralSituationRuntime(store, clock=clock).list_for_owner(
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            replay.get("status") == "replayed"
            and restarted_view.get("count") == 1,
            "replay is idempotent and parent survives restart",
            {"replay": replay, "view": restarted_view},
        )
        expect(
            runtime.list_for_owner(
                user_id="owner-b", session_id=session_id
            ).get("count")
            == 0
            and runtime.list_for_owner(
                user_id=user_id, session_id="session-b"
            ).get("count")
            == 0,
            "general Situation reads require exact owner and included session",
        )

        # A decision/outcome written to event B must not mutate event A or be
        # copied into the observational parent.
        child_a_before = evaluator.get(
            pair[0][1]["situation_id"], user_id=user_id, session_id=session_id
        )
        child_b = evaluator.record_decision(
            pair[1][1]["situation_id"],
            {"kind": "inspect_only"},
            user_id=user_id,
            session_id=session_id,
        )
        child_b = evaluator.record_outcome(
            child_b["situation_id"],
            {"result": "observed-only"},
            user_id=user_id,
            session_id=session_id,
            evidence_verified=False,
        )
        child_a_after = evaluator.get(
            pair[0][1]["situation_id"], user_id=user_id, session_id=session_id
        )
        updated_parent = runtime.ingest_child(child_b).get("general_situation")
        expect(
            child_a_before == child_a_after
            and child_b.get("decision") is not None
            and child_b.get("outcome") is not None,
            "event B decision and outcome never mutate event A Situation",
        )
        expect(
            isinstance(updated_parent, dict)
            and not {"decision", "outcome", "observations", "inferences"}.intersection(
                updated_parent
            )
            and all(set(ref) == CHILD_REF_KEYS for ref in updated_parent["child_refs"]),
            "updated parent still contains references only",
            updated_parent,
        )

        missing_components = copy.deepcopy(COMPONENTS)
        missing_components.pop("uncertainty")
        unknown_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[1],
            text_a="structured incomplete evidence one",
            text_b="structured incomplete evidence two",
            components=missing_components,
        )
        unknown = scheduler.assess(unknown_parent)
        expect(
            unknown.get("status") == "awaiting_evidence"
            and unknown.get("eligible") is False
            and "uncertainty_unknown" in (unknown.get("unknowns") or []),
            "unknown structured input remains awaiting_evidence",
            unknown,
        )

        # Same user but different sessions may aggregate only on a currently
        # active durable Goal/Commitment. A task anchor is insufficient.
        task_left = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id="left-session",
            anchor_kind="task",
            anchor_id="task-nondurable",
            text="left task event",
        )
        task_right = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id="right-session",
            anchor_kind="task",
            anchor_id="task-nondurable",
            text="right task event",
        )
        task_one = runtime.ingest_child(task_left[1], event=task_left[0])
        task_two = runtime.ingest_child(task_right[1], event=task_right[0])
        expect(
            task_one.get("status") == "awaiting_distinct_event"
            and task_two.get("status") == "awaiting_distinct_event",
            "non-durable anchor cannot aggregate across sessions",
            {"left": task_one, "right": task_two},
        )
        cross_left = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id="left-session",
            anchor_kind="goal",
            anchor_id=goals[2],
            text="cross-session durable event one",
        )
        cross_right = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id="right-session",
            anchor_kind="goal",
            anchor_id=goals[2],
            text="cross-session durable event two",
        )
        runtime.ingest_child(cross_left[1], event=cross_left[0])
        cross_result = runtime.ingest_child(cross_right[1], event=cross_right[0])
        expect(
            cross_result.get("status") == "created"
            and cross_result["general_situation"].get("aggregation_scope")
            == "same_user_durable_anchor"
            and GeneralSituationRuntime(store, clock=clock).list_for_owner(
                user_id=user_id,
                session_id="right-session",
            ).get("count")
            == 1,
            "active durable Goal permits same-user cross-session aggregation",
            cross_result,
        )
        other_event, other_child = child_for(
            evaluator,
            normalizer,
            clock,
            user_id="owner-b",
            session_id="left-session",
            anchor_kind="goal",
            anchor_id=goals[2],
            text="another owner cannot join",
        )
        other_result = runtime.ingest_child(other_child, event=other_event)
        expect(
            other_result.get("status") == "awaiting_distinct_event",
            "different user never joins a durable-anchor parent",
            other_result,
        )

        expect(
            outbox.status().get("mode") == "record_only",
            "suggestions default to record_only",
        )
        recorded = outbox.consider(
            parent,
            assessment,
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            recorded.get("status") == "recorded"
            and outbox.list_inbox(
                user_id=user_id, session_id=session_id
            ).get("count")
            == 0,
            "record_only persists evidence but never surfaces an inbox item",
            recorded,
        )

        configure_mode(outbox, store, "advise_only")
        configure_policy(
            outbox,
            store,
            user_id=user_id,
            session_id=session_id,
            budget=1,
        )
        third_event, third_child = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id=session_id,
            anchor_kind="goal",
            anchor_id=goals[0],
            text="third structured observation creates a new parent revision",
        )
        surfaced_parent = runtime.ingest_child(
            third_child, event=third_event
        )["general_situation"]
        surfaced_assessment = scheduler.assess(surfaced_parent)
        surfaced = outbox.consider(
            surfaced_parent,
            surfaced_assessment,
            user_id=user_id,
            session_id=session_id,
        )
        proposal = surfaced.get("proposal") or {}
        expect(
            surfaced.get("status") == "pending"
            and outbox.list_inbox(
                user_id=user_id, session_id=session_id
            ).get("count")
            == 1
            and outbox.list_inbox(
                user_id="owner-b", session_id=session_id
            ).get("count")
            == 0,
            "advise_only surfaces only in the exact-owner Console inbox",
            surfaced,
        )
        expect(
            proposal.get("proposal_kind") == "informational"
            and proposal.get("delivery")
            == {
                "channel": "owner_scoped_console",
                "external_delivery": False,
                "feishu_delivery": False,
                "agent_delivery": False,
            }
            and all(value is False for value in proposal.get("authority", {}).values()),
            "proposal has no execution, tool, Agent, grant, route, or external authority",
            proposal,
        )
        outbox_revision = int(
            store.read_json("suggestion_outbox.json").get("_state_revision") or 0
        )
        try:
            outbox.dismiss(
                str(proposal.get("proposal_id")),
                user_id="owner-b",
                session_id=session_id,
                expected_state_revision=outbox_revision,
            )
        except PermissionError:
            wrong_owner_blocked = True
        else:
            wrong_owner_blocked = False
        expect(wrong_owner_blocked, "feedback mutation is exact-owner scoped")
        dismissed = outbox.dismiss(
            str(proposal.get("proposal_id")),
            user_id=user_id,
            session_id=session_id,
            expected_state_revision=outbox_revision,
            reason="not useful now",
        )
        fourth_event, fourth_child = child_for(
            evaluator,
            normalizer,
            clock,
            user_id=user_id,
            session_id=session_id,
            anchor_kind="goal",
            anchor_id=goals[0],
            text="fourth structured observation during dismiss cooldown",
        )
        cooldown_parent = runtime.ingest_child(
            fourth_child, event=fourth_event
        )["general_situation"]
        cooldown = outbox.consider(
            cooldown_parent,
            scheduler.assess(cooldown_parent),
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            dismissed.get("status") == "dismissed"
            and cooldown.get("status") == "suppressed"
            and cooldown.get("reason") == "dismissed_cooldown",
            "dismissal cooldown suppresses a newer revision",
            {"dismissed": dismissed, "cooldown": cooldown},
        )

        budget_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[3],
            text_a="daily budget observation one",
            text_b="daily budget observation two",
        )
        budget_result = outbox.consider(
            budget_parent,
            scheduler.assess(budget_parent),
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            budget_result.get("status") == "suppressed"
            and budget_result.get("reason") == "daily_budget_exhausted",
            "daily suggestion budget is enforced",
            budget_result,
        )

        clock.advance(days=1)
        configure_policy(
            outbox,
            store,
            user_id=user_id,
            session_id=session_id,
            budget=5,
            quiet_hours={
                "start_hour": 12,
                "end_hour": 13,
                "timezone": "UTC",
            },
        )
        quiet_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[4],
            text_a="quiet-hour observation one",
            text_b="quiet-hour observation two",
        )
        quiet_result = outbox.consider(
            quiet_parent,
            scheduler.assess(quiet_parent),
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            quiet_result.get("status") == "suppressed"
            and quiet_result.get("reason") == "quiet_hours",
            "quiet hours suppress Console surfacing",
            quiet_result,
        )

        configure_policy(
            outbox,
            store,
            user_id=user_id,
            session_id=session_id,
            budget=5,
            quiet_hours=None,
        )
        ack_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[5],
            text_a="acknowledgement observation one",
            text_b="acknowledgement observation two",
        )
        ack_pending = outbox.consider(
            ack_parent,
            scheduler.assess(ack_parent),
            user_id=user_id,
            session_id=session_id,
        )
        ack_revision = int(
            store.read_json("suggestion_outbox.json").get("_state_revision") or 0
        )
        acknowledged = outbox.acknowledge(
            ack_pending["proposal"]["proposal_id"],
            user_id=user_id,
            session_id=session_id,
            expected_state_revision=ack_revision,
        )
        expect(
            acknowledged.get("status") == "acknowledged",
            "acknowledgement is persisted with owner-scoped CAS",
            acknowledged,
        )

        configure_mode(outbox, store, "disabled")
        disabled_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[6],
            text_a="disabled observation one",
            text_b="disabled observation two",
        )
        disabled = outbox.consider(
            disabled_parent,
            scheduler.assess(disabled_parent),
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            disabled.get("status") == "disabled"
            and disabled.get("proposal") is None,
            "disabled mode creates no proposal",
            disabled,
        )
        configure_mode(outbox, store, "shadow")
        shadow_parent, _ = group_goal(
            evaluator,
            normalizer,
            runtime,
            clock,
            user_id=user_id,
            session_id=session_id,
            goal_id=goals[7],
            text_a="shadow observation one",
            text_b="shadow observation two",
        )
        inbox_count_before_shadow = outbox.list_inbox(
            user_id=user_id, session_id=session_id
        )["count"]
        shadow = outbox.consider(
            shadow_parent,
            scheduler.assess(shadow_parent),
            user_id=user_id,
            session_id=session_id,
        )
        expect(
            shadow.get("status") == "would_suggest"
            and outbox.list_inbox(
                user_id=user_id, session_id=session_id
            )["count"]
            == inbox_count_before_shadow,
            "shadow records counterfactual output without surfacing it",
            shadow,
        )

        # Pure GET/read paths may not advance revisions or touch bytes.
        general_path = store.path_for("general_situation_state.json")
        outbox_path = store.path_for("suggestion_outbox.json")
        before_reads = (file_digest(general_path), file_digest(outbox_path))
        runtime.list_for_owner(user_id=user_id, session_id=session_id)
        outbox.status()
        outbox.list_inbox(user_id=user_id, session_id=session_id)
        after_reads = (file_digest(general_path), file_digest(outbox_path))
        expect(before_reads == after_reads, "runtime GET projections are byte-pure")

        awareness = SimpleNamespace(
            event_awareness=SimpleNamespace(
                general_situations=runtime,
                suggestion_outbox=outbox,
            )
        )
        app = FastAPI()
        app.include_router(build_debug_audit_router({"awareness_loop": awareness}))
        client = TestClient(app)
        expect(
            client.get("/awareness/general-situations").status_code == 422
            and client.get("/awareness/suggestions/inbox").status_code == 422,
            "control-plane owner scope is mandatory",
        )
        before_http = (file_digest(general_path), file_digest(outbox_path))
        scoped_http = client.get(
            "/awareness/general-situations",
            params={"user_id": user_id, "session_id": session_id},
        )
        inbox_http = client.get(
            "/awareness/suggestions/inbox",
            params={"user_id": user_id, "session_id": session_id},
        )
        expect(
            scoped_http.status_code == 200
            and inbox_http.status_code == 200
            and before_http
            == (file_digest(general_path), file_digest(outbox_path)),
            "control-plane GET is scoped and byte-pure",
        )

        try:
            outbox.configure_mode(
                "record_only",
                expected_state_revision=0,
            )
        except SuggestionOutboxConflict:
            config_cas_closed = True
        else:
            config_cas_closed = False
        expect(config_cas_closed, "mode mutation rejects stale CAS")

        class FailingMaintenanceOwner:
            event_awareness = SimpleNamespace(
                reconcile_general_situations=lambda **_: (_ for _ in ()).throw(
                    RuntimeError("injected observational maintenance failure")
                )
            )

            def consume(self, *, limit: int) -> dict[str, Any]:
                return {"status": "success", "processed_count": 1, "limit": limit}

        active_loop = object.__new__(ActiveRuntimeLoop)
        active_loop.event_consumer = FailingMaintenanceOwner().consume
        maintenance_result = active_loop._event_inbox_tick()
        expect(
            maintenance_result.get("status") == "success"
            and maintenance_result.get("processed_count") == 1
            and (
                maintenance_result.get("general_situation_maintenance") or {}
            ).get("status")
            == "degraded",
            "general Situation maintenance failure cannot weaken the event step",
            maintenance_result,
        )

    # Out-of-order, digest conflict, expiry, and corrupt state use isolated
    # stores so fail-closed tests cannot damage the main scenario.
    with TemporaryDirectory(prefix="veyra-general-order-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        runtime = GeneralSituationRuntime(store, clock=clock)
        base = {
            "situation_id": "sit-order",
            "observation_revision": 2,
            "source_event_id": "evt-order",
            "user_id": "owner-a",
            "session_id": "session-a",
            "goal_refs": [{"kind": "goal", "id": "goal-order"}],
            "correlation_id": "evt-order",
            "source_event": {
                "event_id": "evt-order",
                "occurred_at": clock().isoformat(),
            },
            "created_at": clock().isoformat(),
            "status": "observed",
        }
        latest = runtime.ingest_child(base)
        stale = copy.deepcopy(base)
        stale["observation_revision"] = 1
        stale_result = runtime.ingest_child(stale)
        conflict = copy.deepcopy(base)
        conflict["salience_components"] = {"severity": 0.9}
        conflict_result = runtime.ingest_child(conflict)
        cas_result = runtime.ingest_child(
            {
                **base,
                "situation_id": "sit-cas",
                "source_event_id": "evt-cas",
                "correlation_id": "evt-cas",
            },
            expected_state_revision=9999,
        )
        expect(
            latest.get("status") == "awaiting_distinct_event"
            and stale_result.get("status") == "stale"
            and conflict_result.get("status") == "fail_closed"
            and cas_result.get("status") == "cas_conflict",
            "out-of-order, digest conflict, and stale CAS fail closed",
            {
                "latest": latest,
                "stale": stale_result,
                "conflict": conflict_result,
                "cas": cas_result,
            },
        )

    with TemporaryDirectory(prefix="veyra-general-expiry-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        normalizer = EventNormalizer()
        evaluator = SituationEvaluator(store, clock=clock)
        runtime = GeneralSituationRuntime(store, clock=clock)
        event_a, child_a = child_for(
            evaluator,
            normalizer,
            clock,
            user_id="owner-a",
            session_id="session-a",
            anchor_kind="task",
            anchor_id="task-expiry",
            text="old event",
        )
        runtime.ingest_child(child_a, event=event_a)
        clock.advance(hours=25)
        event_b, child_b = child_for(
            evaluator,
            normalizer,
            clock,
            user_id="owner-a",
            session_id="session-a",
            anchor_kind="task",
            anchor_id="task-expiry",
            text="event outside effective window",
        )
        expired_result = runtime.ingest_child(child_b, event=event_b)
        expect(
            expired_result.get("status") == "awaiting_distinct_event"
            and expired_result.get("general_situation_count") == 0
            and expired_result.get("candidate_count") == 1,
            "expired candidate cannot aggregate with a later event",
            expired_result,
        )
        store.path_for("general_situation_state.json").write_text(
            "{corrupt", encoding="utf-8"
        )
        corrupt = runtime.ingest_child(child_b, event=event_b)
        expect(
            corrupt.get("status") == "fail_closed"
            and corrupt.get("reason") == "general_situation_state_corrupt",
            "corrupt general Situation state fails closed",
            corrupt,
        )

    # Restart reconciliation must recover every supported non-durable anchor
    # from the persisted child snapshot. It is not allowed to depend on an
    # in-memory VeyraEvent that no longer exists after restart.
    with TemporaryDirectory(prefix="veyra-general-restart-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        normalizer = EventNormalizer()
        evaluator = SituationEvaluator(store, clock=clock)
        runtime = GeneralSituationRuntime(store, clock=clock)

        def anchored_child(
            *,
            kind: str,
            ref_id: str,
            event_id: str,
            workspace_id: str,
            components: dict[str, float] | None = None,
        ) -> tuple[Any, dict[str, Any]]:
            timestamp = clock().isoformat()
            event = normalizer.user_message(
                f"untrusted display text for {event_id}",
                "general-situation-restart-smoke",
                "owner-restart",
                "session-restart",
                event_id=event_id,
                subject={"kind": kind, "id": ref_id},
                occurred_at=timestamp,
                received_at=timestamp,
            )
            event.payload[f"{kind}_id"] = ref_id
            event.payload["workspace_id"] = workspace_id
            child = evaluator.observe(
                event,
                situation_id=f"sit-{event_id}",
                salience_components=copy.deepcopy(components or COMPONENTS),
            )
            clock.advance(seconds=1)
            return event, child

        revision_results: dict[str, Any] = {}
        revision_children: dict[str, dict[str, Any]] = {}
        for kind in ("task", "case", "entity", "trace"):
            event, first_child = anchored_child(
                kind=kind,
                ref_id=f"{kind}-restart",
                event_id=f"evt-{kind}-restart",
                workspace_id="workspace-restart",
            )
            first = runtime.ingest_child(first_child, event=event)
            revised_components = copy.deepcopy(COMPONENTS)
            revised_components["novelty"] = 0.61
            revised_child = evaluator.observe(
                event,
                situation_id=first_child["situation_id"],
                salience_components=revised_components,
            )
            restarted = GeneralSituationRuntime(store, clock=clock)
            revision_results[kind] = restarted.reconcile([revised_child])
            revision_children[kind] = revised_child
            persisted = store.read_json("general_situation_state.json")
            indexed = persisted["child_index"][revised_child["situation_id"]]
            candidate = persisted["candidates"][indexed["candidate_id"]]
            expect(
                first.get("status") == "awaiting_distinct_event"
                and revised_child.get("observation_revision") == 2
                and revision_results[kind].get("status") == "success"
                and indexed.get("max_observation_revision") == 2
                and candidate.get("child_ref", {}).get("observation_revision") == 2
                and {tuple(sorted(item.items())) for item in revised_child[
                    "structured_anchor_refs"
                ]}
                >= {
                    tuple(
                        sorted(
                            {"kind": kind, "ref_id": f"{kind}-restart"}.items()
                        )
                    ),
                    tuple(
                        sorted(
                            {
                                "kind": "workspace",
                                "ref_id": "workspace-restart",
                            }.items()
                        )
                    ),
                },
                f"{kind} revision reconciles after restart without an event",
                {
                    "first": first,
                    "reconcile": revision_results[kind],
                    "child": revised_child,
                    "index": indexed,
                    "candidate": candidate,
                },
            )

        try:
            StructuredAnchor("task", "free text is not an identifier")
        except ValueError:
            free_text_rejected = True
        else:
            free_text_rejected = False
        try:
            StructuredAnchor.from_dict(
                {"kind": "task", "ref_id": "task-1", "similarity": 0.99}
            )
        except ValueError:
            model_shape_rejected = True
        else:
            model_shape_rejected = False
        expect(
            free_text_rejected and model_shape_rejected,
            "structured anchor contract rejects free text and model metadata",
        )

        # Same typed anchor in two workspaces must remain two candidates. A
        # later event may aggregate only with the candidate in its workspace.
        event_a, child_a = anchored_child(
            kind="task",
            ref_id="task-workspace-boundary",
            event_id="evt-workspace-a1",
            workspace_id="workspace-a",
        )
        event_b, child_b = anchored_child(
            kind="task",
            ref_id="task-workspace-boundary",
            event_id="evt-workspace-b1",
            workspace_id="workspace-b",
        )
        event_a2, child_a2 = anchored_child(
            kind="task",
            ref_id="task-workspace-boundary",
            event_id="evt-workspace-a2",
            workspace_id="workspace-a",
        )
        first_a = runtime.ingest_child(child_a, event=event_a)
        first_b = runtime.ingest_child(child_b, event=event_b)
        second_a = GeneralSituationRuntime(store, clock=clock).ingest_child(child_a2)
        workspace_parent = second_a.get("general_situation") or {}
        parent_event_ids = {
            str(item.get("source_event_id") or "")
            for item in workspace_parent.get("child_refs", [])
        }
        expect(
            first_a.get("status") == "awaiting_distinct_event"
            and first_b.get("status") == "awaiting_distinct_event"
            and second_a.get("status") == "created"
            and workspace_parent.get("workspace_anchor_key")
            == "workspace:workspace-a"
            and parent_event_ids
            == {"evt-workspace-a1", "evt-workspace-a2"},
            "workspace anchor is an exact merge boundary after restart",
            {"a": first_a, "b": first_b, "a2": second_a},
        )

        # Rebinding an indexed child to another owner, session, or workspace
        # must be rejected before digest/revision replay logic.
        bound_child = revision_children["task"]
        owner_rebind = copy.deepcopy(bound_child)
        owner_rebind["user_id"] = "owner-other"
        session_rebind = copy.deepcopy(bound_child)
        session_rebind["session_id"] = "session-other"
        workspace_rebind = copy.deepcopy(bound_child)
        workspace_rebind["structured_anchor_refs"] = [
            item
            for item in workspace_rebind["structured_anchor_refs"]
            if item.get("kind") != "workspace"
        ] + [{"kind": "workspace", "ref_id": "workspace-other"}]
        owner_result = runtime.ingest_child(owner_rebind)
        session_result = runtime.ingest_child(session_rebind)
        workspace_result = runtime.ingest_child(workspace_rebind)
        expect(
            owner_result.get("reason") == "child_owner_binding_conflict"
            and session_result.get("reason") == "child_owner_binding_conflict"
            and workspace_result.get("reason")
            == "child_workspace_binding_conflict",
            "indexed child owner session and workspace rebinding fail closed",
            {
                "owner": owner_result,
                "session": session_result,
                "workspace": workspace_result,
            },
        )

        def remove_owner_binding(state: dict[str, Any]) -> dict[str, Any]:
            state["child_index"][bound_child["situation_id"]].pop("user_id", None)
            return state

        store.mutate_json("general_situation_state.json", remove_owner_binding)
        missing_binding = runtime.ingest_child(bound_child)
        expect(
            missing_binding.get("status") == "fail_closed"
            and missing_binding.get("reason") == "child_owner_binding_missing",
            "missing child owner binding fails closed",
            missing_binding,
        )

    # A revised child already attached to a parent replaces the old immutable
    # reference after restart; it never leaves two revisions of one Situation.
    with TemporaryDirectory(prefix="veyra-general-parent-revision-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        normalizer = EventNormalizer()
        evaluator = SituationEvaluator(store, clock=clock)
        runtime = GeneralSituationRuntime(store, clock=clock)

        def parent_child(event_id: str) -> tuple[Any, dict[str, Any]]:
            timestamp = clock().isoformat()
            event = normalizer.user_message(
                event_id,
                "general-parent-revision-smoke",
                "owner-parent",
                "session-parent",
                event_id=event_id,
                subject={"kind": "case", "id": "case-parent-revision"},
                occurred_at=timestamp,
                received_at=timestamp,
            )
            event.payload["case_id"] = "case-parent-revision"
            event.payload["workspace_id"] = "workspace-parent"
            child = evaluator.observe(
                event,
                situation_id=f"sit-{event_id}",
                salience_components=copy.deepcopy(COMPONENTS),
            )
            clock.advance(seconds=1)
            return event, child

        event_one, child_one = parent_child("evt-parent-one")
        event_two, child_two = parent_child("evt-parent-two")
        runtime.ingest_child(child_one, event=event_one)
        created_parent = runtime.ingest_child(child_two, event=event_two)
        revised_components = copy.deepcopy(COMPONENTS)
        revised_components["urgency"] = 0.53
        child_one_revision = evaluator.observe(
            event_one,
            situation_id=child_one["situation_id"],
            salience_components=revised_components,
        )
        restarted = GeneralSituationRuntime(store, clock=clock)
        reconcile = restarted.reconcile([child_one_revision])
        state = store.read_json("general_situation_state.json")
        parent_id = state["child_index"][child_one["situation_id"]][
            "general_situation_id"
        ]
        recovered_parent = state["general_situations"][parent_id]
        matching_refs = [
            item
            for item in recovered_parent["child_refs"]
            if item.get("situation_id") == child_one["situation_id"]
        ]
        removed_anchor_revision = copy.deepcopy(child_one_revision)
        removed_anchor_revision["observation_revision"] = 3
        removed_anchor_revision["structured_anchor_refs"] = [
            item
            for item in removed_anchor_revision["structured_anchor_refs"]
            if item.get("kind") == "workspace"
        ]
        rebound_anchor_revision = copy.deepcopy(child_one_revision)
        rebound_anchor_revision["observation_revision"] = 3
        rebound_anchor_revision["structured_anchor_refs"] = [
            (
                {"kind": "case", "ref_id": "case-parent-rebound"}
                if item.get("kind") == "case"
                else item
            )
            for item in rebound_anchor_revision["structured_anchor_refs"]
        ]
        removed_anchor = restarted.ingest_child(removed_anchor_revision)
        rebound_anchor = restarted.ingest_child(rebound_anchor_revision)
        state_path = store.path_for("general_situation_state.json")
        before_get = file_digest(state_path)
        restarted.list_for_owner(
            user_id="owner-parent",
            session_id="session-parent",
        )
        after_get = file_digest(state_path)
        expect(
            created_parent.get("status") == "created"
            and reconcile.get("status") == "success"
            and reconcile.get("grouped_count") == 1
            and len(recovered_parent.get("child_refs") or []) == 2
            and len(matching_refs) == 1
            and matching_refs[0].get("observation_revision") == 2,
            "restart reconciliation replaces the old parent child revision",
            {
                "created": created_parent,
                "reconcile": reconcile,
                "parent": recovered_parent,
            },
        )
        expect(
            removed_anchor.get("status") == "fail_closed"
            and removed_anchor.get("reason")
            == "child_parent_anchor_binding_conflict"
            and rebound_anchor.get("status") == "fail_closed"
            and rebound_anchor.get("reason")
            == "child_parent_anchor_binding_conflict",
            "child revision cannot remove or rebind a parent merge anchor",
            {
                "removed": removed_anchor,
                "rebound": rebound_anchor,
            },
        )
        expect(
            before_get == after_get,
            "restart-recovered general Situation GET remains byte-pure",
        )

    print("general situation + informational suggestion smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
