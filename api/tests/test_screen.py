"""Screen-captured meeting captions (XERK-2505): Zoom parser, rolling-caption merge, the
/ws/screen socket, its device-token auth, and the replayed-meeting acceptance test."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from starlette.websockets import WebSocketDisconnect

from api import history
from api.config import settings
from api.metrics import metrics
from api.persistence import InMemoryConversationStore, ScreenMeta
from api.persistence.models import Segment, alias_key
from api.screen import meetings as meetings_mod
from api.screen import ws as screen_ws
from api.screen import zoom
from api.screen.meetings import Cadence, MeetingTracker, meetings
from api.screen.merge import CaptionMerger
from api.screen.utterances import sentences
from api.screen.ocr import HttpOcrClient, OcrError, OcrLine, OcrResult, parse_result
from api.screen.zoom import (
    caption_blocks,
    CaptionBlock,
    FullFrame,
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
        self.full: dict[bytes, bool] = {}  # what ws passed as read(full=) per frame
        self.split_once: dict[bytes, bool] = {}  # what ws passed as read(split_once=)
        self.fail = False

    def register(self, jpeg: bytes, result: OcrResult) -> bytes:
        self.by_jpeg[jpeg] = result
        return jpeg

    async def read(self, jpeg: bytes, *, full: bool = False, split_once: bool = False) -> OcrResult:
        self.full[jpeg] = full
        self.split_once[jpeg] = split_once
        if self.fail:
            raise OcrError("down")
        return self.by_jpeg[jpeg]


def unique_jpeg(n: int) -> bytes:
    """A distinct valid-looking JPEG per frame, so the stub can key on the bytes."""
    return b"\xff\xd8" + n.to_bytes(4, "big") + b"\xff\xd9"


def device_token(user, ttl_seconds: int = 3600) -> str:
    """A screen device token for ``user`` backed by a live device_tokens row (XERK-2594)."""
    from api.auth import Principal, get_user_store, issue_token

    row = get_user_store().create_device_token(user.user_id, name="test PC", ttl_seconds=ttl_seconds)
    return issue_token(
        Principal(
            user.user_id, user.household, user.role, user.username,
            scope="screen", token_id=row.token_id,
        ),
        secret=settings.auth_secret,
        ttl_seconds=ttl_seconds,
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch):
    """In-memory store + stub OCR wired into /ws/screen; returns (store, ocr, device token)."""
    from api.auth import get_user_store

    store = InMemoryConversationStore()
    ocr = StubOcr()
    monkeypatch.setattr(screen_ws, "get_conversation_store", lambda: store)
    monkeypatch.setattr(screen_ws, "get_ocr_client", lambda: ocr)
    monkeypatch.setattr(history, "get_conversation_store", lambda: store)
    meetings.clear()
    metrics.reset()
    admin = get_user_store().get_by_username("test-admin")
    token = device_token(admin)
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


@pytest.mark.parametrize("order", [("Tim Mahl", "Tim Mahler"), ("Tim Mahler", "Tim Mahl")])
def test_a_cut_off_name_is_the_same_person_as_its_full_reading(order) -> None:
    # XERK-2695: a truncated Zoom tile label put one speaker in the roster twice, and his
    # lines were stored under both names (25 / 26 in one real meeting).
    roster = Roster()
    for name in order:
        roster.add(name)
    assert roster.names == ["Tim Mahler"]
    assert roster.match("Tim Mahl") == roster.match("Tim Mahler") == "Tim Mahler"
    assert "Tim Mahl" in roster and len(roster) == 1
    assert roster.renamed == ({"Tim Mahl": "Tim Mahler"} if order[0] == "Tim Mahl" else {})
    assert roster.copy().match("Tim Mahl") == "Tim Mahler"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Priya Ra", "Priya Raman"),  # a strip-edge fragment (0.84): dropped by ocr instead
        ("Speaker 1", "Speaker 12"),  # numbered labels are different people
        ("Tim Mahler", "Tom Mahler"),  # alike but neither starts the other
    ],
)
def test_names_that_are_not_cut_offs_of_each_other_stay_apart(a: str, b: str) -> None:
    roster = Roster()
    assert roster.add(a) == a and roster.add(b) == b
    assert len(roster) == 2 and not roster.renamed


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


def test_without_a_roster_or_pixels_a_trailing_name_after_a_finished_line_is_a_header() -> None:
    # XERK-2794: the next entry's name shown before its text was stored as speech.
    blocks = parse_caption_crop(crop("Mark Hale", "We can start now.", "Tom Adams"), Roster())
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        (None, True, ["We can start now."]),
        (None, True, []),
    ]


def test_without_pixels_a_docs_page_over_a_title_case_row_starts_no_meeting() -> None:
    # QA on XERK-2794: a trailing name with no header above it verified a docs page.
    rows = ("Fixed a crash on start.", "Improved sync speed.", "Known Issues")
    assert parse_caption_crop(crop(*rows), Roster()) == []


def test_a_dim_trailing_name_still_starts_a_meeting_and_keeps_the_text_above() -> None:
    # QA on XERK-2794: the no-pixel guard also refused a dim name row, and once the panel
    # scrolled the first entry's text was never stored.
    first = [("so that is where the budget stands", False), ("Shad Tischer", True)]
    then = [("Shad Tischer", True), ("we shipped it on friday", False)]

    async def run() -> list[str]:
        store = InMemoryConversationStore()
        t = MeetingTracker(
            "default",
            "user-1",
            ScreenMeta("tesoro", "Tesoro", "zoom"),
            store=store,
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        ros = await t.ensure_roster()
        ts = 0
        for rows in [first] * 4 + [then] * 4:
            ocr, jpeg = panel_crop(*rows)
            live = t.conversation_id is not None
            await t.on_caption(parse_caption_crop(ocr, ros, live=live, jpeg=jpeg), ts)
            ts += 4000
        await t.end(ts + 70_000)
        (summary,) = store.list("default")
        conv = store.get("default", summary.id)
        assert conv is not None
        return [s.text for s in sorted(conv.segments, key=lambda s: s.start_ms)]

    assert asyncio.run(run()) == ["so that is where the budget stands", "we shipped it on friday"]


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
    blocks = parse_caption_crop(result, Roster(), live=True)  # header-less: mid-meeting
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


def test_lines_from_snapshots_with_equal_ts_keep_increasing_starts() -> None:
    # XERK-2702: a clamped clock repeats a ts across snapshots. A wrapped row first seen in
    # the second must start after the row it continues, or the tie reads back in random order.
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "Are we missing the cedar in test")], 5000)
    m.feed([blk("A", "Are we missing the cedar in test", "environment?")], 5000)
    m.feed([blk("A", "Are we missing the cedar in test", "environment?", "yes")], 4000)
    out = m.flush(9000)
    assert [(s.text, s.start_ms) for s in out] == [
        ("Are we missing the cedar in test", 5000),
        ("environment?", 5001),
        ("yes", 5002),
    ]


def test_extended_line_never_ends_before_it_starts() -> None:
    # XERK-2702: a line pushed a millisecond past a repeated ts and then completed in place at
    # that ts must not end before its start.
    m = CaptionMerger(start_ms=0, stable_ms=60_000)
    m.feed([blk("A", "hello there everyone")], 5000)
    m.feed([blk("A", "hello there everyone", "we need to deploy the"), blk("B", "right")], 5000)
    out = m.feed(
        [
            blk("A", "hello there everyone", "we need to deploy the service today"),
            blk("B", "right"),
        ],
        5000,
    )
    assert [(s.text, s.start_ms, s.end_ms) for s in out] == [
        ("we need to deploy the service today", 5001, 5001)
    ]


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


def test_ocr_jitter_on_a_stored_line_above_the_bottom_is_not_stored() -> None:
    """XERK-2597: a stray character OCR'd onto an older line still on screen must not be
    written into its stored segment."""
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "the login timeout is back on staging")], 0)
    m.feed([blk("A", "the login timeout is back on staging")], 1000)
    m.feed([blk("A", "the login timeout is back on staging"), blk("B", "yes")], 1500)
    out = m.feed([blk("A", "the login timeout is back on stagingl"), blk("B", "yes")], 2000)
    assert [s.text for s in out] == []


def test_ocr_jitter_on_the_stored_bottom_line_is_not_stored() -> None:
    """XERK-2597: a stray trailing character on the stored bottom line is not stored. It looks
    like the ASR finishing a word, which is why that growth isn't extended either."""
    m = CaptionMerger(start_ms=0, stable_ms=1000)
    m.feed([blk("A", "the login timeout is back on staging")], 0)
    (seg,) = m.feed([blk("A", "the login timeout is back on staging")], 1000)
    assert m.feed([blk("A", "the login timeout is back on stagingl")], 1500) == []
    assert m.feed([blk("A", "the login timeout is back on staging")], 2000) == []
    assert m.flush(3000) == [] and seg.text == "the login timeout is back on staging"


def test_a_stored_line_extends_only_within_the_extend_window() -> None:
    def run(grow_at: int) -> list[str]:
        m = CaptionMerger(start_ms=0, stable_ms=1000)
        for ts in range(0, grow_at, 5000):  # re-seen, so it stays on screen
            m.feed([blk("A", "let's ship")], ts)
        return [s.text for s in m.feed([blk("A", "let's ship it Friday")], grow_at)]

    assert run(30_000) == ["let's ship it Friday"]
    assert run(30_001) == []


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
    for bad in (float("inf"), float("-inf"), float("nan"), "inf", "nan"):
        with pytest.raises(OcrError, match="non-finite box"):
            parse_result({"lines": [{"text": "x", "box": [0, 0, bad, 1]}]}, (1, 1))
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


def test_a_truncated_large_frame_is_an_ocr_error() -> None:
    """A JPEG whose header parses but whose pixels don't drops the frame as an OcrError."""
    big = _jpeg(1000, 600)  # over STRIP_PIXELS, so it is decoded and cut into strips
    client = _mock_ocr(lambda _r: httpx.Response(200, json={"lines": []}))
    with pytest.raises(OcrError, match="not a readable image"):
        asyncio.run(client.read(big[: len(big) // 2]))


def test_a_lane_survives_a_frame_that_raises(caplog: pytest.LogCaptureFixture) -> None:
    """An unexpected error on one frame must not stop the window's later frames."""
    handled: list[int] = []

    async def handle(header, _jpeg: bytes) -> None:
        if header == 1:
            raise RuntimeError("boom")
        handled.append(header)

    async def run() -> None:
        metrics.reset()
        lane = screen_ws._Lane(handle)
        lane.put(1, b"")
        await asyncio.sleep(0.01)
        lane.put(2, b"")
        await asyncio.sleep(0.01)
        assert not lane.task.done()
        lane.task.cancel()

    with caplog.at_level("ERROR", logger="api.screen"):
        asyncio.run(run())
    assert handled == [2]
    assert metrics.snapshot()["counters"].get("screen.frame_errors") == 1
    assert "screen lane failed on a frame" in caplog.text


def test_cancelling_a_lane_mid_frame_stops_it_without_an_error() -> None:
    """The socket's finally cancels its lanes: cancellation must end the task, not be
    swallowed as a frame error (a widened catch would leak the lane and log on close)."""
    started = asyncio.Event()

    async def handle(_header, _jpeg: bytes) -> None:
        started.set()
        await asyncio.Event().wait()  # an OCR call still in flight

    async def run() -> asyncio.Task:
        metrics.reset()
        lane = screen_ws._Lane(handle)
        lane.put(1, b"")
        await started.wait()
        lane.task.cancel()
        await asyncio.wait({lane.task}, timeout=1)
        return lane.task

    task = asyncio.run(run())
    assert task.cancelled()
    assert "screen.frame_errors" not in metrics.snapshot()["counters"]


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


def test_a_too_dense_full_frame_strip_is_halved_and_re_read() -> None:
    """XERK-2610: a 4x4 camera-off gallery strip holds over tenir-ocr's 16 boxes (initials
    circles and labels), so the whole strip came back too_dense and the roster was empty.
    A refused strip is cut in two overlapping halves and each half is read."""
    from api.screen import ocr as ocr_mod

    spans: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _, h = Image.open(io.BytesIO(request.content)).size
        spans.append(h)
        if h > 300:  # a full strip: a whole tile row and more
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        name = f"Name {len(spans)}"
        return httpx.Response(
            200, json={"lines": [{"text": name, "box": [10, 60, 90, 80]}], "too_dense": False}
        )

    async def run() -> OcrResult:
        return await _mock_ocr(handler).read(_jpeg(1280, 720), full=True)

    got = asyncio.run(run())
    first = ocr_mod.strips(1280, 720)[0]
    top_half, bottom_half = ocr_mod.halves(*first)
    assert spans[:3] == [first[1], top_half[1] - top_half[0], bottom_half[1] - bottom_half[0]]
    # Each half's line is shifted by that half's own top, in reading order.
    assert [ln.box[1] for ln in got.lines][:2] == [top_half[0] + 60, bottom_half[0] + 60]
    assert top_half[1] - bottom_half[0] == ocr_mod.STRIP_OVERLAP


def test_a_frame_of_slides_stops_splitting_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strip that stays too dense (a shared slide) is never halved below MIN_SPLIT_HEIGHT,
    and at most MAX_SPLITS times per frame, so one frame can't flood tenir-ocr."""
    from api.screen import ocr as ocr_mod

    heights: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        heights.append(Image.open(io.BytesIO(request.content)).size[1])
        return httpx.Response(200, json={"lines": [], "too_dense": True})

    def read(width: int, height: int) -> OcrResult:
        heights.clear()
        return asyncio.run(_mock_ocr(handler).read(_jpeg(width, height), full=True))

    # 1080p: six 250 px strips each halve to 149 and then 99 px; none goes below that.
    assert read(1920, 1080).lines == ()
    assert len(heights) == 6 + 2 * 15 and min(heights) > ocr_mod.MIN_SPLIT_HEIGHT // 2
    # 4K: strips are already under MIN_SPLIT_HEIGHT, so a slide costs no extra requests.
    read(3840, 2160)
    assert len(heights) == len(ocr_mod.strips(3840, 2160))
    # The budget binds when it is the smaller bound.
    monkeypatch.setattr(ocr_mod, "MAX_SPLITS", 4)
    read(1920, 1080)
    assert len(heights) == 6 + 2 * 4


def test_a_7x7_gallery_frame_is_halved_down_to_row_height_on_every_strip() -> None:
    """QA on XERK-2610: a 7x7 gallery at 1280x720 is dense down to ~130 px pieces; with too
    small a budget the lower strips got none and a whole tile row was lost. Model: any piece
    taller than 100 px is too dense; every strip must still be read to the end."""
    heights: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        h = Image.open(io.BytesIO(request.content)).size[1]
        heights.append(h)
        if h > 100:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        line = {"text": f"Row {len(heights)}", "box": [10, 40, 90, 56]}
        return httpx.Response(200, json={"lines": [line], "too_dense": False})

    async def run() -> OcrResult:
        return await _mock_ocr(handler).read(_jpeg(1280, 720), full=True)

    got = asyncio.run(run())
    read = [h for h in heights if h <= 100]
    # Strips 375, 375 and 66 px tall: the two full ones halve three times (8 pieces each).
    assert len(got.lines) == len(read) == 2 * 8 + 1
    assert max(ln.box[1] for ln in got.lines) > 600  # the bottom strip was read too


def test_a_small_window_frame_is_split_but_a_caption_crop_never_is() -> None:
    """QA on XERK-2610: an 800x600 window fits one request and was sent whole, so its 3x3
    gallery stayed too_dense. A full frame is halved however small; a crop never is."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        h = Image.open(io.BytesIO(request.content)).size[1]
        calls.append(h)
        if h > 400:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        line = {"text": "Ana Lopez", "box": [10, 40, 90, 56]}
        return httpx.Response(200, json={"lines": [line], "too_dense": False})

    async def run(full: bool) -> OcrResult:
        return await _mock_ocr(handler).read(_jpeg(800, 600), full=full)

    assert asyncio.run(run(False)).lines == () and calls == [600]
    calls.clear()
    assert asyncio.run(run(True)).lines and calls[0] == 600 and len(calls) == 3

    # A caption crop over the cap is cut into strips, but a dense strip is never halved.
    dense: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        dense.append(Image.open(io.BytesIO(request.content)).size[1])
        return httpx.Response(200, json={"lines": [], "too_dense": True})

    async def crop() -> OcrResult:
        return await _mock_ocr(refuse).read(_jpeg(1600, 400), full=False)

    assert asyncio.run(crop()).lines == () and dense == [300, 148]


def test_a_dense_teams_caption_crops_halves_are_read_at_once() -> None:
    """XERK-2798: a dense Teams crop's two halves went to tenir-ocr one after the other, so
    the crop cost two engines' time in a row (1.8 s p50 in a real meeting)."""
    in_flight: list[int] = []
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak
        if Image.open(io.BytesIO(request.content)).size[1] > 200:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        in_flight.append(1)
        peak = max(peak, len(in_flight))
        await asyncio.sleep(0.02)
        in_flight.pop()
        return httpx.Response(200, json={"lines": [], "too_dense": False})

    asyncio.run(_mock_ocr(handler).read(_jpeg(679, 240), split_once=True))
    assert peak == 2


def test_an_oversized_crops_strips_are_still_read_one_at_a_time() -> None:
    """Only a dense crop's two halves go at once: a crop over the pixel cap reading all its
    strips together put dozens of requests ahead of every other crop (XERK-2798 QA)."""
    in_flight: list[int] = []
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak
        in_flight.append(1)
        peak = max(peak, len(in_flight))
        await asyncio.sleep(0.005)
        in_flight.pop()
        return httpx.Response(200, json={"lines": [], "too_dense": False})

    asyncio.run(_mock_ocr(handler).read(_jpeg(1400, 2000), split_once=True))
    assert peak == 1


def test_a_failed_half_cancels_the_other_halfs_request() -> None:
    """A half that fails drops the frame; the other half's request must not hold an engine."""
    cancelled = False
    halves = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled, halves
        if Image.open(io.BytesIO(request.content)).size[1] > 200:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        halves += 1
        if halves == 2:
            return httpx.Response(503)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return httpx.Response(200, json={"lines": [], "too_dense": False})

    async def run() -> None:
        with pytest.raises(OcrError):
            await _mock_ocr(handler).read(_jpeg(679, 240), split_once=True)
        await asyncio.sleep(0)
        # Inside the loop: asyncio.run cancels leftover tasks on exit, which would pass anyway.
        assert cancelled

    asyncio.run(run())


def test_full_frames_leave_ocr_engines_free_for_caption_crops() -> None:
    """XERK-2798: idle windows' full frames, strip after strip, kept all of tenir-ocr's
    engines busy and every caption crop queued behind them. At most two strips are in flight
    across frames, and a dense Teams crop's halves never wait for those slots."""
    from api.screen import ocr as ocr_mod

    in_flight: list[int] = []
    peak = 0
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak
        w, h = Image.open(io.BytesIO(request.content)).size
        if w == 679:  # the caption crop: too dense whole, its halves read
            return httpx.Response(200, json={"lines": [], "too_dense": h > 200})
        in_flight.append(1)
        peak = max(peak, len(in_flight))
        await release.wait()  # full strips hold their slots until the crop is read
        in_flight.pop()
        return httpx.Response(200, json={"lines": [], "too_dense": False})

    async def run() -> None:
        client = _mock_ocr(handler)
        frames = [asyncio.create_task(client.read(_jpeg(1000, 1200), full=True)) for _ in range(4)]
        while len(in_flight) < ocr_mod.FULL_FRAME_REQUESTS:
            await asyncio.sleep(0.005)
        # Every full-frame slot is taken: a crop that waited for one would time out here.
        await asyncio.wait_for(client.read(_jpeg(679, 240), split_once=True), timeout=5)
        release.set()
        await asyncio.gather(*frames)

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert peak == ocr_mod.FULL_FRAME_REQUESTS == 2


def test_a_dense_teams_caption_crop_is_halved_once_and_both_halves_read() -> None:
    """XERK-2652: a real 679x240 Teams pane crop holding 11-12 lines plus avatars came back
    too_dense every time and the whole frame was dropped. A Teams crop is halved once; a
    half still too dense is not halved again (that is a slide, and it bounds the cost)."""
    from api.screen import ocr as ocr_mod

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        h = Image.open(io.BytesIO(request.content)).size[1]
        calls.append(h)
        if h > 200:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        line = {"text": f"Line {len(calls)}", "box": [10, 40, 200, 56]}
        return httpx.Response(200, json={"lines": [line], "too_dense": False})

    async def run(split_once: bool) -> OcrResult:
        return await _mock_ocr(handler).read(_jpeg(679, 240), split_once=split_once)

    got = asyncio.run(run(True))
    top, bottom = ocr_mod.halves(0, 240)
    assert calls == [240, top[1] - top[0], bottom[1] - bottom[0]]
    # Each half's line, shifted into crop coordinates; the result keeps the crop's size.
    assert [(ln.text, ln.box[1]) for ln in got.lines] == [
        ("Line 2", top[0] + 40),
        ("Line 3", bottom[0] + 40),
    ]
    assert (got.width, got.height) == (679, 240)

    # Without split_once (a Zoom crop) the refusal stands: one request, no lines.
    calls.clear()
    assert asyncio.run(run(False)).lines == () and calls == [240]

    # A half that is still too dense is not split again.
    dense: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        dense.append(Image.open(io.BytesIO(request.content)).size[1])
        return httpx.Response(200, json={"lines": [], "too_dense": True})

    async def slide() -> OcrResult:
        return await _mock_ocr(refuse).read(_jpeg(679, 240), split_once=True)

    assert asyncio.run(slide()).lines == () and dense == [240, 144, 144]

    # A crop too short to halve is refused as before.
    dense.clear()
    short = ocr_mod.MIN_SPLIT_HEIGHT
    assert asyncio.run(_mock_ocr(refuse).read(_jpeg(679, short), split_once=True)).lines == ()
    assert dense == [short]


def test_a_line_both_halves_read_differently_is_kept_once() -> None:
    """QA on XERK-2652 (real Paddle): a name header in the halves' overlap read as
    "CO Okafor, Chidi" in one half and "Okafor, Chidi" in the other, and a text line as
    "week so," / "week so.". Both survived exact-text dedupe, so the parser doubled the header
    and gave its lines to the previous speaker. The reading further from its cut is kept."""
    from api.screen import ocr as ocr_mod

    top, bottom = ocr_mod.halves(0, 240)  # (0, 144), (96, 240): overlap rows 96-144
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        h = Image.open(io.BytesIO(request.content)).size[1]
        seen.append(h)
        if len(seen) == 1:
            return httpx.Response(200, json={"lines": [], "too_dense": True})
        if len(seen) == 2:  # top half: header at frame y 100 (44 px above its cut)
            lines = [
                {"text": "Kunal Shah", "box": [46, 10, 200, 28]},
                {"text": "CO Okafor, Chidi", "box": [13, 100, 200, 118]},
                {"text": "week so,", "box": [45, 122, 312, 140]},  # 4 px above the cut
            ]
        else:  # bottom half (frame y = box y + 96)
            lines = [
                {"text": "Okafor, Chidi", "box": [46, 5, 200, 22]},  # 5 px below its cut
                {"text": "week so.", "box": [47, 27, 310, 43]},  # 27 px below its cut
                {"text": "the next line", "box": [45, 80, 300, 98]},
            ]
        return httpx.Response(200, json={"lines": lines, "too_dense": False})

    got = asyncio.run(_mock_ocr(handler).read(_jpeg(679, 240), split_once=True))
    assert [ln.text for ln in got.lines] == [
        "Kunal Shah",
        "CO Okafor, Chidi",
        "week so.",
        "the next line",
    ]
    assert seen == [240, top[1] - top[0], bottom[1] - bottom[0]]


def test_rereads_are_matched_from_half_overlap_not_only_exact_boxes() -> None:
    """The halves' boxes for one line shift by a few px; ~60% overlap is the same line, ~40%
    (a neighbouring row) is not. Pins the 0.5 threshold (QA mutant 0.5 -> 0.99 escaped)."""
    from api.screen import ocr as ocr_mod

    a = OcrLine("week so,", (40, 100, 300, 120))
    same = OcrLine("week so.", (40, 108, 300, 128))  # 12 of 20 rows shared: 60%
    other = OcrLine("next row", (40, 112, 300, 132))  # 8 of 20 rows shared: 40%
    assert ocr_mod._drop_rereads([(a, 0, 4), (same, 1, 20)]) == [same]
    assert ocr_mod._drop_rereads([(a, 0, 4), (other, 1, 20)]) == [other, a]
    # Two lines from the same piece are never rereads of each other.
    assert ocr_mod._drop_rereads([(a, 0, 4), (same, 0, 20)]) == [same, a]


def test_a_large_teams_crop_halves_each_dense_strip_once() -> None:
    """QA on XERK-2652: a Teams crop over STRIP_PIXELS is read in strips, and one halving
    shared by the whole crop left a second dense strip dropped. Each strip halves once."""
    from api.screen import ocr as ocr_mod

    heights: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        heights.append(Image.open(io.BytesIO(request.content)).size[1])
        return httpx.Response(200, json={"lines": [], "too_dense": True})

    assert asyncio.run(_mock_ocr(refuse).read(_jpeg(1200, 800), split_once=True)).lines == ()
    tall = [b - t > ocr_mod.MIN_SPLIT_HEIGHT for t, b in ocr_mod.strips(1200, 800)]
    assert sum(tall) > 1 and len(heights) == sum(3 if x else 1 for x in tall)


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
        # XERK-2881: a control char in the site key (NUL breaks Postgres TEXT) is refused.
        (envelope(header(1, "caption", "tes\x00oro"), tiny_jpeg()), "bad frame header"),
        (envelope(header(1, "caption", "tesoro\n"), tiny_jpeg()), "bad frame header"),
        (envelope(header(1, "caption", "tes\x7foro"), tiny_jpeg()), "bad frame header"),
        (envelope(header(1, "caption", "tes\x85oro"), tiny_jpeg()), "bad frame header"),
        (envelope({**header(1, "caption"), "orgName": "Tes\x00oro"}, tiny_jpeg()), "bad frame"),
        # Format chars pass the contract pattern, but the history org filter refuses them, so a
        # meeting stored under one could never be selected (XERK-2881).
        (envelope(header(1, "caption", "tes\u202eoro"), tiny_jpeg()), "control characters"),
        (envelope(header(1, "caption", "tes\u200boro"), tiny_jpeg()), "control characters"),
    ],
)
def test_bad_frames_are_refused(data: bytes, error: str) -> None:
    with pytest.raises(screen_ws.FrameError, match=error):
        screen_ws.split_frame(data)


@pytest.mark.parametrize("site", ["tesoro", "Site.Key 1", "café-ü", "org❤️", "x" * 128])
def test_printable_site_keys_are_accepted(site: str) -> None:
    head, _ = screen_ws.split_frame(envelope(header(1, "caption", site), tiny_jpeg()))
    assert head.orgSiteKey == site


@pytest.mark.parametrize("name", ["Dev team 👨\u200d💻", "می\u200cخواهم Co", "Co\u00adop"])
def test_an_org_name_may_hold_format_chars(name: str) -> None:
    """Only the key is matched by the history filter; a display name keeps ZWJ/ZWNJ."""
    frame = envelope({**header(1, "caption"), "orgName": name}, tiny_jpeg())
    assert screen_ws.split_frame(frame)[0].orgName == name


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
        # Full frames are read with too-dense strips halved; caption crops never (XERK-2610).
        assert ocr.full[jpeg] is (frame["region"] == "full")
        assert ocr.split_once[jpeg] is False  # a dense Zoom crop is a slide (XERK-2652)
    return start + len(frames)


def test_replayed_zoom_meeting_becomes_a_ready_conversation(screen) -> None:
    """Done-when (XERK-2505): a recorded frame sequence replays into a ready conversation
    with the right speakers and no duplicate lines."""
    from api.main import app

    store, ocr, token, admin = screen
    store.set_speaker_alias(admin.household, admin.user_id, "tesoro", "kunal@tesoroxp.com", "Kunal")
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
    # The window's top band is never the title (a remote desktop's toolbar was): the
    # meeting is titled by topic at delivery (test_screen_topic.py).
    assert conv.meeting_title is None
    assert conv.participants == expected["participants"]
    got = [{"speaker": s.speaker, "text": s.text} for s in conv.segments]
    assert got == expected["lines"]
    assert len({s.text for s in conv.segments}) == len(conv.segments)
    starts = [s.start_ms for s in conv.segments]
    assert starts == sorted(starts) and all(s.end_ms >= s.start_ms for s in conv.segments)
    assert metrics.snapshot()["counters"]["screen.meetings_ended"] == 1
    # XERK-2722: every caption crop's OCR time is metered per platform; full frames aren't.
    latency = metrics.snapshot()["latency_ms"]
    crops = sum(f["region"] == "caption" for f in frames)
    assert latency["screen.ocr_caption.zoom"]["count"] == crops
    assert "screen.ocr_caption.teams" not in latency


def test_the_gap_between_snapshots_fed_to_a_meeting_is_metered() -> None:
    """XERK-2722: the merger's dropped-frame rules hinge on the gap between the snapshots it
    sees, so it is measured within a meeting only, never across the break between two."""

    async def run() -> None:
        metrics.reset()
        t = _tracker(InMemoryConversationStore())
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        await t.on_caption([blk(None, "hello there friend", header=False)], 1100)
        await t.on_caption([blk(None, "hello there friend of mine", header=False)], 4600)
        await t.end(70_000)
        await t.on_caption([blk(None, "a whole new meeting", header=False)], 100_000)
        await t.on_caption([blk(None, "a whole new meeting today", header=False)], 101_000)
        gap = metrics.snapshot()["latency_ms"]["screen.caption_gap.zoom"]
        assert (gap["count"], gap["max_ms"], gap["p50_ms"]) == (3, 3500, 1100)
        # Keyed by platform: a Teams window's gaps never mix into Zoom's.
        teams = _tracker(InMemoryConversationStore())
        teams.meta = ScreenMeta("tesoro", "Tesoro", "teams")
        await teams.on_caption([blk("Ana Ruiz", "hello there")], 0)
        await teams.on_caption([blk("Ana Ruiz", "hello there friend")], 2500)
        latency = metrics.snapshot()["latency_ms"]
        assert latency["screen.caption_gap.teams"]["max_ms"] == 2500
        assert latency["screen.caption_gap.zoom"]["count"] == 3

    asyncio.run(run())


def test_a_clock_held_flat_meters_no_zero_gaps() -> None:
    """XERK-2701: a client clock running ahead makes ``clock`` hold ts flat, and each frame
    then pushed a 0 ms gap into the shared percentiles."""

    async def run() -> None:
        metrics.reset()
        t = _tracker(InMemoryConversationStore())
        # Frames stamped behind the last one (400, 700): the tracker's clock holds them at 1000.
        for ts, text in (
            (0, "hello there"),
            (1000, "hello there friend"),
            (400, "hello there friend of"),
            (700, "hello there friend of mine"),
        ):
            await t.on_caption([blk(None, text, header=False)], t.clock(ts))
        gap = metrics.snapshot()["latency_ms"]["screen.caption_gap.zoom"]
        assert (gap["count"], gap["p50_ms"]) == (1, 1000)

    asyncio.run(run())


def test_a_meeting_logs_its_own_caption_gaps_when_it_ends(caplog: pytest.LogCaptureFixture) -> None:
    """XERK-2724: /metrics lost a meeting's gaps on every deploy; its end log line keeps them."""

    async def run() -> None:
        t = _tracker(InMemoryConversationStore())
        t.meta = ScreenMeta("tesoro", "Tesoro", "teams")
        for ts, text in (
            (0, "hello there"),
            (1000, "hello there friend"),
            (5000, "hello there friend of"),
        ):
            await t.on_caption([blk("Ana Ruiz", text)], ts)
        with caplog.at_level(logging.INFO, logger="api.screen"):
            await t.end(70_000)
        assert "(teams, caption gaps n=2 p50=1000 p95=4000 max=4000 over_3s=1)" in caplog.text
        # The next meeting on this window reports only its own gaps.
        caplog.clear()
        await t.on_caption([blk("Ana Ruiz", "a whole new meeting")], 100_000)
        with caplog.at_level(logging.INFO, logger="api.screen"):
            await t.end(170_000)
        assert "caption gaps n=0" in caplog.text

    asyncio.run(run())


def test_a_meeting_gap_summary_counts_past_3s_and_says_when_capped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """XERK-2724: ``over_3s`` is strictly past the 3 s gate, and gaps past the cap are
    reported as dropped rather than silently left out."""
    assert meetings_mod.gap_summary([3000, 3001]).endswith("over_3s=1")

    async def run() -> None:
        monkeypatch.setattr(meetings_mod, "_MAX_GAPS", 2)
        t = _tracker(InMemoryConversationStore())
        for ts, text in (
            (0, "hello there"),
            (1000, "hello there friend"),
            (2000, "hello there friend of"),
            (8000, "hello there friend of mine"),
        ):
            await t.on_caption([blk(None, text, header=False)], ts)
        with caplog.at_level(logging.INFO, logger="api.screen"):
            await t.end(70_000)
        assert "caption gaps n=2 p50=1000 p95=1000 max=1000 over_3s=0 dropped=1)" in caplog.text
        # The next meeting on this window doesn't inherit the last one's dropped count.
        caplog.clear()
        await t.on_caption([blk(None, "a whole new meeting", header=False)], 100_000)
        await t.on_caption([blk(None, "a whole new meeting today", header=False)], 101_000)
        with caplog.at_level(logging.INFO, logger="api.screen"):
            await t.end(170_000)
        assert "caption gaps n=1 p50=1000 p95=1000 max=1000 over_3s=0)" in caplog.text

    # The real cap holds a long meeting whole: 3 h at 1 frame a second.
    assert meetings_mod._MAX_GAPS >= 3 * 3600
    asyncio.run(run())


def test_a_frame_the_merger_fails_on_is_not_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """XERK-2722: the gap is between snapshots the merger took; a feed that raised is skipped."""

    async def run() -> None:
        metrics.reset()
        t = _tracker(InMemoryConversationStore())
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        assert t.merger is not None
        real = t.merger.feed

        def boom(*a, **k):
            raise RuntimeError("merge bug")

        monkeypatch.setattr(t.merger, "feed", boom)
        with pytest.raises(RuntimeError):
            await t.on_caption([blk(None, "hello there friend", header=False)], 1000)
        monkeypatch.setattr(t.merger, "feed", real)
        await t.on_caption([blk(None, "hello there friend", header=False)], 1500)
        gap = metrics.snapshot()["latency_ms"]["screen.caption_gap.zoom"]
        assert (gap["count"], gap["max_ms"]) == (1, 1500)

    asyncio.run(run())


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
        # Titled by topic at delivery, not from the window (XERK-2506).
        assert item.get("meetingTitle") is None and item["status"] == "ready"
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
    # A failed read is not latency (a timeout would read as one, XERK-2722).
    assert "screen.ocr_caption.zoom" not in metrics.snapshot()["latency_ms"]


def test_only_the_meeting_owners_aliases_rename_its_speakers(screen) -> None:
    """XERK-2606: aliases are per member. Another member's rename of the same org's speaker
    never shows in this member's meeting; the owner's own rename does."""
    from api.main import app

    store, ocr, token, admin = screen
    store.set_speaker_alias(admin.household, "other-member", "tesoro", "kunal@tesoroxp.com", "Q")
    base = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
            replay(client, ws, ocr, FIXTURE["frames"][:6], base)
            client.portal.call(meetings.close_all)
        (item,) = client.get("/conversations").json()
        detail = client.get(f"/conversations/{item['id']}").json()
        assert "kunal@tesoroxp.com" in detail["participants"] and "Q" not in detail["participants"]

        store.set_speaker_alias(admin.household, admin.user_id, "tesoro", "kunal@tesoroxp.com", "K")
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
            replay(client, ws, ocr, FIXTURE["frames"][:6], base + 3_600_000)
            client.portal.call(meetings.close_all)
        newest = client.get("/conversations").json()[0]
        detail = client.get(f"/conversations/{newest['id']}").json()
        assert "K" in detail["participants"] and "kunal@tesoroxp.com" not in detail["participants"]


def test_a_refused_frame_header_is_counted(screen) -> None:
    from api.main import app

    store, ocr, token, admin = screen
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(header(1, "caption", "tes\x00oro"), tiny_jpeg()))
        assert json.loads(ws.receive_text())["type"] == "error"
    assert metrics.snapshot()["counters"]["screen.frames_refused"] == 1


