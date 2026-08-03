from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore


class ConservatismMonitor:
    """Detect pipelines that are safe but produce nothing.

    Every existing alert answers "did something overstep?". None of them answer
    "did anything happen at all?". That gap is why an empty Attention focus, a
    zero-output Situation aggregation and an empty suggestion outbox all stayed
    invisible: each component was individually fail-closed and therefore
    individually "healthy".

    This complements, and does not duplicate, the `overconservative_alert`
    inside `read_only_cognitive_loop`. That one compares eligible inputs against
    candidates within a single cycle. This one follows the handoffs between
    components -- situation to general situation, general situation to
    hypothesis, hypothesis to outbox -- where a stage can silently absorb
    everything the previous stage produced.

    A stage is only reported when the stage above it has real input. Zero output
    with zero input is correct behaviour, not conservatism, and reporting it
    would train the operator to ignore this component.

    The monitor is read-only. It never mutates state, never notifies, and never
    changes a route, a threshold, or an authority.
    """

    COMPONENT = "conservatism"

    #: Default mode. A new signal must not change /health or /ops/alerts until
    #: it is explicitly enabled, so `disabled` keeps every existing response
    #: byte-identical. `report()` stays callable for direct diagnosis.
    DEFAULT_MODE = "disabled"
    ALLOWED_MODES = ("disabled", "record_only", "advise_only")

    #: A stage is only judged once the stage above it has at least this much
    #: input, so a quiet system is never reported as broken.
    MIN_UPSTREAM_INPUT = 1
    #: Fraction of recorded turn scopes that may hold no focus before the empty
    #: focus is treated as a signal rather than a normal quiet turn.
    MAX_EMPTY_FOCUS_RATIO = 0.8
    #: Fraction of recent routed turns that may ask the user for clarification
    #: before the semantic layer is treated as over-refusing.
    MAX_ASK_USER_RATIO = 0.3
    MIN_ROUTED_TURNS = 10
    RECENT_TRACE_LIMIT = 200

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def mode(self) -> str:
        ops_config = self._read("ops_config.json")
        section = ops_config.get(self.COMPONENT)
        raw = section.get("mode") if isinstance(section, dict) else section
        value = str(raw or "").strip().lower()
        return value if value in self.ALLOWED_MODES else self.DEFAULT_MODE

    def findings(self) -> list[dict[str, Any]]:
        """Findings for the shared alert stream; empty unless explicitly enabled."""

        if self.mode() == "disabled":
            return []
        return self._evaluate()

    def report(self) -> dict[str, Any]:
        """Direct diagnosis. Always evaluates, regardless of mode.

        This never feeds /health, so reading it cannot change any other
        response.
        """

        findings = self._evaluate()
        return {
            "status": "success",
            "component": self.COMPONENT,
            "mode": self.mode(),
            "alert_stream_enabled": self.mode() != "disabled",
            "finding_count": len(findings),
            "findings": findings,
            "measured": self._measurements(),
        }

    def _evaluate(self) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        findings.extend(self._attention_findings())
        findings.extend(self._aggregation_findings())
        findings.extend(self._suggestion_findings())
        findings.extend(self._belief_findings())
        findings.extend(self._route_findings())
        return findings

    def _measurements(self) -> dict[str, Any]:
        attention = self._read("attention_state.json")
        scopes = attention.get("scopes") if isinstance(attention.get("scopes"), dict) else {}
        empty_focus = sum(
            1
            for record in scopes.values()
            if isinstance(record, dict) and not (record.get("focus") or [])
        )
        situations = self._read("situation_state.json")
        general = self._read("general_situation_state.json")
        hypotheses = self._read("attention_hypothesis_state.json")
        outbox = self._read("suggestion_outbox.json")
        belief = self._read("belief_state.json")
        return {
            "attention_scopes": len(scopes),
            "attention_scopes_without_focus": empty_focus,
            "situations": self._count(situations, "situations"),
            "general_situations": self._count(general, "general_situations"),
            "attention_hypotheses": self._count(hypotheses, "hypotheses"),
            "suggestion_proposals": self._count(outbox, "proposals"),
            "belief_claims": self._count(belief, "claims"),
            "belief_unscoped_rejections": int(
                (belief.get("unscoped_rejections") or {}).get("count") or 0
            )
            if isinstance(belief.get("unscoped_rejections"), dict)
            else 0,
        }

    def _attention_findings(self) -> list[dict[str, Any]]:
        attention = self._read("attention_state.json")
        scopes = attention.get("scopes") if isinstance(attention.get("scopes"), dict) else {}
        if len(scopes) < self.MIN_UPSTREAM_INPUT:
            return []
        empty = [
            key
            for key, record in scopes.items()
            if isinstance(record, dict) and not (record.get("focus") or [])
        ]
        ratio = len(empty) / len(scopes)
        if ratio <= self.MAX_EMPTY_FOCUS_RATIO:
            return []
        return [
            self._finding(
                code="attention_focus_mostly_empty",
                message=(
                    f"{len(empty)} of {len(scopes)} turn scopes carry no focus. "
                    "Focus only comes from structured references, so this usually means "
                    "no producer is attaching goal/case/commitment anchors to events."
                ),
                details={"empty_scopes": len(empty), "total_scopes": len(scopes), "ratio": round(ratio, 3)},
            )
        ]

    def _aggregation_findings(self) -> list[dict[str, Any]]:
        situations = self._count(self._read("situation_state.json"), "situations")
        general = self._count(self._read("general_situation_state.json"), "general_situations")
        hypotheses = self._count(self._read("attention_hypothesis_state.json"), "hypotheses")
        findings: list[dict[str, Any]] = []
        if situations >= self.MIN_UPSTREAM_INPUT and general == 0:
            findings.append(
                self._finding(
                    code="situations_never_aggregate",
                    message=(
                        f"{situations} situation(s) exist but none aggregated into a general "
                        "situation. Aggregation needs a shared structured anchor."
                    ),
                    details={"situations": situations, "general_situations": 0},
                )
            )
        if general >= self.MIN_UPSTREAM_INPUT and hypotheses == 0:
            findings.append(
                self._finding(
                    code="general_situations_never_reach_hypothesis",
                    message=(
                        f"{general} general situation(s) exist but produced no attention "
                        "hypothesis. Readiness thresholds may exceed what the evidence can ever supply."
                    ),
                    details={"general_situations": general, "hypotheses": 0},
                )
            )
        return findings

    def _suggestion_findings(self) -> list[dict[str, Any]]:
        general = self._count(self._read("general_situation_state.json"), "general_situations")
        hypotheses = self._count(self._read("attention_hypothesis_state.json"), "hypotheses")
        proposals = self._count(self._read("suggestion_outbox.json"), "proposals")
        upstream = max(general, hypotheses)
        if upstream < self.MIN_UPSTREAM_INPUT or proposals > 0:
            return []
        return [
            self._finding(
                code="suggestions_never_produced",
                message=(
                    f"{upstream} upstream item(s) exist but the suggestion outbox is empty. "
                    "Nothing reaches the operator even in record-only mode."
                ),
                details={"upstream_items": upstream, "proposals": 0},
            )
        ]

    def _belief_findings(self) -> list[dict[str, Any]]:
        belief = self._read("belief_state.json")
        rejections = belief.get("unscoped_rejections")
        if not isinstance(rejections, dict):
            return []
        count = int(rejections.get("count") or 0)
        if count <= 0:
            return []
        by_source = rejections.get("by_source") if isinstance(rejections.get("by_source"), dict) else {}
        return [
            self._finding(
                code="belief_rejects_unscoped_claims",
                message=(
                    f"{count} claim(s) were refused because they carry no readable scope. "
                    "The producer is doing work whose result can never be read back."
                ),
                details={"count": count, "by_source": by_source, "last_key": rejections.get("last_key")},
            )
        ]

    def _route_findings(self) -> list[dict[str, Any]]:
        traces = self.state_store.read_jsonl("runtime_trace.jsonl", limit=self.RECENT_TRACE_LIMIT)
        routes = [
            str(item.get("final_route") or item.get("route") or "")
            for item in traces
            if isinstance(item, dict)
        ]
        routed = [route for route in routes if route]
        if len(routed) < self.MIN_ROUTED_TURNS:
            return []
        ask_user = sum(1 for route in routed if route == "ask_user")
        ratio = ask_user / len(routed)
        if ratio <= self.MAX_ASK_USER_RATIO:
            return []
        return [
            self._finding(
                code="ask_user_route_dominates",
                message=(
                    f"{ask_user} of the last {len(routed)} routed turns asked the user to clarify. "
                    "A high refusal rate is a semantic gap, not a safe default."
                ),
                details={"ask_user": ask_user, "routed_turns": len(routed), "ratio": round(ratio, 3)},
            )
        ]

    def _finding(self, *, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
        # Severity stays informational: producing nothing is not a fault, and it
        # must not push /health into degraded or block deployment readiness.
        return {
            "component": self.COMPONENT,
            "severity": "info",
            "code": code,
            "message": message,
            "details": details,
        }

    def _read(self, name: str) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(name)
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _count(state: dict[str, Any], key: str) -> int:
        explicit = state.get(f"{key.rstrip('s')}_count")
        if isinstance(explicit, int):
            return explicit
        value = state.get(key)
        if isinstance(value, (dict, list)):
            return len(value)
        return 0
