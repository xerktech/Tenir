"""RSS/Atom ingest for the cue news corpus (XERK-120): the stdlib feed parser
and the ingest pass (against httpx.MockTransport — no network)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from api.cue.rss import feed_urls, ingest_once, parse_feed
from api.persistence.news import InMemoryNewsStore

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BBC News</title>
    <item>
      <title>Volcano erupts &amp; town evacuated</title>
      <description><![CDATA[<p>An eruption began <b>overnight</b>.</p>]]></description>
      <link>https://bbc.test/vol</link>
      <guid>bbc-vol-1</guid>
      <pubDate>Fri, 24 Jul 2026 09:12:00 GMT</pubDate>
    </item>
    <item>
      <title>Untitled follows</title>
      <link>https://bbc.test/2</link>
    </item>
    <item><title></title><description>no title, skipped</description></item>
  </channel>
</rss>"""

_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>The Verge</title>
  <entry>
    <title>Chip launch</title>
    <summary>A new chip launched.</summary>
    <link href="https://verge.test/chip"/>
    <id>verge-chip-1</id>
    <published>2026-07-23T10:00:00Z</published>
  </entry>
</feed>"""


def test_parse_rss_items() -> None:
    items = parse_feed(_RSS)
    assert len(items) == 2  # the title-less item is skipped
    first = items[0]
    assert first.source == "BBC News"
    assert first.title == "Volcano erupts & town evacuated"
    assert first.summary == "An eruption began overnight."  # tags stripped
    assert first.link == "https://bbc.test/vol"
    assert first.published_at == datetime(2026, 7, 24, 9, 12, tzinfo=timezone.utc)


def test_parse_rss_missing_date_stamps_now() -> None:
    items = parse_feed(_RSS)
    assert items[1].published_at.tzinfo is not None
    assert (datetime.now(timezone.utc) - items[1].published_at).total_seconds() < 60


def test_parse_atom_items() -> None:
    items = parse_feed(_ATOM)
    assert len(items) == 1
    item = items[0]
    assert item.source == "The Verge"
    assert item.title == "Chip launch"
    assert item.summary == "A new chip launched."
    assert item.link == "https://verge.test/chip"
    assert item.published_at == datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)


def test_parse_ids_are_stable_for_upsert() -> None:
    assert [i.id for i in parse_feed(_RSS)] == [i.id for i in parse_feed(_RSS)]


def test_parse_unknown_root_yields_nothing() -> None:
    assert parse_feed("<html><body>not a feed</body></html>") == []


def test_feed_urls_trims_and_dedupes() -> None:
    assert feed_urls(" a , b ,a,, ") == ["a", "b"]
    assert feed_urls("") == []


def test_ingest_once_upserts_and_survives_a_dead_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.test":
            return httpx.Response(200, text=_RSS)
        return httpx.Response(500)

    store = InMemoryNewsStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run() -> int:
        async with client:
            return await ingest_once(
                store,
                ["https://good.test/rss", "https://dead.test/rss"],
                keep_days=14,
                client=client,
            )

    total = asyncio.run(run())
    assert total == 2
    assert store.count() == 2
    # Re-ingesting the same feed upserts, not duplicates.
    client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def rerun() -> None:
        async with client2:
            await ingest_once(store, ["https://good.test/rss"], keep_days=14, client=client2)

    asyncio.run(rerun())
    assert store.count() == 2


def test_ingest_once_prunes_old_items() -> None:
    old_rss = _RSS.replace("Fri, 24 Jul 2026 09:12:00 GMT", "Mon, 01 Jan 2024 00:00:00 GMT")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=old_rss)

    store = InMemoryNewsStore()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await ingest_once(store, ["https://good.test/rss"], keep_days=14, client=client)

    asyncio.run(run())
    # The 2024-dated item was pruned; the undated one (stamped now) survives.
    assert store.count() == 1
