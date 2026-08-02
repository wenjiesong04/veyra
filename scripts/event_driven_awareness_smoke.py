#!/usr/bin/env python3
from __future__ import annotations

import copy
import gzip
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.situation_evaluator import SituationEvaluator  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.agent_contract import (  # noqa: E402
    AGENT_CONTRACT_VERSION,
    normalize_capabilities,
)
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import (  # noqa: E402
    Decision,
    EventType,
    LoopResult,
    Route,
    VeyraTaskPacket,
    utc_now_iso,
)
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def build_loop(root: Path, *, mode: str = "shadow") -> AwarenessLoop:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner=f"event-driven-{root.name}")
    store.mutate_json(
        "ops_config.json",
        lambda config: config.update(
            {
                "event_awareness": {
                    "mode": mode,
                    "allowed_modes": ["disabled", "record_only", "shadow"],
                }
            }
        ),
    )
    return AwarenessLoop(store, RuntimeEntity(store))


@dataclass(frozen=True, slots=True)
class OfflineRouteCase:
    case_id: str
    text: str
    route: Route
    risk_level: RiskLevel
    expected_status: str
    response: str | None
    selected_probe: str | None = None


OFFLINE_ROUTE_CASES = (
    OfflineRouteCase(
        case_id="direct",
        text="Explain the offline fixture with sufficient context.",
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        expected_status="success",
        response="Offline direct answer.",
    ),
    OfflineRouteCase(
        case_id="probe",
        text="Read the offline fixture status.",
        route=Route.PROBE,
        risk_level=RiskLevel.R1,
        expected_status="verified_success",
        response="Offline probe observation.",
        selected_probe="system",
    ),
    OfflineRouteCase(
        case_id="native_tool",
        text="Prepare the offline native tool boundary.",
        route=Route.NATIVE_TOOL,
        risk_level=RiskLevel.R2,
        expected_status="needs_action_proposal",
        response=(
            "Native tool execution must be submitted as a structured "
            "ActionProposal or routed through Tool Proxy."
        ),
    ),
    OfflineRouteCase(
        case_id="skill",
        text="Summarize the offline fixture logs.",
        route=Route.SKILL,
        risk_level=RiskLevel.R1,
        expected_status="needs_more_probe",
        response=(
            "Log summarization skill is ready; provide a log path through "
            "log_probe for real summaries."
        ),
        selected_probe="summarize_logs",
    ),
    OfflineRouteCase(
        case_id="agent",
        text="Analyze the offline fixture and return a bounded plan.",
        route=Route.AGENT,
        risk_level=RiskLevel.R1,
        expected_status="partially_success",
        response=(
            "offline-agent 报告称：Offline Agent returned a bounded plan. "
            "该内容仍是 Agent 报告，尚未验证为持久执行效果，"
            "不能据此标记完成。"
        ),
    ),
    OfflineRouteCase(
        case_id="ask_user",
        text="Use the referenced target.",
        route=Route.ASK_USER,
        risk_level=RiskLevel.R0,
        expected_status="needs_user_input",
        response=None,
    ),
    OfflineRouteCase(
        case_id="human_review",
        text="Apply the offline fixture change after review.",
        route=Route.HUMAN_REVIEW,
        risk_level=RiskLevel.R3,
        expected_status="needs_confirmation",
        response=None,
    ),
    OfflineRouteCase(
        case_id="block",
        text="Exercise the controlled offline blocked route.",
        route=Route.BLOCK,
        risk_level=RiskLevel.R5,
        expected_status="blocked",
        response=(
            "Blocked by Guardian: destructive or forbidden action requires "
            "a safer plan and explicit review."
        ),
    ),
    OfflineRouteCase(
        case_id="rollback",
        text="Inspect rollback request snap_abcdef without executing it.",
        route=Route.ROLLBACK,
        risk_level=RiskLevel.R1,
        expected_status="needs_confirmation",
        response="Rollback restore requires review approval before execution.",
    ),
)


class OfflineModelClient:
    """Fail-fast model boundary for route equivalence and latency diagnostics."""

    def status(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "configured": False,
            "status": "isolated_for_event_awareness_validation",
            "decision_mode": "disabled",
        }

    def complete_json(self, *, system: str, user: str, purpose: str) -> dict[str, Any]:
        del system, user
        return {
            "status": "disabled_for_event_awareness_validation",
            "purpose": purpose,
        }


class OfflineProbe:
    def run(self, text: str = "", **kwargs: Any) -> dict[str, Any]:
        del text, kwargs
        return {
            "probe": "system",
            "source": "offline_fixture",
            "target": "offline-system",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "status": "ok",
            "summary": "Offline probe observation.",
            "confidence": 1.0,
            "ttl_seconds": 60,
            "details": {"fixture": True},
        }


@dataclass
class OfflineAgentAdapter(AgentAdapter):
    executor: str = "offline-agent"
    sent_packets: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent_packets.append(task_packet.to_dict())
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.executor,
            status="success",
            result="Offline Agent returned a bounded plan.",
            raw={
                "agent_response": {
                    "answer_or_plan": "Offline Agent returned a bounded plan.",
                    "proposed_actions": [],
                },
                "offline_fixture": True,
            },
        )

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=task_id,
            executor=self.executor,
            status="success",
            result="Offline Agent returned a bounded plan.",
            raw={"offline_fixture": True},
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        observed_at = utc_now_iso()
        return {
            **normalize_capabilities(
                {
                    "runtime": "openclaw",
                    "status": "available",
                    "connected": True,
                    "contract_version": AGENT_CONTRACT_VERSION,
                "features": {
                    "structured_task_packet": True,
                    "rendered_prompt_fallback": True,
                    "task_status": True,
                    "stop_task": True,
                    "tool_proxy_enforced": True,
                    "tool_proxy_identity_match": True,
                    "tool_proxy_enforcement_scope": (
                        "veyra_governed_openclaw_sessions"
                    ),
                    "governance_callbacks_complete": True,
                },
                    "compatibility": {
                        "status": "compatible",
                        "native_adapter": False,
                    },
                    "offline_fixture": True,
                },
                runtime="openclaw",
            ),
            "updated_at": observed_at,
            "ttl_seconds": 300,
        }

    def connection_status(self) -> dict[str, Any]:
        capabilities = self.fetch_capabilities()
        return {
            "name": "openclaw",
            "runtime": "openclaw",
            "status": "available",
            "connected": True,
            "capabilities": capabilities,
            "validation": {
                "implemented": True,
                "configured": True,
                "connected": True,
                "validated": True,
                "status": "validated",
                "runtime_status": "available",
            },
        }

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "summary": "",
            "freshness": "fresh",
            "trust": "offline_fixture",
        }


_RUNTIME_TRACE_ID_PATH = ("artifacts", "runtime_trace", "trace_id")
_EXECUTION_TRACE_ID_PATH = ("artifacts", "execution_trace", "trace_id")
_RESPONSE_EXECUTION_RECEIPT_PREFIX = (
    "artifacts",
    "response_authority",
    "execution_receipt_refs",
)
_REVIEW_ID_PATH = ("artifacts", "review", "review_id")
_LATENCY_PATH = ("artifacts", "runtime_trace", "latency_ms")
_AGENT_TASK_ID_PATHS = {
    ("artifacts", "execution_result", "task_id"),
    ("artifacts", "execution_trace", "execution_result", "task_id"),
    ("artifacts", "execution_trace", "task_id"),
    ("artifacts", "execution_trace", "verification", "evidence", "task_id"),
    ("artifacts", "interpreted_result", "evidence", "task_id"),
    (
        "artifacts",
        "task_packet",
        "policy_patch",
        "tool_proxy_contract",
        "task_id",
    ),
    ("artifacts", "task_packet", "task_id"),
    ("artifacts", "verification", "evidence", "task_id"),
}
_AGENT_SESSION_ID_PATHS = {
    ("artifacts", "agent_session", "agent_execution_session_id"),
    ("artifacts", "task_packet", "agent_execution_session_id"),
}
_RUNTIME_TIMESTAMP_PATHS = {
    ("artifacts", "conversation_slots", "updated_at"),
    ("artifacts", "execution_trace", "recorded_at"),
    ("artifacts", "review", "created_at"),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "executor_state",
        "updated_at",
    ),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "relevant_world_state",
        "external",
        "updated_at",
    ),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "relevant_world_state",
        "local",
        "updated_at",
    ),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "relevant_world_state",
        "user",
        "updated_at",
    ),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "risk_state",
        "updated_at",
    ),
    (
        "artifacts",
        "task_packet",
        "context_patch",
        "task_state",
        "updated_at",
    ),
}
_BELIEF_CLAIM_PREFIX = (
    "artifacts",
    "task_packet",
    "context_patch",
    "belief_state",
    "fresh_claims",
)


def _public_result_dict(result: LoopResult | dict[str, Any]) -> dict[str, Any]:
    return result.to_dict() if isinstance(result, LoopResult) else copy.deepcopy(result)


