"""Microsoft Teams layout parser: OCR'd text -> participants, title and caption blocks (XERK-2515).

Same two inputs as Zoom (``zoom.py``), different layout:

- A **full window frame**: the meeting title and call timer sit top-left and the toolbar
  (Chat, People, React, ..., Leave) top-right, in one top band; there is no bottom toolbar.
  Participant names are the name-shaped labels at each tile's bottom-left, and also the
  People pane's list when it is open (both are participants). Live captions sit in a panel
  along the bottom of the stage, left-aligned, so the caption region spans the full width.
- A **caption-panel crop**: entries stacked oldest to newest, each an avatar (a photo, or a
  circle of initials that OCR reads as ``AL``), the speaker's name on its own line, then the
  caption text wrapping over one or more lines. The newest entry is rewritten in place as the
  recognizer refines it; older entries scroll up. ``merge.py`` folds that like Zoom's box.

Teams display names differ from Zoom's: directory names are often ``Last, First`` and carry
suffixes like ``(Guest)``, ``(External)`` or an org in parentheses; captions are punctuated,
capitalised sentences. The roster match is what decides a header, as for Zoom.

No active-speaker detection: Teams' speaking ring is a thin, theme-coloured outline that
can't be told from a tile's own border reliably, so the caption header alone names the
speaker (``FullFrame.active_speaker`` stays None).

Tuned on synthetic frames (no operator screenshot was available, docs/meeting-captions.md
Caveats); re-tune on real XERK-2506 captures.
"""

from __future__ import annotations

import re

from api.screen.ocr import OcrLine, OcrResult
from api.screen.zoom import (
    CaptionBlock,
    FullFrame,
    Roster,
    caption_blocks,
    clean_tile_name,
    is_name_like,
    norm_name,
    row_groups,
    _reading_order,
    _TIME,
)

# Teams chrome that OCR reads off a full frame and that is never a participant or a title.
_UI_TEXT = {
    "microsoft teams",
    "teams",
    "meeting",
    "meet now",
    "chat",
    "people",
    "participants",
    "raise",
    "raise hand",
    "react",
    "view",
    "rooms",
    "apps",
    "notes",
    "more",
    "camera",
    "mic",
    "share",
    "leave",
    "end",
    "copilot",
    "recording",
    "recording and transcription",
    "transcript",
    "live captions",
    "captions",
    "language and speech",
    "in this meeting",
    "invite",
    "invite people",
    "share invite",
    "mute all",
    "presenters",
    "attendees",
    "suggestions",
    "others",
    "you",
    "together mode",
    "gallery",
    "speaker",
    "spotlight",
    "pinned",
    "presenting",
    "is presenting",
    "is presenting now",
    "present",
    "presenter",
    "organizer",
    "lobby",
    "admit",
    "admit all",
    "deny",
    "waiting in lobby",
    "mute",
    "unmute",
    "muted",
    "close",
    "search",
    "whiteboard",
    "reactions",
    "recap",
    "activity",
    "calendar",
    "files",
    "calls",
    "join",
    "join now",
    "settings",
    "help",
    "guest",
    "guests",
    "external",
    "unverified",
    "others invited",
    "more actions",
    "invite someone or dial a number",
}
# "In this meeting (4)", "Attendees (12)": People-pane section headings with a count.
_COUNTED = re.compile(r"^(.*?)\s*\(\d+\)$")
# An avatar's initials as OCR reads them: one to three capital letters, any script ("ŁŻ"),
# possibly read with a space ("P R").
_INITIALS = re.compile(r"^(?:[^\W\d_] ?){1,3}$")
# A name may end in "." only after an initial ("Bo L.", "Tolkien, J.R.R."), a dotted degree
# ("Ph.D.") or Jr./Sr.; otherwise it's a sentence ("Sure.", "Thanks Bo.").
_NAME_END = re.compile(
    r"\s(?:(?:[^\W\d_]\.)+|[^\W\d_]{1,2}\.(?:[^\W\d_]{1,2}\.)+|[JS]r\.)$"
)
# An avatar circle is about two text rows tall; a taller box than this beside a name is the
# avatar even when OCR misreads its letters.
_AVATAR_TALL = 1.4
# Longest text worth cleaning as a name: suffix stripping is per suffix, and caption rows are
# attacker-controlled (a row of 100 "(x)" suffixes cost ~1 s).
_MAX_NAME_TEXT = 100

