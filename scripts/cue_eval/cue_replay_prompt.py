"""Prompt-variant cue replay for Qwen.

Reuses the SHIPPED api.cue.openai prompt (imported verbatim, so every accuracy
rule is byte-identical to what ships) and swaps ONLY the emission-posture tail
of the candidate-discipline paragraph. This isolates the single variable that
matters for the qwen under-emission problem: how strongly the prompt tells the
model to fire when a candidate is safe.

Variants (each only rewrites the "declining is a normal outcome" tail of the
candidate-discipline paragraph; all accuracy rules stay untouched):
  v0  shipped prompt unchanged (baseline; thinking off -> 6 cues/795)
  v1  balanced: declining is normal for routine talk, but substantive turns
       usually DO have a cue; emit when a candidate passes the accuracy rules.
  v2  emissive: stronger expectation that most substantive turns carry a cue;
       caution alone is not a reason to decline.

Output mirrors replay.py's schema so judge.py can grade it directly.
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
    cue_substance_similarity,
    cue_substance_tokens,
    cue_subject_tokens,
    normalize_cue_title,
)
from api.cue.openai import OpenAICueGenerator

CONTEXT_SEGMENTS = 8
ATTEMPT_MS = 2500
AVOID_LIMIT = 40
DUP_THRESHOLD = 0.35

# The exact tail the shipped prompt uses for the candidate-discipline paragraph
# (post-format). We replace this, and only this.
_SHIPPED_TAIL = (
    "Declining is a normal outcome, not a failure — in long stretches of "
    "routine talk the correct answer is {\"cue\": false} turn after turn, and a "
    "quiet run is never a reason to lower the bar."
)

_V1_TAIL = (
    "Declining is a normal outcome when the talk is genuinely routine or small "
    "talk — there, the correct answer is {\"cue\": false}. But in a substantive "
    "conversation that actively discusses entities, questions, technical terms, "
    "or decisions, a cue-worthy candidate is present on most turns, and emitting "
    "a well-grounded, accurate cue is the expected behaviour, not the exception. "
    "A quiet run in a content-rich conversation is a defect to watch, not a virtue. "
    "Do not decline out of caution alone: if a candidate passes every accuracy "
    "rule above and adds information these listeners plausibly do not already "
    "know, emit it. Only the accuracy rules gate emission — a safe, certain, "
    "genuinely additive fact should surface."
)

_V2_TAIL = (
    "In a substantive conversation — one actively discussing entities, "
    "questions, technical terms, plans, or decisions — you should EXPECT to "
    "surface a cue on most turns where any candidate survives the accuracy "
    "rules; under-emission (too few cues in a content-rich conversation) is a "
    "defect just as bad as an over-emission. Declining {\"cue\": false} is "
    "correct only for genuine routine talk, small talk, or when no candidate "
    "actually passes the accuracy rules. If your best candidate is a garbled "
    "name, a fact you cannot verify, or already surfaced, check the next; if "
    "none passes, decline. But a safe, certain, genuinely additive fact about a "
    "topic the conversation is engaging with is cue-worthy — emit it. Caution "
    "alone is never a reason to stay silent; the accuracy rules above are the "
    "only gate."
)

_V4_TAIL = (
    "When the conversation is substantive — actively discussing entities, "
    "questions, terms, or decisions — expect to surface a cue on most turns: "
    "scan the newest turns for a candidate that passes every accuracy rule "
    "above and that these listeners would plausibly not know, and emit it. "
    "Declining {\"cue\": false} is correct for routine small talk, or when no "
    "candidate survives the accuracy rules; a quiet run in a content-rich "
    "conversation is a defect, not a virtue."
)

VARIANTS = {"v0": None, "v1": _V1_TAIL, "v2": _V2_TAIL, "v3": _V1_TAIL, "v4": _V4_TAIL, "v5": None}

# GOOD worked examples, one per major trigger, appended after the shipped
# BAD examples (which run 8:2 against GOOD). v3 = v1 tail + examples;
# v4 = v4 tail + examples.
_GOOD_EXAMPLES = (
    'Speaker: "How far up do planes fly?" -> GOOD cue {"cue": true, "title": '
    '"Cruise Altitude", "body": "Long-haul jets cruise around 10,700 to 12,000 m '
    '— the thin air there means less drag and much better fuel burn."} (answers a '
    "spoken question with a number the listeners did not have).\n"
    'Speaker: "Did you hear SpaceX landed the booster on the drone ship again?" -> '
    'GOOD cue {"cue": true, "title": "Falcon 9 Reuse", "body": "The Falcon 9 first '
    'stage is recovered and reflown — a single booster has launched more than a '
    'dozen times, which is what drives the launch cost down."} (adds a concrete '
    "fact about a named product the conversation is engaging with).\n"
    'Speaker: "The doctor says a stent might be needed" -> GOOD cue {"cue": true, '
    '"title": "Stent", "body": "A stent is a small mesh tube a doctor threads into '
    'a narrowed artery to hold it open, usually expanded with a balloon."} (a term '
    "from outside the speakers' field, defined once).\n"
    'Speakers: "I keep my backups on a second drive in the same closet" -> GOOD '
    'cue {"cue": true, "title": "Offsite Backup", "body": "Two copies in one place '
    'survive a dead disk but not a fire or theft — the standard rule is 3-2-1: '
    'three copies, two media, one offsite."} (informs a decision with a '
    "trade-off the speakers had not mentioned).\n"
)
_EXAMPLE_ANCHOR = "Reply with a single JSON object and nothing else: "
EXAMPLES = {"v3": _GOOD_EXAMPLES, "v4": _GOOD_EXAMPLES}

# v5: a SHORT, emission-first full-prompt rewrite. Keeps the core accuracy
# rules (no garbled-name facts, no cross-generation confabulation, no
# contradicting firsthand details, certain-only) but drops most of the
# "when to stay silent" framing and leads with firing. Full system-prompt
# replacement (not a tail swap).
_V5_PROMPT = '''You are a live research assistant. You listen to an ongoing conversation and silently surface short, accurate cues — private notes only the listener sees. A cue ADDS information the speakers did not say aloud. Repeating or summarizing what they said is worthless, so if you have nothing new, reply {"cue": false}.
You should surface a cue on most turns of a substantive conversation. Fire when you can add ANY of the following, picking the best candidate from the newest turns:
1) A factual question was asked and you know the answer with certainty.
2) A person, place, product, or event is being engaged with and you can add a concrete fact the speakers did not mention.
3) A technical term or piece of jargon appears that these listeners may not know — define it in one plain sentence.
4) A decision or problem is being worked through and a relevant number, precedent, or trade-off would inform it.
5) A claim is clearly wrong and you know the correct fact.
Accuracy is absolute:
- State only what you are certain of; if unsure, stay silent.
- Never invent facts about a name you do not recognize or that sounds garbled.
- Never contradict a firsthand detail the speakers stated about something they are looking at.
- Facts that change over time (current versions, prices, officeholders, recent events) may be past your knowledge; if not confident, stay silent.
- Never present a sibling model's specs, a predecessor's dates, or a rival product's defaults as the named thing's own.
Reply with a single JSON object and nothing else: {"cue": true, "title": "1-3 word label", "body": "one or two short sentences under 200 characters"}. If nothing is cue-worthy, reply {"cue": false}.'''
_V6_PROMPT = (
    _V5_PROMPT.replace(
        "- State only what you are certain of; if unsure, stay silent.\n",
        "- State only what you are certain of; if unsure, stay silent.\n"
        "- A cue must be a definite statement. If it would need 'likely', "
        "'probably', 'seems to', or 'may refer to' to be honest, it is a "
        "guess — do not emit it. Never guess what a garbled or under-supported "
        "name, acronym, or tool refers to.\n",
    )
)
# v7: v5's emission-first frame plus four compact guardrails targeted at v5's
# judged failure classes: garbled-name guessing (Vitra->VirusTotal/IDA),
# invented acronym expansions (BSU), cross-generation/'latest' claims
# (Snapdragon 8 Elite Gen 5, Gemini Nano 4), firsthand-detail contradiction
# (LPDDR5X 'soldered' vs transcript 'buy your own RAM').
_V7_PROMPT = '''You are a live research assistant. You listen to an ongoing conversation and silently surface short, accurate cues — private notes only the listener sees. A cue ADDS information the speakers did not say aloud. Repeating or summarizing what they said is worthless, so if you have nothing new, reply {"cue": false}.
You should surface a cue on most turns of a substantive conversation. Fire when you can add ANY of the following, picking the best candidate from the newest turns:
1) A factual question was asked and you know the answer with certainty.
2) A person, place, product, or event is being engaged with and you can add a concrete fact the speakers did not mention.
3) A technical term or piece of jargon appears that these listeners may not know — define it in one plain sentence.
4) A decision or problem is being worked through and a relevant number, precedent, or trade-off would inform it.
5) A claim is clearly wrong and you know the correct fact.
Accuracy is absolute:
- State only what you are certain of; if unsure, stay silent.
- Never invent facts about a name you do not recognize or that sounds garbled.
- Never contradict a firsthand detail the speakers stated about something they are looking at.
- Facts that change over time (current versions, prices, officeholders, recent events) may be past your knowledge; if not confident, stay silent.
- Never present a sibling model's specs, a predecessor's dates, or a rival product's defaults as the named thing's own.
Confabulation guardrails:
- A name that appears once in fragmented, half-finished speech with nothing around it engaging it is a mishearing — skip it. Do not map a garbled word to a famous brand, tool, or product that merely resembles it: the speakers saying a tool's name badly is not an invitation to name a different tool.
- Acronyms resolve within the conversation's own domain or not at all: if the only expansion you know belongs to a different field, you do not know this acronym — skip it, never invent an expansion.
- The speakers are looking at what they are describing. Never state a fact that contradicts a measurement, name, or plan they themselves stated; you are not in the room, they are.
- Recognizing a product line is not knowing the specific model named. If you cannot specifically recall THAT exact model — not a sibling, predecessor, or the brand itself — say nothing about it, including whether it is 'the latest'.
Reply with a single JSON object and nothing else: {"cue": true, "title": "1-3 word label", "body": "one or two short sentences under 200 characters"}. If nothing is cue-worthy, reply {"cue": false}.'''
FULL_PROMPTS = {"v5": _V5_PROMPT, "v6": _V6_PROMPT, "v7": _V7_PROMPT}


class VariantGen(OpenAICueGenerator):
    def __init__(self, variant: str, thinking: bool = False, max_tokens: int = 600, **kw):
        super().__init__(**kw)
        self.variant = variant
        self.enable_thinking = thinking
        self.max_tokens = max_tokens

    def _build_payload(self, transcript, avoid_cues=(), evidence=()):
        payload = super()._build_payload(transcript, avoid_cues, evidence)
        payload["max_tokens"] = self.max_tokens
        payload["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        full = FULL_PROMPTS.get(self.variant)
        if full is not None:
            # v5 replaces the entire system prompt; keep the avoid/evidence
            # blocks that super() appended, then swap the base frame.
            sysmsg = payload["messages"][0]
            content = sysmsg["content"]
            base, sep, tail_blocks = content.partition(_SHIPPED_TAIL)
            sysmsg["content"] = full + ("\n" + tail_blocks if tail_blocks else "")
            return payload
        tail = VARIANTS.get(self.variant)
        if tail is not None:
            sysmsg = payload["messages"][0]
            content = sysmsg["content"]
            if _SHIPPED_TAIL in content:
                sysmsg["content"] = content.replace(_SHIPPED_TAIL, tail)
            else:
                # fall back to appending if the tail text drifted
                sysmsg["content"] = content + "\n" + tail
        examples = EXAMPLES.get(self.variant)
        if examples:
            sysmsg = payload["messages"][0]
            content = sysmsg["content"]
            if _EXAMPLE_ANCHOR in content:
                sysmsg["content"] = content.replace(
                    _EXAMPLE_ANCHOR, examples + _EXAMPLE_ANCHOR
                )
            else:
                sysmsg["content"] = content + "\n" + examples
        return payload


def load_conversations(path: Path) -> dict:
    by_conv = collections.defaultdict(list)
    for seg in json.loads(path.read_text()):
        by_conv[seg["conversation_id"]].append(seg)
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])
    return by_conv


def replay_conversation(gen, client, url, conv_id, segments):
    recent = collections.deque(maxlen=CONTEXT_SEGMENTS)
    surfaced = []
    norms = set()
    substance = []
    subjects = set()
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
        if at < next_free_ms or at - last_emit_ms < 1500:
            continue
        out["attempts"] += 1
        next_free_ms = at + ATTEMPT_MS
        payload = gen._build_payload("\n".join(recent), surfaced[-AVOID_LIMIT:])
        started = time.monotonic()
        try:
            resp = client.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            content = OpenAICueGenerator._message_content(
                resp.json()["choices"][0]["message"]
            )
        except Exception:
            out["errors"] += 1
            continue
        finally:
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
    ap.add_argument("segments", type=Path)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--variant", choices=sorted(set(VARIANTS) | set(FULL_PROMPTS)), default="v1")
    ap.add_argument("--conversations", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--thinking", choices=["on", "off"], default="off")
    ap.add_argument("--max-tokens", type=int, default=600)
    args = ap.parse_args()

    by_conv = load_conversations(args.segments)
    conv_ids = [c for c in args.conversations.split(",") if c] or list(by_conv)

    gen = VariantGen(
        args.variant,
        thinking=args.thinking == "on",
        max_tokens=args.max_tokens,
        endpoint=args.endpoint,
        model=args.model,
        api_key=args.api_key,
    )
    url = args.endpoint.rstrip("/") + "/chat/completions"

    results = [None] * len(conv_ids)
    lock = threading.Lock()
    index = iter(range(len(conv_ids)))

    def worker():
        with httpx.Client() as client:
            while True:
                with lock:
                    try:
                        i = next(index)
                    except StopIteration:
                        return
                results[i] = replay_conversation(
                    gen, client, url, conv_ids[i], by_conv[conv_ids[i]]
                )
                done = results[i]
                print(
                    f"[{args.variant}] {conv_ids[i][:8]}: {len(done['cues'])} cues / "
                    f"{done['attempts']} attempts",
                    flush=True,
                )

    threads = [threading.Thread(target=worker) for _ in range(min(args.workers, len(conv_ids)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = {"conversations": [r for r in results if r], "variant": args.variant}
    args.out.write_text(json.dumps(out, indent=2))
    total = sum(len(c["cues"]) for c in out["conversations"])
    attempts = sum(c["attempts"] for c in out["conversations"])
    call_ms = sum(c["call_ms_total"] for c in out["conversations"])
    mean = f", mean call {call_ms / attempts / 1000:.2f}s" if attempts else ""
    print(f"[{args.variant}] total: {total} cues / {attempts} attempts{mean}")


if __name__ == "__main__":
    main()
