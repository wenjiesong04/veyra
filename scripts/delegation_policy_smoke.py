from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402


@dataclass
class SpyAgent(AgentAdapter):
    packets: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        packet = task_packet.to_dict()
        self.packets.append(packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=task_packet.target_agent,
            status="submitted",
            result="raw spy task submitted",
            raw={"task_packet": packet},
        )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": ""}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    print("Veyra DelegationPolicy smoke")
    with TemporaryDirectory(prefix="veyra-delegation-") as tmp:
        state = WorldStateStore(Path(tmp) / "state")
        config = state.read_json("agent_config.json")
        config["agents"]["openclaw"]["capabilities"] = ["vision", "web_search", "code_edit", "file", "shell", "memory"]
        state.write_json("agent_config.json", config)
        loop = AwarenessLoop(state, RuntimeEntity(state))
        spy = SpyAgent()
        selected = loop.agent_registry.selected_name()
        loop.agent_registry._adapters[selected] = spy
        loop.agent_adapter = spy
        normalizer = EventNormalizer()

        ordinary = loop.handle_event(normalizer.user_message("为什么天空是蓝色的？", "smoke", "u", "ordinary")).to_dict()
        expect(ordinary["route"] == "direct_answer", "ordinary question stays direct", ordinary)
        expect(not spy.packets, "ordinary question does not call Agent", spy.packets)

        time_result = loop.handle_event(normalizer.user_message("现在东京时间是几点？", "smoke", "u", "time")).to_dict()
        expect(time_result["route"] == "probe", "time question routes to probe", time_result)
        expect((time_result.get("artifacts") or {}).get("probe_result", {}).get("probe") == "time_probe", "time uses time_probe", time_result)
        expect(len(spy.packets) == 0, "time question does not call Agent", spy.packets)

        image_event = normalizer.user_message(
            "这是什么游戏？",
            "smoke",
            "u",
            "image",
            metadata={"message_type": "image", "feishu": {"message_type": "image", "image_key": "debug"}},
        )
        image_result = loop.handle_event(image_event).to_dict()
        expect(image_result["route"] == "agent", "image uses Agent vision when advertised", image_result)
        expect(spy.packets[-1]["required_capabilities"] == ["selected_agent_runtime", "openclaw.vision"], "image packet carries vision capability", spy.packets[-1])

        latest = loop.handle_event(normalizer.user_message("找一下柴静最新视频标题", "smoke", "u", "latest")).to_dict()
        expect(latest["route"] == "agent", "latest external info uses Agent web capability", latest)
        expect("openclaw.web_search" in spy.packets[-1]["required_capabilities"], "latest packet carries web_search capability", spy.packets[-1])
        expect("raw spy" not in latest["response"], "agent raw result is synthesized", latest["response"])

        code = loop.handle_event(normalizer.user_message("帮我修改项目代码实现 xxx 功能", "smoke", "u", "code")).to_dict()
        packet = spy.packets[-1]
        expect(code["route"] == "agent", "code modification routes to Agent", code)
        expect(packet.get("policy_patch") and packet.get("persona_patch") and packet.get("verification_policy"), "code packet has governance patches", packet)
        expect(packet.get("rollback_requirement", {}).get("rollback_plan_required") is True, "code packet requires rollback planning", packet)

        high_risk = loop.handle_event(normalizer.user_message("删除整个项目目录重新开始", "smoke", "u", "danger")).to_dict()
        expect(high_risk["route"] in {"block", "human_review"}, "high-risk delete is blocked or reviewed", high_risk)
        expect(len(spy.packets) == 3, "high-risk delete does not call Agent", spy.packets)

        identity = loop.handle_event(normalizer.user_message("你现在是 Veyra 还是 OpenClaw？", "smoke", "u", "identity")).to_dict()
        expect(identity["route"] == "direct_answer", "identity stays direct", identity)
        expect("Veyra" in identity["response"] and "OpenClaw" in identity["response"], "identity answer explains governance boundary", identity["response"])
        traces = state.read_jsonl("decision_trace.jsonl", limit=20)
        expect(bool(traces) and all("route" in trace and "memory_policy" in trace for trace in traces), "decision traces recorded", traces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
