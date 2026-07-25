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


# ---- query builder on real speech (XERK-124) --------------------------------
#
# The cases above are clean written prose. Live STT is not, and the gap between
# the two is what made production retrieve nothing on every single cue call.


def test_query_ignores_sentence_opening_capitals() -> None:
    # "Right"/"Now" open sentences; only "Lisbon" is actually a name. Looking at
    # the single preceding character finds the space after "back.", not the
    # period, and scores every sentence opener as a proper noun.
    q = build_query(["We got back. Right, now the topic is Lisbon. Now listen."])
    assert "lisbon" in q.keywords
    assert q.keywords[0] == "lisbon"


def test_query_drops_speech_filler() -> None:
    q = build_query(
        [
            "So basically the thing is, I actually thought it was gonna work.",
            "Honestly it's kind of a pretty big deal, you know, seriously.",
        ]
    )
    for filler in ("basically", "thing", "actually", "gonna", "honestly", "kinda",
                   "pretty", "seriously", "it's", "thought"):
        assert filler not in q.keywords, filler


def test_query_keeps_proper_noun_runs_over_stopwords() -> None:
    # "New" is stopworded as filler on its own; inside a capitalized run it is
    # half of the entity and has to survive.
    q = build_query(["I moved to New York last spring."])
    assert "new" in q.keywords
    assert "york" in q.keywords


def test_query_run_outranks_lone_capital() -> None:
    q = build_query(["We met Sarah near the Golden Gate Bridge."])
    assert q.keywords.index("golden") < q.keywords.index("sarah")


def test_query_keeps_subject_named_several_turns_back() -> None:
    # A speaker names the subject once, then talks about it. Scoring only the
    # newest turns loses the one word that makes the query answerable.
    turns = [
        "Have you tried the Raspberry Pi touch display yet?",
        "It's 10.1 inches diagonal, 1200 by 1920.",
        "224 ppi, and 400 candelas per square meter.",
        "The driver board lives inside the enclosure now.",
        "You can use all 10 fingers on it.",
    ]
    q = build_query(turns)
    assert "raspberry" in q.keywords
    assert "pi" in q.keywords


def test_query_rations_bare_figures() -> None:
    # A spec-heavy stretch must not fill every slot with numerals; some figures
    # ride (news FTS matches them) but the subject keeps its place.
    q = build_query(
        [
            "The Hubble telescope came up.",
            "1200 by 1920, 224 ppi, 400 candelas, 85 degrees, 60 percent, 16 mm.",
        ]
    )
    assert sum(k[0].isdigit() for k in q.keywords) <= 2
    assert "hubble" in q.keywords


def test_query_text_is_cut_at_a_word_boundary() -> None:
    q = build_query(["No strings attached, so I can say what I want. " * 12])
    assert len(q.text) <= 200
    # A blind slice left the web tier searching for fragments like "trings".
    assert not q.text.startswith("trings")
    assert q.text.split(" ", 1)[0] in q.text


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
    # The engine wait is bounded just under the retrieval deadline (XERK-124):
    # one slow engine must cost its own results, not the whole tier's.
    assert seen["sx.test"].params["timeout_limit"] == "1.8"  # deadline 2000ms
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


