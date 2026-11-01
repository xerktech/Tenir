from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest
from PIL import Image

from tenir_capture.config import WindowSpec
from tenir_capture.frames import (
    MAX_FULL_PIXELS,
    Cadence,
    WindowPlan,
    encode,
    fit_full,
    parse_server,
    resolve,
    too_old,
)

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contract" / "ws-messages.schema.json").read_text()
)
SPEC = WindowSpec("Horizon 3", "org-c", "Org C", "zoom")
CAD = Cadence(fps=1.0, full_frame_seconds=30.0)


def split(data: bytes) -> tuple[dict, bytes]:
    (n,) = struct.unpack_from(">I", data)
    return json.loads(data[4 : 4 + n]), data[4 + n :]


def test_resolve_takes_an_exact_title_or_one_unique_substring() -> None:
    titles = ["Horizon 3 - Desktop", "Remote Desktop Manager [laptop]", "Inbox"]
    assert resolve("Inbox", titles) == ("Inbox", None)
    assert resolve("horizon 3", titles) == ("Horizon 3 - Desktop", None)
    # RDM with its Dashboard tab in front has another title: the window pauses.
    assert resolve("Remote Desktop Manager [laptop]", ["Remote Desktop Manager [Dashboard]"]) == (
        None,
        "no visible window matches",
    )
    title, problem = resolve("Desktop", titles)
    assert title is None and problem is not None and "ambiguous" in problem
    # An exact title wins even when it is also a substring of another window's.
    assert resolve("Horizon 3", ["Horizon 3", "Horizon 3 (2)"]) == ("Horizon 3", None)


def test_an_encoded_frame_matches_the_screen_frame_header_contract() -> None:
    data = encode(SPEC, "T" * 600, "caption", 1234, b"\xff\xd8jpeg")
    header, body = split(data)
    contract = SCHEMA["$defs"]["ScreenFrameHeader"]
    assert set(header) == set(contract["required"]) == set(contract["properties"])
    assert header["type"] == contract["properties"]["type"]["const"]
    assert header["region"] in contract["properties"]["region"]["enum"]
    assert len(header["windowTitle"]) == 512
    assert body == b"\xff\xd8jpeg"


def test_caption_crops_go_only_while_a_caption_shows_with_a_full_frame_first() -> None:
    caption = {"on": False}
    plan = WindowPlan(lambda img: img.crop((0, 0, 10, 10)) if caption["on"] else None)
    img = Image.new("RGB", (64, 48))

    def regions(now: float) -> list[str]:
        return [r for r, _ in plan.frames(now, img, CAD)]

    assert regions(0) == ["full"]  # first frame of a window: full, so the roster is known
    assert not plan.due(0.5) and plan.due(1.0)
    assert regions(1) == []  # no caption, no full frame due: nothing on the wire
    caption["on"] = True
    assert regions(2) == ["full", "caption"]  # first caption after none: full frame first
    assert regions(3) == ["caption"]
    assert regions(31.9) == ["caption"]
    assert regions(32) == ["full", "caption"]  # fullFrameSeconds since the last full
    caption["on"] = False
    assert regions(33) == []
    assert all(d.startswith(b"\xff\xd8") for _, d in plan.frames(70, img, CAD))


def test_a_faster_cadence_reschedules_a_window_at_once() -> None:
    plan = WindowPlan(lambda img: None)
    plan.frames(0, Image.new("RGB", (8, 8)), Cadence(0.2, 30))
    assert not plan.due(1)
    plan.hurry(1)
    assert plan.due(1)


def test_the_real_finder_sends_the_zoom_caption_box_and_nothing_without_one(zoom_frame) -> None:
    from tenir_capture.caption_box import crop_box

    plan = WindowPlan(crop_box)
    out = dict(plan.frames(0, zoom_frame(caption=True), CAD))
    assert set(out) == {"full", "caption"}
    crop = Image.open(io.BytesIO(out["caption"]))
    full = Image.open(io.BytesIO(out["full"]))
    assert full.size == (1920, 1080)
    # Only the caption box crosses the wire, under tenir-ocr's 0.5 Mpx cap.
    assert crop.width * crop.height < 500_000 and crop.height < 200
    assert plan.frames(1, zoom_frame(caption=False, dark=True), CAD) == []


def test_huge_windows_are_scaled_to_what_the_server_decodes() -> None:
    small = Image.new("RGB", (1920, 1080))
    assert fit_full(small) is small
    big = fit_full(Image.new("RGB", (7680, 4320)))
    assert big.width * big.height <= MAX_FULL_PIXELS
    assert abs(big.width / big.height - 16 / 9) < 0.01


def test_server_messages_are_parsed_and_bounded() -> None:
    m = parse_server('{"type":"screen.cadence","fps":1,"fullFrameSeconds":30}')
    assert (m.kind, m.org_site_key, m.cadence) == ("cadence", None, Cadence(1.0, 30.0))
    m = parse_server('{"type":"screen.cadence","orgSiteKey":"k","fps":100,"fullFrameSeconds":0.1}')
    assert (m.org_site_key, m.cadence) == ("k", Cadence(5.0, 5.0))
    for bad in (
        '{"type":"screen.cadence","fps":0,"fullFrameSeconds":30}',
        '{"type":"screen.cadence","fps":true,"fullFrameSeconds":30}',
        '{"type":"screen.cadence","fps":"1","fullFrameSeconds":30}',
        "not json",
        "[1]",
        '{"type":"pong"}',
        '{"type":"screen.notice","kind":"captions_off"}',
    ):
        assert parse_server(bad).kind == "other", bad
    m = parse_server('{"type":"screen.notice","orgSiteKey":"k","kind":"captions_off"}')
    assert (m.kind, m.org_site_key, m.text) == ("notice", "k", "captions_off")
    assert parse_server('{"type":"error","code":"bad_request","message":"x"}').text == "x"


@pytest.mark.parametrize(
    ("mine", "minimum", "old"),
    [
        ("0.6.3", None, False),
        ("0.6.3", "", False),
        ("0.6.3", "0.6.3", False),
        ("0.6.3", "0.6.10", True),
        ("0.10.0", "0.9.9", False),
        ("1.0.0", "0.99.0", False),
        ("dev", "9.9.9", False),  # an unbuilt checkout is never blocked
        ("0.6.3", "garbage", False),
    ],
)
def test_version_gate(mine: str, minimum: str | None, old: bool) -> None:
    assert too_old(mine, minimum) is old
