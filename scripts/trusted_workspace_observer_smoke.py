#!/usr/bin/env python3
"""Capability acceptance for the bounded TrustedWorkspaceObserver slice."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from runtime.event_awareness_runtime import ShadowAwarenessRuntime  # noqa: E402
from runtime.structured_observation_ingress import (  # noqa: E402
    StructuredObservationIngress,
    StructuredObservationUnauthorizedError,
)
from runtime.trusted_workspace_observer import (  # noqa: E402
    TrustedWorkspaceObserver,
    TrustedWorkspaceObserverConflict,
)
from runtime.isolated_git_snapshot import capture_isolated_git_observation  # noqa: E402
from routers.structured_observations import build_structured_observations_router  # noqa: E402


TOKEN = "trusted-workspace-smoke-token"


class FakeCI:
    """Deterministic exact-SHA provider contract for capability tests."""

    def __init__(self) -> None:
        self.signal_state: str | None = None
        self.probe_suffix = 0

    def _validated_binding(self, value):
        if not isinstance(value, dict) or value.get("api_origin") != "https://api.github.com":
            raise ValueError("invalid provider binding")
        return dict(value)

    def observe(self, *, binding, target_ref, target_sha, now):
        if self.signal_state is None:
            return None
        return {
            "repo_id": "veyra-smoke/fixture",
            "target_ref": target_ref,
            "target_sha": target_sha,
            "signal_state": self.signal_state,
            "probe_digest": hashlib.sha256(
                f"ci:{self.signal_state}:{target_sha}:{self.probe_suffix}".encode()
            ).hexdigest(),
        }


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git setup failed: {args}: {result.stderr}")


def setup_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "veyra-smoke@example.invalid")
    git(root, "config", "user.name", "Veyra Smoke")
    (root / "app.py").write_text("print('baseline')\n", encoding="utf-8")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    git(root, "remote", "add", "origin", "git@github.com:veyra-smoke/fixture.git")
    return root.resolve()


def build_runtime(state_root: Path, workspace: Path, *, clock=None):
    store = WorldStateStore(state_root)
    workspace_text = str(workspace)
    store.mutate_json(
        "local_world.json",
        lambda state: {**state, "current_project": workspace_text},
    )
    store.mutate_json(
        "user_goals.json",
        lambda state: {
            **state,
            "goals": [
                {
                    "goal_id": "goal-smoke",
                    "user_id": "smoke-owner",
                    "workspace_id": workspace_text,
                    "status": "active",
                    "goal_priority": 0.9,
                }
            ],
        },
    )
    store.mutate_json(
        "ops_config.json",
        lambda state: {
            **state,
            "event_awareness": {
                "mode": "record_only",
                "mode_epoch": 1,
                "allowed_modes": ["disabled", "record_only", "shadow"],
            },
            "general_suggestions": {
                "mode": "record_only",
                "mode_epoch": 1,
                "allowed_modes": ["disabled", "record_only", "shadow", "advise_only"],
            },
        },
    )
    awareness = ShadowAwarenessRuntime(store, mode="record_only")
    ingress = StructuredObservationIngress(
        state_store=store,
        event_awareness=awareness,
        control_token=TOKEN,
    )
    observer = TrustedWorkspaceObserver(
        state_store=store,
        publish_observation=ingress.issue_workspace_observer_publisher(),
        control_token=TOKEN,
        grace_seconds=1,
        clock=clock,
        snapshotter=(
            (lambda path: capture_isolated_git_observation(path, clock=clock))
            if clock is not None
            else None
        ),
    )
    return store, awareness, ingress, observer


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-trusted-workspace-") as raw:
        root = Path(raw)
        workspace = setup_repo(root / "repo")
        current_time = [datetime.now(timezone.utc)]
        clock = lambda: current_time[0]
        store, awareness, ingress, observer = build_runtime(
            root / "state", workspace, clock=clock
        )
        revision = int(store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0)
        configured = observer.configure(
            control_token=TOKEN,
            expected_state_revision=revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="smoke-session",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
        )
        expect(configured["status"] == "configured", "explicit CAS configuration")
        api = FastAPI()
        api.include_router(
            build_structured_observations_router(
                ingress=ingress,
                workspace_observer=observer,
            )
        )
        with TestClient(api) as client:
            status_response = client.get(
                "/awareness/structured-observations/workspace/status"
            )
            expect(status_response.status_code == 200, "workspace status route is readable")
            unauthenticated = client.post(
                "/awareness/structured-observations/workspace/run-once",
                json={
                    "schema_version": "veyra.trusted_workspace_observer.run.v1",
                    "reason": "smoke_unauthenticated",
                },
            )
            expect(unauthenticated.status_code == 401, "workspace run route requires token")
        baseline = observer.run_once(reason="smoke_baseline")
        expect(baseline["events_published"] == 0, "first baseline is silent")
        unchanged = observer.run_once(reason="smoke_unchanged")
        expect(unchanged["events_published"] == 0, "unchanged workspace is silent")
        # Adding another owner's Goal must not invalidate the exact bound
        # Goal; only mutation of the bound record is a drift.
        store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    *state["goals"],
                    {
                        "goal_id": "other-owner-goal",
                        "user_id": "other-owner",
                        "workspace_id": str(workspace),
                        "status": "active",
                        "goal_priority": 0.1,
                    },
                ],
            },
        )
        expect(
            observer.run_once(reason="smoke_unrelated_goal")["events_published"] == 0,
            "unrelated owner Goal does not invalidate binding",
        )

        (workspace / "app.py").write_text("print('changed')\n", encoding="utf-8")
        dirty = observer.run_once(reason="smoke_code_dirty")
        expect(dirty["events_published"] == 1, "code dirty emits one record-only change")
        duplicate = observer.run_once(reason="smoke_duplicate")
        expect(duplicate["events_published"] == 0, "duplicate dirty observation is silent")
        current_time[0] += timedelta(seconds=2)
        risk = observer.run_once(reason="smoke_grace_elapsed")
        expect(risk["events_published"] == 1, "dirty validation gap emits after grace")
        awareness.process_pending(limit=20)
        suggestions = store.read_json("suggestion_outbox.json")
        proposal_values = [
            value
            for value in (suggestions.get("proposals") or {}).values()
            if isinstance(value, dict)
        ]
        hypotheses = store.read_json("attention_hypothesis_state.json").get("hypotheses") or {}
        expect(
            int(
                suggestions.get("proposal_count")
                or suggestions.get("suggestion_record_count")
                or 0
            )
            >= 1,
            "real producer events reach the record-only suggestion outbox",
        )
        expect(
            len(proposal_values) == 1
            and proposal_values[0].get("user_id") == "smoke-owner"
            and proposal_values[0].get("session_id") == "smoke-session"
            and proposal_values[0].get("mode") == "record_only"
            and any(
                isinstance(item, dict) and item.get("status") == "confirmed"
                for item in hypotheses.values()
            ),
            "exact-owner GeneralSituation/Attention reaches one record-only proposal",
        )
        # The observer's opaque workspace identity must not leak the local
        # filesystem path into durable downstream state.
        raw_workspace = str(workspace)
        for logical_name in (
            "event_inbox.json",
            "general_situation_state.json",
            "attention_hypothesis_state.json",
            "suggestion_outbox.json",
        ):
            encoded = (store.path_for(logical_name).read_text(encoding="utf-8")
                       if store.path_for(logical_name).exists() else "")
            expect(raw_workspace not in encoded, f"{logical_name} redacts raw workspace path")

        # Documentation-only changes never create a candidate.
        git(workspace, "restore", "app.py")
        (workspace / "README.md").write_text("docs only\n", encoding="utf-8")
        docs = observer.run_once(reason="smoke_docs_only")
        expect(docs["events_published"] == 0, "docs-only change is silent")
        nested_untracked = workspace / "docs" / "newtree" / "code.py"
        nested_untracked.parent.mkdir(parents=True, exist_ok=True)
        nested_untracked.write_text("print('untracked')\n", encoding="utf-8")
        nested_snapshot = capture_isolated_git_observation(workspace)
        expect(
            nested_snapshot.dirty
            and nested_snapshot.category_counts.get("docs", 0) >= 1
            and nested_snapshot.category_counts.get("code", 0) >= 1,
            "all nested untracked paths are classified without path leakage",
        )
        nested_untracked.unlink()

        # A tracked parent directory replaced by a symlink must never let the
        # manifest read content outside the bound workspace.
        git(workspace, "restore", "README.md")
        package = workspace / "pkg"
        package.mkdir(exist_ok=True)
        (package / "a.py").write_text("print('inside')\n", encoding="utf-8")
        git(workspace, "add", "pkg/a.py")
        git(workspace, "commit", "-qm", "add package")
        outside = root / "outside"
        outside.mkdir()
        (outside / "a.py").write_text("print('outside')\n", encoding="utf-8")
        (package / "a.py").unlink()
        package.rmdir()
        os.symlink(outside, package, target_is_directory=True)
        try:
            capture_isolated_git_observation(workspace)
        except Exception as exc:
            expect(
                getattr(exc, "reason_code", "") == "unsupported_layout",
                "symlinked parent cannot escape the bound workspace",
            )
        else:
            raise AssertionError("symlinked parent escaped the workspace")
        package.unlink()
        git(workspace, "restore", "pkg/a.py")
        disabled_store, _disabled_awareness, _disabled_ingress, disabled_observer = build_runtime(
            root / "disabled-state", workspace
        )
        (workspace / "app.py").write_text("dirty while disabling\n", encoding="utf-8")
        disabled_revision = int(
            disabled_store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0
        )
        disabled_configured = disabled_observer.configure(
            control_token=TOKEN,
            expected_state_revision=disabled_revision,
            mode="disabled",
            user_id="smoke-owner",
            session_id="disabled-session",
            workspace_id=str(root / "does-not-exist"),
            goal_id="missing-goal",
        )
        expect(
            disabled_configured["mode"] == "disabled"
            and disabled_observer.run_once(reason="disabled_dirty")["status"] == "disabled",
            "disabled configuration succeeds without Git/Goal/workspace probing",
        )
        git(workspace, "restore", "app.py")

        # A generic HTTP-shaped command cannot impersonate the in-process
        # producer, even with the correct control token.
        fake = {
            "schema_version": "veyra.structured_observation.command.v1",
            "operation_id": "http-impersonation",
            "producer_id": "workspace_observer",
            "producer_receipt_id": "fake-receipt",
            "user_id": "smoke-owner",
            "workspace_id": str(workspace),
            "session_id": "smoke-session",
            "expected_event_inbox_revision": int(ingress.status()["event_inbox_revision"]),
            "occurred_at": "2026-08-12T00:00:00.000000Z",
            "valid_until": "2026-08-12T00:30:00.000000Z",
            "anchors": [{"kind": "goal", "ref_id": "goal-smoke"}, {"kind": "entity", "ref_id": "workspace:fake"}],
            "evidence": [{"evidence_id": "fake", "source": "direct_tool_observation"}],
            "facts": {
                "kind": "change_signal", "state": "changed", "severity": "moderate", "urgency": "soon",
                "novelty": "new", "uncertainty": "low", "evidence_quality": "direct", "epistemic_status": "observed",
            },
        }
        try:
            ingress.submit(fake, control_token=TOKEN)
        except StructuredObservationUnauthorizedError:
            print("PASS HTTP producer impersonation rejected")
        else:
            raise AssertionError("HTTP producer impersonation was accepted")

        # CAS and scope drift fail closed; no Goal is synthesized.
        try:
            observer.configure(
                control_token=TOKEN,
                expected_state_revision=0,
                mode="record_only",
                user_id="smoke-owner",
                session_id="smoke-session",
                workspace_id=str(workspace),
                goal_id="goal-smoke",
            )
        except TrustedWorkspaceObserverConflict:
            print("PASS stale configuration CAS rejected")
        else:
            raise AssertionError("stale observer CAS was accepted")
        expect(
            not any(observer.status()["authority"].values()),
            "observer authority remains fully record-only",
        )

        # Reconstructing the producer from the same durable state is replay
        # safe: no duplicate observation is emitted after restart.
        # A restart test uses the already durable state, so construct directly
        # against the original store with a fresh in-process publisher.
        restarted_ingress = StructuredObservationIngress(
            state_store=store,
            event_awareness=awareness,
            control_token=TOKEN,
        )
        restarted = TrustedWorkspaceObserver(
            state_store=store,
            publish_observation=restarted_ingress.issue_workspace_observer_publisher(),
            control_token=TOKEN,
            grace_seconds=1,
        )
        replay = restarted.run_once(reason="smoke_restart_replay")
        expect(replay["events_published"] == 0, "restart/replay does not duplicate")

        # Crash after durable ingress but before observer-state finalization:
        # pending delivery retains exact operation timestamps and the restart
        # replays the same EventInbox event instead of rebinding a digest.
        git(workspace, "restore", "README.md")
        crash_store = WorldStateStore(root / "crash-state")
        crash_store.mutate_json(
            "local_world.json", lambda state: {**state, "current_project": str(workspace)}
        )
        crash_store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    {
                        "goal_id": "goal-smoke",
                        "user_id": "smoke-owner",
                        "workspace_id": str(workspace),
                        "status": "active",
                        "goal_priority": 0.9,
                    }
                ],
            },
        )
        crash_awareness = ShadowAwarenessRuntime(crash_store, mode="record_only")
        crash_ingress = StructuredObservationIngress(
            state_store=crash_store, event_awareness=crash_awareness, control_token=TOKEN
        )
        crash_publisher = crash_ingress.issue_workspace_observer_publisher()
        crashed = [False]

        def crash_after_ingress(command):
            result = crash_publisher(command)
            if not crashed[0]:
                crashed[0] = True
                raise RuntimeError("simulated process crash after ingress")
            return result

        crash_observer = TrustedWorkspaceObserver(
            state_store=crash_store,
            publish_observation=crash_after_ingress,
            control_token=TOKEN,
            snapshotter=lambda path: capture_isolated_git_observation(path),
        )
        crash_revision = int(crash_store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0)
        crash_observer.configure(
            control_token=TOKEN,
            expected_state_revision=crash_revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="crash-session",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
        )
        expect(crash_observer.run_once(reason="crash_baseline")["events_published"] == 0, "crash baseline is silent")
        (workspace / "app.py").write_text("crash dirty\n", encoding="utf-8")
        crash_first = crash_observer.run_once(reason="crash_after_ingress")
        expect(
            crash_first["status"] == "degraded"
            and crash_store.read_json("trusted_workspace_observer_state.json").get("pending_delivery"),
            "post-ingress crash leaves bounded pending delivery",
        )
        (workspace / "app.py").write_text("crash dirty newer\n", encoding="utf-8")
        crash_restart_ingress = StructuredObservationIngress(
            state_store=crash_store, event_awareness=crash_awareness, control_token=TOKEN
        )
        crash_restart = TrustedWorkspaceObserver(
            state_store=crash_store,
            publish_observation=crash_restart_ingress.issue_workspace_observer_publisher(),
            control_token=TOKEN,
            snapshotter=lambda path: capture_isolated_git_observation(path),
        )
        crash_recovered = crash_restart.run_once(reason="crash_recovery_replay")
        expect(
            crash_recovered["events_published"] == 1
            and crash_store.read_json("trusted_workspace_observer_state.json").get("pending_delivery") is None,
            "restart replays exact pending event and finalizes",
        )
        crash_events_before_new = len(
            crash_store.read_json("event_inbox.json").get("events") or {}
        )
        crash_new_subject = crash_restart.run_once(reason="crash_new_subject")
        expect(
            crash_new_subject["events_published"] == 1
            and len(crash_store.read_json("event_inbox.json").get("events") or {})
            == crash_events_before_new + 1,
            "pending recovery leaves a newer workspace subject for the next tick",
        )
        git(workspace, "restore", "app.py")

        # Pre-ingress crash with an expired, missing event rotates the pending
        # delivery epoch instead of retrying a forever-invalid command.
        pre_store, pre_awareness, pre_ingress, pre_base = build_runtime(
            root / "pre-ingress-state", workspace
        )
        pre_revision = int(pre_store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0)
        pre_base.configure(
            control_token=TOKEN,
            expected_state_revision=pre_revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="pre-ingress",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
        )
        expect(pre_base.run_once(reason="pre_baseline")["events_published"] == 0, "pre-ingress baseline is silent")
        (workspace / "app.py").write_text("pre ingress crash\n", encoding="utf-8")
        pre_fail = TrustedWorkspaceObserver(
            state_store=pre_store,
            publish_observation=lambda command: (_ for _ in ()).throw(RuntimeError("before ingress")),
            control_token=TOKEN,
            clock=lambda: datetime.now(timezone.utc),
        )
        # Reuse the configured binding while intentionally leaving EventInbox
        # empty; the first attempt stores a prepared pending record.
        pre_fail_result = pre_fail.run_once(reason="pre_ingress_crash")
        expect(
            pre_fail_result["status"] == "degraded"
            and pre_store.read_json("event_inbox.json").get("events") == {},
            "pre-ingress crash leaves no event",
        )
        pre_pending = pre_store.read_json("trusted_workspace_observer_state.json").get("pending_delivery")
        expect(isinstance(pre_pending, dict), "pre-ingress crash persists pending record")
        # A pending row cannot attest its own acknowledgement.  Even a valid
        # nested record with a forged acknowledged flag must be reconciled
        # against the exact EventInbox receipt, which is still absent here.
        pre_store.mutate_json(
            "trusted_workspace_observer_state.json",
            lambda state: {
                **state,
                "pending_delivery": {
                    **state["pending_delivery"],
                    "acknowledged_kinds": ["change_signal"],
                },
            },
        )
        pre_restarted = TrustedWorkspaceObserver(
            state_store=pre_store,
            publish_observation=pre_base.publish_observation,
            control_token=TOKEN,
        )
        forged_ack_recovery = pre_restarted.run_once(reason="forged_ack_recovery")
        expect(
            forged_ack_recovery["events_published"] == 1
            and len(pre_store.read_json("event_inbox.json").get("events") or {}) == 1,
            "pending acknowledgement requires an exact durable EventInbox receipt",
        )

        # Create a second missing pending transition for the cross-TTL
        # recovery case below.
        (workspace / "app.py").write_text("pre ingress crash expired\n", encoding="utf-8")
        pre_fail_result = pre_fail.run_once(reason="pre_ingress_crash_expired")
        expect(
            pre_fail_result["status"] == "degraded"
            and isinstance(
                pre_store.read_json("trusted_workspace_observer_state.json").get("pending_delivery"),
                dict,
            ),
            "second pre-ingress crash prepares a distinct pending transition",
        )
        pre_store.mutate_json(
            "trusted_workspace_observer_state.json",
            lambda state: {
                **state,
                "pending_delivery": {
                    **state["pending_delivery"],
                    "occurred_at": "1999-12-31T23:00:00Z",
                    "valid_until": "2000-01-01T00:00:00Z",
                },
            },
        )
        # The current workspace may have returned to clean/docs-only while
        # the immutable old dirty transition is pending.  Recovery must finish
        # the old operation first; classification of the clean snapshot is a
        # later tick.
        git(workspace, "restore", "app.py")
        rotated = pre_restarted.run_once(reason="pre_expired_recovery")
        expect(
            rotated["events_published"] == 1
            and pre_store.read_json("trusted_workspace_observer_state.json").get("pending_delivery") is None,
            "expired missing event rotates delivery epoch and recovers",
        )
        git(workspace, "restore", "app.py")

        git(workspace, "restore", "README.md")
        race_store, race_awareness, race_ingress, race_observer = build_runtime(
            root / "race-state", workspace
        )
        race_revision = int(
            race_store.read_json("trusted_workspace_observer_state.json").get("_state_revision")
            or 0
        )
        race_observer.configure(
            control_token=TOKEN,
            expected_state_revision=race_revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="race-session",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
        )
        original_snapshotter = race_observer._snapshotter
        raced = [False]

        def racing_snapshotter(path):
            snapshot = original_snapshotter(path)
            if not raced[0]:
                raced[0] = True
                race_store.mutate_json(
                    "trusted_workspace_observer_state.json",
                    lambda state: {
                        **state,
                        "binding": {
                            **state["binding"],
                            "binding_generation": int(
                                state["binding"].get("binding_generation") or 0
                            )
                            + 1,
                        },
                    },
                )
            return snapshot

        race_observer._snapshotter = racing_snapshotter
        race = race_observer.run_once(reason="smoke_generation_race")
        expect(
            race["status"] == "degraded"
            and race["events_published"] == 0
            and isinstance(
                race_store.read_json("trusted_workspace_observer_state.json").get("baseline"),
                dict,
            )
            and race_store.read_json("trusted_workspace_observer_state.json").get("last_observation")
            == race_store.read_json("trusted_workspace_observer_state.json").get("baseline"),
            "binding generation race fails closed without advancing baseline/event",
        )

        # If another process changes the binding after ingress admission, the
        # response must preserve the durable event count instead of claiming
        # that nothing was published.
        post_root = root / "post-publish-race-state"
        post_store, _post_awareness, post_ingress, post_observer = build_runtime(
            post_root, workspace
        )
        post_revision = int(
            post_store.read_json("trusted_workspace_observer_state.json").get("_state_revision")
            or 0
        )
        post_observer.configure(
            control_token=TOKEN,
            expected_state_revision=post_revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="post-race-session",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
        )
        real_post_publish = post_observer.publish_observation
        competing_store = WorldStateStore(post_root)
        competing_observer = TrustedWorkspaceObserver(
            state_store=competing_store,
            publish_observation=lambda _command: {},
            control_token=TOKEN,
        )

        def publish_then_disable(command):
            published = real_post_publish(command)
            competing_revision = int(
                competing_store.read_json("trusted_workspace_observer_state.json").get("_state_revision")
                or 0
            )
            competing_observer.configure(
                control_token=TOKEN,
                expected_state_revision=competing_revision,
                mode="disabled",
                user_id="smoke-owner",
                session_id="post-race-session",
                workspace_id=str(workspace),
                goal_id="goal-smoke",
            )
            return published

        post_observer.publish_observation = publish_then_disable
        (workspace / "app.py").write_text("post publish race\n", encoding="utf-8")
        post_result = post_observer.run_once(reason="post_publish_generation_race")
        expect(
            post_result["status"] == "degraded"
            and post_result["reason"] == "published_before_binding_change"
            and post_result["events_attempted"] == 1
            and post_result["events_published"] == 1
            and post_result["durable_events_published"] == 1
            and len(post_store.read_json("event_inbox.json").get("events") or {}) == 1,
            "post-publish generation race reports the durable event truthfully",
        )
        git(workspace, "restore", "app.py")

        # Goal content and owner/workspace scope are part of the binding.  A
        # changed priority is not silently accepted as the same project.
        store.mutate_json(
            "user_goals.json",
            lambda state: {
                **state,
                "goals": [
                    {
                        **state["goals"][0],
                        "goal_priority": 0.2,
                    }
                ],
            },
        )
        drift = restarted.run_once(reason="smoke_goal_drift")
        expect(drift["status"] == "degraded", "Goal digest/priority drift fails closed")

        corrupt_store, _corrupt_awareness, _corrupt_ingress, corrupt_observer = build_runtime(
            root / "corrupt-state", workspace
        )
        corrupt_store.mutate_json(
            "trusted_workspace_observer_state.json",
            lambda state: {**state, "binding": {**(state.get("binding") or {}), "status": "record_ony"}},
        )
        expect(
            corrupt_observer.run_once(reason="corrupt_mode")["status"] == "degraded",
            "corrupt observer mode fails closed",
        )

        # CI is optional but exact-SHA and fail-closed.  A pending provider
        # response does not advance the clean revision baseline; a later
        # exact-SHA failure produces the pair of change+risk events.
        ci_root = root / "ci-state"
        git(workspace, "restore", "README.md")
        ci_store, ci_awareness, ci_ingress, ci_observer = build_runtime(ci_root, workspace)
        fake_ci = FakeCI()
        ci_observer.ci_provider = fake_ci
        ci_revision = int(ci_store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0)
        ci_observer.configure(
            control_token=TOKEN,
            expected_state_revision=ci_revision,
            mode="record_only",
            user_id="smoke-owner",
            session_id="ci-session",
            workspace_id=str(workspace),
            goal_id="goal-smoke",
            ci_binding={
                "schema_version": "veyra.project_guardian.github_actions_ci.v1",
                "provider": "github_actions",
                "api_origin": "https://api.github.com",
                "repo_id": "veyra-smoke/fixture",
                "repository_id": 1,
                "workflow_id": 2,
                "workflow_path": ".github/workflows/gate.yml",
                "event": "push",
                "expected_app_id": 3,
                "required_jobs": ["gate"],
                "policy_digest": "test-policy",
            },
        )
        initial_ci_pending = ci_observer.run_once(reason="ci_initial_unknown")
        expect(
            initial_ci_pending["status"] == "degraded"
            and initial_ci_pending["reason"] in {"initial_ci_unknown", "ci_probe_unknown"}
            and isinstance(
                ci_store.read_json("trusted_workspace_observer_state.json").get("baseline"),
                dict,
            )
            and ci_store.read_json("trusted_workspace_observer_state.json").get("last_observation")
            == ci_store.read_json("trusted_workspace_observer_state.json").get("baseline"),
            "CI transport failure cannot advance the configure baseline",
        )
        fake_ci.signal_state = "clear"
        expect(ci_observer.run_once(reason="ci_baseline")["events_published"] == 0, "CI baseline is silent")
        (workspace / "app.py").write_text("ci revision\n", encoding="utf-8")
        git(workspace, "add", "app.py")
        git(workspace, "commit", "-qm", "ci revision")
        fake_ci.signal_state = None
        pending = ci_observer.run_once(reason="ci_pending")
        expect(
            pending["status"] == "degraded"
            and pending["events_published"] == 0
            and pending["reason"] == "clean_revision_ci_unknown",
            "CI unknown cannot create a false candidate",
        )
        fake_ci.signal_state = "present"
        failure = ci_observer.run_once(reason="ci_failure")
        expect(failure["events_published"] == 2, "exact-SHA CI failure emits change and risk")
        fake_ci.probe_suffix = 1
        rerun = ci_observer.run_once(reason="ci_same_sha_rerun")
        expect(rerun["events_published"] == 2, "same-SHA CI rerun transition emits pair")
        expect(
            ci_observer.run_once(reason="ci_same_sha_duplicate")["events_published"] == 0,
            "same-SHA CI duplicate probe is silent",
        )

        bad_ci = build_runtime(root / "bad-ci-state", workspace)[3]
        git(workspace, "remote", "set-url", "origin", "git@evil.invalid:veyra-smoke/fixture.git")
        try:
            bad_ci.configure(
                control_token=TOKEN,
                expected_state_revision=int(bad_ci.state_store.read_json("trusted_workspace_observer_state.json").get("_state_revision") or 0),
                mode="record_only",
                user_id="smoke-owner",
                session_id="bad-ci",
                workspace_id=str(workspace),
                goal_id="goal-smoke",
                ci_binding={"api_origin": "https://evil.invalid"},
            )
        except TrustedWorkspaceObserverConflict:
            print("PASS non-canonical CI binding rejected")
        else:
            raise AssertionError("non-canonical CI binding was accepted")
        git(workspace, "remote", "set-url", "origin", "git@github.com:veyra-smoke/fixture.git")
    print("trusted workspace observer smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
