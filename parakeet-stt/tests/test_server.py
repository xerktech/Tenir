"""Tests for the Parakeet STT server's transport and dispatch logic.

NeMo (and a GPU) are deliberately out of scope: everything below the
``_model.transcribe`` call is the model's business, and pulling the multi-GB NGC
stack into a PR gate isn't worth it. What *is* worth testing is the layer this repo
actually owns and keeps breaking — response-format handling, the kwargs the server
decides to pass NeMo, and the readiness/warmup sequencing — so ``_model`` is a fake
that records how it was called.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
import server
from fastapi.testclient import TestClient


class FakeHypothesis:
    def __init__(self, text: str = "hello world", *, timing: bool = True) -> None:
        self.text = text
        self.lang = "en"
        self.timestamp = (
            {
                "word": [
                    {"word": "hello", "start": 0.0, "end": 0.4},
                    {"word": "world", "start": 0.4, "end": 0.9},
                ],
                "segment": [{"segment": "hello world", "start": 0.0, "end": 0.9}],
            }
            if timing
            else {}
        )


class FakeModel:
    """Records every transcribe() call. Its signature carries both optional kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, audio, timestamps=True, source_lang=None):  # noqa: ANN001
        self.calls.append({"timestamps": timestamps, "source_lang": source_lang})
        return [FakeHypothesis(timing=timestamps)]


def _wav(seconds: float = 0.5) -> bytes:
    """A 16 kHz mono s16le WAV — exactly what the api's engine uploads."""
    frames = np.zeros(int(server.TARGET_SR * seconds), dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(server.TARGET_SR)
        w.writeframes(frames.tobytes())
    return buf.getvalue()


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(server, "_model", fake)
    monkeypatch.setattr(server, "_supported", frozenset(("timestamps", "source_lang")))
    server._ready.set()
    yield fake
    server._ready.clear()


@pytest.fixture
def client(model: FakeModel) -> TestClient:
    return TestClient(server.app)


def _post(client: TestClient, **data: object):
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("audio.wav", _wav(), "audio/wav")},
        data=data,
    )


# ----- readiness ------------------------------------------------------------


def test_health_is_503_until_the_model_is_resident() -> None:
    server._ready.clear()
    client = TestClient(server.app)
    assert client.get("/health").status_code == 503
    server._ready.set()
    try:
        assert client.get("/health").status_code == 200
    finally:
        server._ready.clear()


def test_transcribe_is_503_while_loading() -> None:
    server._ready.clear()
    client = TestClient(server.app)
    resp = client.post(
        "/v1/audio/transcriptions", files={"file": ("audio.wav", _wav(), "audio/wav")}
    )
    assert resp.status_code == 503


# ----- response formats -----------------------------------------------------


def test_json_carries_text_language_and_words(client: TestClient) -> None:
    body = _post(client).json()
    assert body["text"] == "hello world"
    assert body["language"] == "en"
    assert body["words"][0] == {"word": "hello", "start": 0.0, "end": 0.4}


def test_text_format_returns_plain_text(client: TestClient) -> None:
    resp = _post(client, response_format="text")
    assert resp.text == "hello world"


def test_verbose_json_adds_segments_and_duration(client: TestClient) -> None:
    body = _post(client, response_format="verbose_json").json()
    assert body["duration"] == pytest.approx(0.5)
    assert body["segments"][0]["text"] == "hello world"
    assert body["words"]


# ----- the timestamps flag (XERK-115) ---------------------------------------


def test_timestamps_default_to_on(client: TestClient, model: FakeModel) -> None:
    _post(client)
    assert model.calls[-1]["timestamps"] is True


def test_timestamps_false_skips_timing_work(client: TestClient, model: FakeModel) -> None:
    # The api's partials send this: they only ever read the text.
    body = _post(client, timestamps="false").json()
    assert model.calls[-1]["timestamps"] is False
    assert body["text"] == "hello world"  # still the whole point of the request
    assert body["words"] == []


def test_verbose_json_keeps_timing_even_if_asked_to_skip(
    client: TestClient, model: FakeModel
) -> None:
    # verbose_json's contract includes segments, so the format wins over the flag.
    body = _post(client, timestamps="false", response_format="verbose_json").json()
    assert model.calls[-1]["timestamps"] is True
    assert body["segments"]


# ----- language pinning -----------------------------------------------------


def test_language_is_pinned_when_given(client: TestClient, model: FakeModel) -> None:
    _post(client, language="es")
    assert model.calls[-1]["source_lang"] == "es"


def test_language_is_left_to_auto_detection_when_absent(
    client: TestClient, model: FakeModel
) -> None:
    _post(client)
    assert model.calls[-1]["source_lang"] is None


