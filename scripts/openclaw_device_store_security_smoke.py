#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-device-store-") as raw:
        root = Path(raw)
        local = root / "local"
        store_path = local / "openclaw_device.json"
        adapter = OpenClawAdapter(base_url="ws://127.0.0.1:18789")
        adapter.device_store = store_path
        store = {
            "version": 1,
            "deviceId": "device-test",
            "publicKey": "public-test",
            "privateKey": "private-test",
            "tokens": {
                "operator": {
                    "token": "operator-test",
                    "role": "operator",
                    "scopes": ["operator.read"],
                }
            },
        }

        adapter._write_device_store(store)
        assert mode(local) == 0o700
        assert mode(store_path) == 0o600
        assert adapter._read_device_store() == store
        assert not list(local.glob(f".{store_path.name}.*.tmp"))

        os.chmod(store_path, 0o644)
        assert adapter._read_device_store() == store
        assert mode(store_path) == 0o600

        symlink_path = local / "openclaw_device_symlink.json"
        symlink_path.symlink_to(store_path)
        adapter.device_store = symlink_path
        assert adapter._read_device_store() == {}

    print("openclaw_device_store_security_smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