def test_store_failure_on_a_frame_keeps_the_socket(screen, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.main import app

    store, ocr, token, admin = screen

    def boom(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(store, "speaker_aliases", boom)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        jpeg = ocr.register(unique_jpeg(1), crop("Priya Raman", "hello there"))
        ws.send_bytes(envelope(header(int(time.time() * 1000), "caption"), jpeg))
        wait_handled(1)
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"
    assert metrics.snapshot()["counters"]["screen.frame_errors"] == 1


# A forged log line a device could inject through its window's site key (XERK-2879). The
# contract refuses control chars (XERK-2881), but U+2028 passes it and splits a line in many log
# viewers, so the %r logging still has to hold.
FORGED_SITE = "tesoro\u2028ERROR api.screen forged line"


def test_a_site_key_cannot_forge_a_frame_failed_log_line(
    screen, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """orgSiteKey is client input: logged with %r, its newline can't start a line of its own."""
    from api.main import app

    store, ocr, token, admin = screen

    def boom(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(store, "speaker_aliases", boom)
    with caplog.at_level("ERROR", logger="api.screen"):
        with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
            jpeg = ocr.register(unique_jpeg(1), crop("Priya Raman", "hello there"))
            ws.send_bytes(envelope(header(int(time.time() * 1000), "caption", FORGED_SITE), jpeg))
            wait_handled(1)
    (rec,) = [r for r in caplog.records if r.message.startswith("screen frame failed")]
    assert "\u2028" not in rec.getMessage()
    assert repr(FORGED_SITE) in rec.getMessage()


def test_a_site_key_cannot_forge_a_meeting_started_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        t = _tracker(InMemoryConversationStore())
        t.meta = ScreenMeta(FORGED_SITE, "Tesoro", "teams")
        with caplog.at_level(logging.INFO, logger="api.screen"):
            await t.on_caption([blk("Ana Ruiz", "hello there")], 0)

    asyncio.run(run())
    (rec,) = [r for r in caplog.records if r.message.startswith("screen meeting started")]
    assert "\u2028" not in rec.getMessage()
    assert repr(FORGED_SITE) in rec.getMessage()


def test_meeting_start_for_a_deleted_account_closes_the_socket(screen) -> None:
    from api.auth import get_user_store
    from api.main import app

    store, ocr, token, admin = screen
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        get_user_store().delete(admin.user_id)
        jpeg = ocr.register(unique_jpeg(1), crop("Priya Raman", "hello there"))
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
    from api.auth import get_user_store
    from api.main import app

    store, ocr, _, admin = screen
    member = get_user_store().create("capture-op", "capture-op-pass-1", household=admin.household)
    token = device_token(member)
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


def test_screen_socket_closes_when_its_device_token_expires(screen) -> None:
    """XERK-2612: auth runs only at the handshake, so the token's expiry closes the socket."""
    from dataclasses import replace

    from api.auth.tokens import decode_token
    from api.main import app

    _, _, _, admin = screen
    token = device_token(admin, ttl_seconds=2)
    decoded = decode_token(token, secret=settings.auth_secret)
    # Expiry rides on the Principal but is not part of its identity.
    assert decoded.expires_at is not None and decoded == replace(decoded, expires_at=None)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        assert json.loads(ws.receive_text())["type"] == "screen.cadence"
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        assert json.loads(ws.receive_text())["type"] == "pong"
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
        assert closed.value.code == 1008
        assert closed.value.reason == "device token expired"
        assert time.time() >= decoded.expires_at
    assert metrics.snapshot()["counters"].get("screen.token_expired") == 1
    assert screen_ws._revokers.get(admin.user_id) is None


def test_a_socket_that_fails_its_first_send_leaves_no_timer_or_revoker(
    screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XERK-2612 QA: a client gone before the first send must still be cleaned up."""
    from api.main import app

    _, _, _, admin = screen
    # Not 1 s: ``exp`` is whole seconds, so a 1 s token could expire during the handshake
    # itself under load, a real expiry before cleanup that flaked this test (XERK-2701).
    token = device_token(admin, ttl_seconds=3)

    def gone() -> Cadence:
        raise RuntimeError("client went away")

    monkeypatch.setattr(screen_ws, "cadence", gone)
    with TestClient(app) as client:
        with pytest.raises(RuntimeError), client.websocket_connect(f"/ws/screen?token={token}") as ws:
            ws.receive_text()
        assert screen_ws._revokers.get(admin.user_id) is None
        # A leaked timer would fire after exp and count an expiry for a dead socket.
        time.sleep(3.1)
    assert "screen.token_expired" not in metrics.snapshot()["counters"]


def test_a_delete_racing_the_handshake_closes_the_screen_socket(
    screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-registration account re-check closes a socket whose user just went."""
    from api.main import app

    _, _, token, admin = screen
    with TestClient(app) as client:
        # Auth at the handshake passes; the re-check after registering sees the delete.
        monkeypatch.setattr(screen_ws, "_still_authorized", lambda _principal: False)
        with client.websocket_connect(f"/ws/screen?token={token}") as ws:
            assert json.loads(ws.receive_text())["type"] == "screen.cadence"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert (closed.value.code, closed.value.reason) == (1008, "account removed or device token revoked")
    assert screen_ws._revokers.get(admin.user_id) is None


def test_a_client_gone_before_the_first_send_ends_the_socket_quietly(screen) -> None:
    """XERK-2612 QA: a disconnect on the first send is a normal end, not an ASGI error."""
    _, _, token, admin = screen

    class GoneSocket:
        headers: dict[str, str] = {}
        query_params = {"token": token}

        async def accept(self) -> None:
            pass

        async def send_text(self, _text: str) -> None:
            raise WebSocketDisconnect(1006)

    # Returns instead of raising (an escaping disconnect is uvicorn's ASGI error log).
    asyncio.run(screen_ws.screen_ws(GoneSocket()))
    assert screen_ws._revokers.get(admin.user_id) is None


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


def test_a_cut_off_name_replaced_mid_meeting_stores_no_line_twice() -> None:
    """XERK-2695: once a full frame shows "Tim Mahler" for the "Tim Mahl" already in the
    roster, the box's lines re-read under the full name must align with those held."""

    async def run() -> None:
        store = InMemoryConversationStore()
        t = _tracker(store)
        await t.on_full(FullFrame(participants=["Tim Mahl", "Ana Ruiz"]), 0)
        await t.on_caption([blk("Tim Mahl", "we ship on friday")], 0)
        await t.on_caption([blk("Tim Mahl", "we ship on friday"), blk("Ana Ruiz", "okay")], 500)
        await t.on_full(FullFrame(participants=["Tim Mahler", "Ana Ruiz"]), 1000)
        assert t.roster is not None and t.roster.names == ["Ana Ruiz", "Tim Mahler"]
        await t.on_caption(
            [
                blk("Tim Mahler", "we ship on friday"),
                blk("Ana Ruiz", "okay"),
                blk("Tim Mahler", "and test monday"),
            ],
            1500,
        )
        await t.end(10_000)
        (conv,) = store.list("default")
        assert [(s.speaker, s.text) for s in store.get("default", conv.id).segments] == [
            ("Tim Mahl", "we ship on friday"),  # written before the full name was seen
            ("Ana Ruiz", "okay"),
            ("Tim Mahler", "and test monday"),
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
    # Each stored segment once, as last written: an upsert re-emits a segment (same id) the
    # store keeps one row of (XERK-2721).
    once = {s.segment_id: s for s in out}
    return said, [(s.speaker, s.text) for s in once.values()]


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
    m.feed([blk("Ana", *rows)], 0)
    m.feed([blk("Ana", *rows)], 1000)
    out = m.flush(5000)
    assert len(out) == 12 and all(len(s.text) <= 160 for s in out)


def _crafted_rows(rng) -> list[str]:
    # Low-entropy rows defeat difflib's cheap bounds: ~1.5 s per frame unbudgeted.
    return [" ".join(rng.choice("ab") for _ in range(80)) for _ in range(12)]


def test_crafted_caption_frames_spend_at_most_the_windows_work_budget() -> None:
    """XERK-2730: 12 rows of 160 crafted chars cost ~1.5 s CPU a frame, and one socket feeds
    8 windows into a 2-thread pool. A window now spends its burst, then its rate. Work is
    counted, not timed, so this holds on a loaded machine."""
    import random

    from api.screen import merge as merge_mod
    from api.screen.merge import MergeBudget, OverBudget

    rng = random.Random(1)
    budget = MergeBudget()
    spent = 0
    real_charge = budget.charge

    def charge(a, b) -> None:
        nonlocal spent
        real_charge(a, b)
        spent += merge_mod._work(a, b)

    budget.charge = charge  # type: ignore[method-assign]
    m = CaptionMerger(start_ms=0, stable_ms=3000, budget=budget)
    dropped = 0
    for t in range(30):
        try:
            m.feed([blk("Ana", *_crafted_rows(rng))], t * 1000)
        except OverBudget:
            dropped += 1
    assert spent <= merge_mod._WORK_BURST + 29 * merge_mod._WORK_PER_S
    assert dropped >= 25


def test_a_crafted_box_re_sent_unchanged_pays_for_its_reflow() -> None:
    """QA: exact re-reads cost the alignment nothing, but ``_reflow`` compared the box's word
    lists (12 x 80 one-letter words) unmetered, ~130 ms a frame, never refused. Work after
    ``_match`` now puts the bucket in debt, and the next frames are refused until refilled."""
    from api.screen import merge as merge_mod
    from api.screen.merge import MergeBudget, OverBudget

    budget = MergeBudget()
    m = CaptionMerger(start_ms=0, stable_ms=3000, budget=budget)
    rows = [" ".join("a" * 80) for _ in range(12)]
    refused = 0
    for t in range(40):
        try:
            m.feed([blk("Ana", *rows)], t * 200)
        except OverBudget:
            refused += 1
    assert refused > 20
    # Spent at most the burst, the refill and one frame's debt.
    assert budget.left > -merge_mod._WORK_BURST


def test_a_pair_ruled_out_by_the_cheap_bounds_still_pays_its_setup() -> None:
    """QA: rows unlike every known line skip the quadratic ratio, but setting up and
    bounding 12 x 20 pairs a frame still cost ~25 ms; unpaid, they were never refused."""
    from api.screen.merge import MergeBudget

    budget = MergeBudget()
    left = budget.left
    assert budget.ratio("abc", "xyz", 0.5) == 0.0
    assert budget.left == left - 6


def test_crafted_rows_cost_more_work_than_real_ones_of_their_length() -> None:
    from api.screen.merge import _work

    crafted = "a b " * 40
    real = "so we need to check the cluster before the deploy goes out and then push the code to"
    real = (real + " " + real)[:160]
    assert _work(crafted, crafted) > 1.5 * _work(real, real)


def test_a_frame_over_budget_mid_alignment_changes_nothing() -> None:
    """A frame refused leaves the merger as it was: the next one merges as if it never came."""
    from api.screen import merge as merge_mod
    from api.screen.merge import OverBudget

    def fed(m: CaptionMerger) -> list[tuple[str | None, str]]:
        return [(ln.speaker, ln.text) for ln in m._lines]

    clean, cut = (CaptionMerger(start_ms=0, stable_ms=3000) for _ in range(2))
    for m in (clean, cut):
        m.feed([blk("Ana", "can you hear me", "yes I can hear you")], 0)
    before = fed(cut)
    cut._budget.left = 1  # less than any comparison costs; the same ts refills nothing
    with pytest.raises(OverBudget):  # a misread top row needs comparing
        cut.feed([blk("Ana", "can yu hear me", "yes I can hear you now")], 0)
    assert fed(cut) == before
    cut._budget.left = merge_mod._WORK_BURST
    later = [blk("Ana", "can you hear me", "yes I can hear you now", "good")]
    assert [s.text for s in cut.feed(later, 2000)] == [s.text for s in clean.feed(later, 2000)]
    assert fed(cut) == fed(clean)


def test_a_rewrite_check_over_budget_is_refused() -> None:
    """``_revisions`` compares words too (``_claims``): it is charged like the alignment."""
    from api.screen.merge import MergeBudget, OverBudget, _claims, _shown

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    m.feed([blk("Ana", "okay so let's start")], 0)
    shown = _shown([blk("Ana", "Okay, so let's start.")])
    budget = MergeBudget()
    assert _claims(shown, [0], m._lines, [0], budget) == {0: 0}
    budget.left = 1
    with pytest.raises(OverBudget):
        _claims(shown, [0], m._lines, [0], budget)


def test_a_refused_frame_keeps_what_is_left_so_a_costly_real_frame_passes_later() -> None:
    """Emptying the bucket on a refusal let a frame needing more than one second's refill
    be refused on every retry (QA: a dense noisy box)."""
    from api.screen import merge as merge_mod
    from api.screen.merge import MergeBudget, OverBudget

    budget = MergeBudget()
    budget.start(0)
    budget.left = 10
    with pytest.raises(OverBudget):
        budget.charge("a" * 20, "a" * 20)
    assert budget.left == 10
    budget.start(1000)
    assert budget.left == 10 + merge_mod._WORK_PER_S
    budget.charge("a" * 20, "a" * 20)


def test_the_similarity_memo_is_bounded() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    for t in range(40):
        m.feed([blk("Ana", *(f"line {t} {k} said here" for k in range(12)))], t * 1000)
    assert 0 < len(m._budget.memo) <= m._budget.memo_cap


def test_a_caption_frame_over_budget_is_dropped_and_counted(monkeypatch) -> None:
    """The tracker drops a frame the merger refused, meters it, and still meters merge CPU;
    a dropped frame is no snapshot gap (XERK-2722) and holds no meeting open."""
    from api.screen.merge import OverBudget

    async def run() -> None:
        metrics.reset()
        t = _tracker(InMemoryConversationStore())
        await t.on_caption([blk(None, "hello there", header=False)], 0)
        assert t.merger is not None
        real = t.merger.feed

        def refuse(*a, **k):
            raise OverBudget

        monkeypatch.setattr(t.merger, "feed", refuse)
        await t.on_caption([blk(None, "hello there friend", header=False)], 1000)
        assert t.last_caption_ms == 0
        monkeypatch.setattr(t.merger, "feed", real)
        await t.on_caption([blk(None, "hello there friend", header=False)], 1500)
        assert t.last_caption_ms == 1500
        snap = metrics.snapshot()
        assert snap["counters"]["screen.caption_over_budget.zoom"] == 1
        assert snap["latency_ms"]["screen.merge_cpu.zoom"]["count"] == 3
        gap = snap["latency_ms"]["screen.caption_gap.zoom"]
        assert (gap["count"], gap["max_ms"]) == (1, 1500)

    asyncio.run(run())


def test_frames_after_a_meeting_ended_spend_the_windows_budget() -> None:
    """QA: after a meeting ends each frame gets a fresh merger; a bucket per merger had a full
    burst on every frame, so crafted frames re-reading the ended lines were never refused."""

    async def run() -> None:
        metrics.reset()
        t = _tracker(InMemoryConversationStore())
        await t.on_caption([blk("Ana", "can you hear me", "yes I can hear you")], 0)
        await t.end(70_000)
        assert t.merger is None and t._ended_lines
        t.budget.start(70_000)
        t.budget.left = 1
        # A misread of the ended lines needs comparing: refused, no meeting started.
        await t.on_caption([blk("Ana", "can you hear me", "yes I can hear yuo")], 70_000)
        assert metrics.snapshot()["counters"]["screen.caption_over_budget.zoom"] == 1
        assert t.conversation_id is None

    asyncio.run(run())


def test_a_users_windows_share_one_work_budget() -> None:
    """One device token feeds up to MAX_WINDOWS windows per socket, over many sockets: the
    bucket is per user, so more windows buy no more of the shared CPU pool."""
    from api.screen.meetings import ScreenMeetings
    from api.screen.merge import MergeBudget

    reg = ScreenMeetings()
    store = InMemoryConversationStore()

    def put(user: str, org: str) -> MeetingTracker:
        t = MeetingTracker(
            "default",
            user,
            ScreenMeta(org, org, "zoom"),
            store=store,
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        return reg.put(("default", user, org), t)

    a, b, other = put("user-1", "a"), put("user-1", "b"), put("user-2", "a")
    assert a.budget is b.budget and a.budget is not other.budget
    assert a.budget.rate == 2 * MergeBudget().rate  # room for two of the user's meetings
    reg._drop(("default", "user-1", "a"), a)
    assert put("user-1", "a").budget is b.budget  # a reconnect gets no fresh burst


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


def _share_layout(share: tuple[int, int, int]) -> tuple[OcrResult, bytes]:
    """Zoom's share layout, as on the operator's real capture (XERK-2643): a shared screen of
    ``share`` colour whose sidebar and ticket text is name-shaped, beside a strip of video
    tiles. The share shows a participant's name (a Jira assignee) before their tile in
    reading order."""
    img = Image.new("RGB", (1280, 720), (8, 8, 8))
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 60, 1220, 500], fill=share)
    lines = [line(t, 80, 100 + 20 * i, 150, 114 + 20 * i) for i, t in enumerate(_SHARE_TEXT)]
    lines.append(line("TESDEV-518", 300, 100, 360, 112))
    lines.append(line("Sean ONeil", 300, 118, 380, 130))
    lines.append(line("[Mock API] Audit logging", 300, 136, 460, 148))
    for i, name in enumerate(["Priya Raman", "Sean ONeil", "Ana Lopez"]):
        tx = 200 + i * 300
        # Bright video right under the label, the gap 6 px below its box: real labels have no
        # dark pill under them (luma ~200 down to the tile edge on the real capture).
        draw.rectangle([tx, 520, tx + 280, 629], fill=(220, 210, 200))
        lines.append(line(name, tx + 8, 607, tx + 118, 623))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return OcrResult(1280, 720, tuple(lines)), buf.getvalue()


_SHARE_TEXT = ("Filters", "Dashboards", "Goals", "Teams", "Assets")


@pytest.mark.parametrize("share", [(255, 255, 255), (190, 190, 190)])
def test_a_screen_shares_text_is_not_taken_for_participants(share: tuple[int, int, int]) -> None:
    """XERK-2643: every name-shaped line outside the title, toolbar and captions was a tile,
    so the shared Jira board's sidebar and ticket keys joined the roster. A name the share
    shows first must not take the real tile's place. A light-grey share is content too."""
    result, jpeg = _share_layout(share)
    assert parse_full_frame(result, jpeg).participants == ["Priya Raman", "Sean ONeil", "Ana Lopez"]
    # Without the frame's pixels there is no tile geometry to check: every line is kept.
    assert "Dashboards" in parse_full_frame(result).participants


def test_a_label_with_no_pixels_under_it_is_kept() -> None:
    """A box past the frame's right edge has no rows to check: kept, as without a frame."""
    result, jpeg = _share_layout((255, 255, 255))
    off = OcrResult(1280, 720, (*result.lines, line("Lee Park", 1290, 300, 1400, 316)))
    assert "Lee Park" in parse_full_frame(off, jpeg).participants


def test_a_dark_screen_share_still_passes_as_tiles() -> None:
    """Accepted (XERK-2643): a dark-themed share has dark rows under its text like a tile gap."""
    result, jpeg = _share_layout((20, 20, 20))
    assert "Dashboards" in parse_full_frame(result, jpeg).participants


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


def test_a_captioned_meeting_gone_quiet_is_not_warned_but_a_next_one_without_captions_is() -> None:
    """QA D4 and its delta: silence after captions isn't "captions off" (within
    MEETING_SEEN_MS of the last caption), but a later uncaptioned meeting in the same window
    with the toolbar still up is warned once that has passed."""
    from api.contract import ScreenNotice
    from api.screen import meetings as mt

    async def run() -> None:
        sent: list[object] = []

        async def notify(msg: object) -> None:
            sent.append(msg)

        def notices() -> int:
            return sum(isinstance(m, ScreenNotice) for m in sent)

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        await t.on_full(meeting, 0)
        await t.ensure_roster()
        await t.on_caption([blk(None, "hello there", header=False)], 1_000)
        await t.end(61_000)  # silence ended the meeting; the toolbar stays up
        ts = 61_000
        while ts < 1_000 + mt.MEETING_SEEN_MS:  # full frames every 30 s, still quiet
            await t.on_full(meeting, ts)
            ts += 30_000
        assert notices() == 0
        # Ten minutes without a caption: now it is a meeting with captions off.
        end = ts + mt.CAPTIONS_OFF_MS + 30_000
        while ts <= end:
            await t.on_full(meeting, ts)
            ts += 30_000
        assert notices() == 1

    asyncio.run(run())


def test_an_off_roster_speaker_name_atop_a_panel_crop_is_a_header_not_speech() -> None:
    """XERK-2506 QA D1: on a real capture the crop starts with the speaker's name line. With
    a roster that doesn't hold that speaker (screen share: only a strip of tiles) the name was
    stored as a line of speech, and the text credited to the previous speaker."""
    roster = Roster()
    roster.add("Ana Lopez")
    (block,) = parse_caption_crop(
        crop("Bo Chen", "we shipped the release on friday", "then the rollout went fine"),
        roster,
        active="Ana Lopez",
    )
    assert (block.speaker, block.has_header) == (None, True)
    assert block.lines == ["we shipped the release on friday", "then the rollout went fine"]
    # On the roster, the name still names the speaker.
    roster.add("Bo Chen")
    (block,) = parse_caption_crop(crop("Bo Chen", "then the rollout went fine"), roster)
    assert block.speaker == "Bo Chen"
    # A name-shaped row lower down, mid-sentence, is still text.
    blocks = parse_caption_crop(crop("Ana Lopez", "we met", "Acme Global", "said yes"), roster)
    assert [b.speaker for b in blocks] == ["Ana Lopez"]


def test_an_off_roster_speaker_change_mid_crop_is_a_header_not_speech() -> None:
    """QA delta: the speaker change shape — the previous entry's last line, then the next
    speaker's name, then their text — stored the name as the previous speaker's speech."""
    roster = Roster()
    roster.add("Ana Lopez")
    merger = CaptionMerger(start_ms=0, stable_ms=3000)
    stored = []
    for ts in range(0, 10_000, 1000):
        blocks = parse_caption_crop(crop("Ana Lopez", "the build is green."), roster)
        stored += merger.feed(blocks, ts)
    for ts in range(10_000, 30_000, 1000):
        rows = ("the build is green.", "Bo Chen", "we shipped the release")
        blocks = parse_caption_crop(crop(*rows), roster, active="Ana Lopez")
        assert [b.speaker for b in blocks if b.has_header] == [None]
        stored += merger.feed(blocks, ts)
    stored += merger.flush(40_000)
    assert not any("Chen" in s.text for s in stored)
    assert [s.speaker for s in stored if "shipped" in s.text] == [None]
    # A question or an exclamation ends an entry too.
    for tail in ("was that the season?", "what a season!"):
        blocks = parse_caption_crop(crop("Ana Lopez", tail, "Bo Chen", "that festival"), roster)
        assert [(b.speaker, b.lines) for b in blocks] == [
            ("Ana Lopez", [tail]),
            (None, ["that festival"]),
        ]


_PANEL, _NAME_GREY, _TEXT_WHITE = (21, 21, 21), (154, 154, 154), (255, 255, 255)


def panel_crop(*rows: tuple, light: bool = False) -> tuple[OcrResult, bytes]:
    """A Zoom caption-panel crop drawn like the real one (2026): ``(text, is_name)`` rows, each
    name dim grey beside a bright avatar circle, caption text white, on the dark panel. Returns
    the OCR boxes read off it and its JPEG. ``light``: dark text on a light panel. A third
    item ``(text, is_name, avatar)`` overrides the circle: a docs page's grey line has none."""
    font = ImageFont.load_default(size=20)
    bg, name, text = _PANEL, _NAME_GREY, _TEXT_WHITE
    if light:
        bg, name, text = (250, 250, 250), (130, 130, 130), (20, 20, 20)
    img = Image.new("RGB", (700, 34 * len(rows) + 10), bg)
    draw = ImageDraw.Draw(img)
    lines = []
    for i, (row, is_name, *rest) in enumerate(rows):
        y = 6 + 34 * i
        if rest[0] if rest else is_name:
            draw.ellipse((8, y, 36, y + 28), fill=(230, 200, 180))
        draw.text((50, y), row, font=font, fill=name if is_name else text)
        lines.append(OcrLine(row, tuple(float(v) for v in draw.textbbox((50, y), row, font=font))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return OcrResult(img.width, img.height, tuple(lines)), buf.getvalue()


def test_an_off_roster_name_under_an_unpunctuated_entry_is_a_header_not_speech() -> None:
    """XERK-2644: the previous entry ends without punctuation (an interrupted speaker, a
    trailing comma), then the next speaker's dim name line. Only a sentence end let a header
    sit there, so "Shad Tischer" was stored as Tim's speech and Shad's text credited to Tim."""
    roster = Roster()
    roster.add("Tim Mahler")
    for tail in ("just a movie bench my buddies uh was", "and then,", 'he said "go', "so..."):
        ocr, jpeg = panel_crop(
            ("Tim Mahler", True), (tail, False), ("Shad Tischer", True), ("that festival", False)
        )
        blocks = parse_caption_crop(ocr, roster, jpeg=jpeg)
        assert [(b.speaker, b.lines) for b in blocks] == [
            ("Tim Mahler", [tail]),
            ("Shad Tischer", ["that festival"]),  # beside an avatar: named (XERK-2802)
        ], tail
        # Without the pixels only a sentence end makes room for a header (the old rule).
        if not tail.endswith("."):
            (block,) = parse_caption_crop(ocr, roster)
            assert "Shad Tischer" in block.lines
    # QA D4/D5: a name-shaped first line under the name ("Sounds Good"), and a one-word
    # display name, are still the next entry's header.
    for name, text in (("Shad Tischer", "Sounds Good"), ("Shad", "that festival")):
        ocr, jpeg = panel_crop(
            ("Tim Mahler", True), ("and then", False), (name, True), (text, False)
        )
        blocks = parse_caption_crop(ocr, roster, jpeg=jpeg)
        assert [(b.speaker, b.lines) for b in blocks] == [
            ("Tim Mahler", ["and then"]),
            (name, [text]),
        ], name
    # Dark text on a light panel: the name is still the dimmer ink.
    ocr, jpeg = panel_crop(
        ("Tim Mahler", True),
        ("and then", False),
        ("Shad Tischer", True),
        ("ok", False),
        light=True,
    )
    assert [b.speaker for b in parse_caption_crop(ocr, roster, jpeg=jpeg)] == [
        "Tim Mahler",
        "Shad Tischer",
    ]


def test_an_off_roster_name_on_the_crop_s_last_row_is_never_stored_as_speech() -> None:
    """XERK-2644 QA D1: the next speaker's name shows before any of their text, as the crop's
    last row. Only a row with text below it could be a header, so the name was stored as the
    previous speaker's speech and stayed there once the text arrived."""
    roster = Roster()
    roster.add("Tim Mahler")
    merger = CaptionMerger(start_ms=0, stable_ms=3000)
    stored = []
    head = (("Tim Mahler", True), ("and what it was", False), ("Shad Tischer", True))
    for ts in range(0, 10_000, 1000):
        ocr, jpeg = panel_crop(*head)
        blocks = parse_caption_crop(ocr, roster, jpeg=jpeg)
        assert [(b.speaker, b.has_header, b.lines) for b in blocks][-1] == (
            "Shad Tischer",
            True,
            [],
        )
        stored += merger.feed(blocks, ts)
    for ts in range(10_000, 20_000, 1000):
        ocr, jpeg = panel_crop(*head, ("we shipped it", False))
        stored += merger.feed(parse_caption_crop(ocr, roster, jpeg=jpeg), ts)
    stored += merger.flush(30_000)
    assert [(s.speaker, s.text) for s in stored] == [
        ("Tim Mahler", "and what it was"),
        ("Shad Tischer", "we shipped it"),
    ]


def test_dim_rows_reads_no_pixels_it_cannot_trust() -> None:
    """The brightness read gives up (None: the sentence rule decides) on a JPEG of another
    size than OCR read, a crop with no caption-text row to compare against, and a box with a
    non-finite coordinate (it raised and dropped the frame, QA D2)."""
    def dim_rows(jpeg: bytes, ocr: OcrResult) -> set[int] | None:
        groups = zoom.row_groups(ocr.lines)
        return zoom._dim_rows(zoom._grey_pixels(jpeg, ocr, groups), groups)

    ocr, jpeg = panel_crop(("Tim Mahler", True), ("and then", False), ("Shad Tischer", True))
    assert dim_rows(jpeg, ocr) == {0, 2}
    big = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).resize((ocr.width * 2, ocr.height * 2)).save(big, "JPEG")
    assert dim_rows(big.getvalue(), ocr) is None
    names, names_jpeg = panel_crop(("Tim Mahler", True), ("Shad Tischer", True))
    assert dim_rows(names_jpeg, names) is None
    bad = OcrResult(ocr.width, ocr.height, (*ocr.lines, OcrLine("x y", (float("nan"), 0, 5, 5))))
    assert dim_rows(jpeg, bad) is None


def test_a_bright_name_shaped_caption_row_stays_speech_even_after_a_sentence_end() -> None:
    """With the pixels, brightness decides a row below the top: a name-shaped row drawn as
    white caption text ("Acme Global", a slide-ish phrase) is speech, punctuated above or not."""
    roster = Roster()
    roster.add("Ana Lopez")
    ocr, jpeg = panel_crop(
        ("Ana Lopez", True),
        ("we met yesterday.", False),
        ("Acme Global", False),
        ("said yes", False),
    )
    (block,) = parse_caption_crop(ocr, roster, jpeg=jpeg)
    assert (block.speaker, block.lines) == (
        "Ana Lopez",
        ["we met yesterday.", "Acme Global", "said yes"],
    )
    # A JPEG OCR didn't read (another size) or can't decode falls back to the sentence rule.
    for bad in (tiny_jpeg(), b"not a jpeg"):
        blocks = parse_caption_crop(ocr, roster, jpeg=bad)
        assert [b.speaker for b in blocks] == ["Ana Lopez", None]


# --- Teams rewrites a finished entry (XERK-2694) -------------------------------------------
#
# Teams finalises a live-caption entry by rewriting it in place: punctuation, fixed words, and
# the lines re-wrapped. Each case is one entry's draft and final rows from a real meeting
# (XERK-2506 capture, 2026-11-04), same speaker, re-drawn several seconds after the draft.


def _teams_entry(draft: list, final: list, before: list | None = None) -> list:
    """Show ``draft`` until it is stored (stable), then the rewritten ``final``; returns the
    (speaker, text) rows stored, in order, after updates in place."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str | None, str]] = {}
    t = 0
    for snapshot in [*(before or []), draft, final]:
        for _ in range(5):
            for seg in m.feed(snapshot, t):
                stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
            t += 1000
    for seg in m.flush(t + 10_000):
        stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    return [(sp, text) for _, sp, text in sorted(stored.values(), key=lambda v: v[0])]


REWRITES = {
    "rewrapped": (
        [
            blk(
                "Eli",
                "Copy OK let me do this because I",
                "have to",
                "at least I will understand.",
                "Like what am I doing here",
            )
        ],
        [
            blk(
                "Eli",
                "Copy. OK, let me do this because",
                "I have to do it in prod as well, so",
                "at least I will understand.",
                "Like what am I doing here?",
            )
        ],
    ),
    "grown": (
        [blk("Pelin", "Whenever like we need to do it", "like"), blk("Eli", "Oh.")],
        [
            blk("Pelin", "Whenever, like we need to do it", "like with the keto."),
            blk("Eli", "Oh."),
        ],
    ),
    "split": (
        [blk("Eli", "No no no I'm not")],
        [blk("Eli", "No, no, no.", "I'm not gonna enable the kDa in")],
    ),
    "split_no_match": (
        [blk("Eli", "Oh I guess are we missing the")],
        [blk("Eli", "Oh, I guess.", "Are we missing the cedar in test")],
    ),
    "split_punctuated": (
        [blk("Eli", "Alright that's it that's all it was")],
        [blk("Eli", "Alright, that's it.", "That's all it was.")],
    ),
    "entry_above_grew": (
        [
            blk("Eli", "We need to do this"),
            blk("Malcolm", "Are you gonna do it or want me", "to do it?"),
        ],
        [
            blk("Eli", "We need to do this.", "You see the demo website."),
            blk("Malcolm", "Are you gonna do it or want me", "to do it?"),
        ],
    ),
}


@pytest.mark.parametrize("case", REWRITES)
def test_a_rewritten_teams_entry_is_stored_once(case: str) -> None:
    """XERK-2694: the final version updates the draft's segments; nothing is stored twice."""
    draft, final = REWRITES[case]
    stored = _teams_entry(draft, final)
    # Every word of the final version is kept, in order, under its speaker (a line added to
    # an entry above another speaker's is stored when it was seen, after theirs)...
    for speaker in {b.speaker for b in final}:
        want = [
            w for b in final if b.speaker == speaker for ln in b.lines for w in _words(ln).split()
        ]
        got = [w for sp, text in stored if sp == speaker for w in _words(text).split()]
        it = iter(got)
        assert all(w in it for w in want), stored
        # ...and none twice: a line the alignment matched keeps its draft text, which may
        # carry one word the rewrap moved to the next line ("...because I" / "I have to").
        assert len(got) <= len(want) + 1, stored


def _words(text: str) -> str:
    import re

    return " ".join(re.findall(r"[\w']+", text.casefold()))


def test_a_rewritten_entry_keeps_the_drafts_segment_ids() -> None:
    """The final version upserts the draft's stored segments (same ids), so the store keeps
    one copy and its text is the final one."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed([blk("Eli", "No no no I'm not")], t * 1000)
    (draft,) = out
    said_until = draft.end_ms  # the merger re-emits the same Segment object, changed
    out = []
    for t in range(5, 10):
        out += m.feed([blk("Eli", "No, no, no.", "I'm not gonna enable the kDa in")], t * 1000)
    out += m.flush(20_000)
    assert [(s.segment_id == draft.segment_id, s.text) for s in out] == [
        (True, "No, no, no."),
        (False, "I'm not gonna enable the kDa in"),
    ]
    # The revised segment keeps when it was said, not when Teams rewrote it.
    assert out[0].end_ms == said_until


def _cut_back(draft: str, final: str, *, below: bool = True) -> list[tuple[str | None, str, bool]]:
    """Ana's entry shows ``draft`` for 3 frames, then ``final`` (above Bo's new entry when
    ``below``); the stored (speaker, text, is the draft's segment) rows, in order."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str | None, str]] = {}
    after = [blk("Ana", final), *([blk("Bo", "sure that works")] if below else [])]
    for t, frame in enumerate([[blk("Ana", draft)]] * 3 + [after] * 4):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    for seg in m.flush(60_000):
        stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    first = min(stored, key=lambda k: stored[k][0])
    rows = sorted(stored.items(), key=lambda kv: kv[1][0])
    return [(sp, text, k == first) for k, (_, sp, text) in rows]


@pytest.mark.parametrize("below", [True, False])
@pytest.mark.parametrize(
    ("draft", "final"),
    [
        ("Do you have something and then like", "Do you have something?"),
        ("Merge it to dev it's all automatic", "Merge it to Dev."),
        ("Patch right OK so now let's", "Patch right, OK."),
    ],
)
def test_an_entry_teams_cuts_back_as_it_finalises_is_stored_once(
    draft: str, final: str, below: bool
) -> None:
    """XERK-2774 (real shapes, 2026-11-10): Teams finalised the entry by cutting its live tail
    and punctuating it. The rewrite kept under ``_REVISED`` of the draft's words, so it was
    stored again beside the draft, as the bottom entry or above the next one."""
    want = [("Ana", final, True), *([("Bo", "sure that works", False)] if below else [])]
    assert _cut_back(draft, final, below=below) == want


@pytest.mark.parametrize(
    ("draft", "new"),
    [
        # Under half the draft's words.
        ("we need to fix the cluster before friday", "We need to."),
        # Under ``_CUT_WORDS``.
        ("okay so lets start", "Okay so."),
    ],
)
def test_a_short_new_line_opening_like_the_last_is_not_a_cut_back(draft: str, new: str) -> None:
    """XERK-2774's cut-back needs the rows to keep enough of the draft: else a short new line
    opening like the speaker's last one would overwrite it."""
    assert _cut_back(draft, new) == [
        ("Ana", draft, True),
        ("Ana", new, False),
        ("Bo", "sure that works", False),
    ]


def _stored_at(frames: list[tuple[int, list[CaptionBlock]]]) -> list[tuple[str | None, str]]:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, Segment] = {}
    for t, frame in frames:
        for seg in m.feed(frame, t):
            stored[seg.segment_id] = seg
    for seg in m.flush(frames[-1][0] + 70_000):
        stored[seg.segment_id] = seg
    rows = sorted(stored.values(), key=lambda s: s.start_ms)
    return [(s.speaker, s.text) for s in rows if s.text]


def test_a_new_entry_opening_like_her_entry_before_last_is_not_a_cut_back() -> None:
    """XERK-2774 QA: with her last two entries off screen, the draft ran on into "sounds
    good" and a new entry opening like the first was taken for its cut-back, overwriting it."""
    first = [blk("Ana", "okay so lets start the meeting"), blk("Ana", "sounds good")]
    frames = [*[first] * 3, [blk("Ana", "Okay so lets start")]]
    frames += [[blk("Ana", "Okay so lets start with the demo")]] * 3
    assert _stored_at([(t * 1000, f) for t, f in enumerate(frames)]) == [
        ("Ana", "okay so lets start the meeting"),
        ("Ana", "sounds good"),
        ("Ana", "Okay so lets start with the demo"),
    ]


def test_a_cut_dropping_a_whole_row_of_the_draft_stores_nothing_twice() -> None:
    """XERK-2774 QA (fuzz seed 78): a cut dropping the draft's last row left it known but
    unshown, and the entry above's rewrite on the next frame re-stored rows."""
    above = "design logs um we so change or"
    frames = [
        (1000, [blk("Cy", "contract contract me same same", above, "enable we we we or or or")]),
        (5400, [blk(None, "enable we we we or or or", header=False),
                blk("Cy", "code so the check so task then", "the")]),
        (5900, [blk(None, above, "enable we we we or or or", header=False),
                blk("Cy", "Code so the check?")]),
        (6900, [blk(None, "um we change need enable, we", "or.", header=False),
                blk("Cy", "Code so the check?")]),
    ]
    rows = _stored_at(frames)
    assert sum(text == "Code so the check?" for _, text in rows) <= 1, rows
    assert len(rows) <= 6, rows


def test_an_entry_punctuated_as_it_grows_is_stored_once() -> None:
    """XERK-2774 (real Teams frames, 2026-11-11): Teams added a comma where the entry grew
    ("All right" -> "All right, let's..."). Refused as growth, the line was stored short; the
    real meeting's next frame aligned that row to nothing and stored the box again, and here
    the entry below was stored twice ("Let's go to" / "Let's go to Cy")."""
    frames = [
        (0, [blk("Bo", "That's about it."), blk("Ana", "OK."), blk("Ana", "All right")]),
        (2000, [blk("Ana", "OK."), blk("Ana", "All right, let's wrap it up today."),
                blk("Ana", "Let's go to")]),
        (3000, [blk("Ana", "OK."), blk("Ana", "All right, let's wrap it up today."),
                blk("Ana", "Let's go to Cy")]),
        (5000, [blk("Ana", "All right, let's wrap it up today."), blk("Ana", "Let's go to Cy"),
                blk("Cy", "Morning all")]),
    ]
    assert _stored_at(frames) == [
        ("Bo", "That's about it."),
        ("Ana", "OK."),
        ("Ana", "All right, let's wrap it up today."),
        ("Ana", "Let's go to Cy"),
        ("Cy", "Morning all"),
    ]


def test_an_entry_teams_splits_in_two_as_it_finalises_is_stored_once() -> None:
    """XERK-2774 (real Teams frames): Teams finalised Cy's live entry by punctuating it and
    splitting its tail off into a new entry, while re-punctuating Ana's stored one above
    ("Let's go to Cy" -> "...Cy."). Only a ratio match, Ana's row lost to storing the box
    again; and "Hi, good morning." keeps 3 of the draft's 7 words, under the cut-back's half,
    though the rest open Cy's next entry."""
    frames = [
        (0, [blk("Ana", "Let's go to Cy")]),
        (2000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Hi good morning")]),
        (3000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Hi good morning so yesterday we had")]),
        (5000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Hi, good morning."),
                blk("Cy", "So yesterday we had a demo")]),
        (6000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Hi, good morning."),
                blk("Cy", "So yesterday we had a demo with the team")]),
    ]
    assert _stored_at(frames) == [
        ("Ana", "Let's go to Cy."),
        ("Cy", "Hi, good morning."),
        ("Cy", "So yesterday we had a demo with the team"),
    ]


def test_an_entry_finalised_with_a_period_before_it_is_stored_keeps_the_period() -> None:
    """XERK-2774 (real Teams frames): Teams ended Ana's live line with a period before it was
    stored ("Let's go to Cy" -> "Let's go to Cy."). Refused as growth, the line kept its old
    text; on the real capture the next frames' rows then matched it only by ratio and the box
    was stored again (2 rows). These synthetic frames don't reproduce that duplicate, only
    the period that is now kept."""
    ok = blk("Ana", "OK.")
    intro = blk("Ana", "All right, let's get it going today.")
    frames = [
        (0, [ok, intro, blk("Ana", "Let's go to")]),
        (1000, [ok, intro, blk("Ana", "Let's go to Cy")]),
        (3000, [intro, blk("Ana", "Let's go to Cy."), blk("Cy", "Yeah hi good morning")]),
        (4000, [intro, blk("Ana", "Let's go to Cy."), blk("Cy", "Yeah hi good morning so we")]),
        (6000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Yeah. Hi, good morning."),
                blk("Cy", "So we merged the fix")]),
        (7000, [blk("Ana", "Let's go to Cy."), blk("Cy", "Yeah. Hi, good morning."),
                blk("Cy", "So we merged the fix yesterday")]),
    ]
    rows = _stored_at(frames)
    assert [r for r in rows if "go to" in r[1]] == [("Ana", "Let's go to Cy.")], rows


def test_a_split_entry_with_no_known_row_left_on_screen_is_stored_once() -> None:
    """XERK-2774 (real Teams frames): the frame Teams split Ana's entry showed no row the
    merger knew (all scrolled off), so her draft was looked for from her oldest recent line
    ("About red"), matched nothing, and both halves were stored beside the draft."""
    frames = [
        (0, [blk("Ana", "Is there another color here?"), blk("Ana", "Maybe purple?"),
             blk("Ana", "About red.")]),
        (2000, [blk("Ana", "About red."), blk("Bo", "Yeah, I will check it."),
                blk("Ana", "No that's")]),
        (3000, [blk("Ana", "About red."), blk("Bo", "Yeah, I will check it."),
                blk("Ana", "No that's fine I mean I don't")]),
        (5000, [blk("Ana", "No, that's fine."), blk("Ana", "I mean, I don't."),
                blk("Ana", "Maybe it looks")]),
        (6000, [blk("Ana", "No, that's fine."), blk("Ana", "I mean, I don't."),
                blk("Ana", "Maybe it looks different for you")]),
    ]
    assert _stored_at(frames) == [
        ("Ana", "Is there another color here?"),
        ("Ana", "Maybe purple?"),
        ("Ana", "About red."),
        ("Bo", "Yeah, I will check it."),
        ("Ana", "No, that's fine."),
        ("Ana", "I mean, I don't."),
        ("Ana", "Maybe it looks different for you"),
    ]


def test_a_new_entry_after_dropped_frames_does_not_overwrite_her_last_one() -> None:
    """XERK-2774 QA: with nothing aligned, a later draft start offered her 3-word last row,
    and a new entry sharing 2 of its words overwrote it by coverage. A later start is taken
    only as a split now."""
    old = ["Then enable go that the", "overlay we pods?"]
    new = ["We need what me, pods asking", "what, second um uh asking and", "me."]
    frames = [
        (1000, [blk(None, "build need.", header=False), blk("Bo", *old)]),
        (14100, [blk("Bo", *new)]),
    ]
    assert _stored_at(frames) == [(None, "build need."), *(("Bo", r) for r in old + new)]


def test_a_later_draft_start_never_skips_her_first_draft_line() -> None:
    """XERK-2774 QA (fuzz seed 132): a re-wrap read with nothing aligned took its draft from
    her second unshown line, and the first ("did cluster code right locally") was lost."""
    draft = [
        "did cluster code right locally",
        "the push overlay fix process",
        "again itself test locally do",
        "repeat again overlay check",
    ]
    above = blk(None, "overlay repeat overlay check,", "like.", header=False)
    frames = [
        (1000, [above, blk("Bo", "change fix there locally")]),
        (1700, [above, blk("Bo", "change fix there locally we")]),
        (10600, [blk(None, *draft, header=False)]),
        (13100, [blk(None, "locally, the push overlay fix", "process again itself, test",
                     "locally do repeat, again", "overlay check?", header=False)]),
    ]
    assert ("Bo", "did cluster code right locally") in _stored_at(frames)


def test_a_finished_entry_is_not_taken_for_a_split() -> None:
    """XERK-2774 QA: Ana's entry finished long before a new pair of her entries opened like
    it; taken for Teams splitting it, the pair overwrote it. A split needs a changing draft."""
    frames = [[blk("Ana", "I think we are done here")]] * 2
    frames += [[blk("Ana", "I think we are done here.")]] * 15
    frames += [[blk("Ana", "I think."), blk("Ana", "We are done here then")]] * 4
    rows = _stored_at([(t * 1000, f) for t, f in enumerate(frames)])
    assert rows[0] == ("Ana", "I think we are done here."), rows


def test_a_word_moved_up_with_a_comma_above_her_next_line_is_a_rewrite_not_growth() -> None:
    """XERK-2774 QA (fuzz seed 84): Teams re-laid Ana's three rows as two, moving "or" up
    with a comma. Taken as punctuated growth of her first row, the second row kept too few
    of the draft's words to claim it, and both stale rows were stored too."""
    bo = blk("Bo", "build or can itself what check")
    rows = [
        "we cluster design can second locally",
        "locally or so repeat deploy cluster",
        "cluster so or uh",
    ]
    frames = [
        (0, [bo, blk("Ana", rows[0])]),
        (500, [bo, blk("Ana", rows[0], "locally")]),
        (1300, [bo, blk("Ana", rows[0], "locally or so")]),
        (2700, [bo, blk("Ana", *rows[:2])]),
        (3500, [bo, blk("Ana", *rows[:2], "cluster")]),
        (4300, [bo, blk("Ana", *rows[:2], "cluster so or")]),
        (4800, [bo, blk("Ana", *rows)]),
    ]
    rewrite = blk("Ana", "We cluster design can second locally, or", "repeat api cluster, or uh?")
    frames += [(5300 + 500 * k, [bo, rewrite]) for k in range(8)]
    assert _stored_at(frames) == [
        ("Bo", "build or can itself what check"),
        ("Ana", "We cluster design can second locally, or"),
        ("Ana", "repeat api cluster, or uh?"),
    ]


def test_a_split_of_her_live_entry_keeps_the_like_one_she_just_finished() -> None:
    """XERK-2774 QA fuzz: Cy's new entry opened with the words of the one she had just
    finished, then Teams split it. Taken from the finished one as the draft, the split
    overwrote it. A split is only of her newest line."""
    frames = [(t, [blk("Cy", "cluster fix the")]) for t in (0, 500)]
    frames += [(t, [blk("Cy", "Cluster, fix, the.")]) for t in (1000, 1500, 2000)]
    frames += [
        (2500, [blk("Cy", "Cluster, fix, the."), blk("Cy", "cluster")]),
        (3000, [blk("Cy", "Cluster, fix, the."), blk("Cy", "cluster fix")]),
        (3500, [blk("Cy", "Cluster, fix, the."), blk("Cy", "cluster fix the")]),
    ]
    split = [blk("Cy", "Cluster?"), blk("Cy", "Fix the fix locally.")]
    frames += [(t, split) for t in (4000, 4500, 5000, 6000)]
    rows = _stored_at(frames)
    assert rows[0] == ("Cy", "Cluster, fix, the."), rows
    assert ("Cy", "Cluster?") in rows, rows


def test_a_short_entry_punctuated_otherwise_is_not_a_re_read_of_her_last() -> None:
    """XERK-2774 QA fuzz: "Yes??" (her live "yes? yes" finalised) matched her "Yes." above
    by words alone, so her next entry took the live draft and the entry was lost. Short
    entries repeat too often for words alone to say it is the same one."""
    above = blk(None, "check, push same? then need.", "me.", header=False)
    frames = [(0, [above, blk("Ana", "Yes.")]), (500, [above, blk("Ana", "Yes.")])]
    frames += [
        (1000, [blk("Ana", "Yes."), blk("Ana", "yes")]),
        (1700, [blk("Ana", "Yes."), blk("Ana", "yes? yes")]),
    ]
    grow = ["Yes", "Yes second", "Yes second cluster", "Yes second cluster will."]
    frames += [(2200 + 500 * k, [blk("Ana", "Yes??"), blk("Ana", w)]) for k, w in enumerate(grow)]
    frames += [(4200 + 500 * k, [blk("Ana", "Yes??"), blk("Ana", grow[-1])]) for k in range(3)]
    yes = [w for _, w in _stored_at(frames) if w.startswith("Yes")]
    assert len(yes) == 3, yes


def test_a_new_entry_like_an_old_one_is_not_a_rewrite() -> None:
    """Only a recent line of the same speaker the box no longer shows can be revised: the
    same words from someone else, or after the revision window, are new lines."""
    stored = _teams_entry(
        [blk("Ana", "okay"), blk("Bo", "so we ship on friday")],
        [blk("Ana", "okay"), blk("Bo", "so we ship on friday"), blk("Cy", "okay so we ship")],
    )
    assert stored == [("Ana", "okay"), ("Bo", "so we ship on friday"), ("Cy", "okay so we ship")]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(0, 5000, 1000):
        out += m.feed([blk("Ana", "we ship on friday")], t)
    out += m.feed([blk("Bo", "sure")], 40_000)
    out += m.feed([blk("Bo", "sure"), blk("Ana", "we ship on friday.", "Then lunch")], 41_000)
    out += m.flush(60_000)
    assert [s.text for s in out] == [
        "we ship on friday",
        "sure",
        "we ship on friday.",
        "Then lunch",
    ]


@pytest.mark.parametrize(
    ("first", "then"),
    [
        ([blk("Ana", "sounds good")], [blk("Ana", "okay", "sounds good"), blk("Cy", "yes")]),
        ([blk("Eli", "Yes.")], [blk("Eli", "OK.", "Yes."), blk("Bo", "great")]),
    ],
)
def test_a_repeat_below_a_new_line_after_dropped_frames_is_not_a_rewrite(
    first: list, then: list
) -> None:
    """QA (XERK-2694): frames dropped, then the same speaker's new line above a repeat of her
    last one. Nothing aligns; the repeat must not revise the old line away."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed(first, t * 1000)
    out += m.feed(then, 7000)
    out += m.flush(20_000)
    said = [ln for b in first for ln in b.lines] + [ln for b in then for ln in b.lines]
    assert [s.text for s in out] == said


SAME_TEXT_ENTRIES = {
    "two_alike": (
        [["okay."]] * 5 + [["okay.", "okay."]] * 5 + [["okay, ship it", "okay."]] * 6,
        ["okay, ship it", "okay."],
    ),
    "repeat_later": (
        [["ok."], ["ok.", "notes"], ["ok.", "notes", "ok."]]
        + [["ok, so ship it", "notes", "ok."]] * 6,
        ["ok, so ship it", "notes", "ok."],
    ),
    # QA: growth onto the older of two alike entries is still taken...
    "older_grows": (
        [["yes we can"]] * 5
        + [["yes we can", "yes we can"]] * 5
        + [["yes we can okay", "yes we can"]] * 6,
        ["yes we can okay", "yes we can"],
    ),
    # QA fuzz: growth reading only half like its draft (a "misread") is still a revision.
    "grows_like_a_misread": (
        [["notes"]] * 5 + [["notes", "notes"]] * 5 + [["notes the think", "notes"]] * 6,
        ["notes the think", "notes"],
    ),
    # QA: the bottom twin growing is the live line growing, not a revision of the top one.
    "bottom_twin_grows": (
        [["notes"]] * 5 + [["notes", "notes"]] * 5 + [["notes", "notes the think"]] * 6,
        ["notes", "notes the think"],
    ),
    # QA: growth seen before its twin was said, the twin then showing below it.
    "grew_then_twin": (
        [["sure", "it think"]] * 5
        + [["sure is", "it think"]] * 3
        + [["sure is", "it think", "sure"]] * 6,
        ["sure is", "it think", "sure"],
    ),
    # XERK-2727: the twin below misread ("0kay."): the live twin takes the misread (as any live
    # bottom row), and the grown row above still revises its line.
    "twin_misread": (
        [["okay.", "okay."]] * 4 + [["okay, ship it", "0kay."]] * 5,
        ["okay, ship it", "0kay."],
    ),
    # ...and a garbled top row over a repeat said since is not stored.
    "garbled_top": (
        [["yes."]] * 5 + [["~es.", "yes."]] + [["yes.", "yes."]] * 5,
        ["yes.", "yes."],
    ),
}


@pytest.mark.parametrize("case", SAME_TEXT_ENTRIES)
def test_a_grown_entry_above_a_same_text_entry_is_stored_once(case: str) -> None:
    """XERK-2719: one speaker's entries, each its own block, two of them reading alike. The top
    one grows: it revises its draft, and the identical entry below stays a re-read. Pairing
    the grown row with the newest alike line stored the row below anew on every frame."""
    frames, said = SAME_TEXT_ENTRIES[case]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str]] = {}
    for t, rows in enumerate(frames):
        for seg in m.feed([blk("Ana", row) for row in rows], t * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.text)
    for seg in m.flush(len(frames) * 1000 + 10_000):
        stored[seg.segment_id] = (seg.start_ms, seg.text)
    assert [text for _, text in sorted(stored.values())] == said


@pytest.mark.parametrize(
    ("gap", "strays"),
    [
        (0, ["s"]),  # one frame: two reads in a row must agree
        (70, ["s", "s"]),  # two frames on a line first seen 70 s ago: past _REVISE_MS
        (10, ["s", "c", "s"]),  # a clean frame between resets the held read
    ],
)
def test_a_stray_token_over_a_twin_row_is_not_stored(gap: int, strays: list) -> None:
    """QA (XERK-2719): a grown twin row is taken only when two frames in a row read it alike,
    on a recent line (as ``_reflow``'s rule): one frame of "like l o" over "like" was stored
    for good."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    clean = [blk("Ana", "so we said", "like", "like")]
    stray = [blk("Ana", "so we said", "like l o", "like")]
    frames = [clean] * (5 + gap) + [stray if k == "s" else clean for k in strays] + [clean] * 6
    out = []
    for t, blocks in enumerate(frames):
        out += m.feed(blocks, t * 1000)
    out += m.flush(len(frames) * 1000 + 10_000)
    assert "like l o" not in {s.text for s in out}


def test_a_row_reading_another_line_exactly_is_not_revised_into_it() -> None:
    """QA fuzz seed 183 (XERK-2719): "okay, so it should" re-reads entry 1 but pairs as a
    misread of the "okay so" it starts with; revising that line stored entry 1 twice."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    first = [blk("Ana", "okay, so it should"), blk("Ana", "is yes"), blk("Ana", "okay so")]
    grown = [
        blk("Ana", "okay, so it should"),
        blk("Ana", "is yes should should we"),
        blk("Ana", "okay so"),
    ]
    out = m.feed(first, 14_700)
    for ts in (17_400, 18_100, 18_600):
        out += m.feed(grown, ts)
    out += m.flush(80_000)
    texts = [s.text for s in {s.segment_id: s for s in out}.values()]
    assert texts.count("okay, so it should") == 1


def test_a_carried_line_growing_above_its_twin_is_stored_as_new() -> None:
    """QA (XERK-2719): the ended meeting's "notes" grows above a "notes" said since. It can't
    be revised (its segment is the ended meeting's), so its growth is a new line, not lost."""
    m1 = CaptionMerger(start_ms=0, stable_ms=3000)
    for t in range(5):
        m1.feed([blk("Ana", "notes")], t * 1000)
    m1.flush(5000)
    m2 = CaptionMerger(start_ms=65_000, stable_ms=3000, carried=m1.recent())
    stored: dict[str, tuple[int, str]] = {}
    frames = [["notes", "notes"]] * 5 + [["notes the think", "notes"]] * 6
    for k, rows in enumerate(frames):
        for seg in m2.feed([blk("Ana", row) for row in rows], 65_000 + k * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.text)
    for seg in m2.flush(90_000):
        stored[seg.segment_id] = (seg.start_ms, seg.text)
    assert [text for _, text in sorted(stored.values())] == ["notes the think", "notes"]


def test_a_carried_twin_growing_never_re_stores_a_carried_line() -> None:
    """QA (XERK-2719): the twin guards skip carried lines. Refusing the misread for a carried
    "notes" moved its growth onto the new "notes" and stored Bo's carried "sure" again."""
    m1 = CaptionMerger(start_ms=0, stable_ms=3000)
    for t in range(5):
        m1.feed([blk("Ana", "notes"), blk("Bo", "sure")], t * 1000)
    m1.flush(5000)
    m2 = CaptionMerger(start_ms=65_000, stable_ms=3000, carried=m1.recent())
    out = []
    for k in range(11):
        top = "notes" if k < 5 else "notes the think"
        out += m2.feed(
            [blk("Ana", top), blk("Bo", "sure"), blk("Ana", "notes")], 65_000 + k * 1000
        )
    out += m2.flush(90_000)
    assert "sure" not in {s.text for s in out}


def _stored(m: CaptionMerger, frames: list, t0: int = 0) -> list[tuple[str | None, str]]:
    """Feed one frame a second from ``t0``, flush, and return (speaker, text) in start order."""
    stored: dict[str, tuple[int, str | None, str]] = {}
    for k, blocks in enumerate(frames):
        for seg in m.feed(blocks, t0 + k * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    for seg in m.flush(t0 + len(frames) * 1000 + 10_000):
        stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    return [(sp, text) for _, sp, text in sorted(stored.values())]


def test_a_same_speaker_entry_inserted_above_another_speakers_is_stored_once() -> None:
    """XERK-2727: Teams shows Ana's new entry between her stored one and Bo's. Unpaired under
    a header it cost ``_UNPAIRED``, so Bo's "fine" below aligned nothing and was stored again."""
    frames = (
        [[blk("Ana", "okay so")]]
        + [[blk("Ana", "okay so"), blk("Bo", "fine")]] * 3
        + [[blk("Ana", "okay so"), blk("Ana", "okay so lets go"), blk("Bo", "fine")]] * 4
    )
    # In on-screen order: the new entry is stored above Bo's (``_Line.order_ms``).
    assert _stored(CaptionMerger(start_ms=0, stable_ms=3000), frames) == [
        ("Ana", "okay so"),
        ("Ana", "okay so lets go"),
        ("Bo", "fine"),
    ]


@pytest.mark.parametrize(
    "case",
    ["reread_row_not_inserted", "misread_twin_not_reread_below", "earlier_misread_copy_is_no_twin"],
)
def test_qa_fuzz_panels_store_what_main_did(case: str) -> None:
    """QA fuzz dumps (XERK-2727, synthetic) that this change first stored worse than main:
    - reread_row_not_inserted: a row re-reading a stored line was taken as a new entry
      inserted after its start (``_inserted_entry``); the line was stored twice and the word
      wrapped below it lost.
    - misread_twin_not_reread_below: ``_reread_below`` counted a one-char-misread known line
      as a twin and re-stored a 4-line box.
    - earlier_misread_copy_is_no_twin: the same, for ``_reread_below``'s earlier copy (a
      misread one let a garbled row pair as a re-read); stored 26 extra words, main 18.
    ``stored`` is what main (64b94a3) stores; frames are one a second, null = dropped."""
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "screen" / "merge_fuzz.json"
    data = json.loads(path.read_text())[case]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str | None, str]] = {}
    for k, frame in enumerate(data["frames"]):
        if frame is None:
            continue
        blocks = [CaptionBlock(sp, head, list(rows)) for sp, head, rows in frame]
        for seg in m.feed(blocks, k * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    for seg in m.flush(len(data["frames"]) * 1000 + 10_000):
        stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    assert [[sp, text] for _, sp, text in sorted(stored.values()) if text] == data["stored"]


def test_an_entry_grown_below_its_twin_is_not_an_inserted_entry() -> None:
    """QA (XERK-2727): two "yes" entries, the second growing above a live one. The first
    "yes" row aligns to the second "yes" (leading lines skip free), and the grown row was
    taken as a new entry inserted after it: a third "yes" was stored, the growth after."""
    bo = blk("Bo", "yes we can do that")
    frames = (
        [[bo, blk("Cy", "yes")]] * 2
        + [[bo, blk("Cy", "yes"), blk("Cy", "yes")]] * 4
        + [[bo, blk("Cy", "yes"), blk("Cy", "yes"), blk("Cy", "yes we can")]] * 2
        + [[bo, blk("Cy", "yes"), blk("Cy", "yes maybe"), blk("Cy", "yes we can do")]]
        + [[bo, blk("Cy", "yes"), blk("Cy", "yes maybe please"), blk("Cy", "yes we can do that")]]
        * 6
    )
    assert _stored(CaptionMerger(start_ms=0, stable_ms=3000), frames) == [
        ("Bo", "yes we can do that"),
        ("Cy", "yes"),
        ("Cy", "yes maybe please"),
        ("Cy", "yes we can do that"),
    ]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("okay.", "0kay.", True),  # one char of one word misread
        ("okay.", "Okay!", True),  # case and punctuation aside
        ("ship it today", "ship it monday", False),  # another word
        ("okay ship", "0kay shlp", False),  # two words misread: more than a twin
        # QA: same word count, ratio >= 0.75, different lines (re-stored a 7-line box).
        ("let me check the logs maybe too", "let me check the logs too again", False),
        ("okay", "okay so", False),  # grown by a word
    ],
)
def test_only_a_one_char_misread_is_a_twin(a: str, b: str, same: bool) -> None:
    from api.screen.merge import _norm, _same_text

    assert _same_text(_norm(a), _norm(b)) is same


def test_a_carried_entry_grown_above_a_carried_line_stores_only_the_growth() -> None:
    """XERK-2727: a panel left on screen re-read with the ended meeting's "green." grown. The
    growth is a new line (carried lines never grow); "and the notes" below is the carried one,
    not stored again (``_revisions`` skips carried lines, so it aligned as new)."""
    m1 = CaptionMerger(start_ms=0, stable_ms=3000)
    _stored(m1, [[blk("Ana", "green."), blk("Ana", "and the notes")]] * 5)
    m2 = CaptionMerger(start_ms=70_000, stable_ms=3000, carried=m1.recent())
    frames = [[blk("Ana", "green, so ship it"), blk("Ana", "and the notes")]] * 7
    assert _stored(m2, frames, 70_000) == [("Ana", "green, so ship it")]


def test_a_new_line_sharing_a_word_with_the_last_is_not_a_rewrite() -> None:
    """Revising needs most of the old line's words: one shared word is a new line."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Ana", "let me check the logs")], t * 1000)
    out += m.feed([blk("Ana", "check this out")], 7000)
    out += m.flush(20_000)
    assert [s.text for s in out] == ["let me check the logs", "check this out"]


def test_a_rewrap_moving_a_word_onto_a_matched_row_keeps_it() -> None:
    """QA (XERK-2694): a header-less entry whose rewrite moves "how" up onto a row the
    alignment still matches; that row takes the final text, so no word is lost."""
    draft = [
        blk(None, "yes so so enable should like we", "the cluster not need pods it the", header=False),
        blk(None, "how right right in overlay", header=False),
    ]
    draft = [blk(None, *draft[0].lines, *draft[1].lines, header=False)]
    final = [
        blk(
            None,
            "Yes so enable should like we the",
            "cluster not need pods it, the how",
            "right in overlay.",
            header=False,
        )
    ]
    stored = _teams_entry(draft, final, before=[[blk("Eli", "so")]])
    words = " ".join(_words(text) for _, text in stored)
    assert "the how right in overlay" in words, stored


@pytest.mark.parametrize(
    "misread_top",
    [
        "so enable should like we",  # clipped while scrolling
        "Yes so so enab|e shouId like we",  # misread
        "Yes so so enable should like we'",  # stray character
    ],
)
def test_ocr_jitter_on_a_rewritten_entrys_matched_row_is_not_stored(misread_top: str) -> None:
    """QA (XERK-2694): a matched row of a rewritten entry takes the final text only when the
    rewrap moved words across its line break. A misread on the one frame the rewrite is seen
    must not overwrite the stored line (XERK-2597); later clean frames can't repair it."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["yes so so enable should like we", "the cluster not need pods it the", "how right right in overlay"]
    final = ["Yes so so enable should like we", "cluster not need pods it, the how", "right in overlay."]
    stored: dict[str, str] = {}
    frames = [[blk("Eli", "so")]] * 5 + [[blk(None, *draft, header=False)]] * 5
    frames += [[blk(None, misread_top, *final[1:], header=False)]]
    frames += [[blk(None, *final, header=False)]] * 5
    for t, frame in enumerate(frames):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = seg.text
    for seg in m.flush(60_000):
        stored[seg.segment_id] = seg.text
    texts = list(stored.values())
    assert "yes so so enable should like we" in texts, stored
    # The rewrite's other rows still land once read cleanly: nothing moved by the rewrap lost.
    assert texts[-2:] == ["cluster not need pods it, the how", "right in overlay."], stored


@pytest.mark.parametrize(
    "glitched",
    [
        "Yes so so enable should we",  # a word lost
        "Yes so so enable cluster should like we",  # a word gained that the next line has
    ],
)
def test_a_one_frame_misread_of_a_rewritten_row_is_never_stored(glitched: str) -> None:
    """QA (XERK-2694): a matched row of a rewritten entry takes new words only when two
    frames read them alike, so a misread on the rewrite frame is never stored, while the
    rewrite's own changes still land."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["yes so so enable should like we", "the cluster not need pods it the", "how right right in overlay"]
    final = ["Yes so so enable should like we", "cluster not need pods it, the how", "right in overlay."]
    stored: dict[str, str] = {}
    frames = [[blk("Eli", "so")]] * 5 + [[blk(None, *draft, header=False)]] * 5
    frames += [[blk(None, glitched, *final[1:], header=False)]]
    frames += [[blk(None, *final, header=False)]] * 5
    for t, frame in enumerate(frames):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = seg.text
    for seg in m.flush(60_000):
        stored[seg.segment_id] = seg.text
    texts = list(stored.values())
    assert glitched not in texts and "yes so so enable should like we" in texts, stored
    assert texts[-2:] == ["cluster not need pods it, the how", "right in overlay."], stored

def _entries(*texts: str) -> list[CaptionBlock]:
    """A Teams panel of one speaker's entries, each under its own header."""
    return [blk("Ana", t) for t in texts]


def test_a_middle_entry_finishing_after_the_next_began_is_stored() -> None:
    """XERK-2721: Teams adds words to an entry above a newer one; the row rules let a row
    above the bottom keep only its text (OCR jitter), so "are done" was lost."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    for t in range(5):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t * 1000)
    draft = {s.text: s.segment_id for s in out}
    stored = {s.segment_id: s.text for s in out}
    for t in range(5, 10):
        for s in m.feed(_entries("green.", "and the notes are done", "for the api"), t * 1000):
            stored[s.segment_id] = s.text
    for s in m.flush(20_000):
        stored[s.segment_id] = s.text
    assert list(stored.values()) == ["green.", "and the notes are done", "for the api"]
    assert stored[draft["and the notes"]] == "and the notes are done"  # same segment


def test_a_reworded_top_entry_keeping_its_words_is_stored() -> None:
    """XERK-2721: a rewrite inserting words into the top entry ("red now not") was dropped
    as a glitch above the aligned rows."""
    stored = _teams_entry(
        _entries("the build is green.", "thanks."),
        _entries("the build is red now not green", "thanks."),
    )
    assert stored == [("Ana", "the build is red now not green"), ("Ana", "thanks.")]


def test_a_one_frame_growth_of_a_stored_entry_is_never_stored() -> None:
    """Words added to a stored row the alignment can't match are taken only once two frames
    in a row read them: one misread frame never sticks."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    for t in range(5):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t * 1000)
    out += m.feed(_entries("green.", "and the notes are done", "for the api"), 5000)
    for t in range(6, 10):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t * 1000)
    out += m.flush(20_000)
    assert [s.text for s in out] == ["green.", "and the notes", "for the api"]


def _replay(frames: list[list[CaptionBlock]]) -> list[tuple[str | None, str]]:
    """Feed one snapshot a second; the (speaker, text) rows stored, as last written, blank
    ones (dropped lines) left out."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, Segment] = {}
    for t, frame in enumerate(frames, 1):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = seg
    for seg in m.flush((len(frames) + 2) * 1000):
        stored[seg.segment_id] = seg
    return [(s.speaker, s.text) for s in sorted(stored.values(), key=lambda s: s.start_ms) if s.text]


def test_one_frame_of_a_stray_glyph_on_a_matched_stored_entry_is_never_stored() -> None:
    """XERK-2701: a stored row above the bottom re-read once with a stray glyph ("notes l"
    matches "and the notes" closely) was extended on that single frame and kept it."""
    panel = _entries("green.", "and the notes", "for the api")
    stray = _entries("green.", "and the notes l", "for the api")
    assert _replay([panel] * 5 + [stray] + [panel] * 4) == [
        ("Ana", "green."),
        ("Ana", "and the notes"),
        ("Ana", "for the api"),
    ]


def test_a_word_re_flowing_up_from_the_next_row_is_not_stored_twice() -> None:
    """XERK-2701: Teams re-flows "today" up from the row below; the one-frame extension took
    it onto the row above while the row below kept it, so it was stored twice."""
    before = _entries("we ship the build", "today on friday")
    after = _entries("we ship the build today", "on friday")
    words = " ".join(text for _, text in _replay([before] * 5 + [after] * 5)).split()
    assert words.count("today") == 1


def test_a_rewrite_with_fewer_rows_than_its_draft_drops_the_folded_row() -> None:
    """XERK-2694 accepted it, XERK-2701 fixes it: a rewrite joining the draft's rows into one
    left the second draft row stored beside the final. It is re-emitted blank (no delete)."""
    draft = [blk("Ana", "we did it in test first so", "nothing broke")]
    final = [blk("Ana", "We did it in test first so nothing broke.")]
    assert _replay([draft] * 4 + [final] * 5) == [
        ("Ana", "We did it in test first so nothing broke.")
    ]


@pytest.mark.parametrize(
    ("frames", "want"),
    [
        # Zoom: the next speaker finalized "and so do" mid-sentence, then "I" landed.
        (
            [[blk("Ana", "and so do")]] * 5
            + [[blk("Ana", "and so do I")]] * 5
            + [[blk("Ana", "and so do I"), blk("Bo", "great")]] * 5,
            "and so do I",
        ),
        # Teams: Bo's entry below finalized it; Teams then finished it with a 1-char word.
        (
            [[blk("Ana", "the meeting is in room")]] * 2
            + [[blk("Ana", "the meeting is in room"), blk("Bo", "thanks.")]] * 4
            + [[blk("Ana", "The meeting is in room 5."), blk("Bo", "thanks.")]] * 5,
            "The meeting is in room 5.",
        ),
    ],
)
def test_a_speakers_newest_line_still_takes_a_one_char_last_word(frames, want) -> None:
    """XERK-2701 QA: refusing 1-char growth on one frame lost "I" / "5" for good, since
    ``_inserted`` refuses it on every frame. Only a line she has said on from refuses it."""
    assert ("Ana", want) in _replay(frames)


_FOLD_DRAFT = [blk("Ana", "we did it in test first so", "nothing broke")]
_FOLD_FINAL = [blk("Ana", "We did it in test first so nothing broke.")]


@pytest.mark.parametrize("frames", [1, 2, 3])
def test_a_garbled_rewrite_frame_is_not_kept_by_the_fold(frames: int) -> None:
    """XERK-2701 QA: dropping the folded draft row on the rewrite frame left the next correct
    read aligned onto the garbled line; holding the drop a frame moved that to a misread read
    twice. Once the row is dropped, the claiming line takes two later reads that agree."""
    garbled = [blk("Ana", "We did it in tcst flrst so nothing broke.")]
    stored = _replay([_FOLD_DRAFT] * 4 + [garbled] * frames + [_FOLD_FINAL] * 5)
    assert all("tcst" not in text for _, text in stored)
    assert ("Ana", "We did it in test first so nothing broke.") in stored


def test_a_jittered_read_right_after_a_fold_is_never_stored() -> None:
    """One jittered re-read ("nothinq") of the claiming line on the frame the fold is taken
    is never stored: the rewritten-row window needs two reads that agree."""
    jitter = [blk("Ana", "We did it in test first so nothinq broke.")]
    stored = _replay([_FOLD_DRAFT] * 4 + [_FOLD_FINAL, jitter] + [_FOLD_FINAL] * 5)
    assert stored == [("Ana", "We did it in test first so nothing broke.")]


def test_a_fold_is_taken_over_a_frame_showing_only_another_speaker() -> None:
    """The next frame missing Ana's block (only Cy shows) still takes the fold: the rewrite
    seen again after it is stored once. Holding the drop until the claiming line is seen
    again kept the duplicate here (QA, accepted trade-off: a draft re-shown after such a
    frame, a reverted rewrite, is stored again out of order)."""
    stored = _replay([_FOLD_DRAFT] * 4 + [_FOLD_FINAL, [blk("Cy", "ok")]] + [_FOLD_FINAL] * 5)
    assert [t for sp, t in stored if sp == "Ana"] == ["We did it in test first so nothing broke."]


def test_two_like_misreads_long_after_a_fold_are_not_taken() -> None:
    """The window a fold opens closes after ``_REVISE_MS``: two identical misreads of the
    line read later are jitter, not a rewrite."""
    jitter = [blk("Ana", "We did it in test first so nothinq broke.")]
    frames = [_FOLD_DRAFT] * 4 + [_FOLD_FINAL] * 34 + [jitter] * 2 + [_FOLD_FINAL] * 5
    assert _replay(frames) == [("Ana", "We did it in test first so nothing broke.")]


def test_a_draft_row_shown_again_is_never_folded() -> None:
    """A draft row on screen the frame after the rewrite is real, not a stale copy."""
    stored = _replay([_FOLD_DRAFT] * 4 + [_FOLD_FINAL] + [_FOLD_DRAFT] * 6)
    assert ("Ana", "nothing broke") in stored


def test_no_draft_row_is_folded_when_the_rewrite_revises_nothing() -> None:
    """A fold counts only with the revision it belongs to: here Bo's unshown "ok" voids it
    (the box shows the newest lines), so Ana's "nothing broke" must stay stored."""
    draft = [blk("Ana", "we did it in test first so", "nothing broke")]
    stored = _replay(
        [draft] * 4
        + [[*draft, blk("Bo", "ok")]] * 4
        + [[blk("Ana", "We did it in test first so nothing broke.")]] * 3
        + [[blk("Ana", "We did it in test first so nothing broke."), blk("Bo", "ok")]] * 3
    )
    assert ("Ana", "nothing broke") in stored


def test_growth_matching_another_speakers_unchanged_row_below_is_taken() -> None:
    """Only words the row below no longer starts with moved up: Bo's "today we test" still
    reads as before, so Ana's "today" is her own growth, kept as on main."""
    stored = _replay(
        [[blk("Ana", "we ship the build")]] * 4
        + [[blk("Ana", "we ship the build"), blk("Bo", "today we test")]] * 4
        + [[blk("Ana", "we ship the build today"), blk("Bo", "today we test")]]
        + [[blk("Bo", "today we test"), blk("Cy", "fine")]] * 4
    )
    assert ("Ana", "we ship the build today") in stored


def test_a_rewrite_keeps_a_draft_row_whose_words_it_does_not_hold() -> None:
    """Only a draft row every word of which the rewrite's rows hold is folded away."""
    draft = [blk("Ana", "we did it in test first so", "nothing broke at all")]
    final = [blk("Ana", "We did it in test first so nothing.")]
    stored = _replay([draft] * 4 + [final] * 5)
    assert ("Ana", "nothing broke at all") in stored


def test_ocr_jitter_adding_and_garbling_words_on_a_stored_entry_is_not_stored() -> None:
    """Only whole words added to every word the row had are taken: a read that also garbles
    one ("tne") is jitter, however many frames it lasts."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    for t in range(5):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t * 1000)
    for t in range(5, 10):
        out += m.feed(_entries("green.", "and tne notes are", "for the api"), t * 1000)
    out += m.flush(20_000)
    assert [s.text for s in out] == ["green.", "and the notes", "for the api"]


def test_a_word_inserted_mid_entry_on_a_matched_stored_row_is_stored() -> None:
    """A read close enough to align as a match (not a misread) takes inserted words too."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, str] = {}
    for rows, frames in [
        (("green.", "we ship the build on friday", "for the api"), range(5)),
        (("green.", "we ship the new build on friday", "for the api"), range(5, 10)),
    ]:
        for t in frames:
            for s in m.feed(_entries(*rows), t * 1000):
                stored[s.segment_id] = s.text
    for s in m.flush(20_000):
        stored[s.segment_id] = s.text
    assert list(stored.values()) == ["green.", "we ship the new build on friday", "for the api"]


@pytest.mark.parametrize(
    "jitter",
    [
        "and the i notes",  # a stray glyph read as a one-letter word
        "and notes the are done",  # words out of order
        "and the ll notes",  # two consonants: a stray glyph pair (XERK-2728)
    ],
)
def test_ocr_jitter_adding_a_word_on_every_frame_is_not_stored(jitter: str) -> None:
    """QA (XERK-2721): static pixels repeat a misread on every frame, so two frames agreeing
    don't make it speech. Only whole words (not a stray glyph), each kept word in order."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    for t in range(5):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t * 1000)
    for t in range(5, 10):
        out += m.feed(_entries("green.", jitter, "for the api"), t * 1000)
    out += m.flush(20_000)
    assert [s.text for s in out] == ["green.", "and the notes", "for the api"]


@pytest.mark.parametrize(
    ("draft", "final", "kept"),
    [
        # Taken by ``_reflow`` once growth waits for it.
        (
            ["the it build design the did pods again", "deploy"],
            ["the it second build design the did will", "pods again deploy"],
            ["the it second build design the did will", "pods again deploy"],
        ),
        # A word inserted beside the moved one ("it"): ``_reflow`` gives up, the draft stays.
        (
            ["asking test so second contract can cluster", "we same process need so build process",
             "contract me me"],
            ["did asking test so same second contract", "can cluster we same process need so build",
             "process it contract me me"],
            ["asking test so second contract can cluster", "we same process need so build process",
             "contract me me"],
        ),
    ],
)
def test_a_rewrite_re_flowing_words_onto_the_next_row_is_not_taken_as_its_growth(
    draft: list[str], final: list[str], kept: list[str]
) -> None:
    """QA (XERK-2721): an inserted word pushes a row's last words onto the next, which then
    "keeps its words and adds some". Taking that row alone stored the moved words twice and
    lost the rest of the rewrite: growth is taken only while the block's other rows read
    as their lines."""
    stored = _teams_entry(
        [blk("Ana", *draft), blk("Cy", "check the design")],
        [blk("Ana", *final), blk("Cy", "check the design")],
    )
    assert stored == [*(("Ana", t) for t in kept), ("Cy", "check the design")]


def test_an_entry_growing_twice_keeps_a_doubled_word() -> None:
    """QA (XERK-2721): the first growth is taken, so the second is a growth of it; refusing
    a doubled word as jitter lost "it it works", which main stored."""
    stored = _teams_entry(
        _entries("we will", "ok"), _entries("we will ship it it works", "ok"),
        before=[_entries("we will", "ok"), _entries("we will ship", "ok")],
    )
    assert stored == [("Ana", "we will ship it it works"), ("Ana", "ok")]


def test_a_word_moving_between_two_of_a_speakers_entries_is_not_growth() -> None:
    """QA (XERK-2721): "today" re-flowing from one entry onto the next of the same speaker
    made the lower one "keep its words and add some"; it was stored twice. Growth waits
    while the speaker's rows next to it changed."""
    draft = _entries("we ship the build today", "on friday", "ok then")
    stored = _teams_entry(draft, _entries("we ship the build", "today on friday", "ok then"))
    assert stored == [("Ana", "we ship the build today"), ("Ana", "on friday"), ("Ana", "ok then")]


def test_a_reworded_word_moving_across_the_break_between_entries_is_not_growth() -> None:
    """XERK-2728: a neighbour only blocks growth when a word moved off it. One reworded as it
    re-flowed ("today" -> "tomorrow") is not the word this row gained, but the neighbour's
    facing edge changed while this row gained a word on that side: still a re-flow."""
    draft = _entries("we ship the build today", "on friday", "ok then")
    stored = _teams_entry(draft, _entries("we ship the build", "tomorrow on friday", "ok then"))
    assert stored == [("Ana", "we ship the build today"), ("Ana", "on friday"), ("Ana", "ok then")]


@pytest.mark.parametrize(
    ("draft", "moved"),
    [
        # The moved word lands mid-row: no edge word of the growing row changed.
        (["we ship it today", "and test it", "on friday"], ["we ship it", "and today test it", "on friday"]),
        # It lands two rows down, past a row that reads as its line.
        (["we ship it today", "and test it", "on friday"], ["we ship it", "and test it", "on today friday"]),
    ],
)
def test_a_word_moved_within_an_entry_is_not_growth(draft: list[str], moved: list[str]) -> None:
    """QA (XERK-2728): a neighbour blocks growth when it lost the word the row gained. Each
    shape here stored the moved word twice. Accepted: a word reworded as it moves and landing
    mid-row ("today" -> "and tomorrow test it") is taken; refusing on the neighbour's changed
    edge word alone lost real insertions to one jittered frame."""
    stored = _teams_entry([blk("Ana", *draft), blk("Ana", "ok then")],
                          [blk("Ana", *moved), blk("Ana", "ok then")])
    assert stored == [*(("Ana", t) for t in draft), ("Ana", "ok then")]


def test_a_word_inserted_mid_row_on_the_crops_top_row_is_stored() -> None:
    """XERK-2728: only a gain at the start of the crop's header-less top row may have come off
    the row above it; a word inserted mid-row is growth."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "hello all", "ship the build"), blk("Bo", "ok")], 5),
            ([blk(None, "ship the build", header=False), blk("Bo", "ok"), blk("Bo", "sure")], 5),
            ([blk(None, "ship the new build", header=False), blk("Bo", "ok"), blk("Bo", "sure")], 6),
        ]
    )
    assert ("Ana", "ship the new build") in stored


@pytest.mark.parametrize("edge", ["tben we test it", "then we test it"])
def test_one_jittered_frame_of_a_neighbours_facing_word_does_not_refuse_a_growth(edge: str) -> None:
    """QA (XERK-2728): one OCR frame misreading the word of the entry below that faces the
    growing one ("tben") refused a real mid-entry insertion for good."""
    stored, _ = _entry_run(
        [
            (_entries("we ship the build", "then we test it"), 5),
            (_entries("we ship the new build", edge), 1),
            (_entries("we ship the new build", "then we test it"), 6),
        ]
    )
    assert stored == [("Ana", "we ship the new build"), ("Ana", "then we test it")]


def test_words_moved_down_from_a_row_off_the_crop_are_not_growth() -> None:
    """QA (XERK-2728): the crop's header-less top row gained words at its start from the row
    above it, which has scrolled off: no neighbour shows the move, so it was stored twice."""
    rows = ["go do design the will", "enable second what can", "design again again process",
            "task change so"]
    stored, _ = _entry_run(
        [
            ([blk("Bo", *rows)], 5),
            ([blk(None, *rows[2:], header=False), blk("Bo", "do again it change")], 5),
            ([blk(None, "again process task change so.", header=False),
              blk("Bo", "Do again, it change.")], 6),
        ]
    )
    assert ("Bo", "again process task change so.") not in stored
    assert ("Bo", "task change so") in stored


def test_a_word_moved_off_a_row_rewritten_past_recognition_is_not_growth() -> None:
    """QA fz seed 103 (XERK-2728): the row above was rewritten so far it reads as no known
    line, and the word it lost ("deploy") moved onto this row's start. No neighbour check can
    see that loss, so taking the growth stored "deploy" twice (main did too)."""
    cy = blk("Cy", "Locally, it, locally same contract change", "we deploy it we it the build?")
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, str] = {}
    for ts, top in [
        (0, ["check we like deploy deploy repeat repeat", "repeat fix"]),
        (1000, ["Me build contract again the check we,", "deploy repeat fix."]),
        (1500, ["Me build contract again the check we,", "deploy repeat fix."]),
    ]:
        for seg in m.feed([blk(None, *top, header=False), cy], ts):
            stored[seg.segment_id] = seg.text
    for seg in m.flush(60_000):
        stored[seg.segment_id] = seg.text
    assert "repeat fix" in stored.values()
    assert "deploy repeat fix." not in stored.values()


def test_an_entry_merging_its_rows_into_its_last_is_not_growth() -> None:
    """QA (XERK-2728): an entry rewritten from two rows to one makes its old second row the
    block's top row with the first row's words before it; taking that as growth of the second
    row stored them twice."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "Right."), blk("Bo", "go the so did api there me me me", "will can change"),
              blk("Bo", "or itself logs that process")], 5),
            ([blk("Ana", "Right."), blk("Bo", "Go the did api there me will can change."),
              blk("Bo", "Or itself logs, that process")], 6),
        ]
    )
    assert ("Bo", "Go the did api there me will can change.") not in stored


