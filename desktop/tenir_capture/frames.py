"""What to send for a window and when: the /ws/screen frame format and per-window cadence.

Pure logic, no Windows APIs, so it is tested on any OS. The wire contract is
``ScreenFrameHeader`` / ``ScreenCadence`` / ``ScreenNotice`` in
``contract/ws-messages.schema.json``; docs/meeting-captions.md has the design.
"""

from __future__ import annotations

import io
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from tenir_capture.caption_box import crop_box
from tenir_capture.config import WindowSpec

# Caption-box finders per platform. A platform without one is not captured yet.
FINDERS: dict[str, Callable[[Image.Image], Image.Image | None]] = {"zoom": crop_box}

# The api decodes full frames up to this many pixels (api/src/api/screen/ocr.py
# MAX_FRAME_PIXELS) and refuses frames over 4 MB; a bigger window is scaled down to fit.
MAX_FULL_PIXELS = 3840 * 2400
FULL_QUALITY = 80
CAPTION_QUALITY = 90  # caption crops are small; keep glyph edges crisp for OCR


@dataclass(frozen=True)
class Cadence:
    fps: float
    full_frame_seconds: float


# Used until the server's first screen.cadence arrives (it sends one on connect).
DEFAULT_CADENCE = Cadence(fps=0.2, full_frame_seconds=30.0)


def resolve(wanted: str, titles: list[str]) -> tuple[str | None, str | None]:
    """(title, error) for a configured window among the visible titles.

    An exact title wins. Otherwise the substring must match exactly one window: picking
    the first of several could stream the wrong client's meeting. A title that changes
    (RDM with another tab in front) matches nothing, which pauses that window.
    """
    if wanted in titles:
        return wanted, None
    needle = wanted.casefold()
    hits = sorted({t for t in titles if needle in t.casefold()})
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "no visible window matches"
    return None, f"ambiguous: {len(hits)} windows match"


def jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def fit_full(img: Image.Image) -> Image.Image:
    """Scale a full frame down only if it exceeds what the server decodes."""
    pixels = img.width * img.height
    if pixels <= MAX_FULL_PIXELS:
        return img
    scale = (MAX_FULL_PIXELS / pixels) ** 0.5
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)


def encode(spec: WindowSpec, title: str, region: str, ts_ms: int, image: bytes) -> bytes:
    """One binary frame: 4-byte big-endian header length, header JSON, then the JPEG."""
    header = json.dumps(
        {
            "type": "screen.frame",
            "windowTitle": title[:512],
            "orgSiteKey": spec.org_site_key,
            "orgName": spec.org_name,
            "platform": spec.platform,
            "ts": ts_ms,
            "region": region,
        },
        separators=(",", ":"),
    ).encode()
    return struct.pack(">I", len(header)) + header + image


class WindowPlan:
    """Per-window send schedule.

    Every 1/fps seconds the newest frame is cropped to the caption box. A caption crop is
    sent only while a caption is on screen; a full frame goes every fullFrameSeconds and
    right before the first caption crop after none (the server needs the tile names).
    """

    def __init__(self, finder: Callable[[Image.Image], Image.Image | None]) -> None:
        self.finder = finder
        self.next_at = 0.0
        self.last_full: float | None = None
        self.had_caption = False

    def due(self, now: float) -> bool:
        return now >= self.next_at

    def hurry(self, now: float) -> None:
        """A faster cadence arrived: don't wait out the slow interval already scheduled."""
        self.next_at = min(self.next_at, now)

    def frames(self, now: float, img: Image.Image, cadence: Cadence) -> list[tuple[str, bytes]]:
        self.next_at = now + 1.0 / cadence.fps
        crop = self.finder(img)
        out: list[tuple[str, bytes]] = []
        full_due = self.last_full is None or now - self.last_full >= cadence.full_frame_seconds
        if full_due or (crop is not None and not self.had_caption):
            out.append(("full", jpeg(fit_full(img), FULL_QUALITY)))
            self.last_full = now
        if crop is not None:
            out.append(("caption", jpeg(crop, CAPTION_QUALITY)))
        self.had_caption = crop is not None
        return out


@dataclass(frozen=True)
class ServerMessage:
    kind: str  # "cadence" | "notice" | "error" | "other"
    org_site_key: str | None = None
    cadence: Cadence | None = None
    text: str = ""


def parse_server(raw: str | bytes) -> ServerMessage:
    try:
        msg = json.loads(raw)
    except ValueError:
        return ServerMessage("other")
    if not isinstance(msg, dict):
        return ServerMessage("other")
    key = msg.get("orgSiteKey") if isinstance(msg.get("orgSiteKey"), str) else None
    kind = msg.get("type")
    if kind == "screen.cadence":
        fps, full = msg.get("fps"), msg.get("fullFrameSeconds")
        if (
            isinstance(fps, int | float)
            and isinstance(full, int | float)
            and not isinstance(fps, bool)
            and fps > 0
            and full > 0
        ):
            # Bound what a server can ask for: at most 5 fps, a full frame at most every 5 s.
            cad = Cadence(fps=min(float(fps), 5.0), full_frame_seconds=max(float(full), 5.0))
            return ServerMessage("cadence", key, cad)
        return ServerMessage("other")
    if kind == "screen.notice" and key is not None:
        return ServerMessage("notice", key, text=str(msg.get("kind", "")))
    if kind == "error":
        return ServerMessage("error", key, text=str(msg.get("message", ""))[:500])
    return ServerMessage("other")


def version_tuple(v: str) -> tuple[int, int, int] | None:
    parts = v.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def too_old(mine: str, minimum: str | None) -> bool:
    """True when this build is older than the server's minimum. A dev build never is."""
    if not minimum:
        return False
    have, need = version_tuple(mine), version_tuple(minimum)
    return have is not None and need is not None and have < need
