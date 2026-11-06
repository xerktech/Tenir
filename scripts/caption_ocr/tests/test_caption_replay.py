"""The sampled-frame replay harness (XERK-2731) stores what the live socket path stores.

Its numbers decide merge tuning on real Teams captures, so it must replay through the same
tracker, parser and merger as ``/ws/screen``: the synthetic Teams fixture replayed from a
sampler-shaped directory must store exactly the lines ``test_replayed_teams_meeting_has_the_
right_speakers_and_no_duplicate_lines`` asserts the socket stores.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    rows = asyncio.run(cr.replay(platform, frames)).rows
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


@pytest.mark.parametrize(
    "rows",
    [
        [("Kunal", "Yes, I can see it."), ("Priya", "Yes.")],  # stock reply (QA D3)
        [("Ana", "Okay, I think we should ship it."), ("Bo", "I think we should.")],
        [("Ana", "Is it going out today or is it next week?"), ("Ana", "Is it today?")],
        [("Ana", "No."), ("Ana", "No, no, no.")],
        [("Ana", "okay so let's start"), ("Bo", "Okay, so let's start.")],  # another speaker
    ],
)
def test_short_or_other_speakers_rows_are_not_duplicates(rows: list) -> None:
    assert cr.duplicates(rows)[0] == 0


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


def test_a_reworded_row_repeated_far_ahead_does_not_cascade() -> None:
    # QA D2: the best match over all later rows paired row 0 with its exact repeat at row 21,
    # and the 20 rows between counted as lost and added.
    reference = [("Ana", "okay let's get started with the sprint review")]
    reference += [("Bo", f"item number {i} is done") for i in range(20)]
    reference += [("Ana", "okay let's get started with the sprint review now")]
    rows = [("Ana", reference[-1][1]), *reference[1:-1], reference[-1]]
    assert cr.compare(rows, reference) == {
        "matched": 22,
        "speaker_changed": 0,
        "missing": 0,
        "extra": 0,
    }


def test_compare_on_stock_phrases_with_drops_stays_in_step() -> None:
    rng = random.Random(7)
    reference = [
        ("Ana", "thank you") if i % 25 == 0 else ("Bo", f"line {i} about topic {i * 7}")
        for i in range(300)
    ]
    rows = [r for r in reference if rng.random() >= 0.1]
    lost = len(reference) - len(rows)
    rows[3] = (rows[3][0], rows[3][1] + " all")  # one reworded row
    got = cr.compare(rows, reference)
    assert got["missing"] <= lost + 1 and got["extra"] <= 1


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
    assert report["rows"] == n and report["duplicate_rows"] == 0  # "Thanks Kunal." x2: stock
    assert report["meetings"] == 1 and report["full_frames_without_pane"] == 1  # blank jpegs
    assert report["vs_truth"] == {"matched": n, "speaker_changed": 0, "missing": 0, "extra": 0}
    assert len(report["drop"]["runs"]) == 3
    assert set(report["drop"]["total"]) >= {"speaker_changed", "missing", "extra"}


def test_main_prints_a_readable_report(tmp_path: Path, capsys) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    assert cr.main([str(window), "--rows", "--drop", "0.2", "--runs", "1"]) == 0
    out = capsys.readouterr().out
    assert "duplicate_rows: 0" in out and "  Kunal Shah: Yes, I can see it." in out
    assert out.count("drop 20% x1") == 1


def test_a_gap_past_the_deployments_end_timer_ends_the_meeting(tmp_path: Path, monkeypatch) -> None:
    # The fixture's meeting, then again 100 s on: one meeting under the default 180 s end
    # timer, two under a deployment's 60 s (API_SCREEN_MEETING_END_SECONDS, QA D6).
    again = [dict(f, t=f["t"] + 100_000) for f in FIXTURE["frames"]]
    _, frames = cr.load_window(write_window(tmp_path, FIXTURE["frames"] + again))
    assert asyncio.run(cr.replay("teams", frames)).meetings == 1
    monkeypatch.setattr(cr.settings, "screen_meeting_end_seconds", 60.0)
    assert asyncio.run(cr.replay("teams", frames)).meetings == 2


# --- the Teams caption pane (QA D1) --------------------------------------------------------

sys.path.insert(0, str(_FIXTURE.parents[2]))
import test_screen_teams_pane as tp  # noqa: E402

HEADING_LINE = {"text": "Live Captions", "box": [1524, 308, 1650, 330]}


def _pane_window(root: Path, *, heading: bool) -> Path:
    """A rendered Teams window whose pane holds "Sounds Good", then caption crops of it."""
    window = root / "teams-pane-0123abcd"
    window.mkdir()
    full = [
        {"text": "Kunal Shah", "box": [400, 600, 520, 620]},
        {"text": "Priya Raman", "box": [800, 600, 930, 620]},
        {"text": "Sounds Good", "box": [1564, 452, 1700, 472]},  # a caption row in the pane
    ]
    (window / f"{BASE}-0-full.jpg").write_bytes(tp.render())
    data = {"width": tp.W, "height": tp.H, "lines": full + ([HEADING_LINE] if heading else [])}
    (window / f"{BASE}-0-full.json").write_text(json.dumps(data))
    crop = [
        {"text": "Kunal Shah", "box": [56, 4, 150, 20]},
        {"text": "Can you hear me now?", "box": [56, 26, 240, 46]},
        {"text": "Priya Raman", "box": [56, 60, 160, 76]},
        {"text": "Sounds Good", "box": [56, 82, 170, 102]},
    ]
    for n in range(1, 7):
        stem = f"{BASE + 1000 * n}-{n}-caption"
        (window / f"{stem}.jpg").write_bytes(_jpeg(760, 200))
        (window / f"{stem}.json").write_text(json.dumps({"lines": crop}))
    return window


STORED = [("Kunal Shah", "Can you hear me now?"), ("Priya Raman", "Sounds Good")]


def test_the_pane_is_found_from_the_full_frames_own_heading(tmp_path: Path) -> None:
    platform, frames = cr.load_window(_pane_window(tmp_path, heading=True))
    assert cr.find_pane(frames[0]) is not None
    got = asyncio.run(cr.replay(platform, frames))
    assert (got.rows, got.full_frames_without_pane) == (STORED, 0)


class _BandOcr:
    """tenir-ocr reading each candidate strip: the heading where the render drew it."""

    async def read(self, jpeg: bytes, *, full: bool = False, split_once: bool = False):
        return tp.heading()


def test_a_heading_the_full_ocr_missed_is_counted_and_read_with_ocr(tmp_path: Path) -> None:
    # Live reads the strip above the divider: the pane is found and "Sounds Good" stays a
    # caption. Without that read the replay loses it, and says so.
    platform, frames = cr.load_window(_pane_window(tmp_path, heading=False))
    without = asyncio.run(cr.replay(platform, frames))
    assert without.full_frames_without_pane == 1 and without.rows != STORED
    with_ocr = asyncio.run(cr.replay(platform, frames, ocr=_BandOcr()))
    assert (with_ocr.rows, with_ocr.full_frames_without_pane) == (STORED, 0)


# --- bad input (QA D5) ---------------------------------------------------------------------


def test_truth_in_the_fixtures_own_shape_is_read(tmp_path: Path) -> None:
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps(FIXTURE["expected"]["lines"]))
    assert cr.load_truth(truth)[0] == (
        "Raman, Priya",
        "Okay, let's get started with the sprint review.",
    )
    for bad in ('[{"who": "x"}]', '[["A"]]', '[["A", 5]]', '"ab"', "{not json"):
        truth.write_text(bad)
        with pytest.raises(SystemExit, match="speaker, text"):
            cr.load_truth(truth)


@pytest.mark.parametrize(
    "args", [["--drop", "1.5"], ["--drop", "1"], ["--drop", "-1"], ["--runs", "0"]]
)
def test_out_of_range_options_are_refused(tmp_path: Path, args: list[str]) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    with pytest.raises(SystemExit) as exc:
        cr.main([str(window), *args])
    assert exc.value.code == 2  # argparse refused it, not a later failure


def test_the_edges_of_the_option_ranges_are_accepted(tmp_path: Path, capsys) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    assert cr.main([str(window), "--drop", "0.99", "--runs", "1", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["drop"]["runs"]) == 1


def test_a_malformed_ocr_read_names_its_file(tmp_path: Path) -> None:
    window = write_window(tmp_path, FIXTURE["frames"][:2])
    bad = next(window.glob("*-1-caption.json"))
    bad.write_text("{not json")
    with pytest.raises(SystemExit, match=bad.name):
        cr.load_window(window)


def test_the_window_can_be_given_as_the_current_directory(tmp_path: Path, monkeypatch) -> None:
    window = write_window(tmp_path, FIXTURE["frames"])
    monkeypatch.chdir(window)
    assert cr.load_window(Path("."))[0] == "teams"


# --- compare stays in step (QA D7) ---------------------------------------------------------

LINES = [
    ("Ana" if i % 3 else "Bo", f"distinct line {i} about subject {i * 13} today") for i in range(80)
]
EXTRA = [("Cy", f"unrelated remark {k} on matter {k * 17} again") for k in range(20)]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (EXTRA[:16] + LINES, (80, 0, 16)),  # pre-roll the reference doesn't hold
        (LINES[:40] + EXTRA[:16] + LINES[40:], (80, 0, 16)),  # an extra block mid-meeting
        (LINES[:39] + EXTRA[:12] + LINES[40:], (79, 1, 12)),  # a hole, then a block
        (LINES[1:2] + LINES[:1] + LINES[2:], (80, 0, 0)),  # two rows stored swapped
    ],
)
def test_runs_of_unmatched_rows_never_throw_compare_out_of_step(rows, expected) -> None:
    got = cr.compare(rows, LINES)
    assert (got["matched"], got["missing"], got["extra"]) == expected


def test_a_truth_labelling_only_part_of_the_meeting_matches_that_part() -> None:
    got = cr.compare(LINES, LINES[40:60])
    assert (got["matched"], got["missing"], got["extra"]) == (20, 0, 60)


def test_blank_segments_of_dropped_lines_are_not_rows(tmp_path: Path, monkeypatch) -> None:
    from api.persistence import InMemoryConversationStore

    real = InMemoryConversationStore.add_segment

    def add_segment(self, household, conversation_id, seg):  # noqa: ANN001
        real(self, household, conversation_id, seg)
        blank = dataclasses.replace(seg, segment_id=seg.segment_id + "-x", text="")
        real(self, household, conversation_id, blank)

    monkeypatch.setattr(InMemoryConversationStore, "add_segment", add_segment)
    platform, frames = cr.load_window(write_window(tmp_path, FIXTURE["frames"]))
    rows = asyncio.run(cr.replay(platform, frames)).rows
    assert [{"speaker": s, "text": t} for s, t in rows] == FIXTURE["expected"]["lines"]


class _FailingOcr:
    async def read(self, jpeg: bytes, *, full: bool = False, split_once: bool = False):
        raise cr.OcrError("OCR request failed: 503")


def test_an_ocr_error_reading_the_pane_is_counted_and_replay_goes_on(tmp_path: Path) -> None:
    # As live (ws.read_pane): the frame parses without a pane (QA D8).
    platform, frames = cr.load_window(_pane_window(tmp_path, heading=False))
    got = asyncio.run(cr.replay(platform, frames, ocr=_FailingOcr()))
    assert (got.pane_ocr_errors, got.full_frames_without_pane) == (1, 1)


def test_the_same_words_from_two_speakers_one_dropped_change_no_speaker() -> None:
    t = "thank you very much"
    for reference in ([("B", t), ("A", t)], [("A", t), ("B", t)]):
        got = cr.compare([("B", t)], reference)
        assert (got["matched"], got["speaker_changed"], got["missing"]) == (1, 0, 1)


def test_a_misattributed_row_in_a_run_is_paired_and_counted() -> None:
    rows = list(LINES)
    rows[40] = ("Zed", rows[40][1])
    got = cr.compare(rows, LINES)
    assert (got["matched"], got["speaker_changed"], got["missing"]) == (80, 1, 0)


def test_a_row_exactly_at_the_match_ratio_is_paired() -> None:
    # 4 of 5 words alike: ratio 2 * 4 / 10 = MATCH_RATIO.
    got = cr.compare([("A", "one two three four five")], [("A", "one two three four six")])
    assert got["matched"] == 1
    got = cr.compare([("A", "one two three four five")], [("A", "one two three six seven")])
    assert got["matched"] == 0


@pytest.mark.parametrize("seed", range(40))
def test_dropped_rows_of_a_meeting_with_stock_replies_are_counted_exactly(seed: int) -> None:
    # Speakers trading stock replies ("thank you", "yes we can") between distinct lines:
    # dropping rows only must count exactly the dropped rows missing, no speaker change and
    # no row paired twice (QA mutants: greedy pairing, bisect_right, a reused row).
    rng = random.Random(seed)
    stock = ["thank you very much", "yes we can do that", "okay sounds good to me"]
    reference = [
        (rng.choice("ABC"), rng.choice(stock) if rng.random() < 0.4 else f"point {i} on {i * 7}")
        for i in range(200)
    ]
    rows = [r for r in reference if rng.random() >= 0.2]
    got = cr.compare(rows, reference)
    assert got == {
        "matched": len(rows),
        "speaker_changed": 0,
        "missing": len(reference) - len(rows),
        "extra": 0,
    }


@pytest.mark.parametrize(
    ("rows", "reference", "expected"),
    [
        # Real misattribution, rows verbatim and in order: counted, never hidden (QA D10).
        (
            [("A", "okay thanks"), ("B", "okay thanks everyone")],
            [("B", "okay thanks"), ("A", "okay thanks everyone")],
            (2, 2, 0),
        ),
        (
            [("A", "yes okay yes"), ("A", "yes okay"), ("B", "yes sure okay")],
            [("B", "yes okay yes"), ("A", "yes okay"), ("A", "yes sure okay")],
            (3, 2, 0),
        ),
        ([("A", "a b c")], [("A", "a b")], (1, 0, 0)),  # lengths exactly at the bar: 4 / 5
    ],
)
def test_compare_counts_real_misattribution_and_the_length_bar(rows, reference, expected) -> None:
    got = cr.compare(rows, reference)
    assert (got["matched"], got["speaker_changed"], got["missing"]) == expected


@pytest.mark.parametrize("seed", range(30))
def test_misattributed_rows_are_counted_exactly(seed: int) -> None:
    # Known misattribution over stock replies, no drops: every flipped row counted, no more.
    rng = random.Random(seed)
    stock = ["thank you very much", "yes we can do that", "okay sounds good to me"]
    reference = [
        (rng.choice("ABC"), rng.choice(stock) if rng.random() < 0.4 else f"point {i} on {i * 7}")
        for i in range(200)
    ]
    flipped = 0
    rows = []
    for speaker, text in reference:
        if rng.random() < 0.2:
            speaker, flipped = speaker + "x", flipped + 1
        rows.append((speaker, text))
    got = cr.compare(rows, reference)
    assert (got["matched"], got["speaker_changed"], got["missing"]) == (200, flipped, 0)


def test_a_row_stored_out_of_order_takes_its_own_speakers_alike_row() -> None:
    # The anchor pairs first, so the late row is a leftover: of the two alike rows near it, its
    # own speaker's (ratio 0.8) beats another speaker's exact one.
    t = "we ship the build on friday"
    reference = [("P", "an anchor line nobody else says"), ("A", t)]
    rows = [("B", t), ("A", "we ship the build on monday"), reference[0]]
    got = cr.compare(rows, reference)
    assert (got["matched"], got["speaker_changed"]) == (2, 0)


def test_a_row_stored_a_few_rows_late_is_still_paired() -> None:
    rows = LINES[1:4] + LINES[:1] + LINES[4:]
    assert cr.compare(rows, LINES)["matched"] == 80


def test_a_row_stored_far_out_of_order_is_lost_and_extra() -> None:
    late = ("Ana", "a special line said once at the start")
    got = cr.compare(LINES + [late], [late, *LINES])
    assert (got["matched"], got["missing"], got["extra"]) == (80, 1, 1)


def test_equally_long_chains_keep_the_own_speaker_pairs_across_prefix_ranges() -> None:
    # Two 3-pair chains: A, B, C in place, or C's late copies of all three. The prefix query
    # must compare speaker matches too, or it keeps whichever range it visits first (QA M50).
    reference = [("A", "alpha one"), ("B", "bravo two"), ("C", "charlie three")]
    rows = [
        ("A", "alpha one"),
        ("B", "bravo two"),
        ("D", "filler words here"),
        ("D", "more filler words"),
        ("C", "alpha one"),
        ("C", "bravo two"),
        ("C", "charlie three"),
    ]
    got = cr.compare(rows, reference)
    assert (got["matched"], got["speaker_changed"]) == (3, 0)
