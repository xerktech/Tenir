"""OpenAI-compatible transcription server for NVIDIA Parakeet (XERK-92 follow-up).

Why this exists
---------------
Live STT used to be Voxtral (Mistral) served by vLLM. Voxtral is an instruction-tuned
audio *LLM*: fed silence or noise it sometimes *answered* ("I'm sorry, I couldn't
hear that. Could you please repeat...") instead of transcribing — the whole class
of hallucination XERK-92 fought. A dedicated ASR model has no generative chat
objective and structurally cannot do that; NVIDIA Parakeet also leads the open ASR
leaderboards on accuracy and speed. ``parakeet-tdt-0.6b-v3`` is the multilingual
variant (~25 European languages incl. Spanish) with automatic language detection.
That A/B retired Voxtral; this server has been the production STT ever since.

Parakeet is a NeMo model, not a vLLM one, so it couldn't ride the old vLLM-STT
image. This tiny FastAPI app wraps NeMo behind the **same** OpenAI
``POST /v1/audio/transcriptions`` surface the api already speaks (via
``api.stt.parakeet.ParakeetEngine``), so swapping models was a routing change,
not a code change: point ``API_LITELLM_ENDPOINT`` (or a LiteLLM alias) at this
server. Word timestamps — which the vLLM-Voxtral endpoint didn't return — come
back here, restoring per-word caption timing.

Response shape
--------------
Honors ``response_format`` = ``json`` (default) | ``text`` | ``verbose_json``.
``json`` returns ``{"text", "language", "words"}`` — the ``words`` array is a
superset of the OpenAI schema that ``ParakeetEngine`` already knows how to read
(``word``/``start``/``end``), so per-word timing flows through when the api points
straight at this server. ``verbose_json`` adds ``segments`` and ``duration`` for
LiteLLM's OpenAI transform, which rewrites ``json`` -> ``verbose_json`` to read
``duration`` for cost accounting (the very rewrite vLLM-Voxtral 400'd on — this
server supports it, so routing via the ``openai/`` provider works).

A ``timestamps=false`` form field (a Tenir extension, XERK-115) skips word/segment
timing for callers that only want text — the api's live *partials*, which are most
of the request volume. It defaults to on, so any caller that doesn't know the field
keeps the old response.

Two things to verify on first real run against the model card (guarded here, not
guessed): the per-hypothesis **language** field name, and the **word-timestamp**
dict keys. Both are read defensively and degrade to ``None``/empty rather than
crashing.
"""

from __future__ import annotations

import asyncio
import inspect
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

# Seconds of audio decoded once at startup to force every lazy initialisation the
# first *real* request would otherwise pay for: CUDA context, cuDNN autotuning,
# NeMo's decoding setup. Without it the first utterance of a session is by far the
# slowest one (XERK-115).
WARMUP_SECONDS = 2.0

app = FastAPI(title="tenir-parakeet-stt")

# One resident model on one GPU. NeMo's transcribe() isn't safe to call
# concurrently on the same model, so every decode runs under this lock (and off
# the event loop, in a worker thread). Live captioning concurrency is low, so
# serializing GPU calls is fine and keeps memory predictable.
_model = None
_model_lock = threading.Lock()
_ready = threading.Event()

# Which of the optional kwargs below this NeMo build's transcribe() actually takes.
# Resolved once at load (see _supported_kwargs) instead of discovered by catching
# TypeError per decode: the blind retry that used to guard this dropped *every*
# kwarg on failure, which silently un-pinned the caller's language as a side effect
# of an unrelated signature change.
_OPTIONAL_KWARGS = ("timestamps", "source_lang")
_supported: frozenset[str] = frozenset(_OPTIONAL_KWARGS)


