"""Unit tests for the long-form eval scorer (synthetic data only)."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("jiwer")
pytest.importorskip("sacrebleu")

from build_refs import json3_cues  # noqa: E402
from score import align_units, norm, score_run, wer_counts  # noqa: E402


def cue(t0, t1, text):
    return {"start_ms": t0, "end_ms": t1, "text": text}


def final(t0, t1, text, translation=None, lang="es"):
    return {"segmentId": f"s{t0}", "startMs": t0, "endMs": t1, "text": text, "lang": lang,
            "translation": translation}


def test_norm_strips_punctuation_and_folds_accents():
    assert norm("¿Qué tal?  Sí, ¡bien!") == "qué tal sí bien"
    assert norm("¿Qué tal? Sí", fold=True) == "que tal si"


def test_wer_counts_empty_reference_counts_insertions():
    assert wer_counts("", "hola mundo") == (2, 0)
    assert wer_counts("hola mundo", "hola mundo") == (0, 2)
    assert wer_counts("hola mundo", "hola") == (1, 2)


def test_align_units_assigns_cue_to_max_overlap_turn_and_offsets_chunk():
    # Chunk 1 of 10 s chunks: finals are chunk-relative, cues absolute.
    finals = [final(0, 4000, "uno"), final(4000, 9000, "dos")]
    en = [cue(10500, 13000, "one"), cue(13500, 18000, "two")]  # second overlaps turn 2 most
    units = align_units(finals, en, [cue(10200, 13900, "uno")], offset_ms=10000)
    assert [u["en"] for u in units] == ["one", "two"]
    assert units[0]["es"] == "uno" and units[1]["es"] == ""


def test_align_units_groups_uncovered_cues_into_missed_units():
    en = [cue(20000, 21000, "a"), cue(21500, 22000, "b"), cue(40000, 41000, "c")]
    units = align_units([final(0, 5000, "x")], en, [], offset_ms=0)
    missed = [u for u in units if u.get("missed")]
    assert [u["en"] for u in missed] == ["a b", "c"]  # >5 s gap splits
    assert all(u["hyp"] is None for u in missed)


def test_score_run_perfect_and_untranslated():
    refs = {"es": [cue(0, 2000, "hola amigo"), cue(10500, 12000, "buenos días")],
            "en": [cue(0, 2000, "hello friend"), cue(10500, 12000, "good morning")]}
    run = [{"id": "c0", "finals": [final(0, 2100, "Hola, amigo.", "hello friend")]},
           {"id": "c1", "finals": [final(400, 2100, "buenos dias", None)]}]
    r = score_run(refs, run, chunk_sec=10)
    assert r["per_chunk"][0]["wer"] == 0
    assert r["per_chunk"][1]["wer"] == 0.5 and r["per_chunk"][1]["wer_fold"] == 0
    assert r["units"] == 2 and r["untranslated_units"] == 1 and r["missed_units"] == 0


def test_score_run_rejects_bad_clip_id():
    with pytest.raises(ValueError):
        score_run({"es": [], "en": []}, [{"id": "clip0", "finals": []}], chunk_sec=10)


def test_json3_cues_drops_textless_events():
    d = {"events": [{"tStartMs": 0, "dDurationMs": 5},
                    {"tStartMs": 10, "dDurationMs": 990, "segs": [{"utf8": "Hi "}, {"utf8": "there"}]},
                    {"tStartMs": 20, "segs": [{"utf8": "\n"}]}]}
    assert json3_cues(d) == [{"start_ms": 10, "end_ms": 1000, "text": "Hi there"}]


def test_ocr_mask_keeps_text_span_and_drops_far_yellow():
    pytest.importorskip("PIL")
    from ocr_captions import ocr_mask, runs

    a = np.zeros((40, 300, 3), np.uint8)
    a[10:30, 100:160] = (230, 230, 40)  # caption text block
    a[5:8, 120:125] = (160, 160, 60)    # accent: only the loose mask sees it, near text
    a[10:30, 280:290] = (160, 160, 60)  # yellowish background object, far from text
    m = ocr_mask(a)
    assert m[6, 122] and m[20, 130] and not m[:, 200:].any()
    blank = np.zeros_like(a)
    got = [(s, e) for s, e, _, _ in runs([blank, a, a, a, blank, a])]
    assert got == [(1, 3), (5, 5)]


def test_replay_decide_mirrors_session_trigger():
    pytest.importorskip("api.stt.langid")
    from replay_trigger import decide

    es = "el perro está en la casa"
    en = "the dog is in the house and it is fine"
    finals = [final(0, 1000, es), final(1100, 1500, "Mercurio"), final(1600, 2000, en),
              final(2100, 2500, "Mercurio"), final(9000, 9500, es), final(13000, 13300, "Venus")]
    got = [None if d is None else (d[1], d[2]) for d in decide(finals, hold_ms=3000)]
    assert got == [
        ("es", "es"),   # Spanish opens a run
        (None, "es"),   # undetected inherits it
        None,           # English closes it
        None,           # undetected outside a run decides nothing
        ("es", "es"),   # reopens
        None,           # > hold_ms since the last final: the run expired
    ]


def test_expects_call_tells_a_declined_turn_from_a_failed_one():
    from gold_translate import expects_call

    class Completion:  # completion style: declines with None (no call)
        def _build_payload(self, text, source_lang=None, run_lang=None):
            return None if (source_lang or run_lang) in (None, "en") else {"prompt": text}

    class Chat:  # chat style: always calls for non-empty text
        def _build_payload(self, text, source_lang=None):
            return {"messages": [text]}

    assert expects_call(Completion(), "hola", "es", "es")
    assert not expects_call(Completion(), "hola", None, None)
    assert expects_call(Chat(), "hola", None, None)
    assert not expects_call(Chat(), "  ", None, None)


def test_score_run_counts_unscored_turns_and_translate_errors():
    refs = {"es": [cue(0, 2000, "hola")], "en": [cue(0, 2000, "hello")]}
    f_err = final(0, 2000, "hola", None)
    f_err["translate_error"] = "no output"
    orphan = final(5000, 6000, "adiós", "bye")  # no English cue overlaps it
    r = score_run(refs, [{"id": "c0", "finals": [f_err, orphan]}], chunk_sec=10)
    assert r["translate_errors"] == 1
    assert r["unscored_translated_turns"] == 1
    assert r["units"] == 1 and r["untranslated_units"] == 1


def test_translate_job_records_failures_and_drops_echoes():
    from replay_trigger import translate_job

    class Tr:
        def __init__(self, out):
            self.out = out

        def _build_payload(self, text, source_lang=None):
            return {"messages": [text]}

        def translate(self, text, *, source_lang=None, run_lang=None):
            return self.out

    f = final(0, 1000, "hola")
    translate_job(Tr(None), (f, "es", "es"))
    assert f["translation"] is None and "translate_error" in f
    f = final(0, 1000, "hola")
    translate_job(Tr("Hola"), (f, "es", "es"))  # echo: dropped, not an error
    assert f["translation"] is None and "translate_error" not in f
    f = final(0, 1000, "hola")
    translate_job(Tr("hello"), (f, "es", "es"))
    assert f["translation"] == "hello"


def test_blank_turns_are_neither_translated_nor_failures():
    pytest.importorskip("api.stt.langid")
    from gold_translate import expects_call
    from replay_trigger import decide

    class Completion:
        def _build_payload(self, text, source_lang=None, run_lang=None):
            return {"prompt": text}

    assert not expects_call(Completion(), "  ", "es", "es")
    es = "el perro está en la casa"
    got = decide([final(0, 1000, es), final(1100, 1500, ""), final(1600, 2000, es)], 3000)
    assert got[1] is None and got[2] is not None  # the blank neither translates nor closes


def test_blank_turn_does_not_extend_the_hold():
    # The session never sees a blank final, so it can't keep a run's hold alive: a run
    # whose last real turn ended > hold_ms before the next one has expired.
    pytest.importorskip("api.stt.langid")
    from replay_trigger import decide

    es = "el perro está en la casa"
    got = decide([final(0, 1000, es), final(3000, 3500, ""), final(4500, 5000, "Mercurio")],
                 hold_ms=3000)
    assert got[2] is None  # 3.5 s since the last real final: the inherited turn is dropped
