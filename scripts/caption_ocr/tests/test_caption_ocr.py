"""Unit tests for the caption-OCR spike harness (python -m pytest scripts/caption_ocr/tests)."""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import caption_bench as bench  # noqa: E402
from caption_bench import crop_band, crop_box, find_caption_box, group_lines, upscale_to  # noqa: E402
from capture_windows import frame_stats, is_good, resolve  # noqa: E402
from caption_score import aggregate, char_accuracy, score_frame, split_speaker  # noqa: E402
from caption_synth import _font, generate, make_frame  # noqa: E402


class TestSplitSpeaker:
    def test_name_and_text(self):
        assert split_speaker("Sean O'Neil: ship it  today") == ("Sean O'Neil", "ship it today")

    def test_no_label(self):
        assert split_speaker("just words here") == (None, "just words here")

    def test_colon_inside_text_keeps_first_label(self):
        assert split_speaker("Omar Ahmed: action item: fix CI") == (
            "Omar Ahmed",
            "action item: fix CI",
        )

    def test_non_ascii_first_letter(self):
        assert split_speaker("Élodie Martin: hi") == ("Élodie Martin", "hi")


class TestScoring:
    def test_char_accuracy_bounds(self):
        assert char_accuracy("abc", "abc") == 1.0
        assert char_accuracy("abc", "xyzxyzxyz") == 0.0
        assert char_accuracy("", "") == 1.0

    def test_frame_pairs_lines_and_scores_speaker(self):
        truth = ["Priya Raman: open a ticket", "J. Smith: on it"]
        ocr = ["J. Smith: on it", "Priya Ramen: open a ticket"]  # one-letter name error
        s = score_frame(truth, ocr)
        assert s.lines == 2 and s.speakers == 2
        assert s.speakers_matched == 1  # a one-letter name error is a wrong name
        assert s.lines_matched == 2  # 1 char off in 26 still clears the 0.95 line bar

    def test_speaker_ignores_case_only(self):
        assert score_frame(["Omar Ahmed: hi"], ["omar ahmed: hi"]).speakers_matched == 1

    def test_missing_ocr_scores_zero(self):
        s = score_frame(["Dana: hi there"], [])
        assert s.lines_matched == 0 and s.speakers_matched == 0

    def test_one_ocr_line_cannot_satisfy_two_truth_lines(self):
        assert score_frame(["A: same words"] * 2, ["A: same words"]).lines_matched == 1

    def test_extra_lines_lower_precision(self):
        r = aggregate(
            [score_frame(["A: x"], ["A: x", "garbage", "Omar Ahmed Dana McAllister"])], []
        )
        assert r["line_acc"] == 1.0 and r["line_precision"] == pytest.approx(1 / 3)

    def test_text_on_a_frame_without_captions_is_all_extra(self):
        r = aggregate([score_frame([], ["Chris Jr."]), score_frame(["A: x"], ["A: x"])], [])
        assert r["line_precision"] == 0.5

    def test_aggregate(self):
        r = aggregate([score_frame(["A: x"], ["A: x"]), score_frame(["B: y"], ["C: y"])], [10, 30])
        assert r["line_acc"] == 0.5 and r["speaker_acc"] == 0.5
        assert r["lat_p50_ms"] == 20 and r["lat_p95_ms"] == 30

    def test_no_caption_set_is_not_scored_zero(self):
        r = aggregate([score_frame([], [])], [])
        assert math.isnan(r["line_acc"]) and math.isnan(r["speaker_acc"])
        assert math.isnan(r["char_acc"]) and math.isnan(r["lat_p50_ms"])
        assert math.isnan(r["lat_p95_ms"])
        assert r["line_precision"] == 1.0

    def test_p95_is_nearest_rank_not_max(self):
        assert aggregate([], list(range(1, 21)))["lat_p95_ms"] == 19


