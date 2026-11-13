"""The capture client's own log lines (``screen.log``, XERK-2875), logged next to the api's.

The client's lifecycle lines and warnings (window paused, capturing, caption pane, cadence)
otherwise exist only on the meeting PC. Each accepted line is logged on ``api.screen.client``
as ``client <household>/<user> device <id>: [<LEVEL>] <message>`` so it lands in Loki beside
the api's own screen lines.

Device tokens are client input: lines are capped per message by the contract, and per device
token by a token bucket shared across that token's sockets. Control characters are escaped so
a client line can't forge a second api log line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from api.contract import ScreenLog
from api.metrics import metrics

log = logging.getLogger("api.screen.client")

# A healthy client logs a handful of lines a minute; a reconnect flushes its buffer (≤200).
BURST = 200
PER_SECOND = 1.0
# Buckets kept at most: one per device token seen since start; a full table is rebuilt.
MAX_BUCKETS = 1024


@dataclass
class _Bucket:
    tokens: float
    at: float
    dropped: int = 0  # lines refused since the last accepted one


_buckets: dict[str, _Bucket] = {}


def _escape(text: str) -> str:
    # One log record per client line: newlines and other controls become visible escapes.
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def accept(
    msg: ScreenLog,
    *,
    household: str,
    user_id: str,
    device: str,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Log the batch's lines the device's rate allows; returns how many were logged."""
    now = clock()
    bucket = _buckets.get(device)
    if bucket is None:
        if len(_buckets) >= MAX_BUCKETS:
            _buckets.clear()
        bucket = _buckets[device] = _Bucket(tokens=BURST, at=now)
    bucket.tokens = min(BURST, bucket.tokens + (now - bucket.at) * PER_SECOND)
    bucket.at = now
    who = f"client {household}/{user_id} device {device[:8]}"
    if msg.dropped:
        log.info("%s: dropped %d lines before this batch", who, msg.dropped)
    logged = 0
    for line in msg.lines:
        if bucket.tokens < 1:
            bucket.dropped += 1
            continue
        if bucket.dropped:
            log.info("%s: rate-limited, %d lines dropped", who, bucket.dropped)
            bucket.dropped = 0
        bucket.tokens -= 1
        log.info("%s: [%s] %s", who, line.level.value, _escape(line.message))
        logged += 1
    metrics.incr("screen.client_log_lines", logged)
    if logged < len(msg.lines):
        metrics.incr("screen.client_log_dropped", len(msg.lines) - logged)
    return logged
