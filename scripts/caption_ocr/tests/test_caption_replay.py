"""The sampled-frame replay harness (XERK-2731) stores what the live socket path stores.

Its numbers decide merge tuning on real Teams captures, so it must replay through the same
tracker, parser and merger as ``/ws/screen``: the synthetic Teams fixture replayed from a
sampler-shaped directory must store exactly the lines ``test_replayed_teams_meeting_has_the_
right_speakers_and_no_duplicate_lines`` asserts the socket stores.
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import caption_replay as cr  # noqa: E402

_FIXTURE = Path(__file__).resolve().parents[3] / "api/tests/fixtures/screen/teams_meeting.json"
FIXTURE = json.loads(_FIXTURE.read_text())
BASE = 1_700_000_000_000


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (32, 32, 32)).save(buf, format="JPEG")
    return buf.getvalue()


def write_window(root: Path, frames: list[dict], *, sized: bool = True) -> Path:
    """``frames`` as a sampler window directory: ``<ts>-<n>-<region>.jpg`` + ``.json``."""
    window = root / "teams-replay-0123abcd"
    window.mkdir()
    for n, f in enumerate(frames):
        stem = f"{BASE + f['t']}-{n}-{f['region']}"
        (window / f"{stem}.jpg").write_bytes(_jpeg(f["width"], f["height"]))
        data = {"lines": f["lines"]}
        if sized:
            data |= {"width": f["width"], "height": f["height"]}
        (window / f"{stem}.json").write_text(json.dumps(data))
    return window


@pytest.mark.parametrize("sized", [True, False])
def test_replaying_the_teams_fixture_stores_what_the_socket_stores(
    tmp_path: Path, sized: bool
) -> None:
    platform, frames = cr.load_window(write_window(tmp_path, FIXTURE["frames"], sized=sized))
    assert platform == "teams" and len(frames) == len(FIXTURE["frames"])
    rows = asyncio.run(cr.replay(platform, frames))
    assert [{"speaker": s, "text": t} for s, t in rows] == FIXTURE["expected"]["lines"]


def test_frames_replay_in_ts_order_and_unpaired_files_are_skipped(tmp_path: Path) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    (window / "notes.txt").write_text("x")
    (window / f"{BASE}-99-caption.jpg").write_bytes(b"")  # no .json beside it
    _, frames = cr.load_window(window)
    assert [f.ts for f in frames] == sorted(BASE + f["t"] for f in FIXTURE["frames"])


def test_a_directory_not_named_for_a_platform_is_refused(tmp_path: Path) -> None:
    (tmp_path / "webex-x-1").mkdir()
    with pytest.raises(SystemExit, match="sampler directory"):
        cr.load_window(tmp_path / "webex-x-1")


def test_a_rewritten_entry_stored_twice_counts_as_a_duplicate() -> None:
    rows = [
        ("Ana", "okay so let's start"),
        ("Bo", "sounds good"),
        ("Ana", "Okay, so let's start."),  # the rewrite stored again
        ("Ana", "next item please"),
    ]
    assert cr.duplicates(rows) == (1, 4, 13)


def test_a_repeat_beyond_the_window_is_not_a_duplicate() -> None:
    rows = [("Ana", "thanks everyone")] + [("Bo", f"line {i}") for i in range(cr.DUP_WINDOW)]
    assert cr.duplicates(rows + [("Ana", "thanks everyone")])[0] == 0


def test_compare_counts_a_speaker_change_and_lost_and_added_rows() -> None:
    reference = [("Ana", "okay let's start"), ("Bo", "yes we can"), ("Ana", "ship it today")]
    rows = [("Ana", "okay let's start"), ("Ana", "yes we can"), ("Cy", "something else")]
    assert cr.compare(rows, reference) == {
        "matched": 2,
        "speaker_changed": 1,
        "missing": 1,
        "extra": 1,
    }


def test_drop_keeps_every_full_frame_and_is_seeded(tmp_path: Path) -> None:
    _, frames = cr.load_window(write_window(tmp_path, FIXTURE["frames"]))
    a = cr.drop(frames, 0.5, random.Random(1))
    assert a == cr.drop(frames, 0.5, random.Random(1))
    assert [f for f in frames if f.region == "full"] == [f for f in a if f.region == "full"]
    assert len(a) < len(frames)


def test_main_reports_drops_and_truth_as_json(tmp_path: Path, capsys) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps([[x["speaker"], x["text"]] for x in FIXTURE["expected"]["lines"]]))
    argv = [str(window), "--drop", "0.3", "--runs", "3", "--truth", str(truth), "--json"]
    assert cr.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    n = len(FIXTURE["expected"]["lines"])
    assert report["rows"] == n and report["duplicate_rows"] == 1  # "Thanks Kunal." said twice
    assert report["vs_truth"] == {"matched": n, "speaker_changed": 0, "missing": 0, "extra": 0}
    assert len(report["drop"]["runs"]) == 3
    assert set(report["drop"]["total"]) >= {"speaker_changed", "missing", "extra"}


def test_main_prints_a_readable_report(tmp_path: Path, capsys) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    assert cr.main([str(window), "--rows", "--drop", "0.2", "--runs", "1"]) == 0
    out = capsys.readouterr().out
    assert "duplicate_rows: 1" in out and "  Kunal Shah: Yes, I can see it." in out
    assert out.count("drop 20% x1") == 1
