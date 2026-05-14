from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.awareness_loop import AwarenessLoop
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from interface.event_normalizer import EventNormalizer
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
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
        return review_queue.decide(review_id, "approved", request.reason)
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
    return safe_shell.run(request.command)


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


@app.get("/logs/tools")
async def tool_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("tool_call_log.jsonl", limit=limit)}


@app.get("/logs/rollback")
async def rollback_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("rollback_log.jsonl", limit=limit)}


@app.get("/logs/memory")
async def memory_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("memory_log.jsonl", limit=limit)}


@app.get("/runtime")
async def runtime():
    return {
        "identity": {
            "name": runtime_entity.identity.name,
            "full_name": runtime_entity.identity.full_name,
            "selected_agent": runtime_entity.identity.selected_agent,
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
