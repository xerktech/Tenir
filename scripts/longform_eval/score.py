"""Score a long-form STT -> translation run against timed reference captions.

Inputs
- refs: ``{"es": [cue...], "en": [cue...]}``, cue = ``{"start_ms", "end_ms", "text"}`` on
  the full recording's timeline (see build_refs.py).
- run: ws_driver.py output over chunks of one recording; each clip id must be
  ``c<N>`` for chunk N of ``--chunk-sec`` seconds (finals' startMs/endMs are chunk-relative).

STT: WER per chunk over the concatenated finals vs the concatenated Spanish cues
starting in that chunk (long-form WER — segmentation-independent). Reported twice:
``wer`` (NFKC, lower, punctuation stripped) and ``wer_fold`` (also accent-folded),
because OCR'd references drop/garble accents more than they garble words.

Translation: every reference cue is assigned to the final turn it overlaps most
(cues with no overlapping turn become a "missed" unit, scored as empty output), and each
turn is scored as one unit: chrF++ and BLEU (sacrebleu, corpus-level over units) and,
with ``--comet``, COMET-22 per unit (src = the turn's Spanish reference cues, so STT
errors count against the system). Turns the app left untranslated score as empty.

Usage: python score.py refs.json run.json [run2.json ...] [--comet] [--out scores.json]
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata

import jiwer
import sacrebleu


def norm(s: str, fold: bool = False) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    if fold:
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s']|_", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def chunk_index(clip_id: str) -> int:
    m = re.fullmatch(r"c(\d+)", clip_id)
    if not m:
        raise ValueError(f"clip id {clip_id!r} is not c<N>")
    return int(m.group(1))


def cues_in(cues: list[dict], lo_ms: int, hi_ms: int) -> list[dict]:
    return [c for c in cues if lo_ms <= c["start_ms"] < hi_ms]


def wer_counts(ref: str, hyp: str) -> tuple[int, int]:
    """(errors, reference words). An empty reference with output counts every
    hypothesis word as an insertion error."""
    n = len(ref.split())
    if n == 0:
        return len(hyp.split()), 0
    o = jiwer.process_words(ref, hyp)
    return o.substitutions + o.deletions + o.insertions, n


def overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def align_units(finals: list[dict], en: list[dict], es: list[dict], offset_ms: int) -> list[dict]:
    """One unit per final turn (+ one per run of reference cues no turn overlaps).
    Each unit: hyp translation, STT text, joined en reference, joined es reference."""
    turns = [{"t0": f["startMs"] + offset_ms, "t1": f["endMs"] + offset_ms, "stt": f["text"],
              "lang": f.get("lang"), "hyp": f.get("translation"), "en": [], "es": []}
             for f in finals]
    def best(units: list[dict], c: dict) -> dict | None:
        ov = [(overlap(u["t0"], u["t1"], c["start_ms"], c["end_ms"]), i) for i, u in enumerate(units)]
        top = max(ov, default=(0, -1))
        return units[top[1]] if top[0] > 0 else None

    # English cues no turn overlaps: the app showed nothing for that speech. Group
    # them into "missed" units (a >5 s gap starts a new one) scored as empty output.
    missed: list[dict] = []
    for c in en:
        t = best(turns, c)
        if t is not None:
            t["en"].append(c["text"])
            continue
        if not missed or c["start_ms"] - missed[-1]["t1"] > 5000:
            missed.append({"t0": c["start_ms"], "t1": c["end_ms"], "stt": "", "lang": None,
                           "hyp": None, "en": [], "es": [], "missed": True})
        missed[-1]["en"].append(c["text"])
        missed[-1]["t1"] = c["end_ms"]
    units = turns + missed
    for c in es:
        u = best(units, c)
        if u is not None:
            u["es"].append(c["text"])
    for u in units:
        u["en"] = " ".join(u["en"])
        u["es"] = " ".join(u["es"])
    return sorted(units, key=lambda u: u["t0"])


def score_run(refs: dict, run: list[dict], chunk_sec: int) -> dict:
    per_chunk, units = [], []
    tot = {"err": 0, "n": 0, "err_f": 0, "n_f": 0}
    for clip in sorted(run, key=lambda c: chunk_index(c["id"])):
        k = chunk_index(clip["id"])
        lo, hi = k * chunk_sec * 1000, (k + 1) * chunk_sec * 1000
        es, en = cues_in(refs["es"], lo, hi), cues_in(refs["en"], lo, hi)
        finals = clip.get("finals", [])
        hyp = " ".join(f["text"] for f in finals)
        ref = " ".join(c["text"] for c in es)
        e, n = wer_counts(norm(ref), norm(hyp))
        ef, nf = wer_counts(norm(ref, True), norm(hyp, True))
        tot["err"] += e
        tot["n"] += n
        tot["err_f"] += ef
        tot["n_f"] += nf
        cu = align_units(finals, en, es, lo)
        for u in cu:
            u["chunk"] = k
        units += cu
        per_chunk.append({"chunk": k, "finals": len(finals), "ref_words": n,
                          "wer": e / n if n else None, "wer_fold": ef / nf if nf else None,
                          "translated": sum(1 for f in finals if f.get("translation")),
                          "non_en_finals": sum(1 for f in finals if f.get("lang") != "en"),
                          "error": clip.get("error")})
    scored = [u for u in units if u["en"]]
    # Translated turns no English cue overlaps can't be scored (no reference); count
    # them so they aren't invisible (their words are not in chrF/BLEU/COMET).
    orphans = [u for u in units if not u["en"] and u["hyp"]]
    hyps = [u["hyp"] or "" for u in scored]
    refs_en = [u["en"] for u in scored]
    lat = sorted(f["latency_ms"] for c in run for f in c.get("finals", [])
                 if f.get("latency_ms") is not None)
    return {
        "per_chunk": per_chunk,
        "wer": tot["err"] / tot["n"] if tot["n"] else None,
        "wer_fold": tot["err_f"] / tot["n_f"] if tot["n_f"] else None,
        "units": len(scored),
        "missed_units": sum(1 for u in scored if u.get("missed")),
        "untranslated_units": sum(1 for u in scored if not u["hyp"]),
        "unscored_translated_turns": len(orphans),
        "translate_errors": sum(1 for c in run for f in c.get("finals", [])
                                if f.get("translate_error")),
        "chrf": sacrebleu.corpus_chrf(hyps, [refs_en], word_order=2).score,
        "bleu": sacrebleu.corpus_bleu(hyps, [refs_en]).score,
        "latency_ms": {"p50": lat[len(lat) // 2], "p90": lat[int(0.9 * len(lat))]} if lat else None,
        "_units": units,
    }


def add_comet(results: list[dict]) -> None:
    from comet import download_model, load_from_checkpoint

    model = load_from_checkpoint(download_model("Unbabel/wmt22-comet-da"))
    for r in results:
        scored = [u for u in r["_units"] if u["en"]]
        data = [{"src": u["es"] or u["stt"], "mt": u["hyp"] or "", "ref": u["en"]} for u in scored]
        out = model.predict(data, batch_size=32, gpus=0, progress_bar=False)
        for u, s in zip(scored, out.scores):
            u["comet"] = s
        r["comet"] = 100 * sum(out.scores) / len(out.scores)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("refs")
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--chunk-sec", type=int, default=600)
    ap.add_argument("--comet", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    refs = json.load(open(a.refs))
    results = []
    for path in a.runs:
        r = score_run(refs, json.load(open(path)), a.chunk_sec)
        r["run"] = path
        results.append(r)
    if a.comet:
        add_comet(results)
    print(f"{'run':40} {'WER':>6} {'WERf':>6} {'chrF++':>7} {'BLEU':>6} {'COMET':>6} "
          f"{'units':>5} {'miss':>4} {'untr':>4} {'p50ms':>6}")
    for r in results:
        c = f"{r['comet']:.2f}" if "comet" in r else "-"
        p50 = r["latency_ms"]["p50"] if r["latency_ms"] else "-"
        print(f"{r['run'][-40:]:40} {100 * r['wer']:6.2f} {100 * r['wer_fold']:6.2f} "
              f"{r['chrf']:7.2f} {r['bleu']:6.2f} {c:>6} {r['units']:5} {r['missed_units']:4} "
              f"{r['untranslated_units']:4} {p50:>6}")
        for pc in r["per_chunk"]:
            print(f"    c{pc['chunk']}: WER {100 * (pc['wer'] or 0):5.1f}  "
                  f"WERf {100 * (pc['wer_fold'] or 0):5.1f}  finals {pc['finals']:3}  "
                  f"translated {pc['translated']:3}/{pc['non_en_finals']:3}"
                  + (f"  ERROR {pc['error']}" if pc["error"] else ""))
    if a.out:
        json.dump(results, open(a.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
