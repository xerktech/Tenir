"""Rolling-caption merge: Zoom's re-drawn caption box -> final, de-duplicated lines (XERK-2505).

Zoom redraws its caption box many times a second: the bottom line grows word by word (and
the ASR may rewrite its last words), full lines scroll up and off, and a new speaker's block
pushes older ones out. Each OCR'd crop is a snapshot of that box. A line is **final** when

- another speaker's block follows it in a snapshot, or
- it has not changed for ``stable_ms``, or
- the meeting ends (``flush``).

A final line is never stored twice: while it stays on screen it keeps matching its final and
is skipped; if it grows after it was finalized (the speaker paused mid-line, then went on)
the stored segment is extended in place (same segment id, so the store upserts it).

Saying the same thing again is not a duplicate. The box only ever adds lines at the bottom,
so every line carries a creation order, and a snapshot read top to bottom is in that order:

- a line can only be a re-read of a stored final if no newer *known* line (one an earlier
  snapshot already showed) sits above it — Ana's second "okay" under Bo's reply is new,
  however soon it comes, while a garbled one-frame top line changes nothing below it;
- a final absorbs at most one line per snapshot (two "okay"s on screen are two lines);
- only the newest line can still grow, so a final is extended only while nothing has been
  created after it;
- a final that no snapshot has shown for ``_GONE_MS`` has left the screen; the same words
  after that are new, which covers a repeat after a silence that cleared the box.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from api.persistence.models import Segment
from api.screen.zoom import CaptionBlock

# A re-read of the same line (OCR jitter, an ASR rewrite of a word or two).
_SAME_LINE = 0.75
# Close enough to a stored final that storing it again would be a duplicate.
_DUPLICATE = 0.88
# How long a final can still be extended by its line growing.
_EXTEND_MS = 30_000
_RECENT_FINALS = 300
# A line one snapshot showed that is this close to (or inside) a known line is an OCR
# fragment of it (a half-scrolled or garbled top line), not something said.
_FRAGMENT = 0.6
# A final no snapshot has shown for this long has left the screen. Long, because the order
# rule does the real work: this only separates a repeat after silence from an OCR miss.
_GONE_MS = 20_000


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _alnum(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _same_speaker(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


@dataclass
class _Live:
    speaker: str | None
    text: str
    first_ms: int
    changed_ms: int
    seen_ms: int
    order: int  # creation order: lower is older, i.e. higher up the caption box
    snapshots: int = 1  # how many snapshots showed it
    top: bool = False  # it was the top line of the snapshot that first showed it

    @property
    def norm(self) -> str:
        return _norm(self.text)


@dataclass
class _Final:
    segment: Segment
    norm: str
    at_ms: int  # when it was finalized or last extended
    seen_ms: int  # when it was last on screen
    order: int  # its line's creation order


class CaptionMerger:
    def __init__(self, *, start_ms: int, stable_ms: int) -> None:
        self.start_ms = start_ms
        self.stable_ms = stable_ms
        self._live: list[_Live] = []
        self._finals: deque[_Final] = deque(maxlen=_RECENT_FINALS)
        self.last_speaker: str | None = None
        self._order = 0  # last creation order handed out

    def _rel(self, ms: int) -> int:
        return max(0, ms - self.start_ms)

    def _on_screen_final(
        self, speaker: str | None, norm: str, ts: int, absorbed: set[int], floor: int
    ) -> _Final | None:
        """The stored final this re-read of an on-screen line belongs to, if any.
        ``floor`` is the newest order matched above this line in the snapshot."""
        for f in reversed(self._finals):
            if f.order < floor or ts - f.seen_ms > _GONE_MS:
                continue
            if id(f) in absorbed or not _same_speaker(f.segment.speaker, speaker):
                continue
            # A long line inside a stored one is that line partly scrolled or re-wrapped;
            # a short one ("ok") may well be said again, so it must match whole.
            contained = len(norm) >= 12 and norm in f.norm
            if norm == f.norm or contained or _ratio(norm, f.norm) >= _DUPLICATE:
                f.seen_ms = ts
                absorbed.add(id(f))
                return f
        return None

    def _extendable(self, speaker: str | None, norm: str, ts: int) -> _Final | None:
        """The final this line is a continuation of: only the newest line of all can grow."""
        if not self._finals:
            return None
        f = self._finals[-1]
        if f.order != self._order or ts - f.at_ms > _EXTEND_MS:
            return None
        if _same_speaker(f.segment.speaker, speaker) and norm.startswith(f.norm + " "):
            return f
        return None

    def _is_fragment(self, live: _Live, ts: int) -> bool:
        # Fragments come from the half-scrolled top of the box; a short reply further down
        # ("yes" under "yesterday's build") is real even if only one snapshot showed it.
        if live.snapshots > 1 or not live.top:
            return False
        # Letters and digits only: OCR noise is mostly stray punctuation and tildes.
        norm = _alnum(live.norm)
        known = [f.norm for f in self._finals if ts - f.seen_ms <= _GONE_MS]
        known += [lv.norm for lv in self._live if lv is not live and lv.snapshots > 1]
        return not norm or any(
            norm != (k := _alnum(k_norm)) and (norm in k or _ratio(norm, k) >= _FRAGMENT)
            for k_norm in known
        )

    def _finalize(self, live: _Live, ts: int) -> Segment | None:
        self._live.remove(live)
        if self._is_fragment(live, ts):
            return None
        seg = Segment(
            segment_id=uuid.uuid4().hex,
            text=live.text,
            start_ms=self._rel(live.first_ms),
            end_ms=self._rel(max(live.changed_ms, live.first_ms)),
            lang="en",
            speaker=live.speaker,
        )
        self._finals.append(_Final(seg, live.norm, ts, live.seen_ms, live.order))
        return seg

    def feed(self, blocks: list[CaptionBlock], ts: int) -> list[Segment]:
        """Fold one caption snapshot in; returns segments to store (new or extended)."""
        out: list[Segment] = []
        seen: set[int] = set()
        absorbed: set[int] = set()
        floor = 0  # newest creation order matched so far, top to bottom
        first_line = True
        to_finalize: list[_Live] = []
        last_block = len(blocks) - 1
        for bi, block in enumerate(blocks):
            for text in block.lines:
                norm = _norm(text)
                if not norm:
                    continue
                first_line, was_first = False, first_line
                # A header-less block continues one whose header scrolled off: match it
                # against any speaker's lines first, then attribute it.
                speaker = block.speaker if block.has_header else None
                live = self._match_live(speaker, norm, seen)
                if live is None:
                    ext = self._extendable(speaker, norm, ts)
                    if ext is not None:
                        ext.segment.text = text
                        ext.segment.end_ms = self._rel(ts)
                        ext.norm = norm
                        ext.at_ms = ext.seen_ms = ts
                        absorbed.add(id(ext))
                        floor = max(floor, ext.order)
                        out.append(ext.segment)
                        continue
                    final = self._on_screen_final(speaker, norm, ts, absorbed, floor)
                    if final is not None:
                        floor = max(floor, final.order)
                        continue  # still on screen after it was stored
                    if not block.has_header:
                        speaker = self.last_speaker
                    self._order += 1
                    live = _Live(speaker, text, ts, ts, ts, self._order, top=was_first)
                    self._live.append(live)
                else:
                    if not block.has_header:
                        speaker = live.speaker or self.last_speaker
                    live.snapshots += 1
                    if live.text != text:
                        live.text = text
                        live.changed_ms = ts
                    if live.speaker is None:
                        live.speaker = speaker
                    # Only a line already known raises the floor. A line first read in this
                    # snapshot may be an OCR fragment of the half-scrolled top line, and
                    # letting it raise the floor re-stored every final below it.
                    floor = max(floor, live.order)
                live.seen_ms = ts
                seen.add(id(live))
                if speaker is not None:
                    self.last_speaker = speaker
                if bi < last_block and blocks[bi + 1].has_header:
                    to_finalize.append(live)
        for live in to_finalize:
            if live in self._live and (seg := self._finalize(live, ts)) is not None:
                out.append(seg)
        for live in [lv for lv in self._live if ts - lv.changed_ms >= self.stable_ms]:
            if (seg := self._finalize(live, ts)) is not None:
                out.append(seg)
        # Stored in the order they were said, whichever rule finalized them.
        return sorted(out, key=lambda seg: seg.start_ms)

    def _match_live(self, speaker: str | None, norm: str, seen: set[int]) -> _Live | None:
        best: tuple[float, _Live] | None = None
        for live in self._live:
            if id(live) in seen or not _same_speaker(live.speaker, speaker):
                continue
            ln = live.norm
            if norm == ln or norm.startswith(ln) or ln.startswith(norm):
                score = 1.0
            else:
                score = _ratio(norm, ln)
            if score >= _SAME_LINE and (best is None or score > best[0]):
                best = (score, live)
        return best[1] if best else None

    def flush(self, ts: int) -> list[Segment]:
        """Finalize every line still on screen (the meeting ended)."""
        out = []
        for live in list(self._live):
            if (seg := self._finalize(live, ts)) is not None:
                out.append(seg)
        return sorted(out, key=lambda seg: seg.start_ms)
