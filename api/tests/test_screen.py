"""Screen-captured meeting captions (XERK-2505): Zoom parser, rolling-caption merge, the
/ws/screen socket, its device-token auth, and the replayed-meeting acceptance test."""

from __future__ import annotations

import asyncio
import io
import json
import struct
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from starlette.websockets import WebSocketDisconnect

from api import history
from api.config import settings
from api.metrics import metrics
from api.persistence import InMemoryConversationStore, ScreenMeta
from api.persistence.models import alias_key
from api.screen import ws as screen_ws
from api.screen.meetings import Cadence, MeetingTracker, meetings
from api.screen.merge import CaptionMerger
from api.screen.ocr import HttpOcrClient, OcrError, OcrLine, OcrResult, parse_result
from api.screen.zoom import (
    CaptionBlock,
    Roster,
    clean_tile_name,
    is_name_like,
    parse_caption_crop,
    parse_full_frame,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/screen/zoom_meeting.json").read_text())


# --- helpers -------------------------------------------------------------------------------


def ocr_of(frame: dict) -> OcrResult:
    return parse_result(
        {"width": frame["width"], "height": frame["height"], "lines": frame["lines"]}
    )


def render_full(frame: dict) -> bytes:
    """A dark Zoom-like window with the fixture's active tile ringed in Zoom green."""
    img = Image.new("RGB", (frame["width"], frame["height"]), (26, 26, 26))
    draw = ImageDraw.Draw(img)
    if frame.get("activeTile"):
        draw.rectangle(frame["activeTile"], outline=(35, 217, 89), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    return buf.getvalue()


def envelope(header: dict, jpeg: bytes) -> bytes:
    raw = json.dumps(header).encode()
    return struct.pack(">I", len(raw)) + raw + jpeg


def header(ts: int, region: str, site: str = "tesoro") -> dict:
    return {
        "type": "screen.frame",
        "windowTitle": "Horizon desktop 1",
        "orgSiteKey": site,
        "orgName": "Tesoro",
        "platform": "zoom",
        "ts": ts,
        "region": region,
    }


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> OcrLine:
    return OcrLine(text, (x0, y0, x1, y1))


def crop(*rows: str) -> OcrResult:
    return OcrResult(
        700,
        30 * len(rows),
        tuple(line(r, 10, 30 * i, 10 + 9 * len(r), 30 * i + 20) for i, r in enumerate(rows)),
    )


class StubOcr:
    """tenir-ocr stand-in: answers each frame with the fixture OCR for its header ts."""

    def __init__(self) -> None:
        self.by_jpeg: dict[bytes, OcrResult] = {}
        self.fail = False

    def register(self, jpeg: bytes, result: OcrResult) -> bytes:
        self.by_jpeg[jpeg] = result
        return jpeg

    async def read(self, jpeg: bytes) -> OcrResult:
        if self.fail:
            raise OcrError("down")
        return self.by_jpeg[jpeg]


def unique_jpeg(n: int) -> bytes:
    """A distinct valid-looking JPEG per frame, so the stub can key on the bytes."""
    return b"\xff\xd8" + n.to_bytes(4, "big") + b"\xff\xd9"


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch):
    """In-memory store + stub OCR wired into /ws/screen; returns (store, ocr, device token)."""
    from api.auth import Principal, get_user_store, issue_token

    store = InMemoryConversationStore()
    ocr = StubOcr()
    monkeypatch.setattr(screen_ws, "get_conversation_store", lambda: store)
    monkeypatch.setattr(screen_ws, "get_ocr_client", lambda: ocr)
    monkeypatch.setattr(history, "get_conversation_store", lambda: store)
    meetings.clear()
    metrics.reset()
    admin = get_user_store().get_by_username("test-admin")
    token = issue_token(
        Principal(admin.user_id, admin.household, admin.role, admin.username, scope="screen"),
        secret=settings.auth_secret,
        ttl_seconds=3600,
    )
    yield store, ocr, token, admin
    meetings.clear()


def wait_handled(n: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while metrics.snapshot()["counters"].get("screen.frames_handled", 0) < n:
        if time.monotonic() > deadline:
            raise AssertionError(f"only {metrics.snapshot()['counters']} frames handled")
        time.sleep(0.01)


# --- name helpers / roster -----------------------------------------------------------------


def test_tile_names_lose_role_and_pronoun_suffixes() -> None:
    assert clean_tile_name("Priya Raman (Host)") == "Priya Raman"
    assert clean_tile_name("Ana Lopez (she/her) (Co-host)") == "Ana Lopez"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Priya Raman", True),
        ("J. Smith", True),
        ("Ana de la Cruz", True),
        ("kunal@tesoroxp.com", True),
        ("and add it to the sprint", False),
        ("Sounds good, thanks", False),
        ("Priya:", False),
        ("", False),
        ("One Two Three Four Five Six", False),
    ],
)
def test_name_shape(text: str, expected: bool) -> None:
    assert is_name_like(text) is expected


