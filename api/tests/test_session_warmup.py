"""Session warms the transcriber at start, off the caption path (XERK-128).

The session kicks the transcriber's `warmup()` off as a background task at start
rather than paying any per-session startup cost inline on the first audio frame,
so the first spoken words don't wait behind it. The current backends have no
cold-start cost (warmup is a no-op), but the lifecycle — start it, cancel it on
teardown, swallow its errors — is exercised here against a recording transcriber.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from api import session as session_mod
from api.contract import CaptionFinal, CaptionPartial, MicSource, ServerMessage
from api.session import Session


class RecordingTranscriber:
    """A transcriber that records the lifecycle calls the session makes on it."""

    def __init__(self) -> None:
        self.warmups = 0
        self.warmup_event = asyncio.Event()
        self.pushed: list[bytes] = []
        self.flushed = False
        self.closed = False

    async def warmup(self) -> None:
        self.warmups += 1
        self.warmup_event.set()

    async def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        # Block until close(): the session's pump awaits this, and the test only
        # cares about the warmup lifecycle, not caption output.
        while not self.closed:
            await asyncio.sleep(0.01)
        if False:  # pragma: no cover - makes this an async generator
            yield  # type: ignore[unreachable]

    async def flush(self) -> None:
        self.flushed = True

    async def close(self) -> None:
        self.closed = True


async def _start(sent: list[ServerMessage], fake: RecordingTranscriber) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
    return session


def test_start_warms_transcriber_off_the_ready_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        fake = RecordingTranscriber()
        monkeypatch.setattr(session_mod, "make_transcriber", lambda source_lang=None, **kw: fake)

        sent: list[ServerMessage] = []
        session = await _start(sent, fake)

        # ready is sent regardless of warmup; warmup runs in the background.
        assert any(getattr(m, "type", None) == "session.ready" for m in sent)
        await asyncio.wait_for(fake.warmup_event.wait(), timeout=1.0)
        assert fake.warmups == 1

        await session.close()
        assert fake.flushed and fake.closed

    asyncio.run(run())


def test_close_cancels_a_pending_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowWarmup(RecordingTranscriber):
        async def warmup(self) -> None:
            self.warmups += 1
            self.warmup_event.set()
            await asyncio.sleep(60)  # still warming when the session tears down

    async def run() -> None:
        fake = SlowWarmup()
        monkeypatch.setattr(session_mod, "make_transcriber", lambda source_lang=None, **kw: fake)

        sent: list[ServerMessage] = []
        session = await _start(sent, fake)
        await asyncio.wait_for(fake.warmup_event.wait(), timeout=1.0)

        # close() cancels the in-flight warmup; teardown completes without hanging on it.
        await asyncio.wait_for(session.close(), timeout=1.0)
        assert session._warmup is None
        assert fake.closed

    asyncio.run(run())


def test_warmup_error_is_retrieved_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingWarmup(RecordingTranscriber):
        async def warmup(self) -> None:
            self.warmups += 1
            self.warmup_event.set()
            raise RuntimeError("warmup blew up")

    async def run() -> None:
        fake = ExplodingWarmup()
        monkeypatch.setattr(session_mod, "make_transcriber", lambda source_lang=None, **kw: fake)

        sent: list[ServerMessage] = []
        session = await _start(sent, fake)
        await asyncio.wait_for(fake.warmup_event.wait(), timeout=1.0)
        # Let the failing task settle; its done-callback must swallow the exception so
        # it never surfaces as an unretrieved-task warning.
        await asyncio.sleep(0.01)
        assert session._warmup is not None and session._warmup.done()

        await session.close()

    asyncio.run(run())
