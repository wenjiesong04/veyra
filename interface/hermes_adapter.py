from __future__ import annotations

from typing import Any

from interface.http_agent_adapter import HttpAgentAdapter


class HermesAdapter(HttpAgentAdapter):
    """HTTP adapter for Hermes-compatible runtimes."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 20.0, **paths: Any) -> None:
        super().__init__(
            HttpAgentAdapter.from_env(
                "hermes",
                "HERMES",
                base_url=base_url or "",
                api_key=api_key or "",
                timeout=timeout,
                **paths,
            ).config
        )
