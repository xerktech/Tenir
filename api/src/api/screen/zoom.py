"""Zoom layout parser: OCR'd text -> participants, active speaker, title and caption blocks.

Two inputs (XERK-2505, hybrid wire format):

- A **full window frame**: participant names are the short name-shaped lines on the video tiles;
  the tile with Zoom's green active-speaker border names who is talking (a cross-check for the
  caption headers); the meeting title is the top-band line when it isn't Zoom's own chrome.
- A **caption-box crop**: blocks of a speaker header line (``Name`` on its own line, or a
  ``Name: text`` prefix) followed by text lines. A header is accepted as a speaker only if it
  matches a tile name; anything else is caption text.

Tuned on synthetic frames (the operator's screenshot wasn't available, docs/meeting-captions.md
Caveats): the geometry thresholds are deliberately loose and the roster match is what decides.
One real capture since (XERK-2643, a screen share under a strip of tiles) set the tile-gap check.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import numpy as np

from api.screen.ocr import MAX_FRAME_PIXELS, OcrLine, OcrResult

# Zoom chrome that OCR reads off a full frame and that is never a participant or a title.
_UI_TEXT = {
    "zoom",
    "zoom meeting",
    "zoom workplace",
    "meeting",
    "mute",
    "unmute",
    "stop video",
    "start video",
    "video",
    "audio",
    "participants",
    "chat",
    "share",
    "share screen",
    "record",
    "recording",
    "reactions",
    "apps",
    "more",
    "leave",
    "end",
    "security",
    "view",
    "speaker view",
    "gallery view",
    "show captions",
    "captions",
    "live transcript",
    "transcript",
    "summary",
    "whiteboards",
    "notes",
    "host tools",
    "ai companion",
    "talking",
    "talking:",
}
_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?( ?[ap]\.?m\.?)?$", re.IGNORECASE)
# Trailing "(Host)", "(me)", "(he/him)" etc. on a tile name.
_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")
_NAME_TOKEN = re.compile(r"^[^\W\d_][\w.'@+-]*$", re.UNICODE)
# Zoom's active-speaker border: a saturated green (#23D959-ish) ring around the tile.
_BORDER_MIN_PIXELS = 24
# Largest frame decoded for the border check: a 4K window (3840x2160) and a little more. A
# JPEG is small on the wire but huge decoded (13000x13000 in 2.6 MB is ~500 MB of RGB), so
# anything bigger, or not the size OCR read, is skipped rather than decoded.
_MAX_DECODE_PIXELS = MAX_FRAME_PIXELS
# The gap between Zoom's tiles and the share is near-black (luma ~5 on a real capture; the
# synthetic test windows use 26). Light shared content is ~255.
_GAP_MAX_LUMA = 48

# Zoom's meeting-toolbar buttons. Seeing several of them in the toolbar band means a meeting
# is on screen (a desktop's icon labels are name-shaped too, so tiles alone can't tell).
_MEETING_CONTROLS = {
    "mute",
    "unmute",
    "stop video",
    "start video",
    "participants",
    "chat",
    "share",
    "share screen",
    "reactions",
    "leave",
    "end",
}
_MIN_CONTROLS = 3
# Words of Zoom's toolbar labels as it draws them (Title Case). A caption crop low over the
# window catches the toolbar row ("Participants Chat React Share More", XERK-2693).
_TOOLBAR_WORDS = {w for label in _UI_TEXT | _MEETING_CONTROLS for w in label.split()} | {
    "react",
    "raise",
    "hand",
}

# Full-frame bands, as fractions of the frame height.
_TITLE_BAND = 0.07
_TOOLBAR_BAND = 0.90

# Zoom shows at most 49 tiles a page; a meeting's roster is bounded well above that so a
# crafted frame full of names can't make every header match a fuzzy scan of thousands.
_MAX_ROSTER = 200

# A header matches a roster name at this similarity (OCR slips on one or two characters).
_NAME_MATCH = 0.86


def norm_name(name: str) -> str:
    """Comparison form of a name: NFKC, case-folded, punctuation and extra spaces dropped."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"[^\w@.\s'-]", " ", text)
    return " ".join(text.replace("_", " ").split()).strip(" .'-")


def clean_tile_name(text: str) -> str:
    """A tile label minus Zoom's role / pronoun suffix ("Ana Lopez (Host)" -> "Ana Lopez")."""
    prev = None
    while prev != text:
        prev, text = text, _PAREN_SUFFIX.sub("", text)
    return " ".join(text.split())


# Lower-case name particles ("Ana de la Cruz", "Jan van Dijk").
_PARTICLES = {"de", "da", "del", "der", "di", "du", "la", "le", "van", "von", "bin", "al", "y"}


def is_name_like(text: str) -> bool:
    """One to five name-ish tokens, capitalised (bar particles), or a single email address.

    Caption text is lower-case sentences, so capitalisation is what tells a display name
    ("Priya Raman", "J. Smith") from a caption line ("and add it to the sprint")."""
    if not text or len(text) > 64 or text[-1] in ",;?!:":
        return False
    tokens = text.split()
    if not 1 <= len(tokens) <= 5 or not all(_NAME_TOKEN.match(t) for t in tokens):
        return False
    if len(tokens) == 1 and "@" in tokens[0]:
        return True
    return tokens[0][0].isupper() and all(t[0].isupper() or t in _PARTICLES for t in tokens)


def _is_ui(text: str) -> bool:
    key = norm_name(text)
    return key in _UI_TEXT or bool(_TIME.match(text.strip())) or not key


class Roster:
    """The meeting's participant names, as displayed, with fuzzy lookup for OCR'd headers."""

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._names: dict[str, str] = {}  # norm -> display
        # norm of a cut-off reading of a roster name ("Tim Mahl") -> that name's display.
        self._cut: dict[str, str] = {}
        # Display names replaced by a longer reading of the same person (old -> new), for
        # the caller to remap what it already holds under the old one; it clears this.
        self.renamed: dict[str, str] = {}
        # alias_key -> display name (speaker_aliases); applied to everything added.
        self.aliases = dict(aliases or {})

    def __contains__(self, name: str) -> bool:
        key = norm_name(name)
        return key in self._names or key in self._cut

    def __len__(self) -> int:
        return len(self._names)

    @property
    def names(self) -> list[str]:
        return list(self._names.values())

    def display(self, name: str) -> str:
        from api.persistence.models import alias_key

        return self.aliases.get(alias_key(name), name)

    def copy(self) -> Roster:
        out = Roster(self.aliases)
        out._names = dict(self._names)
        out._cut = dict(self._cut)
        return out

    def add(self, name: str) -> str | None:
        """Add a tile name; returns its display form when it is new, else None."""
        display = self.display(name)
        key = norm_name(name)
        if not key or key in self or len(self._names) + len(self._cut) >= _MAX_ROSTER:
            return None
        same = _same_name(key, self._names)
        if same is not None and len(same) > len(key):
            self._cut[key] = self._names[same]  # a cut-off reading of a name already known
            return None
        if same is not None:
            # The roster holds a cut-off reading of this name: keep the longer one, and
            # point the cut one (and anything held under it) at it.
            old = self._names.pop(same)
            self._cut[same] = display
            for k, d in self._cut.items():
                if d == old:
                    self._cut[k] = display
            self.renamed[old] = display
        self._names[key] = display
        return display

    def match(self, header: str, *, prefer: str | None = None) -> str | None:
        """The roster name ``header`` reads as, or None. Exact (normalized) first; otherwise
        the single best fuzzy match at or above ``_NAME_MATCH``. Two near-equal candidates
        are ambiguous and only resolve to ``prefer`` (the active speaker) when it is one."""
        key = norm_name(header)
        if not key:
            return None
        if key in self._names:
            return self._names[key]
        if key in self._cut:
            return self._cut[key]
        alias = self.display(header)
        if alias != header and norm_name(alias) in self._names:
            return self._names[norm_name(alias)]
        scored = sorted(
            ((SequenceMatcher(None, key, k).ratio(), d) for k, d in self._names.items()),
            reverse=True,
        )
        good = [(r, d) for r, d in scored if r >= _NAME_MATCH]
        if not good:
            return None
        if len(good) > 1 and good[0][0] - good[1][0] < 0.05:
            names = {d for r, d in good if good[0][0] - r < 0.05}
            return prefer if prefer in names else None
        return good[0][1]


def _same_name(key: str, names: dict[str, str]) -> str | None:
    """The roster key ``key`` is a cut-off reading of, or that is one of ``key``: one starts
    with the other and they read alike (``_NAME_MATCH``). A Zoom tile label truncated by its
    tile, or a name cut by the crop, read "Tim Mahl" for "Tim Mahler" and stored one person
    under two names (XERK-2695). "Priya Ra" (0.84) is no match: such strip-edge fragments are
    dropped by ``ocr`` instead. A short one ending in a digit is never cut: "Speaker 1" is
    not "Speaker 12"."""
    for k in names:
        short = min(k, key, key=len)
        if not short[-1:].isalpha():
            continue
        if (k.startswith(key) or key.startswith(k)) and SequenceMatcher(
            None, key, k
        ).ratio() >= _NAME_MATCH:
            return k
    return None


@dataclass(frozen=True)
class CaptionPane:
    """Where a full frame shows its caption pane (Teams, ``teams_pane``), as shares of the
    frame: ``box`` (left, top, right, bottom) below the pane's heading, and ``crop``, how
    much of the pane's height above its lowest text the client sends."""

    box: tuple[float, float, float, float]
    crop: float


@dataclass
class FullFrame:
    participants: list[str] = field(default_factory=list)
    active_speaker: str | None = None
    title: str | None = None
    # The meeting toolbar was on screen (Zoom hides it while the mouse is idle).
    in_meeting: bool = False
    # Teams: the live-captions pane, which the client is told to crop (screen.region).
    caption_pane: CaptionPane | None = None


def _caption_region(lines: list[OcrLine], width: int, height: int) -> tuple[float, float] | None:
    """Vertical extent of the caption box in a full frame: the run of long, centred text
    lines in the lower half. Name lines inside it are caption headers, not tiles."""
    caps = [
        ln
        for ln in lines
        if ln.cy > height * 0.5
        and ln.cy < height * _TOOLBAR_BAND
        and abs(ln.cx - width / 2) < width * 0.3
        and (len(ln.text) > 28 or not is_name_like(ln.text))
    ]
    if not caps:
        return None
    top = min(ln.box[1] for ln in caps)
    bottom = max(ln.box[3] for ln in caps)
    line_h = max(ln.height for ln in caps)
    return top - line_h * 1.8, bottom + line_h * 0.5


def _reading_order(lines: tuple[OcrLine, ...]) -> list[OcrLine]:
    """Rows top to bottom, left to right within a row. Sorting by y alone mixed a row's tile
    names up: real OCR boxes on one row differ by a pixel or two (QA)."""
    rows: list[list[OcrLine]] = []
    for ln in sorted(lines, key=lambda ln: ln.cy):
        if rows and ln.cy - rows[-1][0].cy <= max(4.0, 0.5 * rows[-1][0].height):
            rows[-1].append(ln)
        else:
            rows.append([ln])
    return [ln for row in rows for ln in sorted(row, key=lambda ln: ln.box[0])]


def parse_full_frame(ocr: OcrResult, jpeg: bytes | None = None) -> FullFrame:
    w, h = ocr.width, ocr.height
    out = FullFrame()
    title_lines = [ln for ln in ocr.lines if ln.box[3] <= h * _TITLE_BAND and not _is_ui(ln.text)]
    if title_lines:
        best = max(title_lines, key=lambda ln: ln.box[2] - ln.box[0])
        if len(best.text) >= 3:
            out.title = best.text.strip()
    controls = {
        norm_name(ln.text) for ln in ocr.lines if ln.cy > h * _TOOLBAR_BAND
    } & _MEETING_CONTROLS
    out.in_meeting = len(controls) >= _MIN_CONTROLS
    caption = _caption_region(list(ocr.lines), w, h)
    lines: list[tuple[str, OcrLine]] = []
    for ln in _reading_order(ocr.lines):
        if ln.box[1] < h * _TITLE_BAND or ln.cy > h * _TOOLBAR_BAND:
            continue
        if caption and caption[0] <= ln.cy <= caption[1] and abs(ln.cx - w / 2) < w * 0.3:
            continue
        name = clean_tile_name(ln.text)
        if not _is_ui(name) and is_name_like(name):
            lines.append((name, ln))
    img = _decode(jpeg, (w, h)) if jpeg and lines else None
    if img is not None:
        # Before deduping: a share showing a participant's name ahead of their tile (a Jira
        # assignee) would otherwise claim the name and drop the real tile with it (QA).
        px = np.asarray(img, dtype=np.int16).max(axis=2)
        lines = [(name, ln) for name, ln in lines if _over_tile_gap(px, ln)]
    tiles: list[tuple[str, OcrLine]] = []
    for name, ln in lines:
        if name not in out.participants:
            out.participants.append(name)
            tiles.append((name, ln))
    if img is not None and tiles:
        out.active_speaker = _active_tile(img, tiles)
    return out


def _decode(jpeg: bytes, size: tuple[int, int]):  # -> PIL.Image.Image | None
    """The frame as RGB, or None when it is undecodable, not the size OCR read, or too big."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg))  # reads the header only
        if img.size != size or size[0] * size[1] > _MAX_DECODE_PIXELS:
            return None
        return img.convert("RGB")
    except Exception:
        return None


def _over_tile_gap(px: np.ndarray, ln: OcrLine) -> bool:
    """A tile label sits at its tile's bottom-left, so the dark gap under the tile (or the
    window's edge) runs a few pixels below it. Text inside a screen share sits on the shared
    content instead: the Jira board's sidebar items and ticket keys became "participants"
    (XERK-2643, real capture). ``px`` is the frame's per-pixel max channel.

    Accepted: a dark-themed share (an editor, a dark web page) has dark rows under its text
    too, so its name-shaped lines still pass; the roster match on caption headers decides.
    """
    height = px.shape[0]
    x0, x1 = max(0, int(ln.box[0])), min(px.shape[1], int(ln.box[2]))
    y0 = max(0, int(ln.box[3]))
    y1 = min(height, int(ln.box[3] + ln.height) + 3)
    if x1 <= x0 or y0 >= height:
        return True
    rows = np.percentile(px[y0:y1, x0:x1], 90, axis=1)
    return bool((rows <= _GAP_MAX_LUMA).any())


def _active_tile(img, tiles: list[tuple[str, OcrLine]]) -> str | None:
    """The tile name sitting inside Zoom's green active-speaker border, if exactly one does.

    Zoom labels a tile at its bottom-left corner, so the tile's own border runs just left of
    and just below the label. Green is counted on each side separately and a tile scores the
    smaller count: one window around the label also caught the border of the tile above or to
    the left, so on a 3x3 gallery two tiles scored alike and none won (QA with real OCR).
    Best effort: no clear winner is None, and the caption header alone then names the speaker.
    """
    width, height = img.size

    def green(box: tuple[float, float, float, float]) -> int:
        x0, y0, x1, y1 = (int(v) for v in box)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(width, x1), min(height, y1)
        if x1 <= x0 or y1 <= y0:
            return 0
        px = np.asarray(img.crop((x0, y0, x1, y1)), dtype=np.int16)
        r, g, b = px[..., 0], px[..., 1], px[..., 2]
        return int(np.count_nonzero((g >= 150) & (g - r >= 70) & (g - b >= 40)))

    scores: list[tuple[int, str]] = []
    for name, ln in tiles:
        x0, y0, x1, y1 = ln.box
        pad = max(12.0, ln.height * 1.5)
        # Left: the column just left of the label, the label's height. Below: the band just
        # under it, starting at the label (a left neighbour's bottom border ends further left).
        left = green((x0 - pad, y0, x0, y1))
        below = green((x0 - pad / 3, y1, x1, y1 + pad))
        scores.append((min(left, below), name))
    hits = sorted((s for s in scores if s[0] >= _BORDER_MIN_PIXELS // 2), reverse=True)
    if not hits or (len(hits) > 1 and hits[1][0] * 2 > hits[0][0]):
        return None
    return hits[0][1]


@dataclass
class CaptionBlock:
    # The roster name the header matched, or (Teams) the header's own name when an avatar
    # vouches for it; None for any other unmatched header and for the header-less block of
    # lines continuing from above the crop.
    speaker: str | None
    has_header: bool
    lines: list[str] = field(default_factory=list)
    # Teams: the header is drawn beside a speaker avatar (pixels or OCR'd initials). An
    # unmatched header with one is a speaker; one without may be a slide heading (XERK-2614).
    avatar: bool = False


def row_groups(lines: tuple[OcrLine, ...]) -> list[list[OcrLine]]:
    """The OCR boxes of each visual row, rows top to bottom, boxes left to right."""
    rows: list[list[OcrLine]] = []
    for ln in sorted(lines, key=lambda ln: (ln.cy, ln.box[0])):
        if rows:
            last = rows[-1]
            top = min(x.box[1] for x in last)
            bottom = max(x.box[3] for x in last)
            overlap = min(bottom, ln.box[3]) - max(top, ln.box[1])
            if overlap > 0.5 * min(ln.height, bottom - top):
                last.append(ln)
                continue
        rows.append([ln])
    return [sorted(r, key=lambda x: x.box[0]) for r in rows]


def parse_caption_crop(
    ocr: OcrResult,
    roster: Roster,
    *,
    active: str | None = None,
    live: bool = False,
    jpeg: bytes | None = None,
) -> list[CaptionBlock]:
    """Split a caption-box crop into speaker blocks (``live``: Teams only).

    Zoom's caption panel puts the speaker's name on its own line at the top of the crop (the
    client adds it, XERK-2506). A speaker not on the roster (a screen share shows only a strip
    of tiles) must not have that name stored as speech: a name-shaped first row followed by
    caption text is a header with no speaker, roster or not. Lower down, the panel draws each
    name dimmer than caption text, so with the crop's pixels a dim name-shaped row is the next
    entry's header and a bright one is text (XERK-2644: under an entry left unpunctuated the
    name was stored as the previous speaker's speech). Without readable pixels, a header can
    sit only below a row that ends a sentence.

    Unless a meeting is ``live``, a crop must show a speaker header to yield anything
    (XERK-2693): bright text centred low on a dark page (docs, release notes) passes the
    client's box finder, and its header-less lines started meetings. A header counts when the
    roster matches it or, off the roster, when the pixels show it as a dim name line (with no
    pixels to read, any header). During a meeting a header-less crop is an entry whose
    name scrolled off, and it stays. Zoom's toolbar rows are never caption text."""
    groups = [
        g for g in row_groups(ocr.lines) if _is_caption_row(_row_text(g))
    ]  # minus noise rows with fewer than two letters or digits, and the toolbar
    rows = [_row_text(g) for g in groups]
    px = _grey_pixels(jpeg, ocr, groups)
    dim = _dim_rows(px, groups)

    def header_can_sit(i: int) -> bool:
        # With pixels, a header below the top is a dim row, which ``name_rows`` settles.
        return i == 0 or (dim is None and rows[i - 1].rstrip().endswith((".", "?", "!")))

    # A row the pixels show as a name line may be any display name ("jsmith", "Raman, Priya",
    # "Bo Chen (Guest)"): is_name_like alone stored those as speech (XERK-2693 QA).
    dim_names = {rows[i] for i in dim or () if _is_display_name(rows[i])}

    def name_like(text: str) -> bool:
        return is_name_like(text) or text in dim_names

    blocks = caption_blocks(
        rows,
        roster,
        active=active,
        name_like=name_like,
        unknown_headers=header_can_sit,
        name_rows=dim,
    )
    if live or any(b.has_header and b.speaker for b in blocks):
        return blocks
    # Without pixels a trailing empty header is the position rule's guess (XERK-2794), so it
    # vouches for nothing; with them it is a dim name row, which does (QA).
    if not any(b.has_header and not (dim is None and pending_header(blocks, b)) for b in blocks):
        return []
    # Only unmatched headers. Without pixels, or with no text row to compare brightness
    # against, the position rule's header stands. Otherwise a dim row that reads as a
    # capitalised name ("Raman, Priya" and "(Guest)" allowed) vouches for a speaker off the
    # roster: a docs page's heading is as bright as its text, and any-shape dim rows let its
    # grey meta lines ("updated 3 days ago") start meetings (QA). So does a dim display name
    # of any case with Zoom's avatar circle beside it ("jsmith", XERK-2700). The avatar only
    # adds: with none seen (cut off, low contrast, square) the capitalised rule stands, as
    # requiring one stopped those speakers' meetings (QA). A grey Title Case docs line
    # ("Last Updated") still vouches: telling it from a name needs real crops (XERK-2705).
    if px is None or dim is None:
        return blocks
    vouched = any(
        row in dim_names
        and (
            is_name_like(_name_form(row))
            or _has_avatar(px, group, groups[i + 1] if i + 1 < len(groups) else None)
        )
        for i, (row, group) in enumerate(zip(rows, groups, strict=True))
    )
    return blocks if vouched else []


def _name_form(text: str) -> str:
    """A display name as ``is_name_like`` reads it: "Raman, Priya (Guest)" -> "Raman Priya"."""
    return " ".join(clean_tile_name(text).replace(",", " ").split())


def _is_display_name(text: str) -> bool:
    """Loose shape of a Zoom display name, for rows already drawn as name lines: one to five
    tokens with a letter, not ending a sentence. Any case, commas and a "(Guest)" suffix."""
    tokens = clean_tile_name(text).split()
    return (
        0 < len(tokens) <= 5
        and len(text) <= 64
        and text.rstrip()[-1] not in ".?!:;,"
        and any(c.isalpha() for c in tokens[0])
    )


def _is_caption_row(text: str) -> bool:
    return sum(c.isalnum() for c in text) >= 2 and not _is_toolbar_row(text)


def _is_toolbar_row(text: str) -> bool:
    """A row of Zoom's own toolbar: its Title Case button labels ("Participants Chat React
    Share More"), or the carets and single letters under its buttons ("^ D ^ A ^"). Caption
    text is lower-case sentences, so needing two labels and three quarters of the row keeps
    speech like "Share more of it" (XERK-2693)."""
    tokens = text.split()
    if "^" in text and not any(sum(c.isalnum() for c in t) >= 2 for t in tokens):
        return True
    # The carets under the buttons may be read into the labels' row ("Mute ^ Stop Video ^").
    tokens = [t for t in tokens if t != "^"]
    labels = sum(t[:1].isupper() and norm_name(t) in _TOOLBAR_WORDS for t in tokens)
    return labels >= 2 and labels >= 0.75 * len(tokens)


def _row_text(group: list[OcrLine]) -> str:
    return " ".join(x.text for x in group).strip()


# A speaker-name row's ink contrast is below this share of the caption text's: Zoom draws the
# name ~154 grey and the text white on a ~21 panel (~0.57 of the text's contrast). Same share
# as the capture client's NAME_DIM (desktop/tenir_capture/caption_box.py).
_NAME_DIM = 0.8


@dataclass(frozen=True)
class _Pixels:
    """A caption crop's pixels: ``ink`` is each pixel's grey distance from the panel colour
    (the crop's median grey)."""

    ink: np.ndarray  # rows x columns


def _grey_pixels(
    jpeg: bytes | None, ocr: OcrResult, groups: list[list[OcrLine]]
) -> _Pixels | None:
    """The crop's pixels, None when they can't be read (no image, a size OCR didn't read, a
    box that isn't finite)."""
    if not jpeg or not all(math.isfinite(v) for g in groups for ln in g for v in ln.box):
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg))  # reads the header only
        if img.size != (ocr.width, ocr.height) or ocr.width * ocr.height > _MAX_DECODE_PIXELS:
            return None
        g = np.asarray(img.convert("L"), dtype=np.int16)
    except Exception:
        return None
    return _Pixels(np.abs(g - float(np.median(g))))


