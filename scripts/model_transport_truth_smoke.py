#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from core.model_client import CoreModelClient
import core.model_client as model_client_module
from runtime.ops_monitor import OpsMonitor


class EmptyRetention:
    def summary(self) -> dict[str, Any]:
        return {"files": []}


class PassingSafety:
    def run(self) -> dict[str, Any]:
        return {"status": "passed"}


def expect(condition: bool, label: str, details: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def transport_contract_smoke() -> None:
    """Exercise the synchronous HTTPX seam without making an external call."""

    with tempfile.TemporaryDirectory(prefix="veyra-model-transport-") as temp_root:
        def configured_client(timeout: float = 0.25, retries: int = 2) -> CoreModelClient:
            store = WorldStateStore(Path(temp_root) / "state")
            store.write_json(
                "agent_config.json",
                {
                    "core_model": {
                        "enabled": True,
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "transport-smoke",
                        "timeout": timeout,
                        "retries": retries,
                    }
                },
            )
            return CoreModelClient(store)

        real_client = model_client_module.httpx.Client

        observed: dict[str, Any] = {}
        calls = 0

        def json_client(**kwargs: Any) -> httpx.Client:
            observed.update(kwargs)

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": '{"answer":"ok"}'}}]},
                    request=request,
                )

            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(model_client_module.httpx, "Client", json_client):
            result = configured_client().complete_json(system="Return JSON.", user="{}", purpose="transport_smoke")
        timeout = observed.get("timeout")
        expect(result.get("status") == "model_assisted" and result.get("answer") == "ok", "HTTPX JSON response contract", result)
        expect(calls == 1, "successful model request is sent once", calls)
        expect(
            timeout is not None
            and timeout.connect == 0.25
            and timeout.read == 0.25
            and timeout.write == 0.25
            and timeout.pool == 0.25,
            "HTTPX exposes explicit phase timeouts",
            timeout,
        )
        expect(observed.get("trust_env") is True and bool(observed.get("verify")), "proxy and CA trust are explicit", observed)

        observed = {}
        calls = 0

        def status_client(**kwargs: Any) -> httpx.Client:
            observed.update(kwargs)

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(429, json={"error": "rate limited"}, request=request)

            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(model_client_module.httpx, "Client", status_client):
            result = configured_client().complete_json(system="Return JSON.", user="{}", purpose="transport_http_error")
        expect(result.get("status") == "http_error" and result.get("status_code") == 429, "HTTP status is preserved as http_error", result)
        expect(calls == 1, "HTTP status is not retried", calls)

        observed = {}
        calls = 0

        def connect_timeout_client(**kwargs: Any) -> httpx.Client:
            observed.update(kwargs)

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                raise httpx.ConnectTimeout("synthetic connect timeout", request=request)

            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(model_client_module.httpx, "Client", connect_timeout_client):
            result = configured_client().complete_json(system="Return JSON.", user="{}", purpose="transport_connect_timeout")
        expect(result.get("status") == "error" and result.get("attempts") == 3, "connect timeout uses bounded retries", result)
        expect(calls == 3, "connect timeout retries exactly retries plus one", calls)

        observed = {}
        calls = 0

        def timeout_client(**kwargs: Any) -> httpx.Client:
            observed.update(kwargs)

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                raise httpx.ReadTimeout("synthetic read timeout", request=request)

            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(model_client_module.httpx, "Client", timeout_client):
            result = configured_client().complete_json(system="Return JSON.", user="{}", purpose="transport_timeout")
        expect(result.get("status") == "error" and result.get("attempts") == 1, "read timeout remains a single error", result)
        expect(calls == 1, "read timeout is never retried", calls)


