"""Unit tests for the speaker-tracking eval harness (XERK-2958)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diarize_score import score  # noqa: E402

sf = pytest.importorskip("soundfile")


def _truth() -> dict:
    return {
        "utterances": [
            {"speaker": 1, "start": 0.0, "end": 3.0, "text": "the cat sat on the mat"},
            {"speaker": 2, "start": 2.0, "end": 5.0, "text": "dogs like long walks"},
            {"speaker": 1, "start": 6.0, "end": 8.0, "text": "it rained all day"},
        ]
    }


def _cap(text: str, a: float, b: float, speaker: str | None) -> dict:
    c = {"text": text, "startMs": int(a * 1000), "endMs": int(b * 1000)}
    if speaker:
        c["speaker"] = speaker
    return c


def test_perfect_labels_score_one_whatever_the_label_names() -> None:
    caps = [
        _cap("The cat sat on the mat.", 0, 3, "Speaker 4"),
        _cap("Dogs like long walks.", 2, 5, "Speaker 2"),
        _cap("It rained all day.", 6, 8, "Speaker 4"),
    ]
    r = score(_truth(), caps)
    assert r["all"] == {"words": 14, "recall": 1.0, "attributed": 1.0, "purity": 1.0}
    assert r["mapping"] == {"Speaker 2": 2, "Speaker 4": 1}
    assert r["overlapped"]["words"] == 10 and r["solo"]["words"] == 4
    assert r["precision"] == 1.0


def test_invented_caption_words_cost_precision() -> None:
    caps = [
        _cap("the cat sat on the mat", 0, 3, "Speaker 1"),
        _cap("purple monkeys dance", 2, 3, "Speaker 2"),
    ]
    assert score(_truth(), caps)["precision"] == pytest.approx(6 / 9, abs=1e-3)


def test_unlabelled_captions_recall_words_but_attribute_none() -> None:
    caps = [
        _cap("the cat sat on the mat dogs like long walks", 0, 5, None),
        _cap("it rained all day", 6, 8, None),
    ]
    r = score(_truth(), caps)
    assert r["all"]["recall"] == 1.0 and r["all"]["attributed"] == 0.0


def test_words_under_the_wrong_speaker_cost_purity() -> None:
    caps = [
        _cap("the cat sat on the mat dogs like long walks", 0, 5, "Speaker 1"),
        _cap("it rained all day", 6, 8, "Speaker 1"),
    ]
    r = score(_truth(), caps)
    assert r["all"]["recall"] == 1.0
    assert r["all"]["attributed"] == pytest.approx(10 / 14, abs=1e-3)
    assert r["label_share_per_speaker"][2] == {"Speaker 1": 1.0}  # all under one wrong label


def test_build_session_writes_truth_matching_the_audio(tmp_path: Path) -> None:
    import build_session

    root = tmp_path / "corpus"
    for spk in ("11", "22", "33"):
        d = root / spk / "1"
        d.mkdir(parents=True)
        lines = []
        for i in range(3):
            uid = f"{spk}-1-{i:04d}"
            sf.write(d / f"{uid}.flac", 0.1 * np.ones(16000, np.float32), 16000)
            lines.append(f"{uid} HELLO NUMBER {i}")
        (d / f"{spk}-1.trans.txt").write_text("\n".join(lines))
    out = tmp_path / "s"
    build_session.build(root, out, speakers=3, minutes=0.3, seed=3, overlap_p=0.5, snr_db=20)
    truth = json.loads(out.with_suffix(".json").read_text())
    audio, sr = sf.read(out.with_suffix(".wav"))
    assert sr == 16000 and abs(len(audio) / sr - truth["seconds"]) < 0.01
    spk = [u["speaker"] for u in truth["utterances"]]
    assert spk[0] == 1 and set(spk) <= {1, 2, 3}
    assert all(u["end"] <= truth["seconds"] for u in truth["utterances"])
    assert all(u["text"] is None or u["text"].startswith("hello") for u in truth["utterances"])
