"""Speaker tracking on mic turns (XERK-2958).

The diarizer is faked: it hands back per-speaker pieces for each turn, and the engine
decodes audio by amplitude, so a test can tell which samples each caption came from.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

import httpx
import numpy as np
import pytest

from api import session as session_mod
from api.contract import CaptionFinal, CaptionPartial, MicSource, ServerMessage
from api.persistence import get_conversation_store
from api.session import Session
from api.stt import make_transcriber
from api.stt.diarize import Diarizer, Piece, parse_pieces
from api.stt.engine import EngineResult
from api.stt.streaming import StreamingTranscriber, _repeats

# Amplitudes the engine maps to words: the turn's own audio per speaker, and the
# separator's stream for an overlapped piece.
_A, _B, _SEP = 4000, 9000, 15000
_WORDS = {_A: "alpha says hello", _B: "bravo says hola", _SEP: "separated voice talking"}


class AmplitudeEngine:
    def __init__(self) -> None:
        self.decoded: list[int] = []  # sample count per decode

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        self.decoded.append(len(samples))
        peak = int(round(float(np.abs(samples).max()) * 32768)) if samples.size else 0
        text = next((t for a, t in _WORDS.items() if abs(peak - a) < 500), "")
        return EngineResult(text=text, words=[], language="en" if text else None)


class FakeDiarizer:
    def __init__(self, plan) -> None:
        self.plan = plan  # (pcm, offset) -> list[Piece] | None
        self.calls: list[tuple[int, int]] = []  # (offset_ms, len ms)
        self.closed = False

    async def diarize(self, pcm: bytes, offset_ms: int) -> list[Piece] | None:
        self.calls.append((offset_ms, len(pcm) // 32))
        return self.plan(pcm, offset_ms)

    def close(self) -> None:
        self.closed = True


def _tone(ms: int, amp: int) -> bytes:
    return np.full(16 * ms, amp, np.int16).tobytes()


def _sil(ms: int) -> bytes:
    return bytes(32 * ms)


def _finals(t: StreamingTranscriber) -> list[CaptionFinal]:
    out = []
    while not t._queue.empty():
        m = t._queue.get_nowait()
        if isinstance(m, CaptionFinal):
            out.append(m)
    return out


async def _turn(t: StreamingTranscriber, pcm: bytes) -> list[CaptionFinal]:
    await t.push(pcm)
    await t.flush()
    return _finals(t)


def _transcriber(diarizer: FakeDiarizer | None, engine: AmplitudeEngine | None = None):
    return StreamingTranscriber(
        engine or AmplitudeEngine(),
        partial_interval_ms=100_000,  # no partials: finals only
        local_agreement=False,
        vad_adaptive=False,
        diarizer=diarizer,  # type: ignore[arg-type]
    )


def test_without_a_diarizer_turns_are_unlabelled() -> None:
    async def run() -> None:
        t = _transcriber(None)
        (final,) = await _turn(t, _tone(1500, _A))
        assert final.text == _WORDS[_A] and final.speaker is None

    asyncio.run(run())


def test_a_single_speaker_turn_is_decoded_whole_and_labelled() -> None:
    async def run() -> None:
        engine = AmplitudeEngine()
        d = FakeDiarizer(lambda pcm, off: [Piece(2, 0, len(pcm) // 32, False, None)])
        t = _transcriber(d, engine)
        (final,) = await _turn(t, _tone(1500, _A))
        assert final.speaker == "Speaker 2"
        assert final.text == _WORDS[_A]
        assert engine.decoded == [16 * 1500]  # one whole-turn decode, as without diarizer

    asyncio.run(run())


def test_a_two_speaker_turn_becomes_one_caption_per_speaker() -> None:
    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [Piece(1, 0, 1000, False, None), Piece(3, 1000, 2000, False, None)]
        )
        t = _transcriber(d)
        finals = await _turn(t, _tone(1000, _A) + _tone(1000, _B))
        assert [(f.speaker, f.text, f.startMs, f.endMs) for f in finals] == [
            ("Speaker 1", _WORDS[_A], 0, 1000),
            ("Speaker 3", _WORDS[_B], 1000, 2000),
        ]
        assert len({f.segmentId for f in finals}) == 2

    asyncio.run(run())


def test_overlapped_speech_decodes_each_speakers_separated_stream() -> None:
    async def run() -> None:
        separated = _tone(1000, _SEP)
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1500, True, None),  # separator gave nothing back: raw span
                Piece(2, 500, 1500, True, separated),
            ]
        )
        t = _transcriber(d)
        finals = await _turn(t, _tone(1500, _A))
        assert [(f.speaker, f.text) for f in finals] == [
            ("Speaker 1", _WORDS[_A]),
            ("Speaker 2", _WORDS[_SEP]),
        ]
        # Overlapping times are the point: both spoke from 0.5 s to 1.5 s.
        assert finals[1].startMs < finals[0].endMs
        assert finals[1].words is None

    asyncio.run(run())


class UnsureEngine(AmplitudeEngine):
    """Whisper's confidence per decoded amplitude (verbose_json avg_logprob)."""

    def __init__(self, logprob: dict[int, float | None]) -> None:
        super().__init__()
        self.logprob = logprob

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        r = super().transcribe(samples, language=language, want_words=want_words)
        peak = int(round(float(np.abs(samples).max()) * 32768)) if samples.size else 0
        r.avg_logprob = next((v for a, v in self.logprob.items() if abs(peak - a) < 500), None)
        return r


