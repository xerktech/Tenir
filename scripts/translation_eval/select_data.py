"""Select the translation eval set from a segments export.

Reads a segments JSON export (see scripts/cue_eval/README.md for the psql
export command; include the ``lang`` and ``translation`` columns) and picks
the utterances production would translate: segments whose effective language
— stored ``lang``, else `api.stt.langid.detect_lang`, mirroring
`api/src/api/stt/streaming.py` — is a known non-English language, plus
undetected segments that fall inside an active translation run (production
inherits the run for those and translates with source_lang=None).

Usage:
    python select_data.py segments.json --out eval_set.json [--summary]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from api.stt.langid import detect_lang


def effective_lang(seg: dict) -> str | None:
    return seg.get("lang") or detect_lang(seg["text"] or "")


def select(segments: list[dict]) -> list[dict]:
    """Return eval items in transcript order, tagged with the run logic."""
    items: list[dict] = []
    by_conv: dict[str, list[dict]] = {}
    for seg in segments:
        by_conv.setdefault(seg["conversation_id"], []).append(seg)

    for conv_segs in by_conv.values():
        conv_segs.sort(key=lambda s: s["start_ms"])
        in_run = False
        for seg in conv_segs:
            lang = effective_lang(seg)
            if lang == "en":
                in_run = False
                continue
            if lang is None:
                if not in_run:
                    continue
                # inherited: translated with source_lang=None in production
            else:
                in_run = True
            items.append(
                {
                    "segment_id": seg["segment_id"],
                    "conversation_id": seg["conversation_id"],
                    "text": seg["text"],
                    "source_lang": lang,  # None == inherited run segment
                    "stored_lang": seg.get("lang"),
                    "stored_translation": seg.get("translation"),
                }
            )
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("segments", help="segments export JSON")
    ap.add_argument("--out", default="eval_set.json")
    ap.add_argument("--summary", action="store_true", help="print stats only")
    args = ap.parse_args()

    segments = json.load(open(args.segments))
    items = select(segments)

    langs = Counter(i["source_lang"] for i in items)
    convs = Counter(i["conversation_id"] for i in items)
    print(f"segments in export: {len(segments)}")
    print(f"eval utterances:    {len(items)}  langs={dict(langs)}")
    print(f"stored lang counts: {dict(Counter(s.get('lang') for s in segments))}")
    print(f"with stored translation: {sum(1 for i in items if i['stored_translation'])}")
    for cid, n in convs.most_common():
        cl = Counter(i["source_lang"] for i in items if i["conversation_id"] == cid)
        print(f"  {cid[:8]}  n={n}  {dict(cl)}")

    if not args.summary:
        with open(args.out, "w") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
