"""Synthetic Zoom-caption frames for the caption-OCR spike (XERK-2504).

Stand-in for the operator's real Zoom screenshot when it is not available on the benchmarking
host. Each frame mimics a Zoom meeting window as seen through a remote-desktop client: busy video
tiles or camera-off tiles (near-black, white names), a dark semi-transparent caption box at the
bottom with 1-3 ``Name: text`` lines in a white sans font — or no caption at all — then a
downscale (smaller remote window) and JPEG re-encode (remote-desktop codec).

Synthetic frames over-estimate accuracy versus real captures (clean font, no subpixel/ClearType
fringing, no caption animation); treat their numbers as an engine ranking and a latency budget,
not as the production accuracy. Re-run ``caption_bench.py --images`` on real captures for that.
"""

from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

NAMES = [
    "Elodie Martin",
    "Priya Raman",
    "Sean O'Neil",
    "Dana McAllister",
    "Thanh Nguyen",
    "Omar Ahmed",
    "Ana-Maria Lopez",
    "Chris Jr.",
    "Kateryna Shevchenko",
    "J. Smith",
]
PHRASES = [
    "can you open a ticket for the login timeout on staging",
    "I'll have the migration script ready by Thursday",
    "the build on release 4.2.1 is still failing in CI",
    "let's move the vendor call to next Tuesday at 3 PM",
    "please assign the API rate limit bug to the platform team",
    "we need QA sign-off before the 15th, otherwise it slips",
    "the customer reported 37 failed exports since Monday",
    "I'll update the runbook and share it in the channel",
    "who owns the SSO certificate renewal this quarter?",
    "action item: Sean to follow up with legal on the DPA",
    "Azure DevOps board needs a new epic for the reporting work",
    "rollback worked, but the cache wasn't cleared on node 3",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)  # Pillow's bundled scalable font (Pillow >= 10.1)


@dataclass
class Frame:
    image: Image.Image
    lines: list[str]
    name: str
    box: tuple[float, float, float, float] | None = None  # caption box in frame px, if any


def _background(rng: random.Random, w: int, h: int) -> Image.Image:
    """Video-tile grid: per-tile gradient plus noise, so the caption box sits on busy pixels."""
    img = Image.new("RGB", (w, h), (26, 26, 26))
    draw = ImageDraw.Draw(img)
    cols, rows = rng.choice([(1, 1), (2, 2), (3, 2)])
    tw, th = w // cols, (h - 60) // rows
    for c in range(cols):
        for r in range(rows):
            base = tuple(rng.randint(40, 200) for _ in range(3))
            x0, y0 = c * tw + 4, r * th + 4
            for y in range(y0, y0 + th - 8, 6):
                shade = tuple(
                    max(0, min(255, v + (y - y0) // 6 + rng.randint(-8, 8))) for v in base
                )
                draw.rectangle([x0, y, x0 + tw - 8, y + 6], fill=shade)
            draw.text(
                (x0 + 8, y0 + th - 34), rng.choice(NAMES), fill=(255, 255, 255), font=_font(16)
            )
    # seeded from rng: Image.effect_noise uses PIL's own unseeded RNG, which made frames (and
    # the tests built on them) differ run to run
    gen = np.random.default_rng(rng.getrandbits(32))
    noise = Image.fromarray(np.clip(gen.normal(128, 24, (h, w)), 0, 255).astype(np.uint8))
    noise = noise.convert("RGB")
    return Image.blend(img, noise, 0.12)


def _camera_off(rng: random.Random, w: int, h: int) -> Image.Image:
    """Camera-off grid: near-black tiles, a large centred name and a small bottom-left label.

    The hard case for the caption-box finder: the box fill is no darker than the video, and
    white names sit on dark pixels just as caption glyphs do.
    """
    img = Image.new("RGB", (w, h), (26, 26, 26))
    draw = ImageDraw.Draw(img)
    cols, rows = rng.choice([(1, 1), (2, 2), (3, 2)])
    tw, th = w // cols, (h - 60) // rows
    big = _font(40)
    for c in range(cols):
        for r in range(rows):
            x0, y0 = c * tw + 4, r * th + 4
            draw.rectangle([x0, y0, x0 + tw - 8, y0 + th - 8], fill=(35, 35, 35))
            name = rng.choice(NAMES)
            draw.text((x0 + tw / 2, y0 + th / 2), name, fill=(255, 255, 255), font=big, anchor="mm")
            draw.rectangle([x0 + 6, y0 + th - 40, x0 + 200, y0 + th - 14], fill=(0, 0, 0))
            draw.text((x0 + 10, y0 + th - 38), name, fill=(255, 255, 255), font=_font(16))
    return img


def make_frame(
    rng: random.Random,
    idx: int,
    scale: float,
    jpeg_q: int,
    w: int = 1920,
    h: int = 1080,
    dark: bool = False,
    caption: bool = True,
) -> Frame:
    img = (_camera_off if dark else _background)(rng, w, h).convert("RGBA")
    tag = f"synth_{idx:03d}_s{scale}_q{jpeg_q}_{'dark' if dark else 'video'}"
    if not caption:
        return _finish(img.convert("RGB"), [], f"{tag}_nocap", scale, jpeg_q, w, h, None)
    n_lines = rng.choice([1, 2, 2, 3])
    lines = [f"{rng.choice(NAMES)}: {rng.choice(PHRASES)}" for _ in range(n_lines)]
    font = _font(24)  # Zoom's default caption size at 1080p, roughly
    pad, gap = 14, 8
    line_h = font.getbbox("Ag")[3]
    box_w = max(font.getlength(t) for t in lines) + 2 * pad
    box_h = n_lines * line_h + (n_lines - 1) * gap + 2 * pad
    bx, by = (w - box_w) / 2, h - 120 - box_h
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=10, fill=(0, 0, 0, 185))
    for i, text in enumerate(lines):
        od.text(
            (bx + pad, by + pad + i * (line_h + gap)), text, fill=(255, 255, 255, 255), font=font
        )
    img = Image.alpha_composite(img, overlay).convert("RGB")
    return _finish(img, lines, tag, scale, jpeg_q, w, h, (bx, by, bx + box_w, by + box_h))


def _finish(
    img: Image.Image,
    lines: list[str],
    name: str,
    scale: float,
    jpeg_q: int,
    w: int,
    h: int,
    box: tuple[float, float, float, float] | None,
) -> Frame:
    """Downscale to the remote window size and round-trip through JPEG (remote-desktop codec)."""
    if scale != 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
        img = img.filter(ImageFilter.SMOOTH_MORE) if scale < 0.6 else img
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jpeg_q)
    buf.seek(0)
    scaled = tuple(v * scale for v in box) if box else None
    return Frame(Image.open(buf).convert("RGB"), lines, name, scaled)


def generate(out_dir: Path, n: int, scales: list[float], seed: int = 2504) -> Path:
    """Write ``n`` frames per scale and ``truth.json`` ({image: [lines]}); returns the json path."""
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    truth: dict[str, list[str]] = {}
    i = 0
    for scale in scales:
        for _ in range(n):
            # a third camera-off, a fifth with no caption on screen (truth: no lines); independent
            f = make_frame(
                rng, i, scale, rng.choice([60, 75, 85]), dark=i % 3 == 0, caption=i % 5 != 4
            )
            f.image.save(out_dir / f"{f.name}.png")
            truth[f"{f.name}.png"] = f.lines
            i += 1
    path = out_dir / "truth.json"
    path.write_text(json.dumps(truth, indent=1))
    return path
