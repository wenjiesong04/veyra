from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_runtime_env() -> dict[str, Any]:
    """Load Veyra runtime env once, without overriding process env values."""

    env_file = _runtime_env_file()
    loaded = load_env_file(env_file) if env_file else {"loaded": False, "path": "", "keys": []}
    if loaded.get("path"):
        os.environ.setdefault("VEYRA_ACTIVE_ENV_FILE", str(loaded["path"]))
    os.environ.setdefault("VEYRA_ACTIVE_ENV_LOADED", "1" if loaded.get("loaded") else "0")
    return loaded


def load_env_file(path: str | Path) -> dict[str, Any]:
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists() or not env_path.is_file():
        return {"loaded": False, "path": str(env_path), "keys": [], "reason": "missing"}

    loaded_keys: list[str] = []
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"loaded": False, "path": str(env_path), "keys": [], "reason": str(exc)}

    for line in lines:
        key, value = parse_env_line(line)
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded_keys.append(key)
    os.environ["VEYRA_ACTIVE_ENV_FILE"] = str(env_path)
    os.environ["VEYRA_ACTIVE_ENV_LOADED_KEYS"] = str(len(loaded_keys))
    return {"loaded": True, "path": str(env_path), "keys": loaded_keys}


def runtime_env_status() -> dict[str, Any]:
    path = os.getenv("VEYRA_ACTIVE_ENV_FILE") or str(_runtime_env_file() or "")
    return {
        "active_env_file": path,
        "loaded": os.getenv("VEYRA_ACTIVE_ENV_LOADED", ""),
        "loaded_key_count": os.getenv("VEYRA_ACTIVE_ENV_LOADED_KEYS", ""),
    }


def parse_env_line(line: str) -> tuple[str, str]:
    text = line.strip()
    if not text or text.startswith("#"):
        return "", ""
    if text.startswith("export "):
        text = text[7:].strip()
    if "=" not in text:
        return "", ""
    key, value = text.split("=", 1)
    key = key.strip()
    if not _ENV_KEY_RE.match(key):
        return "", ""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def _runtime_env_file() -> Path | None:
    configured = os.getenv("VEYRA_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    repo_env = Path(__file__).resolve().parents[1] / ".env"
    if repo_env.exists():
        return repo_env
    return None
