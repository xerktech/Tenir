"""Encyclopedia fact check of cues about to be shown (api/cue/factcheck.py): the pure helpers,
the factory's gating, and the session dropping a contradicted cue while failing open."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import CaptionFinal, Cue, MicSource, ServerMessage
from api.cue import GeneratedCue, make_fact_checker
from api.cue.factcheck import FactChecker, _json, passages, quoted, should_check
from api.session import Session

# ---- pure helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "cue, checked",
    [
        (GeneratedCue("Cubone", "Cubone is a Ghost-type Pokémon."), True),
        (GeneratedCue("Mesha Stele", "The stele dates to about 840 BC.", "Wikipedia"), True),
        # Live news is newer than the encyclopedia mirror.
        (GeneratedCue("Storm", "The storm made landfall Tuesday.", "BBC News"), False),
        # So is anything recent, from memory too (the 2026 Isaias vs the 2020 one).
        (GeneratedCue("Hurricane Isaias", "The first hurricane of the 2026 season."), False),
        (GeneratedCue("Pixel", "The latest Pixel has a 6.3-inch screen."), False),
    ],
)
def test_should_check_skips_live_evidence_and_recent_events(cue: GeneratedCue, checked: bool):
    assert should_check(cue) is checked


def test_quoted_requires_the_conflicting_words_to_be_in_the_reference() -> None:
    ref = "Cubone is a Ground-type Pokémon introduced in Generation I."
    assert quoted("a Ground-type Pokémon", ref)
    assert quoted("Ground type Pokemon introduced", ref + " Pokemon")  # near-verbatim counts
    assert not quoted("a Ghost-type Pokémon", "Gastly is a Ghost and Poison type.")  # invented
    assert not quoted("Ground", ref)  # one word is not a quote
    # A pasted paragraph matches anything, so it is not accepted as "the conflicting words".
    assert not quoted(" ".join([ref] * 6), ref)


def test_passages_keep_the_lead_and_paragraphs_that_mention_the_cue() -> None:
    page = (
        "<p>Papa Smurf is the leader of the Smurfs and the oldest of them.</p>"
        "<p>Unrelated history of the comic strip and its many publishers over decades.</p>"
        "<td>He is typically dressed in red pants and a matching red Phrygian cap.</td>"
        "<p>short</p>"
    )
    text = passages(page, "Papa Smurf wears a white hat and white pants")
    assert text.startswith("Papa Smurf is the leader")
    assert "red pants" in text
    assert "Unrelated history" not in text
    assert passages("<div>no paragraphs</div>", "anything") == ""


def test_json_reads_the_first_object_and_tolerates_junk() -> None:
    assert _json('sure: {"titles": ["A"]} done') == {"titles": ["A"]}
    assert _json("no json") == {}
    assert _json("{broken") == {}
    assert _json("[1, 2]") == {}


def test_payload_runs_without_thinking_and_short() -> None:
    checker = FactChecker(endpoint="http://m/v1", model="m", kiwix_endpoint="http://k", kiwix_book="b")
    payload = checker._payload("sys", "user")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] <= 200


# ---- factory ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend, kiwix, enabled, built",
    [
        ("openai", "http://kiwix", True, True),
        ("openai", "", True, False),  # no mirror to check against
        ("openai", "http://kiwix", False, False),  # switched off
        ("stub", "http://kiwix", True, False),
    ],
)
def test_make_fact_checker_gating(
    monkeypatch: pytest.MonkeyPatch, backend: str, kiwix: str, enabled: bool, built: bool
) -> None:
    monkeypatch.setattr(settings, "cue_backend", backend)
    monkeypatch.setattr(settings, "cue_kiwix_endpoint", kiwix)
    monkeypatch.setattr(settings, "cue_fact_check", enabled)
    assert (make_fact_checker() is not None) is built


# ---- session ------------------------------------------------------------------------


def _final(text: str, *, end_ms: int = 2000) -> CaptionFinal:
    return CaptionFinal(
        type="caption.final", segmentId="s1", text=text, startMs=0, endMs=end_ms, lang="en"
    )


async def _session(sent: list[ServerMessage], checker) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
    session._cue_fact_checker = checker
    return session


async def _drain(session: Session) -> None:
    while session._cue_tasks:
        await asyncio.gather(*list(session._cue_tasks))


class _Checker:
    def __init__(self, verdict=None, *, raises: bool = False, sleep: float = 0.0) -> None:
        self.verdict, self.raises, self.sleep, self.seen = verdict, raises, sleep, []

    def contradiction(self, cue: GeneratedCue) -> str | None:
        self.seen.append(cue)
        if self.sleep:
            time.sleep(self.sleep)
        if self.raises:
            raise RuntimeError("kiwix down")
        return self.verdict


def _cues(sent: list[ServerMessage]) -> list[Cue]:
    return [m for m in sent if isinstance(m, Cue)]


def test_a_contradicted_cue_is_dropped_and_never_offered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        checker = _Checker("Type Ground")
        session = await _session(sent, checker)
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        assert _cues(sent) == [] and len(checker.seen) == 1
        # The same proposal again is a dedupe drop: it is not re-checked or shown.
        checker.verdict = None
        session._last_cue_monotonic = None
        session._consider_cue(_final("how far is the sun?", end_ms=4000))
        await _drain(session)
        await session.close()
        assert _cues(sent) == [] and len(checker.seen) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "checker",
    [_Checker(None), _Checker(raises=True), _Checker("Type Ground", sleep=0.5)],
    ids=["not-contradicted", "error", "timeout"],
)
def test_the_check_fails_open(monkeypatch: pytest.MonkeyPatch, checker: _Checker) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")
    monkeypatch.setattr(settings, "cue_fact_check_timeout_s", 0.1)

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _session(sent, checker)
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        await session.close()
        assert len(_cues(sent)) == 1

    asyncio.run(run())


def test_a_cue_checked_while_the_recording_ended_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _session(sent, None)

        class _Finishing(_Checker):
            def contradiction(self, cue: GeneratedCue) -> str | None:
                session._finished = True
                return None

        session._cue_fact_checker = _Finishing()
        session._consider_cue(_final("how far is the sun?"))
        await _drain(session)
        assert _cues(sent) == []
        await session.close()

    asyncio.run(run())
