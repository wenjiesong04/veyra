from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from core.definitions import RiskLevel, lifecycle_statuses, operational_modes, risk_catalog
from core.context_scope import item_visible_to_scope
from core.learning_record import LearningRecordValidationError
from interface.event_schema import utc_now_iso
from memory_bridge.scope import normalize_scope_component
from runtime.project_guardian_producers import ProjectGuardianGoalConflict
from runtime.project_guardian_attention_runtime import (
    ProjectGuardianAttentionConflict,
)
from runtime.learning_calibration_runtime import (
    LearningCalibrationConflict,
    LearningCalibrationStorageError,
)
from runtime.suggestion_outbox import SuggestionOutboxConflict


class ReviewDecisionRequest(BaseModel):
    reason: str = ""


class CoreModelConfigRequest(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    timeout: float | None = None
    decision_mode: str | None = None
    max_tokens: int | None = None


class ExternalWatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )

    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    target: str
    kind: str | None = None
    reason: str = ""
    enabled: bool = True


class BeliefRefreshRequest(BaseModel):
    expire_after_seconds: int | None = 3600
    prune_expired_after_seconds: int | None = None


class ReplayRuntimeRequest(BaseModel):
    limit: int = 200
    auto_create_reviews: bool = True
    auto_execute: bool = False
    allow_r4_restore: bool = False


class ReplayRuntimeConfigRequest(BaseModel):
    auto_execute_enabled: bool | None = None
    allow_r4_restore: bool | None = None


class EventAwarenessConfigRequest(BaseModel):
    mode: str


class GeneralSuggestionModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["disabled", "record_only", "shadow", "advise_only"]
    expected_state_revision: int = Field(ge=0)


class GeneralSuggestionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    sandbox_enabled: bool = False
    daily_budget: int = Field(default=1, ge=0, le=1)
    timezone: str | None = Field(default=None, min_length=1, max_length=120)
    quiet_hours: dict[str, Any] | None = None
    cooldown_seconds: int = Field(ge=0, le=2592000)
    dismiss_cooldown_seconds: int = Field(ge=0, le=2592000)
    expected_state_revision: int = Field(ge=0)


class GeneralSuggestionFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_state_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=500)


class GeneralSuggestionCategoricalFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["veyra.suggestion_feedback_command.v1"]
    feedback_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,240}$")
    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    proposal_revision: str = Field(pattern=r"^sugr_[0-9a-f]{24}$")
    general_situation_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,240}$")
    parent_revision: int = Field(ge=1)
    label: Literal[
        "useful",
        "not_useful",
        "too_frequent",
        "wrong_timing",
        "wrong_evidence",
    ]
    expected_outbox_state_revision: int = Field(ge=0)
    supersedes_learning_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._:-]{1,240}$",
    )


class ProjectGuardianConfigRequest(BaseModel):
    mode: str


class ProjectReleaseGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    workspace_id: str
    repo_id: str
    target_ref: str
    target_environment: str
    release_cycle: str
    workspace_path: str
    active_from: str | None = None
    active_until: str | None = None
    goal_id: str | None = None
    expected_state_revision: int | None = None
    github_actions_workflow: str | None = None
    github_actions_required_jobs: list[str] | None = None
    github_actions_app_id: int | None = None


class ProjectReleaseGoalStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    expected_state_revision: int
    status: str


class ProjectDeploymentIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "veyra.project_guardian_deployment_intent_command.v1"
    ]
    user_id: str
    session_id: str
    goal_revision: str
    expected_goal_state_revision: int
    target_sha: str
    target_environment: str
    transition: Literal["declare", "withdraw"]
    operation_id: str
    occurred_at: str


class ProjectGuardianAttentionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "veyra.project_guardian_attention_policy_command.v1"
    ]
    user_id: str
    goal_revision: str
    expected_goal_state_revision: int
    attention_group_id: str
    goal_priority: float
    deadline_at: str
    timezone: str
    notifications_paused: bool
    quiet_hours: dict[str, Any]
    daily_notification_budget: int
    expected_policy_revision: str | None = None


class ProjectGuardianAttentionDismissalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "veyra.project_guardian_attention_dismissal_command.v1"
    ]
    user_id: str
    dismissed: bool


_PUBLIC_STATUS_VALUES = frozenset(
    {
        "unknown",
        "success",
        "fresh",
        "stale",
        "expired",
        "conflict",
        "degraded",
        "healthy",
        "available",
        "offline",
        "configured",
        "unconfigured",
        "disabled",
        "enabled",
        "idle",
        "running",
        "stopped",
        "pending",
        "complete",
        "completed",
        "recorded",
        "shadow",
        "record_only",
        "accepted",
        "failed",
        "skipped",
        "active",
        "blocked",
        "cancelled",
        "critical",
        "error",
        "needs_confirmation",
        "needs_action_proposal",
        "needs_more_probe",
        "needs_user_input",
        "ok",
        "partially_success",
        "paused",
        "pending_confirmation",
        "ready",
        "snapshot_stale",
        "unavailable",
        "validation_pending",
        "verified_failed",
        "verified_success",
        "warning",
    }
)


def _public_status(value: Any, default: str = "unknown") -> str:
    selected = str(value or default).strip().lower()
    return selected if selected in _PUBLIC_STATUS_VALUES else default


