"""Run chunks through the real StreamingTranscriber + deployed Parakeet, faster than real time.

Turn boundaries are a function of audio bytes (VAD + max-segment cap), not wall time,
so pushing 100 ms frames as fast as the decodes allow yields the same finals the live
app produces — minus the WS/session layer and real-time pacing (QA: byte-identical to a
live run on the chunks compared, at production's 350 ms partial cadence, which this
script defaults to). An STT config variant (``API_STT_*`` env, read by the
installed ``api`` package's settings) then costs ~8 min per chunk instead of a 10-minute
real-time stream, and chunks run in parallel.

``--no-partials`` skips partial decodes (~7x faster) but is NOT production-faithful:
partials feed the empty-final recovery (XERK-174), so turns whose whole-turn decode is
blank are dropped instead of recovered (26/867 turns on the Oct 2026 hour). Compare
no-partials runs only with each other, and know that a knob changing how often finals
blank (max-segment length) also changes how much that mode loses.

Output is a ws_driver-shaped run (no translations): feed it to replay_trigger.py, then
score.py.

Usage: API_STT_ENDPOINT=http://tenir-stt.ai.svc.cluster.local:8000/v1 [API_STT_MAX_SEGMENT_MS=12000 ...]
       python offline_stt.py clips.json --out stt.json [--no-partials] [--workers 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import wave

FRAME = 3200  # 100 ms @ 16 kHz s16le, the app's client frame


async def run_clip(clip: dict) -> dict:
    from api.contract import CaptionFinal
    from api.stt import make_transcriber

    t = make_transcriber()
    # The shared STT server stalls past the engine's 15 s timeout now and then when a
    # sweep hammers it; live, the session logs the failed frame and carries on. Retry
    # here instead so one stall can't sink a 10-minute chunk.
    engine = t._engine
    decode = engine.transcribe

    def transcribe_with_retry(*args, **kwargs):
        for attempt in range(3):
            try:
                return decode(*args, **kwargs)
            except Exception:  # noqa: BLE001 - transport errors from httpx
                if attempt == 2:
                    raise

    engine.transcribe = transcribe_with_retry
    with wave.open(clip["wav"]) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes()) + b"\x00\x00" * 16000 * 2  # 2 s trailing silence
    finals: list[dict] = []

    async def drain() -> None:
        async for r in t.results():
            if isinstance(r, CaptionFinal):
                finals.append({"segmentId": r.segmentId, "text": r.text,
                               "lang": r.lang.value if r.lang else None,
                               "startMs": r.startMs, "endMs": r.endMs})

    d = asyncio.create_task(drain())
    for i in range(0, len(pcm), FRAME):
        await t.push(pcm[i:i + FRAME])
    await t.flush()
    await t.close()
    await d
    return {"id": clip["id"], "finals": finals}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-partials", action="store_true",
                    help="skip partial decodes: much faster, but drops recoverable turns")
    ap.add_argument("--workers", type=int, default=3,
                    help="parallel chunks; the shared STT server stalls past ~3-4")
    a = ap.parse_args()
    if a.no_partials:
        os.environ["API_STT_PARTIAL_INTERVAL_MS"] = str(10 ** 9)
    else:
        # Production's cadence (docker-compose / ArgoCD set 350); the Settings default
        # is 700, which changes which turns the XERK-174 fallback can recover.
        os.environ.setdefault("API_STT_PARTIAL_INTERVAL_MS", "350")
    # First import of the api package, so its settings read the env set above.
    from api.config import settings

    sem = asyncio.Semaphore(a.workers)

    async def one(c: dict) -> dict:
        async with sem:
            r = await run_clip(c)
            print(f"{c['id']}: {len(r['finals'])} finals", flush=True)
            return r

    clips = json.load(open(a.clips))
    out = await asyncio.gather(*(one(c) for c in clips))
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"stt: max_segment={settings.stt_max_segment_ms} silence={settings.stt_silence_ms} "
          f"partial_interval={settings.stt_partial_interval_ms}")


if __name__ == "__main__":
    asyncio.run(main())
