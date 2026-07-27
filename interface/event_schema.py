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
    OBSERVATION = "observation"
    STATE_CHANGED = "state_changed"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    COMMITMENT_DUE = "commitment_due"
    COMPONENT_DEGRADED = "component_degraded"
    USER_FEEDBACK = "user_feedback"


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
    correlation_id: str | None = None
    causation_id: str | None = None
    subject: Any = None
    evidence_refs: list[Any] = field(default_factory=list)
    dedupe_key: str | None = None
    occurred_at: str | None = None
    received_at: str | None = None
    privacy_scope: Any = "user"

    def __post_init__(self) -> None:
        if not isinstance(self.type, EventType):
            self.type = EventType(str(self.type))
        if not isinstance(self.source, EventSource):
            if not isinstance(self.source, dict):
                raise TypeError("event source must be EventSource or a mapping")
            self.source = EventSource(
                channel=str(self.source.get("channel") or ""),
                user_id=str(self.source.get("user_id") or ""),
                session_id=str(self.source.get("session_id") or ""),
            )
        if not isinstance(self.payload, dict):
            raise TypeError("event payload must be a mapping")
        self.event_id = str(self.event_id or f"evt_{uuid4().hex[:12]}")
        self.timestamp = str(self.timestamp or self.occurred_at or utc_now_iso())
        self.occurred_at = str(self.occurred_at or self.timestamp)
        self.received_at = str(self.received_at or utc_now_iso())
        self.correlation_id = str(self.correlation_id or self.event_id)
        self.causation_id = str(self.causation_id) if self.causation_id else None
        self.dedupe_key = str(self.dedupe_key) if self.dedupe_key else None
        self.evidence_refs = list(self.evidence_refs or [])

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VeyraEvent:
        """Restore both legacy and current event envelopes."""
        if not isinstance(raw, dict):
            raise TypeError("event envelope must be a mapping")
        source = raw.get("source")
        if isinstance(source, EventSource):
            normalized_source = source
        elif isinstance(source, dict):
            normalized_source = EventSource(
                channel=str(source.get("channel") or ""),
                user_id=str(source.get("user_id") or ""),
                session_id=str(source.get("session_id") or ""),
            )
        else:
            raise TypeError("event envelope source must be a mapping")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("event envelope payload must be a mapping")
        occurred_at = str(raw.get("occurred_at") or raw.get("timestamp") or utc_now_iso())
        raw_type = raw.get("type")
        normalized_type = raw_type if isinstance(raw_type, EventType) else EventType(str(raw_type or ""))
        return cls(
            type=normalized_type,
            source=normalized_source,
            payload=dict(payload),
            event_id=str(raw.get("event_id") or f"evt_{uuid4().hex[:12]}"),
            timestamp=str(raw.get("timestamp") or occurred_at),
            correlation_id=str(raw.get("correlation_id") or raw.get("event_id") or "") or None,
            causation_id=str(raw.get("causation_id") or "") or None,
            subject=raw.get("subject"),
            evidence_refs=list(raw.get("evidence_refs") or []),
            dedupe_key=str(raw.get("dedupe_key") or "") or None,
            occurred_at=occurred_at,
            received_at=str(raw.get("received_at") or utc_now_iso()),
            privacy_scope=raw.get("privacy_scope", "user"),
        )


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
    followup_messages: list[str] = field(default_factory=list)

    @property
    def primary_response(self) -> str:
        return self.response

    @primary_response.setter
    def primary_response(self, value: str) -> None:
        self.response = value

    def ordered_messages(self) -> list[dict[str, Any]]:
        messages = [{"message_type": "primary", "index": 0, "message": self.response}]
        for index, message in enumerate(self.followup_messages, start=1):
            text = str(message or "").strip()
            if text:
                messages.append({"message_type": "followup", "index": index, "message": text})
        return messages

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route"] = self.route.value
        data["risk_level"] = self.risk_level.value
        data["primary_response"] = self.response
        data["messages"] = self.ordered_messages()
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
    user_goal: str = ""
    required_capabilities: list[str] = field(default_factory=list)
    verification_policy: dict[str, Any] = field(default_factory=dict)
    rollback_requirement: dict[str, Any] = field(default_factory=dict)
    memory_policy: str = "forget"
    # Agent execution session is isolated per task by default so independent user
    # requests never share one polluted agent conversation window. session_id stays
    # the Veyra-side dialogue/user session and must NOT be used as the agent runtime key.
    agent_execution_session_id: str = ""
    agent_session_policy: str = "ephemeral_per_task"
    # Optional provider-neutral Phase 4 envelope. It is public to the selected
    # Agent and must already be bound to this packet by the Durable Case runtime.
    dialogue_message: dict[str, Any] | None = None
    # The runtime run identifier is server-owned coordination state. Unlike the
    # dialogue envelope it must never be serialized into an Agent prompt or a
    # public LoopResult.
    runtime_run_id: str = ""
    # Server-derived identity used only to register a governed Agent dispatch.
    # It is deliberately excluded from to_dict(): neither the Agent prompt nor
    # the public LoopResult may receive this private registration context.
    governance_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("governance_context", None)
        data.pop("runtime_run_id", None)
        if data.get("dialogue_message") is None:
            data.pop("dialogue_message", None)
        return data
