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
        # A mark where the panel's X would be: a slide number, a dot, a logo (QA delta 2 D1).
        *[
            pytest.param({"panel": False, "stage": "black", "mark": m, **kw}, id=f"{m}-{i}")
            for m in ("12", "dot", "logo")
            for i, kw in enumerate([{}, {"size": (1920, 1200), "ui": 1.0}])
        ],
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


@pytest.mark.parametrize("pane", [0.3, 0.45])
def test_captions_survive_a_wide_side_pane(pane) -> None:
    crop = crop_entries(make_teams_window((1920, 1200), 1.0, pane=pane))
    assert crop is not None and 2 <= len(text_lines(crop)) <= MAX_LINES


def window(h: int = 600, w: int = 1000) -> np.ndarray:
    return np.full((h, w), 31, dtype=np.uint8)


def panel(
    top: int = 420, bottom: int = 597, x: bool = True, h: int = 600, w: int = 1000, x_at: int = 13
) -> np.ndarray:
    """A black panel across a window, with its close X (``x_at`` px down, ~20 px in)."""
    g = window(h, w)
    g[top:bottom, 4 : w - 4] = 0
    if x:
        for i in range(10):
            g[top + x_at + i, w - 30 + i] = g[top + x_at + 9 - i, w - 30 + i] = 255
    return g


def lines_at(g: np.ndarray, ys, x0: int = 360, x1: int = 600) -> None:
    for y in ys:
        g[y : y + 6, x0:x1:2] = 255  # glyph-like: strokes, not a solid bar


def avatars_at(g: np.ndarray, ys, x0: int = 300, size: int = 20) -> None:
    for y in ys:
        g[y : y + size, x0 : x0 + size] = 200


def test_black_that_isnt_the_panel_is_rejected() -> None:
    g = window()
    g[300:400] = 0  # a black strip above the stage bottom (a dark slide band)
    assert find_panel(g) is None
    g = window()
    g[590:597] = 0  # a black strip at the bottom, too short to hold an entry
    assert find_panel(g) is None
    assert find_panel(panel(top=100)) is None  # too tall: a black slide filling the stage
    g = panel(top=400, bottom=540)  # an X-marked black block that stops short of the bottom
    assert find_panel(g) is None
    g = panel(top=575, x=False)  # too short to hold an entry, even with an X
    for i in range(5):
        g[576 + i, 975 + i] = g[580 - i, 975 + i] = 255
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


@pytest.mark.parametrize(
    "mark",
    [
        pytest.param((slice(433, 443), slice(972, 980)), id="filled-square"),
        pytest.param((slice(433, 443), slice(975, 977)), id="bar"),
        pytest.param((slice(433, 434), slice(975, 976)), id="speck"),  # a glyph's tip
        pytest.param((slice(433, 436), slice(975, 978)), id="3px-blob"),
    ],
)
def test_a_corner_mark_that_isnt_an_x_is_not_the_close_button(mark) -> None:
    g = panel(x=False)
    g[mark] = 255
    assert find_panel(g) is None


def test_small_and_large_marks_must_be_crosses_too() -> None:
    g = panel(x=False)
    g[433:440, 972:980] = 255
    g[435:438, 974:978] = 0  # a 7x8 ring ("0", "41"): checked for shape from 6 px
    assert find_panel(g) is None
    g = panel(x=False)
    for i in range(10):
        g[433 + i, 970 + i] = 255  # a slash: one diagonal only
    assert find_panel(g) is None
    g = panel(x=False, w=1000)
    for i in range(40):  # a big X: wider than the close button ever is
        g[425 + i, 955 + i] = g[464 - i, 955 + i] = 255
    assert find_panel(g) is None


def test_a_tiny_close_button_in_a_small_remote_view_still_counts() -> None:
    g = panel(x=False)
    g[433:438, 975:980] = 60  # a 5 px X blurred by a remote view's downscale into a blob:
    for i in range(5):  # too small to judge its shape, but square and big enough
        g[433 + i, 975 + i] = g[437 - i, 975 + i] = 255
    assert find_panel(g) == (4, 420, 996, 597)


