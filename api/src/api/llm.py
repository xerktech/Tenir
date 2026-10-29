"""Request-body adjustments for Claude models behind the LiteLLM gateway.

Tenir's chat payloads are written for the self-hosted Qwen route: greedy decoding
(``temperature: 0``) and Qwen's thinking switch (``chat_template_kwargs``). Claude models
reached through LiteLLM (an alias such as ``anthropic/claude-sonnet-5-5``) reject the
first with a 400 (only the default temperature is allowed) and have no use for the
second, so every chat call site passes its payload through ``adapt`` before sending it.

Claude's thinking is left at the model default: in the 2026-10-29 replay Sonnet 5.5 at
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
    """Drop the Qwen-only fields when the payload targets a Claude model. In place."""
    if is_claude(payload.get("model", "")):
        for key in _QWEN_ONLY:
            payload.pop(key, None)
    return payload


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
