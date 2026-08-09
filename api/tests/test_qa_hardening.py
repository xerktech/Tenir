"""Regressions from the first full QA pass (XERK-236).

Each test below pins a defect that was reproduced against a running stack:

* a dropped session that nobody resumes was never finalized and its audio was
  discarded, because ``close()`` cancelled the grace task that was calling it;
* rows left ``live`` by a hard kill stayed that way forever;
* bearer tokens rode the query string into the access log in cleartext;
* an empty or one-character ``API_AUTH_SECRET`` booted, making tokens forgeable;
* an unknown STT backend, and zero/negative durations, booted "healthy" and
  then failed in ways that looked like something else.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from api.auth import assert_secure_auth_config
from api.config import DEFAULT_AUTH_SECRET, Settings, settings
from api.logging_filters import RedactTokensFilter, redact_tokens
from api.persistence import audio_key, get_audio_store, get_conversation_store
from api.session import Session


@pytest.fixture(autouse=True)
def _reset() -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    yield


# --- the grace window must finalize, not lose, the recording ------------------


def test_unresumed_dropped_session_is_finalized_when_grace_lapses() -> None:
    """The glasses rely on this: an abnormal exit can't send session.end, so the
    lapsed grace window IS the finalization path (even/README, controller.ts).

    ``close()`` used to cancel ``self._grace_task`` unconditionally — but
    ``_grace_close()`` calls ``close()``, so that cancelled the running task.
    The CancelledError landed on the first await inside close(), ``_persist()``
    never ran, and the whole retained audio buffer went with the process.
    """

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, household="hh")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(20):
            await session.on_audio(b"\x11\x22" * 1600)
        await asyncio.sleep(0.2)

        session.detach(grace_seconds=0.1)  # socket dropped; nobody resumes
        await asyncio.sleep(1.5)  # well past the window

        conv = get_conversation_store().get("hh", session.session_id)
        assert conv is not None
        assert conv.status == "ready", "the lapsed grace window must finalize the conversation"
        assert conv.ended_at is not None
        assert get_audio_store().get(audio_key("hh", session.session_id)), "audio was discarded"
        assert not session._full_audio, "the retained buffer should have been flushed"

    asyncio.run(run())


def test_explicit_close_still_cancels_a_pending_grace_task() -> None:
    """The guard must only spare the task that is doing the closing."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, household="hh")
        await session.start(mic_source="phone-microphone", source_lang=None)
        session.detach(grace_seconds=30)
        grace = session._grace_task
        assert grace is not None and not grace.done()

        await session.close()  # e.g. shutdown, or session.end after a rebind
        assert grace.cancelled() or grace.done()
        assert session._grace_task is None

    asyncio.run(run())


# --- stale rows from a previous process --------------------------------------


def test_finish_stale_closes_rows_left_live_by_a_previous_run() -> None:
    store = get_conversation_store()
    store.create("hh", "crashed")
    store.create("hh", "also-crashed")
    done = store.create("hh", "clean")
    store.finish("hh", "clean")

    assert store.finish_stale() == 2
    assert store.get("hh", "crashed").status == "ready"
    assert store.get("hh", "crashed").ended_at is not None
    # An already-finished row is untouched, including its original end time.
    assert store.get("hh", "clean").ended_at == done.ended_at
    assert store.finish_stale() == 0  # idempotent


# --- tokens must not reach the logs ------------------------------------------


def test_redact_tokens_scrubs_query_string_credentials() -> None:
    line = 'GET /conversations/abc/audio?token=eyJhbGciOi.J9.sig HTTP/1.1'
    assert redact_tokens(line) == "GET /conversations/abc/audio?token=<redacted> HTTP/1.1"
    assert redact_tokens("/ws?token=abc.def") == "/ws?token=<redacted>"
    # Only the value goes; other params survive, and non-token text is untouched.
    assert redact_tokens("/ws?token=abc&mic=g2") == "/ws?token=<redacted>&mic=g2"
    assert redact_tokens("nothing to see") == "nothing to see"


def test_redaction_filter_scrubs_the_uvicorn_access_record() -> None:
    """uvicorn formats with %-args, so the token is in record.args, not msg."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5", "GET", "/ws?token=secret-token-value", "1.1", 200),
        exc_info=None,
    )
    assert RedactTokensFilter().filter(record) is True
    assert "secret-token-value" not in record.getMessage()
    assert "token=<redacted>" in record.getMessage()


# --- config that must not boot ------------------------------------------------


@pytest.mark.parametrize("secret", ["", "   ", "x", "short-secret", DEFAULT_AUTH_SECRET])
def test_boot_refuses_a_weak_signing_secret(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    """Only the literal default was refused before, so an empty API_AUTH_SECRET
    booted — and an empty HMAC key makes an admin token trivially forgeable."""
    monkeypatch.setattr(settings, "auth_secret", secret)
    with pytest.raises(RuntimeError, match="API_AUTH_SECRET"):
        assert_secure_auth_config()


def test_boot_accepts_a_real_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_secret", "0123456789abcdef0123456789abcdef")
    assert_secure_auth_config()  # does not raise


def test_unknown_stt_backend_is_refused_at_config_time() -> None:
    """It used to boot a container that reported healthy and served /status
    clean, then failed EVERY session with a generic "could not start session"."""
    with pytest.raises(ValueError, match="unknown STT backend"):
        Settings(stt_backend="banana")


@pytest.mark.parametrize(
    "field",
    [
        "auth_token_ttl_seconds",
        "session_resume_grace_seconds",
        "stt_partial_interval_ms",
        "stt_max_segment_ms",
        "cue_rss_keep_days",
        "cue_rss_interval_seconds",
        "status_probe_interval_seconds",
        "status_probe_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_durations_are_refused(field: str, value: int) -> None:
    """TTL 0 handed out a token that was already expired on the next request;
    probe interval 0 left /status a falsely-green page with nothing probed."""
    with pytest.raises(ValueError, match=field):
        Settings(**{field: value})
