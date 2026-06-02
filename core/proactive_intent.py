from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from interface.event_schema import utc_now_iso


PROACTIVE_INTENT_TYPES = {
    "goal_start",
    "track_external_topic",
    "daily_digest",
    "reminder",
    "monitor_local_state",
    "project_assistance",
    "learning_plan",
    "cancel_commitment",
    "pause_commitment",
    "resume_commitment",
    "update_preference",
    "unknown",
}

PROACTIVE_NEXT_ACTIONS = {
    "answer_only",
    "create_goal",
    "create_commitment_draft",
    "create_watchlist_draft",
    "ask_confirmation",
    "cancel_matching_commitments",
    "pause_matching_commitments",
    "resume_matching_commitments",
    "route_to_agent",
    "block",
}

AUTHORIZATION_STATES = {"none", "pending_confirmation", "granted", "denied", "revoked", "paused"}


def stable_intent_id() -> str:
    return f"pin_{uuid4().hex[:12]}"


@dataclass(slots=True)
class ProactiveIntent:
    intent_id: str = field(default_factory=stable_intent_id)
    user_id: str = "local-user"
    session_id: str = "local-session"
    channel_id: str = "api"
    raw_text: str = ""
    intent_type: str = "unknown"
    topic: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    desired_outcome: str = ""
    cadence: dict[str, Any] = field(default_factory=dict)
    trigger_condition: str = ""
    information_sources: list[str] = field(default_factory=list)
    local_context_needed: list[str] = field(default_factory=list)
    external_context_needed: list[str] = field(default_factory=list)
    memory_write_needed: bool = False
    requires_user_authorization: bool = True
    authorization_status: str = "none"
    risk_level: str = "R1"
    confidence: float = 0.0
    proposed_next_action: str = "ask_confirmation"
    source: str = "fallback"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["intent_type"] = self.intent_type if self.intent_type in PROACTIVE_INTENT_TYPES else "unknown"
        data["proposed_next_action"] = self.proposed_next_action if self.proposed_next_action in PROACTIVE_NEXT_ACTIONS else "ask_confirmation"
        data["authorization_status"] = self.authorization_status if self.authorization_status in AUTHORIZATION_STATES else "none"
        data["confidence"] = _bounded_float(self.confidence)
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProactiveIntent":
        data = dict(payload or {})
        data["intent_type"] = str(data.get("intent_type") or "unknown")
        data["proposed_next_action"] = str(data.get("proposed_next_action") or "ask_confirmation")
        data["authorization_status"] = str(data.get("authorization_status") or "none")
        data["confidence"] = _bounded_float(data.get("confidence"))
        for key in ("entities", "cadence"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        for key in ("information_sources", "local_context_needed", "external_context_needed"):
            if not isinstance(data.get(key), list):
                data[key] = []
        return cls(
            intent_id=str(data.get("intent_id") or stable_intent_id()),
            user_id=str(data.get("user_id") or "local-user"),
            session_id=str(data.get("session_id") or "local-session"),
            channel_id=str(data.get("channel_id") or "api"),
            raw_text=str(data.get("raw_text") or ""),
            intent_type=data["intent_type"] if data["intent_type"] in PROACTIVE_INTENT_TYPES else "unknown",
            topic=str(data.get("topic") or ""),
            entities=data["entities"],
            desired_outcome=str(data.get("desired_outcome") or ""),
            cadence=data["cadence"],
            trigger_condition=str(data.get("trigger_condition") or ""),
            information_sources=[str(item) for item in data["information_sources"][:8]],
            local_context_needed=[str(item) for item in data["local_context_needed"][:8]],
            external_context_needed=[str(item) for item in data["external_context_needed"][:8]],
            memory_write_needed=bool(data.get("memory_write_needed")),
            requires_user_authorization=bool(data.get("requires_user_authorization", True)),
            authorization_status=data["authorization_status"] if data["authorization_status"] in AUTHORIZATION_STATES else "none",
            risk_level=str(data.get("risk_level") or "R1"),
            confidence=data["confidence"],
            proposed_next_action=data["proposed_next_action"] if data["proposed_next_action"] in PROACTIVE_NEXT_ACTIONS else "ask_confirmation",
            source=str(data.get("source") or "fallback"),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


@dataclass(slots=True)
class GoalDraft:
    goal_id: str = field(default_factory=lambda: f"goal_{uuid4().hex[:12]}")
    title: str = ""
    description: str = ""
    category: str = "general"
    current_stage: str = "draft"
    success_criteria: list[str] = field(default_factory=list)
    user_profile_assumptions: list[str] = field(default_factory=list)
    required_context: list[str] = field(default_factory=list)
    proactive_allowed: str = "pending_confirmation"
    created_from_intent_id: str = ""
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommitmentDraft:
    type: str = "generic_reminder"
    topic: str = ""
    cadence: dict[str, Any] = field(default_factory=dict)
    delivery_channel: str = "api"
    requires_confirmation: bool = True
    status: str = "pending_confirmation"
    source_intent_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WatchlistDraft:
    topic: str = ""
    query: str = ""
    sources: list[str] = field(default_factory=list)
    refresh_policy: dict[str, Any] = field(default_factory=dict)
    ranking_policy: dict[str, Any] = field(default_factory=dict)
    dedupe_policy: dict[str, Any] = field(default_factory=dict)
    ttl: int = 1800
    status: str = "pending_confirmation"
    source_intent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bounded_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default
