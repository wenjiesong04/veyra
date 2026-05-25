from __future__ import annotations

from typing import Any

from core.definitions import RiskLevel, classify_text_risk
from core.reasoning_core import CoreReasoning, safe_model_risk
from core.world_state import WorldStateStore
from interface.event_schema import Decision, Route, VeyraEvent


class DecisionCore:
    def __init__(self, state_store: WorldStateStore | None = None, reasoning: CoreReasoning | None = None) -> None:
        self.reasoning = reasoning or (CoreReasoning(state_store) if state_store else None)

    def decide(self, text: str, attention_focus: list[str], event: VeyraEvent | None = None) -> Decision:
        base = self._rule_decide(text, attention_focus)
        return self._apply_model_assist(text, attention_focus, base, event=event)

    def _rule_decide(self, text: str, attention_focus: list[str]) -> Decision:
        lowered = text.lower()
        risk = self._risk_for_text(lowered)
        intent, intent_signals = self._intent_for_text(lowered)
        complexity, complexity_signals = self._complexity_for_text(lowered, attention_focus)
        signals = intent_signals + complexity_signals + self._risk_signals(risk)
        if self._is_rollback_request(lowered):
            return self._decision(
                route=Route.ROLLBACK,
                risk=RiskLevel.R4,
                reason="snapshot rollback requires guarded confirmation",
                intent="action",
                complexity="moderate",
                capability="rollback_audit",
                signals=signals + ["route:rollback"],
                requires_confirmation=True,
                constraints=["restore only an existing snapshot", "record rollback trace", "verify restored checksum"],
            )
        if risk == RiskLevel.R5:
            return self._decision(
                route=Route.BLOCK,
                risk=risk,
                reason="destructive or forbidden action detected",
                intent=intent,
                complexity=complexity,
                capability="guardian",
                signals=signals,
                constraints=["block unsafe action", "require safer alternative"],
            )
        if risk in {RiskLevel.R3, RiskLevel.R4}:
            return self._decision(
                route=Route.HUMAN_REVIEW,
                risk=risk,
                reason="medium/high risk action requires review",
                intent=intent,
                complexity=complexity,
                capability="human_review",
                signals=signals,
                requires_confirmation=True,
                constraints=["explain impact", "check rollback path", "wait for explicit approval"],
            )
        skill = self._skill_for_text(lowered)
        if skill:
            skill_risk = RiskLevel.R1 if skill != "safe_git_commit" else RiskLevel.R2
            return self._decision(
                route=Route.SKILL,
                risk=skill_risk,
                reason=f"built-in skill selected: {skill}",
                intent=intent,
                complexity=complexity,
                capability="skill",
                signals=signals + [f"skill:{skill}"],
                selected_probe=skill,
                constraints=["run fixed workflow", "record evidence"],
            )
        probe = self._probe_for_text(lowered)
        if probe:
            return self._decision(
                route=Route.PROBE,
                risk=RiskLevel.R1,
                reason=f"read-only probe selected: {probe}",
                intent=intent,
                complexity=complexity,
                capability="probe",
                signals=signals + [f"probe:{probe}"],
                selected_probe=probe,
                constraints=["read-only", "refresh state cache"],
            )
        if self._needs_agent(lowered, attention_focus, complexity):
            return self._decision(
                route=Route.AGENT,
                risk=risk,
                reason="complex task requires selected agent runtime",
                intent=intent,
                complexity=complexity,
                capability="selected_agent_runtime",
                signals=signals + ["agent_required"],
                constraints=["inject context patch", "enforce policy patch", "verify result"],
            )
        if risk == RiskLevel.R2 and intent == "action":
            return self._decision(
                route=Route.AGENT,
                risk=risk,
                reason="low-risk write action requires governed execution",
                intent=intent,
                complexity=complexity,
                capability="selected_agent_runtime",
                signals=signals + ["agent_required", "write_action"],
                constraints=["limit write scope", "record diff or snapshot", "verify result"],
            )
        return self._decision(
            route=Route.DIRECT_ANSWER,
            risk=RiskLevel.R0 if risk == RiskLevel.R0 else risk,
            reason="low complexity informational request",
            intent=intent,
            complexity=complexity,
            capability="native_answer",
            signals=signals,
            constraints=["no tool execution"],
        )

    def _apply_model_assist(self, text: str, attention_focus: list[str], base: Decision, event: VeyraEvent | None = None) -> Decision:
        if not self.reasoning:
            return base
        assist = self.reasoning.decision_assist(text=text, attention_focus=attention_focus, rule_decision=base.to_dict(), event=event)
        if assist.get("status") != "model_assisted":
            return base

        model_risk = safe_model_risk(assist.get("risk_level"), base.risk_level)
        risk = self._max_risk(base.risk_level, model_risk)
        candidate_route = self._route_from_model(assist.get("route"), base.route)
        policy_signals: list[str] = []
        if base.route == Route.PROBE and candidate_route == Route.DIRECT_ANSWER and base.selected_probe:
            candidate_route = Route.PROBE
            policy_signals.append("policy:required_probe_preserved")
        if candidate_route == Route.AGENT and not self._agent_allowed_by_policy(text, base, assist):
            candidate_route = Route.DIRECT_ANSWER
            policy_signals.append("policy:agent_route_rejected")
        if risk == RiskLevel.R5:
            route = Route.BLOCK
        elif risk in {RiskLevel.R3, RiskLevel.R4}:
            route = Route.HUMAN_REVIEW
        elif candidate_route == Route.BLOCK:
            route = Route.HUMAN_REVIEW
            risk = self._max_risk(risk, RiskLevel.R3)
        elif candidate_route == Route.HUMAN_REVIEW:
            route = Route.HUMAN_REVIEW
            risk = self._max_risk(risk, RiskLevel.R3)
        elif candidate_route == Route.ROLLBACK:
            route = Route.ROLLBACK
            risk = self._max_risk(risk, RiskLevel.R4)
        else:
            route = candidate_route

        selected_probe = base.selected_probe
        if route == Route.PROBE:
            selected_probe = self._selected_probe_from_model(assist, base.selected_probe)
        elif route == Route.SKILL:
            selected_probe = self._selected_skill_from_model(assist, base.selected_probe)

        reason = str(assist.get("reason") or assist.get("rationale") or "model-assisted core reasoning")
        signals = self._dedupe(base.signals + self._string_list(assist.get("signals")) + policy_signals + ["model:decision"])
        constraints = self._dedupe(base.constraints + self._string_list(assist.get("constraints")) + ["guardian remains final execution boundary"])
        model_assist = {
            "status": "model_assisted",
            "reason": reason,
            "route": route.value,
            "risk_level": risk.value,
            "confidence": assist.get("confidence"),
            "solution_outline": self._string_list(assist.get("solution_outline")),
            "agent_context": assist.get("agent_context") if isinstance(assist.get("agent_context"), dict) else {},
            "memory_policy": assist.get("memory_policy") if isinstance(assist.get("memory_policy"), dict) else {},
            "context_gaps": self._string_list(assist.get("context_gaps")),
            "needs_observation": bool(assist.get("needs_observation")),
            "draft_response": str(assist.get("draft_response") or assist.get("response") or "")[:2000],
        }
        return self._decision(
            route=route,
            risk=risk,
            reason=f"model-assisted: {reason}; rule baseline: {base.reason}",
            intent=str(assist.get("intent") or base.intent),
            complexity=str(assist.get("complexity") or base.complexity),
            capability=self._capability_for_route(route, base.capability),
            signals=signals,
            requires_confirmation=base.requires_confirmation or risk in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
            selected_probe=selected_probe,
            target_agent=str(assist.get("target_agent") or base.target_agent or "") or None,
            constraints=constraints,
            model_assist=model_assist,
        )

    def _probe_for_text(self, lowered: str) -> str | None:
        if self._asks_current_time(lowered):
            return "time"
        if "端口" in lowered or "port" in lowered:
            return "port"
        if "git" in lowered or "未提交" in lowered or "工作区" in lowered:
            return "git"
        if "进程" in lowered or "process" in lowered:
            return "process"
        if "系统" in lowered or "环境" in lowered:
            return "system"
        return None

    def _asks_current_time(self, lowered: str) -> bool:
        temporal_markers = ["现在", "当前", "今天", "日期", "几点", "时间", "星期", "today", "date", "time", "now"]
        if not any(marker in lowered for marker in temporal_markers):
            return False
        false_friends = ["耗时", "时间复杂度", "运行时间", "timeout", "timestamp"]
        return not any(marker in lowered for marker in false_friends)

    def _skill_for_text(self, lowered: str) -> str | None:
        if "诊断" in lowered and "openclaw" in lowered:
            return "diagnose_openclaw"
        if "检查端口" in lowered or "check_port" in lowered:
            return "check_port"
        if "总结日志" in lowered or "summarize_logs" in lowered:
            return "summarize_logs"
        if "安全提交" in lowered or "safe_git_commit" in lowered:
            return "safe_git_commit"
        return None

    def _needs_agent(self, lowered: str, attention_focus: list[str], complexity: str) -> bool:
        complex_markers = ["修改", "实现", "开发", "重构", "修复", "调试", "多文件", "代码", "agent", "openclaw", "hermes"]
        return complexity == "complex" or any(marker in lowered for marker in complex_markers) or len(attention_focus) >= 3

    def _is_rollback_request(self, lowered: str) -> bool:
        rollback_markers = ["rollback", "restore snapshot", "restore snap_", "回滚", "恢复快照"]
        return any(marker in lowered for marker in rollback_markers)

    def _risk_for_text(self, lowered: str) -> RiskLevel:
        return classify_text_risk(lowered)

    def _intent_for_text(self, lowered: str) -> tuple[str, list[str]]:
        action_markers = ["检查", "查看", "修复", "执行", "修改", "部署", "重启", "创建", "写入", "诊断"]
        if any(marker in lowered for marker in action_markers):
            return "action", ["intent:action"]
        if any(marker in lowered for marker in ["为什么", "是什么", "解释", "说明", "总结"]):
            return "information", ["intent:information"]
        return "unknown", []

    def _complexity_for_text(self, lowered: str, attention_focus: list[str]) -> tuple[str, list[str]]:
        signals: list[str] = []
        if len(lowered) > 120:
            signals.append("long_request")
        if any(marker in lowered for marker in ["架构", "多步骤", "整体", "完整", "逐步", "跨文件"]):
            signals.append("multi_step")
        if len(attention_focus) >= 3:
            signals.append("broad_attention")
        if "multi_step" in signals or len(signals) >= 2:
            return "complex", [f"complexity:{signal}" for signal in signals]
        if signals:
            return "moderate", [f"complexity:{signal}" for signal in signals]
        return "simple", []

    def _risk_signals(self, risk: RiskLevel) -> list[str]:
        return [f"risk:{risk.value}"]

    def _route_from_model(self, value: Any, default: Route) -> Route:
        if not value:
            return default
        try:
            route = Route(str(value))
        except ValueError:
            return default
        if route == Route.NATIVE_TOOL:
            return Route.HUMAN_REVIEW
        return route

    def _selected_probe_from_model(self, assist: dict[str, Any], default: str | None) -> str:
        allowed = {"time", "system", "git", "port", "process", "file", "log", "network", "web", "openclaw", "hermes", "mcp"}
        selected = str(assist.get("selected_probe") or assist.get("probe") or default or "system")
        return selected if selected in allowed else "system"

    def _agent_allowed_by_policy(self, text: str, base: Decision, assist: dict[str, Any]) -> bool:
        lowered = text.lower()
        route_markers = ["修改", "实现", "开发", "重构", "修复", "调试", "多文件", "代码", "部署", "接入", "agent", "openclaw", "hermes"]
        assist_intent = str(assist.get("intent") or "").lower()
        assist_complexity = str(assist.get("complexity") or base.complexity).lower()
        if base.route == Route.AGENT or "agent_required" in base.signals:
            return True
        if any(marker in lowered for marker in route_markers):
            return True
        return assist_intent in {"action", "implementation", "execution"} and assist_complexity in {"moderate", "complex"}

    def _selected_skill_from_model(self, assist: dict[str, Any], default: str | None) -> str:
        allowed = {"diagnose_openclaw", "check_port", "summarize_logs", "safe_git_commit"}
        selected = str(assist.get("selected_skill") or assist.get("skill") or default or "")
        return selected if selected in allowed else (default or "")

    def _capability_for_route(self, route: Route, default: str) -> str:
        return {
            Route.DIRECT_ANSWER: "native_answer",
            Route.PROBE: "probe",
            Route.SKILL: "skill",
            Route.AGENT: "selected_agent_runtime",
            Route.HUMAN_REVIEW: "human_review",
            Route.BLOCK: "guardian",
            Route.ROLLBACK: "rollback_audit",
        }.get(route, default)

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        order = list(RiskLevel)
        return right if order.index(right) > order.index(left) else left

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value[:12] if item is not None]
        if isinstance(value, str) and value:
            return [value]
        return []

    def _dedupe(self, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            output.append(value)
        return output

    def _decision(
        self,
        *,
        route: Route,
        risk: RiskLevel,
        reason: str,
        intent: str,
        complexity: str,
        capability: str,
        signals: list[str],
        requires_confirmation: bool = False,
        selected_probe: str | None = None,
        target_agent: str | None = None,
        constraints: list[str] | None = None,
        model_assist: dict[str, Any] | None = None,
    ) -> Decision:
        return Decision(
            route=route,
            risk_level=risk,
            reason=reason,
            requires_confirmation=requires_confirmation,
            selected_probe=selected_probe,
            target_agent=target_agent,
            intent=intent,
            complexity=complexity,
            capability=capability,
            signals=signals,
            constraints=constraints or [],
            model_assist=model_assist or {},
        )
