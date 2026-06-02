import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.architecture import architecture_snapshot
from core.agency_core import AgencyCore
from core.awareness_loop import AwarenessLoop
from core.commitment_core import CommitmentCore
from core.definitions import RiskLevel, classify_text_risk, normalize_risk
from core.foresight_engine import ForesightEngine
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from execution.action_executor import ActionExecutor
from guardian.review_queue import ReviewQueue
from interface.agent_adapter import ExecutionResult
from interface.event_normalizer import EventNormalizer
from interface.event_schema import Decision, Route, utc_now_iso
from interface.feishu_adapter import FeishuAdapter
from interface.feishu_ws_runner import FeishuWsRunner
from interface.intake_gateway import IntakeGateway
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from rollback_audit.action_journal import ActionJournal
from rollback_audit.tool_trace import ToolTrace
from rollback_audit.replay import Replay
from rollback_audit.replay_runtime import ReplayRuntime
from routers.agent_memory import build_agent_memory_router
from routers.commitments import build_commitments_router
from routers.debug_audit import build_debug_audit_router
from routers.ops_runtime import build_ops_runtime_router
from routers.runtime_observability import build_runtime_observability_router
from runtime.active_loop import ActiveRuntimeLoop
from runtime.commitment_push import CommitmentPushRuntime
from runtime.agent_orchestrator import AgentOrchestrator
from runtime.alert_dispatcher import AlertDispatcher
from runtime.cron import Cron
from runtime.deployment_config import DeploymentConfigValidator
from runtime.external_world_refresh import ExternalWorldRefresh
from runtime.external_runtime_probe import ExternalRuntimeProbe
from runtime.ops_monitor import OpsMonitor
from runtime.proactive_checks import ProactiveChecks
from runtime.retention_policy import RetentionPolicy
from runtime.routing_metrics import RoutingMetrics
from runtime.runtime_matrix import RuntimeMatrix
from runtime.soak_runner import SoakRunner
from runtime.state_refresh import StateRefresh
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell
from runtime.safety_validation import SafetyValidation


app = FastAPI(title="Veyra", version="0.1.0")


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _env_hosts(name: str) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    hosts = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return hosts or None

