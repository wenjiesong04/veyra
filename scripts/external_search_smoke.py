#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import utc_now_iso  # noqa: E402
from probes.schema import probe_payload  # noqa: E402
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402


class FakeSearchProbe:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        self.queries.append(query)
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"fake search for {query}",
            confidence=0.9,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "Deep Learning latest practical course 2026",
                        "url": "https://www.deeplearning.ai/courses/deep-learning",
                        "snippet": "Updated deep learning tutorial and course path.",
                        "source": "www.deeplearning.ai",
                    },
                    {
                        "title": "Unrelated cooking guide",
                        "url": "https://example.com/cooking",
                        "snippet": "Kitchen notes.",
                        "source": "example.com",
                    },
                ],
            },
        )


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-external-search-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json(
            "user_goals.json",
            {
                "goals": [
                    {
                        "goal_id": "goal_deep_learning",
                        "kind": "learning",
                        "status": "active",
                        "topic": "深度学习",
                        "user_id": "search-user",
                        "commitment_id": "cmt_learning",
                        "permissions": {"external_search": "granted_for_digest", "proactive_push": "granted"},
                        "updated_at": utc_now_iso(),
                    },
                    {
                        "goal_id": "goal_pending",
                        "kind": "learning",
                        "status": "active",
                        "topic": "强化学习",
                        "user_id": "search-user",
                        "permissions": {"external_search": "pending_confirmation", "proactive_push": "pending_confirmation"},
                        "updated_at": utc_now_iso(),
                    },
                ]
            },
        )
        search = FakeSearchProbe()
        refresh = ExternalWorldRefresh(store, search_probe=search)
        result = refresh.refresh_watchlist(limit=5)
        expect(result.get("status") == "success", "external refresh succeeds", result)
        expect(len(search.queries) == 1 and "深度学习" in search.queries[0], "only authorized learning goal searched", search.queries)
        refreshed = result.get("refreshed") if isinstance(result.get("refreshed"), list) else []
        expect(refreshed and refreshed[0].get("kind") == "learning_search", "learning search refreshed", refreshed)
        expect(refreshed[0].get("results") and refreshed[0]["results"][0]["score"] >= 0.6, "search results scored", refreshed[0])

        external = store.read_json("external_world.json")
        watchlist = external.get("watchlist") if isinstance(external.get("watchlist"), list) else []
        expect(any(item.get("target") == "learning:goal_deep_learning" for item in watchlist if isinstance(item, dict)), "goal watchlist created", watchlist)
        expect(not any(item.get("target") == "learning:goal_pending" for item in watchlist if isinstance(item, dict)), "pending goal not watched", watchlist)
        expect(bool(external.get("knowledge_items")), "knowledge items recorded", external)
        expect(bool(external.get("push_candidates")), "push candidates recorded", external)

        second = refresh.refresh_watchlist(limit=5)
        external_again = store.read_json("external_world.json")
        expect(len(external_again.get("watchlist", [])) == len(watchlist), "watchlist sync is idempotent", second)

    print("external search smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
