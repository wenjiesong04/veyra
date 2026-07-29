from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from core.agent_session_router import default_agent_execution_session_id
from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.agent_dialogue_contract import (
    DialogueType,
    collaboration_turn_transport_identity,
    parse_dialogue_message,
    scope_digest,
)
from interface.event_schema import VeyraEvent, VeyraTaskPacket
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


PHASE3_SANDBOX_EXECUTION_PROFILE = "phase3_sandbox_proposal"
PHASE6_READ_ONLY_COLLABORATION_PROFILE = (
    "phase6_read_only_collaboration.v1"
)
TRUSTED_AGENT_EXECUTION_PROFILES = frozenset(
    {
        PHASE3_SANDBOX_EXECUTION_PROFILE,
        PHASE6_READ_ONLY_COLLABORATION_PROFILE,
    }
)


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
        agent_execution_session_id: str | None = None,
        agent_session_policy: str = "ephemeral_per_task",
        task_id: str | None = None,
        case_id: str | None = None,
        step_id: str | None = None,
        runtime_run_id: str | None = None,
        dialogue_message: dict[str, object] | None = None,
        execution_profile: str | None = None,
    ) -> VeyraTaskPacket:
        resolved_task_id = (
            str(task_id or "").strip() or f"task_{uuid4().hex[:12]}"
        )
        resolved_case_id = (
            str(case_id or "").strip() or event.event_id
        )
        resolved_step_id = (
            str(step_id or "").strip() or resolved_task_id
        )
        resolved_execution_profile = (
            PHASE3_SANDBOX_EXECUTION_PROFILE
            if execution_profile is None
            else str(execution_profile)
        )
        if (
            resolved_execution_profile
            not in TRUSTED_AGENT_EXECUTION_PROFILES
        ):
            raise ValueError(
                "execution_profile is not a trusted Agent execution profile"
            )
        requested_execution_session = str(
            agent_execution_session_id or ""
        ).strip()
        execution_session = (
            requested_execution_session
            or default_agent_execution_session_id(resolved_task_id)
        )
        current_workspace_id = str(
            self.state_store.read_json("local_world.json").get(
                "current_project"
            )
            or ""
        )
        resolved_session_id = event.source.session_id
        resolved_context_patch = dict(context_patch)
        resolved_persona_patch = dict(persona_patch)
        resolved_policy_patch = dict(policy_patch)
        resolved_required_capabilities = list(
            dict.fromkeys(required_capabilities or [])
        )
        resolved_user_text = str(event.payload.get("text", ""))
        resolved_verification_policy: dict[str, Any]
        resolved_rollback_requirement: dict[str, Any]
        if (
            resolved_execution_profile
            == PHASE6_READ_ONLY_COLLABORATION_PROFILE
        ):
            if not isinstance(dialogue_message, dict):
                raise ValueError(
                    "Phase 6 collaboration requires a dialogue message"
                )
            parsed_dialogue = parse_dialogue_message(
                dialogue_message,
                expected_sender="veyra",
                allowed_types={
                    DialogueType.TASK_REQUEST,
                    DialogueType.CONTEXT_PATCH,
                    DialogueType.PLAN_SELECTION,
                },
            )
            binding = parsed_dialogue.collaboration_binding
            if binding is None:
                raise ValueError(
                    "Phase 6 collaboration requires an exact binding"
                )
            now = datetime.now(timezone.utc)
            if binding.issued_at > now:
                raise ValueError(
                    "Phase 6 collaboration binding is not active yet"
                )
            if binding.expires_at <= now:
                raise ValueError(
                    "Phase 6 collaboration binding has expired"
                )
            dialogue_type = DialogueType(
                parsed_dialogue.message_type
            )
            expected_context = (
                dict(parsed_dialogue.payload.context)
                if dialogue_type
                in {
                    DialogueType.TASK_REQUEST,
                    DialogueType.CONTEXT_PATCH,
                }
                else {}
            )
            expected_user_text = (
                str(parsed_dialogue.payload.user_goal)
                if dialogue_type == DialogueType.TASK_REQUEST
                else ""
            )
            if resolved_task_id != parsed_dialogue.task_packet_id:
                raise ValueError(
                    "Phase 6 task_id must match dialogue task_packet_id"
                )
            if resolved_case_id != parsed_dialogue.case_id:
                raise ValueError(
                    "Phase 6 case_id must match dialogue case_id"
                )
            if parsed_dialogue.scope_digest != scope_digest(
                user_id=event.source.user_id,
                workspace_id=current_workspace_id,
            ):
                raise ValueError(
                    "Phase 6 dialogue owner scope must match private "
                    "governance context"
                )
            if target_agent != binding.provider.runtime:
                raise ValueError(
                    "Phase 6 target_agent must match bound provider runtime"
                )
            transport_identity = (
                collaboration_turn_transport_identity(dialogue_message)
            )
            expected_execution_session = transport_identity[
                "agent_execution_session_id"
            ]
            if (
                requested_execution_session
                and requested_execution_session
                != expected_execution_session
            ):
                raise ValueError(
                    "Phase 6 Agent execution session must match the exact "
                    "bound turn"
                )
            execution_session = expected_execution_session
            resolved_session_id = transport_identity[
                "packet_session_id"
            ]
            if resolved_context_patch != expected_context:
                raise ValueError(
                    "Phase 6 context_patch must exactly match dialogue context"
                )
            if resolved_persona_patch or resolved_policy_patch:
                raise ValueError(
                    "Phase 6 persona_patch and policy_patch must be empty"
                )
            if resolved_required_capabilities:
                raise ValueError(
                    "Phase 6 required_capabilities must be empty"
                )
            if memory_policy != "forget":
                raise ValueError(
                    "Phase 6 collaboration memory_policy must be forget"
                )
            if agent_session_policy != "ephemeral_per_task":
                raise ValueError(
                    "Phase 6 collaboration session must be ephemeral"
                )
            resolved_user_text = expected_user_text
            resolved_verification_policy = {}
            resolved_rollback_requirement = {}
        else:
            resolved_policy_patch[
                "tool_proxy_contract"
            ] = agent_tool_proxy_contract(resolved_task_id)
            risk_level = str(
                resolved_policy_patch.get("risk_level") or "R0"
            )
            resolved_verification_policy = self._verification_policy(
                risk_level,
                resolved_required_capabilities,
                resolved_context_patch,
            )
            resolved_rollback_requirement = (
                self._rollback_requirement(
                    risk_level,
                    resolved_policy_patch,
                )
            )
        packet_fields: dict[str, Any] = {
            "task_id": resolved_task_id,
            "target_agent": target_agent,
            "session_id": resolved_session_id,
            "user_message": resolved_user_text,
            "user_goal": resolved_user_text,
            "required_capabilities": resolved_required_capabilities,
            "context_patch": resolved_context_patch,
            "persona_patch": resolved_persona_patch,
            "policy_patch": resolved_policy_patch,
            "verification_policy": resolved_verification_policy,
            "rollback_requirement": resolved_rollback_requirement,
            "memory_policy": memory_policy,
            "agent_execution_session_id": execution_session,
            "agent_session_policy": agent_session_policy,
            "governance_context": {
                "user_id": event.source.user_id,
                "workspace_id": str(
                    current_workspace_id
                ),
                "channel_id": event.source.channel,
                "case_id": resolved_case_id,
                "step_id": resolved_step_id,
                # This is deliberately private server-owned dispatch state.
                # VeyraTaskPacket.to_dict() excludes governance_context, so an
                # Agent cannot expand its own tool allowlist by editing prompt
                # or dialogue content.
                "execution_profile": resolved_execution_profile,
            },
        }
        # Phase 4 adds a private, preallocated runtime identity and a public
        # bounded-dialogue envelope. Keep this builder compatible while those
        # packet fields are rolled out alongside it.
        dataclass_fields = getattr(
            VeyraTaskPacket,
            "__dataclass_fields__",
            {},
        )
        if "runtime_run_id" in dataclass_fields:
            packet_fields["runtime_run_id"] = str(
                runtime_run_id or ""
            ).strip()
        if "dialogue_message" in dataclass_fields:
            packet_fields["dialogue_message"] = (
                dict(dialogue_message)
                if isinstance(dialogue_message, dict)
                else None
            )
        return VeyraTaskPacket(**packet_fields)

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
