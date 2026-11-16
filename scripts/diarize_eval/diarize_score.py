"""Score speaker-labelled captions against a synthetic session's truth (XERK-2958).

    python diarize_score.py session.json captions.json [baseline.json]

For every reference utterance, its words are matched (difflib, word level) against the
captions that overlap it in time, per caption speaker label. Labels map one-to-one to
true speakers by the most matched words (Hungarian on the word counts). Reported:

- **recall** — reference words found in any caption (what the transcript keeps);
- **attributed** — reference words found in captions carrying the *right* speaker label:
  the number the ticket is about ("which words belong to which speaker");
- **purity** — of matched words, the share under the right label;
- the same on utterances that overlap another speaker's;
- **precision** — caption words found in a reference utterance near them (what is invented
  lowers it);
- **labels per speaker** — how many labels each true speaker's words were spread over
  (1 everywhere = consistent numbering).
"""

from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher
from itertools import permutations
from pathlib import Path

SLACK_S = 1.0  # caption times are turn-granular; allow a little drift


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def matched(ref: list[str], hyp: list[str]) -> int:
    if not ref or not hyp:
        return 0
    return sum(
        b.size for b in SequenceMatcher(None, ref, hyp, autojunk=False).get_matching_blocks()
    )


def score(truth: dict, captions: list[dict]) -> dict:
    utts = [u for u in truth["utterances"] if u["text"]]
    for i, u in enumerate(truth["utterances"]):
        u["overlapped"] = any(
            v is not u
            and v["speaker"] != u["speaker"]
            and v["start"] < u["end"]
            and u["start"] < v["end"]
            for v in truth["utterances"]
        )
    labels = sorted({c.get("speaker") or "-" for c in captions})
    per: list[dict] = []
    for u in utts:
        ref = words(u["text"])
        near = [
            c
            for c in captions
            if c["startMs"] / 1000 < u["end"] + SLACK_S and u["start"] - SLACK_S < c["endMs"] / 1000
        ]
        near.sort(key=lambda c: c["startMs"])
        by_label = {
            L: matched(
                ref, [w for c in near if (c.get("speaker") or "-") == L for w in words(c["text"])]
            )
            for L in labels
        }
        allm = matched(ref, [w for c in near for w in words(c["text"])])
        per.append({"u": u, "n": len(ref), "all": allm, "by": by_label})

    spks = sorted({p["u"]["speaker"] for p in per})
    counts = {
        L: {s: sum(p["by"][L] for p in per if p["u"]["speaker"] == s) for s in spks} for L in labels
    }
    # One-to-one label -> speaker map maximising matched words (few labels: brute force).
    real = [L for L in labels if L != "-"]
    best, mapping = -1, {}
    slots = spks + [None] * max(0, len(real) - len(spks))
    for perm in permutations(slots, len(real)) if real else [()]:
        tot = sum(counts[L][s] for L, s in zip(real, perm) if s is not None)
        if tot > best:
            best, mapping = tot, {L: s for L, s in zip(real, perm)}
    inv = {s: L for L, s in mapping.items() if s is not None}

    def agg(rows: list[dict]) -> dict:
        n = sum(r["n"] for r in rows) or 1
        rec = sum(r["all"] for r in rows)
        att = sum(r["by"].get(inv.get(r["u"]["speaker"], "?"), 0) for r in rows)
        tot = sum(sum(r["by"].values()) for r in rows) or 1
        return {
            "words": n,
            "recall": round(rec / n, 3),
            "attributed": round(att / n, 3),
            "purity": round(att / tot, 3),
        }

    spread = {}
    for s in spks:
        tot = sum(counts[L][s] for L in real) or 1
        spread[s] = {L: round(counts[L][s] / tot, 2) for L in real if counts[L][s] / tot >= 0.05}
    # Precision: caption words that are in a reference utterance near them. What the
    # captions invent (a separator artefact Whisper "hears") lowers it.
    cap_words = cap_hit = 0
    for c in captions:
        hyp = words(c["text"])
        ref = [
            w
            for u in utts
            if u["start"] - SLACK_S < c["endMs"] / 1000 and c["startMs"] / 1000 < u["end"] + SLACK_S
            for w in words(u["text"])
        ]
        cap_words += len(hyp)
        cap_hit += matched(hyp, ref)
    return {
        "precision": round(cap_hit / (cap_words or 1), 3),
        "all": agg(per),
        "overlapped": agg([p for p in per if p["u"]["overlapped"]]),
        "solo": agg([p for p in per if not p["u"]["overlapped"]]),
        "mapping": {L: s for L, s in mapping.items()},
        "label_share_per_speaker": spread,
        "captions": len(captions),
    }


def main() -> None:
    truth = json.loads(Path(sys.argv[1]).read_text())
    for path in sys.argv[2:]:
        print(
            path,
            json.dumps(
                score(json.loads(json.dumps(truth)), json.loads(Path(path).read_text())), indent=1
            ),
        )


if __name__ == "__main__":
    main()
