#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routers.local_setup import _merge_env_text, _redact_env_value, _validate_env_updates  # noqa: E402


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")
    print(f"ok - {message}")


def main() -> int:
    original = "\n".join(
        [
            "# Veyra local config",
            "VEYRA_CORE_MODEL_ENABLED=0",
            "FEISHU_APP_SECRET=old-secret",
            "UNRELATED=value",
        ]
    )
    updates = {
        "VEYRA_CORE_MODEL_ENABLED": True,
        "FEISHU_APP_SECRET": "new-secret",
        "OPENCLAW_BASE_URL": "http://127.0.0.1:18789",
    }
    merged = _merge_env_text(original, updates)
    expect("VEYRA_CORE_MODEL_ENABLED=1" in merged, "boolean env values are normalized")
    expect("FEISHU_APP_SECRET=new-secret" in merged, "existing secret value is replaced")
    expect("OPENCLAW_BASE_URL=http://127.0.0.1:18789" in merged, "new allowed key is appended")
    expect("UNRELATED=value" in merged, "unmanaged env keys are preserved")
    expect(_redact_env_value("FEISHU_APP_SECRET", "new-secret") == "***", "secrets are redacted")
    expect(_redact_env_value("OPENCLAW_BASE_URL", "http://127.0.0.1:18789").startswith("http"), "non-secret values remain visible")

    try:
        _validate_env_updates({"PATH": "/tmp"})
    except HTTPException as exc:
        expect(exc.status_code == 422, "unsupported env keys are rejected")
    else:
        raise AssertionError("unsupported env key was accepted")

    print("local_setup_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
