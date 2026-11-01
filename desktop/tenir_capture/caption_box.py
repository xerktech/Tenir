"""Zoom caption-box finder (XERK-2504), shared by the capture client and the spike harness.

The capture client crops each frame to Zoom's caption box and sends nothing when there is none
(docs/meeting-captions.md). ``scripts/caption_ocr/caption_bench.py`` imports this module to
benchmark and tune the same code, so a threshold change is measured where it ships.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps

# Zoom draws captions in the lower part of the meeting window; the band keeps them with margin.
BAND_TOP = 0.62
BAND_BOTTOM = 0.97
# Upscale the band until the frame is this tall in "window pixels" (1080p caption font size).
TARGET_HEIGHT = 1080
# Caption-box finder (find_caption_box), in pixels at TARGET_HEIGHT scale.
BOX_DARK = 80  # box fill is ~73% opaque black: even over white video it stays below this
BOX_WHITE = 110  # glyph pixels: white text dulls to ~180 after a 0.5x remote downscale
GLYPH_REACH = 4  # a glyph pixel has a dark pixel within this many px (white text on the dark box)
CENTER_HALF = 40  # half-width of the centre window the caption must cross on both sides
BOX_LINE_GAP = 32  # glyph-row gap still inside one caption (line spacing)
BOX_WORD_GAP = 80  # glyph-column gap still inside one caption line (word spacing)
BOX_MIN_HEIGHT = 10  # shorter glyph runs are UI chrome; a blurred 0.5x caption line is ~13
BOX_PAD = 8  # margin kept around the glyphs
OUTLINE_INSET = 10  # px the box outline can light inside a span (text starts ~14 px in)
BOX_MIN_WIDTH = 80  # the shortest caption ("Omar: ok") is wider than this
BOX_CENTRE_TOL = 0.08  # the box is centred: its middle within this share of the width
BOX_MAX_HEIGHT = 160  # Zoom shows at most 3 caption lines (~36 px each)
BOX_FILL_SHARE = 0.5  # share of dark pixels in a caption box (fill ~73% black, glyphs white)
BOX_TRIM_SHARE = 0.2  # edge rows darker than this are kept as caption rows
FILL_DARK = 90  # grey counted as box fill: <= ~72 over any video, plus JPEG noise and blur
FILL_MARGIN = 12  # px of box padding (Zoom pads ~14 px) included in the fill check
EDGE_SHARE = 0.5  # a column/row lit along more than this share of the band is an edge, not text
# Tallest glyph line that is caption text: Zoom's default caption font measures <= 23 px here,
# a camera-off name centred in its tile 29-30 px. Retune if the operator enlarges captions.
BOX_MAX_LINE = 26
LINE_SPLIT_GAP = 2  # rows of no ink that separate two text lines
LINE_MIN_ROWS = 5  # shorter glyph runs aren't text lines (a blurred 0.5x line is ~13 rows)


def crop_band(img: Image.Image, top: float = BAND_TOP, bottom: float = BAND_BOTTOM) -> Image.Image:
    w, h = img.size
    return img.crop((0, int(h * top), w, int(h * bottom)))


def upscale_to(img: Image.Image, frame_h: int, target_h: int = TARGET_HEIGHT) -> Image.Image:
    """Upscale a crop by target_h/frame_h so small remote windows reach caption-font size."""
    factor = target_h / frame_h
    if factor <= 1.05:
        return img
    return img.resize(
        (round(img.width * factor), round(img.height * factor)), Image.Resampling.LANCZOS
    )


def _runs(flags: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """[start, end) runs of True, merging runs separated by at most ``gap`` False values."""
    idx = np.flatnonzero(flags)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]])) + 1
    return list(zip(starts.tolist(), ends.tolist(), strict=False))


def glyph_mask(img: Image.Image) -> np.ndarray:
    """Bright pixels with a dark pixel within GLYPH_REACH px: white text on a dark fill.

    Bright video is excluded (its neighbours are bright too); so is a dark strip with no text.
    """
    g = np.asarray(ImageOps.grayscale(img), dtype=np.uint8)
    dark = g < BOX_DARK
    near_dark = dark.copy()
    for d in range(1, GLYPH_REACH + 1):
        near_dark[:, d:] |= dark[:, :-d]
        near_dark[:, :-d] |= dark[:, d:]
        near_dark[d:, :] |= dark[:-d, :]
        near_dark[:-d, :] |= dark[d:, :]
    return (g > BOX_WHITE) & near_dark


def find_caption_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Locate Zoom's caption box in a band crop: (left, top, right, bottom), or None.

    Zoom centres its caption box horizontally, so a caption's glyphs cross the frame's centre
    column with ink on both sides of it. Tile name labels (left-aligned in their tile) and dark
    toolbars fail that test, which is what lets this work on camera-off (dark) meetings where
    the box fill is no darker than the video. A camera-off name centred in its tile does pass it,
    so glyph lines taller than caption text are rejected, and of the remaining centred runs the
    lowest wins (the caption box sits below the tiles). The box then grows to the lines above and
    below within its columns: lines are left-aligned in the box, so a short one may miss the
    centre. None means no caption, so the capture client sends nothing.
    """
    m = glyph_mask(img)
    # Long edges (a tile gutter on the centre column, a tile's bottom edge over a dark strip)
    # light up most of a column or row; caption glyphs never do.
    m[:, m.mean(axis=0) > EDGE_SHARE] = False
    m[m.mean(axis=1) > EDGE_SHARE, :] = False
    h, w = m.shape
    c = w // 2
    left_ink = m[:, c - CENTER_HALF : c].any(axis=1)
    right_ink = m[:, c : c + CENTER_HALF].any(axis=1)
    centre_ink = left_ink | right_ink  # the box's own side edges stay out of line heights

    # Text lines crossing the centre; caption-sized ones close together form a candidate box,
    # and anything taller (a centred camera-off name, video texture) separates candidates.
    # Built bottom-up, capped at BOX_MAX_HEIGHT, so video texture above a caption can't swallow it.
    groups: list[list[int]] = []
    prev_top = None
    for a, b in reversed(_runs(centre_ink, LINE_SPLIT_GAP)):
        if b - a < LINE_MIN_ROWS:
            continue  # video speckle or the box's edge: bridging through it splits real captions
        if b - a > BOX_MAX_LINE:
            prev_top = None
            continue
        if (
            prev_top is not None
            and prev_top - b <= BOX_LINE_GAP
            and groups[-1][1] - a <= BOX_MAX_HEIGHT
        ):
            groups[-1][0] = a
        else:
            groups.append([a, b])
        prev_top = a
    candidates = [
        (top, bottom)
        for top, bottom in reversed(groups)
        if bottom - top >= BOX_MIN_HEIGHT
        and left_ink[top:bottom].any()
        and right_ink[top:bottom].any()
    ]
    if not candidates:
        return None

    def centre_span(top: int, bottom: int) -> tuple[int, int] | None:
        """Glyph columns (words bridged) containing the centre, if centred and wide enough."""
        cols = m[top:bottom].any(axis=0)
        span = next(((a, b) for a, b in _runs(cols, BOX_WORD_GAP) if a <= c < b), None)
        if span is None or span[1] - span[0] < BOX_MIN_WIDTH:
            return None
        return span if abs((span[0] + span[1]) / 2 - c) <= BOX_CENTRE_TOL * w else None

    # Box fill over white video is ~66 grey, so the fill test needs a looser bar than BOX_DARK.
    dark = np.asarray(ImageOps.grayscale(img), dtype=np.uint8) < FILL_DARK

    for top, bottom in reversed(candidates):  # lowest first: the caption sits below the tiles
        span = centre_span(top, bottom)
        if span is None:
            continue
        # Grow to caption lines above/below that sit in the box's columns but miss the centre,
        # one caption-sized line on box fill at a time; anything else (video, a tile name) ends
        # the box. Use columns just inside the span: over bright video the box's side outline
        # lights the mask on every box row and would fuse all lines into one too-tall run.
        lo = span[0] + OUTLINE_INSET
        inner = m[:, lo : max(lo + 1, span[1] - OUTLINE_INSET)]
        span_fill = dark[:, span[0] : span[1]].mean(axis=1)

        def caption_line(a: int, b: int, span_fill: np.ndarray = span_fill) -> bool:
            return b - a <= BOX_MAX_LINE and span_fill[a:b].mean() >= BOX_TRIM_SHARE

        inside = _runs(inner.any(axis=1), LINE_SPLIT_GAP)
        for a, b in reversed([r for r in inside if r[1] <= top]):
            if top - b > BOX_LINE_GAP or not caption_line(a, b) or bottom - a > BOX_MAX_HEIGHT:
                break
            top = a
        for a, b in [r for r in inside if r[0] >= bottom]:
            if a - bottom > BOX_LINE_GAP or not caption_line(a, b) or b - top > BOX_MAX_HEIGHT:
                break
            bottom = b
        left, right = centre_span(top, bottom) or span
        # The box edge over bright video lights the mask too; trim rows that are mostly video.
        # (Glyph rows of a blurred 0.5x caption are still >= ~30% fill; edge rows are less.)
        fill = dark[:, left:right].mean(axis=1)
        while bottom - top > BOX_MIN_HEIGHT and fill[top] < BOX_TRIM_SHARE:
            top += 1
        while bottom - top > BOX_MIN_HEIGHT and fill[bottom - 1] < BOX_TRIM_SHARE:
            bottom -= 1
        # Captions are white on a dark fill. Dark text on a white slide has a bright halo that
        # passes the glyph mask, but the region around it is light: not a caption box. Measure
        # with the box's own padding included (dark fill even where blurred glyphs make text
        # rows mostly light), leaving out rows that are plainly video beside the box; the kept
        # rows must still cover the text rows.
        rows = fill[max(0, top - FILL_MARGIN) : min(h, bottom + FILL_MARGIN)]
        rows = rows[rows >= BOX_TRIM_SHARE]
        if rows.size < bottom - top or rows.mean() < BOX_FILL_SHARE:
            continue
        return (
            max(0, left - BOX_PAD),
            max(0, top - BOX_PAD),
            min(w, right + BOX_PAD),
            min(h, bottom + BOX_PAD),
        )
    return None


def crop_box(img: Image.Image) -> Image.Image | None:
    """Tight crop to the caption box at caption-font scale; None when no caption is on screen."""
    # Find the box at caption-font scale so the pixel thresholds hold for small remote windows.
    band = upscale_to(crop_band(img), img.height)
    box = find_caption_box(band)
    return band.crop(box) if box else None
