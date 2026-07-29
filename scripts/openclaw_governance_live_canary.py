#!/usr/bin/env python3
"""Run the real Phase 3 OpenClaw hook canary against restarted services.

This script is intentionally not part of the offline gate. It uses Veyra's
explicit Agent invocation API, waits for the resulting governed dispatch, and
attests the live hook while the Agent run is still active. No bearer token or
private ledger payload is printed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402
from tool_proxy.governance_contract import canonical_sha256  # noqa: E402


BASE_URL = os.getenv(
    "VEYRA_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")
OPENCLAW_URL = os.getenv(
    "OPENCLAW_GATEWAY_URL",
    "ws://127.0.0.1:18789",
)


def _state_root() -> Path:
    configured = os.getenv("VEYRA_STATE_DIR") or os.getenv("VEYRA_STATE_ROOT")
    if not configured:
        return ROOT / "state"
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


HOOK_STATE_PATH = (
    _state_root() / "runtime" / "openclaw_tool_hook_state.json"
)


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float,
) -> dict[str, Any]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"{method} {path} failed against {BASE_URL}: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{method} {path} returned a non-object")
    return decoded


def read_hook_state() -> dict[str, Any]:
    try:
        decoded = json.loads(HOOK_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def matching_dispatch(
    document: dict[str, Any],
    *,
    user_id: str,
    initial_runs: set[str],
) -> tuple[str, dict[str, Any]] | None:
    dispatches = (
        document.get("dispatches")
        if isinstance(document.get("dispatches"), dict)
        else {}
    )
    matches: list[tuple[str, dict[str, Any]]] = []
    for run_id, item in dispatches.items():
        if (
            not isinstance(run_id, str)
            or run_id in initial_runs
            or not isinstance(item, dict)
        ):
            continue
        binding = (
            item.get("binding")
            if isinstance(item.get("binding"), dict)
            else {}
        )
        if binding.get("user_id") == user_id:
            matches.append((run_id, item))
    if not matches:
        return None
    return max(
        matches,
        key=lambda pair: str(pair[1].get("registered_at") or ""),
    )


def canary_evidence_ready(
    document: dict[str, Any],
    *,
    run_id: str,
    dispatch: dict[str, Any],
    sentinel_relative_path: str,
    native_params_digest: str,
) -> tuple[bool, dict[str, int]]:
    attempts = (
        document.get("attempts")
        if isinstance(document.get("attempts"), dict)
        else {}
    )
    run_attempts = [
        item
        for item in attempts.values()
        if isinstance(item, dict) and item.get("run_id") == run_id
    ]
    sentinel_absolute = str(
        Path(str(dispatch.get("sandbox_root") or ""))
        / sentinel_relative_path
    )
    write_success = sum(
        1
        for item in run_attempts
        if item.get("host_tool_name") == "veyra_file_write"
        and item.get("status") == "observed_success"
        and isinstance(item.get("invocation"), dict)
        and item["invocation"].get("derived_targets")
        == [sentinel_absolute]
    )
    broker_blocks = sum(
        1
        for item in run_attempts
        if item.get("status") == "blocked"
    )
    native_blocks = sum(
        1
        for item in run_attempts
        if item.get("status") == "plugin_blocked"
        and item.get("host_tool_name") == "write"
        and isinstance(item.get("plugin_observation"), dict)
        and item["plugin_observation"].get("params_digest")
        == native_params_digest
    )
    counts = {
        "write_success": write_success,
        "broker_blocks": broker_blocks,
        "native_blocks": native_blocks,
    }
    return all(counts.values()), counts


def invocation_execution(
    response: dict[str, Any],
) -> dict[str, Any]:
    results = (
        response.get("results")
        if isinstance(response.get("results"), list)
        else []
    )
    first = (
        results[0]
        if results and isinstance(results[0], dict)
        else {}
    )
    execution = first.get("execution_result")
    return execution if isinstance(execution, dict) else {}


def main() -> int:
    suffix = uuid4().hex[:12]
    user_id = f"phase3-live-canary-{suffix}"
    sentinel_relative_path = f"phase3-sentinel-{suffix}.txt"
    sentinel_content = f"VEYRA_PHASE3_SENTINEL_{suffix}\n"
    native_block_path = str(
        _state_root()
        / "runtime"
        / "openclaw_native_block_canaries"
        / f"phase3-{suffix}.txt"
    )
    native_block_content = f"VEYRA_NATIVE_BLOCK_{suffix}\n"
    invalid_relative_path = f"..{os.sep}phase3-escape-{suffix}.txt"
    native_params_digest = canonical_sha256(
        {
            "path": native_block_path,
            "content": native_block_content,
        }
    )
    initial_document = read_hook_state()
    initial_dispatches = (
        initial_document.get("dispatches")
        if isinstance(initial_document.get("dispatches"), dict)
        else {}
    )
    initial_runs = {
        str(run_id) for run_id in initial_dispatches
    }
    payload = {
        "schema_version": (
            "veyra.openclaw_governance_canary_run.v1"
        ),
        "suffix": suffix,
    }
    adapter = OpenClawAdapter(
        base_url=OPENCLAW_URL,
        timeout=20.0,
    )
    deadline = time.monotonic() + 150.0
    selected_run = ""
    selected_dispatch: dict[str, Any] = {}
    evidence_counts = {
        "write_success": 0,
        "broker_blocks": 0,
        "native_blocks": 0,
    }
    attestation: dict[str, Any] | None = None

    with ThreadPoolExecutor(max_workers=1) as executor:
        response_future = executor.submit(
            request_json,
            "POST",
            "/agents/governance-canary",
            payload,
            timeout=180.0,
        )
        while time.monotonic() < deadline:
            document = read_hook_state()
            matched = matching_dispatch(
                document,
                user_id=user_id,
                initial_runs=initial_runs,
            )
            if matched is not None:
                selected_run, selected_dispatch = matched
                ready, evidence_counts = canary_evidence_ready(
                    document,
                    run_id=selected_run,
                    dispatch=selected_dispatch,
                    sentinel_relative_path=sentinel_relative_path,
                    native_params_digest=native_params_digest,
                )
                if ready:
                    attestation = adapter._gateway_request(
                        "veyra.governance.attestCanary",
                        {
                            "sessionKey": selected_dispatch["session_key"],
                            "sentinelRelativePath": sentinel_relative_path,
                            "nativeBlockPath": native_block_path,
                            "nativeBlockContent": native_block_content,
                        },
                    )
                    break
            if response_future.done() and not selected_run:
                response = response_future.result()
                raise RuntimeError(
                    "Veyra did not create a governed Agent dispatch: "
                    f"status={response.get('status')!r}, "
                    f"risk={response.get('risk_level')!r}, "
                    f"skipped={response.get('skipped')!r}"
                )
            if response_future.done() and selected_run:
                completed_response = response_future.result()
                completed_execution = invocation_execution(
                    completed_response
                )
                completed_status = str(
                    completed_execution.get("status") or ""
                )
                if completed_status not in {
                    "",
                    "accepted",
                    "pending",
                    "queued",
                    "running",
                    "submitted",
                }:
                    raise RuntimeError(
                        "governed Agent run ended before canary evidence: "
                        f"status={completed_status!r}, "
                        f"result={str(completed_execution.get('result') or '')[:300]!r}, "
                        f"counts={evidence_counts}"
                    )
            time.sleep(0.005)
        if attestation is None:
            partial_response = (
                response_future.result()
                if response_future.done()
                else {}
            )
            raise RuntimeError(
                "live canary evidence was incomplete before timeout: "
                f"run={selected_run!r}, counts={evidence_counts}, "
                f"status={partial_response.get('status')!r}, "
                f"risk={partial_response.get('risk_level')!r}"
            )
        response = response_future.result(timeout=120.0)

    status = request_json(
        "GET",
        "/tool-governance/status",
        timeout=20.0,
    )
    hook_status = (
        status.get("hook_enforcement")
        if isinstance(status.get("hook_enforcement"), dict)
        else {}
    )
    if (
        attestation.get("status") != "validated"
        or status.get("tool_proxy_enforced") is not True
        or hook_status.get("status") != "validated"
        or Path(native_block_path).exists()
    ):
        raise RuntimeError(
            "live canary did not establish the scoped enforcement boundary"
        )
    sentinel_absolute = (
        Path(str(selected_dispatch["sandbox_root"]))
        / sentinel_relative_path
    )
    if (
        not sentinel_absolute.is_file()
        or sentinel_absolute.read_text(encoding="utf-8")
        != sentinel_content
    ):
        raise RuntimeError("live canary sentinel content was not verified")
    escaped_target = (
        Path(str(selected_dispatch["sandbox_root"])).parent
        / Path(invalid_relative_path).name
    )
    if escaped_target.exists():
        raise RuntimeError("blocked parent traversal created an outside file")

    execution_result = invocation_execution(response)
    print(
        json.dumps(
            {
                "status": "validated",
                "route": "agent",
                "response_status": response.get("status"),
                "run_id": selected_run,
                "session_key": selected_dispatch.get("session_key"),
                "agent_execution_status": (
                    execution_result.get("status")
                    if isinstance(execution_result, dict)
                    else None
                ),
                "evidence_counts": evidence_counts,
                "native_target_absent": not Path(native_block_path).exists(),
                "escape_target_absent": not escaped_target.exists(),
                "sentinel_content_verified": True,
                "tool_proxy_enforced": status.get(
                    "tool_proxy_enforced"
                ),
                "canary": hook_status.get("canary"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("openclaw governance live canary passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
