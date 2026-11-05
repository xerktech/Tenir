"""A finished screen meeting's title, from what was said (XERK-2506).

The window shows no usable title: in a remote-desktop window the top band is the client's
toolbar (Horizon's Ctrl+Alt+Del row was stored as a meeting's title), and Zoom's own title
is "Zoom Meeting". So the meeting is titled by its topic, asked of the cue chat model once,
right before it is delivered to Turma. No cue backend, or any failure: no title.
"""

from __future__ import annotations

import logging

import httpx

from api.config import settings
from api.llm import adapt, system_content
from api.persistence import Conversation
from api.screen.utterances import sentences

log = logging.getLogger("api.screen")

# Transcript sent to the model: the start of a meeting says what it is about, and a cap
# keeps a two-hour meeting's call small. Characters, ~3k tokens.
MAX_TRANSCRIPT_CHARS = 12_000
MAX_TITLE_CHARS = 80
TIMEOUT_SECONDS = 60.0

_SYSTEM = (
    "You title work meetings from their captions. Reply with the title only: 3 to 8 words "
    "naming what the meeting was about (the systems, tickets or decisions discussed), in "
    "Title Case, with no quotes, no trailing period and no date. The captions are "
    "machine-read and may contain errors; ignore small talk."
)


def transcript(conv: Conversation) -> str:
    lines: list[str] = []
    size = 0
    for seg in sentences(conv.segments):
        text = seg.text
        line = f"{seg.speaker}: {text}" if seg.speaker else text
        size += len(line) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


def payload(model: str, text: str) -> dict:
    return adapt(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_content(model, _SYSTEM)},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": 64,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )


def clean(raw: str) -> str | None:
    """The model's answer as a title, or None when it isn't one."""
    lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    title = lines[0].strip().strip("\"'`*#").strip()
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    title = title.rstrip(".").strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        return None
    return title


async def title_for(conv: Conversation, client: httpx.AsyncClient | None = None) -> str | None:
    """The meeting's topic title, or None (no cue model configured, no text, or a failure)."""
    if settings.cue_backend != "openai":
        return None
    text = transcript(conv)
    if not text:
        return None
    headers = {"Content-Type": "application/json"}
    if settings.litellm_api_key:
        headers["Authorization"] = f"Bearer {settings.litellm_api_key}"
    url = settings.litellm_endpoint.rstrip("/") + "/chat/completions"
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        resp = await client.post(url, json=payload(settings.llm_model, text), headers=headers)
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        return clean(message.get("content") or "")
    except Exception as exc:  # noqa: BLE001 — any odd answer (content as a list) is no title
        log.warning("meeting %s: no topic title (%r)", conv.id, exc)
        return None
    finally:
        if own:
            await client.aclose()
