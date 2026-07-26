#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import JSONL_FILES, STATE_FILE_LAYOUT, WorldStateStore
from interface.event_normalizer import EventNormalizer
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
import runtime.event_awareness_runtime as event_awareness_runtime_module
from runtime.project_guardian_producers import (
    ProjectGuardianGoalConflict,
    ProjectGuardianProducerRuntime,
)
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from scripts.event_driven_awareness_smoke import (
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)
from scripts.project_guardian_smoke import configure_mode, enqueue_signal


NOW = datetime.now(timezone.utc).replace(microsecond=0)
REPO_ID = "wenjiesong04/veyra"
TARGET_REF = "refs/heads/main"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    error_types: type[BaseException] | tuple[type[BaseException], ...],
    label: str,
    action: Callable[[], Any],
) -> BaseException:
    try:
        action()
    except error_types as exc:
        print(f"PASS {label}")
        return exc
    except Exception as exc:
        raise AssertionError(
            f"{label}: unexpected {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"{label}: expected an exception")


def git(repo: Path, *args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0 and not allow_failure:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def create_git_fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "remote" / "wenjiesong04" / "veyra.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    init_bare = subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if init_bare.returncode != 0:
        raise AssertionError(init_bare.stderr.strip())

    repo = root / "worktree"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Veyra Smoke")
    git(repo, "config", "user.email", "veyra-smoke@example.invalid")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo.resolve(), remote.resolve()


def create_local_git_fixture(
    root: Path,
    *,
    object_format: str = "sha1",
) -> Path:
    repo = root / "worktree"
    repo.mkdir(parents=True, exist_ok=True)
    init_args = ["init", "-b", "main"]
    if object_format == "sha256":
        init_args.insert(1, "--object-format=sha256")
    git(repo, *init_args)
    git(repo, "config", "user.name", "Veyra Smoke")
    git(repo, "config", "user.email", "veyra-smoke@example.invalid")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo.resolve()


def forge_matching_stat_index(repo: Path, object_format: str) -> None:
    """Create an index whose cached stat matches content but whose OID is HEAD."""

    tracked = repo / "tracked.txt"
    head_oid = bytes.fromhex(
        git(repo, "rev-parse", "HEAD:tracked.txt")
    )
    oid_length = 20 if object_format == "sha1" else 32
    if len(head_oid) != oid_length:
        raise AssertionError("unexpected Git object id length")

    original = tracked.stat()
    tracked.write_text("changed\n", encoding="utf-8")
    changed = tracked.stat()
    cached_mtime_ns = max(
        0,
        original.st_mtime_ns - 5_000_000_000,
    )
    os.utime(
        tracked,
        ns=(changed.st_atime_ns, cached_mtime_ns),
    )
    git(repo, "add", "tracked.txt")
    git(repo, "update-index", "--index-version=2")

    index_text = git(repo, "rev-parse", "--git-path", "index")
    index_path = Path(index_text)
    if not index_path.is_absolute():
        index_path = repo / index_path
    index_path = index_path.resolve()
    raw = bytearray(index_path.read_bytes())
    if (
        raw[:4] != b"DIRC"
        or int.from_bytes(raw[4:8], "big") != 2
        or int.from_bytes(raw[8:12], "big") != 1
    ):
        raise AssertionError("unexpected deterministic fixture index")

    oid_offset = 12 + 40
    raw[oid_offset : oid_offset + oid_length] = head_oid
    digest = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
    raw[-oid_length:] = digest(raw[:-oid_length]).digest()
    index_path.write_bytes(raw)


def git_snapshot(repo: Path) -> dict[str, str]:
    git_dir_text = git(repo, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    index = git_dir.resolve() / "index"
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "ref": git(repo, "symbolic-ref", "-q", "HEAD"),
        "status": git(
            repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "index_entries": git(repo, "ls-files", "--stage"),
    }


def producer(
    store: WorldStateStore,
    publisher: Callable[..., dict[str, Any]],
) -> ProjectGuardianProducerRuntime:
    return ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=publisher,
        clock=lambda: NOW,
    )


def register_goal(
    runtime: ProjectGuardianProducerRuntime,
    repo: Path,
    *,
    user_id: str = "user-a",
    goal_id: str = "goal_release_producer",
    workspace_path: Path | None = None,
    repo_id: str = REPO_ID,
    target_ref: str = TARGET_REF,
    release_cycle: str = "release_producer_smoke",
    expected_state_revision: int | None = None,
) -> dict[str, Any]:
    return runtime.register_release_goal(
        user_id=user_id,
        workspace_id="workspace-producer-smoke",
        repo_id=repo_id,
        target_ref=target_ref,
        target_environment="production",
        release_cycle=release_cycle,
        workspace_path=str(workspace_path or repo),
        active_from=(NOW - timedelta(hours=1)).isoformat(),
        active_until=(NOW + timedelta(hours=1)).isoformat(),
        goal_id=goal_id,
        expected_state_revision=expected_state_revision,
    )


def signal_envelopes(store: WorldStateStore) -> list[dict[str, Any]]:
    inbox = store.read_json("event_inbox.json")
    events = inbox.get("events") if isinstance(inbox.get("events"), dict) else {}
    return [
        copy.deepcopy(record["envelope"])
        for record in events.values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
        and str(
            (record["envelope"].get("source") or {}).get("channel") or ""
        )
        == ProjectGuardianEvaluator.SIGNAL_CHANNEL
    ]


def frontier_signals(store: WorldStateStore) -> list[dict[str, Any]]:
    state = store.read_json(ProjectGuardianSignalLedger.STATE_FILE)
    signals = state.get("signals") if isinstance(state.get("signals"), dict) else {}
    return [
        copy.deepcopy(record["envelope"]["payload"]["project_guardian_signal"])
        for record in signals.values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
        and isinstance(record["envelope"].get("payload"), dict)
        and isinstance(
            record["envelope"]["payload"].get("project_guardian_signal"),
            dict,
        )
    ]


def test_registration_cas_scope_and_repository_binding(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    runtime = producer(store, lambda **kwargs: {"status": "unexpected"})

    symlink = root / "worktree-link"
    symlink.symlink_to(repo, target_is_directory=True)
    created = register_goal(runtime, repo, workspace_path=symlink)
    binding = store.read_json(runtime.STATE_FILE)["bindings"][created["goal_id"]]
    expect(
        created["status"] == "active"
        and created["source"] == runtime.GOAL_SOURCE
        and created["scope"]["repo_id"] == REPO_ID
        and created["scope"]["target_ref"] == TARGET_REF
        and created["target_sha"] == git(repo, "rev-parse", "HEAD")
        and binding["canonical_path"] == str(repo),
        "release Goal registration binds the canonical Git root and immutable SHA",
        {"goal": created, "binding": binding},
    )

    expect_raises(
        ValueError,
        "registration rejects a repository identity mismatch",
        lambda: register_goal(
            runtime,
            repo,
            goal_id="goal_wrong_repo",
            repo_id="someone/else",
        ),
    )
    expect_raises(
        ValueError,
        "registration rejects a branch ref mismatch",
        lambda: register_goal(
            runtime,
            repo,
            goal_id="goal_wrong_ref",
            target_ref="refs/heads/release",
        ),
    )
    nested = repo / "nested"
    nested.mkdir()
    expect_raises(
        ValueError,
        "registration rejects a nested path instead of silently widening the root",
        lambda: register_goal(
            runtime,
            repo,
            goal_id="goal_nested_root",
            workspace_path=nested,
        ),
    )

    git(repo, "switch", "--detach", "HEAD")
    try:
        expect_raises(
            (RuntimeError, ValueError),
            "registration rejects a detached HEAD",
            lambda: register_goal(
                runtime,
                repo,
                goal_id="goal_detached",
            ),
        )
    finally:
        git(repo, "switch", "main")

    expect_raises(
        ProjectGuardianGoalConflict,
        "existing release Goal requires an explicit CAS state revision",
        lambda: register_goal(runtime, repo),
    )
    expect_raises(
        ProjectGuardianGoalConflict,
        "release Goal registration rejects a stale CAS state revision",
        lambda: register_goal(
            runtime,
            repo,
            expected_state_revision=999,
        ),
    )
    updated = register_goal(
        runtime,
        repo,
        expected_state_revision=created["state_revision"],
        release_cycle="release_producer_smoke_2",
    )
    expect(
        updated["revision"] != created["revision"]
        and updated["state_revision"] == created["state_revision"] + 1
        and updated["scope"]["release_cycle"] == "release_producer_smoke_2",
        "matching CAS replaces the release Goal with a new semantic revision",
        {"created": created, "updated": updated},
    )
    expect_raises(
        ProjectGuardianGoalConflict,
        "status mutation rejects the superseded Goal state revision",
        lambda: runtime.set_release_goal_status(
            user_id="user-a",
            goal_id=updated["goal_id"],
            expected_state_revision=created["state_revision"],
            status="paused",
        ),
    )
    paused = runtime.set_release_goal_status(
        user_id="user-a",
        goal_id=updated["goal_id"],
        expected_state_revision=updated["state_revision"],
        status="paused",
    )
    expect_raises(
        ProjectGuardianGoalConflict,
        "a concurrent status mutation invalidates an older registration CAS",
        lambda: register_goal(
            runtime,
            repo,
            expected_state_revision=updated["state_revision"],
            release_cycle="release_lost_update_attempt",
        ),
    )
    resumed = runtime.set_release_goal_status(
        user_id="user-a",
        goal_id=updated["goal_id"],
        expected_state_revision=paused["state_revision"],
        status="active",
    )
    expect(
        paused["status"] == "paused"
        and paused["state_revision"] == updated["state_revision"] + 1
        and resumed["status"] == "active"
        and resumed["state_revision"] == paused["state_revision"] + 1
        and runtime.list_release_goals(user_id="user-a") == [resumed]
        and runtime.list_release_goals(user_id="other-user") == [],
        "release Goal status and inspection remain CAS-bound and user-scoped",
        {"paused": paused, "resumed": resumed},
    )


def test_repository_rebind_invalidates_old_signal_revision(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    alternate_remote = (
        root / "alternate" / "wenjiesong04" / "veyra.git"
    )
    alternate_remote.parent.mkdir(parents=True, exist_ok=True)
    initialized = subprocess.run(
        ["git", "init", "--bare", str(alternate_remote)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if initialized.returncode != 0:
        raise AssertionError(initialized.stderr.strip())

    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    original = register_goal(runtime, repo)
    (repo / "tracked.txt").write_text(
        "initial\nold-binding-risk\n",
        encoding="utf-8",
    )
    git_run = runtime.run_once(reason="old_binding_git")
    enqueue_signal(
        store,
        kind="ci_failed",
        event_id="evt_old_binding_ci",
        goal_id=original["goal_id"],
        goal_revision=original["revision"],
        scope=original["scope"],
        occurred_at=NOW,
    )
    before = ProjectGuardianEvaluator().evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(
            store
        ).evaluation_state(),
        now=NOW,
    )

    git(repo, "remote", "set-url", "origin", str(alternate_remote))
    rebound = register_goal(
        runtime,
        repo,
        expected_state_revision=original["state_revision"],
    )
    after = ProjectGuardianEvaluator().evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(
            store
        ).evaluation_state(),
        now=NOW,
    )
    expect(
        git_run["status"] == "success"
        and before["candidate_count"] == 1
        and rebound["revision"] != original["revision"]
        and rebound["state_revision"] == original["state_revision"] + 1
        and after["candidate_count"] == 0,
        (
            "changing the exact repository binding creates a new semantic "
            "Goal revision and invalidates old frontier evidence"
        ),
        {
            "original_revision": original["revision"],
            "rebound_revision": rebound["revision"],
            "before": before,
            "after": after,
        },
    )


def test_disabled_skips_probe_and_publish(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    publish_calls: list[dict[str, Any]] = []
    runtime = producer(
        store,
        lambda **kwargs: publish_calls.append(kwargs) or {"status": "unexpected"},
    )
    register_goal(runtime, repo)
    producer_state_before = store.read_json(runtime.STATE_FILE)
    probe_calls = 0

    def forbidden_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal probe_calls
        del args, kwargs
        probe_calls += 1
        raise AssertionError("disabled producer attempted a Git probe")

    runtime._inspect_repository = forbidden_probe  # type: ignore[method-assign]
    result = runtime.run_once(reason="disabled_contract")
    expect(
        result["status"] == "disabled"
        and result["observed_count"] == 0
        and result["published_count"] == 0
        and probe_calls == 0
        and publish_calls == []
        and store.read_json(runtime.STATE_FILE) == producer_state_before
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        "disabled mode performs no Git probe, publish, or telemetry mutation",
        result,
    )


def test_record_only_trusted_dirty_and_clean_ingress(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    expect_raises(
        RuntimeError,
        "Git producer capability can be issued only once per runtime",
        fabric.issue_project_guardian_git_publisher,
    )
    registered = register_goal(runtime, repo)
    binding = store.read_json(runtime.STATE_FILE)["bindings"][
        registered["goal_id"]
    ]
    forged_fact = fabric._publish_project_guardian_git_observation(
        ingress_capability=object(),
        user_id=registered["user_id"],
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        repository_fact={
            "repo_id": registered["scope"]["repo_id"],
            "target_ref": registered["scope"]["target_ref"],
            "target_sha": registered["target_sha"],
            "remote_origin_digest": binding["remote_origin_digest"],
            "dirty": False,
            "probe_digest": "0" * 64,
        },
    )
    expect(
        forged_fact.get("status") == "ignored"
        and forged_fact.get("reason")
        == "untrusted_project_guardian_git_ingress"
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        "dedicated ingress rejects callers without its producer capability",
        forged_fact,
    )

    tracked = repo / "tracked.txt"
    tracked.write_text("initial\ndirty\n", encoding="utf-8")
    index_before_probe = hashlib.sha256(
        (repo / ".git" / "index").read_bytes()
    ).hexdigest()
    dirty_run = runtime.run_once(reason="trusted_dirty")
    index_after_probe = hashlib.sha256(
        (repo / ".git" / "index").read_bytes()
    ).hexdigest()
    dirty_frontier = frontier_signals(store)
    public_run = runtime._public_run_result(dirty_run)
    public_status = runtime.status()
    expect(
        dirty_run["status"] == "success"
        and dirty_run["published_count"] == 1
        and dirty_run["observations"][0]["signal_state"] == "present"
        and len(dirty_frontier) == 1
        and dirty_frontier[0]["state"] == "present"
        and dirty_frontier[0]["goal_id"] == registered["goal_id"]
        and dirty_frontier[0]["goal_revision"] == registered["revision"]
        and dirty_frontier[0]["producer_attestation"]["producer_id"]
        == ProjectGuardianEvaluator.SIGNAL_PRODUCERS["git_dirty"][
            "producer_id"
        ]
        and str(
            dirty_frontier[0]["producer_attestation"]["receipt_id"]
        ).startswith("pgsr_")
        and index_after_probe == index_before_probe,
        "record-only producer admits one bound factual dirty observation",
        {
            "run": dirty_run,
            "frontier": dirty_frontier,
            "index_before": index_before_probe,
            "index_after": index_after_probe,
        },
    )
    expect(
        "observations" not in public_run
        and isinstance(public_status.get("last_run"), dict)
        and "observations" not in public_status["last_run"]
        and public_run.get("observed_count") == 1,
        "tenant-neutral producer status exposes aggregate telemetry only",
        {"run": public_run, "status": public_status},
    )

    tracked.write_text("initial\n", encoding="utf-8")
    clean_run = runtime.run_once(reason="trusted_clean")
    clean_frontier = frontier_signals(store)
    expect(
        clean_run["status"] == "success"
        and clean_run["published_count"] == 1
        and clean_run["observations"][0]["signal_state"] == "clear"
        and len(clean_frontier) == 1
        and clean_frontier[0]["state"] == "clear"
        and clean_frontier[0]["goal_id"] == registered["goal_id"]
        and len(signal_envelopes(store)) == 2,
        "record-only producer records a trusted clean tombstone without projecting",
        {"run": clean_run, "frontier": clean_frontier},
    )
    expect(
        store.read_json("project_guardian_state.json").get("candidates") == []
        and store.read_json("situation_state.json").get("situations") == [],
        "record-only Git observations create neither candidates nor Situations",
    )


def test_read_only_probe_preserves_git_index(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    tracked = repo / "tracked.txt"
    stat = tracked.stat()
    os.utime(
        tracked,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
    )
    index = repo / ".git" / "index"
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    result = runtime.run_once(reason="index_read_only")
    after = hashlib.sha256(index.read_bytes()).hexdigest()
    expect(
        result["status"] == "success"
        and result["observations"][0]["signal_state"] == "clear"
        and before == after,
        "Git probe does not refresh the index in a clean stat-only change",
        {"run": result, "index_before": before, "index_after": after},
    )


def test_read_only_probe_disables_repository_fsmonitor(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "fsmonitor-was-executed"
    hook = root / "malicious-fsmonitor.sh"
    hook.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf '\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    git(repo, "config", "core.fsmonitor", str(hook))
    result = runtime.run_once(reason="fsmonitor_disabled")
    expect(
        result["status"] == "success"
        and result["observations"][0]["signal_state"] == "clear"
        and not marker.exists(),
        (
            "read-only Git probe overrides repository fsmonitor hooks and "
            "does not execute repository-configured code"
        ),
        {"run": result, "marker_exists": marker.exists()},
    )


def test_repository_filters_are_rejected_before_status(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "clean-filter-was-executed"
    clean_filter = root / "evil-clean-filter.sh"
    clean_filter.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf 'initial\\n'\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    git(repo, "config", "filter.evil.clean", str(clean_filter))
    git(repo, "config", "filter.evil.required", "true")
    (repo / ".git" / "info" / "attributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("evilxxx\n", encoding="utf-8")
    result = runtime.run_once(reason="repository_filter_rejected")
    expect(
        result["status"] == "degraded"
        and result["published_count"] == 0
        and result["observations"][0]["reason"] == "git_probe_failed"
        and not marker.exists()
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "repository clean/process filters are rejected before Git status "
            "can execute repository-configured code or hide a change"
        ),
        {"run": result, "marker_exists": marker.exists()},
    )


def test_included_repository_filters_are_rejected_before_status(
    root: Path,
) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "included-clean-filter-was-executed"
    clean_filter = root / "included-evil-clean-filter.sh"
    clean_filter.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf 'initial\\n'\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    included_config = root / "included-git-config"
    included_config.write_text(
        "[filter \"evil\"]\n"
        f"\tclean = {clean_filter}\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    git(repo, "config", "--local", "--add", "include.path", str(included_config))
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("evilxxx\n", encoding="utf-8")
    result = runtime.run_once(reason="included_repository_filter_rejected")
    expect(
        result["status"] == "degraded"
        and result["published_count"] == 0
        and result["observations"][0]["reason"] == "git_probe_failed"
        and not marker.exists()
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "included repository filters are resolved and rejected before "
            "Git status can execute them"
        ),
        {"run": result, "marker_exists": marker.exists()},
    )


def test_worktree_repository_filters_are_rejected_before_status(
    root: Path,
) -> None:
    repo, _ = create_git_fixture(root / "git")
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "tracked attributes")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "worktree-clean-filter-was-executed"
    clean_filter = root / "worktree-evil-clean-filter.sh"
    clean_filter.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf 'initial\\n'\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "filter.evil.clean", str(clean_filter))
    git(repo, "config", "--worktree", "filter.evil.required", "true")
    (repo / "tracked.txt").write_text("evilxxx\n", encoding="utf-8")
    result = runtime.run_once(reason="worktree_repository_filter_rejected")
    expect(
        result["status"] == "degraded"
        and result["published_count"] == 0
        and result["observations"][0]["reason"] == "git_probe_failed"
        and not marker.exists()
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "worktree-scoped repository filters are rejected before Git "
            "status can execute them"
        ),
        {"run": result, "marker_exists": marker.exists()},
    )


def test_filter_config_race_cannot_emit_clear(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "tracked filter attributes")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "racing-clean-filter-was-executed"
    clean_filter = root / "racing-evil-clean-filter.sh"
    clean_filter.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf 'initial\\n'\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    (repo / "tracked.txt").write_text("evilxxx\n", encoding="utf-8")

    original_git_output = runtime._git_output
    injected = False

    def inject_filter_after_initial_check(
        selected_root: Path,
        args: list[str],
        *,
        allow_empty: bool = False,
        allow_missing: bool = False,
    ) -> str:
        nonlocal injected
        output = original_git_output(
            selected_root,
            args,
            allow_empty=allow_empty,
            allow_missing=allow_missing,
        )
        if (
            not injected
            and args
            and args[0] == "config"
            and "--get-regexp" in args
        ):
            injected = True
            git(repo, "config", "filter.evil.clean", str(clean_filter))
            git(repo, "config", "filter.evil.required", "true")
        return output

    runtime._git_output = (  # type: ignore[method-assign]
        inject_filter_after_initial_check
    )
    try:
        result = runtime.run_once(reason="filter_config_race")
    finally:
        runtime._git_output = original_git_output  # type: ignore[method-assign]
    expect(
        injected
        and result["status"] == "degraded"
        and result["published_count"] == 0
        and result["observations"][0]["reason"] == "git_probe_failed"
        and not marker.exists()
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "filter config introduced after the initial check is detected "
            "before status and cannot execute or emit clear"
        ),
        {
            "injected": injected,
            "run": result,
            "marker_exists": marker.exists(),
        },
    )


def test_isolated_status_never_executes_repository_filters(
    root: Path,
) -> None:
    repo, _ = create_git_fixture(root / "git")
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "tracked filter attributes")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    marker = root / "isolated-clean-filter-was-executed"
    clean_filter = root / "isolated-evil-clean-filter.sh"
    clean_filter.write_text(
        "#!/bin/sh\n"
        f": > '{marker}'\n"
        "printf 'initial\\n'\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    git(repo, "config", "filter.evil.clean", str(clean_filter))
    git(repo, "config", "filter.evil.required", "true")
    (repo / "tracked.txt").write_text("evilxxx\n", encoding="utf-8")

    original_filter_guard = runtime._require_no_repository_filters
    runtime._require_no_repository_filters = (  # type: ignore[method-assign]
        lambda _: None
    )
    try:
        result = runtime.run_once(reason="isolated_filter_status")
    finally:
        runtime._require_no_repository_filters = (  # type: ignore[method-assign]
            original_filter_guard
        )
    expect(
        result["status"] == "success"
        and result["published_count"] == 1
        and result["observations"][0]["signal_state"] == "present"
        and not marker.exists(),
        (
            "isolated status ignores repository filter configuration even "
            "if the adjacent drift guard misses it"
        ),
        {"run": result, "marker_exists": marker.exists()},
    )


def test_isolated_status_rehashes_racy_clean_content(root: Path) -> None:
    runtime = producer(
        WorldStateStore(root / "state"),
        lambda **_: {"status": "unused"},
    )
    failures: dict[str, Any] = {}
    for object_format in ("sha1", "sha256"):
        repo = create_local_git_fixture(
            root / object_format,
            object_format=object_format,
        )
        forge_matching_stat_index(repo, object_format)
        copied_index_status = git(
            repo,
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        )
        isolated_status = runtime._isolated_status_output(repo)
        if copied_index_status or not isolated_status.strip():
            failures[object_format] = {
                "copied_index_status": copied_index_status,
                "isolated_status": isolated_status,
            }

    expect(
        not failures,
        (
            "fresh isolated indexes rehash deterministic stale-stat content "
            "for SHA-1 and SHA-256 repositories"
        ),
        failures,
    )


def test_isolated_status_supports_linked_split_indexes(root: Path) -> None:
    runtime = producer(
        WorldStateStore(root / "state"),
        lambda **_: {"status": "unused"},
    )
    failures: dict[str, Any] = {}
    for object_format in ("sha1", "sha256"):
        repo = create_local_git_fixture(
            root / object_format,
            object_format=object_format,
        )
        linked = root / object_format / "linked"
        git(
            repo,
            "worktree",
            "add",
            "-b",
            f"linked-{object_format}",
            str(linked),
        )
        linked = linked.resolve()
        git(linked, "update-index", "--split-index")
        source_index = runtime._resolved_git_path(linked, "index")
        shared_text = git(
            linked,
            "rev-parse",
            "--shared-index-path",
        )
        shared_index = runtime._resolved_path(linked, shared_text)

        before_clean = {
            "index": hashlib.sha256(source_index.read_bytes()).hexdigest(),
            "shared": hashlib.sha256(shared_index.read_bytes()).hexdigest(),
        }
        clean_status = runtime._isolated_status_output(linked)
        after_clean = {
            "index": hashlib.sha256(source_index.read_bytes()).hexdigest(),
            "shared": hashlib.sha256(shared_index.read_bytes()).hexdigest(),
        }

        git(linked, "update-index", "--chmod=+x", "tracked.txt")
        before_staged = {
            "index": hashlib.sha256(source_index.read_bytes()).hexdigest(),
            "shared": hashlib.sha256(shared_index.read_bytes()).hexdigest(),
        }
        staged_status = runtime._isolated_status_output(linked)
        after_staged = {
            "index": hashlib.sha256(source_index.read_bytes()).hexdigest(),
            "shared": hashlib.sha256(shared_index.read_bytes()).hexdigest(),
        }
        if (
            clean_status
            or before_clean != after_clean
            or staged_status != "index:dirty"
            or before_staged != after_staged
        ):
            failures[object_format] = {
                "clean_status": clean_status,
                "clean_index_unchanged": before_clean == after_clean,
                "staged_status": staged_status,
                "staged_index_unchanged": before_staged == after_staged,
            }

    expect(
        not failures,
        (
            "isolated status preserves SHA-1 and SHA-256 linked split "
            "indexes while detecting index-only changes"
        ),
        failures,
    )


def test_git_config_cannot_hide_tracked_changes(root: Path) -> None:
    stat_repo, _ = create_git_fixture(root / "stat" / "git")
    stat_store = WorldStateStore(root / "stat" / "state")
    configure_mode(stat_store, "record_only")
    stat_fabric = ShadowAwarenessRuntime(stat_store, mode="record_only")
    stat_runtime = producer(
        stat_store,
        stat_fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(stat_runtime, stat_repo)
    stat_tracked = stat_repo / "tracked.txt"
    original_stat = stat_tracked.stat()
    git(stat_repo, "config", "core.trustctime", "false")
    git(stat_repo, "config", "core.checkStat", "minimal")
    stat_tracked.write_text("changed\n", encoding="utf-8")
    os.utime(
        stat_tracked,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    stat_result = stat_runtime.run_once(reason="stat_config_override")
    hidden_stat_status = git(
        stat_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )

    mode_repo, _ = create_git_fixture(root / "mode" / "git")
    mode_store = WorldStateStore(root / "mode" / "state")
    configure_mode(mode_store, "record_only")
    mode_fabric = ShadowAwarenessRuntime(mode_store, mode="record_only")
    mode_runtime = producer(
        mode_store,
        mode_fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(mode_runtime, mode_repo)
    mode_tracked = mode_repo / "tracked.txt"
    git(mode_repo, "config", "core.fileMode", "false")
    mode_tracked.chmod(0o755)
    mode_result = mode_runtime.run_once(reason="filemode_config_override")
    hidden_mode_status = git(
        mode_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )

    expect(
        stat_tracked.read_text(encoding="utf-8") == "changed\n"
        and stat_result["status"] == "success"
        and stat_result["observations"][0]["signal_state"] == "present"
        and bool(mode_tracked.stat().st_mode & 0o111)
        and mode_result["status"] == "success"
        and mode_result["observations"][0]["signal_state"] == "present",
        (
            "Git probe overrides trustctime, checkStat, and fileMode so "
            "repository-local config cannot hide tracked changes"
        ),
        {
            "stat_run": stat_result,
            "repo_stat_status": hidden_stat_status,
            "mode_run": mode_result,
            "repo_mode_status": hidden_mode_status,
        },
    )


def test_git_symlink_config_cannot_hide_type_changes(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    tracked = repo / "tracked.txt"
    tracked.unlink()
    (repo / "target.txt").write_text("payload\n", encoding="utf-8")
    os.symlink("target.txt", tracked)
    git(repo, "add", "tracked.txt", "target.txt")
    git(repo, "commit", "-m", "track symbolic link")

    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    git(repo, "config", "core.symlinks", "false")
    tracked.unlink()
    tracked.write_text("target.txt", encoding="utf-8")
    hidden_status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    honest_status = git(
        repo,
        "-c",
        "core.symlinks=true",
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    result = runtime.run_once(reason="symlink_config_override")
    expect(
        hidden_status == ""
        and honest_status
        and result["status"] == "success"
        and result["observations"][0]["signal_state"] == "present",
        (
            "Git probe forces symlink semantics so repository config cannot "
            "hide a tracked type change"
        ),
        {
            "hidden_status": hidden_status,
            "honest_status": honest_status,
            "run": result,
        },
    )


def test_git_ignorecase_config_cannot_hide_case_renames(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    (repo / "tracked.txt").rename(repo / "Tracked.txt")
    git(repo, "config", "core.ignoreCase", "true")
    hidden_status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    honest_status = git(
        repo,
        "-c",
        "core.ignoreCase=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    isolated_status_outputs: list[str] = []
    original_isolated_status = runtime._isolated_status_output

    def capture_isolated_status(worktree: Path) -> str:
        output = original_isolated_status(worktree)
        isolated_status_outputs.append(output)
        return output

    runtime._isolated_status_output = (  # type: ignore[method-assign]
        capture_isolated_status
    )
    try:
        result = runtime.run_once(reason="ignorecase_config_override")
    finally:
        runtime._isolated_status_output = (  # type: ignore[method-assign]
            original_isolated_status
        )
    hidden_lines = {
        line for line in hidden_status.splitlines() if line
    }
    honest_lines = {
        line for line in honest_status.splitlines() if line
    }
    isolated_line_sets = [
        {line for line in output.splitlines() if line}
        for output in isolated_status_outputs
    ]
    expect(
        hidden_lines < honest_lines
        and "?? Tracked.txt" in honest_lines
        and "?? Tracked.txt" not in hidden_lines
        and len(isolated_line_sets) == 2
        and all(
            "?? Tracked.txt" in lines
            for lines in isolated_line_sets
        )
        and result["status"] == "success"
        and result["observations"][0]["signal_state"] == "present",
        (
            "Git probe forces case-sensitive path comparison so repository "
            "config cannot hide a tracked case-only rename"
        ),
        {
            "hidden_status": hidden_status,
            "honest_status": honest_status,
            "isolated_status_outputs": isolated_status_outputs,
            "run": result,
        },
    )


def test_git_environment_cannot_redirect_index(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)

    alternate_index = root / "alternate-clean-index"
    alternate_env = os.environ.copy()
    alternate_env["GIT_INDEX_FILE"] = str(alternate_index)
    read_tree = subprocess.run(
        ["git", "-C", str(repo), "read-tree", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=alternate_env,
    )
    if read_tree.returncode != 0:
        raise AssertionError(read_tree.stderr.strip())

    tracked = repo / "tracked.txt"
    tracked.write_text("staged!\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    tracked.write_text("initial\n", encoding="utf-8")
    real_status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    previous_index = os.environ.get("GIT_INDEX_FILE")
    injected_config = root / "host-injected-git-config"
    injected_config.write_text(
        "[remote \"origin\"]\n"
        "\turl = https://example.invalid/attacker/redirected.git\n",
        encoding="utf-8",
    )
    previous_config = os.environ.get("GIT_CONFIG")
    os.environ["GIT_INDEX_FILE"] = str(alternate_index)
    os.environ["GIT_CONFIG"] = str(injected_config)
    try:
        result = runtime.run_once(reason="host_git_env_override")
    finally:
        if previous_index is None:
            os.environ.pop("GIT_INDEX_FILE", None)
        else:
            os.environ["GIT_INDEX_FILE"] = previous_index
        if previous_config is None:
            os.environ.pop("GIT_CONFIG", None)
        else:
            os.environ["GIT_CONFIG"] = previous_config
    expect(
        real_status
        and result["status"] == "success"
        and result["observations"][0]["signal_state"] == "present",
        (
            "producer clears host Git path/config injection variables and "
            "cannot be redirected to an alternate clean index or config"
        ),
        {"real_status": real_status, "run": result},
    )


def test_replace_refs_cannot_hide_tracked_changes(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "replacement tree")
    replacement = git(repo, "rev-parse", "HEAD")
    git(repo, "replace", base, replacement)
    git(repo, "reset", "--hard", base)
    native_status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    no_replace_env = os.environ.copy()
    no_replace_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    honest_status = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=no_replace_env,
    )
    if honest_status.returncode != 0:
        raise AssertionError(honest_status.stderr.strip())

    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)
    result = runtime.run_once(reason="replace_refs_disabled")
    expect(
        native_status == ""
        and honest_status.stdout.strip()
        and result["status"] == "success"
        and result["observations"][0]["signal_state"] == "present",
        (
            "repository-local replace refs cannot hide a tracked tree change "
            "from the producer"
        ),
        {
            "native_status": native_status,
            "honest_status": honest_status.stdout.strip(),
            "run": result,
        },
    )


def test_multiple_origin_urls_are_rejected(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    git(
        repo,
        "config",
        "--add",
        "remote.origin.url",
        "https://example.invalid/other/repository.git",
    )
    store = WorldStateStore(root / "state")
    runtime = producer(store, lambda **kwargs: {"status": "unexpected"})
    error = expect_raises(
        ValueError,
        "ambiguous multiple origin fetch URLs are rejected",
        lambda: register_goal(runtime, repo),
    )
    expect(
        "exactly one" in str(error),
        "Goal repository binding cannot select a different URL than fetch",
        str(error),
    )


def test_gitlink_without_gitmodules_is_rejected(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    head = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{head},nested",
    )
    git(repo, "commit", "-m", "gitlink without gitmodules")
    raw_status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--ignore-submodules=all",
    )
    stage = git(repo, "ls-files", "--stage")
    store = WorldStateStore(root / "state")
    runtime = producer(store, lambda **kwargs: {"status": "unexpected"})
    error = expect_raises(
        RuntimeError,
        "a gitlink index entry is rejected even without .gitmodules",
        lambda: register_goal(runtime, repo),
    )
    expect(
        not (repo / ".gitmodules").exists()
        and raw_status == ""
        and any(line.startswith("160000 ") for line in stage.splitlines())
        and "Gitlink index entries" in str(error),
        "submodule support cannot be smuggled through an index-only gitlink",
        {"status": raw_status, "stage": stage, "error": str(error)},
    )


def test_hidden_index_flags_never_emit_clear(root: Path) -> None:
    failures: dict[str, Any] = {}
    for name, flag in (
        ("assume-unchanged", "--assume-unchanged"),
        ("skip-worktree", "--skip-worktree"),
    ):
        repo, _ = create_git_fixture(root / name / "git")
        store = WorldStateStore(root / name / "state")
        configure_mode(store, "record_only")
        fabric = ShadowAwarenessRuntime(store, mode="record_only")
        runtime = producer(
            store,
            fabric.issue_project_guardian_git_publisher(),
        )
        register_goal(runtime, repo)
        tracked = repo / "tracked.txt"
        tracked.write_text("initial\nvisible-risk\n", encoding="utf-8")
        seed = runtime.run_once(reason=f"{name}_seed")
        frontier_before = copy.deepcopy(frontier_signals(store))
        inbox_before = len(signal_envelopes(store))
        tracked.write_text("initial\n", encoding="utf-8")
        git(repo, "update-index", flag, "tracked.txt")
        tracked.write_text(
            f"initial\nhidden-by-{name}\n",
            encoding="utf-8",
        )
        hidden_status = git(
            repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        )
        result = runtime.run_once(reason=f"{name}_hidden_change")
        flags = git(repo, "ls-files", "-v")
        if not (
            seed["status"] == "success"
            and len(frontier_before) == 1
            and frontier_before[0]["state"] == "present"
            and hidden_status == ""
            and result["status"] == "degraded"
            and result["published_count"] == 0
            and result["observations"][0]["reason"] == "git_probe_failed"
            and frontier_signals(store) == frontier_before
            and len(signal_envelopes(store)) == inbox_before
        ):
            failures[name] = {
                "seed": seed,
                "result": result,
                "status": hidden_status,
                "flags": flags,
                "frontier_before": frontier_before,
                "frontier_after": frontier_signals(store),
            }
    expect(
        not failures,
        (
            "assume-unchanged and skip-worktree changes are unsupported "
            "unknowns and can never emit a clean tombstone"
        ),
        failures,
    )


def test_signal_ledger_failure_degrades_publish_result(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)
    (repo / "tracked.txt").write_text(
        "initial\nledger-failure\n",
        encoding="utf-8",
    )
    frontier_before = copy.deepcopy(frontier_signals(store))

    def fail_ledger(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("injected signal ledger failure")

    fabric.project_guardian_signals.record_envelope = fail_ledger  # type: ignore[method-assign]
    result = runtime.run_once(reason="ledger_failure")
    observation = result["observations"][0]
    expect(
        result["status"] == "degraded"
        and result["published_count"] == 0
        and result["failed_count"] == 1
        and observation["status"] == "degraded"
        and observation["reason"] == "signal_ledger_not_committed"
        and observation["ingress_status"] == "enqueued"
        and observation["signal_ledger_status"] == "degraded"
        and frontier_signals(store) == frontier_before,
        (
            "an inbox admission is not reported as published until the "
            "Guardian signal ledger commits it"
        ),
        {"run": result, "frontier": frontier_signals(store)},
    )


def test_observation_age_is_rechecked_at_commit(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    publisher = fabric.issue_project_guardian_git_publisher()
    runtime = producer(store, publisher)
    registered = register_goal(runtime, repo)
    binding = store.read_json(runtime.STATE_FILE)["bindings"][
        registered["goal_id"]
    ]
    repository = runtime._inspect_repository(
        str(repo),
        expected_repo_id=registered["scope"]["repo_id"],
        expected_ref=registered["scope"]["target_ref"],
        expected_sha=registered["target_sha"],
        expected_remote_origin_digest=binding["remote_origin_digest"],
    )
    fact = {
        key: repository[key]
        for key in (
            "repo_id",
            "target_ref",
            "target_sha",
            "remote_origin_digest",
            "dirty",
            "observed_at",
            "index_flags_digest",
            "index_stage_digest",
            "probe_digest",
        )
    }

    real_datetime = event_awareness_runtime_module.datetime

    class AdvancingDateTime:
        calls = 0

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            cls.calls += 1
            value = NOW if cls.calls == 1 else NOW + timedelta(minutes=3)
            return value if tz is None else value.astimezone(tz)

    event_awareness_runtime_module.datetime = AdvancingDateTime  # type: ignore[assignment]
    try:
        result = publisher(
            user_id=registered["user_id"],
            goal_id=registered["goal_id"],
            goal_revision=registered["revision"],
            repository_fact=fact,
        )
    finally:
        event_awareness_runtime_module.datetime = real_datetime
    expect(
        AdvancingDateTime.calls == 2
        and result
        == {
            "status": "ignored",
            "reason": "repository_fact_observation_time_invalid",
        }
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "producer observation age is checked again at the ledger commit "
            "boundary after acquiring the state writer"
        ),
        {"result": result, "clock_calls": AdvancingDateTime.calls},
    )


def test_probe_tail_window_is_point_in_time(root: Path) -> None:
    snapshot_time = NOW - timedelta(seconds=30)

    file_repo, _ = create_git_fixture(root / "file" / "git")
    file_store = WorldStateStore(root / "file" / "state")
    configure_mode(file_store, "record_only")
    file_fabric = ShadowAwarenessRuntime(file_store, mode="record_only")
    file_runtime = producer(
        file_store,
        file_fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(file_runtime, file_repo)
    file_runtime._clock = lambda: snapshot_time
    file_original_status = file_runtime._isolated_status_output
    status_calls = 0

    def mutate_after_final_status(
        git_root: Path,
    ) -> str:
        nonlocal status_calls
        output = file_original_status(git_root)
        status_calls += 1
        if status_calls == 2:
            (file_repo / "tracked.txt").write_text(
                "initial\nafter-final-status\n",
                encoding="utf-8",
            )
        return output

    file_runtime._isolated_status_output = (  # type: ignore[method-assign]
        mutate_after_final_status
    )
    try:
        file_result = file_runtime.run_once(reason="file_tail_window")
    finally:
        file_runtime._isolated_status_output = (  # type: ignore[method-assign]
            file_original_status
        )
    file_events = signal_envelopes(file_store)

    head_repo, _ = create_git_fixture(root / "head" / "git")
    head_store = WorldStateStore(root / "head" / "state")
    configure_mode(head_store, "record_only")
    head_fabric = ShadowAwarenessRuntime(head_store, mode="record_only")
    head_runtime = producer(
        head_store,
        head_fabric.issue_project_guardian_git_publisher(),
    )
    registered = register_goal(head_runtime, head_repo)
    head_runtime._clock = lambda: snapshot_time
    head_original = head_runtime._git_output
    head_reads = 0

    def commit_after_final_head_read(
        git_root: Path,
        args: list[str],
        *,
        allow_empty: bool = False,
        allow_missing: bool = False,
    ) -> str:
        nonlocal head_reads
        output = head_original(
            git_root,
            args,
            allow_empty=allow_empty,
            allow_missing=allow_missing,
        )
        if args == ["rev-parse", "HEAD"]:
            head_reads += 1
            if head_reads == 5:
                git(
                    head_repo,
                    "commit",
                    "--allow-empty",
                    "-m",
                    "after-final-head-read",
                )
        return output

    head_runtime._git_output = commit_after_final_head_read  # type: ignore[method-assign]
    try:
        head_result = head_runtime.run_once(reason="head_tail_window")
    finally:
        head_runtime._git_output = head_original  # type: ignore[method-assign]
    head_events = signal_envelopes(head_store)

    expect(
        status_calls == 2
        and file_result["status"] == "success"
        and file_result["observations"][0]["signal_state"] == "clear"
        and len(file_events) == 1
        and file_events[0]["occurred_at"] == snapshot_time.isoformat()
        and bool(git(file_repo, "status", "--porcelain=v1"))
        and head_reads == 5
        and head_result["status"] == "success"
        and head_result["observations"][0]["signal_state"] == "clear"
        and len(head_events) == 1
        and head_events[0]["occurred_at"] == snapshot_time.isoformat()
        and git(head_repo, "rev-parse", "HEAD")
        != registered["target_sha"],
        (
            "changes after the final read retain the probe's pre-change "
            "point-in-time timestamp instead of being relabeled as current"
        ),
        {
            "file_run": file_result,
            "file_status": git(file_repo, "status", "--porcelain=v1"),
            "head_run": head_result,
            "head_now": git(head_repo, "rev-parse", "HEAD"),
            "snapshot_time": snapshot_time.isoformat(),
        },
    )


def test_git_failures_never_clear_a_positive_frontier(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(runtime, repo)
    tracked = repo / "tracked.txt"
    tracked.write_text("initial\nrisk\n", encoding="utf-8")
    present = runtime.run_once(reason="seed_present")
    frontier_before = copy.deepcopy(frontier_signals(store))
    inbox_count_before = len(signal_envelopes(store))
    expect(
        present["status"] == "success"
        and len(frontier_before) == 1
        and frontier_before[0]["state"] == "present",
        "failure test starts from a durable positive Git frontier",
        {"run": present, "frontier": frontier_before},
    )

    original_origin = git(repo, "config", "--get", "remote.origin.url")
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://evil.invalid/wenjiesong04/veyra.git",
    )
    try:
        origin_drift = runtime.run_once(reason="origin_drift")
    finally:
        git(repo, "remote", "set-url", "origin", original_origin)
    expect(
        origin_drift["status"] == "degraded"
        and origin_drift["published_count"] == 0
        and origin_drift["observations"][0]["reason"] == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        "origin drift fails closed even when owner/repository text is unchanged",
        origin_drift,
    )

    gitmodules = repo / ".gitmodules"
    gitmodules.write_text(
        "[submodule \"unsupported\"]\n"
        "\tpath = unsupported\n"
        "\turl = https://example.invalid/unsupported.git\n",
        encoding="utf-8",
    )
    try:
        submodule_rejected = runtime.run_once(
            reason="submodule_boundary"
        )
    finally:
        gitmodules.unlink()
    expect(
        submodule_rejected["status"] == "degraded"
        and submodule_rejected["published_count"] == 0
        and submodule_rejected["observations"][0]["reason"]
        == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        (
            "unsupported submodule worktrees fail closed and cannot emit "
            "a clear tombstone"
        ),
        submodule_rejected,
    )

    git_dir = repo / ".git"
    hidden_git_dir = repo / ".git-smoke-hidden"
    git_dir.rename(hidden_git_dir)
    try:
        git_error = runtime.run_once(reason="real_git_error")
    finally:
        hidden_git_dir.rename(git_dir)
    expect(
        git_error["status"] == "degraded"
        and git_error["published_count"] == 0
        and git_error["observations"][0]["reason"] == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        "a real Git probe error fails closed and cannot emit a clear tombstone",
        git_error,
    )

    tracked.write_text("initial\n", encoding="utf-8")
    original_status_output = runtime._isolated_status_output
    worktree_race_injected = False

    def racing_worktree_output(
        git_root: Path,
    ) -> str:
        nonlocal worktree_race_injected
        output = original_status_output(git_root)
        if not worktree_race_injected:
            worktree_race_injected = True
            tracked.write_text(
                "initial\nworktree-race\n",
                encoding="utf-8",
            )
        return output

    runtime._isolated_status_output = (  # type: ignore[method-assign]
        racing_worktree_output
    )
    try:
        worktree_race = runtime.run_once(reason="worktree_race")
    finally:
        runtime._isolated_status_output = (  # type: ignore[method-assign]
            original_status_output
        )
    expect(
        worktree_race_injected
        and worktree_race["status"] == "degraded"
        and worktree_race["published_count"] == 0
        and worktree_race["observations"][0]["reason"]
        == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        "a tracked-file change racing status fails closed and cannot emit clear",
        worktree_race,
    )

    tracked.write_text("initial\n", encoding="utf-8")
    race_injected = False

    def racing_git_output(
        git_root: Path,
    ) -> str:
        nonlocal race_injected
        if not race_injected:
            race_injected = True
            git(repo, "commit", "--allow-empty", "-m", "status race")
        return original_status_output(git_root)

    runtime._isolated_status_output = (  # type: ignore[method-assign]
        racing_git_output
    )
    try:
        race = runtime.run_once(reason="head_race")
    finally:
        runtime._isolated_status_output = (  # type: ignore[method-assign]
            original_status_output
        )
    expect(
        race_injected
        and race["status"] == "degraded"
        and race["published_count"] == 0
        and race["observations"][0]["reason"] == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        "a HEAD change racing status fails closed and cannot emit clear",
        race,
    )

    git(repo, "commit", "--allow-empty", "-m", "head drift")
    drift = runtime.run_once(reason="head_drift")
    expect(
        drift["status"] == "degraded"
        and drift["published_count"] == 0
        and drift["observations"][0]["reason"] == "git_probe_failed"
        and frontier_signals(store) == frontier_before
        and len(signal_envelopes(store)) == inbox_count_before,
        "HEAD drift fails closed and cannot turn the prior positive into clear",
        drift,
    )


def test_reserved_ingress_and_receipt_validation(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(
        store,
        fabric.issue_project_guardian_git_publisher(),
    )
    registered = register_goal(runtime, repo)
    scope = copy.deepcopy(registered["scope"])

    generic_event = enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_generic_reserved",
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        scope=scope,
        occurred_at=NOW,
        enqueue=False,
    )
    generic_result = fabric.publish(generic_event)
    malformed_reserved_event = copy.deepcopy(generic_event)
    malformed_reserved_event.event_id = "evt_generic_reserved_malformed"
    malformed_reserved_event.correlation_id = (
        malformed_reserved_event.event_id
    )
    malformed_reserved_event.payload = {"untrusted": "payload"}
    malformed_result = fabric.publish(malformed_reserved_event)
    expect(
        generic_result
        == {
            "status": "disabled",
            "event_id": generic_event.event_id,
            "reason": "reserved_project_guardian_signal_ingress",
        }
        and malformed_result
        == {
            "status": "disabled",
            "event_id": malformed_reserved_event.event_id,
            "reason": "reserved_project_guardian_signal_ingress",
        }
        and signal_envelopes(store) == []
        and frontier_signals(store) == [],
        (
            "generic Event Fabric publish cannot enter the reserved signal "
            "channel even with a malformed payload"
        ),
        {"valid_shape": generic_result, "malformed": malformed_result},
    )

    configure_mode(store, "shadow")
    fabric.configure("shadow")
    foreground_events = [
        EventNormalizer().user_message(
            "untrusted foreground signal payload",
            ProjectGuardianEvaluator.SIGNAL_CHANNEL,
            "user-a",
            "session-foreground-reserved-signal",
            event_id="evt_foreground_reserved_signal_channel",
        ),
        EventNormalizer().user_message(
            "untrusted foreground projection payload",
            "project_guardian",
            "user-a",
            "session-foreground-reserved-projection",
            event_id="evt_foreground_reserved_projection_channel",
        ),
    ]
    foreground_results = [
        fabric.begin(event)
        for event in foreground_events
    ]
    expect(
        foreground_results
        == [
            {
                "status": "suppressed",
                "event_id": foreground_events[0].event_id,
                "reason": "reserved_project_guardian_signal_ingress",
                "finalize_allowed": False,
            },
            {
                "status": "suppressed",
                "event_id": foreground_events[1].event_id,
                "reason": "reserved_project_guardian_projection_ingress",
                "finalize_allowed": False,
            },
        ]
        and signal_envelopes(store) == []
        and frontier_signals(store) == []
        and store.read_json("situation_state.json").get("situations") == [],
        (
            "foreground user intake cannot reuse either reserved Guardian "
            "channel to enter a generic Situation"
        ),
        foreground_results,
    )

    missing_receipt = enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_missing_receipt",
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        scope=scope,
        occurred_at=NOW,
        include_attestation=False,
        enqueue=False,
    )
    bad_receipt = enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_bad_receipt",
        goal_id=registered["goal_id"],
        goal_revision=registered["revision"],
        scope=scope,
        occurred_at=NOW + timedelta(seconds=1),
        receipt_id="pgsr_forged",
        enqueue=False,
    )
    admissions = [
        fabric._admit_project_guardian_signal(event)
        for event in (missing_receipt, bad_receipt)
    ]
    repair = ProjectGuardianSignalLedger(store).reconcile_event_inbox()
    expect(
        all(
            admission.get("signal_ledger_status") == "ignored"
            for admission in admissions
        )
        and store.read_json(
            ProjectGuardianSignalLedger.STATE_FILE
        ).get("signal_count")
        == 0
        and frontier_signals(store) == []
        and repair["accepted_signal_count"] == 0,
        "missing or forged ingress receipts never enter or repair into the ledger",
        {"admissions": admissions, "repair": repair},
    )


def inbox_record(
    store: WorldStateStore,
    event_id: str,
) -> dict[str, Any]:
    events = store.read_json("event_inbox.json").get("events")
    if not isinstance(events, dict):
        return {}
    record = events.get(event_id)
    return copy.deepcopy(record) if isinstance(record, dict) else {}


def test_reserved_signals_never_project_generic_situations(root: Path) -> None:
    trusted_repo, _ = create_git_fixture(root / "trusted" / "git")
    trusted_store = WorldStateStore(root / "trusted" / "state")
    configure_mode(trusted_store, "record_only")
    trusted_fabric = ShadowAwarenessRuntime(
        trusted_store,
        mode="record_only",
    )
    trusted_producer = producer(
        trusted_store,
        trusted_fabric.issue_project_guardian_git_publisher(),
    )
    register_goal(trusted_producer, trusted_repo)
    (trusted_repo / "tracked.txt").write_text(
        "initial\nreserved-signal\n",
        encoding="utf-8",
    )
    trusted_run = trusted_producer.run_once(reason="ledger_only_projection")
    trusted_event_id = str(
        trusted_run["observations"][0].get("event_id") or ""
    )
    trusted_projection = trusted_fabric.process_pending(limit=10)
    trusted_record = inbox_record(trusted_store, trusted_event_id)
    trusted_completion = (
        trusted_record.get("completion_result")
        if isinstance(trusted_record.get("completion_result"), dict)
        else {}
    )
    trusted_situations = trusted_store.read_json(
        "situation_state.json"
    ).get("situations")
    expect(
        trusted_run["status"] == "success"
        and trusted_event_id
        and trusted_projection["processed_count"] == 1
        and trusted_projection["processed"]
        == [
            {
                "event_id": trusted_event_id,
                "situation_id": None,
                "status": "signal_recorded",
            }
        ]
        and trusted_projection["suppressed_count"] == 0
        and trusted_projection["failed_count"] == 0
        and trusted_record.get("status") == "completed"
        and trusted_completion.get("status") == "signal_recorded"
        and trusted_completion.get("signal_ledger_status") == "stale"
        and trusted_store.read_json(
            ProjectGuardianSignalLedger.STATE_FILE
        ).get("signal_count")
        == 1
        and trusted_situations == [],
        (
            "a trusted reserved signal completes as ledger-only background "
            "input and never becomes a generic Situation"
        ),
        {
            "producer_run": trusted_run,
            "projection": trusted_projection,
            "inbox_record": trusted_record,
            "situations": trusted_situations,
        },
    )

    invalid_repo, _ = create_git_fixture(root / "invalid" / "git")
    invalid_store = WorldStateStore(root / "invalid" / "state")
    configure_mode(invalid_store, "record_only")
    invalid_fabric = ShadowAwarenessRuntime(
        invalid_store,
        mode="record_only",
    )
    invalid_producer = producer(
        invalid_store,
        invalid_fabric.issue_project_guardian_git_publisher(),
    )
    registered = register_goal(invalid_producer, invalid_repo)
    invalid_events = [
        enqueue_signal(
            invalid_store,
            kind="git_dirty",
            event_id="evt_background_missing_receipt",
            goal_id=registered["goal_id"],
            goal_revision=registered["revision"],
            scope=registered["scope"],
            occurred_at=NOW,
            include_attestation=False,
            enqueue=False,
        ),
        enqueue_signal(
            invalid_store,
            kind="git_dirty",
            event_id="evt_background_forged_receipt",
            goal_id=registered["goal_id"],
            goal_revision=registered["revision"],
            scope=registered["scope"],
            occurred_at=NOW + timedelta(seconds=1),
            receipt_id="pgsr_forged",
            enqueue=False,
        ),
    ]
    for event in invalid_events:
        invalid_fabric.event_inbox.enqueue(event)
    invalid_projection = invalid_fabric.process_pending(limit=10)
    invalid_records = [
        inbox_record(invalid_store, event.event_id)
        for event in invalid_events
    ]
    invalid_ids = [event.event_id for event in invalid_events]
    invalid_situations = invalid_store.read_json(
        "situation_state.json"
    ).get("situations")
    expect(
        invalid_projection["processed_count"] == 0
        and invalid_projection["suppressed_count"] == len(invalid_events)
        and invalid_projection["suppressed"]
        == [
            {
                "event_id": event_id,
                "status": "suppressed",
                "reason": "invalid_project_guardian_signal",
            }
            for event_id in invalid_ids
        ]
        and invalid_projection["failed_count"] == 0
        and all(
            record.get("status") == "completed"
            and record.get("completion_result")
            == {
                "status": "suppressed",
                "reason": "invalid_project_guardian_signal",
            }
            for record in invalid_records
        )
        and invalid_store.read_json(
            ProjectGuardianSignalLedger.STATE_FILE
        ).get("signal_count")
        == 0
        and invalid_situations == [],
        (
            "missing or forged reserved-channel receipts are suppressed in "
            "the background and never become ledger facts or Situations"
        ),
        {
            "projection": invalid_projection,
            "inbox_records": invalid_records,
            "situations": invalid_situations,
        },
    )


def jsonl_snapshot(store: WorldStateStore) -> dict[str, list[dict[str, Any]]]:
    return {
        name: store.read_jsonl(name, limit=100_000)
        for name in JSONL_FILES
    }


def nonproducer_state_snapshot(
    store: WorldStateStore,
) -> dict[str, dict[str, Any]]:
    return {
        name: store.read_json(name)
        for name in STATE_FILE_LAYOUT
        if name.endswith(".json")
        and name != ProjectGuardianProducerRuntime.STATE_FILE
    }


def test_fault_non_interference_for_all_routes(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    normalizer = EventNormalizer()
    failures: dict[str, Any] = {}
    for case in OFFLINE_ROUTE_CASES:
        baseline_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "baseline",
            mode="record_only",
            case=case,
        )
        fault_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "fault",
            mode="record_only",
            case=case,
        )
        configure_mode(baseline_loop.state_store, "record_only")
        configure_mode(fault_loop.state_store, "record_only")
        baseline_producer = producer(
            baseline_loop.state_store,
            lambda **kwargs: {"status": "unused"},
        )
        register_goal(
            baseline_producer,
            repo,
            user_id="matrix-user",
            goal_id=f"goal_{case.case_id}",
        )
        callback_calls: list[dict[str, Any]] = []

        def failing_ingress(**kwargs: Any) -> dict[str, Any]:
            callback_calls.append(copy.deepcopy(kwargs))
            raise RuntimeError("injected trusted-ingress fault")

        fault_producer = producer(fault_loop.state_store, failing_ingress)
        register_goal(
            fault_producer,
            repo,
            user_id="matrix-user",
            goal_id=f"goal_{case.case_id}",
        )

        state_before = nonproducer_state_snapshot(fault_loop.state_store)
        logs_before = jsonl_snapshot(fault_loop.state_store)
        git_before = git_snapshot(repo)
        fault_run = fault_producer.run_once(
            reason=f"route_fault_{case.case_id}"
        )
        state_after = nonproducer_state_snapshot(fault_loop.state_store)
        logs_after = jsonl_snapshot(fault_loop.state_store)
        git_after = git_snapshot(repo)
        if not (
            fault_run["status"] == "degraded"
            and fault_run["published_count"] == 0
            and fault_run["failed_count"] == 1
            and len(callback_calls) == 1
            and state_after == state_before
            and logs_after == logs_before
            and git_after == git_before
        ):
            failures[f"{case.case_id}:side_effects"] = {
                "run": fault_run,
                "callback_count": len(callback_calls),
                "state_changed": state_after != state_before,
                "logs_changed": logs_after != logs_before,
                "git_before": git_before,
                "git_after": git_after,
            }

        event = normalizer.user_message(
            case.text,
            "guardian-producer-route-matrix",
            "matrix-user",
            f"producer-{case.case_id}",
            event_id=f"evt_producer_matrix_{case.case_id}",
            correlation_id=f"corr-producer-matrix-{case.case_id}",
        )
        baseline_result = baseline_loop.handle_event(event)
        fault_result = fault_loop.handle_event(event)
        equivalent, differences = offline_public_outputs_equivalent(
            baseline_result,
            fault_result,
            require_distinct_generated_ids=True,
        )
        if (
            not equivalent
            or baseline_result.route != case.route
            or fault_result.route != case.route
            or baseline_result.status != case.expected_status
            or fault_result.status != case.expected_status
            or baseline_result.risk_level != case.risk_level
            or fault_result.risk_level != case.risk_level
        ):
            failures[f"{case.case_id}:public_output"] = {
                "differences": differences,
                "baseline": baseline_result.to_dict(),
                "fault": fault_result.to_dict(),
            }

    expect(
        len(OFFLINE_ROUTE_CASES) == 9 and not failures,
        (
            "producer ingress faults preserve all nine Route outputs, status, "
            "risk, Agent/Review/notification/tool state, and Git HEAD/index/ref"
        ),
        failures,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="veyra-project-guardian-producer-"
    ) as temporary:
        root = Path(temporary)
        test_registration_cas_scope_and_repository_binding(
            root / "registration"
        )
        test_repository_rebind_invalidates_old_signal_revision(
            root / "repository-rebind"
        )
        test_disabled_skips_probe_and_publish(root / "disabled")
        test_record_only_trusted_dirty_and_clean_ingress(
            root / "record-only"
        )
        test_read_only_probe_preserves_git_index(root / "index-read-only")
        test_read_only_probe_disables_repository_fsmonitor(
            root / "fsmonitor-disabled"
        )
        test_repository_filters_are_rejected_before_status(
            root / "repository-filter"
        )
        test_included_repository_filters_are_rejected_before_status(
            root / "included-repository-filter"
        )
        test_worktree_repository_filters_are_rejected_before_status(
            root / "worktree-repository-filter"
        )
        test_filter_config_race_cannot_emit_clear(
            root / "filter-config-race"
        )
        test_isolated_status_never_executes_repository_filters(
            root / "isolated-filter-status"
        )
        test_isolated_status_rehashes_racy_clean_content(
            root / "isolated-racy-clean"
        )
        test_isolated_status_supports_linked_split_indexes(
            root / "isolated-linked-split-index"
        )
        test_git_config_cannot_hide_tracked_changes(
            root / "git-config-overrides"
        )
        test_git_symlink_config_cannot_hide_type_changes(
            root / "git-symlink-config"
        )
        test_git_ignorecase_config_cannot_hide_case_renames(
            root / "git-ignorecase-config"
        )
        test_git_environment_cannot_redirect_index(
            root / "git-environment"
        )
        test_replace_refs_cannot_hide_tracked_changes(
            root / "replace-refs"
        )
        test_multiple_origin_urls_are_rejected(
            root / "multiple-origin-urls"
        )
        test_gitlink_without_gitmodules_is_rejected(
            root / "gitlink-without-metadata"
        )
        test_hidden_index_flags_never_emit_clear(root / "hidden-index-flags")
        test_signal_ledger_failure_degrades_publish_result(
            root / "ledger-failure"
        )
        test_observation_age_is_rechecked_at_commit(
            root / "observation-age"
        )
        test_probe_tail_window_is_point_in_time(root / "tail-window")
        test_git_failures_never_clear_a_positive_frontier(
            root / "git-failure"
        )
        test_reserved_ingress_and_receipt_validation(
            root / "reserved-ingress"
        )
        test_reserved_signals_never_project_generic_situations(
            root / "reserved-background"
        )
        test_fault_non_interference_for_all_routes(root / "non-interference")
    print("All Project Guardian producer smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
