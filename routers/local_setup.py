from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from interface.event_schema import utc_now_iso


ALLOWED_ENV_KEYS = {
    "VEYRA_STATE_ROOT",
    "VEYRA_AGENCY_ROOT",
    "VEYRA_ACTION_RECORD_RETENTION_LIMIT",
    "VEYRA_HOST",
    "VEYRA_PORT",
    "VEYRA_LOCAL_API_TOKEN",
    "VEYRA_GITHUB_TOKEN",
    "VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS",
    "VEYRA_OPENCLAW_VERSION",
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
SENSITIVE_PATTERNS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK")
ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class SetupEnvRequest(BaseModel):
    values: dict[str, str | int | bool | None] = Field(default_factory=dict)


class SetupCompleteRequest(BaseModel):
    skipped_openclaw: bool = False
    skipped_feishu: bool = False
    skipped_core_model: bool = False
    notes: str = ""


class SetupOpenClawInstallRequest(BaseModel):
    start_gateway: bool = False


DEFAULT_OPENCLAW_VERSION = "2026.6.11"


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
                "state_writer_lock": "validated_posix" if platform.system() != "Windows" else "validation_pending",
                "backend_mode": "desktop_sidecar" if os.getenv("VEYRA_DESKTOP") else "local_api",
                "api_base_url": f"http://{os.getenv('VEYRA_HOST', '127.0.0.1')}:{os.getenv('VEYRA_PORT', '8000')}",
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "paths": {
                "env_file": _public_local_path(env_path),
                "env_exists": env_path.exists(),
                "state_root": _public_local_path(Path(getattr(state_store, "root", Path("state"))).resolve()),
            },
            "env": _redacted_env_file(env_path),
        }
        agent_status = _safe_call(deps.get("agent_status"), fallback={"status": "unknown"})
        status["agent"] = agent_status
        feishu_status = _safe_call(deps.get("feishu_status"), fallback={"status": "unknown"})
        status["feishu"] = feishu_status
        status["feishu_setup"] = _feishu_diagnostics(feishu_status if isinstance(feishu_status, dict) else {})
        status["deployment"] = _safe_call(deps.get("deployment_readiness"), fallback={"status": "unknown"})
        status["openclaw"] = _openclaw_diagnostics(agent_status if isinstance(agent_status, dict) else {})
        status["wizard"] = _wizard_status(state_store, env_path, agent_status if isinstance(agent_status, dict) else {})
        return status

    @router.get("/setup/openclaw")
    async def setup_openclaw() -> dict[str, Any]:
        agent_status = _safe_call(deps.get("agent_status"), fallback={"status": "unknown"})
        return _openclaw_diagnostics(agent_status if isinstance(agent_status, dict) else {})

    @router.post("/setup/openclaw/install")
    async def setup_openclaw_install(request: SetupOpenClawInstallRequest, fastapi_request: Request) -> dict[str, Any]:
        _require_local_client(fastapi_request)
        npm = shutil.which("npm")
        if not npm:
            raise HTTPException(
                status_code=422,
                detail="npm is required to install OpenClaw. Install Node.js from https://nodejs.org/ and retry.",
            )
        openclaw_version = str(os.getenv("VEYRA_OPENCLAW_VERSION") or DEFAULT_OPENCLAW_VERSION).strip()
        if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,63}", openclaw_version):
            raise HTTPException(status_code=422, detail="VEYRA_OPENCLAW_VERSION must be an explicit npm version.")
        install = await asyncio.to_thread(
            subprocess.run,
            [npm, "install", "-g", f"openclaw@{openclaw_version}"],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if install.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "status": "install_failed",
                    "message": (install.stderr or install.stdout or "npm install failed").strip()[-800:],
                },
            )
        start_result: dict[str, Any] | None = None
        if request.start_gateway:
            start_result = await asyncio.to_thread(_start_openclaw_gateway)
        agent_status = _safe_call(deps.get("agent_status"), fallback={"status": "unknown"})
        diagnostics = _openclaw_diagnostics(agent_status if isinstance(agent_status, dict) else {})
        return {
            "status": "installed",
            "version": openclaw_version,
            "install_output_tail": (install.stdout or install.stderr or "").strip()[-800:],
            "start_gateway": start_result,
            "openclaw": diagnostics,
        }

    @router.post("/setup/complete")
    async def setup_complete(request: SetupCompleteRequest, fastapi_request: Request) -> dict[str, Any]:
        _require_local_client(fastapi_request)
        state_store = deps["state_store"]
        payload = {
            "completed": True,
            "completed_at": utc_now_iso(),
            "skipped_openclaw": request.skipped_openclaw,
            "skipped_feishu": request.skipped_feishu,
            "skipped_core_model": request.skipped_core_model,
            "notes": request.notes.strip(),
            "version": 1,
        }
        _write_wizard_state(state_store, payload)
        return {"status": "completed", "wizard": payload}

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
            "env_file": _public_local_path(env_path),
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
    return """# Veyra local configuration
VEYRA_STATE_ROOT=state
VEYRA_AGENCY_ROOT=agency
VEYRA_ACTION_RECORD_RETENTION_LIMIT=10000
VEYRA_HOST=127.0.0.1
VEYRA_PORT=8000
VEYRA_LOCAL_API_TOKEN=
VEYRA_GITHUB_TOKEN=
VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS=0
VEYRA_OPENCLAW_VERSION=2026.6.11
VEYRA_ACTIVE_LOOP_AUTOSTART=1
VEYRA_FEISHU_WS_AUTOSTART=1
VEYRA_CORE_MODEL_ENABLED=0
VEYRA_CORE_MODEL_PROVIDER=openai_compatible
VEYRA_CORE_MODEL_BASE_URL=
VEYRA_CORE_MODEL=
VEYRA_CORE_MODEL_API_KEY_ENV=VEYRA_CORE_MODEL_API_KEY
VEYRA_CORE_MODEL_API_KEY=
VEYRA_CORE_MODEL_DECISION_MODE=auto
VEYRA_CORE_MODEL_MAX_TOKENS=700
VEYRA_SELECTED_AGENT=openclaw
OPENCLAW_BASE_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=
OPENCLAW_GATEWAY_PASSWORD=
OPENCLAW_SCOPES=operator.read,operator.write
OPENCLAW_MEMORY_SCOPES=operator.read,operator.write,operator.admin
VEYRA_OPENCLAW_USE_LOCAL_CONFIG=1
VEYRA_OPENCLAW_WORKSPACE_MEMORY_FALLBACK=1
HERMES_BASE_URL=
HERMES_API_KEY=
CUSTOM_AGENT_BASE_URL=
CUSTOM_AGENT_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_TENANT_ACCESS_TOKEN=
FEISHU_DEFAULT_RECEIVE_ID=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
VEYRA_CHANNEL_WEBHOOK_URL=
VEYRA_ALERT_WEBHOOK_URL=
VEYRA_TOOL_PROXY_BROWSER_ENABLED=0
VEYRA_TOOL_PROXY_BROWSER_ALLOWED_HOSTS=localhost,127.0.0.1,::1
VEYRA_TOOL_PROXY_API_ENABLED=0
VEYRA_TOOL_PROXY_API_ALLOWED_HOSTS=localhost,127.0.0.1,::1
VEYRA_SEARCH_PROVIDER=auto
VEYRA_YOUTUBE_CHANNEL_MAP=
VEYRA_APP_LOG=
VEYRA_AGENT_RESTART_CMD=
"""


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
    return str(value).replace("\n", "").replace("\r", "").strip()


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