def test_a_separated_stream_whisper_is_unsure_of_is_not_captioned() -> None:
    # XERK-3006: separator artefacts decode as fluent lines nobody said, at low confidence.
    # The raw span is the turn's own audio and keeps its caption at any confidence.
    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1500, True, None),
                Piece(2, 500, 1500, True, _tone(1000, _SEP)),
            ]
        )
        t = _transcriber(d, UnsureEngine({_A: -1.2, _SEP: -0.9}))
        finals = await _turn(t, _tone(1500, _A))
        assert [(f.speaker, f.text) for f in finals] == [("Speaker 1", _WORDS[_A])]

    asyncio.run(run())


@pytest.mark.parametrize("logprob", [-0.3, None])
def test_a_separated_stream_whisper_is_sure_of_or_gives_no_score_is_captioned(
    logprob: float | None,
) -> None:
    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1500, True, None),
                Piece(2, 500, 1500, True, _tone(1000, _SEP)),
            ]
        )
        t = _transcriber(d, UnsureEngine({_SEP: logprob}))
        finals = await _turn(t, _tone(1500, _A))
        assert [f.text for f in finals] == [_WORDS[_A], _WORDS[_SEP]]

    asyncio.run(run())


def test_a_turn_whose_separated_streams_are_all_unsure_is_captioned_whole() -> None:
    async def run() -> None:
        sep = _tone(1000, _SEP)
        d = FakeDiarizer(
            lambda pcm, off: [Piece(1, 0, 1000, True, sep), Piece(2, 0, 1000, True, sep)]
        )
        t = _transcriber(d, UnsureEngine({_SEP: -2.0}))
        (final,) = await _turn(t, _tone(1000, _A))
        assert final.text == _WORDS[_A]

    asyncio.run(run())


def test_a_voice_the_separator_left_in_both_streams_is_captioned_once() -> None:
    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1000, True, _tone(1000, _SEP)),
                Piece(2, 0, 1000, True, _tone(1000, _SEP)),
            ]
        )
        t = _transcriber(d)
        finals = await _turn(t, _tone(1000, _A))
        assert [f.speaker for f in finals] == ["Speaker 1"]

    asyncio.run(run())


