from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.context_scope import tenant_scope_storage_key
from core.world_state import WorldStateStore
from interface.general_situation_contract import stable_digest
from memory_bridge.scope import normalize_scope_component


class SuggestionOutboxConflict(RuntimeError):
    pass


class SuggestionOutbox:
    """Persist informational proposals without notification or action authority."""

    STATE_FILE = "suggestion_outbox.json"
    SCHEMA_VERSION = "veyra.suggestion_outbox.v1"
    PROPOSAL_SCHEMA_VERSION = "veyra.informational_suggestion.v1"
    MODES = {"disabled", "record_only", "shadow", "advise_only"}
    DEFAULT_MODE = "record_only"
    MAX_PROPOSALS = 2000
    MAX_INBOX_ITEMS = 100
    DEFAULT_POLICY = {
        "daily_budget": 3,
        "quiet_hours": None,
        "cooldown_seconds": 3600,
        "dismiss_cooldown_seconds": 86400,
    }

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
        daily_budget: int,
        quiet_hours: dict[str, Any] | None,
        cooldown_seconds: int,
        dismiss_cooldown_seconds: int,
        expected_state_revision: int,
    ) -> dict[str, Any]:
        user, session, scope_key = self._owner(user_id, session_id)
        self._required_revision(expected_state_revision)
        if (
            isinstance(daily_budget, bool)
            or not isinstance(daily_budget, int)
            or not 0 <= daily_budget <= 100
        ):
            raise ValueError("daily_budget must be an integer from 0 to 100")
        for name, value in (
            ("cooldown_seconds", cooldown_seconds),
            ("dismiss_cooldown_seconds", dismiss_cooldown_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30 * 86400:
                raise ValueError(f"{name} must be an integer from 0 to 2592000")
        selected_quiet = self._quiet_hours(quiet_hours)
        policy = {
            "user_id": user,
            "session_id": session,
            "daily_budget": daily_budget,
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
        except (TypeError, ValueError) as exc:
            return self._closed("invalid_suggestion_binding", detail=type(exc).__name__)
        config = self._mode_snapshot()
        if config.get("status") == "fail_closed":
            return config
        mode = str(config["mode"])
        if mode == "disabled":
            return {
                "status": "disabled",
                "mode": mode,
                "proposal": None,
                "authority": self._authority_boundary(),
            }
        if assessment.get("eligible") is not True or assessment.get("status") != "eligible":
            return {
                "status": "not_proposed",
                "mode": mode,
                "reason": "structured_attention_not_eligible",
                "proposal": None,
                "authority": self._authority_boundary(),
            }
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy(state):
            return self._closed("suggestion_outbox_state_corrupt")
        now = self._now()
        proposal_id = "sug_" + stable_digest(
            "veyra.informational_suggestion.identity.v1",
            {
                "general_situation_id": general_situation.get(
                    "general_situation_id"
                ),
                "parent_revision": general_situation.get("parent_revision"),
                "assessment": {
                    "scorer_version": assessment.get("scorer_version"),
                    "score": assessment.get("score"),
                    "components": assessment.get("components"),
                },
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
            proposals = self._records(current.get("proposals"))
            if proposal_id in proposals:
                result = {
                    "status": "replayed",
                    "mode": mode,
                    "proposal": self._public_proposal(proposals[proposal_id]),
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
                    policy=policy,
                    now=now,
                )
                if suppression is not None:
                    result = {
                        "status": "suppressed",
                        "mode": mode,
                        "reason": suppression,
                        "proposal": None,
                        "authority": self._authority_boundary(),
                    }
                    return current

            proposal = self._proposal(
                proposal_id=proposal_id,
                mode=mode,
                general_situation=general_situation,
                assessment=assessment,
                user_id=user,
                session_id=session,
                now=now,
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
                day_key = self._day_key(now, policy)
                counter_key = f"{scope_key}:{day_key}"
                counter = counters.get(counter_key) or {
                    "scope_key": scope_key,
                    "day": day_key,
                    "count": 0,
                }
                counter["count"] = self._nonnegative_int(counter.get("count")) + 1
                counter["updated_at"] = now.isoformat()
                counters[counter_key] = counter
                current["daily_counters"] = counters
            current["updated_at"] = now.isoformat()
            result = {
                "status": str(proposal.get("status") or "recorded"),
                "mode": mode,
                "proposal": self._public_proposal(proposal),
                "authority": self._authority_boundary(),
            }
            return current

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result or self._closed("suggestion_outbox_mutation_no_result")

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
        state = self.state_store.read_json(self.STATE_FILE)
        if not self._healthy(state):
            return self._closed("suggestion_outbox_state_corrupt")
        proposals = self._records(state.get("proposals"))
        inboxes = self._list_records(state.get("owner_inboxes"))
        ids = [str(item) for item in inboxes.get(scope_key, []) if str(item)]
        items = [
            self._public_proposal(proposals[proposal_id])
            for proposal_id in reversed(ids)
            if proposal_id in proposals
            and str(proposals[proposal_id].get("user_id") or "") == user
            and str(proposals[proposal_id].get("session_id") or "") == session
        ]
        selected_limit = max(0, min(int(limit), self.MAX_INBOX_ITEMS))
        policy = self._effective_policy(self._records(state.get("policies")).get(scope_key))
        return {
            "status": "success",
            "count": min(len(items), selected_limit),
            "items": items[:selected_limit],
            "policy": self._public_policy(policy),
            "state_revision": self._nonnegative_int(state.get("_state_revision")),
            "authority": self._authority_boundary(),
        }

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
        now = self._now()
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            self._require_healthy(state)
            current_revision = self._nonnegative_int(state.get("_state_revision"))
            if current_revision != expected_state_revision:
                raise SuggestionOutboxConflict(
                    "expected_state_revision does not match suggestion outbox"
                )
            proposals = self._records(state.get("proposals"))
            proposal = proposals.get(selected_id)
            if not isinstance(proposal, dict):
                raise KeyError("suggestion proposal was not found")
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
            proposal[f"{transition}_at"] = now.isoformat()
            proposal["updated_at"] = now.isoformat()
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
                "recorded_at": now.isoformat(),
                "cooldown_until": (
                    now + timedelta(seconds=cooldown_seconds)
                ).isoformat(),
            }
            state["proposals"] = proposals
            state["feedback"] = feedback
            state["updated_at"] = now.isoformat()
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
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        status = {
            "record_only": "recorded",
            "shadow": "would_suggest",
            "advise_only": "pending",
        }[mode]
        components = assessment.get("components") if isinstance(assessment.get("components"), dict) else {}
        why_now = [
            {
                "component": str(name),
                "value": value.get("value"),
                "weight": value.get("weight"),
                "source": value.get("source"),
            }
            for name, value in sorted(components.items())
            if isinstance(value, dict)
        ]
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
            "why_now": why_now,
            "score": assessment.get("score"),
            "evidence": copy.deepcopy(assessment.get("evidence") or []),
            "unknowns": copy.deepcopy(assessment.get("unknowns") or []),
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
        return proposal

    def _surface_suppression(
        self,
        state: dict[str, Any],
        *,
        general_situation_id: str,
        scope_key: str,
        policy: dict[str, Any],
        now: datetime,
    ) -> str | None:
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
        counter = self._records(state.get("daily_counters")).get(
            f"{scope_key}:{day_key}"
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
        return {
            "status": "success",
            "mode": mode,
            "mode_epoch": self._nonnegative_int(section.get("mode_epoch")),
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
        cooldown = value.get("cooldown_seconds")
        dismiss = value.get("dismiss_cooldown_seconds")
        return {
            "daily_budget": (
                daily_budget
                if isinstance(daily_budget, int)
                and not isinstance(daily_budget, bool)
                and 0 <= daily_budget <= 100
                else self.DEFAULT_POLICY["daily_budget"]
            ),
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
                "daily_budget",
                "quiet_hours",
                "cooldown_seconds",
                "dismiss_cooldown_seconds",
                "updated_at",
            )
            if key in value
        }

    @staticmethod
    def _public_proposal(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"user_id", "session_id"}
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
        quiet = policy.get("quiet_hours")
        timezone_name = (
            str(quiet.get("timezone"))
            if isinstance(quiet, dict)
            else "UTC"
        )
        return now.astimezone(ZoneInfo(timezone_name)).date().isoformat()

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
        return str(state.get("schema_version") or "") in {
            "",
            cls.SCHEMA_VERSION,
        }

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
