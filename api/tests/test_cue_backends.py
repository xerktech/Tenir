"""Cue generation backends: the model-free stub, the level tuning, the factory,
and the OpenAI response parser (XERK-81)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.contract import CueLevel
from api.cue import make_cue_generator, min_interval_ms
from api.cue.levels import level_guidance
from api.cue.openai import OpenAICueGenerator
from api.cue.stub import StubCueGenerator, _title_from

# ---- stub generator --------------------------------------------------------


def test_stub_balanced_triggers_on_question_or_number() -> None:
    stub = StubCueGenerator()
    assert stub.generate("how far is the sun?", level=CueLevel.balanced) is not None
    assert stub.generate("it is 150 million km", level=CueLevel.balanced) is not None
    # No question, no number, just a statement -> nothing.
    assert stub.generate("nice weather today", level=CueLevel.balanced) is None


def test_stub_conservative_needs_question_and_number() -> None:
    stub = StubCueGenerator()
    assert stub.generate("how far is the sun?", level=CueLevel.conservative) is None
    assert stub.generate("is it 133?", level=CueLevel.conservative) is not None


def test_stub_aggressive_triggers_on_any_two_words() -> None:
    stub = StubCueGenerator()
    assert stub.generate("the weather", level=CueLevel.aggressive) is not None
    # A single word is below the aggressive bar.
    assert stub.generate("hello", level=CueLevel.aggressive) is None


def test_stub_uses_last_line_and_empty_transcript() -> None:
    stub = StubCueGenerator()
    assert stub.generate("", level=CueLevel.balanced) is None
    assert stub.generate("   \n  ", level=CueLevel.balanced) is None
    cue = stub.generate("small talk\nfavorite pokemon is 133?", level=CueLevel.balanced)
    assert cue is not None
    assert "133" in cue.body


def test_stub_title_is_one_to_three_significant_words() -> None:
    # Stopwords ("how", "is", "the", "far") dropped; number kept.
    assert _title_from("how far is the sun?") == "Sun"
    title = _title_from("favorite pokemon is number 133")
    assert 1 <= len(title.split()) <= 3
    # Punctuation-only line falls back to a default rather than an empty title.
    assert _title_from("!!!") == "Context"


# ---- level tuning ----------------------------------------------------------


def test_min_interval_orders_by_aggressiveness() -> None:
    assert (
        min_interval_ms(CueLevel.aggressive)
        < min_interval_ms(CueLevel.balanced)
        < min_interval_ms(CueLevel.conservative)
    )


def test_level_guidance_present_for_every_level() -> None:
    for level in CueLevel:
        assert level_guidance(level).strip()


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
