"""Re-caption a stored conversation with today's pipeline (XERK-2958).

    python -m api.reprocess <conversation id> [<conversation id> ...] [--household default]

Feeds the conversation's retained audio through the same streaming transcriber a live
session uses (STT, speaker labels from the diarizer when ``API_DIARIZER_URL`` is set) and
translates its non-English turns, then saves the result as a NEW conversation with its own
copy of the audio. The original is never modified: compare the two in history, and delete the
copy like any other recording (that removes only the copy's audio).

All-or-nothing on failures: if STT returns nothing or an outage loses a turn, a diarizer call
fails or a translation is lost, no copy is written; the copy itself is written in one
transaction (a failure says whether a copy may exist). A turn STT answers blank is dropped as
non-speech, exactly as in a live session (Whisper's verdict on noise, not a failure). Each id
runs on its own; the exit status is non-zero when any failed.

Run it where the api runs (it reads the same settings, database and audio store). The copy is
created now, so it lists at the top of history; its owner and microphone are the original's.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys
import uuid
import wave
from dataclasses import dataclass

from api.contract import CaptionFinal, Lang
from api.persistence import Segment, get_audio_store, get_conversation_store
from api.persistence.audio import audio_key
from api.session import _same_text
from api.stt import make_transcriber
from api.translate import make_translator

log = logging.getLogger("api.reprocess")

_FRAME_BYTES = 3200  # 100 ms of 16 kHz s16le, as the clients send it


class ReprocessError(RuntimeError):
    """A dependency failed mid-run: no copy was kept."""


@dataclass
class Result:
    source_id: str
    copy_id: str
    segments: int
    speakers: int
    translated: int


def _pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError("expected 16 kHz s16le mono audio")
        return w.readframes(w.getnframes())


async def _caption(
    pcm: bytes, source_lang: str | None, conversation_id: str
) -> tuple[list[CaptionFinal], int, int]:
    """(finals, STT final failures, diarizer failures) for the whole recording."""
    lang = Lang(source_lang) if source_lang else None
    transcriber = make_transcriber(lang, conversation_id=conversation_id, recaption=True)
    finals: list[CaptionFinal] = []

    async def drain() -> None:
        async for m in transcriber.results():
            if isinstance(m, CaptionFinal):
                finals.append(m)

    pump = asyncio.create_task(drain())
    for i in range(0, len(pcm), _FRAME_BYTES):
        await transcriber.push(pcm[i : i + _FRAME_BYTES])
        if i % (_FRAME_BYTES * 50) == 0:
            await asyncio.sleep(0)  # let the decode worker and the drain run
    await transcriber.flush()
    diarizer = getattr(transcriber, "_diarizer", None)
    stt_failures = getattr(transcriber, "final_failures", 0)
    await transcriber.close()
    await pump
    return finals, stt_failures, (diarizer.failures if diarizer is not None else 0)


def _translate(finals: list[CaptionFinal], translator) -> list[str | None]:
    """Translations, following the live session's rules as far as they apply after the fact:
    a non-English turn is translated; a turn with no detected language inherits an open
    non-English run (only an English turn closes one); a translation that just echoes or
    rewords its source is dropped (``session._same_text``). A turn the translator could not
    translate (None, after one retry) fails the run."""
    out: list[str | None] = []
    run: str | None = None
    for f in finals:
        lang = f.lang.value if f.lang is not None else None
        if lang == "en":
            run = None
        elif lang is not None:
            run = lang
        if translator is None or run is None:
            out.append(None)
            continue
        text = translator.translate(f.text, source_lang=lang, run_lang=run)
        if text is None:
            text = translator.translate(f.text, source_lang=lang, run_lang=run)
        if text is None:
            raise ReprocessError(f"translation failed for turn at {f.startMs} ms")
        out.append(None if _same_text(text, f.text) else text)
    return out


def reprocess(household: str, conversation_id: str) -> Result:
    store, audio = get_conversation_store(), get_audio_store()
    if store is None or audio is None:
        raise RuntimeError("persistence and the audio store must be configured")
    src = store.get(household, conversation_id)
    if src is None:
        raise LookupError(f"no conversation {conversation_id} in household {household}")
    if src.source != "mic":
        raise ValueError("only mic conversations can be re-captioned")
    wav_bytes = audio.get(src.audio_key) if src.audio_key else None
    if not wav_bytes:
        raise LookupError(f"conversation {conversation_id} has no retained audio")

    # Everything that can fail on a dependency runs before anything is written.
    copy_id = str(uuid.uuid4())
    finals, stt_failures, diarizer_failures = asyncio.run(
        _caption(_pcm(wav_bytes), src.source_lang, copy_id)
    )
    if not finals:
        raise ReprocessError("STT returned no turns (is the STT route up?)")
    if stt_failures:
        raise ReprocessError(f"{stt_failures} STT decode(s) failed; those turns would be missing")
    if diarizer_failures:
        raise ReprocessError(
            f"{diarizer_failures} diarizer call(s) failed; those turns would be unlabelled"
        )
    translations = _translate(finals, make_translator())

    segments = [
        Segment(
            segment_id=f.segmentId,
            text=f.text,
            start_ms=f.startMs,
            end_ms=f.endMs,
            lang=f.lang.value if f.lang is not None else None,
            translation=text,
            speaker=f.speaker,
        )
        for f, text in zip(finals, translations, strict=True)
    ]
    # The audio first: without a row it is an unreachable file. Then the row, every segment
    # and the audio key in one transaction, so a database lost part-way leaves no short copy
    # for the stale sweep to finish (QA). If that fails, take the file back out.
    key = audio_key(household, copy_id)
    audio.put(key, wav_bytes)
    try:
        store.import_conversation(
            household,
            copy_id,
            owner=src.owner,
            mic_source=src.mic_source,
            source_lang=src.source_lang,
            segments=segments,
            audio_key=key,
        )
    except BaseException as exc:
        # The commit may have landed with only its acknowledgement lost: then the copy is in
        # history and must keep its audio (QA). Delete the file only when the row is
        # confirmed absent; if that can't be checked, keep it (an orphan file is harmless,
        # a listed copy without audio is not).
        try:
            stored = store.get(household, copy_id) is not None
        except Exception:
            log.exception("could not check whether copy %s was stored", copy_id)
            raise ReprocessError(
                f"writing failed ({exc}); copy {copy_id} may exist - check history, and if it "
                f"is not there delete the audio file {key}"
            ) from exc
        if not stored:
            log.error("writing the copy of %s failed; nothing was stored", conversation_id)
            try:
                audio.delete(key)
            except Exception:
                log.exception("could not remove the copy's audio %s", key)
            raise
        log.warning("write of copy %s reported %r but it is stored; keeping it", copy_id, exc)
        if not isinstance(exc, Exception):
            raise  # interrupted after the commit: the copy stays, the interrupt propagates
    return Result(
        conversation_id,
        copy_id,
        len(finals),
        len({f.speaker for f in finals if f.speaker}),
        sum(t is not None for t in translations),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("conversations", nargs="+")
    ap.add_argument("--household", default="default")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    failed = 0
    for cid in a.conversations:
        try:
            r = reprocess(a.household, cid)
        except Exception as exc:
            failed += 1
            print(f"{cid}: FAILED: {exc}", flush=True)
            continue
        print(
            f"{r.source_id} -> {r.copy_id}: {r.segments} turns, {r.speakers} speakers, "
            f"{r.translated} translated",
            flush=True,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
