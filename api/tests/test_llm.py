"""Claude request-body adjustments (api.llm) at every chat call site."""

from __future__ import annotations

from api.cue import GeneratedCue
from api.cue.factcheck import FactChecker
from api.cue.openai import OpenAICueGenerator
from api.cue.retrieval.base import Evidence
from api.llm import adapt, is_claude, system_content
from api.translate.openai import OpenAITranslator

CLAUDE = "anthropic/claude-sonnet-5-5"
QWEN = "qwen3.8-27b-int4"


def _cue_payload(model: str, **kwargs) -> dict:
    gen = OpenAICueGenerator(endpoint="http://gw/v1", model=model)
    return gen._build_payload("A: which router did you get?", **kwargs)


def test_is_claude_matches_the_litellm_aliases() -> None:
    assert is_claude(CLAUDE)
    assert is_claude("anthropic/claude-haiku-5-5")
    assert is_claude("Claude-Sonnet")
    assert not is_claude(QWEN)
    assert not is_claude("")


def test_adapt_drops_the_fields_claude_rejects() -> None:
    # Measured 2026-10-29: LiteLLM returns 400 for temperature 0.0 on both Claude 5.5 models.
    payload = {"model": CLAUDE, "temperature": 0.0, "chat_template_kwargs": {}, "max_tokens": 9}
    assert adapt(payload) == {"model": CLAUDE, "max_tokens": 9}


def test_adapt_maps_thinking_off_to_claude_thinking_disabled() -> None:
    # Haiku 5.5 thinks by default; without this the fact check's 150 tokens all went to
    # reasoning and it never answered (QA 2026-10-29). LiteLLM accepts it for both models.
    # Per model, measured through LiteLLM: Sonnet 5.5 ignores "disabled", Haiku 400s on
    # "between_tools".
    off = {"model": CLAUDE, "chat_template_kwargs": {"enable_thinking": False}}
    assert adapt(off) == {"model": CLAUDE, "thinking": {"type": "between_tools"}}
    haiku = {"model": "anthropic/claude-haiku-5-5", "chat_template_kwargs": {"enable_thinking": False}}
    assert adapt(haiku)["thinking"] == {"type": "disabled"}
    on = {"model": CLAUDE, "chat_template_kwargs": {"enable_thinking": True}}
    assert adapt(on) == {"model": CLAUDE}


def test_adapt_leaves_other_models_untouched() -> None:
    payload = {"model": QWEN, "temperature": 0.0, "chat_template_kwargs": {"enable_thinking": True}}
    assert adapt(dict(payload)) == payload


def test_system_content_caches_only_the_fixed_part_for_claude() -> None:
    assert system_content(CLAUDE, "rules", "evidence") == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "evidence"},
    ]
    # No empty second block when nothing varies.
    assert system_content(CLAUDE, "rules") == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}},
    ]
    assert system_content(QWEN, "rules", "evidence") == "rulesevidence"


def test_cue_payload_for_claude_has_no_qwen_fields_and_a_cached_prefix() -> None:
    payload = _cue_payload(CLAUDE)
    assert "temperature" not in payload
    assert "chat_template_kwargs" not in payload
    assert "thinking" not in payload  # cues keep Claude's default thinking
    fixed = payload["messages"][0]["content"]
    assert len(fixed) == 1 and fixed[0]["cache_control"] == {"type": "ephemeral"}
    # Sonnet 5.5 caches prefixes of 512+ tokens; the cue rules are several thousand chars.
    assert len(fixed[0]["text"]) > 4000


def test_claude_cue_prompt_is_the_qwen_prompt_split_in_two() -> None:
    evidence = [Evidence(source="Wikipedia", title="Wi-Fi 7", snippet="IEEE 802.11be.")]
    avoid = [GeneratedCue("Wi-Fi 7", "Uses 320 MHz channels.")]
    qwen = _cue_payload(QWEN, avoid_cues=avoid, evidence=evidence)
    claude = _cue_payload(CLAUDE, avoid_cues=avoid, evidence=evidence)
    fixed, varying = claude["messages"][0]["content"]
    # The cached block holds no per-call text, so it is identical from call to call.
    assert "IEEE 802.11be" not in fixed["text"] and "320 MHz" not in fixed["text"]
    assert "IEEE 802.11be" in varying["text"] and "320 MHz" in varying["text"]
    assert fixed["text"] + varying["text"] == qwen["messages"][0]["content"]
    assert qwen["temperature"] == 0.0
    assert claude["messages"][1:] == qwen["messages"][1:]


def test_fact_check_payload_for_claude() -> None:
    checker = FactChecker(endpoint="http://gw/v1", model=CLAUDE, kiwix_endpoint="http://k", kiwix_book="b")
    payload = checker._payload("system", "user")
    assert "temperature" not in payload and "chat_template_kwargs" not in payload
    assert payload["thinking"] == {"type": "between_tools"}
    assert payload["max_tokens"] == 150


def test_translation_payload_for_claude() -> None:
    translator = OpenAITranslator(endpoint="http://gw/v1", model=CLAUDE, disable_thinking=True)
    payload = translator._build_payload("hola", "es")
    assert "temperature" not in payload and "chat_template_kwargs" not in payload
    assert payload["thinking"] == {"type": "between_tools"}
    assert payload["response_format"] == {"type": "json_object"}
