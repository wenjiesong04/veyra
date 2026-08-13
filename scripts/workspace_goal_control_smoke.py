#!/usr/bin/env python3
"""Acceptance smoke for the private, exact-scope Workspace Goal control."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.goal_store_policy import GoalStoreIntegrityError  # noqa: E402
from routers.structured_observations import build_structured_observations_router  # noqa: E402
from runtime.event_awareness_runtime import ShadowAwarenessRuntime  # noqa: E402
from runtime.structured_observation_ingress import StructuredObservationIngress  # noqa: E402
from interface.structured_observation import (  # noqa: E402
    StructuredObservationCommand,
    canonical_utc,
)
from runtime.trusted_workspace_observer import TrustedWorkspaceObserver  # noqa: E402
from runtime.trusted_workspace_observer import (  # noqa: E402
    TrustedWorkspaceObserverConflict,
)


TOKEN = "workspace-goal-smoke-token"
USER = "goal-owner"
SESSION = "goal-session"
OTHER_USER = "other-owner"
OTHER_SESSION = "other-session"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"git setup failed: {args}: {result.stderr}")


def setup_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "workspace-goal-smoke@example.invalid")
    git(root, "config", "user.name", "Veyra Workspace Goal Smoke")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    git(root, "remote", "add", "origin", "git@github.com:veyra-smoke/workspace-goal.git")
    return root.resolve()


def session(store: WorldStateStore, user_id: str, session_id: str) -> None:
    def update(state: dict) -> dict:
        sessions = dict(state.get("sessions") or {})
        sessions[session_id] = {
            "user_id": user_id,
            "dialogue_session_id": session_id,
            "recorded_at": 1.0,
        }
        state["sessions"] = sessions
        return state

    store.mutate_json("channel_state.json", update)


def payload(
    *,
    operation_id: str,
    expected_state_revision: int,
    user_id: str = USER,
    session_id: str = SESSION,
    workspace_id: str,
    title: str = "Observe this workspace",
    description: str | None = "Record-only workspace awareness",
    priority: float = 0.8,
) -> dict:
    return {
        "schema_version": "veyra.workspace_goal.create.v1",
        "operation_id": operation_id,
        "expected_state_revision": expected_state_revision,
        "user_id": user_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "title": title,
        "description": description,
        "priority": priority,
    }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-workspace-goal-") as raw:
        root = Path(raw)
        workspace = setup_repo(root / "repo with spaces")
        wrong_workspace = root / "other"
        wrong_workspace.mkdir()
        store = WorldStateStore(root / "state")
        store.mutate_json(
            "local_world.json",
            lambda state: {**state, "current_project": str(workspace)},
        )
        session(store, USER, SESSION)
        session(store, OTHER_USER, OTHER_SESSION)
        awareness = ShadowAwarenessRuntime(store, mode="record_only")
        ingress = StructuredObservationIngress(
            state_store=store,
            event_awareness=awareness,
            control_token=TOKEN,
        )
        from runtime.workspace_goal_control import WorkspaceGoalControl

        control = WorkspaceGoalControl(store, control_token=TOKEN)
        app = FastAPI()
        app.include_router(
            build_structured_observations_router(
                ingress=ingress,
                workspace_goal_control=control,
            )
        )
        path = "/awareness/structured-observations/workspace/goals"
        headers = {"x-veyra-token": TOKEN}
        with TestClient(app) as client:
            revision = int(store.read_json("user_goals.json").get("_state_revision") or 0)
            unauth = client.post(path, json=payload(
                operation_id="unauth", expected_state_revision=revision,
                workspace_id=str(workspace),
            ))
            expect(unauth.status_code == 401, "Goal create rejects unauthenticated caller")

            missing = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="missing-session",
                    expected_state_revision=revision,
                    session_id="not-registered",
                    workspace_id=str(workspace),
                ),
            )
            expect(missing.status_code == 409, "Goal create requires a registered exact-owner session")

            wrong_owner_session = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="wrong-owner-session",
                    expected_state_revision=revision,
                    session_id=OTHER_SESSION,
                    workspace_id=str(workspace),
                ),
            )
            expect(
                wrong_owner_session.status_code == 409,
                "Goal create rejects a session registered to another owner",
            )

            mismatch = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="wrong-workspace",
                    expected_state_revision=revision,
                    workspace_id=str(wrong_workspace),
                ),
            )
            expect(mismatch.status_code == 409, "Goal create rejects a workspace other than current project")

            extra = client.post(
                path,
                headers=headers,
                json={**payload(
                    operation_id="extra-field",
                    expected_state_revision=revision,
                    workspace_id=str(workspace),
                ), "unexpected": True},
            )
            expect(extra.status_code == 422, "Goal create schema forbids extra fields")

            stale = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="stale-cas",
                    expected_state_revision=revision + 1,
                    workspace_id=str(workspace),
                ),
            )
            expect(stale.status_code == 409, "Goal create enforces state CAS")

            created = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-one",
                    expected_state_revision=revision,
                    workspace_id=str(workspace),
                ),
            )
            expect(created.status_code == 200, "Goal create succeeds for exact current scope")
            receipt = created.json()
            expect(
                receipt.get("status") == "active"
                and receipt.get("replayed") is False
                and not any(receipt.get("authority", {}).values()),
                "Goal receipt is active and all authority remains false",
            )
            goal_bytes = store.path_for("user_goals.json").read_bytes()
            expect(
                str(workspace).encode("utf-8") not in goal_bytes
                and b'"workspace_ref":"workspace:' in goal_bytes.replace(b" ", b""),
                "durable Goal stores an opaque workspace binding without a raw path",
            )

            replay = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-one",
                    expected_state_revision=revision,
                    workspace_id=str(workspace),
                ),
            )
            expect(
                replay.status_code == 200
                and replay.json().get("replayed") is True
                and store.path_for("user_goals.json").read_bytes() == goal_bytes,
                "exact Goal replay is byte-pure",
            )

            rebound = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-one",
                    expected_state_revision=revision,
                    workspace_id=str(workspace),
                    title="Rebound operation",
                ),
            )
            expect(rebound.status_code == 409, "operation rebind is rejected")

            duplicate = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-two",
                    expected_state_revision=int(store.read_json("user_goals.json").get("_state_revision") or 0),
                    workspace_id=str(workspace),
                ),
            )
            expect(duplicate.status_code == 409, "duplicate active owner/session/workspace is rejected")

            other_revision = int(store.read_json("user_goals.json").get("_state_revision") or 0)
            other = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-one",
                    expected_state_revision=other_revision,
                    user_id=OTHER_USER,
                    session_id=OTHER_SESSION,
                    workspace_id=str(workspace),
                    title="Other owner Goal",
                ),
            )
            expect(
                other.status_code == 200,
                "the same client operation id is isolated across exact owner sessions",
            )

            # An acknowledged operation replays before fresh-admission checks.
            # This lets a client recover its immutable receipt after the live
            # session registry or current-project selection has moved on.
            replay_goal_bytes = store.path_for("user_goals.json").read_bytes()
            store.mutate_json(
                "local_world.json",
                lambda state: {**state, "current_project": str(wrong_workspace)},
            )
            store.mutate_json(
                "channel_state.json",
                lambda state: {
                    **state,
                    "sessions": {
                        key: value
                        for key, value in dict(state.get("sessions") or {}).items()
                        if key != SESSION
                    },
                },
            )
            replay_after_drift = client.post(
                path,
                headers=headers,
                json=payload(
                    operation_id="create-one",
                    expected_state_revision=0,
                    workspace_id=str(workspace),
                ),
            )
            expect(
                replay_after_drift.status_code == 200
                and replay_after_drift.json().get("replayed") is True
                and store.path_for("user_goals.json").read_bytes()
                == replay_goal_bytes,
                "acknowledged Goal replay survives session and project drift byte-pure",
            )
            store.mutate_json(
                "local_world.json",
                lambda state: {**state, "current_project": str(workspace)},
            )
            session(store, USER, SESSION)

            owner_list = client.get(
                path,
                headers=headers,
                params={"user_id": USER, "session_id": SESSION},
            )
            expect(
                owner_list.status_code == 200
                and owner_list.json().get("count") == 1
                and owner_list.json()["items"][0].get("user_id") == USER,
                "Goal list is exact-owner scoped",
            )
            unauth_list = client.get(path, params={"user_id": USER, "session_id": SESSION})
            expect(unauth_list.status_code == 401, "Goal list requires token")

        # The generic structured ingress preserves the session-bound Goal
        # contract instead of treating it like a legacy user-scoped Goal.
        from datetime import datetime, timedelta, timezone

        observed_at = datetime.now(timezone.utc)
        cross_session_command = StructuredObservationCommand.model_validate(
            {
                "schema_version": "veyra.structured_observation.command.v1",
                "operation_id": "workspace-goal-cross-session",
                "producer_receipt_id": "workspace-goal-cross-session-receipt",
                "expected_event_inbox_revision": int(
                    store.read_json("event_inbox.json").get("_state_revision") or 0
                ),
                "producer_id": "local_operator",
                "user_id": USER,
                "session_id": OTHER_SESSION,
                "workspace_id": str(workspace),
                "occurred_at": canonical_utc(observed_at),
                "valid_until": canonical_utc(observed_at + timedelta(minutes=5)),
                "facts": {
                    "kind": "risk_signal",
                    "state": "degraded",
                    "severity": "high",
                    "urgency": "soon",
                    "novelty": "new",
                    "uncertainty": "low",
                    "evidence_quality": "direct",
                    "epistemic_status": "observed",
                },
                "anchors": [
                    {"kind": "goal", "ref_id": receipt["goal"]["goal_id"]}
                ],
                "evidence": [
                    {
                        "evidence_id": "workspace-goal-session-evidence",
                        "source": "human_verified",
                    }
                ],
            },
            strict=True,
        )
        inbox_bytes = store.path_for("event_inbox.json").read_bytes()
        try:
            ingress.submit(cross_session_command, control_token=TOKEN)
        except Exception as exc:
            expect(
                exc.__class__.__name__ == "StructuredObservationConflictError"
                and store.path_for("event_inbox.json").read_bytes() == inbox_bytes,
                "cross-session structured Goal anchor fails closed and byte-pure",
            )
        else:
            raise AssertionError("structured ingress accepted a cross-session workspace Goal")

        # Legacy learning updates retain controlled Goals even when their
        # historical list exceeds the old 100-item truncation boundary.
        controlled_ids = {
            receipt["goal"]["goal_id"],
            other.json()["goal"]["goal_id"],
        }

        def add_ordinary_goals(state: dict) -> dict:
            goals = list(state.get("goals") or [])
            goals.extend(
                {
                    "goal_id": f"ordinary-{index}",
                    "kind": "learning",
                    "source": "legacy_learning",
                    "status": "active",
                    "topic": f"topic-{index}",
                    "user_id": USER,
                }
                for index in range(110)
            )
            state["goals"] = goals
            return state

        store.mutate_json("user_goals.json", add_ordinary_goals)
        CommitmentCore(store)._upsert_user_goal(
            {
                "kind": "learning",
                "topic": "retention-check",
                "user_id": USER,
                "status": "active",
            }
        )
        retained = store.read_json("user_goals.json").get("goals") or []
        retained_ids = {
            str(item.get("goal_id") or "")
            for item in retained
            if isinstance(item, dict)
        }
        expect(
            len(retained) == CommitmentCore.MAX_GOALS
            and controlled_ids <= retained_ids,
            "legacy Goal retention stays bounded without evicting controlled Goals",
        )

        update_store = WorldStateStore(root / "goal-retention-state")
        update_store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    {
                        "goal_id": f"ordinary-{index}",
                        "kind": "learning",
                        "source": "legacy_learning",
                        "status": "active",
                        "topic": "oldest" if index == 0 else f"topic-{index}",
                        "user_id": USER,
                    }
                    for index in range(CommitmentCore.MAX_GOALS)
                ],
            },
        )
        updated_oldest = CommitmentCore(update_store)._upsert_user_goal(
            {
                "kind": "learning",
                "topic": "oldest",
                "user_id": USER,
                "status": "active",
                "title": "Updated oldest Goal",
            }
        )
        update_goals = update_store.read_json("user_goals.json").get("goals") or []
        expect(
            any(
                item.get("goal_id") == updated_oldest.get("goal_id")
                and item.get("title") == "Updated oldest Goal"
                for item in update_goals
                if isinstance(item, dict)
            ),
            "updating an old ordinary Goal keeps the touched record durable",
        )

        corrupt_store = WorldStateStore(root / "goal-corrupt-state")
        corrupt_store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    {
                        "schema_version": "forged",
                        "goal_id": "malformed-reserved",
                        "kind": "workspace_observation",
                        "source": "workspace_goal_control",
                        "status": "active",
                    }
                ],
            },
        )
        corrupt_bytes = corrupt_store.path_for("user_goals.json").read_bytes()
        try:
            CommitmentCore(corrupt_store)._upsert_user_goal(
                {
                    "kind": "learning",
                    "topic": "must-not-normalize",
                    "user_id": USER,
                    "status": "active",
                }
            )
        except GoalStoreIntegrityError:
            pass
        else:
            raise AssertionError("malformed reserved Goal was normalized")
        expect(
            corrupt_store.path_for("user_goals.json").read_bytes() == corrupt_bytes,
            "malformed reserved Goal state fails closed and byte-pure",
        )

        # Fresh admission reads current-project and session ownership under
        # the same root writer fence as the Goal append. A competing writer
        # can only linearize after the admitted Goal is durable.
        race_store = WorldStateStore(root / "race-state")
        race_store.mutate_json(
            "local_world.json",
            lambda state: {**state, "current_project": str(workspace)},
        )
        session(race_store, USER, SESSION)
        admission_checked = threading.Event()
        release_admission = threading.Event()
        rival_started = threading.Event()
        rival_finished = threading.Event()
        race_result: dict = {}

        class PausingGoalControl(WorkspaceGoalControl):
            def _require_registered_session(self, *, user_id: str, session_id: str) -> None:
                super()._require_registered_session(
                    user_id=user_id,
                    session_id=session_id,
                )
                admission_checked.set()
                if not release_admission.wait(timeout=5):
                    raise RuntimeError("race smoke admission timeout")

        race_control = PausingGoalControl(race_store, control_token=TOKEN)

        def create_during_race() -> None:
            race_result["receipt"] = race_control.create(
                control_token=TOKEN,
                operation_id="atomic-admission",
                expected_state_revision=int(
                    race_store.read_json("user_goals.json").get("_state_revision")
                    or 0
                ),
                user_id=USER,
                session_id=SESSION,
                workspace_id=str(workspace),
                title="Atomic workspace admission",
                description=None,
                priority=0.8,
            )

        def rebind_during_race() -> None:
            rival_started.set()
            with race_store.writer_transaction():
                race_store.mutate_json(
                    "local_world.json",
                    lambda state: {
                        **state,
                        "current_project": str(wrong_workspace),
                    },
                )
                race_store.mutate_json(
                    "channel_state.json",
                    lambda state: {**state, "sessions": {}},
                )
            rival_finished.set()

        creator = threading.Thread(target=create_during_race)
        creator.start()
        expect(admission_checked.wait(timeout=5), "atomic Goal race reaches admission fence")
        rival = threading.Thread(target=rebind_during_race)
        rival.start()
        expect(rival_started.wait(timeout=5), "competing state rebind starts")
        time.sleep(0.05)
        expect(
            not rival_finished.is_set(),
            "project and session rebind cannot cross the Goal writer fence",
        )
        release_admission.set()
        creator.join(timeout=5)
        rival.join(timeout=5)
        expect(
            race_result.get("receipt", {}).get("status") == "active"
            and rival_finished.is_set(),
            "Goal admission and competing rebind linearize without a torn binding",
        )

        # The observer consumes the Goal produced above only in record_only
        # mode; no delivery/execution authority is implied by the binding.
        observer = TrustedWorkspaceObserver(
            state_store=store,
            publish_observation=ingress.issue_workspace_observer_publisher(),
            control_token=TOKEN,
        )
        observer_revision = int(
            store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0
        )
        try:
            observer.configure(
                control_token=TOKEN,
                expected_state_revision=observer_revision,
                mode="record_only",
                user_id=USER,
                session_id=OTHER_SESSION,
                workspace_id=str(workspace),
                goal_id=receipt["goal"]["goal_id"],
            )
        except TrustedWorkspaceObserverConflict:
            pass
        else:
            raise AssertionError("observer accepted Goal from another session")
        expect(
            int(
                store.read_json("trusted_workspace_observer_state.json").get(
                    "_state_revision"
                )
                or 0
            )
            == observer_revision,
            "observer rejects cross-session Goal without changing state",
        )
        configured = observer.configure(
            control_token=TOKEN,
            expected_state_revision=observer_revision,
            mode="record_only",
            user_id=USER,
            session_id=SESSION,
            workspace_id=str(workspace),
            goal_id=receipt["goal"]["goal_id"],
        )
        expect(
            configured.get("status") == "configured"
            and configured.get("mode") == "record_only"
            and not any(configured.get("authority", {}).values()),
            "record_only observer accepts the exact active Goal without authority expansion",
        )

    print("Workspace Goal control smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
