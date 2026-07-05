#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    old_env = os.environ.get("MCP_SMOKE_SERVER")
    os.environ["MCP_SMOKE_SERVER"] = "enabled"
    try:
        with TemporaryDirectory(prefix="veyra-capability-unified-") as tmp:
            store = WorldStateStore(Path(tmp) / "state")
            ops = store.read_json("ops_config.json")
            ops["tool_proxy"] = {
                "browser_executor_enabled": True,
                "browser_allowed_hosts": ["localhost"],
                "api_executor_enabled": True,
                "api_allowed_hosts": ["localhost"],
            }
            store.write_json("ops_config.json", ops)

            snapshot = CapabilityRegistry(store).snapshot()
            capabilities = snapshot.get("capabilities") or {}
            expect(capabilities["safe_shell"]["namespace"] == "tool_proxy", "safe shell in unified capabilities", capabilities.get("safe_shell"))
            expect(capabilities["safe_file_read"]["available"] is True, "safe file read available", capabilities.get("safe_file_read"))
            expect(capabilities["safe_browser_open"]["available"] is True, "safe browser reflects ToolProxy config", capabilities.get("safe_browser_open"))
            expect(capabilities["safe_api_request"]["available"] is True, "safe api reflects ToolProxy config", capabilities.get("safe_api_request"))
            expect("tool_proxy_capabilities" in snapshot and "safe_shell" in snapshot["tool_proxy_capabilities"], "tool proxy namespace view present", snapshot)
            expect(capabilities["mcp_config"]["namespace"] == "mcp", "mcp config in unified capabilities", capabilities.get("mcp_config"))
            expect("mcp_capabilities" in snapshot and "mcp_runtime" in snapshot["mcp_capabilities"], "mcp namespace view present", snapshot)
    finally:
        if old_env is None:
            os.environ.pop("MCP_SMOKE_SERVER", None)
        else:
            os.environ["MCP_SMOKE_SERVER"] = old_env

    print("capability_registry_unified_smoke: ok")


if __name__ == "__main__":
    main()