def test_a_split_whose_pieces_all_come_back_blank_captions_the_turn_whole() -> None:
    async def run() -> None:
        quiet = _tone(1000, 1)  # decodes to nothing
        d = FakeDiarizer(
            lambda pcm, off: [Piece(1, 0, 500, True, quiet), Piece(2, 500, 1000, True, quiet)]
        )
        t = _transcriber(d)
        (final,) = await _turn(t, _tone(1000, _A))
        assert final.text == _WORDS[_A] and final.speaker is None

    asyncio.run(run())


def test_too_short_pieces_are_not_decoded() -> None:
    async def run() -> None:
        engine = AmplitudeEngine()
        d = FakeDiarizer(
            lambda pcm, off: [Piece(1, 0, 1700, False, None), Piece(2, 1700, 2000, False, None)]
        )
        t = _transcriber(d, engine)
        finals = await _turn(t, _tone(2000, _A))
        assert [f.speaker for f in finals] == ["Speaker 1"]
        assert engine.decoded == [16 * 1700]

    asyncio.run(run())


def test_a_diarizer_outage_captions_turns_unlabelled() -> None:
    async def run() -> None:
        t = _transcriber(FakeDiarizer(lambda pcm, off: None))
        (final,) = await _turn(t, _tone(1000, _A))
        assert final.text == _WORDS[_A] and final.speaker is None

    asyncio.run(run())


def test_every_turn_reaches_the_diarizer_on_one_timeline() -> None:
    """Silent turns too: the service keeps the conversation's clock from them."""

    async def run() -> None:
        d = FakeDiarizer(lambda pcm, off: [])
        t = StreamingTranscriber(
            AmplitudeEngine(),
            partial_interval_ms=100_000,
            local_agreement=False,
            vad_adaptive=False,
            max_segment_ms=1000,
            start_offset_ms=5000,
            diarizer=d,  # type: ignore[arg-type]
        )
        await t.push(_tone(1000, _A) + _sil(1000) + _tone(1000, _B))
        await t.flush()
        offsets = [off for off, _ in d.calls]
        lens = [n for _, n in d.calls]
        assert offsets[0] == 5000
        assert all(offsets[i] + lens[i] == offsets[i + 1] for i in range(len(offsets) - 1))
        assert sum(lens) == 3000
        await t.close()
        assert d.closed

    asyncio.run(run())


def test_repeats_tells_a_duplicated_voice_from_cross_talk() -> None:
    assert _repeats("Ya, te digo.", "ya te digo")
    # A separated stream that kept the tail of the other voice's sentence.
    assert _repeats("And so saying, she took them both by the hand", "she took them by the hand.")
    assert not _repeats("Guys, okay, wait.", "Ya, te digo.")
    assert not _repeats("hola", "")


def test_a_separated_piece_repeating_the_sequential_caption_is_dropped() -> None:
    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1000, False, None),  # raw span: alpha's words
                Piece(2, 500, 1000, True, _tone(500, _A)),  # separated, same voice again
            ]
        )
        t = _transcriber(d)
        finals = await _turn(t, _tone(1000, _A))
        assert [f.speaker for f in finals] == ["Speaker 1"]

    asyncio.run(run())


# ------------------------------------------------------------------- HTTP client ---


def _client(handler) -> Diarizer:
    d = Diarizer("http://diarizer:8000/", "conv-1", timeout=1.0)
    d._client = httpx.Client(transport=httpx.MockTransport(handler))
    return d