class TestGroupLines:
    def test_merges_split_boxes_on_one_line_left_to_right(self):
        boxes = [(10, 30, 200, "open a ticket"), (12, 31, 5, "Priya:"), (50, 70, 5, "Omar: ok")]
        assert group_lines(boxes) == ["Priya: open a ticket", "Omar: ok"]

    def test_slight_vertical_overlap_stays_separate(self):
        # adjacent lines whose boxes touch by a few px are still two lines
        assert group_lines([(0, 22, 0, "one"), (19, 41, 0, "two")]) == ["one", "two"]


class TestCrops:
    def test_band_keeps_lower_part(self):
        assert crop_band(Image.new("RGB", (100, 1000))).size == (100, 350)

    def test_upscale_only_small_frames(self):
        img = Image.new("RGB", (100, 50))
        assert upscale_to(img, 540).size == (200, 100)
        assert upscale_to(img, 1080).size == (100, 50)


class TestCaptionBox:
    # several tile layouts; on seeds 0 and 7 the crop overshoots into video without the top trim
    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 11])
    @pytest.mark.parametrize("dark", [False, True])
    @pytest.mark.parametrize("scale", [1.0, 0.75, 0.5])
    def test_box_holds_the_caption_text_and_little_else(self, scale, dark, seed):
        f = make_frame(random.Random(seed), 0, scale, 60, dark=dark)
        band = upscale_to(crop_band(f.image), f.image.height)
        box = find_caption_box(band)
        assert box is not None
        # the drawn caption box, in band pixels; its text sits >= 14 px inside it
        k = band.width / f.image.width
        top0 = int(f.image.height * bench.BAND_TOP)
        bl, bt, br, bb = (f.box[0] * k, (f.box[1] - top0) * k, f.box[2] * k, (f.box[3] - top0) * k)
        assert box[0] <= bl + 14 * k and box[2] >= br - 14 * k  # every glyph column kept
        assert box[1] <= bt + 18 * k and box[3] >= bb - 14 * k  # every caption line kept
        # tight: BOX_PAD plus anti-aliasing, never strips of video beside the box
        assert box[2] - box[0] <= br - bl + 32 and box[3] - box[1] <= bb - bt + 24
        crop = crop_box(f.image)
        assert crop is not None and crop.size == (box[2] - box[0], box[3] - box[1])

    @pytest.mark.parametrize("seed", range(4))
    @pytest.mark.parametrize("dark", [False, True])
    def test_no_caption_no_box(self, dark, seed):
        f = make_frame(random.Random(seed), 0, 1.0, 75, dark=dark, caption=False)
        assert crop_box(f.image) is None

    @pytest.mark.parametrize("scale", [1.0, 0.75, 0.5])
    def test_box_over_white_video_is_trimmed_to_the_box(self, scale):
        # the box edge over bright video lights the glyph mask; the crop must not grow into it
        img = Image.new("RGB", (1920, 1080), (240, 240, 240)).convert("RGBA")
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        lines = ["Priya Raman: open a ticket for the login timeout", "Omar Ahmed: on it"]
        font = _font(24)
        bw = max(font.getlength(t) for t in lines) + 28
        top, bottom = 880, 880 + 2 * 28 + 8 + 28
        d.rounded_rectangle(
            [(1920 - bw) / 2, top, (1920 + bw) / 2, bottom], 10, fill=(0, 0, 0, 185)
        )
        for i, t in enumerate(lines):
            d.text(((1920 - bw) / 2 + 14, top + 14 + i * 36), t, fill="white", font=font)
        img = Image.alpha_composite(img, over).convert("RGB")
        img = img.resize((int(1920 * scale), int(1080 * scale)), Image.Resampling.BILINEAR)
        band = upscale_to(crop_band(img), img.height)
        box = find_caption_box(band)
        k = band.width / 1920  # band px per px of the 1080p drawing
        band_top = int(img.height * bench.BAND_TOP) / scale  # band's top, in drawing px
        assert box is not None
        assert abs(box[1] - (top - band_top) * k) < 24  # top edge at the drawn box, +- padding
        assert box[3] - box[1] <= (bottom - top) * k + 24

    @pytest.mark.parametrize("size", [18, 24, 32])
    @pytest.mark.parametrize("scale", [1.0, 0.75, 0.5])
    def test_white_slide_text_is_not_a_caption(self, size, scale):
        # screen share: dark text centred on white, where a caption would sit; no caption
        img = Image.new("RGB", (1920, 1080), (255, 255, 255))
        d = ImageDraw.Draw(img)
        for j, t in enumerate(["Q3 roadmap: ship the OCR spike", "Owner: Priya Raman"]):
            d.text((960, 760 + j * 40), t, fill=(0, 0, 0), font=_font(size), anchor="mm")
        img = img.resize((int(1920 * scale), int(1080 * scale)), Image.Resampling.BILINEAR)
        assert crop_box(img) is None

    @pytest.mark.parametrize("scale", [1.0, 0.5])
    def test_centred_camera_off_name_is_not_a_caption(self, scale):
        # a camera-off tile centred under the window centre puts a large white name in the band
        img = Image.new("RGB", (1920, 1080), (35, 35, 35))
        ImageDraw.Draw(img).text(
            (960, 790), "Kateryna Shevchenko", fill="white", font=_font(40), anchor="mm"
        )
        img = img.resize((int(1920 * scale), int(1080 * scale)), Image.Resampling.BILINEAR)
        assert crop_box(img) is None

    def test_blurred_caption_over_bright_video_is_found(self):
        # a 0.5x frame whose box fill sits between grey 80 and 90 after blur and JPEG; the fill
        # bar (FILL_DARK) must stay loose enough to keep it
        rng = random.Random(137)
        frames = [
            make_frame(rng, 137, scale, rng.choice([60, 75, 85]), dark=dark, caption=cap)
            for scale in (1.0, 0.75, 0.5)
            for dark in (False, True)
            for cap in (True, False)
        ]
        f = frames[8]  # scale 0.5, video, captioned
        assert f.name.startswith("synth_137_s0.5_") and f.name.endswith("_video") and f.lines
        assert crop_box(f.image) is not None

    @pytest.mark.parametrize("seed", range(3))
    def test_random_light_slides_are_not_captions(self, seed):
        # white and coloured screen-share slides: dark text, centred or left, 1-4 lines
        rng = random.Random(seed)
        hits = []
        for i in range(30):
            img = _slide(rng, ["white", "colour"][i % 2])
            for scale in (0.75, 0.5):
                small = img.resize(
                    (int(1920 * scale), int(1080 * scale)), Image.Resampling.BILINEAR
                )
                if crop_box(small) is not None:
                    hits.append((i, scale))
        assert hits == []

    @pytest.mark.parametrize("grey", [60, 160, 240])
    @pytest.mark.parametrize("second", ["ok so", "and then we"])
    def test_short_last_line_is_kept_over_any_video(self, grey, second):
        # a last line too short to reach the centre column, over dark to white video
        font = _font(24)
        first = "Priya Raman: so the answer is that"
        bw, line_h = font.getlength(first) + 28, font.getbbox("Ag")[3]
        top = 960 - (28 + 36 + line_h)
        img = Image.new("RGB", (1920, 1080), (grey,) * 3).convert("RGBA")
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        x = (1920 - bw) / 2
        d.rounded_rectangle([x, top, x + bw, 960], 10, fill=(0, 0, 0, 185))
        d.text((x + 14, top + 14), first, fill="white", font=font)
        d.text((x + 14, top + 50), second, fill="white", font=font)
        band = crop_band(Image.alpha_composite(img, over).convert("RGB"))
        box = find_caption_box(band)
        last_bottom = top + 50 + line_h - int(1080 * bench.BAND_TOP)
        assert box is not None and box[3] >= last_bottom - 4

    @pytest.mark.parametrize("grey", [60, 200])
    @pytest.mark.parametrize(
        "lines",
        [("ok so", "Priya Raman: so the answer is"), ("Priya Raman: so the answer is", "mm")],
    )
    def test_growth_reaches_short_lines_and_stops_at_the_box(self, lines, grey):
        # a short line above or below the centre-crossing one: growth must take it in, and
        # must not carry on into the video beyond the box (box drawn at y 880..960)
        font = _font(24)
        bw = max(font.getlength(t) for t in lines) + 28
        x = (1920 - bw) / 2
        img = Image.new("RGB", (1920, 1080), (grey,) * 3).convert("RGBA")
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        d.rounded_rectangle([x, 880, x + bw, 960], 10, fill=(0, 0, 0, 185))
        for i, t in enumerate(lines):
            d.text((x + 14, 894 + 32 * i), t, fill="white", font=font)
        band = crop_band(Image.alpha_composite(img, over).convert("RGB"))
        off = int(1080 * bench.BAND_TOP)
        box = find_caption_box(band)
        assert box is not None
        assert 880 - off - 4 <= box[1] <= 900 - off  # first line's glyph tops, not video above
        assert 948 - off <= box[3] <= 960 - off + 4  # last line's glyph bottoms, not video below

    def test_dark_strip_without_glyphs_is_not_a_box(self):
        img = Image.new("RGB", (400, 100), (150, 150, 150))
        img.paste((20, 20, 20), (0, 70, 400, 100))  # a toolbar-like dark strip, no text
        assert find_caption_box(img) is None

    def test_left_aligned_label_is_not_a_caption(self):
        img = Image.new("RGB", (1000, 200), (30, 30, 30))
        ImageDraw.Draw(img).text((510, 80), "Dana McAllister", fill="white", font=_font(24))
        assert find_caption_box(img) is None  # starts right of centre: a tile label


