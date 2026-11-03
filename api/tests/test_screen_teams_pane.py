"""Locating Teams' live-captions pane on full frames and telling the client (XERK-2608).

Frames are synthetic renders on real-capture levels (stage ~31, pane 0, divider luma ~82,
a text-dense desktop around the Teams window); no real capture is committed (public repo).
OCR is a stub: the heading strip above a divider reads what the test says.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from api.screen import teams
from api.screen import ws as screen_ws
from api.screen.ocr import OcrError, OcrLine, OcrResult
from api.screen.teams_pane import CROP_HEADINGS, candidates, pane_of
from api.screen.zoom import Roster
from test_screen import StubOcr, envelope, screen, unique_jpeg, wait_handled  # noqa: F401
from test_screen_teams import teams_header

W, H = 2560, 1600
WINDOW = (300, 200, 1900, 1400)  # a Teams window floating in a remote desktop
PANE_X = (1500, 1898)  # its pane, docked right
DIVIDER_Y = 354
PANE_BOTTOM = 1398
HEADING_H = 22


def render(
    *,
    divider: str = "line",
    pane_bottom: int = PANE_BOTTOM,
    under_divider: bool = False,
    entries: int = 3,
    long_lines: bool = False,
    heading_on_grey: bool = False,
    avatar_x: int = 1511,
    separator: bool = False,
    decoys: int = 0,
) -> bytes:
    img = Image.new("RGB", (W, H), (37, 37, 38))
    d = ImageDraw.Draw(img)
    for y in range(0, H, 18):  # a text-dense desktop behind Teams (an issue tracker)
        for x in range(0, W, 9):
            d.line((x, y, x + 5, y), fill=(210, 210, 210))
    for i in range(decoys):  # dark windows wider than the pane, a grey line atop each
        y0 = 40 + 300 * i  # beside the Teams window, each wider than its pane
        d.rectangle((1920, y0 - 30, 2550, y0 + 150), fill=(0, 0, 0))
        d.line((1920, y0, 2549, y0), fill=(82, 82, 82))
    d.rectangle(WINDOW, fill=(31, 31, 31))
    d.rectangle((PANE_X[0], 280, PANE_X[1], pane_bottom), fill=(0, 0, 0))
    if heading_on_grey:
        d.rectangle((PANE_X[0], 280, PANE_X[1], DIVIDER_Y - 1), fill=(41, 41, 41))
    d.rectangle((1524, 308, 1650, 308 + HEADING_H), fill=(255, 255, 255))  # the heading
    x0, x1 = PANE_X[0], PANE_X[1] - 1
    if divider == "line":
        d.line((x0, DIVIDER_Y, x1, DIVIDER_Y), fill=(82, 82, 82))
    elif divider == "split":  # a scaled-down window: the line spread over two dim rows
        d.line((x0, DIVIDER_Y, x1, DIVIDER_Y), fill=(30, 30, 30))
        d.line((x0, DIVIDER_Y + 1, x1, DIVIDER_Y + 1), fill=(36, 36, 36))
    elif divider == "double":  # 2 px after JPEG
        d.rectangle((x0, DIVIDER_Y, x1, DIVIDER_Y + 1), fill=(82, 82, 82))
    elif divider == "dashed":  # not a divider: half of it is gaps
        for x in range(x0, x1, 8):
            d.line((x, DIVIDER_Y, x + 3, DIVIDER_Y), fill=(82, 82, 82))
    if under_divider:  # an entry scrolled up against the divider
        d.rectangle((1510, DIVIDER_Y + 1, 1700, DIVIDER_Y + 14), fill=(220, 220, 220))
    y = DIVIDER_Y + 12
    for _ in range(entries):  # avatar, name, text lines (strokes, not solid bars)
        d.ellipse((avatar_x, y, avatar_x + 40, y + 40), fill=(150, 150, 150))
        for x in range(1564, 1700, 5):
            d.line((x, y, x, y + 14), fill=(255, 255, 255))
        for i in range(6 if long_lines else 1):
            for x in range(1564, 1880 if long_lines else 1760, 5):
                d.line((x, y + 20 + 27 * i, x, y + 41 + 27 * i), fill=(255, 255, 255))
        y += 20 + 27 * (6 if long_lines else 1) + 8
    if separator:  # a full-width rule across the pane: both margins lit for 2 rows
        d.rectangle((PANE_X[0], 800, PANE_X[1], 801), fill=(90, 90, 90))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)  # the worst quality measured on real panes
    return buf.getvalue()


def heading(text: str = "Live Captions") -> OcrResult:
    return OcrResult(400, 80, (OcrLine(text, (24, 28, 150, 28 + HEADING_H)),))


def located(jpeg: bytes, band: OcrResult | None = None):
    """The pane as ws finds it: candidates, then each strip "OCR'd". By default a strip reads
    the heading only where the render drew it, so a decoy's strip reads nothing."""
    for c in candidates(jpeg, (W, H)):
        x0, y0, x1, y1 = c.band
        drawn = x0 <= 1524 and 1650 <= x1 and y0 <= 308 and 330 <= y1
        read = band if band is not None else heading() if drawn else OcrResult(400, 80, ())
        if (pane := pane_of(c, read, (W, H))) is not None:
            return pane
    return None


