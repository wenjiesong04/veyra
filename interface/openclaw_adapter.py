from __future__ import annotations

import json
import os
import base64
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - optional runtime dependency
    Ed25519PrivateKey = None  # type: ignore[assignment]
    serialization = None  # type: ignore[assignment]

from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.agent_contract import AGENT_CONTRACT_VERSION, normalize_capabilities, validate_task_packet_payload
from interface.event_schema import VeyraTaskPacket


DEFAULT_OPENCLAW_PROTOCOL_MIN = 3
DEFAULT_OPENCLAW_PROTOCOL_MAX = 4
OPENCLAW_REQUIRED_METHODS = ("chat.send",)
OPENCLAW_OPTIONAL_METHODS = ("health", "status", "tools.catalog", "skills.status")


class OpenClawGatewayError(RuntimeError):
    def __init__(self, status: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


class OpenClawAdapter(AgentAdapter):
    """WebSocket Gateway adapter for a local OpenClaw runtime."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        protocol_min: int | None = None,
        protocol_max: int | None = None,
        **_: Any,
    ) -> None:
        raw_url = base_url or os.getenv("OPENCLAW_GATEWAY_URL") or os.getenv("OPENCLAW_BASE_URL", "")
        self.gateway_url = self._gateway_url(raw_url)
        self.origin = self._origin_for_gateway(self.gateway_url)
        self.api_key = api_key or os.getenv("OPENCLAW_GATEWAY_TOKEN") or os.getenv("OPENCLAW_API_KEY", "") or self._local_gateway_token()
        self.password = os.getenv("OPENCLAW_GATEWAY_PASSWORD", "")
        self.timeout = timeout
        self.connect_timeout = min(max(timeout, 1.0), 5.0)
        self.session_key = os.getenv("OPENCLAW_SESSION_KEY", "main")
        self.task_wait_timeout = float(os.getenv("OPENCLAW_TASK_WAIT_TIMEOUT", "30"))
        self.scopes = self._scopes(os.getenv("OPENCLAW_SCOPES", "operator.read,operator.write"))
        self.device_store = Path(os.getenv("OPENCLAW_DEVICE_STORE", "state/openclaw_device.json"))
        self.protocol_min = protocol_min if protocol_min is not None else self._int_env("OPENCLAW_PROTOCOL_MIN", DEFAULT_OPENCLAW_PROTOCOL_MIN)
        configured_max = protocol_max if protocol_max is not None else self._int_env("OPENCLAW_PROTOCOL_MAX", DEFAULT_OPENCLAW_PROTOCOL_MAX)
        self.protocol_max = max(self.protocol_min, configured_max)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        validation_errors = validate_task_packet_payload(task_packet.to_dict())
        if validation_errors:
            return ExecutionResult(
                task_id=task_packet.task_id,
                executor="openclaw",
                status="failed",
                result="Invalid VeyraTaskPacket for OpenClaw adapter.",
                raw={"validation_errors": validation_errors, "task_packet": task_packet.to_dict()},
            )
        if not self.gateway_url:
            return self._unconfigured_result(task_packet)
        prompt = self.render_prompt(task_packet)
        try:
            raw = self._send_chat(prompt)
        except OpenClawGatewayError as exc:
            return ExecutionResult(
                task_id=task_packet.task_id,
                executor="openclaw",
                status=exc.status,
                result=f"OpenClaw gateway request failed: {exc.message}",
                logs=prompt,
            raw=self._redact_payload({"task_packet": task_packet.to_dict(), "gateway_url": self.gateway_url, "error": exc.details}),
            )
        final = raw.get("final_event") or {}
        text = self._message_text(final.get("message"))
        run_id = str(raw.get("run_id") or raw.get("chat_send", {}).get("runId") or task_packet.task_id)
        if final.get("state") == "final":
            return ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status="success",
                result=text or "OpenClaw finished with no text.",
                raw=self._redact_payload(raw),
            )
        if final.get("state") == "error":
            return ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status="error",
                result=str(final.get("errorMessage") or "OpenClaw task failed."),
                raw=self._redact_payload(raw),
            )
        return ExecutionResult(
            task_id=run_id,
            executor="openclaw",
            status="submitted",
            result=f"Task submitted to OpenClaw. run_id={run_id}",
            raw=self._redact_payload(raw),
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        if not self.gateway_url:
            return normalize_capabilities(
                {
                    "runtime": "openclaw",
                    "status": "adapter_unconfigured",
                    "base_url": None,
                    "protocol": "openclaw_gateway_ws",
                    "tools": [],
                    "skills": [],
                    "requires_tool_proxy": True,
                },
                runtime="openclaw",
                base_url=None,
            )
        try:
            return normalize_capabilities(self._gateway_snapshot(), runtime="openclaw", base_url=self.gateway_url)
        except OpenClawGatewayError as exc:
            return normalize_capabilities(
                {
                    "runtime": "openclaw",
                    "status": exc.status,
                    "base_url": self.gateway_url,
                    "protocol": "openclaw_gateway_ws",
                    "connected": False,
                    "error": exc.message,
                    "details": self._redact_payload(exc.details),
                },
                runtime="openclaw",
                base_url=self.gateway_url,
            )

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "summary": "",
            "runtime": "openclaw",
            "status": "local_memory_bridge",
        }

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        return None

    def stop_task(self, task_id: str) -> bool:
        if not self.gateway_url:
            return False
        try:
            self._gateway_request("chat.abort", {"runId": task_id})
            return True
        except OpenClawGatewayError:
            return False

    def connection_status(self) -> dict[str, Any]:
        capabilities = self.fetch_capabilities()
        status = str(capabilities.get("status", "unknown"))
        connected = status in {"available", "ok", "success"}
        return {
            "name": "openclaw",
            "connected": connected,
            "status": "available" if connected else status,
            "base_url": self.gateway_url,
            "protocol": "openclaw_gateway_ws",
            "contract_version": AGENT_CONTRACT_VERSION,
            "capabilities": capabilities,
        }

    def _send_chat(self, message: str) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        with self._open_socket() as ws:
            hello = self._connect(ws, events)
            idempotency_key = f"veyra-{uuid4().hex}"
            chat_send = self._request_on_socket(
                ws,
                "chat.send",
                {
                    "sessionKey": self.session_key,
                    "message": message,
                    "idempotencyKey": idempotency_key,
                },
                events,
            )
            run_id = str(chat_send.get("runId") or idempotency_key)
            final_event = self._wait_for_chat_final(ws, run_id, events)
        return self._redact_payload(
            {
                "hello": self._hello_summary(hello),
                "chat_send": chat_send,
                "run_id": run_id,
                "final_event": final_event,
                "events": events[-20:],
            }
        )

    def _gateway_snapshot(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        with self._open_socket() as ws:
            hello = self._connect(ws, events)
            health = self._compat_request(ws, hello, "health", {}, events)
            status = self._compat_request(ws, hello, "status", {}, events)
            tools = self._compat_request(ws, hello, "tools.catalog", {}, events)
            skills = self._compat_request(ws, hello, "skills.status", {}, events)
        compatibility = self._compatibility_summary(hello)
        compatible = all(compatibility["required_methods"].values())
        # Raw gateway payloads can include host paths, device tokens, and plugin details.
        # Veyra exposes only a stable summary contract to state endpoints and audit logs.
        return self._redact_payload({
            "runtime": "openclaw",
            "status": "available" if compatible else "incompatible_gateway",
            "base_url": self.gateway_url,
            "protocol": "openclaw_gateway_ws",
            "server": self._server_summary(hello),
            "compatibility": compatibility,
            "auth": self._auth_summary(hello),
            "features": {
                "structured_task_packet": True,
                "rendered_prompt_fallback": True,
                "memory_summary": True,
                "memory_patch": True,
                "stop_task": True,
                "tool_proxy_enforced": True,
            },
            "health": self._health_summary(health),
            "gateway_status": self._status_summary(status),
            "tools": self._tools_summary(tools),
            "skills": self._skills_summary(skills),
        })

    def _gateway_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        with self._open_socket() as ws:
            self._connect(ws, events)
            return self._request_on_socket(ws, method, params, events)

    def _open_socket(self) -> Any:
        try:
            return connect(self.gateway_url, origin=self.origin or None, open_timeout=self.connect_timeout, close_timeout=1)
        except TimeoutError as exc:
            raise OpenClawGatewayError("timeout", "OpenClaw gateway connection timed out") from exc
        except (OSError, WebSocketException) as exc:
            reason = str(exc)
            raise OpenClawGatewayError(self._classify_text(reason), reason) from exc

    def _connect(self, ws: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
        request_id = self._send_request(ws, "connect", self._connect_params(self._initial_nonce(ws, events)))
        deadline = time.monotonic() + self.timeout
        while True:
            raw = self._recv(ws, deadline)
            message = self._parse_message(raw)
            if message.get("type") == "event":
                events.append(message)
                # OpenClaw may require a nonce-bound device signature after the first connect.
                # Keep that retry inside the adapter so VeyraCore does not depend on auth shape.
                if message.get("event") == "connect.challenge":
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    nonce = str(payload.get("nonce") or "")
                    if nonce:
                        request_id = self._send_request(ws, "connect", self._connect_params(nonce))
                continue
            if message.get("type") != "res" or message.get("id") != request_id:
                continue
            if message.get("ok"):
                payload = message.get("payload")
                hello = payload if isinstance(payload, dict) else {"result": payload}
                self._store_device_token(hello, self._device_identity())
                return hello
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            raise self._gateway_error(error, "connect")

    def _connect_params(self, nonce: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "minProtocol": self.protocol_min,
            "maxProtocol": self.protocol_max,
            "client": self._connect_client(),
            "role": "operator",
            "scopes": self.scopes,
            "caps": ["tool-events"],
            "userAgent": "Veyra/OpenClawAdapter",
            "locale": "zh-CN",
        }
        device_identity = self._device_identity()
        auth = self._auth_payload(device_identity)
        if auth:
            params["auth"] = auth
        device = self._device_payload(device_identity, nonce)
        if device:
            params["device"] = device
        return params

    def _initial_nonce(self, ws: Any, events: list[dict[str, Any]]) -> str:
        deadline = time.monotonic() + 1.0
        try:
            raw = self._recv(ws, deadline)
        except OpenClawGatewayError as exc:
            if exc.status == "timeout":
                return ""
            raise
        message = self._parse_message(raw)
        if message.get("type") == "event":
            events.append(message)
            if message.get("event") == "connect.challenge":
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                return str(payload.get("nonce") or "")
        return ""

    def _request_on_socket(self, ws: Any, method: str, params: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        request_id = self._send_request(ws, method, params)
        deadline = time.monotonic() + self.timeout
        while True:
            raw = self._recv(ws, deadline)
            message = self._parse_message(raw)
            if message.get("type") == "event":
                events.append(message)
                continue
            if message.get("type") != "res" or message.get("id") != request_id:
                continue
            if message.get("ok"):
                payload = message.get("payload")
                return payload if isinstance(payload, dict) else {"result": payload}
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            raise self._gateway_error(error, method)

    def _send_request(self, ws: Any, method: str, params: dict[str, Any]) -> str:
        request_id = f"veyra-{uuid4().hex}"
        ws.send(json.dumps({"type": "req", "id": request_id, "method": method, "params": params}, ensure_ascii=False))
        return request_id

    def _compat_request(self, ws: Any, hello: dict[str, Any], method: str, params: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        methods = self._feature_methods(hello)
        if methods and method not in methods:
            return {"status": "method_unavailable", "method": method}
        try:
            return self._request_on_socket(ws, method, params, events)
        except OpenClawGatewayError as exc:
            return {"status": exc.status, "error": exc.message, "details": self._redact_payload(exc.details)}

    def _wait_for_chat_final(self, ws: Any, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        deadline = time.monotonic() + self.task_wait_timeout
        while time.monotonic() < deadline:
            try:
                raw = self._recv(ws, deadline)
            except OpenClawGatewayError as exc:
                if exc.status == "timeout":
                    return {"state": "submitted", "reason": "timeout_waiting_for_final_event"}
                raise
            message = self._parse_message(raw)
            if message.get("type") != "event":
                continue
            events.append(message)
            if message.get("event") != "chat":
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict) or payload.get("runId") != run_id:
                continue
            if payload.get("state") in {"final", "error"}:
                return payload
        return {"state": "submitted", "reason": "timeout_waiting_for_final_event"}

    def _recv(self, ws: Any, deadline: float) -> str:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            return str(ws.recv(timeout=remaining))
        except TimeoutError as exc:
            raise OpenClawGatewayError("timeout", "OpenClaw gateway request timed out") from exc
        except ConnectionClosed as exc:
            reason = str(exc)
            raise OpenClawGatewayError(self._classify_text(reason), reason, {"close": reason}) from exc
        except (OSError, WebSocketException) as exc:
            reason = str(exc)
            raise OpenClawGatewayError(self._classify_text(reason), reason) from exc

    def _parse_message(self, raw: str) -> dict[str, Any]:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenClawGatewayError("protocol_error", "OpenClaw gateway returned non-JSON data", {"raw": raw[:300]}) from exc
        return message if isinstance(message, dict) else {"payload": message}

    def _gateway_error(self, error: dict[str, Any], method: str) -> OpenClawGatewayError:
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        code = str(details.get("code") or error.get("code") or "")
        message = str(error.get("message") or f"{method} failed")
        return OpenClawGatewayError(self._classify_text(f"{code} {message}"), message, {"code": code, "details": self._redact_payload(details)})

    def _connect_client(self) -> dict[str, str]:
        return {
            "id": "openclaw-control-ui",
            "version": "veyra-mvp",
            "platform": "python",
            "mode": "webchat",
            "instanceId": "veyra",
        }

    def _auth_payload(self, device_identity: dict[str, Any] | None = None) -> dict[str, str]:
        payload: dict[str, str] = {}
        stored_token = str((device_identity or {}).get("token") or "")
        token = self.api_key or stored_token
        if token:
            payload["token"] = token
        if stored_token and not self.api_key:
            payload["deviceToken"] = stored_token
        if self.password:
            payload["password"] = self.password
        return payload

    def _local_gateway_token(self) -> str:
        if os.getenv("VEYRA_OPENCLAW_USE_LOCAL_CONFIG", "1").strip().lower() in {"0", "false", "no"}:
            return ""
        path = Path.home() / ".openclaw" / "openclaw.json"
        if not path.exists():
            return ""
        try:
            config = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return ""
        return self._find_token(config)

    def _find_token(self, value: Any) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() == "token" and isinstance(item, str) and len(item) >= 20:
                    return item
                found = self._find_token(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_token(item)
                if found:
                    return found
        return ""

    def _device_identity(self) -> dict[str, Any] | None:
        if Ed25519PrivateKey is None or serialization is None:
            return None
        # This file contains local signing material and must remain gitignored.
        # Only short summaries of device-auth state may leave the adapter boundary.
        self.device_store.parent.mkdir(parents=True, exist_ok=True)
        store = self._read_device_store()
        if not store:
            seed = os.urandom(32)
            private_key = Ed25519PrivateKey.from_private_bytes(seed)
            public_bytes = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            store = {
                "version": 1,
                "deviceId": hashlib.sha256(public_bytes).hexdigest(),
                "publicKey": self._b64url(public_bytes),
                "privateKey": self._b64url(seed),
                "tokens": {},
            }
            self._write_device_store(store)
        token = ""
        role_token = store.get("tokens", {}).get("operator") if isinstance(store.get("tokens"), dict) else None
        if isinstance(role_token, dict):
            token = str(role_token.get("token") or "")
        return {
            "deviceId": str(store.get("deviceId") or ""),
            "publicKey": str(store.get("publicKey") or ""),
            "privateKey": str(store.get("privateKey") or ""),
            "token": token,
        }

    def _device_payload(self, device_identity: dict[str, Any] | None, nonce: str) -> dict[str, Any] | None:
        if not device_identity or Ed25519PrivateKey is None or not nonce:
            return None
        client = self._connect_client()
        signed_at = int(time.time() * 1000)
        token = self.api_key or str(device_identity.get("token") or "")
        message = "|".join(
            [
                "v2",
                str(device_identity["deviceId"]),
                client["id"],
                client["mode"],
                "operator",
                ",".join(self.scopes),
                str(signed_at),
                token,
                nonce,
            ]
        )
        seed = self._b64url_decode(str(device_identity["privateKey"]))
        signature = Ed25519PrivateKey.from_private_bytes(seed).sign(message.encode("utf-8"))
        return {
            "id": device_identity["deviceId"],
            "publicKey": device_identity["publicKey"],
            "signature": self._b64url(signature),
            "signedAt": signed_at,
            "nonce": nonce,
        }

    def _store_device_token(self, hello: dict[str, Any], device_identity: dict[str, Any] | None) -> None:
        auth = hello.get("auth") if isinstance(hello.get("auth"), dict) else {}
        token = str(auth.get("deviceToken") or "")
        if not token or not device_identity:
            return
        store = self._read_device_store()
        tokens = store.setdefault("tokens", {})
        if isinstance(tokens, dict):
            tokens["operator"] = {
                "token": token,
                "role": str(auth.get("role") or "operator"),
                "scopes": auth.get("scopes") if isinstance(auth.get("scopes"), list) else [],
                "updatedAtMs": int(time.time() * 1000),
            }
            self._write_device_store(store)

    def _read_device_store(self) -> dict[str, Any]:
        if not self.device_store.exists():
            return {}
        try:
            data = json.loads(self.device_store.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) and data.get("version") == 1 else {}

    def _write_device_store(self, store: dict[str, Any]) -> None:
        self.device_store.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    def _hello_summary(self, hello: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": hello.get("type"),
            "protocol": hello.get("protocol"),
            "server": self._server_summary(hello),
            "auth": self._auth_summary(hello),
        }

    def _server_summary(self, hello: dict[str, Any]) -> dict[str, Any]:
        server = hello.get("server") if isinstance(hello.get("server"), dict) else {}
        features = hello.get("features") if isinstance(hello.get("features"), dict) else {}
        methods = self._feature_methods(hello)
        events = features.get("events") if isinstance(features.get("events"), list) else []
        return {
            "version": server.get("version"),
            "protocol": hello.get("protocol"),
            "method_count": len(methods),
            "event_count": len(events),
        }

    def _compatibility_summary(self, hello: dict[str, Any]) -> dict[str, Any]:
        methods = set(self._feature_methods(hello))
        required = {method: (not methods or method in methods) for method in OPENCLAW_REQUIRED_METHODS}
        optional = {method: (not methods or method in methods) for method in OPENCLAW_OPTIONAL_METHODS}
        return {
            "native_adapter": True,
            "transport": "openclaw_gateway_ws",
            "status": "compatible" if all(required.values()) else "incompatible",
            "requested_protocol": {"min": self.protocol_min, "max": self.protocol_max},
            "server_protocol": hello.get("protocol"),
            "required_methods": required,
            "optional_methods": optional,
        }

    def _feature_methods(self, hello: dict[str, Any]) -> list[str]:
        features = hello.get("features") if isinstance(hello.get("features"), dict) else {}
        methods = features.get("methods") if isinstance(features.get("methods"), list) else []
        return [str(method) for method in methods]

    def _auth_summary(self, hello: dict[str, Any]) -> dict[str, Any]:
        auth = hello.get("auth") if isinstance(hello.get("auth"), dict) else {}
        scopes = auth.get("scopes") if isinstance(auth.get("scopes"), list) else []
        return {
            "role": auth.get("role"),
            "scopes": [str(scope) for scope in scopes],
        }

    def _health_summary(self, health: dict[str, Any]) -> dict[str, Any]:
        plugins = health.get("plugins") if isinstance(health.get("plugins"), dict) else {}
        loaded = plugins.get("loaded") if isinstance(plugins.get("loaded"), list) else []
        errors = plugins.get("errors") if isinstance(plugins.get("errors"), list) else []
        return {
            "ok": bool(health.get("ok")),
            "duration_ms": health.get("durationMs"),
            "plugin_count": len(loaded),
            "plugin_error_count": len(errors),
            "heartbeat_seconds": health.get("heartbeatSeconds"),
        }

    def _status_summary(self, status: dict[str, Any]) -> dict[str, Any]:
        tasks = status.get("tasks") if isinstance(status.get("tasks"), dict) else {}
        task_audit = status.get("taskAudit") if isinstance(status.get("taskAudit"), dict) else {}
        return {
            "runtime_version": status.get("runtimeVersion"),
            "tasks": {
                "total": tasks.get("total"),
                "active": tasks.get("active"),
                "failures": tasks.get("failures"),
            },
            "task_audit": {
                "warnings": task_audit.get("warnings"),
                "errors": task_audit.get("errors"),
            },
        }

    def _tools_summary(self, tools: dict[str, Any]) -> dict[str, Any]:
        groups = tools.get("groups") if isinstance(tools.get("groups"), list) else []
        tool_count = 0
        group_ids: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_ids.append(str(group.get("id") or "unknown"))
            entries = group.get("tools") if isinstance(group.get("tools"), list) else []
            tool_count += len(entries)
        return {"group_count": len(group_ids), "tool_count": tool_count, "groups": group_ids}

    def _skills_summary(self, skills: dict[str, Any]) -> dict[str, Any]:
        items = skills.get("skills") if isinstance(skills.get("skills"), list) else []
        enabled = [item for item in items if isinstance(item, dict) and item.get("disabled") is not True]
        eligible = [item for item in items if isinstance(item, dict) and item.get("eligible") is True]
        return {"skill_count": len(items), "enabled_count": len(enabled), "eligible_count": len(eligible)}

    def _redact_payload(self, value: Any) -> Any:
        # Treat gateway payloads as hostile-by-default: recurse through nested events
        # before anything is stored in Veyra state or returned through public APIs.
        sensitive_keys = {
            "authorization",
            "devicetoken",
            "password",
            "privatekey",
            "secret",
            "signature",
            "token",
        }
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).replace("_", "").replace("-", "").lower()
                redacted[key] = "__REDACTED__" if normalized in sensitive_keys else self._redact_payload(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_payload(item) for item in value]
        return value

    def _unconfigured_result(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        prompt = self.render_prompt(task_packet)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor="openclaw",
            status="adapter_unconfigured",
            result="OpenClaw adapter is not connected. Configure OPENCLAW_GATEWAY_URL or OPENCLAW_BASE_URL before submitting real tasks.",
            logs=prompt,
            raw={"contract_version": AGENT_CONTRACT_VERSION, "task_packet": task_packet.to_dict(), "configured": False},
        )

    @staticmethod
    def _gateway_url(raw_url: str) -> str:
        url = raw_url.strip()
        if not url:
            return ""
        if "://" not in url:
            url = f"ws://{url}"
        parsed = urlparse(url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/") or "", "", "", ""))

    @staticmethod
    def _origin_for_gateway(gateway_url: str) -> str:
        if not gateway_url:
            return ""
        parsed = urlparse(gateway_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
        return urlunparse((scheme, parsed.netloc, "", "", "", ""))

    @staticmethod
    def _scopes(raw: str) -> list[str]:
        scopes = [item.strip() for item in raw.split(",") if item.strip()]
        return scopes or ["operator.read", "operator.write"]

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    @staticmethod
    def _message_text(message: Any) -> str:
        if isinstance(message, dict):
            if isinstance(message.get("text"), str):
                return message["text"]
            content = message.get("content")
            if isinstance(content, list):
                return "\n".join(
                    str(item.get("text"))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
                ).strip()
        return ""

    @staticmethod
    def _b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _b64url_decode(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * ((4 - len(text) % 4) % 4))

    @staticmethod
    def _classify_text(text: str) -> str:
        lowered = text.lower()
        if "device identity required" in lowered or "device identity" in lowered:
            return "device_identity_required"
        if "pairing" in lowered:
            return "pairing_required"
        if "auth" in lowered or "token" in lowered or "password" in lowered:
            return "auth_required"
        if "missing scope" in lowered or "scope" in lowered:
            return "scope_required"
        if "protocol" in lowered and ("mismatch" in lowered or "unsupported" in lowered or "incompatible" in lowered):
            return "protocol_mismatch"
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout"
        if "connection refused" in lowered or "connect call failed" in lowered or "operation not permitted" in lowered:
            return "unavailable"
        return "unavailable"
