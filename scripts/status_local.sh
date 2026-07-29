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

# Use the same managed interpreter identity as install/start instead of an
# ambient PATH alias. The status client itself remains stdlib-only so it can
# diagnose a running service even if optional runtime packages have drifted.
# shellcheck disable=SC1091
. "$ROOT/scripts/veyra_python_runtime.sh"
if veyra_resolve_python "$ROOT" 2>/dev/null; then
  PYTHON_BIN="$VEYRA_RESOLVED_PYTHON"
  PYTHON_SOURCE="$VEYRA_RESOLVED_PYTHON_SOURCE"
else
  PYTHON_BIN=""
  PYTHON_SOURCE="launchagent"
  if [ "$(uname -s)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/ai.veyra.api.plist"
    if [ -f "$PLIST" ]; then
      PYTHON_BIN="$(
        sed -n \
          -e 's#.*<string>--service-python=\([^<]*\)</string>.*#\1#p' \
          -e 's#.*exec "\([^"]*/python[^"]*\)" -B -m uvicorn.*#\1#p' \
          "$PLIST" \
          | head -n 1
      )"
    fi
  fi
  if [[ "$PYTHON_BIN" != /* ]] || [ ! -x "$PYTHON_BIN" ]; then
    echo "No managed Python is active and the LaunchAgent plist has no usable Veyra interpreter." >&2
    echo "Activate it with 'conda activate veyra' or set VEYRA_PYTHON=/absolute/path/to/python3.11." >&2
    exit 2
  fi
fi
veyra_validate_python_environment "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE" >/dev/null

"$PYTHON_BIN" - "$HOST" "$PORT" <<'PY'
import json
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

host, port = sys.argv[1], sys.argv[2]
if host in {"0.0.0.0", "::", "[::]"}:
    host = "127.0.0.1"
url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
base = f"http://{url_host}:{port}"
headers = {"Accept": "application/json"}
token = str(os.environ.get("VEYRA_LOCAL_API_TOKEN") or "").strip()
if token:
    headers["X-Veyra-Token"] = token
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
        with urlopen(Request(base + path, headers=headers), timeout=5) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw), ""
            except json.JSONDecodeError:
                return response.status, None, raw[:500]
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"

reachable = False
failed_paths: list[str] = []
for path in paths:
    status, payload, error = fetch(path)
    print(f"\n== {path}")
    if status is None:
        print(f"ERROR {error}")
        failed_paths.append(path)
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
        print(f"status: {payload.get('status')}")
        print(f"configured: {payload.get('configured')}")
        print(f"connected: {payload.get('connected')}")
        print(f"readiness: {payload.get('readiness')}")
        print(f"thread_alive: {payload.get('thread_alive')}")
        print(f"last_connected_at: {payload.get('last_connected_at')}")
        print(f"connected_host: {payload.get('connected_host')}")
        print(f"last_event_at: {payload.get('last_event_at')}")
        print(f"last_event_after_start: {payload.get('last_event_after_start')}")
        print(f"last_processed_after_start: {payload.get('last_processed_after_start')}")
        print(f"last_reply_sent_after_start: {payload.get('last_reply_sent_after_start')}")
        print(f"processing_failure_unrecovered: {payload.get('processing_failure_unrecovered')}")
        print(f"last_error: {payload.get('last_error')}")
        print(f"tls: {payload.get('tls')}")
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
if failed_paths:
    print(f"\nVeyra API status was incomplete; failed paths: {', '.join(failed_paths)}")
    raise SystemExit(1)
PY
