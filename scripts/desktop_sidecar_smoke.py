#!/usr/bin/env python3
"""Start an arm64 desktop sidecar against isolated temporary state.

This is a local packaging smoke only. It never uses the user's state, agency,
ports, or network integrations, and it never treats a shell/stub as a sidecar.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


EXPECTED_PYTHON = "3.11"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a Veyra arm64 desktop sidecar.")
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def file_description(path: Path) -> str:
    result = subprocess.run(["file", "-b", str(path)], text=True, capture_output=True, check=False)
    return result.stdout.strip()


def random_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def setup_payload(base_url: str) -> dict[str, object]:
    request = Request(f"{base_url}/setup/status", headers={"Accept": "application/json"})
    with urlopen(request, timeout=2.0) as response:
        if response.status != 200:
            raise RuntimeError(f"/setup/status returned HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("/setup/status did not return an object")
    return value


def terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def main() -> int:
    args = parse_args()
    binary = args.binary.expanduser().resolve()
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        print("desktop sidecar smoke requires Apple Silicon macOS (arm64)", file=sys.stderr)
        return 2
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(f"sidecar is missing or not executable: {binary}", file=sys.stderr)
        return 2
    description = file_description(binary)
    if "Mach-O" not in description or "arm64" not in description:
        print(f"refusing non-arm64 Mach-O sidecar: {description or '<unknown>'}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="veyra-desktop-sidecar-smoke-") as raw_root:
        root = Path(raw_root)
        state_root = root / "state"
        agency_root = root / "agency"
        data_root = root / "data"
        env_file = root / ".env"
        port = random_port()
        environment = os.environ.copy()
        environment.update(
            {
                "VEYRA_DESKTOP": "1",
                "VEYRA_HOST": "127.0.0.1",
                "VEYRA_PORT": str(port),
                "VEYRA_DESKTOP_DATA_DIR": str(data_root),
                "VEYRA_STATE_ROOT": str(state_root),
                "VEYRA_AGENCY_ROOT": str(agency_root),
                "VEYRA_STATE_DIR": str(state_root),
                "VEYRA_AGENCY_DIR": str(agency_root),
                "VEYRA_ENV_FILE": str(env_file),
                "VEYRA_ACTIVE_LOOP_AUTOSTART": "0",
                "VEYRA_FEISHU_WS_AUTOSTART": "0",
                "VEYRA_CORE_MODEL_ENABLED": "0",
                "VEYRA_LOCAL_API_TOKEN": "",
            }
        )
        stdout_path = root / "sidecar.stdout.log"
        stderr_path = root / "sidecar.stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [str(binary)],
                cwd=str(root),
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            payload: dict[str, object] | None = None
            deadline = time.monotonic() + max(3.0, args.timeout)
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        payload = setup_payload(f"http://127.0.0.1:{port}")
                        break
                    except (OSError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
                        time.sleep(0.25)
            finally:
                terminate(process)
        if process.poll() is None:
            raise RuntimeError("sidecar process did not terminate")
        if payload is None:
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"sidecar did not serve /setup/status within timeout: {stderr_text}")
        if payload.get("app_name") != "Veyra":
            raise RuntimeError(f"unexpected app_name: {payload.get('app_name')!r}")
        desktop = payload.get("desktop")
        platform_payload = payload.get("platform")
        if not isinstance(desktop, dict) or desktop.get("backend_mode") != "desktop_sidecar":
            raise RuntimeError(f"unexpected desktop backend mode: {desktop!r}")
        if not isinstance(platform_payload, dict) or str(platform_payload.get("python", "")).startswith(EXPECTED_PYTHON) is False:
            raise RuntimeError(f"sidecar did not report Python 3.11: {platform_payload!r}")
        print(json.dumps({"status": "passed", "binary": str(binary), "file": description, "port": port, "app_name": payload["app_name"], "backend_mode": desktop["backend_mode"], "python": platform_payload["python"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
