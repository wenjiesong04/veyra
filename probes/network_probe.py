from __future__ import annotations

import os
import re
import socket
from urllib.parse import urlparse

from probes.schema import probe_payload


class NetworkProbe:
    def run(self, text: str = "") -> dict:
        host = self._extract_host(text) or "localhost"
        proxies = {key: value for key, value in os.environ.items() if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
        try:
            resolved = socket.gethostbyname(host)
            status = "ok"
            summary = f"Network host {host} resolved to {resolved}."
            confidence = 0.85
            details = {"host": host, "resolved": resolved, "proxies": proxies}
        except OSError as exc:
            status = "dns_error"
            summary = f"Network host {host} could not be resolved: {exc}."
            confidence = 0.45
            details = {"host": host, "error": str(exc), "proxies": proxies}
        return probe_payload(
            probe="network_probe",
            target=host,
            status=status,
            summary=summary,
            confidence=confidence,
            ttl_seconds=120,
            details=details,
        )

    def _extract_host(self, text: str) -> str | None:
        url_match = re.search(r"https?://[^\s]+", text)
        if url_match:
            return urlparse(url_match.group(0)).hostname
        host_match = re.search(r"\b([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|localhost)\b", text)
        return host_match.group(1) if host_match else None