def test_roster_matches_exact_fuzzy_and_aliases() -> None:
    roster = Roster({alias_key("KUNAL@tesoroxp.com "): "Kunal"})
    assert roster.add("Priya Raman") == "Priya Raman"
    assert roster.add("priya raman") is None  # same person, already there
    assert roster.add("kunal@tesoroxp.com") == "Kunal"
    assert roster.match("PRIYA RAMAN") == "Priya Raman"
    assert roster.match("Priya Rarnan") == "Priya Raman"  # OCR rn/m slip
    assert roster.match("kunal@tesoroxp.com") == "Kunal"
    assert roster.match("Kunal") is None or roster.match("Kunal") == "Kunal"
    assert roster.match("Someone Else") is None
    assert roster.match("") is None
    assert "Priya Raman" in roster and len(roster) == 2


def test_ambiguous_header_resolves_only_to_the_active_speaker() -> None:
    roster = Roster()
    roster.add("Chris Jones")
    roster.add("Chris Jonas")
    assert roster.match("Chris Jonts") is None
    assert roster.match("Chris Jonts", prefer="Chris Jonas") == "Chris Jonas"


# --- full frames ---------------------------------------------------------------------------


def test_full_frame_reads_tiles_title_and_active_border() -> None:
    frame = FIXTURE["frames"][0]
    out = parse_full_frame(ocr_of(frame), render_full(frame))
    assert out.participants == ["Priya Raman", "Sean O'Neil", "kunal@tesoroxp.com"]
    assert out.title == "Q3 Release Sync"
    assert out.active_speaker == "Priya Raman"


def test_full_frame_skips_the_caption_box_and_toolbar() -> None:
    frame = FIXTURE["frames"][10]
    out = parse_full_frame(ocr_of(frame), render_full(frame))
    assert out.participants == ["Priya Raman", "Sean O'Neil", "kunal@tesoroxp.com"]
    assert out.active_speaker == "kunal@tesoroxp.com"


def test_no_border_or_bad_image_means_no_active_speaker() -> None:
    frame = dict(FIXTURE["frames"][0], activeTile=None)
    assert parse_full_frame(ocr_of(frame), render_full(frame)).active_speaker is None
    assert parse_full_frame(ocr_of(frame), b"\xff\xd8junk").active_speaker is None
    assert parse_full_frame(ocr_of(frame)).active_speaker is None


