"""The capture loop: watch the configured windows and stream their frames to /ws/screen.

OS-agnostic: everything Windows-specific (window list, capture, notifications) comes in
through a ``Platform``, so the loop runs against fakes in the tests and against
``tenir_capture.win`` on the capture PC.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI
from websockets.sync.client import ClientConnection, connect

from tenir_capture.config import Config, WindowSpec
from tenir_capture.frames import (
    DEFAULT_CADENCE,
    FINDERS,
    Cadence,
    WindowPlan,
    encode,
    parse_server,
    resolve,
)

log = logging.getLogger("tenir_capture")

# The api refuses binary frames over 4 MB (API_SCREEN_MAX_FRAME_BYTES).
MAX_FRAME_BYTES = 4_000_000
# A connection that stayed up this long was healthy: the next reconnect starts from 1 s.
HEALTHY_SECONDS = 10.0
NOTICES = {
    "captions_off": (
        "A meeting is on screen in the {org} window but no captions are showing. "
        "Turn on captions so Tenir can record it."
    ),
}


class Capture(Protocol):
    def latest(self) -> Image.Image | None: ...
    def alive(self) -> bool: ...
    def stop(self) -> None: ...


class Platform(Protocol):
    def list_titles(self) -> list[str]: ...
    def is_minimized(self, title: str) -> bool: ...
    def capture(self, title: str) -> Capture: ...
    def notify(self, title: str, message: str) -> None: ...


class TokenRejected(RuntimeError):
    """The server closed with 1008: the device token is invalid, expired or revoked."""


@dataclass
class _Watched:
    spec: WindowSpec
    plan: WindowPlan
    title: str | None = None
    capture: Capture | None = None
    minimized: bool = False
    problem: str | None = None
    frame_error: bool = False  # the last frame failed: log the next failure without traceback
    cadence: Cadence | None = None  # per-window override from the server (a meeting is on)


@dataclass
class Runner:
    config: Config
    token: str
    platform: Platform
    connect: Callable[..., ClientConnection] = connect
    clock: Callable[[], float] = time.monotonic
    wall_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    tick: float = 0.2
    refresh_every: float = 2.0
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._default = DEFAULT_CADENCE
        self._watched: dict[str, _Watched] = {}
        for spec in self.config.windows:
            # config.parse only admits platforms with a finder.
            self._watched[spec.org_site_key] = _Watched(spec, WindowPlan(FINDERS[spec.platform]))
        self._next_refresh = 0.0

    # -- windows --------------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-resolve titles, (re)start captures, and warn about minimized windows."""
        titles = self.platform.list_titles()
        for w in self._watched.values():
            title, problem = resolve(w.spec.title, titles)
            if title != w.title or (w.capture is not None and not w.capture.alive()):
                self._stop_capture(w)
                w.title = title
                if title is not None:
                    try:
                        w.capture = self.platform.capture(title)
                        log.info("%s: capturing", w.spec.org_name)
                    except Exception as exc:  # noqa: BLE001 — retry on the next refresh
                        problem = f"capture failed: {exc}"
                        w.title = None
            if problem != w.problem:
                if problem:
                    log.warning("%s: paused (%s)", w.spec.org_name, problem)
                w.problem = problem
            minimized = w.title is not None and self.platform.is_minimized(w.title)
            if minimized and not w.minimized:
                self.platform.notify(
                    "Tenir capture",
                    f"The {w.spec.org_name} window is minimized, so Tenir can't read its "
                    "captions. Restore it; it may sit behind other windows.",
                )
            w.minimized = minimized

    def _stop_capture(self, w: _Watched) -> None:
        if w.capture is not None:
            try:
                w.capture.stop()
            except Exception:
                log.debug("capture stop failed", exc_info=True)
        w.capture = None
        w.plan = WindowPlan(w.plan.finder)  # a new window starts with a full frame

    def close(self) -> None:
        for w in self._watched.values():
            self._stop_capture(w)

    # -- server messages ------------------------------------------------------------------

    def on_message(self, raw: str | bytes) -> None:
        msg = parse_server(raw)
        if msg.kind == "cadence" and msg.cadence is not None:
            with self._lock:
                if msg.org_site_key is None:
                    self._default = msg.cadence
                elif (w := self._watched.get(msg.org_site_key)) is not None:
                    w.cadence = msg.cadence
                for w in self._watched.values():
                    w.plan.hurry(self.clock())
            log.info("cadence %s: %s", msg.org_site_key or "default", msg.cadence)
        elif msg.kind == "notice" and msg.org_site_key is not None:
            w = self._watched.get(msg.org_site_key)
            template = NOTICES.get(msg.text)
            if w is not None and template is not None:
                self.platform.notify("Tenir capture", template.format(org=w.spec.org_name))
        elif msg.kind == "error":
            log.warning("server: %s", msg.text)

    # -- sending --------------------------------------------------------------------------

    def step(self, ws: ClientConnection) -> int:
        """Send whatever is due now; returns the number of frames sent."""
        now = self.clock()
        if now >= self._next_refresh:
            self.refresh()
            self._next_refresh = now + self.refresh_every
        sent = 0
        for w in self._watched.values():
            if w.capture is None or w.title is None or w.minimized or not w.plan.due(now):
                continue
            img = w.capture.latest()
            if img is None:
                continue
            with self._lock:
                cadence = w.cadence or self._default
            try:
                frames = w.plan.frames(now, img, cadence)
            except Exception as exc:  # an odd frame (a 1 px window) can't stop capture
                if w.frame_error:
                    log.debug("%s: frame skipped: %r", w.spec.org_name, exc)
                else:
                    log.exception("%s: frame skipped (repeats logged at debug)", w.spec.org_name)
                w.frame_error = True
                continue
            w.frame_error = False
            for region, image in frames:
                data = encode(w.spec, w.title, region, self.wall_ms(), image)
                if len(data) > MAX_FRAME_BYTES:
                    log.warning("%s: %s frame too large, skipped", w.spec.org_name, region)
                    continue
                ws.send(data)
                sent += 1
        return sent

    def _receive(self, ws: ClientConnection, done: threading.Event) -> None:
        try:
            for raw in ws:
                self.on_message(raw)
        except ConnectionClosed:
            pass
        finally:
            done.set()

    def session(self, ws: ClientConnection) -> None:
        """Run one connection until it closes or stop is set."""
        with self._lock:
            # A new socket starts from the server's defaults: overrides were per connection.
            self._default = DEFAULT_CADENCE
            for w in self._watched.values():
                w.cadence = None
        done = threading.Event()
        receiver = threading.Thread(target=self._receive, args=(ws, done), daemon=True)
        receiver.start()
        try:
            while not self.stop.is_set() and not done.is_set():
                self.step(ws)
                self.stop.wait(self.tick)
        except ConnectionClosed:
            pass  # a send raced the server's close; its close code decides below
        finally:
            ws.close()
            receiver.join(timeout=5)
        code = ws.close_code
        if code == 1008:
            raise TokenRejected(ws.close_reason or "device token rejected")
        if code not in (None, 1000) and not self.stop.is_set():
            raise ConnectionError(f"closed {code}: {ws.close_reason}")

    def run(self) -> None:
        """Connect and stream until stopped, reconnecting with backoff. Raises TokenRejected."""
        backoff = 1.0
        while not self.stop.is_set():
            try:
                ws = self.connect(
                    self.config.ws_url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    open_timeout=15,
                    max_size=1 << 20,
                )
            except InvalidURI:
                raise
            except InvalidStatus as exc:
                log.warning("connect refused: HTTP %s", exc.response.status_code)
            except Exception as exc:  # noqa: BLE001 — a logon task must outlive any fault
                # OSError, timeouts, and handshakes cut short (InvalidMessage on a reset).
                log.warning("connect failed: %r", exc)
            else:
                log.info("connected to %s", self.config.ws_url)
                started = self.clock()
                try:
                    with ws:
                        self.session(ws)
                except TokenRejected:
                    raise
                except Exception as exc:  # noqa: BLE001 — reconnect, never die mid-shift
                    log.warning("disconnected: %r", exc)
                if self.clock() - started >= HEALTHY_SECONDS:
                    backoff = 1.0  # it was up for a while: retry promptly
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)
