"""Per-connection session.

Holds the live session identity and the STT seam, fans audio in, and pumps
caption results back out. Every finalized turn is persisted to the conversation
store as it lands, the full audio is retained in memory for the session and
flushed to the audio store on end — a recorded, stored STT session.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher

from api.config import settings
from api.contract import (
    CaptionFinal,
    CaptionPartial,
    CaptionStatus,
    Cue,
    Lang,
    LyricLine,
    MicSource,
    ServerMessage,
    SessionReady,
    Song,
    SongDone,
    SongSync,
    Translation,
    TranslationDone,
)
from api.cue import CueGenerator, make_cue_generator, min_interval_ms, normalize_cue_title
from api.cue.base import (
    CUE_SUBSTANCE_MIN_TOKENS,
    GeneratedCue,
    cue_subject_tokens,
    cue_substance_similarity,
    cue_substance_tokens,
)
from api.cue.retrieval import EvidenceRetriever, make_evidence_retriever
from api.metrics import metrics
from api.music import (
    MusicMatch,
    MusicService,
    make_music_service,
    scan_backoff_ms,
    track_key,
    window_bytes,
)
from api.persistence import (
    Cue as CueRecord,
    Segment,
    Song as SongRecord,
    audio_key,
    get_audio_store,
    get_conversation_store,
    pcm16_to_wav,
    stale,
    wav_to_pcm16,
)
from api.persistence.postgres import is_database_unavailable
from api.stt import Transcriber, make_transcriber
from api.stt.engine import BYTES_PER_SEC
from api.stt.langid import is_english_word, is_shared_english_word
from api.translate import Translator, make_translator

log = logging.getLogger("api.session")

# Send a server message to the client. Returns when the frame is queued.
Sender = Callable[[ServerMessage], Awaitable[None]]

# Cap on messages buffered while detached (resume grace window): captions produced
# during the gap are replayed on rebind, but a never-resumed session must not grow
# without bound — keep the most recent ones.
_DETACHED_BUFFER_MAX = 500

# Bound on the end-of-session STT flush. The transcriber decodes off the WS path, so
# a hung STT upstream builds a backlog of turn decodes, each waiting out the engine
# timeout; an unbounded flush held teardown (persisting the conversation and its
# audio, server shutdown) for minutes, growing with the outage (XERK-1424). Turns
# still queued when it lapses are dropped from the live transcript only — their
# audio is retained and persisted with the rest.
_STT_FLUSH_TIMEOUT_S = 15.0

# A caption.final whose turn's last audio arrived more than this long ago is stored
# but not pushed live, translated or cued. Finals queue through an STT outage and
# land as one burst on recovery (XERK-1447); on the glasses that buried the present
# under seconds-old turns. A healthy final lands within ~1-2 s of its turn's end.
_STALE_FINAL_S = 10.0

# Captions are "delayed" (caption.status, XERK-1498) from the first final that lands
# more than _LATE_FINAL_S after its audio until finals have landed on time for
# _CAUGHT_UP_S and the transcriber is no longer behind real time. Without it, STT
# slower than real time just left the glasses blank or lagging with nothing saying
# why. A healthy final lands within ~1-2 s. The hold-off, and waiting out the
# transcriber, keep a slow engine whose merged backlog lands in bursts (some on time)
# from flapping the indicator.
_LATE_FINAL_S = 4.0
_CAUGHT_UP_S = 10.0

# How many (audio position, arrival time) samples the session keeps to date a final's
# audio. Pruned as finals land; the cap only bounds a long silent stretch with no
# finals. A final older than the oldest sample counts as stale: even at 20 ms chunks
# this spans minutes, far past _STALE_FINAL_S.
_AUDIO_ARRIVALS_MAX = 6000

# Every running session teardown. Paths like the grace-window lapse, session.end
# and revoke unregister the session before closing it, and a cancelled close()
# caller orphans its teardown, so shutdown waits on these rather than the registry
# (XERK-1458, XERK-1460). Also holds the reference that keeps each task from
# being GC'd.
_teardowns: set[asyncio.Task[None]] = set()


# The session still tearing down for each conversation, by (household, session
# id). The grace close unregisters a session before its teardown runs, so a
# resume landing mid-teardown cold-starts a new Session on the same conversation
# while that sitting's audio and tail finals are not yet stored; the new sitting
# takes its timeline from the closing one instead (XERK-1500).
_closing: dict[tuple[str | None, str], "Session"] = {}

# Seconds between attempts to finalize a session that ended during a database
# outage (XERK-1531).
_FINALIZE_RETRY_S = 10.0

# Sessions whose finalize waits on the database, oldest first, and the one task
# retrying them. Serial, so an outage ties up one executor thread rather than one per
# ended session. One still pending at exit leaves its row "live" for the next boot's
# stale sweep; its held writes are lost with the process.
_unfinalized: list["Session"] = []
_finalize_loop: asyncio.Task[None] | None = None


def _defer_finalize(session: "Session") -> None:
    global _finalize_loop
    _unfinalized.append(session)
    if (
        _finalize_loop is None
        or _finalize_loop.done()
        or _finalize_loop.get_loop() is not asyncio.get_running_loop()
    ):
        _finalize_loop = asyncio.create_task(_finalize_deferred())


async def _finalize_deferred() -> None:
    """Retry each deferred finalize until the database is back (XERK-1531)."""
    while _unfinalized:
        await asyncio.sleep(_FINALIZE_RETRY_S)
        while _unfinalized:
            session = _unfinalized[0]
            try:
                # An audio key the outage kept from the row (the WAV itself is stored).
                await session._retain_audio()
                if not await session._finalize():
                    break  # still down: wait out the interval
                log.info("session %s finalized after a database outage", session.session_id)
            except Exception:
                log.exception("session %s could not be finalized", session.session_id)
            _unfinalized.pop(0)


def teardowns_in_flight() -> list[asyncio.Task[None]]:
    """Session teardowns still running, for shutdown to wait on (XERK-1458)."""
    return [t for t in _teardowns if not t.done()]


# How many already-surfaced cue titles to hand the generator as "don't repeat"
# context (XERK-102). Bounds the prompt in a long conversation; the full set is
# still enforced by the post-hoc de-dupe, so nothing repeats beyond this window —
# it only limits how many the model is explicitly reminded of.
_CUE_AVOID_PROMPT_LIMIT = 40

# How often an expired translation hold re-checks a final decode still in flight.
_HOLD_RECHECK_S = 0.05

# A new cue whose content-word fingerprint overlaps a surfaced cue's at or above
# this Jaccard similarity is the same fact reworded, not a new cue. Calibrated
# on recorded production sessions: near-verbatim rewords measure 0.57-0.87 and
# retitled same-fact paraphrases 0.30-0.38, while the closest genuinely distinct
# pairs (different facts about the same entity) top out at 0.26-0.27 — the two
# classes overlap below 0.30, so 0.35 is as low as the hard drop can safely go.
# Paraphrases that share fewer content words than that are the prompt
# avoid-list's job, not this backstop's (see cue/base.py).
_CUE_SUBSTANCE_DUP_THRESHOLD = 0.35


def _enum_str(value: object | None) -> str | None:
    """StrEnum members stringify to their value; plain strings pass through."""
    return str(value) if value is not None else None


# A "translation" whose words match the source this closely MAY be the source reworded
# rather than translated (XERK-1423): an English turn inherited by a run comes back from
# a completion-prompt model with a word changed ("I made sure I can" -> "I made sure I
# could", ratio 0.80). Real translations of name-heavy turns score as high ("Marco,
# Sofia, Lucia, Pedro, sin Ana" -> "... without Ana", 0.83), so the ratio alone does not
# decide; see ``_same_text``.
_ECHO_MIN_WORD_RATIO = 0.75
# English-only words the model left untouched that make a replaced shared word (es "he")
# English too. One is not enough: code-switched turns carry an English word or two.
_REWORD_MIN_CONTEXT = 2

_WORD_RE = re.compile(r"\w+(?:'\w+)?")
_SENTENCE_END = re.compile(r"[.!?]\s*$")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.casefold().replace("\u2019", "'"))


def _name_positions(text: str) -> set[int]:
    """Indexes (into ``_words(text)``) of words that look like names: capitalized but
    not starting the turn or a sentence, and not the pronoun "I" ("I'm")."""
    text = text.replace("\u2019", "'")
    names = set()
    for i, match in enumerate(_WORD_RE.finditer(text)):
        word = match.group()
        if not word[0].isupper() or word.casefold().split("'")[0] == "i":
            continue
        if i > 0 and not _SENTENCE_END.search(text, 0, match.start()):
            names.add(i)
    return names


def _same_text(translated: str, source: str) -> bool:
    """Whether a translation is just its source echoed back (XERK-160) or reworded
    (XERK-1423, XERK-1520). Either way it tells the listener nothing true that the
    caption doesn't, and a rewording misquotes the speaker.

    Reworded: the casefolded word sequences, punctuation ignored, match at least
    ``_ECHO_MIN_WORD_RATIO`` and either
    - the model only deleted source words ("uh Marco left" -> "Marco left"), or
    - every source word the model replaced or deleted is English: a word only English
      has ("can" -> "could", "seen" -> "saw"), or the shared pronoun "he" (es "I have")
      when ``_REWORD_MIN_CONTEXT`` English-only words are kept ("..., Pedro, he don't
      care" -> "..., I don't care").
    A changed foreign word ("sin" -> "without", "Ana incluida" -> "including Ana",
    "gestern" -> "yesterday" beside an English "yesterday") or shared one ("..., Pedro,
    was?" -> "..., what?") is a real translation, however few words it touches, and is
    kept. A name (capitalized mid-sentence) the model dropped doesn't count: it drops
    "Pedro" while rewording "Pedro, he" -> "I".
    """
    src = _words(source)
    out = _words(translated)
    if not src or not out:
        return " ".join(translated.split()).casefold() == " ".join(source.split()).casefold()
    matcher = SequenceMatcher(None, src, out, autojunk=False)
    if matcher.ratio() < _ECHO_MIN_WORD_RATIO:
        return False
    opcodes = matcher.get_opcodes()
    if all(op in ("equal", "delete") for op, *_ in opcodes):
        return True
    names = _name_positions(source)
    changed: list[int] = []
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            continue
        # A name the model dropped (its output span is shorter) is no language; one it
        # replaced word for word may be a translated noun (de "Hund" -> "dog").
        dropped = (i2 - i1) - (j2 - j1)
        for i in range(i1, i2):
            if dropped > 0 and i in names:
                dropped -= 1
            else:
                changed.append(i)
    if all(is_english_word(src[i]) for i in changed):
        return True
    kept = [i for op, i1, i2, _, _ in opcodes if op == "equal" for i in range(i1, i2)]
    if sum(is_english_word(src[i]) for i in kept) < _REWORD_MIN_CONTEXT:
        return False
    return all(is_english_word(src[i]) or is_shared_english_word(src[i]) for i in changed)


def is_valid_session_id(value: str) -> bool:
    """Whether a client-supplied resume id is one the server could have issued.

    The api mints session ids as uuid4 and nothing else, so a resume id that is
    not a UUID did not come from us. This is a security boundary, not a
    nicety: the id flows into the conversation key AND into the audio object
    key (``{household}/{id}.wav``), so an id shaped like ``../other-household/
    <their-id>`` addresses another household's retained audio — enough to read
    its duration and, on session.end, to prepend and rewrite it. Accepting only
    UUIDs removes the whole class (XERK-236).
    """
    try:
        # Compared against the CANONICAL form, so only what uuid4() actually
        # stringifies to passes — an uppercase or dash-stripped variant parses
        # fine but is not an id we issued, and would miss the stored key anyway.
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


class Session:
    def __init__(
        self,
        send: Sender,
        *,
        session_id: str | None = None,
        household: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._send = send
        self.session_id = session_id or str(uuid.uuid4())
        self.resumed = bool(session_id)
        self.mic_source: MicSource | None = None
        self.source_lang: Lang | None = None
        self._transcriber: Transcriber | None = None
        self._pump: asyncio.Task[None] | None = None
        self._warmup: asyncio.Task[None] | None = None
        # Cues (XERK-81): a private context card the api derives from the running
        # transcript. Generation is off unless a backend is configured; when on, each
        # finalized turn feeds a rolling window to the cue model. Kept off the caption
        # path — a cue is a best-effort aside, produced in a background task so a slow
        # or failing model never stalls captions.
        self._cue_generator: CueGenerator | None = None
        # Evidence retrieval (XERK-120): live-source grounding for cue facts. None
        # when retrieval is off — cues then run ungrounded exactly as before.
        self._cue_retriever: EvidenceRetriever | None = None
        self._recent_finals: deque[str] = deque(maxlen=max(1, settings.cue_context_segments))
        # De-dupe cues for the WHOLE conversation, not a short rolling window
        # (XERK-102): a cue surfaced once must not pop up again later, however far
        # apart. ``_surfaced_cue_norms`` is the normalized-title membership set;
        # ``_surfaced_cues`` keeps the surfaced title+body pairs (order-preserving)
        # to hand the generator so it can steer clear of them and find fresh
        # context; ``_surfaced_cue_substance`` holds their content-word
        # fingerprints, the backstop against the same fact returning under a
        # fresh title and reworded body; ``_surfaced_cue_subjects`` is the
        # union of surfaced titles' distinctive subject words — one subject
        # gets one cue per conversation, so "Grafana origin" cannot follow
        # "Grafana" and a tenth SAML-titled cue cannot follow the first.
        self._surfaced_cue_norms: set[str] = set()
        self._surfaced_cues: list[GeneratedCue] = []
        self._surfaced_cue_substance: list[frozenset[str]] = []
        self._surfaced_cue_subjects: set[str] = set()
        self._last_cue_monotonic: float | None = None
        self._cue_inflight = False
        self._cue_tasks: set[asyncio.Task[None]] = set()
        # Live translations (XERK-160): when a finalized turn's detected language
        # isn't English, it is translated off the caption path and delivered as a
        # `translation` message paired to the segment. Consecutive non-English
        # turns form a RUN: while one is live, cues are suppressed; the run ends
        # when an English turn arrives or speech goes quiet past the hold window,
        # which emits `translation.done` (the glasses start their dismiss
        # countdown on it) and lets cues resume. Translation calls are serialized
        # through a single worker + queue so translations reach the client in
        # transcript order even when the model is slow; the `done` marker rides
        # the same queue, so it always follows the run's last translation.
        self._translator: Translator | None = None
        # Queue entries are (kind, final, run_lang): run_lang is the language of the
        # run the turn belongs to, captured when it is queued, so an inherited turn
        # (no lang of its own) can still be translated "from Spanish" by a backend
        # that needs a source language (XERK-1354).
        self._translation_queue: (
            asyncio.Queue[tuple[str, CaptionFinal | None, str | None]] | None
        ) = None
        self._translation_worker: asyncio.Task[None] | None = None
        self._translation_hold: asyncio.Task[None] | None = None
        self._translation_active = False
        # The language of the turn that last opened or extended the live run.
        self._translation_run_lang: str | None = None
        # Music ID (XERK-184): when a song is playing, the session periodically
        # fingerprints a short window of the live audio, identifies the track, and
        # shows its time-synced lyrics in the cue box, auto-scrolling as the song
        # plays. Unlike cues/translations this is AUDIO-driven — it taps on_audio
        # into a bounded rolling window and runs on its OWN scan loop, not off the
        # transcript. A confident match opens a `song` run (full lyrics + a sync
        # anchor); while the same song stays locked, periodic re-identification
        # sends `song.sync` to correct scroll drift; when it stops matching past
        # the hold window the run ends with `song.done`. A live song run suppresses
        # cues, exactly as a translation run does (the box is shared).
        self._music: MusicService | None = None
        self._music_scan: asyncio.Task[None] | None = None
        self._music_audio = bytearray()
        self._music_window_bytes = 0
        self._music_active = False
        self._music_run_id: str | None = None
        self._music_track_key: str | None = None
        self._music_last_match_monotonic: float | None = None
        self._music_misses = 0
        # A different track seen ONCE while a song is locked (XERK-187). Crossfades
        # and DJ blends make single scans flap between two tracks; a takeover only
        # happens when the newcomer matches twice in a row, so the box doesn't
        # reset on every blended window.
        self._music_pending_key: str | None = None
        # Song-end prep (XERK-192): the run carries the full lyrics and (usually) the
        # track duration, so the session knows when the song ends and schedules a
        # precise `song.done` rather than waiting out the no-match hold. These retain
        # the last scroll anchor (the offset we sent and when it was true) and the
        # song-time end position, so both the scheduled end and the scan-cadence
        # tightening near the end can derive the current play position.
        self._music_offset_ms: int | None = None
        self._music_offset_monotonic: float | None = None
        self._music_end_ms: int | None = None
        self._music_end_task: asyncio.Task[None] | None = None
        # The run opened but its lyric fetch failed transiently (LRCLIB 5xx/timeout),
        # so the `song` frame went out with no lines and the client shows the ♪
        # placeholder. Set here, it makes the next sync retry the fetch and re-emit
        # the song with lyrics once LRCLIB recovers, instead of the whole run
        # playing lyric-less (XERK-184). A genuine "no synced lyrics" miss leaves
        # this False — nothing to retry.
        self._music_lyrics_pending = False
        # Running total of audio pushed this sitting, so the music scan can stamp a
        # recognized song at the current session-timeline position (the same
        # timeline cues/segments use), independent of the STT seam.
        self._start_offset_ms = 0
        self._audio_bytes_pushed = 0
        # (session-timeline ms after a push, monotonic time of that push), oldest
        # first, so a final can be dated by when its audio arrived (_final_age_s).
        self._audio_arrivals: deque[tuple[int, float]] = deque(maxlen=_AUDIO_ARRIVALS_MAX)
        # Whether the client was last told captions are delayed, and the monotonic
        # time of the latest late final (see _LATE_FINAL_S).
        self._captions_delayed = False
        self._last_late_final = 0.0
        # Persistence: the household scopes the conversation store; with auth on it
        # comes from the authenticated principal, else the configured default. The
        # full-audio buffer is the retained record, flushed to the audio store on end.
        self._household = household or settings.household_id
        # Who opened the socket, so deleting an account can end its live captures
        # (XERK-236). Auth is checked at the handshake only, so without this a
        # removed member kept recording into the household for as long as they
        # held the socket open.
        self.user_id = user_id
        # How to drop this session's transport. Closing the Session alone is not
        # enough: the WS handler is parked in receive() and keeps feeding audio
        # into a session that is already finalized, so a revoked account went on
        # streaming until it chose to hang up. Set by the WS endpoint that owns
        # the socket; empty for a Session driven directly (tests, tooling). One per
        # socket ever bound: a resume can take the session over while the socket it
        # displaced is still open, and that socket must close too (XERK-1504).
        self._disconnects: list[Callable[[], Awaitable[None]]] = []
        # Set by revoke(): the account is gone, so audio the handler still reads
        # before the socket close lands is dropped, not recorded (XERK-1525).
        self._revoked = False
        self._conversations = get_conversation_store()
        self._audio_store = get_audio_store()
        self._full_audio = bytearray()
        # Audio stored whose conversation row doesn't point at it yet.
        self._audio_key_pending = False
        # Whether the last failed retain was a database outage, which holds the
        # finalize back; any other failure finalizes without audio (XERK-236).
        self._retain_outage = False
        # Transcript writes (segments, translations, cues, songs) not yet stored, in
        # order, with the task storing them. A database outage holds them here for the
        # next write or the finalize retry instead of losing them (XERK-1531).
        self._unsaved_writes: deque[Callable[[], object]] = deque()
        self._writes_lock = asyncio.Lock()
        self._writer: asyncio.Task[bool] | None = None
        # Resume support: on a socket drop (not an explicit session.end) the
        # connection is *detached* and the session is kept alive for a grace window
        # so a reconnect carrying the same id rebinds to it — preserving the
        # transcriber state instead of resetting it.
        self._closed = False
        # The shielded teardown close() runs (XERK-1460).
        self._teardown: asyncio.Task[None] | None = None
        # This teardown's first audio retain, and that of the earlier sitting of
        # this conversation still tearing down when this one resumed — which
        # must land before ours (XERK-1500).
        self._first_retain: asyncio.Future[bool] | None = None
        self._prior_retain: asyncio.Future[bool] | None = None
        self._prior_teardown: asyncio.Task[None] | None = None
        # The sitting that resumed this conversation while this one was still
        # tearing down: our late finish() must not close the row it reopened.
        self._successor: Session | None = None
        # A start() that raised is torn down by close() without ever going live
        # (XERK-1511); _row_opened says whether it got as far as the store row.
        self._start_failed = False
        self._row_opened = False
        # The conversations.create() thread: a cancel can't stop it, so _persist
        # waits for it before finish() (XERK-1529).
        self._create: asyncio.Future[object] | None = None
        self._detached = False
        self._grace_task: asyncio.Task[None] | None = None
        # Messages produced while detached are buffered here and replayed on rebind
        # so a brief drop doesn't silently lose captions.
        self._detached_buffer: list[ServerMessage] = []

    @property
    def household(self) -> str | None:
        return self._household

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def current_send(self) -> Sender:
        """The sender this session is currently bound to (identity-compared by the
        WS handler so a stale handler never detaches a freshly-resumed session)."""
        return self._send

    async def start(
        self,
        *,
        mic_source: MicSource,
        source_lang: Lang | None,
    ) -> None:
        # _start() spawns the worker/scan/pump/warmup tasks and creates the live
        # conversation row before its last await (the session.ready send, which
        # raises on a socket that just died). The caller only registers the session
        # once start() returns, so a raise past that point left those tasks running
        # and the row "live" with nobody to close them (XERK-1511). close() tolerates
        # any partial state, and a cancel lands here too.
        try:
            await self._start(mic_source=mic_source, source_lang=source_lang)
        except BaseException:
            self._start_failed = True
            # No audio of ours to keep in order behind a prior sitting's, so don't
            # hold this cleanup (and the WS handler) behind its teardown.
            self._prior_retain = None
            self._prior_teardown = None
            try:
                await self.close()
            except Exception:
                log.exception("session %s cleanup after failed start failed", self.session_id)
            raise

    async def _start(
        self,
        *,
        mic_source: MicSource,
        source_lang: Lang | None,
    ) -> None:
        self.mic_source = mic_source
        self.source_lang = source_lang
        # Build the transcriber now that we know the source language. A resumed
        # conversation seeds the segment timeline with the duration already
        # retained: this Session's transcriber starts a fresh byte count, but the
        # conversation's transcript and audio don't — _persist appends this
        # sitting's audio after the existing recording, so segments must continue
        # that timeline too or the merged transcript interleaves sittings and
        # History playback desyncs from the audio.
        start_offset_ms = await self._resume_offset_ms() if self.resumed else 0
        self._start_offset_ms = start_offset_ms
        self._transcriber = make_transcriber(
            source_lang=source_lang, start_offset_ms=start_offset_ms
        )
        # Build the cue generator (None when API_CUE_BACKEND=off — the default —
        # so the stripped core does no cue work at all).
        self._cue_generator = make_cue_generator()
        # And its evidence retriever (XERK-120); only meaningful with cues on.
        if self._cue_generator is not None:
            self._cue_retriever = make_evidence_retriever()
        # The translator (XERK-160); None when API_TRANSLATION_BACKEND=off, so the
        # stripped core does no translation work at all.
        self._translator = make_translator()
        if self._translator is not None:
            self._translation_queue = asyncio.Queue()
            self._translation_worker = asyncio.create_task(self._translation_worker_loop())
        # The music identifier (XERK-184); None when API_MUSIC_BACKEND=off, so the
        # stripped core does no music work — on_audio doesn't even buffer for it.
        self._music = make_music_service()
        if self._music is not None:
            self._music_window_bytes = window_bytes(settings.music_window_seconds)
            self._music_scan = asyncio.create_task(self._music_scan_loop())
        if stale.is_pending(self._conversations):
            # The boot sweep of a previous process's live rows hasn't succeeded
            # yet: run it before this session's row exists, so it can never
            # finalize a recording this process started (XERK-1428).
            await asyncio.to_thread(stale.sweep_if_pending, self._conversations)
        if self._conversations is not None:
            # Set before the call: a create that raises may still have written the
            # live row, which close() then finishes.
            self._row_opened = True
            # Idempotent: a resumed session keeps appending to its existing record.
            # Offloaded: a real (Postgres) store blocks, and this is on the connect
            # path — never run a blocking store call on the event loop. Kept as a
            # shielded future: a cancel here leaves the thread running, and its
            # INSERT committing 'live' after close() finished the row left it live
            # until the next boot's stale sweep (XERK-1529); _persist waits on it.
            self._create = asyncio.ensure_future(
                asyncio.to_thread(
                    self._conversations.create,
                    self._household,
                    self.session_id,
                    # Per-user ownership (XERK-651): stamp the recording with the principal
                    # that opened the socket. Idempotent create keeps a resumed row's
                    # original owner, so a resume never re-owns another user's recording.
                    owner=self.user_id,
                    mic_source=_enum_str(mic_source),
                    source_lang=_enum_str(source_lang),
                )
            )
            await asyncio.shield(self._create)
        self._pump = asyncio.create_task(self._pump_results())
        # Warm the transcriber's per-session startup cost now, off the caption path,
        # so the first spoken words don't wait behind it (XERK-128). Best-effort and
        # backgrounded: the ready message and the first audio never block on it, and a
        # resumed session already has a warm transcriber so there's nothing to redo —
        # but re-warming is a cheap no-op there anyway.
        self._warmup = asyncio.create_task(self._transcriber.warmup())
        self._warmup.add_done_callback(self._on_warmup_done)
        await self._send(
            SessionReady(type="session.ready", sessionId=self.session_id, resumed=self.resumed)
        )
        log.info("session %s ready (mic=%s)", self.session_id, mic_source)

    async def _resume_offset_ms(self) -> int:
        """Where a resumed conversation's timeline stands, in ms.

        The retained audio is authoritative when it exists — _persist prepends it
        before this sitting's audio, so its duration is exactly where the new
        audio (and therefore the new segments) begins. Without retained audio
        (audio backend off, or a memory backend emptied by a restart) fall back to
        the last persisted segment's end so the transcript at least stays
        monotonic. Store reads are offloaded: both backends block, and this runs
        on the connect path.

        An earlier sitting still tearing down has stored neither yet — reading
        the stores then restarted this sitting inside it (XERK-1500). Its
        in-memory timeline end is exact, so take that without waiting: blocking
        start() on a teardown (up to its flush + drain caps) let a second
        reconnect cold-start a duplicate sitting in the meantime.
        """
        key = (self._household, self.session_id)
        for deferred in _unfinalized:
            # A sitting whose finalize an outage deferred has left _closing, but its
            # retry still runs finish(): it must not close the row we reopen (XERK-1502).
            if (deferred._household, deferred.session_id) == key:
                deferred._successor = self
        prior = _closing.get(key)
        if prior is not None:
            self._prior_retain = prior._first_retain
            self._prior_teardown = prior._teardown
            prior._successor = self
            return prior._current_audio_ms()
        if self._audio_store is not None:
            existing = await asyncio.to_thread(
                self._audio_store.get, audio_key(self._household, self.session_id)
            )
            if existing:
                return len(wav_to_pcm16(existing)) * 1000 // BYTES_PER_SEC
        if self._conversations is not None:
            conv = await asyncio.to_thread(
                self._conversations.get, self._household, self.session_id
            )
            if conv is not None and conv.segments:
                return max(s.end_ms for s in conv.segments)
        return 0

    def _on_warmup_done(self, task: asyncio.Task[None]) -> None:
        # warmup() swallows its own errors, but retrieve any exception (incl. a
        # cancel on teardown) so it never surfaces as an unretrieved task warning.
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            log.warning("session %s STT warmup failed", self.session_id, exc_info=exc)

    async def on_audio(self, pcm: bytes) -> None:
        # Not is_closed: a shutdown close still keeps audio arriving during its
        # teardown (the final _persist() stores it). Only a revoke drops it — its
        # teardown runs before the socket closes, and the account is already gone.
        if self._revoked:
            return
        # Retain the full audio for the stored session: buffered in memory for the
        # session, flushed to the audio store on end.
        if self._audio_store is not None:
            self._full_audio.extend(pcm)
        # Track the session-timeline position so the music scan can stamp a song
        # at "now" (cheap counter; independent of any backend being on).
        self._audio_bytes_pushed += len(pcm)
        self._audio_arrivals.append((self._current_audio_ms(), time.monotonic()))
        # Music ID (XERK-184): keep a bounded rolling window of the most recent
        # audio for the scan loop to fingerprint. The one place music diverges
        # from cues/translations — it needs the audio, not the transcript.
        if self._music is not None:
            self._music_audio.extend(pcm)
            excess = len(self._music_audio) - self._music_window_bytes
            if excess > 0:
                del self._music_audio[:excess]
        if self._transcriber is not None:
            await self._transcriber.push(pcm)

    async def _buffer_send(self, msg: ServerMessage) -> None:
        """Sink used while detached: hold messages for replay on resume, capped so a
        never-resumed session can't grow without bound (keeps the most recent)."""
        self._detached_buffer.append(msg)
        if len(self._detached_buffer) > _DETACHED_BUFFER_MAX:
            del self._detached_buffer[: len(self._detached_buffer) - _DETACHED_BUFFER_MAX]

    async def rebind(self, send: Sender) -> None:
        """Reattach a resumed connection's sender, cancelling any pending grace close.

        The live transcriber and buffers are untouched, so captions pick up where
        the drop left off; messages produced during the gap are replayed to the new
        socket in order.
        """
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        self._send = send
        self._detached = False
        self.resumed = True
        if self._detached_buffer:
            buffered, self._detached_buffer = self._detached_buffer, []
            for msg in buffered:
                await send(msg)

    async def send_caption_status(self) -> None:
        """Repeat a delayed status to a resumed socket, after its session.ready.

        Clients clear the flag on every session.ready, since a cold resume or a new
        pod starts a session that is not delayed and would never say so."""
        if self._captions_delayed:
            await self._send(CaptionStatus(type="caption.status", delayed=True))

    def detach(self, *, grace_seconds: float) -> None:
        """Connection dropped without an explicit end: keep the session alive for a
        grace window so a resume can rebind it, instead of finalizing immediately.

        Until then sends are buffered (the socket is gone) and replayed on resume; if
        no resume arrives the grace task finalizes and unregisters the session.
        """
        if self._closed or self._detached:
            return
        self._detached = True
        self._send = self._buffer_send
        if grace_seconds <= 0:
            # Resume disabled — finalize on the next loop turn.
            self._grace_task = asyncio.create_task(self._grace_close(0))
        else:
            self._grace_task = asyncio.create_task(self._grace_close(grace_seconds))

    async def _grace_close(self, grace_seconds: float) -> None:
        try:
            if grace_seconds > 0:
                await asyncio.sleep(grace_seconds)
        except asyncio.CancelledError:
            return  # resumed — rebind() cancelled us
        if not self._detached or self._closed:
            return
        # Import here to avoid a circular import at module load (registry only needs
        # Session for typing).
        from api import registry

        registry.unregister(self)
        await self.close()

    def set_mic_source(self, mic_source: MicSource) -> None:
        self.mic_source = mic_source
        log.info("session %s mic -> %s", self.session_id, mic_source)

    async def _pump_results(self) -> None:
        assert self._transcriber is not None
        try:
            await self._drain_results()
        except Exception:
            # A failing STT seam must not take the whole connection down with an
            # unretrieved task exception. Log, count, and let the pump exit cleanly —
            # captions stop, the session lives.
            log.exception("session %s STT pump failed", self.session_id)
            metrics.incr("stage.stt.errors")

    async def _drain_results(self) -> None:
        assert self._transcriber is not None
        async for result in self._transcriber.results():
            if isinstance(result, CaptionPartial) and not result.text:
                continue  # close() sentinel
            # A final that sat out an STT outage is history, not a live caption: it
            # goes to the stored transcript only (XERK-1447).
            is_stale = False
            if isinstance(result, CaptionFinal):
                age = self._final_age_s(result)
                is_stale = age > _STALE_FINAL_S
                await self._track_caption_lag(age)
            if is_stale:
                metrics.incr("caption.final_stale")
            else:
                try:
                    await self._send(result)
                except Exception:
                    # The socket can be gone before the drain finishes: a client that
                    # sends session.end and closes immediately is torn down while the
                    # end-of-session flush is still producing finals. Delivery is
                    # best-effort, the transcript is not — swallow the send failure and
                    # keep draining so those turns are still persisted below (XERK-58).
                    log.warning(
                        "session %s could not deliver a caption (client gone)", self.session_id
                    )
                    metrics.incr("caption.send_errors")
                metrics.incr(
                    "caption.partial" if isinstance(result, CaptionPartial) else "caption.final"
                )
            if isinstance(result, CaptionFinal) and self._conversations is not None:
                # Persist the finalized turn to the conversation transcript.
                # Offloaded: a real (Postgres) store does a blocking round-trip;
                # running it on the loop would freeze every live session for its
                # duration. The caption was already sent (or dropped) above.
                self._store(
                    self._conversations.add_segment,
                    Segment(
                        segment_id=result.segmentId,
                        text=result.text,
                        start_ms=result.startMs,
                        end_ms=result.endMs,
                        lang=result.lang.value if result.lang is not None else None,
                    ),
                )
            if isinstance(result, CaptionPartial):
                # Speech is still flowing: keep a live translation run's silence
                # hold from expiring mid-utterance (finals only land at pauses).
                self._touch_translation_hold()
            if isinstance(result, CaptionFinal) and not is_stale:
                # A non-English turn opens/extends a translation run (XERK-160);
                # an English one closes it. Considered before the cue so the same
                # turn that opens a run never also produces a cue.
                self._consider_translation(result)
                # A finalized turn may be cue-worthy; consider it out of band.
                self._consider_cue(result)

    async def _track_caption_lag(self, age: float) -> None:
        """Tell the client when captions fall behind and when they catch up again."""
        now = time.monotonic()
        if age > _LATE_FINAL_S:
            self._last_late_final = now
            delayed = True
        elif (
            self._captions_delayed
            and now - self._last_late_final >= _CAUGHT_UP_S
            and not (self._transcriber is not None and self._transcriber.behind_real_time)
        ):
            delayed = False
        else:
            return
        if delayed == self._captions_delayed:
            return
        self._captions_delayed = delayed
        metrics.incr("caption.delayed" if delayed else "caption.caught_up")
        try:
            await self._send(CaptionStatus(type="caption.status", delayed=delayed))
        except Exception:
            # Best-effort like the captions themselves (XERK-58): a client already
            # gone must not stop the drain from persisting the remaining turns.
            log.warning("session %s could not deliver caption status", self.session_id)

    def _final_age_s(self, final: CaptionFinal) -> float:
        """Seconds since the audio that ends `final` reached the session.

        Dated by arrival rather than by the session timeline, so audio pushed faster
        than real time (tests, a simulator) doesn't read as old. Finals land in
        timeline order, so samples before this one's end are pruned as they go."""
        arrivals = self._audio_arrivals
        if not arrivals or final.endMs > arrivals[-1][0]:
            return 0.0  # no dated audio covers it (e.g. a backend's own timeline)
        # The first sample at or past its end is the push that delivered that audio.
        # If the cap already dropped it, the oldest kept sample is minutes old anyway.
        while len(arrivals) > 1 and arrivals[0][0] < final.endMs:
            arrivals.popleft()
        return time.monotonic() - arrivals[0][1]

    def _consider_translation(self, result: CaptionFinal) -> None:
        """Track the translation run across finalized turns (XERK-160).

        A non-English turn (re)opens the run and queues its translation; an
        English turn while a run is live means the other language is done being
        spoken, so the run closes. A turn with no detected language INHERITS an
        open run — the speaker is mid-run and only an explicit English turn
        closes one, so an undecidable turn between two Spanish turns (a
        proper-noun list like "Mercurio, Venus, Tierra, Marte.") is
        overwhelmingly a continuation and gets translated with the rest; it is
        queued without a claimed source language so the model reads the text
        for itself. Outside a run an undecidable turn decides nothing — there
        the same call would be a guess.
        """
        if self._translator is None:
            return
        lang = result.lang.value if result.lang is not None else None
        if lang is not None and lang != "en":
            assert self._translation_queue is not None
            self._translation_active = True
            self._translation_run_lang = lang
            self._touch_translation_hold()
            self._translation_queue.put_nowait(("translate", result, lang))
        elif lang == "en" and self._translation_active:
            self._end_translation_run()
        elif lang is None and self._translation_active:
            assert self._translation_queue is not None
            self._touch_translation_hold()
            self._translation_queue.put_nowait(("translate", result, self._translation_run_lang))

    def _touch_translation_hold(self) -> None:
        """Restart the run's silence hold: any speech activity (a partial or a
        final) means the speaker isn't done yet, so the countdown starts over."""
        if not self._translation_active:
            return
        if self._translation_hold is not None:
            self._translation_hold.cancel()
        self._translation_hold = asyncio.create_task(self._translation_hold_expire())

    async def _translation_hold_expire(self) -> None:
        try:
            await asyncio.sleep(max(0, settings.translation_hold_ms) / 1000)
            # A turn with speech still being decoded is not silence: its final is on
            # the way and may belong to this run (an inherited turn arriving after
            # the run closed is dropped). Finals take ~6 s on a loaded STT server
            # against a 3 s hold (XERK-1377), so wait it out; the final's own touch
            # then restarts the hold. Bounded by the STT request timeout plus the
            # failed-final retry budget (XERK-1499). A dead pump never consumes
            # that final, so it can't hold the run either.
            while (
                self._transcriber is not None
                and self._transcriber.finalizing
                and self._pump is not None
                and not self._pump.done()
            ):
                await asyncio.sleep(_HOLD_RECHECK_S)
        except asyncio.CancelledError:
            return
        self._end_translation_run()

    def _end_translation_run(self) -> None:
        """The other language is done being spoken: close the run and queue the
        `translation.done` marker behind any still-pending translations, so the
        client's dismiss countdown never starts before the run's last translation
        is on screen. Cues resume from here (the gate reads this flag)."""
        if not self._translation_active:
            return
        assert self._translation_queue is not None
        self._translation_active = False
        self._translation_run_lang = None
        if self._translation_hold is not None:
            self._translation_hold.cancel()
            self._translation_hold = None
        self._translation_queue.put_nowait(("done", None, None))

    async def _translation_worker_loop(self) -> None:
        """Serialized translation delivery: one queue, one worker, transcript
        order preserved no matter how slow individual model calls are."""
        assert self._translation_queue is not None
        while True:
            kind, final, run_lang = await self._translation_queue.get()
            try:
                if kind == "stop":
                    return
                if kind == "done":
                    try:
                        await self._send(TranslationDone(type="translation.done"))
                    except Exception:
                        log.warning(
                            "session %s could not deliver translation.done (client gone)",
                            self.session_id,
                        )
                        metrics.incr("translation.send_errors")
                    continue
                assert final is not None
                await self._translate_final(final, run_lang)
            finally:
                # Matched to the get() above so Queue.join() tracks the backlog
                # (tests await it to know the worker is idle).
                self._translation_queue.task_done()

    async def _translate_final(self, final: CaptionFinal, run_lang: str | None = None) -> None:
        """Translate one finalized turn and deliver + persist the result.
        Best-effort throughout, like cues: any failure is logged/counted and
        swallowed so the caption stream is never disturbed. ``run_lang`` is the
        live run's language, which an inherited turn (``final.lang`` None) has no
        other way to carry."""
        assert self._translator is not None
        lang = final.lang.value if final.lang is not None else None
        try:
            with metrics.timer("translation.ms"):
                translated = await asyncio.to_thread(
                    self._translator.translate, final.text, source_lang=lang, run_lang=run_lang
                )
        except Exception:
            log.warning("session %s translation failed", self.session_id, exc_info=True)
            metrics.incr("translation.errors")
            return
        if not translated:
            return
        if _same_text(translated, final.text):
            # The "translation" is the original, or the original with a word
            # changed — an English turn that reached the queue as an ambiguous
            # run-continuation. It adds nothing to the listener and a rewording
            # misquotes the speaker, so it is dropped rather than rendered.
            metrics.incr("translation.echo_drops")
            return
        try:
            await self._send(
                Translation(
                    type="translation",
                    segmentId=final.segmentId,
                    text=translated,
                    sourceLang=final.lang,
                )
            )
        except Exception:
            # Like captions, delivery is best-effort but the record is not:
            # persist below even if the socket is gone.
            log.warning(
                "session %s could not deliver a translation (client gone)", self.session_id
            )
            metrics.incr("translation.send_errors")
        metrics.incr("translation.emitted")
        if self._conversations is not None:
            # Queued behind its segment's own write, which may still be held.
            self._store(self._conversations.set_segment_translation, final.segmentId, translated)

    def _consider_cue(self, result: CaptionFinal) -> None:
        """On each finalized turn, maybe kick off cue generation in the background.

        Cheap gating happens here on the event loop (no model call): skip when cues
        are off, while one is already in flight, or inside the fixed rate-limit
        window. Only past those does it spawn a task that calls the model off-loop.
        """
        if self._cue_generator is None:
            return
        if result.text:
            self._recent_finals.append(result.text)
        if self._translation_active:
            # Live translation run (XERK-160): cues neither trigger nor appear
            # while the translation box owns their slot; they resume once the
            # run ends. The turn's text still joined the context window above.
            return
        if self._music_active:
            # A song is playing (XERK-184): its lyrics own the box, so cues stand
            # aside exactly as they do for a translation run; they resume once the
            # song ends. The turn's text still joined the context window above.
            return
        if self._cue_inflight:
            return
        if self._last_cue_monotonic is not None:
            elapsed_ms = (time.monotonic() - self._last_cue_monotonic) * 1000
            if elapsed_ms < min_interval_ms():
                return
        self._cue_inflight = True
        task = asyncio.create_task(self._generate_cue(result.endMs))
        self._cue_tasks.add(task)
        task.add_done_callback(self._cue_tasks.discard)

    async def _generate_cue(self, at_ms: int) -> None:
        """Run the cue model over the recent transcript and, on a hit, deliver +
        persist the cue. Best-effort throughout: any failure is logged/counted and
        swallowed so the caption stream is never disturbed."""
        try:
            transcript = "\n".join(t for t in self._recent_finals if t)
            if not transcript.strip():
                return
            assert self._cue_generator is not None
            # Steer the generator away from cues already surfaced this conversation
            # (XERK-102) so it finds fresh context instead of re-proposing an old one.
            # Only the most recent cues ride the prompt (older ones are still
            # caught by the full backstop sets below), so the ask stays compact.
            avoid = list(self._surfaced_cues[-_CUE_AVOID_PROMPT_LIMIT:])
            # Gather live-source evidence first (XERK-120). The retriever bounds
            # its own latency (deadline inside), and failure means an ungrounded
            # cue, never a missing one — evidence is an upgrade, not a gate.
            evidence = []
            if self._cue_retriever is not None:
                try:
                    with metrics.timer("cue.retrieval_ms"):
                        evidence = await self._cue_retriever.retrieve(list(self._recent_finals))
                except Exception:
                    log.warning(
                        "session %s cue evidence retrieval failed", self.session_id, exc_info=True
                    )
                    metrics.incr("cue.retrieval.errors")
            generated = await asyncio.to_thread(
                self._cue_generator.generate,
                transcript,
                avoid_cues=avoid,
                evidence=evidence,
            )
            if generated is None:
                return
            title = generated.title.strip()
            body = generated.body.strip()
            if not title or not body:
                return
            if self._translation_active or self._music_active:
                # A translation run (XERK-160) or a song (XERK-184) opened while
                # this cue was generating: it must not appear mid-run. Dropped
                # entirely — not surfaced, not recorded — so the fact stays
                # available for later.
                metrics.incr("cue.translation_drops")
                return
            # Backstop de-dupe: never surface the same cue twice in a conversation,
            # however far apart (XERK-102) — the report was an old cue popping up
            # again later once it had aged out of a short rolling window. Two
            # layers: the normalized title, and the content-word fingerprint that
            # catches the same fact returning under a fresh title and reworded
            # body (three "drone factory" cues in one recorded session). A repeat
            # is dropped WITHOUT resetting the rate-limit clock, so the next turn
            # can immediately try again for a genuinely new cue.
            norm = normalize_cue_title(title)
            if norm in self._surfaced_cue_norms:
                metrics.incr("cue.dedupe_drops")
                return
            substance = cue_substance_tokens(title, body)
            if len(substance) >= CUE_SUBSTANCE_MIN_TOKENS and any(
                len(prior) >= CUE_SUBSTANCE_MIN_TOKENS
                and cue_substance_similarity(substance, prior) >= _CUE_SUBSTANCE_DUP_THRESHOLD
                for prior in self._surfaced_cue_substance
            ):
                metrics.incr("cue.dedupe_drops")
                return
            # Third layer: same SUBJECT at a new angle ("Grafana origin" after
            # "Grafana"; ten SAML cues in one recorded session). The prompt
            # already bans this ("a definition, a mechanism, and a piece of
            # history about one thing are all the SAME cue"); this enforces the
            # ban when the model ignores it. Reworded angle-repeats measure
            # 0.04-0.33 substance Jaccard — inside the genuinely-distinct band,
            # unreachable by any threshold — but their titles share a
            # distinctive word. One subject, one cue.
            subject = cue_subject_tokens(title)
            if subject & self._surfaced_cue_subjects:
                metrics.incr("cue.subject_drops")
                return
            self._surfaced_cue_subjects |= subject
            self._surfaced_cue_norms.add(norm)
            self._surfaced_cues.append(GeneratedCue(title=title, body=body))
            self._surfaced_cue_substance.append(substance)
            self._last_cue_monotonic = time.monotonic()
            cue_id = uuid.uuid4().hex
            source = generated.source
            try:
                await self._send(
                    Cue(
                        type="cue",
                        cueId=cue_id,
                        title=title,
                        body=body,
                        atMs=at_ms,
                        source=source,
                    )
                )
            except Exception:
                # Like captions, delivery is best-effort but the record is not:
                # persist below even if the socket is gone.
                log.warning("session %s could not deliver a cue (client gone)", self.session_id)
                metrics.incr("cue.send_errors")
            metrics.incr("cue.emitted")
            if source:
                metrics.incr("cue.grounded")
            if self._conversations is not None:
                self._store(
                    self._conversations.add_cue,
                    CueRecord(cue_id=cue_id, title=title, body=body, at_ms=at_ms, source=source),
                )
        except Exception:
            log.warning("session %s cue generation failed", self.session_id, exc_info=True)
            metrics.incr("cue.errors")
        finally:
            self._cue_inflight = False

    # ---- Music ID (XERK-184) -------------------------------------------------

    def _current_audio_ms(self) -> int:
        """The session-timeline position of the most recent audio, in ms — where a
        recognized song is stamped (same timeline as cues/segments)."""
        return self._start_offset_ms + self._audio_bytes_pushed * 1000 // BYTES_PER_SEC

    def _music_window_wav(self) -> bytes | None:
        """The current rolling window as a WAV, or None until enough audio has
        accumulated. Requires ~5s so a landmark match has something to work with;
        below that a scan would just waste a recognition call."""
        min_bytes = min(self._music_window_bytes, window_bytes(5.0))
        if len(self._music_audio) < min_bytes or min_bytes == 0:
            return None
        return pcm16_to_wav(bytes(self._music_audio))

    async def _music_scan_loop(self) -> None:
        """Periodically fingerprint the audio window and drive the song run.

        Own background task, not the transcript pump: music is audio-driven. The
        cadence adapts — a locked song only needs its anchor nudged
        (`music_lock_interval_ms`), while searching backs off on repeated misses
        so an idle session isn't scanned at full rate. Any failure is swallowed;
        captions are never touched."""
        try:
            while not self._closed:
                await asyncio.sleep(self._next_scan_interval_ms() / 1000)
                if self._closed:
                    return
                await self._scan_music_once()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("session %s music scan loop failed", self.session_id, exc_info=True)
            metrics.incr("music.errors")

    def _next_scan_interval_ms(self) -> int:
        """How long to wait before the next fingerprint. The cadence adapts to the
        run state: slow while a song is locked (the client clock carries the scroll
        between syncs), but fast while searching, while a takeover awaits its
        confirming scan, and — new in XERK-192 — while the locked song is nearing
        its end, so the NEXT track is picked up promptly instead of after one more
        slow locked re-check. With no song, it backs off on repeated misses."""
        if self._music_active and self._music_pending_key is None:
            remaining_ms = self._song_remaining_ms(time.monotonic())
            if remaining_ms is not None and remaining_ms <= max(0, settings.music_end_prep_ms):
                return max(1, settings.music_scan_interval_ms)
            return max(1, settings.music_lock_interval_ms)
        if self._music_active:
            # A takeover candidate is waiting for its confirming scan (XERK-187):
            # re-check at the search cadence so a real song change isn't held up by
            # the slower lock interval.
            return max(1, settings.music_scan_interval_ms)
        return scan_backoff_ms(
            self._music_misses,
            base_ms=max(1, settings.music_scan_interval_ms),
            cap_ms=max(1, settings.music_scan_max_interval_ms),
        )

    async def _scan_music_once(self) -> None:
        """One scan step: fingerprint the window and open / re-sync / end a run."""
        if self._music is None:
            return
        # Music recognition runs independently of a translation run (XERK-194):
        # lyrics and translation are two separate streams that can be live at the
        # same time (web/mobile/phone render both at once), so a song must be free
        # to open, re-sync and end while someone is being translated. Only the
        # glasses have a single popup box for both, and there lyrics beat
        # translation beats cues — the arbitration lives on the lens, not here.
        # Cues still stand aside for either run (see `_consider_cue`).
        wav = self._music_window_wav()
        if wav is None:
            return
        # Anchor the scroll to the END of THIS window, captured BEFORE the slow,
        # variable-latency recognition call (XERK-188). `match.offset_ms` is the
        # song's play position at this instant; `window_end_ms` is the matching
        # session-timeline position and `window_end_monotonic` a reference for
        # compensating the identify + lyric-fetch latency at send time. Without
        # this the offset is anchored to "now" on the client but is really from
        # seconds ago (Shazam can take up to its hard timeout), and every re-sync
        # lands a different lag — the large, jumpy corrections this ticket is about.
        window_end_ms = self._current_audio_ms()
        window_end_monotonic = time.monotonic()
        try:
            match = await self._music.identify(wav)
        except Exception:
            log.warning("session %s music identify failed", self.session_id, exc_info=True)
            metrics.incr("music.errors")
            match = None
        now = time.monotonic()
        if match is None or match.confidence < settings.music_min_confidence:
            self._music_misses += 1
            if self._music_active and self._music_last_match_monotonic is not None:
                quiet_ms = (now - self._music_last_match_monotonic) * 1000
                if quiet_ms >= max(0, settings.music_hold_ms):
                    await self._end_music_run()
            return
        self._music_misses = 0
        key = match.track_key or track_key(match.artist, match.title)
        at_ms = window_end_ms
        if self._music_active and key == self._music_track_key:
            # The locked song again: refresh its clock and drop any takeover
            # candidate — the previous differing scan was a blip, not a change.
            self._music_last_match_monotonic = now
            self._music_pending_key = None
            await self._send_song_sync(match, at_ms, window_end_monotonic)
        elif self._music_active and key != self._music_pending_key:
            # A different track while a song is locked (XERK-187): note it and
            # wait for a confirming scan. Crossfaded windows flap between the
            # outgoing and incoming track; replacing on one sighting resets the
            # box (done → new → done) several times per blend. The locked song's
            # hold clock keeps running — if the newcomer never confirms and the
            # locked song never returns, the hold ends the run here just as a
            # plain miss would.
            self._music_pending_key = key
            if self._music_last_match_monotonic is not None:
                quiet_ms = (now - self._music_last_match_monotonic) * 1000
                if quiet_ms >= max(0, settings.music_hold_ms):
                    await self._end_music_run()
        else:
            # Not active (first sighting opens immediately — nothing to protect),
            # or the pending track matched twice in a row: (re)open the run.
            self._music_last_match_monotonic = now
            self._music_pending_key = None
            await self._open_music_run(match, key, at_ms, window_end_monotonic)

    def _synced_offset_ms(self, match: MusicMatch, window_end_monotonic: float) -> int:
        """The song's play position to anchor the client scroll on, compensated for
        recognition latency (XERK-188).

        `match.offset_ms` is the play position at the END of the fingerprinted
        window (`window_end_monotonic`). The recognition call — and, for a run
        open, the lyric fetch — take real, highly variable time (Shazam can spend
        up to its hard timeout), during which the song keeps playing. The client
        stamps its anchor on arrival, so without advancing the offset by that
        elapsed time the scroll is anchored seconds behind the music and each
        re-sync lands a different lag. Adding the elapsed wall time leaves only the
        small, roughly-constant WS delivery delay for the checker to trim."""
        elapsed_ms = int(max(0.0, (time.monotonic() - window_end_monotonic) * 1000))
        return max(0, match.offset_ms + elapsed_ms)

    def _song_end_ms(self, match: MusicMatch, lines: list[LyricLine]) -> int | None:
        """Song-time position at which the run should auto-end (XERK-192): the
        recognizer's reported duration, else the last synced lyric line plus the
        end tail, else None (unknown — the no-match hold ends the run instead)."""
        if match.duration_ms and match.duration_ms > 0:
            return match.duration_ms
        if lines:
            return lines[-1].atMs + max(0, settings.music_end_tail_ms)
        return None

    def _song_remaining_ms(self, now: float) -> int | None:
        """Wall-ms until the current track reaches its end position, derived from
        the retained scroll anchor; None when the end or the anchor is unknown.
        May be negative — the song is already past its end (dismiss now)."""
        if (
            self._music_end_ms is None
            or self._music_offset_ms is None
            or self._music_offset_monotonic is None
        ):
            return None
        position_ms = self._music_offset_ms + (now - self._music_offset_monotonic) * 1000
        return int(self._music_end_ms - position_ms)

    def _anchor_song(self, offset_ms: int) -> None:
        """Retain the scroll anchor just sent (the offset and when it was true) and
        (re)schedule the precise end-of-song dismissal off it (XERK-192)."""
        self._music_offset_ms = offset_ms
        self._music_offset_monotonic = time.monotonic()
        self._schedule_song_end()

    def _schedule_song_end(self) -> None:
        """(Re)schedule the `song.done` for when the current track ends (XERK-192).
        Cancels any prior schedule; a no-op when the end is unknown (no duration and
        no lyrics), leaving the no-match hold to end the run."""
        if self._music_end_task is not None:
            self._music_end_task.cancel()
            self._music_end_task = None
        remaining_ms = self._song_remaining_ms(time.monotonic())
        if remaining_ms is None:
            return
        self._music_end_task = asyncio.create_task(self._song_end_expire(remaining_ms))

    async def _song_end_expire(self, remaining_ms: int) -> None:
        """Wait out the rest of the track, then end the run so the box clears at the
        song's end instead of lingering to the hold (XERK-192)."""
        try:
            await asyncio.sleep(max(0, remaining_ms) / 1000)
        except asyncio.CancelledError:
            return
        # We ARE the end task; drop the handle before ending so _end_music_run
        # doesn't cancel this coroutine mid-send.
        self._music_end_task = None
        await self._end_music_run()

    async def _open_music_run(
        self, match: MusicMatch, key: str, at_ms: int, window_end_monotonic: float
    ) -> None:
        """A new song took over: close any prior run, fetch its lyrics, deliver the
        `song` frame, and persist the identity. Best-effort throughout."""
        if self._music_active:
            # A different song replaced the last one: close the old run cleanly so
            # the client's box (and the glasses countdown) resets before the new one.
            await self._end_music_run()
        try:
            synced = await self._music.lyrics(match)
            # A clean lookup (lines found, or a genuine miss) is final; nothing to
            # retry. Only a transient failure below leaves the run marked pending.
            self._music_lyrics_pending = False
        except Exception:
            log.warning("session %s lyric lookup failed", self.session_id, exc_info=True)
            synced = []
            # Transient (LRCLIB 5xx/timeout): retry on the next sync rather than
            # letting the placeholder stand for the whole run (XERK-184).
            self._music_lyrics_pending = True
        song_id = uuid.uuid4().hex
        self._music_run_id = song_id
        self._music_track_key = key
        self._music_active = True
        lines = [LyricLine(atMs=max(0, ln.at_ms), text=ln.text) for ln in synced]
        # Song-end prep (XERK-192): fix where the track ends, then anchor the scroll
        # (which schedules the precise `song.done` off that end).
        self._music_end_ms = self._song_end_ms(match, lines)
        self._anchor_song(self._synced_offset_ms(match, window_end_monotonic))
        try:
            await self._send(
                Song(
                    type="song",
                    songId=song_id,
                    title=match.title,
                    artist=match.artist,
                    atMs=at_ms,
                    offsetMs=self._music_offset_ms,
                    durationMs=match.duration_ms,
                    lines=lines,
                )
            )
        except Exception:
            # Like captions, delivery is best-effort but the record is not.
            log.warning("session %s could not deliver a song (client gone)", self.session_id)
            metrics.incr("music.send_errors")
        metrics.incr("music.emitted")
        if self._conversations is not None:
            self._store(
                self._conversations.add_song,
                SongRecord(
                    song_id=song_id,
                    title=match.title,
                    artist=match.artist,
                    at_ms=at_ms,
                    duration_ms=match.duration_ms,
                ),
            )

    async def _retry_song_lyrics(
        self, match: MusicMatch, at_ms: int, window_end_monotonic: float
    ) -> bool:
        """Re-fetch lyrics for a run that opened without them (XERK-184).

        Returns True when lyrics were recovered and a fresh `song` frame was
        emitted (so the caller sends no `song.sync` — the re-emitted frame carries
        the drift-corrected anchor itself). Returns False when the retry found no
        synced lyrics (a genuine miss — stop retrying) or failed again transiently
        (stay pending, fall through to the normal re-anchor so the clock keeps
        moving)."""
        run_id = self._music_run_id
        if run_id is None:
            return False
        try:
            synced = await self._music.lyrics(match)
        except Exception:
            # Still failing: keep pending so the next sync tries once more.
            log.warning("session %s lyric retry failed", self.session_id, exc_info=True)
            return False
        # The fetch awaited: the song-end task may have ended this run (or a
        # takeover replaced it) meanwhile. Don't resurrect a dead run — that would
        # schedule an orphan end task and emit a frame for a run the client has
        # already dismissed. Return True so the caller sends no `song.sync` either.
        if self._music_run_id != run_id:
            return True
        # A clean lookup is final either way: recovered lyrics or a genuine miss.
        self._music_lyrics_pending = False
        if not synced:
            return False
        lines = [LyricLine(atMs=max(0, ln.at_ms), text=ln.text) for ln in synced]
        # Refine the end position now that we have the synced lines, then re-anchor
        # off the fresh offset (this reschedules the precise `song.done`).
        self._music_end_ms = self._song_end_ms(match, lines)
        self._anchor_song(self._synced_offset_ms(match, window_end_monotonic))
        try:
            await self._send(
                Song(
                    type="song",
                    songId=run_id,
                    title=match.title,
                    artist=match.artist,
                    atMs=at_ms,
                    offsetMs=self._music_offset_ms,
                    durationMs=match.duration_ms,
                    lines=lines,
                )
            )
        except Exception:
            log.warning("session %s could not deliver song (client gone)", self.session_id)
            metrics.incr("music.send_errors")
        metrics.incr("music.emitted")
        return True

    async def _send_song_sync(
        self, match: MusicMatch, at_ms: int, window_end_monotonic: float
    ) -> None:
        """Re-anchor the locked song so the client corrects scroll drift.

        If the run opened without lyrics because the fetch failed transiently
        (``_music_lyrics_pending``), retry it here: LRCLIB may have recovered since
        the open, and re-emitting the `song` frame with lyrics swaps the client's
        ♪ placeholder for the real scroll without waiting for the track to end and
        re-open (XERK-184)."""
        if self._music_run_id is None:
            return
        if self._music_lyrics_pending:
            if await self._retry_song_lyrics(match, at_ms, window_end_monotonic):
                return
            # The retry awaited a fetch; the run may have ended meanwhile. Re-guard
            # so the normal re-anchor below never emits a `song.sync` for a dead run.
            if self._music_run_id is None:
                return
        # Re-anchor (XERK-192): refresh the retained offset and reschedule the
        # end-of-song dismissal off the fresh, drift-corrected position.
        self._anchor_song(self._synced_offset_ms(match, window_end_monotonic))
        try:
            await self._send(
                SongSync(
                    type="song.sync",
                    songId=self._music_run_id,
                    atMs=at_ms,
                    offsetMs=self._music_offset_ms,
                )
            )
        except Exception:
            log.warning("session %s could not deliver song.sync (client gone)", self.session_id)
            metrics.incr("music.send_errors")
        metrics.incr("music.sync")

    async def _end_music_run(self) -> None:
        """The song is over (stopped matching past the hold, or replaced): close
        the run and tell the client, so its box dismisses and cues resume."""
        if not self._music_active:
            return
        song_id = self._music_run_id
        self._music_active = False
        self._music_run_id = None
        self._music_track_key = None
        self._music_last_match_monotonic = None
        self._music_pending_key = None
        # Drop the song-end schedule and its anchor (XERK-192). Skips the current
        # task when the scheduled end is what's ending the run (it clears its own
        # handle first), so this never self-cancels the coroutine mid-send.
        if self._music_end_task is not None:
            self._music_end_task.cancel()
            self._music_end_task = None
        self._music_offset_ms = None
        self._music_offset_monotonic = None
        self._music_end_ms = None
        self._music_lyrics_pending = False
        if song_id is not None:
            try:
                await self._send(SongDone(type="song.done", songId=song_id))
            except Exception:
                log.warning(
                    "session %s could not deliver song.done (client gone)", self.session_id
                )
                metrics.incr("music.send_errors")
        metrics.incr("music.done")

    def on_disconnect(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Register how to drop a transport bound to this session (see ``revoke``).

        Each must be safe to call on a socket that has already gone away. The
        caller drops it with :meth:`drop_disconnect` when its socket ends, or a
        session resumed over and over keeps every dead socket alive.
        """
        if fn not in self._disconnects:  # a socket re-resuming its own session
            self._disconnects.append(fn)

    def drop_disconnect(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Forget a hook registered with :meth:`on_disconnect` (no-op if absent)."""
        if fn in self._disconnects:
            self._disconnects.remove(fn)

    async def revoke(self, reason: str) -> None:
        """Finalize the session AND close its socket — the account is gone.

        Order matters: close() first, so everything captured up to this moment
        is persisted, THEN drop the transport so nothing further can be sent
        (XERK-236). Audio still arriving from the socket meanwhile is dropped.
        """
        self._revoked = True
        await self.close()
        # A copy: a handler ending mid-loop drops its hook, which would skip the next.
        for disconnect in list(self._disconnects):
            try:
                await disconnect()
            except Exception:
                log.warning("session %s could not close its socket", self.session_id)
        log.info("session %s revoked: %s", self.session_id, reason)

    async def close(self) -> None:
        if self._closed:
            # Already closing: wait for that teardown rather than returning while
            # the conversation is still "live" (e.g. revoke() racing a close whose
            # caller was cancelled).
            if self._teardown is not None and self._teardown is not asyncio.current_task():
                await asyncio.shield(self._teardown)
            return
        self._closed = True
        # Cancel the pending grace task — UNLESS this close IS the grace task
        # finalizing. _grace_close() calls close(), so cancelling blindly
        # cancelled the currently-running task: the CancelledError landed at the
        # first await below (the pump join) and _persist() never ran, so a
        # dropped-and-never-resumed session stayed "live" forever and its whole
        # retained audio buffer was thrown away (XERK-236). That is exactly the
        # path even/ documents as the safety net for an abnormal exit.
        grace = self._grace_task
        self._grace_task = None
        if grace is not None and grace is not asyncio.current_task():
            grace.cancel()
        # A still-running warmup would race the flush/close below (both drive the same
        # stream session): cancel it so teardown owns the transcriber cleanly.
        if self._warmup is not None:
            self._warmup.cancel()
            self._warmup = None
        # Teardown runs as its own task, shielded: cancelling the caller (a
        # cancelled WS handler or grace task) must not abandon it halfway and skip
        # _persist(), leaving the conversation "live" (XERK-1460). The caller
        # still sees its CancelledError; the teardown finishes on its own, and
        # lifespan shutdown waits for it via teardowns_in_flight().
        # Started here, not in the teardown, so a resume landing before the
        # teardown's first step still finds it to order its own retain behind.
        self._first_retain = asyncio.ensure_future(self._retain_audio_after_prior())
        self._teardown = asyncio.create_task(self._close_teardown())
        _teardowns.add(self._teardown)
        self._teardown.add_done_callback(_teardowns.discard)
        key = (self._household, self.session_id)
        # A failed start never buffered audio, so a resume has nothing to order
        # behind it — and registering would evict the sitting it resumed from if
        # that one is still tearing down, restarting the next resume's timeline
        # inside that sitting's (XERK-1500, XERK-1511).
        if not self._start_failed:
            _closing[key] = self
        self._teardown.add_done_callback(
            lambda _t: _closing.pop(key) if _closing.get(key) is self else None
        )
        await asyncio.shield(self._teardown)

    async def _close_teardown(self) -> None:
        # Retain the audio BEFORE the model drains below. They can take ~30 s
        # together against a slow or hung model, and pod shutdown is SIGKILLed at
        # the 30 s grace period: the recording must already be on disk by then,
        # not queued behind STT/translation calls that can't affect it (XERK-1458).
        # Shielded: the store write runs in a thread that a cancel can't stop, so
        # a cancel landing here must not abandon the retain half-done — the finally
        # waits for it (and its buffer trim) before storing the remainder, or the
        # remainder would be stored behind a second copy of the same audio.
        retain = self._first_retain
        assert retain is not None
        # Finalize the conversation even if this teardown is cancelled mid-drain —
        # the shutdown deadline in main.py cancels teardowns that overrun it.
        try:
            await asyncio.shield(retain)
            # A failing STT seam can raise from flush()/close() too; guard each so
            # teardown still persists the conversation and never leaks an exception
            # out of close(). They are guarded separately: close() is what ends
            # results(), so a flush that raises (an STT timeout on the tail decode)
            # must not skip it, or the pump below is awaited forever.
            #
            # CancelledError is caught too unless this task itself is being cancelled
            # (event-loop shutdown): one escaping a seam otherwise is that seam's own
            # fault (e.g. a cancelled internal task) and must not skip persistence
            # (XERK-1460). Both calls are bounded so a wedged seam can't hold teardown.
            transcriber_closed = True
            if self._transcriber is not None:
                try:
                    await asyncio.wait_for(self._transcriber.flush(), timeout=_STT_FLUSH_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning("session %s STT flush timed out", self.session_id)
                    metrics.incr("stage.stt.flush_timeouts")
                except (Exception, asyncio.CancelledError):
                    self._reraise_if_cancelling()
                    log.exception("session %s transcriber flush failed", self.session_id)
                    metrics.incr("stage.stt.errors")
                # Close even when flush failed: it ends results(), and the pump join
                # below waits on that — a skipped close hung teardown forever.
                try:
                    await asyncio.wait_for(self._transcriber.close(), timeout=_STT_FLUSH_TIMEOUT_S)
                except (Exception, asyncio.CancelledError):
                    self._reraise_if_cancelling()
                    log.exception("session %s transcriber close failed", self.session_id)
                    metrics.incr("stage.stt.errors")
                    transcriber_closed = False
            if self._pump is not None:
                # A failed close() never queued the sentinel that ends results(), so
                # the pump would drain forever: cancel it instead (XERK-1460). Any
                # finals not yet drained are dropped from the transcript only; the
                # audio is retained and persisted below.
                if not transcriber_closed:
                    self._pump.cancel()
                try:
                    await self._pump
                except asyncio.CancelledError:
                    self._reraise_if_cancelling()
            if self._translation_worker is not None:
                # The pump is done, so every translate job is queued. A run still open
                # at teardown is over by definition (queues the `done` marker); then a
                # stop sentinel lets the worker drain pending translations first, so
                # the tail turns still persist translated. Bounded: a wedged model
                # call must not hold teardown hostage.
                self._end_translation_run()
                assert self._translation_queue is not None
                self._translation_queue.put_nowait(("stop", None, None))
                try:
                    await asyncio.wait_for(self._translation_worker, timeout=15)
                except asyncio.TimeoutError:
                    log.warning("session %s translation drain timed out", self.session_id)
                self._translation_worker = None
            if self._translation_hold is not None:
                self._translation_hold.cancel()
                self._translation_hold = None
            # Stop the music scan loop and release its service (XERK-184). A song run
            # left open just ends with the session — the client is tearing down too.
            if self._music_end_task is not None:
                self._music_end_task.cancel()
                self._music_end_task = None
            if self._music_scan is not None:
                self._music_scan.cancel()
                try:
                    await self._music_scan
                except asyncio.CancelledError:
                    # The scan's own cancel is expected; the shutdown deadline's
                    # cancel of this teardown must still reach the finally, not
                    # run on into the unbounded music/cue closes (XERK-1458).
                    self._reraise_if_cancelling()
                self._music_scan = None
            if self._music is not None:
                try:
                    await self._music.close()
                except Exception:
                    log.warning("session %s music service close failed", self.session_id)
            if self._cue_retriever is not None:
                try:
                    await self._cue_retriever.close()
                except Exception:
                    log.warning("session %s cue retriever close failed", self.session_id)
        finally:
            await asyncio.wait({retain})
            await self._persist()
        log.info("session %s closed", self.session_id)

    @staticmethod
    def _reraise_if_cancelling() -> None:
        """Re-raise the active CancelledError if this task is itself being cancelled."""
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise

    async def _persist(self) -> None:
        """Retain the full audio and finalize the conversation.

        The store/audio calls are offloaded to threads: under the real Postgres +
        disk backends they block, and this runs on the event loop during teardown —
        blocking it would freeze every other live session.
        """
        if self._conversations is None:
            return
        # A resumed sitting whose earlier sitting's first retain failed stores
        # only after that sitting's own retry, to keep the recording in order.
        cancelled = False
        prior, self._prior_teardown = self._prior_teardown, None
        if prior is not None:
            try:
                await asyncio.wait({prior})
            except asyncio.CancelledError:
                # The shutdown deadline: it cancels that teardown too and gives
                # both a bounded finalize window. Spend ours finishing the wait
                # and storing, or this sitting's audio would never be stored.
                cancelled = True
                await asyncio.wait({prior})
        # Picks up any audio that arrived after close() retained the buffer
        # (a no-op when it is empty), then finalizes.
        await self._retain_audio()
        if self._start_failed and not self._row_opened:
            # Never reached the store: there is no live row of ours to finalize, and
            # finish() on a resumed recording would rewrite its ended_at.
            return
        if self._create is not None:
            # Its outcome (incl. a raise) was start()'s to report; only order after it.
            # A shutdown-deadline cancel finishes the wait like the prior one above.
            try:
                await asyncio.wait({self._create})
            except asyncio.CancelledError:
                cancelled = True
                await asyncio.wait({self._create})
        if not await self._finalize():
            # The database is down: session.end must not raise out of the socket
            # handler, and the client won't resume an ended session, so nothing
            # else would ever finish this row (XERK-1531). Retry until it's back.
            log.warning("session %s ended during a database outage; will finalize", self.session_id)
            metrics.incr("conversation.finalize_deferred")
            _defer_finalize(self)
        if cancelled:
            raise asyncio.CancelledError

    def _store(self, write: Callable[..., object], *args: object) -> None:
        """Queue a transcript write for this conversation and start storing it.

        Off the pump: during an outage each attempt waits out the pool timeout,
        which inline held every caption behind it until it went stale (XERK-1531).
        """
        assert self._conversations is not None
        self._unsaved_writes.append(functools.partial(write, self._household, self.session_id, *args))
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._flush_writes())

    async def _flush_writes(self) -> bool:
        """Store the held writes in order; returns whether none are left.

        A database outage stops at the first failure and keeps the rest — one pool
        wait per attempt. Any other error drops that one write: it used to kill the
        result pump or translation worker, losing every later turn (XERK-1531).
        """
        async with self._writes_lock:
            while self._unsaved_writes:
                try:
                    await asyncio.to_thread(self._unsaved_writes[0])
                except Exception as exc:
                    if is_database_unavailable(exc):
                        log.warning(
                            "session %s database unavailable; %d write(s) held for retry",
                            self.session_id,
                            len(self._unsaved_writes),
                        )
                        metrics.incr("transcript.writes_deferred")
                        return False
                    log.exception("session %s could not store a transcript write", self.session_id)
                    metrics.incr("transcript.write_errors")
                self._unsaved_writes.popleft()
        return True

    async def _finalize(self) -> bool:
        """Store the held writes, then mark the conversation ready.

        Returns False only while a database outage stops it — including an audio key
        the last retain couldn't write, or the row would be ready but unplayable. Any
        other failure must not hold it: the serial retry would stall behind it.
        """
        assert self._conversations is not None
        if not await self._flush_writes() or (self._audio_key_pending and self._retain_outage):
            return False
        if self._resumed_live():
            # A later sitting reopened the row and is still recording into it: it
            # finishes the row itself, and doing it now would show it ready with
            # this sitting's ended_at for the rest of that one (XERK-1502).
            return True
        try:
            await asyncio.to_thread(
                self._conversations.finish, self._household, self.session_id, status="ready"
            )
        except Exception as exc:
            if not is_database_unavailable(exc):
                raise
            return False
        return True

    def _resumed_live(self) -> bool:
        """Whether a later sitting of this conversation has opened its row and not
        closed yet — following the chain, as that one may itself have been resumed
        mid-teardown."""
        later = self._successor
        while later is not None:
            if later._row_opened and not later._closed:
                return True
            later = later._successor
        return False

    async def _retain_audio_after_prior(self) -> bool:
        """Retain, but never ahead of the sitting this one resumed mid-teardown:
        _persist_audio appends to whatever is stored, so storing first would put
        this sitting's audio before the earlier one's (XERK-1500). Normally only
        the earlier sitting's first retain is waited on, not its model drains;
        if that retain failed, its audio is only retried by its final _persist,
        so ours waits for our own _persist (see there)."""
        prior_retain = self._prior_retain
        self._prior_retain = None
        if prior_retain is not None:
            await asyncio.wait({prior_retain})
            if prior_retain.cancelled() or not prior_retain.result():
                # Leave our audio buffered for _persist, which waits on that
                # teardown — waiting here would hold our own flush behind its
                # model drains, and a shutdown deadline would drop our tail.
                return False
            self._prior_teardown = None
        return await self._retain_audio()

    async def _retain_audio(self) -> bool:
        """Best-effort audio retention that never raises; returns whether it stored.

        Guarded as a whole: audio retention is best-effort, but FINALIZING the
        conversation is not. Anything raising in here — an unwritable audio dir,
        a full disk, or audio_key() rejecting an unusual household name — used
        to propagate out of close() and skip finish(), leaving the session stuck
        "live" forever on top of having lost its audio (XERK-236). Losing the
        recording is bad; losing the recording AND the record of it is worse.
        """
        if self._conversations is None:
            return True
        try:
            await self._persist_audio()
        except Exception as exc:
            self._retain_outage = is_database_unavailable(exc)
            log.exception("session %s could not retain audio", self.session_id)
            metrics.incr("audio.persist_errors")
            return False
        self._retain_outage = False
        return True

    async def _persist_audio(self) -> None:
        """Flush the retained full-session audio to the audio store."""
        if self._audio_store is not None and self._full_audio:
            key = audio_key(self._household, self.session_id)
            pcm = bytes(self._full_audio)
            taken = len(pcm)
            # Extend, don't overwrite. A session that resumes after the grace window
            # has lapsed reaches the api as a *new* Session on the same conversation
            # id, so its buffer holds only the post-resume audio — the glasses do
            # exactly this, persisting their session id across drops and relaunches.
            # Prepend whatever is already retained for this conversation so the stored
            # clip spans the whole session and stays replayable end to end, instead of
            # being clobbered with the latest fragment (XERK-86).
            existing = await asyncio.to_thread(self._audio_store.get, key)
            if existing:
                pcm = wav_to_pcm16(existing) + pcm
            wav = pcm16_to_wav(pcm)
            await asyncio.to_thread(self._audio_store.put, key, wav)
            # Trim as soon as the audio is stored, before anything else can fail:
            # close() retains twice, and an untrimmed buffer would be prepended
            # with its own stored copy on the second pass. Drop only what was
            # written — audio can still arrive during the awaits above, and the
            # final _persist() stores that remainder.
            del self._full_audio[:taken]
            self._audio_key_pending = True
        if self._audio_store is not None and self._audio_key_pending:
            # Retried on the next retain if it fails (the audio itself is stored).
            await asyncio.to_thread(
                self._conversations.set_audio_key,
                self._household,
                self.session_id,
                audio_key(self._household, self.session_id),
            )
            self._audio_key_pending = False
