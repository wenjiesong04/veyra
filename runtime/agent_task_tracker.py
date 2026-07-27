from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import utc_now_iso
from rollback_audit.execution_trace import ExecutionTrace


class AgentTaskTracker:
    """Persists non-terminal Agent tasks and refreshes them later."""

    DEFAULT_STALE_AFTER_HOURS = 6.0
    DEFAULT_FORCE_PRUNE_AFTER_HOURS = 24.0

    def __init__(
        self,
        state_store: WorldStateStore,
        execution_trace: ExecutionTrace | None = None,
        recovery_hook: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.state_store = state_store
        self.execution_trace = execution_trace or ExecutionTrace(state_store)
        self.recovery_hook = recovery_hook

    def set_recovery_hook(self, recovery_hook: Callable[[dict[str, Any]], Any] | None) -> None:
        self.recovery_hook = recovery_hook

    def register(
        self,
        *,
        event_id: str,
        route: str,
        execution: ExecutionResult,
        verification: dict[str, Any],
        session_id: str | None = None,
        channel: str | None = None,
        user_id: str | None = None,
        correlation_id: str | None = None,
        task_packet_id: str | None = None,
        agent_execution_session_id: str | None = None,
        agent_session_policy: str | None = None,
        memory_policy: str | None = None,
        verification_policy: dict[str, Any] | None = None,
        rollback_requirement: dict[str, Any] | None = None,
        user_goal: str | None = None,
        case_id: str | None = None,
        case_workspace_id: str | None = None,
        case_step_id: str | None = None,
        case_operation_id: str | None = None,
        case_revision: int | None = None,
        dialogue_message_id: str | None = None,
        target_agent: str | None = None,
        force_pending: bool = False,
    ) -> dict[str, Any] | None:
        task_context = self._task_context(
            execution=execution,
            event_id=event_id,
            route=route,
            authority="veyra_registered",
            session_id=session_id,
            channel=channel,
            user_id=user_id,
            correlation_id=correlation_id,
            task_packet_id=task_packet_id,
            agent_execution_session_id=agent_execution_session_id,
            agent_session_policy=agent_session_policy,
            memory_policy=memory_policy,
            verification_policy=verification_policy,
            rollback_requirement=rollback_requirement,
            user_goal=user_goal,
            case_id=case_id,
            case_workspace_id=case_workspace_id,
            case_step_id=case_step_id,
            case_operation_id=case_operation_id,
            case_revision=case_revision,
            dialogue_message_id=dialogue_message_id,
            target_agent=target_agent,
        )
        if (
            execution.status not in NON_TERMINAL_STATUSES
            and not force_pending
        ):
            terminal_context = {
                **task_context,
                "final_status": execution.status,
                "verification_status": verification.get("status"),
                "updated_at": utc_now_iso(),
            }
            self._persist_terminal_context(
                execution=execution,
                verification=verification,
                route=route,
                task_context=terminal_context,
            )
            self._run_recovery_hook(execution, verification, terminal_context)
            return None
        task = {
            "task_id": execution.task_id,
            "event_id": event_id,
            "route": route,
            "executor": execution.executor,
            "status": execution.status,
            "verification_status": verification.get("status"),
            "next_action": verification.get("next_action"),
            "session_id": session_id,
            "channel": channel,
            "user_id": user_id,
            "registered_at": utc_now_iso(),
            "last_polled_at": None,
            "poll_count": 0,
            "task_context": task_context,
            **self._flat_context(task_context),
        }
        self._upsert_pending(task)
        return task

    def get_context(self, task_id: str, *, authoritative_only: bool = True) -> dict[str, Any] | None:
        """Resolve durable task context by runtime task, packet, or correlation id."""
        target = str(task_id or "").strip()
        if not target:
            return None
        task_state = self.state_store.read_json("task_state.json")
        pending = task_state.get("pending_agent_tasks") if isinstance(task_state.get("pending_agent_tasks"), list) else []
        for item in pending:
            if not isinstance(item, dict):
                continue
            context = self._context_from_item(item)
            if self._context_matches(target, context) and (
                not authoritative_only or self._is_authoritative(context)
            ):
                return context
        contexts = task_state.get("agent_task_contexts") if isinstance(task_state.get("agent_task_contexts"), dict) else {}
        direct = contexts.get(target)
        if isinstance(direct, dict) and (
            not authoritative_only or self._is_authoritative(direct)
        ):
            return dict(direct)
        for context in contexts.values():
            if (
                isinstance(context, dict)
                and self._context_matches(target, context)
                and (not authoritative_only or self._is_authoritative(context))
            ):
                return dict(context)
        return None

    def retire_case_tasks(
        self,
        *,
        case_id: str,
        case_status: str,
    ) -> dict[str, Any]:
        """Remove pending Agent polls after a Case reaches a terminal fence."""

        retired: list[str] = []

        def retire(task_state: dict[str, Any]) -> dict[str, Any]:
            pending = (
                task_state.get("pending_agent_tasks")
                if isinstance(
                    task_state.get("pending_agent_tasks"), list
                )
                else []
            )
            kept: list[Any] = []
            contexts = (
                dict(task_state.get("agent_task_contexts") or {})
                if isinstance(
                    task_state.get("agent_task_contexts"), dict
                )
                else {}
            )
            for item in pending:
                if not isinstance(item, dict):
                    kept.append(item)
                    continue
                context = self._context_from_item(item)
                if str(context.get("case_id") or "") != case_id:
                    kept.append(item)
                    continue
                task_id = str(item.get("task_id") or "")
                if task_id:
                    retired.append(task_id)
                    contexts[task_id] = {
                        **context,
                        "final_status": (
                            f"durable_case_{case_status.lower()}"
                        ),
                        "verification_status": "case_lifecycle_terminal",
                        "updated_at": utc_now_iso(),
                    }
            task_state["pending_agent_tasks"] = kept
            task_state["agent_task_contexts"] = contexts
            return task_state

        self.state_store.mutate_json("task_state.json", retire)
        return {
            "status": "success",
            "case_id": case_id,
            "case_status": case_status,
            "retired_task_ids": retired,
            "retired_count": len(retired),
        }

    def apply_result(
        self,
        *,
        execution: ExecutionResult,
        verification: dict[str, Any],
        event_id: str | None = None,
        route: str = "agent",
    ) -> dict[str, Any]:
        matched = False
        resolved_context: dict[str, Any] = {}
        incoming_context = self._task_context(
            execution=execution,
            event_id=event_id,
            route=route,
            authority="runtime_reported",
        )

        def update_task_state(task_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal matched, resolved_context
            pending = task_state.get("pending_agent_tasks") if isinstance(task_state.get("pending_agent_tasks"), list) else []
            for item in pending:
                if not isinstance(item, dict) or item.get("task_id") != execution.task_id:
                    continue
                registered_context = self._context_from_item(item)
                # Runtime callbacks may report correlation hints, but only context
                # persisted by Veyra at dispatch time is authoritative. Registered
                # governance fields win over anything supplied in execution.raw.
                resolved_context = self._merge_context(incoming_context, registered_context)
                item.update(
                    {
                        "status": execution.status,
                        "verification_status": verification.get("status"),
                        "last_polled_at": utc_now_iso(),
                        "poll_count": int(item.get("poll_count") or 0) + 1,
                        "task_context": resolved_context,
                        **self._flat_context(resolved_context),
                    }
                )
                matched = True
            if not matched and execution.status in NON_TERMINAL_STATUSES:
                resolved_context = incoming_context
                pending.append(
                    {
                        "task_id": execution.task_id,
                        "event_id": event_id or f"task_{execution.task_id}",
                        "route": route,
                        "executor": execution.executor,
                        "status": execution.status,
                        "verification_status": verification.get("status"),
                        "next_action": verification.get("next_action"),
                        "registered_at": utc_now_iso(),
                        "last_polled_at": utc_now_iso(),
                        "poll_count": 1,
                        "task_context": resolved_context,
                        **self._flat_context(resolved_context),
                    }
                )
            if not resolved_context:
                contexts = task_state.get("agent_task_contexts") if isinstance(task_state.get("agent_task_contexts"), dict) else {}
                archived = contexts.get(execution.task_id)
                if isinstance(archived, dict) and self._is_authoritative(archived):
                    resolved_context = self._merge_context(incoming_context, archived)
            if execution.status not in NON_TERMINAL_STATUSES:
                pending = [
                    item
                    for item in pending
                    if not isinstance(item, dict) or item.get("task_id") != execution.task_id
                ]
            task_state["pending_agent_tasks"] = pending[-100:]
            if resolved_context and self._is_authoritative(resolved_context):
                resolved_context = {
                    **resolved_context,
                    "runtime_task_id": execution.task_id,
                    "final_status": execution.status,
                    "verification_status": verification.get("status"),
                    "updated_at": utc_now_iso(),
                }
                self._store_context(task_state, execution.task_id, resolved_context)
            task_state["current_task"] = {
                "route": route,
                "status": verification.get("status"),
            }
            return task_state

        task_state = self.state_store.mutate_json("task_state.json", update_task_state)
        if resolved_context and self._is_authoritative(resolved_context):
            self._run_recovery_hook(execution, verification, resolved_context)
        return {
            "matched": matched,
            "status": execution.status,
            "pending_count": len(
                task_state.get("pending_agent_tasks", [])
                if isinstance(
                    task_state.get("pending_agent_tasks"), list
                )
                else []
            ),
        }

    def refresh_pending(self, adapter: AgentAdapter, verifier: Any, limit: int = 20) -> dict[str, Any]:
        prune_report = self.prune_stale_pending(adapter, verifier)
        pending = self.state_store.read_json("task_state.json").get("pending_agent_tasks", [])
        refreshed: list[dict[str, Any]] = []
        for item in list(pending)[:limit]:
            task_context = self._context_from_item(item)
            if task_context.get("case_id"):
                # Durable Case recovery owns its exact adapter, dialogue
                # verifier, cancellation fence, and lifecycle transition.
                continue
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            execution = adapter.fetch_task_status(task_id)
            verification = verifier.verify_execution_result(execution)
            trace = self.execution_trace.record(
                {
                    "event_id": item.get("event_id") or f"poll_{task_id}",
                    "route": item.get("route") or "agent",
                    "task_id": execution.task_id,
                    "executor": execution.executor,
                    "status": verification["status"],
                    "execution_result": execution.to_dict(),
                    "verification": verification,
                }
            )
            self.apply_result(execution=execution, verification=verification, event_id=str(item.get("event_id") or ""), route=str(item.get("route") or "agent"))
            refreshed.append(
                {
                    "status": execution.status,
                    "verification_status": verification.get("status"),
                    "trace_id": trace.get("trace_id"),
                }
            )
        remaining = self.state_store.read_json("task_state.json").get(
            "pending_agent_tasks", []
        )
        return {
            "status": "success",
            "pruned_count": len(prune_report.get("pruned", [])),
            "refreshed": refreshed,
            "remaining_count": len(
                remaining if isinstance(remaining, list) else []
            ),
        }

    def prune_stale_pending(
        self,
        adapter: AgentAdapter,
        verifier: Any,
        *,
        stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
        force_after_hours: float = DEFAULT_FORCE_PRUNE_AFTER_HOURS,
    ) -> dict[str, Any]:
        task_state = self.state_store.read_json("task_state.json")
        pending = list(task_state.get("pending_agent_tasks", []))
        if not pending:
            return {"status": "success", "pruned": [], "remaining_count": 0}

        kept: list[dict[str, Any]] = []
        pruned: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for item in pending:
            task_context = self._context_from_item(item)
            if task_context.get("case_id"):
                kept.append(item)
                continue
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            age_hours = self._age_hours(item.get("registered_at"), now)
            if age_hours is not None and age_hours >= force_after_hours:
                pruned.append(self._finalize_pruned(item, reason="force_prune_after_max_age", age_hours=age_hours))
                continue
            if age_hours is not None and age_hours >= stale_after_hours and str(item.get("status") or "") in NON_TERMINAL_STATUSES:
                execution = adapter.fetch_task_status(task_id)
                verification = verifier.verify_execution_result(execution)
                if execution.status in NON_TERMINAL_STATUSES:
                    pruned.append(
                        self._finalize_pruned(
                            item,
                            reason="stale_non_terminal_after_poll",
                            age_hours=age_hours,
                            execution=execution,
                            verification=verification,
                        )
                    )
                    continue
                self.apply_result(
                    execution=execution,
                    verification=verification,
                    event_id=str(item.get("event_id") or ""),
                    route=str(item.get("route") or "agent"),
                )
                if execution.status not in NON_TERMINAL_STATUSES:
                    continue
            kept.append(item)

        original_ids = {
            str(item.get("task_id") or "")
            for item in pending
            if isinstance(item, dict) and item.get("task_id")
        }
        kept_by_id = {
            str(item.get("task_id") or ""): item
            for item in kept
            if isinstance(item, dict) and item.get("task_id")
        }

        def merge_pruned_state(current: dict[str, Any]) -> dict[str, Any]:
            latest = current.get("pending_agent_tasks") if isinstance(current.get("pending_agent_tasks"), list) else []
            merged: list[Any] = []
            for item in latest:
                if not isinstance(item, dict):
                    merged.append(item)
                    continue
                task_id = str(item.get("task_id") or "")
                if task_id in original_ids and task_id not in kept_by_id:
                    continue
                merged.append(kept_by_id.get(task_id, item))
            current["pending_agent_tasks"] = merged[-100:]
            return current

        self.state_store.mutate_json("task_state.json", merge_pruned_state)
        return {"status": "success", "pruned": pruned, "remaining_count": len(kept)}

    def _finalize_pruned(
        self,
        item: dict[str, Any],
        *,
        reason: str,
        age_hours: float | None,
        execution: ExecutionResult | None = None,
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = str(item.get("task_id") or "")
        failed = execution or ExecutionResult(
            task_id=task_id,
            executor=str(item.get("executor") or "openclaw"),
            status="error",
            result="Stale pending agent task pruned after supervision timeout.",
            raw={"auto_pruned": True, "prune_reason": reason, "age_hours": age_hours},
        )
        verified = verification or {"status": "verified_failed", "verdict": "stale_pending_pruned", "next_action": "retry_or_ignore"}
        self.execution_trace.record(
            {
                "event_id": item.get("event_id") or f"prune_{task_id}",
                "route": item.get("route") or "agent",
                "task_id": task_id,
                "executor": failed.executor,
                "status": verified["status"],
                "execution_result": failed.to_dict(),
                "verification": verified,
                "prune_reason": reason,
            }
        )
        return {"task_id": task_id, "reason": reason, "age_hours": age_hours, "execution_result": failed.to_dict()}

    @staticmethod
    def _age_hours(value: Any, now: datetime) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed).total_seconds() / 3600.0)

    def _upsert_pending(self, task: dict[str, Any]) -> None:
        def upsert(task_state: dict[str, Any]) -> dict[str, Any]:
            pending = task_state.get("pending_agent_tasks") if isinstance(task_state.get("pending_agent_tasks"), list) else []
            for index, item in enumerate(pending):
                if isinstance(item, dict) and item.get("task_id") == task["task_id"]:
                    pending[index] = {**item, **task}
                    break
            else:
                pending.append(task)
            task_state["pending_agent_tasks"] = pending[-100:]
            context = self._context_from_item(task)
            if context:
                self._store_context(task_state, str(task.get("task_id") or ""), context)
            return task_state

        self.state_store.mutate_json("task_state.json", upsert)

    def _persist_terminal_context(
        self,
        *,
        execution: ExecutionResult,
        verification: dict[str, Any],
        route: str,
        task_context: dict[str, Any],
    ) -> None:
        def persist(task_state: dict[str, Any]) -> dict[str, Any]:
            self._store_context(task_state, execution.task_id, task_context)
            task_state["current_task"] = {
                "task_id": execution.task_id,
                "route": route,
                "status": verification.get("status"),
                "task_context": task_context,
            }
            return task_state

        self.state_store.mutate_json("task_state.json", persist)

    def _run_recovery_hook(
        self,
        execution: ExecutionResult,
        verification: dict[str, Any],
        task_context: dict[str, Any],
    ) -> None:
        needs_rollback = bool(
            verification.get("needs_rollback")
            or verification.get("status") == "needs_rollback"
        )
        if not needs_rollback or self.recovery_hook is None:
            return
        if not self._claim_recovery_hook(execution.task_id):
            return
        payload = {
            "task_id": execution.task_id,
            "execution": execution.to_dict(),
            "verification": verification,
            "task_context": task_context,
        }
        try:
            result = self.recovery_hook(payload)
        except Exception as exc:
            self._finish_recovery_hook(execution.task_id, "error")
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": task_context.get("event_id") or f"recovery_{execution.task_id}",
                    "route": "agent_recovery_hook",
                    "status": "error",
                    "task_id": execution.task_id,
                    "artifacts": {
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "verification_status": verification.get("status"),
                    },
                },
            )
            return
        self._finish_recovery_hook(execution.task_id, "triggered")
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": task_context.get("event_id") or f"recovery_{execution.task_id}",
                "route": "agent_recovery_hook",
                "status": "triggered",
                "task_id": execution.task_id,
                "artifacts": {
                    "verification_status": verification.get("status"),
                    "result": result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else str(result),
                },
            },
        )

    def _claim_recovery_hook(self, task_id: str) -> bool:
        claimed = False

        def claim(task_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            contexts = task_state.get("agent_task_contexts") if isinstance(task_state.get("agent_task_contexts"), dict) else {}
            context = contexts.get(task_id)
            if not isinstance(context, dict) or not self._is_authoritative(context):
                return task_state
            if context.get("recovery_hook_status") in {"running", "triggered", "error"}:
                return task_state
            context = {
                **context,
                "recovery_hook_status": "running",
                "recovery_hook_updated_at": utc_now_iso(),
            }
            contexts = dict(contexts)
            contexts[task_id] = context
            task_state["agent_task_contexts"] = contexts
            claimed = True
            return task_state

        self.state_store.mutate_json("task_state.json", claim)
        return claimed

    def _finish_recovery_hook(self, task_id: str, status: str) -> None:
        def finish(task_state: dict[str, Any]) -> dict[str, Any]:
            contexts = task_state.get("agent_task_contexts") if isinstance(task_state.get("agent_task_contexts"), dict) else {}
            context = contexts.get(task_id)
            if not isinstance(context, dict) or not self._is_authoritative(context):
                return task_state
            contexts = dict(contexts)
            contexts[task_id] = {
                **context,
                "recovery_hook_status": status,
                "recovery_hook_updated_at": utc_now_iso(),
            }
            task_state["agent_task_contexts"] = contexts
            return task_state

        self.state_store.mutate_json("task_state.json", finish)

    def _task_context(
        self,
        *,
        execution: ExecutionResult,
        event_id: str | None,
        route: str,
        authority: str,
        session_id: str | None = None,
        channel: str | None = None,
        user_id: str | None = None,
        correlation_id: str | None = None,
        task_packet_id: str | None = None,
        agent_execution_session_id: str | None = None,
        agent_session_policy: str | None = None,
        memory_policy: str | None = None,
        verification_policy: dict[str, Any] | None = None,
        rollback_requirement: dict[str, Any] | None = None,
        user_goal: str | None = None,
        case_id: str | None = None,
        case_workspace_id: str | None = None,
        case_step_id: str | None = None,
        case_operation_id: str | None = None,
        case_revision: int | None = None,
        dialogue_message_id: str | None = None,
        target_agent: str | None = None,
    ) -> dict[str, Any]:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        embedded = raw.get("task_context") if isinstance(raw.get("task_context"), dict) else {}
        context = {
            "authority": authority,
            "runtime_task_id": execution.task_id,
            "task_packet_id": task_packet_id or embedded.get("task_packet_id"),
            "correlation_id": correlation_id or embedded.get("correlation_id") or event_id,
            "event_id": event_id or embedded.get("event_id"),
            "route": route or embedded.get("route") or "agent",
            "executor": execution.executor,
            "session_id": session_id or embedded.get("session_id") or embedded.get("dialogue_session_id"),
            "channel": channel or embedded.get("channel"),
            "user_id": user_id or embedded.get("user_id"),
            "agent_execution_session_id": (
                agent_execution_session_id
                or embedded.get("agent_execution_session_id")
                or embedded.get("session_key")
            ),
            "agent_session_policy": agent_session_policy or embedded.get("agent_session_policy"),
            "memory_policy": memory_policy or embedded.get("memory_policy"),
            "verification_policy": verification_policy if verification_policy is not None else embedded.get("verification_policy"),
            "rollback_requirement": (
                rollback_requirement if rollback_requirement is not None else embedded.get("rollback_requirement")
            ),
            "user_goal": self._safe_user_goal(user_goal or embedded.get("user_goal")),
            "case_id": case_id or embedded.get("case_id"),
            "case_workspace_id": (
                case_workspace_id
                or embedded.get("case_workspace_id")
            ),
            "case_step_id": case_step_id or embedded.get("case_step_id"),
            "case_operation_id": (
                case_operation_id or embedded.get("case_operation_id")
            ),
            "case_revision": (
                case_revision
                if case_revision is not None
                else embedded.get("case_revision")
            ),
            "dialogue_message_id": (
                dialogue_message_id or embedded.get("dialogue_message_id")
            ),
            "target_agent": target_agent or embedded.get("target_agent"),
            "registered_at": embedded.get("registered_at") or utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        return {
            key: value
            for key, value in context.items()
            if value is not None and value != "" and value != {}
        }

    def _context_from_item(self, item: dict[str, Any]) -> dict[str, Any]:
        embedded = item.get("task_context") if isinstance(item.get("task_context"), dict) else {}
        fallback = {
            key: item.get(key)
            for key in (
                "task_packet_id",
                "correlation_id",
                "event_id",
                "route",
                "executor",
                "session_id",
                "channel",
                "user_id",
                "agent_execution_session_id",
                "agent_session_policy",
                "memory_policy",
                "verification_policy",
                "rollback_requirement",
                "registered_at",
                "authority",
                "user_goal",
                "case_id",
                "case_workspace_id",
                "case_step_id",
                "case_operation_id",
                "case_revision",
                "dialogue_message_id",
                "target_agent",
            )
            if item.get(key) is not None
        }
        runtime_task_id = item.get("task_id") or embedded.get("runtime_task_id")
        return self._merge_context(
            fallback,
            {
                **embedded,
                "runtime_task_id": runtime_task_id,
            },
        )

    @staticmethod
    def _flat_context(context: dict[str, Any]) -> dict[str, Any]:
        return {
            key: context[key]
            for key in (
                "task_packet_id",
                "correlation_id",
                "agent_execution_session_id",
                "agent_session_policy",
                "memory_policy",
                "verification_policy",
                "rollback_requirement",
                "user_goal",
                "case_id",
                "case_workspace_id",
                "case_step_id",
                "case_operation_id",
                "case_revision",
                "dialogue_message_id",
                "target_agent",
            )
            if key in context
        }

    @staticmethod
    def _merge_context(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        return {
            **base,
            **{
                key: value
                for key, value in update.items()
                if value is not None and value != "" and value != {}
            },
        }

    @staticmethod
    def _context_matches(target: str, context: dict[str, Any]) -> bool:
        return target in {
            str(context.get("runtime_task_id") or ""),
            str(context.get("task_packet_id") or ""),
            str(context.get("correlation_id") or ""),
            str(context.get("case_id") or ""),
            str(context.get("dialogue_message_id") or ""),
        }

    @staticmethod
    def _store_context(task_state: dict[str, Any], task_id: str, context: dict[str, Any]) -> None:
        if not task_id or not context or not AgentTaskTracker._is_authoritative(context):
            return
        contexts = task_state.get("agent_task_contexts") if isinstance(task_state.get("agent_task_contexts"), dict) else {}
        contexts = dict(contexts)
        contexts[task_id] = dict(context)
        if len(contexts) > 200:
            ordered = sorted(
                contexts.items(),
                key=lambda item: str(item[1].get("updated_at") or item[1].get("registered_at") or ""),
                reverse=True,
            )
            contexts = dict(ordered[:200])
        task_state["agent_task_contexts"] = contexts

    @staticmethod
    def _is_authoritative(context: dict[str, Any]) -> bool:
        return str(context.get("authority") or "") == "veyra_registered"

    @staticmethod
    def _safe_user_goal(value: Any) -> str:
        text = str(value or "").replace("\n", " ").strip()
        text = re.sub(r"/Users/[^\s,;:)]+", "<local_path>", text)
        text = re.sub(
            r"(?i)(api[_-]?key|token|secret|password)=([A-Za-z0-9._~+/=-]+)",
            r"\1=<redacted>",
            text,
        )
        return text[:500]
