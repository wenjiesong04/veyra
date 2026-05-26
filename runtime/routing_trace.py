from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any
from uuid import uuid4

from core.definitions import RiskLevel
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import LoopResult, VeyraEvent, utc_now_iso


SUCCESS_STATUSES = {"success", "ok", "verified_success", "partially_success", "submitted", "running", "pending"}


class RuntimeTraceRecorder:
    """Append-only routing trace recorder for real runtime traffic."""

    def __init__(self, state_store: WorldStateStore, *, filename: str = "runtime_trace.jsonl") -> None:
        self.state_store = state_store
        self.filename = filename

    def record(
        self,
        *,
        event: VeyraEvent,
        result: LoopResult,
        started_at: float,
        route_trace: list[dict[str, Any]] | None = None,
        context_metrics: dict[str, Any] | None = None,
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        latency_ms = int(max(0.0, time.perf_counter() - started_at) * 1000)
        artifacts = result.artifacts if isinstance(result.artifacts, dict) else {}
        model_calls = self._model_calls(artifacts)
        probe_used = self._probe_used(artifacts)
        agent_used = self._agent_used(artifacts)
        metrics = context_metrics or self._context_metrics(artifacts)
        status = str(result.status)
        risk_level = result.risk_level.value if isinstance(result.risk_level, RiskLevel) else str(result.risk_level)
        trace_id = f"rt_{uuid4().hex[:12]}"
        trace = {
            "trace_id": trace_id,
            "event_id": event.event_id,
            "message_id": self._message_id(event),
            "channel": event.source.channel,
            "source": self._source(event),
            "user_hash": self._hash(event.source.user_id),
            "session_hash": self._hash(event.source.session_id),
            "message_hash": self._hash(str(event.payload.get("text") or "")),
            "message_chars": len(str(event.payload.get("text") or "")),
            "message_preview": self._preview(str(event.payload.get("text") or "")),
            "received_at": event.timestamp,
            "completed_at": utc_now_iso(),
            "latency_ms": latency_ms,
            "route_trace": redact_sensitive(route_trace or [], max_string=360, max_list=20),
            "final_route": result.route.value,
            "status": status,
            "risk_level": risk_level,
            "model_used": bool(model_calls),
            "model_calls": model_calls,
            "probe_used": probe_used,
            "agent_used": agent_used,
            "context_chars": int(metrics.get("context_chars") or 0),
            "estimated_tokens": int(metrics.get("estimated_tokens") or 0),
            "context_drift": redact_sensitive(artifacts.get("context_drift") or {}, max_string=480, max_list=12),
            "memory_policy": self._memory_policy(artifacts),
            "failure_reason": failure_reason or self._failure_reason(result, artifacts),
            "openclaw_call": self._openclaw_call(probe_used, agent_used),
        }
        self.state_store.append_jsonl(self.filename, trace)
        return trace

    def record_intake_result(
        self,
        *,
        text: str,
        channel: str,
        user_id: str,
        session_id: str,
        message_id: str | None,
        metadata: dict[str, Any] | None,
        started_at: float,
        status: str,
        final_route: str,
        reason: str,
    ) -> dict[str, Any]:
        latency_ms = int(max(0.0, time.perf_counter() - started_at) * 1000)
        trace = {
            "trace_id": f"rt_{uuid4().hex[:12]}",
            "event_id": None,
            "message_id": message_id or "",
            "channel": channel,
            "source": self._source_from_metadata(metadata, channel),
            "user_hash": self._hash(user_id),
            "session_hash": self._hash(session_id),
            "message_hash": self._hash(text),
            "message_chars": len(text),
            "message_preview": self._preview(text),
            "received_at": utc_now_iso(),
            "completed_at": utc_now_iso(),
            "latency_ms": latency_ms,
            "route_trace": [
                {"phase": "intake", "status": status, "channel": channel},
                {"phase": "response", "status": status, "final_route": final_route},
            ],
            "final_route": final_route,
            "status": status,
            "risk_level": "R0",
            "model_used": False,
            "model_calls": [],
            "probe_used": None,
            "agent_used": None,
            "context_chars": 0,
            "estimated_tokens": 0,
            "context_drift": {},
            "memory_policy": "",
            "failure_reason": reason,
            "openclaw_call": False,
        }
        self.state_store.append_jsonl(self.filename, trace)
        return trace

    def recent(self, *, limit: int = 50, channel: str | None = None) -> dict[str, Any]:
        items = self.state_store.read_jsonl(self.filename, limit=max(limit, 500))
        if channel:
            items = [item for item in items if item.get("channel") == channel]
        return {"status": "success", "items": [self._public_trace(item) for item in items[-limit:]]}

    def get(self, trace_id: str) -> dict[str, Any] | None:
        for item in reversed(self.state_store.read_jsonl(self.filename, limit=5000)):
            if item.get("trace_id") == trace_id:
                return self._public_trace(item)
        return None

    def soak_status(self, *, limit: int = 200) -> dict[str, Any]:
        traces = self.state_store.read_jsonl(self.filename, limit=limit)
        feishu = [item for item in traces if item.get("channel") == "feishu"]
        failures = [item for item in feishu if item.get("failure_reason")]
        latencies = [int(item.get("latency_ms") or 0) for item in feishu if item.get("latency_ms") is not None]
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
        return {
            "status": "no_feishu_traces" if not feishu else "degraded" if failures else "healthy",
            "window_size": len(feishu),
            "last_trace_at": feishu[-1].get("completed_at") if feishu else None,
            "avg_latency_ms": avg_latency,
            "failure_count": len(failures),
            "recent_failures": redact_sensitive(failures[-5:], max_string=800, max_list=20),
        }

    def _message_id(self, event: VeyraEvent) -> str:
        metadata = event.payload.get("metadata") if isinstance(event.payload.get("metadata"), dict) else {}
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        return str(feishu.get("message_id") or metadata.get("message_id") or "")

    def _public_trace(self, item: dict[str, Any]) -> dict[str, Any]:
        public = redact_sensitive(item, max_string=1800, max_list=80)
        for key in ("latency_ms", "context_chars", "estimated_tokens", "message_chars"):
            if key in item:
                public[key] = item[key]
        return public

    def _source(self, event: VeyraEvent) -> str:
        metadata = event.payload.get("metadata") if isinstance(event.payload.get("metadata"), dict) else {}
        return self._source_from_metadata(metadata, event.source.channel)

    def _source_from_metadata(self, metadata: dict[str, Any] | None, channel: str) -> str:
        metadata = metadata if isinstance(metadata, dict) else {}
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        return str(feishu.get("source") or metadata.get("source") or channel)

    def _preview(self, text: str) -> str:
        return redact_sensitive(text.strip().replace("\n", " "), max_string=80) if text else ""

    def _hash(self, value: str) -> str:
        if not value:
            return ""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _model_calls(self, value: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for item in self._walk_dicts(value):
            model = item.get("_model") if isinstance(item.get("_model"), dict) else None
            if not model:
                continue
            calls.append(
                {
                    "purpose": model.get("purpose"),
                    "provider": model.get("provider"),
                    "model": model.get("model"),
                }
            )
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for call in calls:
            key = json.dumps(call, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(call)
        return unique

    def _probe_used(self, artifacts: dict[str, Any]) -> str | None:
        probe = artifacts.get("probe_result") if isinstance(artifacts.get("probe_result"), dict) else {}
        if probe.get("probe"):
            return str(probe.get("probe"))
        decision = artifacts.get("decision") if isinstance(artifacts.get("decision"), dict) else {}
        if decision.get("selected_probe"):
            return str(decision.get("selected_probe"))
        return None

    def _agent_used(self, artifacts: dict[str, Any]) -> str | None:
        execution = artifacts.get("execution_result") if isinstance(artifacts.get("execution_result"), dict) else {}
        if execution.get("executor"):
            return str(execution.get("executor"))
        packet = artifacts.get("task_packet") if isinstance(artifacts.get("task_packet"), dict) else {}
        if packet.get("target_agent"):
            return str(packet.get("target_agent"))
        return None

    def _context_metrics(self, artifacts: dict[str, Any]) -> dict[str, Any]:
        direct = artifacts.get("context_metrics") if isinstance(artifacts.get("context_metrics"), dict) else {}
        if direct:
            return direct
        packet = artifacts.get("task_packet") if isinstance(artifacts.get("task_packet"), dict) else {}
        context = packet.get("context_patch") if isinstance(packet.get("context_patch"), dict) else {}
        chars = len(json.dumps(redact_sensitive(context), ensure_ascii=False)) if context else 0
        return {"context_chars": chars, "estimated_tokens": estimate_tokens(chars)}

    def _memory_policy(self, artifacts: dict[str, Any]) -> str:
        execution = artifacts.get("memory_policy_execution") if isinstance(artifacts.get("memory_policy_execution"), dict) else {}
        if execution.get("policy"):
            return str(execution.get("policy"))
        decision = artifacts.get("decision") if isinstance(artifacts.get("decision"), dict) else {}
        return str(decision.get("memory_policy") or "")

    def _failure_reason(self, result: LoopResult, artifacts: dict[str, Any]) -> str:
        if str(result.status) in SUCCESS_STATUSES:
            return ""
        verification = artifacts.get("verification") if isinstance(artifacts.get("verification"), dict) else {}
        guardian = artifacts.get("guardian") if isinstance(artifacts.get("guardian"), dict) else {}
        for candidate in (
            verification.get("verdict"),
            verification.get("message"),
            guardian.get("reason"),
            result.response,
        ):
            if candidate:
                return str(candidate)[:240]
        return str(result.status)

    def _openclaw_call(self, probe_used: str | None, agent_used: str | None) -> bool:
        return any("openclaw" in str(item).lower() for item in (probe_used, agent_used) if item)

    def _walk_dicts(self, value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            found.append(value)
            for item in value.values():
                found.extend(self._walk_dicts(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._walk_dicts(item))
        return found


def estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return int(math.ceil(chars / 4))
