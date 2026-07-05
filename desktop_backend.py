from __future__ import annotations

import os
from pathlib import Path


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


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _clean_env_value(value.strip())


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


if __name__ == "__main__":
    main()
