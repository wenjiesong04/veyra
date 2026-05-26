from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from runtime.routing_metrics import RoutingMetrics
from runtime.routing_trace import RuntimeTraceRecorder


def build_runtime_observability_router(
    *,
    trace_recorder: RuntimeTraceRecorder,
    metrics: RoutingMetrics,
) -> APIRouter:
    router = APIRouter()

    @router.get("/runtime/traces/recent")
    async def runtime_traces_recent(limit: int = 50, channel: str | None = None) -> dict[str, Any]:
        return trace_recorder.recent(limit=min(max(limit, 1), 200), channel=channel)

    @router.get("/runtime/traces/{trace_id}")
    async def runtime_trace(trace_id: str) -> dict[str, Any]:
        trace = trace_recorder.get(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="runtime trace not found")
        return {"status": "success", "trace": trace}

    @router.get("/runtime/soak/status")
    async def runtime_soak_status() -> dict[str, Any]:
        return trace_recorder.soak_status()

    @router.get("/runtime/metrics/summary")
    async def runtime_metrics_summary(limit: int = 1000) -> dict[str, Any]:
        return metrics.summary(limit=min(max(limit, 1), 5000))

    @router.get("/runtime/metrics/routes")
    async def runtime_metrics_routes(limit: int = 1000) -> dict[str, Any]:
        return metrics.routes(limit=min(max(limit, 1), 5000))

    @router.get("/runtime/metrics/model-cost")
    async def runtime_metrics_model_cost(limit: int = 1000) -> dict[str, Any]:
        return metrics.model_cost(limit=min(max(limit, 1), 5000))

    @router.get("/runtime/metrics/failures")
    async def runtime_metrics_failures(limit: int = 50) -> dict[str, Any]:
        return metrics.failures(limit=min(max(limit, 1), 500))

    return router
