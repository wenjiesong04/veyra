from __future__ import annotations

from pathlib import Path
import tempfile
import threading
from typing import Any, Callable
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guardian.review_queue import ReviewQueue
from runtime.extension_deployment_gate import (
    DEPLOYMENT_STATE_FILE,
    ExtensionDeploymentError,
)
from scripts.phase6_extension_deployment_test_support import (
    APPROVER_TOKEN,
    Clock,
    FakeInvocationRunner,
    SESSION,
    OTHER_SESSION,
    OTHER_USER,
    TOKEN,
    USER,
    WORKSPACE,
    admit_review,
    build_gate,
    invoke,
    propose,
    transition,
)


def _revision(gate: Any) -> int:
    return int(gate._read_state()["revision"])


def _advance_to_promoted(gate: Any) -> dict[str, Any]:
    deployment = propose(gate)
    for mode, operation in (
        ("shadow", "security-to-shadow"),
        ("read_only_canary", "security-to-read-only"),
        ("scoped_canary", "security-to-scoped"),
    ):
        review_id = None
        if mode == "scoped_canary":
            review_id = admit_review(
                gate,
                deployment,
                mode,
                review_id="security-review-scoped",
            )
        deployment = transition(
            gate,
            deployment,
            mode,
            operation=operation,
            state_revision=_revision(gate),
            review_id=review_id,
        )
        deployment = invoke(
            gate,
            deployment,
            operation=f"security-invoke-{mode}",
            state_revision=_revision(gate),
        )["deployment"]
    promotion_review = admit_review(
        gate,
        deployment,
        "promoted",
        review_id="security-review-promoted",
    )
    return transition(
        gate,
        deployment,
        "promoted",
        operation="security-to-promoted",
        state_revision=_revision(gate),
        review_id=promotion_review,
    )


def _orphan_registry_fails_closed() -> None:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-orphan-"
    ) as raw:
        clock = Clock()
        gate, _release, _runner, _ = build_gate(Path(raw), clock=clock)
        _advance_to_promoted(gate)

        def orphan(state: dict[str, Any]) -> None:
            state["deployments"] = {}
            state["active_pointers"] = {}
            state["operations"] = {}
            state["invocations"] = {}

        gate.state_store.mutate_json(DEPLOYMENT_STATE_FILE, orphan)
        try:
            rows = gate.public_registry(
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=TOKEN,
            )["items"]
        except ExtensionDeploymentError:
            return
        if rows:
            raise AssertionError(
                "orphan public capability survived without its promoted "
                "deployment and active pointer"
            )


def _revoked_release_is_not_public() -> None:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-revoked-"
    ) as raw:
        clock = Clock()
        gate, release, _runner, _ = build_gate(Path(raw), clock=clock)
        deployment = _advance_to_promoted(gate)
        release.revoked.add(deployment["release_id"])
        rows = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )["items"]
        if any(
            item.get("release_id") == deployment["release_id"]
            for item in rows
        ):
            raise AssertionError(
                "revoked release remained visible as an active public capability"
            )


def _public_registry_is_owner_workspace_session_scoped() -> None:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-scope-"
    ) as raw:
        gate, _release, _runner, _ = build_gate(Path(raw), clock=Clock())
        _advance_to_promoted(gate)
        own = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )["items"]
        other_session = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=OTHER_SESSION,
            control_token=TOKEN,
        )["items"]
        other_owner = gate.public_registry(
            user_id=OTHER_USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )["items"]
        other_workspace = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE + ":other",
            session_id=SESSION,
            control_token=TOKEN,
        )["items"]
        if len(own) != 1 or other_session or other_owner or other_workspace:
            raise AssertionError(
                "public registry crossed owner, workspace, or session scope"
            )


class _BlockingInvocationRunner(FakeInvocationRunner):
    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.started = threading.Event()
        self.resume = threading.Event()

    def run(self, **kwargs: Any) -> Any:
        self.started.set()
        if not self.resume.wait(timeout=5):
            raise RuntimeError("security smoke runner was not resumed")
        return super().run(**kwargs)