def _supported_kwargs(model: object) -> frozenset[str]:
    """The subset of _OPTIONAL_KWARGS this model's transcribe() accepts by name.

    A signature we can't read at all is treated as accepting everything — the same
    optimistic assumption as before, but now it degrades on introspection failure
    rather than on the first decode.
    """
    try:
        params = inspect.signature(model.transcribe).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError):  # builtins / C-implemented callables
        return frozenset(_OPTIONAL_KWARGS)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        # NeMo forwards **config_kwargs into the decode config, so a name that isn't
        # an explicit parameter may still be honoured. Assume it is — _transcribe_sync
        # narrows the set for good if a real call turns out to reject it.
        return frozenset(_OPTIONAL_KWARGS)
    return frozenset(name for name in _OPTIONAL_KWARGS if name in params)


def _warmup() -> None:
    """Decode a throwaway clip so the first real request doesn't pay for setup."""
    t0 = time.perf_counter()
    try:
        _transcribe_sync(np.zeros(int(TARGET_SR * WARMUP_SECONDS), dtype=np.float32), None, True)
    except Exception:  # noqa: BLE001 — a failed warmup must never keep the server down
        log.warning("warmup decode failed; serving anyway", exc_info=True)
        return
    log.info("warmup decode in %.0fms", (time.perf_counter() - t0) * 1000)


def _load_model() -> None:
    """Load Parakeet once at startup. Heavy import kept out of module import so the
    process can start (and answer /health with 503) while the model downloads."""
    global _model, _supported
    import nemo.collections.asr as nemo_asr  # noqa: PLC0415 — deferred heavy import

    log.info("loading %s ...", MODEL_NAME)
    t0 = time.perf_counter()
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
    model.eval()
    _model = model
    _supported = _supported_kwargs(model)
    log.info("transcribe() accepts: %s", ", ".join(sorted(_supported)) or "(none)")
    # Warm up BEFORE going ready, so nothing is routed here until decodes are fast.
    _warmup()
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


def _transcribe_sync(samples: np.ndarray, language: str | None, timestamps: bool) -> dict:
    """Run one decode under the GPU lock. Returns the fields all response formats
    are built from.

    ``timestamps`` False skips word/segment timing entirely. The api's *partials* —
    several requests a second per session, and by far the bulk of the traffic — only
    ever use the text, so not computing timing for them takes real work off the hot
    path. Finals still ask for it.
    """
    global _supported
    with _model_lock:
        # language=None lets v3 auto-detect; a pinned language constrains decoding.
        kwargs: dict = {}
        if "timestamps" in _supported:
            kwargs["timestamps"] = timestamps
        if language and "source_lang" in _supported:
            kwargs["source_lang"] = language
        try:
            out = _model.transcribe([samples], **kwargs)  # type: ignore[union-attr]
        except TypeError:
            if not kwargs:
                raise
            # This build really doesn't take them. Narrow the set permanently rather
            # than paying a failed call on every decode from here on.
            log.warning("transcribe() rejected %s; dropping them", sorted(kwargs))
            _supported = frozenset()
            out = _model.transcribe([samples])  # type: ignore[union-attr]
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
    # Tenir extension (XERK-115): "false" skips word/segment timing for callers that
    # only want the text — the api's live *partials*, which are most of the traffic.
    # Defaults on, so an OpenAI client that never heard of it still gets timestamps,
    # and so does verbose_json below.
    timestamps: bool = Form(default=True),
    # Accepted for OpenAI-client compatibility; unused by this ASR path.
    prompt: str | None = Form(default=None),
    temperature: float | None = Form(default=None),
) -> object:
    if not _ready.is_set():
        return JSONResponse({"error": "model still loading"}, status_code=503)

    raw = await file.read()
    samples = _decode_audio(raw)
    duration = round(len(samples) / TARGET_SR, 3)

    # verbose_json's contract includes segments, so never skip timing for that format
    # however the caller set the flag.
    want_timestamps = timestamps or (response_format or "").lower() == "verbose_json"

    t0 = time.perf_counter()
    result = await asyncio.to_thread(_transcribe_sync, samples, language, want_timestamps)
    log.info(
        "decoded %.2fs audio in %.0fms -> lang=%s, %d words%s",
        duration,
        (time.perf_counter() - t0) * 1000,
        result["language"],
        len(result["words"]),
        "" if want_timestamps else " (timing skipped)",
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
