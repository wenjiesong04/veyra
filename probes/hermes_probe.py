from __future__ import annotations

import os
import re
import socket
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from probes.schema import probe_payload


class HermesProbe:
    def run(self, text: str = "") -> dict:
        base_url = self._extract_url(text) or os.getenv("HERMES_BASE_URL", "")
        if base_url:
            return self._http_probe(base_url.rstrip("/"))
        port = self._extract_port(text) or 18889
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            listening = sock.connect_ex(("127.0.0.1", port)) == 0
        status = "available" if listening else "unavailable"
        return probe_payload(
            probe="hermes_probe",
            target="hermes_runtime",
            status=status,
            summary=f"Hermes runtime port {port} is {'listening' if listening else 'closed'}.",
            confidence=0.8,
            ttl_seconds=30,
            details={"configured": False, "host": "127.0.0.1", "port": port},
        )

    def _http_probe(self, base_url: str) -> dict:
        url = f"{base_url}/capabilities"
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=3) as response:
                return probe_payload(
                    probe="hermes_probe",
                    target="hermes_runtime",
                    status="available",
                    summary=f"Hermes capabilities endpoint returned HTTP {response.status}.",
                    confidence=0.9,
                    ttl_seconds=30,
                    details={"configured": True, "base_url": base_url, "status_code": response.status},
                )
        except (URLError, TimeoutError, OSError) as exc:
            return probe_payload(
                probe="hermes_probe",
                target="hermes_runtime",
                status="unavailable",
                summary=f"Hermes capabilities endpoint is unavailable: {exc}.",
                confidence=0.45,
                ttl_seconds=30,
                details={"configured": True, "base_url": base_url, "error": str(exc)},
            )

    def _extract_url(self, text: str) -> str | None:
        match = re.search(r"https?://[^\s]+", text)
        return match.group(0).rstrip(".,)") if match else None

    def _extract_port(self, text: str) -> int | None:
        match = re.search(r"\b([1-9][0-9]{1,4})\b", text)
        if not match:
            return None
        port = int(match.group(1))
        return port if 0 < port <= 65535 else None
