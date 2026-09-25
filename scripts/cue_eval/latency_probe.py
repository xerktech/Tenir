"""Single-request latency probe for the cue and translation calls.

Production holds one cue attempt in flight per session, so the latency that
matters is a lone request on an idle server — a multi-worker replay's call
times include queueing. This fires N cue calls (fixed transcript windows spread
evenly across the chosen conversations) and M translation calls, one at a time,
using the shipped payloads, and reports mean/p50/p90/max, generated tokens, and
finish reasons. ``finish_reason == "length"`` on a cue call means the model ran
out of budget while thinking: the cue is silently dropped in production.

Usage:
  python scripts/cue_eval/latency_probe.py segments.json \\
    --endpoint http://127.0.0.1:9402/v1 --model qwen3.8-27b \\
    --conversations id1,id2 [--translations eval_set.json] \\
    [--extra '{"custom_params": {"thinking_budget": 512}}']
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import time
from pathlib import Path

import httpx

from api.cue.openai import OpenAICueGenerator
from api.translate.openai import OpenAITranslator

CONTEXT_SEGMENTS = 8


def merge_extra(payload: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict | None):
            payload[key] = {**(payload.get(key) or {}), **value}
        else:
            payload[key] = value
    return payload


def cue_windows(segments_path: Path, conv_ids: list[str], n: int) -> list[str]:
    """Up to ``n`` windows spread evenly across all candidate positions, so every
    conversation contributes in proportion to its length."""
    by_conv: dict[str, list[dict]] = collections.defaultdict(list)
    for seg in json.loads(segments_path.read_text()):
        if (seg.get("text") or "").strip() and (not conv_ids or seg["conversation_id"] in conv_ids):
            by_conv[seg["conversation_id"]].append(seg)
    candidates = []
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])
        candidates += [segs[i - CONTEXT_SEGMENTS : i] for i in range(CONTEXT_SEGMENTS, len(segs) + 1)]
    if n <= 0 or not candidates:
        return []
    take = min(n, len(candidates))
    picks = [candidates[k * len(candidates) // take] for k in range(take)]
    return ["\n".join(s["text"].strip() for s in window) for window in picks]


def run(client: httpx.Client, url: str, payloads: list[dict]) -> dict:
    ms, tokens, finish, empty = [], [], collections.Counter(), 0
    for payload in payloads:
        started = time.monotonic()
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        ms.append((time.monotonic() - started) * 1000)
        tokens.append((body.get("usage") or {}).get("completion_tokens", 0))
        finish[body["choices"][0].get("finish_reason")] += 1
        if not (body["choices"][0]["message"].get("content") or "").strip():
            empty += 1
    if not ms:
        raise SystemExit("nothing to probe: no requests were built")
    q = sorted(ms)
    return {
        "n": len(ms),
        "mean_ms": round(statistics.mean(ms)),
        "p50_ms": round(q[len(q) // 2]),
        "p90_ms": round(q[int(len(q) * 0.9)]),
        "max_ms": round(q[-1]),
        "mean_completion_tokens": round(statistics.mean(tokens)),
        "tok_per_s": round(sum(tokens) / sum(ms) * 1000, 1),
        "empty_content": empty,
        "finish": dict(finish),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("segments", type=Path, help="segments.json export (cue windows)")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--conversations", default="", help="comma-separated ids (default: all)")
    ap.add_argument("--n", type=int, default=40, help="cue calls")
    ap.add_argument("--translations", type=Path, help="translation eval_set.json (optional)")
    ap.add_argument("--m", type=int, default=100, help="translation calls")
    ap.add_argument("--extra", default="{}", help="JSON merged into every cue request")
    args = ap.parse_args()

    base = args.endpoint.rstrip("/")
    url = base + "/chat/completions"
    try:
        extra = json.loads(args.extra)
    except json.JSONDecodeError as exc:
        ap.error(f"--extra is not JSON: {exc}")
    if not isinstance(extra, dict):
        ap.error("--extra must be a JSON object")
    gen = OpenAICueGenerator(endpoint=base, model=args.model, api_key=args.api_key)
    conv_ids = [c for c in args.conversations.split(",") if c]
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    if args.n < 1:
        ap.error("--n must be at least 1")
    known = {s["conversation_id"] for s in json.loads(args.segments.read_text())}
    unknown = [c for c in conv_ids if c not in known]
    if unknown:
        ap.error(f"--conversations not in the export: {', '.join(unknown)}")
    windows = cue_windows(args.segments, conv_ids, args.n)
    if not windows:
        raise SystemExit("no cue windows: the chosen conversations are all under 8 turns")
    with httpx.Client(timeout=180, headers=headers) as client:
        run(client, url, [gen._build_payload("hello there")])  # warm-up
        report = {"cue": run(client, url, [merge_extra(gen._build_payload(w), extra) for w in windows])}
        if args.translations and args.m > 0:
            items = json.loads(args.translations.read_text())
            items = items[:: max(1, len(items) // args.m)][: args.m]
            tr = OpenAITranslator(endpoint=base, model=args.model, api_key=args.api_key)
            report["translation"] = run(
                client, url, [tr._build_payload(i["text"], i.get("source_lang")) for i in items]
            )
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
