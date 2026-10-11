"""Model VAD for rooms too loud for the energy gate (XERK-1819).

Silero VAD v5.1.2 (MIT, https://github.com/snakers4/silero-vad), the bundled
``silero_vad.onnx``. It scores each 32 ms frame for speech from its spectrum, so it
can tell a talker from loud music at the same level — which an RMS threshold can't:
in the July–August 2026 music-room sessions the 100 ms level spans only ~4 dB from
its 10th to its 99th percentile, so no energy threshold ever finds a pause.

CPU only, ~0.4 % of one core per session in real time; it runs inline on
``StreamingTranscriber.push`` (no engine call, so the receive loop is never held).
"""

from __future__ import annotations

import functools
from pathlib import Path

import numpy as np

from api.stt.engine import SAMPLE_RATE, pcm16_to_float32

_MODEL = Path(__file__).with_name("silero_vad.onnx")
# The v5 model scores 512-sample frames at 16 kHz, each preceded by the last 64
# samples of the frame before it (the context its official wrapper carries).
_FRAME = 512
_CONTEXT = 64
# Most audio one chunk scores: 1 s. Clients send ~100 ms chunks; an oversized one
# (a 16 MB WS frame is ~8 min of audio) would otherwise hold the event loop for
# seconds. Only its tail decides the verdict anyway.
_MAX_SCORED = 16000


def preload() -> None:
    """Load the shared model now (~0.3 s), so the first session.start doesn't pay it."""
    _session()


@functools.cache
def _session():
    # Imported here so a deploy that turns the model off never loads onnxruntime.
    import onnxruntime as ort

    opts = ort.SessionOptions()
    # One frame is ~0.1 ms of work: thread fan-out costs more than it saves, and a
    # session per CPU core per live conversation would oversubscribe the pod.
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(_MODEL), opts, providers=["CPUExecutionProvider"])


class SileroVad:
    """One conversation's streaming speech scorer. The model is shared; the recurrent
    state, frame context and the samples not yet filling a frame are per instance."""

    def __init__(self) -> None:
        self._session = _session()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._last = 0.0

    def speech_prob(self, pcm: bytes) -> float:
        """Speech probability for this chunk: the highest frame score in it, so a
        word inside a mostly-quiet chunk still counts. A chunk too short to finish a
        frame repeats the previous score."""
        samples = np.concatenate([self._pending, pcm16_to_float32(pcm)])
        if len(samples) > _MAX_SCORED:
            # Keep the frame alignment of the remainder: drop whole frames off the front.
            drop = (len(samples) - _MAX_SCORED) // _FRAME * _FRAME
            if drop:
                # The first scored frame's context is what directly precedes it.
                self._context = samples[drop - _CONTEXT : drop]
                samples = samples[drop:]
        whole = len(samples) - len(samples) % _FRAME
        best = None
        for i in range(0, whole, _FRAME):
            frame = samples[i : i + _FRAME]
            out, self._state = self._session.run(
                None,
                {
                    "input": np.concatenate([self._context, frame])[None],
                    "state": self._state,
                    "sr": self._sr,
                },
            )
            self._context = frame[-_CONTEXT:]
            score = float(out[0][0])
            best = score if best is None else max(best, score)
        self._pending = samples[whole:]
        if best is not None:
            self._last = best
        return self._last