@pytest.mark.parametrize("shown_for", [1, 2, 5])
def test_a_word_moved_between_entries_stays_refused_once_its_entry_scrolls_off(
    shown_for: int,
) -> None:
    """QA (XERK-2721): growth only waited while the changed entry above was shown; once it
    scrolled off, "today on friday" was taken and "today" stored twice."""
    draft = [*_entries("we ship the build today", "on friday"), blk("Bo", "ok then")]
    moved = [*_entries("we ship the build", "today on friday"), blk("Bo", "ok then")]
    scrolled = [*_entries("today on friday"), blk("Bo", "ok then"), blk("Bo", "sure")]
    stored, _ = _entry_run([(draft, 5), (moved, shown_for), (scrolled, 6)])
    assert stored == [
        ("Ana", "we ship the build today"),
        ("Ana", "on friday"),
        ("Bo", "ok then"),
        ("Bo", "sure"),
    ]


def test_one_letter_words_inside_a_growth_are_kept() -> None:
    """QA (XERK-2721): only a run of added words with no word of 2+ chars is a stray glyph;
    refusing every one-letter word lost "and a fix", which main stored."""
    stored, _ = _entry_run(
        [
            (_entries("we need", "ok"), 4),
            (_entries("we need tests", "ok"), 3),
            (_entries("we need tests and a fix", "ok"), 6),
        ]
    )
    assert stored == [("Ana", "we need tests and a fix"), ("Ana", "ok")]


