from __future__ import annotations

import copy
import json
from typing import Any

from runtime.routing_trace import estimate_tokens


class ContextDriftDetector:
    """Detects context packets that are too stale, oversized, or polluted for a turn."""

    def __init__(self, *, soft_char_limit: int = 6000, hard_char_limit: int = 10000) -> None:
        self.soft_char_limit = soft_char_limit
        self.hard_char_limit = hard_char_limit

    def evaluate(
        self,
        context: dict[str, Any],
        *,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chars = len(json.dumps(context, ensure_ascii=False))
        reasons: list[str] = []
        score = 0.0
        decision = decision or {}

        if chars > self.hard_char_limit:
            score += 0.45
            reasons.append("context_exceeds_hard_threshold")
        elif chars > self.soft_char_limit:
            score += 0.25
            reasons.append("context_exceeds_soft_threshold")

        stale = context.get("stale_beliefs") if isinstance(context.get("stale_beliefs"), list) else []
        if stale:
            score += min(0.3, 0.12 + 0.05 * len(stale))
            reasons.append("stale_beliefs_present")

        active = context.get("active_context") if isinstance(context.get("active_context"), dict) else {}
        persona = active.get("persona") if isinstance(active.get("persona"), dict) else {}
        modes = [str(item).lower() for item in persona.get("active_modes", [])] if isinstance(persona.get("active_modes"), list) else []
        intent = str(decision.get("intent") or active.get("rule_decision", {}).get("intent") or "")
        route = str(decision.get("route") or active.get("rule_decision", {}).get("route") or "")
        if intent in {"information", "conversation", "unknown"} and route in {"direct_answer", ""} and any(mode in {"guardian", "operator"} for mode in modes):
            score += 0.16
            reasons.append("governance_persona_pollutes_ordinary_answer")

        short_memory = context.get("short_memory") if isinstance(context.get("short_memory"), dict) else {}
        task = short_memory.get("task") if isinstance(short_memory.get("task"), dict) else {}
        recent_history = task.get("recent_history") if isinstance(task.get("recent_history"), list) else []
        if self._recent_failed_agent_history(recent_history) and route not in {"agent", "native_tool", "human_review"}:
            score += 0.18
            reasons.append("previous_agent_result_may_bias_current_turn")

        conversation_tail = short_memory.get("conversation_tail") if isinstance(short_memory.get("conversation_tail"), list) else []
        fresh_claims = (
            context.get("belief", {}).get("fresh_claims", [])
            if isinstance(context.get("belief"), dict) and isinstance(context.get("belief", {}).get("fresh_claims"), list)
            else []
        )
        if len(conversation_tail) > 3 or len(fresh_claims) > 4:
            score += 0.16
            reasons.append("memory_over_injection")

        if len(set(modes)) > 3:
            score += 0.12
            reasons.append("persona_mode_switch_noise")

        score = min(1.0, round(score, 3))
        suggested_action = self._suggested_action(score, reasons)
        return {
            "drift_score": score,
            "drift_reasons": reasons,
            "suggested_action": suggested_action,
            "context_chars": chars,
            "estimated_tokens": estimate_tokens(chars),
            "warning": score >= 0.45,
        }

    def remediate(self, context: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        if float(report.get("drift_score") or 0.0) < 0.45:
            return context
        compacted = copy.deepcopy(context)
        compacted["stale_beliefs"] = []
        short_memory = compacted.get("short_memory") if isinstance(compacted.get("short_memory"), dict) else {}
        if isinstance(short_memory.get("conversation_tail"), list):
            short_memory["conversation_tail"] = short_memory["conversation_tail"][-1:]
        task = short_memory.get("task") if isinstance(short_memory.get("task"), dict) else {}
        if isinstance(task.get("recent_history"), list):
            task["recent_history"] = task["recent_history"][-1:]
        if isinstance(task.get("short_term_memory"), list):
            task["short_term_memory"] = task["short_term_memory"][-1:]
        belief = compacted.get("belief") if isinstance(compacted.get("belief"), dict) else {}
        if isinstance(belief.get("fresh_claims"), list):
            belief["fresh_claims"] = belief["fresh_claims"][:1]
        compacted["_context_drift_action"] = report.get("suggested_action")
        return compacted

    def apply(self, context: dict[str, Any], *, decision: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        report = self.evaluate(context, decision=decision)
        compacted = self.remediate(context, report)
        final_chars = len(json.dumps(compacted, ensure_ascii=False))
        report["remediated_context_chars"] = final_chars
        report["remediated_estimated_tokens"] = estimate_tokens(final_chars)
        return compacted, report

    def _recent_failed_agent_history(self, recent_history: list[Any]) -> bool:
        for item in recent_history[-2:]:
            if not isinstance(item, dict):
                continue
            if item.get("route") == "agent" and str(item.get("status") or "") in {"failed", "error", "needs_rollback", "verified_failed"}:
                return True
        return False

    def _suggested_action(self, score: float, reasons: list[str]) -> str:
        if score >= 0.75:
            return "compress_context_remove_stale_beliefs_and_lower_history_weight"
        if "stale_beliefs_present" in reasons:
            return "remove_stale_beliefs_before_core_reasoning"
        if "memory_over_injection" in reasons:
            return "lower_history_and_memory_weight"
        if score >= 0.45:
            return "record_warning_and_use_compacted_context"
        return "none"
