from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.execution_tier import classify_execution_tier
from interface.event_schema import Decision


@dataclass(slots=True)
class DecisionPlan:
    intent: str = "unknown"
    confidence: float = 0.0
    execution_tier: str = "L0_direct_answer"
    primary_response: str = ""
    followup_messages: list[str] = field(default_factory=list)
    needs_probe: bool = False
    probe_requests: list[str] = field(default_factory=list)
    needs_agent: bool = False
    selected_agent: str | None = None
    needs_confirmation: bool = False
    should_create_commitment: bool = False
    should_block: bool = False
    risk_level: str = "R0"
    reason_summary: str = ""
    audit_hints: dict[str, Any] = field(default_factory=dict)
    model_unavailable_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_decision(cls, text: str, decision: Decision) -> DecisionPlan:
        assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        model_status = str(assist.get("status") or "")
        confidence = float(assist.get("confidence") or (0.72 if model_status == "model_assisted" else 0.55))
        tier = str(assist.get("execution_tier") or classify_execution_tier(text, decision))
        return cls(
            intent=decision.intent,
            confidence=confidence,
            execution_tier=tier,
            primary_response=str(assist.get("draft_response") or ""),
            needs_probe=decision.needs_probe,
            probe_requests=[decision.selected_probe] if decision.selected_probe else [],
            needs_agent=decision.needs_agent,
            selected_agent=decision.target_agent,
            needs_confirmation=decision.needs_user_confirmation or decision.requires_confirmation,
            should_create_commitment=tier == "L5_proactive_commitment",
            should_block=decision.route.value == "block",
            risk_level=decision.risk_level.value,
            reason_summary=decision.reason,
            audit_hints={
                "route": decision.route.value,
                "capability": decision.capability,
                "signals": list(decision.signals or []),
                "model_status": model_status,
            },
            model_unavailable_fallback=model_status not in {"model_assisted"} and bool(assist),
        )
