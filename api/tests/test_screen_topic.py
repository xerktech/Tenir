"""A screen meeting's topic title from its transcript (XERK-2506, api/screen/topic.py)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from api.config import settings
from api.persistence import InMemoryConversationStore, ScreenMeta, Segment
from api.screen import topic


def _conv(lines):
    store = InMemoryConversationStore()
    store.create("default", "c1", owner="u1", screen=ScreenMeta("acme", "Acme", "zoom"))
    for i, (speaker, text) in enumerate(lines):
        store.add_segment(
            "default", "c1", Segment(f"s{i}", text, i * 1000, i * 1000 + 900, speaker=speaker)
        )
    return store.get("default", "c1")


@pytest.mark.parametrize(
    ("raw", "title"),
    [
        ("Payment Ingest Retry Strategy", "Payment Ingest Retry Strategy"),
        ('"Payment Ingest Retry Strategy."\n', "Payment Ingest Retry Strategy"),
        ("Title: Sprint Board Review\nThe team reviewed...", "Sprint Board Review"),
        ("**DNS Errors In Search**", "DNS Errors In Search"),
        ("", None),
        ("   \n  ", None),
        ("x" * 200, None),
    ],
)
def test_the_answer_is_cleaned_into_a_title(raw: str, title: str | None) -> None:
    assert topic.clean(raw) == title


def test_the_transcript_names_speakers_and_is_capped() -> None:
    conv = _conv([("Ana", "retry the ingest"), (None, "  "), (None, "and the dns error")])
    assert topic.transcript(conv) == "Ana: retry the ingest\nand the dns error"
    long = _conv([("Ana", "w" * 1000)] * 50)
    assert len(topic.transcript(long)) <= topic.MAX_TRANSCRIPT_CHARS


def test_the_request_has_thinking_off_and_is_claude_safe() -> None:
    qwen = topic.payload("qwen3.8-27b-int4", "Ana: hi")
    assert qwen["chat_template_kwargs"] == {"enable_thinking": False}
    claude = topic.payload("anthropic/claude-haiku-5-5", "Ana: hi")
    assert "temperature" not in claude and "chat_template_kwargs" not in claude
    assert claude["thinking"] == {"type": "disabled"}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_title_comes_from_the_cue_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "litellm_endpoint", "http://gw/v1")
    monkeypatch.setattr(settings, "litellm_api_key", "k")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Retry Logic"}}]})

    conv = _conv([("Ana", "retry the ingest")])
    assert asyncio.run(topic.title_for(conv, _client(handler))) == "Retry Logic"
    (req,) = seen
    assert str(req.url) == "http://gw/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k"
    assert json.loads(req.content)["messages"][1]["content"] == "Ana: retry the ingest"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, content=b"not json"),
        httpx.ConnectError("down"),
    ],
)
def test_any_model_failure_gives_no_title(monkeypatch: pytest.MonkeyPatch, answer) -> None:
    monkeypatch.setattr(settings, "cue_backend", "openai")

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    conv = _conv([("Ana", "retry the ingest")])
    assert asyncio.run(topic.title_for(conv, _client(handler))) is None


def test_no_cue_model_or_no_text_means_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call expected")

    monkeypatch.setattr(settings, "cue_backend", "off")
    assert asyncio.run(topic.title_for(_conv([("Ana", "hi")]), _client(handler))) is None
    monkeypatch.setattr(settings, "cue_backend", "openai")
    assert asyncio.run(topic.title_for(_conv([]), _client(handler))) is None
