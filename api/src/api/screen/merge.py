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

Teams also **rewrites** a finished entry (XERK-2694): punctuation added, words fixed, the lines
re-wrapped, so its rows no longer align line by line and would all look new. Rows the
alignment calls new are first offered to the recent known lines it left unshown: a block whose
words revise a run of the same speaker's lines (``_revisions``) updates those lines' segments
in place (same ids, so the store upserts them) instead of adding new ones.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
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
# pair with is a glitch only without a verified speaker header (a scroll fragment): under
# one it is a line said between two snapshots, wherever it sits — above or between known
# lines — and everything below it is new (Zoom only adds lines at the bottom).
_UNPAIRED = float("-inf")
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
# A rewritten entry (XERK-2694): new rows revise known lines the box no longer shows when they
# keep this share of those lines' words, in order (case and punctuation aside). Only lines
# first seen this recently can be revised: Teams rewrites an entry seconds after it ends.
_REVISED = 0.6
_REVISE_MS = 30_000
_WORD = re.compile(r"[\w']+")


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
    carried: bool = False  # the ended meeting's line, seeded into the next merger

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
                # A carried line can't grow: its segment is the ended meeting's, so the
                # speaker going on after the break is a new line, not lost in the old one.
                growable = not known.carried and (not known.final or j == m)
                s = _score(shown[i - 1], known, growable=growable)
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


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _rewrapped(shown: _Shown, known: _Line, rows: list[_Shown], lines: list[_Line]) -> bool:
    """Whether ``shown`` differs from ``known`` only by words a re-wrap moved across a line
    break: each word it gained is in a neighbouring known line, each it lost is in a
    neighbouring shown row. Anything else (a misread, a stray char, a clipped top row) is
    OCR, which never overwrites a stored line (XERK-2597)."""
    mine, theirs = Counter(_words(shown.text)), Counter(_words(known.text))
    if mine == theirs:
        return True  # punctuation or case only
    near_known = Counter(w for ln in lines if ln is not known for w in _words(ln.text))
    near_shown = Counter(w for r in rows for w in _words(r.text))
    return not (mine - theirs - near_known) and not (theirs - mine - near_shown)


def _revisions(
    shown: list[_Shown],
    speakers: list[str | None],
    aligned: list[int | None],
    tail: list[_Line],
    ts: int,
) -> dict[int, int]:
    """For new shown rows that revise known lines, the index into ``tail`` each one rewrites.

    New rows are those below the last aligned one. Each block's run of them is compared, by
    words, with the next run of the same speaker's recent lines after the last aligned known
    line (the lines the box should still show but the alignment couldn't place). When the
    rows keep ``_REVISED`` of that run's words in order, each row takes the first unclaimed
    line its words matched, until a row matches none: it and the rows after it stay new.

    A block of only new rows under its own header that revises nothing is a fresh entry:
    everything below it is newer (a repeat there is said again), so revising stops."""
    last = max((i for i, a in enumerate(aligned) if a is not None), default=-1)
    start = (aligned[last] or 0) + 1 if last >= 0 else 0
    cands = [
        j
        for j in range(start, len(tail))
        if not tail[j].carried and ts - tail[j].first_ms <= _REVISE_MS
    ]
    runs: list[list[int]] = []
    for i in range(last + 1, len(shown)):
        if runs and shown[runs[-1][0]].block == shown[i].block:
            runs[-1].append(i)
        else:
            runs.append([i])
    out: dict[int, int] = {}
    p = 0  # cands before p are taken (or skipped over by an earlier revision)
    for rows in runs:
        first = rows[0]
        fresh = shown[first].speaker is not None and (
            first == 0 or shown[first - 1].block != shown[first].block
        )
        speaker = speakers[first]
        q = next(
            (k for k in range(p, len(cands)) if _same_speaker(speaker, tail[cands[k]].speaker)),
            None,
        )
        if q is None:
            if fresh:
                break
            continue
        end = q
        while (
            end < len(cands)
            and tail[cands[end]].speaker == tail[cands[q]].speaker
            and (end == q or cands[end] == cands[end - 1] + 1)
        ):
            end += 1
        draft = [(w, j) for j in cands[q:end] for w in _words(tail[j].text)]
        mine = [(w, i) for i in rows for w in _words(shown[i].text)]
        sm = SequenceMatcher(None, [w for w, _ in draft], [w for w, _ in mine], autojunk=False)
        pairs = [
            (mine[mb.b + k][1], draft[mb.a + k][1])
            for mb in sm.get_matching_blocks()
            for k in range(mb.size)
        ]
        # Claims are a prefix of the run: a row above them pairing with nothing is a line said
        # before them, so they are said again ("okay", "sounds good" after "sounds good").
        claims: dict[int, int] = {}
        taken = -1
        for i in rows:
            j = next((j for r, j in pairs if r == i and j > taken), None)
            if j is None:
                break
            claims[i] = taken = j
        if not claims or len(pairs) < _REVISED * len(draft):
            if fresh:
                break
            continue
        out.update(claims)
        p = end
    # The box always shows the newest lines: revisions that leave a newer known line unshown
    # are a new line that merely resembles an older one ("okay", Bo, "okay so lets start").
    return out if p == len(cands) else {}


def _shown(blocks: list[CaptionBlock]) -> list[_Shown]:
    return [
        _Shown(b.speaker if b.has_header else None, text[:_MAX_CHARS], _norm(text[:_MAX_CHARS]), bi)
        for bi, b in enumerate(blocks)
        for text in b.lines
        if _norm(text)
    ][-_MAX_SHOWN:]


