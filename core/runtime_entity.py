from __future__ import annotations

from dataclasses import dataclass, field

from core.definitions import LifecycleStatus, OperationalMode
from core.lifecycle import Lifecycle
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


@dataclass(slots=True)
class VeyraIdentity:
    name: str = "Veyra"
    full_name: str = "Virtual Entity for Yielding Real-time Awareness"
    selected_agent: str = "openclaw"


@dataclass(slots=True)
class LifecycleState:
    status: str = "online"
    started_at: str = field(default_factory=utc_now_iso)
    last_heartbeat_at: str = field(default_factory=utc_now_iso)


class RuntimeEntity:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        config = state_store.read_json("agent_config.json")
        self.identity = VeyraIdentity(selected_agent=str(config.get("selected_agent") or "openclaw"))
        self.lifecycle = LifecycleState()
        self.lifecycle_validator = Lifecycle()
        self.operational_mode: list[str] = [OperationalMode.MINIMALIST.value]

    def set_selected_agent(self, selected_agent: str) -> None:
        self.identity.selected_agent = selected_agent
        config = self.state_store.read_json("agent_config.json")
        config["selected_agent"] = selected_agent
        self.state_store.write_json("agent_config.json", config)

    def set_status(self, status: str) -> None:
        validated = self.lifecycle_validator.validate(status)
        self.lifecycle.status = validated
        self.lifecycle.last_heartbeat_at = utc_now_iso()
        self.state_store.write_text(
            "heartbeat.md",
            f"# Veyra Heartbeat\n\nstatus: {validated}\nupdated_at: {self.lifecycle.last_heartbeat_at}\n",
        )

    def set_idle(self) -> None:
        self.set_status(LifecycleStatus.IDLE.value)
