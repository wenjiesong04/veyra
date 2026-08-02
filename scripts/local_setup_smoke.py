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
    _feishu_diagnostics,
    _merge_env_text,
    _openclaw_diagnostics,
    _port_listening,
    _redact_env_value,
    _validate_env_updates,
    _wizard_status,
    _write_wizard_state,
)
from core.world_state import WorldStateStore  # noqa: E402
from interface.feishu_ws_runner import FeishuWsRunner  # noqa: E402


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")
    print(f"ok - {message}")


class MemoryStateStore:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = dict(state)

    def read_json(self, name: str) -> dict[str, object]:
        if name == "channel_state.json":
            return {
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "app_id": "test-app",
                        "app_secret": "test-secret",
                    }
                }
            }
        return dict(self.state)

    def write_json(self, _name: str, state: dict[str, object]) -> None:
        self.state = dict(state)


class FixedFeishuAdapter:
    def __init__(self, result: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.result = result or {}
        self.error = error

    def handle_message_event(self, _event: dict[str, object], **kwargs: object) -> dict[str, object]:
        expect(kwargs.get("source") == "websocket", "Feishu runner preserves websocket source")
        if self.error is not None:
            raise self.error
        return dict(self.result)


class AliveThread:
    def is_alive(self) -> bool:
        return True


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
        "VEYRA_PHASE6_EXTENSION_DEPLOYMENT_MODE": "record_only",
    }
    merged = _merge_env_text(original, updates)
    expect("VEYRA_CORE_MODEL_ENABLED=1" in merged, "boolean env values are normalized")
    expect("FEISHU_APP_SECRET=new-secret" in merged, "existing secret value is replaced")
    expect("OPENCLAW_BASE_URL=http://127.0.0.1:18789" in merged, "new allowed key is appended")
    expect(
        "VEYRA_PHASE6_EXTENSION_DEPLOYMENT_MODE=record_only" in merged,
        "Phase 6 lifecycle configuration is supported by local setup",
    )
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

    feishu_error = _feishu_diagnostics(
        {
            "configured": True,
            "thread_alive": True,
            "status": "connect_error",
            "started_at": "2026-07-30T00:00:00+00:00",
            "last_connected_at": None,
            "last_event_after_start": False,
        }
    )
    expect(feishu_error["connected"] is False, "Feishu reconnect thread is not reported as connected")
    expect(feishu_error["readiness"] == "not_ready", "Feishu connect error is not mislabeled waiting for event")

    feishu_connecting = _feishu_diagnostics(
        {
            "configured": True,
            "thread_alive": True,
            "status": "connecting",
            "started_at": "2026-07-30T00:00:00+00:00",
            "last_connected_at": None,
            "last_event_after_start": False,
        }
    )
    expect(feishu_connecting["readiness"] == "connecting", "Feishu connecting state stays distinct")

    feishu_waiting = _feishu_diagnostics(
        {
            "configured": True,
            "thread_alive": True,
            "status": "running",
            "started_at": "2026-07-30T00:00:00+00:00",
            "last_connected_at": "2026-07-30T00:00:01+00:00",
            "last_event_after_start": False,
        }
    )
    expect(feishu_waiting["connected"] is True, "current-run Feishu connection is recognized")
    expect(feishu_waiting["readiness"] == "waiting_for_event", "connected Feishu without an event waits honestly")

    feishu_receiving = _feishu_diagnostics(
        {
            "configured": True,
            "thread_alive": True,
            "status": "running",
            "started_at": "2026-07-30T00:00:00+00:00",
            "last_connected_at": "2026-07-30T00:00:01+00:00",
            "last_event_after_start": True,
        }
    )
    expect(feishu_receiving["readiness"] == "receiving", "fresh Feishu inbound event reaches receiving")

    store = MemoryStateStore(
        {
            "status": "running",
            "started_at": "2026-07-30T00:00:00+00:00",
            "last_connected_at": "2026-07-30T00:00:01+00:00",
            "last_processed_at": None,
            "last_reply_sent_at": None,
        }
    )
    delivered = {
        "status": "received",
        "source": "websocket",
        "receipt": {
            "status": "delivered",
            "outbox": {
                "status": "sent",
                "delivery_status": "provider_sent",
                "external_message_id": "om_test",
            },
        },
    }
    runner = FeishuWsRunner(
        state_store=store,  # type: ignore[arg-type]
        adapter=FixedFeishuAdapter(delivered),  # type: ignore[arg-type]
    )
    runner._thread = AliveThread()  # type: ignore[assignment]
    runner._handle_message_event({}, tls_info={"verify": True})
    expect(bool(store.state.get("last_processed_at")), "successful Feishu processing is recorded")
    expect(
        store.state.get("last_reply_sent_at") == store.state.get("last_processed_at"),
        "provider-sent Feishu reply evidence is recorded",
        store.state,
    )
    runner.adapter = FixedFeishuAdapter(
        {
            "status": "received",
            "source": "websocket",
            "receipt": {
                "status": "delivered",
                "outbox": {"delivery_status": "send_failed"},
                "outbox_messages": [{"delivery_status": "send_failed"}],
            },
        }
    )  # type: ignore[assignment]
    runner._handle_message_event({}, tls_info={"verify": True})
    expect(
        runner.status().get("last_reply_sent_after_start") is False,
        "a later processed message with failed delivery cannot reuse old reply evidence",
    )
    previous_processed_at = store.state.get("last_processed_at")
    runner.adapter = FixedFeishuAdapter(
        {
            "status": "received",
            "source": "websocket",
            "receipt": {"status": "duplicate"},
        }
    )  # type: ignore[assignment]
    runner._handle_message_event({}, tls_info={"verify": True})
    expect(
        store.state.get("last_processed_at") == previous_processed_at,
        "duplicate Feishu redelivery does not replace successful processing evidence",
    )
    runner.adapter = FixedFeishuAdapter(error=RuntimeError("synthetic processing failure"))  # type: ignore[assignment]
    try:
        runner._handle_message_event({}, tls_info={"verify": True})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Feishu processing failure was swallowed")
    expect(store.state.get("last_event_failure_at") == store.state.get("last_event_at"), "Feishu processing failure is persisted")
    last_result = store.state.get("last_result")
    expect(
        isinstance(last_result, dict) and last_result.get("status") == "processing_error",
        "Feishu processing failure replaces stale success result",
        last_result,
    )
    expect(
        runner.status().get("readiness") == "processing_failed",
        "connected test runner reports unrecovered processing failure instead of receiving",
    )

    with tempfile.TemporaryDirectory() as invalid_ca_dir:
        tls_store = MemoryStateStore({"status": "starting", "last_error": None})
        tls_runner = FeishuWsRunner(
            state_store=tls_store,  # type: ignore[arg-type]
            adapter=FixedFeishuAdapter(delivered),  # type: ignore[arg-type]
        )
        tls_runner._run("test-app", "test-secret", {"ca_bundle": invalid_ca_dir})
        expect(tls_store.state.get("status") == "connect_error", "invalid Feishu CA path is persisted as connect_error")
        expect(tls_store.state.get("error_type") == "ValueError", "invalid Feishu CA path exposes a typed diagnostic")

    print("local_setup_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
