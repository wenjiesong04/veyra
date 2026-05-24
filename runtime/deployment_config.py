from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class DeploymentConfigValidator:
    """Validates local deployment configuration without contacting external services."""

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def validate(self) -> dict[str, Any]:
        agent_config = self.state_store.read_json("agent_config.json")
        ops_config = self.state_store.read_json("ops_config.json")
        checks = [
            *self._agent_checks(agent_config),
            *self._core_model_checks(agent_config),
            *self._alerting_checks(ops_config),
            *self._tool_proxy_checks(ops_config),
            *self._state_checks(),
        ]
        failed = [item for item in checks if item["status"] == "fail"]
        warnings = [item for item in checks if item["status"] == "warn"]
        status = "not_ready" if failed else "ready_with_warnings" if warnings else "ready"
        return {
            "status": status,
            "checked_at": utc_now_iso(),
            "summary": {"fail": len(failed), "warn": len(warnings), "pass": len([item for item in checks if item["status"] == "pass"])},
            "checks": checks,
        }

    def _agent_checks(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        selected = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected_config = agents.get(selected) if isinstance(agents.get(selected), dict) else {}
        checks = [
            self._check(
                "selected_agent_configured",
                "pass" if bool(selected_config) else "fail",
                f"Selected agent is {selected}.",
                {"selected_agent": selected},
            )
        ]
        base_url = str(selected_config.get("base_url") or "")
        checks.append(
            self._check(
                "selected_agent_base_url",
                "pass" if self._valid_http_like(base_url) else "fail",
                "Selected agent has a valid base_url." if base_url else "Selected agent base_url is empty.",
                {"selected_agent": selected, "base_url_configured": bool(base_url)},
            )
        )
        for name, agent in agents.items():
            if not isinstance(agent, dict) or not agent.get("enabled", True):
                continue
            url = str(agent.get("base_url") or "")
            status = "pass" if not url or self._valid_http_like(url) else "warn"
            checks.append(
                self._check(
                    f"agent_{name}_url_shape",
                    status,
                    f"{name} adapter URL shape checked.",
                    {"enabled": bool(agent.get("enabled", True)), "base_url_configured": bool(url), "kind": agent.get("kind")},
                )
            )
        return checks

    def _core_model_checks(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        core_model = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
        enabled = bool(core_model.get("enabled"))
        if not enabled:
            return [self._check("core_model_enabled", "warn", "Core model is disabled; Veyra will remain mostly rule-driven.", {})]
        base_url = str(core_model.get("base_url") or "")
        model = str(core_model.get("model") or "")
        api_key_env = str(core_model.get("api_key_env") or "")
        api_key = str(core_model.get("api_key") or "")
        return [
            self._check("core_model_base_url", "pass" if self._valid_http_like(base_url) else "fail", "Core model base_url checked.", {"configured": bool(base_url)}),
            self._check("core_model_name", "pass" if bool(model) else "fail", "Core model name checked.", {"configured": bool(model)}),
            self._check(
                "core_model_secret_source",
                "pass" if api_key or (api_key_env and os.getenv(api_key_env)) else "warn",
                "Core model API key source checked.",
                {"api_key_env": api_key_env, "api_key_env_set": bool(api_key_env and os.getenv(api_key_env)), "inline_api_key_set": bool(api_key)},
            ),
            self._check("core_model_inline_secret", "warn" if api_key else "pass", "Inline API keys are discouraged for deployment.", {"inline_api_key_set": bool(api_key)}),
        ]

    def _alerting_checks(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        alerting = config.get("alerting") if isinstance(config.get("alerting"), dict) else {}
        local_log = bool(alerting.get("local_log", True))
        webhook_enabled = bool(alerting.get("webhook_enabled", False))
        webhook_url = str(alerting.get("webhook_url") or "")
        webhook_env = str(alerting.get("webhook_url_env") or "VEYRA_ALERT_WEBHOOK_URL")
        webhook_configured = bool(webhook_url or os.getenv(webhook_env))
        return [
            self._check("alert_delivery_available", "pass" if local_log or webhook_enabled else "fail", "At least one alert delivery path is enabled.", {"local_log": local_log, "webhook_enabled": webhook_enabled}),
            self._check(
                "alert_webhook_configured",
                "pass" if not webhook_enabled or webhook_configured else "fail",
                "Webhook configuration checked.",
                {"webhook_enabled": webhook_enabled, "webhook_url_env": webhook_env, "webhook_configured": webhook_configured},
            ),
        ]

    def _tool_proxy_checks(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        tool_proxy = config.get("tool_proxy") if isinstance(config.get("tool_proxy"), dict) else {}
        browser_enabled = bool(tool_proxy.get("browser_executor_enabled", False))
        api_enabled = bool(tool_proxy.get("api_executor_enabled", False))
        checks = [
            self._check("tool_proxy_default_boundary", "pass", "SafeShell and SafeFile are always routed through Tool Proxy.", {}),
            self._check(
                "browser_executor_explicit",
                "warn" if browser_enabled else "pass",
                "Browser executor is optional and should have deployment-specific allowlists before production use.",
                {"enabled": browser_enabled},
            ),
            self._check(
                "api_executor_explicit",
                "warn" if api_enabled else "pass",
                "API executor is optional and should have deployment-specific allowlists before production use.",
                {"enabled": api_enabled},
            ),
        ]
        return checks

    def _state_checks(self) -> list[dict[str, Any]]:
        root = self.state_store.root
        writable = os.access(root, os.W_OK)
        return [
            self._check("state_root_exists", "pass" if root.exists() else "fail", "State root exists.", {"path": str(root)}),
            self._check("state_root_writable", "pass" if writable else "fail", "State root is writable.", {"path": str(root)}),
        ]

    def _check(self, name: str, status: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "status": status, "passed": status == "pass", "message": message, "details": redact_sensitive(details, max_string=800)}

    def _valid_http_like(self, value: str) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https", "ws", "wss"} and parsed.netloc:
            return True
        path = Path(value)
        return path.is_absolute()
