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


BASE_URL = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def request_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def safe_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    try:
        return request_json(path, method=method, payload=payload, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc), "error_type": type(exc).__name__}


def choose_session(sessions: dict[str, Any], outbox: list[Any]) -> tuple[str, str]:
    explicit = os.getenv("FEISHU_DIAGNOSTIC_SESSION_ID") or os.getenv("VEYRA_FEISHU_DIAGNOSTIC_SESSION_ID")
    if explicit:
        return explicit, "env"
    for session_id, item in sessions.items():
        if str(session_id).startswith("feishu:"):
            return str(session_id), "sessions"
        if isinstance(item, dict) and item.get("channel") == "feishu":
            return str(session_id), "sessions"
    for item in reversed(outbox):
        if not isinstance(item, dict):
            continue
        if item.get("channel") == "feishu" and item.get("session_id"):
            return str(item["session_id"]), "outbox"
    return "feishu-diagnostic", "default_or_unresolved"


def compact_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": delivery.get("status"),
        "delivery_status": delivery.get("delivery_status"),
        "provider": delivery.get("provider"),
        "receive_id_type": delivery.get("receive_id_type"),
        "receive_id_source": delivery.get("receive_id_source"),
        "external_message_id": delivery.get("external_message_id"),
        "reason": delivery.get("reason"),
        "error_type": delivery.get("error_type"),
        "http_status": delivery.get("http_status"),
        "provider_response": delivery.get("provider_response") if isinstance(delivery.get("provider_response"), dict) else None,
    }


def main() -> int:
    warnings: list[str] = []
    ws_status = safe_request("/integrations/feishu/ws/status", timeout=8)
    channel_status = safe_request("/channels", timeout=8)
    sessions_body = safe_request("/channels/sessions", timeout=8)
    outbox_body = safe_request("/channels/outbox?limit=20", timeout=8)
    sessions = sessions_body.get("sessions") if isinstance(sessions_body.get("sessions"), dict) else {}
    outbox = outbox_body.get("items") if isinstance(outbox_body.get("items"), list) else []

    ws_alive = bool(ws_status.get("thread_alive"))
    ws_configured = bool(ws_status.get("configured"))
    last_event_after_start = bool(ws_status.get("last_event_after_start"))
    inbound_ready = bool(ws_alive and ws_configured and last_event_after_start)
    if ws_configured and ws_alive and not last_event_after_start:
        warnings.append("Feishu WS is alive, but no inbound event has arrived since this Veyra process started")
    if not ws_configured:
        warnings.append("Feishu WS is not fully configured")
    if not ws_alive:
        warnings.append("Feishu WS worker is not alive")

    session_id, session_source = choose_session(sessions, outbox)
    send_enabled = os.getenv("VEYRA_FEISHU_DIAGNOSTIC_SEND", "1").strip().lower() not in {"0", "false", "no"}
    send_body: dict[str, Any] = {"status": "skipped", "reason": "VEYRA_FEISHU_DIAGNOSTIC_SEND disabled"}
    provider_sent = False
    if send_enabled:
        send_body = safe_request(
            "/channels/feishu/send",
            method="POST",
            payload={
                "session_id": session_id,
                "message": os.getenv("VEYRA_FEISHU_DIAGNOSTIC_MESSAGE", "Veyra Feishu live diagnostic: outbound provider self-test."),
                "metadata": {"route": "feishu_live_diagnostic", "session_source": session_source},
            },
            timeout=30,
        )
        delivery = send_body.get("delivery") if isinstance(send_body.get("delivery"), dict) else {}
        provider_sent = str(delivery.get("delivery_status") or delivery.get("status") or "") in {"provider_sent", "sent", "delivered"}
        if not provider_sent:
            warnings.append("Feishu outbound provider self-test did not reach provider_sent")

    result = {
        "status": "success" if provider_sent and (ws_alive or not ws_configured) else "degraded",
        "base_url": BASE_URL,
        "inbound_ready": inbound_ready,
        "outbound_ready": provider_sent,
        "last_event_after_start": last_event_after_start,
        "provider_sent": provider_sent,
        "session_source": session_source,
        "ws_status": {
            "status": ws_status.get("status"),
            "thread_alive": ws_alive,
            "configured": ws_configured,
            "started_at": ws_status.get("started_at"),
            "last_event_at": ws_status.get("last_event_at"),
            "last_event_after_start": last_event_after_start,
            "diagnostics": ws_status.get("diagnostics") if isinstance(ws_status.get("diagnostics"), list) else [],
        },
        "channel_status": {
            "status": channel_status.get("status"),
            "session_count": channel_status.get("session_count"),
            "inbox_count": channel_status.get("inbox_count"),
            "outbox_count": channel_status.get("outbox_count"),
        },
        "outbound_delivery": compact_delivery(send_body.get("delivery") if isinstance(send_body.get("delivery"), dict) else send_body),
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("feishu live diagnostic passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
