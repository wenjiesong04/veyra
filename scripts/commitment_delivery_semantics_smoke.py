#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime.commitment_push as commitment_push_module  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.commitment_push import CommitmentPushRuntime  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


class SentAdapter:
    sent_messages: list[dict[str, Any]] = []

    def __init__(self, state_store: WorldStateStore | None = None, channel: str = "api") -> None:
        self.state_store = state_store
        self.channel = channel

    def send(self, session_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent_messages.append({"session_id": session_id, "message": message, "metadata": metadata or {}})
        return {"status": "sent", "delivery_status": "provider_sent", "provider": "smoke"}


def main() -> int:
    with TemporaryDirectory(prefix="veyra-delivery-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        commitments = CommitmentCore(store)
        runtime = CommitmentPushRuntime(state_store=store, commitment_core=commitments)

        queued = commitments.create_commitment(
            {
                "kind": "generic_reminder",
                "status": "active",
                "title": "queued reminder",
                "session_id": "queued-session",
                "next_run_at": past_iso(),
                "payload": {"note": "queued test"},
            }
        )
        queued_before = queued["next_run_at"]
        queued_result = runtime.run_due(limit=5, reason="queued_smoke")
        queued_row = next(row for row in queued_result["processed"] if row["commitment_id"] == queued["commitment_id"])
        queued_after = commitments.get_commitment(queued["commitment_id"]) or {}
        expect(queued_row.get("status") == "queued", "local outbox is queued", queued_row)
        expect(queued_after.get("next_run_at") == queued_before, "queued push does not advance schedule", queued_after)
        expect(not queued_after.get("last_run_at"), "queued push does not set last_run_at", queued_after)
        expect(queued_after.get("last_attempt_at"), "queued push records attempt cooldown", queued_after)

        delivered = commitments.create_commitment(
            {
                "kind": "learning_digest",
                "status": "active",
                "title": "learning candidate",
                "session_id": "sent-session",
                "next_run_at": past_iso(),
                "payload": {"topic": "深度学习", "phase": "入门"},
            }
        )
        store.write_json(
            "external_world.json",
            {
                "watchlist": [],
                "summaries": [],
                "knowledge_items": [],
                "push_candidates": [
                    {
                        "candidate_id": "push_candidate_smoke",
                        "commitment_id": delivered["commitment_id"],
                        "topic": "深度学习",
                        "title": "Deep Learning latest practical course 2026",
                        "url": "https://www.deeplearning.ai/courses/deep-learning",
                        "snippet": "Updated course path for deep learning.",
                        "score": 0.91,
                        "status": "new",
                    }
                ],
            },
        )

        original_adapter = commitment_push_module.ChannelAdapter
        SentAdapter.sent_messages = []
        commitment_push_module.ChannelAdapter = SentAdapter  # type: ignore[assignment]
        try:
            delivered_result = runtime.run_due(limit=5, reason="delivered_smoke")
        finally:
            commitment_push_module.ChannelAdapter = original_adapter  # type: ignore[assignment]

        delivered_row = next(row for row in delivered_result["processed"] if row["commitment_id"] == delivered["commitment_id"])
        delivered_after = commitments.get_commitment(delivered["commitment_id"]) or {}
        external_after = store.read_json("external_world.json")
        candidate = external_after["push_candidates"][0]
        expect(delivered_row.get("status") == "delivered", "provider sent is delivered", delivered_row)
        expect(delivered_after.get("last_run_at"), "delivered push sets last_run_at", delivered_after)
        expect(delivered_after.get("next_run_at") and delivered_after.get("next_run_at") != delivered["next_run_at"], "delivered push advances schedule", delivered_after)
        expect(candidate.get("status") == "delivered", "delivered push marks external candidate", candidate)
        expect(SentAdapter.sent_messages and "deeplearning.ai" in SentAdapter.sent_messages[0]["message"], "learning push uses external candidate", SentAdapter.sent_messages)

    print("commitment delivery semantics smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
