#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="foreground"
HOST="${VEYRA_HOST:-127.0.0.1}"
PORT="${VEYRA_PORT:-8000}"
SERVICE_PYTHON=""

for arg in "$@"; do
  case "$arg" in
    --foreground) MODE="foreground" ;;
    --launchd) MODE="launchd" ;;
    --check-runtime) MODE="check" ;;
    --service) MODE="service" ;;
    --service-python=*) SERVICE_PYTHON="${arg#*=}" ;;
    --host=*) HOST="${arg#*=}" ;;
    --port=*) PORT="${arg#*=}" ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/start_local.sh [--foreground|--launchd] [--host=127.0.0.1] [--port=8000]

--foreground  Run uvicorn in the current terminal.
--launchd     macOS only: install and start user LaunchAgent ai.veyra.api.
--check-runtime  Validate the selected Python, pinned dependencies, and TLS CA without starting Veyra.
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

if [ "$MODE" = "service" ]; then
  if [[ "$SERVICE_PYTHON" != /* ]] || [ ! -x "$SERVICE_PYTHON" ]; then
    echo "The LaunchAgent service requires an absolute executable --service-python path." >&2
    exit 2
  fi
  # The plist-bound interpreter is authoritative even if .env later gains a
  # stale VEYRA_PYTHON/PYTHON value.
  export VEYRA_PYTHON="$SERVICE_PYTHON"
fi

# Keep the policy declaration and the uvicorn listener on the exact same
# resolved values. In particular, command-line --host/--port selections must
# be visible to main.py rather than existing only as shell-local variables.
export VEYRA_HOST="$HOST"
export VEYRA_PORT="$PORT"

# shellcheck disable=SC1091
. "$ROOT/scripts/veyra_python_runtime.sh"
veyra_resolve_python "$ROOT"
PYTHON_BIN="$VEYRA_RESOLVED_PYTHON"
PYTHON_SOURCE="$VEYRA_RESOLVED_PYTHON_SOURCE"
RUNTIME_INFO="$(veyra_runtime_info "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE")"
echo "==> Using validated Veyra runtime: $RUNTIME_INFO"

if [ "$MODE" = "check" ]; then
  exit 0
fi

if [ "$MODE" = "foreground" ] || [ "$MODE" = "service" ]; then
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
  veyra_acquire_update_lock "$ROOT" "LaunchAgent replacement"
  trap 'veyra_release_update_lock' EXIT
  PLIST="$HOME/Library/LaunchAgents/ai.veyra.api.plist"
  PLIST_DIR="$(dirname "$PLIST")"
  SERVICE_DOMAIN="gui/$(id -u)"
  SERVICE_TARGET="$SERVICE_DOMAIN/ai.veyra.api"
  mkdir -p "$PLIST_DIR" state/logs
  PLIST_TMP="$(mktemp "$PLIST_DIR/.ai.veyra.api.plist.XXXXXX")"
  PLIST_BACKUP=""
  HAD_PLIST=0
  JOB_WAS_LOADED=0
  PREVIOUS_PID=""
  REPLACEMENT_IN_PROGRESS=0
  PRESERVE_BACKUP=0

  cleanup_launchagent_temps() {
    local original_status=$?
    trap - EXIT HUP INT TERM
    if [ "$REPLACEMENT_IN_PROGRESS" = "1" ]; then
      echo "LaunchAgent replacement did not complete; restoring the previous configuration." >&2
      if ! rollback_launchagent; then
        echo "LaunchAgent rollback was incomplete; inspect launchctl before retrying." >&2
      fi
      REPLACEMENT_IN_PROGRESS=0
    fi
    [ -z "$PLIST_TMP" ] || rm -f "$PLIST_TMP"
    if [ -n "$PLIST_BACKUP" ]; then
      if [ "$PRESERVE_BACKUP" = "1" ]; then
        echo "Previous LaunchAgent plist preserved for manual recovery at: $PLIST_BACKUP" >&2
      else
        rm -f "$PLIST_BACKUP"
      fi
    fi
    veyra_release_update_lock
    return "$original_status"
  }

  wait_for_launchagent_absent() {
    local launchctl_state=""
    local attempt
    for attempt in $(seq 1 50); do
      if launchctl_state="$(launchctl print "$SERVICE_TARGET" 2>&1)"; then
        sleep 0.1
        continue
      fi
      if printf '%s\n' "$launchctl_state" | grep -Fq "Could not find service"; then
        return 0
      fi
      echo "Could not verify that the previous LaunchAgent was unloaded." >&2
      printf '%s\n' "$launchctl_state" >&2
      return 1
    done
    echo "The previous LaunchAgent did not finish unloading before timeout." >&2
    return 1
  }

  rollback_launchagent() {
    local rollback_failed=0
    launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
    if ! wait_for_launchagent_absent; then
      echo "Could not safely restore the previous LaunchAgent while a job remains loaded." >&2
      PRESERVE_BACKUP=1
      return 1
    fi
    if [ "$HAD_PLIST" = "1" ]; then
      if ! mv "$PLIST_BACKUP" "$PLIST"; then
        echo "Failed to restore the previous LaunchAgent plist: $PLIST" >&2
        rollback_failed=1
        PRESERVE_BACKUP=1
      else
        PLIST_BACKUP=""
      fi
    else
      rm -f "$PLIST"
    fi
    if [ "$JOB_WAS_LOADED" = "1" ]; then
      if [ ! -f "$PLIST" ] || ! launchctl bootstrap "$SERVICE_DOMAIN" "$PLIST"; then
        echo "Failed to restore the previous LaunchAgent job." >&2
        rollback_failed=1
      elif ! launchctl kickstart -k "$SERVICE_TARGET"; then
        echo "The previous LaunchAgent job was restored but could not be kickstarted." >&2
        rollback_failed=1
      fi
    fi
    return "$rollback_failed"
  }

  wait_for_launchagent_ready() {
    local not_before="$1"
    "$PYTHON_BIN" - "$HOST" "$PORT" "${VEYRA_LAUNCHD_READY_TIMEOUT_SECONDS:-15}" "$not_before" <<'PY'
import json
import os
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen

host, port, timeout_raw, not_before_raw = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if host in {"0.0.0.0", "::", "[::]"}:
    host = "127.0.0.1"
url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
token = str(os.environ.get("VEYRA_LOCAL_API_TOKEN") or "").strip()
headers = {"Accept": "application/json"}
if token:
    headers["X-Veyra-Token"] = token
try:
    timeout = max(0.2, min(float(timeout_raw), 60.0))
except ValueError:
    raise SystemExit("VEYRA_LAUNCHD_READY_TIMEOUT_SECONDS must be numeric")
try:
    not_before = datetime.fromisoformat(not_before_raw.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit("LaunchAgent readiness marker is invalid")
deadline = time.monotonic() + timeout
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        with urlopen(Request(f"http://{url_host}:{port}/setup/status", headers=headers), timeout=min(1.0, timeout)) as response:
            setup = json.loads(response.read().decode("utf-8") or "{}")
        with urlopen(Request(f"http://{url_host}:{port}/runtime", headers=headers), timeout=min(1.0, timeout)) as response:
            runtime = json.loads(response.read().decode("utf-8") or "{}")
        platform = setup.get("platform") if isinstance(setup.get("platform"), dict) else {}
        lifecycle = runtime.get("lifecycle") if isinstance(runtime.get("lifecycle"), dict) else {}
        version = str(platform.get("python") or "")
        started_raw = str(lifecycle.get("started_at") or "")
        started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        if setup.get("app_name") == "Veyra" and version.startswith("3.11.") and started_at >= not_before:
            raise SystemExit(0)
        last_error = (
            f"unexpected setup identity/python/start: "
            f"{setup.get('app_name')!r} {version!r} {started_raw!r}"
        )
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
    time.sleep(0.2)
raise SystemExit(f"Veyra API did not become ready before timeout: {last_error}")
PY
  }

  trap cleanup_launchagent_temps EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  cat > "$PLIST_TMP" <<EOF
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
    <string>/bin/bash</string>
    <string>$ROOT/scripts/start_local.sh</string>
    <string>--service</string>
    <string>--service-python=$PYTHON_BIN</string>
    <string>--host=$HOST</string>
    <string>--port=$PORT</string>
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
  plutil -lint "$PLIST_TMP" >/dev/null
  chmod 600 "$PLIST_TMP"

  if [ -f "$PLIST" ]; then
    PLIST_BACKUP="$(mktemp "$PLIST_DIR/.ai.veyra.api.previous.XXXXXX")"
    cp -p "$PLIST" "$PLIST_BACKUP"
    if ! plutil -lint "$PLIST_BACKUP" >/dev/null; then
      echo "The existing LaunchAgent plist is invalid; refusing to stop a job that cannot be restored." >&2
      exit 1
    fi
    PREVIOUS_LABEL="$(plutil -extract Label raw -o - "$PLIST_BACKUP" 2>/dev/null || true)"
    if [ "$PREVIOUS_LABEL" != "ai.veyra.api" ]; then
      echo "The existing LaunchAgent plist has an unexpected Label; refusing replacement." >&2
      exit 1
    fi
    HAD_PLIST=1
  fi
  LAUNCHCTL_STATE=""
  if LAUNCHCTL_STATE="$(launchctl print "$SERVICE_TARGET" 2>&1)"; then
    JOB_WAS_LOADED=1
    PREVIOUS_PID="$(printf '%s\n' "$LAUNCHCTL_STATE" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
    LOADED_PLIST="$(printf '%s\n' "$LAUNCHCTL_STATE" | awk '$1 == "path" && $2 == "=" {print $3; exit}')"
    if [ "$HAD_PLIST" != "1" ] || [ "$LOADED_PLIST" != "$PLIST" ]; then
      echo "The loaded LaunchAgent is not backed by the expected plist; refusing a non-recoverable replacement." >&2
      echo "loaded_path=${LOADED_PLIST:-<missing>} expected_path=$PLIST" >&2
      exit 1
    fi
  elif ! printf '%s\n' "$LAUNCHCTL_STATE" | grep -Fq "Could not find service"; then
    echo "Could not determine the existing LaunchAgent state; no service or plist changes were made." >&2
    printf '%s\n' "$LAUNCHCTL_STATE" >&2
    exit 1
  fi
  REPLACEMENT_IN_PROGRESS=1
  if [ "$JOB_WAS_LOADED" = "1" ]; then
    if ! launchctl bootout "$SERVICE_TARGET"; then
      REPLACEMENT_IN_PROGRESS=0
      echo "Could not stop the existing LaunchAgent; its plist and running job were left unchanged." >&2
      exit 1
    fi
    if ! wait_for_launchagent_absent; then
      echo "Could not confirm that the existing LaunchAgent finished unloading." >&2
      exit 1
    fi
  fi
  if ! mv "$PLIST_TMP" "$PLIST"; then
    echo "Could not install the validated LaunchAgent plist." >&2
    exit 1
  fi
  PLIST_TMP=""
  READY_NOT_BEFORE="$("$PYTHON_BIN" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
  if ! launchctl bootstrap "$SERVICE_DOMAIN" "$PLIST"; then
    echo "Could not bootstrap the updated LaunchAgent." >&2
    exit 1
  fi
  if ! launchctl kickstart -k "$SERVICE_TARGET"; then
    echo "Could not kickstart the updated LaunchAgent." >&2
    exit 1
  fi
  NEW_PID=""
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    NEW_LAUNCHCTL_STATE="$(launchctl print "$SERVICE_TARGET" 2>&1 || true)"
    NEW_PID="$(printf '%s\n' "$NEW_LAUNCHCTL_STATE" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
    [[ "$NEW_PID" =~ ^[0-9]+$ ]] && break
    sleep 0.1
  done
  if ! [[ "$NEW_PID" =~ ^[0-9]+$ ]] || { [ -n "$PREVIOUS_PID" ] && [ "$NEW_PID" = "$PREVIOUS_PID" ]; }; then
    echo "The updated LaunchAgent did not expose a distinct running PID." >&2
    exit 1
  fi
  if ! wait_for_launchagent_ready "$READY_NOT_BEFORE"; then
    echo "The updated LaunchAgent did not become API-ready." >&2
    exit 1
  fi
  FINAL_LAUNCHCTL_STATE="$(launchctl print "$SERVICE_TARGET" 2>&1 || true)"
  FINAL_PID="$(printf '%s\n' "$FINAL_LAUNCHCTL_STATE" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
  if [ "$FINAL_PID" != "$NEW_PID" ]; then
    echo "The updated LaunchAgent restarted during readiness validation; refusing to commit the replacement." >&2
    exit 1
  fi
  LISTENER_PIDS="$(
    { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fp 2>/dev/null || true; } \
      | sed -n 's/^p//p' \
      | sort -u
  )"
  if [ "$LISTENER_PIDS" != "$FINAL_PID" ]; then
    echo "The ready API listener is not owned exclusively by the validated LaunchAgent PID." >&2
    exit 1
  fi
  EXPECTED_EXECUTABLE="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.executable).resolve())')"
  RUNNING_EXECUTABLE="$(
    lsof -a -p "$FINAL_PID" -d txt -Fn 2>/dev/null \
      | sed -n 's/^n//p' \
      | head -n 1
  )"
  if [ -z "$RUNNING_EXECUTABLE" ] || [ "$RUNNING_EXECUTABLE" != "$EXPECTED_EXECUTABLE" ]; then
    echo "The ready LaunchAgent PID is not using the validated Python executable." >&2
    exit 1
  fi
  REPLACEMENT_IN_PROGRESS=0
  [ -z "$PLIST_BACKUP" ] || rm -f "$PLIST_BACKUP"
  PLIST_BACKUP=""
  veyra_release_update_lock
  trap - EXIT HUP INT TERM
  echo "==> Started launchd service ai.veyra.api"
  echo "    Check status with: ./scripts/status_local.sh"
  exit 0
fi

echo "==> Starting Veyra API at http://$HOST:$PORT"
exec "$PYTHON_BIN" -B -m uvicorn main:app --host "$HOST" --port "$PORT"
