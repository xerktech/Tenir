"""Offline HTTP transcription engine — Parakeet (XERK-92).

An OpenAI-compatible ``/audio/transcriptions`` client behind the same engine seam
faster-whisper used, so it reuses all of ``StreamingTranscriber``'s windowing /
VAD / partial-final cadence unchanged. In production it drives the Parakeet
server (``parakeet-stt/``), directly or through the LiteLLM gateway, sending
each window as an in-memory WAV. (It replaced the retired Voxtral audio-LLM —
see ``parakeet-stt/README.md`` for that history; a few defensive choices below
date from it.)

Network/model I/O, so excluded from coverage — CI runs the deterministic stub and
the windowing is covered by ``StreamingTranscriber`` tests against a fake engine;
the factory wiring is covered in ``tests/test_streaming_stt.py``.
"""

from __future__ import annotations

import logging

import numpy as np

from api.stt.engine import EngineResult, EngineWord, float32_to_wav

log = logging.getLogger("api.stt.parakeet")


class ParakeetEngine:
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

    def transcribe(  # pragma: no cover - requires httpx + a live STT endpoint
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        import httpx

        # response_format "json" (no per-word timestamps in the body itself):
        # chosen when the retired vLLM-Voxtral server 400'd on verbose_json, and
        # kept — the Parakeet server returns its words field either way, and
        # StreamingTranscriber falls back to segment-boundary timing without it.
        data = {"model": self._model, "response_format": "json"}
        if language is not None:
            data["language"] = language
        if not want_words:
            # Tenir extension (parakeet-stt/server.py): skip word-timestamp decoding
            # for partials, which only ever use the text. A server that doesn't know
            # the field ignores it and returns words anyway — correct, just not
            # cheaper — so this is safe to send through the gateway too.
            data["timestamps"] = "false"
        files = {"file": ("audio.wav", self._wav_bytes(samples), "audio/wav")}
        # The LiteLLM gateway requires a bearer token; a direct model server ignores
        # it (no key configured → no header sent).
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
