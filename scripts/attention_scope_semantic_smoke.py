#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from awareness.attention_core import AttentionCore  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from core.semantic_frame import TurnSemanticFrame  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from routers.debug_audit import _public_state, build_debug_audit_router  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def model_understanding(
    text: str,
    *,
    authority: str = "direct_user",
    speaker: str = "user",
    mention_mode: str = "normal_use",
    modality: str = "asserted",
) -> TurnUnderstanding:
    frame = TurnSemanticFrame.from_model_payload(
        {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": [
                {
                    "act_id": "a1",
                    "kind": "request",
                    "goal": "检查 OpenClaw 运行状态",
                    "operation": "inspect_runtime_status",
                    "target": {
                        "type": "runtime",
                        "value": "OpenClaw",
                        "attributes": {},
                    },
                    "polarity": "positive",
                    "explicitness": "explicit",
                    "source_quote": {
                        "text": text,
                        "start": 0,
                        "end": len(text),
                    },
                    "speaker": speaker,
                    "authority": authority,
                    "mention_mode": mention_mode,
                    "evidence_need": "fresh_runtime",
                    "referent": {
                        "surface": "",
                        "resolved": "",
                        "status": "not_applicable",
                        "candidates": [],
                    },
                    "condition": None,
                    "modality": modality,
                    "arguments": {},
                }
            ],
            "relations": [],
            "ambiguities": [],
            "resolver_status": "resolved",
            "source": "model",
        },
        source_text=text,
    )
    return TurnUnderstanding(
        intent="information",
        task_summary=text,
        project="OpenClaw",
        source="model",
        semantic_frame=frame,
        confidence=0.92,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-attention-scope-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        normalizer = EventNormalizer()

        # A legacy unscoped v1 focus cannot be assigned to any tenant.
        store.write_json(
            "attention_state.json",
            {
                "focus": ["legacy-global-secret"],
                "source": "attention_core",
            },
        )
        attention = AttentionCore(store)
        expect(
            attention.focus_for_text(
                "继续",
                user_id="user-a",
                session_id="session-a",
            )
            == [],
            "legacy ownerless focus is not inherited",
        )

        event_a = normalizer.user_message(
            "检查这个目标",
            "smoke",
            "user-a",
            "session-a",
            subject={"kind": "runtime", "id": "openclaw-default"},
        )
        first = attention.focus_for_text(
            "检查这个目标",
            user_id="user-a",
            session_id="session-a",
            event=event_a,
        )
        expect(
            first == ["ref:runtime:openclaw-default"],
            "exact structured event reference creates initial focus",
            first,
        )
        expect(
            attention.focus_for_text(
                "继续",
                user_id="user-a",
                session_id="session-b",
            )
            == [],
            "same user different session cannot inherit focus",
        )
        expect(
            attention.focus_for_text(
                "继续",
                user_id="user-b",
                session_id="session-a",
            )
            == [],
            "different user cannot inherit focus",
        )

        restarted = AttentionCore(store)
        expect(
            restarted.focus_for_text(
                "继续",
                user_id="user-a",
                session_id="session-a",
            )
            == first,
            "exact owner continuation survives restart",
        )

        expired_event = normalizer.user_message(
            "检查过期目标",
            "smoke",
            "expired-user",
            "expired-session",
            subject={"kind": "task", "id": "expired-task"},
        )
        expect(
            restarted.focus_for_text(
                "检查过期目标",
                user_id="expired-user",
                session_id="expired-session",
                event=expired_event,
            )
            == ["ref:task:expired-task"],
            "structured focus is available before its TTL expires",
        )

        def expire_attention_scope(state: dict[str, object]) -> dict[str, object]:
            scopes = state.get("scopes")
            if not isinstance(scopes, dict):
                raise AssertionError("attention scopes missing")
            for record in scopes.values():
                if (
                    isinstance(record, dict)
                    and record.get("user_id") == "expired-user"
                    and record.get("session_id") == "expired-session"
                ):
                    record["updated_at"] = "2000-01-01T00:00:00+00:00"
                    record["ttl_seconds"] = 300
                    return state
            raise AssertionError("expired attention scope missing")

        store.mutate_json("attention_state.json", expire_attention_scope)
        attention_path = store.path_for("attention_state.json")
        before_expired_read = attention_path.read_bytes()
        expired_active = restarted.active_scope(
            user_id="expired-user",
            session_id="expired-session",
        )
        after_expired_read = attention_path.read_bytes()
        expect(
            expired_active.get("scope_status") == "expired"
            and expired_active.get("focus") == []
            and expired_active.get("context_scope")
            == {"probe_priority": [], "structured_refs": [], "semantic_targets": []},
            "expired scope is projected as empty focus",
            expired_active,
        )
        expect(
            before_expired_read == after_expired_read,
            "expired active-scope projection is a pure read",
        )
        expect(
            restarted.focus_for_text(
                "继续",
                user_id="expired-user",
                session_id="expired-session",
            )
            == [],
            "continuation cannot inherit an expired scope",
        )
        expired_continuation = restarted.active_scope(
            user_id="expired-user",
            session_id="expired-session",
        )
        expect(
            expired_continuation.get("scope_status") == "exact"
            and expired_continuation.get("focus") == []
            and expired_continuation.get("inherited_from_previous") is False,
            "expired continuation starts a fresh empty scope",
            expired_continuation,
        )
        expect(
            restarted.focus_for_text(
                "Veyra 架构 Docker 部署 git 日志",
                user_id="lexical-user",
                session_id="lexical-session",
            )
            == [],
            "free-text topic keywords never create initial focus",
        )

        semantic_text = "检查 OpenClaw 运行状态"
        semantic_event = normalizer.user_message(
            semantic_text,
            "smoke",
            "semantic-user",
            "semantic-session",
        )
        expect(
            restarted.focus_for_text(
                semantic_text,
                user_id="semantic-user",
                session_id="semantic-session",
                event=semantic_event,
            )
            == [],
            "unstructured user text remains unfocused before understanding",
        )
        refined = restarted.refine_from_understanding(
            text=semantic_text,
            understanding=model_understanding(semantic_text),
            user_id="semantic-user",
            session_id="semantic-session",
            event=semantic_event,
        )
        expect(
            refined.get("status") == "refined"
            and refined.get("focus") == ["target:runtime:OpenClaw"],
            "validated semantic frame refines foreground focus",
            refined,
        )
        cross_owner_refinement = restarted.refine_from_understanding(
            text=semantic_text,
            understanding=model_understanding(semantic_text),
            user_id="different-user",
            session_id="semantic-session",
            event=semantic_event,
        )
        expect(
            cross_owner_refinement.get("status") == "not_upgraded"
            and cross_owner_refinement.get("reason")
            == "event_owner_scope_mismatch"
            and restarted.active_scope(
                user_id="different-user",
                session_id="semantic-session",
            ).get("focus")
            == [],
            "semantic refinement cannot be rebound to another owner",
            cross_owner_refinement,
        )
        rows = (refined.get("assessment") or {}).get("component_scores") or []
        expect(
            len(rows) == 1
            and rows[0].get("eligible") is True
            and rows[0].get("score") == 1.0
            and isinstance(rows[0].get("components"), dict),
            "semantic focus exposes deterministic component scores",
            rows,
        )
        provider_actor_alias = restarted.assess_understanding(
            text=semantic_text,
            understanding=model_understanding(
                semantic_text,
                speaker="direct_user",
            ),
        )
        expect(
            provider_actor_alias.get("status") == "assessed"
            and provider_actor_alias.get("focus")
            == ["target:runtime:OpenClaw"]
            and not provider_actor_alias.get("blockers"),
            "structured direct_user actor alias remains eligible for read-only focus",
            provider_actor_alias,
        )

        invalid_event = normalizer.user_message(
            "检查另一个目标",
            "smoke",
            "invalid-user",
            "invalid-session",
            subject={"kind": "task", "id": "task-safe"},
        )
        invalid_initial = restarted.focus_for_text(
            "检查另一个目标",
            user_id="invalid-user",
            session_id="invalid-session",
            event=invalid_event,
        )
        invalid = restarted.refine_from_understanding(
            text="检查另一个目标",
            understanding=TurnUnderstanding(
                source="model_invalid_output",
                semantic_frame=TurnSemanticFrame.fallback(
                    "检查另一个目标",
                    resolver_status="invalid_output",
                ),
            ),
            user_id="invalid-user",
            session_id="invalid-session",
            event=invalid_event,
        )
        expect(
            invalid.get("status") == "not_upgraded"
            and invalid.get("focus") == invalid_initial,
            "invalid model output cannot upgrade structured initial focus",
            invalid,
        )
        unavailable = restarted.refine_from_understanding(
            text="检查另一个目标",
            understanding=TurnUnderstanding(
                source="rule_fallback",
                semantic_frame=TurnSemanticFrame.fallback("检查另一个目标"),
            ),
            user_id="invalid-user",
            session_id="invalid-session",
            event=invalid_event,
        )
        expect(
            unavailable.get("status") == "not_upgraded"
            and unavailable.get("focus") == invalid_initial,
            "model-unavailable fallback cannot upgrade focus",
            unavailable,
        )

        reported = restarted.assess_understanding(
            text=semantic_text,
            understanding=model_understanding(
                semantic_text,
                authority="reported_speech",
                mention_mode="reported_speech",
                modality="reported",
            ),
        )
        reported_rows = reported.get("component_scores") or []
        expect(
            reported.get("status") == "assessed"
            and reported.get("focus") == []
            and reported_rows
            and reported_rows[0].get("eligible") is False,
            "reported or quoted semantic targets cannot become focus",
            reported,
        )
        reported_event = normalizer.user_message(
            semantic_text,
            "smoke",
            "reported-user",
            "reported-session",
        )
        restarted.focus_for_text(
            semantic_text,
            user_id="reported-user",
            session_id="reported-session",
            event=reported_event,
        )
        reported_refinement = restarted.refine_from_understanding(
            text=semantic_text,
            understanding=model_understanding(
                semantic_text,
                authority="reported_speech",
                speaker="other",
                mention_mode="reported_speech",
                modality="reported",
            ),
            user_id="reported-user",
            session_id="reported-session",
            event=reported_event,
        )
        expect(
            reported_refinement.get("status") == "not_upgraded"
            and reported_refinement.get("focus") == [],
            "ineligible validated semantics are audited without an upgrade",
            reported_refinement,
        )

        active_a = restarted.active_scope(
            user_id="user-a",
            session_id="session-a",
        )
        active_b = restarted.active_scope(
            user_id="user-a",
            session_id="session-b",
        )
        expect(
            active_a.get("focus") == first
            and active_b.get("focus") == [],
            "owner-scoped status projection cannot enumerate another session",
            {"a": active_a, "b": active_b},
        )

        app = FastAPI()
        app.include_router(
            build_debug_audit_router(
                {"awareness_loop": SimpleNamespace(attention=restarted)}
            )
        )
        client = TestClient(app)
        expect(
            client.get("/attention/active").status_code == 422,
            "attention debug route requires an explicit owner and session",
        )
        scoped_response = client.get(
            "/attention/active",
            params={"user_id": "user-a", "session_id": "session-a"},
        )
        expect(
            scoped_response.status_code == 200
            and scoped_response.json().get("focus") == first,
            "attention debug route returns only the requested exact scope",
            scoped_response.json(),
        )

        state = store.read_json("attention_state.json")
        expect(
            state.get("schema_version") == AttentionCore.SCHEMA_VERSION
            and "focus" not in state
            and isinstance(state.get("scopes"), dict),
            "v2 state removes the unscoped focus projection",
            state,
        )
        public_state = _public_state({"attention_state": state})
        public_attention = public_state.get("attention_state") or {}
        expect(
            public_attention.get("focus") == []
            and public_attention.get("scope_status") == "owner_scope_required"
            and "scopes" not in public_attention,
            "legacy state output remains shaped but cannot enumerate owner scopes",
            public_attention,
        )

        print(
            json.dumps(
                {
                    "status": "passed",
                    "schema_version": state.get("schema_version"),
                    "scope_count": state.get("scope_count"),
                    "semantic_focus": refined.get("focus"),
                    "cross_scope_focus": active_b.get("focus"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
