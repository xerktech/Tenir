"""Translate the *reference* Spanish through the shipped translator, on a run's turns.

Separates translation error from STT error: takes a ws_driver run, groups the reference
Spanish cues onto that run's final turns exactly as score.py does, and translates each
turn's reference Spanish with the shipped translator class (source_lang="es", sequential
like production). The output is a run file in ws_driver's shape — finals carry the
reference Spanish as ``text`` — so score.py scores it unchanged (its WER is then ~0 by
construction; read only the translation columns).

Usage: python gold_translate.py refs.json run.json --out gold.json
       --endpoint http://tenir-translator.ai.svc.cluster.local:8000/v1 --model milmmt-46-4b
       [--prompt-style milmmt|chat-json] [--chunk-sec 600]
"""

from __future__ import annotations

import argparse
import json
import time

from score import align_units, chunk_index, cues_in


def make_translator(style: str, endpoint: str, model: str, api_key: str):
    if style == "milmmt":
        from api.translate.completion import CompletionTranslator

        return CompletionTranslator(endpoint=endpoint, model=model, api_key=api_key)
    from api.translate.openai import OpenAITranslator

    return OpenAITranslator(endpoint=endpoint, model=model, api_key=api_key)


def expects_call(tr, text: str, source_lang: str | None, run_lang: str | None) -> bool:
    """Whether the translator will actually call the model for this turn. The shipped
    translators catch their own errors and return None, so a None from a turn that
    *was* sent is the only sign of a failed call (timeout, 4xx/5xx) or empty output;
    a None from a turn the translator declines (completion style: no nameable source
    language, or an inherited turn leaning English) is a decision, not a failure."""
    if not text.strip():
        return False  # translate() returns None for blank text without a call
    build = tr._build_payload
    if "run_lang" in build.__code__.co_varnames:
        return build(text, source_lang=source_lang, run_lang=run_lang) is not None
    return bool(text.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("refs")
    ap.add_argument("run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--prompt-style", default="milmmt", choices=["milmmt", "chat-json"])
    ap.add_argument("--chunk-sec", type=int, default=600)
    a = ap.parse_args()
    refs = json.load(open(a.refs))
    tr = make_translator(a.prompt_style, a.endpoint, a.model, a.api_key)
    out = []
    for clip in sorted(json.load(open(a.run)), key=lambda c: chunk_index(c["id"])):
        k = chunk_index(clip["id"])
        lo, hi = k * a.chunk_sec * 1000, (k + 1) * a.chunk_sec * 1000
        units = align_units(clip.get("finals", []), cues_in(refs["en"], lo, hi),
                            cues_in(refs["es"], lo, hi), lo)
        finals = []
        for u in units:
            if u.get("missed") or not u["es"]:
                continue
            t = time.monotonic()
            hyp = tr.translate(u["es"], source_lang="es")
            f = {"segmentId": f"{clip['id']}-{len(finals)}", "text": u["es"], "lang": "es",
                 "startMs": u["t0"] - lo, "endMs": u["t1"] - lo, "translation": hyp,
                 "latency_ms": round((time.monotonic() - t) * 1000)}
            if not hyp and expects_call(tr, u["es"], "es", None):
                f["translate_error"] = "no output from the translator (failed call or empty)"
            finals.append(f)
        out.append({"id": clip["id"], "finals": finals})
        print(f"{clip['id']}: {len(finals)} turns", flush=True)
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
