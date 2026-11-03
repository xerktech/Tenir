"""Where the Teams live-captions pane sits in a full frame (XERK-2608).

The capture client records a whole remote-desktop window, and Teams floats anywhere inside
it, so the client can't find the captions by window geometry. The api finds them here, on
the full frames it already OCRs, and tells the client which box to crop
(``ScreenRegion``, contract ``screen.region``).

The anchor is text plus one pixel rule, both measured on real captures (new Teams, dark
theme, docked right pane and the popped-out captions window, 100% and 125% scaling):

- the pane's heading reads "Live Captions";
- just under it a 1 px divider (luma ~82, black above it) spans the pane's exact
  width, so the divider gives the pane's left and right edges;
- below it the pane is pure black (0) down to its bottom; caption text and avatars cover
  well under half of any row.

The bottom caption panel Teams also offers has no heading and no divider and is not found:
the operator docks captions on the right (docs/meeting-captions.md).
"""

from __future__ import annotations

import io

import numpy as np

from api.screen.ocr import MAX_FRAME_PIXELS, OcrResult
from api.screen.zoom import CaptionPane, norm_name

HEADING = "live captions"
PANE_BLACK = 12  # the pane is 0; JPEG noise around glyphs stays under this
# The divider: brighter than the stage (~31) and chrome (~41), darker than glyphs (luma
# 70-82 measured): the stage beside the pane at the divider's row must not extend it.
DIVIDER = (55, 130)
DIVIDER_BELOW = 2.0  # the divider sits within this many heading heights under the heading
DARK_ROW = 0.5  # a pane row is at least this share black (text and avatars are the rest)
ROW_GAP = 3  # rows that fail DARK_ROW tolerated inside the pane (a glyph-dense row)
MIN_HEIGHT = 3.0  # pane height in heading heights: shorter is not the pane
# How much of the pane above its lowest text the client sends, in caption-line pitches. The
# crop must stay under tenir-ocr's 16 boxes (too_dense): an entry is up to 3 boxes (initials
# avatar, name, text) over ~2 pitches, so 7 pitches hold at most ~4 entries.
CROP_PITCHES = 7
PITCH = 1.25  # line pitch over OCR box height (27.5 px pitch over 22 px boxes measured)


def _gray(jpeg: bytes | None, ocr: OcrResult) -> np.ndarray | None:
    if not jpeg:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg))  # reads the header only
        if img.size != (ocr.width, ocr.height) or ocr.width * ocr.height > MAX_FRAME_PIXELS:
            return None
        return np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        return None


def _divider(g: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> tuple[int, int, int] | None:
    """(row, left, right) of a divider under the heading's columns ``x0..x1``, or None."""
    h, w = g.shape
    lo, hi = DIVIDER
    for y in range(max(1, y0), min(h - 1, y1)):
        row = g[y]
        lit = (row >= lo) & (row <= hi)
        if not lit[x0:x1].all():
            continue
        # A thin line under black: the pane is black between the heading and the divider
        # (1-2 px thick after JPEG, so look 3 rows up). Not below: an entry scrolled up to
        # the divider sits right under it (real capture).
        if (g[max(0, y - 3), x0:x1] > PANE_BLACK).mean() > 0.2:
            continue
        left = x0
        while left > 0 and lit[left - 1]:
            left -= 1
        right = x1
        while right < w and lit[right]:
            right += 1
        return y, left, right
    return None


def _bottom(g: np.ndarray, top: int, left: int, right: int) -> int:
    """The last row of the pane: rows stay mostly black down to it."""
    dark = (g[top:, left:right] <= PANE_BLACK).mean(axis=1) >= DARK_ROW
    misses = 0
    bottom = top
    for i, ok in enumerate(dark):
        if ok:
            bottom, misses = top + i + 1, 0
        else:
            misses += 1
            if misses > ROW_GAP:
                break
    return bottom


def find_pane(ocr: OcrResult, jpeg: bytes | None) -> CaptionPane | None:
    """The live-captions pane of a full Teams frame, or None when none is on screen."""
    heads = [ln for ln in ocr.lines if norm_name(ln.text) == HEADING]
    if not heads:
        return None
    g = _gray(jpeg, ocr)
    if g is None:
        return None
    h, w = g.shape
    for head in sorted(heads, key=lambda ln: ln.box[1]):
        hh = max(1.0, head.height)
        x0, x1 = max(0, int(head.box[0])), min(w, int(head.box[2]))
        if x1 <= x0:
            continue
        y0 = int(head.box[3])
        found = _divider(g, x0, x1, y0, int(y0 + DIVIDER_BELOW * hh))
        if found is None:
            continue
        line, left, right = found
        top = line + 2
        bottom = _bottom(g, top, left, right)
        if bottom - top < MIN_HEIGHT * hh:
            continue
        inside = [
            ln.height
            for ln in ocr.lines
            if ln is not head and left <= ln.cx <= right and top <= ln.cy <= bottom
        ]
        line_h = sorted(inside)[len(inside) // 2] if inside else hh
        crop = min(bottom - top, CROP_PITCHES * PITCH * line_h)
        return CaptionPane(box=(left / w, top / h, right / w, bottom / h), crop=crop / h)
    return None
