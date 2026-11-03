"""Microsoft Teams caption crops from the pane the api located (XERK-2608).

The captured window is a whole remote desktop and Teams floats anywhere in it, so the client
can't find the Live Captions pane by geometry. The api finds it on the full frames it OCRs
(by its "Live Captions" heading) and sends its box as ``screen.region``; this module crops
caption frames from that box. Until a box arrives, or after the api says the pane is gone,
a Teams window sends only its periodic full frames.

Within the box the newest entries are the lowest text (a pane fills from the top, then
scrolls), so the crop ends at the lowest glyph row and reaches ``cropHeight`` of the frame
above it, opening on a blank row so it never starts on half a line.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageOps

Box = tuple[float, float, float, float]
Region = tuple[Box, float]  # (left, top, right, bottom) and cropHeight, shares of the frame

PANE_BLACK = 12  # the pane is pure black (0); JPEG noise around glyphs stays under this
# A box that is no longer mostly black isn't the pane any more (it closed or moved and the
# api hasn't said yet): send nothing rather than whatever is there now.
PANE_DARK = 0.5
GLYPH = 120  # glyph cores
INK = 40  # anything on a row at all (glyph edges, avatars)
MIN_PX = 8  # a box smaller than this is no pane
PAD = 6  # px kept under the lowest text
BORDER = 0.9  # a column lit on this share of the box's rows is a border (avatars: ~0.75)
# The pane's margins hold no text (the api finds its bottom by them): avatars start ~11 px
# in, text wraps ~30 px before the right edge, where the scrollbar's thumb shows.
MARGIN_LEFT = 0.01
MARGIN_RIGHT = 0.04
MARGIN_RIGHT_MIN = 6
MAX_PIXELS = 480_000  # under tenir-ocr's 0.5 Mpx cap (api STRIP_PIXELS)


class PaneCrop:
    """A Teams window's caption finder: crops the pane at the box the api last sent."""

    def __init__(self) -> None:
        self.region: Region | None = None  # set from the receiver thread; read whole

    def __call__(self, img: Image.Image) -> Image.Image | None:
        return crop_pane(img, self.region)


def crop_pane(img: Image.Image, region: Region | None) -> Image.Image | None:
    """The newest entries of the caption pane at ``region``, or None without any."""
    if region is None:
        return None
    (left, top, right, bottom), share = region
    w, h = img.size
    x0, y0 = max(0, int(left * w)), max(0, int(top * h))
    x1, y1 = min(w, math.ceil(right * w)), min(h, math.ceil(bottom * h))
    if x1 - x0 < MIN_PX or y1 - y0 < MIN_PX:
        return None
    pane = img.crop((x0, y0, x1, y1))
    g = np.asarray(ImageOps.grayscale(pane), dtype=np.uint8)
    if (g <= PANE_BLACK).mean() < PANE_DARK:
        return None
    # Text is looked for inside the pane's margins, in columns not lit down most of the box:
    # a window border the box caught at its edge, or the scrollbar's thumb in the right
    # margin, made the lowest "text" the pane's blank bottom and every crop empty (QA).
    inner = np.zeros(g.shape[1], dtype=bool)
    inner[
        int(MARGIN_LEFT * g.shape[1]) : g.shape[1]
        - max(MARGIN_RIGHT_MIN, int(MARGIN_RIGHT * g.shape[1]))
    ] = True
    lines = (g > GLYPH)[:, inner & ((g > GLYPH).mean(axis=0) < BORDER)]
    rows = np.flatnonzero(lines.any(axis=1))
    if rows.size == 0:
        return None  # an empty pane
    last = int(rows[-1]) + 1
    start = max(0, last - int(share * h))
    ink = (g > INK)[:, (g > INK).mean(axis=0) < BORDER].any(axis=1)  # any ink but borders
    open_at = start
    while open_at < last and ink[open_at]:
        open_at += 1  # move down to a blank row: a line cut by the crop's top is noise
    if open_at < last:
        start = open_at
    crop = pane.crop((0, start, pane.width, min(pane.height, last + PAD)))
    if crop.width * crop.height <= MAX_PIXELS:
        return crop
    # Floor, not round: rounding both sides up can overshoot the cap.
    scale = (MAX_PIXELS / (crop.width * crop.height)) ** 0.5
    size = (max(1, int(crop.width * scale)), max(1, int(crop.height * scale)))
    return crop.resize(size, Image.Resampling.LANCZOS)