class CaptionMerger:
    def __init__(
        self, *, start_ms: int, stable_ms: int, carried: list[_Line] | None = None
    ) -> None:
        self.start_ms = start_ms
        self.stable_ms = stable_ms
        # Known lines, oldest first. ``carried`` are the stored lines of the meeting that
        # last ended on this window (XERK-2615): a panel still on screen after the end timer
        # re-reads them, and they must align as known, not be stored again. Copies, seen
        # now (so _GONE_MS ages them from here) and too old to extend: their segments
        # belong to the ended conversation, never this one.
        self._lines: list[_Line] = [
            _Line(
                ln.speaker,
                ln.text,
                ln.norm,
                ln.first_ms,
                min(ln.changed_ms, start_ms - _EXTEND_MS - 1),
                start_ms,
                ln.segment,
                carried=True,
            )
            for ln in carried or []
            if ln.final
        ]
        self.last_speaker: str | None = None
        # Lines this merger has seen for the first time (carried ones excluded).
        self.new_lines = 0

    def recent(self) -> list[_Line]:
        """The newest known lines, for the window's next merger (``carried``)."""
        return self._lines[-_TAIL:]

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

    def _tail(self, ts: int) -> list[_Line]:
        return [ln for ln in self._lines[-_TAIL:] if ts - ln.seen_ms <= _GONE_MS]

    def _speakers(
        self,
        blocks: list[CaptionBlock],
        shown: list[_Shown],
        aligned: list[int | None],
        tail: list[_Line],
    ) -> list[str | None]:
        """Who says each shown line: its block's header, else the line it re-reads, else the
        speaker above it (``feed`` walks the rows the same way)."""
        out: list[str | None] = []
        speaker = self.last_speaker
        for line_shown, at in zip(shown, aligned, strict=True):
            block = blocks[line_shown.block]
            if block.has_header:
                speaker = block.speaker
            if at is not None:
                speaker = tail[at].speaker or speaker
            out.append(speaker)
        return out

    def _match(
        self, blocks: list[CaptionBlock], ts: int
    ) -> tuple[list[_Shown], list[_Line], list[int | None], dict[int, int]]:
        shown = _shown(blocks)
        tail = self._tail(ts)
        aligned = _align(shown, tail)
        revised = _revisions(shown, self._speakers(blocks, shown, aligned, tail), aligned, tail, ts)
        return shown, tail, aligned, revised

    def knows_any(self, blocks: list[CaptionBlock], ts: int) -> bool:
        """Whether a line of this snapshot re-reads a recent known line (it aligns to one).

        A header-less Teams crop mid-meeting is a long entry whose header scrolled off, and it
        still shows lines the last snapshot did; one sharing none is other text in the band
        (a slide, XERK-2614)."""
        _, _, aligned, revised = self._match(blocks, ts)
        return bool(revised) or any(a is not None for a in aligned)

    def feed(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        """Fold one caption snapshot in; returns segments to store (new or extended)."""
        shown, tail, aligned, revised = self._match(blocks, ts)
        last_aligned = max((i for i, a in enumerate(aligned) if a is not None), default=-1)
        out: dict[str, Segment] = {}
        to_finalize: list[_Line] = []
        speaker = self.last_speaker
        new_in_snapshot = 0
        # The known line each row re-reads or revises. A new line goes just above the known
        # line shown below it, so known lines keep on-screen order: Teams adds lines to an
        # entry above another speaker's (that entry went on). Otherwise it goes at the end.
        known_at = [tail[a] if a is not None else None for a in aligned]
        revised_blocks = {shown[i].block for i in revised}
        for i, j in revised.items():
            known_at[i] = tail[j]
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
                if line_shown.block in revised_blocks and _rewrapped(
                    line_shown,
                    line,
                    shown[i - 1 : i] + shown[i + 1 : i + 2],
                    tail[max(0, at - 1) : at + 2],
                ):
                    # A row of a rewritten entry the alignment still matched: the rewrite moved
                    # words onto it ("...it, the how" over "...it the").
                    self._revise(line, line_shown, ts, out)
                else:
                    self._update(line, line_shown, ts, out, bottom=i == len(shown) - 1)
            elif i in revised:
                line = tail[revised[i]]
                line.seen_ms = ts
                if line.speaker is None:
                    line.speaker = speaker
                speaker = line.speaker or speaker
                self._revise(line, line_shown, ts, out)
            elif i < last_aligned:
                continue  # an OCR glitch above known lines: never a new line
            else:
                # Lines first seen together keep their on-screen order: each a millisecond
                # after the one above, so stores ordering by start time agree.
                first = ts + new_in_snapshot
                new_in_snapshot += 1
                line = _Line(speaker, line_shown.text, line_shown.norm, first, first, ts)
                below = next((ln for ln in known_at[i + 1 :] if ln is not None), None)
                at_ = len(self._lines)
                if below is not None:
                    at_ = next(k for k in range(len(self._lines)) if self._lines[k] is below)
                self._lines.insert(at_, line)
                self.new_lines += 1
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

    def _revise(self, line: _Line, shown: _Shown, ts: int, out: dict[str, Segment]) -> None:
        """A rewritten entry's row replaces the known line it revises, stored or not."""
        if shown.norm == line.norm:
            return
        line.text, line.norm, line.changed_ms = shown.text, shown.norm, ts
        if line.segment is not None:
            # end_ms stays when it was said: the rewrite lands after others spoke.
            line.segment.text = shown.text
            out[line.segment.segment_id] = line.segment

    def flush(self, ts: int) -> list[Segment]:
        """Finalize every line still on screen (the meeting ended)."""
        out = [self._finalize(line) for line in self._lines if not line.final]
        return sorted(out, key=lambda seg: seg.start_ms)
