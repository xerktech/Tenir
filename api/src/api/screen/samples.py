"""Opt-in sampling of real screen frames for tuning the caption parsers (XERK-2705).

The parsers' pixel thresholds (Zoom's dim name line, its avatar circle) were tuned on synthetic
crops only; real crops from the capture client are needed to re-measure them. With
``API_SCREEN_SAMPLE_DIR`` set, each frame's JPEG and its OCR result are written there, up to
``API_SCREEN_SAMPLE_MAX`` frames (and ``MAX_BYTES``) per process. Off (empty) by default: the
frames are screenshots of client meetings, so turn it on for one meeting, copy the files off,
then turn it off and delete them. Never commit them (the repo is public).

Writes run on one dedicated thread and a frame never waits for them: a stalled volume must not
stall a meeting's lane nor tie up the shared executor that ws auth and audio writes use (QA).
While a write is in flight, later frames are skipped, not queued.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from api.contract import ScreenFrameHeader
from api.metrics import metrics
from api.screen.ocr import OcrResult

log = logging.getLogger(__name__)

# Bytes written per process, whatever the frame cap: 2000 4 MB full frames would be 8 GB on a
# volume the audio shares.
MAX_BYTES = 1_000_000_000
# Consecutive failed writes after which sampling stops: a broken directory otherwise logged a
# warning on every frame for the whole meeting (QA).
FAIL_LIMIT = 5
# A write in flight this long is reported once: a stalled volume otherwise stopped sampling
# with only a skip counter to show it (QA).
STUCK_SECONDS = 30.0
# Per process: the frame number restarts at 1, so a restart could reuse a file name (QA).
_RUN = secrets.token_hex(3)

_lock = threading.Lock()
_busy = False
_saved = 0
_bytes = 0
_failed = 0
_started = 0.0
_stuck_told = False
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-sample")


def _slug(value: str) -> str:
    # orgSiteKey comes from the client: never let it pick a path. The hash keeps keys that
    # slug alike ("a/b", "a\\b") in separate directories.
    tag = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', value)[:48]}-{tag}"


def submit(
    root: str, limit: int, header: ScreenFrameHeader, jpeg: bytes, result: OcrResult, ts: int
) -> bool:
    """Queue one frame for ``save`` on the sampler thread; never blocks, never raises.

    Returns whether it was queued: not while the previous write is still running, nor once the
    caps are reached.
    """
    global _busy, _started, _stuck_told
    with _lock:
        stuck = _busy and not _stuck_told and time.monotonic() - _started > STUCK_SECONDS
        if stuck:
            _stuck_told = True
        if _busy or _saved >= limit or _bytes >= MAX_BYTES or _failed >= FAIL_LIMIT:
            metrics.incr("screen.samples_skipped")
            skip = True
        else:
            _busy, _started, skip = True, time.monotonic(), False
    if stuck:
        log.warning("screen sampling stalled: a write has been in flight %.0f s", STUCK_SECONDS)
    if skip:
        return False
    try:
        _pool.submit(_run, root, limit, header, jpeg, result, ts)
    except RuntimeError:  # the pool was shut down (process exit)
        with _lock:
            _busy = False
        return False
    return True


def _run(*args) -> None:
    global _busy, _stuck_told
    try:
        save(*args)
    finally:
        with _lock:
            _busy = False
            _stuck_told = False  # a later stall is reported too


def _score(value: float) -> float | None:
    # A NaN score would be written as bare NaN, which strict JSON parsers refuse (QA).
    return value if math.isfinite(value) else None


def save(
    root: str, limit: int, header: ScreenFrameHeader, jpeg: bytes, result: OcrResult, ts: int
) -> bool:
    """Write one frame and its OCR as ``<root>/<platform>-<org>/<ts>-<run>-<n>-<region>.{jpg,json}``.

    Returns whether both were written. Only a frame written counts toward ``limit``; a failed
    write leaves no half pair behind. Never raises: sampling must not break a meeting.
    """
    global _saved, _bytes, _failed
    with _lock:
        if _saved >= limit or _bytes >= MAX_BYTES or _failed >= FAIL_LIMIT:
            return False
        n = _saved + 1
    region = header.region.value
    stem = f"{ts}-{_RUN}-{n:05d}-{region}"
    meta = {
        "platform": header.platform.value,
        "orgSiteKey": header.orgSiteKey,
        "region": region,
        "ts": ts,
        "width": result.width,
        "height": result.height,
        "lines": [
            {"text": ln.text, "box": list(ln.box), "score": _score(ln.score)} for ln in result.lines
        ],
    }
    d = Path(root) / f"{header.platform.value}-{_slug(header.orgSiteKey)}"
    paths = (d / f"{stem}.jpg", d / f"{stem}.json")
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths[0].write_bytes(jpeg)
        paths[1].write_text(json.dumps(meta, allow_nan=False), encoding="utf-8")
    except (OSError, ValueError) as exc:
        with _lock:
            _failed += 1
            gave_up = _failed >= FAIL_LIMIT
        log.warning("screen sample not saved: %s", exc)
        if gave_up:
            log.warning("screen sampling stopped after %d failed writes", FAIL_LIMIT)
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    with _lock:
        _saved += 1
        _bytes += len(jpeg)
        _failed = 0
        stopped = _saved >= limit or _bytes >= MAX_BYTES
    if stopped:
        log.info("screen sampling stopped: %d frames saved under %s", _saved, root)
    return True


def _reset_for_tests() -> None:
    global _saved, _bytes, _busy, _failed, _stuck_told
    with _lock:
        _saved = _bytes = _failed = 0
        _busy = _stuck_told = False


__all__ = ["MAX_BYTES", "save", "submit"]
