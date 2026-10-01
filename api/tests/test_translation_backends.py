"""Unit tests for the translator seam (XERK-160): factory selection, the stub,
and the OpenAI backend's pure request-building/parsing (no network)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.translate import make_translator
from api.translate.completion import CompletionTranslator
from api.translate.openai import OpenAITranslator
from api.translate.stub import StubTranslator


# ---- factory -------------------------------------------------------------------


def test_factory_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "off")
    assert make_translator() is None


def test_factory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")
    assert isinstance(make_translator(), StubTranslator)


def test_factory_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "openai")
    assert isinstance(make_translator(), OpenAITranslator)


def test_factory_uses_the_translation_model_not_the_cue_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-180: translation has its own gateway alias (reasoning_effort low on
    the LiteLLM side), independent of the cue route's llm_model."""
    monkeypatch.setattr(settings, "translation_backend", "openai")
    monkeypatch.setattr(settings, "translation_model", "translate-alias")
    monkeypatch.setattr(settings, "llm_model", "cue-alias")
    translator = make_translator()
    assert isinstance(translator, OpenAITranslator)
    assert translator._build_payload("hola", "es")["model"] == "translate-alias"


def test_translation_model_defaults_to_the_dedicated_alias() -> None:
    """The default must match the qwen3.8-27b-dflash-translate route in
    litellm/config.yaml — a drifted alias 404s at the gateway and translations
    silently fail closed."""
    assert type(settings).model_fields["translation_model"].default == "qwen3.8-27b-dflash-translate"


def test_gateway_config_carries_the_translation_alias() -> None:
    """Pin the api default to the shipped gateway route (both sides of the
    XERK-180 alias split live in this repo, so drift is testable)."""
    from pathlib import Path

    gateway = Path(__file__).parents[2] / "litellm" / "config.yaml"
    if not gateway.is_file():  # pragma: no cover - api tested outside the monorepo
        pytest.skip("litellm/config.yaml not present")
    text = gateway.read_text()
    assert "model_name: qwen3.8-27b-dflash-translate" in text
    assert "chat_template_kwargs" in text


def test_factory_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "nope")
    with pytest.raises(ValueError):
        make_translator()


def test_factory_wires_the_translation_thinking_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Translations stay thinking-off even though the cue side flipped to
    # thinking-on in the Aug 2026 retune — per-utterance latency on the
    # caption path matters more there. The flag is separate from the cue's.
    monkeypatch.setattr(settings, "translation_backend", "openai")
    monkeypatch.setattr(settings, "translation_disable_thinking", True)
    monkeypatch.setattr(settings, "cue_disable_thinking", False)  # must NOT leak
    translator = make_translator()
    assert translator._build_payload("hola")["chat_template_kwargs"] == {"enable_thinking": False}


# ---- stub ----------------------------------------------------------------------


def test_stub_wraps_text_with_lang() -> None:
    assert StubTranslator().translate("hola", source_lang="es") == "[es→en] hola"


def test_stub_without_lang() -> None:
    assert StubTranslator().translate("bonjour") == "[auto→en] bonjour"


def test_stub_empty_returns_none() -> None:
    assert StubTranslator().translate("   ") is None


# ---- OpenAI backend: payload ---------------------------------------------------


def _translator(**kwargs) -> OpenAITranslator:
    defaults = dict(endpoint="http://gw:4000/v1", model="qwen3.8-27b-dflash", api_key="k")
    defaults.update(kwargs)
    return OpenAITranslator(**defaults)


def test_payload_shape() -> None:
    payload = _translator()._build_payload("hola, ¿qué tal?", "es")
    assert payload["model"] == "qwen3.8-27b-dflash"
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    system, user = payload["messages"]
    assert system["role"] == "system"
    assert user == {"role": "user", "content": "hola, ¿qué tal?"}


def test_payload_names_the_source_language() -> None:
    payload = _translator()._build_payload("hola", "es")
    assert "spoken in Spanish" in payload["messages"][0]["content"]


def test_payload_without_detected_language_omits_the_clause() -> None:
    payload = _translator()._build_payload("hola", None)
    assert "spoken in" not in payload["messages"][0]["content"]


def test_payload_disables_thinking_by_default() -> None:
    payload = _translator()._build_payload("hola", "es")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_payload_thinking_left_on_when_disabled_off() -> None:
    payload = _translator(disable_thinking=False)._build_payload("hola", "es")
    assert "chat_template_kwargs" not in payload


