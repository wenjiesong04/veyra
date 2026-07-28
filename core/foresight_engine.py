from __future__ import annotations

from datetime import datetime
from typing import Any

from core.definitions import RiskLevel, risk_policy
from core.foresight_contract import (
    assess_tool_invocation as assess_canonical_tool_invocation,
    capability_effect_contracts,
)
from core.reasoning_core import CoreReasoning
from tool_proxy.governance_contract import ToolInvocation


class ForesightEngine:
    def __init__(self, reasoning: CoreReasoning | None = None) -> None:
        self.reasoning = reasoning

    def assess_tool_invocation(
        self,
        invocation: ToolInvocation,
        *,
        created_at: datetime | None = None,
        valid_for_seconds: int = 900,
    ) -> dict[str, Any]:
        """Return the deterministic, invocation-bound effect assessment."""

        return assess_canonical_tool_invocation(
            invocation,
            created_at=created_at,
            valid_for_seconds=valid_for_seconds,
        ).model_dump(mode="json")

    def effect_contracts(self) -> tuple[dict[str, Any], ...]:
        """Expose the fixed contract registry without adding capabilities."""

        return tuple(
            contract.model_dump(mode="json")
            for contract in capability_effect_contracts()
        )

    def predict_text_action(self, text: str, risk_level: RiskLevel, decision: dict[str, Any] | None = None) -> dict[str, object]:
        foresight = self._rule_predict_text_action(text, risk_level)
        if not self.reasoning:
            return foresight
        assist = self.reasoning.foresight_assist(text=text, risk_level=risk_level.value, rule_foresight=foresight, decision=decision)
        return self._merge_model_assist(foresight, assist)

    def _rule_predict_text_action(self, text: str, risk_level: RiskLevel) -> dict[str, object]:
        lowered = text.lower()
        reversible = "full"
        side_effects: list[str] = []
        safer_alternatives: list[str] = []
        required_preconditions: list[str] = []
        if any(marker in lowered for marker in ["重启", "restart"]):
            reversible = "partial"
            side_effects.append("service interruption")
            safer_alternatives.append("check status and logs before restart")
            required_preconditions.append("confirm service health and rollback path")
        if any(marker in lowered for marker in ["删除", "rm ", "delete"]):
            reversible = "low"
            side_effects.append("file or data loss")
            safer_alternatives.append("create snapshot and list targets before deletion")
            required_preconditions.append("create snapshot before deletion")
        return {
            "risk_level": risk_level.value,
            "risk_policy": risk_policy(risk_level).to_dict(),
            "reversible": reversible,
            "side_effects": side_effects,
            "required_preconditions": required_preconditions,
            "safer_alternatives": safer_alternatives,
        }

    def _merge_model_assist(self, foresight: dict[str, object], assist: dict[str, Any]) -> dict[str, object]:
        if assist.get("status") != "model_assisted":
            return foresight
        merged = dict(foresight)
        merged["reversible"] = self._more_cautious_reversibility(str(foresight.get("reversible") or "full"), str(assist.get("reversible") or "full"))
        merged["side_effects"] = self._merge_strings(foresight.get("side_effects"), assist.get("side_effects"))
        merged["required_preconditions"] = self._merge_strings(foresight.get("required_preconditions"), assist.get("required_preconditions"))
        merged["safer_alternatives"] = self._merge_strings(foresight.get("safer_alternatives"), assist.get("safer_alternatives"))
        merged["unsafe_assumptions"] = self._merge_strings(foresight.get("unsafe_assumptions"), assist.get("unsafe_assumptions"))
        if assist.get("impact_summary"):
            merged["impact_summary"] = str(assist.get("impact_summary"))[:1000]
        merged["model_assist"] = {
            "status": "model_assisted",
            "confidence": assist.get("confidence"),
            "impact_summary": str(assist.get("impact_summary") or "")[:1000],
        }
        return merged

    def _merge_strings(self, left: object, right: object) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in self._as_string_list(left) + self._as_string_list(right):
            if value in seen:
                continue
            seen.add(value)
            output.append(value)
        return output[:16]

    def _as_string_list(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value[:16] if item is not None and str(item)]
        if isinstance(value, str) and value:
            return [value]
        return []

    def _more_cautious_reversibility(self, left: str, right: str) -> str:
        order = {"full": 0, "partial": 1, "low": 2, "none": 3, "irreversible": 3}
        normalized_left = left if left in order else "full"
        normalized_right = right if right in order else "full"
        return normalized_right if order[normalized_right] > order[normalized_left] else normalized_left
