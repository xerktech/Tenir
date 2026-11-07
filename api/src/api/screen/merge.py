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

Teams also **re-flows** an entry as it grows (XERK-2715): a word inserted or fixed moves words
across its row breaks, so rows gain leading words and land as new lines beside stale drafts.
After the rows are placed, each block is upserted as one entry (``_reflow``): when a word
crossed a break, its rows replace the texts of the lines they landed on, in order, and lines
left between them whose words the rows now hold are dropped (stored ones re-emitted blank).
"""

from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from collections.abc import Hashable, Sequence
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
# A re-flowed entry (XERK-2715): a line its rows skip is a stale copy when they hold this share
# of its words where it sat (Teams may also fix one of them).
_HELD = 0.5
# A grown entry above the same speaker's next one (XERK-2717) cuts its draft at that next entry
# only when it was on screen this recently: at 1 fps a new line and a repeat of her next entry
# can't both be said between two snapshots, but after dropped frames they can (XERK-2716).
_SAID_BETWEEN_MS = 3_000
# A new entry Teams shows between two known ones (XERK-2727): costs more than a misread, so a
# row that is one keeps that pairing (and ``_grew_over_twin``).
_INSERTED = -1.5
# A frame re-reading the last one's rows under other header names re-attributes them
# (XERK-2736) only this soon after it (one frame at 1 fps, with jitter: after a dropped frame
# the rows may be said again), and only for an entry of this many words (a reply that short
# is said whole between two frames).
_REHEAD_MS = 1_500
_REHEAD_WORDS = 3
_WORD = re.compile(r"[\w']+")
# Alignment work a window may spend (XERK-2730): a token bucket refilled by snapshot time.
# Crafted rows (12 x 160 chars of "a b a b") cost difflib ~1.5 s CPU per frame on the shared
# 2-thread CPU_POOL; real ones cost a few ms. Work is counted, not timed: thread CPU time
# doubles on a busy node, and dropping real frames for it made the merger store lines again.
# A unit is ``_work``'s estimate of difflib's steps; ~5M take 1 s of CPU on crafted rows
# (2.2M at worst, short varied ones). A dense 12 x 160-char English box with OCR noise on most
# rows needs ~490k a frame (peaks 1.05M) at the 1 fps meeting cadence; a crafted one ~8M.
# Per window, and a user's bucket holds two (``ScreenMeetings``): at 600k that box dropped
# frames (each refusal wastes the work it did), and the merger re-stores lines across dropped
# frames. Measured: one user's 8 crafted windows spend ~0.2-0.3 s CPU a second (was ~8).
# A frame finding the bucket short is dropped before the merger changes anything.
_WORK_PER_S = 1_000_000
_WORK_BURST = 4_000_000
# Similarities a window remembers: a box re-shows most rows and known lines every frame.
_MEMO = 1024


class OverBudget(Exception):
    """The snapshot would cost more alignment work than its window has left; not taken."""


def _work(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """What difflib's ratio of ``a`` and ``b`` costs: its first longest-match pass (each item
    of ``a`` visits every place ``b`` holds it: quadratic for "a b a b") plus a share of
    ``len(a) * len(b)`` for the passes its recursion makes over varied text. Fitted on 160-char
    pairs from 2 to 94 distinct chars: CPU per unit within 4x across them."""
    ca, cb = Counter(a), Counter(b)
    return sum(n * cb[x] for x, n in ca.items() if x in cb) + len(a) * len(b) // 4


class MergeBudget:
    """A capture window's alignment-work bucket and its memo of line similarities.

    Held by the window's tracker (its user's, via ``ScreenMeetings``), never a merger: after a
    meeting ends every frame gets a fresh merger, and a bucket per merger refilled on each
    (QA). Only ``_match`` refuses (``charge``, before the work it pays for), so a refused frame
    has changed nothing; later work ``spend``s into debt. Counted, not timed: thread CPU time
    grows on a busy node."""

    def __init__(self, windows: int = 1) -> None:
        # ``windows``: how many windows' worth of work it refills and holds (a user's bucket).
        self.rate = _WORK_PER_S * windows
        self.burst = _WORK_BURST * windows
        self.left = self.burst
        self.memo_cap = _MEMO * windows
        self.ts: int | None = None
        self.memo: dict[tuple[str, str], float] = {}

    def start(self, ts: int) -> None:
        """Refill for the snapshot time since the last frame. ``ts`` is the tracker's clock:
        monotonic and within a minute of the server's, so it can't refill faster than time.
        A user's windows share one bucket: only a ts past the newest seen refills it."""
        if self.ts is not None:
            self.left = min(self.burst, self.left + self.rate * max(0, ts - self.ts) // 1000)
        self.ts = ts if self.ts is None else max(ts, self.ts)
        if self.left < 0:  # in debt for work the last frames did after ``_match``
            raise OverBudget

    def charge(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> None:
        """Pay for comparing ``a`` and ``b``, or raise ``OverBudget``. A refused frame keeps
        what is left: emptying the bucket let a real frame needing more than one second's
        refill never pass (each retry refused again)."""
        cost = _work(a, b)
        if cost > self.left:
            raise OverBudget
        self.left -= cost

    def spend(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> None:
        """Pay for comparing ``a`` and ``b`` where a frame can't be refused (after ``_match``,
        the merger is changing): the bucket may go into debt, and ``start`` refuses frames
        until it is refilled. Unmetered, a crafted box re-sent unchanged cost 0 units and
        ~130 ms of ``_reflow`` a frame (QA)."""
        self.left -= _work(a, b)

    def ratio(self, a: str, b: str, floor: float) -> float:
        """Similarity of two lines; 0.0 when it is certainly below ``floor`` (difflib's cheap
        upper bounds rule most pairs out without the quadratic comparison). The full ratio is
        remembered per pair: callers only compare it with their floor, so any floor reuses it."""
        r = self.memo.pop((a, b), None)
        if r is not None:
            self.memo[(a, b)] = r  # most recently used last: eviction takes the oldest use
            return r
        sm = SequenceMatcher(None, a, b, autojunk=False)
        # Setting up and bounding a pair is linear, but 12 x 20 pairs a frame add up (QA).
        self.left -= len(a) + len(b)
        if sm.real_quick_ratio() < floor or sm.quick_ratio() < floor:
            return 0.0
        self.charge(a, b)
        r = sm.ratio()
        if len(self.memo) >= self.memo_cap:
            del self.memo[next(iter(self.memo))]
        self.memo[(a, b)] = r
        return r


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


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
    # A matched row of a rewritten entry: until ``rewrite_until`` it takes the first differing
    # text two reads in a row agree on (``pending``: the last such read).
    pending: str | None = None
    rewrite_until: int = 0
    # A read with words added that came with its neighbours changing (``_apply_grows``): a
    # re-flow, never taken as growth, even once those neighbours scroll off (QA).
    grow_refused: str | None = None
    # Was shown as the first row under a header: on Teams it starts an entry, where a rewrite
    # of the entry above may stop (``_revisions``, XERK-2720).
    head: bool = False
    # Where the store orders it (its segment's start), when not ``first_ms``: a line Teams adds
    # to an entry above a known line is ordered just above it (``_place_above``, XERK-2729).
    store_ms: int | None = None

    @property
    def final(self) -> bool:
        return self.segment is not None

    @property
    def order_ms(self) -> int:
        return self.first_ms if self.store_ms is None else self.store_ms


@dataclass
class _Shown:
    """One line of a snapshot, top to bottom."""

    speaker: str | None  # the block's header speaker; None for a header-less block
    text: str
    norm: str
    block: int


def _score(shown: _Shown, known: _Line, budget: MergeBudget, *, growable: bool) -> float:
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
    r = budget.ratio(a, b, _SAME_LINE)
    if r >= _SAME_LINE:
        return 2 * r
    if growable and len(a) > len(b) and budget.ratio(a[: len(b)], b, _MISREAD_LIKE) >= _MISREAD_LIKE:
        # A live line that grew while snapshots were dropped and is misread on top: its
        # known start still lines up. A weak match, but better than a duplicate.
        return _MATCH - 1.5
    return 0.0


def _misread(shown: _Shown, known: _Line, budget: MergeBudget, *, in_entry: bool = False) -> bool:
    """Whether ``shown`` misreads ``known``. ``in_entry``: ``shown`` is not its block's top row."""
    if not _same_speaker(shown.speaker, known.speaker):
        return False
    a, b = shown.norm, known.norm
    if b.startswith(a[: max(1, len(a) - 1)]) or budget.ratio(a, b, _MISREAD_LIKE) >= _MISREAD_LIKE:
        return True
    # A finished Teams row re-flowing inside its entry: punctuation changed and words joined
    # it ("green." -> "green, so ship it", XERK-2716). Unpaired under a header it read as a
    # line said between snapshots and the whole box was stored again; paired, ``_reflow``
    # takes its words. Only below another row of its block: a block's top row may be a new
    # line starting with an old one's words ("yes." / "yes I think so too."), and a glitch
    # there has no row above for ``_reflow`` to place it by (both lost words, QA).
    if not in_entry:
        return False
    had = _words(known.text)
    return bool(had) and _words(shown.text)[: len(had)] == had


def _moved_up(
    line: _Line, shown: _Shown, below: tuple[_Shown, _Line | None] | None, *, said_on: bool
) -> bool:
    """Whether the words ``shown`` adds to ``line`` are no growth to take on one frame: only
    one-char tokens on a line the speaker has said on from (``said_on``: a stray glyph on a
    finished entry, "notes l"), or the leading words of the known line below that its row no
    longer starts with (Teams re-flowed them up across the row break). Her newest line still
    takes "I", "B" or "5": it was finalized by the next speaker mid-sentence ("so do I"), and
    ``_inserted`` refuses 1-char growth on every frame, so refusing it here lost it (QA)."""
    added = _words(shown.text)[len(_words(line.text)) :]
    if not added:
        return True
    if all(len(w) == 1 for w in added):
        return said_on
    if below is None or below[1] is None:
        return False
    row, known = _words(below[0].text), _words(below[1].text)
    return known[: len(added)] == added and row[: len(added)] != added


def _exact(shown: _Shown, known: _Line) -> bool:
    return shown.norm == known.norm and _same_speaker(shown.speaker, known.speaker)


def _same_text(a: str, b: str) -> bool:
    """Whether two normalized lines read the same text: exactly, or one char of one word
    misread ("0kay." for "okay.", XERK-2727). Looser (same word count, ratio >= 0.75) took
    different lines for twins and re-stored whole boxes (QA). A line grown by a word is not."""
    if a == b:
        return True
    wa, wb = _words(a), _words(b)
    if len(wa) != len(wb):
        return False
    diff = [(x, y) for x, y in zip(wa, wb, strict=True) if x != y]
    if len(diff) > 1:
        return False
    if not diff:
        return True  # case and punctuation aside, the same words
    x, y = diff[0]
    return len(x) == len(y) and sum(c != d for c, d in zip(x, y, strict=True)) == 1


def _twin(shown: _Shown, known: _Line) -> bool:
    """Whether ``shown`` reads ``known``'s text (``_same_text``), by its speaker."""
    return _same_speaker(shown.speaker, known.speaker) and _same_text(shown.norm, known.norm)


def _reread_below(shown: list[_Shown], i: int, tail: list[_Line], k: int) -> bool:
    """Whether rows from ``shown[i]`` on read ``tail[k]``'s text exactly more often than the
    known lines after it hold it (so one of them re-reads ``tail[k]`` itself), and an earlier
    known line holds it too (one ``shown[i - 1]`` may have grown from). Without the count,
    the live bottom twin growing ([notes][notes the think]) stored a third "notes"; without
    the earlier copy, a garbled top row over a repeat said since was stored (both QA)."""
    known = tail[k]
    if known.carried:  # the ended meeting's line keeps main's behaviour
        return False
    below = sum(1 for s in shown[i:] if _twin(s, known))
    after = sum(1 for ln in tail[k + 1 :] if ln.norm == known.norm)
    return below > after and any(
        ln.norm == known.norm and ln.speaker == known.speaker and not ln.carried
        for ln in tail[:k]
    )


def _grew_over_twin(shown: list[_Shown], i: int, tail: list[_Line], k: int) -> bool:
    """Whether ``shown[i - 1]``, aligned as a misread of ``tail[k]``, is it grown by whole
    words while a row below reads a later same-text known line (XERK-2719): ["notes the
    think"]["notes"] over stored notes/notes. ``feed`` revises ``tail[k]`` with it; refusing
    the misread instead left the row nothing to pair with, and the box was stored again."""
    known = tail[k]
    if known.carried:
        return False
    had, now = _words(known.text), _words(shown[i - 1].text)
    return (
        # A row re-reading another known line exactly is that line, misread-paired with the
        # one it starts with; revising made a second copy of it (QA, fuzz seeds 183, 161).
        not any(_exact(shown[i - 1], ln) for ln in tail if ln is not known)
        and len(now) > len(had)
        and now[: len(had)] == had
        and any(_same_text(ln.norm, known.norm) for ln in tail[k + 1 :])
        and any(_twin(s, known) for s in shown[i:])
    )


def _inserted_entry(
    shown: list[_Shown], i: int, tail: list[_Line], j: int, *, above_matched: bool
) -> bool:
    """Whether ``shown[i]`` is a new Teams entry right after ``tail[j]``, above the known
    lines below it (XERK-2727): the first row under a header, of ``tail[j]``'s speaker, that
    starts with its words and adds more. Unpaired under a header it cost ``_UNPAIRED`` and
    the known entries below were stored again. Only where ``tail[j]`` can't be revised
    (``_revisions`` takes a skipped draft): a carried line (it never grows; its growth is
    a new line), or one the row above is aligned to (the entry stays, so this one is new).
    ``above_matched``: the alignment matched ``shown[i - 1]`` with ``tail[j]``. Not when
    another of the speaker's known lines reads like ``tail[j]``: the row above may be that
    one, and this row ``tail[j]`` grown (QA: "yes", "yes" -> "yes maybe please")."""
    row, known = shown[i], tail[j]
    if row.speaker is None or row.speaker != known.speaker:
        return False
    if i and shown[i - 1].block == row.block:
        return False
    had, now = _words(known.text), _words(row.text)
    if not had or len(now) <= len(had) or now[: len(had)] != had:
        return False
    if any(_exact(row, ln) for ln in tail):
        # It re-reads a known line: that line, wrapped or re-flowed, not a new entry (QA:
        # the line was stored twice and the word wrapped below it lost).
        return False
    if known.carried:
        return True
    return above_matched and not any(
        ln is not known and ln.speaker == known.speaker and _same_text(ln.norm, known.norm)
        for ln in tail
    )


def _align(
    shown: list[_Shown],
    tail: list[_Line],
    budget: MergeBudget,
    misreads: dict[int, int] | None = None,
    inserted: set[int] | None = None,
) -> list[int | None]:
    """For each shown line, the index into ``tail`` it re-reads, or None (new or a glitch).

    Monotone alignment: shown[i] -> tail[j] pairs never cross. Leading tail lines (scrolled
    off) are free to skip; shown lines past the last aligned one are new; a shown line left
    unaligned above it is a glitch. ``misreads`` (when given) gets each glitch the alignment
    paired with a known line: shown index -> tail index. ``inserted`` (when given) gets each
    unaligned row above the last match that is a new entry (``_inserted_entry``), not a glitch."""
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
            if j and _inserted_entry(
                shown, i - 1, tail, j - 1, above_matched=i >= 2 and move[i - 1][j] == "match"
            ):
                options.append((best[i - 1][j] + _INSERTED, "inserted"))
            if j:
                options.append((best[i][j - 1] + _MISS, "skip_known"))
                in_entry = i >= 2 and shown[i - 2].block == shown[i - 1].block
                if _misread(shown[i - 1], tail[j - 1], budget, in_entry=in_entry) or (
                    # A block's top row grown over a twin row of its line (XERK-2727):
                    # [okay, ship it][0kay.] over okay./okay. paired nothing when the twin
                    # was misread, so ``_revisions``' verbatim cut missed it too.
                    not in_entry and _grew_over_twin(shown, i, tail, j - 1)
                ):
                    options.append((best[i - 1][j - 1] + _GLITCH, "misread"))
                known = tail[j - 1]
                # A carried line can't grow: its segment is the ended meeting's, so the
                # speaker going on after the break is a new line, not lost in the old one.
                growable = not known.carried and (not known.final or j == m)
                s = _score(shown[i - 1], known, budget, growable=growable)
                if 0 < s < _MATCH and _reread_below(shown, i, tail, j - 1):
                    # A row below is the re-read of this line, and an earlier copy of it is
                    # what this row grew from (XERK-2719): Teams grew an entry above a
                    # same-text one ("okay." -> "okay, ship it" over "okay."). Pairing the
                    # grown row with the newest "okay." stored the row below on every frame.
                    s = 0.0
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
            if misreads is not None:
                misreads[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == "skip_known":
            j -= 1
        else:
            if step == "inserted" and inserted is not None:
                inserted.add(i - 1)
            i -= 1
    return out


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _subsequence(was: list[str], now: list[str]) -> bool:
    """Whether ``now`` holds every word of ``was``, in order."""
    it = iter(now)
    return all(w in it for w in was)


def _inserted(was: list[str], now: list[str]) -> bool:
    """Whether ``now`` keeps every word of ``was``, in order, and adds whole words that read
    as speech: Teams finishing or rewording an entry (XERK-2721). OCR jitter garbles or
    splits words, so it loses one of ``was``'s; a stray glyph read as a word ("x", "l"), a
    run of added words with no word of 2+ chars, is jitter too: static pixels repeat that
    misread on every frame (QA), as is a doubled thin stroke ("ll", ``_spoken``). A
    doubled word, or "a"/"i" inside a longer run ("and a fix"), is kept: refusing them lost growth main stored (QA)."""
    if not was or len(now) <= len(was):
        return False
    added: list[int] = []
    k = 0
    for n, w in enumerate(now):
        if k < len(was) and w == was[k]:
            k += 1
        else:
            added.append(n)
    if k < len(was):
        return False
    runs: list[list[str]] = []
    for n in added:
        if runs and n - 1 in added:
            runs[-1].append(now[n])
        else:
            runs.append([now[n]])
    return all(any(_spoken(w) for w in run) for run in runs)


def _spoken(word: str) -> bool:
    """Whether an added word can be speech rather than a stray glyph OCR reads as a word: one
    letter is a glyph ("x", "l"), and so is a thin stroke read twice ("ll", "ii"; XERK-2728).
    Other two-letter tokens are kept: "PM", "Dr", "vs", "mm" are speech (QA)."""
    if len(word) < 2:
        return False
    return not (len(word) == 2 and word[0] == word[1] and word[0] in "iljtfr")


def _edited(was: list[str], now: list[str], budget: MergeBudget) -> bool:
    """Whether ``now`` may replace a stored line reading ``was``: Teams finishing or rewording
    an entry (XERK-2721, XERK-2732). ``now`` adds whole words that read as speech, and may
    drop some of ``was``'s.

    Added words are speech as for ``_inserted`` (``_spoken``: no stray glyph runs).

    Dropped words (XERK-2732): OCR loses words too (a missed or garbled one), and a miss on
    static pixels repeats on every frame, so no frame count tells it from a Teams edit (QA).
    A read that only drops words is never taken. One that drops words beside an added word
    (Teams rewording: "build on friday" -> "new build friday") is, when it keeps half the
    words and the row's first and last (a read cut off at either end is the crop clipping
    it), and adds nothing resembling what it dropped (a garble splits "should" into "sh
    uld"), span or word: a reword inflecting a word it keeps ("to test it" -> "tested it")
    keeps the draft. Holding it for more frames than an insertion lost words on every fuzz."""
    if not was or not now or now == was:
        return False
    if _inserted(was, now):
        return True
    # Not every word kept in order: a drop, scored on difflib's alignment. An insertion
    # ``_inserted`` refused (a stray glyph) or one difflib aligns oddly drops nothing here.
    budget.spend(was, now)  # after ``_match``: metered, never refused (``MergeBudget``)
    ops = SequenceMatcher(None, was, now, autojunk=False).get_opcodes()
    kept = sum(a2 - a1 for op, a1, a2, _, _ in ops if op == "equal")
    added = [now[b1:b2] for op, _, _, b1, b2 in ops if op in ("insert", "replace")]
    dropped = [was[a1:a2] for op, a1, a2, _, _ in ops if op in ("delete", "replace")]
    if not dropped or not added or not all(any(_spoken(w) for w in run) for run in added):
        return False
    if _subsequence(was, now):
        return False  # every word kept: ``_inserted`` refused the added ones
    if 2 * kept < len(was) or ops[0][0] != "equal" or ops[-1][0] != "equal":
        return False
    for op, a1, a2, b1, b2 in ops:
        if op != "replace":
            continue
        # A garble beside a missed word folds into one span ("ship the" -> "th"): compare
        # each added word with each dropped one too, not only the spans (QA).
        pairs = [(" ".join(was[a1:a2]), " ".join(now[b1:b2]))]
        pairs += [(d, w) for d in was[a1:a2] for w in now[b1:b2]]
        if any(_like(d, w, budget) for d, w in pairs):
            return False
    return True


def _like(a: str, b: str, budget: MergeBudget) -> bool:
    """Whether ``b`` reads like a misread of ``a`` (``_MISREAD_LIKE``), metered by ``spend``:
    ``_edited`` runs after ``_match``, where ``MergeBudget.ratio`` could refuse mid-frame."""
    sm = SequenceMatcher(None, a, b, autojunk=False)
    if sm.real_quick_ratio() < _MISREAD_LIKE or sm.quick_ratio() < _MISREAD_LIKE:
        return False
    budget.spend(a, b)
    return sm.ratio() >= _MISREAD_LIKE


def _revisions(
    shown: list[_Shown],
    speakers: list[str | None],
    aligned: list[int | None],
    tail: list[_Line],
    ts: int,
    budget: MergeBudget,
    merged: list[tuple[int, tuple[int, ...]]] | None = None,
) -> dict[int, int]:
    """For new shown rows that revise known lines, the index into ``tail`` each one rewrites.
    ``merged`` (when given) gets the draft lines a rewrite folded into its rows (``_claims``).

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
    folds: list[tuple[int, tuple[int, ...]]] = []
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
        # The draft is the speaker's whole run of unshown lines, cut first where the next
        # block is hers and re-reads one of them verbatim: Teams shows each entry as its own
        # block, so the run then holds her next entries too, and a one-row entry that grew
        # ("green." -> "green, so ship it") covered too few of its words (XERK-2717). Cuts go
        # first: a full draft passing coverage swallowed a short entry after the next one
        # ("ok."), which was then stored again. Someone else repeating her line cuts nothing,
        # and nor does a line last seen before dropped frames (``_SAID_BETWEEN_MS``).
        # Failing all those, the draft up to her next entry, a line first shown under a header
        # (XERK-2720): her next entry still growing, misread or re-punctuated re-reads none of
        # her lines verbatim. Only last: Zoom heads a speaker's run once (a scrolled-up row
        # looks like an entry start), and cutting there first lost revisions in Zoom
        # simulations.
        nxt = next((r[0] for r in runs if r[0] > rows[-1]), None)
        # Either cut needs the cut entry itself seen within ``_SAID_BETWEEN_MS``: a short entry
        # scrolled off is no longer there to rewrite, and a later row holding its few words
        # claimed it (QA). Not "on the last frame": one frame of OCR missing her block then
        # stored the grown entry twice again (QA).
        recent = ts - tail[cands[q]].seen_ms <= _SAID_BETWEEN_MS
        cuts = [
            k
            for k in range(q + 1, end)
            if recent
            and nxt is not None
            and speakers[nxt] == tail[cands[q]].speaker
            and shown[nxt].norm == tail[cands[k]].norm
            and ts - tail[cands[k]].seen_ms <= _SAID_BETWEEN_MS
        ]
        ends = [*reversed(cuts), end]
        own = next((k for k in range(q + 1, end) if tail[cands[k]].head), end)
        if own not in ends and recent:
            ends.append(own)
        found = None
        for e in ends:
            folded: list[tuple[int, tuple[int, ...]]] = []
            if c := _claims(shown, rows, tail, cands[q:e], budget, folded):
                found = (e, c)
                break
        if found is None:
            if fresh:
                break
            continue
        p, claims = found
        out.update(claims)
        folds += folded
    # The box always shows the newest lines: revisions that leave a newer known line unshown
    # are a new line that merely resembles an older one ("okay", Bo, "okay so lets start").
    if p != len(cands):
        return {}
    if merged is not None:
        merged += folds
    return out


def _claims(
    shown: list[_Shown],
    rows: list[int],
    tail: list[_Line],
    draft_lines: list[int],
    budget: MergeBudget,
    folded: list[tuple[int, tuple[int, ...]]] | None = None,
) -> dict[int, int] | None:
    """The known line (index into ``tail``) each row of ``rows`` revises, or None when the
    rows keep under ``_REVISED`` of ``draft_lines``' words in order.

    ``folded`` (when given) gets the draft lines no row claims but whose every word a claiming
    row holds, between two claimed lines or after the last, each with the claimed lines: a
    rewrite with fewer rows than its draft ("we did it in test first so" / "nothing broke" ->
    one row) folded them into the rows, and kept they were stored twice (XERK-2701)."""
    draft = [(w, j) for j in draft_lines for w in _words(tail[j].text)]
    mine = [(w, i) for i in rows for w in _words(shown[i].text)]
    budget.charge([w for w, _ in draft], [w for w, _ in mine])
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
        return None
    if folded is not None:
        # Draft word positions that pair with a word of a row that claimed a line.
        paired = {
            mb.a + k
            for mb in sm.get_matching_blocks()
            for k in range(mb.size)
            if mine[mb.b + k][1] in claims
        }
        first = min(claims.values())
        claimed = tuple(claims.values())
        for j in draft_lines:
            at = {n for n, (_, line) in enumerate(draft) if line == j}
            if j > first and j not in claims.values() and at and at <= paired:
                folded.append((j, claimed))
    return claims


def _junction(words: list[str], above: list[str], below: list[str]) -> bool:
    """Whether ``_HELD`` of ``words`` now sit at the break between two rows: its first words
    end the row above and its last words start the row below (a re-flow moved them out)."""
    up = max((k for k in range(1, len(words) + 1) if above[-k:] == words[:k]), default=0)
    down = max(
        (k for k in range(1, len(words) - up + 1) if below[:k] == words[len(words) - k :]),
        default=0,
    )
    return up + down >= _HELD * len(words)


def _share(words: list[str], within: list[str], budget: MergeBudget) -> bool:
    """Whether ``within`` holds ``_HELD`` of ``words``, in order."""
    budget.spend(words, within)
    sm = SequenceMatcher(None, words, within, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) >= _HELD * len(words)


def _alike(a: list[str], b: list[str], budget: MergeBudget) -> bool:
    """Whether two rows' words mostly agree, in order (Teams may fix a word as it re-flows)."""
    if not a or not b:
        return a == b
    budget.spend(a, b)
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= _REVISED


def _moved(was: list[list[str]], now: list[list[str]], budget: MergeBudget) -> bool:
    """Whether a word crossed a row break (Teams re-flowed the entry). A word changed within
    a row (OCR jitter) moves no break.

    Either a changed row now starts with the word that ended the row above it, or ends with
    the one that started the row below (the moved word may still be on its old row too, when
    that row is the stale draft); or the rows' words, matched against the lines' in order,
    put a break between two words the lines kept together (or the reverse)."""
    for k in range(min(len(was), len(now)) - 1):
        a, b, c, d = was[k], was[k + 1], now[k], now[k + 1]
        # Down: row k lost its last word to the start of row k+1, and otherwise reads as before.
        if a and c and d and a[-1] == d[0] and c[-1] != a[-1] and _alike(c, a[:-1], budget):
            return True
        # Up: row k gained the word that started row k+1, which no longer starts it.
        if b and c and d and b[0] == c[-1] and d[0] != b[0] and _alike(c[:-1], a, budget):
            return True
    a = [w for ws in was for w in ws]
    b = [w for ws in now for w in ws]
    breaks_a = {sum(map(len, was[: k + 1])) for k in range(len(was) - 1)}
    breaks_b = {sum(map(len, now[: k + 1])) for k in range(len(now) - 1)}
    budget.spend(a, b)
    for mb in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        # Inside a run of words both sides hold in order, the breaks must sit alike.
        for k in range(1, mb.size):
            if (mb.a + k in breaks_a) != (mb.b + k in breaks_b):
                return True
    return False


@dataclass
class _Frame:
    """The last snapshot ``feed`` folded in: its time, each row's text and header speaker,
    and the known line each row ended on (None: a glitch)."""

    ts: int
    norms: list[str]
    speakers: list[str | None]
    lines: list[_Line | None]


@dataclass
class _Held:
    """A frame re-reading the last one's rows under other names, not merged yet."""

    blocks: list[CaptionBlock]
    ts: int
    speakers: tuple[str | None, ...]


def _shown(blocks: list[CaptionBlock]) -> list[_Shown]:
    return [
        _Shown(b.speaker if b.has_header else None, text[:_MAX_CHARS], _norm(text[:_MAX_CHARS]), bi)
        for bi, b in enumerate(blocks)
        for text in b.lines
        if _norm(text)
    ][-_MAX_SHOWN:]


class CaptionMerger:
    def __init__(
        self,
        *,
        start_ms: int,
        stable_ms: int,
        carried: list[_Line] | None = None,
        budget: MergeBudget | None = None,
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
        # The earliest first_ms a new line may take: one after the newest line's. Readers
        # order rows by start time, and a clamped clock (MeetingTracker.clock) can repeat a
        # ts, so starts stay strictly increasing across snapshots, not only within one
        # (XERK-2702). A tie would come back in random (segment id) order.
        self._next_first = 0
        # Lines this merger has seen for the first time (carried ones excluded).
        self.new_lines = 0
        # Each block's last re-flowed read, keyed by its lines (``_reflow``): a re-flow is
        # taken when two frames in a row read it alike.
        self._reflow_reads: dict[tuple[int, ...], tuple[str, ...]] = {}
        # The last ``_match``'s rows aligned as misreads: shown row -> the tail line it misreads.
        self._misreads: dict[int, int] = {}
        # The last ``_match``'s draft lines a rewrite folded into its rows (tail index, the
        # lines the rows claimed), and the last feed's folds (folded line, claiming lines),
        # taken on the next frame if it still doesn't show the folded line (``feed``).
        self._folded: list[tuple[int, tuple[int, ...]]] = []
        self._fold_held: list[tuple[_Line, list[_Line]]] = []
        # The last ``_match``'s rows aligned as new entries between known lines (``_align``).
        self._inserted: set[int] = set()
        # This and the last feed's grown-over-twin reads, keyed by line id (``feed``).
        self._grown_reads: dict[int, str] = {}
        # Each stored line's last read with words added (``_grow``), this frame's, and this
        # frame's candidates for ``_apply_grows``.
        self._grow_seen: dict[int, str] = {}
        self._grow_now: dict[int, str] = {}
        self._grows: list[tuple[_Line, _Shown, bool]] = []
        # The window's alignment-work bucket (XERK-2730): its tracker's, shared by every merger
        # it makes; a merger of its own only where none is given (tests, tools).
        self._budget = budget or MergeBudget()
        # The CPU the last ``feed`` or ``knows_any`` spent, refused or not (ms, for metrics).
        self.last_cpu_ms = 0.0
        # The last snapshot merged, and a frame re-reading its rows under other header names
        # held for the next frame to agree (``_reheaded``, XERK-2736).
        self._last: _Frame | None = None
        self._pending: _Held | None = None

    def rename(self, old: str, new: str) -> None:
        """Re-label the known lines of ``old`` as ``new``: the roster replaced a cut-off
        reading of a name with the full one (XERK-2695). Unrenamed, the box's lines under the
        new name aligned with none of them (a misread needs the same speaker) and were stored
        a second time. Lines already written keep the name they were stored under."""
        for ln in self._lines:
            if ln.speaker == old:
                ln.speaker = new
        if self.last_speaker == old:
            self.last_speaker = new

    def recent(self) -> list[_Line]:
        """The newest known lines, for the window's next merger (``carried``)."""
        return self._lines[-_TAIL:]

    def _rel(self, ms: int) -> int:
        return max(0, ms - self.start_ms)

    def _finalize(self, line: _Line) -> Segment:
        line.segment = Segment(
            segment_id=uuid.uuid4().hex,
            text=line.text,
            start_ms=self._rel(line.order_ms),
            end_ms=self._rel(max(line.changed_ms, line.first_ms, line.order_ms)),
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
    ) -> tuple[list[_Shown], list[_Line], list[int | None], dict[int, int], dict[int, int]]:
        self._budget.start(ts)
        shown = _shown(blocks)
        tail = self._tail(ts)
        self._misreads = {}
        self._inserted = set()
        aligned = _align(shown, tail, self._budget, self._misreads, self._inserted)
        speakers = self._speakers(blocks, shown, aligned, tail)
        self._folded = []
        revised = _revisions(shown, speakers, aligned, tail, ts, self._budget, self._folded)
        return shown, tail, aligned, revised, self._misreads

    def knows_any(self, blocks: list[CaptionBlock], ts: int) -> bool:
        """Whether a line of this snapshot re-reads a recent known line (it aligns to one).

        A header-less Teams crop mid-meeting is a long entry whose header scrolled off, and it
        still shows lines the last snapshot did; one sharing none is other text in the band
        (a slide, XERK-2614). Raises ``OverBudget`` like ``feed``."""
        started = time.thread_time()
        try:
            _, _, aligned, revised, _ = self._match(blocks, ts)
        finally:
            self.last_cpu_ms = 1000 * (time.thread_time() - started)
        return bool(revised) or any(a is not None for a in aligned)

    def feed(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        """Fold one caption snapshot in; returns segments to store (new or extended).

        Raises ``OverBudget``, having changed nothing, when aligning the snapshot would cost
        more work than the window's bucket holds (XERK-2730). Only ``_match`` charges it: the
        work after it mutates the merger, so it is never cut short."""
        started = time.thread_time()
        try:
            return self._feed(blocks, ts)
        finally:
            self.last_cpu_ms = 1000 * (time.thread_time() - started)

    def _feed(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        held, self._pending = self._pending, None
        reheaded = self._reheaded(blocks, ts, held)
        if reheaded is not None:
            return reheaded
        if held is None or self._rereads_last(blocks):
            # No frame held, or it misread names this one reads as before: drop it.
            return self._merge(blocks, ts)
        # The held frame was no re-attribution after all: merge it, then this one.
        out = {seg.segment_id: seg for seg in self._merge(held.blocks, held.ts)}
        try:
            out.update((seg.segment_id, seg) for seg in self._merge(blocks, ts))
        except OverBudget:
            # This frame is refused as any over-budget one is, but the held frame's lines
            # were merged: return them, or they would never be stored.
            pass
        return sorted(out.values(), key=lambda seg: seg.start_ms)

    def _rereads_last(self, blocks: list[CaptionBlock]) -> bool:
        """Whether ``blocks`` read the last merged frame's rows and names exactly."""
        shown = _shown(blocks)
        return self._last is not None and (
            [s.norm for s in shown] == self._last.norms
            and [s.speaker for s in shown] == self._last.speakers
        )

    def _merge(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        shown, tail, aligned, revised, misreads = self._match(blocks, ts)
        self._grow_now = {}
        self._grows = []
        # The known line each row re-reads, revises or misreads, for ``_apply_grows``.
        row_known = [
            tail[a] if a is not None else tail[revised[k]] if k in revised else
            tail[misreads[k]] if k in misreads else None
            for k, a in enumerate(aligned)
        ]
        # Whether shown[0] is the crop's top row: _shown keeps only the bottom _MAX_SHOWN.
        uncut = len(shown) == sum(1 for b in blocks for text in b.lines if _norm(text))
        last_aligned = max((i for i, a in enumerate(aligned) if a is not None), default=-1)
        out: dict[str, Segment] = {}
        to_finalize: list[_Line] = []
        speaker = self.last_speaker
        # The known line each row re-reads or revises. A new line goes just above the known
        # line shown below it, so known lines keep on-screen order: Teams adds lines to an
        # entry above another speaker's (that entry went on). Otherwise it goes at the end.
        known_at = [tail[a] if a is not None else None for a in aligned]
        revised_blocks = {shown[i].block for i in revised}
        for i, j in revised.items():
            known_at[i] = tail[j]
        # The line each shown row ends on (None: an OCR glitch, skipped), for ``_reflow``.
        row_line: list[_Line | None] = [None] * len(shown)
        grown_reads: dict[int, str] = {}
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
                if (
                    line_shown.block in revised_blocks
                    and line_shown.norm != line.norm
                    and not line.carried  # the ended meeting's segment (XERK-2615)
                ):
                    # A row of a rewritten entry the alignment still matched: the rewrite may
                    # have moved or dropped words on it ("...it, the how" over "the ...it the").
                    # One frame can be an OCR misread (XERK-2597), so new words wait for two
                    # reads in a row (``_update``); case or punctuation is taken at once.
                    if _words(line_shown.text) == _words(line.text):
                        self._revise(line, line_shown, ts, out)
                    else:
                        line.pending = line_shown.text
                        line.rewrite_until = ts + _REVISE_MS
                else:
                    self._update(
                        line,
                        line_shown,
                        ts,
                        out,
                        bottom=i == len(shown) - 1,
                        # Only the crop's top row, with no header above it, can be cut off.
                        clippable=i == 0 and uncut and not blocks[line_shown.block].has_header,
                        below=(shown[i + 1], known_at[i + 1]) if i + 1 < len(shown) else None,
                    )
                row_line[i] = line
            elif i in revised:
                line = tail[revised[i]]
                line.seen_ms = ts
                if line.speaker is None:
                    line.speaker = speaker
                speaker = line.speaker or speaker
                self._revise(line, line_shown, ts, out)
                row_line[i] = line
            elif i < last_aligned and i not in self._inserted:
                k = self._misreads.get(i)
                if (
                    k is not None
                    and ts - tail[k].first_ms <= _REVISE_MS
                    and _grew_over_twin(shown, i + 1, tail, k)
                ):
                    # Not a glitch: its line grew above a same-text entry (XERK-2719). Taken
                    # once two frames in a row read it alike, and only for a recent line (as
                    # ``_reflow``): one frame's stray OCR token ("like l o") was stored (QA).
                    line = tail[k]
                    grown_reads[id(line)] = line_shown.norm
                    if self._grown_reads.get(id(line)) == line_shown.norm:
                        line.seen_ms = ts
                        self._revise(line, line_shown, ts, out)
                        row_line[i] = line
                elif i in misreads:
                    # Or Teams added words to the entry it misreads (``_grow``, XERK-2721).
                    self._grow(tail[misreads[i]], line_shown, ts)
                continue  # an OCR glitch above known lines: never a new line
            else:
                # New lines keep the order they appeared in: each at least a millisecond
                # after the line before it, so stores ordering by start time agree.
                first = max(ts, self._next_first)
                self._next_first = first + 1
                line = _Line(speaker, line_shown.text, line_shown.norm, first, first, ts)
                below = next((ln for ln in known_at[i + 1 :] if ln is not None), None)
                at_ = len(self._lines)
                if below is not None:
                    at_ = next(k for k in range(len(self._lines)) if self._lines[k] is below)
                    if not below.carried:  # the ended meeting's segment keeps its start
                        self._place_above(line, at_, out)
                self._lines.insert(at_, line)
                self.new_lines += 1
            row_line[i] = line
            if block.has_header and (i == 0 or shown[i - 1].block != line_shown.block):
                line.head = True
            if speaker is not None:
                self.last_speaker = speaker
            nxt = line_shown.block + 1
            if nxt < len(blocks) and blocks[nxt].has_header and not line.final:
                to_finalize.append(line)
        self._grown_reads = grown_reads
        # A fold is taken a frame late, once that frame doesn't show the draft line either: a
        # draft row shown again is real. On the drop the claiming lines open the rewritten-row
        # window: with the draft row gone, later reads align onto them and keep their text,
        # so a misread rewrite frame was stored for good, where main went on revising (QA).
        for line, claimers in self._fold_held:
            if line.seen_ms != ts and any(ln is line for ln in self._lines):
                self._drop(line, out)
                for c in claimers:
                    c.rewrite_until = ts + _REVISE_MS
        self._fold_held = [(tail[j], [tail[k] for k in claimed]) for j, claimed in self._folded]
        self._reflow(
            shown,
            row_line,
            ts,
            out,
            clippable_top=bool(shown) and uncut and not blocks[shown[0].block].has_header,
        )
        top_open = bool(shown) and not blocks[shown[0].block].has_header
        self._apply_grows(shown, row_known, ts, out, top_open=top_open)
        for line in to_finalize:
            if not line.final and any(ln is line for ln in self._lines):
                seg = self._finalize(line)
                out[seg.segment_id] = seg
        self._grow_seen = self._grow_now
        self._last = _Frame(ts, [s.norm for s in shown], [s.speaker for s in shown], row_line)
        return self._settle(ts, out)

    def _settle(self, ts: int, out: dict[str, Segment]) -> list[Segment]:
        """Finalize lines unchanged for ``stable_ms``; ``out``, in the order said."""
        for line in self._lines:
            if not line.final and ts - line.changed_ms >= self.stable_ms:
                seg = self._finalize(line)
                out[seg.segment_id] = seg
        del self._lines[:-_KEEP]
        # Stored in the order they were said, whichever rule finalized them.
        return sorted(out.values(), key=lambda seg: seg.start_ms)

    def _reheaded(
        self, blocks: list[CaptionBlock], ts: int, held: _Held | None
    ) -> list[Segment] | None:
        """Re-attribute the last snapshot's lines when this one is the same rows under other
        header names (XERK-2736): Teams re-attributed an entry, or OCR read another name. A
        misread needs the same speaker, so the rows aligned as new and the block was stored
        again. None when this is not such a frame (``feed`` merges it as usual).

        Nothing new can have been said only with no time for it: within ``_REHEAD_MS`` of the
        last snapshot. [Ana X][Bo ok] -> [Cy X][Bo ok] after a dropped frame is Cy repeating X
        and Bo his reply; matching X across speakers there lost both lines (XERK-2727 QA).

        The rows must also show the panel didn't turn over or scroll: a row keeps its name (an
        anchor: a one-row panel or a goodbye round re-said in full lost lines without it, QA),
        the names don't read as the last ones scrolled up ([Bo thanks][Ana thanks] -> [Ana
        thanks][Ana thanks] is Ana thanking again), and each re-named entry holds
        ``_REHEAD_WORDS`` words (a short reply is said whole between frames).

        The new names are taken once the next frame agrees (two frames in a row read them), so
        a one-frame misread name changes nothing. The frame between merges nothing (``held``).
        The next frame reading the same rows under yet other names replaces it (its rows are
        the last frame's, so nothing is lost); a frame showing other rows makes ``feed`` merge
        it after all, then that frame, so no line it showed is lost."""
        last, shown = self._last, _shown(blocks)
        speakers = tuple(s.speaker for s in shown)
        if (
            last is None
            # Each frame within _REHEAD_MS of the one before: the held one, else the last.
            or ts - (last.ts if held is None else held.ts) > _REHEAD_MS
            or [s.norm for s in shown] != last.norms
            or any(
                list(speakers[: len(speakers) - k]) == last.speakers[k:]
                for k in range(1, len(speakers))
            )
        ):
            return None
        moved = [i for i, sp in enumerate(speakers) if sp != last.speakers[i]]
        block_words: dict[int, int] = {}
        for row in shown:
            block_words[row.block] = block_words.get(row.block, 0) + len(_words(row.text))
        lines = [last.lines[i] for i in moved]
        if (
            not moved
            or not any(sp is not None and i not in moved for i, sp in enumerate(speakers))
            or any(
                speakers[i] is None
                or block_words[shown[i].block] < _REHEAD_WORDS
                or line is None
                or line.carried  # the ended meeting's segment keeps its speaker (XERK-2615)
                or line.speaker is None
                or line.speaker != last.speakers[i]
                # A twin (the speaker's other recent line of that text): which entry the row
                # is was a guess, and re-labelling the wrong one lost a line (QA fuzz 936).
                or any(
                    ln is not line and ln.norm == line.norm and ln.speaker == line.speaker
                    for ln in self._lines[-_TAIL:]
                )
                for i, line in zip(moved, lines, strict=True)
            )
        ):
            return None
        out: dict[str, Segment] = {}
        if held is None or held.speakers != speakers:
            # Wait for the next frame to agree. A held frame read under other names again is
            # replaced: its rows are the last frame's, so dropping it loses nothing, and Bob ->
            # Bib (a misread) -> Ana settles on Ana instead of storing all three.
            self._pending = _Held(blocks, ts, speakers)
            return self._settle(ts, out)
        for i, line in zip(moved, lines, strict=True):
            assert line is not None
            if self._lines and line is self._lines[-1] and self.last_speaker == line.speaker:
                self.last_speaker = speakers[i]
            line.speaker = speakers[i]
            line.seen_ms = ts
            if line.segment is not None:
                line.segment.speaker = speakers[i]
                out[line.segment.segment_id] = line.segment
        last.ts = ts
        last.speakers = list(speakers)
        return self._settle(ts, out)

    def _place_above(self, line: _Line, at: int, out: dict[str, Segment]) -> None:
        """Order ``line`` in the store just above ``self._lines[at]`` (XERK-2729): a row Teams
        wraps onto an entry above a later one was first seen after it, so ordering by first-seen
        time stored it below. It takes that line's start; it and the lines after it move down
        one ms each until there is room, and stored ones are re-emitted (the upsert moves
        them). Starts stay unique: Postgres orders ties randomly."""
        prev = line.store_ms = self._lines[at].order_ms
        for ln in self._lines[at:]:
            if ln.carried:
                continue  # the ended meeting's segment is never re-emitted (XERK-2615)
            if ln.order_ms > prev:
                break
            prev = ln.store_ms = prev + 1
            if ln.segment is not None:
                ln.segment.start_ms = self._rel(prev)
                ln.segment.end_ms = max(ln.segment.end_ms, ln.segment.start_ms)
                out[ln.segment.segment_id] = ln.segment
        self._next_first = max(self._next_first, prev + 1)

    def _update(
        self,
        line: _Line,
        shown: _Shown,
        ts: int,
        out: dict[str, Segment],
        *,
        bottom: bool,
        clippable: bool = False,
        below: tuple[_Shown, _Line | None] | None = None,
    ) -> None:
        """``below``: the next shown row and the known line it re-reads (if any)."""
        if ts <= line.rewrite_until:
            # A rewritten row: a read equal to the stored text drops what was held (jitter
            # flipping back later must not confirm it); a differing read is taken when the
            # read before it agrees, else held. One noisy frame then only delays the rewrite.
            if shown.norm == line.norm:
                line.pending = None
                return
            mine = _words(shown.text)
            if clippable and any(
                len(mine) < len(full) and full[len(full) - len(mine) :] == mine
                for full in (_words(line.text), _words(line.pending or ""))
            ):
                # The crop cut the row's start off: alike on every frame it lasts. Elsewhere
                # a row that lost its leading words is a rewrite (Teams drops fillers).
                return
            if line.pending is not None and _norm(line.pending) == shown.norm:
                self._revise(line, shown, ts, out)
                return
            line.pending = shown.text
        else:
            line.pending = None
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
        # Growth that is only stray glyphs ("notes" -> "notes l"), or the words the row below
        # just lost (a re-flow moving "today" up from "today on friday"), is not taken on one
        # frame: both were stored for good (XERK-2701). ``_grow`` and ``_reflow`` judge them.
        # Any other growth extends at once: with dropped frames, Zoom completes a line on the
        # one frame it shows it, and waiting lost those last words (``_simulate`` noise+drops).
        seg = line.segment
        assert seg is not None
        said_on = any(
            _same_speaker(ln.speaker, line.speaker) for ln in self._lines[self._index(line) + 1 :]
        )
        if (
            grew
            and ts - line.changed_ms <= _EXTEND_MS
            and not _moved_up(line, shown, below, said_on=said_on)
        ):
            line.text, line.norm, line.changed_ms = shown.text, shown.norm, ts
            seg.text = shown.text
            seg.end_ms = self._rel(max(ts, line.first_ms))
            out[seg.segment_id] = seg
        else:
            self._grow(line, shown, ts)

    def _grow(self, line: _Line, shown: _Shown, ts: int) -> None:
        """A recent stored line re-read with whole words added (XERK-2721): Teams finished a
        middle entry ("and the notes" -> "...are done") or reworded one ("the build is
        green." -> "...red now not green"). Rows above the bottom otherwise keep their text
        (OCR jitter), so these were lost. Taken once two frames in a row read it alike: one
        misread frame is never stored. A read that only drops words keeps the draft (``_edited``).
        Confirmed reads are applied after ``_reflow`` (``_apply_grows``)."""
        # The speaker matches (a misread pairing and a match both require it), and
        # ``_revise`` refuses a carried line (the ended meeting's segment, XERK-2615).
        if not line.final or ts - line.first_ms > _REVISE_MS:
            return
        if shown.norm == line.grow_refused:
            return
        if not _edited(_words(line.text), _words(shown.text), self._budget):
            return
        if any(ln is not line and _exact(shown, ln) for ln in self._lines[-_TAIL:]):
            return  # it re-reads another known line: growing this one copied that (XERK-2719)
        self._grow_now[id(line)] = shown.norm
        self._grows.append((line, shown, self._grow_seen.get(id(line)) == shown.norm))

    def _apply_grows(
        self,
        shown: list[_Shown],
        row_known: list[_Line | None],
        ts: int,
        out: dict[str, Segment],
        *,
        top_open: bool,
    ) -> None:
        """Take the confirmed ``_grow`` reads ``_reflow`` left alone, of a row whose block's
        other rows, and the same speaker's rows right above and below it, still read as their
        lines. When they changed too the entry re-flowed or was rewritten (``_reflow``,
        ``_revisions``): taking the row alone stored words that moved onto it twice and lost
        the rest of the rewrite (QA: within a block, with a word inserted beside the moved
        ones, and across two of the speaker's entries). Such a read is refused for good, on
        its first frame: applied once the changed row scrolled off, it stored the word twice
        again (QA). Live lines read as their lines here: the row rules took this frame's
        text already."""

        def steady(k: int, i: int, was: list[str], now: list[str]) -> bool:
            # A row with no known line is new this frame (the live entry gained a row as the
            # speaker went on) or a glitch: neither moved words off a line. Counting it as a
            # change refused real insertions for good (QA, ``grow_refused``).
            known = row_known[k]
            if known is None:
                return True
            # Words re-flow onto the growing row only off a row that lost them, so only a
            # neighbour missing a word this row adds is a change (XERK-2728). Any change used
            # to count: a neighbour growing too, re-punctuated, or one OCR-jittered frame
            # ("on fridav") refused a real growth for good.
            kw, sw = _words(known.text), _words(shown[k].text)
            if (Counter(kw) - Counter(sw)) & (Counter(now) - Counter(was)):
                return False
            # Or a word crossed the break between them: the neighbour's edge word facing this
            # row changed while this row gained a word on that side. A row misaligned onto
            # the wrong line can't show the moved word itself (``_simulate_teams`` seed 61).
            # Not on a facing edge change alone: one jittered frame of it ("tben") then
            # refused a real insertion for good, losing words in every fuzz (QA).
            if abs(k - i) != 1:
                return True
            edge = -1 if k < i else 0  # the neighbour's side facing this row
            return kw[edge:][:1] == sw[edge:][:1] or now[-1 - edge :][:1] == was[-1 - edge :][:1]

        for line, row, confirmed in self._grows:
            if not any(ln is line for ln in self._lines):
                continue  # ``_reflow`` dropped the line
            i = next(k for k, r in enumerate(shown) if r is row)
            was, now = _words(line.text), _words(row.text)
            near = [
                k
                for k in range(len(shown))
                if k != i
                and (
                    shown[k].block == row.block
                    or (
                        abs(k - i) == 1
                        and _same_speaker(
                            (row_known[k].speaker if row_known[k] else None) or shown[k].speaker,
                            line.speaker,
                        )
                    )
                )
            ]
            # The entry's row above is off screen (the crop's header-less top row) or has no
            # known line: words that re-flowed down onto this row's start came off a row no
            # neighbour check can see (QA: stored twice). Accepted: a real word Teams adds at
            # the crop top row's start is lost. Not a block's top row whose line wasn't its
            # entry's first: refusing that left a merged two-row entry unpaired and the box
            # was stored again (fz seed 176, XERK-2734).
            if i == 0:
                hidden_above = top_open
            else:
                hidden_above = shown[i - 1].block == row.block and row_known[i - 1] is None
            if (hidden_above and now[:1] != was[:1]) or not all(
                steady(k, i, was, now) for k in near
            ):
                line.grow_refused = row.norm
            elif confirmed:
                self._revise(line, row, ts, out)

    def _reflow(
        self,
        shown: list[_Shown],
        row_line: list[_Line | None],
        ts: int,
        out: dict[str, Segment],
        *,
        clippable_top: bool,
    ) -> None:
        """Upsert each block as one entry (XERK-2715): Teams re-flows a growing entry, so words
        move between its rows ("then I will" joins the next row's start, "the." becomes "the
        APl contract" and pushes words down). Row by row, a row that gained leading words
        isn't a growth and lands as a new line beside its stale draft. When a block's rows
        moved words across their row breaks, its rows replace the texts of the lines they
        landed on, in order, and the speaker's recent lines left between them whose words the
        block now holds are dropped (a stored one is blanked: readers skip empty segments).

        Only a re-flow is taken: a word changed within a row with no word crossing a break is
        OCR jitter, left to the row rules. Two frames in a row must read the block's rows
        above its bottom alike (the bottom one is still growing), so one misread frame is
        never stored."""
        reads: dict[tuple[int, ...], tuple[str, ...]] = {}
        groups: list[list[int]] = []
        for i, line_shown in enumerate(shown):
            if groups and shown[groups[-1][0]].block == line_shown.block:
                groups[-1].append(i)
            else:
                groups.append([i])
        for rows in groups:
            found = self._fill_glitches([row_line[i] for i in rows], ts)
            if found is None:
                continue  # a glitch row the lines can't place: the rows can't be mapped
            lines = found
            speaker = lines[0].speaker
            if any(ln.carried or ln.speaker != speaker for ln in lines):
                continue
            pos = [self._index(ln) for ln in lines]
            if pos != sorted(set(pos)):
                continue
            between = [
                ln
                for k, ln in enumerate(self._lines[pos[0] : pos[-1] + 1])
                if k + pos[0] not in pos
            ]
            if any(
                ln.carried or ln.speaker != speaker or ts - ln.first_ms > _REVISE_MS
                for ln in between
            ):
                continue  # another speaker's line (or an old one) between: not one entry
            texts = [shown[i].text for i in rows]
            row_words = [_words(t) for t in texts]
            line_words = [_words(ln.text) for ln in lines]
            if (
                clippable_top
                and rows[0] == 0
                and len(shown[0].norm) < len(lines[0].norm)
                and (
                    lines[0].norm.endswith(shown[0].norm)
                    or line_words[0][-len(row_words[0]) :] == row_words[0]
                )
            ):
                # The crop cut the top row's start off (mid-word too): keep the line's text.
                texts[0], row_words[0] = lines[0].text, line_words[0]
            # A line the block skips is an OCR miss unless the rows around where it sat hold
            # its words: it is a stale copy of what re-flowed onto them.
            if any(not self._held(ln, lines, row_words) for ln in between):
                continue
            # A glitch row that grew its line by whole words is taken too: ``_misread`` paired
            # it ("like" -> "like with the keto."), so no other rule stores its new words. Not
            # an aligned row: the live bottom row grows every frame, and taking it rewrote the
            # rows above onto lines they were misaligned with (`_simulate_teams` seed 125).
            # Only a recent line: Teams re-flows an entry seconds after it ends, and an old
            # row re-read longer is OCR jitter ("like" -> "like l o" 70 s on, QA).
            grew = any(
                row_line[i] is None
                and ts - ln.first_ms <= _REVISE_MS
                and len(rw) > len(lw)
                and rw[: len(lw)] == lw
                for i, ln, rw, lw in zip(rows, lines, row_words, line_words, strict=True)
            )
            if not between and not grew and not _moved(line_words, row_words, self._budget):
                continue
            # The bottom row of a live entry still grows (the row rules take it); a stored one
            # is confirmed like the others, or a misread on the confirming frame would stick.
            confirm = len(texts) if lines[-1].final else len(texts) - 1
            key = tuple(id(ln) for ln in lines[:confirm])
            read = tuple(_norm(t) for t in texts[:confirm])
            reads[key] = read
            if self._reflow_reads.get(key) != read:
                continue
            for ln, text in zip(lines, texts, strict=True):
                ln.pending, ln.rewrite_until = None, 0
                if _norm(text) == ln.norm:
                    continue
                ln.text, ln.norm = text, _norm(text)
                if ln.segment is None:
                    ln.changed_ms = ts
                else:
                    # end_ms stays when it was said, as for a rewrite (_revise).
                    ln.segment.text = text
                    out[ln.segment.segment_id] = ln.segment
            for ln in between:
                self._drop(ln, out)
        self._reflow_reads = reads

    def _drop(self, line: _Line, out: dict[str, Segment]) -> None:
        """Forget a stale known line; a stored one is re-emitted blank (the store has no
        delete, and readers skip empty segments)."""
        self._lines.remove(line)
        if line.segment is not None:
            line.segment.text = ""
            out[line.segment.segment_id] = line.segment

    def _fill_glitches(self, found: list[_Line | None], ts: int) -> list[_Line] | None:
        """``found`` with each run of glitch rows (None) between two placed rows paired, in
        order, with the known lines between those two: the alignment took a re-flowed row
        that still resembles its draft for a misread of it. A run at the block's bottom pairs
        with the lines right after the last placed one, when they are its speaker's and no row
        re-read them (the entry ended; another speaker's block below it aligned). None when a
        run has no such lines to pair with, one for one."""
        out: list[_Line] = []
        k = 0
        while k < len(found):
            ln = found[k]
            if ln is not None:
                out.append(ln)
                k += 1
                continue
            end = k
            while end < len(found) and found[end] is None:
                end += 1
            above, below = (found[k - 1] if k else None), (found[end] if end < len(found) else None)
            if above is None:
                return None
            at = self._index(above) + 1
            if below is None:
                gap = self._lines[at : at + end - k]
                if any(ln.speaker != above.speaker or ln.seen_ms == ts for ln in gap):
                    return None
            else:
                gap = self._lines[at : self._index(below)]
            if len(gap) != end - k:
                return None
            out += gap
            k = end
        return out

    def _held(self, line: _Line, lines: list[_Line], row_words: list[list[str]]) -> bool:
        """Whether ``line`` is a stale copy of words that re-flowed onto the rows next to it
        (those of the lines just above and below it): they hold ``_HELD`` of its words, in
        order, and the words moved there: those rows gained them, or one of them no longer
        reads as its line. Rows reading exactly as their lines did mean OCR missed the row
        between, however alike the lines are ("i think that" / "i think that"), unless its
        words sit right at their break (``_junction``) and one of those lines changed since
        this one was last shown: the row rules may already have grown the line above with
        them ("...to" + "staging")."""
        at = self._index(line)
        k = max(n for n, ln in enumerate(lines) if self._index(ln) < at)
        near_rows = row_words[k : k + 2]
        near_lines = [_words(ln.text) for ln in lines[k : k + 2]]
        words = _words(line.text)
        if (
            len(near_rows) == 2
            # Not the block's bottom line: a live one changes every frame as it grows (QA).
            and any(
                ln.changed_ms > line.seen_ms and ln is not lines[-1] for ln in lines[k : k + 2]
            )
            and _junction(words, near_rows[0], near_rows[1])
        ):
            return True
        near = [w for ws in near_rows for w in ws]
        if not _share(words, near, self._budget):
            return False
        if near_rows != near_lines:
            return True
        had = [w for ws in near_lines for w in ws]
        self._budget.spend(had, near)
        sm = SequenceMatcher(None, had, near, autojunk=False)
        kept = {mb.b + n for mb in sm.get_matching_blocks() for n in range(mb.size)}
        return _share(words, [w for n, w in enumerate(near) if n not in kept], self._budget)

    def _index(self, line: _Line) -> int:
        return next(k for k in range(len(self._lines) - 1, -1, -1) if self._lines[k] is line)

    def _revise(self, line: _Line, shown: _Shown, ts: int, out: dict[str, Segment]) -> None:
        """A rewritten entry's row replaces the known line it revises, stored or not."""
        if shown.norm == line.norm or line.carried:
            return
        line.text, line.norm, line.changed_ms = shown.text, shown.norm, ts
        line.pending, line.rewrite_until = None, 0
        if line.segment is not None:
            # end_ms stays when it was said: the rewrite lands after others spoke.
            line.segment.text = shown.text
            out[line.segment.segment_id] = line.segment

    def flush(self, ts: int) -> list[Segment]:
        """Finalize every line still on screen (the meeting ended)."""
        out = [self._finalize(line) for line in self._lines if not line.final]
        return sorted(out, key=lambda seg: seg.start_ms)
