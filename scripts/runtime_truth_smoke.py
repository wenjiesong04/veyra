from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from probes.port_probe import PortProbe
from runtime.routing_metrics import RoutingMetrics
from runtime.runtime_matrix import RuntimeMatrix


class _EmptyRegistry:
    def refresh(self) -> None:
        return None

    def names(self) -> list[str]:
        return []


class _UnusedMemoryBridge:
    pass


def _port_probe_checks() -> None:
    probe = PortProbe()
    assert probe._extract_port("127.0.0.1:8000") == 8000
    assert probe._extract_port("http://127.0.0.1:8000/health") == 8000
    assert probe._extract_port("localhost:18789") == 18789
    assert probe._extract_port("[::1]:443") == 443
    assert probe._extract_port("检查 18789 端口") == 18789
    assert probe._extract_port("检查端口 8") == 8
    assert probe._extract_port("127.0.0.1") is None
    assert probe._extract_port("127.0.0.1:70000") is None
    assert probe._extract_port("计划在 12:30 执行") is None
    result = probe.run("127.0.0.1:8000")
    assert result["target"] == "127.0.0.1:8000", result
    assert result["details"]["port"] == 8000, result


def _runtime_matrix_checks(store: WorldStateStore) -> None:
    clock = [datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)]
    matrix = RuntimeMatrix(
        store,
        _EmptyRegistry(),  # type: ignore[arg-type]
        _UnusedMemoryBridge(),  # type: ignore[arg-type]
        ttl_seconds=30,
        now_fn=lambda: clock[0],
    )
    observed_at = clock[0].isoformat()
    store.write_json(
        "ops_runtime_matrix.json",
        {
            "status": "ready",
            "checked_at": observed_at,
            "observed_at": observed_at,
            "ttl_seconds": 30,
            "summary": {"total": 1, "ready": 1, "degraded": 0, "not_configured": 0, "error": 0},
            "validation": {"codebase": "implemented", "configured": 1, "validated": 1, "status": "validated"},
            "runtimes": [{"name": "openclaw", "status": "ready"}],
        },
    )

    fresh = matrix.status()
    assert fresh["status"] == "ready", fresh
    assert fresh["freshness"] == "fresh", fresh
    assert fresh["age_seconds"] == 0, fresh
    assert fresh["expires_at"] == (clock[0] + timedelta(seconds=30)).isoformat(), fresh
    assert fresh["validation"]["status"] == "validated", fresh

    clock[0] += timedelta(seconds=31)
    stale = matrix.status()
    assert stale["status"] == "stale", stale
    assert stale["observed_status"] == "ready", stale
    assert stale["freshness"] == "stale", stale
    assert stale["age_seconds"] == 31, stale
    assert stale["validation"]["status"] == "validation_pending", stale
    assert stale["validation"]["observed_status"] == "validated", stale
    assert stale["validation"]["validated"] == 0, stale

    refreshed = matrix.run()
    assert refreshed["status"] == "not_configured", refreshed
    assert refreshed["freshness"] == "fresh", refreshed
    assert refreshed["observed_at"] == clock[0].isoformat(), refreshed
    assert refreshed["expires_at"] == (clock[0] + timedelta(seconds=30)).isoformat(), refreshed


def _routing_metrics_checks(store: WorldStateStore) -> None:
    store.append_jsonl(
        "decision_trace.jsonl",
        {
            "trace_id": "rt_success",
            "final_route": "direct_answer",
            "route": "direct_answer",
            "status": "success",
            "latency_ms": 12,
            "failure_reason": "",
        },
    )
    store.append_jsonl(
        "decision_trace.jsonl",
        {
            "route": "proactive_intent_planner",
            "status": "error",
            "artifacts": {"planner": {"status": "error"}},
        },
    )
    store.append_jsonl(
        "decision_trace.jsonl",
        {
            "route": "semantic_change_set",
            "status": "pending_confirmation",
            "artifacts": {"changeset_id": "chg_smoke"},
        },
    )

    metrics = RoutingMetrics(store)
    summary = metrics.summary()
    assert summary["window_size"] == 1, summary
    assert summary["route_distribution"] == {"direct_answer": 1}, summary
    assert summary["runtime_failure_count"] == 0, summary
    assert metrics.routes()["total"] == 1, metrics.routes()
    assert metrics.failures()["items"] == [], metrics.failures()

    store.append_jsonl(
        "decision_trace.jsonl",
        {
            "trace_id": "rt_failure",
            "final_route": "probe",
            "route": "probe",
            "status": "error",
            "failure_reason": "probe_failed",
        },
    )
    failed = metrics.summary()
    assert failed["window_size"] == 2, failed
    assert failed["route_distribution"] == {"direct_answer": 1, "probe": 1}, failed
    assert failed["runtime_failure_count"] == 1, failed
    assert metrics.failures()["items"][0]["trace_id"] == "rt_failure", metrics.failures()


def main() -> None:
    _port_probe_checks()
    with TemporaryDirectory(prefix="veyra-runtime-truth-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        _runtime_matrix_checks(store)
    with TemporaryDirectory(prefix="veyra-routing-truth-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        _routing_metrics_checks(store)
    print("runtime_truth_smoke: ok")


if __name__ == "__main__":
    main()
