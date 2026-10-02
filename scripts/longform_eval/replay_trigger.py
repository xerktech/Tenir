"""Re-decide language + translation for a recorded run's finals, offline.

Iterating on language ID or the translator no longer needs a 10-minute real-time
stream per chunk: the deployed Parakeet reports no language, so every final's ``lang``
is ``api.stt.langid.detect_lang(text)`` and the translation trigger is a pure function
of the finals' sequence. This replays both with whatever ``api`` package is importable
(point PYTHONPATH at a branch's ``api/src`` to test its langid), reproducing
``Session._consider_translation``: a non-English turn opens/extends a run and is
translated from its language, an English turn closes the run, an undetected turn
inside a run inherits it, and the run also closes after ``--hold-ms`` without a final
(an approximation: live, partials keep the hold alive too). Echoes (translation equal
to the source) are dropped as the session does.

Output is a run file in ws_driver's shape, scored by score.py like any other run.

Usage: python replay_trigger.py run.json --out replay.json
       --endpoint http://tenir-translator.ai.svc.cluster.local:8000/v1 --model milmmt-46-4b
       [--prompt-style milmmt|chat-json] [--hold-ms 3000] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

from api.stt.langid import detect_lang
from gold_translate import expects_call, make_translator


def decide(finals: list[dict], hold_ms: int) -> list[tuple[dict, str | None, str | None] | None]:
    """Per final: None (not translated) or (final, source_lang, run_lang) — the
    arguments production passes to ``translator.translate``."""
    out: list[tuple[dict, str | None, str | None] | None] = []
    active, run_lang, last_end = False, None, None
    for f in sorted(finals, key=lambda f: f["startMs"]):
        if active and last_end is not None and f["startMs"] - last_end > hold_ms:
            active = False
        lang = detect_lang(f["text"])
        f["lang"] = lang
        if lang is not None and lang != "en":
            active, run_lang = True, lang
            out.append((f, lang, lang))
        elif lang == "en":
            active = False
            out.append(None)
        elif active:
            out.append((f, None, run_lang))
        else:
            out.append(None)
        last_end = f["endMs"]
    return out


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def translate_job(tr, job: tuple[dict, str | None, str | None]) -> None:
    """Translate one decided final in place, as the session would: echoes dropped, and a
    turn that was sent but came back empty recorded as ``translate_error``."""
    f, source_lang, run_lang = job
    t = time.monotonic()
    hyp = tr.translate(f["text"], source_lang=source_lang, run_lang=run_lang)
    if not hyp and expects_call(tr, f["text"], source_lang, run_lang):
        f["translate_error"] = "no output from the translator (failed call or empty)"
    f["latency_ms"] = round((time.monotonic() - t) * 1000)
    f["translation"] = None if not hyp or _same(hyp, f["text"]) else hyp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--prompt-style", default="milmmt", choices=["milmmt", "chat-json"])
    ap.add_argument("--hold-ms", type=int, default=3000)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    tr = make_translator(a.prompt_style, a.endpoint, a.model, a.api_key)
    run = json.load(open(a.run))
    jobs = []
    for clip in run:
        for f in clip.get("finals", []):
            f["translation"], f["latency_ms"] = None, None
        jobs += [d for d in decide(clip.get("finals", []), a.hold_ms) if d is not None]

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(lambda j: translate_job(tr, j), jobs))
    json.dump(run, open(a.out, "w"), ensure_ascii=False, indent=1)
    errors = sum(1 for f, _, _ in jobs if f.get("translate_error"))
    print(f"{len(jobs)} sent to the translator of {sum(len(c.get('finals', [])) for c in run)} "
          f"finals; {errors} failed (timeouts etc. — scored as untranslated)")


if __name__ == "__main__":
    main()