_WORDS = (
    "roadmap ship the spike owner quarterly revenue hiring plan budget review next steps".split()
)


def _slide(rng: random.Random, kind: str) -> Image.Image:
    bg = tuple(rng.randint(225, 255) for _ in range(3))
    if kind == "white":
        fg = tuple(rng.randint(0, 60) for _ in range(3))
    else:
        fg = rng.choice([(20, 40, 140), (150, 20, 20), (20, 100, 40)])
    img = Image.new("RGB", (1920, 1080), bg)
    d = ImageDraw.Draw(img)
    size = rng.choice([18, 22, 24, 28, 32, 40])
    lines = rng.randint(1, 4)
    y = rng.randint(680, 1000 - lines * int(size * 1.4))
    centred = rng.random() < 0.6
    for j in range(lines):
        text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(2, 9)))
        x = 960 if centred else rng.randint(300, 900)
        d.text(
            (x, y + j * int(size * 1.4)),
            text,
            fill=fg,
            font=_font(size),
            anchor="mm" if centred else "lm",
        )
    return img


class TestSynth:
    def test_frame_size_and_lines(self):
        f = make_frame(random.Random(1), 0, 0.5, 75)
        assert f.image.size == (960, 540)
        assert 1 <= len(f.lines) <= 3 and all(split_speaker(x)[0] for x in f.lines)

    def test_generate_writes_truth_with_dark_and_empty_frames(self, tmp_path):
        truth = json.loads(generate(tmp_path, 15, [1.0]).read_text())
        assert len(truth) == 15 and all((tmp_path / n).exists() for n in truth)
        assert any("_dark_" in n and v == [] for n, v in truth.items())
        assert any("_video_" in n and v == [] for n, v in truth.items())


