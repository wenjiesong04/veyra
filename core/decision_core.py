from __future__ import annotations

import re

from interface.event_schema import Decision, RiskLevel, Route


class DecisionCore:
    def decide(self, text: str, attention_focus: list[str]) -> Decision:
        lowered = text.lower()
        risk = self._risk_for_text(lowered)
        if risk == RiskLevel.R5:
            return Decision(route=Route.BLOCK, risk_level=risk, reason="destructive or forbidden action detected")
        if risk in {RiskLevel.R3, RiskLevel.R4}:
            return Decision(route=Route.HUMAN_REVIEW, risk_level=risk, reason="medium/high risk action requires review", requires_confirmation=True)
        skill = self._skill_for_text(lowered)
        if skill:
            return Decision(route=Route.SKILL, risk_level=RiskLevel.R1 if skill != "safe_git_commit" else RiskLevel.R2, reason=f"built-in skill selected: {skill}", selected_probe=skill)
        probe = self._probe_for_text(lowered)
        if probe:
            return Decision(route=Route.PROBE, risk_level=RiskLevel.R1, reason=f"read-only probe selected: {probe}", selected_probe=probe)
        if self._needs_agent(lowered, attention_focus):
            return Decision(route=Route.AGENT, risk_level=risk, reason="complex task requires selected agent runtime")
        return Decision(route=Route.DIRECT_ANSWER, risk_level=RiskLevel.R0, reason="low complexity informational request")

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

    def _needs_agent(self, lowered: str, attention_focus: list[str]) -> bool:
        complex_markers = ["修改", "实现", "开发", "重构", "修复", "调试", "多文件", "代码", "agent", "openclaw", "hermes"]
        return any(marker in lowered for marker in complex_markers) or len(attention_focus) >= 3

    def _risk_for_text(self, lowered: str) -> RiskLevel:
        forbidden = [r"rm\s+-rf", "curl | bash", "drop database", "truncate table", "git push --force", "读取.env", ".env 外发"]
        if any(re.search(pattern, lowered) for pattern in forbidden):
            return RiskLevel.R5
        high = ["sudo", "重启", "restart", "部署", "delete", "删除", "chmod", "chown"]
        if any(marker in lowered for marker in high):
            return RiskLevel.R4
        write = ["写入", "创建文件", "修改文件", "commit", "提交"]
        if any(marker in lowered for marker in write):
            return RiskLevel.R2
        return RiskLevel.R0
