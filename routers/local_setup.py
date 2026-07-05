from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


ALLOWED_ENV_KEYS = {
    "VEYRA_STATE_ROOT",
    "VEYRA_AGENCY_ROOT",
    "VEYRA_HOST",
    "VEYRA_PORT",
    "VEYRA_ACTIVE_LOOP_AUTOSTART",
    "VEYRA_FEISHU_WS_AUTOSTART",
    "VEYRA_CORE_MODEL_ENABLED",
    "VEYRA_CORE_MODEL_PROVIDER",
    "VEYRA_CORE_MODEL_BASE_URL",
    "VEYRA_CORE_MODEL",
    "VEYRA_CORE_MODEL_API_KEY_ENV",
    "VEYRA_CORE_MODEL_API_KEY",
    "VEYRA_CORE_MODEL_DECISION_MODE",
    "VEYRA_CORE_MODEL_MAX_TOKENS",
    "VEYRA_SELECTED_AGENT",
    "OPENCLAW_BASE_URL",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_GATEWAY_PASSWORD",
    "OPENCLAW_SCOPES",
    "OPENCLAW_MEMORY_SCOPES",
    "VEYRA_OPENCLAW_USE_LOCAL_CONFIG",
    "VEYRA_OPENCLAW_WORKSPACE_MEMORY_FALLBACK",
    "HERMES_BASE_URL",
    "HERMES_API_KEY",
    "CUSTOM_AGENT_BASE_URL",
    "CUSTOM_AGENT_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_TENANT_ACCESS_TOKEN",
    "FEISHU_DEFAULT_RECEIVE_ID",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "VEYRA_CHANNEL_WEBHOOK_URL",
    "VEYRA_ALERT_WEBHOOK_URL",
    "VEYRA_TOOL_PROXY_BROWSER_ENABLED",
    "VEYRA_TOOL_PROXY_BROWSER_ALLOWED_HOSTS",
    "VEYRA_TOOL_PROXY_API_ENABLED",
    "VEYRA_TOOL_PROXY_API_ALLOWED_HOSTS",
    "VEYRA_SEARCH_PROVIDER",
    "VEYRA_YOUTUBE_CHANNEL_MAP",
    "VEYRA_APP_LOG",
    "VEYRA_AGENT_RESTART_CMD",
}

LOCAL_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
SENSITIVE_PATTERNS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class SetupEnvRequest(BaseModel):
    values: dict[str, str | int | bool | None] = Field(default_factory=dict)


def build_local_setup_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/setup/status")
    async def setup_status() -> dict[str, Any]:
        state_store = deps["state_store"]
        env_path = Path(".env").resolve()
        status: dict[str, Any] = {
            "app_name": "Veyra",
            "desktop": {
                "shell": "tauri",
                "product_name": "Veyra",
                "supported_platforms": ["macos", "windows", "linux"],
                "backend_mode": "local_api",
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "paths": {
                "env_file": str(env_path),
                "env_exists": env_path.exists(),
                "state_root": str(getattr(state_store, "root", Path("state")).resolve()),
            },
            "env": _redacted_env_file(env_path),
        }
        status["agent"] = _safe_call(deps.get("agent_status"), fallback={"status": "unknown"})
        status["feishu"] = _safe_call(deps.get("feishu_status"), fallback={"status": "unknown"})
        status["deployment"] = _safe_call(deps.get("deployment_readiness"), fallback={"status": "unknown"})
        return status

    @router.post("/setup/env")
    async def setup_env(request: SetupEnvRequest, fastapi_request: Request) -> dict[str, Any]:
        _require_local_client(fastapi_request)
        updates = _validate_env_updates(request.values)
        env_path = Path(".env").resolve()
        current = env_path.read_text(encoding="utf-8") if env_path.exists() else _default_env_text()
        env_path.write_text(_merge_env_text(current, updates), encoding="utf-8")
        _chmod_private(env_path)
        for key, value in updates.items():
            os.environ[key] = _format_env_value(value)
        return {
            "status": "saved",
            "env_file": str(env_path),
            "updated_keys": sorted(updates),
            "env": _redacted_env_file(env_path),
        }

    return router


def _safe_call(callable_or_value: Any, *, fallback: dict[str, Any]) -> Any:
    try:
        if callable(callable_or_value):
            return callable_or_value()
        return callable_or_value if callable_or_value is not None else fallback
    except Exception as exc:  # pragma: no cover - defensive status surface
        return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}


def _require_local_client(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in LOCAL_CLIENT_HOSTS:
        raise HTTPException(status_code=403, detail="setup writes are only allowed from the local machine")


def _validate_env_updates(values: dict[str, str | int | bool | None]) -> dict[str, str | int | bool | None]:
    rejected = sorted(key for key in values if key not in ALLOWED_ENV_KEYS)
    if rejected:
        raise HTTPException(status_code=422, detail={"unsupported_keys": rejected})
    return {key: value for key, value in values.items() if key in ALLOWED_ENV_KEYS}


def _default_env_text() -> str:
    example = Path(".env.example")
    if example.exists():
        return example.read_text(encoding="utf-8")
    return "# Veyra local configuration\n"


def _merge_env_text(text: str, updates: dict[str, str | int | bool | None]) -> str:
    remaining = dict(updates)
    merged: list[str] = []
    for line in text.splitlines():
        match = ENV_LINE_RE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            merged.append(f"{key}={_format_env_value(remaining.pop(key))}")
        else:
            merged.append(line)
    if remaining:
        if merged and merged[-1].strip():
            merged.append("")
        merged.append("# Values saved by the Veyra local setup UI")
        for key in sorted(remaining):
            merged.append(f"{key}={_format_env_value(remaining[key])}")
    return "\n".join(merged).rstrip() + "\n"


def _format_env_value(value: str | int | bool | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).replace("\n", "").strip()


def _redacted_env_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "values": {}}
    values: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key in ALLOWED_ENV_KEYS:
            values[key] = _redact_env_value(key, value)
    return {"exists": True, "values": values}


def _redact_env_value(key: str, value: str) -> str:
    if not value:
        return ""
    if any(pattern in key for pattern in SENSITIVE_PATTERNS):
        return "***"
    return value


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
