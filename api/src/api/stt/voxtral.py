"""Voxtral engine (master plan §5.2).

Voxtral (Mistral) behind the same ``WhisperEngine`` seam faster-whisper uses, so
it reuses all of ``StreamingTranscriber``'s windowing / VAD / partial-final cadence
unchanged. It calls an OpenAI-compatible ``/audio/transcriptions`` endpoint — a
vLLM-served Voxtral by default — sending each window as an in-memory WAV.

Voxtral leads open STT on ES⇄EN WER and ships a true-streaming "Realtime" variant;
this HTTP engine gets its accuracy through our existing windowing. Driving its
*native* sub-500 ms causal stream (instead of re-decoding windows) is the natural
follow-up behind this same seam.

Network/model I/O, so excluded from coverage — CI runs the deterministic stub and
the windowing is covered by ``StreamingTranscriber`` tests against a fake engine;
the factory wiring is covered in ``tests/test_streaming_stt.py``.
"""

from __future__ import annotations

import logging

import numpy as np

from api.stt.engine import EngineResult, EngineWord, float32_to_wav

log = logging.getLogger("api.stt.voxtral")


class VoxtralEngine:
    def __init__(
        self, *, endpoint: str, model: str, api_key: str = "", timeout: float = 15.0
    ) -> None:
        self._url = endpoint.rstrip("/") + "/audio/transcriptions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    @staticmethod
    def _wav_bytes(samples: np.ndarray) -> bytes:
        """Encode a mono float32 [-1, 1] window as 16 kHz s16le WAV in memory."""
        return float32_to_wav(samples)

    def transcribe(  # pragma: no cover - requires httpx + a live Voxtral endpoint
        self, samples: np.ndarray, *, language: str | None
    ) -> EngineResult:
        import httpx

        # vLLM-served Voxtral supports response_format "json"/"text" only — it returns
        # 400 "do not support verbose_json for voxtral". We use "json" (no per-word
        # timestamps); StreamingTranscriber falls back to segment-boundary timing.
        data = {"model": self._model, "response_format": "json"}
        if language is not None:
            data["language"] = language
        files = {"file": ("audio.wav", self._wav_bytes(samples), "audio/wav")}
        # The LiteLLM gateway requires a bearer token; a direct vLLM ignores it (no key
        # configured → no header sent).
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        resp = httpx.post(
            self._url, data=data, files=files, headers=headers, timeout=self._timeout
        )
        resp.raise_for_status()
        body = resp.json()
        # Word timestamps are returned only when the server supports them; absent,
        # the streaming layer falls back to segment-boundary timing.
        words = [
            EngineWord(
                text=w.get("word", ""),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
            )
            for w in body.get("words", [])
        ]
        return EngineResult(
            text=(body.get("text") or "").strip(),
            words=words,
            language=body.get("language") or language,
        )
