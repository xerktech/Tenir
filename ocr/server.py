"""tenir-ocr: caption OCR for meeting captions (epic XERK-2503, service XERK-2512).

The capture client on the operator's PC crops each meeting window down to the caption box
(docs/meeting-captions.md) and the api forwards that crop here. This service runs PaddleOCR
PP-OCRv5 mobile on CPU and returns the caption lines in reading order. It knows nothing about
Zoom, Teams or speakers: parsing ``Name: text`` and merging rolling captions is the api's job.

Concurrency: ``OCR_WORKERS`` engine processes, one request per engine at a time. A single
caption crop does not get faster with more threads (measured, XERK-2504), so throughput comes
from running one crop per core. When every engine is busy a request waits up to
``OCR_BUSY_TIMEOUT_S`` and then gets 503: the caller drops stale frames and sends the newest,
so queueing old ones here would only add latency.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import BrokenExecutor, Executor, ProcessPoolExecutor
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

log = logging.getLogger("tenir-ocr")

WORKERS = int(os.environ.get("OCR_WORKERS", "4"))
BUSY_TIMEOUT_S = float(os.environ.get("OCR_BUSY_TIMEOUT_S", "5"))
# This service takes caption-box CROPS, never whole frames: at 1080p-equivalent a crop is at most
# ~1025 x 145 px (0.15 Mpx, XERK-2504). Pixels set both memory and time, so the cap is sized from
# the Deployment's budget, not from a screen: a worker peaks at ~0.5 GB on a 900x100 crop but
# 1.2 GB on a 1080p frame and 3.2 GB on 4K (QA, 2026-10-31), and two 4K frames OOM-killed a
# 4 GiB pod. 0.5 Mpx is ~3x the largest crop.
MAX_PIXELS = int(os.environ.get("OCR_MAX_PIXELS", "500000"))
MAX_BYTES = int(os.environ.get("OCR_MAX_BYTES", str(2 * 1024 * 1024)))
# Recognition runs once per detected box and is the whole cost; detection is cheap. A caption
# crop has a handful of boxes (the 24 synthetic ones: under 6); a dense slide mistaken for a
# caption box has dozens, and recognising them held a worker for up to ~3 minutes (QA). So an
# image with more boxes than this is refused after detection, before recognition (`too_dense`).
MAX_BOXES = int(os.environ.get("OCR_MAX_BOXES", "16"))
MIN_SCORE = 0.5
# Segments on one row further apart than this many line heights are separate lines (tile
# labels side by side on a full window frame), never one caption line.
ROW_GAP = float(os.environ.get("OCR_ROW_GAP", "1.5"))
# PP-OCR's detector drops whole lines on a tight crop without a black border (XERK-2504).
DETECTOR_MARGIN = 32

# Pillow's own bomb guard is off: _decode checks the header's size against MAX_PIXELS before
# anything is decoded, and Pillow's guard fired first for big frames, turning a 413 into a 400.
Image.MAX_IMAGE_PIXELS = None


def make_engine():  # pragma: no cover - needs the Paddle runtime; tests replace it
    """One PaddleOCR PP-OCRv5 mobile pipeline, CPU, single-threaded."""
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="en_PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
        # oneDNN is Paddle's fast CPU path, but 3.3.1 (latest as of 2026-10-31) still fails
        # startup with ConvertPirAttribute2RuntimeAttribute when it is on (XERK-2504, XERK-2512).
        enable_mkldnn=False,
        cpu_threads=1,
    )
    # Count the boxes the detector KEEPS (after its size/score filter) and stop before
    # recognition. Not max_candidates: that truncates raw contours before the filter, so noise
    # used up the slots and the real caption was silently dropped (QA). Private path: a Paddle
    # bump that moves it fails startup here, loudly, instead of losing the time bound.
    limit_boxes(ocr.paddlex_pipeline._pipeline)

    def run(bgr: np.ndarray) -> list[tuple[Any, str, float]]:
        res = ocr.predict(bgr)[0]
        return list(zip(res["rec_polys"], res["rec_texts"], res["rec_scores"], strict=False))

    return run


class TooDense(Exception):
    """More text boxes than a caption crop has; raised between detection and recognition."""


class _BoxLimit:
    """Wraps the detector's post-processor: (polys, scores) per image, after filtering."""

    def __init__(self, post) -> None:
        self._post = post

    def __call__(self, *args, **kwargs):
        polys, scores = self._post(*args, **kwargs)
        if any(len(p) > MAX_BOXES for p in polys):
            raise TooDense
        return polys, scores

    def __getattr__(self, name):
        return getattr(self._post, name)


