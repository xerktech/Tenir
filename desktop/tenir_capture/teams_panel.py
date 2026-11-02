"""Microsoft Teams caption-panel finder (XERK-2608).

Teams shows live captions in a pure-black panel across the full width of the meeting stage,
below the tiles (the stage is dark grey ~31, a camera-off tile ~10, the panel 0). Entries stack
oldest to newest, newest at the bottom: an avatar circle, the speaker's name on a small line,
then the caption text wrapping over one or more lines. The column of entries sits mid-panel;
a close button sits at the panel's top right.

The finder returns the newest entries of that panel, or None when there is no panel or it
holds no text, so an idle Teams window (tiles, a shared document) sends nothing but its
periodic full frame. Measured on one real capture (new Teams, dark theme, 150% scaling);
the thresholds are fractions of the window or pixel levels, not absolute sizes.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps

from tenir_capture.caption_box import TARGET_HEIGHT

PANEL_BLACK = 6  # panel fill is 0; a camera-off tile is ~10 and the stage ~31
INK = 40  # anything brighter than this inside the panel is avatar or glyph
GLYPH = 120  # glyph cores: lines split on these, so JPEG ringing between lines doesn't join them
# A panel row is black over this share of the width: the panel spans the stage, which an open
# Chat/People pane narrows (to ~55% of the window at most).
PANEL_ROW_SHARE = 0.4
PANEL_MIN_HEIGHT = 0.06  # of the window height: shorter black strips are not the panel
# The real panel is 28% of the window; a black slide or screen share filling the stage is ~90%.
PANEL_MAX_HEIGHT = 0.5
PANEL_BOTTOM_SLACK = 0.04  # the panel reaches the window bottom, bar a border
PANEL_LEFT = 0.03  # of the window width: the panel starts at the window's left edge
PANEL_FILL = 0.85  # share of the panel's columns that are mostly black (avatars break a few)
# The panel's close button: a small X near its top-right corner. It is the panel's signature:
# a black slide or a dark video across the stage bottom has no X there. Measured 13-23 px below
# the panel top and 18-28 px in from its right edge at 100% scaling, about 10 px wide.
CLOSE_TOP = 0.3  # the X sits within this share of the panel height from its top
CLOSE_RIGHT = 0.06  # ... and within this share of the window width from the panel's right
CLOSE_MAX_WIDTH = 0.03  # ... and is narrower than this share of the window width
# ... and is a cross: its ink lies on the two diagonals of its box, both drawn. A slide number,
# a dot or a logo in a black slide's corner is not. Below CLOSE_SHAPE_MIN px the X is a blur
# and only its size and squareness are checked. The real X is ~10 px at 100% scaling (15 px
# at 150%, 5 px in a 0.25x remote view); a speck under CLOSE_MIN_PX (a glyph's tip) is not it.
CLOSE_MIN_PX = 4
CLOSE_SHAPE_MIN = 6
CLOSE_ASPECT = (0.7, 1.4)
CLOSE_DIAG = 0.22  # an ink pixel within this share of the box from a diagonal is on it
CLOSE_ON_DIAG = 0.85  # share of the blob's ink on a diagonal
CLOSE_EACH_DIAG = 0.3  # share of the ink on each diagonal (an X, not a slash)
EDGE = 0.01  # ink starting this close to the panel's left edge is the window border
AVATAR_ROUND = (0.6, 1.6)  # avatar circles: each ink run is about as tall as the column is wide
COL_GAP = 1  # ink columns this close are one column
LINE_GAP = 1  # glyph rows this close are one text line
LINE_MIN_ROWS = 4  # shorter ink runs are specks, not text
# Per crop: tenir-ocr refuses more than 16 boxes as too dense; the real panel showed 8 entries
# (17 lines). Both caps apply: lines merge in small remote windows, avatars still count entries.
MAX_LINES = 8
MAX_ENTRIES = 4
PAD = 6  # px kept around the text
MAX_PIXELS = 480_000  # under tenir-ocr's 0.5 Mpx cap (api STRIP_PIXELS)


def _runs(flags: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """[start, end) runs of True, merging runs separated by at most ``gap`` False values."""
    idx = np.flatnonzero(flags)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]])) + 1
    return list(zip(starts.tolist(), ends.tolist(), strict=False))


def _is_cross(blob: np.ndarray) -> bool:
    """True when a blob's ink (bool array, cropped to its box) is an X."""
    h, w = blob.shape
    lo, hi = CLOSE_ASPECT
    if min(h, w) < CLOSE_MIN_PX or not lo <= h / w <= hi:
        return False
    if min(h, w) < CLOSE_SHAPE_MIN:
        return True
    ys, xs = np.nonzero(blob)
    u, v = (xs + 0.5) / w, (ys + 0.5) / h
    main, anti = np.abs(u - v) <= CLOSE_DIAG, np.abs(u + v - 1) <= CLOSE_DIAG
    return bool(
        (main | anti).mean() >= CLOSE_ON_DIAG
        and main.mean() >= CLOSE_EACH_DIAG
        and anti.mean() >= CLOSE_EACH_DIAG
    )


