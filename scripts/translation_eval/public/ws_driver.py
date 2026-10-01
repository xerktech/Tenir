"""Drive FLEURS clips through a live Tenir API over its WebSocket.

One WS session per clip, PCM streamed in 100 ms frames at real-time pace (the
app's turn segmentation and partial cadence are wall-clock), then trailing
silence so the turn closes. Records every caption.final and translation with a
receive timestamp; translation latency = translation recv - its final's recv,
i.e. what the glasses would see.

Usage: python ws_driver.py clips.json --out run.json [--base http://127.0.0.1:8080]
       [--concurrency 4]
clips.json: [{"id":..., "wav": path}]
"""

import argparse
import asyncio
import json
import os
import time
import wave

import httpx
import websockets


def pcm(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


async def run_clip(base, token, clip, wait=True):
    ws_url = base.replace("http://", "ws://") + f"/ws?token={token}"
    audio = pcm(clip["wav"]) + b"\x00\x00" * 16000 * 2  # 2 s trailing silence
    frame = 3200  # 100 ms @ 16 kHz s16le
    finals, trans, done = [], [], None
    async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        ready = json.loads(await ws.recv())
        assert ready.get("type") == "session.ready", ready

        async def reader():
            nonlocal done
            try:
                await _read(ws)
            except websockets.ConnectionClosed:
                pass

        async def _read(ws):
            nonlocal done
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                t = time.monotonic()
                if m["type"] == "caption.final":
                    finals.append({"segmentId": m["segmentId"], "text": m["text"],
                                   "lang": m.get("lang"), "t": t})
                elif m["type"] == "translation":
                    trans.append({"segmentId": m["segmentId"], "text": m["text"], "t": t})
                elif m["type"] == "translation.done":
                    done = t

        rt = asyncio.create_task(reader())
        start = time.monotonic()
        for i in range(0, len(audio), frame):
            await ws.send(audio[i:i + frame])
            # real-time pacing against the wall clock, not cumulative sleeps
            await asyncio.sleep(max(0, start + (i + frame) / 32000 - time.monotonic()))
        # wait for every final to have its translation (or the run to close)
        deadline = time.monotonic() + (20 if wait else 30)
        while time.monotonic() < deadline:
            if not wait:
                # STT-only capture: done once no final has arrived for 6 s
                last = max([f["t"] for f in finals], default=start + len(audio) / 32000)
                if time.monotonic() - max(last, start + len(audio) / 32000) > 6:
                    break
                await asyncio.sleep(0.1)
                continue
            fin_ids = {f["segmentId"] for f in finals if f["lang"] != "en"}
            if finals and fin_ids <= {t["segmentId"] for t in trans}:
                break
            await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "session.end"}))
        await asyncio.sleep(0.3)
        rt.cancel()
    tmap = {t["segmentId"]: t for t in trans}
    for f in finals:
        t = tmap.get(f["segmentId"])
        f["translation"] = t["text"] if t else None
        f["latency_ms"] = round((t["t"] - f["t"]) * 1000) if t else None
        del f["t"]
    return {"id": clip["id"], "finals": finals}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--no-wait", action="store_true",
                    help="STT-only capture: don't wait for translations")
    a = ap.parse_args()
    clips = json.load(open(a.clips))
    r = httpx.post(f"{a.base}/auth/login", json={"username": os.environ["TENIR_USERNAME"],
                                                  "password": os.environ["TENIR_PASSWORD"]})
    r.raise_for_status()
    token = r.json()["token"]
    sem = asyncio.Semaphore(a.concurrency)
    results, n = [], [0]

    async def one(c):
        async with sem:
            try:
                res = await run_clip(a.base, token, c, not a.no_wait)
            except Exception as e:  # noqa: BLE001 - record and move on
                res = {"id": c["id"], "error": f"{type(e).__name__}: {e}"[:200], "finals": []}
            results.append(res)
            n[0] += 1
            if n[0] % 25 == 0:
                print(f"  {n[0]}/{len(clips)}", flush=True)

    await asyncio.gather(*(one(c) for c in clips))
    json.dump(results, open(a.out, "w"), ensure_ascii=False, indent=1)
    lat = sorted(f["latency_ms"] for r in results for f in r["finals"] if f["latency_ms"] is not None)
    if lat:
        print(f"finals={sum(len(r['finals']) for r in results)} translated={len(lat)} "
              f"lat mean={sum(lat)/len(lat):.0f} p50={lat[len(lat)//2]} p90={lat[int(.9*len(lat))]}")


asyncio.run(main())
