from __future__ import annotations

from typing import Any


class ResponseSynthesizer:
    """Compose user-facing responses from interpreted, verified execution results."""

    def agent_response(self, interpreted: dict[str, Any], verification: dict[str, Any]) -> str:
        status = str(interpreted.get("status") or "unknown")
        executor = str(interpreted.get("executor") or "selected agent")
        summary = str(interpreted.get("summary") or "").strip()
        verifier_status = str(verification.get("status") or "")
        if status in {"submitted", "running", "pending"}:
            return f"任务已下发给 {executor}，当前状态是 {status}。我会等待 Agent 返回可验证结果后再给结论。"
        if verifier_status == "verified_success":
            return summary or f"{executor} 已完成任务，Verifier 已确认结果有证据支撑。"
        if verifier_status == "partially_success":
            return summary or f"{executor} 已接收任务，但结果尚未完成验证。"
        if verifier_status == "needs_more_probe":
            next_action = verification.get("next_action") or "collect more evidence"
            return f"{executor} 返回了结果，但证据还不够稳定，需要继续验证：{next_action}。"
        if verifier_status == "needs_rollback":
            return f"{executor} 执行失败且可能产生副作用，需要进入回滚或补偿流程。"
        failure = interpreted.get("failure_reason") or verification.get("verdict") or "execution failed"
        return f"{executor} 未能可靠完成任务：{failure}。"

    def tool_response(self, interpreted: dict[str, Any], verification: dict[str, Any]) -> str:
        summary = str(interpreted.get("summary") or "").strip()
        if verification.get("status") == "verified_success":
            return summary or "工具执行完成，结果已通过验证。"
        reason = interpreted.get("failure_reason") or verification.get("verdict") or "工具结果未通过验证"
        return f"工具执行结果不可直接采信：{reason}。"
