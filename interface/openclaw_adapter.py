from __future__ import annotations

import json
import os
import base64
import hashlib
import threading
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
OPENCLAW_OPTIONAL_METHODS = ("health", "status", "tools.catalog", "skills.status", "agent.wait", "chat.history", "memory.summary", "memory.patch")


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
        task_wait_timeout: float | None = None,
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
        configured_wait = task_wait_timeout
        if configured_wait is None:
            configured_wait = self._float_env("OPENCLAW_TASK_WAIT_TIMEOUT", 75.0)
        self.task_wait_timeout = max(15.0, min(float(configured_wait), 300.0))
        self.scopes = self._scopes(os.getenv("OPENCLAW_SCOPES", "operator.read,operator.write"))
        self.memory_scopes = self._scopes(os.getenv("OPENCLAW_MEMORY_SCOPES", ",".join([*self.scopes, "operator.admin"])))
        self.device_store = Path(os.getenv("OPENCLAW_DEVICE_STORE", "state/local/openclaw_device.json"))
        self.protocol_min = protocol_min if protocol_min is not None else self._int_env("OPENCLAW_PROTOCOL_MIN", DEFAULT_OPENCLAW_PROTOCOL_MIN)
        configured_max = protocol_max if protocol_max is not None else self._int_env("OPENCLAW_PROTOCOL_MAX", DEFAULT_OPENCLAW_PROTOCOL_MAX)
        self.protocol_max = max(self.protocol_min, configured_max)
        self._run_cache: dict[str, dict[str, Any]] = {}
        self._run_cache_lock = threading.Lock()

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
            execution = ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status="success",
                result=text or "OpenClaw finished with no text.",
                raw=self._redact_payload(raw),
            )
            self._remember_chat_run(run_id, final_event=final, status="success", result=text)
            return execution
        if final.get("state") == "error":
            execution = ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status="error",
                result=str(final.get("errorMessage") or "OpenClaw task failed."),
                raw=self._redact_payload(raw),
            )
            self._remember_chat_run(run_id, final_event=final, status="error", result=execution.result)
            return execution
        self._remember_chat_run(run_id, final_event=final, status="submitted", result=f"Task submitted to OpenClaw. run_id={run_id}")
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
        if not self.gateway_url:
            return {"session_id": session_id, "summary": "", "runtime": "openclaw", "status": "not_configured"}
        try:
            response = self._gateway_request("memory.summary", {"sessionId": session_id, "sessionKey": self.session_key}, scopes=self.memory_scopes)
        except OpenClawGatewayError as exc:
            return {
                "session_id": session_id,
                "summary": "",
                "runtime": "openclaw",
                "status": exc.status,
                "error": exc.message,
            }
        summary = response.get("summary") or response.get("text") or response.get("memory") or ""
        return {
            "session_id": session_id,
            "summary": summary,
            "runtime": "openclaw",
            "status": str(response.get("status") or ("success" if summary else "empty")),
            "raw": self._redact_payload(response),
        }

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> dict[str, Any]:
        if not self.gateway_url:
            return {"status": "not_configured", "runtime": "openclaw"}
        try:
            response = self._gateway_request("memory.patch", {"sessionKey": self.session_key, "patch": memory_patch}, scopes=self.memory_scopes)
        except OpenClawGatewayError as exc:
            return {"status": exc.status, "runtime": "openclaw", "error": exc.message}
        return {"status": str(response.get("status") or "submitted"), "runtime": "openclaw", "raw": self._redact_payload(response)}

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        if not self.gateway_url:
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="adapter_unconfigured",
                result="OpenClaw adapter is not connected. Configure OPENCLAW_GATEWAY_URL or OPENCLAW_BASE_URL before polling tasks.",
                raw={"configured": False},
            )
        cached = self._cached_execution(task_id)
        if cached:
            return cached
        return self._resolve_chat_run(task_id, wait_timeout_ms=self._status_wait_ms())

    def poll_task(self, task_id: str, timeout_seconds: float = 0.0, interval_seconds: float = 1.0) -> ExecutionResult:
        if timeout_seconds <= 0:
            return self.fetch_task_status(task_id)
        cached = self._cached_execution(task_id)
        if cached:
            return cached
        wait_ms = int(max(1.0, min(float(timeout_seconds), 300.0)) * 1000)
        return self._resolve_chat_run(task_id, wait_timeout_ms=wait_ms)

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
        configured = bool(self.gateway_url)
        return {
            "name": "openclaw",
            "connected": connected,
            "status": "available" if connected else status,
            "base_url": self.gateway_url,
            "protocol": "openclaw_gateway_ws",
            "contract_version": AGENT_CONTRACT_VERSION,
            "validation": {
                "implemented": True,
                "configured": configured,
                "connected": connected,
                "validated": connected,
                "status": "validated" if connected else "validation_pending" if configured else "not_configured",
                "runtime_status": status,
            },
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
        optional_methods = compatibility.get("optional_methods") if isinstance(compatibility.get("optional_methods"), dict) else {}
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
                "memory_summary": bool(optional_methods.get("memory.summary")),
                "memory_patch": bool(optional_methods.get("memory.patch")),
                "task_status": True,
                "task_poll": "agent.wait+chat.history",
                "stop_task": True,
                "tool_proxy_enforced": True,
            },
            "health": self._health_summary(health),
            "gateway_status": self._status_summary(status),
            "tools": self._tools_summary(tools),
            "skills": self._skills_summary(skills),
        })

    def _gateway_request(self, method: str, params: dict[str, Any], *, scopes: list[str] | None = None) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        with self._open_socket() as ws:
            self._connect(ws, events, scopes=scopes)
            return self._request_on_socket(ws, method, params, events)

    def _open_socket(self) -> Any:
        try:
            return connect(self.gateway_url, origin=self.origin or None, open_timeout=self.connect_timeout, close_timeout=1)
        except TimeoutError as exc:
            raise OpenClawGatewayError("timeout", "OpenClaw gateway connection timed out") from exc
        except (OSError, WebSocketException) as exc:
            reason = str(exc)
            raise OpenClawGatewayError(self._classify_text(reason), reason) from exc

    def _connect(self, ws: Any, events: list[dict[str, Any]], *, scopes: list[str] | None = None) -> dict[str, Any]:
        request_id = self._send_request(ws, "connect", self._connect_params(self._initial_nonce(ws, events), scopes=scopes))
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
                        request_id = self._send_request(ws, "connect", self._connect_params(nonce, scopes=scopes))
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

    def _connect_params(self, nonce: str, *, scopes: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "minProtocol": self.protocol_min,
            "maxProtocol": self.protocol_max,
            "client": self._connect_client(),
            "role": "operator",
            "scopes": scopes or self.scopes,
            "caps": ["tool-events"],
            "userAgent": "Veyra/OpenClawAdapter",
            "locale": "zh-CN",
        }
        device_identity = self._device_identity()
        auth = self._auth_payload(device_identity)
        if auth:
            params["auth"] = auth
        device = self._device_payload(device_identity, nonce, scopes=scopes)
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

    def _resolve_chat_run(self, task_id: str, *, wait_timeout_ms: int) -> ExecutionResult:
        cached = self._cached_execution(task_id)
        if cached:
            return cached

        wait_payload: dict[str, Any] | None = None
        try:
            wait_payload = self._agent_wait(task_id, wait_timeout_ms)
        except OpenClawGatewayError:
            wait_payload = None

        if wait_payload is not None:
            execution = self._execution_from_agent_wait(task_id, wait_payload)
            if execution.status in {"success", "error"}:
                self._remember_chat_run(
                    task_id,
                    final_event=execution.raw.get("final_event") if isinstance(execution.raw.get("final_event"), dict) else {},
                    status=execution.status,
                    result=execution.result,
                )
                return execution
            return execution

        listen_seconds = min(max(wait_timeout_ms / 1000.0, 0.5), 8.0)
        try:
            final_event = self._listen_for_chat_run(task_id, listen_seconds)
        except OpenClawGatewayError:
            final_event = {}
        if final_event.get("state") in {"final", "error"}:
            execution = self._execution_from_chat_event(task_id, final_event)
            self._remember_chat_run(
                task_id,
                final_event=final_event,
                status=execution.status,
                result=execution.result,
            )
            return execution

        return self._fetch_task_status_legacy(task_id)

    def _agent_wait(self, run_id: str, timeout_ms: int) -> dict[str, Any]:
        payload = self._gateway_request(
            "agent.wait",
            {
                "runId": run_id,
                "timeoutMs": max(0, int(timeout_ms)),
            },
        )
        if payload.get("status") == "method_unavailable":
            raise OpenClawGatewayError("method_unavailable", "OpenClaw gateway does not expose agent.wait")
        return payload if isinstance(payload, dict) else {"runId": run_id, "status": "timeout"}

    def _fetch_chat_history(self, *, limit: int = 8) -> dict[str, Any]:
        payload = self._gateway_request(
            "chat.history",
            {
                "sessionKey": self.session_key,
                "limit": max(1, min(int(limit), 50)),
            },
        )
        return payload if isinstance(payload, dict) else {}

    def _execution_from_agent_wait(self, task_id: str, wait_payload: dict[str, Any]) -> ExecutionResult:
        wait_status = str(wait_payload.get("status") or "timeout").lower()
        normalized = self._normalize_wait_status(wait_status)
        if normalized == "running":
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="running",
                result=f"OpenClaw chat run {task_id} is still in progress.",
                raw=self._redact_payload({"agent_wait": wait_payload, "poll_method": "agent.wait"}),
            )
        if normalized == "error":
            error_text = str(wait_payload.get("error") or wait_payload.get("stopReason") or "OpenClaw task failed.")
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="error",
                result=error_text,
                raw=self._redact_payload({"agent_wait": wait_payload, "poll_method": "agent.wait"}),
            )

        result_text = self._result_text_for_run(task_id, wait_payload)
        return ExecutionResult(
            task_id=task_id,
            executor="openclaw",
            status="success",
            result=result_text or "OpenClaw finished with no text.",
            raw=self._redact_payload({"agent_wait": wait_payload, "poll_method": "agent.wait"}),
        )

    def _execution_from_chat_event(self, task_id: str, final_event: dict[str, Any]) -> ExecutionResult:
        text = self._message_text(final_event.get("message"))
        if final_event.get("state") == "error":
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="error",
                result=str(final_event.get("errorMessage") or "OpenClaw task failed."),
                raw=self._redact_payload({"final_event": final_event, "poll_method": "chat.events"}),
            )
        return ExecutionResult(
            task_id=task_id,
            executor="openclaw",
            status="success",
            result=text or "OpenClaw finished with no text.",
            raw=self._redact_payload({"final_event": final_event, "poll_method": "chat.events"}),
        )

    def _listen_for_chat_run(self, run_id: str, timeout_seconds: float) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        with self._open_socket() as ws:
            self._connect(ws, events)
            return self._wait_for_chat_final(ws, run_id, events)

    def _fetch_task_status_legacy(self, task_id: str) -> ExecutionResult:
        try:
            status = self._gateway_request("status", {})
        except OpenClawGatewayError as exc:
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status=exc.status,
                result=f"OpenClaw task status request failed: {exc.message}",
                raw=self._redact_payload({"error": exc.details, "poll_method": "status.snapshot"}),
            )
        known_task = self._find_task(status, task_id)
        if known_task:
            state = str(known_task.get("state") or known_task.get("status") or "running")
            normalized = "success" if state in {"final", "done", "completed"} else "error" if state in {"error", "failed"} else "running"
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status=normalized,
                result=str(known_task.get("result") or known_task.get("message") or f"OpenClaw task {task_id} is {normalized}."),
                raw=self._redact_payload({"task": known_task, "status": status, "poll_method": "status.snapshot"}),
            )
        tasks = status.get("tasks") if isinstance(status.get("tasks"), dict) else {}
        active = tasks.get("active")
        if isinstance(active, int) and active > 0:
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="running",
                result=f"OpenClaw has {active} active task(s); task {task_id} has not reached a final event yet.",
                raw=self._redact_payload({"status": status, "poll_method": "status.snapshot"}),
            )
        if self._is_chat_run_id(task_id):
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="running",
                result=f"OpenClaw chat run {task_id} is still in progress; gateway status snapshot has no per-run detail.",
                raw=self._redact_payload({"status": status, "poll_method": "status.snapshot"}),
            )
        return ExecutionResult(
            task_id=task_id,
            executor="openclaw",
            status="submitted",
            result=f"OpenClaw task {task_id} is not present in the current status snapshot.",
            raw=self._redact_payload({"status": status, "poll_method": "status.snapshot"}),
        )

    def _result_text_for_run(self, task_id: str, wait_payload: dict[str, Any]) -> str:
        cached = self._run_cache.get(task_id) if isinstance(self._run_cache.get(task_id), dict) else {}
        cached_text = str(cached.get("result") or "").strip()
        if cached_text and not cached_text.startswith("Task submitted to OpenClaw."):
            return cached_text
        final_event = cached.get("final_event") if isinstance(cached.get("final_event"), dict) else {}
        text = self._message_text(final_event.get("message"))
        if text:
            return text
        try:
            history = self._fetch_chat_history(limit=12)
        except OpenClawGatewayError:
            history = {}
        return self._latest_assistant_text(history)

    def _latest_assistant_text(self, history: dict[str, Any]) -> str:
        messages = history.get("messages") if isinstance(history.get("messages"), list) else []
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower()
            if role not in {"assistant", "model"}:
                continue
            text = self._message_text(item)
            if text:
                return text
        return ""

    def _remember_chat_run(
        self,
        run_id: str,
        *,
        final_event: dict[str, Any] | None = None,
        status: str,
        result: str,
    ) -> None:
        with self._run_cache_lock:
            current = self._run_cache.get(run_id) if isinstance(self._run_cache.get(run_id), dict) else {}
            self._run_cache[run_id] = {
                **current,
                "run_id": run_id,
                "status": status,
                "result": result,
                "final_event": final_event or current.get("final_event") or {},
                "updated_at": time.time(),
            }

    def _cached_execution(self, task_id: str) -> ExecutionResult | None:
        with self._run_cache_lock:
            cached = self._run_cache.get(task_id)
        if not isinstance(cached, dict):
            return None
        status = str(cached.get("status") or "")
        if status not in {"success", "error"}:
            return None
        final_event = cached.get("final_event") if isinstance(cached.get("final_event"), dict) else {}
        return ExecutionResult(
            task_id=task_id,
            executor="openclaw",
            status=status,
            result=str(cached.get("result") or self._message_text(final_event.get("message")) or ""),
            raw=self._redact_payload({"cached_run": cached, "poll_method": "run_cache"}),
        )

    def _status_wait_ms(self) -> int:
        configured = self._float_env("OPENCLAW_STATUS_WAIT_MS", 800.0)
        return int(max(100.0, min(configured, 5000.0)))

    @staticmethod
    def _normalize_wait_status(status: str) -> str:
        normalized = status.lower().strip()
        if normalized in {"ok", "done", "completed", "final", "success"}:
            return "success"
        if normalized in {"error", "failed", "aborted"}:
            return "error"
        if normalized == "timeout":
            return "running"
        return "running"

    @staticmethod
    def _is_chat_run_id(task_id: str) -> bool:
        lowered = task_id.lower()
        return lowered.startswith("veyra-") or lowered.startswith("run-") or lowered.startswith("chat-")

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

    def _device_payload(self, device_identity: dict[str, Any] | None, nonce: str, *, scopes: list[str] | None = None) -> dict[str, Any] | None:
        if not device_identity or Ed25519PrivateKey is None or not nonce:
            return None
        client = self._connect_client()
        signed_at = int(time.time() * 1000)
        token = self.api_key or str(device_identity.get("token") or "")
        requested_scopes = scopes or self.scopes
        message = "|".join(
            [
                "v2",
                str(device_identity["deviceId"]),
                client["id"],
                client["mode"],
                "operator",
                ",".join(requested_scopes),
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

    def _find_task(self, payload: Any, task_id: str) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            candidates = [payload.get("runId"), payload.get("run_id"), payload.get("task_id"), payload.get("id")]
            if task_id in {str(item) for item in candidates if item is not None}:
                return payload
            for value in payload.values():
                found = self._find_task(value, task_id)
                if found:
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = self._find_task(item, task_id)
                if found:
                    return found
        return None

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
    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
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
