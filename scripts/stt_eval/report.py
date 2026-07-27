"""Compare scored STT runs side by side.

Takes one or more ``score.py`` outputs and prints a markdown table — one row
per run — so candidate models/settings can be read against the baseline on
identical audio. Accuracy columns stay split by reference source (corrected
refs vs stored production text) because only the former measures accuracy.

Usage: see scripts/stt_eval/README.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt_wer(agg: dict) -> str:
    if not agg or agg.get("wer") is None:
        return "—"
    return f"{agg['wer']:.2%} ({agg['ref_words']}w)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scored", type=Path, nargs="+", help="score.py outputs to compare")
    args = ap.parse_args()

    rows = []
    for path in args.scored:
        data = json.loads(path.read_text())
        meta, t = data["meta"], data.get("timing", {})
        opts = []
        if meta.get("language") not in (None, "auto"):
            opts.append(f"lang={meta['language']}")
        if meta.get("pad_ms"):
            opts.append(f"pad={meta['pad_ms']}ms")
        if not meta.get("timestamps", True):
            opts.append("no-ts")
        rows.append(
            [
                meta["model"] + (f" [{' '.join(opts)}]" if opts else ""),
                str(data["overall"]["segments"]),
                fmt_wer(data.get("vs_corrected_refs")),
                fmt_wer(data.get("vs_stored_production")),
                f"{t['wall_ms_p50']:.0f} / {t['wall_ms_p95']:.0f}" if t else "—",
                str(t.get("rtfx", "—")),
            ]
        )

    headers = [
        "model",
        "segs",
        "WER vs corrected",
        "divergence vs stored",
        "p50/p95 ms",
        "RTFx",
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |")
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows:
        print("| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |")
    print(
        "\n'divergence vs stored' compares against the deployed model's own stored"
        " output — it is a difference measure, not accuracy. Only 'WER vs corrected'"
        " (hand-corrected references) is authoritative."
    )


if __name__ == "__main__":
    main()
