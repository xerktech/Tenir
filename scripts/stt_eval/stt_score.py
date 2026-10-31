"""Score a transcribe.py run: WER against references, plus latency stats.

References resolve per segment: a hand-corrected text from ``--refs`` when one
exists, else the stored production transcript. Until segments are
hand-corrected, the numbers therefore measure **divergence from the deployed
model's output, not accuracy** — the report labels the two cases separately so
that distinction can't get lost. The ``--review-out`` sheet lists segments by
divergence so hand-correction effort goes where the models actually disagree.

Text normalization matches docs/stt-model-gpu-benchmark.md: both sides NFKC'd,
lower-cased, punctuation stripped. WER is jiwer's.

Usage: see scripts/stt_eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from pathlib import Path

import jiwer


def normalize(text: str) -> str:
    """NFKC, lower-case, punctuation/symbols -> space, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).lower()
    chars = [" " if unicodedata.category(c)[0] in ("P", "S") else c for c in text]
    return " ".join("".join(chars).split())


def score_pair(ref: str, hyp: str) -> dict:
    """Word-level error counts for one segment (normalized both sides).

    jiwer can't take an empty reference, so a hyp-only segment counts entirely
    as insertions and a ref-only one entirely as deletions.
    """
    ref_n, hyp_n = normalize(ref), normalize(hyp)
    if not ref_n and not hyp_n:
        return {"ref_words": 0, "sub": 0, "del": 0, "ins": 0, "hits": 0}
    if not ref_n:
        return {"ref_words": 0, "sub": 0, "del": 0, "ins": len(hyp_n.split()), "hits": 0}
    if not hyp_n:
        n = len(ref_n.split())
        return {"ref_words": n, "sub": 0, "del": n, "ins": 0, "hits": 0}
    out = jiwer.process_words(ref_n, hyp_n)
    return {
        "ref_words": out.hits + out.substitutions + out.deletions,
        "sub": out.substitutions,
        "del": out.deletions,
        "ins": out.insertions,
        "hits": out.hits,
    }


def aggregate(entries: list[dict]) -> dict:
    """Pooled WER over a set of scored segments (errors / reference words)."""
    ref_words = sum(e["score"]["ref_words"] for e in entries)
    sub = sum(e["score"]["sub"] for e in entries)
    dele = sum(e["score"]["del"] for e in entries)
    ins = sum(e["score"]["ins"] for e in entries)
    errors = sub + dele + ins
    return {
        "segments": len(entries),
        "ref_words": ref_words,
        "sub": sub,
        "del": dele,
        "ins": ins,
        "wer": round(errors / ref_words, 4) if ref_words else None,
    }


def timing(entries: list[dict]) -> dict:
    walls = sorted(e["wall_ms"] for e in entries)
    if not walls:
        return {}
    audio_s = sum(e["audio_ms"] for e in entries) / 1000
    wall_s = sum(walls) / 1000
    return {
        "decodes": len(walls),
        "audio_s": round(audio_s, 1),
        "wall_ms_p50": round(statistics.median(walls), 1),
        "wall_ms_p95": round(walls[int(len(walls) * 0.95) - 1], 1),
        "wall_ms_mean": round(statistics.fmean(walls), 1),
        # Realtime factor: seconds of audio decoded per wall second, requests
        # serialized like production (the server holds one GPU lock anyway).
        "rtfx": round(audio_s / wall_s, 1) if wall_s else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path, help="output of transcribe.py")
    ap.add_argument(
        "--refs",
        type=Path,
        help="hand-corrected references JSON: {segment_id: corrected text}",
    )
    ap.add_argument("--out", type=Path, help="default: <run>.scored.json")
    ap.add_argument(
        "--review-out",
        type=Path,
        help="write a TSV of segments ranked by divergence, for hand-correction",
    )
    args = ap.parse_args()

    data = json.loads(args.run.read_text())
    refs: dict[str, str] = json.loads(args.refs.read_text()) if args.refs else {}

    entries = []
    for row in data["results"]:
        corrected = row["segment_id"] in refs
        ref = refs.get(row["segment_id"], row["stored_text"])
        entry = {
            **row,
            "ref_text": ref,
            "ref_source": "corrected" if corrected else "stored",
            "score": score_pair(ref, row["hyp_text"]),
        }
        s = entry["score"]
        errors = s["sub"] + s["del"] + s["ins"]
        entry["seg_wer"] = round(errors / s["ref_words"], 4) if s["ref_words"] else (1.0 if errors else 0.0)
        entries.append(entry)

    corrected = [e for e in entries if e["ref_source"] == "corrected"]
    stored = [e for e in entries if e["ref_source"] == "stored"]
    by_conv = {}
    for e in entries:
        by_conv.setdefault(e["conversation_id"], []).append(e)

    scored = {
        "meta": data["meta"],
        "overall": aggregate(entries),
        "vs_corrected_refs": aggregate(corrected),
        "vs_stored_production": aggregate(stored),
        "timing": timing(entries),
        "per_conversation": {cid: aggregate(rows) for cid, rows in sorted(by_conv.items())},
        "segments": entries,
    }
    out = args.out or args.run.with_suffix(".scored.json")
    out.write_text(json.dumps(scored, indent=1))

    print(f"model: {data['meta']['model']}  ({data['meta']['endpoint']})")
    if corrected:
        a = scored["vs_corrected_refs"]
        print(f"WER vs corrected refs: {a['wer']:.2%}  ({a['segments']} segments, {a['ref_words']} words)")
    if stored:
        a = scored["vs_stored_production"]
        label = "divergence vs stored production output (NOT accuracy — no corrected refs)" if not corrected else "divergence vs stored production output"
        print(f"{label}: {a['wer']:.2%}  ({a['segments']} segments, {a['ref_words']} words)")
    t = scored["timing"]
    if t:
        print(
            f"latency: p50 {t['wall_ms_p50']}ms  p95 {t['wall_ms_p95']}ms per decode; "
            f"{t['audio_s']}s audio, RTFx {t['rtfx']}"
        )
    print(f"wrote {out}")

    if args.review_out:
        ranked = sorted(entries, key=lambda e: (-e["seg_wer"], -e["score"]["ref_words"]))
        lines = ["segment_id\tconversation_id\tstart_ms\tseg_wer\tref_source\tref_text\thyp_text"]
        for e in ranked:
            if e["seg_wer"] == 0:
                continue
            lines.append(
                "\t".join(
                    [
                        e["segment_id"],
                        e["conversation_id"],
                        str(e["start_ms"]),
                        f"{e['seg_wer']:.2f}",
                        e["ref_source"],
                        e["ref_text"].replace("\t", " "),
                        e["hyp_text"].replace("\t", " "),
                    ]
                )
            )
        args.review_out.write_text("\n".join(lines) + "\n")
        print(f"review sheet ({len(lines) - 1} diverging segments) -> {args.review_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
