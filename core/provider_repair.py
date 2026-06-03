from __future__ import annotations

import re
from typing import Any, Callable


def repair_provider_error(error_text: str, *, retry: Callable[[dict[str, Any]], Any], params: dict[str, Any]) -> Any:
    """Retry tool/provider calls after stripping unsupported parameters."""
    lowered = (error_text or "").lower()
    if "unsupported_language" in lowered or "language filtering is not supported" in lowered:
        cleaned = dict(params)
        cleaned.pop("language", None)
        cleaned.pop("language_filter", None)
        cleaned.pop("locale", None)
        return retry(cleaned)
    if re.search(r"unsupported[_\\s-]?language", lowered):
        cleaned = dict(params)
        cleaned.pop("language", None)
        return retry(cleaned)
    raise RuntimeError(error_text or "provider_error")
