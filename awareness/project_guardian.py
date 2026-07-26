from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any


class ProjectGuardianEvaluator:
    """Pure, deterministic qualification for one read-only release-risk candidate.

    The evaluator consumes only structured Goal and EventInbox projections. It
    never reads natural-language text, calls a model/Agent, or grants authority.
    """

    SIGNAL_SCHEMA = "veyra.project_guardian_signal.v1"
    PRODUCER_ATTESTATION_SCHEMA = (
        "veyra.project_guardian_producer_attestation.v1"
    )
    CANDIDATE_SCHEMA = "veyra.project_guardian_candidate.v1"
    CANDIDATE_KIND = "project_release_risk"
    EVALUATOR_RULESET_VERSION = "veyra.project_guardian_ruleset.v1"
    GOAL_KIND = "project_release"
    GOAL_SCHEMA = "veyra.project_guardian_release_goal.v1"
    GOAL_SOURCE = "project_guardian_release_goal_registry"
    SIGNAL_CHANNEL = "project_guardian_signal"
    SIGNAL_COMPONENTS = {
        "git_dirty": "git_probe",
        "ci_failed": "ci_provider",
        "deployment_intent": "semantic_intent",
    }
    SIGNAL_PRODUCERS = {
        "git_dirty": {
            "producer_id": "veyra.project_guardian.git_probe.v1",
            "trust_class": "local_read_only_probe",
        },
        "ci_failed": {
            "producer_id": "veyra.project_guardian.ci_provider.v1",
            "trust_class": "provider_read_only",
        },
        "deployment_intent": {
            "producer_id": "veyra.project_guardian.semantic_intent.v1",
            "trust_class": "authoritative_semantic_observer",
        },
    }
    SIGNAL_STATES = {"present", "clear"}
    SCOPE_FIELDS = (
        "workspace_id",
        "repo_id",
        "target_ref",
        "target_environment",
        "release_cycle",
    )

    def __init__(
        self,
        *,
        correlation_window_seconds: int = 30 * 60,
        max_signal_age_seconds: int = 60 * 60,
        max_future_skew_seconds: int = 2 * 60,
    ) -> None:
        self.correlation_window_seconds = max(1, int(correlation_window_seconds))
        self.max_signal_age_seconds = max(
            self.correlation_window_seconds,
            int(max_signal_age_seconds),
        )
        self.max_future_skew_seconds = max(0, int(max_future_skew_seconds))

    def evaluate(
        self,
        *,
        goals_state: dict[str, Any],
        event_inbox_state: dict[str, Any],
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        evaluated_at = self._time(now) if now is not None else datetime.now(timezone.utc)
        if evaluated_at is None:
            raise ValueError("evaluation time must be timezone-aware")

        goals, rejected_goals = self._active_release_goals(goals_state, evaluated_at)
        signals, signal_diagnostics = self._signals(event_inbox_state, evaluated_at)
        candidates: list[dict[str, Any]] = []
        for goal in goals:
            matched = [
                signal
                for signal in signals
                if signal["user_id"] == goal["user_id"]
                and signal["goal_id"] == goal["goal_id"]
                and signal["goal_revision"] == goal["revision"]
                and self._same_scope(signal["scope"], goal["scope"])
                and goal["active_from"] <= signal["occurred_at"] <= goal["active_until"]
            ]
            latest_by_kind: dict[str, dict[str, Any]] = {}
            for signal in matched:
                current = latest_by_kind.get(signal["kind"])
                if current is None or self._signal_order(signal) > self._signal_order(current):
                    latest_by_kind[signal["kind"]] = signal

            positive = [
                signal
                for signal in latest_by_kind.values()
                if signal["state"] == "present" and signal["fresh"]
            ]
            positive.sort(key=lambda item: (item["kind"], item["event_id"]))
            if len(positive) < 2:
                continue
            if len({item["producer_id"] for item in positive}) < 2:
                continue
            if len({item["provenance_lineage"] for item in positive}) < 2:
                continue
            occurred = [item["occurred_at"] for item in positive]
            if max(occurred) - min(occurred) > timedelta(
                seconds=self.correlation_window_seconds
            ):
                continue
            candidates.append(
                self._candidate(
                    goal,
                    positive,
                    list(latest_by_kind.values()),
                    evaluated_at,
                )
            )

        candidates.sort(key=lambda item: item["candidate_id"])
        return {
            "schema_version": "veyra.project_guardian_evaluation.v1",
            "ruleset_version": self.EVALUATOR_RULESET_VERSION,
            "status": "qualified" if candidates else "no_candidate",
            "evaluated_at": self._iso(evaluated_at),
            "active_goal_count": len(goals),
            "accepted_signal_count": len(signals),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "diagnostics": {
                "rejected_goal_count": rejected_goals,
                **signal_diagnostics,
            },
        }

    def _active_release_goals(
        self,
        state: dict[str, Any],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], int]:
        raw_goals = state.get("goals") if isinstance(state, dict) else []
        if not isinstance(raw_goals, list):
            return [], 0
        accepted: list[dict[str, Any]] = []
        rejected = 0
        for raw in raw_goals:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            if (
                str(raw.get("kind") or "") != self.GOAL_KIND
                or str(raw.get("status") or "") != "active"
            ):
                continue
            goal_id = self._text(raw.get("goal_id"), 240)
            user_id = self._text(raw.get("user_id"), 240)
            revision = self._text(raw.get("revision"), 120)
            scope = self._scope(raw.get("scope"))
            active_from = self._time(raw.get("active_from"))
            active_until = self._time(raw.get("active_until"))
            target_sha = self._text(raw.get("target_sha"), 64).lower()
            try:
                state_revision = (
                    0
                    if isinstance(raw.get("state_revision"), bool)
                    else int(raw.get("state_revision"))
                )
            except (TypeError, ValueError):
                state_revision = 0
            if (
                str(raw.get("schema_version") or "") != self.GOAL_SCHEMA
                or str(raw.get("source") or "") != self.GOAL_SOURCE
                or not goal_id
                or not user_id
                or not revision
                or scope is None
                or active_from is None
                or active_until is None
                or active_until < active_from
                or not (active_from <= now <= active_until)
                or state_revision < 1
                or len(target_sha) not in {40, 64}
                or any(
                    character not in "0123456789abcdef"
                    for character in target_sha
                )
            ):
                rejected += 1
                continue
            accepted.append(
                {
                    "goal_id": goal_id,
                    "user_id": user_id,
                    "revision": revision,
                    "state_revision": state_revision,
                    "scope": scope,
                    "target_sha": target_sha,
                    "active_from": active_from,
                    "active_until": active_until,
                }
            )
        return accepted, rejected

    def _signals(
        self,
        state: dict[str, Any],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        records = state.get("events") if isinstance(state, dict) else {}
        if not isinstance(records, dict):
            records = {}
        diagnostics = {
            "rejected_signal_count": 0,
            "expired_signal_count": 0,
            "deduplicated_signal_count": 0,
        }
        deduplicated: dict[tuple[str, ...], dict[str, Any]] = {}
        for raw_record in records.values():
            if not isinstance(raw_record, dict):
                diagnostics["rejected_signal_count"] += 1
                continue
            if str(raw_record.get("status") or "") == "failed":
                diagnostics["rejected_signal_count"] += 1
                continue
            envelope = (
                raw_record.get("envelope")
                if isinstance(raw_record.get("envelope"), dict)
                else {}
            )
            payload = (
                envelope.get("payload")
                if isinstance(envelope.get("payload"), dict)
                else {}
            )
            raw_signal = (
                payload.get("project_guardian_signal")
                if isinstance(payload.get("project_guardian_signal"), dict)
                else {}
            )
            if (
                str(envelope.get("type") or "") != "observation"
                or str(payload.get("schema_version") or "") != self.SIGNAL_SCHEMA
            ):
                continue
            source = (
                envelope.get("source")
                if isinstance(envelope.get("source"), dict)
                else {}
            )
            kind = self._text(raw_signal.get("kind"), 120)
            component = self._text(raw_signal.get("source_component"), 120)
            state_value = self._text(raw_signal.get("state"), 40)
            provenance_root = self._text(raw_signal.get("provenance_root"), 240)
            evidence_id = self._text(raw_signal.get("evidence_id"), 240)
            goal_id = self._text(raw_signal.get("goal_id"), 240)
            goal_revision = self._text(raw_signal.get("goal_revision"), 120)
            user_id = self._text(source.get("user_id"), 240)
            session_id = self._text(source.get("session_id"), 240)
            source_channel = self._text(source.get("channel"), 120)
            event_id = self._text(envelope.get("event_id"), 240)
            scope = self._scope(raw_signal.get("scope"))
            occurred_at = self._time(envelope.get("occurred_at") or envelope.get("timestamp"))
            valid_until = self._time(raw_signal.get("valid_until"))
            evidence_refs = self._evidence_refs(envelope.get("evidence_refs"))
            producer_attestation = (
                raw_signal.get("producer_attestation")
                if isinstance(raw_signal.get("producer_attestation"), dict)
                else {}
            )
            expected_producer = self.SIGNAL_PRODUCERS.get(kind, {})
            producer_id = self._text(
                producer_attestation.get("producer_id"),
                200,
            )
            trust_class = self._text(
                producer_attestation.get("trust_class"),
                120,
            )
            receipt_id = self._text(
                producer_attestation.get("receipt_id"),
                240,
            )
            provenance_lineage = (
                provenance_root[len(component) + 1 :]
                if provenance_root.startswith(f"{component}:")
                else ""
            )
            expected_receipt = (
                self.producer_receipt_id_for(
                    kind=kind,
                    state=state_value,
                    source_component=component,
                    provenance_root=provenance_root,
                    evidence_id=evidence_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    scope=scope or {},
                    valid_until=self._iso(valid_until) if valid_until else "",
                    producer_id=producer_id,
                    trust_class=trust_class,
                    user_id=user_id,
                    session_id=session_id,
                    occurred_at=self._iso(occurred_at) if occurred_at else "",
                )
                if scope is not None and occurred_at is not None and valid_until is not None
                else ""
            )
            if (
                kind not in self.SIGNAL_COMPONENTS
                or component != self.SIGNAL_COMPONENTS.get(kind)
                or state_value not in self.SIGNAL_STATES
                or not provenance_root
                or not evidence_id
                or not goal_id
                or not goal_revision
                or not user_id
                or not session_id
                or not event_id
                or scope is None
                or occurred_at is None
                or valid_until is None
                or valid_until < occurred_at
                or not evidence_refs
                or source_channel != self.SIGNAL_CHANNEL
                or str(envelope.get("privacy_scope") or "") != "user"
                or not provenance_lineage
                or str(producer_attestation.get("schema_version") or "")
                != self.PRODUCER_ATTESTATION_SCHEMA
                or str(producer_attestation.get("admission_source") or "")
                != "project_guardian_signal_ingress"
                or producer_id
                != str(expected_producer.get("producer_id") or "")
                or trust_class
                != str(expected_producer.get("trust_class") or "")
                or not receipt_id
                or receipt_id != expected_receipt
                or not self._has_bound_evidence(
                    envelope.get("evidence_refs"),
                    evidence_id=evidence_id,
                    source_component=component,
                )
            ):
                diagnostics["rejected_signal_count"] += 1
                continue
            if occurred_at > now + timedelta(seconds=self.max_future_skew_seconds):
                diagnostics["rejected_signal_count"] += 1
                continue
            fresh = (
                now - occurred_at <= timedelta(seconds=self.max_signal_age_seconds)
                and valid_until >= now
            )
            if not fresh:
                diagnostics["expired_signal_count"] += 1
            signal = {
                "kind": kind,
                "state": state_value,
                "fresh": fresh,
                "source_component": component,
                "provenance_root": provenance_root,
                "provenance_lineage": provenance_lineage,
                "producer_id": producer_id,
                "producer_receipt_id": receipt_id,
                "evidence_id": evidence_id,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "user_id": user_id,
                "session_id": session_id,
                "event_id": event_id,
                "scope": scope,
                "occurred_at": occurred_at,
                "valid_until": valid_until,
                "evidence_refs": evidence_refs,
            }
            dedupe_key = (
                user_id,
                goal_id,
                goal_revision,
                *(scope[key] for key in self.SCOPE_FIELDS),
                kind,
                producer_id,
                receipt_id,
                provenance_root,
                evidence_id,
            )
            current = deduplicated.get(dedupe_key)
            if current is None or self._signal_order(signal) > self._signal_order(current):
                if current is not None:
                    diagnostics["deduplicated_signal_count"] += 1
                deduplicated[dedupe_key] = signal
            else:
                diagnostics["deduplicated_signal_count"] += 1
        return list(deduplicated.values()), diagnostics

    def _candidate(
        self,
        goal: dict[str, Any],
        signals: list[dict[str, Any]],
        frontier: list[dict[str, Any]],
        evaluated_at: datetime,
    ) -> dict[str, Any]:
        candidate_id = self.candidate_id_for(
            user_id=goal["user_id"],
            goal_id=goal["goal_id"],
            goal_revision=goal["revision"],
            scope=goal["scope"],
        )
        signal_kinds = sorted(item["kind"] for item in signals)
        evidence_refs = [
            {
                "ref_id": f"event:{signal['event_id']}",
                "source": signal["source_component"],
                "epistemic_status": "reference",
                "is_fact": False,
            }
            for signal in signals
        ]
        evidence_refs = self._unique_evidence(evidence_refs)
        public_signals = [
            {
                "kind": signal["kind"],
                "source_component": signal["source_component"],
                "producer_ref": (
                    f"producer_{self._digest(signal['producer_id'])[:16]}"
                ),
                "provenance_ref": f"prov_{self._digest(signal['provenance_lineage'])[:16]}",
                "evidence_ref": f"pgev_{self._digest(signal['evidence_id'])[:16]}",
                "event_id": signal["event_id"],
                "occurred_at": self._iso(signal["occurred_at"]),
            }
            for signal in signals
        ]
        public_signals.sort(key=lambda item: (item["kind"], item["event_id"]))
        public_frontier = [
            {
                "kind": signal["kind"],
                "state": signal["state"],
                "fresh": bool(signal["fresh"]),
                "source_component": signal["source_component"],
                "producer_ref": (
                    f"producer_{self._digest(signal['producer_id'])[:16]}"
                ),
                "provenance_ref": (
                    f"prov_{self._digest(signal['provenance_lineage'])[:16]}"
                ),
                "evidence_ref": f"pgev_{self._digest(signal['evidence_id'])[:16]}",
                "event_id": signal["event_id"],
                "occurred_at": self._iso(signal["occurred_at"]),
            }
            for signal in frontier
        ]
        public_frontier.sort(key=lambda item: (item["kind"], item["event_id"]))
        missing = sorted(set(self.SIGNAL_COMPONENTS) - set(signal_kinds))
        checks = [
            {
                "git_dirty": "review_uncommitted_changes",
                "ci_failed": "inspect_and_resolve_ci_failure",
                "deployment_intent": "confirm_release_target_and_rollback_plan",
            }[kind]
            for kind in signal_kinds
        ]
        candidate = {
            "schema_version": self.CANDIDATE_SCHEMA,
            "record_kind": "situation_candidate",
            "candidate_kind": self.CANDIDATE_KIND,
            "candidate_id": candidate_id,
            "user_id": goal["user_id"],
            "goal_id": goal["goal_id"],
            "goal_revision": goal["revision"],
            "scope": dict(goal["scope"]),
            "signal_kinds": signal_kinds,
            "signals": public_signals,
            "signal_frontier": public_frontier,
            "evidence_refs": evidence_refs,
            "source_sessions": sorted(
                {item["session_id"] for item in signals if item["session_id"]}
            ),
            "why_now": {
                "reason_codes": [
                    "active_project_release_goal",
                    "multiple_independent_release_risk_signals",
                    "deterministic_scope_and_time_match",
                ],
                "independent_signal_count": len(signal_kinds),
                "correlation_window_seconds": self.correlation_window_seconds,
            },
            "unknowns": [
                *[f"signal_not_observed:{kind}" for kind in missing],
                "release_approval",
                "rollback_readiness",
            ],
            "candidate_advice": {
                "recommendation": "review_release_readiness",
                "checks": checks,
            },
            "analysis_mode": "deterministic_read_only",
            "agent_invoked": False,
            "shadow_only": True,
            "notification_allowed": False,
            "execution_allowed": False,
            "interrupt_eligible": False,
            "qualified_at": self._iso(max(item["occurred_at"] for item in signals)),
            "transitioned_at": self._iso(
                max(item["occurred_at"] for item in frontier)
            ),
            "evaluated_at": self._iso(evaluated_at),
        }
        candidate["candidate_revision"] = self.candidate_revision_for(candidate)
        return candidate

    @classmethod
    def candidate_id_for(
        cls,
        *,
        user_id: str,
        goal_id: str,
        goal_revision: str,
        scope: dict[str, Any],
    ) -> str:
        identity = {
            "user_id": str(user_id),
            "goal_id": str(goal_id),
            "goal_revision": str(goal_revision),
            "scope": {
                key: str(scope.get(key) or "")
                for key in cls.SCOPE_FIELDS
            },
        }
        return f"pgc_{cls._digest(identity)[:20]}"

    @classmethod
    def candidate_revision_for(cls, candidate: dict[str, Any]) -> str:
        identity = {
            "user_id": str(candidate.get("user_id") or ""),
            "goal_id": str(candidate.get("goal_id") or ""),
            "goal_revision": str(candidate.get("goal_revision") or ""),
            "scope": {
                key: str(
                    (candidate.get("scope") or {}).get(key)
                    if isinstance(candidate.get("scope"), dict)
                    else ""
                )
                for key in cls.SCOPE_FIELDS
            },
        }
        semantics = {
            "schema_version": str(candidate.get("schema_version") or ""),
            "record_kind": str(candidate.get("record_kind") or ""),
            "candidate_kind": str(candidate.get("candidate_kind") or ""),
            "identity": identity,
            "signal_kinds": candidate.get("signal_kinds")
            if isinstance(candidate.get("signal_kinds"), list)
            else [],
            "signals": candidate.get("signals")
            if isinstance(candidate.get("signals"), list)
            else [],
            "signal_frontier": candidate.get("signal_frontier")
            if isinstance(candidate.get("signal_frontier"), list)
            else [],
            "evidence_refs": candidate.get("evidence_refs")
            if isinstance(candidate.get("evidence_refs"), list)
            else [],
            "source_sessions": candidate.get("source_sessions")
            if isinstance(candidate.get("source_sessions"), list)
            else [],
            "why_now": candidate.get("why_now")
            if isinstance(candidate.get("why_now"), dict)
            else {},
            "unknowns": candidate.get("unknowns")
            if isinstance(candidate.get("unknowns"), list)
            else [],
            "candidate_advice": candidate.get("candidate_advice")
            if isinstance(candidate.get("candidate_advice"), dict)
            else {},
            "analysis_mode": str(candidate.get("analysis_mode") or ""),
            "agent_invoked": candidate.get("agent_invoked"),
            "shadow_only": candidate.get("shadow_only"),
            "notification_allowed": candidate.get("notification_allowed"),
            "execution_allowed": candidate.get("execution_allowed"),
            "interrupt_eligible": candidate.get("interrupt_eligible"),
            "qualified_at": str(candidate.get("qualified_at") or ""),
        }
        return f"pgr_{cls._digest(semantics)[:20]}"

    @classmethod
    def producer_receipt_id_for(
        cls,
        *,
        kind: str,
        state: str,
        source_component: str,
        provenance_root: str,
        evidence_id: str,
        goal_id: str,
        goal_revision: str,
        scope: dict[str, Any],
        valid_until: str,
        producer_id: str,
        trust_class: str,
        user_id: str,
        session_id: str,
        occurred_at: str,
    ) -> str:
        """Bind a persisted signal to the dedicated in-process ingress.

        This receipt is an integrity/binding identifier inside Veyra's trusted
        local state boundary, not a cryptographic provider signature.
        """

        binding = {
            "schema_version": cls.SIGNAL_SCHEMA,
            "kind": str(kind),
            "state": str(state),
            "source_component": str(source_component),
            "provenance_root": str(provenance_root),
            "evidence_id": str(evidence_id),
            "goal_id": str(goal_id),
            "goal_revision": str(goal_revision),
            "scope": {
                key: str(scope.get(key) or "")
                for key in cls.SCOPE_FIELDS
            },
            "valid_until": str(valid_until),
            "producer_id": str(producer_id),
            "trust_class": str(trust_class),
            "user_id": str(user_id),
            "session_id": str(session_id),
            "occurred_at": str(occurred_at),
        }
        return f"pgsr_{cls._digest(binding)[:24]}"

    def _scope(self, value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        selected = {
            key: self._text(value.get(key), 240)
            for key in self.SCOPE_FIELDS
        }
        return selected if all(selected.values()) else None

    def _same_scope(self, left: dict[str, str], right: dict[str, str]) -> bool:
        return all(left.get(key) == right.get(key) for key in self.SCOPE_FIELDS)

    @staticmethod
    def _signal_order(
        signal: dict[str, Any],
    ) -> tuple[datetime, int, str]:
        # Producer clocks may only have second precision. When two opposite
        # states share a timestamp and no producer sequence exists, prefer the
        # conservative clear tombstone instead of allowing event-id ordering
        # to resurrect a risk.
        return (
            signal["occurred_at"],
            int(str(signal.get("state") or "") == "clear"),
            str(signal["event_id"]),
        )

    def _evidence_refs(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        refs: list[dict[str, Any]] = []
        for raw in value[:32]:
            if not isinstance(raw, dict):
                continue
            ref_id = self._text(raw.get("ref_id"), 240)
            if not ref_id:
                continue
            refs.append(
                {
                    "ref_id": ref_id,
                    "source": self._text(raw.get("source"), 120) or "unknown",
                    "epistemic_status": "reference",
                    "is_fact": False,
                }
            )
        return self._unique_evidence(refs)

    def _has_bound_evidence(
        self,
        value: Any,
        *,
        evidence_id: str,
        source_component: str,
    ) -> bool:
        if not isinstance(value, list):
            return False
        return any(
            isinstance(raw, dict)
            and self._text(raw.get("ref_id"), 240) == evidence_id
            and self._text(raw.get("source"), 120) == source_component
            and raw.get("is_fact") is True
            for raw in value[:32]
        )

    @staticmethod
    def _unique_evidence(values: Any) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            selected.append(dict(value))
        selected.sort(key=lambda item: (str(item.get("source") or ""), str(item.get("ref_id") or "")))
        return selected[:64]

    @staticmethod
    def _time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str("" if value is None else value).strip()[:limit]

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
