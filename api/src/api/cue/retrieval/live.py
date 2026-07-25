"""The live evidence retriever: news corpus + Wikipedia + SearXNG (XERK-120).

Three tiers fan out **concurrently** under one hard deadline
(``API_CUE_RETRIEVAL_DEADLINE_MS``):

  * **news** — the RSS-fed corpus in the news store. Local FTS, single-digit
    ms, and every hit is recent by construction (the corpus is pruned).
  * **wikipedia** — one round-trip (``generator=search`` + intro extracts,
    ~300-400ms measured) for stable facts: entities, places, figures. When the
    live site fails (the 429 storms that opened XERK-124, an outage, no WAN),
    the tier falls back to a local Kiwix ZIM mirror if one is configured
    (``API_CUE_KIWIX_ENDPOINT``) — measured against live: 2-4x faster and
    unthrottleable, but its snapshot trails by weeks and its ranking is
    noticeably worse ("eiffel tower height" ranked Eiffel Tower (Paris, Texas)
    first), which is why it is the fallback and not the primary.
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
import html
import logging
import re
from collections.abc import Sequence

from api.cue.retrieval.base import Evidence
from api.cue.retrieval.query import RetrievalQuery, build_query
from api.metrics import metrics
from api.persistence.news import NewsStore

log = logging.getLogger("api.cue.retrieval")

# Kiwix article HTML → lead paragraph (see _first_paragraph).
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Prompt budget. Measured on the deployed Qwen3.6-27B-FP8: ~1400 prompt tokens
# adds ~200ms to the fixed ~1.1s call; ~5400 adds ~900ms. Six 300-char snippets
# plus framing sits comfortably under the cheap zone.
_MAX_SNIPPET_CHARS = 300
# Per-tier result caps: news is freshest-first, wikipedia leads with the top
# hits (3 since XERK-124: on "toy story" the article that knew the fifth film
# was already out ranked #2-#3 — a 2-cap clipped exactly the freshness the tier
# exists to provide, for ~75 prompt tokens of savings), web fills the rest.
_NEWS_LIMIT = 3
_WIKI_LIMIT = 3
_WEB_LIMIT = 3
# How many keywords the encyclopedia tier searches with (XERK-124). Unlike the
# news tier — whose FTS scores by how many keywords overlap, so more is better —
# Wikipedia's generator=search ranks pages against the query *as a whole*, and
# every extra term pulls the ranking away from the entity. Measured on a replayed
# session: the top 3 keywords returned "Raspberry Pi" where all 8 returned
# "Personal computer", "List of Arduino boards", or nothing at all.
#
# Bare numerals are excluded outright: a stray figure from the conversation
# ("At approximately 5 45" → keyword "45") demoted "Toy Story 5" clean out of
# the ranking that the entity alone put at #2. Numbers help the news tier's
# FTS ("250,000" matches the headline); against an encyclopedia ranker they
# are pure poison.
_WIKI_KEYWORDS = 3
# The per-session cache holds the most recent query results only; conversations
# revisit topics, they don't archive them.
_CACHE_MAX = 32
# Wikimedia's User-Agent policy asks API clients to identify the tool and give a
# contact, and throttles hard the ones that don't. The original bare
# "tenir-cue-rag/1.0" was on the wrong side of that line (XERK-124).
# https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
_USER_AGENT = "tenir-cue-rag/1.0 (https://github.com/xerktech/tenir)"


class LiveEvidenceRetriever:
    def __init__(
        self,
        *,
        news_store: NewsStore | None,
        wikipedia_endpoint: str = "https://en.wikipedia.org",
        kiwix_endpoint: str = "",
        kiwix_book: str = "wikipedia_en_all",
        searxng_endpoint: str = "",
        searxng_engines: str = "",
        deadline_ms: int = 800,
        max_evidence: int = 6,
        http_client=None,
    ) -> None:
        self._news_store = news_store
        self._wikipedia = wikipedia_endpoint.rstrip("/")
        self._kiwix = kiwix_endpoint.rstrip("/")
        self._kiwix_book = kiwix_book
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
                headers={"User-Agent": _USER_AGENT},
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
        if not self._wikipedia and not self._kiwix:
            return []
        keywords = [k for k in query.keywords if not k[0].isdigit()][:_WIKI_KEYWORDS]
        if not keywords:  # an all-numeral window has no entity to look up
            return []
        # Live first: fresher (crowd-edited within hours vs a ZIM snapshot weeks
        # old) and better-ranked (measured, XERK-124). The local mirror steps in
        # only when the live site fails — which it does *fast* (429/refused), so
        # the fallback still fits inside the retrieval deadline.
        if self._wikipedia:
            try:
                return await self._wikipedia_live(keywords)
            except Exception:
                if not self._kiwix:
                    raise
                log.warning("live wikipedia failed; falling back to kiwix", exc_info=True)
                metrics.incr("cue.retrieval.kiwix_fallbacks")
        return await self._kiwix_search(keywords)

    async def _wikipedia_live(self, keywords: list[str]) -> list[Evidence]:
        # One round trip: generator=search feeds prop=extracts, so the search hits
        # come back *with* their intro text (measured ~300-400ms).
        resp = await self._http().get(
            f"{self._wikipedia}/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": " ".join(keywords),
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

    async def _kiwix_search(self, keywords: list[str]) -> list[Evidence]:
        """The offline mirror: kiwix-serve search, then each hit's lead paragraph.

        Kiwix's own search snippets are keyword-in-context fragments of
        navigational cruft ("'RPi' redirects here. For the dessert, see
        Raspberry pie."), useless as evidence — so the article itself is
        fetched and its first real paragraph used, like live Wikipedia's
        ``exintro``. All local: search ~50-250ms + ~30-110ms per article.
        """
        resp = await self._http().get(
            f"{self._kiwix}/search",
            params={
                "books.filter.name": self._kiwix_book,
                "pattern": " ".join(keywords),
                "pageLength": str(_WIKI_LIMIT),
                "format": "xml",
            },
        )
        resp.raise_for_status()
        # De-dupe by title before fetching leads: when several ZIM books match
        # the name filter (e.g. an old and a refreshed Wikipedia mirror side by
        # side), each contributes the same articles.
        hits, seen = [], set()
        for title, link in _parse_kiwix_search(resp.text):
            if title in seen:
                continue
            seen.add(title)
            hits.append((title, link))
            if len(hits) >= _WIKI_LIMIT:
                break
        leads = await asyncio.gather(
            *(self._kiwix_lead(link) for _, link in hits), return_exceptions=True
        )
        results = []
        for (title, _), lead in zip(hits, leads):
            if isinstance(lead, BaseException) or not lead:
                continue
            results.append(
                Evidence(source="Wikipedia", title=title, snippet=lead[:_MAX_SNIPPET_CHARS])
            )
        return results

    async def _kiwix_lead(self, link: str) -> str:
        resp = await self._http().get(f"{self._kiwix}{link}")
        resp.raise_for_status()
        return _first_paragraph(resp.text)

    async def _searxng_tier(self, query: RetrievalQuery) -> list[Evidence]:
        if not self._searxng:
            return []
        # timeout_limit bounds how long SearXNG waits on its upstream engines
        # for THIS request (XERK-124). Without it, one slow engine holds the
        # whole response until the instance's per-engine timeout (measured:
        # 3.1s with a hanging engine) — past the retrieval deadline, so the
        # tier contributed nothing even though the other engines had answered.
        # Bounded just under the deadline (~200ms margin for LAN + JSON), a
        # slow engine now costs its own results only: whatever arrived in time
        # still rides the prompt on the turn it was asked for. The working
        # engines answer in ~1.0-1.2s, comfortably inside the bound; anything
        # slower is in practice a timeout or a suspension, not results.
        timeout_limit = max(0.5, self._deadline_ms / 1000 - 0.2)
        params = {
            "q": query.text or " ".join(query.keywords),
            "format": "json",
            "timeout_limit": f"{timeout_limit:.1f}",
        }
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
        key = query.cache_key()
        cached = self._cache.get(key)

        # The news tier is local and single-digit ms, so it always runs — its
        # corpus is the one that actually moves during a conversation.
        #
        # The two network tiers are skipped outright while the topic is one we
        # already fetched for (XERK-124). They used to re-fire on every finalized
        # turn, which on a steady topic is the same query several times a minute:
        # Wikipedia answered the first few and then served 429s for the rest of
        # the session, so the tier was dead exactly when a conversation stayed
        # interesting long enough to be worth grounding. A conversation revisits
        # topics rather than archiving them, so the cached answer for an
        # unchanged query is the same answer the refetch would return.
        tiers = {"news": asyncio.create_task(self._news_tier(query))}
        if cached is None:
            tiers["wikipedia"] = asyncio.create_task(self._wikipedia_tier(query))
            tiers["searxng"] = asyncio.create_task(self._searxng_tier(query))
        else:
            metrics.incr("cue.retrieval.cache_hits")

        done, pending = await asyncio.wait(
            tiers.values(), timeout=self._deadline_ms / 1000
        )

        # Tier order (news → wikipedia → searxng) sets prompt order: freshest and
        # most curated first, so budget trimming drops the web tail first.
        local: list[Evidence] = []
        network: list[Evidence] = []
        for name, task in tiers.items():
            if task in done:
                try:
                    (local if name == "news" else network).extend(task.result())
                except Exception:
                    log.warning("cue retrieval tier %r failed", name, exc_info=True)
                    metrics.incr("cue.retrieval.tier_errors")
            else:
                metrics.incr("cue.retrieval.tier_deadline_misses")

        # Let slow tiers finish into the cache for the next consideration on this
        # topic, instead of discarding work already in flight. Only the network
        # tiers are cached; a slow news pass is still awaited (so it is cancelled
        # cleanly on close) but its result is re-fetched fresh next time.
        if pending:
            late_network = {t for n, t in tiers.items() if t in pending and n != "news"}
            late = asyncio.create_task(self._settle_late(key, network, late_network, pending))
            self._late_tasks.add(late)
            late.add_done_callback(self._late_tasks.discard)

        if network:
            self._remember(key, network)
        elif cached is not None:
            network = cached

        return (local + network)[: self._max_evidence]

    async def _settle_late(
        self,
        cache_key: str,
        on_time: list[Evidence],
        network: set[asyncio.Task],
        pending: set[asyncio.Task],
    ) -> None:
        """Await past-deadline tiers and settle the network ones into the cache.

        Everything in ``pending`` is awaited so no task is left dangling, but only
        the ``network`` subset is remembered — the local news tier is cheap enough
        to redo, and caching it would double up against the fresh pass.
        """
        waiting = list(pending)  # fix an order to zip results back against
        results = await asyncio.gather(*waiting, return_exceptions=True)
        late: list[Evidence] = []
        for task, result in zip(waiting, results):
            if isinstance(result, BaseException):
                metrics.incr("cue.retrieval.tier_errors")
            elif task in network:
                late.extend(result)
        if late:
            self._remember(cache_key, list(on_time) + late)

    def _remember(self, key: str, evidence: list[Evidence]) -> None:
        self._cache[key] = evidence
        while len(self._cache) > _CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))


def _parse_kiwix_search(xml_text: str) -> list[tuple[str, str]]:
    """(title, content link) pairs from a kiwix-serve RSS search response.

    Defensive by construction: a malformed document yields [] rather than an
    exception — the caller treats an empty fallback like any other empty tier.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    hits = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link.startswith("/"):
            hits.append((title, link))
    return hits


def _first_paragraph(html_text: str, *, min_chars: int = 80) -> str:
    """The article's first real paragraph, tags stripped.

    Kiwix serves full article HTML; the opening <p> elements are hatnotes and
    infobox scraps, so the first paragraph longer than ``min_chars`` is taken —
    the same content live Wikipedia's ``exintro`` returns.
    """
    for match in _PARAGRAPH_RE.finditer(html_text):
        text = html.unescape(_TAG_RE.sub("", match.group(1))).strip()
        if len(text) >= min_chars:
            return " ".join(text.split())
    return ""


def _domain_label(url: str) -> str:
    """A web hit's attribution label: its bare domain ("bbc.co.uk"), or "Web"
    when the URL doesn't parse."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host.removeprefix("www.") or "Web"
