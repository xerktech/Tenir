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


def test_session_hands_generator_the_cues_already_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-102: each generation is told which cues already appeared — title AND
    body, in order — so a model-backed generator can steer clear of their
    substance, not just their titles, and produce a genuinely new cue."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        seen_avoid: list[list[tuple[str, str]]] = []
        counter = {"n": 0}

        class SpyGen:
            def generate(self, transcript, *, avoid_cues=(), evidence=()):  # type: ignore[no-untyped-def]
                seen_avoid.append([(c.title, c.body) for c in avoid_cues])
                counter["n"] += 1
                return GeneratedCue(title=f"Cue {counter['n']}", body=f"Body {counter['n']}")

        session._cue_generator = SpyGen()

        for i, seg in enumerate(("a", "b", "c")):
            if i:
                session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"trigger {i}?", segment_id=seg))
            await _drain_cues(session)
        await session.close()

        # First call had nothing to avoid; each later call receives every cue
        # surfaced so far, in order, with its body.
        assert seen_avoid == [
            [],
            [("Cue 1", "Body 1")],
            [("Cue 1", "Body 1"), ("Cue 2", "Body 2")],
        ]
        assert [c.title for c in _cues(sent)] == ["Cue 1", "Cue 2", "Cue 3"]

    asyncio.run(run())


def test_session_drops_a_rephrased_duplicate_of_a_surfaced_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded-production failure the title dedupe can't catch: the same
    fact re-proposed under a fresh title with a reworded body ("Charlotte Drone
    Facility" then "Charlotte Drone Production"). The substance fingerprint must
    drop it, without blocking a genuinely different fact about the same topic."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    proposals = [
        GeneratedCue(
            title="Charlotte Drone Facility",
            body="The speaker claims their US facility in Charlotte can produce "
            "a drone every ninety seconds.",
        ),
        # Same fact, new title, reworded body — must be dropped.
        GeneratedCue(
            title="US Drone Production Scale",
            body="The speaker claims their Charlotte facility can produce a "
            "drone every 90 seconds, matching overseas production scale.",
        ),
        # Different fact about the same topic — must surface.
        GeneratedCue(
            title="Drone Payload",
            body="A 7-inch drone in this class can typically carry roughly one "
            "kilogram of payload over several kilometers.",
        ),
    ]

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        class ScriptedGen:
            def generate(self, transcript, *, avoid_cues=(), evidence=()):  # type: ignore[no-untyped-def]
                return proposals.pop(0)

        session._cue_generator = ScriptedGen()

        for i, seg in enumerate(("a", "b", "c")):
            if i:
                session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"trigger {i}?", segment_id=seg))
            await _drain_cues(session)
        await session.close()

        assert [c.title for c in _cues(sent)] == [
            "Charlotte Drone Facility",
            "Drone Payload",
        ]

    asyncio.run(run())


def test_session_drops_a_retitled_paraphrase_of_a_surfaced_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the session-2 review (RESULTS-2026-07.md): the same fact
    re-surfaced under a fresh title with a paraphrased body ("Under-Display
    Ultrasonic Sensor" then "Ultrasonic scanner", substance similarity 0.38) —
    under the old 0.5 threshold both were delivered. The 0.35 threshold must
    drop the paraphrase while a genuinely different fact still surfaces."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    proposals = [
        GeneratedCue(
            title="Under-Display Ultrasonic Sensor",
            body="An under-display ultrasonic fingerprint scanner uses "
            "high-frequency sound waves that pass through the screen to map "
            "the ridges of a finger, reading prints even through glass or "
            "wet skin.",
        ),
        # The measured paraphrase pair: same fact, new title, reworded body.
        GeneratedCue(
            title="Ultrasonic scanner",
            body="In smartphones, an ultrasonic fingerprint sensor projects "
            "high-frequency sound waves onto the finger and measures the echo "
            "to build a map of the ridges, so it reads prints through water "
            "or oil.",
        ),
        # Different fact about the same phone — must surface.
        GeneratedCue(
            title="IP68 rating",
            body="An IP68 rating means the phone is dust-tight and survives "
            "immersion in about 1.5 m of fresh water for roughly 30 minutes.",
        ),
    ]

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        class ScriptedGen:
            def generate(self, transcript, *, avoid_cues=(), evidence=()):  # type: ignore[no-untyped-def]
                return proposals.pop(0)

        session._cue_generator = ScriptedGen()

        for i, seg in enumerate(("a", "b", "c")):
            if i:
                session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"trigger {i}?", segment_id=seg))
            await _drain_cues(session)
        await session.close()

        assert [c.title for c in _cues(sent)] == [
            "Under-Display Ultrasonic Sensor",
            "IP68 rating",
        ]

    asyncio.run(run())


def test_session_drops_same_subject_at_a_new_angle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the 2026-07-27/28 production sessions: a surfaced subject
    re-cued at a new angle slipped both old backstops ("Grafana" then "Grafana
    origin" at substance similarity 0.18; "AWS Cognito User Pools" then "...User
    Pool" at 0.348 vs the 0.35 threshold). The title-subject containment layer
    must drop those, while a different subject sharing a generic title word
    ("C language" then "Ruby language") still surfaces."""
    monkeypatch.setattr(settings, "cue_backend", "stub")

    proposals = [
        GeneratedCue(
            title="Grafana",
            body="Grafana is an open-source analytics and monitoring platform "
            "popular for building dashboards over time-series data.",
        ),
        # Same subject, new angle — must be dropped by subject containment.
        GeneratedCue(
            title="Grafana origin",
            body="Grafana was launched in 2014 by Swedish developer Torkel "
            "Odegaard as an internal monitoring tool before going open source.",
        ),
        # Different subject sharing a generic title word — must surface.
        GeneratedCue(
            title="C language",
            body="The C programming language was developed at Bell Labs in the "
            "early 1970s and underpins many modern operating systems.",
        ),
        # Distinct subject sharing that generic word — must also surface.
        GeneratedCue(
            title="Ruby language",
            body="Ruby was created by Yukihiro Matsumoto and released in 1995, "
            "with a focus on programmer happiness and expressiveness.",
        ),
    ]

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        class ScriptedGen:
            def generate(self, transcript, *, avoid_cues=(), evidence=()):  # type: ignore[no-untyped-def]
                return proposals.pop(0)

        session._cue_generator = ScriptedGen()

        for i, seg in enumerate(("a", "b", "c", "d")):
            if i:
                session._last_cue_monotonic = time.monotonic() - 3600  # bypass rate limit
            session._consider_cue(_final(f"trigger {i}?", segment_id=seg))
            await _drain_cues(session)
        await session.close()

        assert [c.title for c in _cues(sent)] == [
            "Grafana",
            "C language",
            "Ruby language",
        ]

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
