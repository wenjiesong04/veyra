from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class SoakRequest(BaseModel):
    iterations: int = 1


class ActiveLoopRequest(BaseModel):
    interval_seconds: float = 300.0


class ActiveLoopTickRequest(BaseModel):
    reason: str = "manual"
    include_runtime_matrix: bool = False


class CronConfigRequest(BaseModel):
    job_id: str = "active_awareness_tick"
    enabled: bool | None = None
    interval_seconds: float | None = None
    include_runtime_matrix: bool | None = None


class CronRunRequest(BaseModel):
    job_id: str = "active_awareness_tick"
    reason: str = "cron_manual"


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


class RetentionCompactRequest(BaseModel):
    dry_run: bool = False
    min_files_per_group: int = 10
    max_groups: int | None = None


class ReviewQueueActionRequest(BaseModel):
    reason: str = "runtime hygiene"


def build_ops_runtime_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/ops/safety/red-team")
    async def ops_safety_red_team() -> dict[str, Any]:
        return deps["safety_validation"].run()

    @router.get("/ops/retention")
    async def ops_retention() -> dict[str, Any]:
        return deps["retention_policy"].summary()

    @router.post("/ops/retention/enforce")
    async def ops_retention_enforce(request: RetentionEnforceRequest | None = None) -> dict[str, Any]:
        payload = request or RetentionEnforceRequest()
        return deps["retention_policy"].enforce(dry_run=payload.dry_run, limits=payload.limit_overrides)

    @router.post("/ops/retention/compact")
    async def ops_retention_compact(request: RetentionCompactRequest | None = None) -> dict[str, Any]:
        payload = request or RetentionCompactRequest()
        return deps["retention_policy"].compact_archives(
            dry_run=payload.dry_run,
            min_files_per_group=max(2, payload.min_files_per_group),
            max_groups=payload.max_groups,
        )

    @router.get("/ops/reviews/diagnostic")
    async def ops_reviews_diagnostic(stale_after_days: int = 7) -> dict[str, Any]:
        return deps["review_queue"].diagnostic(stale_after_days=stale_after_days)

    @router.post("/ops/reviews/{review_id}/resolve")
    async def ops_review_resolve(review_id: str, request: ReviewQueueActionRequest | None = None) -> dict[str, Any]:
        payload = request or ReviewQueueActionRequest()
        try:
            return deps["review_queue"].mark_resolved(review_id, payload.reason)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/ops/reviews/{review_id}/archive")
    async def ops_review_archive(review_id: str, request: ReviewQueueActionRequest | None = None) -> dict[str, Any]:
        payload = request or ReviewQueueActionRequest()
        try:
            return deps["review_queue"].archive(review_id, payload.reason)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/ops/health")
    async def ops_health() -> dict[str, Any]:
        return deps["ops_monitor"].health()

    @router.get("/ops/alerts")
    async def ops_alerts() -> dict[str, Any]:
        return deps["ops_monitor"].alerts()

    @router.get("/ops/alerting")
    async def ops_alerting_status() -> dict[str, Any]:
        return deps["alert_dispatcher"].status()

    @router.post("/ops/alerting/config")
    async def ops_alerting_config(request: AlertingConfigRequest) -> dict[str, Any]:
        return deps["alert_dispatcher"].configure(request.model_dump(exclude_none=True))

    @router.post("/ops/alerts/dispatch")
    async def ops_alerts_dispatch(min_severity: str | None = None) -> dict[str, Any]:
        return deps["alert_dispatcher"].dispatch(min_severity=min_severity)

    @router.get("/ops/deployment")
    async def ops_deployment() -> dict[str, Any]:
        return deps["deployment_readiness"]()

    @router.get("/ops/deployment/config")
    async def ops_deployment_config() -> dict[str, Any]:
        return deps["deployment_validator"].validate()

    @router.get("/ops/runtime-matrix")
    async def ops_runtime_matrix_status() -> dict[str, Any]:
        return deps["runtime_matrix"].status()

    @router.post("/ops/runtime-matrix/run")
    async def ops_runtime_matrix_run(write_memory_probe: bool = False) -> dict[str, Any]:
        return deps["runtime_matrix"].run(write_memory_probe=write_memory_probe)

    @router.get("/ops/external-runtime")
    async def ops_external_runtime_status() -> dict[str, Any]:
        return deps["external_runtime_probe"].status(run_runtime_matrix=False, write_memory_probe=False)

    @router.post("/ops/external-runtime/probe")
    async def ops_external_runtime_probe(write_memory_probe: bool = False) -> dict[str, Any]:
        return deps["external_runtime_probe"].status(run_runtime_matrix=True, write_memory_probe=write_memory_probe)

    @router.get("/runtime/active-loop")
    async def runtime_active_loop_status() -> dict[str, Any]:
        return deps["active_loop"].status()

    @router.post("/runtime/active-loop/start")
    async def runtime_active_loop_start(request: ActiveLoopRequest) -> dict[str, Any]:
        return deps["active_loop"].start(interval_seconds=request.interval_seconds)

    @router.post("/runtime/active-loop/stop")
    async def runtime_active_loop_stop() -> dict[str, Any]:
        return deps["active_loop"].stop()

    @router.post("/runtime/active-loop/tick")
    async def runtime_active_loop_tick(request: ActiveLoopTickRequest | None = None) -> dict[str, Any]:
        payload = request or ActiveLoopTickRequest()
        return deps["active_loop"].tick(reason=payload.reason, include_runtime_matrix=payload.include_runtime_matrix)

    @router.get("/runtime/cron")
    async def runtime_cron_status() -> dict[str, Any]:
        return deps["runtime_cron"].status()

    @router.post("/runtime/cron/config")
    async def runtime_cron_config(request: CronConfigRequest) -> dict[str, Any]:
        return deps["runtime_cron"].configure(
            job_id=request.job_id,
            enabled=request.enabled,
            interval_seconds=request.interval_seconds,
            include_runtime_matrix=request.include_runtime_matrix,
        )

    @router.post("/runtime/cron/run")
    async def runtime_cron_run(request: CronRunRequest | None = None) -> dict[str, Any]:
        payload = request or CronRunRequest()
        return deps["runtime_cron"].run_once(job_id=payload.job_id, reason=payload.reason)

    @router.post("/runtime/cron/run-due")
    async def runtime_cron_run_due() -> dict[str, Any]:
        return deps["runtime_cron"].run_due()

    @router.post("/ops/soak")
    async def ops_soak(request: SoakRequest) -> dict[str, Any]:
        return deps["soak_runner"].run(iterations=request.iterations)

    @router.get("/ops/soak/status")
    async def ops_soak_status() -> dict[str, Any]:
        return deps["soak_runner"].status()

    @router.post("/ops/soak/start")
    async def ops_soak_start(request: SoakSessionRequest) -> dict[str, Any]:
        return deps["soak_runner"].start(iterations=request.iterations, interval_seconds=request.interval_seconds)

    @router.post("/ops/soak/stop")
    async def ops_soak_stop() -> dict[str, Any]:
        return deps["soak_runner"].stop()

    return router
