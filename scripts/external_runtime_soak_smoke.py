from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TEST_RUNTIME = TemporaryDirectory(prefix="veyra-external-runtime-")
TEST_ROOT = Path(_TEST_RUNTIME.name)
TEST_STATE_ROOT = TEST_ROOT / "state"
TEST_AGENCY_ROOT = TEST_ROOT / "agency"
os.environ.setdefault("VEYRA_STATE_ROOT", str(TEST_STATE_ROOT))
os.environ.setdefault("VEYRA_AGENCY_ROOT", str(TEST_AGENCY_ROOT))


class _HermesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/capabilities"):
            self._send_json(
                {
                    "runtime": "hermes",
                    "status": "available",
                    "tools": [],
                    "skills": [],
                    "compatibility": {"status": "compatible"},
                }
            )
            return
        if self.path.startswith("/memory/summary"):
            self._send_json(
                {
                    "provider": "hermes",
                    "status": "success",
                    "summary": "mock external memory",
                    "freshness": "fresh",
                    "trust": "external",
                }
            )
            return
        if self.path.startswith("/tasks/"):
            task_id = self.path.split("/")[-1]
            self._send_json(
                {
                    "task_id": task_id,
                    "executor": "hermes",
                    "status": "success",
                    "result": f"task {task_id} completed",
                    "raw": {"source": "mock-hermes"},
                }
            )
            return
        self._send_json({"status": "not_found", "path": self.path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/memory/patch"):
            self._send_json({"status": "submitted"})
            return
        self._send_json({"status": "success"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _start_mock_runtime() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HermesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def post_json(client: TestClient, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(path, json=payload or {})
    expect(response.status_code < 400, f"POST {path}", response.text)
    return response.json()


def get_json(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    expect(response.status_code < 400, f"GET {path}", response.text)
    return response.json()


def main() -> int:
    print("Veyra external runtime soak smoke")
    server, base_url = _start_mock_runtime()
    try:
        from main import app  # noqa: E402

        client = TestClient(app)
        post_json(client, "/agents/openclaw/config", {"enabled": False})
        post_json(client, "/agents/custom/config", {"enabled": False})
        post_json(client, "/agents/hermes/config", {"kind": "hermes", "enabled": True, "base_url": base_url})
        post_json(client, "/agents/select", {"name": "hermes"})

        probe = post_json(client, "/ops/external-runtime/probe")
        expect(probe.get("status") == "validated", "external runtime probe validated", probe)
        expect(probe.get("runtime_matrix", {}).get("status") == "ready", "runtime matrix ready", probe)
        expect(probe.get("feishu", {}).get("validation", {}).get("status") == "not_configured", "feishu not configured is explicit", probe)

        soak = post_json(client, "/ops/soak", {"iterations": 2})
        expect(soak.get("status") == "success", "soak run succeeds", soak)
        expect(soak.get("validation", {}).get("external_runtime", {}).get("status") == "validated", "soak external runtime validated", soak)
        expect(int(soak.get("validation", {}).get("external_runtime", {}).get("samples", 0)) == 2, "soak sample count", soak)

        status = get_json(client, "/ops/soak/status")
        expect(status.get("status") in {"idle", "completed", "running", "stale", "stopped"}, "soak status endpoint", status)

        print(
            json.dumps(
                {
                    "probe_status": probe.get("status"),
                    "runtime_matrix": probe.get("runtime_matrix", {}).get("summary"),
                    "soak_validation": soak.get("validation"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
