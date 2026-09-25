"""Replay exported deployment transcripts through the SHIPPED cue prompt.

Builds each request with ``api.cue.openai.OpenAICueGenerator._build_payload``
from the installed ``api`` package — so what this measures is always the prompt
that ships — and emulates the session's gating: an 8-turn rolling context, one
attempt in flight at a time, the min interval between emitted cues, and all
three dedupe backstops (normalized title + substance fingerprint +
title-subject containment). Ungrounded by default; ``--grounded`` adds live
retrieval evidence (cached per transcript window so every model under
comparison sees identical evidence), ``--realtime`` spaces attempts by the
measured call latency instead of a fixed 2.5 s, and ``--verify`` runs the
self-check pass on every emitted cue.

Usage: see scripts/cue_eval/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
import re
import sys
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

# Self-check prompt for --verify: the same model, thinking off, decides whether a
# generated cue is safe to show. Measured 2026-09 (RESULTS-2026-09-shootout.md):
# ~0.3 s per cue, cuts judged-wrong cues ~3x on full-thinking output.
VERIFY_SYSTEM = (
    "You fact-check short notes (\"cues\") that an assistant shows a listener during a "
    "live conversation. The transcript is speech-to-text and may contain misheard words. "
    "Decide whether the cue is SAFE to show:\n"
    "- every factual claim in it is correct (dates, numbers, specs, names, arithmetic);\n"
    "- it does not contradict what the speakers themselves said they see, own, or measured;\n"
    "- it is about something actually being discussed, not a misheard word resolved to an "
    "unrelated famous thing or an invented meaning for an unknown name/acronym;\n"
    "- if you are not confident a key claim is correct, it is NOT safe.\n"
    'Reply with JSON only: {"safe": true|false, "reason": "<=12 words"}'
)


def merge_extra(payload: dict, extra: dict) -> dict:
    """Overlay --extra onto a request body; dict values merge one level deep."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict | None):
            payload[key] = {**(payload.get(key) or {}), **value}
        else:
            payload[key] = value
    return payload


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class EvidenceCache:
    """Evidence per transcript window, fetched once and shared across runs.

    Retrieval results drift minute to minute and rate limits make them flaky, so
    comparing two models on live retrieval would compare two evidence sets. The
    cache pins the evidence: the first run fetches it, every later run (any
    model) reads the same items — and the fetch latency, which ``--realtime``
    charges to the in-flight window as the live session does — back from ``path``.

    An empty result is ambiguous: no hits, or a tier that failed (Wikipedia 429s
    bursts, and the retriever swallows tier errors). Empties are cached like any
    other result so a comparison stays on identical evidence, but they are
    counted, and ``refetch_empty`` retries them — warm the cache with it until
    the empty count stops falling, then compare.
    """

    def __init__(self, path: Path, retriever_factory, config: dict, refetch_empty: bool = False):
        self._path = path
        self._factory = retriever_factory
        self._config = config
        self._refetch_empty = refetch_empty
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._local = threading.local()
        self._windows: dict[str, dict] = {}
        raw = path.read_text() if path.exists() else ""
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path} is not valid JSON ({exc}); use a fresh --evidence-cache")
            if not isinstance(data, dict) or not isinstance(data.get("windows"), dict):
                raise SystemExit(
                    f"{path} is not an evidence cache written by this version; "
                    "use a fresh --evidence-cache"
                )
            if data.get("config") != config:
                raise SystemExit(
                    f"{path} was filled from different retrieval endpoints "
                    f"({data.get('config')}); use a fresh --evidence-cache"
                )
            self._windows = data["windows"]

    @staticmethod
    def key(turns: list[str]) -> str:
        return hashlib.sha1("\n".join(turns).encode()).hexdigest()

    def _fetch(self, turns: list[str]) -> dict:
        # The retriever is async and owns an httpx.AsyncClient bound to its
        # loop, so each worker thread gets its own loop + retriever.
        if not hasattr(self._local, "loop"):
            self._local.loop = asyncio.new_event_loop()
            self._local.retriever = self._factory()
        started = time.monotonic()
        try:
            found = self._local.loop.run_until_complete(
                self._local.retriever.retrieve(list(turns))
            )
        except Exception:
            found = []
        return {
            "evidence": [
                {"source": e.source, "title": e.title, "snippet": e.snippet,
                 "published": e.published, "url": e.url}
                for e in found
            ],
            "ms": int((time.monotonic() - started) * 1000),
        }

    def get(self, turns: list[str]) -> tuple[list, int]:
        """The window's evidence and the retrieval latency it cost when fetched."""
        from api.cue.retrieval.base import Evidence

        key = self.key(turns)
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:  # single flight: concurrent askers for one window fetch once
            with self._lock:
                hit = self._windows.get(key)
            if hit is None or (self._refetch_empty and not hit["evidence"]):
                hit = self._fetch(turns)
                with self._lock:
                    self._windows[key] = hit
        return [Evidence(**e) for e in hit["evidence"]], hit["ms"]

    def save(self) -> None:
        """Atomic write, so an interrupted save can't corrupt the pinned evidence."""
        with self._lock:
            body = json.dumps({"config": self._config, "windows": self._windows})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(body)
        os.replace(tmp, self._path)


