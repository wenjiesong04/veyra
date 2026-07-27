from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.model_client import redact_sensitive
from core.state_proposal import (
    ProposalNotFoundError,
    StateChangeProposal,
    StateChangeProposalStore,
    deterministic_idempotency_key,
    deterministic_proposal_id,
)
from core.world_state import WorldStateStore
from interface.event_schema import Decision, LoopResult, VeyraEvent, utc_now_iso


MEMORY_POLICIES = {"forget", "short_term", "long_term"}


def normalize_memory_policy(value: Any, *, default: str = "forget") -> str:
    if isinstance(value, dict):
        value = value.get("mode") or value.get("policy") or default
    normalized = str(value or default).strip().lower()
    aliases = {
        "ignore": "forget",
        "none": "forget",
        "session": "short_term",
        "temporary": "short_term",
        "working": "short_term",
        "persistent": "long_term",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in MEMORY_POLICIES else default


class MemoryPolicyRuntime:
    """Applies per-turn memory policy after verification."""

    def __init__(self, state_store: WorldStateStore, long_term_writer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.state_store = state_store
        self.long_term_writer = long_term_writer
        self.state_proposals = StateChangeProposalStore(state_store)

    def apply(self, event: VeyraEvent, decision: Decision, result: LoopResult) -> dict[str, Any]:
        policy = normalize_memory_policy(decision.memory_policy)
        if policy == "forget":
            return {"status": "skipped", "policy": policy, "reason": "turn marked forget"}
        if policy == "short_term":
            return self._write_short_term(event, decision, result)
        if not self._semantic_memory_allowed(decision):
            return {
                "status": "skipped",
                "policy": policy,
                "reason": "semantic policy did not authorize durable memory",
            }
        verification = (
            result.artifacts.get("verification")
            if isinstance(result.artifacts.get("verification"), dict)
            else {}
        )
        user_attested_preference = (
            result.route.value == "direct_answer"
            and result.status == "success"
            and (decision.intent == "preference" or "memory:preference" in decision.signals)
        )
        if verification.get("status") != "verified_success" and not user_attested_preference:
            return {
                "status": "skipped",
                "policy": policy,
                "reason": "durable memory requires verified_success or an explicit user-attested preference",
                "verification_status": verification.get("status") or "missing",
            }
        if result.status not in {"success", "verified_success"}:
            return {"status": "skipped", "policy": policy, "reason": f"result status {result.status} is not stable enough for long-term memory"}
        patch = {
            "session_id": event.source.session_id,
            "task": str(event.payload.get("text", ""))[:1200],
            "route": result.route.value,
            "status": result.status,
            "risk_level": result.risk_level.value,
            "result": redact_sensitive(result.response, max_string=1200),
            "freshness": "fresh",
            "trust": "user_attested" if user_attested_preference else "verified",
            "verification_status": verification.get("status") or "user_attested_preference",
            "memory_policy": policy,
        }
        if decision.intent == "preference" or "memory:preference" in decision.signals:
            patch.update(
                {
                    "memory_type": "user_preference",
                    "preference": {
                        "source_text": str(event.payload.get("text", ""))[:500],
                        "scope": "response_style",
                    },
                }
            )
        return self._commit_long_term_memory(
            patch=patch,
            writer=self.long_term_writer,
            identity_parts=(event.event_id, "turn_memory"),
            decision=decision,
            base={"policy": policy},
            preconditions=[
                "semantic policy explicitly authorizes memory.write",
                "turn result is stable enough for durable memory",
            ],
        )

    def _semantic_memory_allowed(self, decision: Decision) -> bool:
        assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        semantic = assist.get("semantic_policy")
        if not isinstance(semantic, dict):
            # Legacy callers keep their existing behavior until they provide a
            # semantic frame; the AwarenessLoop now always provides one.
            return True
        allowed = {str(item) for item in semantic.get("allowed_effects", []) if isinstance(item, str)}
        denied = {str(item) for item in semantic.get("denied_effects", []) if isinstance(item, str)}
        return "memory.write" in allowed and "memory.write" not in denied

    def apply_agent_result(
        self,
        *,
        task_context: dict[str, Any],
        execution: Any,
        verification: dict[str, Any],
        context_found: bool,
        long_term_writer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Apply the original task's memory policy to an asynchronous Agent result."""

        policy = normalize_memory_policy(task_context.get("memory_policy"), default="forget")
        task_id = str(getattr(execution, "task_id", "") or task_context.get("task_id") or "")
        session_id = str(task_context.get("session_id") or "")
        correlation_id = str(
            task_context.get("correlation_id")
            or task_context.get("event_id")
            or task_context.get("task_packet_id")
            or task_id
        )
        base = {
            "policy": policy,
            "task_id": task_id,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "context_authority": str(task_context.get("authority") or "missing"),
            "verification_status": str(verification.get("status") or "missing"),
        }
        if not context_found or str(task_context.get("authority") or "") != "veyra_registered":
            return {
                **base,
                "status": "skipped",
                "reason": "authoritative original task context missing; callback payload cannot authorize memory",
            }
        if policy == "forget":
            return {**base, "status": "skipped", "reason": "original task marked forget"}
        if policy == "short_term":
            return self._write_agent_short_term(
                task_context=task_context,
                execution=execution,
                verification=verification,
                base=base,
            )
        if verification.get("status") != "verified_success" or not bool(verification.get("needs_memory_patch")):
            return {
                **base,
                "status": "skipped",
                "reason": "durable Agent memory requires verifier-approved memory patch",
            }
        if str(getattr(execution, "status", "") or "") != "success":
            return {
                **base,
                "status": "skipped",
                "reason": "durable Agent memory requires a terminal successful execution",
            }
        if not session_id:
            return {**base, "status": "skipped", "reason": "original task session is missing"}
        task_packet = task_context.get("task_packet") if isinstance(task_context.get("task_packet"), dict) else {}
        task = str(
            task_context.get("user_goal")
            or task_context.get("task")
            or task_packet.get("user_goal")
            or task_packet.get("user_message")
            or task_id
        )
        patch = {
            "session_id": session_id,
            "task": task[:1200],
            "task_id": task_id,
            "correlation_id": correlation_id,
            "agent_execution_session_id": str(
                task_context.get("agent_execution_session_id")
                or task_packet.get("agent_execution_session_id")
                or ""
            ),
            "executor": str(getattr(execution, "executor", "") or task_context.get("executor") or ""),
            "status": str(getattr(execution, "status", "") or ""),
            "result": redact_sensitive(
                self._trusted_execution_summary(execution, verification),
                max_string=1200,
            ),
            "freshness": "fresh",
            "trust": "verified",
            "verification_status": "verified_success",
            "verification_verdict": str(verification.get("verdict") or ""),
            "memory_policy": policy,
        }
        writer = long_term_writer or self.long_term_writer
        return self._commit_long_term_memory(
            patch=patch,
            writer=writer,
            identity_parts=(correlation_id, task_id or "agent_result", "verified_agent_memory"),
            decision=None,
            base=base,
            preconditions=[
                "authoritative Veyra task context exists",
                "execution and verification are terminal verified_success",
                "verifier requested a durable memory patch",
            ],
        )

    def _commit_long_term_memory(
        self,
        *,
        patch: dict[str, Any],
        writer: Callable[[dict[str, Any]], dict[str, Any]],
        identity_parts: tuple[str, ...],
        decision: Decision | None,
        base: dict[str, Any],
        preconditions: list[str],
    ) -> dict[str, Any]:
        key = deterministic_idempotency_key(*identity_parts, namespace="veyra.memory-write.v1")
        proposal = self._existing_or_create_proposal(
            idempotency_key=key,
            patch=patch,
            decision=decision,
            preconditions=preconditions,
        )

        def apply_memory(_: StateChangeProposal) -> dict[str, Any]:
            written = writer(patch)
            return written if isinstance(written, dict) else {"status": "submitted"}

        committed = self.state_proposals.commit(proposal.proposal_id, apply_memory)
        result_payload = committed.model_dump(mode="json")
        if committed.status != "committed":
            return {
                **base,
                "status": committed.status,
                "reason": committed.error or "durable memory proposal did not commit",
                "state_change": result_payload,
            }
        written = committed.output if isinstance(committed.output, dict) else {"status": "submitted"}
        return {
            **base,
            "status": str(written.get("status") or "written"),
            "write": written,
            "state_change": result_payload,
        }

    def _existing_or_create_proposal(
        self,
        *,
        idempotency_key: str,
        patch: dict[str, Any],
        decision: Decision | None,
        preconditions: list[str],
    ) -> StateChangeProposal:
        proposal_id = deterministic_proposal_id(idempotency_key)
        existing: StateChangeProposal | None = None
        try:
            existing = self.state_proposals.get(proposal_id)
        except ProposalNotFoundError:
            pass
        operation, target, source_span, explicitness = self._semantic_metadata(decision)
        expected_revisions = (
            dict(existing.expected_state_revisions)
            if existing is not None
            else {
                "agent_memory.json": int(
                    self.state_store.read_json("agent_memory.json").get("_state_revision")
                    or 0
                )
            }
        )
        return self.state_proposals.create_or_get(
            effect="memory.write",
            operation=operation,
            target=target,
            source_span=source_span,
            explicitness=explicitness,
            preconditions=preconditions,
            payload={"patch": patch},
            idempotency_key=idempotency_key,
            expected_state_revisions=expected_revisions,
            requires_approval=False,
        )

    @staticmethod
    def _semantic_metadata(
        decision: Decision | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None, str]:
        if decision is None:
            return (
                "persist_verified_agent_result",
                {"type": "agent_result", "value": "", "scope": "task_session", "attributes": {}},
                None,
                "explicit",
            )
        assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
        frame = assist.get("semantic_frame") if isinstance(assist.get("semantic_frame"), dict) else {}
        policy = assist.get("semantic_policy") if isinstance(assist.get("semantic_policy"), dict) else {}
        authoritative = {
            str(item)
            for item in policy.get("authoritative_act_ids", [])
            if isinstance(item, str)
        }
        acts = frame.get("acts") if isinstance(frame.get("acts"), list) else []
        act = next(
            (
                item
                for item in acts
                if isinstance(item, dict) and str(item.get("act_id") or "") in authoritative
            ),
            {},
        )
        target = act.get("target") if isinstance(act.get("target"), dict) else {}
        source_span = act.get("source_quote") if isinstance(act.get("source_quote"), dict) else None
        explicitness = str(act.get("explicitness") or "unknown")
        if explicitness not in {"explicit", "strong_implied", "weak_implied", "inferred", "unknown"}:
            explicitness = "unknown"
        return (
            str(act.get("operation") or "remember_user_preference"),
            {
                "type": str(target.get("type") or "user_memory"),
                "value": str(target.get("value") or ""),
                "scope": "current_user",
                "attributes": target.get("attributes") if isinstance(target.get("attributes"), dict) else {},
            },
            source_span,
            explicitness,
        )

    def _write_short_term(self, event: VeyraEvent, decision: Decision, result: LoopResult) -> dict[str, Any]:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        item = {
            "event_id": event.event_id,
            "session_id": event.source.session_id,
            "route": result.route.value,
            "status": result.status,
            "risk_level": result.risk_level.value,
            "intent": decision.intent,
            "summary": redact_sensitive(result.response, max_string=800),
            "created_at": utc_now_iso(),
            "expires_at": expires_at,
            "memory_class": "soft",
            "memory_namespace": "task_short_term",
        }

        def update_short_term(state: dict[str, Any]) -> dict[str, Any]:
            short_term = state.get("short_term_memory") if isinstance(state.get("short_term_memory"), list) else []
            now = datetime.now(timezone.utc)
            active = [
                candidate
                for candidate in short_term
                if isinstance(candidate, dict) and not self._is_expired(candidate, now)
            ]
            state["short_term_memory"] = (active + [item])[-50:]
            return state

        self.state_store.mutate_json("task_state.json", update_short_term)
        return {"status": "written", "policy": "short_term", "item": item}

    def _write_agent_short_term(
        self,
        *,
        task_context: dict[str, Any],
        execution: Any,
        verification: dict[str, Any],
        base: dict[str, Any],
    ) -> dict[str, Any]:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        item = {
            "event_id": str(task_context.get("event_id") or base["correlation_id"]),
            "task_id": base["task_id"],
            "session_id": base["session_id"],
            "correlation_id": base["correlation_id"],
            "route": str(task_context.get("route") or "agent"),
            "status": str(getattr(execution, "status", "") or ""),
            "verification_status": str(verification.get("status") or "missing"),
            "summary": redact_sensitive(
                self._trusted_execution_summary(execution, verification),
                max_string=800,
            ),
            "created_at": utc_now_iso(),
            "expires_at": expires_at,
            "memory_class": "soft",
            "memory_namespace": "agent_callback_short_term",
        }

        def update_short_term(state: dict[str, Any]) -> dict[str, Any]:
            short_term = state.get("short_term_memory") if isinstance(state.get("short_term_memory"), list) else []
            now = datetime.now(timezone.utc)
            active = [
                candidate
                for candidate in short_term
                if isinstance(candidate, dict) and not self._is_expired(candidate, now)
            ]
            state["short_term_memory"] = (active + [item])[-50:]
            return state

        self.state_store.mutate_json("task_state.json", update_short_term)
        return {**base, "status": "written", "item": item}

    @staticmethod
    def _trusted_execution_summary(
        execution: Any,
        verification: dict[str, Any],
    ) -> str:
        projection = verification.get("verified_projection")
        if (
            verification.get("status") == "verified_success"
            and verification.get("claim_scope")
            == "authoritative_verified_projection_only"
            and isinstance(projection, dict)
        ):
            summary = str(projection.get("summary") or "").strip()
            return (
                summary
                or "Tool effects were verified by Veyra authoritative evidence."
            )
        return str(getattr(execution, "result", "") or "")

    def _is_expired(self, item: dict[str, Any], now: datetime) -> bool:
        value = str(item.get("expires_at") or "")
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= now
