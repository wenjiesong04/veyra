from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from runtime.retention_policy import RetentionPolicy
from runtime.safety_validation import SafetyValidation


class OpsMonitor:
    """Aggregates local production-readiness signals into alerts."""

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        agent_status_resolver: Callable[[], dict[str, Any]],
        retention_policy: RetentionPolicy,
        safety_validation: SafetyValidation,
    ) -> None:
        self.state_store = state_store
        self.agent_status_resolver = agent_status_resolver
        self.retention_policy = retention_policy
        self.safety_validation = safety_validation

    def health(self) -> dict[str, Any]:
        alerts = self.alerts()["items"]
        severity_order = {"info": 0, "warning": 1, "critical": 2}
        max_severity = max((severity_order.get(str(item.get("severity")), 0) for item in alerts), default=0)
        status = "critical" if max_severity >= 2 else "degraded" if max_severity == 1 else "healthy"
        return {
            "status": status,
            "checked_at": utc_now_iso(),
            "alert_count": len(alerts),
            "alerts": alerts,
            "components": self._components(alerts),
        }

    def alerts(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        items.extend(self._safety_alerts())
        items.extend(self._retention_alerts())
        items.extend(self._agent_alerts())
        items.extend(self._task_alerts())
        items.extend(self._review_alerts())
        items.extend(self._belief_alerts())
        items.extend(self._heartbeat_alerts())
        return {"status": "success", "items": items, "summary": self._alert_summary(items)}

    def deployment_readiness(self) -> dict[str, Any]:
        health = self.health()
        checks = [
            self._check("safety_red_team", not any(item.get("component") == "safety" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("retention_within_limits", not any(item.get("component") == "retention" and item.get("severity") == "warning" for item in health["alerts"])),
            self._check("agent_runtime_known", not any(item.get("component") == "agent" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("no_stale_heartbeat", not any(item.get("component") == "heartbeat" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("no_belief_conflict", not any(item.get("component") == "belief" and item.get("severity") == "critical" for item in health["alerts"])),
        ]
        ready = all(item["passed"] for item in checks)
        return {
            "status": "ready" if ready else "not_ready",
            "checked_at": utc_now_iso(),
            "checks": checks,
            "health_status": health["status"],
            "blocking_alerts": [item for item in health["alerts"] if item.get("severity") == "critical"],
        }

    def _safety_alerts(self) -> list[dict[str, Any]]:
        safety = self.safety_validation.run()
        if safety.get("status") == "passed":
            return []
        return [
            {
                "component": "safety",
                "severity": "critical",
                "code": "red_team_failed",
                "message": "Safety validation failed.",
                "details": safety,
            }
        ]

    def _retention_alerts(self) -> list[dict[str, Any]]:
        retention = self.retention_policy.summary()
        alerts = []
        for item in retention.get("files", []):
            if item.get("status") == "over_limit":
                alerts.append(
                    {
                        "component": "retention",
                        "severity": "warning",
                        "code": "log_over_retention_limit",
                        "message": f"{item.get('file')} has {item.get('entries')} entries over limit {item.get('limit')}.",
                        "details": item,
                    }
                )
        return alerts

    def _agent_alerts(self) -> list[dict[str, Any]]:
        try:
            status = self.agent_status_resolver()
        except Exception as exc:
            return [{"component": "agent", "severity": "critical", "code": "agent_status_error", "message": str(exc), "details": {}}]
        raw_status = str(status.get("status") or "unknown")
        connected = bool(status.get("connected"))
        if connected or raw_status in {"available", "ok", "success"}:
            return []
        severity = "warning" if raw_status in {"adapter_unconfigured", "unconfigured", "unknown"} else "critical"
        return [
            {
                "component": "agent",
                "severity": severity,
                "code": "agent_runtime_not_connected",
                "message": f"Selected Agent runtime is {raw_status}.",
                "details": status,
            }
        ]

    def _task_alerts(self) -> list[dict[str, Any]]:
        task_state = self.state_store.read_json("task_state.json")
        pending = task_state.get("pending_agent_tasks", [])
        if not isinstance(pending, list) or not pending:
            return []
        return [
            {
                "component": "tasks",
                "severity": "warning",
                "code": "pending_agent_tasks",
                "message": f"{len(pending)} Agent task(s) are pending refresh.",
                "details": {"count": len(pending), "items": pending[-10:]},
            }
        ]

    def _review_alerts(self) -> list[dict[str, Any]]:
        review_queue = self.state_store.read_json("review_queue.json")
        items = review_queue.get("items", [])
        pending = [item for item in items if isinstance(item, dict) and item.get("status") == "pending"] if isinstance(items, list) else []
        if not pending:
            return []
        return [
            {
                "component": "review",
                "severity": "warning",
                "code": "pending_reviews",
                "message": f"{len(pending)} action review item(s) are pending.",
                "details": {"count": len(pending), "items": pending[-10:]},
            }
        ]

    def _belief_alerts(self) -> list[dict[str, Any]]:
        summary = self.state_store.read_json("belief_state.json").get("summary", {})
        alerts = []
        conflict = int(summary.get("conflict") or 0)
        stale = int(summary.get("stale") or 0)
        if conflict:
            alerts.append(
                {
                    "component": "belief",
                    "severity": "critical",
                    "code": "belief_conflicts",
                    "message": f"{conflict} belief claim(s) are conflicting.",
                    "details": summary,
                }
            )
        if stale:
            alerts.append(
                {
                    "component": "belief",
                    "severity": "warning",
                    "code": "stale_beliefs",
                    "message": f"{stale} belief claim(s) are stale.",
                    "details": summary,
                }
            )
        return alerts

    def _heartbeat_alerts(self) -> list[dict[str, Any]]:
        heartbeat = self.state_store.read_text("heartbeat.md")
        updated_at = None
        for line in heartbeat.splitlines():
            if line.startswith("updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
        if not updated_at:
            return [{"component": "heartbeat", "severity": "warning", "code": "heartbeat_missing_timestamp", "message": "Heartbeat has no updated_at.", "details": {}}]
        age = self._age_seconds(updated_at)
        if age is None:
            return [{"component": "heartbeat", "severity": "warning", "code": "heartbeat_unparseable", "message": "Heartbeat updated_at cannot be parsed.", "details": {"updated_at": updated_at}}]
        if age > 3600:
            return [{"component": "heartbeat", "severity": "critical", "code": "heartbeat_stale", "message": f"Heartbeat is stale by {int(age)} seconds.", "details": {"updated_at": updated_at, "age_seconds": age}}]
        if age > 600:
            return [{"component": "heartbeat", "severity": "warning", "code": "heartbeat_aging", "message": f"Heartbeat is aging by {int(age)} seconds.", "details": {"updated_at": updated_at, "age_seconds": age}}]
        return []

    def _components(self, alerts: list[dict[str, Any]]) -> dict[str, str]:
        components = {name: "ok" for name in ["safety", "retention", "agent", "tasks", "review", "belief", "heartbeat"]}
        for alert in alerts:
            component = str(alert.get("component") or "unknown")
            severity = str(alert.get("severity") or "info")
            if severity == "critical":
                components[component] = "critical"
            elif severity == "warning" and components.get(component) != "critical":
                components[component] = "warning"
        return components

    def _alert_summary(self, alerts: list[dict[str, Any]]) -> dict[str, int]:
        summary = {"critical": 0, "warning": 0, "info": 0, "total": len(alerts)}
        for alert in alerts:
            severity = str(alert.get("severity") or "info")
            if severity in summary:
                summary[severity] += 1
        return summary

    def _check(self, name: str, passed: bool) -> dict[str, Any]:
        return {"name": name, "passed": passed}

    def _age_seconds(self, value: str) -> float | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds()
