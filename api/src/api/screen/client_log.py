"""The capture client's own log lines (``screen.log``, XERK-2875), logged next to the api's.

The client's lifecycle lines and warnings (window paused, capturing, caption pane, cadence)
otherwise exist only on the meeting PC. Each accepted line is logged on ``api.screen.client``
as ``client <household>/<user> device <id>: [<LEVEL>] <client time> "<message>"`` so it lands in
Loki beside the api's own screen lines.

Device tokens are client input: lines are capped per message by the contract, and per user by
a token bucket shared across all of the user's device tokens and sockets (a per-token bucket
was bypassed by minting, flooding and revoking tokens, QA). Every logged line, the "lines
dropped" notices included, is paid for from that bucket. The message is quoted with quotes,
backslashes and control characters escaped, so a client line can't forge a second api log
line nor pass its text off as another user's prefix.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from collections.abc import Callable
from dataclasses import dataclass

from api.contract import ScreenLog
from api.metrics import metrics

log = logging.getLogger("api.screen.client")

# A healthy client logs a handful of lines a minute; a reconnect flushes its buffer (≤200).
BURST = 200
PER_SECOND = 1.0
# Buckets kept at most: one per user seen since start; a full table is rebuilt.
MAX_BUCKETS = 1024


@dataclass
class _Bucket:
    tokens: float
    at: float
    dropped: int = 0  # lines refused since the last accepted one
    client_dropped: int = 0  # lines the client said it dropped, not yet logged


_buckets: dict[str, _Bucket] = {}


def _quote(text: str) -> str:
    # One log record per client line: quotes, backslashes and controls become visible escapes.
    out = []
    for c in text:
        if c in '"\\':
            out.append("\\" + c)
        elif c.isprintable():
            out.append(c)
        else:
            out.append(repr(c)[1:-1])
    return '"' + "".join(out) + '"'


def _when(ts_ms: int) -> str:
    """The client's own time for a line (a buffered line arrives after an outage)."""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return f"ts={ts_ms}"


def accept(
    msg: ScreenLog,
    *,
    household: str,
    user_id: str,
    device: str,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Log the batch's lines the user's rate allows; returns how many were logged."""
    now = clock()
    bucket = _buckets.get(user_id)
    if bucket is None:
        if len(_buckets) >= MAX_BUCKETS:
            _buckets.clear()
        bucket = _buckets[user_id] = _Bucket(tokens=BURST, at=now)
    bucket.tokens = min(BURST, bucket.tokens + (now - bucket.at) * PER_SECOND)
    bucket.at = now
    bucket.client_dropped += msg.dropped or 0
    who = f"client {household}/{user_id} device {device[:8]}"
    logged = 0
    for line in msg.lines:
        # A notice costs a line too: free, it let "dropped" carry a log line per message (QA).
        if (bucket.dropped or bucket.client_dropped) and bucket.tokens >= 2:
            log.info(
                "%s: lines dropped before this: %d by the client, %d by the api rate limit",
                who, bucket.client_dropped, bucket.dropped,
            )  # fmt: skip
            bucket.tokens -= 1
            bucket.dropped = bucket.client_dropped = 0
        if bucket.tokens < 1:
            bucket.dropped += 1
            continue
        bucket.tokens -= 1
        log.info("%s: [%s] %s %s", who, line.level.value, _when(line.ts), _quote(line.message))
        logged += 1
    metrics.incr("screen.client_log_lines", logged)
    if logged < len(msg.lines):
        metrics.incr("screen.client_log_dropped", len(msg.lines) - logged)
    return logged