def test_a_near_black_video_is_not_the_panel() -> None:
    g = panel()
    g[420:597, 4:996][g[420:597, 4:996] == 0] = 15  # darker than a tile, but not the panel's 0
    assert find_panel(g) is None


def test_the_close_button_must_be_alone_high_in_the_corner() -> None:
    g = panel()
    g[433:443, 955:958] = 255  # a second mark beside the X
    assert find_panel(g) is None
    g = panel()
    g[433:443, 986:989] = 255  # ... or right of it
    assert find_panel(g) is None
    g = panel()
    g[450:454, 972:980] = 255  # a mark under the X, in the same columns
    assert find_panel(g) is None
    assert find_panel(panel(x_at=120)) is None  # an X low in the panel isn't its close button
    g = panel()
    g[:, 996:] = 255  # a lit window border is outside the panel
    assert find_panel(g) == (4, 420, 996, 597)


def test_dim_marks_without_glyphs_are_not_captions() -> None:
    g = panel()
    g[450:456, 360:600:2] = 80  # grey, not white text (a dim icon row)
    assert find_caption_entries(g) is None


def test_a_panel_without_avatars_still_crops_its_newest_lines() -> None:
    g = panel()
    lines_at(g, range(425, 595, 17))  # ten text lines, no avatar column
    box = find_caption_entries(g)
    assert box is not None
    left, top, right, bottom = box
    assert (left, right) == (354, 605)
    assert top == 425 + 2 * 17 - 6  # the newest MAX_LINES lines
    assert bottom == 425 + 9 * 17 + 6 + 6


def test_a_line_cut_by_the_panel_top_is_left_out() -> None:
    g = panel()
    g[420:424, 360:600:2] = 255  # the bottom of a line scrolled half off
    lines_at(g, (440, 460))
    box = find_caption_entries(g)
    assert box is not None and box[1] == 440 - 6


def test_an_avatar_below_the_last_line_cant_invert_the_crop() -> None:
    g = panel()
    avatars_at(g, (425,))
    lines_at(g, range(425, 570, 15))  # one entry of ten lines: over MAX_LINES
    avatars_at(g, (575,))  # a new entry whose text hasn't arrived yet
    box = find_caption_entries(g)
    assert box is not None and box[1] < box[3]
    assert box[1] == 425 + 2 * 15 - 6  # the newest 8 lines; the header scrolled out of reach
    assert crop_entries(Image.fromarray(g).convert("RGB")) is not None


def test_at_most_four_entries_go_even_when_their_lines_merge() -> None:
    g = panel()
    ys = range(430, 570, 26)  # six entries whose name and text merged into one line each
    avatars_at(g, ys)
    lines_at(g, ys)
    box = find_caption_entries(g)
    assert box is not None and box[1] == ys[2] - 6


def test_an_avatar_close_to_its_text_is_still_split_off() -> None:
    g = panel()
    avatars_at(g, (430, 470))
    lines_at(g, (430, 440, 470, 480), x0=322)  # 2 px from the avatars
    box = find_caption_entries(g)
    assert box is not None and box[0] == 322 - 6


def test_a_column_that_isnt_round_is_text_not_avatars() -> None:
    g = panel()
    g[430:500, 300:320] = 200  # a tall bar left of the text: not a column of circles
    lines_at(g, (430, 450, 470, 490))
    box = find_caption_entries(g)
    assert box is not None and box[0] == 300 - 6


@pytest.mark.parametrize("x1", range(1100, 1300, 23))
def test_the_crop_stays_under_the_ocr_pixel_cap(x1) -> None:
    g = panel(top=280, bottom=497, h=500, w=1400)
    lines_at(g, range(290, 490, 24), x1=x1)
    crop = crop_entries(Image.fromarray(g).convert("RGB"))
    assert crop is not None
    # Upscaling to caption size (x2.16) would give ~0.9 Mpx: the cap binds. Sizes are floored;
    # rounding both sides overshoots the cap for about half of all shapes.
    assert MAX_PIXELS * 0.99 <= crop.width * crop.height <= MAX_PIXELS
