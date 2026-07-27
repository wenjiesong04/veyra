from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from interface.agent_adapter import ExecutionResult
from interface.agent_contract import contract_summary
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


class MemoryPatchRequest(BaseModel):
    patch: dict[str, Any]
    provider: str = "selected"


class MemoryDiagnosticsRequest(BaseModel):
    provider: str = "all"
    session_id: str = "memory-diagnostics"
    write_probe: bool = False


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


class AgentResultRequest(BaseModel):
    task_id: str
    executor: str = "external"
    status: str
    result: str = ""
    logs: str = ""
    changed_files: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


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


def build_agent_memory_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/agent/tasks/{task_id}")
    async def agent_task_status(task_id: str) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        loop.agent_adapter = loop.agent_registry.selected()
        execution = loop.agent_adapter.fetch_task_status(task_id)
        verified = loop.verifier.verify_execution_result(execution)
        trace = loop.execution_trace.record(
            {
                "event_id": f"poll_{task_id}",
                "route": "agent",
                "task_id": execution.task_id,
                "executor": execution.executor,
                "status": verified["status"],
                "execution_result": execution.to_dict(),
                "verification": verified,
            }
        )
        state_store.patch_json("task_state.json", {"current_task": {"task_id": task_id, "route": "agent", "status": verified["status"]}})
        loop.task_tracker.apply_result(execution=execution, verification=verified, event_id=f"poll_{task_id}")
        return {"execution_result": execution.to_dict(), "verification": verified, "execution_trace": trace}

    @router.post("/agent/tasks/refresh")
    async def agent_tasks_refresh() -> dict[str, Any]:
        loop = deps["awareness_loop"]
        loop.agent_adapter = loop.agent_registry.selected()
        return loop.task_tracker.refresh_pending(loop.agent_adapter, loop.verifier)

    @router.post("/agent/results")
    async def agent_result_callback(request: AgentResultRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        task_context, context_found = _task_context_for_callback(loop, request.task_id)
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
        verified = loop.verifier.verify_execution_result(execution)
        trace = loop.execution_trace.record(
            {
                "event_id": f"callback_{execution.task_id}",
                "route": "agent",
                "task_id": execution.task_id,
                "executor": execution.executor,
                "status": verified["status"],
                "execution_result": execution.to_dict(),
                "verification": verified,
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
        if memory_runtime is None or not hasattr(memory_runtime, "apply_agent_result"):
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
            "execution_result": execution.to_dict(),
            "verification": verified,
            "execution_trace": trace,
            "task_update": task_update,
            "memory_write": memory_write,
            "memory_policy_execution": memory_policy_execution,
            "callback_context": {
                "context_found": context_found,
                "session_id": task_context.get("session_id"),
                "correlation_id": task_context.get("correlation_id")
                or task_context.get("event_id")
                or task_context.get("task_packet_id"),
                "memory_policy": task_context.get("memory_policy") or "forget",
                "provider": provider,
            },
        }

    @router.post("/agent/tasks/{task_id}/stop")
    async def agent_task_stop(task_id: str) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        loop.agent_adapter = loop.agent_registry.selected()
        stopped = loop.agent_adapter.stop_task(task_id)
        state_store.append_jsonl("action_record.jsonl", {"route": "agent_stop", "status": "stopped" if stopped else "not_stopped", "artifacts": {"task_id": task_id}})
        return {"task_id": task_id, "stopped": stopped}

    @router.get("/agent/status")
    async def agent_status() -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        loop.agent_adapter = loop.agent_registry.selected()
        status = loop.agent_adapter.connection_status()
        state_store.patch_json("executor_state.json", {"selected_agent": loop.agent_registry.selected_name(), **status})
        return status

    @router.get("/agent/contract")
    async def agent_contract() -> dict[str, Any]:
        return {**contract_summary(), "tool_proxy_contract": agent_tool_proxy_contract()}

    @router.get("/agents")
    async def agents() -> dict[str, Any]:
        loop = deps["awareness_loop"]
        state_store = deps["state_store"]
        status = loop.agent_registry.list_status()
        state_store.patch_json("executor_state.json", status)
        return status

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

    @router.post("/agents/select")
    async def select_agent(request: AgentSelectRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        runtime_entity = deps["runtime_entity"]
        try:
            status = loop.agent_registry.select(request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime_entity.set_selected_agent(request.name)
        loop.agent_adapter = loop.agent_registry.selected()
        return status

    @router.post("/agents/{name}/config")
    async def configure_agent(name: str, request: AgentConfigRequest) -> dict[str, Any]:
        loop = deps["awareness_loop"]
        payload = request.model_dump()
        if payload.get("model_decision_mode") and payload["model_decision_mode"] not in {"auto", "always"}:
            raise HTTPException(status_code=422, detail="model_decision_mode must be 'auto' or 'always'")
        status = loop.agent_registry.upsert(name, payload)
        if status.get("selected_agent") == name:
            loop.agent_adapter = loop.agent_registry.selected()
        return status

    @router.get("/memory/summary")
    async def memory_summary(session_id: str = "console-session", provider: str = "selected") -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.read_summary(session_id, provider=provider)

    @router.get("/memory/providers")
    async def memory_providers() -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.provider_status()

    @router.get("/memory/providers/diagnostics")
    async def memory_provider_diagnostics(provider: str = "all", session_id: str = "memory-diagnostics") -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.provider_diagnostics(provider=provider, session_id=session_id, write_probe=False)

    @router.post("/memory/providers/diagnostics")
    async def memory_provider_diagnostics_write(request: MemoryDiagnosticsRequest) -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.provider_diagnostics(
            provider=request.provider,
            session_id=request.session_id,
            write_probe=request.write_probe,
        )

    @router.post("/memory/patch")
    async def memory_patch(request: MemoryPatchRequest) -> dict[str, Any]:
        return deps["awareness_loop"].memory_bridge.write_patch(request.patch, provider=request.provider)

    return router
