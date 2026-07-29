#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.semantic_generalization_smoke as semantic_smoke  # noqa: E402
from core.memory_policy_runtime import MemoryPolicyRuntime  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


PREFERENCE_TEXT = "以后回答尽量简短，先给结论。"
PROACTIVE_TEXT = "每天九点，上海出门前给我一条气象简报。"
CANCEL_TEXT = "PyTorch 那条以后别再跟了。"
MULTI_PROACTIVE_TEXT = "每天九点提醒我喝水，十八点提醒我写日报。"
DENIAL_EXPLAIN_TEXT = "不要取消 PyTorch，只解释一下 cancel_task。"
RESOLVED_REFERENT_TEXT = "把那个停掉。"
MULTI_PROBE_TEXT = "查上海天气，同时看看当前 git 有没有脏文件。"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def install_fixtures() -> None:
    semantic_smoke.FRAME_FIXTURES[PREFERENCE_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                PREFERENCE_TEXT,
                act_id="pref-1",
                kind="preference",
                operation="update_response_preference",
                goal="回答时优先简洁并先给结论",
                quote=PREFERENCE_TEXT[:-1],
                target=semantic_smoke._target(
                    "response_style",
                    "简短且结论优先",
                ),
                arguments={"verbosity": "concise", "answer_order": "conclusion_first"},
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[PROACTIVE_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                PROACTIVE_TEXT,
                act_id="proactive-1",
                kind="recurring_request",
                operation="schedule_morning_brief",
                goal="每天九点在上海出门前收到气象简报",
                quote=PROACTIVE_TEXT[:-1],
                target=semantic_smoke._target("weather_summary", "上海"),
                arguments={
                    "location": "上海",
                    "schedule": {
                        "kind": "daily",
                        "time_local": "09:00",
                        "timezone": "Asia/Shanghai",
                        "interval_seconds": 86400.0,
                    },
                },
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[CANCEL_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                CANCEL_TEXT,
                act_id="cancel-1",
                kind="commitment_control",
                operation="discontinue_monitoring",
                goal="停止跟进 PyTorch",
                quote=CANCEL_TEXT[:-1],
                target=semantic_smoke._target("tracked_topic", "PyTorch"),
                arguments={"topic": "PyTorch", "scope": "matching"},
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[MULTI_PROACTIVE_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                MULTI_PROACTIVE_TEXT,
                act_id="reminder-1",
                kind="recurring_request",
                operation="schedule_hydration_reminder",
                goal="每天九点提醒喝水",
                quote="每天九点提醒我喝水",
                target=semantic_smoke._target("reminder", "喝水"),
                arguments={
                    "note": "喝水",
                    "schedule": {
                        "kind": "daily",
                        "time_local": "09:00",
                        "timezone": "Asia/Shanghai",
                        "interval_seconds": 86400.0,
                    },
                },
            ),
            semantic_smoke._act(
                MULTI_PROACTIVE_TEXT,
                act_id="reminder-2",
                kind="recurring_request",
                operation="schedule_report_reminder",
                goal="每天十八点提醒写日报",
                quote="十八点提醒我写日报",
                target=semantic_smoke._target("reminder", "写日报"),
                arguments={
                    "note": "写日报",
                    "schedule": {
                        "kind": "daily",
                        "time_local": "18:00",
                        "timezone": "Asia/Shanghai",
                        "interval_seconds": 86400.0,
                    },
                },
            ),
        ],
        relations=[{"type": "sequence", "from": "reminder-1", "to": "reminder-2"}],
    )
    semantic_smoke.FRAME_FIXTURES[DENIAL_EXPLAIN_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                DENIAL_EXPLAIN_TEXT,
                act_id="deny-cancel",
                kind="prohibition",
                operation="cancel_task",
                goal="保持 PyTorch 跟踪",
                quote="不要取消 PyTorch",
                target=semantic_smoke._target("tracked_topic", "PyTorch"),
                polarity="negative",
            ),
            semantic_smoke._act(
                DENIAL_EXPLAIN_TEXT,
                act_id="explain-cancel",
                kind="request",
                operation="explain",
                goal="解释 cancel_task",
                quote="只解释一下 cancel_task",
                target=semantic_smoke._target("concept", "cancel_task"),
            ),
        ],
        relations=[{"type": "contrast", "from": "deny-cancel", "to": "explain-cancel"}],
    )
    semantic_smoke.FRAME_FIXTURES[RESOLVED_REFERENT_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                RESOLVED_REFERENT_TEXT,
                act_id="resolved-cancel",
                kind="commitment_control",
                operation="discontinue_monitoring",
                goal="停止所指的 PyTorch 跟踪",
                quote=RESOLVED_REFERENT_TEXT[:-1],
                target=semantic_smoke._target("tracked_topic", ""),
                referent={
                    "surface": "那个",
                    "resolved": "PyTorch",
                    "status": "resolved",
                    "candidates": ["PyTorch"],
                },
                arguments={"scope": "matching"},
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[MULTI_PROBE_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                MULTI_PROBE_TEXT,
                act_id="weather-read",
                kind="request",
                operation="query_current_weather",
                goal="查询上海当前天气",
                quote="查上海天气",
                target=semantic_smoke._target("weather", "上海"),
                evidence_need="fresh_external_weather",
                arguments={"location": "上海"},
            ),
            semantic_smoke._act(
                MULTI_PROBE_TEXT,
                act_id="git-read",
                kind="question",
                operation="query_git_status",
                goal="查询当前 git 工作区状态",
                quote="看看当前 git 有没有脏文件",
                target=semantic_smoke._target("git", "current_repository"),
                evidence_need="local_git_status",
            ),
        ],
        relations=[{"type": "parallel", "from": "weather-read", "to": "git-read"}],
    )


