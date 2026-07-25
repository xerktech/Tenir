"""Session-level cue behaviour: generation off the caption path, delivery,
persistence, rate-limiting and dedupe — all against the model-free stub (XERK-81)."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import CaptionFinal, Cue, MicSource, ServerMessage
from api.cue import GeneratedCue
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


async def _fresh_session(sent: list[ServerMessage]) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
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
        session = await _fresh_session(sent)
        session._consider_cue(_final("is it 133?", segment_id="a"))
        await _drain_cues(session)
        # A second cue-worthy final immediately after is inside the rate-limit window.
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


def test_dedupe_spans_the_whole_conversation_not_a_short_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-102): a cue that already appeared must not pop up again
    later in the same conversation, even after many other cues — the old dedupe
    was a 10-deep rolling window an aged-out title slipped back through."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        # The first cue of the conversation.
        session._consider_cue(_final("pokemon number 1?", segment_id="s1"))
        await _drain_cues(session)
        assert len(_cues(sent)) == 1
        first_title = _cues(sent)[0].title

        # Fourteen more DISTINCT cues follow — well past any short rolling window.
        for i in range(2, 16):
            session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"pokemon number {i}?", segment_id=f"s{i}"))
            await _drain_cues(session)
        assert len(_cues(sent)) == 15

        # The very first cue's topic recurs much later.
        session._last_cue_monotonic = time.monotonic() - 3600
        session._consider_cue(_final("pokemon number 1?", segment_id="again"))
        await _drain_cues(session)
        await session.close()

        # It must not surface a second time.
        titles = [c.title for c in _cues(sent)]
        assert len(titles) == 15
        assert titles.count(first_title) == 1

    asyncio.run(run())


def test_session_hands_generator_the_titles_already_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-102: each generation is told which cues already appeared, in order, so
    a model-backed generator can produce a genuinely new cue rather than repeat."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        seen_avoid: list[list[str]] = []
        counter = {"n": 0}

        class SpyGen:
            def generate(self, transcript, *, avoid_titles=(), evidence=()):  # type: ignore[no-untyped-def]
                seen_avoid.append(list(avoid_titles))
                counter["n"] += 1
                return GeneratedCue(title=f"Cue {counter['n']}", body="body")

        session._cue_generator = SpyGen()

        for i, seg in enumerate(("a", "b", "c")):
            if i:
                session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"trigger {i}?", segment_id=seg))
            await _drain_cues(session)
        await session.close()

        # First call had nothing to avoid; each later call receives every cue
        # surfaced so far, in order.
        assert seen_avoid == [[], ["Cue 1"], ["Cue 1", "Cue 2"]]
        assert [c.title for c in _cues(sent)] == ["Cue 1", "Cue 2", "Cue 3"]

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
        await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
        session._consider_cue(_final("how far is the sun? 150", segment_id="a"))
        await _drain_cues(session)
        await session.close()

        # The socket was gone, but the cue is still recorded (like captions).
        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None and len(conv.cues) == 1

    asyncio.run(run())


def test_grounded_cue_carries_source_to_frame_and_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # XERK-120: with retrieval on (stub), the evidence source label rides the WS
    # frame and the persisted record — the whole attribution path, no network.
    monkeypatch.setattr(settings, "cue_backend", "stub")
    monkeypatch.setattr(settings, "cue_retrieval_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._cue_retriever is not None
        session._consider_cue(_final("how far is the Eiffel Tower?", end_ms=2000))
        await _drain_cues(session)
        await session.close()

        cues = _cues(sent)
        assert len(cues) == 1
        assert cues[0].source == "Stub Reference"

        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None
        assert conv.cues[0].source == "Stub Reference"

    asyncio.run(run())


def test_retrieval_off_leaves_cues_ungrounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")
    monkeypatch.setattr(settings, "cue_retrieval_backend", "off")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._cue_retriever is None
        session._consider_cue(_final("how far is the sun?"))
        await _drain_cues(session)
        await session.close()
        cues = _cues(sent)
        assert len(cues) == 1
        assert cues[0].source is None

    asyncio.run(run())


def test_retrieval_failure_still_yields_an_ungrounded_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Evidence is an upgrade, not a gate: a broken retriever must not cost the cue.
    monkeypatch.setattr(settings, "cue_backend", "stub")
    monkeypatch.setattr(settings, "cue_retrieval_backend", "stub")

    class ExplodingRetriever:
        closed = False

        async def retrieve(self, turns):
            raise RuntimeError("retrieval down")

        async def close(self):
            self.closed = True

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        exploding = ExplodingRetriever()
        session._cue_retriever = exploding
        session._consider_cue(_final("how far is the sun?"))
        await _drain_cues(session)
        await session.close()
        cues = _cues(sent)
        assert len(cues) == 1
        assert cues[0].source is None
        # The session releases the retriever's resources at close.
        assert exploding.closed

    asyncio.run(run())
