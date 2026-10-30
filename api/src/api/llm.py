"""Request-body adjustments for Claude models behind the LiteLLM gateway.

Tenir's chat payloads are written for the self-hosted Qwen route: greedy decoding
(``temperature: 0``) and Qwen's thinking switch (``chat_template_kwargs``). Claude models
reached through LiteLLM (an alias such as ``anthropic/claude-sonnet-5-5``) reject the
first with a 400 (only the default temperature is allowed) and have no use for the
second, so every chat call site passes its payload through ``adapt`` before sending it.

Where a call turns Qwen's thinking off, Claude's is turned off too (``adapt``). Cues keep
Claude's thinking at the model default: in the 2026-10-29 replay Sonnet 5.5 at
its default (it rarely thinks on a cue window) gave the best cues, and lowering the
effort made it decline nearly every window (scripts/cue_eval/RESULTS-2026-10.md).

A model is treated as Claude when its alias contains "claude"; name the LiteLLM alias
accordingly.
"""

from __future__ import annotations

# Fields a Claude model rejects (temperature) or ignores (Qwen's template switch).
_QWEN_ONLY = ("temperature", "chat_template_kwargs")


def is_claude(model: str) -> bool:
    return "claude" in model.lower()


def adapt(payload: dict) -> dict:
    """Rewrite a Qwen-shaped payload for a Claude model, in place; others pass unchanged.

    Qwen's thinking-off switch becomes Claude's. Without it Haiku 5.5 thinks by default and
    spent the fact check's whole 150-token budget reasoning (QA, 2026-10-29: finish_reason
    length, no answer, every wrong cue shown). Thinking on maps to the model default.
    """
    if not is_claude(payload.get("model", "")):
        return payload
    kwargs = payload.get("chat_template_kwargs") or {}
    for key in _QWEN_ONLY:
        payload.pop(key, None)
    if kwargs.get("enable_thinking") is False:
        payload["thinking"] = {"type": _thinking_off(payload["model"])}
    return payload


def _thinking_off(model: str) -> str:
    """Claude's thinking-off type through LiteLLM, measured 2026-10-29. Sonnet 5.5 ignores
    "disabled" (it still reasoned on 3 of 5 calls) and honours "between_tools"; Haiku 5.5
    honours "disabled" and rejects "between_tools" with a 400."""
    return "between_tools" if "sonnet" in model.lower() else "disabled"


def system_content(model: str, fixed: str, varying: str = "") -> str | list[dict]:
    """The system message content: ``fixed`` then ``varying``.

    For Claude the fixed part is its own block marked for prompt caching. Input tokens are
    over 90% of a Claude cue call's cost and the fixed cue instructions are about half of
    them, so a cached prefix (billed at a tenth of the input price, warm while a
    conversation runs) roughly halves the bill. Other models get the plain concatenation,
    byte-identical to the uncached prompt.
    """
    if not is_claude(model):
        return fixed + varying
    blocks: list[dict] = [{"type": "text", "text": fixed, "cache_control": {"type": "ephemeral"}}]
    if varying:
        blocks.append({"type": "text", "text": varying})
    return blocks
