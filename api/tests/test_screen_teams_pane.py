"""Locating Teams' live-captions pane on full frames and telling the client (XERK-2608).

Frames are synthetic renders on real-capture levels (stage ~31, pane 0, divider luma ~82);
no real capture is committed (public repo). OCR is hand-placed: the heading and caption
lines where the render draws them.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from api.screen import teams
from api.screen.ocr import OcrLine, OcrResult
from api.screen.teams_pane import CROP_PITCHES, PITCH, find_pane
from test_screen import envelope, screen, unique_jpeg, wait_handled  # noqa: F401
from test_screen_teams import teams_header

W, H = 2560, 1600
# A Teams window floating in a remote desktop; its pane docked right.
WINDOW = (300, 200, 1900, 1400)
PANE_X = (1500, 1898)
HEADING = (1524, 308, 1650, 330)  # "Live Captions"
DIVIDER_Y = 354


def render(
    *,
    divider: bool = True,
    pane_bottom: int = 1398,
    under_divider: bool = False,
    desktop: tuple[int, int, int] = (37, 37, 38),
) -> bytes:
    img = Image.new("RGB", (W, H), desktop)
    d = ImageDraw.Draw(img)
    d.rectangle(WINDOW, fill=(31, 31, 31))
    d.rectangle((PANE_X[0], 280, PANE_X[1], pane_bottom), fill=(0, 0, 0))
    d.rectangle(HEADING, fill=(255, 255, 255))  # the heading's glyphs, as one block
    if divider:
        d.line((PANE_X[0], DIVIDER_Y, PANE_X[1] - 1, DIVIDER_Y), fill=(82, 82, 82))
    if under_divider:  # an entry scrolled up against the divider
        d.rectangle((1510, DIVIDER_Y + 1, 1700, DIVIDER_Y + 14), fill=(220, 220, 220))
    for i in range(4):  # caption lines: strokes, not solid bars
        y = 1200 + 40 * i
        for x in range(1564, 1860, 6):
            d.line((x, y, x, y + 21), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def ocr(*extra: OcrLine, heading: str = "Live Captions") -> OcrResult:
    lines = [OcrLine(heading, HEADING)]
    lines += [
        OcrLine(f"Caption line {i}.", (1564, 1200 + 40 * i, 1860, 1222 + 40 * i)) for i in range(4)
    ]
    return OcrResult(W, H, (*lines, *extra))


def test_the_pane_is_found_by_its_heading_and_divider_anywhere_in_the_frame() -> None:
    pane = find_pane(ocr(), render())
    assert pane is not None
    left, top, right, bottom = (round(v * s) for v, s in zip(pane.box, (W, H, W, H), strict=True))
    assert abs(left - PANE_X[0]) <= 1 and abs(right - PANE_X[1]) <= 1
    assert DIVIDER_Y < top <= DIVIDER_Y + 3 and abs(bottom - 1399) <= 2
    # The crop height: CROP_PITCHES caption lines (22 px boxes here), as a share of the frame.
    assert pane.crop == pytest.approx(CROP_PITCHES * PITCH * 22 / H)


def test_an_entry_scrolled_up_to_the_divider_still_finds_the_pane() -> None:
    # Real capture: a scrolled pane's top entry sits right under the divider.
    assert find_pane(ocr(), render(under_divider=True)) is not None


@pytest.mark.parametrize(
    ("result", "jpeg"),
    [
        (OcrResult(W, H, ()), render()),  # no heading read
        (ocr(heading="Live captions are on"), render()),  # other text, not the heading
        (ocr(), render(divider=False)),  # the heading on a slide: no divider under it
        (ocr(), render(pane_bottom=380)),  # a strip, not a pane
        (ocr(), None),  # no pixels
        (ocr(), b"\xff\xd8not a jpeg"),
    ],
)
def test_no_pane_without_heading_divider_and_black_below(result, jpeg) -> None:
    assert find_pane(result, jpeg) is None


def test_a_frame_of_another_size_than_ocr_read_finds_no_pane() -> None:
    assert find_pane(OcrResult(W // 2, H // 2, ocr().lines), render()) is None


def test_caption_lines_in_the_pane_are_never_participants() -> None:
    # Title-Case replies anywhere in the pane (not just its lower half) are captions.
    jpeg = render()
    frame = teams.parse_full_frame(
        ocr(
            OcrLine("Sounds Good", (1564, 420, 1700, 442)),
            OcrLine("Priya Raman", (400, 900, 520, 918)),  # a tile label outside the pane
        ),
        jpeg,
    )
    assert frame.caption_pane is not None
    assert "Sounds Good" not in frame.participants
    assert "Priya Raman" in frame.participants


def test_the_client_is_told_where_the_pane_is_and_when_it_goes(screen) -> None:  # noqa: F811
    from api.main import app

    _store, stub, token, _admin = screen
    with_pane, gone, still_gone = render(), unique_jpeg(1), unique_jpeg(2)
    stub.register(with_pane, ocr())
    stub.register(gone, OcrResult(W, H, ()))  # captions closed: no heading read
    stub.register(still_gone, OcrResult(W, H, ()))
    base = int(time.time() * 1000)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        ws.send_bytes(envelope(teams_header(base, "full"), with_pane))
        wait_handled(1)
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "screen.region" and msg["orgSiteKey"] == "yprime"
        assert len(msg["box"]) == 4 and 0 < msg["cropHeight"] < 1
        assert msg["box"][0] == pytest.approx(PANE_X[0] / W, abs=0.001)
        # The same pane again: nothing re-sent. Then it goes: the client is told, once.
        for i, jpeg in enumerate((with_pane, gone, still_gone), start=1):
            ws.send_bytes(envelope(teams_header(base + 30_000 * i, "full"), jpeg))
            wait_handled(1 + i)
        ws.send_text('{"type":"ping","t":7}')
        assert json.loads(ws.receive_text()) == {"type": "screen.region", "orgSiteKey": "yprime"}
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 7}
    # A new socket is told again: the client dropped its region on reconnect.
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        ws.send_bytes(envelope(teams_header(base + 200_000, "full"), with_pane))
        wait_handled(5)
        assert json.loads(ws.receive_text())["type"] == "screen.region"


def test_a_zoom_window_is_never_sent_a_pane(screen) -> None:  # noqa: F811
    from api.main import app
    from test_screen import header

    _store, stub, token, _admin = screen
    jpeg = render()
    stub.register(jpeg, ocr())
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        ws.send_bytes(envelope(header(int(time.time() * 1000), "full"), jpeg))
        wait_handled(1)
        ws.send_text('{"type":"ping","t":1}')
        assert json.loads(ws.receive_text()) == {"type": "pong", "t": 1}