# Zoom's avatar circle: grey distance from the panel that counts as its ink, how far left of
# the name it may sit (name heights), its height range (name heights), and the share of the
# search column's width a y line needs lit to count, and the window its centre must fall in.
# Real Zoom crops (XERK-2705, 22 name rows, one meeting) draw a ~1.1-1.3 h photo circle
# astride the name and its first text row: centred ~4 px below the name box's bottom, the
# text row's centre ~11 px lower. The panel_crop fixture centres it level with the name.
# The window runs from a quarter name height above the name's centre (a disc above the name
# isn't its avatar) down to ``_AVATAR_BELOW`` of the text row's height past the gap between
# the rows: past that it is a circle beside the text row (an emoji, a bullet). Measured on the
# text row, not the name: OCR boxes "vera cruz" shorter and lower than "Tim Mahler" while the
# avatar stays put, and a name-height window missed it (QA). With no row below, the window is
# ``_AVATAR_CENTRE`` name heights below the name's top.
_AVATAR_INK = 40
_AVATAR_REACH = 3.0
_AVATAR_TALL = (0.8, 4.0)
_AVATAR_LIT = 0.12
_AVATAR_CENTRE = (0.25, 1.35)
_AVATAR_BELOW = 0.3


def _has_avatar(
    px: _Pixels, group: list[OcrLine], below: list[OcrLine] | None = None
) -> bool:
    """A round patch of ink left of this row's first box: Zoom's avatar beside a speaker's
    name line, astride it and ``below`` (the next row, its first text line; XERK-2705). A docs
    page's grey meta line or heading has bare page (or a bullet, an outline icon: not filled
    circles) there. False when the row starts at the crop's left edge, with no room to show a
    circle."""
    x0, y0, _, y1 = group[0].box
    h = max(y1 - y0, 1.0)
    lo = (y0 + y1) / 2 - 0.25 * h
    if below:
        by0, by1 = below[0].box[1], below[0].box[3]
        hi = (y1 + by0) / 2 + _AVATAR_BELOW * max(by1 - by0, 1.0)
    else:
        lo, hi = y0 + _AVATAR_CENTRE[0] * h, y0 + _AVATAR_CENTRE[1] * h
    right = int(x0 - 0.1 * h)
    left = max(0, int(x0 - _AVATAR_REACH * h))
    if right - left < _AVATAR_TALL[0] * h:
        return False
    ink = px.ink[:, left:right] > _AVATAR_INK
    lit = ink.mean(axis=1) >= _AVATAR_LIT
    return any(
        _AVATAR_TALL[0] * h <= bottom - top <= _AVATAR_TALL[1] * h
        and top < y1
        and bottom > y0
        # Centred astride the name and its first text row, never on the row below.
        and lo <= (top + bottom) / 2 <= hi
        and _is_round(ink[top:bottom])
        for top, bottom in _runs(lit)
    )


