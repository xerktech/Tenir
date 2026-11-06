"""Replay sampled meeting frames through the live caption path and measure what it stores.

The screen sampler (``API_SCREEN_SAMPLE_DIR``, XERK-2705) keeps a window's frames as
``<dir>/<platform>-<slug>-<hash>/<ts>-<n>-<region>.jpg`` plus a ``.json`` of the OCR read the
API made of it. This replays one such window, in ``(ts, n)`` order, through the production
``MeetingTracker`` (parser, ``CaptionMerger``, meeting start/end, the deployment's
``API_SCREEN_*`` timings) into an in-memory store. Run it from ``api/`` (``pip install -e .``)::

    python ../scripts/caption_ocr/caption_replay.py <dir>/teams-<slug>-<hash>
    python ../scripts/caption_ocr/caption_replay.py <window> --drop 0.3 --runs 20 --rows
    python ../scripts/caption_ocr/caption_replay.py <window> --truth truth.json --json
    python ../scripts/caption_ocr/caption_replay.py <window> --ocr http://localhost:8080

It reports (XERK-2731):

- ``duplicate_rows`` / ``duplicate_words``: a row of 3+ words whose speaker said nearly the
  same words in a recent row (a rewritten entry stored twice). Real frames only: synthetic
  panes and stored exports can't show these. A sentence really said twice counts too, so read
  the rows (``--rows``) before tuning on the number.
- With ``--drop P``: each run drops that share of the caption crops (seeded) and is compared
  with the full replay. ``speaker_changed`` counts rows stored under another speaker than the
  full replay gave them (a header-less continuation after a scroll taking the previous
  speaker), ``missing``/``extra`` rows it lost or added (look-alike entries merged or split).
- With ``--truth`` (a JSON list of ``[speaker, text]`` or ``{"speaker", "text"}``: the
  meeting's own transcript or a hand labelling): the same comparison against it.

The OCR reads are reused, with one gap: live, a Teams full frame's caption pane is confirmed by a
second OCR read of the strip above its divider (``ws.locate_pane``), which the sampler doesn't
keep. By default the full frame's own OCR stands in for it; when that missed the "Live Captions"
heading, the pane is lost, caption lines can join the roster and rows the live path stored are
lost (``full_frames_without_pane`` counts such frames). ``--ocr`` re-reads the strips with a
tenir-ocr service (``kubectl port-forward``), exactly as live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from api.config import settings
from api.persistence import InMemoryConversationStore, ScreenMeta
from api.screen import teams, teams_pane, zoom
from api.screen.meetings import Cadence, MeetingTracker
from api.screen.ocr import (
    HttpOcrClient,
    OcrClient,
    OcrError,
    OcrLine,
    OcrResult,
    image_size,
    parse_result,
)
from api.screen.zoom import CaptionPane

_NAME = re.compile(r"^(?P<ts>\d+)-(?P<n>\d+)-(?P<region>caption|full)$")
_PARSERS = {"teams": teams, "zoom": zoom}
DUP_SHARE = 0.8  # a row is a duplicate when this share of its words is in a recent row
DUP_MIN_WORDS = 3  # shorter rows ("Yes.", "Thanks Kunal.") are stock replies, not rewrites
DUP_WINDOW = 8  # how many earlier rows a duplicate is looked for in
MATCH_RATIO = 0.8  # word-sequence similarity for two rows to be the same caption line


@dataclass(frozen=True)
class Frame:
    ts: int
    n: int
    region: str
    jpeg: bytes
    ocr: OcrResult


Row = tuple[str | None, str]


def load_ocr(data: dict, jpeg: bytes) -> OcrResult:
    """An ``OcrResult`` from a sampler ``.json`` (the OCR service's answer, size optional)."""
    if "width" in data and "height" in data:
        size = (int(data["width"]), int(data["height"]))
    else:
        size = image_size(jpeg)
    return parse_result(data, size)


def load_window(path: Path) -> tuple[str, list[Frame]]:
    """The platform (from the directory name) and the window's frames in replay order."""
    path = path.resolve()
    platform = path.name.split("-", 1)[0]
    if platform not in _PARSERS:
        raise SystemExit(f"{path}: not a <platform>-<slug>-<hash> sampler directory")
    frames = []
    for jpg in path.glob("*.jpg"):
        m = _NAME.match(jpg.stem)
        meta = jpg.with_suffix(".json")
        if m is None or not meta.exists():
            continue
        jpeg = jpg.read_bytes()
        try:
            ocr = load_ocr(json.loads(meta.read_text()), jpeg)
        except Exception as exc:  # one bad file names itself rather than a bare traceback
            raise SystemExit(f"{meta}: unreadable OCR read: {exc!r}") from exc
        frames.append(Frame(int(m["ts"]), int(m["n"]), m["region"], jpeg, ocr))
    frames.sort(key=lambda f: (f.ts, f.n))
    return platform, frames


def find_pane(frame: Frame) -> CaptionPane | None:
    """The Teams caption pane, with the full frame's heading line standing in for the band read."""
    size = (frame.ocr.width, frame.ocr.height)
    for cand in teams_pane.candidates(frame.jpeg, size):
        x0, y0, x1, y1 = cand.band
        inside = tuple(
            OcrLine(ln.text, (ln.box[0] - x0, ln.box[1] - y0, ln.box[2] - x0, ln.box[3] - y0))
            for ln in frame.ocr.lines
            if x0 <= ln.cx <= x1 and y0 <= ln.cy <= y1
        )
        pane = teams_pane.pane_of(cand, OcrResult(x1 - x0, y1 - y0, inside), size)
        if pane is not None:
            return pane
    return None


@dataclass
class Replay:
    rows: list[Row]
    meetings: int = 0
    full_frames: int = 0
    full_frames_without_pane: int = 0
    pane_ocr_errors: int = 0


async def replay(platform: str, frames: list[Frame], *, ocr: OcrClient | None = None) -> Replay:
    """Feed ``frames`` through a fresh tracker; the rows every meeting stored, in order."""
    from api.screen.ws import locate_pane  # ws builds the app's OCR client lazily

    store = InMemoryConversationStore()
    tracker = MeetingTracker(
        "replay",
        "replay",
        ScreenMeta("replay", "Replay", platform),
        store=store,
        account_exists=lambda _: True,
        cadence=Cadence(0.2, 1.0, 10.0),
        stable_ms=int(settings.screen_line_stable_seconds * 1000),
        end_ms=int(settings.screen_meeting_end_seconds * 1000),
    )
    parser = _PARSERS[platform]
    out = Replay([])
    ended: list[str] = []
    last = 0
    for f in frames:
        # Live, the sweeper ends an idle meeting every few seconds: before this frame, here.
        if tracker.expired(f.ts) and (cid := await tracker.end(f.ts)):
            ended.append(cid)
        ts = tracker.clock(f.ts)
        last = ts
        if f.region == "full":
            out.full_frames += 1
            if platform == "teams":
                size = (f.ocr.width, f.ocr.height)
                if ocr is None:
                    pane = find_pane(f)
                else:
                    try:
                        pane = await locate_pane(ocr, f.jpeg, size)
                    except OcrError:  # as live (ws.read_pane): this frame parses without one
                        out.pane_ocr_errors += 1
                        pane = None
                out.full_frames_without_pane += pane is None
                frame = teams.parse_full_frame(f.ocr, f.jpeg, pane=pane)
            else:
                frame = parser.parse_full_frame(f.ocr, f.jpeg)
            await tracker.on_full(frame, ts)
        else:
            roster = await tracker.ensure_roster()
            blocks = parser.parse_caption_crop(
                f.ocr,
                roster,
                active=tracker.active,
                live=tracker.conversation_id is not None,
                jpeg=f.jpeg,
            )
            await tracker.on_caption(blocks, ts)
    if cid := await tracker.end(last):
        ended.append(cid)
    out.meetings = len(ended)
    for cid in ended:
        conv = store.get("replay", cid)
        assert conv is not None
        segs = sorted(conv.segments, key=lambda s: s.start_ms)
        out.rows += [(s.speaker, s.text) for s in segs if s.text]  # "" = a dropped line
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def duplicates(rows: list[Row]) -> tuple[int, int, int]:
    """(duplicate rows, their words, all words): rows mostly repeating the same speaker's
    recent row of a similar length."""
    dup_rows = dup_words = total = 0
    for i, (speaker, text) in enumerate(rows):
        words = _words(text)
        total += len(words)
        if len(words) < DUP_MIN_WORDS:
            continue
        for who, earlier in rows[max(0, i - DUP_WINDOW) : i]:
            seen = _words(earlier)
            if who != speaker or not 0.5 <= len(words) / len(seen or [""]) <= 2:
                continue
            if sum(w in set(seen) for w in words) >= DUP_SHARE * len(words):
                dup_rows += 1
                dup_words += len(words)
                break
    return dup_rows, dup_words, total


def _matches(rows: list[Row], reference: list[Row]) -> list[list[tuple[float, int]]]:
    """Per reference row, the (ratio, j) of every row of ``rows`` reading alike. Rows share
    words to be compared at all (an index, not every pair: a meeting has 1000+ rows)."""
    words = [_words(t) for _, t in rows]
    bags = [Counter(ws) for ws in words]
    index: dict[str, set[int]] = {}
    for j, ws in enumerate(words):
        for w in set(ws):
            index.setdefault(w, set()).add(j)
    out = []
    for _, text in reference:
        ref = _words(text)
        bag = Counter(ref)
        shared: set[int] = set()
        for w in set(ref):
            shared |= index.get(w, set())
        found = []
        for j in shared:
            # ratio = 2 * matched / (la + lb), and matched is at most min(la, lb) and the
            # words both hold: skip pairs either bound keeps under the bar.
            la, lb = len(ref), len(words[j])
            if 2 * min(la, lb) < MATCH_RATIO * (la + lb):
                continue
            if 2 * (bag & bags[j]).total() < MATCH_RATIO * (la + lb):  # common words only
                continue
            r = SequenceMatcher(None, ref, words[j], autojunk=False).ratio()
            if r >= MATCH_RATIO:
                found.append((r, j))
        out.append(found)
    return out


def compare(rows: list[Row], reference: list[Row]) -> dict[str, int]:
    """Pair reference rows with alike rows of ``rows`` and count the drift.

    The pairing is the longest in-order one (a longest increasing subsequence over the alike
    pairs), so no run of unmatched rows on either side can throw the rest out of step: a
    greedy search paired a reworded line with its repeat far ahead (QA), and a bounded one
    lost sync for good after 16 unmatched rows (QA). Among the longest chains, the one pairing
    most rows with their own speaker wins: a speaker-blind chain paired the same words said by
    two speakers across them when one was dropped, and a same-speaker-only chain hid real
    misattribution (both QA). A reference row left over then takes the most alike unused row
    within ``DUP_WINDOW`` of its neighbours' pairs, its own speaker's first: rows can store
    slightly out of order.
    """
    import bisect

    alike = _matches(rows, reference)
    # A weighted LIS over j (reference rows in order), scored (pairs, same-speaker pairs),
    # with a Fenwick tree of prefix maxima over j. One i's candidates go in descending j, so
    # none of them chains onto another of the same i.
    size = len(rows)
    tree: list[tuple[int, int, tuple[int, int] | None]] = [(0, 0, None)] * (size + 1)

    def best_below(j: int) -> tuple[int, int, tuple[int, int] | None]:
        out = (0, 0, None)
        while j > 0:
            out = max(out, tree[j], key=lambda t: t[:2])
            j -= j & -j
        return out

    def put(j: int, value: tuple[int, int, tuple[int, int] | None]) -> None:
        j += 1
        while j <= size:
            tree[j] = max(tree[j], value, key=lambda t: t[:2])
            j += j & -j

    back: dict[tuple[int, int], tuple[int, int] | None] = {}
    end: tuple[int, int, tuple[int, int] | None] = (0, 0, None)
    for i, found in enumerate(alike):
        for _, j in sorted(found, key=lambda x: -x[1]):
            n, same, prev = best_below(j)
            node = (n + 1, same + (rows[j][0] == reference[i][0]), (i, j))
            back[(i, j)] = prev
            put(j, node)
            end = max(end, node, key=lambda t: t[:2])
    pair: dict[int, int] = {}
    at = end[2]
    while at is not None:
        pair[at[0]] = at[1]
        at = back[at]
    used = set(pair.values())
    paired = sorted(pair)
    for i, found in enumerate(alike):
        if i in pair or not found:
            continue
        k = bisect.bisect_left(paired, i)
        lo = pair[paired[k - 1]] - DUP_WINDOW if k else 0
        hi = pair[paired[k]] + DUP_WINDOW if k < len(paired) else len(rows)
        speaker = reference[i][0]
        near = [(rows[j][0] == speaker, r, -j) for r, j in found if lo <= j <= hi and j not in used]
        if near:
            pair[i] = -max(near)[2]
            used.add(pair[i])
    changed = sum(rows[j][0] != reference[i][0] for i, j in pair.items())
    return {
        "matched": len(pair),
        "speaker_changed": changed,
        "missing": len(reference) - len(pair),
        "extra": len(rows) - len(pair),
    }


def drop(frames: list[Frame], share: float, rng: random.Random) -> list[Frame]:
    """``frames`` without ``share`` of the caption crops (full frames are kept)."""
    return [f for f in frames if f.region == "full" or rng.random() >= share]


def load_truth(path: Path) -> list[Row]:
    bad = f"{path}: not a JSON list of [speaker, text] or {{speaker, text}}"
    try:
        data = json.loads(path.read_text())
        rows = [(x["speaker"], x["text"]) if isinstance(x, dict) else (x[0], x[1]) for x in data]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SystemExit(bad) from exc
    if not all(isinstance(t, str) and (s is None or isinstance(s, str)) for s, t in rows):
        raise SystemExit(bad)
    return rows


def summarize(rows: list[Row]) -> dict[str, int]:
    dup_rows, dup_words, total = duplicates(rows)
    return {
        "rows": len(rows),
        "duplicate_rows": dup_rows,
        "duplicate_words": dup_words,
        "words": total,
    }


async def run(args: argparse.Namespace) -> dict:
    platform, frames = load_window(Path(args.window))
    if not frames:
        raise SystemExit(f"{args.window}: no <ts>-<n>-<region>.jpg/.json frame pairs")
    ocr = HttpOcrClient(args.ocr, timeout=settings.ocr_timeout_seconds) if args.ocr else None
    full = await replay(platform, frames, ocr=ocr)
    rows = full.rows
    report: dict = {
        "platform": platform,
        "frames": len(frames),
        "caption_frames": sum(f.region == "caption" for f in frames),
        "meetings": full.meetings,
        "full_frames_without_pane": full.full_frames_without_pane,
        "pane_ocr_errors": full.pane_ocr_errors,
        **summarize(rows),
    }
    if args.rows:
        report["stored"] = rows
    if args.truth:
        report["vs_truth"] = compare(rows, load_truth(Path(args.truth)))
    if args.drop:
        rng = random.Random(args.seed)
        runs = []
        for _ in range(args.runs):
            dropped = (await replay(platform, drop(frames, args.drop, rng), ocr=ocr)).rows
            runs.append({**summarize(dropped), **compare(dropped, rows)})
        keys = ("rows", "duplicate_rows", "matched", "speaker_changed", "missing", "extra")
        report["drop"] = {
            "share": args.drop,
            "seed": args.seed,
            "runs": runs,
            "total": {k: sum(r[k] for r in runs) for k in keys},
        }
    return report


def _share(text: str) -> float:
    value = float(text)
    if not 0 <= value < 1:
        raise argparse.ArgumentTypeError("a share in [0, 1)")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("window", help="one sampler window directory: <platform>-<slug>-<hash>")
    ap.add_argument("--drop", type=_share, default=0.0, help="share of caption crops to drop")
    ap.add_argument("--runs", type=_positive, default=10, help="dropped-frame runs (with --drop)")
    ap.add_argument("--seed", type=int, default=2731)
    ap.add_argument("--truth", help="JSON list of [speaker, text] to score against")
    ap.add_argument("--ocr", help="tenir-ocr URL to re-read Teams pane headings as live does")
    ap.add_argument("--rows", action="store_true", help="include the stored rows")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    report = asyncio.run(run(args))
    if args.json:
        print(json.dumps(report))
        return 0
    for key, value in report.items():
        if key == "stored":
            for speaker, text in value:
                print(f"  {speaker or '?'}: {text}")
        elif key == "drop":
            print(f"drop {value['share']:.0%} x{len(value['runs'])}: {value['total']}")
        else:
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