class TestRun:
    def test_no_caption_frames_skip_the_engine(self):
        calls = []

        def engine(img, crop):
            calls.append(crop)
            return ["A: x"]

        engine.name = "fake"
        empty = make_frame(random.Random(0), 0, 1.0, 75, dark=True, caption=False)
        cap = make_frame(random.Random(0), 1, 1.0, 75)
        frames = [("e", empty.image, []), ("c", cap.image, cap.lines)]
        r = bench.run(engine, "box", frames, None)
        assert calls == ["box"]  # only the captioned frame reached OCR
        assert r["line_precision"] == 0.0  # the fake's "A: x" matches no real line

    def test_warm_up_is_the_first_ocr_call_not_frame_zero(self, monkeypatch):
        clock = iter([0.0, 0.0, 1.0, 10.0, 2.0, 2.5, 3.0, 3.5])  # perf_counter pairs per frame
        monkeypatch.setattr(bench.time, "perf_counter", lambda: next(clock))

        def engine(img, crop):
            return []

        engine.name = "fake"
        empty = make_frame(random.Random(0), 0, 1.0, 75, dark=True, caption=False)
        cap = make_frame(random.Random(0), 1, 1.0, 75)
        frames = [("e", empty.image, [])] + [(f"c{i}", cap.image, cap.lines) for i in range(3)]
        r = bench.run(engine, "box", frames, None)
        # frame "c0" took 9 s of warm-up and is excluded; c1, c2 took 0.5 s each
        assert r["lat_p50_ms"] == 500 and r["lat_p95_ms"] == 500


