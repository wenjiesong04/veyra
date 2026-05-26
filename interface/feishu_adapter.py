from __future__ import annotations

import json
import os
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.intake_gateway import IntakeGateway


class FeishuAdapter:
    """Inbound Feishu event callback adapter for the local Veyra gateway."""

    def __init__(self, gateway: IntakeGateway, *, state_store: WorldStateStore) -> None:
        self.gateway = gateway
        self.state_store = state_store

    def handle_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("challenge") and payload.get("type") == "url_verification":
            if not self._verify_token(payload.get("token")):
                return {"status": "blocked", "reason": "Feishu verification token mismatch."}
            return {"challenge": payload["challenge"]}
        if payload.get("encrypt"):
            return {"status": "unsupported", "reason": "Encrypted Feishu callbacks require decrypt support; disable encryption or configure a decrypting edge first."}

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if not self._verify_token(header.get("token") or payload.get("token")):
            return {"status": "blocked", "reason": "Feishu verification token mismatch."}
        event_type = str(header.get("event_type") or payload.get("event_type") or "")
        if event_type != "im.message.receive_v1":
            return {"status": "ignored", "event_type": event_type or "unknown"}

        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        return self.handle_message_event(event, header=header, source="callback")

    def handle_message_event(self, event: dict[str, Any], *, header: dict[str, Any] | None = None, source: str = "callback") -> dict[str, Any]:
        header = header or {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        text = self._message_text(message)
        message_id = str(message.get("message_id") or header.get("event_id") or "")
        chat_id = str(message.get("chat_id") or "")
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        user_id = str(sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id") or "feishu-user")
        receipt = self.gateway.receive_message(
            text=text,
            channel="feishu",
            user_id=user_id,
            session_id=chat_id or message_id or "feishu-session",
            message_id=message_id or None,
            metadata={
                "feishu": {
                    "event_id": header.get("event_id"),
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type"),
                    "message_type": message.get("message_type"),
                    "source": source,
                    "content_text": self._content_text(message),
                    "attachment_id": self._attachment_id(message),
                    "sender_id": redact_sensitive(sender_id),
                }
            },
        )
        return {"status": "received", "event_type": str(header.get("event_type") or "im.message.receive_v1"), "source": source, "message_id": message_id, "receipt": receipt}

    def _message_text(self, message: dict[str, Any]) -> str:
        message_type = str(message.get("message_type") or "")
        content = message.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"text": content}
        elif isinstance(content, dict):
            parsed = content
        else:
            parsed = {}
        if message_type == "text" or "text" in parsed:
            return str(parsed.get("text") or "").strip() or "[empty feishu text message]"
        if message_type == "image":
            return "[feishu image message: image content not available to Veyra Core]"
        return f"[feishu {message_type or 'unknown'} message]"

    def _content_text(self, message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return content[:500]
        elif isinstance(content, dict):
            parsed = content
        else:
            parsed = {}
        for key in ("text", "title", "file_name"):
            if parsed.get(key):
                return str(parsed.get(key))[:500]
        return ""

    def _attachment_id(self, message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return ""
        elif isinstance(content, dict):
            parsed = content
        else:
            parsed = {}
        for key in ("image_key", "file_key", "media_key"):
            if parsed.get(key):
                return str(parsed.get(key))[:500]
        return ""

    def _verify_token(self, received: Any) -> bool:
        expected = self._configured_token()
        if not expected:
            return True
        return str(received or "") == expected

    def _configured_token(self) -> str:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        env_name = str(config.get("verification_token_env") or "FEISHU_VERIFICATION_TOKEN")
        if env_name and os.getenv(env_name):
            return str(os.getenv(env_name) or "")
        return str(config.get("verification_token") or "")
