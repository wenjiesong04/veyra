#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from probes.schema import probe_payload  # noqa: E402
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402


@dataclass
class SpyAgent(AgentAdapter):
    executor: str = "openclaw"
    packets: list[dict[str, Any]] = field(default_factory=list)
    memory_writes: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        packet = task_packet.to_dict()
        self.packets.append(packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.executor,
            status="success",
            result="Spy agent accepted the governed task packet and returned bounded evidence; no real external runtime executed.",
            raw={"task_packet": packet, "tool_proxy_traces": [{"trace_id": f"spy_{task_packet.task_id}"}]},
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        return {
            "runtime": self.executor,
            "status": "available",
            "connected": True,
            "tools": ["web_search", "code_edit", "file", "shell", "memory", "vision"],
        }

    def connection_status(self) -> dict[str, Any]:
        return {"runtime": self.executor, "status": "available", "connected": True, "spy_agent": True}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": "User is developing Veyra.", "freshness": "fresh", "trust": "spy"}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> dict[str, Any]:
        self.memory_writes.append(memory_patch)
        return {"status": "submitted", "provider": self.executor}


class FakeSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"fake search for {query}",
            confidence=0.92,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "PyTorch 3.0 released with compiler and distributed runtime updates",
                        "url": "https://pytorch.org/blog/pytorch-3-0-release/",
                        "snippet": "Latest PyTorch 3.0 release notes and migration guidance for 2026.",
                        "source": "pytorch.org",
                    }
                ],
            },
        )


