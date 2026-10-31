"""Tests for tenir-ocr's transport, pooling and line grouping.

PaddleOCR is out of scope: ``make_engine`` is replaced by a fake that returns fixed polygons,
so what is tested is the layer this repo owns — the detector margin and its removal from the
returned boxes, line grouping, the score floor, input limits, and readiness/busy handling.
The real engine is exercised by the image smoke test in .github/workflows/ocr.yml.
"""

from __future__ import annotations

import threading
from concurrent.futures import BrokenExecutor, ThreadPoolExecutor
from io import BytesIO

import numpy as np
import pytest
import server
from fastapi.testclient import TestClient
from PIL import Image

M = server.DETECTOR_MARGIN


def _poly(x0, y0, x1, y1):
    # Polygons come back in the padded image's coordinates.
    return np.array([[x0 + M, y0 + M], [x1 + M, y0 + M], [x1 + M, y1 + M], [x0 + M, y1 + M]])


class FakeEngine:
    def __init__(self, result=None, gate: threading.Event | None = None) -> None:
        self.result = result or []
        self.shapes: list[tuple[int, ...]] = []
        self.gate = gate

    def __call__(self, bgr):
        self.shapes.append(bgr.shape)
        if self.gate is not None:
            self.gate.wait(5)
        return self.result


def _png(w=200, h=40) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def make_client(monkeypatch):
    def factory(engine, workers=1):
        # Threads stand in for the worker processes: same initializer and task, but the fake
        # engine stays visible to the test. One fake is shared by every worker.
        monkeypatch.setattr(server, "make_engine", lambda: engine)
        monkeypatch.setattr(server, "WORKERS", workers)
        monkeypatch.setattr(
            server,
            "make_executor",
            lambda: ThreadPoolExecutor(workers, initializer=server._init_worker),
        )
        return TestClient(server.app)

    return factory


def test_lines_grouped_in_reading_order_with_unpadded_boxes(make_client):
    engine = FakeEngine(
        [
            (_poly(70, 40, 190, 60), "see you then", 0.97),
            (_poly(10, 10, 60, 30), "Ana:", 0.99),
            (_poly(65, 12, 180, 31), "shall we ship it", 0.91),
            (_poly(10, 40, 65, 60), "Bo:", 0.95),
        ]
    )
    with make_client(engine) as c:
        r = c.post("/v1/ocr", content=_png(), headers={"content-type": "image/png"})
    assert r.status_code == 200
    body = r.json()
    assert [ln["text"] for ln in body["lines"]] == ["Ana: shall we ship it", "Bo: see you then"]
    assert body["lines"][0]["box"] == [10, 10, 180, 31]
    assert body["lines"][0]["score"] == 0.91
    assert isinstance(body["ms"], int)
    assert body["too_dense"] is False
    # The engine saw the image with the black detector margin on every side (plus the
    # warm-up call on the blank image first).
    assert engine.shapes[-1] == (40 + 2 * M, 200 + 2 * M, 3)


def test_low_score_and_blank_segments_dropped(make_client):
    engine = FakeEngine([(_poly(0, 0, 10, 10), "noise", 0.4), (_poly(0, 20, 10, 30), "  ", 0.99)])
    with make_client(engine) as c:
        r = c.post("/v1/ocr", content=_png())
    assert r.json()["lines"] == []


def test_rejects_non_images_empty_and_oversize(make_client, monkeypatch):
    with make_client(FakeEngine()) as c:
        assert c.post("/v1/ocr", content=b"not an image").status_code == 400
        assert c.post("/v1/ocr", content=b"").status_code == 400
        monkeypatch.setattr(server, "MAX_BYTES", 100)
        assert c.post("/v1/ocr", content=_png()).status_code == 413


def test_rejects_too_many_pixels(make_client, monkeypatch):
    with make_client(FakeEngine()) as c:
        monkeypatch.setattr(server, "MAX_PIXELS", 100)
        assert c.post("/v1/ocr", content=_png()).status_code == 413


