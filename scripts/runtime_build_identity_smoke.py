#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime.isolated_git_snapshot as isolated_git_snapshot
from runtime.build_identity import RuntimeBuildIdentity
from runtime.isolated_git_snapshot import (
    GitSnapshotUnavailable,
    capture_isolated_git_snapshot,
)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def git(root: Path, *args: str) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Git setup command failed: {args!r}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def initialized_repo(root: Path, *, object_format: str | None = None) -> Path:
    root.mkdir(parents=True)
    init_args = ["init", "-q"]
    if object_format is not None:
        init_args.append(f"--object-format={object_format}")
    git(root, *init_args)
    git(root, "config", "user.email", "veyra-smoke@example.invalid")
    git(root, "config", "user.name", "Veyra Smoke")
    (root / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(root, "add", ".gitignore", "tracked.txt")
    git(root, "commit", "-qm", "initial")
    return root


def test_current_checkout_and_projection_purity() -> None:
    identity = RuntimeBuildIdentity.capture(repository_root=ROOT)
    projection = identity.public_projection()
    expect(projection["status"] == "available", "checkout identity available")
    expect(
        isinstance(projection["build_revision"], str)
        and len(str(projection["build_revision"])) in {40, 64},
        "checkout exposes a full Git object id",
    )
    expect(isinstance(projection["dirty_flag"], bool), "dirty flag is boolean")
    expect(
        projection["schema_version"] == "veyra.runtime_build_identity.v2"
        and projection["source"] == "startup_git_snapshot"
        and isinstance(projection["captured_at"], str)
        and projection["loaded_code_attested"] is False
        and projection["unavailable_reason"] is None
        and projection["git_checked_on_request"] is False,
        "projection states its bounded authority",
    )
    with patch(
        "runtime.isolated_git_snapshot.subprocess.run",
        side_effect=AssertionError("projection attempted Git"),
    ):
        expect(
            identity.public_projection() == projection
            and identity.public_projection() is not projection,
            "repeated projections are frozen values in fresh dictionaries",
        )


def test_dirty_semantics_and_repository_index_purity(root: Path) -> None:
    repo = initialized_repo(root)
    clean = capture_isolated_git_snapshot(repo)
    expect(clean.dirty is False, "clean repository is clean")

    ignored = repo / "ignored.tmp"
    ignored.write_text("ignored\n", encoding="utf-8")
    expect(
        capture_isolated_git_snapshot(repo).dirty is False,
        "ignored files do not make the snapshot dirty",
    )

    untracked = repo / "untracked.txt"
    untracked.write_text("untracked\n", encoding="utf-8")
    expect(
        capture_isolated_git_snapshot(repo).dirty is True,
        "untracked files make the snapshot dirty",
    )
    untracked.unlink()

    tracked = repo / "tracked.txt"
    tracked.write_text("modified\n", encoding="utf-8")
    expect(
        capture_isolated_git_snapshot(repo).dirty is True,
        "unstaged tracked changes make the snapshot dirty",
    )
    git(repo, "add", "tracked.txt")
    expect(
        capture_isolated_git_snapshot(repo).dirty is True,
        "staged changes make the snapshot dirty",
    )

    git(repo, "commit", "-qm", "modified")
    index = repo / ".git" / "index"
    before_bytes = index.read_bytes()
    before_mtime = index.stat().st_mtime_ns
    os.utime(tracked, None)
    expect(
        capture_isolated_git_snapshot(repo).dirty is False,
        "stat-only worktree drift does not invent a content change",
    )
    expect(
        index.read_bytes() == before_bytes
        and index.stat().st_mtime_ns == before_mtime,
        "snapshot never refreshes or rewrites the repository index",
    )


def test_repository_hooks_and_environment_are_not_executed(root: Path) -> None:
    repo = initialized_repo(root)
    marker = root / "fsmonitor-executed"
    hook = root / "fsmonitor-hook.sh"
    hook.write_text(
        "#!/bin/sh\n"
        f"touch '{marker}'\n"
        "printf '2\\n\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    git(repo, "config", "core.fsmonitor", str(hook))

    original_env = dict(os.environ)
    try:
        os.environ["GIT_DIR"] = str(root / "attacker-git-dir")
        os.environ["GIT_WORK_TREE"] = str(root / "attacker-worktree")
        os.environ["GIT_INDEX_FILE"] = str(root / "attacker-index")
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
        os.environ["GIT_CONFIG_VALUE_0"] = str(hook)
        snapshot = capture_isolated_git_snapshot(repo)
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    expect(snapshot.revision == git(repo, "rev-parse", "HEAD"), "env ignored")
    expect(not marker.exists(), "fsmonitor hook was not executed")


def test_sha256_split_index_and_unsupported_flags(root: Path) -> None:
    sha256_repo = initialized_repo(root / "sha256", object_format="sha256")
    sha256 = capture_isolated_git_snapshot(sha256_repo)
    expect(
        sha256.object_format == "sha256" and len(sha256.revision) == 64,
        "SHA-256 repositories expose a full object id",
    )

    split_repo = initialized_repo(root / "split-index")
    git(split_repo, "update-index", "--split-index")
    expect(
        capture_isolated_git_snapshot(split_repo).dirty is False,
        "a clean linked split index is supported",
    )
    (split_repo / "tracked.txt").write_text("split dirty\n", encoding="utf-8")
    expect(
        capture_isolated_git_snapshot(split_repo).dirty is True,
        "linked split index still detects worktree changes",
    )

    linked_base = initialized_repo(root / "linked-base")
    linked_worktree = root / "linked-worktree"
    git(
        linked_base,
        "worktree",
        "add",
        "-q",
        "-b",
        "runtime-build-linked-smoke",
        str(linked_worktree),
    )
    expect(
        capture_isolated_git_snapshot(linked_worktree).dirty is False,
        "a clean linked worktree is supported",
    )
    git(linked_worktree, "update-index", "--split-index")
    expect(
        capture_isolated_git_snapshot(linked_worktree).dirty is False,
        "a clean linked worktree with a split index is supported",
    )
    (linked_worktree / "tracked.txt").write_text(
        "linked split dirty\n",
        encoding="utf-8",
    )
    expect(
        capture_isolated_git_snapshot(linked_worktree).dirty is True,
        "linked-worktree split index detects worktree changes",
    )

    flags_repo = initialized_repo(root / "unsupported-flags")
    git(flags_repo, "update-index", "--assume-unchanged", "tracked.txt")
    flags_identity = RuntimeBuildIdentity.capture(
        repository_root=flags_repo
    ).public_projection()
    expect(
        flags_identity["status"] == "unavailable"
        and flags_identity["unavailable_reason"] == "unsupported_layout",
        "assume-unchanged cannot produce a falsely clean identity",
    )


def test_repository_filters_fail_closed_without_execution(root: Path) -> None:
    repo = initialized_repo(root)
    marker = root / "filter-executed"
    filter_script = root / "clean-filter.sh"
    filter_script.write_text(
        "#!/bin/sh\n"
        f"touch '{marker}'\n"
        "cat\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o700)
    git(repo, "config", "filter.evil.clean", str(filter_script))
    git(repo, "config", "filter.evil.required", "true")
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil\n",
        encoding="utf-8",
    )

    identity = RuntimeBuildIdentity.capture(repository_root=repo)
    projection = identity.public_projection()
    expect(
        projection["status"] == "unavailable"
        and projection["build_revision"] is None
        and projection["dirty_flag"] is None
        and projection["unavailable_reason"] == "unsafe_config",
        "repository filters fail closed as a complete unavailable identity",
    )
    expect(not marker.exists(), "repository clean filter was not executed")


def test_unavailable_partial_timeout_and_drift(root: Path) -> None:
    non_git = RuntimeBuildIdentity.capture(repository_root=root / "non-git")
    non_git_projection = non_git.public_projection()
    expect(
        non_git_projection["status"] == "unavailable"
        and non_git_projection["build_revision"] is None
        and non_git_projection["dirty_flag"] is None
        and non_git_projection["unavailable_reason"] == "non_git",
        "non-Git deployment fails closed without partial identity",
    )

    partial = RuntimeBuildIdentity(
        build_revision="a" * 40,
        dirty_flag=None,
        started_at="2026-01-01T00:00:00+00:00",
        captured_at=None,
        unavailable_reason="probe_error",
    ).public_projection()
    expect(
        partial["status"] == "unavailable"
        and partial["build_revision"] is None
        and partial["dirty_flag"] is None
        and partial["captured_at"] is None
        and partial["unavailable_reason"] == "probe_error",
        "partial identity can never project as available",
    )

    forged = RuntimeBuildIdentity(
        build_revision="not-a-revision",
        dirty_flag=False,
        started_at="not-a-time",
        captured_at="not-a-time",
        unavailable_reason="/private/path/attacker-controlled-error",
    ).public_projection()
    expect(
        forged["status"] == "unavailable"
        and forged["build_revision"] is None
        and forged["dirty_flag"] is None
        and forged["started_at"] is None
        and forged["captured_at"] is None
        and forged["unavailable_reason"] == "probe_error"
        and "/private/path" not in str(forged),
        "invalid direct construction fails closed without error disclosure",
    )

    reversed_time = RuntimeBuildIdentity(
        build_revision="a" * 40,
        dirty_flag=False,
        started_at="2026-01-01T01:00:00+00:00",
        captured_at="2026-01-01T00:00:00+00:00",
    ).public_projection()
    expect(
        reversed_time["status"] == "unavailable"
        and reversed_time["build_revision"] is None
        and reversed_time["dirty_flag"] is None
        and reversed_time["captured_at"] is None,
        "capture time before start time cannot project as available",
    )

    oversized_time = "2026-01-01T00:00:00." + ("1" * 10_000) + "+00:00"
    oversized = RuntimeBuildIdentity(
        build_revision="a" * 40,
        dirty_flag=False,
        started_at=oversized_time,
        captured_at=oversized_time,
    ).public_projection()
    expect(
        oversized["status"] == "unavailable"
        and oversized["started_at"] is None
        and oversized["captured_at"] is None
        and len(str(oversized)) < 512,
        "oversized timestamps fail closed without an unbounded response",
    )

    for extreme_time in (
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ):
        extreme = RuntimeBuildIdentity(
            build_revision="a" * 40,
            dirty_flag=False,
            started_at=extreme_time,
            captured_at=extreme_time,
        ).public_projection()
        expect(
            extreme["status"] == "unavailable"
            and extreme["started_at"] is None
            and extreme["captured_at"] is None,
            "UTC-overflow timestamps fail closed without an exception",
        )

    unhashable_reason = RuntimeBuildIdentity(
        build_revision="a" * 40,
        dirty_flag=False,
        started_at="2026-01-01T00:00:00+00:00",
        captured_at="2026-01-01T00:00:01+00:00",
        unavailable_reason=["attacker-controlled"],  # type: ignore[arg-type]
    ).public_projection()
    expect(
        unhashable_reason["status"] == "unavailable"
        and unhashable_reason["unavailable_reason"] == "probe_error",
        "non-string unavailable reasons fail closed without an exception",
    )

    repo = initialized_repo(root / "timeout")
    with patch(
        "runtime.isolated_git_snapshot.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["git"], 0.2),
    ):
        timed_out = RuntimeBuildIdentity.capture(
            repository_root=repo
        ).public_projection()
    expect(
        timed_out["status"] == "unavailable"
        and timed_out["unavailable_reason"] == "timeout",
        "timeout fails closed with a bounded reason",
    )

    pass_one = isolated_git_snapshot._SnapshotPass(
        revision="a" * 40,
        dirty=False,
        object_format="sha1",
    )
    pass_two = isolated_git_snapshot._SnapshotPass(
        revision="b" * 40,
        dirty=False,
        object_format="sha1",
    )
    with patch.object(
        isolated_git_snapshot._IsolatedGitReader,
        "capture_pass",
        side_effect=[pass_one, pass_two],
    ):
        try:
            capture_isolated_git_snapshot(repo)
        except GitSnapshotUnavailable as exc:
            expect(exc.reason_code == "drift", "drift reason is bounded")
        else:
            raise AssertionError("two-pass revision drift must fail closed")


def test_real_head_index_and_worktree_drift(root: Path) -> None:
    head_repo = initialized_repo(root / "head")
    head_original = isolated_git_snapshot._IsolatedGitReader.capture_pass
    head_calls = 0

    def capture_with_head_drift(
        reader: isolated_git_snapshot._IsolatedGitReader,
    ) -> isolated_git_snapshot._SnapshotPass:
        nonlocal head_calls
        result = head_original(reader)
        head_calls += 1
        if head_calls == 1:
            (head_repo / "tracked.txt").write_text(
                "new committed head\n",
                encoding="utf-8",
            )
            git(head_repo, "add", "tracked.txt")
            git(head_repo, "commit", "-qm", "racing head")
        return result

    with patch.object(
        isolated_git_snapshot._IsolatedGitReader,
        "capture_pass",
        new=capture_with_head_drift,
    ):
        try:
            capture_isolated_git_snapshot(head_repo)
        except GitSnapshotUnavailable as exc:
            expect(exc.reason_code == "drift", "HEAD drift reason is bounded")
        else:
            raise AssertionError("HEAD drift between passes must fail closed")

    worktree_repo = initialized_repo(root / "worktree")
    worktree_original = isolated_git_snapshot._IsolatedGitReader.capture_pass
    worktree_calls = 0

    def capture_with_worktree_drift(
        reader: isolated_git_snapshot._IsolatedGitReader,
    ) -> isolated_git_snapshot._SnapshotPass:
        nonlocal worktree_calls
        result = worktree_original(reader)
        worktree_calls += 1
        if worktree_calls == 1:
            (worktree_repo / "tracked.txt").write_text(
                "racing worktree\n",
                encoding="utf-8",
            )
        return result

    with patch.object(
        isolated_git_snapshot._IsolatedGitReader,
        "capture_pass",
        new=capture_with_worktree_drift,
    ):
        try:
            capture_isolated_git_snapshot(worktree_repo)
        except GitSnapshotUnavailable as exc:
            expect(
                exc.reason_code == "drift",
                "worktree drift reason is bounded",
            )
        else:
            raise AssertionError("worktree drift between passes must fail closed")

    index_repo = initialized_repo(root / "index")
    dirty_original = (
        isolated_git_snapshot._IsolatedGitReader._isolated_dirty_status
    )
    index_raced = False

    def status_with_index_drift(
        reader: isolated_git_snapshot._IsolatedGitReader,
        **kwargs: object,
    ) -> bool:
        nonlocal index_raced
        result = dirty_original(reader, **kwargs)  # type: ignore[arg-type]
        if not index_raced:
            index_raced = True
            (index_repo / "tracked.txt").write_text(
                "racing index\n",
                encoding="utf-8",
            )
            git(index_repo, "add", "tracked.txt")
        return result

    with patch.object(
        isolated_git_snapshot._IsolatedGitReader,
        "_isolated_dirty_status",
        new=status_with_index_drift,
    ):
        try:
            capture_isolated_git_snapshot(index_repo)
        except GitSnapshotUnavailable as exc:
            expect(exc.reason_code == "drift", "index drift reason is bounded")
        else:
            raise AssertionError("index drift inside a pass must fail closed")


def main() -> int:
    test_current_checkout_and_projection_purity()
    with tempfile.TemporaryDirectory(
        prefix="veyra-runtime-build-identity-smoke-"
    ) as temporary:
        root = Path(temporary)
        test_dirty_semantics_and_repository_index_purity(root / "dirty")
        test_repository_hooks_and_environment_are_not_executed(root / "hooks")
        test_sha256_split_index_and_unsupported_flags(root / "formats")
        test_repository_filters_fail_closed_without_execution(root / "filters")
        test_unavailable_partial_timeout_and_drift(root / "unavailable")
        test_real_head_index_and_worktree_drift(root / "drift")
    print("runtime build identity smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
