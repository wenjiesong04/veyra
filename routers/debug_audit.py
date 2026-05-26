from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.definitions import RiskLevel, lifecycle_statuses, operational_modes, risk_catalog
from core.model_client import redact_sensitive
from interface.event_schema import utc_now_iso


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


def build_debug_audit_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    async def state() -> dict[str, Any]:
        payload = _public_state(deps["state_store"].read_all())
        payload["agency"] = deps["agency_core"].state()
        return payload

    @router.get("/state/health")
    async def state_health() -> dict[str, Any]:
        return deps["state_store"].state_health()

    @router.get("/core/model/status")
    async def core_model_status() -> dict[str, Any]:
        return deps["awareness_loop"].core_reasoning.status()

    @router.post("/core/model/config")
    async def configure_core_model(request: CoreModelConfigRequest) -> dict[str, Any]:
        patch = request.model_dump(exclude_none=True)
        if "decision_mode" in patch and patch["decision_mode"] not in {"auto", "always"}:
            raise HTTPException(status_code=422, detail="decision_mode must be 'auto' or 'always'")
        if "provider" in patch and patch["provider"] != "openai_compatible":
            raise HTTPException(status_code=422, detail="only openai_compatible provider is supported")
        if "max_tokens" in patch and not 128 <= int(patch["max_tokens"]) <= 2000:
            raise HTTPException(status_code=422, detail="max_tokens must be between 128 and 2000")
        state_store = deps["state_store"]
        config = state_store.read_json("agent_config.json")
        core_model = config.setdefault("core_model", {})
        core_model.update(patch)
        state_store.write_json("agent_config.json", config)
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
            review = deps["review_queue"].decide(review_id, "approved", request.reason)
            execution = deps["action_executor"].execute_review(review)
            return deps["review_queue"].update_execution(review_id, execution)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/reviews/{review_id}/reject")
    async def reject_review(review_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        try:
            return deps["review_queue"].decide(review_id, "rejected", request.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
        return {"items": deps["state_store"].read_jsonl("rollback_log.jsonl", limit=limit)}

    @router.get("/logs/memory")
    async def memory_logs(limit: int = 100) -> dict[str, Any]:
        return {"items": deps["state_store"].read_jsonl("memory_log.jsonl", limit=limit)}

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
        deps["replay_runtime"].configure(auto_execute_enabled=True, allow_r4_restore=payload.allow_r4_restore)
        scan = deps["replay_runtime"].scan(limit=payload.limit)
        run = deps["replay_runtime"].run_pending(
            auto_create_reviews=True,
            auto_execute=True,
            allow_r4_restore=payload.allow_r4_restore,
            limit=min(payload.limit, 50),
        )
        return {"status": "success", "scan": scan, "run": run}

    @router.post("/proactive/check")
    async def proactive_check() -> dict[str, Any]:
        return deps["proactive_checks"].run_read_only()

    @router.post("/state/refresh-stale")
    async def refresh_stale_state() -> dict[str, Any]:
        return deps["state_refresh"].refresh_stale()

    @router.post("/external/refresh")
    async def refresh_external_world(limit: int = 10) -> dict[str, Any]:
        return deps["external_world_refresh"].refresh_watchlist(limit=limit)

    @router.post("/external/watchlist")
    async def add_external_watch(request: ExternalWatchRequest) -> dict[str, Any]:
        state_store = deps["state_store"]
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

    @router.get("/agency/intentions")
    async def agency_intentions() -> dict[str, Any]:
        return deps["agency_core"].state()

    @router.get("/personas/status")
    async def personas_status() -> dict[str, Any]:
        return {"status": "success", **deps["state_store"].read_json("persona_state.json")}

    @router.get("/belief/status")
    async def belief_status(limit: int = 50) -> dict[str, Any]:
        return deps["awareness_loop"].belief.ttl_report(limit=limit)

    @router.get("/belief/stale")
    async def belief_stale(limit: int = 50) -> dict[str, Any]:
        return deps["awareness_loop"].belief.stale_report(limit=limit)

    @router.get("/attention/active")
    async def attention_active() -> dict[str, Any]:
        return deps["awareness_loop"].attention.active_scope()

    @router.post("/belief/refresh")
    async def belief_refresh(request: BeliefRefreshRequest | None = None) -> dict[str, Any]:
        payload = request or BeliefRefreshRequest()
        result = deps["awareness_loop"].belief.refresh(
            expire_after_seconds=payload.expire_after_seconds,
            prune_expired_after_seconds=payload.prune_expired_after_seconds,
        )
        return {"status": "success", **result}

    @router.get("/rollback/diff")
    async def rollback_diff(full: bool = False) -> Any:
        return deps["diff_tracker"].git_diff_text() if full else deps["diff_tracker"].git_diff()

    return router


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
