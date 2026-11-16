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

from fastapi.testclient import TestClient

import server
from server import FRAME_SAMPLES, Engines, Sessions, create_app

KEY = "test-diarizer-key"
AUTH = {"authorization": f"Bearer {KEY}"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "API_KEY", KEY)


class ScriptStream:
    """Activity from a script: speaker k talks over the sample spans given."""

    def __init__(self, spans: dict[int, list[tuple[float, float]]]) -> None:
        self.spans = spans
        self.samples = 0
        self.fed: list[int] = []
        self.checks: list = []  # the abandoned callback each feed was given
        self.started_at: int | None = None

    def start_at(self, sample: int) -> None:
        self.started_at = sample
        self.samples = sample

    def feed(self, audio: np.ndarray, abandoned=None) -> None:
        self.checks.append(abandoned)
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
    app = create_app(Engines(sessions, separator), load=False)
    return TestClient(app, headers=AUTH), sessions


def test_health_reports_loading_until_the_model_is_resident() -> None:
    c = TestClient(create_app(Engines(), load=False), headers=AUTH)
    assert c.get("/health").status_code == 503
    c2, _ = _app(ScriptStream({}))
    assert c2.get("/health").json() == {"status": "ok", "sessions": 0}


def test_a_loading_server_refuses_turns() -> None:
    c = TestClient(create_app(Engines(), load=False), headers=AUTH)
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


def test_a_new_sitting_behind_the_timeline_restarts_the_session() -> None:
    """QA: a resume with no retained audio restarts the api's clock behind ours; that audio
    is new speech, and skipping it labelled one speaker's turn from another's frames."""
    streams: list[ScriptStream] = []

    def make() -> ScriptStream:
        streams.append(ScriptStream({}))
        return streams[-1]

    c = TestClient(create_app(Engines(Sessions(make), None), load=False), headers=AUTH)
    for off in (0, 5000, 10000):
        c.post(f"/v1/diarize?session=s&offset_ms={off}&epoch=a", content=_pcm(5))
    c.post("/v1/diarize?session=s&offset_ms=10000&epoch=b", content=_pcm(5))
    assert len(streams) == 2
    assert streams[1].started_at == 160000 and streams[1].fed == [80000]


def test_a_late_resend_in_the_same_sitting_is_not_a_restart() -> None:
    stream = ScriptStream({})
    c, _ = _app(stream)
    c.post("/v1/diarize?session=s&offset_ms=0&epoch=a", content=_pcm(2))
    c.post("/v1/diarize?session=s&offset_ms=0&epoch=a", content=_pcm(1))
    assert stream.fed == [32000] and stream.started_at is None


def test_a_long_jump_moves_the_clock_instead_of_feeding_silence() -> None:
    """QA: offset_ms=300000 on a live session ran the model over 5 min of zeros (56 s CPU)."""
    stream = ScriptStream({})
    c, _ = _app(stream)
    c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1))
    c.post("/v1/diarize?session=s&offset_ms=300000", content=_pcm(1))
    assert stream.fed == [16000, 16000] and stream.started_at == 16000 * 300


def test_offset_and_epoch_are_bounded() -> None:
    c, _ = _app(ScriptStream({}))
    assert (
        c.post(
            f"/v1/diarize?session=s&offset_ms={server.MAX_OFFSET_MS + 1}", content=_pcm(1)
        ).status_code
        == 422
    )
    assert (
        c.post("/v1/diarize?session=s&offset_ms=0&epoch=" + "e" * 65, content=_pcm(1)).status_code
        == 422
    )


def test_a_body_without_a_length_is_capped_while_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_BODY", 1000)
    c, _ = _app(ScriptStream({}))

    def chunks():
        for _ in range(10):
            yield bytes(500)

    r = c.post("/v1/diarize?session=s&offset_ms=0", content=chunks())
    assert r.status_code == 413


def test_a_clock_jump_drops_the_scored_history_before_it() -> None:
    """SortformerStream.start_at: frames scored before a jump must not be read back as if
    they sat after it (the committed buffer is indexed from its origin)."""
    from types import SimpleNamespace

    s = server.SortformerStream.__new__(server.SortformerStream)
    s._mods = SimpleNamespace(n_spk=4)
    s._lc = 1
    s._head, s._preds_origin = 10, 0
    s._preds = np.full((10, 4), 0.9, np.float32)
    s._buf, s._buf_origin, s.samples = np.zeros(0, np.float32), 0, 10 * FRAME_SAMPLES
    s.start_at(100 * FRAME_SAMPLES)
    assert s._preds.shape == (0, 4) and s._preds_origin == s._head == 100
    assert s.activity(95, 100).max() == 0.0  # nothing committed there


# ----------------------------------------------------------- auth and abandoned turns ---
# XERK-3005: the service holds every conversation's speaker cache, so only the api (holding
# the shared key) may feed or read it; and a turn the api gave up on is not worth running.


