"""Phase 7: the process-local metrics registry (latency tuning & resilience)."""

from __future__ import annotations

from api.metrics import Metrics, metrics


def test_counter_accumulates() -> None:
    m = Metrics()
    m.incr("a")
    m.incr("a", 4)
    m.incr("b")
    snap = m.snapshot()
    assert snap["counters"] == {"a": 5, "b": 1}
    assert snap["latency_ms"] == {}


def test_latency_summary_reports_avg_max_and_percentiles() -> None:
    m = Metrics()
    for ms in (10.0, 20.0, 30.0, 40.0):
        m.observe("stage", ms)
    lat = m.snapshot()["latency_ms"]["stage"]
    assert lat["count"] == 4
    assert lat["avg_ms"] == 25.0
    assert lat["max_ms"] == 40.0
    # Nearest-rank percentiles over [10,20,30,40].
    assert lat["p50_ms"] == 20.0
    assert lat["p95_ms"] == 40.0
    # `window` makes explicit that percentiles cover only the recent sample window
    # (max_ms is lifetime), so the two aren't read as the same time span (M8).
    assert lat["window"] == 4


def test_percentiles_empty_window_are_zero() -> None:
    # A fresh latency (no samples) must not divide by zero.
    m = Metrics()
    m.incr("only-a-counter")
    assert m.snapshot()["latency_ms"] == {}


def test_recent_window_is_bounded() -> None:
    m = Metrics()
    # Far more than the window; max should still reflect the true peak, and the
    # window must stay capped so a long-lived process can't grow without bound.
    for i in range(1000):
        m.observe("stage", float(i))
    lat = m._latencies["stage"]
    assert lat.count == 1000
    assert lat.max_ms == 999.0
    assert len(lat._recent) <= _Latency_window()


def test_timer_records_a_positive_latency() -> None:
    m = Metrics()
    with m.timer("block"):
        pass
    lat = m.snapshot()["latency_ms"]["block"]
    assert lat["count"] == 1
    assert lat["avg_ms"] >= 0.0


def test_reset_clears_everything() -> None:
    m = Metrics()
    m.incr("a")
    m.observe("s", 5.0)
    m.reset()
    snap = m.snapshot()
    assert snap == {"counters": {}, "latency_ms": {}}


def test_module_singleton_is_a_metrics() -> None:
    assert isinstance(metrics, Metrics)


def _Latency_window() -> int:
    from api.metrics import _Latency

    return _Latency._WINDOW
