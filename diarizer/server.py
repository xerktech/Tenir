"""tenir-diarizer: who spoke when, and who said what when two talk at once (XERK-2958).

One mono mic hears the whole table. For each finalized caption turn the api posts the
turn's PCM here; the server

1. feeds it into that session's **streaming Sortformer** (NVIDIA
   ``diar_streaming_sortformer_4spk-v2``). Its speaker cache carries every speaker
   heard so far through the session, and it numbers speakers by arrival order, so
   "Speaker 2" is the same person from the first minute to the last;
2. cuts the turn into per-speaker pieces (``planner.plan_pieces``);
3. for a piece two people spoke over each other, runs a 2-speaker separator
   (SpeechBrain SepFormer, WHAMR! 16 kHz: noisy, reverberant rooms) and returns that
   speaker's separated stream, so the api transcribes each voice on its own.

The api transcribes the pieces with the normal STT route. Without this service (or on any
error) the api captions the turn whole and unlabelled, exactly as before.

Sessions are kept in memory, keyed by the api's conversation id. A restart forgets them;
the next turn starts a fresh speaker cache, so numbering restarts for that conversation.
That is the accepted cost of keeping the service stateless on disk.
"""

from __future__ import annotations

import base64
import copy
import hmac
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

import planner

log = logging.getLogger("tenir-diarizer")

SAMPLE_RATE = 16_000
HOP = 160  # 10 ms feature hop
SUB = 8  # feature frames per diarizer frame
FRAME_SAMPLES = HOP * SUB  # 80 ms
# A session idle this long is dropped (a conversation that ended, or one whose api pod
# went away). Long enough to outlast a paused session.
SESSION_TTL_S = float(os.environ.get("DIAR_SESSION_TTL_S", "1800"))
# Past this many live conversations the least recently used is forgotten (its numbering
# restarts). A session is ~100 KB of state, so the bound is generous on purpose: 32 could
# evict a conversation still talking (QA).
MAX_SESSIONS = int(os.environ.get("DIAR_MAX_SESSIONS", "256"))
# A jump in the timeline up to this long is fed as silence (a turn the api dropped after a
# timeout). A longer one restarts the clock there, keeping the speakers: feeding minutes of
# zeros through the model cost 56 s of CPU for one 32 KB request (QA).
MAX_GAP_S = float(os.environ.get("DIAR_MAX_GAP_S", "10"))
# offset_ms ceiling: a week of conversation timeline.
MAX_OFFSET_MS = 7 * 24 * 3600 * 1000
# Activity kept per session for turns that reach back before the newest commit. A turn
# is at most the api's segment cap (28 s) plus a merged backlog; 120 s covers both.
KEEP_FRAMES = int(120 / planner.FRAME_S)
# One request carries one turn; refuse anything a turn cannot be (5 min of PCM).
MAX_BODY = SAMPLE_RATE * 2 * 300
# SepFormer's memory grows with the window; a longer overlapped piece is returned as the
# raw mix (the api then captions the dominant voice, as without separation). The api's
# turns are capped at 8 s by default, so this only bites on merged backlog turns.
MAX_SEPARATE_S = float(os.environ.get("DIAR_MAX_SEPARATE_S", "12"))
# Separation runs only on talk shared this long with another speaker. Brief hand-offs
# (one starts as the other finishes) are captioned from the mix: Whisper already hears both
# there, and separating them only added invented words (scripts/diarize_eval, Nov 2026).
MIN_SEPARATE_OVERLAP_S = float(os.environ.get("DIAR_MIN_SEPARATE_OVERLAP_S", "1.5"))
# Shared bearer secret the api sends on /v1/diarize (XERK-3005). Without it anyone who can
# reach the service and knows a conversation id could post audio into that conversation's
# speaker cache (re-numbering its speakers) or read its speaker activity back. Unset, the
# service refuses every turn (503): the api then captions unlabelled, never unauthenticated.
API_KEY = os.environ.get("DIAR_API_KEY", "")


class Abandoned(Exception):
    """The api hung up before its turn was done (it gives up after its timeout). The
    turn is dropped so the next one does not queue behind work nobody will read."""


Check = Callable[[], bool]


