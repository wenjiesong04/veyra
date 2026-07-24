from __future__ import annotations

import os
import shlex
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from time import perf_counter
from typing import Callable, Any

from typing import Optional

from interface.event_schema import Decision, Route, utc_now_iso
from core.agency_core import AgencyCore
from core.state_compact import compact_foresight, compact_guardian_decision
from core.definitions import RiskLevel
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.log_probe import LogProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.system_probe import SystemProbe
from probes.web_probe import WebProbe


class ProactiveChecks:
    """A2/A3 MVP: run low-risk read-only checks and surface state gaps.

    Beyond detection, a bounded remediation worker turns reviewed intentions into
    action: R1 read-only gaps are remediated in place (targeted re-probe / capability
    refresh), while R2+ gaps become one-click-approvable review proposals. It never
    auto-executes a write or restart; those stay behind Guardian review.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        agency_root: str = "agency",
        reasoning: CoreReasoning | None = None,
        *,
        model_assist_enabled: bool = False,
        review_queue: ReviewQueue | None = None,
        agent_adapter_resolver: Optional[Callable[[], Any]] = None,
        agency: AgencyCore | None = None,
    ) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.model_assist_enabled = model_assist_enabled
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        selected_agency_root = os.getenv("VEYRA_AGENCY_ROOT", "agency") if str(agency_root) == "agency" else agency_root
        self.agency = agency or AgencyCore(state_store, agency_root=selected_agency_root, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        self.foresight = ForesightEngine(reasoning=self.reasoning if model_assist_enabled else None)
        self.guardian = GuardianController()
        self.review_queue = review_queue or ReviewQueue(state_store)
        self._agent_adapter_resolver = agent_adapter_resolver

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
        watched_log = os.getenv("VEYRA_APP_LOG", "").strip()
        if watched_log:
            probes["log"] = lambda path=watched_log: LogProbe().run(path)
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
        remediation: dict[str, Any] | None = None
        review_id: str | None = None
        if risk == RiskLevel.R1 and guardian.get("decision") in {"allow", "allow_with_constraints"}:
            remediation = self._remediate_read_only(intention)
            status = "executed_read_only" if remediation.get("status") in {"refreshed", "observed", "probed_fallback"} else "acknowledged"
        elif risk == RiskLevel.R2 and guardian.get("decision") in {"allow", "allow_with_constraints"}:
            review = self._create_remediation_review(intention, foresight, guardian)
            review_id = str(review.get("review_id")) if isinstance(review, dict) else None
            status = "review_created" if review_id else "suggested"
        elif guardian.get("decision") == "block":
            status = "blocked"
        elif guardian.get("decision") == "ask_user":
            status = "needs_confirmation"
        else:
            status = "suggested"
        patch: dict[str, Any] = {
            "status": status,
            "guardian_decision": compact_guardian_decision(guardian),
            "foresight": compact_foresight(foresight),
        }
        if remediation is not None:
            patch["remediation"] = remediation
        if review_id:
            patch["review_id"] = review_id
        updated = self.agency.update_intention(str(intention.get("intention_id")), patch)
        return updated or intention

    # --- Remediation worker: turn reviewed intentions into bounded action. ---

    def _remediate_read_only(self, intention: dict) -> dict[str, Any]:
        gap = intention.get("source_gap") if isinstance(intention.get("source_gap"), dict) else {}
        action = str(intention.get("suggested_action") or gap.get("suggested_action") or "")
        try:
            if action == "refresh_agent_capabilities":
                return self._refresh_agent_capabilities()
            if action == "probe_executor_status":
                return self._self_heal_agent(gap)
            if action in {"refresh_probe_group", "refresh_probe"} or action.startswith("refresh_state"):
                return self._refresh_probe_for_target(gap)
            if action == "review_core_model_config":
                return {"action": action, "status": "observed", "core_model": self.reasoning.status()}
            if action == "check_feishu_intake":
                feishu = self.state_store.read_json("feishu_ws_state.json")
                return {"action": action, "status": "observed", "feishu_ws_status": feishu.get("status")}
            if action == "suggest_diff_review" or action.startswith("review_"):
                return self._diagnose_for_review(action, gap)
        except Exception as exc:
            return {"action": action, "status": "error", "error": str(exc)}
        return {"action": action, "status": "no_handler"}

    def execute_approved_proposal(self, proposal: dict[str, Any], approved_by: str = "") -> dict[str, Any]:
        """Called after a user approves a proactive review. Lands the remediation.

        proactive_remediation gathers/refreshes read-only evidence for the gap.
        agent_restart tries a reversible reconnect first, and only runs a configured
        restart command (through SafeShell/ToolProxy) when reconnect fails.
        """
        proposal_type = str(proposal.get("type") or "")
        if proposal_type == "agent_restart":
            return self._execute_agent_restart(approved_by)
        if proposal_type == "proactive_remediation":
            intention = {
                "suggested_action": proposal.get("suggested_action"),
                "source_gap": {
                    "gap_id": proposal.get("gap_id"),
                    "target": proposal.get("target"),
                    "observed_status": proposal.get("observed_status"),
                    "suggested_action": proposal.get("suggested_action"),
                    "action_text": proposal.get("action_text"),
                },
            }
            return self._remediate_read_only(intention)
        return {"status": "not_supported", "reason": f"unknown proactive proposal type {proposal_type}"}

    def _diagnose_for_review(self, action: str, gap: dict) -> dict[str, Any]:
        if action == "review_resource_pressure":
            details = SystemProbe().run("").get("details", {})
            return {"action": action, "status": "diagnosed", "resources": details.get("resources"), "resource_pressure": details.get("resource_pressure")}
        if action == "review_log_anomaly":
            path = os.getenv("VEYRA_APP_LOG", "").strip()
            if not path:
                return {"action": action, "status": "diagnosed", "note": "no VEYRA_APP_LOG configured"}
            details = LogProbe().run(path).get("details", {})
            return {"action": action, "status": "diagnosed", "log": {"path": details.get("path"), "error_count": details.get("error_count"), "anomaly": details.get("anomaly")}}
        if action == "review_agent_task_drift":
            pending = self.state_store.read_json("task_state.json").get("pending_agent_tasks", [])
            drift = [
                t for t in pending
                if isinstance(t, dict) and (str(t.get("verification_status") or "") in {"verified_failed", "needs_rollback"} or int(t.get("poll_count") or 0) >= 5)
            ]
            return {"action": action, "status": "diagnosed", "drifting_tasks": drift[:10]}
        if action == "review_commitment_push_failures":
            ticks = self.state_store.read_json("active_loop_state.json").get("ticks", [])
            recent = []
            for tick in list(ticks)[-10:]:
                steps = tick.get("steps") if isinstance(tick.get("steps"), list) else []
                push = next((s for s in steps if isinstance(s, dict) and s.get("name") == "commitment_push"), None)
                if push:
                    recent.append({"result_status": push.get("result_status") or push.get("status")})
            return {"action": action, "status": "diagnosed", "recent_push_steps": recent}
        if action == "review_executor_for_active_commitments":
            return {
                "action": action,
                "status": "diagnosed",
                "executor": self.state_store.read_json("executor_state.json").get("status"),
                "active_commitments": self.agency._active_commitments(),
            }
        if action == "suggest_diff_review":
            git = GitProbe().run("")
            return {"action": action, "status": "diagnosed", "git": {"status": git.get("status"), "summary": git.get("summary"), "dirty": git.get("dirty")}}
        return {"action": action, "status": "diagnosed", "note": "surfaced for manual review", "gap": gap}

    def _execute_agent_restart(self, approved_by: str = "") -> dict[str, Any]:
        adapter = self._resolve_agent_adapter()
        conn: dict[str, Any] = {}
        if adapter is not None and hasattr(adapter, "connection_status"):
            try:
                conn = adapter.connection_status() or {}
            except Exception as exc:
                conn = {"status": "error", "error": str(exc)}
        if bool(conn.get("connected")) or str(conn.get("status") or "") in {"available", "ok", "success"}:
            self.state_store.patch_json(
                "executor_state.json",
                {
                    "status": str(conn.get("status") or "available"),
                    "connected": True,
                    "updated_at": utc_now_iso(),
                },
            )
            return {"status": "recovered", "method": "reconnect", "connection": self._compact_conn(conn)}
        command = os.getenv("VEYRA_AGENT_RESTART_CMD", "").strip()
        if command:
            from tool_proxy.safe_shell import SafeShell

            result = SafeShell(state_store=self.state_store).run(shlex.split(command), approved_by=approved_by or "agent_restart_review")
            return {"status": str(result.get("status") or "executed"), "method": "restart_command", "tool_result": result}
        return {
            "status": "approved_no_op",
            "method": "none",
            "reason": "reconnect failed and no VEYRA_AGENT_RESTART_CMD configured; manual restart required",
            "connection": self._compact_conn(conn),
        }

    def _refresh_agent_capabilities(self) -> dict[str, Any]:
        adapter = self._resolve_agent_adapter()
        if adapter is not None and hasattr(adapter, "fetch_capabilities"):
            caps = adapter.fetch_capabilities()
            if isinstance(caps, dict) and caps:
                def update_capabilities(executor: dict[str, Any]) -> None:
                    snapshot = (
                        executor.get("capability_snapshot")
                        if isinstance(executor.get("capability_snapshot"), dict)
                        else {}
                    )
                    executor["capability_snapshot"] = {
                        **snapshot,
                        **caps,
                        "freshness": "fresh",
                        "refreshed_at": utc_now_iso(),
                    }
                    executor["updated_at"] = utc_now_iso()

                self.state_store.mutate_json("executor_state.json", update_capabilities)
                return {"action": "refresh_agent_capabilities", "status": "refreshed", "source": "agent_adapter"}
        result = OpenClawProbe().run("18789")
        self.perception.interpret_probe_result(result)
        return {"action": "refresh_agent_capabilities", "status": "probed_fallback", "probe_status": result.get("status")}

    def _self_heal_agent(self, gap: dict) -> dict[str, Any]:
        """Reversible self-heal first; only escalate the irreversible restart to review.

        Reconnect/verify is reversible and runs automatically. A real process restart is
        irreversible (R4) and is never auto-executed - it is escalated to a review the
        user can approve, and only when active commitments actually depend on the agent.
        """
        openclaw = OpenClawProbe().run("18789")
        self.perception.interpret_probe_result(openclaw)
        adapter = self._resolve_agent_adapter()
        conn: dict[str, Any] = {}
        if adapter is not None and hasattr(adapter, "connection_status"):
            try:
                conn = adapter.connection_status() or {}
            except Exception as exc:
                conn = {"status": "error", "error": str(exc)}
        connected = bool(conn.get("connected")) or str(conn.get("status") or "") in {"available", "ok", "success"}
        if connected:
            self.state_store.patch_json(
                "executor_state.json",
                {
                    "status": str(conn.get("status") or "available"),
                    "connected": True,
                    "updated_at": utc_now_iso(),
                },
            )
            return {"action": "probe_executor_status", "status": "refreshed", "self_heal": "reconnected", "connection": self._compact_conn(conn)}
        if self.agency._active_commitments() and not self._has_pending_restart_review():
            review = self._create_restart_review(gap, conn)
            return {
                "action": "probe_executor_status",
                "status": "observed",
                "self_heal": "reconnect_failed_escalated_to_review",
                "review_id": str(review.get("review_id")) if isinstance(review, dict) else None,
            }
        return {"action": "probe_executor_status", "status": "observed", "self_heal": "reconnect_failed", "connection": self._compact_conn(conn)}

    def _compact_conn(self, conn: dict[str, Any]) -> dict[str, Any]:
        return {key: conn.get(key) for key in ("status", "connected", "error") if key in conn}

    def _has_pending_restart_review(self) -> bool:
        try:
            items = self.review_queue.list("pending")
        except Exception:
            return False
        return any(isinstance(item, dict) and (item.get("proposal") or {}).get("type") == "agent_restart" for item in items)

    def _create_restart_review(self, gap: dict, conn: dict[str, Any]) -> dict[str, Any]:
        text = "restart selected agent runtime to recover delivery (irreversible, requires approval)"
        decision = Decision(
            route=Route.HUMAN_REVIEW,
            risk_level=RiskLevel.R4,
            reason="agent self-heal escalation: reversible reconnect failed",
            requires_confirmation=True,
            intent="action",
            complexity="simple",
            capability="human_review",
            signals=["agency:self_heal", "risk:R4"],
            constraints=["irreversible restart requires explicit approval"],
        )
        foresight = self.foresight.predict_text_action(text, RiskLevel.R4, decision=decision.to_dict())
        guardian = self.guardian.review_text_action(text=text, decision=decision, foresight=foresight)
        proposal = {
            "type": "agent_restart",
            "target": gap.get("target") or "selected_agent",
            "reason": "reversible reconnect failed",
            "connection": self._compact_conn(conn),
            "risk_guess": RiskLevel.R4.value,
            "reversible": "no",
        }
        return self.review_queue.create(
            event_id=f"self_heal_{gap.get('gap_id') or 'selected_agent'}",
            task_text=text,
            risk_level=RiskLevel.R4.value,
            foresight=foresight,
            guardian_decision=guardian,
            proposal=proposal,
        )

    def _refresh_probe_for_target(self, gap: dict) -> dict[str, Any]:
        source = (str(gap.get("target") or "") + " " + str(gap.get("gap_id") or "")).lower()
        probe_plan: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            ("openclaw", lambda: OpenClawProbe().run("18789")),
            ("hermes", lambda: HermesProbe().run("")),
            ("git", lambda: GitProbe().run("")),
            ("network", lambda: NetworkProbe().run("localhost")),
            ("web", lambda: WebProbe().run("http://127.0.0.1:8000/")),
            ("mcp", lambda: McpProbe().run("")),
            ("system", lambda: SystemProbe().run("")),
        ]
        for key, call in probe_plan:
            if key in source:
                result = call()
                self.perception.interpret_probe_result(result)
                return {"action": "refresh_probe_group", "status": "refreshed", "probe": key, "probe_status": result.get("status")}
        return {"action": "refresh_probe_group", "status": "no_probe_for_source", "source": source.strip()}

    def _resolve_agent_adapter(self) -> Any:
        if self._agent_adapter_resolver is None:
            return None
        try:
            return self._agent_adapter_resolver()
        except Exception:
            return None

    def _create_remediation_review(self, intention: dict, foresight: dict, guardian: dict) -> dict[str, Any]:
        gap = intention.get("source_gap") if isinstance(intention.get("source_gap"), dict) else {}
        proposal = {
            "type": "proactive_remediation",
            "gap_id": gap.get("gap_id"),
            "target": gap.get("target"),
            "observed_status": gap.get("observed_status"),
            "suggested_action": intention.get("suggested_action") or gap.get("suggested_action"),
            "action_text": gap.get("action_text"),
            "risk_guess": str(intention.get("risk_level") or RiskLevel.R2.value),
            "reversible": "review_required",
        }
        return self.review_queue.create(
            event_id=str(intention.get("intention_id")),
            task_text=str(gap.get("action_text") or intention.get("suggested_action") or "proactive remediation"),
            risk_level=str(intention.get("risk_level") or RiskLevel.R2.value),
            foresight=foresight,
            guardian_decision=guardian,
            proposal=proposal,
        )
