"""Replay exported deployment transcripts through the SHIPPED cue prompt.

Builds each request with ``api.cue.openai.OpenAICueGenerator._build_payload``
from the installed ``api`` package — so what this measures is always the prompt
that ships — and emulates the session's gating: an 8-turn rolling context, one
attempt in flight at a time, the min interval between emitted cues, and all
three dedupe backstops (normalized title + substance fingerprint +
title-subject containment). Ungrounded only.

Usage: see scripts/cue_eval/README.md.
"""

from __future__ import annotations

import argparse
import collections
import json
import threading
import time
from pathlib import Path

import httpx

from api.cue.base import (
    CUE_SUBSTANCE_MIN_TOKENS,
    GeneratedCue,
    cue_subject_tokens,
    cue_substance_similarity,
    cue_substance_tokens,
    normalize_cue_title,
)
from api.cue.openai import OpenAICueGenerator
from api.cue.tuning import MIN_INTERVAL_MS

CONTEXT_SEGMENTS = 8  # settings.cue_context_segments default
ATTEMPT_MS = 2500  # transcript time one serialized attempt occupies
AVOID_LIMIT = 40  # session._CUE_AVOID_PROMPT_LIMIT
DUP_THRESHOLD = 0.35  # session._CUE_SUBSTANCE_DUP_THRESHOLD


def load_conversations(path: Path) -> dict[str, list[dict]]:
    by_conv: dict[str, list[dict]] = collections.defaultdict(list)
    for seg in json.loads(path.read_text()):
        by_conv[seg["conversation_id"]].append(seg)
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])
    return by_conv


def replay_conversation(
    gen: OpenAICueGenerator,
    client: httpx.Client,
    url: str,
    conv_id: str,
    segments: list[dict],
) -> dict:
    recent: collections.deque[str] = collections.deque(maxlen=CONTEXT_SEGMENTS)
    surfaced: list[GeneratedCue] = []
    norms: set[str] = set()
    substance: list[frozenset[str]] = []
    subjects: set[str] = set()
    out = {
        "conversation_id": conv_id,
        "attempts": 0,
        "declines": 0,
        "dedup_drops": 0,
        "errors": 0,
        "call_ms_total": 0,
        "cues": [],
    }
    next_free_ms = 0
    last_emit_ms = -(10**9)
    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        recent.append(text)
        at = seg["end_ms"]
        if at < next_free_ms or at - last_emit_ms < MIN_INTERVAL_MS:
            continue
        out["attempts"] += 1
        next_free_ms = at + ATTEMPT_MS
        payload = gen._build_payload("\n".join(recent), surfaced[-AVOID_LIMIT:])
        started = time.monotonic()
        try:
            resp = client.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            content = OpenAICueGenerator._message_content(resp.json()["choices"][0]["message"])
        except Exception:
            out["errors"] += 1
            continue
        finally:
            # Mean call latency bounds cue frequency directly (attempts are
            # serialized one-in-flight live), so the replay records it.
            out["call_ms_total"] += int((time.monotonic() - started) * 1000)
        cue = gen._parse(content)
        if cue is None:
            out["declines"] += 1
            continue
        norm = normalize_cue_title(cue.title)
        tokens = cue_substance_tokens(cue.title, cue.body)
        subject = cue_subject_tokens(cue.title)
        if (
            norm in norms
            or (
                len(tokens) >= CUE_SUBSTANCE_MIN_TOKENS
                and any(
                    len(p) >= CUE_SUBSTANCE_MIN_TOKENS
                    and cue_substance_similarity(tokens, p) >= DUP_THRESHOLD
                    for p in substance
                )
            )
            or bool(subject & subjects)
        ):
            out["dedup_drops"] += 1
            continue
        norms.add(norm)
        surfaced.append(cue)
        substance.append(tokens)
        subjects |= subject
        last_emit_ms = at
        out["cues"].append(
            {"title": cue.title, "body": cue.body, "at_ms": at, "seg_index": i}
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("segments", type=Path, help="segments.json export")
    ap.add_argument("--endpoint", required=True, help="OpenAI-compatible /v1 base URL")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--out", type=Path, default=Path("results.json"))
    ap.add_argument(
        "--conversations", default="", help="comma-separated conversation ids (default: all)"
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--reasoning-effort",
        default="",
        choices=["", "low", "medium", "high"],
        help="for gpt-oss models: sets reasoning_effort on every request and strips "
        "the Qwen-specific chat_template_kwargs (vLLM and Ollama both accept the "
        "OpenAI-style field; the dial is gpt-oss's selectivity/speed trade)",
    )
    ap.add_argument(
        "--no-template-kwargs",
        action="store_true",
        help="strip the Qwen-specific chat_template_kwargs without setting a "
        "reasoning effort — required for Mistral models, whose vLLM tokenizer "
        "mode rejects chat_template_kwargs with a 400",
    )
    args = ap.parse_args()

    by_conv = load_conversations(args.segments)
    conv_ids = [c for c in args.conversations.split(",") if c] or list(by_conv)
    gen = OpenAICueGenerator(endpoint=args.endpoint, model=args.model, api_key=args.api_key)
    if args.reasoning_effort or args.no_template_kwargs:

        class _AdaptedGen(OpenAICueGenerator):
            def _build_payload(self, transcript, avoid_cues=(), evidence=()):
                payload = super()._build_payload(transcript, avoid_cues, evidence)
                payload.pop("chat_template_kwargs", None)
                if args.reasoning_effort:
                    payload["reasoning_effort"] = args.reasoning_effort
                return payload

        gen = _AdaptedGen(endpoint=args.endpoint, model=args.model, api_key=args.api_key)
    url = args.endpoint.rstrip("/") + "/chat/completions"

    results: list[dict | None] = [None] * len(conv_ids)
    lock = threading.Lock()
    index = iter(range(len(conv_ids)))

    def worker() -> None:
        with httpx.Client() as client:
            while True:
                with lock:
                    try:
                        i = next(index)
                    except StopIteration:
                        return
                results[i] = replay_conversation(gen, client, url, conv_ids[i], by_conv[conv_ids[i]])
                done = results[i]
                print(f"{conv_ids[i][:8]}: {len(done['cues'])} cues / {done['attempts']} attempts")

    threads = [threading.Thread(target=worker) for _ in range(min(args.workers, len(conv_ids)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = {"conversations": [r for r in results if r]}
    args.out.write_text(json.dumps(out, indent=2))
    total = sum(len(c["cues"]) for c in out["conversations"])
    attempts = sum(c["attempts"] for c in out["conversations"])
    call_ms = sum(c["call_ms_total"] for c in out["conversations"])
    mean = f", mean call {call_ms / attempts / 1000:.2f}s" if attempts else ""
    print(f"total: {total} cues / {attempts} attempts{mean} -> {args.out}")


if __name__ == "__main__":
    main()
