"""Replay the translation eval set through the SHIPPED translation prompt.

Builds each request with ``api.translate.openai.OpenAITranslator._build_payload``
from the installed ``api`` package — so what this measures is always the prompt
that ships — and parses responses with the shipped ``_parse``. Each eval item is
one finalized turn, translated exactly as the session worker would: the item's
``source_lang`` goes into the prompt's source clause (None == inherited run
segment, the model identifies the language itself).

Records per-call wall-clock ms and token usage so the same run yields both the
accuracy inputs (for judge.py) and the latency comparison. Production serializes
one translation in flight per session, so per-call latency is the number that
bounds how far the translation box lags a live speaker.

Usage: see scripts/translation_eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import httpx

from api.translate.openai import OpenAITranslator


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("eval_set", type=Path, help="eval_set.json from select_data.py")
    ap.add_argument("--endpoint", required=True, help="OpenAI-compatible /v1 base URL")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--out", type=Path, default=Path("results.json"))
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel requests; keep at or under the server's concurrency cap "
        "(OLLAMA_NUM_PARALLEL=4 in prod). Use 1 for clean latency numbers — "
        "production has one translation in flight at a time.",
    )
    ap.add_argument(
        "--conversations", default="", help="comma-separated conversation ids (default: all)"
    )
    ap.add_argument("--limit", type=int, default=0, help="first N items only (0 = all)")
    ap.add_argument(
        "--reasoning-effort",
        default="",
        choices=["", "low", "medium", "high"],
        help="for gpt-oss models: sets reasoning_effort on every request and strips "
        "the Qwen-specific chat_template_kwargs (LiteLLM injects medium in prod, "
        "so the production baseline is --reasoning-effort medium)",
    )
    ap.add_argument(
        "--no-template-kwargs",
        action="store_true",
        help="strip chat_template_kwargs without setting a reasoning effort — for "
        "servers that reject unknown fields",
    )
    args = ap.parse_args()

    items = json.loads(args.eval_set.read_text())
    conv_ids = {c for c in args.conversations.split(",") if c}
    if conv_ids:
        items = [i for i in items if i["conversation_id"] in conv_ids]
    if args.limit:
        items = items[: args.limit]

    tr = OpenAITranslator(endpoint=args.endpoint, model=args.model, api_key=args.api_key)
    if args.reasoning_effort or args.no_template_kwargs:

        class _AdaptedTr(OpenAITranslator):
            def _build_payload(self, text, source_lang=None):
                payload = super()._build_payload(text, source_lang)
                payload.pop("chat_template_kwargs", None)
                if args.reasoning_effort:
                    payload["reasoning_effort"] = args.reasoning_effort
                return payload

        tr = _AdaptedTr(endpoint=args.endpoint, model=args.model, api_key=args.api_key)
    url = args.endpoint.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    results: list[dict | None] = [None] * len(items)
    lock = threading.Lock()
    index = iter(range(len(items)))
    done = [0]

    def worker() -> None:
        with httpx.Client() as client:
            while True:
                with lock:
                    try:
                        i = next(index)
                    except StopIteration:
                        return
                item = items[i]
                payload = tr._build_payload(item["text"], item["source_lang"])
                rec = dict(item)
                started = time.monotonic()
                try:
                    resp = client.post(url, json=payload, headers=headers, timeout=120)
                    resp.raise_for_status()
                    body = resp.json()
                    content = OpenAITranslator._message_content(body["choices"][0]["message"])
                    rec["translation"] = OpenAITranslator._parse(content)
                    rec["raw_content"] = content if rec["translation"] is None else None
                    rec["usage"] = body.get("usage")
                except Exception as exc:  # noqa: BLE001 - record and move on
                    rec["translation"] = None
                    rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
                rec["call_ms"] = int((time.monotonic() - started) * 1000)
                results[i] = rec
                with lock:
                    done[0] += 1
                    if done[0] % 25 == 0:
                        print(f"  {done[0]}/{len(items)}")

    threads = [threading.Thread(target=worker) for _ in range(min(args.workers, len(items)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = {
        "endpoint": args.endpoint,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort or None,
        "workers": args.workers,
        "items": [r for r in results if r],
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    ok = [r for r in out["items"] if r.get("translation")]
    errs = sum(1 for r in out["items"] if r.get("error"))
    ms = sorted(r["call_ms"] for r in out["items"] if not r.get("error"))
    if ms:
        mean = sum(ms) / len(ms)
        p = lambda q: ms[min(len(ms) - 1, int(q * len(ms)))]  # noqa: E731
        print(
            f"{args.model}: {len(ok)}/{len(out['items'])} translated, {errs} errors, "
            f"call ms mean {mean:.0f} p50 {p(0.5)} p90 {p(0.9)} max {ms[-1]}"
        )
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