def px(pane) -> tuple[int, ...]:
    return tuple(round(v * s) for v, s in zip(pane.box, (W, H, W, H), strict=True))


def test_the_pane_is_found_by_its_divider_and_confirmed_by_its_heading() -> None:
    pane = located(render())
    assert pane is not None
    left, top, right, bottom = px(pane)
    assert abs(left - PANE_X[0]) <= 2 and abs(right - PANE_X[1]) <= 2
    assert DIVIDER_Y < top <= DIVIDER_Y + 3 and abs(bottom - PANE_BOTTOM) <= 4
    # The crop: CROP_HEADINGS heading heights, ~7 caption lines, under tenir-ocr's 16 boxes.
    assert pane.crop * H == pytest.approx(CROP_HEADINGS * HEADING_H)
    assert pane.crop * H <= 8 * 27  # never more than 8 caption pitches (QA: 30 passed)


@pytest.mark.parametrize(
    "kw",
    [
        {"under_divider": True},  # real capture: a scrolled pane's top entry under the line
        {"divider": "split"},  # a scaled-down window
        {"long_lines": True, "entries": 4},  # wrapped lines across the pane (QA: box shrank)
        {"avatar_x": PANE_X[0] + 2, "entries": 6},  # the left margin lit: the right one holds
        {"separator": True},  # a rule across the pane: a short gap in both margins
        {"decoys": 5},  # wider dark windows under grey lines don't push the pane out (QA)
    ],
    ids=[
        "entry-under-divider",
        "split-divider",
        "long-lines",
        "left-margin-lit",
        "separator",
        "wide-decoys",
    ],
)
def test_the_pane_and_its_full_height_are_found(kw) -> None:
    pane = located(render(**kw))
    assert pane is not None
    assert abs(px(pane)[3] - PANE_BOTTOM) <= 4


@pytest.mark.parametrize(
    ("jpeg", "band"),
    [
        (render(), heading("Live captions are on")),  # other words above the line
        (render(), OcrResult(400, 80, ())),  # nothing read above it
        (render(divider="none"), None),  # the heading on a slide: no divider
        (render(divider="dashed"), None),  # a dashed rule, not the divider
        (render(heading_on_grey=True), None),  # a grey toolbar's edge, not black above
        (render(pane_bottom=400), None),  # a strip, not a pane
        (b"\xff\xd8not a jpeg", None),
    ],
    ids=[
        "other-text",
        "no-text",
        "no-divider",
        "dashed",
        "not-black-above",
        "short",
        "bad-jpeg",
    ],
)
def test_no_pane_without_heading_divider_and_black_below(jpeg, band) -> None:
    assert located(jpeg, band) is None


