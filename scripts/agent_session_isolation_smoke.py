#!/usr/bin/env python3
"""Smoke test for agent session isolation and context scope filtering (P1).

Runs fully offline with a temp state store and a fake agent adapter. Verifies:
- Independent user requests in the same dialogue session each get a fresh, isolated
  agent_execution_session_id (never the dialogue/feishu session id, never a shared key).
- An explicit "continue previous task" request reuses the previous agent session.
- Gateway diagnostics / workspace-file-fallback memory dumps and unrelated memory are
  filtered out of the agent context patch (routed to audit only) and the serialized
  context stays bounded.
- The OpenClaw adapter sends the per-task sessionKey rather than the fixed "main" key.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import Decision, EventSource, EventType, Route, VeyraEvent, VeyraTaskPacket  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


DIRTY_WORKSPACE_MARKDOWN = (
    "# ~/.openclaw/workspace/memory/2026-06-03-veyra.md\n"
    "深度学习学习目标：下一步学习卷积网络\n"
    "openclaw_workspace_memory_roundtrip probe topic: roundtrip_probe\n" * 50
)


class FakeOpenClawAdapter(AgentAdapter):
    """Captures the task packets it receives and returns a clean success."""

    def __init__(self) -> None:
        self.sent: list[VeyraTaskPacket] = []

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent.append(task_packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor="openclaw",
            status="success",
            result="标题：示例视频 来源：official_youtube_channel 发布时间：2026-06-03",
            raw={"result": "ok"},
        )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        # Simulate gateway memory RPC being unavailable -> dirty workspace fallback dump.
        return {
            "session_id": session_id,
            "summary": DIRTY_WORKSPACE_MARKDOWN,
            "runtime": "openclaw",
            "status": "workspace_file_fallback",
            "freshness": "fresh",
            "trust": "workspace_file_fallback",
            "source": "openclaw_workspace_files",
            "sync_status": "pending_gateway_support",
            "files": ["~/.openclaw/workspace/memory/2026-06-03-veyra.md"],
            "gateway_error": {"status": "unknown_method", "message": "unknown method memory.summary"},
        }


def build_loop(tmp: Path) -> tuple[AwarenessLoop, FakeOpenClawAdapter]:
    store = WorldStateStore(root=str(tmp))
    runtime_entity = RuntimeEntity(state_store=store)
    commitment_core = CommitmentCore(store)
    loop = AwarenessLoop(state_store=store, runtime_entity=runtime_entity, commitment_core=commitment_core)
    commitment_core.memory_bridge = loop.memory_bridge

    fake = FakeOpenClawAdapter()
    loop.agent_registry.selected = lambda: fake  # type: ignore[assignment]
    loop.agent_registry.selected_name = lambda: "openclaw"  # type: ignore[assignment]

    # Force the AGENT route so we exercise the agent path deterministically offline.
    def forced_decide(
        text: str,
        attention_focus: list[str],
        event: VeyraEvent | None = None,
        **_kwargs: Any,
    ) -> Decision:
        return Decision(
            route=Route.AGENT,
            risk_level=RiskLevel.R0,
            reason="forced_agent_route_for_smoke",
            target_agent="openclaw",
            intent="information",
            needs_agent=True,
            required_capabilities=["openclaw.web_search"],
        )

    loop.decision_core.decide = forced_decide  # type: ignore[assignment]
    # Keep controller from rerouting away from AGENT.
    original_prepare = loop.controller.prepare

    def prepare(decision: Decision):
        plan_decision, plan = original_prepare(decision)
        plan_decision.route = Route.AGENT
        return plan_decision, plan

    loop.controller.prepare = prepare  # type: ignore[assignment]
    return loop, fake


def make_event(text: str, *, session_id: str, idx: int) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="feishu", user_id="ou_test", session_id=session_id),
        payload={"text": text, "message_id": f"iso-{idx}"},
    )


def run() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        loop, fake = build_loop(tmp)
        dialogue_session = "feishu:ou_test:oc_chat"

        # Use implementation/analysis tasks here so deterministic read-only probes do not
        # intercept the requests before the forced AGENT decision used by this smoke.
        queries = [
            "重构 Veyra 仓库的状态写入模块并给出补丁",
            "为 OpenClaw gateway 实现连接重试机制",
            "总结 awareness_loop 在本项目里的职责",
        ]
        for idx, text in enumerate(queries):
            loop.handle_event(make_event(text, session_id=dialogue_session, idx=idx))

        expect(len(fake.sent) == 3, "three agent tasks were dispatched", len(fake.sent))

        session_ids = [pkt.agent_execution_session_id for pkt in fake.sent]
        expect(len(set(session_ids)) == 3, "independent queries get distinct agent sessions", session_ids)
        expect(
            all(sid != dialogue_session for sid in session_ids),
            "agent session id never equals the dialogue/feishu session id",
            session_ids,
        )
        expect(
            all(sid.startswith("agent-exec:") for sid in session_ids),
            "agent sessions use the isolated agent-exec namespace",
            session_ids,
        )
        expect(
            all(pkt.agent_session_policy == "ephemeral_per_task" for pkt in fake.sent),
            "independent queries use ephemeral_per_task policy",
            [pkt.agent_session_policy for pkt in fake.sent],
        )

        # Context scope: the third agent packet must be clean (no workspace fallback / gateway_error dumps).
        third = fake.sent[2]
        serialized = __import__("json").dumps(third.context_patch, ensure_ascii=False, sort_keys=True)
        expect("agent_memory_summary" not in third.context_patch, "workspace fallback memory omitted from packet")
        expect("gateway_error" not in serialized, "gateway_error not present in agent context")
        expect("memory_roundtrip" not in serialized, "memory_roundtrip not present in agent context")
        expect("深度学习" not in serialized, "unrelated learning-goal markdown not present in agent context")
        scope = third.context_patch.get("context_scope") or {}
        expect(bool(scope), "context_scope report attached", scope)
        expect("agent_memory_summary" in scope.get("omitted_sections", []), "fallback omission recorded in scope", scope)

        # Explicit continuation should reuse the previous agent session.
        loop.handle_event(make_event("基于刚才对 awareness_loop 的结果，继续给出重构建议", session_id=dialogue_session, idx=99))
        expect(len(fake.sent) == 4, "continuation dispatched a task", len(fake.sent))
        cont = fake.sent[3]
        expect(
            cont.agent_session_policy == "continue_previous_task",
            "explicit continue uses continue_previous_task policy",
            cont.agent_session_policy,
        )
        expect(
            cont.agent_execution_session_id == session_ids[2],
            "continuation reuses the most recent agent session",
            (cont.agent_execution_session_id, session_ids[2]),
        )

        # L1 YouTube lookup must not dispatch a heavy agent packet.
        loop.handle_event(make_event("帮我找一下王志安在 YouTube 最新一期视频标题", session_id=dialogue_session, idx=300))
        expect(len(fake.sent) == 4, "YouTube title lookup uses L1 compact path, not agent", len(fake.sent))

        # A different dialogue session must not collide with this one.
        loop.handle_event(make_event("为 Hermes runtime 实现新的会话隔离模块", session_id="feishu:ou_other:oc_chat2", idx=200))
        other = fake.sent[4]
        expect(
            other.agent_execution_session_id not in session_ids,
            "different dialogue session gets its own isolated agent session",
            other.agent_execution_session_id,
        )

    _check_openclaw_adapter_uses_packet_session_key()
    print("\nALL agent session isolation checks passed.")


def _check_openclaw_adapter_uses_packet_session_key() -> None:
    from interface.openclaw_adapter import OpenClawAdapter

    adapter = OpenClawAdapter(base_url="http://127.0.0.1:18789")
    captured: dict[str, Any] = {}

    def fake_send_chat(message: str, *, session_key: str | None = None) -> dict[str, Any]:
        captured["session_key"] = session_key
        return {"final_event": {"state": "final", "message": "ok"}, "run_id": "run_test"}

    adapter._send_chat = fake_send_chat  # type: ignore[assignment]
    packet = VeyraTaskPacket(
        task_id="task_xyz",
        target_agent="openclaw",
        session_id="feishu:ou_test:oc_chat",
        user_message="hi",
        context_patch={"user_goal": "hi"},
        persona_patch={},
        policy_patch={"risk_level": "R0"},
        agent_execution_session_id="agent-exec:task_xyz",
    )
    adapter.send_task(packet)
    expect(captured.get("session_key") == "agent-exec:task_xyz", "OpenClaw send uses per-task sessionKey", captured)
    expect(captured.get("session_key") != "main", "OpenClaw send does not fall back to fixed main key", captured)


if __name__ == "__main__":
    run()
