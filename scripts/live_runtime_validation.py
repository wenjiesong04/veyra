from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4


READ_ONLY_ENDPOINTS = (
    "/agent/status",
    "/integrations/feishu/ws/status",
    "/runtime/soak/status",
    "/ops/health",
)

EVENT_CASES = (
    ("direct_answer", "你是谁？"),
    ("time_probe", "现在东京几点？"),
    ("weather_probe", "现在贵阳市花溪区天气怎么样？"),
    ("search_refresh", "搜索一下 PyTorch 3.0 最新进展"),
    ("agent_handoff", "请通过 OpenClaw 回答 PONG_VEYRA_PIPELINE_OK"),
)


def request_json(method: str, base_url: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 20.0) -> tuple[dict[str, Any] | None, str | None, int]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}, None, response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return None, f"http_{exc.code}:{raw[:300]}", exc.code
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc), 0


def read_only_checks(base_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in READ_ONLY_ENDPOINTS:
        started = time.perf_counter()
        payload, error, status_code = request_json("GET", base_url, path, timeout=15.0)
        results.append(
            {
                "path": path,
                "status_code": status_code,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": error,
                "status": payload.get("status") if isinstance(payload, dict) else None,
                "payload": payload,
            }
        )
    return results


def exercise_events(base_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    run_id = uuid4().hex[:8]
    for case_id, text in EVENT_CASES:
        started = time.perf_counter()
        payload, error, status_code = request_json(
            "POST",
            base_url,
            "/events/message",
            {
                "text": text,
                "channel": "live-validation",
                "user_id": f"codex-live-{run_id}",
                "session_id": f"live-validation-{case_id}-{run_id}",
                "message_id": f"live-validation-{case_id}-{run_id}",
                "metadata": {"source": "codex_live_validation", "case_id": case_id},
            },
            timeout=180.0,
        )
        results.append(
            {
                "case_id": case_id,
                "status_code": status_code,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": error,
                "route": payload.get("route") if isinstance(payload, dict) else None,
                "status": payload.get("status") if isinstance(payload, dict) else None,
                "response_preview": str(payload.get("response") or "")[:240] if isinstance(payload, dict) else "",
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate live Veyra runtime surfaces.")
    parser.add_argument("--base-url", default=os.getenv("VEYRA_LIVE_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--exercise-events", action="store_true", help="Send diagnostic /events/message traffic.")
    args = parser.parse_args()

    output: dict[str, Any] = {
        "base_url": args.base_url,
        "read_only": read_only_checks(args.base_url),
        "feishu_outbound_send_enabled": os.getenv("VEYRA_LIVE_VALIDATION_FEISHU_SEND") == "1",
    }
    if args.exercise_events:
        output["event_exercises"] = exercise_events(args.base_url)
    else:
        output["event_exercises"] = "skipped; pass --exercise-events to send local diagnostic events"

    print(json.dumps(output, ensure_ascii=False, indent=2))
    errors = [item for item in output["read_only"] if isinstance(item, dict) and item.get("error")]
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
