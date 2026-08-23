"""Narrow proof that source audit churn does not wake cognition."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.living_source_primitives import stable_digest  # noqa: E402
from runtime.read_only_cognitive_loop import ReadOnlyCognitiveLoopRuntime  # noqa: E402


def main() -> int:
    base = {
        "scope": "exact_owner_session",
        "situations": [
            {
                "title": "上海团建",
                "material_revision": 2,
                "material_digest": "a" * 64,
                "revision": 7,
                "change_token": "lcchg_" + "1" * 32,
                "row_evidence_ref": "lcref_" + "1" * 32,
                "row_novelty": "unchanged",
                "updated_at": "2026-08-23T10:00:00Z",
            }
        ],
        "information_needs": [
            {"status": "resolved", "generation": 1, "updated_at": "2026-08-23T10:00:00Z"}
        ],
        "source_receipt_count": 1,
        "source_receipts": [
            {
                "status": "ok",
                "observed_at": "2026-08-23T10:00:00Z",
                "fresh_until": "2026-08-23T16:00:00Z",
                "payload_digest": "b" * 64,
            }
        ],
    }
    changed_audit = {
        **base,
        "situations": [
            {
                **base["situations"][0],
                "revision": 8,
                "change_token": "lcchg_" + "2" * 32,
                "row_evidence_ref": "lcref_" + "2" * 32,
                "updated_at": "2026-08-23T11:00:00Z",
            }
        ],
        "source_receipt_count": 2,
        "source_receipts": [
            {
                "status": "ok",
                "observed_at": "2026-08-23T11:00:00Z",
                "fresh_until": "2026-08-23T17:00:00Z",
                "payload_digest": "c" * 64,
            }
        ],
    }
    row_digests = {"sit": stable_digest({"material_revision": 2, "material_digest": "a" * 64}, namespace="material-row")}
    first = ReadOnlyCognitiveLoopRuntime._living_context_digest_payload(base, row_digests)
    second = ReadOnlyCognitiveLoopRuntime._living_context_digest_payload(changed_audit, row_digests)
    assert stable_digest(first, namespace="digest") == stable_digest(second, namespace="digest")
    print("COGNITIVE_MATERIAL_DIGEST_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
