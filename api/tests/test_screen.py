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
    return parse_result({"lines": frame["lines"]}, (frame["width"], frame["height"]))


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
        parse_result([], (1, 1))
    with pytest.raises(OcrError):
        parse_result({"lines": [{"text": "x", "box": [1, 2]}]}, (1, 1))
    got = parse_result({"lines": [{"text": "  ", "box": [0, 0, 1, 1]}]}, (4, 4))
    assert got.lines == ()
    # tenir-ocr's answer for a slide-like image: no lines, never partial.
    assert parse_result({"lines": [], "too_dense": True, "ms": 400}, (9, 9)).lines == ()


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="JPEG")
    return buf.getvalue()


def _mock_ocr(handler) -> HttpOcrClient:
    client = HttpOcrClient("http://ocr.test/", timeout=1)
    client._client = httpx.AsyncClient(
        base_url="http://ocr.test", transport=httpx.MockTransport(handler)
    )
    return client


def test_http_ocr_client_posts_a_caption_crop_whole() -> None:
    """The tenir-ocr contract (ocr/README.md): raw image in, lines with boxes out."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"lines": [{"text": "hi", "box": [0, 0, 5, 5], "score": 0.9}], "too_dense": False},
        )

    async def run() -> None:
        client = _mock_ocr(handler)
        got = await client.read(_jpeg(700, 120))
        assert (got.width, got.height) == (700, 120)
        assert got.lines[0].text == "hi" and got.lines[0].score == 0.9
        await client.aclose()

    asyncio.run(run())
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/ocr" and seen[0].headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("status", [413, 503, 500])
def test_ocr_refusals_and_bad_bodies_drop_the_frame(status: int) -> None:
    async def run() -> None:
        client = _mock_ocr(lambda request: httpx.Response(status))
        with pytest.raises(OcrError):
            await client.read(_jpeg(100, 40))
        client = _mock_ocr(lambda request: httpx.Response(200, text="<html>"))
        with pytest.raises(OcrError):
            await client.read(_jpeg(100, 40))
        with pytest.raises(OcrError, match="not a readable image"):
            await client.read(b"\xff\xd8junk")
        with pytest.raises(OcrError, match="too large"):
            await client.read(_jpeg(4000, 2600))

    asyncio.run(run())


def test_a_full_frame_is_read_in_strips_under_the_service_cap() -> None:
    """tenir-ocr refuses images over 0.5 Mpx; a 1280x720 window is cut into overlapping
    strips and each strip's lines are shifted back into window coordinates."""
    from api.screen import ocr as ocr_mod

    sizes: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        w, h = Image.open(io.BytesIO(request.content)).size
        sizes.append((w, h))
        if len(sizes) == 1:
            # The title, plus a name cut by the strip's bottom edge that the next one sees whole.
            lines = [
                {"text": "Q3 Release Sync", "box": [560, 6, 720, 26], "score": 1},
                {"text": "Priya Ra", "box": [30, h - 12, 150, h], "score": 1},
            ]
        elif len(sizes) == 2:
            lines = [{"text": "Priya Raman", "box": [30, 28, 190, 48], "score": 1}]
        else:
            lines = []
        return httpx.Response(200, json={"lines": lines, "too_dense": False})

    async def run() -> OcrResult:
        client = _mock_ocr(handler)
        return await client.read(_jpeg(1280, 720))

    got = asyncio.run(run())
    assert all(w * h <= ocr_mod.STRIP_PIXELS for w, h in sizes) and len(sizes) == 3
    tops = [top for top, _ in ocr_mod.strips(1280, 720)]
    assert [ln.text for ln in got.lines] == ["Q3 Release Sync", "Priya Raman"]
    assert got.lines[1].box[1] == tops[1] + 28  # shifted into window coordinates
    assert (got.width, got.height) == (1280, 720)


