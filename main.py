import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.action_risk import ActionRiskAssessment, assess_action_risk
from core.architecture import architecture_snapshot
from core.agency_core import AgencyCore
from core.awareness_loop import AwarenessLoop
from core.commitment_core import CommitmentCore
from core.definitions import RiskLevel, normalize_risk
from core.env_loader import load_runtime_env
from core.foresight_engine import ForesightEngine
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from execution.action_executor import ActionExecutor
from guardian.review_queue import ReviewQueue
from interface.agent_adapter import ExecutionResult
from interface.event_normalizer import EventNormalizer
from interface.event_schema import Decision, Route, utc_now_iso
from interface.feishu_adapter import FeishuAdapter
from interface.feishu_ws_runner import FeishuWsRunner, feishu_connection_snapshot
from interface.intake_gateway import IntakeGateway
from interface.local_control_guard import LocalControlPolicy
from pydantic import Field
from rollback_audit.rollback_manager import RollbackManager
from rollback_audit.diff_tracker import DiffTracker
from rollback_audit.action_journal import ActionJournal
from rollback_audit.tool_trace import ToolTrace
from rollback_audit.replay import Replay
from rollback_audit.replay_runtime import ReplayRuntime
from routers.agent_memory import build_agent_memory_router
from routers.cases import build_cases_router
from routers.commitments import build_commitments_router
from routers.debug_audit import build_debug_audit_router
from routers.local_setup import build_local_setup_router
from routers.ops_runtime import build_ops_runtime_router
from routers.phase5 import build_phase5_router
from routers.phase6 import build_phase6_router
from routers.phase6_extensions import (
    build_phase6_extensions_router,
)
from routers.phase6_extension_artifacts import (
    build_phase6_extension_artifacts_router,
)
from routers.phase6_extension_source_checks import (
    build_phase6_extension_source_checks_router,
)
from routers.phase6_extension_isolated_runner import (
    build_phase6_extension_isolated_runner_router,
)
from routers.phase6_extension_generation import (
    build_phase6_extension_generation_router,
)
from routers.phase6_extension_dynamic_validations import (
    build_phase6_extension_dynamic_validations_router,
)
from routers.phase6_extension_releases import (
    build_phase6_extension_releases_router,
)
from routers.phase6_extension_deployments import (
    build_phase6_extension_deployments_router,
)
from routers.phase6_extension_pipelines import (
    build_phase6_extension_pipelines_router,
)
from routers.phase6_capability_gaps import (
    build_phase6_capability_gaps_router,
)
from routers.structured_observations import (
    build_structured_observations_router,
)
from routers.runtime_observability import build_runtime_observability_router
from routers.tool_governance import build_tool_governance_router
from runtime.active_loop import ActiveRuntimeLoop
from runtime.commitment_push import CommitmentPushRuntime
from runtime.agent_orchestrator import AgentOrchestrator
from runtime.alert_dispatcher import AlertDispatcher
from runtime.cron import Cron
from runtime.deployment_config import DeploymentConfigValidator
from runtime.external_world_refresh import ExternalWorldRefresh
from runtime.external_runtime_probe import ExternalRuntimeProbe
from runtime.foresight_runtime import ForesightRuntime
from runtime.learning_calibration_runtime import LearningCalibrationRuntime
from runtime.ops_monitor import OpsMonitor
from runtime.performance_portfolio import PerformancePortfolio
from runtime.proactive_checks import ProactiveChecks
from runtime.project_guardian import ProjectGuardianRuntime
from runtime.project_guardian_attention_runtime import (
    ProjectGuardianAttentionRuntime,
)
from runtime.project_guardian_producers import (
    ProjectGuardianProducerRuntime,
)
from runtime.project_guardian_github_ci import GitHubActionsCIProvider
from runtime.retention_policy import RetentionPolicy
from runtime.routing_metrics import RoutingMetrics
from runtime.runtime_matrix import RuntimeMatrix
from runtime.agent_capability_directory import AgentCapabilityDirectory
from runtime.read_only_agent_collaboration import (
    ReadOnlyAgentCollaborationRuntime,
)
from runtime.read_only_cognitive_loop import ReadOnlyCognitiveLoopRuntime
from runtime.extension_spec_quarantine import (
    ExtensionSpecQuarantine,
)
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactQuarantine,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourcePolicyGate,
)
from runtime.extension_isolated_runner_gate import (
    ExtensionIsolatedRunnerGate,
)
from runtime.trusted_isolated_runner import (
    TrustedIsolatedRunnerBackend,
)
from runtime.bounded_extension_generator import BoundedExtensionGenerator
from runtime.extension_generation_gate import ExtensionGenerationGate
from runtime.extension_dynamic_validation_gate import (
    ExtensionDynamicValidationGate,
)
from runtime.trusted_extension_validation_runner import (
    TrustedExtensionValidationRunner,
)
from runtime.extension_release_runtime import (
    build_extension_release_registry,
)
from runtime.trusted_extension_invocation_runner import (
    TrustedExtensionInvocationRunner,
)
from runtime.extension_deployment_gate import ExtensionDeploymentGate
from runtime.extension_pipeline_coordinator import (
    ExtensionPipelineCoordinator,
)
from runtime.capability_gap_registry import CapabilityGapRegistry
from runtime.structured_observation_ingress import (
    StructuredObservationIngress,
)
from runtime.soak_runner import SoakRunner
from runtime.state_refresh import StateRefresh
from runtime.openclaw_tool_broker import (
    OpenClawToolBroker,
    project_current_hook_enforcement,
)
from runtime.tool_governance_runtime import ToolGovernanceRuntime
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell
from runtime.safety_validation import SafetyValidation