def main() -> int:
    transport_contract_smoke()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="veyra-model-truth-") as temp_dir:
        store = WorldStateStore(Path(temp_dir) / "state")
        store.write_json(
            "agent_config.json",
            {
                "core_model": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "local-smoke",
                }
            },
        )
        client = CoreModelClient(store)
        pending = client.status()
        expect(pending["configured"] and pending["validation"]["status"] == "validation_pending", "configured is not treated as validated", pending)
        store.append_jsonl(
            "core_model_trace.jsonl",
            {
                "purpose": "truth_smoke",
                "status": "invalid_json",
                "duration_ms": 25,
                "timestamp": now.isoformat(),
                "result": {"status": "invalid_json", "duration_ms": 25},
            },
        )
        degraded = client.status()
        expect(degraded["validation"]["status"] == "degraded", "recent invalid JSON marks transport degraded", degraded)

        recent_failure = {
            "enabled": True,
            "configured": True,
            "validation": {
                "status": "degraded",
                "transport_status": "invalid_json",
                "observed_at": now.isoformat(),
                "age_seconds": 2,
            },
            "last_transport_status": {"status": "invalid_json", "timestamp": now.isoformat()},
        }
        monitor = OpsMonitor(
            store,
            agent_status_resolver=lambda: {"connected": True, "status": "available"},
            retention_policy=EmptyRetention(),
            safety_validation=PassingSafety(),
            model_status_resolver=lambda: recent_failure,
        )
        alerts = monitor._model_alerts()
        expect(alerts and alerts[0]["severity"] == "warning", "recent invalid transport degrades health", alerts)

        stale_failure = {
            **recent_failure,
            "validation": {
                "status": "stale",
                "transport_status": "invalid_json",
                "observed_at": (now - timedelta(hours=2)).isoformat(),
                "age_seconds": 7200,
            },
        }
        stale_monitor = OpsMonitor(
            store,
            agent_status_resolver=lambda: {"connected": True, "status": "available"},
            retention_policy=EmptyRetention(),
            safety_validation=PassingSafety(),
            model_status_resolver=lambda: stale_failure,
        )
        stale_alerts = stale_monitor._model_alerts()
        expect(stale_alerts and stale_alerts[0]["severity"] == "info", "stale failure does not masquerade as current degradation", stale_alerts)

        store.write_json(
            "belief_state.json",
            {
                "summary": {"fresh": 0, "stale": 1, "expired": 0, "conflict": 1, "total": 1},
                "claims": [
                    {
                        "key": "local_system:platform",
                        "status": "conflict",
                        "expires_at": (now - timedelta(days=8)).isoformat(),
                        "ttl_remaining_seconds": -700000,
                        "claim": "stale probe conflict",
                    }
                ],
            },
        )
        store.write_json(
            "review_queue.json",
            {
                "items": [
                    {
                        "review_id": "rev_aged",
                        "status": "pending",
                        "created_at": (now - timedelta(days=47)).isoformat(),
                        "task_text": "restart selected agent runtime",
                        "risk_level": "R4",
                        "event_id": "evt_aged",
                    },
                    {
                        "review_id": "rev_fresh",
                        "status": "pending",
                        "created_at": now.isoformat(),
                        "task_text": "confirm a living-context fact",
                        "risk_level": "R3",
                        "event_id": "evt_fresh",
                    },
                ]
            },
        )
        honesty = OpsMonitor(
            store,
            agent_status_resolver=lambda: {"connected": True, "status": "available", "updated_at": now.isoformat(), "ttl_seconds": 300},
            retention_policy=EmptyRetention(),
            safety_validation=PassingSafety(),
            model_status_resolver=lambda: {"enabled": True, "configured": True, "validation": {"status": "ok"}},
        )
        belief_alerts = honesty._belief_alerts()
        expect(
            any(item.get("code") == "stale_belief_conflicts" and item.get("severity") == "warning" for item in belief_alerts),
            "past-TTL belief conflicts are warning, not critical",
            belief_alerts,
        )
        expect(
            not any(item.get("code") == "belief_conflicts" for item in belief_alerts),
            "stale conflicts do not keep the live belief_conflicts critical code",
            belief_alerts,
        )
        review_alerts = honesty._review_alerts()
        expect(
            any(item.get("code") == "aged_pending_reviews" and item.get("severity") == "info" for item in review_alerts),
            "reviews older than 7 days are aged info, not a live warning",
            review_alerts,
        )
        expect(
            any(item.get("code") == "pending_reviews" and item.get("details", {}).get("count") == 1 for item in review_alerts),
            "fresh pending reviews remain a warning",
            review_alerts,
        )

    print("model transport truth smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
