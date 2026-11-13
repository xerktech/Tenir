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
    assert lines[0].endswith(
        ": lines dropped before this: 3 by the client, 0 by the api rate limit"
    )
    # The client's own time: lines buffered through an outage arrive later (QA).
    assert lines[1].endswith(': [INFO] 1970-01-01T00:00:00.001+00:00 "Org A: capturing"')
    assert lines[2].endswith('"Org A: paused (no visible window matches)"')
    assert metrics.snapshot()["counters"]["screen.client_log_lines"] == 2


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "screen.log", "lines": []},
        batch(*["x"] * 51),
        batch("x" * 2001),
        batch("ok", level="TRACE"),
        {**batch("ok"), "extra": 1},
        {"type": "screen.log", "lines": [{"ts": "1", "level": "INFO", "message": "x"}]},
        {**batch("ok"), "dropped": "3"},
        {**batch("ok"), "dropped": -1},
    ],
    ids=[
        "empty",
        "too-many-lines",
        "line-too-long",
        "bad-level",
        "unknown-field",
        "string-ts",
        "string-dropped",
        "negative-dropped",
    ],  # fmt: skip
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
        # A ping behind it: a batch wrongly accepted gets the pong first, never a hang (QA).
        ws.send_text(json.dumps({"type": "ping"}))
        reply = json.loads(ws.receive_text())
    assert reply["code"] == "bad_request" and reply["message"].startswith("bad screen.log")
    assert client_lines(caplog) == []


@pytest.mark.parametrize("fill", ["x", "語"], ids=["ascii", "3-byte-chars"])
def test_an_oversized_text_frame_is_refused(screen, fill: str) -> None:  # noqa: F811
    from api.main import app

    _, _, token, _ = screen
    # Not JSON: with no cap it is answered "expected ...", never hangs the test.
    big = fill * (screen_ws.MAX_TEXT_BYTES // len(fill.encode()) + 1)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_text(big)
        assert json.loads(ws.receive_text())["message"] == "text frame too large"


def test_a_users_device_tokens_and_sockets_share_one_rate(
    screen,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.main import app
    from test_screen import device_token

    _, _, token, admin = screen
    other = device_token(admin)  # a second device of the same user
    caplog.set_level(logging.INFO, logger="api.screen.client")
    full = json.dumps(batch(*["l"] * 50))
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/screen?token={token}") as a,
        client.websocket_connect(f"/ws/screen?token={other}") as b,
    ):
        for ws in (a, b):
            ws.receive_text()
        for i in range(6):
            (a if i % 2 else b).send_text(full)
        for ws in (a, b):
            ws.send_text(json.dumps({"type": "ping"}))
            ws.receive_text()
    assert len(client_lines(caplog)) == client_log.BURST


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
    # Another device of the same user shares it; another user has their own.
    assert client_log.accept(full, device="dev-b", **kw) == 0
    assert client_log.accept(full, device="dev-a", **{**kw, "user_id": "v"}) == 50
    t["now"] = 5.0  # refills PER_SECOND lines a second
    one = ScreenLog.model_validate(batch("back"))
    assert client_log.accept(one, device="dev-a", **kw) == 1  # 5 tokens: notice + line
    lines = client_lines(caplog)
    assert lines[-2].endswith(": 0 by the client, 100 by the api rate limit")
    assert lines[-1].endswith('"back"')
    assert metrics.snapshot()["counters"]["screen.client_log_dropped"] >= 50


def test_dropped_counts_never_buy_log_lines_past_the_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # QA: a free "dropped" notice logged one line per message once the burst was spent.
    caplog.set_level(logging.INFO, logger="api.screen.client")
    kw = {"household": "h", "user_id": "u", "device": "d", "clock": lambda: 0.0}
    spam = ScreenLog.model_validate(batch("x", dropped=1))
    for _ in range(1000):
        client_log.accept(spam, **kw)
    assert len(client_lines(caplog)) <= client_log.BURST


def test_a_client_line_cannot_forge_a_log_line_or_another_users_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="api.screen.client")
    forged = (
        "Org A: ok\n2026-11-13 INFO api.screen captions off\r\x1b[2J"
        '" client h/other device deadbeef: [ERROR] "x\\n'
    )
    client_log.accept(
        ScreenLog.model_validate(batch(forged)), household="h", user_id="u", device="d"
    )
    (line,) = client_lines(caplog)
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    body = line.split(" ", 6)[-1]
    # One quoted string: its own quotes and backslashes are escaped, so it never closes early
    # and a literal backslash-n reads apart from an escaped newline.
    assert body.startswith('"Org A: ok\\n2026') and body.endswith('x\\\\n"')
    assert '\\" client h/other' in body


def test_an_out_of_range_client_time_is_logged_raw(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="api.screen.client")
    msg = ScreenLog.model_validate(
        {"type": "screen.log", "lines": [{"ts": 10**20, "level": "INFO", "message": "x"}]}
    )
    client_log.accept(msg, household="h", user_id="u", device="d")
    assert f"ts={10**20}" in client_lines(caplog)[0]


def test_the_bucket_table_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_log, "MAX_BUCKETS", 3)
    one = ScreenLog.model_validate(batch("x"))
    for i in range(7):
        client_log.accept(one, household="h", user_id=f"u{i}", device="d")
    assert len(client_log._buckets) <= 3
