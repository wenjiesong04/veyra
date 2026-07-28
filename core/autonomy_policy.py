from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from core.definitions import RiskLevel


class AutonomyLevel(StrEnum):
    """Domain-scoped autonomy levels; never a process-wide authority switch."""

    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"


_RISK_ORDER = {
    RiskLevel.R0.value: 0,
    RiskLevel.R1.value: 1,
    RiskLevel.R2.value: 2,
    RiskLevel.R3.value: 3,
    RiskLevel.R4.value: 4,
    RiskLevel.R5.value: 5,
}
_LEVEL_ORDER = {
    AutonomyLevel.A0: 0,
    AutonomyLevel.A1: 1,
    AutonomyLevel.A2: 2,
    AutonomyLevel.A3: 3,
    AutonomyLevel.A4: 4,
    AutonomyLevel.A5: 5,
}
_CAPABILITY_MINIMUM_LEVEL = {
    "agent.status.read": AutonomyLevel.A1,
    "agent.capabilities.refresh": AutonomyLevel.A1,
    # The first playbook only performs a fresh authenticated transport
    # handshake. It does not restart a process or switch a provider.
    "agent.reconnect": AutonomyLevel.A2,
}
_CAPABILITY_ALLOWED_MODES = {
    "agent.status.read": {"shadow", "scoped_canary"},
    "agent.capabilities.refresh": {"shadow", "scoped_canary"},
    "agent.reconnect": {"scoped_canary"},
}


@dataclass(frozen=True, slots=True)
class DomainAutonomyProfile:
    """An immutable authority envelope for one capability domain.

    A profile is descriptive and restrictive: callers still need the concrete
    capability implementation and its normal governance checks. Neither a model
    response nor an Agent proposal can modify this object at runtime.
    """

    profile_id: str
    policy_version: int
    level: AutonomyLevel
    enabled: bool
    revoked: bool
    user_scope: str
    domain: str
    environment: str
    capabilities: tuple[str, ...]
    target_scope: str
    risk_ceiling: RiskLevel
    max_attempts: int
    cooldown_seconds: int
    allowed_modes: tuple[str, ...]
    valid_from: str | None = None
    valid_until: str | None = None

    def permits(
        self,
        *,
        capability: str,
        risk_level: RiskLevel | str,
        user_scope: str,
        environment: str,
        target_scope: str,
        mode: str,
        attempt_count: int,
        cooldown_elapsed: bool,
        now: datetime | None = None,
    ) -> bool:
        risk = (
            risk_level.value
            if isinstance(risk_level, RiskLevel)
            else str(risk_level)
        )
        minimum_level = _CAPABILITY_MINIMUM_LEVEL.get(capability)
        capability_modes = _CAPABILITY_ALLOWED_MODES.get(capability, set())
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        valid_from = self._parse_time(self.valid_from)
        valid_until = self._parse_time(self.valid_until)
        level_order = _LEVEL_ORDER.get(self.level)
        minimum_order = _LEVEL_ORDER.get(minimum_level)
        ceiling_order = _RISK_ORDER.get(
            self.risk_ceiling.value
            if isinstance(self.risk_ceiling, RiskLevel)
            else str(self.risk_ceiling)
        )
        if (
            (self.valid_from and valid_from is None)
            or (self.valid_until and valid_until is None)
        ):
            return False
        return bool(
            self.policy_version == 1
            and self.enabled
            and not self.revoked
            and minimum_level is not None
            and level_order is not None
            and minimum_order is not None
            and level_order >= minimum_order
            and capability in self.capabilities
            and user_scope == self.user_scope
            and environment == self.environment
            and target_scope == self.target_scope
            and mode in self.allowed_modes
            and mode in capability_modes
            and type(attempt_count) is int
            and attempt_count >= 0
            and (
                capability != "agent.reconnect"
                or (
                    attempt_count < self.max_attempts
                    and cooldown_elapsed
                )
            )
            and (valid_from is None or current >= valid_from)
            and (valid_until is None or current < valid_until)
            and risk != RiskLevel.R5.value
            and _RISK_ORDER.get(risk, 99)
            <= (ceiling_order if ceiling_order is not None else -1)
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "policy_version": self.policy_version,
            "level": self.level.value,
            "enabled": self.enabled,
            "revoked": self.revoked,
            "user_scope": self.user_scope,
            "domain": self.domain,
            "environment": self.environment,
            "capabilities": list(self.capabilities),
            "target_scope": self.target_scope,
            "risk_ceiling": self.risk_ceiling.value,
            "max_attempts": self.max_attempts,
            "cooldown_seconds": self.cooldown_seconds,
            "allowed_modes": list(self.allowed_modes),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "global_authority": False,
        }

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
