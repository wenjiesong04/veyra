from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.architecture import architecture_snapshot
from core.agency_core import AgencyCore
from core.awareness_loop import AwarenessLoop
from core.definitions import RiskLevel, classify_text_risk, lifecycle_statuses, normalize_risk, operational_modes, risk_catalog
from core.foresight_engine import ForesightEngine
from core.model_client import redact_sensitive
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from execution.action_executor import ActionExecutor
from guardian.review_queue import ReviewQueue
from interface.agent_contract import contract_summary
from interface.agent_adapter import ExecutionResult
from interface.event_normalizer import EventNormalizer
from interface.event_schema import Decision, Route, utc_now_iso
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from runtime.proactive_checks import ProactiveChecks
from runtime.retention_policy import RetentionPolicy
from runtime.soak_runner import SoakRunner
from runtime.state_refresh import StateRefresh
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell
from runtime.safety_validation import SafetyValidation


app = FastAPI(title="Veyra", version="0.1.0")

state_store = WorldStateStore()
runtime_entity = RuntimeEntity(state_store=state_store)
awareness_loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime_entity)
event_normalizer = EventNormalizer()
console_dir = Path("ui/console")
review_queue = ReviewQueue(state_store)
rollback_manager = RollbackManager(state_store)
safe_shell = SafeShell(state_store=state_store)
safe_file = SafeFile(state_store=state_store)
safe_browser = SafeBrowser(state_store=state_store)
safe_api = SafeAPI(state_store=state_store)
action_executor = ActionExecutor(state_store=state_store)
foresight_engine = ForesightEngine(reasoning=awareness_loop.core_reasoning)
proactive_checks = ProactiveChecks(state_store, reasoning=awareness_loop.core_reasoning)
diff_tracker = DiffTracker()
agency_core = AgencyCore(state_store, reasoning=awareness_loop.core_reasoning)
safety_validation = SafetyValidation()
retention_policy = RetentionPolicy(state_store)
state_refresh = StateRefresh(state_store, reasoning=awareness_loop.core_reasoning)
soak_runner = SoakRunner(
    proactive_checks=proactive_checks,
    task_tracker=awareness_loop.task_tracker,
    state_refresh=state_refresh,
    retention_policy=retention_policy,
    safety_validation=safety_validation,
    adapter_resolver=lambda: awareness_loop.agent_registry.selected(),
    verifier=awareness_loop.verifier,
)


class MessageRequest(BaseModel):
    text: str
    channel: str = "webhook"
    user_id: str = "local-user"
    session_id: str = "local-session"


class ReviewDecisionRequest(BaseModel):
    reason: str = ""


class ShellRequest(BaseModel):
    command: list[str] = Field(min_length=1)


class FileReadRequest(BaseModel):
    path: str


class FileWriteRequest(BaseModel):
    path: str
    content: str
    reason: str = ""


class BrowserOpenRequest(BaseModel):
    url: str


class APIProxyRequest(BaseModel):
    payload: dict[str, Any]


class MemoryPatchRequest(BaseModel):
    patch: dict[str, Any]


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


