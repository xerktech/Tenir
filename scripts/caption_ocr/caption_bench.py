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

# The caption-box finder ships in the capture client (desktop/); the bench measures that code.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
from tenir_capture.caption_box import (  # noqa: E402,F401  (re-exported for the tests)
    BAND_TOP,
    crop_band,
    crop_box,
    find_caption_box,
    upscale_to,
)

DETECTOR_MARGIN = 32  # black border PP-OCR engines get around a box crop (see _detector_margin)

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
