"""Aggregate a judged replay into a scorecard. Usage: see README.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("judged", type=Path, nargs="+", help="judge.py output file(s)")
    args = ap.parse_args()

    header = ("file", "cues", "attempts", "emit%", "novelty", "relevance",
              "accuracy", "restates", "wrong", "dups")
    print(("{:>28}" + "{:>10}" * (len(header) - 1)).format(*header))
    for path in args.judged:
        data = json.loads(path.read_text())
        cues = [c for conv in data["conversations"] for c in conv["cues"]]
        attempts = sum(conv["attempts"] for conv in data["conversations"])
        scores: dict[str, list[int]] = {"novelty": [], "relevance": [], "accuracy": []}
        restates = wrong = dups = 0
        for cue in cues:
            judge = cue.get("judge") or {}
            for key, values in scores.items():
                if isinstance(judge.get(key), (int, float)):
                    values.append(judge[key])
            restates += judge.get("novelty") == 0
            wrong += judge.get("accuracy") == 0
            dups += bool(judge.get("duplicate"))
        mean = {
            k: (sum(v) / len(v) if v else float("nan")) for k, v in scores.items()
        }
        print(("{:>28}" + "{:>10}" * (len(header) - 1)).format(
            path.name[:28], len(cues), attempts,
            f"{100 * len(cues) / attempts:.0f}" if attempts else "-",
            f"{mean['novelty']:.2f}", f"{mean['relevance']:.2f}",
            f"{mean['accuracy']:.2f}", restates, wrong, dups))
        print("  per-conversation: " + ", ".join(
            f"{conv['conversation_id'][:8]}={len(conv['cues'])}"
            for conv in data["conversations"]))


if __name__ == "__main__":
    main()
