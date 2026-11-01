"""Windows CI smoke test: capture a real window with WGC through the client's platform layer.

Opens Notepad, captures it by title like the client does, and checks a non-black frame
arrives and is minimized-aware. Run on a Windows runner after ``pip install -e desktop``.
"""

from __future__ import annotations

import subprocess
import sys
import time

from tenir_capture.frames import resolve
from tenir_capture.win import WindowsPlatform


def main() -> int:
    proc = subprocess.Popen(["notepad.exe"])
    try:
        platform = WindowsPlatform()
        title = None
        for _ in range(50):
            title, _problem = resolve("Notepad", platform.list_titles())
            if title:
                break
            time.sleep(0.2)
        if not title:
            print("no Notepad window found", platform.list_titles())
            return 1
        cap = platform.capture(title)
        img = None
        for _ in range(50):
            img = cap.latest()
            if img is not None:
                break
            time.sleep(0.2)
        cap.stop()
        if img is None:
            print("WGC delivered no frame")
            return 1
        lo, hi = img.convert("L").getextrema()
        print(
            f"captured {title!r}: {img.size}, luma {lo}-{hi}, minimized={platform.is_minimized(title)}"
        )
        return 0 if hi > 8 else 1
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
