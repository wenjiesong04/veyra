#!/usr/bin/env python3
"""Counterexamples for the V1 Living Context admission boundaries.

This smoke intentionally exercises the negative space: a model cannot use a
stale or truncated catalog, silently revive a terminal Situation, rebind one
event to new meaning, or cause a Situation write that a Need capacity check
would later reject.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.situation_state_repository import SituationStateError, SituationStateRepository  # noqa: E402
from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import (  # noqa: E402
    CandidateNeed,
    CandidateNeedReference,
    ContextQuote,
    LivingContextCandidate,
)
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402
from runtime.living_context_admission import (  # noqa: E402
    AdmissionRetentionError,
    LivingContextAdmissionLedger,
)
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def event(event_id: str, text: str, *, user_id: str = "adversarial-user", session_id: str = "adversarial-session") -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
        event_id=event_id,
    )


def need(blocked: str) -> CandidateNeed:
    return CandidateNeed(
        blocked_judgment=blocked,
        evidence_kind="user",
        why_now="This missing fact changes the next judgment.",
        urgency=0.7,
        allowed_source_classes=["user"],
        fallback_reaction="ask",
        question=f"What is known about {blocked}?",
    )


def create(subject: str, *, blocked: str | None = None) -> LivingContextCandidate:
    selected_need = need(blocked) if blocked else None
    return LivingContextCandidate(
        schema_version="veyra.living_context_candidate.v1",
        disposition="create",
        create_subject=subject,
        category="general",
        label=subject,
        title=subject,
        summary=f"A user-reported Situation: {subject}",
        goal=f"Make progress on {subject}",
        lifecycle="active",
        unknown=[blocked] if blocked else [],
        needs=[selected_need] if selected_need else [],
        requested_reaction="ask",
        source="model",
    )


def existing(
    row: dict[str, object],
    *,
    disposition: str = "update",
    text: str = "a new user update",
    lifecycle: str = "active",
    needs: list[CandidateNeed] | None = None,
    answered: list[tuple[str, int]] | None = None,
    source_quote: bool = False,
    assertion_mode: str = "inferred",
    reopen: bool = False,
) -> LivingContextCandidate:
    quote = ContextQuote(text=text, start=0, end=len(text)) if source_quote else None
    references = [CandidateNeedReference(need_token=token, generation=generation) for token, generation in (answered or [])]
    return LivingContextCandidate(
        schema_version="veyra.living_context_candidate.v1",
        disposition=disposition,
        situation_token=str(row["situation_token"]),
        situation_revision=int(row["observation_revision"]),
        catalog_token=str(row["catalog_token"]),
        label=str(row.get("label") or "updated Situation"),
        title=str(row.get("title") or "updated Situation"),
        summary=text,
        goal=str(row.get("goal") or "make progress"),
        lifecycle=lifecycle,
        unknown=[],
        needs=needs or [],
        answered_need_tokens=[token for token, _ in (answered or [])],
        answered_need_bindings=references,
        requested_reaction="wait",
        reopen=reopen,
        reopen_reason="explicit user correction" if reopen else "",
        source_quote=quote,
        assertion_mode=assertion_mode,
        source="model",
    )


def process(runtime: LivingContextRuntime, source_event: VeyraEvent, candidate: LivingContextCandidate, *, catalog: list[dict[str, object]] | None = None, expected_revision: int | None = None) -> dict[str, object]:
    return runtime.process_user_turn(
        source_event,
        SimpleNamespace(living_context_candidate=candidate),
        catalog=catalog,
        expected_revision=expected_revision,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-living-context-adversarial-") as tmp:
        store = WorldStateStore(tmp)
        runtime = LivingContextRuntime(store)

        first = process(runtime, event("evt_adv_one", "first concern"), create("first concern"))
        sid = str(first["situation"]["situation_id"])
        second = process(runtime, event("evt_adv_two", "second concern"), create("second concern"))
        sid_two = str(second["situation"]["situation_id"])

        full_catalog = runtime.model_catalog(owner_id="adversarial-user", session_id="adversarial-session")
        row_one = next(item for item in full_catalog if str(item["situation_token"]) == sid)
        row_two = next(item for item in full_catalog if str(item["situation_token"]) == sid_two)

        # A token that is valid in the store but outside this turn's bounded
        # catalog cannot be used by the model.
        try:
            process(
                runtime,
                event("evt_adv_outside_catalog", "outside catalog"),
                existing(row_two),
                catalog=[row_one],
            )
        except StateRevisionConflictError:
            expect(True, "token outside bounded catalog fails closed")
        else:  # pragma: no cover
            expect(False, "token outside bounded catalog fails closed")

        # The old catalog row has a stale revision and a stale binding token
        # after a valid update.
        old_row = dict(row_one)
        update = process(
            runtime,
            event("evt_adv_update", "valid update"),
            existing(row_one),
            catalog=full_catalog,
            expected_revision=int(row_one["observation_revision"]),
        )
        new_catalog = runtime.model_catalog(owner_id="adversarial-user", session_id="adversarial-session")
        try:
            process(
                runtime,
                event("evt_adv_stale_catalog", "stale catalog"),
                existing(old_row),
                catalog=[old_row, row_two],
            )
        except StateRevisionConflictError:
            expect(True, "old catalog revision fails closed")
        else:  # pragma: no cover
            expect(False, "old catalog revision fails closed")
        expect(int(update["situation"]["observation_revision"]) == int(old_row["observation_revision"]) + 1, "valid CAS update advances revision")

        # Need token and generation are bound to the same current catalog row.
        need_first = process(runtime, event("evt_adv_need_create", "need concern"), create("need concern", blocked="missing detail"))
        need_sid = str(need_first["situation"]["situation_id"])
        need_catalog = runtime.model_catalog(owner_id="adversarial-user", session_id="adversarial-session")
        need_row = next(item for item in need_catalog if str(item["situation_token"]) == need_sid)
        open_need = need_row["open_needs"][0]
        bad_binding = existing(
            need_row,
            answered=[(str(open_need["need_token"]), int(open_need["generation"]) + 1)],
        )
        try:
            process(runtime, event("evt_adv_bad_need_generation", "bad generation"), bad_binding, catalog=need_catalog)
        except StateRevisionConflictError:
            expect(True, "Need token outside current generation fails closed")
        else:  # pragma: no cover
            expect(False, "Need token outside current generation fails closed")

        # Lifecycle changes cannot be smuggled in as unquoted model inference.
        try:
            existing(need_row, disposition="resolve", lifecycle="resolved")
        except ValueError:
            expect(True, "unquoted lifecycle disposition is rejected by contract")
        else:  # pragma: no cover
            expect(False, "unquoted lifecycle disposition is rejected by contract")
        no_quote_direct = existing(
            need_row,
            disposition="resolve",
            lifecycle="resolved",
            assertion_mode="direct_user",
        )
        try:
            process(runtime, event("evt_adv_no_quote", "resolved"), no_quote_direct, catalog=need_catalog)
        except ValueError:
            expect(True, "direct lifecycle command without exact quote fails closed")
        else:  # pragma: no cover
            expect(False, "direct lifecycle command without exact quote fails closed")

        # Same event cannot be rebound to a different semantic subject.
        ledger_before = Path(tmp, "runtime", "situation_admission_ledger.json").read_bytes()
        try:
            process(runtime, event("evt_adv_one", "rebound meaning"), create("different meaning"))
        except (StateRevisionConflictError, ValueError):
            expect(True, "same event semantic rebind fails closed")
        else:  # pragma: no cover
            expect(False, "same event semantic rebind fails closed")
        expect(Path(tmp, "runtime", "situation_admission_ledger.json").read_bytes() == ledger_before, "rebind rejection leaves admission ledger unchanged")

        # A corrupt authoritative document is not silently treated as empty.
        corrupt_tmp = TemporaryDirectory(prefix="veyra-living-context-corrupt-")
        try:
            corrupt_store = WorldStateStore(corrupt_tmp.name)
            situation_path = Path(corrupt_tmp.name, "runtime", "situation_state.json")
            original = situation_path.read_bytes()
            situation_path.write_bytes(b"{")
            corrupt_bytes = situation_path.read_bytes()
            try:
                SituationStateRepository(corrupt_store).read()
            except SituationStateError:
                expect(True, "corrupt Situation state fails closed")
            else:  # pragma: no cover
                expect(False, "corrupt Situation state fails closed")
            expect(situation_path.read_bytes() == corrupt_bytes, "corrupt-state read is byte-pure")
            try:
                SituationStateRepository(corrupt_store).mutate(lambda state: state)
            except SituationStateError:
                expect(True, "corrupt Situation mutation fails closed")
            else:  # pragma: no cover
                expect(False, "corrupt Situation mutation fails closed")
            expect(situation_path.read_bytes() == corrupt_bytes, "corrupt-state mutation is byte-pure")
            situation_path.write_bytes(original)
        finally:
            corrupt_tmp.cleanup()

        # Preflight must reject Need overflow before mutating Situation truth.
        bounded_store = WorldStateStore(TemporaryDirectory(prefix="veyra-living-context-cap-").name)
        # Keep this store alive through the local scope; the temporary path is
        # intentionally isolated from the main fixture.
        bounded_runtime = LivingContextRuntime(
            bounded_store,
            information_need_runtime=InformationNeedRuntime(bounded_store, max_needs_per_situation=1),
        )
        bounded_event = event("evt_adv_cap_create", "capacity concern")
        bounded_first = process(bounded_runtime, bounded_event, create("capacity concern", blocked="first missing"))
        bounded_sid = str(bounded_first["situation"]["situation_id"])
        bounded_catalog = bounded_runtime.model_catalog(owner_id="adversarial-user", session_id="adversarial-session")
        bounded_row = next(item for item in bounded_catalog if str(item["situation_token"]) == bounded_sid)
        situation_path = Path(bounded_store.path_for("situation_state.json"))
        need_path = Path(bounded_store.path_for("information_need_state.json"))
        admission_path = Path(bounded_store.path_for("situation_admission_ledger.json"))
        situation_before_replay = situation_path.read_bytes()
        need_before_replay = need_path.read_bytes()
        admission_before_replay = admission_path.read_bytes()
        committed_replay = process(
            bounded_runtime,
            bounded_event,
            create("capacity concern", blocked="first missing"),
        )
        expect(committed_replay["status"] == "replayed", "committed Situation event returns replay status")
        expect(
            situation_path.read_bytes() == situation_before_replay
            and need_path.read_bytes() == need_before_replay
            and admission_path.read_bytes() == admission_before_replay,
            "committed Situation replay does not write ledger or current records",
        )
        before_capacity = situation_path.read_bytes()
        try:
            process(
                bounded_runtime,
                event("evt_adv_cap_overflow", "capacity overflow"),
                existing(bounded_row, needs=[need("second missing")]),
                catalog=bounded_catalog,
                expected_revision=int(bounded_row["observation_revision"]),
            )
        except StateRevisionConflictError:
            expect(True, "Need capacity preflight fails before Situation write")
        else:  # pragma: no cover
            expect(False, "Need capacity preflight fails before Situation write")
        expect(situation_path.read_bytes() == before_capacity, "Need preflight leaves Situation bytes unchanged")

        global_store = WorldStateStore(Path(tmp) / "global-cap")
        global_runtime = LivingContextRuntime(
            global_store,
            information_need_runtime=InformationNeedRuntime(
                global_store,
                max_needs=1,
                max_needs_per_situation=1,
            ),
        )
        process(global_runtime, event("evt_adv_global_one", "global one"), create("global one", blocked="global first"))
        global_situation_path = Path(global_store.path_for("situation_state.json"))
        global_before = global_situation_path.read_bytes()
        try:
            process(global_runtime, event("evt_adv_global_two", "global two"), create("global two", blocked="global second"))
        except StateRevisionConflictError:
            expect(True, "global Need capacity preflight fails before Situation write")
        else:  # pragma: no cover
            expect(False, "global Need capacity preflight fails before Situation write")
        expect(
            global_situation_path.read_bytes() == global_before
            and len(global_runtime.list_situations(owner_id="adversarial-user", session_id="adversarial-session")) == 1,
            "global Need preflight preserves Situation truth",
        )

        # A prepared admission left by a writer failure is repairable by
        # replaying the same event; the Need and ledger finish together.
        retry_store = WorldStateStore(Path(tmp) / "retry")
        retry_runtime = LivingContextRuntime(retry_store)
        retry_candidate = create("retry concern", blocked="retry detail")
        retry_event = event("evt_adv_retry", "retry concern")
        original_upsert = retry_runtime.needs.upsert_for_situation
        failed_once = {"value": False}

        def fail_once(**kwargs: object) -> list[dict[str, object]]:
            if not failed_once["value"]:
                failed_once["value"] = True
                raise RuntimeError("temporary Need writer failure")
            return original_upsert(**kwargs)

        retry_runtime.needs.upsert_for_situation = fail_once  # type: ignore[method-assign]
        try:
            process(retry_runtime, retry_event, retry_candidate)
        except RuntimeError:
            expect(True, "temporary Need write failure leaves a prepared admission")
        else:  # pragma: no cover
            expect(False, "temporary Need write failure leaves a prepared admission")
        retry_entry = retry_store.read_json("situation_admission_ledger.json")["entries"]["evt_adv_retry"]
        expect(
            retry_entry["phase"] == "semantic_applied"
            and retry_entry.get("result_situation_revision") == 1
            and isinstance(retry_entry.get("expected_need_state_revision"), int),
            "prepared admission records exact scope and stage revisions",
        )
        retry_runtime.needs.upsert_for_situation = original_upsert  # type: ignore[method-assign]
        repaired = process(retry_runtime, retry_event, retry_candidate)
        expect(repaired["status"] == "replayed" and repaired["information_needs"], "prepared admission replay completes the Need")
        expect(
            retry_store.read_json("situation_admission_ledger.json")["entries"]["evt_adv_retry"]["phase"] == "committed",
            "repaired admission is committed after retry",
        )

        # The same recovery contract must cover an ordinary update, not only
        # create/terminal transitions.  The first attempt has already
        # advanced Situation truth; the stale turn-start catalog is accepted
        # only for this exact progressed event.
        update_catalog = retry_runtime.model_catalog(
            owner_id="adversarial-user", session_id="adversarial-session"
        )
        update_row = next(
            item for item in update_catalog
            if str(item["situation_token"]) == str(repaired["situation"]["situation_id"])
        )
        update_candidate = existing(
            update_row,
            disposition="update",
            text="retry concern changed",
            needs=[need("retry follow-up")],
        )
        update_event = event("evt_adv_retry_update", "retry concern changed")
        update_original = retry_runtime.needs.upsert_for_situation
        update_failed = {"value": False}

        def update_fail_once(**kwargs: object) -> list[dict[str, object]]:
            if not update_failed["value"]:
                update_failed["value"] = True
                raise RuntimeError("temporary update Need writer failure")
            return update_original(**kwargs)

        retry_runtime.needs.upsert_for_situation = update_fail_once  # type: ignore[method-assign]
        try:
            process(retry_runtime, update_event, update_candidate, catalog=update_catalog)
        except RuntimeError:
            expect(True, "ordinary update Need failure leaves semantic_applied admission")
        else:  # pragma: no cover
            expect(False, "ordinary update Need failure leaves semantic_applied admission")
        retry_runtime.needs.upsert_for_situation = update_original  # type: ignore[method-assign]
        repaired_update = process(
            retry_runtime,
            update_event,
            update_candidate,
            catalog=update_catalog,
        )
        expect(
            repaired_update["status"] == "replayed"
            and retry_store.read_json("situation_admission_ledger.json")["entries"]["evt_adv_retry_update"]["phase"] == "committed",
            "ordinary update stale-catalog retry converges",
        )

        # A terminal resolve may fail after the semantic CAS has committed but
        # before its active Needs are dismissed.  The retry receives the old
        # catalog row; only the exact prepared/repair admission may bridge that
        # one-revision gap.  Wrong event identity and wrong catalog row stay
        # byte-pure and fail closed.
        terminal_retry_store = WorldStateStore(Path(tmp) / "terminal-retry")
        terminal_retry_runtime = LivingContextRuntime(terminal_retry_store)
        terminal_retry_first = process(
            terminal_retry_runtime,
            event("evt_adv_terminal_create", "terminal retry concern"),
            create("terminal retry concern", blocked="terminal retry detail"),
        )
        terminal_retry_sid = str(terminal_retry_first["situation"]["situation_id"])
        terminal_retry_catalog = terminal_retry_runtime.model_catalog(
            owner_id="adversarial-user", session_id="adversarial-session"
        )
        terminal_retry_row = next(
            item for item in terminal_retry_catalog
            if str(item["situation_token"]) == terminal_retry_sid
        )
        terminal_retry_text = "terminal retry concern is resolved"
        terminal_retry_event = event("evt_adv_terminal_resolve", terminal_retry_text)
        terminal_retry_candidate = existing(
            terminal_retry_row,
            disposition="resolve",
            lifecycle="resolved",
            text=terminal_retry_text,
            source_quote=True,
            assertion_mode="direct_user",
        )
        original_dismiss = terminal_retry_runtime.needs.dismiss
        dismiss_failed = {"value": False}

        def dismiss_fail_once(*args: object, **kwargs: object) -> dict[str, object]:
            if not dismiss_failed["value"]:
                dismiss_failed["value"] = True
                raise RuntimeError("temporary dismiss writer failure")
            return original_dismiss(*args, **kwargs)

        terminal_retry_runtime.needs.dismiss = dismiss_fail_once  # type: ignore[method-assign]
        try:
            process(
                terminal_retry_runtime,
                terminal_retry_event,
                terminal_retry_candidate,
                catalog=terminal_retry_catalog,
            )
        except RuntimeError:
            expect(True, "terminal dismiss failure leaves a repairable admission")
        else:  # pragma: no cover
            expect(False, "terminal dismiss failure leaves a repairable admission")
        terminal_retry_runtime.needs.dismiss = original_dismiss  # type: ignore[method-assign]
        terminal_retry_entry = terminal_retry_store.read_json("situation_admission_ledger.json")["entries"]["evt_adv_terminal_resolve"]
        terminal_retry_current = terminal_retry_runtime.get_situation(
            terminal_retry_sid,
            owner_id="adversarial-user",
            session_id="adversarial-session",
        )
        expect(
            terminal_retry_entry["phase"] in {"prepared", "semantic_applied"}
            and terminal_retry_current["source_event_id"] == terminal_retry_event.event_id
            and int(terminal_retry_current["observation_revision"])
            == int(terminal_retry_row["observation_revision"]) + 1,
            "terminal semantic write is tied to the prepared event and next revision",
        )
        ledger_before_nonexact = terminal_retry_store.path_for("situation_admission_ledger.json").read_bytes()
        wrong_event = event("evt_adv_terminal_resolve_other", terminal_retry_text)
        try:
            process(
                terminal_retry_runtime,
                wrong_event,
                terminal_retry_candidate,
                catalog=terminal_retry_catalog,
            )
        except StateRevisionConflictError:
            expect(True, "terminal repair rejects a non-exact event")
        else:  # pragma: no cover
            expect(False, "terminal repair rejects a non-exact event")
        expect(
            terminal_retry_store.path_for("situation_admission_ledger.json").read_bytes() == ledger_before_nonexact,
            "non-exact event rejection is byte-pure",
        )
        wrong_row = dict(terminal_retry_row)
        wrong_row["catalog_token"] = "cat_wrong_terminal_retry"
        try:
            process(
                terminal_retry_runtime,
                terminal_retry_event,
                terminal_retry_candidate,
                catalog=[wrong_row],
            )
        except StateRevisionConflictError:
            expect(True, "terminal repair rejects a non-exact catalog row")
        else:  # pragma: no cover
            expect(False, "terminal repair rejects a non-exact catalog row")
        expect(
            terminal_retry_store.path_for("situation_admission_ledger.json").read_bytes() == ledger_before_nonexact,
            "non-exact catalog rejection is byte-pure",
        )
        terminal_repaired = process(
            terminal_retry_runtime,
            terminal_retry_event,
            terminal_retry_candidate,
            catalog=terminal_retry_catalog,
        )
        terminal_retry_fresh_catalog = terminal_retry_runtime.model_catalog(
            owner_id="adversarial-user", session_id="adversarial-session"
        )
        terminal_retry_fresh_row = next(
            item for item in terminal_retry_fresh_catalog
            if str(item["situation_token"]) == terminal_retry_sid
        )
        terminal_retry_replay_candidate = existing(
            terminal_retry_fresh_row,
            disposition="resolve",
            lifecycle="resolved",
            text=terminal_retry_text,
            source_quote=True,
            assertion_mode="direct_user",
        )
        terminal_repaired_again = process(
            terminal_retry_runtime,
            terminal_retry_event,
            terminal_retry_replay_candidate,
            catalog=terminal_retry_fresh_catalog,
        )
        terminal_retry_needs = terminal_retry_runtime.needs.list(
            owner_id="adversarial-user",
            session_id="adversarial-session",
            situation_id=terminal_retry_sid,
            limit=8,
        )
        expect(
            terminal_repaired["status"] == "replayed"
            and terminal_repaired_again["status"] == "replayed"
            and terminal_repaired["situation"]["status"] == "resolved"
            and terminal_retry_needs
            and all(str(item["status"]) == "dismissed" for item in terminal_retry_needs)
            and terminal_retry_store.read_json("situation_admission_ledger.json")["entries"]["evt_adv_terminal_resolve"]["phase"] == "committed",
            "terminal repair dismisses Needs once and converges on replay",
        )

        # Terminal transition fences and dismisses active Needs; the hint is
        # not a final reaction decision.
        terminal_catalog = bounded_runtime.model_catalog(owner_id="adversarial-user", session_id="adversarial-session")
        terminal_row = next(item for item in terminal_catalog if str(item["situation_token"]) == bounded_sid)
        resolve_text = "capacity concern resolved"
        resolved = process(
            bounded_runtime,
            event("evt_adv_cap_resolve", resolve_text),
            existing(
                terminal_row,
                disposition="resolve",
                lifecycle="resolved",
                text=resolve_text,
                source_quote=True,
                assertion_mode="direct_user",
            ),
            catalog=terminal_catalog,
        )
        expect(resolved["reaction_hint"] is None, "terminal Situation has no reaction hint")
        expect(all(str(item["status"]) == "dismissed" for item in resolved["information_needs"]), "terminal transition dismisses active Needs")

        # Source resolver projection is pure and contains only allow-listed
        # source classes and current CAS identity.
        projection = bounded_runtime.needs.authoritative_projection(
            str(resolved["information_needs"][0]["need_id"]),
            owner_id="adversarial-user",
            session_id="adversarial-session",
        )
        expect(
            isinstance(projection, dict)
            and set(projection) == {
                "owner_id", "session_id", "need_id", "situation_id", "status",
                "generation", "record_digest", "allowed_source_classes",
            },
            "Need source projection exposes exact current identity and digest",
        )

        # The admission retention floor is monotonic even when event time and
        # commit order disagree; an event at or before that floor is rejected
        # without changing the ledger.
        floor_store = WorldStateStore(Path(tmp) / "admission-floor")
        floor_ledger = LivingContextAdmissionLedger(floor_store, max_entries=8)
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def admit_ledger(event_id: str, event_time: datetime) -> None:
            floor_ledger.prepare(
                event_id=event_id,
                owner_id="floor-owner",
                session_id="floor-session",
                situation_id="floor-situation",
                operation="update",
                semantic_digest=f"semantic-{event_id}",
                need_plan_digest=f"needs-{event_id}",
                answered_need_generations={},
                event_timestamp=event_time.isoformat(),
            )
            floor_ledger.mark_committed(
                event_id=event_id,
                semantic_digest=f"semantic-{event_id}",
                need_plan_digest=f"needs-{event_id}",
            )

        for index, day in [(0, 20), (1, 5), *[(item, item + 5) for item in range(2, 8)]]:
            admit_ledger(f"floor-{index}", base + timedelta(days=day))
        # Make commit ordering deterministic for the eviction assertions.
        floor_store.mutate_json(
            floor_ledger.STATE_FILE,
            lambda state: state["entries"].update(
                {
                    event_id: {
                        **row,
                        "committed_at": f"2026-08-01T00:00:{index:02d}+00:00",
                        "updated_at": f"2026-08-01T00:00:{index:02d}+00:00",
                    }
                    for index, (event_id, row) in enumerate(state["entries"].items())
                }
            )
            or state,
        )
        admit_ledger("floor-8", base + timedelta(days=21))
        first_floor = floor_store.read_json(floor_ledger.STATE_FILE)["retention_floor_at"]
        admit_ledger("floor-9", base + timedelta(days=22))
        second_floor = floor_store.read_json(floor_ledger.STATE_FILE)["retention_floor_at"]
        expect(first_floor == second_floor, "admission retention floor is monotonic")
        before_floor_reject = floor_store.path_for(floor_ledger.STATE_FILE).read_bytes()
        try:
            admit_ledger("floor-too-old", base + timedelta(days=20))
        except AdmissionRetentionError:
            expect(True, "admission rejects events at the retained floor")
        else:  # pragma: no cover
            expect(False, "admission rejects events at the retained floor")
        expect(
            floor_store.path_for(floor_ledger.STATE_FILE).read_bytes() == before_floor_reject,
            "retention-floor rejection is byte-pure",
        )

    print("RESULT Living Context adversarial smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
