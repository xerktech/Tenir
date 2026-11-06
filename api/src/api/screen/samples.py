"""Opt-in sampling of real screen frames for tuning the caption parsers (XERK-2705).

The parsers' pixel thresholds (Zoom's dim name line, its avatar circle) were tuned on synthetic
crops only; real crops from the capture client are needed to re-measure them. With
``API_SCREEN_SAMPLE_DIR`` set, each frame's JPEG and its OCR result are written there, up to
``API_SCREEN_SAMPLE_MAX`` frames per process. Off (empty) by default: the frames are screenshots
of client meetings, so turn it on for one meeting, copy the files off, then turn it off and
delete them. Never commit them (the repo is public).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from api.contract import ScreenFrameHeader
from api.screen.ocr import OcrResult

log = logging.getLogger(__name__)

_lock = threading.Lock()
_saved = 0


def _slug(value: str) -> str:
    # orgSiteKey comes from the client: never let it pick a path.
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:64] or "_"


def save(
    root: str, limit: int, header: ScreenFrameHeader, jpeg: bytes, result: OcrResult, ts: int
) -> bool:
    """Write one frame and its OCR as ``<root>/<platform>-<org>/<ts>-<n>-<region>.{jpg,json}``.

    Returns whether it was written. Stops for good (this process) after ``limit`` frames, so a
    sampler left on can't fill the volume. Never raises: sampling must not break a meeting.
    """
    global _saved
    with _lock:
        if _saved >= limit:
            return False
        _saved += 1
        n = _saved
    region = header.region.value
    stem = f"{ts}-{n:05d}-{region}"
    meta = {
        "platform": header.platform.value,
        "orgSiteKey": header.orgSiteKey,
        "region": region,
        "ts": ts,
        "width": result.width,
        "height": result.height,
        "lines": [{"text": ln.text, "box": list(ln.box), "score": ln.score} for ln in result.lines],
    }
    try:
        d = Path(root) / f"{_slug(header.platform.value)}-{_slug(header.orgSiteKey)}"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        (d / f"{stem}.jpg").write_bytes(jpeg)
        (d / f"{stem}.json").write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        log.warning("screen sample not saved: %s", exc)
        return False
    if n == limit:
        log.info("screen sampling stopped: %d frames saved under %s", limit, root)
    return True


def _reset_for_tests() -> None:
    global _saved
    with _lock:
        _saved = 0


__all__ = ["save"]
