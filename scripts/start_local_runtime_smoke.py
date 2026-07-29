#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
START_SCRIPT = ROOT / "scripts" / "start_local.sh"
INSTALL_SCRIPT = ROOT / "scripts" / "install_local.sh"
RUNTIME_HELPER = ROOT / "scripts" / "veyra_python_runtime.sh"
STATUS_SCRIPT = ROOT / "scripts" / "status_local.sh"


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")
    print(f"ok - {message}")


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "VEYRA_PYTHON",
        "PYTHON",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "FEISHU_CA_BUNDLE",
        "LARK_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
    ):
        env.pop(key, None)
    return env


def run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=script.parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def copy_runtime_fixture(target: Path) -> Path:
    scripts_dir = target / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(START_SCRIPT, scripts_dir / START_SCRIPT.name)
    shutil.copy2(INSTALL_SCRIPT, scripts_dir / INSTALL_SCRIPT.name)
    shutil.copy2(RUNTIME_HELPER, scripts_dir / RUNTIME_HELPER.name)
    shutil.copy2(STATUS_SCRIPT, scripts_dir / STATUS_SCRIPT.name)
    shutil.copy2(ROOT / "requirements.txt", target / "requirements.txt")
    return scripts_dir / START_SCRIPT.name


def write_fake_macos_tools(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    tools = {
        "uname": "#!/bin/bash\necho Darwin\n",
        "plutil": """#!/bin/bash
if [ "${1:-}" = "-extract" ] && [ "${2:-}" = "Label" ]; then
  echo ai.veyra.api
fi
exit 0
""",
        "shlock": """#!/bin/bash
if [ "${FAKE_LOCK_HELD:-0}" = "1" ]; then
  exit 1
fi
lock_file=""
pid="$$"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -f) lock_file="$2"; shift 2 ;;
    -p) pid="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s\n' "$pid" > "$lock_file"
""",
        "launchctl": """#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in
  print)
    if [ "${FAKE_PRINT_ERROR:-0}" = "1" ]; then
      echo "Permission denied while reading launchd state"
      exit 77
    fi
    if [ ! -f "$FAKE_SERVICE_STATE" ]; then
      echo "Bad request."
      echo "Could not find service \"ai.veyra.api\" in domain for user gui: 501"
      exit 113
    fi
    echo "gui/501/ai.veyra.api = {"
    echo "  path = $FAKE_PLIST_PATH"
    echo "  state = running"
    echo "  pid = $(cat "$FAKE_SERVICE_STATE")"
    echo "}"
    ;;
  bootout)
    if [ -f "${FAKE_BOOTOUT_FAIL_ONCE:-/nonexistent}" ]; then
      rm -f "$FAKE_BOOTOUT_FAIL_ONCE"
      exit 72
    fi
    rm -f "$FAKE_SERVICE_STATE"
    ;;
  bootstrap)
    if [ -f "${FAKE_BOOTSTRAP_FAIL_ONCE:-/nonexistent}" ]; then
      rm -f "$FAKE_BOOTSTRAP_FAIL_ONCE"
      exit 73
    fi
    plist="${@: -1}"
    if grep -q "OLD_PLIST_MARKER" "$plist"; then
      echo 4242 > "$FAKE_SERVICE_STATE"
    else
      echo 5252 > "$FAKE_SERVICE_STATE"
    fi
    ;;
  kickstart)
    if [ -f "${FAKE_KICKSTART_FAIL_ONCE:-/nonexistent}" ]; then
      rm -f "$FAKE_KICKSTART_FAIL_ONCE"
      exit 74
    fi
    ;;
esac
""",
    }
    for name, source in tools.items():
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)


