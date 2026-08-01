"""Score one or more judged replay runs and print a comparison table.

Latency comes from replay.py's per-call wall-clock (use a --workers 1 run for
clean numbers); accuracy from judge.py's verdicts, split by what the source
utterance actually was (the judge's source_type, not the langid tag — English
utterances misdetected as foreign are part of the production distribution and
are scored on leave-it-alone behavior).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def pct(ms: list[int], q: float) -> int:
    return ms[min(len(ms) - 1, int(q * len(ms)))] if ms else 0


def summarize(path: Path) -> dict:
    data = json.loads(path.read_text())
    items = data["items"]
    ok = [r for r in items if r.get("translation")]
    judged = [r for r in ok if r.get("judge")]
    ms = sorted(r["call_ms"] for r in items if not r.get("error"))
    comp_tokens = [
        r["usage"]["completion_tokens"]
        for r in items
        if r.get("usage") and r["usage"].get("completion_tokens") is not None
    ]

    def bucket(types: set[str]) -> dict:
        sub = [r for r in judged if r["judge"].get("source_type") in types]
        if not sub:
            return {"n": 0}
        return {
            "n": len(sub),
            "adequacy": sum(r["judge"]["adequacy"] for r in sub) / len(sub),
            "fluency": sum(r["judge"]["fluency"] for r in sub) / len(sub),
            "adequacy0": sum(1 for r in sub if r["judge"]["adequacy"] == 0),
        }

    label = data["model"] + (f" ({data['reasoning_effort']})" if data.get("reasoning_effort") else "")
    return {
        "label": label,
        "file": path.name,
        "n": len(items),
        "translated": len(ok),
        "errors": sum(1 for r in items if r.get("error")),
        "parse_fails": sum(1 for r in items if not r.get("translation") and not r.get("error")),
        "ms_mean": sum(ms) / len(ms) if ms else 0,
        "ms_p50": pct(ms, 0.5),
        "ms_p90": pct(ms, 0.9),
        "ms_max": ms[-1] if ms else 0,
        "completion_tokens_mean": sum(comp_tokens) / len(comp_tokens) if comp_tokens else None,
        "foreign": bucket({"foreign", "mixed"}),
        "english": bucket({"english"}),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("judged", type=Path, nargs="+", help="one or more .judged.json files")
    args = ap.parse_args()

    rows = [summarize(p) for p in args.judged]
    cols = (
        f"{'model':38} {'n':>4} {'ok':>4} {'err':>4} {'mean':>6} {'p50':>6} {'p90':>6} "
        f"{'tok':>5} | {'for-n':>5} {'adeq':>5} {'flu':>5} {'a=0':>4} | {'en-n':>5} {'adeq':>5}"
    )
    print(cols)
    print("-" * len(cols))
    for r in rows:
        f, e = r["foreign"], r["english"]
        tok = f"{r['completion_tokens_mean']:.0f}" if r["completion_tokens_mean"] else "-"
        print(
            f"{r['label'][:38]:38} {r['n']:>4} {r['translated']:>4} {r['errors']:>4} "
            f"{r['ms_mean']:>6.0f} {r['ms_p50']:>6} {r['ms_p90']:>6} {tok:>5} | "
            f"{f['n']:>5} {f.get('adequacy', 0):>5.2f} {f.get('fluency', 0):>5.2f} "
            f"{f.get('adequacy0', 0):>4} | "
            f"{e['n']:>5} {e.get('adequacy', 0):>5.2f}"
        )


if __name__ == "__main__":
    main()
