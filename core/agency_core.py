from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class AgencyCore:
    """Turns state gaps into bounded intentions.

    Agency stays conservative: it may execute R1 refresh work through callers that
    already run read-only probes, but R2+ intentions are suggestions or reviews.
    """

    def __init__(self, state_store: WorldStateStore | None = None, agency_root: str | Path = "agency") -> None:
        self.state_store = state_store
        self.agency_root = Path(agency_root)
        self.intention_path = self.agency_root / "intention_queue.json"
        self.goals_path = self.agency_root / "goals.json"
        self.triggers_path = self.agency_root / "triggers.yaml"
        self.agency_root.mkdir(parents=True, exist_ok=True)
        if not self.intention_path.exists():
            self.intention_path.write_text("[]", encoding="utf-8")

    def state(self) -> dict[str, Any]:
        return {
            "goals": self._read_json(self.goals_path, {}),
            "triggers": self._read_triggers(),
            "intentions": self.read_intentions(),
        }

    def detect_state_gap(self, goals: dict[str, Any] | None = None, world_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        goals = goals or self._read_json(self.goals_path, {})
        world_state = world_state or (self.state_store.read_all() if self.state_store else {})
        gaps: list[dict[str, Any]] = []

        executor = world_state.get("executor_state", {}) if isinstance(world_state.get("executor_state"), dict) else {}
        executor_status = str(executor.get("status") or "unknown")
        if goals.get("selected_agent_must_be_available") and executor_status not in {"available", "ok", "success"}:
            gaps.append(
                {
                    "gap_id": "selected_agent_unavailable",
                    "target": "selected_agent",
                    "observed_status": executor_status,
                    "risk_level": RiskLevel.R1.value,
                    "suggested_action": "probe_executor_status",
                    "action_text": "refresh selected agent runtime status with read-only probes",
                }
            )

        belief = world_state.get("belief_state", {}) if isinstance(world_state.get("belief_state"), dict) else {}
        for claim in belief.get("claims", []) if isinstance(belief.get("claims"), list) else []:
            if not isinstance(claim, dict):
                continue
            if claim.get("status") == "stale" and claim.get("next_action") == "refresh_probe":
                gaps.append(
                    {
                        "gap_id": f"stale_claim:{claim.get('key') or claim.get('claim')}",
                        "target": claim.get("key") or claim.get("source") or "belief",
                        "observed_status": "stale",
                        "risk_level": RiskLevel.R1.value,
                        "suggested_action": "refresh_probe",
                        "action_text": f"refresh stale belief claim from {claim.get('source', 'unknown')}",
                    }
                )

        local_world = world_state.get("local_world", {}) if isinstance(world_state.get("local_world"), dict) else {}
        git_probe = ((local_world.get("probes") or {}).get("git_probe") if isinstance(local_world.get("probes"), dict) else {}) or {}
        if isinstance(git_probe, dict) and git_probe.get("dirty"):
            gaps.append(
                {
                    "gap_id": "git_workspace_dirty",
                    "target": "git_workspace",
                    "observed_status": "dirty",
                    "risk_level": RiskLevel.R2.value,
                    "suggested_action": "suggest_diff_review",
                    "action_text": "suggest reviewing git diff before risky operations",
                }
            )

        return gaps

    def sync_intentions(self, world_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        goals = self._read_json(self.goals_path, {})
        gaps = self.detect_state_gap(goals=goals, world_state=world_state)
        existing = self.read_intentions()
        by_gap = {str(item.get("source_gap", {}).get("gap_id")): item for item in existing if item.get("status") not in {"done", "blocked", "dismissed"}}
        for gap in gaps:
            key = str(gap.get("gap_id"))
            if key in by_gap:
                by_gap[key]["last_seen_at"] = utc_now_iso()
                continue
            existing.append(
                {
                    "intention_id": f"int_{uuid4().hex[:12]}",
                    "source_gap": gap,
                    "risk_level": gap.get("risk_level", RiskLevel.R1.value),
                    "suggested_action": gap.get("suggested_action"),
                    "status": "pending",
                    "created_at": utc_now_iso(),
                    "last_seen_at": utc_now_iso(),
                }
            )
        self.write_intentions(existing[-200:])
        return self.read_intentions()

    def update_intention(self, intention_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        intentions = self.read_intentions()
        updated_item = None
        for item in intentions:
            if item.get("intention_id") == intention_id:
                item.update(patch)
                item["updated_at"] = utc_now_iso()
                updated_item = item
                break
        self.write_intentions(intentions)
        return updated_item

    def read_intentions(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.intention_path.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def write_intentions(self, intentions: list[dict[str, Any]]) -> None:
        self.intention_path.write_text(json.dumps(intentions, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8") or json.dumps(default))
        except (OSError, json.JSONDecodeError):
            return default

    def _read_triggers(self) -> list[dict[str, str]]:
        if not self.triggers_path.exists():
            return []
        triggers: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in self.triggers_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("- name:"):
                if current:
                    triggers.append(current)
                current = {"name": line.split(":", 1)[1].strip()}
            elif ":" in line and current:
                key, value = line.split(":", 1)
                current[key.strip()] = value.strip()
        if current:
            triggers.append(current)
        return triggers
