"""Synthetic remote-desktop frames with a Microsoft Teams window floating in them (XERK-2608).

Levels and layout follow real captures (new Teams, dark theme, docked right caption pane):
stage ~31, pane 0 with a "Live Captions" heading and a 1 px divider of luma ~82 under it
spanning the pane, entries of avatar + small bold name + text. No real capture is committed
(public repo), and names and text here are fictional.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

DESKTOP = (37, 37, 38)
STAGE = (31, 31, 31)
PANE = (0, 0, 0)
DIVIDER = (82, 82, 82)
WHITE = (255, 255, 255)

ENTRIES = [
    ("Sean O'Neil", ["The build went green after the second retry, so", "I merged it."]),
    ("Raman, Priya", ["Thanks, that's great."]),
    ("Kunal Shah", ["Can we move the review to Thursday afternoon?"]),
    ("Raman, Priya", ["Thursday works, I'll update the invite."]),
    ("Kunal Shah", ["Next item is the release."]),
]


def _font(px: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=max(6, round(px)))


def make_desktop(
    size: tuple[int, int] = (2560, 1600),
    window: tuple[int, int, int, int] = (300, 200, 1900, 1400),
    ui: float = 1.0,
    captions: list[tuple[str, list[str]]] | None = None,
    pane: bool = True,
) -> tuple[Image.Image, tuple[tuple[float, float, float, float], float] | None]:
    """A remote desktop with a Teams window at ``window``; returns it and the pane's region
    as the api would send it (box below the divider, crop height), None without a pane.

    ``captions`` None shows ENTRIES, [] an open, empty pane.
    """
    w, h = size
    img = Image.new("RGB", size, DESKTOP)
    d = ImageDraw.Draw(img)
    for i in range(0, h, round(30 * ui)):  # an app behind Teams: rows of text
        d.text(
            (20, i),
            f"Backlog item {i} assigned to Someone",
            fill=(200, 200, 200),
            font=_font(11 * ui),
        )
    wx0, wy0, wx1, wy1 = window
    d.rectangle(window, fill=STAGE)
    d.text((wx0 + 16 * ui, wy0 + 10 * ui), "Sprint Planning", fill=WHITE, font=_font(10 * ui))
    stage_top = wy0 + round(80 * ui)
    if not pane:
        return img, None
    px0 = wx1 - round(400 * ui)
    d.rectangle((px0, stage_top, wx1 - 2, wy1 - 2), fill=PANE)
    d.text((px0 + 24 * ui, stage_top + 28 * ui), "Live Captions", fill=WHITE, font=_font(15 * ui))
    div = stage_top + round(74 * ui)
    d.line((px0, div, wx1 - 2, div), fill=DIVIDER, width=1)
    entries = ENTRIES if captions is None else captions
    # Entries go on their own canvas, pasted under the divider: a pane that overflows
    # scrolls, so its newest entry ends at the bottom and the oldest is cut by the divider.
    cw, ch = wx1 - 2 - px0, wy1 - 2 - (div + 2)
    line_h, name_h, gap = round(27 * ui), round(20 * ui), round(8 * ui)
    heights = [name_h + line_h * len(lines) + gap for _, lines in entries]
    y = round(8 * ui) if sum(heights) <= ch else ch - sum(heights)
    canvas = Image.new("RGB", (cw, ch), PANE)
    c = ImageDraw.Draw(canvas)
    av = round(40 * ui)
    for (name, lines), eh in zip(entries, heights, strict=True):
        c.ellipse((round(10 * ui), y, round(10 * ui) + av, y + av), fill=(120, 150, 170))
        c.text((round(64 * ui), y), name, fill=WHITE, font=_font(11 * ui))
        for i, text in enumerate(lines):
            c.text((round(64 * ui), y + name_h + i * line_h), text, fill=WHITE, font=_font(15 * ui))
        y += eh
    img.paste(canvas, (px0, div + 2))
    box = (px0 / w, (div + 2) / h, (wx1 - 2) / w, (wy1 - 2) / h)
    return img, (box, 7 * 1.25 * round(22 * ui) / h)
