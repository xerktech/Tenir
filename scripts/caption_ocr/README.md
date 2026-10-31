# caption_ocr — meeting-captions spike harness (XERK-2504)

Results and the decision they led to: [`docs/meeting-captions.md`](../../docs/meeting-captions.md).

- `capture_windows.py` — Windows only. Captures the four meeting windows with Windows Graphics
  Capture and reports real vs black frames per window state.
- `caption_bench.py` — runs Tesseract, PaddleOCR (mobile/server) and RapidOCR over caption frames with
  several crop strategies; prints line accuracy, speaker accuracy and per-frame latency.
- `caption_synth.py` — synthetic Zoom-caption frames with ground truth, for when no real capture is at hand.
- `caption_score.py` — the metrics.

```bash
apt install tesseract-ocr
pip install pytesseract paddlepaddle paddleocr rapidocr onnxruntime pillow rapidfuzz
python scripts/caption_ocr/caption_bench.py --synth 30 --engines tesseract,paddle-mobile --crops box --out /tmp/capocr
python scripts/caption_ocr/caption_bench.py --images captures/ --out /tmp/capocr   # needs captures/truth.json
```

`truth.json` maps each PNG name to its caption lines, `["Name: text", ...]`.
Tests: `python -m pytest scripts/caption_ocr/tests` (needs only `rapidfuzz` and `pillow`).
