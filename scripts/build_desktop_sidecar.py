#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = ROOT / "apps" / "desktop" / "src-tauri"
BIN_DIR = TAURI_ROOT / "binaries"
BUILD_ROOT = ROOT / "build"
ENTRYPOINT = ROOT / "desktop_backend.py"


def main() -> int:
    if not ENTRYPOINT.exists():
        print(f"Missing desktop backend entrypoint: {ENTRYPOINT}", file=sys.stderr)
        return 2
    triple = rust_host_triple()
    if not triple:
        print("rustc is required to determine the Tauri sidecar target triple.", file=sys.stderr)
        print("Install Rust from https://www.rust-lang.org/tools/install, then rerun this script.", file=sys.stderr)
        return 2
    if not pyinstaller_available():
        print("PyInstaller is required to build the Veyra backend sidecar.", file=sys.stderr)
        print("Install it in the active Python environment with: python3 -m pip install pyinstaller", file=sys.stderr)
        return 2

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    binary_name = f"veyra-backend-{triple}{binary_extension()}"
    binary_path = BIN_DIR / binary_name
    if binary_path.exists():
        binary_path.unlink()

    name_without_extension = binary_name[:-4] if binary_name.endswith(".exe") else binary_name
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        name_without_extension,
        "--distpath",
        str(BIN_DIR),
        "--workpath",
        str(BUILD_ROOT / "desktop-sidecar-work"),
        "--specpath",
        str(BUILD_ROOT / "desktop-sidecar-spec"),
        "--paths",
        str(ROOT),
        "--collect-submodules",
        "uvicorn",
        "--collect-submodules",
        "websockets",
        "--collect-submodules",
        "cryptography",
        "--collect-submodules",
        "lark_oapi",
    ]
    cmd.extend(add_data_args(ROOT / ".env.example", "."))
    cmd.extend(add_data_args(ROOT / "personas", "personas"))
    cmd.append(str(ENTRYPOINT))

    subprocess.run(cmd, cwd=ROOT, check=True)
    if not binary_path.exists():
        print(f"PyInstaller finished but sidecar binary was not created: {binary_path}", file=sys.stderr)
        return 1
    if platform.system() != "Windows":
        binary_path.chmod(binary_path.stat().st_mode | 0o111)
    print(f"Built Veyra backend sidecar: {binary_path}")
    return 0


def rust_host_triple() -> str:
    rustc = shutil.which("rustc")
    if not rustc:
        return ""
    direct = subprocess.run([rustc, "--print", "host-tuple"], text=True, capture_output=True)
    if direct.returncode == 0 and direct.stdout.strip():
        return direct.stdout.strip()
    verbose = subprocess.run([rustc, "-Vv"], text=True, capture_output=True, check=False)
    for line in verbose.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    return ""


def pyinstaller_available() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def binary_extension() -> str:
    return ".exe" if platform.system() == "Windows" else ""


def add_data_args(source: Path, target: str) -> list[str]:
    if not source.exists():
        return []
    separator = ";" if platform.system() == "Windows" else ":"
    return ["--add-data", f"{source}{separator}{target}"]


if __name__ == "__main__":
    raise SystemExit(main())
