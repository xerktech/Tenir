"""Replay sampled meeting frames through the live caption path and measure what it stores.

The screen sampler (``API_SCREEN_SAMPLE_DIR``, XERK-2705) keeps a window's frames as
``<dir>/<platform>-<slug>-<hash>/<ts>-<n>-<region>.jpg`` plus a ``.json`` of the OCR read the
API made of it. This replays one such window, in ``ts`` order, through the production
``MeetingTracker`` (parser, ``CaptionMerger``, meeting start/end) into an in-memory store, with
no OCR service: the saved reads are reused. Run it from ``api/`` (``pip install -e .``)::

    python ../scripts/caption_ocr/caption_replay.py <dir>/teams-<slug>-<hash>
    python ../scripts/caption_ocr/caption_replay.py <window> --drop 0.3 --runs 20 --rows
    python ../scripts/caption_ocr/caption_replay.py <window> --truth truth.json --json

It reports (XERK-2731):

- ``duplicate_rows`` / ``duplicate_words``: a stored row whose words are mostly another
  recent row's (a rewritten entry stored twice). Real frames only: synthetic panes and stored
  exports can't show these. A line really said twice close together counts too, so read the
  rows (``--rows``) before tuning on the number.
- With ``--drop P``: each run drops that share of the caption crops (seeded) and is compared
  with the full replay. ``speaker_changed`` counts rows stored under another speaker than the
  full replay gave them (a header-less continuation after a scroll taking the previous
  speaker), ``missing``/``extra`` rows it lost or added (look-alike entries merged or split).
- With ``--truth`` (a JSON list of ``[speaker, text]``, the meeting's own transcript or a
  hand labelling): the same comparison against it, for absolute rates.

Replay differs from live in one way: the Teams caption pane is found from the full frame's own
OCR (its heading line) instead of a second OCR read of the strip above the divider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from api.persistence import InMemoryConversationStore, ScreenMeta
from api.screen import teams, teams_pane, zoom
from api.screen.meetings import Cadence, MeetingTracker
from api.screen.ocr import OcrLine, OcrResult, image_size, parse_result
from api.screen.zoom import CaptionPane

_NAME = re.compile(r"^(?P<ts>\d+)-(?P<n>\d+)-(?P<region>caption|full)$")
_PARSERS = {"teams": teams, "zoom": zoom}
DUP_SHARE = 0.8  # a row is a duplicate when this share of its words is in a recent row
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
    platform = path.name.split("-", 1)[0]
    if platform not in _PARSERS:
        raise SystemExit(f"{path.name}: not a <platform>-<slug>-<hash> sampler directory")
    frames = []
    for jpg in path.glob("*.jpg"):
        m = _NAME.match(jpg.stem)
        meta = jpg.with_suffix(".json")
        if m is None or not meta.exists():
            continue
        jpeg = jpg.read_bytes()
        frames.append(
            Frame(
                int(m["ts"]),
                int(m["n"]),
                m["region"],
                jpeg,
                load_ocr(json.loads(meta.read_text()), jpeg),
            )
        )
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


async def replay(platform: str, frames: list[Frame], *, end_ms: int = 180_000) -> list[Row]:
    """Feed ``frames`` through a fresh tracker; the rows every meeting stored, in order."""
    store = InMemoryConversationStore()
    tracker = MeetingTracker(
        "replay",
        "replay",
        ScreenMeta("replay", "Replay", platform),
        store=store,
        account_exists=lambda _: True,
        cadence=Cadence(0.2, 1.0, 10.0),
        stable_ms=3000,
        end_ms=end_ms,
    )
    parser = _PARSERS[platform]
    ended: list[str] = []
    last = 0
    for f in frames:
        if tracker.expired(f.ts) and (cid := await tracker.end(f.ts)):
            ended.append(cid)
        ts = tracker.clock(f.ts)
        last = ts
        if f.region == "full":
            if platform == "teams":
                frame = teams.parse_full_frame(f.ocr, f.jpeg, pane=find_pane(f))
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
    rows: list[Row] = []
    for cid in ended:
        conv = store.get("replay", cid)
        assert conv is not None
        rows += [(s.speaker, s.text) for s in sorted(conv.segments, key=lambda s: s.start_ms)]
    return rows


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def duplicates(rows: list[Row]) -> tuple[int, int, int]:
    """(duplicate rows, their words, all words): rows mostly repeating a recent row."""
    dup_rows = dup_words = total = 0
    for i, (_, text) in enumerate(rows):
        words = _words(text)
        total += len(words)
        if not words:
            continue
        for _, earlier in rows[max(0, i - DUP_WINDOW) : i]:
            seen = set(_words(earlier))
            if sum(w in seen for w in words) >= DUP_SHARE * len(words):
                dup_rows += 1
                dup_words += len(words)
                break
    return dup_rows, dup_words, total


def compare(rows: list[Row], reference: list[Row]) -> dict[str, int]:
    """Match each reference row to a row of ``rows`` (in order, by words) and count the drift."""
    used: set[int] = set()
    changed = missing = 0
    start = 0
    for speaker, text in reference:
        ref = _words(text)
        best, best_j = 0.0, None
        for j in range(start, len(rows)):
            if j in used:
                continue
            r = SequenceMatcher(None, ref, _words(rows[j][1]), autojunk=False).ratio()
            if r > best:
                best, best_j = r, j
        if best_j is None or best < MATCH_RATIO:
            missing += 1
            continue
        used.add(best_j)
        start = max(0, best_j - DUP_WINDOW)  # rows can store slightly out of order
        changed += rows[best_j][0] != speaker
    return {
        "matched": len(used),
        "speaker_changed": changed,
        "missing": missing,
        "extra": len(rows) - len(used),
    }


def drop(frames: list[Frame], share: float, rng: random.Random) -> list[Frame]:
    """``frames`` without ``share`` of the caption crops (full frames are kept)."""
    return [f for f in frames if f.region == "full" or rng.random() >= share]


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
    rows = await replay(platform, frames)
    report: dict = {
        "platform": platform,
        "frames": len(frames),
        "caption_frames": sum(f.region == "caption" for f in frames),
        **summarize(rows),
    }
    if args.rows:
        report["stored"] = rows
    if args.truth:
        truth = [(s, t) for s, t in json.loads(Path(args.truth).read_text())]
        report["vs_truth"] = compare(rows, truth)
    if args.drop:
        rng = random.Random(args.seed)
        runs = []
        for _ in range(args.runs):
            dropped = await replay(platform, drop(frames, args.drop, rng))
            runs.append({**summarize(dropped), **compare(dropped, rows)})
        report["drop"] = {"share": args.drop, "seed": args.seed, "runs": runs}
        report["drop"]["total"] = {
            k: sum(r[k] for r in runs)
            for k in ("rows", "duplicate_rows", "matched", "speaker_changed", "missing", "extra")
        }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("window", help="one sampler window directory: <platform>-<slug>-<hash>")
    ap.add_argument("--drop", type=float, default=0.0, help="share of caption crops to drop")
    ap.add_argument("--runs", type=int, default=10, help="dropped-frame runs (with --drop)")
    ap.add_argument("--seed", type=int, default=2731)
    ap.add_argument("--truth", help="JSON list of [speaker, text] to score against")
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
