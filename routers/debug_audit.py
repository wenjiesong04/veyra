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


class EventAwarenessConfigRequest(BaseModel):
    mode: str


def build_debug_audit_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    async def state() -> dict[str, Any]:
        payload = _public_state(deps["state_store"].read_all())
        payload["agency"] = deps["agency_core"].state()
        if "commitment_core" in deps:
            payload["commitments"] = deps["commitment_core"].list_commitments()
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
        snapshot.update(
            {
                "runtime": snapshot.get("runtime") or selected_agent,
                "source": "agent_adapter.fetch_capabilities",
                "updated_at": utc_now_iso(),
                "ttl_seconds": 300,
            }
        )
        deps["state_store"].patch_json(
            "executor_state.json",
            {
                "selected_agent": selected_agent,
                "capability_snapshot": snapshot,
                "status": snapshot.get("status") or "unknown",
                "connected": snapshot.get("connected"),
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
        item = request.model_dump()

        def update_watchlist(external: dict[str, Any]) -> None:
            watchlist = external.setdefault("watchlist", [])
            if not isinstance(watchlist, list):
                external["watchlist"] = watchlist = []
            existing = [
                entry
                for entry in watchlist
                if isinstance(entry, dict) and entry.get("target") == request.target
            ]
            if existing:
                existing[0].update(item)
            else:
                watchlist.append(item)

        return state_store.mutate_json("external_world.json", update_watchlist)

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
            "allowed_modes": sorted(runtime.MODES),
        }

    @router.post("/events/awareness/config")
    async def event_awareness_config(
        request: EventAwarenessConfigRequest,
    ) -> dict[str, Any]:
        mode = str(request.mode or "").strip().lower()
        runtime = deps["awareness_loop"].event_awareness
        if mode not in runtime.MODES:
            raise HTTPException(
                status_code=422,
                detail=f"mode must be one of {sorted(runtime.MODES)}",
            )

        def update(config: dict[str, Any]) -> None:
            config["event_awareness"] = {
                "mode": mode,
                "allowed_modes": sorted(runtime.MODES),
            }

        deps["state_store"].mutate_json("ops_config.json", update)
        previous = runtime.mode
        runtime.mode = mode
        deps["state_store"].append_jsonl(
            "action_record.jsonl",
            {
                "route": "event_awareness_config",
                "status": "updated",
                "artifacts": {"previous_mode": previous, "mode": mode},
            },
        )
        return {
            "status": "updated",
            "previous_mode": previous,
            "mode": mode,
            "allowed_modes": sorted(runtime.MODES),
        }

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
    # Event and situation projections are tenant-scoped records. The generic
    # state endpoint has no authenticated tenant identity, so it must not
    # expose either collection.
    public.pop("event_inbox", None)
    public.pop("situation_state", None)
    agent_config = public.get("agent_config") if isinstance(public.get("agent_config"), dict) else {}
    if isinstance(agent_config, dict):
        core_model = agent_config.get("core_model") if isinstance(agent_config.get("core_model"), dict) else {}
        if isinstance(core_model, dict):
            original = payload.get("agent_config", {}).get("core_model", {}) if isinstance(payload.get("agent_config"), dict) else {}
            core_model["api_key_set"] = bool(original.get("api_key")) if isinstance(original, dict) else False
            core_model["api_key"] = "<redacted>" if core_model.get("api_key") else ""
    return public