def test_two_bordered_tiles_are_no_clear_winner() -> None:
    frame = FIXTURE["frames"][0]
    img = Image.open(io.BytesIO(render_full(frame)))
    ImageDraw.Draw(img).rectangle([440, 40, 840, 260], outline=(35, 217, 89), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    assert parse_full_frame(ocr_of(frame), buf.getvalue()).active_speaker is None


def test_generic_zoom_chrome_is_not_a_title() -> None:
    result = OcrResult(1280, 720, (line("Zoom Meeting", 560, 6, 700, 26),))
    assert parse_full_frame(result).title is None


# --- caption crops -------------------------------------------------------------------------


def _roster(*names: str) -> Roster:
    r = Roster()
    for n in names:
        r.add(n)
    return r


def test_caption_headers_must_match_a_tile_name() -> None:
    blocks = parse_caption_crop(
        crop("Priya Raman", "we ship Friday", "Bob Stranger", "no we don't"),
        _roster("Priya Raman"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["we ship Friday", "Bob Stranger", "no we don't"])
    ]


def test_inline_name_colon_headers() -> None:
    blocks = parse_caption_crop(
        crop("Priya Raman: we ship Friday", "Sean O'Neil: agreed"),
        _roster("Priya Raman", "Sean O'Neil"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["we ship Friday"]),
        ("Sean O'Neil", ["agreed"]),
    ]


def test_headerless_lines_continue_the_block_above_the_crop() -> None:
    blocks = parse_caption_crop(
        crop("still talking here", "Sean O'Neil", "ok"), _roster("Sean O'Neil")
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        (None, False, ["still talking here"]),
        ("Sean O'Neil", True, ["ok"]),
    ]


def test_without_a_roster_a_name_line_is_an_unverified_header() -> None:
    blocks = parse_caption_crop(crop("Priya Raman", "we ship Friday"), Roster())
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        (None, True, ["we ship Friday"])
    ]


def test_ocr_pieces_on_one_row_are_joined_and_noise_dropped() -> None:
    result = OcrResult(
        700,
        60,
        (
            line("timeout", 386, 35, 460, 54),
            line("for the login", 10, 34, 380, 54),
            line("~", 600, 2, 610, 20),
        ),
    )
    blocks = parse_caption_crop(result, Roster())
    assert blocks[0].lines == ["for the login timeout"]


# --- merge ---------------------------------------------------------------------------------


def blk(speaker: str | None, *lines: str, header: bool = True) -> CaptionBlock:
    return CaptionBlock(speaker, header, list(lines))


def test_growing_line_is_stored_once_when_stable() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    assert m.feed([blk("A", "we need")], 1000) == []
    assert m.feed([blk("A", "we need the fix")], 2000) == []
    assert m.feed([blk("A", "we need the fix")], 4000) == []
    out = m.feed([blk("A", "we need the fix")], 5000)
    assert [(s.speaker, s.text, s.start_ms, s.end_ms) for s in out] == [
        ("A", "we need the fix", 1000, 2000)
    ]
    # Still on screen afterwards: never stored again.
    assert m.feed([blk("A", "we need the fix")], 9000) == []
    assert m.flush(10_000) == []


def test_new_speaker_block_finalizes_the_lines_above_it() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=60_000)
    m.feed([blk("A", "first line")], 0)
    out = m.feed([blk("A", "first line"), blk("B", "reply")], 500)
    assert [s.text for s in out] == ["first line"]
    assert [s.text for s in m.flush(600)] == ["reply"]


def test_line_that_grows_after_it_was_final_extends_the_same_segment() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "let's ship")], 0)
    (seg,) = m.feed([blk("A", "let's ship")], 1000)
    (ext,) = m.feed([blk("A", "let's ship it Friday")], 2000)
    assert ext.segment_id == seg.segment_id and ext.text == "let's ship it Friday"
    assert m.flush(3000) == []


def test_a_line_said_again_is_stored_again() -> None:
    """QA D2: "okay", someone else, "okay" - both shown at once, then the same words again
    after the first scrolled away. Every utterance is stored; re-reads of one are not."""
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "okay")], 0)
    assert [s.text for s in m.feed([blk("A", "okay")], 1000)] == ["okay"]
    out = m.feed([blk("A", "okay"), blk("B", "something else entirely"), blk("A", "okay")], 1500)
    assert [s.text for s in out] == ["something else entirely"]
    assert [(s.speaker, s.text) for s in m.flush(1700)] == [("A", "okay")]
    # Gone from the screen (no snapshot shows it for 20 s), then said again: a new line.
    m.feed([blk("A", "let me share my screen now")], 2000)
    (first,) = m.feed([blk("A", "let me share my screen now")], 3000)
    m.feed([blk("B", "go ahead")], 4000)
    m.feed([blk("A", "let me share my screen now")], 25_000)
    (again,) = m.flush(26_000)
    assert again.text == first.text and again.segment_id != first.segment_id