class CoreModelConfigRequest(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    timeout: float | None = None
    decision_mode: str | None = None


class ActionProposalRequest(BaseModel):
    proposal_id: str | None = None
    task_id: str | None = None
    agent: str = "external"
    action: dict[str, Any]
    risk_guess: str = "R2"
    reversible: str = "unknown"
    reason: str = ""


class AgentResultRequest(BaseModel):
    task_id: str
    executor: str = "external"
    status: str
    result: str = ""
    logs: str = ""
    changed_files: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SoakRequest(BaseModel):
    iterations: int = 1


@app.get("/")
async def root():
    return {
        "name": runtime_entity.identity.name,
        "definition": "Virtual Entity for Yielding Real-time Awareness",
        "status": runtime_entity.lifecycle.status,
    }

@app.get("/console")
async def console():
    index = console_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {
        "status": "console_not_built",
        "next_step": "Run `cd web && npm install && npm run build`.",
    }


@app.post("/events/message")
async def message(request: MessageRequest):
    event = event_normalizer.user_message(
        text=request.text,
        channel=request.channel,
        user_id=request.user_id,
        session_id=request.session_id,
    )
    return awareness_loop.handle_event(event).to_dict()


@app.get("/state")
async def state():
    payload = _public_state(state_store.read_all())
    payload["agency"] = agency_core.state()
    return payload


@app.get("/core/model/status")
async def core_model_status():
    return awareness_loop.core_reasoning.status()


@app.post("/core/model/config")
async def configure_core_model(request: CoreModelConfigRequest):
    patch = request.model_dump(exclude_none=True)
    if "decision_mode" in patch and patch["decision_mode"] not in {"auto", "always"}:
        raise HTTPException(status_code=422, detail="decision_mode must be 'auto' or 'always'")
    if "provider" in patch and patch["provider"] != "openai_compatible":
        raise HTTPException(status_code=422, detail="only openai_compatible provider is supported")
    config = state_store.read_json("agent_config.json")
    core_model = config.setdefault("core_model", {})
    core_model.update(patch)
    state_store.write_json("agent_config.json", config)
    return awareness_loop.core_reasoning.status()


@app.get("/architecture")
async def architecture():
    return architecture_snapshot()


@app.get("/definitions")
async def definitions():
    return {
        "risk_levels": risk_catalog(),
        "lifecycle_statuses": lifecycle_statuses(),
        "operational_modes": operational_modes(),
    }


@app.get("/heartbeat")
async def heartbeat():
    return {"heartbeat": state_store.read_text("heartbeat.md")}


@app.get("/logs/events")
async def event_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("event_log.jsonl", limit=limit)}


@app.get("/logs/actions")
async def action_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("action_record.jsonl", limit=limit)}


@app.get("/reviews/actions")
async def action_reviews(status: str | None = None, limit: int = 100):
    return {"items": review_queue.list(status=status)[-limit:]}


@app.post("/reviews/{review_id}/approve")
async def approve_review(review_id: str, request: ReviewDecisionRequest):
    try:
        review = review_queue.decide(review_id, "approved", request.reason)
        execution = action_executor.execute_review(review)
        return review_queue.update_execution(review_id, execution)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/reviews/{review_id}/reject")
async def reject_review(review_id: str, request: ReviewDecisionRequest):
    try:
        return review_queue.decide(review_id, "rejected", request.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tool-proxy/shell")
async def tool_proxy_shell(request: ShellRequest):
    result = safe_shell.run(request.command)
    if result.get("status") == "needs_confirmation":
        command_text = " ".join(request.command)
        review = review_queue.create(
            event_id=f"tool_{utc_now_iso()}",
            task_text=f"Confirm shell command: {command_text}",
            risk_level="R4",
            foresight=foresight_engine.predict_text_action(command_text, RiskLevel.R4),
            guardian_decision={"decision": "ask_user", "risk_level": "R4", "reason": result.get("review", {}).get("reason", "requires confirmation")},
            proposal={
                "agent": "tool_proxy",
                "action": {"type": "shell_command", "command": request.command},
                "risk_guess": "R4",
                "reversible": "partial",
            },
        )
        return {"status": "needs_confirmation", "review": review}
    return result


@app.post("/tool-proxy/file/read")
async def tool_proxy_file_read(request: FileReadRequest):
    return safe_file.read_text(request.path)


@app.post("/tool-proxy/file/write")
async def tool_proxy_file_write(request: FileWriteRequest):
    return safe_file.write_text(request.path, request.content, request.reason)


@app.post("/tool-proxy/browser/open")
async def tool_proxy_browser_open(request: BrowserOpenRequest):
    return safe_browser.open(request.url)


@app.post("/tool-proxy/api/request")
async def tool_proxy_api_request(request: APIProxyRequest):
    return safe_api.request(request.payload)


@app.post("/rollback/snapshot")
async def rollback_snapshot(request: FileReadRequest):
    return rollback_manager.snapshot_file(request.path, reason="manual_snapshot")


@app.post("/rollback/{snapshot_id}/restore")
async def rollback_restore(snapshot_id: str):
    try:
        return rollback_manager.restore(snapshot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/rollback/{snapshot_id}/diff")
async def rollback_snapshot_diff(snapshot_id: str):
    try:
        return rollback_manager.diff(snapshot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/logs/tools")
async def tool_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("tool_call_log.jsonl", limit=limit)}


@app.get("/logs/policy")
async def policy_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("policy_trace.jsonl", limit=limit)}


@app.get("/logs/execution")
async def execution_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("execution_trace.jsonl", limit=limit)}


