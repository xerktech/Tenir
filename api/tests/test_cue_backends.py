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


# ---- response content extraction (regression: reasoning model empty content) --


def test_message_content_prefers_content() -> None:
    assert OpenAICueGenerator._message_content({"content": "hello"}) == "hello"


def test_message_content_falls_back_to_reasoning_content() -> None:
    # A reasoning model/gateway may leave content empty and put the answer in
    # reasoning_content; None content must not crash and must fall back.
    msg = {"content": None, "reasoning_content": '{"cue": true, "title": "T", "body": "B"}'}
    assert OpenAICueGenerator._message_content(msg) == '{"cue": true, "title": "T", "body": "B"}'
    assert OpenAICueGenerator._message_content({}) == ""
