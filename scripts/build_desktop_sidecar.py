#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
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
EXPECTED_TARGET = "aarch64-apple-darwin"
EXPECTED_PYINSTALLER = "6.22.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Apple Silicon Veyra desktop sidecar.")
    parser.add_argument(
        "--target",
        default=EXPECTED_TARGET,
        help="Rust/Tauri sidecar target triple (only aarch64-apple-darwin is supported).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not ENTRYPOINT.exists():
        print(f"Missing desktop backend entrypoint: {ENTRYPOINT}", file=sys.stderr)
        return 2
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        print("Desktop sidecar builds are supported only on Apple Silicon macOS (arm64).", file=sys.stderr)
        return 2
    if sys.version_info[:2] != (3, 11):
        print(f"Veyra desktop sidecar requires Python 3.11.x; selected {sys.version.split()[0]}.", file=sys.stderr)
        return 2
    if args.target != EXPECTED_TARGET:
        print(f"Only target {EXPECTED_TARGET} is supported for this local preview; got {args.target}.", file=sys.stderr)
        return 2
    triple = rust_host_triple()
    if triple != EXPECTED_TARGET:
        print(f"rustc host target must be {EXPECTED_TARGET}; got {triple or '<missing>'}.", file=sys.stderr)
        return 2
    requirements_error = verify_build_requirements()
    if requirements_error:
        print(requirements_error, file=sys.stderr)
        return 2
    pyinstaller_version = installed_pyinstaller_version()
    if pyinstaller_version != EXPECTED_PYINSTALLER:
        print(
            f"PyInstaller {EXPECTED_PYINSTALLER} is required; selected environment has {pyinstaller_version or '<missing>'}.",
            file=sys.stderr,
        )
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
        "--target-architecture",
        "arm64",
    ]
    cmd.extend(add_data_args(ROOT / ".env.example", "."))
    cmd.extend(add_data_args(ROOT / "personas", "personas"))
    cmd.append(str(ENTRYPOINT))

    subprocess.run(cmd, cwd=ROOT, check=True)
    if not binary_path.exists():
        print(f"PyInstaller finished but sidecar binary was not created: {binary_path}", file=sys.stderr)
        return 1
    binary_path.chmod(binary_path.stat().st_mode | 0o111)
    description = macho_description(binary_path)
    if "Mach-O" not in description or "arm64" not in description:
        print(f"Refusing non-arm64 Mach-O sidecar {binary_path}: {description or '<unknown>'}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "built",
                "path": str(binary_path),
                "target": triple,
                "python_version": platform.python_version(),
                "pyinstaller_version": pyinstaller_version,
                "sha256": sha256(binary_path),
                "file": description,
            },
            sort_keys=True,
        )
    )
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


def installed_pyinstaller_version() -> str:
    try:
        return importlib.metadata.version("pyinstaller")
    except importlib.metadata.PackageNotFoundError:
        return ""


def verify_build_requirements() -> str | None:
    requirements = ROOT / "requirements-desktop-build.txt"
    if not requirements.is_file():
        return f"Missing desktop build requirements file: {requirements}"
    text = requirements.read_text(encoding="utf-8")
    if "-r requirements.txt" not in text or "pyinstaller==6.22.0" not in text.lower():
        return "requirements-desktop-build.txt must include -r requirements.txt and pyinstaller==6.22.0"
    return None


def macho_description(path: Path) -> str:
    file_bin = shutil.which("file")
    if not file_bin:
        return ""
    result = subprocess.run([file_bin, "-b", str(path)], text=True, capture_output=True, check=False)
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binary_extension() -> str:
    return ".exe" if platform.system() == "Windows" else ""


def add_data_args(source: Path, target: str) -> list[str]:
    if not source.exists():
        return []
    separator = ";" if platform.system() == "Windows" else ":"
    return ["--add-data", f"{source}{separator}{target}"]


if __name__ == "__main__":
    raise SystemExit(main())
