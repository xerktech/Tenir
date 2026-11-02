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


def window(h: int = 600, w: int = 1000) -> np.ndarray:
    return np.full((h, w), 31, dtype=np.uint8)


def test_black_that_isnt_the_panel_is_rejected() -> None:
    g = window()
    g[300:400] = 0  # a black strip above the stage bottom (a dark slide band)
    assert find_panel(g) is None
    g = window()
    g[590:597] = 0  # a black strip at the bottom, too short to hold an entry
    assert find_panel(g) is None
    g = window()
    g[400:597, :800] = 0  # black reaching the bottom but not across the stage (a video tile)
    assert find_panel(g) is None
    g = window()
    g[420:597, 4:996] = 0
    assert find_panel(g) == (420, 597)


def test_a_panel_without_avatars_still_crops_its_newest_lines() -> None:
    g = window()
    g[420:597, 4:996] = 0
    for i in range(10):  # ten text lines, no avatar column
        y = 425 + i * 17
        g[y : y + 6, 360:600:2] = 255  # glyph-like: strokes, not a solid bar
    box = find_caption_entries(g)
    assert box is not None
    left, top, right, bottom = box
    assert (left, right) == (354, 605)
    assert top == 425 + 2 * 17 - 6  # the newest MAX_LINES lines
    assert bottom == 425 + 9 * 17 + 6 + 6
