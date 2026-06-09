from __future__ import annotations

import time
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from runtime.routing_metrics import RoutingMetrics
from runtime.routing_trace import RuntimeTraceRecorder


def _append_trace(store: WorldStateStore, **payload: object) -> None:
    base = {
        "trace_id": str(payload.get("trace_id") or "rt_smoke"),
        "channel": "feishu",
        "completed_at": "2026-06-09T00:00:00+00:00",
        "latency_ms": 10,
        "final_route": payload.get("final_route") or payload.get("route") or "direct_answer",
        "route": payload.get("route") or payload.get("final_route") or "direct_answer",
        "status": payload.get("status") or "success",
        "failure_reason": payload.get("failure_reason") or "",
    }
    if payload.get("outcome_category"):
        base["outcome_category"] = payload.get("outcome_category")
    store.append_jsonl("decision_trace.jsonl", base)


def main() -> None:
    with TemporaryDirectory(prefix="veyra-runtime-observability-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        recorder = RuntimeTraceRecorder(store)
        metrics = RoutingMetrics(store)

        recorder.record_intake_result(
            text="duplicate",
            channel="feishu",
            user_id="user",
            session_id="session",
            message_id="msg-1",
            metadata={},
            started_at=time.perf_counter(),
            status="duplicate",
            final_route="duplicate",
            reason="duplicate_message",
        )
        _append_trace(store, trace_id="rt_human", final_route="human_review", status="needs_confirmation", failure_reason="guardian_review")
        _append_trace(store, trace_id="rt_block", final_route="block", status="blocked", failure_reason="blocked_by_guardian")

        healthy = recorder.soak_status()
        assert healthy["status"] == "healthy", healthy
        assert healthy["runtime_failure_count"] == 0, healthy
        assert healthy["outcome_counts"].get("duplicate_intake") == 1, healthy
        assert healthy["outcome_counts"].get("expected_governance") == 2, healthy
        assert metrics.failures()["items"] == [], metrics.failures()
        assert len(metrics.failures(include_expected=True)["items"]) == 3

        _append_trace(
            store,
            trace_id="rt_runtime_failure",
            final_route="probe",
            status="adapter_unconfigured",
            failure_reason="weather_probe_error",
        )
        degraded = recorder.soak_status()
        assert degraded["status"] == "degraded", degraded
        assert degraded["failure_count"] == 1, degraded
        assert degraded["runtime_failure_count"] == 1, degraded
        assert degraded["recent_failures"][0]["trace_id"] == "rt_runtime_failure", degraded
        assert len(metrics.failures()["items"]) == 1

    print("runtime_observability_smoke: ok")


if __name__ == "__main__":
    main()
