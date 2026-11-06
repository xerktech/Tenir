"""XERK-2705: opt-in sampling of real screen frames for re-tuning the caption parsers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.contract import ScreenFrameHeader
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


def test_replayed_frames_are_sampled_only_when_a_directory_is_set(
    screen, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from api.main import app

    _, ocr, token, _ = screen
    frames = FIXTURE["frames"][:6]
    base = int(time.time() * 1000)
    # Off by default: nothing is written anywhere.
    assert settings.screen_sample_dir == ""
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        start = replay(client, ws, ocr, frames, base)
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(settings, "screen_sample_dir", str(tmp_path))
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        replay(client, ws, ocr, frames, base, start=start)
    (d,) = tmp_path.iterdir()
    assert d.name == "zoom-tesoro"
    jpgs = sorted(d.glob("*.jpg"))
    assert len(jpgs) == len(frames) == len(list(d.glob("*.json")))
    regions = sorted(json.loads(p.with_suffix(".json").read_text())["region"] for p in jpgs)
    assert regions == sorted(f["region"] for f in frames)
    meta = json.loads(jpgs[0].with_suffix(".json").read_text())
    assert meta["platform"] == "zoom" and meta["orgSiteKey"] == "tesoro"
    assert all(set(ln) == {"text", "box", "score"} for ln in meta["lines"])
    assert jpgs[0].read_bytes()[:2] == b"\xff\xd8"


def test_a_client_supplied_org_key_never_picks_the_path(tmp_path: Path) -> None:
    root = tmp_path / "s"
    assert samples.save(str(root), 10, _header("../../etc"), b"\xff\xd8x", _RESULT, 5)
    (d,) = root.iterdir()
    assert d.name == "zoom-______etc" and d.parent == root
    (meta,) = d.glob("*.json")
    assert json.loads(meta.read_text())["lines"][0]["text"] == "Bo Chen"


def test_sampling_stops_at_the_cap(tmp_path: Path) -> None:
    saved = [samples.save(str(tmp_path), 3, _header(), b"j", _RESULT, t) for t in range(5)]
    assert saved == [True, True, True, False, False]
    assert len(list((tmp_path / "zoom-tesoro").glob("*.jpg"))) == 3


def test_an_unwritable_directory_never_breaks_the_frame(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    assert samples.save(str(blocker), 3, _header(), b"j", _RESULT, 1) is False
