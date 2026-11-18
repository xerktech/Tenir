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
``--separator MossFormer2_SS_16K`` (or any ClearerVoice speech-separation model name) runs
that model through ClearerVoice instead (``pip install clearvoice``; weights download to
``<--checkpoints>/checkpoints/<model>``). Either model runs on a GPU when torch sees one;
MossFormer2 on CPU ran 8x (idle host) to 70x (loaded) slower than real time.
"""

from __future__ import annotations

import argparse
import json
import os
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
from api.stt.parakeet import ParakeetEngine

SR = 16_000


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)


def _scaled(x: np.ndarray) -> np.ndarray:
    """diarizer/server.py ``_pcm16``: separator output has no meaningful scale."""
    x = np.nan_to_num(x)  # a degenerate clip can come back NaN from MossFormer2
    peak = float(np.abs(x).max()) if len(x) else 0.0
    x = x * (0.5 / peak) if peak > 0 else x
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).astype(np.float32) / 32768


def load_separator(name: str, checkpoints: Path) -> Callable[[np.ndarray], np.ndarray]:
    """``name`` → a function from a mono 16 kHz float clip to its two streams, shape (2, T).

    A Hugging Face id with a slash is a speechbrain SepFormer (what the diarizer serves);
    a bare name is a ClearerVoice separation model such as ``MossFormer2_SS_16K``.
    """
    import torch

    if "/" in name:
        from speechbrain.inference.separation import SepformerSeparation

        sep = SepformerSeparation.from_hparams(
            source=name,
            savedir=os.path.join(os.environ.get("HF_HOME", "/tmp"), "speechbrain", name),
            run_opts={"device": "cuda" if torch.cuda.is_available() else "cpu"},
        )

        def sepformer(x: np.ndarray) -> np.ndarray:
            with torch.inference_mode():
                return sep.separate_batch(torch.from_numpy(x)[None])[0].T.cpu().numpy()

        return sepformer

    from clearvoice import ClearVoice
    from clearvoice.utils.decode import decode_one_audio

    # ClearerVoice resolves its checkpoint dir relative to the working directory.
    checkpoints.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(checkpoints)
    try:
        model = ClearVoice(task="speech_separation", model_names=[name]).models[0]
    finally:
        os.chdir(cwd)

    def clearvoice(x: np.ndarray) -> np.ndarray:
        with torch.inference_mode():
            streams = decode_one_audio(model.model, model.device, x[None], model.args)
        return np.stack([s[: len(x)] for s in streams]).astype(np.float32)

    return clearvoice


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
    ap.add_argument("mixes", type=Path, nargs="+", help="dirs with session.wav + truth.json")
    ap.add_argument("--stt", required=True)
    ap.add_argument("--stt-model", default="whisper-large-v3-turbo")
    ap.add_argument("--separator", default="speechbrain/sepformer-whamr16k")
    ap.add_argument(
        "--checkpoints",
        type=Path,
        default=Path(os.environ.get("HF_HOME", "/tmp")) / "clearvoice",
        help="ClearerVoice models download under <this>/checkpoints/<model>",
    )
    ap.add_argument(
        "--out-name",
        default="pieces.jsonl",
        help="output file in each mix dir, so separators can be compared side by side",
    )
    args = ap.parse_args()

    separate = load_separator(args.separator, args.checkpoints)
    engine = ParakeetEngine(endpoint=args.stt, model=args.stt_model, timeout=60.0)
    for d in args.mixes:
        audio = _read(d / "session.wav") / 32768
        truth = json.loads((d / "truth.json").read_text())
        rows = []
        for m in truth["mixes"]:
            x = audio[m["start_ms"] * SR // 1000 : m["end_ms"] * SR // 1000]
            streams = separate(x)
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
                        "turn": mix,  # the mixture is the whole turn
                        "sep": _decode(engine, _scaled(s)),
                    }
                )
        with open(d / args.out_name, "w") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        print(f"{d}: {len(truth['mixes'])} mixtures")


if __name__ == "__main__":
    main()