def main() -> int:
    start_source = START_SCRIPT.read_text(encoding="utf-8")
    install_source = INSTALL_SCRIPT.read_text(encoding="utf-8")
    expect("$(command -v python3)" not in start_source, "runtime never falls back to PATH python3")
    expect('${PYTHON:-python3}' not in install_source, "installer never bootstraps from implicit PATH python3")
    expect("plutil -lint" in start_source and "mktemp" in start_source and 'mv "$PLIST_TMP" "$PLIST"' in start_source, "LaunchAgent plist is validated before atomic replacement")
    expect(start_source.index('RUNTIME_INFO=') < start_source.index('launchctl bootout'), "runtime preflight happens before service replacement")
    expect(
        'WorldStateStore(Path(sys.argv[1]))' in install_source
        and '"$PYTHON_BIN" - "$SELECTED_STATE_ROOT"' in install_source,
        "installer initializes the same resolved state root used by its writer guard",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir)
        fixture_start = copy_runtime_fixture(fixture_root)

        valid_env = clean_env()
        valid_env["VEYRA_PYTHON"] = sys.executable
        valid = run(fixture_start, "--check-runtime", env=valid_env)
        expect(valid.returncode == 0, "explicit synchronized Python 3.11 runtime passes", valid.stderr)
        expect('"python_version": "3.11.' in valid.stdout, "runtime check reports Python 3.11", valid.stdout)
        expect('"requirements_verified"' in valid.stdout, "runtime check verifies pinned requirements", valid.stdout)

        missing_env = clean_env()
        missing = run(fixture_start, "--check-runtime", env=missing_env)
        expect(missing.returncode == 2, "missing managed environment fails closed", (missing.stdout, missing.stderr))
        expect("No Veyra Python environment was selected" in missing.stderr, "failure explains how to select Veyra Python", missing.stderr)

        fixture_install = fixture_root / "scripts" / INSTALL_SCRIPT.name
        missing_install = run(fixture_install, env=missing_env)
        expect(missing_install.returncode == 2, "installer without an explicit managed environment fails closed", (missing_install.stdout, missing_install.stderr))
        expect(not (fixture_root / ".venv").exists(), "failed installer does not create a PATH-derived environment")
        fixture_env_file = fixture_root / ".env"
        fixture_env_file.write_text("VEYRA_CORE_MODEL_ENABLED=0\n", encoding="utf-8")
        fixture_env_file.chmod(0o644)
        private_env_failure = run(fixture_install, env=missing_env)
        expect(private_env_failure.returncode == 2, "missing interpreter still fails after protecting .env")
        expect(
            fixture_env_file.stat().st_mode & 0o777 == 0o600,
            "installer protects existing .env before later failure points",
            oct(fixture_env_file.stat().st_mode & 0o777),
        )

        invalid_env = clean_env()
        invalid_env["VEYRA_PYTHON"] = str(fixture_root / "missing-python")
        invalid = run(fixture_start, "--check-runtime", env=invalid_env)
        expect(invalid.returncode == 2, "invalid explicit interpreter never falls back", (invalid.stdout, invalid.stderr))

        wrapper = fixture_root / "python-without-site-packages"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n', encoding="utf-8")
        wrapper.chmod(0o700)
        missing_deps_env = clean_env()
        missing_deps_env["VEYRA_PYTHON"] = str(wrapper)
        missing_deps = run(fixture_start, "--check-runtime", env=missing_deps_env)
        expect(missing_deps.returncode != 0, "missing required packages fail before launch", (missing_deps.stdout, missing_deps.stderr))
        expect("dependencies are not synchronized" in missing_deps.stderr, "dependency failure is explicit", missing_deps.stderr)

        empty_ca = fixture_root / "empty-ca.pem"
        empty_ca.write_bytes(b"")
        empty_ca_env = clean_env()
        empty_ca_env["VEYRA_PYTHON"] = sys.executable
        empty_ca_env["SSL_CERT_FILE"] = str(empty_ca)
        empty_ca_result = run(fixture_start, "--check-runtime", env=empty_ca_env)
        expect(empty_ca_result.returncode != 0, "empty CA bundle fails runtime preflight", empty_ca_result.stderr)
        expect(
            "could not load the selected TLS CA bundle" in empty_ca_result.stderr
            or "loaded no trusted certificates" in empty_ca_result.stderr,
            "CA failure is explicit",
            empty_ca_result.stderr,
        )

        global_python_313 = Path("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3")
        if global_python_313.is_file():
            wrong_version_env = clean_env()
            wrong_version_env["VEYRA_PYTHON"] = str(global_python_313)
            # A valid explicit CA must not let the later dependency/TLS probe
            # overwrite the Python-version preflight failure.
            wrong_version_env["SSL_CERT_FILE"] = str(
                Path(sys.prefix) / "ssl" / "cert.pem"
            )
            wrong_version = run(fixture_start, "--check-runtime", env=wrong_version_env)
            expect(wrong_version.returncode != 0, "Python 3.13 is rejected", (wrong_version.stdout, wrong_version.stderr))
            expect("requires Python 3.11.x" in wrong_version.stderr, "wrong-version failure is explicit", wrong_version.stderr)
            expect("Using validated Veyra runtime" not in wrong_version.stdout, "wrong-version runtime is never labeled validated", wrong_version.stdout)

            wrong_installer_env = clean_env()
            wrong_installer_env["PYTHON"] = str(global_python_313)
            wrong_installer = run(fixture_install, env=wrong_installer_env)
            expect(wrong_installer.returncode != 0, "installer rejects Python 3.13 before creating .venv", (wrong_installer.stdout, wrong_installer.stderr))
            expect(not (fixture_root / ".venv").exists(), "wrong-version installer leaves no project environment")

    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir)
        copy_runtime_fixture(fixture_root)
        (fixture_root / ".venv" / "bin").mkdir(parents=True)
        (fixture_root / ".venv" / "bin" / "python").symlink_to(sys.executable)
        forged_env = clean_env()
        forged_install = run(fixture_root / "scripts" / INSTALL_SCRIPT.name, env=forged_env)
        expect(forged_install.returncode != 0, "forged project venv symlink is rejected", forged_install.stderr)
        expect(
            "resolved outside" in forged_install.stderr
            and "self-contained Veyra Python 3.11 environment" in forged_install.stderr,
            "forged venv failure explains the project boundary",
            forged_install.stderr,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir)
        fixture_start = copy_runtime_fixture(fixture_root)
        fake_home = fixture_root / "home"
        plist = fake_home / "Library" / "LaunchAgents" / "ai.veyra.api.plist"
        plist.parent.mkdir(parents=True)
        fake_bin = fixture_root / "fake-bin"
        write_fake_macos_tools(fake_bin)
        service_state = fixture_root / "service.pid"
        launchctl_log = fixture_root / "launchctl.log"

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            unused_port = probe.getsockname()[1]

        def launchd_failure(marker_name: str | None = None, *, print_error: bool = False, lock_held: bool = False) -> subprocess.CompletedProcess[str]:
            plist.write_text("OLD_PLIST_MARKER\n", encoding="utf-8")
            service_state.write_text("4242\n", encoding="utf-8")
            launchctl_log.write_text("", encoding="utf-8")
            env = clean_env()
            env.update(
                {
                    "HOME": str(fake_home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "VEYRA_PYTHON": sys.executable,
                    "VEYRA_LAUNCHD_READY_TIMEOUT_SECONDS": "0.2",
                    "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                    "FAKE_SERVICE_STATE": str(service_state),
                    "FAKE_PLIST_PATH": str(plist),
                    "FAKE_PRINT_ERROR": "1" if print_error else "0",
                    "FAKE_LOCK_HELD": "1" if lock_held else "0",
                }
            )
            if marker_name:
                marker = fixture_root / marker_name
                marker.write_text("fail once\n", encoding="utf-8")
                env[marker_name.upper()] = str(marker)
            return run(fixture_start, "--launchd", f"--port={unused_port}", env=env)

        for marker_name, label in (
            ("fake_bootout_fail_once", "bootout failure"),
            ("fake_bootstrap_fail_once", "bootstrap failure"),
            ("fake_kickstart_fail_once", "kickstart failure"),
        ):
            result = launchd_failure(marker_name)
            expect(result.returncode != 0, f"{label} aborts LaunchAgent replacement", (result.stdout, result.stderr))
            expect(plist.read_text(encoding="utf-8") == "OLD_PLIST_MARKER\n", f"{label} preserves the previous plist")
            expect(service_state.read_text(encoding="utf-8").strip() == "4242", f"{label} preserves or restores the previous job")
            expect(not list(plist.parent.glob(".ai.veyra.api.previous.*")), f"{label} leaves no orphaned rollback file")

        readiness_failure = launchd_failure()
        expect(readiness_failure.returncode != 0, "API readiness failure rolls back LaunchAgent replacement", readiness_failure.stderr)
        expect(plist.read_text(encoding="utf-8") == "OLD_PLIST_MARKER\n", "readiness failure restores the previous plist")
        expect(service_state.read_text(encoding="utf-8").strip() == "4242", "readiness failure restores the previous job")

        unknown_state = launchd_failure(print_error=True)
        expect(unknown_state.returncode != 0, "unknown launchctl state fails before replacement", unknown_state.stderr)
        expect(plist.read_text(encoding="utf-8") == "OLD_PLIST_MARKER\n", "unknown launchctl state preserves the previous plist")
        expect(service_state.read_text(encoding="utf-8").strip() == "4242", "unknown launchctl state leaves the previous job running")

        locked = launchd_failure(lock_held=True)
        expect(locked.returncode != 0, "concurrent runtime update lock blocks LaunchAgent replacement", locked.stderr)
        expect(plist.read_text(encoding="utf-8") == "OLD_PLIST_MARKER\n", "lock contention leaves the previous plist unchanged")

    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir)
        copy_runtime_fixture(fixture_root)
        fake_home = fixture_root / "home"
        plist = fake_home / "Library" / "LaunchAgents" / "ai.veyra.api.plist"
        plist.parent.mkdir(parents=True)
        fake_bin = fixture_root / "fake-bin"
        write_fake_macos_tools(fake_bin)
        status_python = fixture_root / "status-python"
        status_python.write_text(
            f"""#!/bin/bash
if [ "$1" = "-" ] && [ "${{2:-}}" = "127.0.0.1" ]; then
  echo status-used-launchagent-python
  exit 0
fi
exec "{sys.executable}" "$@"
""",
            encoding="utf-8",
        )
        status_python.chmod(0o700)
        plist.write_text(
            f"<plist><array><string>--service-python={status_python}</string></array></plist>\n",
            encoding="utf-8",
        )
        status_env = clean_env()
        status_env.update(
            {
                "HOME": str(fake_home),
                "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            }
        )
        status_result = run(fixture_root / "scripts" / STATUS_SCRIPT.name, env=status_env)
        expect(status_result.returncode == 0, "fresh shell status resolves the LaunchAgent interpreter", status_result.stderr)
        expect("status-used-launchagent-python" in status_result.stdout, "status client uses the plist-bound interpreter")

    from scripts import feishu_live_diagnostic as feishu_diagnostic
    from scripts import runtime_reproducibility_validation as reproducibility

    class FakeJsonResponse:
        def __enter__(self) -> "FakeJsonResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    captured_requests: list[object] = []

    def capture_request(request: object, *, timeout: float) -> FakeJsonResponse:
        del timeout
        captured_requests.append(request)
        return FakeJsonResponse()

    original_token = os.environ.get("VEYRA_LOCAL_API_TOKEN")
    original_repro_urlopen = reproducibility.urlopen
    original_feishu_urlopen = feishu_diagnostic.urlopen
    try:
        os.environ["VEYRA_LOCAL_API_TOKEN"] = "runtime-smoke-token"
        reproducibility.urlopen = capture_request  # type: ignore[assignment]
        feishu_diagnostic.urlopen = capture_request  # type: ignore[assignment]
        reproducibility.get_json("/health")
        feishu_diagnostic.request_json("/integrations/feishu/ws/status")
        expect(len(captured_requests) == 2, "protected diagnostics issue both test requests")
        for captured in captured_requests:
            header_items = {
                str(key).lower(): value
                for key, value in captured.header_items()  # type: ignore[attr-defined]
            }
            expect(
                header_items.get("x-veyra-token") == "runtime-smoke-token",
                "protected diagnostic carries the configured local API token",
                header_items,
            )
    finally:
        reproducibility.urlopen = original_repro_urlopen
        feishu_diagnostic.urlopen = original_feishu_urlopen
        if original_token is None:
            os.environ.pop("VEYRA_LOCAL_API_TOKEN", None)
        else:
            os.environ["VEYRA_LOCAL_API_TOKEN"] = original_token

    original_repro_safe_get = reproducibility.safe_get
    try:
        def fake_repro_get(path: str, *, timeout: float = 10.0) -> dict[str, object]:
            if path == "/health":
                return {"status": "healthy"}
            if path == "/core/model/status":
                return {"status": "unconfigured", "configured": False}
            if path == "/agent/status":
                return {"status": "unavailable", "validation": {"status": "validation_pending"}}
            if path == "/channels":
                return {"status": "success"}
            return {"status": "error", "request_error": True, "error_type": "URLError"}

        reproducibility.safe_get = fake_repro_get  # type: ignore[assignment]
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                reproducibility.main()
        except AssertionError:
            pass
        else:
            raise AssertionError("Feishu status endpoint failure was accepted by runtime reproducibility validation")
        expect(True, "runtime reproducibility fails when the Feishu status route is unreachable")

        def fake_processing_failure(path: str, *, timeout: float = 10.0) -> dict[str, object]:
            del timeout
            if path == "/health":
                return {"status": "healthy"}
            if path == "/core/model/status":
                return {"status": "unconfigured", "configured": False}
            if path == "/agent/status":
                return {"status": "unavailable", "validation": {"status": "validation_pending"}}
            if path == "/channels":
                return {"status": "success"}
            return {
                "status": "running",
                "configured": True,
                "thread_alive": True,
                "connected": True,
                "last_event_after_start": True,
                "last_processed_after_start": True,
                "last_reply_sent_after_start": True,
                "processing_failure_unrecovered": True,
            }

        reproducibility.safe_get = fake_processing_failure  # type: ignore[assignment]
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                reproducibility.main()
        except AssertionError:
            pass
        else:
            raise AssertionError("Unrecovered Feishu processing failure was accepted by runtime reproducibility validation")
        expect(True, "runtime reproducibility fails on unrecovered Feishu processing errors")
    finally:
        reproducibility.safe_get = original_repro_safe_get

    original_feishu_safe_request = feishu_diagnostic.safe_request
    original_send_flag = os.environ.get("VEYRA_FEISHU_DIAGNOSTIC_SEND")
    try:
        def fake_feishu_request(
            path: str,
            *,
            method: str = "GET",
            payload: dict[str, object] | None = None,
            timeout: float = 15.0,
        ) -> dict[str, object]:
            del payload, timeout
            if path == "/integrations/feishu/ws/status":
                return {
                    "status": "running",
                    "configured": True,
                    "thread_alive": True,
                    "started_at": "2026-07-30T00:00:00+00:00",
                    "last_connected_at": "2026-07-30T00:00:01+00:00",
                    "last_event_after_start": True,
                    "last_processed_after_start": True,
                    "last_reply_sent_after_start": True,
                    "processing_failure_unrecovered": False,
                }
            if method == "POST":
                return {"status": "error", "error_type": "URLError"}
            if path == "/channels/sessions":
                return {"sessions": {"feishu:test": {"channel": "feishu"}}}
            if path.startswith("/channels/outbox"):
                return {"items": []}
            return {"status": "success"}

        feishu_diagnostic.safe_request = fake_feishu_request  # type: ignore[assignment]
        os.environ["VEYRA_FEISHU_DIAGNOSTIC_SEND"] = "1"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            diagnostic_rc = feishu_diagnostic.main()
        expect(diagnostic_rc != 0, "explicit Feishu diagnostic send failure cannot reuse historical reply evidence")
    finally:
        feishu_diagnostic.safe_request = original_feishu_safe_request
        if original_send_flag is None:
            os.environ.pop("VEYRA_FEISHU_DIAGNOSTIC_SEND", None)
        else:
            os.environ["VEYRA_FEISHU_DIAGNOSTIC_SEND"] = original_send_flag

    print("start local runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
