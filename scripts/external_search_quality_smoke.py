#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from probes.schema import probe_payload  # noqa: E402
from probes.search_probe import SearchProbe  # noqa: E402
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class DuplicateSearchProbe:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        self.calls += 1
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"duplicate search for {query}",
            confidence=0.9,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "PyTorch 2.9 release notes latest update",
                        "url": "https://pytorch.org/blog/pytorch-2-9/",
                        "snippet": "Important updated PyTorch release information.",
                        "source": "pytorch.org",
                    },
                    {
                        "title": "PyTorch 2.9 release notes latest update duplicate",
                        "url": "https://pytorch.org/blog/pytorch-2-9/",
                        "snippet": "Duplicate URL should not create duplicate push candidates.",
                        "source": "pytorch.org",
                    },
                ],
            },
        )


class FailingSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        raise TimeoutError(f"synthetic timeout for {query}")


def cache_smoke() -> None:
    calls = {"count": 0}

    def fetcher(_url: str) -> str:
        calls["count"] += 1
        return """
        <html>
          <a class="result__a" href="https://pytorch.org/blog/pytorch-2-9/">PyTorch release notes</a>
          <a class="result__snippet">Latest important PyTorch updates.</a>
        </html>
        """

    old_cache = os.environ.get("VEYRA_SEARCH_CACHE_SECONDS")
    os.environ["VEYRA_SEARCH_CACHE_SECONDS"] = "600"
    try:
        probe = SearchProbe(fetcher=fetcher)
        first = probe.run("PyTorch latest release", max_results=3)
        second = probe.run("PyTorch latest release", max_results=3)
    finally:
        if old_cache is None:
            os.environ.pop("VEYRA_SEARCH_CACHE_SECONDS", None)
        else:
            os.environ["VEYRA_SEARCH_CACHE_SECONDS"] = old_cache
    expect(first.get("status") == "ok", "first public search succeeds", first)
    expect(second.get("status") == "ok", "cached public search succeeds", second)
    expect(calls["count"] == 1, "search probe cache avoids duplicate fetch", calls)
    expect((second.get("details") or {}).get("cache_hit") is True, "cache hit is explicit", second)


def placeholder_filter_smoke() -> None:
    def fetcher(_url: str) -> str:
        return """
        <html>
          <a href="https://duckduckgo.com/">here</a>
        </html>
        """

    old_provider = os.environ.get("VEYRA_SEARCH_PROVIDER")
    os.environ["VEYRA_SEARCH_PROVIDER"] = "public"
    try:
        result = SearchProbe(fetcher=fetcher).run("卢成风 视频", max_results=3)
    finally:
        if old_provider is None:
            os.environ.pop("VEYRA_SEARCH_PROVIDER", None)
        else:
            os.environ["VEYRA_SEARCH_PROVIDER"] = old_provider
    results = (result.get("details") or {}).get("results") if isinstance(result.get("details"), dict) else []
    expect(result.get("status") == "empty", "duckduckgo placeholder is not a search result", result)
    expect(results == [], "placeholder result list is empty", result)


def bing_fallback_smoke() -> None:
    def fetcher(url: str) -> str:
        if "duckduckgo.com" in url:
            return """
            <html>
              <form id="challenge-form" action="//duckduckgo.com/anomaly.js?sv=html"></form>
            </html>
            """
        return """
        <html>
          <ol id="b_results">
            <li class="b_algo">
              <h2><a href="https://example.com/campus-2026">2026 秋招公司信息汇总</a></h2>
              <p>公司、岗位和网申入口汇总。</p>
            </li>
          </ol>
        </html>
        """

    result = SearchProbe(fetcher=fetcher).run("2026 秋招 校招 公司 招聘 信息 网申 岗位", max_results=3)
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    results = details.get("results") if isinstance(details.get("results"), list) else []
    expect(result.get("status") == "ok", "bing fallback recovers from duckduckgo anomaly", result)
    expect(details.get("provider") == "bing_html", "fallback result records provider", result)
    expect(results and results[0].get("url") == "https://example.com/campus-2026", "fallback keeps result URL", result)


