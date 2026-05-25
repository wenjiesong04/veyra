from __future__ import annotations

import json
import os
from typing import Any

import httpx

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ChannelAdapter:
    """Outbound chat delivery adapter with local outbox fallback."""

    def __init__(self, state_store: WorldStateStore | None = None, channel: str = "api") -> None:
        self.state_store = state_store
        self.channel = channel

    def send(self, session_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = metadata or {}
        config = self._channel_config()
        item = {
            "channel": self.channel,
            "session_id": session_id,
            "message": message,
            "metadata": metadata,
            "status": "queued",
            "created_at": utc_now_iso(),
            "delivery": str(config.get("delivery") or "local_outbox"),
        }
        if config.get("enabled") is False:
            item.update({"status": "not_configured", "reason": f"{self.channel} channel is disabled."})
        elif item["delivery"] == "feishu" or self.channel == "feishu":
            item.update(self._send_feishu(session_id=session_id, message=message, metadata=metadata, config=config))
        if self.state_store:
            state = self.state_store.read_json("channel_state.json")
            outbox = state.setdefault("outbox", [])
            if not isinstance(outbox, list):
                outbox = []
                state["outbox"] = outbox
            outbox.append(item)
            state["outbox"] = outbox[-500:]
            self.state_store.write_json("channel_state.json", state)
        return item

    def _channel_config(self) -> dict[str, Any]:
        if not self.state_store:
            return {"enabled": True, "delivery": "local_outbox"}
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get(self.channel) if isinstance(channels.get(self.channel), dict) else {}
        if not config:
            config = {"enabled": True, "delivery": "local_outbox"}
        return config

    def _send_feishu(self, *, session_id: str, message: str, metadata: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        target = self._feishu_target(session_id=session_id, metadata=metadata, config=config)
        if not target.get("receive_id"):
            return {"status": "not_configured", "reason": "Feishu receive_id is not configured and no inbound chat_id is available."}
        token_result = self._feishu_token(config)
        if token_result.get("status") != "success":
            return token_result
        base_url = str(config.get("base_url") or "https://open.feishu.cn").rstrip("/")
        timeout = float(config.get("timeout") or 15)
        payload = {
            "receive_id": target["receive_id"],
            "msg_type": "text",
            "content": json.dumps({"text": message}, ensure_ascii=False),
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{base_url}/open-apis/im/v1/messages",
                    params={"receive_id_type": target.get("receive_id_type") or "chat_id"},
                    headers={"Authorization": f"Bearer {token_result['tenant_access_token']}", "Content-Type": "application/json; charset=utf-8"},
                    json=payload,
                )
            body = response.json()
        except Exception as exc:
            return {"status": "error", "delivery_status": "request_failed", "reason": str(exc), "error_type": type(exc).__name__}
        if response.status_code >= 400:
            return {"status": "error", "delivery_status": "http_error", "http_status": response.status_code, "provider_response": self._compact_provider_response(body)}
        if body.get("code") != 0:
            return {"status": "error", "delivery_status": "provider_error", "provider_response": self._compact_provider_response(body)}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "status": "sent",
            "delivery_status": "provider_sent",
            "provider": "feishu",
            "receive_id_type": target.get("receive_id_type") or "chat_id",
            "receive_id_source": target.get("source"),
            "external_message_id": data.get("message_id"),
            "provider_response": self._compact_provider_response(body),
        }

    def _feishu_token(self, config: dict[str, Any]) -> dict[str, Any]:
        direct_token = self._secret(config, "tenant_access_token", "FEISHU_TENANT_ACCESS_TOKEN")
        if direct_token:
            return {"status": "success", "tenant_access_token": direct_token, "source": "configured_token"}
        app_id = self._secret(config, "app_id", "FEISHU_APP_ID")
        app_secret = self._secret(config, "app_secret", "FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            return {"status": "not_configured", "reason": "Feishu app_id/app_secret or tenant_access_token is required."}
        base_url = str(config.get("base_url") or "https://open.feishu.cn").rstrip("/")
        timeout = float(config.get("timeout") or 15)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    json={"app_id": app_id, "app_secret": app_secret},
                )
            body = response.json()
        except Exception as exc:
            return {"status": "error", "delivery_status": "token_request_failed", "reason": str(exc), "error_type": type(exc).__name__}
        if response.status_code >= 400 or body.get("code") != 0:
            return {"status": "error", "delivery_status": "token_provider_error", "http_status": response.status_code, "provider_response": self._compact_provider_response(body)}
        return {"status": "success", "tenant_access_token": body.get("tenant_access_token"), "expire": body.get("expire"), "source": "app_credentials"}

    def _feishu_target(self, *, session_id: str, metadata: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
        inbound = metadata.get("inbound") if isinstance(metadata.get("inbound"), dict) else {}
        inbound_feishu = inbound.get("feishu") if isinstance(inbound.get("feishu"), dict) else {}
        receive_id = str(metadata.get("receive_id") or "")
        receive_id_type = str(metadata.get("receive_id_type") or "")
        if not receive_id and config.get("reply_to_session", True):
            receive_id = str(feishu.get("chat_id") or inbound_feishu.get("chat_id") or "")
            if receive_id:
                receive_id_type = "chat_id"
        if not receive_id:
            receive_id = self._secret(config, "default_receive_id", "FEISHU_DEFAULT_RECEIVE_ID")
            receive_id_type = receive_id_type or str(config.get("default_receive_id_type") or "chat_id")
        return {
            "receive_id": receive_id,
            "receive_id_type": receive_id_type or "chat_id",
            "source": "inbound_session" if (feishu.get("chat_id") or inbound_feishu.get("chat_id")) else "configured_default",
            "session_id": session_id,
        }

    def _secret(self, config: dict[str, Any], key: str, fallback_env: str) -> str:
        env_name = str(config.get(f"{key}_env") or fallback_env)
        if env_name and os.getenv(env_name):
            return str(os.getenv(env_name) or "")
        return str(config.get(key) or "")

    def _compact_provider_response(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            return {"raw_type": type(body).__name__}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "code": body.get("code"),
            "msg": body.get("msg"),
            "message_id": data.get("message_id"),
        }
