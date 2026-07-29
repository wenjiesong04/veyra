from __future__ import annotations

import json
import os
import ssl
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.feishu_adapter import FeishuAdapter


FEISHU_CONNECTED_STATUSES = frozenset({"running", "already_running"})
FEISHU_CONNECTING_STATUSES = frozenset({"starting", "connecting", "reconnecting"})
FEISHU_ERROR_STATUSES = frozenset({"connect_error", "dependency_missing", "error", "stale"})


def feishu_connection_snapshot(status_payload: dict[str, Any]) -> dict[str, Any]:
    configured = bool(status_payload.get("configured"))
    thread_alive = bool(status_payload.get("thread_alive"))
    status = str(status_payload.get("status") or "unknown")
    started_at = str(status_payload.get("started_at") or "")
    last_connected_at = str(status_payload.get("last_connected_at") or "")
    last_event_after_start = bool(status_payload.get("last_event_after_start"))
    connected_after_start = bool(
        (last_connected_at and (not started_at or last_connected_at >= started_at))
        or last_event_after_start
    )
    connected = bool(
        configured
        and thread_alive
        and status in FEISHU_CONNECTED_STATUSES
        and connected_after_start
    )

    processing_failure_unrecovered = bool(status_payload.get("processing_failure_unrecovered"))
    if not configured:
        readiness = "not_configured"
    elif connected and processing_failure_unrecovered:
        readiness = "processing_failed"
    elif connected and last_event_after_start:
        readiness = "receiving"
    elif connected:
        readiness = "waiting_for_event"
    elif status in FEISHU_CONNECTING_STATUSES and thread_alive:
        readiness = "connecting"
    elif status in FEISHU_ERROR_STATUSES or thread_alive:
        readiness = "not_ready"
    else:
        readiness = "configured_not_running"

    return {
        "connected": connected,
        "connected_after_start": connected_after_start,
        "readiness": readiness,
        "status": status,
        "thread_alive": thread_alive,
        "last_event_after_start": last_event_after_start,
        "processing_failure_unrecovered": processing_failure_unrecovered,
    }


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
        last_processed_at = str(state.get("last_processed_at") or "")
        last_reply_sent_at = str(state.get("last_reply_sent_at") or "")
        status = str(state.get("status") or "stopped")
        last_event_after_start = bool(last_event_at and (not started_at or last_event_at >= started_at))
        last_processed_after_start = bool(last_processed_at and (not started_at or last_processed_at >= started_at))
        last_reply_sent_after_start = bool(
            last_processed_after_start
            and last_reply_sent_at
            and last_reply_sent_at >= last_processed_at
        )
        last_event_failure_at = str(state.get("last_event_failure_at") or "")
        processing_failure_unrecovered = bool(
            last_event_failure_at
            and (not last_processed_at or last_event_failure_at >= last_processed_at)
        )
        diagnostics: list[str] = []
        if status in {"running", "starting"} and not alive:
            status = "stale"
            diagnostics.append("Feishu websocket state is running but the worker thread is not alive.")
        if alive and status in {"starting", "connecting"}:
            diagnostics.append("Feishu websocket worker is alive but has not completed the websocket connection.")
        if alive and started_at and not last_event_after_start:
            diagnostics.append("No Feishu events have been received since this websocket runner started.")
        if processing_failure_unrecovered:
            diagnostics.append("The latest Feishu event reached the websocket worker but failed during Veyra processing.")
        elif last_processed_after_start and not last_reply_sent_after_start:
            diagnostics.append("The latest processed Feishu message has no provider-sent reply evidence.")
        last_error = str(state.get("last_error") or "")
        if "CERTIFICATE_VERIFY_FAILED" in last_error:
            diagnostics.append("Feishu websocket TLS verification failed; configure FEISHU_CA_BUNDLE or the system trust store.")
        result = {
            **state,
            "status": status,
            "thread_alive": alive,
            "configured": bool(self._secret(config, "app_id", "FEISHU_APP_ID") and self._secret(config, "app_secret", "FEISHU_APP_SECRET")),
            "last_event_after_start": last_event_after_start,
            "last_processed_after_start": last_processed_after_start,
            "last_reply_sent_after_start": last_reply_sent_after_start,
            "processing_failure_unrecovered": processing_failure_unrecovered,
            "diagnostics": diagnostics,
            "channel_config": redact_sensitive(config),
        }
        connection = feishu_connection_snapshot(result)
        result["connected"] = connection["connected"]
        result["readiness"] = connection["readiness"]
        return result

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
        current: dict[str, Any] = {}

        def update_feishu(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal current
            channels = state.setdefault("channels", {})
            if not isinstance(channels, dict):
                channels = {}
                state["channels"] = channels
            existing = channels.setdefault("feishu", {})
            current = existing if isinstance(existing, dict) else {}
            current.update({key: value for key, value in patch.items() if value is not None})
            channels["feishu"] = current
            return state

        self.state_store.mutate_json("channel_state.json", update_feishu)
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
                "last_processed_at": None,
                "last_reply_sent_at": None,
                "last_event_failure_at": None,
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
            import lark_oapi.ws.client as lark_ws_client
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401
        except Exception as exc:
            self._write_state({**self._read_state(), "status": "dependency_missing", "last_error": str(exc), "updated_at": utc_now_iso()})
            return

        tls_info: dict[str, Any] = {"verify": True, "mode": "initialization_failed"}
        try:
            ssl_context, tls_info = self._ssl_context(config)
            self._install_lark_ws_hooks(lark_ws_client, ssl_context=ssl_context, tls_info=tls_info)
        except Exception as exc:
            self._mark_ws_connect_failed(exc, tls_info)
            return

        def handle_message(data: Any) -> None:
            event = self._event_from_sdk_payload(lark, data)
            self._handle_message_event(event, tls_info=tls_info)

        try:
            handler = lark.EventDispatcherHandler.builder(
                self._secret(config, "encrypt_key", "FEISHU_ENCRYPT_KEY"),
                self._secret(config, "verification_token", "FEISHU_VERIFICATION_TOKEN"),
            ).register_p2_im_message_receive_v1(handle_message).build()
            client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                log_level=lark.LogLevel.WARNING,
                event_handler=handler,
                domain=str(config.get("base_url") or "https://open.feishu.cn"),
                auto_reconnect=True,
            )
            client.on_reconnecting = lambda: self._mark_ws_reconnecting(tls_info)
            client.on_reconnected = lambda: self._mark_ws_connected(client, tls_info)
            self._write_state({**self._read_state(), "status": "connecting", "tls": tls_info, "updated_at": utc_now_iso()})
            client.start()
        except Exception as exc:
            self._write_state({**self._read_state(), "status": "error", "last_error": redact_sensitive(str(exc)), "error_type": type(exc).__name__, "tls": tls_info, "updated_at": utc_now_iso()})

    def _handle_message_event(self, event: dict[str, Any], *, tls_info: dict[str, Any]) -> dict[str, Any]:
        observed_at = utc_now_iso()
        try:
            result = self.adapter.handle_message_event(
                event,
                header={"event_type": "im.message.receive_v1"},
                source="websocket",
            )
        except Exception as exc:
            state = self._read_state()
            state.update(
                {
                    "status": "running",
                    "last_event_at": observed_at,
                    "last_event_failure_at": observed_at,
                    "last_result": {
                        "status": "processing_error",
                        "source": "websocket",
                        "error_type": type(exc).__name__,
                    },
                    "last_error": redact_sensitive(str(exc), max_string=600),
                    "error_type": type(exc).__name__,
                    "tls": tls_info,
                    "updated_at": utc_now_iso(),
                }
            )
            self._write_state(state)
            raise

        receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        receipt_status = str(receipt.get("status") or "")
        outbox = receipt.get("outbox") if isinstance(receipt.get("outbox"), dict) else {}
        outbox_messages = receipt.get("outbox_messages") if isinstance(receipt.get("outbox_messages"), list) else []
        deliveries = [outbox, *(item for item in outbox_messages if isinstance(item, dict))]
        reply_provider_sent = any(
            str(item.get("delivery_status") or "") == "provider_sent"
            for item in deliveries
        )
        state = self._read_state()
        state.update(
            {
                "status": "running",
                "last_event_at": observed_at,
                "last_result": redact_sensitive(result, max_string=1800),
                "last_error": None,
                "error_type": None,
                "tls": tls_info,
                "updated_at": utc_now_iso(),
            }
        )
        if receipt_status == "delivered":
            state["last_event_failure_at"] = None
            state["last_processed_at"] = observed_at
        if receipt_status == "delivered" and reply_provider_sent:
            state["last_reply_sent_at"] = observed_at
        self._write_state(state)
        return result

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

    def _ssl_context(self, config: dict[str, Any]) -> tuple[ssl.SSLContext, dict[str, Any]]:
        if self._env_bool("FEISHU_WS_TLS_VERIFY") is False:
            return ssl._create_unverified_context(), {"verify": False, "mode": "disabled_by_env"}

        ca_bundle = self._ca_bundle_path(config)
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle), {"verify": True, "mode": "ca_bundle", "ca_bundle": self._public_path(ca_bundle)}

        try:
            import truststore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), {"verify": True, "mode": "system_truststore"}
        except Exception as exc:
            return ssl.create_default_context(), {"verify": True, "mode": "python_default", "truststore_error": redact_sensitive(str(exc), max_string=240)}

    def _ca_bundle_path(self, config: dict[str, Any]) -> str:
        ca_env = str(config.get("ca_bundle_env") or "FEISHU_CA_BUNDLE")
        candidates = [
            config.get("ca_bundle"),
            os.getenv(ca_env, ""),
            os.getenv("LARK_CA_BUNDLE", ""),
            os.getenv("REQUESTS_CA_BUNDLE", ""),
            os.getenv("SSL_CERT_FILE", ""),
            os.getenv("CURL_CA_BUNDLE", ""),
        ]
        for candidate in candidates:
            path = str(candidate or "").strip()
            if not path:
                continue
            expanded = Path(path).expanduser()
            if not expanded.is_file():
                raise ValueError(f"Configured Feishu CA bundle is not a readable file: {self._public_path(str(expanded))}")
            return str(expanded)
        return ""

    def _install_lark_ws_hooks(self, lark_ws_client: Any, *, ssl_context: ssl.SSLContext, tls_info: dict[str, Any]) -> None:
        base_kwargs = getattr(lark_ws_client, "_veyra_base_ws_connect_kwargs", None)
        if base_kwargs is None:
            base_kwargs = lark_ws_client._ws_connect_kwargs
            setattr(lark_ws_client, "_veyra_base_ws_connect_kwargs", base_kwargs)
        setattr(lark_ws_client, "_veyra_ws_ssl_context", ssl_context)
        setattr(lark_ws_client, "_veyra_runner", self)
        setattr(lark_ws_client, "_veyra_tls_info", tls_info)

        def ws_connect_kwargs() -> dict[str, Any]:
            kwargs = dict(base_kwargs() or {})
            context = getattr(lark_ws_client, "_veyra_ws_ssl_context", None)
            if context is not None:
                kwargs["ssl"] = context
            return kwargs

        lark_ws_client._ws_connect_kwargs = ws_connect_kwargs

        if getattr(lark_ws_client.Client, "_veyra_connect_observed", False):
            return
        original_connect = lark_ws_client.Client._connect

        async def observed_connect(client: Any) -> None:
            runner = getattr(lark_ws_client, "_veyra_runner", None)
            active_tls_info = getattr(lark_ws_client, "_veyra_tls_info", tls_info)
            try:
                await original_connect(client)
            except Exception as exc:
                if runner is not None:
                    runner._mark_ws_connect_failed(exc, active_tls_info)
                raise
            if runner is not None:
                runner._mark_ws_connected(client, active_tls_info)

        lark_ws_client.Client._connect = observed_connect
        setattr(lark_ws_client.Client, "_veyra_connect_observed", True)

    def _mark_ws_reconnecting(self, tls_info: dict[str, Any]) -> None:
        state = self._read_state()
        state.update({"status": "reconnecting", "tls": tls_info, "last_reconnect_at": utc_now_iso(), "updated_at": utc_now_iso()})
        self._write_state(state)

    def _mark_ws_connected(self, client: Any, tls_info: dict[str, Any]) -> None:
        conn_url = str(getattr(client, "_conn_url", "") or "")
        state = self._read_state()
        state.update(
            {
                "status": "running",
                "last_connected_at": utc_now_iso(),
                "last_error": None,
                "error_type": None,
                "connected_host": urlparse(conn_url).hostname or "",
                "tls": tls_info,
                "updated_at": utc_now_iso(),
            }
        )
        self._write_state(state)

    def _mark_ws_connect_failed(self, exc: Exception, tls_info: dict[str, Any]) -> None:
        state = self._read_state()
        state.update(
            {
                "status": "connect_error",
                "last_connect_error_at": utc_now_iso(),
                "last_error": redact_sensitive(str(exc), max_string=600),
                "error_type": type(exc).__name__,
                "tls": tls_info,
                "updated_at": utc_now_iso(),
            }
        )
        self._write_state(state)

    def _env_bool(self, name: str) -> bool | None:
        value = os.getenv(name)
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return None

    def _public_path(self, path: str) -> str:
        if not path:
            return ""
        if "certifi" in path.lower():
            return "<certifi>"
        return redact_sensitive(path)

    def _domain_to_base_url(self, domain: str) -> str:
        lowered = domain.lower()
        if lowered in {"lark", "larksuite"}:
            return "https://open.larksuite.com"
        return "https://open.feishu.cn"

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json("feishu_ws_state.json") or {"status": "stopped", "last_event_at": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json("feishu_ws_state.json", state)