def yahoo_fallback_smoke() -> None:
    def fetcher(url: str) -> str:
        if "duckduckgo.com" in url:
            return """
            <html>
              <form id="challenge-form" action="//duckduckgo.com/anomaly.js?sv=html"></form>
            </html>
            """
        return """
        <html>
          <ol class="reg searchCenterMiddle">
            <li>
              <div class="compTitle">
                <a href="https://job.example.com/campus2026">
                  <h3><span>2026 届秋招公司信息汇总</span></h3>
                </a>
              </div>
              <div class="compText"><p>公司、岗位和网申入口汇总。</p></div>
            </li>
          </ol>
        </html>
        """

    result = SearchProbe(fetcher=fetcher).run("2026届秋招 公司招聘 网申入口", max_results=3)
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    results = details.get("results") if isinstance(details.get("results"), list) else []
    expect(result.get("status") == "ok", "yahoo fallback recovers from duckduckgo anomaly", result)
    expect(details.get("provider") == "yahoo_html", "yahoo fallback records provider", result)
    expect(results and results[0].get("title") == "2026 届秋招公司信息汇总", "yahoo fallback keeps result title", result)


def quality_and_dedupe_smoke() -> None:
    with TemporaryDirectory(prefix="veyra-external-search-quality-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json(
            "external_world.json",
            {
                "watchlist": [
                    {
                        "target": "external:pytorch",
                        "kind": "external_search",
                        "enabled": True,
                        "topic": "PyTorch",
                        "query": "PyTorch latest release",
                        "commitment_id": "cmt_pytorch",
                    }
                ],
                "summaries": [],
                "knowledge_items": [],
                "push_candidates": [],
            },
        )
        search = DuplicateSearchProbe()
        refresh = ExternalWorldRefresh(store, search_probe=search)  # type: ignore[arg-type]
        first = refresh.refresh_watchlist(limit=5)
        second = refresh.refresh_watchlist(limit=5)
        external = store.read_json("external_world.json")
        knowledge_items = external.get("knowledge_items") if isinstance(external.get("knowledge_items"), list) else []
        push_candidates = external.get("push_candidates") if isinstance(external.get("push_candidates"), list) else []
        expect(first.get("status") == "success" and second.get("status") == "success", "external refresh succeeds twice", {"first": first, "second": second})
        expect(len(knowledge_items) == 1, "duplicate search result creates one knowledge item", knowledge_items)
        expect(len(push_candidates) == 1, "duplicate search result creates one push candidate", push_candidates)
        candidate = push_candidates[0]
        for key in ("source", "retrieved_at", "ttl", "dedupe_key", "quality_score", "summary"):
            expect(bool(candidate.get(key)), f"push candidate records {key}", candidate)
        expect(float(candidate.get("quality_score") or 0) >= 0.6, "push candidate has useful quality score", candidate)


def failure_smoke() -> None:
    with TemporaryDirectory(prefix="veyra-external-search-failure-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json(
            "external_world.json",
            {
                "watchlist": [
                    {
                        "target": "external:slow",
                        "kind": "external_search",
                        "enabled": True,
                        "topic": "Slow Topic",
                        "query": "slow topic latest",
                        "commitment_id": "cmt_slow",
                    }
                ],
                "summaries": [],
                "knowledge_items": [],
                "push_candidates": [],
            },
        )
        refresh = ExternalWorldRefresh(store, search_probe=FailingSearchProbe())  # type: ignore[arg-type]
        result = refresh.refresh_watchlist(limit=5)
        refreshed = result.get("refreshed") if isinstance(result.get("refreshed"), list) else []
        expect(result.get("status") == "success", "search failure does not block refresh loop", result)
        expect(refreshed and refreshed[0].get("status") == "unavailable", "search failure is recorded as unavailable", refreshed)
        expect(not store.read_json("external_world.json").get("push_candidates"), "failed search creates no push candidate", store.read_json("external_world.json"))


def main() -> int:
    cache_smoke()
    placeholder_filter_smoke()
    yahoo_fallback_smoke()
    bing_fallback_smoke()
    quality_and_dedupe_smoke()
    failure_smoke()
    print("external search quality smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
