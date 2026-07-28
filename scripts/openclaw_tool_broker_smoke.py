from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.verifier import Verifier
from core.world_state import WorldStateStore
from interface.agent_adapter import ExecutionResult
from interface.event_schema import VeyraTaskPacket
from routers.tool_governance import build_tool_governance_router
from runtime.foresight_runtime import ForesightRuntime
from runtime.openclaw_tool_broker import (
    HOOK_STATE_FILE,
    HOOK_PLUGIN_IMPLEMENTATION_REVISION,
    HOOK_PLUGIN_PROTOCOL,
    OpenClawHookConflict,
    OpenClawHookDenied,
    OpenClawToolBroker,
    OpenClawToolBrokerError,
    project_current_hook_enforcement,
)
from runtime.tool_governance_runtime import ToolGovernanceRuntime
from tool_proxy.governance_contract import canonical_sha256


def expect(condition: bool, message: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {details!r}")


def packet(task_id: str) -> VeyraTaskPacket:
    return VeyraTaskPacket(
        task_id=task_id,
        target_agent="openclaw",
        session_id="dialogue-local",
        user_message="Run the governed sandbox canary.",
        user_goal="Run the governed sandbox canary.",
        context_patch={},
        persona_patch={},
        policy_patch={},
        agent_execution_session_id=f"agent-exec:{task_id}",
        governance_context={
            "user_id": "user_local",
            "workspace_id": str(ROOT),
            "channel_id": "api",
            "case_id": f"case_{task_id}",
            "step_id": f"step_{task_id}",
        },
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-hook-broker-") as raw_root:
        root = Path(raw_root)
        now = [datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)]
        clock = lambda: now[0]
        store = WorldStateStore(root / "state")
        governance = ToolGovernanceRuntime(store, clock=clock)
        foresight = ForesightRuntime(
            store,
            tool_receipt_resolver=governance.resolve_receipt,
            now=clock,
        )
        broker = OpenClawToolBroker(
            store,
            governance,
            sandbox_base=root / "sandboxes",
            foresight_runtime=foresight,
            clock=clock,
        )
        registration = broker.prepare_dispatch(
            packet("task_canary"),
            run_id="veyra-run-canary",
            session_key="agent-exec:task_canary",
        )
        replayed_registration = broker.prepare_dispatch(
            packet("task_canary"),
            run_id="veyra-run-canary",
            session_key="agent-exec:task_canary",
        )
        hook_state = store.read_json("openclaw_tool_hook_state.json")
        expect(
            replayed_registration == registration
            and hook_state.get("metrics", {}).get(
                "dispatch_registered"
            )
            == 1
            and registration.dispatch_token
            not in json.dumps(hook_state, sort_keys=True),
            "same-process governed dispatch retry is idempotent without persisting bearer",
            {
                "first": registration,
                "replayed": replayed_registration,
                "metrics": hook_state.get("metrics"),
            },
        )
        absent = broker.cancel_registered_run(
            "veyra-run-never-registered",
            reason="prepared_before_broker_registration",
        )
        expect(
            absent["status"] == "absent"
            and absent["authority_revoked"] is True
            and absent["executing_reservations"] == [],
            "validated broker ledger proves an unregistered run had no authority",
            absent,
        )

        partial_run = "veyra-run-partial-registration"
        original_mutate_json = store.mutate_json
        fail_hook_commit = {"armed": True}

        def crash_after_governance_registration(
            name: str,
            mutator: object,
        ) -> dict[str, object]:
            if name == HOOK_STATE_FILE and fail_hook_commit["armed"]:
                fail_hook_commit["armed"] = False
                raise RuntimeError(
                    "simulated crash before hook dispatch commit"
                )
            return original_mutate_json(name, mutator)  # type: ignore[arg-type,return-value]

        store.mutate_json = crash_after_governance_registration  # type: ignore[method-assign]
        try:
            broker.prepare_dispatch(
                packet("task_partial"),
                run_id=partial_run,
                session_key="agent-exec:task_partial",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "fault injection did not interrupt hook dispatch commit"
            )
        finally:
            store.mutate_json = original_mutate_json  # type: ignore[method-assign]

        partial_before = [
            entry
            for entry in store.read_json(
                "tool_governance_state.json"
            ).get("sessions", {}).values()
            if isinstance(entry, dict)
            and isinstance(entry.get("binding"), dict)
            and entry["binding"].get("run_id") == partial_run
            and entry.get("status") == "active"
        ]
        partial_cancel = broker.cancel_registered_run(
            partial_run,
            reason="reconcile_partial_registration",
        )
        partial_after = [
            entry
            for entry in store.read_json(
                "tool_governance_state.json"
            ).get("sessions", {}).values()
            if isinstance(entry, dict)
            and isinstance(entry.get("binding"), dict)
            and entry["binding"].get("run_id") == partial_run
            and entry.get("status") == "active"
        ]
        expect(
            len(partial_before) == 1
            and partial_cancel["status"] == "absent"
            and partial_cancel["authority_revoked"] is True
            and partial_after == [],
            "absent hook dispatch cancellation closes partial governance session",
            {
                "before": partial_before,
                "cancellation": partial_cancel,
                "after": partial_after,
            },
        )
        try:
            broker.prepare_dispatch(
                packet("task_partial"),
                run_id=partial_run,
                session_key="agent-exec:task_partial",
            )
        except OpenClawHookConflict:
            pass
        else:
            raise AssertionError(
                "cancelled partial governance authority was reactivated"
            )

        allowed = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_write",
            tool_name="veyra_file_write",
            params={"path": "sentinel.txt", "content": "phase3-ok"},
            dispatch_token=registration.dispatch_token,
        )
        expect(allowed["allow"] is True, "sandbox write must reserve", allowed)
        replay_counts_before = {
            "assessments": foresight.status()["assessment_count"],
            "grants": len(
                store.read_json("tool_governance_state.json")["grants"]
            ),
            "calls": len(
                store.read_json("tool_governance_state.json")["calls"]
            ),
        }
        replay_results = [
            broker.preflight(
                run_id=registration.run_id,
                session_key=registration.session_key,
                tool_call_id="call_write",
                tool_name="veyra_file_write",
                params={
                    "path": "sentinel.txt",
                    "content": f"replay-{index}",
                },
                dispatch_token=registration.dispatch_token,
            )
            for index in range(16)
        ]
        replay_counts_after = {
            "assessments": foresight.status()["assessment_count"],
            "grants": len(
                store.read_json("tool_governance_state.json")["grants"]
            ),
            "calls": len(
                store.read_json("tool_governance_state.json")["calls"]
            ),
        }
        expect(
            all(
                item["allow"] is False
                and item["reason"] == "tool call replay"
                for item in replay_results
            )
            and replay_counts_after == replay_counts_before,
            "same-call param drift is rejected before Foresight or grant admission",
            {
                "before": replay_counts_before,
                "after": replay_counts_after,
                "results": replay_results,
            },
        )
        executed = broker.execute(
            run_id=registration.run_id,
            tool_call_id="call_write",
            tool_name="veyra_file_write",
            params={"path": "sentinel.txt", "content": "phase3-ok"},
            dispatch_token=registration.dispatch_token,
            reservation_token=allowed["reservationToken"],
            execution_token=allowed["executionToken"],
        )
        expect(executed["status"] == "ok", "sandbox write must execute", executed)
        expect(
            executed.get("foresight", {}).get("status") == "exact"
            and executed.get("foresight", {}).get("promotion_status")
            == "eligible"
            and executed.get("foresight", {}).get("eligibility_only") is True
            and executed.get("foresight", {}).get("promotion_applied") is False
            and foresight.status().get("residual_counts", {}).get("exact") == 1,
            "governed sandbox effect must close its pre-registered exact prediction without promotion",
            executed.get("foresight"),
        )
        sentinel = Path(registration.sandbox_root) / "sentinel.txt"
        expect(
            sentinel.read_text(encoding="utf-8") == "phase3-ok",
            "sandbox sentinel content mismatch",
        )
        evidence = governance.run_evidence(registration.run_id)
        expect(
            evidence["tool_calls"] == ["file.write"]
            and evidence["changed_files"] == [str(sentinel)],
            "authoritative run projection mismatch",
            evidence,
        )
        verifier = Verifier(governance.resolve_receipt)
        verified = verifier.verify_execution_result(
            ExecutionResult(
                task_id=registration.run_id,
                executor="openclaw",
                status="success",
                result="done",
                tool_calls=evidence["tool_calls"],
                changed_files=evidence["changed_files"],
                raw={"tool_receipt_refs": evidence["tool_receipt_refs"]},
            )
        )
        expect(
            verified["status"] == "verified_success",
            "verified effect must satisfy Verifier",
            verified,
        )

        try:
            broker.execute(
                run_id=registration.run_id,
                tool_call_id="call_write",
                tool_name="veyra_file_write",
                params={"path": "sentinel.txt", "content": "phase3-ok"},
                dispatch_token=registration.dispatch_token,
                reservation_token=allowed["reservationToken"],
                execution_token=allowed["executionToken"],
            )
        except Exception:
            pass
        else:
            raise AssertionError("execution token replay must fail")

        outside = root / "outside.txt"
        escaped = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_escape",
            tool_name="veyra_file_write",
            params={"path": "../outside.txt", "content": "escape"},
            dispatch_token=registration.dispatch_token,
        )
        expect(escaped["allow"] is False, "parent escape must be blocked", escaped)
        expect(not outside.exists(), "blocked escape created an outside file")

        tamper = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_tamper",
            tool_name="veyra_file_write",
            params={"path": "tamper.txt", "content": "original"},
            dispatch_token=registration.dispatch_token,
        )
        expect(tamper["allow"] is True, "tamper setup must reserve", tamper)
        try:
            broker.execute(
                run_id=registration.run_id,
                tool_call_id="call_tamper",
                tool_name="veyra_file_write",
                params={"path": "tamper.txt", "content": "changed"},
                dispatch_token=registration.dispatch_token,
                reservation_token=tamper["reservationToken"],
                execution_token=tamper["executionToken"],
            )
        except OpenClawHookDenied:
            pass
        else:
            raise AssertionError("changed params must fail before execution")
        expect(
            not (Path(registration.sandbox_root) / "tamper.txt").exists(),
            "param tamper caused a side effect",
        )

        failure_reserved = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_executor_failure",
            tool_name="veyra_file_write",
            params={"path": "failure.txt", "content": "never"},
            dispatch_token=registration.dispatch_token,
        )
        expect(
            failure_reserved["allow"] is True,
            "executor failure setup must reserve",
            failure_reserved,
        )
        indeterminate_before = foresight.status()[
            "residual_counts"
        ]["indeterminate"]
        original_execute_tool = broker._execute_tool

        def fail_executor(**_kwargs: object) -> dict[str, object]:
            raise RuntimeError("deterministic executor failure")

        broker._execute_tool = fail_executor  # type: ignore[method-assign]
        try:
            broker.execute(
                run_id=registration.run_id,
                tool_call_id="call_executor_failure",
                tool_name="veyra_file_write",
                params={"path": "failure.txt", "content": "never"},
                dispatch_token=registration.dispatch_token,
                reservation_token=(
                    failure_reserved["reservationToken"]
                ),
                execution_token=failure_reserved["executionToken"],
            )
        except OpenClawToolBrokerError:
            pass
        else:
            raise AssertionError(
                "deterministic executor failure was not surfaced"
            )
        finally:
            broker._execute_tool = original_execute_tool  # type: ignore[method-assign]
        failed_receipt = governance.resolve_receipt(
            registration.run_id,
            "call_executor_failure",
        )
        expect(
            isinstance(failed_receipt, dict)
            and failed_receipt.get("ledger_state")
            == "observed_failure"
            and foresight.status()["residual_counts"]["indeterminate"]
            == indeterminate_before + 1
            and not (
                Path(registration.sandbox_root) / "failure.txt"
            ).exists(),
            "authoritative executor failure closes Foresight without masking the error",
            {
                "receipt": failed_receipt,
                "foresight": foresight.status(),
            },
        )

        revoked = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_revoked",
            tool_name="veyra_file_write",
            params={"path": "revoked.txt", "content": "no"},
            dispatch_token=registration.dispatch_token,
        )
        expect(revoked["allow"] is True, "revoke setup must reserve", revoked)
        hook_state = store.read_json("openclaw_tool_hook_state.json")
        revoked_attempt_id = hook_state["call_index"][
            broker._call_key(registration.run_id, "call_revoked")
        ]
        revoked_grant = hook_state["attempts"][revoked_attempt_id]["grant_id"]
        governance.revoke_grant(revoked_grant, reason="smoke_revoke")
        try:
            broker.execute(
                run_id=registration.run_id,
                tool_call_id="call_revoked",
                tool_name="veyra_file_write",
                params={"path": "revoked.txt", "content": "no"},
                dispatch_token=registration.dispatch_token,
                reservation_token=revoked["reservationToken"],
                execution_token=revoked["executionToken"],
            )
        except Exception:
            pass
        else:
            raise AssertionError("revoked grant must fail at execution time")
        expect(
            not (Path(registration.sandbox_root) / "revoked.txt").exists(),
            "revoked grant caused a side effect",
        )

        expiring = broker.preflight(
            run_id=registration.run_id,
            session_key=registration.session_key,
            tool_call_id="call_expired",
            tool_name="veyra_file_write",
            params={"path": "expired.txt", "content": "no"},
            dispatch_token=registration.dispatch_token,
        )
        expect(expiring["allow"] is True, "expiry setup must reserve", expiring)
        now[0] += timedelta(minutes=6)
        try:
            broker.execute(
                run_id=registration.run_id,
                tool_call_id="call_expired",
                tool_name="veyra_file_write",
                params={"path": "expired.txt", "content": "no"},
                dispatch_token=registration.dispatch_token,
                reservation_token=expiring["reservationToken"],
                execution_token=expiring["executionToken"],
            )
        except Exception:
            pass
        else:
            raise AssertionError("expired grant must fail at execution time")
        expect(
            not (Path(registration.sandbox_root) / "expired.txt").exists(),
            "expired grant caused a side effect",
        )

        native_block_path = (
            broker.native_canary_root / "phase3-smoke-native.txt"
        )
        native_block_content = "must-not-be-written"
        native_block_params = {
            "path": str(native_block_path),
            "content": native_block_content,
        }
        plugin_block = broker.observe(
            run_id=registration.run_id,
            tool_call_id="call_native_write",
            tool_name="write",
            dispatch_token=registration.dispatch_token,
            outcome="blocked",
            params_digest=canonical_sha256(native_block_params),
            reason="native tool blocked for governed run",
        )
        expect(
            plugin_block["status"] == "recorded",
            "native block observation was not recorded",
            plugin_block,
        )

        diagnostic_registration = broker.prepare_dispatch(
            packet("task_diagnostic_block"),
            run_id="veyra-run-diagnostic-block",
            session_key="agent-exec:task_diagnostic_block",
        )
        diagnostic_write = broker.preflight(
            run_id=diagnostic_registration.run_id,
            session_key=diagnostic_registration.session_key,
            tool_call_id="call_diagnostic_write",
            tool_name="veyra_file_write",
            params={"path": "diagnostic.txt", "content": "not-a-canary"},
            dispatch_token=diagnostic_registration.dispatch_token,
        )
        broker.execute(
            run_id=diagnostic_registration.run_id,
            tool_call_id="call_diagnostic_write",
            tool_name="veyra_file_write",
            params={"path": "diagnostic.txt", "content": "not-a-canary"},
            dispatch_token=diagnostic_registration.dispatch_token,
            reservation_token=diagnostic_write["reservationToken"],
            execution_token=diagnostic_write["executionToken"],
        )
        diagnostic_native_path = (
            broker.native_canary_root / "phase3-diagnostic-native.txt"
        )
        diagnostic_native_content = "diagnostic-must-not-be-written"
        broker.observe(
            run_id=diagnostic_registration.run_id,
            tool_call_id="call_diagnostic_native",
            tool_name="write",
            dispatch_token=diagnostic_registration.dispatch_token,
            outcome="blocked",
            params_digest=canonical_sha256(
                {
                    "path": str(diagnostic_native_path),
                    "content": diagnostic_native_content,
                }
            ),
            reason="caller-reported native block",
        )
        try:
            broker.mark_canary_validated(
                run_id=diagnostic_registration.run_id,
                session_key=diagnostic_registration.session_key,
                sentinel_relative_path="diagnostic.txt",
                native_block_path=str(diagnostic_native_path),
                native_block_content=diagnostic_native_content,
                plugin_protocol=HOOK_PLUGIN_PROTOCOL,
                plugin_implementation_revision=(
                    HOOK_PLUGIN_IMPLEMENTATION_REVISION
                ),
                dispatch_token=diagnostic_registration.dispatch_token,
            )
        except OpenClawHookDenied:
            pass
        else:
            raise AssertionError(
                "non-authoritative plugin block must not validate a canary"
            )

        try:
            broker.mark_canary_validated(
                run_id=registration.run_id,
                session_key="agent-exec:wrong-canary-session",
                sentinel_relative_path="sentinel.txt",
                native_block_path=str(native_block_path),
                native_block_content=native_block_content,
                plugin_protocol=HOOK_PLUGIN_PROTOCOL,
                plugin_implementation_revision=(
                    HOOK_PLUGIN_IMPLEMENTATION_REVISION
                ),
                dispatch_token=registration.dispatch_token,
            )
        except OpenClawHookDenied:
            pass
        else:
            raise AssertionError(
                "canary attestation accepted another OpenClaw session"
            )

        attestation = broker.mark_canary_validated(
            run_id=registration.run_id,
            session_key=registration.session_key,
            sentinel_relative_path="sentinel.txt",
            native_block_path=str(native_block_path),
            native_block_content=native_block_content,
            plugin_protocol=HOOK_PLUGIN_PROTOCOL,
            plugin_implementation_revision=(
                HOOK_PLUGIN_IMPLEMENTATION_REVISION
            ),
            dispatch_token=registration.dispatch_token,
        )
        plugin_status = {
            "connected": True,
            "capabilities": {
                "raw": {
                    "governance_plugin": {
                        "status": "active",
                        "protocol_version": HOOK_PLUGIN_PROTOCOL,
                        "implementation_revision": (
                            HOOK_PLUGIN_IMPLEMENTATION_REVISION
                        ),
                    }
                }
            },
        }
        broker_snapshot = broker.status()
        current_status = project_current_hook_enforcement(
            broker_snapshot,
            plugin_status,
        )
        expect(
            attestation["status"] == "validated"
            and broker_snapshot["canary_validated"] is True
            and broker_snapshot["tool_proxy_enforced"] is False
            and current_status["tool_proxy_enforced"] is True
            and project_current_hook_enforcement(
                broker_snapshot,
                {"connected": False},
            )["tool_proxy_enforced"]
            is False,
            "authoritative canary attestation failed",
            attestation,
        )

        app = FastAPI()
        app.include_router(
            build_tool_governance_router(
                governance,
                broker,
                plugin_status_resolver=lambda: plugin_status,
            )
        )
        client = TestClient(app)
        missing_header = client.post(
            "/tool-governance/hook/preflight",
            json={
                "runId": registration.run_id,
                "sessionKey": registration.session_key,
                "toolCallId": "call_http_missing_header",
                "toolName": "veyra_file_read",
                "params": {"path": "sentinel.txt"},
            },
        )
        expect(
            missing_header.status_code == 422,
            "hook bearer must be required in a header",
            missing_header.text,
        )
        missing_attestation_header = client.post(
            "/tool-governance/hook/canary/attest",
            json={
                "runId": registration.run_id,
                "sessionKey": registration.session_key,
                "sentinelRelativePath": "sentinel.txt",
                "nativeBlockPath": str(native_block_path),
                "nativeBlockContent": native_block_content,
                "pluginProtocol": HOOK_PLUGIN_PROTOCOL,
                "pluginImplementationRevision": (
                    HOOK_PLUGIN_IMPLEMENTATION_REVISION
                ),
            },
        )
        expect(
            missing_attestation_header.status_code == 422,
            "canary attestation must require the dispatch bearer header",
            missing_attestation_header.text,
        )
        missing_attestation_session = client.post(
            "/tool-governance/hook/canary/attest",
            headers={
                "X-Veyra-Dispatch-Token": registration.dispatch_token,
            },
            json={
                "runId": registration.run_id,
                "sentinelRelativePath": "sentinel.txt",
                "nativeBlockPath": str(native_block_path),
                "nativeBlockContent": native_block_content,
                "pluginProtocol": HOOK_PLUGIN_PROTOCOL,
                "pluginImplementationRevision": (
                    HOOK_PLUGIN_IMPLEMENTATION_REVISION
                ),
            },
        )
        expect(
            missing_attestation_session.status_code == 422,
            "canary attestation must require sessionKey",
            missing_attestation_session.text,
        )
        mismatched_attestation_session = client.post(
            "/tool-governance/hook/canary/attest",
            headers={
                "X-Veyra-Dispatch-Token": registration.dispatch_token,
            },
            json={
                "runId": registration.run_id,
                "sessionKey": "agent-exec:wrong-canary-session",
                "sentinelRelativePath": "sentinel.txt",
                "nativeBlockPath": str(native_block_path),
                "nativeBlockContent": native_block_content,
                "pluginProtocol": HOOK_PLUGIN_PROTOCOL,
                "pluginImplementationRevision": (
                    HOOK_PLUGIN_IMPLEMENTATION_REVISION
                ),
            },
        )
        expect(
            mismatched_attestation_session.status_code == 409,
            "canary attestation must match the registered sessionKey",
            mismatched_attestation_session.text,
        )
        http_allowed = client.post(
            "/tool-governance/hook/preflight",
            headers={
                "X-Veyra-Dispatch-Token": registration.dispatch_token,
            },
            json={
                "runId": registration.run_id,
                "sessionKey": registration.session_key,
                "toolCallId": "call_http_read",
                "toolName": "veyra_file_read",
                "params": {"path": "sentinel.txt"},
            },
        )
        expect(
            http_allowed.status_code == 200
            and http_allowed.json().get("allow") is True,
            "authenticated hook HTTP preflight failed",
            http_allowed.text,
        )

        serialized = json.dumps(
            {
                "hook": store.read_json("openclaw_tool_hook_state.json"),
                "ledger": store.read_json("tool_governance_state.json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for secret in (
            registration.dispatch_token,
            allowed["reservationToken"],
            allowed["executionToken"],
        ):
            expect(secret not in serialized, "raw bearer leaked into state")

        cancelled_registration = broker.prepare_dispatch(
            packet("task_cancel"),
            run_id="veyra-run-cancel",
            session_key="agent-exec:task_cancel",
        )
        cancel_reserved = broker.preflight(
            run_id=cancelled_registration.run_id,
            session_key=cancelled_registration.session_key,
            tool_call_id="call_cancel",
            tool_name="veyra_file_write",
            params={"path": "cancelled.txt", "content": "no"},
            dispatch_token=cancelled_registration.dispatch_token,
        )
        broker.cancel_dispatch(
            run_id=cancelled_registration.run_id,
            dispatch_token=cancelled_registration.dispatch_token,
        )
        try:
            broker.execute(
                run_id=cancelled_registration.run_id,
                tool_call_id="call_cancel",
                tool_name="veyra_file_write",
                params={"path": "cancelled.txt", "content": "no"},
                dispatch_token=cancelled_registration.dispatch_token,
                reservation_token=cancel_reserved["reservationToken"],
                execution_token=cancel_reserved["executionToken"],
            )
        except Exception:
            pass
        else:
            raise AssertionError("cancelled dispatch must fail closed")

        reserve_bind_registration = broker.prepare_dispatch(
            packet("task_cancel_between_reserve_bind"),
            run_id="veyra-run-cancel-between-reserve-bind",
            session_key="agent-exec:task_cancel_between_reserve_bind",
        )
        original_bind = governance.bind_scoped_execution_token
        bind_entered = threading.Event()
        release_bind = threading.Event()
        reserve_bind_results: list[dict[str, object]] = []

        def bind_after_cancel(**kwargs: object) -> None:
            bind_entered.set()
            release_bind.wait(timeout=5)
            original_bind(**kwargs)

        governance.bind_scoped_execution_token = bind_after_cancel  # type: ignore[method-assign]

        def preflight_across_reserve_bind() -> None:
            reserve_bind_results.append(
                broker.preflight(
                    run_id=reserve_bind_registration.run_id,
                    session_key=reserve_bind_registration.session_key,
                    tool_call_id="call_cancel_between_reserve_bind",
                    tool_name="veyra_file_write",
                    params={"path": "reserve-bind.txt", "content": "no"},
                    dispatch_token=reserve_bind_registration.dispatch_token,
                )
            )

        reserve_bind_thread = threading.Thread(
            target=preflight_across_reserve_bind,
            name="reserve-bind-preflight",
        )
        reserve_bind_thread.start()
        expect(
            bind_entered.wait(timeout=5),
            "reserve-bind race did not reach the binding barrier",
        )
        reserve_bind_cancel = broker.cancel_registered_run(
            reserve_bind_registration.run_id,
            reason="cancel between reservation and executor binding",
        )
        release_bind.set()
        reserve_bind_thread.join(timeout=5)
        governance.bind_scoped_execution_token = original_bind  # type: ignore[method-assign]
        reserve_bind_hook = store.read_json("openclaw_tool_hook_state.json")
        reserve_bind_attempts = [
            item
            for item in reserve_bind_hook["attempts"].values()
            if isinstance(item, dict)
            and item.get("run_id") == reserve_bind_registration.run_id
        ]
        expect(
            reserve_bind_cancel["status"] == "cancelled"
            and len(reserve_bind_results) == 1
            and reserve_bind_results[0].get("allow") is False
            and not any(
                item.get("status") == "reserved"
                for item in reserve_bind_attempts
            ),
            "cancel between reserve and bind must prevent an allowed preflight",
            {
                "cancellation": reserve_bind_cancel,
                "preflight": reserve_bind_results,
                "attempts": reserve_bind_attempts,
            },
        )

        bind_persist_registration = broker.prepare_dispatch(
            packet("task_cancel_between_bind_persist"),
            run_id="veyra-run-cancel-between-bind-persist",
            session_key="agent-exec:task_cancel_between_bind_persist",
        )
        original_mutate_json = store.mutate_json
        persist_entered = threading.Event()
        release_persist = threading.Event()
        persist_paused = [False]
        bind_persist_results: list[dict[str, object]] = []

        def pause_broker_persist(name, mutator):
            if (
                threading.current_thread().name == "bind-persist-preflight"
                and name == "openclaw_tool_hook_state.json"
                and not persist_paused[0]
            ):
                persist_paused[0] = True
                persist_entered.set()
                release_persist.wait(timeout=5)
            return original_mutate_json(name, mutator)

        store.mutate_json = pause_broker_persist  # type: ignore[method-assign]

        def preflight_across_bind_persist() -> None:
            bind_persist_results.append(
                broker.preflight(
                    run_id=bind_persist_registration.run_id,
                    session_key=bind_persist_registration.session_key,
                    tool_call_id="call_cancel_between_bind_persist",
                    tool_name="veyra_file_write",
                    params={"path": "bind-persist.txt", "content": "no"},
                    dispatch_token=bind_persist_registration.dispatch_token,
                )
            )

        bind_persist_thread = threading.Thread(
            target=preflight_across_bind_persist,
            name="bind-persist-preflight",
        )
        bind_persist_thread.start()
        expect(
            persist_entered.wait(timeout=5),
            "bind-persist race did not reach the broker persistence barrier",
        )
        bind_persist_cancel = broker.cancel_registered_run(
            bind_persist_registration.run_id,
            reason="cancel between executor binding and broker persistence",
        )
        release_persist.set()
        bind_persist_thread.join(timeout=5)
        store.mutate_json = original_mutate_json  # type: ignore[method-assign]
        bind_persist_hook = store.read_json("openclaw_tool_hook_state.json")
        bind_persist_attempts = [
            item
            for item in bind_persist_hook["attempts"].values()
            if isinstance(item, dict)
            and item.get("run_id") == bind_persist_registration.run_id
        ]
        expect(
            bind_persist_cancel["status"] == "cancelled"
            and len(bind_persist_results) == 1
            and bind_persist_results[0].get("allow") is False
            and not any(
                item.get("status") == "reserved"
                for item in bind_persist_attempts
            ),
            "cancel between bind and broker persist must prevent an allowed preflight",
            {
                "cancellation": bind_persist_cancel,
                "preflight": bind_persist_results,
                "attempts": bind_persist_attempts,
            },
        )

        cancel_wins_registration = broker.prepare_dispatch(
            packet("task_cancel_wins"),
            run_id="veyra-run-cancel-wins",
            session_key="agent-exec:task_cancel_wins",
        )
        cancel_wins_reserved = broker.preflight(
            run_id=cancel_wins_registration.run_id,
            session_key=cancel_wins_registration.session_key,
            tool_call_id="call_cancel_wins",
            tool_name="veyra_file_write",
            params={"path": "cancel-wins.txt", "content": "no"},
            dispatch_token=cancel_wins_registration.dispatch_token,
        )
        original_claim = governance.claim_scoped_execution
        claim_entered = threading.Event()
        release_claim = threading.Event()
        cancel_wins_errors: list[Exception] = []

        def claim_after_cancel(**kwargs: object) -> object:
            claim_entered.set()
            release_claim.wait(timeout=5)
            return original_claim(**kwargs)

        governance.claim_scoped_execution = claim_after_cancel  # type: ignore[method-assign]

        def execute_after_cancel() -> None:
            try:
                broker.execute(
                    run_id=cancel_wins_registration.run_id,
                    tool_call_id="call_cancel_wins",
                    tool_name="veyra_file_write",
                    params={"path": "cancel-wins.txt", "content": "no"},
                    dispatch_token=cancel_wins_registration.dispatch_token,
                    reservation_token=cancel_wins_reserved["reservationToken"],
                    execution_token=cancel_wins_reserved["executionToken"],
                )
            except Exception as exc:
                cancel_wins_errors.append(exc)

        cancel_wins_thread = threading.Thread(target=execute_after_cancel)
        cancel_wins_thread.start()
        expect(
            claim_entered.wait(timeout=5),
            "cancel-wins race did not reach the claim barrier",
        )
        cancel_wins = broker.cancel_registered_run(
            cancel_wins_registration.run_id,
            reason="deterministic cancel-wins race",
        )
        release_claim.set()
        cancel_wins_thread.join(timeout=5)
        governance.claim_scoped_execution = original_claim  # type: ignore[method-assign]
        expect(
            cancel_wins["status"] == "cancelled"
            and len(cancel_wins_errors) == 1
            and not (
                Path(cancel_wins_registration.sandbox_root)
                / "cancel-wins.txt"
            ).exists(),
            "a cancellation that wins the authority ledger must prevent effects",
            {"cancellation": cancel_wins, "errors": cancel_wins_errors},
        )

        execute_wins_registration = broker.prepare_dispatch(
            packet("task_execute_wins"),
            run_id="veyra-run-execute-wins",
            session_key="agent-exec:task_execute_wins",
        )
        execute_wins_reserved = broker.preflight(
            run_id=execute_wins_registration.run_id,
            session_key=execute_wins_registration.session_key,
            tool_call_id="call_execute_wins",
            tool_name="veyra_file_write",
            params={"path": "execute-wins.txt", "content": "yes"},
            dispatch_token=execute_wins_registration.dispatch_token,
        )
        execution_claimed = threading.Event()
        release_execution = threading.Event()
        execute_wins_errors: list[Exception] = []

        def claim_before_cancel(**kwargs: object) -> object:
            receipt = original_claim(**kwargs)
            execution_claimed.set()
            release_execution.wait(timeout=5)
            return receipt

        governance.claim_scoped_execution = claim_before_cancel  # type: ignore[method-assign]

        def execute_before_cancel() -> None:
            try:
                broker.execute(
                    run_id=execute_wins_registration.run_id,
                    tool_call_id="call_execute_wins",
                    tool_name="veyra_file_write",
                    params={"path": "execute-wins.txt", "content": "yes"},
                    dispatch_token=execute_wins_registration.dispatch_token,
                    reservation_token=execute_wins_reserved["reservationToken"],
                    execution_token=execute_wins_reserved["executionToken"],
                )
            except Exception as exc:
                execute_wins_errors.append(exc)

        execute_wins_thread = threading.Thread(target=execute_before_cancel)
        execute_wins_thread.start()
        expect(
            execution_claimed.wait(timeout=5),
            "execute-wins race did not reach the post-claim barrier",
        )
        execute_wins_cancel = broker.cancel_registered_run(
            execute_wins_registration.run_id,
            reason="deterministic execute-wins race",
        )
        expect(
            execute_wins_cancel["status"] == "too_late",
            "cancellation must not report success after execution claims authority",
            execute_wins_cancel,
        )
        release_execution.set()
        execute_wins_thread.join(timeout=5)
        governance.claim_scoped_execution = original_claim  # type: ignore[method-assign]
        expect(
            not execute_wins_errors
            and (
                Path(execute_wins_registration.sandbox_root)
                / "execute-wins.txt"
            ).read_text(encoding="utf-8")
            == "yes",
            "the winning execution claim must complete without a false cancel",
            execute_wins_errors,
        )
        completed_cancel = broker.cancel_registered_run(
            execute_wins_registration.run_id,
            reason="finalize completed execute-wins race",
        )
        expect(
            completed_cancel["status"] == "cancelled",
            "a later cancellation closes the dispatch after execution completes",
            completed_cancel,
        )

        def inject_missing_reservation(current: dict[str, object]) -> None:
            metrics = current.get("metrics")
            if not isinstance(metrics, dict):
                raise AssertionError("broker metrics missing")
            metrics["started_without_reservation"] = 1

        broker.state_store.mutate_json(
            HOOK_STATE_FILE,
            inject_missing_reservation,
        )
        invariant_failure = broker.status()
        expect(
            invariant_failure["status"] == "degraded"
            and invariant_failure["safety_invariants_ok"] is False
            and invariant_failure["canary_validated"] is False
            and project_current_hook_enforcement(
                invariant_failure,
                plugin_status,
            )["tool_proxy_enforced"]
            is False,
            "a post-canary reservation invariant failure stayed enforced",
            invariant_failure,
        )

    print("openclaw_tool_broker_smoke: PASS")


if __name__ == "__main__":
    main()
