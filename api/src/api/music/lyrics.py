"""Time-synced lyrics from LRCLIB (XERK-184).

Once a track is recognized, its synced lyrics drive the auto-scroll. LRCLIB
(lrclib.net) is a free, open, key-less database of ~3M LRC (time-stamped)
lyrics — the natural fit: it returns per-line timestamps, and it is self-hostable
so lyric lookups can stay on-premises (point ``music_lyrics_endpoint`` at your
own instance). The network call is best-effort and excluded from coverage; the
LRC parser is pure and unit-tested. Results are cached per track for the
session, so a locked song is fetched once.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("api.music")

# [mm:ss.xx] or [mm:ss.xxx] or [mm:ss] — the fractional part is optional and may
# be centiseconds (2 digits) or milliseconds (3). Minutes may exceed 2 digits.
_LRC_TS = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class SyncedLine:
    """One parsed LRC line: text and its offset (ms) into the song."""

    at_ms: int
    text: str


def _frac_to_ms(frac: str | None) -> int:
    """Scale an LRC fractional field to milliseconds by its digit count: 2 digits
    are centiseconds (×10), 3 are already ms, 1 is tenths (×100)."""
    if not frac:
        return 0
    value = int(frac)
    if len(frac) == 1:
        return value * 100
    if len(frac) == 2:
        return value * 10
    return value  # 3 digits: already milliseconds


def parse_lrc(text: str) -> list[SyncedLine]:
    """Parse an LRC document into time-ordered lyric lines.

    Handles multiple timestamps on one line (``[00:10][00:40] chorus``), skips
    metadata tags (``[ar:...]``, ``[length:...]`` — anything whose bracket isn't a
    timestamp), keeps empty lyric lines (instrumental gaps) so the scroll timing
    stays honest, and returns the lines sorted by song time. A blank/garbage
    document yields ``[]`` — the caller then shows the title without a scroll.
    """
    lines: list[SyncedLine] = []
    for raw in text.splitlines():
        stamps = list(_LRC_TS.finditer(raw))
        if not stamps:
            continue
        # The lyric text is whatever follows the last timestamp on the line.
        body = raw[stamps[-1].end() :].strip()
        for m in stamps:
            minutes, seconds, frac = m.group(1), m.group(2), m.group(3)
            at_ms = (int(minutes) * 60 + int(seconds)) * 1000 + _frac_to_ms(frac)
            lines.append(SyncedLine(at_ms=at_ms, text=body))
    lines.sort(key=lambda ln: ln.at_ms)
    return lines


class LrcLibLyrics:
    """Fetches synced lyrics from an LRCLIB-compatible endpoint, cached per track."""

    def __init__(self, *, endpoint: str, timeout: float = 8.0) -> None:
        self._base = endpoint.rstrip("/")
        self._timeout = timeout
        self._cache: dict[str, list[SyncedLine]] = {}
        self._client = None  # lazily created httpx.AsyncClient (real backend only)

    async def synced_lines(  # pragma: no cover - requires httpx + the live LRCLIB API
        self,
        *,
        artist: str,
        title: str,
        duration_ms: int | None = None,
        album: str | None = None,
        cache_key: str | None = None,
    ) -> list[SyncedLine]:
        """Synced lyric lines for a track, or ``[]`` when none are found.

        Best-effort: any failure (miss, network error, plain-only lyrics) returns
        ``[]`` so the song box still shows the title, just without a scroll.
        """
        key = cache_key or f"{artist}␟{title}"
        if key in self._cache:
            return self._cache[key]
        result: list[SyncedLine] = []
        try:
            synced = await self._lookup(
                artist=artist, title=title, duration_ms=duration_ms, album=album
            )
            if synced:
                result = parse_lrc(synced)
        except Exception:
            log.warning("LRCLIB lookup failed for %s - %s", artist, title, exc_info=True)
        self._cache[key] = result
        return result

    async def _lookup(  # pragma: no cover - requires httpx + the live LRCLIB API
        self, *, artist: str, title: str, duration_ms: int | None, album: str | None
    ) -> str | None:
        """Return the raw syncedLyrics string from LRCLIB, or None.

        Tries the exact ``/api/get`` (needs a duration LRCLIB matches within a
        couple seconds) first, then falls back to ``/api/search`` and takes the
        best result that actually carries synced lyrics.
        """
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        headers = {"User-Agent": "Tenir (https://github.com/xerktech/tenir)"}
        if duration_ms is not None:
            params = {
                "artist_name": artist,
                "track_name": title,
                "duration": round(duration_ms / 1000),
            }
            if album:
                params["album_name"] = album
            resp = await self._client.get(
                f"{self._base}/api/get", params=params, headers=headers
            )
            if resp.status_code == 200:
                synced = resp.json().get("syncedLyrics")
                if synced:
                    return synced
        # Fallback: search by artist + title and take the first synced hit.
        resp = await self._client.get(
            f"{self._base}/api/search",
            params={"track_name": title, "artist_name": artist},
            headers=headers,
        )
        resp.raise_for_status()
        for hit in resp.json():
            if hit.get("syncedLyrics"):
                return hit["syncedLyrics"]
        return None

    async def close(self) -> None:  # pragma: no cover - requires httpx
        if self._client is not None:
            await self._client.aclose()
            self._client = None
