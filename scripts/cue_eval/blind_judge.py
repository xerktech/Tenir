"""Blind, cross-family judging of replayed cues.

Model judges share blind spots with the model they grade: in the 2026-09
shoot-out gpt-oss-120b judged 3 of its own 273 cues wrong where a blind
reviewer found 37 (RESULTS-2026-09-shootout.md). This tool pools cues from
several replay runs, strips which run produced them, shuffles, and splits them
into text packs for independent reviewers (e.g. one Claude subagent per pack,
each given RUBRIC). ``report`` un-blinds the verdicts and scores each run.

  python scripts/cue_eval/blind_judge.py pack segments.json run_a.json run_b.json \\
      --packs 4 --out-dir blind/
  # reviewers write blind/verdicts_<n>.jsonl, one JSON object per cue
  python scripts/cue_eval/blind_judge.py report blind/

Always pool the runs being compared into ONE pack set, and include an anchor
run already scored in an earlier batch: reviewer batches drift by a few points,
so only scores from the same batch are comparable. The packs quote transcript
text: keep them in a scratch directory, never in the repo.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
from pathlib import Path

RUBRIC = """\
You are an independent judge grading "cues": short notes an assistant surfaced to a
listener during a live, recorded conversation. The cues come from several AI systems,
shuffled and anonymized; do not try to guess which system wrote which.

Each item in your pack:
### c0123
TRANSCRIPT: (speech-to-text lines; the cue was produced after the last lines)
EARLIER CUES: (up to 8 cues already shown in that conversation)
NEW CUE: title: body

Grade EVERY NEW CUE:
- novelty: 2 = key info NOT in the transcript; 1 = mostly restates but adds a little;
  0 = pure restatement. Answering a question the speakers asked aloud counts as novel.
- relevance: 2 = about what is discussed now and a listener would care; 1 = tangential;
  0 = off-topic, or about a garbled/misheard/nonexistent thing.
- accuracy: 2 = confident every factual claim is correct; 1 = a key claim is
  unverifiable to you, or it asserts facts about an entity that looks misheard;
  0 = a claim is wrong, contradicts the transcript's firsthand details, or invents facts
  about a misrecognized name. Post-cutoff products/events are 1, not 0, unless
  internally inconsistent.
- duplicate: true if its key info was already conveyed by an EARLIER CUE.
Accuracy matters most: a confident wrong cue is the worst failure.

Write JSON Lines, one object per cue, every id exactly once:
{"id": "c0123", "novelty": 2, "relevance": 2, "accuracy": 2, "duplicate": false,
 "reason": "<=20 words; required when accuracy < 2 or relevance 0"}
