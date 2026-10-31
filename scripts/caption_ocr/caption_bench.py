"""Caption-OCR benchmark for the meeting-captions spike (XERK-2504).

Runs Tesseract and PaddleOCR (CPU) over caption frames under several crop strategies and reports
line accuracy, speaker-name accuracy and per-frame latency (see caption_score.py for the metrics).

    # synthetic Zoom-caption frames (no real screenshot needed)
    python scripts/caption_ocr/caption_bench.py --synth 40 --out /tmp/capocr
    # real frames: a dir of PNGs plus truth.json {"file.png": ["Name: text", ...]}
    python scripts/caption_ocr/caption_bench.py --images captures/ --out /tmp/capocr

Deps: pip install pytesseract rapidocr onnxruntime paddlepaddle paddleocr pillow rapidfuzz;
apt install tesseract-ocr.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))

from caption_score import aggregate, score_frame  # noqa: E402

# Zoom draws captions in the lower part of the meeting window; the band keeps them with margin.
BAND_TOP = 0.62
BAND_BOTTOM = 0.97
# Upscale the band until the frame is this tall in "window pixels" (1080p caption font size).
TARGET_HEIGHT = 1080
# Caption-box finder (find_caption_box), in pixels at TARGET_HEIGHT scale.
BOX_DARK = 80  # box fill is ~73% opaque black: even over white video it stays below this
BOX_WHITE = 110  # glyph pixels: white text dulls to ~180 after a 0.5x remote downscale
GLYPH_REACH = 4  # a glyph pixel has a dark pixel within this many px (white text on the dark box)
CENTER_HALF = 40  # half-width of the centre window the caption must cross on both sides
BOX_LINE_GAP = 32  # glyph-row gap still inside one caption (line spacing)
BOX_WORD_GAP = 80  # glyph-column gap still inside one caption line (word spacing)
BOX_MIN_HEIGHT = 10  # shorter glyph runs are UI chrome; a blurred 0.5x caption line is ~13
BOX_PAD = 8  # margin kept around the glyphs
OUTLINE_INSET = 10  # px the box outline can light inside a span (text starts ~14 px in)
DETECTOR_MARGIN = 32  # black border PP-OCR engines get around a box crop (see _detector_margin)
BOX_MIN_WIDTH = 80  # the shortest caption ("Omar: ok") is wider than this
BOX_CENTRE_TOL = 0.08  # the box is centred: its middle within this share of the width
BOX_MAX_HEIGHT = 160  # Zoom shows at most 3 caption lines (~36 px each)
BOX_FILL_SHARE = 0.5  # share of dark pixels in a caption box (fill ~73% black, glyphs white)
BOX_TRIM_SHARE = 0.2  # edge rows darker than this are kept as caption rows
FILL_DARK = 90  # grey counted as box fill: <= ~72 over any video, plus JPEG noise and blur
FILL_MARGIN = 12  # px of box padding (Zoom pads ~14 px) included in the fill check
EDGE_SHARE = 0.5  # a column/row lit along more than this share of the band is an edge, not text
# Tallest glyph line that is caption text: Zoom's default caption font measures <= 23 px here,
# a camera-off name centred in its tile 29-30 px. Retune if the operator enlarges captions.
BOX_MAX_LINE = 26
LINE_SPLIT_GAP = 2  # rows of no ink that separate two text lines
LINE_MIN_ROWS = 5  # shorter glyph runs aren't text lines (a blurred 0.5x line is ~13 rows)


def crop_band(img: Image.Image, top: float = BAND_TOP, bottom: float = BAND_BOTTOM) -> Image.Image:
    w, h = img.size
    return img.crop((0, int(h * top), w, int(h * bottom)))


def upscale_to(img: Image.Image, frame_h: int, target_h: int = TARGET_HEIGHT) -> Image.Image:
    """Upscale a crop by target_h/frame_h so small remote windows reach caption-font size."""
    factor = target_h / frame_h
    if factor <= 1.05:
        return img
    return img.resize(
        (round(img.width * factor), round(img.height * factor)), Image.Resampling.LANCZOS
    )


def _runs(flags: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """[start, end) runs of True, merging runs separated by at most ``gap`` False values."""
    idx = np.flatnonzero(flags)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]])) + 1
    return list(zip(starts.tolist(), ends.tolist(), strict=False))


def glyph_mask(img: Image.Image) -> np.ndarray:
    """Bright pixels with a dark pixel within GLYPH_REACH px: white text on a dark fill.

    Bright video is excluded (its neighbours are bright too); so is a dark strip with no text.
    """
    g = np.asarray(ImageOps.grayscale(img), dtype=np.uint8)
    dark = g < BOX_DARK
    near_dark = dark.copy()
    for d in range(1, GLYPH_REACH + 1):
        near_dark[:, d:] |= dark[:, :-d]
        near_dark[:, :-d] |= dark[:, d:]
        near_dark[d:, :] |= dark[:-d, :]
        near_dark[:-d, :] |= dark[d:, :]
    return (g > BOX_WHITE) & near_dark


def find_caption_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Locate Zoom's caption box in a band crop: (left, top, right, bottom), or None.

    Zoom centres its caption box horizontally, so a caption's glyphs cross the frame's centre
    column with ink on both sides of it. Tile name labels (left-aligned in their tile) and dark
    toolbars fail that test, which is what lets this work on camera-off (dark) meetings where
    the box fill is no darker than the video. A camera-off name centred in its tile does pass it,
    so glyph lines taller than caption text are rejected, and of the remaining centred runs the
    lowest wins (the caption box sits below the tiles). The box then grows to the lines above and
    below within its columns: lines are left-aligned in the box, so a short one may miss the
    centre. None means no caption, so the capture client sends nothing.
    """
    m = glyph_mask(img)
    # Long edges (a tile gutter on the centre column, a tile's bottom edge over a dark strip)
    # light up most of a column or row; caption glyphs never do.
    m[:, m.mean(axis=0) > EDGE_SHARE] = False
    m[m.mean(axis=1) > EDGE_SHARE, :] = False
    h, w = m.shape
    c = w // 2
    left_ink = m[:, c - CENTER_HALF : c].any(axis=1)
    right_ink = m[:, c : c + CENTER_HALF].any(axis=1)
    centre_ink = left_ink | right_ink  # the box's own side edges stay out of line heights

    # Text lines crossing the centre; caption-sized ones close together form a candidate box,
    # and anything taller (a centred camera-off name, video texture) separates candidates.
    # Built bottom-up, capped at BOX_MAX_HEIGHT, so video texture above a caption can't swallow it.
    groups: list[list[int]] = []
    prev_top = None
    for a, b in reversed(_runs(centre_ink, LINE_SPLIT_GAP)):
        if b - a < LINE_MIN_ROWS:
            continue  # video speckle or the box's edge: bridging through it splits real captions
        if b - a > BOX_MAX_LINE:
            prev_top = None
            continue
        if (
            prev_top is not None
            and prev_top - b <= BOX_LINE_GAP
            and groups[-1][1] - a <= BOX_MAX_HEIGHT
        ):
            groups[-1][0] = a
        else:
            groups.append([a, b])
        prev_top = a
    candidates = [
        (top, bottom)
        for top, bottom in reversed(groups)
        if bottom - top >= BOX_MIN_HEIGHT
        and left_ink[top:bottom].any()
        and right_ink[top:bottom].any()
    ]
    if not candidates:
        return None

    def centre_span(top: int, bottom: int) -> tuple[int, int] | None:
        """Glyph columns (words bridged) containing the centre, if centred and wide enough."""
        cols = m[top:bottom].any(axis=0)
        span = next(((a, b) for a, b in _runs(cols, BOX_WORD_GAP) if a <= c < b), None)
        if span is None or span[1] - span[0] < BOX_MIN_WIDTH:
            return None
        return span if abs((span[0] + span[1]) / 2 - c) <= BOX_CENTRE_TOL * w else None

    # Box fill over white video is ~66 grey, so the fill test needs a looser bar than BOX_DARK.
    dark = np.asarray(ImageOps.grayscale(img), dtype=np.uint8) < FILL_DARK

    for top, bottom in reversed(candidates):  # lowest first: the caption sits below the tiles
        span = centre_span(top, bottom)
        if span is None:
            continue
        # Grow to caption lines above/below that sit in the box's columns but miss the centre,
        # one caption-sized line on box fill at a time; anything else (video, a tile name) ends
        # the box. Use columns just inside the span: over bright video the box's side outline
        # lights the mask on every box row and would fuse all lines into one too-tall run.
        lo = span[0] + OUTLINE_INSET
        inner = m[:, lo : max(lo + 1, span[1] - OUTLINE_INSET)]
        span_fill = dark[:, span[0] : span[1]].mean(axis=1)

        def caption_line(a: int, b: int, span_fill: np.ndarray = span_fill) -> bool:
            return b - a <= BOX_MAX_LINE and span_fill[a:b].mean() >= BOX_TRIM_SHARE

        inside = _runs(inner.any(axis=1), LINE_SPLIT_GAP)
        for a, b in reversed([r for r in inside if r[1] <= top]):
            if top - b > BOX_LINE_GAP or not caption_line(a, b) or bottom - a > BOX_MAX_HEIGHT:
                break
            top = a
        for a, b in [r for r in inside if r[0] >= bottom]:
            if a - bottom > BOX_LINE_GAP or not caption_line(a, b) or b - top > BOX_MAX_HEIGHT:
                break
            bottom = b
        left, right = centre_span(top, bottom) or span
        # The box edge over bright video lights the mask too; trim rows that are mostly video.
        # (Glyph rows of a blurred 0.5x caption are still >= ~30% fill; edge rows are less.)
        fill = dark[:, left:right].mean(axis=1)
        while bottom - top > BOX_MIN_HEIGHT and fill[top] < BOX_TRIM_SHARE:
            top += 1
        while bottom - top > BOX_MIN_HEIGHT and fill[bottom - 1] < BOX_TRIM_SHARE:
            bottom -= 1
        # Captions are white on a dark fill. Dark text on a white slide has a bright halo that
        # passes the glyph mask, but the region around it is light: not a caption box. Measure
        # with the box's own padding included (dark fill even where blurred glyphs make text
        # rows mostly light), leaving out rows that are plainly video beside the box; the kept
        # rows must still cover the text rows.
        rows = fill[max(0, top - FILL_MARGIN) : min(h, bottom + FILL_MARGIN)]
        rows = rows[rows >= BOX_TRIM_SHARE]
        if rows.size < bottom - top or rows.mean() < BOX_FILL_SHARE:
            continue
        return (
            max(0, left - BOX_PAD),
            max(0, top - BOX_PAD),
            min(w, right + BOX_PAD),
            min(h, bottom + BOX_PAD),
        )
    return None


