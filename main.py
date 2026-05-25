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
from interface.intake_gateway import IntakeGateway
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from rollback_audit.action_journal import ActionJournal
from rollback_audit.replay import Replay
from rollback_audit.replay_runtime import ReplayRuntime
from runtime.active_loop import ActiveRuntimeLoop
from runtime.agent_orchestrator import AgentOrchestrator
from runtime.alert_dispatcher import AlertDispatcher
from runtime.deployment_config import DeploymentConfigValidator
from runtime.external_world_refresh import ExternalWorldRefresh
from runtime.ops_monitor import OpsMonitor
from runtime.proactive_checks import ProactiveChecks
from runtime.retention_policy import RetentionPolicy
from runtime.runtime_matrix import RuntimeMatrix
from runtime.soak_runner import SoakRunner
from runtime.state_refresh import StateRefresh
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract
from runtime.safety_validation import SafetyValidation


app = FastAPI(title="Veyra", version="0.1.0")

state_store = WorldStateStore()
runtime_entity = RuntimeEntity(state_store=state_store)
awareness_loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime_entity)
event_normalizer = EventNormalizer()
intake_gateway = IntakeGateway(awareness_loop, event_normalizer, state_store=state_store)
console_dir = Path("ui/console")
review_queue = ReviewQueue(state_store)
rollback_manager = RollbackManager(state_store)
action_journal = ActionJournal(state_store)
replay_engine = Replay(state_store)
safe_shell = SafeShell(state_store=state_store)
safe_file = SafeFile(state_store=state_store)
safe_browser = SafeBrowser(state_store=state_store)
safe_api = SafeAPI(state_store=state_store)
_tool_proxy_config = state_store.read_json("ops_config.json").get("tool_proxy", {})
if isinstance(_tool_proxy_config, dict):
    safe_browser.configure_executor(bool(_tool_proxy_config.get("browser_executor_enabled", False)), _tool_proxy_config.get("browser_allowed_hosts"))
    safe_api.configure_executor(bool(_tool_proxy_config.get("api_executor_enabled", False)), _tool_proxy_config.get("api_allowed_hosts"))
action_executor = ActionExecutor(
    state_store=state_store,
    safe_shell=safe_shell,
    safe_file=safe_file,
    safe_browser=safe_browser,
    safe_api=safe_api,
    rollback_manager=rollback_manager,
)
foresight_engine = ForesightEngine(reasoning=awareness_loop.core_reasoning)
proactive_checks = ProactiveChecks(state_store, reasoning=awareness_loop.core_reasoning)
diff_tracker = DiffTracker()
agency_core = AgencyCore(state_store, reasoning=awareness_loop.core_reasoning)
safety_validation = SafetyValidation()
retention_policy = RetentionPolicy(state_store)
state_refresh = StateRefresh(state_store, reasoning=awareness_loop.core_reasoning, model_assist_enabled=False)
external_world_refresh = ExternalWorldRefresh(state_store, reasoning=awareness_loop.core_reasoning)
ops_monitor = OpsMonitor(
    state_store,
    agent_status_resolver=lambda: awareness_loop.agent_registry.selected().connection_status(),
    retention_policy=retention_policy,
    safety_validation=safety_validation,
)
alert_dispatcher = AlertDispatcher(state_store, ops_monitor)
deployment_validator = DeploymentConfigValidator(state_store)
runtime_matrix = RuntimeMatrix(state_store, awareness_loop.agent_registry, awareness_loop.memory_bridge)
soak_runner = SoakRunner(
    proactive_checks=proactive_checks,
    task_tracker=awareness_loop.task_tracker,
    state_refresh=state_refresh,
    retention_policy=retention_policy,
    safety_validation=safety_validation,
    adapter_resolver=lambda: awareness_loop.agent_registry.selected(),
    verifier=awareness_loop.verifier,
)
replay_runtime = ReplayRuntime(
    state_store=state_store,
    replay=replay_engine,
    journal=action_journal,
    review_queue=review_queue,
    foresight_engine=foresight_engine,
)
active_loop = ActiveRuntimeLoop(
    state_store=state_store,
    runtime_entity=runtime_entity,
    proactive_checks=proactive_checks,
    state_refresh=state_refresh,
    external_world_refresh=external_world_refresh,
    runtime_matrix=runtime_matrix,
    retention_policy=retention_policy,
    task_tracker=awareness_loop.task_tracker,
    adapter_resolver=lambda: awareness_loop.agent_registry.selected(),
    verifier=awareness_loop.verifier,
    replay_runtime=replay_runtime,
)
agent_orchestrator = AgentOrchestrator(
    state_store=state_store,
    registry=awareness_loop.agent_registry,
    task_tracker=awareness_loop.task_tracker,
    execution_trace=awareness_loop.execution_trace,
    verifier=awareness_loop.verifier,
)