@app.get("/logs/rollback")
async def rollback_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("rollback_log.jsonl", limit=limit)}


@app.get("/logs/memory")
async def memory_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("memory_log.jsonl", limit=limit)}


@app.get("/logs/core-model")
async def core_model_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("core_model_trace.jsonl", limit=limit)}


@app.post("/proactive/check")
async def proactive_check():
    return proactive_checks.run_read_only()


@app.post("/state/refresh-stale")
async def refresh_stale_state():
    return state_refresh.refresh_stale()


@app.get("/agency/intentions")
async def agency_intentions():
    return agency_core.state()


@app.get("/ops/safety/red-team")
async def ops_safety_red_team():
    return safety_validation.run()


@app.get("/ops/retention")
async def ops_retention():
    return retention_policy.summary()


@app.post("/ops/soak")
async def ops_soak(request: SoakRequest):
    return soak_runner.run(iterations=request.iterations)


@app.get("/rollback/diff")
async def rollback_diff(full: bool = False):
    return diff_tracker.git_diff_text() if full else diff_tracker.git_diff()


@app.post("/actions/proposals")
async def action_proposal(request: ActionProposalRequest):
    validation_error = _validate_action_proposal(request)
    if validation_error:
        raise HTTPException(status_code=422, detail=validation_error)
    action_text = _action_text(request.action)
    try:
        guessed_risk = normalize_risk(request.risk_guess)
    except ValueError:
        guessed_risk = RiskLevel.R2
    detected_risk = classify_text_risk(action_text)
    risk_order = list(RiskLevel)
    risk_level = detected_risk if risk_order.index(detected_risk) > risk_order.index(guessed_risk) else guessed_risk
    risk = risk_level.value
    proposal_decision = _proposal_decision(risk_level, action_text, request)
    foresight = foresight_engine.predict_text_action(action_text, risk_level, decision=proposal_decision.to_dict())
    guardian_decision = awareness_loop.guardian.review_text_action(
        text=action_text,
        decision=proposal_decision,
        foresight=foresight,
    )
    if risk == "R5":
        state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": request.proposal_id or "proposal",
                "route": "block",
                "status": "blocked",
                "artifacts": {"proposal": request.model_dump(), "guardian_decision": guardian_decision},
            },
        )
        return {"status": "blocked", "decision": "block", "risk_level": risk, "guardian_decision": guardian_decision}
    if risk in {"R3", "R4"}:
        review = review_queue.create(
            event_id=request.proposal_id or f"proposal_{utc_now_iso()}",
            task_text=request.reason or action_text,
            risk_level=risk,
            foresight=foresight,
            guardian_decision=guardian_decision,
            proposal=request.model_dump(),
        )
        return {"status": "needs_confirmation", "decision": "ask_user", "guardian_decision": guardian_decision, "review": review}
    execution = action_executor.execute_review(
        {
            "review_id": request.proposal_id or "direct_action",
            "proposal": request.model_dump(),
        }
    )
    state_store.append_jsonl(
        "action_record.jsonl",
        {
            "event_id": request.proposal_id or "direct_action",
            "route": "native_tool",
            "status": execution.get("status", "unknown"),
            "artifacts": {"proposal": request.model_dump(), "guardian_decision": guardian_decision, "execution_result": execution},
        },
    )
    return {
        "status": execution.get("status", "unknown"),
        "decision": guardian_decision.get("decision", "allow"),
        "guardian_decision": guardian_decision,
        "execution_result": execution,
    }


