"""RSS/Atom ingest for the cue news corpus (XERK-120).

A background loop fetches the configured feeds (``API_CUE_RSS_FEEDS``) every
``API_CUE_RSS_INTERVAL_SECONDS``, parses them with a dependency-free stdlib
parser, and upserts the entries into the news store — the fresh-reporting
corpus cue retrieval searches so cues about current events are grounded in
this week's news, not the model's years-stale training data. Old items are
pruned each pass so the corpus stays small and recent by construction.

Parsing is deliberately stdlib-only (xml.etree + email.utils + html): the
subset of RSS 2.0 / Atom that curated news feeds emit is small, and one small
tested function beats a new dependency for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree

from api.persistence.news import NewsItem, NewsStore

log = logging.getLogger("api.cue.rss")

# Cap how much of an entry's description is kept: evidence snippets are short by
# design (they ride an LLM prompt), and some feeds inline whole articles.
_MAX_SUMMARY_CHARS = 500

_TAG_RE = re.compile(r"<[^>]+>")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _clean(text: str | None) -> str:
    """Feed text → plain text: tags stripped, entities unescaped, whitespace
    collapsed. Feeds embed HTML in titles/descriptions routinely. Tags become
    spaces (so ``a<br>b`` stays two words), then the space a stripped tag leaves
    before punctuation (``<b>x</b>.`` → "x .") is closed back up."""
    if not text:
        return ""
    collapsed = " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())
    return re.sub(r"\s+([.,;:!?])", r"\1", collapsed)[:_MAX_SUMMARY_CHARS]


def _parse_date(text: str | None) -> datetime | None:
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom published/updated) → aware UTC."""
    if not text:
        return None
    text = text.strip()
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_id(source: str, guid: str | None, link: str | None, title: str) -> str:
    """Stable id for upserting: the feed GUID, else the link, else a hash of
    source+title (some minimal feeds carry neither)."""
    basis = guid or link or f"{source}:{title}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def parse_feed(xml_text: str, *, fallback_source: str = "") -> list[NewsItem]:
    """Parse an RSS 2.0 or Atom document into news items.

    Entries without a parseable date are stamped "now": a fresh fetch implies
    recency, and a wrong-but-recent stamp only affects pruning order. Malformed
    XML raises ``ElementTree.ParseError`` for the caller to count.
    """
    root = ElementTree.fromstring(xml_text)
    items: list[NewsItem] = []

    if root.tag == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        if channel is None:
            return []
        source = _clean(channel.findtext("title")) or fallback_source or "News"
        for entry in channel.findall("item"):
            title = _clean(entry.findtext("title"))
            if not title:
                continue
            link = (entry.findtext("link") or "").strip() or None
            guid = (entry.findtext("guid") or "").strip() or None
            items.append(
                NewsItem(
                    id=_entry_id(source, guid, link, title),
                    source=source,
                    title=title,
                    summary=_clean(entry.findtext("description")),
                    link=link,
                    published_at=_parse_date(entry.findtext("pubDate"))
                    or datetime.now(timezone.utc),
                )
            )
        return items

    if root.tag == f"{_ATOM_NS}feed":
        source = _clean(root.findtext(f"{_ATOM_NS}title")) or fallback_source or "News"
        for entry in root.findall(f"{_ATOM_NS}entry"):
            title = _clean(entry.findtext(f"{_ATOM_NS}title"))
            if not title:
                continue
            link_el = entry.find(f"{_ATOM_NS}link")
            link = (link_el.get("href") or "").strip() or None if link_el is not None else None
            guid = (entry.findtext(f"{_ATOM_NS}id") or "").strip() or None
            summary = entry.findtext(f"{_ATOM_NS}summary") or entry.findtext(
                f"{_ATOM_NS}content"
            )
            published = _parse_date(
                entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated")
            )
            items.append(
                NewsItem(
                    id=_entry_id(source, guid, link, title),
                    source=source,
                    title=title,
                    summary=_clean(summary),
                    link=link,
                    published_at=published or datetime.now(timezone.utc),
                )
            )
        return items

    return []


def feed_urls(configured: str) -> list[str]:
    """The configured comma-separated feed list, trimmed and de-duplicated."""
    return list(dict.fromkeys(u.strip() for u in configured.split(",") if u.strip()))


async def ingest_once(store: NewsStore, urls: list[str], *, keep_days: int, client=None) -> int:
    """One ingest pass: fetch + parse + upsert every feed, then prune. Returns how
    many items were upserted. Per-feed failures are logged and counted, never
    raised — one dead feed must not starve the others. ``client`` is injectable
    for tests (httpx.MockTransport); by default one is built per pass."""
    import httpx

    from api.metrics import metrics

    total = 0
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, headers={"User-Agent": "tenir-cue-rss/1.0"}
        )
    try:
        for url in urls:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                items = parse_feed(resp.text)
            except Exception:
                log.warning("rss feed fetch/parse failed: %s", url, exc_info=True)
                metrics.incr("cue.rss.fetch_errors")
                continue
            if items:
                await asyncio.to_thread(store.upsert, items)
                total += len(items)
    finally:
        if own_client:
            await client.aclose()
    if total:
        metrics.incr("cue.rss.items_ingested", total)
    pruned = await asyncio.to_thread(store.prune, keep_days=keep_days)
    if pruned:
        log.info("pruned %d news items older than %d days", pruned, keep_days)
    return total


async def ingest_loop() -> None:
    """The background ingest task (started from the api lifespan when cue
    retrieval is live): an immediate first pass so the corpus is warm for the
    first session, then one pass per configured interval."""
    from api.config import settings
    from api.persistence.news import get_news_store

    urls = feed_urls(settings.cue_rss_feeds)
    if not urls:
        log.info("no RSS feeds configured; news corpus stays empty")
        return
    store = get_news_store()
    while True:
        try:
            count = await ingest_once(store, urls, keep_days=settings.cue_rss_keep_days)
            log.info("rss ingest pass done: %d items across %d feeds", count, len(urls))
        except Exception:
            # The loop must survive anything — a failed pass just waits for the next.
            log.exception("rss ingest pass failed")
        await asyncio.sleep(settings.cue_rss_interval_seconds)
