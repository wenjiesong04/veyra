from __future__ import annotations

import json
import os
import base64
import copy
import hashlib
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
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
from interface.agent_contract import (
    AGENT_CONTRACT_VERSION,
    TERMINAL_STATUSES,
    normalize_capabilities,
    normalize_execution_payload,
    validate_task_packet_payload,
)
from interface.event_schema import VeyraTaskPacket


DEFAULT_OPENCLAW_PROTOCOL_MIN = 3
DEFAULT_OPENCLAW_PROTOCOL_MAX = 4
OPENCLAW_REQUIRED_METHODS = ("chat.send",)
OPENCLAW_OPTIONAL_METHODS = ("health", "status", "tools.catalog", "skills.status", "agent.wait", "chat.history", "memory.summary", "memory.patch")
OPENCLAW_GOVERNANCE_PROTOCOL = "veyra.openclaw.governance.v1"
OPENCLAW_GOVERNANCE_IMPLEMENTATION_REVISION = (
    "veyra.openclaw.governance.phase3.v2"
)


class OpenClawGatewayError(RuntimeError):
    def __init__(self, status: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


def _ensure_crypto_available() -> bool:
    """Recover if cryptography becomes available after this module was imported."""
    global Ed25519PrivateKey, serialization
    if Ed25519PrivateKey is not None and serialization is not None:
        return True
    try:
        from cryptography.hazmat.primitives import serialization as loaded_serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as loaded_ed25519
    except ImportError:
        return False
    Ed25519PrivateKey = loaded_ed25519  # type: ignore[assignment]
    serialization = loaded_serialization  # type: ignore[assignment]
    return True


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
        agent_id: str | None = None,
        governance_dispatch_preparer: Callable[..., Any] | None = None,
        governance_dispatch_canceller: Callable[..., Any] | None = None,
        governance_run_evidence_resolver: Callable[..., Any] | None = None,
        governance_status_resolver: Callable[..., Any] | None = None,
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
        self.agent_id = self._openclaw_agent_id(
            agent_id or os.getenv("OPENCLAW_AGENT_ID", "main")
        )
        configured_wait = task_wait_timeout
        if configured_wait is None:
            configured_wait = self._float_env("OPENCLAW_TASK_WAIT_TIMEOUT", 75.0)
        self.task_wait_timeout = max(15.0, min(float(configured_wait), 300.0))
        self.scopes = self._scopes(os.getenv("OPENCLAW_SCOPES", "operator.read,operator.write"))
        self.memory_scopes = self._scopes(os.getenv("OPENCLAW_MEMORY_SCOPES", ",".join([*self.scopes, "operator.admin"])))
        self.device_store = Path(os.getenv("OPENCLAW_DEVICE_STORE", "state/local/openclaw_device.json"))
        self.workspace_memory_fallback_enabled = os.getenv("VEYRA_OPENCLAW_WORKSPACE_MEMORY_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
        self.openclaw_workspace = Path(os.getenv("OPENCLAW_WORKSPACE_DIR", str(Path.home() / ".openclaw" / "workspace"))).expanduser()
        self.openclaw_workspace_memory_dir = Path(
            os.getenv("OPENCLAW_WORKSPACE_MEMORY_DIR", str(self.openclaw_workspace / "memory"))
        ).expanduser()
        self.veyra_memory_mirror_dir = Path(os.getenv("VEYRA_OPENCLAW_MEMORY_MIRROR_DIR", "state/local/openclaw_memory_mirror")).expanduser()
        self.protocol_min = protocol_min if protocol_min is not None else self._int_env("OPENCLAW_PROTOCOL_MIN", DEFAULT_OPENCLAW_PROTOCOL_MIN)
        configured_max = protocol_max if protocol_max is not None else self._int_env("OPENCLAW_PROTOCOL_MAX", DEFAULT_OPENCLAW_PROTOCOL_MAX)
        self.protocol_max = max(self.protocol_min, configured_max)
        self._run_cache: dict[str, dict[str, Any]] = {}
        self._run_cache_lock = threading.Lock()
        self._governance_cleanup_lock = threading.Lock()
        self._capabilities_cache: dict[str, Any] = {}
        self._capabilities_cache_at = 0.0
        self._capabilities_cache_ttl = max(0.0, min(self._float_env("OPENCLAW_CAPABILITIES_CACHE_TTL", 5.0), 30.0))
        self._capabilities_cache_lock = threading.Lock()
        self._governance_dispatch_preparer = governance_dispatch_preparer
        self._governance_dispatch_canceller = governance_dispatch_canceller
        self._governance_run_evidence_resolver = (
            governance_run_evidence_resolver
        )
        self._governance_status_resolver = governance_status_resolver

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
        # Isolate each task in its own agent conversation window. The Veyra dialogue/user
        # session id (e.g. feishu:ou_xxx:oc_xxx) must never be the OpenClaw sessionKey, and
        # the fixed "main" key must not be shared across independent tasks.
        session_key = self._canonical_session_key(
            str(
                getattr(task_packet, "agent_execution_session_id", "")
                or ""
            ).strip()
            or self.session_key
        )
        task_context = self._task_context_for_packet(task_packet, session_key)
        run_id = f"veyra-{uuid4().hex}"
        governance_registration: dict[str, Any] | None = None
        if self._governance_dispatch_preparer is not None:
            try:
                prepared = self._governance_dispatch_preparer(
                    task_packet,
                    run_id=run_id,
                    session_key=session_key,
                )
                if hasattr(prepared, "plugin_payload"):
                    governance_registration = prepared.plugin_payload()
                elif isinstance(prepared, dict):
                    governance_registration = dict(prepared)
                else:
                    raise TypeError(
                        "governance dispatch preparer returned an invalid registration"
                    )
            except Exception as exc:
                self._remember_chat_run(
                    run_id,
                    status="blocked",
                    result="Governed OpenClaw registration failed.",
                    task_context=task_context,
                )
                cleanup = self._cancel_run_authority(
                    run_id,
                    reason="dispatch_registration_failed",
                    abort_chat=True,
                )
                return ExecutionResult(
                    task_id=run_id,
                    executor="openclaw",
                    status="blocked",
                    result=(
                        "OpenClaw dispatch was blocked because Veyra could not "
                        "establish a governed session."
                    ),
                    logs=prompt,
                    raw=self._redact_payload(
                        {
                            "task_context": task_context,
                            "governance": {
                                "status": "registration_failed",
                                "reason": str(exc)[:600],
                            },
                            "governance_cleanup": cleanup,
                        }
                    ),
                )
        governance_identity = self._normalized_governance_identity(
            governance_registration,
            run_id=run_id,
            fallback_session_key=session_key,
        )
        if (
            governance_registration is not None
            and not self._complete_governance_identity(governance_identity)
        ):
            self._remember_chat_run(
                run_id,
                status="blocked",
                result="Governed OpenClaw registration identity was invalid.",
                task_context=task_context,
                governance_identity=governance_identity,
            )
            cleanup = self._cancel_run_authority(
                run_id,
                reason="dispatch_identity_invalid",
                identity=governance_identity,
                abort_chat=True,
            )
            return ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status="blocked",
                result=(
                    "OpenClaw dispatch was blocked because its governed "
                    "run identity was incomplete or inconsistent."
                ),
                logs=prompt,
                raw=self._redact_payload(
                    {
                        "task_context": task_context,
                        "governance": {
                            "status": "registration_identity_invalid",
                        },
                        "governance_cleanup": cleanup,
                    }
                ),
            )
        try:
            if governance_registration is None:
                raw = self._send_chat(
                    prompt,
                    session_key=session_key,
                )
            else:
                raw = self._send_chat(
                    prompt,
                    session_key=session_key,
                    idempotency_key=run_id,
                    governance_registration=governance_registration,
                )
        except OpenClawGatewayError as exc:
            if governance_registration is None:
                return ExecutionResult(
                    task_id=task_packet.task_id,
                    executor="openclaw",
                    status=exc.status,
                    result=f"OpenClaw gateway request failed: {exc.message}",
                    logs=prompt,
                    raw=self._redact_payload(
                        {
                            "task_context": task_context,
                            "gateway_url": self.gateway_url,
                            "error": exc.details,
                        }
                    ),
                )
            self._remember_chat_run(
                run_id,
                status=exc.status,
                result=f"OpenClaw gateway request failed: {exc.message}",
                task_context=task_context,
                governance_identity=governance_identity,
            )
            cleanup = self._cancel_run_authority(
                run_id,
                reason="gateway_submission_failed",
                identity=governance_identity,
                abort_chat=True,
            )
            return ExecutionResult(
                task_id=run_id,
                executor="openclaw",
                status=exc.status,
                result=f"OpenClaw gateway request failed: {exc.message}",
                logs=prompt,
                raw=self._redact_payload(
                    {
                        "task_context": task_context,
                        "gateway_url": self.gateway_url,
                        "error": exc.details,
                        "governance_cleanup": cleanup,
                    }
                ),
            )
        final = raw.get("final_event") or {}
        text = self._message_text(final.get("message"))
        run_id = str(raw.get("run_id") or raw.get("chat_send", {}).get("runId") or run_id)
        execution_raw = {**raw, "task_context": task_context}
        agent_response = self._structured_agent_response(text)
        if agent_response:
            execution_raw["agent_response"] = agent_response
        self._remember_chat_run(
            run_id,
            final_event=final,
            status="running",
            result=f"Task submitted to OpenClaw. run_id={run_id}",
            task_context=task_context,
            agent_response=agent_response,
            governance_identity=governance_identity,
        )
        if final.get("state") == "final":
            execution = self._project_terminal_execution(
                ExecutionResult(
                    task_id=run_id,
                    executor="openclaw",
                    status="success",
                    result=text or "OpenClaw finished with no text.",
                    raw=execution_raw,
                ),
                projection_source="chat.send.final",
            )
            self._remember_chat_run(
                run_id,
                final_event=final,
                status="success",
                result=text,
                task_context=task_context,
                agent_response=agent_response,
                changed_files=execution.changed_files,
                tool_calls=execution.tool_calls,
                governance_identity=governance_identity,
            )
            return execution
        if final.get("state") == "error":
            execution = self._project_terminal_execution(
                ExecutionResult(
                    task_id=run_id,
                    executor="openclaw",
                    status="error",
                    result=str(
                        final.get("errorMessage")
                        or "OpenClaw task failed."
                    ),
                    raw=execution_raw,
                ),
                projection_source="chat.send.error",
            )
            self._remember_chat_run(
                run_id,
                final_event=final,
                status="error",
                result=execution.result,
                task_context=task_context,
                changed_files=execution.changed_files,
                tool_calls=execution.tool_calls,
                governance_identity=governance_identity,
            )
            return execution
        self._remember_chat_run(
            run_id,
            final_event=final,
            status="submitted",
            result=f"Task submitted to OpenClaw. run_id={run_id}",
            task_context=task_context,
            governance_identity=governance_identity,
        )
        return ExecutionResult(
            task_id=run_id,
            executor="openclaw",
            status="submitted",
            result=f"Task submitted to OpenClaw. run_id={run_id}",
            raw=self._redact_payload(execution_raw),
        )

    def fetch_capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
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
        if not force_refresh:
            cached = self._cached_capabilities()
            if cached:
                return cached
        try:
            capabilities = normalize_capabilities(self._gateway_snapshot(), runtime="openclaw", base_url=self.gateway_url)
        except OpenClawGatewayError as exc:
            capabilities = normalize_capabilities(
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
        self._remember_capabilities(capabilities)
        return copy.deepcopy(capabilities)

    def invalidate_capabilities_cache(self) -> None:
        with self._capabilities_cache_lock:
            self._capabilities_cache = {}
            self._capabilities_cache_at = 0.0

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        if not self.gateway_url:
            return {"session_id": session_id, "summary": "", "runtime": "openclaw", "status": "not_configured"}
        try:
            response = self._gateway_request("memory.summary", {"sessionId": session_id, "sessionKey": self.session_key}, scopes=self.memory_scopes)
        except OpenClawGatewayError as exc:
            return self._workspace_memory_summary(session_id, gateway_error={"status": exc.status, "message": exc.message})
        summary = response.get("summary") or response.get("text") or response.get("memory") or ""
        if str(response.get("status") or "") in {"method_unavailable", "unknown_method", "unsupported"}:
            return self._workspace_memory_summary(session_id, gateway_error={"status": str(response.get("status")), "message": "memory.summary unavailable"})
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
            return self._workspace_memory_patch(memory_patch, gateway_error={"status": exc.status, "message": exc.message})
        if str(response.get("status") or "") in {"method_unavailable", "unknown_method", "unsupported"}:
            return self._workspace_memory_patch(memory_patch, gateway_error={"status": str(response.get("status")), "message": "memory.patch unavailable"})
        return {"status": str(response.get("status") or "submitted"), "runtime": "openclaw", "raw": self._redact_payload(response)}

    def _workspace_memory_summary(self, session_id: str, *, gateway_error: dict[str, Any]) -> dict[str, Any]:
        if not self.workspace_memory_fallback_enabled:
            return {
                "session_id": session_id,
                "summary": "",
                "runtime": "openclaw",
                "status": str(gateway_error.get("status") or "memory_unavailable"),
                "error": str(gateway_error.get("message") or "OpenClaw memory RPC unavailable"),
                "fallback": {"enabled": False},
            }
        files = self._workspace_memory_files()
        chunks: list[str] = []
        for path in files:
            text = self._read_workspace_memory_file(path)
            if text:
                chunks.append(f"# {self._display_path(path)}\n{text}")
        summary = "\n\n".join(chunks).strip()
        max_chars = self._int_env("VEYRA_OPENCLAW_MEMORY_SUMMARY_MAX_CHARS", 6000)
        if len(summary) > max_chars:
            summary = summary[-max_chars:]
        status = "workspace_file_fallback" if summary else "workspace_file_empty"
        result = {
            "session_id": session_id,
            "summary": summary,
            "runtime": "openclaw",
            "status": status,
            "freshness": "fresh" if summary else "stale",
            "trust": "workspace_file_fallback" if summary else "untrusted",
            "source": "openclaw_workspace_files",
            "sync_status": "pending_gateway_support",
            "files": [self._display_path(path) for path in files],
            "gateway_error": self._redact_payload(gateway_error),
        }
        self._audit_workspace_memory("summary", status, result)
        return result

    def _workspace_memory_patch(self, memory_patch: dict[str, Any], *, gateway_error: dict[str, Any]) -> dict[str, Any]:
        if not self.workspace_memory_fallback_enabled:
            return {
                "status": str(gateway_error.get("status") or "memory_unavailable"),
                "runtime": "openclaw",
                "error": str(gateway_error.get("message") or "OpenClaw memory RPC unavailable"),
                "fallback": {"enabled": False},
            }
        sanitized = self._redact_payload(memory_patch if isinstance(memory_patch, dict) else {"patch": memory_patch})
        now = self._utc_now()
        note = self._memory_patch_note(sanitized, now=now)
        target = self.openclaw_workspace_memory_dir / f"{now[:10]}-veyra.md"
        write_target = target
        mode = "workspace_file"
        try:
            self._append_text(write_target, note)
        except OSError as exc:
            mode = "local_mirror"
            write_target = self.veyra_memory_mirror_dir / f"{now[:10]}-veyra.md"
            try:
                self._append_text(write_target, note + f"\n\nfallback_error: {type(exc).__name__}: {str(exc)[:300]}\n")
            except OSError as mirror_exc:
                result = {
                    "status": "error",
                    "runtime": "openclaw",
                    "error": f"workspace and local mirror memory fallback failed: {mirror_exc}",
                    "sync_status": "pending_gateway_support",
                    "gateway_error": self._redact_payload(gateway_error),
                }
                self._audit_workspace_memory("patch", "error", result)
                return result
        memory_id = "owm_" + hashlib.sha256(json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        result = {
            "status": "workspace_file_fallback",
            "runtime": "openclaw",
            "memory_id": memory_id,
            "path": str(write_target),
            "fallback_mode": mode,
            "sync_status": "pending_gateway_support",
            "gateway_error": self._redact_payload(gateway_error),
        }
        self._audit_workspace_memory("patch", "workspace_file_fallback", result)
        return result

    def _workspace_memory_files(self) -> list[Path]:
        explicit = os.getenv("OPENCLAW_WORKSPACE_MEMORY_FILES", "")
        files: list[Path] = []
        if explicit:
            for item in explicit.split(","):
                path = Path(item.strip()).expanduser()
                if path.is_file():
                    files.append(path)
        memory_md = self.openclaw_workspace / "MEMORY.md"
        if memory_md.is_file():
            files.append(memory_md)
        if self.openclaw_workspace_memory_dir.is_dir():
            dated = sorted(
                [
                    path
                    for path in self.openclaw_workspace_memory_dir.glob("*.md")
                    if path.is_file() and self._looks_like_memory_note(path.name)
                ],
                key=lambda path: path.stat().st_mtime,
            )
            files.extend(dated[-20:])
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in files:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _looks_like_memory_note(self, name: str) -> bool:
        if name == "MEMORY.md":
            return True
        return bool(re.match(r"\d{4}-\d{2}-\d{2}(?:[-_].*)?\.md$", name))

    def _read_workspace_memory_file(self, path: Path) -> str:
        try:
            max_bytes = max(1000, self._int_env("VEYRA_OPENCLAW_MEMORY_FILE_MAX_BYTES", 12000))
            raw = path.read_bytes()
            if len(raw) > max_bytes:
                raw = raw[-max_bytes:]
            return raw.decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _memory_patch_note(self, sanitized_patch: Any, *, now: str) -> str:
        patch = sanitized_patch if isinstance(sanitized_patch, dict) else {"patch": sanitized_patch}
        summary = str(patch.get("summary") or patch.get("result") or patch.get("task") or "Veyra memory patch").strip()
        topic = str(patch.get("topic") or patch.get("task") or patch.get("memory_type") or "general").strip()
        session_id = str(patch.get("session_id") or "").strip()
        return (
            f"\n\n## Veyra memory patch - {now}\n\n"
            "sync_status: pending_gateway_support\n"
            "source: veyra_workspace_file_fallback\n"
            f"session_id: {session_id[:180]}\n"
            f"topic: {topic[:180]}\n"
            f"summary: {summary[:500]}\n\n"
            "```json\n"
            f"{json.dumps(patch, ensure_ascii=False, indent=2)[:4000]}\n"
            "```\n"
        )

    def _append_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def _audit_workspace_memory(self, action: str, status: str, payload: dict[str, Any]) -> None:
        state_root = Path(os.getenv("VEYRA_STATE_DIR") or os.getenv("VEYRA_STATE_ROOT") or "state")
        audit_path = state_root / "logs" / "openclaw_workspace_memory_fallback.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": self._utc_now(),
            "runtime": "openclaw",
            "route": "openclaw_workspace_memory_fallback",
            "action": action,
            "status": status,
            "payload": self._redact_payload(payload),
        }
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.expanduser().resolve())
        except OSError:
            return str(path)

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

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

    def receive_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        """Normalize external results without trusting Agent-reported effects."""

        payload = normalize_execution_payload(
            raw_result,
            default_task_id="unknown",
            default_executor="openclaw",
            default_status="submitted",
        )
        execution = ExecutionResult(**payload)
        if execution.status in TERMINAL_STATUSES:
            return self._project_terminal_execution(
                execution,
                projection_source="receive_result",
            )
        # A non-terminal Agent payload cannot establish that any effect occurred.
        execution.changed_files = []
        execution.tool_calls = []
        execution.raw = self._redact_payload(
            {
                **execution.raw,
                "governed_tool_evidence": {
                    "status": "pending_terminal_projection",
                    "authority_source": "veyra_private_tool_ledger",
                    "observed_call_count": 0,
                    "effect_count": 0,
                },
                "tool_receipt_refs": [],
            }
        )
        return execution

    def stop_task(self, task_id: str) -> bool:
        if self._governance_dispatch_canceller is None:
            if not self.gateway_url:
                return False
            with self._run_cache_lock:
                cached = copy.deepcopy(
                    self._run_cache.get(str(task_id or "").strip()) or {}
                )
            task_context = (
                cached.get("task_context")
                if isinstance(cached.get("task_context"), dict)
                else {}
            )
            session_key = str(
                task_context.get("agent_execution_session_id")
                or task_context.get("session_key")
                or ""
            ).strip()
            if not session_key:
                return False
            try:
                response = self._gateway_request(
                    "chat.abort",
                    {
                        "sessionKey": self._canonical_session_key(
                            session_key
                        ),
                        "agentId": self.agent_id,
                        "runId": task_id,
                    },
                )
            except (OpenClawGatewayError, ValueError):
                return False
            return self._confirmed_chat_abort(response)
        cleanup = self._cancel_run_authority(
            task_id,
            reason="user_requested_stop",
            abort_chat=True,
        )
        return cleanup.get("status") == "cancelled"

    def connection_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        try:
            capabilities = self.fetch_capabilities(force_refresh=force_refresh)
        except Exception as exc:
            capabilities = normalize_capabilities(
                {
                    "runtime": "openclaw",
                    "status": "unavailable",
                    "connected": False,
                    "error": str(exc),
                    "protocol": "openclaw_gateway_ws",
                },
                runtime="openclaw",
                base_url=self.gateway_url,
            )
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
            "auth_diagnostics": self._auth_diagnostics(),
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

    def _cached_capabilities(self) -> dict[str, Any]:
        if self._capabilities_cache_ttl <= 0:
            return {}
        with self._capabilities_cache_lock:
            if not self._capabilities_cache:
                return {}
            if (time.monotonic() - self._capabilities_cache_at) > self._capabilities_cache_ttl:
                return {}
            return copy.deepcopy(self._capabilities_cache)

    def _remember_capabilities(self, capabilities: dict[str, Any]) -> None:
        with self._capabilities_cache_lock:
            self._capabilities_cache = copy.deepcopy(capabilities)
            self._capabilities_cache_at = time.monotonic()

    def _send_chat(
        self,
        message: str,
        *,
        session_key: str | None = None,
        idempotency_key: str | None = None,
        governance_registration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        active_session_key = str(session_key or "").strip() or self.session_key
        with self._open_socket() as ws:
            hello = self._connect(ws, events)
            active_run_id = (
                str(idempotency_key or "").strip()
                or f"veyra-{uuid4().hex}"
            )
            governance_status: dict[str, Any] | None = None
            if governance_registration is not None:
                governance_status = self._request_on_socket(
                    ws,
                    "veyra.governance.registerSession",
                    governance_registration,
                    events,
                )
                if governance_status.get("status") not in {
                    "registered",
                    "active",
                    "ok",
                    "success",
                }:
                    raise OpenClawGatewayError(
                        "blocked",
                        "OpenClaw governance plugin rejected the dispatch",
                        self._redact_payload(governance_status),
                    )
            chat_send = self._request_on_socket(
                ws,
                "chat.send",
                {
                    "sessionKey": active_session_key,
                    "agentId": self.agent_id,
                    "message": message,
                    "idempotencyKey": active_run_id,
                },
                events,
            )
            run_id = str(chat_send.get("runId") or active_run_id)
            if run_id != active_run_id:
                raise OpenClawGatewayError(
                    "blocked",
                    "OpenClaw runId did not preserve the governed idempotency key",
                    {
                        "expected_run_id": active_run_id,
                        "observed_run_id": run_id,
                    },
                )
            final_event = self._wait_for_chat_final(ws, run_id, events)
        return self._redact_payload(
            {
                "hello": self._hello_summary(hello),
                "chat_send": chat_send,
                "run_id": run_id,
                "governance_registration": (
                    {
                        "status": governance_status.get("status"),
                        "runId": run_id,
                    }
                    if isinstance(governance_status, dict)
                    else None
                ),
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
            try:
                governance_plugin = self._request_on_socket(
                    ws,
                    "veyra.governance.status",
                    {},
                    events,
                )
            except Exception as exc:
                governance_plugin = {
                    "status": "unavailable",
                    "reason": (
                        exc.message
                        if isinstance(exc, OpenClawGatewayError)
                        else str(exc)[:600]
                    ),
                }
        try:
            governance_runtime = (
                self._governance_status_resolver()
                if self._governance_status_resolver is not None
                else {
                    "status": "not_configured",
                    "tool_proxy_enforced": False,
                }
            )
        except Exception as exc:
            governance_runtime = {
                "status": "degraded",
                "tool_proxy_enforced": False,
                "reason": str(exc)[:600],
            }
        plugin_active = governance_plugin.get("status") in {
            "active",
            "ok",
            "success",
            "validated",
        }
        runtime_implementation = (
            governance_runtime.get("implementation")
            if isinstance(
                governance_runtime.get("implementation"),
                dict,
            )
            else {}
        )
        governance_identity_match = bool(
            governance_plugin.get("protocol_version")
            == OPENCLAW_GOVERNANCE_PROTOCOL
            and governance_plugin.get("implementation_revision")
            == OPENCLAW_GOVERNANCE_IMPLEMENTATION_REVISION
            and runtime_implementation.get("plugin_protocol")
            == OPENCLAW_GOVERNANCE_PROTOCOL
            and runtime_implementation.get(
                "plugin_implementation_revision"
            )
            == OPENCLAW_GOVERNANCE_IMPLEMENTATION_REVISION
        )
        scoped_enforcement = bool(
            plugin_active
            and governance_identity_match
            and governance_runtime.get("canary_validated") is True
        )
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
                "tool_proxy_enforced": scoped_enforcement,
                "tool_proxy_contract_advertised": True,
                "tool_proxy_enforcement_status": (
                    "validated"
                    if scoped_enforcement
                    else "validation_pending"
                ),
                "tool_proxy_enforcement_scope": (
                    "veyra_governed_openclaw_sessions"
                ),
                "tool_proxy_identity_match": (
                    governance_identity_match
                ),
            },
            "governance_plugin": self._redact_payload(governance_plugin),
            "governance_runtime": self._redact_payload(governance_runtime),
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
                    task_context=(
                        execution.raw.get("task_context")
                        if isinstance(execution.raw.get("task_context"), dict)
                        else None
                    ),
                    agent_response=(
                        execution.raw.get("agent_response")
                        if isinstance(execution.raw.get("agent_response"), dict)
                        else None
                    ),
                    changed_files=execution.changed_files,
                    tool_calls=execution.tool_calls,
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
                task_context=(
                    execution.raw.get("task_context")
                    if isinstance(execution.raw.get("task_context"), dict)
                    else None
                ),
                agent_response=(
                    execution.raw.get("agent_response")
                    if isinstance(execution.raw.get("agent_response"), dict)
                    else None
                ),
                changed_files=execution.changed_files,
                tool_calls=execution.tool_calls,
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

    def _fetch_chat_history(self, *, session_key: str, limit: int = 8) -> dict[str, Any]:
        active_session_key = str(session_key or "").strip()
        if not active_session_key:
            return {}
        payload = self._gateway_request(
            "chat.history",
            {
                "sessionKey": active_session_key,
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
                raw=self._redact_payload(
                    self._execution_raw_for_run(
                        task_id,
                        {"agent_wait": wait_payload, "poll_method": "agent.wait"},
                    )
                ),
            )
        if normalized == "error":
            error_text = str(wait_payload.get("error") or wait_payload.get("stopReason") or "OpenClaw task failed.")
            return self._project_terminal_execution(
                ExecutionResult(
                    task_id=task_id,
                    executor="openclaw",
                    status="error",
                    result=error_text,
                    raw=self._execution_raw_for_run(
                        task_id,
                        {
                            "agent_wait": wait_payload,
                            "poll_method": "agent.wait",
                        },
                        result_text=error_text,
                    ),
                ),
                projection_source="agent.wait.error",
            )

        result_text = self._result_text_for_run(task_id, wait_payload)
        execution_raw = self._execution_raw_for_run(
            task_id,
            {"agent_wait": wait_payload, "poll_method": "agent.wait"},
            result_text=result_text,
        )
        return self._project_terminal_execution(
            ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="success",
                result=result_text or "OpenClaw finished with no text.",
                raw=execution_raw,
            ),
            projection_source="agent.wait.final",
        )

    def _execution_from_chat_event(self, task_id: str, final_event: dict[str, Any]) -> ExecutionResult:
        text = self._message_text(final_event.get("message"))
        execution_raw = self._execution_raw_for_run(
            task_id,
            {"final_event": final_event, "poll_method": "chat.events"},
            result_text=text,
        )
        if final_event.get("state") == "error":
            return self._project_terminal_execution(
                ExecutionResult(
                    task_id=task_id,
                    executor="openclaw",
                    status="error",
                    result=str(
                        final_event.get("errorMessage")
                        or "OpenClaw task failed."
                    ),
                    raw=execution_raw,
                ),
                projection_source="chat.events.error",
            )
        return self._project_terminal_execution(
            ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="success",
                result=text or "OpenClaw finished with no text.",
                raw=execution_raw,
            ),
            projection_source="chat.events.final",
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
                raw=self._redact_payload(
                    self._execution_raw_for_run(
                        task_id,
                        {"error": exc.details, "poll_method": "status.snapshot"},
                    )
                ),
            )
        known_task = self._find_task(status, task_id)
        if known_task:
            state = str(
                known_task.get("state")
                or known_task.get("status")
                or "running"
            ).strip().lower()
            normalized = (
                "success"
                if state in {"final", "done", "completed", "success", "ok"}
                else "error"
                if state in {"error", "failed", "aborted"}
                else "running"
            )
            execution = ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status=normalized,
                result=str(known_task.get("result") or known_task.get("message") or f"OpenClaw task {task_id} is {normalized}."),
                raw=self._redact_payload(
                    self._execution_raw_for_run(
                        task_id,
                        {"task": known_task, "status": status, "poll_method": "status.snapshot"},
                        result_text=str(known_task.get("result") or known_task.get("message") or ""),
                    )
                ),
            )
            if normalized in TERMINAL_STATUSES:
                return self._project_terminal_execution(
                    execution,
                    projection_source="status.snapshot",
                )
            return execution
        tasks = status.get("tasks") if isinstance(status.get("tasks"), dict) else {}
        active = tasks.get("active")
        if isinstance(active, int) and active > 0:
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="running",
                result=f"OpenClaw has {active} active task(s); task {task_id} has not reached a final event yet.",
                raw=self._redact_payload(
                    self._execution_raw_for_run(
                        task_id,
                        {"status": status, "poll_method": "status.snapshot"},
                    )
                ),
            )
        if self._is_chat_run_id(task_id):
            return ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="running",
                result=f"OpenClaw chat run {task_id} is still in progress; gateway status snapshot has no per-run detail.",
                raw=self._redact_payload(
                    self._execution_raw_for_run(
                        task_id,
                        {"status": status, "poll_method": "status.snapshot"},
                    )
                ),
            )
        return ExecutionResult(
            task_id=task_id,
            executor="openclaw",
            status="submitted",
            result=f"OpenClaw task {task_id} is not present in the current status snapshot.",
            raw=self._redact_payload(
                self._execution_raw_for_run(
                    task_id,
                    {"status": status, "poll_method": "status.snapshot"},
                )
            ),
        )

    def _result_text_for_run(self, task_id: str, wait_payload: dict[str, Any]) -> str:
        with self._run_cache_lock:
            cached = dict(self._run_cache.get(task_id) or {})
        cached_text = str(cached.get("result") or "").strip()
        if cached_text and not cached_text.startswith("Task submitted to OpenClaw."):
            return cached_text
        final_event = cached.get("final_event") if isinstance(cached.get("final_event"), dict) else {}
        text = self._message_text(final_event.get("message"))
        if text:
            return text
        task_context = cached.get("task_context") if isinstance(cached.get("task_context"), dict) else {}
        session_key = str(task_context.get("agent_execution_session_id") or task_context.get("session_key") or "").strip()
        if not session_key:
            # Never fall back to the shared "main" history for a task whose isolated
            # execution session cannot be resolved.
            return ""
        try:
            history = self._fetch_chat_history(session_key=session_key, limit=12)
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
        task_context: dict[str, Any] | None = None,
        agent_response: dict[str, Any] | None = None,
        changed_files: list[str] | None = None,
        tool_calls: list[str] | None = None,
        governance_identity: dict[str, str] | None = None,
    ) -> None:
        with self._run_cache_lock:
            current = self._run_cache.get(run_id) if isinstance(self._run_cache.get(run_id), dict) else {}
            self._run_cache[run_id] = {
                **current,
                "run_id": run_id,
                "status": status,
                "result": result,
                "final_event": final_event or current.get("final_event") or {},
                "task_context": task_context or current.get("task_context") or {},
                "agent_response": agent_response or current.get("agent_response") or {},
                "changed_files": changed_files if changed_files is not None else current.get("changed_files") or [],
                "tool_calls": tool_calls if tool_calls is not None else current.get("tool_calls") or [],
                "governance_identity": (
                    governance_identity
                    if governance_identity
                    else current.get("governance_identity") or {}
                ),
                "updated_at": time.time(),
            }

    def _execution_raw_for_run(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        result_text: str = "",
    ) -> dict[str, Any]:
        with self._run_cache_lock:
            cached = dict(self._run_cache.get(task_id) or {})
        raw = dict(payload)
        task_context = cached.get("task_context")
        if isinstance(task_context, dict) and task_context:
            raw["task_context"] = task_context
        agent_response = self._structured_agent_response(result_text)
        if not agent_response and isinstance(cached.get("agent_response"), dict):
            agent_response = cached["agent_response"]
        if agent_response:
            raw["agent_response"] = agent_response
        return raw

    def _cancel_run_authority(
        self,
        run_id: str,
        *,
        reason: str,
        identity: dict[str, Any] | None = None,
        abort_chat: bool,
    ) -> dict[str, Any]:
        """Close Veyra authority before cancelling plugin and Agent state."""

        with self._governance_cleanup_lock:
            return self._cancel_run_authority_locked(
                run_id,
                reason=reason,
                identity=identity,
                abort_chat=abort_chat,
            )

    def _cancel_run_authority_locked(
        self,
        run_id: str,
        *,
        reason: str,
        identity: dict[str, Any] | None,
        abort_chat: bool,
    ) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return {
                "status": "cancellation_unconfirmed",
                "run_id": "",
                "reason": "run id is required for authoritative cancellation",
                "authority_revoked": False,
                "plugin_authority_closed": False,
                "agent_abort_confirmed": False,
            }

        with self._run_cache_lock:
            cached = copy.deepcopy(
                self._run_cache.get(normalized_run_id) or {}
            )
        task_context = (
            cached.get("task_context")
            if isinstance(cached.get("task_context"), dict)
            else {}
        )
        cached_identity = (
            cached.get("governance_identity")
            if isinstance(cached.get("governance_identity"), dict)
            else {}
        )
        merged_identity = {
            **cached_identity,
            **(identity if isinstance(identity, dict) else {}),
        }
        governance_identity = self._normalized_governance_identity(
            merged_identity,
            run_id=normalized_run_id,
            fallback_session_key=str(
                task_context.get("agent_execution_session_id")
                or task_context.get("session_key")
                or ""
            ),
        )
        prior = (
            cached.get("governance_cleanup")
            if isinstance(cached.get("governance_cleanup"), dict)
            else {}
        )

        prior_broker = (
            prior.get("broker")
            if isinstance(prior.get("broker"), dict)
            else {}
        )
        if prior_broker.get("status") == "cancelled":
            broker = copy.deepcopy(prior_broker)
        elif self._governance_dispatch_canceller is None:
            broker = {
                "status": "not_configured",
                "reason": "trusted Veyra dispatch canceller is not configured",
            }
        else:
            try:
                response = self._governance_dispatch_canceller(
                    normalized_run_id,
                    reason=reason,
                )
                broker = self._normalized_broker_cancellation(
                    response,
                    run_id=normalized_run_id,
                )
            except Exception as exc:
                broker = {
                    "status": "failed",
                    "reason": str(exc)[:600],
                }

        broker_digest = str(broker.get("binding_digest") or "").strip()
        identity_digest = str(
            governance_identity.get("binding_digest") or ""
        ).strip()
        identity_conflict = bool(
            broker_digest
            and identity_digest
            and broker_digest != identity_digest
        )
        if broker_digest and not identity_digest:
            governance_identity["binding_digest"] = broker_digest

        prior_plugin = (
            prior.get("plugin")
            if isinstance(prior.get("plugin"), dict)
            else {}
        )
        plugin_closed_statuses = {"cancelled", "absent"}
        if (
            prior_plugin.get("status") in plugin_closed_statuses
            and self._complete_governance_identity(governance_identity)
            and prior.get("identity") == governance_identity
        ):
            plugin = copy.deepcopy(prior_plugin)
        elif identity_conflict:
            plugin = {
                "status": "identity_mismatch",
                "reason": (
                    "broker binding digest does not match the registered "
                    "OpenClaw session identity"
                ),
            }
        elif not self._complete_governance_identity(governance_identity):
            plugin = {
                "status": "identity_unavailable",
                "reason": (
                    "exact sessionKey, runId and bindingDigest are required"
                ),
            }
        elif not self.gateway_url:
            plugin = {
                "status": "not_configured",
                "reason": "OpenClaw gateway is not configured",
            }
        else:
            plugin = self._cancel_plugin_session(governance_identity)

        prior_abort = (
            prior.get("agent_abort")
            if isinstance(prior.get("agent_abort"), dict)
            else {}
        )
        if not abort_chat:
            agent_abort = (
                copy.deepcopy(prior_abort)
                if prior_abort.get("status") == "aborted"
                else {"status": "not_requested"}
            )
        elif prior_abort.get("status") == "aborted":
            agent_abort = copy.deepcopy(prior_abort)
        elif not self.gateway_url:
            agent_abort = {
                "status": "not_configured",
                "reason": "OpenClaw gateway is not configured",
            }
        elif not str(
            governance_identity.get("session_key") or ""
        ).strip():
            agent_abort = {
                "status": "identity_unavailable",
                "reason": (
                    "OpenClaw chat.abort requires the exact session key"
                ),
            }
        else:
            try:
                response = self._gateway_request(
                    "chat.abort",
                    {
                        "sessionKey": self._canonical_session_key(
                            str(
                                governance_identity.get("session_key")
                                or ""
                            )
                        ),
                        "agentId": self.agent_id,
                        "runId": normalized_run_id,
                    },
                )
                if self._confirmed_chat_abort(response):
                    agent_abort = {
                        "status": "aborted",
                        "response": self._redact_payload(response),
                    }
                else:
                    agent_abort = {
                        "status": "unconfirmed",
                        "reason": (
                            "OpenClaw did not confirm that the run was aborted"
                        ),
                        "response": self._redact_payload(response),
                    }
            except Exception as exc:
                agent_abort = {
                    "status": "failed",
                    "reason": str(exc)[:600],
                }

        authority_revoked = broker.get("status") == "cancelled"
        plugin_authority_closed = (
            plugin.get("status") in plugin_closed_statuses
        )
        agent_abort_confirmed = (
            not abort_chat or agent_abort.get("status") == "aborted"
        )
        if (
            authority_revoked
            and plugin_authority_closed
            and agent_abort_confirmed
        ):
            status = "cancelled"
        elif broker.get("status") == "too_late":
            status = "too_late"
        else:
            status = "cancellation_unconfirmed"
        cleanup = {
            "status": status,
            "run_id": normalized_run_id,
            "reason": str(reason or "dispatch_cancelled")[:600],
            "authority_revoked": authority_revoked,
            "plugin_authority_closed": plugin_authority_closed,
            "agent_abort_confirmed": agent_abort_confirmed,
            "broker": broker,
            "plugin": plugin,
            "agent_abort": agent_abort,
            "identity": governance_identity,
        }
        with self._run_cache_lock:
            current = (
                self._run_cache.get(normalized_run_id)
                if isinstance(
                    self._run_cache.get(normalized_run_id),
                    dict,
                )
                else {}
            )
            self._run_cache[normalized_run_id] = {
                **current,
                "run_id": normalized_run_id,
                "governance_identity": (
                    governance_identity
                    if governance_identity
                    else current.get("governance_identity") or {}
                ),
                "governance_cleanup": copy.deepcopy(cleanup),
                "updated_at": time.time(),
            }
        return self._redact_payload(cleanup)

    def _cancel_plugin_session(
        self,
        identity: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = self._gateway_request(
                "veyra.governance.cancelSession",
                {
                    "sessionKey": identity["session_key"],
                    "runId": identity["run_id"],
                    "bindingDigest": identity["binding_digest"],
                },
            )
        except Exception as exc:
            return {
                "status": "failed",
                "reason": str(exc)[:600],
            }
        if not isinstance(response, dict):
            return {
                "status": "failed",
                "reason": "OpenClaw cancellation returned a non-object",
            }
        response_run_id = str(
            response.get("runId") or response.get("run_id") or ""
        ).strip()
        response_session_key = str(
            response.get("sessionKey")
            or response.get("session_key")
            or ""
        ).strip()
        if (
            response_run_id != identity["run_id"]
            or response_session_key != identity["session_key"]
        ):
            return {
                "status": "identity_mismatch",
                "reason": (
                    "OpenClaw cancellation response did not preserve the "
                    "exact run and session identity"
                ),
            }
        cancelled = response.get("cancelled")
        idempotent = response.get("idempotent") is True
        if cancelled is True:
            status = "cancelled"
        elif cancelled is False and idempotent:
            status = "absent"
        else:
            status = "unconfirmed"
        return {
            "status": status,
            "idempotent": idempotent,
            "response": self._redact_payload(response),
        }

    @staticmethod
    def _confirmed_chat_abort(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        if response.get("aborted") is False or response.get("ok") is False:
            return False
        if response.get("aborted") is True or response.get("ok") is True:
            return True
        return str(response.get("status") or "").strip().lower() in {
            "aborted",
            "cancelled",
            "ok",
            "success",
        }

    @staticmethod
    def _normalized_broker_cancellation(
        response: Any,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            return {
                "status": "failed",
                "reason": "Veyra dispatch cancellation returned a non-object",
            }
        response_run_id = str(
            response.get("run_id") or response.get("runId") or ""
        ).strip()
        digest = str(
            response.get("binding_digest")
            or response.get("bindingDigest")
            or ""
        ).strip()
        if response_run_id != run_id:
            return {
                "status": "identity_mismatch",
                "reason": "Veyra dispatch cancellation belongs to another run",
            }
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return {
                "status": "identity_mismatch",
                "reason": "Veyra dispatch cancellation has no exact binding",
            }
        status = str(response.get("status") or "").strip().lower()
        if status not in {"cancelled", "too_late"}:
            return {
                "status": status or "failed",
                "reason": str(
                    response.get("reason")
                    or "Veyra did not confirm authority revocation"
                )[:600],
                "binding_digest": digest,
            }
        return {
            "status": status,
            "binding_digest": digest,
            "revoked_grants": response.get("revoked_grants", 0),
            "executing_reservations": OpenClawAdapter._string_items(
                response.get("executing_reservations")
            ),
            "cancelled_reservations": OpenClawAdapter._string_items(
                response.get("cancelled_reservations")
            ),
        }

    @staticmethod
    def _normalized_governance_identity(
        value: Any,
        *,
        run_id: str,
        fallback_session_key: str = "",
    ) -> dict[str, str]:
        normalized_run_id = str(run_id or "").strip()
        source = value if isinstance(value, dict) else {}
        reported_run_id = str(
            source.get("runId") or source.get("run_id") or ""
        ).strip()
        reported_session_key = str(
            source.get("sessionKey") or source.get("session_key") or ""
        ).strip()
        if source and (not reported_run_id or not reported_session_key):
            return {}
        if reported_run_id and reported_run_id != normalized_run_id:
            return {}
        session_key = str(
            reported_session_key
            or fallback_session_key
            or ""
        ).strip()
        digest = str(
            source.get("bindingDigest")
            or source.get("binding_digest")
            or ""
        ).strip()
        if digest and (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            digest = ""
        return {
            "run_id": normalized_run_id,
            "session_key": session_key,
            "binding_digest": digest,
        }

    @staticmethod
    def _complete_governance_identity(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        run_id = str(value.get("run_id") or "").strip()
        session_key = str(value.get("session_key") or "").strip()
        digest = str(value.get("binding_digest") or "").strip()
        return bool(
            run_id
            and session_key
            and len(digest) == 64
            and all(
                character in "0123456789abcdef"
                for character in digest
            )
        )

    def _project_terminal_execution(
        self,
        execution: ExecutionResult,
        *,
        projection_source: str,
    ) -> ExecutionResult:
        """Replace Agent-reported effects with Veyra's run ledger projection."""

        projection = self._authoritative_run_projection(execution.task_id)
        raw = (
            copy.deepcopy(execution.raw)
            if isinstance(execution.raw, dict)
            else {}
        )
        agent_response = (
            raw.get("agent_response")
            if isinstance(raw.get("agent_response"), dict)
            else {}
        )
        reported_tool_calls = self._string_items(
            agent_response.get("tool_calls")
        ) or self._string_items(execution.tool_calls)
        reported_changed_files = self._string_items(
            agent_response.get("changed_files")
        ) or self._string_items(execution.changed_files)
        raw["agent_reported_tool_evidence"] = {
            "status": "diagnostic_only",
            "authoritative": False,
            "tool_call_count": len(reported_tool_calls),
            "changed_file_count": len(reported_changed_files),
        }
        raw["governed_tool_evidence"] = {
            "status": projection["status"],
            "authority_source": "veyra_private_tool_ledger",
            "projection_source": projection_source,
            "observed_call_count": projection["observed_call_count"],
            "effect_count": projection["effect_count"],
            **(
                {"reason": projection["reason"]}
                if projection.get("reason")
                else {}
            ),
        }
        raw["tool_receipt_refs"] = projection["tool_receipt_refs"]
        raw["governance_cleanup"] = self._cancel_run_authority(
            execution.task_id,
            reason=f"terminal_projection:{projection_source}",
            abort_chat=False,
        )
        return ExecutionResult(
            task_id=execution.task_id,
            executor=execution.executor,
            status=execution.status,
            result=execution.result,
            logs=execution.logs,
            changed_files=projection["changed_files"],
            tool_calls=projection["tool_calls"],
            raw=self._redact_payload(raw),
        )

    def _authoritative_run_projection(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()

        def empty(status: str, reason: str) -> dict[str, Any]:
            return {
                "status": status,
                "reason": reason[:600],
                "tool_calls": [],
                "changed_files": [],
                "tool_receipt_refs": [],
                "observed_call_count": 0,
                "effect_count": 0,
            }

        if not normalized_run_id:
            return empty(
                "resolution_failed",
                "OpenClaw terminal result has no normalized run id.",
            )
        if self._governance_run_evidence_resolver is None:
            return empty(
                "not_configured",
                "Veyra authoritative run evidence resolver is not configured.",
            )
        try:
            evidence = self._governance_run_evidence_resolver(
                normalized_run_id
            )
        except Exception as exc:
            return empty("resolution_failed", str(exc))
        if not isinstance(evidence, dict):
            return empty(
                "resolution_failed",
                "Veyra run evidence resolver returned a non-object.",
            )
        evidence_status = str(evidence.get("status") or "").strip().lower()
        if evidence_status in {
            "blocked",
            "degraded",
            "error",
            "failed",
            "not_available",
            "not_configured",
            "resolution_failed",
            "unavailable",
        }:
            return empty(
                "resolution_failed",
                str(
                    evidence.get("reason")
                    or evidence.get("error")
                    or f"Veyra run evidence resolver reported {evidence_status}."
                ),
            )
        if str(evidence.get("run_id") or "").strip() != normalized_run_id:
            return empty(
                "resolution_failed",
                "Veyra run evidence projection belongs to another run.",
            )

        tool_calls = self._string_items(evidence.get("tool_calls"))
        changed_files = self._string_items(evidence.get("changed_files"))
        refs = self._validated_tool_receipt_refs(
            normalized_run_id,
            evidence.get("tool_receipt_refs"),
        )
        if refs is None or len(tool_calls) != len(refs):
            return empty(
                "resolution_failed",
                "Veyra run evidence projection has malformed receipt bindings.",
            )
        raw_observed_count = evidence.get(
            "observed_call_count",
            len(refs),
        )
        raw_effect_count = evidence.get("effect_count", 0)
        if isinstance(raw_observed_count, bool) or isinstance(
            raw_effect_count,
            bool,
        ):
            return empty(
                "resolution_failed",
                "Veyra run evidence projection has invalid counters.",
            )
        try:
            observed_call_count = int(raw_observed_count)
            effect_count = int(raw_effect_count)
        except (TypeError, ValueError):
            return empty(
                "resolution_failed",
                "Veyra run evidence projection has invalid counters.",
            )
        if (
            observed_call_count != len(refs)
            or effect_count < 0
            or effect_count > observed_call_count
        ):
            return empty(
                "resolution_failed",
                "Veyra run evidence projection counters do not match receipts.",
            )
        return {
            "status": "resolved",
            "reason": "",
            "tool_calls": tool_calls,
            "changed_files": changed_files,
            "tool_receipt_refs": refs,
            "observed_call_count": observed_call_count,
            "effect_count": effect_count,
        }

    @staticmethod
    def _validated_tool_receipt_refs(
        run_id: str,
        value: Any,
    ) -> list[dict[str, Any]] | None:
        if not isinstance(value, (list, tuple)):
            return None
        refs: list[dict[str, Any]] = []
        for expected_index, item in enumerate(value):
            if not isinstance(item, dict):
                return None
            call_id = str(item.get("tool_call_id") or "").strip()
            digest = str(item.get("invocation_digest") or "").strip()
            reported_index = item.get("reported_call_index")
            if (
                str(item.get("run_id") or "").strip() != run_id
                or not call_id
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
                or isinstance(reported_index, bool)
                or not isinstance(reported_index, int)
                or reported_index != expected_index
            ):
                return None
            refs.append(
                {
                    "run_id": run_id,
                    "tool_call_id": call_id,
                    "invocation_digest": digest,
                    "reported_call_index": reported_index,
                }
            )
        return refs

    def _cached_execution(self, task_id: str) -> ExecutionResult | None:
        with self._run_cache_lock:
            cached = self._run_cache.get(task_id)
        if not isinstance(cached, dict):
            return None
        status = str(cached.get("status") or "")
        if status not in {"success", "error"}:
            return None
        final_event = cached.get("final_event") if isinstance(cached.get("final_event"), dict) else {}
        public_cached = copy.deepcopy(cached)
        public_cached.pop("governance_identity", None)
        execution_raw = self._execution_raw_for_run(
            task_id,
            {"cached_run": public_cached, "poll_method": "run_cache"},
            result_text=str(cached.get("result") or ""),
        )
        return self._project_terminal_execution(
            ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status=status,
                result=str(
                    cached.get("result")
                    or self._message_text(final_event.get("message"))
                    or ""
                ),
                raw=execution_raw,
            ),
            projection_source="run_cache",
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
        if not _ensure_crypto_available():
            return None
        # This file contains local signing material and must remain gitignored.
        # Only short summaries of device-auth state may leave the adapter boundary.
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
        if not device_identity or not _ensure_crypto_available() or Ed25519PrivateKey is None or not nonce:
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

    def _auth_diagnostics(self) -> dict[str, Any]:
        crypto_available = _ensure_crypto_available()
        identity = self._device_identity() if crypto_available else None
        return {
            "crypto_available": bool(crypto_available),
            "device_store": str(self.device_store),
            "device_store_exists": self.device_store.exists(),
            "device_identity_loaded": bool(identity and identity.get("deviceId") and identity.get("privateKey")),
            "device_token_available": bool(identity and identity.get("token")),
            "gateway_token_configured": bool(self.api_key),
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
        try:
            self._prepare_device_store_parent()
        except OSError:
            return {}
        if not self.device_store.exists():
            return {}
        if self.device_store.is_symlink():
            return {}
        try:
            os.chmod(self.device_store, 0o600)
            data = json.loads(self.device_store.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) and data.get("version") == 1 else {}

    def _write_device_store(self, store: dict[str, Any]) -> None:
        self._prepare_device_store_parent()
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        temporary = self.device_store.parent / f".{self.device_store.name}.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.device_store)
            os.chmod(self.device_store, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _prepare_device_store_parent(self) -> None:
        parent = self.device_store.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Veyra's standard credential location is a dedicated local-state
        # directory. Do not chmod arbitrary custom parent directories.
        if self.device_store.name == "openclaw_device.json" and parent.name == "local":
            os.chmod(parent, 0o700)

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
        items: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id") or group.get("name") or "unknown")[:120]
            group_ids.append(group_id)
            entries = group.get("tools") if isinstance(group.get("tools"), list) else []
            tool_count += len(entries)
            for entry in entries:
                if len(items) >= 120:
                    break
                if isinstance(entry, str):
                    tool_id = entry
                    enabled = True
                elif isinstance(entry, dict):
                    tool_id = str(entry.get("id") or entry.get("name") or entry.get("tool") or "")
                    enabled = entry.get("disabled") is not True and entry.get("enabled") is not False
                else:
                    continue
                if tool_id:
                    items.append({"id": tool_id[:160], "group": group_id, "enabled": enabled})
        return {
            "group_count": len(group_ids),
            "tool_count": tool_count,
            "groups": group_ids[:60],
            "items": items,
            "truncated": tool_count > len(items),
        }

    def _skills_summary(self, skills: dict[str, Any]) -> dict[str, Any]:
        items = skills.get("skills") if isinstance(skills.get("skills"), list) else []
        enabled = [item for item in items if isinstance(item, dict) and item.get("disabled") is not True]
        eligible = [item for item in items if isinstance(item, dict) and item.get("eligible") is True]
        catalog: list[dict[str, Any]] = []
        for item in items[:120]:
            if isinstance(item, str):
                skill_id = item
                disabled = False
                is_eligible = None
            elif isinstance(item, dict):
                skill_id = str(item.get("id") or item.get("name") or item.get("skill") or "")
                disabled = item.get("disabled") is True
                is_eligible = item.get("eligible")
            else:
                continue
            if skill_id:
                catalog.append(
                    {
                        "id": skill_id[:160],
                        "enabled": not disabled,
                        "eligible": is_eligible if isinstance(is_eligible, bool) else None,
                    }
                )
        return {
            "skill_count": len(items),
            "enabled_count": len(enabled),
            "eligible_count": len(eligible),
            "items": catalog,
            "truncated": len(items) > len(catalog),
        }

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
            "capabilitytoken",
            "dispatchtoken",
            "executiontoken",
            "reservationtoken",
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
        session_key = self._canonical_session_key(
            str(
                getattr(task_packet, "agent_execution_session_id", "")
                or ""
            ).strip()
            or self.session_key
        )
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor="openclaw",
            status="adapter_unconfigured",
            result="OpenClaw adapter is not connected. Configure OPENCLAW_GATEWAY_URL or OPENCLAW_BASE_URL before submitting real tasks.",
            logs=prompt,
            raw={
                "contract_version": AGENT_CONTRACT_VERSION,
                "task_context": self._task_context_for_packet(task_packet, session_key),
                "configured": False,
            },
        )

    def _task_context_for_packet(self, task_packet: VeyraTaskPacket, session_key: str) -> dict[str, Any]:
        return {
            "task_packet_id": task_packet.task_id,
            "correlation_id": task_packet.task_id,
            "session_id": task_packet.session_id,
            "agent_execution_session_id": session_key,
            "agent_session_policy": task_packet.agent_session_policy,
            "memory_policy": task_packet.memory_policy,
            "verification_policy": task_packet.verification_policy,
            "rollback_requirement": task_packet.rollback_requirement,
            "user_goal": self._safe_task_summary(task_packet.user_goal or task_packet.user_message),
            "registered_at": self._utc_now(),
        }

    @staticmethod
    def _structured_agent_response(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        candidates = [raw]
        fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidates.insert(0, fenced.group(1))
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            candidates.append(raw[first : last + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _string_items(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = json.dumps(item, ensure_ascii=False, sort_keys=True)
            else:
                text = str(item).strip()
            if text:
                items.append(text)
        return items

    @staticmethod
    def _safe_task_summary(value: Any) -> str:
        text = str(value or "").replace("\n", " ").strip()
        text = re.sub(r"/Users/[^\s,;:)]+", "<local_path>", text)
        text = re.sub(
            r"(?i)(api[_-]?key|token|secret|password)=([A-Za-z0-9._~+/=-]+)",
            r"\1=<redacted>",
            text,
        )
        return text[:500]

    @staticmethod
    def _openclaw_agent_id(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
            raise ValueError("OpenClaw agent_id is invalid")
        return normalized

    def _canonical_session_key(self, value: str) -> str:
        session_key = str(value or "").strip()
        if (
            not session_key
            or "\x00" in session_key
            or len(session_key.encode("utf-8")) > 512
        ):
            raise ValueError("OpenClaw session key is invalid")
        if session_key in {"global", "unknown"}:
            return session_key
        if session_key.startswith("agent:"):
            parts = session_key.split(":", 2)
            if (
                len(parts) != 3
                or parts[1] != self.agent_id
                or not parts[2]
            ):
                raise ValueError(
                    "OpenClaw session key does not match configured agent_id"
                )
            return session_key
        return f"agent:{self.agent_id}:{session_key}"

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