def _has_close_button(ink: np.ndarray, right: int, w: int) -> bool:
    """One X-shaped blob near the panel's top-right corner, and nothing else there."""
    rows = max(1, int(ink.shape[0] * CLOSE_TOP))
    x0 = max(0, right - int(w * CLOSE_RIGHT))
    corner = ink[:rows, x0:right]
    blobs = _runs(corner.any(axis=0), 2)
    if len(blobs) != 1 or blobs[0][1] - blobs[0][0] > w * CLOSE_MAX_WIDTH:
        return False
    a, b = blobs[0]
    ys = _runs(corner[:, a:b].any(axis=1), 2)
    if len(ys) != 1:
        return False
    return _is_cross(corner[ys[0][0] : ys[0][1], a:b])


def find_panel(g: np.ndarray) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the caption panel in a grayscale window, or None."""
    h, w = g.shape
    black = g <= PANEL_BLACK
    rows = black.mean(axis=1) >= PANEL_ROW_SHARE
    runs = _runs(rows, 2)
    if not runs:
        return None
    top, bottom = runs[-1]  # the panel is the lowest black strip and reaches the bottom
    if bottom < h * (1 - PANEL_BOTTOM_SLACK):
        return None
    if not h * PANEL_MIN_HEIGHT <= bottom - top <= h * PANEL_MAX_HEIGHT:
        return None
    cols = np.flatnonzero(black[top:bottom].mean(axis=0) >= 0.5)
    left, right = int(cols[0]), int(cols[-1]) + 1
    if left > w * PANEL_LEFT:
        return None
    if cols.size < (right - left) * PANEL_FILL:
        return None
    if not _has_close_button(g[top:bottom] > INK, right, w):
        return None  # a black slide or a dark video, not the caption panel
    return left, top, right, bottom


def find_caption_entries(g: np.ndarray) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the newest caption lines, or None without any."""
    h, w = g.shape
    panel = find_panel(g)
    if panel is None:
        return None
    p_left, p_top, p_right, p_bottom = panel
    ink = g[p_top:p_bottom] > INK
    glyph = g[p_top:p_bottom] > GLYPH
    text_right = p_right - int(w * CLOSE_RIGHT)  # the close button's zone holds no captions
    cols = [
        (a, b)
        for a, b in _runs(ink.any(axis=0), COL_GAP)
        if p_left + w * EDGE < a and b <= text_right
    ]
    if not cols:
        return None
    avatars: list[tuple[int, int]] = []
    a0, a1 = cols[0]
    # Circles cut by the panel top (scrolling off) aren't round; judge the whole ones.
    first = [(a, b) for a, b in _runs(ink[:, a0:a1].any(axis=1), LINE_GAP) if a > 1]
    lo, hi = AVATAR_ROUND
    if len(cols) > 1 and first and all(lo <= (b - a) / (a1 - a0) <= hi for a, b in first):
        avatars = first  # a column of circles: one per entry, its top level with the name
        cols = cols[1:]
    left, right = cols[0][0], cols[-1][1]
    lines = [
        (a, b)
        for a, b in _runs(glyph[:, left:right].any(axis=1), LINE_GAP)
        if b - a >= LINE_MIN_ROWS and a > 1  # a line cut by the panel top is half a line
    ]
    if not lines:
        return None
    bottom = lines[-1][1]
    top = lines[-MAX_LINES:][0][0]
    # Open on an entry's avatar (its name line) when one fits, so the crop starts on a header.
    entries = [a for a, _ in avatars if a < bottom][-MAX_ENTRIES:]
    fits = [s for s in entries if s >= top - PAD]
    if fits:
        top = min(fits)  # else the newest entry alone is over MAX_LINES: its first lines drop
    return (
        max(0, left - PAD),
        max(0, p_top + top - PAD),
        min(w, right + PAD),
        min(h, p_top + bottom + PAD),
    )


def crop_entries(img: Image.Image) -> Image.Image | None:
    """The newest Teams caption entries at caption-font scale; None when none are on screen."""
    g = np.asarray(ImageOps.grayscale(img), dtype=np.uint8)
    box = find_caption_entries(g)
    if box is None:
        return None
    crop = img.crop(box)
    # Upscale small (remote, downscaled) windows to caption-font size, never shrink, then cap.
    scale = min(
        max(1.0, TARGET_HEIGHT / img.height), (MAX_PIXELS / (crop.width * crop.height)) ** 0.5
    )
    if 1.0 <= scale < 1.05:  # already at caption size and under the cap: skip a blurring resize
        return crop
    # Floor, not round: rounding both sides up can overshoot the cap.
    size = (max(1, int(crop.width * scale)), max(1, int(crop.height * scale)))
    return crop.resize(size, Image.Resampling.LANCZOS)
