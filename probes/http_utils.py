from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def ssl_context() -> ssl.SSLContext:
    ca_bundle = _ca_bundle_path()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


def fetch_json(url: str, *, timeout: float = 5.0, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=timeout, context=ssl_context()) as response:
        import json

        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, *, timeout: float = 5.0, headers: dict[str, str] | None = None, max_bytes: int = 500_000) -> str:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=timeout, context=ssl_context()) as response:
        return response.read(max_bytes).decode("utf-8", errors="replace")


def _ca_bundle_path() -> str:
    candidates = [
        os.getenv("REQUESTS_CA_BUNDLE", ""),
        os.getenv("SSL_CERT_FILE", ""),
        os.getenv("CURL_CA_BUNDLE", ""),
    ]
    try:
        import certifi

        candidates.append(certifi.where())
    except Exception:
        pass
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and Path(path).expanduser().exists():
            return str(Path(path).expanduser())
    return ""
