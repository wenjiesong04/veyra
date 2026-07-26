from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import WorldStateStore
from interface.event_schema import EventSource, EventType, VeyraEvent, utc_now_iso
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger


class ProjectGuardianRuntime:
    """Bounded record-only/shadow runtime with no project execution authority."""

    MODES = {"disabled", "record_only", "shadow"}
    STATE_FILE = "project_guardian_state.json"
    MAX_RUNS = 100
    MAX_CANDIDATES = 200
    MAX_PROJECTION_HISTORY = 64
    LIFECYCLE_PROJECTION_KINDS = {"candidate", "closure", "reopen"}
    LIVE_PROJECTION_STATUSES = {"admitted", "projected"}

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        publish_event: Callable[[VeyraEvent], dict[str, Any]],
        event_fabric_mode: Callable[[], str | dict[str, Any]],
        evaluator: ProjectGuardianEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.publish_event = publish_event
        self.event_fabric_mode = event_fabric_mode
        self.evaluator = evaluator or ProjectGuardianEvaluator()
        self.signal_ledger = ProjectGuardianSignalLedger(state_store)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run_lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._guardian_snapshot()["mode"]

    @property
    def mode_epoch(self) -> int:
        return self._guardian_snapshot()["mode_epoch"]

    def configure(self, mode: str) -> dict[str, Any]:
        selected = str(mode or "").strip().lower()
        if selected not in self.MODES:
            raise ValueError(f"mode must be one of {sorted(self.MODES)}")
        result: dict[str, Any] = {}

        def update(config: dict[str, Any]) -> None:
            current = (
                config.get("project_guardian")
                if isinstance(config.get("project_guardian"), dict)
                else {}
            )
            previous = self._mode_value(current.get("mode"), default="disabled")
            previous_epoch = self._nonnegative_int(current.get("mode_epoch"))
            next_epoch = previous_epoch + int(previous != selected)
            config["project_guardian"] = {
                **copy.deepcopy(current),
                "mode": selected,
                "mode_epoch": next_epoch,
                "allowed_modes": sorted(self.MODES),
            }
            result.update(
                previous_mode=previous,
                mode=selected,
                mode_epoch=next_epoch,
            )

        self.state_store.mutate_json("ops_config.json", update)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "project_guardian_config",
                "status": "updated",
                "artifacts": {
                    **result,
                    "read_only": True,
                },
            },
        )
        return {
            "status": "updated",
            **result,
            "allowed_modes": sorted(self.MODES),
        }

    def status(self) -> dict[str, Any]:
        guardian = self._guardian_snapshot()
        fabric = self._fabric_snapshot()
        state = self.state_store.read_json(self.STATE_FILE)
        signal_state = self.state_store.read_json(
            ProjectGuardianSignalLedger.STATE_FILE
        )
        candidates = (
            state.get("candidates")
            if isinstance(state.get("candidates"), list)
            else []
        )
        return {
            "status": "success",
            "mode": guardian["mode"],
            "mode_epoch": guardian["mode_epoch"],
            "effective_mode": (
                "blocked_dependency"
                if guardian["mode"] == "shadow"
                and fabric["mode"] not in {"record_only", "shadow"}
                else guardian["mode"]
            ),
            "allowed_modes": sorted(self.MODES),
            "event_fabric_mode": fabric["mode"],
            "event_fabric_mode_epoch": fabric["mode_epoch"],
            "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
            "contracts": {
                "read_only": True,
                "shadow_only": True,
                "notifications": "disabled_by_contract",
                "agent_execution": "disabled_by_contract",
                "review_creation": "disabled_by_contract",
                "project_mutation": "disabled_by_contract",
            },
            "last_run": copy.deepcopy(state.get("last_run")),
            "candidate_count": sum(
                1
                for candidate in candidates
                if isinstance(candidate, dict)
                and str(candidate.get("disposition") or "") != "inactive"
            ),
            "total_candidate_count": len(candidates),
            "signal_count": int(signal_state.get("signal_count") or 0),
        }

    def run_once(self, *, reason: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "mode": self.mode,
                "candidate_count": 0,
                "published_count": 0,
            }
        try:
            return self._run_once_locked(reason=reason)
        finally:
            self._run_lock.release()

    def list_candidates(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        disposition: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        candidates = self.state_store.read_json(self.STATE_FILE).get("candidates")
        if not isinstance(candidates, list):
            return []
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("user_id") or "") != str(user_id):
                continue
            sessions = (
                candidate.get("source_sessions")
                if isinstance(candidate.get("source_sessions"), list)
                else []
            )
            if session_id is not None and str(session_id) not in {
                str(item) for item in sessions
            }:
                continue
            if (
                disposition is not None
                and str(candidate.get("disposition") or "")
                != str(disposition)
            ):
                continue
            selected.append(copy.deepcopy(candidate))
        selected.sort(
            key=lambda item: str(item.get("last_evaluated_at") or ""),
            reverse=True,
        )
        return selected[: max(0, min(int(limit), 500))]

    def _run_once_locked(self, *, reason: str) -> dict[str, Any]:
        guardian = self._guardian_snapshot()
        guardian_state = self.state_store.read_json(self.STATE_FILE)
        if guardian_state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "mode": guardian["mode"],
                "mode_epoch": guardian["mode_epoch"],
                "reason": "project_guardian_state_corrupt",
                "state_frozen": True,
                "candidate_count": 0,
                "would_publish_count": 0,
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 1,
                "capacity_skipped_count": 0,
                "error_type": "state_corrupt",
            }
        if guardian["mode"] == "disabled":
            return {
                "status": "disabled",
                "mode": "disabled",
                "mode_epoch": guardian["mode_epoch"],
                "candidate_count": 0,
                "published_count": 0,
            }
        try:
            signal_reconciliation = (
                self.signal_ledger.reconcile_event_inbox()
            )
        except Exception as exc:
            return {
                "status": "degraded",
                "mode": guardian["mode"],
                "mode_epoch": guardian["mode_epoch"],
                "reason": "signal_reconciliation_failed",
                "candidate_count": 0,
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 1,
                "signal_reconciliation": {
                    "status": "degraded",
                    "error_type": type(exc).__name__,
                },
            }
        now = self._clock()
        goals_state = self.state_store.read_json("user_goals.json")
        evaluation = self.evaluator.evaluate(
            goals_state=goals_state,
            event_inbox_state=self.signal_ledger.evaluation_state(),
            now=now,
        )
        evaluation["signal_reconciliation"] = signal_reconciliation
        candidates = [
            copy.deepcopy(candidate)
            for candidate in (
                evaluation.get("candidates")
                if isinstance(evaluation.get("candidates"), list)
                else []
            )
            if isinstance(candidate, dict)
        ]
        active_ids = {
            str(candidate.get("candidate_id") or "")
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        previous = self._candidate_state()
        capacity = self._candidate_capacity_admission(
            previous=previous,
            candidates=candidates,
            active_ids=active_ids,
        )
        if capacity["frozen"]:
            return self._result(
                status="degraded",
                mode=guardian["mode"],
                reason="candidate_capacity_invariant_violation",
                evaluation=evaluation,
                candidate_count=len(candidates),
                would_publish_count=0,
                failed_count=1,
                capacity_skipped_count=len(candidates),
            )
        admitted_candidates = capacity["candidates"]
        evicted_candidate_ids = set(capacity["evicted_candidate_ids"])
        capacity_skipped_count = len(capacity["skipped_candidate_ids"])
        capacity_reason = (
            "candidate_capacity_exhausted"
            if capacity_skipped_count
            else ""
        )

        if guardian["mode"] == "record_only":
            entries: list[dict[str, Any]] = []
            for candidate in admitted_candidates:
                candidate_id = str(candidate["candidate_id"])
                prior = previous.get(candidate_id, {})
                event_candidate, projection_kind = self._active_projection_candidate(
                    candidate,
                    prior,
                )
                context = self._projection_context(
                    candidate_id=candidate_id,
                    candidate_revision=str(
                        event_candidate["candidate_revision"]
                    ),
                    projection_kind=projection_kind,
                    previous=prior,
                )
                disposition = (
                    context["status"]
                    if context["status"] in {"admitted", "projected"}
                    else "would_reopen"
                    if projection_kind == "reopen"
                    else "would_publish"
                )
                entries.append({**candidate, "disposition": disposition})
            result = self._result(
                status="degraded" if capacity_skipped_count else "success",
                mode="record_only",
                reason=capacity_reason or reason,
                evaluation=evaluation,
                candidate_count=len(candidates),
                would_publish_count=sum(
                    1
                    for entry in entries
                    if entry.get("disposition") == "would_publish"
                ),
                capacity_skipped_count=capacity_skipped_count,
            )
            self._persist_run(
                result,
                candidates=entries,
                active_ids=active_ids,
                evicted_candidate_ids=evicted_candidate_ids,
            )
            return result

        fabric = self._fabric_snapshot()
        if fabric["mode"] not in {"record_only", "shadow"}:
            entries = [
                {
                    **candidate,
                    "disposition": "blocked_dependency",
                    "blocked_reason": (
                        "event_fabric_disabled"
                        if fabric["mode"] == "disabled"
                        else "event_fabric_unavailable"
                    ),
                }
                for candidate in admitted_candidates
            ]
            result = self._result(
                status="degraded" if capacity_skipped_count else "skipped",
                mode="shadow",
                reason=(
                    capacity_reason
                    or (
                        entries[0]["blocked_reason"]
                        if entries
                        else "event_fabric_disabled"
                    )
                ),
                evaluation=evaluation,
                candidate_count=len(candidates),
                would_publish_count=len(admitted_candidates),
                capacity_skipped_count=capacity_skipped_count,
            )
            self._persist_run(
                result,
                candidates=entries,
                active_ids=active_ids,
                evicted_candidate_ids=evicted_candidate_ids,
            )
            return result

        entries: list[dict[str, Any]] = []
        counters = {
            "published_count": 0,
            "projected_count": 0,
            "deduplicated_count": 0,
            "failed_count": 0,
            "closure_count": 0,
            "capacity_skipped_count": capacity_skipped_count,
        }
        skipped_reason = ""
        for candidate in admitted_candidates:
            candidate_id = str(candidate["candidate_id"])
            prior = previous.get(candidate_id, {})
            event_candidate, projection_kind = self._active_projection_candidate(
                candidate,
                prior,
            )
            projection = self._project_revision(
                state_candidate=candidate,
                event_candidate=event_candidate,
                projection_kind=projection_kind,
                previous=prior,
            )
            entries.append(projection["candidate"])
            self._add_counts(counters, projection)
            skipped_reason = skipped_reason or str(projection.get("skipped_reason") or "")

        for candidate_id, prior in previous.items():
            if (
                candidate_id in active_ids
                or candidate_id in evicted_candidate_ids
                or not self._has_candidate_projection(prior)
            ):
                continue
            if (
                str(
                    self._latest_lifecycle_projection(prior).get(
                        "projection_kind"
                    )
                    or ""
                )
                == "closure"
            ):
                continue
            closure = self._closure_candidate(prior, now=now)
            projection = self._project_revision(
                state_candidate=prior,
                event_candidate=closure,
                projection_kind="closure",
                previous=prior,
            )
            closure_entry = projection["candidate"]
            closure_entry["closure_disposition"] = closure_entry.pop(
                "disposition",
                "close_pending",
            )
            entries.append(closure_entry)
            self._add_counts(counters, projection)
            counters["closure_count"] += int(
                projection.get("published_count", 0)
                or projection.get("projected_count", 0)
            )
            skipped_reason = skipped_reason or str(projection.get("skipped_reason") or "")

        status = (
            "degraded"
            if counters["failed_count"] or capacity_skipped_count
            else "skipped"
            if skipped_reason
            else "success"
        )
        result = self._result(
            status=status,
            mode="shadow",
            reason=skipped_reason or capacity_reason or reason,
            evaluation=evaluation,
            candidate_count=len(candidates),
            would_publish_count=len(admitted_candidates),
            **counters,
        )
        self._persist_run(
            result,
            candidates=entries,
            active_ids=active_ids,
            evicted_candidate_ids=evicted_candidate_ids,
        )
        return result

    def _project_revision(
        self,
        *,
        state_candidate: dict[str, Any],
        event_candidate: dict[str, Any],
        projection_kind: str,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_id = str(event_candidate["candidate_id"])
        revision = str(event_candidate["candidate_revision"])
        context = self._projection_context(
            candidate_id=candidate_id,
            candidate_revision=revision,
            projection_kind=projection_kind,
            previous=previous,
        )
        if context["status"] in {"projected", "admitted"}:
            reconciled = (
                copy.deepcopy(context.get("record"))
                if isinstance(context.get("record"), dict)
                else None
            )
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": context["status"],
                    "replay_status": "deduplicated",
                    "projection_event_id": str(
                        (reconciled or {}).get("event_id") or ""
                    ),
                    "_projection_record": reconciled,
                },
                "published_count": 0,
                "projected_count": int(context["status"] == "projected"),
                "deduplicated_count": 1,
                "failed_count": 0,
            }

        guardian = self._guardian_snapshot()
        fabric = self._fabric_snapshot()
        if guardian["mode"] != "shadow" or fabric["mode"] not in {
            "record_only",
            "shadow",
        }:
            reason = (
                "project_guardian_disabled_during_run"
                if guardian["mode"] != "shadow"
                else "event_fabric_disabled_during_run"
                if fabric["mode"] == "disabled"
                else "event_fabric_unavailable_during_run"
            )
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": "blocked_dependency",
                    "blocked_reason": reason,
                },
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 0,
                "skipped_reason": reason,
            }

        event = self._candidate_event(
            event_candidate,
            projection_kind=projection_kind,
            projection_sequence=context["sequence"],
            projection_attempt=context["next_attempt"],
            guardian_mode_epoch=guardian["mode_epoch"],
            event_fabric_mode_epoch=fabric["mode_epoch"],
        )
        try:
            admission = self.publish_event(event)
        except Exception as exc:
            record = self._projection_record(
                event,
                status="failed",
                error_type=type(exc).__name__,
            )
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": "publish_failed",
                    "_projection_record": record,
                },
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 1,
            }

        admission_status = str(admission.get("status") or "unknown")
        guardian_after = self._guardian_snapshot()
        fabric_after = self._fabric_snapshot()
        if (
            guardian_after != guardian
            or fabric_after["mode"] != fabric["mode"]
            or fabric_after["mode_epoch"] != fabric["mode_epoch"]
        ):
            reason = "mode_changed_during_publish"
            record = self._projection_record(event, status="admitted")
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": "admitted",
                    "blocked_reason": reason,
                    "_projection_record": record,
                },
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 0,
                "skipped_reason": reason,
            }
        if admission_status in {"enqueued", "duplicate"}:
            delivery = self._delivery_status(event.event_id)
            record = self._projection_record(event, status=delivery)
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": delivery,
                    "projection_event_id": event.event_id,
                    "_projection_record": record,
                },
                "published_count": int(admission_status == "enqueued"),
                "projected_count": int(delivery == "projected"),
                "deduplicated_count": int(admission_status == "duplicate"),
                "failed_count": 0,
            }
        if admission_status == "disabled":
            reason = str(admission.get("reason") or "publish_gate_disabled")
            return {
                "candidate": {
                    **copy.deepcopy(state_candidate),
                    "disposition": "blocked_dependency",
                    "blocked_reason": reason,
                },
                "published_count": 0,
                "projected_count": 0,
                "deduplicated_count": 0,
                "failed_count": 0,
                "skipped_reason": reason,
            }
        record = self._projection_record(
            event,
            status="failed",
            error_type=admission_status,
        )
        return {
            "candidate": {
                **copy.deepcopy(state_candidate),
                "disposition": "publish_failed",
                "_projection_record": record,
            },
            "published_count": 0,
            "projected_count": 0,
            "deduplicated_count": 0,
            "failed_count": 1,
        }

    def _projection_context(
        self,
        *,
        candidate_id: str,
        candidate_revision: str,
        projection_kind: str,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        history = self._projection_history(previous)
        by_event = {
            str(item.get("event_id") or ""): copy.deepcopy(item)
            for item in history
            if item.get("event_id")
        }
        lifecycle_head = self._lifecycle_head(previous)
        if lifecycle_head:
            by_event[str(lifecycle_head["event_id"])] = lifecycle_head
        inbox = self.state_store.read_json("event_inbox.json")
        records = inbox.get("events") if isinstance(inbox.get("events"), dict) else {}
        active_event_ids: set[str] = set()
        for raw in records.values():
            if not isinstance(raw, dict) or not isinstance(raw.get("envelope"), dict):
                continue
            try:
                event = VeyraEvent.from_dict(raw["envelope"])
            except Exception:
                continue
            binding = self.validate_projection_event(event)
            if (
                binding is None
                or binding["candidate_id"] != candidate_id
                or binding["candidate_revision"] != candidate_revision
                or binding["projection_kind"] != projection_kind
            ):
                continue
            status = self._record_delivery_status(raw)
            by_event[event.event_id] = self._projection_record(event, status=status)
            if str(raw.get("status") or "") in {"pending", "claimed"}:
                active_event_ids.add(event.event_id)
        all_history = list(by_event.values())
        selected = [
            item
            for item in all_history
            if str(item.get("candidate_revision") or "") == candidate_revision
            and str(item.get("projection_kind") or "") == projection_kind
        ]
        selected.sort(
            key=lambda item: (
                self._nonnegative_int(item.get("projection_sequence")),
                self._nonnegative_int(item.get("projection_attempt")),
                str(item.get("event_id") or ""),
            )
        )
        situation_projection = self._situation_projection(
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
        )
        active = [
            item
            for item in selected
            if str(item.get("event_id") or "") in active_event_ids
        ]
        status = (
            "projected"
            if situation_projection is not None
            else "admitted"
            if active
            else "retry"
        )
        sequences = [
            self._nonnegative_int(item.get("projection_sequence"))
            for item in selected
        ]
        all_sequences = [
            self._nonnegative_int(item.get("projection_sequence"))
            for item in all_history
        ]
        sequence = (
            max(sequences)
            if sequences
            else self._nonnegative_int(
                (situation_projection or {}).get("projection_sequence")
            )
            if situation_projection is not None
            else max(all_sequences, default=0) + 1
        )
        attempts = [
            self._nonnegative_int(item.get("projection_attempt"))
            for item in selected
        ]
        situation_event_id = str(
            (situation_projection or {}).get("event_id") or ""
        )
        projected_records = [
            item
            for item in selected
            if situation_event_id
            and str(item.get("event_id") or "") == situation_event_id
        ]
        if status == "projected" and projected_records:
            selected_record: dict[str, Any] | None = copy.deepcopy(
                projected_records[-1]
            )
        elif status == "projected":
            selected_record = {
                "candidate_revision": candidate_revision,
                "content_revision": candidate_revision,
                "projection_kind": projection_kind,
                "projection_sequence": max(1, sequence),
                "projection_attempt": 0,
                "event_id": situation_event_id,
            }
        elif status == "admitted" and active:
            selected_record = copy.deepcopy(active[-1])
        else:
            selected_record = copy.deepcopy(selected[-1]) if selected else None
        if isinstance(selected_record, dict):
            selected_record["status"] = status
            if situation_projection is not None:
                selected_record["situation_id"] = str(
                    situation_projection.get("situation_id") or ""
                )
        return {
            "status": status,
            "sequence": max(1, sequence),
            "next_attempt": max(attempts, default=-1) + 1,
            "history": all_history,
            "record": selected_record,
        }

    def _candidate_event(
        self,
        candidate: dict[str, Any],
        *,
        projection_kind: str = "candidate",
        projection_sequence: int = 1,
        projection_attempt: int = 0,
        guardian_mode_epoch: int | None = None,
        event_fabric_mode_epoch: int | None = None,
    ) -> VeyraEvent:
        guardian_epoch = (
            self.mode_epoch
            if guardian_mode_epoch is None
            else self._nonnegative_int(guardian_mode_epoch)
        )
        fabric_epoch = (
            self._fabric_snapshot()["mode_epoch"]
            if event_fabric_mode_epoch is None
            else self._nonnegative_int(event_fabric_mode_epoch)
        )
        event_candidate = self._event_candidate(candidate)
        event_candidate.update(
            projection_kind=projection_kind,
            projection_sequence=max(1, int(projection_sequence)),
            projection_attempt=max(0, int(projection_attempt)),
            guardian_mode_epoch=guardian_epoch,
            event_fabric_mode_epoch=fabric_epoch,
            lifecycle_state=(
                "inactive" if projection_kind == "closure" else "active"
            ),
            situation_status=(
                "closed" if projection_kind == "closure" else "observed"
            ),
        )
        candidate_id = str(event_candidate["candidate_id"])
        event_candidate["situation_id"] = self.situation_id_for(candidate_id)
        binding = self._projection_binding(event_candidate)
        event_id = self.projection_event_id_for(binding)
        event_candidate["observation"] = copy.deepcopy(event_candidate)
        event_candidate["goal_refs"] = [
            {
                "ref_id": str(event_candidate["goal_id"]),
                "kind": ProjectGuardianEvaluator.GOAL_KIND,
                "revision": str(event_candidate["goal_revision"]),
            }
        ]
        event_candidate["salience_components"] = {
            "goal_relevance": 1.0,
            "evidence_diversity": round(
                len(event_candidate.get("signal_kinds") or [])
                / len(ProjectGuardianEvaluator.SIGNAL_COMPONENTS),
                4,
            ),
        }
        return VeyraEvent(
            type=EventType.OBSERVATION,
            source=EventSource(
                channel="project_guardian",
                user_id=str(event_candidate["user_id"]),
                session_id=self.session_id_for(
                    str(event_candidate["user_id"]),
                    candidate_id,
                ),
            ),
            payload=event_candidate,
            event_id=event_id,
            correlation_id=self.correlation_id_for(candidate_id),
            subject=[
                {
                    "subject_type": "goal",
                    "ref_id": str(event_candidate["goal_id"]),
                },
                {
                    "subject_type": "project",
                    "ref_id": str(event_candidate["scope"]["workspace_id"]),
                },
            ],
            evidence_refs=copy.deepcopy(event_candidate.get("evidence_refs") or []),
            dedupe_key=f"project_guardian_projection:{event_id}",
            timestamp=str(
                event_candidate.get("transitioned_at")
                or event_candidate.get("qualified_at")
            ),
            occurred_at=str(
                event_candidate.get("transitioned_at")
                or event_candidate.get("qualified_at")
            ),
            privacy_scope="user",
        )

    @classmethod
    def validate_projection_event(
        cls,
        event: VeyraEvent,
    ) -> dict[str, Any] | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            event.type != EventType.OBSERVATION
            or str(event.source.channel or "") != "project_guardian"
            or str(payload.get("schema_version") or "")
            != ProjectGuardianEvaluator.CANDIDATE_SCHEMA
            or str(payload.get("candidate_kind") or "")
            != ProjectGuardianEvaluator.CANDIDATE_KIND
            or str(event.privacy_scope or "") != "user"
        ):
            return None
        candidate_id = str(payload.get("candidate_id") or "")
        user_id = str(payload.get("user_id") or "")
        goal_id = str(payload.get("goal_id") or "")
        goal_revision = str(payload.get("goal_revision") or "")
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        expected_candidate_id = ProjectGuardianEvaluator.candidate_id_for(
            user_id=user_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            scope=scope,
        )
        projection_kind = str(payload.get("projection_kind") or "")
        content_revision = ProjectGuardianEvaluator.candidate_revision_for(
            payload
        )
        if projection_kind == "candidate":
            expected_revision = content_revision
            expected_status = "observed"
            expected_lifecycle = "active"
        elif projection_kind == "closure":
            closure_of_revision = str(
                payload.get("closure_of_revision") or ""
            )
            if (
                not closure_of_revision
                or str(payload.get("content_revision") or "")
                != content_revision
            ):
                return None
            expected_revision = cls.closure_revision_for(
                candidate_id,
                closure_of_revision,
            )
            expected_status = "closed"
            expected_lifecycle = "inactive"
        elif projection_kind == "reopen":
            reopen_of_closure_revision = str(
                payload.get("reopen_of_closure_revision") or ""
            )
            if (
                not reopen_of_closure_revision
                or str(payload.get("content_revision") or "")
                != content_revision
            ):
                return None
            expected_revision = cls.reopen_revision_for(
                candidate_id,
                content_revision,
                reopen_of_closure_revision,
            )
            expected_status = "observed"
            expected_lifecycle = "active"
        else:
            return None
        binding = cls._projection_binding(payload)
        expected_event_id = cls.projection_event_id_for(binding)
        expected_goal_ref = {
            "ref_id": goal_id,
            "kind": ProjectGuardianEvaluator.GOAL_KIND,
            "revision": goal_revision,
        }
        goal_refs = payload.get("goal_refs") if isinstance(payload.get("goal_refs"), list) else []
        observations = payload.get("observation")
        evidence = event.evidence_refs if isinstance(event.evidence_refs, list) else []
        payload_evidence = (
            payload.get("evidence_refs")
            if isinstance(payload.get("evidence_refs"), list)
            else []
        )
        expected_observation = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key not in {"observation", "goal_refs", "salience_components"}
        }
        expected_subject = [
            {
                "subject_type": "goal",
                "ref_id": goal_id,
            },
            {
                "subject_type": "project",
                "ref_id": str(scope.get("workspace_id") or ""),
            },
        ]
        expected_salience = {
            "goal_relevance": 1.0,
            "evidence_diversity": round(
                len(payload.get("signal_kinds") or [])
                / len(ProjectGuardianEvaluator.SIGNAL_COMPONENTS),
                4,
            ),
        }
        expected_time = str(
            payload.get("transitioned_at")
            or payload.get("qualified_at")
            or ""
        )
        if (
            not candidate_id
            or candidate_id != expected_candidate_id
            or not all(
                str(scope.get(key) or "")
                for key in ProjectGuardianEvaluator.SCOPE_FIELDS
            )
            or str(payload.get("candidate_revision") or "") != expected_revision
            or not isinstance(payload.get("projection_sequence"), int)
            or isinstance(payload.get("projection_sequence"), bool)
            or payload.get("projection_sequence", 0) < 1
            or not isinstance(payload.get("projection_attempt"), int)
            or isinstance(payload.get("projection_attempt"), bool)
            or payload.get("projection_attempt", -1) < 0
            or not isinstance(payload.get("guardian_mode_epoch"), int)
            or isinstance(payload.get("guardian_mode_epoch"), bool)
            or payload.get("guardian_mode_epoch", -1) < 0
            or not isinstance(payload.get("event_fabric_mode_epoch"), int)
            or isinstance(payload.get("event_fabric_mode_epoch"), bool)
            or payload.get("event_fabric_mode_epoch", -1) < 0
            or event.event_id != expected_event_id
            or str(event.correlation_id or "")
            != cls.correlation_id_for(candidate_id)
            or str(event.source.user_id or "") != user_id
            or str(event.source.session_id or "")
            != cls.session_id_for(user_id, candidate_id)
            or str(payload.get("situation_id") or "")
            != cls.situation_id_for(candidate_id)
            or str(payload.get("situation_status") or "") != expected_status
            or str(payload.get("lifecycle_state") or "") != expected_lifecycle
            or goal_refs != [expected_goal_ref]
            or payload.get("salience_components") != expected_salience
            or payload.get("agent_invoked") is not False
            or payload.get("shadow_only") is not True
            or payload.get("notification_allowed") is not False
            or payload.get("execution_allowed") is not False
            or payload.get("interrupt_eligible") is not False
            or str(payload.get("analysis_mode") or "")
            != "deterministic_read_only"
            or str(payload.get("record_kind") or "")
            != "situation_candidate"
            or str(event.timestamp or "") != expected_time
            or str(event.occurred_at or "") != expected_time
            or not isinstance(observations, dict)
            or observations != expected_observation
            or evidence != payload_evidence
            or any(
                not isinstance(ref, dict) or ref.get("is_fact") is not False
                for ref in evidence
            )
            or event.dedupe_key
            != f"project_guardian_projection:{expected_event_id}"
        ):
            return None
        subject = event.subject if isinstance(event.subject, list) else []
        if subject != expected_subject:
            return None
        return binding

    @classmethod
    def record_projection_result(
        cls,
        state_store: WorldStateStore,
        event: VeyraEvent,
        *,
        status: str,
        situation_id: str | None = None,
    ) -> None:
        binding = cls.validate_projection_event(event)
        if binding is None:
            return
        selected_status = str(status or "")
        if selected_status not in {
            "admitted",
            "projected",
            "suppressed",
            "superseded",
            "failed",
        }:
            return
        record = cls._projection_record(
            event,
            status=selected_status,
            situation_id=situation_id,
        )

        def update(state: dict[str, Any]) -> None:
            if state.get("_state_corrupt") is True:
                return
            candidates = (
                state.get("candidates")
                if isinstance(state.get("candidates"), list)
                else []
            )
            for candidate in candidates:
                if (
                    not isinstance(candidate, dict)
                    or str(candidate.get("candidate_id") or "")
                    != binding["candidate_id"]
                ):
                    continue
                history = cls._projection_history(candidate)
                history = cls._merge_projection_records(history, [record])
                candidate["projection_history"] = history
                lifecycle_head = cls._select_lifecycle_head(
                    candidate,
                    [record],
                )
                if lifecycle_head:
                    candidate["lifecycle_head"] = lifecycle_head
                else:
                    candidate.pop("lifecycle_head", None)
                closures = [
                    item
                    for item in history
                    if str(item.get("projection_kind") or "") == "closure"
                ]
                if closures:
                    candidate["closure_status"] = str(
                        closures[-1].get("status") or ""
                    )
                if binding["projection_kind"] == "reopen":
                    if (
                        str(candidate.get("candidate_revision") or "")
                        == str(event.payload.get("content_revision") or "")
                    ):
                        candidate["disposition"] = selected_status
                elif (
                    binding["projection_kind"] == "candidate"
                    and
                    str(candidate.get("candidate_revision") or "")
                    == binding["candidate_revision"]
                ):
                    candidate["disposition"] = selected_status
                latest = cls._latest_lifecycle_projection(candidate)
                if str(latest.get("status") or "") == "projected":
                    if str(latest.get("projection_kind") or "") == "closure":
                        candidate["situation_status"] = "closed"
                        if str(candidate.get("disposition") or "") == "inactive":
                            candidate["closure_status"] = "projected"
                    else:
                        candidate["situation_status"] = "observed"
                        if (
                            str(latest.get("projection_kind") or "")
                            == "reopen"
                        ):
                            candidate["closure_status"] = "reopened"

        state_store.mutate_json(cls.STATE_FILE, update)

    def _persist_run(
        self,
        result: dict[str, Any],
        *,
        candidates: list[dict[str, Any]],
        active_ids: set[str],
        evicted_candidate_ids: set[str] | None = None,
    ) -> None:
        now = utc_now_iso()
        capacity_evictions = {
            str(candidate_id)
            for candidate_id in (evicted_candidate_ids or set())
            if str(candidate_id)
        }
        compact_run = {
            key: copy.deepcopy(result.get(key))
            for key in (
                "status",
                "mode",
                "reason",
                "candidate_count",
                "would_publish_count",
                "published_count",
                "projected_count",
                "deduplicated_count",
                "failed_count",
                "closure_count",
                "capacity_skipped_count",
                "evaluation",
            )
            if key in result
        }
        compact_run["recorded_at"] = now

        def update(state: dict[str, Any]) -> None:
            if state.get("_state_corrupt") is True:
                raise RuntimeError(
                    "project guardian state is corrupt; persistence is frozen"
                )
            existing = (
                state.get("candidates")
                if isinstance(state.get("candidates"), list)
                else []
            )
            by_id = {
                str(item.get("candidate_id") or ""): copy.deepcopy(item)
                for item in existing
                if isinstance(item, dict) and item.get("candidate_id")
            }
            for candidate_id in capacity_evictions:
                by_id.pop(candidate_id, None)
            for raw in candidates:
                candidate = copy.deepcopy(raw)
                projection_record = candidate.pop("_projection_record", None)
                candidate_id = str(candidate.get("candidate_id") or "")
                if not candidate_id:
                    continue
                previous = by_id.get(candidate_id, {})
                history = self._projection_history(previous)
                if isinstance(projection_record, dict):
                    history = self._merge_projection_records(
                        history,
                        [projection_record],
                    )
                candidate["projection_history"] = history
                lifecycle_head = self._select_lifecycle_head(
                    previous,
                    [projection_record]
                    if isinstance(projection_record, dict)
                    else [],
                )
                if lifecycle_head:
                    candidate["lifecycle_head"] = lifecycle_head
                else:
                    candidate.pop("lifecycle_head", None)
                candidate["first_evaluated_at"] = str(
                    previous.get("first_evaluated_at")
                    or candidate.get("evaluated_at")
                    or now
                )
                candidate["last_evaluated_at"] = str(
                    candidate.get("evaluated_at") or now
                )
                if candidate_id in active_ids:
                    candidate.pop("inactive_at", None)
                    candidate.pop("inactive_reason", None)
                by_id[candidate_id] = candidate
            for candidate_id, candidate in by_id.items():
                if candidate_id in active_ids:
                    continue
                closure_status = self._latest_projection_status(
                    candidate,
                    projection_kind="closure",
                )
                if str(candidate.get("disposition") or "") != "inactive":
                    candidate["previous_disposition"] = str(
                        candidate.get("disposition") or "unknown"
                    )
                candidate["disposition"] = "inactive"
                candidate["inactive_reason"] = "no_longer_qualified"
                candidate.setdefault("inactive_at", now)
                candidate["closure_status"] = closure_status or str(
                    candidate.get("closure_status") or "not_required"
                )
                if closure_status == "projected":
                    candidate["situation_status"] = "closed"
                candidate["last_evaluated_at"] = now
            selected = list(by_id.values())
            if len(selected) > self.MAX_CANDIDATES:
                raise RuntimeError(
                    "candidate capacity admission failed to preserve bounded state"
                )
            selected.sort(key=lambda item: str(item.get("last_evaluated_at") or ""))
            runs = state.get("runs") if isinstance(state.get("runs"), list) else []
            runs.append(compact_run)
            state.update(
                {
                    "schema_version": "veyra.project_guardian_state.v1",
                    "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
                    "candidates": selected,
                    "runs": runs[-self.MAX_RUNS :],
                    "last_run": compact_run,
                    "updated_at": now,
                }
            )

        self.state_store.mutate_json(self.STATE_FILE, update)

    def _candidate_state(self) -> dict[str, dict[str, Any]]:
        candidates = self.state_store.read_json(self.STATE_FILE).get("candidates")
        if not isinstance(candidates, list):
            return {}
        return {
            str(candidate.get("candidate_id") or ""): copy.deepcopy(candidate)
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        }

    def _candidate_capacity_admission(
        self,
        *,
        previous: dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]],
        active_ids: set[str],
    ) -> dict[str, Any]:
        retained_ids = set(previous)
        evictable = [
            candidate_id
            for candidate_id, candidate in previous.items()
            if candidate_id not in active_ids
            and self._candidate_telemetry_is_evictable(candidate)
        ]
        evictable.sort(
            key=lambda candidate_id: (
                str(
                    previous[candidate_id].get("inactive_at")
                    or previous[candidate_id].get("last_evaluated_at")
                    or ""
                ),
                candidate_id,
            )
        )
        evicted: list[str] = []
        while len(retained_ids) > self.MAX_CANDIDATES and evictable:
            candidate_id = evictable.pop(0)
            if candidate_id in retained_ids:
                retained_ids.remove(candidate_id)
                evicted.append(candidate_id)
        if len(retained_ids) > self.MAX_CANDIDATES:
            return {
                "frozen": True,
                "candidates": [],
                "evicted_candidate_ids": [],
                "skipped_candidate_ids": [
                    str(candidate.get("candidate_id") or "")
                    for candidate in candidates
                    if candidate.get("candidate_id")
                ],
            }

        admitted: list[dict[str, Any]] = []
        skipped: list[str] = []
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "")
            if not candidate_id:
                continue
            if candidate_id in retained_ids:
                admitted.append(candidate)
                continue
            if len(retained_ids) >= self.MAX_CANDIDATES:
                while evictable:
                    evicted_id = evictable.pop(0)
                    if evicted_id not in retained_ids:
                        continue
                    retained_ids.remove(evicted_id)
                    evicted.append(evicted_id)
                    break
            if len(retained_ids) >= self.MAX_CANDIDATES:
                skipped.append(candidate_id)
                continue
            retained_ids.add(candidate_id)
            admitted.append(candidate)
        return {
            "frozen": False,
            "candidates": admitted,
            "evicted_candidate_ids": evicted,
            "skipped_candidate_ids": skipped,
        }

    @classmethod
    def _candidate_telemetry_is_evictable(
        cls,
        candidate: dict[str, Any],
    ) -> bool:
        if str(candidate.get("disposition") or "") != "inactive":
            return False
        latest = cls._latest_lifecycle_projection(candidate)
        if not latest:
            return True
        return (
            str(latest.get("projection_kind") or "") == "closure"
            and str(latest.get("status") or "") == "projected"
        )

    def _delivery_status(self, event_id: str) -> str:
        record = self.state_store.read_json("event_inbox.json").get("events", {}).get(
            str(event_id)
        )
        if not isinstance(record, dict):
            return "retry"
        delivery = self._record_delivery_status(record)
        if delivery != "projected":
            return delivery
        envelope = (
            record.get("envelope")
            if isinstance(record.get("envelope"), dict)
            else {}
        )
        try:
            event = VeyraEvent.from_dict(envelope)
        except Exception:
            return "retry"
        binding = self.validate_projection_event(event)
        if binding is None:
            return "retry"
        return (
            "projected"
            if self._situation_projection(
                candidate_id=binding["candidate_id"],
                candidate_revision=binding["candidate_revision"],
            )
            is not None
            else "retry"
        )

    @staticmethod
    def _record_delivery_status(record: dict[str, Any]) -> str:
        status = str(record.get("status") or "")
        completion = (
            record.get("completion_result")
            if isinstance(record.get("completion_result"), dict)
            else {}
        )
        completed_status = str(completion.get("status") or "")
        if status == "completed" and completed_status == "observed":
            return "projected"
        if status == "completed" and completed_status in {
            "suppressed",
            "superseded",
        }:
            return completed_status
        if status == "failed":
            return "failed"
        if status in {"pending", "claimed"}:
            return "admitted"
        return "retry"

    def _situation_projection(
        self,
        *,
        candidate_id: str,
        candidate_revision: str,
    ) -> dict[str, Any] | None:
        situation_id = self.situation_id_for(candidate_id)
        observation_id = f"{candidate_id}:{candidate_revision}"
        state = self.state_store.read_json("situation_state.json")
        raw_situations = state.get("situations")
        situations = (
            list(raw_situations.values())
            if isinstance(raw_situations, dict)
            else raw_situations
            if isinstance(raw_situations, list)
            else []
        )
        for situation in situations:
            if (
                not isinstance(situation, dict)
                or str(situation.get("situation_id") or "")
                != situation_id
            ):
                continue
            if str(situation.get("observation_id") or "") == observation_id:
                return {
                    "situation_id": situation_id,
                    "observation_id": observation_id,
                    "projection_sequence": self._nonnegative_int(
                        situation.get("observation_sequence")
                    ),
                    "event_id": str(
                        situation.get("source_event_id") or ""
                    ),
                }
            observations = (
                situation.get("observations")
                if isinstance(situation.get("observations"), list)
                else []
            )
            for observation in observations:
                if (
                    not isinstance(observation, dict)
                    or str(observation.get("observation_id") or "")
                    != observation_id
                ):
                    continue
                return {
                    "situation_id": situation_id,
                    "observation_id": observation_id,
                    "projection_sequence": self._nonnegative_int(
                        observation.get("observation_sequence")
                    ),
                    "event_id": str(
                        observation.get("source_event_id") or ""
                    ),
                }
        return None

    @classmethod
    def _projection_record(
        cls,
        event: VeyraEvent,
        *,
        status: str,
        situation_id: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        payload = event.payload
        record = {
            "candidate_revision": str(payload.get("candidate_revision") or ""),
            "content_revision": str(
                payload.get("content_revision")
                or payload.get("candidate_revision")
                or ""
            ),
            "projection_kind": str(payload.get("projection_kind") or ""),
            "projection_sequence": cls._nonnegative_int(
                payload.get("projection_sequence")
            ),
            "projection_attempt": cls._nonnegative_int(
                payload.get("projection_attempt")
            ),
            "event_id": event.event_id,
            "status": str(status),
        }
        for key in (
            "closure_of_revision",
            "reopen_of_closure_revision",
        ):
            if payload.get(key):
                record[key] = str(payload[key])
        if situation_id:
            record["situation_id"] = str(situation_id)
        if error_type:
            record["error_type"] = str(error_type)
        return record

    @classmethod
    def _lifecycle_head(
        cls,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        raw = (
            candidate.get("lifecycle_head")
            if isinstance(candidate.get("lifecycle_head"), dict)
            else {}
        )
        if (
            str(raw.get("projection_kind") or "")
            not in cls.LIFECYCLE_PROJECTION_KINDS
            or str(raw.get("status") or "")
            not in cls.LIVE_PROJECTION_STATUSES
            or not str(raw.get("candidate_revision") or "")
            or not str(raw.get("event_id") or "")
        ):
            return {}
        return copy.deepcopy(raw)

    @classmethod
    def _select_lifecycle_head(
        cls,
        candidate: dict[str, Any],
        additions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        invalidated_event_ids = {
            str(item.get("event_id") or "")
            for item in additions
            if isinstance(item, dict)
            and str(item.get("event_id") or "")
            and str(item.get("status") or "")
            not in cls.LIVE_PROJECTION_STATUSES
        }
        records = cls._projection_history(candidate)
        current = cls._lifecycle_head(candidate)
        if (
            current
            and str(current.get("event_id") or "")
            not in invalidated_event_ids
        ):
            records.append(current)
        records.extend(
            copy.deepcopy(item)
            for item in additions
            if isinstance(item, dict)
        )
        by_event: dict[str, dict[str, Any]] = {}
        for item in records:
            event_id = str(item.get("event_id") or "")
            if not event_id or event_id in invalidated_event_ids:
                continue
            existing = by_event.get(event_id)
            if (
                existing is None
                or cls._lifecycle_projection_order(item)
                > cls._lifecycle_projection_order(existing)
            ):
                by_event[event_id] = item
        selected = [
            item
            for item in by_event.values()
            if str(item.get("projection_kind") or "")
            in cls.LIFECYCLE_PROJECTION_KINDS
            and str(item.get("status") or "")
            in cls.LIVE_PROJECTION_STATUSES
        ]
        if not selected:
            return {}
        selected.sort(key=cls._lifecycle_projection_order)
        return copy.deepcopy(selected[-1])

    @classmethod
    def _lifecycle_projection_order(
        cls,
        item: dict[str, Any],
    ) -> tuple[int, int, int, str]:
        return (
            cls._nonnegative_int(item.get("projection_sequence")),
            1 if str(item.get("status") or "") == "projected" else 0,
            cls._nonnegative_int(item.get("projection_attempt")),
            str(item.get("event_id") or ""),
        )

    @classmethod
    def _projection_history(cls, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        history = (
            candidate.get("projection_history")
            if isinstance(candidate.get("projection_history"), list)
            else []
        )
        return [
            copy.deepcopy(item)
            for item in history
            if isinstance(item, dict) and item.get("event_id")
        ][-cls.MAX_PROJECTION_HISTORY :]

    @classmethod
    def _merge_projection_records(
        cls,
        existing: list[dict[str, Any]],
        additions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_event = {
            str(item.get("event_id") or ""): copy.deepcopy(item)
            for item in existing
            if item.get("event_id")
        }
        for item in additions:
            event_id = str(item.get("event_id") or "")
            if event_id:
                by_event[event_id] = copy.deepcopy(item)
        selected = list(by_event.values())
        selected.sort(
            key=lambda item: (
                cls._nonnegative_int(item.get("projection_sequence")),
                cls._nonnegative_int(item.get("projection_attempt")),
                str(item.get("event_id") or ""),
            )
        )
        return selected[-cls.MAX_PROJECTION_HISTORY :]

    @classmethod
    def _latest_projection_status(
        cls,
        candidate: dict[str, Any],
        *,
        projection_kind: str,
    ) -> str:
        lifecycle_head = cls._lifecycle_head(candidate)
        if (
            str(lifecycle_head.get("projection_kind") or "")
            == projection_kind
        ):
            return str(lifecycle_head.get("status") or "")
        selected = [
            item
            for item in cls._projection_history(candidate)
            if str(item.get("projection_kind") or "") == projection_kind
        ]
        if not selected:
            return ""
        return str(selected[-1].get("status") or "")

    @classmethod
    def _has_candidate_projection(cls, candidate: dict[str, Any]) -> bool:
        latest = cls._latest_lifecycle_projection(candidate)
        return (
            str(latest.get("projection_kind") or "")
            in {"candidate", "reopen"}
            and str(latest.get("status") or "")
            in cls.LIVE_PROJECTION_STATUSES
        )

    @classmethod
    def _active_projection_candidate(
        cls,
        candidate: dict[str, Any],
        previous: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        latest = cls._latest_lifecycle_projection(previous)
        latest_kind = str(latest.get("projection_kind") or "")
        selected = cls._event_candidate(candidate)
        content_revision = str(selected.get("candidate_revision") or "")
        if (
            latest_kind == "reopen"
            and str(latest.get("content_revision") or "")
            == content_revision
            and str(latest.get("reopen_of_closure_revision") or "")
        ):
            selected.update(
                content_revision=content_revision,
                reopen_of_closure_revision=str(
                    latest["reopen_of_closure_revision"]
                ),
                candidate_revision=str(latest["candidate_revision"]),
            )
            return selected, "reopen"
        if latest_kind != "closure":
            return copy.deepcopy(candidate), "candidate"
        closure_revision = str(latest.get("candidate_revision") or "")
        selected.update(
            content_revision=content_revision,
            reopen_of_closure_revision=closure_revision,
            candidate_revision=cls.reopen_revision_for(
                str(selected.get("candidate_id") or ""),
                content_revision,
                closure_revision,
            ),
        )
        return selected, "reopen"

    @classmethod
    def _latest_lifecycle_projection(
        cls,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        lifecycle_head = cls._lifecycle_head(candidate)
        if lifecycle_head:
            return lifecycle_head
        selected = [
            item
            for item in cls._projection_history(candidate)
            if str(item.get("projection_kind") or "")
            in cls.LIFECYCLE_PROJECTION_KINDS
            and str(item.get("status") or "")
            in cls.LIVE_PROJECTION_STATUSES
        ]
        if not selected:
            return {}
        selected.sort(key=cls._lifecycle_projection_order)
        return copy.deepcopy(selected[-1])

    @classmethod
    def _closure_candidate(
        cls,
        candidate: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        selected = cls._event_candidate(candidate)
        content_revision = str(selected.get("candidate_revision") or "")
        latest_active = cls._latest_lifecycle_projection(candidate)
        closure_of = str(
            latest_active.get("candidate_revision")
            or content_revision
        )
        selected.update(
            content_revision=content_revision,
            closure_of_revision=closure_of,
            candidate_revision=cls.closure_revision_for(
                str(selected.get("candidate_id") or ""),
                closure_of,
            ),
            transitioned_at=now.astimezone(timezone.utc).isoformat(),
            evaluated_at=now.astimezone(timezone.utc).isoformat(),
        )
        return selected

    @classmethod
    def _event_candidate(cls, candidate: dict[str, Any]) -> dict[str, Any]:
        excluded = {
            "disposition",
            "replay_status",
            "blocked_reason",
            "projection_event_id",
            "projection_history",
            "lifecycle_head",
            "first_evaluated_at",
            "last_evaluated_at",
            "inactive_at",
            "inactive_reason",
            "previous_disposition",
            "closure_disposition",
            "closure_status",
            "situation_status",
        }
        selected = {
            key: copy.deepcopy(value)
            for key, value in candidate.items()
            if key not in excluded and not key.startswith("_")
        }
        selected.pop("evaluated_at", None)
        return selected

    @classmethod
    def closure_revision_for(
        cls,
        candidate_id: str,
        closure_of_revision: str,
    ) -> str:
        return (
            "pgrc_"
            + cls._digest(
                {
                    "candidate_id": str(candidate_id),
                    "closure_of_revision": str(closure_of_revision),
                    "lifecycle_state": "inactive",
                }
            )[:20]
        )

    @classmethod
    def reopen_revision_for(
        cls,
        candidate_id: str,
        content_revision: str,
        reopen_of_closure_revision: str,
    ) -> str:
        return (
            "pgrr_"
            + cls._digest(
                {
                    "candidate_id": str(candidate_id),
                    "content_revision": str(content_revision),
                    "reopen_of_closure_revision": str(
                        reopen_of_closure_revision
                    ),
                    "lifecycle_state": "active",
                }
            )[:20]
        )

    @classmethod
    def _projection_binding(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": str(payload.get("candidate_id") or ""),
            "candidate_revision": str(payload.get("candidate_revision") or ""),
            "projection_kind": str(payload.get("projection_kind") or ""),
            "projection_sequence": max(
                1,
                cls._nonnegative_int(payload.get("projection_sequence")),
            ),
            "projection_attempt": cls._nonnegative_int(
                payload.get("projection_attempt")
            ),
            "guardian_mode_epoch": cls._nonnegative_int(
                payload.get("guardian_mode_epoch")
            ),
            "event_fabric_mode_epoch": cls._nonnegative_int(
                payload.get("event_fabric_mode_epoch")
            ),
            "transitioned_at": str(
                payload.get("transitioned_at")
                or payload.get("qualified_at")
                or ""
            ),
        }

    @classmethod
    def projection_event_id_for(cls, binding: dict[str, Any]) -> str:
        return f"evt_pg_{cls._digest(binding)[:20]}"

    @classmethod
    def situation_id_for(cls, candidate_id: str) -> str:
        return f"sit_pg_{hashlib.sha256(str(candidate_id).encode('utf-8')).hexdigest()[:20]}"

    @classmethod
    def correlation_id_for(cls, candidate_id: str) -> str:
        return f"corr_pg_{hashlib.sha256(str(candidate_id).encode('utf-8')).hexdigest()[:20]}"

    @classmethod
    def session_id_for(cls, user_id: str, candidate_id: str) -> str:
        digest = hashlib.sha256(
            f"{user_id}\0{candidate_id}".encode("utf-8")
        ).hexdigest()[:16]
        return f"pg_{digest}"

    def _guardian_snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        section = config.get("project_guardian") if isinstance(config, dict) else {}
        section = section if isinstance(section, dict) else {}
        return {
            "mode": self._mode_value(section.get("mode"), default="disabled"),
            "mode_epoch": self._nonnegative_int(section.get("mode_epoch")),
        }

    def _fabric_snapshot(self) -> dict[str, Any]:
        try:
            raw = self.event_fabric_mode()
        except Exception:
            raw = "unavailable"
        callback_mode = (
            self._mode_value(raw.get("mode"), default="unavailable")
            if isinstance(raw, dict)
            else self._mode_value(raw, default="unavailable")
        )
        config = self.state_store.read_json("ops_config.json")
        section = config.get("event_awareness") if isinstance(config, dict) else {}
        section = section if isinstance(section, dict) else {}
        configured_mode = self._mode_value(
            section.get("mode"),
            default=callback_mode,
        )
        configured_epoch = self._nonnegative_int(
            section.get("mode_epoch")
        )
        if isinstance(raw, dict):
            # A dict callback is an in-process cache/health view. The durable
            # config remains authoritative across runtime instances/processes.
            return {
                "mode": configured_mode,
                "mode_epoch": configured_epoch,
            }
        return {
            "mode": callback_mode,
            "mode_epoch": configured_epoch,
        }

    @classmethod
    def _mode_value(cls, value: Any, *, default: str) -> str:
        selected = str(value or "").strip().lower()
        return selected if selected in cls.MODES else default

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _add_counts(target: dict[str, int], source: dict[str, Any]) -> None:
        for key in (
            "published_count",
            "projected_count",
            "deduplicated_count",
            "failed_count",
        ):
            target[key] += int(source.get(key) or 0)

    @staticmethod
    def _result(
        *,
        status: str,
        mode: str,
        reason: str,
        evaluation: dict[str, Any],
        candidate_count: int,
        would_publish_count: int,
        published_count: int = 0,
        projected_count: int = 0,
        deduplicated_count: int = 0,
        failed_count: int = 0,
        closure_count: int = 0,
        capacity_skipped_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "mode": mode,
            "reason": reason,
            "candidate_count": candidate_count,
            "would_publish_count": would_publish_count,
            "published_count": published_count,
            "projected_count": projected_count,
            "deduplicated_count": deduplicated_count,
            "failed_count": failed_count,
            "closure_count": closure_count,
            "capacity_skipped_count": capacity_skipped_count,
            "evaluation": {
                key: copy.deepcopy(evaluation.get(key))
                for key in (
                    "schema_version",
                    "ruleset_version",
                    "status",
                    "evaluated_at",
                    "active_goal_count",
                    "accepted_signal_count",
                    "candidate_count",
                    "diagnostics",
                )
                if key in evaluation
            },
        }
