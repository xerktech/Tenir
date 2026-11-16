"""tenir-diarizer HTTP surface and per-session timeline, with a fake model (XERK-2958).

The real Sortformer driver is checked against NeMo's own streaming pass by
``scripts/diarize_eval`` (it needs the model weights); here the stream is scripted.
"""

from __future__ import annotations

import base64
import os

import numpy as np
import pytest

os.environ["DIAR_NO_LOAD"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from server import FRAME_SAMPLES, Engines, Sessions, create_app  # noqa: E402


class ScriptStream:
    """Activity from a script: speaker k talks over the sample spans given."""

    def __init__(self, spans: dict[int, list[tuple[float, float]]]) -> None:
        self.spans = spans
        self.samples = 0
        self.fed: list[int] = []
        self.started_at: int | None = None

    def start_at(self, sample: int) -> None:
        self.started_at = sample
        self.samples = sample

    def feed(self, audio: np.ndarray) -> None:
        self.fed.append(len(audio))
        self.samples += len(audio)

    def activity(self, start_frame: int, end_frame: int) -> np.ndarray:
        out = np.zeros((end_frame - start_frame, 4), np.float32)
        for k, lst in self.spans.items():
            for a, b in lst:
                fa, fb = int(a * 12.5), int(b * 12.5)  # seconds -> 80 ms frames
                lo, hi = max(fa, start_frame), min(fb, end_frame)
                if hi > lo:
                    out[lo - start_frame : hi - start_frame, k] = 0.9
        return out


class OppositeSeparator:
    """Stream 0 is loud where the input's first half is, stream 1 the second half."""

    def __init__(self) -> None:
        self.calls = 0

    def separate(self, audio: np.ndarray) -> np.ndarray:
        self.calls += 1
        half = len(audio) // 2
        a = np.zeros(len(audio), np.float32)
        b = np.zeros(len(audio), np.float32)
        a[:half] = 0.3
        b[half:] = 0.3
        return np.stack([a, b])


def _pcm(seconds: float) -> bytes:
    return (np.full(int(16000 * seconds), 1000, np.int16)).tobytes()


def _app(stream: ScriptStream, separator=None) -> tuple[TestClient, Sessions]:
    sessions = Sessions(lambda: stream)
    return TestClient(create_app(Engines(sessions, separator), load=False)), sessions


def test_health_reports_loading_until_the_model_is_resident() -> None:
    c = TestClient(create_app(Engines(), load=False))
    assert c.get("/health").status_code == 503
    c2, _ = _app(ScriptStream({}))
    assert c2.get("/health").json() == {"status": "ok", "sessions": 0}


def test_a_loading_server_refuses_turns() -> None:
    c = TestClient(create_app(Engines(), load=False))
    assert c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1)).status_code == 503


def test_one_speaker_turn_is_one_piece() -> None:
    c, _ = _app(ScriptStream({1: [(0.2, 1.8)]}))
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(2)).json()
    assert r["pieces"] == [
        {"speaker": 2, "startMs": 0, "endMs": 2000, "overlap": False, "audio": None}
    ]


def test_a_brief_hand_off_is_not_separated() -> None:
    sep = OppositeSeparator()
    c, _ = _app(ScriptStream({0: [(0.0, 2.0)], 1: [(1.2, 4.0)]}), sep)  # 0.8 s shared
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(4)).json()
    assert all(p["overlap"] and p["audio"] is None for p in r["pieces"]) and sep.calls == 0


def test_overlap_comes_back_as_each_speakers_separated_audio() -> None:
    sep = OppositeSeparator()
    c, _ = _app(ScriptStream({0: [(0.0, 3.0)], 1: [(1.0, 4.0)]}), sep)  # 2 s shared
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(4)).json()
    pieces = r["pieces"]
    assert [(p["speaker"], p["overlap"]) for p in pieces] == [(1, True), (2, True)]
    assert all(p["audio"] for p in pieces) and sep.calls == 2
    pcm = np.frombuffer(base64.b64decode(pieces[0]["audio"]), "<i2")
    assert len(pcm) == (pieces[0]["endMs"] - pieces[0]["startMs"]) * 16


def test_a_third_speaker_in_an_overlap_gets_the_raw_mix() -> None:
    c, _ = _app(
        ScriptStream({0: [(0.0, 4.0)], 1: [(0.0, 4.0)], 2: [(1.0, 2.0)]}), OppositeSeparator()
    )
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(4)).json()
    third = [p for p in r["pieces"] if p["speaker"] == 3]
    assert third and third[0]["audio"] is None


def test_the_timeline_continues_across_turns_and_fills_gaps() -> None:
    stream = ScriptStream({})
    c, _ = _app(stream)
    c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1))
    # The api dropped a turn (its diarizer call timed out): the next one starts 0.5 s later.
    c.post("/v1/diarize?session=s&offset_ms=1500", content=_pcm(1))
    assert stream.fed == [16000, 8000, 16000]
    assert stream.samples == 16000 * 5 // 2


def test_a_resent_turn_is_not_fed_twice() -> None:
    stream = ScriptStream({})
    c, _ = _app(stream)
    c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1))
    c.post("/v1/diarize?session=s&offset_ms=500", content=_pcm(1))  # half already seen
    assert stream.fed == [16000, 8000]


def test_an_unknown_session_mid_conversation_starts_its_timeline_there() -> None:
    stream = ScriptStream({})
    c, _ = _app(stream)
    c.post("/v1/diarize?session=s&offset_ms=60000", content=_pcm(1))
    assert stream.started_at == 16000 * 60 and stream.fed == [16000]


def test_oversized_and_bad_requests_are_refused() -> None:
    c, _ = _app(ScriptStream({}))
    big = bytes(server.MAX_BODY + 2)
    assert c.post("/v1/diarize?session=s&offset_ms=0", content=big).status_code == 413
    assert c.post("/v1/diarize?session=&offset_ms=0", content=_pcm(1)).status_code == 422
    assert c.post("/v1/diarize?session=s&offset_ms=-1", content=_pcm(1)).status_code == 422


def test_sessions_expire_and_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    made = []
    s = Sessions(lambda: made.append(1) or ScriptStream({}), clock=lambda: now[0])
    first, fresh = s.get("a")
    assert fresh and s.get("a") == (first, False)
    now[0] += server.SESSION_TTL_S + 1
    assert s.get("a")[1] is True  # idle past the TTL: forgotten
    monkeypatch.setattr(server, "MAX_SESSIONS", 2)
    s.get("b")
    s.get("c")
    assert len(s) == 2 and s.get("a")[1] is True  # least recently used went first


def test_envelope_and_pcm_helpers() -> None:
    env = server._envelope(np.ones(FRAME_SAMPLES * 2, np.float32), 3)
    assert env.shape == (3,) and env[0] == pytest.approx(1.0) and env[2] < 1e-3
    pcm = np.frombuffer(base64.b64decode(server._pcm16(np.full(4, 0.01, np.float32))), "<i2")
    assert pcm.max() == int(0.5 * 32767)  # normalised: separator output has no scale
    assert server._pcm16(np.zeros(0, np.float32)) == ""


def test_an_overlap_too_long_to_separate_comes_back_as_the_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "MAX_SEPARATE_S", 2.0)
    sep = OppositeSeparator()
    c, _ = _app(ScriptStream({0: [(0.0, 4.0)], 1: [(0.5, 4.0)]}), sep)
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(4)).json()
    assert all(p["overlap"] and p["audio"] is None for p in r["pieces"]) and sep.calls == 0
