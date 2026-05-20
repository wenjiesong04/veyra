from __future__ import annotations

import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from probes.schema import probe_payload


class WebProbe:
    def run(self, text: str = "") -> dict:
        url = self._extract_url(text)
        if not url:
            return probe_payload(
                probe="web_probe",
                target="web",
                status="missing_target",
                summary="Web probe needs an http or https URL.",
                confidence=0.5,
                ttl_seconds=60,
                details={"configured": True},
            )
        request = Request(url, method="GET", headers={"User-Agent": "Veyra-WebProbe/0.1"})
        try:
            with urlopen(request, timeout=3) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                return probe_payload(
                    probe="web_probe",
                    target=url,
                    status="ok",
                    summary=f"Web target {url} returned HTTP {response.status}.",
                    confidence=0.9,
                    ttl_seconds=60,
                    details={"url": url, "status_code": response.status, "sample": body},
                )
        except HTTPError as exc:
            return probe_payload(
                probe="web_probe",
                target=url,
                status="http_error",
                summary=f"Web target {url} returned HTTP {exc.code}.",
                confidence=0.75,
                ttl_seconds=60,
                details={"url": url, "status_code": exc.code, "error": str(exc)},
            )
        except (URLError, TimeoutError, OSError) as exc:
            return probe_payload(
                probe="web_probe",
                target=url,
                status="unavailable",
                summary=f"Web target {url} is unavailable: {exc}.",
                confidence=0.45,
                ttl_seconds=30,
                details={"url": url, "error": str(exc)},
            )

    def _extract_url(self, text: str) -> str | None:
        match = re.search(r"https?://[^\s]+", text)
        return match.group(0).rstrip(".,)") if match else None