def test_client_posts_the_turn_and_parses_pieces() -> None:
    seen = {}
    sep = _tone(100, _SEP)

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["len"] = len(req.content)
        return httpx.Response(
            200,
            json={
                "speakers": 4,
                "pieces": [
                    {"speaker": 1, "startMs": 0, "endMs": 400, "overlap": False, "audio": None},
                    {
                        "speaker": 2,
                        "startMs": 300,
                        "endMs": 900,
                        "overlap": True,
                        "audio": base64.b64encode(sep).decode(),
                    },
                ],
            },
        )

    async def run() -> list[Piece] | None:
        return await _client(handler).diarize(_tone(1000, _A), 2500)

    pieces = asyncio.run(run())
    url = httpx.URL(seen["url"])
    assert url.path == "/v1/diarize"
    assert url.params["session"] == "conv-1" and url.params["offset_ms"] == "2500"
    assert len(url.params["epoch"]) == 32
    assert seen["len"] == 32000
    assert pieces == [Piece(1, 0, 400, False, None), Piece(2, 300, 900, True, sep)]
    assert pieces[1].label == "Speaker 2"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"status": "loading"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"pieces": [{"speaker": 0, "startMs": 0, "endMs": 5}]}),
        httpx.Response(200, json={"pieces": [{"speaker": 1, "startMs": 9, "endMs": 5}]}),
        httpx.Response(
            200, json={"pieces": [{"speaker": 1, "startMs": 0, "endMs": 5, "audio": "@@"}]}
        ),
    ],
    ids=["unavailable", "garbage", "speaker-0", "backwards", "bad-audio"],
)
def test_client_failures_read_as_no_diarization(response: httpx.Response) -> None:
    async def run() -> list[Piece] | None:
        return await _client(lambda req: response).diarize(_tone(100, _A), 0)

    assert asyncio.run(run()) is None


def test_client_deadline_covers_the_whole_request() -> None:
    import time

    def slow(req: httpx.Request) -> httpx.Response:
        time.sleep(0.5)
        return httpx.Response(200, json={"pieces": []})

    async def run() -> list[Piece] | None:
        d = _client(slow)
        d._timeout = 0.05
        return await d.diarize(_tone(100, _A), 0)

    assert asyncio.run(run()) is None


def test_parse_clamps_a_piece_to_the_turn_and_drops_an_odd_audio_byte() -> None:
    (p,) = parse_pieces(
        {"pieces": [{"speaker": 1, "startMs": 0, "endMs": 9999, "audio": "AAAA"}]}, turn_ms=500
    )
    assert p.end_ms == 500 and p.audio == b"\x00\x00"


