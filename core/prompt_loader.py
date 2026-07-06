from __future__ import annotations

from pathlib import Path


_CACHE: dict[str, str] = {}


def load_prompt(relative_path: str, fallback: str) -> str:
    if relative_path in _CACHE:
        return _CACHE[relative_path]
    path = Path(__file__).resolve().parents[1] / "prompts" / relative_path
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    value = text or fallback
    _CACHE[relative_path] = value
    return value