def limit_boxes(pipeline) -> None:
    """Install _BoxLimit on a paddlex OCR pipeline's detector (idempotent)."""
    det = pipeline.text_det_model
    assert callable(getattr(det, "post_op", None)), "paddlex moved text_det_model.post_op"
    if not isinstance(det.post_op, _BoxLimit):
        det.post_op = _BoxLimit(det.post_op)


def group_lines(boxes: list[tuple[float, float, float, float, str, float]]) -> list[dict]:
    """Merge (y0, y1, x0, x1, text, score) segments into reading-order lines.

    Two segments share a line when their vertical spans overlap by more than half the shorter
    one; PaddleOCR sometimes splits ``Name:`` from the rest of a caption line. Same rule as
    scripts/caption_ocr/caption_bench.py, which the accuracy numbers were measured with.
    """
    lines: list[list[tuple[float, float, float, float, str, float]]] = []
    for box in sorted(boxes, key=lambda b: (b[0] + b[1]) / 2):
        y0, y1 = box[0], box[1]
        for line in lines:
            ly0, ly1 = min(b[0] for b in line), max(b[1] for b in line)
            if min(y1, ly1) - max(y0, ly0) > 0.5 * min(y1 - y0, ly1 - ly0):
                line.append(box)
                break
        else:
            lines.append([box])
    out = []
    for line in lines:
        for segs in _split_at_gaps(sorted(line, key=lambda b: b[2])):
            out.append(
                {
                    "text": " ".join(b[4] for b in segs),
                    "score": round(min(b[5] for b in segs), 4),
                    "box": [
                        round(min(b[2] for b in segs)),
                        round(min(b[0] for b in segs)),
                        round(max(b[3] for b in segs)),
                        round(max(b[1] for b in segs)),
                    ],
                }
            )
    return out


def _split_at_gaps(
    segs: list[tuple[float, float, float, float, str, float]],
) -> list[list[tuple[float, float, float, float, str, float]]]:
    """Split one row (segments sorted left to right) at gaps much wider than a word space.

    A caption row is continuous text, but a full window frame (the api reads it in strips for
    the participant tiles, XERK-2505) has several tile labels on one row: joined, "Priya Raman
    Sean ONeil" read as one name and the roster broke. A gap over ROW_GAP line heights splits
    them; word gaps, and Paddle splitting ``Name:`` off a caption line, are far smaller.
    """
    height = max(b[1] for b in segs) - min(b[0] for b in segs)
    runs = [[segs[0]]]
    for seg in segs[1:]:
        if seg[2] - max(b[3] for b in runs[-1]) > ROW_GAP * height:
            runs.append([seg])
        else:
            runs[-1].append(seg)
    return runs


def recognise(engine, img: Image.Image) -> tuple[list[dict], bool]:
    """OCR one image -> (lines, too_dense). Boxes are [x0, y0, x1, y1] in the submitted image's
    pixels. too_dense: more than MAX_BOXES text boxes, so nothing was recognised (lines == [])."""
    padded = ImageOps.expand(img.convert("RGB"), border=DETECTOR_MARGIN, fill=(0, 0, 0))
    bgr = np.asarray(padded)[:, :, ::-1]
    m = DETECTOR_MARGIN
    boxes = []
    try:
        raw = engine(bgr)
    except TooDense:
        return [], True
    for poly, text, score in raw:
        if not text.strip() or score <= MIN_SCORE:
            continue
        p = np.asarray(poly, dtype=float)
        boxes.append(
            (
                max(0.0, p[:, 1].min() - m),
                max(0.0, p[:, 1].max() - m),
                max(0.0, p[:, 0].min() - m),
                max(0.0, p[:, 0].max() - m),
                text.strip(),
                float(score),
            )
        )
    return group_lines(boxes), False