# A comma OCR read with no space after it, between letters ("Lee,Bo").
_TIGHT_COMMA = re.compile(r"(?<=[^\W\d_]),(?=[^\W\d_])")

# Top band holding the title, timer and toolbar, as a fraction of the frame height.
_TOP_BAND = 0.09


def is_teams_name(text: str) -> bool:
    """A Teams display name: Zoom's name shape, or a directory ``Last, First`` name.

    Teams punctuates its captions, so a trailing full stop marks a sentence ("Sure.",
    "Thanks Kunal.") unless it ends an initial: without that, short replies on a full frame
    became participants and later swallowed caption lines as speaker headers (QA)."""
    text = text.strip()
    if text.endswith(".") and not _NAME_END.search(text):
        return False
    if is_name_like(text):
        return True
    last, sep, first = text.partition(",")
    return bool(sep) and "," not in first and is_name_like(last.strip()) and is_name_like(
        first.strip()
    )


def clean_name(text: str) -> str:
    """A tile label or caption header minus Teams' suffixes ("Lee, Bo (Guest)" -> "Lee, Bo")."""
    return text if len(text) > _MAX_NAME_TEXT else clean_tile_name(text)


def _initials(text: str, *, any_case: bool = False) -> str | None:
    """The letters of an initials circle as OCR read it ("P R" -> "PR"), else None.

    Capitals only unless ``any_case``: real OCR also reads a circle's "SO" as "So" or "so",
    but a lone lower-case word is caption text everywhere else."""
    text = text.strip()
    if not _INITIALS.match(text):
        return None
    letters = text.replace(" ", "")
    return letters if any_case or letters.isupper() else None


def _is_circle_text(text: str, *, tall: bool) -> bool:
    """An initials circle's letters, however OCR cased them ("SO", "sO", a tall "So").

    A title-case word of two letters is a name ("Al", "Jo") unless its box is tall: a tile's
    circle text stands about 1.4x or more over the frame's lines (XERK-2609)."""
    letters = _initials(text, any_case=True)
    if letters is None:
        return False
    if letters.isupper() or not letters.istitle():
        return True
    return len(letters) <= 2 and tall


def _initials_of(name: str) -> set[str]:
    return {t[0].upper() for t in name.replace(",", " ").split() if t[0].isalpha()}


def _is_avatar(token: str, name: str, *, any_case: bool = False) -> bool:
    """``token`` is the initials circle drawn for ``name`` (any order: "Lee, Bo" may be BL).

    With ``any_case`` a mis-cased reading ("So" for Sean O'Neil's "SO", XERK-2609) counts
    only as all of the name's initials, two letters at least: "so" or "I" alone are words."""
    letters = _initials(token, any_case=any_case)
    if letters is None:
        return False
    if letters.isupper():
        return set(letters) <= _initials_of(name)
    return len(letters) >= 2 and set(letters.upper()) == _initials_of(name)


def _is_ui(text: str) -> bool:
    text = text.strip()
    if (m := _COUNTED.match(text)) is not None:
        text = m.group(1)
    key = norm_name(text)
    return key in _UI_TEXT or bool(_TIME.match(text)) or not key


def _is_sentence(text: str) -> bool:
    """Caption-like text: four words or more, ending like a sentence, or starting lower-case
    (an unpunctuated interim: "okay so"). Short presence and status lines under a pane name or
    tile label ("In a call", "Out of office") are none of these."""
    text = text.strip()
    return (
        len(text.split()) >= 4 or text.endswith((".", "?", "!")) or text[:1].islower()
    )


def _is_name_row(text: str) -> bool:
    """A tile label or a caption header, its avatar initials read into the box or not."""
    name = clean_name(text)
    return is_teams_name(clean_name(_strip_avatar_token(name, Roster()))) and not _is_sentence(
        name
    )


