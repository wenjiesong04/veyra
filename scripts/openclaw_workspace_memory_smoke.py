#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter, OpenClawGatewayError  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class MissingGatewayMemoryAdapter(OpenClawAdapter):
    def _gateway_request(self, method: str, params: dict[str, Any], *, scopes: list[str] | None = None) -> dict[str, Any]:
        if method in {"memory.summary", "memory.patch"}:
            raise OpenClawGatewayError("method_unavailable", f"unknown method: {method}", {"method": method})
        return {}


def main() -> int:
    with TemporaryDirectory(prefix="veyra-openclaw-workspace-memory-") as tmp:
        root = Path(tmp)
        workspace = root / "openclaw-workspace"
        memory_dir = workspace / "memory"
        state_root = root / "state"
        memory_dir.mkdir(parents=True)
        memory_md = workspace / "MEMORY.md"
        daily_md = memory_dir / "2026-06-03.md"
        memory_md.write_text("# OpenClaw Memory\n\nUser is building Veyra proactive memory fallback.\n", encoding="utf-8")
        daily_md.write_text("# Daily Memory\n\nPyTorch tracking should be remembered.\n", encoding="utf-8")
        legacy_scoped = (
            memory_dir
            / "veyra-scoped"
            / "scope-legacy"
            / "2026-07-29-veyra.md"
        )
        legacy_scoped.parent.mkdir(parents=True)
        legacy_scoped.write_text(
            "LEGACY_OPENCLAW_SCOPED_PRIVATE_CONTENT",
            encoding="utf-8",
        )
        original_memory_md = memory_md.read_text(encoding="utf-8")
        openclaw_config = root / "openclaw.json"
        openclaw_config.write_text(
            '{"agents":{"defaults":{"memorySearch":{"extraPaths":[]}}},'
            '"memory":{"qmd":{"paths":[]}}}',
            encoding="utf-8",
        )

        old_env = {
            key: os.environ.get(key)
            for key in (
                "OPENCLAW_CONFIG_PATH",
                "OPENCLAW_WORKSPACE_DIR",
                "VEYRA_STATE_DIR",
                "VEYRA_OPENCLAW_MEMORY_MIRROR_DIR",
            )
        }
        os.environ["OPENCLAW_CONFIG_PATH"] = str(openclaw_config)
        os.environ["OPENCLAW_WORKSPACE_DIR"] = str(workspace)
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_OPENCLAW_MEMORY_MIRROR_DIR"] = str(root / "mirror")
        try:
            adapter = MissingGatewayMemoryAdapter(base_url="ws://127.0.0.1:18789", api_key="workspace-memory-test")
            scope_a = "veyra-memory-v2-scope-a"
            scope_b = "veyra-memory-v2-scope-b"
            initial_summary = adapter.fetch_memory_summary(scope_a)
            expect(initial_summary.get("status") == "workspace_file_empty", "new scope starts empty", initial_summary)
            expect(
                "Veyra proactive memory fallback" not in str(initial_summary.get("summary"))
                and "PyTorch tracking" not in str(initial_summary.get("summary")),
                "legacy shared workspace files are not returned as scoped user memory",
                initial_summary,
            )
            expect(
                "LEGACY_OPENCLAW_SCOPED_PRIVATE_CONTENT"
                not in str(initial_summary.get("summary")),
                "legacy scoped files inside OpenClaw workspace are ignored",
                initial_summary,
            )
            audit_path = state_root / "logs" / "openclaw_workspace_memory_fallback.jsonl"
            expect(not audit_path.exists(), "summary read does not write fallback audit")

            write = adapter.write_memory_patch(
                {
                    "session_id": scope_a,
                    "topic": "PyTorch",
                    "summary": "Scope A authorized PyTorch tracking digests.",
                    "confidence": 0.9,
                }
            )
            expect(write.get("status") == "workspace_file_fallback", "memory patch falls back to Veyra-private scoped note", write)
            expect(
                write.get("fallback_mode") == "veyra_private_mirror",
                "unverified native Memory never writes inside OpenClaw workspace",
                write,
            )
            expect(write.get("sync_status") == "pending_gateway_support", "memory patch marks pending gateway support", write)
            note_path = Path(str(write.get("path") or ""))
            expect(
                note_path.exists()
                and note_path.name.endswith("-veyra.md")
                and "veyra-scoped" in note_path.parts,
                "Veyra-managed note is written under a private scoped directory",
                write,
            )
            expect(
                note_path.resolve().is_relative_to(
                    (root / "mirror").resolve()
                )
                and [
                    path.resolve()
                    for path in memory_dir.rglob("*-veyra.md")
                ]
                == [legacy_scoped.resolve()],
                "fallback data stays outside OpenClaw recursive workspace indexing",
                note_path,
            )
            note_text = note_path.read_text(encoding="utf-8")
            expect("Scope A authorized PyTorch tracking digests." in note_text, "patch summary is appended", note_text)

            summary = adapter.fetch_memory_summary(scope_a)
            expect(summary.get("status") == "workspace_file_fallback", "summary reads its scoped fallback files", summary)
            expect("Scope A authorized PyTorch tracking digests." in str(summary.get("summary")), "scope A reads its own note", summary)
            expect("Veyra proactive memory fallback" not in str(summary.get("summary")), "scope A excludes legacy MEMORY.md", summary)
            expect("PyTorch tracking should be remembered" not in str(summary.get("summary")), "scope A excludes legacy dated note", summary)
            expect(summary.get("sync_status") == "pending_gateway_support", "summary marks pending gateway support", summary)

            write_b = adapter.write_memory_patch(
                {
                    "session_id": scope_b,
                    "topic": "Private B",
                    "summary": "Scope B private note.",
                    "confidence": 0.9,
                }
            )
            summary_b = adapter.fetch_memory_summary(scope_b)
            expect(write_b.get("status") == "workspace_file_fallback", "second scope writes independently", write_b)
            expect("Scope B private note." in str(summary_b.get("summary")), "scope B reads its own note", summary_b)
            expect("Scope A authorized" not in str(summary_b.get("summary")), "scope B cannot read scope A", summary_b)
            expect("Scope B private note." not in str(summary.get("summary")), "scope A cannot read scope B", summary)

            blocked_mirror_root = root / "blocked-memory-root"
            blocked_mirror_root.write_text(
                "not a directory",
                encoding="utf-8",
            )
            adapter.veyra_memory_mirror_dir = blocked_mirror_root
            scope_c = "veyra-memory-v2-scope-c"
            write_c = adapter.write_memory_patch(
                {
                    "session_id": scope_c,
                    "topic": "Mirror C",
                    "summary": "Scope C local mirror note.",
                }
            )
            expect(
                write_c.get("status") == "error",
                "private mirror failure does not fall back into OpenClaw workspace",
                write_c,
            )
            expect(
                [
                    path.resolve()
                    for path in memory_dir.rglob("*-veyra.md")
                ]
                == [legacy_scoped.resolve()],
                "private mirror failure creates no OpenClaw workspace note",
            )
            unsafe_mirror = memory_dir / "veyra-private"
            adapter.veyra_memory_mirror_dir = unsafe_mirror
            unsafe_write = adapter.write_memory_patch(
                {
                    "session_id": "veyra-memory-v2-unsafe",
                    "topic": "Unsafe",
                    "summary": "Must never enter OpenClaw indexing.",
                }
            )
            unsafe_read = adapter.fetch_memory_summary(
                "veyra-memory-v2-unsafe"
            )
            expect(
                unsafe_write.get("status") == "blocked"
                and unsafe_write.get("reason")
                == "memory_mirror_inside_openclaw_index",
                "configured mirror inside OpenClaw workspace fails closed",
                unsafe_write,
            )
            expect(
                unsafe_read.get("status") == "blocked"
                and unsafe_read.get("reason")
                == "memory_mirror_inside_openclaw_index",
                "unsafe configured mirror cannot be read as fallback",
                unsafe_read,
            )
            expect(
                not unsafe_mirror.exists(),
                "unsafe configured mirror creates no indexed private note",
                unsafe_mirror,
            )
            extra_index = root / "openclaw-extra-index"
            openclaw_config.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "memorySearch": {
                                    "extraPaths": [str(extra_index)]
                                }
                            }
                        },
                        "memory": {"qmd": {"paths": []}},
                    }
                ),
                encoding="utf-8",
            )
            extra_index_mirror = extra_index / "veyra-private"
            adapter.veyra_memory_mirror_dir = extra_index_mirror
            extra_index_write = adapter.write_memory_patch(
                {
                    "session_id": "veyra-memory-v2-extra-index",
                    "topic": "Unsafe extra index",
                    "summary": "Must not enter an OpenClaw extraPath.",
                }
            )
            expect(
                extra_index_write.get("status") == "blocked"
                and extra_index_write.get("reason")
                == "memory_mirror_inside_openclaw_index"
                and not extra_index_mirror.exists(),
                "configured OpenClaw extraPath also fails closed",
                extra_index_write,
            )
            openclaw_config.write_text(
                '{"agents":{"defaults":{"memorySearch":{"extraPaths":[]}}},'
                '"memory":{"qmd":{"paths":[]}}}',
                encoding="utf-8",
            )
            adapter.veyra_memory_mirror_dir = root / "mirror"
            expect(
                adapter.fetch_memory_summary(
                    "scope-a\x00scope-b"
                ).get("status")
                == "invalid_memory_scope",
                "fallback rejects embedded control characters",
            )
            expect(
                adapter._workspace_memory_scope_dir(  # noqa: SLF001
                    root / "mirror",
                    "scope-a:scope-b",
                )
                != adapter._workspace_memory_scope_dir(  # noqa: SLF001
                    root / "mirror",
                    "scope-a",
                ),
                "private fallback scope path uses framed identity",
            )

            expect(memory_md.read_text(encoding="utf-8") == original_memory_md, "original MEMORY.md is not modified", memory_md.read_text(encoding="utf-8"))
            expect(daily_md.read_text(encoding="utf-8") == "# Daily Memory\n\nPyTorch tracking should be remembered.\n", "original dated note is not modified")
            audit_text = audit_path.read_text(encoding="utf-8")
            expect("workspace_file_fallback" in audit_text, "fallback writes are audited", audit_path)
            expect('"action": "summary"' not in audit_text, "summary reads are never audited as writes", audit_text)
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    print("openclaw workspace memory smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
