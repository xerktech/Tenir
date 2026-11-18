"""Unit tests for the offline separated-caption gate replay (XERK-3069)."""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diarize_gate as dg  # noqa: E402


def _dec(text: str, lp: float | None = -0.2, lang: str = "es") -> dict:
    return {
        "wav": "x.wav",
        "text": text,
        "lang": lang,
        "avg_logprob": lp,
        "non_speech": False,
    }


def _piece(n, a, b, speaker, mix, sep=None, overlap=True, turn=0) -> dict:
    p = {
        "n": n,
        "turn_start_ms": turn,
        "start_ms": a,
        "end_ms": b,
        "speaker": speaker,
        "overlap": overlap,
        "mix": mix,
    }
    if sep is not None:
        p["sep"] = sep
    return p


def test_words_keep_accents_and_case_fold() -> None:
    assert dg.words("¿Está BIEN, João?") == ["está", "bien", "joão"]


def test_the_gate_drops_only_separated_pieces_below_it() -> None:
    pieces = [
        _piece(0, 0, 2000, 1, _dec("raw words", lp=-2.0), overlap=False),
        _piece(1, 2000, 4000, 1, _dec("mixed a"), _dec("hola amigo", lp=-0.6)),
        _piece(2, 2000, 4000, 2, _dec("mixed b"), _dec("buenas noches", lp=-0.3)),
    ]
    texts = lambda caps: [c["text"] for c in caps]  # noqa: E731
    assert texts(dg.captions(pieces, "sep", None)) == [
        "raw words",
        "hola amigo",
        "buenas noches",
    ]
    # A raw-span piece is never gated, however unsure Whisper is of it.
    assert texts(dg.captions(pieces, "sep", -0.5)) == ["raw words", "buenas noches"]
    assert texts(dg.captions(pieces, "mix", -0.5)) == [
        "raw words",
        "mixed a",
        "mixed b",
    ]


def test_replay_dedups_one_voice_heard_twice_like_the_transcriber() -> None:
    pieces = [
        _piece(0, 0, 3000, 1, _dec("m"), _dec("la casa es grande")),
        _piece(1, 0, 3000, 2, _dec("m"), _dec("la casa es grande sí")),
    ]
    assert [c["n"] for c in dg.captions(pieces, "sep", None)] == [0]


def test_replay_scores_policies_and_bands_against_mix_truth() -> None:
    truth = {
        "mixes": [
            {
                "start_ms": 0,
                "end_ms": 4000,
                "refs": [{"text": "hola amigo mío"}, {"text": "buenas noches a todos"}],
            }
        ]
    }
    pieces = [
        _piece(0, 0, 4000, 1, _dec("hola noches"), _dec("hola amigo mío", lp=-0.2)),
        _piece(1, 0, 4000, 2, _dec("amigo"), _dec("pan con queso", lp=-0.9)),
    ]
    r = dg.replay(pieces, truth)
    assert r["by_lang"]["es"] == {
        "separated": 2,
        "dropped": {"-0.4": 1, "-0.5": 1, "-0.6": 1, "-0.7": 1, "-0.8": 1, "-1.0": 0},
    }
    keep_all = r["policies"]["sep gate None"]
    assert keep_all == {"precision": 0.5, "recall": round(3 / 7, 3), "words": 6}
    assert r["policies"]["sep gate -0.5"] == {
        "precision": 1.0,
        "recall": round(3 / 7, 3),
        "words": 3,
    }
    # Each reference word is credited once, however many captions repeat it.
    assert r["policies"]["mix (no separation)"] == {
        "precision": 1.0,
        "recall": round(3 / 7, 3),
        "words": 3,
    }
    assert r["sep_word_accuracy"]["es"] == {">=-0.3": "3/3 (100%)", "<-0.8": "0/3 (0%)"}


def _wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dg.SR)
        w.writeframes(x.astype(np.int16).tobytes())