@app.get("/agent/tasks/{task_id}")
async def agent_task_status(task_id: str):
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    execution = awareness_loop.agent_adapter.fetch_task_status(task_id)
    verified = awareness_loop.verifier.verify_execution_result(execution)
    trace = awareness_loop.execution_trace.record(
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
    awareness_loop.task_tracker.apply_result(execution=execution, verification=verified, event_id=f"poll_{task_id}")
    return {"execution_result": execution.to_dict(), "verification": verified, "execution_trace": trace}


@app.post("/agent/tasks/refresh")
async def agent_tasks_refresh():
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    return awareness_loop.task_tracker.refresh_pending(awareness_loop.agent_adapter, awareness_loop.verifier)


@app.post("/agent/results")
async def agent_result_callback(request: AgentResultRequest):
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
    verified = awareness_loop.verifier.verify_execution_result(execution)
    trace = awareness_loop.execution_trace.record(
        {
            "event_id": f"callback_{execution.task_id}",
            "route": "agent",
            "task_id": execution.task_id,
            "executor": execution.executor,
            "status": verified["status"],
            "execution_result": execution.to_dict(),
            "verification": verified,
        }
    )
    task_update = awareness_loop.task_tracker.apply_result(execution=execution, verification=verified, event_id=f"callback_{execution.task_id}")
    memory_write = None
    if verified.get("needs_memory_patch"):
        memory_write = awareness_loop.memory_bridge.write_patch(
            {
                "session_id": str(request.raw.get("session_id") or "agent-callback"),
                "task": str(request.raw.get("task") or request.task_id),
                "executor": execution.executor,
                "status": execution.status,
                "result": execution.result,
            }
        )
    return {
        "status": verified["status"],
        "execution_result": execution.to_dict(),
        "verification": verified,
        "execution_trace": trace,
        "task_update": task_update,
        "memory_write": memory_write,
    }


@app.post("/agent/tasks/{task_id}/stop")
async def agent_task_stop(task_id: str):
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    stopped = awareness_loop.agent_adapter.stop_task(task_id)
    state_store.append_jsonl("action_record.jsonl", {"route": "agent_stop", "status": "stopped" if stopped else "not_stopped", "artifacts": {"task_id": task_id}})
    return {"task_id": task_id, "stopped": stopped}


def _validate_action_proposal(request: ActionProposalRequest) -> str | None:
    action_type = request.action.get("type")
    allowed = {"shell_command", "file_write", "file_read", "browser_open", "api_request"}
    if action_type not in allowed:
        return f"action.type must be one of {sorted(allowed)}"
    if action_type == "shell_command" and not request.action.get("command"):
        return "shell_command action requires command"
    if action_type in {"file_write", "file_read"} and not request.action.get("path"):
        return f"{action_type} action requires path"
    if action_type == "browser_open" and not request.action.get("url"):
        return "browser_open action requires url"
    if action_type == "api_request" and not isinstance(request.action.get("payload"), dict):
        return "api_request action requires payload object"
    return None


def _action_text(action: dict[str, Any]) -> str:
    if action.get("type") == "shell_command":
        command = action.get("command")
        return " ".join(str(part) for part in command) if isinstance(command, list) else str(command)
    if action.get("type") in {"file_write", "file_read"}:
        return str(action.get("path") or action)
    if action.get("type") == "browser_open":
        return str(action.get("url") or action)
    if action.get("type") == "api_request":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        return f"{payload.get('method', 'GET')} {payload.get('url') or payload.get('endpoint') or ''}"
    return str(action)


def _proposal_decision(risk_level: RiskLevel, action_text: str, request: ActionProposalRequest) -> Decision:
    if risk_level == RiskLevel.R5:
        route = Route.BLOCK
        capability = "guardian"
        constraints = ["block unsafe action", "require safer alternative"]
    elif risk_level in {RiskLevel.R3, RiskLevel.R4}:
        route = Route.HUMAN_REVIEW
        capability = "human_review"
        constraints = ["explain impact", "wait for explicit approval"]
    else:
        route = Route.NATIVE_TOOL
        capability = "tool_proxy"
        constraints = ["record tool trace", "return evidence"]
    return Decision(
        route=route,
        risk_level=risk_level,
        reason=request.reason or f"ActionProposal from {request.agent}: {action_text}",
        requires_confirmation=risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
        intent="action",
        complexity="simple" if risk_level in {RiskLevel.R0, RiskLevel.R1, RiskLevel.R2} else "moderate",
        capability=capability,
        signals=[f"risk:{risk_level.value}", f"agent:{request.agent}", "action_proposal"],
        constraints=constraints,
    )


def _public_state(payload: dict[str, Any]) -> dict[str, Any]:
    public = redact_sensitive(payload)
    agent_config = public.get("agent_config") if isinstance(public.get("agent_config"), dict) else {}
    if isinstance(agent_config, dict):
        core_model = agent_config.get("core_model") if isinstance(agent_config.get("core_model"), dict) else {}
        if isinstance(core_model, dict):
            original = payload.get("agent_config", {}).get("core_model", {}) if isinstance(payload.get("agent_config"), dict) else {}
            core_model["api_key_set"] = bool(original.get("api_key")) if isinstance(original, dict) else False
            core_model["api_key"] = "<redacted>" if core_model.get("api_key") else ""
    return public


@app.get("/agent/status")
async def agent_status():
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    status = awareness_loop.agent_adapter.connection_status()
    state_store.patch_json("executor_state.json", {"selected_agent": awareness_loop.agent_registry.selected_name(), **status})
    return status


@app.get("/agent/contract")
async def agent_contract():
    return contract_summary()


@app.get("/agents")
async def agents():
    status = awareness_loop.agent_registry.list_status()
    state_store.patch_json("executor_state.json", status)
    return status


@app.post("/agents/select")
async def select_agent(request: AgentSelectRequest):
    try:
        status = awareness_loop.agent_registry.select(request.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    runtime_entity.set_selected_agent(request.name)
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    return status


@app.post("/agents/{name}/config")
async def configure_agent(name: str, request: AgentConfigRequest):
    payload = request.model_dump()
    if payload.get("model_decision_mode") and payload["model_decision_mode"] not in {"auto", "always"}:
        raise HTTPException(status_code=422, detail="model_decision_mode must be 'auto' or 'always'")
    status = awareness_loop.agent_registry.upsert(name, payload)
    if status.get("selected_agent") == name:
        awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    return status


@app.get("/memory/summary")
async def memory_summary(session_id: str = "console-session"):
    return awareness_loop.memory_bridge.read_summary(session_id)


@app.post("/memory/patch")
async def memory_patch(request: MemoryPatchRequest):
    return awareness_loop.memory_bridge.write_patch(request.patch)


@app.get("/mvp/status")
async def mvp_status():
    agent_status_data = awareness_loop.agent_registry.selected().connection_status()
    return {
        "mvp": "core_governance",
        "status": "ready",
        "core_loops": {
            "direct_answer": True,
            "probe_route": True,
            "skill_route": True,
            "human_review_queue": True,
            "approve_reject": True,
            "approved_action_execution": True,
            "tool_proxy": True,
            "policy_trace": True,
            "execution_trace": True,
            "verifier_evidence_chain": True,
            "rollback_audit_depth": True,
            "rollback_snapshot_restore": True,
            "memory_bridge_local": True,
            "proactive_read_only_checks": True,
            "multi_agent_registry": True,
            "agent_adapter_contract": True,
            "agent_task_polling": True,
            "agent_pending_task_refresh": True,
            "agent_result_callback": True,
            "agency_intention_queue": True,
            "stale_belief_refresh": True,
            "real_probe_envelopes": True,
            "external_memory_bridge_hooks": True,
            "ops_soak_runner": True,
            "core_model_reasoning_layer": True,
            "web_console": console_dir.exists(),
        },
        "core_model": awareness_loop.core_reasoning.status(),
        "agent_runtime": {
            "selected_agent": awareness_loop.agent_registry.selected_name(),
            "real_agent_connected": bool(agent_status_data.get("connected")),
            "status": agent_status_data.get("status"),
            "note": "OpenClaw Gateway, Hermes HTTP, and Custom HTTP adapters share the AgentAdapter interface. OpenClaw remains the default selected runtime.",
        },
    }


@app.get("/runtime")
async def runtime():
    return {
        "identity": {
            "name": runtime_entity.identity.name,
            "full_name": runtime_entity.identity.full_name,
            "selected_agent": awareness_loop.agent_registry.selected_name(),
        },
        "lifecycle": {
            "status": runtime_entity.lifecycle.status,
            "started_at": runtime_entity.lifecycle.started_at,
            "last_heartbeat_at": runtime_entity.lifecycle.last_heartbeat_at,
        },
        "operational_mode": runtime_entity.operational_mode,
        "core_model": {
            "runtime_state": runtime_entity.core_model_runtime_state(),
            "status": awareness_loop.core_reasoning.status(),
        },
    }


if console_dir.exists():
    app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console_static")
