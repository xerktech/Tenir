"""Delivery of finished screen meetings to the Turma hub (XERK-2513).

The hub side is Turma's POST /api/orgs/<site>/meetings (contract-meetings.md). These tests
fake it with an httpx MockTransport; the real hub was driven end to end in QA.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from api import status as st
from api.config import settings
from api.persistence import InMemoryConversationStore, ScreenMeta, Segment
from api.persistence.models import utcnow
from api.screen import turma
from api.screen.meetings import Cadence, MeetingTracker
from api.screen.zoom import CaptionBlock

SITE = "acme.atlassian.net"


def _meeting(store: InMemoryConversationStore, cid: str = "c1", *, lines=("hello there",)):
    store.create("default", cid, owner="u1", screen=ScreenMeta(SITE, "Acme", "zoom"))
    for i, text in enumerate(lines):
        store.add_segment(
            "default", cid, Segment(f"s{i}", text, i * 1000, i * 1000 + 900, speaker="Ana")
        )
    store.add_participants("default", cid, [("Ana", 0), ("Bo", 0)])
    store.set_meeting_title("default", cid, "Weekly sync")
    store.finish("default", cid)
    return store.get("default", cid)


class Hub:
    """A scripted hub: answers each POST with the next scripted response."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _delivery(store, hub, clock, **kw) -> turma.TurmaDelivery:
    return turma.TurmaDelivery(
        store,
        url="http://hub/",
        token="tok",
        client=httpx.AsyncClient(transport=httpx.MockTransport(hub)),
        clock=lambda: clock["now"],
        **kw,
    )


def _ok(duplicate: bool = False) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "duplicate": duplicate, "meeting": {}})


@pytest.fixture
def clock():
    return {"now": utcnow().timestamp()}


def test_a_ready_meeting_is_posted_once_in_the_contract_shape(clock) -> None:
    store = InMemoryConversationStore()
    conv = _meeting(store, lines=("hello there", "   ", "let's ship it"))
    hub = Hub(_ok())
    d = _delivery(store, hub, clock)

    assert asyncio.run(d.run_once()) == 1
    assert asyncio.run(d.run_once()) == 0  # sent: never posted again

    (req,) = hub.requests
    assert str(req.url) == "http://hub/api/orgs/acme.atlassian.net/meetings"
    assert req.headers["authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body == {
        "meetingId": "c1",
        "startedAt": int(conv.started_at.timestamp() * 1000),
        "endedAt": int(conv.ended_at.timestamp() * 1000),
        "participants": ["Ana", "Bo"],
        "segments": [
            {"text": "hello there", "startMs": 0, "endMs": 900, "speaker": "Ana"},
            {"text": "let's ship it", "startMs": 2000, "endMs": 2900, "speaker": "Ana"},
        ],
        "title": "Weekly sync",
        "platform": "zoom",
    }
    got = store.get("default", "c1")
    assert (got.delivery_state, got.delivery_error) == ("sent", None)


def test_a_live_meeting_or_a_mic_conversation_is_never_posted(clock) -> None:
    store = InMemoryConversationStore()
    store.create("default", "live", screen=ScreenMeta(SITE, "Acme", "zoom"))
    store.add_segment("default", "live", Segment("s0", "hi", 0, 1))
    store.create("default", "mic")
    store.add_segment("default", "mic", Segment("s0", "hi", 0, 1))
    store.finish("default", "mic")
    hub = Hub(_ok())
    assert asyncio.run(_delivery(store, hub, clock).run_once()) == 0
    assert hub.requests == []
    assert store.get("default", "mic").delivery_state is None
    store.set_delivery("default", "mic", "sent", None)  # a mic row never gets a state
    assert store.get("default", "mic").delivery_state is None


