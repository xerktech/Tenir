"""Where the Teams live-captions pane sits in a full frame (XERK-2608).

The capture client records a whole remote-desktop window, and Teams floats anywhere inside
it, so the client can't find the captions by window geometry. The api finds them here, on
each Teams full frame, and tells the client which box to crop (``ScreenRegion``, contract
``screen.region``).

Measured on real captures (new Teams, dark theme, docked right pane and the popped-out
captions window, 100% and 125% scaling, inside a remote desktop):

- under the pane's heading a 1 px divider (luma 70-82, black just above it) spans the
  pane's exact width; the divider gives the pane's left and right edges;
- the heading reads "Live Captions";
- below the divider the pane is pure black (0) down to its bottom, and its left or right
  margin stays black on every row (avatars and text sit inside the margins).

The divider is found in the pixels first, and only the strip above it is OCR'd to confirm
the heading: the full frame's own OCR misses the heading when Teams floats over a
text-dense desktop (every strip comes back too dense, QA on a real capture), while the
strip above a divider is a few words.

The bottom caption panel Teams also offers has no heading and no divider and is not found:
the operator docks captions on the right (docs/meeting-captions.md).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np

from api.screen.ocr import MAX_FRAME_PIXELS, OcrResult
from api.screen.zoom import CaptionPane, norm_name

HEADING = "live captions"
PANE_BLACK = 12  # the pane is 0; JPEG noise around glyphs stays under this
# The divider: brighter than the stage (~31), darker than glyphs (luma 70-82 measured, JPEG
# dips it to 45): the stage beside the pane at the divider's row must not extend it.
DIVIDER = (45, 140)
DIVIDER_ABOVE = 3  # rows above the divider that are black (the heading's bottom margin)
ABOVE_BLACK = 24  # ... up to JPEG ringing off the divider (14-27 measured on real frames)
DIVIDER_GAP = 6  # px of ringing a divider run bridges
DIVIDER_FILL = 0.9  # share of a bridged run that is divider
SPLIT_ROW = 15  # each half of a divider split over two rows is at least this bright
MIN_WIDTH = 150  # px: a pane is ~300 px wide even in a small remote window
MAX_CANDIDATES = 4  # dividers OCR'd per frame: each costs one small OCR request
# Strip above the divider read for the heading, as a share of the pane's width (the heading
# sits 23-45 px above the divider at 100-125% on a ~400-730 px pane).
BAND = 0.3
BAND_PX = (60, 200)
# A pane row: its left or right margin strip is at least this black. Real panes at JPEG
# quality 60 measured >= 0.68 on one side on every row; the content below a pane <= 0.14.
MARGIN_DARK = 0.6
MARGIN_LEFT = (0.015, 4)  # share of the pane's width, minimum px (avatars start ~11 px in)
MARGIN_RIGHT = (0.04, 6)  # text wraps ~30 px before the right edge
ROW_GAP = 3  # rows failing both margins tolerated inside the pane
MIN_HEIGHT = 80  # px under the divider: shorter is not the pane
# How much of the pane above its lowest text the client sends, in heading heights: real
# caption lines are ~0.65 of the heading's OCR box (23 px under a 36 px heading), so 6
# heading heights hold ~7 caption lines, at most ~4 entries of 3 boxes each, under
# tenir-ocr's 16-box cap (too_dense).
CROP_HEADINGS = 6


@dataclass(frozen=True)
class Candidate:
    """A divider-shaped line and the pane it would head, in frame pixels."""

    left: int
    top: int  # first row under the divider
    right: int
    bottom: int
    band: tuple[int, int, int, int]  # the strip above the divider to OCR for the heading
    band_jpeg: bytes


def _gray(jpeg: bytes | None, size: tuple[int, int]) -> np.ndarray | None:
    if not jpeg:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg))  # reads the header only
        if img.size != size or size[0] * size[1] > MAX_FRAME_PIXELS:
            return None
        return np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        return None


def _runs(flags: np.ndarray, min_len: int, gap: int = 0) -> list[tuple[int, int]]:
    """[start, end) runs of True at least ``min_len`` long, bridging ``gap`` False values."""
    idx = np.flatnonzero(flags)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]])) + 1
    return [(int(a), int(b)) for a, b in zip(starts, ends, strict=True) if b - a >= min_len]


def _bottom(g: np.ndarray, top: int, left: int, right: int) -> int:
    """The pane's last row: one of its margins stays black down to it."""
    w = right - left
    ml = max(MARGIN_LEFT[1], int(MARGIN_LEFT[0] * w))
    mr = max(MARGIN_RIGHT[1], int(MARGIN_RIGHT[0] * w))
    body = g[top:] <= PANE_BLACK
    ok = (body[:, left + 2 : left + 2 + ml].mean(axis=1) >= MARGIN_DARK) | (
        body[:, right - mr : right - 2].mean(axis=1) >= MARGIN_DARK
    )
    misses, bottom = 0, top
    for i, row_ok in enumerate(ok):
        if row_ok:
            bottom, misses = top + i + 1, 0
        else:
            misses += 1
            if misses > ROW_GAP:
                break
    return bottom


