from __future__ import annotations

from pathlib import Path
from typing import Any

from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from probes.time_probe import TimeProbe
from probes.web_probe import WebProbe


class StateRefresh:
    """Refreshes stale belief claims through known read-only probes."""

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None, *, model_assist_enabled: bool = True) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        self.probes = {
            "git_probe": GitProbe(),
            "hermes_probe": HermesProbe(),
            "mcp_probe": McpProbe(),
            "network_probe": NetworkProbe(),
            "openclaw_probe": OpenClawProbe(),
            "port_probe": PortProbe(),
            "process_probe": ProcessProbe(),
            "system_probe": SystemProbe(),
            "time_probe": TimeProbe(),
            "web_probe": WebProbe(),
        }

    def refresh_stale(self, limit: int = 20) -> dict[str, Any]:
        claims = self.state_store.read_json("belief_state.json").get("claims", [])
        stale = [
            claim
            for claim in claims
            if isinstance(claim, dict) and claim.get("status") in {"stale", "expired", "conflict"} and claim.get("next_action") == "refresh_probe"
        ]
        limit = max(1, int(limit))
        supported = [claim for claim in stale if str(claim.get("source") or "") in self.probes]
        unsupported = [claim for claim in stale if str(claim.get("source") or "") not in self.probes]
        selected, cursor_before, cursor_after = self._select_fair_batch(supported, limit=limit)
        refreshed: list[dict[str, Any]] = []
        for claim in selected:
            source = str(claim.get("source") or "")
            probe = self.probes.get(source)
            if probe is None:
                continue
            target = self._target_for_claim(claim)
            raw = probe.run(target)
            patch = self.perception.interpret_probe_result(raw)
            refreshed.append({"claim": claim.get("key") or claim.get("claim"), "probe_result": raw, "state_patch": patch})
        skipped = [
            {
                "claim": claim.get("key") or claim.get("claim"),
                "source": claim.get("source"),
                "reason": f"no probe for source {claim.get('source')}",
            }
            for claim in unsupported[:limit]
        ]
        def record_refresh_batch(state: dict[str, Any]) -> None:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "supported_count": len(supported),
                    "unsupported_count": len(unsupported),
                    "last_selected": [
                        str(claim.get("key") or claim.get("claim") or "")
                        for claim in selected
                    ],
                }
            )

        self.state_store.mutate_json("state_refresh_state.json", record_refresh_batch)
        return {
            "status": "success",
            "refreshed": refreshed,
            "skipped": skipped,
            "remaining_stale": max(0, len(supported) - len(refreshed)),
            "unsupported_stale": len(unsupported),
            "cursor": {"before": cursor_before, "after": cursor_after},
        }

    def _select_fair_batch(self, claims: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], int, int]:
        if not claims:
            self.state_store.patch_json(
                "state_refresh_state.json",
                {"cursor": 0, "updated_at": utc_now_iso()},
            )
            return [], 0, 0
        ordered = sorted(
            claims,
            key=lambda claim: (
                str(claim.get("updated_at") or claim.get("observed_at") or ""),
                str(claim.get("key") or claim.get("claim") or ""),
            ),
        )
        count = min(limit, len(ordered))
        selected: list[dict[str, Any]] = []
        cursor_before = 0
        cursor_after = 0

        def reserve_batch(state: dict[str, Any]) -> None:
            nonlocal cursor_before, cursor_after
            try:
                cursor_before = int(state.get("cursor") or 0) % len(ordered)
            except (TypeError, ValueError):
                cursor_before = 0
            selected.extend(
                ordered[(cursor_before + offset) % len(ordered)]
                for offset in range(count)
            )
            cursor_after = (cursor_before + count) % len(ordered)
            state["cursor"] = cursor_after
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("state_refresh_state.json", reserve_batch)
        return selected, cursor_before, cursor_after

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
