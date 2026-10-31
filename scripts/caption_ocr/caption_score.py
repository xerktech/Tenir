"""Scoring for the caption-OCR spike (XERK-2504).

A Zoom caption frame holds a few caption lines, each ``Speaker Name: spoken text``. OCR output is
a list of text lines in reading order. We score:

- line accuracy: share of ground-truth lines whose paired OCR line matches at >= LINE_MATCH_RATIO
  character similarity (a line that is "readable as-is" for downstream extraction);
- char accuracy: mean (1 - CER) over ground-truth lines against their paired OCR line;
- speaker accuracy: share of ground-truth lines whose parsed speaker name matches its OCR line's
  (case- and whitespace-insensitive, otherwise exact) — a wrong name misattributes an action
  item, so no fuzziness;
- line precision: share of OCR lines that are a matched caption line. Extra lines (tile names,
  UI text, captions invented on a frame without any) count against it.

Truth and OCR lines are paired one-to-one, best similarity first, so one OCR line can never
satisfy two caption lines.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

from rapidfuzz.distance import Levenshtein

LINE_MATCH_RATIO = 0.95
# A speaker label is a short run of name-ish words followed by a colon at the start of the line.
_SPEAKER_RE = re.compile(r"^\s*([^\W\d_][\w.'\- ]{0,40}?)\s*:\s*(.*)$")


def normalize(text: str) -> str:
    """Collapse whitespace; keep case and punctuation (names and numbers matter downstream)."""
    return " ".join(text.split())


def split_speaker(line: str) -> tuple[str | None, str]:
    """Split ``"Name: text"`` into (name, text); (None, line) when there is no speaker label."""
    m = _SPEAKER_RE.match(line)
    if not m:
        return None, normalize(line)
    return normalize(m.group(1)), normalize(m.group(2))


def char_accuracy(ref: str, hyp: str) -> float:
    """1 - CER, floored at 0."""
    ref, hyp = normalize(ref), normalize(hyp)
    if not ref:
        return 1.0 if not hyp else 0.0
    return max(0.0, 1.0 - Levenshtein.distance(ref, hyp) / len(ref))


@dataclass
class FrameScore:
    lines: int = 0
    lines_matched: int = 0
    speakers: int = 0
    speakers_matched: int = 0
    ocr_lines: int = 0
    char_acc: list[float] = field(default_factory=list)


def pair_lines(truth: list[str], ocr: list[str]) -> list[str]:
    """For each truth line, its OCR partner ("" when none), paired one-to-one best-first."""
    pairs = sorted(
        ((char_accuracy(t, o), i, j) for i, t in enumerate(truth) for j, o in enumerate(ocr)),
        reverse=True,
    )
    partner = [""] * len(truth)
    used_t: set[int] = set()
    used_o: set[int] = set()
    for _, i, j in pairs:
        if i not in used_t and j not in used_o:
            partner[i] = ocr[j]
            used_t.add(i)
            used_o.add(j)
    return partner


def score_frame(truth: list[str], ocr: list[str]) -> FrameScore:
    """Score one frame (truth may be empty: a frame with no caption on screen)."""
    s = FrameScore(ocr_lines=len(ocr))
    for ref, best in zip(truth, pair_lines(truth, ocr), strict=False):
        acc = char_accuracy(ref, best)
        s.lines += 1
        s.char_acc.append(acc)
        s.lines_matched += acc >= LINE_MATCH_RATIO
        ref_name, _ = split_speaker(ref)
        if ref_name is not None:
            hyp_name, _ = split_speaker(best)
            s.speakers += 1
            s.speakers_matched += (hyp_name or "").casefold() == ref_name.casefold()
    return s


def aggregate(scores: list[FrameScore], latencies_ms: list[float]) -> dict:
    lines = sum(s.lines for s in scores)
    speakers = sum(s.speakers for s in scores)
    ocr_lines = sum(s.ocr_lines for s in scores)
    matched = sum(s.lines_matched for s in scores)
    accs = [a for s in scores for a in s.char_acc]
    lat = sorted(latencies_ms)
    return {
        "frames": len(scores),
        # NaN, not 0, when the set has no caption lines: there is nothing to be right about
        "line_acc": matched / lines if lines else math.nan,
        "line_precision": matched / ocr_lines if ocr_lines else 1.0,
        "char_acc": statistics.fmean(accs) if accs else math.nan,
        "speaker_acc": sum(s.speakers_matched for s in scores) / speakers if speakers else math.nan,
        "lat_p50_ms": statistics.median(lat) if lat else math.nan,
        # nearest-rank p95 (ceil), so 20 samples give the 19th, not the maximum
        "lat_p95_ms": lat[max(0, math.ceil(0.95 * len(lat)) - 1)] if lat else math.nan,
    }