def build_debug_audit_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    async def state() -> dict[str, Any]:
        payload = _public_state(deps["state_store"].read_all())
        payload["agency"] = _public_agency_projection(
            deps["agency_core"].state()
        )
        return payload

    @router.get("/state/health")
    async def state_health() -> dict[str, Any]:
        return deps["state_store"].state_health()

    @router.get("/capabilities/snapshot")
    async def capabilities_snapshot() -> dict[str, Any]:
        return deps["awareness_loop"].capabilities.snapshot()

    @router.post("/capabilities/refresh")
    async def capabilities_refresh() -> dict[str, Any]:
        loop = deps["awareness_loop"]
        adapter = loop.agent_registry.selected()
        selected_agent = loop.agent_registry.selected_name()
        invalidate = getattr(adapter, "invalidate_capabilities_cache", None)
        if callable(invalidate):
            invalidate()
        snapshot = adapter.fetch_capabilities()
        observed_at = utc_now_iso()
        snapshot.update(
            {
                "runtime": snapshot.get("runtime") or selected_agent,
                "source": "agent_adapter.fetch_capabilities",
                "updated_at": observed_at,
                "ttl_seconds": 300,
            }
        )
        deps["state_store"].patch_json(
            "executor_state.json",
            {
                "selected_agent": selected_agent,
                "capabilities": snapshot,
                "capability_snapshot": snapshot,
                "status": snapshot.get("status") or "unknown",
                "connected": snapshot.get("connected"),
                "updated_at": observed_at,
                "ttl_seconds": 300,
            },
        )
        return loop.capabilities.snapshot()

    @router.get("/core/model/status")
    async def core_model_status() -> dict[str, Any]:
        return deps["awareness_loop"].core_reasoning.status()

    @router.post("/core/model/config")
    async def configure_core_model(request: CoreModelConfigRequest) -> dict[str, Any]:
        patch = request.model_dump(exclude_none=True)
        clear_direct_api_key = False
        if "api_key" in patch:
            if str(patch.get("api_key") or "").strip():
                raise HTTPException(status_code=422, detail="Do not store direct API keys in Veyra state; set api_key_env and provide the secret through the environment.")
            clear_direct_api_key = True
            patch.pop("api_key", None)
        if "decision_mode" in patch and patch["decision_mode"] not in {"auto", "always"}:
            raise HTTPException(status_code=422, detail="decision_mode must be 'auto' or 'always'")
        if "provider" in patch and patch["provider"] != "openai_compatible":
            raise HTTPException(status_code=422, detail="only openai_compatible provider is supported")
        if "max_tokens" in patch and not 128 <= int(patch["max_tokens"]) <= 2000:
            raise HTTPException(status_code=422, detail="max_tokens must be between 128 and 2000")
        state_store = deps["state_store"]

        def update_core_model(config: dict[str, Any]) -> None:
            core_model = config.setdefault("core_model", {})
            if not isinstance(core_model, dict):
                config["core_model"] = core_model = {}
            if clear_direct_api_key:
                core_model.pop("api_key", None)
            core_model.update(patch)

        state_store.mutate_json("agent_config.json", update_core_model)
        return deps["awareness_loop"].core_reasoning.status()

    @router.get("/architecture")
    async def architecture() -> dict[str, Any]:
        return deps["architecture_snapshot"]()

    @router.get("/definitions")
    async def definitions() -> dict[str, Any]:
        return {
            "risk_levels": risk_catalog(),
            "lifecycle_statuses": lifecycle_statuses(),
            "operational_modes": operational_modes(),
        }

    @router.get("/heartbeat")
    async def heartbeat() -> dict[str, Any]:
        return {"heartbeat": deps["state_store"].read_text("heartbeat.md")}

    @router.get("/logs/events")
    async def event_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("event_log.jsonl", limit=limit)}

    @router.get("/logs/actions")
    async def action_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("action_record.jsonl", limit=limit)}

    @router.get("/reviews/actions")
    async def action_reviews(status: str | None = None, limit: int = 100) -> dict[str, Any]:
        return {"items": deps["review_queue"].list(status=status)[-limit:]}

    @router.post("/reviews/{review_id}/approve")
    async def approve_review(review_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        try:
            review, claim_token = deps["review_queue"].approve_and_claim(
                review_id,
                request.reason,
            )
            if claim_token is None:
                if review.get("status") != "approved":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Review cannot be approved from terminal status "
                            f"{review.get('status')!r}."
                        ),
                    )
                return review
            execution = await run_in_threadpool(
                deps["action_executor"].execute_review,
                review,
                claim_token=claim_token,
            )
            return deps["review_queue"].update_execution(
                review_id,
                execution,
                claim_token=claim_token,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/reviews/{review_id}/reject")
    async def reject_review(review_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        try:
            return deps["review_queue"].decide(review_id, "rejected", request.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/logs/tools")
    async def tool_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("tool_call_log.jsonl", limit=limit)}

    @router.get("/logs/policy")
    async def policy_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("policy_trace.jsonl", limit=limit)}

    @router.get("/logs/execution")
    async def execution_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("execution_trace.jsonl", limit=limit)}

    @router.get("/logs/rollback")
    async def rollback_logs(limit: int = 100) -> dict[str, Any]:
        return {
            "schema_version": "veyra.rollback_log.public.v1",
            "items": [
                _public_rollback_log_item(item)
                for item in deps["state_store"].read_jsonl(
                    "rollback_log.jsonl",
                    limit=max(0, min(int(limit), 500)),
                )
                if isinstance(item, dict)
            ],
        }

    @router.get("/logs/memory")
    async def memory_logs(
        *,
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        try:
            user_scope = normalize_scope_component(
                user_id,
                "user_id",
            )
            session_scope = normalize_scope_component(
                session_id,
                "session_id",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=str(exc),
            ) from exc
        rows = deps["state_store"].read_jsonl(
            "memory_log.jsonl",
            limit=10_000,
        )
        scoped = [
            row
            for row in rows
            if _memory_log_visible(
                row,
                user_id=user_scope,
                session_id=session_scope,
            )
        ]
        return {"items": scoped[-limit:]}

    @router.get("/logs/core-model")
    async def core_model_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("core_model_trace.jsonl", limit=limit)}

    @router.get("/logs/alerts")
    async def alert_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("alert_log.jsonl", limit=limit)}

    @router.get("/audit/journal")
    async def audit_journal(
        limit: int = 100,
        event_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return deps["action_journal"].timeline(limit=limit, event_id=event_id, task_id=task_id, trace_id=trace_id)

    @router.get("/audit/time-travel")
    async def audit_time_travel(until: str | None = None, limit: int = 200) -> dict[str, Any]:
        return deps["action_journal"].time_travel(until=until, limit=limit)

    @router.get("/audit/replay/{trace_id}")
    async def audit_replay(trace_id: str) -> dict[str, Any]:
        return deps["replay_engine"].replay(trace_id)

    def create_replay_review(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("proposal_status") != "ready":
            return payload
        proposal = payload["proposal"]
        snapshot_id = str(payload.get("snapshot_id") or proposal.get("action", {}).get("snapshot_id") or "")
        task_text = f"Replay compensation: restore snapshot {snapshot_id}"
        review = deps["review_queue"].create(
            event_id=str(payload.get("plan", {}).get("event_id") or f"replay_{snapshot_id}"),
            task_text=task_text,
            risk_level="R4",
            foresight=deps["foresight_engine"].predict_text_action(task_text, RiskLevel.R4),
            guardian_decision={
                "decision": "ask_user",
                "risk_level": "R4",
                "reason": "Replay compensation restores prior filesystem state and requires explicit approval.",
            },
            proposal=proposal,
        )
        return {"status": "needs_confirmation", "review": review, "proposal": proposal, "plan": payload.get("plan")}

    @router.post("/audit/replay/{trace_id}/propose")
    async def audit_replay_propose(trace_id: str) -> dict[str, Any]:
        return create_replay_review(deps["replay_engine"].compensation_proposal(trace_id=trace_id))

    @router.get("/audit/replay/event/{event_id}")
    async def audit_replay_event(event_id: str) -> dict[str, Any]:
        return deps["replay_engine"].plan(event_id=event_id)

    @router.post("/audit/replay/event/{event_id}/propose")
    async def audit_replay_event_propose(event_id: str) -> dict[str, Any]:
        return create_replay_review(deps["replay_engine"].compensation_proposal(event_id=event_id))

    @router.get("/audit/replay/runtime/status")
    async def audit_replay_runtime_status() -> dict[str, Any]:
        return deps["replay_runtime"].status()

    @router.post("/audit/replay/runtime/scan")
    async def audit_replay_runtime_scan(request: ReplayRuntimeRequest | None = None) -> dict[str, Any]:
        payload = request or ReplayRuntimeRequest()
        return deps["replay_runtime"].scan(limit=payload.limit)

    @router.post("/audit/replay/runtime/run")
    async def audit_replay_runtime_run(request: ReplayRuntimeRequest | None = None) -> dict[str, Any]:
        payload = request or ReplayRuntimeRequest()
        scan = deps["replay_runtime"].scan(limit=payload.limit)
        run = deps["replay_runtime"].run_pending(
            auto_create_reviews=payload.auto_create_reviews,
            auto_execute=payload.auto_execute,
            allow_r4_restore=payload.allow_r4_restore,
            limit=min(payload.limit, 50),
        )
        return {"status": "success", "scan": scan, "run": run}

    @router.post("/audit/replay/runtime/config")
    async def audit_replay_runtime_config(request: ReplayRuntimeConfigRequest) -> dict[str, Any]:
        return deps["replay_runtime"].configure(
            auto_execute_enabled=request.auto_execute_enabled,
            allow_r4_restore=request.allow_r4_restore,
        )

    @router.post("/audit/replay/runtime/execute")
    async def audit_replay_runtime_execute(request: ReplayRuntimeRequest | None = None) -> dict[str, Any]:
        payload = request or ReplayRuntimeRequest(auto_execute=True, allow_r4_restore=True)
        scan = deps["replay_runtime"].scan(limit=payload.limit)
        run = deps["replay_runtime"].run_pending(
            auto_create_reviews=True,
            auto_execute=True,
            allow_r4_restore=True,
            limit=min(payload.limit, 50),
        )
        return {
            "status": "needs_review",
            "reason": (
                "Replay execution endpoint is deny-only; compensation remains "
                "behind an explicit canonical review."
            ),
            "execution_authority_enabled": False,
            "scan": scan,
            "run": run,
        }

    @router.post("/proactive/check")
    async def proactive_check() -> dict[str, Any]:
        return deps["proactive_checks"].run_read_only()

    @router.get("/proactive/self-heal/status")
    async def proactive_self_heal_status() -> dict[str, Any]:
        return deps["proactive_checks"].self_heal.status()

    @router.post("/state/refresh-stale")
    async def refresh_stale_state() -> dict[str, Any]:
        return _public_refresh_result(deps["state_refresh"].refresh_stale())

    @router.post("/external/refresh")
    async def refresh_external_world(
        limit: int = 10,
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
    ) -> dict[str, Any]:
        scope = _belief_scope(user_id, session_id)
        result = deps["external_world_refresh"].refresh_watchlist(
            limit=max(0, min(int(limit), 100)),
            user_id=scope[0] if scope else None,
            session_id=scope[1] if scope else None,
        )
        reason = _public_external_failure_reason(result.get("reason"))
        if scope is None:
            response = {
                "schema_version": "veyra.external_refresh.aggregate.v1",
                "status": _public_status(result.get("status")),
                "refreshed_count": len(result.get("refreshed") or []),
                "skipped_count": len(result.get("skipped") or []),
                "summary_count": max(0, int(result.get("summary_count") or 0)),
                "scope_status": "aggregate_only",
            }
            if reason is not None:
                response["reason"] = reason
            return response
        response = {
            "schema_version": "veyra.external_refresh.scoped.v1",
            "status": _public_status(result.get("status")),
            "refreshed": [
                _public_external_item(item, summary=True)
                for item in (result.get("refreshed") or [])
                if isinstance(item, dict)
            ],
            "skipped": [
                _public_external_item(item)
                for item in (result.get("skipped") or [])
                if isinstance(item, dict)
            ],
            "summary_count": max(0, int(result.get("summary_count") or 0)),
            "scope": {"user_id": scope[0], "session_id": scope[1]},
        }
        if reason is not None:
            response["reason"] = reason
        return response

    @router.get("/external/watchlist")
    async def external_watchlist(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = 50,
    ) -> dict[str, Any]:
        scope = _belief_scope(user_id, session_id)
        assert scope is not None
        selected_limit = max(0, min(int(limit), 100))
        external = deps["state_store"].read_json("external_world.json")
        if external.get("_state_corrupt"):
            return {
                "schema_version": "veyra.external_watchlist.scoped.v1",
                "status": "degraded",
                "reason": "external_world_integrity_invalid",
                "watchlist": [],
                "summaries": [],
                "watchlist_count": 0,
                "summary_count": 0,
                "scope": {"user_id": scope[0], "session_id": scope[1]},
            }
        watchlist = [
            _public_external_item(item)
            for item in (external.get("watchlist") or [])
            if isinstance(item, dict)
            and item_visible_to_scope(
                item,
                user_id=scope[0],
                session_id=scope[1],
            )
        ]
        summaries = [
            _public_external_item(item, summary=True)
            for item in (external.get("summaries") or [])
            if isinstance(item, dict)
            and item_visible_to_scope(
                item,
                user_id=scope[0],
                session_id=scope[1],
            )
        ]
        return {
            "schema_version": "veyra.external_watchlist.scoped.v1",
            "status": "success",
            "watchlist": watchlist[-selected_limit:] if selected_limit else [],
            "summaries": summaries[-selected_limit:] if selected_limit else [],
            "watchlist_count": len(watchlist),
            "summary_count": len(summaries),
            "scope": {"user_id": scope[0], "session_id": scope[1]},
        }

    @router.post("/external/watchlist")
    async def add_external_watch(request: ExternalWatchRequest) -> dict[str, Any]:
        state_store = deps["state_store"]
        try:
            user_scope = normalize_scope_component(
                request.user_id,
                "user_id",
            )
            session_scope = normalize_scope_component(
                request.session_id,
                "session_id",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        item = {
            **request.model_dump(),
            "user_id": user_scope,
            "session_id": session_scope,
        }

        def update_watchlist(external: dict[str, Any]) -> None:
            watchlist = external.setdefault("watchlist", [])
            if not isinstance(watchlist, list):
                external["watchlist"] = watchlist = []
            existing = [
                entry
                for entry in watchlist
                if (
                    isinstance(entry, dict)
                    and entry.get("target") == request.target
                    and entry.get("user_id") == user_scope
                    and entry.get("session_id") == session_scope
                )
            ]
            if existing:
                existing[0].update(item)
            else:
                watchlist.append(item)

        state_store.mutate_json("external_world.json", update_watchlist)
        return {
            "schema_version": "veyra.external_watchlist_write.v1",
            "status": "success",
            "item": _public_external_item(item),
            "scope": {"user_id": user_scope, "session_id": session_scope},
        }

    @router.get("/agency/intentions")
    async def agency_intentions() -> dict[str, Any]:
        return deps["agency_core"].state()

    @router.get("/personas/status")
    async def personas_status() -> dict[str, Any]:
        return {"status": "success", **deps["state_store"].read_json("persona_state.json")}

    @router.get("/belief/status")
    async def belief_status(
        limit: int = 50,
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
    ) -> dict[str, Any]:
        belief = deps["awareness_loop"].belief
        selected_limit = max(0, min(int(limit), 500))
        scope = _belief_scope(user_id, session_id)
        if scope is None:
            # Unauthenticated/broad reads expose only aggregate freshness; raw
            # claims, evidence refs, probes and CAS receipts remain private.
            report = belief.ttl_report(limit=selected_limit)
            return _public_belief_aggregate(report)
        snapshot = belief._snapshot()  # noqa: SLF001 - scoped integrity gate
        if snapshot.get("_state_corrupt"):
            return {
                "status": "degraded",
                "reason": "belief_state_integrity_invalid",
                "summary": belief.summary_from_claims([]),
                "refreshable": [],
                "oldest": [],
                "newest": [],
                "scope": {"user_id": scope[0], "session_id": scope[1]},
            }
        claims = _scoped_belief_claims(belief, *scope)
        ranked = belief._rank_claims(claims)  # noqa: SLF001 - scoped projection
        refreshable = [
            claim
            for claim in ranked
            if claim.get("next_action") == "refresh_probe"
        ]
        return {
            "schema_version": "veyra.belief.scoped_status.v1",
            "status": "success",
            "summary": belief.summary_from_claims(claims),
            "refreshable": refreshable[-selected_limit:] if selected_limit else [],
            "oldest": ranked[:selected_limit],
            "newest": ranked[-selected_limit:] if selected_limit else [],
            "scope": {"user_id": scope[0], "session_id": scope[1]},
        }

    @router.get("/belief/stale")
    async def belief_stale(
        limit: int = 50,
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
    ) -> dict[str, Any]:
        belief = deps["awareness_loop"].belief
        selected_limit = max(0, min(int(limit), 500))
        scope = _belief_scope(user_id, session_id)
        if scope is None:
            report = belief.stale_report(limit=selected_limit)
            return _public_belief_aggregate(report, stale=True)
        snapshot = belief._snapshot()  # noqa: SLF001 - scoped integrity gate
        if snapshot.get("_state_corrupt"):
            return {
                "status": "degraded",
                "reason": "belief_state_integrity_invalid",
                "summary": belief.summary_from_claims([]),
                "items": [],
                "next_action": None,
                "scope": {"user_id": scope[0], "session_id": scope[1]},
            }
        claims = _scoped_belief_claims(belief, *scope)
        stale = [
            claim
            for claim in belief._rank_claims(claims)  # noqa: SLF001
            if claim.get("next_action") == "refresh_probe"
        ]
        return {
            "schema_version": "veyra.belief.scoped_stale.v1",
            "status": "success",
            "summary": belief.summary_from_claims(claims),
            "items": stale[-selected_limit:] if selected_limit else [],
            "next_action": "refresh_probe" if stale else None,
            "scope": {"user_id": scope[0], "session_id": scope[1]},
        }

    @router.get("/attention/active")
    async def attention_active(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            selected_user = normalize_scope_component(user_id, "user_id")
            selected_session = normalize_scope_component(
                session_id,
                "session_id",
            )
            return deps["awareness_loop"].attention.active_scope(
                user_id=selected_user,
                session_id=selected_session,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/project-guardian/status")
    async def project_guardian_status() -> dict[str, Any]:
        return deps["project_guardian"].status()

    @router.post("/awareness/project-guardian/config")
    async def project_guardian_config(
        request: ProjectGuardianConfigRequest,
    ) -> dict[str, Any]:
        try:
            return deps["project_guardian"].configure(request.mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/project-guardian/run-once")
    async def project_guardian_run_once() -> dict[str, Any]:
        return await run_in_threadpool(
            deps["project_guardian"].run_once,
            reason="debug_api",
        )

    @router.get("/awareness/project-guardian/producers/status")
    async def project_guardian_producers_status() -> dict[str, Any]:
        return deps["project_guardian_producers"].status()

    @router.post("/awareness/project-guardian/producers/run-once")
    async def project_guardian_producers_run_once() -> dict[str, Any]:
        return await run_in_threadpool(
            deps["project_guardian_producers"].public_run_once,
            reason="debug_api",
        )

    @router.post("/awareness/project-guardian/release-goals")
    async def project_guardian_register_release_goal(
        request: ProjectReleaseGoalRequest,
    ) -> dict[str, Any]:
        try:
            item = await run_in_threadpool(
                deps["project_guardian_producers"].register_release_goal,
                **request.model_dump(),
            )
        except ProjectGuardianGoalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "registered", "item": item}

    @router.post(
        "/awareness/project-guardian/release-goals/{goal_id}/status"
    )
    async def project_guardian_release_goal_status(
        goal_id: str,
        request: ProjectReleaseGoalStatusRequest,
    ) -> dict[str, Any]:
        try:
            item = await run_in_threadpool(
                deps["project_guardian_producers"].set_release_goal_status,
                goal_id=goal_id,
                **request.model_dump(),
            )
        except ProjectGuardianGoalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "updated", "item": item}

    @router.post(
        "/awareness/project-guardian/release-goals/"
        "{goal_id}/deployment-intent"
    )
    async def project_guardian_deployment_intent(
        goal_id: str,
        request: ProjectDeploymentIntentRequest,
    ) -> dict[str, Any]:
        command = request.model_dump(exclude={"schema_version"})
        try:
            return await run_in_threadpool(
                deps["project_guardian_producers"].record_deployment_intent,
                goal_id=goal_id,
                **command,
            )
        except ProjectGuardianGoalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/project-guardian/attention/status")
    async def project_guardian_attention_status() -> dict[str, Any]:
        return deps["project_guardian_attention"].status()

    @router.post("/awareness/project-guardian/attention/run-once")
    async def project_guardian_attention_run_once() -> dict[str, Any]:
        return await run_in_threadpool(
            deps["project_guardian_attention"].run_once,
            reason="debug_api",
        )

    @router.post(
        "/awareness/project-guardian/release-goals/"
        "{goal_id}/attention-policy"
    )
    async def project_guardian_attention_policy(
        goal_id: str,
        request: ProjectGuardianAttentionPolicyRequest,
    ) -> dict[str, Any]:
        command = request.model_dump(exclude={"schema_version"})
        command["timezone_name"] = command.pop("timezone")
        try:
            item = await run_in_threadpool(
                deps["project_guardian_attention"].set_policy,
                goal_id=goal_id,
                **command,
            )
        except ProjectGuardianAttentionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "updated", "item": item}

    @router.post(
        "/awareness/project-guardian/attention/dismissals/"
        "{suppression_key}"
    )
    async def project_guardian_attention_dismissal(
        suppression_key: str,
        request: ProjectGuardianAttentionDismissalRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                deps["project_guardian_attention"].set_dismissal,
                suppression_key=suppression_key,
                user_id=request.user_id,
                dismissed=request.dismissed,
            )
        except ProjectGuardianAttentionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/project-guardian/attention/assessments")
    async def project_guardian_attention_assessments(
        user_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            items = deps["project_guardian_attention"].list_assessments(
                user_id=user_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "success", "count": len(items), "items": items}

    @router.get(
        "/awareness/project-guardian/attention/general-situations"
    )
    async def project_guardian_attention_general_situations(
        user_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            items = deps[
                "project_guardian_attention"
            ].list_general_situations(
                user_id=user_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "success", "count": len(items), "items": items}

    @router.get("/awareness/project-guardian/release-goals")
    async def project_guardian_release_goals(
        user_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            items = deps[
                "project_guardian_producers"
            ].list_release_goals(user_id=user_id, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "status": "success",
            "count": len(items),
            "items": items,
        }

    @router.get("/awareness/project-guardian/candidates")
    async def project_guardian_candidates(
        user_id: str,
        session_id: str | None = None,
        disposition: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = deps["project_guardian"].list_candidates(
            user_id=user_id,
            session_id=session_id,
            disposition=disposition,
            limit=limit,
        )
        return {
            "status": "success",
            "projection_kind": "project_guardian_candidate",
            "count": len(items),
            "items": items,
        }

    @router.get("/awareness/situations")
    async def awareness_situations(
        user_id: str,
        session_id: str | None = None,
        status: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = deps["awareness_loop"].situation_evaluator.list(
            user_id=user_id,
            session_id=session_id,
            status=status,
            correlation_id=correlation_id,
            limit=max(0, min(limit, 500)),
        )
        return {
            "status": "success",
            "projection_kind": "situation_candidate",
            "count": len(items),
            "items": items,
        }

    @router.get("/awareness/situations/{situation_id}")
    async def awareness_situation(
        situation_id: str,
        user_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        item = deps["awareness_loop"].situation_evaluator.get(
            situation_id,
            user_id=user_id,
            session_id=session_id,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="situation not found in the requested scope")
        return {"status": "success", "item": item}

    @router.get("/events/inbox")
    async def event_inbox(
        user_id: str,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        document = deps["state_store"].read_json("event_inbox.json")
        records = document.get("events") if isinstance(document.get("events"), dict) else {}
        items: list[dict[str, Any]] = []
        for event_id, record in records.items():
            if not isinstance(record, dict):
                continue
            record_status = str(record.get("status") or "unknown")
            if status is not None and record_status != status:
                continue
            envelope = record.get("envelope") if isinstance(record.get("envelope"), dict) else {}
            source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
            if str(source.get("user_id") or "") != user_id:
                continue
            if session_id is not None and str(source.get("session_id") or "") != session_id:
                continue
            items.append(
                {
                    "event_id": event_id,
                    "event_type": envelope.get("type"),
                    "correlation_id": envelope.get("correlation_id"),
                    "channel": source.get("channel"),
                    "user_id": source.get("user_id"),
                    "session_id": source.get("session_id"),
                    "privacy_scope": envelope.get("privacy_scope"),
                    "status": record_status,
                    "attempts": record.get("attempts"),
                    "max_attempts": record.get("max_attempts"),
                    "enqueued_at": record.get("enqueued_at"),
                    "completed_at": record.get("completed_at"),
                    "last_error": record.get("last_error"),
                }
            )
        items.sort(key=lambda item: str(item.get("enqueued_at") or ""), reverse=True)
        scoped_stats: dict[str, int] = {}
        for item in items:
            item_status = str(item.get("status") or "unknown")
            scoped_stats[item_status] = scoped_stats.get(item_status, 0) + 1
        scoped_stats["total"] = len(items)
        selected_limit = max(0, min(limit, 500))
        return {
            "status": "success",
            "mode": deps["awareness_loop"].event_awareness.mode,
            "stats": scoped_stats,
            "count": min(len(items), selected_limit),
            "items": items[:selected_limit],
        }

    @router.get("/events/awareness/status")
    async def event_awareness_status() -> dict[str, Any]:
        runtime = deps["awareness_loop"].event_awareness
        return {
            "status": "success",
            "mode": runtime.mode,
            "mode_epoch": runtime.mode_epoch,
            "allowed_modes": sorted(runtime.MODES),
            "context_binding": runtime.context_bindings.status(),
        }

    @router.post("/events/awareness/config")
    async def event_awareness_config(
        request: EventAwarenessConfigRequest,
    ) -> dict[str, Any]:
        runtime = deps["awareness_loop"].event_awareness
        try:
            return runtime.configure(request.mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=str(exc),
            ) from exc

    @router.get("/awareness/general-situations")
    async def general_situations(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=100, ge=0, le=500),
    ) -> dict[str, Any]:
        runtime = deps["awareness_loop"].event_awareness.general_situations
        try:
            return runtime.list_for_owner(
                user_id=user_id,
                session_id=session_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/attention-hypotheses/status")
    async def attention_hypothesis_status() -> dict[str, Any]:
        return deps[
            "awareness_loop"
        ].event_awareness.attention_hypotheses.status()

    @router.get("/awareness/attention-hypotheses")
    async def attention_hypotheses(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=100, ge=0, le=500),
    ) -> dict[str, Any]:
        runtime = deps[
            "awareness_loop"
        ].event_awareness.attention_hypotheses
        try:
            return runtime.list_for_owner(
                user_id=user_id,
                session_id=session_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/context-bindings/status")
    async def context_binding_status() -> dict[str, Any]:
        return deps["awareness_loop"].event_awareness.context_bindings.status()

    @router.get("/awareness/cognitive-loop/status")
    async def cognitive_loop_status() -> dict[str, Any]:
        return deps["read_only_cognitive_loop"].status()

    @router.get("/awareness/suggestions/status")
    async def general_suggestion_status() -> dict[str, Any]:
        return deps[
            "awareness_loop"
        ].event_awareness.suggestion_outbox.status()

    @router.get("/awareness/suggestions/inbox")
    async def general_suggestion_inbox(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=100, ge=0, le=100),
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.list_inbox(
                user_id=user_id,
                session_id=session_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/awareness/suggestions/decisions")
    async def general_suggestion_decisions(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=100, ge=0, le=500),
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.list_decisions(
                user_id=user_id,
                session_id=session_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/suggestions/config")
    async def general_suggestion_config(
        request: GeneralSuggestionModeRequest,
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.configure_mode(
                request.mode,
                expected_state_revision=request.expected_state_revision,
            )
        except SuggestionOutboxConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/suggestions/policy")
    async def general_suggestion_policy(
        request: GeneralSuggestionPolicyRequest,
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.configure_policy(**request.model_dump())
        except SuggestionOutboxConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/suggestions/{proposal_id}/ack")
    async def general_suggestion_ack(
        proposal_id: str,
        request: GeneralSuggestionFeedbackRequest,
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.acknowledge(
                proposal_id,
                **request.model_dump(),
            )
        except SuggestionOutboxConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/suggestions/{proposal_id}/dismiss")
    async def general_suggestion_dismiss(
        proposal_id: str,
        request: GeneralSuggestionFeedbackRequest,
    ) -> dict[str, Any]:
        outbox = deps["awareness_loop"].event_awareness.suggestion_outbox
        try:
            return outbox.dismiss(
                proposal_id,
                **request.model_dump(),
            )
        except SuggestionOutboxConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/awareness/suggestions/{proposal_id}/feedback")
    async def general_suggestion_categorical_feedback(
        proposal_id: str,
        request: GeneralSuggestionCategoricalFeedbackRequest,
    ) -> dict[str, Any]:
        learning = deps["learning_calibration"]
        try:
            result = await run_in_threadpool(
                learning.record_suggestion_feedback,
                proposal_id=proposal_id,
                **request.model_dump(exclude={"schema_version"}),
            )
        except LearningCalibrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LearningCalibrationStorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="suggestion_calibration_unavailable",
            ) from exc
        except (LearningRecordValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_suggestion_feedback_contract",
            ) from exc
        return _public_suggestion_feedback_result(result)

    @router.get("/awareness/suggestions/feedback")
    async def general_suggestion_feedback_list(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        active_only: bool = Query(default=False),
        limit: int = Query(default=100, ge=0, le=500),
    ) -> dict[str, Any]:
        learning = deps["learning_calibration"]
        try:
            rows = await run_in_threadpool(
                learning.list_suggestion_feedback,
                user_id=user_id,
                session_id=session_id,
                active_only=active_only,
                limit=limit,
            )
        except LearningCalibrationStorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="suggestion_calibration_unavailable",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "status": "success",
            "count": len(rows),
            "items": [
                _public_suggestion_feedback_record(item) for item in rows
            ],
            "authority": learning._suggestion_authority_contract(),
        }

    @router.get("/awareness/suggestions/calibration")
    async def general_suggestion_calibration(
        user_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        learning = deps["learning_calibration"]
        try:
            return await run_in_threadpool(
                learning.suggestion_summary,
                user_id=user_id,
                session_id=session_id,
            )
        except LearningCalibrationStorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="suggestion_calibration_unavailable",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/belief/refresh")
    async def belief_refresh(request: BeliefRefreshRequest | None = None) -> dict[str, Any]:
        payload = request or BeliefRefreshRequest()
        result = deps["awareness_loop"].belief.refresh(
            expire_after_seconds=payload.expire_after_seconds,
            prune_expired_after_seconds=payload.prune_expired_after_seconds,
        )
        aggregate = _public_belief_aggregate(result)
        aggregate["claims"] = [
            {"status": _public_status(item.get("status"))}
            for item in (result.get("claims") or [])
            if isinstance(item, dict)
        ]
        return aggregate

    @router.get("/rollback/diff")
    async def rollback_diff(full: bool = False) -> Any:
        return deps["diff_tracker"].git_diff_text() if full else deps["diff_tracker"].git_diff()

    return router


def _public_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the unauthenticated aggregate state contract.

    ``read_all`` is intentionally broad because internal callers need the
    complete durable state graph.  That document is not a public projection:
    it contains owner/session items, target identifiers, commitments,
    evidence, receipts, and runtime bindings.  Build the response from an
    explicit allow-list instead of redacting a copy; a newly added private
    state field must not become public by default.
    """

    source = payload if isinstance(payload, dict) else {}

    def record(name: str) -> dict[str, Any]:
        value = source.get(name)
        return value if isinstance(value, dict) else {}

    def status(value: dict[str, Any]) -> str:
        return _public_status(value.get("status"))

    def bounded_timestamp(selected: Any) -> str | None:
        if not isinstance(selected, str) or len(selected) > 80:
            return None
        try:
            datetime.fromisoformat(selected.replace("Z", "+00:00"))
        except ValueError:
            return None
        return selected

    def timestamp(value: dict[str, Any]) -> str | None:
        return bounded_timestamp(value.get("updated_at"))

    def bounded_schema(selected: Any) -> str | int | None:
        if isinstance(selected, int) and not isinstance(selected, bool):
            return selected
        if (
            isinstance(selected, str)
            and len(selected) <= 120
            and selected.startswith("veyra.")
            and all(char.isalnum() or char in "._-" for char in selected)
        ):
            return selected
        return None

    def count(value: Any, fallback: Any = 0) -> int:
        if isinstance(value, (list, tuple, dict)):
            return len(value)
        try:
            return max(0, int(fallback or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def aggregate(
        name: str,
        *,
        arrays: tuple[str, ...] = (),
        counts: tuple[tuple[str, str], ...] = (),
        schema: bool = False,
    ) -> dict[str, Any]:
        value = record(name)
        output: dict[str, Any] = {
            "scope_status": "aggregate_only",
            "status": status(value),
        }
        if schema:
            schema_value = bounded_schema(value.get("schema_version"))
            if schema_value is not None:
                output["schema_version"] = schema_value
        updated = timestamp(value)
        if updated is not None:
            output["updated_at"] = updated
        for field in arrays:
            output[field] = []
        for field, count_key in counts:
            output[count_key] = count(value.get(field), value.get(count_key))
        return output

    raw_belief = record("belief_state")
    raw_summary = raw_belief.get("summary") if isinstance(raw_belief.get("summary"), dict) else {}
    belief_summary: dict[str, Any] = {}
    for key in ("fresh", "stale", "expired", "conflict", "total", "refreshable"):
        try:
            belief_summary[key] = max(0, int(raw_summary.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            belief_summary[key] = 0

    public: dict[str, Any] = {
        "schema_version": "veyra.public_state.v2",
        "user_world": aggregate("user_world"),
        "user_goals": aggregate(
            "user_goals", arrays=("goals",), counts=(("goals", "goal_count"),)
        ),
        "user_commitments": aggregate(
            "user_commitments",
            arrays=("commitments",),
            counts=(("commitments", "commitment_count"),),
        ),
        "proactive_intents": aggregate(
            "proactive_intents", arrays=("intents",), counts=(("intents", "intent_count"),)
        ),
        "proactive_authorizations": aggregate(
            "proactive_authorizations",
            arrays=("authorizations",),
            counts=(("authorizations", "authorization_count"),),
        ),
        "belief_state": {
            "scope_status": "owner_scope_required",
            "status": status(raw_belief),
            "summary": belief_summary,
        },
    }
    if timestamp(raw_belief) is not None:
        public["belief_state"]["updated_at"] = timestamp(raw_belief)

    raw_local = record("local_world")
    raw_probes = raw_local.get("probes") if isinstance(raw_local.get("probes"), dict) else {}
    raw_scoped_probes = raw_local.get("scoped_probes") if isinstance(raw_local.get("scoped_probes"), dict) else {}
    public["local_world"] = {
        "scope_status": "owner_scope_required",
        "status": status(raw_local),
        "last_probe_at": bounded_timestamp(raw_local.get("last_probe_at")),
        "probe_count": len(raw_probes),
        "scoped_probe_owner_count": len(raw_scoped_probes),
    }
    public["external_world"] = aggregate(
        "external_world",
        arrays=("watchlist", "summaries", "knowledge_items", "push_candidates"),
        counts=(
            ("watchlist", "watchlist_count"),
            ("summaries", "summary_count"),
            ("knowledge_items", "knowledge_item_count"),
            ("push_candidates", "push_candidate_count"),
        ),
    )
    public["executor_state"] = aggregate("executor_state")
    risk = record("risk_state")
    current_risk = str(risk.get("current_risk") or "R0")
    public["risk_state"] = {
        "scope_status": "aggregate_only",
        "status": status(risk),
        "current_risk": current_risk if current_risk in {"R0", "R1", "R2", "R3", "R4", "R5"} else "unknown",
        "signal_count": count(risk.get("signals")),
        "assessment_count": count(risk.get("assessments")),
    }
    public["risk_policy"] = aggregate("risk_policy", schema=True)

    task = record("task_state")
    current_task = task.get("current_task") if isinstance(task.get("current_task"), dict) else None
    public["task_state"] = {
        "scope_status": "owner_scope_required",
        "status": status(task),
        "pending_agent_task_count": count(task.get("pending_agent_tasks")),
        "history_count": count(task.get("history")),
        "current_task": (
            {"status": _public_status(current_task.get("status"))}
            if current_task
            else None
        ),
    }

    attention_state = record("attention_state")
    attention_schema = bounded_schema(attention_state.get("schema_version"))
    if not isinstance(attention_schema, str):
        attention_schema = "veyra.attention_state.v2"
    public["attention_state"] = {
        "schema_version": attention_schema,
        "source": "attention_core",
        "scope_status": "owner_scope_required",
        "focus": [],
        "ignored_noise": ["owner_scope_required"],
    }
    public["channel_state"] = aggregate(
        "channel_state",
        arrays=("inbox", "outbox"),
        counts=(("inbox", "inbox_count"), ("outbox", "outbox_count")),
        schema=True,
    )
    public["persona_state"] = aggregate(
        "persona_state", arrays=("active_modes",), counts=(("active_modes", "active_mode_count"),)
    )
    public["review_queue"] = aggregate(
        "review_queue", arrays=("items",), counts=(("items", "item_count"),)
    )
    public["rollback_state"] = aggregate(
        "rollback_state", arrays=("snapshots",), counts=(("snapshots", "snapshot_count"),)
    )
    public["replay_runtime_state"] = aggregate(
        "replay_runtime_state", arrays=("jobs",), counts=(("jobs", "job_count"),)
    )
    public["ops_soak_state"] = aggregate(
        "ops_soak_state", arrays=("runs",), counts=(("runs", "run_count"),)
    )
    public["ops_runtime_matrix"] = aggregate(
        "ops_runtime_matrix", arrays=("runtimes",), counts=(("runtimes", "runtime_count"),)
    )
    public["active_loop_state"] = aggregate("active_loop_state")
    public["runtime_cron_state"] = aggregate(
        "runtime_cron_state", arrays=("jobs",), counts=(("jobs", "job_count"),)
    )
    public["self_improvement_proposals"] = aggregate(
        "self_improvement_proposals", arrays=("proposals",), counts=(("proposals", "proposal_count"),)
    )
    public["state_change_proposals"] = aggregate(
        "state_change_proposals", arrays=("proposals",), counts=(("proposals", "proposal_count"),), schema=True
    )
    public["feishu_ws_state"] = aggregate("feishu_ws_state")
    public["state_schema"] = aggregate("state_schema", schema=True)
    public["ops_config"] = aggregate("ops_config")

    # Keep the existing core-model status shape without returning raw config,
    # agent registries, local URLs, or credential material.
    agent_config = record("agent_config")
    core_model = agent_config.get("core_model") if isinstance(agent_config.get("core_model"), dict) else {}
    decision_mode = str(core_model.get("decision_mode") or "auto")
    public["agent_config"] = {
        "scope_status": "aggregate_only",
        "status": status(agent_config),
        "core_model": {
            "enabled": bool(core_model.get("enabled")),
            "configured": bool(core_model.get("configured")),
            "decision_mode": decision_mode if decision_mode in {"auto", "always"} else "auto",
            "api_key_set": bool(core_model.get("api_key")),
            "api_key": "",
        },
        "agent_count": count(agent_config.get("agents")),
    }

    raw_refresh = record("state_refresh_state")
    refresh_summary: dict[str, Any] = {"scope_status": "owner_scope_required"}
    try:
        refresh_summary["cursor"] = max(0, int(raw_refresh.get("cursor") or 0))
    except (TypeError, ValueError, OverflowError):
        refresh_summary["cursor"] = 0
    refresh_updated_at = bounded_timestamp(raw_refresh.get("updated_at"))
    if refresh_updated_at is not None:
        refresh_summary["updated_at"] = refresh_updated_at
    for key in (
        "supported_count", "unsupported_count", "malformed_count", "selected_count",
        "refreshed_count", "failed_count", "skipped_count",
    ):
        try:
            refresh_summary[key] = max(0, int(raw_refresh.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            refresh_summary[key] = 0
    public["state_refresh_state"] = refresh_summary
    return public


def _public_agency_projection(value: Any) -> dict[str, Any]:
    """Expose only aggregate Agency health on the unauthenticated state route."""

    agency = value if isinstance(value, dict) else {}
    goals = agency.get("goals") if isinstance(agency.get("goals"), dict) else {}
    commitments = agency.get("active_commitments")
    triggers = agency.get("triggers")
    intentions = agency.get("intentions")
    return {
        "scope_status": "owner_scope_required",
        "config_status": _public_status(agency.get("config_status")),
        "goal_count": len(goals),
        "active_commitment_count": len(commitments) if isinstance(commitments, list) else 0,
        "trigger_count": len(triggers) if isinstance(triggers, list) else 0,
        "intention_count": len(intentions) if isinstance(intentions, list) else 0,
    }


def _public_refresh_result(value: Any) -> dict[str, Any]:
    """Return aggregate refresh outcomes without claim/probe/receipt payloads."""

    result = value if isinstance(value, dict) else {}

    def _count(raw: Any) -> int:
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    output: dict[str, Any] = {
        "status": _public_status(result.get("status"), default="degraded"),
        "refreshed": [
            {"status": "accepted"}
            for item in (result.get("refreshed") or [])
            if isinstance(item, dict)
        ],
        "failed": [
            {"status": "failed"}
            for item in (result.get("failed") or [])
            if isinstance(item, dict)
        ],
        "skipped": [
            {"status": "skipped"}
            for item in (result.get("skipped") or [])
            if isinstance(item, dict)
        ],
        "refreshed_count": _count(result.get("refreshed_count")),
        "selected_count": _count(result.get("selected_count")),
        "remaining_stale": _count(result.get("remaining_stale")),
        "unsupported_stale": _count(result.get("unsupported_stale")),
        "malformed_count": len(result.get("malformed") or [])
        if isinstance(result.get("malformed"), list)
        else _count(result.get("malformed_count")),
    }
    cursor = result.get("cursor")
    if isinstance(cursor, dict):
        output["cursor"] = {
            "before": _count(cursor.get("before")),
            "after": _count(cursor.get("after")),
        }
    return output


def _public_belief_aggregate(value: Any, *, stale: bool = False) -> dict[str, Any]:
    """Sanitize broad Belief status/stale reports to counts/status-only rows."""

    report = value if isinstance(value, dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    aggregate_summary: dict[str, int] = {}
    for key in ("fresh", "stale", "expired", "conflict", "total", "refreshable"):
        try:
            aggregate_summary[key] = max(0, int(summary.get(key) or 0))
        except (TypeError, ValueError):
            aggregate_summary[key] = 0
    output: dict[str, Any] = {
        "schema_version": "veyra.belief.aggregate_status.v1",
        "status": _public_status(report.get("status"), default="success"),
        "summary": aggregate_summary,
        "scope_status": "aggregate_only",
    }
    if report.get("reason") == "belief_claims_quarantined":
        output["reason"] = "belief_claims_quarantined"
        try:
            output["quarantined_claim_count"] = max(
                0,
                int(report.get("quarantined_claim_count") or 0),
            )
        except (TypeError, ValueError, OverflowError):
            output["quarantined_claim_count"] = 0
    if stale:
        output["items"] = [
            {"status": _public_status(item.get("status"))}
            for item in (report.get("items") or [])
            if isinstance(item, dict)
        ]
        output["next_action"] = (
            "refresh_probe" if report.get("next_action") else None
        )
    else:
        output["refreshable_count"] = len(report.get("refreshable") or [])
        output["oldest_count"] = len(report.get("oldest") or [])
        output["newest_count"] = len(report.get("newest") or [])
    return output


def _belief_scope(
    user_id: str | None,
    session_id: str | None,
) -> tuple[str, str] | None:
    if user_id is None and session_id is None:
        return None
    if user_id is None or session_id is None:
        raise HTTPException(
            status_code=422,
            detail="user_id and session_id must be supplied together",
        )
    try:
        return (
            normalize_scope_component(user_id, "user_id"),
            normalize_scope_component(session_id, "session_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _public_external_item(
    value: Any,
    *,
    summary: bool = False,
) -> dict[str, Any]:
    """Project one exact-owner ExternalWorld row without probe internals."""

    item = value if isinstance(value, dict) else {}

    def text(field: str, limit: int) -> str | None:
        selected = item.get(field)
        if not isinstance(selected, str):
            return None
        selected = selected.strip()
        return selected[:limit] if selected else None

    output: dict[str, Any] = {
        "target": text("target", 500),
        "kind": text("kind", 80),
        "status": _public_status(item.get("status")),
    }
    if not summary:
        output.update(
            {
                "reason": text("reason", 240),
                "enabled": item.get("enabled") is not False,
            }
        )
    else:
        output.update(
            {
                "summary": text("summary", 1200),
                "observed_at": text("observed_at", 80),
                "watch_recommendation": text("watch_recommendation", 80),
            }
        )
    return output


def _public_external_failure_reason(value: Any) -> str | None:
    selected = str(value or "").strip().lower()
    if selected in {
        "external_refresh_scope_incomplete",
        "external_world_integrity_invalid",
    }:
        return selected
    return None


def _public_rollback_log_item(value: Any) -> dict[str, Any]:
    """Project rollback audit rows without filesystem or integrity internals."""

    item = value if isinstance(value, dict) else {}
    action = str(item.get("action") or "unknown").strip().lower()
    if action not in {"snapshot", "restore"}:
        action = "unknown"
    output: dict[str, Any] = {"action": action}
    timestamp = item.get("timestamp")
    if isinstance(timestamp, str) and len(timestamp) <= 80:
        output["timestamp"] = timestamp

    record_key = "snapshot" if action == "snapshot" else "result"
    record = item.get(record_key)
    if not isinstance(record, dict):
        return output
    snapshot_id = str(record.get("snapshot_id") or "").strip()
    status = str(record.get("status") or "unknown").strip().lower()
    if status not in {"blocked", "created", "restored", "tombstone"}:
        status = "unknown"
    projected: dict[str, Any] = {
        "snapshot_id": snapshot_id[:80] if snapshot_id.startswith("snap_") else "",
        "status": status,
        "source_scope": "workspace_scoped",
    }
    created_at = record.get("created_at") or record.get("restored_at")
    if isinstance(created_at, str) and len(created_at) <= 80:
        projected["observed_at"] = created_at
    rollback_mode = str(record.get("rollback_mode") or "").strip().lower()
    if rollback_mode in {"delete_created_file", "none", "restore"}:
        projected["rollback_mode"] = rollback_mode
    output[record_key] = projected
    return output


def _scoped_belief_claims(belief: Any, user_id: str, session_id: str) -> list[dict[str, Any]]:
    snapshot = belief._snapshot()  # noqa: SLF001 - route-owned read projection
    claims = snapshot.get("claims") if isinstance(snapshot.get("claims"), list) else []
    return [
        item
        for item in claims
        if item_visible_to_scope(item, user_id=user_id, session_id=session_id)
    ]


def _public_suggestion_feedback_record(value: Any) -> dict[str, Any]:
    record = value if isinstance(value, dict) else {}
    return {
        key: record.get(key)
        for key in (
            "schema_version",
            "learning_id",
            "feedback_id",
            "proposal_id",
            "proposal_revision",
            "general_situation_id",
            "parent_revision",
            "label",
            "status",
            "supersedes_learning_id",
            "superseded_by_learning_id",
            "promotion_status",
            "policy_effect",
            "source",
            "created_at",
            "updated_at",
        )
    }


def _public_suggestion_feedback_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=503,
            detail="suggestion_calibration_invalid_projection",
        )
    return {
        "status": str(value.get("status") or "unknown"),
        "record": _public_suggestion_feedback_record(value.get("record")),
        "authority": (
            value.get("authority")
            if isinstance(value.get("authority"), dict)
            else {}
        ),
    }


def _memory_log_visible(
    row: Any,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    if not isinstance(row, dict):
        return False
    item = row.get("item") if isinstance(row.get("item"), dict) else {}
    patch = item.get("patch") if isinstance(item.get("patch"), dict) else {}
    item_user = str(item.get("user_id") or "").strip()
    item_session = str(item.get("session_id") or "").strip()
    patch_user = str(patch.get("user_id") or "").strip()
    patch_session = str(patch.get("session_id") or "").strip()
    return (
        bool(item_user)
        and bool(item_session)
        and item_user == patch_user == user_id
        and item_session == patch_session == session_id
    )