def _caption_lines(lines: list[OcrLine], height: int) -> set[int]:
    """Ids of the full-frame lines that belong to the caption panel.

    Teams captions are capitalised ("Thanks Everyone"), so they are told from names by layout:
    the panel is a tight stack of left-aligned rows (gaps under one line height, or boxes
    overlapping by up to half a line), while People
    pane rows and tile labels are spaced by their avatars or tiles. Lower-half lines are linked
    into stacks by that spacing (and a similar left edge); a stack holding a caption-like line
    is the panel, and every line in it is caption text.

    Five QA rounds of boxes around "sentence" lines each traded one layout for another: a box
    stretched across the People pane wiped its names, one per column was set off by a status
    line or a lower-case guest name, and the edges leaked a Title-Case caption. Linking rows by
    spacing needs no box: a pane row only joins a stack it really is pressed against.

    A name row links only within half its own height: a tile label sits a little above the
    panel (19 px under an 18 px label at 1080p, real OCR, XERK-2609), further than any two
    panel rows are apart (under 0.4 of a row), and linked in it the label was lost from the
    roster. Its own height, not the pair's: OCR can read the avatar and first caption line as
    one tall box, and that box's height let the label in."""
    low = [ln for ln in lines if ln.cy > height * 0.5 and not _is_ui(ln.text)]
    named = [_is_name_row(ln.text) for ln in low]
    parent = list(range(len(low)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(low):
        for j in range(i + 1, len(low)):
            b = low[j]
            line_h = max(a.height, b.height, 1.0)
            gap = max(a.box[1], b.box[1]) - min(a.box[3], b.box[3])
            # Real Paddle boxes of stacked rows overlap by a few pixels (gap -1..-4 at normal
            # leading); half a line of overlap still reads as the next row, not the same one.
            name_h = [ln.height for ln, n in ((a, named[i]), (b, named[j])) if n]
            near = min(name_h) / 2 if name_h else line_h
            if -line_h / 2 <= gap <= near and abs(a.box[0] - b.box[0]) <= 3 * line_h:
                parent[find(i)] = find(j)
    stacks: dict[int, list[OcrLine]] = {}
    for i, ln in enumerate(low):
        stacks.setdefault(find(i), []).append(ln)
    out: set[int] = set()
    for stack in stacks.values():
        if any(
            _is_sentence(ln.text) and not is_teams_name(clean_name(ln.text)) for ln in stack
        ):
            out.update(id(ln) for ln in stack)
    return out


def parse_full_frame(ocr: OcrResult, jpeg: bytes | None = None) -> FullFrame:
    """Participants and title from a full Teams window (``jpeg`` is unused: no border check)."""
    del jpeg
    h = ocr.height
    out = FullFrame()
    top = [ln for ln in ocr.lines if ln.box[3] <= h * _TOP_BAND and not _is_ui(ln.text)]
    if top:
        # The title is the leftmost text up there; the toolbar sits to its right.
        best = min(top, key=lambda ln: ln.box[0])
        if len(best.text) >= 3:
            out.title = best.text.strip()
    caption = _caption_lines(list(ocr.lines), h)
    text_h = sorted(ln.height for ln in ocr.lines if _initials(ln.text, any_case=True) is None)
    median_h = text_h[len(text_h) // 2] if text_h else 0.0
    for ln in _reading_order(ocr.lines):
        if ln.box[1] < h * _TOP_BAND:
            continue
        if id(ln) in caption:
            continue
        name = clean_name(ln.text)
        tall = median_h > 0 and ln.height >= _AVATAR_TALL * median_h
        if _is_ui(name) or not is_teams_name(name) or _is_circle_text(name, tall=tall):
            continue
        if name not in out.participants:
            out.participants.append(name)
    for name in _panel_speakers(ocr.lines, caption):
        if name not in out.participants:
            out.participants.append(name)
    return out


def _avatar_for(box: OcrLine, lines: tuple[OcrLine, ...]) -> OcrLine | None:
    """The name line ``box`` is the initials circle of, or None. A box is an avatar when it
    reads as initials and a name with those initials starts just right of it, overlapping it
    vertically."""
    if _initials(box.text, any_case=True) is None:
        return None
    for other in lines:
        if other is box or other.box[0] < box.box[2] - 2:
            continue
        if other.box[0] - box.box[2] > 4 * other.height:
            continue  # too far right to be this name's avatar
        if min(other.box[3], box.box[3]) <= max(other.box[1], box.box[1]):
            continue
        name = clean_name(other.text)
        if not is_teams_name(name):
            continue
        # Its letters, or (misread, an email's initials) the circle's two-row height.
        # Two letters at least for the height rule: a tall lone "I" is a caption word.
        tall = len(box.text.replace(" ", "")) >= 2 and box.height >= _AVATAR_TALL * other.height
        if _is_avatar(box.text, name, any_case=True) or tall:
            return other
    return None


def _drop_avatar_boxes(ocr: OcrResult) -> OcrResult:
    """Remove the initials circles beside speaker names before rows are grouped: the circle
    is taller than the name line and would glue the name and the first text line into one
    row."""
    return OcrResult(
        ocr.width, ocr.height, tuple(b for b in ocr.lines if _avatar_for(b, ocr.lines) is None)
    )


def _panel_speakers(lines: tuple[OcrLine, ...], caption: set[int]) -> list[str]:
    """Names of the caption panel's speaker headers: a panel row with an initials avatar,
    read into its box ("BL Lee, Bo") or beside it. Speakers are participants even when no
    tile shows them: speaker view shows one tile, and without these the other speakers'
    headers matched no one and their captions went to the previous speaker (XERK-2609).
    A Title-Case caption line has no avatar, so it isn't taken for a name."""
    panel = tuple(ln for ln in lines if id(ln) in caption)
    found: list[str] = []
    for ln, fixed in zip(panel, _strip_header_boxes(panel, Roster()), strict=True):
        if fixed is not ln or _strip_avatar_token(clean_name(ln.text), Roster()) != clean_name(
            ln.text
        ):
            found.append(fixed.text)
        elif (name_line := _avatar_for(ln, lines)) is not None and id(name_line) in caption:
            found.append(name_line.text)
    out = []
    for text in found:
        name = clean_name(_strip_avatar_token(clean_name(text), Roster()))
        if is_teams_name(name) and not _is_ui(name):
            out.append(name)
    return out


def _drop_tile_initials(ocr: OcrResult) -> OcrResult:
    """Remove the big initials circles of camera-off tiles a bottom-band crop catches: two
    letters at least twice the height of the crop's text. Left in, a row of them ("so MK")
    read as an older caption line above the panel (XERK-2609)."""
    text_h = sorted(b.height for b in ocr.lines if _initials(b.text, any_case=True) is None)
    if not text_h:
        return ocr
    median = text_h[len(text_h) // 2]

    def circle(b: OcrLine) -> bool:
        letters = _initials(b.text, any_case=True)
        return letters is not None and len(letters) >= 2 and b.height >= 2 * median

    return OcrResult(ocr.width, ocr.height, tuple(b for b in ocr.lines if not circle(b)))


def _drop_tile_labels(ocr: OcrResult) -> OcrResult:
    """Remove rows of video-tile name labels a bottom-band crop catches: two or more names
    on one row, apart. Joined they'd read as one caption line ("Kunal Shah Sean O'Neil")."""
    drop: set[int] = set()
    for row in row_groups(ocr.lines):
        if len(row) < 2 or not all(is_teams_name(clean_name(b.text)) for b in row):
            continue
        gaps = [b.box[0] - a.box[2] for a, b in zip(row, row[1:], strict=False)]
        if min(gaps) > 2 * max(b.height for b in row):
            drop.update(id(b) for b in row)
    return OcrResult(ocr.width, ocr.height, tuple(b for b in ocr.lines if id(b) not in drop))


def _strip_avatar_token(row: str, roster: Roster, *, any_case: bool = False) -> str:
    """A name row whose OCR box swallowed the initials circle: "PR Raman, Priya".

    Mis-cased initials ("So Sean O'Neil") are stripped too only with ``any_case`` (an avatar
    box, ``_strip_header_boxes``) and off a name the roster knows when it knows any."""
    for n in (3, 2, 1):  # longest first: "B L Lee, Bo" is not "B" + "L Lee, Bo"
        parts = row.split(" ", n)
        if len(parts) <= n:
            continue
        token, rest = " ".join(parts[:n]), parts[n]
        name = clean_name(rest)
        if not is_teams_name(name):
            continue
        if _is_avatar(token, name):
            return rest
        if not any_case:
            continue
        if _is_avatar(token, name, any_case=True) and (not len(roster) or roster.match(name)):
            return rest
    return row


def _strip_header_boxes(lines: tuple[OcrLine, ...], roster: Roster) -> tuple[OcrLine, ...]:
    """``lines`` with mis-cased initials ("So Sean O'Neil") stripped off a box that starts left
    of the caption text right under it, where the avatar sits; other boxes come back as is.
    Mid-caption, "Hi Hank Ives" starts where the text below it does and stays text (QA on
    XERK-2609). Judged against the text below, not a column: a shared slide or tile labels
    beside the panel shift any median edge."""
    out = []
    for b in lines:
        text = b.text.strip()
        new = _strip_avatar_token(text, roster, any_case=True)
        if new == text:
            new = _strip_misread_avatar(text, roster)
        if new != text and _starts_left_of_text_below(b, lines):
            b = OcrLine(new, b.box, b.score)
        out.append(b)
    return tuple(out)


def _strip_misread_avatar(text: str, roster: Roster) -> str:
    """A roster name behind a short token OCR misread off its circle ("3 Lee, Bo", "BI Lee,
    Bo" for BL): the name alone, else ``text``. Real OCR, 1920x1080 and 1680x1050 (QA on
    XERK-2609): unstripped, the row matched no one and Lee's caption went to Priya. Only
    called for a box left of the text below it, so a caption's short first word is safe."""
    token, sep, rest = text.partition(" ")
    if not sep or len(token) > 3 or not len(roster):
        return text
    name = clean_name(rest)
    return rest if is_teams_name(name) and roster.match(name) else text


def _starts_left_of_text_below(box: OcrLine, lines: tuple[OcrLine, ...]) -> bool:
    below = [
        o
        for o in lines
        if o is not box
        and box.cy < o.cy
        and o.box[1] - box.box[3] <= box.height
        and o.box[2] > box.box[0]
        and not is_teams_name(clean_name(o.text))
        and _initials(o.text, any_case=True) is None
    ]
    if not below:
        return False
    nearest = min(below, key=lambda o: o.box[1])
    return nearest.box[0] >= box.box[0] + box.height / 2


def _ends_sentence(row: str) -> bool:
    return row.rstrip().endswith((".", "?", "!"))


def _respaced(row: str, roster: Roster, *, at_header: bool) -> str:
    """``row`` with OCR's spacing slips in a name undone, else unchanged: a split name
    ("Sean O'Ne il") read as caption text, and a lost space ("Lee,Bo") as one word, too few
    for a header. A roster name is respelled anywhere; a ``Last, First`` one only where a
    header can sit (mid-caption, "Paris,London" is text)."""
    if len(row) > _MAX_NAME_TEXT:
        return row
    spaced = _TIGHT_COMMA.sub(", ", row)
    if at_header and is_teams_name(spaced):
        return spaced
    key = norm_name(row).replace(" ", "")
    return next((n for n in roster.names if norm_name(n).replace(" ", "") == key), row)


def parse_caption_crop(
    ocr: OcrResult, roster: Roster, *, active: str | None = None, live: bool = False
) -> list[CaptionBlock]:
    """Split a Teams caption-panel crop into speaker blocks.

    A crop showing no caption text at all (only names: tile labels caught by a fixed
    bottom-band crop, or an entry whose words haven't arrived) yields nothing, so it can't
    start a meeting. Unless a meeting is ``live``, nor does one with no speaker header the
    roster matches (any name-shaped header before there is a roster): text without one is
    tile initials, a misread label or a shared slide, and it started meetings (XERK-2609).
    During a meeting a header-less crop is an entry whose header scrolled off, and it stays.
    A header-less block above a header stays too: an older entry scrolled up."""
    rows: list[str] = []
    headed: list[bool] = []
    ocr = _drop_tile_initials(ocr)
    stripped = _strip_header_boxes(ocr.lines, roster)
    # Boxes with avatar evidence: initials stripped off them, or a circle beside them.
    avatared = {id(new) for old, new in zip(ocr.lines, stripped, strict=True) if new is not old}
    avatared |= {id(n) for b in stripped if (n := _avatar_for(b, stripped)) is not None}
    ocr = OcrResult(ocr.width, ocr.height, stripped)
    boxes = _drop_tile_labels(_drop_avatar_boxes(ocr))
    for group in row_groups(boxes.lines):
        raw = " ".join(b.text for b in group).strip()
        if sum(c.isalnum() for c in raw) < 2:
            continue  # noise, as caption_rows drops it
        # A header opens the crop, follows an entry's finished (punctuated) last line, or
        # carries its avatar: small OCR drops the final "." (QA on XERK-2609).
        avatar = any(id(b) in avatared for b in group)
        at_header = not rows or _ends_sentence(rows[-1]) or avatar
        headed.append(at_header)
        row = _respaced(_strip_avatar_token(raw, roster), roster, at_header=at_header)
        cleaned = clean_name(row)
        # A header's "(Guest)" suffix goes; a caption ending in parentheses stays caption.
        header = cleaned != row and is_teams_name(cleaned)
        rows.append(cleaned if header and (not len(roster) or roster.match(cleaned)) else row)
    blocks = caption_blocks(
        rows, roster, active=active, name_like=is_teams_name, unknown_headers=headed.__getitem__
    )
    if not any(b.lines for b in blocks):
        return []
    verified = any(b.has_header and (b.speaker or not len(roster)) for b in blocks)
    return blocks if verified or live else []
