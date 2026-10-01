"""Freeze the app's own STT finals into replay items (set "fleurs_asr").

Input: results/stt_capture.json from an STT-only `ws_driver.py --no-wait` pass. The
production trigger is applied per clip: a non-English final opens/extends a run and is
translated with its language; an undetected final inside a run is translated with
source_lang=None; an English final, or an undetected one outside a run, is shown
untranslated (replay keeps its text as-is). Writes data/items_all.json = the text sets
from prep.py + these.
"""

from __future__ import annotations

import json


def app_trigger(finals: list[dict]) -> list[tuple[bool, str | None]]:
    """(translate?, source_lang) per final, mirroring session._consider_translation."""
    out, in_run = [], False
    for f in finals:
        lang = f.get("lang")
        if lang and lang != "en":
            in_run = True
            out.append((True, lang))
        elif lang is None and in_run:
            out.append((True, None))
        else:
            in_run = False
            out.append((False, None))
    return out


def main() -> None:
    refs = json.load(open("/work/data/fleurs_refs.json"))
    items, stats = [], {"finals": 0, "translated": 0, "en_or_untagged": 0, "clips_no_final": 0}
    for clip in json.load(open("/work/results/stt_capture.json")):
        stats["clips_no_final"] += not clip["finals"]
        for k, (f, (tr, src)) in enumerate(zip(clip["finals"], app_trigger(clip["finals"]))):
            stats["finals"] += 1
            stats["translated" if tr else "en_or_untagged"] += 1
            items.append({"id": f"{clip['id']}#{k}", "clip": clip["id"], "part": k,
                          "set": "fleurs_asr", "text": f["text"], "source_lang": src,
                          "translate": tr, "ref": refs[clip["id"]]["en"], "app_lang": f["lang"]})
    base = json.load(open("/work/data/items.json"))
    json.dump(base + items, open("/work/data/items_all.json", "w"), ensure_ascii=False, indent=1)
    print(stats)


if __name__ == "__main__":
    main()