def test_strips_cover_the_frame_with_overlap() -> None:
    from api.screen import ocr as ocr_mod

    spans = ocr_mod.strips(1920, 1080)
    assert spans[0][0] == 0 and spans[-1][1] == 1080
    assert all(b[0] == a[1] - ocr_mod.STRIP_OVERLAP for a, b in zip(spans, spans[1:]))
    assert all(1920 * (bottom - top) <= ocr_mod.STRIP_PIXELS for top, bottom in spans)
    assert ocr_mod._dedupe(
        [
            OcrLine("A b", (0, 10, 5, 20)),
            OcrLine("A b", (2, 12, 7, 22)),
            OcrLine("C", (0, 40, 1, 50)),
        ]
    ) == [OcrLine("A b", (0, 10, 5, 20)), OcrLine("C", (0, 40, 1, 50))]


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


@pytest.mark.parametrize("gap_s", [1, 2, 5, 8, 12, 30])
def test_a_reply_said_again_after_someone_else_is_kept_at_any_gap(gap_s: int) -> None:
    """QA delta D2: Ana "okay", Bo talks for gap_s seconds, Ana "okay" again — at 1 fps,
    with the box scrolling the first "okay" away at some point."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    out += m.feed([blk("Ana", "okay")], 0)
    for t in range(1, gap_s + 1):
        out += m.feed([blk("Ana", "okay"), blk("Bo", "can you hear me")], t * 1000)
    out += m.feed([blk("Bo", "can you hear me"), blk("Ana", "okay")], (gap_s + 1) * 1000)
    out += m.feed([blk("Ana", "okay")], (gap_s + 2) * 1000)
    out += m.flush((gap_s + 20) * 1000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "okay"),
        ("Bo", "can you hear me"),
        ("Ana", "okay"),
    ]


def test_an_ocr_miss_of_many_frames_does_not_store_a_line_twice() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed([blk("Ana", "the release is blocked on review")], t * 1000)
    for t in range(5, 15):  # OCR loses the line for 10 frames, the next one stays
        out += m.feed([blk("Bo", "agreed")], t * 1000)
    out += m.feed([blk("Ana", "the release is blocked on review"), blk("Bo", "agreed")], 15_000)
    out += m.flush(20_000)
    assert [s.text for s in out] == ["the release is blocked on review", "agreed"]


def test_a_new_sentence_after_another_speaker_does_not_extend_an_old_final() -> None:
    """QA delta finding 3: "okay" ... Bo ... "okay so lets start the review" is two lines."""
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    out = []
    out += m.feed([blk("Ana", "okay")], 0)
    out += m.feed([blk("Ana", "okay")], 1000)
    out += m.feed([blk("Bo", "can you hear me")], 2000)
    out += m.feed([blk("Bo", "can you hear me")], 3000)
    out += m.feed([blk("Ana", "okay so lets start the review")], 5000)
    out += m.flush(9000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "okay"),
        ("Bo", "can you hear me"),
        ("Ana", "okay so lets start the review"),
    ]


@pytest.mark.parametrize(
    "glitch",
    [
        [blk("Ana", "w3 shou1d sh1p the b@ild")],  # garbled top line
        [blk(None, "we sh~ ,,", header=False)],  # scroll fragment above the box
        [blk("Ana", "we shou")],  # half-cut top line
    ],
)
def test_a_one_frame_ocr_glitch_above_the_box_stores_nothing_twice(glitch) -> None:
    """QA delta 3: a fresh (unknown) line on top must not push the known lines below it
    out of matching their finals."""
    box = [
        blk("Ana", "we should ship the build"),
        blk("Bo", "agreed on friday then"),
        blk("Cy", "i will tell the customer"),
    ]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed(box, t * 1000)
    out += m.feed(glitch + box[1:], 5000)
    for t in range(6, 9):
        out += m.feed(box, t * 1000)
    out += m.flush(20_000)
    # Each real line once, and the glitch itself is not stored as something said.
    assert sorted(s.text for s in out) == sorted(b.lines[0] for b in box)


def test_a_user_deleted_during_the_handshake_is_closed(screen, monkeypatch) -> None:
    """The delete lands after auth but before the socket registered for revocation."""
    from api.auth import get_user_store
    from api.main import app

    _, _, token, admin = screen
    real = screen_ws._screen_principal

    def auth_then_delete(ws):
        got = real(ws)
        get_user_store().delete(admin.user_id)
        assert screen_ws.revoke_user(admin.user_id) == 0  # nothing registered yet
        return got

    monkeypatch.setattr(screen_ws, "_screen_principal", auth_then_delete)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        assert json.loads(ws.receive_text())["type"] == "screen.cadence"
        ws.send_text(json.dumps({"type": "ping"}))
        # A socket left open answers the ping instead of closing (fails, never hangs).
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1008


def test_a_short_reply_seen_once_below_the_top_is_kept() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(3):
        out += m.feed([blk("Ana", "yesterday the build failed")], t * 1000)
    out += m.feed([blk("Ana", "yesterday the build failed"), blk("Bo", "yes")], 3000)
    out += m.flush(10_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "yesterday the build failed"),
        ("Bo", "yes"),
    ]


def test_a_box_showing_a_repeat_stores_each_line_once() -> None:
    """QA delta 4: [okay], [okay, Bo], [okay, Bo, okay] held for 8 frames is 3 lines."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    t = 0
    for box in (
        [blk("Ana", "okay")],
        [blk("Ana", "okay"), blk("Bo", "sure lets start")],
        [blk("Ana", "okay"), blk("Bo", "sure lets start"), blk("Ana", "okay")],
    ):
        for _ in range(3 if len(box) < 3 else 8):
            out += m.feed(box, t)
            t += 1000
    out += m.flush(t + 10_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "okay"),
        ("Bo", "sure lets start"),
        ("Ana", "okay"),
    ]


