"""LLM-judge replayed translations.

Grades every item in a replay.py results file, writing ``<results>.judged.json``.
The judge sees the source utterance and the candidate translation — NOT the
detected source language, because language detection misfires in production
(English utterances get tagged pt/it/fr and translated anyway), and the judge
must grade what the utterance actually is:

- source_type: what the utterance really is — foreign / english / mixed.
- adequacy 0-2: meaning preserved. For an already-English source, 2 means it
  came back (essentially) unchanged — rewriting or answering it scores 0.
- fluency 0-2: natural English output.

The judge may share the generator's weights — use its numbers only to compare
runs on identical data, and hand-review a sample (see README).
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from pathlib import Path

import httpx

JUDGE_SYSTEM = (
    "You are grading one step of a live speech translation system. You get one "
    "utterance transcribed by speech-to-text (may contain recognition errors, "
    "disfluencies, or mixed languages) and the English rendering the system "
    "produced for it. Grade the rendering on these axes.\n"
    "source_type: 'english' if the source utterance is already entirely "
    "English; 'mixed' if it mixes English with another language; 'foreign' "
    "otherwise.\n"
    "adequacy: how well the rendering preserves the source's meaning. 2 = "
    "faithful and complete (recognition errors rendered by their most "
    "plausible intent are fine; for an 'english' source, 2 means it was "
    "returned essentially unchanged); 1 = partial — some meaning lost, "
    "garbled, or a minor addition; 0 = wrong — meaning changed, content "
    "invented, the source answered/summarized/commented on instead of "
    "translated, or an 'english' source rewritten.\n"
    "fluency: 2 = natural English; 1 = understandable but awkward; 0 = "
    "broken or not English. Grade fluency only on the rendering itself; a "
    "faithfully-preserved source disfluency is not a fluency fault.\n"
    "Reply with a single JSON object only: "
    '{"source_type": "foreign"|"english"|"mixed", "adequacy": 0|1|2, '
    '"fluency": 0|1|2, "reason": "one short sentence"}'
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", type=Path, help="replay.py output")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--reasoning-effort",
        default="",
        help="for gpt-oss judges: cap the analysis channel (low|medium|high)",
    )
    ap.add_argument("--max-tokens", type=int, default=500)
    args = ap.parse_args()

    data = json.loads(args.results.read_text())
    jobs = [r for r in data["items"] if r.get("translation")]

    url = args.endpoint.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    lock = threading.Lock()
    it = iter(jobs)
    done = [0]

    def worker() -> None:
        with httpx.Client() as client:
            while True:
                with lock:
                    try:
                        rec = next(it)
                    except StopIteration:
                        return
                user = (
                    "SOURCE UTTERANCE (speech-to-text):\n" + rec["text"]
                    + "\n\nENGLISH RENDERING PRODUCED:\n" + rec["translation"]
                )
                payload = {
                    "model": args.model,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.0,
                    "max_tokens": args.max_tokens,
                    "response_format": {"type": "json_object"},
                }
                if args.reasoning_effort:
                    payload["reasoning_effort"] = args.reasoning_effort
                verdict = None
                try:
                    resp = client.post(url, json=payload, headers=headers, timeout=120)
                    resp.raise_for_status()
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content") or msg.get("reasoning_content") or ""
                    match = re.search(r"\{.*\}", content, re.DOTALL)
                    if match:
                        verdict = json.loads(match.group(0))
                except Exception:  # noqa: BLE001 - judged as missing
                    pass
                rec["judge"] = verdict
                with lock:
                    done[0] += 1
                    if done[0] % 25 == 0:
                        print(f"  {done[0]}/{len(jobs)}")

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = args.results.with_suffix(".judged.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    missing = sum(1 for r in jobs if not r.get("judge"))
    print(f"judged {len(jobs) - missing}/{len(jobs)} translations -> {out}")


if __name__ == "__main__":
    main()
