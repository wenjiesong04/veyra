#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.task_packet_builder import TaskPacketBuilder  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import (  # noqa: E402
    EventSource,
    EventType,
    VeyraEvent,
)
from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402


SUFFIX = "abcdef123456"
USER_ID = f"phase3-live-canary-{SUFFIX}"


def expect(
    condition: bool,
    label: str,
    detail: object = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"ok - {label}")


def main() -> int:
    with TemporaryDirectory(
        prefix="veyra-openclaw-canary-bootstrap-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json",
            {"current_project": "workspace-canary-bootstrap"},
        )
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(
                channel="api",
                user_id=USER_ID,
                session_id=f"phase3-canary-session-{SUFFIX}",
            ),
            payload={"text": "Run the fixed governance canary."},
            event_id=f"canary-event-{SUFFIX}",
        )
        packet = TaskPacketBuilder(store).build(
            event=event,
            target_agent="openclaw",
            context_patch={},
            persona_patch={},
            policy_patch={},
            task_id=f"canary-task-{SUFFIX}",
            case_id=f"canary-case-{SUFFIX}",
            step_id=f"canary-step-{SUFFIX}",
            runtime_run_id=f"canary-run-{SUFFIX}",
        )
        packet.governance_context["governance_canary"] = {
            "schema_version": "veyra.openclaw_governance_canary.v1",
            "suffix": SUFFIX,
        }
        prepared: list[dict[str, Any]] = []
        submitted: list[dict[str, Any]] = []

        def prepare(
            _packet: object,
            *,
            run_id: str,
            session_key: str,
        ) -> dict[str, Any]:
            payload = {
                "runId": run_id,
                "sessionKey": session_key,
                "dispatchToken": "canary-dispatch-token",
                "expiresAt": (
                    datetime.now(timezone.utc)
                    + timedelta(minutes=5)
                ).isoformat(),
                "bindingDigest": "a" * 64,
                "allowedTools": [
                    "veyra_file_read",
                    "veyra_file_write",
                    "veyra_shell_probe",
                ],
            }
            prepared.append(payload)
            return payload

        adapter = OpenClawAdapter(
            base_url="ws://127.0.0.1:9",
            governance_dispatch_preparer=prepare,
            governance_dispatch_canceller=lambda *_args, **_kwargs: {
                "status": "cancelled"
            },
            governance_run_evidence_resolver=lambda run_id: {
                "status": "resolved",
                "run_id": run_id,
                "tool_calls": [],
                "changed_files": [],
                "tool_receipt_refs": [],
                "observed_call_count": 0,
                "effect_count": 0,
            },
            governance_status_resolver=lambda: {
                "status": "validation_pending"
            },
        )
        missing_attestation = {
            "allowed": False,
            "status": "blocked",
            "reasons": ["tool_proxy_enforcement_not_verified"],
            "provider_certification_status": "validated",
            "tool_proxy_enforced": False,
            "tool_proxy_identity_match": True,
            "tool_proxy_enforcement_scope": (
                "veyra_governed_openclaw_sessions"
            ),
            "governance_callbacks_complete": True,
            "policy_effect": "none",
        }
        adapter._governed_dispatch_preflight = (  # type: ignore[method-assign]
            lambda: dict(missing_attestation)
        )

        def send_chat(
            _prompt: str,
            *,
            session_key: str,
            idempotency_key: str | None = None,
            governance_registration: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            submitted.append(
                {
                    "session_key": session_key,
                    "run_id": idempotency_key,
                    "registration": governance_registration,
                }
            )
            return {
                "run_id": idempotency_key,
                "chat_send": {"runId": idempotency_key},
                "final_event": {"state": "running"},
            }

        adapter._send_chat = send_chat  # type: ignore[method-assign]
        ordinary = adapter.send_task(packet)
        expect(
            ordinary.status == "blocked"
            and not prepared
            and not submitted,
            "ordinary Agent dispatch cannot use the canary bootstrap",
            ordinary.to_dict(),
        )
        canary = adapter.send_governance_canary(packet)
        expect(
            canary.status == "submitted"
            and len(prepared) == 1
            and len(submitted) == 1
            and submitted[0]["run_id"]
            == f"canary-run-{SUFFIX}",
            (
                "fixed canary bypasses only a missing fresh attestation "
                "and keeps exact governed registration"
            ),
            {
                "execution": canary.to_dict(),
                "prepared": prepared,
                "submitted": submitted,
            },
        )
        adapter._governed_dispatch_preflight = (  # type: ignore[method-assign]
            lambda: {
                **missing_attestation,
                "reasons": [
                    "provider_certification_required",
                    "tool_proxy_enforcement_not_verified",
                ],
            }
        )
        blocked = adapter.send_governance_canary(packet)
        expect(
            blocked.status == "blocked"
            and len(prepared) == 1
            and len(submitted) == 1,
            "canary cannot bypass any provider or identity failure",
            blocked.to_dict(),
        )
        packet.governance_context["governance_canary"] = {
            "schema_version": "veyra.openclaw_governance_canary.v1",
            "suffix": "INVALID",
        }
        invalid = adapter.send_governance_canary(packet)
        expect(
            invalid.status == "blocked"
            and len(prepared) == 1
            and len(submitted) == 1,
            "invalid private canary marker has zero dispatch",
            invalid.to_dict(),
        )

    print("openclaw_canary_bootstrap_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
