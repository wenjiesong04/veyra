"""Legacy Unknown binding cannot cross a new typed Need endpoint."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_context_contract import CandidateNeed  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402


class FakeNeeds:
    max_needs_per_situation = 8
    need_identity_digest_for_candidate = staticmethod(InformationNeedRuntime.need_identity_digest_for_candidate)

    def list(self, **kwargs):
        return [{
            "evidence_kind": "weather",
            "observation_mode": "watch",
            "blocked_judgment": "old blocker",
            "unknown_binding": "old unknown",
            "evidence_target_digest": None,
            "need_identity_digest": None,
        }]


def main() -> int:
    with TemporaryDirectory(prefix="veyra-unknown-binding-") as temp:
        runtime = LivingContextRuntime(WorldStateStore(Path(temp) / "state"), information_need_runtime=FakeNeeds())
        candidate = CandidateNeed.model_validate(
            {
                "blocked_judgment": "new blocker",
                "evidence_kind": "weather",
                "observation_mode": "watch",
                "observation_requirement": {"coverage": "current", "metrics": ["temperature_2m"]},
                "why_now": "new target",
                "urgency": 0.5,
                "allowed_source_classes": ["weather"],
                "fallback_reaction": "read",
                "question": "new question",
            },
            strict=True,
        )
        target = {
            "location": "上海",
            "observation_requirement": {"coverage": "current", "metrics": ["temperature_2m"]},
        }
        bindings = runtime._derive_unknown_bindings(
            candidate=SimpleNamespace(needs=[candidate], unknown=["new unknown"]),
            semantic_state={"unknown": ["old unknown", "new unknown"]},
            situation_id="sit",
            owner_id="u",
            session_id="s",
            evidence_targets=[target],
        )
        # With one current-turn Unknown, only the new typed endpoint may bind
        # it; the old legacy binding is never inherited.
        assert bindings == {"new blocker": "new unknown"}
    print("UNKNOWN_BINDING_IDENTITY_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
