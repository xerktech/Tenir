"""Rolling-caption merge: Zoom's re-drawn caption box -> final, de-duplicated lines (XERK-2505).

Zoom redraws its caption box many times a second: the bottom line grows word by word (and
the ASR may rewrite its last words), full lines scroll up and off, and a new speaker's block
pushes older ones out. Each OCR'd crop is a snapshot of that box: a window onto the tail of
the transcript, oldest line at the top, new lines only ever added at the bottom.

So each snapshot is **aligned** against the recent known lines, in order (a small
edit-distance alignment, ``_align``):

- known lines above the window (scrolled off the top) are skipped for free;
- snapshot lines below the last aligned one are new lines;
- a snapshot line above it that matches nothing is an OCR glitch (a half-scrolled or garbled
  top line) and is ignored — it can't be new, new lines appear at the bottom;
- a known line the snapshot skips in the middle is an OCR miss; it stays known.

Because the alignment is ordered, saying the same thing again is a new line ("okay", Bo's
reply, "okay" aligns as known, known, new), while a line still on screen is never stored twice.

A line is **final** when another speaker's block follows it in a snapshot, when it has not
changed for ``stable_ms``, or when the meeting ends (``flush``). A final that grows afterwards
(the speaker paused mid-line, then went on) is extended in place while it is still the newest
line (same segment id, so the store upserts it). A known line no snapshot has shown for
``_GONE_MS`` has left the screen and is no longer aligned against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher

from api.persistence.models import Segment
from api.screen.zoom import CaptionBlock

# A re-read of the same line (OCR jitter, an ASR rewrite of a word or two).
_SAME_LINE = 0.75
# How long a final can still be extended by its line growing.
_EXTEND_MS = 30_000
# A known line no snapshot has shown for this long has left the screen. It only separates
# a repeat after a silence that cleared the box from a long OCR miss.
_GONE_MS = 20_000
# Known lines kept, and the most recent of them a snapshot is aligned against.
_KEEP = 300
_TAIL = 20

# Alignment scores: a match earns up to _MATCH; a snapshot line left out above the aligned
# region (a glitch) or a known line skipped inside it (an OCR miss) costs a little; a new
# line at the bottom costs least, so a real match always beats calling everything new. The
# box always shows the newest lines, so a known line left after the aligned region costs
# most: otherwise an older run of the same words ("yes", "thanks", "yes") could be matched
# instead, swallowing the new line.
_MATCH = 3.0
_GLITCH = -1.0
# A glitch is normally a misread of a known line in the same place (a garbled or half-cut top
# line): that pairing is a substitution and costs _GLITCH. A shown line with no known line to
# pair with, under a verified speaker header, is a line said between two snapshots (Bo's
# "sure" above Ana's second "okay"); calling it a glitch costs more than calling it new.
_UNPAIRED = -3.5
# A misread must look like the line it misreads: same speaker and close text. Pairing any
# two lines let a real reply said between snapshots be "explained" as a misread and lost.
_MISREAD_LIKE = 0.5
# A Zoom box shows a handful of lines; a snapshot is cut to its bottom lines and each line to
# a sane length, which bounds the alignment's cost on a crafted frame (it runs per frame).
_MAX_SHOWN = 12
_MAX_CHARS = 160
_MISS = -2.5
_UNSHOWN_NEWEST = -2.5
_NEW = -0.1


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _ratio(a: str, b: str, floor: float = 0.0) -> float:
    """Similarity of two lines; 0.0 when it is certainly below ``floor`` (difflib's cheap
    upper bounds rule most pairs out without the quadratic comparison)."""
    sm = SequenceMatcher(None, a, b, autojunk=False)
    if floor and (sm.real_quick_ratio() < floor or sm.quick_ratio() < floor):
        return 0.0
    return sm.ratio()


def _same_speaker(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


@dataclass
class _Line:
    speaker: str | None
    text: str
    norm: str
    first_ms: int
    changed_ms: int
    seen_ms: int
    segment: Segment | None = None  # set once final

    @property
    def final(self) -> bool:
        return self.segment is not None


@dataclass
class _Shown:
    """One line of a snapshot, top to bottom."""

    speaker: str | None  # the block's header speaker; None for a header-less block
    text: str
    norm: str
    block: int


def _score(shown: _Shown, known: _Line, *, growable: bool) -> float:
    """How well ``shown`` re-reads ``known`` (0: not at all). Only a live line or the newest
    line can grow, so a longer line matches a stored older one only by similarity: "okay so
    lets start" after Bo spoke is a new line, not Ana's earlier "okay" growing."""
    if not _same_speaker(shown.speaker, known.speaker):
        return 0.0
    a, b = shown.norm, known.norm
    if a == b:
        return _MATCH
    if growable and a.startswith(b):
        return _MATCH - 0.5  # the line growing
    if b.startswith(a):
        return _MATCH - 1.0  # a re-read cut short: weaker than a line growing into it
    r = _ratio(a, b, _SAME_LINE)
    if r >= _SAME_LINE:
        return 2 * r
    if growable and len(a) > len(b) and _ratio(a[: len(b)], b, _MISREAD_LIKE) >= _MISREAD_LIKE:
        # A live line that grew while snapshots were dropped and is misread on top: its
        # known start still lines up. A weak match, but better than a duplicate.
        return _MATCH - 1.5
    return 0.0


