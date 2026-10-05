"""The once-per-process sweep of conversations a previous process left ``live``.

Only a graceful shutdown finalizes live sessions, so boot sweeps the rest
(XERK-236). If the database is unreachable at boot that sweep fails, and it must
be retried rather than dropped — or the orphans stay "recording" in every
client's history until the next restart with the database up (XERK-1428).

A retry must never finalize a session *this* process started. So a failed boot
sweep leaves the store "pending", and every new session runs the sweep itself
before creating its row: no row is created by this process until the sweep has
succeeded, so the sweep can only ever see a previous process's rows.

A swept session may have stored its WAV but died before recording the key (it
ended during a database outage, XERK-1553). The key is deterministic, so the
sweep links any WAV it finds — otherwise History can never play that audio.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from api.persistence.audio import AudioStore, audio_key
from api.persistence.conversations import ConversationStore

log = logging.getLogger("api.persistence")

# Seconds between background retries while a failed boot sweep is pending.
RETRY_INTERVAL_SECONDS = 10.0

_lock = threading.Lock()
_pending: ConversationStore | None = None


def arm(store: ConversationStore) -> None:
    """Mark ``store`` as owing a sweep; cleared by the first successful one."""
    global _pending
    with _lock:
        _pending = store


def is_pending(store: ConversationStore | None) -> bool:
    return store is not None and _pending is store


def _link_stored_audio(
    store: ConversationStore, audio: AudioStore, swept: list[tuple[str, str]]
) -> None:
    """Point each swept row at its WAV when one is stored. Best effort per row:
    the sweep itself already succeeded, and a row left unlinked is no worse off
    than before."""
    for household, conversation_id in swept:
        try:
            key = audio_key(household, conversation_id)
            if audio.exists(key):
                store.set_audio_key(household, conversation_id, key)
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            log.warning(
                "could not link stored audio for swept conversation %s",
                conversation_id,
                exc_info=True,
            )


def sweep_if_pending(store: ConversationStore, audio: AudioStore | None = None) -> None:
    """Run the sweep once, if still owed. Raises (leaving it owed) on failure.

    Blocking — call it off the event loop. The lock makes concurrent callers (the
    retry loop, several session starts) sweep exactly once between them.
    """
    global _pending
    with _lock:
        if _pending is not store:
            return
        swept = store.finish_stale()
        _pending = None
    if swept:
        log.warning("finalized %d conversation(s) left live by a previous run", len(swept))
        if audio is not None:
            _link_stored_audio(store, audio, swept)


async def retry_loop(store: ConversationStore, audio: AudioStore | None = None) -> None:
    """Keep retrying a failed boot sweep until it succeeds, so orphans are healed
    as soon as the database is back even if nobody starts a session."""
    while is_pending(store):
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(sweep_if_pending, store, audio)
        except Exception as exc:  # noqa: BLE001 - still unreachable; try again later
            log.warning("stale conversation sweep still failing; will retry: %s", exc)