def test_long_lines_partly_scrolled_are_not_stored_again() -> None:
    m2 = CaptionMerger(start_ms=0, stable_ms=1000)
    m2.feed([blk("A", "the deployment is blocked on review")], 0)
    m2.feed([blk("A", "the deployment is blocked on review")], 1000)
    m2.feed([blk("A", "deployment is blocked")], 1100)
    assert m2.flush(5000) == []


def test_headerless_continuation_takes_the_last_speaker() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "hello there")], 0)
    m.feed([blk(None, "and another thing", header=False)], 100)
    out = m.flush(200)
    assert {(s.speaker, s.text) for s in out} == {("A", "hello there"), ("A", "and another thing")}


# --- OCR client ----------------------------------------------------------------------------


def test_parse_result_rejects_garbage() -> None:
    with pytest.raises(OcrError):
        parse_result([])
    with pytest.raises(OcrError):
        parse_result({"width": 1, "height": 1, "lines": [{"text": "x", "box": [1, 2]}]})
    got = parse_result({"width": 4, "height": 4, "lines": [{"text": "  ", "box": [0, 0, 1, 1]}]})
    assert got.lines == ()


def test_http_ocr_client_posts_the_jpeg() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.content
        if request.content == b"bad":
            return httpx.Response(500)
        if request.content == b"html":
            return httpx.Response(200, text="<html>")
        return httpx.Response(
            200,
            json={
                "width": 10,
                "height": 5,
                "lines": [{"text": "hi", "box": [0, 0, 5, 5], "score": 0.9}],
            },
        )

    async def run() -> None:
        client = HttpOcrClient("http://ocr.test/", timeout=1)
        client._client = httpx.AsyncClient(
            base_url="http://ocr.test", transport=httpx.MockTransport(handler)
        )
        got = await client.read(b"\xff\xd8x")
        assert got.lines[0].text == "hi" and got.lines[0].score == 0.9
        for body in (b"bad", b"html"):
            with pytest.raises(OcrError):
                await client.read(body)
        await client.aclose()

    asyncio.run(run())
    assert seen["path"] == "/v1/ocr" and seen["type"] == "image/jpeg"


def test_ocr_client_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen_ws, "_ocr", None)
    monkeypatch.setattr(settings, "ocr_url", "")
    assert screen_ws.get_ocr_client() is None
    monkeypatch.setattr(settings, "ocr_url", "http://tenir-ocr:8000")
    assert isinstance(screen_ws.get_ocr_client(), HttpOcrClient)
    monkeypatch.setattr(screen_ws, "_ocr", None)


# --- frame envelope ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"\x00", "shorter"),
        (struct.pack(">I", 0) + b"xx", "bad header length"),
        (struct.pack(">I", 99) + b"{}", "bad header length"),
        (envelope({"type": "screen.frame"}, tiny_jpeg()), "bad frame header"),
        (envelope(header(1, "caption"), b"PNG..."), "not a JPEG"),
    ],
)
def test_bad_frames_are_refused(data: bytes, error: str) -> None:
    with pytest.raises(screen_ws.FrameError, match=error):
        screen_ws.split_frame(data)


# --- device tokens -------------------------------------------------------------------------


