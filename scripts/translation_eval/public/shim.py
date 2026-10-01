"""Prompt-adapter shim between the Tenir API and a vLLM candidate.

The Tenir API sends its SHIPPED translation request (system prompt + JSON envelope)
to /v1/chat/completions here. MODE decides what reaches vLLM:

  shipped  forward the request untouched (only the model id rewritten) — what a
           zero-code swap to this model would do.
  hymt     Hy-MT2's documented native prompt (no system prompt, plain output).
  tgemma   TranslateGemma via the Infomaniak vLLM template
           (<<<source>>>es<<<target>>>en<<<text>>>...).
  milmmt   MiLMMT's documented completion prompt (/v1/completions).

Native modes re-wrap the plain output as {"translation": ...} so the API's shipped
_parse accepts it unchanged: this shim is the adapter a dedicated model would need in
api/src/api/translate/openai.py. The source language is read back out of the shipped
prompt's source clause; an inherited-run turn (no clause) falls back to RUN_LANG.

Run: UPSTREAM=http://127.0.0.1:8000/v1 SERVED_MODEL=<id> MODE=<mode> \
     python3 -m uvicorn shim:app --port 9000
"""

from __future__ import annotations

import json
import os
import re

CODES = {"Spanish": "es", "French": "fr", "German": "de", "Portuguese": "pt",
         "Italian": "it", "English": "en"}
HYMT_PROMPT = ("Translate the following text into English. Note that you should only "
               "output the translated result without any additional explanation:\n\n")


def source_language(system: str, run_lang: str = "Spanish") -> str:
    """The language name the shipped prompt claims (`..., spoken in Spanish, ...`)."""
    m = re.search(r"spoken in (\w+),", system)
    return m.group(1) if m else run_lang


def native_request(mode: str, model: str, text: str, src: str) -> tuple[str, dict]:
    """(endpoint path, body) for a model's own documented prompt format. Greedy, like
    the shipped payload."""
    if mode == "milmmt":
        return "/completions", {
            "model": model, "temperature": 0.0, "max_tokens": 512, "stop": ["\n"],
            "prompt": f"Translate this from {src} to English:\n{src}: {text}\nEnglish:"}
    if mode == "hymt":
        content, extra = HYMT_PROMPT + text, {"repetition_penalty": 1.05}
    elif mode == "tgemma":
        content, extra = f"<<<source>>>{CODES.get(src, 'es')}<<<target>>>en<<<text>>>{text}", {}
    else:
        raise ValueError(f"unknown native mode {mode!r}")
    return "/chat/completions", {"model": model, "temperature": 0.0, "max_tokens": 512,
                                 "messages": [{"role": "user", "content": content}], **extra}


def wrap(out: str, usage: dict | None) -> dict:
    """A plain translation as the chat response the shipped _parse expects."""
    content = json.dumps({"translation": (out or "").strip()}, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": usage}


def _make_app():  # pragma: no cover - needs fastapi + a live upstream
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    upstream = os.environ.get("UPSTREAM", "http://127.0.0.1:8000/v1")
    model = os.environ["SERVED_MODEL"]
    mode = os.environ.get("MODE", "shipped")
    run_lang = os.environ.get("RUN_LANG", "Spanish")
    app = FastAPI()
    client = httpx.AsyncClient(timeout=120)

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        msgs = body["messages"]
        if mode == "shipped":
            body["model"] = model
            r = await client.post(f"{upstream}/chat/completions", json=body)
            return JSONResponse(r.json(), status_code=r.status_code)
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        text = next(m["content"] for m in msgs if m["role"] == "user")
        path, payload = native_request(mode, model, text, source_language(system, run_lang))
        r = await client.post(upstream + path, json=payload)
        if r.status_code != 200:
            return JSONResponse(r.json(), status_code=r.status_code)
        choice = r.json()["choices"][0]
        out = choice["text"] if mode == "milmmt" else choice["message"]["content"]
        return wrap(out, r.json().get("usage"))

    return app


if "SERVED_MODEL" in os.environ:  # uvicorn shim:app
    app = _make_app()
