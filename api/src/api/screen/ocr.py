"""Client for the tenir-ocr service (XERK-2505; engine and placement per XERK-2504).

Contract (docs/meeting-captions.md, "tenir-ocr API"): ``POST {API_OCR_URL}/v1/ocr`` with the
JPEG as the body (``Content-Type: image/jpeg``) answers
``{"width": int, "height": int, "lines": [{"text": str, "box": [x0, y0, x1, y1], "score": float}]}``
in the image's pixel coordinates. The service owns the engine details (PP-OCRv5 mobile, the
detector border a tight caption crop needs); this side only reads positioned text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


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


# More text lines than any meeting window shows; the rest of a crafted result is dropped so
# parsing and merging stay cheap (they run per frame).
MAX_LINES = 400


class OcrError(RuntimeError):
    """The OCR service failed or answered something this client can't read."""


class OcrClient(Protocol):
    async def read(self, jpeg: bytes) -> OcrResult: ...


def parse_result(payload: object) -> OcrResult:
    """Validate a tenir-ocr response body into an ``OcrResult``; raises ``OcrError``."""
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
        return OcrResult(
            width=int(payload["width"]), height=int(payload["height"]), lines=tuple(lines)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OcrError(f"unreadable OCR response: {exc!r}") from exc


class HttpOcrClient:
    """Posts frames to tenir-ocr. One pooled client per process."""

    def __init__(self, base_url: str, *, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def read(self, jpeg: bytes) -> OcrResult:
        try:
            resp = await self._client.post(
                "/v1/ocr", content=jpeg, headers={"Content-Type": "image/jpeg"}
            )
            resp.raise_for_status()
            return parse_result(resp.json())
        except httpx.HTTPError as exc:
            raise OcrError(f"OCR request failed: {exc!r}") from exc
        except ValueError as exc:  # body is not JSON
            raise OcrError(f"OCR response is not JSON: {exc!r}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