def test_a_2px_divider_is_one_candidate() -> None:
    assert len(candidates(render(divider="double"), (W, H))) == 1


def test_a_frame_of_another_size_than_ocr_read_finds_no_pane() -> None:
    assert candidates(render(), (W // 2, H // 2)) == []


def test_a_frame_of_many_lines_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.screen import teams_pane

    calls = []
    real_bottom = teams_pane._bottom
    monkeypatch.setattr(teams_pane, "_bottom", lambda *a: calls.append(1) or real_bottom(*a))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(10, H - 200, 6):  # crafted: hundreds of divider-like lines on black
        d.line((0, y, W - 1, y), fill=(82, 82, 82))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    t = time.monotonic()
    assert len(candidates(buf.getvalue(), (W, H))) <= 4
    assert time.monotonic() - t < 5
    assert len(calls) <= 16  # the work stops at 4x the candidates kept


def test_caption_lines_in_the_pane_are_never_participants() -> None:
    pane = located(render())
    frame = teams.parse_full_frame(
        OcrResult(
            W,
            H,
            (
                OcrLine("Sounds Good", (1564, 420, 1700, 442)),  # a Title-Case reply, high up
                OcrLine("Priya Raman", (400, 900, 520, 918)),  # a tile label outside the pane
            ),
        ),
        pane=pane,
    )
    assert frame.caption_pane is pane
    assert "Sounds Good" not in frame.participants
    assert "Priya Raman" in frame.participants


def _crop(*rows: str) -> OcrResult:
    lines = tuple(
        OcrLine(t, (64, 30 * i, 64 + 9 * len(t), 30 * i + 20)) for i, t in enumerate(rows)
    )
    return OcrResult(600, 30 * len(rows), lines)


@pytest.mark.parametrize("live", [False, True])
def test_teams_speaker_labels_are_headers(live) -> None:
    # Real captures: Teams labels voices it hasn't matched "Speaker N". They are headers of
    # their own speaker, and they start a meeting (no blocks at all before, QA).
    crop = _crop("Speaker 1", "Proceed with that.", "Speaker 2", "So.", "Speaker 1", "Agreed?")
    roster = Roster()
    roster.add("Kunal Shah")
    blocks = teams.parse_caption_crop(crop, roster, live=live)
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Speaker 1", ["Proceed with that."]),
        ("Speaker 2", ["So."]),
        ("Speaker 1", ["Agreed?"]),
    ]
    assert "Speaker 1" not in roster  # the labels never join the participants


def test_a_speaker_label_heads_its_entry_even_where_pixels_show_no_avatar() -> None:
    # Avatars found in the pixels decide headers; a "Speaker N" row is one even where its
    # avatar wasn't found beside it.
    from test_screen_teams import panel

    ocr, jpeg = panel(
        [
            ("Priya Raman", "circle"),
            ("Let's look at the numbers for the north region", None),
            ("Speaker 1", None),
            ("Sure, I have them right here.", None),
        ],
        above=300,
    )
    roster = Roster()
    roster.add("Priya Raman")
    blocks = teams.parse_caption_crop(ocr, roster, jpeg=jpeg)
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["Let's look at the numbers for the north region"]),
        ("Speaker 1", ["Sure, I have them right here."]),
    ]


def test_a_speaker_label_inside_a_caption_line_is_text() -> None:
    blocks = teams.parse_caption_crop(
        _crop("Kunal Shah", "Speaker 1 said so."), Roster(), live=True
    )
    assert [b.lines for b in blocks] == [["Speaker 1 said so."]]


class BandOcr(StubOcr):
    """Full frames read what is registered; a heading strip reads ``self.band``."""

    def __init__(self) -> None:
        super().__init__()
        self.band: OcrResult | Exception = heading()

    async def read(self, jpeg: bytes, *, full: bool = False) -> OcrResult:
        if jpeg in self.by_jpeg:
            return await super().read(jpeg, full=full)
        if isinstance(self.band, Exception):
            raise self.band
        return self.band