def verify_cue(
    client, url: str, model: str, mode: str, turns: list[str], evidence, cue, adapt=None
) -> str:
    """'safe', 'unsafe', or 'error'. The caller drops both of the last two (a
    broken verifier must not pass cues), but counts errors separately so a broken
    verifier can't pass for a strict one."""
    evidence_text = ""
    if evidence:
        evidence_text = "\n\nEVIDENCE (retrieved, may be irrelevant):\n" + "\n".join(
            f"- [{e.source}] {e.title}: {e.snippet}" for e in evidence
        )
    user = (
        "TRANSCRIPT (latest turns):\n" + "\n".join(turns) + evidence_text
        + f"\n\nCUE:\n{cue.title}: {cue.body}"
    )
    kwargs = {"enable_thinking": False} if mode == "off" else {
        "enable_thinking": True, "reasoning_effort": mode,
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": kwargs,
    }
    if adapt is not None:
        payload = adapt(payload)
        if "reasoning_effort" in payload:
            # gpt-oss style: no thinking switch, so "off" maps to the lowest effort.
            payload["reasoning_effort"] = "low" if mode == "off" else mode
    try:
        resp = client.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        content = OpenAICueGenerator._message_content(resp.json()["choices"][0]["message"])
        match = _JSON_OBJECT.search(content or "")  # tolerate fences, like the cue parser
        verdict = json.loads(match.group(0)) if match else None
    except Exception:
        return "error"
    if not isinstance(verdict, dict) or not isinstance(verdict.get("safe"), bool):
        return "error"
    return "safe" if verdict["safe"] else "unsafe"


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
    *,
    extra: dict | None = None,
    evidence: EvidenceCache | None = None,
    realtime: bool = False,
    verify: str = "",
    verify_adapt=None,
    clock=time.monotonic,
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
        "call_ms": [],
        "verify_ms": [],
        "verify_drops": 0,
        "verify_errors": 0,
        "evidence_windows": 0,
        "evidence_empty": 0,
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
        turns = list(recent)
        found, retrieval_ms = evidence.get(turns) if evidence else ((), 0)
        if evidence:
            out["evidence_windows"] += 1
            out["evidence_empty"] += not found
        payload = merge_extra(
            gen._build_payload("\n".join(turns), surfaced[-AVOID_LIMIT:], found), extra or {}
        )
        # Live, the in-flight window opens before retrieval (session awaits the
        # retriever under the same flag), so realtime spacing charges its latency.
        in_flight_ms = retrieval_ms
        started = clock()
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
            call_ms = int((clock() - started) * 1000)
            out["call_ms_total"] += call_ms
            out["call_ms"].append(call_ms)
            in_flight_ms += call_ms
            if realtime:
                # Live sessions hold one attempt in flight, so the next attempt
                # waits for this one: a slow model gets fewer attempts.
                next_free_ms = at + in_flight_ms
        cue = gen._parse(content)
        if cue is None:
            out["declines"] += 1
            continue
        if verify:
            v_started = clock()
            verdict = verify_cue(
                client, url, payload["model"], verify, turns, found, cue, verify_adapt
            )
            verify_ms = int((clock() - v_started) * 1000)
            out["verify_ms"].append(verify_ms)
            in_flight_ms += verify_ms
            if realtime:
                next_free_ms = at + in_flight_ms
            if verdict != "safe":
                out["verify_drops" if verdict == "unsafe" else "verify_errors"] += 1
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
        # The min interval runs from when the cue is shown: after the call live.
        last_emit_ms = at + in_flight_ms if realtime else at
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
    ap.add_argument(
        "--max-tokens", type=int, default=0,
        help="override the generator's max_tokens (default: the shipped value)",
    )
    ap.add_argument(
        "--extra", default="{}",
        help='JSON merged into every cue request, e.g. \'{"chat_template_kwargs": '
        '{"reasoning_effort": "low"}}\' or \'{"custom_params": {"thinking_budget": 512}}\'',
    )
    ap.add_argument(
        "--grounded", action="store_true",
        help="add live retrieval evidence (Tenir's LiveEvidenceRetriever), cached "
        "per transcript window in --evidence-cache",
    )
    ap.add_argument("--evidence-cache", type=Path, default=Path("evidence_cache.json"))
    ap.add_argument("--wikipedia", default="https://en.wikipedia.org")
    ap.add_argument("--kiwix", default="", help="Kiwix root (Wikipedia fallback)")
    ap.add_argument("--searxng", default="", help="SearXNG root")
    ap.add_argument("--searxng-engines", default="google cse,duckduckgo,mojeek")
    ap.add_argument(
        "--realtime", action="store_true",
        help="space attempts by the measured call (+ verify) latency, one in "
        "flight, instead of a fixed 2.5 s — use --workers 1 for faithful latency",
    )
    ap.add_argument(
        "--verify", default="", choices=["", "off", "low", "medium"],
        help="run the self-check on every emitted cue with this thinking mode "
        "('off' = thinking disabled; with --reasoning-effort it maps to "
        "reasoning_effort=low; with --no-template-kwargs no thinking control is "
        "sent, so the verifier runs at the model's default) and drop cues it "
        "calls unsafe",
    )
    ap.add_argument(
        "--refetch-empty", action="store_true",
        help="with --grounded: re-query windows cached with no evidence (a tier "
        "may have been rate-limited); repeat until the empty count stops falling",
    )
    args = ap.parse_args()
    try:
        extra = json.loads(args.extra)
    except json.JSONDecodeError as exc:
        ap.error(f"--extra is not JSON: {exc}")
    if not isinstance(extra, dict):
        ap.error("--extra must be a JSON object")
    if args.no_template_kwargs and args.verify in ("low", "medium"):
        # Without chat_template_kwargs there is no way to ask for a thinking mode.
        ap.error("--verify low/medium needs chat_template_kwargs; use --verify off here")

    evidence = None
    if args.grounded:
        from api.cue.retrieval.live import LiveEvidenceRetriever

        config = {
            "wikipedia": args.wikipedia, "kiwix": args.kiwix,
            "searxng": args.searxng, "searxng_engines": args.searxng_engines,
        }
        evidence = EvidenceCache(
            args.evidence_cache,
            lambda: LiveEvidenceRetriever(
                news_store=None,  # the news corpus lives in the deployment's DB
                wikipedia_endpoint=args.wikipedia,
                kiwix_endpoint=args.kiwix,
                searxng_endpoint=args.searxng,
                searxng_engines=args.searxng_engines,
                deadline_ms=2000,
                max_evidence=6,
            ),
            config,
            refetch_empty=args.refetch_empty,
        )

    by_conv = load_conversations(args.segments)
    conv_ids = [c for c in args.conversations.split(",") if c] or list(by_conv)
    unknown = [c for c in conv_ids if c not in by_conv]
    if unknown:
        ap.error(f"--conversations not in the export: {', '.join(unknown)}")
    gen_kwargs = {"max_tokens": args.max_tokens} if args.max_tokens else {}
    gen = OpenAICueGenerator(
        endpoint=args.endpoint, model=args.model, api_key=args.api_key, **gen_kwargs
    )
    adapt = None
    if args.reasoning_effort or args.no_template_kwargs:

        def adapt(payload: dict) -> dict:
            payload.pop("chat_template_kwargs", None)
            if args.reasoning_effort:
                payload["reasoning_effort"] = args.reasoning_effort
            return payload

        class _AdaptedGen(OpenAICueGenerator):
            def _build_payload(self, transcript, avoid_cues=(), evidence=()):
                return adapt(super()._build_payload(transcript, avoid_cues, evidence))

        gen = _AdaptedGen(
            endpoint=args.endpoint, model=args.model, api_key=args.api_key, **gen_kwargs
        )
    url = args.endpoint.rstrip("/") + "/chat/completions"

    results: list[dict | None] = [None] * len(conv_ids)
    lock = threading.Lock()
    index = iter(range(len(conv_ids)))
    failures: list[str] = []
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    def worker() -> None:
        with httpx.Client(headers=headers) as client:
            while True:
                with lock:
                    try:
                        i = next(index)
                    except StopIteration:
                        return
                try:
                    results[i] = replay_conversation(
                        gen, client, url, conv_ids[i], by_conv[conv_ids[i]],
                        extra=extra, evidence=evidence, realtime=args.realtime,
                        verify=args.verify, verify_adapt=adapt,
                    )
                except Exception as exc:  # a dead thread must fail the run, not shrink it
                    with lock:
                        failures.append(f"{conv_ids[i]}: {exc!r}")
                    continue
                done = results[i]
                print(f"{conv_ids[i][:8]}: {len(done['cues'])} cues / {done['attempts']} attempts")

    threads = [threading.Thread(target=worker) for _ in range(min(args.workers, len(conv_ids)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if evidence:
        try:  # keep what was fetched, even if the run then fails
            evidence.save()
        except OSError as exc:
            print(f"warning: evidence cache not saved: {exc}", file=sys.stderr)
    if failures:
        raise SystemExit("replay failed; no results written:\n" + "\n".join(failures))
    out = {
        # What produced these numbers, so runs can be told apart afterwards.
        "settings": {
            "model": args.model, "grounded": args.grounded, "realtime": args.realtime,
            "verify": args.verify, "max_tokens": args.max_tokens or None, "extra": extra,
            "reasoning_effort": args.reasoning_effort or None,
            "no_template_kwargs": args.no_template_kwargs,
        },
        "conversations": [r for r in results if r],
    }
    args.out.write_text(json.dumps(out, indent=2))
    convs = out["conversations"]
    total = sum(len(c["cues"]) for c in convs)
    attempts = sum(c["attempts"] for c in convs)
    call_ms = sum(c["call_ms_total"] for c in convs)
    mean = f", mean call {call_ms / attempts / 1000:.2f}s" if attempts else ""
    notes = []
    if args.verify:
        notes.append(
            f"verify dropped {sum(c['verify_drops'] for c in convs)} unsafe, "
            f"{sum(c['verify_errors'] for c in convs)} on verifier errors"
        )
    if evidence:
        notes.append(
            f"{sum(c['evidence_empty'] for c in convs)}/"
            f"{sum(c['evidence_windows'] for c in convs)} attempts had no evidence"
        )
    suffix = f" ({'; '.join(notes)})" if notes else ""
    print(f"total: {total} cues / {attempts} attempts{mean}{suffix} -> {args.out}")


if __name__ == "__main__":
    main()