def test_another_speakers_jittered_row_does_not_hold_back_a_growth() -> None:
    """Only the speaker's own neighbouring entries can hold words that re-flowed onto hers."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "i think"), blk("Bo", "ok then")], 5),
            ([blk("Ana", "i think that is fine"), blk("Bo", "ok tnen")], 8),
        ]
    )
    assert stored == [("Ana", "i think that is fine"), ("Bo", "ok then")]


@pytest.mark.parametrize("word", ["is", "PM", "Dr", "vs", "mm"])
def test_a_two_letter_word_inserted_mid_entry_is_stored(word: str) -> None:
    """XERK-2728: only a thin stroke read twice ("ll") is a stray glyph pair. Two-letter words,
    abbreviations and fillers without a vowel are speech (QA)."""
    stored, _ = _entry_run(
        [(_entries("green.", "the build green", "ok"), 5),
         (_entries("green.", f"the build {word} green", "ok"), 6)]
    )
    assert stored == [("Ana", "green."), ("Ana", f"the build {word} green"), ("Ana", "ok")]


@pytest.mark.parametrize(
    ("frames", "grown"),
    [
        # Both entries gain a word in the same frame: neither moved a word off the other.
        (
            [(("we ship the build", "then we test it"), 5),
             (("we ship the new build", "then we really test it"), 5)],
            ("we ship the new build", "then we really test it"),
        ),
        # One OCR-jittered frame of the entry below, on the growth's first frame.
        (
            [(("we ship the build", "then we test it"), 5),
             (("we ship the new build", "then we tesl it"), 1),
             (("we ship the new build", "then we test it"), 5)],
            ("we ship the new build", "then we test it"),
        ),
        # The entry below misread on every frame.
        (
            [(("we ship the build", "on friday"), 5), (("we ship the new build", "on fridav"), 6)],
            ("we ship the new build", "on friday"),
        ),
        # The entry below re-punctuated in the same frame.
        (
            [(("we ship the build", "ok then"), 5), (("we ship the new build", "Ok then."), 6)],
            ("we ship the new build", "ok then"),
        ),
    ],
)
def test_a_neighbour_changing_without_losing_the_added_words_does_not_refuse_a_growth(
    frames: list, grown: tuple[str, str]
) -> None:
    """XERK-2728: any change on the speaker's neighbouring entry refused a growth for good.
    Words re-flow onto a row only off a row that lost them, so only that refuses it."""
    stored, _ = _entry_run([(_entries(*rows), n) for rows, n in frames])
    assert stored == [("Ana", t) for t in grown]


def test_a_word_inserted_as_the_live_entry_gains_a_row_is_stored() -> None:
    """QA (XERK-2721): the speaker went on, so the entry gained a bottom row with no stored
    line yet on the frame after the insertion. Counting that new row as a changed neighbour
    refused the insertion for good."""
    rows = ["push process again push cluster locally", "enable fix build push"]
    grown = "push process again it push cluster locally"
    stored, _ = _entry_run(
        [
            ([blk("Ana", *rows)], 5),
            ([blk("Ana", grown, rows[1])], 1),
            ([blk("Ana", grown, rows[1], "locally")], 1),
            ([blk("Ana", grown, rows[1], "locally then")], 6),
        ]
    )
    assert stored == [("Ana", grown), ("Ana", rows[1]), ("Ana", "locally then")]


def test_an_entry_rewritten_from_two_rows_into_one_is_stored_once() -> None:
    """XERK-2734: Teams merged the entry's rows (fillers dropped). The row keeps the old second
    row's words with words added in front, so it grew that line, and the first row's line
    stayed stored beside it: its words were stored twice. The row now claims both lines: the
    entry's first keeps its segment (and start) with the new text and the end of the last,
    the other is blanked."""
    draft = ["so or what like so so so like itself", "cluster logs build"]
    final = "So or what itself cluster logs build."
    cy = blk("Cy", "Okay.")
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    segs: dict[str, Segment] = {}
    first: dict[str, tuple[int, int]] = {}  # each segment's first emitted (start, end)
    t = 0
    for blocks, count in [([blk("Bo", draft[0]), cy], 3), ([blk("Bo", *draft), cy], 5)] + [
        ([blk("Bo", final), cy], 6)
    ]:
        for _ in range(count):
            for seg in m.feed(blocks, t):
                segs[seg.segment_id] = seg
                first.setdefault(seg.text, (seg.start_ms, seg.end_ms))
            t += 1000
    texts = sorted(seg.text for seg in segs.values())
    assert texts == ["", "Okay.", final]
    merged = next(seg for seg in segs.values() if seg.text == final)
    assert merged.start_ms == first[draft[0]][0]
    assert merged.end_ms == first[draft[1]][1]


_CY = blk("Cy", "Okay.")


def test_an_entry_rewritten_from_three_rows_into_one_blanks_both_rows_it_merged() -> None:
    """XERK-2734 over three rows: the merged row claims every row above the one it grew, so
    the middle row is blanked too, not left stored beside the merged text (QA mutant)."""
    stored, _ = _entry_run(
        [([blk("Bo", "so we check", "the cluster", "and deploy it"), _CY], 5),
         ([blk("Bo", "So we check the cluster and deploy it."), _CY], 6)]
    )
    assert stored == [("Bo", "So we check the cluster and deploy it."), ("Cy", "Okay.")]


@pytest.mark.parametrize(
    "between",
    [[], [([_CY], 1)], [([blk("Bo", "we should ship", "the build on friday"), _CY], 1)]],
    ids=["none", "cy-only", "draft"],
)
def test_a_merged_row_aligned_to_the_entrys_second_row_still_merges_it(
    between: list[tuple[list[CaptionBlock] | None, int]],
) -> None:
    """XERK-2734: a merged row close to the second row's text aligns to it (not a misread),
    which marks that line as heading the entry on the rewrite's first frame. The merge follows
    the rows shown above it (``_Line.above``), not heads, so it still fires on the confirming
    frame, and after a frame that interrupts the rewrite (QA)."""
    final = ([blk("Bo", "We ship the build on friday."), _CY], 1)
    stored, _ = _entry_run(
        [([blk("Bo", "we should ship", "the build on friday"), _CY], 5), final, *between,
         (final[0], 6)]
    )
    assert stored == [("Bo", "We ship the build on friday."), ("Cy", "Okay.")]


@pytest.mark.parametrize(
    ("frames", "kept"),
    [
        # Words said anew in front of the second row: its growth, not the rows merged.
        (
            [([blk("Bo", "we should ship the build", "on friday"), _CY], 5),
             ([blk("Bo", "we should ship the build", "and maybe not on friday"), _CY], 6)],
            "we should ship the build",
        ),
        # The head row scrolled off the crop's top, then a re-flow moved its last words down
        # onto the header-less top row: not a merge, its first words stay (QA).
        (
            [([blk("Bo", "we should ship the build", "on friday"), _CY], 5),
             ([blk(None, "on friday", header=False), _CY, blk("Dy", "fine")], 3),
             ([blk(None, "ship the build on friday", header=False), _CY, blk("Dy", "fine")], 6)],
            "we should ship the build",
        ),
        # The rows above are still shown: they weren't merged away (QA).
        (
            [([blk("Bo", "we should ship the build", "on friday"), _CY], 5),
             ([blk("Bo", "we should ship the build", "ship the build on friday"), _CY], 6)],
            "we should ship the build",
        ),
        # One shared word in front covers too few of the rows' words (kit fz.py lost 13).
        (
            [([blk("Bo", "we should check the cluster logs", "on friday"), _CY], 5),
             ([blk("Bo", "check on friday"), _CY], 6)],
            "we should check the cluster logs",
        ),
        # The speaker's previous entry, off screen, is not part of this one.
        (
            [([blk("Bo", "the cluster"), blk("Bo", "so like the cluster", "logs build"), _CY], 5),
             ([blk("Bo", "So the cluster logs build."), _CY], 6)],
            "the cluster",
        ),
        # Her next entry first seen with its header cut off, then shown under it as one row:
        # its first line never headed anything, so the walk must not run on into "yes we can"
        # and overwrite it (QA).
        (
            [([blk("Bo", "yes we can")], 4),
             ([blk(None, "we can ship", "it on friday", header=False), _CY], 5),
             ([blk("Bo", "We can ship it on friday."), _CY], 6)],
            "yes we can",
        ),
        # OCR missed her next entry's header for one frame: its rows joined the block above,
        # linking it to her previous entry, which the merge then blanked (QA). Also after the
        # entry is shown under its own header again.
        (
            [([blk("Bo", "sure thing"), blk("Bo", "we need to check", "the cluster"), _CY], 4),
             ([blk("Bo", "sure thing", "we need to check", "the cluster"), _CY], 1),
             ([blk("Bo", "We need to check the cluster."), _CY], 6)],
            "sure thing",
        ),
        (
            [([blk("Bo", "sure thing"), blk("Bo", "we need to check", "the cluster"), _CY], 4),
             ([blk("Bo", "sure thing", "we need to check", "the cluster"), _CY], 1),
             ([blk("Bo", "sure thing"), blk("Bo", "we need to check", "the cluster"), _CY], 10),
             ([blk("Bo", "We need to check the cluster."), _CY], 6)],
            "sure thing",
        ),
        # Her next entry never seen under its own header before the merge: the link to her
        # previous entry stays, and only per-row coverage keeps it from being blanked (QA).
        (
            [([blk("Bo", "sure thing"), _CY], 4),
             ([blk("Bo", "sure thing", "we need to check", "the cluster"), _CY], 1),
             ([blk("Bo", "We need to check the cluster."), _CY], 6)],
            "sure thing",
        ),
        # The same, with the merged row holding most of her previous entry's words but not
        # starting with them: the row must start where the entry did (QA).
        (
            [([blk("Bo", "right we check"), _CY], 4),
             ([blk("Bo", "right we check", "it now", "the cluster"), _CY], 1),
             ([blk("Bo", "We check it now the cluster."), _CY], 6)],
            "right we check",
        ),
        # Her previous entry's one word is in the merged row, but not where that entry sat:
        # the rows are aligned against it in order, not looked up anywhere in it (QA af4.py).
        (
            [([blk("Bo", "we")], 4),
             ([blk("Bo", "we", "then ship we then um", "enable same then")], 1),
             ([blk("Bo", "we", "then ship we then um", "enable same then"), _CY], 1),
             ([blk("Bo", "then ship we then enable same then."), _CY], 6)],
            "we",
        ),
    ],
    ids=[
        "new-words", "clipped-top", "rows-shown", "one-word", "previous-entry", "header-cut",
        "header-missed", "header-missed-then-shown", "header-never-read", "first-word-dropped",
        "words-elsewhere",
    ],
)
def test_a_growth_in_front_of_an_entrys_row_blanks_no_row_it_did_not_merge(
    frames: list[tuple[list[CaptionBlock] | None, int]], kept: str
) -> None:
    """XERK-2734's merge blanks the rows above only when the row is the entry's first under its
    header and its leading words are theirs; otherwise those rows' words stay stored."""
    stored, _ = _entry_run(frames)
    assert ("Bo", kept) in stored


