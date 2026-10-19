"""Encyclopedia fact check of cues about to be shown (api/cue/factcheck.py): the pure helpers,
the factory's gating, and the session dropping a contradicted cue while failing open."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import CaptionFinal, Cue, MicSource, ServerMessage
from api.cue import GeneratedCue, make_fact_checker
from api.cue import factcheck
from api.cue.factcheck import FactChecker, _json, passages, quoted, should_check
from api.session import Session

# ---- pure helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "cue, checked",
    [
        (GeneratedCue("Cubone", "Cubone is a Ghost-type Pokémon."), True),
        (GeneratedCue("Mesha Stele", "The stele dates to about 840 BC.", "Wikipedia"), True),
        # Live news is newer than the encyclopedia mirror.
        (GeneratedCue("Storm", "The storm made landfall Tuesday.", "BBC News"), False),
        # So is anything recent, from memory too (the 2026 Isaias vs the 2020 one).
        (GeneratedCue("Hurricane Isaias", "The first hurricane of the 2026 season."), False),
        (GeneratedCue("Pixel", "The latest Pixel has a 6.3-inch screen."), False),
    ],
)
def test_should_check_skips_live_evidence_and_recent_events(cue: GeneratedCue, checked: bool):
    assert should_check(cue) is checked


def test_quoted_requires_the_conflicting_words_to_be_in_the_reference() -> None:
    ref = "Cubone is a Ground-type Pokémon introduced in Generation I."
    assert quoted("a Ground-type Pokémon", ref)
    assert quoted("Ground type Pokemon introduced", ref + " Pokemon")  # near-verbatim counts
    assert not quoted("a Ghost-type Pokémon", "Gastly is a Ghost and Poison type.")  # invented
    assert not quoted("Ground", ref)  # one word is not a quote
    # A pasted paragraph matches anything, so it is not accepted as "the conflicting words".
    assert not quoted(" ".join([ref] * 6), ref)


def test_passages_keep_the_lead_and_paragraphs_that_mention_the_cue() -> None:
    page = (
        "<p>Papa Smurf is the leader of the Smurfs and the oldest of them.</p>"
        "<p>Unrelated history of the comic strip and its many publishers over decades.</p>"
        '<table class="infobox"><tr><td>Dressed in red pants and a red Phrygian cap</td></tr>'
        "</table>"
        "<p>short</p>"
    )
    text = passages(page, "Papa Smurf wears a white hat and white pants")
    assert text.startswith("Papa Smurf is the leader")
    assert "red pants" in text  # the infobox row
    assert "Unrelated history" not in text
    assert passages("<div>no paragraphs</div>", "anything") == ""


def test_passages_ignore_the_page_head_and_inline_styles() -> None:
    # Real Kiwix pages: <link> in <head> matched the paragraph pattern (<l..i..>) and inline
    # <style> sat in infobox cells, so the "lead" was kilobytes of CSS (QA, Oct 2026).
    page = (
        '<html><head><link rel="icon" href="x"><style>@media screen { .mw { color: red } }'
        "</style><title>Cubone</title></head><body>"
        "<p>Cubone is a Pokémon species in Nintendo and Game Freak's Pokémon franchise.</p>"
        '<table class="infobox vcard"><tr><th>Type</th><td><style>.x { width: 22em }</style>'
        "Ground</td></tr></table><p>It wears the skull of its dead mother and is a Ground type.</p>"
        # A navigation box: rows full of related names that out-score a fact on keyword count.
        "<table class=\"navbox\"><tr><td>Pokémon type list: Cubone Ghost Gastly Haunter type Pokémon"
        "</td></tr></table>"
        "</body></html>"
    )
    text = passages(page, "Cubone is a Ghost-type Pokémon that wears a skull")
    assert text.startswith("Cubone is a Pokémon species")
    assert "@media" not in text and "infobox" not in text and "icon" not in text
    assert "Type Ground" in text and "skull of its dead mother" in text  # the infobox row kept
    assert "Gastly" not in text  # navigation boxes are not facts


def test_short_list_items_are_facts_ahead_of_long_paragraphs() -> None:
    long_para = "<p>" + "Batman and Robin and Poison Ivy and Mr. Freeze in Gotham. " * 30 + "</p>"
    page = (
        "<p>Batman & Robin is a 1997 American superhero film based on DC Comics characters.</p>"
        + long_para * 3
        + "<ul><li>Uma Thurman as Doctor Pamela Isley / Poison Ivy</li></ul>"
    )
    text = passages(page, "Michelle Pfeiffer played Poison Ivy in Batman and Robin", limit=1500)
    assert "Uma Thurman as Doctor Pamela Isley" in text


def test_citations_and_title_words_never_crowd_out_a_long_cast_line() -> None:
    # The real Batman & Robin page with the production "title: body" note: every citation
    # repeats "Batman & Robin" and 1997, and the deciding cast line runs ~280 chars (QA pass 3).
    cast = (
        "Uma Thurman as Doctor Pamela Isley / Poison Ivy : A botanist-turned-eco-terrorist "
        "who wields a deadly kiss, plans to overthrow humanity with plant life, and uses her "
        "pheromones to turn Batman and Robin against each other while seducing the men of "
        "Gotham City."
    )
    assert len(cast) > 200
    citations = "".join(
        f"<li>Critic {i} (1997). \"Batman & Robin\". Review of Batman & Robin. 1997.</li>"
        for i in range(12)
    )
    page = (
        "<p>Batman & Robin is a 1997 American superhero film based on the DC Comics characters "
        "Batman and Robin.</p>"
        f"<ul><li>{cast}</li></ul>"
        f'<ol class="mw-references references">{citations}</ol>'
    )
    note = "Batman & Robin: Michelle Pfeiffer played Poison Ivy in the 1997 film Batman & Robin."
    text = passages(page, note, article_title="Batman & Robin (film)")
    assert "Uma Thurman as Doctor Pamela Isley" in text
    assert "Critic" not in text


def test_the_articles_own_title_words_rank_nothing() -> None:
    trivia = "".join(f"<li>Batman & Robin trivia: who played whom, item {i}</li>" for i in range(9))
    page = (
        "<p>Batman & Robin is a 1997 American superhero film based on DC Comics characters.</p>"
        f"<ul>{trivia}<li>Pfeiffer was never cast as Ivy</li></ul>"
    )
    note = "Batman & Robin: Michelle Pfeiffer played Poison Ivy in Batman & Robin."
    text = passages(page, note, article_title="Batman & Robin (film)")
    assert "Pfeiffer was never cast as Ivy" in text


def test_every_title_word_ranks_nothing_not_just_the_first() -> None:
    # A line matching only a later title word ("Robin", "film") is no fact about the cue.
    page = (
        "<p>Batman & Robin is a 1997 American superhero film based on DC Comics characters.</p>"
        "<ul><li>Robin film merchandise list</li><li>Pfeiffer was never cast as Ivy</li></ul>"
    )
    note = "Batman & Robin: Michelle Pfeiffer played Poison Ivy in the Robin film."
    text = passages(page, note, article_title="Batman & Robin (film)")
    assert "Pfeiffer was never cast as Ivy" in text
    assert "merchandise" not in text


def test_an_overlong_list_item_is_not_a_fact() -> None:
    blob = "Poison Ivy " * 60  # a list item the size of a paragraph is not a short fact
    page = f"<p>A lead paragraph that is long enough to be kept as the lead.</p><ul><li>{blob}</li></ul>"
    assert "Poison Ivy Poison Ivy" not in passages(page, "Poison Ivy")


def test_three_letter_words_count_as_keywords() -> None:
    page = (
        "<p>An article lead paragraph that is long enough to be kept as the lead.</p>"
        "<ul><li>Played by someone else: the Ivy role</li></ul>"
    )
    assert "Ivy role" in passages(page, "Ivy")


def test_json_reads_the_first_object_and_tolerates_junk() -> None:
    assert _json('sure: {"titles": ["A"]} done') == {"titles": ["A"]}
    assert _json("no json") == {}
    assert _json("{broken") == {}
    assert _json("[1, 2]") == {}


def test_check_prompt_spares_estimates_other_senses_and_unmentioned_claims() -> None:
    # Measured false drops each rule removed: "2-8 players" vs "2-10", a business
    # "inflection point" vs the mathematical one (Oct 2026 labelled set).
    prompt = factcheck._CHECK.lower()
    assert "does not mention the claim" in prompt
    assert "overlapping estimate" in prompt
    assert "different sense" in prompt
    assert "at most 20 conflicting words" in prompt


def test_payload_runs_without_thinking_and_short() -> None:
    checker = FactChecker(endpoint="http://m/v1", model="m", kiwix_endpoint="http://k", kiwix_book="b")
    payload = checker._payload("sys", "user")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] <= 200


# ---- the live path, against a mock transport ----------------------------------------


_KIWIX_CATALOG = '<feed><entry><link type="text/html" href="/content/b"/></entry></feed>'
_KIWIX_PAGE = (
    "<html><head><style>.x{}</style></head><body>"
    "<p>Cubone is a Pokémon species in Nintendo and Game Freak's Pokémon franchise.</p>"
    "<p>Cubone is a Ground type Pokémon that wears a skull on its head.</p></body></html>"
)


def _transport(
    verdict: dict,
    *,
    kiwix_fails: bool = False,
    calls: list | None = None,
    page_status: int = 200,
    kiwix_error=None,
):
    import json as _j

    import httpx

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            system = _j.loads(request.content)["messages"][0]["content"]
            answer = {"titles": ["Cubone", "Cubone", 7]} if "Name the" in system else verdict
            return httpx.Response(
                200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]}
            )
        if kiwix_fails:
            raise httpx.ConnectError("kiwix down")
        if kiwix_error is not None:
            raise kiwix_error
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(page_status, text=_KIWIX_PAGE)

    return httpx.MockTransport(handle)


def _checker(transport, budget_s: float = 5.0) -> FactChecker:
    return FactChecker(
        endpoint="http://llm/v1", model="m", kiwix_endpoint="http://kiwix", kiwix_book="b",
        budget_s=budget_s, transport=transport,
    )


_GHOST = GeneratedCue("Cubone", "Cubone is a Ghost-type Pokémon.")


def test_a_quoted_contradiction_drops_the_cue() -> None:
    calls: list = []
    verdict = {"contradiction": True, "reference": "a Ground type Pokémon"}
    assert _checker(_transport(verdict, calls=calls)).contradiction(_GHOST) == (
        "a Ground type Pokémon"
    )
    # Duplicate and non-string titles are filtered before any lookup: one article read, by title
    # (Kiwix full-text search takes seconds), after one catalog read for the book.
    assert calls.count("/content/b/Cubone") == 1
    assert calls.count("/catalog/v2/entries") == 1
    assert "/search" not in calls


@pytest.mark.parametrize(
    "verdict",
    [
        {"contradiction": True, "reference": "a Ghost type and Poison type"},  # not in the text
        {"contradiction": False, "reference": "a Ground type Pokémon"},
        {"contradiction": "yes", "reference": "a Ground type Pokémon"},
    ],
    ids=["invented-quote", "not-contradicted", "not-a-bool"],
)
def test_no_drop_without_a_real_quoted_contradiction(verdict: dict) -> None:
    assert _checker(_transport(verdict)).contradiction(_GHOST) is None


def test_the_check_prompt_gets_the_cast_line_past_title_repeating_citations() -> None:
    # Production wiring: the Kiwix hit's title must reach passages(), or every citation that
    # repeats it out-ranks the deciding cast line (QA pass 3/4).
    import json as _j

    import httpx

    citations = "".join(
        f'<li>Critic {i} (1997). "Batman & Robin". Batman & Robin review.</li>' for i in range(12)
    )
    page = (
        "<html><body><p>Batman & Robin is a 1997 American superhero film based on DC Comics "
        "characters.</p><ul><li>Uma Thurman as Doctor Pamela Isley / Poison Ivy : a botanist "
        "turned eco-terrorist</li></ul>"
        f'<ol class="mw-references references">{citations}</ol></body></html>'
    )
    # Citations a non-references list would carry, to out-score the cast line on title words.
    page = page.replace("</body>", f"<ul>{citations}</ul></body>")
    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" in msgs[0]["content"]:
                answer = {"titles": ["Batman & Robin (film)"]}
            else:
                prompts.append(msgs[1]["content"])
                answer = {"contradiction": False}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]})
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(200, text=page)

    cue = GeneratedCue(
        "Batman & Robin", "Michelle Pfeiffer played Poison Ivy in the 1997 film Batman & Robin."
    )
    assert _checker(httpx.MockTransport(handle)).contradiction(cue) is None
    assert len(prompts) == 1 and "Pamela Isley" in prompts[0]


def test_unchecked_cues_make_no_requests() -> None:
    calls: list = []
    checker = _checker(_transport({"contradiction": True}, calls=calls))
    assert checker.contradiction(GeneratedCue("Storm", "It hit Tuesday.", "BBC News")) is None
    assert calls == []


def test_a_failing_kiwix_switches_the_check_off_for_a_while() -> None:
    calls: list = []
    checker = _checker(_transport({"contradiction": True}, kiwix_fails=True, calls=calls))
    assert checker.contradiction(_GHOST) is None
    made = len(calls)
    assert checker.contradiction(_GHOST) is None
    assert len(calls) == made  # skipped outright: no model or Kiwix call


def test_a_missing_article_is_skipped_without_switching_the_check_off() -> None:
    checker = _checker(_transport({"contradiction": True, "reference": "x y"}, page_status=404))
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until == 0.0


def test_a_kiwix_server_error_switches_the_check_off() -> None:
    checker = _checker(_transport({"contradiction": True}, page_status=503))
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until > 0.0


def test_only_a_full_length_kiwix_timeout_switches_the_check_off() -> None:
    import httpx

    timeout = httpx.ReadTimeout("hung")
    checker = _checker(_transport({"contradiction": True}, kiwix_error=timeout))
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until > 0.0  # Kiwix had its full 2 s and hung
    # A request cut short by the check's own deadline (a slow model ate the budget) is not
    # Kiwix's fault.
    checker = _checker(_transport({"contradiction": True}, kiwix_error=timeout), budget_s=1.0)
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until == 0.0


def test_the_lookup_stops_at_the_first_kiwix_failure() -> None:
    calls: list = []
    checker = _checker(_transport({"contradiction": True}, kiwix_fails=True, calls=calls))
    checker._ask = lambda *a: {"titles": ["A", "B", "C"]}  # three titles to look up
    assert checker.contradiction(_GHOST) is None
    assert len([c for c in calls if not c.endswith("/chat/completions")]) == 1


def test_one_slow_article_among_answers_is_skipped_without_switching_the_check_off() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" in msgs[0]["content"]:
                answer = {"titles": ["Slow", "Cubone"]}
            else:
                prompts.append(msgs[1]["content"])
                answer = {"contradiction": False}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]})
        if path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        if path.endswith("/Slow"):
            raise httpx.ReadTimeout("cold article")
        return httpx.Response(200, text=_KIWIX_PAGE)

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until == 0.0
    assert prompts and "[Cubone]" in prompts[0]


def test_the_newest_snapshot_is_read_and_cached() -> None:
    import httpx

    calls: list = []
    catalog = (
        '<feed><link href="/content/wikipedia_en_all_maxi_2026-08"/>'
        '<link href="/content/wikipedia_en_all_nopic_2026-06"/></feed>'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=catalog)
        return httpx.Response(200, text=_KIWIX_PAGE)

    checker = _checker(httpx.MockTransport(handle))
    client = httpx.Client(transport=httpx.MockTransport(handle))
    deadline = factcheck.time.monotonic() + 5
    assert checker._book_path(client, deadline) == "/content/wikipedia_en_all_maxi_2026-08"
    assert checker._book_path(client, deadline) == "/content/wikipedia_en_all_maxi_2026-08"
    assert calls.count("/catalog/v2/entries") == 1


def test_a_redirect_names_the_article_it_landed_on() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" in msgs[0]["content"]:
                answer = {"titles": ["Super Saiyan"]}
            else:
                prompts.append(msgs[1]["content"])
                answer = {"contradiction": False}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]})
        if path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        if path == "/content/b/Super_Saiyan":
            return httpx.Response(302, headers={"location": "/content/b/Dragon_Ball"})
        return httpx.Response(200, text=_KIWIX_PAGE)

    assert _checker(httpx.MockTransport(handle)).contradiction(_GHOST) is None
    assert prompts and prompts[0].startswith("REFERENCE TEXT:\n[Dragon Ball]")


def test_every_title_missing_rereads_the_catalog_next_time() -> None:
    # The weekly updater can replace the snapshot a long session cached.
    calls: list = []
    checker = _checker(_transport({"contradiction": True}, calls=calls, page_status=404))
    assert checker.contradiction(_GHOST) is None
    assert checker.contradiction(_GHOST) is None
    assert calls.count("/catalog/v2/entries") == 2
    assert checker._kiwix_down_until == 0.0


def test_a_title_with_a_slash_keeps_its_whole_name() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            answer = {"titles": ["AC/DC"]} if "Name the" in msgs[0]["content"] else {}
            if "Name the" not in msgs[0]["content"]:
                prompts.append(msgs[1]["content"])
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]})
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(200, text=_KIWIX_PAGE)

    _checker(httpx.MockTransport(handle)).contradiction(_GHOST)
    assert prompts and prompts[0].startswith("REFERENCE TEXT:\n[AC/DC]")


@pytest.mark.parametrize(
    "titles",
    [["../../catalog/v2/entries"], ["Cubone/../../../search"], [" .. /x", "./Cubone"]],
)
def test_a_dot_segment_title_is_never_read(titles: list[str]) -> None:
    # httpx resolves "." and ".." in a path, so such a title would read another Kiwix route.
    calls: list = []
    checker = _checker(_transport({"contradiction": True, "reference": "x"}, calls=calls))
    checker._ask = lambda *a: {"titles": titles}
    assert checker.contradiction(_GHOST) is None
    assert [c for c in calls if not c.endswith("/chat/completions")] == []
    assert checker._kiwix_down_until == 0.0  # a bad title is not a failing Kiwix


def test_dots_inside_a_title_segment_still_read() -> None:
    calls: list = []
    checker = _checker(_transport({}, calls=calls))
    checker._ask = lambda *a: {"titles": ["...And Justice for All (album)"]}
    checker.contradiction(_GHOST)
    assert "/content/b/...And_Justice_for_All_(album)" in calls


def test_an_empty_title_is_never_read() -> None:
    # "/content/<book>/" redirects to the book's landing page, which is no reference.
    calls: list = []
    checker = _checker(_transport({}, calls=calls))
    checker._ask = lambda *a: {"titles": ["", "  ", "___"]}
    assert checker.contradiction(_GHOST) is None
    assert [c for c in calls if not c.endswith("/chat/completions")] == []


def test_a_bare_string_of_titles_is_not_read_letter_by_letter() -> None:
    calls: list = []
    checker = _checker(_transport({}, calls=calls))
    checker._ask = lambda *a: {"titles": "Cubone"}
    assert checker.contradiction(_GHOST) is None
    assert [c for c in calls if not c.endswith("/chat/completions")] == []


def test_an_unencodable_title_skips_only_itself() -> None:
    import json as _j

    import httpx

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            system = _j.loads(request.content)["messages"][0]["content"]
            answer = (
                {"titles": ["\ud800", "Cubone"]}
                if "Name the" in system
                else {"contradiction": True, "reference": "Cubone is a Ground type Pokémon"}
            )
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(answer)}}]})
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(200, text=_KIWIX_PAGE)

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) == "Cubone is a Ground type Pokémon"
    assert checker._kiwix_down_until == 0.0


def test_a_redirect_off_kiwix_is_not_a_reference() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" not in msgs[0]["content"]:
                prompts.append(msgs[1]["content"])
            body = {"titles": ["Cubone"]} if "Name the" in msgs[0]["content"] else {}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(body)}}]})
        if request.url.host == "evil":
            return httpx.Response(200, text=_KIWIX_PAGE)
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(302, headers={"Location": "http://evil/Cubone"})

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) is None
    assert prompts == []  # nothing was checked against the other host's page
    assert checker._kiwix_down_until == 0.0


def test_junk_titles_never_use_up_the_three_reads() -> None:
    calls: list = []
    checker = _checker(_transport({}, calls=calls))
    checker._ask = lambda *a: {"titles": ["", " ", "\ud800", "../x", "Cubone"]}
    checker.contradiction(_GHOST)
    assert "/content/b/Cubone" in calls


def test_a_redirect_to_another_port_on_kiwix_host_is_not_a_reference() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" not in msgs[0]["content"]:
                prompts.append(msgs[1]["content"])
            body = {"titles": ["Cubone"]} if "Name the" in msgs[0]["content"] else {}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(body)}}]})
        if request.url.port == 9999:
            return httpx.Response(200, text=_KIWIX_PAGE)
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(302, headers={"Location": "http://kiwix:9999/Cubone"})

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) is None
    assert prompts == []


def test_spellings_of_one_article_take_one_read() -> None:
    calls: list = []
    checker = _checker(_transport({}, calls=calls))
    checker._ask = lambda *a: {"titles": [" Cubone", "Cubone_", "Cubone", "Pikachu"]}
    checker.contradiction(_GHOST)
    assert calls.count("/content/b/Cubone") == 1
    assert "/content/b/Pikachu" in calls


def test_a_redirect_to_another_scheme_is_not_a_reference() -> None:
    import json as _j

    import httpx

    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            msgs = _j.loads(request.content)["messages"]
            if "Name the" not in msgs[0]["content"]:
                prompts.append(msgs[1]["content"])
            body = {"titles": ["Cubone"]} if "Name the" in msgs[0]["content"] else {}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(body)}}]})
        if request.url.scheme == "https":
            return httpx.Response(200, text=_KIWIX_PAGE)
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        return httpx.Response(302, headers={"Location": "https://kiwix/Cubone"})

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) is None
    assert prompts == []


def test_the_book_root_survives_a_concurrent_reset() -> None:
    # Another check clears the cached root (every title 404'd) between this one's test and use.
    class Racing(FactChecker):
        reads = 0

        @property
        def _content(self):
            Racing.reads += 1
            return "/content/b" if Racing.reads == 1 else None

        @_content.setter
        def _content(self, value) -> None:
            pass

    checker = Racing(endpoint="http://llm/v1", model="m", kiwix_endpoint="http://kiwix", kiwix_book="b")
    assert checker._book_path(None, time.monotonic() + 5) == "/content/b"


def test_the_articles_are_read_in_parallel() -> None:
    import json as _j
    import threading

    import httpx

    # Each read waits until all three are in flight; one after another, the barrier breaks.
    barrier = threading.Barrier(3, timeout=2.0)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = {"contradiction": True, "reference": "Cubone is a Ground type Pokémon"}
            return httpx.Response(200, json={"choices": [{"message": {"content": _j.dumps(body)}}]})
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=_KIWIX_CATALOG)
        barrier.wait()
        return httpx.Response(200, text=_KIWIX_PAGE)

    checker = _checker(httpx.MockTransport(handle))
    real_ask = checker._ask
    checker._ask = lambda c, system, user, d: (
        {"titles": ["A", "B", "C"]} if "Name the" in system else real_ask(c, system, user, d)
    )
    assert checker.contradiction(_GHOST) == "Cubone is a Ground type Pokémon"
    assert not barrier.broken


def test_a_catalog_without_the_book_switches_the_check_off() -> None:
    import httpx

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = {"choices": [{"message": {"content": '{"titles": ["Cubone"]}'}}]}
            return httpx.Response(200, json=body)
        return httpx.Response(200, text="<feed></feed>")

    checker = _checker(httpx.MockTransport(handle))
    assert checker.contradiction(_GHOST) is None
    assert checker._kiwix_down_until > 0.0


def test_the_factory_budget_stops_inside_the_sessions_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "cue_kiwix_endpoint", "http://kiwix")
    monkeypatch.setattr(settings, "cue_fact_check", True)
    monkeypatch.setattr(settings, "cue_fact_check_timeout_s", 6.0)
    checker = make_fact_checker()
    assert checker is not None and 0 < checker._budget_s < 6.0


def test_a_check_out_of_time_never_calls_the_model_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    clock = iter([0.0, 0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(factcheck.time, "monotonic", lambda: next(clock, 100.0))
    checker = _checker(_transport({"contradiction": True}, calls=calls), budget_s=5.0)
    assert checker.contradiction(_GHOST) is None
    assert calls.count("/v1/chat/completions") == 1  # the title proposal only


# ---- factory ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend, kiwix, enabled, built",
    [
        ("openai", "http://kiwix", True, True),
        ("openai", "", True, False),  # no mirror to check against
        ("openai", "http://kiwix", False, False),  # switched off
        ("stub", "http://kiwix", True, False),
    ],
)
def test_make_fact_checker_gating(
    monkeypatch: pytest.MonkeyPatch, backend: str, kiwix: str, enabled: bool, built: bool
) -> None:
    monkeypatch.setattr(settings, "cue_backend", backend)
    monkeypatch.setattr(settings, "cue_kiwix_endpoint", kiwix)
    monkeypatch.setattr(settings, "cue_fact_check", enabled)
    assert (make_fact_checker() is not None) is built


# ---- session ------------------------------------------------------------------------


def _final(text: str, *, end_ms: int = 2000) -> CaptionFinal:
    return CaptionFinal(
        type="caption.final", segmentId="s1", text=text, startMs=0, endMs=end_ms, lang="en"
    )


async def _session(sent: list[ServerMessage], checker) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
    session._cue_fact_checker = checker
    return session


async def _drain(session: Session) -> None:
    while session._cue_tasks:
        await asyncio.gather(*list(session._cue_tasks))


class _Checker:
    def __init__(self, verdict=None, *, raises: bool = False, sleep: float = 0.0) -> None:
        self.verdict, self.raises, self.sleep, self.seen = verdict, raises, sleep, []

    def contradiction(self, cue: GeneratedCue) -> str | None:
        self.seen.append(cue)
        if self.sleep:
            time.sleep(self.sleep)
        if self.raises:
            raise RuntimeError("kiwix down")
        return self.verdict


def _cues(sent: list[ServerMessage]) -> list[Cue]:
    return [m for m in sent if isinstance(m, Cue)]


def test_a_contradicted_cue_is_dropped_and_never_offered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        checker = _Checker("Type Ground")
        session = await _session(sent, checker)
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        assert _cues(sent) == [] and len(checker.seen) == 1
        # The same proposal again is a dedupe drop: it is not re-checked or shown.
        checker.verdict = None
        session._last_cue_monotonic = None
        session._consider_cue(_final("how far is the sun?", end_ms=4000))
        await _drain(session)
        await session.close()
        assert _cues(sent) == [] and len(checker.seen) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "checker",
    [_Checker(None), _Checker(raises=True), _Checker("Type Ground", sleep=0.5)],
    ids=["not-contradicted", "error", "timeout"],
)
def test_the_check_fails_open(monkeypatch: pytest.MonkeyPatch, checker: _Checker) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")
    monkeypatch.setattr(settings, "cue_fact_check_timeout_s", 0.1)

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _session(sent, checker)
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        await session.close()
        assert len(_cues(sent)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("flag", ["_translation_active", "_music_active"])
def test_a_cue_checked_while_a_translation_or_song_started_is_dropped(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _session(sent, None)

        class _Starting(_Checker):
            def contradiction(self, cue: GeneratedCue) -> str | None:
                setattr(session, flag, True)
                return None

        session._cue_fact_checker = _Starting()
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        assert _cues(sent) == []
        setattr(session, flag, False)
        await session.close()

    asyncio.run(run())


def test_a_cue_checked_while_the_recording_ended_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _session(sent, None)

        class _Finishing(_Checker):
            def contradiction(self, cue: GeneratedCue) -> str | None:
                session._finished = True
                return None

        session._cue_fact_checker = _Finishing()
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        assert _cues(sent) == []
        await session.close()

    asyncio.run(run())
