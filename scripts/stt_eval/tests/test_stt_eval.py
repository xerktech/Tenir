"""Unit tests for the STT eval harness (run locally: python -m pytest scripts/stt_eval/tests/)."""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from score import aggregate, normalize, score_pair  # noqa: E402
from transcribe import SAMPLE_RATE, SAMPLE_WIDTH, ConversationAudio  # noqa: E402


class TestNormalize:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize("Hello, World!") == "hello world"

    def test_nfkc_folds_nonbreaking_space(self):
        # The narrow no-break space the cue model emits inside names.
        assert normalize("Fold 8") == "fold 8"

    def test_collapses_whitespace(self):
        assert normalize("  a \t b\n c ") == "a b c"

    def test_numbers_split_on_punctuation(self):
        assert normalize("2.3 million") == "2 3 million"


class TestScorePair:
    def test_identical_after_normalization_is_zero_error(self):
        s = score_pair("Hello, world!", "hello world")
        assert (s["sub"], s["del"], s["ins"]) == (0, 0, 0)
        assert s["ref_words"] == 2

    def test_substitution_counted(self):
        s = score_pair("the cat sat", "the dog sat")
        assert s["sub"] == 1 and s["ref_words"] == 3

    def test_empty_ref_counts_hyp_as_insertions(self):
        s = score_pair("", "spurious words here")
        assert s == {"ref_words": 0, "sub": 0, "del": 0, "ins": 3, "hits": 0}

    def test_empty_hyp_counts_ref_as_deletions(self):
        s = score_pair("dropped words", "")
        assert s == {"ref_words": 2, "sub": 0, "del": 2, "ins": 0, "hits": 0}

    def test_both_empty_is_no_op(self):
        assert score_pair("...", "!?")["ref_words"] == 0


class TestAggregate:
    def test_pools_errors_over_ref_words(self):
        entries = [
            {"score": score_pair("the cat sat", "the dog sat")},  # 1 err / 3 words
            {"score": score_pair("hello world", "hello world")},  # 0 err / 2 words
        ]
        agg = aggregate(entries)
        assert agg["ref_words"] == 5
        assert agg["wer"] == pytest.approx(1 / 5)

    def test_empty_set_has_null_wer(self):
        assert aggregate([])["wer"] is None


def make_wav(path: Path, n_samples: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        # Ramp so slices are byte-verifiable against sample indices.
        out.writeframes(b"".join((i % 32768).to_bytes(2, "little") for i in range(n_samples)))


class TestConversationAudio:
    def test_slice_maps_ms_to_samples(self, tmp_path):
        make_wav(tmp_path / "a.wav", SAMPLE_RATE)  # 1 s
        audio = ConversationAudio(tmp_path / "a.wav")
        assert audio.duration_ms == 1000
        wav_bytes, actual_ms = audio.slice_wav(250, 750)
        assert actual_ms == 500
        with wave.open(io.BytesIO(wav_bytes), "rb") as sliced:
            assert sliced.getnframes() == SAMPLE_RATE // 2
            first = sliced.readframes(1)
        # 250 ms in = sample 4000.
        assert int.from_bytes(first, "little") == SAMPLE_RATE // 4

    def test_slice_clamps_to_file(self, tmp_path):
        make_wav(tmp_path / "a.wav", SAMPLE_RATE // 2)  # 0.5 s
        audio = ConversationAudio(tmp_path / "a.wav")
        _, actual_ms = audio.slice_wav(400, 2000)
        assert actual_ms == 100
        _, actual_ms = audio.slice_wav(900, 2000)
        assert actual_ms == 0

    def test_rejects_wrong_format(self, tmp_path):
        with wave.open(str(tmp_path / "bad.wav"), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(b"\x00" * 64)
        with pytest.raises(ValueError, match="expected"):
            ConversationAudio(tmp_path / "bad.wav")
