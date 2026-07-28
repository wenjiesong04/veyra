#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.agent_contract import (
    AGENT_CONTRACT_VERSION,
    normalize_capabilities,
)
from interface.provider_certification import certify_agent_provider


NOW = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
FEATURES = {
    "structured_task_packet": True,
    "rendered_prompt_fallback": True,
    "task_status": True,
    "stop_task": True,
}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def certify(
    runtime: str,
    capabilities: dict[str, Any],
    *,
    observed_at: datetime | None = None,
    trusted_native_adapter: bool = False,
) -> dict[str, Any]:
    return certify_agent_provider(
        runtime=runtime,
        capabilities=capabilities,
        observed_at=observed_at or (NOW - timedelta(seconds=10)),
        now=NOW,
        ttl_seconds=60,
        trusted_native_adapter=trusted_native_adapter,
    )


def main() -> int:
    native_openclaw = normalize_capabilities(
        {
            "runtime": "openclaw",
            "status": "available",
            "connected": True,
            "features": FEATURES,
            "compatibility": {
                "native_adapter": True,
                "status": "compatible",
                "required_methods": {
                    "agent": True,
                    "agent.wait": True,
                },
            },
        },
        runtime="openclaw",
    )
    native_result = certify(
        "openclaw",
        native_openclaw,
        trusted_native_adapter=True,
    )
    expect(
        native_result["certification_status"] == "validated"
        and native_result["validated"] is True
        and native_result["diagnostic_reads_eligible"] is True
        and native_result["read_only_dispatch_allowed"] is False,
        "fresh native OpenClaw v2 observation with explicit features validates",
        native_result,
    )

    spoofed_native = certify("openclaw", native_openclaw)
    expect(
        spoofed_native["validated"] is False
        and spoofed_native["certification_status"] == "unverified"
        and "contract_version_not_advertised"
        in spoofed_native["issues"]
        and spoofed_native["evidence"]["native_adapter"] is False
        and spoofed_native["evidence"]["native_adapter_claimed"] is True,
        "remote native-adapter claim cannot replace local adapter provenance",
        spoofed_native,
    )

    generic_result = certify(
        "candidate-runtime",
        {
            "runtime": "candidate-runtime",
            "status": "available",
            "connected": True,
            "contract_version": AGENT_CONTRACT_VERSION,
            "features": dict(FEATURES),
            "compatibility": {
                "status": "compatible",
                "native_adapter": False,
            },
        },
    )
    expect(
        generic_result["certification_status"] == "validated"
        and generic_result["validated"] is True,
        "generic provider validates only when its raw observation advertises exact v2 and every required feature",
        generic_result,
    )

    optimistic_hermes = normalize_capabilities(
        {
            "runtime": "hermes",
            "status": "available",
            "connected": True,
        },
        runtime="hermes",
    )
    hermes_result = certify("hermes", optimistic_hermes)
    optimistic_custom = normalize_capabilities(
        {
            "runtime": "custom",
            "status": "available",
            "connected": True,
        },
        runtime="custom",
    )
    custom_result = certify("custom", optimistic_custom)
    expect(
        hermes_result["certification_status"] == "unverified"
        and custom_result["certification_status"] == "unverified"
        and hermes_result["diagnostic_reads_eligible"] is False
        and custom_result["diagnostic_reads_eligible"] is False
        and any(
            issue.startswith("required_feature_not_advertised:")
            for issue in hermes_result["issues"]
        ),
        "normalizer defaults cannot certify unverified Hermes or custom runtimes",
        {
            "hermes": hermes_result,
            "custom": custom_result,
        },
    )

    stale = certify(
        "candidate-runtime",
        {
            "runtime": "candidate-runtime",
            "status": "available",
            "connected": True,
            "contract_version": AGENT_CONTRACT_VERSION,
            "features": dict(FEATURES),
            "compatibility": {"status": "compatible"},
        },
        observed_at=NOW - timedelta(seconds=61),
    )
    expect(
        stale["certification_status"] == "stale"
        and stale["validated"] is False
        and stale["diagnostic_reads_eligible"] is False,
        "stale compatibility evidence fails closed",
        stale,
    )

    wrong_contract = certify(
        "candidate-runtime",
        {
            "runtime": "candidate-runtime",
            "status": "available",
            "connected": True,
            "contract_version": "veyra.agent_adapter.v1",
            "features": dict(FEATURES),
            "compatibility": {"status": "compatible"},
        },
    )
    missing_feature = certify(
        "candidate-runtime",
        {
            "runtime": "candidate-runtime",
            "status": "available",
            "connected": True,
            "contract_version": AGENT_CONTRACT_VERSION,
            "features": {
                **FEATURES,
                "stop_task": False,
            },
            "compatibility": {"status": "compatible"},
        },
    )
    mismatched_runtime = certify(
        "candidate-runtime",
        {
            "runtime": "other-runtime",
            "status": "available",
            "connected": True,
            "contract_version": AGENT_CONTRACT_VERSION,
            "features": dict(FEATURES),
            "compatibility": {"status": "compatible"},
        },
    )
    expect(
        wrong_contract["certification_status"] == "incompatible"
        and missing_feature["certification_status"] == "incompatible"
        and mismatched_runtime["certification_status"] == "incompatible",
        "contract, feature, and runtime identity mismatches are incompatible",
        {
            "contract": wrong_contract,
            "feature": missing_feature,
            "runtime": mismatched_runtime,
        },
    )

    for result in (
        native_result,
        spoofed_native,
        generic_result,
        hermes_result,
        custom_result,
        stale,
        wrong_contract,
        missing_feature,
        mismatched_runtime,
    ):
        expect(
            result["automatic_selection_allowed"] is False
            and result["provider_switch_allowed"] is False
            and result["read_only_dispatch_allowed"] is False
            and result["side_effect_dispatch_allowed"] is False
            and result["dispatch_authority"] == "none"
            and result["policy_effect"] == "none",
            "certification never selects, switches, or authorizes a provider",
            result,
        )

    print("provider certification smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