class MessageRequest(BaseModel):
    text: str
    channel: str = "webhook"
    user_id: str = "local-user"
    session_id: str = "local-session"
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class ToolProxyConfigRequest(BaseModel):
    browser_executor_enabled: bool | None = None
    api_executor_enabled: bool | None = None
    browser_allowed_hosts: list[str] | None = None
    api_allowed_hosts: list[str] | None = None


class MemoryPatchRequest(BaseModel):
    patch: dict[str, Any]
    provider: str = "selected"


class MemoryDiagnosticsRequest(BaseModel):
    provider: str = "all"
    session_id: str = "memory-diagnostics"
    write_probe: bool = False


class ExternalWatchRequest(BaseModel):
    target: str
    kind: str | None = None
    reason: str = ""
    enabled: bool = True


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


class ActiveLoopRequest(BaseModel):
    interval_seconds: float = 300.0


class ActiveLoopTickRequest(BaseModel):
    reason: str = "manual"
    include_runtime_matrix: bool = False


class BeliefRefreshRequest(BaseModel):
    expire_after_seconds: int | None = 3600
    prune_expired_after_seconds: int | None = None


class ReplayRuntimeRequest(BaseModel):
    limit: int = 200
    auto_create_reviews: bool = True


class SoakSessionRequest(BaseModel):
    iterations: int = 60
    interval_seconds: float = 60.0


class AlertingConfigRequest(BaseModel):
    enabled: bool | None = None
    local_log: bool | None = None
    webhook_enabled: bool | None = None
    webhook_url: str | None = None
    webhook_url_env: str | None = None
    min_severity: str | None = None


class RetentionEnforceRequest(BaseModel):
    dry_run: bool = False
    limit_overrides: dict[str, int] | None = None


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


def _tool_proxy_status() -> dict[str, Any]:
    return {
        "status": "success",
        "shell": {"tool": "safe_shell", "configured": True, "mode": "subprocess"},
        "file": {"tool": "safe_file", "configured": True, "mode": "local_filesystem"},
        "browser": safe_browser.status(),
        "api": safe_api.status(),
    }


def _adapter_validation(name: str, status: dict[str, Any]) -> dict[str, Any]:
    existing = status.get("validation") if isinstance(status.get("validation"), dict) else {}
    if existing:
        return {"name": name, **existing}
    raw_status = str(status.get("status") or "unknown")
    configured = bool(status.get("base_url")) or raw_status not in {"adapter_unconfigured", "unconfigured", "unknown"}
    connected = bool(status.get("connected"))
    validation_status = "validated" if connected else "validation_pending" if configured else "not_configured"
    return {
        "name": name,
        "implemented": True,
        "configured": configured,
        "connected": connected,
        "validated": connected,
        "status": validation_status,
        "runtime_status": raw_status,
    }


def _runtime_validation_summary(agent_status_data: dict[str, Any] | None = None) -> dict[str, Any]:
    registry_status = awareness_loop.agent_registry.list_status()
    selected_agent = str(registry_status.get("selected_agent") or awareness_loop.agent_registry.selected_name())
    agents = registry_status.get("agents") if isinstance(registry_status.get("agents"), dict) else {}
    validations = {
        name: _adapter_validation(str(name), raw if isinstance(raw, dict) else {})
        for name, raw in agents.items()
    }
    if agent_status_data is not None:
        validations[selected_agent] = _adapter_validation(selected_agent, agent_status_data)
    selected = validations.get(selected_agent, _adapter_validation(selected_agent, agent_status_data or {}))
    configured_count = len([item for item in validations.values() if item["configured"]])
    validated_count = len([item for item in validations.values() if item["validated"]])
    if selected["validated"]:
        status = "validated"
    elif configured_count:
        status = "validation_pending"
    else:
        status = "not_configured"
    return {
        "status": status,
        "selected_agent": selected_agent,
        "selected": selected,
        "summary": {
            "implemented": len(validations),
            "configured": configured_count,
            "validated": validated_count,
            "validation_pending": len([item for item in validations.values() if item["status"] == "validation_pending"]),
            "not_configured": len([item for item in validations.values() if item["status"] == "not_configured"]),
        },
        "agents": validations,
    }


