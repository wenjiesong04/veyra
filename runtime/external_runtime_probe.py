from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from core.model_client import redact_sensitive
from interface.event_schema import utc_now_iso


class ExternalRuntimeProbe:
    """Builds a normalized validation view for Agent runtimes and Feishu connectivity."""

    def __init__(
        self,
        *,
        runtime_matrix: Any,
        feishu_status_resolver: Callable[[], dict[str, Any]] | None = None,
        channel_status_resolver: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.runtime_matrix = runtime_matrix
        self.feishu_status_resolver = feishu_status_resolver
        self.channel_status_resolver = channel_status_resolver

    def status(self, *, run_runtime_matrix: bool = False, write_memory_probe: bool = False) -> dict[str, Any]:
        runtime_matrix = self._runtime_matrix(run_runtime_matrix=run_runtime_matrix, write_memory_probe=write_memory_probe)
        feishu = self._feishu_status()
        statuses = [runtime_matrix["validation_status"], feishu["validation"]["status"]]
        overall = self._overall_status(statuses)
        status_counts = dict(Counter(statuses))
        return {
            "status": overall,
            "checked_at": utc_now_iso(),
            "validation": {
                "status": overall,
                "statuses": status_counts,
                "validated": status_counts.get("validated", 0),
                "validation_pending": status_counts.get("validation_pending", 0),
                "not_configured": status_counts.get("not_configured", 0),
                "error": status_counts.get("error", 0),
            },
            "runtime_matrix": {
                "status": runtime_matrix["status"],
                "summary": runtime_matrix["summary"],
                "validation_status": runtime_matrix["validation_status"],
                "runtimes": runtime_matrix["runtimes"],
            },
            "feishu": feishu,
        }

    def _runtime_matrix(self, *, run_runtime_matrix: bool, write_memory_probe: bool) -> dict[str, Any]:
        try:
            raw = self.runtime_matrix.run(write_memory_probe=write_memory_probe) if run_runtime_matrix else self.runtime_matrix.status()
        except Exception as exc:
            return {
                "status": "error",
                "summary": {"total": 0, "ready": 0, "degraded": 0, "not_configured": 0, "error": 1},
                "validation_status": "error",
                "runtimes": [],
                "error": str(exc),
            }
        if not isinstance(raw, dict):
            return {
                "status": "error",
                "summary": {"total": 0, "ready": 0, "degraded": 0, "not_configured": 0, "error": 1},
                "validation_status": "error",
                "runtimes": [],
                "error": "runtime_matrix response is not a dict",
            }
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        validation = raw.get("validation") if isinstance(raw.get("validation"), dict) else {}
        validation_status = str(validation.get("status") or self._matrix_validation_from_status(str(raw.get("status") or "unknown")))
        runtimes = raw.get("runtimes") if isinstance(raw.get("runtimes"), list) else []
        return {
            "status": str(raw.get("status") or "unknown"),
            "summary": {
                "total": int(summary.get("total", 0) or 0),
                "ready": int(summary.get("ready", 0) or 0),
                "degraded": int(summary.get("degraded", 0) or 0),
                "not_configured": int(summary.get("not_configured", 0) or 0),
                "error": int(summary.get("error", 0) or 0),
            },
            "validation_status": validation_status,
            "runtimes": redact_sensitive(runtimes, max_string=1200, max_list=30),
        }

    def _feishu_status(self) -> dict[str, Any]:
        raw_status = self._safe_resolve(self.feishu_status_resolver)
        channel_status = self._safe_resolve(self.channel_status_resolver)
        channels = channel_status.get("channels") if isinstance(channel_status.get("channels"), dict) else {}
        feishu_channel = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        ws_state = str(raw_status.get("status") or "unknown")
        thread_alive = bool(raw_status.get("thread_alive"))
        configured = bool(raw_status.get("configured")) or bool(feishu_channel.get("enabled") and feishu_channel.get("delivery") == "feishu")
        connected = thread_alive or ws_state in {"running", "already_running"}
        if connected:
            validation_status = "validated"
        elif ws_state in {"error", "dependency_missing"}:
            validation_status = "error" if configured else "validation_pending"
        elif configured:
            validation_status = "validation_pending"
        else:
            validation_status = "not_configured"
        return {
            "status": ws_state,
            "configured": configured,
            "connected": connected,
            "thread_alive": thread_alive,
            "channel": redact_sensitive(feishu_channel, max_string=1000),
            "channel_counters": {
                "session_count": int(channel_status.get("session_count", 0) or 0),
                "inbox_count": int(channel_status.get("inbox_count", 0) or 0),
                "outbox_count": int(channel_status.get("outbox_count", 0) or 0),
            },
            "validation": {
                "implemented": True,
                "configured": configured,
                "connected": connected,
                "validated": connected,
                "status": validation_status,
            },
            "details": redact_sensitive(raw_status, max_string=1400),
        }

    def _safe_resolve(self, resolver: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        if not resolver:
            return {}
        try:
            value = resolver()
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}
        return value if isinstance(value, dict) else {}

    def _overall_status(self, statuses: list[str]) -> str:
        if any(status == "error" for status in statuses):
            return "error"
        if any(status == "validation_pending" for status in statuses):
            return "validation_pending"
        if any(status == "validated" for status in statuses):
            return "validated"
        return "not_configured"

    def _matrix_validation_from_status(self, status: str) -> str:
        if status == "ready":
            return "validated"
        if status in {"degraded", "error"}:
            return "validation_pending"
        if status == "not_configured":
            return "not_configured"
        return "validation_pending"
