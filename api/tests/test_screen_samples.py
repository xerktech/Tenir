"""XERK-2705: opt-in sampling of real screen frames for re-tuning the caption parsers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.contract import ScreenFrameHeader
from api.metrics import metrics
from api.screen import samples
from api.screen.ocr import OcrLine, OcrResult
from test_screen import FIXTURE, replay, screen  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _fresh_count():
    samples._reset_for_tests()
    yield
    samples._reset_for_tests()


def _header(org: str = "tesoro", region: str = "caption") -> ScreenFrameHeader:
    return ScreenFrameHeader.model_validate(
        {
            "type": "screen.frame",
            "windowTitle": "w",
            "orgSiteKey": org,
            "orgName": "Tesoro",
            "platform": "zoom",
            "ts": 1,
            "region": region,
        }
    )


_RESULT = OcrResult(200, 40, (OcrLine("Bo Chen", (10.0, 2.0, 80.0, 18.0), 0.9),))


def _wait_idle(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while samples._busy:
        assert time.monotonic() < deadline, "sample write never finished"
        time.sleep(0.01)


def test_replayed_frames_are_sampled_only_when_a_directory_is_set(
    screen,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.main import app

    _, ocr, token, _ = screen
    # Run from tmp_path: an unguarded sampler would write under the relative "" root here.
    monkeypatch.chdir(tmp_path)
    frames = FIXTURE["frames"][:6]
    base = int(time.time() * 1000)
    assert settings.screen_sample_dir == ""
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        start = replay(client, ws, ocr, frames, base)
    _wait_idle()
    assert list(tmp_path.iterdir()) == []

    root = tmp_path / "samples"
    monkeypatch.setattr(settings, "screen_sample_dir", str(root))
    threads: list[str] = []
    real_save = samples.save

    def spy(*args):
        threads.append(threading.current_thread().name)
        return real_save(*args)

    monkeypatch.setattr(samples, "save", spy)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        for i, frame in enumerate(frames, start=start):
            replay(client, ws, ocr, [frame], base, start=i)
            _wait_idle()
    # Written on the sampler's own thread, never the event loop or the shared executor.
    assert threads and all(t.startswith("screen-sample") for t in threads)
    (d,) = root.iterdir()
    assert d.name.startswith("zoom-tesoro-")
    jpgs = sorted(d.glob("*.jpg"))
    assert len(jpgs) == len(frames) == len(list(d.glob("*.json")))
    regions = sorted(json.loads(p.with_suffix(".json").read_text())["region"] for p in jpgs)
    assert regions == sorted(f["region"] for f in frames)
    meta = json.loads(jpgs[0].with_suffix(".json").read_text())
    assert meta["platform"] == "zoom" and meta["orgSiteKey"] == "tesoro"
    assert all(set(ln) == {"text", "box", "score"} for ln in meta["lines"])
    assert jpgs[0].read_bytes()[:2] == b"\xff\xd8"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["samples"]


def test_a_hung_write_never_holds_up_a_frame_and_later_frames_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA D1: a stalled volume once stalled the window's lane and the shared executor."""
    release = threading.Event()
    calls: list[int] = []

    def hang(*args):
        calls.append(args[-1])
        release.wait(5)
        return True

    monkeypatch.setattr(samples, "save", hang)
    metrics.reset()
    t0 = time.monotonic()
    assert samples.submit(str(tmp_path), 10, _header(), b"j", _RESULT, 1) is True
    assert samples.submit(str(tmp_path), 10, _header(), b"j", _RESULT, 2) is False
    assert time.monotonic() - t0 < 1.0
    assert metrics.snapshot()["counters"]["screen.samples_skipped"] == 1
    release.set()
    _wait_idle()
    assert samples.submit(str(tmp_path), 10, _header(), b"j", _RESULT, 3) is True
    _wait_idle()
    assert calls == [1, 3]


def test_a_client_supplied_org_key_never_picks_the_path(tmp_path: Path) -> None:
    root = tmp_path / "s"
    for key in ("../../etc", "a/b", "a\\b", "a\x00b"):
        assert samples.save(str(root), 10, _header(key), b"\xff\xd8x", _RESULT, 5)
    dirs = sorted(root.iterdir())
    # Each key its own directory, all directly under the root.
    assert len(dirs) == 4 and all(d.parent == root for d in dirs)
    assert not (tmp_path / "etc").exists()
    meta = next(d for d in dirs if d.name.startswith("zoom-______etc-")).glob("*.json")
    assert json.loads(next(meta).read_text())["lines"][0]["text"] == "Bo Chen"


