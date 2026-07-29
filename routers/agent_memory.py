from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from interface.agent_adapter import ExecutionResult
from interface.agent_contract import contract_summary
from memory_bridge.scope import normalize_scope_component
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


class MemoryPatchRequest(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    patch: dict[str, Any]
    provider: str = Field(default="selected", min_length=1, max_length=120)


class MemoryDiagnosticsRequest(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    provider: str = Field(default="all", min_length=1, max_length=120)
    user_id: str = Field(default="local-user", min_length=1, max_length=240)
    session_id: str = Field(default="memory-diagnostics", min_length=1, max_length=240)
    write_probe: bool = False


class MemorySummaryRequest(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    provider: str = Field(default="selected", min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    focus: list[str] = Field(default_factory=list, max_length=20)


class AgentSelectRequest(BaseModel):
    name: str


class AgentConfigRequest(BaseModel):
    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    enabled: bool | None = None
    timeout: float | None = None
    task_path: str | None = None
    capabilities_path: str | None = None
    memory_summary_path: str | None = None
    memory_patch_path: str | None = None
    status_path_template: str | None = None
    stop_path_template: str | None = None
    protocol_min: int | None = None
    protocol_max: int | None = None
    use_model_for_core: bool | None = None
    model_provider: str | None = None
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_api_key_env: str | None = None
    model: str | None = None
    model_timeout: float | None = None
    model_decision_mode: str | None = None


class AgentInvokeRequest(BaseModel):
    text: str
    agents: list[str] = Field(default_factory=list)
    mode: str = "fanout"
    channel: str = "api"
    user_id: str = "local-user"
    session_id: str = "multi-agent"


class GovernanceCanaryRunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.openclaw_governance_canary_run.v1"
    ]
    suffix: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^[0-9a-f]{12}$",
    )


class AgentResultRequest(BaseModel):
    task_id: str
    executor: str = "external"
    status: str
    result: str = ""
    logs: str = ""
    changed_files: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


PUBLIC_MEMORY_CONTENT_FIELDS = {
    "memory_type",
    "result",
    "summary",
    "tags",
    "task",
    "topic",
}


def _required_memory_identifier(value: str, field: str) -> str:
    try:
        return normalize_scope_component(value, field)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc


def _memory_provider(bridge: Any, provider: str) -> str:
    normalized = _required_memory_identifier(provider, "provider")
    status = bridge.provider_status(probe=False)
    providers = (
        status.get("providers")
        if isinstance(status.get("providers"), list)
        else []
    )
    if normalized not in providers:
        raise HTTPException(
            status_code=422,
            detail="unknown memory provider",
        )
    return normalized


def _agent_status_snapshot(deps: dict[str, Any]) -> dict[str, Any]:
    state = deps["state_store"].read_json("executor_state.json")
    state = state if isinstance(state, dict) else {}
    selected = str(state.get("selected_agent") or "openclaw")
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    selected_status = (
        agents.get(selected)
        if isinstance(agents.get(selected), dict)
        else {
            key: value
            for key, value in state.items()
            if key not in {"agents", "selected_agent"}
        }
    )
    snapshot = dict(selected_status) if isinstance(selected_status, dict) else {}
    snapshot.setdefault("name", selected)
    snapshot.setdefault("status", "not_observed")
    snapshot.setdefault("connected", False)
    observed_at = str(
        snapshot.get("updated_at")
        or state.get("updated_at")
        or ""
    )
    try:
        ttl_seconds = int(
            snapshot.get("ttl_seconds")
            or state.get("ttl_seconds")
            or 300
        )
    except (TypeError, ValueError):
        ttl_seconds = 300
    freshness = "unknown"
    age_seconds: int | None = None
    if observed_at:
        try:
            parsed = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0,
                int(
                    (
                        datetime.now(timezone.utc)
                        - parsed.astimezone(timezone.utc)
                    ).total_seconds()
                ),
            )
            freshness = (
                "fresh"
                if age_seconds <= max(0, ttl_seconds)
                else "stale"
            )
        except ValueError:
            freshness = "invalid"
    snapshot["freshness"] = {
        "status": freshness,
        "age_seconds": age_seconds,
        "ttl_seconds": ttl_seconds,
    }
    if freshness in {"stale", "invalid"}:
        snapshot["observed_status"] = snapshot.get("status")
        snapshot["observed_connected"] = snapshot.get("connected")
        snapshot["status"] = "snapshot_stale"
        snapshot["connected"] = False
    snapshot["snapshot_source"] = "executor_state"
    snapshot["snapshot_updated_at"] = state.get("updated_at")
    return snapshot


def _task_context_for_callback(loop: Any, task_id: str) -> tuple[dict[str, Any], bool]:
    getter = getattr(loop.task_tracker, "get_context", None)
    if callable(getter):
        try:
            resolved = getter(task_id, authoritative_only=True)
        except TypeError:
            try:
                resolved = getter(task_id)
            except Exception:
                resolved = None
        except Exception:
            resolved = None
        if isinstance(resolved, dict):
            nested = resolved.get("task_context")
            if isinstance(nested, dict):
                return (
                    (dict(nested), True)
                    if str(nested.get("authority") or "") == "veyra_registered"
                    else ({}, False)
                )
            if resolved and str(resolved.get("authority") or "") in {"", "veyra_registered"}:
                return dict(resolved), True
    state = loop.state_store.read_json("task_state.json")
    pending = state.get("pending_agent_tasks") if isinstance(state.get("pending_agent_tasks"), list) else []
    for item in pending:
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id:
            embedded = item.get("task_context") if isinstance(item.get("task_context"), dict) else {}
            if embedded:
                if str(embedded.get("authority") or "") != "veyra_registered":
                    return {}, False
                return dict(embedded), True
            return dict(item), True
    return {}, False


def _memory_provider_for_callback(task_context: dict[str, Any], execution: ExecutionResult) -> str:
    task_packet = task_context.get("task_packet") if isinstance(task_context.get("task_packet"), dict) else {}
    provider = str(
        task_context.get("memory_provider")
        or task_context.get("target_agent")
        or task_packet.get("target_agent")
        or execution.executor
        or "selected"
    ).strip()
    return "selected" if provider in {"", "external", "unknown"} else provider


def _is_case_context(
    task_context: dict[str, Any], context_found: bool
) -> bool:
    return bool(
        context_found
        and str(task_context.get("case_id") or "").strip()
    )


def _require_task_owner(
    task_context: dict[str, Any],
    context_found: bool,
    *,
    user_id: str,
    session_id: str,
) -> tuple[str, str]:
    user_scope = _required_memory_identifier(user_id, "user_id")
    session_scope = _required_memory_identifier(session_id, "session_id")
    if (
        not context_found
        or str(task_context.get("user_id") or "").strip() != user_scope
        or str(task_context.get("session_id") or "").strip()
        != session_scope
    ):
        # Do not disclose whether an unowned task id exists.
        raise HTTPException(status_code=404, detail="Agent task not found")
    return user_scope, session_scope


def _adapter_for_context(loop: Any, task_context: dict[str, Any]) -> Any:
    target = str(
        task_context.get("target_agent")
        or task_context.get("executor")
        or ""
    ).strip()
    if not target:
        return loop.agent_registry.selected()
    names = {str(item) for item in loop.agent_registry.names()}
    if target not in names:
        raise HTTPException(
            status_code=409,
            detail=f"registered Agent runtime is unavailable: {target}",
        )
    return loop.agent_registry.get(target)


def _accept_case_execution(
    loop: Any,
    *,
    execution: ExecutionResult,
    task_context: dict[str, Any],
) -> dict[str, Any]:
    try:
        return loop.bounded_negotiation.accept_registered_execution(
            execution=execution,
            task_context=task_context,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Durable Case callback rejected: {str(exc)[:500]}",
        ) from exc


def _sync_phase6_case_projection(
    deps: dict[str, Any],
    *,
    task_context: dict[str, Any],
) -> None:
    runtime_task_id = str(
        task_context.get("runtime_task_id") or ""
    )
    task_packet_id = str(
        task_context.get("task_packet_id") or ""
    )
    if not (
        runtime_task_id.startswith("veyra-p6-")
        or task_packet_id.startswith("p6task_")
    ):
        return
    collaboration = deps.get("phase6_collaboration")
    if collaboration is None:
        return
    try:
        collaboration.reconcile_case_projection(
            case_id=str(task_context["case_id"]),
            user_id=str(task_context["user_id"]),
            workspace_id=str(task_context["case_workspace_id"]),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Phase 6 Case advanced but its public projection could "
                f"not be reconciled: {str(exc)[:400]}"
            ),
        ) from exc


