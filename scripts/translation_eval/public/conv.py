"""FLEURS ships IEEE-float WAVs; Tenir wants 16 kHz s16le mono. Minimal RIFF parse."""
import json, os, struct, wave
import numpy as np

def read_float_wav(p):
    b = open(p, "rb").read()
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE"
    i, fmt, data = 12, None, None
    while i < len(b):
        cid, size = b[i:i+4], struct.unpack("<I", b[i+4:i+8])[0]
        body = b[i+8:i+8+size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        i += 8 + size + (size & 1)
    tag, ch, sr, _, _, bits = fmt
    assert tag == 3 and bits == 32, fmt
    x = np.frombuffer(data, "<f4").reshape(-1, ch).mean(1)
    return x, sr

clips = json.load(open("/work/data/clips.json"))
os.makedirs("/work/data/pcm16", exist_ok=True)
tot = 0
for c in clips:
    x, sr = read_float_wav(c["wav"])
    assert sr == 16000, sr
    out = f"/work/data/pcm16/{c['id']}.wav"
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    c["wav"] = out
    tot += len(x) / 16000
json.dump(clips, open("/work/data/clips.json", "w"), indent=1)
print("converted", len(clips), "audio sec", round(tot))
