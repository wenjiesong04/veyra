#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.local_control_guard import LocalControlPolicy


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def main() -> int:
    local = LocalControlPolicy(bind_host="127.0.0.1", token="")
    expect(local.authorize(method="POST", path="/tool-proxy/shell", headers={}).allowed, "loopback remains zero-config")
    wildcard_mismatch = local.authorize(
        method="POST",
        path="/tool-proxy/shell",
        headers={},
        actual_server_host="0.0.0.0",
    )
    expect(
        not wildcard_mismatch.allowed
        and wildcard_mismatch.status_code == 503
        and wildcard_mismatch.code == "listener_binding_mismatch",
        "loopback declaration fails closed on a wildcard listener",
        wildcard_mismatch,
    )
    public_mismatch = local.authorize(
        method="GET",
        path="/setup/status",
        headers={},
        actual_server_host="192.0.2.10",
    )
    expect(
        not public_mismatch.allowed
        and public_mismatch.code == "listener_binding_mismatch",
        "loopback declaration fails closed on a public listener address",
        public_mismatch,
    )
    named_mismatch = local.authorize(
        method="GET",
        path="/setup/status",
        headers={},
        actual_server_host="veyra.example",
    )
    expect(
        not named_mismatch.allowed
        and named_mismatch.code == "listener_binding_mismatch",
        "loopback declaration fails closed on a named external listener",
        named_mismatch,
    )
    expect(
        local.authorize(
            method="GET",
            path="/health",
            headers={},
            actual_server_host="0.0.0.0",
        ).allowed,
        "public health rule remains available on a mismatched listener",
    )
    expect(
        local.authorize(
            method="POST",
            path="/tool-proxy/shell",
            headers={},
            actual_server_host="::ffff:127.0.0.1",
        ).allowed,
        "IPv4-mapped loopback remains local",
    )
    mismatch_status = local.status(actual_server_host="0.0.0.0")
    expect(
        mismatch_status["status"] == "blocked_binding_mismatch"
        and mismatch_status["binding_mismatch"] is True,
        "binding mismatch is explicit in control status",
        mismatch_status,
    )
    dns_rebinding = local.authorize(
        method="POST",
        path="/phase5/feedback",
        headers={
            "origin": "http://attacker.example",
            "host": "attacker.example",
        },
        actual_server_host="127.0.0.1",
    )
    expect(
        not dns_rebinding.allowed
        and dns_rebinding.status_code == 403
        and dns_rebinding.code == "origin_not_allowed",
        "matching attacker Origin and Host cannot DNS-rebind loopback control",
        dns_rebinding,
    )
    expect(
        local.authorize(
            method="POST",
            path="/phase5/feedback",
            headers={
                "origin": "http://127.0.0.1:8000",
                "host": "127.0.0.1:8000",
            },
            actual_server_host="127.0.0.1",
        ).allowed,
        "same-origin loopback console remains allowed",
    )

    exposed_without_token = LocalControlPolicy(bind_host="0.0.0.0", token="")
    health = exposed_without_token.authorize(method="GET", path="/health", headers={})
    expect(health.allowed, "network health read remains public", health)
    setup_read = exposed_without_token.authorize(method="GET", path="/setup/status", headers={})
    expect(not setup_read.allowed and setup_read.status_code == 503, "network setup read fails closed without token", setup_read)
    missing = exposed_without_token.authorize(method="POST", path="/tool-proxy/shell", headers={})
    expect(not missing.allowed and missing.status_code == 503, "network exposure fails closed without token", missing)

    exposed = LocalControlPolicy(bind_host="0.0.0.0", token="secret-token")
    unauthenticated = exposed.authorize(method="POST", path="/memory/patch", headers={})
    expect(not unauthenticated.allowed and unauthenticated.status_code == 401, "network mutation rejects missing token", unauthenticated)
    authenticated = exposed.authorize(
        method="POST",
        path="/memory/patch",
        headers={"authorization": "Bearer secret-token"},
    )
    expect(authenticated.allowed, "valid bearer token permits mutation", authenticated)
    authenticated_setup = exposed.authorize(
        method="GET",
        path="/setup/status",
        headers={"x-veyra-token": "secret-token"},
    )
    expect(authenticated_setup.allowed, "valid token permits protected reads", authenticated_setup)

    bad_origin = local.authorize(
        method="POST",
        path="/runtime/active-loop/start",
        headers={"origin": "https://attacker.example"},
    )
    expect(not bad_origin.allowed and bad_origin.code == "origin_not_allowed", "browser origin is enforced", bad_origin)
    same_origin = local.authorize(
        method="GET",
        path="/console/assets/app.js",
        headers={"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"},
    )
    expect(same_origin.allowed, "same-origin console assets are allowed on the active API port", same_origin)
    mismatched_port = local.authorize(
        method="GET",
        path="/console/assets/app.js",
        headers={"origin": "http://127.0.0.1:8999", "host": "127.0.0.1:8000"},
    )
    expect(not mismatched_port.allowed, "different-port browser origin remains blocked", mismatched_port)
    callback = exposed_without_token.authorize(method="POST", path="/integrations/feishu/events", headers={})
    expect(not callback.allowed, "provider callback is closed by default on exposed bindings", callback)
    callback_policy = LocalControlPolicy(
        bind_host="0.0.0.0",
        token="",
        public_callback_paths={"/integrations/feishu/events"},
    )
    callback_allowed = callback_policy.authorize(method="POST", path="/integrations/feishu/events", headers={})
    expect(callback_allowed.allowed, "explicit signed-provider callback opt-in is supported", callback_allowed)
    original_callback_flag = os.environ.get("VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS")
    original_feishu_token = os.environ.get("FEISHU_VERIFICATION_TOKEN")
    try:
        os.environ["VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS"] = "1"
        os.environ.pop("FEISHU_VERIFICATION_TOKEN", None)
        unsafe_callback = LocalControlPolicy(bind_host="0.0.0.0", token="")
        expect(
            "/integrations/feishu/events" not in unsafe_callback.public_callback_paths,
            "public callback opt-in fails closed without a verification token",
            unsafe_callback.status(),
        )
        os.environ["FEISHU_VERIFICATION_TOKEN"] = "verification-secret"
        verified_callback = LocalControlPolicy(bind_host="0.0.0.0", token="")
        expect(
            "/integrations/feishu/events" in verified_callback.public_callback_paths,
            "verified public callback can be enabled explicitly",
            verified_callback.status(),
        )
    finally:
        if original_callback_flag is None:
            os.environ.pop("VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS", None)
        else:
            os.environ["VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS"] = original_callback_flag
        if original_feishu_token is None:
            os.environ.pop("FEISHU_VERIFICATION_TOKEN", None)
        else:
            os.environ["FEISHU_VERIFICATION_TOKEN"] = original_feishu_token
    expect(exposed.status()["status"] == "token_required", "security status is explicit", exposed.status())

    print("local control guard smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
