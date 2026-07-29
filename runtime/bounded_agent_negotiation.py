from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.durable_case import (
    CaseCheckpoint,
    CaseStatus,
    CheckpointEffectState,
    DialogueMessageType,
    DialogueRecord,
)
from core.agent_session_router import default_agent_execution_session_id
from core.task_packet_builder import TaskPacketBuilder
from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_dialogue_contract import (
    DialogueContractError,
    DialogueType,
    build_task_request,
    expected_agent_reply_message_id,
    extract_agent_dialogue,
    scope_digest,
)
from interface.event_schema import VeyraEvent, VeyraTaskPacket
from runtime.durable_case_store import (
    CaseRevisionConflictError,
    DurableCaseStore,
)


class BoundedNegotiationError(RuntimeError):
    """Base error for the Phase 4 analysis-only negotiation runtime."""


class BoundedNegotiationRuntime:
    """Coordinate one recoverable, proposal-only Agent dialogue turn.

    This runtime owns no execution authority. It admits a Durable Case,
    persists the exact TASK_REQUEST identity before dispatch, accepts only one
    of the three strict Agent reply envelopes, and advances the Case no further
    than evidence waiting, proposal, or pause.
    """

    EXECUTION_PROFILE = "phase3_sandbox_proposal"

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        case_store: DurableCaseStore,
        task_packet_builder: TaskPacketBuilder,
        verifier: Any,
        response_synthesizer: Any,
        registry: Any,
        task_tracker: Any | None = None,
    ) -> None:
        self.state_store = state_store
        self.case_store = case_store
        self.task_packet_builder = task_packet_builder
        self.verifier = verifier
        self.response_synthesizer = response_synthesizer
        self.registry = registry
        self.task_tracker = task_tracker

    @staticmethod
    def supports_adapter(adapter: AgentAdapter) -> bool:
        try:
            status = adapter.connection_status()
        except Exception:
            return False
        capabilities = (
            status.get("capabilities")
            if isinstance(status, dict)
            and isinstance(status.get("capabilities"), dict)
            else {}
        )
        features = (
            status.get("features")
            if isinstance(status, dict)
            and isinstance(status.get("features"), dict)
            else capabilities.get("features")
            if isinstance(capabilities.get("features"), dict)
            else {}
        )
        return bool(
            status.get("connected") is True
            and features.get("agent_dialogue_v1") is True
            and features.get("caller_supplied_run_id") is True
            and features.get("idempotent_submit") is True
            and features.get("exact_stop") is True
            and features.get("tool_proxy_enforced") is True
            and features.get("enforced_execution_profile")
            == BoundedNegotiationRuntime.EXECUTION_PROFILE
        )

    def prepare(
        self,
        *,
        event: VeyraEvent,
        selected_agent: str,
        context_patch: dict[str, Any],
        persona_patch: dict[str, Any],
        policy_patch: dict[str, Any],
        required_capabilities: list[str],
        memory_policy: str,
        agent_execution_session_id: str,
        agent_session_policy: str,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id()
        user_goal = str(
            context_patch.get("user_goal")
            or event.payload.get("text")
            or ""
        ).strip()
        admission = self.case_store.admit_event(
            event_id=event.event_id,
            user_id=event.source.user_id,
            workspace_id=workspace_id,
            user_goal=user_goal,
            situation_id=self._optional_text(
                context_patch.get("situation_id")
            ),
            case_type="foreground_agent_deliberation",
            operation_id=f"phase4:{event.event_id}:admit",
        )
        if (
            admission.get("operation_replayed") is True
            and str(admission.get("status") or "")
            != CaseStatus.QUALIFIED.value
        ):
            return {
                "replayed": True,
                "case": admission,
                "packet": None,
                "request": None,
            }
        if str(admission.get("status") or "") != CaseStatus.QUALIFIED.value:
            raise BoundedNegotiationError(
                "durable case is not eligible for its first Agent turn"
            )

        case_id = str(admission["case_id"])
        case_revision = int(admission["revision"])
        turn_index = 1 + sum(
            1
            for item in admission.get("dialogue", [])
            if isinstance(item, dict)
            and item.get("message_type") == DialogueType.TASK_REQUEST.value
        )
        identity = self._turn_identity(
            case_id=case_id,
            case_revision=case_revision,
            turn_index=turn_index,
        )
        execution_session_id = (
            str(agent_execution_session_id or "").strip()
            or default_agent_execution_session_id(
                identity["task_packet_id"]
            )
        )
        request = build_task_request(
            case_id=case_id,
            case_revision=case_revision,
            turn_index=turn_index,
            message_id=identity["message_id"],
            task_packet_id=identity["task_packet_id"],
            operation_id=identity["operation_id"],
            scope_digest=scope_digest(
                user_id=event.source.user_id,
                workspace_id=workspace_id,
            ),
            user_goal=user_goal,
            constraints=self._bounded_strings(
                [
                    *(
                        context_patch.get("constraints")
                        if isinstance(
                            context_patch.get("constraints"), list
                        )
                        else []
                    ),
                    "return exactly one bounded dialogue reply",
                    "do not treat requested capabilities as permission",
                    "do not claim execution or verification authority",
                ],
                limit=16,
                item_limit=500,
            ),
            evidence_refs=self._bounded_strings(
                context_patch.get("evidence_refs")
                if isinstance(context_patch.get("evidence_refs"), list)
                else [],
                limit=32,
                item_limit=256,
            ),
            context={
                "case_summary": self._case_context(context_patch),
                "allowed_reply_types": [
                    DialogueType.EVIDENCE_REQUEST.value,
                    DialogueType.CHALLENGE.value,
                    DialogueType.OPTION_SET.value,
                ],
                "remaining_budget": {
                    "agent_calls": 1,
                    "dialogue_rounds": 1,
                },
                "execution_profile": self.EXECUTION_PROFILE,
            },
            authority={
                "mode": "sandbox",
                "side_effects_require_governance": True,
                "capability_expansion_authorized": False,
                "verification_authority": False,
            },
        )
        checkpoint = self._checkpoint(
            checkpoint_id=identity["checkpoint_id"],
            phase="agent_dispatch_prepared",
            operation_id=identity["operation_id"],
            step_id=identity["step_id"],
            task_id=identity["task_packet_id"],
            run_id=identity["runtime_run_id"],
            session_key=execution_session_id,
            executor=selected_agent,
            target_agent=selected_agent,
            dialogue_message_id=identity["message_id"],
            result_status="prepared",
        )
        dialogue_record = self._dialogue_record(request)
        started = self.case_store.transition(
            case_id=case_id,
            user_id=event.source.user_id,
            workspace_id=workspace_id,
            operation_id=f"{identity['operation_id']}:persist",
            expected_revision=case_revision,
            to_status=CaseStatus.DELIBERATING,
            reason="bounded Agent dialogue prepared",
            checkpoint=checkpoint,
            dialogue_message=dialogue_record,
        )
        packet = self.task_packet_builder.build(
            event=event,
            target_agent=selected_agent,
            context_patch=context_patch,
            persona_patch=persona_patch,
            policy_patch=policy_patch,
            required_capabilities=required_capabilities,
            memory_policy=memory_policy,
            agent_execution_session_id=execution_session_id,
            agent_session_policy=agent_session_policy,
            task_id=identity["task_packet_id"],
            case_id=case_id,
            step_id=identity["step_id"],
            runtime_run_id=identity["runtime_run_id"],
            dialogue_message=request,
        )
        return {
            "replayed": False,
            "case": started,
            "packet": packet,
            "request": request,
            "binding": {
                **identity,
                "case_id": case_id,
                "case_revision": case_revision,
                "turn_index": turn_index,
                "user_id": event.source.user_id,
                "workspace_id": workspace_id,
                "scope_digest": request["scope_digest"],
                "session_key": packet.agent_execution_session_id,
                "target_agent": selected_agent,
            },
        }

    def accept_execution(
        self,
        *,
        prepared: dict[str, Any],
        execution: ExecutionResult,
    ) -> dict[str, Any]:
        binding = (
            prepared.get("binding")
            if isinstance(prepared.get("binding"), dict)
            else {}
        )
        self._require_binding(binding)
        self._assert_execution_binding(binding, execution)
        case = self.case_store.get_case(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
        )
        collaboration_effect_reported = (
            self._collaboration_effect_reported(binding, execution)
        )
        terminal_closure: dict[str, Any] = {}
        if execution.status not in NON_TERMINAL_STATUSES:
            if not self._has_exact_run_observation(execution):
                failure_code = self._run_observation_failure_code(
                    execution
                )
                raise BoundedNegotiationError(
                    "terminal Agent run was not observed exactly; "
                    "Durable Case remains evaluable "
                    f"(reason={failure_code})"
                )
            try:
                terminal_closure = (
                    self._confirm_terminal_authority_closed(
                        binding=binding,
                        case=case,
                        execution=execution,
                    )
                )
            except Exception as exc:
                if not collaboration_effect_reported:
                    raise
                terminal_closure = {
                    "status": "unconfirmed",
                    "reason": (
                        str(exc).strip()[:600]
                        or exc.__class__.__name__
                    ),
                }
        if collaboration_effect_reported:
            return self._fence_collaboration_effect(
                binding=binding,
                case=case,
                execution=execution,
                terminal_closure=terminal_closure,
            )
        actual_session_key = self._execution_session_key(
            execution,
            fallback=str(binding["session_key"]),
        )
        if execution.status in NON_TERMINAL_STATUSES:
            submitted_checkpoint_id = (
                f"{binding['checkpoint_id']}:submitted"
            )
            existing_checkpoint = self._find_checkpoint(
                case, submitted_checkpoint_id
            )
            if existing_checkpoint:
                self._assert_checkpoint_execution(
                    existing_checkpoint,
                    execution=execution,
                    binding=binding,
                )
                return self._outcome(
                    case=case,
                    execution=execution,
                    dialogue=None,
                    verification=self._pending_verification(
                        execution.status
                    ),
                    response=(
                        f"任务仍由 {execution.executor} 处理中；Durable Case "
                        "已存在同一 dispatch checkpoint，没有重复下发。"
                    ),
                )
            if str(case.get("status") or "") != CaseStatus.DELIBERATING.value:
                return self._outcome(
                    case=case,
                    execution=execution,
                    dialogue=None,
                    verification=self._fenced_verification(
                        str(case.get("status") or "")
                    ),
                    response=(
                        "Case 已不再接受新的 Agent 进展；我没有让迟到状态"
                        "改变其生命周期。"
                    ),
                )
            updated = self.case_store.append_checkpoint(
                case_id=str(binding["case_id"]),
                user_id=str(binding["user_id"]),
                workspace_id=str(binding["workspace_id"]),
                operation_id=f"{binding['operation_id']}:submitted",
                expected_revision=int(case["revision"]),
                reason="Agent dialogue run is pending",
                checkpoint=self._checkpoint(
                    checkpoint_id=f"{binding['checkpoint_id']}:submitted",
                    phase="agent_dispatched",
                    operation_id=str(binding["operation_id"]),
                    step_id=str(binding["step_id"]),
                    task_id=str(binding["task_packet_id"]),
                    run_id=str(execution.task_id),
                    session_key=actual_session_key,
                    executor=str(execution.executor),
                    target_agent=str(binding["target_agent"]),
                    dialogue_message_id=str(binding["message_id"]),
                    result_status=str(execution.status),
                ),
            )
            verification = self._pending_verification(execution.status)
            return self._outcome(
                case=updated,
                execution=execution,
                dialogue=None,
                verification=verification,
                response=(
                    f"任务已下发给 {execution.executor}；Durable Case 已保存 "
                    "dispatch checkpoint，只有收到严格协商消息后才会推进。"
                ),
            )

        raw = execution.raw if isinstance(execution.raw, dict) else {}
        agent_response = (
            raw.get("agent_response")
            if isinstance(raw.get("agent_response"), dict)
            else {}
        )
        dialogue: dict[str, Any] | None = None
        if execution.status == "success":
            expected_reply_message_id = (
                expected_agent_reply_message_id(
                    prepared["request"]
                )
                if isinstance(
                    binding.get("collaboration_binding"), dict
                )
                else None
            )
            verification = self.verifier.verify_agent_dialogue(
                agent_response,
                expected_message_id=expected_reply_message_id,
                expected_case_id=str(binding["case_id"]),
                expected_case_revision=int(binding["case_revision"]),
                expected_turn_index=int(binding["turn_index"]),
                expected_in_reply_to=str(binding["message_id"]),
                expected_task_packet_id=str(binding["task_packet_id"]),
                expected_operation_id=str(binding["operation_id"]),
                expected_scope_digest=str(binding["scope_digest"]),
                expected_collaboration_binding=(
                    binding.get("collaboration_binding")
                    if isinstance(
                        binding.get("collaboration_binding"), dict
                    )
                    else None
                ),
            )
            if verification.get("status") == "partially_success":
                dialogue = extract_agent_dialogue(
                    agent_response,
                    expected_message_id=expected_reply_message_id,
                    expected_case_id=str(binding["case_id"]),
                    expected_case_revision=int(binding["case_revision"]),
                    expected_turn_index=int(binding["turn_index"]),
                    expected_in_reply_to=str(binding["message_id"]),
                    expected_task_packet_id=str(binding["task_packet_id"]),
                    expected_operation_id=str(binding["operation_id"]),
                    expected_scope_digest=str(binding["scope_digest"]),
                    expected_collaboration_binding=(
                        binding.get("collaboration_binding")
                        if isinstance(
                            binding.get("collaboration_binding"), dict
                        )
                        else None
                    ),
                )
        else:
            verification = self._failed_execution_verification(
                execution.status
            )
        current = self.case_store.get_case(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
        )
        result_checkpoint_id = f"{binding['checkpoint_id']}:result"
        existing_checkpoint = self._find_checkpoint(
            current, result_checkpoint_id
        )
        if existing_checkpoint:
            self._assert_checkpoint_execution(
                existing_checkpoint,
                execution=execution,
                binding=binding,
            )
            if dialogue is None:
                if (
                    existing_checkpoint.get("phase")
                    != "agent_dialogue_rejected"
                ):
                    raise BoundedNegotiationError(
                        "replayed Agent result conflicts with the persisted "
                        "dialogue outcome"
                    )
            else:
                persisted_dialogue = self._find_dialogue(
                    current, str(dialogue["message_id"])
                )
                if (
                    not persisted_dialogue
                    or persisted_dialogue.get("content") != dialogue
                ):
                    raise BoundedNegotiationError(
                        "replayed Agent dialogue conflicts with the persisted "
                        "message"
                    )
            return self._outcome(
                case=current,
                execution=execution,
                dialogue=dialogue,
                verification=verification,
                response=self.response_synthesizer.dialogue_response(
                    dialogue or {}, verification
                ),
            )
        if (
            str(current.get("status") or "")
            != CaseStatus.DELIBERATING.value
        ):
            return self._outcome(
                case=current,
                execution=execution,
                dialogue=None,
                verification=self._fenced_verification(
                    str(current.get("status") or "")
                ),
                response=(
                    "Case 已进入暂停、取消或终态；迟到的 Agent 消息"
                    "没有被采信，也没有推进 Case。"
                ),
            )
        result_checkpoint = self._checkpoint(
            checkpoint_id=result_checkpoint_id,
            phase=(
                "agent_dialogue_received"
                if dialogue is not None
                else "agent_dialogue_rejected"
            ),
            operation_id=str(binding["operation_id"]),
            step_id=str(binding["step_id"]),
            task_id=str(binding["task_packet_id"]),
            run_id=str(execution.task_id),
            session_key=actual_session_key,
            binding_digest=self._optional_text(
                (
                    terminal_closure.get("identity")
                    if isinstance(
                        terminal_closure.get("identity"), dict
                    )
                    else {}
                ).get("binding_digest")
                or self._latest_checkpoint(current).get(
                    "binding_digest"
                )
            ),
            executor=str(execution.executor),
            target_agent=str(binding["target_agent"]),
            dialogue_message_id=(
                str(dialogue["message_id"])
                if dialogue is not None
                else str(binding["message_id"])
            ),
            result_status=str(execution.status),
            evidence_refs=[
                "authority_closure:"
                f"{self._digest(terminal_closure)[:32]}"
            ],
        )
        if dialogue is None:
            paused = self.case_store.transition(
                case_id=str(binding["case_id"]),
                user_id=str(binding["user_id"]),
                workspace_id=str(binding["workspace_id"]),
                operation_id=f"{binding['operation_id']}:reject",
                expected_revision=int(current["revision"]),
                to_status=CaseStatus.PAUSED,
                reason="Agent reply did not satisfy the bound dialogue contract",
                checkpoint=result_checkpoint,
            )
            return self._outcome(
                case=paused,
                execution=execution,
                dialogue=None,
                verification=verification,
                response=self.response_synthesizer.dialogue_response(
                    {}, verification
                ),
            )

        message_type = str(dialogue["message_type"])
        target_status = {
            DialogueType.EVIDENCE_REQUEST.value: CaseStatus.AWAITING_EVIDENCE,
            DialogueType.CHALLENGE.value: CaseStatus.PAUSED,
            DialogueType.OPTION_SET.value: CaseStatus.PROPOSED,
        }[message_type]
        advanced = self.case_store.transition(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
            operation_id=f"{binding['operation_id']}:reply",
            expected_revision=int(current["revision"]),
            to_status=target_status,
            reason=f"accepted bound {message_type} proposal",
            checkpoint=result_checkpoint,
            dialogue_message=self._dialogue_record(dialogue),
        )
        return self._outcome(
            case=advanced,
            execution=execution,
            dialogue=dialogue,
            verification=verification,
            response=self.response_synthesizer.dialogue_response(
                dialogue, verification
            ),
        )

    def _confirm_terminal_authority_closed(
        self,
        *,
        binding: dict[str, Any],
        case: dict[str, Any],
        execution: ExecutionResult,
    ) -> dict[str, Any]:
        """Close the exact governed run before accepting a terminal reply."""

        # The Case persists the provider-neutral execution-session identity.
        # An adapter may canonicalize that identity for its own runtime (for
        # example OpenClaw prefixes the configured agent id).  Do not overwrite
        # the adapter's trusted cached/broker binding with the neutral value
        # while confirming a terminal cleanup.  The exact run id selects the
        # authority record; the adapter and its broker must supply and validate
        # the provider-specific session key and binding digest.
        identity = {
            "run_id": str(binding["runtime_run_id"]),
        }
        adapter = self._adapter_for(str(binding["target_agent"]))
        receipt = adapter.cancel_task_authority(
            execution.task_id,
            reason="terminal_agent_dialogue_observed",
            identity=identity,
            # A terminal Agent no longer needs a chat abort, but its broker
            # and plugin authority must still be closed exactly.
            abort_agent=False,
        )
        if not isinstance(receipt, dict):
            raise BoundedNegotiationError(
                "terminal Agent authority closure returned no receipt"
            )
        confirmed = bool(
            receipt.get("status") == "cancelled"
            and receipt.get("authority_revoked") is True
            and receipt.get("plugin_authority_closed") is True
            and receipt.get("agent_abort_confirmed") is True
            and receipt.get("executing_reservations") in (None, [])
        )
        if not confirmed:
            raise BoundedNegotiationError(
                "terminal Agent authority closure is unconfirmed; "
                "Durable Case remains evaluable"
            )
        return receipt

    def accept_registered_execution(
        self,
        *,
        execution: ExecutionResult,
        task_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply an observed result through Veyra's registered binding."""

        prepared = self._prepared_for_registered_context(task_context)
        runtime_task_id = str(
            task_context.get("runtime_task_id") or ""
        ).strip()
        if runtime_task_id and runtime_task_id != execution.task_id:
            raise BoundedNegotiationError(
                "registered Case callback binding mismatch: "
                "['runtime_task_id']"
            )
        return self.accept_execution(
            prepared=prepared,
            execution=execution,
        )

    def fetch_registered_execution(
        self,
        *,
        task_context: dict[str, Any],
    ) -> ExecutionResult:
        """Re-fetch a callback hint from its exact persisted Agent binding."""

        prepared = self._prepared_for_registered_context(task_context)
        binding = prepared["binding"]
        case = prepared["case"]
        runtime_task_id = str(
            task_context.get("runtime_task_id")
            or binding["runtime_run_id"]
        ).strip()
        if runtime_task_id != str(binding["runtime_run_id"]):
            raise BoundedNegotiationError(
                "registered Case callback binding mismatch: "
                "['runtime_task_id']"
            )
        checkpoint = self._latest_checkpoint(case)
        adapter = self._adapter_for(str(binding["target_agent"]))
        fetch_bound = getattr(adapter, "fetch_bound_task_status", None)
        if not callable(fetch_bound):
            raise BoundedNegotiationError(
                "bound Agent runtime cannot re-fetch an exact Case run"
            )
        execution = fetch_bound(
            runtime_task_id,
            identity={
                "run_id": str(binding["runtime_run_id"]),
                "session_key": str(
                    checkpoint.get("session_key")
                    or binding.get("session_key")
                    or ""
                ),
                "binding_digest": str(
                    checkpoint.get("binding_digest") or ""
                ),
            },
        )
        if not isinstance(execution, ExecutionResult):
            raise BoundedNegotiationError(
                "bound Agent runtime returned an invalid execution result"
            )
        self._assert_execution_binding(binding, execution)
        if not self._has_exact_run_observation(execution):
            raise BoundedNegotiationError(
                "bound Agent run was not observed exactly"
            )
        return execution

    def _fence_collaboration_effect(
        self,
        *,
        binding: dict[str, Any],
        case: dict[str, Any],
        execution: ExecutionResult,
        terminal_closure: dict[str, Any],
    ) -> dict[str, Any]:
        """Stop a bound read-only collaboration before dialogue acceptance."""

        if execution.status in NON_TERMINAL_STATUSES:
            adapter = self._adapter_for(str(binding["target_agent"]))
            try:
                cancellation = adapter.cancel_task_authority(
                    execution.task_id,
                    reason="phase6_read_only_effect_boundary_violation",
                    identity={
                        "run_id": str(binding["runtime_run_id"]),
                    },
                    abort_agent=True,
                )
            except Exception as exc:  # pragma: no cover - diagnostic only
                cancellation = {
                    "status": "unconfirmed",
                    "error": type(exc).__name__,
                }
        else:
            cancellation = terminal_closure
        checkpoint_id = f"{binding['checkpoint_id']}:effect_fence"
        existing = self._find_checkpoint(case, checkpoint_id)
        if existing:
            current = self.case_store.get_case(
                case_id=str(binding["case_id"]),
                user_id=str(binding["user_id"]),
                workspace_id=str(binding["workspace_id"]),
            )
            return self._outcome(
                case=current,
                execution=execution,
                dialogue=None,
                verification=self._collaboration_effect_verification(),
                response=(
                    "只读协作报告了工具或文件效果；Case 已保持不确定并停止"
                    "继续协商，等待人工核对。"
                ),
            )
        current = self.case_store.get_case(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
        )
        if str(current.get("status") or "") in {
            CaseStatus.CANCELLED.value,
            CaseStatus.FAILED.value,
            CaseStatus.INDETERMINATE.value,
            CaseStatus.CLOSED.value,
        }:
            return self._outcome(
                case=current,
                execution=execution,
                dialogue=None,
                verification=self._collaboration_effect_verification(),
                response=(
                    "只读协作报告了工具或文件效果；终态 Case 未被迟到结果"
                    "推进。"
                ),
            )
        fenced = self.case_store.transition(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
            operation_id=f"{binding['operation_id']}:effect_fence",
            expected_revision=int(current["revision"]),
            to_status=CaseStatus.INDETERMINATE,
            reason=(
                "read-only collaboration reported or produced an effect; "
                "dialogue was not accepted"
            ),
            checkpoint=self._checkpoint(
                checkpoint_id=checkpoint_id,
                phase="phase6_effect_violation",
                operation_id=str(binding["operation_id"]),
                step_id=str(binding["step_id"]),
                task_id=str(binding["task_packet_id"]),
                run_id=str(execution.task_id),
                session_key=str(binding["session_key"]),
                executor=str(execution.executor),
                target_agent=str(binding["target_agent"]),
                dialogue_message_id=str(binding["message_id"]),
                result_status="indeterminate",
                effect_state=CheckpointEffectState.UNKNOWN,
                evidence_refs=[
                    "phase6_effect_boundary_violation",
                    "authority_closure:"
                    f"{self._digest(cancellation)[:32]}",
                ],
            ),
        )
        self._retire_case_tasks(
            case_id=str(binding["case_id"]),
            case_status=CaseStatus.INDETERMINATE.value,
        )
        return self._outcome(
            case=fenced,
            execution=execution,
            dialogue=None,
            verification=self._collaboration_effect_verification(),
            response=(
                "只读协作报告了工具或文件效果；Case 已保持不确定并停止"
                "继续协商，等待人工核对。"
            ),
        )

    @staticmethod
    def _collaboration_effect_reported(
        binding: dict[str, Any],
        execution: ExecutionResult,
    ) -> bool:
        collaboration = binding.get("collaboration_binding")
        if not isinstance(collaboration, dict):
            return False
        if execution.tool_calls or execution.changed_files:
            return True
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        reported_evidence = raw.get("agent_reported_tool_evidence")
        if reported_evidence is not None:
            if not isinstance(reported_evidence, dict):
                return True
            for flag_name in (
                "tool_calls_reported",
                "changed_files_reported",
            ):
                flag = reported_evidence.get(flag_name)
                if not isinstance(flag, bool) or flag:
                    return True
            for count_name in (
                "tool_call_count",
                "changed_file_count",
            ):
                count = reported_evidence.get(count_name)
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count != 0
                ):
                    return True
        if execution.status not in NON_TERMINAL_STATUSES:
            governed_evidence = raw.get("governed_tool_evidence")
            if not isinstance(governed_evidence, dict):
                return True
            if (
                str(governed_evidence.get("status") or "").strip().lower()
                != "resolved"
            ):
                return True
            for count_name in (
                "observed_call_count",
                "effect_count",
            ):
                count = governed_evidence.get(count_name)
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count != 0
                ):
                    return True
        return any(
            (
                isinstance(raw.get(key), list)
                and bool(raw.get(key))
            )
            or (
                isinstance(raw.get(key), dict)
                and bool(raw.get(key))
            )
            for key in (
                "authoritative_tool_receipts",
                "verified_tool_effects",
                "authoritative_effects",
            )
        )

    @staticmethod
    def _collaboration_effect_verification() -> dict[str, Any]:
        return {
            "status": "indeterminate",
            "verdict": "read_only_effect_boundary_violation",
            "next_action": "human_reconcile_effect_before_any_continuation",
            "evidence_status": "unknown",
            "verified": False,
        }

    def _prepared_for_registered_context(
        self,
        task_context: dict[str, Any],
    ) -> dict[str, Any]:
        if str(task_context.get("authority") or "") != "veyra_registered":
            raise BoundedNegotiationError(
                "Agent callback lacks Veyra-registered task authority"
            )
        required_context = {
            "case_id",
            "case_workspace_id",
            "user_id",
            "case_step_id",
            "case_operation_id",
            "case_revision",
            "dialogue_message_id",
            "task_packet_id",
            "target_agent",
        }
        missing = [
            key
            for key in sorted(required_context)
            if task_context.get(key) in (None, "")
        ]
        if missing:
            raise BoundedNegotiationError(
                f"registered Case callback context is incomplete: {missing}"
            )
        case = self.case_store.get_case(
            case_id=str(task_context["case_id"]),
            user_id=str(task_context["user_id"]),
            workspace_id=str(task_context["case_workspace_id"]),
        )
        prepared = self._prepared_from_case(case)
        binding = prepared["binding"]
        expected = {
            "case_id": task_context["case_id"],
            "workspace_id": task_context["case_workspace_id"],
            "user_id": task_context["user_id"],
            "step_id": task_context["case_step_id"],
            "operation_id": task_context["case_operation_id"],
            "case_revision": task_context["case_revision"],
            "message_id": task_context["dialogue_message_id"],
            "task_packet_id": task_context["task_packet_id"],
            "target_agent": task_context["target_agent"],
        }
        mismatched = [
            key
            for key, value in expected.items()
            if str(binding.get(key) or "") != str(value or "")
        ]
        if mismatched:
            raise BoundedNegotiationError(
                "registered Case callback binding mismatch: "
                f"{sorted(set(mismatched))}"
            )
        return prepared

    def cancel_case(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        current = self.case_store.get_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        checkpoint = self._latest_checkpoint(current)
        persisted_request = self._find_cancellation_request(
            current, operation_id
        )
        if persisted_request:
            requested = self.case_store.request_cancel(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_id=operation_id,
                expected_revision=expected_revision,
                reason=reason,
                checkpoint=persisted_request,
            )
            checkpoint = persisted_request
            if (
                str(requested.get("status") or "")
                == CaseStatus.CANCELLED.value
            ):
                self._retire_case_tasks(
                    case_id=case_id,
                    case_status=CaseStatus.CANCELLED.value,
                )
                return {
                    "status": "cancelled",
                    "case": self.public_case_summary(requested),
                    "cancellation": {
                        "status": "cancelled",
                        "authority_revoked": True,
                        "plugin_authority_closed": True,
                        "agent_abort_confirmed": True,
                        "receipt_replayed": True,
                    },
                }
        elif str(current.get("status") or "") == CaseStatus.CANCELLING.value:
            if int(current["revision"]) != expected_revision:
                raise CaseRevisionConflictError(
                    f"case revision conflict: expected {expected_revision}, "
                    f"observed {current['revision']}"
                )
            # Recovery reuses the already-persisted cancellation intent. It
            # retries only exact authority revocation, never the Case
            # transition or the Agent task itself.
            requested = current
        else:
            requested = self.case_store.request_cancel(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_id=operation_id,
                expected_revision=expected_revision,
                reason=reason,
                checkpoint=self._checkpoint(
                    checkpoint_id=(
                        f"cp_cancel_{self._digest(operation_id)[:20]}"
                    ),
                    phase="cancellation_requested",
                    operation_id=operation_id,
                    step_id=self._optional_text(
                        checkpoint.get("step_id")
                    ),
                    task_id=self._optional_text(
                        checkpoint.get("task_id")
                    ),
                    run_id=self._optional_text(
                        checkpoint.get("run_id")
                    ),
                    session_key=self._optional_text(
                        checkpoint.get("session_key")
                    ),
                    binding_digest=self._optional_text(
                        checkpoint.get("binding_digest")
                    ),
                    executor=self._optional_text(
                        checkpoint.get("executor")
                    ),
                    target_agent=self._optional_text(
                        checkpoint.get("target_agent")
                    ),
                    dialogue_message_id=self._optional_text(
                        checkpoint.get("dialogue_message_id")
                    ),
                    result_status="cancellation_requested",
                ),
            )
            checkpoint = self._latest_checkpoint(requested)
        run_id = str(checkpoint.get("run_id") or "").strip()
        executor = str(
            checkpoint.get("executor")
            or checkpoint.get("target_agent")
            or ""
        ).strip()
        if not run_id or not executor:
            cancellation = {
                "status": "cancelled",
                "authority_revoked": True,
                "plugin_authority_closed": True,
                "agent_abort_confirmed": True,
                "reason": "case had no dispatched Agent authority",
            }
        else:
            adapter = self._adapter_for(executor)
            cancellation_identity = {
                "run_id": run_id,
                "session_key": str(
                    checkpoint.get("session_key") or ""
                ),
                "binding_digest": str(
                    checkpoint.get("binding_digest") or ""
                ),
            }
            cancellation = adapter.cancel_task_authority(
                run_id,
                reason=reason,
                identity=cancellation_identity,
                abort_agent=abort_agent,
            )
            if not isinstance(cancellation, dict):
                raise BoundedNegotiationError(
                    "Agent cancellation did not return a structured receipt"
                )
        cancellation_ref = (
            f"cancel_receipt:{self._digest(cancellation)[:32]}"
        )
        if (
            cancellation.get("status") == "cancelled"
            and cancellation.get("authority_revoked") is True
            and cancellation.get("plugin_authority_closed") is True
            and cancellation.get("agent_abort_confirmed") is True
            and cancellation.get("executing_reservations") in (None, [])
        ):
            completed = self.case_store.complete_cancel(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_id=f"{operation_id}:complete",
                expected_revision=int(requested["revision"]),
                reason="all known Agent authority was revoked",
                checkpoint=self._checkpoint(
                    checkpoint_id=(
                        f"cp_cancelled_{self._digest(operation_id)[:20]}"
                    ),
                    phase="cancellation_confirmed",
                    operation_id=operation_id,
                    step_id=self._optional_text(
                        checkpoint.get("step_id")
                    ),
                    task_id=self._optional_text(
                        checkpoint.get("task_id")
                    ),
                    run_id=run_id or None,
                    session_key=self._optional_text(
                        checkpoint.get("session_key")
                    ),
                    binding_digest=self._optional_text(
                        cancellation.get("binding_digest")
                        or checkpoint.get("binding_digest")
                    ),
                    executor=executor or None,
                    target_agent=self._optional_text(
                        checkpoint.get("target_agent")
                    ),
                    dialogue_message_id=self._optional_text(
                        checkpoint.get("dialogue_message_id")
                    ),
                    result_status="cancelled",
                    effect_state=CheckpointEffectState.OBSERVED,
                    evidence_refs=[cancellation_ref],
                ),
            )
            self._retire_case_tasks(
                case_id=case_id,
                case_status=CaseStatus.CANCELLED.value,
            )
            return {
                "status": "cancelled",
                "case": self.public_case_summary(completed),
                "cancellation": self._public_cancellation(cancellation),
            }
        if cancellation.get("status") == "too_late":
            indeterminate = self.case_store.transition(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_id=f"{operation_id}:too_late",
                expected_revision=int(requested["revision"]),
                to_status=CaseStatus.INDETERMINATE,
                reason="cancellation raced with a started effect",
                checkpoint=self._checkpoint(
                    checkpoint_id=(
                        f"cp_indeterminate_{self._digest(operation_id)[:20]}"
                    ),
                    phase="cancellation_too_late",
                    operation_id=operation_id,
                    run_id=run_id or None,
                    session_key=self._optional_text(
                        checkpoint.get("session_key")
                    ),
                    executor=executor or None,
                    target_agent=self._optional_text(
                        checkpoint.get("target_agent")
                    ),
                    result_status="too_late",
                    effect_state=CheckpointEffectState.UNKNOWN,
                    evidence_refs=[cancellation_ref],
                ),
            )
            self._retire_case_tasks(
                case_id=case_id,
                case_status=CaseStatus.INDETERMINATE.value,
            )
            return {
                "status": "indeterminate",
                "case": self.public_case_summary(indeterminate),
                "cancellation": self._public_cancellation(cancellation),
            }
        return {
            "status": "cancellation_unconfirmed",
            "case": self.public_case_summary(requested),
            "cancellation": self._public_cancellation(cancellation),
        }

    def recover_case(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if int(case["revision"]) != expected_revision:
            raise CaseRevisionConflictError(
                f"case revision conflict: expected {expected_revision}, "
                f"observed {case['revision']}"
            )
        if str(case.get("status") or "") == CaseStatus.CANCELLING.value:
            return self.cancel_case(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                expected_revision=expected_revision,
                operation_id=operation_id,
                reason=reason,
            )
        checkpoint = self._latest_checkpoint(case)
        checkpoint_phase = str(checkpoint.get("phase") or "")
        if checkpoint_phase not in {
            "agent_dispatch_prepared",
            "agent_dispatched",
        }:
            return {
                "status": "nothing_to_reconcile",
                "case": self.public_case_summary(case),
                "trace_outbox": self.case_store.flush_trace_outbox(),
            }
        run_id = str(checkpoint.get("run_id") or "")
        executor = str(
            checkpoint.get("executor")
            or checkpoint.get("target_agent")
            or ""
        )
        if not run_id or not executor:
            return {
                "status": "identity_unavailable",
                "case": self.public_case_summary(case),
            }
        adapter = self._adapter_for(executor)
        recovery_identity = {
            "run_id": run_id,
            "session_key": str(
                checkpoint.get("session_key") or ""
            ),
            "binding_digest": str(
                checkpoint.get("binding_digest") or ""
            ),
        }
        fetch_bound = getattr(adapter, "fetch_bound_task_status", None)
        execution = (
            fetch_bound(run_id, identity=recovery_identity)
            if callable(fetch_bound)
            else adapter.fetch_task_status(run_id)
        )
        exact_run_observed = self._has_exact_run_observation(execution)
        if (
            checkpoint_phase == "agent_dispatch_prepared"
            and not exact_run_observed
        ):
            cancelled = self.cancel_case(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                expected_revision=expected_revision,
                operation_id=f"{operation_id}:ambiguous_dispatch",
                reason=(
                    "prepared Agent dispatch could not be proven after "
                    "worker recovery"
                ),
                abort_agent=False,
            )
            return {
                **cancelled,
                "recovery_status": "ambiguous_dispatch_cancelled",
            }
        if (
            execution.status not in NON_TERMINAL_STATUSES
            and not exact_run_observed
        ):
            return {
                "status": "recovery_degraded",
                "case": self.public_case_summary(case),
                "execution_result": self.public_execution_summary(
                    execution
                ),
                "reason": "terminal Agent run state was not observed exactly",
            }
        if execution.status in NON_TERMINAL_STATUSES:
            outcome = self.accept_execution(
                prepared=self._prepared_from_case(case),
                execution=execution,
            )
            self._apply_tracker_result(
                execution,
                outcome.get("verification")
                if isinstance(outcome.get("verification"), dict)
                else {},
            )
            return {
                "status": "pending",
                "case": outcome.get("case"),
                "execution_result": self.public_execution_summary(
                    execution
                ),
            }
        prepared = self._prepared_from_case(case)
        outcome = self.accept_execution(
            prepared=prepared,
            execution=execution,
        )
        self._apply_tracker_result(
            execution,
            outcome.get("verification")
            if isinstance(outcome.get("verification"), dict)
            else {},
        )
        return {
            **outcome,
            "recovery_status": "reconciled",
        }

    def recover_pending(
        self,
        *,
        limit: int = 20,
        reason: str = "active_loop",
    ) -> dict[str, Any]:
        trace = self.case_store.flush_trace_outbox()
        document = self.state_store.read_json("durable_case_state.json")
        cases = (
            document.get("cases")
            if isinstance(document.get("cases"), dict)
            else {}
        )
        candidates: list[dict[str, Any]] = []
        for case in cases.values():
            if not isinstance(case, dict):
                continue
            status = str(case.get("status") or "")
            checkpoint = self._latest_checkpoint(case)
            if status == CaseStatus.CANCELLING.value:
                candidates.append(case)
            elif (
                status == CaseStatus.DELIBERATING.value
                and checkpoint.get("phase")
                in {
                    "agent_dispatch_prepared",
                    "agent_dispatched",
                }
            ):
                candidates.append(case)
        candidates.sort(
            key=lambda case: (
                str(case.get("updated_at") or ""),
                str(case.get("case_id") or ""),
            )
        )
        recovery_batch = self.case_store.rotate_recovery_candidates(
            candidates,
            limit=limit,
        )
        processed: list[dict[str, Any]] = []
        for case in recovery_batch:
            try:
                recovered = self.recover_case(
                    case_id=str(case["case_id"]),
                    user_id=str(case["scope"]["user_id"]),
                    workspace_id=str(case["scope"]["workspace_id"]),
                    expected_revision=int(case["revision"]),
                    operation_id=(
                        f"recover:{case['case_id']}:{case['revision']}"
                    ),
                    reason=reason,
                )
            except Exception as exc:
                recovered = {
                    "status": "degraded",
                    "case_id": case.get("case_id"),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            processed.append(recovered)
        return {
            "status": (
                "degraded"
                if any(
                    item.get("status")
                    in {
                        "degraded",
                        "recovery_degraded",
                        "cancellation_unconfirmed",
                    }
                    for item in processed
                )
                else "success"
            ),
            "processed_count": len(processed),
            "processed": [
                self.public_recovery_summary(item) for item in processed
            ],
            "trace_outbox": trace,
        }

    @staticmethod
    def _checkpoint(
        *,
        checkpoint_id: str,
        phase: str,
        operation_id: str,
        step_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
        binding_digest: str | None = None,
        executor: str | None = None,
        target_agent: str | None = None,
        dialogue_message_id: str | None = None,
        result_status: str | None = None,
        effect_state: CheckpointEffectState = (
            CheckpointEffectState.NOT_STARTED
        ),
        evidence_refs: list[str] | None = None,
    ) -> CaseCheckpoint:
        return CaseCheckpoint(
            checkpoint_id=checkpoint_id,
            phase=phase,
            operation_id=operation_id,
            step_id=step_id,
            task_id=task_id,
            run_id=run_id,
            session_key=session_key,
            binding_digest=binding_digest,
            executor=executor,
            target_agent=target_agent,
            dialogue_message_id=dialogue_message_id,
            result_status=result_status,
            effect_state=effect_state,
            evidence_refs=list(evidence_refs or []),
            recorded_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _dialogue_record(message: dict[str, Any]) -> DialogueRecord:
        sender = str(message.get("sender") or "")
        return DialogueRecord(
            message_id=str(message["message_id"]),
            message_type=DialogueMessageType(
                str(message["message_type"])
            ),
            sender=sender,
            direction=(
                "veyra_to_agent"
                if sender == "veyra"
                else "agent_to_veyra"
            ),
            case_revision=int(message["case_revision"]),
            turn_index=int(message["turn_index"]),
            in_reply_to=(
                str(message["in_reply_to"])
                if message.get("in_reply_to")
                else None
            ),
            content=json.loads(
                json.dumps(
                    message,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            recorded_at=datetime.now(timezone.utc),
        )

    def _prepared_from_case(self, case: dict[str, Any]) -> dict[str, Any]:
        requests = [
            item
            for item in case.get("dialogue", [])
            if isinstance(item, dict)
            and item.get("sender") == "veyra"
            and item.get("message_type")
            in {
                DialogueType.TASK_REQUEST.value,
                DialogueType.CONTEXT_PATCH.value,
            }
        ]
        if not requests:
            raise BoundedNegotiationError(
                "durable case has no persisted dispatch request"
            )
        record = requests[-1]
        request = (
            record.get("content")
            if isinstance(record.get("content"), dict)
            else {}
        )
        checkpoint = self._latest_checkpoint(case)
        binding = {
            "case_id": case["case_id"],
            "case_revision": request["case_revision"],
            "turn_index": request["turn_index"],
            "user_id": case["scope"]["user_id"],
            "workspace_id": case["scope"]["workspace_id"],
            "scope_digest": request["scope_digest"],
            "message_id": request["message_id"],
            "task_packet_id": request["task_packet_id"],
            "operation_id": request["operation_id"],
            "step_id": checkpoint.get("step_id"),
            "checkpoint_id": str(
                checkpoint.get("checkpoint_id") or "cp_recovered"
            ).split(":", 1)[0],
            "runtime_run_id": checkpoint.get("run_id"),
            "session_key": checkpoint.get("session_key"),
            "target_agent": (
                checkpoint.get("target_agent")
                or checkpoint.get("executor")
            ),
            "collaboration_binding": (
                dict(request["collaboration_binding"])
                if isinstance(
                    request.get("collaboration_binding"), dict
                )
                else None
            ),
        }
        self._require_binding(binding)
        return {
            "replayed": True,
            "case": case,
            "packet": None,
            "request": request,
            "binding": binding,
        }

    @staticmethod
    def _outcome(
        *,
        case: dict[str, Any],
        execution: ExecutionResult,
        dialogue: dict[str, Any] | None,
        verification: dict[str, Any],
        response: str,
    ) -> dict[str, Any]:
        return {
            "status": str(
                verification.get("status") or "partially_success"
            ),
            "case": BoundedNegotiationRuntime.public_case_summary(case),
            "execution_result": (
                BoundedNegotiationRuntime.public_execution_summary(
                    execution
                )
            ),
            "dialogue_message": (
                BoundedNegotiationRuntime.public_dialogue_message(dialogue)
                if isinstance(dialogue, dict)
                else None
            ),
            "verification": verification,
            "response": response,
        }

    @staticmethod
    def public_case_summary(case: dict[str, Any]) -> dict[str, Any]:
        dialogue = (
            case.get("dialogue")
            if isinstance(case.get("dialogue"), list)
            else []
        )
        latest_dialogue = next(
            (
                item
                for item in reversed(dialogue)
                if isinstance(item, dict)
            ),
            {},
        )
        checkpoints = (
            case.get("checkpoints")
            if isinstance(case.get("checkpoints"), list)
            else []
        )
        latest_checkpoint = next(
            (
                item
                for item in reversed(checkpoints)
                if isinstance(item, dict)
            ),
            {},
        )
        return {
            "case_id": case.get("case_id"),
            "case_type": case.get("case_type"),
            "status": case.get("status"),
            "revision": case.get("revision"),
            "priority": case.get("priority"),
            "source_event_id": case.get("source_event_id"),
            "updated_at": case.get("updated_at"),
            "operation_replayed": case.get("operation_replayed", False),
            "dialogue_count": len(dialogue),
            "checkpoint_count": len(checkpoints),
            "latest_dialogue": {
                "message_id": latest_dialogue.get("message_id"),
                "message_type": latest_dialogue.get("message_type"),
                "sender": latest_dialogue.get("sender"),
                "turn_index": latest_dialogue.get("turn_index"),
            },
            "latest_checkpoint": {
                "checkpoint_id": latest_checkpoint.get("checkpoint_id"),
                "phase": latest_checkpoint.get("phase"),
                "result_status": latest_checkpoint.get("result_status"),
                "effect_state": latest_checkpoint.get("effect_state"),
            },
        }

    @staticmethod
    def public_case_detail(case: dict[str, Any]) -> dict[str, Any]:
        """Return the user-visible replay without private run authority."""

        checkpoints = (
            case.get("checkpoints")
            if isinstance(case.get("checkpoints"), list)
            else []
        )
        dialogue = (
            case.get("dialogue")
            if isinstance(case.get("dialogue"), list)
            else []
        )
        return {
            **BoundedNegotiationRuntime.public_case_summary(case),
            "scope": (
                dict(case["scope"])
                if isinstance(case.get("scope"), dict)
                else {}
            ),
            "user_goal": case.get("user_goal"),
            "situation_id": case.get("situation_id"),
            "goal_ids": list(case.get("goal_ids") or []),
            "commitment_ids": list(
                case.get("commitment_ids") or []
            ),
            "paused_from_status": case.get("paused_from_status"),
            "next_wakeup_at": case.get("next_wakeup_at"),
            "created_at": case.get("created_at"),
            "checkpoints": [
                {
                    key: item.get(key)
                    for key in (
                        "schema_version",
                        "checkpoint_id",
                        "phase",
                        "step_id",
                        "dialogue_message_id",
                        "result_status",
                        "effect_state",
                        "evidence_refs",
                        "recorded_at",
                    )
                    if key in item
                }
                for item in checkpoints
                if isinstance(item, dict)
            ],
            "dialogue": [
                {
                    key: (
                        BoundedNegotiationRuntime._public_dialogue_content(
                            item
                        )
                        if key == "content"
                        else item.get(key)
                    )
                    for key in (
                        "schema_version",
                        "message_id",
                        "message_type",
                        "sender",
                        "direction",
                        "case_revision",
                        "turn_index",
                        "in_reply_to",
                        "content",
                        "authority_granted",
                        "evidence_verified",
                        "recorded_at",
                    )
                    if key in item
                }
                for item in dialogue
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def case_has_live_agent_authority(case: dict[str, Any]) -> bool:
        checkpoint = BoundedNegotiationRuntime._latest_checkpoint(case)
        return bool(
            str(case.get("status") or "")
            == CaseStatus.DELIBERATING.value
            and checkpoint.get("phase")
            in {"agent_dispatch_prepared", "agent_dispatched"}
        )

    @staticmethod
    def public_execution_summary(
        execution: ExecutionResult | dict[str, Any],
    ) -> dict[str, Any]:
        value = (
            execution.to_dict()
            if isinstance(execution, ExecutionResult)
            else execution
        )
        if not isinstance(value, dict):
            return {}
        return {
            key: value.get(key)
            for key in (
                "executor",
                "status",
            )
            if key in value
        }

    @staticmethod
    def public_dialogue_message(
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Expose proposal content without its private dispatch binding."""

        if not isinstance(message, dict):
            return {}
        message_type = str(message.get("message_type") or "")
        payload = (
            message.get("payload")
            if isinstance(message.get("payload"), dict)
            else {}
        )
        if message_type == DialogueType.TASK_REQUEST.value:
            payload = {
                key: json.loads(
                    json.dumps(
                        payload.get(key),
                        ensure_ascii=False,
                        default=str,
                    )
                )
                for key in (
                    "user_goal",
                    "constraints",
                    "evidence_refs",
                    "authority",
                )
                if key in payload
            }
        else:
            payload = json.loads(
                json.dumps(payload, ensure_ascii=False, default=str)
            )
        return {
            key: message.get(key)
            for key in (
                "contract_version",
                "message_id",
                "message_type",
                "case_id",
                "case_revision",
                "turn_index",
                "sender",
                "in_reply_to",
            )
            if key in message
        } | {"payload": payload}

    @staticmethod
    def public_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(trace, dict):
            return {}
        return {
            key: trace.get(key)
            for key in (
                "trace_id",
                "route",
                "status",
                "recorded_at",
            )
            if key in trace
        }

    @staticmethod
    def public_recovery_summary(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"status": "invalid_recovery_result"}
        output = {
            key: value.get(key)
            for key in (
                "status",
                "case_id",
                "error_type",
                "recovery_status",
                "reason",
            )
            if key in value
        }
        case = value.get("case")
        if isinstance(case, dict):
            output["case"] = (
                case
                if set(case).issubset(
                    set(
                        BoundedNegotiationRuntime.public_case_summary(
                            case
                        )
                    )
                )
                else BoundedNegotiationRuntime.public_case_summary(case)
            )
        execution = value.get("execution_result")
        if isinstance(execution, dict):
            output["execution_result"] = (
                BoundedNegotiationRuntime.public_execution_summary(
                    execution
                )
            )
        return output

    @staticmethod
    def _public_dialogue_content(item: dict[str, Any]) -> dict[str, Any]:
        content = (
            item.get("content")
            if isinstance(item.get("content"), dict)
            else {}
        )
        return BoundedNegotiationRuntime.public_dialogue_message(content)

    @staticmethod
    def _public_cancellation(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "status",
            "reason",
            "authority_revoked",
            "plugin_authority_closed",
            "agent_abort_confirmed",
            "receipt_replayed",
        }
        projected = {
            key: value.get(key)
            for key in sorted(allowed)
            if key in value
        }
        for source, target in (
            ("revoked_grants", "revoked_grant_count"),
            ("executing_reservations", "executing_reservation_count"),
            ("cancelled_reservations", "cancelled_reservation_count"),
        ):
            item = value.get(source)
            projected[target] = (
                len(item)
                if isinstance(item, (list, tuple, set, dict))
                else int(item)
                if isinstance(item, int) and not isinstance(item, bool)
                else 0
            )
        return projected

    @staticmethod
    def _pending_verification(status: str) -> dict[str, Any]:
        return {
            "status": "partially_success",
            "verdict": f"agent_dialogue_{status}_not_final",
            "confidence": 0.55,
            "evidence": {
                "proposal_is_evidence": False,
                "proposal_is_authority": False,
            },
            "next_action": "poll_bound_agent_run",
            "needs_rollback": False,
            "needs_memory_patch": False,
        }

    @staticmethod
    def _failed_execution_verification(status: str) -> dict[str, Any]:
        return {
            # Phase 4 runs are analysis/proposal-only. A provider transport or
            # dialogue failure is not authoritative evidence that the real
            # Situation failed, so it must remain evaluable.
            "status": "needs_more_probe",
            "verdict": "agent_dialogue_execution_not_successful",
            "confidence": 0.55,
            "evidence": {
                "execution_status": str(status or "unknown"),
                "proposal_is_evidence": False,
                "proposal_is_authority": False,
            },
            "next_action": "keep_case_evaluable_or_retry_deliberation",
            "needs_rollback": False,
            "needs_memory_patch": False,
        }

    @staticmethod
    def _fenced_verification(case_status: str) -> dict[str, Any]:
        return {
            # A late reply crossing a Case fence is rejected, not promoted to
            # a verified outcome for the surrounding Situation.
            "status": "needs_more_probe",
            "verdict": "case_not_accepting_agent_dialogue",
            "confidence": 0.55,
            "evidence": {
                "case_status": case_status,
                "proposal_is_evidence": False,
                "proposal_is_authority": False,
            },
            "next_action": "honor_case_lifecycle_fence",
            "needs_rollback": False,
            "needs_memory_patch": False,
        }

    @staticmethod
    def _latest_checkpoint(case: dict[str, Any]) -> dict[str, Any]:
        checkpoints = case.get("checkpoints")
        if not isinstance(checkpoints, list):
            return {}
        for item in reversed(checkpoints):
            if isinstance(item, dict):
                return item
        return {}

    @staticmethod
    def _find_checkpoint(
        case: dict[str, Any], checkpoint_id: str
    ) -> dict[str, Any]:
        checkpoints = case.get("checkpoints")
        if not isinstance(checkpoints, list):
            return {}
        for item in reversed(checkpoints):
            if (
                isinstance(item, dict)
                and item.get("checkpoint_id") == checkpoint_id
            ):
                return item
        return {}

    @staticmethod
    def _find_dialogue(
        case: dict[str, Any], message_id: str
    ) -> dict[str, Any]:
        dialogue = case.get("dialogue")
        if not isinstance(dialogue, list):
            return {}
        for item in reversed(dialogue):
            if (
                isinstance(item, dict)
                and item.get("message_id") == message_id
            ):
                return item
        return {}

    @staticmethod
    def _find_cancellation_request(
        case: dict[str, Any], operation_id: str
    ) -> dict[str, Any]:
        checkpoints = case.get("checkpoints")
        if not isinstance(checkpoints, list):
            return {}
        for item in reversed(checkpoints):
            if (
                isinstance(item, dict)
                and item.get("phase") == "cancellation_requested"
                and item.get("operation_id") == operation_id
            ):
                return item
        return {}

    @staticmethod
    def _assert_execution_binding(
        binding: dict[str, Any],
        execution: ExecutionResult,
    ) -> None:
        mismatched: list[str] = []
        if execution.task_id != str(binding["runtime_run_id"]):
            mismatched.append("runtime_run_id")
        if execution.executor != str(binding["target_agent"]):
            mismatched.append("target_agent")
        if mismatched:
            raise BoundedNegotiationError(
                "Agent execution does not match its Durable Case binding: "
                f"{mismatched}"
            )

    @staticmethod
    def _assert_checkpoint_execution(
        checkpoint: dict[str, Any],
        *,
        execution: ExecutionResult,
        binding: dict[str, Any],
    ) -> None:
        expected = {
            "task_id": binding["task_packet_id"],
            "run_id": execution.task_id,
            "executor": execution.executor,
            "target_agent": binding["target_agent"],
            "step_id": binding["step_id"],
        }
        mismatched = [
            key
            for key, value in expected.items()
            if str(checkpoint.get(key) or "") != str(value or "")
        ]
        if mismatched:
            raise BoundedNegotiationError(
                "replayed Agent execution conflicts with its checkpoint: "
                f"{mismatched}"
            )

    @staticmethod
    def _has_exact_run_observation(
        execution: ExecutionResult,
    ) -> bool:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        if raw.get("exact_run_observed") is True:
            return True
        chat_send = (
            raw.get("chat_send")
            if isinstance(raw.get("chat_send"), dict)
            else {}
        )
        final_event = (
            raw.get("final_event")
            if isinstance(raw.get("final_event"), dict)
            else {}
        )
        top_level_run_id = str(raw.get("run_id") or "").strip()
        chat_run_id = str(
            chat_send.get("runId")
            or chat_send.get("run_id")
            or ""
        ).strip()
        final_run_id = str(
            final_event.get("runId")
            or final_event.get("run_id")
            or ""
        ).strip()
        if (
            top_level_run_id == execution.task_id
            and chat_run_id == execution.task_id
            and (not final_run_id or final_run_id == execution.task_id)
            and str(final_event.get("state") or "").strip().lower()
            in {"final", "error"}
        ):
            for payload in (chat_send, final_event):
                if payload.get("providerStarted") is False:
                    return False
                if (
                    str(payload.get("status") or "").strip().lower()
                    == "timeout"
                    and str(
                        payload.get("timeoutPhase") or ""
                    ).strip().lower()
                    == "queue"
                ):
                    return False
            return True
        for key in ("agent_wait", "final_event", "task"):
            payload = raw.get(key)
            if not isinstance(payload, dict):
                continue
            observed_id = str(
                payload.get("runId")
                or payload.get("run_id")
                or payload.get("taskId")
                or payload.get("task_id")
                or payload.get("id")
                or ""
            ).strip()
            if observed_id != execution.task_id:
                continue
            if payload.get("providerStarted") is False:
                return False
            if (
                str(payload.get("status") or "").strip().lower()
                == "timeout"
                and str(
                    payload.get("timeoutPhase") or ""
                ).strip().lower()
                == "queue"
            ):
                return False
            if observed_id == execution.task_id:
                return True
        return False

    @staticmethod
    def _run_observation_failure_code(
        execution: ExecutionResult,
    ) -> str:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        governance = (
            raw.get("governance")
            if isinstance(raw.get("governance"), dict)
            else {}
        )
        failure_code = str(
            governance.get("failure_code") or ""
        ).strip()
        if (
            governance.get("status") == "registration_failed"
            and failure_code
        ):
            return failure_code
        if (
            governance.get("status")
            == "registration_identity_invalid"
        ):
            return "governance_registration_identity_invalid"
        if raw.get("reason") == "openclaw_governed_dispatch_not_verified":
            return "governance_preflight_blocked"
        if isinstance(raw.get("error"), dict):
            return "gateway_submission_failed"
        if execution.status in NON_TERMINAL_STATUSES:
            return "provider_run_not_terminal"
        return "provider_run_provenance_missing"

    def _adapter_for(self, executor: str) -> AgentAdapter:
        names = getattr(self.registry, "names", None)
        if callable(names):
            available = {str(item) for item in names()}
            if executor not in available:
                raise BoundedNegotiationError(
                    f"bound Agent runtime is unavailable: {executor}"
                )
        adapter = self.registry.get(executor)
        if not isinstance(adapter, AgentAdapter):
            # Test doubles and alternate adapters may use structural typing.
            if not all(
                callable(getattr(adapter, name, None))
                for name in (
                    "fetch_task_status",
                    "cancel_task_authority",
                )
            ):
                raise BoundedNegotiationError(
                    f"bound Agent runtime is invalid: {executor}"
                )
        return adapter

    def _apply_tracker_result(
        self,
        execution: ExecutionResult,
        verification: dict[str, Any],
    ) -> None:
        if self.task_tracker is None:
            return
        getter = getattr(self.task_tracker, "get_context", None)
        apply_result = getattr(self.task_tracker, "apply_result", None)
        if not callable(getter) or not callable(apply_result):
            return
        context = getter(execution.task_id, authoritative_only=True)
        if not isinstance(context, dict) or not context.get("case_id"):
            return
        apply_result(
            execution=execution,
            verification=verification,
            event_id=str(context.get("event_id") or ""),
            route=str(context.get("route") or "agent"),
        )

    def _retire_case_tasks(
        self,
        *,
        case_id: str,
        case_status: str,
    ) -> None:
        retire = getattr(
            self.task_tracker, "retire_case_tasks", None
        )
        if callable(retire):
            retire(case_id=case_id, case_status=case_status)

    def _workspace_id(self) -> str:
        workspace = str(
            self.state_store.read_json("local_world.json").get(
                "current_project"
            )
            or ""
        ).strip()
        if not workspace:
            raise BoundedNegotiationError(
                "local workspace identity is unavailable"
            )
        return workspace

    @staticmethod
    def _case_context(context_patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            key: value
            for key, value in context_patch.items()
            if key
            in {
                "attention_focus",
                "decision_trace",
                "foresight",
                "semantic_frame",
                "semantic_policy",
                "core_reasoning",
                "context_scope",
            }
        }
        encoded = json.dumps(
            allowed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded.encode("utf-8")) <= 8 * 1024:
            return allowed
        return {
            "summary": "bounded context omitted because it exceeded 8 KiB",
            "keys": sorted(allowed),
        }

    @staticmethod
    def _turn_identity(
        *, case_id: str, case_revision: int, turn_index: int
    ) -> dict[str, str]:
        digest = BoundedNegotiationRuntime._digest(
            {
                "namespace": "veyra.phase4.turn.v1",
                "case_id": case_id,
                "case_revision": case_revision,
                "turn_index": turn_index,
            }
        )
        return {
            "task_packet_id": f"task_case_{digest[:24]}",
            "runtime_run_id": f"veyra-case-{digest[:32]}",
            "step_id": f"case_step_{turn_index}_{digest[:16]}",
            "message_id": f"msg_{digest[:24]}",
            "operation_id": f"case_turn_{turn_index}_{digest[:24]}",
            "checkpoint_id": f"cp_{digest[:24]}",
        }

    @staticmethod
    def _require_binding(binding: dict[str, Any]) -> None:
        required = {
            "case_id",
            "case_revision",
            "turn_index",
            "user_id",
            "workspace_id",
            "scope_digest",
            "message_id",
            "task_packet_id",
            "operation_id",
            "step_id",
            "checkpoint_id",
            "runtime_run_id",
            "session_key",
            "target_agent",
        }
        missing = [
            key for key in sorted(required) if binding.get(key) in (None, "")
        ]
        if missing:
            raise BoundedNegotiationError(
                f"durable dialogue binding is incomplete: {missing}"
            )

    @staticmethod
    def _bounded_strings(
        values: Any,
        *,
        limit: int,
        item_limit: int,
    ) -> list[str]:
        if not isinstance(values, list):
            return []
        output: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in output:
                output.append(text[:item_limit])
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _execution_session_key(
        execution: ExecutionResult,
        *,
        fallback: str,
    ) -> str:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        task_context = (
            raw.get("task_context")
            if isinstance(raw.get("task_context"), dict)
            else {}
        )
        return str(
            task_context.get("agent_execution_session_id")
            or task_context.get("session_key")
            or fallback
        ).strip()

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()


__all__ = [
    "BoundedNegotiationError",
    "BoundedNegotiationRuntime",
]
