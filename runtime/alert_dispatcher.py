from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from runtime.ops_monitor import OpsMonitor


SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


class AlertDispatcher:
    """Dispatches operational alerts to local audit log and optional webhook."""

    def __init__(self, state_store: WorldStateStore, ops_monitor: OpsMonitor) -> None:
        self.state_store = state_store
        self.ops_monitor = ops_monitor

    def status(self) -> dict[str, Any]:
        config = self._config()
        webhook_url = self._webhook_url(config)
        return {
            "enabled": bool(config.get("enabled", True)),
            "local_log": bool(config.get("local_log", True)),
            "webhook_enabled": bool(config.get("webhook_enabled", False)),
            "webhook_configured": bool(webhook_url),
            "webhook_url_env": config.get("webhook_url_env", "VEYRA_ALERT_WEBHOOK_URL"),
            "min_severity": config.get("min_severity", "warning"),
            "validation": {
                "local_log": "validated" if bool(config.get("local_log", True)) else "disabled",
                "webhook": "validated" if bool(config.get("webhook_enabled", False) and webhook_url) else "not_configured",
            },
            "recent": self.state_store.read_jsonl("alert_log.jsonl", limit=20),
        }

    def configure(self, patch: dict[str, Any]) -> dict[str, Any]:
        def configure_alerting(config: dict[str, Any]) -> None:
            alerting = config.setdefault("alerting", {})
            if not isinstance(alerting, dict):
                config["alerting"] = alerting = {}
            for key in ("enabled", "local_log", "webhook_enabled", "webhook_url", "webhook_url_env", "min_severity"):
                if key in patch and patch[key] is not None:
                    alerting[key] = patch[key]
            if alerting.get("min_severity") not in SEVERITY_ORDER:
                alerting["min_severity"] = "warning"

        self.state_store.mutate_json("ops_config.json", configure_alerting)
        return self.status()

    def dispatch(self, min_severity: str | None = None) -> dict[str, Any]:
        config = self._config()
        if not config.get("enabled", True):
            return {"status": "disabled", "dispatched": [], "external": {"status": "disabled"}}
        threshold = min_severity or str(config.get("min_severity") or "warning")
        alerts = [
            alert
            for alert in self.ops_monitor.alerts()["items"]
            if SEVERITY_ORDER.get(str(alert.get("severity") or "info"), 0) >= SEVERITY_ORDER.get(threshold, 1)
        ]
        dispatch_id = f"alert_{uuid4().hex[:12]}"
        payload = {
            "dispatch_id": dispatch_id,
            "status": "no_alerts" if not alerts else "dispatching",
            "threshold": threshold,
            "alerts": redact_sensitive(alerts, max_string=1600),
            "summary": self._summary(alerts),
            "created_at": utc_now_iso(),
        }
        if config.get("local_log", True):
            self.state_store.append_jsonl("alert_log.jsonl", payload)
        external = self._send_webhook(config, payload) if alerts else {"status": "skipped", "reason": "no_alerts"}
        result_status = "success" if external.get("status") in {"sent", "not_configured", "disabled", "skipped"} else "partial"
        result = {
            **payload,
            "status": result_status,
            "external": external,
            "validation": {
                "local_audit_recorded": bool(config.get("local_log", True)),
                "external_delivery": external.get("status"),
                "status": "validated" if external.get("status") == "sent" else "not_configured" if external.get("status") in {"disabled", "not_configured", "skipped"} else "validation_pending",
            },
        }
        self.state_store.append_jsonl("action_record.jsonl", {"route": "ops_alert_dispatch", "status": result_status, "artifacts": result})
        return result

    def _send_webhook(self, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if not config.get("webhook_enabled", False):
            return {"status": "disabled"}
        url = self._webhook_url(config)
        if not url:
            return {"status": "not_configured"}
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "sent", "status_code": getattr(response, "status", None), "response": body[:500]}

    def _webhook_url(self, config: dict[str, Any]) -> str:
        env_name = str(config.get("webhook_url_env") or "VEYRA_ALERT_WEBHOOK_URL")
        return str(config.get("webhook_url") or os.getenv(env_name, ""))

    def _config(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        alerting = config.get("alerting") if isinstance(config.get("alerting"), dict) else {}
        return {
            "enabled": True,
            "local_log": True,
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_url_env": "VEYRA_ALERT_WEBHOOK_URL",
            "min_severity": "warning",
            **alerting,
        }

    def _summary(self, alerts: list[dict[str, Any]]) -> dict[str, int]:
        summary = {"critical": 0, "warning": 0, "info": 0, "total": len(alerts)}
        for alert in alerts:
            severity = str(alert.get("severity") or "info")
            if severity in summary:
                summary[severity] += 1
        return summary