def _valid_generated_id(value: Any, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value[len(prefix) :]
    return len(suffix) == 12 and all(
        character in "0123456789abcdef" for character in suffix
    )


def _generated_id_rule(
    path: tuple[str, ...],
    *,
    route: str,
) -> tuple[str, str] | None:
    if path == _RUNTIME_TRACE_ID_PATH:
        return ("runtime_trace_id", "rt_")
    if path == _EXECUTION_TRACE_ID_PATH:
        return ("execution_trace_id", "exec_")
    if (
        len(path) == len(_RESPONSE_EXECUTION_RECEIPT_PREFIX) + 1
        and path[: len(_RESPONSE_EXECUTION_RECEIPT_PREFIX)]
        == _RESPONSE_EXECUTION_RECEIPT_PREFIX
        and path[-1].isdigit()
    ):
        return ("execution_trace_id", "exec_")
    if path == _REVIEW_ID_PATH:
        return ("review_id", "rev_")
    if route == Route.AGENT.value and path in _AGENT_TASK_ID_PATHS:
        return ("task_id", "task_")
    if route == Route.AGENT.value and path in _AGENT_SESSION_ID_PATHS:
        return ("agent_execution_session_id", "agent-exec:task_")
    return None


def _is_belief_claim_path(
    path: tuple[str, ...],
    fields: set[str],
) -> bool:
    return (
        len(path) == len(_BELIEF_CLAIM_PREFIX) + 2
        and path[: len(_BELIEF_CLAIM_PREFIX)] == _BELIEF_CLAIM_PREFIX
        and path[-2].isdigit()
        and path[-1] in fields
    )


def _timestamp_tolerance_seconds(path: tuple[str, ...]) -> float | None:
    if path in _RUNTIME_TIMESTAMP_PATHS:
        return 2.0
    if _is_belief_claim_path(
        path,
        {"expires_at", "observed_at", "updated_at"},
    ):
        return 2.0
    return None


def _relative_time_tolerance(path: tuple[str, ...]) -> float | None:
    if _is_belief_claim_path(
        path,
        {"age_seconds", "ttl_remaining_seconds"},
    ):
        return 2.0
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _value_at(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    selected: Any = document
    for part in path:
        if isinstance(selected, dict) and part in selected:
            selected = selected[part]
            continue
        if (
            isinstance(selected, list)
            and part.isdigit()
            and int(part) < len(selected)
        ):
            selected = selected[int(part)]
            continue
        if not isinstance(selected, (dict, list)):
            return None
        return None
    return selected


def _timestamp_in_runtime_window(
    value: Any,
    *,
    document: dict[str, Any],
    path: tuple[str, ...],
    window: tuple[datetime, datetime],
) -> bool:
    parsed = _parse_timestamp(value)
    started_at, completed_at = window
    if (
        parsed is None
        or started_at.utcoffset() is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        return False
    if _is_belief_claim_path(path, {"expires_at"}):
        observed_at = _parse_timestamp(
            _value_at(
                document,
                path[:-1] + ("observed_at",),
            )
        )
        ttl_seconds = _value_at(
            document,
            path[:-1] + ("ttl_seconds",),
        )
        if (
            observed_at is None
            or not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) < 0
        ):
            return False
        return (
            started_at <= observed_at <= completed_at
            and parsed
            == observed_at + timedelta(seconds=float(ttl_seconds))
        )
    return started_at <= parsed <= completed_at


def _relative_time_in_runtime_window(
    value: Any,
    *,
    document: dict[str, Any],
    path: tuple[str, ...],
    window: tuple[datetime, datetime],
) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    started_at, completed_at = window
    observed_at = _parse_timestamp(
        _value_at(
            document,
            path[:-1] + ("observed_at",),
        )
    )
    expires_at = _parse_timestamp(
        _value_at(
            document,
            path[:-1] + ("expires_at",),
        )
    )
    if (
        observed_at is None
        or expires_at is None
        or started_at.utcoffset() is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        return False
    if path[-1] == "age_seconds":
        minimum = max(
            0,
            int((started_at - observed_at).total_seconds()),
        )
        maximum = max(
            0,
            int((completed_at - observed_at).total_seconds()),
        )
    elif path[-1] == "ttl_remaining_seconds":
        minimum = int((expires_at - completed_at).total_seconds())
        maximum = int((expires_at - started_at).total_seconds())
    else:
        return False
    return minimum <= value <= maximum


def _belief_relative_times_share_refresh(
    *,
    document: dict[str, Any],
    path: tuple[str, ...],
    window: tuple[datetime, datetime],
) -> bool:
    claim_path = path[:-1]
    observed_at = _parse_timestamp(
        _value_at(document, claim_path + ("observed_at",))
    )
    expires_at = _parse_timestamp(
        _value_at(document, claim_path + ("expires_at",))
    )
    ttl_seconds = _value_at(document, claim_path + ("ttl_seconds",))
    age_seconds = _value_at(document, claim_path + ("age_seconds",))
    ttl_remaining_seconds = _value_at(
        document,
        claim_path + ("ttl_remaining_seconds",),
    )
    started_at, completed_at = window
    integer_fields = (ttl_seconds, age_seconds, ttl_remaining_seconds)
    if (
        observed_at is None
        or expires_at is None
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in integer_fields
        )
        or ttl_seconds < 0
        or age_seconds < 0
        or started_at.utcoffset() is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
        or not started_at <= observed_at <= completed_at
        or expires_at
        != observed_at + timedelta(seconds=ttl_seconds)
    ):
        return False

    lower_bounds = [
        (started_at, True),
        (observed_at, True),
        (observed_at + timedelta(seconds=age_seconds), True),
    ]
    upper_bounds = [
        (completed_at, True),
        (
            observed_at + timedelta(seconds=age_seconds + 1),
            False,
        ),
    ]
    if ttl_remaining_seconds > 0:
        lower_bounds.append(
            (
                expires_at
                - timedelta(seconds=ttl_remaining_seconds + 1),
                False,
            )
        )
        upper_bounds.append(
            (
                expires_at - timedelta(seconds=ttl_remaining_seconds),
                True,
            )
        )
    elif ttl_remaining_seconds == 0:
        lower_bounds.append((expires_at - timedelta(seconds=1), False))
        upper_bounds.append((expires_at + timedelta(seconds=1), False))
    else:
        lower_bounds.append(
            (
                expires_at - timedelta(seconds=ttl_remaining_seconds),
                True,
            )
        )
        upper_bounds.append(
            (
                expires_at
                - timedelta(seconds=ttl_remaining_seconds - 1),
                False,
            )
        )

    lower_time = max(value for value, _ in lower_bounds)
    upper_time = min(value for value, _ in upper_bounds)
    lower_inclusive = all(
        inclusive
        for value, inclusive in lower_bounds
        if value == lower_time
    )
    upper_inclusive = all(
        inclusive
        for value, inclusive in upper_bounds
        if value == upper_time
    )
    return lower_time < upper_time or (
        lower_time == upper_time
        and lower_inclusive
        and upper_inclusive
    )


def _validate_agent_id_bindings(
    document: dict[str, Any],
    *,
    side: str,
    differences: list[str],
) -> None:
    if document.get("route") != Route.AGENT.value:
        return
    task_ids = [_value_at(document, path) for path in _AGENT_TASK_ID_PATHS]
    if (
        any(not _valid_generated_id(value, "task_") for value in task_ids)
        or len(set(task_ids)) != 1
    ):
        differences.append(f"{side}: agent task_id bindings are incomplete or inconsistent")
        return
    task_id = str(task_ids[0])
    session_ids = [
        _value_at(document, path)
        for path in _AGENT_SESSION_ID_PATHS
    ]
    if (
        any(
            not _valid_generated_id(value, "agent-exec:task_")
            for value in session_ids
        )
        or len(set(session_ids)) != 1
        or session_ids[0] != f"agent-exec:{task_id}"
    ):
        differences.append(
            f"{side}: agent execution session is not bound to its task_id"
        )


def offline_public_outputs_equivalent(
    left: LoopResult | dict[str, Any],
    right: LoopResult | dict[str, Any],
    *,
    require_distinct_generated_ids: bool,
    left_runtime_window: tuple[datetime, datetime] | None = None,
    right_runtime_window: tuple[datetime, datetime] | None = None,
) -> tuple[bool, list[str]]:
    """Compare every public field while permitting bounded runtime-only variance."""

    if (left_runtime_window is None) != (right_runtime_window is None):
        raise ValueError("runtime timestamp windows must be supplied as a pair")
    left_document = _public_result_dict(left)
    right_document = _public_result_dict(right)
    route = str(left_document.get("route") or "")
    differences: list[str] = []
    id_bindings: dict[str, dict[str, str]] = {}
    reverse_id_bindings: dict[str, dict[str, str]] = {}
    _validate_agent_id_bindings(
        left_document,
        side="left",
        differences=differences,
    )
    _validate_agent_id_bindings(
        right_document,
        side="right",
        differences=differences,
    )

    def compare(left_value: Any, right_value: Any, path: tuple[str, ...]) -> None:
        if len(differences) >= 20:
            return
        generated_id = _generated_id_rule(path, route=route)
        if generated_id is not None:
            kind, prefix = generated_id
            if not _valid_generated_id(left_value, prefix) or not _valid_generated_id(
                right_value,
                prefix,
            ):
                differences.append(
                    f"{'.'.join(path)}: generated IDs are missing or malformed"
                )
                return
            if require_distinct_generated_ids and left_value == right_value:
                differences.append(
                    f"{'.'.join(path)}: isolated runs reused one generated ID"
                )
                return
            forward = id_bindings.setdefault(kind, {})
            reverse = reverse_id_bindings.setdefault(kind, {})
            if left_value in forward and forward[left_value] != right_value:
                differences.append(
                    f"{'.'.join(path)}: generated ID binding diverged"
                )
                return
            if right_value in reverse and reverse[right_value] != left_value:
                differences.append(
                    f"{'.'.join(path)}: generated ID binding collapsed"
                )
                return
            forward[left_value] = right_value
            reverse[right_value] = left_value
            return

        timestamp_tolerance = _timestamp_tolerance_seconds(path)
        if timestamp_tolerance is not None:
            left_time = _parse_timestamp(left_value)
            right_time = _parse_timestamp(right_value)
            if left_time is None or right_time is None:
                differences.append(
                    f"{'.'.join(path)}: timestamp is missing or invalid"
                )
            elif (
                left_runtime_window is not None
                and right_runtime_window is not None
            ):
                if not _timestamp_in_runtime_window(
                    left_value,
                    document=left_document,
                    path=path,
                    window=left_runtime_window,
                ):
                    differences.append(
                        f"{'.'.join(path)}: left timestamp is outside its "
                        "runtime window"
                    )
                if not _timestamp_in_runtime_window(
                    right_value,
                    document=right_document,
                    path=path,
                    window=right_runtime_window,
                ):
                    differences.append(
                        f"{'.'.join(path)}: right timestamp is outside its "
                        "runtime window"
                    )
            elif abs((left_time - right_time).total_seconds()) > timestamp_tolerance:
                differences.append(
                    f"{'.'.join(path)}: timestamps exceed runtime tolerance"
                )
            return

        relative_tolerance = _relative_time_tolerance(path)
        if relative_tolerance is not None:
            valid_left = isinstance(left_value, (int, float)) and not isinstance(
                left_value,
                bool,
            )
            valid_right = isinstance(right_value, (int, float)) and not isinstance(
                right_value,
                bool,
            )
            if (
                not valid_left
                or not valid_right
                or not math.isfinite(float(left_value))
                or not math.isfinite(float(right_value))
            ):
                differences.append(
                    f"{'.'.join(path)}: relative time is missing or invalid"
                )
            elif (
                left_runtime_window is not None
                and right_runtime_window is not None
            ):
                left_relative_valid = _relative_time_in_runtime_window(
                    left_value,
                    document=left_document,
                    path=path,
                    window=left_runtime_window,
                )
                right_relative_valid = _relative_time_in_runtime_window(
                    right_value,
                    document=right_document,
                    path=path,
                    window=right_runtime_window,
                )
                if not left_relative_valid:
                    differences.append(
                        f"{'.'.join(path)}: left relative time is outside "
                        "its runtime window"
                    )
                if not right_relative_valid:
                    differences.append(
                        f"{'.'.join(path)}: right relative time is outside "
                        "its runtime window"
                    )
                if path[-1] == "age_seconds":
                    if (
                        left_relative_valid
                        and not _belief_relative_times_share_refresh(
                            document=left_document,
                            path=path,
                            window=left_runtime_window,
                        )
                    ):
                        differences.append(
                            f"{'.'.join(path)}: left belief times cannot "
                            "share one refresh instant"
                        )
                    if (
                        right_relative_valid
                        and not _belief_relative_times_share_refresh(
                            document=right_document,
                            path=path,
                            window=right_runtime_window,
                        )
                    ):
                        differences.append(
                            f"{'.'.join(path)}: right belief times cannot "
                            "share one refresh instant"
                        )
            elif (
                abs(float(left_value) - float(right_value))
                > relative_tolerance
            ):
                differences.append(
                    f"{'.'.join(path)}: relative time differs beyond tolerance"
                )
            return

        if path == _LATENCY_PATH:
            if (
                not isinstance(left_value, (int, float))
                or isinstance(left_value, bool)
                or float(left_value) < 0
                or not math.isfinite(float(left_value))
                or not isinstance(right_value, (int, float))
                or isinstance(right_value, bool)
                or float(right_value) < 0
                or not math.isfinite(float(right_value))
            ):
                differences.append(
                    f"{'.'.join(path)}: latency must be a non-negative number"
                )
            return

        if type(left_value) is not type(right_value):
            differences.append(
                f"{'.'.join(path)}: type {type(left_value).__name__} != "
                f"{type(right_value).__name__}"
            )
            return
        if isinstance(left_value, dict):
            if set(left_value) != set(right_value):
                differences.append(
                    f"{'.'.join(path)}: object keys differ "
                    f"{sorted(left_value)} != {sorted(right_value)}"
                )
                return
            for key in sorted(left_value):
                compare(
                    left_value[key],
                    right_value[key],
                    path + (str(key),),
                )
            return
        if isinstance(left_value, list):
            if len(left_value) != len(right_value):
                differences.append(
                    f"{'.'.join(path)}: list lengths differ "
                    f"{len(left_value)} != {len(right_value)}"
                )
                return
            for index, (left_item, right_item) in enumerate(
                zip(left_value, right_value)
            ):
                compare(left_item, right_item, path + (str(index),))
            return
        if left_value != right_value:
            differences.append(
                f"{'.'.join(path)}: {left_value!r} != {right_value!r}"
            )

    compare(left_document, right_document, ())
    return not differences, differences


def offline_result_signature(
    result: LoopResult | dict[str, Any],
) -> dict[str, Any]:
    """Render one full result with only validated runtime scalars canonicalized."""

    document = _public_result_dict(result)
    route = str(document.get("route") or "")
    id_tokens: dict[str, dict[str, str]] = {}

    def normalize(value: Any, path: tuple[str, ...]) -> Any:
        generated_id = _generated_id_rule(path, route=route)
        if generated_id is not None:
            kind, prefix = generated_id
            if not _valid_generated_id(value, prefix):
                return value
            tokens = id_tokens.setdefault(kind, {})
            if value not in tokens:
                tokens[value] = f"<{kind}:{len(tokens)}>"
            return tokens[value]
        if _timestamp_tolerance_seconds(path) is not None:
            return "<timestamp>" if _parse_timestamp(value) is not None else value
        if _relative_time_tolerance(path) is not None:
            return (
                "<relative_seconds>"
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else value
            )
        if path == _LATENCY_PATH:
            return (
                "<latency_ms>"
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else value
            )
        if isinstance(value, dict):
            return {
                str(key): normalize(item, path + (str(key),))
                for key, item in sorted(
                    value.items(),
                    key=lambda pair: str(pair[0]),
                )
            }
        if isinstance(value, list):
            return [
                normalize(item, path + (str(index),))
                for index, item in enumerate(value)
            ]
        return value

    normalized = normalize(document, ())
    return normalized if isinstance(normalized, dict) else {}


def build_offline_route_loop(
    root: Path,
    *,
    mode: str,
    case: OfflineRouteCase,
) -> AwarenessLoop:
    """Build the real turn pipeline with every external boundary replaced locally."""

    loop = build_loop(root, mode=mode)
    loop.core_reasoning.client = OfflineModelClient()  # type: ignore[assignment]
    selected_agent = loop.agent_registry.selected_name()
    loop.agent_registry._adapters[selected_agent] = OfflineAgentAdapter()
    loop.agent_adapter = loop.agent_registry.selected()
    observed_capabilities = loop.agent_adapter.fetch_capabilities()
    loop.state_store.patch_json(
        "executor_state.json",
        {
            "selected_agent": selected_agent,
            "status": "available",
            "connected": True,
            "capabilities": observed_capabilities,
            "ttl_seconds": 300,
        },
    )
    loop.probes["system"] = OfflineProbe()

    def fixed_understanding(**kwargs: Any) -> TurnUnderstanding:
        del kwargs
        return TurnUnderstanding(
            intent="offline_validation",
            task_summary=case.text,
            user_goal=case.text,
            what_user_really_needs="validate event awareness non-interference",
            task_type="offline_validation",
            explicit_request=case.text,
            hidden_need="validate event awareness non-interference",
            suggested_mode=case.route.value,
            confidence=1.0,
            reason="scripted offline fixture",
            source="offline_fixture",
        )

    def fixed_decision(*args: Any, **kwargs: Any) -> Decision:
        del args, kwargs
        route_capabilities = {
            Route.DIRECT_ANSWER: ["native_answer"],
            Route.PROBE: ["system_probe"],
            Route.NATIVE_TOOL: ["safe_file_read"],
            Route.SKILL: ["summarize_logs_skill"],
            Route.AGENT: ["selected_agent_runtime"],
            Route.ASK_USER: ["ask_user"],
            Route.HUMAN_REVIEW: ["human_review"],
            Route.BLOCK: ["guardian"],
            Route.ROLLBACK: ["rollback_audit"],
        }
        route_capability = {
            Route.DIRECT_ANSWER: "native_answer",
            Route.PROBE: "probe",
            Route.NATIVE_TOOL: "safe_file_read",
            Route.SKILL: "skill",
            Route.AGENT: "selected_agent_runtime",
            Route.ASK_USER: "ask_user",
            Route.HUMAN_REVIEW: "human_review",
            Route.BLOCK: "guardian",
            Route.ROLLBACK: "rollback_audit",
        }
        semantic_policy = {
            "preferred_route": case.route.value,
            "requires_clarification": case.route == Route.ASK_USER,
            "clarification_reason": (
                "the referenced target is unresolved"
                if case.route == Route.ASK_USER
                else ""
            ),
            "selected_probe": case.selected_probe,
            "allowed_capabilities": route_capabilities[case.route],
            "allowed_effects": (
                ["agent.execute"]
                if case.route == Route.AGENT
                else []
            ),
            "denied_effects": [],
        }
        return Decision(
            route=case.route,
            risk_level=case.risk_level,
            reason="scripted offline event awareness validation",
            requires_confirmation=case.route == Route.HUMAN_REVIEW,
            selected_probe=case.selected_probe,
            intent="offline_validation",
            complexity="complex" if case.route == Route.AGENT else "simple",
            capability=route_capability[case.route],
            needs_probe=case.route == Route.PROBE,
            needs_agent=case.route == Route.AGENT,
            needs_user_confirmation=case.route == Route.HUMAN_REVIEW,
            memory_policy="forget",
            reasoning_mode=(
                "execution"
                if case.route
                in {
                    Route.AGENT,
                    Route.HUMAN_REVIEW,
                    Route.NATIVE_TOOL,
                    Route.ROLLBACK,
                    Route.SKILL,
                }
                else "direct"
            ),
            required_capabilities=list(semantic_policy["allowed_capabilities"]),
            model_assist={
                "status": "offline_fixture",
                "draft_response": case.response,
                "semantic_policy": semantic_policy,
            },
        )

    loop.understanding_core.build = fixed_understanding  # type: ignore[method-assign]
    loop.decision_core.decide = fixed_decision  # type: ignore[method-assign]
    loop._direct_answer = (  # type: ignore[method-assign]
        lambda *args, **kwargs: str(case.response or "")
    )
    loop._low_latency_short_response = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._conversation_followup_result = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._early_awareness_response = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    loop._process_commitment_turn = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._sync_user_awareness_from_text = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._persist_conversation_slots = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._render_final_user_messages = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return loop


class HealthyRuntimeDependency:
    def refresh_pending(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_stale(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def run_read_only(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_watchlist(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def summary(self) -> dict[str, Any]:
        return {"policy": "smoke", "files": []}

    def enforce(self) -> dict[str, Any]:
        return {"policy": "smoke", "changed": 0, "files": []}

    def state(self) -> dict[str, Any]:
        return {"status": "idle"}


def build_active_loop(
    loop: AwarenessLoop,
    *,
    event_consumer: Any,
) -> ActiveRuntimeLoop:
    dependency = HealthyRuntimeDependency()
    return ActiveRuntimeLoop(
        state_store=loop.state_store,
        runtime_entity=loop.runtime_entity,
        proactive_checks=dependency,
        state_refresh=dependency,
        external_world_refresh=dependency,
        runtime_matrix=dependency,
        retention_policy=dependency,
        task_tracker=dependency,
        adapter_resolver=lambda: object(),
        verifier=object(),
        event_consumer=event_consumer,
    )


def test_user_turn_shadow_equivalence(base: Path) -> None:
    normalizer = EventNormalizer()
    control = build_loop(base / "control", mode="disabled")
    shadow = build_loop(base / "shadow")

    control_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_control",
        correlation_id="corr-equivalence",
    )
    shadow_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_shadow",
        correlation_id="corr-equivalence",
    )
    control_result = control.handle_event(control_event)
    shadow_result = shadow.handle_event(shadow_event)

    expect(
        (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        )
        == (
            shadow_result.route,
            shadow_result.status,
            shadow_result.response,
            shadow_result.risk_level,
        ),
        "shadow event fabric does not change route, status, response, or risk",
        {
            "control": control_result.to_dict(),
            "shadow": shadow_result.to_dict(),
        },
    )
    record = shadow.event_inbox.get_record(shadow_event.event_id)
    expect(
        isinstance(record, dict) and record.get("status") == "completed",
        "user turn is durably completed in the event inbox",
        record,
    )
    situations = shadow.situation_evaluator.list(
        user_id="user-a",
        session_id="session-a",
        correlation_id="corr-equivalence",
    )
    expect(
        len(situations) == 1
        and situations[0].get("status") == "resolved"
        and situations[0].get("decision")
        and situations[0].get("outcome"),
        "user turn is linked to a decision and outcome situation",
        situations,
    )
    expect(
        "text" not in situations[0],
        "shadow situation does not copy raw user text into the materialized view",
        situations[0],
    )
    trace_types = [
        row.get("trace_type")
        for row in shadow.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    ]
    expect(
        trace_types
        == [
            "situation_observed",
            "situation_decision_recorded",
            "situation_outcome_recorded",
        ],
        "situation lifecycle is append-only and ordered",
        trace_types,
    )

    failing = build_loop(base / "failing-shadow")
    original_append = failing.state_store.append_jsonl

    def fail_alert_only(name: str, payload: dict[str, Any]) -> None:
        if name == "alert_log.jsonl":
            raise OSError("injected alert storage failure")
        original_append(name, payload)

    def fail_enqueue_and_claim(
        event: Any,
        consumer_id: str,
        lease_seconds: float = 60.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del event, consumer_id, lease_seconds, kwargs
        raise OSError("injected inbox failure")

    failing.state_store.append_jsonl = fail_alert_only  # type: ignore[method-assign]
    failing.event_awareness.event_inbox.enqueue_and_claim = fail_enqueue_and_claim  # type: ignore[method-assign]
    failure_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_shadow_failure",
    )
    failure_result = failing.handle_event(failure_event)
    expect(
        (
            failure_result.route,
            failure_result.status,
            failure_result.response,
            failure_result.risk_level,
        )
        == (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        ),
        "shadow storage and alert failures cannot escape into the user turn",
        failure_result.to_dict(),
    )

    finalize_failure = build_loop(base / "finalize-failure")
    original_list = finalize_failure.situation_evaluator.list
    list_calls = 0

    def fail_finalize_lookup(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal list_calls
        list_calls += 1
        if list_calls > 1:
            raise OSError("injected situation lookup failure")
        return original_list(*args, **kwargs)

    finalize_failure.situation_evaluator.list = fail_finalize_lookup  # type: ignore[method-assign]
    finalize_failure_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_finalize_failure",
    )
    finalize_failure_result = finalize_failure.handle_event(finalize_failure_event)
    expect(
        (
            finalize_failure_result.route,
            finalize_failure_result.status,
            finalize_failure_result.response,
            finalize_failure_result.risk_level,
        )
        == (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        ),
        "situation finalization lookup failures cannot escape into the user turn",
        finalize_failure_result.to_dict(),
    )


def test_offline_route_equivalence_matrix(base: Path) -> None:
    normalizer = EventNormalizer()
    modes = ("disabled", "record_only", "shadow")
    matrix: list[dict[str, Any]] = []
    expect(
        {case.route for case in OFFLINE_ROUTE_CASES} == set(Route),
        "offline fixtures cover every Route enum member",
        {
            "expected": [route.value for route in Route],
            "covered": [case.route.value for case in OFFLINE_ROUTE_CASES],
        },
    )
    for case in OFFLINE_ROUTE_CASES:
        signatures: dict[str, dict[str, Any]] = {}
        results: dict[str, LoopResult] = {}
        loops: dict[str, AwarenessLoop] = {}
        runtime_windows: dict[str, tuple[datetime, datetime]] = {}
        event = normalizer.user_message(
            case.text,
            "offline-matrix",
            "matrix-user",
            f"matrix-{case.case_id}",
            event_id=f"evt_matrix_{case.case_id}",
            correlation_id=f"corr-matrix-{case.case_id}",
        )
        for mode in modes:
            started_at = datetime.now(timezone.utc)
            loop = build_offline_route_loop(
                base / case.case_id / mode,
                mode=mode,
                case=case,
            )
            result = loop.handle_event(event)
            signatures[mode] = offline_result_signature(result)
            results[mode] = result
            loops[mode] = loop
            runtime_windows[mode] = (
                started_at,
                datetime.now(timezone.utc),
            )

        baseline = signatures["disabled"]
        expect(
            baseline["route"] == case.route.value
            and baseline["status"] == case.expected_status
            and bool(baseline["response"])
            and (case.response is None or baseline["response"] == case.response)
            and baseline["risk_level"] == case.risk_level.value,
            f"{case.case_id} offline fixture exercises its intended route",
            baseline,
        )
        pair_differences: dict[str, list[str]] = {}
        for left_mode, right_mode in combinations(modes, 2):
            equivalent, differences = offline_public_outputs_equivalent(
                results[left_mode],
                results[right_mode],
                require_distinct_generated_ids=True,
                left_runtime_window=runtime_windows[left_mode],
                right_runtime_window=runtime_windows[right_mode],
            )
            if not equivalent:
                pair_differences[f"{left_mode}:{right_mode}"] = differences
        expect(
            not pair_differences,
            (
                f"{case.case_id} disabled, record-only, and shadow complete "
                "public outputs are equivalent"
            ),
            pair_differences,
        )
        expect(
            loops["disabled"].event_inbox.stats().get("total") == 0
            and loops["record_only"].event_inbox.stats().get("pending") == 1
            and loops["shadow"].event_inbox.stats().get("completed") == 1,
            f"{case.case_id} mode changes observation state only",
            {
                mode: loop.event_inbox.stats()
                for mode, loop in loops.items()
            },
        )
        matrix.append({"case": case.case_id, "signatures": signatures})

    expect(
        len(matrix) == len(Route) == 9,
        "offline equivalence matrix validates all nine Route branches",
        matrix,
    )


def test_public_output_comparison_sensitivity(base: Path) -> None:
    case = next(
        item for item in OFFLINE_ROUTE_CASES if item.route == Route.AGENT
    )
    event = EventNormalizer().user_message(
        case.text,
        "offline-matrix",
        "matrix-user",
        "matrix-comparison-sensitivity",
        event_id="evt_matrix_comparison_sensitivity",
        correlation_id="corr-matrix-comparison-sensitivity",
    )
    left_started_at = datetime.now(timezone.utc)
    left = build_offline_route_loop(
        base / "disabled",
        mode="disabled",
        case=case,
    ).handle_event(event)
    left_runtime_window = (
        left_started_at,
        datetime.now(timezone.utc),
    )
    right_started_at = datetime.now(timezone.utc)
    right = build_offline_route_loop(
        base / "shadow",
        mode="shadow",
        case=case,
    ).handle_event(event)
    right_runtime_window = (
        right_started_at,
        datetime.now(timezone.utc),
    )
    baseline_equal, baseline_differences = offline_public_outputs_equivalent(
        left,
        right,
        require_distinct_generated_ids=True,
        left_runtime_window=left_runtime_window,
        right_runtime_window=right_runtime_window,
    )
    right_document = right.to_dict()

    def set_path(
        document: dict[str, Any],
        path: tuple[str, ...],
        value: Any,
    ) -> None:
        selected: Any = document
        for part in path[:-1]:
            selected = (
                selected[int(part)]
                if isinstance(selected, list)
                else selected[part]
            )
        if isinstance(selected, list):
            selected[int(path[-1])] = value
        else:
            selected[path[-1]] = value

    def compare_candidate(
        candidate: dict[str, Any],
        *,
        runtime_window: tuple[datetime, datetime] = right_runtime_window,
    ) -> bool:
        equivalent, _ = offline_public_outputs_equivalent(
            left,
            candidate,
            require_distinct_generated_ids=True,
            left_runtime_window=left_runtime_window,
            right_runtime_window=runtime_window,
        )
        return equivalent

    missing_task = copy.deepcopy(right_document)
    set_path(
        missing_task,
        ("artifacts", "execution_result", "task_id"),
        None,
    )
    missing_task_equal = compare_candidate(missing_task)

    mismatched_task = copy.deepcopy(right_document)
    set_path(
        mismatched_task,
        ("artifacts", "execution_result", "task_id"),
        "task_aaaaaaaaaaaa",
    )
    mismatched_task_equal = compare_candidate(mismatched_task)

    stale_evidence = copy.deepcopy(right_document)
    observed_path = _BELIEF_CLAIM_PREFIX + ("0", "observed_at")
    observed_at = _parse_timestamp(_value_at(stale_evidence, observed_path))
    expect(
        observed_at is not None,
        "offline fixture exposes a valid evidence timestamp",
        _value_at(stale_evidence, observed_path),
    )
    set_path(
        stale_evidence,
        observed_path,
        (observed_at - timedelta(hours=1)).isoformat(),
    )
    stale_equal = compare_candidate(stale_evidence)

    invalid_expiry = copy.deepcopy(right_document)
    expiry_path = _BELIEF_CLAIM_PREFIX + ("0", "expires_at")
    expires_at = _parse_timestamp(
        _value_at(invalid_expiry, expiry_path)
    )
    age_path = _BELIEF_CLAIM_PREFIX + ("0", "age_seconds")
    ttl_remaining_path = _BELIEF_CLAIM_PREFIX + (
        "0",
        "ttl_remaining_seconds",
    )
    age_seconds = _value_at(invalid_expiry, age_path)
    ttl_remaining_seconds = _value_at(
        invalid_expiry,
        ttl_remaining_path,
    )
    ttl_path = _BELIEF_CLAIM_PREFIX + ("0", "ttl_seconds")
    ttl_seconds = _value_at(invalid_expiry, ttl_path)
    expect(
        expires_at is not None
        and isinstance(age_seconds, int)
        and not isinstance(age_seconds, bool)
        and isinstance(ttl_remaining_seconds, int)
        and not isinstance(ttl_remaining_seconds, bool)
        and isinstance(ttl_seconds, int)
        and not isinstance(ttl_seconds, bool),
        "offline fixture exposes internally checkable belief timing",
        {
            "expires_at": expires_at,
            "age_seconds": age_seconds,
            "ttl_remaining_seconds": ttl_remaining_seconds,
            "ttl_seconds": ttl_seconds,
        },
    )
    set_path(
        invalid_expiry,
        expiry_path,
        (expires_at + timedelta(seconds=1)).isoformat(),
    )
    invalid_expiry_equal = compare_candidate(invalid_expiry)

    invalid_age = copy.deepcopy(right_document)
    maximum_age = max(
        0,
        int(
            (
                right_runtime_window[1] - observed_at
            ).total_seconds()
        ),
    )
    set_path(invalid_age, age_path, maximum_age + 1)
    invalid_age_equal = compare_candidate(invalid_age)

    invalid_ttl_remaining = copy.deepcopy(right_document)
    minimum_ttl_remaining = int(
        (
            expires_at - right_runtime_window[1]
        ).total_seconds()
    )
    set_path(
        invalid_ttl_remaining,
        ttl_remaining_path,
        minimum_ttl_remaining - 1,
    )
    invalid_ttl_remaining_equal = compare_candidate(
        invalid_ttl_remaining
    )

    inconsistent_refresh = copy.deepcopy(right_document)
    set_path(inconsistent_refresh, age_path, 3)
    set_path(
        inconsistent_refresh,
        ttl_remaining_path,
        ttl_seconds - 1,
    )
    inconsistent_runtime_window = (
        right_runtime_window[0],
        max(
            right_runtime_window[1],
            observed_at + timedelta(seconds=3, milliseconds=500),
        ),
    )
    inconsistent_refresh_equal = compare_candidate(
        inconsistent_refresh,
        runtime_window=inconsistent_runtime_window,
    )

    shared_session = copy.deepcopy(right_document)
    left_document = left.to_dict()
    left_task_id = _value_at(
        left_document,
        ("artifacts", "task_packet", "task_id"),
    )
    left_agent_session_id = _value_at(
        left_document,
        ("artifacts", "task_packet", "agent_execution_session_id"),
    )
    for path in _AGENT_TASK_ID_PATHS:
        set_path(shared_session, path, left_task_id)
    for path in _AGENT_SESSION_ID_PATHS:
        set_path(shared_session, path, left_agent_session_id)
    shared_session_equal = compare_candidate(shared_session)

    weakened_artifact = copy.deepcopy(right_document)
    set_path(
        weakened_artifact,
        ("artifacts", "controller", "status"),
        "degraded",
    )
    weakened_artifact_equal = compare_candidate(weakened_artifact)
    invalid_latency = copy.deepcopy(right_document)
    set_path(invalid_latency, _LATENCY_PATH, float("nan"))
    invalid_latency_equal = compare_candidate(invalid_latency)
    expect(
        baseline_equal
        and not missing_task_equal
        and not mismatched_task_equal
        and not stale_equal
        and not invalid_expiry_equal
        and not invalid_age_equal
        and not invalid_ttl_remaining_equal
        and not inconsistent_refresh_equal
        and not shared_session_equal
        and not weakened_artifact_equal
        and not invalid_latency_equal,
        "complete-output comparison detects binding, freshness, isolation, and artifact weakening",
        {
            "baseline": baseline_differences,
            "missing_task_equal": missing_task_equal,
            "mismatched_task_equal": mismatched_task_equal,
            "stale_equal": stale_equal,
            "invalid_expiry_equal": invalid_expiry_equal,
            "invalid_age_equal": invalid_age_equal,
            "invalid_ttl_remaining_equal": (
                invalid_ttl_remaining_equal
            ),
            "inconsistent_refresh_equal": inconsistent_refresh_equal,
            "shared_session_equal": shared_session_equal,
            "weakened_artifact_equal": weakened_artifact_equal,
            "invalid_latency_equal": invalid_latency_equal,
        },
    )


def test_situation_trace_retention(base: Path) -> None:
    store = WorldStateStore(
        base / "situation-trace-retention",
        exclusive_writer=True,
        writer_owner="situation-trace-retention",
    )
    seeded = [
        {
            "trace_type": "situation_observed",
            "schema_version": "veyra.situation_trace.v1",
            "situation_id": f"sit_retention_{index}",
            "sequence": index,
        }
        for index in range(8)
    ]
    for row in seeded:
        store.append_jsonl("situation_trace.jsonl", row)

    policy = RetentionPolicy(store, limits={"situation_trace.jsonl": 3})
    summary_row = next(
        item
        for item in policy.summary()["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    before = store.path_for("situation_trace.jsonl").read_bytes()
    preview = policy.enforce(dry_run=True)
    preview_row = next(
        item
        for item in preview["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    expect(
        "situation_trace.jsonl" in RetentionPolicy.DEFAULT_LIMITS
        and summary_row["target_limit"] == 3
        and summary_row["status"] == "over_limit"
        and preview_row["status"] == "would_rotate"
        and store.path_for("situation_trace.jsonl").read_bytes() == before
        and not (store.root / preview_row["archive_path"]).exists(),
        "situation trace retention preview is explicit and non-mutating",
        {"summary": summary_row, "preview": preview_row},
    )

    enforced = policy.enforce()
    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    retained = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    expect(
        enforced_row["status"] == "rotated"
        and enforced_row["archive_compression"] == "gzip"
        and enforced_row["pruned_entries"] == 5
        and [row["sequence"] for row in retained] == [5, 6, 7]
        and [row["sequence"] for row in archived] == [0, 1, 2, 3, 4]
        and [row["sequence"] for row in archived + retained] == list(range(8))
        and all(
            row["schema_version"] == "veyra.situation_trace.v1"
            for row in archived + retained
        ),
        "situation trace archives before truncation and preserves ordered lifecycle history",
        {
            "rotation": enforced_row,
            "archived": archived,
            "retained": retained,
        },
    )


def test_retention_defers_pending_trace_outbox(base: Path) -> None:
    store = WorldStateStore(
        base / "pending-outbox-retention",
        exclusive_writer=True,
        writer_owner="pending-outbox-retention",
    )
    evaluator = SituationEvaluator(store)
    event = EventNormalizer().normalize(
        EventType.OBSERVATION,
        {"component": "retention-recovery-fixture"},
        channel="runtime",
        user_id="retention-user",
        session_id="retention-session",
        event_id="evt_retention_pending_outbox",
        correlation_id="corr-retention-pending-outbox",
    )
    original_append = store.append_jsonl
    interrupted = False

    def interrupt_after_trace_append(
        name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal interrupted
        original_append(name, payload)
        if name == "situation_trace.jsonl" and not interrupted:
            interrupted = True
            raise OSError("injected interruption before trace outbox ack")

    store.append_jsonl = interrupt_after_trace_append  # type: ignore[method-assign]
    try:
        evaluator.observe(event)
    finally:
        store.append_jsonl = original_append  # type: ignore[method-assign]
    pending_state = store.read_json("situation_state.json")
    pending_entry = pending_state.get("trace_outbox", [])[0]
    pending_transition_id = str(pending_entry.get("transition_id") or "")
    for index in range(7):
        store.append_jsonl(
            "situation_trace.jsonl",
            {
                "schema_version": "veyra.situation_trace.v1",
                "transition_id": f"sitxn_retention_fixture_{index}",
                "trace_type": "situation_observed",
                "sequence": index,
            },
        )

    policy = RetentionPolicy(store, limits={"situation_trace.jsonl": 3})
    before = store.path_for("situation_trace.jsonl").read_bytes()
    deferred = policy.enforce()
    deferred_row = next(
        item
        for item in deferred["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    expect(
        interrupted
        and pending_state.get("trace_outbox_count") == 1
        and deferred_row.get("status") == "deferred_pending_outbox"
        and deferred_row.get("pending_trace_outbox") == 1
        and store.path_for("situation_trace.jsonl").read_bytes() == before,
        "retention cannot rotate a trace that still awaits outbox acknowledgement",
        {
            "state": pending_state,
            "retention": deferred_row,
        },
    )

    repair = evaluator.flush_trace_outbox()
    enforced = policy.enforce()
    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    live = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    all_rows = archived + live
    expect(
        repair.get("deduplicated") == 1
        and repair.get("pending") == 0
        and enforced_row.get("status") == "rotated"
        and sum(
            row.get("transition_id") == pending_transition_id
            for row in all_rows
        )
        == 1,
        "outbox acknowledgement precedes retention and preserves one transition",
        {
            "repair": repair,
            "retention": enforced_row,
            "transition_id": pending_transition_id,
            "rows": all_rows,
        },
    )


def test_retention_rotation_is_atomic_with_trace_delivery(base: Path) -> None:
    store = WorldStateStore(
        base / "atomic-retention",
        exclusive_writer=True,
        writer_owner="atomic-retention",
    )
    for index in range(8):
        store.append_jsonl(
            "situation_trace.jsonl",
            {
                "schema_version": "veyra.situation_trace.v1",
                "transition_id": f"sitxn_atomic_retention_seed_{index}",
                "trace_type": "situation_observed",
                "sequence": index,
            },
        )
    evaluator = SituationEvaluator(store)
    event = EventNormalizer().normalize(
        EventType.OBSERVATION,
        {"component": "atomic-retention-fixture"},
        channel="runtime",
        user_id="atomic-retention-user",
        session_id="atomic-retention-session",
        event_id="evt_atomic_retention",
        correlation_id="corr-atomic-retention",
    )
    original_append = store.append_jsonl
    original_rotate = store.rotate_jsonl
    worker_started = Event()
    worker_finished = Event()
    worker_threads: list[Thread] = []
    blocked_while_locked = False
    interrupted = False

    def interrupt_target_after_append(
        name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal interrupted
        original_append(name, payload)
        if (
            name == "situation_trace.jsonl"
            and payload.get("source_event_id") == event.event_id
            and not interrupted
        ):
            interrupted = True
            raise OSError("injected concurrent interruption before ack")

    def observe_during_rotation() -> None:
        worker_started.set()
        evaluator.observe(event)
        worker_finished.set()

    def rotate_with_concurrent_delivery(
        name: str,
        *,
        limit: int,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        nonlocal blocked_while_locked
        if name == "situation_trace.jsonl":
            worker = Thread(
                target=observe_during_rotation,
                name="atomic-retention-observer",
                daemon=True,
            )
            worker_threads.append(worker)
            worker.start()
            worker_started.wait(timeout=2)
            blocked_while_locked = not worker_finished.wait(timeout=0.05)
        return original_rotate(name, limit=limit, dry_run=dry_run)

    store.append_jsonl = interrupt_target_after_append  # type: ignore[method-assign]
    store.rotate_jsonl = rotate_with_concurrent_delivery  # type: ignore[method-assign]
    try:
        policy = RetentionPolicy(
            store,
            limits={"situation_trace.jsonl": 3},
        )
        enforced = policy.enforce()
    finally:
        store.rotate_jsonl = original_rotate  # type: ignore[method-assign]
        for worker in worker_threads:
            worker.join(timeout=5)
        store.append_jsonl = original_append  # type: ignore[method-assign]

    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    pending_state = store.read_json("situation_state.json")
    pending_entry = pending_state.get("trace_outbox", [])[0]
    transition_id = str(pending_entry.get("transition_id") or "")
    repair = evaluator.flush_trace_outbox()
    live = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    expect(
        blocked_while_locked
        and worker_finished.is_set()
        and interrupted
        and enforced_row.get("status") == "rotated"
        and pending_state.get("trace_outbox_count") == 1
        and repair.get("deduplicated") == 1
        and sum(
            row.get("transition_id") == transition_id
            for row in archived + live
        )
        == 1,
        "retention check and rotation serialize against concurrent trace delivery",
        {
            "retention": enforced_row,
            "pending_state": pending_state,
            "repair": repair,
            "transition_id": transition_id,
        },
    )


def test_default_record_only_contract(base: Path) -> None:
    store = WorldStateStore(
        base / "default-record-only",
        exclusive_writer=True,
        writer_owner="default-record-only",
    )
    loop = AwarenessLoop(store, RuntimeEntity(store))
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_record_only_default",
    )
    result = loop.handle_event(event)
    expect(
        loop.event_awareness.mode == "record_only"
        and "situation" not in result.artifacts
        and loop.situation_evaluator.list(user_id="user-a") == []
        and loop.event_inbox.get_record(event.event_id).get("status") == "pending",
        "default record-only mode performs one admission without changing public artifacts",
        {
            "mode": loop.event_awareness.mode,
            "result": result.to_dict(),
            "inbox": loop.event_inbox.get_record(event.event_id),
        },
    )


def test_shadow_foreground_atomic_claim(base: Path) -> None:
    loop = build_loop(base / "atomic-foreground", mode="shadow")
    event = EventNormalizer().user_message(
        "foreground atomic claim fixture",
        "offline-race",
        "atomic-user",
        "atomic-session",
        event_id="evt_atomic_foreground",
    )
    original_atomic = loop.event_inbox.enqueue_and_claim
    atomic_calls: list[dict[str, Any]] = []
    background_claims: list[dict[str, Any] | None] = []

    def atomic_with_background_probe(
        incoming: Any,
        consumer_id: str,
        lease_seconds: float = 60.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        admission = original_atomic(
            incoming,
            consumer_id,
            lease_seconds,
            **kwargs,
        )
        atomic_calls.append(admission)
        # The atomic mutation has returned, but foreground still has not
        # observed or completed the event. A background consumer must not be
        # able to acquire the already-leased record in this window.
        background_claims.append(
            loop.event_inbox.claim(
                "awareness-shadow-background-race",
                lease_seconds=60,
            )
        )
        return admission

    def legacy_split_call_forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("shadow foreground used legacy split enqueue/claim")

    loop.event_inbox.enqueue_and_claim = atomic_with_background_probe  # type: ignore[method-assign]
    loop.event_inbox.enqueue = legacy_split_call_forbidden  # type: ignore[method-assign]
    loop.event_inbox.claim_by_id = legacy_split_call_forbidden  # type: ignore[method-assign]
    observed = loop.event_awareness.begin(event)
    record = loop.event_inbox.get_record(event.event_id)
    expect(
        len(atomic_calls) == 1
        and atomic_calls[0].get("claimed") is True
        and background_claims == [None]
        and observed.get("status") == "observed"
        and isinstance(record, dict)
        and record.get("status") == "completed"
        and record.get("attempts") == 1,
        "shadow foreground atomically admits and claims before background can race",
        {
            "admission": atomic_calls,
            "background_claims": background_claims,
            "observed": observed,
            "record": record,
        },
    )


def test_cross_event_resolution_binding(base: Path) -> None:
    loop = build_loop(base / "cross-event-binding", mode="shadow")
    normalizer = EventNormalizer()
    event_a = normalizer.user_message(
        "event A",
        "api",
        "binding-user",
        "binding-session",
        event_id="evt_binding_a",
        correlation_id="corr-binding-a",
    )
    event_b = normalizer.user_message(
        "event B",
        "api",
        "binding-user",
        "binding-session",
        event_id="evt_binding_b",
        correlation_id="corr-binding-b",
    )
    begin_a = loop.event_awareness.begin(event_a)
    begin_b = loop.event_awareness.begin(event_b)
    result_b = LoopResult(
        event_id=event_b.event_id,
        route=Route.DIRECT_ANSWER,
        status="success",
        response="result B",
        risk_level=RiskLevel.R0,
    )
    rejected = loop.event_awareness.finalize(
        event_b,
        result_b,
        {"trace_id": "rt_binding_b_rejected"},
        situation_id=str(begin_a.get("situation_id") or ""),
        canonical_event_id=event_b.event_id,
    )
    state_a = loop.situation_evaluator.get(
        str(begin_a.get("situation_id") or ""),
        user_id="binding-user",
        session_id="binding-session",
    )
    state_b_before = loop.situation_evaluator.get(
        str(begin_b.get("situation_id") or ""),
        user_id="binding-user",
        session_id="binding-session",
    )
    accepted = loop.event_awareness.finalize(
        event_b,
        result_b,
        {"trace_id": "rt_binding_b"},
        situation_id=str(begin_b.get("situation_id") or ""),
        canonical_event_id=event_b.event_id,
    )
    state_b_after = loop.situation_evaluator.get(
        str(begin_b.get("situation_id") or ""),
        user_id="binding-user",
        session_id="binding-session",
    )
    expect(
        isinstance(rejected, dict)
        and rejected.get("status") == "degraded"
        and isinstance(state_a, dict)
        and state_a.get("outcome") is None
        and isinstance(state_b_before, dict)
        and state_b_before.get("outcome") is None
        and isinstance(accepted, dict)
        and accepted.get("situation_id") == begin_b.get("situation_id")
        and isinstance(state_b_after, dict)
        and state_b_after.get("source_event_id") == event_b.event_id
        and state_b_after.get("outcome", {}).get("value", {}).get("status")
        == "success",
        "event B cannot write its resolution into event A's Situation",
        {
            "rejected": rejected,
            "accepted": accepted,
            "event_a": state_a,
            "event_b_before": state_b_before,
            "event_b_after": state_b_after,
        },
    )

    canonical = normalizer.user_message(
        "same delivery",
        "api",
        "dedupe-binding-user",
        "dedupe-binding-session",
        event_id="evt_dedupe_canonical",
        correlation_id="corr-dedupe-canonical",
        dedupe_key="provider-delivery-1",
    )
    suppressed = normalizer.user_message(
        "same delivery",
        "api",
        "dedupe-binding-user",
        "dedupe-binding-session",
        event_id="evt_dedupe_suppressed",
        correlation_id="corr-dedupe-suppressed",
        dedupe_key="provider-delivery-1",
    )
    canonical_begin = loop.event_awareness.begin(canonical)
    suppressed_begin = loop.event_awareness.begin(suppressed)
    suppressed_result = LoopResult(
        event_id=suppressed.event_id,
        route=Route.BLOCK,
        status="blocked",
        response="suppressed result",
        risk_level=RiskLevel.R5,
    )
    suppressed_finalize = loop.event_awareness.finalize(
        suppressed,
        suppressed_result,
        {"trace_id": "rt_dedupe_suppressed"},
        situation_id=str(suppressed_begin.get("situation_id") or ""),
        canonical_event_id=str(suppressed_begin.get("event_id") or ""),
    )
    canonical_state = loop.situation_evaluator.get(
        str(canonical_begin.get("situation_id") or ""),
        user_id="dedupe-binding-user",
        session_id="dedupe-binding-session",
    )
    expect(
        canonical_begin.get("finalize_allowed") is True
        and suppressed_begin.get("event_id") == canonical.event_id
        and suppressed_begin.get("finalize_allowed") is False
        and isinstance(suppressed_finalize, dict)
        and suppressed_finalize.get("status") == "degraded"
        and isinstance(canonical_state, dict)
        and canonical_state.get("outcome") is None,
        "a different event suppressed by shared dedupe cannot finalize the canonical Situation",
        {
            "canonical_begin": canonical_begin,
            "suppressed_begin": suppressed_begin,
            "suppressed_finalize": suppressed_finalize,
            "canonical_state": canonical_state,
        },
    )

    pending_canonical = normalizer.user_message(
        "pending canonical delivery",
        "api",
        "pending-dedupe-user",
        "pending-dedupe-session",
        event_id="evt_pending_canonical",
        correlation_id="corr-pending-canonical",
        dedupe_key="provider-delivery-pending",
    )
    pending_suppressed = normalizer.user_message(
        "pending canonical delivery",
        "api",
        "pending-dedupe-user",
        "pending-dedupe-session",
        event_id="evt_pending_suppressed",
        correlation_id="corr-pending-suppressed",
        dedupe_key="provider-delivery-pending",
    )
    loop.event_awareness.publish(pending_canonical)
    pending_suppressed_begin = loop.event_awareness.begin(pending_suppressed)
    pending_record = loop.event_inbox.get_record(pending_canonical.event_id)
    pending_state = loop.situation_evaluator.get(
        str(pending_suppressed_begin.get("situation_id") or ""),
        user_id="pending-dedupe-user",
        session_id="pending-dedupe-session",
    )
    expect(
        pending_suppressed_begin.get("event_id") == pending_canonical.event_id
        and pending_suppressed_begin.get("finalize_allowed") is False
        and isinstance(pending_record, dict)
        and pending_record.get("status") == "completed"
        and isinstance(pending_state, dict)
        and pending_state.get("source_event_id") == pending_canonical.event_id
        and pending_state.get("outcome") is None,
        "claiming a pending canonical event never authorizes the suppressed event to finalize",
        {
            "begin": pending_suppressed_begin,
            "record": pending_record,
            "situation": pending_state,
        },
    )


def test_duplicate_foreground_skips_finalize(base: Path) -> None:
    loop = build_loop(base / "duplicate-foreground", mode="shadow")
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "duplicate-user",
        "duplicate-session",
        event_id="evt_duplicate_foreground",
        correlation_id="corr-duplicate-foreground",
    )
    first = loop.handle_event(event)
    first_situation = loop.situation_evaluator.list(
        user_id="duplicate-user",
        session_id="duplicate-session",
        correlation_id="corr-duplicate-foreground",
    )[0]
    second = loop.handle_event(event)
    second_situation = loop.situation_evaluator.list(
        user_id="duplicate-user",
        session_id="duplicate-session",
        correlation_id="corr-duplicate-foreground",
    )[0]
    record = loop.event_inbox.get_record(event.event_id)
    outputs_equal, output_differences = offline_public_outputs_equivalent(
        first,
        second,
        require_distinct_generated_ids=True,
    )
    expect(
        outputs_equal
        and isinstance(record, dict)
        and record.get("status") == "completed"
        and record.get("delivery_count") == 2
        and len(first_situation.get("decision_history") or []) == 1
        and len(first_situation.get("outcome_history") or []) == 1
        and len(second_situation.get("decision_history") or []) == 1
        and len(second_situation.get("outcome_history") or []) == 1,
        "duplicate foreground delivery does not finalize the same outcome twice",
        {
            "first": first.to_dict(),
            "second": second.to_dict(),
            "record": record,
            "situation": second_situation,
            "output_differences": output_differences,
        },
    )


def test_duplicate_foreground_repairs_failed_resolution(base: Path) -> None:
    loop = build_loop(base / "duplicate-resolution-repair", mode="shadow")
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "resolution-repair-user",
        "resolution-repair-session",
        event_id="evt_resolution_repair",
        correlation_id="corr-resolution-repair",
    )
    original_resolution = loop.situation_evaluator.record_resolution
    attempts = 0

    def fail_first_finalize_attempts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("injected resolution persistence failure")
        return original_resolution(*args, **kwargs)

    loop.situation_evaluator.record_resolution = fail_first_finalize_attempts  # type: ignore[method-assign]
    first = loop.handle_event(event)
    incomplete = loop.situation_evaluator.list(
        user_id="resolution-repair-user",
        session_id="resolution-repair-session",
        correlation_id="corr-resolution-repair",
    )[0]
    second = loop.handle_event(event)
    repaired = loop.situation_evaluator.list(
        user_id="resolution-repair-user",
        session_id="resolution-repair-session",
        correlation_id="corr-resolution-repair",
    )[0]
    outputs_equal, output_differences = offline_public_outputs_equivalent(
        first,
        second,
        require_distinct_generated_ids=True,
    )
    expect(
        outputs_equal
        and attempts == 3
        and len(incomplete.get("decision_history") or []) == 0
        and len(incomplete.get("outcome_history") or []) == 0
        and len(repaired.get("decision_history") or []) == 1
        and len(repaired.get("outcome_history") or []) == 1,
        "duplicate foreground delivery repairs an atomic resolution that previously failed",
        {
            "attempts": attempts,
            "incomplete": incomplete,
            "repaired": repaired,
            "output_differences": output_differences,
        },
    )


def test_background_trace_failure_recovery(base: Path) -> None:
    loop = build_loop(base / "trace-recovery", mode="record_only")
    event = EventNormalizer().normalize(
        EventType.COMPONENT_DEGRADED,
        {
            "component": "offline-trace-fixture",
            "observed_status": "degraded",
            "salience_components": {"impact": 0.7},
        },
        channel="runtime",
        user_id="trace-recovery-user",
        session_id="trace-recovery-session",
        event_id="evt_trace_recovery",
        correlation_id="corr-trace-recovery",
    )
    admitted = loop.publish_event(event)
    original_append = loop.state_store.append_jsonl
    injected_failures = 0

    def fail_first_situation_trace(name: str, payload: dict[str, Any]) -> None:
        nonlocal injected_failures
        if name == "situation_trace.jsonl" and injected_failures == 0:
            injected_failures += 1
            raise OSError("injected transient situation trace append failure")
        original_append(name, payload)

    loop.state_store.append_jsonl = fail_first_situation_trace  # type: ignore[method-assign]
    try:
        first = loop.process_event_inbox(limit=1)
    finally:
        loop.state_store.append_jsonl = original_append  # type: ignore[method-assign]
    first_record = loop.event_inbox.get_record(event.event_id)
    pending_state = loop.state_store.read_json("situation_state.json")
    pending_trace = loop.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    expect(
        admitted.get("status") == "enqueued"
        and injected_failures == 1
        and first.get("processed_count") == 1
        and first.get("failed_count") == 0
        and isinstance(first_record, dict)
        and first_record.get("status") == "completed"
        and pending_state.get("trace_outbox_count") == 1
        and pending_trace == [],
        "trace append failure completes durable state while retaining a trace outbox",
        {
            "first": first,
            "record": first_record,
            "state": pending_state,
            "trace": pending_trace,
        },
    )

    # A later background tick performs a strict outbox flush before looking for
    # queue work. Recovery therefore does not depend on illegally retrying an
    # already-completed event.
    second = loop.process_event_inbox(limit=1)
    completed_record = loop.event_inbox.get_record(event.event_id)
    recovered_state = loop.state_store.read_json("situation_state.json")
    recovered_trace = loop.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    situations = loop.situation_evaluator.list(
        user_id=event.source.user_id,
        session_id=event.source.session_id,
        correlation_id=event.correlation_id,
    )
    expect(
        second.get("processed_count") == 0
        and second.get("failed_count") == 0
        and isinstance(completed_record, dict)
        and completed_record.get("status") == "completed"
        and recovered_state.get("trace_outbox_count") == 0
        and len(recovered_trace) == 1
        and recovered_trace[0].get("source_event_id") == event.event_id
        and recovered_trace[0].get("trace_type") == "situation_observed"
        and len(situations) == 1,
        "the next background tick repairs trace delivery for the completed event",
        {
            "second": second,
            "record": completed_record,
            "state": recovered_state,
            "trace": recovered_trace,
            "situations": situations,
        },
    )


def test_background_event_projection(base: Path) -> None:
    loop = build_loop(base / "background", mode="record_only")
    event = EventNormalizer().normalize(
        EventType.COMPONENT_DEGRADED,
        {
            "component": "openclaw",
            "observed_status": "unreachable",
            "salience_components": {
                "goal_relevance": 0.7,
                "urgency": 0.8,
                "impact": 0.6,
            },
        },
        channel="runtime",
        user_id="user-a",
        session_id="system-monitor",
        event_id="evt_component_degraded",
        correlation_id="corr-component-degraded",
        subject={"kind": "component", "id": "openclaw"},
        evidence_refs=[{"ref_id": "probe-openclaw-1", "source": "runtime_probe"}],
        dedupe_key="openclaw-unreachable-probe-1",
        privacy_scope={"tenant": "user-a", "visibility": "private"},
    )
    admitted = loop.publish_event(event)
    expect(admitted.get("status") == "enqueued", "internal event is admitted without execution", admitted)
    active_loop = build_active_loop(loop, event_consumer=loop.process_event_inbox)
    tick = active_loop.tick(reason="event-driven-smoke")
    event_step = next(
        step for step in tick.get("steps", []) if step.get("name") == "event_inbox"
    )
    expect(
        tick.get("status") == "success"
        and event_step.get("result", {}).get("processed_count") == 1,
        "active runtime tick projects a pending event into a situation",
        tick,
    )
    situation = loop.situation_evaluator.list(
        user_id="user-a",
        session_id="system-monitor",
        correlation_id="corr-component-degraded",
    )[0]
    expect(
        situation.get("status") == "observed"
        and not situation.get("decision")
        and situation.get("salience_score", 0) > 0,
        "background observation creates salience but no decision or authority",
        situation,
    )
    expect(
        loop.situation_evaluator.list(user_id="user-b") == [],
        "situation reads remain isolated by user",
    )

    degraded_calls: list[int] = []

    def degraded_consumer(*, limit: int) -> dict[str, Any]:
        degraded_calls.append(limit)
        return {"status": "degraded", "processed_count": 0, "failed_count": 1}

    degraded_tick = build_active_loop(
        loop,
        event_consumer=degraded_consumer,
    ).tick(reason="event-consumer-degraded-smoke")
    degraded_step = next(
        step for step in degraded_tick.get("steps", []) if step.get("name") == "event_inbox"
    )
    expect(
        degraded_calls == [100]
        and degraded_step.get("status") == "degraded"
        and degraded_tick.get("status") == "degraded"
        and any(step.get("name") == "retention" for step in degraded_tick.get("steps", [])),
        "degraded event consumer degrades the tick while later maintenance still runs",
        degraded_tick,
    )


def test_verification_truth_boundary(base: Path) -> None:
    loop = build_loop(base / "verification")
    normalizer = EventNormalizer()
    uncertain_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-uncertain"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_uncertain",
    )
    loop.event_awareness.begin(uncertain_event)
    expect(
        loop.event_inbox.get_record(uncertain_event.event_id).get("status")
        == "completed"
        and loop.process_event_inbox(limit=10).get("processed_count") == 0,
        "foreground shadow releases its inbox lease immediately after observation",
        loop.event_inbox.get_record(uncertain_event.event_id),
    )
    uncertain_result = LoopResult(
        event_id=uncertain_event.event_id,
        route=Route.AGENT,
        status="needs_more_probe",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": {
                "status": "needs_more_probe",
                "verdict": "execution_success_without_structured_evidence",
            }
        },
    )
    loop.event_awareness.finalize(
        uncertain_event,
        uncertain_result,
        {"trace_id": "rt_uncertain"},
    )
    uncertain = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=uncertain_event.event_id,
    )[0]
    expect(
        uncertain.get("status") == "monitoring"
        and uncertain.get("outcome", {}).get("is_fact") is False,
        "incomplete verification stays non-factual and open for monitoring",
        uncertain,
    )

    forged_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-forged"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_forged",
    )
    loop.event_awareness.begin(forged_event)
    forged_result = LoopResult(
        event_id=forged_event.event_id,
        route=Route.AGENT,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": {
                "status": "verified_success",
                "verdict": "forged",
                "evidence": {"source": "untrusted-artifact"},
            },
            "execution_trace": {"trace_id": "exec_never_persisted"},
        },
    )
    loop.event_awareness.finalize(
        forged_event,
        forged_result,
        {"trace_id": "rt_forged"},
    )
    forged = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=forged_event.event_id,
    )[0]
    forged_due = loop.situation_evaluator.due_candidates(
        user_id="user-a",
        session_id="session-a",
    )
    expect(
        forged.get("status") == "monitoring"
        and forged.get("decision", {}).get("value", {}).get("status")
        == "verification_pending"
        and forged.get("outcome", {}).get("is_fact") is False
        and forged.get("outcome", {}).get("value", {}).get("status")
        == "verification_pending"
        and forged.get("outcome", {})
        .get("value", {})
        .get("verification", {})
        .get("claimed_status")
        == "verified_success"
        and any(
            item.get("situation_id") == forged.get("situation_id")
            for item in forged_due
        ),
        "unpersisted verification stays non-factual and open for reevaluation",
        forged,
    )

    route_mismatch_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-route-mismatch"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_route_mismatch",
    )
    loop.event_awareness.begin(route_mismatch_event)
    route_mismatch_verification = {
        "status": "verified_success",
        "verdict": "probe_result_has_structured_evidence",
        "evidence": {
            "source": "runtime_probe",
            "observed_at": route_mismatch_event.timestamp,
        },
    }
    probe_trace = loop.execution_trace.record(
        {
            "event_id": route_mismatch_event.event_id,
            "route": Route.PROBE.value,
            "task_id": route_mismatch_event.event_id,
            "executor": "probe:test",
            "status": "verified_success",
            "verification": route_mismatch_verification,
        }
    )
    route_mismatch_result = LoopResult(
        event_id=route_mismatch_event.event_id,
        route=Route.AGENT,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "execution_result": {"task_id": route_mismatch_event.event_id},
            "verification": route_mismatch_verification,
            "execution_trace": probe_trace,
        },
    )
    loop.event_awareness.finalize(
        route_mismatch_event,
        route_mismatch_result,
        {"trace_id": "rt_route_mismatch"},
    )
    route_mismatch = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=route_mismatch_event.event_id,
    )[0]
    route_mismatch_due = loop.situation_evaluator.due_candidates(
        user_id="user-a",
        session_id="session-a",
    )
    expect(
        route_mismatch.get("status") == "monitoring"
        and route_mismatch.get("decision", {}).get("value", {}).get("status")
        == "verification_pending"
        and route_mismatch.get("outcome", {}).get("is_fact") is False
        and route_mismatch.get("outcome", {}).get("value", {}).get("status")
        == "verification_pending"
        and any(
            item.get("situation_id") == route_mismatch.get("situation_id")
            for item in route_mismatch_due
        ),
        "a mismatched persisted trace cannot certify or close an outcome",
        route_mismatch,
    )

    verified_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-verified"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_verified",
    )
    loop.event_awareness.begin(verified_event)
    verification = {
        "status": "verified_success",
        "verdict": "probe_result_has_structured_evidence",
        "evidence": {"source": "runtime_probe", "observed_at": verified_event.timestamp},
    }
    execution_trace = loop.execution_trace.record(
        {
            "event_id": verified_event.event_id,
            "route": Route.PROBE.value,
            "task_id": "task-verified",
            "executor": "probe:test",
            "status": "verified_success",
            "verification": verification,
        }
    )
    verified_result = LoopResult(
        event_id=verified_event.event_id,
        route=Route.PROBE,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": verification,
            "execution_trace": execution_trace,
        },
    )
    loop.event_awareness.finalize(
        verified_event,
        verified_result,
        {"trace_id": "rt_verified"},
    )
    verified = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=verified_event.event_id,
    )[0]
    trace_count_before_replay = len(
        loop.state_store.read_jsonl("situation_trace.jsonl", limit=100)
    )
    verified_due = loop.situation_evaluator.due_candidates(
        user_id="user-a",
        session_id="session-a",
    )
    loop.event_awareness.finalize(
        verified_event,
        verified_result,
        {"trace_id": "rt_verified_replay"},
    )
    trace_count_after_replay = len(
        loop.state_store.read_jsonl("situation_trace.jsonl", limit=100)
    )
    original_read_jsonl = loop.state_store.read_jsonl

    def execution_trace_temporarily_unavailable(
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if name == "execution_trace.jsonl":
            return []
        return original_read_jsonl(name, *args, **kwargs)

    loop.state_store.read_jsonl = (  # type: ignore[method-assign]
        execution_trace_temporarily_unavailable
    )
    try:
        loop.event_awareness.finalize(
            verified_event,
            verified_result,
            {"trace_id": "rt_verified_unavailable_replay"},
        )
    finally:
        loop.state_store.read_jsonl = original_read_jsonl  # type: ignore[method-assign]
    verified_after_unavailable_replay = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=verified_event.event_id,
    )[0]
    trace_count_after_unavailable_replay = len(
        loop.state_store.read_jsonl("situation_trace.jsonl", limit=100)
    )
    expect(
        verified.get("status") == "resolved"
        and verified.get("outcome", {}).get("is_fact") is True
        and not any(
            item.get("situation_id") == verified.get("situation_id")
            for item in verified_due
        )
        and verified.get("outcome", {}).get("evidence_refs", [])[1].get("source")
        == "execution_trace",
        "verified outcome becomes factual only through a persisted execution trace",
        verified,
    )
    expect(
        trace_count_after_replay == trace_count_before_replay,
        "replayed foreground finalization is idempotent",
        {
            "before": trace_count_before_replay,
            "after": trace_count_after_replay,
        },
    )
    expect(
        verified_after_unavailable_replay.get("status") == "resolved"
        and verified_after_unavailable_replay.get("outcome", {}).get("is_fact")
        is True
        and len(
            verified_after_unavailable_replay.get("outcome_history") or []
        )
        == len(verified.get("outcome_history") or [])
        and trace_count_after_unavailable_replay == trace_count_after_replay,
        "temporary execution-ledger unavailability cannot downgrade a persisted verified fact",
        {
            "before": verified,
            "after": verified_after_unavailable_replay,
            "trace_count_before": trace_count_after_replay,
            "trace_count_after": trace_count_after_unavailable_replay,
        },
    )