def test_a_carried_entry_gaining_words_is_not_revised() -> None:
    """A carried line's segment belongs to the ended meeting (XERK-2615): words added to it
    on the next meeting's panel never re-emit it."""
    m1 = CaptionMerger(start_ms=0, stable_ms=3000)
    old = set()
    for t in range(5):
        old |= {s.segment_id for s in m1.feed(_entries("green.", "and the notes", "for the api"), t * 1000)}
    old |= {s.segment_id for s in m1.flush(5000)}
    m2 = CaptionMerger(start_ms=6000, stable_ms=3000, carried=m1.recent())
    emitted = []
    for t in range(6, 12):
        emitted += m2.feed(_entries("green.", "and the notes are done", "for the api"), t * 1000)
    emitted += m2.flush(20_000)
    assert not {s.segment_id for s in emitted} & old


def test_an_old_entry_gaining_words_is_not_revised() -> None:
    """Teams rewrites an entry seconds after it ends: past ``_REVISE_MS`` a stored row keeps
    its text."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out: list = []
    for t in range(0, 40_000, 1000):
        out += m.feed(_entries("green.", "and the notes", "for the api"), t)
    for t in range(40_000, 45_000, 1000):
        out += m.feed(_entries("green.", "and the notes are done", "for the api"), t)
    out += m.flush(60_000)
    assert [s.text for s in out] == ["green.", "and the notes", "for the api"]




def _emitted(frames: list[tuple[list[CaptionBlock], int]]) -> list[str]:
    """Every text the merger emits over ``frames`` (each fed ``count`` times, one a second),
    in order: an upsert stored and later overwritten still reached the store."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    texts: list[str] = []
    t = 0
    for blocks, count in frames:
        for _ in range(count):
            texts += [seg.text for seg in m.feed(blocks, t)]
            t += 1000
    return texts + [seg.text for seg in m.flush(t + 60_000)]


_SHIP = _entries("green.", "we ship the build on friday", "ok")
_REWRITE = _entries("green.", "we ship the new build friday", "ok")


def test_a_rewrite_inserting_and_dropping_a_word_is_stored() -> None:
    """XERK-2732: Teams inserts "new" and drops "on" on one frame; the whole draft was kept."""
    stored, _ = _entry_run([(_SHIP, 5), (_REWRITE, 2)])
    assert stored == [("Ana", "green."), ("Ana", "we ship the new build friday"), ("Ana", "ok")]


def test_a_one_frame_rewrite_dropping_a_word_is_never_stored() -> None:
    """Two frames in a row must agree: one, or one between full reads, emits nothing (QA:
    asserting the final text alone missed a mutant storing it, then overwriting it)."""
    short = _emitted([(_SHIP, 5), (_REWRITE, 1), (_SHIP, 5)])
    broken = _emitted([(_SHIP, 5), (_REWRITE, 1), (_SHIP, 1), (_REWRITE, 1), (_SHIP, 5)])
    for texts in (short, broken):
        assert "we ship the new build friday" not in texts, texts


def test_a_word_dropped_alone_is_never_taken() -> None:
    """A read that only drops words is OCR missing them, however long it lasts (QA: a
    deterministic miss repeats on every frame; "we do not ship it" became "we do ship it").
    Accepted: Teams dropping a word with nothing added keeps it (XERK-2732's two-step shape)."""
    for draft, read in [
        ("we do not ship it on friday", "we do ship it on friday"),
        ("we ship the new build on friday", "we ship the new build friday"),
    ]:
        texts = _emitted(
            [(_entries("green.", draft, "ok"), 5), (_entries("green.", read, "ok"), 20)]
        )
        assert read not in texts, texts


def test_two_words_read_as_one_beside_a_new_one_keeps_the_stored_entry() -> None:
    """Only the dropped span resembles the read ("of is" -> "ofbuild"): no single word does
    (QA's differential found such merged reads)."""
    texts = _emitted(
        [(_entries("green.", "we of is friday i", "ok"), 5), (_entries("green.", "we ofbuild friday i", "ok"), 20)]
    )
    assert "we ofbuild friday i" not in texts, texts


@pytest.mark.parametrize(
    "read",
    [
        "we sh uld ship the build",  # a garble splitting a word
        "we should th build",  # a word missed beside a garbled one ("ship the" -> "th")
        "we should ship l build",  # a word missed, a stray glyph read
        "ship the new build",  # the crop clipping the row's start
        "we should ship the new",  # ... or its end
        "we new build",  # most of the row missed
    ],
)
def test_a_lasting_misread_dropping_words_keeps_the_stored_entry(read: str) -> None:
    """Reads that drop words the way OCR does are never taken, however long they last."""
    texts = _emitted(
        [
            (_entries("green.", "we should ship the build", "ok"), 5),
            (_entries("green.", read, "ok"), 20),
        ]
    )
    assert read not in texts, texts


def test_a_misread_held_for_confirmation_lapses_on_a_clean_read() -> None:
    """QA (XERK-2694): a rewrite's new words on a matched row must be read on the next frame
    too; a clean read in between drops them, so jitter flipping back later is never stored."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["yes the pods need enable should we", "the cluster is up now", "how right right in overlay"]
    final = ["Yes the pods need enable should we", "the cluster is up now,", "right in overlay."]
    misread = "Yes pods need enable should we"
    stored: dict[str, str] = {}
    frames = [[blk("Eli", "so")]] * 5 + [[blk(None, *draft, header=False)]] * 5
    frames += [[blk(None, misread, *final[1:], header=False)]]
    frames += [[blk(None, *final, header=False)]] * 3
    frames += [[blk(None, misread, *final[1:], header=False)]]
    frames += [[blk(None, *final, header=False)]] * 3
    for t, frame in enumerate(frames):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = seg.text
    for seg in m.flush(60_000):
        stored[seg.segment_id] = seg.text
    assert misread not in stored.values(), stored


@pytest.mark.parametrize(
    "noisy",
    [
        "so enable should like we the",  # clipped
        "Yes so enable should like we the'",  # stray character
        "yes so so enable should like we",  # still the draft
    ],
)
def test_one_noisy_frame_after_a_rewrite_only_delays_it(noisy: str) -> None:
    """QA (XERK-2694): a noisy read right after the rewrite frame must not lose the rewrite
    when every later frame reads it cleanly."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["yes so so enable should like we", "the cluster not need pods it the", "how right right in overlay"]
    final = ["Yes so enable should like we the", "cluster not need pods it, the how", "right in overlay."]
    stored: dict[str, str] = {}
    frames = [[blk("Eli", "so")]] * 5 + [[blk(None, *draft, header=False)]] * 5
    frames += [[blk(None, *final, header=False)]]
    frames += [[blk(None, noisy, *final[1:], header=False)]]
    frames += [[blk(None, *final, header=False)]] * 8
    for t, frame in enumerate(frames):
        for seg in m.feed(frame, t * 1000):
            stored[seg.segment_id] = seg.text
    for seg in m.flush(60_000):
        stored[seg.segment_id] = seg.text
    assert list(stored.values())[-3:] == final, stored


def _rewrite_run(frames: list[list[str]]) -> tuple[dict[str, str], list[list[str]]]:
    """Feed the rewrap fixture's draft, then ``frames`` (rows of one header-less block, one
    frame a second); returns the stored texts and each frame's emitted texts."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["yes so so enable should like we", "the cluster not need pods it the", "how right right in overlay"]
    stored: dict[str, str] = {}
    emitted: list[list[str]] = []
    feed = [[blk("Eli", "so")]] * 5 + [[blk(None, *draft, header=False)]] * 5
    feed += [[blk(None, *rows, header=False)] for rows in frames]
    for t, frame in enumerate(feed):
        segs = m.feed(frame, t * 1000)
        stored.update({s.segment_id: s.text for s in segs})
        emitted.append([s.text for s in segs])
    stored.update({s.segment_id: s.text for s in m.flush(len(feed) * 1000 + 60_000)})
    return stored, emitted[10:]


_REWRAP_FINAL = ["Yes so enable should like we the", "cluster not need pods it, the how", "right in overlay."]


def test_a_clipped_top_row_read_alike_after_a_rewrite_is_not_stored() -> None:
    """QA (XERK-2694): a crop cutting a row's start reads it the same on every frame; two
    alike reads of it inside the rewrite window must not replace the row."""
    clipped = ["so enable should like we the", *_REWRAP_FINAL[1:]]
    # The rewrite frame arms row 0 with new words; the crop then clips it on every frame.
    stored, _ = _rewrite_run([_REWRAP_FINAL] + [clipped] * 3)
    assert "so enable should like we the" not in stored.values(), stored


def test_the_rewrite_window_closes_once_taken_and_after_it_expires() -> None:
    """QA (XERK-2694): once a rewrite is taken, later alike misreads are OCR again (the window
    closed); and new words read only after the window are never taken."""
    misread = ["Yes so enab1e should like we the", *_REWRAP_FINAL[1:]]
    stored, emitted = _rewrite_run([_REWRAP_FINAL] * 3 + [misread] * 3 + [_REWRAP_FINAL] * 2)
    assert all("Yes so enab1e should like we the" not in e for e in emitted), emitted
    assert "Yes so enable should like we the" in stored.values(), stored
    # The rewrite frame arms row 0; the draft is re-read until the window has passed.
    still = ["yes so so enable should like we", *_REWRAP_FINAL[1:]]
    stored, _ = _rewrite_run([_REWRAP_FINAL] + [still] * 35 + [_REWRAP_FINAL] * 3)
    assert "yes so so enable should like we" in stored.values(), stored


def test_a_case_or_punctuation_rewrite_of_a_matched_row_is_taken_at_once() -> None:
    """Only new words wait for a second read: "Yes, so so ..." over "yes so so ..." is the
    same words, stored on the rewrite frame itself."""
    final = ["Yes, so so enable should like we.", *_REWRAP_FINAL[1:]]
    _, emitted = _rewrite_run([final])
    assert "Yes, so so enable should like we." in emitted[0], emitted


_REWRAP_DRAFT = ["yes so so enable should like we", "the cluster not need pods it the", "how right right in overlay"]


@pytest.mark.parametrize("where", ["under its header", "below another block"])
def test_a_rewrite_dropping_leading_words_is_taken_where_no_crop_cuts(where: str) -> None:
    """QA (XERK-2694): only the crop's header-less top row can be cut off; elsewhere a row
    that lost its leading words is Teams' rewrite (fillers dropped) and is taken."""
    final = ["so so enable should like we", "the cluster not need pods it, the how", "right in overlay."]

    def frame(rows: list[str]) -> list[CaptionBlock]:
        if where == "under its header":
            return [blk("Eli", *rows)]
        return [blk("Mia", "earlier words from mia here"), blk(None, *rows, header=False)]

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, str] = {}
    for t, rows in enumerate([_REWRAP_DRAFT] * 6 + [final] * 8):
        stored.update({s.segment_id: s.text for s in m.feed(frame(rows), t * 1000)})
    stored.update({s.segment_id: s.text for s in m.flush(60_000)})
    assert "so so enable should like we" in stored.values(), stored
    assert "yes so so enable should like we" not in stored.values(), stored


def test_a_clipped_draft_row_after_a_rewrite_is_not_stored() -> None:
    """QA (XERK-2694): the rewrite arms the top row, the draft comes back, then the crop
    clips the draft's start on every frame: that cut-off read is not taken either."""
    clipped = ["so enable should like we", *_REWRAP_DRAFT[1:]]
    stored, _ = _rewrite_run([_REWRAP_FINAL, _REWRAP_DRAFT] + [clipped] * 3)
    assert "so enable should like we" not in stored.values(), stored


def test_only_the_crops_own_top_row_counts_as_clippable_on_a_long_crop() -> None:
    """QA (XERK-2694): a crop over ``_MAX_SHOWN`` rows is cut to its bottom ones; the first
    row kept is not the crop's top row, so its leading-word rewrite is taken."""
    middle = [f"ravi middle line number {k} words" for k in range(8)]
    final = ["so so enable should like we", "the cluster not need pods it, the how", "right in overlay."]

    def frame(row1: str, rows: list[str]) -> list[CaptionBlock]:
        return [blk(None, "tail of ravi row zero", row1, *middle, *rows, header=False)]

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, str] = {}
    frames = [frame("um we were saying that the build", _REWRAP_DRAFT)] * 6
    frames += [frame("we were saying that the build", final)] * 8
    for t, f in enumerate(frames):
        stored.update({s.segment_id: s.text for s in m.feed(f, t * 1000)})
    stored.update({s.segment_id: s.text for s in m.flush(60_000)})
    assert "we were saying that the build" in stored.values(), stored


# --- XERK-2715: Teams re-flows a growing entry ------------------------------------------
#
# Teams wraps an entry as one text, so a word inserted or rewritten in it moves words across
# its row breaks ("the." becomes "the API contract" and pulls "can" down from the row above).
# Row by row, a row that gained leading words isn't a growth: it landed as a new line beside
# its stale draft, and both were stored (~7% of a real meeting's lines).


def _entry_run(
    frames: list[tuple[list[CaptionBlock] | None, int]],
) -> tuple[list[tuple], dict]:
    """Feed each frame ``count`` times, one a second (``None``: that many seconds of dropped
    frames); returns the stored (speaker, text) rows in order (blanked ones left out) and
    every segment's last text by id."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    final: dict[str, object] = {}
    t = 0
    for blocks, count in frames:
        for _ in range(count):
            if blocks is not None:
                for seg in m.feed(blocks, t):
                    final[seg.segment_id] = seg
            t += 1000
    for seg in m.flush(t + 60_000):
        final[seg.segment_id] = seg
    rows = sorted(final.values(), key=lambda s: s.start_ms)
    return [(s.speaker, s.text) for s in rows if s.text], {k: s.text for k, s in final.items()}


def test_a_rewrite_pulling_a_word_onto_the_next_row_is_stored_once() -> None:
    """The ticket's shape: a stored row "do the second task the." re-reads as "can do the
    second task, the API contract" ("can" left the row above). Both rows take the final text
    in place; the draft isn't kept beside it."""
    draft = ["so after the review I can", "do the second task the."]
    final = ["so after the review I", "can do the second task, the API contract"]
    stored, _ = _entry_run([([blk("Ana", *draft)], 6), ([blk("Ana", *final)], 6)])
    assert stored == [("Ana", final[0]), ("Ana", final[1])]


def test_a_word_moving_down_a_live_entry_is_stored_once() -> None:
    """A word inserted while the entry grows pushes the end of each row onto the next."""
    frames = [
        ([blk("Ana", "we should check the cluster")], 2),
        ([blk("Ana", "we should check the cluster logs")], 2),
        ([blk("Ana", "we should check the cluster logs", "before we")], 2),
        ([blk("Ana", "we should check the staging cluster", "logs before we deploy")], 6),
        (
            [
                blk("Ana", "We should check the staging cluster", "logs before we deploy."),
                blk("Bo", "sure"),
            ],
            6,
        ),
    ]
    stored, _ = _entry_run(frames)
    # Case alone is a rewrite, not a re-flow: the row rules keep the draft's.
    assert [(sp, _words(text)) for sp, text in stored] == [
        ("Ana", "we should check the staging cluster"),
        ("Ana", "logs before we deploy"),
        ("Bo", "sure"),
    ]


def test_a_word_pulled_up_blanks_its_stored_draft_row() -> None:
    """The ticket's shape: "staging" joins the row above, the next row's words follow it. The
    stored draft row "staging" is re-emitted blank (same id, which readers skip), not kept."""
    draft = ["then we can ship the build to", "staging"]
    final = ["then we can ship the build to staging", "right after lunch"]
    stored, by_id = _entry_run([([blk("Ana", *draft)], 6), ([blk("Ana", *final)], 6)])
    assert stored == [("Ana", final[0]), ("Ana", final[1])]
    assert list(by_id.values()).count("") == 1
    segs = [
        Segment(segment_id=k, text=v, start_ms=0, end_ms=0, lang="en", speaker="Ana")
        for k, v in by_id.items()
    ]
    assert all(seg.text for seg in sentences(segs))


@pytest.mark.parametrize(
    ("draft", "final"),
    [
        (
            ["so we need to", "check the cluster", "and deploy it"],
            "So we need to check the cluster and deploy it.",
        ),
        (["we ship the build", "on friday"], "We will ship the build on friday."),
    ],
)
def test_an_entry_re_laid_onto_one_row_blanks_its_later_rows(draft: list, final: str) -> None:
    """XERK-2739: Teams re-lays a finished entry onto one row starting with its first row's
    words. The rewrite claimed only the first row; the rows below kept their words, stored
    twice. They are now blanked (same ids)."""
    cy = blk("Cy", "Okay.")
    stored, by_id = _entry_run([([blk("Bo", *draft), cy], 5), ([blk("Bo", final), cy], 6)])
    assert stored == [("Bo", final), ("Cy", "Okay.")]
    assert list(by_id.values()).count("") == len(draft) - 1


def test_the_speakers_next_entry_ocr_missed_during_a_re_lay_is_never_blanked() -> None:
    """Her next entry repeats the words the re-lay fixed in a draft row ("to tst first" ->
    "to test first"), and OCR misses its block on the re-lay's frame. Its words pair with the
    rewrite, but it is her own entry, said again: it is kept, as on main (QA, XERK-2739)."""
    a = blk("Bo", "we deploy it", "to tst first")
    b = blk("Bo", "to test first")
    r = blk("Bo", "We deploy it to test first.")
    stored, by_id = _entry_run([([a], 4), ([a, b], 4), ([r], 1), ([r, b], 6)])
    assert ("Bo", "to test first") in stored
    assert "" not in by_id.values()


def test_the_next_entry_read_into_the_block_during_a_re_lay_is_never_blanked() -> None:
    """Teams missed her next entry's avatar, so its name and row joined the draft's block,
    and then OCR missed it on the re-lay frame. The rewrite's words pair with that entry, not
    the typo'd draft row above it ("to tst first"); it is never dropped as a folded copy, so
    the entry stays stored (QA, XERK-2739)."""
    a = blk("Bo", "we deploy it", "to tst first")
    joined = blk("Bo", "we deploy it", "to tst first", "Bo", "to test first")
    b = blk("Bo", "to test first")
    r = blk("Bo", "We deploy it to test first.")
    stored, _ = _entry_run([([a], 4), ([joined], 2), ([r], 1), ([r, b], 6)])
    assert ("Bo", "to test first") in stored


def test_a_refused_rewrite_blanks_nothing() -> None:
    """A rewrite leaving a newer known line unshown is refused (a new line like an old one):
    no draft row it seemed to take is blanked."""
    a = blk("Bo", "we deploy it", "to tst first")
    cy = blk("Cy", "to test first")
    r = blk("Bo", "We deploy it to test first.")
    stored, by_id = _entry_run([([a], 4), ([a, cy], 4), ([r], 1), ([r, cy], 6)])
    assert ("Bo", "to tst first") in stored
    assert "" not in by_id.values()


def test_a_draft_row_the_rewrite_took_one_word_of_is_kept() -> None:
    """A draft row is a stale copy only when the rewrite holds its words; one shared word
    ("and") is not enough to blank it."""
    cy = blk("Cy", "Okay.")
    draft = ["so we need to", "check the cluster", "and then call the vendor today"]
    final = "So we need to check the cluster and."
    stored, by_id = _entry_run([([blk("Bo", *draft), cy], 5), ([blk("Bo", final), cy], 6)])
    assert ("Bo", draft[2]) in stored
    assert list(by_id.values()).count("") == 1


def test_a_re_lay_onto_fewer_rows_blanks_rows_below_a_second_claimed_row() -> None:
    """Four draft rows re-laid onto two: both new rows claim a draft row, and the unclaimed
    rows taken around and below them are blanked too, not just those before the second."""
    draft = ["so we need", "to check the", "cluster and", "deploy it now"]
    final = ["So we need to check the", "cluster and deploy it now."]
    cy = blk("Cy", "Okay.")
    relaid = blk("Bo", *final)
    stored, _ = _entry_run([([blk("Bo", *draft)], 4), ([relaid], 6), ([relaid, cy], 4)])
    assert stored == [("Bo", final[0]), ("Bo", final[1]), ("Cy", "Okay.")]


def test_a_row_a_rewrite_dropped_is_kept() -> None:
    """A row whose words the shorter rewrite left out (a stutter Teams dropped, or speech the
    re-laid row lacks) is no stale copy: it stays stored."""
    cy = blk("Cy", "Okay.")
    draft = ["so we need to", "check the cluster", "um um um uh"]
    final = "So we need to check the cluster."
    stored, _ = _entry_run([([blk("Bo", *draft), cy], 5), ([blk("Bo", final), cy], 6)])
    assert stored == [("Bo", final), ("Bo", "um um um uh"), ("Cy", "Okay.")]


@pytest.mark.parametrize(
    ("draft", "final"),
    [
        (
            ["so we checked the logs and", "the build", "failed after the merge yesterday"],
            [
                "so we checked the logs",
                "and the build broke right",
                "failed after the merge yesterday",
            ],
        ),
        (
            ["alpha bravo charlie delta", "echo foxtrot", "golf hotel india"],
            ["alpha bravo charlie", "delta echo foxtrot golf", "hotel india"],
        ),
    ],
)
def test_a_rewrapped_middle_row_takes_its_new_words(draft: list, final: list) -> None:
    """A middle row that still resembles its draft aligns as an OCR misread of it, and the
    words the re-flow moved onto it were lost; it now takes the entry's text in place."""
    stored, _ = _entry_run([([blk("Ana", *draft)], 6), ([blk("Ana", *final)], 6)])
    assert stored == [("Ana", row) for row in final]


def test_a_one_frame_rewrap_is_never_stored() -> None:
    """A re-flow is taken only when two frames in a row read it: one frame (a misread that
    happens to move a word) changes nothing."""
    rows = ["we should check the cluster", "logs before we deploy"]
    moved = ["we should check the", "cluster logs before we deploy"]
    stored, _ = _entry_run(
        [([blk("Ana", *rows)], 6), ([blk("Ana", *moved)], 1), ([blk("Ana", *rows)], 6)]
    )
    assert stored == [("Ana", rows[0]), ("Ana", rows[1])]


_ALIKE_ROWS = ["so i think that we should", "do it and i think that", "we should ship it today."]


@pytest.mark.parametrize("below", [[blk("Bo", "sure")], []])
def test_a_middle_row_missed_on_two_frames_is_kept_when_its_neighbours_share_its_words(
    below: list,
) -> None:
    """QA (XERK-2715): OCR missing a row on two identical frames left rows that hold half its
    words ("i think that"); the line was blanked. Rows reading as their lines did moved no
    words, so the line is an OCR miss and stays, in its place."""
    rows = _ALIKE_ROWS
    stored, _ = _entry_run(
        [
            ([blk("Ana", *rows), *below], 6),
            ([blk("Ana", rows[0], rows[2]), *below], 2),
            ([blk("Ana", *rows), *below], 4),
        ]
    )
    assert stored == [("Ana", row) for row in rows] + [(b.speaker, b.lines[0]) for b in below]


def test_a_middle_row_missed_while_the_bottom_row_grows_is_kept() -> None:
    """QA (XERK-2715): the missed row's words sit at its neighbours' break ("...so i think" /
    "i think so we" / "so we can ..."), and the live bottom row changes every frame as it
    grows; that change is no sign a word moved, so the row stays, in its place, once."""
    top, mid = "we should check it so i think", "i think so we"
    stored, _ = _entry_run(
        [
            ([blk("Ana", top, mid, "so we can deploy")], 4),
            ([blk("Ana", top, "so we can deploy it")], 1),
            ([blk("Ana", top, "so we can deploy it now")], 1),
            ([blk("Ana", top, "so we can deploy it now ok")], 1),
            ([blk("Ana", top, mid, "so we can deploy it now ok"), blk("Bo", "ok")], 6),
        ]
    )
    assert stored == [("Ana", top), ("Ana", mid), ("Ana", "so we can deploy it now ok"), ("Bo", "ok")]


def test_a_misread_bottom_row_on_the_confirming_frame_is_never_stored() -> None:
    """QA (XERK-2715): a stored bottom row is confirmed like the rows above it, so a misread
    on the frame after the re-flow doesn't replace it; the clean frames after it do."""
    draft = ["so after the review I can", "do the second task the."]
    final = ["so after the review I", "can do the second task, the API contract"]
    misread = [final[0], "can do tbe secand task, the apl contrcat"]
    stored, _ = _entry_run(
        [
            ([blk("Ana", *draft)], 6),
            ([blk("Ana", *final)], 1),
            ([blk("Ana", *misread)], 1),
            ([blk("Ana", *final)], 6),
        ]
    )
    assert stored == [("Ana", final[0]), ("Ana", final[1])]


def test_a_top_row_clipped_mid_word_keeps_its_line_during_a_reflow() -> None:
    """QA (XERK-2715): the crop cutting the header-less top row mid-word ("ent over ...")
    must not replace the stored line while the rows below re-flow."""
    top = "we went over the release plan"
    draft = ["so after the review I can", "do the second task the."]
    final = ["so after the review I", "can do the second task, the API contract"]
    stored, _ = _entry_run(
        [
            ([blk("Ana", top, *draft)], 6),
            ([blk(None, top, *draft, header=False)], 2),
            ([blk(None, "ent over the release plan", *final, header=False)], 6),
        ]
    )
    assert stored == [("Ana", top), ("Ana", final[0]), ("Ana", final[1])]


def test_a_finished_entry_reflowing_above_the_next_speaker_is_stored_once() -> None:
    """QA (XERK-2715): Teams re-flows an entry after it ends, with the next speaker's block
    below it. That block aligns, so the re-flowed bottom row reads as a glitch above it; it
    still pairs with the draft row it replaces, whose new words were lost."""
    draft = ["so after the review I can", "do the second task the."]
    final = ["so after the review I", "can do the second task, the API contract"]
    stored, _ = _entry_run(
        [([blk("Ana", *draft), blk("Bo", "sure")], 6), ([blk("Ana", *final), blk("Bo", "sure")], 6)]
    )
    assert stored == [("Ana", final[0]), ("Ana", final[1]), ("Bo", "sure")]


@pytest.mark.parametrize("below", ["Ana", "Bo", None])
def test_a_finished_entry_reflowing_above_the_same_speakers_next_entry_is_stored_once(
    below: str | None,
) -> None:
    """XERK-2716: a 3-row entry re-flowing (its bottom row now unlike its draft) above the
    same speaker's next entry stored the whole box again. Another speaker below, or nothing,
    was already stored once."""
    draft = ["we went over the plan and", "then the build is", "green."]
    final = ["we went over the plan", "and then the build is", "green, so ship it"]
    nxt = ["and then the release notes", "for the api contract"]
    tail = [blk(below, *nxt)] if below else []
    stored, _ = _entry_run([([blk("Ana", *draft), *tail], 6), ([blk("Ana", *final), *tail], 6)])
    assert stored == [("Ana", text) for text in final] + [(below, text) for text in nxt if below]


def test_a_one_row_entry_growing_above_the_next_speaker_keeps_its_new_words() -> None:
    """QA (XERK-2716): a block's top row is never paired by word growth: it has no row above
    to place it by, so "so ship it" was lost (the rewrite path stores it)."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "green.")], 5),
            ([blk("Ana", "green."), blk("Bo", "sure.")], 5),
            ([blk("Ana", "green, so ship it"), blk("Bo", "sure.")], 6),
        ]
    )
    assert stored == [("Ana", "green, so ship it"), ("Bo", "sure.")]


@pytest.mark.parametrize("above", [False, True])
def test_a_new_line_starting_with_an_old_ones_words_is_not_explained_away(above: bool) -> None:
    """QA (XERK-2716): "yes I think so too." said between snapshots above a repeat of Ana's
    next entry is a new line, not the stored "yes." re-flowing; also with Bo's block above it
    (a row of another block above is not one of its entry's, or the new line is lost). Both
    lines said between snapshots needs frames dropped: one second apart, it is the stored
    entry growing (XERK-2717)."""
    bo = [blk("Bo", "ok.")] if above else []
    stored, _ = _entry_run(
        [
            ([*bo, blk("Ana", "yes.")], 5),
            ([*bo, blk("Ana", "yes."), blk("Ana", "we ship friday.")], 5),
            (None, 8),
            ([*bo, blk("Ana", "yes I think so too."), blk("Ana", "we ship friday.")], 6),
        ]
    )
    # With Bo above, the whole box is stored again (as on main): a line said between
    # snapshots under a header makes everything below it new.
    assert ("Ana", "yes I think so too.") in stored and (above or len(stored) == 4)


def test_a_reflow_moving_two_words_down_above_the_same_speakers_next_entry_is_stored_once() -> (
    None
):
    """QA (XERK-2716): the row above the grown one gained two words, so it no longer matches
    its draft; the grown row still pairs as a row of its entry."""
    draft = ["so we went over the plan and then", "the build is", "green."]
    final = ["so we went over the plan", "and then the build is", "green, so ship it"]
    nxt = ["and then the release notes", "for the api contract"]
    stored, _ = _entry_run(
        [([blk("Ana", *draft), blk("Ana", *nxt)], 6), ([blk("Ana", *final), blk("Ana", *nxt)], 6)]
    )
    assert stored == [("Ana", text) for text in (*final, *nxt)]


def test_an_old_row_re_read_longer_is_jitter_not_growth() -> None:
    """QA (XERK-2716): a middle row re-read with stray glyphs long after its entry ended is
    OCR jitter; only a recent (``_REVISE_MS``) row takes growth this way."""
    rows = ["we went over the plan", "like", "and then it is green"]
    stored, _ = _entry_run(
        [([blk("Ana", *rows)], 75), ([blk("Ana", rows[0], "like l o", rows[2])], 6)]
    )
    assert stored == [("Ana", text) for text in rows]


def test_a_growing_bottom_row_never_rewrites_the_rows_above() -> None:
    """QA (XERK-2716, `_simulate_teams` seed 125): the live bottom row growing by whole words
    is no re-flow; taking it copied a misaligned row's text onto a draft line, stored twice."""
    top = "pods itself can locally design deploy"
    stored, _ = _entry_run(
        [
            ([blk("Ana", top)], 1),
            ([blk("Ana", top, "pods.")], 1),
            ([blk("Ana", f"{top} pods", "itself")], 1),
            ([blk("Ana", f"{top} pods", "itself enable")], 1),
            ([blk("Ana", f"{top} pods", "itself enable right.")], 1),
            ([blk("Ana", f"{top} pods", "itself enable right so")], 1),
            ([blk("Ana", f"{top} pods", "itself enable right so need")], 1),
            ([blk("Ana", f"{top} pods", "itself enable right so need right deploy")], 6),
        ]
    )
    words = [w for _, text in stored for w in _words(text).split()]
    assert words == _words(f"{top} pods itself enable right so need right deploy").split()


@pytest.mark.parametrize(
    ("draft", "final"),
    [
        (["green."], ["green, so ship it"]),
        (
            ["we went over the plan and", "then the build is", "green."],
            ["we went over the plan", "and then the build is", "green, so ship it"],
        ),
    ],
)
def test_an_entry_growing_above_the_same_speakers_next_entry_is_stored_once(
    draft: list[str], final: list[str]
) -> None:
    """XERK-2717: a finished entry grows while the same speaker's next entry shows below it.
    Nothing aligns, and the speaker's unshown lines run on into that next entry, so the grown
    rows covered too few of the draft's words and the whole box was stored again."""
    nxt = ["and then the release notes", "for the api contract"]
    stored, _ = _entry_run(
        [
            ([blk("Ana", *draft)], 5),
            ([blk("Ana", *draft), blk("Ana", *nxt)], 5),
            ([blk("Ana", *final), blk("Ana", *nxt)], 6),
        ]
    )
    assert stored == [("Ana", text) for text in [*final, *nxt]]


def test_a_short_entry_after_the_same_speakers_next_one_is_stored_once() -> None:
    """QA (XERK-2717): with a third entry of hers below, the second entry's draft ran on into
    it, still covered enough words, and swallowed it unclaimed; "ok." was stored again."""
    said = ["green.", "and the notes", "ok."]
    frames = [([blk("Ana", text) for text in said[:k]], 5) for k in range(1, 4)]
    stored, _ = _entry_run(
        [*frames, ([blk("Ana", text) for text in ["green, so ship it", *said[1:]]], 6)]
    )
    assert stored == [("Ana", "green, so ship it"), ("Ana", "and the notes"), ("Ana", "ok.")]


def test_the_longest_cut_of_a_draft_is_tried_first() -> None:
    """QA (XERK-2717): a two-row entry whose second row reads like her next entry ("ok.")
    grows. Cutting the draft at the first "ok." would leave its row unclaimed and store the
    box again; the cut at her next entry's own line is tried first."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "green.", "ok.")], 5),
            ([blk("Ana", "green.", "ok."), blk("Ana", "ok.")], 5),
            ([blk("Ana", "green, so ship it", "ok. great"), blk("Ana", "ok.")], 6),
        ]
    )
    assert stored == [("Ana", "green, so ship it"), ("Ana", "ok. great"), ("Ana", "ok.")]


def test_a_header_less_next_block_still_cuts_the_draft() -> None:
    """A header-less block under the grown entry (a misread header) re-reading her next line
    cuts the draft too; requiring a header stored the box twice."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "green.")], 5),
            ([blk("Ana", "green."), blk("Ana", "and the notes")], 5),
            ([blk("Ana", "green, so ship it"), blk(None, "and the notes", header=False)], 6),
        ]
    )
    assert stored == [("Ana", "green, so ship it"), ("Ana", "and the notes")]


