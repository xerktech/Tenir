"""Client for the tenir-ocr service (XERK-2505 → XERK-2512's ``ocr/``; contract in ocr/README.md).

``POST {API_OCR_URL}/v1/ocr`` takes one image of at most ``OCR_MAX_PIXELS`` (0.5 Mpx) and
answers ``{"lines": [{"text", "score", "box": [x0, y0, x1, y1]}], "too_dense", "ms"}`` in the
image's pixels. ``too_dense`` (more than 16 text boxes: a slide, not captions) comes with no
lines. 413 is over the size cap, 503 a busy pool: both drop this frame, the next one retries.

A caption-box crop fits in one request. A full window frame (read for the participant tiles,
the active-speaker border and the title) does not, so it is cut into overlapping horizontal
strips under the cap, read one by one, and its lines shifted back into window coordinates.
A strip that is too dense (a shared slide) just contributes nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Protocol

import httpx

# More text lines than any meeting window shows; the rest of a crafted result is dropped so
# parsing and merging stay cheap (they run per frame).
MAX_LINES = 400
# Kept under tenir-ocr's OCR_MAX_PIXELS (500,000) so a strip is never refused with 413.
STRIP_PIXELS = 480_000
# Strips overlap so a text line cut by one boundary is whole in the next strip.
STRIP_OVERLAP = 48
# A line box this close to a strip's cut edge was cut by it.
_EDGE = 3
# The largest frame the api decodes to cut into strips (a 4K window and a little more).
MAX_FRAME_PIXELS = 3840 * 2400


@dataclass(frozen=True)
class OcrLine:
    text: str
    # x0, y0, x1, y1 in image pixels, y down.
    box: tuple[float, float, float, float]
    score: float = 1.0

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass(frozen=True)
class OcrResult:
    width: int
    height: int
    lines: tuple[OcrLine, ...]


class OcrError(RuntimeError):
    """The OCR service failed or answered something this client can't read."""


class OcrClient(Protocol):
    async def read(self, jpeg: bytes) -> OcrResult: ...


def parse_lines(payload: object) -> list[OcrLine]:
    """The lines of a tenir-ocr response body; raises ``OcrError``."""
    if not isinstance(payload, dict):
        raise OcrError("unreadable OCR response: not an object")
    try:
        lines = []
        for raw in (payload.get("lines") or [])[:MAX_LINES]:
            x0, y0, x1, y1 = (float(v) for v in raw["box"])
            text = str(raw["text"]).strip()
            if text:
                score = float(raw.get("score", 1.0))
                lines.append(OcrLine(text=text, box=(x0, y0, x1, y1), score=score))
        return lines
    except (KeyError, TypeError, ValueError) as exc:
        raise OcrError(f"unreadable OCR response: {exc!r}") from exc


def parse_result(payload: object, size: tuple[int, int]) -> OcrResult:
    """A whole-image tenir-ocr response as an ``OcrResult`` of an image of ``size``."""
    return OcrResult(width=size[0], height=size[1], lines=tuple(parse_lines(payload)))


def image_size(data: bytes) -> tuple[int, int]:
    """Width and height from the image header, without decoding the pixels."""
    from PIL import Image

    try:
        return Image.open(io.BytesIO(data)).size
    except Exception as exc:
        raise OcrError(f"not a readable image: {exc!r}") from exc


def strips(width: int, height: int) -> list[tuple[int, int]]:
    """``(top, bottom)`` rows of overlapping strips of at most ``STRIP_PIXELS`` each."""
    step_h = max(STRIP_OVERLAP * 2, STRIP_PIXELS // max(1, width))
    out, top = [], 0
    while True:
        bottom = min(height, top + step_h)
        out.append((top, bottom))
        if bottom >= height:
            return out
        top = bottom - STRIP_OVERLAP


def _dedupe(lines: list[OcrLine]) -> list[OcrLine]:
    """Drop the second reading of a line two overlapping strips both saw whole."""
    kept: list[OcrLine] = []
    for ln in sorted(lines, key=lambda ln: (ln.box[1], ln.box[0])):
        if any(
            k.text == ln.text and abs(k.box[1] - ln.box[1]) < 8 and abs(k.box[0] - ln.box[0]) < 8
            for k in kept
        ):
            continue
        kept.append(ln)
    return kept


class HttpOcrClient:
    """Posts frames to tenir-ocr. One pooled client per process."""

    def __init__(self, base_url: str, *, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def _post(self, image: bytes) -> object:
        try:
            resp = await self._client.post(
                "/v1/ocr", content=image, headers={"Content-Type": "image/jpeg"}
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OcrError(f"OCR request failed: {exc!r}") from exc
        except ValueError as exc:  # body is not JSON
            raise OcrError(f"OCR response is not JSON: {exc!r}") from exc

    async def read(self, jpeg: bytes) -> OcrResult:
        width, height = image_size(jpeg)
        if width * height <= STRIP_PIXELS:
            return parse_result(await self._post(jpeg), (width, height))
        if width * height > MAX_FRAME_PIXELS:
            raise OcrError(f"frame too large to read: {width}x{height}")
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        lines: list[OcrLine] = []
        for top, bottom in strips(width, height):
            buf = io.BytesIO()
            img.crop((0, top, width, bottom)).save(buf, format="JPEG", quality=90)
            for ln in parse_lines(await self._post(buf.getvalue())):
                # A line touching a cut edge is a fragment ("Priya Ra"); the overlap is taller
                # than a text line, so the neighbouring strip reads it whole.
                if (top > 0 and ln.box[1] <= _EDGE) or (
                    bottom < height and ln.box[3] >= bottom - top - _EDGE
                ):
                    continue
                lines.append(
                    OcrLine(
                        ln.text, (ln.box[0], ln.box[1] + top, ln.box[2], ln.box[3] + top), ln.score
                    )
                )
        return OcrResult(width=width, height=height, lines=tuple(_dedupe(lines)[:MAX_LINES]))

    async def aclose(self) -> None:
        await self._client.aclose()
