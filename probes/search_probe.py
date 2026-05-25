from __future__ import annotations

import html
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from probes.schema import probe_payload


class SearchProbe:
    """Lightweight web search probe for fresh external context.

    This is intentionally read-only and returns evidence snippets instead of
    generating a final answer. Veyra or the Core model can summarize it later.
    """

    def run(self, text: str = "") -> dict[str, Any]:
        query = self._extract_query(text)
        if not query:
            return probe_payload(
                probe="search_probe",
                target="web_search",
                status="missing_target",
                summary="Search probe needs a query.",
                confidence=0.5,
                ttl_seconds=120,
                details={"configured": True},
            )
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            body = self._fetch(url)
            results = self._parse_results(body)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return probe_payload(
                probe="search_probe",
                target=query,
                status="unavailable",
                summary=f"Search query failed: {exc}",
                confidence=0.35,
                ttl_seconds=120,
                details={"query": query, "provider": "duckduckgo_html", "error": str(exc)},
            )
        status = "ok" if results else "empty"
        summary = (
            f"Search returned {len(results)} result(s) for: {query}."
            if results
            else f"Search returned no parseable results for: {query}."
        )
        return probe_payload(
            probe="search_probe",
            target=query,
            status=status,
            summary=summary,
            confidence=0.78 if results else 0.45,
            ttl_seconds=900,
            details={"query": query, "provider": "duckduckgo_html", "results": results},
            claims=[
                {
                    "key": f"search:{query}:results",
                    "claim": summary,
                    "confidence": 0.75 if results else 0.45,
                    "ttl_seconds": 900,
                    "evidence": {"provider": "duckduckgo_html", "result_count": len(results)},
                }
            ],
        )

    def _fetch(self, url: str) -> str:
        request = Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 VeyraSearchProbe/0.1"})
        with urlopen(request, timeout=8) as response:
            return response.read(120_000).decode("utf-8", errors="replace")

    def _parse_results(self, body: str) -> list[dict[str, str]]:
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r'(?:<a[^>]+class="result__snippet"[^>]*>|<div[^>]+class="result__snippet"[^>]*>)(?P<snippet>.*?)</',
            re.DOTALL | re.IGNORECASE,
        )
        results: list[dict[str, str]] = []
        for match in pattern.finditer(body):
            title = self._strip_html(match.group("title"))
            snippet = self._strip_html(match.group("snippet"))
            link = self._decode_link(html.unescape(match.group("href")))
            if not title or not link:
                continue
            results.append({"title": title[:240], "url": link, "snippet": snippet[:500]})
            if len(results) >= 5:
                break
        return results

    def _extract_query(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        for prefix in ["搜索", "查找", "查询", "查一下", "帮我查", "search for", "search"]:
            if normalized.lower().startswith(prefix.lower()):
                normalized = normalized[len(prefix) :].strip(" ：:，,")
                break
        return normalized[:180].strip()

    def _strip_html(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", "", value)
        return html.unescape(re.sub(r"\s+", " ", text)).strip()

    def _decode_link(self, href: str) -> str:
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        uddg = query.get("uddg", [""])[0]
        return unquote(uddg) if uddg else href
