from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.architecture import architecture_snapshot
from core.awareness_loop import AwarenessLoop
from core.definitions import RiskLevel, lifecycle_statuses, normalize_risk, operational_modes, risk_catalog
from core.foresight_engine import ForesightEngine
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from execution.action_executor import ActionExecutor
from guardian.review_queue import ReviewQueue
from interface.agent_contract import contract_summary
from interface.event_normalizer import EventNormalizer
from interface.event_schema import Decision, Route, utc_now_iso
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from runtime.proactive_checks import ProactiveChecks
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell


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
foresight_engine = ForesightEngine()
proactive_checks = ProactiveChecks(state_store)
diff_tracker = DiffTracker()


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
    stop_path_template: str | None = None
    protocol_min: int | None = None
    protocol_max: int | None = None


class ActionProposalRequest(BaseModel):
    proposal_id: str | None = None
    task_id: str | None = None
    agent: str = "external"
    action: dict
    risk_guess: str = "R2"
    reversible: str = "unknown"
    reason: str = ""


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
    return state_store.read_all()


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


@app.get("/logs/rollback")
async def rollback_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("rollback_log.jsonl", limit=limit)}


@app.get("/logs/memory")
async def memory_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("memory_log.jsonl", limit=limit)}


@app.post("/proactive/check")
async def proactive_check():
    return proactive_checks.run_read_only()


@app.get("/rollback/diff")
async def rollback_diff(full: bool = False):
    return diff_tracker.git_diff_text() if full else diff_tracker.git_diff()


@app.post("/actions/proposals")
async def action_proposal(request: ActionProposalRequest):
    action_text = str(request.action.get("command") or request.action.get("path") or request.action)
    try:
        risk_level = normalize_risk(request.risk_guess)
    except ValueError:
        risk_level = RiskLevel.R2
    risk = risk_level.value
    proposal_decision = _proposal_decision(risk_level, action_text, request)
    foresight = foresight_engine.predict_text_action(action_text, risk_level)
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
    return {
        "status": execution.get("status", "unknown"),
        "decision": guardian_decision.get("decision", "allow"),
        "guardian_decision": guardian_decision,
        "execution_result": execution,
    }


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
    status = awareness_loop.agent_registry.upsert(name, request.model_dump())
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
            "rollback_snapshot_restore": True,
            "memory_bridge_local": True,
            "proactive_read_only_checks": True,
            "multi_agent_registry": True,
            "agent_adapter_contract": True,
            "web_console": console_dir.exists(),
        },
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
    }


if console_dir.exists():
    app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console_static")