load_runtime_env()
local_control_policy = LocalControlPolicy()
app = FastAPI(title="Veyra", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(local_control_policy.allowed_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


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


state_store = WorldStateStore(exclusive_writer=True, writer_owner="veyra-api")
tool_governance = ToolGovernanceRuntime(state_store)
foresight_runtime = ForesightRuntime(
    state_store,
    tool_receipt_resolver=tool_governance.resolve_receipt,
)
openclaw_tool_broker = OpenClawToolBroker(
    state_store,
    tool_governance,
    sandbox_base=state_store.root / "runtime" / "openclaw_tool_sandboxes",
    foresight_runtime=foresight_runtime,
)
runtime_entity = RuntimeEntity(state_store=state_store)
commitment_core = CommitmentCore(state_store)
awareness_loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime_entity, commitment_core=commitment_core)
awareness_loop.verifier.tool_receipt_resolver = tool_governance.resolve_receipt
awareness_loop.agent_registry.configure_openclaw_governance(
    dispatch_preparer=openclaw_tool_broker.prepare_dispatch,
    dispatch_canceller=openclaw_tool_broker.cancel_registered_run,
    run_evidence_resolver=tool_governance.run_evidence,
    status_resolver=openclaw_tool_broker.status,
)
commitment_core.memory_bridge = awareness_loop.memory_bridge
commitment_push = CommitmentPushRuntime(
    state_store=state_store,
    commitment_core=commitment_core,
    guardian=awareness_loop.guardian,
    foresight=awareness_loop.foresight,
)
routing_metrics = RoutingMetrics(state_store)
event_normalizer = EventNormalizer()
intake_gateway = IntakeGateway(awareness_loop, event_normalizer, state_store=state_store)
structured_observation_ingress = StructuredObservationIngress(
    state_store=state_store,
    event_awareness=awareness_loop.event_awareness,
    control_token=os.getenv("VEYRA_LOCAL_API_TOKEN") or "",
)
feishu_adapter = FeishuAdapter(intake_gateway, state_store=state_store)
feishu_ws_runner = FeishuWsRunner(state_store=state_store, adapter=feishu_adapter)
console_dir = Path("ui/console")
review_queue = ReviewQueue(state_store)
local_tool_sandbox = (
    state_store.root / "runtime" / "local_tool_sandbox"
).absolute()
local_tool_sandbox.mkdir(parents=True, exist_ok=True, mode=0o700)
rollback_manager = RollbackManager(
    state_store,
    snapshot_root=state_store.root / "runtime" / "rollback_snapshots",
    sandbox_root=local_tool_sandbox,
)
action_journal = ActionJournal(state_store)
replay_engine = Replay(state_store)
safe_shell = SafeShell(
    state_store=state_store,
    sandbox_root=local_tool_sandbox,
)
safe_file = SafeFile(
    state_store=state_store,
    sandbox_root=local_tool_sandbox,
)
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
    review_authorizer=review_queue.authorize_execution,
)
foresight_engine = ForesightEngine(reasoning=awareness_loop.core_reasoning)
# The autonomous loop is intentionally deterministic/read-only by default.
# Inject the same model-assist setting exposed by ProactiveChecks; otherwise the
# shared AgencyCore silently enables periodic model calls despite that setting.
agency_core = AgencyCore(
    state_store,
    reasoning=awareness_loop.core_reasoning,
    model_assist_enabled=False,
)
proactive_checks = ProactiveChecks(
    state_store,
    reasoning=awareness_loop.core_reasoning,
    review_queue=review_queue,
    agent_adapter_resolver=awareness_loop.agent_registry.selected,
    agency=agency_core,
)
learning_calibration = LearningCalibrationRuntime(
    state_store=state_store,
)
performance_portfolio = PerformancePortfolio(
    state_store=state_store,
)
# Close the loop: approved proactive remediation / agent-restart reviews now execute.
action_executor.proactive_executor = proactive_checks.execute_approved_proposal
diff_tracker = DiffTracker()
safety_validation = SafetyValidation()
retention_policy = RetentionPolicy(state_store)
state_refresh = StateRefresh(state_store, reasoning=awareness_loop.core_reasoning, model_assist_enabled=False)
external_world_refresh = ExternalWorldRefresh(state_store, reasoning=awareness_loop.core_reasoning)
ops_monitor = OpsMonitor(
    state_store,
    agent_status_resolver=lambda: awareness_loop.agent_registry.selected().connection_status(),
    retention_policy=retention_policy,
    safety_validation=safety_validation,
    model_status_resolver=lambda: awareness_loop.core_reasoning.status(),
    feishu_status_resolver=lambda: feishu_ws_runner.status(),
    active_loop_status_resolver=lambda: active_loop.status(),
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
project_guardian_producers = ProjectGuardianProducerRuntime(
    state_store=state_store,
    publish_git_observation=(
        awareness_loop.event_awareness.issue_project_guardian_git_publisher()
    ),
    publish_ci_observation=(
        awareness_loop.event_awareness.issue_project_guardian_ci_publisher()
    ),
    publish_deployment_intent=(
        awareness_loop.event_awareness
        .issue_project_guardian_deployment_intent_publisher()
    ),
    ci_provider=GitHubActionsCIProvider(),
)
project_guardian = ProjectGuardianRuntime(
    state_store=state_store,
    publish_event=awareness_loop.publish_event,
    event_fabric_mode=lambda: {
        "mode": awareness_loop.event_awareness.mode,
        "mode_epoch": awareness_loop.event_awareness.mode_epoch,
    },
)
project_guardian_attention = ProjectGuardianAttentionRuntime(
    state_store=state_store,
)


def _schedule_agent_recovery(payload: dict[str, Any]) -> dict[str, Any]:
    scan = replay_runtime.scan(limit=200)
    reviews = replay_runtime.run_pending(
        auto_create_reviews=True,
        auto_execute=False,
        limit=20,
    )
    return {
        "status": "review_scheduled" if reviews.get("processed_count") else "candidate_recorded",
        "task_id": payload.get("task_id"),
        "scan": {
            "candidate_count": scan.get("candidate_count"),
            "created_count": scan.get("created_count"),
        },
        "reviews": {
            "processed_count": reviews.get("processed_count"),
        },
    }


awareness_loop.task_tracker.set_recovery_hook(_schedule_agent_recovery)
read_only_cognitive_loop = ReadOnlyCognitiveLoopRuntime(
    state_store=state_store,
    reasoning=awareness_loop.core_reasoning,
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
    event_consumer=awareness_loop.process_event_inbox,
    project_guardian_producers=project_guardian_producers.run_once,
    project_guardian=project_guardian.run_once,
    project_guardian_attention=project_guardian_attention.run_once,
    case_recovery=awareness_loop.bounded_negotiation.recover_pending,
    cognitive_loop=read_only_cognitive_loop,
)
runtime_cron = Cron(state_store=state_store, active_loop=active_loop, commitment_push=commitment_push)
agent_orchestrator = AgentOrchestrator(
    state_store=state_store,
    registry=awareness_loop.agent_registry,
    task_tracker=awareness_loop.task_tracker,
    execution_trace=awareness_loop.execution_trace,
    verifier=awareness_loop.verifier,
)
phase6_capability_directory = AgentCapabilityDirectory(
    state_store=state_store,
    registry=awareness_loop.agent_registry,
)
phase6_collaboration = ReadOnlyAgentCollaborationRuntime(
    state_store=state_store,
    case_store=awareness_loop.durable_case_store,
    task_packet_builder=awareness_loop.task_packet_builder,
    bounded_negotiation=awareness_loop.bounded_negotiation,
    capability_directory=phase6_capability_directory,
    task_tracker=awareness_loop.task_tracker,
)
phase6_extension_specs = ExtensionSpecQuarantine(
    state_store=state_store,
)
phase6_extension_artifacts = ExtensionArtifactQuarantine(
    state_store=state_store,
    spec_quarantine=phase6_extension_specs,
)
phase6_extension_source_checks = ExtensionSourcePolicyGate(
    state_store=state_store,
    artifact_quarantine=phase6_extension_artifacts,
)
phase6_extension_isolated_backend = TrustedIsolatedRunnerBackend(
    docker_binary=(
        os.getenv("VEYRA_ISOLATED_RUNNER_DOCKER_BINARY") or None
    ),
    expected_image_id=(
        os.getenv("VEYRA_ISOLATED_RUNNER_EXPECTED_IMAGE_ID") or None
    ),
    expected_engine_identity_digest=(
        os.getenv("VEYRA_ISOLATED_RUNNER_EXPECTED_ENGINE_DIGEST") or None
    ),
    image_conformance_digest=(
        os.getenv("VEYRA_ISOLATED_RUNNER_CONFORMANCE_DIGEST") or None
    ),
    conformance_certified=(
        _env_bool("VEYRA_ISOLATED_RUNNER_CONFORMANCE_CERTIFIED") is True
    ),
)
phase6_extension_isolated_runner = ExtensionIsolatedRunnerGate(
    state_store=state_store,
    source_check_gate=phase6_extension_source_checks,
    backend=phase6_extension_isolated_backend,
)
phase6_extension_source_checks.bind_isolated_runner_projection(
    phase6_extension_isolated_runner.projection_for_source_check
)
phase6_extension_generator = BoundedExtensionGenerator(
    awareness_loop.core_reasoning.client,
)
phase6_extension_generation = ExtensionGenerationGate(
    state_store=state_store,
    spec_quarantine=phase6_extension_specs,
    artifact_quarantine=phase6_extension_artifacts,
    generator=phase6_extension_generator,
)
phase6_extension_validation_backend = TrustedExtensionValidationRunner(
    isolation_backend=phase6_extension_isolated_backend,
    expected_validation_conformance_digest=(
        os.getenv(
            "VEYRA_PHASE6_DYNAMIC_VALIDATION_CONFORMANCE_DIGEST"
        )
        or None
    ),
    validation_conformance_certified=(
        _env_bool(
            "VEYRA_PHASE6_DYNAMIC_VALIDATION_CONFORMANCE_CERTIFIED"
        )
        is True
    ),
)
phase6_extension_dynamic_validations = ExtensionDynamicValidationGate(
    state_store=state_store,
    isolated_runner_gate=phase6_extension_isolated_runner,
    backend=phase6_extension_validation_backend,
)
phase6_capability_gaps = CapabilityGapRegistry(
    state_store=state_store,
    spec_quarantine=phase6_extension_specs,
)
commitment_core.self_improvement.capability_gaps = phase6_capability_gaps
phase6_extension_releases = build_extension_release_registry(
    state_store=state_store,
    generation_gate=phase6_extension_generation,
    dynamic_validation_gate=phase6_extension_dynamic_validations,
)
phase6_extension_invocation_backend = TrustedExtensionInvocationRunner(
    isolation_backend=phase6_extension_isolated_backend,
    expected_invocation_conformance_digest=(
        os.getenv("VEYRA_PHASE6_INVOCATION_CONFORMANCE_DIGEST") or None
    ),
    invocation_conformance_certified=(
        _env_bool("VEYRA_PHASE6_INVOCATION_CONFORMANCE_CERTIFIED") is True
    ),
)
phase6_extension_deployments = ExtensionDeploymentGate(
    state_store=state_store,
    release_registry=phase6_extension_releases,
    invocation_runner=phase6_extension_invocation_backend,
    control_token=os.getenv("VEYRA_LOCAL_API_TOKEN") or None,
    approver_token=(
        os.getenv("VEYRA_PHASE6_EXTENSION_APPROVER_TOKEN") or None
    ),
    lifecycle_mode=(
        os.getenv("VEYRA_PHASE6_EXTENSION_DEPLOYMENT_MODE")
        or "record_only"
    ),
)
phase6_extension_pipeline = ExtensionPipelineCoordinator(
    state_store=state_store,
    generation_gate=phase6_extension_generation,
    artifact_quarantine=phase6_extension_artifacts,
    source_check_gate=phase6_extension_source_checks,
    isolated_runner_gate=phase6_extension_isolated_runner,
    dynamic_validation_gate=phase6_extension_dynamic_validations,
    release_registry=phase6_extension_releases,
    deployment_gate=phase6_extension_deployments,
    enabled=(
        _env_bool("VEYRA_PHASE6_EXTENSION_PIPELINE_ENABLED") is True
    ),
    control_token=os.getenv("VEYRA_LOCAL_API_TOKEN") or "",
)
phase6_capability_directory.bind_extension_deployment_gate(
    phase6_extension_deployments
)


@app.middleware("http")
async def guard_local_control_plane(request: Request, call_next):
    server = request.scope.get("server")
    actual_server_host = (
        str(server[0])
        if isinstance(server, (tuple, list)) and server
        else None
    )
    decision = local_control_policy.authorize(
        method=request.method,
        path=request.url.path,
        headers=request.headers,
        actual_server_host=actual_server_host,
    )
    if not decision.allowed:
        return JSONResponse(
            status_code=decision.status_code,
            content={
                "status": "blocked",
                "code": decision.code,
                "reason": decision.reason,
                "control_plane": local_control_policy.status(
                    actual_server_host=actual_server_host,
                ),
            },
        )
    return await call_next(request)


@app.on_event("startup")
async def startup_integrations() -> None:
    try:
        runtime_entity.set_status(runtime_entity.lifecycle.status)
    except Exception as exc:
        state_store.append_jsonl("action_record.jsonl", {"route": "runtime_startup_heartbeat", "status": "error", "artifacts": {"error": str(exc), "error_type": type(exc).__name__}})
    try:
        retention_summary = retention_policy.summary()
        if any(item.get("status") == "over_limit" for item in retention_summary.get("files", []) if isinstance(item, dict)):
            retention_policy.enforce()
    except Exception as exc:
        state_store.append_jsonl("action_record.jsonl", {"route": "runtime_startup_retention", "status": "error", "artifacts": {"error": str(exc), "error_type": type(exc).__name__}})
    try:
        healing = commitment_core.heal_invalid_commitments()
        if healing.get("invalid_count"):
            state_store.append_jsonl("action_record.jsonl", {"route": "runtime_startup_commitment_healing", "status": "success", "artifacts": healing})
    except Exception as exc:
        state_store.append_jsonl("action_record.jsonl", {"route": "runtime_startup_commitment_healing", "status": "error", "artifacts": {"error": str(exc), "error_type": type(exc).__name__}})
    try:
        active_loop_config = state_store.read_json("ops_config.json").get("active_loop", {})
        if not isinstance(active_loop_config, dict):
            active_loop_config = {}
        env_autostart = _env_bool("VEYRA_ACTIVE_LOOP_AUTOSTART")
        autostart = env_autostart if env_autostart is not None else bool(active_loop_config.get("autostart", False))
        if autostart:
            active_loop.start(interval_seconds=float(active_loop_config.get("interval_seconds") or 300.0))
            retention_summary = retention_policy.summary()
            if any(item.get("status") == "over_limit" for item in retention_summary.get("files", []) if isinstance(item, dict)):
                retention_policy.enforce()
    except Exception as exc:
        state_store.write_json(
            "active_loop_state.json",
            {
                **state_store.read_json("active_loop_state.json"),
                "status": "error",
                "last_error": str(exc),
                "error_type": type(exc).__name__,
                "updated_at": utc_now_iso(),
            },
        )
    if _env_bool("VEYRA_FEISHU_WS_AUTOSTART") is False:
        return
    try:
        feishu_ws_runner.autostart_if_configured()
    except Exception as exc:
        state_store.write_json(
            "feishu_ws_state.json",
            {
                "status": "error",
                "last_error": str(exc),
                "error_type": type(exc).__name__,
                "started_at": None,
                "last_event_at": None,
            },
        )


@app.get("/health")
async def health() -> dict[str, Any]:
    return ops_monitor.health()


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
    hook_snapshot = openclaw_tool_broker.status()
    try:
        openclaw_status = awareness_loop.agent_registry.get(
            "openclaw"
        ).connection_status(force_refresh=True)
    except Exception as exc:
        openclaw_status = {
            "connected": False,
            "status": "unavailable",
            "reason": str(exc)[:600],
        }
    hook_status = project_current_hook_enforcement(
        hook_snapshot,
        openclaw_status,
    )
    local_statuses = {
        "shell": safe_shell.status(),
        "file": safe_file.status(),
        "rollback": rollback_manager.status(),
        "browser": safe_browser.status(),
        "api": safe_api.status(),
    }
    return {
        "status": hook_status.get("status", "validation_pending"),
        "scope": hook_status.get("scope"),
        "tool_proxy_enforced": hook_status.get("tool_proxy_enforced") is True,
        "hook_enforcement": hook_status,
        **local_statuses,
        "legacy_side_effect_ingress": "deny_only",
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
    def _resolve(self, value: Any) -> Any:
        return value() if callable(value) else value

    def __getitem__(self, key: str) -> Any:
        return self._resolve(super().__getitem__(key))

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            return default
        return self._resolve(super().__getitem__(key))


app.include_router(
    build_runtime_observability_router(
        trace_recorder=awareness_loop.runtime_trace,
        metrics=routing_metrics,
    )
)
app.include_router(
    build_phase5_router(
        state_store=state_store,
        foresight_runtime=foresight_runtime,
        learning_runtime=learning_calibration,
        performance_portfolio=performance_portfolio,
        sandbox_playbook=proactive_checks.sandbox_repair,
        playbook_registry=proactive_checks.playbook_registry,
    )
)
app.include_router(
    build_phase6_router(
        collaboration=phase6_collaboration,
    )
)
app.include_router(
    build_phase6_extensions_router(
        quarantine=phase6_extension_specs,
        artifact_quarantine=phase6_extension_artifacts,
        source_check_gate=phase6_extension_source_checks,
    )
)
app.include_router(
    build_phase6_extension_artifacts_router(
        quarantine=phase6_extension_artifacts,
        source_check_gate=phase6_extension_source_checks,
    )
)
app.include_router(
    build_phase6_extension_source_checks_router(
        gate=phase6_extension_source_checks,
    )
)
app.include_router(
    build_phase6_extension_isolated_runner_router(
        gate=phase6_extension_isolated_runner,
    )
)
app.include_router(
    build_phase6_extension_generation_router(
        gate=phase6_extension_generation,
    )
)
app.include_router(
    build_phase6_extension_dynamic_validations_router(
        gate=phase6_extension_dynamic_validations,
    )
)
app.include_router(
    build_phase6_extension_releases_router(
        registry=phase6_extension_releases,
    )
)
app.include_router(
    build_phase6_extension_deployments_router(
        gate=phase6_extension_deployments,
    )
)
app.include_router(
    build_phase6_extension_pipelines_router(
        coordinator=phase6_extension_pipeline,
    )
)
app.include_router(
    build_phase6_capability_gaps_router(
        registry=phase6_capability_gaps,
    )
)
app.include_router(
    build_structured_observations_router(
        ingress=structured_observation_ingress,
    )
)
app.include_router(
    build_tool_governance_router(
        tool_governance,
        openclaw_tool_broker,
        plugin_status_resolver=lambda: (
            awareness_loop.agent_registry.get(
                "openclaw"
            ).connection_status(force_refresh=True)
        ),
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
                "phase6_collaboration": (
                    lambda: phase6_collaboration
                ),
            }
        )
    )
)
app.include_router(
    build_cases_router(
        _DynamicDeps(
            {
                "durable_case_store": (
                    lambda: awareness_loop.durable_case_store
                ),
                "bounded_negotiation": (
                    lambda: awareness_loop.bounded_negotiation
                ),
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
                "project_guardian": lambda: project_guardian,
                "project_guardian_producers": (
                    lambda: project_guardian_producers
                ),
                "project_guardian_attention": (
                    lambda: project_guardian_attention
                ),
                "read_only_cognitive_loop": (
                    lambda: read_only_cognitive_loop
                ),
                "learning_calibration": (
                    lambda: learning_calibration
                ),
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
                "review_queue": lambda: review_queue,
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
app.include_router(
    build_local_setup_router(
        _DynamicDeps(
            {
                "state_store": lambda: state_store,
                "agent_status": lambda: awareness_loop.agent_registry.selected().connection_status(),
                "feishu_status": lambda: feishu_ws_runner.status(),
                "deployment_readiness": lambda: _deployment_readiness(),
            }
        )
    )
)


@app.post("/events/message")
async def message(request: MessageRequest):
    receipt = await run_in_threadpool(
        intake_gateway.receive_message,
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
    return await run_in_threadpool(
        intake_gateway.receive_message,
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
    return {
        "status": "blocked",
        "reason": (
            "Legacy shell ingress is deny-only. Use a registered OpenClaw "
            "veyra_shell_probe call or an explicit canonical review."
        ),
        "execution_authority_enabled": False,
        "command": request.command,
    }


@app.post("/tool-proxy/file/read")
async def tool_proxy_file_read(request: FileReadRequest):
    return safe_file.read_text(request.path)


@app.post("/tool-proxy/file/write")
async def tool_proxy_file_write(request: FileWriteRequest):
    return {
        "status": "blocked",
        "reason": (
            "Legacy file-write ingress is deny-only. Use the registered "
            "veyra_file_write tool so preflight, execution and effect evidence "
            "share one exact invocation."
        ),
        "execution_authority_enabled": False,
        "path": request.path,
    }


@app.post("/tool-proxy/browser/open")
async def tool_proxy_browser_open(request: BrowserOpenRequest):
    return {
        "status": "needs_action_proposal",
        "reason": (
            "Caller-supplied approved_by is diagnostic only. Browser execution "
            "requires a canonical review-backed executor path."
        ),
        "execution_authority_enabled": False,
        "url": request.url,
    }


@app.post("/tool-proxy/api/request")
async def tool_proxy_api_request(request: APIProxyRequest):
    return {
        "status": "needs_action_proposal",
        "reason": (
            "Caller-supplied approved_by is diagnostic only. API execution "
            "requires a canonical review-backed executor path."
        ),
        "execution_authority_enabled": False,
    }


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
    risk_assessment = assess_action_risk(request.action)
    detected_risk = RiskLevel(risk_assessment.risk_level)
    risk_order = list(RiskLevel)
    risk_level = detected_risk if risk_order.index(detected_risk) > risk_order.index(guessed_risk) else guessed_risk
    risk = risk_level.value
    proposal_decision = _proposal_decision(risk_level, action_text, request, risk_assessment)
    foresight = foresight_engine.predict_text_action(action_text, risk_level, decision=proposal_decision.to_dict())
    guardian_decision = awareness_loop.guardian.review_text_action(
        text=action_text,
        decision=proposal_decision,
        foresight=foresight,
    )
    guardian_decision["risk_assessment"] = _effective_risk_assessment(risk_assessment, guessed_risk, risk_level)
    if risk == "R5":
        tool_trace = _record_action_proposal_trace(request, "blocked", guardian_decision, risk_assessment=risk_assessment)
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
                    "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
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
            "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
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
        tool_trace = _record_action_proposal_trace(request, "needs_confirmation", guardian_decision, review=review, risk_assessment=risk_assessment)
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
                    "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
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
            "risk_level": risk,
            "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
            "guardian_decision": guardian_decision,
            "review": review,
            "tool_trace": tool_trace,
            "verification": verification,
        }
    execution = {
        "status": "needs_governed_execution",
        "reason": (
            "R0-R2 proposals do not mint execution authority. Use a registered "
            "Veyra-backed OpenClaw tool; unsupported external actions remain "
            "non-executable in Phase 3."
        ),
        "execution_authority_enabled": False,
    }
    tool_trace = (
        execution.get("tool_trace")
        if isinstance(execution.get("tool_trace"), dict)
        else _record_action_proposal_trace(request, execution.get("status", "unknown"), guardian_decision, risk_assessment=risk_assessment)
    )
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
                "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
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
        "risk_level": risk,
        "risk_assessment": _effective_risk_assessment(risk_assessment, guessed_risk, risk_level),
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


def _proposal_decision(
    risk_level: RiskLevel,
    action_text: str,
    request: ActionProposalRequest,
    risk_assessment: ActionRiskAssessment,
) -> Decision:
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
        reason=request.reason or risk_assessment.reason or f"ActionProposal from {request.agent}: {action_text}",
        requires_confirmation=risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
        intent="action",
        complexity="simple" if risk_level in {RiskLevel.R0, RiskLevel.R1, RiskLevel.R2} else "moderate",
        capability=capability,
        signals=list(dict.fromkeys([f"risk:{risk_level.value}", f"agent:{request.agent}", "action_proposal", *risk_assessment.signals])),
        constraints=constraints,
    )


def _effective_risk_assessment(
    assessment: ActionRiskAssessment,
    guessed_risk: RiskLevel,
    effective_risk: RiskLevel,
) -> dict[str, Any]:
    payload = assessment.to_dict()
    payload["guessed_risk_level"] = guessed_risk.value
    payload["effective_risk_level"] = effective_risk.value
    payload["risk_floor_applied"] = effective_risk.value == assessment.risk_level
    return payload


def _record_action_proposal_trace(
    request: ActionProposalRequest,
    status: str,
    guardian_decision: dict[str, Any],
    *,
    review: dict[str, Any] | None = None,
    risk_assessment: ActionRiskAssessment | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "trace_id": request.proposal_id,
        "operation": "action_proposal_guard",
        "review_id": (review or {}).get("review_id"),
        "reason": guardian_decision.get("reason"),
        "risk_assessment": risk_assessment.to_dict() if risk_assessment else guardian_decision.get("risk_assessment"),
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


def _mvp_validation_snapshot(agent_status_data: dict[str, Any]) -> dict[str, Any]:
    capabilities = agent_status_data.get("capabilities") if isinstance(agent_status_data.get("capabilities"), dict) else {}
    features = capabilities.get("features") if isinstance(capabilities.get("features"), dict) else {}
    proxy_validation = openclaw_tool_broker.status()
    proxy_enforced = (
        features.get("tool_proxy_enforced") is True
        and proxy_validation.get("tool_proxy_enforced") is True
        and proxy_validation.get("status") == "validated"
    )

    memory_status = awareness_loop.memory_bridge.provider_status(
        probe=False
    )
    memory_validations = memory_status.get("validation") if isinstance(memory_status.get("validation"), dict) else {}
    selected_memory = memory_validations.get("selected") if isinstance(memory_validations.get("selected"), dict) else {}
    model_status = awareness_loop.core_reasoning.status()
    model_validation = model_status.get("validation") if isinstance(model_status.get("validation"), dict) else {}
    matrix_status = runtime_matrix.status()
    feishu_status_data = feishu_ws_runner.status()
    feishu_connection = feishu_connection_snapshot(feishu_status_data)
    feishu_connected = bool(feishu_connection.get("connected"))
    feishu_fresh = bool(
        feishu_status_data.get("last_processed_after_start")
        and feishu_status_data.get("last_reply_sent_after_start")
        and not feishu_status_data.get("processing_failure_unrecovered")
    )
    control_status = local_control_policy.status()

    return {
        "agent_runtime": {
            "implementation": "implemented",
            "status": "validated" if agent_status_data.get("connected") else "validation_pending",
            "validated": bool(agent_status_data.get("connected")),
            "runtime_status": agent_status_data.get("status"),
        },
        "agent_tool_proxy_enforcement": {
            "implementation": "implemented",
            "status": "validated" if proxy_enforced else "validation_pending",
            "validated": proxy_enforced,
            "runtime_claim": features.get("tool_proxy_enforced"),
            "evidence": proxy_validation,
            "note": "Agent connection alone never proves Tool Proxy enforcement.",
        },
        "local_tool_proxy": {
            "implementation": "implemented",
            "status": "configured",
            "validated": False,
            "executors": _tool_proxy_status(),
        },
        "memory_selected_provider": {
            "implementation": "implemented",
            "status": selected_memory.get("status", "validation_pending"),
            "validated": bool(selected_memory.get("validated")),
            "validation": selected_memory,
            "fallback": "local",
        },
        "runtime_matrix": {
            "implementation": "implemented",
            "status": matrix_status.get("status", "not_run"),
            "validated": matrix_status.get("status") == "ready",
            "freshness": matrix_status.get("freshness"),
            "observed_at": matrix_status.get("observed_at") or matrix_status.get("run_at"),
            "expires_at": matrix_status.get("expires_at"),
        },
        "core_model_transport": {
            "implementation": "implemented",
            "status": model_validation.get("status", "validation_pending"),
            "validated": bool(model_validation.get("validated")),
            "validation": model_validation,
        },
        "feishu_websocket": {
            "implementation": "implemented",
            "status": "validated"
            if feishu_connected and feishu_fresh
            else "processing_failed"
            if feishu_connected and feishu_status_data.get("processing_failure_unrecovered")
            else "waiting_for_event"
            if feishu_connected and not feishu_status_data.get("last_event_after_start")
            else "processing_incomplete"
            if feishu_connected and not feishu_status_data.get("last_processed_after_start")
            else "reply_unverified"
            if feishu_connected and not feishu_status_data.get("last_reply_sent_after_start")
            else "waiting_for_event"
            if feishu_connected
            else str(feishu_status_data.get("status") or "not_configured"),
            "validated": feishu_connected and feishu_fresh,
            "connected": feishu_connected,
            "thread_alive": bool(feishu_status_data.get("thread_alive")),
            "last_event_after_start": feishu_status_data.get("last_event_after_start"),
            "last_processed_after_start": feishu_status_data.get("last_processed_after_start"),
            "last_reply_sent_after_start": feishu_status_data.get("last_reply_sent_after_start"),
            "processing_failure_unrecovered": feishu_status_data.get("processing_failure_unrecovered"),
        },
        "local_control_security": {
            "implementation": "implemented",
            "status": control_status.get("status"),
            "validated": control_status.get("status") in {"local_only", "token_required"},
            "configuration": control_status,
        },
    }


@app.get("/mvp/status")
async def mvp_status():
    agent_status_data = awareness_loop.agent_registry.selected().connection_status()
    runtime_validation = _runtime_validation_summary(agent_status_data)
    capability_validation = _mvp_validation_snapshot(agent_status_data)
    ops_health = ops_monitor.health()
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
    if ops_health.get("status") in {"critical", "degraded"}:
        overall_status = "degraded"
    elif capability_validation["agent_tool_proxy_enforcement"]["validated"]:
        overall_status = "ready_for_soak"
    else:
        overall_status = "validation_pending"
    return {
        "mvp": "core_governance",
        "status": overall_status,
        "implementation_status": "implemented",
        "core_loops": implemented_loops,
        "core_loop_semantics": "implementation_flags_only",
        "capability_validation": capability_validation,
        "validation": {
            "codebase": "implemented",
            "local_self_tests": "available",
            "runtime_validation": runtime_validation,
            "production_validation": "ready_for_soak" if overall_status == "ready_for_soak" else "pending_live_environment",
            "data_reality": "live_local_api_no_mock_fixtures",
            "health": ops_health,
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
