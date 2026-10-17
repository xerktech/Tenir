"""Offline HTTP transcription engine (XERK-92; named for its first model, Parakeet).

An OpenAI-compatible ``/audio/transcriptions`` client behind the same engine seam
faster-whisper used, so it reuses all of ``StreamingTranscriber``'s windowing /
VAD / partial-final cadence unchanged. In production it drives Whisper
large-v3-turbo on vLLM (XERK-1476; Parakeet, ``parakeet-stt/``, before it),
directly or through the LiteLLM gateway, sending each window as an in-memory WAV.

The windowing is covered by ``StreamingTranscriber`` tests against a fake engine;
the request shape and the whole-request deadline by ``tests/test_stt_backends.py``
(the latter against a real local server that trickles its response).
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

import numpy as np

from api.stt.engine import EngineResult, EngineWord, float32_to_wav

log = logging.getLogger("api.stt.parakeet")

# Whisper's canonical output for a window with no speech in it (XERK-1476): mostly
# video-subtitle outros it learned, in whatever language it guesses. vLLM reports no
# no_speech_prob, and on FLEURS streams neither avg_logprob nor RMS separated these from real
# short turns, so a decode whose WHOLE text is one of them is treated as blank: it then takes
# the ordinary blank-final path (padded retry, XERK-174 partial fallback). A real one-phrase
# "Thank you." is lost with them — an English turn Tenir never translates. Compared with
# sound-event notes ("*sad music*", "[Music]") removed, then punctuation and case; "" covers
# a decode that was only those (".", "-" on noise).
_NON_SPEECH = frozenset(
    {
        "",
        "you",
        "thank you",
        "thanks for watching",
        "thank you for watching",
        "well be right back",
        "subtítulos realizados por la comunidad de amaraorg",
        "subtítulos por la comunidad de amaraorg",
        "soustitres réalisés par la communauté damaraorg",
        # Seen in QA on noise between turns (auto, or with es/fr pinned). Bare "Gracias." /
        # "Merci." also came out, but are left alone: they are real replies too.
        "gracias por ver el video",
        "gracias por ver el vídeo",
        "gracias por ver",
        "soustitrage société radiocanada",
        "soustitrage st 501",
        "ご視聴ありがとうございました",
        "продолжение следует",
        # Recorded sessions, Oct 2026: whole decodes over noise, music or silence. Every one of
        # the 15 stored (12 + 3) sat between turns or inside lyrics; none was said. Bare "I'm
        # going to go." is left alone: people say it when leaving.
        "im going to go to the next one",
        "im going to go to the restaurant",
    }
)
_SOUND_EVENT = re.compile(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)")


def _non_speech(text: str) -> bool:
    bare = re.sub(r"[^\w\s]|_", "", _SOUND_EVENT.sub(" ", text).lower())
    if " ".join(bare.split()) in _NON_SPEECH:
        return True
    # Every contract language (Lang: en es fr de pt it) is written in Latin script, so a decode
    # whose letters are all in another script ("はい", "ありがとうございました。", "Это сейчас
    # не...") cannot be a turn Tenir captions or translates. On recorded sessions all ~25
    # stored CJK decodes were Whisper output on noise or English speech, Oct 2026.
    letters = [ch for ch in bare if ch.isalpha()]
    return bool(letters) and not any(unicodedata.name(ch, "").startswith("LATIN") for ch in letters)


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

    async def _post(self, *, data: dict, files: dict, headers: dict) -> dict:
        """POST one window, bounded by a single deadline over the whole request.

        httpx's ``timeout`` is per phase and its read timeout is per chunk, so an
        upstream that trickles bytes (a degraded proxy or port-forward) would hold a
        decode — and, with one decode worker per session, that session's whole
        caption backlog — indefinitely (XERK-1448). ``wait_for`` caps connect +
        upload + headers + body together; on expiry it raises ``TimeoutError``
        like any other failed decode. ``transcribe`` runs in a worker thread
        (``asyncio.to_thread``) with no loop of its own, so it can run a private loop.
        """
        import httpx

        async def _request() -> dict:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, data=data, files=files, headers=headers)
                resp.raise_for_status()
                return resp.json()

        return await asyncio.wait_for(_request(), self._timeout)

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        # verbose_json: vLLM's Whisper returns the detected language only in it (plain
        # json is text alone; LiteLLM forwards the format as sent). Per-turn language
        # from the model beat text langid on every turn measured (XERK-1476). Word
        # timing rides the body's words field when the server has it;
        # StreamingTranscriber falls back to segment timing without.
        data = {"model": self._model, "response_format": "verbose_json"}
        if language is not None:
            data["language"] = language
        if not want_words:
            # Tenir extension (parakeet-stt/server.py): skip word-timestamp decoding
            # for partials, which only ever use the text. A server that doesn't know
            # the field (vLLM) ignores it and returns words anyway — correct, just not
            # cheaper — so this is safe to send through the gateway too.
            data["timestamps"] = "false"
        files = {"file": ("audio.wav", self._wav_bytes(samples), "audio/wav")}
        # The LiteLLM gateway requires a bearer token; a direct model server ignores
        # it (no key configured → no header sent).
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        # Not asyncio.run: on exit it joins the default executor, so a hung DNS lookup
        # (getaddrinfo runs in that executor) would outlast the deadline. close() shuts
        # the executor down without waiting; the stuck thread ends with the resolver.
        loop = asyncio.new_event_loop()
        try:
            body = loop.run_until_complete(self._post(data=data, files=files, headers=headers))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        text = (body.get("text") or "").strip()
        if _non_speech(text):
            return EngineResult(text="", words=[], language=language, non_speech=True)
        # Word timestamps are returned only when the server supports them; absent (vLLM
        # sends "words": null), the streaming layer falls back to segment-boundary timing.
        words = [
            EngineWord(
                text=w.get("word", ""),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
            )
            for w in body.get("words") or []
        ]
        return EngineResult(
            text=text,
            words=words,
            language=body.get("language") or language,
        )
