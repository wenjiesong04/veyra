from __future__ import annotations

import re
import time
from typing import Any

from core.world_state import WorldStateStore

# Markers that signal the user explicitly wants to continue the previous agent task,
# i.e. reuse the previous agent execution session instead of opening a fresh one.
CONTINUE_MARKERS_ZH = (
    "继续刚才",
    "继续上一个",
    "继续上个",
    "继续这个任务",
    "继续刚刚",
    "接着刚才",
    "接着上一步",
    "接着上面",
    "基于刚才",
    "基于刚刚",
    "基于上面",
    "基于上一步",
    "基于上述",
    "在此基础上",
    "在刚才基础上",
    "顺着刚才",
    "用刚才的结果",
    "根据刚才的结果",
)
CONTINUE_MARKERS_EN = (
    "continue the previous",
    "continue previous",
    "continue that task",
    "based on the previous",
    "based on that result",
    "based on the last",
    "building on that",
    "follow up on that",
)

STATE_FILE = "agent_session_index.json"
# Default window after which a previous agent session is considered stale and a
# continuation request still opens a fresh isolated session.
DEFAULT_REUSE_TTL_SECONDS = 30 * 60


class AgentSessionRouter:
    """Decides which agent execution session a task runs in.

    Policy:
    - ``ephemeral_per_task`` (default): every independent task gets its own isolated
      agent execution session so unrelated requests never share a polluted window.
    - ``continue_previous_task``: only when the user explicitly asks to continue, and a
      recent previous session exists for the same dialogue session.
    """

    def __init__(self, state_store: WorldStateStore, *, reuse_ttl_seconds: float = DEFAULT_REUSE_TTL_SECONDS) -> None:
        self.state_store = state_store
        self.reuse_ttl_seconds = reuse_ttl_seconds

    def resolve(self, *, dialogue_session_id: str, text: str) -> dict[str, Any]:
        """Return the agent session plan for this turn.

        Returns a dict with ``policy``, ``reuse_session_id`` (or ``None``) and a
        compact ``trace`` for audit. When ``reuse_session_id`` is ``None`` the task
        packet builder mints a fresh ``agent-exec:<task_id>`` key.
        """
        wants_continue = self._wants_continue(text)
        if not wants_continue:
            return {
                "policy": "ephemeral_per_task",
                "reuse_session_id": None,
                "trace": {"requested_continue": False, "reason": "default_isolated_session"},
            }
        previous = self._previous_for(dialogue_session_id)
        if not previous:
            return {
                "policy": "ephemeral_per_task",
                "reuse_session_id": None,
                "trace": {"requested_continue": True, "reason": "no_previous_session"},
            }
        if self._is_expired(previous):
            return {
                "policy": "ephemeral_per_task",
                "reuse_session_id": None,
                "trace": {"requested_continue": True, "reason": "previous_session_expired"},
            }
        return {
            "policy": "continue_previous_task",
            "reuse_session_id": str(previous.get("agent_execution_session_id") or "") or None,
            "trace": {
                "requested_continue": True,
                "reason": "reusing_previous_session",
                "previous_task_id": previous.get("task_id"),
                "previous_user_goal": previous.get("user_goal"),
            },
        }

    def record(
        self,
        *,
        dialogue_session_id: str,
        agent_execution_session_id: str,
        task_id: str,
        user_goal: str,
    ) -> None:
        if not dialogue_session_id or not agent_execution_session_id:
            return
        index = self._read_index()
        sessions = index.get("sessions") if isinstance(index.get("sessions"), dict) else {}
        sessions[dialogue_session_id] = {
            "agent_execution_session_id": agent_execution_session_id,
            "task_id": task_id,
            "user_goal": (user_goal or "")[:200],
            "recorded_at": time.time(),
        }
        # Bound growth: keep the 200 most recently touched dialogue sessions.
        if len(sessions) > 200:
            ordered = sorted(sessions.items(), key=lambda kv: kv[1].get("recorded_at", 0.0), reverse=True)
            sessions = dict(ordered[:200])
        self.state_store.write_json(STATE_FILE, {"sessions": sessions})

    def _wants_continue(self, text: str) -> bool:
        raw = str(text or "")
        if any(marker in raw for marker in CONTINUE_MARKERS_ZH):
            return True
        lowered = raw.lower()
        return any(marker in lowered for marker in CONTINUE_MARKERS_EN)

    def _previous_for(self, dialogue_session_id: str) -> dict[str, Any] | None:
        index = self._read_index()
        sessions = index.get("sessions") if isinstance(index.get("sessions"), dict) else {}
        entry = sessions.get(dialogue_session_id)
        return entry if isinstance(entry, dict) else None

    def _is_expired(self, entry: dict[str, Any]) -> bool:
        recorded_at = entry.get("recorded_at")
        if not isinstance(recorded_at, (int, float)):
            return False
        return (time.time() - float(recorded_at)) > self.reuse_ttl_seconds

    def _read_index(self) -> dict[str, Any]:
        data = self.state_store.read_json(STATE_FILE)
        return data if isinstance(data, dict) else {}


def default_agent_execution_session_id(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_:-]", "_", str(task_id or "task"))
    return f"agent-exec:{safe}"