class Stream(Protocol):
    """One session's streaming diarizer: feed audio, read frame activity."""

    samples: int  # session samples received so far

    def start_at(self, sample: int) -> None:
        """Move the timeline to ``sample`` without feeding the gap. The speakers heard so
        far are kept; the scored history before the jump is dropped."""
        ...

    def feed(self, audio: np.ndarray, abandoned: Check | None = None) -> None:
        """Feed ``audio``. ``abandoned`` is polled between model steps; when it turns true
        :class:`Abandoned` is raised. The state stays consistent: the audio is buffered
        and the chunks not yet scored are scored by the next feed."""
        ...

    def activity(self, start_frame: int, end_frame: int) -> np.ndarray:
        """frames x speakers activity for [start, end) on the session timeline. Frames
        past the committed head are scored provisionally (no right context yet) and do
        not change the session's speaker cache."""
        ...


class Separator(Protocol):
    def separate(self, audio: np.ndarray) -> np.ndarray:
        """float32 mono → 2 x T float32 streams."""
        ...


# --------------------------------------------------------------------------- engine ---


class SortformerStream:
    """Incremental driver of NeMo's streaming Sortformer for one session.

    NeMo's own ``forward_streaming`` needs the whole recording up front; the api delivers a
    turn at a time. This keeps the model's streaming state and an audio tail, and commits a
    chunk only once its right context has arrived, so committed frames are scored exactly
    as the offline streaming pass scores them. The tail past the last committed chunk is
    scored on a *copy* of the state, so a turn's last second gets a label now without
    the speaker cache learning from audio it has not finished hearing.
    """

    def __init__(self, model: Any, lock: threading.Lock) -> None:
        self._m = model
        self._mods = model.sortformer_modules
        self._lock = lock
        with lock:
            self._state = self._mods.init_streaming_state(
                batch_size=1, async_streaming=False, device=model.device
            )
        self._chunk = self._mods.chunk_len  # diar frames
        self._lc = self._mods.chunk_left_context
        self._rc = self._mods.chunk_right_context
        self.samples = 0  # session samples received
        self._buf = np.zeros(0, np.float32)  # audio from _buf_origin
        self._buf_origin = 0
        self._head = 0  # diar frames committed
        self._preds = np.zeros((0, self._mods.n_spk), np.float32)  # committed, from _preds_origin
        self._preds_origin = 0

    def start_at(self, sample: int) -> None:
        self._head = self._preds_origin = sample // FRAME_SAMPLES
        self._buf_origin = self._head * FRAME_SAMPLES
        self._buf = np.zeros(sample - self._buf_origin, np.float32)
        self._preds = self._preds[:0]
        self.samples = sample

    def feed(self, audio: np.ndarray, abandoned: Check | None = None) -> None:
        self._buf = np.concatenate([self._buf, audio.astype(np.float32)])
        self.samples += len(audio)
        while self._feat_frames_ready() >= (self._head + self._chunk + self._rc) * SUB:
            if abandoned is not None and abandoned():
                raise Abandoned
            preds, self._state = self._step(self._state, self._head, self._chunk, self._rc)
            self._preds = np.concatenate([self._preds, preds])
            self._head += self._chunk
        self._trim()

    def activity(self, start_frame: int, end_frame: int) -> np.ndarray:
        n_spk = self._mods.n_spk
        out = np.zeros((max(0, end_frame - start_frame), n_spk), np.float32)
        lo = max(start_frame, self._preds_origin)
        hi = min(end_frame, self._head)
        if hi > lo:
            out[lo - start_frame : hi - start_frame] = self._preds[
                lo - self._preds_origin : hi - self._preds_origin
            ]
        if end_frame > self._head:
            tail = self._provisional_tail()
            lo = max(start_frame, self._head)
            hi = min(end_frame, self._head + len(tail))
            if hi > lo:
                out[lo - start_frame : hi - start_frame] = tail[lo - self._head : hi - self._head]
        return out

    # -- internals --

    def _feat_frames_ready(self) -> int:
        """Feature frames whose full analysis window has arrived (center=True, n_fft 512)."""
        return max(0, (self.samples - 256) // HOP + 1)

    def _features(self, f0: int, f1: int) -> Any:
        """Feature frames [f0, f1) on the session timeline, bit-for-bit as a whole-file pass
        computes them: the preprocessor runs on audio with 2 frames of margin each side and
        the margin frames (affected by its edge padding) are dropped."""
        import torch

        margin = 2
        s0 = (f0 - margin) * HOP
        s1 = (f1 + margin) * HOP
        lead = max(0, self._buf_origin - s0)  # before the session start: silence
        seg = self._buf[max(0, s0 - self._buf_origin) : max(0, s1 - self._buf_origin)]
        seg = np.concatenate([np.zeros(lead, np.float32), seg])
        x = torch.from_numpy(seg)[None].to(self._m.device)
        n = torch.tensor([x.shape[1]], device=self._m.device)
        feats, _ = self._m.preprocessor(input_signal=x, length=n)
        return feats[:, :, margin : margin + (f1 - f0)]

    def _step(self, state: Any, head: int, chunk: int, rc: int) -> tuple[np.ndarray, Any]:
        import torch

        lc = min(self._lc, head)
        f0 = (head - lc) * SUB
        f1 = (head + chunk + rc) * SUB
        with self._lock, torch.inference_mode():
            feats = self._features(f0, f1)
            n = feats.shape[2]
            total = torch.zeros((1, 0, self._mods.n_spk), device=self._m.device)
            state, total = self._m.forward_streaming_step(
                processed_signal=feats.transpose(1, 2),
                processed_signal_length=torch.tensor([n], device=self._m.device),
                streaming_state=state,
                total_preds=total,
                left_offset=lc * SUB,
                right_offset=rc * SUB,
            )
        return total[0].float().cpu().numpy(), state

    def _provisional_tail(self) -> np.ndarray:
        """Score the frames after the committed head with whatever right context exists."""
        ready = self.samples // HOP
        tail_frames = -(-max(0, ready - self._head * SUB) // SUB)
        if tail_frames <= 0:
            return np.zeros((0, self._mods.n_spk), np.float32)
        state = copy.deepcopy(self._state)
        head, parts = self._head, []
        while tail_frames > 0:
            chunk = min(self._chunk, tail_frames)
            rc = min(self._rc, tail_frames - chunk)
            preds, state = self._step(state, head, chunk, rc)
            parts.append(preds[:chunk])
            head += chunk
            tail_frames -= chunk
        return np.concatenate(parts)

    def _trim(self) -> None:
        keep_from = (self._head - self._lc - 1) * FRAME_SAMPLES
        if keep_from > self._buf_origin:
            self._buf = self._buf[keep_from - self._buf_origin :]
            self._buf_origin = keep_from
        if len(self._preds) > KEEP_FRAMES:
            drop = len(self._preds) - KEEP_FRAMES
            self._preds = self._preds[drop:]
            self._preds_origin += drop


class SepformerSeparator:
    def __init__(self, model: Any, lock: threading.Lock) -> None:
        self._m = model
        self._lock = lock

    def separate(self, audio: np.ndarray) -> np.ndarray:
        import torch

        with self._lock, torch.inference_mode():
            x = torch.from_numpy(audio.astype(np.float32))[None].to(self._m.device)
            est = self._m.separate_batch(x)  # 1 x T x 2
        return est[0].T.float().cpu().numpy()


# ------------------------------------------------------------------------- sessions ---


@dataclass
class _Session:
    stream: Stream
    factory: Any = None  # builds a fresh Stream (a new sitting behind our timeline)
    used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # The api's per-sitting token: a resume sends a new one. A new sitting whose offset is
    # behind what this session has heard is a timeline the api restarted, not a late turn.
    epoch: str | None = None


class Sessions:
    def __init__(self, factory: Any, clock: Any = time.monotonic) -> None:
        self._factory = factory
        self._clock = clock
        self._items: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sid: str) -> tuple[_Session, bool]:
        now = self._clock()
        with self._lock:
            for k in [k for k, s in self._items.items() if now - s.used > SESSION_TTL_S]:
                del self._items[k]
            s = self._items.get(sid)
            fresh = s is None
            if fresh:
                while len(self._items) >= MAX_SESSIONS:
                    self._items.popitem(last=False)
                s = _Session(self._factory(), self._factory)
                self._items[sid] = s
            self._items.move_to_end(sid)
            s.used = now
            return s, fresh

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------- logic ---


def _envelope(x: np.ndarray, frames: int) -> np.ndarray:
    pad = np.zeros(frames * FRAME_SAMPLES, np.float32)
    pad[: min(len(x), len(pad))] = x[: len(pad)]
    return np.sqrt((pad.reshape(frames, FRAME_SAMPLES) ** 2).mean(1) + 1e-12)


def _pcm16(x: np.ndarray) -> str:
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak > 0:
        x = x * (0.5 / peak)  # separator output has no meaningful scale
    return base64.b64encode((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()).decode()


def diarize_turn(
    session: _Session,
    fresh: bool,
    offset_ms: int,
    pcm: bytes,
    separator: Separator | None,
    epoch: str | None = None,
    abandoned: Check | None = None,
) -> dict:
    """Feed one turn into the session and cut it into per-speaker pieces. ``abandoned``
    is polled before each model step and raises :class:`Abandoned` once the api has gone:
    a turn dropped before it is fed is a gap the next turn fills, as for an api timeout."""

    def check() -> None:
        if abandoned is not None and abandoned():
            raise Abandoned

    check()
    audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    start = offset_ms * SAMPLE_RATE // 1000
    if (
        not fresh
        and epoch is not None
        and session.epoch is not None
        and epoch != session.epoch
        and start < session.stream.samples
        and session.factory is not None
    ):
        # A resumed sitting whose timeline starts behind ours (the api resumed with no
        # retained audio): its audio is new speech, not a resend. Skipping it labelled one
        # person's turn from another's frames (QA). Numbering restarts for the conversation.
        session.stream = session.factory()
        fresh = True
    if epoch is not None:
        session.epoch = epoch
    stream = session.stream
    have = stream.samples
    if (fresh and start > 0) or start - have > MAX_GAP_S * SAMPLE_RATE:
        # A session we don't know (first turn after a restart), or a long jump: start the
        # clock here rather than run the model over made-up silence.
        stream.start_at(start)
        have = start
    if start > have:
        # The api skipped audio (a diarizer call that timed out): keep the clock honest.
        stream.feed(np.zeros(start - have, np.float32), abandoned)
        have = start
    new = audio[max(0, have - start) :]
    if len(new):
        stream.feed(new, abandoned)
    check()  # before the provisional tail is scored

    end = start + len(audio)
    f0, f1 = start // FRAME_SAMPLES, -(-end // FRAME_SAMPLES)
    probs = stream.activity(f0, f1)
    turn_s = len(audio) / SAMPLE_RATE
    pieces = planner.plan_pieces(probs, turn_s)

    out = []
    for p in pieces:
        a = int(p.start_s * SAMPLE_RATE)
        b = int(p.end_s * SAMPLE_RATE)
        item: dict[str, Any] = {
            "speaker": p.speaker,
            "startMs": a * 1000 // SAMPLE_RATE,
            "endMs": b * 1000 // SAMPLE_RATE,
            "overlap": p.separate,
            "audio": None,
        }
        if (
            p.separate
            and separator is not None
            and p.overlap_s >= MIN_SEPARATE_OVERLAP_S
            and FRAME_SAMPLES <= b - a <= MAX_SEPARATE_S * SAMPLE_RATE
        ):
            check()
            item["audio"] = _separated(audio[a:b], probs, a, b, p.speaker - 1, separator)
        out.append(item)
    return {"speakers": int(probs.shape[1]), "pieces": out}


def _separated(
    x: np.ndarray, probs: np.ndarray, a: int, b: int, spk: int, separator: Separator
) -> str | None:
    fa, fb = a // FRAME_SAMPLES, max(a // FRAME_SAMPLES + 1, b // FRAME_SAMPLES)
    window = probs[fa:fb]
    frames = len(window)
    # The two speakers talking most in this window are the two the separator splits.
    talk = (window > planner.ACTIVE_P).sum(0)
    pair = [int(k) for k in np.argsort(-talk)[:2] if talk[k] > 0]
    if spk not in pair or len(pair) < 2:
        return None
    streams = separator.separate(x)
    env = np.stack([_envelope(s, frames) for s in streams])
    choice = planner.assign_streams(env, window[:, pair].T)
    return _pcm16(streams[choice[pair.index(spk)]])


# ------------------------------------------------------------------------------ app ---


@dataclass
class Engines:
    sessions: Sessions | None = None
    separator: Separator | None = None
    error: str | None = None


def load_engines() -> Engines:
    import torch
    from nemo.collections.asr.models import SortformerEncLabelModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    claim = float(os.environ.get("DIAR_VRAM_CLAIM_GIB", "0"))
    if device == "cuda" and claim > 0:
        # Hold torch inside the pod's DRA claim on a shared card (as Parakeet did): past it
        # this process OOMs, never a co-tenant.
        total = torch.cuda.get_device_properties(0).total_memory / 2**30
        torch.cuda.set_per_process_memory_fraction(min(1.0, claim / total))
    name = os.environ.get("SORTFORMER_MODEL", "nvidia/diar_streaming_sortformer_4spk-v2")
    if name.endswith(".nemo"):
        model = SortformerEncLabelModel.restore_from(name, map_location=device)
    else:
        model = SortformerEncLabelModel.from_pretrained(name, map_location=device)
    model.eval()
    mods = model.sortformer_modules
    # Model card "high latency" streaming setup (10 s chunks). Latency is not what it costs
    # here: the api asks at each turn end and the turn's tail is scored provisionally, so
    # a long chunk only means more context. On scripts/diarize_eval sessions it got 93.6 %
    # of single-speaker frames right against 85.7 % for the 1.04 s setup, at 1/18 the
    # compute (diarizer/README.md).
    mods.chunk_len = int(os.environ.get("DIAR_CHUNK_LEN", "124"))
    mods.chunk_right_context = int(os.environ.get("DIAR_CHUNK_RC", "1"))
    mods.fifo_len = int(os.environ.get("DIAR_FIFO_LEN", "124"))
    mods.spkcache_update_period = int(os.environ.get("DIAR_SPKCACHE_UPDATE", "124"))
    mods.spkcache_len = int(os.environ.get("DIAR_SPKCACHE_LEN", "188"))
    mods._check_streaming_parameters()
    lock = threading.Lock()

    separator: Separator | None = None
    sep_name = os.environ.get("SEPARATOR_MODEL", "speechbrain/sepformer-whamr16k")
    if sep_name:
        from speechbrain.inference.separation import SepformerSeparation

        sep = SepformerSeparation.from_hparams(
            source=sep_name,
            savedir=os.path.join(os.environ.get("HF_HOME", "/tmp"), "speechbrain", sep_name),
            run_opts={"device": device},
        )
        separator = SepformerSeparator(sep, lock)
    return Engines(Sessions(lambda: SortformerStream(model, lock)), separator)


def create_app(engines: Engines | None = None, *, load: bool = True) -> FastAPI:
    app = FastAPI(title="tenir-diarizer")
    state = engines or Engines()
    if not API_KEY:
        log.error("DIAR_API_KEY is not set: every /v1/diarize call is refused (503)")

    if engines is None and load:

        def _load() -> None:
            try:
                loaded = load_engines()
                state.sessions, state.separator = loaded.sessions, loaded.separator
                log.info("diarizer ready")
            except Exception as exc:  # surfaced on /health
                log.exception("diarizer failed to load")
                state.error = repr(exc)

        threading.Thread(target=_load, daemon=True).start()

    @app.get("/health")
    def health() -> JSONResponse:
        if state.sessions is None:
            return JSONResponse(
                {"status": "error" if state.error else "loading", "error": state.error},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "sessions": len(state.sessions)})

    @app.post("/v1/diarize")
    async def diarize(
        request: Request,
        session: str = Query(min_length=1, max_length=128),
        offset_ms: int = Query(ge=0, le=MAX_OFFSET_MS),
        epoch: str | None = Query(None, max_length=64),
    ) -> JSONResponse:
        if not API_KEY:
            raise HTTPException(503, "DIAR_API_KEY is not set")
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {API_KEY}".encode()):
            raise HTTPException(401, "bad or missing bearer token")
        if state.sessions is None:
            raise HTTPException(503, "model loading")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            raise HTTPException(413, "turn too long")
        # Read with a cap, so a body without (or lying about) its length is refused before
        # it is buffered whole.
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > MAX_BODY:
                raise HTTPException(413, "turn too long")
        body = bytes(buf)
        import anyio

        sess, fresh = state.sessions.get(session)

        def gone() -> bool:
            # Polled from the worker thread between model steps; asks the event loop
            # whether the api has hung up.
            return anyio.from_thread.run(request.is_disconnected)

        def run() -> dict:
            # The session lock is where a late turn queues; check once it is ours, before
            # any model work.
            with sess.lock:
                return diarize_turn(
                    sess, fresh, offset_ms, body, state.separator, epoch, abandoned=gone
                )

        try:
            return JSONResponse(await anyio.to_thread.run_sync(run))
        except Abandoned:
            log.info("dropped a turn the api abandoned (session %s)", session)
            return Response(status_code=499)  # nobody reads it; nginx's "client closed"

    return app


app = create_app(load=os.environ.get("DIAR_NO_LOAD") != "1")
