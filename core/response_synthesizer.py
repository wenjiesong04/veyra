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

    def dialogue_response(
        self,
        dialogue_message: dict[str, Any],
        verification: dict[str, Any] | None = None,
    ) -> str:
        """Render a bounded Agent proposal without implying authority or success."""

        verification = verification or {}
        if verification.get("status") == "verified_failed":
            return "Agent 返回的协商消息与当前 Case 不匹配；我没有采信它，Case 会保持可继续评估。"

        message_type = str(dialogue_message.get("message_type") or "")
        payload = dialogue_message.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if message_type == "EVIDENCE_REQUEST":
            needs = payload.get("requested_evidence")
            questions = [
                str(item.get("question") or "").strip()
                for item in needs
                if isinstance(item, dict) and str(item.get("question") or "").strip()
            ] if isinstance(needs, list) else []
            detail = "；".join(questions[:3])
            return (
                f"Agent 认为还缺少证据：{detail}。我会先验证或补齐这些信息，再决定是否继续。"
                if detail
                else "Agent 请求补充证据；这不是事实结论，我会先验证请求并保持 Case 可继续评估。"
            )
        if message_type == "CHALLENGE":
            reason = str(payload.get("reason") or "").strip()
            alternative = str(payload.get("alternative") or "").strip()
            detail = f"{reason}；替代判断是：{alternative}" if alternative else reason
            return (
                f"Agent 对当前前提提出了异议：{detail}。这项异议会进入 Case 评估，不会自动改写事实或授权。"
                if detail
                else "Agent 对当前前提提出了异议；我会把它作为待验证提案，而不是事实。"
            )
        if message_type == "OPTION_SET":
            options = payload.get("options")
            summaries = [
                str(item.get("summary") or "").strip()
                for item in options
                if isinstance(item, dict) and str(item.get("summary") or "").strip()
            ] if isinstance(options, list) else []
            detail = "；".join(summaries[:5])
            return (
                f"Agent 给出了这些候选方案：{detail}。它们仍需由 Veyra 比较证据、风险和权限后决定。"
                if detail
                else "Agent 返回了候选方案；这些只是提案，不代表已经授权、执行或验证。"
            )
        return "Agent 返回了无法识别的协商消息；我没有采信它，Case 会保持可继续评估。"
