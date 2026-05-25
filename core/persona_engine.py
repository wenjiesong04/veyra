from __future__ import annotations

from typing import Any

from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


class PersonaEngine:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store

    def patch_for(
        self,
        text: str,
        risk_level: RiskLevel,
        *,
        channel: str = "api",
        route: str | None = None,
        target_agent: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        modes = ["Minimalist"]
        lowered = text.lower()
        if any(marker in lowered for marker in ["端口", "进程", "部署", "服务", "openclaw", "hermes", "飞书", "feishu", "discord"]):
            modes.append("Operator")
        if any(marker in lowered for marker in ["代码", "实现", "修复", "重构", "开发"]):
            modes.append("Engineer")
        if any(marker in lowered for marker in ["解释", "为什么", "教学", "说明", "teach"]):
            modes.append("Teacher")
        if any(marker in lowered for marker in ["计划", "拆解", "架构", "路线", "多步骤"]):
            modes.append("Planner")
        if channel in {"feishu", "discord", "slack"}:
            modes.append("Steward")
        if risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            modes.append("Guardian")
        if route == "agent" or target_agent:
            modes.append("Operator")
        modes = list(dict.fromkeys(modes))
        token_budget = self._token_budget(channel=channel, risk_level=risk_level, route=route)
        response_style = self._response_style(channel)
        tool_policy = self._tool_policy(risk_level)
        return {
            "mode": modes,
            "channel": channel,
            "route": route,
            "target_agent": target_agent,
            "risk_level": risk_level.value,
            "response_style": response_style,
            "token_budget": token_budget,
            "tool_policy": tool_policy,
            "agent_policy": {
                "selected_agent": target_agent,
                "dispatch": "allowed_with_evidence" if risk_level in {RiskLevel.R0, RiskLevel.R1, RiskLevel.R2} else "review_required",
                "requires_tool_proxy_evidence": True,
            },
            "memory_policy": {
                "read": "focus_ranked",
                "write": "filtered_after_verification",
                "channel_context": channel,
            },
            "review_policy": {
                "r3_r4": "ask_user",
                "r5": "block",
            },
            "behavior": [
                "prefer read-only diagnosis first",
                "ask before destructive or hard-to-revert actions",
                "return evidence for execution claims",
                f"format for {channel} channel using {response_style}",
            ],
            "decision_binding": decision or {},
        }

    def record_binding(self, event: VeyraEvent, persona_patch: dict[str, Any]) -> dict[str, Any]:
        if not self.state_store:
            return {"status": "skipped", "reason": "no state_store"}
        state = self.state_store.read_json("persona_state.json") or {"active_modes": [], "history": []}
        binding = {
            "event_id": event.event_id,
            "channel": event.source.channel,
            "session_id": event.source.session_id,
            "active_modes": persona_patch.get("mode", []),
            "route": persona_patch.get("route"),
            "target_agent": persona_patch.get("target_agent"),
            "risk_level": persona_patch.get("risk_level"),
            "response_style": persona_patch.get("response_style"),
            "token_budget": persona_patch.get("token_budget"),
            "updated_at": utc_now_iso(),
        }
        history = state.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            state["history"] = history
        history.append(binding)
        state["history"] = history[-100:]
        state["active_modes"] = persona_patch.get("mode", [])
        state["last_binding"] = binding
        state["updated_at"] = utc_now_iso()
        self.state_store.write_json("persona_state.json", state)
        return {"status": "success", "binding": binding}

    def _response_style(self, channel: str) -> str:
        if channel in {"feishu", "discord", "slack"}:
            return "compact_chat_with_clear_next_action"
        if channel == "console":
            return "operator_console_structured"
        return "direct_structured"

    def _token_budget(self, *, channel: str, risk_level: RiskLevel, route: str | None) -> dict[str, int]:
        base = 500 if channel in {"feishu", "discord", "slack"} else 900
        if route == "agent":
            base += 400
        if risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            base += 250
        return {"response_tokens": base, "agent_context_tokens": max(base * 2, 1200)}

    def _tool_policy(self, risk_level: RiskLevel) -> dict[str, str]:
        if risk_level == RiskLevel.R5:
            return {"default": "block", "rationale": "forbidden risk"}
        if risk_level in {RiskLevel.R3, RiskLevel.R4}:
            return {"default": "review_required", "rationale": "medium/high impact"}
        return {"default": "read_only_first", "rationale": "bounded low-risk handling"}