def test_live_retriever_wikipedia_searches_with_only_the_top_keywords() -> None:
    # generator=search ranks against the whole string, so padding the query with
    # low-scoring terms drags the result off the entity (XERK-124).
    seen: dict[str, httpx.URL] = {}

    def spy(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.url
        return _dispatch(request)

    r = _retriever(spy)
    turns = ["The Eiffel Tower in Paris was finished in 1889 and stands 330 metres tall."]
    asyncio.run(r.retrieve(turns))
    searched = seen["wiki.test"].params["gsrsearch"].split()
    assert len(searched) <= 3
    # Bare numerals are ranking poison against the encyclopedia: a stray "45"
    # from the conversation demoted "Toy Story 5" out of the top hits that the
    # entity alone retrieved (XERK-124). Figures stay out of this tier entirely.
    assert not any(k[0].isdigit() for k in searched)
    # The web tier is unaffected: it queries with prose, not keywords.
    assert len(seen["sx.test"].params["q"]) > 0
    asyncio.run(r.close())


def test_live_retriever_wikipedia_skips_an_all_numeral_query() -> None:
    calls: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return _dispatch(request)

    r = _retriever(spy, searxng_endpoint="")
    asyncio.run(r.retrieve(["1200 by 1920. 224. 400. 85 60 16 10."]))
    assert "wiki.test" not in calls  # no entity to look up, no wasted round trip
    asyncio.run(r.close())


def test_live_retriever_reuses_cached_network_evidence_for_an_unchanged_topic() -> None:
    # Re-firing Wikipedia on every finalized turn is what earned production a
    # session-long stream of 429s (XERK-124).
    calls: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return _dispatch(request)

    r = _retriever(counting)
    turns = ["Talking about the Eiffel Tower today."]
    first = asyncio.run(r.retrieve(turns))
    assert first
    after_first = list(calls)

    second = asyncio.run(r.retrieve(turns))
    assert calls == after_first  # no further network calls for the same topic
    assert [e.source for e in second] == [e.source for e in first]
    asyncio.run(r.close())


def test_live_retriever_refetches_when_the_topic_moves() -> None:
    calls: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return _dispatch(request)

    r = _retriever(counting)
    asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    before = len(calls)
    asyncio.run(r.retrieve(["Now the subject is Iceland volcanoes instead."]))
    assert len(calls) > before
    asyncio.run(r.close())


def test_live_retriever_news_tier_still_runs_on_a_cache_hit() -> None:
    # Only the network tiers are cached; the local corpus is the one that moves
    # during a conversation, so it is re-read every time.
    store = InMemoryNewsStore()
    r = _retriever(_dispatch, news_store=store)
    turns = ["Talking about the Eiffel Tower today."]
    asyncio.run(r.retrieve(turns))

    store.upsert([_item("n1", "Eiffel Tower repainted", "The repaint began.")])
    second = asyncio.run(r.retrieve(turns))
    assert second[0].source == "Test Feed"  # freshly ingested item, news tier first
    asyncio.run(r.close())


# ---- kiwix fallback for the encyclopedia tier (XERK-124) --------------------

_KIWIX_SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Search: eiffel tower</title>
    <item>
      <title>Eiffel Tower</title>
      <link>/content/wikipedia_en_all_nopic/Eiffel_Tower</link>
      <description>...keyword fragment cruft...</description>
    </item>
    <item>
      <title>Champ de Mars</title>
      <link>/content/wikipedia_en_all_nopic/Champ_de_Mars</link>
      <description>...more cruft...</description>
    </item>
  </channel>
</rss>"""

_KIWIX_ARTICLE_HTML = (
    "<html><body>"
    '<p class="hatnote">For other uses, see disambiguation.</p>'
    "<p>The <b>Eiffel Tower</b> is a lattice tower on the Champ de Mars in "
    "Paris, France, completed in 1889 and 330 metres tall.</p>"
    "</body></html>"
)


def _kiwix_dispatch(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/search":
        return httpx.Response(200, text=_KIWIX_SEARCH_XML)
    if request.url.path.startswith("/content/"):
        return httpx.Response(200, text=_KIWIX_ARTICLE_HTML)
    raise AssertionError(f"unexpected kiwix path {request.url.path}")


def test_wikipedia_tier_falls_back_to_kiwix_when_live_fails() -> None:
    # The 429 storms that opened XERK-124: live Wikipedia errors fast, and the
    # local ZIM mirror steps in with the article lead as the snippet.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wiki.test":
            return httpx.Response(429)
        if request.url.host == "kiwix.test":
            return _kiwix_dispatch(request)
        return _dispatch(request)

    r = _retriever(handler, searxng_endpoint="", kiwix_endpoint="https://kiwix.test")
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert [e.title for e in evidence] == ["Eiffel Tower", "Champ de Mars"]
    assert all(e.source == "Wikipedia" for e in evidence)
    # The snippet is the article's first REAL paragraph — the hatnote is skipped.
    assert "lattice tower" in evidence[0].snippet
    assert "disambiguation" not in evidence[0].snippet
    asyncio.run(r.close())


def test_wikipedia_tier_prefers_live_when_it_works() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "kiwix.test":
            return _kiwix_dispatch(request)
        return _dispatch(request)

    r = _retriever(handler, searxng_endpoint="", kiwix_endpoint="https://kiwix.test")
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert "kiwix.test" not in calls  # fresher + better-ranked live tier won
    assert any(e.source == "Wikipedia" for e in evidence)
    asyncio.run(r.close())


def test_wikipedia_tier_without_kiwix_still_degrades_to_partial_evidence() -> None:
    # No fallback configured (the default): a live failure behaves exactly as
    # before — the tier errors, the other tiers still contribute.
    def wiki_down(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wiki.test":
            return httpx.Response(429)
        return _dispatch(request)

    r = _retriever(wiki_down)
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert evidence
    assert all(e.source != "Wikipedia" for e in evidence)
    asyncio.run(r.close())


def test_kiwix_fallback_dedupes_hits_across_books() -> None:
    # Two ZIM books matching the name filter (an old and a refreshed mirror)
    # each contribute the same articles; only one lead per title is fetched.
    duplicated = _KIWIX_SEARCH_XML.replace(
        "</channel>",
        "<item><title>Eiffel Tower</title>"
        "<link>/content/wikipedia_en_all_maxi/Eiffel_Tower</link></item></channel>",
    )
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wiki.test":
            return httpx.Response(429)
        if request.url.path == "/search":
            return httpx.Response(200, text=duplicated)
        fetched.append(request.url.path)
        return httpx.Response(200, text=_KIWIX_ARTICLE_HTML)

    r = _retriever(handler, searxng_endpoint="", kiwix_endpoint="https://kiwix.test")
    evidence = asyncio.run(r.retrieve(["Talking about the Eiffel Tower today."]))
    assert [e.title for e in evidence] == ["Eiffel Tower", "Champ de Mars"]
    assert len(fetched) == 2  # the duplicate title cost no extra article fetch
    asyncio.run(r.close())


def test_parse_kiwix_search_is_defensive() -> None:
    from api.cue.retrieval.live import _parse_kiwix_search

    assert _parse_kiwix_search("not xml at all") == []
    assert _parse_kiwix_search("<rss><channel></channel></rss>") == []
    hits = _parse_kiwix_search(_KIWIX_SEARCH_XML)
    assert hits[0] == ("Eiffel Tower", "/content/wikipedia_en_all_nopic/Eiffel_Tower")


def test_first_paragraph_skips_short_hatnotes_and_strips_tags() -> None:
    from api.cue.retrieval.live import _first_paragraph

    lead = _first_paragraph(_KIWIX_ARTICLE_HTML)
    assert lead.startswith("The Eiffel Tower is a lattice tower")
    assert "<" not in lead
    assert _first_paragraph("<html><p>short</p></html>") == ""


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
