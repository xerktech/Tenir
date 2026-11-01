"""The capture loop against fake windows and a real local WebSocket server."""

from __future__ import annotations

import json
import queue
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from PIL import Image
from websockets.sync.server import ServerConnection, serve

from tenir_capture.config import Config, WindowSpec
from tenir_capture.runner import Runner, TokenRejected


class FakeCapture:
    def __init__(self, img: Image.Image | None) -> None:
        self.img = img
        self.stopped = False
        self.dead = False

    def latest(self) -> Image.Image | None:
        return self.img

    def alive(self) -> bool:
        return not self.dead

    def stop(self) -> None:
        self.stopped = True


class FakePlatform:
    def __init__(self, titles: list[str], img: Image.Image | None) -> None:
        self.titles = titles
        self.img = img
        self.minimized: set[str] = set()
        self.notes: list[str] = []
        self.captures: dict[str, FakeCapture] = {}

    def list_titles(self) -> list[str]:
        return list(self.titles)

    def is_minimized(self, title: str) -> bool:
        return title in self.minimized

    def capture(self, title: str) -> FakeCapture:
        cap = self.captures[title] = FakeCapture(self.img)
        return cap

    def notify(self, title: str, message: str) -> None:
        self.notes.append(message)


class Sink:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)


@contextmanager
def server(handler: Callable[[ServerConnection], None]) -> Iterator[str]:
    with serve(handler, "127.0.0.1", 0) as srv:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{srv.socket.getsockname()[1]}"
        srv.shutdown()


def split(data: bytes) -> dict:
    (n,) = struct.unpack_from(">I", data)
    return json.loads(data[4 : 4 + n])


ZOOM = WindowSpec("Horizon 3", "org-c", "Org C", "zoom")
TEAMS = WindowSpec("Horizon 1", "org-a", "Org A", "teams")


def runner(url: str, platform: FakePlatform, **kw) -> Runner:
    cfg = Config(server=url, windows=(ZOOM, TEAMS))
    return Runner(cfg, "tok", platform, tick=0.02, refresh_every=0.05, **kw)


def run_in_thread(r: Runner) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def go() -> None:
        try:
            r.run()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, errors


def test_frames_stream_with_the_token_and_honour_the_servers_cadence(zoom_frame) -> None:
    got: queue.Queue = queue.Queue()
    auth: list[str | None] = []

    def handler(ws: ServerConnection) -> None:
        auth.append(ws.request.headers.get("Authorization"))
        ws.send(json.dumps({"type": "screen.cadence", "fps": 5, "fullFrameSeconds": 5}))
        for msg in ws:
            got.put(split(msg))

    platform = FakePlatform(["Horizon 3 - VMware", "Horizon 1"], zoom_frame(caption=True))
    with server(handler) as url:
        r = runner(url, platform)
        t, errors = run_in_thread(r)
        first = [got.get(timeout=5) for _ in range(4)]
        r.stop.set()
        t.join(5)
    assert not errors
    assert auth == ["Bearer tok"]
    assert [h["region"] for h in first[:2]] == ["full", "caption"]
    assert {h["orgSiteKey"] for h in first} == {"org-c"}  # the Teams window isn't captured yet
    assert first[0]["windowTitle"] == "Horizon 3 - VMware"
    assert "Horizon 1" not in platform.captures
    # 5 fps from the server: the caption crops come fast, not at the 0.2 fps default.
    assert [h["region"] for h in first[2:]] == ["caption", "caption"]
    assert platform.captures["Horizon 3 - VMware"].stopped is False
    r.close()
    assert platform.captures["Horizon 3 - VMware"].stopped


