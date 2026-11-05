"""Caption rows -> sentences, for reading a screen meeting back (XERK-2696).

The merger stores one segment per *visual* caption row, so a sentence is cut wherever the
caption pane wrapped ("Are we missing the cedar in test" / "environment?"). Storage stays
row-based on purpose: the merge logic aligns rows, and rewriting it is far riskier than
re-joining at read time. Everything that reads a screen meeting back (history, delivery to
Turma, the topic titler) goes through ``sentences`` instead.

Rules:
- Consecutive rows of one *known* speaker join (with a space) until the text ends a sentence.
  A row with no speaker never joins: Zoom stores every off-roster speaker as None, so two
  None rows may be two people.
- A pause of more than ``MAX_GAP_MS`` between rows also ends the utterance, or words said
  minutes later would be shown at the earlier time.
- A sentence ends at . ? ! … (plus closing quotes) followed by a piece that doesn't start
  lower-case, and not after a title ("Dr."). The same test decides a cut mid-row and a break
  between rows, so a wrap right after "Dr." or "e.g." joins like the unwrapped text would.
- A row is cut at most ``MAX_CUTS`` times (the rest stays in its last piece): a crafted
  "A. A. A. ..." row otherwise fanned out into ~50 segments per stored row.
- Each piece keeps its row's start/end ms (row timing is all the capture knows); pieces after
  the first get ``<row id>.<n>`` ids so ids stay unique and stable across reads.
- An utterance never grows past ``max_chars``: unpunctuated speech still comes out in pieces.
- A row carrying a translation is left alone (its translation can't be split or joined).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from api.persistence.models import Segment

MAX_CHARS = 2000  # the Turma hub's per-segment cap (turma.SEGMENT_TEXT_MAX)
# Wrapped rows of one entry appear within a second or two of each other; a longer silence is
# a new utterance even without punctuation.
MAX_GAP_MS = 8000
MAX_CUTS = 6

_END = re.compile(r"[.?!…。！？][\"'”’)\]]*$")
# A candidate cut: a sentence end, closing quotes, whitespace. `_ends_at` decides it.
_CUT = re.compile(r"(?<=[.?!…])[\"'”’)\]]*\s+(?=\S)")
# Searched only over the few chars before a period (pos/endpos), so it stays O(1) per cut.
_TITLES = re.compile(r"(?<!\S)(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Prof|vs)\.$")
_TITLE_SPAN = 6


def _ends_at(text: str, end: int, following: str) -> bool:
    """Whether ``text[:end]`` (ending in a sentence mark) ends a sentence before ``following``."""
    if following[:1].islower():
        return False
    return not _TITLES.search(text, max(0, end - _TITLE_SPAN), end)


def _pieces(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in _CUT.finditer(text):
        if len(out) >= MAX_CUTS:
            break
        if not _ends_at(text, m.start(), text[m.end() : m.end() + 1]):
            continue
        out.append(text[start : m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [p for p in out if p]


def _ends_sentence(text: str, following: str) -> bool:
    m = _END.search(text)
    if m is None:
        return False
    # The mark itself, before any closing quotes, is where a title would end.
    mark = m.start() + 1
    return _ends_at(text, mark, following)


def sentences(segments: Iterable[Segment], max_chars: int = MAX_CHARS) -> list[Segment]:
    """The rows of one meeting as utterances, in time order (see the module docstring)."""
    out: list[Segment] = []
    cur: Segment | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            out.append(cur)
            cur = None

    for seg in sorted(segments, key=lambda s: (s.start_ms, s.segment_id)):
        text = seg.text.strip()
        if not text:
            continue
        if seg.translation:
            flush()
            out.append(seg)
            continue
        for n, piece in enumerate(_pieces(text)):
            if (
                cur is not None
                and seg.speaker
                and cur.speaker == seg.speaker
                and (n > 0 or seg.start_ms - cur.end_ms <= MAX_GAP_MS)
                and not _ends_sentence(cur.text, piece)
                and len(cur.text) + 1 + len(piece) <= max_chars
            ):
                cur.text = f"{cur.text} {piece}"
                cur.end_ms = max(cur.end_ms, seg.end_ms)
            else:
                flush()
                cur = Segment(
                    segment_id=seg.segment_id if n == 0 else f"{seg.segment_id}.{n}",
                    text=piece,
                    start_ms=seg.start_ms,
                    end_ms=max(seg.start_ms, seg.end_ms),
                    lang=seg.lang,
                    speaker=seg.speaker,
                )
    flush()
    return out