state_store = WorldStateStore()
runtime_entity = RuntimeEntity(state_store=state_store)
commitment_core = CommitmentCore(state_store)
awareness_loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime_entity, commitment_core=commitment_core)
commitment_push = CommitmentPushRuntime(
    state_store=state_store,
    commitment_core=commitment_core,
    guardian=awareness_loop.guardian,
    foresight=awareness_loop.foresight,
)
routing_metrics = RoutingMetrics(state_store)
event_normalizer = EventNormalizer()
intake_gateway = IntakeGateway(awareness_loop, event_normalizer, state_store=state_store)
feishu_adapter = FeishuAdapter(intake_gateway, state_store=state_store)
feishu_ws_runner = FeishuWsRunner(state_store=state_store, adapter=feishu_adapter)
console_dir = Path("ui/console")
review_queue = ReviewQueue(state_store)
rollback_manager = RollbackManager(state_store)
action_journal = ActionJournal(state_store)
replay_engine = Replay(state_store)
safe_shell = SafeShell(state_store=state_store)
safe_file = SafeFile(state_store=state_store)
safe_browser = SafeBrowser(state_store=state_store)
safe_api = SafeAPI(state_store=state_store)
action_proposal_tool_trace = ToolTrace(state_store)
_tool_proxy_config = state_store.read_json("ops_config.json").get("tool_proxy", {})
_tool_proxy_env_overrides: dict[str, Any] = {}
if isinstance(_tool_proxy_config, dict):
    env_browser_enabled = _env_bool("VEYRA_TOOL_PROXY_BROWSER_ENABLED")
    env_api_enabled = _env_bool("VEYRA_TOOL_PROXY_API_ENABLED")
    env_browser_hosts = _env_hosts("VEYRA_TOOL_PROXY_BROWSER_ALLOWED_HOSTS")
    env_api_hosts = _env_hosts("VEYRA_TOOL_PROXY_API_ALLOWED_HOSTS")
    if env_browser_enabled is not None:
        _tool_proxy_config["browser_executor_enabled"] = env_browser_enabled
        _tool_proxy_env_overrides["browser_executor_enabled"] = "VEYRA_TOOL_PROXY_BROWSER_ENABLED"
    if env_api_enabled is not None:
        _tool_proxy_config["api_executor_enabled"] = env_api_enabled
        _tool_proxy_env_overrides["api_executor_enabled"] = "VEYRA_TOOL_PROXY_API_ENABLED"
    if env_browser_hosts is not None:
        _tool_proxy_config["browser_allowed_hosts"] = env_browser_hosts
        _tool_proxy_env_overrides["browser_allowed_hosts"] = "VEYRA_TOOL_PROXY_BROWSER_ALLOWED_HOSTS"
    if env_api_hosts is not None:
        _tool_proxy_config["api_allowed_hosts"] = env_api_hosts
        _tool_proxy_env_overrides["api_allowed_hosts"] = "VEYRA_TOOL_PROXY_API_ALLOWED_HOSTS"
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
external_runtime_probe = ExternalRuntimeProbe(
    runtime_matrix=runtime_matrix,
    feishu_status_resolver=feishu_ws_runner.status,
    channel_status_resolver=intake_gateway.channel_status,
)
soak_runner = SoakRunner(
    proactive_checks=proactive_checks,
    task_tracker=awareness_loop.task_tracker,
    state_refresh=state_refresh,
    retention_policy=retention_policy,
    safety_validation=safety_validation,
    adapter_resolver=lambda: awareness_loop.agent_registry.selected(),
    verifier=awareness_loop.verifier,
    external_runtime_probe=lambda: external_runtime_probe.status(run_runtime_matrix=True, write_memory_probe=False),
)
replay_runtime = ReplayRuntime(
    state_store=state_store,
    replay=replay_engine,
    journal=action_journal,
    review_queue=review_queue,
    foresight_engine=foresight_engine,
    action_executor=action_executor,
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
    commitment_push=commitment_push,
)
runtime_cron = Cron(state_store=state_store, active_loop=active_loop, commitment_push=commitment_push)
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
    approved_by: str | None = None

class APIProxyRequest(BaseModel):
    payload: dict[str, Any]
    approved_by: str | None = None

class ToolProxyConfigRequest(BaseModel):
    browser_executor_enabled: bool | None = None
    api_executor_enabled: bool | None = None
    browser_allowed_hosts: list[str] | None = None
    api_allowed_hosts: list[str] | None = None

class ActionProposalRequest(BaseModel):
    proposal_id: str | None = None
    task_id: str | None = None
    agent: str = "external"
    action: dict[str, Any]
    risk_guess: str = "R2"
    reversible: str = "unknown"
    reason: str = ""

class ChannelConfigRequest(BaseModel):
    enabled: bool | None = None
    delivery: str | None = None
    base_url: str | None = None
    webhook_url: str | None = None
    webhook_url_env: str | None = None
    trust_env: bool | None = None
    app_id: str | None = None
    app_id_env: str | None = None
    app_secret: str | None = None
    app_secret_env: str | None = None
    tenant_access_token: str | None = None
    tenant_access_token_env: str | None = None
    default_receive_id: str | None = None
    default_receive_id_env: str | None = None
    default_receive_id_type: str | None = None
    connection_mode: str | None = None
    verification_token: str | None = None
    verification_token_env: str | None = None
    encrypt_key: str | None = None
    encrypt_key_env: str | None = None
    reply_to_session: bool | None = None
    timeout: float | None = None
    agent_follow_up_enabled: bool | None = None
    agent_follow_up_timeout_seconds: float | None = None
    agent_follow_up_interval_seconds: float | None = None
    agent_follow_up_first_progress_seconds: float | None = None
    agent_follow_up_progress_interval_seconds: float | None = None
    agent_missing_snapshot_fail_count: int | None = None

class FeishuImportRequest(BaseModel):
    path: str | None = None
    enable: bool = True

class ChannelSendRequest(BaseModel):
    message: str
    session_id: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


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
        "environment_overrides": _tool_proxy_env_overrides,
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


