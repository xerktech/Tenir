# ocr — tenir-ocr caption OCR service

PaddleOCR PP-OCRv5 mobile on CPU, the engine and placement XERK-2504 chose
([`docs/meeting-captions.md`](../docs/meeting-captions.md)). The api (XERK-2505) sends it the
caption-box crop the capture client cut from a meeting window and gets the caption lines back.
Deployed by the ArgoCD repo as `ai/tenir/ocr.yaml`.

## Contract

- `POST /v1/ocr` — body is the raw image (PNG or JPEG) of the caption-box crop, at most 2 MiB
  and 0.5 Mpx (~3x the largest 1080p-equivalent crop). Whole frames are refused: a 4K frame
  needs ~3.2 GB in one worker.
  - `200` → `{"lines": [{"text", "score", "box": [x0, y0, x1, y1]}], "too_dense", "ms"}`; lines in reading
    order, `box` in the submitted image's pixels, `score` the line's lowest segment confidence
    (segments ≤ 0.5 are dropped), `ms` the OCR time. `too_dense` is true, with no lines,
    when the detector kept more than `OCR_MAX_BOXES` (16) boxes: not a caption crop (a dense
    slide, a caption plus a ticker). Results are never partial.
  - `400` not an image or empty · `413` over the byte or pixel cap · `503` every worker busy for
    `OCR_BUSY_TIMEOUT_S` (default 5), still loading, or a worker died.
- `GET /health` — `200 {"status": "ok", "workers"}` once every engine is loaded, else `503`.
- No auth: the cluster NetworkPolicy admits only the Tenir api pod.
- It returns text only. Speaker parsing and rolling-caption merge are the api's.

## Behaviour worth knowing

- One process per engine (`OCR_WORKERS`, default 4). Paddle holds the GIL while predicting, so
  4 threads ran at 0.97x of serial; 4 processes ran at 2.6–3.6x through the server (cluster,
  2026-10-31, 4–8 clients).
- A busy pool refuses rather than queues: the caller drops stale frames for the newest.
- The server pads every image with a 32 px black border before detection; PP-OCR drops whole
  lines from a tight crop without it. Returned boxes have the border subtracted.
- Memory: ~550 MB per idle engine, ~850–890 MB peak at the pixel cap (5Gi pod limit).
- Time: a real caption crop takes ~1 s. Nothing interrupts a running OCR; the bounds are the
  pixel cap and `OCR_MAX_BOXES`, because recognition (once per detected box) is the cost and
  detection is cheap. The box count is taken after the detector's own filter and checked before
  recognition, so an over-limit image costs only detection.
- Measured (5Gi pod, 2026-10-31): over-limit pages refused in ~0.5 s; the slowest image still
  under the limit (14 long lines) took ~18 s alone and ~23–24 s with 4 at once. Pod memory
  stayed ≤ 4.2 GB. Set the api's client timeout above that, e.g. 30 s.
- Models are baked into the image; no egress at runtime.

Tests: `pip install -r requirements-test.txt && pytest tests` (fake engine, no Paddle).