# ----- NeMo signature drift -------------------------------------------------


def test_supported_kwargs_reads_an_explicit_signature() -> None:
    class OldModel:
        def transcribe(self, audio, timestamps=True):  # noqa: ANN001 — no source_lang
            return []

    assert server._supported_kwargs(OldModel()) == frozenset({"timestamps"})


def test_supported_kwargs_assumes_everything_behind_var_keyword() -> None:
    # NeMo forwards **config_kwargs into the decode config, so an implicit name may
    # still be honoured — assume it is rather than silently stopping pinning language.
    class KwargsModel:
        def transcribe(self, audio, **config_kwargs):  # noqa: ANN001
            return []

    assert server._supported_kwargs(KwargsModel()) == frozenset(server._OPTIONAL_KWARGS)


def test_supported_kwargs_falls_back_when_signature_is_unreadable() -> None:
    class Opaque:
        transcribe = print  # a builtin: inspect.signature may refuse it

    assert server._supported_kwargs(Opaque()) <= frozenset(server._OPTIONAL_KWARGS)


def test_unsupported_kwargs_are_dropped_once_not_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that rejects the kwargs must cost ONE failed call, not one per decode —
    and must still return a transcript."""

    class PickyModel:
        def __init__(self) -> None:
            self.rejections = 0
            self.calls = 0

        def transcribe(self, audio, **kwargs):  # noqa: ANN001
            self.calls += 1
            if kwargs:
                self.rejections += 1
                raise TypeError("unexpected keyword argument")
            return [FakeHypothesis()]

    picky = PickyModel()
    monkeypatch.setattr(server, "_model", picky)
    monkeypatch.setattr(server, "_supported", frozenset(server._OPTIONAL_KWARGS))

    samples = np.zeros(server.TARGET_SR // 2, dtype=np.float32)
    assert server._transcribe_sync(samples, "es", True)["text"] == "hello world"
    assert server._transcribe_sync(samples, "es", True)["text"] == "hello world"

    assert picky.rejections == 1  # narrowed permanently after the first failure
    assert picky.calls == 3
    assert server._supported == frozenset()


def test_a_bare_typeerror_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no kwargs left to blame, a TypeError is a real bug — don't swallow it."""

    class BrokenModel:
        def transcribe(self, audio, **kwargs):  # noqa: ANN001
            raise TypeError("something else entirely")

    monkeypatch.setattr(server, "_model", BrokenModel())
    monkeypatch.setattr(server, "_supported", frozenset())

    with pytest.raises(TypeError, match="something else entirely"):
        server._transcribe_sync(np.zeros(16, dtype=np.float32), None, True)


# ----- warmup (XERK-115) ----------------------------------------------------


def test_warmup_decodes_a_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeModel()
    monkeypatch.setattr(server, "_model", fake)
    monkeypatch.setattr(server, "_supported", frozenset(("timestamps", "source_lang")))

    server._warmup()
    assert len(fake.calls) == 1  # the first real request no longer pays for setup


def test_warmup_failure_does_not_keep_the_server_down(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingModel:
        def transcribe(self, audio, **kwargs):  # noqa: ANN001
            raise RuntimeError("CUDA is having a day")

    monkeypatch.setattr(server, "_model", ExplodingModel())
    monkeypatch.setattr(server, "_supported", frozenset())

    server._warmup()  # must not raise — a slow first decode beats a dead server


# ----- audio decoding -------------------------------------------------------


def test_off_rate_stereo_audio_is_downmixed_and_resampled() -> None:
    """LiteLLM's health-probe clip and ad-hoc uploads aren't 16 kHz mono."""
    stereo = np.zeros((8000, 2), dtype="<i2")  # 1s of 8 kHz stereo
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(stereo.tobytes())

    samples = server._decode_audio(buf.getvalue())
    assert samples.ndim == 1
    assert samples.dtype == np.float32
    assert samples.size == pytest.approx(server.TARGET_SR, rel=0.05)


# ----- defensive hypothesis readers -----------------------------------------


def test_hyp_words_drops_entries_without_real_timing() -> None:
    class OffsetOnly:
        timestamp = {"word": [{"word": "hi", "start_offset": 3, "end_offset": 9}]}

    assert server._hyp_words(OffsetOnly()) == []


def test_hyp_words_handles_a_missing_timestamp_dict() -> None:
    assert server._hyp_words(object()) == []


def test_hyp_language_probes_candidate_attributes() -> None:
    class Listed:
        languages = ["es"]

    assert server._hyp_language(Listed(), None) == "es"
    assert server._hyp_language(object(), "fr") == "fr"  # falls back to the request
    assert server._hyp_language(object(), None) is None