def crop_box(img: Image.Image) -> Image.Image | None:
    """Tight crop to the caption box at caption-font scale; None when no caption is on screen."""
    # Find the box at caption-font scale so the pixel thresholds hold for small remote windows.
    band = upscale_to(crop_band(img), img.height)
    box = find_caption_box(band)
    return band.crop(box) if box else None


CROPS: dict[str, Callable[[Image.Image], Image.Image | None]] = {
    "full": lambda im: im,
    "band": crop_band,
    "band_up": lambda im: upscale_to(crop_band(im), im.height),
    "box": crop_box,
}


def group_lines(boxes: list[tuple[float, float, float, str]]) -> list[str]:
    """Merge (y_top, y_bottom, x_left, text) word/segment boxes into reading-order lines.

    Two boxes share a line when their vertical spans overlap by more than half the shorter one;
    PaddleOCR sometimes splits ``Name:`` from the rest of the caption.
    """
    lines: list[list[tuple[float, float, float, str]]] = []
    for box in sorted(boxes, key=lambda b: (b[0] + b[1]) / 2):
        y0, y1 = box[0], box[1]
        for line in lines:
            ly0, ly1 = min(b[0] for b in line), max(b[1] for b in line)
            overlap = min(y1, ly1) - max(y0, ly0)
            if overlap > 0.5 * min(y1 - y0, ly1 - ly0):
                line.append(box)
                break
        else:
            lines.append([box])
    return [" ".join(b[3] for b in sorted(line, key=lambda b: b[2])) for line in lines]