@pytest.mark.real_auth
def test_device_token_is_minted_for_the_caller_and_scoped_to_the_screen_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.auth import get_user_store, reset_user_store
    from api.auth.tokens import renew_token_if_due
    from api.main import app

    reset_user_store()
    get_user_store().create("op", "op-password-123", household="default", role="member")
    monkeypatch.setattr(screen_ws, "get_ocr_client", lambda: StubOcr())
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": "op", "password": "op-password-123"})
        bearer = {"Authorization": f"Bearer {login.json()['token']}"}
        assert client.post("/auth/device-token").status_code == 401
        minted = client.post("/auth/device-token", headers=bearer).json()
        assert (
            minted["scope"] == "screen"
            and minted["expiresIn"] == settings.auth_device_token_ttl_seconds
        )
        device = minted["token"]
        # Refused on every normal route, and never renewed into a login token.
        assert (
            client.get("/conversations", headers={"Authorization": f"Bearer {device}"}).status_code
            == 401
        )
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {device}"}).status_code == 401
        )
        assert (
            renew_token_if_due(
                device,
                secret=settings.auth_secret,
                ttl_seconds=minted["expiresIn"],
                # Past half its life: a login token would renew here (QA D5).
                now=time.time() + 0.6 * minted["expiresIn"],
            )
            is None
        )
        # The login token can't open the screen socket; the device token can.
        for token in (login.json()["token"], "nope", ""):
            with client.websocket_connect(f"/ws/screen?token={token}") as ws:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == 1008
        with client.websocket_connect(
            "/ws/screen", headers={"Authorization": f"Bearer {device}"}
        ) as ws:
            assert json.loads(ws.receive_text())["type"] == "screen.cadence"
    reset_user_store()


@pytest.mark.real_auth
def test_oidc_tokens_cannot_open_the_screen_socket() -> None:
    from api.auth import AuthError, principal_from_bearer

    with pytest.raises(AuthError, match="device token"):
        principal_from_bearer("a.b.c", scope="screen")


@pytest.mark.real_auth
def test_device_token_of_a_deleted_user_is_refused() -> None:
    from api.auth import AuthError, Principal, issue_token, principal_from_bearer

    token = issue_token(
        Principal("gone", "default", scope="screen"), secret=settings.auth_secret, ttl_seconds=60
    )
    with pytest.raises(AuthError, match="no longer exists"):
        principal_from_bearer(token, scope="screen")


# --- the socket ----------------------------------------------------------------------------


def test_socket_refuses_when_ocr_or_persistence_is_off(
    screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.main import app

    _, _, token, _ = screen
    with TestClient(app) as client:
        for target in ("get_ocr_client", "get_conversation_store"):
            with monkeypatch.context() as m:
                m.setattr(screen_ws, target, lambda: None)
                with client.websocket_connect(f"/ws/screen?token={token}") as ws:
                    with pytest.raises(WebSocketDisconnect) as closed:
                        ws.receive_text()
                    assert closed.value.code == 1013


def test_socket_sends_idle_cadence_answers_ping_and_reports_bad_frames(screen) -> None:
    from api.main import app

    _, _, token, _ = screen
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        assert json.loads(ws.receive_text()) == {
            "type": "screen.cadence",
            "fps": settings.screen_idle_fps,
            "fullFrameSeconds": settings.screen_full_frame_seconds,
        }
        ws.send_text(json.dumps({"type": "ping", "t": 7}))
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 7}
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["code"] == "bad_request"
        ws.send_bytes(b"\x00\x00")
        assert "shorter" in json.loads(ws.receive_text())["message"]
        ws.send_bytes(b"\xff" * (settings.screen_max_frame_bytes + 1))
        assert json.loads(ws.receive_text())["message"] == "frame too large"


def replay(
    client: TestClient, ws, ocr: StubOcr, frames: list[dict], base: int, start: int = 0
) -> int:
    for i, frame in enumerate(frames, start=start):
        jpeg = render_full(frame) if frame["region"] == "full" else unique_jpeg(i)
        ocr.register(jpeg, ocr_of(frame))
        ws.send_bytes(envelope(header(base + frame["t"], frame["region"]), jpeg))
        wait_handled(i + 1)
    return start + len(frames)


