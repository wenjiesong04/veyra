#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="foreground"
HOST="${VEYRA_HOST:-127.0.0.1}"
PORT="${VEYRA_PORT:-8000}"

for arg in "$@"; do
  case "$arg" in
    --foreground) MODE="foreground" ;;
    --launchd) MODE="launchd" ;;
    --host=*) HOST="${arg#*=}" ;;
    --port=*) PORT="${arg#*=}" ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/start_local.sh [--foreground|--launchd] [--host=127.0.0.1] [--port=8000]

--foreground  Run uvicorn in the current terminal.
--launchd     macOS only: install and start user LaunchAgent ai.veyra.api.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  HOST="${VEYRA_HOST:-$HOST}"
  PORT="${VEYRA_PORT:-$PORT}"
fi

# Keep the policy declaration and the uvicorn listener on the exact same
# resolved values. In particular, command-line --host/--port selections must
# be visible to main.py rather than existing only as shell-local variables.
export VEYRA_HOST="$HOST"
export VEYRA_PORT="$PORT"

if [ -x .venv/bin/python ]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON:-$(command -v python3)}"
  if [ -z "$PYTHON_BIN" ]; then
    echo "python3 was not found on PATH. Set PYTHON=/absolute/path/to/python." >&2
    exit 2
  fi
  if [[ "$PYTHON_BIN" != /* ]]; then
    RESOLVED_PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
    if [ -z "$RESOLVED_PYTHON_BIN" ]; then
      echo "Python executable not found: $PYTHON_BIN" >&2
      exit 2
    fi
    PYTHON_BIN="$RESOLVED_PYTHON_BIN"
  fi
fi

if [ "$MODE" = "foreground" ]; then
  echo "==> Ensuring local state exists"
  "$PYTHON_BIN" - <<'PY'
from core.world_state import WorldStateStore

WorldStateStore()
PY
else
  # A running launchd instance owns the exclusive state-writer lease until
  # bootout below. The replacement API initializes state after acquiring that
  # lease, so a competing preflight writer would make every restart fail.
  echo "==> State initialization delegated to the launchd API process"
fi

if [ "$MODE" = "launchd" ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "--launchd is only supported on macOS. Use --foreground on this system." >&2
    exit 2
  fi
  PLIST="$HOME/Library/LaunchAgents/ai.veyra.api.plist"
  mkdir -p "$(dirname "$PLIST")" state/logs
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.veyra.api</string>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "$ROOT" || exit 1; set -a; [ -f .env ] &amp;&amp; source .env; set +a; export VEYRA_HOST="$HOST" VEYRA_PORT="$PORT"; exec "$PYTHON_BIN" -B -m uvicorn main:app --host "$HOST" --port "$PORT"</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$ROOT/state/logs/veyra_launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT/state/logs/veyra_launchd.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/ai.veyra.api"
  echo "==> Started launchd service ai.veyra.api"
  echo "    Check status with: ./scripts/status_local.sh"
  exit 0
fi

echo "==> Starting Veyra API at http://$HOST:$PORT"
exec "$PYTHON_BIN" -B -m uvicorn main:app --host "$HOST" --port "$PORT"
