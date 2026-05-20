from __future__ import annotations

import os
import shutil
from pathlib import Path

from probes.schema import probe_payload


class McpProbe:
    def run(self, text: str = "") -> dict:
        candidates = [
            Path.cwd() / ".mcp.json",
            Path.home() / ".config" / "mcp" / "config.json",
            Path.home() / ".cursor" / "mcp.json",
        ]
        existing = [str(path) for path in candidates if path.exists()]
        cli = shutil.which("mcp") or shutil.which("npx")
        env_servers = [key for key in os.environ if key.startswith("MCP_")]
        status = "available" if existing or env_servers else "not_detected"
        return probe_payload(
            probe="mcp_probe",
            target="mcp_runtime",
            status=status,
            summary="MCP configuration was detected." if status == "available" else "No MCP configuration was detected.",
            confidence=0.75 if status == "available" else 0.55,
            ttl_seconds=300,
            details={"config_files": existing, "cli": cli, "env_keys": env_servers},
        )
