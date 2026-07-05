from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from core.action_risk import assess_text_risk


class LifecycleStatus(str, Enum):
    ONLINE = "online"
    IDLE = "idle"
    MONITORING = "monitoring"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING_CONFIRMATION = "waiting_confirmation"
    BLOCKED = "blocked"
    RECOVERING = "recovering"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class OperationalMode(str, Enum):
    MINIMALIST = "Minimalist"
    RESEARCHER = "Researcher"
    ENGINEER = "Engineer"
    OPERATOR = "Operator"
    GUARDIAN = "Guardian"
    PLANNER = "Planner"
    TEACHER = "Teacher"
    STEWARD = "Steward"


class RiskLevel(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"


class GuardianDecision(str, Enum):
    ALLOW = "allow"
    ALLOW_WITH_CONSTRAINTS = "allow_with_constraints"
    ASK_USER = "ask_user"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    level: RiskLevel
    title: str
    description: str
    default_decision: GuardianDecision
    requires_confirmation: bool
    requires_snapshot: bool
    examples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["level"] = self.level.value
        data["default_decision"] = self.default_decision.value
        data["examples"] = list(self.examples)
        return data


RISK_POLICIES: dict[RiskLevel, RiskPolicy] = {
    RiskLevel.R0: RiskPolicy(
        level=RiskLevel.R0,
        title="answer_only",
        description="No tool execution and no side effect.",
        default_decision=GuardianDecision.ALLOW,
        requires_confirmation=False,
        requires_snapshot=False,
        examples=("direct explanation", "cached state summary"),
    ),
    RiskLevel.R1: RiskPolicy(
        level=RiskLevel.R1,
        title="read_only",
        description="Read-only probes or state inspection with repeatable low cost.",
        default_decision=GuardianDecision.ALLOW,
        requires_confirmation=False,
        requires_snapshot=False,
        examples=("port check", "git status", "process list", "log read"),
    ),
    RiskLevel.R2: RiskPolicy(
        level=RiskLevel.R2,
        title="low_risk_write",
        description="Small, scoped writes with clear rollback or reviewable diff.",
        default_decision=GuardianDecision.ALLOW_WITH_CONSTRAINTS,
        requires_confirmation=False,
        requires_snapshot=True,
        examples=("create a new file", "small code edit", "local git commit"),
    ),
    RiskLevel.R3: RiskPolicy(
        level=RiskLevel.R3,
        title="medium_change",
        description="Configuration, broad file, or stateful changes that need snapshot and review.",
        default_decision=GuardianDecision.ASK_USER,
        requires_confirmation=True,
        requires_snapshot=True,
        examples=("modify config", "overwrite files", "database migration"),
    ),
    RiskLevel.R4: RiskPolicy(
        level=RiskLevel.R4,
        title="high_impact",
        description="Service, deployment, permission, external-cost, or hard-to-revert action.",
        default_decision=GuardianDecision.ASK_USER,
        requires_confirmation=True,
        requires_snapshot=True,
        examples=("restart service", "deploy", "sudo command", "delete file"),
    ),
    RiskLevel.R5: RiskPolicy(
        level=RiskLevel.R5,
        title="forbidden_or_manual_takeover",
        description="Destructive, secret-exfiltrating, or policy-bypassing action.",
        default_decision=GuardianDecision.BLOCK,
        requires_confirmation=True,
        requires_snapshot=True,
        examples=("rm -rf", "curl pipe bash", "drop database", "git push --force", "externalize secrets"),
    ),
}


def risk_policy(level: RiskLevel | str) -> RiskPolicy:
    return RISK_POLICIES[normalize_risk(level)]


def risk_catalog() -> list[dict[str, Any]]:
    return [RISK_POLICIES[level].to_dict() for level in RiskLevel]


def normalize_risk(level: RiskLevel | str) -> RiskLevel:
    if isinstance(level, RiskLevel):
        return level
    return RiskLevel(str(level))


def classify_text_risk(text: str) -> RiskLevel:
    return RiskLevel(assess_text_risk(text).risk_level)


def lifecycle_statuses() -> list[str]:
    return [status.value for status in LifecycleStatus]


def operational_modes() -> list[str]:
    return [mode.value for mode in OperationalMode]