def _configure_tool_proxy(patch: dict[str, Any]) -> dict[str, Any]:
    config = state_store.read_json("ops_config.json")
    tool_proxy = config.setdefault("tool_proxy", {})
    if patch.get("browser_executor_enabled") is not None:
        tool_proxy["browser_executor_enabled"] = bool(patch["browser_executor_enabled"])
    if patch.get("api_executor_enabled") is not None:
        tool_proxy["api_executor_enabled"] = bool(patch["api_executor_enabled"])
    if patch.get("browser_allowed_hosts") is not None:
        tool_proxy["browser_allowed_hosts"] = [str(item).strip().lower() for item in patch["browser_allowed_hosts"] if str(item).strip()]
    if patch.get("api_allowed_hosts") is not None:
        tool_proxy["api_allowed_hosts"] = [str(item).strip().lower() for item in patch["api_allowed_hosts"] if str(item).strip()]
    state_store.write_json("ops_config.json", config)
    safe_browser.configure_executor(bool(tool_proxy.get("browser_executor_enabled", False)), tool_proxy.get("browser_allowed_hosts"))
    safe_api.configure_executor(bool(tool_proxy.get("api_executor_enabled", False)), tool_proxy.get("api_allowed_hosts"))
    return _tool_proxy_status()


def _deployment_readiness() -> dict[str, Any]:
    readiness = ops_monitor.deployment_readiness()
    config = deployment_validator.validate()
    runtime_validation = _runtime_validation_summary()
    config_passed = config["status"] in {"ready", "ready_with_warnings"}
    readiness["checks"].append({"name": "deployment_config", "passed": config_passed})
    readiness["configuration"] = config
    readiness["validation"] = {
        "codebase": "implemented",
        "runtime_matrix": runtime_matrix.status().get("status", "not_run"),
        "agent_runtime": runtime_validation,
    }
    if not config_passed:
        readiness["status"] = "not_ready"
        readiness.setdefault("blocking_alerts", []).append({"component": "deployment_config", "severity": "critical", "details": config})
    elif runtime_validation["status"] == "not_configured":
        readiness["status"] = "not_configured"
    elif runtime_validation["status"] == "validation_pending":
        readiness["status"] = "validation_pending"
    elif readiness.get("health_status") == "degraded":
        readiness["status"] = "degraded"
    return readiness


@app.post("/events/message")
async def message(request: MessageRequest):
    receipt = intake_gateway.receive_message(
        text=request.text,
        channel=request.channel,
        user_id=request.user_id,
        session_id=request.session_id,
        message_id=request.message_id,
        metadata=request.metadata,
    )
    if receipt.get("status") == "duplicate":
        return {"event_id": receipt.get("previous", {}).get("event_id"), "route": "duplicate", "status": "duplicate", "response": "Duplicate message ignored.", "risk_level": "R0", "artifacts": receipt}
    if receipt.get("status") != "delivered":
        return {"event_id": None, "route": "block", "status": receipt.get("status"), "response": receipt.get("reason", "Channel intake rejected."), "risk_level": "R0", "artifacts": receipt}
    return receipt["loop_result"]


@app.get("/channels")
async def channels_status():
    return intake_gateway.channel_status()


@app.post("/channels/{channel}/messages")
async def channel_message(channel: str, request: MessageRequest):
    return intake_gateway.receive_message(
        text=request.text,
        channel=channel,
        user_id=request.user_id,
        session_id=request.session_id,
        message_id=request.message_id,
        metadata=request.metadata,
    )


@app.get("/channels/outbox")
async def channels_outbox(limit: int = 100):
    return intake_gateway.outbox(limit=limit)


