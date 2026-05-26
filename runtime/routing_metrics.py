from __future__ import annotations

from collections import Counter
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore


class RoutingMetrics:
    def __init__(self, state_store: WorldStateStore, *, trace_filename: str = "runtime_trace.jsonl") -> None:
        self.state_store = state_store
        self.trace_filename = trace_filename

    def summary(self, *, limit: int = 1000) -> dict[str, Any]:
        traces = self._traces(limit)
        routes = Counter(str(item.get("final_route") or "unknown") for item in traces)
        latencies = [int(item.get("latency_ms") or 0) for item in traces if item.get("latency_ms") is not None]
        context_chars = [int(item.get("context_chars") or 0) for item in traces if item.get("context_chars") is not None]
        model_calls = sum(len(item.get("model_calls") if isinstance(item.get("model_calls"), list) else []) for item in traces)
        agent_calls = sum(1 for item in traces if item.get("agent_used"))
        probe_calls = sum(1 for item in traces if item.get("probe_used"))
        openclaw_calls = sum(1 for item in traces if item.get("openclaw_call"))
        failures = self.failures(limit=10)["items"]
        return {
            "status": "success",
            "window_size": len(traces),
            "route_distribution": dict(routes),
            "avg_latency_ms": self._avg(latencies),
            "core_model_calls": model_calls,
            "agent_calls": agent_calls,
            "probe_calls": probe_calls,
            "human_review_count": routes.get("human_review", 0),
            "block_count": routes.get("block", 0),
            "context_avg_chars": self._avg(context_chars),
            "estimated_tokens": sum(int(item.get("estimated_tokens") or 0) for item in traces),
            "token_cost_estimate": self.model_cost(limit=limit)["estimate"],
            "recent_failures": failures,
            "openclaw_call_share": round(openclaw_calls / len(traces), 4) if traces else 0.0,
        }

    def routes(self, *, limit: int = 1000) -> dict[str, Any]:
        traces = self._traces(limit)
        counter = Counter(str(item.get("final_route") or "unknown") for item in traces)
        total = sum(counter.values())
        items = [
            {"route": route, "count": count, "share": round(count / total, 4) if total else 0.0}
            for route, count in sorted(counter.items())
        ]
        return {"status": "success", "total": total, "items": items}

    def model_cost(self, *, limit: int = 1000) -> dict[str, Any]:
        traces = self._traces(limit)
        total_context_tokens = sum(int(item.get("estimated_tokens") or 0) for item in traces)
        model_calls = []
        for item in traces:
            for call in item.get("model_calls") if isinstance(item.get("model_calls"), list) else []:
                if isinstance(call, dict):
                    model_calls.append(call)
        by_model = Counter(str(call.get("model") or "unknown") for call in model_calls)
        estimate = {
            "estimated_context_tokens": total_context_tokens,
            "estimated_model_call_count": len(model_calls),
            "estimated_billable_token_units": round(total_context_tokens / 1000, 3),
            "currency_cost": None,
            "note": "No provider price table is configured; this is a token-volume estimate only.",
        }
        return {"status": "success", "estimate": estimate, "by_model": dict(by_model)}

    def failures(self, *, limit: int = 50) -> dict[str, Any]:
        traces = self._traces(max(limit, 1000))
        failures = [item for item in traces if item.get("failure_reason")]
        return {"status": "success", "items": [self._public_trace(item) for item in failures[-limit:]]}

    def _traces(self, limit: int) -> list[dict[str, Any]]:
        return self.state_store.read_jsonl(self.trace_filename, limit=limit)

    def _avg(self, values: list[int]) -> int:
        return int(sum(values) / len(values)) if values else 0

    def _public_trace(self, item: dict[str, Any]) -> dict[str, Any]:
        public = redact_sensitive(item, max_string=1000, max_list=50)
        for key in ("latency_ms", "context_chars", "estimated_tokens", "message_chars"):
            if key in item:
                public[key] = item[key]
        return public
