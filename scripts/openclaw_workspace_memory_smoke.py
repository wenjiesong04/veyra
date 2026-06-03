#!/usr/bin/env python3
from __future__ import annotations

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
        original_memory_md = memory_md.read_text(encoding="utf-8")

        old_env = {key: os.environ.get(key) for key in ("OPENCLAW_WORKSPACE_DIR", "VEYRA_STATE_DIR", "VEYRA_OPENCLAW_MEMORY_MIRROR_DIR")}
        os.environ["OPENCLAW_WORKSPACE_DIR"] = str(workspace)
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_OPENCLAW_MEMORY_MIRROR_DIR"] = str(root / "mirror")
        try:
            adapter = MissingGatewayMemoryAdapter(base_url="ws://127.0.0.1:18789", api_key="workspace-memory-test")
            summary = adapter.fetch_memory_summary("workspace-session")
            expect(summary.get("status") == "workspace_file_fallback", "summary falls back to workspace files", summary)
            expect("Veyra proactive memory fallback" in str(summary.get("summary")), "summary includes MEMORY.md content", summary)
            expect("PyTorch tracking" in str(summary.get("summary")), "summary includes dated memory note", summary)
            expect(summary.get("sync_status") == "pending_gateway_support", "summary marks pending gateway support", summary)

            write = adapter.write_memory_patch(
                {
                    "session_id": "workspace-session",
                    "topic": "PyTorch",
                    "summary": "User authorized PyTorch tracking digests.",
                    "confidence": 0.9,
                }
            )
            expect(write.get("status") == "workspace_file_fallback", "memory patch falls back to workspace note", write)
            expect(write.get("sync_status") == "pending_gateway_support", "memory patch marks pending gateway support", write)
            note_path = Path(str(write.get("path") or ""))
            expect(note_path.exists() and note_path.name.endswith("-veyra.md"), "Veyra-managed note is written", write)
            note_text = note_path.read_text(encoding="utf-8")
            expect("User authorized PyTorch tracking digests." in note_text, "patch summary is appended", note_text)
            expect(memory_md.read_text(encoding="utf-8") == original_memory_md, "original MEMORY.md is not modified", memory_md.read_text(encoding="utf-8"))
            audit_path = state_root / "logs" / "openclaw_workspace_memory_fallback.jsonl"
            expect(audit_path.exists() and "workspace_file_fallback" in audit_path.read_text(encoding="utf-8"), "fallback audit is written", audit_path)
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
