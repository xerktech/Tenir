---
paths:
  - "ocr/**"
  - ".github/workflows/ocr.yml"
---

# tenir-ocr (XERK-2512)

- Paddle holds the GIL while predicting: 4 threads ran at 0.97x of serial, 4 processes at 3.66x.
  Keep one engine per process (`ProcessPoolExecutor`, `spawn`); never swap it for a thread pool.
- The fake-engine tests replace `make_executor` with a thread pool so the fake stays visible;
  `ocr/smoke.py` (run by `ocr.yml` against the built image) is the only real-Paddle check.
- oneDNN (`enable_mkldnn=True`) still fails startup on Paddle 3.3.1. Re-test on a Paddle bump.
- Models are baked into the image under `PADDLE_PDX_CACHE_HOME`; paddlex writes locks there at
  runtime, so the runtime uid must own it.
- A busy pool refuses (503) instead of queueing: the api drops stale frames for the newest.
- `OCR_MAX_PIXELS` (0.5 Mpx) is the memory AND time bound, sized against the pod limit: 4K frames
  OOM-killed a 4 GiB pod (QA). Raise it only with ai/tenir/ocr.yaml's memory limit in ArgoCD.
- `OCR_MAX_BOXES` (16) is the time bound: `_BoxLimit` wraps the detector's post_op and raises
  `TooDense` before recognition. Never use DBPostProcess.max_candidates for it: that truncates
  raw contours BEFORE the size/score filter, so noise used the slots and the caption was lost.
- Pillow's bomb guard is off (`MAX_IMAGE_PIXELS = None`) because it fired before the 413 check.
- Pixel size is checked from the header before decode; keep `/health` `async` (a sync one
  queued behind parked slot waiters and missed the probe timeout).
- The api depends on the contract in `ocr/README.md`; change both in one PR.
