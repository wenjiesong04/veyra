from __future__ import annotations

import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from rollback_audit.replay_runtime import ReplayRuntime
from rollback_audit.rollback_manager import RollbackManager
from runtime.cron import Cron
from runtime.state_refresh import StateRefresh


class _ActiveLoop:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def tick(self, **_: Any) -> dict[str, Any]:
        with self._lock:
            self.calls += 1
        return {"status": "success"}


class _Journal:
    def __init__(self, count: int) -> None:
        self.items = [
            {
                "journal_id": f"journal-{index}",
                "trace_id": f"trace-{index}",
                "event_id": f"event-{index}",
                "status": "failed",
                "summary": f"failed action {index}",
            }
            for index in range(count)
        ]

    def timeline(self, **_: Any) -> dict[str, Any]:
        return {"items": list(self.items)}


class _Replay:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proposal_calls = 0

    def plan(self, **_: Any) -> dict[str, Any]:
        return {"status": "ready"}

    def compensation_proposal(self, **_: Any) -> dict[str, Any]:
        with self._lock:
            self.proposal_calls += 1
        return {
            "proposal_status": "not_available",
            "status": "not_available",
            "reason": "smoke has no restorable snapshot",
            "plan": {},
        }


def _review_payload(index: int) -> dict[str, Any]:
    return {
        "event_id": f"review-event-{index}",
        "task_text": f"review task {index}",
        "risk_level": "R2",
        "foresight": {"status": "predicted"},
        "guardian_decision": {"decision": "ask_user", "risk_level": "R2"},
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-state-mutation-") as temp_dir:
        root = Path(temp_dir)
        store = WorldStateStore(root / "state")

        review_queue = ReviewQueue(store)
        with ThreadPoolExecutor(max_workers=12) as pool:
            reviews = list(pool.map(lambda index: review_queue.create(**_review_payload(index)), range(36)))
        stored_reviews = review_queue.list()
        assert len(stored_reviews) == 36, f"review append lost updates: {len(stored_reviews)}"
        assert len({item["review_id"] for item in stored_reviews}) == 36

        with ThreadPoolExecutor(max_workers=12) as pool:
            decided = list(
                pool.map(
                    lambda item: review_queue.decide(item["review_id"], "approved", "concurrency smoke"),
                    reviews,
                )
            )
        assert all(item.get("status") == "approved" for item in decided)
        assert len(review_queue.list(status="approved")) == 36

        sandbox = root / "sandbox"
        sandbox.mkdir()
        source = sandbox / "source.txt"
        source.write_text("snapshot payload\n", encoding="utf-8")
        rollback = RollbackManager(
            store,
            snapshot_root=root / "snapshots",
            sandbox_root=sandbox,
        )
        with ThreadPoolExecutor(max_workers=12) as pool:
            snapshots = list(pool.map(lambda _: rollback.snapshot_file(str(source), "concurrency smoke"), range(30)))
        assert all(item.get("status") == "created" for item in snapshots)
        stored_snapshots = store.read_json("rollback_state.json").get("snapshots", [])
        assert len(stored_snapshots) == 30, f"snapshot append lost updates: {len(stored_snapshots)}"
        assert len({item["snapshot_id"] for item in snapshots}) == 30

        active_loop = _ActiveLoop()
        cron = Cron(state_store=store, active_loop=active_loop)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: cron.run_once(reason="concurrency_smoke"), range(40)))
        assert all(item.get("status") == "success" for item in results)
        cron_job = cron.status()["jobs"][Cron.DEFAULT_JOB_ID]
        assert cron_job["run_count"] == 40, f"cron run_count lost updates: {cron_job['run_count']}"
        assert active_loop.calls == 40

        def make_one_job_due(state: dict[str, Any]) -> None:
            jobs = state["jobs"]
            jobs[Cron.DEFAULT_JOB_ID]["next_run_at"] = "2000-01-01T00:00:00+00:00"
            jobs[Cron.COMMITMENT_PUSH_JOB_ID]["enabled"] = False

        store.mutate_json("runtime_cron_state.json", make_one_job_due)
        with ThreadPoolExecutor(max_workers=8) as pool:
            due_results = list(pool.map(lambda _: cron.run_due(), range(8)))
        assert sum(item["processed_count"] for item in due_results) == 1, "due job was claimed more than once"
        assert active_loop.calls == 41
        assert cron.status()["jobs"][Cron.DEFAULT_JOB_ID]["run_count"] == 41

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(
                pool.map(
                    lambda index: cron.configure(
                        job_id=f"custom-{index}",
                        interval_seconds=30 + index,
                    ),
                    range(24),
                )
            )
        jobs = cron.status()["jobs"]
        assert all(f"custom-{index}" in jobs for index in range(24)), "concurrent cron config lost jobs"

        refresher = object.__new__(StateRefresh)
        refresher.state_store = store
        stale_claims = [
            {
                "key": f"claim-{index}",
                "updated_at": f"2026-01-01T00:00:{index:02d}+00:00",
            }
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(
                pool.map(
                    lambda _: refresher._select_fair_batch(stale_claims, limit=4),
                    range(2),
                )
            )
        selected_claims = [
            claim["key"]
            for batch, _, _ in batches
            for claim in batch
        ]
        assert len(set(selected_claims)) == 8, "state refresh cursor assigned the same batch twice"
        assert {before for _, before, _ in batches} == {0, 4}

        replay = _Replay()
        replay_runtime = ReplayRuntime(
            state_store=store,
            replay=replay,
            journal=_Journal(18),
            review_queue=review_queue,
            foresight_engine=None,
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: replay_runtime.scan(), range(8)))
        replay_jobs = replay_runtime.status()["jobs"]
        assert len(replay_jobs) == 18, f"replay scan duplicated/lost jobs: {len(replay_jobs)}"
        assert len({item["candidate_key"] for item in replay_jobs}) == 18

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(
                pool.map(
                    lambda _: replay_runtime.run_pending(
                        auto_create_reviews=False,
                        limit=3,
                    ),
                    range(6),
                )
            )
        replay_status = replay_runtime.status()
        final_jobs = replay_status["jobs"]
        assert all(item.get("status") == "plan_only" for item in final_jobs)
        assert replay.proposal_calls == 18, f"replay jobs processed more than once: {replay.proposal_calls}"
        assert replay_status["status"] == "idle"

        def simulate_abandoned_claim(state: dict[str, Any]) -> None:
            job = state["jobs"][0]
            job["status"] = "processing"
            job["processing_token"] = "abandoned"
            job["processing_started_at"] = "2000-01-01T00:00:00+00:00"

        store.mutate_json("replay_runtime_state.json", simulate_abandoned_claim)
        recovered = replay_runtime.run_pending(auto_create_reviews=False, limit=1)
        assert recovered["processed_count"] == 1
        assert replay_runtime.status()["jobs"][0]["status"] == "plan_only"
        assert replay.proposal_calls == 19, "stale replay processing claim was not recovered exactly once"

    print(
        "state mutation concurrency smoke passed "
        "(reviews=36 snapshots=30 cron_runs=41 refresh_claims=8 replay_jobs=18 stale_claims=1)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
