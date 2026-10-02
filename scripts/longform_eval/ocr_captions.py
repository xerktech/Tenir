"""OCR burned-in (yellow) captions out of a video into timed reference cues.

Two steps over one decode at FPS frames/s of the bottom caption band:
1. Run detection: a caption is a run of frames whose "core" yellow mask stays
   (nearly) the same; a blank band or a big mask change starts a new run.
2. OCR one frame from the middle of each run with tesseract (spa).

The OCR mask is the *loose* yellow mask (keeps thin accents and the tilde, which
the strict mask drops) intersected with a dilation of the strict one (drops
yellowish background objects far from the text), then cropped to the densest
horizontal span of ink.

Tuned for 854x480 video with yellow text in rows 330-480; other layouts need
--y0/--height. Needs ffmpeg and `tesseract` with the `spa` traineddata.

Usage: python ocr_captions.py video.mp4 --out es_caps.json [--ffmpeg PATH] [--lang spa]
Output: [{"start_ms", "end_ms", "text"}]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter

FPS = 5


def core_mask(a: np.ndarray) -> np.ndarray:
    r, g, b = (a[..., i].astype(int) for i in range(3))
    return (r > 170) & (g > 170) & (b < 110)


def ocr_mask(a: np.ndarray) -> np.ndarray | None:
    r, g, b = (a[..., i].astype(int) for i in range(3))
    loose = (r > 130) & (g > 130) & (r - b > 50) & (g - b > 50)
    core = Image.fromarray((core_mask(a) * 255).astype(np.uint8))
    m = loose & (np.array(core.filter(ImageFilter.MaxFilter(9))) > 0)
    cols = np.where(m.sum(0) > 0)[0]
    if len(cols) == 0:
        return None
    spans = [[cols[0], cols[0]]]
    for c in cols[1:]:
        if c - spans[-1][1] < 30:
            spans[-1][1] = c
        else:
            spans.append([c, c])
    lo, hi = max(spans, key=lambda s: m[:, s[0]:s[1] + 1].sum())
    m[:, :lo] = False
    m[:, hi + 1:] = False
    return m


def ocr(m: np.ndarray, lang: str) -> str:
    ys, xs = np.where(m)
    img = Image.fromarray(np.where(m, 0, 255).astype(np.uint8))
    img = img.crop((max(0, xs.min() - 12), max(0, ys.min() - 12), xs.max() + 12, ys.max() + 12))
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        img.save(f.name)
        out = subprocess.run(["tesseract", f.name, "-", "-l", lang, "--psm", "6"],
                             capture_output=True, check=True).stdout.decode("utf-8", "replace")
    return " ".join(out.split())


def runs(frames, min_px: int = 150):
    """Yield (start_idx, end_idx, middle_frame) per stable caption run."""
    cur = prev = None
    i = -1
    for i, fr in enumerate(frames):
        m = core_mask(fr)
        n = int(m.sum())
        if n < min_px:
            m = None
        if m is None:
            if cur:
                yield cur
            cur = None
        else:
            same = prev is not None and (m ^ prev).sum() < 0.25 * max(n, prev.sum())
            if cur and same:
                cur[1] = i
                cur[3].append(fr)
            else:
                if cur:
                    yield cur
                cur = [i, i, None, [fr]]
        prev = m
        if cur and len(cur[3]) > 12:  # bound memory: keep a window around the start
            cur[3] = cur[3][:12]
    if cur:
        yield cur


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--lang", default="spa")
    ap.add_argument("--width", type=int, default=854)
    ap.add_argument("--y0", type=int, default=330)
    ap.add_argument("--height", type=int, default=150)
    a = ap.parse_args()
    w, h = a.width, a.height
    p = subprocess.Popen([a.ffmpeg, "-v", "error", "-i", a.video, "-vf",
                          f"fps={FPS},crop={w}:{h}:0:{a.y0}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

    def frames():
        while len(buf := p.stdout.read(w * h * 3)) == w * h * 3:
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)

    caps = []
    for start, end, _, kept in runs(frames()):
        if end - start < 1:  # one-frame flicker (<0.4 s)
            continue
        m = ocr_mask(kept[len(kept) // 2].copy())
        text = ocr(m, a.lang) if m is not None and m.sum() > 100 else ""
        if text:
            caps.append({"start_ms": start * 1000 // FPS, "end_ms": (end + 1) * 1000 // FPS,
                         "text": text})
    json.dump(caps, open(a.out, "w"), ensure_ascii=False, indent=0)
    print(f"{len(caps)} captions")


if __name__ == "__main__":
    main()
