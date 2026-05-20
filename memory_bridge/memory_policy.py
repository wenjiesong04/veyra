from __future__ import annotations

import re


class MemoryPolicy:
    SENSITIVE_PATTERNS = (
        re.compile(r"\.env(?:\b|[/.])", re.IGNORECASE),
        re.compile(r"\b(api[_-]?key|authorization|bearer\s+[a-z0-9._-]+|token|secret|private[_-]?key)\b", re.IGNORECASE),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
        re.compile(r"/Users/[^/\s]+/[^\s]+"),
        re.compile(r"/home/[^/\s]+/[^\s]+"),
    )

    def allow_write(self, patch: dict) -> bool:
        text = str(patch)
        return not any(pattern.search(text) for pattern in self.SENSITIVE_PATTERNS)
