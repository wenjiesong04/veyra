from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from core.definitions import RiskLevel


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    TOOL_RESULT = "tool_result"
    AGENT_RESULT = "agent_result"
    HEARTBEAT = "heartbeat"
    ACTION_PROPOSAL = "action_proposal"


class Route(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    PROBE = "probe"
    NATIVE_TOOL = "native_tool"
    SKILL = "skill"
    AGENT = "agent"
    ASK_USER = "ask_user"
    HUMAN_REVIEW = "human_review"
    BLOCK = "block"
    ROLLBACK = "rollback"


@dataclass(slots=True)
class EventSource:
    channel: str
    user_id: str
    session_id: str


@dataclass(slots=True)
class VeyraEvent:
    type: EventType
    source: EventSource
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: f"evt_{uuid4().hex[:12]}")
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return data


@dataclass(slots=True)
class Decision:
    route: Route
    risk_level: RiskLevel
    reason: str
    requires_confirmation: bool = False
    selected_probe: str | None = None
    target_agent: str | None = None
    intent: str = "unknown"
    complexity: str = "unknown"
    capability: str = "unknown"
    freshness_required: bool = False
    needs_probe: bool = False
    needs_agent: bool = False
    needs_user_confirmation: bool = False
    memory_policy: str = "forget"
    reasoning_mode: str = "direct"
    required_capabilities: list[str] = field(default_factory=list)
    capability_request: dict[str, Any] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    model_assist: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route"] = self.route.value
        data["risk_level"] = self.risk_level.value
        return data


@dataclass(slots=True)
class LoopResult:
    event_id: str
    route: Route
    status: str
    response: str
    risk_level: RiskLevel
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route"] = self.route.value
        data["risk_level"] = self.risk_level.value
        return data


@dataclass(slots=True)
class VeyraTaskPacket:
    task_id: str
    target_agent: str
    session_id: str
    user_message: str
    context_patch: dict[str, Any]
    persona_patch: dict[str, Any]
    policy_patch: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
