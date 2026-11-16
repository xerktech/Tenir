"""Zoom's caption panel layout, as seen on a real meeting-PC capture (2026).

The real screenshot shows real people, so it stays out of this public repo; this draws the
same layout: a shared screen above, then a dark panel with an avatar, the speaker's name on
its own dim line, and the caption text below it.
"""

from __future__ import annotations

import numpy as np
from caption_synth import _font
from PIL import Image, ImageDraw

from tenir_capture.caption_box import (
    AVATAR_ROOM,
    crop_band,
    crop_box,
    find_caption_box,
    upscale_to,
)
from tenir_capture.caption_pane import WindowCrop

LINES = [
    "So, uh, the release went out on friday, and the rollout to the second region",
    "took about an hour longer than we planned because of the cache warmup",
    "then the rollout went fine.",
]


def panel_frame(name: bool = True, avatar: tuple[int, int, int] = (170, 150, 130)) -> Image.Image:
    img = Image.new("RGB", (1772, 1313), (21, 21, 21))
    d = ImageDraw.Draw(img)
    d.rectangle((140, 40, 1630, 1000), fill=(190, 240, 215))  # the shared screen
    d.text((442, 1052), "The build is green.", font=_font(22), fill=(255, 255, 255))
    d.ellipse((400, 1090, 426, 1116), fill=avatar)  # the speaker's avatar
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
    assert crop.size == (box[2] - box[0] + AVATAR_ROOM, box[3] - box[1])


def test_the_crop_keeps_a_dark_avatar_left_of_the_name() -> None:
    """XERK-2700: the api tells a speaker from a docs page's grey line by the avatar beside the
    name. The box was cut at the glyphs, so a dark circle (Zoom's blue) never reached it."""
    img = panel_frame(avatar=(14, 113, 235))
    crop = crop_box(img)
    assert crop is not None
    px = np.asarray(crop.convert("RGB"), dtype=np.int16)
    blue = (np.abs(px - (14, 113, 235)).max(axis=2) < 30).sum(axis=0)
    name_left = int(np.flatnonzero(np.asarray(crop.convert("L"))[:20].max(axis=0) > 120)[0])
    # The whole circle is in the crop, left of the name line, with no ink cut at its edge.
    assert blue.sum() > 300 and int(np.flatnonzero(blue)[-1]) < name_left
    assert blue[0] == 0


def test_the_avatar_room_stops_at_the_frame_s_left_edge() -> None:
    """A caption box starting nearer the frame's left edge than AVATAR_ROOM keeps what is
    there, never a strip of padding past the edge."""
    img = Image.new("RGB", (640, 1313), (21, 21, 21))
    d = ImageDraw.Draw(img)
    d.text(
        (12, 1110),
        "so the release went out on friday and the rollout",
        font=_font(22),
        fill="white",
    )
    band = upscale_to(crop_band(img), img.height)
    box = find_caption_box(band)
    assert box is not None and box[0] < AVATAR_ROOM
    crop = crop_box(img)
    assert crop is not None and crop.width == box[2]


def remote_desktop(zoom: Image.Image, at: tuple[int, int]) -> Image.Image:
    """A Horizon desktop (3075x2100, as the operator's) with Zoom floating at ``at`` and a
    browser whose dark sidebar holds white UI text across the desktop's centre column."""
    desk = Image.new("RGB", (3075, 2100), (236, 236, 236))
    d = ImageDraw.Draw(desk)
    d.rectangle((1380, 1300, 1700, 2060), fill=(32, 32, 36))  # the browser's sidebar
    d.text((1420, 1640), "Management", font=_font(22), fill=(255, 255, 255))
    d.text((1420, 1700), "Collapse sidebar", font=_font(22), fill=(255, 255, 255))
    desk.paste(zoom, at)
    return desk


AT = (40, 760)  # Zoom on the left: the desktop's centre column runs through the sidebar
PANEL = (340, 1040, 1330, 1230)  # as the api boxes it: ~3 rows left of the text, 1 right
GROUND = (21, 21, 21)


def located(at: tuple[int, int] = AT, ground: tuple[int, int, int] | None = GROUND) -> WindowCrop:
    """A Zoom window's finder told where the panel is, as the api's screen.region does."""
    x0, y0, x1, y1 = (v + at[i % 2] for i, v in enumerate(PANEL))
    finder = WindowCrop()
    finder.locate(((x0 / 3075, y0 / 2100, x1 / 3075, y1 / 2100), None), ground)
    return finder


def test_a_zoom_window_floating_in_a_remote_desktop_is_cropped_where_the_api_located_it() -> None:
    """XERK-3027: the finder looked for captions crossing the whole desktop's centre, never
    found Zoom's off-centre panel, and cropped the browser's sidebar text instead."""
    zoom = panel_frame()
    desk = remote_desktop(zoom, AT)
    assert crop_box(desk) is None  # the old whole-window finder: Zoom's panel is off-centre
    assert WindowCrop()(desk) is None  # no region from the api: nothing is sent
    crop, want = located()(desk), crop_box(zoom)
    assert crop is not None and want is not None
    # The same crop as of Zoom alone: avatar, name line and text, at the desktop's scale.
    assert crop.size == want.size
    assert np.array_equal(np.asarray(crop), np.asarray(want))


def test_a_located_panel_with_no_captions_sends_nothing() -> None:
    blank = Image.new("RGB", (1772, 1313), GROUND)
    assert located()(remote_desktop(blank, AT)) is None


def test_a_window_moved_over_the_panel_is_never_cropped() -> None:
    """Real frames: Slack brought over Zoom left white text in the stale box, and a live
    meeting would have stored it. The box must still show the panel's ground colour."""
    desk = remote_desktop(panel_frame(), AT)
    slack = panel_frame()
    slack.paste((26, 29, 33), (0, 1000, slack.width, slack.height), mask=None)
    d = ImageDraw.Draw(slack)
    d.text((442, 1110), "@channel working session link: zoom.us/j/86479", font=_font(22))
    d.text((442, 1141), "Last reply 4 days ago, can you check the route", font=_font(22))
    covered = remote_desktop(slack, AT)
    assert located()(desk) is not None
    assert located()(covered) is None
    assert located(ground=(13, 16, 23))(desk) is None  # another dark ground: not this panel
