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
"""

from __future__ import annotations

import io
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
        # alias_key -> display name (speaker_aliases); applied to everything added.
        self.aliases = dict(aliases or {})

    def __contains__(self, name: str) -> bool:
        return norm_name(name) in self._names

    def __len__(self) -> int:
        return len(self._names)

    @property
    def names(self) -> list[str]:
        return list(self._names.values())

    def display(self, name: str) -> str:
        from api.persistence.models import alias_key

        return self.aliases.get(alias_key(name), name)

    def add(self, name: str) -> str | None:
        """Add a tile name; returns its display form when it is new, else None."""
        display = self.display(name)
        key = norm_name(name)
        if not key or key in self._names or len(self._names) >= _MAX_ROSTER:
            return None
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


@dataclass
class FullFrame:
    participants: list[str] = field(default_factory=list)
    active_speaker: str | None = None
    title: str | None = None
    # The meeting toolbar was on screen (Zoom hides it while the mouse is idle).
    in_meeting: bool = False


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
    tiles: list[tuple[str, OcrLine]] = []
    for ln in _reading_order(ocr.lines):
        if ln.box[1] < h * _TITLE_BAND or ln.cy > h * _TOOLBAR_BAND:
            continue
        if caption and caption[0] <= ln.cy <= caption[1] and abs(ln.cx - w / 2) < w * 0.3:
            continue
        name = clean_tile_name(ln.text)
        if _is_ui(name) or not is_name_like(name):
            continue
        if name not in out.participants:
            out.participants.append(name)
            tiles.append((name, ln))
    if jpeg and tiles:
        out.active_speaker = _active_tile(jpeg, tiles, (w, h))
    return out


def _active_tile(
    jpeg: bytes, tiles: list[tuple[str, OcrLine]], size: tuple[int, int]
) -> str | None:
    """The tile name sitting inside Zoom's green active-speaker border, if exactly one does.

    Zoom labels a tile at its bottom-left corner, so the tile's own border runs just left of
    and just below the label. Green is counted on each side separately and a tile scores the
    smaller count: one window around the label also caught the border of the tile above or to
    the left, so on a 3x3 gallery two tiles scored alike and none won (QA with real OCR).
    Best effort: an undecodable frame or no clear winner is None, and the caption header
    alone then names the speaker.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg))  # reads the header only
        if img.size != size or size[0] * size[1] > _MAX_DECODE_PIXELS:
            return None
        img = img.convert("RGB")
    except Exception:
        return None
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
    # The roster name the header matched; None for an unmatched header (when the roster is
    # still empty) and for the header-less block of lines continuing from above the crop.
    speaker: str | None
    has_header: bool
    lines: list[str] = field(default_factory=list)
    # Teams: the header is drawn beside a speaker avatar (pixels or OCR'd initials). An
    # unmatched header with one is a speaker; one without may be a slide heading (XERK-2614).
    avatar: bool = False


def _rows(lines: tuple[OcrLine, ...]) -> list[str]:
    """OCR boxes grouped into visual rows, top to bottom, each row's pieces left to right."""
    return [" ".join(x.text for x in row).strip() for row in row_groups(lines)]


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
    """Split a caption-box crop into speaker blocks (``live`` and ``jpeg``: Teams only)."""
    del live, jpeg
    return caption_blocks(caption_rows(ocr), roster, active=active)


def caption_rows(ocr: OcrResult) -> list[str]:
    """The crop's visual rows, minus noise rows with fewer than two letters or digits."""
    return [r for r in _rows(ocr.lines) if sum(c.isalnum() for c in r) >= 2]


def caption_blocks(
    rows: list[str],
    roster: Roster,
    *,
    active: str | None = None,
    name_like: Callable[[str], bool] = is_name_like,
    unknown_headers: Callable[[int], bool] | None = None,
    avatars: set[int] | None = None,
    avatars_decide: bool = False,
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
    Partners"), and one outside it is caption text (a slide heading)."""
    marked = avatars or set()
    blocks: list[CaptionBlock] = []
    for i, row in enumerate(rows):
        speaker = roster.match(row, prefer=active) if name_like(row) else None
        if speaker is not None:
            blocks.append(CaptionBlock(speaker, True, avatar=i in marked))
            continue
        head, sep, rest = row.partition(":")
        if sep and name_like(head.strip()):
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
            unknown = (
                i + 1 < len(rows)
                and name_like(row)
                and len(row.split()) >= 2
                and (
                    not len(roster)
                    or (
                        unknown_headers is not None
                        and not name_like(rows[i + 1])
                        and unknown_headers(i)
                    )
                )
            )
        if unknown:
            blocks.append(CaptionBlock(None, True, avatar=i in marked))
            continue
        if not blocks:
            blocks.append(CaptionBlock(None, False))
        blocks[-1].lines.append(row)
    return [b for b in blocks if b.lines or b.has_header]
