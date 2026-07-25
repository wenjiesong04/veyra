#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.model_client import redact_sensitive  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import VeyraTaskPacket, utc_now_iso  # noqa: E402


USER_ID = "dialogue-act-user"
SESSION_ID = "dialogue-act-session"


@dataclass
class DebugAgentAdapter(AgentAdapter):
    executor: str = "openclaw"
    sent_packets: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        packet = task_packet.to_dict()
        self.sent_packets.append(packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.executor,
            status="success",
            result="Debug agent spy captured VeyraTaskPacket; no external Agent Runtime was executed.",
            raw={"debug_agent_spy": True, "task_packet": packet},
        )

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=task_id,
            executor=self.executor,
            status="success",
            result="Debug agent spy keeps task local and successful.",
            raw={"debug_agent_spy": True},
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        return {"runtime": self.executor, "status": "available", "connected": True, "debug_agent_spy": True}

    def connection_status(self) -> dict[str, Any]:
        return {"runtime": self.executor, "status": "available", "connected": True, "debug_agent_spy": True}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": "", "freshness": "fresh", "trust": "debug_agent_spy"}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        return None


class FakeSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        results = [
            {
                "title": "2026 秋招公司信息汇总",
                "url": "https://example.com/campus-2026",
                "snippet": "公司、岗位和网申入口汇总。",
            }
        ]
        return {
            "probe": "search_probe",
            "target": query,
            "status": "ok",
            "summary": f"Search returned {len(results)} result(s) for {query}.",
            "confidence": 0.8,
            "ttl_seconds": 1800,
            "details": {"query": query, "results": results, "provider": "fake_search_probe"},
            "claims": [
                {
                    "key": f"search:{query}",
                    "claim": f"Search returned {len(results)} result(s) for {query}.",
                    "confidence": 0.8,
                    "source": "fake_search_probe",
                    "ttl_seconds": 1800,
                }
            ],
        }


