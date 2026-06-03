#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from probes.schema import probe_payload  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class PyTorchSearchProbe:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        self.calls += 1
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"PyTorch refresh {self.calls}",
            confidence=0.9,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "PyTorch 2.9 latest release notes",
                        "url": "https://pytorch.org/blog/pytorch-2-9/",
                        "snippet": "Important updated PyTorch release information and migration notes.",
                        "source": "pytorch.org",
                    },
                    {
                        "title": "PyTorch 2.9 latest release notes duplicate",
                        "url": "https://pytorch.org/blog/pytorch-2-9/",
                        "snippet": "Duplicate URL should not create another push candidate.",
                        "source": "pytorch.org",
                    },
                    {
                        "title": "Unrelated marketing page",
                        "url": "https://example.com/unrelated",
                        "snippet": "A generic low signal page without useful release details.",
                        "source": "example.com",
                    },
                ],
            },
        )


class TimeoutSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        raise TimeoutError(f"synthetic search timeout for {query}")


class DummyTaskTracker:
    def refresh_pending(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success", "refreshed": []}


class DummyStateRefresh:
    def refresh_stale(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success", "remaining_stale": 0}


class DummyProactiveChecks:
    def run_read_only(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success"}


class DummyRuntimeMatrix:
    def run(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "ready"}


class DummyReplayRuntime:
    def scan(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success", "created_count": 0}

    def run_pending(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success", "processed_count": 0}


class DummyCommitmentPush:
    def run_due(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "idle", "due_count": 0, "processed_count": 0}


def seed_external_world(store: WorldStateStore, *, commitment_id: str = "cmt_pytorch") -> None:
    store.write_json(
        "external_world.json",
        {
            "watchlist": [
                {
                    "target": "external:pytorch",
                    "kind": "external_search",
                    "enabled": True,
                    "status": "active",
                    "topic": "PyTorch",
                    "query": "PyTorch latest release",
                    "commitment_id": commitment_id,
                }
            ],
            "summaries": [],
            "knowledge_items": [],
            "push_candidates": [],
        },
    )


def push_quality_smoke() -> None:
    with TemporaryDirectory(prefix="veyra-external-world-push-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        seed_external_world(store)
        search = PyTorchSearchProbe()
        refresh = ExternalWorldRefresh(store, search_probe=search)  # type: ignore[arg-type]

        first = refresh.refresh_watchlist(limit=5)
        second = refresh.refresh_watchlist(limit=5)
        external = store.read_json("external_world.json")
        candidates = external.get("push_candidates") if isinstance(external.get("push_candidates"), list) else []
        knowledge = external.get("knowledge_items") if isinstance(external.get("knowledge_items"), list) else []

        expect(first.get("status") == "success" and second.get("status") == "success", "two PyTorch refreshes succeed", {"first": first, "second": second})
        expect(search.calls == 2, "search refresh runs twice", search.calls)
        expect(len(candidates) == 1, "duplicate and low-quality results create one push candidate", candidates)
        expect(len(knowledge) == 1, "low-quality duplicate refresh records one useful knowledge item", knowledge)
        candidate = candidates[0]
        for key in ("retrieved_at", "ttl", "quality_score", "dedupe_key", "summary"):
            expect(bool(candidate.get(key)), f"push candidate has {key}", candidate)
        expect(candidate.get("url") == "https://pytorch.org/blog/pytorch-2-9/", "push candidate is the high-quality PyTorch item", candidate)
        expect(float(candidate.get("quality_score") or 0) >= 0.6, "push candidate meets quality threshold", candidate)


def timeout_active_loop_smoke() -> None:
    with TemporaryDirectory(prefix="veyra-external-world-timeout-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        seed_external_world(store, commitment_id="cmt_timeout")
        refresh = ExternalWorldRefresh(store, search_probe=TimeoutSearchProbe())  # type: ignore[arg-type]
        loop = ActiveRuntimeLoop(
            state_store=store,
            runtime_entity=RuntimeEntity(store),
            proactive_checks=DummyProactiveChecks(),
            state_refresh=DummyStateRefresh(),
            external_world_refresh=refresh,
            runtime_matrix=DummyRuntimeMatrix(),
            retention_policy=RetentionPolicy(store),
            task_tracker=DummyTaskTracker(),
            adapter_resolver=lambda: None,
            verifier=object(),
            replay_runtime=DummyReplayRuntime(),
            commitment_push=DummyCommitmentPush(),
        )
        tick = loop.tick(reason="external_world_timeout_smoke")
        external_step = next((step for step in tick.get("steps", []) if isinstance(step, dict) and step.get("name") == "external_world"), {})
        external = store.read_json("external_world.json")
        candidates = external.get("push_candidates") if isinstance(external.get("push_candidates"), list) else []
        expect(tick.get("status") == "success", "search timeout does not block active loop tick", tick)
        expect(external_step.get("status") == "success", "external_world step records timeout as non-blocking result", external_step)
        expect(not candidates, "timeout refresh creates no push candidate", candidates)


def main() -> int:
    push_quality_smoke()
    timeout_active_loop_smoke()
    print("external world push quality smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