def _dim_rows(px: _Pixels | None, groups: list[list[OcrLine]]) -> set[int] | None:
    """Indices of the rows drawn dimmer than the crop's caption text (Zoom's name lines),
    read from the crop's pixels: None when they can't be read or there is no text row to
    compare against.

    A row's ink is the 99.5th percentile of its pixels' distance from the panel colour,
    inside its OCR boxes only: the avatar left of a name is bright."""
    if px is None or len(groups) < 2:
        return None
    inks: list[float] = []
    for group in groups:
        parts = []
        for ln in group:
            x0, y0, x1, y1 = (max(0, int(round(v))) for v in ln.box)
            box = px.ink[y0:y1, x0:x1]
            if box.size:
                parts.append(box.reshape(-1))
        inks.append(float(np.percentile(np.concatenate(parts), 99.5)) if parts else 0.0)
    # The text's ink: the brightest row that isn't name-shaped (a crop always has text).
    text = [ink for ink, gr in zip(inks, groups, strict=True) if not is_name_like(_row_text(gr))]
    if not text or max(text) <= 0:
        return None
    return {i for i, ink in enumerate(inks) if ink < _NAME_DIM * max(text)}


def _runs(lit: np.ndarray) -> list[tuple[int, int]]:
    """The [top, bottom) spans of consecutive lit y lines."""
    out: list[tuple[int, int]] = []
    y, height = 0, len(lit)
    while y < height:
        if not lit[y]:
            y += 1
            continue
        top = y
        while y < height and lit[y]:
            y += 1
        out.append((top, y))
    return out