def _fetch_registered_case_execution(
    loop: Any,
    *,
    task_context: dict[str, Any],
) -> ExecutionResult:
    try:
        execution = loop.bounded_negotiation.fetch_registered_execution(
            task_context=task_context,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Durable Case run could not be observed exactly: "
                f"{str(exc)[:500]}"
            ),
        ) from exc
    return execution


def build_agent_memory_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/agent/tasks/{task_id}")
    async def agent_task_status(
        task_id: str,
        *,
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        task_context, context_found = _task_context_for_callback(
            loop, task_id
        )
        _require_task_owner(
            task_context,
            context_found,
            user_id=user_id,
            session_id=session_id,
        )
        task_state = deps["state_store"].read_json("task_state.json")
        pending = (
            task_state.get("pending_agent_tasks")
            if isinstance(task_state.get("pending_agent_tasks"), list)
            else []
        )
        runtime_task_id = str(
            task_context.get("runtime_task_id") or task_id
        )
        pending_item = next(
            (
                item
                for item in pending
                if isinstance(item, dict)
                and str(item.get("task_id") or "") == runtime_task_id
                and str(item.get("user_id") or "") == user_id.strip()
                and str(item.get("session_id") or "")
                == session_id.strip()
            ),
            None,
        )
        task_status = str(
            (pending_item or {}).get("status")
            or task_context.get("final_status")
            or "not_observed"
        )
        verification_status = str(
            (pending_item or {}).get("verification_status")
            or task_context.get("verification_status")
            or "unverified"
        )
        durable_case = None
        if _is_case_context(task_context, context_found):
            case = loop.durable_case_store.get_case(
                case_id=str(task_context["case_id"]),
                user_id=str(task_context["user_id"]),
                workspace_id=str(task_context["case_workspace_id"]),
            )
            durable_case = (
                loop.bounded_negotiation.public_case_summary(case)
            )
        return {
            "status": "cached",
            "task": {
                "status": task_status,
                "verification_status": verification_status,
                "next_action": (
                    (pending_item or {}).get("next_action")
                    or (
                        "POST /agent/tasks/{task_id}/refresh"
                        if pending_item
                        else None
                    )
                ),
                "updated_at": (
                    (pending_item or {}).get("last_polled_at")
                    or task_context.get("updated_at")
                ),
            },
            "durable_case": durable_case,
            "snapshot_source": "task_state",
        }

    @router.post("/agent/tasks/{task_id}/refresh")
    async def agent_task_refresh(
        task_id: str,
        *,
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        task_context, context_found = _task_context_for_callback(
            loop, task_id
        )
        user_scope, session_scope = _require_task_owner(
            task_context,
            context_found,
            user_id=user_id,
            session_id=session_id,
        )
        adapter = _adapter_for_context(loop, task_context)
        runtime_task_id = str(
            task_context.get("runtime_task_id") or task_id
        )
        case_bound = _is_case_context(task_context, context_found)
        execution = (
            _fetch_registered_case_execution(
                loop,
                task_context=task_context,
            )
            if case_bound
            else adapter.fetch_task_status(runtime_task_id)
        )
        negotiation = (
            _accept_case_execution(
                loop,
                execution=execution,
                task_context=task_context,
            )
            if case_bound
            else None
        )
        if isinstance(negotiation, dict):
            _sync_phase6_case_projection(
                deps,
                task_context=task_context,
            )
        verified = (
            negotiation["verification"]
            if isinstance(negotiation, dict)
            else loop.verifier.verify_execution_result(execution)
        )
        trace = loop.execution_trace.record(
            {
                "event_id": f"poll_{task_id}",
                "route": "agent",
                "task_id": execution.task_id,
                "executor": execution.executor,
                "status": verified["status"],
                "execution_result": (
                    loop.bounded_negotiation.public_execution_summary(
                        execution
                    )
                    if case_bound
                    else execution.to_dict()
                ),
                "verification": verified,
            }
        )
        state_store.patch_json(
            "task_state.json",
            {
                "current_task": {
                    "task_id": task_id,
                    "user_id": user_scope,
                    "session_id": session_scope,
                    "route": "agent",
                    "status": verified["status"],
                }
            },
        )
        loop.task_tracker.apply_result(execution=execution, verification=verified, event_id=f"poll_{task_id}")
        return {
            "execution_result": (
                loop.bounded_negotiation.public_execution_summary(
                    execution
                )
                if case_bound
                else execution.to_dict()
            ),
            "verification": verified,
            "execution_trace": (
                loop.bounded_negotiation.public_trace_summary(trace)
                if case_bound
                else trace
            ),
            "durable_case": (
                negotiation.get("case")
                if isinstance(negotiation, dict)
                else None
            ),
            "agent_dialogue": (
                negotiation.get("dialogue_message")
                if isinstance(negotiation, dict)
                else None
            ),
        }

    @router.post("/agent/tasks/refresh")
    async def agent_tasks_refresh() -> dict[str, Any]:
        loop = deps["awareness_loop"]
        selected_adapter = loop.agent_registry.selected()
        case_recovery = await run_in_threadpool(
            loop.bounded_negotiation.recover_pending,
            limit=20,
            reason="agent_tasks_refresh",
        )
        legacy = await run_in_threadpool(
            loop.task_tracker.refresh_pending,
            selected_adapter,
            loop.verifier,
        )
        return {
            "status": (
                "degraded"
                if case_recovery.get("status") == "degraded"
                else legacy.get("status", "success")
            ),
            "durable_cases": case_recovery,
            "legacy_tasks": {
                "status": legacy.get("status", "success"),
                "pruned_count": int(
                    legacy.get("pruned_count") or 0
                ),
                "refreshed_count": len(
                    legacy.get("refreshed", [])
                    if isinstance(legacy.get("refreshed"), list)
                    else []
                ),
                "remaining_count": int(
                    legacy.get("remaining_count") or 0
                ),
            },
        }

    @router.post("/agent/results")
    async def agent_result_callback(request: AgentResultRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        task_context, context_found = _task_context_for_callback(loop, request.task_id)
        case_bound = _is_case_context(task_context, context_found)
        if (
            request.task_id.startswith(("p6task_", "veyra-p6-"))
            and (
                not case_bound
                or str(task_context.get("authority") or "")
                != "veyra_registered"
            )
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Phase 6 callback requires a Veyra-registered "
                    "Durable Case binding"
                ),
            )
        if case_bound:
            registered_run_id = str(
                task_context.get("runtime_task_id") or ""
            ).strip()
            if (
                registered_run_id
                and request.task_id != registered_run_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Durable Case callback task_id does not match "
                        "the registered runtime_task_id"
                    ),
                )
            expected_executor = str(
                task_context.get("target_agent") or ""
            ).strip()
            if request.executor != expected_executor:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Durable Case callback executor does not match "
                        "the registered target_agent"
                    ),
                )
            # A callback is only a wake-up hint. Its status, dialogue, raw
            # evidence and cleanup claims are untrusted; re-fetch the exact
            # persisted run through its dispatch-time adapter binding.
            execution = _fetch_registered_case_execution(
                loop,
                task_context=task_context,
            )
        else:
            execution = ExecutionResult(
                task_id=request.task_id,
                executor=request.executor,
                status=request.status,
                result=request.result,
                logs=request.logs,
                changed_files=request.changed_files,
                tool_calls=request.tool_calls,
                raw=request.raw,
            )
        negotiation = (
            _accept_case_execution(
                loop,
                execution=execution,
                task_context=task_context,
            )
            if case_bound
            else None
        )
        if isinstance(negotiation, dict):
            _sync_phase6_case_projection(
                deps,
                task_context=task_context,
            )
        verified = (
            negotiation["verification"]
            if isinstance(negotiation, dict)
            else loop.verifier.verify_execution_result(execution)
        )
        trace = loop.execution_trace.record(
            {
                "event_id": f"callback_{execution.task_id}",
                "route": "agent",
                "task_id": execution.task_id,
                "executor": execution.executor,
                "status": verified["status"],
                "execution_result": (
                    loop.bounded_negotiation.public_execution_summary(
                        execution
                    )
                    if case_bound
                    else execution.to_dict()
                ),
                "verification": verified,
                "durable_case": (
                    negotiation.get("case")
                    if isinstance(negotiation, dict)
                    else None
                ),
                "callback_context": {
                    "context_found": context_found,
                    "session_id": task_context.get("session_id"),
                    "correlation_id": task_context.get("correlation_id")
                    or task_context.get("event_id")
                    or task_context.get("task_packet_id"),
                    "memory_policy": task_context.get("memory_policy"),
                },
            }
        )
        task_update = loop.task_tracker.apply_result(execution=execution, verification=verified, event_id=f"callback_{execution.task_id}")
        provider = _memory_provider_for_callback(task_context, execution)
        memory_runtime = getattr(loop, "memory_policy_runtime", None)
        if case_bound:
            memory_policy_execution = {
                "status": "skipped",
                "policy": str(
                    task_context.get("memory_policy") or "forget"
                ),
                "reason": (
                    "Durable Case dialogue is a proposal and cannot authorize "
                    "Agent-result memory"
                ),
            }
        elif memory_runtime is None or not hasattr(memory_runtime, "apply_agent_result"):
            memory_policy_execution = {
                "status": "skipped",
                "policy": str(task_context.get("memory_policy") or "forget"),
                "reason": "memory policy runtime unavailable",
            }
        else:
            memory_policy_execution = memory_runtime.apply_agent_result(
                task_context=task_context,
                execution=execution,
                verification=verified,
                context_found=context_found,
                long_term_writer=lambda patch: loop.memory_bridge.write_patch(patch, provider=provider),
            )
        memory_write = (
            memory_policy_execution.get("write")
            if isinstance(memory_policy_execution.get("write"), dict)
            else None
        )
        return {
            "status": verified["status"],
            "execution_result": (
                loop.bounded_negotiation.public_execution_summary(
                    execution
                )
                if case_bound
                else execution.to_dict()
            ),
            "verification": verified,
            "execution_trace": (
                loop.bounded_negotiation.public_trace_summary(trace)
                if case_bound
                else trace
            ),
            "task_update": (
                {
                    "matched": bool(task_update.get("matched")),
                    "status": task_update.get("status"),
                    "pending_count": int(
                        task_update.get("pending_count") or 0
                    ),
                }
                if case_bound
                else task_update
            ),
            "durable_case": (
                negotiation.get("case")
                if isinstance(negotiation, dict)
                else None
            ),
            "agent_dialogue": (
                negotiation.get("dialogue_message")
                if isinstance(negotiation, dict)
                else None
            ),
            "memory_write": memory_write,
            "memory_policy_execution": memory_policy_execution,
            "callback_context": {
                "context_found": context_found,
                "memory_policy": task_context.get("memory_policy") or "forget",
                "provider": provider,
                **(
                    {}
                    if case_bound
                    else {
                        "session_id": task_context.get("session_id"),
                        "correlation_id": (
                            task_context.get("correlation_id")
                            or task_context.get("event_id")
                            or task_context.get("task_packet_id")
                        ),
                    }
                ),
            },
        }

    @router.post("/agent/tasks/{task_id}/stop")
    async def agent_task_stop(
        task_id: str,
        *,
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        task_context, context_found = _task_context_for_callback(
            loop, task_id
        )
        _require_task_owner(
            task_context,
            context_found,
            user_id=user_id,
            session_id=session_id,
        )
        if _is_case_context(task_context, context_found):
            case = await run_in_threadpool(
                loop.durable_case_store.get_case,
                case_id=str(task_context["case_id"]),
                user_id=str(task_context["user_id"]),
                workspace_id=str(task_context["case_workspace_id"]),
            )
            if str(case.get("status") or "") == "CANCELLED":
                result = {
                    "status": "cancelled",
                    "case": loop.bounded_negotiation.public_case_summary(
                        case
                    ),
                    "cancellation": {"receipt_replayed": True},
                }
            else:
                result = await run_in_threadpool(
                    loop.bounded_negotiation.cancel_case,
                    case_id=str(task_context["case_id"]),
                    user_id=str(task_context["user_id"]),
                    workspace_id=str(
                        task_context["case_workspace_id"]
                    ),
                    expected_revision=int(case["revision"]),
                    operation_id=(
                        f"agent_stop:{case['case_id']}:"
                        f"{case['revision']}:{task_id}"
                    ),
                    reason="explicit_agent_task_stop",
                )
            stopped = result.get("status") == "cancelled"
        else:
            adapter = _adapter_for_context(
                loop, task_context
            )
            runtime_task_id = str(
                task_context.get("runtime_task_id") or task_id
            )
            cancellation = await run_in_threadpool(
                adapter.cancel_task_authority,
                runtime_task_id,
                reason="explicit_agent_task_stop",
            )
            result = {
                "status": cancellation.get("status"),
                "cancellation": (
                    loop.bounded_negotiation._public_cancellation(
                        cancellation
                    )
                ),
            }
            stopped = cancellation.get("status") == "cancelled"
        state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "agent_stop",
                "status": "stopped" if stopped else "not_stopped",
                "artifacts": {
                    "task_id": task_id,
                    "case_id": task_context.get("case_id"),
                },
            },
        )
        response = {
            "stopped": stopped,
            "result": result,
        }
        if _is_case_context(task_context, context_found):
            case_summary = (
                result.get("case")
                if isinstance(result.get("case"), dict)
                else {}
            )
            response["case_id"] = case_summary.get("case_id")
            response["case_revision"] = case_summary.get("revision")
        else:
            response["task_id"] = task_id
        return response

    @router.get("/agent/status")
    async def agent_status() -> dict[str, Any]:
        return _agent_status_snapshot(deps)

    @router.get("/agent/contract")
    async def agent_contract() -> dict[str, Any]:
        return {**contract_summary(), "tool_proxy_contract": agent_tool_proxy_contract()}

    @router.get("/agents")
    async def agents() -> dict[str, Any]:
        state = deps["state_store"].read_json("executor_state.json")
        state = state if isinstance(state, dict) else {}
        agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
        selected_status = _agent_status_snapshot(deps)
        selected = str(
            state.get("selected_agent")
            or selected_status.get("name")
            or "openclaw"
        )
        if selected not in agents:
            agents = {**agents, selected: selected_status}
        return {
            "selected_agent": selected,
            "selected_status": selected_status,
            "agents": agents,
            "snapshot_source": "executor_state",
            "snapshot_updated_at": state.get("updated_at"),
        }

    @router.get("/agents/certification")
    async def agents_certification_status() -> dict[str, Any]:
        return deps["runtime_matrix"].status()

    @router.post("/agents/certification/run")
    async def agents_certification_run(write_memory_probe: bool = False) -> dict[str, Any]:
        return deps["runtime_matrix"].run(write_memory_probe=write_memory_probe)

    @router.post("/agents/invoke")
    async def agents_invoke(request: AgentInvokeRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            deps["agent_orchestrator"].invoke,
            text=request.text,
            agents=request.agents,
            mode=request.mode,
            channel=request.channel,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    @router.post("/agents/governance-canary")
    async def run_governance_canary(
        request: GovernanceCanaryRunRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                deps["agent_orchestrator"].invoke_governance_canary,
                suffix=request.suffix,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=str(exc)[:400]
            ) from exc

    @router.post("/agents/select")
    async def select_agent(request: AgentSelectRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        runtime_entity = deps["runtime_entity"]
        try:
            status = loop.agent_registry.select(request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime_entity.set_selected_agent(request.name)
        return status

    @router.post("/agents/{name}/config")
    async def configure_agent(name: str, request: AgentConfigRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        payload = request.model_dump()
        if payload.get("model_decision_mode") and payload["model_decision_mode"] not in {"auto", "always"}:
            raise HTTPException(status_code=422, detail="model_decision_mode must be 'auto' or 'always'")
        status = loop.agent_registry.upsert(name, payload)
        return status

    @router.get("/memory/summary")
    async def memory_summary(
        *,
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        provider: str = Query(default="selected", min_length=1, max_length=120),
    ) -> dict[str, Any]:
        bridge = deps["awareness_loop"].memory_bridge
        user_scope = _required_memory_identifier(user_id, "user_id")
        session_scope = _required_memory_identifier(
            session_id,
            "session_id",
        )
        provider_name = _memory_provider(bridge, provider)
        return bridge.read_summary(
            session_scope,
            provider=provider_name,
            user_id=user_scope,
            model_assist=False,
            external_read=False,
        )

    @router.post("/memory/summary/resolve")
    async def memory_summary_resolve(
        request: MemorySummaryRequest,
    ) -> dict[str, Any]:
        bridge = deps["awareness_loop"].memory_bridge
        provider = _memory_provider(bridge, request.provider)
        user_scope = _required_memory_identifier(
            request.user_id,
            "user_id",
        )
        session_scope = _required_memory_identifier(
            request.session_id,
            "session_id",
        )
        focus = [
            value
            for value in (
                str(item or "").strip()[:240]
                for item in request.focus
            )
            if value
        ]
        return await run_in_threadpool(
            bridge.read_summary,
            session_scope,
            focus,
            provider,
            user_id=user_scope,
            model_assist=True,
            external_read=True,
        )

    @router.get("/memory/providers")
    async def memory_providers() -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.provider_status(
            probe=False
        )

    @router.get("/memory/providers/diagnostics")
    async def memory_provider_diagnostics(
        provider: str = Query(default="all", min_length=1, max_length=120),
        user_id: str = Query(default="local-user", min_length=1, max_length=240),
        session_id: str = Query(
            default="memory-diagnostics",
            min_length=1,
            max_length=240,
        ),
    ) -> dict[str, Any]:
        bridge = deps["awareness_loop"].memory_bridge
        return bridge.provider_diagnostics(
            provider=_memory_provider(bridge, provider),
            user_id=_required_memory_identifier(user_id, "user_id"),
            session_id=_required_memory_identifier(
                session_id,
                "session_id",
            ),
            write_probe=False,
            record=False,
            persist=False,
            active_probe=False,
        )

    @router.post("/memory/providers/diagnostics")
    async def memory_provider_diagnostics_write(request: MemoryDiagnosticsRequest) -> dict[str, Any]:
        bridge = deps["awareness_loop"].memory_bridge
        return bridge.provider_diagnostics(
            provider=_memory_provider(bridge, request.provider),
            user_id=_required_memory_identifier(
                request.user_id,
                "user_id",
            ),
            session_id=_required_memory_identifier(
                request.session_id,
                "session_id",
            ),
            write_probe=request.write_probe,
            record=True,
            persist=True,
            active_probe=True,
        )

    @router.post("/memory/patch")
    async def memory_patch(request: MemoryPatchRequest) -> dict[str, Any]:
        bridge = deps["awareness_loop"].memory_bridge
        provider = _memory_provider(bridge, request.provider)
        user_scope = _required_memory_identifier(
            request.user_id,
            "user_id",
        )
        session_scope = _required_memory_identifier(
            request.session_id,
            "session_id",
        )
        supplied_patch = dict(request.patch)
        for field, expected in (
            ("user_id", user_scope),
            ("session_id", session_scope),
        ):
            supplied = str(supplied_patch.get(field) or "").strip()
            if supplied and supplied != expected:
                raise HTTPException(
                    status_code=409,
                    detail=f"memory patch {field} conflicts with request scope",
                )
        patch = {
            field: supplied_patch[field]
            for field in PUBLIC_MEMORY_CONTENT_FIELDS
            if field in supplied_patch
        }
        if not patch:
            raise HTTPException(
                status_code=422,
                detail="memory patch contains no supported caller content",
            )
        patch["user_id"] = user_scope
        patch["session_id"] = session_scope
        patch["trust"] = "caller_attested"
        patch["freshness"] = "fresh"
        return bridge.write_patch(
            patch,
            provider=provider,
        )

    return router