def _misread(shown: _Shown, known: _Line) -> bool:
    if not _same_speaker(shown.speaker, known.speaker):
        return False
    a, b = shown.norm, known.norm
    return b.startswith(a[: max(1, len(a) - 1)]) or _ratio(a, b, _MISREAD_LIKE) >= _MISREAD_LIKE


def _align(shown: list[_Shown], tail: list[_Line]) -> list[int | None]:
    """For each shown line, the index into ``tail`` it re-reads, or None (new or a glitch).

    Monotone alignment: shown[i] -> tail[j] pairs never cross. Leading tail lines (scrolled
    off) are free to skip; shown lines past the last aligned one are new; a shown line left
    unaligned above it is a glitch."""
    n, m = len(shown), len(tail)
    neg = float("-inf")
    # best[i][j]: best score with shown[:i] and tail[:j] consumed.
    best = [[neg] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    unpaired = [_GLITCH if s.speaker is None else _UNPAIRED for s in shown]
    for j in range(m + 1):
        best[0][j] = 0.0  # skipping known lines above the window is free
        move[0][j] = "skip_known"
    for i in range(1, n + 1):
        for j in range(m + 1):
            options: list[tuple[float, str]] = [(best[i - 1][j] + unpaired[i - 1], "glitch")]
            if j:
                options.append((best[i][j - 1] + _MISS, "skip_known"))
                if _misread(shown[i - 1], tail[j - 1]):
                    options.append((best[i - 1][j - 1] + _GLITCH, "misread"))
                known = tail[j - 1]
                s = _score(shown[i - 1], known, growable=not known.final or j == m)
                if s > 0:
                    options.append((best[i - 1][j - 1] + s, "match"))
            best[i][j], move[i][j] = max(options)
    # End on the last match: shown lines after it are new, known lines after it should have
    # been on screen. Or match nothing: the whole box is new (old lines scrolled off).
    ends = [(i, j) for i in range(1, n + 1) for j in range(1, m + 1) if move[i][j] == "match"]
    end_i, end_j = max(
        [(0, m), *ends],
        key=lambda ij: best[ij[0]][ij[1]] + _NEW * (n - ij[0]) + _UNSHOWN_NEWEST * (m - ij[1]),
    )
    out: list[int | None] = [None] * n
    i, j = end_i, end_j
    while i > 0:
        step = move[i][j]
        if step == "match":
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == "misread":
            i, j = i - 1, j - 1
        elif step == "skip_known":
            j -= 1
        else:
            i -= 1
    return out


class CaptionMerger:
    def __init__(self, *, start_ms: int, stable_ms: int) -> None:
        self.start_ms = start_ms
        self.stable_ms = stable_ms
        self._lines: list[_Line] = []  # known lines, oldest first
        self.last_speaker: str | None = None

    def _rel(self, ms: int) -> int:
        return max(0, ms - self.start_ms)

    def _finalize(self, line: _Line) -> Segment:
        line.segment = Segment(
            segment_id=uuid.uuid4().hex,
            text=line.text,
            start_ms=self._rel(line.first_ms),
            end_ms=self._rel(max(line.changed_ms, line.first_ms)),
            lang="en",
            speaker=line.speaker,
        )
        return line.segment

    def feed(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        """Fold one caption snapshot in; returns segments to store (new or extended)."""
        shown = [
            _Shown(
                b.speaker if b.has_header else None, text[:_MAX_CHARS], _norm(text[:_MAX_CHARS]), bi
            )
            for bi, b in enumerate(blocks)
            for text in b.lines
            if _norm(text)
        ][-_MAX_SHOWN:]
        tail = [ln for ln in self._lines[-_TAIL:] if ts - ln.seen_ms <= _GONE_MS]
        aligned = _align(shown, tail)
        last_aligned = max((i for i, a in enumerate(aligned) if a is not None), default=-1)
        out: dict[str, Segment] = {}
        to_finalize: list[_Line] = []
        speaker = self.last_speaker
        new_in_snapshot = 0
        for i, (line_shown, at) in enumerate(zip(shown, aligned, strict=True)):
            block = blocks[line_shown.block]
            if block.has_header:
                # A header resets who is talking; an unmatched one (no roster yet) is unknown.
                speaker = block.speaker
            if at is not None:
                line = tail[at]
                line.seen_ms = ts
                if line.speaker is None:
                    line.speaker = speaker
                speaker = line.speaker or speaker
                self._update(line, line_shown, ts, out, bottom=i == len(shown) - 1)
            elif i < last_aligned:
                continue  # an OCR glitch above known lines: never a new line
            else:
                # Lines first seen together keep their on-screen order: each a millisecond
                # after the one above, so stores ordering by start time agree.
                first = ts + new_in_snapshot
                new_in_snapshot += 1
                line = _Line(speaker, line_shown.text, line_shown.norm, first, first, ts)
                self._lines.append(line)
            if speaker is not None:
                self.last_speaker = speaker
            nxt = line_shown.block + 1
            if nxt < len(blocks) and blocks[nxt].has_header and not line.final:
                to_finalize.append(line)
        for line in to_finalize:
            if not line.final:
                seg = self._finalize(line)
                out[seg.segment_id] = seg
        for line in self._lines:
            if not line.final and ts - line.changed_ms >= self.stable_ms:
                seg = self._finalize(line)
                out[seg.segment_id] = seg
        del self._lines[:-_KEEP]
        # Stored in the order they were said, whichever rule finalized them.
        return sorted(out.values(), key=lambda seg: seg.start_ms)

    def _update(
        self, line: _Line, shown: _Shown, ts: int, out: dict[str, Segment], *, bottom: bool
    ) -> None:
        if shown.norm == line.norm:
            return
        grew = shown.norm.startswith(line.norm + " ")
        if not line.final:
            # Only the bottom line is still being spoken (the ASR may rewrite its last
            # words); a line above it changes only by OCR jitter, so it keeps its text.
            if bottom or grew:
                line.text, line.norm, line.changed_ms = shown.text, shown.norm, ts
            return
        # The alignment placed this re-read on a stored line that now reads longer: it was
        # stored while only partly seen (dropped snapshots), so complete it in place. A new
        # sentence that merely starts the same way doesn't get here: a longer line only
        # aligns to the newest line, or to an older one it closely resembles. A re-read that
        # differs otherwise (jitter, a cut-off top line) is ignored.
        seg = line.segment
        assert seg is not None
        if grew and ts - line.changed_ms <= _EXTEND_MS:
            line.text, line.norm, line.changed_ms = shown.text, shown.norm, ts
            seg.text = shown.text
            seg.end_ms = self._rel(ts)
            out[seg.segment_id] = seg

    def flush(self, ts: int) -> list[Segment]:
        """Finalize every line still on screen (the meeting ended)."""
        out = [self._finalize(line) for line in self._lines if not line.final]
        return sorted(out, key=lambda seg: seg.start_ms)