@app.get("/channels/sessions")
async def channels_sessions():
    return intake_gateway.sessions()


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


@app.get("/tool-proxy/status")
async def tool_proxy_status():
    return _tool_proxy_status()


@app.post("/tool-proxy/config")
async def tool_proxy_config(request: ToolProxyConfigRequest):
    return _configure_tool_proxy(request.model_dump(exclude_none=True))


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


@app.get("/logs/alerts")
async def alert_logs(limit: int = 100):
    return {"items": state_store.read_jsonl("alert_log.jsonl", limit=limit)}


@app.get("/audit/journal")
async def audit_journal(
    limit: int = 100,
    event_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
):
    return action_journal.timeline(limit=limit, event_id=event_id, task_id=task_id, trace_id=trace_id)


@app.get("/audit/time-travel")
async def audit_time_travel(until: str | None = None, limit: int = 200):
    return action_journal.time_travel(until=until, limit=limit)


@app.get("/audit/replay/{trace_id}")
async def audit_replay(trace_id: str):
    return replay_engine.replay(trace_id)


def _create_replay_review(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("proposal_status") != "ready":
        return payload
    proposal = payload["proposal"]
    snapshot_id = str(payload.get("snapshot_id") or proposal.get("action", {}).get("snapshot_id") or "")
    task_text = f"Replay compensation: restore snapshot {snapshot_id}"
    review = review_queue.create(
        event_id=str(payload.get("plan", {}).get("event_id") or f"replay_{snapshot_id}"),
        task_text=task_text,
        risk_level="R4",
        foresight=foresight_engine.predict_text_action(task_text, RiskLevel.R4),
        guardian_decision={
            "decision": "ask_user",
            "risk_level": "R4",
            "reason": "Replay compensation restores prior filesystem state and requires explicit approval.",
        },
        proposal=proposal,
    )
    return {"status": "needs_confirmation", "review": review, "proposal": proposal, "plan": payload.get("plan")}


@app.post("/audit/replay/{trace_id}/propose")
async def audit_replay_propose(trace_id: str):
    return _create_replay_review(replay_engine.compensation_proposal(trace_id=trace_id))


@app.get("/audit/replay/event/{event_id}")
async def audit_replay_event(event_id: str):
    return replay_engine.plan(event_id=event_id)


@app.post("/audit/replay/event/{event_id}/propose")
async def audit_replay_event_propose(event_id: str):
    return _create_replay_review(replay_engine.compensation_proposal(event_id=event_id))


@app.get("/audit/replay/runtime/status")
async def audit_replay_runtime_status():
    return replay_runtime.status()


@app.post("/audit/replay/runtime/scan")
async def audit_replay_runtime_scan(request: ReplayRuntimeRequest | None = None):
    payload = request or ReplayRuntimeRequest()
    return replay_runtime.scan(limit=payload.limit)


@app.post("/audit/replay/runtime/run")
async def audit_replay_runtime_run(request: ReplayRuntimeRequest | None = None):
    payload = request or ReplayRuntimeRequest()
    scan = replay_runtime.scan(limit=payload.limit)
    run = replay_runtime.run_pending(auto_create_reviews=payload.auto_create_reviews, limit=min(payload.limit, 50))
    return {"status": "success", "scan": scan, "run": run}


@app.post("/proactive/check")
async def proactive_check():
    return proactive_checks.run_read_only()


@app.post("/state/refresh-stale")
async def refresh_stale_state():
    return state_refresh.refresh_stale()


@app.post("/external/refresh")
async def refresh_external_world(limit: int = 10):
    return external_world_refresh.refresh_watchlist(limit=limit)


@app.post("/external/watchlist")
async def add_external_watch(request: ExternalWatchRequest):
    external = state_store.read_json("external_world.json")
    watchlist = external.setdefault("watchlist", [])
    if not isinstance(watchlist, list):
        watchlist = []
        external["watchlist"] = watchlist
    item = request.model_dump()
    existing = [entry for entry in watchlist if isinstance(entry, dict) and entry.get("target") == request.target]
    if existing:
        existing[0].update(item)
    else:
        watchlist.append(item)
    state_store.write_json("external_world.json", external)
    return external


@app.get("/agency/intentions")
async def agency_intentions():
    return agency_core.state()


@app.get("/belief/status")
async def belief_status(limit: int = 50):
    return awareness_loop.belief.ttl_report(limit=limit)


@app.post("/belief/refresh")
async def belief_refresh(request: BeliefRefreshRequest | None = None):
    payload = request or BeliefRefreshRequest()
    result = awareness_loop.belief.refresh(
        expire_after_seconds=payload.expire_after_seconds,
        prune_expired_after_seconds=payload.prune_expired_after_seconds,
    )
    return {"status": "success", **result}


@app.get("/ops/safety/red-team")
async def ops_safety_red_team():
    return safety_validation.run()


@app.get("/ops/retention")
async def ops_retention():
    return retention_policy.summary()


@app.post("/ops/retention/enforce")
async def ops_retention_enforce(request: RetentionEnforceRequest | None = None):
    payload = request or RetentionEnforceRequest()
    return retention_policy.enforce(dry_run=payload.dry_run, limits=payload.limit_overrides)


@app.get("/ops/health")
async def ops_health():
    return ops_monitor.health()


@app.get("/ops/alerts")
async def ops_alerts():
    return ops_monitor.alerts()


@app.get("/ops/alerting")
async def ops_alerting_status():
    return alert_dispatcher.status()


@app.post("/ops/alerting/config")
async def ops_alerting_config(request: AlertingConfigRequest):
    return alert_dispatcher.configure(request.model_dump(exclude_none=True))


@app.post("/ops/alerts/dispatch")
async def ops_alerts_dispatch(min_severity: str | None = None):
    return alert_dispatcher.dispatch(min_severity=min_severity)


@app.get("/ops/deployment")
async def ops_deployment():
    return _deployment_readiness()


@app.get("/ops/deployment/config")
async def ops_deployment_config():
    return deployment_validator.validate()


@app.get("/ops/runtime-matrix")
async def ops_runtime_matrix_status():
    return runtime_matrix.status()


@app.post("/ops/runtime-matrix/run")
async def ops_runtime_matrix_run(write_memory_probe: bool = False):
    return runtime_matrix.run(write_memory_probe=write_memory_probe)


@app.get("/runtime/active-loop")
async def runtime_active_loop_status():
    return active_loop.status()


@app.post("/runtime/active-loop/start")
async def runtime_active_loop_start(request: ActiveLoopRequest):
    return active_loop.start(interval_seconds=request.interval_seconds)


@app.post("/runtime/active-loop/stop")
async def runtime_active_loop_stop():
    return active_loop.stop()


@app.post("/runtime/active-loop/tick")
async def runtime_active_loop_tick(request: ActiveLoopTickRequest | None = None):
    payload = request or ActiveLoopTickRequest()
    return active_loop.tick(reason=payload.reason, include_runtime_matrix=payload.include_runtime_matrix)


@app.post("/ops/soak")
async def ops_soak(request: SoakRequest):
    return soak_runner.run(iterations=request.iterations)


@app.get("/ops/soak/status")
async def ops_soak_status():
    return soak_runner.status()


@app.post("/ops/soak/start")
async def ops_soak_start(request: SoakSessionRequest):
    return soak_runner.start(iterations=request.iterations, interval_seconds=request.interval_seconds)


@app.post("/ops/soak/stop")
async def ops_soak_stop():
    return soak_runner.stop()


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
    allowed = {"shell_command", "file_write", "file_read", "browser_open", "api_request", "rollback_restore"}
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
    if action_type == "rollback_restore" and not request.action.get("snapshot_id"):
        return "rollback_restore action requires snapshot_id"
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
    if action.get("type") == "rollback_restore":
        return f"rollback restore {action.get('snapshot_id') or ''}"
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
    return {**contract_summary(), "tool_proxy_contract": agent_tool_proxy_contract()}


@app.get("/agents")
async def agents():
    status = awareness_loop.agent_registry.list_status()
    state_store.patch_json("executor_state.json", status)
    return status


@app.get("/agents/certification")
async def agents_certification_status():
    return runtime_matrix.status()


@app.post("/agents/certification/run")
async def agents_certification_run(write_memory_probe: bool = False):
    return runtime_matrix.run(write_memory_probe=write_memory_probe)


@app.post("/agents/invoke")
async def agents_invoke(request: AgentInvokeRequest):
    return agent_orchestrator.invoke(
        text=request.text,
        agents=request.agents,
        mode=request.mode,
        channel=request.channel,
        user_id=request.user_id,
        session_id=request.session_id,
    )


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
async def memory_summary(session_id: str = "console-session", provider: str = "selected"):
    return awareness_loop.memory_bridge.read_summary(session_id, provider=provider)


@app.get("/memory/providers")
async def memory_providers():
    return awareness_loop.memory_bridge.provider_status()


@app.get("/memory/providers/diagnostics")
async def memory_provider_diagnostics(provider: str = "all", session_id: str = "memory-diagnostics"):
    return awareness_loop.memory_bridge.provider_diagnostics(provider=provider, session_id=session_id, write_probe=False)


@app.post("/memory/providers/diagnostics")
async def memory_provider_diagnostics_write(request: MemoryDiagnosticsRequest):
    return awareness_loop.memory_bridge.provider_diagnostics(
        provider=request.provider,
        session_id=request.session_id,
        write_probe=request.write_probe,
    )


@app.post("/memory/patch")
async def memory_patch(request: MemoryPatchRequest):
    return awareness_loop.memory_bridge.write_patch(request.patch, provider=request.provider)


@app.get("/mvp/status")
async def mvp_status():
    agent_status_data = awareness_loop.agent_registry.selected().connection_status()
    runtime_validation = _runtime_validation_summary(agent_status_data)
    implemented_loops = {
        "direct_answer": True,
        "multi_channel_intake": True,
        "channel_outbox": True,
        "channel_session_mapping": True,
        "channel_dedupe": True,
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
        "action_journal_timeline": True,
        "replay_plan": True,
        "replay_compensation_review": True,
        "replay_runtime_auto_scan": True,
        "replay_runtime_review_creation": True,
        "time_travel_audit": True,
        "memory_bridge_local": True,
        "memory_bridge_provider_routing": True,
        "memory_provider_diagnostics": True,
        "proactive_read_only_checks": True,
        "multi_agent_registry": True,
        "multi_agent_explicit_invocation": True,
        "agent_certification_runtime_matrix": True,
        "agent_adapter_contract": True,
        "agent_task_polling": True,
        "agent_pending_task_refresh": True,
        "agent_result_callback": True,
        "agency_intention_queue": True,
        "stale_belief_refresh": True,
        "belief_ttl_status": True,
        "belief_source_trust": True,
        "real_probe_envelopes": True,
        "external_memory_bridge_hooks": True,
        "tool_proxy_executor_config": True,
        "ops_soak_runner": True,
        "ops_soak_session": True,
        "ops_runtime_matrix": True,
        "ops_health_alerts": True,
        "ops_alert_dispatch": True,
        "ops_retention_enforce": True,
        "deployment_readiness": True,
        "deployment_config_validation": True,
        "core_model_reasoning_layer": True,
        "core_model_memory_relevance": True,
        "external_world_watchlist_refresh": True,
        "active_runtime_loop": True,
        "active_loop_manual_tick": True,
        "agent_tool_proxy_contract": True,
        "agent_tool_bypass_verification": True,
        "web_console": console_dir.exists(),
    }
    return {
        "mvp": "core_governance",
        "status": "implemented",
        "core_loops": implemented_loops,
        "validation": {
            "codebase": "implemented",
            "local_self_tests": "available",
            "runtime_validation": runtime_validation,
            "production_validation": "pending_live_environment" if runtime_validation["status"] != "validated" else "ready_for_soak",
            "data_reality": "live_local_api_no_mock_fixtures",
        },
        "core_model": awareness_loop.core_reasoning.status(),
        "agent_runtime": {
            "selected_agent": awareness_loop.agent_registry.selected_name(),
            "real_agent_connected": bool(agent_status_data.get("connected")),
            "status": agent_status_data.get("status"),
            "note": "OpenClaw Gateway, Hermes HTTP, and Custom HTTP adapters share the AgentAdapter interface. OpenClaw remains the default selected runtime.",
        },
        "active_loop": active_loop.status(),
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
        "active_loop": active_loop.status(),
    }


if console_dir.exists():
    app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console_static")