def test_health_reports_ready_only_inside_lifespan(make_client):
    with make_client(FakeEngine(), workers=2) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "workers": 2}
    # Outside the lifespan (not started / shut down) both endpoints refuse.
    c2 = TestClient(server.app)
    assert c2.get("/health").status_code == 503
    assert c2.post("/v1/ocr", content=_png()).status_code == 503


def test_busy_pool_returns_503_instead_of_queueing(make_client, monkeypatch):
    gate = threading.Event()
    gate.set()  # let the warm-up through
    engine = FakeEngine(gate=gate)
    monkeypatch.setattr(server, "BUSY_TIMEOUT_S", 0.2)
    with make_client(engine) as c:
        gate.clear()
        first: dict = {}
        t = threading.Thread(target=lambda: first.setdefault("r", c.post("/v1/ocr", content=_png())))
        t.start()
        for _ in range(200):  # wait until the first request holds the only engine
            if len(engine.shapes) == 2:
                break
            threading.Event().wait(0.01)
        assert c.post("/v1/ocr", content=_png()).status_code == 503
        gate.set()
        t.join(5)
        assert first["r"].status_code == 200
        # The slot was released: the next request is served.
        assert c.post("/v1/ocr", content=_png()).status_code == 200


def test_broken_worker_pool_fails_health(make_client, monkeypatch):
    with make_client(FakeEngine()) as c:
        def boom(_data):
            raise BrokenExecutor("worker died")

        monkeypatch.setattr(server, "_ocr_in_worker", boom)
        assert c.post("/v1/ocr", content=_png()).status_code == 503
        assert c.get("/health").status_code == 503


def test_chunked_body_over_the_byte_cap_is_refused(make_client, monkeypatch):
    # No Content-Length, so only the streaming cap can catch it.
    with make_client(FakeEngine()) as c:
        monkeypatch.setattr(server, "MAX_BYTES", 1000)
        r = c.post("/v1/ocr", content=iter([b"x" * 600, b"x" * 600]))
        assert r.status_code == 413


def test_oversize_pixels_refused_from_the_header(make_client, monkeypatch):
    # Just over the cap, and a 4K frame far past where Pillow's own bomb guard would fire (it
    # used to turn this into a 400): both 413, and neither is ever decoded.
    loads = []
    real_load = Image.Image.load
    monkeypatch.setattr(Image.Image, "load", lambda self: (loads.append(self.size), real_load(self))[1])
    bodies = [_png(2000, 300), _png(3840, 2160)]  # built first: saving a PNG calls load()
    with make_client(FakeEngine()) as c:
        loads.clear()
        for body in bodies:
            assert c.post("/v1/ocr", content=body).status_code == 413
        assert loads == []


def test_box_limit_counts_kept_boxes_and_stops_before_recognition(monkeypatch):
    # The wrapper sees the detector's FILTERED output: at the limit it passes, over it raises.
    monkeypatch.setattr(server, "MAX_BOXES", 3)
    limit = server._BoxLimit(lambda n: ([list(range(n))], [[0.9] * n]))
    assert limit(3) == ([[0, 1, 2]], [[0.9] * 3])
    with pytest.raises(server.TooDense):
        limit(4)


def test_too_dense_returns_no_lines(make_client):
    class Dense(FakeEngine):
        def __call__(self, bgr):
            if bgr.shape[0] > 64 + 2 * M:  # let the 64x32 warm-up through
                raise server.TooDense
            return []

    with make_client(Dense()) as c:
        body = c.post("/v1/ocr", content=_png(200, 100)).json()
    assert body["lines"] == [] and body["too_dense"] is True


def test_health_does_not_need_a_worker_thread():
    # A sync handler waits for an anyio thread, and under a burst those are all parked in
    # _slots.acquire: /health then stalled past the probe timeout (QA).
    import inspect

    assert inspect.iscoroutinefunction(server.health)


def test_health_fails_as_soon_as_the_pool_is_broken(make_client):
    with make_client(FakeEngine()) as c:
        server._executor._broken = "a worker died"
        assert c.get("/health").status_code == 503
