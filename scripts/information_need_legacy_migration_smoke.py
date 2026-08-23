"""Read-only legacy projection plus idempotent writer-index migration."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_context_contract import CandidateNeed  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402


def main() -> int:
    with TemporaryDirectory(prefix="veyra-need-legacy-") as temp:
        store = WorldStateStore(Path(temp) / "state")
        runtime = InformationNeedRuntime(store)
        candidate = CandidateNeed.model_validate(
            {
                "blocked_judgment": "旧目标",
                "evidence_kind": "other",
                "why_now": "仅验证迁移",
                "allowed_source_classes": ["other"],
                "fallback_reaction": "wait",
                "question": "",
            },
            strict=True,
        )
        runtime.upsert_for_situation(
            situation_id="sit-legacy",
            owner_id="u",
            session_id="s",
            needs=[candidate],
            source_event_id="event-legacy",
        )

        def make_legacy(state: dict[str, object]) -> dict[str, object]:
            row = state["needs"][next(iter(state["needs"]))]  # type: ignore[index]
            state["event_fingerprints"] = {
                row["need_id"]: {
                    row["source_event_id"]: runtime._legacy_record_fingerprint(row)  # type: ignore[arg-type]
                }
            }
            state.pop("event_replay_evidence", None)
            state.pop("index_schema_version", None)
            return state

        store.mutate_json(runtime.STATE_FILE, make_legacy)
        assert runtime.list(owner_id="u", session_id="s")
        runtime.upsert_for_situation(
            situation_id="sit-legacy",
            owner_id="u",
            session_id="s",
            needs=[candidate],
            source_event_id="event-legacy",
        )
        state = store.read_json(runtime.STATE_FILE)
        row = next(iter(state["needs"].values()))
        assert state["index_schema_version"] == runtime.INDEX_SCHEMA_VERSION
        assert state["event_fingerprints"][row["need_id"]][row["source_event_id"]] == runtime._record_fingerprint(row)
    print("INFORMATION_NEED_LEGACY_MIGRATION_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