def candidates(jpeg: bytes | None, size: tuple[int, int]) -> list[Candidate]:
    """Divider-shaped lines that head a black pane, widest first, at most MAX_CANDIDATES."""
    g = _gray(jpeg, size)
    if g is None:
        return []
    from PIL import Image

    h, _ = g.shape
    lo, hi = DIVIDER
    k = DIVIDER_ABOVE
    a = g.astype(np.int16)
    one = (a[k:-1] >= lo) & (a[k:-1] <= hi)
    # A window the remote session or the client scaled down splits the 1 px divider over
    # two rows (25 + 54, 37 + 41 measured at 0.79x): their sum is the line.
    pair = a[k:-1] + a[k + 1 :]
    split = (a[k:-1] >= SPLIT_ROW) & (a[k + 1 :] >= SPLIT_ROW) & (pair >= lo) & (pair <= hi)
    # (A black area over a grey one passes here too; the pane check below rejects it: its
    # margins aren't black under the line.)
    line = one | split
    for i in range(1, k + 1):
        line &= a[k - i : h - 1 - i] <= ABOVE_BLACK  # black above it: row r is frame row r + k
    found: list[Candidate] = []
    img = Image.fromarray(g)
    for r in np.flatnonzero(line.sum(axis=1) >= MIN_WIDTH * DIVIDER_FILL):
        if len(found) >= 4 * MAX_CANDIDATES:
            break  # a crafted frame of many lines: bound the work
        y = int(r) + k
        for left, right in _runs(line[r], MIN_WIDTH, DIVIDER_GAP):
            if line[r, left:right].mean() < DIVIDER_FILL:
                continue
            if any(abs(y + 2 - c.top) <= 3 and abs(left - c.left) <= 3 for c in found):
                continue  # a 2 px divider after JPEG: the same pane's next row
            top = y + 2
            bottom = _bottom(g, top, left, right)
            if bottom - top < MIN_HEIGHT:
                continue
            band_h = int(min(max(BAND * (right - left), BAND_PX[0]), BAND_PX[1]))
            band = (left, max(0, y - band_h), right, y)
            buf = io.BytesIO()
            img.crop(band).save(buf, format="JPEG", quality=90)
            found.append(Candidate(left, top, right, bottom, band, buf.getvalue()))
    return sorted(found, key=lambda c: (c.left - c.right, c.top))[:MAX_CANDIDATES]


def pane_of(c: Candidate, band_ocr: OcrResult, size: tuple[int, int]) -> CaptionPane | None:
    """The pane ``c`` heads when the strip above its divider reads the heading, else None."""
    heads = [ln for ln in band_ocr.lines if norm_name(ln.text) == HEADING]
    if not heads:
        return None
    w, h = size
    heading_h = max(ln.height for ln in heads)
    crop = min(c.bottom - c.top, CROP_HEADINGS * heading_h)
    if crop <= 0:
        return None
    return CaptionPane(box=(c.left / w, c.top / h, c.right / w, c.bottom / h), crop=crop / h)
