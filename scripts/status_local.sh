#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${VEYRA_HOST:-127.0.0.1}"
PORT="${VEYRA_PORT:-8000}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  HOST="${VEYRA_HOST:-$HOST}"
  PORT="${VEYRA_PORT:-$PORT}"
fi

python - "$HOST" "$PORT" <<'PY'
import json
import sys
from urllib.error import URLError
from urllib.request import urlopen

host, port = sys.argv[1], sys.argv[2]
base = f"http://{host}:{port}"
paths = [
    "/health",
    "/ops/health",
    "/agent/status",
    "/integrations/feishu/ws/status",
    "/runtime/metrics/summary",
    "/ops/reviews/diagnostic",
    "/belief/status",
]

def fetch(path: str) -> tuple[int | None, dict | None, str]:
    try:
        with urlopen(base + path, timeout=5) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw), ""
            except json.JSONDecodeError:
                return response.status, None, raw[:500]
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"

reachable = False
for path in paths:
    status, payload, error = fetch(path)
    print(f"\n== {path}")
    if status is None:
        print(f"ERROR {error}")
        continue
    reachable = True
    print(f"HTTP {status}")
    if not isinstance(payload, dict):
        print(error)
        continue
    if path in {"/health", "/ops/health"}:
        print(f"status: {payload.get('status')}")
        print(f"alerts: {payload.get('alert_count', len(payload.get('alerts') or []))}")
        components = payload.get("components")
        if isinstance(components, dict):
            print("components:")
            for key, value in sorted(components.items()):
                print(f"  {key}: {value}")
    elif path == "/agent/status":
        validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
        print(f"name: {payload.get('name')}")
        print(f"status: {payload.get('status')}")
        print(f"connected: {payload.get('connected')}")
        print(f"validation: {validation.get('status')}")
    elif path == "/integrations/feishu/ws/status":
        print(f"configured: {payload.get('configured')}")
        print(f"thread_alive: {payload.get('thread_alive')}")
        print(f"last_event_at: {payload.get('last_event_at')}")
        print(f"last_error: {payload.get('last_error')}")
    elif path == "/runtime/metrics/summary":
        print(f"window_size: {payload.get('window_size')}")
        print(f"routes: {payload.get('route_distribution')}")
        print(f"runtime_failures: {payload.get('runtime_failure_count')}")
        print(f"avg_latency_ms: {payload.get('avg_latency_ms')}")
    elif path == "/ops/reviews/diagnostic":
        print(f"pending_count: {payload.get('pending_count')}")
        print(f"stale_pending_count: {payload.get('stale_pending_count')}")
    elif path == "/belief/status":
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
        print(f"fresh: {summary.get('fresh')}")
        print(f"stale: {summary.get('stale')}")
        print(f"expired: {summary.get('expired')}")

if not reachable:
    print(f"\nVeyra API is not reachable at {base}")
    raise SystemExit(1)
PY
