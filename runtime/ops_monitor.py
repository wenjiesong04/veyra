from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from runtime.conservatism_monitor import ConservatismMonitor
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
        model_status_resolver: Callable[[], dict[str, Any]] | None = None,
        feishu_status_resolver: Callable[[], dict[str, Any]] | None = None,
        active_loop_status_resolver: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.state_store = state_store
        self.agent_status_resolver = agent_status_resolver
        self.retention_policy = retention_policy
        self.safety_validation = safety_validation
        self.model_status_resolver = model_status_resolver
        self.feishu_status_resolver = feishu_status_resolver
        self.active_loop_status_resolver = active_loop_status_resolver
        self.conservatism_monitor = ConservatismMonitor(state_store)

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
        items.extend(self._model_alerts())
        items.extend(self._agent_alerts())
        items.extend(self._feishu_alerts())
        items.extend(self._active_loop_alerts())
        items.extend(self._push_alerts())
        items.extend(self._task_alerts())
        items.extend(self._review_alerts())
        items.extend(self._belief_alerts())
        items.extend(self._heartbeat_alerts())
        # Every alert above answers "did something overstep?". This one answers
        # "did anything happen at all?", which no other check covers.
        items.extend(self.conservatism_monitor.findings())
        return {"status": "success", "items": items, "summary": self._alert_summary(items)}

    def deployment_readiness(self) -> dict[str, Any]:
        health = self.health()
        checks = [
            self._check("safety_red_team", not any(item.get("component") == "safety" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("retention_within_limits", not any(item.get("component") == "retention" and item.get("severity") == "warning" for item in health["alerts"])),
            self._check("agent_runtime_known", not any(item.get("component") == "agent" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("core_model_available", not any(item.get("component") == "model" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("feishu_available", not any(item.get("component") == "feishu" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("active_loop_available", not any(item.get("component") == "active_loop" and item.get("severity") == "critical" for item in health["alerts"])),
            self._check("push_runtime_available", not any(item.get("component") == "push" and item.get("severity") == "critical" for item in health["alerts"])),
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
        alerts: list[dict[str, Any]] = []
        features = status.get("capabilities", {}).get("features") if isinstance(status.get("capabilities"), dict) else {}
        if connected and isinstance(features, dict) and (features.get("memory_summary") is False or features.get("memory_patch") is False):
            alerts.append(
                {
                    "component": "memory",
                    "severity": "info",
                    "code": "openclaw_workspace_memory_fallback",
                    "message": (
                        "OpenClaw native memory RPC is unavailable; "
                        "Veyra-private scoped fallback is enabled."
                    ),
                    "details": {"features": {"memory_summary": features.get("memory_summary"), "memory_patch": features.get("memory_patch")}},
                }
            )
        if connected or raw_status in {"available", "ok", "success"}:
            return alerts
        severity = "warning" if raw_status in {"adapter_unconfigured", "unconfigured", "unknown"} else "critical"
        alerts.append(
            {
                "component": "agent",
                "severity": severity,
                "code": "agent_runtime_not_connected",
                "message": f"Selected Agent runtime is {raw_status}.",
                "details": status,
            }
        )
        return alerts

    def _model_alerts(self) -> list[dict[str, Any]]:
        if self.model_status_resolver is None:
            return []
        try:
            status = self.model_status_resolver()
        except Exception as exc:
            return [{"component": "model", "severity": "critical", "code": "core_model_status_error", "message": str(exc), "details": {}}]
        if not status.get("enabled"):
            return [
                {
                    "component": "model",
                    "severity": "info",
                    "code": "core_model_disabled",
                    "message": "Core model is disabled; deterministic governance rules remain active.",
                    "details": {"status": status.get("status"), "missing": status.get("missing")},
                }
            ]
        if not status.get("configured"):
            return [
                {
                    "component": "model",
                    "severity": "critical",
                    "code": "core_model_not_configured",
                    "message": "Core model is enabled but not fully configured.",
                    "details": {"status": status.get("status"), "missing": status.get("missing"), "api_key_set": status.get("api_key_set")},
                }
            ]
        validation = status.get("validation") if isinstance(status.get("validation"), dict) else {}
        validation_status = str(validation.get("status") or "")
        if validation_status == "degraded":
            transport_status = str(validation.get("transport_status") or status.get("last_transport_status", {}).get("status") or "unknown")
            severity = "critical" if transport_status in {"auth_error"} else "warning"
            return [
                {
                    "component": "model",
                    "severity": severity,
                    "code": "core_model_transport_degraded",
                    "message": f"Core model is configured, but the latest transport result is {transport_status}.",
                    "details": {
                        "validation": validation,
                        "last_transport_status": status.get("last_transport_status"),
                    },
                }
            ]
        if validation_status in {"validation_pending", "stale"}:
            return [
                {
                    "component": "model",
                    "severity": "info",
                    "code": f"core_model_{validation_status}",
                    "message": "Core model transport has not been validated recently.",
                    "details": {
                        "validation": validation,
                        "last_transport_status": status.get("last_transport_status"),
                    },
                }
            ]
        return []

    def _feishu_alerts(self) -> list[dict[str, Any]]:
        if self.feishu_status_resolver is None:
            return []
        try:
            status = self.feishu_status_resolver()
        except Exception as exc:
            return [{"component": "feishu", "severity": "critical", "code": "feishu_status_error", "message": str(exc), "details": {}}]
        config = status.get("channel_config") if isinstance(status.get("channel_config"), dict) else {}
        if not config.get("enabled"):
            return []
        raw_status = str(status.get("status") or "unknown")
        if status.get("connected") is True:
            if status.get("processing_failure_unrecovered") is True:
                return [
                    {
                        "component": "feishu",
                        "severity": "warning",
                        "code": "feishu_latest_event_processing_failed",
                        "message": "The latest Feishu websocket event failed during Veyra processing.",
                        "details": {
                            "status": raw_status,
                            "last_event_at": status.get("last_event_at"),
                            "error_type": status.get("error_type"),
                            "diagnostics": status.get("diagnostics"),
                        },
                    }
                ]
            if status.get("last_event_after_start") is False:
                return [
                    {
                        "component": "feishu",
                        "severity": "info",
                        "code": "feishu_no_current_run_event",
                        "message": "Feishu websocket is running but has not received an inbound event in this process.",
                        "details": {
                            "status": raw_status,
                            "thread_alive": status.get("thread_alive"),
                            "last_event_at": status.get("last_event_at"),
                            "started_at": status.get("started_at"),
                        },
                    }
                ]
            if status.get("last_processed_after_start") is False:
                return [
                    {
                        "component": "feishu",
                        "severity": "warning",
                        "code": "feishu_no_successful_current_run_processing",
                        "message": "Feishu received an event in this process, but no message has completed Veyra processing.",
                        "details": {
                            "status": raw_status,
                            "last_event_at": status.get("last_event_at"),
                        },
                    }
                ]
            if status.get("last_reply_sent_after_start") is False:
                return [
                    {
                        "component": "feishu",
                        "severity": "warning",
                        "code": "feishu_no_current_run_reply",
                        "message": "Feishu processed a message in this process, but no provider-sent reply is proven.",
                        "details": {
                            "status": raw_status,
                            "last_processed_at": status.get("last_processed_at"),
                        },
                    }
                ]
            return []
        if raw_status in {"not_configured", "stopped"}:
            severity = "warning"
        else:
            severity = "critical"
        return [
            {
                "component": "feishu",
                "severity": severity,
                "code": "feishu_not_ready",
                "message": f"Feishu websocket is {raw_status}.",
                "details": {
                    "status": raw_status,
                    "configured": status.get("configured"),
                    "thread_alive": status.get("thread_alive"),
                    "diagnostics": status.get("diagnostics"),
                },
            }
        ]

    def _active_loop_alerts(self) -> list[dict[str, Any]]:
        config = self.state_store.read_json("ops_config.json").get("active_loop", {})
        if not isinstance(config, dict):
            config = {}
        required = bool(config.get("health_required", True))
        if self.active_loop_status_resolver is None:
            return []
        try:
            status = self.active_loop_status_resolver()
        except Exception as exc:
            return [{"component": "active_loop", "severity": "critical", "code": "active_loop_status_error", "message": str(exc), "details": {}}]
        raw_status = str(status.get("status") or "stopped")
        alive = bool(status.get("thread_alive"))
        if raw_status in {"running", "already_running"} and alive:
            return []
        if not required:
            return [
                {
                    "component": "active_loop",
                    "severity": "info",
                    "code": "active_loop_not_required",
                    "message": "Active loop is not required by current ops config.",
                    "details": {"status": raw_status, "thread_alive": alive},
                }
            ]
        severity = "critical" if raw_status in {"stale", "stopped", "error"} else "warning"
        return [
            {
                "component": "active_loop",
                "severity": severity,
                "code": "active_loop_not_running",
                "message": f"Active runtime loop is {raw_status}.",
                "details": {"status": raw_status, "thread_alive": alive, "enabled": status.get("enabled")},
            }
        ]

    def _push_alerts(self) -> list[dict[str, Any]]:
        if self.active_loop_status_resolver is None:
            return []
        try:
            status = self.active_loop_status_resolver()
        except Exception:
            return []
        last_tick = status.get("last_tick") if isinstance(status.get("last_tick"), dict) else {}
        steps = last_tick.get("steps") if isinstance(last_tick.get("steps"), list) else []
        push_step = next((step for step in steps if isinstance(step, dict) and step.get("name") == "commitment_push"), None)
        if not push_step:
            return []
        result_status = str(push_step.get("result_status") or push_step.get("status") or "")
        if result_status in {"", "idle", "success", "ok", "not_configured"}:
            return []
        severity = "critical" if result_status in {"error", "timeout"} else "warning"
        result = push_step.get("result") if isinstance(push_step.get("result"), dict) else {}
        return [
            {
                "component": "push",
                "severity": severity,
                "code": "commitment_push_degraded",
                "message": f"Commitment push runtime reported {result_status} in the latest active-loop tick.",
                "details": {
                    "result_status": result_status,
                    "duration_ms": push_step.get("duration_ms"),
                    "processed_count": result.get("processed_count"),
                    "due_count": result.get("due_count"),
                },
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
        diagnostics = [self._review_diagnostic_item(item) for item in pending[-10:]]
        return [
            {
                "component": "review",
                "severity": "warning",
                "code": "pending_reviews",
                "message": f"{len(pending)} action review item(s) are pending.",
                "details": {"count": len(pending), "items": diagnostics},
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
                    "severity": "info",
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
        components = {
            name: "ok"
            for name in ["safety", "retention", "model", "agent", "feishu", "active_loop", "push", "tasks", "review", "belief", "heartbeat", "memory"]
        }
        for alert in alerts:
            component = str(alert.get("component") or "unknown")
            severity = str(alert.get("severity") or "info")
            if severity == "critical":
                components[component] = "critical"
            elif severity == "warning" and components.get(component) != "critical":
                components[component] = "warning"
            elif severity == "info" and components.get(component) == "ok":
                components[component] = "info"
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

    def _review_diagnostic_item(self, item: dict[str, Any]) -> dict[str, Any]:
        created_at = str(item.get("created_at") or "")
        task_text = str(item.get("task_text") or "")
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        review_type = str(proposal.get("type") or "action_review")
        if "openclaw" in task_text.lower() and ("重启" in task_text or "restart" in task_text.lower()):
            review_type = "service_restart_request"
        return {
            "review_id": item.get("review_id"),
            "type": review_type,
            "source": f"event:{item.get('event_id')}" if item.get("event_id") else "guardian_review_queue",
            "created_at": created_at,
            "age_seconds": self._age_seconds(created_at),
            "risk": item.get("risk_level"),
            "task_text": task_text[:240],
        }
