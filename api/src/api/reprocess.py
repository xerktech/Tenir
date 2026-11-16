"""Re-caption a stored conversation with today's pipeline (XERK-2958).

    python -m api.reprocess <conversation id> [<conversation id> ...] [--household default]

Feeds the conversation's retained audio through the same streaming transcriber a live
session uses (STT, speaker labels from the diarizer when ``API_DIARIZER_URL`` is set) and
translates its non-English turns, then saves the result as a NEW conversation with its own
copy of the audio. The original is never modified: compare the two in history, and delete the
copy like any other recording (that removes only the copy's audio).

Run it where the api runs (it reads the same settings, database and audio store). The copy is
created now, so it lists at the top of history; its owner and microphone are the original's.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import uuid
import wave
from dataclasses import dataclass

from api.contract import CaptionFinal
from api.persistence import Segment, get_audio_store, get_conversation_store
from api.persistence.audio import audio_key
from api.stt import make_transcriber
from api.translate import make_translator

log = logging.getLogger("api.reprocess")

_FRAME_BYTES = 3200  # 100 ms of 16 kHz s16le, as the clients send it


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


async def _caption(pcm: bytes, source_lang: str | None, conversation_id: str) -> list[CaptionFinal]:
    from api.contract import Lang

    lang = Lang(source_lang) if source_lang else None
    # Partials are for the live band; a re-caption stores finals only, so skip their decodes.
    transcriber = make_transcriber(lang, conversation_id=conversation_id, partials=False)
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
    await transcriber.close()
    await pump
    return finals


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

    copy_id = str(uuid.uuid4())
    finals = asyncio.run(_caption(_pcm(wav_bytes), src.source_lang, copy_id))

    translator = make_translator()
    store.create(
        household, copy_id, owner=src.owner, mic_source=src.mic_source, source_lang=src.source_lang
    )
    translated = 0
    for f in finals:
        lang = f.lang.value if f.lang is not None else None
        text = None
        if translator is not None and lang not in (None, "en"):
            text = translator.translate(f.text, source_lang=lang)
            translated += text is not None
        store.add_segment(
            household,
            copy_id,
            Segment(
                segment_id=f.segmentId,
                text=f.text,
                start_ms=f.startMs,
                end_ms=f.endMs,
                lang=lang,
                translation=text,
                speaker=f.speaker,
            ),
        )
    key = audio_key(household, copy_id)
    audio.put(key, wav_bytes)
    store.set_audio_key(household, copy_id, key)
    store.finish(household, copy_id)
    return Result(
        conversation_id,
        copy_id,
        len(finals),
        len({f.speaker for f in finals if f.speaker}),
        translated,
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("conversations", nargs="+")
    ap.add_argument("--household", default="default")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for cid in a.conversations:
        r = reprocess(a.household, cid)
        print(
            f"{r.source_id} -> {r.copy_id}: {r.segments} turns, {r.speakers} speakers, "
            f"{r.translated} translated",
            flush=True,
        )


if __name__ == "__main__":
    main()
