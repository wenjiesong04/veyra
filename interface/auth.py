from __future__ import annotations

from typing import Any


class AuthPolicy:
    """Local channel auth gate.

    Remote providers can later add signature checks here. For now Veyra accepts
    only enabled local channels and always records the decision.
    """

    def allow(self, channel: str, user_id: str, channel_config: dict[str, Any] | None = None) -> bool:
        if not channel or not user_id:
            return False
        if channel_config and channel_config.get("enabled") is False:
            return False
        return True
