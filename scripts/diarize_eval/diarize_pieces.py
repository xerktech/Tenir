"""Record every speaker piece of a diarized run once, for offline gate replay (XERK-3069).

Drives the api's ``StreamingTranscriber`` over a WAV with the diarizer on, exactly as
``diarize_run.py`` does, but instead of captioning a multi-speaker turn it records, per
piece: its span, label, whether it is overlapped, and the decode of every audio the gate
could choose between — the separated stream (when the diarizer sent one) and the turn's own
samples for the same span ("mix"). Each decode keeps text, detected language and Whisper's
``avg_logprob``. Pieces' audio is written next to the JSONL, so a replay can build
references from solo pieces without another diarizer pass.

    python diarize_pieces.py dinner.wav out/dinner --stt $STT --stt-model parakeet \
        --diarizer http://127.0.0.1:8790 --diarizer-timeout 300

Inside the api pod the diarizer and LiteLLM keys come from its own env
(``API_DIARIZER_KEY``, ``API_LITELLM_API_KEY``), so no secret leaves it.

Output: ``out/dinner/pieces.jsonl`` and ``out/dinner/<n>.{sep,mix}.wav``. Recordings stay
outside the repo (``.claude/rules/diarizer.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

import soundfile as sf
import numpy as np
from api.stt import streaming
from api.stt.diarize import Diarizer, Piece
from api.stt.parakeet import ParakeetEngine
from api.stt.streaming import StreamingTranscriber

SR = 16_000


def _ms_to_bytes(ms: int) -> int:
    return ms * SR // 1000 * 2


class Recorder(StreamingTranscriber):
    """Records each piece's decodes and captions nothing for multi-speaker turns."""

    def __init__(self, out: Path, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.out = out
        self.rows: list[dict] = []

    async def _decode(self, pcm: bytes, name: str) -> dict:
        sf.write(self.out / name, np.frombuffer(pcm, dtype=np.int16), SR)
        try:
            r = await self._decode_final(pcm)
        except Exception as e:  # noqa: BLE001 - a lost decode is recorded, not fatal
            return {"wav": name, "error": repr(e)}
        return {
            "wav": name,
            "text": r.text.strip(),
            "lang": r.language,
            "avg_logprob": r.avg_logprob,
            "non_speech": r.non_speech,
        }

    async def _finalize_pieces(
        self, pcm: bytes, start: int, pieces: list[Piece]
    ) -> bool:
        for piece in pieces:
            mix = pcm[_ms_to_bytes(piece.start_ms) : _ms_to_bytes(piece.end_ms)]
            if len(mix) < _ms_to_bytes(streaming._MIN_PIECE_MS):
                continue
            n = len(self.rows)
            row = {
                "n": n,
                "turn_start_ms": start,
                "start_ms": start + piece.start_ms,
                "end_ms": start + piece.end_ms,
                "speaker": piece.speaker,
                "overlap": piece.overlap,
                "mix": await self._decode(mix, f"{n}.mix.wav"),
            }
            if piece.audio is not None:
                row["sep"] = await self._decode(piece.audio, f"{n}.sep.wav")
            self.rows.append(row)
        return True  # nothing captioned: the replay decides what would have been

    async def _finalize_turn(
        self,
        pcm: bytes,
        start: int,
        has_speech: bool,
        fallback: str,
        speaker: str | None,
    ) -> None:
        # A one-speaker turn: recorded as a solo piece (the clean references for
        # ``diarize_gate.py mixes``); a turn the diarizer did not label is skipped.
        if not has_speech or speaker is None:
            return
        n = len(self.rows)
        self.rows.append(
            {
                "n": n,
                "turn_start_ms": start,
                "start_ms": start,
                "end_ms": start + len(pcm) * 1000 // (SR * 2),
                "speaker": int(speaker.rsplit(" ", 1)[-1]),
                "overlap": False,
                "mix": await self._decode(pcm, f"{n}.mix.wav"),
            }
        )


async def run(args: argparse.Namespace) -> list[dict]:
    pcm, sr = sf.read(args.wav, dtype="int16")
    assert sr == SR and pcm.ndim == 1, "16 kHz mono expected"
    args.out.mkdir(parents=True, exist_ok=True)
    t = Recorder(
        args.out,
        ParakeetEngine(endpoint=args.stt, model=args.stt_model, api_key=args.stt_key),
        partial_interval_ms=10**9,
        local_agreement=False,
        diarizer=Diarizer(
            args.diarizer,
            f"eval-{uuid.uuid4()}",
            timeout=args.diarizer_timeout,
            api_key=os.environ.get("DIAR_API_KEY")
            or os.environ.get("API_DIARIZER_KEY", ""),
        ),
    )

    async def drain() -> None:
        async for _ in t.results():
            pass

    pump = asyncio.create_task(drain())
    data, step = pcm.tobytes(), 3200
    for i in range(0, len(data), step):
        await t.push(data[i : i + step])
        if i % (step * 50) == 0:
            await asyncio.sleep(0)
    await t.flush()
    await t.close()
    await pump
    return t.rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("out", type=Path, help="directory for pieces.jsonl and piece audio")
    ap.add_argument(
        "--stt", required=True, help="OpenAI-compatible /v1 base of the STT model"
    )
    ap.add_argument("--stt-model", default="whisper-large-v3-turbo")
    ap.add_argument(
        "--stt-key",
        default=os.environ.get("STT_API_KEY")
        or os.environ.get("API_LITELLM_API_KEY", ""),
    )
    ap.add_argument("--diarizer", required=True, help="tenir-diarizer base URL")
    ap.add_argument("--diarizer-timeout", type=float, default=300.0)
    args = ap.parse_args()
    rows = asyncio.run(run(args))
    with open(args.out / "pieces.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} pieces, {sum('sep' in r for r in rows)} separated")


if __name__ == "__main__":
    main()