def test_scoped_debug_api(base: Path) -> None:
    loop = build_loop(base / "scoped-api")
    normalizer = EventNormalizer()
    for user_id in ("user-a", "user-b"):
        event = normalizer.normalize(
            EventType.OBSERVATION,
            {"metric": "health"},
            channel="runtime",
            user_id=user_id,
            session_id=f"{user_id}-session",
            event_id=f"evt_{user_id}",
        )
        loop.event_awareness.begin(event)
        loop.event_awareness.finalize(
            event,
            LoopResult(
                event_id=event.event_id,
                route=Route.DIRECT_ANSWER,
                status="success",
                response="",
                risk_level=RiskLevel.R0,
            ),
            {"trace_id": f"rt_{user_id}"},
        )

    app = FastAPI()
    app.include_router(
        build_debug_audit_router(
            {
                "state_store": loop.state_store,
                "awareness_loop": loop,
                "agency_core": HealthyRuntimeDependency(),
            }
        )
    )
    client = TestClient(app)
    expect(
        client.get("/awareness/situations").status_code == 422
        and client.get("/events/inbox").status_code == 422,
        "tenant-scoped debug APIs require an explicit user boundary",
    )
    user_a = client.get("/awareness/situations", params={"user_id": "user-a"})
    user_b_detail = client.get(
        "/awareness/situations/sit_missing",
        params={"user_id": "user-b"},
    )
    user_a_item = user_a.json()["items"][0]
    cross_tenant = client.get(
        f"/awareness/situations/{user_a_item['situation_id']}",
        params={"user_id": "user-b"},
    )
    inbox = client.get("/events/inbox", params={"user_id": "user-a"})
    public_state = client.get("/state")
    mode_status = client.get("/events/awareness/status")
    invalid_mode = client.post(
        "/events/awareness/config",
        json={"mode": "execute_everything"},
    )
    disabled_mode = client.post(
        "/events/awareness/config",
        json={"mode": "disabled"},
    )
    expect(
        user_a.status_code == 200
        and user_a.json().get("count") == 1
        and user_b_detail.status_code == 404
        and cross_tenant.status_code == 404
        and inbox.status_code == 200
        and inbox.json().get("stats", {}).get("total") == 1
        and public_state.status_code == 200
        and "event_inbox" not in public_state.json()
        and "situation_state" not in public_state.json()
        and "agent_memory" not in public_state.json()
        and "phase6_collaboration_state" not in public_state.json()
        and mode_status.json().get("mode") == "shadow"
        and invalid_mode.status_code == 422
        and disabled_mode.json().get("mode") == "disabled"
        and loop.state_store.read_json("ops_config.json")
        .get("event_awareness", {})
        .get("mode")
        == "disabled",
        "debug reads stay scoped and the human kill switch is validated and durable",
        {
            "situations": user_a.json(),
            "inbox": inbox.json(),
            "state_keys": sorted(public_state.json()),
        },
    )


def main() -> int:
    with TemporaryDirectory() as tmp:
        test_user_turn_shadow_equivalence(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_offline_route_equivalence_matrix(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_public_output_comparison_sensitivity(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_situation_trace_retention(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_retention_defers_pending_trace_outbox(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_retention_rotation_is_atomic_with_trace_delivery(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_default_record_only_contract(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_shadow_foreground_atomic_claim(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_cross_event_resolution_binding(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_duplicate_foreground_skips_finalize(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_duplicate_foreground_repairs_failed_resolution(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_background_trace_failure_recovery(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_background_event_projection(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_verification_truth_boundary(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_scoped_debug_api(Path(tmp))
    print("event driven awareness smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
