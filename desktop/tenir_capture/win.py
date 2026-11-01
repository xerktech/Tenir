"""Windows side: window titles, Windows Graphics Capture, notifications, logon task.

Imported only on Windows (``windows-capture`` and ``plyer`` are Windows dependencies of the
packaged exe). Nothing here parses frames; see runner.py and frames.py.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("tenir_capture.win is Windows only")

log = logging.getLogger("tenir_capture")

TASK_NAME = "Tenir capture"
# Copy at most this often from WGC's frame stream (it delivers on every change, up to
# the display rate); the server asks for at most a few frames a second.
COPY_INTERVAL = 0.2

user32 = ctypes.windll.user32  # type: ignore[attr-defined]


def _windows() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _hwnd(title: str) -> int | None:
    hits = [h for h, t in _windows() if t == title]
    return hits[0] if len(hits) == 1 else None


class WgcCapture:
    """The newest frame of one window, kept as RGB; WGC reads it while occluded."""

    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture

        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        self._copied = 0.0
        self._closed = threading.Event()
        cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(frame, control) -> None:  # noqa: ANN001
            now = time.monotonic()
            if now - self._copied < COPY_INTERVAL:
                return
            self._copied = now
            # The buffer is WGC's mapped texture, only valid inside this callback: copy it.
            rgb = np.ascontiguousarray(frame.frame_buffer[:, :, 2::-1])
            with self._lock:
                self._latest = rgb

        @cap.event
        def on_closed() -> None:
            self._closed.set()

        self._control = cap.start_free_threaded()

    def latest(self) -> Image.Image | None:
        with self._lock:
            buf = self._latest
        return None if buf is None else Image.fromarray(buf, "RGB")

    def alive(self) -> bool:
        return not self._closed.is_set()

    def stop(self) -> None:
        self._control.stop()


class WindowsPlatform:
    def list_titles(self) -> list[str]:
        return [t for _, t in _windows()]

    def is_minimized(self, title: str) -> bool:
        hwnd = _hwnd(title)
        return bool(hwnd and user32.IsIconic(hwnd))

    def capture(self, title: str) -> WgcCapture:
        hwnd = _hwnd(title)
        if hwnd is None:
            raise RuntimeError("window gone or its title is not unique")
        return WgcCapture(hwnd)

    def notify(self, title: str, message: str) -> None:
        log.warning("notify: %s", message)
        try:
            from plyer import notification

            notification.notify(title=title, message=message[:256], app_name="Tenir", timeout=15)
        except Exception:  # noqa: BLE001 — a missed toast must not stop capture
            log.exception("desktop notification failed")


def install_logon_task(exe: str, config: str) -> int:
    """Start the client at logon via Task Scheduler (current user, normal privileges)."""
    cmd = [
        "schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON",
        "/RL", "LIMITED", "/TR", f'"{exe}" --config "{config}"',
    ]  # fmt: skip
    return subprocess.run(cmd, check=False).returncode


def uninstall_logon_task() -> int:
    return subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], check=False).returncode