def make_executor() -> Executor:
    """One process per engine. Paddle holds the GIL while it predicts, so threads gave no
    speedup at all (4 threads: 0.97x of serial, measured in the cluster); processes do."""
    return ProcessPoolExecutor(WORKERS, mp_context=mp.get_context("spawn"), initializer=_init_worker)


_engine = None


def _init_worker() -> None:
    global _engine
    _engine = make_engine()
    recognise(_engine, Image.new("RGB", (64, 32)))  # the first predict initialises the predictor


def _ocr_in_worker(data: bytes) -> dict:
    t0 = time.perf_counter()
    lines, too_dense = recognise(_engine, Image.open(BytesIO(data)))
    return {"lines": lines, "too_dense": too_dense, "ms": round((time.perf_counter() - t0) * 1000)}


_executor: Executor | None = None
# One slot per engine; a request that cannot get one in BUSY_TIMEOUT_S is refused, not queued.
_slots = threading.BoundedSemaphore(1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _executor, _slots
    _slots = threading.BoundedSemaphore(WORKERS)
    _executor = make_executor()
    # Every worker runs its initializer (model load + warm-up) before taking a task, so a round
    # of trivial tasks completing means the engines are loaded.
    for f in [_executor.submit(os.getpid) for _ in range(WORKERS)]:
        await asyncio.wrap_future(f)
    log.info("tenir-ocr ready with %d engines", WORKERS)
    try:
        yield
    finally:
        ex, _executor = _executor, None
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="tenir-ocr", lifespan=lifespan)


@app.get("/health")
async def health():
    # async: a sync handler would wait for an anyio worker thread, and under a burst those are
    # all parked in _slots.acquire, which stalled /health past the probe timeout (QA).
    # _broken is set as soon as any worker process dies, before a request notices.
    if _executor is None or getattr(_executor, "_broken", False):
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok", "workers": WORKERS}


def _decode(body: bytes) -> None:
    """Validate the image: 413 on size from the header alone, 400 if it does not decode."""
    try:
        img = Image.open(BytesIO(body))
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(400, f"not a decodable image: {type(e).__name__}") from e
    if img.width * img.height > MAX_PIXELS:
        raise HTTPException(413, f"image over {MAX_PIXELS} pixels; send the caption crop")
    try:
        img.load()
    except OSError as e:
        raise HTTPException(400, f"not a decodable image: {type(e).__name__}") from e


@app.post("/v1/ocr")
async def ocr(request: Request):
    """Body: the raw image bytes (PNG or JPEG). Returns ``{"lines": [...], "ms": n}``."""
    global _executor
    ex = _executor
    if ex is None:
        raise HTTPException(503, "unavailable")
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BYTES:
        raise HTTPException(413, "image too large")
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise HTTPException(413, "image too large")
    except ClientDisconnect:
        return Response(status_code=499)  # nobody to answer; skip the traceback per disconnect
    if not body:
        raise HTTPException(400, "empty body")
    data = bytes(body)
    # On a thread: decoding even a capped image should not stall the event loop. Validated here
    # so a bad image never reaches (or crashes) a worker.
    await run_in_threadpool(_decode, data)
    if not await run_in_threadpool(_slots.acquire, True, BUSY_TIMEOUT_S):
        raise HTTPException(503, "all OCR workers busy")
    try:
        return await asyncio.wrap_future(ex.submit(_ocr_in_worker, data))
    except BrokenExecutor:
        # A worker died (OOM, native crash). Fail /health so the liveness probe restarts the pod.
        log.exception("OCR worker pool broken")
        _executor = None
        raise HTTPException(503, "OCR worker pool broken") from None
    finally:
        _slots.release()
