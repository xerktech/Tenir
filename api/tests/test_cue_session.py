"""Session-level cue behaviour: generation off the caption path, delivery,
persistence, rate-limiting and dedupe — all against the model-free stub (XERK-81)."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import CaptionFinal, Cue, CueLevel, MicSource, ServerMessage
from api.persistence import get_conversation_store
from api.session import Session


def _final(text: str, *, segment_id: str = "s1", end_ms: int = 2000) -> CaptionFinal:
    return CaptionFinal(
        type="caption.final",
        segmentId=segment_id,
        text=text,
        startMs=0,
        endMs=end_ms,
        lang="en",
    )


async def _fresh_session(
    sent: list[ServerMessage], *, level: CueLevel = CueLevel.balanced
) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None, cue_level=level)
    return session


async def _drain_cues(session: Session) -> None:
    await asyncio.gather(*list(session._cue_tasks))


def _cues(sent: list[ServerMessage]) -> list[Cue]:
    return [m for m in sent if isinstance(m, Cue)]


def test_session_emits_and_persists_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_cue(_final("how far is the sun?", end_ms=2000))
        await _drain_cues(session)
        await session.close()

        cues = _cues(sent)
        assert len(cues) == 1
        assert cues[0].title and cues[0].body
        assert cues[0].atMs == 2000

        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None
        assert len(conv.cues) == 1
        assert conv.cues[0].cue_id == cues[0].cueId
        assert conv.cues[0].at_ms == 2000

    asyncio.run(run())


def test_no_cue_when_backend_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "off")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._cue_generator is None
        session._consider_cue(_final("how far is the sun?"))
        await _drain_cues(session)
        await session.close()
        assert _cues(sent) == []

    asyncio.run(run())


def test_rate_limit_suppresses_second_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent, level=CueLevel.balanced)
        session._consider_cue(_final("is it 133?", segment_id="a"))
        await _drain_cues(session)
        # A second cue-worthy final immediately after is inside the balanced window.
        session._consider_cue(_final("how about 42?", segment_id="b"))
        await _drain_cues(session)
        await session.close()
        assert len(_cues(sent)) == 1

    asyncio.run(run())


def test_dedupes_repeated_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_cue(_final("pokemon number 133?", segment_id="a"))
        await _drain_cues(session)
        # Move the clock back past the rate-limit window so only dedupe can block it.
        session._last_cue_monotonic = time.monotonic() - 3600
        session._consider_cue(_final("pokemon number 133?", segment_id="b"))
        await _drain_cues(session)
        await session.close()
        assert len(_cues(sent)) == 1

    asyncio.run(run())


def test_cue_persisted_even_when_delivery_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        delivered: list[ServerMessage] = []

        async def sender(m: ServerMessage) -> None:
            if isinstance(m, Cue):
                raise RuntimeError("socket gone")
            delivered.append(m)

        session = Session(sender, household="default")
        await session.start(
            mic_source=MicSource("phone-microphone"),
            source_lang=None,
            cue_level=CueLevel.balanced,
        )
        session._consider_cue(_final("how far is the sun? 150", segment_id="a"))
        await _drain_cues(session)
        await session.close()

        # The socket was gone, but the cue is still recorded (like captions).
        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None and len(conv.cues) == 1

    asyncio.run(run())
