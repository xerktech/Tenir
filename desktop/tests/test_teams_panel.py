"""The Teams caption-panel finder on synthetic windows shaped like a real capture."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageOps
from teams_synth import make_teams_window

from tenir_capture.frames import FINDERS, Cadence, WindowPlan
from tenir_capture.teams_panel import (
    LINE_GAP,
    LINE_MIN_ROWS,
    MAX_LINES,
    MAX_PIXELS,
    _runs,
    crop_entries,
    find_caption_entries,
    find_panel,
)

CAD = Cadence(fps=1.0, full_frame_seconds=30.0)


def text_lines(crop: Image.Image) -> list[tuple[int, int]]:
    ink = np.asarray(ImageOps.grayscale(crop)) > 120
    return [(a, b) for a, b in _runs(ink.any(axis=1), LINE_GAP) if b - a >= LINE_MIN_ROWS]


@pytest.mark.parametrize(
    ("size", "ui"),
    [((2475, 1551), 1.5), ((1920, 1080), 1.0), ((1280, 800), 1.0), ((3840, 2400), 2.0)],
)
def test_the_newest_entries_are_cropped_from_a_name_line(size, ui) -> None:
    crop = crop_entries(make_teams_window(size, ui))
    assert crop is not None
    assert crop.width * crop.height <= MAX_PIXELS
    lines = text_lines(crop)
    # Whole entries only, and few enough lines that tenir-ocr doesn't refuse it as too dense
    # (the panel holds 10 lines, the oldest cut by its top edge).
    assert 2 <= len(lines) <= MAX_LINES
    assert len(lines) % 2 == 0  # name + one-line text per newest entry
    # It opens on a speaker's name (each synthetic name is narrower than its caption).
    ink = np.asarray(ImageOps.grayscale(crop)) > 120

    def width(line: tuple[int, int]) -> int:
        cols = np.flatnonzero(ink[line[0] : line[1]].any(axis=0))
        return int(cols[-1] - cols[0])

    assert width(lines[0]) < width(lines[1])
    # Neither the avatar circles nor the close button come along: the crop is text only.
    assert crop.width < size[0] * 0.4


def test_a_small_remote_window_is_upscaled_to_caption_font_size() -> None:
    small = make_teams_window((960, 600), 0.6)
    crop = crop_entries(small)
    assert crop is not None and crop.width * crop.height <= MAX_PIXELS
    assert len(text_lines(crop)) >= 2
    native = crop_entries(make_teams_window((1920, 1200), 1.2))
    assert native is not None and crop.height >= native.height * 0.8


@pytest.mark.parametrize(
    "window",
    [
        pytest.param({"panel": False}, id="captions-off"),
        pytest.param({"captions": []}, id="panel-open-no-one-speaking"),
        pytest.param({"panel": False, "stage": "share"}, id="shared-document"),
        pytest.param({"captions": [], "stage": "share"}, id="shared-document-empty-panel"),
        # A black slide letterboxed to the full width, down to the window bottom (QA D1).
        pytest.param({"panel": False, "stage": "black"}, id="black-slide"),
        pytest.param(
            {"panel": False, "stage": "black", "size": (1440, 900), "ui": 1.0},
            id="black-slide-1440",
        ),
        pytest.param({"panel": False, "pane": 0.22}, id="side-pane-captions-off"),
    ],
)
def test_no_caption_text_sends_nothing(window) -> None:
    img = make_teams_window(**window)
    assert crop_entries(img) is None
    # Only the periodic full frame goes; the shared page never streams as captions.
    plan = WindowPlan(FINDERS["teams"])
    assert [r for r, _ in plan.frames(0, img, CAD)] == ["full"]
    assert plan.frames(1, img, CAD) == []


def test_captions_under_a_shared_document_are_cropped_without_the_page() -> None:
    crop = crop_entries(make_teams_window(stage="share"))
    assert crop is not None
    g = np.asarray(ImageOps.grayscale(crop))
    assert (g > 200).mean() < 0.3  # white glyphs on black, not the white page


def test_a_long_entry_is_kept_whole_or_left_out() -> None:
    entries = [("Kunal Shah", [f"line {i} of a long monologue" for i in range(12)])]
    crop = crop_entries(make_teams_window(captions=entries))
    assert crop is not None
    # No avatar fits under the line cap: the newest lines go without their header.
    assert len(text_lines(crop)) <= MAX_LINES


def test_the_plan_sends_a_full_frame_then_crops_while_captions_show() -> None:
    plan = WindowPlan(FINDERS["teams"])
    out = plan.frames(0, make_teams_window(), CAD)
    assert [r for r, _ in out] == ["full", "caption"]
    assert Image.open(io.BytesIO(out[1][1])).width < 2475 * 0.4


@pytest.mark.parametrize("pane", [0.08, 0.22])
def test_captions_are_found_beside_an_open_side_pane(pane) -> None:
    crop = crop_entries(make_teams_window(pane=pane))
    assert crop is not None
    assert 2 <= len(text_lines(crop)) <= MAX_LINES
    assert crop.width < 2475 * (1 - pane) * 0.4  # the caption column, not the pane's names


def test_a_half_scale_remote_window_keeps_avatars_and_lines_apart() -> None:
    # A 1920x1080 meeting at 100% shown in a 960x540 remote window (QA D2).
    crop = crop_entries(make_teams_window((960, 540), 0.5))
    assert crop is not None
    assert 2 <= len(text_lines(crop)) <= MAX_LINES


def test_the_crop_opens_on_a_name_even_when_the_line_cap_falls_mid_entry() -> None:
    entries = [(f"Speaker {i}", ["first line of a reply", "and its second line"]) for i in range(5)]
    crop = crop_entries(make_teams_window(captions=entries))
    assert crop is not None
    # 3 lines per entry: the newest 8 lines start inside an entry, so the crop starts at the
    # next avatar instead: two whole entries.
    assert len(text_lines(crop)) == 6


def test_the_crop_stays_under_the_ocr_pixel_cap() -> None:
    entries = [("Kunal Shah", ["a very long caption line " * 6])] * 3
    crop = crop_entries(make_teams_window((3000, 420), 1.5, captions=entries))
    assert crop is not None
    assert MAX_PIXELS * 0.95 <= crop.width * crop.height <= MAX_PIXELS


def window(h: int = 600, w: int = 1000) -> np.ndarray:
    return np.full((h, w), 31, dtype=np.uint8)


def panel(top: int = 420, bottom: int = 597, x: bool = True) -> np.ndarray:
    """A black panel across a 1000x600 window, with its close X (13-23 px down, ~20 px in)."""
    g = window()
    g[top:bottom, 4:996] = 0
    if x:
        for i in range(10):
            g[top + 13 + i, 970 + i] = g[top + 22 - i, 970 + i] = 255
    return g


def test_black_that_isnt_the_panel_is_rejected() -> None:
    g = window()
    g[300:400] = 0  # a black strip above the stage bottom (a dark slide band)
    assert find_panel(g) is None
    g = window()
    g[590:597] = 0  # a black strip at the bottom, too short to hold an entry
    assert find_panel(g) is None
    g = panel()
    g[420:597, 450:] = 31  # black reaching the bottom but not across the stage (a video tile)
    assert find_panel(g) is None
    g = panel()
    g[420:597, 300:500] = 31  # black on both sides of a lit block: not one panel
    assert find_panel(g) is None
    g = panel()
    g[420:597, 4:100] = 31  # black that doesn't start at the window's left edge
    assert find_panel(g) is None
    assert find_panel(panel(x=False)) is None  # no close button: a black slide
    g = panel()
    g[425:440, 940:990] = 255  # something wide in the corner is not the close X
    assert find_panel(g) is None
    assert find_panel(panel()) == (4, 420, 996, 597)


def test_dim_marks_without_glyphs_are_not_captions() -> None:
    g = panel()
    g[450:456, 360:600:2] = 80  # grey, not white text (a dim icon row)
    assert find_caption_entries(g) is None


def test_a_panel_without_avatars_still_crops_its_newest_lines() -> None:
    g = panel()
    for i in range(10):  # ten text lines, no avatar column
        y = 425 + i * 17
        g[y : y + 6, 360:600:2] = 255  # glyph-like: strokes, not a solid bar
    box = find_caption_entries(g)
    assert box is not None
    left, top, right, bottom = box
    assert (left, right) == (354, 605)
    assert top == 425 + 2 * 17 - 6  # the newest MAX_LINES lines
    assert bottom == 425 + 9 * 17 + 6 + 6


def test_a_line_cut_by_the_panel_top_is_left_out() -> None:
    g = panel()
    g[420:424, 360:600:2] = 255  # the bottom of a line scrolled half off
    g[440:446, 360:600:2] = 255
    g[460:466, 360:600:2] = 255
    box = find_caption_entries(g)
    assert box is not None and box[1] == 440 - 6


def test_an_avatar_below_the_last_line_cant_invert_the_crop() -> None:
    g = panel()
    for y in (440, 470):
        g[y : y + 20, 300:320] = 200  # avatars
        g[y : y + 6, 360:600:2] = 255
    g[560:580, 300:320] = 200  # an avatar whose text hasn't arrived yet
    box = find_caption_entries(g)
    assert box is not None and box[1] < box[3]
    assert crop_entries(Image.fromarray(g).convert("RGB")) is not None
