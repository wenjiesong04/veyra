from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core.durable_case import (
    CaseStatus,
    CheckpointEffectState,
)
from core.task_packet_builder import (
    PHASE6_READ_ONLY_COLLABORATION_PROFILE,
    TaskPacketBuilder,
)
from core.world_state import WorldStateStore
from interface.agent_adapter import ExecutionResult
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_dialogue_contract import (
    DialogueType,
    build_collaboration_binding,
    build_context_patch,
    build_plan_selection,
    build_task_request,
    collaboration_turn_transport_identity,
    parse_collaboration_binding,
    scope_digest,
    validate_collaboration_child,
)
from interface.event_schema import VeyraEvent
from runtime.agent_capability_directory import (
    AgentCapabilityDirectory,
    AgentCapabilitySelection,
    AgentCapabilitySelectionError,
)
from runtime.bounded_agent_negotiation import (
    BoundedNegotiationError,
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import DurableCaseStore
from runtime.durable_case_store import (
    CaseOperationConflictError,
    CaseRevisionConflictError,
)


COLLABORATION_STATE_FILE = "phase6_collaboration_state.json"
COLLABORATION_STATE_SCHEMA = "veyra.phase6.collaboration_state.v1"
COLLABORATION_PUBLIC_SCHEMA = "veyra.phase6.collaboration.v1"
COLLABORATION_STATUS_SCHEMA = "veyra.phase6.status.v1"
MAX_PARTICIPANTS = 2
MAX_HANDOFFS = 1
MAX_EVIDENCE_PATCHES = 1
MAX_AGENT_CALLS = 3
MAX_WALL_TIME_SECONDS = 300


class ReadOnlyCollaborationError(RuntimeError):
    """A Phase 6 collaboration could not continue inside its frozen scope."""


class CollaborationNotFoundError(ReadOnlyCollaborationError):
    pass


class CollaborationConflictError(ReadOnlyCollaborationError):
    pass


class CollaborationStorageError(ReadOnlyCollaborationError):
    pass


class ReadOnlyAgentCollaborationRuntime:
    """Sequential, proposal-only collaboration over a Durable Case.

    This first Phase 6 slice deliberately supports two logical participants on
    one exact certified runtime. It is not a provider portfolio and never has
    more than one live Agent authority at a time.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        case_store: DurableCaseStore,
        task_packet_builder: TaskPacketBuilder,
        bounded_negotiation: BoundedNegotiationRuntime,
        capability_directory: AgentCapabilityDirectory,
        task_tracker: Any,
    ) -> None:
        self.state_store = state_store
        self.case_store = case_store
        self.task_packet_builder = task_packet_builder
        self.bounded_negotiation = bounded_negotiation
        self.capability_directory = capability_directory
        self.task_tracker = task_tracker

    def status(self) -> dict[str, Any]:
        try:
            state = self._state()
            self._validate_state(state)
            collaborations = state.get("collaborations", {})
            counts: dict[str, int] = {}
            for value in collaborations.values():
                if not isinstance(value, dict):
                    continue
                status = str(value.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
            storage_status = "ready"
            issue = None
        except CollaborationStorageError:
            counts = {}
            storage_status = "degraded"
            issue = "collaboration_state_invalid"
        # Status is a pure projection. Explicit runtime selection remains the
        # only path allowed to perform a fresh provider observation.
        directory = self.capability_directory.snapshot(read_only=True)
        eligible = list(directory.get("eligible_runtimes") or [])
        return {
            "schema_version": COLLABORATION_STATUS_SCHEMA,
            "phase": "6.1",
            "status": (
                "technical_complete_read_only"
                if storage_status == "ready" and eligible
                else "validation_pending"
                if storage_status == "ready"
                else "degraded"
            ),
            "mode": "shadow_proposal_only",
            "topology": "single_runtime_multi_participant",
            "cross_provider_collaboration": "not_implemented",
            "execution_profile": (
                PHASE6_READ_ONLY_COLLABORATION_PROFILE
            ),
            "limits": {
                "participants": MAX_PARTICIPANTS,
                "handoffs": MAX_HANDOFFS,
                "evidence_patches": MAX_EVIDENCE_PATCHES,
                "agent_calls": MAX_AGENT_CALLS,
                "wall_time_seconds": MAX_WALL_TIME_SECONDS,
            },
            "authority": {
                "tools": [],
                "workspace_access": False,
                "memory_read": False,
                "memory_write": False,
                "side_effects": False,
                "execution_authorized": False,
                "verification_authority": False,
                "provider_switch_allowed": False,
            },
            "storage": {
                "status": storage_status,
                "issue": issue,
                "counts": counts,
            },
            "capability_directory": directory,
        }

    def start(
        self,
        *,
        event: VeyraEvent,
        workspace_id: str,
        runtime: str,
        operation_id: str,
        context_summary: str = "",
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        selected_workspace = self._required_text(
            workspace_id, "workspace_id", 240
        )
        self._require_current_workspace(selected_workspace)
        selected_operation = self._required_text(
            operation_id, "operation_id", 240
        )
        goal = self._required_text(
            event.payload.get("text"), "user_goal", 4_000
        )
        selected_context = self._optional_text(
            context_summary, "context_summary", 4_000
        )
        selected_evidence = self._canonical_ids(
            evidence_refs or [],
            field="evidence_refs",
            limit=32,
            item_limit=256,
        )
        selection = self.capability_directory.select_exact(runtime)
        admission = self.case_store.admit_event(
            event_id=event.event_id,
            user_id=event.source.user_id,
            workspace_id=selected_workspace,
            user_goal=goal,
            situation_id=None,
            case_type="phase6_read_only_agent_collaboration",
            operation_id=f"phase6:{selected_operation}:admit",
        )
        case_id = str(admission["case_id"])
        request_digest = self._digest(
            {
                "event_id": event.event_id,
                "user_id": event.source.user_id,
                "workspace_id": selected_workspace,
                "runtime": runtime,
                "operation_id": selected_operation,
                "goal": goal,
                "context_summary": selected_context,
                "evidence_refs": selected_evidence,
            }
        )
        graph, created = self._ensure_graph(
            case=admission,
            event=event,
            operation_id=selected_operation,
            runtime_selection=selection,
            request_digest=request_digest,
            context_summary=selected_context,
            evidence_refs=selected_evidence,
        )
        if not created:
            self._assert_selection_matches_graph(selection, graph)
            self._resume_qualified_root(
                event=event,
                graph=graph,
                selection=selection,
            )
            return self.get(
                case_id=case_id,
                user_id=event.source.user_id,
                workspace_id=selected_workspace,
            ) | {"operation_replayed": True}

        try:
            prepared = self._prepare_task_turn(
                case=admission,
                graph=graph,
                selection=selection,
                role="primary_analyst",
                parent_message=None,
                parent_binding=None,
            )
            self._dispatch_chain(
                event=event,
                selection=selection,
                prepared=prepared,
            )
        except Exception as exc:
            self._record_runtime_issue(
                case_id=case_id,
                reason=self._safe_error_reason(exc),
            )
            if isinstance(
                exc,
                (
                    AgentCapabilitySelectionError,
                    BoundedNegotiationError,
                    ReadOnlyCollaborationError,
                ),
            ):
                raise ReadOnlyCollaborationError(str(exc)) from exc
            raise
        return self.get(
            case_id=case_id,
            user_id=event.source.user_id,
            workspace_id=selected_workspace,
        )

    def advance(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        resolved_operation_id = self._required_text(
            operation_id, "operation_id", 240
        )
        graph = self._graph(case_id, user_id, workspace_id)
        selection = self.capability_directory.select_exact(
            str(graph["runtime"])
        )
        self._assert_selection_matches_graph(selection, graph)
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if (
            str(case.get("status") or "")
            == CaseStatus.QUALIFIED.value
            and not graph.get("participants")
            and not graph.get("dispatches")
        ):
            self._resume_qualified_root(
                event=self._event_from_graph(graph),
                graph=graph,
                selection=selection,
            )
            return self.get(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        if self.bounded_negotiation.case_has_live_agent_authority(case):
            recovered = self.bounded_negotiation.recover_case(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                expected_revision=int(case["revision"]),
                operation_id=resolved_operation_id,
                reason="phase6_explicit_advance",
            )
            self._sync_graph_from_case(case_id)
            recovered_case = recovered.get("case")
            if isinstance(recovered_case, dict):
                current_status = str(
                    recovered_case.get("status") or ""
                )
            else:
                current_status = ""
            if current_status == CaseStatus.DELIBERATING.value:
                return self.get(
                    case_id=case_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )

        self._continue_if_possible(
            event=self._event_from_graph(graph),
            selection=selection,
            case_id=case_id,
        )
        return self.get(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    def select_plan(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        selected_option_id: str,
        decision_reason: str,
    ) -> dict[str, Any]:
        graph = self._graph(case_id, user_id, workspace_id)
        selected_operation = self._required_text(
            operation_id, "operation_id", 240
        )
        option = self._required_text(
            selected_option_id, "selected_option_id", 128
        )
        selected_reason = self._required_text(
            decision_reason, "decision_reason", 1_200
        )
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        option_record = self._latest_dialogue(
            case, DialogueType.OPTION_SET.value, sender="agent"
        )
        option_message = self._content(option_record)
        options = (
            option_message.get("payload", {}).get("options", [])
            if isinstance(option_message.get("payload"), dict)
            else []
        )
        option_ids = {
            str(item.get("option_id") or "")
            for item in options
            if isinstance(item, dict)
        }
        if option not in option_ids:
            raise CollaborationConflictError(
                "selected option is not in the latest bound OPTION_SET"
            )
        binding = option_message.get("collaboration_binding")
        if not isinstance(binding, dict):
            raise CollaborationConflictError(
                "latest option set has no collaboration binding"
            )
        parent_binding = parse_collaboration_binding(binding)
        selection_budget = dict(binding["budget"])
        selection_budget["remaining_agent_calls"] = max(
            0,
            int(selection_budget["remaining_agent_calls"]) - 1,
        )
        selection_binding = build_collaboration_binding(
            participant_id=str(binding["participant_id"]),
            role=str(binding["role"]),  # type: ignore[arg-type]
            parent_participant_id=binding.get(
                "parent_participant_id"
            ),
            handoff_index=int(binding["handoff_index"]),
            capability_scope=list(binding["capability_scope"]),
            evidence_scope=list(binding["evidence_scope"]),
            provider=dict(binding["provider"]),
            budget=selection_budget,
            issued_at=parent_binding.issued_at,
            expires_at=parent_binding.expires_at,
            privacy=dict(binding["privacy"]),
            effect=dict(binding["effect"]),
        )
        digest = self._digest(
            {
                "namespace": "veyra.phase6.plan_selection.v1",
                "case_id": case_id,
                "case_revision": expected_revision,
                "operation_id": selected_operation,
                "option_set_message_id": option_message["message_id"],
                "selected_option_id": option,
                "decision_reason": selected_reason,
            }
        )
        selection_message = build_plan_selection(
            case_id=case_id,
            case_revision=expected_revision,
            turn_index=int(option_message["turn_index"]) + 1,
            message_id=f"p6plan_{digest[:24]}",
            task_packet_id=f"p6planpacket_{digest[:24]}",
            operation_id=f"p6planop_{digest[:24]}",
            scope_digest=str(
                scope_digest(
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
            ),
            in_reply_to=str(option_message["message_id"]),
            collaboration_binding=selection_binding,
            option_set_message_id=str(option_message["message_id"]),
            selected_option_id=option,
            decision_reason=(
                selected_reason
            ),
            decision_evidence_refs=[],
            rejected_option_ids=sorted(option_ids - {option}),
        )
        closed = self.case_store.transition(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=selected_operation,
            expected_revision=expected_revision,
            to_status=CaseStatus.CLOSED,
            reason=(
                "human selected and closed a proposal without execution"
            ),
            dialogue_message=(
                self.bounded_negotiation._dialogue_record(
                    selection_message
                )
            ),
        )
        return self.get(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        ) | {
            "operation_replayed": bool(
                closed.get("operation_replayed")
            )
        }

    def cancel(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self._graph(case_id, user_id, workspace_id)
        result = self.bounded_negotiation.cancel_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
            reason=reason,
        )
        case = result.get("case") if isinstance(result, dict) else {}
        status = str(case.get("status") or "INDETERMINATE")
        self._update_graph(
            case_id,
            lambda value: value.update(
                {
                    "status": status,
                    "case_revision": int(
                        case.get("revision")
                        or value.get("case_revision")
                        or 1
                    ),
                    "updated_at": self._now(),
                }
            ),
        )
        return self.get(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    def get(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        self._graph(case_id, user_id, workspace_id)
        for _attempt in range(4):
            self._sync_graph_from_case(case_id)
            graph = self._graph(case_id, user_id, workspace_id)
            case = self.case_store.get_case(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            graph_revision = graph.get("case_revision")
            case_revision = case.get("revision")
            if (
                not isinstance(graph_revision, bool)
                and isinstance(graph_revision, int)
                and not isinstance(case_revision, bool)
                and isinstance(case_revision, int)
                and graph_revision == case_revision
            ):
                return self._public(graph, case)
        raise CollaborationConflictError(
            "collaboration projection changed during read"
        )

    def reconcile_case_projection(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Refresh the Phase 6 graph after callback/poll Case transitions."""

        self._graph(case_id, user_id, workspace_id)
        self._sync_graph_from_case(case_id)
        return self.get(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        state = self._state()
        self._validate_state(state)
        owned_case_ids = [
            str(graph.get("case_id") or "")
            for graph in state["collaborations"].values()
            if isinstance(graph, dict)
            and graph.get("user_id") == user_id
            and graph.get("workspace_id") == workspace_id
            and str(graph.get("case_id") or "")
        ]
        for case_id in owned_case_ids:
            self._sync_graph_from_case(case_id)
        state = self._state()
        self._validate_state(state)
        selected: list[dict[str, Any]] = []
        for graph in state["collaborations"].values():
            if not isinstance(graph, dict):
                continue
            if (
                graph.get("user_id") != user_id
                or graph.get("workspace_id") != workspace_id
            ):
                continue
            selected.append(self._public_graph(graph))
        selected.sort(
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        return {
            "schema_version": "veyra.phase6.collaboration_list.v1",
            "count": min(len(selected), max(1, min(limit, 100))),
            "collaborations": selected[: max(1, min(limit, 100))],
        }

    def _dispatch_chain(
        self,
        *,
        event: VeyraEvent,
        selection: AgentCapabilitySelection,
        prepared: dict[str, Any],
    ) -> None:
        current = prepared
        for _index in range(MAX_AGENT_CALLS):
            outcome = self._dispatch_one(
                event=event,
                selection=selection,
                prepared=current,
            )
            if outcome.get("terminal") is not True:
                return
            next_prepared = self._next_turn(
                selection=selection,
                case_id=str(prepared["binding"]["case_id"]),
            )
            if next_prepared is None:
                return
            current = next_prepared

    def _resume_qualified_root(
        self,
        *,
        event: VeyraEvent,
        graph: dict[str, Any],
        selection: AgentCapabilitySelection,
    ) -> bool:
        case_id = str(graph["case_id"])
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=str(graph["user_id"]),
            workspace_id=str(graph["workspace_id"]),
        )
        if (
            str(case.get("status") or "")
            != CaseStatus.QUALIFIED.value
            or graph.get("participants")
            or graph.get("dispatches")
        ):
            return False
        try:
            prepared = self._prepare_task_turn(
                case=case,
                graph=graph,
                selection=selection,
                role="primary_analyst",
                parent_message=None,
                parent_binding=None,
            )
        except (
            CaseOperationConflictError,
            CaseRevisionConflictError,
        ):
            current = self.case_store.get_case(
                case_id=case_id,
                user_id=str(graph["user_id"]),
                workspace_id=str(graph["workspace_id"]),
            )
            if (
                str(current.get("status") or "")
                != CaseStatus.QUALIFIED.value
            ):
                return False
            raise
        self._dispatch_chain(
            event=event,
            selection=selection,
            prepared=prepared,
        )
        return True

    def _dispatch_one(
        self,
        *,
        event: VeyraEvent,
        selection: AgentCapabilitySelection,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        binding = dict(prepared["binding"])
        self._assert_selection_matches_graph(
            selection,
            self._graph_unscoped(str(binding["case_id"])),
        )
        self._claim_dispatch(
            case_id=str(binding["case_id"]),
            participant_id=str(
                binding["collaboration_binding"]["participant_id"]
            ),
            task_packet_id=str(binding["task_packet_id"]),
        )
        try:
            execution = self.capability_directory.dispatch(
                selection,
                prepared["packet"],
            )
        except AgentCapabilitySelectionError as exc:
            self._mark_pre_dispatch_blocked(
                binding=binding,
                reason=exc.reason,
            )
            raise
        if not isinstance(execution, ExecutionResult):
            self._mark_pre_dispatch_blocked(
                binding=binding,
                reason="adapter_returned_invalid_execution",
            )
            raise ReadOnlyCollaborationError(
                "adapter returned an invalid execution result"
            )
        pending_verification = (
            self.bounded_negotiation._pending_verification(
                execution.status
            )
            if execution.status in NON_TERMINAL_STATUSES
            else {
                "status": "partially_success",
                "verdict": "phase6_terminal_result_pending_acceptance",
                "next_action": "accept_exact_bound_dialogue",
            }
        )
        self.task_tracker.register(
            event_id=event.event_id,
            route="phase6_collaboration",
            execution=execution,
            verification=pending_verification,
            session_id=event.source.session_id,
            channel=event.source.channel,
            user_id=event.source.user_id,
            correlation_id=event.event_id,
            task_packet_id=str(binding["task_packet_id"]),
            agent_execution_session_id=str(binding["session_key"]),
            agent_session_policy="ephemeral_per_task",
            memory_policy="forget",
            verification_policy=dict(
                prepared["packet"].verification_policy
            ),
            rollback_requirement=dict(
                prepared["packet"].rollback_requirement
            ),
            user_goal=str(prepared["packet"].user_goal),
            case_id=str(binding["case_id"]),
            case_workspace_id=str(binding["workspace_id"]),
            case_step_id=str(binding["step_id"]),
            case_operation_id=str(binding["operation_id"]),
            case_revision=int(binding["case_revision"]),
            dialogue_message_id=str(binding["message_id"]),
            target_agent=selection.runtime,
            force_pending=execution.status in NON_TERMINAL_STATUSES,
        )
        try:
            outcome = self.bounded_negotiation.accept_execution(
                prepared=prepared,
                execution=execution,
            )
        except BoundedNegotiationError:
            self._record_result(
                case_id=str(binding["case_id"]),
                participant_id=str(
                    binding["collaboration_binding"]["participant_id"]
                ),
                execution=execution,
                dialogue=None,
                case_status=CaseStatus.DELIBERATING.value,
                issue="exact_result_requires_reconciliation",
            )
            raise

        effects_reported = self._reported_effects(execution)
        authoritative_effects = self._authoritative_effects(execution)
        if effects_reported or authoritative_effects:
            self._fence_effect_violation(
                binding=binding,
                execution=execution,
            )
            return {"terminal": True, "fenced": True}
        dialogue = (
            outcome.get("dialogue_message")
            if isinstance(outcome.get("dialogue_message"), dict)
            else None
        )
        case_summary = (
            outcome.get("case")
            if isinstance(outcome.get("case"), dict)
            else {}
        )
        self._record_result(
            case_id=str(binding["case_id"]),
            participant_id=str(
                binding["collaboration_binding"]["participant_id"]
            ),
            execution=execution,
            dialogue=dialogue,
            case_status=str(
                case_summary.get("status") or CaseStatus.PAUSED.value
            ),
            issue=None,
        )
        return {
            "terminal": execution.status not in NON_TERMINAL_STATUSES,
            "dialogue": dialogue,
        }

    def _continue_if_possible(
        self,
        *,
        event: VeyraEvent,
        selection: AgentCapabilitySelection,
        case_id: str,
    ) -> None:
        prepared = self._next_turn(
            selection=selection,
            case_id=case_id,
        )
        if prepared is not None:
            self._dispatch_chain(
                event=event,
                selection=selection,
                prepared=prepared,
            )

    def _next_turn(
        self,
        *,
        selection: AgentCapabilitySelection,
        case_id: str,
    ) -> dict[str, Any] | None:
        graph = self._graph_unscoped(case_id)
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=str(graph["user_id"]),
            workspace_id=str(graph["workspace_id"]),
        )
        if self.bounded_negotiation.case_has_live_agent_authority(case):
            return None
        latest_agent = self._latest_agent_dialogue(case)
        if not latest_agent:
            return None
        content = self._content(latest_agent)
        message_type = str(content.get("message_type") or "")
        binding = content.get("collaboration_binding")
        if not isinstance(binding, dict):
            return None
        role = str(binding.get("role") or "")
        if (
            message_type == DialogueType.EVIDENCE_REQUEST.value
            and int(graph["budgets"]["evidence_patches_claimed"])
            < MAX_EVIDENCE_PATCHES
            and int(graph["budgets"]["agent_calls_claimed"])
            < MAX_AGENT_CALLS
        ):
            return self._prepare_unresolved_context_patch(
                case=case,
                graph=graph,
                selection=selection,
                parent_message=content,
                parent_binding=binding,
            )
        if (
            message_type == DialogueType.OPTION_SET.value
            and role == "primary_analyst"
            and int(graph["budgets"]["handoffs_claimed"]) < MAX_HANDOFFS
            and len(graph.get("participants") or []) < MAX_PARTICIPANTS
            and int(graph["budgets"]["agent_calls_claimed"])
            < MAX_AGENT_CALLS
        ):
            return self._prepare_task_turn(
                case=case,
                graph=graph,
                selection=selection,
                role="critic",
                parent_message=content,
                parent_binding=binding,
            )
        return None

    def _prepare_task_turn(
        self,
        *,
        case: dict[str, Any],
        graph: dict[str, Any],
        selection: AgentCapabilitySelection,
        role: str,
        parent_message: dict[str, Any] | None,
        parent_binding: dict[str, Any] | None,
    ) -> dict[str, Any]:
        turn_index = self._next_turn_index(case)
        identity = self._turn_identity(
            case_id=str(case["case_id"]),
            case_revision=int(case["revision"]),
            turn_index=turn_index,
            role=role,
        )
        if role == "primary_analyst":
            participant_id = f"p6participant_primary_{identity['digest'][:12]}"
            handoff_index = 0
            parent_participant_id = None
            remaining_calls = MAX_AGENT_CALLS
            remaining_handoffs = MAX_HANDOFFS
            remaining_patches = MAX_EVIDENCE_PATCHES
            parent_message_id = None
        else:
            if parent_message is None or parent_binding is None:
                raise ReadOnlyCollaborationError(
                    "critic handoff requires an exact parent proposal"
                )
            participant_id = f"p6participant_critic_{identity['digest'][:12]}"
            handoff_index = 1
            parent_participant_id = str(
                parent_binding["participant_id"]
            )
            remaining_calls = max(
                0,
                int(
                    parent_binding["budget"][
                        "remaining_agent_calls"
                    ]
                )
                - 1,
            )
            remaining_handoffs = 0
            remaining_patches = min(
                int(
                    parent_binding["budget"][
                        "remaining_evidence_patches"
                    ]
                ),
                max(
                    0,
                    MAX_EVIDENCE_PATCHES
                    - int(
                        graph["budgets"][
                            "evidence_patches_claimed"
                        ]
                    ),
                ),
            )
            parent_message_id = str(parent_message["message_id"])
        expires_at = self._expiry(graph)
        issued_at = datetime.now(timezone.utc)
        collaboration_binding = build_collaboration_binding(
            participant_id=participant_id,
            role=role,  # type: ignore[arg-type]
            parent_participant_id=parent_participant_id,
            handoff_index=handoff_index,
            capability_scope=[],
            evidence_scope=list(graph["evidence_refs"]),
            provider=selection.provider_binding,
            budget={
                "remaining_agent_calls": remaining_calls,
                "remaining_handoffs": remaining_handoffs,
                "remaining_evidence_patches": remaining_patches,
                "max_wall_time_seconds": MAX_WALL_TIME_SECONDS,
                "max_context_bytes": 8 * 1024,
                "max_output_bytes": 24 * 1024,
            },
            issued_at=issued_at,
            expires_at=expires_at,
        )
        if parent_binding is not None:
            errors = validate_collaboration_child(
                parent=parent_binding,
                child=collaboration_binding,
                outbound_type=DialogueType.TASK_REQUEST,
            )
            if errors:
                raise ReadOnlyCollaborationError(
                    "critic scope is not a strict child: "
                    + "; ".join(errors)
                )
        context = {
            "collaboration_mode": "read_only_proposal_only",
            "participant_role": role,
            "reply_policy": (
                "return one strict OPTION_SET, CHALLENGE, or "
                "EVIDENCE_REQUEST envelope"
            ),
            "context_summary": str(graph.get("context_summary") or ""),
        }
        if role == "critic" and parent_message is not None:
            context["peer_proposal"] = (
                self.bounded_negotiation.public_dialogue_message(
                    parent_message
                ).get("payload")
            )
            context["critic_instruction"] = (
                "independently challenge unsupported assumptions and return "
                "a bounded replacement option set only when useful"
            )
        request = build_task_request(
            case_id=str(case["case_id"]),
            case_revision=int(case["revision"]),
            turn_index=turn_index,
            message_id=identity["message_id"],
            task_packet_id=identity["task_packet_id"],
            operation_id=identity["operation_id"],
            scope_digest=str(
                scope_digest(
                    user_id=str(graph["user_id"]),
                    workspace_id=str(graph["workspace_id"]),
                )
            ),
            user_goal=str(graph["user_goal"]),
            constraints=[
                "analysis and proposals only",
                "do not call tools, read memory, or access a workspace",
                "do not claim execution, authorization, or verification",
                "do not create another participant or change provider",
                "use only supplied evidence_refs; omit claim_ref when none exists",
                "return exactly one strict dialogue envelope",
            ],
            evidence_refs=list(graph["evidence_refs"]),
            context=context,
            authority={
                "mode": "read_only",
                "side_effects_require_governance": True,
                "capability_expansion_authorized": False,
                "verification_authority": False,
            },
            collaboration_binding=collaboration_binding,
            in_reply_to=parent_message_id,
        )
        return self._persist_prepared_turn(
            case=case,
            graph=graph,
            selection=selection,
            identity=identity,
            request=request,
            collaboration_binding=collaboration_binding,
            role=role,
            parent_participant_id=parent_participant_id,
            handoff_index=handoff_index,
        )

    def _prepare_unresolved_context_patch(
        self,
        *,
        case: dict[str, Any],
        graph: dict[str, Any],
        selection: AgentCapabilitySelection,
        parent_message: dict[str, Any],
        parent_binding: dict[str, Any],
    ) -> dict[str, Any]:
        requested = (
            parent_message.get("payload", {}).get(
                "requested_evidence", []
            )
            if isinstance(parent_message.get("payload"), dict)
            else []
        )
        request_ids = [
            self._evidence_request_id(item, index)
            for index, item in enumerate(requested)
            if isinstance(item, dict)
        ]
        if not request_ids:
            return None  # type: ignore[return-value]
        turn_index = self._next_turn_index(case)
        identity = self._turn_identity(
            case_id=str(case["case_id"]),
            case_revision=int(case["revision"]),
            turn_index=turn_index,
            role=str(parent_binding["role"]),
        )
        child_budget = dict(parent_binding["budget"])
        child_budget["remaining_agent_calls"] = max(
            0, int(child_budget["remaining_agent_calls"]) - 1
        )
        child_budget["remaining_evidence_patches"] = max(
            0,
            int(child_budget["remaining_evidence_patches"]) - 1,
        )
        collaboration_binding = build_collaboration_binding(
            participant_id=str(parent_binding["participant_id"]),
            role=str(parent_binding["role"]),  # type: ignore[arg-type]
            parent_participant_id=parent_binding.get(
                "parent_participant_id"
            ),
            handoff_index=int(parent_binding["handoff_index"]),
            capability_scope=list(parent_binding["capability_scope"]),
            evidence_scope=list(parent_binding["evidence_scope"]),
            provider=dict(parent_binding["provider"]),
            budget=child_budget,
            issued_at=datetime.now(timezone.utc),
            expires_at=self._expiry(graph),
            privacy=dict(parent_binding["privacy"]),
            effect=dict(parent_binding["effect"]),
        )
        errors = validate_collaboration_child(
            parent=parent_binding,
            child=collaboration_binding,
            outbound_type=DialogueType.CONTEXT_PATCH,
            provided_evidence_refs=[],
        )
        if errors:
            raise ReadOnlyCollaborationError(
                "context patch scope is invalid: " + "; ".join(errors)
            )
        patch = build_context_patch(
            case_id=str(case["case_id"]),
            case_revision=int(case["revision"]),
            turn_index=turn_index,
            message_id=identity["message_id"],
            task_packet_id=identity["task_packet_id"],
            operation_id=identity["operation_id"],
            scope_digest=str(
                scope_digest(
                    user_id=str(graph["user_id"]),
                    workspace_id=str(graph["workspace_id"]),
                )
            ),
            in_reply_to=str(parent_message["message_id"]),
            collaboration_binding=collaboration_binding,
            resolved_request_ids=[],
            evidence_refs=[],
            unresolved_request_ids=request_ids,
            context={
                "status": "no_additional_verified_evidence_available",
                "instruction": (
                    "keep these claims unresolved and bound any proposal "
                    "to explicit assumptions"
                ),
            },
        )
        prepared = self._persist_prepared_turn(
            case=case,
            graph=graph,
            selection=selection,
            identity=identity,
            request=patch,
            collaboration_binding=collaboration_binding,
            role=str(parent_binding["role"]),
            parent_participant_id=parent_binding.get(
                "parent_participant_id"
            ),
            handoff_index=int(parent_binding["handoff_index"]),
        )
        self._update_graph(
            str(case["case_id"]),
            lambda value: value["budgets"].update(
                {
                    "evidence_patches_claimed": int(
                        value["budgets"][
                            "evidence_patches_claimed"
                        ]
                    )
                    + 1
                }
            ),
        )
        return prepared

    def _persist_prepared_turn(
        self,
        *,
        case: dict[str, Any],
        graph: dict[str, Any],
        selection: AgentCapabilitySelection,
        identity: dict[str, str],
        request: dict[str, Any],
        collaboration_binding: dict[str, Any],
        role: str,
        parent_participant_id: str | None,
        handoff_index: int,
    ) -> dict[str, Any]:
        transport_identity = collaboration_turn_transport_identity(
            request
        )
        session_key = transport_identity[
            "agent_execution_session_id"
        ]
        checkpoint = self.bounded_negotiation._checkpoint(
            checkpoint_id=identity["checkpoint_id"],
            phase="agent_dispatch_prepared",
            operation_id=identity["operation_id"],
            step_id=identity["step_id"],
            task_id=identity["task_packet_id"],
            run_id=identity["runtime_run_id"],
            session_key=session_key,
            executor=selection.runtime,
            target_agent=selection.runtime,
            dialogue_message_id=identity["message_id"],
            result_status="prepared",
            effect_state=CheckpointEffectState.NOT_STARTED,
        )
        started = self.case_store.transition(
            case_id=str(case["case_id"]),
            user_id=str(graph["user_id"]),
            workspace_id=str(graph["workspace_id"]),
            operation_id=f"{identity['operation_id']}:persist",
            expected_revision=int(case["revision"]),
            to_status=CaseStatus.DELIBERATING,
            reason="Phase 6 read-only participant prepared",
            checkpoint=checkpoint,
            dialogue_message=(
                self.bounded_negotiation._dialogue_record(request)
            ),
        )
        packet = self.task_packet_builder.build(
            event=self._event_from_graph(graph),
            target_agent=selection.runtime,
            context_patch=dict(
                request.get("payload", {}).get("context", {})
            ),
            persona_patch={},
            policy_patch={},
            required_capabilities=[],
            memory_policy="forget",
            agent_execution_session_id=session_key,
            agent_session_policy="ephemeral_per_task",
            task_id=identity["task_packet_id"],
            case_id=str(case["case_id"]),
            step_id=identity["step_id"],
            runtime_run_id=identity["runtime_run_id"],
            dialogue_message=request,
            execution_profile=(
                PHASE6_READ_ONLY_COLLABORATION_PROFILE
            ),
        )
        self._record_participant_prepared(
            case_id=str(case["case_id"]),
            participant_id=str(
                collaboration_binding["participant_id"]
            ),
            role=role,
            parent_participant_id=parent_participant_id,
            handoff_index=handoff_index,
            message_type=str(request["message_type"]),
            message_id=str(request["message_id"]),
            task_packet_id=str(request["task_packet_id"]),
            case_revision=int(started["revision"]),
        )
        return {
            "replayed": False,
            "case": started,
            "packet": packet,
            "request": request,
            "binding": {
                **{
                    key: value
                    for key, value in identity.items()
                    if key != "digest"
                },
                "case_id": str(case["case_id"]),
                "case_revision": int(request["case_revision"]),
                "turn_index": int(request["turn_index"]),
                "user_id": str(graph["user_id"]),
                "workspace_id": str(graph["workspace_id"]),
                "scope_digest": str(request["scope_digest"]),
                "session_key": session_key,
                "target_agent": selection.runtime,
                "collaboration_binding": collaboration_binding,
            },
        }

    def _ensure_graph(
        self,
        *,
        case: dict[str, Any],
        event: VeyraEvent,
        operation_id: str,
        runtime_selection: AgentCapabilitySelection,
        request_digest: str,
        context_summary: str,
        evidence_refs: list[str],
    ) -> tuple[dict[str, Any], bool]:
        case_id = str(case["case_id"])
        created = False
        selected: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=MAX_WALL_TIME_SECONDS)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal created, selected
            self._validate_state(state)
            event_index = state["event_index"]
            indexed = event_index.get(event.event_id)
            if indexed is not None and indexed != case_id:
                raise CollaborationConflictError(
                    "event is already bound to another collaboration"
                )
            existing = state["collaborations"].get(case_id)
            if isinstance(existing, dict):
                if (
                    existing.get("request_digest") != request_digest
                    or existing.get("user_id") != event.source.user_id
                    or existing.get("workspace_id")
                    != case["scope"]["workspace_id"]
                ):
                    raise CollaborationConflictError(
                        "collaboration replay conflicts with persisted scope"
                    )
                selected = dict(existing)
                return state
            graph = {
                "schema_version": "veyra.phase6.collaboration_record.v1",
                "case_id": case_id,
                "source_event_id": event.event_id,
                "operation_id": operation_id,
                "request_digest": request_digest,
                "user_id": event.source.user_id,
                "workspace_id": case["scope"]["workspace_id"],
                "channel": event.source.channel,
                "dialogue_session_id": event.source.session_id,
                "runtime": runtime_selection.runtime,
                "provider_binding": dict(
                    runtime_selection.provider_binding
                ),
                "provider_certification_digest": (
                    runtime_selection.certification_digest
                ),
                "execution_profile": (
                    PHASE6_READ_ONLY_COLLABORATION_PROFILE
                ),
                "topology": "single_runtime_multi_participant",
                "status": str(case["status"]),
                "case_revision": int(case["revision"]),
                "user_goal": str(
                    event.payload.get("text") or ""
                )[:4_000],
                "context_summary": context_summary,
                "evidence_refs": evidence_refs,
                "budgets": {
                    "agent_calls_max": MAX_AGENT_CALLS,
                    "agent_calls_claimed": 0,
                    "participants_max": MAX_PARTICIPANTS,
                    "handoffs_max": MAX_HANDOFFS,
                    "handoffs_claimed": 0,
                    "evidence_patches_max": MAX_EVIDENCE_PATCHES,
                    "evidence_patches_claimed": 0,
                },
                "participants": [],
                "dispatches": {},
                "selection": None,
                "effect_status": "not_started",
                "verification_status": "unverified",
                "issue": None,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            state["collaborations"][case_id] = graph
            event_index[event.event_id] = case_id
            state["collaboration_count"] = len(
                state["collaborations"]
            )
            state["updated_at"] = now.isoformat()
            selected = dict(graph)
            created = True
            return state

        self.state_store.mutate_json(COLLABORATION_STATE_FILE, mutate)
        return selected, created

    def _claim_dispatch(
        self,
        *,
        case_id: str,
        participant_id: str,
        task_packet_id: str,
    ) -> None:
        def update(value: dict[str, Any]) -> None:
            budgets = value["budgets"]
            dispatches = value["dispatches"]
            existing = dispatches.get(task_packet_id)
            if isinstance(existing, dict):
                if existing.get("participant_id") != participant_id:
                    raise CollaborationConflictError(
                        "task packet is bound to another participant"
                    )
                return
            claimed = int(budgets["agent_calls_claimed"])
            if claimed >= int(budgets["agent_calls_max"]):
                raise CollaborationConflictError(
                    "Agent-call budget is exhausted"
                )
            dispatches[task_packet_id] = {
                "participant_id": participant_id,
                "status": "claimed_before_dispatch",
                "claimed_at": self._now(),
                "dialogue_type": self._participant_field(
                    value, participant_id, "last_outbound_type"
                ),
            }
            budgets["agent_calls_claimed"] = claimed + 1
            value["status"] = CaseStatus.DELIBERATING.value
            value["updated_at"] = self._now()

        self._update_graph(case_id, update)

    def _record_participant_prepared(
        self,
        *,
        case_id: str,
        participant_id: str,
        role: str,
        parent_participant_id: str | None,
        handoff_index: int,
        message_type: str,
        message_id: str,
        task_packet_id: str,
        case_revision: int,
    ) -> None:
        def update(value: dict[str, Any]) -> None:
            participants = value["participants"]
            participant = next(
                (
                    item
                    for item in participants
                    if isinstance(item, dict)
                    and item.get("participant_id") == participant_id
                ),
                None,
            )
            if participant is None:
                if len(participants) >= MAX_PARTICIPANTS:
                    raise CollaborationConflictError(
                        "participant budget is exhausted"
                    )
                participant = {
                    "participant_id": participant_id,
                    "role": role,
                    "parent_participant_id": parent_participant_id,
                    "handoff_index": handoff_index,
                    "status": "prepared",
                    "dispatch_count": 0,
                    "last_outbound_type": message_type,
                    "last_outbound_message_id": message_id,
                    "last_reply_type": None,
                    "last_reply_message_id": None,
                }
                participants.append(participant)
                if handoff_index > 0:
                    value["budgets"]["handoffs_claimed"] = (
                        int(
                            value["budgets"]["handoffs_claimed"]
                        )
                        + 1
                    )
            else:
                if (
                    participant.get("role") != role
                    or participant.get("parent_participant_id")
                    != parent_participant_id
                    or int(participant.get("handoff_index") or 0)
                    != handoff_index
                ):
                    raise CollaborationConflictError(
                        "participant lineage changed"
                    )
                participant["status"] = "prepared"
                participant["last_outbound_type"] = message_type
                participant["last_outbound_message_id"] = message_id
            value["case_revision"] = case_revision
            value["status"] = CaseStatus.DELIBERATING.value
            value["updated_at"] = self._now()

        self._update_graph(case_id, update)

    def _record_result(
        self,
        *,
        case_id: str,
        participant_id: str,
        execution: ExecutionResult,
        dialogue: dict[str, Any] | None,
        case_status: str,
        issue: str | None,
    ) -> None:
        def update(value: dict[str, Any]) -> None:
            participant = next(
                (
                    item
                    for item in value["participants"]
                    if isinstance(item, dict)
                    and item.get("participant_id") == participant_id
                ),
                None,
            )
            if participant is None:
                raise CollaborationStorageError(
                    "dispatch result has no persisted participant"
                )
            participant["dispatch_count"] = int(
                participant.get("dispatch_count") or 0
            ) + 1
            participant["status"] = (
                "pending"
                if execution.status in NON_TERMINAL_STATUSES
                else "responded"
                if dialogue is not None
                else "rejected"
            )
            if dialogue is not None:
                participant["last_reply_type"] = dialogue.get(
                    "message_type"
                )
                participant["last_reply_message_id"] = dialogue.get(
                    "message_id"
                )
            for dispatch in value["dispatches"].values():
                if (
                    isinstance(dispatch, dict)
                    and dispatch.get("participant_id")
                    == participant_id
                    and dispatch.get("status")
                    == "claimed_before_dispatch"
                ):
                    dispatch["status"] = str(execution.status)
                    dispatch["completed_at"] = self._now()
                    dispatch["dialogue_type"] = (
                        dialogue.get("message_type")
                        if dialogue is not None
                        else None
                    )
                    break
            value["status"] = case_status
            value["issue"] = issue
            value["updated_at"] = self._now()

        self._update_graph(case_id, update)
        self._sync_graph_from_case(case_id)

    def _mark_pre_dispatch_blocked(
        self,
        *,
        binding: dict[str, Any],
        reason: str,
    ) -> None:
        case = self.case_store.get_case(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
        )
        if str(case.get("status") or "") == CaseStatus.DELIBERATING.value:
            paused = self.case_store.transition(
                case_id=str(binding["case_id"]),
                user_id=str(binding["user_id"]),
                workspace_id=str(binding["workspace_id"]),
                operation_id=f"{binding['operation_id']}:dispatch_blocked",
                expected_revision=int(case["revision"]),
                to_status=CaseStatus.PAUSED,
                reason=f"Phase 6 dispatch blocked: {reason[:400]}",
                checkpoint=self.bounded_negotiation._checkpoint(
                    checkpoint_id=(
                        f"{binding['checkpoint_id']}:blocked"
                    ),
                    phase="agent_dispatch_blocked",
                    operation_id=str(binding["operation_id"]),
                    step_id=str(binding["step_id"]),
                    task_id=str(binding["task_packet_id"]),
                    run_id=str(binding["runtime_run_id"]),
                    executor=str(binding["target_agent"]),
                    target_agent=str(binding["target_agent"]),
                    dialogue_message_id=str(binding["message_id"]),
                    result_status="blocked",
                    effect_state=CheckpointEffectState.NOT_STARTED,
                ),
            )
            self._update_graph(
                str(binding["case_id"]),
                lambda value: value.update(
                    {
                        "status": CaseStatus.PAUSED.value,
                        "case_revision": int(paused["revision"]),
                        "issue": reason[:400],
                        "updated_at": self._now(),
                    }
                ),
            )
        self._sync_graph_from_case(str(binding["case_id"]))

    def _fence_effect_violation(
        self,
        *,
        binding: dict[str, Any],
        execution: ExecutionResult,
    ) -> None:
        case = self.case_store.get_case(
            case_id=str(binding["case_id"]),
            user_id=str(binding["user_id"]),
            workspace_id=str(binding["workspace_id"]),
        )
        if str(case.get("status") or "") not in {
            CaseStatus.CANCELLED.value,
            CaseStatus.FAILED.value,
            CaseStatus.INDETERMINATE.value,
            CaseStatus.CLOSED.value,
        }:
            fenced = self.case_store.transition(
                case_id=str(binding["case_id"]),
                user_id=str(binding["user_id"]),
                workspace_id=str(binding["workspace_id"]),
                operation_id=f"{binding['operation_id']}:effect_fence",
                expected_revision=int(case["revision"]),
                to_status=CaseStatus.INDETERMINATE,
                reason=(
                    "read-only Agent reported or produced an effect; "
                    "collaboration cannot continue"
                ),
                checkpoint=self.bounded_negotiation._checkpoint(
                    checkpoint_id=(
                        f"{binding['checkpoint_id']}:effect_fence"
                    ),
                    phase="phase6_effect_violation",
                    operation_id=str(binding["operation_id"]),
                    step_id=str(binding["step_id"]),
                    task_id=str(binding["task_packet_id"]),
                    run_id=str(execution.task_id),
                    executor=str(execution.executor),
                    target_agent=str(binding["target_agent"]),
                    dialogue_message_id=str(binding["message_id"]),
                    result_status="indeterminate",
                    effect_state=CheckpointEffectState.UNKNOWN,
                    evidence_refs=[
                        "phase6_effect_boundary_violation"
                    ],
                ),
            )
            self._update_graph(
                str(binding["case_id"]),
                lambda value: value.update(
                    {
                        "status": CaseStatus.INDETERMINATE.value,
                        "case_revision": int(fenced["revision"]),
                        "issue": "read_only_effect_boundary_violation",
                        "updated_at": self._now(),
                    }
                ),
            )
        self._sync_graph_from_case(str(binding["case_id"]))

    def _sync_graph_from_case(self, case_id: str) -> None:
        graph = self._graph_unscoped(case_id)
        case = self.case_store.get_case(
            case_id=case_id,
            user_id=str(graph["user_id"]),
            workspace_id=str(graph["workspace_id"]),
        )
        replies_by_participant: dict[str, list[dict[str, Any]]] = {}
        outbound_by_message: dict[str, dict[str, Any]] = {}
        selection_projection: dict[str, Any] | None = None
        for record in case.get("dialogue", []):
            if not isinstance(record, dict):
                continue
            content = self._content(record)
            if record.get("sender") == "veyra":
                if (
                    record.get("message_type")
                    == DialogueType.PLAN_SELECTION.value
                ):
                    payload = (
                        content.get("payload")
                        if isinstance(content.get("payload"), dict)
                        else {}
                    )
                    selection_projection = {
                        "option_set_message_id": str(
                            payload.get("option_set_message_id") or ""
                        ),
                        "selected_option_id": str(
                            payload.get("selected_option_id") or ""
                        ),
                        "execution_authorized": False,
                        "selected_at": str(
                            record.get("recorded_at")
                            or case.get("updated_at")
                            or ""
                        ),
                    }
                binding = content.get("collaboration_binding")
                if isinstance(binding, dict):
                    outbound_by_message[str(record.get("message_id"))] = {
                        "participant_id": str(
                            binding.get("participant_id") or ""
                        ),
                        "task_packet_id": str(
                            content.get("task_packet_id") or ""
                        ),
                    }
                continue
            parent = outbound_by_message.get(
                str(record.get("in_reply_to") or "")
            )
            if not parent or not parent["participant_id"]:
                continue
            replies_by_participant.setdefault(
                parent["participant_id"], []
            ).append(
                {
                    "message_id": record.get("message_id"),
                    "message_type": record.get("message_type"),
                    "task_packet_id": parent["task_packet_id"],
                }
            )

        checkpoints = (
            case.get("checkpoints")
            if isinstance(case.get("checkpoints"), list)
            else []
        )
        effect_violation = any(
            isinstance(checkpoint, dict)
            and checkpoint.get("phase")
            == "phase6_effect_violation"
            for checkpoint in checkpoints
        )
        effect_unknown = any(
            isinstance(checkpoint, dict)
            and checkpoint.get("effect_state")
            == CheckpointEffectState.UNKNOWN.value
            for checkpoint in checkpoints
        )
        needs_sync = bool(
            str(graph.get("status") or "") != str(case["status"])
            or not isinstance(graph.get("case_revision"), int)
            or graph.get("case_revision") != int(case["revision"])
            or graph.get("selection") != selection_projection
            or (
                effect_unknown
                and graph.get("effect_status") != "unknown"
            )
            or (
                effect_unknown
                and graph.get("verification_status")
                != "indeterminate"
            )
            or (
                effect_violation
                and graph.get("issue")
                != "read_only_effect_boundary_violation"
            )
        )
        dispatches = (
            graph.get("dispatches")
            if isinstance(graph.get("dispatches"), dict)
            else {}
        )
        participants = (
            graph.get("participants")
            if isinstance(graph.get("participants"), list)
            else []
        )
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            replies = replies_by_participant.get(
                str(participant.get("participant_id") or ""),
                [],
            )
            if not replies:
                continue
            latest = replies[-1]
            if (
                participant.get("status") != "responded"
                or int(participant.get("dispatch_count") or 0)
                != len(replies)
                or participant.get("last_reply_type")
                != latest["message_type"]
                or participant.get("last_reply_message_id")
                != latest["message_id"]
            ):
                needs_sync = True
            dispatch = dispatches.get(latest["task_packet_id"])
            if (
                isinstance(dispatch, dict)
                and dispatch.get("status")
                == "claimed_before_dispatch"
            ):
                needs_sync = True
        if not needs_sync:
            return

        def sync(value: dict[str, Any]) -> None:
            current_revision = value.get("case_revision")
            if (
                not isinstance(current_revision, bool)
                and isinstance(current_revision, int)
                and current_revision > int(case["revision"])
            ):
                return
            for participant in value["participants"]:
                if not isinstance(participant, dict):
                    continue
                replies = replies_by_participant.get(
                    str(participant.get("participant_id") or ""),
                    [],
                )
                if not replies:
                    continue
                latest = replies[-1]
                participant["status"] = "responded"
                participant["dispatch_count"] = len(replies)
                participant["last_reply_type"] = latest[
                    "message_type"
                ]
                participant["last_reply_message_id"] = latest[
                    "message_id"
                ]
                dispatch = value["dispatches"].get(
                    latest["task_packet_id"]
                )
                if isinstance(dispatch, dict) and dispatch.get(
                    "status"
                ) == "claimed_before_dispatch":
                    dispatch["status"] = "recovered_terminal"
                    dispatch["completed_at"] = self._now()
                    dispatch["dialogue_type"] = latest[
                        "message_type"
                    ]
            value.update(
                {
                    "status": str(case["status"]),
                    "case_revision": int(case["revision"]),
                    "selection": selection_projection,
                    "effect_status": (
                        "unknown"
                        if effect_unknown
                        else value.get("effect_status", "not_started")
                    ),
                    "verification_status": (
                        "indeterminate"
                        if effect_unknown
                        else value.get(
                            "verification_status", "unverified"
                        )
                    ),
                    "issue": (
                        "read_only_effect_boundary_violation"
                        if effect_violation
                        else value.get("issue")
                    ),
                    "updated_at": self._now(),
                }
            )

        self._update_graph(
            case_id,
            sync,
        )

    def _record_runtime_issue(
        self, *, case_id: str, reason: str
    ) -> None:
        try:
            self._update_graph(
                case_id,
                lambda value: value.update(
                    {
                        "issue": reason[:400],
                        "updated_at": self._now(),
                    }
                ),
            )
        except ReadOnlyCollaborationError:
            return

    @staticmethod
    def _assert_selection_matches_graph(
        selection: AgentCapabilitySelection,
        graph: dict[str, Any],
    ) -> None:
        if (
            selection.runtime != graph.get("runtime")
            or selection.provider_binding
            != graph.get("provider_binding")
            or selection.certification_digest
            != graph.get("provider_certification_digest")
        ):
            raise AgentCapabilitySelectionError(
                "collaboration_provider_binding_changed"
            )

    def _graph(
        self, case_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        graph = self._graph_unscoped(case_id)
        if (
            graph.get("user_id") != user_id
            or graph.get("workspace_id") != workspace_id
        ):
            raise CollaborationNotFoundError(
                "collaboration not found"
            )
        return graph

    def _graph_unscoped(self, case_id: str) -> dict[str, Any]:
        state = self._state()
        self._validate_state(state)
        graph = state["collaborations"].get(case_id)
        if not isinstance(graph, dict):
            raise CollaborationNotFoundError(
                "collaboration not found"
            )
        return json.loads(json.dumps(graph))

    def _update_graph(
        self,
        case_id: str,
        updater: Any,
    ) -> None:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._validate_state(state)
            graph = state["collaborations"].get(case_id)
            if not isinstance(graph, dict):
                raise CollaborationNotFoundError(
                    "collaboration not found"
                )
            updater(graph)
            state["updated_at"] = self._now()
            return state

        self.state_store.mutate_json(COLLABORATION_STATE_FILE, mutate)

    def _state(self) -> dict[str, Any]:
        value = self.state_store.read_json(COLLABORATION_STATE_FILE)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _validate_state(state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("schema_version")
            != COLLABORATION_STATE_SCHEMA
            or not isinstance(state.get("collaborations"), dict)
            or not isinstance(state.get("event_index"), dict)
            or isinstance(state.get("collaboration_count"), bool)
            or not isinstance(state.get("collaboration_count"), int)
            or int(state["collaboration_count"])
            != len(state["collaborations"])
        ):
            raise CollaborationStorageError(
                "Phase 6 collaboration state is invalid"
            )
        for case_id, value in state["collaborations"].items():
            if (
                not isinstance(case_id, str)
                or not isinstance(value, dict)
                or value.get("case_id") != case_id
                or not isinstance(value.get("participants"), list)
                or not isinstance(value.get("dispatches"), dict)
                or not isinstance(value.get("budgets"), dict)
            ):
                raise CollaborationStorageError(
                    "Phase 6 collaboration record is invalid"
                )
            if len(value["participants"]) > MAX_PARTICIPANTS:
                raise CollaborationStorageError(
                    "Phase 6 participant budget was exceeded"
                )
            budgets = value["budgets"]
            for claimed, maximum in (
                ("agent_calls_claimed", MAX_AGENT_CALLS),
                ("handoffs_claimed", MAX_HANDOFFS),
                ("evidence_patches_claimed", MAX_EVIDENCE_PATCHES),
            ):
                amount = budgets.get(claimed)
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, int)
                    or not 0 <= amount <= maximum
                ):
                    raise CollaborationStorageError(
                        f"Phase 6 {claimed} is invalid"
                    )

    def _public(
        self, graph: dict[str, Any], case: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema_version": COLLABORATION_PUBLIC_SCHEMA,
            **self._public_graph(graph),
            "case": self.bounded_negotiation.public_case_detail(case),
        }

    @staticmethod
    def _public_graph(graph: dict[str, Any]) -> dict[str, Any]:
        participants = (
            graph.get("participants")
            if isinstance(graph.get("participants"), list)
            else []
        )
        budgets = (
            graph.get("budgets")
            if isinstance(graph.get("budgets"), dict)
            else {}
        )
        selection = (
            graph.get("selection")
            if isinstance(graph.get("selection"), dict)
            else None
        )
        effect_violation = (
            graph.get("issue")
            == "read_only_effect_boundary_violation"
        )
        effect_unknown = bool(
            effect_violation
            or graph.get("effect_status") == "unknown"
        )
        return {
            "case_id": graph.get("case_id"),
            "source_event_id": graph.get("source_event_id"),
            "status": graph.get("status"),
            "case_revision": graph.get("case_revision"),
            "runtime": graph.get("runtime"),
            "topology": graph.get("topology"),
            "execution_profile": graph.get("execution_profile"),
            "proposal_only": True,
            "effect_status": (
                "unknown" if effect_unknown else "not_started"
            ),
            "verification_status": (
                "indeterminate" if effect_unknown else "unverified"
            ),
            "execution_authorized": False,
            "provider_switch_allowed": False,
            "participants": [
                {
                    key: item.get(key)
                    for key in (
                        "participant_id",
                        "role",
                        "parent_participant_id",
                        "handoff_index",
                        "status",
                        "dispatch_count",
                        "last_outbound_type",
                        "last_reply_type",
                    )
                }
                for item in participants
                if isinstance(item, dict)
            ],
            "budgets": {
                "agent_calls": {
                    "claimed": int(
                        budgets.get("agent_calls_claimed") or 0
                    ),
                    "max": MAX_AGENT_CALLS,
                },
                "participants": {
                    "used": len(participants),
                    "max": MAX_PARTICIPANTS,
                },
                "handoffs": {
                    "claimed": int(
                        budgets.get("handoffs_claimed") or 0
                    ),
                    "max": MAX_HANDOFFS,
                },
                "evidence_patches": {
                    "claimed": int(
                        budgets.get("evidence_patches_claimed") or 0
                    ),
                    "max": MAX_EVIDENCE_PATCHES,
                },
            },
            "selection": selection,
            "issue": graph.get("issue"),
            "created_at": graph.get("created_at"),
            "updated_at": graph.get("updated_at"),
            "expires_at": graph.get("expires_at"),
        }

    def _event_from_graph(self, graph: dict[str, Any]) -> VeyraEvent:
        return VeyraEvent.from_dict(
            {
                "type": "user_message",
                "source": {
                    "channel": graph["channel"],
                    "user_id": graph["user_id"],
                    "session_id": graph["dialogue_session_id"],
                },
                "payload": {"text": graph["user_goal"]},
                "event_id": graph["source_event_id"],
                "privacy_scope": "user",
            }
        )

    def _require_current_workspace(self, workspace_id: str) -> None:
        current = str(
            self.state_store.read_json("local_world.json").get(
                "current_project"
            )
            or ""
        ).strip()
        if not current or workspace_id != current:
            raise CollaborationConflictError(
                "workspace must match the current local Veyra scope"
            )

    @staticmethod
    def _next_turn_index(case: dict[str, Any]) -> int:
        return 1 + sum(
            1
            for item in case.get("dialogue", [])
            if isinstance(item, dict)
            and item.get("sender") == "veyra"
            and item.get("message_type")
            in {
                DialogueType.TASK_REQUEST.value,
                DialogueType.CONTEXT_PATCH.value,
            }
        )

    @classmethod
    def _turn_identity(
        cls,
        *,
        case_id: str,
        case_revision: int,
        turn_index: int,
        role: str,
    ) -> dict[str, str]:
        digest = cls._digest(
            {
                "namespace": "veyra.phase6.read_only_turn.v1",
                "case_id": case_id,
                "case_revision": case_revision,
                "turn_index": turn_index,
                "role": role,
            }
        )
        return {
            "digest": digest,
            "task_packet_id": f"p6task_{digest[:32]}",
            "runtime_run_id": f"veyra-p6-{digest[:32]}",
            "step_id": f"p6step_{turn_index}_{digest[:16]}",
            "message_id": f"p6msg_{digest[:24]}",
            "operation_id": f"p6turn_{turn_index}_{digest[:24]}",
            "checkpoint_id": f"p6cp_{digest[:24]}",
        }

    @staticmethod
    def _latest_dialogue(
        case: dict[str, Any], message_type: str, *, sender: str
    ) -> dict[str, Any]:
        return next(
            (
                item
                for item in reversed(case.get("dialogue", []))
                if isinstance(item, dict)
                and item.get("message_type") == message_type
                and item.get("sender") == sender
            ),
            {},
        )

    @staticmethod
    def _latest_agent_dialogue(
        case: dict[str, Any]
    ) -> dict[str, Any]:
        return next(
            (
                item
                for item in reversed(case.get("dialogue", []))
                if isinstance(item, dict)
                and item.get("sender") == "agent"
            ),
            {},
        )

    @staticmethod
    def _content(record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("content")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _expiry(graph: dict[str, Any]) -> datetime:
        try:
            value = datetime.fromisoformat(
                str(graph["expires_at"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise CollaborationStorageError(
                "collaboration expiry is invalid"
            ) from exc
        if value.tzinfo is None:
            raise CollaborationStorageError(
                "collaboration expiry is not timezone-aware"
            )
        if value <= datetime.now(timezone.utc):
            raise CollaborationConflictError(
                "collaboration wall-time budget expired"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _evidence_request_id(
        value: dict[str, Any], index: int
    ) -> str:
        explicit = str(value.get("request_id") or "").strip()
        if explicit:
            return explicit
        digest = ReadOnlyAgentCollaborationRuntime._digest(
            {
                "question": value.get("question"),
                "reason": value.get("reason"),
                "claim_ref": value.get("claim_ref"),
                "index": index,
            }
        )
        return f"evidence_request_{digest[:16]}"

    @staticmethod
    def _authoritative_effects(
        execution: ExecutionResult,
    ) -> bool:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        for key in (
            "authoritative_tool_receipts",
            "verified_tool_effects",
            "authoritative_effects",
        ):
            value = raw.get(key)
            if isinstance(value, list) and value:
                return True
            if isinstance(value, dict) and value:
                return True
        return False

    @staticmethod
    def _reported_effects(
        execution: ExecutionResult,
    ) -> bool:
        if execution.tool_calls or execution.changed_files:
            return True
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        diagnostic = raw.get("agent_reported_tool_evidence")
        if diagnostic is None:
            return False
        if not isinstance(diagnostic, dict):
            return True
        for flag_name in (
            "tool_calls_reported",
            "changed_files_reported",
        ):
            flag = diagnostic.get(flag_name)
            if not isinstance(flag, bool) or flag:
                return True
        for count_name in (
            "tool_call_count",
            "changed_file_count",
        ):
            count = diagnostic.get(count_name)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count != 0
            ):
                return True
        return False

    @staticmethod
    def _participant_field(
        graph: dict[str, Any], participant_id: str, field: str
    ) -> Any:
        for item in graph.get("participants", []):
            if (
                isinstance(item, dict)
                and item.get("participant_id") == participant_id
            ):
                return item.get(field)
        return None

    @staticmethod
    def _required_text(
        value: Any, field: str, max_length: int
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        selected = value.strip()
        if (
            not selected
            or selected != value
            or len(selected) > max_length
            or any(ord(character) < 32 for character in selected)
        ):
            raise ValueError(f"{field} must be normalized bounded text")
        return selected

    @staticmethod
    def _optional_text(
        value: Any, field: str, max_length: int
    ) -> str:
        if value in (None, ""):
            return ""
        return ReadOnlyAgentCollaborationRuntime._required_text(
            value, field, max_length
        )

    @staticmethod
    def _canonical_ids(
        values: list[str],
        *,
        field: str,
        limit: int,
        item_limit: int,
    ) -> list[str]:
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"{field} exceeds its item budget")
        selected = [
            ReadOnlyAgentCollaborationRuntime._required_text(
                item, field, item_limit
            )
            for item in values
        ]
        if len(selected) != len(set(selected)):
            raise ValueError(f"{field} cannot contain duplicates")
        return sorted(selected)

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _safe_error_reason(exc: Exception) -> str:
        if isinstance(exc, AgentCapabilitySelectionError):
            return exc.reason[:400]
        if isinstance(
            exc,
            (
                BoundedNegotiationError,
                CollaborationConflictError,
                CollaborationNotFoundError,
                CollaborationStorageError,
                ReadOnlyCollaborationError,
            ),
        ):
            return f"{type(exc).__name__}:{str(exc)[:300]}"
        # Unexpected adapter/runtime exceptions may contain provider payloads,
        # host paths, or credentials. Public collaboration projections expose
        # only a stable class-level code.
        return f"unexpected_runtime_error:{type(exc).__name__}"


__all__ = [
    "COLLABORATION_PUBLIC_SCHEMA",
    "COLLABORATION_STATUS_SCHEMA",
    "CollaborationConflictError",
    "CollaborationNotFoundError",
    "CollaborationStorageError",
    "ReadOnlyAgentCollaborationRuntime",
    "ReadOnlyCollaborationError",
]
