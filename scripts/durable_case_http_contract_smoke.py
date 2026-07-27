#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.durable_case import (  # noqa: E402
    CaseCheckpoint,
    CheckpointEffectState,
    DialogueMessageType,
    DialogueRecord,
)
from core.world_state import WorldStateStore  # noqa: E402
from routers.cases import build_cases_router  # noqa: E402
from routers.debug_audit import _public_state  # noqa: E402
from runtime.bounded_agent_negotiation import (  # noqa: E402
    BoundedNegotiationError,
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import (  # noqa: E402
    CaseRevisionConflictError,
    DurableCaseStore,
)


USER_ID = "user-a"
WORKSPACE_ID = "workspace-a"
PRIVATE_RUN_ID = "run-private-http-contract"
PRIVATE_SESSION_KEY = "agent:openclaw:private-http-contract"
PRIVATE_BINDING_DIGEST = "b" * 64
PRIVATE_CONTEXT_VALUE = "private-governance-context-http-contract"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def _checkpoint(
    *,
    checkpoint_id: str,
    phase: str,
    result_status: str,
    effect_state: CheckpointEffectState = CheckpointEffectState.NOT_STARTED,
    evidence_refs: list[str] | None = None,
) -> CaseCheckpoint:
    return CaseCheckpoint.model_validate(
        {
            "checkpoint_id": checkpoint_id,
            "phase": phase,
            "operation_id": f"dispatch:{checkpoint_id}",
            "step_id": "step-private-http-contract",
            "task_id": "task-private-http-contract",
            "run_id": PRIVATE_RUN_ID,
            "session_key": PRIVATE_SESSION_KEY,
            "binding_digest": PRIVATE_BINDING_DIGEST,
            "executor": "openclaw",
            "target_agent": "openclaw",
            "dialogue_message_id": "message-private-http-contract",
            "result_status": result_status,
            "effect_state": effect_state,
            "evidence_refs": list(evidence_refs or []),
            "recorded_at": datetime.now(timezone.utc),
        },
        strict=True,
    )


def _task_request(*, case_id: str, case_revision: int) -> DialogueRecord:
    message_id = "message-private-http-contract"
    turn_index = 1
    return DialogueRecord.model_validate(
        {
            "message_id": message_id,
            "message_type": DialogueMessageType.TASK_REQUEST,
            "sender": "veyra",
            "direction": "veyra_to_agent",
            "case_revision": case_revision,
            "turn_index": turn_index,
            "content": {
                "contract_version": "veyra.agent_dialogue.v1",
                "message_id": message_id,
                "message_type": "TASK_REQUEST",
                "sender": "veyra",
                "case_id": case_id,
                "case_revision": case_revision,
                "turn_index": turn_index,
                "task_packet_id": "task-private-http-contract",
                "operation_id": "dispatch-private-http-contract",
                "scope_digest": "a" * 64,
                "in_reply_to": None,
                "payload": {
                    "user_goal": "Compare two read-only recovery options.",
                    "constraints": ["analysis_only"],
                    "evidence_refs": [],
                    "authority": {
                        "mode": "read_only",
                        "side_effects_require_governance": True,
                        "capability_expansion_authorized": False,
                        "verification_authority": False,
                    },
                    "context": {
                        "governance_context": PRIVATE_CONTEXT_VALUE,
                        "runtime_run_id": PRIVATE_RUN_ID,
                        "session_key": PRIVATE_SESSION_KEY,
                        "binding_digest": PRIVATE_BINDING_DIGEST,
                    },
                },
            },
            "authority_granted": False,
            "evidence_verified": False,
            "recorded_at": datetime.now(timezone.utc),
        },
        strict=True,
    )


class FakeBoundedRuntime:
    """Small command-boundary fake; the real DurableCaseStore owns lifecycle."""

    def __init__(self, store: DurableCaseStore) -> None:
        self.store = store
        self.cancel_calls = 0
        self.reconcile_calls = 0

    @staticmethod
    def case_has_live_agent_authority(case: dict[str, Any]) -> bool:
        return BoundedNegotiationRuntime.case_has_live_agent_authority(case)

    def cancel_case(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self.cancel_calls += 1
        if reason == "raise-bounded-error":
            raise BoundedNegotiationError("bounded cancellation refusal")
        requested = self.store.request_cancel(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            reason=reason,
            checkpoint=CaseCheckpoint.model_validate(
                {
                    "checkpoint_id": f"cancel-request-{operation_id}",
                    "phase": "cancellation_requested",
                    "operation_id": operation_id,
                    "result_status": "cancellation_requested",
                    "effect_state": CheckpointEffectState.NOT_STARTED,
                    "recorded_at": datetime.now(timezone.utc),
                },
                strict=True,
            ),
        )
        if requested["status"] == "CANCELLED":
            completed = requested
            receipt_replayed = True
        else:
            completed = self.store.complete_cancel(
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_id=f"{operation_id}:complete",
                expected_revision=int(requested["revision"]),
                reason="fake runtime observed all authority revoked",
                checkpoint=CaseCheckpoint.model_validate(
                    {
                        "checkpoint_id": f"cancel-complete-{operation_id}",
                        "phase": "cancellation_confirmed",
                        "operation_id": operation_id,
                        "result_status": "cancelled",
                        "effect_state": CheckpointEffectState.OBSERVED,
                        "evidence_refs": ["cancel_receipt:http_contract"],
                        "recorded_at": datetime.now(timezone.utc),
                    },
                    strict=True,
                ),
            )
            receipt_replayed = False
        raw_receipt = {
            "status": "cancelled",
            "run_id": PRIVATE_RUN_ID,
            "authority_revoked": True,
            "plugin_authority_closed": True,
            "agent_abort_confirmed": True,
            "receipt_replayed": receipt_replayed,
            "session_key": PRIVATE_SESSION_KEY,
            "binding_digest": PRIVATE_BINDING_DIGEST,
            "governance_context": PRIVATE_CONTEXT_VALUE,
        }
        return {
            "status": "cancelled",
            "case": BoundedNegotiationRuntime.public_case_summary(completed),
            "cancellation": BoundedNegotiationRuntime._public_cancellation(
                raw_receipt
            ),
        }

    def recover_case(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        del operation_id
        self.reconcile_calls += 1
        if reason == "raise-bounded-error":
            raise BoundedNegotiationError("bounded reconciliation refusal")
        case = self.store.get_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if int(case["revision"]) != expected_revision:
            raise CaseRevisionConflictError(
                f"case revision conflict: expected {expected_revision}, "
                f"observed {case['revision']}"
            )
        return {
            "status": "nothing_to_reconcile",
            "case": BoundedNegotiationRuntime.public_case_summary(case),
            "execution_result": {
                "task_id": "task-public-reconcile",
                "executor": "openclaw",
                "status": "success",
                "result": "bounded public result",
                "raw": {
                    "session_key": PRIVATE_SESSION_KEY,
                    "binding_digest": PRIVATE_BINDING_DIGEST,
                    "provider_debug": PRIVATE_CONTEXT_VALUE,
                },
            },
            "dialogue_message": {
                "context": PRIVATE_CONTEXT_VALUE,
            },
            "trace_outbox": {
                "appended": 0,
                "deduplicated": 0,
                "acknowledged": 0,
            },
        }


def _admit(
    store: DurableCaseStore,
    event_id: str,
    *,
    user_id: str = USER_ID,
    workspace_id: str = WORKSPACE_ID,
) -> dict[str, Any]:
    return store.admit_event(
        event_id=event_id,
        user_id=user_id,
        workspace_id=workspace_id,
        user_goal=f"Analyze bounded case {event_id}.",
    )


def _command(
    *,
    command: str,
    expected_revision: int | bool,
    operation_id: str,
    reason: str = "http contract smoke",
    user_id: str = USER_ID,
    workspace_id: str = WORKSPACE_ID,
) -> dict[str, Any]:
    return {
        "schema_version": "veyra.durable_case_command.v1",
        "user_id": user_id,
        "workspace_id": workspace_id,
        "expected_revision": expected_revision,
        "operation_id": operation_id,
        "command": command,
        "reason": reason,
    }


def _forbidden_paths(value: Any) -> list[str]:
    forbidden_keys = {
        "session_key",
        "binding_digest",
        "run_id",
        "runtime_run_id",
        "governance_context",
        "context",
        "operations",
        "operation_result",
    }
    found: list[str] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{path}.{key}"
                if key in forbidden_keys:
                    found.append(child_path)
                walk(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(value, "$")
    return found


def _contains_private_value(value: Any) -> bool:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return any(
        private in serialized
        for private in (
            PRIVATE_RUN_ID,
            PRIVATE_SESSION_KEY,
            PRIVATE_BINDING_DIGEST,
            PRIVATE_CONTEXT_VALUE,
        )
    )


async def run_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-case-http-") as temp_dir:
        store = DurableCaseStore(WorldStateStore(Path(temp_dir)))
        runtime = FakeBoundedRuntime(store)
        app = FastAPI()
        app.include_router(
            build_cases_router(
                {
                    "durable_case_store": store,
                    "bounded_negotiation": runtime,
                }
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://veyra.test",
        ) as client:
            await check_validation_and_scope(client, store, runtime)
            await check_pause_resume_cas(client, store)
            await check_public_views_and_live_fence(client, store, runtime)
            await check_cancel_reconcile_and_errors(
                client, store, runtime
            )


async def check_validation_and_scope(
    client: httpx.AsyncClient,
    store: DurableCaseStore,
    runtime: FakeBoundedRuntime,
) -> None:
    case = _admit(store, "event-http-validation")
    invalid = await client.post(
        f"/cases/{case['case_id']}/commands",
        json=_command(
            command="cancel",
            expected_revision=True,
            operation_id="bool-revision",
        ),
    )
    expect(
        invalid.status_code == 422
        and runtime.cancel_calls == 0
        and store.get_case(
            case_id=case["case_id"],
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
        )["revision"]
        == 1,
        "HTTP command rejects bool-to-int revision before mutation",
        invalid.json(),
    )
    invalid_limit = await client.get(
        "/cases",
        params={
            "user_id": USER_ID,
            "workspace_id": WORKSPACE_ID,
            "limit": "true",
        },
    )
    expect(
        invalid_limit.status_code == 422,
        "HTTP list rejects boolean text as integer limit",
        invalid_limit.json(),
    )
    cross_detail = await client.get(
        f"/cases/{case['case_id']}",
        params={"user_id": "user-b", "workspace_id": WORKSPACE_ID},
    )
    cross_command = await client.post(
        f"/cases/{case['case_id']}/commands",
        json=_command(
            command="pause",
            expected_revision=1,
            operation_id="cross-owner-pause",
            user_id="user-b",
        ),
    )
    cross_list = await client.get(
        "/cases",
        params={"user_id": "user-b", "workspace_id": WORKSPACE_ID},
    )
    expect(
        cross_detail.status_code == 404
        and cross_command.status_code == 404
        and cross_list.status_code == 200
        and cross_list.json()["count"] == 0,
        "owner isolation collapses detail and command to 404",
        {
            "detail": cross_detail.json(),
            "command": cross_command.json(),
            "list": cross_list.json(),
        },
    )


async def check_pause_resume_cas(
    client: httpx.AsyncClient,
    store: DurableCaseStore,
) -> None:
    case = _admit(store, "event-http-pause-resume")
    url = f"/cases/{case['case_id']}/commands"
    pause_payload = _command(
        command="pause",
        expected_revision=1,
        operation_id="pause-once",
    )
    pause = await client.post(url, json=pause_payload)
    pause_replay = await client.post(url, json=pause_payload)
    expect(
        pause.status_code == 200
        and pause.json()["result"]["status"] == "PAUSED"
        and pause.json()["result"]["revision"] == 2
        and pause_replay.status_code == 200
        and pause_replay.json()["result"]["status"] == "PAUSED"
        and pause_replay.json()["result"]["revision"] == 2
        and pause_replay.json()["result"]["operation_replayed"] is True,
        "pause command is at-least-once idempotent",
        {"first": pause.json(), "replay": pause_replay.json()},
    )
    operation_conflict = await client.post(
        url,
        json={
            **pause_payload,
            "reason": "different semantics for same operation",
        },
    )
    stale_revision = await client.post(
        url,
        json=_command(
            command="resume",
            expected_revision=1,
            operation_id="resume-stale",
        ),
    )
    expect(
        operation_conflict.status_code == 409
        and stale_revision.status_code == 409,
        "operation semantic conflicts and stale CAS return controlled 409",
        {
            "operation": operation_conflict.json(),
            "revision": stale_revision.json(),
        },
    )
    resume_payload = _command(
        command="resume",
        expected_revision=2,
        operation_id="resume-once",
    )
    resume = await client.post(url, json=resume_payload)
    resume_replay = await client.post(url, json=resume_payload)
    expect(
        resume.status_code == 200
        and resume.json()["result"]["status"] == "QUALIFIED"
        and resume.json()["result"]["revision"] == 3
        and resume_replay.status_code == 200
        and resume_replay.json()["result"]["status"] == "QUALIFIED"
        and resume_replay.json()["result"]["revision"] == 3
        and resume_replay.json()["result"]["operation_replayed"] is True,
        "resume restores the exact pre-pause state and replays safely",
        {"first": resume.json(), "replay": resume_replay.json()},
    )


async def check_public_views_and_live_fence(
    client: httpx.AsyncClient,
    store: DurableCaseStore,
    runtime: FakeBoundedRuntime,
) -> None:
    admitted = _admit(store, "event-http-live-agent")
    active = store.transition(
        case_id=admitted["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        operation_id="dispatch-live-agent",
        expected_revision=1,
        to_status="DELIBERATING",
        reason="record exact active Agent binding",
        checkpoint=_checkpoint(
            checkpoint_id="checkpoint-live-agent",
            phase="agent_dispatched",
            result_status="running",
        ),
        dialogue_message=_task_request(
            case_id=admitted["case_id"],
            case_revision=1,
        ),
    )
    list_response = await client.get(
        "/cases",
        params={"user_id": USER_ID, "workspace_id": WORKSPACE_ID},
    )
    detail_response = await client.get(
        f"/cases/{admitted['case_id']}",
        params={"user_id": USER_ID, "workspace_id": WORKSPACE_ID},
    )
    list_body = list_response.json()
    detail_body = detail_response.json()
    expect(
        list_response.status_code == 200
        and detail_response.status_code == 200
        and not _forbidden_paths(list_body)
        and not _forbidden_paths(detail_body)
        and not _contains_private_value(list_body)
        and not _contains_private_value(detail_body),
        "list and detail recursively redact private run authority and context",
        {
            "list_forbidden": _forbidden_paths(list_body),
            "detail_forbidden": _forbidden_paths(detail_body),
            "list": list_body,
            "detail": detail_body,
        },
    )
    detail_case = detail_body["case"]
    expect(
        detail_case["checkpoint_count"] == 1
        and detail_case["dialogue_count"] == 1
        and detail_case["checkpoints"][0]["phase"] == "agent_dispatched"
        and detail_case["dialogue"][0]["content"]["payload"]["user_goal"]
        == "Compare two read-only recovery options.",
        "detail retains bounded replay evidence after redaction",
        detail_case,
    )
    store.state_store.patch_json(
        "task_state.json",
        {
            "pending_agent_tasks": [
                {
                    "task_id": PRIVATE_RUN_ID,
                    "task_context": {
                        "runtime_task_id": PRIVATE_RUN_ID,
                        "agent_execution_session_id": PRIVATE_SESSION_KEY,
                        "binding_digest": PRIVATE_BINDING_DIGEST,
                        "user_goal": PRIVATE_CONTEXT_VALUE,
                    },
                }
            ],
            "agent_task_contexts": {
                PRIVATE_RUN_ID: {
                    "runtime_task_id": PRIVATE_RUN_ID,
                    "agent_execution_session_id": PRIVATE_SESSION_KEY,
                }
            },
            "current_task": {
                "task_id": PRIVATE_RUN_ID,
                "status": "submitted",
                "task_context": {"private": PRIVATE_CONTEXT_VALUE},
            },
        },
    )
    generic_state = _public_state(store.state_store.read_all())
    expect(
        "durable_case_state" not in generic_state
        and not _contains_private_value(generic_state),
        "generic state endpoint exposes neither Cases nor private task bindings",
        generic_state,
    )
    pause = await client.post(
        f"/cases/{admitted['case_id']}/commands",
        json=_command(
            command="pause",
            expected_revision=int(active["revision"]),
            operation_id="pause-active-agent",
        ),
    )
    close = await client.post(
        f"/cases/{admitted['case_id']}/commands",
        json=_command(
            command="close",
            expected_revision=int(active["revision"]),
            operation_id="close-active-agent",
        ),
    )
    persisted = store.get_case(
        case_id=admitted["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        pause.status_code == 409
        and close.status_code == 409
        and persisted["status"] == "DELIBERATING"
        and persisted["revision"] == active["revision"]
        and runtime.cancel_calls == 0,
        "active Agent dispatch rejects pause and close until revocation",
        {
            "pause": pause.json(),
            "close": close.json(),
            "case": persisted,
        },
    )


async def check_cancel_reconcile_and_errors(
    client: httpx.AsyncClient,
    store: DurableCaseStore,
    runtime: FakeBoundedRuntime,
) -> None:
    cancel_case = _admit(store, "event-http-cancel")
    cancel_url = f"/cases/{cancel_case['case_id']}/commands"
    cancel = await client.post(
        cancel_url,
        json=_command(
            command="cancel",
            expected_revision=1,
            operation_id="cancel-once",
        ),
    )
    cancel_body = cancel.json()
    cancellation = cancel_body.get("result", {}).get("cancellation", {})
    expect(
        cancel.status_code == 200
        and cancel_body["result"]["status"] == "cancelled"
        and cancel_body["result"]["case"]["status"] == "CANCELLED"
        and set(cancellation).issubset(
            {
                "status",
                "reason",
                "authority_revoked",
                "plugin_authority_closed",
                "agent_abort_confirmed",
                "revoked_grant_count",
                "executing_reservation_count",
                "cancelled_reservation_count",
                "receipt_replayed",
            }
        )
        and "session_key" not in cancellation
        and "binding_digest" not in cancellation
        and PRIVATE_RUN_ID not in json.dumps(cancel_body)
        and PRIVATE_SESSION_KEY not in json.dumps(cancel_body)
        and PRIVATE_BINDING_DIGEST not in json.dumps(cancel_body),
        "cancel response exposes only the bounded revocation projection",
        cancel_body,
    )

    reconcile_case = _admit(store, "event-http-reconcile")
    reconcile = await client.post(
        f"/cases/{reconcile_case['case_id']}/commands",
        json=_command(
            command="reconcile",
            expected_revision=1,
            operation_id="reconcile-once",
        ),
    )
    reconcile_body = reconcile.json()
    expect(
        reconcile.status_code == 200
        and reconcile_body["result"]["status"]
        == "nothing_to_reconcile"
        and reconcile_body["result"]["execution_result"]
        == {
            "executor": "openclaw",
            "status": "success",
        }
        and not _forbidden_paths(reconcile_body)
        and not _contains_private_value(reconcile_body),
        "reconcile response is a bounded public projection",
        reconcile_body,
    )
    bounded_error = await client.post(
        f"/cases/{reconcile_case['case_id']}/commands",
        json=_command(
            command="reconcile",
            expected_revision=1,
            operation_id="reconcile-error",
            reason="raise-bounded-error",
        ),
    )
    owner_error = await client.post(
        f"/cases/{reconcile_case['case_id']}/commands",
        json=_command(
            command="cancel",
            expected_revision=1,
            operation_id="cancel-other-owner",
            user_id="user-b",
        ),
    )
    expect(
        bounded_error.status_code == 409
        and bounded_error.json()["detail"]
        == "bounded reconciliation refusal"
        and owner_error.status_code == 404
        and owner_error.json()["detail"] == "durable case not found",
        "runtime and ownership failures map to controlled public errors",
        {
            "runtime": bounded_error.json(),
            "owner": owner_error.json(),
        },
    )


if __name__ == "__main__":
    asyncio.run(run_contract())
    print("durable case HTTP contract smoke passed")