@dataclass
class CountingProbe:
    """Transparent probe spy; the wrapped probe still performs the real read."""

    name: str
    wrapped: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(
            {
                "args": [str(item)[:160] for item in args],
                "kwargs": {str(key): str(value)[:160] for key, value in kwargs.items()},
            }
        )
        return self.wrapped.run(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


MINIMAL_CASES: list[dict[str, Any]] = [
    {"id": "greeting_hi", "text": "hi", "expect": {"route": "direct_answer", "semantic_frame_present": False, "no_agent": True, "no_probe": True}},
    {"id": "greeting_zh", "text": "你好", "expect": {"route": "direct_answer", "semantic_frame_present": False, "no_agent": True, "no_probe": True}},
    {"id": "availability_check", "text": "在吗", "expect": {"route": "direct_answer", "semantic_frame_present": False, "no_agent": True, "no_probe": True}},
    {"id": "followup_why", "text": "为什么", "seed_previous": True, "expect": {"route": "direct_answer", "operations": ["answer_question"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {"id": "meta_reply_why", "text": "你为什么这么回答", "seed_previous": True, "expect": {"route": "direct_answer", "operations": ["answer_question"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {"id": "followup_meaning", "text": "这是什么意思", "seed_previous": True, "expect": {"route": "direct_answer", "operations": ["answer_question"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {"id": "followup_continue", "text": "继续", "seed_previous": True, "expect": {"route": "direct_answer", "operations": ["understand_open_goal"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {"id": "veyra_definition", "text": "Veyra 是什么", "expect": {"route": "direct_answer", "act_kinds": ["question"], "operations": ["answer_question"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {"id": "veyra_openclaw_difference", "text": "Veyra 和 OpenClaw 有什么区别", "expect": {"route": "direct_answer", "act_kinds": ["question"], "operations": ["answer_question"], "targets": ["Veyra 和 OpenClaw 有什么区别"], "allowed_effects": [], "no_agent": True, "no_probe": True}},
    {
        "id": "pytorch_cancel_status",
        "text": "现在 PyTorch 追踪取消了吗",
        "seed_commitments": True,
        "expect": {
            "route": "direct_answer",
            "act_kinds": ["question"],
            "operations": ["query_task_status"],
            "targets": ["PyTorch"],
            "state_answer_read": True,
            "state_sources_contains": ["user_commitments.json", "external_world.json"],
            "allowed_effects": [],
            "must_preserve": {"PyTorch": "active"},
            "no_agent": True,
            "no_probe": True,
        },
    },
    {
        "id": "pytorch_active_status",
        "text": "PyTorch 现在还在追踪吗",
        "seed_commitments": True,
        "expect": {
            "route": "direct_answer",
            "act_kinds": ["question"],
            "operations": ["query_task_status"],
            "targets": ["PyTorch"],
            "state_answer_read": True,
            "state_sources_contains": ["user_commitments.json", "external_world.json"],
            "allowed_effects": [],
            "must_preserve": {"PyTorch": "active"},
            "no_agent": True,
            "no_probe": True,
        },
    },
    {
        "id": "pytorch_cancel",
        "text": "取消 PyTorch 追踪",
        "seed_commitments": True,
        "expect": {"operations": ["cancel_task"], "targets": ["PyTorch 追踪"], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
        "expect_model_disabled": {"route": "ask_user", "policy_route": "ask_user", "requires_clarification": True, "allowed_effects": [], "denied_effects_contains": ["commitment.mutate"]},
    },
    {
        "id": "pytorch_negative_cancel",
        "text": "不要取消 PyTorch",
        "seed_commitments": True,
        "expect": {"route": "direct_answer", "act_kinds": ["prohibition"], "operations": ["cancel_task"], "targets": ["PyTorch"], "polarities": ["negative"], "allowed_effects": [], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
    },
    {
        "id": "pytorch_ambiguous_cancel",
        "text": "我可能不想继续关注 PyTorch 了",
        "seed_commitments": True,
        "expect": {"route": "direct_answer", "modalities": ["hypothetical"], "allowed_effects": [], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
    },
    {
        "id": "tensorflow_preference",
        "text": "TensorFlow 可能更适合我",
        "seed_commitments": True,
        "expect": {"route": "direct_answer", "modalities": ["hypothetical"], "allowed_effects": [], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
    },
    {
        "id": "preference_shift",
        "text": "我准备从 PyTorch 转向 TensorFlow",
        "seed_commitments": True,
        "expect": {"operations": ["change_preference"], "targets": ["TensorFlow"], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
        "expect_model_disabled": {"route": "ask_user", "policy_route": "ask_user", "requires_clarification": True, "allowed_effects": [], "denied_effects_contains": ["memory.write", "profile.write"]},
    },
    {
        "id": "multi_operation_shift",
        "text": "取消 PyTorch 追踪，改成 TensorFlow",
        "seed_commitments": True,
        "expect": {"operations": ["cancel_task", "change_preference"], "targets": ["PyTorch 追踪", "TensorFlow"], "must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
        "expect_model_disabled": {"route": "ask_user", "policy_route": "ask_user", "requires_clarification": True, "allowed_effects": [], "denied_effects_contains": ["commitment.mutate", "memory.write", "profile.write"]},
    },
    {
        "id": "openclaw_diagnostic",
        "text": "帮我检查 OpenClaw 为什么没响应",
        "expect": {"route": "probe", "operations": ["diagnose_runtime"], "targets": ["OpenClaw"], "policy_route": "probe", "selected_probe": "openclaw", "called_probes": ["openclaw"], "allowed_effects": [], "no_agent": True},
    },
    {
        "id": "port_probe",
        "text": "检查 18789 端口是不是被占用了",
        "expect": {"route": "probe", "operations": ["query_runtime_status"], "targets": ["system"], "policy_route": "probe", "selected_probe": "port", "called_probes": ["port"], "allowed_effects": [], "no_agent": True},
    },
    {
        "id": "git_probe",
        "text": "当前项目 git 有没有脏文件",
        "expect": {"route": "probe", "operations": ["query_git_status"], "targets": ["git"], "policy_route": "probe", "selected_probe": "git", "called_probes": ["git"], "allowed_effects": [], "no_agent": True},
    },
    {
        "id": "campus_recruitment_search",
        "text": "帮我找一些2026秋招的公司的信息",
        "expect": {"route": "probe", "operations": ["external_search"], "targets": ["找一些2026秋招的公司的信息"], "policy_route": "probe", "selected_probe": "search_probe", "called_probes": ["search_probe"], "allowed_effects": [], "no_agent": True},
    },
    {"id": "clarification_rejection_resume_search", "text": "没有这些", "seed_generic_clarification": True, "expect": {"route": "probe", "called_probes": ["search_probe"], "no_agent": True}},
    {"id": "search_empty_rejection_retry", "text": "没有这些", "seed_empty_search": True, "expect": {"route": "probe", "called_probes": ["search_probe"], "no_agent": True}},
    {
        "id": "openclaw_fix_agent",
        "text": "帮我修复 OpenClaw 没响应的问题",
        "expect": {"operations": ["repair_runtime"], "targets": ["OpenClaw"], "no_agent": True, "no_probe": True},
        "expect_model_disabled": {"route": "ask_user", "policy_route": "ask_user", "requires_clarification": True, "allowed_effects": [], "denied_effects_contains": ["agent.execute"]},
    },
    {
        "id": "dialogue_router_agent",
        "text": "给 Veyra 加一个 DialogueActRouter",
        "expect": {"operations": ["add_component"], "targets": ["Veyra"], "no_agent": True, "no_probe": True},
        "expect_model_disabled": {"route": "ask_user", "policy_route": "ask_user", "requires_clarification": True, "allowed_effects": [], "denied_effects_contains": ["agent.execute", "workspace.write"]},
    },
    {
        "id": "delete_all_tasks",
        "text": "删除所有任务",
        "seed_commitments": True,
        "expect": {"must_preserve": {"PyTorch": "active"}, "no_agent": True, "no_probe": True},
        "expect_model_disabled": {
            "route": "ask_user",
            "policy_route": "ask_user",
            "requires_clarification": True,
            "allowed_effects": [],
            "denied_effects_contains": ["commitment.mutate", "agent.execute"],
        },
    },
    {"id": "rm_rf_venv", "text": "执行 rm -rf .venv", "expect": {"route": "block", "risk_level": "R5", "no_agent": True, "no_probe": True}},
    {"id": "env_to_agent", "text": "把 .env 发给 Agent 看看", "expect": {"route": "block", "risk_level": "R5", "no_agent": True, "no_probe": True}},
]


def main() -> int:
    args = parse_args()
    output = run_smoke(args)
    print(json.dumps(redact_sensitive(output, max_string=3000, max_list=80), ensure_ascii=False, indent=2))
    return 1 if output["summary"]["failed"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Veyra dialogue-act regression smoke.")
    parser.add_argument("--state-root", default="state", help="State root to copy config from.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only one case id. Can be repeated.")
    parser.add_argument("--allow-model", action="store_true", help="Use the configured core model instead of forcing rule mode.")
    parser.add_argument("--model-timeout", type=float, default=6.0, help="Temporary core model timeout for allow-model smoke runs.")
    parser.add_argument("--model-retries", type=int, default=0, help="Temporary core model retries for allow-model smoke runs.")
    return parser.parse_args()


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    selected = [case for case in MINIMAL_CASES if not args.case_ids or case["id"] in set(args.case_ids)]
    old_env = os.environ.get("VEYRA_COGNITION_MODE")
    old_model_enabled = os.environ.get("VEYRA_CORE_MODEL_ENABLED")
    if not args.allow_model:
        os.environ["VEYRA_COGNITION_MODE"] = "rules"
        os.environ["VEYRA_CORE_MODEL_ENABLED"] = "0"
    try:
        with TemporaryDirectory(prefix="veyra-dialogue-act-") as raw_tmp:
            state_store = WorldStateStore(Path(raw_tmp) / "state")
            _copy_config(Path(args.state_root), state_store)
            if args.allow_model:
                _apply_model_smoke_limits(state_store, timeout=args.model_timeout, retries=args.model_retries)
            else:
                _disable_model_for_smoke(state_store)
            return _run_cases(state_store=state_store, cases=selected, model_allowed=args.allow_model)
    finally:
        if old_env is None:
            os.environ.pop("VEYRA_COGNITION_MODE", None)
        else:
            os.environ["VEYRA_COGNITION_MODE"] = old_env
        if old_model_enabled is None:
            os.environ.pop("VEYRA_CORE_MODEL_ENABLED", None)
        else:
            os.environ["VEYRA_CORE_MODEL_ENABLED"] = old_model_enabled


def _copy_config(source_root: Path, state_store: WorldStateStore) -> None:
    source_store = WorldStateStore(source_root, read_only=True)
    for filename in ["agent_config.json", "user_world.json"]:
        payload = source_store.read_json(filename)
        if payload:
            payload.pop("_state_revision", None)
            state_store.write_json(filename, payload)


def _apply_model_smoke_limits(state_store: WorldStateStore, *, timeout: float, retries: int) -> None:
    config = state_store.read_json("agent_config.json")
    core_model = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
    if not core_model:
        return
    core_model["timeout"] = max(1.0, min(float(timeout), 10.0))
    core_model["retries"] = max(0, min(int(retries), 1))
    config["core_model"] = core_model
    state_store.write_json("agent_config.json", config)


def _disable_model_for_smoke(state_store: WorldStateStore) -> None:
    config = state_store.read_json("agent_config.json")
    core_model = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
    core_model["enabled"] = False
    config["core_model"] = core_model
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    for agent in agents.values():
        if isinstance(agent, dict):
            agent["use_model_for_core"] = False
    if agents:
        config["agents"] = agents
    state_store.write_json("agent_config.json", config)


def _run_cases(*, state_store: WorldStateStore, cases: list[dict[str, Any]], model_allowed: bool) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        case_root = state_store.root / "dialogue_cases" / f"{index:02d}_{case['id']}"
        case_store = WorldStateStore(case_root)
        for filename in ("agent_config.json", "user_world.json"):
            payload = state_store.read_json(filename)
            if payload:
                payload.pop("_state_revision", None)
                case_store.write_json(filename, payload)
        runtime = RuntimeEntity(state_store=case_store)
        commitment_core = CommitmentCore(case_store)
        loop = AwarenessLoop(state_store=case_store, runtime_entity=runtime, commitment_core=commitment_core)
        commitment_core.memory_bridge = loop.memory_bridge
        selected_agent = loop.agent_registry.selected_name()
        debug_agent = DebugAgentAdapter(executor=selected_agent)
        loop.agent_registry._adapters[selected_agent] = debug_agent
        loop.agent_adapter = debug_agent
        counted_probes: dict[str, CountingProbe] = {}
        for name, probe in list(loop.probes.items()):
            wrapped = FakeSearchProbe() if name == "search_probe" else probe
            counted = CountingProbe(name=name, wrapped=wrapped)
            loop.probes[name] = counted
            counted_probes[name] = counted
        normalizer = EventNormalizer()

        _reset_case_state(case_store, commitment_core)
        if case.get("seed_commitments"):
            _seed_commitments(commitment_core)
        if case.get("seed_previous"):
            _seed_previous_turn(case_store)
        if case.get("seed_generic_clarification"):
            _seed_generic_clarification_turn(case_store)
        if case.get("seed_empty_search"):
            _seed_empty_search_turn(case_store)

        before_status = _topic_statuses(case_store)
        before_agent_calls = len(debug_agent.sent_packets)
        probe_counts = {name: len(probe.calls) for name, probe in counted_probes.items()}
        model_trace_count = len(case_store.read_jsonl("core_model_trace.jsonl", limit=10000))
        event = normalizer.user_message(
            text=str(case["text"]),
            channel="smoke",
            user_id=USER_ID,
            session_id=SESSION_ID,
        )
        started_at = time.perf_counter()
        result = loop.handle_event(event)
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        after_agent_calls = len(debug_agent.sent_packets)
        after_status = _topic_statuses(case_store)
        called_probes = sorted(
            name
            for name, probe in counted_probes.items()
            if len(probe.calls) > probe_counts[name]
        )
        actual = _actual_contract(
            result=result.to_dict(),
            agent_called=after_agent_calls > before_agent_calls,
            called_probes=called_probes,
            model_traces=case_store.read_jsonl("core_model_trace.jsonl", limit=10000)[model_trace_count:],
            latency_ms=latency_ms,
            before_status=before_status,
            after_status=after_status,
        )
        expectation = dict(case.get("expect") or {})
        if not model_allowed:
            expectation.update(case.get("expect_model_disabled") or {})
        failures = _expectation_failures(actual, expectation)
        results.append(
            {
                "id": case["id"],
                "text": case["text"],
                "expect": case.get("expect") or {},
                "actual": actual,
                "failures": failures,
            }
        )

    failed = sum(1 for item in results if item["failures"])
    return {
        "schema": "veyra.dialogue_act_regression.v1",
        "model_allowed": model_allowed,
        "summary": {"total": len(results), "passed": len(results) - failed, "failed": failed},
        "cases": results,
    }


def _reset_case_state(state_store: WorldStateStore, commitment_core: CommitmentCore) -> None:
    state_store.write_json("user_commitments.json", {"commitments": [], "updated_at": utc_now_iso()})
    state_store.write_json("semantic_change_sets.json", {"change_sets": [], "updated_at": utc_now_iso()})
    state_store.write_json("external_world.json", {"watchlist": [], "updated_at": utc_now_iso()})
    task_state = state_store.read_json("task_state.json")
    task_state["conversation_slots"] = {}
    state_store.write_json("task_state.json", task_state)
    commitment_core.intent_planner.state_store = state_store


def _seed_commitments(commitment_core: CommitmentCore) -> None:
    commitment_core.create_commitment(
        {
            "kind": "external_digest",
            "status": "active",
            "title": "外部追踪：PyTorch",
            "user_id": USER_ID,
            "channel": "smoke",
            "session_id": SESSION_ID,
            "payload": {"topic": "PyTorch", "query": "PyTorch latest updates", "watchlist_id": "watch_dialogue_pytorch"},
            "schedule": {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"},
        }
    )
    commitment_core.create_commitment(
        {
            "kind": "external_digest",
            "status": "active",
            "title": "外部追踪：React",
            "user_id": USER_ID,
            "channel": "smoke",
            "session_id": SESSION_ID,
            "payload": {"topic": "React", "query": "React latest updates"},
            "schedule": {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"},
        }
    )


def _seed_previous_turn(state_store: WorldStateStore) -> None:
    channel_state = state_store.read_json("channel_state.json")
    inbox = channel_state.get("inbox") if isinstance(channel_state.get("inbox"), list) else []
    outbox = channel_state.get("outbox") if isinstance(channel_state.get("outbox"), list) else []
    inbox.append(
        {
            "message_id": "dialogue-act-prev-user",
            "event_id": "dialogue-act-prev-event",
            "channel": "smoke",
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "text": "Veyra 是什么",
            "metadata": {},
            "received_at": utc_now_iso(),
        }
    )
    outbox.append(
        {
            "channel": "smoke",
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "message": "Veyra 是一个 awareness + governance harness：先理解用户意图和世界状态，再决定直答、probe、skill、Agent 或阻断。",
            "metadata": {"route": "direct_answer", "status": "success"},
            "status": "queued",
            "created_at": utc_now_iso(),
            "delivery": "local_outbox",
        }
    )
    channel_state["inbox"] = inbox[-20:]
    channel_state["outbox"] = outbox[-20:]
    state_store.write_json("channel_state.json", channel_state)
    state_store.write_json(
        "task_state.json",
        {
            "conversation_slots": {
                SESSION_ID: {
                    "last_topic": "Veyra",
                    "last_intent": "information",
                    "updated_at": utc_now_iso(),
                }
            }
        },
    )


def _seed_generic_clarification_turn(state_store: WorldStateStore) -> None:
    channel_state = state_store.read_json("channel_state.json")
    inbox = channel_state.get("inbox") if isinstance(channel_state.get("inbox"), list) else []
    outbox = channel_state.get("outbox") if isinstance(channel_state.get("outbox"), list) else []
    inbox.append(
        {
            "message_id": "dialogue-act-search-user",
            "event_id": "dialogue-act-search-event",
            "channel": "smoke",
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "text": "帮我找一些2026秋招的公司的信息",
            "metadata": {},
            "received_at": utc_now_iso(),
        }
    )
    outbox.append(
        {
            "channel": "smoke",
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "message": "我需要你补一点具体上下文：你想问哪个对象、现象或决定？",
            "metadata": {"route": "direct_answer", "status": "success"},
            "status": "queued",
            "created_at": utc_now_iso(),
            "delivery": "local_outbox",
        }
    )
    channel_state["inbox"] = inbox[-20:]
    channel_state["outbox"] = outbox[-20:]
    state_store.write_json("channel_state.json", channel_state)


def _seed_empty_search_turn(state_store: WorldStateStore) -> None:
    state_store.write_json(
        "task_state.json",
        {
            "conversation_slots": {
                SESSION_ID: {
                    "last_intent": "information",
                    "last_topic": "一些2026秋招的公司的信息",
                    "last_search_query": "一些2026秋招的公司的信息",
                    "last_tool_result": {
                        "type": "search",
                        "status": "empty",
                        "query": "一些2026秋招的公司的信息",
                        "summary": "Search returned no parseable results for 一些2026秋招的公司的信息.",
                        "observed_at": utc_now_iso(),
                        "results": [],
                    },
                    "updated_at": utc_now_iso(),
                }
            }
        },
    )


def _topic_statuses(state_store: WorldStateStore) -> dict[str, str]:
    payload = state_store.read_json("user_commitments.json")
    items = payload.get("commitments") if isinstance(payload.get("commitments"), list) else []
    statuses: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        topic = str(item_payload.get("topic") or item_payload.get("location") or item.get("title") or "").strip()
        if topic:
            statuses[topic] = str(item.get("status") or "")
    return statuses


def _actual_contract(
    *,
    result: dict[str, Any],
    agent_called: bool,
    called_probes: list[str],
    model_traces: list[dict[str, Any]],
    latency_ms: int,
    before_status: dict[str, str],
    after_status: dict[str, str],
) -> dict[str, Any]:
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    decision = artifacts.get("decision") if isinstance(artifacts.get("decision"), dict) else {}
    assist = decision.get("model_assist") if isinstance(decision.get("model_assist"), dict) else {}
    frame = assist.get("semantic_frame") if isinstance(assist.get("semantic_frame"), dict) else {}
    policy = assist.get("semantic_policy") if isinstance(assist.get("semantic_policy"), dict) else {}
    acts = [item for item in (frame.get("acts") if isinstance(frame.get("acts"), list) else []) if isinstance(item, dict)]
    commitment = artifacts.get("commitment") if isinstance(artifacts.get("commitment"), dict) else {}
    semantic = commitment.get("semantic_intent") if isinstance(commitment.get("semantic_intent"), dict) else {}
    changeset = commitment.get("semantic_change_set") if isinstance(commitment.get("semantic_change_set"), dict) else {}
    probe = artifacts.get("probe_result") if isinstance(artifacts.get("probe_result"), dict) else {}
    early = artifacts.get("early_awareness") if isinstance(artifacts.get("early_awareness"), dict) else {}
    state_answer = early.get("state_answer") if isinstance(early.get("state_answer"), dict) else {}
    risk = result.get("risk_level") or decision.get("risk_level") or ""
    route = str(result.get("route") or "")
    requires_probe = bool(called_probes)
    requires_agent = bool(agent_called)
    requires_confirmation = (
        route in {"human_review", "block"}
        or str(result.get("status") or "") == "needs_confirmation"
        or bool(decision.get("requires_confirmation") or decision.get("needs_user_confirmation"))
        or (changeset.get("status") == "pending_confirmation")
    )
    targets = [
        str((act.get("target") or {}).get("value") or "")
        for act in acts
        if isinstance(act.get("target"), dict) and str((act.get("target") or {}).get("value") or "")
    ]
    return {
        "route": route,
        "status": result.get("status"),
        "semantic_frame_present": bool(frame),
        "resolver_status": frame.get("resolver_status"),
        "act_kinds": [str(act.get("kind") or "") for act in acts],
        "operations": [str(act.get("operation") or "") for act in acts],
        "targets": targets,
        "polarities": [str(act.get("polarity") or "") for act in acts],
        "modalities": [str(act.get("modality") or "") for act in acts],
        "policy_route": policy.get("preferred_route"),
        "selected_probe": policy.get("selected_probe"),
        "allowed_effects": sorted(str(item) for item in policy.get("allowed_effects", []) if isinstance(item, str)),
        "denied_effects": sorted(str(item) for item in policy.get("denied_effects", []) if isinstance(item, str)),
        "requires_clarification": bool(policy.get("requires_clarification")),
        "state_answer_read": bool(state_answer),
        "requires_state_read": bool(state_answer),
        "state_sources": [
            str(item)
            for item in (state_answer.get("sources") if isinstance(state_answer.get("sources"), list) else [])
        ],
        "called_probes": called_probes,
        "requires_probe": requires_probe,
        "requires_agent": requires_agent,
        "requires_confirmation": requires_confirmation,
        "risk_level": risk,
        "agent_called": agent_called,
        "model_trace_delta": len(model_traces),
        "latency_ms": latency_ms,
        "commitment_status": commitment.get("status"),
        "semantic_operation": semantic.get("operation"),
        "semantic_change_status": changeset.get("status"),
        "probe": probe.get("probe"),
        "before_status": before_status,
        "after_status": after_status,
        "response": str(result.get("response") or "")[:300],
        "followup_messages": [str(item)[:300] for item in result.get("followup_messages") or []],
    }


def _expectation_failures(actual: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key in (
        "route",
        "risk_level",
        "semantic_frame_present",
        "policy_route",
        "selected_probe",
        "requires_clarification",
        "state_answer_read",
    ):
        if key in expect and actual.get(key) != expect.get(key):
            failures.append(f"{key}: expected {expect.get(key)!r}, got {actual.get(key)!r}")
    if "route_any" in expect and actual.get("route") not in set(expect["route_any"]):
        failures.append(f"route: expected one of {expect['route_any']!r}, got {actual.get('route')!r}")
    for key in ("requires_state_read", "requires_probe", "requires_agent", "requires_confirmation"):
        if key in expect and bool(actual.get(key)) != bool(expect.get(key)):
            failures.append(f"{key}: expected {expect.get(key)!r}, got {actual.get(key)!r}")
    if expect.get("requires_probe_or_agent") and not (actual.get("requires_probe") or actual.get("requires_agent")):
        failures.append("expected probe or agent route")
    if expect.get("no_agent") and actual.get("agent_called"):
        failures.append("agent was called but expectation forbids agent")
    if expect.get("no_probe") and actual.get("called_probes"):
        failures.append("probe was called but expectation forbids probe")
    for key in ("act_kinds", "operations", "targets", "polarities", "modalities"):
        if key not in expect:
            continue
        missing = [item for item in expect[key] if item not in actual.get(key, [])]
        if missing:
            failures.append(f"{key}: missing {missing!r}; actual={actual.get(key)!r}")
    if "allowed_effects" in expect and sorted(expect["allowed_effects"]) != actual.get("allowed_effects"):
        failures.append(
            f"allowed_effects: expected {sorted(expect['allowed_effects'])!r}, got {actual.get('allowed_effects')!r}"
        )
    if "denied_effects_contains" in expect:
        missing = [item for item in expect["denied_effects_contains"] if item not in actual.get("denied_effects", [])]
        if missing:
            failures.append(f"denied_effects: missing {missing!r}; actual={actual.get('denied_effects')!r}")
    if "called_probes" in expect and sorted(expect["called_probes"]) != actual.get("called_probes"):
        failures.append(
            f"called_probes: expected {sorted(expect['called_probes'])!r}, got {actual.get('called_probes')!r}"
        )
    if "state_sources_contains" in expect:
        missing = [item for item in expect["state_sources_contains"] if item not in actual.get("state_sources", [])]
        if missing:
            failures.append(f"state_sources: missing {missing!r}; actual={actual.get('state_sources')!r}")
    for topic, status in (expect.get("must_preserve") or {}).items():
        before = actual.get("before_status", {}).get(topic)
        after = actual.get("after_status", {}).get(topic)
        if before != status or after != status:
            failures.append(f"{topic} should remain {status!r}; before={before!r}, after={after!r}")
    for topic, status in (expect.get("must_status") or {}).items():
        after = actual.get("after_status", {}).get(topic)
        if after != status:
            failures.append(f"{topic} status expected {status!r}, got {after!r}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
