from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.awareness_loop import AwarenessLoop
from core.foresight_engine import ForesightEngine
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from execution.action_executor import ActionExecutor
from guardian.review_queue import ReviewQueue
from interface.event_normalizer import EventNormalizer
from interface.event_schema import RiskLevel, utc_now_iso
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from runtime.proactive_checks import ProactiveChecks
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
    risk = request.risk_guess if request.risk_guess in {"R0", "R1", "R2", "R3", "R4", "R5"} else "R2"
    if risk == "R5":
        state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": request.proposal_id or "proposal",
                "route": "block",
                "status": "blocked",
                "artifacts": {"proposal": request.model_dump(), "reason": "R5 proposals are blocked"},
            },
        )
        return {"status": "blocked", "decision": "block", "risk_level": risk}
    if risk in {"R3", "R4"}:
        review = review_queue.create(
            event_id=request.proposal_id or f"proposal_{utc_now_iso()}",
            task_text=request.reason or action_text,
            risk_level=risk,
            foresight=foresight_engine.predict_text_action(action_text, RiskLevel(risk)),
            guardian_decision={"decision": "ask_user", "risk_level": risk, "reason": "ActionProposal requires human confirmation."},
            proposal=request.model_dump(),
        )
        return {"status": "needs_confirmation", "decision": "ask_user", "review": review}
    execution = action_executor.execute_review(
        {
            "review_id": request.proposal_id or "direct_action",
            "proposal": request.model_dump(),
        }
    )
    return {"status": execution.get("status", "unknown"), "decision": "allow", "execution_result": execution}


@app.get("/agent/status")
async def agent_status():
    awareness_loop.agent_adapter = awareness_loop.agent_registry.selected()
    status = awareness_loop.agent_adapter.connection_status()
    state_store.patch_json("executor_state.json", {"selected_agent": awareness_loop.agent_registry.selected_name(), **status})
    return status


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
            "rollback_snapshot_restore": True,
            "memory_bridge_local": True,
            "proactive_read_only_checks": True,
            "multi_agent_registry": True,
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
