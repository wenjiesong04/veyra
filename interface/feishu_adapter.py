from __future__ import annotations

import json
import os
from urllib import error as urlerror
from urllib import request as urlrequest
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
        content_payload = self._content_payload(message)
        attachment = self._attachment_payload(message, content_payload=content_payload)
        if attachment.get("image_key") and self._image_fetch_enabled():
            attachment_fetch = self._fetch_attachment(message=message, attachment=attachment)
        elif attachment.get("image_key"):
            attachment_fetch = {"status": "skipped_by_config"}
        else:
            attachment_fetch = {"status": "skipped"}
        text = self._message_text(message)
        message_id = str(message.get("message_id") or header.get("event_id") or "")
        chat_id = str(message.get("chat_id") or "")
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        user_id = str(sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id") or "feishu-user")
        session_key = self._session_key(chat_id=chat_id, user_id=user_id)
        receipt = self.gateway.receive_message(
            text=text,
            channel="feishu",
            user_id=user_id,
            session_id=session_key,
            message_id=message_id or None,
            metadata={
                "feishu": {
                    "event_id": header.get("event_id"),
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type"),
                    "message_type": message.get("message_type"),
                    "source": source,
                    "content_text": self._content_text(message, content_payload=content_payload),
                    "attachment_id": attachment.get("attachment_id"),
                    "image_key": attachment.get("image_key"),
                    "file_key": attachment.get("file_key"),
                    "media_key": attachment.get("media_key"),
                    "attachment_fetch": attachment_fetch,
                    "sender_id": redact_sensitive(sender_id),
                }
            },
        )
        return {"status": "received", "event_type": str(header.get("event_type") or "im.message.receive_v1"), "source": source, "message_id": message_id, "receipt": receipt}

    def _session_key(self, *, chat_id: str, user_id: str) -> str:
        if chat_id:
            return chat_id
        if user_id:
            return f"user_{user_id}"
        return "feishu-session"

    def _message_text(self, message: dict[str, Any]) -> str:
        message_type = str(message.get("message_type") or "")
        parsed = self._content_payload(message)
        if message_type == "text" or "text" in parsed:
            return str(parsed.get("text") or "").strip() or "[empty feishu text message]"
        if message_type == "image":
            return "[feishu image message: image content not available to Veyra Core]"
        return f"[feishu {message_type or 'unknown'} message]"

    def _content_text(self, message: dict[str, Any], *, content_payload: dict[str, Any] | None = None) -> str:
        parsed = content_payload if isinstance(content_payload, dict) else self._content_payload(message)
        for key in ("text", "title", "file_name"):
            if parsed.get(key):
                return str(parsed.get(key))[:500]
        return ""

    def _attachment_payload(self, message: dict[str, Any], *, content_payload: dict[str, Any] | None = None) -> dict[str, str]:
        parsed = content_payload if isinstance(content_payload, dict) else self._content_payload(message)
        image_key = str(parsed.get("image_key") or "")[:500]
        file_key = str(parsed.get("file_key") or "")[:500]
        media_key = str(parsed.get("media_key") or "")[:500]
        attachment_id = image_key or file_key or media_key
        return {
            "attachment_id": attachment_id,
            "image_key": image_key,
            "file_key": file_key,
            "media_key": media_key,
        }

    def _content_payload(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content")
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = {"text": content}
        elif isinstance(content, dict):
            payload = content
        else:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _fetch_attachment(self, *, message: dict[str, Any], attachment: dict[str, str]) -> dict[str, Any]:
        image_key = str(attachment.get("image_key") or "")
        if not image_key:
            return {"status": "skipped"}
        token = self._feishu_tenant_token()
        if not token:
            return {"status": "token_unavailable"}
        base_url = self._feishu_base_url()
        timeout_seconds = self._image_fetch_timeout_seconds()
        url = f"{base_url.rstrip('/')}/open-apis/im/v1/images/{image_key}"
        request = urlrequest.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
                content_type = str(response.headers.get("Content-Type") or "application/octet-stream")
        except urlerror.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(512).decode("utf-8", errors="ignore")
            except Exception:
                detail = ""
            return {
                "status": "download_failed",
                "reason": f"http_{exc.code}",
                "detail": detail,
            }
        except Exception as exc:
            return {"status": "download_failed", "reason": str(exc)}
        path = self._store_attachment(
            payload=payload,
            content_type=content_type,
            image_key=image_key,
            message_id=str(message.get("message_id") or ""),
        )
        if not path:
            return {"status": "store_failed"}
        return {
            "status": "downloaded",
            "content_type": content_type,
            "size_bytes": len(payload),
            "local_path": path,
        }

    def _store_attachment(self, *, payload: bytes, content_type: str, image_key: str, message_id: str) -> str:
        suffix = self._suffix_for_content_type(content_type)
        file_id = message_id or image_key
        if not file_id:
            return ""
        safe_id = "".join(ch for ch in file_id if ch.isalnum() or ch in {"_", "-"})[:80]
        target_dir = self.state_store.root / "attachments" / "feishu"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_id}{suffix}"
        target.write_bytes(payload)
        return str(target)

    def _suffix_for_content_type(self, content_type: str) -> str:
        lowered = str(content_type or "").lower()
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        if "png" in lowered:
            return ".png"
        if "gif" in lowered:
            return ".gif"
        if "webp" in lowered:
            return ".webp"
        return ".bin"

    def _image_fetch_timeout_seconds(self) -> float:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        raw = config.get("image_fetch_timeout_seconds")
        try:
            value = float(raw)
            return value if value > 0 else 2.0
        except (TypeError, ValueError):
            return 2.0

    def _image_fetch_enabled(self) -> bool:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        value = config.get("image_fetch_enabled")
        if value is None:
            return True
        return bool(value)

    def _feishu_tenant_token(self) -> str:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        token_env = str(config.get("tenant_access_token_env") or "FEISHU_TENANT_ACCESS_TOKEN")
        env_token = str(os.getenv(token_env) or "")
        if env_token:
            return env_token
        direct_token = str(config.get("tenant_access_token") or "")
        if direct_token:
            return direct_token
        app_id = self._config_or_env(config, "app_id", "app_id_env", "FEISHU_APP_ID")
        app_secret = self._config_or_env(config, "app_secret", "app_secret_env", "FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            return ""
        body = json.dumps({"app_id": app_id, "app_secret": app_secret}, ensure_ascii=False).encode("utf-8")
        request = urlrequest.Request(
            f"{self._feishu_base_url().rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=6.0) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
        except Exception:
            return ""
        if int(payload.get("code", -1)) != 0:
            return ""
        return str(payload.get("tenant_access_token") or "")

    def _config_or_env(self, config: dict[str, Any], key: str, env_key: str, default_env: str) -> str:
        env_name = str(config.get(env_key) or default_env)
        env_value = str(os.getenv(env_name) or "")
        if env_value:
            return env_value
        return str(config.get(key) or "")

    def _feishu_base_url(self) -> str:
        state = self.state_store.read_json("channel_state.json")
        channels = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        config = channels.get("feishu") if isinstance(channels.get("feishu"), dict) else {}
        return str(config.get("base_url") or "https://open.feishu.cn")

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
