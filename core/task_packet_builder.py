from __future__ import annotations

from uuid import uuid4
from typing import Any

from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, VeyraTaskPacket
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


class TaskPacketBuilder:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def build(
        self,
        event: VeyraEvent,
        target_agent: str,
        context_patch: dict[str, object],
        persona_patch: dict[str, object],
        policy_patch: dict[str, object],
        required_capabilities: list[str] | None = None,
        memory_policy: str = "forget",
    ) -> VeyraTaskPacket:
        task_id = f"task_{uuid4().hex[:12]}"
        patched_policy = dict(policy_patch)
        patched_policy["tool_proxy_contract"] = agent_tool_proxy_contract(task_id)
        risk_level = str(patched_policy.get("risk_level") or "R0")
        return VeyraTaskPacket(
            task_id=task_id,
            target_agent=target_agent,
            session_id=event.source.session_id,
            user_message=str(event.payload.get("text", "")),
            user_goal=str(context_patch.get("user_goal") or event.payload.get("text", "")),
            required_capabilities=list(dict.fromkeys(required_capabilities or [])),
            context_patch=context_patch,
            persona_patch=persona_patch,
            policy_patch=patched_policy,
            verification_policy=self._verification_policy(risk_level, required_capabilities or [], context_patch),
            rollback_requirement=self._rollback_requirement(risk_level, patched_policy),
            memory_policy=memory_policy,
        )

    def _verification_policy(self, risk_level: str, capabilities: list[str], context_patch: dict[str, Any]) -> dict[str, Any]:
        risk = self._risk(risk_level)
        requires_evidence = risk != RiskLevel.R0 or bool(capabilities)
        freshness_required = bool((context_patch.get("decision_trace") or {}).get("freshness_required")) if isinstance(context_patch.get("decision_trace"), dict) else False
        return {
            "requires_evidence": requires_evidence,
            "allow_guessing": False,
            "requires_reprobe": freshness_required,
            "check_file_changes": risk in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5} or any("code_edit" in item or item.endswith(".file") for item in capabilities),
            "check_tool_proxy_traces": risk in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
            "check_rollback_need": risk in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
        }

    def _rollback_requirement(self, risk_level: str, policy_patch: dict[str, Any]) -> dict[str, Any]:
        risk = self._risk(risk_level)
        snapshot_required = bool(policy_patch.get("requires_snapshot") or policy_patch.get("whether_snapshot_required"))
        return {
            "snapshot_required": snapshot_required,
            "rollback_plan_required": snapshot_required or risk in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
            "allowed_without_snapshot": risk in {RiskLevel.R0, RiskLevel.R1},
            "reason": "side-effecting or risky actions require snapshot/diff/rollback evidence" if snapshot_required or risk != RiskLevel.R0 else "answer-only task",
        }

    def _risk(self, value: str) -> RiskLevel:
        try:
            return RiskLevel(value)
        except ValueError:
            return RiskLevel.R0
