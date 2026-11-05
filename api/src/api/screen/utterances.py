"""Caption rows -> sentences, for reading a screen meeting back (XERK-2696).

The merger stores one segment per *visual* caption row, so a sentence is cut wherever the
caption pane wrapped ("Are we missing the cedar in test" / "environment?"). Storage stays
row-based on purpose: the merge logic aligns rows, and rewriting it is far riskier than
re-joining at read time. Everything that reads a screen meeting back (history, delivery to
Turma, the topic titler) goes through ``sentences`` instead.

Rules:
- Consecutive rows of one speaker join (with a space) until the text ends a sentence.
- A row holding a sentence end mid-row is cut there; each piece keeps its row's start/end ms
  (row timing is all the capture knows), and pieces after the first get ``<row id>.<n>`` ids
  so ids stay unique and stable across reads.
- An utterance never grows past ``max_chars``: unpunctuated speech still comes out in pieces.
- A row carrying a translation is left alone (its translation can't be split or joined).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from api.persistence.models import Segment

MAX_CHARS = 2000  # the Turma hub's per-segment cap (turma.SEGMENT_TEXT_MAX)

_END = re.compile(r"[.?!…。！？][\"'”’)\]]*$")
# A sentence end followed by a space and a capital, digit or opening quote. Requiring the
# capital keeps "e.g. the" whole; the short title list keeps "Dr. Smith" whole.
_CUT = re.compile(r"(?<=[.?!…])[\"'”’)\]]*\s+(?=[A-Z0-9\"'“‘(])")
_TITLES = re.compile(r"(?:^|\s)(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Prof|vs)\.$")


def _pieces(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in _CUT.finditer(text):
        head = text[start : m.start()] + m.group().rstrip()
        if _TITLES.search(text[: m.start()]):
            continue
        out.append(head.strip())
        start = m.end()
    out.append(text[start:].strip())
    return [p for p in out if p]


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
                and cur.speaker == seg.speaker
                and not _END.search(cur.text)
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
