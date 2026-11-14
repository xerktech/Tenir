"""Ship the client's own log lines to the api (``screen.log``, XERK-2875).

A failing meeting is otherwise diagnosable only from the log on the meeting PC. This handler
keeps the INFO+ lines of ``tenir_capture`` (capturing, paused, cadence, caption pane,
warnings) in a bounded buffer; the runner sends them in batches over its ``/ws/screen``
socket. Logging never waits on the network: a full buffer drops its oldest line and the
next batch says how many went.

The client reads no text off the screen, so its lines hold org names, never OCR or caption
text. Window titles (configured and resolved, any case) are masked anyway, in case an
exception message carries one. A title found inside an org name is left alone: org names ship
anyway, and masking one hid which org a line was about ("<window>: paused").
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque

# Lines held between sends: a disconnected client keeps the newest of them for the reconnect.
BUFFER = 200
# Contract (ScreenLog): at most 50 lines a message, 2000 characters a line.
BATCH = 50
MAX_LINE = 2000
# A longer line keeps this much of its head and the rest from its tail: a deep traceback's
# last line (the exception) is the part a diagnosis needs (QA).
HEAD = 500
CUT = " … "
# Resolved window titles kept for masking (newest): a retitling window must not grow it forever.
MAX_RESOLVED = 32
# The server's error replies are logged here and not shipped: an api that refuses screen.log
# would otherwise answer every batch with an error that becomes the next batch.
SERVER_LOGGER = "tenir_capture.server"
MASK = "<window>"


class LogShipper(logging.Handler):
    def __init__(
        self, redact: list[str] | tuple[str, ...] = (), orgs: list[str] | tuple[str, ...] = ()
    ) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))
        self._orgs = tuple(o.casefold() for o in orgs if o)
        self._configured = tuple(t for t in redact if t and not self._in_org(t))
        self._resolved: deque[str] = deque(maxlen=MAX_RESOLVED)
        self._pattern: re.Pattern[str] | None = None
        self._compile()
        self._lines: deque[dict] = deque()
        self._dropped = 0
        self._mu = threading.Lock()

    def mask(self, title: str) -> None:
        """Mask this window title in every later line (the runner adds each resolved title)."""
        if not title or title in self._configured or title in self._resolved or self._in_org(title):
            return
        self._resolved.append(title)
        self._compile()

    def _in_org(self, title: str) -> bool:
        t = title.casefold()
        return any(t in o for o in self._orgs)

    def _compile(self) -> None:
        # Longest first, so a title containing another is masked whole; any case, as resolve().
        alts = sorted({*self._configured, *self._resolved}, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, alts)), re.IGNORECASE) if alts else None

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == SERVER_LOGGER or record.name.startswith(SERVER_LOGGER + "."):
            return
        try:
            text = self.format(record)
            if (pattern := self._pattern) is not None:
                text = pattern.sub(MASK, text)
            # A lone surrogate made the api refuse the whole batch (QA).
            text = text.encode("utf-8", "replace").decode("utf-8")
            line = {
                "ts": int(record.created * 1000),
                "level": record.levelname
                if record.levelname in ("INFO", "WARNING", "ERROR", "CRITICAL")
                else "ERROR",
                "message": text
                if len(text) <= MAX_LINE
                else text[:HEAD] + CUT + text[-(MAX_LINE - HEAD - len(CUT)) :],
            }
        except Exception:  # noqa: BLE001 — a log line must never break the caller
            self.handleError(record)
            return
        with self._mu:
            if len(self._lines) >= BUFFER:
                self._lines.popleft()
                self._dropped += 1
            self._lines.append(line)

    def batch(self) -> str | None:
        """The next ``screen.log`` message (oldest lines first), or None when there is none."""
        with self._mu:
            if not self._lines:
                return None
            lines = [self._lines.popleft() for _ in range(min(BATCH, len(self._lines)))]
            dropped, self._dropped = self._dropped, 0
        msg: dict = {"type": "screen.log", "lines": lines}
        if dropped:
            msg["dropped"] = dropped
        return json.dumps(msg, separators=(",", ":"))

    def requeue(self, payload: str) -> None:
        """Put back a batch whose send failed, ahead of newer lines, as far as the buffer allows."""
        msg = json.loads(payload)
        with self._mu:
            self._dropped += msg.get("dropped", 0)
            for line in reversed(msg["lines"]):
                if len(self._lines) >= BUFFER:
                    self._dropped += 1
                    continue
                self._lines.appendleft(line)
