"""Transcribe a recorded run's final turns with any OpenAI-compatible STT endpoint.

Model comparison with segmentation held fixed: every candidate decodes exactly the
audio spans (startMs..endMs) of the finals in a reference run (normally a live
ws_driver run of the production stack), one request at a time like production's
per-session decode. The output is a ws_driver-shaped run — each final's text
replaced by the candidate's, ``lang`` re-detected with the installed api's
``detect_lang`` (Parakeet reports none; this keeps every candidate on the same
langid) and ``stt_ms`` = the request's wall time — so replay_trigger.py and
score.py take it unchanged. A summary of latency percentiles prints at the end.

Usage: python turn_stt.py run.json --audio-dir chunks/ --endpoint http://host:8000/v1
       --model NAME --out cand.json [--language es] [--no-timestamps] [--workers 1]
       chunks/c<N>.wav must be the 16 kHz mono chunks the run streamed.
"""

from __future__ import annotations

import argparse
import io
import json
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx


def span_wav(path: str, start_ms: int, end_ms: int) -> bytes:
    with wave.open(path) as w:
        w.setpos(min(w.getnframes(), start_ms * 16))
        pcm = w.readframes(max(0, end_ms - start_ms) * 16)
    b = io.BytesIO()
    with wave.open(b, "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(16000)
        o.writeframes(pcm)
    return b.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--audio-dir", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--language", help="pin the source language (else the model detects)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--no-timestamps", action="store_true",
                    help="send timestamps=false (Tenir's Parakeet-server extension; production "
                         "finals run with word timing off — without it latency is ~5x)")
    a = ap.parse_args()
    from api.stt.langid import detect_lang

    run = json.load(open(a.run))
    url = a.endpoint.rstrip("/") + "/audio/transcriptions"
    client = httpx.Client(timeout=120)
    jobs = [(c["id"], f) for c in run for f in c.get("finals", [])]

    def one(job) -> None:
        cid, f = job
        data = {"model": a.model, "response_format": "json"}
        if a.language:
            data["language"] = a.language
        if a.no_timestamps:
            data["timestamps"] = "false"
        audio = span_wav(f"{a.audio_dir}/{cid}.wav", f["startMs"], f["endMs"])
        t = time.perf_counter()
        for attempt in range(3):
            try:
                r = client.post(url, files={"file": ("turn.wav", audio, "audio/wav")}, data=data)
                r.raise_for_status()
                body = r.json()
                text = body.get("text") if isinstance(body, dict) else None
                if text is not None and not isinstance(text, str):
                    raise ValueError(f"non-string text in response: {type(text).__name__}")
                if not isinstance(body, dict):
                    raise ValueError(f"response is not a JSON object: {type(body).__name__}")
                text = (text or "").strip()
                break
            except (httpx.HTTPError, ValueError) as exc:  # ValueError: a non-JSON 200
                if attempt == 2:
                    f["stt_error"] = f"{type(exc).__name__}: {exc}"[:200]
                    text = ""
        f["stt_ms"] = round((time.perf_counter() - t) * 1000)
        f["text"], f["lang"] = text, detect_lang(text)
        f.pop("translation", None)
        f.pop("latency_ms", None)

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, jobs))
    json.dump(run, open(a.out, "w"), ensure_ascii=False, indent=1)
    if not jobs:
        raise SystemExit("no finals in the run — nothing to transcribe")
    ms = sorted(f["stt_ms"] for _, f in jobs)
    audio_s = sum(f["endMs"] - f["startMs"] for _, f in jobs) / 1000
    errors = sum(1 for _, f in jobs if f.get("stt_error"))
    print(f"{a.model}: {len(jobs)} turns, {errors} errors, stt ms p50={ms[len(ms) // 2]} "
          f"p90={ms[int(0.9 * len(ms))]} max={ms[-1]}, RTFx={audio_s / (sum(ms) / 1000):.1f}")
    if jobs and errors == len(jobs):
        raise SystemExit("every turn failed — check the endpoint/model (output written anyway)")


if __name__ == "__main__":
    main()
