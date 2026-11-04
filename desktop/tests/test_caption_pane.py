"""Teams caption crops from the pane the api located (XERK-2608)."""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageOps
from teams_synth import ENTRIES, make_desktop

from tenir_capture.caption_pane import MAX_PIXELS, PaneCrop, crop_pane
from tenir_capture.frames import parse_server


def lines(crop: Image.Image) -> list[tuple[int, int]]:
    """Glyph rows of a crop, as [start, end) runs."""
    g = np.asarray(ImageOps.grayscale(crop)) > 120
    rows = np.flatnonzero(g.any(axis=1))
    runs, start = [], int(rows[0])
    for a, b in itertools.pairwise(rows):
        if b - a > 1:
            runs.append((start, int(a) + 1))
            start = int(b)
    runs.append((start, int(rows[-1]) + 1))
    return runs


@pytest.mark.parametrize("captions", [ENTRIES, ENTRIES * 4], ids=["filling", "scrolled"])
def test_the_crop_ends_at_the_newest_entry_and_opens_on_a_whole_line(captions) -> None:
    img, region = make_desktop(captions=captions)
    crop = crop_pane(img, region)
    assert crop is not None
    assert region is not None
    (left, _, right, _), share = region
    assert crop.width == round(right * img.width) - int(left * img.width)
    assert crop.height <= share * img.height + 6
    g = np.asarray(ImageOps.grayscale(crop))
    assert not (g[0] > 40).any()  # it opens on a blank row, never half a line
    assert (g[-6:] <= 40).all()  # and ends just under the newest text
    assert len(lines(crop)) >= 3  # several entries (each name runs into its text here)


def test_a_partly_filled_pane_sends_its_entries_from_the_top() -> None:
    img, region = make_desktop(captions=ENTRIES[:1])
    crop = crop_pane(img, region)
    # One entry of a name and two lines (74 px), whole: the crop doesn't reach above it.
    assert crop is not None and 74 <= crop.height <= 74 + 2 * 6 + 8


@pytest.mark.parametrize(
    "case",
    ["no region", "empty pane", "pane closed", "tiny box"],
)
def test_nothing_is_sent_without_a_pane_showing_text(case) -> None:
    img, region = make_desktop(captions=[] if case == "empty pane" else None)
    assert region is not None
    if case == "no region":
        region = None
    elif case == "pane closed":
        # The api's box from before; the window now shows a slide with text there (grey, not
        # the pane's black): its text must not go out as captions.
        img, _ = make_desktop(pane=False)
        (left, top, right, bottom), _share = region
        d = ImageDraw.Draw(img)
        x0, x1 = int(left * img.width), int(right * img.width)
        for y in range(int(top * img.height), int(bottom * img.height), 30):
            d.text((x0 + 10, y), "Quarterly roadmap and owners", fill=(255, 255, 255))
        assert crop_pane(img, (region[0], 1.0)) is None
        g = np.asarray(ImageOps.grayscale(img.crop((x0, 0, x1, img.height))))
        assert (g > 120).any()  # there is glyph ink: only the black check refuses it
    elif case == "tiny box":
        (left, top, _, _), share = region
        region = ((left, top, left + 1 / img.width, top + 1 / img.height), share)
    assert crop_pane(img, region) is None


@pytest.mark.parametrize("mark", ["border", "scrollbar", "corner"])
def test_a_border_or_scrollbar_in_the_box_is_not_the_newest_text(mark) -> None:
    # Real capture at 0.6-0.7x: the box caught a window border at its edge, or the pane's
    # scrollbar thumb in its right margin, and every crop was the pane's blank bottom (QA).
    img, region = make_desktop()
    assert region is not None
    (left, top, right, bottom), _share = region
    w, h = img.size
    d = ImageDraw.Draw(img)
    x0, x1, y0, y1 = int(left * w), int(right * w), int(top * h), int(bottom * h)
    if mark == "border":
        d.line((x0 + 8, y0, x0 + 8, y1), fill=(150, 150, 150))  # in the left margin
    elif mark == "scrollbar":
        d.rectangle((x1 - 6, y1 - 60, x1 - 3, y1 - 20), fill=(160, 160, 160))
    else:  # a window's rounded corner, at the bottom of a box placed a few px too wide
        d.rectangle((x0, y1 - 10, x0 + 8, y1 - 4), fill=(160, 160, 160))
    crop = crop_pane(img, region)
    assert crop is not None
    g = np.asarray(ImageOps.grayscale(crop))
    assert (g[:, 20:-20] > 120).mean() > 0.05  # the entries, not a blank tail


def test_the_crop_opens_between_entries_not_through_an_avatar() -> None:
    # Avatars nearly touch in a scrolled pane, lighting their column on most rows. The row
    # the crop opens on must be blank across the whole box: judged without the avatar
    # column, real crops opened through an avatar, OCR read its initials into the name and
    # a real meeting recorded nothing (QA).
    w, h = 400, 900
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(0, h - 54, 54):  # 50 px avatars every 54 px: lit on ~93% of rows
        d.ellipse((12, y, 62, y + 50), fill=(150, 150, 150))
        d.text((72, y + 4), "Kunal Shah", fill=(255, 255, 255))
        d.text((72, y + 24), "Can we move the review?", fill=(255, 255, 255))
    crop = crop_pane(img, ((0.0, 0.0, 1.0, 1.0), 200 / h))
    assert crop is not None
    g = np.asarray(ImageOps.grayscale(crop))
    assert not (g[0] > 40).any()


def test_a_crop_over_the_ocr_pixel_cap_is_scaled_under_it() -> None:
    img, region = make_desktop((5000, 3000), (100, 100, 4900, 2900), ui=3.0, captions=ENTRIES * 4)
    assert region is not None
    crop = crop_pane(img, (region[0], 0.6))
    assert crop is not None and crop.width * crop.height <= MAX_PIXELS


def test_the_finder_holds_the_last_region() -> None:
    img, region = make_desktop()
    finder = PaneCrop()
    assert finder(img) is None
    finder.region = region
    assert finder(img) is not None


@pytest.mark.parametrize(
    "raw",
    [
        '{"type":"screen.region","box":[0.1,0.1,0.5,0.5],"cropHeight":0.1}',  # no window
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.1,0.5],"cropHeight":0.1}',
        '{"type":"screen.region","orgSiteKey":"k","box":[0.5,0.1,0.1,0.5],"cropHeight":0.1}',
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.5,0.5,0.1],"cropHeight":0.1}',
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.1,1.5,0.5],"cropHeight":0.1}',
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.1,0.5,0.5],"cropHeight":0}',
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.1,0.5,0.5]}',
        '{"type":"screen.region","orgSiteKey":"k","box":[true,0.1,0.5,0.5],"cropHeight":0.1}',
        '{"type":"screen.region","orgSiteKey":"k","box":"0.1,0.1,0.5,0.5","cropHeight":0.1}',
    ],
)
def test_a_malformed_region_is_ignored(raw) -> None:
    assert parse_server(raw).kind == "other"


def test_a_region_is_parsed_and_its_absence_means_the_pane_is_gone() -> None:
    m = parse_server(
        '{"type":"screen.region","orgSiteKey":"k","box":[0.1,0.2,0.5,0.9],"cropHeight":0.1}'
    )
    assert (m.kind, m.org_site_key, m.region) == ("region", "k", ((0.1, 0.2, 0.5, 0.9), 0.1))
    m = parse_server('{"type":"screen.region","orgSiteKey":"k"}')
    assert (m.kind, m.org_site_key, m.region) == ("region", "k", None)
