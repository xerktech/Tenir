"""Windows Graphics Capture probe for the meeting-captions spike (XERK-2504). Windows only.

Captures each meeting window by title with WGC (``pip install windows-capture``), saves a few
frames per window and reports whether they are real or black. Run it on the operator's central
PC once per window state the ticket asks about. Window titles name clients, so they are never
committed: pass them per run, or keep them in a local JSON file ({"slug": "exact title"}).

    python capture_windows.py --list                          # find the exact titles
    python capture_windows.py --windows windows.json --label occluded
    python capture_windows.py --window horizon1="<exact title>" --label minimized

Each run writes ``<out>/<label>/<window>_<n>.png`` and appends a row per window to
``<out>/capture-report.jsonl``: frames received, first-frame latency, size, mean luminance and the
share of near-black pixels. A window counts as captured when frames arrive and are not black.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from pathlib import Path

BLACK_LEVEL = 8  # 0-255 grey; WGC returns all-zero frames for windows it cannot read
BLACK_SHARE = 0.98  # frames with at least this share of near-black pixels count as black


def frame_stats(gray_values) -> dict:
    """Mean luminance and near-black share for a flat iterable of 0-255 grey values."""
    n = total = black = 0
    for v in gray_values:
        n += 1
        total += v
        black += v <= BLACK_LEVEL
    if not n:
        return {"mean_luma": 0.0, "black_share": 1.0, "black": True}
    share = black / n
    return {
        "mean_luma": round(total / n, 1),
        "black_share": round(share, 4),
        "black": share >= BLACK_SHARE,
    }


def capture_one(title: str, seconds: float, max_frames: int, out: Path, slug: str) -> dict:
    from PIL import Image
    from windows_capture import Frame, InternalCaptureControl, WindowsCapture

    row: dict = {"window": slug, "title": title, "frames": 0, "error": None}
    saved: list[Path] = []
    done = threading.Event()
    t0 = time.perf_counter()

    cap = WindowsCapture(cursor_capture=False, draw_border=False, window_name=title)

    @cap.event
    def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
        row["frames"] += 1
        if row["frames"] == 1:
            row["first_frame_ms"] = round((time.perf_counter() - t0) * 1000)
            row["size"] = [frame.width, frame.height]
        # WGC only delivers a frame when the window content changes; keep ~1 fps samples.
        if len(saved) < max_frames and (time.perf_counter() - t0) >= len(saved):
            path = out / f"{slug}_{len(saved)}.png"
            frame.save_as_image(str(path))
            saved.append(path)
        if time.perf_counter() - t0 >= seconds:
            control.stop()

    @cap.event
    def on_closed() -> None:
        done.set()

    try:
        control = cap.start_free_threaded()
        if not done.wait(seconds + 5):
            control.stop()
    except Exception as exc:  # noqa: BLE001 — report any WGC failure per window, keep going
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    if saved:
        gray = Image.open(saved[-1]).convert("L")
        row |= frame_stats(gray.getdata())
    row["saved"] = [str(p) for p in saved]
    return row


def list_titles() -> list[str]:
    """Visible top-level window titles, to find the exact Horizon/RDM names."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(cb, 0)
    return titles


def resolve(wanted: str, titles: list[str]) -> tuple[str | None, str | None]:
    """(title, error) for the window to capture; WGC needs the exact title.

    An exact title wins. Otherwise the substring must match exactly one window: the first of
    several would be a guess, and capturing a browser tab that mentions the meeting instead of
    the meeting would report a false pass.
    """
    if wanted in titles:
        return wanted, None
    needle = wanted.casefold()
    hits = [t for t in titles if needle in t.casefold()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "no visible window matches"
    return None, f"ambiguous: {len(hits)} windows match {hits!r}; pass the exact title"


def is_good(row: dict) -> bool:
    """A window passes when it delivered frames, none failed, and the last one is not black."""
    return not row.get("error") and row.get("frames", 0) > 0 and not row.get("black", True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--label", help="window state for this run, e.g. occluded")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--out", type=Path, default=Path("captures"))
    ap.add_argument(
        "--window",
        action="append",
        default=[],
        metavar="SLUG=TITLE",
        help="a window to capture (repeatable)",
    )
    ap.add_argument(
        "--windows",
        type=Path,
        metavar="JSON",
        help='local file of windows to capture: {"slug": "exact title"}',
    )
    ap.add_argument("--list", action="store_true", help="print visible window titles and exit")
    a = ap.parse_args(argv)

    titles = list_titles()
    if a.list:
        print("\n".join(titles))
        return 0
    windows = json.loads(a.windows.read_text()) if a.windows else {}
    for w in a.window:
        if "=" not in w:
            ap.error(f"--window expects SLUG=TITLE, got {w!r}")
        slug, title = w.split("=", 1)
        windows[slug] = title
    if not windows or not a.label:
        ap.error("pass --label and at least one window (--window or --windows)")
    out = a.out / re.sub(r"[^\w.-]", "_", a.label)
    out.mkdir(parents=True, exist_ok=True)
    ok = True
    with (a.out / "capture-report.jsonl").open("a") as report:
        for slug, substr in windows.items():
            title, error = resolve(substr, titles)
            if title is None:
                row = {"window": slug, "title": substr, "error": error}
            else:
                row = capture_one(title, a.seconds, a.frames, out, slug)
            row["label"] = a.label
            report.write(json.dumps(row) + "\n")
            good = is_good(row)
            ok &= good
            print(f"{slug:11} {'OK   ' if good else 'FAIL '} {json.dumps(row)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