class CountingLocalMemory:
    def __init__(self, store: WorldStateStore) -> None:
        self.store = store
        self.write_attempts: list[dict[str, Any]] = []

    def read_summary(
        self,
        session_id: str,
        focus: list[str] | None = None,
        provider: str = "local",
        *,
        user_id: str,
    ) -> dict[str, Any]:
        del focus, provider
        return {
            "user_id": user_id,
            "session_id": session_id,
            "provider": "local",
            "summary": "",
            "external_summary": {},
            "freshness": "fresh",
            "trust": "local",
            "relevance": {"status": "not_needed"},
        }

    def write_patch(self, patch: dict[str, Any], provider: str = "local") -> dict[str, Any]:
        self.write_attempts.append(copy.deepcopy(patch))
        item = {
            "memory_id": f"smoke-memory-{len(self.write_attempts)}",
            "patch": copy.deepcopy(patch),
            "provider": provider,
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            items = state.get("items") if isinstance(state.get("items"), list) else []
            state["items"] = [*items, item]
            return state

        stored = self.store.mutate_json("agent_memory.json", update)
        return {
            "status": "written",
            "item": item,
            "state_revision": int(stored.get("_state_revision") or 0),
        }


def harness_with_local_memory(root: Path) -> tuple[Any, CountingLocalMemory]:
    store = WorldStateStore(root)
    harness = semantic_smoke._build_harness(store)
    memory = CountingLocalMemory(store)
    harness.loop.memory_bridge = memory  # type: ignore[assignment]
    harness.commitment_core.memory_bridge = memory  # type: ignore[assignment]
    harness.loop.memory_policy_runtime = MemoryPolicyRuntime(store, memory.write_patch)
    return harness, memory


def event_for(harness: Any, text: str, *, event_id: str, session_id: str) -> Any:
    event = harness.normalizer.user_message(
        text=text,
        channel="semantic_state_smoke",
        user_id="semantic-state-user",
        session_id=session_id,
        metadata={"semantic_state_smoke": True},
    )
    event.event_id = event_id
    return event


def test_preference_replay(root: Path) -> None:
    harness, memory = harness_with_local_memory(root)
    event = event_for(
        harness,
        PREFERENCE_TEXT,
        event_id="evt-semantic-preference",
        session_id="semantic-preference",
    )
    first = harness.loop.handle_event(event).to_dict()
    second = harness.loop.handle_event(event).to_dict()
    proposals = harness.store.read_json("state_change_proposals.json").get("proposals", {})
    memory_items = harness.store.read_json("agent_memory.json").get("items", [])
    expect(len(memory.write_attempts) == 1, "preference replay invokes the memory writer once")
    expect(len(memory_items) == 1, "preference replay persists one memory item")
    expect(
        len(
            [
                item
                for item in proposals.values()
                if isinstance(item, dict) and item.get("effect") == "memory.write"
            ]
        )
        == 1,
        "preference produces one typed memory proposal",
    )
    first_change = first.get("artifacts", {}).get("memory_policy_execution", {}).get("state_change", {})
    second_change = second.get("artifacts", {}).get("memory_policy_execution", {}).get("state_change", {})
    expect(first_change.get("status") == "committed", "first preference commit succeeds", first_change)
    expect(
        second_change.get("status") == "committed" and second_change.get("replayed") is True,
        "second preference turn replays the recorded commit",
        second_change,
    )


def test_open_proactive_paraphrase(root: Path) -> None:
    harness, memory = harness_with_local_memory(root)
    before_world = copy.deepcopy(harness.store.read_json("user_world.json"))
    event = event_for(
        harness,
        PROACTIVE_TEXT,
        event_id="evt-semantic-proactive",
        session_id="semantic-proactive",
    )
    first = harness.loop.handle_event(event).to_dict()
    second = harness.loop.handle_event(event).to_dict()
    commitments = harness.commitment_core.list_commitments(
        user_id="semantic-state-user",
        session_id="semantic-proactive",
    )
    expect(len(commitments) == 1, "semantic proactive paraphrase creates one commitment")
    expect(
        commitments[0].get("kind") == "weather_daily"
        and commitments[0].get("status") == "active"
        and commitments[0].get("payload", {}).get("location") == "上海",
        "typed semantic arguments drive the proactive task",
        commitments,
    )
    expect(len(memory.write_attempts) == 0, "commitment authorization does not imply memory.write")
    after_world = harness.store.read_json("user_world.json")
    expect(
        before_world.get("preferences") == after_world.get("preferences"),
        "proactive authorization does not imply profile.write/default-location mutation",
    )
    first_commit = first.get("artifacts", {}).get("commitment", {})
    second_commit = second.get("artifacts", {}).get("commitment", {})
    expect(
        first_commit.get("state_change", {}).get("status") == "committed",
        "first proactive proposal commits",
        first_commit,
    )
    expect(
        second_commit.get("state_change", {}).get("replayed") is True,
        "proactive event replay does not create a second task",
        second_commit,
    )


def test_semantic_control_without_nested_writes(root: Path) -> None:
    harness, memory = harness_with_local_memory(root)
    seeded = harness.commitment_core.create_commitment(
        {
            "kind": "external_digest",
            "status": "active",
            "title": "持续关注：PyTorch",
            "user_id": "semantic-state-user",
            "channel": "semantic_state_smoke",
            "session_id": "semantic-cancel",
            "source_event_id": "seed-pytorch",
            "schedule": {
                "kind": "daily",
                "time_local": "09:00",
                "timezone": "Asia/Shanghai",
                "interval_seconds": 86400.0,
            },
            "payload": {"topic": "PyTorch", "query": "PyTorch updates"},
        }
    )
    memory.write_attempts.clear()
    before_world = copy.deepcopy(harness.store.read_json("user_world.json"))
    event = event_for(
        harness,
        CANCEL_TEXT,
        event_id="evt-semantic-cancel",
        session_id="semantic-cancel",
    )
    result = harness.loop.handle_event(event).to_dict()
    updated = harness.commitment_core.get_commitment(str(seeded.get("commitment_id") or ""))
    expect(updated is not None and updated.get("status") == "cancelled", "open semantic operation cancels the intended task", result)
    expect(len(memory.write_attempts) == 0, "commitment control does not write memory without authorization")
    after_world = harness.store.read_json("user_world.json")
    expect(
        before_world.get("profiles_by_user") == after_world.get("profiles_by_user"),
        "commitment control does not mutate the user profile",
    )
    state_change = result.get("artifacts", {}).get("commitment", {}).get("state_change", {})
    expect(state_change.get("status") == "committed", "commitment control is recorded through Proposal/Commit", state_change)


def test_failed_memory_never_claims_success(root: Path) -> None:
    store = WorldStateStore(root)
    harness = semantic_smoke._build_harness(store)

    def failing_writer(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("simulated durable memory failure")

    harness.loop.memory_policy_runtime = MemoryPolicyRuntime(store, failing_writer)
    event = event_for(
        harness,
        PREFERENCE_TEXT,
        event_id="evt-semantic-memory-failure",
        session_id="semantic-memory-failure",
    )
    result = harness.loop.handle_event(event).to_dict()
    state_change = result.get("artifacts", {}).get("memory_policy_execution", {}).get("state_change", {})
    expect(state_change.get("status") == "indeterminate", "failed writer is conservatively indeterminate", state_change)
    expect(
        result.get("response") == "我理解了你的偏好，但这次没有成功保存；后续不会假装已经记住。",
        "user response consumes the real commit result",
        result.get("response"),
    )


def test_multi_proactive_acts_are_atomic_per_act(root: Path) -> None:
    harness, _ = harness_with_local_memory(root)
    event = event_for(
        harness,
        MULTI_PROACTIVE_TEXT,
        event_id="evt-semantic-multi-proactive",
        session_id="semantic-multi-proactive",
    )
    first = harness.loop.handle_event(event).to_dict()
    second = harness.loop.handle_event(event).to_dict()
    commitments = harness.commitment_core.list_commitments(
        user_id="semantic-state-user",
        session_id="semantic-multi-proactive",
    )
    times = sorted(
        str(
            (
                item.get("schedule")
                if isinstance(item.get("schedule"), dict)
                else {}
            ).get("time_local")
            or ""
        )
        for item in commitments
    )
    proposals = harness.store.read_json("state_change_proposals.json").get("proposals", {})
    proactive_proposals = [
        item
        for item in proposals.values()
        if isinstance(item, dict) and item.get("effect") == "proactive.create"
    ]
    expect(len(commitments) == 2, "two proactive acts create two commitments", commitments)
    expect(times == ["09:00", "18:00"], "each proactive act keeps its own schedule", times)
    expect(len(proactive_proposals) == 2, "each proactive act has its own proposal", proactive_proposals)
    expect(
        len(first.get("artifacts", {}).get("commitment", {}).get("outcomes", [])) == 2,
        "the first multi-act result exposes both outcomes",
        first,
    )
    expect(
        all(
            item.get("state_change", {}).get("replayed") is True
            for item in second.get("artifacts", {}).get("commitment", {}).get("outcomes", [])
        ),
        "replaying the multi-act event reuses both commits",
        second,
    )


def test_denial_and_resolved_referent_boundaries(root: Path) -> None:
    harness, _ = harness_with_local_memory(root)
    seed = harness.commitment_core.create_commitment(
        {
            "kind": "external_digest",
            "status": "active",
            "title": "持续关注：PyTorch",
            "user_id": "semantic-state-user",
            "channel": "semantic_state_smoke",
            "session_id": "semantic-denial",
            "source_event_id": "seed-denial-pytorch",
            "schedule": {
                "kind": "daily",
                "time_local": "09:00",
                "timezone": "Asia/Shanghai",
                "interval_seconds": 86400.0,
            },
            "payload": {"topic": "PyTorch", "query": "PyTorch updates"},
        }
    )
    denial_event = event_for(
        harness,
        DENIAL_EXPLAIN_TEXT,
        event_id="evt-denial-explain",
        session_id="semantic-denial",
    )
    denial_result = harness.loop.handle_event(denial_event).to_dict()
    preserved = harness.commitment_core.get_commitment(str(seed.get("commitment_id") or ""))
    expect(
        preserved is not None and preserved.get("status") == "active",
        "a cancellation denial plus explanation preserves the task",
        denial_result,
    )

    referent_event = event_for(
        harness,
        RESOLVED_REFERENT_TEXT,
        event_id="evt-resolved-referent",
        session_id="semantic-denial",
    )
    harness.loop.handle_event(referent_event)
    cancelled = harness.commitment_core.get_commitment(str(seed.get("commitment_id") or ""))
    expect(
        cancelled is not None and cancelled.get("status") == "cancelled",
        "a model-resolved referent controls the intended commitment",
        cancelled,
    )


def test_multi_probe_turn_executes_every_read_act(root: Path) -> None:
    harness, _ = harness_with_local_memory(root)
    event = event_for(
        harness,
        MULTI_PROBE_TEXT,
        event_id="evt-semantic-multi-probe",
        session_id="semantic-multi-probe",
    )
    result = harness.loop.handle_event(event).to_dict()
    decision = result.get("artifacts", {}).get("decision", {})
    policy = (
        decision.get("model_assist", {}).get("semantic_policy", {})
        if isinstance(decision, dict)
        else {}
    )
    probe_results = result.get("artifacts", {}).get("probe_results", [])
    expect(
        len(policy.get("probe_requests", [])) == 2,
        "semantic policy keeps both read acts in its probe plan",
        policy,
    )
    expect(
        len(harness.probes["weather_probe"].calls) == 1
        and len(harness.probes["git"].calls) == 1,
        "the runtime executes both authorized probes",
        {
            "weather": harness.probes["weather_probe"].calls,
            "git": harness.probes["git"].calls,
        },
    )
    expect(
        len(probe_results) == 2
        and {str(item.get("probe") or "") for item in probe_results}
        == {"weather_probe", "git"},
        "the final result preserves both probe outputs",
        result,
    )


def main() -> None:
    install_fixtures()
    with TemporaryDirectory(prefix="veyra-semantic-state-effects-") as tmp:
        base = Path(tmp)
        test_preference_replay(base / "preference")
        test_open_proactive_paraphrase(base / "proactive")
        test_semantic_control_without_nested_writes(base / "control")
        test_failed_memory_never_claims_success(base / "failure")
        test_multi_proactive_acts_are_atomic_per_act(base / "multi")
        test_denial_and_resolved_referent_boundaries(base / "boundaries")
        test_multi_probe_turn_executes_every_read_act(base / "multi-probe")
    print("semantic state-effect smoke passed")


if __name__ == "__main__":
    main()
