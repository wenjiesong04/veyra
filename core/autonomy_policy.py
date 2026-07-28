from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

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
    # A3 is confined to a Veyra-owned disposable sandbox. These capabilities
    # do not authorize a source/workspace read, shell, Agent, network call, or
    # promotion into a real environment.
    "sandbox.json_candidate.stage": AutonomyLevel.A3,
    "sandbox.json_candidate.verify": AutonomyLevel.A3,
}
_CAPABILITY_ALLOWED_MODES = {
    "agent.status.read": {"shadow", "scoped_canary"},
    "agent.capabilities.refresh": {"shadow", "scoped_canary"},
    "agent.reconnect": {"scoped_canary"},
    "sandbox.json_candidate.stage": {"scoped_canary"},
    "sandbox.json_candidate.verify": {"scoped_canary"},
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


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    """Structured result from the immutable domain policy selector.

    ``requested_level`` is diagnostic only. A caller, model, or Agent cannot
    raise the selected profile by asking for A4/A5.
    """

    outcome: str
    reason_code: str
    profile_id: str | None
    domain: str
    capability: str
    effective_level: AutonomyLevel
    requested_level: str | None
    certified: bool

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "profile_id": self.profile_id,
            "domain": self.domain,
            "capability": self.capability,
            "effective_level": self.effective_level.value,
            "requested_level": self.requested_level,
            "certified": self.certified,
            "global_authority": False,
        }


class AutonomyPolicyRegistry:
    """Immutable exact-profile selector for domain-scoped authority.

    Profiles are supplied by trusted application assembly. There is
    intentionally no runtime registration or mutation method.
    """

    def __init__(
        self,
        profiles: Iterable[DomainAutonomyProfile],
    ) -> None:
        selected: dict[str, DomainAutonomyProfile] = {}
        for profile in profiles:
            if not isinstance(profile, DomainAutonomyProfile):
                raise TypeError("autonomy profiles must be DomainAutonomyProfile")
            if not profile.profile_id or profile.profile_id != profile.profile_id.strip():
                raise ValueError("autonomy profile_id must be normalized")
            if profile.profile_id in selected:
                raise ValueError(
                    f"duplicate autonomy profile_id: {profile.profile_id}"
                )
            if profile.level in {AutonomyLevel.A4, AutonomyLevel.A5}:
                raise ValueError(
                    "A4/A5 profiles require a future certified authority loader"
                )
            selected[profile.profile_id] = profile
        self._profiles: Mapping[str, DomainAutonomyProfile] = (
            MappingProxyType(selected)
        )

    def profile(self, profile_id: str) -> DomainAutonomyProfile | None:
        return self._profiles.get(str(profile_id or ""))

    def decide(
        self,
        *,
        profile_id: str,
        domain: str,
        capability: str,
        risk_level: RiskLevel | str,
        user_scope: str,
        environment: str,
        target_scope: str,
        mode: str,
        attempt_count: int,
        cooldown_elapsed: bool,
        requested_level: AutonomyLevel | str | None = None,
        now: datetime | None = None,
    ) -> AutonomyDecision:
        requested = (
            requested_level.value
            if isinstance(requested_level, AutonomyLevel)
            else str(requested_level)
            if requested_level is not None
            else None
        )
        if requested in {AutonomyLevel.A4.value, AutonomyLevel.A5.value}:
            return self._decision(
                outcome="not_certified",
                reason_code="requested_level_not_certified",
                profile=None,
                domain=domain,
                capability=capability,
                requested_level=requested,
            )
        profile = self.profile(profile_id)
        if profile is None:
            return self._decision(
                outcome="denied",
                reason_code="unknown_profile",
                profile=None,
                domain=domain,
                capability=capability,
                requested_level=requested,
            )
        if domain != profile.domain:
            return self._decision(
                outcome="denied",
                reason_code="domain_mismatch",
                profile=profile,
                domain=domain,
                capability=capability,
                requested_level=requested,
            )
        allowed = profile.permits(
            capability=capability,
            risk_level=risk_level,
            user_scope=user_scope,
            environment=environment,
            target_scope=target_scope,
            mode=mode,
            attempt_count=attempt_count,
            cooldown_elapsed=cooldown_elapsed,
            now=now,
        )
        return self._decision(
            outcome="allowed" if allowed else "denied",
            reason_code="profile_permitted" if allowed else "profile_denied",
            profile=profile,
            domain=domain,
            capability=capability,
            requested_level=requested,
        )

    def public_status(self) -> dict[str, Any]:
        return {
            "scope": "domain_scoped",
            "global_level": None,
            "profiles": [
                profile.to_public_dict()
                for _, profile in sorted(self._profiles.items())
            ],
            "certification": {
                AutonomyLevel.A4.value: "not_certified",
                AutonomyLevel.A5.value: "not_certified",
            },
        }

    @staticmethod
    def _decision(
        *,
        outcome: str,
        reason_code: str,
        profile: DomainAutonomyProfile | None,
        domain: str,
        capability: str,
        requested_level: str | None,
    ) -> AutonomyDecision:
        return AutonomyDecision(
            outcome=outcome,
            reason_code=reason_code,
            profile_id=profile.profile_id if profile else None,
            domain=str(domain or ""),
            capability=str(capability or ""),
            effective_level=(
                profile.level
                if outcome == "allowed" and profile is not None
                else AutonomyLevel.A0
            ),
            requested_level=requested_level,
            certified=False,
        )


JSON_SANDBOX_REPAIR_PROFILE = DomainAutonomyProfile(
    profile_id="aut.sandbox_repair.json_candidate.v1",
    policy_version=1,
    level=AutonomyLevel.A3,
    enabled=True,
    revoked=False,
    user_scope="local-user",
    domain="sandbox_repair",
    environment="local",
    capabilities=(
        "sandbox.json_candidate.stage",
        "sandbox.json_candidate.verify",
    ),
    target_scope="veyra_private:sandbox_repair_json",
    risk_ceiling=RiskLevel.R2,
    max_attempts=1,
    cooldown_seconds=0,
    allowed_modes=("shadow", "scoped_canary"),
)
