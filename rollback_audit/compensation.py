from __future__ import annotations

from typing import Any


class Compensation:
    def plan(self, failed_action: dict[str, Any]) -> dict[str, Any]:
        steps = [
            {"type": "inspect_journal", "reason": "Confirm exact event, policy, tool, execution, and rollback evidence."},
            {"type": "prefer_snapshot_restore", "reason": "Use snapshot restore only when a matching snapshot_id is present."},
            {"type": "request_confirmation", "reason": "Any write, restore, or resubmit remains subject to Guardian review."},
        ]
        return {"status": "planned", "steps": steps, "failed_action": failed_action}