def test_minimized_or_retitled_windows_pause_and_the_operator_is_told(zoom_frame) -> None:
    platform = FakePlatform(["Horizon 3"], zoom_frame(caption=True))
    clock = {"t": 0.0}
    r = runner("http://x", platform, clock=lambda: clock["t"])
    sink = Sink()
    assert r.step(sink) == 2  # full + caption
    platform.minimized.add("Horizon 3")
    clock["t"] = 10
    assert r.step(sink) == 0
    assert len(platform.notes) == 1 and "Org C window is minimized" in platform.notes[0]
    clock["t"] = 20
    r.step(sink)
    assert len(platform.notes) == 1  # once per minimize, not every refresh
    platform.minimized.clear()
    clock["t"] = 25
    assert r.step(sink) == 1  # restored: the caption crop resumes
    # The RDM-style title change: no window matches, capture stops until it comes back.
    cap = platform.captures["Horizon 3"]
    platform.titles = ["Remote Desktop Manager [Dashboard]"]
    clock["t"] = 40
    assert r.step(sink) == 0 and cap.stopped
    platform.titles = ["Horizon 3"]
    clock["t"] = 50
    assert r.step(sink) == 2  # a re-found window starts with a full frame again
    # A capture whose window closed under it is restarted on the next refresh.
    platform.captures["Horizon 3"].dead = True
    clock["t"] = 60
    r.step(sink)
    assert platform.captures["Horizon 3"].dead is False
    assert {split(d)["orgSiteKey"] for d in sink.sent} == {"org-c"}


def test_a_window_without_a_frame_yet_sends_nothing() -> None:
    platform = FakePlatform(["Horizon 3"], None)
    r = runner("http://x", platform)
    assert r.step(Sink()) == 0


def test_a_captions_off_notice_becomes_a_desktop_notification() -> None:
    platform = FakePlatform([], None)
    r = runner("http://x", platform)
    r.on_message('{"type":"screen.notice","orgSiteKey":"org-c","kind":"captions_off"}')
    r.on_message('{"type":"screen.notice","orgSiteKey":"unknown","kind":"captions_off"}')
    r.on_message('{"type":"screen.notice","orgSiteKey":"org-c","kind":"new_kind"}')
    r.on_message('{"type":"error","code":"bad_request","message":"bad frame"}')
    assert len(platform.notes) == 1 and "Org C" in platform.notes[0]


def test_a_per_window_cadence_applies_to_that_window_only() -> None:
    platform = FakePlatform([], None)
    r = runner("http://x", platform)
    r.on_message('{"type":"screen.cadence","orgSiteKey":"org-c","fps":1,"fullFrameSeconds":30}')
    assert r._watched["org-c"].cadence is not None
    assert r._default.fps == 0.2


def test_a_rejected_token_stops_the_client() -> None:
    def handler(ws: ServerConnection) -> None:
        ws.close(1008, "invalid or expired device token")

    with server(handler) as url:
        r = runner(url, FakePlatform([], None))
        t, errors = run_in_thread(r)
        t.join(5)
    assert not t.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], TokenRejected)


def test_the_client_reconnects_after_the_server_drops_it() -> None:
    connections: list[int] = []

    def handler(ws: ServerConnection) -> None:
        connections.append(1)
        if len(connections) == 1:
            ws.close(1013, "ocr not configured")
            return
        for _ in ws:
            pass

    with server(handler) as url:
        r = runner(url, FakePlatform([], None))
        t, errors = run_in_thread(r)
        deadline = time.monotonic() + 8
        while len(connections) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        r.stop.set()
        t.join(5)
    assert not errors and len(connections) == 2


def test_an_unreachable_server_is_retried_until_stopped() -> None:
    r = runner("http://127.0.0.1:9", FakePlatform([], None))
    t, errors = run_in_thread(r)
    time.sleep(0.3)
    r.stop.set()
    t.join(5)
    assert not t.is_alive() and not errors


