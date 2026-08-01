"""Pure helpers shared by the music session loop and backends (XERK-184).

Kept in one place so the session's scan cadence, the recognizer, and the tests
agree on the same derived values without importing each other.
"""

from __future__ import annotations

import re

from api.stt.engine import BYTES_PER_SEC

# Collapse to a comparable identity: casefolded, punctuation dropped, whitespace
# squeezed. "Radiohead — Weird Fishes" and "radiohead  weird fishes" are one key.
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def track_key(artist: str, title: str) -> str:
    """Stable identity for a recognized track (dedupe across scans, lyric cache).

    Two recognitions of the same song — however the recognizer cases or spaces
    the metadata — must collapse to the same key, or every re-scan looks like a
    new song and the box flickers.
    """
    raw = f"{artist}␟{title}"
    cleaned = _NON_WORD.sub(" ", raw)
    return " ".join(cleaned.split()).casefold()


def window_bytes(seconds: float) -> int:
    """Bytes of 16 kHz mono PCM16 in ``seconds`` of audio — the rolling scan
    window's cap. A whole number of samples (BYTES_PER_SEC is already even)."""
    return max(0, int(seconds * BYTES_PER_SEC))


def scan_backoff_ms(misses: int, *, base_ms: int, cap_ms: int) -> int:
    """Exponential backoff for consecutive no-match/error scans.

    While a session has no song playing there is no point scanning at the base
    cadence forever — it just spends recognition calls (and, with shazamio,
    invites rate-limiting). Each consecutive miss doubles the wait up to a cap;
    a hit resets ``misses`` to 0 and the base interval resumes.
    """
    if misses <= 0:
        return base_ms
    # Cap the shift so 2**misses can't overflow into an absurd number first.
    shift = min(misses, 20)
    return min(cap_ms, base_ms * (2**shift))
