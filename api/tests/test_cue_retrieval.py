"""Cue evidence retrieval (XERK-120): the transcript→query builder, the news
store contract, the live retriever's tiers (against httpx.MockTransport — no
network), its deadline/cache behaviour, and the factory."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from api.config import settings
from api.cue.retrieval import build_query, make_evidence_retriever
from api.cue.retrieval.base import Evidence
from api.cue.retrieval.live import LiveEvidenceRetriever
from api.cue.retrieval.stub import StubEvidenceRetriever
from api.persistence.news import InMemoryNewsStore, NewsItem

# ---- query builder ----------------------------------------------------------


def test_query_prefers_proper_nouns_and_numbers() -> None:
    q = build_query(["I heard the Great Wall of China is 21000 km long."])
    assert "great" in q.keywords
    assert "wall" in q.keywords
    assert "china" in q.keywords
    assert "21000" in q.keywords


def test_query_strips_trailing_punctuation_from_numbers() -> None:
    q = build_query(["That was back in 1990."])
    assert "1990" in q.keywords
    assert "1990." not in q.keywords


def test_query_drops_stopwords_and_is_casefolded() -> None:
    q = build_query(["What do you know about the weather in Lisbon?"])
    assert "the" not in q.keywords
    assert "what" not in q.keywords
    assert "lisbon" in q.keywords
    assert all(k == k.casefold() for k in q.keywords)


def test_query_weights_later_turns_higher() -> None:
    q = build_query(["Talking about Portugal today.", "Now the topic is Iceland instead."])
    assert q.keywords.index("iceland") < q.keywords.index("portugal")


def test_query_empty_transcript_is_falsy() -> None:
    q = build_query(["", "   "])
    assert not q
    assert q.keywords == []


def test_query_works_on_lowercase_stt_output() -> None:
    # STT transcripts don't guarantee casing; content words still form a query.
    q = build_query(["how far away is the eiffel tower from paris"])
    assert "eiffel" in q.keywords
    assert "tower" in q.keywords


def test_query_text_is_capped_and_keywords_bounded() -> None:
    q = build_query(["Ramble " * 100 + " Endpoint"])
    assert len(q.text) <= 200
    assert len(q.keywords) <= 8


def test_query_cache_key_is_stable() -> None:
    a = build_query(["Talking about Iceland volcanoes."])
    b = build_query(["Talking about Iceland volcanoes."])
    assert a.cache_key() == b.cache_key()


# ---- news store (memory contract) -------------------------------------------


def _item(id_: str, title: str, summary: str = "", *, days_old: int = 0) -> NewsItem:
    return NewsItem(
        id=id_,
        source="Test Feed",
        title=title,
        summary=summary,
        published_at=datetime.now(timezone.utc) - timedelta(days=days_old),
    )


def test_news_store_upserts_by_id() -> None:
    store = InMemoryNewsStore()
    store.upsert([_item("a", "First title")])
    store.upsert([_item("a", "Updated title")])
    assert store.count() == 1
    assert store.search(["updated"], limit=5)[0].title == "Updated title"


def test_news_store_search_ranks_overlap_then_recency() -> None:
    store = InMemoryNewsStore()
    store.upsert(
        [
            _item("both", "Iceland volcano eruption", "ash cloud", days_old=2),
            _item("one-old", "Iceland tourism rebounds", days_old=5),
            _item("one-new", "Iceland election results", days_old=0),
            _item("none", "Completely unrelated story"),
        ]
    )
    got = store.search(["iceland", "volcano"], limit=3)
    assert [i.id for i in got] == ["both", "one-new", "one-old"]


def test_news_store_search_empty_keywords() -> None:
    store = InMemoryNewsStore()
    store.upsert([_item("a", "Anything")])
    assert store.search([]) == []
    assert store.search(["x"]) == []  # single-char tokens never match


def test_news_store_prune_drops_only_old_items() -> None:
    store = InMemoryNewsStore()
    store.upsert([_item("old", "Old news", days_old=30), _item("new", "New news")])
    assert store.prune(keep_days=14) == 1
    assert store.count() == 1
    assert store.search(["news"], limit=5)[0].id == "new"


# ---- live retriever tiers (MockTransport) -----------------------------------


def _wiki_payload() -> dict:
    return {
        "query": {
            "pages": {
                "1": {"index": 2, "title": "Second Hit", "extract": "Second extract."},
                "2": {"index": 1, "title": "Eiffel Tower", "extract": "A tower in Paris."},
                "3": {"index": 3, "title": "No Extract", "extract": ""},
            }
        }
    }


def _searxng_payload() -> dict:
    return {
        "results": [
            {
                "title": "BBC report",
                "content": "Something happened.",
                "url": "https://www.bbc.co.uk/news/1",
                "publishedDate": "2026-07-24T09:00:00",
            },
            {"title": "No content", "content": "", "url": "https://x.example"},
            {"title": "Web hit", "content": "Details.", "url": "not a url"},
        ]
    }


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _retriever(handler, **kwargs) -> LiveEvidenceRetriever:
    defaults = dict(
        news_store=None,
        wikipedia_endpoint="https://wiki.test",
        searxng_endpoint="https://sx.test",
        searxng_engines="startpage,bing",
        deadline_ms=2000,
        http_client=_mock_client(handler),
    )
    defaults.update(kwargs)
    return LiveEvidenceRetriever(**defaults)


def _dispatch(request: httpx.Request) -> httpx.Response:
    if request.url.host == "wiki.test":
        return httpx.Response(200, json=_wiki_payload())
    if request.url.host == "sx.test":
        return httpx.Response(200, json=_searxng_payload())
    raise AssertionError(f"unexpected host {request.url.host}")


def test_live_retriever_merges_tiers_in_freshness_order() -> None:
    store = InMemoryNewsStore()
    store.upsert([_item("n1", "Eiffel Tower repainted", "The repaint began.")])
    r = _retriever(_dispatch, news_store=store)

    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    # news first, then wikipedia (index order), then web.
    assert [e.source for e in evidence] == [
        "Test Feed",
        "Wikipedia",
        "Wikipedia",
        "bbc.co.uk",
        "Web",
    ]
    wiki = [e for e in evidence if e.source == "Wikipedia"]
    assert wiki[0].title == "Eiffel Tower"  # gsr index rank, not dict order
    assert evidence[0].published is not None  # news items carry their date
    bbc = next(e for e in evidence if e.source == "bbc.co.uk")
    assert bbc.published == "2026-07-24"
    asyncio.run(r.close())


def test_live_retriever_sends_pinned_engines_and_query_text(monkeypatch) -> None:
    seen: dict[str, httpx.URL] = {}

    def spy(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.url
        return _dispatch(request)

    r = _retriever(spy)
    asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert seen["sx.test"].params["engines"] == "startpage,bing"
    assert seen["sx.test"].params["format"] == "json"
    assert "eiffel" in seen["sx.test"].params["q"].casefold()
    assert "eiffel" in seen["wiki.test"].params["gsrsearch"]
    asyncio.run(r.close())


def test_live_retriever_disabled_tiers_are_skipped() -> None:
    def only_wiki(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "wiki.test"
        return httpx.Response(200, json=_wiki_payload())

    r = _retriever(only_wiki, searxng_endpoint="")
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert {e.source for e in evidence} == {"Wikipedia"}
    asyncio.run(r.close())


def test_live_retriever_tier_failure_yields_partial_evidence() -> None:
    def wiki_down(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wiki.test":
            return httpx.Response(500)
        return _dispatch(request)

    r = _retriever(wiki_down)
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert evidence  # searxng still contributed
    assert all(e.source != "Wikipedia" for e in evidence)
    asyncio.run(r.close())


def test_live_retriever_empty_transcript_short_circuits() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call expected")

    r = _retriever(explode)
    assert asyncio.run(r.retrieve(["", " "])) == []
    asyncio.run(r.close())


def test_live_retriever_caps_total_evidence() -> None:
    store = InMemoryNewsStore()
    store.upsert([_item(f"n{i}", f"Eiffel story {i}", "Tower news.") for i in range(5)])
    r = _retriever(_dispatch, news_store=store, max_evidence=4)
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert len(evidence) == 4
    asyncio.run(r.close())


def test_live_retriever_deadline_miss_serves_cache_next_time() -> None:
    async def run() -> None:
        release = asyncio.Event()

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "sx.test":
                await release.wait()  # slower than the deadline, then settles
            return _dispatch(request)

        r = _retriever(slow_handler, wikipedia_endpoint="", deadline_ms=50)
        first = await r.retrieve(["Talking about the Eiffel Tower today."])
        assert first == []  # only tier was past-deadline

        release.set()
        await asyncio.gather(*r._late_tasks)  # late tier settles into the cache

        second = await r.retrieve(["Talking about the Eiffel Tower today."])
        # The follow-up on the same topic gets the slow tier's (cached) evidence
        # even though this pass timed out again.
        assert any(e.source == "bbc.co.uk" for e in second)
        await r.close()

    asyncio.run(run())


def test_live_retriever_close_cancels_late_tasks() -> None:
    async def run() -> None:
        async def hang(request: httpx.Request) -> httpx.Response:
            if request.url.host == "sx.test":
                await asyncio.sleep(3600)
            return _dispatch(request)

        r = _retriever(hang, wikipedia_endpoint="", deadline_ms=50)
        await r.retrieve(["Talking about the Eiffel Tower today."])
        assert r._late_tasks
        await r.close()
        await asyncio.sleep(0)  # let cancellation propagate
        assert all(t.cancelled() or t.done() for t in r._late_tasks or set())

    asyncio.run(run())


# ---- stub + factory ---------------------------------------------------------


def test_stub_retriever_is_deterministic_and_offline() -> None:
    ev1 = asyncio.run(StubEvidenceRetriever().retrieve(["Talking about the Eiffel Tower."]))
    ev2 = asyncio.run(StubEvidenceRetriever().retrieve(["Talking about the Eiffel Tower."]))
    assert ev1 == ev2
    assert ev1[0].source == "Stub Reference"
    assert asyncio.run(StubEvidenceRetriever().retrieve([""])) == []


def test_factory_selects_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_retrieval_backend", "off")
    assert make_evidence_retriever() is None
    monkeypatch.setattr(settings, "cue_retrieval_backend", "stub")
    assert isinstance(make_evidence_retriever(), StubEvidenceRetriever)
    monkeypatch.setattr(settings, "cue_retrieval_backend", "live")
    live = make_evidence_retriever()
    assert isinstance(live, LiveEvidenceRetriever)
    monkeypatch.setattr(settings, "cue_retrieval_backend", "bogus")
    with pytest.raises(ValueError):
        make_evidence_retriever()


def test_evidence_serializes_for_prompts() -> None:
    # Evidence is a plain dataclass a prompt builder can format directly.
    e = Evidence(source="BBC News", title="T", snippet="S", published="2026-07-24")
    assert json.dumps(e.__dict__)  # no exotic types