def test_mixes_pair_confident_solo_pieces_of_different_speakers(tmp_path: Path) -> None:
    src = tmp_path / "rec"
    src.mkdir()
    rng = np.random.default_rng(0)
    rows = []
    for n, (spk, lp, overlap) in enumerate(
        [
            (1, -0.1, False),
            (2, -0.2, False),
            (1, -0.1, False),
            (2, -0.9, False),
            (3, -0.1, True),
        ]
    ):
        _wav(src / f"{n}.mix.wav", rng.normal(0, 1000, 2 * dg.SR))
        rows.append(
            _piece(
                n,
                n * 3000,
                n * 3000 + 2000,
                spk,
                {**_dec(f"una frase número {n} aquí", lp=lp), "wav": f"{n}.mix.wav"},
                overlap=overlap,
                turn=n,
            )
        )
    (src / "pieces.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    out = tmp_path / "mixes"
    dg.main(["mixes", str(src / "pieces.jsonl"), str(out), "--seed", "3"])
    truth = json.loads((out / "0" / "truth.json").read_text())
    # Only pieces 0-2 qualify (3 is unsure, 4 overlapped); one pair needs two speakers.
    assert len(truth["mixes"]) == 1
    assert {r["speaker"] for r in truth["mixes"][0]["refs"]} == {1, 2}
    with wave.open(str(out / "0" / "session.wav"), "rb") as w:
        assert w.getframerate() == dg.SR
        dur_ms = w.getnframes() * 1000 // dg.SR
    assert truth["mixes"][0]["end_ms"] <= dur_ms


def test_a_gated_piece_can_fall_back_to_its_mixed_span() -> None:
    pieces = [
        _piece(
            0, 0, 3000, 1, _dec("lo que se oye"), _dec("palabras inventadas", lp=-0.9)
        ),
        _piece(1, 0, 3000, 2, _dec("lo que se oye"), _dec("buenas noches", lp=-0.2)),
    ]
    assert [c["text"] for c in dg.captions(pieces, "sep+mix", -0.5)] == [
        "lo que se oye",
        "buenas noches",
    ]


def test_pool_puts_runs_on_one_timeline_without_overlap() -> None:
    run = [_piece(0, 0, 3000, 1, _dec("hola"), _dec("hola"))]
    truth = {"mixes": [{"start_ms": 0, "end_ms": 3500, "refs": [{"text": "hola"}]}]}
    pieces, pooled = dg.pool([run, run], [truth, truth])
    assert [p["n"] for p in pieces] == [0, 1]
    assert pieces[1]["start_ms"] > pooled["mixes"][0]["end_ms"]
    assert pooled["mixes"][1]["start_ms"] == pieces[1]["start_ms"]
    # Two identical runs score as one run twice, not as one voice heard twice.
    r = dg.replay(pieces, pooled)["policies"]["sep gate None"]
    assert (r["precision"], r["recall"]) == (1.0, 1.0)


def test_recorder_keeps_every_decode_and_captions_nothing(tmp_path: Path) -> None:
    import asyncio

    import diarize_pieces as rec
    from api.stt.diarize import Piece
    from api.stt.engine import EngineResult

    class Engine:
        def transcribe(self, samples, *, language, want_words=True):
            loud = float(np.abs(samples).max())
            return EngineResult(
                text="separada" if loud < 0.2 else "mezcla",
                language="es",
                avg_logprob=-0.7,
            )

    t = rec.Recorder(
        tmp_path, Engine(), partial_interval_ms=10**9, local_agreement=False
    )
    pcm = (np.full(dg.SR * 2, 16000, dtype=np.int16)).tobytes()
    sep = (np.full(dg.SR, 3000, dtype=np.int16)).tobytes()
    pieces = [Piece(1, 0, 1000, False, None), Piece(2, 1000, 2000, True, sep)]

    async def go() -> bool:
        done = await t._finalize_pieces(pcm, 5000, pieces)
        await t._finalize_turn(pcm, 9000, True, "", "Speaker 3")
        await t._finalize_turn(pcm, 12000, True, "", None)  # unlabelled: skipped
        return done

    assert asyncio.run(go()) is True
    assert [(r["speaker"], r["overlap"], "sep" in r) for r in t.rows] == [
        (1, False, False),
        (2, True, True),
        (3, False, False),
    ]
    assert (
        t.rows[1]["sep"]["text"] == "separada" and t.rows[1]["mix"]["text"] == "mezcla"
    )
    assert t.rows[1]["sep"]["avg_logprob"] == -0.7
    assert (t.rows[1]["start_ms"], t.rows[1]["end_ms"]) == (6000, 7000)
    assert t.rows[2]["end_ms"] == 11000
    with wave.open(str(tmp_path / t.rows[1]["sep"]["wav"]), "rb") as w:
        assert w.getnframes() == dg.SR
