#!/usr/bin/env python3
"""Prove approved review side effects use an atomic, one-time execution claim."""
from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from routers.debug_audit import ReviewDecisionRequest, build_debug_audit_router  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def create_review(queue: ReviewQueue, event_id: str) -> dict:
    return queue.create(
        event_id=event_id,
        task_text="execute one bounded reviewed side effect",
        risk_level="R3",
        foresight={},
        guardian_decision={},
        proposal={
            "type": "safe_test_action",
            "action": {"target": "fixture", "value": 1},
        },
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-review-claim-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        queue = ReviewQueue(store)
        unclaimed = create_review(queue, "claim-required")
        queue.decide(str(unclaimed["review_id"]), "approved", "legacy approval")
        try:
            queue.update_execution(
                str(unclaimed["review_id"]),
                {"status": "success"},
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("unclaimed review accepted an execution result")
        expect(
            True,
            "approved review cannot record execution without an atomic claim",
        )

        review = create_review(queue, "claim-concurrency")
        review_id = str(review["review_id"])

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(
                pool.map(
                    lambda index: queue.approve_and_claim(
                        review_id,
                        f"concurrent approval {index}",
                    ),
                    range(32),
                )
            )

        tokens = [token for _, token in outcomes if token is not None]
        expect(len(tokens) == 1, "exactly one concurrent caller receives a claim", len(tokens))
        raw_state = store.read_json("review_queue.json")
        serialized = json.dumps(raw_state, sort_keys=True)
        expect(tokens[0] not in serialized, "raw claim token is never persisted")

        stored = next(
            item
            for item in raw_state.get("items", [])
            if item.get("review_id") == review_id
        )
        claim = stored.get("execution_claim") or {}
        expect(
            claim.get("state") == "indeterminate"
            and bool(claim.get("token_digest"))
            and bool(claim.get("proposal_digest"))
            and bool(claim.get("claimed_at")),
            "claim is persisted as indeterminate digests",
            claim,
        )

        effect_count = 0
        effect_lock = Lock()

        def execute_if_claimed(outcome: tuple[dict, str | None]) -> None:
            nonlocal effect_count
            _, token = outcome
            if token is None:
                return
            with effect_lock:
                effect_count += 1

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(execute_if_claimed, outcomes))
        expect(effect_count == 1, "only the claim holder performs the side effect", effect_count)

        try:
            queue.update_execution(
                review_id,
                {"status": "success"},
                claim_token="wrong-token",
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("wrong claim token was accepted")

        completed = queue.update_execution(
            review_id,
            {"status": "success", "effect_count": effect_count},
            claim_token=tokens[0],
        )
        expect(
            (completed.get("execution_claim") or {}).get("state") == "observed",
            "matching post-effect observation closes the claim",
            completed,
        )
        repeated = queue.update_execution(
            review_id,
            {"status": "success", "effect_count": effect_count},
            claim_token=tokens[0],
        )
        expect(
            repeated.get("execution_result") == completed.get("execution_result"),
            "identical completion retry is idempotent",
            repeated,
        )
        try:
            queue.update_execution(
                review_id,
                {"status": "success", "effect_count": 2},
                claim_token=tokens[0],
            )
        except ValueError:
            pass
        else:
            raise AssertionError("contradictory completion overwrote the first result")

        route_review = create_review(queue, "claim-route-concurrency")
        route_review_id = str(route_review["review_id"])

        class CountingExecutor:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = Lock()

            def execute_review(self, claimed_review: dict) -> dict:
                with self.lock:
                    self.calls += 1
                return {
                    "status": "success",
                    "review_id": claimed_review.get("review_id"),
                }

        counting_executor = CountingExecutor()
        router = build_debug_audit_router(
            {
                "review_queue": queue,
                "action_executor": counting_executor,
            }
        )
        approve_endpoint = next(
            route.endpoint
            for route in router.routes
            if getattr(route, "path", "") == "/reviews/{review_id}/approve"
        )

        async def approve_concurrently() -> list[dict]:
            return await asyncio.gather(
                *(
                    approve_endpoint(
                        route_review_id,
                        ReviewDecisionRequest(reason=f"route approval {index}"),
                    )
                    for index in range(16)
                )
            )

        asyncio.run(approve_concurrently())
        expect(
            counting_executor.calls == 1,
            "duplicate concurrent approve requests invoke executor once",
            counting_executor.calls,
        )

        rejected_review = create_review(queue, "claim-rejected-terminal")
        rejected_id = str(rejected_review["review_id"])
        queue.decide(rejected_id, "rejected", "terminal rejection")
        try:
            asyncio.run(
                approve_endpoint(
                    rejected_id,
                    ReviewDecisionRequest(reason="must not revive"),
                )
            )
        except HTTPException as exc:
            expect(
                exc.status_code == 409,
                "terminal review approval is an explicit conflict",
                exc.detail,
            )
        else:
            raise AssertionError("terminal rejected review returned approval success")
        expect(
            counting_executor.calls == 1,
            "terminal review conflict invokes no executor",
            counting_executor.calls,
        )

        malformed_review = create_review(queue, "claim-malformed-result")
        malformed_id = str(malformed_review["review_id"])
        _, malformed_token = queue.approve_and_claim(
            malformed_id,
            "reject malformed post-effect result",
        )
        try:
            queue.update_execution(
                malformed_id,
                [],  # type: ignore[arg-type]
                claim_token=malformed_token,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("malformed execution result was persisted")
        malformed_stored = next(
            item
            for item in queue.list()
            if item.get("review_id") == malformed_id
        )
        expect(
            malformed_stored.get("execution_result") is None
            and (malformed_stored.get("execution_claim") or {}).get("state")
            == "indeterminate",
            "malformed result is rejected before claim state mutation",
            malformed_stored,
        )

        crash_review = create_review(queue, "claim-crash")
        _, crash_token = queue.approve_and_claim(
            str(crash_review["review_id"]),
            "simulate crash after reservation",
        )
        expect(bool(crash_token), "crash simulation obtained initial claim")
        retry_review, retry_token = queue.approve_and_claim(
            str(crash_review["review_id"]),
            "retry after unknown side effect",
        )
        expect(
            retry_token is None
            and (retry_review.get("execution_claim") or {}).get("state")
            == "indeterminate",
            "crash after claim never auto-replays",
            retry_review,
        )

        tampered_review = create_review(queue, "claim-proposal-tamper")
        tampered_id = str(tampered_review["review_id"])
        _, tampered_token = queue.approve_and_claim(
            tampered_id,
            "claim exact proposal",
        )

        def tamper_proposal(state: dict) -> None:
            for item in state.get("items", []):
                if item.get("review_id") == tampered_id:
                    item["proposal"]["action"]["value"] = 2
                    return

        store.mutate_json("review_queue.json", tamper_proposal)
        try:
            queue.update_execution(
                tampered_id,
                {"status": "success"},
                claim_token=tampered_token,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("changed proposal accepted the earlier claim")
        expect(
            next(
                item
                for item in queue.list()
                if item.get("review_id") == tampered_id
            ).get("execution_result")
            is None,
            "proposal digest mismatch remains indeterminate",
        )

    print("review_execution_claim_smoke: ok")


if __name__ == "__main__":
    main()
