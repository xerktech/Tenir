"""Zoom's caption panel layout, as seen on a real meeting-PC capture (2026).

The real screenshot shows real people, so it stays out of this public repo; this draws the
same layout: a shared screen above, then a dark panel with an avatar, the speaker's name on
its own dim line, and the caption text below it.
"""

from __future__ import annotations

import numpy as np
from caption_synth import _font
from PIL import Image, ImageDraw

from tenir_capture.caption_box import crop_band, crop_box, find_caption_box, upscale_to

LINES = [
    "So, uh, the release went out on friday, and the rollout to the second region",
    "took about an hour longer than we planned because of the cache warmup",
    "then the rollout went fine.",
]


def panel_frame(name: bool = True) -> Image.Image:
    img = Image.new("RGB", (1772, 1313), (21, 21, 21))
    d = ImageDraw.Draw(img)
    d.rectangle((140, 40, 1630, 1000), fill=(190, 240, 215))  # the shared screen
    d.text((442, 1052), "The build is green.", font=_font(22), fill=(255, 255, 255))
    d.ellipse((400, 1090, 426, 1116), fill=(170, 150, 130))  # the speaker's avatar
    if name:
        d.text((442, 1088), "Bo Chen", font=_font(16), fill=(154, 154, 154))
    for i, text in enumerate(LINES):
        d.text((442, 1110 + 31 * i), text, font=_font(22), fill=(255, 255, 255))
    return img


def box_of(img: Image.Image) -> tuple[int, int, int, int]:
    box = find_caption_box(upscale_to(crop_band(img), img.height))
    assert box is not None
    return box


def test_the_crop_keeps_the_speaker_line_of_zooms_caption_panel() -> None:
    """The avatar fused the name into the text block, the finder stopped below the name, and
    every caption reached the server with no speaker."""
    img = panel_frame()
    box = box_of(img)
    crop = crop_box(img)
    assert crop is not None and crop.height > box[3] - box[1]
    g = np.asarray(crop.convert("L"))
    # The grey name glyphs are at the top of the crop; the older white line above is not.
    grey_rows = [y for y in range(g.shape[0]) if ((g[y] > 140) & (g[y] < 170)).sum() > 5]
    assert grey_rows and grey_rows[0] < 20
    assert not (g[:6] > 240).any()
    assert crop.height < 200


def test_without_a_name_line_the_crop_is_the_box_alone() -> None:
    img = panel_frame(name=False)
    box = box_of(img)
    crop = crop_box(img)
    assert crop is not None
    assert crop.size == (box[2] - box[0], box[3] - box[1])