class _DynamicDeps(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        return value() if callable(value) else value


app.include_router(
    build_runtime_observability_router(
        trace_recorder=awareness_loop.runtime_trace,
        metrics=routing_metrics,
    )
)
app.include_router(
    build_agent_memory_router(
        _DynamicDeps(
            {
                "awareness_loop": lambda: awareness_loop,
                "state_store": lambda: state_store,
                "runtime_entity": lambda: runtime_entity,
                "runtime_matrix": lambda: runtime_matrix,
                "agent_orchestrator": lambda: agent_orchestrator,
            }
        )
    )
)
app.include_router(
    build_commitments_router(
        _DynamicDeps(
            {
                "commitment_core": lambda: commitment_core,
                "commitment_push": lambda: commitment_push,
            }
        )
    )
)
app.include_router(
    build_debug_audit_router(
        _DynamicDeps(
            {
                "state_store": lambda: state_store,
                "agency_core": lambda: agency_core,
                "commitment_core": lambda: commitment_core,
                "awareness_loop": lambda: awareness_loop,
                "review_queue": lambda: review_queue,
                "action_executor": lambda: action_executor,
                "action_journal": lambda: action_journal,
                "replay_engine": lambda: replay_engine,
                "replay_runtime": lambda: replay_runtime,
                "foresight_engine": lambda: foresight_engine,
                "proactive_checks": lambda: proactive_checks,
                "state_refresh": lambda: state_refresh,
                "external_world_refresh": lambda: external_world_refresh,
                "diff_tracker": lambda: diff_tracker,
                "architecture_snapshot": lambda: architecture_snapshot,
            }
        )
    )
)
app.include_router(
    build_ops_runtime_router(
        _DynamicDeps(
            {
                "safety_validation": lambda: safety_validation,
                "retention_policy": lambda: retention_policy,
                "ops_monitor": lambda: ops_monitor,
                "alert_dispatcher": lambda: alert_dispatcher,
                "deployment_readiness": lambda: _deployment_readiness,
                "deployment_validator": lambda: deployment_validator,
                "runtime_matrix": lambda: runtime_matrix,
                "external_runtime_probe": lambda: external_runtime_probe,
                "active_loop": lambda: active_loop,
                "runtime_cron": lambda: runtime_cron,
                "soak_runner": lambda: soak_runner,
            }
        )
    )
)


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


@app.get("/channels/{channel}/config")
async def channel_config(channel: str):
    return intake_gateway.channel_config(channel)


@app.post("/channels/{channel}/config")
async def configure_channel(channel: str, request: ChannelConfigRequest):
    patch = request.model_dump(exclude_none=True)
    if "delivery" in patch and patch["delivery"] not in {"local_outbox", "feishu", "webhook"}:
        raise HTTPException(status_code=422, detail="delivery must be local_outbox, feishu, or webhook")
    if "default_receive_id_type" in patch and patch["default_receive_id_type"] not in {"open_id", "user_id", "union_id", "email", "chat_id"}:
        raise HTTPException(status_code=422, detail="unsupported Feishu receive_id_type")
    if "connection_mode" in patch and patch["connection_mode"] not in {"callback", "websocket"}:
        raise HTTPException(status_code=422, detail="connection_mode must be callback or websocket")
    return intake_gateway.configure_channel(channel, patch)


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


@app.post("/channels/{channel}/send")
async def channel_send(channel: str, request: ChannelSendRequest):
    return intake_gateway.send_channel_message(
        channel=channel,
        session_id=request.session_id,
        message=request.message,
        metadata=request.metadata,
    )


@app.get("/channels/outbox")
async def channels_outbox(limit: int = 100):
    return intake_gateway.outbox(limit=limit)


@app.get("/channels/sessions")
async def channels_sessions():
    return intake_gateway.sessions()


@app.post("/integrations/feishu/events")
async def feishu_events(payload: dict[str, Any]):
    return feishu_adapter.handle_callback(payload)


@app.get("/integrations/feishu/ws/status")
async def feishu_ws_status():
    return feishu_ws_runner.status()


@app.post("/integrations/feishu/import-openclaw")
async def feishu_import_openclaw(request: FeishuImportRequest | None = None):
    payload = request or FeishuImportRequest()
    return feishu_ws_runner.import_openclaw_config(path=payload.path, enable=payload.enable)


@app.post("/integrations/feishu/ws/start")
async def feishu_ws_start():
    return feishu_ws_runner.start()


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
    return safe_browser.open(request.url, approved_by=request.approved_by)


@app.post("/tool-proxy/api/request")
async def tool_proxy_api_request(request: APIProxyRequest):
    return safe_api.request(request.payload, approved_by=request.approved_by)


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
        tool_trace = _record_action_proposal_trace(request, "blocked", guardian_decision)
        verification = _verify_action_proposal_result(
            request=request,
            action_text=action_text,
            status="blocked",
            guardian_decision=guardian_decision,
            tool_trace=tool_trace,
        )
        state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": request.proposal_id or "proposal",
                "route": "block",
                "status": "blocked",
                "artifacts": {
                    "proposal": request.model_dump(),
                    "guardian_decision": guardian_decision,
                    "tool_trace": tool_trace,
                    "verification": verification,
                },
            },
        )
        return {
            "status": "blocked",
            "decision": "block",
            "risk_level": risk,
            "guardian_decision": guardian_decision,
            "tool_trace": tool_trace,
            "verification": verification,
        }
    if risk in {"R3", "R4"}:
        review = review_queue.create(
            event_id=request.proposal_id or f"proposal_{utc_now_iso()}",
            task_text=request.reason or action_text,
            risk_level=risk,
            foresight=foresight,
            guardian_decision=guardian_decision,
            proposal=request.model_dump(),
        )
        tool_trace = _record_action_proposal_trace(request, "needs_confirmation", guardian_decision, review=review)
        verification = _verify_action_proposal_result(
            request=request,
            action_text=action_text,
            status="pending",
            guardian_decision=guardian_decision,
            tool_trace=tool_trace,
            review=review,
        )
        state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": request.proposal_id or "proposal",
                "route": "human_review",
                "status": "needs_confirmation",
                "artifacts": {
                    "proposal": request.model_dump(),
                    "guardian_decision": guardian_decision,
                    "review": review,
                    "tool_trace": tool_trace,
                    "verification": verification,
                },
            },
        )
        return {
            "status": "needs_confirmation",
            "decision": "ask_user",
            "guardian_decision": guardian_decision,
            "review": review,
            "tool_trace": tool_trace,
            "verification": verification,
        }
    execution = action_executor.execute_review(
        {
            "review_id": request.proposal_id or "direct_action",
            "proposal": request.model_dump(),
        }
    )
    tool_trace = execution.get("tool_trace") if isinstance(execution.get("tool_trace"), dict) else _record_action_proposal_trace(request, execution.get("status", "unknown"), guardian_decision)
    verification = _verify_action_proposal_result(
        request=request,
        action_text=action_text,
        status=str(execution.get("status") or "unknown"),
        guardian_decision=guardian_decision,
        tool_trace=tool_trace,
        execution=execution,
    )
    state_store.append_jsonl(
        "action_record.jsonl",
        {
            "event_id": request.proposal_id or "direct_action",
            "route": "native_tool",
            "status": execution.get("status", "unknown"),
            "artifacts": {
                "proposal": request.model_dump(),
                "guardian_decision": guardian_decision,
                "execution_result": execution,
                "tool_trace": tool_trace,
                "verification": verification,
            },
        },
    )
    return {
        "status": execution.get("status", "unknown"),
        "decision": guardian_decision.get("decision", "allow"),
        "guardian_decision": guardian_decision,
        "execution_result": execution,
        "tool_trace": tool_trace,
        "verification": verification,
    }


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


