"""Turn → per-speaker pieces (XERK-2958).

Pure: frame-level speaker activity in, a list of pieces out. Every number that decides
where a turn is cut lives here, so the policy is tested without a model or a GPU.

A *piece* is a span of the turn one speaker owns. Pieces of different speakers never
overlap in time unless both spoke at once; an overlapped piece is marked ``separate`` so
the server hands back that speaker's separated audio instead of the raw mix.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FRAME_S = 0.08  # Sortformer's output frame (8 x 10 ms features)

# A speaker counts as talking in a frame at this activity probability (Sortformer's own
# threshold in its post-processing).
ACTIVE_P = 0.5
# Gaps shorter than this inside one speaker's talk are breaths, not turn ends.
MERGE_GAP_S = 0.4
# A speaker interval shorter than this is a flicker of the model, not a turn. Short real
# back-channels ("yeah") run ~0.3-0.5 s.
MIN_SPEECH_S = 0.32
# Overlap shorter than this is two turns touching (one starts as the other ends); it is
# cut at the boundary, not separated. Separation of <0.5 s of mix buys nothing a
# transcriber can read and costs a model call.
MIN_OVERLAP_S = 0.48
# Separated pieces get this much context either side: the separator keeps a speaker's
# stream steadier with some of their solo speech in the window.
SEPARATE_PAD_S = 0.24
# A piece shorter than this is merged into a neighbour rather than transcribed alone:
# Whisper on <0.6 s of audio mostly invents ("Thank you.").
MIN_PIECE_S = 0.6


@dataclass(frozen=True)
class Piece:
    speaker: int  # 1-based, session-stable (Sortformer arrival order)
    start_s: float
    end_s: float
    separate: bool  # overlapped: transcribe this speaker's separated stream
    overlap_s: float = 0.0  # longest stretch shared with one other speaker


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) frame runs where mask is true."""
    if not mask.any():
        return []
    padded = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def speaker_intervals(probs: np.ndarray) -> dict[int, list[tuple[int, int]]]:
    """Per speaker index (0-based), smoothed [start, end) frame intervals of talk."""
    gap = round(MERGE_GAP_S / FRAME_S)
    min_len = round(MIN_SPEECH_S / FRAME_S)
    out: dict[int, list[tuple[int, int]]] = {}
    for k in range(probs.shape[1]):
        merged: list[tuple[int, int]] = []
        for a, b in _runs(probs[:, k] > ACTIVE_P):
            if merged and a - merged[-1][1] <= gap:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        kept = [(a, b) for a, b in merged if b - a >= min_len]
        if kept:
            out[k] = kept
    return out


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def plan_pieces(probs: np.ndarray, turn_s: float) -> list[Piece]:
    """Cut one turn into per-speaker pieces.

    ``probs`` is frames x speakers activity for the turn (frame 0 = turn start). The
    result covers the whole turn in time order: sequential talk is cut at the middle of
    the gap between speakers, so no word in a pause the model scored as silence is
    lost; overlapped talk becomes one ``separate`` piece per speaker over the shared span.

    No speaker heard → ``[]`` (the caller transcribes the turn unlabelled). One speaker →
    one piece covering the turn, so a single-speaker turn decodes exactly as it does
    without the diarizer.
    """
    ivs = speaker_intervals(probs)
    if not ivs:
        return []
    min_ov = round(MIN_OVERLAP_S / FRAME_S)
    flat = sorted((a, b, k) for k, lst in ivs.items() for a, b in lst)

    # Mark intervals that genuinely overlap another speaker's.
    sep = [False] * len(flat)
    shared = [0] * len(flat)
    for i, (a, b, k) in enumerate(flat):
        for j, (c, d, m) in enumerate(flat):
            ov = _overlap((a, b), (c, d)) if m != k else 0
            if ov >= min_ov:
                sep[i] = sep[j] = True
                shared[i] = max(shared[i], ov)

    # Sequential (non-separated) intervals: fold short ones into a neighbour of the same
    # timeline, then tile the turn by cutting at gap midpoints.
    seq = [(a, b, k) for (a, b, k), s in zip(flat, sep, strict=True) if not s]
    seq = _absorb_short(seq)
    pieces: list[Piece] = []
    n = probs.shape[0]
    # A sequential piece grows into the gap up to the middle of the pause before the next
    # interval of ANY speaker — overlapped ones included. Tiling against sequential
    # neighbours only stretched a lone solo stretch over the whole turn, overlaps and all,
    # and every word of the turn was then captioned under that one speaker (QA).
    timeline = seq + [x for x, s in zip(flat, sep, strict=True) if s]
    for a, b, k in seq:
        before = [d for c, d, m in timeline if (c, d, m) != (a, b, k) and c < a]
        after = [c for c, d, m in timeline if (c, d, m) != (a, b, k) and d > b]
        prev_end = max(before, default=None)
        next_start = min(after, default=None)
        lo = 0 if prev_end is None else (a if prev_end >= a else (prev_end + a) // 2)
        hi = n if next_start is None else (b if next_start <= b else (b + next_start) // 2)
        pieces.append(Piece(k + 1, lo * FRAME_S, min(turn_s, hi * FRAME_S), False))

    pad = SEPARATE_PAD_S
    for (a, b, k), s, ov in zip(flat, sep, shared, strict=True):
        if s:
            pieces.append(
                Piece(
                    k + 1,
                    max(0.0, a * FRAME_S - pad),
                    min(turn_s, b * FRAME_S + pad),
                    True,
                    ov * FRAME_S,
                )
            )

    pieces.sort(key=lambda p: (p.start_s, p.speaker))
    if not any(p.separate for p in pieces):
        pieces = _join_same_speaker(pieces)
    if len(pieces) == 1:
        p = pieces[0]
        return [Piece(p.speaker, 0.0, turn_s, False)]
    return pieces


def _absorb_short(seq: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Merge a too-short sequential interval into its longer neighbour."""
    min_len = round(MIN_PIECE_S / FRAME_S)
    out = list(seq)
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, (a, b, k) in enumerate(out):
            if b - a >= min_len:
                continue
            left = out[i - 1] if i > 0 else None
            right = out[i + 1] if i + 1 < len(out) else None
            target = max((x for x in (left, right) if x is not None), key=lambda x: x[1] - x[0])
            j = out.index(target)
            ta, tb, tk = target
            out[j] = (min(ta, a), max(tb, b), tk)
            del out[i]
            changed = True
            break
    # Neighbours that now share a speaker are one interval.
    joined: list[tuple[int, int, int]] = []
    for a, b, k in out:
        if joined and joined[-1][2] == k:
            joined[-1] = (joined[-1][0], b, k)
        else:
            joined.append((a, b, k))
    return joined


def _join_same_speaker(pieces: list[Piece]) -> list[Piece]:
    out: list[Piece] = []
    for p in pieces:
        if out and out[-1].speaker == p.speaker:
            out[-1] = Piece(p.speaker, out[-1].start_s, p.end_s, False)
        else:
            out.append(p)
    return out


def assign_streams(envelopes: np.ndarray, activity: np.ndarray) -> list[int]:
    """Which separated stream belongs to which speaker.

    ``envelopes`` is streams x frames RMS of the separator's outputs over a window;
    ``activity`` is speakers x frames activity of the speakers overlapping there. Returns,
    per speaker row, the stream index whose energy follows that speaker's activity best
    (Pearson). With two streams and two speakers the pair is chosen jointly, so both
    never take the same stream.
    """

    def corr(x: np.ndarray, y: np.ndarray) -> float:
        if x.std() < 1e-6 or y.std() < 1e-6:
            return 0.0
        return float(np.corrcoef(x, y)[0, 1])

    c = np.array(
        [
            [corr(envelopes[s], activity[k]) for s in range(len(envelopes))]
            for k in range(len(activity))
        ]
    )
    if len(activity) == 2 and len(envelopes) == 2:
        return [0, 1] if c[0, 0] + c[1, 1] >= c[0, 1] + c[1, 0] else [1, 0]
    return [int(np.argmax(row)) for row in c]