def test_bo_repeating_anas_next_entry_cuts_none_of_her_draft() -> None:
    """Only her own next entry cuts her draft: Ana's two entries rewritten as one, with Bo
    saying her second one word for word below it, still revise her first line in place."""
    stored, _ = _entry_run(
        [
            ([blk("Ana", "we ship.")], 5),
            ([blk("Ana", "we ship."), blk("Ana", "today.")], 5),
            ([blk("Ana", "we ship today."), blk("Bo", "today.")], 6),
        ]
    )
    assert stored[0] == ("Ana", "we ship today."), stored
    assert stored.count(("Ana", "we ship today.")) == 1, stored


def test_a_line_the_ocr_missed_between_rows_is_kept() -> None:
    """A stored line a block skips is a stale copy only when the rows around it hold its
    words; otherwise OCR missed it and it stays."""
    rows = ["alpha bravo charlie", "delta echo foxtrot", "golf hotel india"]
    stored, _ = _entry_run(
        [([blk("Ana", *rows)], 6), ([blk("Ana", rows[0], "golf hotel india juliet")], 6)]
    )
    assert [text for _, text in stored] == [*rows[:2], "golf hotel india juliet"]


def _entries(*rows: str) -> list[CaptionBlock]:
    """One Teams entry per row, each under its own header (one speaker talking on)."""
    return [blk("Ana", row) for row in rows]


@pytest.mark.parametrize(
    ("after", "bottom"),
    [
        pytest.param(
            [(_entries("green, so ship it", "and the notes"), 6)], "and the notes", id="verbatim"
        ),
        # Punctuation is taken at once on a revised row; a misread waits for two reads alike.
        pytest.param(
            [(_entries("green, so ship it", "and the notes."), 6)], "and the notes.", id="stray-dot"
        ),
        pytest.param(
            [(_entries("green, so ship it", n), 1) for n in ["and the notes", "and tne notes"] * 3],
            "and the notes",
            id="misread",
        ),
    ],
)
def test_a_grown_entry_above_the_speakers_next_entry_is_stored_once(
    after: list, bottom: str
) -> None:
    """XERK-2720: Ana's finished entry "green." grows to "green, so ship it" above her next
    entry. Its rewrite revises only its own entry, never the next one (however that reads):
    the draft was cut by words across both entries and the whole box stored again."""
    stored, _ = _entry_run(
        [(_entries("green."), 5), (_entries("green.", "and the notes"), 5), *after]
    )
    assert [text for _, text in stored] == ["green, so ship it", bottom]


def test_a_grown_entry_above_a_live_entry_is_stored_once() -> None:
    """XERK-2720: the next entry is still growing a word a frame when the first one's rewrite
    lands, so it re-reads no stored line exactly."""
    frames = [
        (_entries("green."), 5),
        (_entries("green.", "and"), 1),
        (_entries("green.", "and the"), 1),
    ]
    for n, words in enumerate(
        ["and the release", "and the release notes", "and the release notes are"]
    ):
        frames.append((_entries("green, so ship it" if n else "green.", words), 1))
    frames.append((_entries("green, so ship it", "and the release notes are out"), 6))
    stored, _ = _entry_run(frames)
    assert [text for _, text in stored] == ["green, so ship it", "and the release notes are out"]


def test_a_grown_middle_entry_is_stored_once() -> None:
    """XERK-2720: the entry above the grown one ("hello.") is its own entry too, so revising
    it doesn't reach into the grown one's words."""
    stored, _ = _entry_run(
        [
            (_entries("hello."), 5),
            (_entries("hello.", "green."), 5),
            (_entries("hello.", "green.", "and the notes"), 5),
            (_entries("hello.", "green, so ship it", "and the notes"), 6),
        ]
    )
    assert [text for _, text in stored] == ["hello.", "green, so ship it", "and the notes"]


def test_an_entry_scrolled_off_is_never_claimed_by_a_later_one() -> None:
    """QA (XERK-2720): with nothing on the frame aligning, the cut-at-the-next-entry draft of a
    short entry that has scrolled off was claimed by a later row holding its few words: the
    entry's segment took that row's text and was lost. Only an entry still on the last frame
    is cut out for a rewrite."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    frames = [
        (5, ("i that do.", "that that")),
        (10, ("i that i we do.", "that that do is.", "think we")),
        (
            24,
            (
                "i that i we do.",
                "that should that do is we that.",
                "think we should so i should.",
                "we it should that the is think",
            ),
        ),
    ]
    tail = (
        "that should that do is we that.",
        "think we should so i should.",
        "we it should that the is is think.",
        "we we that",
    )
    frames += [(28, tail), (31, tail)]
    final: dict[str, str] = {}
    first: str | None = None
    for t, rows in frames:
        for seg in m.feed(_entries(*rows), t * 1000):
            first = first or seg.segment_id
            final[seg.segment_id] = seg.text
    # Its segment keeps its words; Teams grew it ("i that i we do.", XERK-2728).
    assert first is not None and final[first] == "i that i we do."
    assert len(final.values()) == len(set(final.values()))  # no row stored twice


def _gapped_run(frames: list[list[CaptionBlock]], gap: int) -> list[tuple]:
    """Feed one frame every ``gap`` ms, flush; the stored (speaker, text) rows in order."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    final: dict[str, Segment] = {}
    for k, blocks in enumerate(frames):
        for seg in m.feed(blocks, k * gap):
            final[seg.segment_id] = seg
    for seg in m.flush(len(frames) * gap + 60_000):
        final[seg.segment_id] = seg
    rows = sorted(final.values(), key=lambda s: s.start_ms)
    return [(s.speaker, s.text) for s in rows if s.text]


@pytest.mark.parametrize("gap", [1000, 2000, 3000])
@pytest.mark.parametrize(
    ("short", "draft", "said", "grown", "tail"),
    [
        pytest.param(
            "Yes.",
            "yes we can.",
            "Yes we can ship it.",
            ("Yes we can ship it friday if the", "tests pass."),
            ("Yes we can ship it, after the", "review."),
            id="yes",
        ),
        pytest.param(
            "Okay.",
            "okay so the build.",
            "Okay so the build is green.",
            ("Okay so the build is green on the", "staging cluster."),
            ("Okay so the build is green, after the", "review."),
            id="okay",
        ),
    ],
)
def test_a_short_entry_scrolled_off_above_her_next_starting_with_its_words_is_kept(
    gap: int, short: str, draft: str, said: str, grown: tuple, tail: tuple
) -> None:
    """XERK-2799: Bo says "Yes." then entries starting "Yes we can ...". Once "Yes." scrolls
    off and the next entry is re-laid, the top run's draft cut at her next entry was "Yes."
    alone: its one word passed coverage inside the re-laid row, so every Bo segment shifted up
    an entry and "Yes." was lost. Gaps up to ``_SAID_BETWEEN_MS``, the 3 s gate."""
    frames = [
        [blk("Bo", short), blk("Bo", draft)],
        [blk("Bo", short), blk("Bo", said), blk("Bo", draft)],
        [blk("Bo", short), blk("Bo", said), blk("Bo", *grown)],
    ] + [[blk("Bo", *tail), blk("Bo", *grown), blk("Cy", "can you share the screen.")]] * 4
    assert _gapped_run(frames, gap) == [
        ("Bo", short),
        *[("Bo", row) for row in tail],
        *[("Bo", row) for row in grown],
        ("Cy", "can you share the screen."),
    ]


