"""Cue generation backends: the model-free stub, the fixed tuning, the factory,
and the OpenAI response parser (XERK-81, XERK-114)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.cue import cue_guidance, make_cue_generator, min_interval_ms, normalize_cue_title
from api.cue.openai import OpenAICueGenerator
from api.cue.stub import StubCueGenerator, _title_from

# ---- stub generator --------------------------------------------------------


def test_stub_triggers_on_any_non_empty_turn() -> None:
    # XERK-114: with the aggressiveness toggle gone, the stub fires on essentially
    # every turn — a statement, a single word, a bare number all produce a cue.
    stub = StubCueGenerator()
    assert stub.generate("how far is the sun?") is not None
    assert stub.generate("it is 150 million km") is not None
    assert stub.generate("nice weather today") is not None
    assert stub.generate("hello") is not None
    assert stub.generate("133") is not None


def test_stub_uses_last_line_and_empty_transcript() -> None:
    stub = StubCueGenerator()
    # Nothing to draw from -> no cue.
    assert stub.generate("") is None
    assert stub.generate("   \n  ") is None
    cue = stub.generate("small talk\nfavorite pokemon is 133?")
    assert cue is not None
    assert "133" in cue.body


def test_stub_skips_a_cue_it_was_told_to_avoid() -> None:
    # A cue already surfaced this conversation must not be proposed again (XERK-102);
    # the avoid list is matched on the normalized title, so casing/punctuation vary.
    stub = StubCueGenerator()
    first = stub.generate("how far is the sun?")
    assert first is not None and first.title == "Sun"
    assert stub.generate("how far is the sun?", avoid_titles=["Sun"]) is None
    assert stub.generate("how far is the sun?", avoid_titles=[" sun. "]) is None
    # An unrelated avoid entry doesn't block a genuinely different cue.
    other = stub.generate("how far is the sun?", avoid_titles=["Moon"])
    assert other is not None and other.title == "Sun"


def test_normalize_cue_title_collapses_trivial_variants() -> None:
    assert normalize_cue_title("Sun") == "sun"
    assert normalize_cue_title(" SUN ! ") == "sun"
    assert normalize_cue_title("Pikachu #25") == normalize_cue_title("pikachu 25")
    assert normalize_cue_title("!!!") == ""


def test_stub_title_is_one_to_three_significant_words() -> None:
    # Stopwords ("how", "is", "the", "far") dropped; number kept.
    assert _title_from("how far is the sun?") == "Sun"
    title = _title_from("favorite pokemon is number 133")
    assert 1 <= len(title.split()) <= 3
    # Punctuation-only line falls back to a default rather than an empty title.
    assert _title_from("!!!") == "Context"


# ---- fixed tuning (XERK-114: single aggressive setting, no per-level toggle) --


def test_min_interval_is_more_aggressive_than_the_old_aggressive_level() -> None:
    # The old "aggressive" level spaced cues 3000ms apart; the fixed setting is
    # tighter still, so cues come at least as thick as the old top level.
    assert 0 < min_interval_ms() < 3000


def test_cue_guidance_is_present() -> None:
    assert cue_guidance().strip()


def test_cue_guidance_is_accuracy_first() -> None:
    # XERK-118: the cue's job is accurate, fact-checked info. The guidance must steer
    # the model to correct errors and to stay silent when it isn't sure, rather than
    # padding with tangential context (the reverse of the old "when in doubt, emit").
    guidance = cue_guidance().lower()
    assert "accura" in guidance  # accuracy / accurate
    assert "correct" in guidance  # correcting a wrong statement
    assert "silent" in guidance or "silence" in guidance  # stay silent when unsure


def test_grounded_guidance_is_generous_but_evidence_gated() -> None:
    # XERK-120: with evidence in the prompt the bar loosens ONE-SIDEDLY — emit
    # freely for evidence-covered facts, but anything uncovered keeps the tight
    # memory bar (silence over a guess), and time-sensitive facts must come only
    # from evidence. A symmetric loosening measurably made the model miscite.
    grounded = cue_guidance(grounded=True).lower()
    assert "prefer emitting" in grounded  # generous where evidence covers
    assert "only from the evidence" in grounded  # time-sensitive facts gated
    assert "stay silent" in grounded  # uncovered topics keep the tight bar
    assert "accura" in grounded  # accuracy stays absolute
    # The two bars are genuinely different settings.
    assert cue_guidance(grounded=True) != cue_guidance()


# ---- factory ---------------------------------------------------------------


@pytest.mark.parametrize(
    "backend,expected",
    [("off", type(None)), ("stub", StubCueGenerator), ("openai", OpenAICueGenerator)],
)
def test_factory_selects_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, expected: type
) -> None:
    monkeypatch.setattr(settings, "cue_backend", backend)
    gen = make_cue_generator()
    assert isinstance(gen, expected) or (expected is type(None) and gen is None)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "bogus")
    with pytest.raises(ValueError):
        make_cue_generator()


# ---- OpenAI response parsing (pure; the network call itself is not covered) --


def _gen() -> OpenAICueGenerator:
    return OpenAICueGenerator(endpoint="http://litellm:4000/v1", model="qwen3-llm")


def test_parse_valid_cue() -> None:
    cue = _gen()._parse('{"cue": true, "title": "Sun", "body": "About 150M km away."}')
    assert cue is not None
    assert cue.title == "Sun"
    assert cue.body == "About 150M km away."


def test_parse_no_cue_returns_none() -> None:
    assert _gen()._parse('{"cue": false}') is None


def test_parse_missing_fields_returns_none() -> None:
    assert _gen()._parse('{"cue": true, "title": "", "body": "x"}') is None
    assert _gen()._parse('{"cue": true, "title": "x"}') is None


def test_parse_extracts_json_wrapped_in_reasoning() -> None:
    # A reasoning model may prepend thinking text before the JSON object.
    raw = 'Let me think... The answer is:\n{"cue": true, "title": "Pikachu", "body": "#25."}\nDone.'
    cue = _gen()._parse(raw)
    assert cue is not None and cue.title == "Pikachu"


def test_parse_garbage_returns_none() -> None:
    assert _gen()._parse("not json at all") is None
    assert _gen()._parse("{broken json") is None


def test_parse_truncates_long_body() -> None:
    gen = OpenAICueGenerator(endpoint="e", model="m", max_body_chars=10)
    cue = gen._parse('{"cue": true, "title": "T", "body": "0123456789ABCDEF"}')
    assert cue is not None and cue.body == "0123456789"


# ---- request payload (regression: reasoning model must not think) -----------


def test_payload_disables_thinking_by_default() -> None:
    # Qwen3 left thinking spends the whole token budget on reasoning and returns an
    # empty content, so no cue is produced. The payload must switch thinking off.
    payload = _gen()._build_payload("how far is the sun?")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "qwen3-llm"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "how far is the sun?"


def test_payload_keeps_thinking_when_disabled_off() -> None:
    gen = OpenAICueGenerator(endpoint="e", model="m", disable_thinking=False)
    payload = gen._build_payload("hi")
    assert "chat_template_kwargs" not in payload


def test_payload_tells_model_to_avoid_already_surfaced_cues() -> None:
    # XERK-102: already-surfaced titles ride the system prompt as "don't repeat",
    # order-preserving and de-duplicated, so the model finds fresh context.
    payload = _gen()._build_payload(
        "how deep is the ocean?",
        ["Sun", "Moon", "Sun"],
    )
    system = payload["messages"][0]["content"]
    assert "do NOT repeat" in system
    assert "Sun, Moon" in system  # de-duped, order preserved
    assert system.count("Sun") == 1


def test_payload_omits_avoid_clause_when_nothing_surfaced_yet() -> None:
    system = _gen()._build_payload("hi")["messages"][0]["content"]
    assert "do NOT repeat" not in system


def test_payload_system_prompt_is_accuracy_and_correction_framed() -> None:
    # XERK-118: the system prompt frames the cue as a fact-checker — correct wrong
    # statements, surface only verified facts — and embeds the accuracy guidance, so
    # the model's whole instruction is about being right, not about volume.
    system = _gen()._build_payload("the sun is 15 thousand km away")["messages"][0]["content"].lower()
    assert "fact-check" in system or "fact check" in system
    assert "correct" in system  # correcting things said that are wrong
    assert "accura" in system  # the accuracy guidance rides the system prompt


# ---- response content extraction (regression: reasoning model empty content) --


def test_message_content_prefers_content() -> None:
    assert OpenAICueGenerator._message_content({"content": "hello"}) == "hello"


def test_message_content_falls_back_to_reasoning_content() -> None:
    # A reasoning model/gateway may leave content empty and put the answer in
    # reasoning_content; None content must not crash and must fall back.
    msg = {"content": None, "reasoning_content": '{"cue": true, "title": "T", "body": "B"}'}
    assert OpenAICueGenerator._message_content(msg) == '{"cue": true, "title": "T", "body": "B"}'
    assert OpenAICueGenerator._message_content({}) == ""


# ---- evidence grounding (XERK-120) ------------------------------------------


def _evidence() -> list:
    from api.cue.retrieval.base import Evidence

    return [
        Evidence(source="BBC News", title="PM sworn in", snippet="A new PM took office.",
                 published="2026-07-20"),
        Evidence(source="Wikipedia", title="Prime Minister", snippet="The head of government."),
    ]


def test_payload_embeds_numbered_dated_evidence() -> None:
    system = _gen()._build_payload("who is the PM?", (), _evidence())["messages"][0]["content"]
    assert "[1] (BBC News, 2026-07-20) PM sworn in: A new PM took office." in system
    assert "[2] (Wikipedia) Prime Minister: The head of government." in system
    # The staleness rule rides with the evidence: retrieved facts outrank memory.
    assert "out of date" in system
    assert "evidence wins" in system


def test_payload_omits_evidence_block_without_evidence() -> None:
    system = _gen()._build_payload("who is the PM?")["messages"][0]["content"]
    assert "EVIDENCE" not in system
    assert "out of date" not in system


def test_parse_maps_citation_to_source_label() -> None:
    cue = _gen()._parse(
        '{"cue": true, "title": "PM", "body": "B.", "evidence": [1, 2]}', _evidence()
    )
    assert cue is not None
    assert cue.source == "BBC News"  # first cited item's label


def test_parse_uncited_cue_has_no_source() -> None:
    cue = _gen()._parse('{"cue": true, "title": "PM", "body": "B."}', _evidence())
    assert cue is not None
    assert cue.source is None


def test_parse_ignores_malformed_or_out_of_range_citations() -> None:
    for cited in ('"evidence": [99]', '"evidence": "1"', '"evidence": [0, -1]',
                  '"evidence": ["one"]'):
        cue = _gen()._parse(
            '{"cue": true, "title": "PM", "body": "B.", %s}' % cited, _evidence()
        )
        assert cue is not None
        assert cue.source is None, cited


def test_stub_grounds_in_first_evidence_item() -> None:
    stub = StubCueGenerator()
    grounded = stub.generate("who is the PM?", evidence=_evidence())
    assert grounded is not None and grounded.source == "BBC News"
    ungrounded = stub.generate("who is the PM?")
    assert ungrounded is not None and ungrounded.source is None


def test_payload_guidance_is_grounded_only_when_evidence_arrived() -> None:
    # XERK-120: the generous bar may ride the prompt ONLY alongside actual
    # evidence — a retrieval outage (empty evidence) must fall back to the tight
    # memory bar, never combine "prefer emitting" with guessing.
    with_evidence = _gen()._build_payload("who is the PM?", (), _evidence())
    without = _gen()._build_payload("who is the PM?")
    assert "prefer emitting" in with_evidence["messages"][0]["content"]
    assert "prefer emitting" not in without["messages"][0]["content"]
    assert "prefer silence over a guess" in without["messages"][0]["content"]
