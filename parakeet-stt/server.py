"""OpenAI-compatible transcription server for NVIDIA Parakeet (XERK-92 follow-up).

Why this exists
---------------
Live STT today is Voxtral (Mistral) served by vLLM. Voxtral is an instruction-tuned
audio *LLM*: fed silence or noise it sometimes *answers* ("I'm sorry, I couldn't
hear that. Could you please repeat...") instead of transcribing — the whole class
of hallucination XERK-92 fought. A dedicated ASR model has no generative chat
objective and structurally cannot do that; NVIDIA Parakeet also leads the open ASR
leaderboards on accuracy and speed. ``parakeet-tdt-0.6b-v3`` is the multilingual
variant (~25 European languages incl. Spanish) with automatic language detection.

Parakeet is a NeMo model, not a vLLM one, so it can't ride the existing vLLM-STT
image. This tiny FastAPI app wraps NeMo behind the **same** OpenAI
``POST /v1/audio/transcriptions`` surface the api already speaks (via
``api.stt.voxtral.VoxtralEngine``), so evaluating Parakeet against Voxtral is a
routing change, not a code change: point ``API_LITELLM_ENDPOINT`` (or a LiteLLM
alias) at this server. Word timestamps — which the vLLM-Voxtral endpoint didn't
return — come back here, restoring per-word caption timing.

Response shape
--------------
Honors ``response_format`` = ``json`` (default) | ``text`` | ``verbose_json``.
``json`` returns ``{"text", "language", "words"}`` — the ``words`` array is a
superset of the OpenAI schema that ``VoxtralEngine`` already knows how to read
(``word``/``start``/``end``), so per-word timing flows through when the api points
straight at this server. ``verbose_json`` adds ``segments`` and ``duration`` for
LiteLLM's OpenAI transform, which rewrites ``json`` -> ``verbose_json`` to read
``duration`` for cost accounting (the very rewrite vLLM-Voxtral 400'd on — this
server supports it, so routing via the ``openai/`` provider works).

Two things to verify on first real run against the model card (guarded here, not
guessed): the per-hypothesis **language** field name, and the **word-timestamp**
dict keys. Both are read defensively and degrade to ``None``/empty rather than
crashing.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("parakeet-stt")

MODEL_NAME = os.environ.get("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
TARGET_SR = 16000  # Parakeet decodes 16 kHz mono; the api already sends exactly this.

app = FastAPI(title="tenir-parakeet-stt")

# One resident model on one GPU. NeMo's transcribe() isn't safe to call
# concurrently on the same model, so every decode runs under this lock (and off
# the event loop, in a worker thread). Live captioning concurrency is low, so
# serializing GPU calls is fine and keeps memory predictable.
_model = None
_model_lock = threading.Lock()
_ready = threading.Event()


def _load_model() -> None:
    """Load Parakeet once at startup. Heavy import kept out of module import so the
    process can start (and answer /health with 503) while the model downloads."""
    global _model
    import nemo.collections.asr as nemo_asr  # noqa: PLC0415 — deferred heavy import

    log.info("loading %s ...", MODEL_NAME)
    t0 = time.perf_counter()
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
    model.eval()
    _model = model
    _ready.set()
    log.info("model ready in %.1fs", time.perf_counter() - t0)


@app.on_event("startup")
async def _startup() -> None:
    # Load in a background thread so uvicorn binds the port immediately; /health
    # reports 503 until the weights are resident.
    threading.Thread(target=_load_model, name="model-loader", daemon=True).start()


@app.get("/health")
async def health() -> JSONResponse:
    if _ready.is_set():
        return JSONResponse({"status": "ok", "model": MODEL_NAME})
    return JSONResponse({"status": "loading", "model": MODEL_NAME}, status_code=503)


def _decode_audio(raw: bytes) -> np.ndarray:
    """Bytes of an uploaded audio file -> mono float32 at 16 kHz.

    The api always sends 16 kHz mono WAV, so this is usually a no-op decode; the
    downmix/resample guards keep off-rate callers (LiteLLM's health-probe clip,
    ad-hoc uploads) working instead of erroring, mirroring the PyAV robustness the
    vLLM-STT image added for Voxtral."""
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)  # downmix any channel count to mono
    if sr != TARGET_SR:
        import librosa  # noqa: PLC0415 — only needed for the off-rate path

        mono = librosa.resample(mono, orig_sr=sr, target_sr=TARGET_SR)
    return np.ascontiguousarray(mono, dtype=np.float32)


def _hyp_language(hyp: object, fallback: str | None) -> str | None:
    """Best-effort detected language off a NeMo hypothesis. v3 auto-detects; the
    exact attribute name varies by release, so probe the known candidates and fall
    back to the requested language (or None) rather than guessing one."""
    for attr in ("lang", "language", "langs", "languages"):
        val = getattr(hyp, attr, None)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (list, tuple)) and val and isinstance(val[0], str):
            return val[0]
    return fallback


def _hyp_words(hyp: object) -> list[dict]:
    """Word timestamps off a NeMo hypothesis, as OpenAI-style {word,start,end}.

    NeMo puts them under hyp.timestamp['word'] when transcribe(timestamps=True);
    each entry carries 'word' and start/end seconds (older builds expose only
    '*_offset' frame indices — those get dropped rather than mis-timed)."""
    ts = getattr(hyp, "timestamp", None)
    if not isinstance(ts, dict):
        return []
    words = []
    for w in ts.get("word", []) or []:
        text = w.get("word") or w.get("char") or ""
        if "start" in w and "end" in w:
            words.append({"word": text, "start": float(w["start"]), "end": float(w["end"])})
    return words


def _transcribe_sync(samples: np.ndarray, language: str | None) -> dict:
    """Run one decode under the GPU lock. Returns the fields all response formats
    are built from."""
    with _model_lock:
        # timestamps=True yields word/segment timing; language=None lets v3 auto-detect.
        kwargs: dict = {"timestamps": True}
        if language:
            kwargs["source_lang"] = language  # pin decoding when the caller knows the language
        try:
            out = _model.transcribe([samples], **kwargs)  # type: ignore[union-attr]
        except TypeError:
            # Older/newer NeMo signatures differ on kwargs (e.g. no source_lang); retry lean.
            out = _model.transcribe([samples], timestamps=True)  # type: ignore[union-attr]
    hyp = out[0] if out else None
    if hyp is None:
        return {"text": "", "language": language, "words": [], "segments": []}
    text = (getattr(hyp, "text", None) or (hyp if isinstance(hyp, str) else "") or "").strip()
    words = _hyp_words(hyp)
    segments = []
    ts = getattr(hyp, "timestamp", None)
    if isinstance(ts, dict):
        for i, s in enumerate(ts.get("segment", []) or []):
            if "start" in s and "end" in s:
                segments.append(
                    {
                        "id": i,
                        "start": float(s["start"]),
                        "end": float(s["end"]),
                        "text": (s.get("segment") or s.get("text") or "").strip(),
                    }
                )
    return {
        "text": text,
        "language": _hyp_language(hyp, language),
        "words": words,
        "segments": segments,
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default=MODEL_NAME),  # accepted + echoed; this server serves one model
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    # Accepted for OpenAI-client compatibility; unused by this ASR path.
    prompt: str | None = Form(default=None),
    temperature: float | None = Form(default=None),
) -> object:
    if not _ready.is_set():
        return JSONResponse({"error": "model still loading"}, status_code=503)

    raw = await file.read()
    samples = _decode_audio(raw)
    duration = round(len(samples) / TARGET_SR, 3)

    t0 = time.perf_counter()
    result = await asyncio.to_thread(_transcribe_sync, samples, language)
    log.info(
        "decoded %.2fs audio in %.0fms -> lang=%s, %d words",
        duration,
        (time.perf_counter() - t0) * 1000,
        result["language"],
        len(result["words"]),
    )

    fmt = (response_format or "json").lower()
    if fmt == "text":
        return PlainTextResponse(result["text"])
    if fmt == "verbose_json":
        return JSONResponse(
            {
                "task": "transcribe",
                "language": result["language"],
                "duration": duration,
                "text": result["text"],
                "segments": result["segments"],
                "words": result["words"],
            }
        )
    # default "json": OpenAI's {"text"} plus a superset "language"/"words" the api reads.
    return JSONResponse(
        {"text": result["text"], "language": result["language"], "words": result["words"]}
    )
