#!/usr/bin/env python3
"""Risk-tier smoke for entity provenance and weather source parameters."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.situation_state_repository import SituationStateRepository  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import parse_living_context_candidate_detailed  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402
from runtime.living_context_source_policy import LivingContextSourcePolicy  # noqa: E402


OWNER = "provenance-owner"
SESSION = "provenance-session"
TEXT = "下周六户外团建在上海。"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS {label}")


def event(event_id: str, text: str = TEXT) -> VeyraEvent:
    now = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc).isoformat()
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=OWNER, session_id=SESSION),
        payload={"text": text},
        event_id=event_id,
        timestamp=now,
        occurred_at=now,
        received_at=now,
    )


def raw_candidate(
    location: str,
    *,
    subject: str,
    source_text: str = TEXT,
    entity_quote: dict[str, object] | None = None,
    disposition: str = "create",
    binding: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": disposition,
        "create_subject": subject,
        "goal": f"完成{subject}",
        "entities": [
            {
                "kind": "place",
                "value": location,
                "epistemic_status": "reported",
                "source_quote": entity_quote,
            }
        ],
        "known": [{"statement": source_text, "epistemic_status": "reported"}],
        "unknown": [],
        "assumptions": [],
        "timeline": [],
        "needs": [
            {
                "blocked_judgment": "天气未确认",
                "evidence_kind": "weather",
                "why_now": "需要确认是否适合户外活动",
                "allowed_source_classes": ["public_web"],
                "fallback_reaction": "read",
                "question": "天气如何？",
            }
        ],
        "source": "model",
    }
    if binding:
        payload.update(binding)
    return payload


def parse(raw: dict[str, object], text: str) -> object:
    candidate, issues, _report = parse_living_context_candidate_detailed(
        raw,
        source_text=text,
    )
    expect(candidate is not None and not issues, "candidate crosses the strict boundary", issues)
    return candidate


def admit(
    runtime: LivingContextRuntime,
    raw: dict[str, object],
    event_id: str,
    *,
    text: str = TEXT,
) -> dict[str, object]:
    candidate = parse(raw, text)
    return runtime.process_user_turn(
        event(event_id, text),
        SimpleNamespace(living_context_candidate=candidate),
        catalog=(
            [
                {
                    "situation_token": raw["situation_token"],
                    "observation_revision": raw["situation_revision"],
                    "catalog_token": raw["catalog_token"],
                    "owner_id": OWNER,
                    "session_id": SESSION,
                }
            ]
            if "situation_token" in raw
            else []
        ),
    )


def source_binding(runtime: LivingContextRuntime, situation: dict[str, object]) -> object:
    situation_id = str(situation.get("situation_id") or "")
    needs = runtime.needs.list(
        owner_id=OWNER,
        session_id=SESSION,
        situation_id=situation_id,
        limit=8,
    )
    need = next(item for item in needs if item.get("evidence_kind") == "weather")
    projection = runtime.needs.authoritative_projection(
        str(need["need_id"]),
        owner_id=OWNER,
        session_id=SESSION,
    )
    return LivingContextSourcePolicy().derive_binding(
        situation=situation,
        need={**need, **(projection or {})},
        now=datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc),
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-entity-provenance-") as tmp:
        runtime = LivingContextRuntime(WorldStateStore(Path(tmp)))

        # P1 reproduction: user text says Shanghai, the model says Beijing,
        # and there is no exact entity span. The read may use Beijing only as
        # a tentative weather parameter; durable truth remains inferred.
        wrong = admit(
            runtime,
            raw_candidate("北京", subject="北京候选团建"),
            "evt_wrong_place",
        )
        wrong_situation = wrong.get("situation") if isinstance(wrong.get("situation"), dict) else {}
        wrong_id = str(wrong_situation.get("situation_id") or "")
        persisted_wrong = runtime.situations.get_semantic(
            wrong_id,
            user_id=OWNER,
            session_id=SESSION,
        )
        wrong_entities = ((persisted_wrong or {}).get("semantic") or {}).get("entities") or []
        wrong_entity = wrong_entities[0] if wrong_entities else {}
        expect(
            wrong_entity.get("value") == "北京"
            and wrong_entity.get("epistemic_status") == "inferred"
            and wrong_entity.get("provenance_scope") == "model_attributed"
            and wrong_entity.get("source_event_id") == "evt_wrong_place",
            "jointly hallucinated place stays inferred/model_attributed",
            wrong_entity,
        )
        wrong_known = (((persisted_wrong or {}).get("semantic") or {}).get("known") or [])[0]
        expect(
            wrong_known.get("epistemic_status") == "inferred"
            and wrong_known.get("provenance_scope") == "model_attributed",
            "Known without an exact span is not promoted by model self-report",
            wrong_known,
        )
        tentative = source_binding(runtime, persisted_wrong or {})
        expect(
            tentative is not None
            and tentative.parameters.get("location") == "北京",
            "weather may use model_attributed place as a tentative read parameter",
            tentative,
        )

        # An exact candidate quote is the higher-confidence span tier.
        span_start = TEXT.index("上海")
        span = admit(
            runtime,
            raw_candidate(
                "上海",
                subject="上海户外团建",
                entity_quote={"text": "上海", "start": span_start, "end": span_start + 2},
            ),
            "evt_span_place",
        )
        span_situation = span.get("situation") if isinstance(span.get("situation"), dict) else {}
        span_id = str(span_situation.get("situation_id") or "")
        persisted_span = runtime.situations.get_semantic(span_id, user_id=OWNER, session_id=SESSION)
        span_entity = (((persisted_span or {}).get("semantic") or {}).get("entities") or [])[0]
        expect(
            span_entity.get("epistemic_status") == "reported"
            and span_entity.get("provenance_scope") == "span",
            "valid exact entity quote remains reported span provenance",
            span_entity,
        )
        expect(
            source_binding(runtime, persisted_span or {}) is not None,
            "reported span place binds weather",
        )

        # Legacy state is readable but not source-executable.
        legacy = dict(persisted_span or {})
        legacy_semantic = dict(legacy.get("semantic") or {})
        legacy_semantic["entities"] = [
            {"kind": "place", "value": "上海", "epistemic_status": "reported"}
        ]
        legacy["semantic"] = legacy_semantic
        expect(source_binding(runtime, legacy) is None, "legacy bare reported place is rejected")

        # A direct-user correction with no entity span is still only a
        # tentative model attribution; it must not supersede Shanghai.
        correction_text = "地点改为北京"
        current_row = runtime.model_catalog(owner_id=OWNER, session_id=SESSION)
        span_row = next(row for row in current_row if row.get("situation_token") == span_id)
        no_span_correction = raw_candidate(
            "北京",
            subject="上海户外团建",
            source_text=correction_text,
            disposition="correct",
            binding={
                "situation_token": span_row["situation_token"],
                "situation_revision": span_row["observation_revision"],
                "catalog_token": span_row["catalog_token"],
                "source_quote": {"text": correction_text, "start": 0, "end": len(correction_text)},
                "assertion_mode": "direct_user",
            },
        )
        no_span_result = admit(runtime, no_span_correction, "evt_correct_without_entity_span", text=correction_text)
        no_span_situation = no_span_result.get("situation") if isinstance(no_span_result.get("situation"), dict) else {}
        no_span_values = {
            row.get("value")
            for row in ((no_span_situation.get("semantic") or {}).get("entities") or [])
        }
        expect(
            no_span_values == {"上海", "北京"},
            "correct without an entity span does not supersede the prior place",
            no_span_values,
        )
        tentative_correction = source_binding(runtime, no_span_situation)
        expect(
            tentative_correction is not None
            and tentative_correction.parameters.get("location") == "北京",
            "weather correction uses the latest model-attributed place as a tentative target",
            tentative_correction,
        )

        # Correction supersedes prior place entities only with the existing
        # direct-user root authority, exactly one new place entity, and that
        # entity's own exact span.
        correction_text = "地点改为杭州"
        current_row = runtime.model_catalog(owner_id=OWNER, session_id=SESSION)
        span_row = next(row for row in current_row if row.get("situation_token") == span_id)
        correction_raw = raw_candidate(
            "杭州",
            subject="上海户外团建",
            source_text=correction_text,
            disposition="correct",
            binding={
                "situation_token": span_row["situation_token"],
                "situation_revision": span_row["observation_revision"],
                "catalog_token": span_row["catalog_token"],
                "source_quote": {"text": correction_text, "start": 0, "end": len(correction_text)},
                "assertion_mode": "direct_user",
            },
            entity_quote={
                "text": "杭州",
                "start": 4,
                "end": 6,
            },
        )
        corrected = admit(runtime, correction_raw, "evt_correct_place", text=correction_text)
        corrected_situation = corrected.get("situation") if isinstance(corrected.get("situation"), dict) else {}
        corrected_entities = ((corrected_situation.get("semantic") or {}).get("entities") or [])
        expect(
            [row.get("value") for row in corrected_entities] == ["杭州"],
            "direct-user correct with one place supersedes the prior place",
            corrected_entities,
        )

        # Ordinary update retains distinct places instead of treating every
        # update as a correction.
        current_row = runtime.model_catalog(owner_id=OWNER, session_id=SESSION)
        span_row = next(row for row in current_row if row.get("situation_token") == span_id)
        ordinary_text = "又考虑南京"
        ordinary_raw = raw_candidate(
            "南京",
            subject="上海户外团建",
            source_text=ordinary_text,
            disposition="update",
            binding={
                "situation_token": span_row["situation_token"],
                "situation_revision": span_row["observation_revision"],
                "catalog_token": span_row["catalog_token"],
            },
        )
        ordinary = admit(runtime, ordinary_raw, "evt_update_place", text=ordinary_text)
        ordinary_situation = ordinary.get("situation") if isinstance(ordinary.get("situation"), dict) else {}
        ordinary_values = {
            row.get("value")
            for row in ((ordinary_situation.get("semantic") or {}).get("entities") or [])
        }
        expect(ordinary_values == {"杭州", "南京"}, "ordinary update preserves multiple places", ordinary_values)

        # Repository rejects the removed self-certification shape and keeps
        # only the two honest risk tiers.
        repository = SituationStateRepository(WorldStateStore(Path(tmp) / "validation"))
        invalid = dict(legacy_semantic)
        invalid["entities"] = [
            {
                "kind": "place",
                "value": "上海",
                "epistemic_status": "reported",
                "provenance_scope": "unsupported_scope",
            }
        ]
        try:
            repository.normalize_semantic(invalid)
        except ValueError:
            expect(True, "repository rejects unsupported provenance scope")
        else:  # pragma: no cover
            expect(False, "repository rejects unsupported provenance scope")

    print("entity risk-tier provenance smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