# ---- OpenAI backend: parsing ---------------------------------------------------


def test_parse_plain_json() -> None:
    assert OpenAITranslator._parse('{"translation": "Hello, how are you?"}') == (
        "Hello, how are you?"
    )


def test_parse_json_wrapped_in_prose() -> None:
    content = 'Sure! Here it is: {"translation": "Good morning"} — done.'
    assert OpenAITranslator._parse(content) == "Good morning"


def test_parse_rejects_garbage() -> None:
    assert OpenAITranslator._parse("no json here") is None
    assert OpenAITranslator._parse("{broken") is None
    assert OpenAITranslator._parse('{"translation": ""}') is None
    assert OpenAITranslator._parse('{"other": "x"}') is None


def test_message_content_falls_back_to_reasoning_content() -> None:
    assert OpenAITranslator._message_content({"content": "a"}) == "a"
    assert OpenAITranslator._message_content({"content": "", "reasoning_content": "b"}) == "b"
    assert OpenAITranslator._message_content({"content": None, "reasoning_content": "b"}) == "b"
    assert OpenAITranslator._message_content({}) == ""


# ---- completion-prompt backend (XERK-1354) -----------------------------------


def test_factory_milmmt_style_selects_the_completion_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "translation_backend", "openai")
    monkeypatch.setattr(settings, "translation_prompt_style", "milmmt")
    monkeypatch.setattr(settings, "translation_model", "milmmt-46-4b-translate")
    translator = make_translator()
    assert isinstance(translator, CompletionTranslator)
    assert translator._url.endswith("/completions")
    assert translator._build_payload("hola", "es")["model"] == "milmmt-46-4b-translate"


def test_factory_rejects_an_unknown_prompt_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "openai")
    monkeypatch.setattr(settings, "translation_prompt_style", "chatml")
    with pytest.raises(ValueError, match="prompt style"):
        make_translator()


def test_prompt_style_defaults_to_chat_json() -> None:
    assert type(settings).model_fields["translation_prompt_style"].default == "chat-json"


def _ct() -> CompletionTranslator:
    return CompletionTranslator(endpoint="http://gw/v1/", model="m")


def test_completion_payload_is_the_documented_milmmt_prompt() -> None:
    body = _ct()._build_payload("Mañana vamos a la playa.", "es")
    assert body == {
        "model": "m",
        "prompt": "Translate this from Spanish to English:\nSpanish: Mañana vamos a la playa.\nEnglish:",
        "temperature": 0.0,
        "max_tokens": 512,
        "stop": ["\n"],
    }
    assert _ct()._url == "http://gw/v1/completions"


def test_completion_payload_flattens_newlines() -> None:
    # A newline inside the turn would end the prompt's source line early.
    body = _ct()._build_payload("hola\n  qué tal", "es")
    assert "Spanish: hola qué tal\nEnglish:" in body["prompt"]


def test_inherited_turn_is_translated_from_the_run_language() -> None:
    body = _ct()._build_payload("Mercurio, Venus, Tierra.", None, run_lang="pt")
    assert body["prompt"].startswith("Translate this from Portuguese to English:")


def test_own_language_wins_over_the_run_language() -> None:
    body = _ct()._build_payload("bonjour à tous", "fr", run_lang="es")
    assert body["prompt"].startswith("Translate this from French to English:")


def test_inherited_turn_leaning_english_is_not_sent() -> None:
    # Told English is Spanish, an MT model paraphrases it instead of returning it.
    assert _ct()._build_payload("I think that is the one", None, run_lang="es") is None


def test_tagged_turn_is_sent_even_if_it_looks_english() -> None:
    # The English guard is only for inherited turns; a tagged turn's label stands.
    assert _ct()._build_payload("the menu del día", "es") is not None


@pytest.mark.parametrize(
    ("source", "run"),
    [(None, None), ("en", None), (None, "en"), ("xx", None)],
)
def test_no_nameable_source_language_means_no_call(source, run) -> None:
    assert _ct()._build_payload("hola", source, run_lang=run) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"choices": [{"text": " Tomorrow we go to the beach. "}]}, "Tomorrow we go to the beach."),
        ({"choices": [{"text": "   "}]}, None),
        ({"choices": []}, None),
        ({}, None),
        ({"choices": [None]}, None),
    ],
)
def test_completion_parse(body, expected) -> None:
    assert CompletionTranslator._parse(body) == expected