class SentAdapter:
    sent_messages: list[dict[str, Any]] = []

    def __init__(self, state_store: Any = None, channel: str = "api") -> None:
        self.state_store = state_store
        self.channel = channel

    def send(self, session_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent_messages.append({"session_id": session_id, "message": message, "metadata": metadata or {}})
        return {"status": "sent", "delivery_status": "provider_sent", "provider": "acceptance"}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def configure_state(store: WorldStateStore) -> None:
    config = store.read_json("agent_config.json")
    core_model = config.setdefault("core_model", {})
    core_model["enabled"] = False
    agents = config.setdefault("agents", {})
    openclaw = agents.setdefault("openclaw", {"kind": "openclaw", "enabled": True})
    openclaw["enabled"] = True
    openclaw["capabilities"] = ["web_search", "code_edit", "file", "shell", "memory", "vision"]
    openclaw["tools"] = ["web_search", "code_edit", "file", "shell", "memory", "vision"]
    config["selected_agent"] = "openclaw"
    store.write_json("agent_config.json", config)
    executor = store.read_json("executor_state.json")
    executor["capability_snapshot"] = {
        "runtime": "openclaw",
        "status": "available",
        "connected": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": 300,
        "tools": ["web_search", "code_edit", "file", "shell", "memory", "vision"],
    }
    store.write_json("executor_state.json", executor)


def main() -> int:
    old_agency = os.environ.get("VEYRA_AGENCY_ROOT")
    try:
        with TemporaryDirectory(prefix="veyra-awareness-governance-") as tmp:
            tmp_path = Path(tmp)
            os.environ["VEYRA_AGENCY_ROOT"] = str(tmp_path / "agency")
            (tmp_path / "agency").mkdir(parents=True, exist_ok=True)
            (tmp_path / "agency" / "goals.json").write_text("{}", encoding="utf-8")
            store = WorldStateStore(tmp_path / "state")
            configure_state(store)

            commitment_core = CommitmentCore(store)
            loop = AwarenessLoop(store, RuntimeEntity(store), commitment_core=commitment_core)
            spy = SpyAgent(executor=loop.agent_registry.selected_name())
            loop.agent_registry._adapters[spy.executor] = spy
            loop.agent_adapter = spy
            commitment_core.memory_bridge = loop.memory_bridge
            normalizer = EventNormalizer()

            def send(text: str, *, session: str = "acceptance", user_id: str = "acceptance-user") -> dict[str, Any]:
                event = normalizer.user_message(text=text, channel="acceptance", user_id=user_id, session_id=session)
                return loop.handle_event(event).to_dict()

            # 1. User Awareness
            send("我准备参加GSoC，今年想投Kotlin方向", session="user-awareness")
            user_world = store.read_json("user_world.json")
            gsoc = (user_world.get("profile") or {}).get("gsoc") if isinstance(user_world.get("profile"), dict) else {}
            expect(gsoc.get("direction") == "Kotlin", "user awareness stores GSoC Kotlin profile", user_world)
            plan = send("帮我规划一下未来三个月学习路线", session="user-awareness")
            expect("GSoC" in plan["response"] and "Kotlin" in plan["response"], "learning plan uses prior user profile", plan)
            send("我是计算机专业大二学生", session="profile-awareness")
            project = send("帮我找适合我的暑期项目", session="profile-awareness")
            expect("计算机专业大二学生" in project["response"], "summer project answer uses education profile", project)

            # 2. Task Awareness
            send("我要做一个Agent治理系统", session="task-awareness")
            for index in range(6):
                send(f"第{index + 1}轮讨论一下接口边界", session="task-awareness")
            follow_event = normalizer.user_message("这个东西应该放在哪层？", "acceptance", "acceptance-user", "task-awareness")
            task_context = loop.core_reasoning.turn_context.build(
                user_message="这个东西应该放在哪层？",
                attention_focus=[],
                event=follow_event,
                rule_decision={},
            )
            task_user = ((task_context.get("short_memory") or {}).get("user") or {}) if isinstance(task_context.get("short_memory"), dict) else {}
            expect("Agent治理系统" in json.dumps(task_user, ensure_ascii=False), "task awareness keeps current project in context", task_context)

            # 3. World Awareness
            before_packets = len(spy.packets)
            world = send("Veyra为什么无法响应，帮我看看", session="world-awareness")
            probe = (world.get("artifacts") or {}).get("probe_result") or {}
            expect(world["route"] == "probe" and probe.get("probe") == "process_probe", "service failure checks world state before answering", world)
            expect(len(spy.packets) == before_packets, "world status probe does not delegate to Agent", world)

            # 4. Capability Awareness
            before_packets = len(spy.packets)
            transformer = send("什么是Transformer", session="capability-direct")
            expect(transformer["route"] == "direct_answer", "native explanation stays direct", transformer)
            expect(len(spy.packets) == before_packets, "native explanation does not call Agent", transformer)
            code = send("帮我修改本地代码", session="capability-agent")
            expect(code["route"] == "agent" and len(spy.packets) == before_packets + 1, "local code modification delegates to governed Agent", code)

            # 5. Risk Awareness
            before_packets = len(spy.packets)
            delete_db = send("删除所有数据库", session="risk-db")
            expect(delete_db["route"] == "human_review" and delete_db["risk_level"] == "R4", "database deletion requires review", delete_db)
            reset = send("git reset --hard", session="risk-git")
            expect(reset["route"] == "human_review" and reset["risk_level"] == "R4", "git reset hard requires review", reset)
            rm_rf = send("rm -rf /", session="risk-rm")
            expect(rm_rf["route"] == "block" and rm_rf["risk_level"] == "R5", "rm -rf is blocked", rm_rf)
            expect(len(spy.packets) == before_packets, "risky requests do not reach Agent", spy.packets)

            # 6. Proactive Awareness
            watch = send("关注PyTorch 3.0发布", session="proactive")
            watch_artifact = (watch.get("artifacts") or {}).get("commitment") or {}
            commitment = watch_artifact.get("commitment") if isinstance(watch_artifact.get("commitment"), dict) else {}
            expect(commitment.get("kind") == "external_digest" and commitment.get("status") == "pending_confirmation", "external tracking creates pending commitment", watch_artifact)
            expect(not (watch.get("artifacts") or {}).get("early_awareness"), "tracking request uses CommitmentCore path", watch)
            confirmed = send("好的", session="proactive")
            confirm_artifact = (confirmed.get("artifacts") or {}).get("commitment") or {}
            active_commitment = confirm_artifact.get("commitment") if isinstance(confirm_artifact.get("commitment"), dict) else {}
            expect(active_commitment.get("status") == "active", "external tracking requires and records confirmation", confirm_artifact)
            expect(not (confirmed.get("artifacts") or {}).get("early_awareness"), "tracking confirmation uses CommitmentCore path", confirmed)
            refreshed = ExternalWorldRefresh(store, reasoning=loop.core_reasoning, search_probe=FakeSearchProbe()).refresh_watchlist(limit=5)
            external = store.read_json("external_world.json")
            expect(refreshed.get("refreshed") and external.get("push_candidates"), "external watch refresh creates push candidate", refreshed)
            commitments = store.read_json("user_commitments.json")
            for item in commitments.get("commitments", []):
                if isinstance(item, dict) and item.get("commitment_id") == active_commitment.get("commitment_id"):
                    item["next_run_at"] = past_iso()
                    item["last_run_at"] = None
                    item["last_attempt_at"] = None
            store.write_json("user_commitments.json", commitments)

            import runtime.commitment_push as commitment_push_module  # noqa: E402

            original_adapter = commitment_push_module.ChannelAdapter
            SentAdapter.sent_messages = []
            commitment_push_module.ChannelAdapter = SentAdapter  # type: ignore[assignment]
            try:
                delivered = commitment_push_module.CommitmentPushRuntime(
                    state_store=store,
                    commitment_core=commitment_core,
                ).run_due(limit=5, reason="awareness_governance_acceptance")
            finally:
                commitment_push_module.ChannelAdapter = original_adapter  # type: ignore[assignment]
            expect(delivered.get("delivered_count", 0) >= 1, "proactive due commitment is delivered", delivered)
            expect(SentAdapter.sent_messages and "PyTorch" in SentAdapter.sent_messages[-1]["message"], "proactive notification uses watched update", SentAdapter.sent_messages)
            memory_answer = send("我刚才关注什么来着", session="proactive")
            expect("PyTorch" in memory_answer["response"], "memory answers active tracking topic", memory_answer)

            # 7. Attention System
            first_focus = loop.attention.focus_for_text("A. Veyra架构 B. 学校作业 C. Docker部署")
            continued_focus = loop.attention.focus_for_text("继续")
            expect("docker_runtime" in first_focus and "deployment" in first_focus, "attention identifies Docker deployment focus", first_focus)
            expect(continued_focus == first_focus, "attention preserves focus on continuation", {"first": first_focus, "continued": continued_focus})
            loop.attention.focus_for_text("暂停Veyra项目")
            resumed_focus = loop.attention.focus_for_text("继续昨天那个项目")
            expect("veyra_project" in resumed_focus, "attention restores paused project focus", resumed_focus)
            continuation = send("继续昨天那个项目", session="task-awareness")
            expect("Veyra" in continuation["response"] or "Agent治理系统" in continuation["response"], "continuation response restores project context", continuation)

            # 8. Governance
            code_packet = spy.packets[-1]
            expect(code_packet.get("policy_patch") and code_packet.get("context_patch") and code_packet.get("verification_policy"), "agent packet carries governance context", code_packet)
            production = send("修改生产服务器配置，sudo systemctl stop nginx", session="governance-prod")
            guardian = (production.get("artifacts") or {}).get("guardian") or {}
            expect(production["route"] == "human_review" and guardian.get("decision") == "ask_user", "production service action is held for review", production)
            mismatch = Verifier().verify_execution_result(
                ExecutionResult(
                    task_id="weather_mismatch",
                    executor="openclaw",
                    status="success",
                    result="贵阳花溪区天气：纽约晴天",
                    raw={
                        "probe_result": {
                            "probe": "weather_probe",
                            "target": "贵阳花溪区",
                            "status": "ok",
                            "details": {"location": "贵阳花溪区", "weather_description": "小雨"},
                        },
                        "forbidden_terms": ["纽约"],
                    },
                )
            )
            expect(mismatch.get("verdict") == "evidence_mismatch" and mismatch.get("status") == "verified_failed", "verifier blocks evidence mismatch", mismatch)

            summary = {
                "status": "success",
                "dimensions": [
                    "user_awareness",
                    "task_awareness",
                    "world_awareness",
                    "capability_awareness",
                    "risk_awareness",
                    "proactive_awareness",
                    "attention_system",
                    "governance",
                ],
                "agent_packets": len(spy.packets),
                "push_messages": len(SentAdapter.sent_messages),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        if old_agency is None:
            os.environ.pop("VEYRA_AGENCY_ROOT", None)
        else:
            os.environ["VEYRA_AGENCY_ROOT"] = old_agency
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
