from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from time import perf_counter
from typing import Callable, Any

from interface.event_schema import Decision, Route
from core.agency_core import AgencyCore
from core.state_compact import compact_foresight, compact_guardian_decision
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

    def __init__(
        self,
        state_store: WorldStateStore,
        agency_root: str = "agency",
        reasoning: CoreReasoning | None = None,
        *,
        model_assist_enabled: bool = False,
    ) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.model_assist_enabled = model_assist_enabled
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        selected_agency_root = os.getenv("VEYRA_AGENCY_ROOT", "agency") if str(agency_root) == "agency" else agency_root
        self.agency = AgencyCore(state_store, agency_root=selected_agency_root, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        self.foresight = ForesightEngine(reasoning=self.reasoning if model_assist_enabled else None)
        self.guardian = GuardianController()

    def run_read_only(self, *, timeout_seconds: float = 12.0) -> dict:
        started = perf_counter()
        probes: dict[str, Callable[[], dict[str, Any]]] = {
            "system": lambda: SystemProbe().run(""),
            "git": lambda: GitProbe().run(""),
            "openclaw": lambda: OpenClawProbe().run("18789"),
            "hermes": lambda: HermesProbe().run(""),
            "network": lambda: NetworkProbe().run("localhost"),
            "web": lambda: WebProbe().run("http://127.0.0.1:8000/"),
            "mcp": lambda: McpProbe().run(""),
        }
        results = self._run_probes(probes, timeout_seconds=max(1.0, min(timeout_seconds, 30.0)))
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
            "model_assist_enabled": self.model_assist_enabled,
            "duration_ms": int((perf_counter() - started) * 1000),
            "results": results,
            "state_gaps": gaps,
            "intentions": reviewed_intentions,
        }
        self.state_store.append_jsonl("action_record.jsonl", {"route": "proactive_check", "status": "success", "artifacts": output})
        return output

    def _run_probes(self, probes: dict[str, Callable[[], dict[str, Any]]], *, timeout_seconds: float) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        executor = ThreadPoolExecutor(max_workers=min(len(probes), 8), thread_name_prefix="veyra-proactive")
        futures = {name: executor.submit(call) for name, call in probes.items()}
        per_probe_timeout = max(0.5, timeout_seconds / max(len(probes), 1))
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=per_probe_timeout)
            except FutureTimeoutError:
                future.cancel()
                results[name] = self._timeout_probe(name, timeout_seconds=per_probe_timeout)
            except Exception as exc:
                results[name] = self._error_probe(name, exc)
        executor.shutdown(wait=False, cancel_futures=True)
        return results

    def _timeout_probe(self, name: str, *, timeout_seconds: float) -> dict[str, Any]:
        return {
            "probe": f"{name}_probe",
            "source": f"{name}_probe",
            "target": name,
            "status": "timeout",
            "summary": f"{name} proactive probe exceeded {timeout_seconds:.1f}s.",
            "confidence": 0.3,
            "ttl_seconds": 30,
            "details": {"timeout_seconds": timeout_seconds},
        }

    def _error_probe(self, name: str, exc: Exception) -> dict[str, Any]:
        return {
            "probe": f"{name}_probe",
            "source": f"{name}_probe",
            "target": name,
            "status": "error",
            "summary": f"{name} proactive probe failed: {exc}",
            "confidence": 0.2,
            "ttl_seconds": 30,
            "details": {"error": str(exc), "error_type": type(exc).__name__},
        }

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
            {
                "status": status,
                "guardian_decision": compact_guardian_decision(guardian),
                "foresight": compact_foresight(foresight),
            },
        )
        return updated or intention