def _record_action_proposal_trace(
    request: ActionProposalRequest,
    status: str,
    guardian_decision: dict[str, Any],
    *,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "trace_id": request.proposal_id,
        "operation": "action_proposal_guard",
        "review_id": (review or {}).get("review_id"),
        "reason": guardian_decision.get("reason"),
    }
    return action_proposal_tool_trace.record(
        tool="action_proposal",
        action_type=str(request.action.get("type") or "unknown"),
        target=_action_trace_target(request.action),
        result=result,
        review=guardian_decision,
        approved_by=(review or {}).get("review_id"),
    )


def _verify_action_proposal_result(
    *,
    request: ActionProposalRequest,
    action_text: str,
    status: str,
    guardian_decision: dict[str, Any],
    tool_trace: dict[str, Any],
    execution: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution_status = _verifier_status(status)
    raw = {
        "action_proposals": [
            {
                "proposal_id": request.proposal_id,
                "status": status,
                "decision": guardian_decision.get("decision"),
                "review_id": (review or {}).get("review_id"),
            }
        ],
        "guardian_decision": guardian_decision,
        "tool_proxy_traces": [tool_trace] if tool_trace else [],
        "review_id": (review or {}).get("review_id"),
    }
    if execution:
        raw["execution_result"] = execution
        if execution.get("approved_by"):
            raw["approved_by"] = execution.get("approved_by")
    return awareness_loop.verifier.verify_execution_result(
        ExecutionResult(
            task_id=request.proposal_id or "action_proposal",
            executor="tool_proxy",
            status=execution_status,
            result=_execution_summary(execution, guardian_decision, status),
            tool_calls=[action_text] if action_text and execution_status in {"success", "blocked", "pending"} else [],
            raw=raw,
        )
    )


def _verifier_status(status: str) -> str:
    normalized = str(status or "unknown")
    if normalized in {"ok", "success"}:
        return "success"
    if normalized in {"blocked", "block"}:
        return "blocked"
    if normalized in {"needs_confirmation", "pending"}:
        return "pending"
    if normalized in {"error", "failed", "timeout"}:
        return "failed"
    return normalized


def _execution_summary(execution: dict[str, Any] | None, guardian_decision: dict[str, Any], status: str) -> str:
    if execution:
        if execution.get("stdout"):
            return str(execution.get("stdout")).strip()[:500]
        if execution.get("content"):
            return "file content read through Tool Proxy"
        if execution.get("reason"):
            return str(execution.get("reason"))
    return str(guardian_decision.get("reason") or status)


def _action_trace_target(action: dict[str, Any]) -> Any:
    action_type = action.get("type")
    if action_type == "shell_command":
        command = action.get("command")
        return command if isinstance(command, list) else str(command or "")
    if action_type in {"file_read", "file_write"}:
        return {"path": action.get("path"), "content_chars": len(str(action.get("content") or ""))}
    if action_type == "browser_open":
        return {"url": action.get("url")}
    if action_type == "api_request":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        return {"method": payload.get("method", "GET"), "url": payload.get("url") or payload.get("endpoint")}
    if action_type == "rollback_restore":
        return {"snapshot_id": action.get("snapshot_id")}
    return action


@app.get("/mvp/status")
async def mvp_status():
    agent_status_data = awareness_loop.agent_registry.selected().connection_status()
    runtime_validation = _runtime_validation_summary(agent_status_data)
    implemented_loops = {
        "direct_answer": True,
        "multi_channel_intake": True,
        "feishu_callback_intake": True,
        "feishu_websocket_intake": True,
        "feishu_openclaw_config_import": True,
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
        "replay_runtime_auto_execute_guarded": True,
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
        "persona_deep_binding": True,
        "persona_channel_binding": True,
        "persona_agent_policy_binding": True,
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
        "core_cognition_loop": True,
        "capability_registry": True,
        "controller_route_gate": True,
        "turn_context_scope_minimized": True,
        "memory_policy_runtime": True,
        "core_model_memory_relevance": True,
        "external_world_watchlist_refresh": True,
        "active_runtime_loop": True,
        "active_loop_manual_tick": True,
        "runtime_cron_scheduler": True,
        "runtime_cron_placeholder_removed": True,
        "user_commitments": True,
        "commitment_proactive_push": True,
        "weather_probe_native": True,
        "agent_tool_proxy_contract": True,
        "agent_tool_bypass_verification": True,
        "runtime_routing_traces": True,
        "runtime_telemetry_metrics": True,
        "context_drift_detection": True,
        "tool_proxy_guard_smoke": True,
        "route_layer_split_phase1": True,
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
        "persona": state_store.read_json("persona_state.json"),
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
        "persona": state_store.read_json("persona_state.json"),
        "core_model": {
            "runtime_state": runtime_entity.core_model_runtime_state(),
            "status": awareness_loop.core_reasoning.status(),
        },
        "active_loop": active_loop.status(),
    }


if console_dir.exists():
    app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console_static")
