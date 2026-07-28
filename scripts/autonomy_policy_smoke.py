#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.autonomy_policy import AutonomyLevel  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from runtime.self_heal_playbook import (  # noqa: E402
    OPENCLAW_RECONNECT_PROFILE,
)


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def allowed(profile=OPENCLAW_RECONNECT_PROFILE, **overrides: object) -> bool:
    values = {
        "capability": "agent.reconnect",
        "risk_level": RiskLevel.R1,
        "user_scope": "local-user",
        "environment": "local",
        "target_scope": "selected_agent:openclaw",
        "mode": "scoped_canary",
        "attempt_count": 0,
        "cooldown_elapsed": True,
        "now": datetime(2026, 7, 28, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return profile.permits(**values)


def main() -> None:
    expect(allowed(), "exact A2 runtime-health envelope permits fresh handshake")
    expect(
        not allowed(replace(OPENCLAW_RECONNECT_PROFILE, level=AutonomyLevel.A0)),
        "A0 cannot inherit reconnect from a capability list",
    )
    expect(
        not allowed(replace(OPENCLAW_RECONNECT_PROFILE, level=AutonomyLevel.A1)),
        "A1 cannot execute an A2 transport reconnect",
    )
    expect(
        allowed(
            replace(
                OPENCLAW_RECONNECT_PROFILE,
                level=AutonomyLevel.A1,
            ),
            capability="agent.status.read",
            mode="shadow",
        ),
        "A1 can perform the fixed shadow status observation",
    )
    expect(
        allowed(
            replace(
                OPENCLAW_RECONNECT_PROFILE,
                level=AutonomyLevel.A1,
            ),
            capability="agent.capabilities.refresh",
            mode="shadow",
        ),
        "A1 can refresh the fixed shadow capability observation",
    )
    expect(not allowed(user_scope="user-b"), "cross-user scope denied")
    expect(not allowed(environment="production"), "environment drift denied")
    expect(
        not allowed(target_scope="selected_agent:hermes"),
        "provider/target drift denied",
    )
    expect(not allowed(mode="shadow"), "shadow never receives action authority")
    expect(not allowed(attempt_count=2), "attempt budget exhausted")
    expect(not allowed(cooldown_elapsed=False), "cooldown cannot be bypassed")
    expect(
        not allowed(risk_level=RiskLevel.R2),
        "risk above the fixed R1 ceiling denied",
    )
    expect(
        not allowed(risk_level=RiskLevel.R5),
        "R5 permanently denied",
    )
    expect(
        not allowed(
            replace(OPENCLAW_RECONNECT_PROFILE, enabled=False)
        ),
        "disabled profile denied",
    )
    expect(
        not allowed(
            replace(OPENCLAW_RECONNECT_PROFILE, revoked=True)
        ),
        "revoked profile denied",
    )
    expect(
        not allowed(
            replace(OPENCLAW_RECONNECT_PROFILE, policy_version=2)
        ),
        "unknown policy revision fails closed",
    )
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    expect(
        not allowed(
            replace(
                OPENCLAW_RECONNECT_PROFILE,
                valid_from=(now + timedelta(seconds=1)).isoformat(),
            ),
            now=now,
        ),
        "not-yet-valid profile denied",
    )
    expect(
        not allowed(
            replace(
                OPENCLAW_RECONNECT_PROFILE,
                valid_until=now.isoformat(),
            ),
            now=now,
        ),
        "expired profile denied",
    )
    expect(
        not allowed(
            replace(
                OPENCLAW_RECONNECT_PROFILE,
                valid_until="not-a-time",
            ),
            now=now,
        ),
        "invalid time policy fails closed",
    )
    public = OPENCLAW_RECONNECT_PROFILE.to_public_dict()
    expect(
        public["global_authority"] is False
        and public["level"] == "A2"
        and public["allowed_modes"] == ["shadow", "scoped_canary"],
        "profile is explicitly domain-scoped and non-global",
        public,
    )
    print("autonomy_policy_smoke: ok")


if __name__ == "__main__":
    main()
