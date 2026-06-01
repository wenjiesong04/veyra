from __future__ import annotations

import html
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from probes.http_utils import fetch_text
from probes.schema import probe_payload


class SearchProbe:
    """Read-only web search probe using a public HTML search endpoint."""

    def __init__(self, fetcher: Callable[[str], str] | None = None) -> None:
        self.fetcher = fetcher

    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        query = " ".join((query or "").split())
        if not query:
            return probe_payload(
                probe="search_probe",
                target="search",
                status="missing_target",
                summary="Search probe needs a non-empty query.",
                confidence=0.4,
                ttl_seconds=600,
                details={"configured": True},
            )
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            body = self.fetcher(url) if self.fetcher else fetch_text(url, timeout=8, headers={"User-Agent": "Veyra-SearchProbe/0.1"})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            return probe_payload(
                probe="search_probe",
                target=query,
                status="unavailable",
                summary=f"Search unavailable for {query}: {exc}",
                confidence=0.35,
                ttl_seconds=300,
                details={"query": query, "source_url": url, "error": str(exc)},
            )
        results = self._parse_results(body, limit=max_results)
        status = "ok" if results else "empty"
        summary = f"Search returned {len(results)} result(s) for {query}." if results else f"Search returned no parseable results for {query}."
        return probe_payload(
            probe="search_probe",
            target=query,
            status=status,
            summary=summary,
            confidence=0.7 if results else 0.45,
            ttl_seconds=1800,
            details={"query": query, "source_url": url, "results": results},
            claims=[
                {
                    "key": f"search:{query}",
                    "claim": summary,
                    "confidence": 0.7 if results else 0.45,
                    "source": "search_probe",
                    "ttl_seconds": 1800,
                }
            ],
        )

    def _parse_results(self, body: str, *, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(body or ""):
            title = self._clean_html(match.group("title"))
            url = self._clean_url(match.group("href"))
            if not title or not url:
                continue
            snippet = self._snippet_after(body, match.end())
            results.append({"title": title, "url": url, "snippet": snippet, "source": self._host(url)})
            if len(results) >= limit:
                return results
        if results:
            return results

        fallback = re.compile(r'<a[^>]+href="(?P<href>https?://[^"]+)"[^>]*>(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
        for match in fallback.finditer(body or ""):
            title = self._clean_html(match.group("title"))
            url = self._clean_url(match.group("href"))
            if title and url and not any(item["url"] == url for item in results):
                results.append({"title": title, "url": url, "snippet": "", "source": self._host(url)})
            if len(results) >= limit:
                break
        return results

    def _snippet_after(self, body: str, offset: int) -> str:
        window = body[offset : offset + 1200]
        match = re.search(r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', window, re.IGNORECASE | re.DOTALL)
        return self._clean_html(match.group("snippet")) if match else ""

    def _clean_html(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value or "")
        return " ".join(html.unescape(text).split())

    def _clean_url(self, value: str) -> str:
        url = html.unescape(value or "")
        parsed = urlparse(url)
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                url = unquote(target)
        return url if url.startswith(("http://", "https://")) else ""

    def _host(self, url: str) -> str:
        return urlparse(url).netloc.lower()
