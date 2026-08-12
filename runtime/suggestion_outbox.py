from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.general_situation_contract import stable_digest
from memory_bridge.scope import framed_sha256, normalize_scope_component


class SuggestionOutboxConflict(RuntimeError):
    pass


class SuggestionOutbox:
    """Persist informational proposals without notification or action authority."""

    STATE_FILE = "suggestion_outbox.json"
    SCHEMA_VERSION = "veyra.suggestion_outbox.v1"
    PROPOSAL_SCHEMA_VERSION = "veyra.informational_suggestion.v2"
    MODES = {"disabled", "record_only", "shadow", "advise_only"}
    DEFAULT_MODE = "record_only"
    ATTENTION_HYPOTHESIS_SURFACE_SCHEMA_VERSION = (
        "veyra.attention_hypothesis_surface.v1"
    )
    ATTENTION_HYPOTHESIS_RULESET_VERSION = (
        "veyra.attention_hypothesis.rules.v1"
    )
    ATTENTION_READINESS_SEMANTICS = (
        "attention_policy_readiness_not_factual_probability"
    )
    INTERACTION_DECISIONS = frozenset({"say", "ask", "wait", "silent"})
    DELIVERY_DISPOSITIONS = frozenset(
        {"none", "owner_scoped_console", "suppressed"}
    )
    MAX_PROPOSALS = 2000
    MAX_INTERACTION_DECISIONS = 4000
    DECISION_SCHEMA_VERSION = "veyra.interaction_decision.v1"
    MAX_INBOX_ITEMS = 100
    DEFAULT_POLICY = {
        "sandbox_enabled": False,
        "daily_budget": 1,
        "timezone": "UTC",
        "quiet_hours": None,
        "cooldown_seconds": 3600,
        "dismiss_cooldown_seconds": 86400,
    }
    _V2_PROPOSAL_BASE_KEYS = frozenset(
        {
            "schema_version",
            "proposal_id",
            "proposal_kind",
            "user_id",
            "session_id",
            "general_situation_id",
            "parent_revision",
            "mode",
            "status",
            "reason",
            "decision_disposition",
            "delivery_disposition",
            "why_now",
            "score",
            "upstream_scorer_version",
            "evidence",
            "unknowns",
            "attention_readiness",
            "evidence_diversity",
            "assessment_binding",
            "source_expires_at",
            "options",
            "delivery",
            "authority",
            "created_at",
            "updated_at",
            "attention_hypothesis_ref",
            "proposal_revision",
        }
    )
    _PUBLIC_PROPOSAL_KEYS = (
        "schema_version",
        "proposal_id",
        "proposal_kind",
        "general_situation_id",
        "parent_revision",
        "mode",
        "status",
        "reason",
        "decision_disposition",
        "delivery_disposition",
        "why_now",
        "score",
        "upstream_scorer_version",
        "evidence",
        "unknowns",
        "attention_readiness",
        "evidence_diversity",
        "assessment_binding",
        "source_expires_at",
        "options",
        "delivery",
        "authority",
        "created_at",
        "updated_at",
        "attention_hypothesis_ref",
        "proposal_revision",
        "acknowledged_at",
        "dismissed_at",
    )

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def configure_mode(
        self,
        mode: str,
        *,
        expected_state_revision: int,
    ) -> dict[str, Any]:
        if not self._healthy(self.state_store.read_json(self.STATE_FILE)):
            return self._closed("suggestion_outbox_state_corrupt")
        selected = str(mode or "").strip().lower()
        if selected not in self.MODES:
            raise ValueError(f"mode must be one of {sorted(self.MODES)}")
        self._required_revision(expected_state_revision)
        result: dict[str, Any] = {}

        def mutate(config: dict[str, Any]) -> dict[str, Any]:
            if config.get("_state_corrupt") is True:
                raise SuggestionOutboxConflict("ops_config state is corrupt")
            current_revision = self._nonnegative_int(config.get("_state_revision"))
            if current_revision != expected_state_revision:
                raise SuggestionOutboxConflict(
                    "expected_state_revision does not match ops_config"
                )
            section = (
                config.get("general_suggestions")
                if isinstance(config.get("general_suggestions"), dict)
                else {}
            )
            previous = str(section.get("mode") or self.DEFAULT_MODE).strip().lower()
            if previous not in self.MODES:
                previous = self.DEFAULT_MODE
            epoch = self._nonnegative_int(section.get("mode_epoch"))
            config["general_suggestions"] = {
                **copy.deepcopy(section),
                "mode": selected,
                "mode_epoch": epoch + int(previous != selected),
                "allowed_modes": sorted(self.MODES),
            }
            result.update(
                previous_mode=previous,
                mode=selected,
                mode_epoch=epoch + int(previous != selected),
            )
            return config

        persisted = self.state_store.mutate_json("ops_config.json", mutate)
        return {
            "status": "updated",
            **result,
            "state_revision": self._nonnegative_int(
                persisted.get("_state_revision")
            ),
            "authority": self._authority_boundary(),
        }

    def configure_policy(
        self,
        *,
        user_id: str,
        session_id: str,
        sandbox_enabled: bool = False,
        daily_budget: int,
        quiet_hours: dict[str, Any] | None,
        timezone: str | None = None,
        cooldown_seconds: int,
        dismiss_cooldown_seconds: int,
        expected_state_revision: int,
    ) -> dict[str, Any]:
        user, session, scope_key = self._owner(user_id, session_id)
        self._required_revision(expected_state_revision)
        if not isinstance(sandbox_enabled, bool):
            raise ValueError("sandbox_enabled must be boolean")
        if (
            isinstance(daily_budget, bool)
            or not isinstance(daily_budget, int)
            or not 0 <= daily_budget <= 1
        ):
            raise ValueError("daily_budget must be 0 or 1 in the sandbox")
        for name, value in (
            ("cooldown_seconds", cooldown_seconds),
            ("dismiss_cooldown_seconds", dismiss_cooldown_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30 * 86400:
                raise ValueError(f"{name} must be an integer from 0 to 2592000")
        selected_quiet = self._quiet_hours(quiet_hours)
        timezone_name = str(
            timezone
            or (
                selected_quiet.get("timezone")
                if isinstance(selected_quiet, dict)
                else "UTC"
            )
        ).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("suggestion policy timezone is unknown") from exc
        if (
            isinstance(selected_quiet, dict)
            and selected_quiet.get("timezone") != timezone_name
        ):
            raise ValueError("quiet hours and daily budget timezone must match")
        policy = {
            "user_id": user,
            "session_id": session,
            "sandbox_enabled": sandbox_enabled,
            "daily_budget": daily_budget,
            "timezone": timezone_name,
            "quiet_hours": selected_quiet,
            "cooldown_seconds": cooldown_seconds,
            "dismiss_cooldown_seconds": dismiss_cooldown_seconds,
            "updated_at": self._now().isoformat(),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._require_healthy(state)
            current_revision = self._nonnegative_int(state.get("_state_revision"))
            if current_revision != expected_state_revision:
                raise SuggestionOutboxConflict(
                    "expected_state_revision does not match suggestion outbox"
                )
            policies = self._records(state.get("policies"))
            other_owner_timezones = {
                self._policy_timezone(item)
                for key, item in policies.items()
                if key != scope_key
                and isinstance(item, dict)
                and item.get("user_id") == user
            }
            if any(item != timezone_name for item in other_owner_timezones):
                raise SuggestionOutboxConflict(
                    "owner daily budget timezone conflicts with another session"
                )
            previous_policy = policies.get(scope_key)
            if (
                isinstance(previous_policy, dict)
                and self._policy_timezone(previous_policy) != timezone_name
            ):
                owner_key = self._owner_budget_key(user)
                counters = self._records(state.get("daily_counters"))
                if any(
                    key.startswith(f"{owner_key}:")
                    for key in counters
                ):
                    raise SuggestionOutboxConflict(
                        "owner daily budget timezone is frozen after first use"
                    )
            policies[scope_key] = copy.deepcopy(policy)
            state["schema_version"] = self.SCHEMA_VERSION
            state["policies"] = policies
            state["policy_count"] = len(policies)
            state["updated_at"] = policy["updated_at"]
            return state

        persisted = self.state_store.mutate_json(self.STATE_FILE, mutate)
        return {
            "status": "updated",
            "policy": self._public_policy(policy),
            "state_revision": self._nonnegative_int(
                persisted.get("_state_revision")
            ),
            "authority": self._authority_boundary(),
        }

    def consider(
        self,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Record or surface one informational proposal according to mode."""

        try:
            user, session, scope_key = self._owner(user_id, session_id)
            self._validate_binding(
                general_situation,
                assessment,
                user_id=user,
                scope_key=scope_key,
            )
            attention_hypothesis_ref = (
                self._validated_attention_hypothesis_ref(assessment)
            )
        except (TypeError, ValueError) as exc:
            return self._closed("invalid_suggestion_binding", detail=type(exc).__name__)
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy(state):
            return self._closed("suggestion_outbox_state_corrupt")
        config = self._mode_snapshot()
        if config.get("status") == "fail_closed":
            return config
        mode = str(config["mode"])
        decision_disposition, decision_reason = self._interaction_decision(
            assessment
        )
        if decision_disposition != "say":
            return self._persist_decision_result(
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user,
                session_id=session,
                output={
                    "status": "not_proposed",
                    "mode": mode,
                    "reason": decision_reason,
                    "decision_disposition": decision_disposition,
                    "delivery_disposition": "none",
                    "proposal": None,
                },
                mode_epoch=config.get("mode_epoch"),
            )
        if mode == "disabled":
            return self._persist_decision_result(
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user,
                session_id=session,
                output={
                    "status": "disabled",
                    "mode": mode,
                    "reason": decision_reason,
                    "decision_disposition": decision_disposition,
                    "delivery_disposition": "none",
                    "proposal": None,
                },
                mode_epoch=config.get("mode_epoch"),
            )
        if assessment.get("eligible") is not True or assessment.get("status") != "eligible":
            return self._persist_decision_result(
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user,
                session_id=session,
                output={
                    "status": "not_proposed",
                    "mode": mode,
                    "reason": decision_reason,
                    "decision_disposition": decision_disposition,
                    "delivery_disposition": "none",
                    "proposal": None,
                },
                mode_epoch=config.get("mode_epoch"),
            )
        proposal_id = "sug_" + stable_digest(
            "veyra.informational_suggestion.identity.v2",
            {
                "schema_version": self.PROPOSAL_SCHEMA_VERSION,
                "general_situation_id": general_situation.get(
                    "general_situation_id"
                ),
                "parent_revision": general_situation.get("parent_revision"),
                "attention_hypothesis_ref": attention_hypothesis_ref,
                "scorer_version": assessment.get("scorer_version"),
                "upstream_scorer_version": assessment.get(
                    "upstream_scorer_version"
                ),
                "user_id": user,
                "session_id": session,
            },
        )[:24]
        result: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if not self._healthy(current):
                result = self._closed("suggestion_outbox_state_corrupt")
                return current
            committed_config = self._mode_snapshot()
            if committed_config.get("status") == "fail_closed":
                result = committed_config
                return current
            commit_now = self._now()
            if (
                committed_config.get("mode") != mode
                or committed_config.get("mode_epoch")
                != config.get("mode_epoch")
            ):
                attention_issue = self._current_attention_binding_issue(
                    general_situation=general_situation,
                    assessment=assessment,
                    attention_hypothesis_ref=attention_hypothesis_ref,
                    user_id=user,
                    scope_key=scope_key,
                    now=commit_now,
                )
                if attention_issue is not None:
                    result = self._closed(attention_issue)
                    return current
                decision_record = self._decision_record(
                    general_situation=general_situation,
                    assessment=assessment,
                    attention_hypothesis_ref=attention_hypothesis_ref,
                    user_id=user,
                    session_id=session,
                    mode=str(committed_config.get("mode") or mode),
                    mode_epoch=committed_config.get("mode_epoch"),
                    decision_disposition=decision_disposition,
                    delivery_disposition="suppressed",
                    reason="suggestion_mode_changed_before_commit",
                    proposal_id=None,
                    now=commit_now,
                )
                persisted_decision = self._record_decision_in_state(
                    current, decision_record
                )
                result = {
                    "status": (
                        "disabled"
                        if committed_config.get("mode") == "disabled"
                        else "suppressed"
                    ),
                    "mode": committed_config.get("mode"),
                    "reason": "suggestion_mode_changed_before_commit",
                    "decision_disposition": decision_disposition,
                    "delivery_disposition": "suppressed",
                    "proposal": None,
                    "interaction_decision": self._public_decision(
                        persisted_decision
                    ),
                    "authority": self._authority_boundary(),
                }
                return current
            # The writer transaction may have waited behind another commit.  All
            # freshness and budget decisions must therefore use a time sampled
            # after both the durable outbox read and the mode/epoch re-check.
            attention_issue = self._current_attention_binding_issue(
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user,
                scope_key=scope_key,
                now=commit_now,
            )
            if attention_issue is not None:
                result = self._closed(attention_issue)
                return current
            proposals = self._records(current.get("proposals"))
            if proposal_id in proposals:
                existing = proposals[proposal_id]
                if (
                    str(existing.get("schema_version") or "")
                    == "veyra.informational_suggestion.v1"
                ):
                    result = {
                        "status": "legacy_recorded",
                        "mode": mode,
                        "reason": "legacy_proposal_isolated_from_sandbox",
                        "proposal": None,
                        "interaction_decision": self._public_decision(
                            self._record_decision_in_state(
                                current,
                                self._decision_record(
                                    general_situation=general_situation,
                                    assessment=assessment,
                                    attention_hypothesis_ref=attention_hypothesis_ref,
                                    user_id=user,
                                    session_id=session,
                                    mode=mode,
                                    mode_epoch=config.get("mode_epoch"),
                                    decision_disposition=decision_disposition,
                                    delivery_disposition="none",
                                    reason="legacy_proposal_isolated_from_sandbox",
                                    proposal_id=proposal_id,
                                    now=commit_now,
                                ),
                            )
                        ),
                        "authority": self._authority_boundary(),
                    }
                    return current
                if not self._current_proposal_integrity(existing):
                    result = self._closed("suggestion_proposal_revision_corrupt")
                    return current
                result = {
                    "status": "replayed",
                    "mode": mode,
                    "decision_disposition": existing.get("decision_disposition"),
                    "delivery_disposition": existing.get("delivery_disposition"),
                    "reason": existing.get("reason"),
                    "proposal": self._public_proposal(existing),
                    "interaction_decision": self._public_decision(
                        self._record_decision_in_state(
                            current,
                            self._decision_record(
                                general_situation=general_situation,
                                assessment=assessment,
                                attention_hypothesis_ref=attention_hypothesis_ref,
                                user_id=user,
                                session_id=session,
                                mode=mode,
                                mode_epoch=config.get("mode_epoch"),
                                decision_disposition=str(
                                    existing.get("decision_disposition")
                                    or decision_disposition
                                ),
                                delivery_disposition=str(
                                    existing.get("delivery_disposition")
                                    or "none"
                                ),
                                reason=str(existing.get("reason") or decision_reason),
                                proposal_id=proposal_id,
                                now=commit_now,
                            ),
                        )
                    ),
                    "authority": self._authority_boundary(),
                }
                return current
            policies = self._records(current.get("policies"))
            policy = self._effective_policy(policies.get(scope_key))
            if mode == "advise_only":
                suppression = self._surface_suppression(
                    current,
                    general_situation_id=str(
                        general_situation.get("general_situation_id") or ""
                    ),
                    scope_key=scope_key,
                    user_id=user,
                    policy=policy,
                    now=commit_now,
                )
                if suppression is not None:
                    result = {
                        "status": "suppressed",
                        "mode": mode,
                        "reason": suppression,
                        "decision_disposition": decision_disposition,
                        "delivery_disposition": "suppressed",
                        "proposal": None,
                        "interaction_decision": self._public_decision(
                            self._record_decision_in_state(
                                current,
                                self._decision_record(
                                    general_situation=general_situation,
                                    assessment=assessment,
                                    attention_hypothesis_ref=attention_hypothesis_ref,
                                    user_id=user,
                                    session_id=session,
                                    mode=mode,
                                    mode_epoch=config.get("mode_epoch"),
                                    decision_disposition=decision_disposition,
                                    delivery_disposition="suppressed",
                                    reason=suppression,
                                    proposal_id=proposal_id,
                                    now=commit_now,
                                ),
                            )
                        ),
                        "authority": self._authority_boundary(),
                    }
                    return current

            proposal = self._proposal(
                proposal_id=proposal_id,
                mode=mode,
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user,
                session_id=session,
                now=commit_now,
            )
            proposals[proposal_id] = proposal
            if len(proposals) > self.MAX_PROPOSALS:
                result = self._closed("suggestion_outbox_capacity_exhausted")
                return current
            current["schema_version"] = self.SCHEMA_VERSION
            current["proposals"] = proposals
            current["proposal_count"] = len(proposals)
            current.setdefault("policies", policies)
            current.setdefault("feedback", {})
            current.setdefault("daily_counters", {})
            if mode == "advise_only":
                inboxes = self._list_records(current.get("owner_inboxes"))
                inbox = [
                    str(item)
                    for item in inboxes.get(scope_key, [])
                    if str(item) and str(item) != proposal_id
                ]
                inbox.append(proposal_id)
                inboxes[scope_key] = inbox[-self.MAX_INBOX_ITEMS :]
                current["owner_inboxes"] = inboxes
                counters = self._records(current.get("daily_counters"))
                day_key = self._day_key(commit_now, policy)
                owner_key = self._owner_budget_key(user)
                counter_key = f"{owner_key}:{day_key}"
                counter = counters.get(counter_key) or {
                    "owner_budget_key": owner_key,
                    "day": day_key,
                    "count": 0,
                }
                counter["count"] = self._nonnegative_int(counter.get("count")) + 1
                counter["updated_at"] = commit_now.isoformat()
                counters[counter_key] = counter
                current["daily_counters"] = counters
            current["updated_at"] = commit_now.isoformat()
            result = {
                "status": str(proposal.get("status") or "recorded"),
                "mode": mode,
                "decision_disposition": decision_disposition,
                "delivery_disposition": proposal.get("delivery_disposition"),
                "proposal": self._public_proposal(proposal),
                "interaction_decision": self._public_decision(
                    self._record_decision_in_state(
                        current,
                        self._decision_record(
                            general_situation=general_situation,
                            assessment=assessment,
                            attention_hypothesis_ref=attention_hypothesis_ref,
                            user_id=user,
                            session_id=session,
                            mode=mode,
                            mode_epoch=config.get("mode_epoch"),
                            decision_disposition=decision_disposition,
                            delivery_disposition=str(
                                proposal.get("delivery_disposition") or "none"
                            ),
                            reason=decision_reason,
                            proposal_id=proposal_id,
                            now=commit_now,
                        ),
                    )
                ),
                "authority": self._authority_boundary(),
            }
            return current

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result or self._closed("suggestion_outbox_mutation_no_result")

    def _persist_decision_result(
        self,
        *,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        attention_hypothesis_ref: dict[str, Any],
        user_id: str,
        session_id: str,
        output: dict[str, Any],
        mode_epoch: Any,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        record = self._decision_record(
            general_situation=general_situation,
            assessment=assessment,
            attention_hypothesis_ref=attention_hypothesis_ref,
            user_id=user_id,
            session_id=session_id,
            mode=str(output.get("mode") or self.DEFAULT_MODE),
            mode_epoch=mode_epoch,
            decision_disposition=str(
                output.get("decision_disposition") or "silent"
            ),
            delivery_disposition=str(
                output.get("delivery_disposition") or "none"
            ),
            reason=str(output.get("reason") or ""),
            proposal_id=proposal_id,
            now=now,
        )
        result: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if not self._healthy(current):
                result = self._closed("suggestion_outbox_state_corrupt")
                return current
            committed_config = self._mode_snapshot()
            if committed_config.get("status") == "fail_closed":
                result = committed_config
                return current
            committed_mode = str(
                committed_config.get("mode") or output.get("mode") or self.DEFAULT_MODE
            )
            committed_epoch = committed_config.get("mode_epoch")
            if (
                committed_mode != str(output.get("mode") or self.DEFAULT_MODE)
                or committed_epoch != mode_epoch
            ):
                commit_now = self._now()
                attention_issue = self._current_attention_binding_issue(
                    general_situation=general_situation,
                    assessment=assessment,
                    attention_hypothesis_ref=attention_hypothesis_ref,
                    user_id=user_id,
                    scope_key=tenant_scope_storage_key(user_id, session_id),
                    now=commit_now,
                    require_confirmed=False,
                )
                if attention_issue is not None:
                    result = {
                        **copy.deepcopy(output),
                        "status": "fail_closed",
                        "reason": attention_issue,
                        "delivery_disposition": "suppressed",
                        "interaction_decision": None,
                        "authority": self._authority_boundary(),
                    }
                    return current
                suppressed = self._decision_record(
                    general_situation=general_situation,
                    assessment=assessment,
                    attention_hypothesis_ref=attention_hypothesis_ref,
                    user_id=user_id,
                    session_id=session_id,
                    mode=committed_mode,
                    mode_epoch=committed_epoch,
                    decision_disposition=str(
                        output.get("decision_disposition") or "silent"
                    ),
                    delivery_disposition="suppressed",
                    reason="suggestion_mode_changed_before_commit",
                    proposal_id=proposal_id,
                    now=commit_now,
                )
                selected = self._record_decision_in_state(current, suppressed)
                result = {
                    **copy.deepcopy(output),
                    "status": "suppressed",
                    "mode": committed_mode,
                    "reason": "suggestion_mode_changed_before_commit",
                    "delivery_disposition": "suppressed",
                    "interaction_decision": self._public_decision(selected),
                    "authority": self._authority_boundary(),
                }
                return current
            decision_disposition = str(
                output.get("decision_disposition") or "silent"
            )
            # Every non-say disposition is a durable review of the exact
            # current owner Situation/Attention binding.  A missing or forged
            # reference is rejected before the decision ledger can record it;
            # otherwise an unbound ``wait``/``silent`` row could outlive the
            # evidence it claims to review.
            attention_issue = self._current_attention_binding_issue(
                general_situation=general_situation,
                assessment=assessment,
                attention_hypothesis_ref=attention_hypothesis_ref,
                user_id=user_id,
                scope_key=tenant_scope_storage_key(user_id, session_id),
                now=self._now(),
                require_confirmed=False,
            )
            if attention_issue is not None:
                # No-ref and stale/forged bindings are rejected before a
                # durable decision exists.  The caller receives an
                # owner-scoped fail-closed result, but the decision ledger
                # remains free of an unreviewable claim.
                result = {
                    **copy.deepcopy(output),
                    "status": "fail_closed",
                    "reason": attention_issue,
                    "delivery_disposition": "suppressed",
                    "interaction_decision": None,
                    "authority": self._authority_boundary(),
                }
                return current
            selected = self._record_decision_in_state(current, record)
            result = {
                **copy.deepcopy(output),
                "interaction_decision": self._public_decision(selected),
                "authority": self._authority_boundary(),
            }
            return current

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result or self._closed("suggestion_outbox_decision_mutation_no_result")

    def _decision_record(
        self,
        *,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        attention_hypothesis_ref: dict[str, Any],
        user_id: str,
        session_id: str,
        mode: str,
        mode_epoch: Any,
        decision_disposition: str,
        delivery_disposition: str,
        reason: str,
        proposal_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        parent_revision = self._nonnegative_int(
            general_situation.get("parent_revision")
        )
        assessment_binding_digest = stable_digest(
            "veyra.interaction_decision.assessment_binding.v1",
            assessment.get("assessment_binding") or {},
        )
        identity = self._decision_identity(
            user_id=user_id,
            session_id=session_id,
            general_situation_id=str(
                general_situation.get("general_situation_id") or ""
            ),
            parent_revision=parent_revision,
            attention_hypothesis_ref=attention_hypothesis_ref,
            assessment_binding_digest=assessment_binding_digest,
            mode=mode,
            mode_epoch=self._nonnegative_int(mode_epoch),
            decision_disposition=decision_disposition,
            delivery_disposition=delivery_disposition,
            reason=reason,
            proposal_id=proposal_id,
        )
        decision_id = self.decision_id_for_identity(identity)
        return {
            "schema_version": self.DECISION_SCHEMA_VERSION,
            "decision_id": decision_id,
            "user_id": user_id,
            "session_id": session_id,
            "general_situation_id": str(
                general_situation.get("general_situation_id") or ""
            ),
            "parent_revision": parent_revision,
            "attention_hypothesis_ref": copy.deepcopy(attention_hypothesis_ref),
            "assessment_binding_digest": assessment_binding_digest,
            "mode": mode,
            "mode_epoch": self._nonnegative_int(mode_epoch),
            "decision_disposition": decision_disposition,
            "delivery_disposition": delivery_disposition,
            "reason": reason,
            "proposal_id": proposal_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "authority": self._authority_boundary(),
        }

    @staticmethod
    def _decision_identity(
        *,
        user_id: str,
        session_id: str,
        general_situation_id: str,
        parent_revision: int,
        attention_hypothesis_ref: dict[str, Any] | None,
        assessment_binding_digest: str,
        mode: str,
        mode_epoch: int,
        decision_disposition: str,
        delivery_disposition: str,
        reason: str,
        proposal_id: str | None,
    ) -> dict[str, Any]:
        """Return the immutable interaction-decision identity payload."""

        return {
            "user_id": user_id,
            "session_id": session_id,
            "general_situation_id": general_situation_id,
            "parent_revision": parent_revision,
            "attention_hypothesis_ref": copy.deepcopy(attention_hypothesis_ref),
            "assessment_binding_digest": assessment_binding_digest,
            "mode": mode,
            "mode_epoch": mode_epoch,
            "decision_disposition": decision_disposition,
            "delivery_disposition": delivery_disposition,
            "reason": reason,
            "proposal_id": proposal_id,
        }

    @staticmethod
    def decision_id_for_identity(identity: dict[str, Any]) -> str:
        if not isinstance(identity, dict):
            raise TypeError("interaction decision identity must be a mapping")
        return "idec_" + stable_digest(
            "veyra.interaction_decision.identity.v1", identity
        )[:24]

    @classmethod
    def decision_id_for(cls, record: dict[str, Any]) -> str:
        """Recompute an interaction decision ID from immutable semantics."""

        if not isinstance(record, dict):
            raise TypeError("interaction decision must be a mapping")
        identity = cls._decision_identity(
            user_id=str(record.get("user_id") or ""),
            session_id=str(record.get("session_id") or ""),
            general_situation_id=str(record.get("general_situation_id") or ""),
            parent_revision=cls._nonnegative_int(record.get("parent_revision")),
            attention_hypothesis_ref=copy.deepcopy(
                record.get("attention_hypothesis_ref")
            ),
            assessment_binding_digest=str(
                record.get("assessment_binding_digest") or ""
            ),
            mode=str(record.get("mode") or ""),
            mode_epoch=cls._nonnegative_int(record.get("mode_epoch")),
            decision_disposition=str(
                record.get("decision_disposition") or ""
            ),
            delivery_disposition=str(
                record.get("delivery_disposition") or ""
            ),
            reason=str(record.get("reason") or ""),
            proposal_id=(
                str(record.get("proposal_id"))
                if record.get("proposal_id") is not None
                else None
            ),
        )
        return cls.decision_id_for_identity(identity)

    interaction_decision_id_for = decision_id_for

    def _record_decision_in_state(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        decision_id = str(record.get("decision_id") or "")
        if not self._valid_decision_record(decision_id, record):
            raise SuggestionOutboxConflict(
                "interaction decision record failed immutable identity validation"
            )
        decisions = self._records(state.get("interaction_decisions"))
        existing = decisions.get(decision_id)
        if existing is not None:
            if not self._valid_decision_record(decision_id, existing):
                raise SuggestionOutboxConflict(
                    "interaction decision ledger record is corrupt"
                )
            immutable = {
                key: existing.get(key)
                for key in (
                    "user_id",
                    "session_id",
                    "general_situation_id",
                    "parent_revision",
                    "attention_hypothesis_ref",
                    "assessment_binding_digest",
                    "mode",
                    "mode_epoch",
                    "decision_disposition",
                    "delivery_disposition",
                    "reason",
                    "proposal_id",
                )
            }
            incoming = {key: record.get(key) for key in immutable}
            if immutable != incoming:
                raise SuggestionOutboxConflict(
                    "interaction decision identity was rebound"
                )
            return existing
        if len(decisions) >= self.MAX_INTERACTION_DECISIONS:
            raise SuggestionOutboxConflict(
                "interaction decision ledger capacity exhausted"
            )
        decisions[decision_id] = copy.deepcopy(record)
        state["schema_version"] = self.SCHEMA_VERSION
        state["interaction_decisions"] = decisions
        state["interaction_decision_count"] = len(decisions)
        state["updated_at"] = record["updated_at"]
        return record

    @classmethod
    def _public_decision(cls, value: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(value)

    def status(self) -> dict[str, Any]:
        """Pure operational status; it does not resolve or deliver proposals."""

        config = self._mode_snapshot()
        state = self.state_store.read_json(self.STATE_FILE)
        if config.get("status") == "fail_closed":
            return config
        if not self._healthy(state):
            return self._closed("suggestion_outbox_state_corrupt")
        return {
            "status": "success",
            "mode": config["mode"],
            "mode_epoch": config["mode_epoch"],
            "allowed_modes": sorted(self.MODES),
            "proposal_count": len(self._records(state.get("proposals"))),
            "interaction_decision_count": len(
                self._records(state.get("interaction_decisions"))
            ),
            "policy_count": len(self._records(state.get("policies"))),
            "state_revision": self._nonnegative_int(state.get("_state_revision")),
            "ops_config_revision": config["ops_config_revision"],
            "delivery_channels": ["owner_scoped_console"],
            "external_delivery_enabled": False,
            "authority": self._authority_boundary(),
        }

    def list_inbox(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pure exact-owner Console inbox read."""

        user, session, scope_key = self._owner(user_id, session_id)
        config = self._mode_snapshot()
        if config.get("status") == "fail_closed":
            return config
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy(state):
            return self._closed("suggestion_outbox_state_corrupt")
        proposals = self._records(state.get("proposals"))
        inboxes = self._list_records(state.get("owner_inboxes"))
        ids = [str(item) for item in inboxes.get(scope_key, []) if str(item)]
        items: list[dict[str, Any]] = []
        legacy_hidden_count = 0
        stale_hidden_count = 0
        seen: set[str] = set()
        for proposal_id in reversed(ids):
            if proposal_id in seen:
                continue
            seen.add(proposal_id)
            proposal = proposals.get(proposal_id)
            if (
                not isinstance(proposal, dict)
                or str(proposal.get("user_id") or "") != user
                or str(proposal.get("session_id") or "") != session
            ):
                continue
            if str(proposal.get("schema_version") or "") == (
                "veyra.informational_suggestion.v1"
            ):
                legacy_hidden_count += 1
                continue
            if not self._current_proposal_integrity(proposal):
                stale_hidden_count += 1
                continue
            if self._current_proposal_issue(
                proposal,
                user_id=user,
                scope_key=scope_key,
                now=self._now(),
            ) is not None:
                stale_hidden_count += 1
                continue
            items.append(self._public_proposal(proposal))
        if config.get("mode") != "advise_only":
            items = []
        selected_limit = max(0, min(int(limit), self.MAX_INBOX_ITEMS))
        policy = self._effective_policy(self._records(state.get("policies")).get(scope_key))
        return {
            "status": "success",
            "count": min(len(items), selected_limit),
            "items": items[:selected_limit],
            "legacy_hidden_count": legacy_hidden_count,
            "stale_hidden_count": stale_hidden_count,
            "policy": self._public_policy(policy),
            "mode": config.get("mode"),
            "state_revision": self._nonnegative_int(state.get("_state_revision")),
            "authority": self._authority_boundary(),
        }

    def list_decisions(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pure exact-owner review of typed interaction decisions."""

        user, session, _scope_key = self._owner(user_id, session_id)
        selected_limit = max(0, min(int(limit), 500))
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy(state):
            return {**self._closed("suggestion_outbox_state_corrupt"), "items": [], "count": 0}
        decisions = self._records(state.get("interaction_decisions"))
        visible = [
            item
            for item in decisions.values()
            if str(item.get("user_id") or "") == user
            and str(item.get("session_id") or "") == session
        ]
        visible.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("decision_id") or ""),
            ),
            reverse=True,
        )
        return {
            "status": "success",
            "count": min(len(visible), selected_limit),
            "decision_count": len(visible),
            "items": [copy.deepcopy(item) for item in visible[:selected_limit]],
            "state_revision": self._nonnegative_int(state.get("_state_revision")),
            "authority": self._authority_boundary(),
        }

    # ``review_decisions`` is intentionally an alias rather than a second
    # implementation so any future owner-scoped API keeps the same pure read
    # and validation boundary.
    review_decisions = list_decisions

    def acknowledge(
        self,
        proposal_id: str,
        *,
        user_id: str,
        session_id: str,
        expected_state_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return self._transition(
            proposal_id,
            transition="acknowledged",
            user_id=user_id,
            session_id=session_id,
            expected_state_revision=expected_state_revision,
            reason=reason,
        )

    def dismiss(
        self,
        proposal_id: str,
        *,
        user_id: str,
        session_id: str,
        expected_state_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return self._transition(
            proposal_id,
            transition="dismissed",
            user_id=user_id,
            session_id=session_id,
            expected_state_revision=expected_state_revision,
            reason=reason,
        )

    def _transition(
        self,
        proposal_id: str,
        *,
        transition: str,
        user_id: str,
        session_id: str,
        expected_state_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        user, session, scope_key = self._owner(user_id, session_id)
        self._required_revision(expected_state_revision)
        selected_id = str(proposal_id or "").strip()
        if not selected_id or len(selected_id) > 120:
            raise ValueError("proposal_id is invalid")
        selected_reason = " ".join(str(reason or "").strip().split())[:500]
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            self._require_healthy(state)
            current_revision = self._nonnegative_int(state.get("_state_revision"))
            if current_revision != expected_state_revision:
                raise SuggestionOutboxConflict(
                    "expected_state_revision does not match suggestion outbox"
                )
            commit_now = self._now()
            proposals = self._records(state.get("proposals"))
            proposal = proposals.get(selected_id)
            if not isinstance(proposal, dict):
                raise KeyError("suggestion proposal was not found")
            if proposal.get("schema_version") != self.PROPOSAL_SCHEMA_VERSION:
                raise SuggestionOutboxConflict(
                    "legacy suggestion proposals are not transitionable"
                )
            if not self._current_proposal_integrity(proposal):
                raise SuggestionOutboxConflict(
                    "suggestion proposal revision is corrupt"
                )
            if (
                str(proposal.get("user_id") or "") != user
                or str(proposal.get("session_id") or "") != session
            ):
                raise PermissionError("suggestion proposal belongs to another owner")
            if str(proposal.get("status") or "") == transition:
                result = {
                    "status": "replayed",
                    "proposal": self._public_proposal(proposal),
                    "authority": self._authority_boundary(),
                }
                return state
            if str(proposal.get("status") or "") not in {"pending", "acknowledged"}:
                raise SuggestionOutboxConflict(
                    "suggestion proposal is not transitionable"
                )
            policies = self._records(state.get("policies"))
            policy = self._effective_policy(policies.get(scope_key))
            cooldown_seconds = int(
                policy[
                    "dismiss_cooldown_seconds"
                    if transition == "dismissed"
                    else "cooldown_seconds"
                ]
            )
            proposal["status"] = transition
            proposal[f"{transition}_at"] = commit_now.isoformat()
            proposal["updated_at"] = commit_now.isoformat()
            proposals[selected_id] = proposal
            feedback = self._records(state.get("feedback"))
            feedback_key = (
                f"{scope_key}:"
                f"{str(proposal.get('general_situation_id') or '')}"
            )
            feedback[feedback_key] = {
                "user_id": user,
                "session_id": session,
                "proposal_id": selected_id,
                "status": transition,
                "reason": selected_reason,
                "recorded_at": commit_now.isoformat(),
                "cooldown_until": (
                    commit_now + timedelta(seconds=cooldown_seconds)
                ).isoformat(),
            }
            state["proposals"] = proposals
            state["feedback"] = feedback
            state["updated_at"] = commit_now.isoformat()
            result = {
                "status": transition,
                "proposal": self._public_proposal(proposal),
                "cooldown_until": feedback[feedback_key]["cooldown_until"],
                "authority": self._authority_boundary(),
            }
            return state

        persisted = self.state_store.mutate_json(self.STATE_FILE, mutate)
        result["state_revision"] = self._nonnegative_int(
            persisted.get("_state_revision")
        )
        return result

    def _proposal(
        self,
        *,
        proposal_id: str,
        mode: str,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        attention_hypothesis_ref: dict[str, Any] | None,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        status = {
            "record_only": "recorded",
            "shadow": "would_suggest",
            "advise_only": "pending",
        }[mode]
        why_now = self._why_now(assessment.get("components"))
        proposal = {
            "schema_version": self.PROPOSAL_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "proposal_kind": "informational",
            "user_id": user_id,
            "session_id": session_id,
            "general_situation_id": general_situation.get(
                "general_situation_id"
            ),
            "parent_revision": general_situation.get("parent_revision"),
            "mode": mode,
            "status": status,
            "reason": "structured_attention_threshold_met",
            "decision_disposition": "say",
            "delivery_disposition": (
                "owner_scoped_console" if mode == "advise_only" else "none"
            ),
            "why_now": why_now,
            "score": assessment.get("score"),
            "upstream_scorer_version": assessment.get(
                "upstream_scorer_version"
            ),
            "evidence": copy.deepcopy(assessment.get("evidence") or []),
            "unknowns": copy.deepcopy(assessment.get("unknowns") or []),
            "attention_readiness": copy.deepcopy(
                assessment.get("attention_readiness") or {}
            ),
            "evidence_diversity": copy.deepcopy(
                assessment.get("evidence_diversity") or {}
            ),
            "assessment_binding": copy.deepcopy(
                assessment.get("assessment_binding") or {}
            ),
            "source_expires_at": general_situation.get("expires_at"),
            "options": [
                {"id": "inspect_evidence", "execution_allowed": False},
                {"id": "acknowledge", "execution_allowed": False},
                {"id": "dismiss", "execution_allowed": False},
            ],
            "delivery": {
                "channel": "owner_scoped_console" if mode == "advise_only" else "none",
                "external_delivery": False,
                "feishu_delivery": False,
                "agent_delivery": False,
            },
            "authority": self._authority_boundary(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        if attention_hypothesis_ref is not None:
            proposal["attention_hypothesis_ref"] = copy.deepcopy(
                attention_hypothesis_ref
            )
        proposal["proposal_revision"] = self.proposal_revision_for(proposal)
        return proposal

    @staticmethod
    def _why_now(components: Any) -> list[dict[str, Any]]:
        selected = components if isinstance(components, dict) else {}
        return [
            {
                "component": str(name),
                "value": value.get("value"),
                "weight": value.get("weight"),
                "source": value.get("source"),
            }
            for name, value in sorted(selected.items())
            if isinstance(value, dict)
        ]

    @classmethod
    def _interaction_decision(cls, assessment: dict[str, Any]) -> tuple[str, str]:
        """Choose a typed interaction disposition without adding authority.

        ``ask`` remains part of the persisted enum for compatibility, but no
        current producer is trusted to issue an owner-question lifecycle.  In
        particular, caller-supplied ``interaction_gap`` fields are treated as
        untrusted context and can never manufacture an ``ask`` decision.
        """

        hypothesis_status = str(assessment.get("hypothesis_status") or "")
        if (
            assessment.get("status") == "eligible"
            and assessment.get("eligible") is not False
            and not hypothesis_status
        ):
            return "say", "structured_attention_threshold_met"
        if hypothesis_status == "confirmed" and assessment.get("eligible") is True:
            return "say", "structured_attention_threshold_met"
        if hypothesis_status in {"candidate", "accumulating"}:
            return "wait", "attention_evidence_accumulating"
        if hypothesis_status == "contradicted":
            return "silent", "attention_hypothesis_contradicted"
        if hypothesis_status == "expired":
            return "silent", "attention_hypothesis_expired"
        return "silent", "structured_attention_not_eligible"

    def _surface_suppression(
        self,
        state: dict[str, Any],
        *,
        general_situation_id: str,
        scope_key: str,
        user_id: str,
        policy: dict[str, Any],
        now: datetime,
    ) -> str | None:
        if policy.get("sandbox_enabled") is not True:
            return "suggestion_sandbox_not_enabled"
        feedback = self._records(state.get("feedback")).get(
            f"{scope_key}:{general_situation_id}"
        )
        if isinstance(feedback, dict):
            cooldown_until = self._aware_time(feedback.get("cooldown_until"))
            if cooldown_until is None:
                return "feedback_cooldown_invalid"
            if cooldown_until > now:
                return (
                    "dismissed_cooldown"
                    if feedback.get("status") == "dismissed"
                    else "acknowledged_cooldown"
                )
        if self._in_quiet_hours(now, policy):
            return "quiet_hours"
        day_key = self._day_key(now, policy)
        owner_key = self._owner_budget_key(user_id)
        counter = self._records(state.get("daily_counters")).get(
            f"{owner_key}:{day_key}"
        )
        used = self._nonnegative_int(
            counter.get("count") if isinstance(counter, dict) else 0
        )
        if used >= int(policy["daily_budget"]):
            return "daily_budget_exhausted"
        return None

    def _validate_binding(
        self,
        parent: dict[str, Any],
        assessment: dict[str, Any],
        *,
        user_id: str,
        scope_key: str,
    ) -> None:
        if not isinstance(parent, dict) or not isinstance(assessment, dict):
            raise TypeError("general situation and assessment must be mappings")
        if str(parent.get("user_id") or "") != user_id:
            raise ValueError("general situation belongs to another user")
        scope_keys = parent.get("session_scope_keys")
        if not isinstance(scope_keys, list) or scope_key not in scope_keys:
            raise ValueError("general situation does not include this session")
        if (
            str(assessment.get("general_situation_id") or "")
            != str(parent.get("general_situation_id") or "")
            or assessment.get("parent_revision")
            != parent.get("parent_revision")
        ):
            raise ValueError("assessment is not bound to the parent revision")
        authority = assessment.get("authority") if isinstance(assessment.get("authority"), dict) else {}
        if any(authority.get(key) is not False for key in self._authority_boundary()):
            raise ValueError("assessment authority boundary is invalid")

    def _current_attention_binding_issue(
        self,
        *,
        general_situation: dict[str, Any],
        assessment: dict[str, Any],
        attention_hypothesis_ref: dict[str, Any] | None,
        user_id: str,
        scope_key: str,
        now: datetime,
        require_confirmed: bool = True,
    ) -> str | None:
        """Require an exact, current confirmed ledger record before surfacing."""

        if not isinstance(attention_hypothesis_ref, dict):
            return "current_attention_hypothesis_required"
        from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime
        from runtime.general_situation_runtime import GeneralSituationRuntime

        general_state = self.state_store.read_json(
            GeneralSituationRuntime.STATE_FILE
        )
        if not GeneralSituationRuntime._healthy_state(general_state):
            return "general_situation_state_corrupt"
        current_parent = self._records(
            general_state.get("general_situations")
        ).get(str(general_situation.get("general_situation_id") or ""))
        if not isinstance(current_parent, dict):
            return "current_general_situation_required"
        if AttentionHypothesisRuntime._parent_binding(
            current_parent
        ) != AttentionHypothesisRuntime._parent_binding(general_situation):
            return "general_situation_revision_not_current"
        attention_state = self.state_store.read_json(
            AttentionHypothesisRuntime.STATE_FILE
        )
        if not AttentionHypothesisRuntime._healthy_state(attention_state):
            return "attention_hypothesis_state_corrupt"
        hypothesis_id = str(
            attention_hypothesis_ref.get("hypothesis_id") or ""
        )
        record = self._records(attention_state.get("hypotheses")).get(
            hypothesis_id
        )
        if not isinstance(record, dict):
            return "attention_hypothesis_not_found"
        expires_at = self._aware_time(record.get("expires_at"))
        if expires_at is None:
            return "attention_hypothesis_expired"
        if expires_at <= now and record.get("status") not in {"expired", "contradicted"}:
            return "attention_hypothesis_expired"
        if (
            record.get("user_id") != user_id
            or scope_key not in set(record.get("session_scope_keys") or [])
            or record.get("general_situation_id")
            != general_situation.get("general_situation_id")
            or record.get("parent_revision")
            != general_situation.get("parent_revision")
            or record.get("hypothesis_revision")
            != attention_hypothesis_ref.get("hypothesis_revision")
            or record.get("ruleset_version")
            != attention_hypothesis_ref.get("ruleset_version")
            or str(assessment.get("hypothesis_status") or "")
            != str(record.get("status") or "")
            or assessment.get("status")
            != ("eligible" if record.get("status") == "confirmed" else "awaiting_evidence")
            or assessment.get("eligible")
            is not (record.get("status") == "confirmed")
            or (require_confirmed and record.get("status") != "confirmed")
            or record.get("general_attention_scorer_version")
            != assessment.get("upstream_scorer_version")
        ):
            return "attention_hypothesis_binding_not_current"
        # A durable contradiction or expiry is itself the canonical terminal
        # Attention decision. Its lifecycle marker intentionally changes the
        # record digest, so replaying the pre-terminal evidence generation
        # would incorrectly turn the required silent result into a stale
        # binding failure. Parent/owner/revision checks above still bind this
        # terminal result exactly; superseded records remain fail-closed.
        if not require_confirmed and record.get("status") in {"contradicted", "expired"}:
            if not self._terminal_surface_matches_record(
                assessment,
                record=record,
            ):
                return "attention_hypothesis_surface_not_current"
            return None
        canonical = self._canonical_attention_evaluation(
            current_parent,
            record=record,
            now=now,
            require_confirmable=require_confirmed,
        )
        if canonical is None:
            return "attention_hypothesis_evidence_not_current"
        if not self._surface_matches_current_generation(
            assessment,
            canonical=canonical,
            record=record,
            now=now,
        ):
            return "attention_hypothesis_surface_not_current"
        return None

    @staticmethod
    def _terminal_surface_matches_record(
        surface: dict[str, Any],
        *,
        record: dict[str, Any],
    ) -> bool:
        """Bind silent terminal decisions to the durable lifecycle row."""

        readiness = record.get("attention_readiness")
        return bool(
            surface.get("general_situation_id")
            == record.get("general_situation_id")
            and surface.get("parent_revision") == record.get("parent_revision")
            and surface.get("upstream_scorer_version")
            == record.get("general_attention_scorer_version")
            and surface.get("components") == record.get("components")
            and surface.get("unknowns") == record.get("unknowns")
            and surface.get("evidence")
            == copy.deepcopy(record.get("current_evidence_refs") or [])
            and surface.get("attention_readiness") == readiness
            and surface.get("evidence_diversity")
            == record.get("evidence_diversity")
            and surface.get("assessment_binding")
            == record.get("assessment_binding")
            and isinstance(readiness, dict)
            and surface.get("score") == readiness.get("value")
        )

    @classmethod
    def _current_proposal_integrity(cls, proposal: dict[str, Any]) -> bool:
        if (
            not isinstance(proposal, dict)
            or proposal.get("schema_version") != cls.PROPOSAL_SCHEMA_VERSION
        ):
            return False
        revision = str(proposal.get("proposal_revision") or "")
        return bool(
            revision.startswith("sugr_")
            and revision == cls.proposal_revision_for(proposal)
        )

    def _current_proposal_issue(
        self,
        proposal: dict[str, Any],
        *,
        user_id: str,
        scope_key: str,
        now: datetime,
    ) -> str | None:
        from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime
        from runtime.general_situation_runtime import GeneralSituationRuntime

        ref = proposal.get("attention_hypothesis_ref")
        if not isinstance(ref, dict):
            return "attention_hypothesis_reference_missing"
        attention_state = self.state_store.read_json(
            AttentionHypothesisRuntime.STATE_FILE
        )
        if not AttentionHypothesisRuntime._healthy_state(attention_state):
            return "attention_hypothesis_state_corrupt"
        record = self._records(attention_state.get("hypotheses")).get(
            str(ref.get("hypothesis_id") or "")
        )
        if not isinstance(record, dict):
            return "attention_hypothesis_not_found"
        general_state = self.state_store.read_json(
            GeneralSituationRuntime.STATE_FILE
        )
        if not GeneralSituationRuntime._healthy_state(general_state):
            return "general_situation_state_corrupt"
        parent = self._records(general_state.get("general_situations")).get(
            str(proposal.get("general_situation_id") or "")
        )
        expires_at = self._aware_time(record.get("expires_at"))
        if (
            not isinstance(parent, dict)
            or parent.get("parent_revision") != proposal.get("parent_revision")
            or record.get("user_id") != user_id
            or scope_key not in set(record.get("session_scope_keys") or [])
            or record.get("general_situation_id")
            != proposal.get("general_situation_id")
            or record.get("parent_revision") != proposal.get("parent_revision")
            or record.get("hypothesis_revision")
            != ref.get("hypothesis_revision")
            or record.get("ruleset_version") != ref.get("ruleset_version")
            or record.get("status") != "confirmed"
            or expires_at is None
            or expires_at <= now
            or proposal.get("upstream_scorer_version")
            != record.get("general_attention_scorer_version")
        ):
            return "proposal_attention_binding_not_current"
        canonical = self._canonical_attention_evaluation(
            parent,
            record=record,
            now=now,
        )
        if canonical is None:
            return "proposal_attention_evidence_not_current"
        if not self._proposal_matches_current_generation(
            proposal,
            parent=parent,
            canonical=canonical,
            record=record,
            now=now,
        ):
            return "proposal_attention_surface_not_current"
        return None

    def _canonical_attention_evaluation(
        self,
        parent: dict[str, Any],
        *,
        record: dict[str, Any],
        now: datetime,
        require_confirmable: bool = True,
    ) -> dict[str, Any] | None:
        from awareness.general_attention_scheduler import GeneralAttentionScheduler
        from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime

        scheduler = GeneralAttentionScheduler(
            self.state_store,
            clock=lambda: now,
        )
        assessment = scheduler.assess(parent)
        runtime = AttentionHypothesisRuntime(
            self.state_store,
            clock=lambda: now,
        )
        try:
            evaluated = runtime._evaluate(parent, assessment)
        except (TypeError, ValueError):
            return None
        if (
            (require_confirmable and evaluated.get("confirmable") is not True)
            or record.get("general_attention_scorer_version")
            != evaluated.get("general_attention_scorer_version")
            or not runtime._same_assessment_generation(record, evaluated)
        ):
            return None
        return evaluated

    def _surface_matches_current_generation(
        self,
        surface: dict[str, Any],
        *,
        canonical: dict[str, Any],
        record: dict[str, Any],
        now: datetime,
    ) -> bool:
        record_refs = copy.deepcopy(record.get("current_evidence_refs") or [])
        return bool(
            surface.get("upstream_scorer_version")
            == record.get("general_attention_scorer_version")
            == canonical.get("general_attention_scorer_version")
            and surface.get("general_situation_id")
            == record.get("general_situation_id")
            and surface.get("parent_revision") == record.get("parent_revision")
            and surface.get("evidence") == record_refs
            and surface.get("components") == record.get("components")
            and surface.get("unknowns") == record.get("unknowns")
            and surface.get("attention_readiness")
            == record.get("attention_readiness")
            and surface.get("evidence_diversity")
            == record.get("evidence_diversity")
            and surface.get("score")
            == record.get("attention_readiness", {}).get("value")
            and surface.get("assessment_binding")
            == record.get("assessment_binding")
            and self._bounded_assessment_binding(
                surface.get("assessment_binding"),
                canonical=canonical.get("assessment_binding"),
                record=record.get("assessment_binding"),
                now=now,
            )
        )

    def _proposal_matches_current_generation(
        self,
        proposal: dict[str, Any],
        *,
        parent: dict[str, Any],
        canonical: dict[str, Any],
        record: dict[str, Any],
        now: datetime,
    ) -> bool:
        record_refs = copy.deepcopy(record.get("current_evidence_refs") or [])
        record_readiness = record.get("attention_readiness")
        return bool(
            proposal.get("decision_disposition") == "say"
            and proposal.get("delivery_disposition")
            == (
                "owner_scoped_console"
                if proposal.get("mode") == "advise_only"
                else "none"
            )
            and proposal.get("source_expires_at") == parent.get("expires_at")
            and proposal.get("evidence") == record_refs
            and proposal.get("why_now")
            == self._why_now(record.get("components"))
            and proposal.get("unknowns") == record.get("unknowns")
            and proposal.get("evidence_diversity")
            == record.get("evidence_diversity")
            and proposal.get("attention_readiness") == record_readiness
            and isinstance(record_readiness, dict)
            and proposal.get("score") == record_readiness.get("value")
            and proposal.get("upstream_scorer_version")
            == record.get("general_attention_scorer_version")
            == canonical.get("general_attention_scorer_version")
            and proposal.get("assessment_binding")
            == record.get("assessment_binding")
            and self._bounded_assessment_binding(
                proposal.get("assessment_binding"),
                canonical=canonical.get("assessment_binding"),
                record=record.get("assessment_binding"),
                now=now,
            )
        )

    @classmethod
    def _bounded_assessment_binding(
        cls,
        value: Any,
        *,
        canonical: Any,
        record: Any,
        now: datetime,
    ) -> bool:
        if not all(isinstance(item, dict) for item in (value, canonical, record)):
            return False
        fixed_keys = {
            "schema_version",
            "scorer_version",
            "general_situation_id",
            "parent_revision",
            "parent_digest",
        }
        revision_keys = {
            "situation_state_revision",
            "general_situation_state_revision",
            "goal_state_revision",
            "guardian_attention_state_revision",
        }
        if set(value) != fixed_keys | revision_keys | {"assessed_at"}:
            return False
        if any(value.get(key) != canonical.get(key) for key in fixed_keys):
            return False
        for key in revision_keys:
            selected = value.get(key)
            lower = record.get(key)
            upper = canonical.get(key)
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (selected, lower, upper)
            ) or not lower <= selected <= upper:
                return False
        assessed_at = cls._aware_time(value.get("assessed_at"))
        record_at = cls._aware_time(record.get("assessed_at"))
        return bool(
            assessed_at is not None
            and record_at is not None
            and record_at <= assessed_at <= now
        )

    @classmethod
    def _validated_attention_hypothesis_ref(
        cls,
        assessment: dict[str, Any],
    ) -> dict[str, Any] | None:
        raw = assessment.get("attention_hypothesis_ref")
        is_hypothesis_surface = (
            str(assessment.get("schema_version") or "")
            == cls.ATTENTION_HYPOTHESIS_SURFACE_SCHEMA_VERSION
        )
        if raw is None:
            if assessment.get("eligible") is True:
                raise ValueError(
                    "eligible attention hypothesis surface requires an exact reference"
                )
            return None
        if not is_hypothesis_surface:
            raise ValueError("attention hypothesis reference requires its exact surface")
        if not isinstance(raw, dict) or set(raw) != {
            "hypothesis_id",
            "hypothesis_revision",
            "ruleset_version",
            "readiness_semantics",
        }:
            raise ValueError("attention hypothesis reference is invalid")
        hypothesis_id = str(raw.get("hypothesis_id") or "").strip()
        hypothesis_revision = raw.get("hypothesis_revision")
        ruleset_version = str(raw.get("ruleset_version") or "").strip()
        readiness_semantics = str(
            raw.get("readiness_semantics") or ""
        ).strip()
        if (
            not hypothesis_id.startswith("ahyp_")
            or len(hypothesis_id) > 120
            or isinstance(hypothesis_revision, bool)
            or not isinstance(hypothesis_revision, int)
            or hypothesis_revision < 1
            or ruleset_version != cls.ATTENTION_HYPOTHESIS_RULESET_VERSION
            or readiness_semantics != cls.ATTENTION_READINESS_SEMANTICS
        ):
            raise ValueError("attention hypothesis reference is invalid")
        if is_hypothesis_surface and (
            str(assessment.get("scorer_version") or "") != ruleset_version
            or (
                assessment.get("eligible") is True
                and str(assessment.get("hypothesis_status") or "")
                != "confirmed"
            )
        ):
            raise ValueError(
                "attention hypothesis reference does not match its surface"
            )
        return {
            "hypothesis_id": hypothesis_id,
            "hypothesis_revision": hypothesis_revision,
            "ruleset_version": ruleset_version,
            "readiness_semantics": readiness_semantics,
        }

    def _mode_snapshot(self) -> dict[str, Any]:
        config = self.state_store.read_json("ops_config.json")
        if config.get("_state_corrupt") is True:
            return self._closed("ops_config_state_corrupt")
        section = (
            config.get("general_suggestions")
            if isinstance(config.get("general_suggestions"), dict)
            else {}
        )
        mode = str(section.get("mode") or self.DEFAULT_MODE).strip().lower()
        if mode not in self.MODES:
            return self._closed("general_suggestion_mode_invalid")
        mode_epoch = section.get("mode_epoch", 0)
        if (
            isinstance(mode_epoch, bool)
            or not isinstance(mode_epoch, int)
            or mode_epoch < 0
        ):
            return self._closed("general_suggestion_mode_epoch_invalid")
        return {
            "status": "success",
            "mode": mode,
            "mode_epoch": mode_epoch,
            "ops_config_revision": self._nonnegative_int(
                config.get("_state_revision")
            ),
        }

    def _effective_policy(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return copy.deepcopy(self.DEFAULT_POLICY)
        try:
            quiet_hours = self._quiet_hours(value.get("quiet_hours"))
        except ValueError:
            quiet_hours = None
        daily_budget = value.get("daily_budget")
        timezone_name = str(value.get("timezone") or "UTC").strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone_name = "UTC"
        cooldown = value.get("cooldown_seconds")
        dismiss = value.get("dismiss_cooldown_seconds")
        return {
            "sandbox_enabled": value.get("sandbox_enabled") is True,
            "daily_budget": (
                daily_budget
                if isinstance(daily_budget, int)
                and not isinstance(daily_budget, bool)
                and 0 <= daily_budget <= 1
                else self.DEFAULT_POLICY["daily_budget"]
            ),
            "timezone": timezone_name,
            "quiet_hours": quiet_hours,
            "cooldown_seconds": (
                cooldown
                if isinstance(cooldown, int)
                and not isinstance(cooldown, bool)
                and 0 <= cooldown <= 30 * 86400
                else self.DEFAULT_POLICY["cooldown_seconds"]
            ),
            "dismiss_cooldown_seconds": (
                dismiss
                if isinstance(dismiss, int)
                and not isinstance(dismiss, bool)
                and 0 <= dismiss <= 30 * 86400
                else self.DEFAULT_POLICY["dismiss_cooldown_seconds"]
            ),
        }

    @staticmethod
    def _public_policy(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value.get(key))
            for key in (
                "sandbox_enabled",
                "daily_budget",
                "timezone",
                "quiet_hours",
                "cooldown_seconds",
                "dismiss_cooldown_seconds",
                "updated_at",
            )
            if key in value
        }

    @classmethod
    def _public_proposal(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Project only reviewed Console fields, independent of validation."""

        return {
            key: copy.deepcopy(value.get(key))
            for key in cls._PUBLIC_PROPOSAL_KEYS
            if key in value
        }

    def _quiet_hours(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("quiet_hours must be an object or null")
        start = value.get("start_hour")
        end = value.get("end_hour")
        timezone_name = str(value.get("timezone") or "UTC").strip()
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start <= 23
            or not 0 <= end <= 23
            or start == end
        ):
            raise ValueError("quiet hour boundaries must be distinct integers from 0 to 23")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("quiet_hours timezone is unknown") from exc
        return {
            "start_hour": start,
            "end_hour": end,
            "timezone": timezone_name,
        }

    @classmethod
    def proposal_revision_for(cls, proposal: dict[str, Any]) -> str:
        """Digest immutable proposal semantics, excluding lifecycle metadata."""

        if not isinstance(proposal, dict):
            raise TypeError("suggestion proposal must be a mapping")
        semantics = {
            key: copy.deepcopy(proposal.get(key))
            for key in (
                "schema_version",
                "proposal_id",
                "proposal_kind",
                "user_id",
                "session_id",
                "general_situation_id",
                "parent_revision",
                "mode",
                "reason",
                "decision_disposition",
                "delivery_disposition",
                "why_now",
                "score",
                "upstream_scorer_version",
                "evidence",
                "unknowns",
                "options",
                "delivery",
                "authority",
                "attention_hypothesis_ref",
                "attention_readiness",
                "evidence_diversity",
                "assessment_binding",
                "source_expires_at",
            )
        }
        return "sugr_" + stable_digest(
            "veyra.informational_suggestion.revision.v1",
            semantics,
        )[:24]

    def _in_quiet_hours(self, now: datetime, policy: dict[str, Any]) -> bool:
        quiet = policy.get("quiet_hours")
        if not isinstance(quiet, dict):
            return False
        local = now.astimezone(ZoneInfo(str(quiet["timezone"])))
        start = int(quiet["start_hour"])
        end = int(quiet["end_hour"])
        return (
            start <= local.hour < end
            if start < end
            else local.hour >= start or local.hour < end
        )

    def _day_key(self, now: datetime, policy: dict[str, Any]) -> str:
        timezone_name = str(policy.get("timezone") or "UTC")
        return now.astimezone(ZoneInfo(timezone_name)).date().isoformat()

    @staticmethod
    def _owner_budget_key(user_id: str) -> str:
        return "owner-" + framed_sha256(
            "veyra-suggestion-owner-daily-budget.v1",
            user_id,
        )

    @staticmethod
    def _authority_boundary() -> dict[str, bool]:
        return {
            "execution_allowed": False,
            "tool_allowed": False,
            "agent_allowed": False,
            "capability_grant_allowed": False,
            "route_change_allowed": False,
        }

    def _closed(self, reason: str, *, detail: str | None = None) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": "fail_closed",
            "reason": reason,
            "proposal": None,
            "authority": self._authority_boundary(),
        }
        if detail:
            output["detail"] = detail
        return output

    @classmethod
    def _healthy(cls, state: dict[str, Any]) -> bool:
        if not isinstance(state, dict) or state.get("_state_corrupt") is True:
            return False
        if state.get("schema_version") != cls.SCHEMA_VERSION:
            return False
        revision = state.get("_state_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            return False

        collection_names = (
            "policies",
            "proposals",
            "owner_inboxes",
            "feedback",
            "daily_counters",
            "interaction_decisions",
        )
        if any(not isinstance(state.get(name), dict) for name in collection_names):
            return False
        policies = state["policies"]
        proposals = state["proposals"]
        inboxes = state["owner_inboxes"]
        feedback = state["feedback"]
        counters = state["daily_counters"]
        decisions = state["interaction_decisions"]
        if len(proposals) > cls.MAX_PROPOSALS:
            return False
        if len(decisions) > cls.MAX_INTERACTION_DECISIONS:
            return False
        if not cls._exact_count(state.get("policy_count"), len(policies)):
            return False
        if not cls._exact_count(state.get("proposal_count"), len(proposals)):
            return False
        if not cls._exact_count(
            state.get("interaction_decision_count"), len(decisions)
        ):
            return False

        for scope_key, policy in policies.items():
            if not cls._valid_policy_record(scope_key, policy):
                return False
        owner_timezones: dict[str, str] = {}
        for policy in policies.values():
            user_id = str(policy.get("user_id") or "")
            timezone_name = cls._policy_timezone(policy)
            existing_timezone = owner_timezones.get(user_id)
            if (
                existing_timezone is not None
                and existing_timezone != timezone_name
            ):
                return False
            owner_timezones[user_id] = timezone_name
        for proposal_id, proposal in proposals.items():
            if not cls._valid_proposal_record(proposal_id, proposal):
                return False
        for scope_key, proposal_ids in inboxes.items():
            if (
                not isinstance(scope_key, str)
                or not scope_key
                or not isinstance(proposal_ids, list)
                or len(proposal_ids) > cls.MAX_INBOX_ITEMS
                or any(not isinstance(item, str) for item in proposal_ids)
                or len(proposal_ids) != len(set(proposal_ids))
            ):
                return False
            for proposal_id in proposal_ids:
                proposal = proposals.get(proposal_id)
                if (
                    not isinstance(proposal_id, str)
                    or not isinstance(proposal, dict)
                    or cls._proposal_scope_key(proposal) != scope_key
                    or proposal.get("mode") != "advise_only"
                ):
                    return False
        for feedback_key, record in feedback.items():
            if not cls._valid_feedback_record(
                feedback_key,
                record,
                proposals=proposals,
            ):
                return False
        for counter_key, counter in counters.items():
            if not cls._valid_daily_counter(counter_key, counter):
                return False
        for decision_id, decision in decisions.items():
            if not cls._valid_decision_record(decision_id, decision):
                return False
        return True

    @staticmethod
    def _exact_count(value: Any, expected: int) -> bool:
        return bool(
            not isinstance(value, bool)
            and isinstance(value, int)
            and value == expected
        )

    @classmethod
    def _valid_policy_record(cls, scope_key: Any, value: Any) -> bool:
        if not isinstance(scope_key, str) or not isinstance(value, dict):
            return False
        current_keys = {
            "user_id",
            "session_id",
            "sandbox_enabled",
            "daily_budget",
            "timezone",
            "quiet_hours",
            "cooldown_seconds",
            "dismiss_cooldown_seconds",
            "updated_at",
        }
        legacy_keys = current_keys - {"sandbox_enabled", "timezone"}
        value_keys = frozenset(value)
        if value_keys not in {frozenset(current_keys), frozenset(legacy_keys)}:
            return False
        if cls._proposal_scope_key(value) != scope_key:
            return False
        daily_budget = value.get("daily_budget")
        legacy = value_keys == frozenset(legacy_keys)
        if (
            isinstance(daily_budget, bool)
            or not isinstance(daily_budget, int)
            or not 0 <= daily_budget <= (100 if legacy else 1)
        ):
            return False
        if not legacy and not isinstance(value.get("sandbox_enabled"), bool):
            return False
        for key in ("cooldown_seconds", "dismiss_cooldown_seconds"):
            duration = value.get(key)
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or not 0 <= duration <= 30 * 86400
            ):
                return False
        quiet = value.get("quiet_hours")
        if quiet is not None and not cls._valid_quiet_hours(quiet):
            return False
        if not legacy:
            timezone_name = str(value.get("timezone") or "")
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                return False
            if isinstance(quiet, dict) and quiet.get("timezone") != timezone_name:
                return False
        return cls._aware_time(value.get("updated_at")) is not None

    @staticmethod
    def _policy_timezone(value: dict[str, Any]) -> str:
        return str(value.get("timezone") or "UTC")

    @staticmethod
    def _valid_quiet_hours(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "start_hour",
            "end_hour",
            "timezone",
        }:
            return False
        start = value.get("start_hour")
        end = value.get("end_hour")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start <= 23
            or not 0 <= end <= 23
            or start == end
        ):
            return False
        try:
            ZoneInfo(str(value.get("timezone") or ""))
        except (ZoneInfoNotFoundError, ValueError):
            return False
        return True

    @classmethod
    def _valid_proposal_record(cls, proposal_id: Any, value: Any) -> bool:
        if (
            not isinstance(proposal_id, str)
            or not proposal_id.startswith("sug_")
            or len(proposal_id) > 120
            or not isinstance(value, dict)
            or value.get("proposal_id") != proposal_id
            or value.get("proposal_kind") != "informational"
            or cls._proposal_scope_key(value) is None
            or not isinstance(value.get("general_situation_id"), str)
            or not value.get("general_situation_id")
        ):
            return False
        parent_revision = value.get("parent_revision")
        if (
            isinstance(parent_revision, bool)
            or not isinstance(parent_revision, int)
            or parent_revision < 1
        ):
            return False
        mode = value.get("mode")
        status = value.get("status")
        allowed_statuses = {
            "record_only": {"recorded"},
            "shadow": {"would_suggest"},
            "advise_only": {"pending", "acknowledged", "dismissed"},
        }
        if mode not in allowed_statuses or status not in allowed_statuses[mode]:
            return False
        if (
            not isinstance(value.get("reason"), str)
            or value.get("decision_disposition")
            not in cls.INTERACTION_DECISIONS
            or value.get("delivery_disposition")
            not in cls.DELIVERY_DISPOSITIONS
            or value.get("delivery_disposition")
            != (
                "owner_scoped_console"
                if mode == "advise_only"
                else "none"
            )
            or not isinstance(value.get("why_now"), list)
            or not isinstance(value.get("evidence"), list)
            or not isinstance(value.get("unknowns"), list)
            or not cls._valid_options(value.get("options"))
            or not cls._valid_delivery(value.get("delivery"), mode=mode)
            or value.get("authority") != cls._authority_boundary()
            or cls._aware_time(value.get("created_at")) is None
            or cls._aware_time(value.get("updated_at")) is None
        ):
            return False
        schema_version = value.get("schema_version")
        if schema_version == "veyra.informational_suggestion.v1":
            return True
        if schema_version != cls.PROPOSAL_SCHEMA_VERSION:
            return False
        expected_keys = set(cls._V2_PROPOSAL_BASE_KEYS)
        if status == "acknowledged":
            expected_keys.add("acknowledged_at")
        elif status == "dismissed":
            expected_keys.add("dismissed_at")
            # A pending proposal may be dismissed directly, while a proposal
            # dismissed after acknowledgement retains that earlier lifecycle
            # timestamp. No other lifecycle-shaped fields are accepted.
            if "acknowledged_at" in value:
                expected_keys.add("acknowledged_at")
        if set(value) != expected_keys:
            return False
        score = value.get("score")
        attention_ref = value.get("attention_hypothesis_ref")
        binding = value.get("assessment_binding")
        if (
            not cls._current_proposal_integrity(value)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0.0 <= float(score) <= 1.0
            or not isinstance(value.get("upstream_scorer_version"), str)
            or not value.get("upstream_scorer_version")
            or not isinstance(value.get("attention_readiness"), dict)
            or not isinstance(value.get("evidence_diversity"), dict)
            or not isinstance(binding, dict)
            or binding.get("scorer_version")
            != value.get("upstream_scorer_version")
            or not isinstance(attention_ref, dict)
            or set(attention_ref) != {
                "hypothesis_id",
                "hypothesis_revision",
                "ruleset_version",
                "readiness_semantics",
            }
            or not str(attention_ref.get("hypothesis_id") or "").startswith(
                "ahyp_"
            )
            or attention_ref.get("ruleset_version")
            != cls.ATTENTION_HYPOTHESIS_RULESET_VERSION
            or attention_ref.get("readiness_semantics")
            != cls.ATTENTION_READINESS_SEMANTICS
            or cls._aware_time(value.get("source_expires_at")) is None
        ):
            return False
        hypothesis_revision = attention_ref.get("hypothesis_revision")
        created_at = cls._aware_time(value.get("created_at"))
        updated_at = cls._aware_time(value.get("updated_at"))
        acknowledged_at = cls._aware_time(value.get("acknowledged_at"))
        dismissed_at = cls._aware_time(value.get("dismissed_at"))
        lifecycle_valid = bool(
            created_at is not None
            and updated_at is not None
            and created_at <= updated_at
            and (
                status != "acknowledged"
                or (
                    acknowledged_at is not None
                    and created_at <= acknowledged_at <= updated_at
                )
            )
            and (
                status != "dismissed"
                or (
                    dismissed_at is not None
                    and created_at <= dismissed_at <= updated_at
                    and (
                        acknowledged_at is None
                        or created_at
                        <= acknowledged_at
                        <= dismissed_at
                    )
                )
            )
        )
        return bool(
            not isinstance(hypothesis_revision, bool)
            and isinstance(hypothesis_revision, int)
            and hypothesis_revision >= 1
            and lifecycle_valid
        )

    @staticmethod
    def _valid_options(value: Any) -> bool:
        if not isinstance(value, list) or len(value) != 3:
            return False
        expected = {"inspect_evidence", "acknowledge", "dismiss"}
        selected: set[str] = set()
        for item in value:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "execution_allowed"}
                or not isinstance(item.get("id"), str)
                or item.get("execution_allowed") is not False
            ):
                return False
            selected.add(item["id"])
        return selected == expected

    @staticmethod
    def _valid_delivery(value: Any, *, mode: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "channel",
            "external_delivery",
            "feishu_delivery",
            "agent_delivery",
        }:
            return False
        return bool(
            value.get("channel")
            == ("owner_scoped_console" if mode == "advise_only" else "none")
            and value.get("external_delivery") is False
            and value.get("feishu_delivery") is False
            and value.get("agent_delivery") is False
        )

    @classmethod
    def _valid_decision_record(cls, decision_id: Any, value: Any) -> bool:
        if (
            not isinstance(decision_id, str)
            or not re.fullmatch(r"idec_[0-9a-f]{24}", decision_id)
            or not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "decision_id",
                "user_id",
                "session_id",
                "general_situation_id",
                "parent_revision",
                "attention_hypothesis_ref",
                "assessment_binding_digest",
                "mode",
                "mode_epoch",
                "decision_disposition",
                "delivery_disposition",
                "reason",
                "proposal_id",
                "created_at",
                "updated_at",
                "authority",
            }
            or value.get("schema_version") != cls.DECISION_SCHEMA_VERSION
            or value.get("decision_id") != decision_id
            or cls._proposal_scope_key(value) is None
            or not isinstance(value.get("general_situation_id"), str)
            or not value.get("general_situation_id")
            or isinstance(value.get("parent_revision"), bool)
            or not isinstance(value.get("parent_revision"), int)
            or value.get("parent_revision") < 1
            or value.get("mode") not in cls.MODES
            or isinstance(value.get("mode_epoch"), bool)
            or not isinstance(value.get("mode_epoch"), int)
            or value.get("mode_epoch") < 0
            or value.get("decision_disposition") not in cls.INTERACTION_DECISIONS
            or value.get("delivery_disposition") not in cls.DELIVERY_DISPOSITIONS
            or not isinstance(value.get("reason"), str)
            or len(value.get("reason") or "") > 240
            or value.get("authority") != cls._authority_boundary()
            or cls._aware_time(value.get("created_at")) is None
            or cls._aware_time(value.get("updated_at")) is None
            or value.get("decision_id") != cls.decision_id_for(value)
        ):
            return False
        ref = value.get("attention_hypothesis_ref")
        if (
            not isinstance(ref, dict)
            or set(ref)
            != {
                "hypothesis_id",
                "hypothesis_revision",
                "ruleset_version",
                "readiness_semantics",
            }
            or not str(ref.get("hypothesis_id") or "").startswith("ahyp_")
            or isinstance(ref.get("hypothesis_revision"), bool)
            or not isinstance(ref.get("hypothesis_revision"), int)
            or ref.get("hypothesis_revision") < 1
            or ref.get("ruleset_version")
            != cls.ATTENTION_HYPOTHESIS_RULESET_VERSION
            or ref.get("readiness_semantics")
            != cls.ATTENTION_READINESS_SEMANTICS
            or not isinstance(value.get("assessment_binding_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["assessment_binding_digest"])
        ):
            return False
        proposal_id = value.get("proposal_id")
        if proposal_id is not None and (
            not isinstance(proposal_id, str)
            or not proposal_id.startswith("sug_")
        ):
            return False
        created_at = cls._aware_time(value.get("created_at"))
        updated_at = cls._aware_time(value.get("updated_at"))
        if created_at is None or updated_at is None or updated_at < created_at:
            return False
        mode = value.get("mode")
        delivery = value.get("delivery_disposition")
        if mode == "advise_only":
            if delivery not in {"owner_scoped_console", "suppressed"}:
                return False
        elif delivery not in {"none", "suppressed"}:
            return False
        return True

    @classmethod
    def _valid_feedback_record(
        cls,
        feedback_key: Any,
        value: Any,
        *,
        proposals: dict[str, Any],
    ) -> bool:
        if not isinstance(feedback_key, str) or not isinstance(value, dict):
            return False
        required = {
            "user_id",
            "session_id",
            "proposal_id",
            "status",
            "reason",
            "recorded_at",
            "cooldown_until",
        }
        if set(value) != required:
            return False
        proposal_id = value.get("proposal_id")
        if not isinstance(proposal_id, str):
            return False
        proposal = proposals.get(proposal_id)
        scope_key = cls._proposal_scope_key(value)
        if (
            scope_key is None
            or not isinstance(proposal, dict)
            or feedback_key
            != f"{scope_key}:{str(proposal.get('general_situation_id') or '')}"
            or proposal.get("user_id") != value.get("user_id")
            or proposal.get("session_id") != value.get("session_id")
            or value.get("status") not in {"acknowledged", "dismissed"}
            or proposal.get("status") != value.get("status")
            or not isinstance(value.get("reason"), str)
            or len(value.get("reason")) > 500
            or cls._aware_time(value.get("recorded_at")) is None
            or cls._aware_time(value.get("cooldown_until")) is None
        ):
            return False
        return True

    @classmethod
    def _valid_daily_counter(cls, counter_key: Any, value: Any) -> bool:
        if (
            not isinstance(counter_key, str)
            or not isinstance(value, dict)
            or set(value) != {
                "owner_budget_key",
                "day",
                "count",
                "updated_at",
            }
        ):
            return False
        owner_key = value.get("owner_budget_key")
        day = value.get("day")
        count = value.get("count")
        if (
            not isinstance(owner_key, str)
            or not owner_key.startswith("owner-")
            or len(owner_key) != 70
            or not isinstance(day, str)
            or counter_key != f"{owner_key}:{day}"
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count != 1
            or cls._aware_time(value.get("updated_at")) is None
        ):
            return False
        try:
            return datetime.strptime(day, "%Y-%m-%d").date().isoformat() == day
        except ValueError:
            return False

    @staticmethod
    def _proposal_scope_key(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        try:
            user = normalize_scope_component(value.get("user_id"), "user_id")
            session = normalize_scope_component(
                value.get("session_id"),
                "session_id",
            )
        except (TypeError, ValueError):
            return None
        if user != value.get("user_id") or session != value.get("session_id"):
            return None
        return tenant_scope_storage_key(user, session)

    @classmethod
    def _require_healthy(cls, state: dict[str, Any]) -> None:
        if not cls._healthy(state):
            raise SuggestionOutboxConflict("suggestion outbox state is corrupt")

    @staticmethod
    def _records(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): copy.deepcopy(item)
            for key, item in value.items()
            if isinstance(item, dict)
        }

    @staticmethod
    def _list_records(value: Any) -> dict[str, list[Any]]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): copy.deepcopy(item)
            for key, item in value.items()
            if isinstance(item, list)
        }

    @staticmethod
    def _required_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("expected_state_revision must be non-negative")
        return value

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _aware_time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            selected = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                selected = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if selected.tzinfo is None or selected.utcoffset() is None:
            return None
        return selected.astimezone(timezone.utc)

    def _owner(self, user_id: Any, session_id: Any) -> tuple[str, str, str]:
        user = normalize_scope_component(user_id, "user_id")
        session = normalize_scope_component(session_id, "session_id")
        return user, session, tenant_scope_storage_key(user, session)

    def _now(self) -> datetime:
        selected = self._clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("suggestion outbox clock must be timezone-aware")
        return selected.astimezone(timezone.utc)
