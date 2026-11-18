"""Separate each ``diarize_gate.py mixes`` pair with the diarizer's separator and decode both
streams (XERK-3069).

Real-voice mixtures made from short dinner clips rarely reach the diarizer's 1.5 s overlap
floor, and Sortformer on a one-minute session seldom tells the two new voices apart, so a
full ``diarize_pieces.py`` run separates almost none of them. The gate only ever judges
SepFormer's output, though, so this runs that separator (same model, same 0.5-peak
scaling as ``diarizer/server.py``) on each whole mixture, decodes both streams and the
mixture with the shipped STT engine, and writes the result as ``pieces.jsonl`` that
``diarize_gate.py replay --truth`` scores: each mixture is one turn with two overlapped
pieces.

    python diarize_sepmix.py out/mixes/s1/0 --stt $STT --stt-model whisper-large-v3-turbo

Needs the diarizer's requirements (speechbrain, torch) and the api package in one env.
"""

from __future__ import annotations

import argparse
import json
import os
import wave
from pathlib import Path

import numpy as np
from api.stt.parakeet import ParakeetEngine

SR = 16_000


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(
            np.float32
        )


def _scaled(x: np.ndarray) -> np.ndarray:
    """diarizer/server.py ``_pcm16``: separator output has no meaningful scale."""
    peak = float(np.abs(x).max()) if len(x) else 0.0
    x = x * (0.5 / peak) if peak > 0 else x
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).astype(np.float32) / 32768


def _decode(engine: ParakeetEngine, x: np.ndarray) -> dict:
    try:
        r = engine.transcribe(x, language=None, want_words=False)
    except Exception as e:  # noqa: BLE001 - a lost decode is recorded, not fatal
        return {"error": repr(e)}
    return {
        "text": r.text.strip(),
        "lang": r.language,
        "avg_logprob": r.avg_logprob,
        "non_speech": r.non_speech,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "mixes", type=Path, nargs="+", help="dirs with session.wav + truth.json"
    )
    ap.add_argument("--stt", required=True)
    ap.add_argument("--stt-model", default="whisper-large-v3-turbo")
    ap.add_argument("--separator", default="speechbrain/sepformer-whamr16k")
    args = ap.parse_args()

    import torch
    from speechbrain.inference.separation import SepformerSeparation

    sep = SepformerSeparation.from_hparams(
        source=args.separator,
        savedir=os.path.join(
            os.environ.get("HF_HOME", "/tmp"), "speechbrain", args.separator
        ),
    )
    engine = ParakeetEngine(endpoint=args.stt, model=args.stt_model, timeout=60.0)
    for d in args.mixes:
        audio = _read(d / "session.wav") / 32768
        truth = json.loads((d / "truth.json").read_text())
        rows = []
        for m in truth["mixes"]:
            x = audio[m["start_ms"] * SR // 1000 : m["end_ms"] * SR // 1000]
            with torch.inference_mode():
                streams = sep.separate_batch(torch.from_numpy(x)[None])[0].T.numpy()
            mix = _decode(engine, x)
            for k, s in enumerate(streams):
                rows.append(
                    {
                        "n": len(rows),
                        "turn_start_ms": m["start_ms"],
                        "start_ms": m["start_ms"],
                        "end_ms": m["end_ms"],
                        "speaker": k + 1,
                        "overlap": True,
                        "mix": mix,
                        "sep": _decode(engine, _scaled(s)),
                    }
                )
        with open(d / "pieces.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{d}: {len(truth['mixes'])} mixtures")


if __name__ == "__main__":
    main()