def _detector_margin(img: Image.Image, crop: str) -> Image.Image:
    """Pad a tight caption-box crop with black: PP-OCR's detector misses whole lines without a
    margin (it dropped 5 of 156 caption lines on 37 px crops; a 32 px border recovered all)."""
    return ImageOps.expand(img, border=DETECTOR_MARGIN, fill=(0, 0, 0)) if crop == "box" else img


class Tesseract:
    name = "tesseract"

    def __init__(self) -> None:
        import pytesseract

        self._t = pytesseract

    def __call__(self, img: Image.Image, crop: str) -> list[str]:
        # Captions are white on a dark box: invert to dark-on-light, which Tesseract expects.
        # (Hard thresholding was tried and lost badly: it keeps bright video pixels and erodes
        # anti-aliased glyphs.) A crop is one text block (psm 6); a full frame is sparse (psm 11).
        gray = ImageOps.invert(ImageOps.grayscale(img))
        psm = 11 if crop == "full" else 6
        d = self._t.image_to_data(gray, config=f"--psm {psm}", output_type=self._t.Output.DICT)
        boxes = [
            (d["top"][i], d["top"][i] + d["height"][i], d["left"][i], d["text"][i])
            for i in range(len(d["text"]))
            if d["text"][i].strip() and float(d["conf"][i]) > 30
        ]
        return group_lines(boxes)


