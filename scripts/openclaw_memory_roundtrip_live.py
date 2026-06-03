#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402


BASE_URL = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def request_json(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise AssertionError(f"{method} {path} failed against {BASE_URL}: {exc}") from exc


def get_json(path: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    suffix = f"?{urlencode(params)}" if params else ""
    return request_json("GET", f"{path}{suffix}", None, timeout=timeout)


def external_write_path(write_result: dict[str, Any]) -> Path | None:
    external = write_result.get("external_write") if isinstance(write_result.get("external_write"), dict) else {}
    path = str(external.get("path") or "")
    if path:
        return Path(path)
    return None


def main() -> int:
    marker = f"veyra-memory-roundtrip-{uuid4().hex[:10]}"
    session_id = f"memory-roundtrip-{uuid4().hex[:8]}"
    summary = f"{marker}: User is validating OpenClaw workspace memory fallback roundtrip."
    patch = {
        "session_id": session_id,
        "memory_type": "roundtrip_probe",
        "topic": "openclaw_workspace_memory_roundtrip",
        "summary": summary,
        "freshness": "fresh",
        "trust": "veyra_live_probe",
        "confidence": 0.99,
    }

    agent_status = get_json("/agent/status", timeout=15.0)
    write = request_json("POST", "/memory/patch", {"provider": "selected", "patch": patch}, timeout=45.0)
    expect(write.get("status") == "written", "Veyra memory patch is accepted", write)

    fallback_path = external_write_path(write)
    external = write.get("external_write") if isinstance(write.get("external_write"), dict) else {}
    fallback_status = str(external.get("status") or "")
    expect(fallback_status in {"workspace_file_fallback", "submitted", "local_only"}, "external memory write reports a concrete status", external)

    fallback_verified = False
    fallback_text = ""
    if fallback_path and fallback_path.exists():
        fallback_text = fallback_path.read_text(encoding="utf-8")
        fallback_verified = marker in fallback_text
    expect(fallback_status != "workspace_file_fallback" or fallback_verified, "workspace fallback file contains marker when fallback is used", {"path": str(fallback_path), "status": fallback_status})

    veyra_summary = get_json("/memory/summary", {"session_id": session_id, "provider": "selected"}, timeout=45.0)
    veyra_summary_text = str(veyra_summary.get("external_summary", {}).get("summary") if isinstance(veyra_summary.get("external_summary"), dict) else "")
    fallback_read_verified = marker in veyra_summary_text or marker in str(veyra_summary.get("summary") or "")
    expect(fallback_read_verified or fallback_verified, "Veyra can read the roundtrip marker from local/fallback memory", veyra_summary)

    native_gateway = {"status": "not_verified", "limitation": ""}
    features = agent_status.get("capabilities", {}).get("features") if isinstance(agent_status.get("capabilities"), dict) else {}
    if isinstance(features, dict) and features.get("memory_summary") and features.get("memory_patch"):
        adapter = OpenClawAdapter(timeout=12.0)
        gateway_summary = adapter.fetch_memory_summary(session_id)
        native_gateway = {
            "status": "verified" if marker in str(gateway_summary.get("summary") or "") else "not_found",
            "summary_status": gateway_summary.get("status"),
            "marker_found": marker in str(gateway_summary.get("summary") or ""),
        }
    else:
        native_gateway = {
            "status": "not_verified",
            "limitation": "OpenClaw gateway currently does not advertise memory.summary/memory.patch; verified workspace fallback file roundtrip instead of native new-context retrieval.",
            "features": features,
        }

    result = {
        "status": "success" if fallback_verified or fallback_read_verified else "degraded",
        "marker": marker,
        "session_id": session_id,
        "write_status": write.get("status"),
        "external_write": external,
        "fallback_file": str(fallback_path) if fallback_path else "",
        "fallback_file_verified": fallback_verified,
        "veyra_summary_verified": fallback_read_verified,
        "native_gateway": native_gateway,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "success":
        raise AssertionError(f"memory roundtrip did not verify fallback/local read: {result}")
    print("openclaw memory roundtrip live passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
