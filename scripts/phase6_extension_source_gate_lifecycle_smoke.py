#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.extension_artifact import (  # noqa: E402
    artifact_owner_scope_digest,
    parse_extension_artifact,
)
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime.extension_artifact_quarantine import (  # noqa: E402
    STATE_FILE as ARTIFACT_STATE_FILE,
    ExtensionArtifactConflictError,
    ExtensionArtifactQuarantine,
    ExtensionArtifactStorageError,
)
from runtime.extension_source_checker import (  # noqa: E402
    MAX_SOURCE_AST_NODES,
    check_extension_source,
)
from runtime.extension_source_policy_gate import (  # noqa: E402
    STATE_FILE as SOURCE_CHECK_STATE_FILE,
    ExtensionSourceCheckConflictError,
    ExtensionSourceCheckNotFoundError,
    ExtensionSourceCheckStorageError,
    ExtensionSourcePolicyGate,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecConflictError,
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_artifact_contract_smoke import (  # noqa: E402
    valid_artifact_envelope,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


USER = "phase6-extension-source-gate-user"
WORKSPACE = "/private/veyra/phase6-extension-source-gate"
BASE_TIME = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
VALID_SOURCE = (
    b"def run_extension(payload):\n"
    b"    return {\"label\": payload[\"name\"]}\n"
)


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class CountingChecker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        with self._lock:
            self.calls += 1
        return check_extension_source(**kwargs)


class CrashChecker(CountingChecker):
    def __call__(self, **kwargs: Any) -> Any:
        with self._lock:
            self.calls += 1
        raise KeyboardInterrupt("simulated process interruption")


@dataclass(frozen=True)
class SourceGateContext:
    root: Path
    store: WorldStateStore
    clock: Clock
    spec_runtime: ExtensionSpecQuarantine
    artifact_runtime: ExtensionArtifactQuarantine
    source_gate: ExtensionSourcePolicyGate
    checker: CountingChecker
    candidate: dict[str, Any]
    artifact: dict[str, Any]
    source: bytes


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> BaseException:
    try:
        call()
    except errors as exc:
        print(f"PASS {label}")
        return exc
    raise AssertionError(f"{label}: call did not fail closed")


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def prepare_store(root: Path) -> WorldStateStore:
    store = WorldStateStore(root)
    store.mutate_json(
        "local_world.json",
        lambda state: {
            **state,
            "current_project": WORKSPACE,
        },
    )
    return store


def core_invariants(store: WorldStateStore) -> dict[str, Any]:
    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    snapshot = CapabilityRegistry(store, now=fixed).snapshot()
    capabilities = snapshot.get("capabilities", {})
    return {
        "capability": {
            "routes": snapshot.get("routes"),
            "items": {
                capability_id: {
                    key: value.get(key)
                    for key in (
                        "available",
                        "kind",
                        "route",
                        "executor",
                        "status",
                        "reason",
                        "namespace",
                    )
                }
                for capability_id, value in capabilities.items()
                if isinstance(value, dict)
            },
        },
        "memory": store.read_json("agent_memory.json"),
        "agent_config": store.read_json("agent_config.json"),
        "task": store.read_json("task_state.json"),
        "durable_case": store.read_json("durable_case_state.json"),
        "phase6_collaboration": store.read_json(
            "phase6_collaboration_state.json"
        ),
        "self_improvement": store.read_json(
            "self_improvement_proposals.json"
        ),
        "state_change_proposals": store.read_json(
            "state_change_proposals.json"
        ),
        "review": store.read_json("review_queue.json"),
        "tool_governance": store.read_json(
            "tool_governance_state.json"
        ),
        "openclaw_broker": store.read_json(
            "openclaw_tool_hook_state.json"
        ),
    }


def action_bytes(store: WorldStateStore) -> bytes:
    path = store.path_for("action_record.jsonl")
    return path.read_bytes() if path.exists() else b""


def prepare_context(
    root: Path,
    *,
    extension_id: str,
    operation_prefix: str,
    source: bytes = VALID_SOURCE,
    artifact_expires_at: datetime | None = None,
    checker: CountingChecker | None = None,
) -> SourceGateContext:
    store = prepare_store(root)
    clock = Clock(BASE_TIME)
    spec_runtime = ExtensionSpecQuarantine(
        state_store=store,
        now=clock,
    )
    artifact_runtime = ExtensionArtifactQuarantine(
        state_store=store,
        spec_quarantine=spec_runtime,
        now=clock,
    )
    spec_payload = valid_spec(now=clock.current)
    spec_payload["extension_id"] = extension_id
    spec_payload["dependencies"] = []
    spec_payload["purpose"] = (
        "Validate one exact source blob with a fixed non-executing "
        "AST policy."
    )
    spec = parse_extension_spec(spec_payload)
    quarantined = spec_runtime.quarantine(
        spec=spec,
        expected_spec_digest=spec.digest(),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=f"{operation_prefix}-spec-submit",
    )
    candidate = spec_runtime.review(
        candidate_id=quarantined["candidate_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        expected_revision=quarantined["candidate_revision"],
        operation_id=f"{operation_prefix}-spec-gate",
        decision="accept_for_future_isolated_generation",
        reason="Permit only inert artifact admission and source checking.",
    )
    subject = spec_runtime.artifact_subject(
        candidate_id=candidate["candidate_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        require_gate_passed=True,
    )
    envelope = valid_artifact_envelope(
        content=source,
        now=clock.current,
    )
    envelope.update(
        {
            "candidate_id": subject["candidate_id"],
            "candidate_revision": subject["candidate_revision"],
            "owner_scope_digest": artifact_owner_scope_digest(
                USER,
                WORKSPACE,
            ),
            "extension_id": subject["extension_id"],
            "extension_version": subject["extension_version"],
            "spec_digest": subject["spec_digest"],
            "extension_policy_revision": subject[
                "extension_policy_revision"
            ],
            "expires_at": canonical_utc(
                artifact_expires_at
                or clock.current + timedelta(days=5)
            ),
        }
    )
    parsed_envelope = parse_extension_artifact(envelope)
    artifact = artifact_runtime.submit(
        envelope=parsed_envelope,
        expected_artifact_sha256=parsed_envelope.artifact_sha256,
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=f"{operation_prefix}-artifact-submit",
    )
    selected_checker = checker or CountingChecker()
    source_gate = ExtensionSourcePolicyGate(
        state_store=store,
        artifact_quarantine=artifact_runtime,
        checker=selected_checker,
        now=clock,
    )
    return SourceGateContext(
        root=root,
        store=store,
        clock=clock,
        spec_runtime=spec_runtime,
        artifact_runtime=artifact_runtime,
        source_gate=source_gate,
        checker=selected_checker,
        candidate=candidate,
        artifact=artifact,
        source=source,
    )


def start_check(
    context: SourceGateContext,
    operation_id: str,
    *,
    expected_revision: int | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    return context.source_gate.start(
        artifact_id=context.artifact["artifact_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        expected_artifact_revision=(
            expected_revision
            if expected_revision is not None
            else context.artifact["artifact_revision"]
        ),
        expected_artifact_sha256=(
            expected_digest
            or context.artifact["artifact_sha256"]
        ),
        operation_id=operation_id,
    )


def assert_zero_dynamic_authority(
    value: dict[str, Any],
    label: str,
) -> None:
    expect(
        value["isolated_generation_status"] == "not_started"
        and value["unit_checks_status"] == "not_started"
        and value["contract_checks_status"] == "not_started"
        and value["security_runtime_checks_status"] == "not_started"
        and value["fuzz_checks_status"] == "not_started"
        and value["test_execution_status"] == "not_started"
        and value["behavior_verification_status"] == "not_started"
        and value["execution_status"] == "not_started"
        and value["signature_status"]
        in {"not_started", "not_implemented"}
        and value["activation_status"]
        in {"not_started", "not_installed"}
        and value["capability_registry_visible"] is False
        and value["promotion_authorized"] is False
        and value["policy_effect"] == "none"
        and not any(value["authority"].values()),
        label,
        value,
    )


def private_blob_path(context: SourceGateContext) -> Path:
    state = context.store.read_json(ARTIFACT_STATE_FILE)
    record = state["artifacts"][context.artifact["artifact_id"]]
    return (
        context.store.path_for(ARTIFACT_STATE_FILE).parent
        / "phase6_extension_artifacts"
        / record["blob"]["relative_filename"]
    )


def main_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-lifecycle-"
    ) as raw:
        context = prepare_context(
            Path(raw),
            extension_id="example.source_gate_lifecycle",
            operation_prefix="source-gate-lifecycle",
        )
        before = core_invariants(context.store)
        result = start_check(
            context,
            "source-gate-lifecycle-start",
        )
        expect(
            result["source_check_status"] == "passed"
            and result["source_syntax_status"] == "passed"
            and result["static_checks_status"] == "passed"
            and result["static_security_policy_status"] == "passed"
            and result["issue_codes"] == []
            and context.checker.calls == 1,
            "fresh exact dependency-free Spec and Artifact pass source gate",
            result,
        )
        assert_zero_dynamic_authority(
            result,
            "source gate pass grants no dynamic or candidate authority",
        )

        private_state = context.store.read_json(
            SOURCE_CHECK_STATE_FILE
        )
        private_text = json.dumps(
            private_state,
            ensure_ascii=False,
            sort_keys=True,
        )
        world = context.store.read_all()
        world_text = json.dumps(
            world,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            private_state["check_count"] == 1
            and result["check_id"] in private_state["checks"]
            and "phase6_extension_source_check_state" not in world
            and context.source.decode("utf-8") not in private_text
            and context.source.decode("utf-8") not in world_text
            and USER not in private_text
            and WORKSPACE not in private_text,
            "source-check state is private and source-free",
            private_state,
        )

        surfaces = {
            "status": context.source_gate.status(),
            "get": context.source_gate.get(
                check_id=result["check_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "list": context.source_gate.list(
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "integrity": context.source_gate.integrity(
                check_id=result["check_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "projection": context.source_gate.projection_for_artifact(
                artifact_id=context.artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
        }
        public_text = json.dumps(
            surfaces,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            context.source.decode("utf-8") not in public_text
            and USER not in public_text
            and WORKSPACE not in public_text
            and str(private_blob_path(context)) not in public_text,
            "public source-check surfaces expose no source, owner, or path",
            surfaces,
        )

        state_path = context.store.path_for(
            SOURCE_CHECK_STATE_FILE
        )
        state_before_replay = state_path.read_bytes()
        audit_before_replay = action_bytes(context.store)
        replay = start_check(
            context,
            "source-gate-lifecycle-start",
        )
        expect(
            replay["operation_replayed"] is True
            and replay["check_id"] == result["check_id"]
            and context.checker.calls == 1
            and state_path.read_bytes() == state_before_replay
            and action_bytes(context.store) == audit_before_replay,
            "exact replay is a checker, state, and global-audit no-op",
            replay,
        )
        expect_raises(
            (
                ExtensionSourceCheckConflictError,
                ExtensionArtifactConflictError,
                ValueError,
            ),
            lambda: start_check(
                context,
                "source-gate-lifecycle-start",
                expected_digest="f" * 64,
            ),
            "operation replay cannot change the artifact digest binding",
        )

        restarted_store = WorldStateStore(context.root)
        restarted_spec = ExtensionSpecQuarantine(
            state_store=restarted_store,
            now=context.clock,
        )
        restarted_artifact = ExtensionArtifactQuarantine(
            state_store=restarted_store,
            spec_quarantine=restarted_spec,
            now=context.clock,
        )
        restarted_checker = CountingChecker()
        restarted_gate = ExtensionSourcePolicyGate(
            state_store=restarted_store,
            artifact_quarantine=restarted_artifact,
            checker=restarted_checker,
            now=context.clock,
        )
        restarted = restarted_gate.get(
            check_id=result["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        restarted_replay = restarted_gate.start(
            artifact_id=context.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_artifact_revision=context.artifact[
                "artifact_revision"
            ],
            expected_artifact_sha256=context.artifact[
                "artifact_sha256"
            ],
            operation_id="source-gate-lifecycle-start",
        )
        expect(
            restarted["check_id"] == result["check_id"]
            and restarted["source_check_status"] == "passed"
            and restarted_replay["operation_replayed"] is True
            and restarted_checker.calls == 0,
            "restart preserves exact source-check result and replay",
            restarted_replay,
        )
        expect(
            before == core_invariants(context.store),
            "source gate changes no capability, memory, review, task, "
            "Tool Governance, or broker state",
        )


def malicious_sources() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-attacks-"
    ) as raw:
        root = Path(raw)
        cases: list[
            tuple[str, Callable[[Path], bytes], str]
        ] = [
            (
                "dangerous top-level sentinel",
                lambda sentinel: (
                    f"open({str(sentinel)!r}, 'w').write('executed')\n"
                    + VALID_SOURCE.decode("utf-8")
                ).encode("utf-8"),
                "top_level_shape_invalid",
            ),
            (
                "top-level import",
                lambda _sentinel: b"import os\n" + VALID_SOURCE,
                "top_level_shape_invalid",
            ),
            (
                "function call",
                lambda _sentinel: (
                    b"def run_extension(payload):\n"
                    b"    return {\"label\": str(payload[\"name\"])}\n"
                ),
                "input_projection_invalid",
            ),
            (
                "attribute call",
                lambda _sentinel: (
                    b"def run_extension(payload):\n"
                    b"    return {\"label\": payload.get(\"name\")}\n"
                ),
                "input_projection_invalid",
            ),
            (
                "decorator expression",
                lambda sentinel: (
                    f"@open({str(sentinel)!r}, 'w').write\n"
                    + VALID_SOURCE.decode("utf-8")
                ).encode("utf-8"),
                "entrypoint_signature_invalid",
            ),
            (
                "default expression",
                lambda sentinel: (
                    "def run_extension("
                    f"payload=open({str(sentinel)!r}, 'w').write('x')"
                    "):\n"
                    "    return {\"label\": \"never\"}\n"
                ).encode("utf-8"),
                "entrypoint_signature_invalid",
            ),
            (
                "annotation expression",
                lambda sentinel: (
                    "def run_extension("
                    f"payload: open({str(sentinel)!r}, 'w').write('x')"
                    "):\n"
                    "    return {\"label\": \"never\"}\n"
                ).encode("utf-8"),
                "entrypoint_signature_invalid",
            ),
            (
                "dunder access",
                lambda _sentinel: (
                    b"def run_extension(payload):\n"
                    b"    return {\"label\": payload.__class__}\n"
                ),
                "input_projection_invalid",
            ),
            (
                "AST depth budget",
                lambda _sentinel: (
                    "def run_extension(payload):\n"
                    "    return {\"label\": "
                    + ("+" * 20)
                    + "1}\n"
                ).encode("utf-8"),
                "source_ast_budget_exceeded",
            ),
            (
                "AST node budget",
                lambda _sentinel: (
                    "def run_extension(payload):\n    return {"
                    + ", ".join(
                        f"\"k{index}\": \"v\""
                        for index in range(MAX_SOURCE_AST_NODES)
                    )
                    + "}\n"
                ).encode("utf-8"),
                "source_ast_budget_exceeded",
            ),
            (
                "invalid syntax",
                lambda _sentinel: b"def run_extension(:\n",
                "source_syntax_invalid",
            ),
        ]
        for index, (label, source_factory, issue_code) in enumerate(
            cases
        ):
            sentinel = root / f"must-not-execute-{index}"
            context = prepare_context(
                root / f"case-{index}",
                extension_id=f"example.source_gate_attack_{index}",
                operation_prefix=f"source-gate-attack-{index}",
                source=source_factory(sentinel),
            )
            before = core_invariants(context.store)
            result = start_check(
                context,
                f"source-gate-attack-{index}-start",
            )
            expect(
                result["source_check_status"] == "failed"
                and issue_code in result["issue_codes"]
                and context.checker.calls == 1
                and not sentinel.exists(),
                f"{label} fails without candidate execution",
                result,
            )
            assert_zero_dynamic_authority(
                result,
                f"{label} failure grants no dynamic authority",
            )
            expect(
                before == core_invariants(context.store),
                f"{label} changes no governed core state",
            )


def concurrency_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-concurrency-"
    ) as raw:
        context = prepare_context(
            Path(raw),
            extension_id="example.source_gate_concurrency",
            operation_prefix="source-gate-concurrency",
        )

        def run(operation_id: str) -> dict[str, Any]:
            return start_check(context, operation_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    run,
                    (
                        "source-gate-concurrent-a",
                        "source-gate-concurrent-b",
                    ),
                )
            )
        state = context.store.read_json(SOURCE_CHECK_STATE_FILE)
        expect(
            results[0]["check_id"] == results[1]["check_id"]
            and context.checker.calls == 1
            and state["check_count"] == 1
            and len(state["artifact_index"]) == 1,
            "concurrent starts persist and evaluate one exact source check",
            results,
        )


def prerequisite_lifecycle() -> None:
    conflict_errors = (
        ExtensionSourceCheckConflictError,
        ExtensionArtifactConflictError,
        ExtensionSpecConflictError,
        ExtensionSourceCheckStorageError,
    )
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-prerequisites-"
    ) as raw:
        root = Path(raw)

        revoked_spec = prepare_context(
            root / "spec-revoked",
            extension_id="example.source_gate_spec_revoked",
            operation_prefix="source-gate-spec-revoked",
        )
        revoked_spec.spec_runtime.revoke(
            candidate_id=revoked_spec.candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=revoked_spec.candidate[
                "candidate_revision"
            ],
            operation_id="source-gate-spec-revoked-control",
            reason="Revoke before source checking.",
        )
        expect_raises(
            conflict_errors,
            lambda: start_check(
                revoked_spec,
                "source-gate-after-spec-revoke",
            ),
            "Spec revoke before start fails closed",
        )

        revoked_artifact = prepare_context(
            root / "artifact-revoked",
            extension_id="example.source_gate_artifact_revoked",
            operation_prefix="source-gate-artifact-revoked",
        )
        revoked_artifact.artifact_runtime.revoke(
            artifact_id=revoked_artifact.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=revoked_artifact.artifact[
                "artifact_revision"
            ],
            operation_id="source-gate-artifact-revoked-control",
            reason="Revoke before source checking.",
        )
        expect_raises(
            conflict_errors,
            lambda: start_check(
                revoked_artifact,
                "source-gate-after-artifact-revoke",
            ),
            "Artifact revoke before start fails closed",
        )

        expired = prepare_context(
            root / "expired",
            extension_id="example.source_gate_expired",
            operation_prefix="source-gate-expired",
            artifact_expires_at=BASE_TIME + timedelta(seconds=1),
        )
        expired.clock.current = BASE_TIME + timedelta(seconds=1)
        expect_raises(
            conflict_errors,
            lambda: start_check(
                expired,
                "source-gate-after-expiry",
            ),
            "Artifact expiry before start fails closed",
        )

        drifted = prepare_context(
            root / "drifted",
            extension_id="example.source_gate_drifted",
            operation_prefix="source-gate-drifted",
        )
        expect_raises(
            conflict_errors + (ValueError,),
            lambda: start_check(
                drifted,
                "source-gate-revision-drift",
                expected_revision=(
                    drifted.artifact["artifact_revision"] + 1
                ),
            ),
            "artifact revision drift before start fails closed",
        )
        expect_raises(
            conflict_errors + (ValueError,),
            lambda: start_check(
                drifted,
                "source-gate-digest-drift",
                expected_digest="f" * 64,
            ),
            "artifact digest drift before start fails closed",
        )

        after_spec = prepare_context(
            root / "after-spec",
            extension_id="example.source_gate_after_spec",
            operation_prefix="source-gate-after-spec",
        )
        passed = start_check(
            after_spec,
            "source-gate-before-spec-revoke",
        )
        after_spec.spec_runtime.revoke(
            candidate_id=after_spec.candidate["candidate_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=after_spec.candidate[
                "candidate_revision"
            ],
            operation_id="source-gate-after-spec-revoke-control",
            reason="Invalidate a completed static result.",
        )
        state_before = after_spec.store.path_for(
            SOURCE_CHECK_STATE_FILE
        ).read_bytes()
        audit_before = action_bytes(after_spec.store)
        replay = start_check(
            after_spec,
            "source-gate-before-spec-revoke",
        )
        expect(
            replay["check_id"] == passed["check_id"]
            and replay["operation_replayed"] is True
            and replay["effective_status"]
            in {
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
                "SOURCE_CHECK_INDETERMINATE",
            }
            and after_spec.checker.calls == 1
            and after_spec.store.path_for(
                SOURCE_CHECK_STATE_FILE
            ).read_bytes()
            == state_before
            and action_bytes(after_spec.store) == audit_before,
            "exact replay after Spec revoke is blocked and side-effect free",
            replay,
        )
        expect_raises(
            conflict_errors,
            lambda: start_check(
                after_spec,
                "source-gate-new-after-spec-revoke",
            ),
            "new operation after Spec revoke fails closed",
        )

        after_artifact = prepare_context(
            root / "after-artifact",
            extension_id="example.source_gate_after_artifact",
            operation_prefix="source-gate-after-artifact",
        )
        after_artifact_result = start_check(
            after_artifact,
            "source-gate-before-artifact-revoke",
        )
        after_artifact.artifact_runtime.revoke(
            artifact_id=after_artifact.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=after_artifact.artifact[
                "artifact_revision"
            ],
            operation_id="source-gate-after-artifact-revoke-control",
            reason="Invalidate a completed static result.",
        )
        blocked = after_artifact.source_gate.get(
            check_id=after_artifact_result["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            blocked["effective_status"]
            in {
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
                "SOURCE_CHECK_INDETERMINATE",
            }
            and blocked["promotion_authorized"] is False,
            "Artifact revoke blocks an existing source-check result",
            blocked,
        )

        after_expiry = prepare_context(
            root / "after-expiry",
            extension_id="example.source_gate_after_expiry",
            operation_prefix="source-gate-after-expiry",
            artifact_expires_at=BASE_TIME + timedelta(seconds=1),
        )
        after_expiry_result = start_check(
            after_expiry,
            "source-gate-before-expiry",
        )
        after_expiry.clock.current = BASE_TIME + timedelta(seconds=1)
        expired_result = after_expiry.source_gate.get(
            check_id=after_expiry_result["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            expired_result["effective_status"]
            in {
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
                "SOURCE_CHECK_INDETERMINATE",
            }
            and expired_result["promotion_authorized"] is False,
            "Artifact expiry blocks an existing source-check result",
            expired_result,
        )


def tamper_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-tamper-"
    ) as raw:
        root = Path(raw)

        corrupt_state = prepare_context(
            root / "corrupt-state",
            extension_id="example.source_gate_corrupt_state",
            operation_prefix="source-gate-corrupt-state",
        )
        corrupt_result = start_check(
            corrupt_state,
            "source-gate-corrupt-state-start",
        )
        corrupt_state.store.path_for(
            SOURCE_CHECK_STATE_FILE
        ).write_text("{invalid-source-check-state", encoding="utf-8")
        status = corrupt_state.source_gate.status()
        expect(
            status["operational_health"] == "degraded",
            "corrupt source-check state degrades only the source gate",
            status,
        )
        expect_raises(
            (ExtensionSourceCheckStorageError,),
            lambda: corrupt_state.source_gate.get(
                check_id=corrupt_result["check_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "corrupt source-check state raises local StorageError",
        )
        terminal = corrupt_state.artifact_runtime.revoke(
            artifact_id=corrupt_state.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=corrupt_state.artifact[
                "artifact_revision"
            ],
            operation_id="source-gate-corrupt-state-artifact-revoke",
            reason="Artifact safety control remains independent.",
        )
        expect(
            terminal["stage"] == "ARTIFACT_REVOKED",
            "source-check state corruption does not block artifact revoke",
            terminal,
        )

        report_tamper = prepare_context(
            root / "report-tamper",
            extension_id="example.source_gate_report_tamper",
            operation_prefix="source-gate-report-tamper",
        )
        report_result = start_check(
            report_tamper,
            "source-gate-report-tamper-start",
        )

        def mutate_report(state: dict[str, Any]) -> None:
            state["checks"][report_result["check_id"]][
                "report_digest"
            ] = "f" * 64

        report_tamper.store.mutate_json(
            SOURCE_CHECK_STATE_FILE,
            mutate_report,
        )
        expect_raises(
            (ExtensionSourceCheckStorageError,),
            lambda: report_tamper.source_gate.integrity(
                check_id=report_result["check_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "report digest tamper raises local StorageError",
        )

        blob_tamper = prepare_context(
            root / "blob-tamper",
            extension_id="example.source_gate_blob_tamper",
            operation_prefix="source-gate-blob-tamper",
        )
        blob_result = start_check(
            blob_tamper,
            "source-gate-blob-tamper-start",
        )
        blob_path = private_blob_path(blob_tamper)
        content = bytearray(blob_path.read_bytes())
        content[0] ^= 1
        blob_path.write_bytes(bytes(content))
        expect_raises(
            (
                ExtensionSourceCheckStorageError,
                ExtensionArtifactStorageError,
            ),
            lambda: blob_tamper.source_gate.integrity(
                check_id=blob_result["check_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            ),
            "artifact blob tamper raises local StorageError",
        )

        expect_raises(
            (ExtensionSourceCheckNotFoundError,),
            lambda: blob_tamper.source_gate.get(
                check_id=blob_result["check_id"],
                user_id="another-user",
                workspace_id=WORKSPACE,
            ),
            "cross-owner source-check read is indistinguishable from missing",
        )


def owner_and_operation_binding_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-owner-operation-"
    ) as raw:
        root = Path(raw)
        workspace = prepare_context(
            root / "workspace",
            extension_id="example.source_gate_workspace",
            operation_prefix="source-gate-workspace",
        )
        checked = start_check(
            workspace,
            "source-gate-workspace-start",
        )
        workspace.store.mutate_json(
            "local_world.json",
            lambda state: {
                **state,
                "current_project": "/private/veyra/another-project",
            },
        )
        for label, call in (
            (
                "detail",
                lambda: workspace.source_gate.get(
                    check_id=checked["check_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                ),
            ),
            (
                "list",
                lambda: workspace.source_gate.list(
                    user_id=USER,
                    workspace_id=WORKSPACE,
                ),
            ),
            (
                "integrity",
                lambda: workspace.source_gate.integrity(
                    check_id=checked["check_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                ),
            ),
            (
                "operation replay",
                lambda: start_check(
                    workspace,
                    "source-gate-workspace-start",
                ),
            ),
        ):
            expect_raises(
                (ExtensionSourceCheckConflictError,),
                call,
                f"current-workspace switch blocks source-check {label}",
            )

        shared_root = root / "operation-redirect"
        first = prepare_context(
            shared_root,
            extension_id="example.source_gate_redirect_a",
            operation_prefix="source-gate-redirect-a",
        )
        first_result = start_check(
            first,
            "source-gate-redirect-operation",
        )
        second = prepare_context(
            shared_root,
            extension_id="example.source_gate_redirect_b",
            operation_prefix="source-gate-redirect-b",
        )
        owner_scope = artifact_owner_scope_digest(USER, WORKSPACE)
        operation_key = first.source_gate._operation_key(
            owner_scope,
            "source-gate-redirect-operation",
        )
        redirected_digest = first.source_gate._request_digest(
            owner_scope,
            second.artifact["artifact_id"],
            second.artifact["artifact_revision"],
            second.artifact["artifact_sha256"],
        )

        def redirect_operation(state: dict[str, Any]) -> None:
            operation = state["operation_index"][operation_key]
            expect(
                operation["check_id"] == first_result["check_id"],
                "redirect fixture points to the first source check",
                operation,
            )
            operation["request_digest"] = redirected_digest

        first.store.mutate_json(
            SOURCE_CHECK_STATE_FILE,
            redirect_operation,
        )
        state_before = first.store.path_for(
            SOURCE_CHECK_STATE_FILE
        ).read_bytes()
        audit_before = action_bytes(first.store)
        expect_raises(
            (ExtensionSourceCheckStorageError,),
            lambda: start_check(
                second,
                "source-gate-redirect-operation",
            ),
            "operation request digest cannot redirect replay to another artifact",
        )
        expect(
            second.checker.calls == 0
            and first.store.path_for(
                SOURCE_CHECK_STATE_FILE
            ).read_bytes()
            == state_before
            and action_bytes(first.store) == audit_before,
            "operation redirect tamper fails without checker, state, or audit effects",
        )


def claimed_crash_lifecycle() -> None:
    with TemporaryDirectory(
        prefix="veyra-phase6-source-gate-claimed-crash-"
    ) as raw:
        crash_checker = CrashChecker()
        context = prepare_context(
            Path(raw),
            extension_id="example.source_gate_claimed_crash",
            operation_prefix="source-gate-claimed-crash",
            checker=crash_checker,
        )
        expect_raises(
            (KeyboardInterrupt,),
            lambda: start_check(
                context,
                "source-gate-claimed-crash-start",
            ),
            "process interruption leaves a durable claimed check",
        )
        state = context.store.read_json(SOURCE_CHECK_STATE_FILE)
        check_id = next(iter(state["checks"]))
        expect(
            state["checks"][check_id]["stage"]
            == "SOURCE_CHECK_CLAIMED"
            and crash_checker.calls == 1,
            "interrupted checker cannot persist a passed or failed result",
            state["checks"][check_id],
        )

        restarted_store = WorldStateStore(context.root)
        restarted_spec = ExtensionSpecQuarantine(
            state_store=restarted_store,
            now=context.clock,
        )
        restarted_artifact = ExtensionArtifactQuarantine(
            state_store=restarted_store,
            spec_quarantine=restarted_spec,
            now=context.clock,
        )
        restarted_checker = CountingChecker()
        restarted_gate = ExtensionSourcePolicyGate(
            state_store=restarted_store,
            artifact_quarantine=restarted_artifact,
            checker=restarted_checker,
            now=context.clock,
        )
        detail = restarted_gate.get(
            check_id=check_id,
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        replay = restarted_gate.start(
            artifact_id=context.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_artifact_revision=context.artifact[
                "artifact_revision"
            ],
            expected_artifact_sha256=context.artifact[
                "artifact_sha256"
            ],
            operation_id="source-gate-claimed-crash-start",
        )
        expect(
            detail["stored_stage"] == "SOURCE_CHECK_CLAIMED"
            and detail["effective_status"]
            == "SOURCE_CHECK_INDETERMINATE"
            and detail["source_check_status"] == "indeterminate"
            and detail["operational_health"] == "degraded"
            and replay["operation_replayed"] is True
            and replay["effective_status"]
            == "SOURCE_CHECK_INDETERMINATE"
            and restarted_checker.calls == 0,
            "restart keeps an interrupted claim indeterminate without auto-replay",
            {"detail": detail, "replay": replay},
        )
        assert_zero_dynamic_authority(
            detail,
            "interrupted claim grants no dynamic or candidate authority",
        )


def main() -> int:
    main_lifecycle()
    malicious_sources()
    concurrency_lifecycle()
    prerequisite_lifecycle()
    tamper_lifecycle()
    owner_and_operation_binding_lifecycle()
    claimed_crash_lifecycle()
    print("phase6 extension source gate lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