def _public_local_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return resolved.name


def _read_wizard_state(state_store: Any) -> dict[str, Any]:
    try:
        data = state_store.read_json("setup_wizard.json")
        return data if isinstance(data, dict) else {"completed": False}
    except (AttributeError, OSError, ValueError):
        return {"completed": False}


def _write_wizard_state(state_store: Any, payload: dict[str, Any]) -> None:
    state_store.mutate_json("setup_wizard.json", lambda current: {**current, **payload})


def _wizard_status(state_store: Any, env_path: Path, agent_status: dict[str, Any]) -> dict[str, Any]:
    saved = _read_wizard_state(state_store)
    completed = bool(saved.get("completed"))
    env_values = _redacted_env_file(env_path).get("values", {})
    core_enabled = str(env_values.get("VEYRA_CORE_MODEL_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    agent_connected = bool(agent_status.get("connected"))
    checklist = {
        "env_file": env_path.exists(),
        "core_model": core_enabled,
        "agent_connected": agent_connected,
        "openclaw_port": _port_listening(18789),
    }
    return {
        "completed": completed,
        "should_show": not completed,
        "completed_at": saved.get("completed_at"),
        "skipped_openclaw": bool(saved.get("skipped_openclaw")),
        "skipped_core_model": bool(saved.get("skipped_core_model")),
        "checklist": checklist,
    }


def _openclaw_diagnostics(agent_status: dict[str, Any]) -> dict[str, Any]:
    executable = _find_openclaw_executable()
    npm = shutil.which("npm")
    port_listening = _port_listening(18789)
    connected = bool(agent_status.get("connected"))
    openclaw_version = str(os.getenv("VEYRA_OPENCLAW_VERSION") or DEFAULT_OPENCLAW_VERSION).strip()
    return {
        "installed": bool(executable),
        "executable": Path(executable).name if executable else None,
        "npm_available": bool(npm),
        "port": 18789,
        "port_listening": port_listening,
        "connected": connected,
        "agent_status": str(agent_status.get("status") or "unknown"),
        "base_url": str(agent_status.get("base_url") or os.getenv("OPENCLAW_BASE_URL") or "http://127.0.0.1:18789"),
        "control_ui_url": "http://127.0.0.1:18789",
        "install_version": openclaw_version,
        "install_command": f"npm install -g openclaw@{openclaw_version}",
        "start_command": "openclaw gateway",
        "docs_hint": "After install, run `openclaw gateway`, open the control UI, copy the gateway token if required, then set OPENCLAW_GATEWAY_TOKEN in Veyra.",
        "ready": bool(connected),
        "needs_gateway": bool(port_listening and not connected),
        "needs_install": not bool(executable),
        "needs_start": bool(executable and not port_listening),
    }


def _feishu_diagnostics(feishu_status: dict[str, Any]) -> dict[str, Any]:
    config = feishu_status.get("channel_config") if isinstance(feishu_status.get("channel_config"), dict) else {}
    configured = bool(feishu_status.get("configured"))
    thread_alive = bool(feishu_status.get("thread_alive"))
    last_event_after_start = bool(feishu_status.get("last_event_after_start"))
    status = str(feishu_status.get("status") or "unknown")
    if configured and thread_alive and last_event_after_start:
        readiness = "receiving"
    elif configured and thread_alive:
        readiness = "waiting_for_event"
    elif configured:
        readiness = "configured_not_running"
    else:
        readiness = "not_configured"
    return {
        "configured": configured,
        "enabled": bool(config.get("enabled")),
        "connection_mode": str(config.get("connection_mode") or "callback"),
        "status": status,
        "thread_alive": thread_alive,
        "last_event_after_start": last_event_after_start,
        "last_event_at": feishu_status.get("last_event_at"),
        "readiness": readiness,
        "app_id_set": bool(config.get("app_id")),
        "app_secret_set": bool(config.get("app_secret")),
        "default_receive_id_set": bool(config.get("default_receive_id")),
        "diagnostics": feishu_status.get("diagnostics") if isinstance(feishu_status.get("diagnostics"), list) else [],
    }


def _find_openclaw_executable() -> str:
    found = shutil.which("openclaw")
    if found:
        return found
    candidates = (
        Path.home() / ".npm-global" / "bin" / "openclaw",
        Path.home() / ".local" / "bin" / "openclaw",
        Path("/opt/homebrew/bin/openclaw"),
        Path("/usr/local/bin/openclaw"),
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return ""


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


def _start_openclaw_gateway() -> dict[str, Any]:
    executable = _find_openclaw_executable()
    if not executable:
        return {"status": "skipped", "reason": "openclaw executable not found after install"}
    if _port_listening(18789):
        return {"status": "already_running", "port": 18789}
    try:
        process = subprocess.Popen(
            [executable, "gateway"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {"status": "error", "message": str(exc)}
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if _port_listening(18789):
            return {"status": "started", "pid": process.pid, "port": 18789}
        time.sleep(0.2)
    return {"status": "starting", "pid": process.pid, "port": 18789, "message": "Gateway process launched; port not ready yet."}