@pytest.fixture
def band_screen(screen, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    store, _stub, token, admin = screen
    ocr = BandOcr()
    monkeypatch.setattr(screen_ws, "get_ocr_client", lambda: ocr)
    return store, ocr, token, admin


def test_the_client_is_told_where_the_pane_is_and_when_it_goes(band_screen) -> None:
    from api.main import app

    _store, ocr, token, _admin = band_screen
    with_pane, without = render(), render(divider="none")
    # The full frame's own OCR read nothing: Teams over a text-dense desktop is too dense
    # strip by strip (QA, real capture). The pane is still found from its pixels.
    ocr.register(with_pane, OcrResult(W, H, ()))
    ocr.register(without, OcrResult(W, H, ()))
    base = int(time.time() * 1000)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        ws.send_bytes(envelope(teams_header(base, "full"), with_pane))
        wait_handled(1)
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "screen.region" and msg["orgSiteKey"] == "yprime"
        assert msg["box"][0] == pytest.approx(PANE_X[0] / W, abs=0.001)
        assert msg["cropHeight"] == pytest.approx(CROP_HEADINGS * HEADING_H / H)
        # The same pane again: nothing re-sent. An OCR blip on the heading strip: the client
        # keeps its region (no "gone" then the pane again). Then the pane goes: told, once.
        ws.send_bytes(envelope(teams_header(base + 30_000, "full"), with_pane))
        wait_handled(2)
        ocr.band = OcrError("busy")
        ws.send_bytes(envelope(teams_header(base + 60_000, "full"), with_pane))
        wait_handled(3)
        ocr.band = heading()
        ws.send_bytes(envelope(teams_header(base + 90_000, "full"), with_pane))
        wait_handled(4)
        for i in (5, 6):
            ws.send_bytes(envelope(teams_header(base + 30_000 * i, "full"), without))
            wait_handled(i)
        ws.send_text('{"type":"ping","t":7}')
        assert json.loads(ws.receive_text()) == {"type": "screen.region", "orgSiteKey": "yprime"}
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 7}
    # A new socket is told again: the client dropped its region on reconnect.
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(teams_header(base + 200_000, "full"), with_pane))
        wait_handled(7)
        assert json.loads(ws.receive_text())["type"] == "screen.region"


def test_the_pane_is_told_even_when_the_frames_own_ocr_fails(band_screen) -> None:
    from api.main import app

    _store, ocr, token, _admin = band_screen
    jpeg = render()  # not registered: its full read fails, as a timeout under load would

    real_read = ocr.read

    async def read(data: bytes, *, full: bool = False) -> OcrResult:
        if full:
            raise OcrError("timed out")
        return await real_read(data, full=full)

    ocr.read = read  # type: ignore[method-assign]
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(teams_header(int(time.time() * 1000), "full"), jpeg))
        wait_handled(1)
        assert json.loads(ws.receive_text())["type"] == "screen.region"


def test_a_zoom_window_is_never_sent_a_pane(band_screen) -> None:
    from api.main import app
    from test_screen import header

    _store, ocr, token, _admin = band_screen
    jpeg = render()
    ocr.register(jpeg, OcrResult(W, H, ()))
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        ws.send_bytes(envelope(header(int(time.time() * 1000), "full"), jpeg))
        wait_handled(1)
        ws.send_text('{"type":"ping","t":1}')
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 1}


def test_a_frame_whose_pixels_cant_be_read_sends_nothing(band_screen) -> None:
    from api.main import app

    _store, ocr, token, _admin = band_screen
    jpeg = unique_jpeg(9)
    ocr.register(jpeg, OcrResult(W, H, ()))
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(teams_header(int(time.time() * 1000), "full"), jpeg))
        wait_handled(1)
        ws.send_text('{"type":"ping","t":2}')
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 2}
