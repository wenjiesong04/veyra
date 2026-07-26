#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from os import chdir
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.local_setup import (  # noqa: E402
    _default_env_text,
    _merge_env_text,
    _openclaw_diagnostics,
    _port_listening,
    _redact_env_value,
    _validate_env_updates,
    _wizard_status,
    _write_wizard_state,
)
from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")
    print(f"ok - {message}")


def main() -> int:
    original = "\n".join(
        [
            "# Veyra local config",
            "VEYRA_CORE_MODEL_ENABLED=0",
            "FEISHU_APP_SECRET=old-secret",
            "UNRELATED=value",
        ]
    )
    updates = {
        "VEYRA_CORE_MODEL_ENABLED": True,
        "FEISHU_APP_SECRET": "new-secret",
        "OPENCLAW_BASE_URL": "http://127.0.0.1:18789",
        "VEYRA_GITHUB_TOKEN": "github-read-token",
    }
    merged = _merge_env_text(original, updates)
    expect("VEYRA_CORE_MODEL_ENABLED=1" in merged, "boolean env values are normalized")
    expect("FEISHU_APP_SECRET=new-secret" in merged, "existing secret value is replaced")
    expect("OPENCLAW_BASE_URL=http://127.0.0.1:18789" in merged, "new allowed key is appended")
    expect("UNRELATED=value" in merged, "unmanaged env keys are preserved")
    injected = _merge_env_text("VEYRA_CORE_MODEL=old\n", {"VEYRA_CORE_MODEL": "safe\rINJECTED=1"})
    expect("\nINJECTED=1" not in injected, "carriage returns cannot inject additional env lines", injected)
    expect(_redact_env_value("FEISHU_APP_SECRET", "new-secret") == "***", "secrets are redacted")
    expect(_redact_env_value("VEYRA_GITHUB_TOKEN", "github-read-token") == "***", "GitHub token is redacted")
    expect(_redact_env_value("VEYRA_ALERT_WEBHOOK_URL", "https://secret.example/hook") == "***", "webhook credentials are redacted")
    expect(_redact_env_value("OPENCLAW_BASE_URL", "http://127.0.0.1:18789").startswith("http"), "non-secret values remain visible")
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            chdir(tmpdir)
            fallback = _default_env_text()
        finally:
            chdir(cwd)
    expect("VEYRA_CORE_MODEL_ENABLED=0" in fallback, "fallback env template is available without .env.example")
    expect("OPENCLAW_BASE_URL=http://127.0.0.1:18789" in fallback, "fallback env template includes default OpenClaw URL")
    expect("VEYRA_GITHUB_TOKEN=" in fallback, "fallback env template includes optional GitHub token")

    try:
        _validate_env_updates({"PATH": "/tmp"})
    except HTTPException as exc:
        expect(exc.status_code == 422, "unsupported env keys are rejected")
    else:
        raise AssertionError("unsupported env key was accepted")

    with tempfile.TemporaryDirectory() as tmpdir:
        isolated_state = Path(tmpdir) / "state"
        isolated_env = Path(tmpdir) / ".env"
        store = WorldStateStore(isolated_state)
        wizard = _wizard_status(store, isolated_env, {"connected": False, "status": "unavailable"})
        expect(wizard["should_show"] is True, "wizard should show before completion")
        expect(wizard["checklist"]["env_file"] is False, "missing env is reflected in checklist")
        _write_wizard_state(store, {"completed": True, "completed_at": "2026-07-24T00:00:00+00:00"})
        saved = store.read_json("setup_wizard.json")
        expect(saved.get("completed") is True and saved.get("_state_revision"), "wizard state uses atomic versioned WorldState storage", saved)

    openclaw = _openclaw_diagnostics({"connected": False, "status": "unavailable", "base_url": "http://127.0.0.1:18789"})
    expect("install_command" in openclaw, "openclaw diagnostics include install command")
    expect(openclaw["install_command"] == "npm install -g openclaw@2026.6.11", "openclaw install uses an explicit pinned version")
    expect(openclaw["port"] == 18789, "openclaw default port is exposed")

    print("local_setup_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