def test_replayed_zoom_meeting_becomes_a_ready_conversation(screen) -> None:
    """Done-when (XERK-2505): a recorded frame sequence replays into a ready conversation
    with the right speakers and no duplicate lines."""
    from api.main import app

    store, ocr, token, admin = screen
    store.set_speaker_alias(admin.household, "tesoro", "kunal@tesoroxp.com", "Kunal")
    base = int(time.time() * 1000)
    frames = FIXTURE["frames"]
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        assert json.loads(ws.receive_text())["fps"] == settings.screen_idle_fps
        replay(client, ws, ocr, frames, base)
        # Captions appeared: the window was switched to the meeting cadence.
        meeting_cadence = json.loads(ws.receive_text())
        assert meeting_cadence == {
            "type": "screen.cadence",
            "orgSiteKey": "tesoro",
            "fps": settings.screen_meeting_fps,
            "fullFrameSeconds": settings.screen_full_frame_seconds,
        }
        (summary,) = store.list(admin.household)
        assert summary.status == "live" and summary.source == "screen"
        # Not over until captions have been gone for the end window.
        last = base + frames[-1]["t"]
        end_ms = int(settings.screen_meeting_end_seconds * 1000)
        assert client.portal.call(meetings.sweep, last + end_ms - 1) == []
        assert client.portal.call(meetings.sweep, last + end_ms) == [summary.id]
        assert json.loads(ws.receive_text())["fps"] == settings.screen_idle_fps

    conv = store.get(admin.household, summary.id)
    assert conv is not None
    assert conv.status == "ready" and conv.ended_at is not None
    assert conv.owner == admin.user_id
    assert (conv.source, conv.org_site_key, conv.org_name, conv.platform) == (
        "screen",
        "tesoro",
        "Tesoro",
        "zoom",
    )
    expected = FIXTURE["expected"]
    assert conv.meeting_title == expected["title"]
    assert conv.participants == expected["participants"]
    got = [{"speaker": s.speaker, "text": s.text} for s in conv.segments]
    assert got == expected["lines"]
    assert len({s.text for s in conv.segments}) == len(conv.segments)
    starts = [s.start_ms for s in conv.segments]
    assert starts == sorted(starts) and all(s.end_ms >= s.start_ms for s in conv.segments)
    assert metrics.snapshot()["counters"]["screen.meetings_ended"] == 1


def test_history_api_shows_speakers_org_and_participants(screen) -> None:
    from api.main import app

    store, ocr, token, admin = screen
    base = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
            replay(client, ws, ocr, FIXTURE["frames"][:6], base)
            client.portal.call(meetings.close_all)
        (item,) = client.get("/conversations").json()
        assert item["source"] == "screen" and item["orgSiteKey"] == "tesoro"
        assert item["orgName"] == "Tesoro" and item["platform"] == "zoom"
        assert item["meetingTitle"] == "Q3 Release Sync" and item["status"] == "ready"
        detail = client.get(f"/conversations/{item['id']}").json()
        assert detail["participants"] == ["Priya Raman", "Sean O'Neil", "kunal@tesoroxp.com"]
        assert {s["speaker"] for s in detail["segments"]} == {"Priya Raman", "Sean O'Neil"}


def test_a_reconnect_mid_meeting_continues_the_same_conversation(screen) -> None:
    from api.main import app

    store, ocr, token, admin = screen
    base = int(time.time() * 1000)
    frames = FIXTURE["frames"]
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
            n = replay(client, ws, ocr, frames[:6], base)
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            assert "orgSiteKey" not in json.loads(ws.receive_text())  # idle default
            replay(client, ws, ocr, frames[6:7], base, start=n)
            # The meeting is still on: the new socket is told so (QA D3).
            assert json.loads(ws.receive_text()) == {
                "type": "screen.cadence",
                "orgSiteKey": "tesoro",
                "fps": settings.screen_meeting_fps,
                "fullFrameSeconds": settings.screen_full_frame_seconds,
            }
            replay(client, ws, ocr, frames[7:], base, start=n + 1)
            client.portal.call(meetings.sweep, base + 10**7)
    (summary,) = store.list(admin.household)
    conv = store.get(admin.household, summary.id)
    assert [s.text for s in conv.segments] == [x["text"] for x in FIXTURE["expected"]["lines"]]