def test_sampling_stops_at_the_frame_cap_and_only_writes_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = [samples.save(str(tmp_path), 3, _header(), b"j", _RESULT, t) for t in range(5)]
    assert saved == [True, True, True, False, False]
    samples._reset_for_tests()
    # QA D2/D3: a failed json write leaves no lone jpg and uses no slot.
    real = Path.write_text

    def full(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", full)
    other = tmp_path / "other"
    assert samples.save(str(other), 1, _header(), b"j", _RESULT, 9) is False
    assert list(other.rglob("*.*")) == []
    monkeypatch.setattr(Path, "write_text", real)
    assert samples.save(str(other), 1, _header(), b"j", _RESULT, 10) is True


def test_sampling_stops_at_the_byte_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(samples, "MAX_BYTES", 10)
    saved = [samples.save(str(tmp_path), 99, _header(), b"x" * 6, _RESULT, t) for t in range(3)]
    assert saved == [True, True, False]
    assert samples.submit(str(tmp_path), 99, _header(), b"x", _RESULT, 7) is False


def test_a_nan_ocr_score_is_written_as_valid_json(tmp_path: Path) -> None:
    result = OcrResult(10, 10, (OcrLine("hi", (0.0, 0.0, 5.0, 5.0), float("nan")),))
    assert samples.save(str(tmp_path), 9, _header(), b"j", result, 1)
    (meta,) = tmp_path.rglob("*.json")
    assert json.loads(meta.read_text(), parse_constant=pytest.fail)["lines"][0]["score"] is None


def test_an_unwritable_directory_never_breaks_the_frame(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    assert samples.save(str(blocker), 3, _header(), b"j", _RESULT, 1) is False


def test_a_frame_whose_account_went_while_ocr_ran_is_not_sampled(
    screen,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA D8: the account deleted mid-OCR closes the socket; its frame must not be written."""
    from api.main import app
    from api.screen import ws as screen_ws

    _, ocr, token, admin = screen
    monkeypatch.setattr(settings, "screen_sample_dir", str(tmp_path))
    real_read = ocr.read

    async def revoked_mid_read(jpeg, **kw):
        screen_ws.revoke_user(admin.user_id)
        return await real_read(jpeg, **kw)

    monkeypatch.setattr(ocr, "read", revoked_mid_read)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        replay(client, ws, ocr, FIXTURE["frames"][:1], int(time.time() * 1000))
    _wait_idle()
    assert list(tmp_path.iterdir()) == []


def test_sampling_stops_after_repeated_failed_writes(tmp_path: Path) -> None:
    """QA: a broken directory logged a warning on every frame for the whole meeting."""
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    assert samples.FAIL_LIMIT == 5
    # A success in between resets the count: 4 failures, a write, 4 more keep sampling on.
    for t in range(4):
        assert samples.save(str(blocker), 99, _header(), b"j", _RESULT, t) is False
    assert samples.save(str(tmp_path / "ok"), 99, _header(), b"j", _RESULT, 1) is True
    for t in range(4):
        assert samples.save(str(blocker), 99, _header(), b"j", _RESULT, t) is False
    assert samples.save(str(tmp_path / "ok"), 99, _header(), b"j", _RESULT, 2) is True
    for t in range(5):
        assert samples.save(str(blocker), 99, _header(), b"j", _RESULT, t) is False
    assert samples.submit(str(tmp_path), 99, _header(), b"j", _RESULT, 1) is False
    assert samples.save(str(tmp_path), 99, _header(), b"j", _RESULT, 1) is False


def test_a_stalled_write_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert samples.STUCK_SECONDS == 30.0
    monkeypatch.setattr(samples, "STUCK_SECONDS", 0.3)

    def stalled() -> int:
        return sum("stalled" in r.message for r in caplog.records)

    with caplog.at_level("WARNING", logger="api.screen.samples"):
        for stall in (1, 2):  # each stall in a process is reported
            release = threading.Event()
            monkeypatch.setattr(samples, "save", lambda *a, ev=release: ev.wait(5))
            assert samples.submit(str(tmp_path), 9, _header(), b"j", _RESULT, 1)
            # Not before STUCK_SECONDS has passed.
            assert samples.submit(str(tmp_path), 9, _header(), b"j", _RESULT, 2) is False
            assert stalled() == stall - 1
            time.sleep(0.35)
            for t in range(3):
                assert samples.submit(str(tmp_path), 9, _header(), b"j", _RESULT, t) is False
            assert stalled() == stall
            release.set()
            _wait_idle()


def test_a_restart_never_reuses_a_samples_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA: the frame number restarts per process; a failed write once deleted an older pair."""
    assert samples.save(str(tmp_path), 9, _header(), b"j", _RESULT, 7)
    samples._reset_for_tests()
    monkeypatch.setattr(samples, "_RUN", "beef00")  # the next process
    nan = OcrResult(10, 10, (OcrLine("x", (0.0, float("nan"), 1.0, 1.0)),))
    assert samples.save(str(tmp_path), 9, _header(), b"j", nan, 7) is False
    assert len(list(tmp_path.rglob("*.jpg"))) == len(list(tmp_path.rglob("*.json"))) == 1
