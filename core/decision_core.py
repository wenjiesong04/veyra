from __future__ import annotations

from core.definitions import RiskLevel, classify_text_risk
from interface.event_schema import Decision, Route


class DecisionCore:
    def decide(self, text: str, attention_focus: list[str]) -> Decision:
        lowered = text.lower()
        risk = self._risk_for_text(lowered)
        intent, intent_signals = self._intent_for_text(lowered)
        complexity, complexity_signals = self._complexity_for_text(lowered, attention_focus)
        signals = intent_signals + complexity_signals + self._risk_signals(risk)
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
        return self._decision(
            route=Route.DIRECT_ANSWER,
            risk=RiskLevel.R0,
            reason="low complexity informational request",
            intent=intent,
            complexity=complexity,
            capability="native_answer",
            signals=signals,
            constraints=["no tool execution"],
        )

    def _probe_for_text(self, lowered: str) -> str | None:
        if "端口" in lowered or "port" in lowered:
            return "port"
        if "git" in lowered or "未提交" in lowered or "工作区" in lowered:
            return "git"
        if "进程" in lowered or "process" in lowered:
            return "process"
        if "系统" in lowered or "环境" in lowered:
            return "system"
        return None

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

    def _risk_for_text(self, lowered: str) -> RiskLevel:
        return classify_text_risk(lowered)

    def _intent_for_text(self, lowered: str) -> tuple[str, list[str]]:
        action_markers = ["检查", "查看", "看", "修复", "执行", "修改", "部署", "重启", "创建", "写入", "诊断"]
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
        )
