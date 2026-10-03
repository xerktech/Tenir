"""Build refs.json for score.py from OCR'd Spanish cues + YouTube json3 English cues.

Usage: python build_refs.py es_caps.json subs.en.json3 --out refs.json
"""

from __future__ import annotations

import argparse
import json


def json3_cues(d: dict) -> list[dict]:
    """YouTube json3 events -> cues. Events without text (window/style events,
    lone newlines) are dropped."""
    out = []
    for e in d.get("events", []):
        text = " ".join("".join(s.get("utf8", "") for s in e.get("segs", [])).split())
        if text:
            start = e["tStartMs"]
            out.append({"start_ms": start, "end_ms": start + e.get("dDurationMs", 0), "text": text})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("es")
    ap.add_argument("en_json3")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    es = [c for c in json.load(open(a.es)) if c["text"].strip()]
    en = json3_cues(json.load(open(a.en_json3)))
    json.dump({"es": es, "en": en}, open(a.out, "w"), ensure_ascii=False, indent=0)
    print(f"es={len(es)} en={len(en)}")


if __name__ == "__main__":
    main()
