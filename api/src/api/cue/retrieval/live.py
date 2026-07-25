"""The live evidence retriever: news corpus + Wikipedia + SearXNG (XERK-120).

Three tiers fan out **concurrently** under one hard deadline
(``API_CUE_RETRIEVAL_DEADLINE_MS``):

  * **news** — the RSS-fed corpus in the news store. Local FTS, single-digit
    ms, and every hit is recent by construction (the corpus is pruned).
  * **wikipedia** — one round-trip (``generator=search`` + intro extracts,
    ~300-400ms measured) for stable facts: entities, places, figures.
  * **searxng** — the self-hosted metasearch instance for everything else.
    Engines are pinned per request (``API_CUE_SEARXNG_ENGINES``): the measured
    default set answers in ~500-800ms where the instance's full engine fan-out
    takes 1-1.5s, and it keeps the shared instance's config untouched.

Whatever lands inside the deadline rides the prompt; a slow tier contributes
nothing rather than delaying the cue — the deadline, not the slowest upstream,
bounds added latency. Tiers that miss the deadline keep running and settle
into a per-session cache keyed by the query, so a conversation that stays on a
topic gets the slow tier's evidence on the *next* cue consideration.

Network calls are excluded from coverage per repo convention; the pure parts
(result mapping, trimming, ordering, cache) are unit-tested against
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from api.cue.retrieval.base import Evidence
from api.cue.retrieval.query import RetrievalQuery, build_query
from api.metrics import metrics
from api.persistence.news import NewsStore

log = logging.getLogger("api.cue.retrieval")

# Prompt budget. Measured on the deployed Qwen3.6-27B-FP8: ~1400 prompt tokens
# adds ~200ms to the fixed ~1.1s call; ~5400 adds ~900ms. Six 300-char snippets
# plus framing sits comfortably under the cheap zone.
_MAX_SNIPPET_CHARS = 300
# Per-tier result caps: news is freshest-first, wikipedia is one solid extract,
# web fills the rest.
_NEWS_LIMIT = 3
_WIKI_LIMIT = 2
_WEB_LIMIT = 3
# The per-session cache holds the most recent query results only; conversations
# revisit topics, they don't archive them.
_CACHE_MAX = 32


class LiveEvidenceRetriever:
    def __init__(
        self,
        *,
        news_store: NewsStore | None,
        wikipedia_endpoint: str = "https://en.wikipedia.org",
        searxng_endpoint: str = "",
        searxng_engines: str = "",
        deadline_ms: int = 800,
        max_evidence: int = 6,
        http_client=None,
    ) -> None:
        self._news_store = news_store
        self._wikipedia = wikipedia_endpoint.rstrip("/")
        self._searxng = searxng_endpoint.rstrip("/")
        self._engines = searxng_engines
        self._deadline_ms = deadline_ms
        self._max_evidence = max_evidence
        self._client = http_client  # injected in tests; else lazily built
        self._own_client = http_client is None
        # Query cache-key → evidence. Late tiers (past-deadline finishers) settle
        # here, so a conversation that stays on topic gets them next time.
        self._cache: dict[str, list[Evidence]] = {}
        self._late_tasks: set[asyncio.Task] = set()

    def _http(self):
        if self._client is None:  # pragma: no cover - built lazily in prod
            import httpx

            self._client = httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": "tenir-cue-rag/1.0"},
            )
        return self._client

    async def close(self) -> None:
        for task in self._late_tasks:
            task.cancel()
        if self._client is not None and self._own_client:
            await self._client.aclose()
            self._client = None

    # ---- tiers ---------------------------------------------------------------

    async def _news_tier(self, query: RetrievalQuery) -> list[Evidence]:
        if self._news_store is None:
            return []
        items = await asyncio.to_thread(
            self._news_store.search, list(query.keywords), limit=_NEWS_LIMIT
        )
        return [
            Evidence(
                source=item.source,
                title=item.title,
                snippet=item.summary[:_MAX_SNIPPET_CHARS] or item.title,
                published=item.published_at.strftime("%Y-%m-%d"),
                url=item.link,
            )
            for item in items
        ]

    async def _wikipedia_tier(self, query: RetrievalQuery) -> list[Evidence]:
        if not self._wikipedia:
            return []
        # One round trip: generator=search feeds prop=extracts, so the search hits
        # come back *with* their intro text (measured ~300-400ms).
        resp = await self._http().get(
            f"{self._wikipedia}/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": " ".join(query.keywords),
                "gsrlimit": str(_WIKI_LIMIT),
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "exchars": str(_MAX_SNIPPET_CHARS),
                "format": "json",
            },
        )
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
        results = []
        # gsr results carry an "index" rank; sort by it for stable ordering.
        for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            extract = (page.get("extract") or "").strip()
            title = (page.get("title") or "").strip()
            if not extract or not title:
                continue
            results.append(
                Evidence(source="Wikipedia", title=title, snippet=extract[:_MAX_SNIPPET_CHARS])
            )
        return results

    async def _searxng_tier(self, query: RetrievalQuery) -> list[Evidence]:
        if not self._searxng:
            return []
        params = {"q": query.text or " ".join(query.keywords), "format": "json"}
        if self._engines:
            params["engines"] = self._engines
        resp = await self._http().get(f"{self._searxng}/search", params=params)
        resp.raise_for_status()
        results = []
        for hit in (resp.json().get("results") or [])[:_WEB_LIMIT]:
            title = (hit.get("title") or "").strip()
            content = (hit.get("content") or "").strip()
            if not title or not content:
                continue
            results.append(
                Evidence(
                    source=_domain_label(hit.get("url") or ""),
                    title=title,
                    snippet=content[:_MAX_SNIPPET_CHARS],
                    published=(hit.get("publishedDate") or "")[:10] or None,
                    url=hit.get("url"),
                )
            )
        return results

    # ---- composition ---------------------------------------------------------

    async def retrieve(self, turns: Sequence[str]) -> list[Evidence]:
        query = build_query(turns)
        if not query:
            return []
        cached = self._cache.get(query.cache_key())

        tiers = {
            "news": asyncio.create_task(self._news_tier(query)),
            "wikipedia": asyncio.create_task(self._wikipedia_tier(query)),
            "searxng": asyncio.create_task(self._searxng_tier(query)),
        }
        done, pending = await asyncio.wait(
            tiers.values(), timeout=self._deadline_ms / 1000
        )

        evidence: list[Evidence] = []
        # Tier order (news → wikipedia → searxng) sets prompt order: freshest and
        # most curated first, so budget trimming drops the web tail first.
        for name, task in tiers.items():
            if task in done:
                try:
                    evidence.extend(task.result())
                except Exception:
                    log.warning("cue retrieval tier %r failed", name, exc_info=True)
                    metrics.incr("cue.retrieval.tier_errors")
            else:
                metrics.incr("cue.retrieval.tier_deadline_misses")

        # Let slow tiers finish into the cache for the next consideration on this
        # topic, instead of discarding work already in flight.
        if pending:
            late = asyncio.create_task(self._settle_late(query.cache_key(), evidence, pending))
            self._late_tasks.add(late)
            late.add_done_callback(self._late_tasks.discard)

        if not evidence and cached is not None:
            metrics.incr("cue.retrieval.cache_hits")
            evidence = cached
        elif evidence:
            self._remember(query.cache_key(), evidence)

        return evidence[: self._max_evidence]

    async def _settle_late(
        self, cache_key: str, on_time: list[Evidence], pending: set[asyncio.Task]
    ) -> None:
        """Await past-deadline tiers and settle their results into the cache."""
        results = await asyncio.gather(*pending, return_exceptions=True)
        late: list[Evidence] = []
        for result in results:
            if isinstance(result, BaseException):
                metrics.incr("cue.retrieval.tier_errors")
            else:
                late.extend(result)
        if late:
            self._remember(cache_key, list(on_time) + late)

    def _remember(self, key: str, evidence: list[Evidence]) -> None:
        self._cache[key] = evidence
        while len(self._cache) > _CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))


def _domain_label(url: str) -> str:
    """A web hit's attribution label: its bare domain ("bbc.co.uk"), or "Web"
    when the URL doesn't parse."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host.removeprefix("www.") or "Web"
