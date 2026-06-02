from __future__ import annotations

import json
import html
import os
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from probes.http_utils import fetch_text
from probes.schema import probe_payload


class SearchProbe:
    """Read-only web search probe using a public HTML search endpoint."""

    def __init__(self, fetcher: Callable[[str], str] | None = None, cli_runner: Callable[[list[str], float], str] | None = None) -> None:
        self.fetcher = fetcher
        self.cli_runner = cli_runner

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
        if self.fetcher is None and self._openclaw_search_enabled():
            openclaw_result = self._run_openclaw_search(query, max_results=max_results)
            provider = os.getenv("VEYRA_SEARCH_PROVIDER", "auto").strip().lower()
            if openclaw_result.get("status") == "ok" or provider in {"openclaw", "openclaw_cli"}:
                return openclaw_result
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

    def _openclaw_search_enabled(self) -> bool:
        provider = os.getenv("VEYRA_SEARCH_PROVIDER", "auto").strip().lower()
        if provider in {"public", "duckduckgo", "ddg"}:
            return False
        if provider in {"openclaw", "openclaw_cli", "auto"}:
            return bool(self.cli_runner or shutil.which("openclaw"))
        return False

    def _run_openclaw_search(self, query: str, *, max_results: int) -> dict[str, Any]:
        timeout = self._float_env("VEYRA_OPENCLAW_SEARCH_TIMEOUT", 60.0)
        command = ["openclaw", "infer", "web", "search", "--query", query, "--limit", str(max(1, min(int(max_results), 10))), "--json"]
        try:
            body = self.cli_runner(command, timeout) if self.cli_runner else self._run_cli(command, timeout)
        except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
            return probe_payload(
                probe="search_probe",
                target=query,
                status="unavailable",
                summary=f"OpenClaw search unavailable for {query}: {exc}",
                confidence=0.35,
                ttl_seconds=300,
                details={"query": query, "provider": "openclaw_cli", "error": str(exc)},
            )
        results = self._parse_openclaw_results(body, limit=max_results, query=query)
        status = "ok" if results else "empty"
        summary = f"OpenClaw search returned {len(results)} result(s) for {query}." if results else f"OpenClaw search returned no parseable results for {query}."
        return probe_payload(
            probe="search_probe",
            target=query,
            status=status,
            summary=summary,
            confidence=0.82 if results else 0.45,
            ttl_seconds=1800,
            details={"query": query, "provider": "openclaw_cli", "results": results},
            claims=[
                {
                    "key": f"search:{query}",
                    "claim": summary,
                    "confidence": 0.82 if results else 0.45,
                    "source": "openclaw_cli_search",
                    "ttl_seconds": 1800,
                }
            ],
        )

    def _run_cli(self, command: list[str], timeout: float) -> str:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
        return completed.stdout

    def _parse_openclaw_results(self, body: str, *, limit: int, query: str = "") -> list[dict[str, Any]]:
        payloads: list[Any] = []
        try:
            payloads.append(json.loads(body))
        except json.JSONDecodeError:
            for line in (body or "").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    payloads.append(json.loads(text))
                except json.JSONDecodeError:
                    continue
        results: list[dict[str, Any]] = []
        for payload in payloads:
            for item in self._candidate_items(payload):
                parsed = self._normalize_openclaw_item(item)
                if parsed and not any(existing["url"] == parsed["url"] for existing in results):
                    results.append(parsed)
                if len(results) >= limit:
                    return results
            summary = self._openclaw_summary_result(payload, query=query)
            if summary and not results:
                results.append(summary)
        return results

    def _candidate_items(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("results", "items", "data", "matches"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = self._candidate_items(value)
                if nested:
                    return nested
        return []

    def _normalize_openclaw_item(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or item.get("name") or item.get("headline") or "").strip()
        url = str(item.get("url") or item.get("link") or item.get("href") or "").strip()
        snippet = str(item.get("snippet") or item.get("summary") or item.get("content") or item.get("text") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return None
        return {"title": title or url, "url": url, "snippet": snippet[:700], "source": self._host(url)}

    def _openclaw_summary_result(self, payload: Any, *, query: str) -> dict[str, Any] | None:
        content = self._find_text_field(payload, keys={"content", "summary", "text"})
        if not content:
            return None
        source = "openclaw_cli"
        provider = self._find_text_field(payload, keys={"provider"})
        if provider:
            source = f"openclaw_cli:{provider[:40]}"
        title = self._first_heading(content) or f"OpenClaw web search summary: {query}"
        return {"title": title[:240], "url": "", "snippet": content[:900], "source": source}

    def _find_text_field(self, value: Any, *, keys: set[str]) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) in keys and isinstance(item, str) and item.strip():
                    return item.strip()
            for item in value.values():
                found = self._find_text_field(item, keys=keys)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_text_field(item, keys=keys)
                if found:
                    return found
        return ""

    def _first_heading(self, text: str) -> str:
        for line in str(text or "").splitlines():
            cleaned = self._clean_html(line).strip(" #-*\t")
            if len(cleaned) >= 8:
                return cleaned
        return ""

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

    def _float_env(self, name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default