def test_similar_lines_in_one_block_are_each_stored_once() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    rows = ["first line here", "second line here", "third line here"]
    for t in range(12):
        out += m.feed([blk("Ana", *rows[: min(3, 1 + t // 3)])], t * 1000)
    out += m.flush(30_000)
    assert [s.text for s in out] == rows


def test_a_garbled_top_line_then_the_clean_one_stores_nothing_twice() -> None:
    """QA delta 4: one bad frame, then the correct text returns."""
    box = [
        blk("Ana", "we should ship the build"),
        blk("Bo", "agreed on friday then"),
        blk("Cy", "i will tell the customer"),
    ]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed(box, t * 1000)
    out += m.feed([blk("Ana", "w3 shou1d sh1p tbe b@ild")] + box[1:], 5000)
    for t in range(6, 12):
        out += m.feed(box, t * 1000)
    out += m.flush(30_000)
    assert sorted(s.text for s in out) == sorted(b.lines[0] for b in box)


def test_another_speakers_similar_short_line_is_kept() -> None:
    """QA delta 4: Bo's "we should ship on monday" after Ana's "...friday" is real."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(3):
        out += m.feed([blk("Ana", "we should ship on friday")], t * 1000)
    out += m.feed(
        [blk("Ana", "we should ship on friday"), blk("Bo", "we should ship on monday")], 3000
    )
    out += m.feed([blk("Bo", "we should ship on monday"), blk("Ana", "no")], 4000)
    out += m.flush(10_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "we should ship on friday"),
        ("Bo", "we should ship on monday"),
        ("Ana", "no"),
    ]


def _simulate(
    seed: int, *, noise: bool, skip: float = 0.0
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Roll a 3-line Zoom caption box over a random conversation at 1 snapshot per word.

    The bottom line grows a word at a time; finished lines scroll up; short replies repeat.
    With ``noise``, OCR garbles the top line or drops a middle line in some snapshots; with
    ``skip``, that share of snapshots is never read (latest-frame-wins drops it)."""
    import random

    rng = random.Random(seed)
    speakers = ["Ana", "Bo", "Cy"]
    phrases = [
        "okay",
        "yes",
        "sounds good",
        "we should ship the build on friday",
        "the login timeout is back on staging",
        "can you open a ticket for that",
        "i will tell the customer today",
        "thanks everyone",
    ]
    # Short replies repeat verbatim all the time; whole sentences almost never do, so each
    # long one names a different item (an all-verbatim pool made every box ambiguous).
    items = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    said = []
    for _ in range(25):
        phrase = rng.choice(phrases)
        if len(phrase.split()) > 2:
            phrase = f"{phrase} {rng.choice(items)} {rng.choice(items)}"
        said.append((rng.choice(speakers), phrase))
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    shown: list[tuple[str, str]] = []
    t = 0
    for speaker, phrase in said:
        words = phrase.split()
        for k in range(1, len(words) + 1):
            box = (shown + [(speaker, " ".join(words[:k]))])[-3:]
            if noise and len(box) == 3 and rng.random() < 0.2:
                if rng.random() < 0.5:
                    # OCR misreads about one character in five of the top line.
                    chars = list(box[0][1])
                    for c in range(0, len(chars), 5):
                        chars[c] = "~"
                    box = [(box[0][0], "".join(chars))] + box[1:]
                else:
                    box = [box[0], box[2]]
            # Any snapshot may be dropped, so whole short replies can land between reads.
            if skip and rng.random() < skip:
                t += 1000
                continue
            blocks: list[CaptionBlock] = []
            for sp, text in box:
                if blocks and blocks[-1].speaker == sp:
                    blocks[-1].lines.append(text)
                else:
                    blocks.append(blk(sp, text))
            out += m.feed(blocks, t)
            t += 1000
        shown.append((speaker, phrase))
        t += 1000
    out += m.flush(t + 10_000)
    return said, [(s.speaker, s.text) for s in out]


def _only_said(said: list, stored: list) -> bool:
    """Every stored line is something said (or, cut short, the start of it): nothing made up."""
    return all(any(sp == x and text.startswith(t) for sp, text in said) for x, t in stored)


@pytest.mark.parametrize("seed", range(20))
def test_simulated_rolling_captions_store_what_was_said(seed: int) -> None:
    """Measured over 1000 seeds: at most one line lost (a speaker repeating a short phrase
    back to back), none duplicated, nothing made up."""
    said, stored = _simulate(seed, noise=False)
    assert len(said) - 1 <= len(stored) <= len(said)
    assert _only_said(said, stored)


@pytest.mark.parametrize("seed", range(20))
def test_ocr_noise_never_duplicates_a_line(seed: int) -> None:
    said, stored = _simulate(seed, noise=True)
    assert len(said) - 2 <= len(stored) <= len(said)


def test_a_reply_and_a_repeat_between_two_snapshots_are_both_kept() -> None:
    """QA delta 5: [Ana okay] then [Bo sure, Ana okay] — Bo's line has a verified header and
    resembles nothing known, so it is a real line, not an OCR glitch above a match."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed([blk("Ana", "okay")], t * 1000)
    for t in range(5, 10):
        out += m.feed([blk("Bo", "sure"), blk("Ana", "okay")], t * 1000)
    out += m.flush(20_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Ana", "okay"),
        ("Bo", "sure"),
        ("Ana", "okay"),
    ]


def test_a_crafted_huge_caption_snapshot_is_bounded() -> None:
    """QA delta 5: OCR text is attacker-controlled; the merge cuts a snapshot to its bottom
    lines and each line's length, so one frame can't stall the server."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    rows = [f"line {i} " + "word " * 100 for i in range(200)]
    started = time.monotonic()
    m.feed([blk("Ana", *rows)], 0)
    m.feed([blk("Ana", *rows)], 1000)
    assert time.monotonic() - started < 2.0
    out = m.flush(5000)
    assert len(out) == 12 and all(len(s.text) <= 160 for s in out)


def test_ocr_results_and_rosters_are_bounded() -> None:
    from api.screen import ocr as ocr_mod
    from api.screen import zoom as zoom_mod

    many = {"lines": [{"text": "x", "box": [0, 0, 1, 1]}] * 1000}
    assert len(parse_result(many, (9, 9)).lines) == ocr_mod.MAX_LINES
    roster = Roster()
    for i in range(zoom_mod._MAX_ROSTER + 50):
        roster.add(f"Person Number{i}")
    assert len(roster) == zoom_mod._MAX_ROSTER


@pytest.mark.parametrize("seed", range(20))
def test_dropped_snapshots_never_duplicate_a_line(seed: int) -> None:
    """Latest-frame-wins drops snapshots under load, so lines can land between reads (QA
    delta 5/6). Measured over 1000 seeds: nothing duplicated or made up, at most 3 lost."""
    said, stored = _simulate(seed, noise=False, skip=0.3)
    assert len(said) - 3 <= len(stored) <= len(said)
    assert _only_said(said, stored)


def test_a_reply_and_a_repeat_between_snapshots_are_kept_with_earlier_context() -> None:
    """QA delta 6: a misread must look like what it misreads. With Bo's earlier line in the
    box, [Bo sure, Ana okay] arriving between snapshots used to be explained away."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bo", "can you hear me")], t * 1000)
    for t in range(4, 9):
        out += m.feed([blk("Bo", "can you hear me"), blk("Ana", "okay")], t * 1000)
    for t in range(9, 14):
        out += m.feed([blk("Bo", "sure"), blk("Ana", "okay")], t * 1000)
    out += m.flush(30_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Bo", "can you hear me"),
        ("Ana", "okay"),
        ("Bo", "sure"),
        ("Ana", "okay"),
    ]


def test_lines_first_seen_together_keep_their_on_screen_order() -> None:
    """QA delta 6: they used to share one start_ms, and Postgres orders ties by a random id."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = m.feed([blk("Ana", "okay"), blk("Bo", "sure"), blk("Ana", "okay again")], 1000)
    out += m.flush(10_000)
    starts = {s.text: s.start_ms for s in out}
    assert starts["okay"] < starts["sure"] < starts["okay again"]


def test_a_line_said_between_snapshots_landing_between_known_lines_is_kept() -> None:
    """QA delta 7: a 3-line box [Cy, Bo, Ana okay] then [Bo, Cy yes, Ana okay]."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    box1 = [blk("Cy", "lets begin"), blk("Bo", "can you hear me"), blk("Ana", "okay")]
    box2 = [blk("Bo", "can you hear me"), blk("Cy", "yes"), blk("Ana", "okay")]
    for t in range(5):
        out += m.feed(box1, t * 1000)
    for t in range(5, 10):
        out += m.feed(box2, t * 1000)
    out += m.flush(30_000)
    assert [(s.speaker, s.text) for s in out] == [
        ("Cy", "lets begin"),
        ("Bo", "can you hear me"),
        ("Ana", "okay"),
        ("Cy", "yes"),
        ("Ana", "okay"),
    ]


def _gallery(active: int) -> tuple[OcrResult, bytes]:
    """A 1080p 3x3 Zoom gallery with 12 px gaps: labels inset at each tile's bottom-left,
    the active tile ringed in Zoom green, label boxes jittered a pixel or two like real OCR."""
    names = ["Priya Raman", "Sean ONeil", "Ana Lopez", "Bo Chen", "Kunal Shah", "Maria Diaz"]
    names += ["Omar Ali", "Lee Park", "Jo Kim"]
    img = Image.new("RGB", (1920, 1080), (26, 26, 26))
    draw = ImageDraw.Draw(img)
    lines = []
    w, h, gap = 620, 290, 12  # leaves the bottom 10% for the toolbar
    for i, name in enumerate(names):
        tx, ty = 20 + (i % 3) * (w + gap), 40 + (i // 3) * (h + gap)
        if i == active:
            draw.rectangle([tx, ty, tx + w, ty + h], outline=(35, 217, 89), width=4)
        jitter = (i * 7) % 3
        lines.append(OcrLine(name, (tx + 10, ty + h - 32 + jitter, tx + 150, ty + h - 12 + jitter)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    # Shuffled the way a y-first sort would see jittered boxes.
    return OcrResult(1920, 1080, tuple(sorted(lines, key=lambda ln: ln.box[1]))), buf.getvalue()


@pytest.mark.parametrize("active", range(9))
def test_the_active_tile_wins_on_a_tight_gallery(active: int) -> None:
    """QA delta 10: one window around each label also caught the neighbours' border."""
    result, jpeg = _gallery(active)
    out = parse_full_frame(result, jpeg)
    assert out.active_speaker == out.participants[active]


def test_participants_keep_tile_order_despite_jittered_boxes() -> None:
    result, jpeg = _gallery(0)
    assert parse_full_frame(result, jpeg).participants == [
        "Priya Raman", "Sean ONeil", "Ana Lopez", "Bo Chen", "Kunal Shah", "Maria Diaz",
        "Omar Ali", "Lee Park", "Jo Kim",
    ]  # fmt: skip


# --- captions-off notice (XERK-2506) -------------------------------------------------------


def _toolbar_frame(*extra: OcrLine) -> OcrResult:
    """A 1280x720 full frame with Zoom's meeting toolbar along the bottom."""
    buttons = ("Mute", "Stop Video", "Participants", "Share Screen", "Leave")
    toolbar = tuple(line(b, 100 + 200 * i, 680, 180 + 200 * i, 700) for i, b in enumerate(buttons))
    return OcrResult(1280, 720, (*extra, *toolbar))


def test_a_full_frame_is_in_a_meeting_only_when_the_toolbar_shows() -> None:
    assert parse_full_frame(_toolbar_frame(line("Priya Raman", 40, 300, 160, 320))).in_meeting
    # A remote desktop's icon labels are name-shaped, but no toolbar: not a meeting.
    desktop = OcrResult(
        1280,
        720,
        (line("Recycle Bin", 20, 100, 110, 115), line("Google Chrome", 20, 200, 130, 215)),
    )
    assert not parse_full_frame(desktop).in_meeting
    # Two toolbar words alone (e.g. a chat line mentioning "Chat" and "Share") aren't enough.
    two = OcrResult(1280, 720, (line("Chat", 100, 680, 150, 700), line("Share", 300, 680, 360, 700)))
    assert not parse_full_frame(two).in_meeting
    # Toolbar words higher up the window (a slide listing them) don't count.
    high = OcrResult(
        1280, 720, tuple(line(b, 100 + 200 * i, 300, 180 + 200 * i, 320) for i, b in enumerate(
            ("Mute", "Stop Video", "Participants", "Leave")))
    )
    assert not parse_full_frame(high).in_meeting


def test_a_meeting_on_screen_without_captions_sends_a_notice_then_repeats_slowly() -> None:
    from api.contract import ScreenNotice
    from api.screen import meetings as mt

    async def run() -> None:
        sent: list[object] = []

        async def notify(msg: object) -> None:
            sent.append(msg)

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        idle = parse_full_frame(OcrResult(1280, 720, ()))

        def notices() -> list[ScreenNotice]:
            return [m for m in sent if isinstance(m, ScreenNotice)]

        await t.on_full(meeting, 0)
        await t.on_full(idle, mt.CAPTIONS_OFF_MS - 1)  # toolbar hidden again: still a meeting
        assert notices() == []
        await t.on_full(idle, mt.CAPTIONS_OFF_MS)
        (n,) = notices()
        assert (n.orgSiteKey, n.kind.value) == ("tesoro", "captions_off")
        await t.on_full(idle, mt.CAPTIONS_OFF_MS + 60_000)
        assert len(notices()) == 1  # not every full frame
        await t.on_full(meeting, mt.CAPTIONS_OFF_MS + mt.CAPTIONS_OFF_REPEAT_MS)
        assert len(notices()) == 2
        assert metrics.snapshot()["counters"]["screen.captions_off"] >= 2

    metrics.reset()
    asyncio.run(run())


def test_captions_or_a_long_gone_toolbar_clear_the_captions_off_warning() -> None:
    from api.contract import ScreenNotice
    from api.screen import meetings as mt

    async def run() -> None:
        sent: list[object] = []

        async def notify(msg: object) -> None:
            sent.append(msg)

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        idle = parse_full_frame(OcrResult(1280, 720, ()))
        await t.on_full(meeting, 0)
        # Captions show: the meeting is recording, so no warning while it is open.
        await t.ensure_roster()
        await t.on_caption([blk(None, "hello there", header=False)], 1000)
        await t.on_full(meeting, mt.CAPTIONS_OFF_MS * 2)
        assert not any(isinstance(m, ScreenNotice) for m in sent)

        # No toolbar for longer than MEETING_SEEN_MS: the meeting is over, nothing to warn.
        t2 = _tracker(InMemoryConversationStore())
        t2.notify = notify
        await t2.on_full(meeting, 0)
        await t2.on_full(idle, mt.MEETING_SEEN_MS + 1)
        await t2.on_full(idle, mt.MEETING_SEEN_MS + mt.CAPTIONS_OFF_MS + 2)
        assert not any(isinstance(m, ScreenNotice) for m in sent)

    asyncio.run(run())


def test_a_captions_off_notice_reaches_the_capture_client(screen, monkeypatch) -> None:
    from api.screen import meetings as mt

    store, ocr, token, _ = screen
    monkeypatch.setattr(mt, "CAPTIONS_OFF_MS", 0)
    frame = ocr.register(unique_jpeg(1), _toolbar_frame())
    frame2 = ocr.register(unique_jpeg(2), _toolbar_frame())
    from api.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/screen", headers={"Authorization": f"Bearer {token}"}) as ws:
            assert json.loads(ws.receive_text())["type"] == "screen.cadence"
            now = int(time.time() * 1000)
            ws.send_bytes(envelope(header(now, "full"), frame))
            wait_handled(1)
            ws.send_bytes(envelope(header(now + 1000, "full"), frame2))
            assert json.loads(ws.receive_text()) == {
                "type": "screen.notice",
                "orgSiteKey": "tesoro",
                "kind": "captions_off",
            }


def test_a_meeting_that_had_captions_never_warns_when_it_goes_quiet() -> None:
    """QA D4: after a captioned meeting ends on silence, the toolbar must not trigger a
    'turn on captions' notice; a new uncaptioned meeting after it has gone still does."""
    from api.contract import ScreenNotice
    from api.screen import meetings as mt

    async def run() -> None:
        sent: list[object] = []

        async def notify(msg: object) -> None:
            sent.append(msg)

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        idle = parse_full_frame(OcrResult(1280, 720, ()))
        await t.ensure_roster()
        await t.on_caption([blk(None, "hello there", header=False)], 0)  # toolbar never seen
        await t.end(60_000)  # silence ended the meeting
        await t.on_full(meeting, 70_000)  # toolbar now shows, meeting still on, quiet
        await t.on_full(meeting, 70_000 + mt.CAPTIONS_OFF_MS * 3)
        assert not any(isinstance(m, ScreenNotice) for m in sent)
        # Window leaves the meeting long enough, then a meeting without captions starts.
        later = 70_000 + mt.CAPTIONS_OFF_MS * 3 + mt.MEETING_SEEN_MS + 1
        await t.on_full(idle, later)
        await t.on_full(meeting, later + 1)
        await t.on_full(meeting, later + 2 + mt.CAPTIONS_OFF_MS)
        assert sum(isinstance(m, ScreenNotice) for m in sent) == 1

    asyncio.run(run())