def test_a_turn_needs_the_shared_bearer_key() -> None:
    stream = ScriptStream({0: [(0, 1)]})
    c, _ = _app(stream)
    url = "/v1/diarize?session=s&offset_ms=0"
    assert c.post(url, content=_pcm(1), headers={"authorization": ""}).status_code == 401
    wrong = {"authorization": "Bearer nope"}
    assert c.post(url, content=_pcm(1), headers=wrong).status_code == 401
    bare = {"authorization": KEY}  # the key without its scheme
    assert c.post(url, content=_pcm(1), headers=bare).status_code == 401
    assert stream.fed == []  # a refused caller never reaches the session
    assert c.post(url, content=_pcm(1)).status_code == 200
    assert c.get("/health", headers={"authorization": ""}).status_code == 200  # probes


def test_without_a_configured_key_every_turn_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "API_KEY", "")
    stream = ScriptStream({})
    c, _ = _app(stream)
    no_key = {"authorization": "Bearer "}  # an empty key must not match an empty secret
    for headers in (AUTH, no_key, {"authorization": ""}):
        r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1), headers=headers)
        assert r.status_code == 503
    assert stream.fed == []


def test_a_turn_the_api_hung_up_on_is_dropped_unfed(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    async def gone(self) -> bool:
        return True

    monkeypatch.setattr(Request, "is_disconnected", gone)
    stream = ScriptStream({0: [(0, 1)]})
    c, _ = _app(stream)
    r = c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1))
    assert r.status_code == 499
    assert stream.fed == [] and stream.samples == 0


def test_a_connected_api_gets_its_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    polls = []

    async def here(self) -> bool:
        polls.append(1)
        return False

    monkeypatch.setattr(Request, "is_disconnected", here)
    c, _ = _app(ScriptStream({0: [(0, 1)]}))
    assert c.post("/v1/diarize?session=s&offset_ms=0", content=_pcm(1)).status_code == 200
    assert polls  # really asked


def test_abandoning_after_the_feed_skips_separation() -> None:
    stream = ScriptStream({0: [(0, 3)], 1: [(1, 4)]})
    sep = OppositeSeparator()
    sess, _ = Sessions(lambda: stream).get("s")
    calls = []

    def abandoned() -> bool:
        calls.append(1)
        return len(calls) > 2  # entry, after the feed: connected; then gone

    with pytest.raises(server.Abandoned):
        server.diarize_turn(sess, True, 0, _pcm(4), sep, abandoned=abandoned)
    assert stream.fed == [64000]  # the turn was fed: the timeline stays continuous
    assert sep.calls == 0  # but the separator never ran for it


def test_abandoning_mid_feed_leaves_the_stream_resumable() -> None:
    """Sortformer stops between chunks; the next feed scores what was left."""
    import threading

    class Mods:
        chunk_len, chunk_left_context, chunk_right_context, n_spk = 1, 0, 0, 4

        def init_streaming_state(self, **_: object) -> None:
            return None

    class Model:
        sortformer_modules = Mods()
        device = "cpu"

    stream = server.SortformerStream(Model(), threading.Lock())
    steps = []

    def step(state, head, chunk, rc):
        steps.append(head)
        return np.zeros((chunk, 4), np.float32), state

    stream._step = step  # type: ignore[method-assign]
    polls = []

    def abandoned() -> bool:
        polls.append(1)
        return len(polls) > 3

    with pytest.raises(server.Abandoned):
        stream.feed(np.zeros(FRAME_SAMPLES * 10, np.float32), abandoned)
    assert steps == [0, 1, 2]
    stream.feed(np.zeros(0, np.float32))
    assert steps == list(range(len(steps))) and stream._head == len(steps) >= 9


def test_every_feed_of_a_turn_can_be_abandoned_between_steps() -> None:
    """The gap fill and the turn's own audio both get the check (Sortformer polls it
    between chunks), not only diarize_turn's own checkpoints."""
    stream = ScriptStream({0: [(0, 3)]})
    sess, fresh = Sessions(lambda: stream).get("s")
    server.diarize_turn(sess, fresh, 0, _pcm(1), None)
    stream.checks.clear()

    def here() -> bool:
        return False

    server.diarize_turn(sess, False, 2000, _pcm(1), None, abandoned=here)
    assert stream.fed[-2:] == [16000, 16000]  # the 1 s gap, then the turn
    assert stream.checks == [here, here]


def test_a_key_with_a_trailing_newline_still_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    monkeypatch.setenv("DIAR_API_KEY", f" {KEY}\n")
    try:
        assert importlib.reload(server).API_KEY == KEY
    finally:
        monkeypatch.delenv("DIAR_API_KEY")
        importlib.reload(server)
