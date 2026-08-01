"""Unit tests for the translator seam (XERK-160): factory selection, the stub,
and the OpenAI backend's pure request-building/parsing (no network)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.translate import make_translator
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
    """The default must match the gpt-oss:120b-translate route in
    litellm/config.yaml — a drifted alias 404s at the gateway and translations
    silently fail closed."""
    assert type(settings).model_fields["translation_model"].default == "gpt-oss:120b-translate"


def test_gateway_config_carries_the_translation_alias() -> None:
    """Pin the api default to the shipped gateway route (both sides of the
    XERK-180 alias split live in this repo, so drift is testable)."""
    from pathlib import Path

    gateway = Path(__file__).parents[2] / "litellm" / "config.yaml"
    if not gateway.is_file():  # pragma: no cover - api tested outside the monorepo
        pytest.skip("litellm/config.yaml not present")
    text = gateway.read_text()
    assert "model_name: gpt-oss:120b-translate" in text
    assert "reasoning_effort: low" in text


def test_factory_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "nope")
    with pytest.raises(ValueError):
        make_translator()


# ---- stub ----------------------------------------------------------------------


def test_stub_wraps_text_with_lang() -> None:
    assert StubTranslator().translate("hola", source_lang="es") == "[es→en] hola"


def test_stub_without_lang() -> None:
    assert StubTranslator().translate("bonjour") == "[auto→en] bonjour"


def test_stub_empty_returns_none() -> None:
    assert StubTranslator().translate("   ") is None


# ---- OpenAI backend: payload ---------------------------------------------------


def _translator(**kwargs) -> OpenAITranslator:
    defaults = dict(endpoint="http://gw:4000/v1", model="gpt-oss:120b", api_key="k")
    defaults.update(kwargs)
    return OpenAITranslator(**defaults)


def test_payload_shape() -> None:
    payload = _translator()._build_payload("hola, ¿qué tal?", "es")
    assert payload["model"] == "gpt-oss:120b"
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
