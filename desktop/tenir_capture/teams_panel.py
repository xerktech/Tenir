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
PANEL_ROW_SHARE = 0.75  # a panel row is mostly black even through a long caption line
PANEL_MIN_HEIGHT = 0.06  # of the window height: shorter black strips are not the panel
PANEL_BOTTOM_SLACK = 0.04  # the panel reaches the window bottom, bar a border
PANEL_MIN_WIDTH = 0.9  # it spans the stage: black columns over this share of the width
CLOSE_ZONE = 0.92  # ink that starts right of this share of the width is the close button
EDGE = 0.01  # ink starting this close to the left edge is the window border
AVATAR_MAX_WIDTH = 0.05  # of the window width: a wider first ink column is text, not avatars
COL_GAP = 4  # ink columns this close are one column (letter and word gaps are filled by rows)
LINE_GAP = 1  # glyph rows this close are one text line
LINE_MIN_ROWS = 4  # shorter ink runs are specks, not text
# Lines sent per crop. tenir-ocr refuses more than 16 boxes as too dense; the real panel showed
# 8 entries (17 lines). Eight lines is about four entries, plenty of overlap for the merge.
MAX_LINES = 8
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


def find_panel(g: np.ndarray) -> tuple[int, int] | None:
    """(top, bottom) rows of the caption panel in a grayscale window, or None."""
    h, w = g.shape
    black = g <= PANEL_BLACK
    rows = black.mean(axis=1) >= PANEL_ROW_SHARE
    for top, bottom in reversed(_runs(rows, 2)):
        if bottom < h * (1 - PANEL_BOTTOM_SLACK):
            return None  # the lowest black strip doesn't reach the bottom: no panel
        if bottom - top < h * PANEL_MIN_HEIGHT:
            continue
        # A black slide or an all-dark camera tile is not the panel: the panel is black across
        # (nearly) every column, row after row.
        if black[top:bottom].mean(axis=0).__ge__(0.5).mean() < PANEL_MIN_WIDTH:
            return None
        return top, bottom
    return None


def find_caption_entries(g: np.ndarray) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the newest caption lines, or None without any."""
    h, w = g.shape
    panel = find_panel(g)
    if panel is None:
        return None
    p_top, p_bottom = panel
    ink = g[p_top:p_bottom] > INK
    glyph = g[p_top:p_bottom] > GLYPH
    cols = [(a, b) for a, b in _runs(ink.any(axis=0), COL_GAP) if w * EDGE < a < w * CLOSE_ZONE]
    if not cols:
        return None
    avatars: list[tuple[int, int]] = []
    if len(cols) > 1 and cols[0][1] - cols[0][0] <= w * AVATAR_MAX_WIDTH:
        a0, a1 = cols[0]
        avatars = _runs(ink[:, a0:a1].any(axis=1), LINE_GAP)
        cols = cols[1:]
    left, right = cols[0][0], cols[-1][1]
    lines = [
        (a, b)
        for a, b in _runs(glyph[:, left:right].any(axis=1), LINE_GAP)
        if b - a >= LINE_MIN_ROWS and a > 1  # a line cut by the panel top is half a line
    ]
    if not lines:
        return None
    # Start at an entry's avatar (its name line) when one fits, so the crop opens on a header.
    starts = [a for a, _ in avatars if a > 1] or [a for a, _ in lines]
    newest = lines[-MAX_LINES:]
    fits = [s for s in starts if s >= newest[0][0] - PAD]
    top = min(fits) if fits else newest[0][0]
    return (
        max(0, left - PAD),
        max(0, p_top + top - PAD),
        min(w, right + PAD),
        min(h, p_top + lines[-1][1] + PAD),
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
    size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    return crop.resize(size, Image.Resampling.LANCZOS)
