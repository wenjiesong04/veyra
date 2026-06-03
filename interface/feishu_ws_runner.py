from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.feishu_adapter import FeishuAdapter


class FeishuWsRunner:
    """Feishu long-connection runner for local Veyra-first operation."""

    def __init__(self, *, state_store: WorldStateStore, adapter: FeishuAdapter) -> None:
        self.state_store = state_store
        self.adapter = adapter
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        alive = bool(self._thread and self._thread.is_alive())
        config = self._config()
        started_at = str(state.get("started_at") or "")
        last_event_at = str(state.get("last_event_at") or "")
        status = str(state.get("status") or "stopped")
        last_event_after_start = bool(last_event_at and (not started_at or last_event_at >= started_at))
        diagnostics: list[str] = []
        if status in {"running", "starting"} and not alive:
            status = "stale"
            diagnostics.append("Feishu websocket state is running but the worker thread is not alive.")
        if alive and started_at and not last_event_after_start:
            diagnostics.append("No Feishu events have been received since this websocket runner started.")
        return {
            **state,
            "status": status,
            "thread_alive": alive,
            "configured": bool(self._secret(config, "app_id", "FEISHU_APP_ID") and self._secret(config, "app_secret", "FEISHU_APP_SECRET")),
            "last_event_after_start": last_event_after_start,
            "diagnostics": diagnostics,
            "channel_config": redact_sensitive(config),
        }

    def import_openclaw_config(self, *, path: str | None = None, enable: bool = True) -> dict[str, Any]:
        config_path = Path(path or Path.home() / ".openclaw" / "openclaw.json")
        if not config_path.exists():
            return {"status": "not_found", "path": str(config_path)}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"status": "error", "reason": str(exc), "error_type": type(exc).__name__, "path": str(config_path)}
        feishu = ((payload.get("channels") if isinstance(payload.get("channels"), dict) else {}).get("feishu") or {})
        if not isinstance(feishu, dict) or not feishu.get("appId") or not feishu.get("appSecret"):
            return {"status": "not_configured", "reason": "OpenClaw config does not contain Feishu appId/appSecret.", "path": str(config_path)}
        patch = {
            "enabled": bool(enable),
            "delivery": "feishu",
            "connection_mode": "websocket" if str(feishu.get("connectionMode") or "").lower() == "websocket" else "callback",
            "base_url": self._domain_to_base_url(str(feishu.get("domain") or "feishu")),
            "app_id": str(feishu.get("appId") or ""),
            "app_secret": str(feishu.get("appSecret") or ""),
            "dm_policy": feishu.get("dmPolicy"),
            "group_policy": feishu.get("groupPolicy"),
            "allow_from": feishu.get("allowFrom") if isinstance(feishu.get("allowFrom"), list) else [],
        }
        state = self.state_store.read_json("channel_state.json")
        channels = state.setdefault("channels", {})
        current = channels.setdefault("feishu", {})
        current.update({key: value for key, value in patch.items() if value is not None})
        self.state_store.write_json("channel_state.json", state)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "feishu_import_openclaw_config", "status": "success", "artifacts": {"path": str(config_path), "connection_mode": patch["connection_mode"]}})
        return {"status": "success", "path": str(config_path), "config": redact_sensitive(current)}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {**self.status(), "status": "already_running"}
            config = self._config()
            app_id = self._secret(config, "app_id", "FEISHU_APP_ID")
            app_secret = self._secret(config, "app_secret", "FEISHU_APP_SECRET")
            if not app_id or not app_secret:
                return {"status": "not_configured", "reason": "Feishu app_id/app_secret is required for websocket mode."}
            previous = self._read_state()
            state = {
                "status": "starting",
                "started_at": utc_now_iso(),
                "last_event_at": None,
                "last_result": None,
                "previous_last_event_at": previous.get("last_event_at"),
                "previous_last_result": redact_sensitive(previous.get("last_result"), max_string=1800),
                "last_error": None,
            }
            self._write_state(state)
            self._thread = threading.Thread(target=self._run, args=(app_id, app_secret, config), daemon=True, name="veyra-feishu-ws")
            self._thread.start()
        return self.status()

    def autostart_if_configured(self) -> dict[str, Any]:
        config = self._config()
        if not config.get("enabled"):
            return {"status": "skipped", "reason": "Feishu channel is disabled."}
        if str(config.get("connection_mode") or "callback") != "websocket":
            return {"status": "skipped", "reason": "Feishu channel is not in websocket mode."}
        return self.start()

    def _run(self, app_id: str, app_secret: str, config: dict[str, Any]) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401
        except Exception as exc:
            self._write_state({**self._read_state(), "status": "dependency_missing", "last_error": str(exc), "updated_at": utc_now_iso()})
            return

        def handle_message(data: Any) -> None:
            event = self._event_from_sdk_payload(lark, data)
            result = self.adapter.handle_message_event(event, header={"event_type": "im.message.receive_v1"}, source="websocket")
            state = self._read_state()
            state.update({"status": "running", "last_event_at": utc_now_iso(), "last_result": redact_sensitive(result, max_string=1800), "updated_at": utc_now_iso()})
            self._write_state(state)

        try:
            handler = lark.EventDispatcherHandler.builder(
                self._secret(config, "encrypt_key", "FEISHU_ENCRYPT_KEY"),
                self._secret(config, "verification_token", "FEISHU_VERIFICATION_TOKEN"),
            ).register_p2_im_message_receive_v1(handle_message).build()
            client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=handler,
                domain=str(config.get("base_url") or "https://open.feishu.cn"),
                auto_reconnect=True,
            )
            self._write_state({**self._read_state(), "status": "running", "updated_at": utc_now_iso()})
            client.start()
        except Exception as exc:
            self._write_state({**self._read_state(), "status": "error", "last_error": str(exc), "error_type": type(exc).__name__, "updated_at": utc_now_iso()})

    def _event_from_sdk_payload(self, lark: Any, data: Any) -> dict[str, Any]:
        try:
            payload = json.loads(lark.JSON.marshal(data))
        except Exception:
            payload = {}
        if isinstance(payload.get("event"), dict):
            return payload["event"]
        if "message" in payload or "sender" in payload:
            return payload
        event = getattr(data, "event", None)
        if event is not None:
            try:
                parsed = json.loads(lark.JSON.marshal(event))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {}

    def _config(self) -> dict[str, Any]:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        return channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}

    def _secret(self, config: dict[str, Any], key: str, fallback_env: str) -> str:
        env_name = str(config.get(f"{key}_env") or fallback_env)
        if env_name and os.getenv(env_name):
            return str(os.getenv(env_name) or "")
        return str(config.get(key) or "")

    def _domain_to_base_url(self, domain: str) -> str:
        lowered = domain.lower()
        if lowered in {"lark", "larksuite"}:
            return "https://open.larksuite.com"
        return "https://open.feishu.cn"

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json("feishu_ws_state.json") or {"status": "stopped", "last_event_at": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json("feishu_ws_state.json", state)
