"""Process-local metrics for latency tuning & resilience (master plan §11 Phase 7).

Hardening needs numbers: the master plan's latency budget (§6 — captions < ~1s,
labels as the turn finalizes, translations 1–2s, cues a few seconds) can only be
*tuned* if it is *measured*, and the resilience work (a model seam failing must
degrade gracefully, never crash a session) is only trustworthy if those failures
are counted instead of vanishing.

This is a tiny, dependency-free registry — counters plus latency summaries — so it
runs in the simulator/CI with no extra services. A real deployment can scrape the
``/metrics`` snapshot or swap this seam for Prometheus later; the call sites don't
change.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


class _Latency:
    """Running latency summary: count, total, max and a recent-sample window."""

    # Keep a bounded ring of recent samples so a snapshot can report percentiles
    # without unbounded memory growth over a long-lived process.
    _WINDOW = 256

    __slots__ = ("count", "total_ms", "max_ms", "_recent")

    def __init__(self) -> None:
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self._recent: list[float] = []

    def observe(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        if ms > self.max_ms:
            self.max_ms = ms
        self._recent.append(ms)
        if len(self._recent) > self._WINDOW:
            del self._recent[: len(self._recent) - self._WINDOW]

    def _percentile(self, pct: float) -> float:
        if not self._recent:
            return 0.0
        ordered = sorted(self._recent)
        # Nearest-rank percentile over the recent window.
        rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
        return ordered[rank]

    def snapshot(self) -> dict[str, float]:
        # count / avg_ms / max_ms are lifetime aggregates; the percentiles are over
        # the recent-sample window only (bounded memory). `window` makes that
        # explicit so p50/p95 and max aren't mistaken for the same time span.
        avg = self.total_ms / self.count if self.count else 0.0
        return {
            "count": self.count,
            "avg_ms": round(avg, 3),
            "max_ms": round(self.max_ms, 3),
            "window": len(self._recent),
            "p50_ms": round(self._percentile(50), 3),
            "p95_ms": round(self._percentile(95), 3),
        }


class Metrics:
    """Thread-safe counters + latency summaries, snapshotable as plain JSON."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, _Latency] = defaultdict(_Latency)
        self._lock = threading.Lock()

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def observe(self, name: str, ms: float) -> None:
        with self._lock:
            self._latencies[name].observe(ms)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Time a block and record it as a latency observation in milliseconds."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "latency_ms": {name: lat.snapshot() for name, lat in self._latencies.items()},
            }

    def reset(self) -> None:
        """Clear everything — used between tests; never on the live path."""
        with self._lock:
            self._counters.clear()
            self._latencies.clear()


# Process-wide registry. Like the other in-memory stores it is a singleton; a
# clustered deployment would scrape each api separately.
metrics = Metrics()
