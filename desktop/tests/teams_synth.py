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
    pane: float = 0.0,
    mark: str = "",
) -> Image.Image:
    """A Teams meeting window; ``captions`` None shows ENTRIES, [] an empty open panel.

    ``panel=False`` is captions turned off. ``stage`` is "tiles" (camera-off grid with name
    labels), "share" (a white document filling the stage) or "black" (a black slide with white
    text filling the stage down to the window bottom). ``mark`` puts a slide number ("12"),
    a "dot" or a square "logo" in the stage's top-right corner, where the panel's X would be. ``pane`` is the width share of a Chat /
    People pane on the right; the stage and the caption panel narrow to the rest.
    """
    w, h = size
    img = Image.new("RGB", size, STAGE)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, round(48 * ui)), fill=TOOLBAR)
    d.text((round(30 * ui), round(10 * ui)), "Sprint Planning", fill=WHITE, font=_font(9 * ui))
    sw = round(w * (1 - pane))  # stage width
    if pane:
        d.rectangle((sw, round(48 * ui), w, h), fill=(45, 45, 45))
        for i in range(12):
            d.text(
                (sw + 16 * ui, 70 * ui + i * 30 * ui),
                f"Member {i}",
                fill=WHITE,
                font=_font(10 * ui),
            )
    stage_bottom = round(h * 0.70) if panel else h - 4
    top = round(64 * ui)
    if stage in ("share", "black"):
        page, ink = ((255, 255, 255), (20, 20, 20)) if stage == "share" else (PANEL, WHITE)
        d.rectangle((4, top, sw - 5, stage_bottom - (8 if panel else 0)), fill=page)
        y = top + 20
        while y < stage_bottom - 40 * ui:
            d.text(
                (40, y), "Quarterly roadmap: milestones and owners", fill=ink, font=_font(12 * ui)
            )
            y += round(24 * ui)
        mx, my = sw - 40 * ui, top + 30 * ui
        if mark == "dot":
            d.ellipse((mx, my, mx + 8 * ui, my + 8 * ui), fill=ink)
        elif mark == "logo":
            d.rectangle((mx, my, mx + 12 * ui, my + 12 * ui), outline=ink, width=2)
            d.line((mx, my + 6 * ui, mx + 12 * ui, my + 6 * ui), fill=ink, width=2)
        elif mark:
            d.text((mx, my), mark, fill=ink, font=_font(10 * ui))
    else:
        cols, rows = 5, 3
        tw, th = (sw - 8) / cols, (stage_bottom - top - 8) / rows
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
    pw, ph = sw - 9, p_bottom - p_top
    pan = Image.new("RGB", (pw, ph), PANEL)
    d = ImageDraw.Draw(pan)
    # The close X, where the real one sits: 13-23 px down, 18-28 px in from the right at 100%.
    x0, y0, x1, y1 = pw - 28 * ui, 13 * ui, pw - 18 * ui, 23 * ui
    d.line((x0, y0, x1, y1), fill=WHITE, width=max(1, round(1.5 * ui)))
    d.line((x0, y1, x1, y0), fill=WHITE, width=max(1, round(1.5 * ui)))
    entries = ENTRIES if captions is None else captions
    # Laid out bottom-up, newest last; the oldest scroll off the top.
    x_av, x_text = round(sw * 0.35), round(sw * 0.35 + 37 * ui)
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