def test_a_hub_restart_mid_delivery_is_retried_with_backoff_and_lands_once(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    # The connection drops after the hub kept it, then the restarted hub is still
    # re-registering its hosts (404), then it answers the repeat as a duplicate.
    hub = Hub(
        httpx.RemoteProtocolError("server disconnected"),
        httpx.Response(404, json={"error": "no host is in that org"}),
        _ok(duplicate=True),
    )
    d = _delivery(store, hub, clock)

    assert asyncio.run(d.run_once()) == 0
    got = store.get("default", "c1")
    assert got.delivery_state == "pending"
    assert got.delivery_error == "hub unreachable (RemoteProtocolError)"
    assert asyncio.run(d.run_once()) == 0  # inside the 5 s backoff: not tried
    assert len(hub.requests) == 1

    clock["now"] += 5
    assert asyncio.run(d.run_once()) == 0
    assert store.get("default", "c1").delivery_error == "hub answered 404: no host is in that org"
    clock["now"] += 5
    assert asyncio.run(d.run_once()) == 0  # backoff doubled to 10 s
    clock["now"] += 5
    assert asyncio.run(d.run_once()) == 1
    assert len(hub.requests) == 3
    assert {json.loads(r.content)["meetingId"] for r in hub.requests} == {"c1"}
    assert store.get("default", "c1").delivery_state == "sent"


def test_backoff_is_capped(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(httpx.Response(503, json={"error": "not configured"}))
    d = _delivery(store, hub, clock, retry_max_seconds=20)
    for _ in range(6):
        asyncio.run(d.run_once())
        clock["now"] += 20
    assert len(hub.requests) == 6  # 5, 10, 20, 20, 20: every 20 s pass tries again


@pytest.mark.parametrize("status", [400, 401, 403, 413])
def test_a_refusal_waiting_cannot_fix_fails_with_the_hubs_words(clock, status) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(httpx.Response(status, json={"error": "nope"}))
    d = _delivery(store, hub, clock)
    asyncio.run(d.run_once())
    clock["now"] += 3600
    asyncio.run(d.run_once())
    assert len(hub.requests) == 1
    got = store.get("default", "c1")
    assert (got.delivery_state, got.delivery_error) == ("failed", f"hub answered {status}: nope")


def test_a_200_that_is_not_the_hubs_ok_is_retried(clock) -> None:
    # A proxy's or login page's 200 must not mark a meeting sent that never arrived.
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(httpx.Response(200, text="<html>sign in</html>"), _ok())
    d = _delivery(store, hub, clock)
    asyncio.run(d.run_once())
    assert store.get("default", "c1").delivery_state == "pending"
    assert store.get("default", "c1").delivery_error == "hub answered 200"
    clock["now"] += 5
    assert asyncio.run(d.run_once()) == 1


def test_retries_give_up_a_day_after_the_meeting_ended(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(httpx.ConnectError("refused"))
    d = _delivery(store, hub, clock, give_up_seconds=600)
    asyncio.run(d.run_once())
    assert store.get("default", "c1").delivery_state == "pending"
    clock["now"] += 601
    asyncio.run(d.run_once())
    got = store.get("default", "c1")
    assert got.delivery_state == "failed"
    assert got.delivery_error == "gave up retrying: hub unreachable (ConnectError)"


def test_a_meeting_with_no_caption_text_fails_without_a_post(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store, lines=("  ",))
    hub = Hub(_ok())
    asyncio.run(_delivery(store, hub, clock).run_once())
    assert hub.requests == []
    got = store.get("default", "c1")
    assert (got.delivery_state, got.delivery_error) == ("failed", "no caption lines to send")


def test_body_respects_the_hubs_caps() -> None:
    store = InMemoryConversationStore()
    conv = _meeting(store)
    conv.meeting_title = "t" * 400
    conv.platform = "p" * 50
    conv.participants = [f"P{i}" for i in range(150)]
    conv.segments = [
        Segment(f"s{i}", "x" * 2100 if i == 0 else "x", i, i + 1) for i in range(10_050)
    ]
    body, total = turma.meeting_body(conv)
    assert total == 10_050
    assert len(body["title"]) == 300 and len(body["platform"]) == 40
    assert len(body["participants"]) == 100 and len(body["segments"]) == 10_000
    assert len(body["segments"][0]["text"]) == 2000
    assert "speaker" not in body["segments"][0]


def test_a_meeting_past_the_hubs_byte_limit_sends_what_fits_and_says_so(clock) -> None:
    # QA: 9000 lines x 130 chars was a 413, failed forever.
    store = InMemoryConversationStore()
    _meeting(store, lines=[f"{i:05d} " + "é" * 124 for i in range(9000)])
    hub = Hub(_ok())
    assert asyncio.run(_delivery(store, hub, clock).run_once()) == 1
    (req,) = hub.requests
    assert len(req.content) <= 1024 * 1024
    assert req.headers["content-type"] == "application/json"
    sent = json.loads(req.content)["segments"]
    assert 0 < len(sent) < 9000 and sent[0]["text"].startswith("00000")
    got = store.get("default", "c1")
    assert got.delivery_state == "sent"
    assert got.delivery_error == (
        f"sent the first {len(sent)} of 9000 lines: the rest is past the hub's limit"
    )


def _hub_counted_bytes(body: dict) -> int:
    """The hub's per-org transcript measure: JSON of its sanitized segments, where every
    line carries a speaker (empty when none) (Turma server.js meetingSegmentBytes)."""
    segs = [{"speaker": s.get("speaker", ""), **s} for s in body["segments"]]
    return len(json.dumps(segs, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("line", ["a" * 130, "日本語" * 166])
def test_a_big_meeting_fits_the_hubs_per_org_transcript_budget(clock, line) -> None:
    # Delta QA: fitted to the 1 MiB body alone, 9000 speaker-less lines measured 1.12 MB by
    # the hub's count, so it answered 429 for a day.
    store = InMemoryConversationStore()
    store.create("default", "big", screen=ScreenMeta(SITE, "Acme", "zoom"))
    for i in range(9000):
        store.add_segment("default", "big", Segment(f"s{i}", line, i, i + 1))
    store.finish("default", "big")
    body, total = turma.meeting_body(store.get("default", "big"))
    assert total == 9000 and len(body["segments"]) < 9000
    assert _hub_counted_bytes(body) <= 1024 * 1024 - 1024
    assert len(turma._encode(body)) <= 1024 * 1024


def test_multibyte_text_goes_out_unescaped(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store, lines=("café 日本",))
    hub = Hub(_ok())
    asyncio.run(_delivery(store, hub, clock).run_once())
    assert "café 日本".encode() in hub.requests[0].content


def test_a_meeting_whose_tail_is_cut_by_the_line_cap_says_so(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store, lines=["x"] * 10_001)
    asyncio.run(_delivery(store, Hub(_ok()), clock).run_once())
    assert store.get("default", "c1").delivery_error == (
        "sent the first 10000 of 10001 lines: the rest is past the hub's limit"
    )


def test_a_json_200_without_ok_is_retried(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(httpx.Response(200, json={"meeting": {}}))
    asyncio.run(_delivery(store, hub, clock).run_once())
    assert store.get("default", "c1").delivery_state == "pending"


def test_one_orgs_stuck_backlog_never_starves_another_org(clock) -> None:
    # QA: 20 meetings of an org the hub 404s hid every later meeting for a day.
    store = InMemoryConversationStore()
    for i in range(25):
        _meeting(store, f"stuck{i}")
    store.create("default", "other", screen=ScreenMeta("other.atlassian.net", "O", "zoom"))
    store.add_segment("default", "other", Segment("o0", "hi", 0, 1))
    store.finish("default", "other")

    def hub(request: httpx.Request) -> httpx.Response:
        hub.paths.append(request.url.path)
        if SITE in request.url.path:
            return httpx.Response(404, json={"error": "no host is in that org"})
        return _ok()

    hub.paths = []
    d = _delivery(store, hub, clock)
    reads = []
    real_get = store.get
    store.get = lambda *a, **k: reads.append(a[1]) or real_get(*a, **k)  # type: ignore[method-assign]
    assert asyncio.run(d.run_once()) == 1
    assert len(reads) == 2  # a deferred org's backlog is skipped before it is loaded
    assert store.get("default", "other").delivery_state == "sent"
    # The refused org was asked once this pass, not 25 times.
    assert hub.paths.count(f"/api/orgs/{SITE}/meetings") == 1
    clock["now"] += 1
    asyncio.run(d.run_once())  # the next stuck meeting (not backing off) gets its turn
    assert hub.paths.count(f"/api/orgs/{SITE}/meetings") == 2


def test_an_unreachable_hub_ends_the_pass(clock) -> None:
    store = InMemoryConversationStore()
    for i in range(5):
        _meeting(store, f"m{i}")
    hub = Hub(httpx.ConnectTimeout("timed out"))
    asyncio.run(_delivery(store, hub, clock).run_once())
    assert len(hub.requests) == 1  # not one 30 s timeout per meeting


def test_a_screen_meeting_from_before_delivery_existed_is_sent(clock) -> None:
    # QA: a meeting live across the upgrade had no state and was never posted.
    store = InMemoryConversationStore()
    _meeting(store)
    store._by_household["default"]["c1"].delivery_state = None
    assert store.delivery_summary(utcnow() - timedelta(days=1))["pending"] == 1
    assert asyncio.run(_delivery(store, Hub(_ok()), clock).run_once()) == 1
    assert store.get("default", "c1").delivery_state == "sent"


def test_a_database_outage_does_not_kill_the_loop(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    hub = Hub(_ok())
    d = _delivery(store, hub, clock)
    calls = {"n": 0}
    real = store.pending_deliveries

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return real(*a, **k)

    store.pending_deliveries = flaky  # type: ignore[method-assign]

    async def run() -> None:
        task = asyncio.create_task(d.loop(interval=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if store.get("default", "c1").delivery_state == "sent":
                break
        task.cancel()

    asyncio.run(run())
    assert store.get("default", "c1").delivery_state == "sent"


def _run_loop_until(d, done) -> None:
    async def run() -> None:
        task = asyncio.create_task(d.loop(interval=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if done():
                break
        task.cancel()

    asyncio.run(run())


def test_meetings_failed_on_a_wrong_token_are_resent_after_a_restart(clock) -> None:
    # XERK-2605: the QA repro. A wrong token fails every meeting 401; the operator fixes the
    # token and restarts, and the new process's loop re-queues and delivers them.
    store = InMemoryConversationStore()
    for cid in ("a", "b", "c"):
        _meeting(store, cid)
    bad = _delivery(store, Hub(httpx.Response(401, json={"error": "unauthorized"})), clock)
    asyncio.run(bad.run_once())
    assert {store.get("default", c).delivery_state for c in "abc"} == {"failed"}

    hub = Hub(_ok())
    fixed = _delivery(store, hub, clock)
    _run_loop_until(
        fixed, lambda: all(store.get("default", c).delivery_state == "sent" for c in "abc")
    )
    assert {store.get("default", c).delivery_state for c in "abc"} == {"sent"}
    assert len(hub.requests) == 3


@pytest.mark.parametrize(
    "reason, requeued",
    [
        ("hub answered 401: unauthorized", True),
        ("hub answered 403", True),
        ("gave up retrying: hub answered 503", True),
        ("hub answered 400: bad title", False),
        ("hub answered 413", False),
        ("no caption lines to send", False),
        ("the first caption line is past the hub's limit", False),
    ],
)
def test_only_failures_a_config_fix_can_change_are_requeued(clock, reason, requeued) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    store.set_delivery("default", "c1", "failed", reason)
    d = _delivery(store, Hub(_ok()), clock)
    assert asyncio.run(d.requeue_failed()) == int(requeued)
    assert store.get("default", "c1").delivery_state == ("pending" if requeued else "failed")


def test_requeue_never_touches_sent_or_mic_rows(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store, "sent")
    store.set_delivery("default", "sent", "sent", "hub answered 401")
    store.create("default", "mic", owner="u1")
    store.finish("default", "mic")
    # set_delivery refuses a mic row: stamp it directly, as a bad migration might.
    mic = store._by_household["default"]["mic"]
    mic.delivery_state, mic.delivery_error = "failed", "hub answered 401"
    assert store.requeue_failed_deliveries(turma.REQUEUE_ON_BOOT) == []
    assert store.get("default", "sent").delivery_state == "sent"
    assert store.get("default", "mic").delivery_state == "failed"


def test_the_written_failure_reasons_match_the_requeue_list(clock) -> None:
    # The re-queue matches on the reason text: pin it to what _deliver really writes.
    store = InMemoryConversationStore()
    _meeting(store, "tok")
    asyncio.run(_delivery(store, Hub(httpx.Response(403)), clock).run_once())
    _meeting(store, "old")
    clock["now"] += 2 * 86_400
    asyncio.run(_delivery(store, Hub(httpx.Response(503)), clock).run_once())
    assert store.get("default", "old").delivery_error.startswith(turma.GIVE_UP_PREFIX)
    assert len(store.requeue_failed_deliveries(turma.REQUEUE_ON_BOOT)) == 2


def test_a_requeued_gave_up_meeting_gets_a_fresh_retry_window(clock) -> None:
    # QA of XERK-2605: re-queued after a cluster restart, a meeting days past its give-up
    # window must ride out the hub's 404s while its hosts re-register, not fail at the first.
    store = InMemoryConversationStore()
    _meeting(store)
    clock["now"] += 3 * 86_400
    store.set_delivery("default", "c1", "failed", "gave up retrying: hub answered 503")
    hub = Hub(httpx.Response(404, json={"error": "no host is in that org"}), _ok())
    d = _delivery(store, hub, clock)
    assert asyncio.run(d.requeue_failed()) == 1
    asyncio.run(d.run_once())
    assert store.get("default", "c1").delivery_state == "pending"
    clock["now"] += 10
    assert asyncio.run(d.run_once()) == 1
    assert store.get("default", "c1").delivery_state == "sent"


def test_a_requeued_meeting_still_gives_up_a_window_after_the_boot(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    clock["now"] += 3 * 86_400
    store.set_delivery("default", "c1", "failed", "gave up retrying: hub answered 503")
    d = _delivery(store, Hub(httpx.Response(503)), clock, give_up_seconds=60)
    asyncio.run(d.requeue_failed())
    asyncio.run(d.run_once())
    assert store.get("default", "c1").delivery_state == "pending"
    clock["now"] += 61
    asyncio.run(d.run_once())
    assert store.get("default", "c1").delivery_error.startswith(turma.GIVE_UP_PREFIX)


def test_a_still_wrong_token_fails_the_requeued_meetings_again(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)
    store.set_delivery("default", "c1", "failed", "hub answered 401: unauthorized")
    hub = Hub(httpx.Response(401, json={"error": "unauthorized"}))
    d = _delivery(store, hub, clock)
    _run_loop_until(d, lambda: len(hub.requests) >= 1)
    assert store.get("default", "c1").delivery_state == "failed"
    assert len(hub.requests) == 1  # once per boot, not a hot loop


def test_a_database_outage_during_requeue_does_not_kill_the_loop(clock) -> None:
    store = InMemoryConversationStore()
    _meeting(store)

    def down(*a, **k):
        raise RuntimeError("db down")

    store.requeue_failed_deliveries = down  # type: ignore[method-assign]
    d = _delivery(store, Hub(_ok()), clock)
    _run_loop_until(d, lambda: store.get("default", "c1").delivery_state == "sent")
    assert store.get("default", "c1").delivery_state == "sent"


def test_a_tracker_ending_a_meeting_wakes_the_delivery(monkeypatch, clock) -> None:
    store = InMemoryConversationStore()
    hub = Hub(_ok())

    async def run() -> None:
        d = _delivery(store, hub, clock)
        monkeypatch.setattr(turma, "_delivery", d)
        task = asyncio.create_task(d.loop(interval=3600))  # only a wake can trigger it
        tracker = MeetingTracker(
            "default",
            "u1",
            ScreenMeta(SITE, "Acme", "zoom"),
            store=store,
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        await tracker.on_caption([CaptionBlock(None, False, ["hello there"])], 1000)
        await asyncio.sleep(0.05)
        assert hub.requests == []  # still live
        await tracker.end(70_000)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if hub.requests:
                break
        task.cancel()

    asyncio.run(run())
    assert len(hub.requests) == 1


def test_start_needs_both_url_and_token(monkeypatch, caplog) -> None:
    store = InMemoryConversationStore()
    monkeypatch.setattr(settings, "turma_url", "http://hub")
    monkeypatch.setattr(settings, "turma_meetings_token", "")
    with caplog.at_level("ERROR", logger="api.screen.turma"):
        assert turma.start(store) is None and turma.get_delivery() is None
    assert "API_TURMA_MEETINGS_TOKEN is not set" in caplog.text
    turma.wake()  # a no-op when off
    monkeypatch.setattr(settings, "turma_url", "")
    monkeypatch.setattr(settings, "turma_meetings_token", "tok")
    caplog.clear()
    with caplog.at_level("ERROR", logger="api.screen.turma"):
        assert turma.start(store) is None
    assert "API_TURMA_URL is not set" in caplog.text
    for bad in ("turma.example:8300", "http://", "ftp://hub"):
        monkeypatch.setattr(settings, "turma_url", bad)
        caplog.clear()
        with caplog.at_level("ERROR", logger="api.screen.turma"):
            assert turma.start(store) is None
        assert "must be an http(s) URL" in caplog.text
    assert "tok" not in caplog.text
    monkeypatch.setattr(settings, "turma_url", "http://hub")
    assert turma.start(None) is None
    d = turma.start(store)
    assert d is not None and turma.get_delivery() is d and d.url == "http://hub"
    asyncio.run(d.aclose())
    monkeypatch.setattr(turma, "_delivery", None)


# ---- /status ------------------------------------------------------------------------------


@pytest.fixture
def delivery_on(monkeypatch):
    store = InMemoryConversationStore()
    monkeypatch.setattr(settings, "turma_url", "http://hub")
    monkeypatch.setattr(settings, "turma_meetings_token", "tok")
    monkeypatch.setattr(st, "get_conversation_store", lambda: store)
    monkeypatch.setattr(settings, "status_grace_seconds", 0.0)
    st.reset()
    yield store
    st.reset()


def _turma_component():
    (c,) = [c for c in asyncio.run(st.refresh()) if c.id == "turma"]
    return c


def test_status_shows_delivery_green_yellow_red(delivery_on) -> None:
    store = delivery_on
    assert _turma_component().state == st.STATE_READY
    _meeting(store, "a")
    c = _turma_component()
    assert (c.state, c.detail) == (st.STATE_CONNECTING, "1 meeting(s) waiting to be delivered")
    store.set_delivery("default", "a", "failed", "hub answered 401: unauthorized")
    c = _turma_component()
    assert c.state == st.STATE_DOWN
    assert c.detail == "1 meeting(s) not delivered: hub answered 401: unauthorized"
    assert "Weekly sync" not in json.dumps(st.snapshot())  # /status is public


def test_status_forgets_failures_older_than_the_window(delivery_on) -> None:
    store = delivery_on
    _meeting(store, "a")
    store.set_delivery("default", "a", "failed", "x")
    store._by_household["default"]["a"].ended_at = utcnow() - timedelta(days=8)
    assert _turma_component().state == st.STATE_READY


def test_status_reports_an_unreadable_delivery_summary(delivery_on, monkeypatch) -> None:
    def boom(_since):
        raise RuntimeError("db down")

    monkeypatch.setattr(delivery_on, "delivery_summary", boom)
    assert _turma_component().state == st.STATE_DOWN


def test_status_has_no_turma_light_when_delivery_is_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "turma_url", "")
    assert all(c.id != "turma" for c in asyncio.run(st.refresh()))


@pytest.mark.parametrize(
    ("url", "token", "detail"),
    [
        ("http://hub", "", "API_TURMA_MEETINGS_TOKEN is not set"),
        ("", "tok", "API_TURMA_URL is not set"),
        ("http://", "tok", "API_TURMA_URL must be an http(s) URL"),
    ],
)
def test_status_shows_a_delivery_config_error_red_at_once(monkeypatch, url, token, detail):
    # Delta QA: yellow for the 180 s grace window, and a hostless URL showed no reason.
    monkeypatch.setattr(settings, "turma_url", url)
    monkeypatch.setattr(settings, "turma_meetings_token", token)
    monkeypatch.setattr(settings, "status_grace_seconds", 180.0)
    (c,) = [c for c in asyncio.run(st.refresh()) if c.id == "turma"]
    assert (c.state, c.detail) == (st.STATE_DOWN, detail)
    assert not turma.configured()
