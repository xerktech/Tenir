"""Smoke test against a running tenir-ocr: render a caption crop, OCR it, check the text.

Usage: python ocr/smoke.py http://127.0.0.1:8000   (needs only Pillow)
Exercises the real Paddle engine, which the unit tests replace with a fake.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

LINES = ["Ana Lopez: can we ship the build on Friday", "Bo Chen: yes, after the review"]


def _post(base: str, img: Image.Image) -> dict:
    buf = BytesIO()
    img.save(buf, format="PNG")
    req = urllib.request.Request(
        f"{base}/v1/ocr", data=buf.getvalue(), headers={"content-type": "image/png"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def main(base: str) -> int:
    font = ImageFont.load_default(28)
    img = Image.new("RGB", (900, 100), (20, 20, 20))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(LINES):
        draw.text((16, 10 + 44 * i), line, fill=(255, 255, 255), font=font)
    body = _post(base, img)
    got = [ln["text"] for ln in body["lines"]]
    print(json.dumps(body))
    if body["too_dense"] or [g.lower() for g in got] != [w.lower() for w in LINES]:
        print(f"expected {LINES}, got {got}", file=sys.stderr)
        return 1
    # A page of 24 text lines must be refused before recognition: proves the real engine has
    # the box limit installed (the unit tests replace the engine and cannot see it).
    dense = Image.new("RGB", (700, 700), (20, 20, 20))
    draw = ImageDraw.Draw(dense)
    for i in range(24):
        draw.text((10, 10 + 28 * i), f"line {i} of a slide that is not a caption", fill="white", font=ImageFont.load_default(20))
    body = _post(base, dense)
    print(json.dumps(body))
    if not body["too_dense"] or body["lines"]:
        print("dense page was not refused as too_dense", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