def test_ocr_failures_are_counted_and_the_socket_stays_up(screen) -> None:
    from api.main import app

    store, ocr, token, admin = screen
    ocr.fail = True
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(header(int(time.time() * 1000), "caption"), unique_jpeg(1)))
        wait_handled(1)
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"
    assert metrics.snapshot()["counters"]["screen.ocr_errors"] == 1
    assert store.list(admin.household) == []


def test_store_failure_on_a_frame_keeps_the_socket(screen, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.main import app

    store, ocr, token, admin = screen

    def boom(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(store, "speaker_aliases", boom)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        jpeg = ocr.register(unique_jpeg(1), crop("hello there"))
        ws.send_bytes(envelope(header(int(time.time() * 1000), "caption"), jpeg))
        wait_handled(1)
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"
    assert metrics.snapshot()["counters"]["screen.frame_errors"] == 1


def test_meeting_start_for_a_deleted_account_closes_the_socket(screen) -> None:
    from api.auth import get_user_store
    from api.main import app

    store, ocr, token, admin = screen
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        get_user_store().delete(admin.user_id)
        jpeg = ocr.register(unique_jpeg(1), crop("hello there"))
        ws.send_bytes(envelope(header(int(time.time() * 1000), "caption"), jpeg))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1008
    assert store.list(admin.household) == []


def test_skewed_client_clock_is_replaced_by_the_server_clock(screen) -> None:
    from api.main import app

    store, ocr, token, admin = screen
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        jpeg = ocr.register(unique_jpeg(1), crop("Priya Raman", "hello there"))
        ws.send_bytes(envelope(header(1, "caption"), jpeg))
        wait_handled(1)
        tracker = meetings.get((admin.household, admin.user_id, "tesoro"))
        assert tracker is not None and tracker.last_ts > time.time() * 1000 - 60_000


# --- tracker lifecycle without a socket ----------------------------------------------------


def _tracker(store: InMemoryConversationStore, *, exists: bool = True) -> MeetingTracker:
    return MeetingTracker(
        "default",
        "user-1",
        ScreenMeta("tesoro", "Tesoro", "zoom"),
        store=store,
        account_exists=lambda _: exists,
        cadence=Cadence(0.2, 1.0, 10.0),
        stable_ms=3000,
        end_ms=60_000,
    )


def test_sweep_drops_idle_detached_trackers_and_shutdown_finishes_open_ones() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        idle = _tracker(store)
        busy = _tracker(store)
        meetings.clear()
        meetings.put(("default", "user-1", "idle"), idle)
        meetings.put(("default", "user-1", "busy"), busy)
        await busy.ensure_roster()
        await busy.on_caption([blk(None, "hello there", header=False)], 1000)
        assert meetings.open_count() == 1
        assert await meetings.sweep(2000) == []
        assert meetings.get(("default", "user-1", "idle")) is None
        await meetings.close_all()
        (conv,) = store.list("default")
        assert conv.status == "ready"
        assert [s.text for s in store.get("default", conv.id).segments] == ["hello there"]
        assert await busy.end(5000) is None  # nothing open any more

    asyncio.run(run())


def test_a_failed_meeting_end_is_retried_by_the_next_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        t = _tracker(store)
        meetings.clear()
        meetings.put(("default", "user-1", "x"), t)
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        real = store.finish
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db blip")
            return real(*a, **k)

        monkeypatch.setattr(store, "finish", flaky)
        assert await meetings.sweep(10**6) == []
        assert t.conversation_id is not None
        assert len(await meetings.sweep(10**6 + 1)) == 1
        meetings.clear()

    asyncio.run(run())


def test_cadence_to_a_dead_socket_is_ignored() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        t = _tracker(store)

        async def dead(_msg) -> None:
            raise RuntimeError("socket gone")

        t.notify = dead
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        assert t.conversation_id is not None

    asyncio.run(run())


def test_deleting_the_user_closes_their_open_screen_socket(screen) -> None:
    """QA D1: auth is checked at the handshake, so a delete must stop a live socket."""
    from api.auth import Principal, get_user_store, issue_token
    from api.main import app

    store, ocr, _, admin = screen
    member = get_user_store().create("capture-op", "capture-op-pass-1", household=admin.household)
    token = issue_token(
        Principal(member.user_id, member.household, "member", member.username, scope="screen"),
        secret=settings.auth_secret,
        ttl_seconds=3600,
    )
    now = int(time.time() * 1000)
    jpeg = ocr.register(unique_jpeg(1), crop("Ana Lopez", "first line here"))
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(header(now, "caption"), jpeg))
        wait_handled(1)
        assert json.loads(ws.receive_text())["fps"] == settings.screen_meeting_fps
        assert client.delete(f"/auth/users/{member.user_id}").status_code == 204
        # Synchronous first, so a missing revoke fails here instead of hanging below.
        assert metrics.snapshot()["counters"].get("screen.revoked") == 1
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
        assert closed.value.code == 1008
    assert screen_ws._revokers.get(member.user_id) is None
    assert screen_ws.revoke_user(member.user_id) == 0


def test_one_socket_feeds_a_bounded_number_of_windows(screen) -> None:
    from api.main import app

    _, ocr, token, _ = screen
    now = int(time.time() * 1000)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        for i in range(screen_ws.MAX_WINDOWS):
            jpeg = ocr.register(unique_jpeg(i), crop())
            ws.send_bytes(envelope(header(now, "caption", site=f"site-{i}"), jpeg))
        wait_handled(screen_ws.MAX_WINDOWS)
        ws.send_bytes(envelope(header(now, "caption", site="one-too-many"), unique_jpeg(99)))
        assert "windows" in json.loads(ws.receive_text())["message"]


def test_oversized_or_mismatched_frames_are_not_decoded() -> None:
    """QA D4: a small JPEG can decode to gigabytes; only frames the size OCR read, and at
    most 4096x4096, are decoded for the border check."""
    frame = FIXTURE["frames"][0]
    wrong_size = dict(frame, width=640, height=360)
    assert parse_full_frame(ocr_of(wrong_size), render_full(frame)).active_speaker is None
    huge = OcrResult(5000, 5000, ocr_of(frame).lines)
    buf = io.BytesIO()
    Image.new("RGB", (5000, 5000)).save(buf, format="JPEG")
    assert parse_full_frame(huge, buf.getvalue()).active_speaker is None


def test_a_failed_segment_write_is_retried_not_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA D7: the merger counts a line final once; a failed write must be retried."""

    async def run() -> None:
        store = InMemoryConversationStore()
        t = _tracker(store)
        real = store.add_segment
        fails = {"n": 1}

        def flaky(*a, **k):
            if fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("db blip")
            return real(*a, **k)

        monkeypatch.setattr(store, "add_segment", flaky)
        await t.on_caption([blk(None, "first important line", header=False)], 0)
        with pytest.raises(RuntimeError):
            await t.on_caption(
                [blk(None, "first important line", header=False), blk(None, "next")], 500
            )
        await t.on_caption([blk(None, "next")], 900)
        await t.end(10_000)
        (conv,) = store.list("default")
        assert [s.text for s in store.get("default", conv.id).segments] == [
            "first important line",
            "next",
        ]

    asyncio.run(run())


def test_a_pending_stale_sweep_runs_before_the_meeting_row_exists() -> None:
    """QA D8: the boot sweep retry must never finish a meeting this process started."""
    from api.persistence import stale

    async def run() -> None:
        store = InMemoryConversationStore()
        store.create("default", "left-live-by-a-crash")
        stale.arm(store)
        t = _tracker(store)
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        assert not stale.is_pending(store)
        assert store.get("default", t.conversation_id).status == "live"
        assert store.get("default", "left-live-by-a-crash").status == "ready"

    asyncio.run(run())
