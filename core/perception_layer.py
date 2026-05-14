from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore


class PerceptionLayer:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def interpret_probe_result(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        probe_name = probe_result.get("probe", "unknown")
        local_world = self.state_store.read_json("local_world.json")
        probes = local_world.setdefault("probes", {})
        probes[probe_name] = probe_result
        self.state_store.write_json("local_world.json", local_world)

        if probe_name == "port_probe":
            claim = {
                "claim": f"port {probe_result.get('port')} is {probe_result.get('status')}",
                "confidence": 0.9,
                "source": probe_name,
                "ttl_seconds": 60,
                "status": "fresh",
            }
            belief = self.state_store.read_json("belief_state.json")
            belief.setdefault("claims", []).append(claim)
            self.state_store.write_json("belief_state.json", belief)
        return {"local_world.probes": {probe_name: probe_result}}