class TestBenchCli:
    def test_results_json_is_strict_json(self, tmp_path, monkeypatch):
        # a scale whose few frames all lack captions yields NaN metrics; they're written as null
        frames = tmp_path / "f"
        frames.mkdir()
        empty = make_frame(random.Random(0), 0, 1.0, 75, dark=True, caption=False)
        empty.image.save(frames / "a.png")
        (frames / "truth.json").write_text(json.dumps({"a.png": []}))

        # CI installs no OCR engine; this one is never called (the frame has no caption box)
        def never_called(img, crop):
            pytest.fail("OCR ran on a frame with no caption box")

        never_called.name = "tesseract"
        monkeypatch.setattr(bench, "make_engine", lambda name, threads: never_called)
        assert (
            bench.main(
                [
                    "--images",
                    str(frames),
                    "--engines",
                    "tesseract",
                    "--crops",
                    "box",
                    "--out",
                    str(tmp_path / "o"),
                ]
            )
            == 0
        )
        r = json.loads((tmp_path / "o" / "results.json").read_text(), parse_constant=pytest.fail)
        assert r[0]["line_acc"] is None and r[0]["lat_p50_ms"] is None

    @pytest.mark.parametrize(
        "args",
        [
            ["--synth", "0"],
            ["--synth", "1", "--engines", "bogus"],
            ["--synth", "1", "--crops", "nope"],
            ["--synth", "1", "--scales", "abc"],
            ["--synth", "1", "--scales", "0"],
            ["--images", "/nonexistent"],
        ],
    )
    def test_bad_args_are_usage_errors(self, args, tmp_path):
        with pytest.raises(SystemExit) as e:
            bench.main([*args, "--out", str(tmp_path)])
        assert e.value.code == 2


class TestCaptureHelpers:
    def test_black_frame_detected(self):
        assert frame_stats([0] * 99 + [255])["black"] is True
        assert frame_stats([0] * 50 + [200] * 50)["black"] is False
        assert frame_stats([])["black"] is True

    def test_resolve_exact_then_unique_substring(self):
        titles = ["Inbox - Outlook", "Desk A - VMware Horizon", "Desk B - VMware Horizon"]
        assert resolve("Desk A - VMware Horizon", titles) == ("Desk A - VMware Horizon", None)
        assert resolve("desk b", titles) == ("Desk B - VMware Horizon", None)
        assert resolve("Desk C", titles)[0] is None

    def test_resolve_refuses_ambiguous_match(self):
        titles = ["Desk A meeting notes - Google Chrome", "Desk A - VMware Horizon"]
        title, error = resolve("Desk A", titles)
        assert title is None and "ambiguous" in error

    @pytest.mark.parametrize(
        "row,good",
        [
            ({"frames": 3, "black": False}, True),
            ({"frames": 3, "black": True}, False),
            ({"frames": 0}, False),
            ({"frames": 3, "black": False, "error": "boom"}, False),
            ({"error": "no visible window matches"}, False),
        ],
    )
    def test_is_good(self, row, good):
        assert is_good(row) is good
