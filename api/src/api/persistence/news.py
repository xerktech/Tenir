"""News-item store: the RSS-fed knowledge corpus behind cue retrieval (XERK-120).

Recent items from the configured RSS feeds are ingested here (see
``api.cue.rss``) and searched at cue time so cue facts about current events are
grounded in fresh reporting instead of the cue model's years-stale training
data. Same seam pattern as the conversation store: an in-memory backend for
CI/dev and a Postgres swap behind one Protocol.

Why keyword FTS and not a vector index (the ticket floated one): the corpus is
deliberately tiny — a few thousand headlines+summaries, pruned after
``API_CUE_RSS_KEEP_DAYS`` — and the queries are entity keywords pulled from the
transcript (names, places, numbers), which keyword search matches with high
precision. Postgres FTS answers in single-digit ms with zero new
infrastructure, while a vector index would add an embedding model (a new GPU
tenant or a heavy CPU dependency), embedding latency on the cue hot path, and
an operational moving part — for marginal recall gain on a corpus this small
and this fresh. The seam keeps a vector swap possible if the corpus ever grows
beyond what FTS serves well; see docs/cue-rag.md for the measurements.

The store is household-free on purpose: news is public context, not user data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

# Tokens shorter than this never match (too noisy for a keyword corpus search).
_MIN_TOKEN = 2


@dataclass
class NewsItem:
    """One ingested feed entry. ``id`` is the feed GUID (or the link when the feed
    has none), so re-ingesting a feed upserts instead of duplicating."""

    id: str
    source: str  # short feed label, e.g. "BBC News"
    title: str
    summary: str
    published_at: datetime
    link: str | None = None


class NewsStore(Protocol):
    """Synchronous like the conversation store; callers offload via to_thread."""

    def upsert(self, items: list[NewsItem]) -> None: ...

    def search(self, keywords: list[str], *, limit: int = 4) -> list[NewsItem]:
        """Most relevant recent items matching any of the keywords, freshest
        first among equally-relevant hits."""
        ...

    def prune(self, *, keep_days: int) -> int:
        """Drop items older than ``keep_days``; returns how many were dropped."""
        ...

    def count(self) -> int: ...


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w']+", text.casefold()) if len(t) >= _MIN_TOKEN]


class InMemoryNewsStore:
    """Dict-backed store for CI/dev. Search is keyword-overlap scoring over
    title+summary — the same contract the SQL FTS backend serves."""

    def __init__(self) -> None:
        self._items: dict[str, NewsItem] = {}

    def upsert(self, items: list[NewsItem]) -> None:
        for item in items:
            self._items[item.id] = item

    def search(self, keywords: list[str], *, limit: int = 4) -> list[NewsItem]:
        wanted = {k.casefold() for k in keywords if len(k) >= _MIN_TOKEN}
        if not wanted:
            return []
        scored: list[tuple[int, datetime, NewsItem]] = []
        for item in self._items.values():
            hay = set(_tokens(item.title)) | set(_tokens(item.summary))
            score = len(wanted & hay)
            if score > 0:
                scored.append((score, item.published_at, item))
        # Highest score first; among equal scores, freshest first.
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [item for _, _, item in scored[:limit]]

    def prune(self, *, keep_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        stale = [k for k, v in self._items.items() if v.published_at < cutoff]
        for k in stale:
            del self._items[k]
        return len(stale)

    def count(self) -> int:
        return len(self._items)


class SqlNewsStore:
    """Postgres swap: the ``news_items`` table (schema.sql) searched with the same
    functional-GIN FTS pattern as transcript search, ranked by relevance then
    recency. Excluded from coverage like the other SQL stores; the contract is
    covered against ``InMemoryNewsStore`` and the schema by the compose stack."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    def _ensure_pool(self):  # pragma: no cover - requires psycopg + a live database
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(self._dsn, open=True)
        return self._pool

    def upsert(self, items: list[NewsItem]) -> None:  # pragma: no cover - live database
        if not items:
            return
        with self._ensure_pool().connection() as conn:
            conn.cursor().executemany(
                """
                INSERT INTO news_items (id, source, title, summary, link, published_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    source = EXCLUDED.source, title = EXCLUDED.title,
                    summary = EXCLUDED.summary, link = EXCLUDED.link,
                    published_at = EXCLUDED.published_at
                """,
                [(i.id, i.source, i.title, i.summary, i.link, i.published_at) for i in items],
            )

    def search(  # pragma: no cover - requires a live database
        self, keywords: list[str], *, limit: int = 4
    ) -> list[NewsItem]:
        wanted = [k for k in keywords if len(k) >= _MIN_TOKEN]
        if not wanted:
            return []
        from psycopg.rows import dict_row

        # OR the keywords: transcript keyword sets are noisy, so any-match with
        # rank ordering beats all-match returning nothing.
        query = " OR ".join(wanted)
        with self._ensure_pool().connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    """
                    SELECT *, ts_rank(to_tsvector('simple', title || ' ' || summary),
                                      websearch_to_tsquery('simple', %s)) AS rank
                    FROM news_items
                    WHERE to_tsvector('simple', title || ' ' || summary)
                          @@ websearch_to_tsquery('simple', %s)
                    ORDER BY rank DESC, published_at DESC
                    LIMIT %s
                    """,
                    (query, query, limit),
                )
                .fetchall()
            )
        return [
            NewsItem(
                id=r["id"],
                source=r["source"],
                title=r["title"],
                summary=r["summary"],
                link=r["link"],
                published_at=r["published_at"],
            )
            for r in rows
        ]

    def prune(self, *, keep_days: int) -> int:  # pragma: no cover - live database
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with self._ensure_pool().connection() as conn:
            cur = conn.execute("DELETE FROM news_items WHERE published_at < %s", (cutoff,))
            return cur.rowcount

    def count(self) -> int:  # pragma: no cover - requires a live database
        with self._ensure_pool().connection() as conn:
            row = conn.execute("SELECT count(*) FROM news_items").fetchone()
            return int(row[0])


_news_store: NewsStore | None = None


def get_news_store() -> NewsStore:
    """The shared news store: Postgres when the conversation store is Postgres
    (one durable database), in-memory otherwise. Built lazily — only cue
    retrieval needs it, so the stripped core never touches it."""
    global _news_store
    if _news_store is None:
        from api.config import settings

        if settings.persistence_backend == "postgres":
            _news_store = SqlNewsStore(settings.database_url)
        else:
            _news_store = InMemoryNewsStore()
    return _news_store


def reset_news_store() -> None:
    """Testing hook: forget the singleton so the next get rebuilds from settings."""
    global _news_store
    _news_store = None
