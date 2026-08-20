#!/usr/bin/env python3
"""CAS, replay, and authority-boundary smoke for Living Context."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import CandidateNeed, ContextQuote, LivingContextCandidate  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"ok - {label}")


def event(event_id: str, text: str, *, user_id: str = "boundary-user", session_id: str = "boundary-session") -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
        event_id=event_id,
    )


def create_candidate(subject: str = "boundary concern") -> LivingContextCandidate:
    return LivingContextCandidate(
        schema_version="veyra.living_context_candidate.v1",
        disposition="create",
        create_subject=subject,
        title=subject,
        summary="A bounded concern",
        goal="Understand it",
        lifecycle="active",
        unknown=["a missing fact"],
        needs=[
            CandidateNeed(
                blocked_judgment="a missing fact",
                evidence_kind="user",
                why_now="It changes the next step",
                allowed_source_classes=["user"],
                fallback_reaction="ask",
                question="What is the missing fact?",
            )
        ],
        requested_reaction="ask",
        source="model",
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-living-context-boundary-") as tmp:
        store = WorldStateStore(tmp)
        runtime = LivingContextRuntime(store)
        first_event = event("evt_boundary_create", "I have a bounded concern")
        first = runtime.process_user_turn(
            first_event,
            SimpleNamespace(living_context_candidate=create_candidate()),
        )
        situation = first["situation"]
        sid = str(situation["situation_id"])
        revision = int(situation["observation_revision"])

        wrong_session = event("evt_boundary_wrong_session", "attempt", session_id="other-session")
        catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
        catalog_row = next(item for item in catalog if str(item["situation_token"]) == sid)
        update = LivingContextCandidate(
            schema_version="veyra.living_context_candidate.v1",
            disposition="update",
            situation_token=sid,
            situation_revision=int(catalog_row["observation_revision"]),
            catalog_token=str(catalog_row["catalog_token"]),
            title="attempt",
            summary="attempt",
            lifecycle="active",
            source="model",
        )
        try:
            runtime.process_user_turn(
                wrong_session,
                SimpleNamespace(living_context_candidate=update),
                catalog=catalog,
            )
        except (KeyError, StateRevisionConflictError, PermissionError):
            expect(True, "cross-session token cannot be used")
        else:  # pragma: no cover
            expect(False, "cross-session token cannot be used")

        right_event = event("evt_boundary_update", "new material fact")
        catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
        catalog_row = next(item for item in catalog if str(item["situation_token"]) == sid)
        first_update = runtime.process_user_turn(
            right_event,
            SimpleNamespace(
                living_context_candidate=LivingContextCandidate(
                    schema_version="veyra.living_context_candidate.v1",
                    disposition="update",
                    situation_token=sid,
                    situation_revision=int(catalog_row["observation_revision"]),
                    catalog_token=str(catalog_row["catalog_token"]),
                    title="updated concern",
                    summary="new material fact",
                    lifecycle="active",
                    material_change="the concern changed",
                    source="model",
                )
            ),
            catalog=catalog,
            expected_revision=revision,
        )
        expect(int(first_update["situation"]["observation_revision"]) == revision + 1, "CAS update advances revision")
        try:
            current_catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
            current_row = next(item for item in current_catalog if str(item["situation_token"]) == sid)
            runtime.process_user_turn(
                event("evt_boundary_stale", "stale update"),
                SimpleNamespace(
                    living_context_candidate=LivingContextCandidate(
                        schema_version="veyra.living_context_candidate.v1",
                        disposition="update",
                        situation_token=sid,
                        situation_revision=int(current_row["observation_revision"]),
                        catalog_token=str(current_row["catalog_token"]),
                        title="stale",
                        summary="stale",
                        lifecycle="active",
                        source="model",
                    )
                ),
                catalog=current_catalog,
                expected_revision=revision,
            )
        except StateRevisionConflictError:
            expect(True, "stale Situation revision fails closed")
        else:  # pragma: no cover
            expect(False, "stale Situation revision fails closed")

        resolve_text = "concern resolved"
        current_catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
        current_row = next(item for item in current_catalog if str(item["situation_token"]) == sid)
        resolved = runtime.process_user_turn(
            event("evt_boundary_resolve", resolve_text),
            SimpleNamespace(
                living_context_candidate=LivingContextCandidate(
                    schema_version="veyra.living_context_candidate.v1",
                    disposition="resolve",
                    situation_token=sid,
                    situation_revision=int(current_row["observation_revision"]),
                    catalog_token=str(current_row["catalog_token"]),
                    title="resolved concern",
                    summary="resolved",
                    lifecycle="resolved",
                    source_quote=ContextQuote(text=resolve_text, start=0, end=len(resolve_text)),
                    assertion_mode="direct_user",
                    source="model",
                )
            ),
            catalog=current_catalog,
        )
        expect(resolved["situation"]["status"] == "resolved", "resolve enters terminal lifecycle")
        try:
            terminal_catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
            terminal_row = next(item for item in terminal_catalog if str(item["situation_token"]) == sid)
            runtime.process_user_turn(
                event("evt_boundary_revive", "ordinary update"),
                SimpleNamespace(
                    living_context_candidate=LivingContextCandidate(
                        schema_version="veyra.living_context_candidate.v1",
                        disposition="update",
                        situation_token=sid,
                        situation_revision=int(terminal_row["observation_revision"]),
                        catalog_token=str(terminal_row["catalog_token"]),
                        title="revive",
                        summary="revive",
                        lifecycle="active",
                        source="model",
                    )
                ),
                catalog=terminal_catalog,
            )
        except StateRevisionConflictError:
            expect(True, "terminal Situation cannot revive through ordinary update")
        else:  # pragma: no cover
            expect(False, "terminal Situation cannot revive through ordinary update")
        reopen_text = "此前标记解决有误"
        terminal_catalog = runtime.model_catalog(owner_id="boundary-user", session_id="boundary-session")
        terminal_row = next(item for item in terminal_catalog if str(item["situation_token"]) == sid)
        reopened = runtime.process_user_turn(
            event("evt_boundary_reopen", reopen_text),
            SimpleNamespace(
                living_context_candidate=LivingContextCandidate(
                    schema_version="veyra.living_context_candidate.v1",
                    disposition="correct",
                    situation_token=sid,
                    situation_revision=int(terminal_row["observation_revision"]),
                    catalog_token=str(terminal_row["catalog_token"]),
                    title="reopened concern",
                    summary="the prior resolution was incorrect",
                    lifecycle="active",
                    reopen=True,
                    reopen_reason="用户纠正了此前的解决状态",
                    source_quote=ContextQuote(text=reopen_text, start=0, end=len(reopen_text)),
                    assertion_mode="direct_user",
                    source="model",
                )
            ),
            catalog=terminal_catalog,
        )
        expect(reopened["situation"]["status"] == "active", "explicit correct+reopen is audited and admitted")
        expect(reopened["situation"]["semantic_history"][-1]["reopen"] is True, "reopen audit is durable")

        bad_need = CandidateNeed(
            blocked_judgment="x",
            evidence_kind="user",
            why_now="x",
            allowed_source_classes=["tool"],
            fallback_reaction="wait",
        )
        try:
            runtime.needs.upsert_for_situation(
                situation_id=sid,
                owner_id="boundary-user",
                session_id="boundary-session",
                needs=[bad_need],
                source_event_id="evt_bad_need",
            )
        except ValueError:
            expect(True, "InformationNeed source class rejects tool authority")
        else:  # pragma: no cover
            expect(False, "InformationNeed source class rejects tool authority")

        state_files = {path.name for path in Path(tmp, "runtime").glob("*.json")}
        expect("situation_state.json" in state_files, "semantic Situation uses unique Situation truth")
        expect("information_need_state.json" in state_files, "InformationNeed has its only durable state")
        expect("living_context_state.json" not in state_files, "no LivingContext aggregate state was created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
