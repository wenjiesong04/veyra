from __future__ import annotations

from interface.event_schema import Decision, Route
from core.agency_core import AgencyCore
from core.definitions import RiskLevel
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.system_probe import SystemProbe
from probes.web_probe import WebProbe


class ProactiveChecks:
    """A2/A3 MVP: run low-risk read-only checks and surface state gaps."""

    def __init__(self, state_store: WorldStateStore, agency_root: str = "agency", reasoning: CoreReasoning | None = None) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning)
        self.agency = AgencyCore(state_store, agency_root=agency_root, reasoning=self.reasoning)
        self.foresight = ForesightEngine(reasoning=self.reasoning)
        self.guardian = GuardianController()

    def run_read_only(self) -> dict:
        results = {
            "system": SystemProbe().run(""),
            "git": GitProbe().run(""),
            "openclaw": OpenClawProbe().run("18789"),
            "hermes": HermesProbe().run(""),
            "network": NetworkProbe().run("localhost"),
            "web": WebProbe().run("http://127.0.0.1:8000/"),
            "mcp": McpProbe().run(""),
        }
        for result in results.values():
            self.perception.interpret_probe_result(result)
        gaps = []
        openclaw = results["openclaw"]
        if openclaw.get("status") != "listening":
            gaps.append({"target": "openclaw_runtime", "status": "unavailable", "suggestion": "Check OpenClaw runtime or configure its port."})
        git = results["git"]
        if git.get("dirty"):
            gaps.append({"target": "git_workspace", "status": "dirty", "suggestion": "Review changes before risky operations."})
        intentions = self.agency.sync_intentions(self.state_store.read_all())
        reviewed_intentions = [self._review_intention(item) for item in intentions if item.get("status") == "pending"]
        output = {
            "status": "success",
            "autonomy_level": "A3",
            "results": results,
            "state_gaps": gaps,
            "intentions": reviewed_intentions,
        }
        self.state_store.append_jsonl("action_record.jsonl", {"route": "proactive_check", "status": "success", "artifacts": output})
        return output

    def _review_intention(self, intention: dict) -> dict:
        risk = RiskLevel(str(intention.get("risk_level") or RiskLevel.R1.value))
        text = str(intention.get("source_gap", {}).get("action_text") or intention.get("suggested_action") or "")
        decision = Decision(
            route=Route.PROBE if risk == RiskLevel.R1 else Route.HUMAN_REVIEW,
            risk_level=risk,
            reason="proactive intention review",
            requires_confirmation=risk not in {RiskLevel.R0, RiskLevel.R1, RiskLevel.R2},
            intent="action",
            complexity="simple",
            capability="probe" if risk == RiskLevel.R1 else "human_review",
            signals=["agency:intention", f"risk:{risk.value}"],
            constraints=["read-only automatic execution" if risk == RiskLevel.R1 else "suggest only"],
        )
        foresight = self.foresight.predict_text_action(text, risk, decision=decision.to_dict())
        guardian = self.guardian.review_text_action(text=text, decision=decision, foresight=foresight)
        if risk == RiskLevel.R1 and guardian.get("decision") in {"allow", "allow_with_constraints"}:
            status = "executed_read_only"
        elif risk == RiskLevel.R2 and guardian.get("decision") in {"allow", "allow_with_constraints"}:
            status = "suggested"
        elif guardian.get("decision") == "block":
            status = "blocked"
        elif guardian.get("decision") == "ask_user":
            status = "needs_confirmation"
        else:
            status = "suggested"
        updated = self.agency.update_intention(
            str(intention.get("intention_id")),
            {"status": status, "guardian_decision": guardian, "foresight": foresight},
        )
        return updated or intention