def test_a_reset_during_the_handshake_is_retried_not_fatal() -> None:
    """QA D1: a server that drops the connection mid-handshake raised InvalidMessage out of
    run(), killing the logon task until the next logon."""
    import socket

    accepted: list[int] = []
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen()

    def drop() -> None:
        while True:
            try:
                conn, _ = lsock.accept()
            except OSError:
                return
            accepted.append(1)
            conn.recv(4096)
            conn.close()  # no HTTP response at all

    threading.Thread(target=drop, daemon=True).start()
    r = runner(f"http://127.0.0.1:{lsock.getsockname()[1]}", FakePlatform([], None))
    t, errors = run_in_thread(r)
    deadline = time.monotonic() + 8
    while len(accepted) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    r.stop.set()
    t.join(5)
    lsock.close()
    assert not errors and len(accepted) >= 2


def test_a_rejected_token_stops_the_client_even_while_it_is_sending(zoom_frame) -> None:
    """QA D3: a send racing the server's 1008 close must still end in TokenRejected, at once."""
    connections: list[int] = []

    def handler(ws: ServerConnection) -> None:
        connections.append(1)
        ws.recv()  # the client is already streaming
        ws.close(1008, "invalid or expired device token")

    platform = FakePlatform(["Horizon 3"], zoom_frame(caption=True))
    with server(handler) as url:
        r = Runner(
            Config(server=url, windows=(ZOOM,)), "tok", platform, tick=0.001, refresh_every=0.05
        )
        r.on_message('{"type":"screen.cadence","fps":5,"fullFrameSeconds":5}')
        t, errors = run_in_thread(r)
        t.join(5)
    assert len(errors) == 1 and isinstance(errors[0], TokenRejected)
    assert connections == [1]


def test_a_new_socket_forgets_the_last_ones_cadence() -> None:
    """Per-window overrides belong to the socket that sent them (QA mutant m7)."""

    class Closed:
        close_code = 1000
        close_reason = ""

        def __iter__(self):
            return iter(())

        def close(self) -> None:
            pass

    r = runner("http://x", FakePlatform([], None))
    r.on_message('{"type":"screen.cadence","orgSiteKey":"org-c","fps":1,"fullFrameSeconds":30}')
    r.on_message('{"type":"screen.cadence","fps":2,"fullFrameSeconds":30}')
    r.session(Closed())  # type: ignore[arg-type]
    assert r._watched["org-c"].cadence is None and r._default.fps == 0.2


def test_a_faster_cadence_sends_the_next_caption_at_once(zoom_frame) -> None:
    """Without the reschedule the first 1 fps crop waits out the 5 s idle interval (m4)."""
    platform = FakePlatform(["Horizon 3"], zoom_frame(caption=True))
    clock = {"t": 0.0}
    r = runner("http://x", platform, clock=lambda: clock["t"])
    sink = Sink()
    assert r.step(sink) == 2
    clock["t"] = 1.0
    assert r.step(sink) == 0  # idle: next crop due at 5 s
    r.on_message('{"type":"screen.cadence","orgSiteKey":"org-c","fps":1,"fullFrameSeconds":30}')
    assert r.step(sink) == 1


def test_oversized_and_broken_frames_are_skipped_without_stopping_capture(
    zoom_frame, monkeypatch
) -> None:
    from tenir_capture import runner as runner_mod

    platform = FakePlatform(["Horizon 3"], zoom_frame(caption=True))
    clock = {"t": 0.0}
    r = runner("http://x", platform, clock=lambda: clock["t"])
    monkeypatch.setattr(runner_mod, "MAX_FRAME_BYTES", 100_000)  # full ~250 kB, crop ~20 kB
    sink = Sink()
    assert r.step(sink) == 1 and split(sink.sent[0])["region"] == "caption"  # m8
    # A window shrunk to a sliver: the finder raises; the frame is skipped, the loop lives.
    platform.captures["Horizon 3"].img = Image.new("RGB", (300, 2))
    clock["t"] = 10
    assert r.step(sink) == 0
    platform.captures["Horizon 3"].img = zoom_frame(caption=True)
    clock["t"] = 20
    assert r.step(sink) == 1


