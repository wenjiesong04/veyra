#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool_proxy.execution_scope import ExecutionScope, ExecutionScopeError  # noqa: E402
from tool_proxy.safe_file import SafeFile  # noqa: E402
from tool_proxy.safe_shell import SafeShell  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def main() -> int:
    with TemporaryDirectory(prefix="veyra-safe-tool-sandbox-") as temp:
        base = Path(temp)
        sandbox = base / "sandbox"
        outside = base / "outside"
        sandbox.mkdir()
        outside.mkdir()
        (sandbox / "nested").mkdir()
        outside_secret = outside / "secret.txt"
        outside_secret.write_text("outside-secret\n", encoding="utf-8")

        scope = ExecutionScope.create(sandbox)
        safe_file = SafeFile(
            scope=scope,
            max_read_bytes=64,
            max_write_bytes=64,
        )
        expect(safe_file.status()["configured"] is True, "SafeFile explicit scope")

        write = safe_file.write_text("nested/value.txt", "inside\n")
        expect(write["status"] == "ok", "write inside sandbox", write)
        expect(
            (sandbox / "nested/value.txt").read_text(encoding="utf-8")
            == "inside\n",
            "inside content persisted",
        )
        expect(
            not list((sandbox / "nested").glob(".veyra-write-*.tmp")),
            "atomic temporary file cleaned",
        )
        read = safe_file.read_text(sandbox / "nested/value.txt")
        expect(
            read["status"] == "ok" and read["content"] == "inside\n",
            "read exact sandbox file",
            read,
        )

        unconfigured = SafeFile().write_text("value.txt", "no")
        expect(
            unconfigured["status"] == "blocked",
            "missing sandbox fails closed",
            unconfigured,
        )
        traversal = safe_file.write_text("../outside/escape.txt", "escape")
        expect(
            traversal["status"] == "blocked"
            and not (outside / "escape.txt").exists(),
            "parent traversal blocked",
            traversal,
        )
        absolute_escape = safe_file.read_text(outside_secret)
        expect(
            absolute_escape["status"] == "blocked",
            "absolute outside read blocked",
            absolute_escape,
        )

        final_link = sandbox / "secret-link"
        final_link.symlink_to(outside_secret)
        symlink_read = safe_file.read_text(final_link)
        symlink_write = safe_file.write_text(final_link, "changed")
        expect(
            symlink_read["status"] == "blocked"
            and symlink_write["status"] == "blocked"
            and outside_secret.read_text(encoding="utf-8") == "outside-secret\n",
            "final symlink read/write blocked",
            {"read": symlink_read, "write": symlink_write},
        )

        parent_link = sandbox / "outside-link"
        parent_link.symlink_to(outside, target_is_directory=True)
        intermediate = safe_file.write_text("outside-link/escape.txt", "escape")
        expect(
            intermediate["status"] == "blocked"
            and not (outside / "escape.txt").exists(),
            "intermediate symlink blocked",
            intermediate,
        )
        directory_read = safe_file.read_text("nested")
        expect(
            directory_read["status"] == "blocked",
            "directory rejected",
            directory_read,
        )
        if hasattr(os, "mkfifo"):
            fifo = sandbox / "pipe"
            os.mkfifo(fifo)
            fifo_read = safe_file.read_text("pipe")
            expect(
                fifo_read["status"] == "blocked",
                "FIFO rejected without opening",
                fifo_read,
            )

        (sandbox / "large.txt").write_bytes(b"x" * 65)
        large_read = safe_file.read_text("large.txt")
        large_write = safe_file.write_text("too-large.txt", "x" * 65)
        expect(
            large_read["status"] == "blocked",
            "read budget enforced",
            large_read,
        )
        expect(
            large_write["status"] == "blocked"
            and not (sandbox / "too-large.txt").exists(),
            "write budget enforced",
            large_write,
        )
        (sandbox / "binary.txt").write_bytes(b"\xff")
        binary = safe_file.read_text("binary.txt")
        expect(binary["status"] == "error", "non-UTF8 text rejected", binary)

        fixture_dir = base / "fixtures"
        fixture_dir.mkdir()
        probe = executable(
            fixture_dir / "probe",
            "#!/bin/sh\n"
            "printf '%s|%s|%s|%s\\n' \"${VEYRA_TEST_SECRET-unset}\" "
            "\"$PWD\" \"$HOME\" \"$TMPDIR\"\n",
        )
        sleeper = executable(
            fixture_dir / "sleeper",
            "#!/bin/sh\n"
            "if [ \"${1-}\" = child ]; then\n"
            "  while :; do :; done\n"
            "fi\n"
            "\"$0\" child &\n"
            "wait\n",
        )
        flood = executable(
            fixture_dir / "flood",
            "#!/bin/sh\nwhile :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done\n",
        )

        old_secret = os.environ.get("VEYRA_TEST_SECRET")
        os.environ["VEYRA_TEST_SECRET"] = "must-not-leak"
        try:
            safe_shell = SafeShell(
                scope=scope,
                timeout_seconds=2.0,
                max_output_bytes=1024,
                executable_allowlist={
                    "echo": probe,
                    "true": sleeper,
                    "printf": flood,
                },
            )
            clean = safe_shell.run(["echo"])
        finally:
            if old_secret is None:
                os.environ.pop("VEYRA_TEST_SECRET", None)
            else:
                os.environ["VEYRA_TEST_SECRET"] = old_secret
        expected_scope = str(scope.root)
        expect(
            clean["status"] == "ok"
            and clean["stdout"].strip()
            == f"unset|{expected_scope}|{expected_scope}|{expected_scope}",
            "SafeShell clean env and fixed cwd",
            clean,
        )
        expect(
            safe_shell.run(["sh", "-c", "echo unsafe"])["status"] == "blocked",
            "shell wrapper blocked",
        )
        expect(
            safe_shell.run(["echo", "value;"])["status"] == "blocked",
            "shell-like token blocked",
        )
        expect(
            safe_shell.run(["echo", " leading"])["status"] == "blocked",
            "non-normalized argv blocked",
        )

        bounded_shell = SafeShell(
            scope=scope,
            timeout_seconds=0.25,
            max_output_bytes=1024,
            executable_allowlist={
                "true": sleeper,
                "printf": flood,
            },
        )
        timeout = bounded_shell.run(["true"])
        expect(
            timeout["status"] == "timeout"
            and timeout["termination_reason"] == "timeout",
            "timeout kills process group",
            timeout,
        )
        output_shell = SafeShell(
            scope=scope,
            timeout_seconds=2.0,
            max_output_bytes=1024,
            executable_allowlist={"printf": flood},
        )
        flooded = output_shell.run(["printf"])
        expect(
            flooded["status"] == "error"
            and flooded["termination_reason"] == "output_limit"
            and len(flooded["stdout"].encode("utf-8")) <= 1024,
            "output budget terminates process",
            flooded,
        )

        identity_target = executable(
            fixture_dir / "identity",
            "#!/bin/sh\nprintf 'before\\n'\n",
        )
        identity_shell = SafeShell(
            scope=scope,
            executable_allowlist={"echo": identity_target},
        )
        identity_target.write_text(
            "#!/bin/sh\nprintf 'after!\\n'\n", encoding="utf-8"
        )
        identity_target.chmod(identity_target.stat().st_mode | stat.S_IXUSR)
        changed = identity_shell.run(["echo"])
        expect(
            changed["status"] == "blocked"
            and "identity changed" in changed["reason"],
            "executable mutation fails closed",
            changed,
        )

        link_root = base / "sandbox-link"
        link_root.symlink_to(sandbox, target_is_directory=True)
        try:
            ExecutionScope.create(link_root)
        except ExecutionScopeError:
            pass
        else:
            raise AssertionError("symlink sandbox root was accepted")
        print("ok - symlink sandbox root blocked")

    print("safe_tool_sandbox_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
