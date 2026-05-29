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

_TEST_RUNTIME = TemporaryDirectory(prefix="veyra-tool-proxy-live-")
TEST_ROOT = Path(_TEST_RUNTIME.name)
TEST_STATE_ROOT = TEST_ROOT / "state"
TEST_AGENCY_ROOT = TEST_ROOT / "agency"
os.environ.setdefault("VEYRA_STATE_ROOT", str(TEST_STATE_ROOT))
os.environ.setdefault("VEYRA_AGENCY_ROOT", str(TEST_AGENCY_ROOT))


class _LocalApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ping"):
            self._send_json({"status": "ok", "path": self.path})
            return
        self._send_json({"status": "not_found", "path": self.path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/echo"):
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            self._send_json({"status": "ok", "payload": payload})
            return
        self._send_json({"status": "not_found", "path": self.path}, status=404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _start_local_api() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalApiHandler)
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


def main() -> int:
    print("Veyra Tool Proxy live execution smoke")
    server, base_url = _start_local_api()
    try:
        from main import app  # noqa: E402

        client = TestClient(app)
        initial = post_json(client, "/tool-proxy/config", {"api_executor_enabled": False, "browser_executor_enabled": False})
        expect(initial.get("api", {}).get("configured") is False, "api starts disabled", initial)
        expect(initial.get("browser", {}).get("configured") is False, "browser starts disabled", initial)

        configured = post_json(
            client,
            "/tool-proxy/config",
            {
                "api_executor_enabled": True,
                "browser_executor_enabled": True,
                "api_allowed_hosts": ["127.0.0.1", "localhost"],
                "browser_allowed_hosts": ["127.0.0.1", "localhost"],
            },
        )
        expect(configured.get("api", {}).get("configured") is True, "api configured", configured)
        expect(configured.get("browser", {}).get("configured") is True, "browser configured", configured)

        api_get = post_json(
            client,
            "/tool-proxy/api/request",
            {"payload": {"method": "GET", "url": f"{base_url}/ping"}},
        )
        expect(api_get.get("status") == "ok", "api GET executes", api_get)
        expect(api_get.get("execution_attempted") is True, "api execution attempted", api_get)
        expect(api_get.get("validation", {}).get("status") in {"validated", "validation_pending"}, "api validation status", api_get)

        api_blocked = post_json(
            client,
            "/tool-proxy/api/request",
            {"payload": {"method": "GET", "url": "https://example.com/ping"}},
        )
        expect(api_blocked.get("status") == "blocked", "api allowlist blocks external host", api_blocked)

        api_post_needs_review = post_json(
            client,
            "/tool-proxy/api/request",
            {"payload": {"method": "POST", "url": f"{base_url}/echo", "json": {"hello": "veyra"}}},
        )
        expect(api_post_needs_review.get("status") == "needs_confirmation", "state-changing API needs approval", api_post_needs_review)

        api_post_approved = post_json(
            client,
            "/tool-proxy/api/request",
            {
                "payload": {"method": "POST", "url": f"{base_url}/echo", "json": {"hello": "veyra"}},
                "approved_by": "tool_proxy_live_smoke",
            },
        )
        expect(api_post_approved.get("status") == "ok", "approved API POST executes", api_post_approved)

        browser_local = post_json(
            client,
            "/tool-proxy/browser/open",
            {"url": f"{base_url}/ping"},
        )
        expect(browser_local.get("status") in {"ok", "error"}, "browser execution attempted", browser_local)
        expect(browser_local.get("validation", {}).get("executor_configured") is True, "browser executor configured", browser_local)
        expect(browser_local.get("execution_attempted") is True, "browser execution attempted flag", browser_local)

        browser_blocked = post_json(
            client,
            "/tool-proxy/browser/open",
            {"url": "https://example.com"},
        )
        expect(browser_blocked.get("status") == "blocked", "browser allowlist blocks external host", browser_blocked)

        disabled = post_json(client, "/tool-proxy/config", {"api_executor_enabled": False, "browser_executor_enabled": False})
        expect(disabled.get("api", {}).get("configured") is False, "api disabled again", disabled)
        expect(disabled.get("browser", {}).get("configured") is False, "browser disabled again", disabled)

        print(
            json.dumps(
                {
                    "api_get": {"status": api_get.get("status"), "validation": api_get.get("validation")},
                    "api_post_approved": {"status": api_post_approved.get("status"), "validation": api_post_approved.get("validation")},
                    "browser_local": {
                        "status": browser_local.get("status"),
                        "validation": browser_local.get("validation"),
                    },
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
