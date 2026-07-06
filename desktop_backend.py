from __future__ import annotations

import os
import sys
from pathlib import Path

from core.env_loader import load_env_file


def main() -> None:
    _configure_desktop_environment()
    import uvicorn
    import main as veyra_main

    host = os.getenv("VEYRA_HOST", "127.0.0.1")
    port = _int_env("VEYRA_PORT", 8000)
    uvicorn.run(veyra_main.app, host=host, port=port)


def _configure_desktop_environment() -> None:
    data_dir = Path(os.getenv("VEYRA_DESKTOP_DATA_DIR") or Path.cwd()).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(data_dir)

    env_file = Path(os.getenv("VEYRA_ENV_FILE") or data_dir / ".env").expanduser()
    _ensure_env_file(env_file)
    _load_env_file(env_file)

    state_root = data_dir / "state"
    agency_root = data_dir / "agency"
    log_dir = data_dir / "logs"
    for path in (state_root, agency_root, log_dir):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("VEYRA_DESKTOP", "1")
    os.environ.setdefault("VEYRA_HOST", "127.0.0.1")
    os.environ.setdefault("VEYRA_PORT", "8000")
    os.environ.setdefault("VEYRA_ENV_FILE", str(env_file))
    os.environ.setdefault("VEYRA_STATE_ROOT", str(state_root))
    os.environ.setdefault("VEYRA_AGENCY_ROOT", str(agency_root))
    os.environ.setdefault("VEYRA_APP_LOG", str(log_dir / "veyra_backend.log"))
    os.environ.setdefault("OPENCLAW_DEVICE_STORE", str(state_root / "local" / "openclaw_device.json"))
    os.environ.setdefault("VEYRA_OPENCLAW_MEMORY_MIRROR_DIR", str(state_root / "local" / "openclaw_memory_mirror"))


def _ensure_env_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_default_env_text(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _default_env_text() -> str:
    for candidate in _default_env_candidates():
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return """# Veyra local-first configuration.
VEYRA_STATE_ROOT=state
VEYRA_AGENCY_ROOT=agency
VEYRA_ACTION_RECORD_RETENTION_LIMIT=10000
VEYRA_HOST=127.0.0.1
VEYRA_PORT=8000
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


def _default_env_candidates() -> list[Path]:
    candidates = [Path.cwd() / ".env.example", Path(__file__).resolve().parent / ".env.example"]
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.append(Path(str(bundle_root)) / ".env.example")
    return candidates


def _load_env_file(path: Path) -> None:
    load_env_file(path)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


if __name__ == "__main__":
    main()
