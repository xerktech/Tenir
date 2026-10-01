"""Replay fixed text items through the SHIPPED Tenir translator payload/parser.

Same contract as ../replay.py (build with OpenAITranslator._build_payload, parse with
_parse) but over the public reference sets and strictly sequential — production holds
one translation in flight per session, so per-call ms is the caption-lag number.
Items marked translate=False (frozen STT finals the app would show untranslated) are
passed through without a call.

Usage: python replay_text.py items_all.json [--subset] --out x.json
       [--endpoint http://127.0.0.1:9000/v1]
"""

from __future__ import annotations

import argparse
import json
import time

try:
    from api.translate.openai import OpenAITranslator
except ImportError:  # in the eval pod: a copy of api/src/api/translate/openai.py on the path
    from openai_translator import OpenAITranslator

SUBSET_CAPS = {"fleurs_gold": 60, "opus_conv": 100, "passthrough": 50}
SUBSET_ASR_CLIPS = 100


def subset(items: list[dict]) -> list[dict]:
    """The fixed stratified subset the shipped-prompt runs use: the first N of each text
    set, and every frozen STT final of the first 100 clips (whole clips, so the
    clip-level score is still the app's)."""
    seen: dict[str, int] = {}
    clips: list[str] = []
    keep = []
    for it in items:
        if it["set"] == "fleurs_asr":
            if it["clip"] not in clips and len(clips) < SUBSET_ASR_CLIPS:
                clips.append(it["clip"])
            if it["clip"] in clips:
                keep.append(it)
        elif seen.get(it["set"], 0) < SUBSET_CAPS[it["set"]]:
            seen[it["set"]] = seen.get(it["set"], 0) + 1
            keep.append(it)
    return keep


def main() -> None:  # pragma: no cover - needs a live endpoint
    import httpx

    ap = argparse.ArgumentParser()
    ap.add_argument("items")
    ap.add_argument("--endpoint", default="http://127.0.0.1:9000/v1")
    ap.add_argument("--model", default="candidate")
    ap.add_argument("--out", required=True)
    ap.add_argument("--subset", action="store_true", help="see subset()")
    a = ap.parse_args()

    tr = OpenAITranslator(endpoint=a.endpoint, model=a.model)
    items = json.load(open(a.items))
    if a.subset:
        items = subset(items)
    url = a.endpoint.rstrip("/") + "/chat/completions"
    out = []
    with httpx.Client(timeout=120) as c:
        # warm-up: first-request effects stay out of the numbers
        c.post(url, json=tr._build_payload("Hola, ¿cómo estás?", "es"))
        for i, it in enumerate(items):
            rec = dict(it)
            if it.get("translate") is False:
                rec["hyp"], rec["ms"] = it["text"], None
                out.append(rec)
                continue
            t0 = time.monotonic()
            try:
                r = c.post(url, json=tr._build_payload(it["text"], it["source_lang"]))
                r.raise_for_status()
                body = r.json()
                content = OpenAITranslator._message_content(body["choices"][0]["message"])
                rec["hyp"] = OpenAITranslator._parse(content)
                rec["raw"] = content if rec["hyp"] is None else None
                rec["usage"] = body.get("usage")
            except Exception as e:  # noqa: BLE001 - record and move on
                rec["hyp"] = None
                rec["error"] = f"{type(e).__name__}: {e}"[:200]
            rec["ms"] = (time.monotonic() - t0) * 1000
            out.append(rec)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(items)}", flush=True)
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    ms = sorted(r["ms"] for r in out if "error" not in r and r["ms"] is not None)
    fails = sum(1 for r in out if r["hyp"] is None)
    lat = f"ms mean={sum(ms) / len(ms):.0f} p50={ms[len(ms) // 2]:.0f} p90={ms[int(.9 * len(ms))]:.0f}" \
        if ms else "no successful calls"
    print(f"{a.out}: n={len(out)} parse/err fails={fails} {lat}")


if __name__ == "__main__":
    main()
