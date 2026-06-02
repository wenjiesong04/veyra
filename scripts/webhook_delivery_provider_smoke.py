#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.commitment_core import CommitmentCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.commitment_push import CommitmentPushRuntime  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


class CaptureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(body.decode("utf-8") or "{}")
        self.requests.append({"path": self.path, "payload": payload})
        response = json.dumps({"status": "accepted", "message_id": "webhook-smoke-1"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args: Any) -> None:
        return


def main() -> int:
    CaptureHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix="veyra-webhook-provider-") as tmp:
            store = WorldStateStore(Path(tmp) / "state")
            state = store.read_json("channel_state.json")
            channels = state.setdefault("channels", {})
            channels["webhook"] = {
                "enabled": True,
                "delivery": "webhook",
                "webhook_url": f"http://127.0.0.1:{server.server_address[1]}/notify",
                "timeout": 5,
            }
            store.write_json("channel_state.json", state)

            commitments = CommitmentCore(store)
            commitment = commitments.create_commitment(
                {
                    "kind": "generic_reminder",
                    "status": "active",
                    "title": "webhook delivery",
                    "channel": "webhook",
                    "session_id": "webhook-session",
                    "next_run_at": past_iso(),
                    "payload": {"note": "provider smoke"},
                }
            )
            before = commitment["next_run_at"]
            runtime = CommitmentPushRuntime(state_store=store, commitment_core=commitments)
            result = runtime.run_due(limit=5, reason="webhook_provider_smoke")
            row = next(item for item in result["processed"] if item["commitment_id"] == commitment["commitment_id"])
            after = commitments.get_commitment(commitment["commitment_id"]) or {}
            outbox = store.read_json("channel_state.json").get("outbox", [])

            expect(row.get("status") == "delivered", "webhook provider delivery is delivered", row)
            expect(row.get("delivery_status") == "provider_sent", "webhook provider sent status is explicit", row)
            expect(after.get("last_run_at"), "webhook delivery records last_run_at", after)
            expect(after.get("next_run_at") and after.get("next_run_at") != before, "webhook delivery advances schedule", after)
            expect(CaptureHandler.requests and CaptureHandler.requests[0]["path"] == "/notify", "webhook received POST", CaptureHandler.requests)
            expect(CaptureHandler.requests[0]["payload"]["session_id"] == "webhook-session", "webhook payload includes session", CaptureHandler.requests[0])
            expect(outbox and outbox[-1].get("provider") == "webhook", "webhook delivery recorded in outbox", outbox[-1] if outbox else None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    print("webhook delivery provider smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
