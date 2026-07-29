#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.env_loader import load_runtime_env  # noqa: E402


load_runtime_env()

BASE_URL = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def get_json(path: str, *, timeout: float = 10.0) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    token = str(os.getenv("VEYRA_LOCAL_API_TOKEN") or "").strip()
    if token:
        headers["X-Veyra-Token"] = token
    request = Request(f"{BASE_URL}{path}", headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def safe_get(path: str, *, timeout: float = 10.0) -> dict[str, Any]:
    try:
        return get_json(path, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "status": "error",
            "request_error": True,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def main() -> int:
    health = safe_get("/health", timeout=8)
    model = safe_get("/core/model/status", timeout=8)
    agent = safe_get("/agent/status", timeout=20)
    channels = safe_get("/channels", timeout=8)
    feishu = safe_get("/integrations/feishu/ws/status", timeout=8)
    warnings: list[str] = []

    expect(str(health.get("status") or "").lower() in {"ok", "success", "healthy", "degraded"}, "health endpoint is reachable", health)
    expect(model.get("status") in {"configured", "unconfigured"} or "configured" in model, "core model status endpoint is reachable", model)
    expect(bool(agent.get("validation")) or agent.get("status") in {"available", "adapter_unconfigured", "unavailable", "error"}, "agent status endpoint is reachable", agent)
    expect(channels.get("status") == "success", "channel status endpoint is reachable", channels)
    expect(
        feishu.get("request_error") is not True
        and "configured" in feishu
        and "thread_alive" in feishu,
        "Feishu websocket status endpoint is reachable",
        feishu,
    )
    expect(
        not (
            feishu.get("configured")
            and feishu.get("processing_failure_unrecovered")
        ),
        "Feishu websocket has no unrecovered processing failure",
        feishu,
    )

    if model.get("configured") and not model.get("api_key_set"):
        warnings.append("core model is configured but api_key_set=false")
    if str(health.get("status") or "").lower() == "degraded":
        warnings.append("health endpoint is reachable but reports degraded runtime warnings")
    capabilities = agent.get("capabilities") if isinstance(agent.get("capabilities"), dict) else {}
    features = capabilities.get("features") if isinstance(capabilities.get("features"), dict) else {}
    if features and not features.get("memory_summary"):
        warnings.append("OpenClaw gateway does not expose memory.summary; Veyra should use workspace fallback")
    if features and not features.get("memory_patch"):
        warnings.append("OpenClaw gateway does not expose memory.patch; Veyra should use workspace fallback")
    if feishu.get("configured") and feishu.get("connected") and not feishu.get("last_event_after_start"):
        warnings.append("Feishu WS is connected but has not received an inbound event since restart")
    elif feishu.get("configured") and feishu.get("connected") and not feishu.get("last_processed_after_start"):
        warnings.append("Feishu WS received an event but has no successful processing evidence since restart")
    elif feishu.get("configured") and feishu.get("connected") and not feishu.get("last_reply_sent_after_start"):
        warnings.append("Feishu WS processed an event but has no provider-sent reply evidence since restart")
    elif feishu.get("configured") and feishu.get("thread_alive") and not feishu.get("connected"):
        warnings.append("Feishu WS worker is alive but not connected")

    summary = {
        "status": "success" if not warnings else "warning",
        "base_url": BASE_URL,
        "health": {"status": health.get("status")},
        "core_model": {
            "status": model.get("status"),
            "configured": model.get("configured"),
            "api_key_set": model.get("api_key_set"),
            "model": model.get("model"),
        },
        "agent": {
            "status": agent.get("status"),
            "connected": agent.get("connected"),
            "memory_summary": features.get("memory_summary"),
            "memory_patch": features.get("memory_patch"),
        },
        "channels": {
            "status": channels.get("status"),
            "session_count": channels.get("session_count"),
            "inbox_count": channels.get("inbox_count"),
            "outbox_count": channels.get("outbox_count"),
        },
        "feishu_ws": {
            "status": feishu.get("status"),
            "configured": feishu.get("configured"),
            "connected": feishu.get("connected"),
            "thread_alive": feishu.get("thread_alive"),
            "last_event_after_start": feishu.get("last_event_after_start"),
            "last_processed_after_start": feishu.get("last_processed_after_start"),
            "last_reply_sent_after_start": feishu.get("last_reply_sent_after_start"),
            "processing_failure_unrecovered": feishu.get("processing_failure_unrecovered"),
        },
        "warnings": warnings,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("runtime reproducibility validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