def _is_round(ink: np.ndarray) -> bool:
    """A run of lit lines (``ink``: rows x columns) is a filled circle: about as wide as tall
    at its middle, and narrower near its top and bottom than there. A square picture or a
    tile's edge is as wide at the ends."""
    tall = len(ink)

    def width(y: int) -> int:
        # The ink count, not its span: JPEG speckles beside a circle widened the span (QA).
        return int(np.count_nonzero(ink[y]))

    mid = width(tall // 2)
    if not 0.7 * tall <= mid <= 1.4 * tall:
        return False
    k = max(1, round(0.15 * tall))
    ends = max(width(k), width(tall - 1 - k))
    return ends <= 0.9 * mid


def pending_header(blocks: list[CaptionBlock], block: CaptionBlock) -> bool:
    """``block`` is the crop's last row read as the next entry's header before its text
    (no speaker, no text, no avatar): it vouches for no crop, or slide text over a name
    would start a meeting (XERK-2794 QA)."""
    return block is blocks[-1] and block.speaker is None and not block.lines and not block.avatar


def caption_blocks(
    rows: list[str],
    roster: Roster,
    *,
    active: str | None = None,
    name_like: Callable[[str], bool] = is_name_like,
    unknown_headers: Callable[[int], bool] | None = None,
    avatars: set[int] | None = None,
    avatars_decide: bool = False,
    name_rows: set[int] | None = None,
) -> list[CaptionBlock]:
    """Group caption rows into speaker blocks; ``name_like`` is the platform's name shape.

    ``unknown_headers`` (Teams): with a roster too, a name-shaped row ``i`` followed by a row
    that isn't name-shaped is a header with no speaker when ``unknown_headers(i)`` says a
    header can sit there. Teams captions are Title Case, so the platform decides where
    ("I spoke with" / "John Smith" / "yesterday" is one caption).

    ``avatars`` (Teams): the rows drawn beside a speaker avatar; their headers are marked
    ``avatar``. With ``avatars_decide`` (avatars were found in the crop's pixels) it alone
    decides a header the roster doesn't match, with or without a roster: a name-shaped row in
    it is a header even before a name-shaped first line ("Hank Ives" / "Acme Global
    Partners"), and one outside it is caption text (a slide heading). So is a row the roster
    does match ("Raman, Priya" / "Kunal Shah" / "will demo", XERK-2604), wherever it sits:
    a rule that also asked where the row sits flipped it back to a header once the header
    above scrolled off, and the merge stored the entry twice (QA). A missed avatar merges
    that speaker into the one before (accepted).

    ``name_rows`` (Zoom, from pixels): rows drawn as speaker names. A name-shaped one is a
    header whatever follows it: the last row (no text shown yet), another name-shaped row
    ("Sounds Good"), a one-word display name ("Shad")."""
    marked = avatars or set()
    named = name_rows or set()
    blocks: list[CaptionBlock] = []
    for i, row in enumerate(rows):
        # Avatars were found and none is beside this row: whatever it reads, it is caption.
        no_header = avatars_decide and i not in marked
        speaker = roster.match(row, prefer=active) if name_like(row) and not no_header else None
        if speaker is not None:
            blocks.append(CaptionBlock(speaker, True, avatar=i in marked))
            continue
        head, sep, rest = row.partition(":")
        if sep and not no_header and name_like(head.strip()):
            speaker = roster.match(head.strip(), prefer=active)
            if speaker is not None:
                text = [rest.strip()] if rest.strip() else []
                blocks.append(CaptionBlock(speaker, True, text, avatar=i in marked))
                continue
        # Before any full frame has named the tiles there is nothing to validate a header
        # against: a short name-shaped row followed by more text is taken as a header with
        # no speaker rather than stored as a caption.
        if avatars_decide:
            # With no roster a name row right above an avatar's (a tile label the band
            # caught) is an empty header, as the position rule makes it, not caption text.
            unknown = name_like(row) and (i in marked or (not len(roster) and i + 1 in marked))
        else:
            has_next = i + 1 < len(rows)
            unknown = (
                name_like(row)
                and (i in named or len(row.split()) >= 2)
                and (
                    i in named
                    or (has_next and not len(roster))
                    # No roster, last row: a name where a header can sit (after a finished
                    # line) is the next entry's header, its text not shown yet. Read as text
                    # it was stored as speech for good (XERK-2794). It verifies no crop
                    # (``pending_header``): slide text over a name started meetings (QA).
                    or (
                        0 < i
                        and not has_next
                        and not len(roster)
                        and unknown_headers is not None
                        and unknown_headers(i)
                    )
                    or (
                        unknown_headers is not None
                        and has_next
                        and not name_like(rows[i + 1])
                        and unknown_headers(i)
                    )
                )
            )
        if unknown:
            # An avatar beside the header vouches that it names a speaker, so it keeps that
            # name though the roster lacks it: a meeting whose window isn't on screen fills
            # no roster, and every row was stored with no speaker (XERK-2791).
            named_by = roster.display(row) if i in marked else None
            blocks.append(CaptionBlock(named_by, True, avatar=i in marked))
            continue
        if not blocks:
            blocks.append(CaptionBlock(None, False))
        blocks[-1].lines.append(row)
    return [b for b in blocks if b.lines or b.has_header]
