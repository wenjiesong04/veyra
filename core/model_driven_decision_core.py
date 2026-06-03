from __future__ import annotations

from typing import Any

from core.decision_plan import DecisionPlan
from core.execution_tier import classify_execution_tier
from interface.event_schema import Decision, VeyraEvent


class ModelDrivenDecisionCore:
    """Attaches a structured DecisionPlan to each Decision.

    Rule/keyword layers remain as signals and safety locks; the plan records the
    model-assisted overlay (when configured) plus the selected execution tier.
    """

    def enrich(self, text: str, decision: Decision, *, event: VeyraEvent | None = None) -> Decision:
        assist = dict(decision.model_assist or {})
        assist.setdefault("execution_tier", classify_execution_tier(text, decision))
        plan = DecisionPlan.from_decision(text, decision)
        assist["decision_plan"] = plan.to_dict()
        decision.model_assist = assist
        decision.signals = list(dict.fromkeys([*(decision.signals or []), f"execution_tier:{plan.execution_tier}"]))
        if plan.model_unavailable_fallback:
            decision.signals.append("model_unavailable_fallback")
        return decision
