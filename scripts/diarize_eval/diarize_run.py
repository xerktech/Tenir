"""Caption a session through the api's own streaming transcriber (XERK-2958).

Feeds a WAV through ``api.stt.streaming.StreamingTranscriber`` exactly as a live mic
session would (same turn cutting, same final decode), with or without the diarizer,
and writes the caption.finals it produced:

    python diarize_run.py session.wav out.json \
        --stt http://tenir-stt.ai.svc.cluster.local:8000/v1 --stt-model whisper-large-v3-turbo \
        --diarizer http://127.0.0.1:8790 --diarizer-timeout 120

Leave ``--diarizer`` off for the baseline. Imports the installed ``api`` package
(``pip install -e api``). A CPU diarizer runs slower than real time; raise the timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import soundfile as sf

from api.contract import CaptionFinal
from api.stt.diarize import Diarizer
from api.stt.parakeet import ParakeetEngine
from api.stt.streaming import StreamingTranscriber


async def run(args: argparse.Namespace) -> list[dict]:
    pcm, sr = sf.read(args.wav, dtype="int16")
    assert sr == 16_000 and pcm.ndim == 1, "16 kHz mono expected"
    data = pcm.tobytes()
    diarizer = (
        Diarizer(args.diarizer, f"eval-{uuid.uuid4()}", timeout=args.diarizer_timeout)
        if args.diarizer
        else None
    )
    t = StreamingTranscriber(
        ParakeetEngine(endpoint=args.stt, model=args.stt_model, api_key=""),
        partial_interval_ms=10**9,  # finals only: partials don't change what is stored
        local_agreement=False,
        diarizer=diarizer,
    )
    finals: list[dict] = []

    async def drain() -> None:
        async for m in t.results():
            if isinstance(m, CaptionFinal):
                finals.append(m.model_dump(mode="json", exclude_none=True))

    pump = asyncio.create_task(drain())
    step = 3200  # 100 ms frames, like the clients
    t0 = time.perf_counter()
    for i in range(0, len(data), step):
        await t.push(data[i : i + step])
        if i % (step * 50) == 0:
            await asyncio.sleep(0)
    await t.flush()
    await t.close()
    await pump
    print(f"{len(finals)} finals in {time.perf_counter() - t0:.0f}s")
    return finals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--stt", required=True, help="OpenAI-compatible /v1 base of the STT model")
    ap.add_argument("--stt-model", default="whisper-large-v3-turbo")
    ap.add_argument("--diarizer", default="", help="tenir-diarizer base URL (omit: baseline)")
    ap.add_argument("--diarizer-timeout", type=float, default=3.0)
    args = ap.parse_args()
    finals = asyncio.run(run(args))
    args.out.write_text(json.dumps(finals, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
