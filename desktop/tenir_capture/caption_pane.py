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

from tenir_capture.caption_box import crop_caption

Box = tuple[float, float, float, float]
# (left, top, right, bottom) and cropHeight, shares of the frame. Zoom's region has no
# cropHeight: its box is the caption panel, searched by the caption-box finder.
Region = tuple[Box, float | None]
RGB = tuple[int, int, int]

# Zoom's panel ground (XERK-3027): the box must still show the colour the api measured on
# at least this share of its pixels, each channel within GROUND_TOL. Real panels: 35-42% exact
# on JPEG frames; Slack (8 levels off), GitHub (5), Explorer (7) and Azure (9) dark themes,
# moved over the panel's box on real frames, share none of it.
GROUND_TOL = 3
GROUND_SHARE = 0.2

PANE_BLACK = 12  # the pane is pure black (0); JPEG noise around glyphs stays under this
# A box that is no longer mostly black isn't the pane any more (it closed or moved and the
# api hasn't said yet): send nothing rather than whatever is there now.
PANE_DARK = 0.5
GLYPH = 120  # glyph cores
INK = 40  # anything on a row at all (glyph edges, avatars)
MIN_PX = 8  # a box smaller than this is no pane
PAD = 6  # px kept under the lowest text
# The pane's margins hold no text (the api finds its bottom by them): text starts 11-16% in
# (after the avatars), and wraps ~30 px before the right edge, where the scrollbar's thumb
# shows. The left margin also skips a window's rounded corner just outside a box placed a
# few px wide (real capture at 0.6x, QA).
MARGIN_LEFT = 0.08
MARGIN_RIGHT = 0.04
MARGIN_RIGHT_MIN = 6
MAX_PIXELS = 480_000  # under tenir-ocr's 0.5 Mpx cap (api STRIP_PIXELS)


class PaneCrop:
    """A Teams window's caption finder: crops the pane at the box the api last sent."""

    def __init__(self) -> None:
        self.region: Region | None = None  # set from the receiver thread; read whole

    def locate(self, region: Region | None, ground: RGB | None = None) -> None:
        """Where the api last said the captions are (screen.region); None: gone."""
        self.region = region

    def __call__(self, img: Image.Image) -> Image.Image | None:
        return crop_pane(img, self.region)


class WindowCrop(PaneCrop):
    """A Zoom window's caption finder (XERK-3027): the caption-box finder, run only inside
    the caption panel the api located on a full frame and sent as ``screen.region``.

    The captured window may be a whole remote desktop with Zoom floating in it, and the
    finder looks for a caption crossing its image's centre column: run on the desktop, it
    never found Zoom's off-centre panel and cropped whatever UI text crossed the desktop's
    centre. Inside the panel it runs at the desktop's own scale, as it did when Zoom's panel
    happened to cross the centre. Without a region nothing is sent (the operator's call: no
    whole-window fallback), nor while the box no longer shows the panel's ground (another
    window over Zoom, or Zoom moved, until the api's next full frame says where it went).
    """

    def __init__(self) -> None:
        super().__init__()
        self.panel: tuple[Box, RGB | None] | None = None  # set from the receiver thread

    def locate(self, region: Region | None, ground: RGB | None = None) -> None:
        self.region = region
        self.panel = None if region is None else (region[0], ground)

    def __call__(self, img: Image.Image) -> Image.Image | None:
        located = self.panel
        if located is None:
            return None
        box, ground = located
        panel = box_of(img, box)
        if panel is None or (ground is not None and not shows_ground(panel, ground)):
            return None
        return crop_caption(panel, img.height)


def shows_ground(img: Image.Image, ground: RGB) -> bool:
    """At least GROUND_SHARE of ``img``'s pixels are the panel's ground colour."""
    px = np.asarray(img.convert("RGB"), dtype=np.int16)[::2, ::2]
    near = (np.abs(px - np.asarray(ground, dtype=np.int16)) <= GROUND_TOL).all(axis=2)
    return bool(near.mean() >= GROUND_SHARE)


def box_of(img: Image.Image, box: Box) -> Image.Image | None:
    """``img`` cropped to ``box`` (shares of its size), or None when that is too small."""
    left, top, right, bottom = box
    w, h = img.size
    x0, y0 = max(0, int(left * w)), max(0, int(top * h))
    x1, y1 = min(w, math.ceil(right * w)), min(h, math.ceil(bottom * h))
    if x1 - x0 < MIN_PX or y1 - y0 < MIN_PX:
        return None
    return img.crop((x0, y0, x1, y1))


def crop_pane(img: Image.Image, region: Region | None) -> Image.Image | None:
    """The newest entries of the caption pane at ``region``, or None without any."""
    if region is None or region[1] is None:
        return None
    box, share = region
    h = img.height
    pane = box_of(img, box)
    if pane is None:
        return None
    g = np.asarray(ImageOps.grayscale(pane), dtype=np.uint8)
    if (g <= PANE_BLACK).mean() < PANE_DARK:
        return None
    # Text is looked for inside the pane's margins: a window border or corner the box caught
    # at its edge, or the scrollbar's thumb in the right margin, made the lowest "text" the
    # pane's blank bottom and every crop empty (QA).
    lines = (g > GLYPH)[
        :,
        int(MARGIN_LEFT * g.shape[1]) : g.shape[1]
        - max(MARGIN_RIGHT_MIN, int(MARGIN_RIGHT * g.shape[1])),
    ]
    rows = np.flatnonzero(lines.any(axis=1))
    if rows.size == 0:
        return None  # an empty pane
    last = int(rows[-1]) + 1
    start = max(0, last - int(share * h))
    ink = (g > INK).any(axis=1)  # every column: skipping some moved real crops off their header
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