"""


def pack(args: argparse.Namespace) -> None:
    by_conv: dict[str, list[dict]] = collections.defaultdict(list)
    for seg in json.loads(args.segments.read_text()):
        by_conv[seg["conversation_id"]].append(seg)
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])

    if args.packs < 1:
        raise SystemExit("--packs must be at least 1")
    names = [p.stem for p in args.runs]
    if len(set(names)) != len(names):
        raise SystemExit(f"run file names must be unique (they label the runs): {names}")
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        # Old verdicts_*.jsonl would attach to the new, reused ids and score silently.
        raise SystemExit(f"{args.out_dir} is not empty; pack into a fresh directory")

    items = []
    for run_path in args.runs:
        run = run_path.stem
        for conv in json.loads(run_path.read_text())["conversations"]:
            segs = by_conv[conv["conversation_id"]]
            for k, cue in enumerate(conv["cues"]):
                i = cue["seg_index"]
                # Up to and including the segment the cue fired on — never a line
                # spoken after it, which would make a cue look like a restatement.
                window = [s["text"].strip() for s in segs[max(0, i - 7) : i + 1] if (s.get("text") or "").strip()]
                prior = [f"{p['title']}: {p['body']}" for p in conv["cues"][max(0, k - 8) : k]]
                items.append({
                    "window": window, "prior": prior, "cue": f"{cue['title']}: {cue['body']}",
                    "key": {"run": run, "conv": conv["conversation_id"], "k": k},
                })
    random.Random(args.seed).shuffle(items)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    key = {}
    size = -(-len(items) // args.packs)
    for p in range(args.packs):
        lines = []
        for n, item in enumerate(items[p * size : (p + 1) * size], start=p * size):
            cid = f"c{n:04d}"
            key[cid] = item["key"]
            lines.append(
                f"### {cid}\nTRANSCRIPT:\n" + "\n".join(item["window"])
                + "\nEARLIER CUES:\n" + ("\n".join(item["prior"]) or "(none)")
                + f"\nNEW CUE: {item['cue']}\n"
            )
        (args.out_dir / f"pack_{p}.txt").write_text("\n".join(lines))
    (args.out_dir / "key.json").write_text(json.dumps(key))
    (args.out_dir / "RUBRIC.md").write_text(RUBRIC)
    print(f"{len(items)} cues -> {args.packs} packs in {args.out_dir}")


def report(args: argparse.Namespace) -> None:
    key = json.loads((args.dir / "key.json").read_text())
    verdicts = {}
    for path in sorted(args.dir.glob("verdicts_*.jsonl")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip():
                try:
                    v = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path.name}:{lineno}: not JSON ({exc})") from exc
                if not isinstance(v, dict) or not isinstance(v.get("id"), str):
                    raise SystemExit(f"{path.name}:{lineno}: not a verdict object with a string id")
                cid = v.get("id")
                if cid not in key:
                    raise SystemExit(f"{path.name}: verdict for unknown id {cid!r}")
                if cid in verdicts:
                    raise SystemExit(f"{path.name}: duplicate verdict for {cid}")
                absent = {"novelty", "relevance", "accuracy", "duplicate"} - v.keys()
                if absent:
                    raise SystemExit(f"{path.name}: {cid} is missing {sorted(absent)}")
                bad = [f for f in ("novelty", "relevance", "accuracy") if v[f] not in (0, 1, 2)
                       or isinstance(v[f], bool)]
                if bad or not isinstance(v["duplicate"], bool):
                    raise SystemExit(
                        f"{path.name}: {cid} needs 0/1/2 scores and a boolean duplicate "
                        f"(bad: {bad or ['duplicate']})"
                    )
                verdicts[cid] = v
    missing = len(key) - len(verdicts)
    by_run: dict[str, list[dict]] = collections.defaultdict(list)
    for cid, k in key.items():
        if cid in verdicts:
            by_run[k["run"]].append(verdicts[cid])
    print(f"verdicts {len(key) - missing}/{len(key)}")
    print(f"{'run':24}{'cues':>6}{'accuracy':>9}{'wrong':>7}{'wrong%':>8}{'off-topic':>10}{'dups':>6}{'perfect':>8}")
    for run, vs in sorted(by_run.items()):
        n = len(vs)
        wrong = sum(v["accuracy"] == 0 for v in vs)
        perfect = sum(
            v["novelty"] == 2 and v["relevance"] == 2 and v["accuracy"] == 2 and not v["duplicate"]
            for v in vs
        )
        print(
            f"{run:24}{n:>6}{sum(v['accuracy'] for v in vs) / n:>9.2f}{wrong:>7}"
            f"{100 * wrong / n:>7.1f}%{sum(v['relevance'] == 0 for v in vs):>10}"
            f"{sum(bool(v['duplicate']) for v in vs):>6}{perfect:>8}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="pool + shuffle runs into reviewer packs")
    p.add_argument("segments", type=Path)
    p.add_argument("runs", type=Path, nargs="+", help="replay.py outputs")
    p.add_argument("--packs", type=int, default=4)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("report", help="un-blind verdicts and score each run")
    r.add_argument("dir", type=Path)
    args = ap.parse_args()
    pack(args) if args.cmd == "pack" else report(args)


if __name__ == "__main__":
    main()