def test_factory_wires_the_diarizer_only_when_configured(monkeypatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    monkeypatch.setattr(settings, "stt_vad_model", False)
    monkeypatch.setattr(settings, "diarizer_url", "")
    assert make_transcriber(conversation_id="c")._diarizer is None
    monkeypatch.setattr(settings, "diarizer_url", "http://diarizer:8000")
    assert make_transcriber()._diarizer is None  # no conversation to key it by
    d = make_transcriber(conversation_id="c")._diarizer
    assert isinstance(d, Diarizer) and d._url == "http://diarizer:8000/v1/diarize"
    assert "authorization" not in d._client.headers  # no key configured
    monkeypatch.setattr(settings, "diarizer_key", "s3cret")
    d = make_transcriber(conversation_id="c")._diarizer
    assert d._client.headers["authorization"] == "Bearer s3cret"


def test_client_sends_the_shared_key_on_every_turn() -> None:
    """XERK-3005: the diarizer refuses a turn without the bearer key."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json={"speakers": 0, "pieces": []})

    d = Diarizer("http://diarizer:8000", "conv-1", timeout=1.0, api_key="k1")
    d._client = httpx.Client(transport=httpx.MockTransport(handler), headers=d._client.headers)
    assert asyncio.run(d.diarize(b"\0" * 3200, 0)) == []
    assert seen == ["Bearer k1"]


# ----------------------------------------------------------------------- session ---


class _OneFinal:
    def __init__(self) -> None:
        self.closed = False
        self.final = CaptionFinal(
            type="caption.final",
            segmentId="seg-1",
            text="hola",
            startMs=0,
            endMs=900,
            speaker="Speaker 3",
        )

    async def warmup(self) -> None: ...

    async def push(self, pcm: bytes) -> None: ...

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        yield self.final
        while not self.closed:
            await asyncio.sleep(0.01)

    async def flush(self) -> None: ...

    async def close(self) -> None:
        self.closed = True

    finalizing = False
    behind_real_time = False


def test_session_sends_and_stores_the_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        fake = _OneFinal()
        monkeypatch.setattr(session_mod, "make_transcriber", lambda source_lang=None, **kw: fake)
        sent: list[ServerMessage] = []

        async def sender(m: ServerMessage) -> None:
            sent.append(m)

        session = Session(sender, household="default", session_id="conv-speakers")
        await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
        for _ in range(100):
            if any(isinstance(m, CaptionFinal) for m in sent):
                break
            await asyncio.sleep(0.01)
        await session.close()
        (final,) = [m for m in sent if isinstance(m, CaptionFinal)]
        assert json.loads(final.model_dump_json(exclude_none=True))["speaker"] == "Speaker 3"
        conv = get_conversation_store().get("default", "conv-speakers")
        assert conv is not None and [s.speaker for s in conv.segments] == ["Speaker 3"]

    asyncio.run(run())


def test_a_piece_whose_decode_fails_loses_only_that_piece() -> None:
    class Flaky(AmplitudeEngine):
        def transcribe(self, samples, *, language, want_words=True):
            if samples.size and abs(float(np.abs(samples).max()) * 32768 - _SEP) < 500:
                raise ValueError("upstream rejects this input")
            return super().transcribe(samples, language=language, want_words=want_words)

    async def run() -> None:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1000, False, None),
                Piece(2, 500, 1500, True, _tone(1000, _SEP)),
            ]
        )
        t = _transcriber(d, Flaky())
        finals = await _turn(t, _tone(1000, _A) + _tone(500, _B))
        assert [f.speaker for f in finals] == ["Speaker 1"]

    asyncio.run(run())


def test_a_failing_diarizer_is_skipped_for_a_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA: a blackholed diarizer cost its whole timeout on every turn; finals ran 27 s late."""
    import api.stt.diarize as diarize_mod

    now = [1000.0]
    monkeypatch.setattr(diarize_mod.time, "monotonic", lambda: now[0])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async def run() -> list:
        d = _client(handler)
        out = [await d.diarize(_tone(100, _A), 0)]
        now[0] += diarize_mod._COOLDOWN_S - 1
        out.append(await d.diarize(_tone(100, _A), 100))  # still cooling down: not sent
        now[0] += 2
        out.append(await d.diarize(_tone(100, _A), 200))  # retried
        return out

    assert asyncio.run(run()) == [None, None, None]
    assert len(calls) == 2


def test_each_sitting_sends_its_own_epoch() -> None:
    assert Diarizer("http://d", "c")._epoch != Diarizer("http://d", "c")._epoch


def test_a_silent_turn_is_never_split() -> None:
    """A turn with no speech decodes at most once, whatever the diarizer says."""

    async def run() -> None:
        engine = AmplitudeEngine()
        d = FakeDiarizer(
            lambda pcm, off: [Piece(1, 0, 500, False, None), Piece(2, 500, 1000, False, None)]
        )
        t = StreamingTranscriber(
            engine,
            partial_interval_ms=100_000,
            local_agreement=False,
            vad_adaptive=False,
            max_segment_ms=1000,
            diarizer=d,  # type: ignore[arg-type]
        )
        await t.push(_sil(1000))
        await t._jobs.join()
        assert d.calls and len(engine.decoded) <= 1 and _finals(t) == []

    asyncio.run(run())


def test_a_piece_starting_past_the_turn_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_pieces({"pieces": [{"speaker": 1, "startMs": 600, "endMs": 900}]}, turn_ms=500)


def test_a_padded_or_unsendable_key_never_crashes_the_session(caplog) -> None:
    d = Diarizer("http://diarizer:8000", "c", api_key="k1\n")
    assert d._client.headers["authorization"] == "Bearer k1"
    d = Diarizer("http://diarizer:8000", "c", api_key="é-key")  # no UnicodeEncodeError
    assert "authorization" not in d._client.headers
    assert "not printable ASCII" in caplog.text
    d = Diarizer("http://diarizer:8000", "c", api_key="ab\nc")  # inner control character
    assert "authorization" not in d._client.headers


def test_a_refused_key_is_named_in_the_log(caplog) -> None:
    d = _client(lambda req: httpx.Response(401, json={"detail": "bad"}))
    assert asyncio.run(d.diarize(b"\0" * 3200, 0)) is None
    assert "API_DIARIZER_KEY does not match" in caplog.text


def test_an_unconfigured_diarizer_says_why_in_the_log(caplog) -> None:
    d = _client(lambda req: httpx.Response(503, json={"detail": "DIAR_API_KEY is not set"}))
    assert asyncio.run(d.diarize(b"\0" * 3200, 0)) is None
    assert "DIAR_API_KEY is not set" in caplog.text


def test_factory_builds_an_offline_transcriber_for_a_recaption(monkeypatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    monkeypatch.setattr(settings, "stt_vad_model", False)
    monkeypatch.setattr(settings, "diarizer_url", "http://diarizer:8000")
    live = make_transcriber(conversation_id="c")
    off = make_transcriber(conversation_id="c", recaption=True)
    assert live._partial_bytes == settings.stt_partial_interval_ms * 32
    assert off._partial_bytes == 10**9 * 32
    assert live._shed_backlog and not off._shed_backlog
    assert off._diarizer._cooldown_s == 0 and live._diarizer._cooldown_s == 30.0
    assert off._diarizer._timeout == live._diarizer._timeout * 10


def test_a_diarizer_without_cooldown_retries_every_turn_and_counts_failures() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async def run() -> None:
        d = _client(handler)
        d._cooldown_s = 0.0
        for off in (0, 100, 200):
            assert await d.diarize(_tone(100, _A), off) is None
        assert d.failures == 3

    asyncio.run(run())
    assert len(calls) == 3


def test_a_recaption_never_merges_its_backlog(monkeypatch) -> None:
    """Fed faster than real time every final 'waits'; merging would fuse turns and speakers."""
    import api.stt.streaming as streaming

    async def run(shed: bool) -> int:
        t = StreamingTranscriber(
            AmplitudeEngine(),
            partial_interval_ms=100_000,
            local_agreement=False,
            vad_adaptive=False,
            max_segment_ms=1000,
            shed_backlog=shed,
        )
        merges = []
        monkeypatch.setattr(StreamingTranscriber, "behind_real_time", property(lambda self: True))
        monkeypatch.setattr(streaming, "_FINAL_BEHIND_S", -1.0)
        real = t._coalesce_backlog
        t._coalesce_backlog = lambda pcm: merges.append(1) or real(pcm)  # type: ignore[method-assign]
        await t.push(_tone(3000, _A))
        await t.flush()
        return len(merges)

    assert asyncio.run(run(True)) > 0
    assert asyncio.run(run(False)) == 0


def test_a_speaker_piece_stt_never_answered_counts_as_a_lost_turn() -> None:
    class Down(AmplitudeEngine):
        def transcribe(self, samples, *, language, want_words=True):
            if samples.size and abs(float(np.abs(samples).max()) * 32768 - _SEP) < 500:
                raise ValueError("rejected")
            return super().transcribe(samples, language=language, want_words=want_words)

    async def run() -> int:
        d = FakeDiarizer(
            lambda pcm, off: [
                Piece(1, 0, 1000, False, None),
                Piece(2, 500, 1500, True, _tone(1000, _SEP)),
            ]
        )
        t = _transcriber(d, Down())
        await _turn(t, _tone(1000, _A) + _tone(500, _B))
        return t.final_failures

    assert asyncio.run(run()) == 1
