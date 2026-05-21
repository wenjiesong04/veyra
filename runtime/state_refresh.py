from __future__ import annotations

from pathlib import Path
from typing import Any

from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from probes.web_probe import WebProbe


class StateRefresh:
    """Refreshes stale belief claims through known read-only probes."""

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning)
        self.probes = {
            "git_probe": GitProbe(),
            "hermes_probe": HermesProbe(),
            "mcp_probe": McpProbe(),
            "network_probe": NetworkProbe(),
            "openclaw_probe": OpenClawProbe(),
            "port_probe": PortProbe(),
            "process_probe": ProcessProbe(),
            "system_probe": SystemProbe(),
            "web_probe": WebProbe(),
        }

    def refresh_stale(self, limit: int = 20) -> dict[str, Any]:
        claims = self.state_store.read_json("belief_state.json").get("claims", [])
        stale = [claim for claim in claims if isinstance(claim, dict) and claim.get("status") == "stale" and claim.get("next_action") == "refresh_probe"]
        refreshed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for claim in stale[:limit]:
            source = str(claim.get("source") or "")
            probe = self.probes.get(source)
            if not probe:
                skipped.append({"claim": claim.get("key") or claim.get("claim"), "reason": f"no probe for source {source}"})
                continue
            target = self._target_for_claim(claim)
            raw = probe.run(target)
            patch = self.perception.interpret_probe_result(raw)
            refreshed.append({"claim": claim.get("key") or claim.get("claim"), "probe_result": raw, "state_patch": patch})
        return {"status": "success", "refreshed": refreshed, "skipped": skipped, "remaining_stale": max(0, len(stale) - len(refreshed))}

    def _target_for_claim(self, claim: dict[str, Any]) -> str:
        evidence = claim.get("evidence") if isinstance(claim.get("evidence"), dict) else {}
        for key in ("target", "url", "host", "path"):
            if evidence.get(key):
                return str(evidence[key])
        details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
        for key in ("target", "url", "host", "path", "port"):
            if details.get(key):
                value = str(details[key])
                if key == "path" and not Path(value).exists():
                    return ""
                return value
        return str(claim.get("claim") or "")