def _stale_completion_cannot_cross_disable() -> None:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-race-"
    ) as raw:
        clock = Clock()
        gate, _release, _runner, _ = build_gate(Path(raw), clock=clock)
        runner = _BlockingInvocationRunner(clock)
        gate.invocation_runner = runner
        deployment = propose(gate)
        deployment = transition(
            gate,
            deployment,
            "shadow",
            operation="security-race-to-shadow",
            state_revision=_revision(gate),
        )
        outcome: dict[str, Any] = {}

        def call() -> None:
            try:
                outcome["value"] = gate.invoke(
                    operation_id="security-race-invoke",
                    request_id="security-race-request",
                    deployment_id=deployment["deployment_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                    session_id=SESSION,
                    expected_state_revision=_revision(gate),
                    expected_deployment_revision=deployment["revision"],
                    expected_mode_epoch=deployment["mode_epoch"],
                    input_payload={"name": "race"},
                    control_token=TOKEN,
                )
            except BaseException as exc:  # noqa: BLE001 - thread evidence only.
                outcome["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        if not runner.started.wait(timeout=5):
            raise AssertionError("invocation did not reach the isolated runner")
        current = gate.get(
            deployment_id=deployment["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        gate.disable(
            operation_id="security-disable-during-invocation",
            deployment_id=deployment["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            expected_state_revision=_revision(gate),
            expected_deployment_revision=current["revision"],
            expected_mode_epoch=current["mode_epoch"],
            reason_digest="d" * 64,
            control_token=TOKEN,
        )
        runner.resume.set()
        worker.join(timeout=5)
        if worker.is_alive():
            raise AssertionError("invocation worker did not finish")
        final = gate.get(
            deployment_id=deployment["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        if final["mode"] != "disabled":
            raise AssertionError("explicit disable did not remain authoritative")
        if "disabled" in final["successful_modes"]:
            raise AssertionError(
                "a stale pre-disable completion was credited to disabled mode"
            )
        if "value" in outcome:
            result = outcome["value"].get("result", {})
            if result.get("invocation_status") in {"passed", "discarded"}:
                raise AssertionError(
                    "a stale pre-disable completion was returned as successful"
                )


def _canary_output_is_not_durable() -> None:
    marker = "VEYRA_PHASE6_PRIVATE_CANARY_OUTPUT_7f91"
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-output-"
    ) as raw:
        gate, _release, _runner, _ = build_gate(Path(raw), clock=Clock())
        deployment = propose(gate)
        deployment = transition(
            gate,
            deployment,
            "shadow",
            operation="security-output-to-shadow",
            state_revision=_revision(gate),
        )
        deployment = invoke(
            gate,
            deployment,
            operation="security-output-shadow",
            state_revision=_revision(gate),
        )["deployment"]
        deployment = transition(
            gate,
            deployment,
            "read_only_canary",
            operation="security-output-to-read-only",
            state_revision=_revision(gate),
        )
        response = invoke(
            gate,
            deployment,
            operation="security-output-read-only",
            state_revision=_revision(gate),
            name=marker,
        )
        if marker not in str(response["result"].get("output_payload")):
            raise AssertionError("test fixture did not project the canary marker")
        durable = gate.state_store.path_for(
            DEPLOYMENT_STATE_FILE
        ).read_text(encoding="utf-8")
        if marker in durable:
            raise AssertionError(
                "raw read-only-canary output was persisted in durable state"
            )


def _extension_reviews_use_only_the_dedicated_approver() -> None:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase6-deployment-review-boundary-"
    ) as raw:
        gate, _release, _runner, _ = build_gate(Path(raw), clock=Clock())
        deployment = propose(gate)
        deployment = transition(
            gate,
            deployment,
            "shadow",
            operation="review-boundary-to-shadow",
            state_revision=_revision(gate),
        )
        deployment = invoke(
            gate,
            deployment,
            operation="review-boundary-shadow-invocation",
            state_revision=_revision(gate),
        )["deployment"]
        deployment = transition(
            gate,
            deployment,
            "read_only_canary",
            operation="review-boundary-to-read-only",
            state_revision=_revision(gate),
        )
        deployment = invoke(
            gate,
            deployment,
            operation="review-boundary-read-only-invocation",
            state_revision=_revision(gate),
        )["deployment"]

        reviews: list[dict[str, Any]] = []
        for index in range(4):
            reviews.append(
                gate.request_transition_review(
                    operation_id=f"review-boundary-request-{index}",
                    request_id=f"review-boundary-request-id-{index}",
                    deployment_id=deployment["deployment_id"],
                    target_mode="scoped_canary",
                    user_id=USER,
                    workspace_id=WORKSPACE,
                    session_id=SESSION,
                    expected_state_revision=_revision(gate),
                    expected_deployment_revision=deployment["revision"],
                    expected_mode_epoch=deployment["mode_epoch"],
                    control_token=TOKEN,
                )
            )

        queue = ReviewQueue(gate.state_store)
        mutations: tuple[tuple[str, Callable[[], Any]], ...] = (
            (
                "approve",
                lambda: queue.approve_and_claim(
                    reviews[0]["review_id"], "generic approval"
                ),
            ),
            (
                "reject",
                lambda: queue.decide(
                    reviews[1]["review_id"], "rejected", "generic rejection"
                ),
            ),
            (
                "resolve",
                lambda: queue.mark_resolved(
                    reviews[2]["review_id"], "generic resolution"
                ),
            ),
            (
                "archive",
                lambda: queue.archive(
                    reviews[3]["review_id"], "generic archival"
                ),
            ),
            (
                "authorize execution",
                lambda: queue.authorize_execution(
                    reviews[0]["review_id"], "generic-claim-token"
                ),
            ),
            (
                "update execution",
                lambda: queue.update_execution(
                    reviews[1]["review_id"],
                    {"status": "success"},
                    claim_token="generic-claim-token",
                ),
            ),
        )
        for label, mutation in mutations:
            before = gate.state_store.read_json("review_queue.json")
            try:
                mutation()
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    f"generic {label} mutated a dedicated extension review"
                )
            after = gate.state_store.read_json("review_queue.json")
            if after != before:
                raise AssertionError(
                    f"generic {label} changed the dedicated review queue"
                )

        approved = gate.approve_transition_review(
            review_id=reviews[0]["review_id"],
            expected_review_revision=reviews[0]["review_revision"],
            reason_digest="7" * 64,
            approver_token=APPROVER_TOKEN,
        )
        if (
            approved.get("status") != "approved"
            or approved.get("review_revision") != 2
            or not approved.get("approver_identity_digest")
            or not approved.get("approval_receipt_digest")
            or approved.get("raw_approver_credential_persisted") is not False
        ):
            raise AssertionError(
                "dedicated extension approver did not produce an exact receipt"
            )
        transitioned = transition(
            gate,
            deployment,
            "scoped_canary",
            operation="review-boundary-dedicated-transition",
            state_revision=_revision(gate),
            review_id=approved["review_id"],
        )
        if transitioned.get("mode") != "scoped_canary":
            raise AssertionError(
                "dedicated extension approval was not consumable by its transition"
            )


def main() -> None:
    checks: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "orphan public registry fails closed",
            _orphan_registry_fails_closed,
        ),
        (
            "revoked release is absent from public registry",
            _revoked_release_is_not_public,
        ),
        (
            "public registry is owner workspace session scoped",
            _public_registry_is_owner_workspace_session_scoped,
        ),
        (
            "stale invocation completion cannot cross explicit disable",
            _stale_completion_cannot_cross_disable,
        ),
        (
            "canary output is response-only and not durable",
            _canary_output_is_not_durable,
        ),
        (
            "extension reviews use only the dedicated approver boundary",
            _extension_reviews_use_only_the_dedicated_approver,
        ),
    )
    failures: list[str] = []
    for label, check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - collect every invariant.
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {label}")
    if failures:
        raise SystemExit(
            "Phase 6 deployment security smoke failed "
            f"({len(failures)}/{len(checks)})"
        )
    print(
        "Phase 6 extension deployment security smoke passed: "
        f"{len(checks)}/{len(checks)}"
    )


if __name__ == "__main__":
    main()