def test_backoff_restarts_from_one_second_after_a_healthy_connection(monkeypatch) -> None:
    """QA D2: a session shorter than 60 s kept the doubled backoff and lost ~10 s of captions."""
    from tenir_capture import runner as runner_mod

    waits: list[float] = []
    clock = {"t": 0.0}

    class Ws:
        def __enter__(self):
            return self

        def __exit__(self, *a: object) -> None:
            pass

    attempts = {"n": 0}

    def connect(*a: object, **k: object) -> Ws:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise OSError("refused")
        return Ws()

    r = runner("http://x", FakePlatform([], None), clock=lambda: clock["t"], connect=connect)

    def session(ws: object) -> None:
        clock["t"] += runner_mod.HEALTHY_SECONDS  # up long enough to count as healthy
        raise ConnectionError("closed 1012")

    monkeypatch.setattr(r, "session", session)

    class Stop:
        def is_set(self) -> bool:
            return len(waits) >= 5

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) >= 5

    r.stop = Stop()  # type: ignore[assignment]
    r.run()
    assert waits[:4] == [1.0, 2.0, 4.0, 1.0]


class ScriptedWs:
    """A connection whose send fails the way a server's close makes it fail."""

    def __init__(self, code: int, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason

    def __iter__(self):
        return iter(())

    def send(self, data: bytes) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.frames import Close

        raise ConnectionClosed(Close(self.close_code, self.close_reason), None)

    def close(self) -> None:
        pass


def test_a_send_that_races_a_1008_close_raises_token_rejected(zoom_frame) -> None:
    """QA D3 regression: without the ConnectionClosed swallow in session() the send error
    escaped as a plain disconnect and the client reconnected before stopping."""
    platform = FakePlatform(["Horizon 3"], zoom_frame(caption=True))
    r = runner("http://x", platform)
    ws = ScriptedWs(1008, "invalid or expired device token")
    done = threading.Event()
    r._receive = lambda ws, d: done.wait(1)  # the receiver hasn't seen the close yet
    try:
        r.session(ws)  # type: ignore[arg-type]
    except TokenRejected as exc:
        assert "device token" in str(exc)
    else:
        raise AssertionError("expected TokenRejected")


def test_any_session_error_reconnects_instead_of_killing_the_client(monkeypatch) -> None:
    """QA d8: an unexpected error inside a session must reconnect, not end run()."""

    class Ws:
        def __enter__(self):
            return self

        def __exit__(self, *a: object) -> None:
            pass

    sessions: list[int] = []
    r = runner("http://x", FakePlatform([], None), connect=lambda *a, **k: Ws())

    def session(ws: object) -> None:
        sessions.append(1)
        if len(sessions) >= 2:
            r.stop.set()
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(r, "session", session)
    r.run()
    assert len(sessions) == 2


def test_a_failing_window_logs_one_traceback_not_one_per_tick(zoom_frame, caplog) -> None:
    import logging

    platform = FakePlatform(["Horizon 3"], Image.new("RGB", (300, 1)))
    clock = {"t": 0.0}
    r = runner("http://x", platform, clock=lambda: clock["t"])
    with caplog.at_level(logging.INFO, logger="tenir_capture"):
        for t in range(0, 50, 10):
            clock["t"] = t
            r.step(Sink())
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 1
    # After a good frame, a new failure is news again and gets its traceback.
    with caplog.at_level(logging.INFO, logger="tenir_capture"):
        platform.captures["Horizon 3"].img = zoom_frame(caption=True)
        clock["t"] = 60
        r.step(Sink())
        platform.captures["Horizon 3"].img = Image.new("RGB", (300, 1))
        clock["t"] = 70
        r.step(Sink())
    assert len([rec for rec in caplog.records if rec.levelno >= logging.ERROR]) == 2
