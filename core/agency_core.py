from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.definitions import RiskLevel
from core.reasoning_core import CoreReasoning, safe_model_risk
from core.state_compact import compact_intention
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class AgencyCore:
    """Turns state gaps into bounded intentions.

    Agency stays conservative: it may execute R1 refresh work through callers that
    already run read-only probes, but R2+ intentions are suggestions or reviews.
    """

    def __init__(
        self,
        state_store: WorldStateStore | None = None,
        agency_root: str | Path = "agency",
        reasoning: CoreReasoning | None = None,
        *,
        model_assist_enabled: bool = True,
    ) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or (CoreReasoning(state_store) if state_store else None)
        self.model_assist_enabled = model_assist_enabled
        selected_root = self._selected_agency_root(agency_root)
        self.agency_root = Path(selected_root)
        self.intention_path = self.agency_root / "intention_queue.json"
        self.goals_path = self.agency_root / "goals.json"
        self.triggers_path = self.agency_root / "triggers.yaml"
        self.agency_root.mkdir(parents=True, exist_ok=True)
        if not self.intention_path.exists():
            self.intention_path.write_text("[]", encoding="utf-8")

    def _selected_agency_root(self, agency_root: str | Path) -> str | Path:
        if str(agency_root) != "agency":
            return agency_root
        explicit = os.getenv("VEYRA_AGENCY_DIR") or os.getenv("VEYRA_AGENCY_ROOT")
        if explicit:
            return explicit
        env = os.getenv("VEYRA_ENV", "").strip().lower()
        if env in {"dev", "prod", "test"}:
            return Path("agency") / env
        return "agency"

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
        capability_snapshot = executor.get("capability_snapshot") if isinstance(executor.get("capability_snapshot"), dict) else {}
        if str(capability_snapshot.get("freshness") or "") in {"stale", "expired"} or self._state_age_exceeds_ttl(capability_snapshot):
            gaps.append(
                {
                    "gap_id": "selected_agent_capability_stale",
                    "target": "selected_agent.capabilities",
                    "observed_status": str(capability_snapshot.get("freshness") or "stale"),
                    "risk_level": RiskLevel.R1.value,
                    "suggested_action": "refresh_agent_capabilities",
                    "action_text": "refresh selected agent capability snapshot with read-only AgentAdapter probe",
                }
            )

        agent_config = world_state.get("agent_config", {}) if isinstance(world_state.get("agent_config"), dict) else {}
        core_model = agent_config.get("core_model") if isinstance(agent_config.get("core_model"), dict) else {}
        if core_model.get("enabled") and (not core_model.get("base_url") or not core_model.get("model")):
            gaps.append(
                {
                    "gap_id": "core_model_provider_unhealthy",
                    "target": "core_model",
                    "observed_status": "configuration_incomplete",
                    "risk_level": RiskLevel.R1.value,
                    "suggested_action": "review_core_model_config",
                    "action_text": "suggest reviewing Core model provider configuration before relying on model-assisted cognition",
                }
            )

        channel_state = world_state.get("channel_state", {}) if isinstance(world_state.get("channel_state"), dict) else {}
        feishu_channel = ((channel_state.get("channels") or {}).get("feishu") if isinstance(channel_state.get("channels"), dict) else {}) or {}
        feishu_ws = world_state.get("feishu_ws_state", {}) if isinstance(world_state.get("feishu_ws_state"), dict) else {}
        if feishu_channel.get("enabled") and str(feishu_ws.get("status") or "stopped") not in {"running", "connected"}:
            gaps.append(
                {
                    "gap_id": "feishu_intake_disconnected",
                    "target": "feishu_intake",
                    "observed_status": str(feishu_ws.get("status") or "stopped"),
                    "risk_level": RiskLevel.R1.value,
                    "suggested_action": "check_feishu_intake",
                    "action_text": "suggest checking Feishu intake connection before expecting proactive delivery",
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

        state_health = self.state_store.state_health() if self.state_store else {}
        stale_states = state_health.get("stale") if isinstance(state_health.get("stale"), list) else []
        for item in stale_states[:5]:
            if not isinstance(item, dict):
                continue
            gaps.append(
                {
                    "gap_id": f"state_ttl:{item.get('name')}",
                    "target": item.get("name"),
                    "observed_status": item.get("health_status"),
                    "risk_level": RiskLevel.R1.value,
                    "suggested_action": item.get("next_action") or "refresh_state",
                    "action_text": f"refresh stale state file {item.get('name')} before using it for execution",
                }
            )

        risk_state = world_state.get("risk_state", {}) if isinstance(world_state.get("risk_state"), dict) else {}
        current_risk = str(risk_state.get("current_risk") or "R0")
        if current_risk in {"R3", "R4", "R5"}:
            gaps.append(
                {
                    "gap_id": "tool_proxy_high_risk_signal",
                    "target": "risk_state",
                    "observed_status": current_risk,
                    "risk_level": RiskLevel.R2.value,
                    "suggested_action": "review_recent_tool_proxy_trace",
                    "action_text": "suggest reviewing high-risk ToolProxy or Guardian signal before further execution",
                }
            )

        return self._merge_model_gaps(goals, world_state, gaps)

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
        compacted = [compact_intention(item) for item in intentions if isinstance(item, dict)]
        self.intention_path.write_text(json.dumps(compacted, ensure_ascii=False, indent=2), encoding="utf-8")

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

    def _merge_model_gaps(self, goals: dict[str, Any], world_state: dict[str, Any], gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.reasoning or not self.model_assist_enabled:
            return gaps
        assist = self.reasoning.agency_assist(goals=goals, world_state=world_state, rule_gaps=gaps)
        if assist.get("status") != "model_assisted":
            return gaps
        raw_gaps = assist.get("state_gaps") or assist.get("gaps")
        if not isinstance(raw_gaps, list):
            return gaps
        existing = {str(gap.get("gap_id")) for gap in gaps}
        merged = list(gaps)
        for index, raw_gap in enumerate(raw_gaps[:8]):
            if not isinstance(raw_gap, dict):
                continue
            gap_id = str(raw_gap.get("gap_id") or f"model_gap_{index}").strip()
            if not gap_id:
                continue
            if not gap_id.startswith("model:"):
                gap_id = f"model:{gap_id}"
            if gap_id in existing:
                continue
            existing.add(gap_id)
            risk = safe_model_risk(raw_gap.get("risk_level"), RiskLevel.R2)
            merged.append(
                {
                    "gap_id": gap_id,
                    "target": str(raw_gap.get("target") or "world_state"),
                    "observed_status": str(raw_gap.get("observed_status") or "needs_attention"),
                    "risk_level": risk.value,
                    "suggested_action": str(raw_gap.get("suggested_action") or "review_state_gap"),
                    "action_text": str(raw_gap.get("action_text") or raw_gap.get("suggested_action") or "review model-detected state gap"),
                    "confidence": raw_gap.get("confidence"),
                    "source": "core_model",
                }
            )
        return merged

    def _state_age_exceeds_ttl(self, value: dict[str, Any]) -> bool:
        if not isinstance(value, dict):
            return False
        updated_at = str(value.get("updated_at") or "")
        ttl_seconds = int(value.get("ttl_seconds") or 0)
        if not updated_at or ttl_seconds <= 0:
            return False
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds() > ttl_seconds
