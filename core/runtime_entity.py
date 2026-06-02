from __future__ import annotations

from dataclasses import dataclass, field

from core.definitions import LifecycleStatus, OperationalMode
from core.lifecycle import Lifecycle
from core.model_client import _env_value, redact_sensitive
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

    def core_model_runtime_state(self) -> dict[str, object]:
        config = self.state_store.read_json("agent_config.json")
        core_model = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
        public = redact_sensitive(core_model)
        api_key_env = str(core_model.get("api_key_env") or "VEYRA_CORE_MODEL_API_KEY")
        return {
            "enabled": bool(core_model.get("enabled")),
            "provider": public.get("provider", "openai_compatible"),
            "base_url": public.get("base_url", ""),
            "model": public.get("model", ""),
            "decision_mode": public.get("decision_mode", "auto"),
            "api_key_env": public.get("api_key_env", "VEYRA_CORE_MODEL_API_KEY"),
            "api_key_set": bool(core_model.get("api_key") or _env_value(api_key_env, "")),
        }
