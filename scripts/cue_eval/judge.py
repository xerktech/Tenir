"""LLM-judge replayed cues against their transcript windows.

Grades every cue in a replay.py results file 0-2 on novelty / relevance /
accuracy and flags duplicates of earlier cues, writing ``<results>.judged.json``.
The judge shares the generator's weights, so treat accuracy comparatively and
spot-check flagged cues by hand. Usage: see scripts/cue_eval/README.md.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import threading
from pathlib import Path

import httpx

JUDGE_SYSTEM = (
    "You are grading a 'cue' produced by an assistant that listens to a live "
    "conversation and surfaces short helpful notes to a listener. You get the "
    "recent transcript (speech-recognition text, may contain errors), the cues "
    "already shown earlier, and the new cue. Grade the NEW cue on four axes.\n"
    "novelty: 2 = the body's key information is NOT in the transcript (a real "
    "addition: definition, background, number, comparison, correction); 1 = "
    "mostly restates the transcript but adds a little; 0 = pure restatement or "
    "summary of what was already said.\n"
    "relevance: 2 = directly about what is being discussed right now and a "
    "listener would plausibly care; 1 = tangential; 0 = off-topic or about a "
    "garbled/nonexistent thing.\n"
    "accuracy: 2 = you are confident every claim in the body is correct; 1 = "
    "plausible but you cannot verify a key claim, or it asserts facts about an "
    "entity that looks misheard/unknown; 0 = contains a claim that is wrong, "
    "contradicts the transcript's firsthand details, or invents facts about a "
    "misrecognized name.\n"
    "duplicate: true if the cue's key information was already conveyed by an "
    "earlier cue (even in different words), else false.\n"
    "Reply with a single JSON object only: "
    '{"novelty": 0|1|2, "relevance": 0|1|2, "accuracy": 0|1|2, '
    '"duplicate": true|false, "reason": "one short sentence"}'
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", type=Path, help="replay.py output")
    ap.add_argument("segments", type=Path, help="segments.json export")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    by_conv: dict[str, list[dict]] = collections.defaultdict(list)
    for seg in json.loads(args.segments.read_text()):
        by_conv[seg["conversation_id"]].append(seg)
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])

    data = json.loads(args.results.read_text())
    jobs = []
    for conv in data["conversations"]:
        segs = by_conv[conv["conversation_id"]]
        for k, cue in enumerate(conv["cues"]):
            i = cue["seg_index"]
            window = [s["text"] for s in segs[max(0, i - 9) : i + 3]]
            jobs.append((cue, window, conv["cues"][:k]))

    url = args.endpoint.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    lock = threading.Lock()
    it = iter(jobs)

    def worker() -> None:
        with httpx.Client() as client:
            while True:
                with lock:
                    try:
                        cue, window, prior = next(it)
                    except StopIteration:
                        return
                prior_txt = "\n".join(f"- {p['title']}: {p['body']}" for p in prior) or "(none)"
                user = (
                    "TRANSCRIPT (recent turns):\n" + "\n".join(window)
                    + "\n\nCUES ALREADY SHOWN:\n" + prior_txt
                    + f"\n\nNEW CUE:\n{cue['title']}: {cue['body']}"
                )
                payload = {
                    "model": args.model,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"},
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                verdict = None
                try:
                    resp = client.post(url, json=payload, headers=headers, timeout=60)
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"].get("content") or ""
                    match = re.search(r"\{.*\}", content, re.DOTALL)
                    if match:
                        verdict = json.loads(match.group(0))
                except Exception:
                    pass
                cue["judge"] = verdict

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = args.results.with_suffix(".judged.json")
    out.write_text(json.dumps(data, indent=2))
    print(f"judged {len(jobs)} cues -> {out}")


if __name__ == "__main__":
    main()
