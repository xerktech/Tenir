"""Frame-level speaker accuracy of 4-slot vs fingerprint tracking (XERK-3017).

Runs streaming Sortformer (the shipped 10 s setup) over each session, cuts it into 6 s turns
with the shipped planner, embeds each solo piece with TitaNet, then scores
``diarizer/fingerprint.Fingerprints`` against the session's truth: the share of single-speaker
frames whose label maps (one label to one true speaker, Hungarian) to the right person.
No STT, so a sweep over thresholds takes seconds once the model pass is cached.

    python diarize_fp_tune.py out/ s1 s2 m6 m8 ...          # sweep, best settings first
    python diarize_fp_tune.py out/ h4 h6 --match 0.55 --new 0.30 --min 2.0   # one setting

``out/`` holds ``<session>.wav/.json`` from build_session.py and the model-pass cache
(``fp_cache.pkl``). Needs torch + NeMo (the diarizer's requirements) and scipy.
"""

from __future__ import annotations

import argparse
import itertools
import json
import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "diarizer"))

import fingerprint
import planner

TURN_FRAMES = 75  # 6 s turns, like capped api turns


def model_pass(root: Path, names: list[str]) -> dict:
    cache_path = root / "fp_cache.pkl"
    cache = pickle.loads(cache_path.read_bytes()) if cache_path.exists() else {}
    need = [n for n in names if n not in cache]
    if not need:
        return cache
    import soundfile as sf
    import torch
    from nemo.collections.asr.models import EncDecSpeakerLabelModel, SortformerEncLabelModel

    m = SortformerEncLabelModel.from_pretrained("nvidia/diar_streaming_sortformer_4spk-v2")
    m.eval()
    sm = m.sortformer_modules
    sm.chunk_len, sm.chunk_right_context, sm.fifo_len = 124, 1, 124
    sm.spkcache_update_period, sm.spkcache_len = 124, 188
    emb = EncDecSpeakerLabelModel.from_pretrained("nvidia/speakerverification_en_titanet_large")
    emb.eval()
    for n in need:
        wav_path = root / f"{n}.wav"
        wav, _ = sf.read(wav_path, dtype="float32")
        _, probs = m.diarize(
            audio=[str(wav_path)], batch_size=1, include_tensor_outputs=True, verbose=False
        )
        p_all = probs[0].squeeze(0).numpy()
        turns = []
        for f0 in range(0, len(p_all), TURN_FRAMES):
            pr = p_all[f0 : f0 + TURN_FRAMES]
            items = []
            for p in planner.plan_pieces(pr, len(pr) * planner.FRAME_S):
                e = None
                if not p.separate and p.end_s - p.start_s >= 1.0:
                    a = int((f0 * planner.FRAME_S + p.start_s) * 16000)
                    b = int((f0 * planner.FRAME_S + p.end_s) * 16000)
                    x = wav[a:b]
                    if len(x) > 8 * 16000:
                        mid = (len(x) - 8 * 16000) // 2
                        x = x[mid : mid + 8 * 16000]
                    with torch.inference_mode():
                        _, ee = emb.forward(
                            input_signal=torch.from_numpy(x)[None],
                            input_signal_length=torch.tensor([len(x)]),
                        )
                    e = ee[0].numpy()
                items.append((p, e))
            turns.append((f0, items))
        cache[n] = (p_all.shape[0], turns)
        cache_path.write_bytes(pickle.dumps(cache))
    return cache


def score(
    root: Path, entry: tuple, name: str, fp_on: bool, match=0.55, new=0.30, min_s=2.0
) -> tuple[float, int, int]:
    """(accuracy, labels used, true speakers)."""
    from scipy.optimize import linear_sum_assignment

    nfr, turns = entry
    truth = json.loads((root / f"{name}.json").read_text())
    g = np.zeros((nfr, 32), bool)
    for u in truth["utterances"]:
        g[int(u["start"] / 0.08) : int(u["end"] / 0.08), u["speaker"] - 1] = True
    lab = np.zeros(nfr, int)
    fingerprint.MATCH_COS, fingerprint.NEW_COS = match, new
    fp = fingerprint.Fingerprints()
    for f0, items in turns:
        for p, e in items:
            if p.separate:
                continue
            spk = p.speaker
            if fp_on:
                spk = fp.person(p.speaker, e if e is not None and p.end_s - p.start_s >= min_s else None)
            lab[f0 + int(p.start_s / 0.08) : f0 + int(p.end_s / 0.08)] = spk
    single = g.sum(1) == 1
    gt, lb = g[single].argmax(1), lab[single]
    labs, gts = sorted(set(lb) - {0}), sorted(set(gt))
    c = np.array([[np.sum((lb == x) & (gt == y)) for y in gts] for x in labs])
    r, k = linear_sum_assignment(-c)
    return float(c[r, k].sum() / max(1, len(gt))), len(labs), len(gts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--match", type=float)
    ap.add_argument("--new", type=float)
    ap.add_argument("--min", type=float, dest="min_s")
    a = ap.parse_args()
    cache = model_pass(a.root, a.sessions)
    base = {n: score(a.root, cache[n], n, False) for n in a.sessions}
    if a.match is not None:
        for n in a.sessions:
            f = score(a.root, cache[n], n, True, a.match, a.new, a.min_s)
            print(f"{n}: 4-slot {base[n][0]:.3f} -> fingerprints {f[0]:.3f} "
                  f"({f[1]} labels, {f[2]} true)")
        return
    rows = []
    grid = itertools.product(
        (0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65), (0.05, 0.1, 0.15, 0.2, 0.25, 0.3), (1.0, 1.5, 2.0, 2.5)
    )
    for match, new, min_s in grid:
        if new >= match:
            continue
        r = {n: score(a.root, cache[n], n, True, match, new, min_s) for n in a.sessions}
        worst = min(r[n][0] - base[n][0] for n in a.sessions)
        mean = sum(r[n][0] for n in a.sessions) / len(a.sessions)
        rows.append((worst >= -0.005, mean, match, new, min_s, r))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    print("4-slot:", {n: round(b[0], 3) for n, b in base.items()})
    for ok, mean, match, new, min_s, r in rows[:5]:
        print(f"match {match} new {new} min {min_s}: mean {mean:.3f} no-regression {ok}",
              {n: round(v[0], 3) for n, v in r.items()})


if __name__ == "__main__":
    main()
