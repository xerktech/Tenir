"""The capture client's own log lines on /ws/screen (screen.log, XERK-2875)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from test_screen import screen  # noqa: F401

from api.contract import ScreenLog
from api.metrics import metrics
from api.screen import client_log
from api.screen import ws as screen_ws


def batch(*messages: str, level: str = "INFO", dropped: int | None = None) -> dict:
    msg: dict = {
        "type": "screen.log",
        "lines": [{"ts": 1, "level": level, "message": m} for m in messages],
    }
    if dropped is not None:
        msg["dropped"] = dropped
    return msg


@pytest.fixture(autouse=True)
def fresh_buckets():
    client_log._buckets.clear()
    yield
    client_log._buckets.clear()


def client_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "api.screen.client"]


def test_a_device_s_log_lines_are_logged_under_its_owner_and_never_answered(
    screen,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.main import app

    _, _, token, admin = screen
    caplog.set_level(logging.INFO, logger="api.screen.client")
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # the idle cadence
        ws.send_text(json.dumps(batch("Org A: capturing", dropped=3)))
        ws.send_text(
            json.dumps(batch("Org A: paused (no visible window matches)", level="WARNING"))
        )
        # Nothing answers a log batch: the next reply is the ping's.
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 1}
    who = f"client {admin.household}/{admin.user_id} device "
    lines = client_lines(caplog)
    assert len(lines) == 3 and all(x.startswith(who) for x in lines)
    assert lines[0].endswith(": dropped 3 lines before this batch")
    assert lines[1].endswith(": [INFO] Org A: capturing")
    assert lines[2].endswith(": [WARNING] Org A: paused (no visible window matches)")
    assert metrics.snapshot()["counters"]["screen.client_log_lines"] == 2


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "screen.log", "lines": []},
        batch(*["x"] * 51),
        batch("x" * 2001),
        batch("ok", level="TRACE"),
        {**batch("ok"), "extra": 1},
    ],
    ids=["empty", "too-many-lines", "line-too-long", "bad-level", "unknown-field"],
)
def test_a_malformed_log_batch_is_refused_and_logs_nothing(
    screen,  # noqa: F811
    msg: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.main import app

    _, _, token, _ = screen
    caplog.set_level(logging.INFO, logger="api.screen.client")
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_text(json.dumps(msg))
        reply = json.loads(ws.receive_text())
    assert reply["code"] == "bad_request" and reply["message"].startswith("bad screen.log")
    assert client_lines(caplog) == []


def test_an_oversized_text_frame_is_refused(screen) -> None:  # noqa: F811
    from api.main import app

    _, _, token, _ = screen
    big = json.dumps(batch("x")) + " " * screen_ws.MAX_TEXT_BYTES
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_text(big)
        assert json.loads(ws.receive_text())["message"] == "text frame too large"


def test_lines_past_a_device_s_rate_are_dropped_and_counted_once_it_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="api.screen.client")
    t = {"now": 0.0}
    kw = {"household": "h", "user_id": "u", "clock": lambda: t["now"]}
    full = ScreenLog.model_validate(batch(*[f"l{i}" for i in range(50)]))
    for _ in range(client_log.BURST // 50):
        assert client_log.accept(full, device="dev-a", **kw) == 50
    assert client_log.accept(full, device="dev-a", **kw) == 0  # the burst is spent
    # Another device token has its own budget.
    assert client_log.accept(full, device="dev-b", **kw) == 50
    t["now"] = 5.0  # refills PER_SECOND lines a second
    one = ScreenLog.model_validate(batch("back"))
    assert client_log.accept(one, device="dev-a", **kw) == 1
    lines = client_lines(caplog)
    assert lines[-2].endswith(": rate-limited, 50 lines dropped")
    assert lines[-1].endswith(": [INFO] back")
    assert metrics.snapshot()["counters"]["screen.client_log_dropped"] >= 50


def test_a_client_line_cannot_forge_a_second_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="api.screen.client")
    forged = "Org A: ok\n2026-11-13 INFO api.screen screen captions off: fake\r\x1b[2J"
    client_log.accept(
        ScreenLog.model_validate(batch(forged)), household="h", user_id="u", device="d"
    )
    (line,) = client_lines(caplog)
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert "Org A: ok\\n2026-11-13" in line and line.endswith("\\r\\x1b[2J")


def test_the_bucket_table_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_log, "MAX_BUCKETS", 3)
    one = ScreenLog.model_validate(batch("x"))
    for i in range(7):
        client_log.accept(one, household="h", user_id="u", device=f"d{i}")
    assert len(client_log._buckets) <= 3
