from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from runtime.extension_release_registry import (  # noqa: E402
    ExtensionReleaseRegistry,
    ExtensionReleaseUnauthorizedError,
    ExtensionReleaseUnavailableError,
)
from runtime.extension_release_runtime import (  # noqa: E402
    FailClosedExtensionReleaseRegistry,
    build_extension_release_registry,
)


CONTROL_TOKEN = "phase6-startup-test-control-token"


class StubGate:
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS: {message}")


def expect_raises(error: type[BaseException], call: Callable[[], Any], message: str) -> None:
    try:
        call()
    except error:
        print(f"PASS: {message}")
        return
    raise AssertionError(message)


def write_key(path: Path, *, mode: int = 0o600) -> bytes:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, private_raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return public_raw


def build(store: WorldStateStore, env: dict[str, str]) -> Any:
    return build_extension_release_registry(
        state_store=store,
        generation_gate=StubGate(),
        dynamic_validation_gate=StubGate(),
        env=env,
    )


def assert_safe_failure(
    registry: Any,
    *,
    expected_issue: str,
    control_token: str = "",
) -> None:
    expect(
        isinstance(registry, FailClosedExtensionReleaseRegistry),
        "bad startup configuration returns the fail-closed registry",
    )
    if control_token:
        expect_raises(
            ExtensionReleaseUnauthorizedError,
            lambda: registry.status(control_token="wrong-token"),
            "fail-closed startup status preserves private token authentication",
        )
    status = registry.status(control_token=control_token)
    serialized = json.dumps(status, sort_keys=True)
    expect(
        status["status"] == "fail_closed"
        and status["operational_health"] == "unavailable"
        and status["storage"]["issue"] == expected_issue,
        "startup status exposes an explicit source-free fail-closed reason",
    )
    expect(
        "PRIVATE" not in serialized
        and "BEGIN" not in serialized
        and "/tmp/" not in serialized
        and status["private_key_material_present_in_response"] is False,
        "fail-closed status leaks no path or key material",
    )
    expect_raises(
        ExtensionReleaseUnavailableError,
        lambda: registry.list(
            user_id="user",
            workspace_id="workspace",
            session_id="session",
            control_token=control_token,
        ),
        "all release operations remain unavailable after startup failure",
    )
    expect_raises(
        ExtensionReleaseUnavailableError,
        lambda: registry.deployment_subject(
            release_id="extrel_" + "0" * 24,
            user_id="user",
            workspace_id="workspace",
            session_id="session",
            expected_release_revision=1,
            expected_attestation_digest="0" * 64,
            control_token=control_token,
        ),
        "private deployment handoff also fails closed",
    )


def run() -> None:
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        store = WorldStateStore(root=state_dir)
        key_root = Path(key_dir)
        before = list(key_root.iterdir())
        missing = build(store, {})
        assert_safe_failure(
            missing,
            expected_issue="release_runtime_configuration_missing",
        )
        expect(
            list(key_root.iterdir()) == before,
            "missing configuration never generates a signing key",
        )

        partial = build(
            store,
            {
                "VEYRA_PHASE6_EXTENSION_RELEASE_ENABLED": "1",
                "VEYRA_PHASE6_RELEASE_PRIVATE_KEY_PATH": str(
                    key_root / "absent.key"
                ),
                "VEYRA_LOCAL_API_TOKEN": CONTROL_TOKEN,
            },
        )
        assert_safe_failure(
            partial,
            expected_issue="release_runtime_configuration_invalid",
            control_token=CONTROL_TOKEN,
        )
        expect(
            not (key_root / "absent.key").exists(),
            "partial configuration does not create or repair a private key",
        )

        key_path = key_root / "release-ed25519.key"
        public_raw = write_key(key_path)
        common = {
            "VEYRA_PHASE6_EXTENSION_RELEASE_ENABLED": "1",
            "VEYRA_PHASE6_RELEASE_PRIVATE_KEY_PATH": str(key_path),
            "VEYRA_PHASE6_RELEASE_SIGNING_SERVICE_ID": (
                "veyra.release.signer.startup-smoke"
            ),
            "VEYRA_LOCAL_API_TOKEN": CONTROL_TOKEN,
        }
        bad_public = build(
            store,
            {
                **common,
                "VEYRA_PHASE6_RELEASE_PUBLIC_KEY_B64": base64.b64encode(
                    b"x" * 32
                ).decode("ascii"),
            },
        )
        assert_safe_failure(
            bad_public,
            expected_issue="release_runtime_construction_failed",
            control_token=CONTROL_TOKEN,
        )

        unsafe_key_path = key_root / "unsafe-mode.key"
        unsafe_public = write_key(unsafe_key_path, mode=0o644)
        unsafe_mode = build(
            store,
            {
                **common,
                "VEYRA_PHASE6_RELEASE_PRIVATE_KEY_PATH": str(unsafe_key_path),
                "VEYRA_PHASE6_RELEASE_PUBLIC_KEY_B64": base64.b64encode(
                    unsafe_public
                ).decode("ascii"),
            },
        )
        assert_safe_failure(
            unsafe_mode,
            expected_issue="release_runtime_construction_failed",
            control_token=CONTROL_TOKEN,
        )

        configured = build(
            store,
            {
                **common,
                "VEYRA_PHASE6_RELEASE_PUBLIC_KEY_B64": base64.b64encode(
                    public_raw
                ).decode("ascii"),
            },
        )
        expect(
            type(configured) is ExtensionReleaseRegistry,
            "matching external mode-0600 Ed25519 configuration builds the real registry",
        )
        status = configured.status(control_token=CONTROL_TOKEN)
        expect(
            status["status"]
            == "technical_complete_signed_private_registry_only"
            and status["admission"]["trusted_signing_key"] is True
            and status["private_key_material_present_in_response"] is False,
            "real registry reports a trusted signer without private material",
        )
        expect(
            str(key_path) not in json.dumps(status, sort_keys=True),
            "real registry status does not expose the external private-key path",
        )


if __name__ == "__main__":
    run()
