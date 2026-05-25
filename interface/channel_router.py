from __future__ import annotations


class ChannelRouter:
    """Normalizes external channel names into Veyra channel ids."""

    ALIASES = {
        "web": "console",
        "webui": "console",
        "browser": "console",
        "http": "api",
        "rest": "api",
        "lark": "feishu",
    }

    def resolve(self, channel: str) -> str:
        normalized = str(channel or "api").strip().lower().replace(" ", "_")
        return self.ALIASES.get(normalized, normalized or "api")
