from __future__ import annotations

import shlex
import threading
from typing import Any, Callable

from core.world_state import WorldStateStore
from rollback_audit.rollback_manager import RollbackManager
from tool_proxy.safe_api import SafeAPI
from tool_proxy.safe_browser import SafeBrowser
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell


ReviewAuthorizer = Callable[[str, str], dict[str, Any]]


class ActionExecutor:
    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        safe_shell: SafeShell | None = None,
        safe_file: SafeFile | None = None,
        safe_browser: SafeBrowser | None = None,
        safe_api: SafeAPI | None = None,
        rollback_manager: RollbackManager | None = None,
        proactive_executor: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
        review_authorizer: ReviewAuthorizer | None = None,
    ) -> None:
        self.state_store = state_store
        self.safe_shell = safe_shell or SafeShell(state_store=state_store)
        self.safe_file = safe_file or SafeFile(state_store=state_store)
        self.safe_browser = safe_browser or SafeBrowser(state_store=state_store)
        self.safe_api = safe_api or SafeAPI(state_store=state_store)
        self.rollback_manager = rollback_manager or RollbackManager(state_store)
        # Executes approved proactive remediation / agent-restart proposals. Wired
        # after construction because ProactiveChecks is built later in app assembly.
        self.proactive_executor = proactive_executor
        self.review_authorizer = review_authorizer
        self._file_write_lock = threading.Lock()

    def execute_review(
        self,
        review: dict[str, Any],
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute only the canonical queue entry behind a one-time claim.

        ``review`` is a routing hint, not authorization.  A caller-supplied
        ``review_id`` or ``approved_by`` string can never unlock a side effect;
        the injected ReviewQueue authorizer must consume the matching claim and
        return the authoritative proposal first.
        """

        if not isinstance(review, dict):
            raise PermissionError("A canonical review reference is required")
        review_id = str(review.get("review_id") or "")
        if not review_id or review_id != review_id.strip():
            raise PermissionError("A normalized review_id is required")
        if self.review_authorizer is None:
            raise PermissionError(
                "ActionExecutor has no authoritative review claim resolver"
            )
        if not isinstance(claim_token, str) or not claim_token:
            raise PermissionError(
                f"Execution claim token required for review: {review_id}"
            )
        canonical_review = self.review_authorizer(review_id, claim_token)
        if (
            not isinstance(canonical_review, dict)
            or canonical_review.get("review_id") != review_id
            or canonical_review.get("status") != "approved"
        ):
            raise PermissionError(
                f"Review authorizer returned an invalid authority object: {review_id}"
            )

        proposal = canonical_review.get("proposal")
        if not proposal:
            return {
                "status": "approved_noop",
                "reason": "Review item has no executable proposal. Approval is recorded for governance only.",
            }
        if not isinstance(proposal, dict):
            return {
                "status": "error",
                "reason": "Canonical review proposal must be an object.",
            }
        approval_id = review_id
        proposal_type = str(proposal.get("type") or "")
        if proposal_type in {"proactive_remediation", "agent_restart"}:
            if self.proactive_executor is None:
                return {"status": "not_supported", "reason": f"no proactive executor configured for {proposal_type}", "proposal": proposal}
            try:
                result = self.proactive_executor(proposal, approval_id)
            except Exception as exc:
                return {"status": "error", "reason": f"proactive remediation failed: {exc}", "proposal": proposal}
            if not isinstance(result, dict):
                return {"status": "error", "reason": "proactive executor returned a non-object result"}
            return {**result, "approved_by": approval_id, "operation": proposal_type}
        action = proposal.get("action", {})
        action_type = action.get("type")
        if action_type == "shell_command":
            command = action.get("command", [])
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not command:
                return {"status": "error", "reason": "shell_command action requires a non-empty command"}
            return self.safe_shell.run([str(part) for part in command], approved_by=approval_id)
        if action_type == "file_write":
            path = str(action.get("path", ""))
            content = str(action.get("content", ""))
            if not path:
                return {"status": "error", "reason": "file_write action requires path"}
            with self._file_write_lock:
                return self.safe_file.write_text(
                    path,
                    content,
                    reason=f"approved review {approval_id}",
                    approved_by=approval_id,
                    require_snapshot=True,
                    snapshotter=self.rollback_manager.snapshot_file,
                )
        if action_type == "file_read":
            path = str(action.get("path", ""))
            if not path:
                return {"status": "error", "reason": "file_read action requires path"}
            return self.safe_file.read_text(path)
        if action_type == "browser_open":
            url = str(action.get("url", ""))
            if not url:
                return {"status": "error", "reason": "browser_open action requires url"}
            return self.safe_browser.open(url, approved_by=approval_id)
        if action_type == "api_request":
            payload = action.get("payload", {})
            if not isinstance(payload, dict):
                return {"status": "error", "reason": "api_request action requires payload object"}
            return self.safe_api.request(payload, approved_by=approval_id)
        if action_type == "rollback_restore":
            snapshot_id = str(action.get("snapshot_id", ""))
            if not snapshot_id:
                return {"status": "error", "reason": "rollback_restore action requires snapshot_id"}
            result = self.rollback_manager.restore(snapshot_id, authorized=True)
            return {**result, "approved_by": approval_id, "operation": "rollback_restore"}
        return {"status": "not_supported", "reason": f"Unsupported action type: {action_type}", "proposal": proposal}


__all__ = ["ActionExecutor", "ReviewAuthorizer"]
