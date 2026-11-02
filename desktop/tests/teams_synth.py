"""Synthetic Microsoft Teams meeting windows for the caption-panel finder (XERK-2608).

Geometry and grey levels follow one real capture (new Teams, dark theme, 150% scaling, a
2475x1551 window); no real capture is committed (public repo). Fractions of the window are
kept, and UI sizes scale with ``ui`` (1.5 = the capture's 150% display scaling).
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

STAGE = (31, 31, 31)
TOOLBAR = (41, 41, 41)
TILE = (10, 10, 10)
PANEL = (0, 0, 0)
WHITE = (255, 255, 255)

# Fictional speakers and lines (the docs' usual cast); never copy a real capture's text.
ENTRIES = [
    (
        "Sean O'Neil",
        ["The build went green after the second retry, so", "I merged it this morning."],
    ),
    ("Sean O'Neil", ["Nothing else to report."]),
    ("Raman, Priya", ["Thanks, that's great."]),
    ("Kunal Shah", ["Can we move the review to Thursday afternoon?"]),
    ("Kunal Shah", ["Wednesday is fully booked for me."]),
    ("Raman, Priya", ["Thursday works, I'll update the invite."]),
    ("Raman, Priya", ["Next item is the release"]),
]


def _font(px: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=max(6, round(px)))


def make_teams_window(
    size: tuple[int, int] = (2475, 1551),
    ui: float = 1.5,
    captions: list[tuple[str, list[str]]] | None = None,
    panel: bool = True,
    stage: str = "tiles",
) -> Image.Image:
    """A Teams meeting window; ``captions`` None shows ENTRIES, [] an empty open panel.

    ``panel=False`` is captions turned off. ``stage`` is "tiles" (camera-off grid with name
    labels) or "share" (a white document with dark text filling the stage).
    """
    w, h = size
    img = Image.new("RGB", size, STAGE)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, round(48 * ui)), fill=TOOLBAR)
    d.text((round(30 * ui), round(10 * ui)), "Sprint Planning", fill=WHITE, font=_font(9 * ui))
    stage_bottom = round(h * 0.70) if panel else h - 4
    top = round(64 * ui)
    if stage == "share":
        d.rectangle((4, top, w - 5, stage_bottom - 8), fill=WHITE)
        y = top + 20
        while y < stage_bottom - 40:
            d.text(
                (40, y),
                "Quarterly roadmap: milestones and owners",
                fill=(20, 20, 20),
                font=_font(12 * ui),
            )
            y += round(24 * ui)
    else:
        cols, rows = 5, 3
        tw, th = (w - 8) / cols, (stage_bottom - top - 8) / rows
        for r in range(rows):
            for c in range(cols):
                x0, y0 = 4 + c * tw, top + r * th
                d.rectangle((x0 + 2, y0 + 2, x0 + tw - 2, y0 + th - 2), fill=TILE)
                d.text(
                    (x0 + 10 * ui, y0 + th - 20 * ui),
                    f"Person {r}{c}",
                    fill=WHITE,
                    font=_font(9 * ui),
                )
    if not panel:
        return img
    p_top, p_bottom = round(h * 0.72), h - 5
    # The panel is drawn on its own canvas so the oldest entry is cut by its top edge.
    pw, ph = w - 9, p_bottom - p_top
    pan = Image.new("RGB", (pw, ph), PANEL)
    d = ImageDraw.Draw(pan)
    d.line((pw - 40 * ui, 12 * ui, pw - 30 * ui, 22 * ui), fill=WHITE, width=2)
    d.line((pw - 40 * ui, 22 * ui, pw - 30 * ui, 12 * ui), fill=WHITE, width=2)
    entries = ENTRIES if captions is None else captions
    # Laid out bottom-up, newest last; the oldest scroll off the top.
    x_av, x_text = round(w * 0.35), round(w * 0.35 + 37 * ui)
    name_f, text_f = _font(9 * ui), _font(13 * ui)
    y = ph - round(8 * ui)
    for name, lines in reversed(entries):
        y0 = y - round(13 * ui) - round(19 * ui) * len(lines)
        if y < 0:
            break
        av = round(26 * ui)
        d.ellipse((x_av, y0, x_av + av, y0 + av), fill=(120, 150, 170))
        d.text((x_text, y0), name, fill=WHITE, font=name_f)
        for i, line in enumerate(lines):
            ty = y0 + round(13 * ui) + i * round(19 * ui)
            d.text((x_text, ty), line, fill=WHITE, font=text_f)
        y = y0 - round(9 * ui)
    img.paste(pan, (4, p_top))
    return img