class Paddle:
    def __init__(self, variant: str, threads: int) -> None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        self.name = f"paddle-{variant}"
        models = {
            "mobile": ("PP-OCRv5_mobile_det", "en_PP-OCRv5_mobile_rec"),
            "server": ("PP-OCRv5_server_det", "PP-OCRv5_server_rec"),
        }[variant]
        self._ocr = PaddleOCR(
            text_detection_model_name=models[0],
            text_recognition_model_name=models[1],
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
            # Paddle 3.x oneDNN path raises ConvertPirAttribute2RuntimeAttribute on CPU.
            enable_mkldnn=False,
            cpu_threads=threads,
        )

    def __call__(self, img: Image.Image, crop: str) -> list[str]:
        img = _detector_margin(img, crop)
        res = self._ocr.predict(np.asarray(img.convert("RGB"))[:, :, ::-1])[0]
        boxes = [
            (float(p[:, 1].min()), float(p[:, 1].max()), float(p[:, 0].min()), t)
            for p, t, s in zip(res["rec_polys"], res["rec_texts"], res["rec_scores"], strict=False)
            if t.strip() and s > 0.5
        ]
        return group_lines(boxes)


class Rapid:
    """PP-OCR mobile models on onnxruntime (RapidOCR): Paddle's accuracy without its CPU runtime."""

    name = "rapidocr"

    def __init__(self, threads: int) -> None:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        self._ocr = RapidOCR(
            params={
                "Global.use_cls": False,
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.lang_type": LangRec.EN,
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )

    def __call__(self, img: Image.Image, crop: str) -> list[str]:
        res = self._ocr(np.asarray(_detector_margin(img, crop).convert("RGB")))
        if res.boxes is None:
            return []
        boxes = [
            (float(p[:, 1].min()), float(p[:, 1].max()), float(p[:, 0].min()), t)
            for p, t, s in zip(res.boxes, res.txts, res.scores, strict=False)
            if t.strip() and s > 0.5
        ]
        return group_lines(boxes)


ENGINES = ("tesseract", "rapidocr", "paddle-mobile", "paddle-server")


def make_engine(name: str, threads: int):
    if name == "tesseract":
        return Tesseract()
    if name == "rapidocr":
        return Rapid(threads)
    return Paddle(name.split("-", 1)[1], threads)


def run(engine, crop: str, frames: list[tuple[str, Image.Image, list[str]]], dump: Path | None):
    scores, lat = [], []
    warm = False
    for name, img, truth in frames:
        t0 = time.perf_counter()
        region = CROPS[crop](img)
        lines = [] if region is None else engine(region, crop)  # no caption box: nothing to OCR
        dt = (time.perf_counter() - t0) * 1000
        if region is not None:  # skipped frames aren't OCR latency
            if warm:
                lat.append(dt)
            warm = True  # the first OCR call pays model warm-up
        scores.append(score_frame(truth, lines))
        if dump is not None:
            with dump.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "engine": engine.name,
                            "crop": crop,
                            "frame": name,
                            "truth": truth,
                            "ocr": lines,
                            "ms": round(dt, 1),
                        }
                    )
                    + "\n"
                )
    return aggregate(scores, lat)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--synth", type=int, metavar="N", help="generate N synthetic frames per scale")
    src.add_argument("--images", type=Path, help="dir of PNGs with truth.json")
    ap.add_argument(
        "--scales",
        default="1.0,0.75,0.5",
        help="synthetic window scales (remote window size vs 1080p)",
    )
    ap.add_argument("--engines", default=",".join(ENGINES))
    ap.add_argument("--crops", default=",".join(CROPS))
    ap.add_argument("--threads", type=int, default=1, help="CPU threads per engine")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.synth is not None and a.synth < 1:
        ap.error("--synth needs at least 1 frame per scale")
    for e in a.engines.split(","):
        if e not in ENGINES:
            ap.error(f"unknown engine {e!r}; choose from {', '.join(ENGINES)}")
    try:
        scales = [float(x) for x in a.scales.split(",")]
    except ValueError:
        ap.error(f"--scales must be comma-separated numbers, got {a.scales!r}")
    if any(not 0 < x <= 1 for x in scales):
        ap.error("--scales must be in (0, 1]")
    if a.images is not None and not (a.images / "truth.json").is_file():
        ap.error(f"{a.images}/truth.json not found")
    for c in a.crops.split(","):
        if c not in CROPS:
            ap.error(f"unknown crop {c!r}; choose from {', '.join(CROPS)}")

    os.environ["OMP_THREAD_LIMIT"] = str(a.threads)  # Tesseract's OpenMP pool
    a.out.mkdir(parents=True, exist_ok=True)
    if a.synth is not None:
        from caption_synth import generate

        img_dir = a.out / "frames"
        truth_path = generate(img_dir, a.synth, scales)
    else:
        img_dir, truth_path = a.images, a.images / "truth.json"
    truth = json.loads(truth_path.read_text())
    frames = [(n, Image.open(img_dir / n).convert("RGB"), lines) for n, lines in truth.items()]

    def groups():
        if a.synth is None:
            yield "all", frames
            return
        for s in a.scales.split(","):
            yield f"scale={s}", [f for f in frames if f"_s{float(s)}_" in f[0]]

    dump = a.out / "ocr.jsonl"
    dump.unlink(missing_ok=True)
    results = []
    for ename in a.engines.split(","):
        engine = make_engine(ename, a.threads)
        for crop in a.crops.split(","):
            for label, subset in groups():
                r = run(engine, crop, subset, dump) | {"engine": ename, "crop": crop, "set": label}
                results.append(r)
                print(
                    f"{ename:14} {crop:8} {label:11} line={r['line_acc']:.3f} "
                    f"prec={r['line_precision']:.3f} "
                    f"char={r['char_acc']:.3f} speaker={r['speaker_acc']:.3f} "
                    f"p50={r['lat_p50_ms']:.0f}ms p95={r['lat_p95_ms']:.0f}ms",
                    flush=True,
                )
    # NaN (a metric with nothing to measure) isn't valid JSON; write it as null
    clean = [
        {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in r.items()}
        for r in results
    ]
    (a.out / "results.json").write_text(json.dumps(clean, indent=1, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