@pytest.mark.parametrize("gap", [1000, 2000, 3000])
@pytest.mark.parametrize("flicker", [False, True])
def test_a_short_entry_still_shown_and_rewritten_is_revised_in_place(
    gap: int, flicker: bool
) -> None:
    """QA (XERK-2799): "Yes." still at the panel's top and rewritten ("Yes, we can."), above
    her next entry re-laid. She shows as many header blocks as she has entries, so nothing
    scrolled off and the top row revises "Yes." (as main). Switching to her next entry kept
    "Yes." and stored "tests pass." twice. ``flicker``: the rewrite shows on one frame only."""
    grown = ("Yes we can ship it friday if the", "tests pass.")
    relaid = [blk("Bo", "Yes we can ship it, after the", "review."), blk("Bo", *grown)]
    cy = blk("Cy", "can you share the screen.")
    frames = [
        [blk("Bo", "Yes."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", *grown)],
        [blk("Bo", "Yes, we can."), *relaid, cy],
    ] + [[blk("Bo", "Yes." if flicker else "Yes, we can."), *relaid, cy]] * 3
    assert _gapped_run(frames, gap) == [
        ("Bo", "Yes, we can."),
        ("Bo", "Yes we can ship it, after the"),
        ("Bo", "review."),
        *[("Bo", row) for row in grown],
        ("Cy", "can you share the screen."),
    ]


@pytest.mark.parametrize("gap", [1000, 3000])
def test_a_rewrite_re_punctuated_like_her_stale_line_still_revises_her_short_entry(
    gap: int,
) -> None:
    """XERK-2774 QA: as above, with the rewrite differing from her stale "yes we can." (a
    row since grown) only in end punctuation. Matched as a re-punctuation of that line, the
    rewrite of "Yes." was lost and "tests pass." stored twice. That match needs the line's
    row to have last read with the same words."""
    grown = ("Yes we can ship it friday if the", "tests pass.")
    relaid = [blk("Bo", "Yes we can ship it, after the", "review."), blk("Bo", *grown)]
    cy = blk("Cy", "can you share the screen.")
    frames = [
        [blk("Bo", "Yes."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", *grown)],
    ] + [[blk("Bo", "Yes we can?"), *relaid, cy]] * 4
    assert _gapped_run(frames, gap) == [
        ("Bo", "Yes we can?"),
        ("Bo", "Yes we can ship it, after the"),
        ("Bo", "review."),
        *[("Bo", row) for row in grown],
        ("Cy", "can you share the screen."),
    ]


@pytest.mark.parametrize(
    ("frames", "kept"),
    [
        pytest.param(
            [
                [blk("Bo", "Okay.")],
                [blk("Bo", "Okay.")],
                [blk("Bo", "Okay."), blk("Bo", "okay, after")],
                [
                    blk("Bo", "Okay, after the review we merge."),
                    blk("Bo", "okay, after the review we"),
                    blk("Cy", "Yes."),
                ],
                [
                    blk("Bo", "Okay, after the review we merge."),
                    blk("Bo", "okay, after the review we merge that works"),
                    blk("Cy", "Yes."),
                ],
                [
                    blk("Bo", "okay, after the review we merge that works", "for"),
                    blk("Cy", "Yes."),
                    blk("Cy", "yes we"),
                ],
            ]
            + [
                [
                    blk("Bo", "Okay, after the review we merge that works", "for me."),
                    blk("Cy", "Yes."),
                    blk("Cy", "yes we can ship"),
                ]
            ]
            * 4,
            "Okay, after the review we merge.",
            id="latest-whole-entry",  # fuzz seed 891: both pair whole, the shown one is later
        ),
        pytest.param(
            [
                [blk("Bo", "yes")],
                [blk("Bo", "Yes."), blk("Bo", "yes,")],
                [blk("Bo", "Yes, I will share."), blk("Bo", "yes, i will")],
                [blk("Bo", "Yes, I will share."), blk("Bo", "yes, i will share"), blk("Cy", "ok")],
            ]
            + [[blk("Bo", "yes, i will share can", "you"), blk("Cy", "Okay so.")]] * 4,
            "Yes, I will share.",
            id="two-entries-off",  # fuzz seed 570: a stale "yes" draft above it scrolled too
        ),
    ],
)
def test_the_entry_a_top_run_re_lays_is_the_latest_it_pairs_whole(
    frames: list[list[CaptionBlock]], kept: str
) -> None:
    """QA fuzz (XERK-2799): the top run of a scrolled panel may pair every word of more than
    one of her stored entries (a stale draft, or the entry it grew from). Taking the one
    pairing most words, or only her next one, overwrote a shown entry with the live one."""
    stored = [text for _, text in _gapped_run(frames, 1000)]
    assert kept in stored


@pytest.mark.parametrize("gap", [1000, 2000, 3000])
@pytest.mark.parametrize("flicker", [False, True])
@pytest.mark.parametrize("misread", ["merged", "unmatched-mid", "headerless-mid", "unmatched-low"])
def test_a_short_entry_rewritten_under_a_misread_header_is_never_kept_beside_it(
    gap: int, flicker: bool, misread: str
) -> None:
    """QA (XERK-2799): "Yes." still shown and rewritten ("Yes, we can.") while OCR misreads a
    lower block's header (no roster match, no header, or its rows run into the block above).
    Counting her header blocks then saw an entry scrolled off and switched, keeping "Yes."
    beside its rewrite. The switch must pair strictly more of the whole panel's words."""
    grown = ("Yes we can ship it friday if the", "tests pass.")
    relaid = ("Yes we can ship it, after the", "review.")
    cy = blk("Cy", "can you share the screen.")
    if misread == "merged":
        lower = [blk("Bo", *relaid, *grown)]
    elif misread == "unmatched-mid":
        lower = [blk(None, *relaid), blk("Bo", *grown)]
    elif misread == "headerless-mid":
        lower = [blk(None, *relaid, header=False), blk("Bo", *grown)]
    else:
        lower = [blk("Bo", *relaid), blk(None, *grown)]
    frames = [
        [blk("Bo", "Yes."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", "yes we can.")],
        [blk("Bo", "Yes."), blk("Bo", "Yes we can ship it."), blk("Bo", *grown)],
        [blk("Bo", "Yes, we can."), *lower, cy],
    ]
    after = [blk("Bo", "Yes."), blk("Bo", *relaid), blk("Bo", *grown), cy]
    frames += [after if flicker else frames[-1]] * 3
    stored = [text for _, text in _gapped_run(frames, gap)]
    assert not ("Yes." in stored and "Yes, we can." in stored)


@pytest.mark.parametrize("missed", [0, 1])
def test_a_grown_entry_is_stored_once_after_ocr_missed_a_block_for_a_frame(missed: int) -> None:
    """QA (XERK-2720): one frame of OCR missing one of Ana's blocks just before her entry grows
    must not stop the draft being cut at her next entry. Requiring the cut entry on the very
    last frame stored the whole box again."""
    said = ["green.", "and the notes", "ok."]
    frames = [(_entries(*said[:k]), 5) for k in (1, 2, 3)]
    frames.append((_entries(*[t for n, t in enumerate(said) if n != missed]), 1))
    frames.append((_entries("green, so ship it", *said[1:]), 6))
    stored, _ = _entry_run(frames)
    assert [text for _, text in stored] == ["green, so ship it", "and the notes", "ok."]


def test_a_grown_entry_ocr_missed_for_3_s_is_stored_again_but_never_lost() -> None:
    """Accepted trade-off (XERK-2720 QA): an entry unseen for longer than _SAID_BETWEEN_MS is
    no longer cut out for a rewrite, so after OCR missed Ana's top block for 3 frames her grown
    entry stores the box again. Cutting such an entry let a later row claim a short entry that
    had scrolled off and lose it (``test_an_entry_scrolled_off_is_never_claimed_by_a_later_one``);
    a duplicate is the lesser harm. Every line said is still stored."""
    said = ["green.", "and the notes", "ok."]
    frames = [(_entries(*said[:k]), 5) for k in (1, 2, 3)]
    frames.append((_entries(*said[1:]), 3))
    frames.append((_entries("green, so ship it", *said[1:]), 6))
    stored, _ = _entry_run(frames)
    assert [text for _, text in stored] == [*said, "green, so ship it", *said[1:]]


# Caps count each stored segment once (``_simulate``, XERK-2721): main stores 25, 27, 31.
@pytest.mark.parametrize(("seed", "most"), [(145, 25), (325, 27), (599, 31)])
def test_a_zoom_speakers_scrolled_lines_are_revised_as_one_run(seed: int, most: int) -> None:
    """XERK-2720: Zoom lists a speaker's lines under one header, so a line the box scrolled to
    the block's top is marked as an entry start too. Cutting the draft there first (rather
    than only when the whole run fails) stored up to 4 more lines on these noisy, dropped-frame
    seeds."""
    _, stored = _simulate(seed, noise=True, skip=0.3)
    assert len(stored) <= most


def _simulate_teams(seed: int, *, noise: float = 0.0) -> tuple[int, int, int]:
    """Roll a Teams caption panel over 8 random entries: each grows a word per snapshot and is
    wrapped as one text, while Teams adds and drops provisional periods and inserts or fixes
    an earlier word (re-flowing the rows after it); a finished entry is capitalised and
    punctuated. With ``noise``, that share of snapshots garbles one row. Returns words said,
    and words stored beyond / short of what was said (per speaker, in order)."""
    import random
    import re
    from difflib import SequenceMatcher

    rng = random.Random(seed)
    vocab = (
        "so the build we need to check that cluster deploy test locally again repeat same "
        "process push code there itself asking change design what did go second task api "
        "contract then will do it or me can and pods enable right overlay logs fix"
    ).split()
    words_of = lambda text: re.findall(r"[\w']+", text.casefold())  # noqa: E731

    def wrap(words: list[str]) -> list[str]:
        rows: list[str] = []
        for w in words:
            if rows and len(rows[-1]) + 1 + len(w) <= 42:
                rows[-1] += f" {w}"
            else:
                rows.append(w)
        return rows

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, object] = {}
    entries: list[tuple[str, list[str]]] = []
    t = 0

    def snapshot() -> None:
        rows = [r for sp, words in entries for r in [("H", sp), *(("T", x) for x in wrap(words))]]
        rows = rows[-8:]
        if noise and rng.random() < noise:
            k = rng.randrange(len(rows))
            if rows[k][0] == "T":
                rows[k] = (
                    "T",
                    "".join("~" if c % 5 == 0 else ch for c, ch in enumerate(rows[k][1])),
                )
        blocks: list[CaptionBlock] = []
        for kind, val in rows:
            if kind == "H":
                blocks.append(blk(val))
            else:
                if not blocks:
                    blocks.append(blk(None, header=False))
                blocks[-1].lines.append(val)
        for seg in m.feed([b for b in blocks if b.lines], t):
            stored[seg.segment_id] = seg

    for _ in range(8):
        speaker = rng.choice([s for s in ("Ana", "Bo", "Cy") if not entries or s != entries[-1][0]])
        words: list[str] = []
        entries.append((speaker, words))
        for w in [rng.choice(vocab) for _ in range(rng.randint(6, 40))]:
            if words and words[-1].endswith("."):
                words[-1] = words[-1][:-1]
            words.append(w + ("." if rng.random() < 0.15 else ""))
            if rng.random() < 0.08 and len(words) > 4:
                at = rng.randint(max(0, len(words) - 12), len(words) - 2)
                if rng.random() < 0.5:
                    words.insert(at, rng.choice(vocab))
                else:
                    words[at] = rng.choice(vocab)
            t += rng.choice([300, 500, 700])
            snapshot()
        words[0] = words[0].capitalize()
        words[-1] += "" if words[-1].endswith(".") else "."
        for _ in range(rng.randint(2, 8)):
            t += 500
            snapshot()
    for seg in m.flush(t + 60_000):
        stored[seg.segment_id] = seg
    said_words = extra = lost = 0
    for speaker in ("Ana", "Bo", "Cy"):
        said = [w for sp, ws in entries if sp == speaker for w in words_of(" ".join(ws))]
        got = [
            w
            for seg in sorted(stored.values(), key=lambda s: s.start_ms)
            if seg.speaker == speaker
            for w in words_of(seg.text)
        ]
        said_words += len(said)
        for op, a1, a2, b1, b2 in SequenceMatcher(None, said, got, autojunk=False).get_opcodes():
            extra += max(0, (b2 - b1) - (a2 - a1)) if op in ("insert", "replace") else 0
            lost += max(0, (a2 - a1) - (b2 - b1)) if op in ("delete", "replace") else 0
    return said_words, extra, lost


def test_simulated_teams_reflow_stores_few_words_twice() -> None:
    """XERK-2715, measured over 200 seeds (38k words said): words stored twice 607 -> 139,
    words lost 522 -> 236. These 20 seeds stored 75 extra and lost 53 before the entry pass
    (23 and 24 after)."""
    totals = [sum(x) for x in zip(*(_simulate_teams(seed) for seed in range(20)), strict=True)]
    said, extra, lost = totals
    assert extra <= 0.008 * said, totals
    assert lost <= 0.01 * said, totals


# --- XERK-2693: no meeting from speaker-less text, no toolbar rows -----------------------

# Text the capture client cropped off a dark docs page in production (XERK-2693): bright text
# centred low on a dark page passes its caption-box finder.
_DOCS_PAGE = (
    "3 GitHub App - Tesoro Relea",
    "SHA256:loVVq1x8Zk the signing key",
    "organizations:read The VGS CLI's org lookup befc",
    "routes:write Applying routes Adding",
)


def _feed(tracker: MeetingTracker, frames, *, roster_names=()) -> None:
    """Each (ocr, jpeg) crop through the Zoom parser and the tracker, one a second, as ws does."""

    async def run() -> None:
        roster = await tracker.ensure_roster()
        for name in roster_names:
            roster.add(name)
        for i, (ocr, jpeg) in enumerate(frames):
            blocks = parse_caption_crop(
                ocr, roster, live=tracker.conversation_id is not None, jpeg=jpeg
            )
            await tracker.on_caption(blocks, i * 1000)
        if tracker.conversation_id is not None:
            await tracker.end(len(frames) * 1000 + 60_000)

    asyncio.run(run())


@pytest.mark.parametrize("names", [(), ("Ana Lopez",)])
def test_a_dark_docs_page_in_the_caption_band_starts_no_meeting(names: tuple) -> None:
    """Two false meetings were docs pages read as speaker-less captions, then sent to Turma."""
    store = InMemoryConversationStore()
    no_pixels = [(crop(*_DOCS_PAGE), None)] * 12
    _feed(_tracker(store), no_pixels, roster_names=names)
    # A heading over its text is name-shaped, but drawn as bright as the text: not a speaker.
    heading = panel_crop(("Tesoro Release Notes", False), *((r, False) for r in _DOCS_PAGE))
    _feed(_tracker(store), [heading] * 12, roster_names=names)
    assert store.list("default") == []


def test_an_off_roster_speaker_s_dim_name_still_starts_a_meeting() -> None:
    """The pixel check must not refuse a real speaker the tiles don't show (a screen share)."""
    store = InMemoryConversationStore()
    frame = panel_crop(("Bo Chen", True), ("we shipped the release on friday", False))
    _feed(_tracker(store), [frame] * 6, roster_names=("Ana Lopez",))
    (conv,) = store.list("default")
    assert [s.text for s in store.get("default", conv.id).segments] == [
        "we shipped the release on friday"
    ]


def test_header_less_lines_mid_meeting_are_still_kept() -> None:
    # Live, an entry whose name line scrolled off the crop is still that speaker's speech.
    blocks = parse_caption_crop(crop(*_DOCS_PAGE[:1]), Roster(), live=True)
    assert [b.lines for b in blocks] == [[_DOCS_PAGE[0]]]


@pytest.mark.parametrize(
    "toolbar",
    [
        ("Participants Chat React Share More",),
        ("^ D ^ A ^",),
        ("Mute Stop Video Participants", "^ ^"),
        ("Mute ^ Stop Video ^ Participants ^ Chat React Share More",),
        # Exactly three quarters labels.
        ("Chat Share More today",),
    ],
)
def test_zooms_toolbar_rows_in_the_caption_crop_are_not_stored(toolbar: tuple) -> None:
    """Seen ~10 times in one real meeting: the toolbar under the panel stored as captions."""
    store = InMemoryConversationStore()
    frame = crop("Ana Lopez", "we should ship it today.", *toolbar)
    _feed(_tracker(store), [(frame, None)] * 6, roster_names=("Ana Lopez",))
    (conv,) = store.list("default")
    assert [s.text for s in store.get("default", conv.id).segments] == ["we should ship it today."]
    # A toolbar row alone starts nothing.
    assert parse_caption_crop(crop(*toolbar), Roster()) == []


@pytest.mark.parametrize(
    "speech",
    [
        "share more of it",
        "Share more of it",
        "we can chat more",
        "More video please",
        # Two labels, under three quarters of the row: speech, not the toolbar.
        "Share Screen with the team",
        # Lower-case toolbar words are speech however many there are.
        "chat share more",
        # Two labels of three: under three quarters.
        "Share Screen today",
    ],
)
def test_speech_using_toolbar_words_is_kept(speech: str) -> None:
    blocks = parse_caption_crop(crop("Ana Lopez", speech), _roster("Ana Lopez"))
    assert [b.lines for b in blocks] == [[speech]]


@pytest.mark.parametrize("name", ["Raman, Priya", "Bo Chen (Guest)"])
def test_a_dim_last_first_or_guest_name_starts_a_meeting_and_is_not_speech(name: str) -> None:
    """QA (XERK-2693): "Last, First" and suffixed names aren't name-shaped, so they never
    vouched for a header and the meeting never started; before, they were stored as speech."""
    for names in ((), (name,)):
        store = InMemoryConversationStore()
        frame = panel_crop((name, True), ("we shipped the release on friday", False))
        _feed(_tracker(store), [frame] * 6, roster_names=names)
        (conv,) = store.list("default")
        assert [s.text for s in store.get("default", conv.id).segments] == [
            "we shipped the release on friday"
        ]


@pytest.mark.parametrize(
    "grey",
    [
        "updated 3 days ago",
        "by jsmith",
        "Edit this page",
        "required",
        "see also",
        "v2.3.1",
    ],
)
def test_a_grey_meta_line_on_a_docs_page_starts_no_meeting(grey: str) -> None:
    """Delta QA: letting a dim row of any shape vouch for a start let these start meetings."""
    store = InMemoryConversationStore()
    frame = panel_crop(
        ("Release Notes", False), (grey, True, False), *((r, False) for r in _DOCS_PAGE)
    )
    for names in ((), ("Ana Lopez",)):
        _feed(_tracker(store), [frame] * 6, roster_names=names)
    assert store.list("default") == []


@pytest.mark.parametrize("name", ["jsmith", "malcolm h"])
@pytest.mark.parametrize("light", [False, True])
def test_a_lower_case_speaker_beside_an_avatar_starts_a_meeting(name: str, light: bool) -> None:
    """XERK-2700: only a capitalised dim name vouched for a start, so a meeting whose speakers
    all had lower-case display names never started. Zoom's avatar circle beside the name
    tells it from a docs page's grey line ("by jsmith" has none), roster or not."""
    for names in ((), ("Ana Lopez",)):
        store = InMemoryConversationStore()
        frame = panel_crop((name, True), ("we shipped the release on friday", False), light=light)
        _feed(_tracker(store), [frame] * 6, roster_names=names)
        (conv,) = store.list("default")
        segments = store.get("default", conv.id).segments
        assert [s.text for s in segments] == ["we shipped the release on friday"]


def test_the_avatar_needs_a_round_patch_level_with_the_name() -> None:
    """A square icon, a bullet, or nothing left of a dim name line is no avatar; a row
    flush with the crop's left edge has no room for one."""
    ocr, jpeg = panel_crop(("Bo Chen", True), ("we shipped it", False))
    groups = zoom.row_groups(ocr.lines)
    px = zoom._grey_pixels(jpeg, ocr, groups)
    assert px is not None
    assert zoom._has_avatar(px, groups[0]) is True
    assert zoom._has_avatar(px, groups[1]) is False  # its text row: the circle is above it
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw.rectangle((8, 6, 36, 34), fill=(230, 200, 180))  # a square picture, not a circle
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    square = zoom._grey_pixels(buf.getvalue(), ocr, groups)
    assert square is not None and zoom._has_avatar(square, groups[0]) is False
    flush = [OcrLine("Bo Chen", (2.0, *groups[0][0].box[1:]))]
    assert zoom._has_avatar(px, flush) is False


def _cut_left(frame: tuple[OcrResult, bytes], x: int) -> tuple[OcrResult, bytes]:
    """``frame`` cut ``x`` px in from the left, as a capture client before XERK-2700 sent it:
    the crop began at the glyphs, so a dark avatar left of the name was cut off."""
    ocr, jpeg = frame
    img = Image.open(io.BytesIO(jpeg)).crop((x, 0, ocr.width, ocr.height))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    lines = tuple(
        OcrLine(ln.text, (ln.box[0] - x, ln.box[1], ln.box[2] - x, ln.box[3])) for ln in ocr.lines
    )
    return OcrResult(img.width, img.height, lines), buf.getvalue()


@pytest.mark.parametrize("avatar", ["cut off", "none", "navy", "square"])
def test_with_no_avatar_seen_the_capitalised_name_rule_stands(avatar: str) -> None:
    """QA (XERK-2700): requiring an avatar stopped meetings of off-roster speakers whose avatar
    wasn't seen: cut off by an older client, not drawn, too close to the panel's colour, or not
    round. The avatar only adds: a capitalised dim name vouches without one, a lower-case one
    doesn't (a docs page's grey "by jsmith" looks the same)."""
    text = ("we shipped the release on friday", False)
    for name, starts in (("Bo Chen", True), ("Raman, Priya", True), ("jsmith", False)):
        frame = panel_crop((name, True, avatar == "cut off"), text)
        if avatar == "cut off":
            frame = _cut_left(frame, 44)
        elif avatar in ("navy", "square"):
            ocr, jpeg = frame
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            draw = ImageDraw.Draw(img)
            shape = draw.ellipse if avatar == "navy" else draw.rectangle
            shape((8, 6, 36, 34), fill=(20, 40, 90) if avatar == "navy" else (230, 200, 180))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            frame = (ocr, buf.getvalue())
        store = InMemoryConversationStore()
        _feed(_tracker(store), [frame] * 6, roster_names=("Ana Lopez",))
        assert len(store.list("default")) == starts, name


def test_with_no_text_row_to_compare_the_position_header_still_starts() -> None:
    """Every row name-shaped ("Bo Chen" / "Sounds Good"): there is no caption-text row to
    read the name's dimness against, so the position rule's header stands, as on main (QA)."""
    store = InMemoryConversationStore()
    frame = panel_crop(("Bo Chen", True, False), ("Sounds Good", False))
    _feed(_tracker(store), [frame] * 6)
    (conv,) = store.list("default")
    assert [s.text for s in store.get("default", conv.id).segments] == ["Sounds Good"]


def test_a_bright_name_shaped_row_beside_an_avatar_vouches_for_nothing() -> None:
    """With a text row to compare against, the vouching row must be dim: a docs heading drawn
    as bright as its text, with a round icon beside it, is no speaker (QA mutant)."""
    store = InMemoryConversationStore()
    frame = panel_crop(("Release Notes", False, True), *((r, False) for r in _DOCS_PAGE))
    _feed(_tracker(store), [frame] * 6)
    assert store.list("default") == []


def test_a_circle_starting_below_the_name_s_middle_is_not_its_avatar() -> None:
    """A circle centred lower than Zoom's avatar sits (past ``_AVATAR_CENTRE``) belongs to
    something below the name, an emoji or bullet on the next row (QA mutant)."""
    ocr, jpeg = panel_crop(("Bo Chen", True, False), ("we shipped it", False))
    groups = zoom.row_groups(ocr.lines)
    y0, y1 = groups[0][0].box[1], groups[0][0].box[3]
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    top = int((y0 + y1) / 2) + 3
    ImageDraw.Draw(img).ellipse((8, top, 36, top + 28), fill=(230, 200, 180))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    px = zoom._grey_pixels(buf.getvalue(), ocr, groups)
    assert px is not None and zoom._has_avatar(px, groups[0]) is False


def _with_disc(frame: tuple[OcrResult, bytes], centre: float, d: int) -> bytes:
    """``frame``'s JPEG with a filled disc ``d`` px across left of the rows, centred at y."""
    ocr, jpeg = frame
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    top = round(centre - d / 2)
    ImageDraw.Draw(img).ellipse((44 - d - 4, top, 44 - 4, top + d), fill=(230, 200, 180))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.mark.parametrize("name", ["jsmith", "vera cruz", "anna"])
def test_zooms_real_avatar_astride_the_name_and_its_text_starts_a_meeting(name: str) -> None:
    """XERK-2705: real Zoom crops centre the avatar astride the name and its first text row,
    2-6 px below the name box. Requiring it to start level with the name (the fixture's
    shape) found no avatar on any of 22 real name rows, so a lower-case off-roster speaker
    never started a meeting. A name of x-height letters ("vera cruz") gets a shorter, lower
    OCR box while the avatar stays put: a window in name heights missed it (QA)."""
    frame = panel_crop(("earlier entry", False), (name, True, False), ("we shipped it", False))
    ocr, _ = frame
    groups = zoom.row_groups(ocr.lines)
    (_, y0, _, y1), (_, by0, _, by1) = groups[1][0].box, groups[2][0].box
    # Real crops centre it 0.1-0.22 of the text row's height past the rows' gap midpoint; the
    # fixture's gap is wider, so a bigger disc reaches up to overlap the name as there (QA).
    real = _with_disc(frame, (y1 + by0) / 2 + 0.2 * (by1 - by0), 32)
    px = zoom._grey_pixels(real, ocr, groups)
    assert px is not None and zoom._has_avatar(px, groups[1], groups[2]) is True
    assert parse_caption_crop(ocr, Roster(), jpeg=real) != []
    # Not the name's avatar, and no start: a disc centred above the name's middle (a docs
    # icon over the line), or a big one centred low beside the text row (an emoji, a photo).
    hi = (y1 + by0) / 2 + 0.3 * (by1 - by0)  # _AVATAR_BELOW
    for centre, d in (((y0 + y1) / 2 - 0.25 * (y1 - y0) - 3, 26), (hi + 2, 40)):
        other = _with_disc(frame, centre, d)
        px = zoom._grey_pixels(other, ocr, groups)
        assert px is not None and zoom._has_avatar(px, groups[1], groups[2]) is False, centre
        assert parse_caption_crop(ocr, Roster(), jpeg=other) == [], centre


@pytest.mark.parametrize("grey", ["Parameters:", "Note,", "x" * 30 + " " + "y" * 40])
def test_a_dim_row_that_is_no_display_name_stays_text(grey: str) -> None:
    ocr, jpeg = panel_crop(("Ana Lopez", True), ("we shipped it", False), (grey, True))
    blocks = parse_caption_crop(ocr, _roster("Ana Lopez"), live=True, jpeg=jpeg)
    assert [b.lines for b in blocks] == [["we shipped it", grey]]


WRAPPED_ROWS = {
    # Ana's entry grows and wraps onto a second row above Bo's entry.
    "above_next_speaker": (
        [[blk("Ana", "we ship"), blk("Bo", "ok")]] * 4
        + [[blk("Ana", "we ship the build", "on friday"), blk("Bo", "ok")]] * 5,
        [("Ana", "we ship the build"), ("Ana", "on friday"), ("Bo", "ok")],
    ),
    # ...and above her own identical next entry (XERK-2719's twin).
    "above_same_text_twin": (
        [[blk("Ana", "notes"), blk("Ana", "notes")]] * 4
        + [[blk("Ana", "notes, ship", "it"), blk("Ana", "notes")]] * 5,
        [("Ana", "notes, ship"), ("Ana", "it"), ("Ana", "notes")],
    ),
}


@pytest.mark.parametrize("case", WRAPPED_ROWS)
def test_a_grown_entrys_wrapped_row_is_stored_before_the_next_entry(case: str) -> None:
    """XERK-2729: a row Teams wraps onto an entry above a later one was first seen after it,
    so its start (first-seen time) stored it after that entry. It is ordered just above the
    entry below it, which moves down a millisecond (same segment, upserted)."""
    frames, said = WRAPPED_ROWS[case]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str | None, str]] = {}
    for t, blocks in enumerate(frames):
        for seg in m.feed(blocks, t * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
            assert seg.end_ms >= seg.start_ms
    for seg in m.flush(len(frames) * 1000 + 10_000):
        stored[seg.segment_id] = (seg.start_ms, seg.speaker, seg.text)
    rows = sorted(stored.values())
    assert [(sp, text) for _, sp, text in rows] == said
    assert len({start for start, _, _ in rows}) == len(rows)  # no ties


@pytest.mark.parametrize("shown_for", [5, 25, 35])
def test_a_row_added_above_an_old_entry_is_stored_before_it_once(shown_for: int) -> None:
    """XERK-2738: Teams adds a row to Bo's entry above Cy's. Once Cy's line was on screen past
    ``_REVISE_MS``, nothing paired her row with it: "Okay." was stored again and Bo's row
    after the first copy. Her row re-reads it verbatim, so it is that line at any age."""
    first = blk("Bo", "so or what like so so so like itself")
    grown = blk("Bo", "so or what like so so so like itself", "cluster logs build")
    cy = blk("Cy", "Okay.")
    stored, _ = _entry_run([([first, cy], shown_for), ([grown, cy], 5)])
    assert stored == [
        ("Bo", "so or what like so so so like itself"),
        ("Bo", "cluster logs build"),
        ("Cy", "Okay."),
    ]


def test_an_old_line_is_never_rewritten_by_a_new_row_resembling_it() -> None:
    """XERK-2738's guard: an old line is offered only to its verbatim re-read. When the claims
    would hand it another row ("Okay, sure." above a re-read "Okay."), nothing is revised:
    Cy's stored "Okay." keeps its text."""
    first = blk("Bo", "so or what like so so so like itself")
    grown = blk("Bo", "so or what like so so so like itself", "cluster logs build")
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    texts: dict[str, str] = {}
    cy_id = None
    for t in range(40):
        if t < 35:
            blocks = [first, blk("Cy", "Okay.")]
        else:
            blocks = [grown, blk("Cy", "Okay, sure.", "Okay.")]
        for seg in m.feed(blocks, t * 1000):
            texts[seg.segment_id] = seg.text
            if cy_id is None and seg.speaker == "Cy":
                cy_id = seg.segment_id
    assert cy_id is not None
    assert texts[cy_id] == "Okay."
    assert "Okay, sure." in texts.values()


def test_an_older_twin_of_a_line_never_spoils_a_revision() -> None:
    """XERK-2738 (QA): Bo said "yes" twice, the first over ``_REVISE_MS`` ago. Offering that
    old copy too took the recent one's claim, so nothing was revised and Bo's "yes" and Ana's
    entry were stored again. Only the newest copy of a text is offered."""
    a1 = "need asking same did"
    a3 = ("again task same task need", "again that api")
    stored, _ = _entry_run(
        [
            ([blk("Bo", "yes")], 40),
            ([blk("Bo", "yes"), blk("Ana", a1)], 8),
            ([blk("Bo", "yes"), blk("Ana", a1), blk("Bo", "yes")], 6),
            ([blk("Bo", "yes"), blk("Ana", a1), blk("Bo", "yes"), blk("Ana", *a3)], 2),
            ([blk("Bo", "yes", "we ship it"), blk("Ana", *a3)], 8),
        ]
    )
    assert stored == [
        ("Bo", "yes"),
        ("Ana", a1),
        ("Bo", "yes"),
        ("Bo", "we ship it"),
        ("Ana", a3[0]),
        ("Ana", a3[1]),
    ]


def test_rows_re_reading_an_old_repeated_line_each_pair_with_a_copy() -> None:
    """XERK-2738 (QA): Bo said "yes" twice, both copies over ``_REVISE_MS`` old, and Ana's
    entry above gained a row. Two rows read "yes", so both copies are offered; offering only
    the newest left the other row new, and Bo's lines and Cy's were stored again."""
    x, a1 = "so or what like so so so like itself", "need asking same did"
    box = [blk("Bo", "yes"), blk("Cy", a1), blk("Bo", "yes")]
    stored, _ = _entry_run(
        [([blk("Ana", x), *box], 40), ([blk("Ana", x, "cluster logs build"), *box], 8)]
    )
    assert stored == [
        ("Ana", x),
        ("Ana", "cluster logs build"),
        ("Bo", "yes"),
        ("Cy", a1),
        ("Bo", "yes"),
    ]


def test_another_speakers_copy_of_an_old_line_does_not_hide_it() -> None:
    """XERK-2738 (QA): only the speaker's own later copies count against an old line. Bo's
    "yes" below Cy's is not hers, so her row still re-reads her line."""
    x = "so or what like so so so like itself"
    stored, _ = _entry_run(
        [
            ([blk("Bo", x), blk("Cy", "yes"), blk("Bo", "yes")], 35),
            ([blk("Bo", x, "cluster logs build"), blk("Cy", "yes"), blk("Bo", "yes")], 5),
        ]
    )
    assert stored == [("Bo", x), ("Bo", "cluster logs build"), ("Cy", "yes"), ("Bo", "yes")]


def test_an_old_line_no_row_re_reads_verbatim_is_not_offered() -> None:
    """XERK-2738 (QA): an old line joins the revision candidates only when a new row reads it
    exactly. Offering every old line let Cy's rewritten entry miss a candidate, and Bo's row
    above was stored a second time."""
    x, y = "so or what like so so so like itself", "cluster logs build"
    stored, _ = _entry_run(
        [
            ([blk("Bo", x), blk("Cy", "Okay.", "let me check")], 35),
            ([blk("Bo", x, y), blk("Cy", "Okay, let me check that")], 2),
            ([blk("Bo", x, y), blk("Cy", "Okay, let me check that"), blk("Cy", "Okay.")], 5),
        ]
    )
    assert stored.count(("Bo", x)) == 1
    assert stored.count(("Bo", y)) == 1


def test_lines_after_a_wrapped_row_keep_unique_increasing_starts() -> None:
    """XERK-2729: lines first seen in one snapshot are a millisecond apart, so making room
    above one moves every line after it down; a line said later still sorts last."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    stored: dict[str, tuple[int, str]] = {}
    frames = [[blk("Ana", "we ship"), blk("Bo", "ok"), blk("Cy", "sure"), blk("Di", "fine")]] * 4
    frames += [
        [blk("Ana", "we ship the build", "on friday"), blk("Bo", "ok"), blk("Cy", "sure"),
         blk("Di", "fine")]
    ] * 5
    frames += [
        [blk("Ana", "we ship the build", "on friday"), blk("Bo", "ok"), blk("Cy", "sure"),
         blk("Di", "fine"), blk("Ed", "agreed")]
    ] * 5
    for t, blocks in enumerate(frames):
        for seg in m.feed(blocks, t * 1000):
            stored[seg.segment_id] = (seg.start_ms, seg.text)
            assert seg.end_ms >= seg.start_ms  # a moved line's end moves with it
    for seg in m.flush(30_000):
        stored[seg.segment_id] = (seg.start_ms, seg.text)
        assert seg.end_ms >= seg.start_ms
    rows = sorted(stored.values())
    assert [text for _, text in rows] == [
        "we ship the build", "on friday", "ok", "sure", "fine", "agreed",
    ]
    assert len({start for start, _ in rows}) == len(rows)


def test_making_room_never_moves_or_re_emits_a_carried_line() -> None:
    """XERK-2729 (QA): carried lines sit at the front of the known lines, so the feed path never
    has one after the place a line is made room at. Should it, the ended meeting's segment keeps
    its start and is not re-emitted into this conversation."""
    from api.screen.merge import _Line

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    m.feed([blk("Ana", "we ship"), blk("Bo", "ok")], 0)
    old = Segment(segment_id="old", text="earlier", start_ms=5000, end_ms=5000, lang="en")
    carried = _Line("Cy", "earlier", "earlier", 2, 2, 0, segment=old, carried=True)
    m._lines.append(carried)  # after Bo's line, which the new line goes above
    for ln in m._lines:
        if not ln.carried:
            m._finalize(ln)
    out: dict = {}
    new = _Line("Ana", "on friday", "on friday", 1000, 1000, 1000)
    m._place_above(new, 1, out)
    assert "old" not in out and old.start_ms == 5000
    bo = m._lines[1]
    assert new.order_ms == 1 and bo.order_ms == 2  # the live line still moves down
    assert set(out) == {bo.segment.segment_id}


# --- XERK-2736: an entry re-attributed to another speaker ---------------------------------


def _once(out: list[Segment]) -> list[tuple[str | None, str]]:
    """Each segment once, as last written (an upsert re-emits it), in store order."""
    once = {s.segment_id: s for s in out}
    return [(s.speaker, s.text) for s in sorted(once.values(), key=lambda s: s.start_ms)]


def test_an_entry_re_attributed_to_another_speaker_is_stored_once_under_the_new_name() -> None:
    """The ticket's repro: Teams re-labels Bob's stored entry as Ana's, rows unchanged."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", "we ship today"), blk("Cy", "ok")], t * 1000)
    for t in range(4, 8):
        out += m.feed([blk("Ana", "we ship today"), blk("Cy", "ok")], t * 1000)
    out += m.flush(20_000)
    assert _once(out) == [("Ana", "we ship today"), ("Cy", "ok")]


def test_a_re_attributed_live_entry_keeps_growing_under_the_new_name() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    out += m.feed([blk("Cy", "ok"), blk("Bob", "we ship it")], 0)
    out += m.feed([blk("Cy", "ok"), blk("Ana", "we ship it")], 1000)
    out += m.feed([blk("Cy", "ok"), blk("Ana", "we ship it")], 2000)
    out += m.feed([blk("Cy", "ok"), blk("Ana", "we ship it today")], 3000)
    out += m.flush(20_000)
    assert _once(out) == [("Cy", "ok"), ("Ana", "we ship it today")]


def test_a_name_misread_on_one_frame_changes_nothing() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", "we ship today"), blk("Cy", "ok")], t * 1000)
    out += m.feed([blk("Bo", "we ship today"), blk("Cy", "ok")], 4000)
    for t in range(5, 8):
        out += m.feed([blk("Bob", "we ship today"), blk("Cy", "ok")], t * 1000)
    out += m.flush(20_000)
    assert _once(out) == [("Bob", "we ship today"), ("Cy", "ok")]


@pytest.mark.parametrize("gap_ms", [2000, 3000, 5000])
def test_another_speaker_repeating_a_line_and_its_reply_after_dropped_frames_stores_both(
    gap_ms: int,
) -> None:
    """XERK-2727 QA's shape: with time between the snapshots for Cy to say X and Bo "ok"
    again, the same rows under a new header are a repeat, never a re-attribution."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Ana", "we ship it today"), blk("Bo", "ok")], t * 1000)
    for t in range(4):
        out += m.feed([blk("Cy", "we ship it today"), blk("Bo", "ok")], 3000 + gap_ms + t * 1000)
    out += m.flush(60_000)
    assert _once(out) == [
        ("Ana", "we ship it today"),
        ("Bo", "ok"),
        ("Cy", "we ship it today"),
        ("Bo", "ok"),
    ]


def test_a_cross_speaker_repeat_round_at_a_two_second_cadence_keeps_every_line() -> None:
    """XERK-2797 QA: Teams frames reach the merger ~2 s apart, time for Cy to say X again.
    The same rows under a new header are then a repeat; a re-attribution reads the same, so
    it is stored twice (accepted: widening ``_REHEAD_MS`` to the cadence lost these lines)."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(5):
        out += m.feed([blk("Dee", "thank you all so much"), blk("Dee", "yes")], t * 2000)
    for t in range(5, 9):
        out += m.feed([blk("Cy", "thank you all so much"), blk("Dee", "yes")], t * 2000)
    out += m.flush(60_000)
    assert _once(out) == [
        ("Dee", "thank you all so much"),
        ("Dee", "yes"),
        ("Cy", "thank you all so much"),
        ("Dee", "yes"),
    ]


def test_another_speaker_repeating_a_line_shown_as_it_is_said_stores_both() -> None:
    """At 1 fps a repeat is seen growing, so its frames never read the old rows exactly."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(3):
        out += m.feed([blk("Ana", "we ship it today"), blk("Bo", "ok")], t * 1000)
    frames = [
        [blk("Ana", "we ship it today"), blk("Bo", "ok"), blk("Cy", "we ship")],
        [blk("Bo", "ok"), blk("Cy", "we ship it today")],
        [blk("Cy", "we ship it today"), blk("Bo", "ok")],
        [blk("Cy", "we ship it today"), blk("Bo", "ok")],
        [blk("Cy", "we ship it today"), blk("Bo", "ok")],
    ]
    for k, frame in enumerate(frames):
        out += m.feed(frame, 3000 + k * 1000)
    out += m.flush(60_000)
    assert _once(out) == [
        ("Ana", "we ship it today"),
        ("Bo", "ok"),
        ("Cy", "we ship it today"),
        ("Bo", "ok"),
    ]


def test_a_short_reply_said_again_as_the_panel_scrolls_is_kept() -> None:
    """[Bo thanks][Ana thanks] -> [Ana thanks][Ana thanks]: Ana thanked again within a
    frame, and the rows read as the panel scrolled up a row (cross-speaker fuzz, seed 2)."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    out += m.feed([blk("Bo", "thanks"), blk("Ana", "thanks")], 0)
    for t in range(1, 5):
        out += m.feed([blk("Ana", "thanks"), blk("Ana", "thanks")], t * 1000)
    out += m.flush(20_000)
    assert _once(out) == [("Bo", "thanks"), ("Ana", "thanks"), ("Ana", "thanks")]


def test_a_short_entry_re_attributed_is_stored_again_but_never_lost() -> None:
    """Accepted: under ``_REHEAD_WORDS`` a re-labelled reply is indistinguishable from the
    reply said again whole between frames, and a duplicate beats losing speech."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", "sounds good"), blk("Cy", "ok")], t * 1000)
    for t in range(4, 8):
        out += m.feed([blk("Ana", "sounds good"), blk("Cy", "ok")], t * 1000)
    out += m.flush(20_000)
    assert ("Bob", "sounds good") in _once(out) and ("Ana", "sounds good") in _once(out)


X = "can you hear me"


@pytest.mark.parametrize("ana_frames", [1, 2])
def test_a_cross_speaker_repeat_on_a_one_row_panel_keeps_every_line(ana_frames: int) -> None:
    """QA: with no row keeping its name, nothing shows the panel didn't turn over. Holding
    Ana's one frame lost her line; two frames re-labelled Bob's line as hers."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    frames = [[blk("Bob", "can")], [blk("Bob", X)], [blk("Bob", X)]]
    frames += [[blk("Ana", X)]] * ana_frames + [[blk("Cy", "yes")], [blk("Cy", "yes")]]
    out = []
    for k, frame in enumerate(frames):
        out += m.feed(frame, k * 1000)
    out += m.flush(60_000)
    assert _once(out) == [("Bob", X), ("Ana", X), ("Cy", "yes")]


def test_a_goodbye_round_said_again_in_full_keeps_every_line() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    g = "thanks everyone see you"
    out = []
    for t in range(3):
        out += m.feed([blk("Ana", g), blk("Bob", g)], t * 1000)
    for t in range(3, 6):
        out += m.feed([blk("Cy", g), blk("Dee", g)], t * 1000)
    out += m.flush(60_000)
    assert _once(out) == [("Ana", g), ("Bob", g), ("Cy", g), ("Dee", g)]


def test_a_long_line_said_again_as_the_panel_scrolls_is_kept() -> None:
    """The names read as the last frame's scrolled up a row: Ana said her line again."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    out += m.feed([blk("Bo", X), blk("Ana", X)], 0)
    for t in range(1, 5):
        out += m.feed([blk("Ana", X), blk("Ana", X)], t * 1000)
    out += m.flush(60_000)
    assert _once(out) == [("Bo", X), ("Ana", X), ("Ana", X)]


def test_a_name_misread_on_one_frame_before_a_scroll_re_labels_nothing() -> None:
    """The next frame doesn't agree, so the held frame is merged as usual (main's duplicate),
    and Bob keeps his line."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(3):
        out += m.feed([blk("Bob", X), blk("Cy", "ok")], t * 1000)
    out += m.feed([blk("Bo", X), blk("Cy", "ok")], 3000)
    for t in range(4, 7):
        out += m.feed([blk("Cy", "ok"), blk("Dee", "hi there")], t * 1000)
    out += m.flush(60_000)
    stored = _once(out)
    assert stored[:2] == [("Bob", X), ("Cy", "ok")]
    assert ("Dee", "hi there") in stored


def test_a_held_frame_s_repeat_is_stored_when_the_panel_moves_on() -> None:
    """[Ana X][Bo ok] -> [Cy X][Bo ok] once, then the panel scrolls: Cy did repeat X."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(3):
        out += m.feed([blk("Ana", X), blk("Bo", "ok")], t * 1000)
    out += m.feed([blk("Cy", X), blk("Bo", "ok")], 3000)
    for t in range(4, 7):
        out += m.feed([blk("Bo", "ok"), blk("Dee", "hi there")], t * 1000)
    out += m.flush(60_000)
    assert _once(out) == [
        ("Ana", X), ("Bo", "ok"), ("Cy", X), ("Bo", "ok"), ("Dee", "hi there")
    ]


def test_a_re_attributed_entry_wrapping_onto_a_short_row_is_stored_once() -> None:
    """The word gate counts the entry, not each row (QA: a 2-word last row blocked it)."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    rows = ("we ship the release today and", "then test")
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", *rows), blk("Cy", "ok")], t * 1000)
    for t in range(4, 8):
        out += m.feed([blk("Ana", *rows), blk("Cy", "ok")], t * 1000)
    out += m.flush(20_000)
    assert _once(out) == [("Ana", rows[0]), ("Ana", rows[1]), ("Cy", "ok")]


def test_an_entry_with_a_same_text_twin_is_never_re_attributed() -> None:
    """QA fuzz seed 936: Cy says G twice; the merger folded the second G into the first
    (scrolled off a frame, then back), so re-labelling "that" line lost one of Cy's."""
    g = "sounds good to me"
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    frames = [
        [blk("Ana", "let us do that"), blk("Cy", g)],
        [blk("Cy", g), blk("Dee", "let")],
        [blk("Dee", "let us do that"), blk("Cy", "sounds")],
        [blk("Cy", g), blk("Cy", "sounds good to")],
        [blk("Cy", g), blk("Cy", g)],
    ] + [[blk("Ana", g), blk("Cy", g)]] * 6
    out = []
    for k, frame in enumerate(frames):
        out += m.feed(frame, k * 1000)
    out += m.flush(60_000)
    stored = _once(out)
    assert stored.count(("Cy", g)) >= 2, stored


def test_a_misread_name_then_the_real_one_re_labels_to_the_real_one() -> None:
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", X), blk("Cy", "ok")], t * 1000)
    out += m.feed([blk("Bib", X), blk("Cy", "ok")], 4000)
    for t in range(5, 9):
        out += m.feed([blk("Ana", X), blk("Cy", "ok")], t * 1000)
    out += m.flush(60_000)
    assert _once(out) == [("Ana", X), ("Cy", "ok")]


def test_names_read_on_one_frame_each_re_label_nothing() -> None:
    """Bib then Ana for a frame each, then the panel moves on: no two frames agreed, so
    Bob keeps his line (QA: no test needed the names to agree)."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    out = []
    for t in range(4):
        out += m.feed([blk("Bob", X), blk("Cy", "ok")], t * 1000)
    out += m.feed([blk("Bib", X), blk("Cy", "ok")], 4000)
    out += m.feed([blk("Ana", X), blk("Cy", "ok")], 5000)
    for t in range(6, 9):
        out += m.feed([blk("Cy", "ok"), blk("Dee", "hi there")], t * 1000)
    out += m.flush(60_000)
    stored = _once(out)
    assert stored[:2] == [("Bob", X), ("Cy", "ok")], stored
    assert ("Dee", "hi there") in stored


def test_a_carried_entry_read_under_another_name_is_never_re_attributed() -> None:
    """The ended meeting's segment keeps its speaker (XERK-2615): main's behaviour stands."""
    m1 = CaptionMerger(start_ms=0, stable_ms=3000)
    old = []
    for t in range(4):
        old += m1.feed([blk("Bob", "we ship today"), blk("Cy", "ok")], t * 1000)
    old += m1.flush(10_000)
    m2 = CaptionMerger(start_ms=65_000, stable_ms=3000, carried=m1.recent())
    m2.feed([blk("Bob", "we ship today"), blk("Cy", "ok")], 65_000)
    for t in range(66, 70):
        m2.feed([blk("Ana", "we ship today"), blk("Cy", "ok")], t * 1000)
    assert _once(old) == [("Bob", "we ship today"), ("Cy", "ok")]


# XERK-2746: Teams re-lays the bottom (live) entry onto fewer rows.
_CY = blk("Cy", "Okay.")
_TWO_ROWS = blk("Bo", "so we need to", "check the cluster")
_ONE_ROW = blk("Bo", "So we need to check the cluster.")
_STUTTER = blk("Bo", "so or what like so so so like itself", "cluster logs build")
_STUTTER_ONE_ROW = blk("Bo", "So or what itself cluster logs build.")


@pytest.mark.parametrize("shown_for", [4, 5])
@pytest.mark.parametrize(
    ("draft", "merged"), [(_TWO_ROWS, _ONE_ROW), (_STUTTER, _STUTTER_ONE_ROW)], ids=["plain", "stutter"]
)
def test_an_entry_re_laid_onto_its_bottom_row_is_stored_once(
    draft: CaptionBlock, merged: CaptionBlock, shown_for: int
) -> None:
    """XERK-2746: the bottom row took the merged text (the live line, or a stored one
    re-read as its rewrite) and nothing claimed the entry's rows below it: the second row was
    stored beside it, or the whole box again (Cy's "Okay." twice) when a stutter Teams
    dropped failed the rewrite's word coverage."""
    stored, _ = _entry_run([([_CY, draft], shown_for), ([_CY, merged], 6)])
    assert stored == [("Cy", "Okay."), ("Bo", merged.lines[0])]


def test_a_row_added_above_an_entrys_first_row_is_stored_before_it_once() -> None:
    """XERK-2746: Teams adds words at the start of Bo's entry, on a row above the one shown.
    A claim had to start at a run's first row, so the box was stored again. At 1 fps a line
    and a repeat of the next one can't both be said between frames: the row is new, stored
    before the entry's line."""
    stored, _ = _entry_run([([blk("Bo", "check the cluster"), _CY], 4), ([_TWO_ROWS, _CY], 5)])
    assert stored == [("Bo", "so we need to"), ("Bo", "check the cluster"), ("Cy", "Okay.")]


def test_an_entry_grown_at_its_start_then_re_laid_above_another_is_stored_once() -> None:
    """XERK-2746's third shape: with the row added above stored before the entry's line, the
    re-lay onto one row is XERK-2734's merge: one line for Bo's entry, Cy once."""
    stored, _ = _entry_run(
        [([blk("Bo", "check the cluster"), _CY], 4), ([_TWO_ROWS, _CY], 4), ([_ONE_ROW, _CY], 6)]
    )
    assert stored == [("Bo", "So we need to check the cluster."), ("Cy", "Okay.")]


def test_a_row_above_a_repeat_after_dropped_frames_is_a_line_said_between_them() -> None:
    """XERK-2746's gate: only a draft seen on the last frames may be claimed below a new row.
    After dropped frames [okay, sounds good] over a known "sounds good" is two new lines."""
    stored, _ = _entry_run(
        [([blk("Bo", "sounds good")], 4), (None, 4), ([blk("Bo", "okay", "sounds good")], 5)]
    )
    assert stored == [("Bo", "sounds good"), ("Bo", "okay"), ("Bo", "sounds good")]


def test_a_bottom_row_growing_other_words_keeps_the_row_ocr_missed_below_it() -> None:
    """XERK-2746's gate: the rows below fold into the bottom row only when it holds their
    words; a row OCR stopped reading while the line above grew other words is kept."""
    stored, _ = _entry_run(
        [([_CY, _TWO_ROWS], 4), ([_CY, blk("Bo", "so we need to restart it now")], 6)]
    )
    assert ("Bo", "check the cluster") in stored


@pytest.mark.parametrize(
    ("draft", "merged", "kept"),
    [
        # Teams's re-lay (or OCR) lacks two of the row's words.
        (["so we need to", "check the prod cluster quickly"], "So we need to check the cluster.",
         "check the prod cluster quickly"),
        # A re-flowed row's new bottom row OCR-missed: "check" lives only in the row.
        (["so we", "need to check"], "so we need to", "need to check"),
    ],
)
def test_a_row_below_with_a_word_the_bottom_row_lacks_is_never_folded(
    draft: list[str], merged: str, kept: str
) -> None:
    """XERK-2746 QA: folding a row the bottom row holds most of lost the rest for good. A row
    folds only when every one of its words is in the bottom row."""
    stored, _ = _entry_run([([_CY, blk("Bo", *draft)], 4), ([_CY, blk("Bo", merged)], 6)])
    assert ("Bo", kept) in stored


def test_a_row_the_bottom_line_did_not_take_is_never_folded() -> None:
    """XERK-2746's gate: a one-letter word is no growth of a stored line the speaker said on
    from (``_moved_up``), so the line keeps its text; folding the row below would lose "I"."""
    stored, _ = _entry_run([([_CY, blk("Bo", "we ship", "I")], 6), ([_CY, blk("Bo", "we ship I")], 6)])
    assert ("Bo", "I") in stored


def test_another_speakers_entry_is_never_folded_into_the_bottom_row() -> None:
    """XERK-2746: Cy's line below Bo's is never folded into Bo's row. (The speaker check in
    ``_merged_below`` is defensive: Cy's header finalizes Bo's line, so it can't grow.)"""
    stored, _ = _entry_run(
        [([blk("Bo", "so we need to"), blk("Cy", "check the cluster")], 2),
         ([blk("Bo", "so we need to check the cluster")], 6)]
    )
    assert ("Cy", "check the cluster") in stored


def test_one_frame_reading_two_rows_as_one_blanks_neither() -> None:
    """XERK-2746: a fold is taken a frame late, once the next frame doesn't show the row
    either. One frame of OCR reading both rows as one box blanks nothing."""
    stored, _ = _entry_run([([_CY, _TWO_ROWS], 4), ([_CY, _ONE_ROW], 1), ([_CY, _TWO_ROWS], 5)])
    assert ("Bo", "check the cluster") in stored


@pytest.mark.parametrize(("seed", "noise"), [(32, 0.0), (191, 0.2)])
def test_a_fold_never_takes_a_reflow_or_an_older_line(seed: int, noise: float) -> None:
    """XERK-2746: ``_merged_below`` lost a word on these seeds (main lost none). Seed 32: the
    line lost a word moved up onto the row above (a re-flow, ``_reflow``'s). Seed 191: the
    bottom row paired with an older line it starts like, not the row above the folded one."""
    assert _simulate_teams(seed, noise=noise)[2] == 0


def test_an_entry_growing_as_the_next_one_appears_below_is_stored_once() -> None:
    """XERK-2791, a real Teams shape: one speaker's one-row entries, each a block's top row.
    "All right" grew on the frame the next entry appeared under it; unpaired under its header,
    that frame stored "OK.", the grown entry and "Next up is" again. Off-roster speakers had
    no name before, so their rows were cheap glitches and the growth was lost instead."""
    ana = ["we can ship the release on friday once", "the last two checks are green for the"]
    stored, _ = _entry_run(
        [
            ([blk("Ana", *ana[:1], ana[1] + " release."), blk("Bo", "OK")], 1),
            (
                [
                    blk(None, ana[1] + " release.", header=False),
                    blk("Ana", "That's pretty much."),
                    blk("Bo", "OK."),
                    blk("Bo", "All right"),
                ],
                1,
            ),
            ([blk("Bo", t) for t in ("OK.", "All right, let's wrap it up today.", "Next up is")], 1),
            ([blk("Bo", t) for t in ("OK.", "All right, let's wrap it up today.", "Next up is Cy")], 6),
        ]
    )
    assert [text for _, text in stored][2:] == [
        "OK",
        "That's pretty much.",
        "OK.",
        "All right, let's wrap it up today.",
        "Next up is Cy",
    ]


def test_a_new_entry_under_a_shown_twin_of_its_start_is_a_new_line() -> None:
    """The row above re-reading the line before is what places a grown top row: with the old
    entry still shown above, a row starting with its words is a new entry (XERK-2716)."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 5),
            (_entries("OK.", "yes.", "yes I think so too."), 6),
        ]
    )
    assert [text for _, text in stored] == ["OK.", "yes.", "yes I think so too."]


@pytest.mark.parametrize("missed", [1, 2, 3, 5])
@pytest.mark.parametrize(
    ("first", "next_entry"),
    [("yes.", "yes I think so too."), ("yes", "yes I think so too."), ("So", "So let me share.")],
)
def test_a_new_entry_read_while_ocr_missed_the_row_above_is_stored_once(
    missed: int, first: str, next_entry: str
) -> None:
    """XERK-2796: OCR missed the "yes." row for a few frames, so the live "yes." line grew into
    Ana's next entry "yes I think so too.". Once both rows showed, "yes." read as a cut-short
    re-read of it (or as nothing) and the next entry was stored again: OK., yes I think so
    too., yes., yes I think so too. The line goes back to the row's text instead."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", first), 4),
            (_entries("OK.", next_entry), missed),
            (_entries("OK.", first, next_entry), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", first), ("Ana", next_entry)]


@pytest.mark.parametrize("grown", [False, True])
@pytest.mark.parametrize("clipped", [1, 3])
@pytest.mark.parametrize(
    ("said", "cut"), [("Thank you so much.", "Thank you"), ("I agree.", "I")]
)
def test_a_repeated_entry_ocr_clipped_on_a_frame_keeps_both_copies(
    said: str, cut: str, clipped: int, grown: bool
) -> None:
    """QA (XERK-2796): Ana says the same entry twice and OCR clips the upper copy's end on the
    frames the repeat appears. That reads like the miss above, and reverting the line cut it
    for good ("Thank you"). Only a text the line held for ``_HELD_MS`` is reverted to; a live
    entry passes through each word-by-word prefix for one frame."""
    words = said.split()
    build = [(_entries("OK.", " ".join(words[:k])), 1) for k in range(1, len(words))] if grown else []
    stored, _ = _entry_run(
        [
            *build,
            (_entries("OK.", said), 4),
            (_entries("OK.", cut, said), clipped),
            (_entries("OK.", said, said), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", said), ("Ana", said)]


def test_another_speakers_entry_read_while_ocr_missed_the_row_above_is_stored_once() -> None:
    """XERK-2796: the entry Ana's live line grew into while her row was OCR-missed is Bo's
    (his header was missed too). Once both show, her line goes back to "yes." and his row is
    his own line."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", "yes I think so too."), 3),
            ([*_entries("OK.", "yes."), blk("Bo", "yes I think so too.")], 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Bo", "yes I think so too.")]


@pytest.mark.parametrize("first_for", [4, 8, 28])
def test_a_finished_entry_grown_while_ocr_missed_the_row_above_is_stored_once(
    first_for: int,
) -> None:
    """XERK-2796: shown 5+ frames before the miss, "yes." is stored and grows through
    ``_apply_grows`` (``_revise``), which must note its held text too; shown 4, it grows as
    the live line (``_update``)."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), first_for),
            (_entries("OK.", "yes I think so too."), 3),
            (_entries("OK.", "yes.", "yes I think so too."), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Ana", "yes I think so too.")]


def test_a_paused_entry_repeated_long_after_keeps_both_copies() -> None:
    """QA (XERK-2796): Ana paused on "Thank you", finished the entry, and said it again over
    half a minute later with the upper copy clipped to "Thank you" for a frame. Only a line
    first seen within ``_REVISE_MS`` is reverted: this one is a repeat, kept whole."""
    said = "Thank you so much."
    stored, _ = _entry_run(
        [
            (_entries("OK.", "Thank you"), 3),
            (_entries("OK.", said), 35),
            (_entries("OK.", "Thank you", said), 1),
            (_entries("OK.", said, said), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", said), ("Ana", said)]


@pytest.mark.parametrize(
    ("header", "speaker"),
    [("Bo Chen", "Bo Chen"), ("jsmith", "jsmith"), ("Raman, Priya (Guest)", "Raman, Priya")],
)
def test_an_off_roster_zoom_speaker_beside_an_avatar_is_named(header: str, speaker: str) -> None:
    """XERK-2802: a dim Zoom name row with the avatar circle beside it names its speaker though
    the roster lacks them (a share shows only a strip of tiles); its lines had no speaker."""
    for roster in (Roster(), _roster("Ana Lopez")):
        ocr, jpeg = panel_crop(
            ("Ana Lopez", True), ("and then", False), (header, True), ("we shipped it", False)
        )
        blocks = parse_caption_crop(ocr, roster, jpeg=jpeg)
        assert [(b.speaker, b.lines) for b in blocks][-1] == (speaker, ["we shipped it"])


_SAID = "Sure, I can take that one"


def _initials_crop(
    name: str, text: str, shape: str, *, light: bool = False
) -> tuple[OcrResult, bytes]:
    """``panel_crop`` of ``name`` over ``text`` with Zoom's default avatar as real crops draw
    it: a 26 px circle astride the name and its text row (centred 4 px below the name box,
    ``_with_disc``), the initials white on it, and OCR reading them in the avatar column as
    ``shape`` says: "text-row" their own box joined into the text row (the real 2026-11-11
    meeting), "merged" one box over the circle and the name (real tenir-ocr pads it past the
    circle, QA), "own-row" a row of their own between the two."""
    ocr, jpeg = panel_crop((name, True, False), (text, False), light=light)
    named, said = ocr.lines
    # Joined into the text row, the circle sits lower: OCR boxes the letters on it, and
    # that box overlaps the text row (the real meeting's "HB Yeah, ...").
    centre = said.box[1] + 4 if shape == "text-row" else named.box[3] + 4
    d = 26
    img = Image.open(io.BytesIO(_with_disc((ocr, jpeg), centre, d))).convert("RGB")
    initials = "".join(w[0] for w in name.split()).upper()
    font = ImageFont.load_default(size=13)
    ImageDraw.Draw(img).text((19, centre - 7), initials, font=font, fill="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    if shape == "merged":
        # Real tenir-ocr pads the text row as much (QA: 36 px each), so no row height read
        # off the other rows trims the merged box back to the name.
        x0, y0, x1, y1 = named.box
        box = (14.0, min(y0, centre - d / 2 - 4), x1, max(y1, centre + d / 2 + 4))
        tall = box[3] - box[1]
        padded = (said.box[0], said.box[3] - tall + 4, said.box[2], said.box[3] + 4)
        lines = (OcrLine(f"{initials} {name}", box), OcrLine(said.text, padded))
    elif shape == "own-row":
        y = named.box[3] - 2  # under the name, above the text row: rows of their own
        lines = (named, OcrLine(initials, (17.0, y, 34.0, y + 15)), said)
    else:
        lines = (named, OcrLine(initials, (17.0, centre - 7, 34.0, centre + 7)), said)
    return OcrResult(ocr.width, ocr.height, lines), buf.getvalue()


@pytest.mark.parametrize("shape", ["text-row", "merged", "own-row"])
def test_zooms_initials_avatar_read_into_a_row_is_neither_name_nor_speech(shape: str) -> None:
    """Zoom's default avatar is the speaker's initials in a coloured circle, astride the name
    line and its first text row: OCR read "HB" into the text row and a real meeting stored it
    as speech (XERK-2803). One box over the initials and the name kept their bright ink and
    the name no longer read dim: off the roster the crop was dropped (QA, real OCR).

    Off the roster the speaker is named only when ``_has_avatar`` sees the circle, and a
    name row that lost initials vouches for no start on its own, as on main: by the initials
    alone a docs glossary's terms were named or started meetings (QA; XERK-2820 tunes that
    on real crops). The initials are gone from the name and the text either way."""
    for roster in (Roster(), _roster("Hana Bauer")):
        ocr, jpeg = _initials_crop("Hana Bauer", _SAID, shape)
        (block,) = parse_caption_crop(ocr, roster, live=True, jpeg=jpeg)
        assert block.lines == [_SAID]
        assert block.speaker in ((None, "Hana Bauer") if not roster else ("Hana Bauer",))
        if roster or shape != "merged":
            assert parse_caption_crop(ocr, roster, jpeg=jpeg) == [block]


def test_a_merged_initials_name_below_another_entry_is_a_header_not_speech() -> None:
    """Below the top only a dim row is an off-roster header: a box over the bright initials
    and the name read as text, and "Hana Bauer" was stored as Ana's speech."""
    ocr, jpeg = _initials_crop("Hana Bauer", _SAID, "merged")
    top, top_jpeg = panel_crop(("Ana Lopez", True), ("and then we", False))
    above, below = Image.open(io.BytesIO(top_jpeg)), Image.open(io.BytesIO(jpeg))
    img = Image.new("RGB", (above.width, above.height + below.height))
    img.paste(above, (0, 0))
    img.paste(below, (0, above.height))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    dy = above.height
    moved = [OcrLine(ln.text, (ln.box[0], ln.box[1] + dy, ln.box[2], ln.box[3] + dy)) for ln in ocr.lines]
    crop = OcrResult(img.width, img.height, (*top.lines, *moved))
    blocks = parse_caption_crop(crop, _roster("Ana Lopez"), live=True, jpeg=buf.getvalue())
    assert [b.lines for b in blocks] == [["and then we"], [_SAID]]


def test_a_light_theme_merged_initials_name_still_starts_a_meeting() -> None:
    """QA: on a light panel the white initials are panel-coloured, so main read the merged
    "HB Hana Bauer" row dim and started the meeting; refusing every trimmed row's Title
    Case vouch stopped it. A trimmed row vouches when it read dim with its initials."""
    ocr, jpeg = _initials_crop("Hana Bauer", _SAID, "merged", light=True)
    blocks = parse_caption_crop(ocr, Roster(), jpeg=jpeg)
    assert [b.lines for b in blocks] == [[_SAID]]


@pytest.mark.parametrize(
    ("name", "initials"), [("Mary Ann Lee", "ML"), ("Mary Ann Lee", "MAL"), ("jsmith", "J")]
)
def test_any_initials_zoom_may_draw_are_dropped_from_the_avatar_column(
    name: str, initials: str
) -> None:
    ocr, jpeg = _initials_crop(name, _SAID, "text-row")
    named, own, said = ocr.lines
    ocr = OcrResult(ocr.width, ocr.height, (named, replace(own, text=initials), said))
    (block,) = parse_caption_crop(ocr, _roster(name), jpeg=jpeg)
    assert block.lines == [_SAID]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Oliver King", "OK so we ship it"),
        ("Oliver King", "OK"),
        ("Priya Raman", "PR is merged now"),
        ("Bo Chen", "BC is the one we picked"),
        ("Ian", "I think so"),
    ],
)
def test_a_speaker_saying_their_own_initials_keeps_them(name: str, text: str) -> None:
    """QA: matched by text alone, "OK" from Oliver King was deleted, and an entry of just "OK"
    was lost. Spoken words start at the text column; only the avatar's sit left of it, so
    real OCR boxing a spoken "PR" a few px left of the column keeps it too (QA)."""
    ocr, jpeg = panel_crop((name, True), (text, False))
    named, said = ocr.lines
    early = OcrLine(said.text, (said.box[0] - 4, *said.box[1:]))
    for crop in (ocr, OcrResult(ocr.width, ocr.height, (named, early))):
        for pixels in (jpeg, None):
            (block,) = parse_caption_crop(crop, _roster(name), jpeg=pixels)
            assert (block.speaker, block.lines) == (name, [text])


@pytest.mark.parametrize("light", [False, True])
@pytest.mark.parametrize("term", ["Pull Request", "pull request"])
def test_a_glossary_acronym_left_of_its_term_names_no_speaker(term: str, light: bool) -> None:
    """QA: a docs glossary puts "PR" left of a grey "Pull Request" on bare page, the shape of
    initials beside a name. Taken as the avatar it named the term; dropped, the grey term
    vouched for a start. Main starts nothing here (the acronym's ink keeps the row bright)."""
    ocr, jpeg = panel_crop(
        (term, True, False), ("a proposed change to the code", False), light=light
    )
    named, said = ocr.lines
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    ink = (20, 20, 20) if light else (240, 240, 240)
    ImageDraw.Draw(img).text((12, named.box[1]), "PR", font=ImageFont.load_default(size=20), fill=ink)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    acronym = OcrLine("PR", (12.0, named.box[1], 40.0, named.box[3]))
    ocr = OcrResult(ocr.width, ocr.height, (acronym, named, said))
    # Dropped as initials, the grey Title Case term started a meeting main didn't (QA).
    assert parse_caption_crop(ocr, Roster(), jpeg=buf.getvalue()) == []


def test_the_text_column_is_where_most_rows_start() -> None:
    """A row OCR boxes from a later word (starting right of the column) never moves the
    column: taken as the rows' rightmost start, "OK" at the column read as the avatar."""
    ocr, jpeg = panel_crop(("Oliver King", True), ("OK so we ship it", False), ("it", False))
    late = OcrLine("and then the rest", (300.0, *ocr.lines[2].box[1:2], 520.0, ocr.lines[2].box[3]))
    ocr = OcrResult(ocr.width, ocr.height, (*ocr.lines[:2], late))
    (block,) = parse_caption_crop(ocr, _roster("Oliver King"), jpeg=None)
    assert block.lines == ["OK so we ship it", "and then the rest"]


def test_a_row_left_with_one_char_once_the_initials_go_is_noise() -> None:
    ocr, jpeg = _initials_crop("Hana Bauer", "a", "text-row")
    ocr = OcrResult(ocr.width, ocr.height, (*ocr.lines, OcrLine(_SAID, (50.0, 80.0, 400.0, 100.0))))
    blocks = parse_caption_crop(ocr, _roster("Hana Bauer"), jpeg=None)
    assert [b.lines for b in blocks] == [[_SAID]]


def test_an_off_roster_zoom_name_with_no_avatar_stays_unnamed() -> None:
    """A dim row alone is no name (a docs page's grey "Last Updated" reads the same), and
    without pixels there is no avatar to see: the header keeps no speaker, as before."""
    ocr, jpeg = panel_crop(("Bo Chen", True, False), ("we shipped it", False))
    for pixels in (jpeg, None):
        (block,) = parse_caption_crop(ocr, Roster(), jpeg=pixels)
        assert (block.speaker, block.has_header, block.lines) == (None, True, ["we shipped it"])


def test_an_off_roster_zoom_speaker_s_lines_are_stored_under_their_name() -> None:
    """End to end: the meeting stores the off-roster speaker's words under their name, and a
    name misread on one frame is unified to the spelling read most (``_as_heard``)."""
    store = InMemoryConversationStore()
    text = ("we shipped the release on friday", False)
    frames = [panel_crop(("Shad Tischer", True), text)] * 5
    frames[2] = panel_crop(("Shad Tischcr", True), text)
    _feed(_tracker(store), frames, roster_names=("Ana Lopez",))
    (conv,) = store.list("default")
    segments = store.get("default", conv.id).segments
    assert [(s.speaker, s.text) for s in segments] == [
        ("Shad Tischer", "we shipped the release on friday")
    ]


@pytest.mark.parametrize(
    "misread", ["Tirn Mahler", "Tim Mah", "Tim", "Ti", "Mahler", "Timothy Mahler"]
)
def test_a_roster_speaker_s_name_misread_beside_an_avatar_names_no_new_speaker(
    misread: str,
) -> None:
    """XERK-2802 QA: a roster name misread on one frame past ``Roster.match`` ("Tirn Mahler"
    reads 0.857) was named by its text, and the merger stored the whole entry again under it.
    A header reading near a roster name stays unnamed, merging as before."""
    store = InMemoryConversationStore()
    text = ("we shipped the release on friday", False)
    frames = [panel_crop(("Tim Mahler", True), text)] * 6
    frames[2] = panel_crop((misread, True), text)
    _feed(_tracker(store), frames, roster_names=("Tim Mahler",))
    (conv,) = store.list("default")
    segments = store.get("default", conv.id).segments
    assert [(s.speaker, s.text) for s in segments] == [
        ("Tim Mahler", "we shipped the release on friday")
    ]
    assert Roster().near(misread) is False


def test_a_header_reading_near_a_roster_name_is_never_named_by_its_avatar() -> None:
    """XERK-2802 QA: shared with Teams (``avatars_decide``): "Priya R" beside roster "Priya
    Raman" was named a new speaker. A short roster word only blocks itself, not every name
    it starts ("Jo" / "John Smith")."""
    roster = _roster("Priya Raman", "Jo")
    rows = ["Priya R", "we shipped it", "John Smith", "and then", "Hank Ives", "ok then"]
    for decide in (False, True):
        blocks = caption_blocks(
            rows, roster, avatars={0, 2, 4}, avatars_decide=decide, name_rows={0, 2, 4}
        )
        assert [b.speaker for b in blocks if b.has_header] == [None, "John Smith", "Hank Ives"]
    near = ("Jo", "Jo Ann", "Priya", "Pri", "Raman")
    assert [roster.near(n) for n in near] == [True] * len(near)
    far = ("Samantha Jones", "John", "Prim Shah", "Ram", "John Kim")
    assert [roster.near(n) for n in far] == [False] * len(far)
    (block,) = caption_blocks(
        ["John Kim", "we shipped it"], _roster("Kim"), avatars={0}, avatars_decide=True
    )
    assert block.speaker == "John Kim"


@pytest.mark.parametrize(
    "back",
    [
        # Teams re-punctuated the next entry the frame the row came back.
        [(_entries("OK.", "yes.", "yes I think so too."), 6)],
        # OCR clipped the next entry's row that frame.
        [
            (_entries("OK.", "yes.", "yes I think so"), 1),
            (_entries("OK.", "yes.", "yes I think so too."), 6),
        ],
    ],
    ids=["re-punctuated", "clipped"],
)
def test_the_row_below_a_missed_row_need_not_read_the_grown_line_exactly(back: list) -> None:
    """XERK-2804: the row below the missed one read the grown line re-punctuated or clipped,
    so ``_split_grown`` never paired it and the next entry was stored twice."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", "yes I think so too"), 3),
            *back,
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Ana", "yes I think so too.")]


@pytest.mark.parametrize(
    "live",
    ["yes we ship at 3", "yes मुझे लगता है", "yes I think so too "],
    ids=["digit", "vowel-sign", "trailing-space"],
)
def test_a_live_line_ending_on_any_letter_digit_or_mark_is_unfinished(live: str) -> None:
    """XERK-2804 (QA): an unfinished line is one ending on a letter, digit or combining mark
    (Hindi ends most words on a vowel sign, which ``isalnum`` rejects), trailing space aside;
    its re-punctuated row below splits it."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", live), 3),
            (_entries("OK.", "yes.", live.rstrip() + "."), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Ana", live.rstrip() + ".")]


def test_an_older_one_word_line_does_not_stop_the_split() -> None:
    """QA (XERK-2804): with no row aligned above, the line to split was any known line the row
    below starts with; Cy's earlier "Yes." matched too and the next entry was stored twice."""
    stored, _ = _entry_run(
        [
            ([blk("Cy", "Yes.")], 3),
            ([blk("Cy", "Yes."), blk("Ana", "OK.")], 3),
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", "yes I think so too"), 3),
            (_entries("yes.", "yes I think so too."), 6),
        ]
    )
    assert stored == [
        ("Cy", "Yes."),
        ("Ana", "OK."),
        ("Ana", "yes."),
        ("Ana", "yes I think so too."),
    ]


def test_a_two_row_entry_read_while_ocr_missed_the_row_above_is_stored_once() -> None:
    """XERK-2804: the live "yes." grew into a two-row entry while OCR missed its row. Once
    both show, the entry's second row pairs with the line it re-reads, not stored again."""
    nxt = ["yes I think so too, and we", "should ship it."]
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            ([blk("Ana", "OK."), blk("Ana", *nxt)], 3),
            ([blk("Ana", "OK."), blk("Ana", "yes."), blk("Ana", *nxt)], 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Ana", nxt[0]), ("Ana", nxt[1])]


@pytest.mark.parametrize(
    "back",
    [
        ["yes I think so too, and we", "should ship it today."],
        ["yes I think so too, and", "we should ship it."],
        ["yes I think so too, and", "we should ship it today."],
    ],
    ids=["grown", "re-wrapped", "both"],
)
def test_a_two_row_entry_changing_as_the_missed_row_returns_is_stored_once(
    back: list[str],
) -> None:
    """XERK-2807: the second row of the entry "yes." grew into grew (Ana talking on) or
    took a word re-wrapped down from the row above as the missed row came back. Only an exact
    read paired it, so the entry was stored twice."""
    nxt = ["yes I think so too, and we", "should ship it"]
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            ([blk("Ana", "OK."), blk("Ana", *nxt)], 3),
            ([blk("Ana", "OK."), blk("Ana", "yes."), blk("Ana", *back)], 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), *(("Ana", row) for row in back)]


def test_a_row_below_holding_words_the_row_above_never_lost_is_a_new_line() -> None:
    """XERK-2807: a later row pairs with the next entry's line only when its extra words in
    front are the ones the row above lost; other words in front make it another line."""
    nxt = ["yes I think so too, and we", "should ship it"]
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            ([blk("Ana", "OK."), blk("Ana", *nxt)], 3),
            ([blk("Ana", "OK."), blk("Ana", "yes."), blk("Ana", nxt[0], "so should ship it.")], 6),
        ]
    )
    assert ("Ana", "so should ship it.") in stored


@pytest.mark.parametrize(
    "live", ["yes I think so too,", "yes I think so too -", "yes the plan is:", "yes we can; then"]
)
def test_a_live_line_pausing_on_a_comma_dash_or_colon_is_unfinished(live: str) -> None:
    """XERK-2807: a live line ending on a mark that leads on read as finished, so its
    re-punctuated return was never split and the next entry was stored twice."""
    from api.screen.merge import _unfinished

    assert _unfinished(live)
    assert _unfinished("はい、") and not _unfinished("❤️") and not _unfinished("yes.")
    back = live.rstrip(",-: ") + "."
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", live), 3),
            (_entries("OK.", "yes.", back), 6),
        ]
    )
    assert stored == [("Ana", "OK."), ("Ana", "yes."), ("Ana", back)]


def _gap_run(frames: list[tuple[list[CaptionBlock], int]], gap: int) -> list[str]:
    """``_entry_run`` at ``gap`` ms between frames; returns every segment's last text."""
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    final: dict[str, object] = {}
    t = 0
    for blocks, count in frames:
        for _ in range(count):
            for seg in m.feed(blocks, t):
                final[seg.segment_id] = seg
            t += gap
    for seg in m.flush(t + 60_000):
        final[seg.segment_id] = seg
    return [s.text for s in sorted(final.values(), key=lambda s: s.start_ms) if s.text]


def test_a_reflowed_stored_line_s_text_is_held_only_from_the_reflow() -> None:
    """XERK-2804: ``_reflow`` changes a stored line's text but not its ``changed_ms`` (the
    growth window), so a rewrite within ``_HELD_MS`` noted the re-flowed text as held
    (evidence ``_split_grown`` cuts a repeat back to)."""
    from api.screen.merge import _hold

    m = CaptionMerger(start_ms=0, stable_ms=3000)
    draft = ["so after the review I can", "do the second task the."]
    final = ["so after the review I", "can do the second task, the API contract"]
    for t in range(6):
        m.feed([blk("Ana", *draft)], t * 1000)
    m.feed([blk("Ana", *final)], 6000)
    m.feed([blk("Ana", *final)], 7000)  # two reads agree: the re-flow lands
    line = next(ln for ln in m._lines if ln.text == final[0])
    assert line.final and line.changed_ms < 7000
    _hold(line, 8000)  # a rewrite one frame later
    assert final[0] not in line.held
    _hold(line, 9000)  # shown 2 s
    assert line.held[-1] == final[0]


def test_a_one_frame_glyph_long_after_a_late_reflow_is_never_stored() -> None:
    """QA (XERK-2804): a re-flow moving a stored line's ``changed_ms`` reopened its growth
    window (``_EXTEND_MS``), and a stray glyph read once 30 s+ after it was said was stored."""
    d = ["so after the review I can", "do the second task the."]
    f = ["so after the review I", "can do the second task, the API contract"]
    rows = _gap_run(
        [
            ([blk("Ana", *d)], 1),
            ([blk("Ana", *d), blk("Bo", "Right.")], 2),
            ([blk("Ana", *f), blk("Bo", "Right.")], 13),
            ([blk("Ana", f[0], f[1] + " l"), blk("Bo", "Right.")], 1),
            ([blk("Ana", *f), blk("Bo", "Right.")], 6),
        ],
        2000,
    )
    assert rows == [*f, "Right."]


@pytest.mark.parametrize(
    ("said", "clip", "nxt"),
    [
        # The next entry extends her finished one: never the line grown while missed.
        ("Thank you all.", "Thank you", "Thank you all, and see you next week."),
        # The row below holds no more words than the clipped row.
        ("Thank you all so much.", "Thank you all", "Thank you all."),
    ],
    ids=["extends", "not-longer"],
)
def test_a_finished_entry_clipped_as_her_next_entry_shows_is_kept_whole(
    said: str, clip: str, nxt: str
) -> None:
    """QA (XERK-2804): a finished entry, its row clipped on the one frame her next entry first
    shows, then scrolled up: reverted to the clipped text, it was cut for good. At 2 s frame
    gaps every word-by-word prefix of it is held."""
    words = said.split()
    rows = _gap_run(
        [
            *[(_entries("OK.", " ".join(words[:k])), 1) for k in range(1, len(words) + 1)],
            (_entries("OK.", said), 3),
            (_entries("OK.", clip, nxt), 1),
            ([blk("Ana", nxt), blk("Bo", "Great.")], 6),
        ],
        2000,
    )
    assert said in rows and clip not in rows


@pytest.mark.parametrize(
    "end", [".", "…", '."', "。", "？", "؟", " 🙂", " ❤️", " 1️⃣", " \u2139\ufe0f", " 1\u20e3"]
)
def test_a_finished_entry_clipped_over_her_next_entrys_first_frame_is_kept_whole(
    end: str,
) -> None:
    """QA fuzz (XERK-2804, tf seed 146, its frames): Ana's entry, read whole once, is clipped
    to "Sounds" on the frame her next entry first shows, reading its words unpunctuated. On
    that frame it reads like the ticket's re-punctuated return; split, the entry was stored
    as "Sounds" for good. Only an unfinished line (ending on a letter or digit) is read
    inexactly: a closing mark outside ".?!" ("。", "…") was cut too (QA)."""
    said = "Sounds good ok go cluster api pods" + end
    cut = said.lower()[: -len(end)]  # her next entry's first frame: the words, unpunctuated
    nxt = ("Sounds good ok go cluster api pods sounds", "good.")
    frames = [
        (2240, _entries("Sounds good.", "Sounds good ok.", "sounds")),
        (4335, _entries("Sounds good.", "Sounds good ok.", said)),
        (6396, _entries("Sounds good.", "Sounds good ok.", "Sounds", cut)),
        (8454, [blk(None, "Sounds good ok."), blk("Ana", said), blk("Ana", *nxt), blk("Cy", "right")]),
        (10430, [blk(None, said), blk("Ana", *nxt), blk("Cy", "Right."), blk("Bo", "thanks")]),
        (12418, [blk("Cy", "Right."), blk("Bo", "Thanks.")]),
    ]
    m = CaptionMerger(start_ms=0, stable_ms=3000)
    final: dict[str, str] = {}
    for t, blocks in frames:
        for seg in m.feed(blocks, t):
            final[seg.segment_id] = seg.text
    for seg in m.flush(72_418):
        final[seg.segment_id] = seg.text
    assert said in final.values() and "Sounds" not in final.values()


def test_another_speakers_row_over_the_grown_entry_never_splits_it() -> None:
    """XERK-2804: Ana's live "yes." grew into her next entry while OCR missed its row; Bo's
    "yes." shown above it is his own line, never her held text (``_splits`` speaker check)."""
    stored, _ = _entry_run(
        [
            (_entries("OK.", "yes."), 4),
            (_entries("OK.", "yes I think so too"), 3),
            ([blk("Ana", "OK."), blk("Bo", "yes."), blk("Ana", "yes I think so too.")], 6),
        ]
    )
    assert ("Bo", "yes.") in stored and ("Ana", "yes I think so too") in stored


def test_the_captions_off_warning_logs_whether_crops_came_and_were_refused(caplog) -> None:
    # XERK-2820: a Zoom meeting with captions on raised "no captions" and stored nothing.
    # The log tells a client sending no crops from OCR failing or the parser refusing them.
    from api.screen import meetings as mt

    async def run() -> None:
        async def notify(msg: object) -> None:
            pass

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        await t.on_full(meeting, 0)
        t.crop_received(ocr_failed=True)  # never reaches on_caption
        for ts in (1_000, 2_000):
            t.crop_received()
            await t.on_caption([], ts)  # a crop the parser refused
        await t.on_full(meeting, mt.CAPTIONS_OFF_MS)
        await t.on_full(meeting, mt.CAPTIONS_OFF_MS + 1)

    with caplog.at_level(logging.WARNING, logger="api.screen"):
        asyncio.run(run())
    (line,) = [r.getMessage() for r in caplog.records if "captions off" in r.getMessage()]
    assert line == (
        "screen captions off: 'tesoro' (zoom), 3 caption crops received, 1 OCR failed, "
        "2 with no speaker block"
    )


def test_the_captions_off_crop_counts_restart_once_the_warning_clears(caplog) -> None:
    # A meeting gone long enough clears the warning; the next one's line counts only its crops.
    from api.screen import meetings as mt

    async def run() -> None:
        async def notify(msg: object) -> None:
            pass

        t = _tracker(InMemoryConversationStore())
        t.notify = notify
        meeting = parse_full_frame(_toolbar_frame())
        idle = parse_full_frame(OcrResult(1280, 720, ()))
        await t.on_full(meeting, 0)
        t.crop_received(ocr_failed=True)
        await t.on_caption([], 1_000)
        await t.on_full(idle, mt.MEETING_SEEN_MS + 1)  # toolbar long gone: cleared
        start = mt.MEETING_SEEN_MS + 2
        await t.on_full(meeting, start)
        await t.on_full(meeting, start + 1)
        await t.on_full(meeting, start + mt.CAPTIONS_OFF_MS + 1)

    with caplog.at_level(logging.WARNING, logger="api.screen"):
        asyncio.run(run())
    (line,) = [r.getMessage() for r in caplog.records if "captions off" in r.getMessage()]
    assert line.endswith("0 caption crops received, 0 OCR failed, 0 with no speaker block")


def test_crops_failing_ocr_on_the_socket_are_counted_in_the_captions_off_line(
    screen, caplog, monkeypatch
) -> None:
    # XERK-2820 QA: a crop failing OCR never reaches the tracker's caption path; uncounted, the
    # line read "0 crops" and blamed the client for sending none.
    from api.main import app
    from api.screen import meetings as mt
    from api.screen import ws as wsmod

    monkeypatch.setattr(wsmod, "_MAX_CLOCK_SKEW_MS", 10**12)  # frames minutes apart, sent now
    _, ocr, token, _ = screen
    base = int(time.time() * 1000)

    def send(ws, i: int, ts: int, region: str) -> None:
        n = metrics.snapshot()["counters"].get("screen.frames_handled", 0)
        jpeg = unique_jpeg(40_000 + i)
        ocr.register(jpeg, _toolbar_frame())
        ws.send_bytes(envelope(header(ts, region), jpeg))
        wait_handled(n + 1)

    with caplog.at_level(logging.WARNING, logger="api.screen"):
        with TestClient(app) as client, client.websocket_connect(
            f"/ws/screen?token={token}"
        ) as ws:
            ws.receive_text()
            send(ws, 0, base, "full")
            ocr.fail = True
            send(ws, 1, base + 1_000, "caption")
            send(ws, 2, base + 2_000, "caption")
            ocr.fail = False
            send(ws, 3, base + 3_000, "caption")
            send(ws, 4, base + mt.CAPTIONS_OFF_MS + 10, "full")
            send(ws, 5, base + mt.CAPTIONS_OFF_MS + 20, "full")
    lines = [r.getMessage() for r in caplog.records if "captions off" in r.getMessage()]
    assert lines and "3 caption crops received, 2 OCR failed" in lines[0]


def test_a_meeting_is_on_screen_while_live_or_for_ten_minutes_after_its_toolbar() -> None:
    # XERK-2820: gates full-frame sampling. A live meeting counts with no toolbar ever seen.
    from api.screen import meetings as mt

    async def run() -> None:
        t = _tracker(InMemoryConversationStore())
        assert not t.meeting_on_screen(0)
        t.conversation_id = "11111111-1111-4111-8111-111111111111"
        assert t.meeting_on_screen(0)
        t.conversation_id = None
        await t.on_full(parse_full_frame(_toolbar_frame()), 1_000)
        assert t.meeting_on_screen(1_000 + mt.MEETING_SEEN_MS)
        assert not t.meeting_on_screen(1_000 + mt.MEETING_SEEN_MS + 1)

    asyncio.run(run())
